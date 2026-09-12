# SPDX-License-Identifier: AGPL-3.0-only
"""Shared immutable-plan artifact hashing for local and hosted Code execution."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, NoReturn

from sanka.runtime.extensions.model import ExtensionError
from sanka.runtime.hashing import content_hash
from sanka.runtime.safe_local_io import safe_read_bytes

MAX_ARTIFACT_FILES = 20_000


def _error(code: str, message: str, **details: Any) -> NoReturn:
    raise ExtensionError(code, message, details=details)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(safe_read_bytes(path)).hexdigest()


def artifact_digest(path: Path) -> str:
    try:
        status = path.lstat()
    except OSError as error:
        raise ExtensionError(
            "SANKA_EXTENSION_IDENTITY",
            "Reviewed extension artifact is missing",
            details={"path": str(path), "reason": str(error)},
        ) from error
    if stat.S_ISLNK(status.st_mode):
        _error("SANKA_EXTENSION_IDENTITY", "Reviewed artifact cannot be a symlink", path=str(path))
    if stat.S_ISREG(status.st_mode):
        return _file_digest(path)
    if not stat.S_ISDIR(status.st_mode):
        _error("SANKA_EXTENSION_IDENTITY", "Reviewed artifact has an unsafe type", path=str(path))
    records: list[dict[str, str]] = []
    count = 0
    for current, directories, files in os.walk(path, topdown=True, followlinks=False):
        root = Path(current)
        directories.sort()
        files.sort()
        for name in directories:
            child = root / name
            if child.is_symlink():
                _error(
                    "SANKA_EXTENSION_IDENTITY",
                    "Reviewed artifact tree contains a symlink",
                    path=str(child),
                )
            records.append({"path": child.relative_to(path).as_posix(), "type": "directory"})
        for name in files:
            child = root / name
            count += 1
            if count > MAX_ARTIFACT_FILES:
                _error(
                    "SANKA_EXTENSION_IDENTITY",
                    "Reviewed artifact tree exceeds the file limit",
                    path=str(path),
                    limit=MAX_ARTIFACT_FILES,
                )
            if child.is_symlink():
                _error(
                    "SANKA_EXTENSION_IDENTITY",
                    "Reviewed artifact tree contains a symlink",
                    path=str(child),
                )
            records.append(
                {
                    "path": child.relative_to(path).as_posix(),
                    "sha256": _file_digest(child),
                    "type": "file",
                }
            )
    return content_hash(records).removeprefix("sha256:")
