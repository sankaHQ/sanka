# SPDX-License-Identifier: AGPL-3.0-only
"""Private-by-default SQLite files for local migration state."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, cast

from sanka.runtime.safe_local_io import (
    UnsafeLocalPathError,
    absolute_path,
    ensure_safe_directory,
    validate_regular_file,
)

_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


def _validate_sidecars(state_path: Path) -> None:
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{state_path}{suffix}")
        info = validate_regular_file(sidecar, allow_missing=True)
        if info is not None and os.name == "posix":
            os.chmod(sidecar, 0o600, follow_symlinks=False)


def connect_private_sqlite(path: str | Path, **kwargs: Any) -> sqlite3.Connection:
    """Open SQLite state without exposing its database or sidecar files."""
    state_path = absolute_path(path)
    parent = state_path.parent
    created_parent = not parent.exists()
    ensure_safe_directory(parent)
    if os.name == "posix":
        parent_mode = stat.S_IMODE(parent.lstat().st_mode)
        if created_parent:
            os.chmod(parent, 0o700, follow_symlinks=False)
            parent_mode = 0o700
        if parent_mode & 0o077:
            raise PermissionError(
                f"migration state directory must not be group/world accessible: {parent}"
            )
        _validate_sidecars(state_path)
        prior = validate_regular_file(state_path, allow_missing=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(state_path, flags, 0o600)
        opened = os.fstat(descriptor)
        os.close(descriptor)
        current = validate_regular_file(state_path)
        if current is None or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise UnsafeLocalPathError(f"migration state file changed while opening: {state_path}")
        if prior is not None and (prior.st_dev, prior.st_ino) != (current.st_dev, current.st_ino):
            raise UnsafeLocalPathError(f"migration state file changed while opening: {state_path}")
        os.chmod(state_path, 0o600, follow_symlinks=False)
    connection = cast(sqlite3.Connection, sqlite3.connect(state_path, **kwargs))
    if os.name == "posix":
        try:
            _validate_sidecars(state_path)
        except OSError:
            connection.close()
            raise
        current = validate_regular_file(state_path)
        if current is None or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            connection.close()
            raise UnsafeLocalPathError(f"migration state file changed while opening: {state_path}")
        os.chmod(state_path, 0o600, follow_symlinks=False)
    return connection
