"""Safe local SQLite snapshots. Backups remain unencrypted on disk."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

if __package__:
    from .file_lock import try_lock, unlock
else:  # Keep direct script invocation working.
    from file_lock import try_lock, unlock

_SQLITE_TIMEOUT = 1.0
_CHECK_TIMEOUT = 2.0
_BACKUP_DEADLINE = 8.0
_LOCK_DEADLINE = 4.0
_PUBLISH_RETRY_SECONDS = 1.0
_SNAPSHOT_NAME = re.compile(r"budget-(\d{4}-\d{2}-\d{2})\.sqlite3\Z")
_LEFTOVER_NAME = re.compile(r"\.budget-(\d{4}-\d{2}-\d{2})\.sqlite3\.(tmp|corrupt)-.+\.sqlite3\Z")
_LEFTOVER_RETENTION_DAYS = 7
_TEMP_MIN_AGE_SECONDS = 3600.0
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Same offset as common.VIETNAM_TZ. common is not importable when this file runs as a script.
_VIETNAM_TZ = dt.timezone(dt.timedelta(hours=7))


class _BackupDeadlineExceeded(TimeoutError):
    pass


def _readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _integrity_check(path: Path, *, deadline: float | None = None, immutable: bool = False) -> None:
    started = time.monotonic()
    deadline = min(deadline, started + _CHECK_TIMEOUT) if deadline is not None else started + _CHECK_TIMEOUT
    if started >= deadline:
        raise _BackupDeadlineExceeded("SQLite integrity check exceeded time limit")
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"snapshot is not a regular file: {path}")
    if not mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
        raise PermissionError(f"snapshot is not readable: {path}")
    if immutable:
        _reject_sidecars(path)
    try:
        with path.open("rb") as snapshot:
            if snapshot.read(16) != b"SQLite format 3\x00":
                raise RuntimeError(f"invalid SQLite header: {path}")
        uri = _readonly_uri(path) + ("&immutable=1" if immutable else "")
        with closing(sqlite3.connect(uri, uri=True, timeout=max(0, deadline - time.monotonic()))) as connection:
            connection.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            try:
                result = connection.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.OperationalError as exc:
                if time.monotonic() >= deadline:
                    raise _BackupDeadlineExceeded("SQLite integrity check exceeded time limit") from exc
                raise
        if time.monotonic() >= deadline:
            raise _BackupDeadlineExceeded("SQLite integrity check exceeded time limit")
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError(f"snapshot is not a regular file: {path}")
        if result != ("ok",):
            raise RuntimeError(f"integrity_check failed for {path}: {result!r}")
    finally:
        if immutable:
            _reject_sidecars(path)


def _is_regular_file(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode)


def _is_confirmed_corruption(exc: sqlite3.DatabaseError) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "file is not a database",
            "database disk image is malformed",
            "malformed database schema",
            "unsupported file format",
        )
    )


def _is_valid_snapshot(path: Path, *, deadline: float | None = None, immutable: bool = False) -> bool:
    if not _is_regular_file(path):
        return False
    try:
        _integrity_check(path, deadline=deadline, immutable=immutable)
    except RuntimeError:
        return False
    except sqlite3.DatabaseError as exc:
        if _is_confirmed_corruption(exc):
            return False
        raise
    return True


def snapshot_integrity(path: Path, *, deadline: float | None = None, immutable: bool = False) -> str:
    """Return verified/invalid/error without hiding backup and restore exceptions.

    Explicit deadlines are absolute; omitted deadlines allow two seconds.
    Immutable checks reject sidecars before/after and never create them.
    """
    try:
        _integrity_check(Path(path), deadline=deadline, immutable=immutable)
    except RuntimeError as exc:
        if str(exc).startswith(("invalid SQLite header:", "integrity_check failed for ", "snapshot is not a regular file:")):
            return "invalid"
        return "error"
    except sqlite3.DatabaseError as exc:
        return "invalid" if _is_confirmed_corruption(exc) else "error"
    except (OSError, ValueError):
        return "error"
    return "verified"


def _copy_verified(source: Path, temp: Path, deadline: float, label: str) -> None:
    """Copy and verify using caller's original absolute deadline."""
    with closing(sqlite3.connect(_readonly_uri(source), uri=True, timeout=_SQLITE_TIMEOUT)) as source_db:
        if label == "backup":
            source_db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        with closing(sqlite3.connect(temp, timeout=_SQLITE_TIMEOUT)) as destination_db:
            def progress(_status: int, _remaining: int, _total: int) -> None:
                if time.monotonic() >= deadline:
                    raise _BackupDeadlineExceeded(f"SQLite {label} exceeded time limit")

            timer = threading.Timer(max(0, deadline - time.monotonic()), destination_db.interrupt)
            timer.daemon = True
            timer.start()
            try:
                source_db.backup(destination_db, pages=128, progress=progress, sleep=0.05)
                destination_db.commit()
                destination_db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
                destination_db.execute("PRAGMA journal_mode = DELETE").fetchone()
                _integrity_check(temp, deadline=deadline)
            except sqlite3.OperationalError as exc:
                if time.monotonic() >= deadline:
                    raise _BackupDeadlineExceeded(f"SQLite {label} exceeded time limit") from exc
                raise
            finally:
                timer.cancel()
                timer.join()


def _acquire_lock(path: Path) -> int:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    deadline = time.monotonic() + _LOCK_DEADLINE
    try:
        while True:
            try:
                try_lock(descriptor)
                return descriptor
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for backup lock: {path}") from exc
                time.sleep(0.05)
    except BaseException:
        os.close(descriptor)
        raise


def _release_lock(descriptor: int) -> None:
    try:
        unlock(descriptor)
    finally:
        os.close(descriptor)


def _remove_temp(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-journal"), Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _new_quarantine_path(final: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{final.name}.corrupt-", suffix=".sqlite3", dir=final.parent
    )
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _reject_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if os.path.lexists(sidecar):
            raise FileExistsError(f"daily backup sidecar already exists: {sidecar}")


def _prune(directory: Path) -> None:
    snapshots: list[tuple[dt.date, Path]] = []
    for path in directory.iterdir():
        match = _SNAPSHOT_NAME.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            continue
        try:
            day = dt.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        try:
            _reject_sidecars(path)
        except FileExistsError:
            continue
        if _is_valid_snapshot(path, immutable=True):
            try:
                _reject_sidecars(path)
            except FileExistsError:
                continue
            snapshots.append((day, path))
    snapshots.sort(key=lambda item: item[0], reverse=True)
    for _, path in snapshots[7:]:
        try:
            _reject_sidecars(path)
            path.unlink()
        except OSError:
            pass


def _is_final_valid(directory: Path, day: dt.date) -> bool:
    try:
        return _is_valid_snapshot(directory / f"budget-{day.isoformat()}.sqlite3", immutable=True)
    except (OSError, sqlite3.Error):
        return False


def _cleanup_leftovers(directory: Path, today: dt.date) -> None:
    """Remove temp copies and quarantined files that are no longer needed.

    Call this while holding today's lock, after a successful publish.
    """
    try:
        paths = list(directory.iterdir())
    except OSError:
        return
    now = time.time()
    valid_days: dict[dt.date, bool] = {}
    for path in paths:
        match = _LEFTOVER_NAME.fullmatch(path.name)
        if match is None:
            continue
        try:
            day = dt.date.fromisoformat(match.group(1))
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            if (today - day).days > _LEFTOVER_RETENTION_DAYS:
                remove = True
            elif match.group(2) == "tmp" and (day == today or now - info.st_mtime > _TEMP_MIN_AGE_SECONDS):
                # Today's snapshot was just verified and published under our lock.
                if day not in valid_days:
                    valid_days[day] = day == today or _is_final_valid(directory, day)
                remove = valid_days[day]
            else:
                remove = False
            if remove:
                _remove_temp(path)
        except (OSError, ValueError):
            pass


def _publish(temporary: Path, final: Path, *, retry: bool) -> None:
    """Publish atomically; a reader can briefly hold today's snapshot open on Windows."""
    deadline = None
    while True:
        try:
            os.replace(temporary, final)
            return
        except PermissionError:
            if not retry:
                raise
            if deadline is None:
                deadline = time.monotonic() + _PUBLISH_RETRY_SECONDS
            elif time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def snapshot_is_fresh(directory: Path, today: dt.date, max_age_seconds: float) -> bool:
    """Return True when today's snapshot is a regular file that is not older than max_age_seconds.

    This reads file metadata only; it does not check the database content.
    """
    path = Path(directory) / f"budget-{today.isoformat()}.sqlite3"
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    # A future mtime is treated as stale so that a wrong clock cannot block backups.
    return 0 <= time.time() - info.st_mtime <= max_age_seconds


def backup_database(source: Path, directory: Path, today: dt.date) -> Path:
    """Create or refresh today's verified snapshot using SQLite's online backup API."""
    source = Path(source)
    directory = Path(directory)
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"budget-{today.isoformat()}.sqlite3"
    lock = directory / (final.name + ".lock")
    lock_fd = _acquire_lock(lock)
    try:
        _reject_sidecars(final)
        refresh = False
        if os.path.lexists(final):
            if not _is_regular_file(final):
                raise RuntimeError(f"existing daily backup is not a regular file: {final}")
            # immutable snapshots require no external writers; keep backups in a private directory.
            refresh = _is_valid_snapshot(final, immutable=True)
            if refresh:
                _reject_sidecars(final)

        descriptor, temp_name = tempfile.mkstemp(prefix=f".{final.name}.tmp-", suffix=".sqlite3", dir=directory)
        os.close(descriptor)
        temporary = Path(temp_name)
        deadline = time.monotonic() + _BACKUP_DEADLINE
        keep_temporary = False
        try:
            _copy_verified(source, temporary, deadline, "backup")
            try:
                _reject_sidecars(final)
                if not refresh and os.path.lexists(final):
                    if not _is_regular_file(final):
                        raise RuntimeError(f"existing daily backup is not a regular file: {final}")
                    if _is_valid_snapshot(final, immutable=True):
                        _reject_sidecars(final)
                        refresh = True
                    else:
                        quarantine = _new_quarantine_path(final)
                        _reject_sidecars(final)
                        os.replace(final, quarantine)
                _reject_sidecars(final)
                _publish(temporary, final, retry=refresh)
            except BaseException:
                # A valid snapshot is still in place after a failed refresh; only then is the copy disposable.
                keep_temporary = not refresh
                raise
        finally:
            if not keep_temporary:
                _remove_temp(temporary)

        if not refresh:
            _prune(directory)
        # Runs after refreshes too: an earlier failed first publish leaves a temp copy behind.
        _cleanup_leftovers(directory, today)
        return final
    finally:
        _release_lock(lock_fd)


def restore_database(snapshot: Path, target: Path) -> None:
    """Restore a verified snapshot to a new path; existing targets are never replaced."""
    snapshot = Path(snapshot)
    target = Path(target)
    # SQLite callbacks bound work, not OS stalls; use a worker process for hard deadlines.
    deadline = time.monotonic() + _BACKUP_DEADLINE
    if not _is_valid_snapshot(snapshot, deadline=deadline):
        raise RuntimeError(f"invalid snapshot: {snapshot}")
    targets = (target, Path(str(target) + "-wal"), Path(str(target) + "-shm"), Path(str(target) + "-journal"))
    for candidate in targets:
        if os.path.lexists(candidate):
            raise FileExistsError(f"restore target already exists: {candidate}")
    target.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.tmp-", suffix=".sqlite3", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temp_name)
    try:
        _copy_verified(snapshot, temporary, deadline, "restore")
        if time.monotonic() >= deadline:
            raise _BackupDeadlineExceeded("SQLite restore exceeded time limit")
        # sidecar rechecks require no concurrent writers; use a private directory if shared.
        for candidate in targets:
            if os.path.lexists(candidate):
                raise FileExistsError(f"restore target already exists: {candidate}")
        # Hard-link publication is atomic and fails if another process created target.
        os.link(temporary, target)
    finally:
        _remove_temp(temporary)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create or restore local SQLite backups (files are unencrypted)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="create or refresh today's snapshot")
    backup.add_argument("source", nargs="?", type=Path, default=_PROJECT_ROOT / "budget_state.sqlite3")
    backup.add_argument("--directory", type=Path, default=_PROJECT_ROOT / "backups")
    restore = commands.add_parser("restore", help="restore snapshot to a new path")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("target", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = backup_database(args.source, args.directory, dt.datetime.now(_VIETNAM_TZ).date())
        else:
            restore_database(args.snapshot, args.target)
            result = args.target
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        parser.exit(1, f"db_backup: {exc}\n")
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
