"""Loopback-only calendar dashboard with optional audited label edits."""

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

from budget import income_budget
from budget.budget_history import effective_budget, forecast_for_day, forecast_for_month, month_closed
from common import CONTROL_CHARS, VIETNAM_TZ, table_exists
from monitoring import sync_status
import categories
import ledger_edits


ROOT = Path(__file__).resolve().parent
_PAGE_SIZE = 50
_QUERY_SECONDS = 3
_WRITE_KEY_MIN = 16
_WRITE_BODY_MAX = 4096
_WRITE_READ_SECONDS = 5
_WRITE_FAILURE_LIMIT = 5
_WRITE_FAILURE_SECONDS = 60
_WRITE_LOCK = threading.Lock()
_WRITE_FAILURES = []
# Write route -> (body field next to transaction_id, ledger_edits function name).
_WRITE_EDITS = {"/api/labels": ("label", "set_label"), "/api/income-shift": ("next_month", "set_income_shift")}
_WRITE_ROUTES = tuple(_WRITE_EDITS)
_CHECK_SECONDS = 3
_INTEGRITY_CACHE_SECONDS = 600
_CACHE_ENTRIES = 16
_INTEGRITY_LOCK = threading.Lock()
_INTEGRITY = {}      # resolved database path -> (state, checked_at_monotonic, verdict)
_BACKUP_CACHE = {}   # backup file path -> (state, checked_at_monotonic, result_dict)
_ASSETS = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js", "/helpers.js": "helpers.js"}
_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}


def _month(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise ValueError("invalid month")
    year, month = map(int, value.split("-"))
    if not 1 <= year <= 9998:
        raise ValueError("invalid year")
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("invalid date")
    result = date.fromisoformat(value)
    if result.year > 9998:
        raise ValueError("invalid year")
    return result


def _path_state(path):
    """stat identity of an existing regular file; None when absent or not regular."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _quick_check(database):
    """Return True (ok), False (failed) or None (timed out, so unverified)."""
    deadline = time.monotonic() + _CHECK_SECONDS
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
        connection.execute("PRAGMA query_only = ON")
        try:
            return connection.execute("PRAGMA quick_check(1)").fetchone() == ("ok",)
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                return None
            raise
    finally:
        connection.close()


def _require_integrity(database, state):
    """Run quick_check once per observed database state; a timeout is not corruption."""
    key = str(database.resolve())
    with _INTEGRITY_LOCK:
        cached = _INTEGRITY.get(key)
        if cached is None or cached[0] != state or time.monotonic() - cached[1] >= _INTEGRITY_CACHE_SECONDS:
            verdict = _quick_check(database)
            if len(_INTEGRITY) >= _CACHE_ENTRIES:
                _INTEGRITY.clear()
            _INTEGRITY[key] = cached = (state, time.monotonic(), verdict)
    if cached[2] is False:
        raise sqlite3.DatabaseError("database integrity check failed")


@contextmanager
def _reader(database):
    if not database.exists():
        if database.is_symlink() or any(os.path.lexists(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal")):
            raise sqlite3.DatabaseError("missing database with sidecars")
        yield None
        return
    state = _path_state(database)
    if state is None:
        raise sqlite3.DatabaseError("database is not a regular file")
    with database.open("rb") as source:
        header = source.read(28)
    if header[:16] != b"SQLite format 3\x00":
        raise sqlite3.DatabaseError("invalid database header")
    # Bytes 24-27 are SQLite's file change counter; the -wal sidecar covers WAL mode.
    _require_integrity(database, (state, header[24:28], _path_state(str(database) + "-wal")))
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    deadline = time.monotonic() + _QUERY_SECONDS
    try:
        connection.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def _write_locked_out():
    now = time.monotonic()
    with _WRITE_LOCK:
        _WRITE_FAILURES[:] = [at for at in _WRITE_FAILURES if now - at < _WRITE_FAILURE_SECONDS]
        return len(_WRITE_FAILURES) >= _WRITE_FAILURE_LIMIT


def _write_failed():
    with _WRITE_LOCK:
        _WRITE_FAILURES.append(time.monotonic())


@contextmanager
def _writer(database):
    state = _path_state(database)
    if state is None:
        raise sqlite3.DatabaseError("database is not a regular file")
    with database.open("rb") as source:
        if source.read(16) != b"SQLite format 3\x00":
            raise sqlite3.DatabaseError("invalid database header")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _ledger_available(connection, account):
    if connection is None:
        return False
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if ("accounts" in tables) != ("transactions" in tables):
        raise sqlite3.DatabaseError("incomplete financial schema")
    if "accounts" not in tables:
        if "sync_status" not in tables:
            raise sqlite3.DatabaseError("unrecognized database schema")
        if connection.execute("SELECT 1 FROM sync_status WHERE last_success_at IS NOT NULL LIMIT 1").fetchone():
            raise sqlite3.DatabaseError("financial schema missing after successful sync")
        return False
    row = connection.execute("SELECT initialized FROM accounts WHERE account = ?", (account,)).fetchone()
    if row is not None and (type(row[0]) is not int or row[0] not in (0, 1)):
        raise sqlite3.DatabaseError("invalid account state")
    return row is not None and row[0] == 1


_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?")


def _clock_sql(time_column):
    """Return SQL that reads a valid clock as HH:MM:SS. Other values become NULL."""
    return (
        f"CASE WHEN typeof({time_column}) = 'text' AND instr({time_column}, char(0)) = 0 "
        f"AND substr({time_column}, 1, 2) <= '23' "
        f"AND ({time_column} GLOB '[0-2][0-9]:[0-5][0-9]' OR {time_column} GLOB '[0-2][0-9]:[0-5][0-9]:[0-5][0-9]') "
        f"THEN substr({time_column} || ':00', 1, 8) END"
    )


def _clean_note(note):
    """Return the note when it has 1-500 characters and no control characters, else None."""
    if isinstance(note, str) and 0 < len(note) <= 500 and not CONTROL_CHARS.search(note):
        return note
    return None


def _clean_time(value):
    """Return the clock text (HH:MM or HH:MM:SS) when valid, else None."""
    return value if isinstance(value, str) and _CLOCK.fullmatch(value) else None


def _amount(value):
    if type(value) is not int or value < 0:
        raise sqlite3.DatabaseError("invalid stored amount")
    return value


def _checked(value):
    return None if value is None else _amount(value)


def _day_budget(connection, account, day, today):
    """Return (budget, basis) of one day."""
    if connection is None:
        return None, "forecast"
    forecast = _checked(forecast_for_day(connection, account, day))
    actual = None
    # Only a closed month can use the actual budget, so skip the income reads otherwise.
    if month_closed(day, today):
        daily = income_budget.actual_daily_budgets(connection, account, income_budget.month_key(day.year, day.month))
        actual = None if daily is None else daily[day.day - 1]
    return effective_budget(forecast, actual, day, today)


def _incoming_total(connection, account, start, end):
    return sum(amount for _day, amount, _shifted in income_budget.incoming_rows(connection, account, start, end))


def _clock_key(value):
    """Sort key for a clean clock: HH:MM reads as HH:MM:00, and a missing clock sorts last."""
    if value is None:
        return ""
    return value if len(value) == 8 else value + ":00"


def _income_detail(connection, account, day, start_month):
    """Return the day's income total, count and newest records. Each record keeps its shift flag."""
    join, shifted = income_budget.shift_join(connection)
    rows = connection.execute(
        f"SELECT i.transaction_id, i.amount, i.transaction_time, i.note, {shifted} "
        f"FROM incoming_transactions AS i {join}WHERE i.account = ? AND i.day = ?",
        (account, day.isoformat()),
    ).fetchall()
    total, records = 0, []
    for identifier, amount, transaction_time, note, next_month in rows:
        # Reject lone surrogates: they come from invalid UTF-8 bytes in a stored identifier.
        if not isinstance(identifier, str) or not identifier or any(0xD800 <= ord(char) <= 0xDFFF for char in identifier):
            raise sqlite3.DatabaseError("invalid income identifier")
        amount = _amount(amount)
        total += amount
        records.append({"id": identifier, "amount": str(amount), "time": _clean_time(transaction_time),
                        "note": _clean_note(note),
                        "budget_month": income_budget.budget_month(day, bool(next_month), start_month),
                        "next_month": bool(next_month)})
    # Stable sorts: id ascending first, then clock descending with missing clocks last.
    records.sort(key=lambda record: record["id"])
    records.sort(key=lambda record: _clock_key(record["time"]), reverse=True)
    return {"total": str(total), "count": len(rows), "transactions": records[:_PAGE_SIZE]}


def calendar_snapshot(database, account, month, *, now=None):
    now = now or datetime.now(VIETNAM_TZ)
    today = now.astimezone(VIETNAM_TZ).date()
    start, end = _month(month)
    totals, counts, incoming = {}, {}, {}
    with _reader(database) as connection:
        available = _ledger_available(connection, account)
        if available:
            for day, amount in connection.execute(
                "SELECT day, amount FROM transactions WHERE account = ? AND day >= ? AND day < ?",
                (account, start.isoformat(), end.isoformat()),
            ):
                try:
                    _day(day)
                except ValueError as error:
                    raise sqlite3.DatabaseError("invalid stored transaction date") from error
                totals[day] = totals.get(day, 0) + _amount(amount)
                counts[day] = counts.get(day, 0) + 1
        state = income_budget.read_state(connection, account) if connection is not None else None
        # Only a closed month is priced from its IN (plan 3.3), so only then read the month before too.
        priced = month_closed(start, today) and state is not None and month >= state["start_month"]
        rows = []
        if connection is not None and (available or priced):
            window = income_budget.income_window(month) if priced else (start.isoformat(), end.isoformat())
            rows = income_budget.incoming_rows(connection, account, *window)
        if available:
            for day, amount, _shifted in rows:
                if day[:7] == month:
                    incoming[day] = incoming.get(day, 0) + amount
        daily = None
        if priced:
            daily = income_budget.split_evenly(income_budget.figures_from_rows(state, month, rows)["pool"], (end - start).days)
        forecasts = (None,) * (end - start).days if connection is None else forecast_for_month(connection, account, month)
        effective, basis = effective_budget(forecasts, daily, start, today)
        days, elapsed = [], 0
        current = start
        while current < end:
            budget = _checked(effective[current.day - 1])
            if current <= today:
                elapsed += 1
            key = current.isoformat()
            days.append({"date": key, "spent": str(totals.get(key, 0)) if available else None,
                         "budget": str(budget) if budget is not None else None,
                         "transactions": counts.get(key, 0) if available else None,
                         "income": str(incoming.get(key, 0)) if available else None})
            current += timedelta(days=1)
    spent = sum(totals.values())
    elapsed_records = [record for record in days if record["date"] <= today.isoformat()]
    highest = max(elapsed_records, key=lambda record: int(record["spent"] or 0), default=None)
    return {"month": month, "today": today.isoformat(),
            "generated_at": now.isoformat(), "database_status": "ok" if connection is not None else "absent",
            "ledger_status": "available" if available else "absent", "days": days,
            "summary": {"spent": str(spent) if available else None, "elapsed_days": elapsed,
                        "transactions": sum(counts.values()) if available else None,
                        "highest_day": {"date": highest["date"], "spent": highest["spent"]}
                        if available and highest is not None and int(highest["spent"]) > 0 else None,
                        "income": str(sum(incoming.values())) if available else None,
                        "budget_start_month": state["start_month"] if state is not None else None,
                        "budget_coverage_from": state["coverage_from"] if state is not None else None,
                        "budget_basis": basis}}


def categories_snapshot(database, account, month, *, rules=()):
    start, end = _month(month)
    counts, totals, variants = {}, {}, {}
    with _reader(database) as connection:
        if not _ledger_available(connection, account):
            return {"month": month, "ledger_status": "absent", "spent": None, "categories": []}
        labels = ledger_edits.read_labels(connection, account)
        columns = {row[1].lower() for row in connection.execute("PRAGMA table_info(transactions)")}
        note_column = "note" if "note" in columns else "NULL"
        # Decode notes as day_snapshot does, so both views classify the same rows.
        connection.text_factory = lambda value: value.decode("utf-8", "surrogateescape")
        for identifier, day, amount, note in connection.execute(
            f"SELECT transaction_id, day, amount, {note_column} FROM transactions WHERE account = ? AND day >= ? AND day < ?",
            (account, start.isoformat(), end.isoformat()),
        ):
            try:
                _day(day)
            except ValueError as error:
                raise sqlite3.DatabaseError("invalid stored transaction date") from error
            name, _source = categories.classify(labels.get(str(identifier)), _clean_note(note), rules)
            key, value = categories.fold(name), _amount(amount)
            counts[key] = counts.get(key, 0) + 1
            totals[key] = totals.get(key, 0) + value
            names = variants.setdefault(key, {})
            names[name] = names.get(name, 0) + value
    # Labels that differ only by case or accents share one row, named by the variant with the most spending.
    shown = {key: min(names, key=lambda name: (-names[name], name)) for key, names in variants.items()}
    ordered = sorted(totals, key=lambda key: (-totals[key], shown[key]))
    return {"month": month, "ledger_status": "available", "spent": str(sum(totals.values())),
            "categories": [{"category": shown[key], "count": counts[key], "spent": str(totals[key])} for key in ordered]}


def labels_snapshot(database, account):
    with _reader(database) as connection:
        if not _ledger_available(connection, account):
            return {"labels": []}
        labels = ledger_edits.label_counts(connection, account)
    return {"labels": [{"label": label, "count": count} for label, count in labels]}


def _day_totals(connection, account, day):
    spent, total = 0, 0
    # SQLite SUM can overflow even though every stored amount fits int64.
    for (amount,) in connection.execute("SELECT amount FROM transactions WHERE account = ? AND day = ?", (account, day)):
        spent += _amount(amount)
        total += 1
    return spent, total


def today_snapshot(database, account, *, now=None):
    now = now or datetime.now(VIETNAM_TZ)
    day = now.astimezone(VIETNAM_TZ).date()
    with _reader(database) as connection:
        available = _ledger_available(connection, account)
        budget, basis = _day_budget(connection, account, day, day)
        spent, total = _day_totals(connection, account, day.isoformat()) if available else (0, 0)
        income = (_incoming_total(connection, account, day.isoformat(), (day + timedelta(days=1)).isoformat())
                  if available else None)
    return {"date": day.isoformat(), "generated_at": now.isoformat(),
            "ledger_status": "available" if available else "absent",
            "spent": str(spent) if available else None,
            "budget": str(budget) if budget is not None else None,
            "remaining": str(budget - spent) if available and budget is not None else None,
            "transactions": total if available else None,
            "income": str(income) if available else None,
            "budget_basis": basis}


def day_snapshot(database, account, day, offset=0, *, sort="amount", rules=(), now=None):
    selected = _day(day)
    today = (now or datetime.now(VIETNAM_TZ)).astimezone(VIETNAM_TZ).date()
    if type(offset) is not int or offset < 0 or offset > 1000000:
        raise ValueError("invalid offset")
    if sort not in ("latest", "amount"):
        raise ValueError("invalid sort")
    timed_transactions = None
    income = None
    with _reader(database) as connection:
        available = _ledger_available(connection, account)
        budget, basis = _day_budget(connection, account, selected, today)
        total, spent, records = 0, 0, []
        if available:
            spent, total = _day_totals(connection, account, day)
            labels = ledger_edits.read_labels(connection, account)
            columns = {row[1].lower() for row in connection.execute("PRAGMA table_info(transactions)")}
            time_column = "transaction_time" if "transaction_time" in columns else "NULL"
            note_column = "note" if "note" in columns else "NULL"
            # Normalize valid clocks; unknown clocks stay NULL for counting and sorting.
            clock = _clock_sql(time_column)
            timed_transactions = connection.execute(
                f"SELECT COUNT({clock}) FROM transactions WHERE account = ? AND day = ?", (account, day),
            ).fetchone()[0] if "transaction_time" in columns else 0
            order = f"{clock} IS NULL, {clock} DESC, transaction_id ASC" if sort == "latest" and timed_transactions else "amount DESC, transaction_id ASC"
            connection.text_factory = lambda value: value.decode("utf-8", "surrogateescape")
            rows = connection.execute(
                f"SELECT transaction_id, amount, {time_column}, {note_column} FROM transactions WHERE account = ? AND day = ? "
                f"ORDER BY {order} LIMIT ? OFFSET ?", (account, day, _PAGE_SIZE, offset),
            )
            for identifier, amount, transaction_time, note in rows:
                if isinstance(identifier, str) and re.search(r"[\ud800-\udfff]", identifier):
                    raise sqlite3.DatabaseError("invalid transaction identifier")
                shown = _clean_note(note)
                label = labels.get(str(identifier))
                category, source = categories.classify(label, shown, rules)
                records.append({"id": str(identifier), "amount": str(_amount(amount)),
                                "time": _clean_time(transaction_time), "note": shown, "label": label,
                                "category": category, "category_source": source})
            if table_exists(connection, "incoming_transactions"):
                state = income_budget.read_state(connection, account)
                income = _income_detail(connection, account, selected, None if state is None else state["start_month"])
    return {"date": day, "ledger_status": "available" if available else "absent",
            "spent": str(spent) if available else None, "budget": str(budget) if budget is not None else None,
            "transactions": records, "total": total, "sort": sort, "timed_transactions": timed_transactions,
            "next_offset": offset + len(records) if offset + len(records) < total else None,
            "income": income, "budget_basis": basis}


def _search_fold(value):
    return "".join(char for char in unicodedata.normalize("NFD", value.casefold().replace("\u0111", "d"))
                   if not unicodedata.combining(char))


def transactions_snapshot(database, account, q="", start="", end="", min_amount="", max_amount="", offset=0, *, sort="latest", label="", category="", rules=(), now=None):
    if not isinstance(q, str):
        raise ValueError("invalid search")
    q = q.strip()
    if len(q) > 200 or CONTROL_CHARS.search(q):
        raise ValueError("invalid search")
    for value in (start, end):
        if value != "":
            _day(value)
    if start and end and start > end:
        raise ValueError("invalid date range")
    bounds = []
    for value in (min_amount, max_amount):
        if not isinstance(value, str) or (value != "" and not re.fullmatch(r"[0-9]+", value)):
            raise ValueError("invalid amount filter")
        canonical = value.lstrip("0") or "0"
        if len(canonical) > 19:
            raise ValueError("invalid amount filter")
        number = int(canonical) if value else None
        if number is not None and number > 9223372036854775807:
            raise ValueError("invalid amount filter")
        bounds.append(number)
    if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]:
        raise ValueError("invalid amount range")
    if type(offset) is not int or not 0 <= offset <= 1000000:
        raise ValueError("invalid offset")
    if sort not in ("latest", "amount"):
        raise ValueError("invalid sort")
    if not isinstance(label, str) or len(label) > 40 or CONTROL_CHARS.search(label):
        raise ValueError("invalid label")
    if not isinstance(category, str) or len(category) > 40 or CONTROL_CHARS.search(category):
        raise ValueError("invalid category")
    filters = {"q": q, "start": start, "end": end,
               "min": str(bounds[0]) if bounds[0] is not None else "",
               "max": str(bounds[1]) if bounds[1] is not None else "",
               "label": label, "category": category}
    now = now or datetime.now(VIETNAM_TZ)
    records, spent = [], 0
    needle = _search_fold(q)
    with _reader(database) as connection:
        # ponytail: scan/hash stays within 3 seconds; add an indexed search and ledger revision for larger ledgers.
        deadline = time.monotonic() + _QUERY_SECONDS
        available = _ledger_available(connection, account)
        if available:
            labels = ledger_edits.read_labels(connection, account)
            collapsed = " ".join(label.split())
            wanted = _search_fold(collapsed) if collapsed else None
            wanted_category = categories.fold(category) or None
            columns = {row[1].lower() for row in connection.execute("PRAGMA table_info(transactions)")}
            time_column = "transaction_time" if "transaction_time" in columns else "NULL"
            note_column = "note" if "note" in columns else "NULL"
            clock = _clock_sql(time_column)
            order = f"day DESC, {clock} IS NULL, {clock} DESC, transaction_id ASC" if sort == "latest" else "amount DESC, day DESC, transaction_id ASC"
            conditions, parameters = [], [account]
            for column, operator, value in (("day", ">=", start or None), ("day", "<=", end or None),
                                            ("amount", ">=", bounds[0]), ("amount", "<=", bounds[1])):
                if value is not None:
                    conditions.append(f"{column} {operator} ?")
                    parameters.append(value)
            where = " AND ".join(conditions) or "1"
            # Keep corrupt candidates visible to validation, rather than treating them as no matches.
            invalid = ("typeof(transaction_id) != 'text' OR transaction_id = '' "
                       "OR typeof(amount) != 'integer' OR amount < 0 OR typeof(day) != 'text' "
                       "OR day NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
                       "OR day < '0001-01-01' OR day > '9998-12-31' "
                       "OR date(day, '+0 days') IS NULL OR date(day, '+0 days') != day")
            connection.text_factory = lambda value: value.decode("utf-8", "surrogateescape")
            rows = connection.execute(
                f"SELECT transaction_id, day, amount, {time_column}, {note_column} FROM transactions "
                f"WHERE account = ? AND (({where}) OR ({invalid})) ORDER BY {order}", parameters,
            )
            for identifier, day, amount, transaction_time, note in rows:
                if time.monotonic() >= deadline:
                    raise sqlite3.OperationalError("search exceeded read deadline")
                try:
                    _day(day)
                except ValueError as error:
                    raise sqlite3.DatabaseError("invalid stored transaction date") from error
                amount = _amount(amount)
                if not isinstance(identifier, str) or not identifier or re.search(r"[\ud800-\udfff]", identifier):
                    raise sqlite3.DatabaseError("invalid transaction identifier")
                note = _clean_note(note)
                if q and (not needle or note is None or needle not in _search_fold(note)):
                    continue
                row_label = labels.get(identifier)
                if wanted is not None and (row_label is None or _search_fold(row_label) != wanted):
                    continue
                row_category, category_source = categories.classify(row_label, note, rules)
                if wanted_category is not None and categories.fold(row_category) != wanted_category:
                    continue
                records.append({"id": identifier, "date": day, "amount": str(amount),
                                "time": _clean_time(transaction_time), "note": note, "label": row_label,
                                "category": row_category, "category_source": category_source})
                spent += amount
    revision = hashlib.sha256()
    for record in records:
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError("search exceeded read deadline")
        revision.update(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    total = len(records)
    page = records[offset:offset + _PAGE_SIZE]
    return {"generated_at": now.isoformat(), "ledger_status": "available" if available else "absent",
            "filters": filters, "sort": sort, "spent": str(spent) if available else None,
            "total": total, "transactions": page, "next_offset": offset + len(page) if offset + len(page) < total else None,
            "revision": revision.hexdigest()}


def _backup_snapshot(backups):
    """Reuse a verified/invalid verdict while the newest backup file is unchanged."""
    key = state = None
    try:
        newest = None if backups is None else sync_status.newest_backup(backups)
        if newest is not None and not any(
                os.path.lexists(str(newest[2]) + suffix) for suffix in ("-wal", "-shm", "-journal")):
            state = _path_state(newest[2])
            if state is not None:
                key = str(newest[2])
    except (OSError, TypeError, ValueError):
        key = None
    started = time.monotonic()
    if key is not None:
        with _INTEGRITY_LOCK:
            cached = _BACKUP_CACHE.get(key)
            if cached is not None and cached[0] == state and started - cached[1] < _INTEGRITY_CACHE_SECONDS:
                return dict(cached[2])
    result = sync_status.read_backup_snapshot(backups)
    if (key is not None and result.get("status") in ("verified", "invalid")
            and result.get("filename") == os.path.basename(key)):
        with _INTEGRITY_LOCK:
            if len(_BACKUP_CACHE) >= _CACHE_ENTRIES:
                _BACKUP_CACHE.clear()
            _BACKUP_CACHE[key] = (state, started, dict(result))
    return result


def status_snapshot(database, account, backups, *, now=None):
    now = now or datetime.now(VIETNAM_TZ)
    backup = _backup_snapshot(backups)
    with _reader(database) as connection:
        if connection is not None:
            _ledger_available(connection, account)
        return sync_status.snapshot_from_connection(connection, account, now, backup)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BudgetDashboard"
    sys_version = ""

    def log_message(self, _format, *args):
        pass

    def _send(self, status, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            self.close_connection = True

    def _trusted(self):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        navigation = self.headers.get("Sec-Fetch-Mode") == "navigate" and self.path in _ASSETS
        return (host in hosts and (origin is None or origin == "http://" + host)
                and (navigation or self.headers.get("Sec-Fetch-Site", "none") in {"none", "same-origin"}))

    def do_GET(self):
        if not self._trusted():
            self._send(403, {"error": "Truy c\u1eadp ch\u1ec9 \u0111\u01b0\u1ee3c ph\xe9p t\u1eeb dashboard c\u1ee5c b\u1ed9."})
            return
        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=9 if url.path == "/api/transactions" else 3)
            if any(len(value) != 1 for value in query.values()):
                raise ValueError("duplicate parameter")
            if url.path in _ASSETS:
                if query:
                    raise ValueError("unexpected parameter")
                path = self.server.assets / _ASSETS[url.path]
                if path.is_symlink() or not path.is_file():
                    self._send(404, {"error": "Kh\xf4ng t\xecm th\u1ea5y giao di\u1ec7n."})
                    return
                self._send(200, path.read_bytes(), _TYPES[path.suffix])
                return
            if not url.path.startswith("/api/"):
                self._send(404, {"error": "Kh\xf4ng t\xecm th\u1ea5y \u0111\u01b0\u1eddng d\u1eabn."})
                return
            if self.headers.get("X-Budget-Dashboard") != "1":
                self._send(403, {"error": "Y\xeau c\u1ea7u dashboard kh\xf4ng h\u1ee3p l\u1ec7."})
                return
            now = datetime.now(VIETNAM_TZ)
            database, account = self.server.database, self.server.account
            if url.path == "/api/calendar" and not (query.keys() - {"month"}):
                month = query.get("month", [now.date().isoformat()[:7]])[0]
                result = calendar_snapshot(database, account, month, now=now)
            elif url.path == "/api/categories" and not (query.keys() - {"month"}):
                month = query.get("month", [now.date().isoformat()[:7]])[0]
                result = categories_snapshot(database, account, month, rules=categories.current_rules(self.server.categories_file))
            elif url.path == "/api/labels" and not query:
                result = labels_snapshot(database, account)
            elif url.path == "/api/day" and not (query.keys() - {"date", "offset", "sort"}):
                selected = query.get("date", [""])[0]
                raw_offset = query.get("offset", ["0"])[0]
                if not re.fullmatch(r"[0-9]{1,7}", raw_offset):
                    raise ValueError("invalid offset")
                result = day_snapshot(database, account, selected, int(raw_offset), sort=query.get("sort", ["amount"])[0],
                                      rules=categories.current_rules(self.server.categories_file), now=now)
            elif url.path == "/api/transactions" and not (query.keys() - {"q", "start", "end", "min", "max", "sort", "label", "category", "offset"}):
                raw_offset = query.get("offset", ["0"])[0]
                if not re.fullmatch(r"[0-9]{1,7}", raw_offset):
                    raise ValueError("invalid offset")
                result = transactions_snapshot(database, account, query.get("q", [""])[0],
                                               query.get("start", [""])[0], query.get("end", [""])[0],
                                               query.get("min", [""])[0], query.get("max", [""])[0], int(raw_offset),
                                               sort=query.get("sort", ["latest"])[0],
                                               label=query.get("label", [""])[0], category=query.get("category", [""])[0],
                                               rules=categories.current_rules(self.server.categories_file), now=now)
            elif url.path == "/api/session" and not query:
                result = {"write_enabled": self.server.write_key is not None}
            elif url.path == "/api/today" and not query:
                result = today_snapshot(database, account, now=now)
            elif url.path == "/api/status" and not query:
                result = status_snapshot(database, account, self.server.backups, now=now)
            else:
                raise ValueError("invalid route or parameters")
            self._send(200, result)
        except ValueError:
            self._send(400, {"error": "Th\xe1ng, ng\xe0y ho\u1eb7c tham s\u1ed1 kh\xf4ng h\u1ee3p l\u1ec7."})
        except (sqlite3.Error, OSError, TypeError):
            self._send(503, {"error": "Kh\xf4ng \u0111\u1ecdc \u0111\u01b0\u1ee3c d\u1eef li\u1ec7u. Th\u1eed l\u1ea1i sau; dashboard kh\xf4ng s\u1eeda DB."})

    def _method_not_allowed(self):
        self._send(405, {"error": "Dashboard ch\u1ec9 \u0111\u1ecdc d\u1eef li\u1ec7u."})

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path in _WRITE_ROUTES and not url.query:
            self._write(url.path)
        else:
            self._method_not_allowed()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    def _reject(self, status, payload):
        # Read the body first. Unread bytes make Windows reset the connection and drop the reply.
        length = self.headers.get("Content-Length", "")
        if re.fullmatch(r"[0-9]{1,7}", length):
            try:
                self.rfile.read(min(int(length), _WRITE_BODY_MAX))
            except OSError:
                pass
        self._send(status, payload)

    def _write(self, path):
        self.close_connection = True
        self.connection.settimeout(_WRITE_READ_SECONDS)
        server = self.server
        if not self._trusted() or self.headers.get("X-Budget-Dashboard") != "1":
            self._reject(403, {"error": "Y\xeau c\u1ea7u dashboard kh\xf4ng h\u1ee3p l\u1ec7."})
            return
        if server.write_key is None:
            self._reject(403, {"error": "Ch\u1ec9nh s\u1eeda ch\u01b0a \u0111\u01b0\u1ee3c b\u1eadt. \u0110\u1eb7t DASHBOARD_WRITE_KEY trong .env."})
            return
        if _write_locked_out():
            self._reject(429, {"error": "Th\u1eed sai qu\xe1 nhi\u1ec1u l\u1ea7n. Ch\u1edd 60 gi\xe2y."})
            return
        supplied = self.headers.get("X-Budget-Write-Key", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), server.write_key.encode("utf-8")):
            _write_failed()
            self._reject(403, {"error": "M\xe3 ch\u1ec9nh s\u1eeda kh\xf4ng \u0111\xfang."})
            return
        invalid = {"error": "D\u1eef li\u1ec7u kh\xf4ng h\u1ee3p l\u1ec7."}
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._reject(415, invalid)
            return
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,7}", length) or int(length) == 0:
            self._reject(411, invalid)
            return
        if int(length) > _WRITE_BODY_MAX:
            self._reject(413, invalid)
            return
        try:
            body = json.loads(self.rfile.read(int(length)).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            result = self._edit(path, body)
            self._send(200, {"ok": True, **result})
        except ValueError:
            self._send(400, invalid)
        except LookupError:
            self._send(404, {"error": "Kh\xf4ng t\xecm th\u1ea5y."})
        except (sqlite3.Error, OSError):
            self._send(503, {"error": "Kh\xf4ng ghi \u0111\u01b0\u1ee3c. Th\u1eed l\u1ea1i sau."})

    def _edit(self, path, body):
        field, name = _WRITE_EDITS[path]
        if set(body) != {"transaction_id", field}:
            raise ValueError("unexpected body keys")
        with _writer(self.server.database) as connection:
            if not _ledger_available(connection, self.server.account):
                raise LookupError("ledger is not available")
            # Look the function up per call so tests can patch ledger_edits.
            return getattr(ledger_edits, name)(
                connection, self.server.account, body["transaction_id"], body[field],
                datetime.now(VIETNAM_TZ).isoformat(),
            )


def make_server(root, account, port=8765, *, assets=None, write_key=None, categories_file=None):
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account is required")
    if write_key is not None and (not isinstance(write_key, str) or len(write_key) < _WRITE_KEY_MIN):
        raise ValueError("write key is too short")
    categories_file = Path(categories_file) if categories_file is not None else Path(root) / "categories.json"
    categories.read_rules(categories_file)
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    server.database = Path(root) / "budget_state.sqlite3"
    server.backups = Path(root) / "backups"
    server.account = account
    server.write_key = write_key
    server.categories_file = categories_file
    server.assets = Path(assets) if assets is not None else ROOT / "dashboard"
    return server


def _self_check():
    from tests import run_checks

    run_checks(
        sys.modules[__name__], "dashboard",
        "tests.test_dashboard_data.DashboardDataTests",
        "tests.test_dashboard_status_integration.DashboardStatusIntegrationTests",
        "tests.test_dashboard_structure.DashboardStructureTests",
        "tests.test_dashboard_search.DashboardSearchTests",
        "tests.test_dashboard_cache.DashboardCacheTests",
        "tests.test_dashboard_edit.DashboardEditTests",
        "tests.test_dashboard_categories.DashboardCategoryTests",
    )
    print("Dashboard self-check passed.")


def cli():
    parser = argparse.ArgumentParser(description="Local expense calendar dashboard with optional label edits.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return 0
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        token = os.getenv("BANK_KEY", "").strip()
        if not token:
            raise ValueError("BANK_KEY is required.")
        account = hashlib.sha256(token.encode("utf-8")).hexdigest()
        write_key = os.getenv("DASHBOARD_WRITE_KEY", "").strip() or None
        if write_key is not None and len(write_key) < _WRITE_KEY_MIN:
            print("DASHBOARD_WRITE_KEY qu\xe1 ng\u1eafn (c\u1ea7n \xedt nh\u1ea5t 16 k\xfd t\u1ef1); ch\u1ec9nh s\u1eeda b\u1ecb t\u1eaft.", file=sys.stderr)
            write_key = None
        with make_server(ROOT, account, args.port, write_key=write_key) as server:
            mode = "read-only" if write_key is None else "writes enabled"
            print(f"Dashboard: http://127.0.0.1:{server.server_address[1]} ({mode})", flush=True)
            server.serve_forever()
    except ImportError:
        print("Install dependencies: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except categories.CategoryConfigError as error:
        print(f"Cannot start dashboard. Fix categories.json: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Cannot start dashboard. Check BANK_KEY and local port.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
