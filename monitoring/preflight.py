"""Check local startup requirements without API calls or logical DB changes.

SQLite uses mode=ro. Reading a live WAL database can still need SQLite sidecars.
"""

from collections.abc import Mapping
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import time

from notifications.budget_alerts import parse_alert_settings
from common import parse_budget_amount
from monitoring.runtime_log import check_paths


_MIN_FREE_SPACE = 16 * 1024 * 1024
_DB_CHECK_SECONDS = 2.0
_BASE_COLUMNS = {
    "accounts": {"account", "initialized"},
    "transactions": {"account", "transaction_id", "day", "amount"},
    "incoming_transactions": {"account", "transaction_id", "day", "amount"},
    "income_state": {"account", "start_month"},
    "income_budget_shifts": {"account", "transaction_id"},
    "budget_history": {"account", "effective_day", "amount"},
    "monthly_budget_history": {"account", "effective_day", "amount"},
    "budget_alerts": {"account", "day", "kind"},
    "sync_status": {
        "account", "last_attempt_at", "last_success_at", "last_error_code",
        "consecutive_failures",
    },
    "history_health": {
        "account", "transaction_count", "checked_at", "transaction_ids",
        "gap_suspected",
    },
    "history_reconciliation_audit": {
        "audit_id", "account", "reconciled_at", "reason", "prior_gap_suspected",
        "prior_history_unknown", "baseline_checked_at",
    },
    "notification_outbox": {"id", "account", "event_key", "payload", "delivered"},
    "outbox_actions": {"id", "event_id", "account", "action", "reason", "at"},
    "weekly_summary_state": {"account", "next_week"},
    "weekly_report_revisions": {
        "account", "week_start", "revision", "spent", "allowance", "unknown_days",
        "event_id", "event_key", "kind",
    },
    "weekly_report_migrations": {"account"},
    "step_status": {
        "account", "step", "status", "last_checked_at", "last_attempt_at",
        "last_success_at", "last_error_code", "consecutive_failures",
    },
    "sync_health_alerts": {"account", "active", "sequence"},
}


def _writable_parent(directory: Path, allow_missing: bool) -> Path:
    candidate = directory
    while True:
        try:
            candidate.lstat()
            break
        except FileNotFoundError:
            if not allow_missing or candidate == candidate.parent:
                raise OSError("Storage directory is not available.") from None
            candidate = candidate.parent
    if not stat.S_ISDIR(candidate.stat().st_mode):
        raise OSError("Storage path is not a directory.")
    # access checks are hints; callers must handle real open failures.
    if not os.access(candidate, os.R_OK | os.W_OK | os.X_OK):
        raise OSError("Storage directory permissions are insufficient.")
    return candidate


def _database_size(database_path: Path) -> int | None:
    try:
        details = database_path.lstat()
    except FileNotFoundError:
        if any(os.path.lexists(str(database_path) + suffix)
               for suffix in ("-wal", "-shm", "-journal")):
            raise RuntimeError("Database is missing but journal files remain.") from None
        return None
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("Database must be a regular file without symlinks.")
    if details.st_size == 0:
        raise RuntimeError("Database file is empty.")
    if not os.access(database_path, os.R_OK | os.W_OK):
        raise OSError("Database permissions are insufficient.")
    with database_path.open("rb") as database:
        if database.read(16) != b"SQLite format 3\x00":
            raise RuntimeError("Database file is not a SQLite database.")
    return details.st_size


def _has_finance_key(connection, table, primary_key, check_deadline) -> bool:
    key = {"account"} if table == "accounts" else {"account", "transaction_id"}
    if primary_key == key:
        return True
    for index in connection.execute(f"PRAGMA index_list({table})"):
        check_deadline()
        if not index[2] or index[4]:
            continue
        name = index[1].replace('"', '""')
        indexed = [
            row[2].lower() if row[2] is not None else None
            for row in connection.execute(f'PRAGMA index_info("{name}")')
        ]
        if len(indexed) == len(key) and set(indexed) == key:
            return True
    return False


def _check_schema(connection: sqlite3.Connection, check_deadline) -> None:
    tables = set()
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    ):
        check_deadline()
        if name.lower() in _BASE_COLUMNS:
            tables.add(name.lower())
    if not tables:
        raise RuntimeError("Database schema is not recognized.")
    if ("accounts" in tables) != ("transactions" in tables):
        raise RuntimeError("Database financial schema is incomplete.")
    if not tables.intersection({"accounts", "sync_status"}):
        raise RuntimeError("Database schema is not recognized.")
    for table in tables:
        columns = set()
        primary_key = set()
        for row in connection.execute(f"PRAGMA table_info({table})"):
            check_deadline()
            columns.add(row[1].lower())
            if row[5]:
                primary_key.add(row[1].lower())
        if not _BASE_COLUMNS[table].issubset(columns):
            raise RuntimeError("Database schema is incompatible.")
        # Check finance keys only; add other keys with their write-contract checks.
        if table in {"accounts", "transactions", "incoming_transactions"} and not _has_finance_key(
            connection, table, primary_key, check_deadline,
        ):
            raise RuntimeError("Database schema is incompatible.")
    if "accounts" not in tables and connection.execute(
        "SELECT 1 FROM sync_status WHERE last_success_at IS NOT NULL LIMIT 1"
    ).fetchone() is not None:
        raise RuntimeError("Database financial schema is incomplete.")


def _check_database(database_path: Path) -> None:
    deadline = time.monotonic() + _DB_CHECK_SECONDS

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise RuntimeError("Database validation timed out.") from None

    try:
        with closing(sqlite3.connect(
            database_path.as_uri() + "?mode=ro", uri=True, timeout=1.0,
        )) as connection:
            connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 1000,
            )
            connection.execute("PRAGMA busy_timeout = 1000")
            if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                raise RuntimeError("Database integrity check failed.")
            check_deadline()
            _check_schema(connection, check_deadline)
            check_deadline()
    except sqlite3.Error:
        check_deadline()
        raise RuntimeError("Database cannot be read or validated.") from None


def check_startup(
    database_path: Path,
    backup_directory: Path,
    log_directory: Path,
    environ: Mapping,
) -> list[str]:
    """Return safe check labels, or raise a fixed-message startup error."""
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required.")
    if not isinstance(environ, Mapping):
        raise ValueError("Environment must be a mapping.")
    for name in ("BANK_KEY", "DEVICE_KEY", "BUDGET_PERMONTH"):
        value = environ.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string.")
    try:
        parse_budget_amount(environ["BUDGET_PERMONTH"].strip())
    except ValueError:
        raise ValueError("BUDGET_PERMONTH must be a nonnegative 64-bit integer.") from None
    try:
        parse_alert_settings(environ)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Budget alert settings are invalid.") from None
    paths = (database_path, backup_directory, log_directory)
    if any(not isinstance(path, Path) or "\x00" in str(path) for path in paths):
        raise ValueError("Storage paths must be valid Path objects.")
    try:
        database_path, backup_directory, log_directory = (
            path.absolute() for path in paths
        )
        database_parent = _writable_parent(database_path.parent, allow_missing=False)
        backup_parent = _writable_parent(backup_directory, allow_missing=True)
        log_parent = _writable_parent(log_directory, allow_missing=True)
        check_paths(log_directory)
        database_size = _database_size(database_path)
        required = max(_MIN_FREE_SPACE, 2 * (database_size or 0) + 4 * 1024 * 1024)
        for directory, minimum in (
            (database_parent, required),
            (backup_parent, required),
            (log_parent, _MIN_FREE_SPACE),
        ):
            if shutil.disk_usage(directory).free < minimum:
                raise RuntimeError("Insufficient free disk space.")
        if database_size is not None:
            _check_database(database_path)
    except OSError:
        raise OSError("Startup storage check failed.") from None
    return [
        "Python version",
        "Required environment",
        "Alert settings",
        "Storage directories",
        "New database" if database_size is None else "Database integrity and schema",
        "Free disk space",
    ]
