"""Persist runtime outcomes; write APIs require caller-owned transactions."""

from __future__ import annotations

from datetime import datetime
import sqlite3
import sys

from common import STALE_AFTER_SECONDS, SQLITE_INT64_MAX, table_columns
from monitoring import sync_state as sync_status
from notifications.notification_outbox import enqueue


STAGES = ("bank_api", "validation", "financial_save", "weekly_report", "outbox", "backup")
_STATUSES = frozenset({"running", "healthy", "failed", "skipped"})
_ERROR_CODES = frozenset({"request", "validation", "database", "unexpected", "report", "delivery", "backup"})
_STEP_COLUMNS = {
    "account", "step", "status", "last_checked_at", "last_attempt_at",
    "last_success_at", "last_error_code", "consecutive_failures",
}
_SYNC_COLUMNS = {"account", "last_attempt_at", "last_success_at", "last_error_code", "consecutive_failures"}


def _validate(account: str, at: datetime) -> str:
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account must be a nonempty string")
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be an aware datetime")
    return at.isoformat()


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("caller must begin a transaction")


def _aware_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed
    except (ValueError, OverflowError):
        pass
    return None


def _age(value: object, now: datetime) -> float | None:
    parsed = _aware_time(value)
    if parsed is None:
        return None
    try:
        elapsed = (now - parsed).total_seconds()
    except (ValueError, OverflowError):
        return None
    return elapsed if elapsed >= 0 else None


def _safe_step_error(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in _ERROR_CODES else "unexpected"


def record_step(
    connection: sqlite3.Connection,
    account: str,
    step: str,
    status: str,
    at: datetime,
    error_code: str | None = None,
) -> None:
    """Record outcome without committing; running/skipped retain prior failure context."""
    timestamp = _validate(account, at)
    if not isinstance(step, str) or step not in STAGES:
        raise ValueError("invalid step")
    if not isinstance(status, str) or status not in _STATUSES:
        raise ValueError("invalid status")
    if error_code is not None and (not isinstance(error_code, str) or error_code not in _ERROR_CODES):
        raise ValueError("invalid error_code")
    _require_transaction(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS step_status (
            account TEXT NOT NULL,
            step TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running', 'healthy', 'failed', 'skipped')),
            last_checked_at TEXT NOT NULL,
            last_attempt_at TEXT,
            last_success_at TEXT,
            last_error_code TEXT,
            consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
            PRIMARY KEY (account, step)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO step_status
            (account, step, status, last_checked_at, last_attempt_at,
             last_success_at, last_error_code, consecutive_failures)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account, step) DO UPDATE SET
            status = excluded.status,
            last_checked_at = excluded.last_checked_at,
            last_attempt_at = CASE WHEN excluded.status = 'skipped'
                THEN step_status.last_attempt_at ELSE excluded.last_attempt_at END,
            last_success_at = CASE WHEN excluded.status = 'healthy'
                THEN excluded.last_success_at ELSE step_status.last_success_at END,
            last_error_code = CASE WHEN excluded.status IN ('running', 'skipped')
                THEN step_status.last_error_code ELSE excluded.last_error_code END,
            consecutive_failures = CASE
                WHEN excluded.status = 'healthy' THEN 0
                WHEN excluded.status = 'failed' THEN step_status.consecutive_failures + 1
                ELSE step_status.consecutive_failures END
        """,
        (
            account, step, status, timestamp,
            None if status == "skipped" else timestamp,
            timestamp if status == "healthy" else None,
            error_code if status == "failed" else None,
            1 if status == "failed" else 0,
        ),
    )


def start_cycle(connection: sqlite3.Connection, account: str, at: datetime) -> None:
    """Mark current-cycle entry; unattempted downstream steps become skipped."""
    _validate(account, at)
    _require_transaction(connection)
    sync_status.ensure_sync_schema(connection)
    for step in STAGES:
        record_step(connection, account, step, "running" if step == "bank_api" else "skipped", at)


def read_steps(
    connection: sqlite3.Connection, account: str, now: datetime
) -> dict[str, dict[str, object]]:
    """Read only. Skipped freshness proves a check, not an attempt or success."""
    _validate(account, now)
    columns = table_columns(connection, "step_status")
    fallback = "absent" if not columns else "unknown"
    result = {
        step: {
            "status": fallback,
            "last_checked_at": None,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error_code": None,
            "consecutive_failures": None,
            "freshness": "unknown",
        }
        for step in STAGES
    }
    if not _STEP_COLUMNS.issubset(columns):
        return result
    rows = connection.execute(
        "SELECT step, status, last_checked_at, last_attempt_at, last_success_at, "
        "last_error_code, consecutive_failures FROM step_status WHERE account = ?",
        (account,),
    )
    for step, status, checked, attempted, succeeded, error, failures in rows:
        if step not in result:
            continue
        age = _age(checked, now)
        result[step] = {
            "status": status if status in _STATUSES else "unknown",
            "last_checked_at": checked,
            "last_attempt_at": attempted,
            "last_success_at": succeeded,
            "last_error_code": _safe_step_error(error),
            "consecutive_failures": failures if type(failures) is int and failures >= 0 else None,
            "freshness": "unknown" if age is None else "fresh" if age <= STALE_AFTER_SECONDS else "stale",
        }
    return result


def _sync_alert(connection: sqlite3.Connection, account: str, at: datetime) -> tuple[str, dict | None] | None:
    """Classify valid sync metadata. An incident has a safe payload; a recovery has none."""
    if not _SYNC_COLUMNS.issubset(table_columns(connection, "sync_status")):
        return None
    row = sync_status.read_sync_status(connection, account)
    if row is None:
        return None
    attempted = row["last_attempt_at"]
    succeeded = row["last_success_at"]
    error = row["last_error_code"]
    failures = row["consecutive_failures"]
    if type(failures) is not int or failures < 0:
        return None
    success_age = _age(succeeded, at)
    if failures > 0 and (
        failures >= 3 or (success_age is not None and success_age > STALE_AFTER_SECONDS)
    ):
        success_text = _aware_time(succeeded).isoformat() if success_age is not None else "unknown"
        return "incident", {
            "title": "C\u1ea3nh b\u00e1o \u0111\u1ed3ng b\u1ed9",
            "body": (
                f"L\u1ed7i li\u00ean ti\u1ebfp: {failures}. "
                f"L\u1ea7n th\u00e0nh c\u00f4ng cu\u1ed1i: {success_text}. "
                f"M\u00e3 l\u1ed7i: {sync_status.safe_sync_error(error) or 'unexpected'}."
            ),
        }
    attempt_age = _age(attempted, at)
    if (failures == 0 and error is None
            and success_age is not None and success_age <= STALE_AFTER_SECONDS
            and attempt_age is not None and attempt_age <= success_age):
        return "recovery", None
    return None


def _alert_transition(connection: sqlite3.Connection, account: str, kind: str) -> tuple | None:
    """Read persisted state; reject invalid metadata and duplicate transitions."""
    columns = table_columns(connection, "sync_health_alerts")
    if columns and not {"account", "active", "sequence"}.issubset(columns):
        return None
    state = connection.execute(
        "SELECT active, sequence FROM sync_health_alerts WHERE account = ?", (account,)
    ).fetchone() if columns else None
    active, sequence = (0, 0) if state is None else state
    if type(active) is not int or active not in (0, 1):
        return None
    if type(sequence) is not int or sequence < active:
        return None
    next_active = int(kind == "incident")
    if next_active == active:
        return None
    return state, active, sequence, next_active


def queue_sync_alert(connection: sqlite3.Connection, account: str, at: datetime) -> str | None:
    """Queue the incident event once per persisted transition, without transport or commit.

    A recovery only closes the incident so the next one can alert; it sends no event and returns None.
    """
    # alerts need a running process; add an external watchdog for process outages.
    _validate(account, at)
    _require_transaction(connection)
    alert = _sync_alert(connection, account, at)
    if alert is None:
        return None
    kind, payload = alert
    transition = _alert_transition(connection, account, kind)
    if transition is None:
        return None
    state, active, sequence, next_active = transition
    if sequence >= SQLITE_INT64_MAX:
        raise ValueError("alert sequence exhausted")
    sequence += 1
    event_key = f"health:sync:{sequence}:{kind}"
    connection.execute("SAVEPOINT runtime_health_alert")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sync_health_alerts ("
            "account TEXT PRIMARY KEY, "
            "active INTEGER NOT NULL CHECK (active IN (0, 1)), "
            "sequence INTEGER NOT NULL)"
        )
        if state is None:
            connection.execute(
                "INSERT INTO sync_health_alerts(account, active, sequence) VALUES (?, ?, ?)",
                (account, next_active, sequence),
            )
        else:
            cursor = connection.execute(
                "UPDATE sync_health_alerts SET active = ?, sequence = ? "
                "WHERE account = ? AND active = ? AND sequence = ?",
                (next_active, sequence, account, active, sequence - 1),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("sync alert state changed during transition")
        if kind == "incident":
            enqueue(connection, account, event_key, payload)
    except BaseException:
        connection.execute("ROLLBACK TO runtime_health_alert")
        connection.execute("RELEASE runtime_health_alert")
        raise
    connection.execute("RELEASE runtime_health_alert")
    return event_key if kind == "incident" else None


def _self_check() -> None:
    """Run offline SQLite checks: python -m monitoring.runtime_health."""
    from tests import run_checks

    run_checks(
        sys.modules[__name__], "monitoring.runtime_health",
        "tests.test_monitoring_refactor.StepTests",
        "tests.test_monitoring_refactor.RuntimeAlertTests",
    )
    print("runtime_health self-check passed")


if __name__ == "__main__":
    _self_check()
