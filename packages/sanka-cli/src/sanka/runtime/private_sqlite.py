# SPDX-License-Identifier: AGPL-3.0-only
"""Private-by-default SQLite files for local migration state."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, cast


def connect_private_sqlite(path: str | Path, **kwargs: Any) -> sqlite3.Connection:
    """Open SQLite state without exposing its database or sidecar files."""
    state_path = Path(path)
    parent = state_path.parent
    created_parent = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        parent_mode = stat.S_IMODE(parent.stat().st_mode)
        if created_parent:
            parent.chmod(0o700)
            parent_mode = 0o700
        if parent_mode & 0o077:
            raise PermissionError(
                f"migration state directory must not be group/world accessible: {parent}"
            )
        descriptor = os.open(state_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        state_path.chmod(0o600)
    connection = cast(sqlite3.Connection, sqlite3.connect(state_path, **kwargs))
    if os.name == "posix":
        state_path.chmod(0o600)
    return connection
