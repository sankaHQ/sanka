# SPDX-License-Identifier: AGPL-3.0-only
"""Private-by-default SQLite files for local migration state."""

from __future__ import annotations

import atexit
import hashlib
import importlib
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any

from sanka.runtime.safe_local_io import (
    absolute_path,
    open_safe_directory_fd,
    safe_read_bytes_at,
    safe_write_bytes_at,
)

_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_MAX_STATE_BYTES = 512 * 1024 * 1024
_SESSIONS: dict[Path, _StateSession] = {}
_SESSIONS_LOCK = threading.Lock()


class _StateSession:
    """One process-local working database bound to a canonical state path."""

    def __init__(
        self,
        destination: Path,
        working: Path,
        temporary_root: Path,
        *,
        parent_identity: tuple[int, int],
        lock_descriptor: int,
        published_digest: str | None,
    ) -> None:
        self.destination = destination
        self.working = working
        self.temporary_root = temporary_root
        self.parent_identity = parent_identity
        self.lock_descriptor = lock_descriptor
        self.published_digest = published_digest
        self.publish_lock = threading.Lock()


class _PrivateStateConnection(sqlite3.Connection):
    """SQLite connection that atomically publishes committed state."""

    _sanka_session: _StateSession | None = None

    def commit(self) -> None:
        super().commit()
        session = self._sanka_session
        if session is not None:
            _publish_state(session, self)


def _validate_sidecars_at(directory_fd: int, state_name: str) -> None:
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = state_name + suffix
        try:
            info = os.stat(sidecar, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise OSError(f"migration state sidecar is a symbolic link: {sidecar}")
        raise OSError(
            f"migration state has an unexpected SQLite sidecar; close other writers: {sidecar}"
        )


def _prepare_parent(state_path: Path) -> int:
    parent = state_path.parent
    directory_fd = open_safe_directory_fd(parent)
    parent_mode = stat.S_IMODE(os.fstat(directory_fd).st_mode)
    if os.name == "posix" and parent_mode & 0o077:
        os.close(directory_fd)
        raise PermissionError(
            f"migration state directory must not be group/world accessible: {parent}"
        )
    return directory_fd


def _copy_into_working(directory_fd: int, state_name: str, working: Path) -> str | None:
    try:
        data = safe_read_bytes_at(directory_fd, state_name, max_bytes=_MAX_STATE_BYTES)
    except FileNotFoundError:
        return None
    directory_fd = open_safe_directory_fd(working.parent)
    try:
        safe_write_bytes_at(directory_fd, working.name, data, mode=0o600)
    finally:
        os.close(directory_fd)
    return hashlib.sha256(data).hexdigest()


def _acquire_state_lock(directory_fd: int, state_name: str) -> int:
    """Reject a second process before either process snapshots migration state."""

    lock_name = f".{state_name}.sanka.lock"
    descriptor = os.open(
        lock_name,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f"migration state lock is not a private regular file: {lock_name}")
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OSError(
                    f"migration state is already open in another process: {state_name}"
                ) from error
        elif os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")

            if info.st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise OSError(
                    f"migration state is already open in another process: {state_name}"
                ) from error
        else:
            raise OSError("migration state locking is unsupported on this platform")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _session_for(state_path: Path) -> _StateSession:
    with _SESSIONS_LOCK:
        existing = _SESSIONS.get(state_path)
        if existing is not None:
            return existing
        parent_fd = _prepare_parent(state_path)
        lock_descriptor: int | None = None
        try:
            parent_info = os.fstat(parent_fd)
            parent_identity = (parent_info.st_dev, parent_info.st_ino)
            lock_descriptor = _acquire_state_lock(parent_fd, state_path.name)
            _validate_sidecars_at(parent_fd, state_path.name)
            temporary_root = Path(tempfile.mkdtemp(prefix="sanka-state-session-"))
            os.chmod(temporary_root, 0o700)
            atexit.register(shutil.rmtree, temporary_root, ignore_errors=True)
            working = temporary_root / "state.db"
            published_digest = _copy_into_working(parent_fd, state_path.name, working)
            session = _StateSession(
                state_path,
                working,
                temporary_root,
                parent_identity=parent_identity,
                lock_descriptor=lock_descriptor,
                published_digest=published_digest,
            )
            atexit.register(os.close, lock_descriptor)
            lock_descriptor = None
            _SESSIONS[state_path] = session
            return session
        finally:
            if lock_descriptor is not None:
                os.close(lock_descriptor)
            os.close(parent_fd)


def _published_digest_at(directory_fd: int, state_name: str) -> str | None:
    try:
        data = safe_read_bytes_at(directory_fd, state_name, max_bytes=_MAX_STATE_BYTES)
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


def _validate_state_lock_at(directory_fd: int, session: _StateSession) -> None:
    lock_name = f".{session.destination.name}.sanka.lock"
    try:
        entry = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise OSError(f"migration state lock disappeared: {lock_name}") from error
    opened = os.fstat(session.lock_descriptor)
    if stat.S_ISLNK(entry.st_mode) or (entry.st_dev, entry.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        raise OSError(f"migration state lock changed outside this process: {lock_name}")


def _publish_state(session: _StateSession, connection: sqlite3.Connection) -> None:
    with session.publish_lock:
        content = connection.serialize()
        if len(content) > _MAX_STATE_BYTES:
            raise OSError(
                f"migration state exceeds the {_MAX_STATE_BYTES}-byte safety limit: "
                f"{session.destination}"
            )
        directory_fd = open_safe_directory_fd(session.destination.parent)
        try:
            parent_info = os.fstat(directory_fd)
            if (parent_info.st_dev, parent_info.st_ino) != session.parent_identity:
                raise OSError(
                    f"migration state directory changed before publication: "
                    f"{session.destination.parent}"
                )
            _validate_state_lock_at(directory_fd, session)
            _validate_sidecars_at(directory_fd, session.destination.name)
            if (
                _published_digest_at(directory_fd, session.destination.name)
                != session.published_digest
            ):
                raise OSError(
                    f"migration state changed outside this process: {session.destination}"
                )
            safe_write_bytes_at(directory_fd, session.destination.name, content, mode=0o600)
            session.published_digest = hashlib.sha256(content).hexdigest()
        finally:
            os.close(directory_fd)


def connect_private_sqlite(path: str | Path, **kwargs: Any) -> sqlite3.Connection:
    """Open state through a private working database and atomic publication.

    SQLite never opens the repository-controlled destination pathname. That
    removes main-file and journal-sidecar link races from the SQLite VFS while
    preserving ordinary multi-connection locking inside this process.
    """

    if "factory" in kwargs:
        raise TypeError("connect_private_sqlite owns the SQLite connection factory")
    state_path = absolute_path(path)
    session = _session_for(state_path)
    connection = sqlite3.connect(session.working, factory=_PrivateStateConnection, **kwargs)
    connection._sanka_session = session
    connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    return connection
