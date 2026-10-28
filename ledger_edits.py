"""Audited edits shared by the CLI and the dashboard. Callers own the transaction."""
import json
import unicodedata

from common import CONTROL_CHARS, table_exists

REASON_MAX = 200
LABEL_MAX = 40


class ConflictStateError(RuntimeError):
    """The conflict is not open, or the stored row changed since it was recorded."""


def _text(value, name, limit):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    cleaned = " ".join(unicodedata.normalize("NFC", value).split())
    if not cleaned or len(cleaned) > limit or CONTROL_CHARS.search(value):
        raise ValueError(f"{name} must be 1-{limit} characters without control characters")
    return cleaned


def _require_transaction(connection):
    if not connection.in_transaction:
        raise RuntimeError("edits require a caller-owned transaction")


def ensure_tables(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS transaction_conflicts ("
        "account TEXT NOT NULL, conflict_key TEXT NOT NULL, transaction_id TEXT NOT NULL, "
        "stored_day TEXT NOT NULL, stored_amount INTEGER NOT NULL, "
        "incoming_day TEXT NOT NULL, incoming_amount INTEGER NOT NULL, "
        "first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, seen_count INTEGER NOT NULL, "
        "status TEXT NOT NULL CHECK(status IN ('open', 'accepted', 'superseded')), "
        "resolved_at TEXT, resolution_reason TEXT, PRIMARY KEY (account, conflict_key))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS edit_audit ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, at TEXT NOT NULL, "
        "action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL, reason TEXT)"
    )


def _audit(connection, account, at, action, target, detail, reason=None):
    connection.execute(
        "INSERT INTO edit_audit(account, at, action, target, detail, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (account, at, action, target, json.dumps(detail, ensure_ascii=False, sort_keys=True), reason),
    )


def record_conflict(connection, account, conflict_key, conflict, at):
    """Insert or refresh an open conflict. Accepted or superseded rows stay unchanged."""
    _require_transaction(connection)
    ensure_tables(connection)
    transaction_id, stored_day, stored_amount, incoming_day, incoming_amount = conflict
    connection.execute(
        "INSERT INTO transaction_conflicts (account, conflict_key, transaction_id, stored_day, stored_amount, "
        "incoming_day, incoming_amount, first_seen_at, last_seen_at, seen_count, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'open') "
        "ON CONFLICT(account, conflict_key) DO UPDATE SET last_seen_at = excluded.last_seen_at, "
        "seen_count = seen_count + 1 WHERE status = 'open'",
        (account, conflict_key, transaction_id, stored_day, stored_amount,
         incoming_day, incoming_amount, at, at),
    )


def open_conflicts(connection, account, limit=50):
    """Read open conflicts, newest first. A missing table means none. Never writes."""
    if not table_exists(connection, "transaction_conflicts"):
        return []
    rows = connection.execute(
        "SELECT conflict_key, transaction_id, stored_day, stored_amount, incoming_day, incoming_amount, "
        "first_seen_at, last_seen_at, seen_count FROM transaction_conflicts "
        "WHERE account = ? AND status = 'open' ORDER BY last_seen_at DESC, conflict_key LIMIT ?",
        (account, limit),
    ).fetchall()
    names = ("key", "transaction_id", "stored_day", "stored_amount", "incoming_day", "incoming_amount",
             "first_seen_at", "last_seen_at", "seen_count")
    return [dict(zip(names, row)) for row in rows]


def latest_open_conflict_key(connection, account, transaction_id):
    if not table_exists(connection, "transaction_conflicts"):
        return None
    row = connection.execute(
        "SELECT conflict_key FROM transaction_conflicts WHERE account = ? AND transaction_id = ? "
        "AND status = 'open' ORDER BY last_seen_at DESC, conflict_key LIMIT 1",
        (account, transaction_id),
    ).fetchone()
    return row[0] if row else None


def ensure_label_table(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS transaction_labels ("
        "account TEXT NOT NULL, transaction_id TEXT NOT NULL, label TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, PRIMARY KEY (account, transaction_id))"
    )


def read_labels(connection, account):
    """Return {transaction_id: label}. A missing table means no labels. Never writes."""
    if not table_exists(connection, "transaction_labels"):
        return {}
    return dict(connection.execute(
        "SELECT transaction_id, label FROM transaction_labels WHERE account = ?", (account,)
    ).fetchall())


def label_counts(connection, account):
    """Return [(label, count)] by count descending, then label. Never writes."""
    if not table_exists(connection, "transaction_labels"):
        return []
    return connection.execute(
        "SELECT label, COUNT(*) FROM transaction_labels WHERE account = ? "
        "GROUP BY label ORDER BY COUNT(*) DESC, label", (account,)
    ).fetchall()


def _check_transaction_id(transaction_id):
    if not isinstance(transaction_id, str) or not 0 < len(transaction_id) <= 200:
        raise ValueError("transaction_id must be 1-200 characters")


def set_label(connection, account, transaction_id, label, at):
    """Set or clear one label. A None or blank label clears it. Returns {"changed": bool}."""
    _require_transaction(connection)
    _check_transaction_id(transaction_id)
    blank = label is None or (isinstance(label, str) and not label.strip() and not CONTROL_CHARS.search(label))
    cleaned = None if blank else _text(label, "label", LABEL_MAX)
    if connection.execute(
        "SELECT 1 FROM transactions WHERE account = ? AND transaction_id = ?", (account, transaction_id)
    ).fetchone() is None:
        raise LookupError("transaction not found")
    if cleaned is None and not table_exists(connection, "transaction_labels"):
        return {"changed": False}
    ensure_label_table(connection)
    row = connection.execute(
        "SELECT label FROM transaction_labels WHERE account = ? AND transaction_id = ?",
        (account, transaction_id),
    ).fetchone()
    before = row[0] if row else None
    if before == cleaned:
        return {"changed": False}
    if cleaned is None:
        connection.execute(
            "DELETE FROM transaction_labels WHERE account = ? AND transaction_id = ?",
            (account, transaction_id),
        )
    else:
        connection.execute(
            "INSERT INTO transaction_labels(account, transaction_id, label, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(account, transaction_id) DO UPDATE SET label = excluded.label, "
            "updated_at = excluded.updated_at",
            (account, transaction_id, cleaned, at),
        )
    ensure_tables(connection)
    _audit(connection, account, at, "clear_label" if cleaned is None else "set_label",
           transaction_id, {"before": before, "after": cleaned})
    return {"changed": True}


def ensure_income_shift_table(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS income_budget_shifts ("
        "account TEXT NOT NULL, transaction_id TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (account, transaction_id))"
    )


def set_income_shift(connection, account, transaction_id, next_month, at):
    """Count one IN for the next month (True) or its own month (False). Returns {"changed": bool}."""
    _require_transaction(connection)
    _check_transaction_id(transaction_id)
    if type(next_month) is not bool:
        raise ValueError("next_month must be a boolean")
    if not table_exists(connection, "incoming_transactions") or connection.execute(
        "SELECT 1 FROM incoming_transactions WHERE account = ? AND transaction_id = ?",
        (account, transaction_id),
    ).fetchone() is None:
        raise LookupError("transaction not found")
    # Turning the shift off must not create the table.
    if not next_month and not table_exists(connection, "income_budget_shifts"):
        return {"changed": False}
    ensure_income_shift_table(connection)
    if next_month:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO income_budget_shifts(account, transaction_id, updated_at) VALUES (?, ?, ?)",
            (account, transaction_id, at),
        )
    else:
        cursor = connection.execute(
            "DELETE FROM income_budget_shifts WHERE account = ? AND transaction_id = ?",
            (account, transaction_id),
        )
    if not cursor.rowcount:
        return {"changed": False}
    ensure_tables(connection)
    _audit(connection, account, at, "shift_income" if next_month else "unshift_income",
           transaction_id, {"next_month": next_month})
    return {"changed": True}


def accept_conflict(connection, account, conflict_key, reason, at):
    """Replace the stored bank day and amount with the incoming values. Returns a summary dict."""
    _require_transaction(connection)
    reason = _text(reason, "reason", REASON_MAX)
    if not table_exists(connection, "transaction_conflicts"):
        raise LookupError("conflict not found")
    row = connection.execute(
        "SELECT transaction_id, stored_day, stored_amount, incoming_day, incoming_amount, status "
        "FROM transaction_conflicts WHERE account = ? AND conflict_key = ?",
        (account, conflict_key),
    ).fetchone()
    if row is None:
        raise LookupError("conflict not found")
    transaction_id, stored_day, stored_amount, incoming_day, incoming_amount, status = row
    if status != "open":
        raise ConflictStateError("conflict is not open")
    cursor = connection.execute(
        "UPDATE transactions SET day = ?, amount = ? "
        "WHERE account = ? AND transaction_id = ? AND day = ? AND amount = ?",
        (incoming_day, incoming_amount, account, transaction_id, stored_day, stored_amount),
    )
    if cursor.rowcount != 1:
        raise ConflictStateError("stored transaction changed since the conflict was recorded")
    connection.execute(
        "UPDATE transaction_conflicts SET status = 'accepted', resolved_at = ?, resolution_reason = ? "
        "WHERE account = ? AND conflict_key = ?",
        (at, reason, account, conflict_key),
    )
    connection.execute(
        "UPDATE transaction_conflicts SET status = 'superseded', resolved_at = ?, "
        "resolution_reason = 'another conflict of this transaction was accepted' "
        "WHERE account = ? AND transaction_id = ? AND status = 'open'",
        (at, account, transaction_id),
    )
    ensure_tables(connection)
    _audit(connection, account, at, "accept_conflict", transaction_id,
           {"conflict_key": conflict_key, "before": [stored_day, stored_amount],
            "after": [incoming_day, incoming_amount]}, reason)
    return {"transaction_id": transaction_id, "day": incoming_day, "amount": incoming_amount}
