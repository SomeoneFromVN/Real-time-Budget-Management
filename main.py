import argparse
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
try:
    from dotenv import load_dotenv
except ImportError:
    raise SystemExit("Thiếu python-dotenv; chạy python -m pip install -r requirements.txt.") from None
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from common import (SQLITE_INT64_MAX, VIETNAM_TZ, apply_notification_options,
                    format_vnd, parse_budget_amount)
import ledger_edits
from notifications.budget_alerts import mark_alerts, parse_alert_settings, pending_alerts
from budget import income_budget
from budget.budget_history import budget_for_day, record_monthly_budget
from notifications import notification_outbox, weekly_reports
from storage.db_backup import backup_database, snapshot_is_fresh
from monitoring.history_health import (
    RECONCILIATION_REASON_MAX_LENGTH,
    reconcile_history,
    record_history_window,
)
from storage.instance_lock import InstanceAlreadyRunningError, single_instance
from monitoring.sync_status import (
    snapshot_outbox,
    read_status_snapshot,
    record_sync_failure,
    record_sync_success,
)
from monitoring.preflight import check_startup
from monitoring.runtime_health import STAGES, queue_sync_alert, record_step, start_cycle
from monitoring.runtime_log import close_logging, configure_logging, redact, write_log

if not any(flag in sys.argv for flag in ("--self-check", "--help", "-h")):
    load_dotenv()
_KNOWN_DATABASES = set()
_MAX_RESPONSE_BYTES = 1024 * 1024
_REQUEST_DEADLINE_SECONDS = 60
_FETCH_RETRIES = 2
_FETCH_RETRY_DELAY_SECONDS = 5
_SNAPSHOT_MAX_AGE_SECONDS = 6 * 60 * 60


def log_status(message, at=None):
    at = at or datetime.now(VIETNAM_TZ)
    line = redact(f"[{at:%Y-%m-%d %H:%M:%S} UTC+07:00] {message}")
    write_log(line)
    try:
        print(line, flush=True)
    except (OSError, UnicodeError):
        pass


def redact_secrets(message):
    return redact(message)


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Thiếu biến {name} trong file .env")
    return value


def request_json(request, service_name):
    deadline = time.monotonic() + _REQUEST_DEADLINE_SECONDS
    try:
        with urlopen(request, timeout=20) as response:
            # timeout=20 is per socket read; the deadline stops slow-drip responses.
            chunks = []
            size = 0
            while True:
                chunk = response.read1(min(65536, _MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    raise SystemExit(f"{service_name} trả phản hồi vượt giới hạn 1 MiB.")
                if time.monotonic() >= deadline:
                    raise SystemExit(f"{service_name} phản hồi quá thời gian.")
            return json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    except HTTPError as error:
        raise SystemExit(f"{service_name} trả HTTP {error.code}.")
    except URLError as error:
        raise SystemExit(f"Không kết nối được tới {service_name}: {error.reason}")
    except json.JSONDecodeError:
        raise SystemExit(f"{service_name} không trả về JSON hợp lệ.")


def transaction_day(transaction, kind):
    date_text = str(transaction.get("transactionDate", "")).strip()
    try:
        return datetime.strptime(
            date_text.split(maxsplit=1)[0] if date_text else "", "%d/%m/%Y"
        ).date()
    except ValueError:
        raise SystemExit(f"Không đọc được transactionDate ở giao dịch {kind}: {date_text}")


def transaction_metadata(transaction):
    transaction_time = None
    date_text = transaction.get("transactionDate")
    if isinstance(date_text, str):
        date_text = date_text.strip()
        match = re.fullmatch(r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4} ([0-9]{2}:[0-9]{2}(?::[0-9]{2})?)", date_text)
        if match:
            clock = match.group(1)
            try:
                datetime.strptime(date_text, "%d/%m/%Y %H:%M:%S" if len(clock) == 8 else "%d/%m/%Y %H:%M")
            except ValueError:
                pass
            else:
                transaction_time = clock
    # Optional display fields must not block valid financial transactions.
    note = transaction.get("description")
    if not isinstance(note, str) or len(note) > 500:
        note = None
    else:
        note = "".join(
            char for char in " ".join(note.split())
            if unicodedata.category(char) not in ("Cc", "Cs")
            and char not in "\u061c\u200e\u200f"
            and unicodedata.bidirectional(char) not in ("LRE", "RLE", "PDF", "LRO", "RLO", "LRI", "RLI", "FSI", "PDI")
        ).strip() or None
    return transaction_time, note


def _prepare_transaction_metadata(connection):
    columns = {row[1].lower() for row in connection.execute("PRAGMA table_info(transactions)")}
    for column in ("transaction_time", "note"):
        if column not in columns:
            connection.execute(f"ALTER TABLE transactions ADD COLUMN {column} TEXT")


def _enrich_transaction_metadata(connection, account, metadata, table="transactions"):
    connection.executemany(
        f"UPDATE {table} SET transaction_time = COALESCE(transaction_time, ?), note = COALESCE(note, ?) "
        "WHERE account = ? AND transaction_id = ? "
        "AND ((transaction_time IS NULL AND ? IS NOT NULL) OR (note IS NULL AND ? IS NOT NULL))",
        ((clock, note, account, transaction_id, clock, note)
         for transaction_id, (clock, note) in metadata.items()),
    )


def _runtime_update(database_path, operation, *args):
    connection = None
    try:
        create = operation is start_cycle and database_path.resolve() not in _KNOWN_DATABASES
        mode = "rwc" if create else "rw"
        connection = sqlite3.connect(database_path.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=5, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        result = operation(connection, *args)
        connection.commit()
        if operation is queue_sync_alert and result:
            log_status(f"Sức khỏe đồng bộ: {result}; đã xếp cảnh báo vào outbox.")
        return True
    except Exception as error:
        if connection is not None and connection.in_transaction:
            connection.rollback()
        log_status(f"Không ghi được trạng thái vận hành ({type(error).__name__}).")
        return False
    finally:
        if connection is not None:
            connection.close()


def _step(database_path, account, step, status, error_code=None):
    _runtime_update(
        database_path, record_step, account, step, status,
        datetime.now(VIETNAM_TZ), error_code,
    )


def _queue_weekly_reports(database_path, account, today):
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        weekly_reports.queue_weekly_reports(connection, account, today)
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _weekly_best_effort(database_path, account, today):
    _step(database_path, account, "weekly_report", "running")
    try:
        _queue_weekly_reports(database_path, account, today)
    except (SystemExit, Exception) as error:
        _step(database_path, account, "weekly_report", "failed", "report")
        log_status(f"Tổng kết tuần lỗi ({type(error).__name__}): {redact_secrets(str(error))}")
    else:
        _step(database_path, account, "weekly_report", "healthy")


def _send_outbox_payload(payload):
    device_key = required_env("DEVICE_KEY")
    bark_request = Request(
        f"https://api.day.app/{quote(device_key, safe='')}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    return request_json(bark_request, "Bark")


def _dispatch_pending(database_path, account):
    if not database_path.is_file():
        return 0
    _step(database_path, account, "outbox", "running")
    connection = None
    try:
        connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
        sent = notification_outbox.drain(connection, account, _send_outbox_payload, limit=20)
        if sent:
            log_status(f"Đã gửi {sent} thông báo từ outbox.")
        pending = snapshot_outbox(connection, account)["pending"]
        if pending:
            log_status(f"Còn {pending} thông báo outbox chờ gửi.")
        _step(database_path, account, "outbox", "healthy")
        return sent
    except Exception as error:
        _step(database_path, account, "outbox", "failed", "delivery")
        log_status(f"Outbox lỗi; sự kiện còn chờ ({type(error).__name__}): {redact_secrets(str(error))}")
        return 0
    finally:
        if connection is not None:
            connection.close()


def _record_sync_failure_best_effort(database_path, account, error_code):
    connection = None
    recorded = False
    try:
        connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        record_sync_failure(connection, account, datetime.now(VIETNAM_TZ), error_code)
        connection.commit()
        recorded = True
    except Exception as error:
        if connection is not None and connection.in_transaction:
            connection.rollback()
        log_status(
            "Cảnh báo: không ghi được trạng thái đồng bộ "
            f"({type(error).__name__}); trạng thái có thể đã lỗi thời."
        )
    finally:
        if connection is not None:
            connection.close()
    if recorded:
        _runtime_update(database_path, queue_sync_alert, account, datetime.now(VIETNAM_TZ))


def _record_sync_success_in_transaction(connection, account):
    try:
        record_sync_success(connection, account, datetime.now(VIETNAM_TZ))
        return True
    except Exception as error:
        if not connection.in_transaction:
            raise
        log_status(
            "Cảnh báo: không ghi được trạng thái đồng bộ "
            f"({type(error).__name__}); trạng thái có thể đã lỗi thời."
        )
        return False


def _daily_event_key(account, day, transaction_ids):
    canonical = json.dumps(
        {"account": account, "day": day, "transaction_ids": sorted(transaction_ids)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "daily-" + hashlib.sha256(canonical).hexdigest()


def _conflict_event_key(account, conflict):
    transaction_id, stored_day, stored_amount, incoming_day, incoming_amount = conflict
    canonical = json.dumps(
        {"account": account, "transaction_id": transaction_id,
         "stored": [stored_day, stored_amount], "incoming": [incoming_day, incoming_amount]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "conflict-" + hashlib.sha256(canonical).hexdigest()


def _queue_conflict_alerts(connection, account, conflicts):
    for conflict in conflicts:
        event_key = _conflict_event_key(account, conflict)
        ledger_edits.record_conflict(connection, account, event_key, conflict, datetime.now(VIETNAM_TZ).isoformat())
        # enqueue() rejects a changed payload for an existing key; options can change between cycles.
        if notification_outbox.has_event(connection, account, event_key):
            continue
        transaction_id, stored_day, stored_amount, incoming_day, incoming_amount = conflict
        payload = {
            "title": "Giao dịch bị ngân hàng thay đổi",
            "body": (
                f"Mã giao dịch: {transaction_id}\n"
                f"Sổ đã ghi: {stored_day}, {format_vnd(stored_amount)}\n"
                f"Ngân hàng hiện trả: {incoming_day}, {format_vnd(incoming_amount)}\n"
                "Sổ giữ nguyên số đã ghi; tổng chi có thể lệch so với sao kê."
            ),
        }
        notification_outbox.enqueue(
            connection, account, event_key, apply_notification_options(payload, os.environ),
        )


def _ledger_row(transaction, kind):
    transaction_id = transaction.get("transactionID")
    if isinstance(transaction_id, bool) or not isinstance(transaction_id, (str, int)):
        raise SystemExit(f"transactionID không hợp lệ ở giao dịch {kind}.")
    if isinstance(transaction_id, str) and not transaction_id.strip():
        raise SystemExit(f"transactionID không hợp lệ ở giao dịch {kind}.")
    transaction_id = str(transaction_id)

    day = transaction_day(transaction, kind)
    amount = transaction.get("amount")
    maximum = str(SQLITE_INT64_MAX)
    if isinstance(amount, str):
        amount_text = amount.strip()
        digits = amount_text[1:] if amount_text[:1] in ("+", "-") else amount_text
        if not digits or not digits.isascii() or not digits.isdecimal():
            raise SystemExit(f"Amount không hợp lệ ở giao dịch {kind} {transaction_id}")
        normalized_digits = digits.lstrip("0") or "0"
        if (len(normalized_digits) > len(maximum) or
                (len(normalized_digits) == len(maximum) and normalized_digits > maximum)):
            raise SystemExit(f"Amount không hợp lệ ở giao dịch {kind} {transaction_id}")
        amount = int(normalized_digits)
        if amount_text.startswith("-"):
            amount = -amount
    if isinstance(amount, bool) or not isinstance(amount, int) or abs(amount) > SQLITE_INT64_MAX:
        raise SystemExit(f"Amount không hợp lệ ở giao dịch {kind} {transaction_id}")
    return transaction_id, (day.isoformat(), abs(amount))


def ledger_rows(transactions, out_metadata=None, in_metadata=None):
    out_rows = {}
    in_rows = {}
    for transaction in transactions:
        if not isinstance(transaction, dict):
            raise SystemExit("Mỗi giao dịch phải là object.")
        kind = transaction.get("type")
        kind = kind.strip().upper() if isinstance(kind, str) else None
        if kind not in ("IN", "OUT"):
            raise SystemExit("Trường type phải là IN hoặc OUT.")
        rows, metadata = (out_rows, out_metadata) if kind == "OUT" else (in_rows, in_metadata)

        transaction_id, row = _ledger_row(transaction, kind)
        previous = rows.get(transaction_id)
        if previous is not None and previous != row:
            raise SystemExit(f"transactionID bị trùng với dữ liệu khác ở giao dịch {kind}: {transaction_id}")
        rows[transaction_id] = row
        if metadata is not None:
            incoming = transaction_metadata(transaction)
            existing_metadata = metadata.get(transaction_id, (None, None))
            metadata[transaction_id] = tuple(old if old is not None else new for old, new in zip(existing_metadata, incoming))
    for transaction_id in in_rows:
        if transaction_id in out_rows:
            raise SystemExit(f"transactionID xuất hiện ở cả IN và OUT: {transaction_id}")
    return out_rows, in_rows


def _fail_cycle(database_path, account, stage, step_error_code, sync_error_code):
    _step(database_path, account, stage, "failed", step_error_code)
    _record_sync_failure_best_effort(database_path, account, sync_error_code)
    _dispatch_pending(database_path, account)


def _request_transactions(bank_request):
    bank_data = request_json(bank_request, "Bank API")
    if not isinstance(bank_data, dict):
        raise SystemExit("Bank API không trả về object hợp lệ.")
    if bank_data.get("status") != "success":
        raise SystemExit("Bank API báo lỗi; không có giao dịch được xử lý.")
    if "transactions" not in bank_data or not isinstance(bank_data["transactions"], list):
        raise SystemExit("Trường transactions không phải danh sách.")
    return bank_data["transactions"]


def _fetch(database_path, account, bank_request):
    # A failure is recorded, and counted toward the sync alert, only after the last retry fails.
    for attempt in range(_FETCH_RETRIES + 1):
        try:
            transactions = _request_transactions(bank_request)
            break
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            if attempt == _FETCH_RETRIES:
                _fail_cycle(database_path, account, "bank_api", "request", "bank_api")
                raise
            log_status(
                f"Lấy giao dịch lỗi ({redact_secrets(str(error))}); thử lại sau "
                f"{_FETCH_RETRY_DELAY_SECONDS} giây (lần {attempt + 1}/{_FETCH_RETRIES})."
            )
            time.sleep(_FETCH_RETRY_DELAY_SECONDS)
    _step(database_path, account, "bank_api", "healthy")
    return transactions


def _validate(database_path, account, transactions):
    _step(database_path, account, "validation", "running")
    try:
        out_metadata = {}
        in_metadata = {}
        out_rows, in_rows = ledger_rows(transactions, out_metadata=out_metadata, in_metadata=in_metadata)
    except KeyboardInterrupt:
        raise
    except BaseException:
        _fail_cycle(database_path, account, "validation", "validation", "validation")
        raise

    _step(database_path, account, "validation", "healthy")
    return out_rows, in_rows, out_metadata, in_metadata


def _after_commit(database_path, account, today_date, daily_committed, sync_recorded,
                  outbox_attempted, ledger_changed):
    outbox_sent = 0
    if daily_committed and sync_recorded:
        _runtime_update(database_path, queue_sync_alert, account, datetime.now(VIETNAM_TZ))
    if not outbox_attempted:
        outbox_sent = _dispatch_pending(database_path, account)
    backup_directory = Path(__file__).resolve().with_name("backups")
    # Skip while nothing changed and today's snapshot is fresh; the step stays "skipped".
    if daily_committed and (
        ledger_changed
        or not snapshot_is_fresh(backup_directory, today_date, _SNAPSHOT_MAX_AGE_SECONDS)
    ):
        _step(database_path, account, "backup", "running")
        try:
            backup_database(database_path, backup_directory, today_date)
        except Exception as error:
            _step(database_path, account, "backup", "failed", "backup")
            log_status(f"Sao lưu SQLite lỗi ({type(error).__name__}): {redact_secrets(str(error))}")
        else:
            _step(database_path, account, "backup", "healthy")

    return outbox_sent


def _log_budget(effective_monthly, monthly_budget, daily_budget, history_warnings):
    log_status(
        f"Ngân sách tháng dự kiến đang hiệu lực: {format_vnd(effective_monthly)} "
        f"(hôm nay: {format_vnd(daily_budget)})."
    )
    if effective_monthly != monthly_budget:
        log_status(
            f"Cấu hình ngân sách tháng {format_vnd(monthly_budget)} sẽ có hiệu lực từ ngày mai; "
            f"hôm nay dùng {format_vnd(effective_monthly)}."
        )
    for warning in history_warnings:
        log_status(warning)


def _log_actual_budget(connection, account, today_date):
    month = income_budget.month_key(today_date.year, today_date.month)
    try:
        figures = income_budget.month_figures(connection, account, month)
        if figures is None:
            return
        first = today_date.replace(day=1)
        following = (first + timedelta(days=32)).replace(day=1)
        spent = sum(income_budget.stored_amount(amount) for (amount,) in connection.execute(
            "SELECT amount FROM transactions WHERE account = ? AND day >= ? AND day < ?",
            (account, first.isoformat(), following.isoformat()),
        ))
    except sqlite3.Error as error:
        # The cycle is already committed; a failed read here must not mark it as failed.
        log_status(f"Không đọc được ngân sách thật ({type(error).__name__}).")
        return
    remaining = figures["pool"] - spent
    log_status(
        f"Ngân sách thật tháng này (từ tiền vào): {format_vnd(figures['pool'])}; "
        f"còn lại thật: {'-' if remaining < 0 else ''}{format_vnd(remaining)}."
    )


def _prepare_ledger(connection, account, today_date, rows, metadata, in_rows, in_metadata):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS accounts ("
        "account TEXT PRIMARY KEY, initialized INTEGER NOT NULL CHECK(initialized IN (0, 1)))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS transactions ("
        "account TEXT NOT NULL, transaction_id TEXT NOT NULL, day TEXT NOT NULL, amount INTEGER NOT NULL, "
        "PRIMARY KEY (account, transaction_id), "
        "FOREIGN KEY (account) REFERENCES accounts(account))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS incoming_transactions ("
        "account TEXT NOT NULL, transaction_id TEXT NOT NULL, day TEXT NOT NULL, amount INTEGER NOT NULL, "
        "transaction_time TEXT, note TEXT, "
        "PRIMARY KEY (account, transaction_id), "
        "FOREIGN KEY (account) REFERENCES accounts(account))"
    )
    _prepare_transaction_metadata(connection)
    connection.execute(
        "INSERT OR IGNORE INTO accounts(account, initialized) VALUES (?, 0)", (account,)
    )
    initialized = connection.execute(
        "SELECT initialized FROM accounts WHERE account = ?", (account,)
    ).fetchone()[0]
    weekly_reports.prepare_legacy_baseline(connection, account, today_date)
    conflicts = _drop_stored_conflicts(connection, "transactions", account, rows, metadata)
    _enrich_transaction_metadata(connection, account, metadata)
    # IN conflicts are only logged: no push and no conflict row.
    in_conflicts = _drop_stored_conflicts(connection, "incoming_transactions", account, in_rows, in_metadata)
    _enrich_transaction_metadata(connection, account, in_metadata, "incoming_transactions")
    return initialized, conflicts, in_conflicts


def _drop_stored_conflicts(connection, table, account, rows, metadata):
    """Return (id, stored day, stored amount, bank day, bank amount) for rows the bank changed."""
    conflicts = []
    for transaction_id, (day, amount) in rows.items():
        existing = connection.execute(
            f"SELECT day, amount FROM {table} WHERE account = ? AND transaction_id = ?",
            (account, transaction_id),
        ).fetchone()
        if existing is not None and existing != (day, amount):
            conflicts.append((transaction_id, existing[0], existing[1], day, amount))
    # Stored day/amount stay authoritative: drop revised copies so later steps cannot insert or enrich them.
    for conflict in conflicts:
        del rows[conflict[0]]
        metadata.pop(conflict[0], None)
    return conflicts


def _spent_today(connection, account, today_date):
    return connection.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account = ? AND day = ?",
        (account, today_date.isoformat()),
    ).fetchone()[0]


def _write_baseline(connection, account, today_date, rows, metadata, budget, alert_percent):
    connection.executemany(
        "INSERT OR IGNORE INTO transactions(account, transaction_id, day, amount, transaction_time, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ((account, transaction_id, day, amount, *metadata[transaction_id])
         for transaction_id, (day, amount) in rows.items()),
    )
    connection.execute("UPDATE accounts SET initialized = 1 WHERE account = ?", (account,))
    spent = _spent_today(connection, account, today_date)
    alerts = pending_alerts(connection, account, today_date, spent, budget, alert_percent)
    mark_alerts(connection, account, today_date, alerts)
    # Persist the first weekly marker before baseline commit.
    weekly_reports.queue_weekly_reports(connection, account, today_date)
    return sum(day == today_date.isoformat() for day, _ in rows.values()), spent


def _write_new_transactions(connection, account, today_date, rows, metadata, table="transactions"):
    new_today_ids = []
    new_other_day_count = 0
    for transaction_id, (day, amount) in rows.items():
        cursor = connection.execute(
            f"INSERT OR IGNORE INTO {table}(account, transaction_id, day, amount, transaction_time, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (account, transaction_id, day, amount, *metadata[transaction_id]),
        )
        if cursor.rowcount:
            if day == today_date.isoformat():
                new_today_ids.append(transaction_id)
            else:
                new_other_day_count += 1
    return new_today_ids, new_other_day_count


def _build_daily_notification(today_date, spent, budget):
    remaining = budget - spent
    payload = {
        "title": f"Ngân sách ngày {today_date:%d/%m/%Y}",
        "body": (
            f"Đã chi: {format_vnd(spent)} / {format_vnd(budget)}\n"
            f"{'Còn' if remaining >= 0 else 'Vượt'}: {format_vnd(remaining)}"
        ),
    }
    return apply_notification_options(payload, os.environ)


def _queue_daily_notification(connection, account, today_date, new_today_ids, spent,
                              budget, alert_percent, notification_mode):
    if not new_today_ids:
        return "Không có OUT mới hôm nay"
    pending = pending_alerts(connection, account, today_date, spent, budget, alert_percent)
    if notification_mode != "all" and not pending:
        return "Đã ghi nhận; chưa có mốc cảnh báo mới"
    payload = _build_daily_notification(today_date, spent, budget)
    mark_alerts(connection, account, today_date, pending)
    notification_outbox.enqueue(
        connection, account, _daily_event_key(account, today_date.isoformat(), new_today_ids), payload,
    )
    return "Đã xếp hàng thông báo"


def _save_and_alert(database_path, account, transactions, rows, metadata, in_rows, in_metadata,
                    monthly_budget, alert_percent, notification_mode):
    _step(database_path, account, "financial_save", "running")
    today_date = datetime.now(VIETNAM_TZ).date()
    outbox_attempted = False
    connection = None
    daily_committed = False
    sync_recorded = False
    ledger_changed = False
    try:
        try:
            connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
            connection.execute("BEGIN IMMEDIATE")
            effective_monthly = record_monthly_budget(connection, account, today_date, monthly_budget)
            daily_budget = budget_for_day(connection, account, today_date, today_date)
            history_warnings = record_history_window(
                connection, account, transactions, datetime.now(VIETNAM_TZ)
            )
            # Read the days before _prepare_ledger drops conflicting rows.
            days = {day for day, _ in rows.values()} | {day for day, _ in in_rows.values()}
            initialized, conflicts, in_conflicts = _prepare_ledger(
                connection, account, today_date, rows, metadata, in_rows, in_metadata,
            )
            income_budget.ensure_state(connection, account, today_date, days)
            _queue_conflict_alerts(connection, account, conflicts)
            if not initialized:
                baseline_today, spent_today = _write_baseline(
                    connection, account, today_date, rows, metadata, daily_budget, alert_percent,
                )
            else:
                new_today_ids, new_other_day_count = _write_new_transactions(
                    connection, account, today_date, rows, metadata,
                )
                spent_today = _spent_today(connection, account, today_date)
                cycle_status = _queue_daily_notification(
                    connection, account, today_date, new_today_ids, spent_today,
                    daily_budget, alert_percent, notification_mode,
                )
            new_in_today_ids, new_in_other_count = _write_new_transactions(
                connection, account, today_date, in_rows, in_metadata, "incoming_transactions",
            )
            new_in_count = len(new_in_today_ids) + new_in_other_count
            ledger_changed = (
                not initialized or bool(new_today_ids or new_other_day_count or new_in_count)
            )
            sync_recorded = _record_sync_success_in_transaction(connection, account)
            connection.commit()
            daily_committed = True
            _step(database_path, account, "financial_save", "healthy")
            _log_budget(effective_monthly, monthly_budget, daily_budget, history_warnings)
            _log_actual_budget(connection, account, today_date)
            if conflicts:
                log_status(
                    f"Bỏ qua {len(conflicts)} giao dịch bị ngân hàng đổi ngày/số tiền; "
                    f"sổ giữ số đã ghi: {', '.join(conflict[0] for conflict in conflicts)}."
                )
            if in_conflicts:
                log_status(
                    f"Bỏ qua {len(in_conflicts)} giao dịch IN bị ngân hàng đổi ngày/số tiền; "
                    "sổ giữ số đã ghi."
                )
            if not initialized:
                log_status(
                    f"Đã tạo mốc ban đầu: {baseline_today} giao dịch OUT hôm nay, {new_in_count} giao dịch IN; "
                    f"tổng chi đã ghi nhận: {format_vnd(spent_today)} (không gửi thông báo)."
                )
            _weekly_best_effort(database_path, account, today_date)
        except KeyboardInterrupt:
            outbox_attempted = True
            raise
        except (SystemExit, Exception) as error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            code = ("validation" if isinstance(error, SystemExit) else
                    "database" if isinstance(error, sqlite3.Error) else "unexpected")
            _step(database_path, account, "financial_save", "failed", code)
            _record_sync_failure_best_effort(database_path, account, code)
            raise
    except BaseException:
        if connection is not None:
            connection.close()
        _after_commit(
            database_path, account, today_date, daily_committed, sync_recorded, outbox_attempted,
            ledger_changed,
        )
        raise
    connection.close()
    outbox_sent = _after_commit(
        database_path, account, today_date, daily_committed, sync_recorded, outbox_attempted,
        ledger_changed,
    )
    if not initialized:
        return
    if outbox_sent:
        cycle_status = "Đã gửi thông báo từ outbox"
    log_status(
        f"{cycle_status}: {len(new_today_ids)} OUT mới hôm nay"
        + (f", {new_other_day_count} OUT mới ngày khác" if new_other_day_count else "")
        + (f", {new_in_count} IN mới" if new_in_count else "")
        + f"; tổng chi đã ghi nhận: {format_vnd(spent_today)}."
    )


def main():
    device_key = required_env("DEVICE_KEY")
    bank_token = required_env("BANK_KEY")
    try:
        monthly_budget = int(required_env("BUDGET_PERMONTH"))
    except ValueError:
        raise SystemExit("BUDGET_PERMONTH phải là số nguyên, ví dụ 15000000.")
    try:
        monthly_budget = parse_budget_amount(monthly_budget)
    except ValueError:
        raise SystemExit("BUDGET_PERMONTH phải là số nguyên không âm vừa SQLite.")
    configured_day = datetime.now(VIETNAM_TZ).date()
    try:
        alert_percent, notification_mode = parse_alert_settings(os.environ)
    except ValueError:
        raise SystemExit(
            "Cấu hình cảnh báo ngân sách không hợp lệ: BUDGET_ALERT_PERCENT phải từ 1 đến 99; "
            "NOTIFICATION_MODE phải là 'all' hoặc 'thresholds'."
        ) from None

    bank_url = f"https://thueapibank.vn/historyapimbv2/{quote(bank_token, safe='')}"
    # headers only; persistent Cloudflare blocks need provider allowlisting, not retries.
    bank_request = Request(
        bank_url,
        headers={
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        method="GET",
    )
    database_path = Path(__file__).resolve().with_name("budget_state.sqlite3")
    account = hashlib.sha256(bank_token.encode("utf-8")).hexdigest()
    if database_path.is_file():
        _KNOWN_DATABASES.add(database_path.resolve())
        with database_path.open("rb") as database:
            if database.read(16) != b"SQLite format 3\x00":
                raise SystemExit("DB không hợp lệ; không tự tạo lại dữ liệu.")
    if not _runtime_update(database_path, start_cycle, account, datetime.now(VIETNAM_TZ)):
        raise SystemExit("Không thể bắt đầu chu kỳ; kiểm tra cơ sở dữ liệu.")
    _KNOWN_DATABASES.add(database_path.resolve())
    # Persist configuration before network work so outages cannot postpone its effective day.
    if not _runtime_update(database_path, record_monthly_budget, account, configured_day, monthly_budget):
        _step(database_path, account, "bank_api", "skipped")
        _step(database_path, account, "financial_save", "failed", "database")
        _record_sync_failure_best_effort(database_path, account, "database")
        _dispatch_pending(database_path, account)
        raise SystemExit("Không lưu được cấu hình ngân sách; chưa gọi Bank API.")
    transactions = _fetch(database_path, account, bank_request)
    rows, in_rows, metadata, in_metadata = _validate(database_path, account, transactions)
    _save_and_alert(
        database_path, account, transactions, rows, metadata, in_rows, in_metadata, monthly_budget,
        alert_percent, notification_mode,
    )


def run_forever():
    log_status("Bắt đầu theo dõi ngân sách; chu kỳ 30 phút. Lịch sử API chưa xác minh đầy đủ nên tổng chi là phần đã ghi nhận trong SQLite.")
    if os.getenv("BUDGET_PERDAY", "").strip():
        log_status("BUDGET_PERDAY không còn được dùng; đặt BUDGET_PERMONTH.")
    while True:
        log_status("Bắt đầu chu kỳ kiểm tra.")
        try:
            main()
        except KeyboardInterrupt:
            log_status("Đã dừng theo yêu cầu.")
            return
        except (SystemExit, Exception) as error:
            log_status(f"Chu kỳ lỗi ({type(error).__name__}): {redact_secrets(str(error))}")
        else:
            log_status("Chu kỳ xử lý xong; xem --status để kiểm tra từng bước.")

        completed_at = datetime.now(VIETNAM_TZ)
        next_run = completed_at + timedelta(seconds=1800)
        log_status(
            f"Đợi tới chu kỳ tiếp theo lúc {next_run:%Y-%m-%d %H:%M:%S} UTC+07:00.",
            at=completed_at,
        )
        try:
            time.sleep(1800)
        except KeyboardInterrupt:
            log_status("Đã dừng theo yêu cầu.")
            return


def self_check():
    """Run offline checks without importing tests during normal startup."""
    from tests import run_checks

    run_checks(
        sys.modules[__name__], "main",
        "tests.test_main_helpers.MainHelperTests",
        "tests.test_main_cli.MainCliTests",
        "tests.test_main_cycle.MainCycleTests",
        "tests.test_ledger_edits.LedgerEditsTests",
        "tests.test_domain_refactor.SharedContractTests",
        "tests.test_domain_refactor.DomainTests",
        "tests.test_domain_refactor.BudgetAndOutboxTests",
        "tests.test_storage_refactor.StorageRefactorTests",
    )


def _status_text(value, unknown="không xác định"):
    if value is None:
        return unknown
    if value is True:
        return "có"
    if value is False:
        return "không"
    return str(value)


def _run_status(database_path, account, backup_directory):
    try:
        snapshot = read_status_snapshot(database_path, account, backup_directory)
    except Exception:
        print("Không thể đọc trạng thái.", file=sys.stderr)
        return 1

    database_status = {
        "ok": "bình thường",
        "absent": "chưa có",
        "error": "không xác định",
    }.get(snapshot["database"].get("status"), "không xác định")
    sync = snapshot["sync"]
    outbox = snapshot["outbox"]
    history = snapshot["history"]
    backup = snapshot["backup"]
    sync_outcome = {
        "healthy": "thành công",
        "failed": "thất bại",
        "absent": "chưa có",
        "unknown": "không xác định",
    }.get(sync.get("status"), "không xác định")
    freshness = {
        "fresh": "fresh",
        "stale": "stale",
        "unknown": "unknown",
    }.get(sync.get("freshness"), "unknown")
    print(f"Cơ sở dữ liệu: {database_status}")
    print(f"Đồng bộ cuối: {sync_outcome}; độ mới: {freshness}")
    print(f"Lần thử đồng bộ cuối: {_status_text(sync.get('last_attempt_at'), 'không có')}")
    print(f"Lần thành công cuối: {_status_text(sync.get('last_success_at'), 'không có')}")
    print(f"Lỗi cuối (mã an toàn): {_status_text(sync.get('last_error_code'), 'không có')}")
    print(f"Lỗi liên tiếp: {_status_text(sync.get('consecutive_failures'))}")
    for step in STAGES:
        outcome = snapshot.get("steps", {}).get(step, {})
        print(
            f"Bước {step}: {outcome.get('status', 'unknown')}; "
            f"độ mới={outcome.get('freshness', 'unknown')}; "
            f"lỗi liên tiếp={_status_text(outcome.get('consecutive_failures'))}; "
            f"lỗi cuối={_status_text(outcome.get('last_error_code'), 'none')}; "
            f"kiểm tra cuối={_safe_outbox_timestamp(outcome.get('last_checked_at'))}; "
            f"lần thử cuối={_safe_outbox_timestamp(outcome.get('last_attempt_at'))}; "
            f"thành công cuối={_safe_outbox_timestamp(outcome.get('last_success_at'))}"
        )
    print(
        "Outbox: "
        f"đã gửi={_status_text(outbox.get('delivered'))}; "
        f"đang chờ={_status_text(outbox.get('pending'))}; "
        f"cách ly={_status_text(outbox.get('quarantined'))}"
    )
    print(
        "Độ bao phủ: "
        f"gap_suspected={_status_text(history.get('gap_suspected'))}; "
        f"history_unknown={_status_text(history.get('history_unknown'))}"
    )
    backup_status = backup.get("status")
    if backup_status in {"verified", "invalid", "error"}:
        backup_name = backup.get("filename") or "không xác định"
    elif backup_status == "absent":
        backup_name = "chưa có"
    else:
        backup_name = "không xác định"
    print(f"Sao lưu mới nhất: {backup_name}")
    backup_message = {
        "verified": "verified (đã xác minh toàn vẹn)",
        "invalid": "invalid (không hợp lệ)",
        "error": "error (không xác định)",
        "absent": "absent (chưa xác minh toàn vẹn)",
    }.get(backup_status, "unknown (chưa xác minh toàn vẹn)")
    print(f"Toàn vẹn sao lưu: {backup_message}.")
    return 0


def _run_reconcile_history(database_path, reason):
    bank_key = os.getenv("BANK_KEY", "").strip()
    if not bank_key:
        print("Thiếu cấu hình BANK_KEY.", file=sys.stderr)
        return 1
    if not database_path.is_file():
        print("Không tìm thấy cơ sở dữ liệu.", file=sys.stderr)
        return 1

    account = hashlib.sha256(bank_key.encode("utf-8")).hexdigest()
    connection = None
    try:
        with single_instance(database_path):
            try:
                connection = sqlite3.connect(
                    database_path.resolve().as_uri() + "?mode=rw",
                    uri=True,
                    timeout=30,
                    isolation_level=None,
                )
                connection.execute("BEGIN IMMEDIATE")
                reconcile_history(connection, account, datetime.now(VIETNAM_TZ), reason)
                connection.commit()
            except BaseException:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                if connection is not None:
                    connection.close()
    except InstanceAlreadyRunningError:
        print("Chương trình theo dõi ngân sách đã chạy.", file=sys.stderr)
        return 1
    except BaseException:
        print("Không thể đối soát lịch sử.", file=sys.stderr)
        return 1

    print("Đã ghi nhận xác nhận của người dùng; không tải giao dịch thiếu.")
    return 0


_OUTBOX_VISIBLE_ERRORS = frozenset({"callback_error", "unconfirmed_response"})


def _outbox_account():
    bank_key = os.getenv("BANK_KEY", "").strip()
    if not bank_key:
        print("Thiếu cấu hình BANK_KEY.", file=sys.stderr)
        return None
    return hashlib.sha256(bank_key.encode("utf-8")).hexdigest()


def _safe_outbox_error(value):
    if value is None:
        return "none"
    if isinstance(value, str) and value in _OUTBOX_VISIBLE_ERRORS:
        return value
    return "unexpected"


def _safe_outbox_timestamp(value):
    if value is None:
        return "none"
    if not isinstance(value, str):
        return "unknown"
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    return value


def _run_outbox_list(database_path, account, after_id, limit):
    if not database_path.is_file():
        print("Outbox database: absent.")
        print(f"Outbox page after_id={after_id}; limit={limit}; no events.")
        print("Next cursor: none")
        return 0

    connection = None
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        events = notification_outbox.list_events(
            connection, account, limit=limit + 1, after_id=after_id
        )
        has_more = len(events) > limit
        events = events[:limit]
    except BaseException:
        print("Unable to read outbox.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()

    print(f"Outbox page after_id={after_id}; limit={limit};")
    if not events:
        print("No outbox events in page.")
    for event in events:
        if event.get("delivered") == 1:
            state = "delivered"
        elif event.get("quarantined") == 1:
            state = "quarantined"
        else:
            state = "pending"
        attempts = event.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            attempts = "unknown"
        print(
            f"ID={event.get('id')} state={state} attempts={attempts} "
            f"last_attempt_at={_safe_outbox_timestamp(event.get('last_attempt_at'))} "
            f"last_error={_safe_outbox_error(event.get('last_error'))}"
        )
    next_cursor = events[-1]["id"] if has_more and events else "none"
    print(f"Next cursor: {next_cursor}")
    return 0


def _run_outbox_mutation(database_path, account, event_id, quarantined, reason):
    if not database_path.is_file():
        print("Outbox database not found.", file=sys.stderr)
        return 1

    connection = None
    try:
        with single_instance(database_path):
            try:
                connection = sqlite3.connect(
                    database_path.resolve().as_uri() + "?mode=rw",
                    uri=True,
                    timeout=30,
                    isolation_level=None,
                )
                connection.execute("BEGIN IMMEDIATE")
                notification_outbox.set_quarantine(
                    connection, account, event_id, quarantined, reason
                )
                connection.commit()
            except BaseException:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                if connection is not None:
                    connection.close()
    except InstanceAlreadyRunningError:
        print("Budget monitor is already running.", file=sys.stderr)
        return 1
    except BaseException:
        print("Unable to update outbox.", file=sys.stderr)
        return 1

    state = "quarantined" if quarantined else "pending"
    print(f"Outbox event {event_id} state changed to {state}; not delivered.")
    if quarantined:
        print("Event stays out of normal delivery until released.")
    else:
        print("Release queues event for retry in next normal cycle; not delivered now.")
    return 0


def _run_list_conflicts(database_path, account):
    if not database_path.is_file():
        print("Không có cơ sở dữ liệu.")
        return 0

    connection = None
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        conflicts = ledger_edits.open_conflicts(connection, account)
    except sqlite3.Error:
        print("Không đọc được xung đột.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()

    if not conflicts:
        print("Không có xung đột đang mở.")
    for conflict in conflicts:
        print(
            f"KEY={conflict['key']} giao_dịch={conflict['transaction_id']} "
            f"đã_lưu={conflict['stored_day']},{conflict['stored_amount']} "
            f"ngân_hàng={conflict['incoming_day']},{conflict['incoming_amount']} "
            f"lần_thấy={conflict['seen_count']} lần_cuối={conflict['last_seen_at']}"
        )
    return 0


def _run_accept_conflict(database_path, account, transaction_id, reason):
    if not database_path.is_file():
        print("Không có cơ sở dữ liệu.", file=sys.stderr)
        return 1

    connection = None
    try:
        # No single_instance lock: SQLite serialises writers, so this runs beside the monitor.
        connection = sqlite3.connect(
            database_path.resolve().as_uri() + "?mode=rw",
            uri=True,
            timeout=30,
            isolation_level=None,
        )
        connection.execute("BEGIN IMMEDIATE")
        conflict_key = ledger_edits.latest_open_conflict_key(connection, account, transaction_id)
        if conflict_key is None:
            print("Không có xung đột đang mở cho giao dịch này.", file=sys.stderr)
            return 1
        accepted = ledger_edits.accept_conflict(
            connection, account, conflict_key, reason, datetime.now(VIETNAM_TZ).isoformat(),
        )
        connection.commit()
    except (LookupError, ledger_edits.ConflictStateError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError):
        print("Không nhận được xung đột.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                connection.close()

    print(f"Đã nhận ngày/số tiền của ngân hàng cho giao dịch {accepted['transaction_id']}: "
          f"{accepted['day']}, {format_vnd(accepted['amount'])}.")
    return 0


def _argument_parser():
    parser = argparse.ArgumentParser(description="Theo dõi ngân sách cá nhân.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--self-check", action="store_true", help="chạy kiểm tra offline")
    modes.add_argument("--preflight", action="store_true", help="kiểm tra trước chạy, không gọi API hoặc ghi dữ liệu")
    modes.add_argument(
        "--reconcile-history",
        action="store_true",
        help="ghi nhận xác nhận lịch sử hiện có, không gọi API",
    )
    modes.add_argument(
        "--status",
        action="store_true",
        help="đọc trạng thái hiện tại, không ghi cơ sở dữ liệu",
    )
    modes.add_argument(
        "--outbox-list",
        action="store_true",
        help="liệt kê một trang sự kiện outbox, chỉ đọc",
    )
    modes.add_argument(
        "--outbox-quarantine",
        type=int,
        metavar="ID",
        help="cách ly sự kiện outbox",
    )
    modes.add_argument(
        "--outbox-release",
        type=int,
        metavar="ID",
        help="gỡ cách ly sự kiện outbox",
    )
    modes.add_argument("--list-conflicts", action="store_true",
                       help="liệt kê giao dịch bị ngân hàng đổi ngày/số tiền, chỉ đọc")
    modes.add_argument("--accept-conflict", metavar="TRANSACTION_ID",
                       help="nhận ngày/số tiền mới của ngân hàng cho một giao dịch")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="xác nhận tường minh cho thao tác ghi",
    )
    parser.add_argument(
        "--reason",
        help="lý do thay đổi; bắt buộc với thao tác ghi outbox",
    )
    parser.add_argument(
        "--after-id",
        type=int,
        default=None,
        metavar="ID",
        help="chỉ đọc sự kiện có ID lớn hơn ID này",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="số sự kiện mỗi trang (1-500; mặc định 50)",
    )
    return parser


def _require_write_confirmation(parser, args, mode_name, max_length):
    if not args.confirm:
        parser.error(f"{mode_name} cần --confirm")
    if (args.reason is None or not args.reason.strip()
            or len(args.reason) > max_length):
        parser.error(
            f"{mode_name} cần --reason không rỗng, tối đa "
            f"{max_length} ký tự"
        )


def _validate_cli_args(parser, args):
    write_mode = None
    event_id = args.outbox_quarantine
    if args.reconcile_history:
        write_mode = "--reconcile-history"
    elif event_id is not None:
        write_mode = "--outbox-quarantine"
    elif args.outbox_release is not None:
        write_mode = "--outbox-release"
        event_id = args.outbox_release
    elif args.accept_conflict is not None:
        write_mode = "--accept-conflict"
        if not args.accept_conflict.strip():
            parser.error("--accept-conflict cần mã giao dịch không rỗng")
    page_modifiers = args.after_id is not None or args.limit is not None
    unrelated_mode = "--preflight" if args.preflight else "--self-check" if args.self_check else None
    if unrelated_mode and (args.confirm or args.reason is not None or page_modifiers):
        parser.error(f"tùy chọn không liên quan tới {unrelated_mode}")
    if page_modifiers and not args.outbox_list:
        if args.list_conflicts:
            parser.error("--after-id và --limit không dùng với --list-conflicts")
        if args.reconcile_history:
            parser.error("tùy chọn không liên quan tới --reconcile-history")
        parser.error("--after-id và --limit chỉ dùng với --outbox-list")
    if write_mode:
        if event_id is not None and event_id <= 0:
            parser.error(f"{write_mode} ID phải là số nguyên dương")
        limit = ledger_edits.REASON_MAX if write_mode == "--accept-conflict" else RECONCILIATION_REASON_MAX_LENGTH
        _require_write_confirmation(parser, args, write_mode, limit)
    elif args.confirm or args.reason is not None:
        if args.list_conflicts:
            parser.error("--confirm và --reason không dùng với --list-conflicts")
        if args.outbox_list:
            parser.error("--confirm và --reason không dùng với --outbox-list")
        parser.error(
            "--confirm và --reason chỉ dùng với --reconcile-history, --accept-conflict, "
            "--outbox-quarantine hoặc --outbox-release"
        )
    if args.outbox_list:
        if args.after_id is not None and args.after_id < 0:
            parser.error("--after-id phải là số nguyên không âm")
        if args.limit is not None and not 1 <= args.limit <= 500:
            parser.error("--limit phải từ 1 đến 500")


def _run_preflight(root):
    try:
        checks = check_startup(root / "budget_state.sqlite3", root / "backups", root / "logs", os.environ)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"Kiểm tra trước chạy thất bại: {redact_secrets(str(error))}", file=sys.stderr)
        return 1
    for check in checks:
        print(f"PASS {check}")
    print("Kiểm tra trước chạy hoàn tất; không gọi API hoặc gửi thông báo.")
    return 0


def _run_monitor(database_path):
    try:
        with single_instance(database_path):
            root = database_path.parent
            if database_path.exists():
                _KNOWN_DATABASES.add(database_path.resolve())
            check_startup(database_path, root / "backups", root / "logs", os.environ)
            configure_logging(root / "logs")
            log_status("Kiểm tra trước chạy hoàn tất.")
            run_forever()
    except InstanceAlreadyRunningError:
        print("Chương trình theo dõi ngân sách đã chạy.", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError) as error:
        print(f"Không thể khởi động: {redact_secrets(str(error))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Đã dừng trước khi bắt đầu theo dõi.", file=sys.stderr)
        return 130
    finally:
        close_logging()
    return 0


def cli():
    for stream in (sys.stdout, sys.stderr):
        if callable(getattr(stream, "reconfigure", None)):
            stream.reconfigure(encoding="utf-8", errors=stream.errors)
    parser = _argument_parser()
    args = parser.parse_args()
    _validate_cli_args(parser, args)
    root = Path(__file__).resolve().parent
    database_path = root / "budget_state.sqlite3"
    if args.preflight:
        return _run_preflight(root)
    if args.self_check:
        self_check()
        print("Self-check passed.")
        return 0
    if args.reconcile_history:
        return _run_reconcile_history(database_path, args.reason)
    if args.list_conflicts or args.accept_conflict is not None:
        account = _outbox_account()
        if account is None:
            return 1
        if args.list_conflicts:
            return _run_list_conflicts(database_path, account)
        return _run_accept_conflict(database_path, account, args.accept_conflict, args.reason)
    if args.status or args.outbox_list or args.outbox_quarantine is not None or args.outbox_release is not None:
        account = _outbox_account()
        if account is None:
            return 1
        if args.status:
            return _run_status(database_path, account, root / "backups")
        if args.outbox_list:
            return _run_outbox_list(
                database_path, account,
                0 if args.after_id is None else args.after_id,
                50 if args.limit is None else args.limit,
            )
        quarantined = args.outbox_quarantine is not None
        event_id = args.outbox_quarantine if quarantined else args.outbox_release
        return _run_outbox_mutation(database_path, account, event_id, quarantined, args.reason)
    return _run_monitor(database_path)


if __name__ == "__main__":
    raise SystemExit(cli())
