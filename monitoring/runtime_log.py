from __future__ import annotations

import copy
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import stat
import sys
from urllib.parse import quote, quote_plus

_MAX_BYTES = 1024 * 1024
_MAX_MESSAGE_BYTES = 16 * 1024
_BACKUP_COUNT = 3
_LOGGER = logging.getLogger("budget.monitor")
_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False
_CONTROLS = str.maketrans(
    {chr(code): " " for code in (*range(32), *range(127, 160), 0x2028, 0x2029)}
)


def redact(message: str) -> str:
    values = set()
    for name in ("BANK_KEY", "DEVICE_KEY"):
        secret = os.getenv(name, "")
        for value in {secret, secret.strip()} - {""}:
            values.update((value, quote(value), quote(value, safe=""), quote_plus(value)))
    values = sorted(values, key=len, reverse=True)
    for value in values:
        message = message.replace(value, "[REDACTED]")
    message = message.translate(_CONTROLS)
    for value in values:
        message = message.replace(value, "[REDACTED]")
    return message


def _warning() -> None:
    try:
        sys.stderr.write("Warning: runtime log unavailable.\n")
        sys.stderr.flush()
    except Exception:
        pass


def check_paths(directory: Path) -> None:
    # Path checks assume trusted directories; use OS directory handles for hostile writers.
    for path in (directory, *directory.parents):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISDIR(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise OSError("Unsafe log directory.")
    for name in ("monitor.log", *(f"monitor.log.{index}" for index in range(1, _BACKUP_COUNT + 1))):
        try:
            info = (directory / name).lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > _MAX_BYTES
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise OSError("Unsafe log file.")


class _SafeRotatingFileHandler(RotatingFileHandler):
    def __init__(self, filename: Path) -> None:
        self._emit_failed = False
        self._warning_active = False
        super().__init__(filename, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8")

    def _open(self):
        check_paths(Path(self.baseFilename).parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("Unsafe log file.")
            return os.fdopen(descriptor, "a", encoding="utf-8", errors="replace", newline="\n")
        except Exception:
            os.close(descriptor)
            raise

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, os.SEEK_END)
        size = len((self.format(record) + self.terminator).encode("utf-8"))
        return self.stream.tell() + size >= self.maxBytes

    def doRollover(self) -> None:
        check_paths(Path(self.baseFilename).parent)
        super().doRollover()

    def emit(self, record: logging.LogRecord) -> None:
        self._emit_failed = False
        try:
            record = copy.copy(record)
            message = redact(record.getMessage())[:_MAX_MESSAGE_BYTES].encode("utf-8", errors="replace")
            record.msg = message[:_MAX_MESSAGE_BYTES].decode("utf-8", errors="ignore")
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
            super().emit(record)
        except Exception:
            self.handleError(record)
        if not self._emit_failed:
            self._warning_active = False

    def handleError(self, record: logging.LogRecord | None) -> None:
        self._emit_failed = True
        if not self._warning_active:
            self._warning_active = True
            _warning()


def configure_logging(log_directory: Path) -> None:
    try:
        directory = Path(log_directory).absolute()
        check_paths(directory)
        directory.mkdir(parents=True, exist_ok=True)
        handler = _SafeRotatingFileHandler(directory / "monitor.log")
        handler.setFormatter(logging.Formatter("%(message)s"))
    except Exception:
        raise RuntimeError("Cannot initialize runtime logging.") from None
    close_logging()
    _LOGGER.disabled = False
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False
    _LOGGER.addHandler(handler)


def write_log(message: str) -> None:
    try:
        _LOGGER.info(message)
    except Exception:
        if _LOGGER.handlers and isinstance(_LOGGER.handlers[0], _SafeRotatingFileHandler):
            _LOGGER.handlers[0].handleError(None)
        else:
            _warning()


def close_logging() -> None:
    for handler in _LOGGER.handlers[:]:
        _LOGGER.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            _warning()
