from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import quote

from common import STALE_AFTER_SECONDS, table_columns, table_exists
from monitoring.sync_state import (
    record_sync_failure, record_sync_success, read_sync_status, safe_sync_error,
)
from monitoring.runtime_health import read_steps
from storage.db_backup import snapshot_integrity

def _account(account: str) -> str:
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account must be nonempty")
    return account


_BACKUP_NAME = re.compile(r"^budget-(\d{4}-\d{2}-\d{2})\.sqlite3$")


def _empty_sync_snapshot(status: str) -> dict[str, object]:
    """Return last-outcome status plus freshness metadata; missing times stay unknown."""
    return {
        "status": status,
        "last_attempt_at": None,
        "last_success_at": None,
        "last_error_code": None,
        "consecutive_failures": None,
        "freshness": "unknown",
        "stale_after_seconds": STALE_AFTER_SECONDS,
    }


def _sync_freshness(last_attempt_at: object, now: datetime) -> str:
    """Classify aware last attempt: age > 3600 is stale; exact 3600 stays fresh."""
    if not isinstance(last_attempt_at, str):
        return "unknown"
    try:
        attempted = datetime.fromisoformat(last_attempt_at)
        if attempted.tzinfo is None or attempted.utcoffset() is None:
            return "unknown"
        age = (now - attempted).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    if age < 0:
        return "unknown"
    return "stale" if age > STALE_AFTER_SECONDS else "fresh"


def _snapshot_sync(
    connection: sqlite3.Connection, account: str, now: datetime
) -> dict[str, object]:
    empty = _empty_sync_snapshot("unknown")
    if not table_exists(connection, "sync_status"):
        empty["status"] = "absent"
        return empty
    columns = table_columns(connection, "sync_status")
    required = {
        "account",
        "last_attempt_at",
        "last_success_at",
        "last_error_code",
        "consecutive_failures",
    }
    if not required.issubset(columns):
        return empty
    row = read_sync_status(connection, account)
    if row is None:
        return empty
    empty.update(
        {
            "status": "healthy" if row["last_error_code"] is None and row["consecutive_failures"] == 0 else "failed",
            "last_attempt_at": row["last_attempt_at"],
            "last_success_at": row["last_success_at"],
            "last_error_code": safe_sync_error(row["last_error_code"]),
            "consecutive_failures": row["consecutive_failures"],
            "freshness": _sync_freshness(row["last_attempt_at"], now),
        }
    )
    return empty


def snapshot_outbox(connection: sqlite3.Connection, account: str) -> dict[str, object]:
    empty = {
        "status": "unknown",
        "delivered": None,
        "pending": None,
        "quarantined": None,
    }
    if not table_exists(connection, "notification_outbox"):
        empty["status"] = "absent"
        return empty
    columns = table_columns(connection, "notification_outbox")
    if not {"account", "delivered"}.issubset(columns):
        return empty
    if "quarantined" in columns:
        row = connection.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN delivered = 1 THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN delivered = 0 AND COALESCE(quarantined, 0) = 0 "
            "THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN quarantined = 1 THEN 1 ELSE 0 END), 0) "
            "FROM notification_outbox WHERE account = ?",
            (account,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN delivered = 1 THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN delivered = 0 THEN 1 ELSE 0 END), 0) "
            "FROM notification_outbox WHERE account = ?",
            (account,),
        ).fetchone()
        row = (*row, 0)
    return {
        "status": "available",
        "delivered": row[0],
        "pending": row[1],
        "quarantined": row[2],
    }


def _snapshot_history(connection: sqlite3.Connection, account: str) -> dict[str, object]:
    empty = {
        "status": "unknown",
        "gap_suspected": None,
        "history_unknown": None,
    }
    if not table_exists(connection, "history_health"):
        empty["status"] = "absent"
        return empty
    columns = table_columns(connection, "history_health")
    if "account" not in columns:
        return empty
    flags = [name for name in ("gap_suspected", "history_unknown") if name in columns]
    if not flags:
        return empty
    row = connection.execute(
        "SELECT " + ", ".join(flags) + " FROM history_health WHERE account = ?",
        (account,),
    ).fetchone()
    if row is None:
        return empty
    for name, value in zip(flags, row):
        empty[name] = None if value is None else bool(value)
    empty["status"] = "available"
    return empty


def newest_backup(directory):
    """Return (date, filename, path) of the newest dated backup candidate, or None.

    None means the directory is missing or holds no candidate. A path that is not a
    directory raises NotADirectoryError; other OSError, TypeError and ValueError propagate.
    """
    directory = Path(directory)
    try:
        directory_mode = directory.stat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(directory_mode):
        raise NotADirectoryError(str(directory))
    candidates = []
    for path in directory.iterdir():
        match = _BACKUP_NAME.fullmatch(path.name)
        if not match:
            continue
        try:
            backup_date = date.fromisoformat(match.group(1))
            path_mode = path.lstat().st_mode
        except ValueError:
            continue
        if stat.S_ISREG(path_mode) or stat.S_ISLNK(path_mode):
            candidates.append((backup_date, path.name, path))
    return max(candidates) if candidates else None


def read_backup_snapshot(directory) -> dict[str, object]:
    """Verify newest dated backup read-only; never hide a newer invalid candidate."""
    empty = {"status": "absent", "filename": None, "date": None}
    if directory is None:
        return empty
    try:
        newest = newest_backup(directory)
    except (OSError, TypeError, ValueError):
        empty["status"] = "error"
        return empty
    if newest is None:
        return empty
    backup_date, filename, path = newest
    return {
        "status": snapshot_integrity(path, immutable=True),
        "filename": filename,
        "date": backup_date.isoformat(),
    }


def _unknown_snapshot(backup: dict[str, object]) -> dict[str, object]:
    return {
        "database": {"status": "error"},
        "sync": _empty_sync_snapshot("unknown"),
        "outbox": {
            "status": "unknown",
            "delivered": None,
            "pending": None,
            "quarantined": None,
        },
        "history": {"status": "unknown", "gap_suspected": None, "history_unknown": None},
        "steps": {},
        "backup": backup,
    }


def snapshot_from_connection(
    connection: sqlite3.Connection | None,
    account: str,
    now: datetime,
    backup: dict,
) -> dict:
    """Assemble status without changing caller-owned connection or transaction."""
    account = _account(account)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    if connection is None:
        snapshot = _unknown_snapshot(backup)
        snapshot["database"]["status"] = "absent"
        for name in ("sync", "outbox", "history"):
            snapshot[name]["status"] = "absent"
        return snapshot
    return {
        "database": {"status": "ok"},
        "sync": _snapshot_sync(connection, account, now),
        "outbox": snapshot_outbox(connection, account),
        "history": _snapshot_history(connection, account),
        "steps": read_steps(connection, account, now),
        "backup": backup,
    }


def read_status_snapshot(
    database_path,
    account: str,
    backup_directory,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Read status read-only; freshness uses aware UTC ``now`` and 3600-second grace."""
    account = _account(account)
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    backup = read_backup_snapshot(backup_directory)
    database = Path(database_path)
    if not database.exists():
        return snapshot_from_connection(None, account, now, backup)

    uri = "file:" + quote(database.resolve(strict=False).as_posix(), safe="/:") + "?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("BEGIN")
        check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise sqlite3.DatabaseError("database integrity check failed")
        return snapshot_from_connection(connection, account, now, backup)
    except (sqlite3.DatabaseError, OSError):
        return _unknown_snapshot(backup)
    finally:
        if connection is not None:
            connection.close()
