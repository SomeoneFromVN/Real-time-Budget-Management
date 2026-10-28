from datetime import date, datetime
from collections.abc import Mapping, Sequence
import sqlite3


_ALERT_KINDS = ("near", "over")


def parse_alert_settings(environ: Mapping) -> tuple[int, str]:
    """Parse and validate budget alert environment settings."""
    raw_percent = environ.get("BUDGET_ALERT_PERCENT", "80")
    if isinstance(raw_percent, bool):
        raise ValueError("BUDGET_ALERT_PERCENT must be an integer from 1 to 99")
    try:
        threshold_percent = int(raw_percent)
    except (TypeError, ValueError):
        raise ValueError("BUDGET_ALERT_PERCENT must be an integer from 1 to 99") from None
    if not isinstance(raw_percent, int) and str(raw_percent).strip() != str(threshold_percent):
        raise ValueError("BUDGET_ALERT_PERCENT must be an integer from 1 to 99")
    if not 1 <= threshold_percent <= 99:
        raise ValueError("BUDGET_ALERT_PERCENT must be an integer from 1 to 99")

    mode = environ.get("NOTIFICATION_MODE", "all")
    if mode not in ("all", "thresholds"):
        raise ValueError("NOTIFICATION_MODE must be 'all' or 'thresholds'")
    return threshold_percent, mode


def _validate_amounts(spent: int, budget: int, threshold_percent: int) -> None:
    for name, value in (("spent", spent), ("budget", budget), ("threshold_percent", threshold_percent)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if not 1 <= threshold_percent <= 99:
        raise ValueError("threshold_percent must be from 1 to 99")


def _validate_scope(account: str, day: date) -> tuple[str, str]:
    if not isinstance(account, str) or not account:
        raise ValueError("account must be a nonempty string")
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError("day must be a date")
    return account, day.isoformat()


def _ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS budget_alerts ("
        "account TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL "
        "CHECK (kind IN ('near', 'over')), PRIMARY KEY (account, day, kind))"
    )


def pending_alerts(
    connection: sqlite3.Connection,
    account: str,
    day: date,
    spent: int,
    budget: int,
    threshold_percent: int,
) -> list[str]:
    _validate_amounts(spent, budget, threshold_percent)
    account, day_key = _validate_scope(account, day)
    _ensure_table(connection)
    sent = {
        row[0]
        for row in connection.execute(
            "SELECT kind FROM budget_alerts WHERE account = ? AND day = ?",
            (account, day_key),
        )
    }
    pending = []
    if spent > 0 and budget > 0 and spent * 100 >= budget * threshold_percent and "near" not in sent:
        pending.append("near")
    if spent > budget and "over" not in sent:
        pending.append("over")
    return pending


def mark_alerts(connection: sqlite3.Connection, account: str, day: date, kinds: Sequence[str]) -> None:
    account, day_key = _validate_scope(account, day)
    if isinstance(kinds, (str, bytes)):
        raise ValueError("kinds must contain alert codes")
    kinds = tuple(kinds)
    if any(kind not in _ALERT_KINDS for kind in kinds):
        raise ValueError("kinds must contain only 'near' or 'over'")
    _ensure_table(connection)
    connection.executemany(
        "INSERT OR IGNORE INTO budget_alerts (account, day, kind) VALUES (?, ?, ?)",
        ((account, day_key, kind) for kind in kinds),
    )
