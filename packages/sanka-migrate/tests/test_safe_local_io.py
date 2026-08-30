# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pathlib import Path

import pytest

from sanka.runtime.safe_local_io import (
    UnsafeLocalPathError,
    safe_copy_regular_file,
    safe_write_text,
)


def test_safe_copy_regular_file_copies_exact_content(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    source.write_bytes(b"sqlite-bytes")

    destination = safe_copy_regular_file(source, tmp_path / "output" / "copy.sqlite3")

    assert destination.read_bytes() == b"sqlite-bytes"
    assert destination.stat().st_mode & 0o077 == 0


def test_safe_copy_regular_file_rejects_symlink_source(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    source.write_bytes(b"sqlite-bytes")
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(source)

    with pytest.raises(UnsafeLocalPathError, match="symbolic link"):
        safe_copy_regular_file(link, tmp_path / "copy.sqlite3")


def test_safe_write_replaces_regular_file_atomically(tmp_path: Path) -> None:
    path = tmp_path / "artifacts" / "scan.json"
    safe_write_text(path, "first\n")
    safe_write_text(path, "second\n")
    assert path.read_text(encoding="utf-8") == "second\n"


def test_safe_write_rejects_file_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("sentinel\n", encoding="utf-8")
    link = tmp_path / "scan.json"
    link.symlink_to(target)
    with pytest.raises(UnsafeLocalPathError, match="symbolic link"):
        safe_write_text(link, "overwrite\n")
    assert target.read_text(encoding="utf-8") == "sentinel\n"


def test_safe_write_rejects_directory_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "artifacts"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(UnsafeLocalPathError, match="symbolic link"):
        safe_write_text(link / "scan.json", "overwrite\n")
    assert not (target / "scan.json").exists()
