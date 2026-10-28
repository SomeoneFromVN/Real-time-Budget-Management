import calendar
from datetime import date, datetime, timedelta

from budget import income_budget
from common import SQLITE_INT64_MAX as _MAX_BUDGET, table_exists
_DAILY_TABLE = "budget_history"
_MONTHLY_TABLE = "monthly_budget_history"


def _valid_day(day):
    if not isinstance(day, date) or isinstance(day, datetime):
        raise TypeError("day must be a date")
    return day.isoformat()


def _valid_budget(amount):
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError("configured_amount must be an integer")
    if amount < 0 or amount > _MAX_BUDGET:
        raise ValueError("configured_amount must fit a non-negative SQLite integer")


def _monthly_share(amount, day):
    return income_budget.share_of_day(amount, calendar.monthrange(day.year, day.month)[1], day.day - 1)


def record_monthly_budget(connection, account, today, monthly_amount):
    today_text = _valid_day(today)
    _valid_budget(monthly_amount)
    tomorrow_text = (today + timedelta(days=1)).isoformat()
    connection.execute(
        "CREATE TABLE IF NOT EXISTS monthly_budget_history ("
        "account TEXT NOT NULL, effective_day TEXT NOT NULL, amount INTEGER NOT NULL "
        "CHECK(amount >= 0), PRIMARY KEY(account, effective_day))"
    )
    current = connection.execute(
        "SELECT amount FROM monthly_budget_history "
        "WHERE account = ? AND effective_day <= ? ORDER BY effective_day DESC LIMIT 1",
        (account, today_text),
    ).fetchone()
    if current is None:
        connection.execute(
            "INSERT INTO monthly_budget_history(account, effective_day, amount) VALUES (?, ?, ?)",
            (account, today_text, monthly_amount),
        )
        return monthly_amount

    effective_today = current[0]
    if monthly_amount == effective_today:
        connection.execute(
            "DELETE FROM monthly_budget_history WHERE account = ? AND effective_day = ?",
            (account, tomorrow_text),
        )
    else:
        connection.execute(
            "INSERT INTO monthly_budget_history(account, effective_day, amount) VALUES (?, ?, ?) "
            "ON CONFLICT(account, effective_day) DO UPDATE SET amount = excluded.amount",
            (account, tomorrow_text, monthly_amount),
        )
    return effective_today


def _latest_amount(connection, table, account, day_text):
    if not table_exists(connection, table):
        return None
    row = connection.execute(
        f"SELECT amount FROM {table} WHERE account = ? AND effective_day <= ? "
        "ORDER BY effective_day DESC LIMIT 1",
        (account, day_text),
    ).fetchone()
    return None if row is None else row[0]


def forecast_for_day(connection, account, day):
    day_text = _valid_day(day)
    monthly_amount = _latest_amount(connection, _MONTHLY_TABLE, account, day_text)
    if monthly_amount is not None:
        return _monthly_share(monthly_amount, day)
    return _latest_amount(connection, _DAILY_TABLE, account, day_text)


def _history_rows(connection, table, account, end_text):
    if not table_exists(connection, table):
        return []
    return connection.execute(
        f"SELECT effective_day, amount FROM {table} "
        "WHERE account = ? AND effective_day < ? ORDER BY effective_day",
        (account, end_text),
    ).fetchall()


def _forecast_days(connection, account, start, end):
    """Yield the forecast of every day in [start, end) from one read of each history table."""
    monthly = _history_rows(connection, _MONTHLY_TABLE, account, end.isoformat())
    daily = _history_rows(connection, _DAILY_TABLE, account, end.isoformat())
    monthly_index = daily_index = 0
    monthly_amount = daily_amount = None
    day = start
    while day < end:
        day_text = day.isoformat()
        while monthly_index < len(monthly) and monthly[monthly_index][0] <= day_text:
            monthly_amount = monthly[monthly_index][1]
            monthly_index += 1
        while daily_index < len(daily) and daily[daily_index][0] <= day_text:
            daily_amount = daily[daily_index][1]
            daily_index += 1
        # Any monthly row on or before the day wins over the legacy daily table (plan D4).
        yield _monthly_share(monthly_amount, day) if monthly_amount is not None else daily_amount
        day += timedelta(days=1)


def forecast_for_period(connection, account, start, end):
    _valid_day(start)
    _valid_day(end)
    if start >= end:
        return 0, (end - start).days
    total = 0
    unknown_days = 0
    for value in _forecast_days(connection, account, start, end):
        if value is None:
            unknown_days += 1
        else:
            total += value
    return total, unknown_days


def forecast_for_month(connection, account, month):
    """Return the forecast of every day in a YYYY-MM month; None marks an unknown day."""
    year, number = income_budget.parse_month(month)
    first = date(year, number, 1)
    return tuple(_forecast_days(connection, account, first, _month_end(first)))


def month_closed(day, today):
    """A month is closed once today reaches the first day of the next month (plan D11)."""
    return (today.year, today.month) > (day.year, day.month)


def effective_budget(forecast, actual, day, today):
    """Return (budget, basis): the actual budget of a closed month when known (plan 3.3), else the forecast.

    forecast and actual may be single values or per-day tuples of the month of day.
    """
    if actual is not None and month_closed(day, today):
        return actual, "actual"
    return forecast, "forecast"


def budget_for_day(connection, account, day, today):
    """Return the effective budget of day (plan 3.3), or None when it is unknown."""
    _valid_day(day)
    _valid_day(today)
    if month_closed(day, today):
        daily = income_budget.actual_daily_budgets(connection, account, income_budget.month_key(day.year, day.month))
        if daily is not None:
            return daily[day.day - 1]
    return forecast_for_day(connection, account, day)


def _month_end(day):
    """Return the first day of the month after day; date.max stops the year 9999 overflow."""
    if day.month == 12:
        return date(day.year + 1, 1, 1) if day.year < 9999 else date.max
    return date(day.year, day.month + 1, 1)


def budget_for_period(connection, account, start, end, today):
    """Return (effective budget total, unknown day count) for days in [start, end)."""
    _valid_day(start)
    _valid_day(end)
    _valid_day(today)
    if start >= end:
        return forecast_for_period(connection, account, start, end)
    total = 0
    unknown_days = 0
    segment = start
    while segment < end:
        stop = min(_month_end(segment), end)
        actual = None
        if month_closed(segment, today):
            actual = income_budget.actual_daily_budgets(
                connection, account, income_budget.month_key(segment.year, segment.month))
        if actual is not None:
            # A segment never crosses a month, so its days are one slice of the month.
            total += sum(actual[segment.day - 1:segment.day - 1 + (stop - segment).days])
        else:
            part, unknown = forecast_for_period(connection, account, segment, stop)
            total += part
            unknown_days += unknown
        segment = stop
    return total, unknown_days
