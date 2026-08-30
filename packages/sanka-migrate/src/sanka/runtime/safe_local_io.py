# SPDX-License-Identifier: AGPL-3.0-only
"""Descriptor-relative, no-follow local filesystem primitives."""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path


class UnsafeLocalPathError(OSError):
    """A local runtime path crosses a link or non-regular filesystem object."""


def absolute_path(path: str | Path) -> Path:
    """Return an absolute lexical path without resolving symbolic links."""

    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def open_safe_directory_fd(
    path: str | Path,
    *,
    create: bool = True,
    mode: int = 0o700,
) -> int:
    """Open a directory by walking every component through anchored descriptors."""

    directory = absolute_path(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory.anchor, flags)
    try:
        for component in directory.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, mode=mode, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as error:
                try:
                    info = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                except OSError:
                    info = None
                if info is not None and stat.S_ISLNK(info.st_mode):
                    raise UnsafeLocalPathError(
                        f"local path contains a symbolic link: {directory}"
                    ) from error
                raise UnsafeLocalPathError(
                    f"local path contains an unsafe directory component: {directory}"
                ) from error
            os.close(descriptor)
            descriptor = child
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise UnsafeLocalPathError(f"local path is not a directory: {directory}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def ensure_safe_directory(path: str | Path, *, mode: int = 0o700) -> Path:
    """Create a directory tree without ever following a symbolic link."""

    directory = absolute_path(path)
    descriptor = open_safe_directory_fd(directory, mode=mode)
    os.close(descriptor)
    return directory


def _validate_regular_at(
    directory_fd: int,
    name: str,
    *,
    allow_missing: bool = False,
) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeLocalPathError(f"local file is a symbolic link: {name}")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeLocalPathError(f"local file is not a regular file: {name}")
    if info.st_nlink != 1:
        raise UnsafeLocalPathError(f"local file has multiple hard links: {name}")
    return info


def validate_regular_file(
    path: str | Path, *, allow_missing: bool = False
) -> os.stat_result | None:
    """Reject link traversal and require a single-link regular file."""

    target = absolute_path(path)
    directory_fd = open_safe_directory_fd(target.parent)
    try:
        return _validate_regular_at(directory_fd, target.name, allow_missing=allow_missing)
    finally:
        os.close(directory_fd)


def safe_write_bytes_at(
    directory_fd: int,
    name: str,
    content: bytes,
    *,
    mode: int = 0o600,
) -> None:
    """Atomically replace one regular file inside an already-bound directory."""

    if not name or Path(name).name != name or name in {".", ".."}:
        raise UnsafeLocalPathError(f"unsafe local file name: {name!r}")
    _validate_regular_at(directory_fd, name, allow_missing=True)
    temporary_name = f".{name}.sanka-{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=directory_fd,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("could not write the local file")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        raise


def safe_write_text(path: str | Path, content: str, *, mode: int = 0o600) -> Path:
    """Atomically replace a regular file through its bound parent descriptor."""

    target = absolute_path(path)
    directory_fd = open_safe_directory_fd(target.parent)
    try:
        safe_write_bytes_at(directory_fd, target.name, content.encode("utf-8"), mode=mode)
    finally:
        os.close(directory_fd)
    return target


def safe_read_bytes_at(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int = 128 * 1024 * 1024,
) -> bytes:
    """Read one stable, bounded regular file inside a bound directory."""

    expected = _validate_regular_at(directory_fd, name)
    descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if expected is None or (opened.st_dev, opened.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise UnsafeLocalPathError(f"local file changed while opening: {name}")
        if opened.st_size > max_bytes:
            raise UnsafeLocalPathError(f"local file exceeds the read limit: {name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise UnsafeLocalPathError(f"local file exceeds the read limit: {name}")
        final = os.fstat(descriptor)
        if (final.st_dev, final.st_ino, final.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise UnsafeLocalPathError(f"local file changed while reading: {name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def safe_read_bytes(path: str | Path, *, max_bytes: int = 128 * 1024 * 1024) -> bytes:
    target = absolute_path(path)
    directory_fd = open_safe_directory_fd(target.parent, create=False)
    try:
        return safe_read_bytes_at(directory_fd, target.name, max_bytes=max_bytes)
    finally:
        os.close(directory_fd)


def safe_read_text(path: str | Path, *, max_bytes: int = 128 * 1024 * 1024) -> str:
    return safe_read_bytes(path, max_bytes=max_bytes).decode("utf-8")


def safe_copy_regular_file(
    source: str | Path,
    destination: str | Path,
    *,
    max_bytes: int = 4 * 1024 * 1024 * 1024,
    mode: int = 0o600,
) -> Path:
    """Copy one stable source into an atomic file through bound directories."""

    source_path = absolute_path(source)
    source_directory_fd = open_safe_directory_fd(source_path.parent, create=False)
    try:
        data = safe_read_bytes_at(source_directory_fd, source_path.name, max_bytes=max_bytes)
    finally:
        os.close(source_directory_fd)
    destination_path = absolute_path(destination)
    destination_directory_fd = open_safe_directory_fd(destination_path.parent)
    try:
        safe_write_bytes_at(destination_directory_fd, destination_path.name, data, mode=mode)
    finally:
        os.close(destination_directory_fd)
    return destination_path
