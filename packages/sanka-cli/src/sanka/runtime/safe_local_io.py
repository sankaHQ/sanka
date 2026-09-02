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


def _validate_read_limit(max_bytes: int) -> None:
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")


def _open_regular_at(directory_fd: int, name: str) -> tuple[int, os.stat_result]:
    expected = _validate_regular_at(directory_fd, name)
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if expected is None or (opened.st_dev, opened.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise UnsafeLocalPathError(f"local file changed while opening: {name}")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise UnsafeLocalPathError(f"local file is no longer safe: {name}")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _assert_file_unchanged(descriptor: int, opened: os.stat_result, name: str) -> None:
    final = os.fstat(descriptor)
    if (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
        final.st_ctime_ns,
        final.st_nlink,
    ) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
        1,
    ):
        raise UnsafeLocalPathError(f"local file changed while reading: {name}")


def _write_all(descriptor: int, content: bytes | memoryview) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("could not write the local file")
        view = view[written:]


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
        _write_all(descriptor, content)
        os.fsync(descriptor)
        if os.fstat(descriptor).st_nlink != 1:
            raise UnsafeLocalPathError(
                f"temporary local file has multiple hard links: {temporary_name}"
            )
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        _validate_regular_at(directory_fd, name)
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

    _validate_read_limit(max_bytes)
    descriptor, opened = _open_regular_at(directory_fd, name)
    try:
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
        _assert_file_unchanged(descriptor, opened, name)
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
    """Stream one stable source into an atomic file through bound directories."""

    _validate_read_limit(max_bytes)
    source_path = absolute_path(source)
    source_directory_fd = open_safe_directory_fd(source_path.parent, create=False)
    try:
        source_fd, opened = _open_regular_at(source_directory_fd, source_path.name)
    finally:
        os.close(source_directory_fd)
    try:
        if opened.st_size > max_bytes:
            raise UnsafeLocalPathError(f"local file exceeds the read limit: {source_path.name}")
        destination_path = absolute_path(destination)
        destination_directory_fd = open_safe_directory_fd(destination_path.parent)
        try:
            _validate_regular_at(
                destination_directory_fd,
                destination_path.name,
                allow_missing=True,
            )
            temporary_name = f".{destination_path.name}.sanka-{secrets.token_hex(12)}.tmp"
            destination_fd: int | None = None
            try:
                destination_fd = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    mode,
                    dir_fd=destination_directory_fd,
                )
                total = 0
                while True:
                    chunk = os.read(source_fd, min(1024 * 1024, max_bytes - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise UnsafeLocalPathError(
                            f"local file exceeds the read limit: {source_path.name}"
                        )
                    _write_all(destination_fd, chunk)
                _assert_file_unchanged(source_fd, opened, source_path.name)
                os.fsync(destination_fd)
                if os.fstat(destination_fd).st_nlink != 1:
                    raise UnsafeLocalPathError(
                        f"temporary local file has multiple hard links: {temporary_name}"
                    )
                os.close(destination_fd)
                destination_fd = None
                os.replace(
                    temporary_name,
                    destination_path.name,
                    src_dir_fd=destination_directory_fd,
                    dst_dir_fd=destination_directory_fd,
                )
                _validate_regular_at(destination_directory_fd, destination_path.name)
                os.fsync(destination_directory_fd)
            except BaseException:
                if destination_fd is not None:
                    os.close(destination_fd)
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=destination_directory_fd)
                raise
        finally:
            os.close(destination_directory_fd)
    finally:
        os.close(source_fd)
    return destination_path
