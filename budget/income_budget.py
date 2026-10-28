"""Income budget state for each account."""

import calendar
import re
import sqlite3
from datetime import date, datetime

from common import VIETNAM_TZ, table_exists

_MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def ensure_state(connection, account, today, days):
    """Create the account state once. Return the stored start month."""
    if not connection.in_transaction:
        raise RuntimeError("ensure_state requires a caller-owned transaction")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS income_state ("
        "account TEXT PRIMARY KEY, start_month TEXT NOT NULL, coverage_from TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    row = connection.execute(
        "SELECT start_month FROM income_state WHERE account = ?", (account,)
    ).fetchone()
    if row is not None:
        return row[0]
    # An empty payload has no earliest day, so coverage starts on the run day.
    start_month = f"{today:%Y-%m}"
    coverage_from = min(days) if days else today.isoformat()
    connection.execute(
        "INSERT INTO income_state(account, start_month, coverage_from, created_at) VALUES (?, ?, ?, ?)",
        (account, start_month, coverage_from, datetime.now(VIETNAM_TZ).isoformat()),
    )
    return start_month


def share_of_day(total, days, index):
    """Return the share of day index (0-based) when total is split over days (plan D5)."""
    quotient, remainder = divmod(total, days)
    return quotient + 1 if index < remainder else quotient


def split_evenly(pool, days):
    """Split pool over days. The first remainder days get one extra unit."""
    return tuple(share_of_day(pool, days, index) for index in range(days))


def parse_month(month):
    if not isinstance(month, str) or not _MONTH.fullmatch(month):
        raise ValueError("month must be YYYY-MM")
    return int(month[:4]), int(month[5:])


def _shift(year, month, offset):
    year_index, month_index = divmod(year * 12 + month - 1 + offset, 12)
    return year_index, month_index + 1


def month_key(year, month):
    # Build bounds as text: date arithmetic overflows at year 1 and year 9999.
    return f"{year:04d}-{month:02d}"


def _day_text(value):
    """Return a stored day when it is a valid ISO date; otherwise raise DatabaseError."""
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise sqlite3.DatabaseError("invalid stored income date")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise sqlite3.DatabaseError("invalid stored income date") from error
    return value


def stored_amount(value):
    """Return a stored amount when it is a non-negative int; otherwise raise DatabaseError."""
    if type(value) is not int or value < 0:
        raise sqlite3.DatabaseError("invalid stored amount")
    return value


def read_state(connection, account):
    """Return the stored start month and coverage start, or None. Reads only."""
    if not table_exists(connection, "income_state"):
        return None
    row = connection.execute(
        "SELECT start_month, coverage_from FROM income_state WHERE account = ?", (account,),
    ).fetchone()
    if row is None:
        return None
    start_month, coverage_from = row
    if not isinstance(start_month, str) or not _MONTH.fullmatch(start_month):
        raise sqlite3.DatabaseError("invalid stored start month")
    return {"start_month": start_month, "coverage_from": _day_text(coverage_from)}


def shift_join(connection):
    """Return (join SQL, shifted flag SQL) for incoming_transactions AS i; the shifts table may not exist."""
    if not table_exists(connection, "income_budget_shifts"):
        return "", "0"
    return ("LEFT JOIN income_budget_shifts AS s ON s.account = i.account "
            "AND s.transaction_id = i.transaction_id ", "s.transaction_id IS NOT NULL")


def incoming_rows(connection, account, start, end):
    """Return (day, amount, shifted) for IN dated in [start, end). Reads only."""
    if not table_exists(connection, "incoming_transactions"):
        return []
    join, shifted = shift_join(connection)
    rows = connection.execute(
        f"SELECT i.day, i.amount, {shifted} FROM incoming_transactions AS i {join}"
        "WHERE i.account = ? AND i.day >= ? AND i.day < ?",
        (account, start, end),
    ).fetchall()
    return [(_day_text(day), stored_amount(amount), bool(flag)) for day, amount, flag in rows]


def income_window(month):
    """Return the [start, end) day texts that month_figures reads for a YYYY-MM month."""
    year, number = parse_month(month)
    previous = month_key(*_shift(year, number, -1))
    following = month_key(*_shift(year, number, 1))
    return f"{previous}-01", f"{following}-01"


def figures_from_rows(state, month, rows):
    """Return the income figures of a month from stored rows, without any SQL.

    rows are the incoming_rows of income_window(month). The caller can reuse one
    read for these figures and for other per-day sums.
    """
    if state is None or month < state["start_month"]:
        return None
    income = shifted_out = shifted_in = 0
    for day, amount, shifted in rows:
        if day[:7] == month:
            income += amount
            if shifted:
                shifted_out += amount
        elif shifted:
            shifted_in += amount
    return {"start_month": state["start_month"], "coverage_from": state["coverage_from"],
            "income": income, "shifted_in": shifted_in, "shifted_out": shifted_out,
            "pool": income - shifted_out + shifted_in}


def month_figures(connection, account, month):
    """Return the income figures of a YYYY-MM month, or None before the start month or without state."""
    start, end = income_window(month)
    state = read_state(connection, account)
    if state is None or month < state["start_month"]:
        return None
    return figures_from_rows(state, month, incoming_rows(connection, account, start, end))


def actual_daily_budgets(connection, account, month):
    """Return the actual daily budget of every day in a YYYY-MM month, or None when unknown."""
    figures = month_figures(connection, account, month)
    if figures is None:
        return None
    year, number = parse_month(month)
    return split_evenly(figures["pool"], calendar.monthrange(year, number)[1])


def budget_month(day, shifted, start_month):
    """Return the YYYY-MM month that counts an IN dated day, or None when it is not counted."""
    text = month_key(*_shift(day.year, day.month, 1 if shifted else 0))
    return text if start_month is not None and text >= start_month else None
