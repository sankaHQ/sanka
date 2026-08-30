# SPDX-License-Identifier: AGPL-3.0-only
"""No-follow local filesystem primitives for runtime-owned state and artifacts."""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path


class UnsafeLocalPathError(OSError):
    """A local runtime path crosses a symlink or non-regular filesystem object."""


def absolute_path(path: str | Path) -> Path:
    """Return an absolute lexical path without resolving symbolic links."""

    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def ensure_safe_directory(path: str | Path, *, mode: int = 0o700) -> Path:
    """Create a directory tree while rejecting every symbolic-link component."""

    directory = absolute_path(path)
    current = Path(directory.anchor)
    for component in directory.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            with suppress(FileExistsError):
                current.mkdir(mode=mode)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeLocalPathError(f"local path contains a symbolic link: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeLocalPathError(f"local path component is not a directory: {current}")
    return directory


def validate_regular_file(
    path: str | Path, *, allow_missing: bool = False
) -> os.stat_result | None:
    """Reject link traversal and require a single-link regular file."""

    target = absolute_path(path)
    ensure_safe_directory(target.parent)
    try:
        info = target.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeLocalPathError(f"local file is a symbolic link: {target}")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeLocalPathError(f"local file is not a regular file: {target}")
    if info.st_nlink != 1:
        raise UnsafeLocalPathError(f"local file has multiple hard links: {target}")
    return info


def safe_write_text(path: str | Path, content: str, *, mode: int = 0o600) -> Path:
    """Atomically replace a regular file without following the target path."""

    target = absolute_path(path)
    parent = ensure_safe_directory(target.parent)
    validate_regular_file(target, allow_missing=True)
    temporary_name = f".{target.name}.sanka-{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(parent, directory_flags)
    try:
        descriptor = os.open(temporary_name, flags, mode, dir_fd=directory_fd)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary_name,
                target.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)
            raise
    finally:
        os.close(directory_fd)
    return target


def safe_read_bytes(path: str | Path, *, max_bytes: int = 128 * 1024 * 1024) -> bytes:
    """Read one regular single-link file through a no-follow descriptor."""

    target = absolute_path(path)
    expected = validate_regular_file(target)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if expected is None or (opened.st_dev, opened.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise UnsafeLocalPathError(f"local file changed while opening: {target}")
        if opened.st_size > max_bytes:
            raise UnsafeLocalPathError(f"local file exceeds the read limit: {target}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise UnsafeLocalPathError(f"local file exceeds the read limit: {target}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def safe_read_text(path: str | Path, *, max_bytes: int = 128 * 1024 * 1024) -> str:
    return safe_read_bytes(path, max_bytes=max_bytes).decode("utf-8")


def safe_copy_regular_file(
    source: str | Path,
    destination: str | Path,
    *,
    max_bytes: int = 4 * 1024 * 1024 * 1024,
    mode: int = 0o600,
) -> Path:
    """Copy one stable no-follow source into an atomic runtime-owned file."""

    source_path = absolute_path(source)
    expected = validate_regular_file(source_path)
    destination_path = absolute_path(destination)
    parent = ensure_safe_directory(destination_path.parent)
    validate_regular_file(destination_path, allow_missing=True)
    temporary_name = f".{destination_path.name}.sanka-{secrets.token_hex(12)}.tmp"
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(parent, directory_flags)
    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source_path, source_flags)
        opened = os.fstat(source_fd)
        if expected is None or (opened.st_dev, opened.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise UnsafeLocalPathError(f"local file changed while opening: {source_path}")
        if opened.st_size > max_bytes:
            raise UnsafeLocalPathError(f"local file exceeds the copy limit: {source_path}")
        destination_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=directory_fd,
        )
        total = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UnsafeLocalPathError(f"local file exceeds the copy limit: {source_path}")
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("could not copy the local file")
                view = view[written:]
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        os.replace(
            temporary_name,
            destination_path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except BaseException:
        if destination_fd is not None:
            os.close(destination_fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        os.close(directory_fd)
    return destination_path
