"""Shared values and pure helpers; no project imports or environment reads."""
from datetime import timedelta, timezone
import re

VIETNAM_TZ = timezone(timedelta(hours=7))
STALE_AFTER_SECONDS = 3600
SQLITE_INT64_MAX = (1 << 63) - 1
# Rejects control and bidi characters in labels, keywords and notes.
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ud800-\udfff]")


def format_vnd(amount):
    return f"{abs(amount):,}".replace(",", ".") + "đ"


def parse_budget_amount(value):
    amount = int(value)
    if amount < 0 or amount > SQLITE_INT64_MAX:
        raise ValueError("budget must fit a non-negative SQLite integer")
    return amount


def table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,),
    ).fetchone() is not None


def table_columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def apply_notification_options(payload, environ):
    for name, key in (("ICON_URL", "icon"), ("BARK_SOUND", "sound")):
        value = environ.get(name, "").strip()
        if value:
            payload[key] = value
    return payload
