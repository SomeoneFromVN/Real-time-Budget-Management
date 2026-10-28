"""Queue weekly summaries and corrections inside the caller's SQLite transaction.

Legacy sent weeks get observed-data baselines, not reconstructed originals. Existing
transaction weeks and latest pre-marker week are baselined; older empty weeks cannot
be inferred from the legacy marker alone. Reports remain queued, then corrections
enter the outbox after originals.
"""

import os
import re
import sqlite3
from datetime import date, datetime, timedelta

from . import notification_outbox
from budget.budget_history import budget_for_period
from common import SQLITE_INT64_MAX, apply_notification_options, format_vnd as _format_vnd


def _monday(day):
    return day - timedelta(days=day.weekday())


def _week_spent(connection, account, start):
    return connection.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions "
        "WHERE account = ? AND day >= ? AND day < ?",
        (account, start.isoformat(), (start + timedelta(days=7)).isoformat()),
    ).fetchone()[0]


def _week_snapshot(connection, account, start, spent=None, *, today):
    if spent is None:
        spent = _week_spent(connection, account, start)
    allowance, unknown_days = budget_for_period(connection, account, start, start + timedelta(days=7), today)
    return spent, allowance, unknown_days


_DAY_PREFIX = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _spent_by_week(connection, account, before):
    """Sum stored spending per Monday-start week in one ledger pass.

    Returns None when any row cannot be bucketed exactly like _week_snapshot's
    range query; the caller then uses one SUM per week.
    """
    totals = {}
    for day_text, amount in connection.execute(
        "SELECT day, amount FROM transactions WHERE account = ? AND day < ?",
        (account, before.isoformat()),
    ):
        if type(amount) is not int or amount < 0 or not isinstance(day_text, str) or not _DAY_PREFIX.fullmatch(day_text[:10]):
            return None
        try:
            week = _monday(date.fromisoformat(day_text[:10])).isoformat()
        except ValueError:
            return None
        totals[week] = totals.get(week, 0) + amount
    if any(abs(total) > SQLITE_INT64_MAX for total in totals.values()):
        return None
    return totals


def _history_health(connection, account):
    """Read current account evidence, not proof of any particular week's coverage."""
    evidence = {"scope": "current_account", "gap_suspected": None, "history_unknown": None}
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'history_health'"
    ).fetchone() is None:
        return evidence
    columns = {row[1] for row in connection.execute("PRAGMA table_info(history_health)")}
    if "account" not in columns:
        return evidence
    flags = [name for name in ("gap_suspected", "history_unknown") if name in columns]
    if not flags:
        return evidence
    row = connection.execute(
        f"SELECT {', '.join(flags)} FROM history_health WHERE account = ?", (account,)
    ).fetchone()
    if row is not None:
        for name, value in zip(flags, row):
            if type(value) is int and value in (0, 1):
                evidence[name] = value
    return evidence


def _payload(account, start, revision, snapshot, previous=None, legacy=False, *, history_health):
    spent, allowance, unknown_days = snapshot
    uncertain = any(history_health[name] != 0 for name in ("gap_suspected", "history_unknown"))
    end = start + timedelta(days=6)
    lines = [f"Tuần: {start:%d/%m/%Y}–{end:%d/%m/%Y}"]
    if unknown_days:
        lines.extend((
            f"Ngân sách đã biết: {_format_vnd(allowance)}",
            f"Ngày chưa biết ngân sách: {unknown_days}",
            "Không thể xác định tiết kiệm/vượt ngân sách cả tuần do thiếu lịch sử ngân sách.",
        ))
    else:
        net = allowance - spent
        lines.append(f"Ngân sách tuần: {_format_vnd(allowance)}")
        if uncertain:
            signed_net = ("-" if net < 0 else "") + _format_vnd(net)
            lines.append(f"Chênh lệch ngân sách tạm tính từ dữ liệu đã ghi nhận: {signed_net}")
        elif net > 0:
            lines.append(f"Tiết kiệm theo dữ liệu đã ghi nhận: {_format_vnd(net)}")
        elif net < 0:
            lines.append(f"Vượt ngân sách theo dữ liệu đã ghi nhận: {_format_vnd(-net)}")
        else:
            lines.append("Không thặng dư, không vượt ngân sách theo dữ liệu đã ghi nhận (0đ)")
    lines.append(f"Chi đã ghi nhận: {_format_vnd(spent)}")
    payload = {
        "title": "Tổng kết ngân sách tuần",
        "week_start": start.isoformat(),
        "revision": revision,
        "spent": spent,
        "allowance": allowance,
        "unknown_days": unknown_days,
        "history_health": history_health,
    }
    if previous is not None:
        delta = spent - previous[0]
        basis = "mốc dữ liệu đã quan sát" if legacy else "phiên bản trước"
        delta_text = ("+" if delta > 0 else "-" if delta < 0 else "") + _format_vnd(delta)
        payload["title"] = "Điều chỉnh tổng kết tuần"
        payload["delta_spent"] = delta
        payload["delta_basis"] = "observed_baseline" if legacy else "previous_revision"
        lines.extend((
            f"Tổng chi cập nhật: {_format_vnd(spent)}",
            f"Delta chi (chênh lệch chi) so với {basis}: {delta_text}",
        ))
    if uncertain:
        reasons = []
        if history_health["gap_suspected"] == 1:
            reasons.append("nghi có khoảng trống")
        elif history_health["gap_suspected"] is None:
            reasons.append("chưa xác định tình trạng khoảng trống")
        if history_health["history_unknown"] == 1:
            reasons.append("chưa xác minh độ bao phủ")
        elif history_health["history_unknown"] is None:
            reasons.append("chưa xác định trạng thái xác minh")
        lines.append(
            f"Cảnh báo lịch sử giao dịch cấp tài khoản hiện tại: {', '.join(reasons)}; "
            "số liệu chỉ tạm tính, chưa thể kết luận tiết kiệm/vượt ngân sách cả tuần. "
            "Đây không phải bằng chứng xác minh riêng tuần này."
        )
    else:
        lines.append("Lịch sử giao dịch có thể chưa đầy đủ; trạng thái cấp tài khoản "
                     "không xác minh riêng tuần này.")
    payload["body"] = "\n".join(lines)
    return apply_notification_options(payload, os.environ)


def _ensure_tables(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS weekly_summary_state ("
        "account TEXT PRIMARY KEY, next_week TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS weekly_report_revisions ("
        "account TEXT NOT NULL, week_start TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1), "
        "spent INTEGER NOT NULL, allowance INTEGER NOT NULL, unknown_days INTEGER NOT NULL, "
        "event_id INTEGER, event_key TEXT, kind TEXT NOT NULL CHECK(kind IN ('legacy', 'original', 'correction')), "
        "PRIMARY KEY(account, week_start, revision), UNIQUE(account, event_key))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS weekly_report_migrations (account TEXT PRIMARY KEY)"
    )


def _insert_revision(connection, account, week_text, revision, snapshot, kind,
                     event_id=None, event_key=None):
    connection.execute(
        "INSERT INTO weekly_report_revisions "
        "(account, week_start, revision, spent, allowance, unknown_days, event_id, event_key, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account, week_text, revision, *snapshot, event_id, event_key, kind),
    )


def _queue_revision(connection, account, start, week_text, revision, snapshot, previous=None,
                    legacy=False, *, history_health):
    event_key = f"weekly:{week_text}:r{revision}"
    payload = _payload(
        account, start, revision, snapshot, previous=previous, legacy=legacy,
        history_health=history_health,
    )
    event_id = notification_outbox.enqueue(connection, account, event_key, payload)
    kind = "original" if previous is None else "correction"
    _insert_revision(connection, account, week_text, revision, snapshot, kind, event_id, event_key)
    return event_key


def _baseline_legacy_weeks(connection, account, marker, today):
    current_week = _monday(today)
    migrated = connection.execute(
        "SELECT 1 FROM weekly_report_migrations WHERE account = ?", (account,)
    ).fetchone()
    if migrated:
        return

    starts = set()
    rows = connection.execute(
        "SELECT DISTINCT day FROM transactions WHERE account = ? AND day < ?",
        (account, marker.isoformat()),
    ).fetchall()
    for (day_text,) in rows:
        starts.add(_monday(date.fromisoformat(day_text[:10])))
    latest_completed = marker - timedelta(days=7)
    if latest_completed < current_week:
        starts.add(latest_completed)

    for start in sorted(starts):
        if start >= marker:
            continue
        exists = connection.execute(
            "SELECT 1 FROM weekly_report_revisions WHERE account = ? AND week_start = ? LIMIT 1",
            (account, start.isoformat()),
        ).fetchone()
        if exists:
            continue
        snapshot = _week_snapshot(connection, account, start, today=today)
        _insert_revision(connection, account, start.isoformat(), 1, snapshot, "legacy")
    connection.execute(
        "INSERT INTO weekly_report_migrations(account) VALUES (?)", (account,)
    )


def prepare_legacy_baseline(connection, account, today):
    """Capture stored data for sent legacy weeks before importing new rows."""
    if not connection.in_transaction:
        raise RuntimeError("prepare_legacy_baseline requires a caller-owned transaction")
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account must be a nonempty string")
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date")

    _ensure_tables(connection)
    marker_row = connection.execute(
        "SELECT next_week FROM weekly_summary_state WHERE account = ?", (account,)
    ).fetchone()
    if marker_row is not None:
        _baseline_legacy_weeks(connection, account, date.fromisoformat(marker_row[0]), today)


def queue_weekly_reports(connection, account, today):
    """Queue reports; caller must own transaction and decide commit or rollback.

    At most one unreported original week advances per call. All changed tracked
    weeks can receive corrections; FIFO outbox IDs keep each later correction
    behind any already queued original, whether that original was delivered or not.
    """
    if not connection.in_transaction:
        raise RuntimeError("queue_weekly_reports requires a caller-owned transaction")
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account must be a nonempty string")
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date")

    _ensure_tables(connection)
    current_week = _monday(today)
    marker_row = connection.execute(
        "SELECT next_week FROM weekly_summary_state WHERE account = ?", (account,)
    ).fetchone()
    if marker_row is None:
        connection.execute(
            "INSERT INTO weekly_summary_state(account, next_week) VALUES (?, ?)",
            (account, current_week.isoformat()),
        )
        connection.execute(
            "INSERT INTO weekly_report_migrations(account) VALUES (?)", (account,)
        )
        return []

    marker = date.fromisoformat(marker_row[0])
    _baseline_legacy_weeks(connection, account, marker, today)
    queued = []
    history_health = _history_health(connection, account)
    spent_by_week = _spent_by_week(connection, account, current_week)

    latest_revisions = connection.execute(
        "SELECT week_start, revision, spent, allowance, unknown_days, kind "
        "FROM weekly_report_revisions AS latest "
        "WHERE account = ? AND week_start < ? AND revision = ("
        "SELECT MAX(revision) FROM weekly_report_revisions "
        "WHERE account = latest.account AND week_start = latest.week_start) "
        "ORDER BY week_start",
        (account, current_week.isoformat()),
    ).fetchall()
    for week_text, old_revision, old_spent, old_allowance, old_unknown, old_kind in latest_revisions:
        start = date.fromisoformat(week_text)
        previous = (old_spent, old_allowance, old_unknown)
        # The one-pass totals are keyed by Monday; any other stored start uses the range SUM.
        known = spent_by_week is not None and start.weekday() == 0 and start.isoformat() == week_text
        spent = spent_by_week.get(week_text, 0) if known else _week_spent(connection, account, start)
        # Only a change in spending queues a correction. A month close (allowance) or an
        # account-level history flag change alone does not, so skip before the allowance is computed.
        if spent == old_spent:
            continue
        snapshot = _week_snapshot(connection, account, start, spent, today=today)
        queued.append(_queue_revision(
            connection, account, start, week_text, old_revision + 1, snapshot, previous=previous,
            legacy=(old_kind == "legacy"), history_health=history_health,
        ))

    if marker < current_week:
        snapshot = _week_snapshot(connection, account, marker, today=today)
        event_key = _queue_revision(
            connection, account, marker, marker.isoformat(), 1, snapshot,
            history_health=history_health,
        )
        next_week = marker + timedelta(days=7)
        connection.execute(
            "UPDATE weekly_summary_state SET next_week = ? WHERE account = ?",
            (next_week.isoformat(), account),
        )
        queued.append(event_key)
    return queued
