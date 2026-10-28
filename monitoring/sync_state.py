"""Sync state leaf: schema and record APIs never manage caller transactions."""

from __future__ import annotations

from datetime import datetime
import sqlite3

SYNC_ERROR_CODES = frozenset({"bank_api", "validation", "database", "unexpected"})
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sync_status (
    account TEXT PRIMARY KEY,
    last_attempt_at TEXT NOT NULL,
    last_success_at TEXT,
    last_error_code TEXT,
    consecutive_failures INTEGER NOT NULL
)
"""


def _account(account: str) -> str:
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account must be nonempty")
    return account


def _timestamp(at: datetime) -> str:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be an aware datetime")
    return at.isoformat()


def _error_code(error_code: str) -> str:
    if error_code not in SYNC_ERROR_CODES:
        raise ValueError("invalid error_code")
    return error_code


def safe_sync_error(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in SYNC_ERROR_CODES else "unexpected"


def ensure_sync_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_CREATE_TABLE)


def record_sync_success(connection: sqlite3.Connection, account: str, at: datetime) -> None:
    account = _account(account)
    timestamp = _timestamp(at)
    ensure_sync_schema(connection)
    connection.execute(
        """
        INSERT INTO sync_status
            (account, last_attempt_at, last_success_at, last_error_code, consecutive_failures)
        VALUES (?, ?, ?, NULL, 0)
        ON CONFLICT(account) DO UPDATE SET
            last_attempt_at = excluded.last_attempt_at,
            last_success_at = excluded.last_success_at,
            last_error_code = NULL,
            consecutive_failures = 0
        """,
        (account, timestamp, timestamp),
    )


def record_sync_failure(
    connection: sqlite3.Connection,
    account: str,
    at: datetime,
    error_code: str,
) -> None:
    account = _account(account)
    timestamp = _timestamp(at)
    error_code = _error_code(error_code)
    ensure_sync_schema(connection)
    connection.execute(
        """
        INSERT INTO sync_status
            (account, last_attempt_at, last_success_at, last_error_code, consecutive_failures)
        VALUES (?, ?, NULL, ?, 1)
        ON CONFLICT(account) DO UPDATE SET
            last_attempt_at = excluded.last_attempt_at,
            last_error_code = excluded.last_error_code,
            consecutive_failures = sync_status.consecutive_failures + 1
        """,
        (account, timestamp, error_code),
    )


def read_sync_status(connection: sqlite3.Connection, account: str) -> dict[str, object] | None:
    account = _account(account)
    try:
        row = connection.execute(
            """
            SELECT account, last_attempt_at, last_success_at, last_error_code,
                   consecutive_failures
            FROM sync_status
            WHERE account = ?
            """,
            (account,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error).lower():
            raise
        return None
    if row is None:
        return None
    return {
        "account": row[0],
        "last_attempt_at": row[1],
        "last_success_at": row[2],
        "last_error_code": row[3],
        "consecutive_failures": row[4],
    }

