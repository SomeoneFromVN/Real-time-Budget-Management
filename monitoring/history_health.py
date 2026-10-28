import json
import sqlite3
from datetime import datetime


API_WINDOW_CAP = 50
RECONCILIATION_REASON_MAX_LENGTH = 1000


def _validate_account(account: str) -> None:
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account must be a non-empty string")


def _validate_checked_at(checked_at: datetime) -> None:
    if not isinstance(checked_at, datetime) or checked_at.utcoffset() is None:
        raise ValueError("checked_at must be an aware datetime")


def _ensure_history_health_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS history_health ("
        "account TEXT PRIMARY KEY, transaction_count INTEGER NOT NULL, "
        "checked_at TEXT NOT NULL, transaction_ids TEXT NOT NULL, "
        "gap_suspected INTEGER NOT NULL DEFAULT 0, "
        "history_unknown INTEGER NOT NULL DEFAULT 1)"
    )
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(history_health)")
    }
    if "history_unknown" not in columns:
        connection.execute(
            "ALTER TABLE history_health ADD COLUMN "
            "history_unknown INTEGER NOT NULL DEFAULT 1"
        )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS history_reconciliation_audit ("
        "audit_id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, "
        "reconciled_at TEXT NOT NULL, reason TEXT NOT NULL, "
        "prior_gap_suspected INTEGER NOT NULL, "
        "prior_history_unknown INTEGER NOT NULL, "
        "baseline_checked_at TEXT NOT NULL)"
    )


def _stored_transaction_ids(value) -> set[str] | None:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list) or not decoded:
        return None

    identifiers = set()
    for identifier in decoded:
        if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
            return None
        if isinstance(identifier, str) and not identifier.strip():
            return None
        identifiers.add(str(identifier))
    return identifiers


def _valid_baseline(row) -> bool:
    transaction_count, checked_at, transaction_ids, _, _ = row
    if (
        isinstance(transaction_count, bool)
        or not isinstance(transaction_count, int)
        or transaction_count <= 0
        or not isinstance(checked_at, str)
        or not checked_at.strip()
        or _stored_transaction_ids(transaction_ids) is None
    ):
        return False
    try:
        baseline_time = datetime.fromisoformat(checked_at)
    except (TypeError, ValueError):
        return False
    return baseline_time.utcoffset() is not None


def _window_identifiers(transactions):
    identifiers = set()
    invalid_id = False
    for transaction in transactions:
        if not isinstance(transaction, dict) or "transactionID" not in transaction:
            invalid_id = True
            continue
        value = transaction["transactionID"]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            invalid_id = True
            continue
        if isinstance(value, str) and not value.strip():
            invalid_id = True
            continue
        identifiers.add(str(value))

    return identifiers, invalid_id


def _mark_gap(connection, account, previous, warnings):
    connection.execute(
        "UPDATE history_health SET gap_suspected = 1 WHERE account = ?", (account,)
    )
    if previous[2]:
        warnings.append("Độ bao phủ vẫn chưa được xác minh; cần đối soát tường minh.")
    return warnings


def record_history_window(
    connection: sqlite3.Connection,
    account: str,
    transactions: list,
    checked_at: datetime,
) -> list[str]:
    """Warn on uncertain API windows; leave transaction control to the caller."""
    _validate_account(account)
    if not isinstance(transactions, list):
        raise TypeError("transactions must be a list")
    _validate_checked_at(checked_at)
    _ensure_history_health_schema(connection)

    previous = connection.execute(
        "SELECT transaction_count, transaction_ids, gap_suspected, history_unknown "
        "FROM history_health WHERE account = ?",
        (account,),
    ).fetchone()

    warnings = []
    if not transactions:
        if previous is None:
            return ["Cửa sổ rỗng; chưa có lịch sử không rỗng để xác minh."]
        return _mark_gap(connection, account, previous, [
            "Cửa sổ rỗng sau cửa sổ không rỗng; giữ nguyên bằng chứng cũ."
        ])

    identifiers, invalid_id = _window_identifiers(transactions)

    full_window = len(transactions) >= API_WINDOW_CAP
    if full_window:
        warnings.append("API trả về ít nhất 50 giao dịch; cửa sổ có thể đã chạm giới hạn.")
    if invalid_id:
        warnings.append("Thiếu hoặc sai ID giao dịch; chưa thể xác minh đầy đủ cửa sổ.")
        if previous is None:
            return warnings
        return _mark_gap(connection, account, previous, warnings)

    if previous is None:
        warnings.append("Ảnh chụp lịch sử đầu tiên; độ bao phủ trước đó chưa được xác minh.")
        gap_suspected = full_window
        history_unknown = 1
    else:
        previous_ids = _stored_transaction_ids(previous[1]) or set()
        overlap = identifiers.intersection(previous_ids)
        if not overlap:
            warnings.append("Không có ID giao nhau với cửa sổ trước; có thể thiếu lịch sử.")
        same_reconciled_full_window = (
            full_window
            and not previous[2]
            and not previous[3]
            and previous[0] == len(transactions)
            and previous_ids == identifiers
        )
        gap_suspected = bool(
            previous[2] or not overlap or (full_window and not same_reconciled_full_window)
        )
        history_unknown = previous[3]
        if previous[2] and overlap:
            warnings.append("Độ bao phủ vẫn chưa được xác minh; cần đối soát tường minh.")

    connection.execute(
        "INSERT INTO history_health "
        "(account, transaction_count, checked_at, transaction_ids, "
        "gap_suspected, history_unknown) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account) DO UPDATE SET "
        "transaction_count = excluded.transaction_count, "
        "checked_at = excluded.checked_at, "
        "transaction_ids = excluded.transaction_ids, "
        "gap_suspected = excluded.gap_suspected, "
        "history_unknown = excluded.history_unknown",
        (
            account,
            len(transactions),
            checked_at.isoformat(),
            json.dumps(sorted(identifiers), separators=(",", ":")),
            int(gap_suspected),
            int(history_unknown),
        ),
    )
    return warnings


def reconcile_history(
    connection: sqlite3.Connection,
    account: str,
    checked_at: datetime,
    reason: str,
) -> None:
    """Record user reconciliation without fetching or changing transaction evidence."""
    _validate_account(account)
    _validate_checked_at(checked_at)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    if len(reason) > RECONCILIATION_REASON_MAX_LENGTH:
        raise ValueError(
            "reason must be at most "
            f"{RECONCILIATION_REASON_MAX_LENGTH} characters"
        )

    _ensure_history_health_schema(connection)
    baseline = connection.execute(
        "SELECT transaction_count, checked_at, transaction_ids, "
        "gap_suspected, history_unknown "
        "FROM history_health WHERE account = ?",
        (account,),
    ).fetchone()
    if baseline is None or not _valid_baseline(baseline):
        raise ValueError("account must have an existing non-empty valid baseline")

    connection.execute(
        "INSERT INTO history_reconciliation_audit "
        "(account, reconciled_at, reason, prior_gap_suspected, "
        "prior_history_unknown, baseline_checked_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            account,
            checked_at.isoformat(),
            reason,
            int(bool(baseline[3])),
            int(bool(baseline[4])),
            baseline[1],
        ),
    )
    connection.execute(
        "UPDATE history_health SET gap_suspected = 0, history_unknown = 0 "
        "WHERE account = ?",
        (account,),
    )
