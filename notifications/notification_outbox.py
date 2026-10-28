"""Durable, account-scoped notification queue; transport stays caller-owned.

Delivery is at-least-once: process failure after remote confirmation but before
SQLite commit can resend an event. Use an idempotent receiver when available.
"""

import json
import re
import sqlite3
from datetime import date, datetime, timezone

from common import table_exists


_TABLE = "notification_outbox"
_ACTIONS_TABLE = "outbox_actions"
_OUTBOX_REASON_MAX_LENGTH = 1000
_WEEKLY_EVENT_RE = re.compile(r"weekly:([0-9]{4}-[0-9]{2}-[0-9]{2}):r([1-9][0-9]*)\Z")


class OutboxSendError(RuntimeError):
    """Send failed or was not explicitly confirmed; event remains pending."""


def _ensure_table(connection):
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
        "id INTEGER PRIMARY KEY, "
        "account TEXT NOT NULL, "
        "event_key TEXT NOT NULL, "
        "payload TEXT NOT NULL, "
        "delivered INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0, 1)), "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "last_attempt_at TEXT, "
        "last_error TEXT, "
        "quarantined INTEGER NOT NULL DEFAULT 0 CHECK (quarantined IN (0, 1)), "
        "quarantine_reason TEXT, "
        "quarantined_at TEXT, "
        "UNIQUE (account, event_key))"
    )
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_TABLE})")}
    for name, definition in (
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_attempt_at", "TEXT"),
        ("last_error", "TEXT"),
        ("quarantined", "INTEGER NOT NULL DEFAULT 0 CHECK (quarantined IN (0, 1))"),
        ("quarantine_reason", "TEXT"),
        ("quarantined_at", "TEXT"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {name} {definition}")
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {_ACTIONS_TABLE} ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id INTEGER NOT NULL, "
        "account TEXT NOT NULL, "
        "action TEXT NOT NULL CHECK (action IN ('quarantine', 'release')), "
        "reason TEXT NOT NULL, "
        "at TEXT NOT NULL)"
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS {_ACTIONS_TABLE}_event_idx "
        f"ON {_ACTIONS_TABLE}(event_id, account, id)"
    )


def _validate_event_id(event_id):
    if isinstance(event_id, bool) or not isinstance(event_id, int):
        raise TypeError("event_id must be a positive integer")
    if event_id <= 0:
        raise ValueError("event_id must be a positive integer")


def _validate_reason(reason, quarantined):
    if not isinstance(reason, str):
        raise TypeError("reason must be a string")
    if len(reason) > _OUTBOX_REASON_MAX_LENGTH:
        raise ValueError(
            f"reason must be at most {_OUTBOX_REASON_MAX_LENGTH} characters"
        )
    if quarantined and not reason.strip():
        raise ValueError("reason must be a non-empty string when quarantining")


def _weekly_revision(event_key):
    if not isinstance(event_key, str):
        return None
    match = _WEEKLY_EVENT_RE.fullmatch(event_key)
    if match is None:
        return None
    week_text = match.group(1)
    try:
        date.fromisoformat(week_text)
        revision = int(match.group(2))
    except ValueError:
        return None
    return week_text, revision

def _pending_metadata(connection, account):
    last_id = 0
    while True:
        rows = connection.execute(
            f"SELECT id, event_key, COALESCE(quarantined, 0) FROM {_TABLE} "
            "WHERE account = ? AND delivered = 0 AND id > ? "
            "ORDER BY id LIMIT 256",
            (account, last_id),
        ).fetchall()
        if not rows:
            return
        yield from rows
        last_id = rows[-1][0]


def _next_eligible_event(connection, account):
    earliest_pending_revision = {}
    for _event_id, event_key, _quarantined in _pending_metadata(connection, account):
        parsed = _weekly_revision(event_key)
        if parsed is not None:
            week_text, revision = parsed
            previous = earliest_pending_revision.get(week_text)
            if previous is None or revision < previous:
                earliest_pending_revision[week_text] = revision

    for event_id, _event_key, quarantined in _pending_metadata(connection, account):
        if quarantined:
            continue
        parsed = _weekly_revision(_event_key)
        if parsed is not None:
            week_text, revision = parsed
            if earliest_pending_revision[week_text] < revision:
                continue
        return event_id
    return None


def set_quarantine(connection, account, event_id, quarantined, reason):
    """Set pending event quarantine state and append explicit user audit."""
    _validate_scope(account, "pending")
    _validate_event_id(event_id)
    if type(quarantined) is not bool:
        raise TypeError("quarantined must be a bool")
    _validate_reason(reason, quarantined)
    _ensure_table(connection)

    row = connection.execute(
        f"SELECT delivered FROM {_TABLE} WHERE id = ? AND account = ?",
        (event_id, account),
    ).fetchone()
    if row is None:
        raise ValueError("event_id does not identify an event for account")
    if row[0] != 0:
        raise ValueError("delivered events cannot be quarantined or released")

    timestamp = datetime.now(timezone.utc).isoformat()
    if quarantined:
        cursor = connection.execute(
            f"UPDATE {_TABLE} SET quarantined = 1, quarantine_reason = ?, "
            "quarantined_at = ? WHERE id = ? AND account = ? AND delivered = 0",
            (reason, timestamp, event_id, account),
        )
        action = "quarantine"
    else:
        cursor = connection.execute(
            f"UPDATE {_TABLE} SET quarantined = 0, quarantine_reason = NULL, "
            "quarantined_at = NULL WHERE id = ? AND account = ? AND delivered = 0",
            (event_id, account),
        )
        action = "release"
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("outbox event changed during quarantine update")
    connection.execute(
        f"INSERT INTO {_ACTIONS_TABLE}(event_id, account, action, reason, at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, account, action, reason, timestamp),
    )


def list_events(connection, account, limit=50, after_id=None):
    """Read event metadata without changing schema or data.

    ``after_id`` is an exclusive page cursor: only IDs greater than it are
    returned. Existing callers can keep using the three-argument form.
    """
    _validate_scope(account, "pending")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if after_id is None:
        after_id = 0
    elif isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
        raise ValueError("after_id must be a non-negative integer")
    if not table_exists(connection, _TABLE):
        return []

    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_TABLE})")}
    if not {"id", "account", "delivered"}.issubset(columns):
        return []
    selected = {
        "attempts": "attempts" if "attempts" in columns else "0",
        "last_attempt_at": "last_attempt_at" if "last_attempt_at" in columns else "NULL",
        "last_error": "last_error" if "last_error" in columns else "NULL",
        "quarantined": "COALESCE(quarantined, 0)" if "quarantined" in columns else "0",
        "quarantine_reason": "quarantine_reason" if "quarantine_reason" in columns else "NULL",
        "quarantined_at": "quarantined_at" if "quarantined_at" in columns else "NULL",
    }
    rows = connection.execute(
        f"SELECT id, delivered, {selected['quarantined']}, {selected['attempts']}, "
        f"{selected['last_error']}, {selected['last_attempt_at']}, "
        f"{selected['quarantine_reason']}, {selected['quarantined_at']} "
        f"FROM {_TABLE} WHERE account = ? AND id > ? ORDER BY id LIMIT ?",
        (account, after_id, limit),
    ).fetchall()
    return [
        {
            "id": row[0],
            "delivered": row[1],
            "quarantined": row[2],
            "attempts": row[3],
            "last_error": row[4],
            "last_attempt_at": row[5],
            "quarantine_reason": row[6],
            "quarantined_at": row[7],
        }
        for row in rows
    ]


def _commit_event_update(connection, statement, parameters):
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(statement, parameters)
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("outbox event changed during delivery")
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def _validate_scope(account, event_key):
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account must be a nonempty string")
    if not isinstance(event_key, str) or not event_key.strip():
        raise ValueError("event_key must be a nonempty string")


def _reject_transport_fields(payload):
    values = [payload]
    seen = set()
    while values:
        value = values.pop()
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                continue
            seen.add(id(value))
        if isinstance(value, dict):
            for key, nested in value.items():
                if isinstance(key, str) and key.casefold() in {"device_key", "bank_key", "url"}:
                    raise ValueError("payload must not include credential or transport fields")
                values.append(nested)
        elif isinstance(value, list):
            values.extend(value)


def has_event(connection, account, event_key):
    """Return whether the key is already queued or delivered; never creates tables."""
    _validate_scope(account, event_key)
    if not table_exists(connection, _TABLE):
        return False
    return connection.execute(
        f"SELECT 1 FROM {_TABLE} WHERE account = ? AND event_key = ?",
        (account, event_key),
    ).fetchone() is not None


def enqueue(connection, account, event_key, payload):
    """Queue JSON object, without committing caller's transaction."""
    _validate_scope(account, event_key)
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    _reject_transport_fields(payload)
    try:
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if json.loads(serialized) != payload:
            raise ValueError("payload must use JSON-native values and string object keys")
    except (TypeError, ValueError):
        raise ValueError("payload must contain only finite JSON values and string object keys") from None

    _ensure_table(connection)
    connection.execute(
        f"INSERT INTO {_TABLE}(account, event_key, payload) VALUES (?, ?, ?) "
        "ON CONFLICT(account, event_key) DO NOTHING",
        (account, event_key, serialized),
    )
    row = connection.execute(
        f"SELECT id, payload FROM {_TABLE} WHERE account = ? AND event_key = ?",
        (account, event_key),
    ).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("outbox insert did not produce an event")
    event_id, existing_payload = row
    if existing_payload != serialized:
        raise ValueError("event_key already exists with a different payload")
    return event_id


def drain(connection, account, send_callback, limit=20):
    """Send eligible committed pending events FIFO; return confirmed count."""
    if connection.in_transaction:
        raise RuntimeError("drain requires a connection with no open transaction")
    _validate_scope(account, "pending")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if not callable(send_callback):
        raise ValueError("send_callback must be callable")

    _ensure_table(connection)
    sent = 0
    while sent < limit:
        event_id = _next_eligible_event(connection, account)
        if event_id is None:
            break
        row = connection.execute(
            f"SELECT payload FROM {_TABLE} "
            "WHERE id = ? AND account = ? AND delivered = 0 "
            "AND COALESCE(quarantined, 0) = 0",
            (event_id, account),
        ).fetchone()
        if row is None:
            continue
        payload = json.loads(row[0])
        _commit_event_update(
            connection,
            f"UPDATE {_TABLE} SET attempts = attempts + 1, last_attempt_at = ? "
            "WHERE id = ? AND account = ? AND delivered = 0 "
            "AND COALESCE(quarantined, 0) = 0",
            (datetime.now(timezone.utc).isoformat(), event_id, account),
        )
        try:
            response = send_callback(payload)
        except (Exception, SystemExit) as error:
            _commit_event_update(
                connection,
                f"UPDATE {_TABLE} SET last_error = ? "
                "WHERE id = ? AND account = ? AND delivered = 0",
                ("callback_error", event_id, account),
            )
            raise OutboxSendError(
                f"notification outbox event {event_id} send failed ({type(error).__name__})"
            ) from None
        if not isinstance(response, dict) or type(response.get("code")) is not int or response["code"] != 200:
            _commit_event_update(
                connection,
                f"UPDATE {_TABLE} SET last_error = ? "
                "WHERE id = ? AND account = ? AND delivered = 0",
                ("unconfirmed_response", event_id, account),
            )
            raise OutboxSendError(
                f"notification outbox event {event_id} not confirmed (expected response code 200)"
            )

        _commit_event_update(
            connection,
            f"UPDATE {_TABLE} SET delivered = 1, last_error = NULL "
            "WHERE id = ? AND account = ? AND delivered = 0 "
            "AND COALESCE(quarantined, 0) = 0",
            (event_id, account),
        )
        sent += 1
    return sent
