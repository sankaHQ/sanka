# SPDX-License-Identifier: Apache-2.0
"""Bounded local Code source packaging; never execute project files."""

from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath

import click

MAX_ZIP = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_FILES = 20000
EXCLUDED = {
    ".git",
    ".ssh",
    ".aws",
    ".env",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".sanka",
    ".next",
}


def excluded(name: str) -> bool:
    return any(
        part.lower() in EXCLUDED or part.lower().startswith(".env.")
        for part in PurePosixPath(name).parts
    )


def validate_zip(content: bytes) -> bytes:
    if not 0 < len(content) <= MAX_ZIP:
        raise click.ClickException("Source ZIP must be nonempty and at most 8 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_FILES:
                raise ValueError("ZIP must have 1-20,000 entries")
            names: set[str] = set()
            total = 0
            for entry in entries:
                name = entry.filename
                parts = name.rstrip("/").split("/")
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (
                    not name
                    or name.startswith("/")
                    or "\\" in name
                    or ":" in name
                    or any(p in {"", ".", ".."} for p in parts)
                    or any(ord(c) < 32 for c in name)
                    or name.casefold() in names
                    or entry.flag_bits & 1
                    or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    raise ValueError(
                        "ZIP contains unsafe paths, links, special files, duplicates, or encryption"
                    )
                names.add(name.casefold())
                if excluded(name):
                    raise ValueError(f"Remove excluded files from ZIP before uploading: {name}")
                total += entry.file_size
                if total > MAX_EXPANDED:
                    raise ValueError("Source exceeds 64 MiB expanded")
                if not entry.is_dir():
                    with archive.open(entry) as stream:
                        if len(stream.read(entry.file_size + 1)) != entry.file_size:
                            raise ValueError("ZIP entry size mismatch")
    except (ValueError, OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    return content


def package_source(path: Path) -> bytes:
    try:
        return _package_source(path)
    except OSError as exc:
        raise click.ClickException(f"Cannot safely read source: {exc.strerror}") from exc


def _package_source(path: Path) -> bytes:
    if path.is_symlink():
        raise click.ClickException("Source must not be a symbolic link")
    if path.is_file():
        with path.open("rb") as stream:
            return validate_zip(stream.read(MAX_ZIP + 1))
    if not path.is_dir():
        raise click.ClickException("Select an existing directory or ZIP")
    result = io.BytesIO()
    total = count = 0
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for root, directories, files, root_fd in os.fwalk(path, follow_symlinks=False):
            directories[:] = sorted(d for d in directories if not excluded(d))
            for directory in directories:
                if (Path(root) / directory).is_symlink():
                    raise click.ClickException("Source contains a directory symlink")
            for filename in sorted(files):
                file = Path(root) / filename
                name = file.relative_to(path).as_posix()
                if excluded(name):
                    continue
                # O_NOFOLLOW closes the ordinary symlink replacement race at open.
                descriptor = os.open(
                    filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd
                )
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise click.ClickException("Source contains a special file")
                    content = stream.read(MAX_EXPANDED - total + 1)
                total += len(content)
                count += 1
                if total > MAX_EXPANDED or count > MAX_FILES:
                    raise click.ClickException("Source exceeds 64 MiB or 20,000 files")
                info = zipfile.ZipInfo(name)  # Fixed timestamp makes retry digests stable.
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, content)
                if result.tell() > MAX_ZIP:
                    raise click.ClickException("Source ZIP exceeds 8 MiB")
    return validate_zip(result.getvalue())
