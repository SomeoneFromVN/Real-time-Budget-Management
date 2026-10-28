"""Spending groups from a keyword file. Matching is pure; the loader reloads when the file changes."""
import json
import os
import re
import sys
import threading
import unicodedata

from common import CONTROL_CHARS

UNCATEGORIZED = "Ch\u01b0a ph\xe2n lo\u1ea1i"
MAX_GROUPS = 50
MAX_KEYWORDS = 200
NAME_LIMIT = 40
KEYWORD_LIMIT = 60
FILE_LIMIT = 65536
CACHE_ENTRIES = 16

_LOCK = threading.Lock()
_CACHE = {}  # file path -> (stamp, rules)


class CategoryConfigError(ValueError):
    pass


def fold(value):
    """Lower-case, drop accents, collapse spaces."""
    text = unicodedata.normalize("NFD", value.casefold().replace("\u0111", "d"))
    return " ".join("".join(char for char in text if not unicodedata.combining(char)).split())


def _name(value):
    if not isinstance(value, str) or CONTROL_CHARS.search(value):
        raise CategoryConfigError("group name must be text without control characters")
    name = " ".join(unicodedata.normalize("NFC", value).split())
    if not 0 < len(name) <= NAME_LIMIT:
        raise CategoryConfigError(f"group name must have 1-{NAME_LIMIT} characters")
    return name


def _pattern(value):
    if not isinstance(value, str) or CONTROL_CHARS.search(value):
        raise CategoryConfigError("keyword must be text without control characters")
    keyword = fold(value)
    if not re.search(r"[0-9a-z]", keyword) or len(keyword) > KEYWORD_LIMIT:
        raise CategoryConfigError(f"keyword needs a letter or digit and at most {KEYWORD_LIMIT} characters")
    # Whole words only: "ca" must not match "cam".
    return re.compile(r"(?<![0-9a-z])" + re.escape(keyword) + r"(?![0-9a-z])")


def parse_rules(data):
    """Validate the file content. Return ((name, (pattern, ...)), ...) in file order."""
    if not isinstance(data, dict) or set(data) != {"groups"} or not isinstance(data["groups"], list):
        raise CategoryConfigError('file must be {"groups": [...]}')
    if len(data["groups"]) > MAX_GROUPS:
        raise CategoryConfigError(f"at most {MAX_GROUPS} groups")
    rules, seen = [], {fold(UNCATEGORIZED)}
    for group in data["groups"]:
        if not isinstance(group, dict) or set(group) != {"name", "keywords"}:
            raise CategoryConfigError('each group must be {"name": ..., "keywords": [...]}')
        name = _name(group["name"])
        if fold(name) in seen:
            raise CategoryConfigError(f"group name is reserved or repeated: {name}")
        seen.add(fold(name))
        if not isinstance(group["keywords"], list) or len(group["keywords"]) > MAX_KEYWORDS:
            raise CategoryConfigError(f"keywords must be a list of at most {MAX_KEYWORDS} items")
        rules.append((name, tuple(_pattern(keyword) for keyword in group["keywords"])))
    return tuple(rules)


def category_for(note, rules):
    """Return the first group with a keyword in the whole note, else None."""
    if not isinstance(note, str) or not note:
        return None
    text = fold(note)
    for name, patterns in rules:
        if any(pattern.search(text) for pattern in patterns):
            return name
    return None


def classify(label, note, rules):
    """Return (category, source). A label wins over a keyword group."""
    if label is not None:
        return label, "label"
    group = category_for(note, rules)
    return (group, "keyword") if group is not None else (UNCATEGORIZED, "none")


def read_rules(path):
    """Read the file strictly. A missing file means no rules."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(FILE_LIMIT + 1)
    except FileNotFoundError:
        return ()
    if len(raw) > FILE_LIMIT:
        raise CategoryConfigError(f"file is larger than {FILE_LIMIT} bytes")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError) as error:
        raise CategoryConfigError("file is not valid UTF-8 JSON") from error
    return parse_rules(data)


def current_rules(path):
    """Return the rules for path. Reload on change; keep the last good rules after a bad edit."""
    path = str(path)
    try:
        info = os.stat(path)
        stamp = (info.st_mtime_ns, info.st_size)
    except FileNotFoundError:
        stamp = None
    except OSError:
        stamp = False
    with _LOCK:
        cached = _CACHE.get(path)
        if stamp is False:
            return cached[1] if cached else ()
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            rules = read_rules(path)
        except (CategoryConfigError, OSError) as error:
            print(f"categories.json: {error}. Keeping the previous rules.", file=sys.stderr)
            rules = cached[1] if cached else ()
        if len(_CACHE) >= CACHE_ENTRIES and path not in _CACHE:
            _CACHE.clear()
        _CACHE[path] = (stamp, rules)
        return rules
