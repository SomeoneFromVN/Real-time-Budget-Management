"""OS-backed single-instance lock keyed by canonical database path."""

from contextlib import contextmanager
from pathlib import Path
import os

from .file_lock import try_lock, unlock


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when another process already holds this database's lock."""


@contextmanager
def single_instance(database_path):
    """Hold exclusive lock for database_path until context exits or process dies."""
    database = Path(database_path).expanduser().resolve()
    lock_path = database.with_name(database.name + ".lock")
    lock_file = open(lock_path, "a+b")
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            try_lock(lock_file.fileno())
        except OSError as exc:
            raise InstanceAlreadyRunningError(
                f"Another process is already using database: {database}"
            ) from exc

        try:
            yield
        finally:
            unlock(lock_file.fileno())
    finally:
        lock_file.close()
