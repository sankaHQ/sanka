# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted immutable marketplace snapshots and hash-bound extension installs."""

from __future__ import annotations

import configparser
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from email.parser import BytesParser
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

from packaging.requirements import InvalidRequirement, Requirement
from packaging.tags import parse_tag, sys_tags
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from sanka.runtime.connector_client import ExtensionHostClient, build_remote_extension
from sanka.runtime.extensions.discovery import (
    IDENTIFIER,
    STATUS_ORDER,
    _compatible,
    _normalized_distribution,
    _wheel_identity,
    load_marketplace,
    recommend,
)
from sanka.runtime.extensions.model import (
    LIFECYCLE_COMMANDS,
    ExtensionError,
    Fingerprint,
    Manifest,
    Recommendation,
    SystemSupport,
    Wheel,
)
from sanka.runtime.hashing import content_hash
from sanka_cli import __version__
from sanka_extensions.systems import ENTRY_POINT_GROUP, ExtensionRegistration

OFFICIAL_IDENTITY = "github.com/sankaHQ/extensions"
OFFICIAL_SOURCE = "https://github.com/sankaHQ/extensions.git"
DEFAULT_EXTENSION_ID = "sanka/drf-to-fastapi"
MAX_WHEEL_BYTES = 128 * 1024 * 1024
MAX_WHEEL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_WHEEL_METADATA_BYTES = 1024 * 1024
MAX_WHEEL_MEMBERS = 10_000
MARKETPLACE_SCHEMA = "sanka-extension-marketplaces/v1"
INSTALLATION_SCHEMA = "sanka-extension-installations/v1"
LOCK_SCHEMA = "sanka-extension-lock/v1"
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_ENTRY_POINT = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def user_extension_root() -> Path:
    override = os.environ.get("SANKA_HOME")
    return (Path(override) if override else Path.home() / ".sanka") / "extensions"


def _error(code: str, message: str, **details: Any) -> NoReturn:
    raise ExtensionError(code, message, details=details)


def _store_operation[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    @wraps(operation)
    def protected(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return operation(*args, **kwargs)
        except ExtensionError:
            raise
        except OSError as error:
            raise ExtensionError(
                "SANKA_EXTENSION_IO",
                "Extension store operation failed",
                details={"operation": operation.__name__, "reason": str(error)},
            ) from error

    return protected


def _close_descriptors(descriptors: Iterator[int]) -> None:
    primary_active = sys.exc_info()[0] is not None
    first_error: OSError | None = None
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError as error:
            if not primary_active and first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def _close_descriptor(descriptor: int) -> None:
    _close_descriptors(iter((descriptor,)))


@contextmanager
def _regular_file(path: Path, *, require_single_link: bool = True) -> Iterator[Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError) as error:
        raise ExtensionError(
            "SANKA_EXTENSION_PATH",
            "Extension cache path must be a regular file",
            details={"path": str(path)},
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or before.st_dev != opened.st_dev
            or before.st_ino != opened.st_ino
            or (require_single_link and before.st_nlink != 1)
            or (require_single_link and opened.st_nlink != 1)
        ):
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension artifact path must be one regular file",
                path=str(path),
            )
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source
        after = path.lstat()
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or (require_single_link and after.st_nlink != 1)
        ):
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension cache path changed while it was being read",
                path=str(path),
            )
    except ExtensionError:
        raise
    except OSError as error:
        raise ExtensionError(
            "SANKA_EXTENSION_PATH",
            "Extension artifact path changed or became unreadable",
            details={"path": str(path)},
        ) from error
    finally:
        _close_descriptor(descriptor)


def _sha256_file(path: Path, *, require_single_link: bool = True) -> str:
    digest = hashlib.sha256()
    with _regular_file(path, require_single_link=require_single_link) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity_key(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()


def _marketplace_sort_key(value: dict[str, Any]) -> str:
    return str(value.get("name"))


def _installation_sort_key(value: dict[str, Any]) -> str:
    return str(value.get("artifact_digest"))


def _tree_records(
    descriptor: int,
    prefix: str = "",
    *,
    error_code: str = "SANKA_MARKETPLACE_PATH_INVALID",
    subject: str = "Marketplace snapshot",
    allowed_symlinks: Mapping[str, Path] | None = None,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with os.scandir(descriptor) as entries:
        children = sorted(entries, key=lambda item: item.name)
    for child in children:
        relative = f"{prefix}/{child.name}" if prefix else child.name
        linked = os.stat(child.name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(linked.st_mode):
            target = Path(os.readlink(child.name, dir_fd=descriptor))
            expected = (allowed_symlinks or {}).get(relative)
            if expected is None or not target.is_absolute() or target != expected:
                _error(
                    error_code,
                    f"{subject} contains an unverified symlink",
                    path=relative,
                )
            records.append(
                {
                    "type": "symlink",
                    "path": relative,
                    "size": linked.st_size,
                    "sha256": _sha256_file(expected, require_single_link=False),
                    "target": str(target),
                }
            )
            continue
        if not (stat.S_ISDIR(linked.st_mode) or stat.S_ISREG(linked.st_mode)):
            _error(
                error_code,
                f"{subject} may contain only real directories and regular files",
                path=relative,
            )
        if stat.S_ISREG(linked.st_mode):
            file_descriptor = os.open(child.name, _FILE_FLAGS, dir_fd=descriptor)
            try:
                opened = os.fstat(file_descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
                ):
                    _error(
                        error_code,
                        f"{subject} file changed while being inspected",
                        path=relative,
                    )
                digest = hashlib.sha256()
                with os.fdopen(file_descriptor, "rb", closefd=False) as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
            finally:
                _close_descriptor(file_descriptor)
            records.append(
                {
                    "type": "file",
                    "path": relative,
                    "size": opened.st_size,
                    "sha256": digest.hexdigest(),
                }
            )
        else:
            records.append({"type": "directory", "path": relative, "size": 0, "sha256": None})
            directory = os.open(child.name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                opened = os.fstat(directory)
                if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                    linked.st_dev,
                    linked.st_ino,
                ):
                    _error(
                        error_code,
                        f"{subject} directory changed while being inspected",
                        path=relative,
                    )
                records.extend(
                    _tree_records(
                        directory,
                        relative,
                        error_code=error_code,
                        subject=subject,
                        allowed_symlinks=allowed_symlinks,
                    )
                )
            finally:
                _close_descriptor(directory)
    return records


def _tree_digest(root: Path) -> str:
    descriptor = os.open(root, _DIRECTORY_FLAGS)
    try:
        records = _tree_records(descriptor)
    finally:
        _close_descriptor(descriptor)
    return content_hash(records)


def _canonical_source(source: str | Path) -> tuple[str, str, str]:
    raw = os.fspath(source)
    candidate = Path(raw).expanduser()
    if candidate.exists():
        if candidate.is_symlink() or not candidate.is_dir():
            _error(
                "SANKA_MARKETPLACE_SOURCE_INVALID",
                "Local marketplace source must be a real directory",
                source=raw,
            )
        resolved = candidate.resolve()
        return "local", str(resolved), f"local:{resolved.as_posix()}"

    scp = re.fullmatch(r"git@([^:]+):(.+?)(?:\.git)?", raw)
    if scp:
        host, repository = scp.groups()
        identity = f"{host.lower()}/{repository.removesuffix('.git').strip('/')}"
        return "git", OFFICIAL_SOURCE if identity == OFFICIAL_IDENTITY else raw, identity

    try:
        parsed = urlparse(raw)
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme not in {"file", "https", "ssh"}:
        _error(
            "SANKA_MARKETPLACE_SOURCE_INVALID",
            "Marketplace source must be a local directory or Git SSH/HTTPS URL",
            source=raw,
        )
    if parsed.scheme == "file":
        repository = Path(unquote(parsed.path)).resolve()
        if not repository.is_dir():
            _error(
                "SANKA_MARKETPLACE_SOURCE_INVALID",
                "Git fixture source does not exist",
                source=raw,
            )
        return "git", raw, f"file://{repository.as_posix()}"
    if not parsed.hostname:
        _error(
            "SANKA_MARKETPLACE_SOURCE_INVALID",
            "Git marketplace URL must include a host",
            source=raw,
        )
    repository = parsed.path.removesuffix(".git").strip("/")
    if not repository:
        _error(
            "SANKA_MARKETPLACE_SOURCE_INVALID",
            "Git marketplace URL must include a repository",
            source=raw,
        )
    identity = f"{parsed.hostname.lower()}/{repository}"
    return "git", OFFICIAL_SOURCE if identity == OFFICIAL_IDENTITY else raw, identity


@dataclass(frozen=True)
class MarketplaceRecord:
    name: str
    identity: str
    source: str
    trusted: bool
    snapshot_digest: str
    resolved_commit: str | None
    content_digest: str | None
    tree_digest: str
    snapshot_root: Path

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"snapshot_root": str(self.snapshot_root)}


@dataclass(frozen=True)
class ExtensionRecord:
    id: str
    version: str
    marketplace: str
    marketplace_identity: str
    manifest_digest: str
    kind: str
    providers: tuple[SystemSupport, ...]
    targets: tuple[str, ...]
    status: tuple[str, ...]
    wheels: tuple[Wheel, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "providers": [
                asdict(provider) | {"roles": list(provider.roles)} for provider in self.providers
            ],
            "targets": list(self.targets),
            "status": list(self.status),
            "wheels": [asdict(wheel) for wheel in self.wheels],
        }


@dataclass(frozen=True)
class LockEntry:
    id: str
    version: str
    marketplace_identity: str
    snapshot_digest: str
    manifest_digest: str
    distribution: str
    artifact_digest: str
    protocol_version: str
    executable: str | None
    commands: tuple[str, ...]
    enabled: bool
    configuration_digest: str
    kind: str = "migration"
    entry_point: str | None = None
    providers: tuple[SystemSupport, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "commands": list(self.commands),
            "providers": [
                asdict(provider) | {"roles": list(provider.roles)} for provider in self.providers
            ],
        }


def _exact_path(
    root: Path,
    candidate: Path,
    *,
    must_exist: bool = False,
    expected: Path | None = None,
) -> Path:
    try:
        root_status = root.lstat()
        relative = candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ExtensionError(
            "SANKA_EXTENSION_PATH",
            "Extension path cannot be placed inside its store",
            details={"path": str(candidate)},
        ) from error
    if (
        stat.S_ISLNK(root_status.st_mode)
        or not stat.S_ISDIR(root_status.st_mode)
        or (expected is not None and candidate != expected)
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        _error(
            "SANKA_EXTENSION_PATH",
            "Extension path does not have its exact store placement",
            path=str(candidate),
        )
    current = root
    missing = False
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            status = current.lstat()
        except FileNotFoundError:
            missing = True
            if must_exist:
                _error(
                    "SANKA_EXTENSION_PATH",
                    "Required extension path does not exist",
                    path=str(candidate),
                )
            continue
        except OSError as error:
            raise ExtensionError(
                "SANKA_EXTENSION_PATH",
                "Extension path cannot be inspected",
                details={"path": str(current)},
            ) from error
        if missing or stat.S_ISLNK(status.st_mode):
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension paths cannot contain symlinks or redirections",
                path=str(current),
            )
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(status.st_mode):
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension path parent must be a directory",
                path=str(current),
            )
        if stat.S_ISREG(status.st_mode) and status.st_nlink != 1:
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension state and cache files cannot have multiple links",
                path=str(current),
            )
    return candidate


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _run_fd_helper(
    descriptor: int,
    source: str,
    arguments: list[str],
    *,
    environment: Mapping[str, str],
    cwd: Path,
) -> None:
    subprocess.run(
        [str(Path(sys.executable).resolve()), "-I", "-c", source, str(descriptor), *arguments],
        check=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=dict(environment),
        pass_fds=(descriptor,),
    )


def _create_venv(
    environments: int,
    temporary: str,
    *,
    environment: Mapping[str, str],
    cwd: Path,
    system_site_packages: bool = True,
) -> None:
    _run_fd_helper(
        environments,
        (
            "import os, sys, venv; "
            "os.fchdir(int(sys.argv[1])); "
            f"venv.EnvBuilder(with_pip=False, system_site_packages={system_site_packages!r}, "
            "symlinks=os.name != 'nt').create(sys.argv[2])"
        ),
        [temporary],
        environment=environment,
        cwd=cwd,
    )


def _venv_launcher_names() -> tuple[str, ...]:
    names = ("python", "python3", f"python{sys.version_info.major}.{sys.version_info.minor}")
    if sys.version_info[:2] == (3, 14) and sys.getfilesystemencoding() == "utf-8":
        names += ("𝜋thon",)
    return names


def _normalize_venv_launchers(environment_descriptor: int) -> None:
    if os.name == "nt":
        return
    with suppress(FileNotFoundError):
        alias = os.stat("lib64", dir_fd=environment_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(alias.st_mode):
            if Path(os.readlink("lib64", dir_fd=environment_descriptor)) != Path("lib"):
                _error(
                    "SANKA_EXTENSION_PATH",
                    "Virtual environment library alias is not verifiable",
                    path="lib64",
                )
            os.unlink("lib64", dir_fd=environment_descriptor)
    interpreter = Path(sys.executable).resolve()
    with _directory_at(environment_descriptor, "bin", Path("bin")) as bin_descriptor:
        assert bin_descriptor is not None
        for name in _venv_launcher_names():
            with suppress(FileNotFoundError):
                status = os.stat(name, dir_fd=bin_descriptor, follow_symlinks=False)
                if not (stat.S_ISLNK(status.st_mode) or stat.S_ISREG(status.st_mode)):
                    _error(
                        "SANKA_EXTENSION_PATH",
                        "Virtual environment launcher is not replaceable",
                        path=f"bin/{name}",
                    )
                os.unlink(name, dir_fd=bin_descriptor)
            os.symlink(str(interpreter), name, dir_fd=bin_descriptor)


def _run_venv_python(
    environment_descriptor: int,
    arguments: list[str],
    *,
    environment: Mapping[str, str],
    cwd: Path,
) -> None:
    _run_fd_helper(
        environment_descriptor,
        (
            "import os, sys; "
            "os.fchdir(int(sys.argv[1])); "
            "os.set_inheritable(int(sys.argv[1]), False); "
            "os.execv(sys.argv[2], [sys.argv[2], *sys.argv[3:]])"
        ),
        ["bin/python", *arguments],
        environment=environment,
        cwd=cwd,
    )


def _require_directory_identity(path: Path, descriptor: int) -> None:
    try:
        linked = path.lstat()
        opened = os.fstat(descriptor)
    except OSError as error:
        raise ExtensionError(
            "SANKA_EXTENSION_PATH",
            "Extension directory placement cannot be verified",
            details={"path": str(path), "reason": str(error)},
        ) from error
    if (
        stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISDIR(linked.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        _error(
            "SANKA_EXTENSION_PATH",
            "Extension directory changed during a store operation",
            path=str(path),
        )


@contextmanager
def _parent_descriptor(
    root: Path,
    candidate: Path,
    *,
    create: bool = False,
) -> Iterator[tuple[int, str]]:
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise ExtensionError(
            "SANKA_EXTENSION_PATH",
            "Extension path escapes its store",
            details={"path": str(candidate)},
        ) from error
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        _error(
            "SANKA_EXTENSION_PATH",
            "Extension path does not have an exact relative placement",
            path=str(candidate),
        )
    descriptor = os.open(root, _DIRECTORY_FLAGS)
    try:
        for part in relative.parts[:-1]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                _close_descriptor(descriptor)
            except BaseException:
                with suppress(OSError):
                    _close_descriptor(child)
                raise
            descriptor = child
        yield descriptor, relative.parts[-1]
    finally:
        _close_descriptor(descriptor)


@contextmanager
def _regular_at(
    parent: int,
    name: str,
    display_path: Path,
    *,
    missing_ok: bool = False,
) -> Iterator[Any | None]:
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if missing_ok:
            yield None
            return
        raise
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension state and cache must be one regular file",
                path=str(display_path),
            )
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source
    finally:
        _close_descriptor(descriptor)


@contextmanager
def _store_file(
    root: Path,
    path: Path,
    *,
    missing_ok: bool = False,
) -> Iterator[Any | None]:
    yielded = False
    try:
        with (
            _parent_descriptor(root, path) as (parent, name),
            _regular_at(parent, name, path, missing_ok=missing_ok) as source,
        ):
            yielded = True
            yield source
    except FileNotFoundError:
        if not missing_ok or yielded:
            raise
        yield None


def _store_file_sha256(root: Path, path: Path) -> str:
    digest = hashlib.sha256()
    with _store_file(root, path) as source:
        assert source is not None
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _directory_at(
    parent: int,
    name: str,
    display_path: Path,
    *,
    missing_ok: bool = False,
) -> Iterator[int | None]:
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if missing_ok:
            yield None
            return
        raise
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension directory changed while being inspected",
                path=str(display_path),
            )
        yield descriptor
    finally:
        _close_descriptor(descriptor)


def _atomic_json(root: Path, path: Path, payload: object) -> None:
    temporary = ""
    try:
        with _parent_descriptor(root, path, create=True) as (parent, name):
            for _attempt in range(10):
                temporary = f".{name}.{secrets.token_hex(8)}.tmp"
                try:
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=parent,
                    )
                    break
                except FileExistsError:
                    continue
            else:
                raise FileExistsError("could not allocate extension state temporary")
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            temporary = ""
    except OSError as error:
        raise ExtensionError(
            "SANKA_EXTENSION_IO",
            "Extension state could not be written atomically",
            details={"path": str(path), "reason": str(error)},
        ) from error
    finally:
        if temporary:
            with (
                suppress(OSError),
                _parent_descriptor(root, path) as (parent, _name),
            ):
                os.unlink(temporary, dir_fd=parent)


@contextmanager
def _locked(root: Path, path: Path, *, exclusive: bool = True) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    try:
        with _parent_descriptor(root, lock_path, create=True) as (parent, name):
            for attempt in range(3):
                try:
                    descriptor = os.open(
                        name,
                        os.O_RDWR
                        | os.O_CREAT
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=parent,
                    )
                    break
                except FileNotFoundError:
                    if attempt == 2:
                        raise
    except OSError as error:
        raise ExtensionError(
            "SANKA_EXTENSION_IO",
            "Extension mutation lock could not be opened",
            details={"path": str(lock_path), "reason": str(error)},
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension lock must be one regular file",
                path=str(lock_path),
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError as error:
            raise ExtensionError(
                "SANKA_EXTENSION_IO",
                "Extension mutation lock could not be acquired",
                details={"path": str(lock_path), "reason": str(error)},
            ) from error
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        _close_descriptor(descriptor)


def _remove_store_tree(root: Path, path: Path) -> None:
    with (
        _parent_descriptor(root, path) as (parent, name),
        _directory_at(parent, name, path, missing_ok=True) as descriptor,
    ):
        if descriptor is not None:
            shutil.rmtree(name, dir_fd=parent)


def _unlink_store_file(root: Path, path: Path) -> None:
    with (
        _parent_descriptor(root, path) as (parent, name),
        _regular_at(parent, name, path, missing_ok=True) as source,
    ):
        if source is None:
            return
        opened = os.fstat(source.fileno())
        linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension cache file changed before deletion",
                path=str(path),
            )
        os.unlink(name, dir_fd=parent)


def _rewrite_console_script(
    environment: int,
    executable: str,
    interpreter: Path,
    target: str,
) -> None:
    bin_name = "Scripts" if os.name == "nt" else "bin"
    with _directory_at(environment, bin_name, interpreter.parent) as bin_descriptor:
        assert bin_descriptor is not None
        descriptor = os.open(
            executable,
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=bin_descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                _error(
                    "SANKA_EXTENSION_INSTALL_FAILED",
                    "Installed extension executable is not one regular file",
                    executable=executable,
                )
            payload = _console_script(interpreter, target)
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(descriptor, "wb", closefd=False) as script:
                script.write(payload)
                script.flush()
                os.fsync(script.fileno())
            os.fchmod(descriptor, opened.st_mode | 0o111)
        finally:
            _close_descriptor(descriptor)


def _console_script(interpreter: Path, target: str) -> bytes:
    if _ENTRY_POINT.fullmatch(target) is None:
        _error(
            "SANKA_EXTENSION_ARTIFACT_INVALID",
            "Extension executable entry point is invalid",
            entry_point=target,
        )
    module, attribute = target.split(":", 1)
    return (
        f"#!/bin/sh\n'''exec' {shlex.quote(str(interpreter))} \"$0\" \"$@\"\n' '''\n"
        "import importlib\n"
        "import sys\n"
        "sys.dont_write_bytecode = True\n"
        f"target = importlib.import_module({module!r})\n"
        f"for name in {attribute.split('.')!r}:\n"
        "    target = getattr(target, name)\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(target())\n"
    ).encode()


class ExtensionStore:
    """One stdlib-only trust, snapshot, cache, and project-lock boundary."""

    def __init__(self, project_root: Path, user_root: Path | None = None) -> None:
        self.project_root = self._real_root(project_root, "project")
        self.user_root = self._real_root(user_root or user_extension_root(), "user")
        self._configure_official_marketplace = user_root is None
        self._marketplace_path = self.user_root / "marketplaces.json"
        self._installation_path = self.user_root / "installations.json"
        self._disabled_path = self.user_root / "disabled.json"
        self._project_lock_path = self.project_root / ".sanka" / "extensions.lock"
        self._connector_clients: dict[str, ExtensionHostClient] = {}

    def close(self) -> None:
        for client in self._connector_clients.values():
            client.close()
        self._connector_clients.clear()

    @contextmanager
    def execution_guard(self) -> Iterator[None]:
        """Keep the exact project pin stable through one lifecycle dispatch."""
        with _locked(self.project_root, self._project_lock_path, exclusive=False):
            yield

    @contextmanager
    def execution_lease(self, entry: LockEntry) -> Iterator[int]:
        """Bind the verified console script inode until its subprocess completes."""
        if entry.kind != "migration" or entry.executable is None:
            _error(
                "SANKA_EXTENSION_PROTOCOL",
                "Only migration extensions expose lifecycle executables",
                extension_id=entry.id,
            )
        if self.resolve_locked(entry.id) != entry:
            _error(
                "SANKA_EXTENSION_IDENTITY",
                "Resolved extension changed before execution",
                extension_id=entry.id,
            )
        executable = (
            self.user_root
            / "environments"
            / entry.artifact_digest
            / ("Scripts" if os.name == "nt" else "bin")
            / entry.executable
        )
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(executable, _FILE_FLAGS)
            except OSError as error:
                raise ExtensionError(
                    "SANKA_EXTENSION_NOT_CACHED",
                    "Verified extension executable could not be leased",
                    details={"path": str(executable), "reason": str(error)},
                ) from error
            assert descriptor is not None
            try:
                opened = os.fstat(descriptor)
                linked = executable.lstat()
            except OSError as error:
                raise ExtensionError(
                    "SANKA_EXTENSION_PATH",
                    "Extension executable changed while acquiring its execution lease",
                    details={"path": str(executable), "reason": str(error)},
                ) from error
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not stat.S_ISREG(linked.st_mode)
                or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
            ):
                _error(
                    "SANKA_EXTENSION_PATH",
                    "Extension executable changed while acquiring its execution lease",
                    path=str(executable),
                )
            yield descriptor
        finally:
            if descriptor is not None:
                _close_descriptor(descriptor)

    @staticmethod
    def _real_root(path: Path, label: str) -> Path:
        path = path.expanduser()
        try:
            symlink = path.is_symlink()
            path.mkdir(parents=True, exist_ok=True)
            directory = path.is_dir()
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise ExtensionError(
                "SANKA_EXTENSION_PATH",
                "Extension store root cannot be created or resolved",
                details={"store": label, "path": str(path), "reason": str(error)},
            ) from error
        if symlink:
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension store root cannot be a symlink",
                store=label,
                path=str(path),
            )
        if not directory:
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension store root must be a directory",
                store=label,
                path=str(path),
            )
        return resolved

    @staticmethod
    def _confined(
        root: Path,
        candidate: Path,
        *,
        must_exist: bool = False,
        expected: Path | None = None,
    ) -> Path:
        return _exact_path(
            root,
            candidate,
            must_exist=must_exist,
            expected=expected,
        )

    @staticmethod
    def _load_json(root: Path, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        try:
            with _store_file(root, path, missing_ok=True) as source:
                if source is None:
                    return default
                payload = json.loads(source.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExtensionError(
                "SANKA_EXTENSION_STATE_INVALID",
                "Extension state is invalid",
                details={"path": str(path), "reason": str(error)},
            ) from error
        if not isinstance(payload, dict):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Extension state must be a JSON object",
                path=str(path),
            )
        return payload

    @classmethod
    def _state_path(cls, root: Path, path: Path) -> Path:
        return cls._confined(root, path, expected=path)

    def _marketplace_state(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        path = self._state_path(self.user_root, self._marketplace_path)
        payload = self._load_json(
            self.user_root,
            path,
            {"schema_version": MARKETPLACE_SCHEMA, "marketplaces": [], "snapshots": []},
        )
        if (
            set(payload) != {"schema_version", "marketplaces", "snapshots"}
            or payload.get("schema_version") != MARKETPLACE_SCHEMA
            or not isinstance(payload.get("marketplaces"), list)
            or not isinstance(payload.get("snapshots"), list)
        ):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace configuration is invalid",
                path=str(self._marketplace_path),
            )
        records = payload["marketplaces"]
        snapshots = payload["snapshots"]
        if any(not isinstance(item, dict) for item in records):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace configuration records must be objects",
                path=str(self._marketplace_path),
            )
        if any(
            not isinstance(item, dict)
            or set(item) != {"identity", "snapshot_digest", "tree_digest"}
            or any(not isinstance(item.get(field), str) or not item[field] for field in item)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", item["tree_digest"]) is None
            for item in snapshots
        ):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace snapshot history is invalid",
                path=str(self._marketplace_path),
            )
        snapshot_keys = [(item["identity"], item["snapshot_digest"]) for item in snapshots]
        if len(snapshot_keys) != len(set(snapshot_keys)):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace snapshot history contains duplicate identities",
                path=str(self._marketplace_path),
            )
        history = {
            (item["identity"], item["snapshot_digest"]): item["tree_digest"] for item in snapshots
        }
        for item in records:
            self._record(item, history)
        names = [item["name"] for item in records]
        if len(names) != len(set(names)):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace configuration contains duplicate names",
                path=str(self._marketplace_path),
            )
        return cast(list[dict[str, Any]], records), cast(list[dict[str, str]], snapshots)

    def _raw_marketplaces(self) -> list[dict[str, Any]]:
        return self._marketplace_state()[0]

    def _record(
        self,
        value: Mapping[str, Any],
        history: Mapping[tuple[str, str], str],
    ) -> MarketplaceRecord:
        keys = {
            "content_digest",
            "identity",
            "kind",
            "name",
            "resolved_commit",
            "snapshot_digest",
            "snapshot_root",
            "source",
            "trusted",
            "tree_digest",
        }
        if set(value) != keys or not isinstance(value.get("snapshot_root"), str):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace record is invalid",
                path=str(self._marketplace_path),
            )
        strings = ("identity", "kind", "name", "snapshot_digest", "source", "tree_digest")
        if any(not isinstance(value.get(field), str) or not value[field] for field in strings):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace record fields are invalid",
                path=str(self._marketplace_path),
            )
        if (
            value["kind"] not in {"git", "local"}
            or not isinstance(value["trusted"], bool)
            or (
                value["resolved_commit"] is not None
                and not isinstance(value["resolved_commit"], str)
            )
            or (
                value["content_digest"] is not None and not isinstance(value["content_digest"], str)
            )
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value["tree_digest"]) is None
            or history.get((value["identity"], value["snapshot_digest"])) != value["tree_digest"]
        ):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace trust or snapshot fields are invalid",
                path=str(self._marketplace_path),
            )
        if value["kind"] == "git":
            valid_snapshot = (
                value["resolved_commit"] == value["snapshot_digest"]
                and value["content_digest"] is None
                and isinstance(value["snapshot_digest"], str)
                and _COMMIT.fullmatch(value["snapshot_digest"]) is not None
            )
        else:
            valid_snapshot = (
                value["resolved_commit"] is None
                and value["content_digest"] == value["snapshot_digest"]
                and value["tree_digest"] == value["snapshot_digest"]
                and isinstance(value["snapshot_digest"], str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", value["snapshot_digest"]) is not None
            )
        if not valid_snapshot:
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace snapshot identity is invalid",
                path=str(self._marketplace_path),
            )
        root = self._confined(
            self.user_root,
            self.user_root / value["snapshot_root"],
            must_exist=True,
        )
        if not root.is_dir():
            _error(
                "SANKA_EXTENSION_PATH",
                "Marketplace snapshot root must be a directory",
                path=str(root),
            )
        expected_root = self._snapshot_destination(value["identity"], value["snapshot_digest"])
        if root != expected_root:
            _error(
                "SANKA_EXTENSION_PATH",
                "Marketplace snapshot path does not match its identity and digest",
                path=str(root),
            )
        return MarketplaceRecord(
            name=value["name"],
            identity=value["identity"],
            source=value["source"],
            trusted=value["trusted"] is True,
            snapshot_digest=value["snapshot_digest"],
            resolved_commit=value["resolved_commit"],
            content_digest=value["content_digest"],
            tree_digest=value["tree_digest"],
            snapshot_root=root,
        )

    @_store_operation
    def marketplaces(self) -> tuple[MarketplaceRecord, ...]:
        self._ensure_official_marketplace()
        records, snapshots = self._marketplace_state()
        history = {
            (item["identity"], item["snapshot_digest"]): item["tree_digest"] for item in snapshots
        }
        return tuple(
            sorted(
                (self._record(item, history) for item in records),
                key=lambda item: item.name,
            )
        )

    def _ensure_official_marketplace(self) -> None:
        if not self._configure_official_marketplace or self._marketplace_path.exists():
            return
        with (
            _locked(self.user_root, self._marketplace_path),
            _locked(self.project_root, self._project_lock_path),
        ):
            if self._marketplace_path.exists():
                return
            root, digest, tree_digest, descriptor = self._snapshot_git(
                OFFICIAL_SOURCE, OFFICIAL_IDENTITY
            )
            try:
                self._load_verified_snapshot(root, descriptor, tree_digest)
                raw = {
                    "content_digest": None,
                    "identity": OFFICIAL_IDENTITY,
                    "kind": "git",
                    "name": "official",
                    "resolved_commit": digest,
                    "snapshot_digest": digest,
                    "snapshot_root": root.relative_to(self.user_root).as_posix(),
                    "source": OFFICIAL_SOURCE,
                    "tree_digest": tree_digest,
                    "trusted": True,
                }
                self._write_marketplace_state(
                    [raw],
                    [
                        {
                            "identity": OFFICIAL_IDENTITY,
                            "snapshot_digest": digest,
                            "tree_digest": tree_digest,
                        }
                    ],
                )
            finally:
                _close_descriptor(descriptor)

    def _write_marketplace_state(
        self,
        records: list[dict[str, Any]],
        snapshots: list[dict[str, str]],
    ) -> None:
        records.sort(key=_marketplace_sort_key)
        snapshots.sort(key=lambda item: (item["identity"], item["snapshot_digest"]))
        _atomic_json(
            self.user_root,
            self._marketplace_path,
            {
                "schema_version": MARKETPLACE_SCHEMA,
                "marketplaces": records,
                "snapshots": snapshots,
            },
        )

    @staticmethod
    def _remember_snapshot(
        snapshots: list[dict[str, str]],
        identity: str,
        snapshot_digest: str,
        tree_digest: str,
    ) -> None:
        existing = next(
            (
                item
                for item in snapshots
                if item["identity"] == identity and item["snapshot_digest"] == snapshot_digest
            ),
            None,
        )
        if existing is not None:
            if existing["tree_digest"] != tree_digest:
                _error(
                    "SANKA_MARKETPLACE_SNAPSHOT_INVALID",
                    "Marketplace snapshot identity has conflicting tree content",
                    identity=identity,
                    snapshot_digest=snapshot_digest,
                )
            return
        snapshots.append(
            {
                "identity": identity,
                "snapshot_digest": snapshot_digest,
                "tree_digest": tree_digest,
            }
        )

    @staticmethod
    def _load_verified_snapshot(
        root: Path,
        descriptor: int,
        expected_tree_digest: str,
    ) -> tuple[Manifest, ...]:
        _require_directory_identity(root, descriptor)
        if content_hash(_tree_records(descriptor)) != expected_tree_digest:
            _error(
                "SANKA_MARKETPLACE_SNAPSHOT_INVALID",
                "Immutable marketplace snapshot no longer matches its expected tree",
                path=str(root),
            )
        return load_marketplace(root, root_descriptor=descriptor)

    @contextmanager
    def _verified_snapshot(
        self,
        root: Path,
        expected_tree_digest: str,
    ) -> Iterator[tuple[Manifest, ...]]:
        with (
            _parent_descriptor(self.user_root, root) as (parent, name),
            _directory_at(parent, name, root) as descriptor,
        ):
            assert descriptor is not None
            yield self._load_verified_snapshot(root, descriptor, expected_tree_digest)

    def _snapshot_destination(self, identity: str, digest: str) -> Path:
        return self._confined(
            self.user_root,
            self.user_root / "snapshots" / _identity_key(identity) / digest.removeprefix("sha256:"),
        )

    def _place_snapshot(
        self,
        source: Path,
        identity: str,
        snapshot_digest: str,
        expected_tree_digest: str,
    ) -> tuple[Path, int]:
        destination = self._snapshot_destination(identity, snapshot_digest)
        with (
            _parent_descriptor(self.user_root, source) as (source_parent, source_name),
            _directory_at(source_parent, source_name, source) as source_descriptor,
        ):
            assert source_descriptor is not None
            if content_hash(_tree_records(source_descriptor)) != expected_tree_digest:
                _error(
                    "SANKA_MARKETPLACE_SNAPSHOT_INVALID",
                    "Staged marketplace snapshot changed after its digest was fixed",
                    path=str(source),
                )
            with (
                _parent_descriptor(self.user_root, destination, create=True) as (
                    destination_parent,
                    destination_name,
                ),
                _directory_at(
                    destination_parent, destination_name, destination, missing_ok=True
                ) as existing_descriptor,
            ):
                if existing_descriptor is not None:
                    if content_hash(_tree_records(existing_descriptor)) != expected_tree_digest:
                        _error(
                            "SANKA_MARKETPLACE_SNAPSHOT_INVALID",
                            "Existing immutable marketplace snapshot does not match "
                            "expected content",
                            path=str(destination),
                        )
                    shutil.rmtree(source_name, dir_fd=source_parent)
                    return destination, os.dup(existing_descriptor)
                os.replace(
                    source_name,
                    destination_name,
                    src_dir_fd=source_parent,
                    dst_dir_fd=destination_parent,
                )
                try:
                    with _directory_at(
                        destination_parent,
                        destination_name,
                        destination,
                    ) as placed_descriptor:
                        assert placed_descriptor is not None
                        placed_digest = content_hash(_tree_records(placed_descriptor))
                        retained_descriptor = os.dup(placed_descriptor)
                    if placed_digest != expected_tree_digest:
                        _close_descriptor(retained_descriptor)
                        _error(
                            "SANKA_MARKETPLACE_SNAPSHOT_INVALID",
                            "Placed immutable marketplace snapshot does not match expected content",
                            path=str(destination),
                        )
                except BaseException:
                    with suppress(OSError, ExtensionError):
                        shutil.rmtree(destination_name, dir_fd=destination_parent)
                    raise
                return destination, retained_descriptor

    def _snapshot_local(self, source: Path, identity: str) -> tuple[Path, str, str, int]:
        temporary_root = self._confined(self.user_root, self.user_root / "tmp")
        temporary_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="snapshot-", dir=temporary_root)) / "content"
        try:
            shutil.copytree(
                source,
                staging,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__"),
            )
            digest = _tree_digest(staging)
            load_marketplace(staging)
            root, descriptor = self._place_snapshot(staging, identity, digest, digest)
            return root, digest, digest, descriptor
        finally:
            with suppress(OSError, ExtensionError):
                _remove_store_tree(self.user_root, staging.parent)

    def _snapshot_git(self, source: str, identity: str) -> tuple[Path, str, str, int]:
        temporary_root = self._confined(self.user_root, self.user_root / "tmp")
        temporary_root.mkdir(parents=True, exist_ok=True)
        parent = Path(tempfile.mkdtemp(prefix="git-", dir=temporary_root))
        checkout = parent / "checkout"
        try:
            subprocess.run(
                ["git", "clone", "--quiet", "--", source, str(checkout)],
                check=True,
                capture_output=True,
                text=True,
            )
            commit = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if _COMMIT.fullmatch(commit) is None:
                _error(
                    "SANKA_MARKETPLACE_GIT_INVALID",
                    "Marketplace Git source did not resolve to an immutable commit",
                    source=source,
                )
            git_directory = checkout / ".git"
            if git_directory.is_symlink() or not git_directory.is_dir():
                _error(
                    "SANKA_MARKETPLACE_GIT_INVALID",
                    "Marketplace clone metadata is invalid",
                    source=source,
                )
            shutil.rmtree(git_directory)
            expected_tree_digest = _tree_digest(checkout)
            load_marketplace(checkout)
            root, descriptor = self._place_snapshot(
                checkout, identity, commit, expected_tree_digest
            )
            return root, commit, expected_tree_digest, descriptor
        except subprocess.CalledProcessError as error:
            raise ExtensionError(
                "SANKA_MARKETPLACE_GIT_FAILED",
                "Marketplace Git snapshot failed",
                details={"source": source, "reason": error.stderr.strip()},
            ) from error
        finally:
            with suppress(OSError, ExtensionError):
                _remove_store_tree(self.user_root, parent)

    @staticmethod
    def _marketplace_name(source: str, identity: str, name: str | None) -> str:
        chosen = (name or identity.rsplit("/", 1)[-1] or Path(source).name).strip()
        if not chosen or "/" in chosen or "\\" in chosen or chosen in {".", ".."}:
            _error(
                "SANKA_MARKETPLACE_NAME_INVALID",
                "Marketplace name must be a non-empty path-free value",
                name=chosen,
            )
        return chosen

    @_store_operation
    def add_marketplace(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        trust: bool = False,
    ) -> MarketplaceRecord:
        kind, fetch_source, identity = _canonical_source(source)
        trusted = identity == OFFICIAL_IDENTITY or trust
        if not trusted:
            _error(
                "SANKA_MARKETPLACE_TRUST_REQUIRED",
                "Third-party marketplace source requires explicit trust",
                identity=identity,
            )
        chosen_name = self._marketplace_name(fetch_source, identity, name)
        with (
            _locked(self.user_root, self._marketplace_path),
            _locked(self.project_root, self._project_lock_path),
        ):
            records, snapshots = self._marketplace_state()
            if any(item.get("name") == chosen_name for item in records):
                _error(
                    "SANKA_MARKETPLACE_EXISTS",
                    "Marketplace name is already configured",
                    name=chosen_name,
                )
            if kind == "git":
                root, digest, tree_digest, descriptor = self._snapshot_git(fetch_source, identity)
                commit, content_digest = digest, None
            else:
                root, digest, tree_digest, descriptor = self._snapshot_local(
                    Path(fetch_source), identity
                )
                commit, content_digest = None, digest
            try:
                self._load_verified_snapshot(root, descriptor, tree_digest)
                self._remember_snapshot(snapshots, identity, digest, tree_digest)
                history = {
                    (item["identity"], item["snapshot_digest"]): item["tree_digest"]
                    for item in snapshots
                }
                raw = {
                    "content_digest": content_digest,
                    "identity": identity,
                    "kind": kind,
                    "name": chosen_name,
                    "resolved_commit": commit,
                    "snapshot_digest": digest,
                    "snapshot_root": root.relative_to(self.user_root).as_posix(),
                    "source": fetch_source,
                    "tree_digest": tree_digest,
                    "trusted": trusted,
                }
                records.append(raw)
                self._write_marketplace_state(records, snapshots)
                return self._record(raw, history)
            finally:
                _close_descriptor(descriptor)

    @_store_operation
    def upgrade_marketplace(self, name: str | None = None) -> tuple[MarketplaceRecord, ...]:
        with (
            _locked(self.user_root, self._marketplace_path),
            _locked(self.project_root, self._project_lock_path),
        ):
            records, snapshots = self._marketplace_state()
            selected = [item for item in records if name is None or item.get("name") == name]
            if not selected:
                _error(
                    "SANKA_MARKETPLACE_NOT_FOUND",
                    "Marketplace is not configured",
                    name=name,
                )
            descriptors: list[int] = []
            try:
                for raw in selected:
                    kind = raw["kind"]
                    identity = raw["identity"]
                    source = raw["source"]
                    if kind == "git":
                        root, digest, tree_digest, descriptor = self._snapshot_git(source, identity)
                        raw["resolved_commit"], raw["content_digest"] = digest, None
                    elif kind == "local":
                        current_kind, current_source, current_identity = _canonical_source(source)
                        if current_kind != "local" or current_identity != identity:
                            _error(
                                "SANKA_MARKETPLACE_SOURCE_INVALID",
                                "Local marketplace source no longer matches its trusted identity",
                                source=source,
                            )
                        root, digest, tree_digest, descriptor = self._snapshot_local(
                            Path(current_source), identity
                        )
                        raw["resolved_commit"], raw["content_digest"] = None, digest
                    else:
                        _error(
                            "SANKA_EXTENSION_STATE_INVALID",
                            "Marketplace source kind is invalid",
                            name=raw.get("name"),
                        )
                    descriptors.append(descriptor)
                    self._load_verified_snapshot(root, descriptor, tree_digest)
                    self._remember_snapshot(snapshots, identity, digest, tree_digest)
                    raw["snapshot_digest"] = digest
                    raw["snapshot_root"] = root.relative_to(self.user_root).as_posix()
                    raw["tree_digest"] = tree_digest
                history = {
                    (item["identity"], item["snapshot_digest"]): item["tree_digest"]
                    for item in snapshots
                }
                upgraded = [self._record(raw, history) for raw in selected]
                self._write_marketplace_state(records, snapshots)
                return tuple(sorted(upgraded, key=lambda item: item.name))
            finally:
                _close_descriptors(reversed(descriptors))

    @_store_operation
    def remove_marketplace(self, name: str) -> MarketplaceRecord:
        with (
            _locked(self.user_root, self._marketplace_path),
            _locked(self.project_root, self._project_lock_path),
        ):
            records, snapshots = self._marketplace_state()
            raw = next((item for item in records if item.get("name") == name), None)
            if raw is None:
                _error(
                    "SANKA_MARKETPLACE_NOT_FOUND",
                    "Marketplace is not configured",
                    name=name,
                )
            if any(
                entry.marketplace_identity == raw["identity"]
                for entry in self._load_lock().values()
            ):
                _error(
                    "SANKA_MARKETPLACE_IN_USE",
                    "Marketplace is referenced by the current project lock",
                    name=name,
                )
            records.remove(raw)
            self._write_marketplace_state(records, snapshots)
            history = {
                (item["identity"], item["snapshot_digest"]): item["tree_digest"]
                for item in snapshots
            }
            return self._record(raw, history)

    def _load_installations(self) -> list[dict[str, Any]]:
        path = self._state_path(self.user_root, self._installation_path)
        payload = self._load_json(
            self.user_root,
            path,
            {"schema_version": INSTALLATION_SCHEMA, "installations": []},
        )
        values = payload.get("installations")
        if payload.get("schema_version") != INSTALLATION_SCHEMA or not isinstance(values, list):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Extension installation state is invalid",
                path=str(self._installation_path),
            )
        if any(not isinstance(item, dict) for item in values):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Extension installation records must be objects",
                path=str(self._installation_path),
            )
        expected = {
            "artifact_digest",
            "environment",
            "environment_digest",
            "id",
            "manifest_digest",
            "marketplace_identity",
            "snapshot_digest",
            "version",
            "wheels",
        }
        digests: list[str] = []
        for item in values:
            string_fields = expected - {"wheels"}
            if (
                set(item) != expected
                or any(
                    not isinstance(item.get(field), str) or not item[field]
                    for field in string_fields
                )
                or re.fullmatch(r"[0-9a-f]{64}", item["artifact_digest"]) is None
                or re.fullmatch(r"sha256:[0-9a-f]{64}", item["environment_digest"]) is None
                or re.fullmatch(r"sha256:[0-9a-f]{64}", item["manifest_digest"]) is None
                or not isinstance(item["wheels"], list)
                or not item["wheels"]
            ):
                _error(
                    "SANKA_EXTENSION_STATE_INVALID",
                    "Extension installation record is invalid",
                    path=str(self._installation_path),
                )
            environment = self.user_root / item["environment"]
            self._confined(
                self.user_root,
                environment,
                expected=self.user_root / "environments" / item["artifact_digest"],
            )
            for wheel in item["wheels"]:
                if (
                    not isinstance(wheel, dict)
                    or set(wheel) != {"path", "sha256"}
                    or not isinstance(wheel.get("path"), str)
                    or not wheel["path"]
                    or not isinstance(wheel.get("sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", wheel["sha256"]) is None
                ):
                    _error(
                        "SANKA_EXTENSION_STATE_INVALID",
                        "Extension installation wheel record is invalid",
                        path=str(self._installation_path),
                    )
                cached = self.user_root / wheel["path"]
                if (
                    cached.parent != self.user_root / "cache" / "wheels" / wheel["sha256"]
                    or cached.name in {"", ".", ".."}
                    or not cached.name.endswith(".whl")
                ):
                    _error(
                        "SANKA_EXTENSION_PATH",
                        "Extension wheel does not have its exact cache placement",
                        path=str(cached),
                    )
                self._confined(self.user_root, cached, expected=cached)
            digests.append(item["artifact_digest"])
        if len(digests) != len(set(digests)):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Extension installation records contain duplicate artifacts",
                path=str(self._installation_path),
            )
        return values

    def _load_disabled(self) -> set[str]:
        path = self._state_path(self.user_root, self._disabled_path)
        payload = self._load_json(
            self.user_root,
            path,
            {"schema_version": "sanka-extension-disabled/v1", "extensions": []},
        )
        values = payload.get("extensions")
        if (
            payload.get("schema_version") != "sanka-extension-disabled/v1"
            or not isinstance(values, list)
            or any(not isinstance(item, str) for item in values)
            or len(values) != len(set(values))
        ):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Disabled extension state is invalid",
                path=str(self._disabled_path),
            )
        return set(values)

    def _write_installations(self, values: list[dict[str, Any]]) -> None:
        values = list(values)
        values.sort(key=_installation_sort_key)
        _atomic_json(
            self.user_root,
            self._installation_path,
            {
                "schema_version": INSTALLATION_SCHEMA,
                "installations": values,
            },
        )

    def _write_disabled(self, values: set[str]) -> None:
        _atomic_json(
            self.user_root,
            self._disabled_path,
            {"schema_version": "sanka-extension-disabled/v1", "extensions": sorted(values)},
        )

    def _load_lock(self) -> dict[str, LockEntry]:
        path = self._state_path(self.project_root, self._project_lock_path)
        payload = self._load_json(
            self.project_root,
            path,
            {"schema_version": LOCK_SCHEMA, "extensions": []},
        )
        values = payload.get("extensions")
        if payload.get("schema_version") != LOCK_SCHEMA or not isinstance(values, list):
            _error(
                "SANKA_EXTENSION_LOCK_INVALID",
                "Project extension lock is invalid",
                path=str(path),
            )
        entries: dict[str, LockEntry] = {}
        expected = set(LockEntry.__dataclass_fields__)
        for value in values:
            if not isinstance(value, dict) or set(value) != expected:
                _error(
                    "SANKA_EXTENSION_LOCK_INVALID",
                    "Project extension lock entry is invalid",
                    path=str(path),
                )
            string_fields = expected - {
                "commands",
                "enabled",
                "entry_point",
                "executable",
                "providers",
            }
            raw_providers = value["providers"]
            if (
                any(
                    not isinstance(value[field], str) or not value[field] for field in string_fields
                )
                or not isinstance(value["commands"], list)
                or any(
                    not isinstance(command, str) or command not in LIFECYCLE_COMMANDS
                    for command in value["commands"]
                )
                or len(value["commands"]) != len(set(value["commands"]))
                or value["commands"] != sorted(value["commands"])
                or not isinstance(value["enabled"], bool)
                or not isinstance(raw_providers, list)
                or any(
                    not isinstance(provider, dict)
                    or set(provider) != {"name", "roles"}
                    or not isinstance(provider["name"], str)
                    or IDENTIFIER.fullmatch(provider["name"]) is None
                    or not isinstance(provider["roles"], list)
                    or not provider["roles"]
                    or any(not isinstance(role, str) for role in provider["roles"])
                    or len(provider["roles"]) != len(set(provider["roles"]))
                    or any(role not in {"source", "destination"} for role in provider["roles"])
                    for provider in raw_providers
                )
            ):
                _error(
                    "SANKA_EXTENSION_LOCK_INVALID",
                    "Project extension lock fields are invalid",
                    path=str(path),
                )
            providers = tuple(
                SystemSupport(
                    provider["name"],
                    tuple(role for role in ("source", "destination") if role in provider["roles"]),
                )
                for provider in raw_providers
            )
            entry = LockEntry(
                **{
                    **value,
                    "commands": tuple(value["commands"]),
                    "providers": providers,
                }
            )
            if (
                not re.fullmatch(r"sha256:[0-9a-f]{64}", entry.manifest_digest)
                or not re.fullmatch(r"[0-9a-f]{64}", entry.artifact_digest)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", entry.configuration_digest)
                or not (
                    re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", entry.snapshot_digest)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", entry.snapshot_digest)
                )
                or entry.kind not in {"migration", "connector"}
                or (
                    entry.kind == "migration"
                    and (
                        entry.protocol_version != "sanka-extension/v1"
                        or not isinstance(entry.executable, str)
                        or not entry.executable
                        or "/" in entry.executable
                        or entry.entry_point is not None
                        or entry.providers
                        or not entry.commands
                    )
                )
                or (
                    entry.kind == "connector"
                    and (
                        entry.protocol_version != "sanka-connector/v1"
                        or entry.executable is not None
                        or not isinstance(entry.entry_point, str)
                        or not entry.entry_point
                        or "/" in entry.entry_point
                        or not entry.providers
                        or entry.commands
                        or len({provider.name for provider in entry.providers})
                        != len(entry.providers)
                    )
                )
            ):
                _error(
                    "SANKA_EXTENSION_LOCK_INVALID",
                    "Project extension lock identity fields are invalid",
                    extension_id=entry.id,
                )
            if entry.id in entries:
                _error(
                    "SANKA_EXTENSION_LOCK_INVALID",
                    "Project extension lock contains duplicate ids",
                    extension_id=entry.id,
                )
            entries[entry.id] = entry
        return entries

    def _write_lock(self, entries: Mapping[str, LockEntry]) -> None:
        path = self._state_path(self.project_root, self._project_lock_path)
        _atomic_json(
            self.project_root,
            path,
            {
                "schema_version": LOCK_SCHEMA,
                "extensions": [entries[key].to_dict() for key in sorted(entries)],
            },
        )

    def _catalog(self) -> list[tuple[MarketplaceRecord, Manifest]]:
        values: list[tuple[MarketplaceRecord, Manifest]] = []
        for record in self.marketplaces():
            if not record.trusted:
                _error(
                    "SANKA_MARKETPLACE_TRUST_REQUIRED",
                    "Configured marketplace is not trusted",
                    identity=record.identity,
                )
            with self._verified_snapshot(record.snapshot_root, record.tree_digest) as manifests:
                values.extend((record, manifest) for manifest in manifests)
        return values

    @_store_operation
    def list_extensions(self) -> tuple[ExtensionRecord, ...]:
        installations = self._load_installations()
        disabled = self._load_disabled()
        locks = self._load_lock()
        records: list[ExtensionRecord] = []
        for marketplace, manifest in self._catalog():
            statuses = {"available"}
            lock = locks.get(manifest.id)
            scoped_lock = (
                lock
                if lock is not None and lock.marketplace_identity == marketplace.identity
                else None
            )
            exact_lock = (
                scoped_lock
                if scoped_lock is not None
                and scoped_lock.version == manifest.version
                and scoped_lock.snapshot_digest == marketplace.snapshot_digest
                and scoped_lock.manifest_digest == manifest.digest
                else None
            )
            installed = any(
                item.get("id") == manifest.id
                and item.get("marketplace_identity") == marketplace.identity
                and item.get("snapshot_digest") == marketplace.snapshot_digest
                and item.get("manifest_digest") == manifest.digest
                and item.get("version") == manifest.version
                for item in installations
            )
            if installed and manifest.id not in disabled:
                statuses.add("installed")
            if exact_lock and exact_lock.enabled:
                statuses.add("locked")
            if scoped_lock and scoped_lock.enabled and exact_lock is None:
                statuses.add("update_available")
            if not _compatible(manifest.runtime_sanka_cli, __version__):
                statuses.add("incompatible")
            if manifest.id in disabled:
                statuses.add("disabled")
            records.append(
                ExtensionRecord(
                    id=manifest.id,
                    version=manifest.version,
                    marketplace=marketplace.name,
                    marketplace_identity=marketplace.identity,
                    manifest_digest=manifest.digest,
                    kind=manifest.kind,
                    providers=manifest.providers,
                    targets=manifest.targets,
                    status=tuple(item for item in STATUS_ORDER if item in statuses),
                    wheels=manifest.wheels,
                )
            )
        return tuple(sorted(records, key=lambda item: (item.id, item.version, item.marketplace)))

    @_store_operation
    def recommendations(self, fingerprint: Fingerprint) -> tuple[Recommendation, ...]:
        """Match only manifests read through the verified snapshot boundary."""
        catalog = self._catalog()
        records = {(item.id, item.marketplace_identity): item for item in self.list_extensions()}
        marketplaces = {item.identity: item for item in self.marketplaces()}
        locks = self._load_lock()
        installations = self._load_installations()
        disabled = self._load_disabled()
        recommendations: list[Recommendation] = []
        selected: list[tuple[MarketplaceRecord, Manifest, LockEntry | None]] = []
        covered_locks: set[str] = set()
        for marketplace, current in catalog:
            lock = locks.get(current.id)
            if lock is not None and lock.marketplace_identity == marketplace.identity:
                selected.append((marketplace, self._manifest_for_lock(lock), lock))
                covered_locks.add(lock.id)
            else:
                selected.append((marketplace, current, None))
        for lock in locks.values():
            if lock.id in covered_locks or lock.marketplace_identity not in marketplaces:
                continue
            selected.append(
                (marketplaces[lock.marketplace_identity], self._manifest_for_lock(lock), lock)
            )
        for marketplace, manifest, lock in selected:
            matched = recommend(
                fingerprint,
                (replace(manifest, marketplace=marketplace.name),),
                {},
            )
            if not matched:
                continue
            if lock is None:
                status = records[(manifest.id, marketplace.identity)].status
                snapshot_digest = marketplace.snapshot_digest
            else:
                statuses = {"available", "locked"}
                if any(
                    item.get("id") == lock.id
                    and item.get("marketplace_identity") == lock.marketplace_identity
                    and item.get("snapshot_digest") == lock.snapshot_digest
                    and item.get("manifest_digest") == lock.manifest_digest
                    and item.get("version") == lock.version
                    for item in installations
                ):
                    statuses.add("installed")
                available_current = next(
                    (
                        item
                        for source, item in catalog
                        if source.identity == lock.marketplace_identity and item.id == lock.id
                    ),
                    None,
                )
                if available_current is not None and (
                    available_current.version != lock.version
                    or available_current.digest != lock.manifest_digest
                    or marketplace.snapshot_digest != lock.snapshot_digest
                ):
                    statuses.add("update_available")
                if not _compatible(manifest.runtime_sanka_cli, __version__):
                    statuses.add("incompatible")
                if lock.id in disabled or not lock.enabled:
                    statuses.add("disabled")
                status = tuple(item for item in STATUS_ORDER if item in statuses)
                snapshot_digest = lock.snapshot_digest
            recommendations.append(
                replace(
                    matched[0],
                    commands=manifest.commands,
                    marketplace_identity=marketplace.identity,
                    snapshot_digest=snapshot_digest,
                    manifest_digest=manifest.digest,
                    status=status,
                )
            )
        return tuple(
            sorted(
                recommendations,
                key=lambda item: (item.id, item.version, item.marketplace_identity),
            )
        )

    def _select(
        self, extension_id: str, marketplace: str | None
    ) -> tuple[MarketplaceRecord, Manifest]:
        selected = [
            item
            for item in self._catalog()
            if item[1].id == extension_id
            and (
                marketplace is None
                or item[0].name == marketplace
                or item[0].identity == marketplace
            )
        ]
        if not selected:
            _error(
                "SANKA_EXTENSION_NOT_FOUND",
                "Extension is not available from the selected marketplace",
                extension_id=extension_id,
                marketplace=marketplace,
            )
        if len(selected) > 1:
            _error(
                "SANKA_EXTENSION_AMBIGUOUS",
                "Extension id is available from more than one marketplace",
                extension_id=extension_id,
                marketplaces=sorted(item[0].name for item in selected),
            )
        return selected[0]

    def _wheel_cache_path(self, wheel: Wheel) -> Path:
        return self._confined(
            self.user_root,
            self.user_root / "cache" / "wheels" / wheel.sha256 / wheel.name,
        )

    @staticmethod
    def _select_compatible_wheels(manifest: Manifest) -> tuple[Wheel, ...]:
        tag_ranks = {tag: rank for rank, tag in enumerate(sys_tags())}
        candidates: dict[str, list[tuple[int, str, Wheel]]] = {}
        versions: dict[str, set[str]] = {}
        for wheel in manifest.wheels:
            identity = _wheel_identity(wheel.name)
            try:
                _distribution, _version, _build, tags = parse_wheel_filename(wheel.name)
            except InvalidWheelFilename:
                identity = None
                tags = frozenset()
            if identity is None:
                _error(
                    "SANKA_EXTENSION_ARTIFACT_INVALID",
                    "Only valid Python wheels may be installed",
                    artifact=wheel.name,
                )
            versions.setdefault(identity[0], set()).add(identity[1])
            compatible = [tag_ranks[tag] for tag in tags if tag in tag_ranks]
            if compatible:
                candidates.setdefault(identity[0], []).append((min(compatible), wheel.name, wheel))
        if any(len(items) != 1 for items in versions.values()):
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Extension wheel variants must pin one version per distribution",
                extension_id=manifest.id,
            )
        selected = tuple(
            min(items, key=lambda item: (item[0], item[1]))[2]
            for _distribution, items in sorted(candidates.items())
        )
        primary = _normalized_distribution(manifest.distribution)
        if primary not in candidates:
            _error(
                "SANKA_EXTENSION_PLATFORM_UNSUPPORTED",
                "Extension has no wheel compatible with this Python runtime and platform",
                extension_id=manifest.id,
                distribution=manifest.distribution,
            )
        return selected

    def _cache_wheel(self, wheel: Wheel) -> Path:
        if not wheel.name.endswith(".whl") or _wheel_identity(wheel.name) is None:
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Only valid Python wheels may be installed",
                artifact=wheel.name,
            )
        destination = self._wheel_cache_path(wheel)
        temporary = ""
        with _parent_descriptor(self.user_root, destination, create=True) as (parent, name):
            with _regular_at(parent, name, destination, missing_ok=True) as existing:
                if existing is not None:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                        digest.update(chunk)
                    if digest.hexdigest() != wheel.sha256:
                        _error(
                            "SANKA_EXTENSION_HASH_MISMATCH",
                            "Cached extension wheel does not match its declared SHA-256",
                            artifact=wheel.name,
                        )
                    return destination
            temporary = f".{name}.{secrets.token_hex(8)}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
            try:
                try:
                    with (
                        os.fdopen(descriptor, "wb") as output,
                        urlopen(wheel.url, timeout=30) as response,
                    ):
                        declared_size = (
                            response.headers.get("Content-Length")
                            if hasattr(response, "headers")
                            else None
                        )
                        if declared_size is not None and int(declared_size) > MAX_WHEEL_BYTES:
                            _error(
                                "SANKA_EXTENSION_ARTIFACT_TOO_LARGE",
                                "Extension wheel exceeds the download limit",
                                artifact=wheel.name,
                                limit=MAX_WHEEL_BYTES,
                            )
                        digest = hashlib.sha256()
                        size = 0
                        while chunk := response.read(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_WHEEL_BYTES:
                                _error(
                                    "SANKA_EXTENSION_ARTIFACT_TOO_LARGE",
                                    "Extension wheel exceeds the download limit",
                                    artifact=wheel.name,
                                    limit=MAX_WHEEL_BYTES,
                                )
                            digest.update(chunk)
                            output.write(chunk)
                except ExtensionError:
                    raise
                except (OSError, ValueError) as error:
                    raise ExtensionError(
                        "SANKA_EXTENSION_NOT_CACHED",
                        "Exact extension artifact is unavailable and not cached",
                        details={"artifact": wheel.name, "reason": str(error)},
                    ) from error
                if digest.hexdigest() != wheel.sha256:
                    _error(
                        "SANKA_EXTENSION_HASH_MISMATCH",
                        "Downloaded extension wheel does not match its declared SHA-256",
                        artifact=wheel.name,
                    )
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                temporary = ""
                return destination
            finally:
                if temporary:
                    with suppress(OSError):
                        os.unlink(temporary, dir_fd=parent)

    @staticmethod
    def _zip_member(archive: zipfile.ZipFile, suffix: str, artifact: str) -> bytes:
        matches = [info for info in archive.infolist() if info.filename.endswith(suffix)]
        if len(matches) != 1:
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Extension wheel metadata is incomplete or ambiguous",
                artifact=artifact,
                member=suffix,
            )
        if matches[0].file_size > MAX_WHEEL_METADATA_BYTES:
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Extension wheel metadata exceeds the read limit",
                artifact=artifact,
                member=suffix,
                limit=MAX_WHEEL_METADATA_BYTES,
            )
        return archive.read(matches[0])

    @staticmethod
    def _wheel_install_path(info: zipfile.ZipInfo, artifact: str) -> PurePosixPath:
        parts = PurePosixPath(info.filename).parts
        identity = _wheel_identity(artifact)
        if identity is None:
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Only valid Python wheels may be installed",
                artifact=artifact,
            )
        data_root = f"{identity[0]}-{identity[1]}.data"
        if parts and parts[0].endswith(".data"):
            if parts[0] != data_root:
                _error(
                    "SANKA_EXTENSION_ARTIFACT_INVALID",
                    "Extension wheel .data root does not match its wheel identity",
                    artifact=artifact,
                    root=parts[0],
                )
            if len(parts) == 1 and info.is_dir():
                return PurePosixPath()
            scheme = parts[1] if len(parts) > 1 else ""
            if scheme != "purelib" or (len(parts) < 3 and not info.is_dir()):
                _error(
                    "SANKA_EXTENSION_ARTIFACT_INVALID",
                    "Extension wheel uses an unsupported .data installation scheme",
                    artifact=artifact,
                    scheme=scheme,
                )
            return PurePosixPath(*parts[2:])
        return PurePosixPath(*parts)

    def _inspect_wheel(
        self,
        path: Path,
        wheel: Wheel,
        manifest: Manifest,
    ) -> tuple[tuple[str, Requirement], ...]:
        identity = _wheel_identity(wheel.name)
        if identity is None:
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Only valid Python wheels may be installed",
                artifact=wheel.name,
            )
        try:
            with (
                _store_file(self.user_root, path) as source,
                zipfile.ZipFile(cast(Any, source)) as archive,
            ):
                members = archive.infolist()
                normalized_members: set[str] = set()
                unsafe = len(members) > MAX_WHEEL_MEMBERS
                for info in members:
                    raw = info.filename
                    pure = PurePosixPath(raw)
                    normalized = pure.as_posix().rstrip("/")
                    mode_type = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
                    safe_type = (info.is_dir() and mode_type in {0, stat.S_IFDIR}) or (
                        not info.is_dir() and mode_type in {0, stat.S_IFREG}
                    )
                    if (
                        not raw
                        or "\\" in raw
                        or info.flag_bits & 0x1
                        or pure.is_absolute()
                        or ".." in pure.parts
                        or not normalized
                        or normalized in normalized_members
                        or not safe_type
                    ):
                        unsafe = True
                    normalized_members.add(normalized)
                if unsafe or sum(info.file_size for info in members) > MAX_WHEEL_UNCOMPRESSED_BYTES:
                    _error(
                        "SANKA_EXTENSION_ARTIFACT_INVALID",
                        "Extension wheel members are unsafe",
                        artifact=wheel.name,
                    )
                for info in members:
                    self._wheel_install_path(info, wheel.name)
                package = BytesParser().parsebytes(
                    self._zip_member(archive, ".dist-info/METADATA", wheel.name)
                )
                wheel_metadata = BytesParser().parsebytes(
                    self._zip_member(archive, ".dist-info/WHEEL", wheel.name)
                )
                entry_point_members = [
                    info
                    for info in archive.infolist()
                    if info.filename.endswith(".dist-info/entry_points.txt")
                ]
                if len(entry_point_members) > 1:
                    _error(
                        "SANKA_EXTENSION_ARTIFACT_INVALID",
                        "Extension wheel entry-point metadata is ambiguous",
                        artifact=wheel.name,
                    )
                if (
                    entry_point_members
                    and entry_point_members[0].file_size > MAX_WHEEL_METADATA_BYTES
                ):
                    _error(
                        "SANKA_EXTENSION_ARTIFACT_INVALID",
                        "Extension wheel entry-point metadata exceeds the read limit",
                        artifact=wheel.name,
                        limit=MAX_WHEEL_METADATA_BYTES,
                    )
                entry_points = (
                    archive.read(entry_point_members[0]).decode("utf-8")
                    if entry_point_members
                    else ""
                )
        except ExtensionError:
            raise
        except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as error:
            raise ExtensionError(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Extension wheel metadata is invalid",
                details={"artifact": wheel.name, "reason": str(error)},
            ) from error
        if (
            package.get_all("Name", []) != [package.get("Name")]
            or package.get_all("Version", []) != [package.get("Version")]
            or _normalized_distribution(package.get("Name", "")) != identity[0]
            or package.get("Version") != identity[1]
        ):
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Extension wheel package identity does not match its filename",
                artifact=wheel.name,
            )
        declared_tags = wheel_metadata.get_all("Tag", [])
        try:
            _distribution, _version, _build, filename_tags = parse_wheel_filename(wheel.name)
            metadata_tags = frozenset(
                tag for declared_tag in declared_tags for tag in parse_tag(declared_tag)
            )
        except (InvalidWheelFilename, ValueError):
            metadata_tags = frozenset()
            filename_tags = frozenset()
        if (
            wheel_metadata.get_all("Wheel-Version", []) != ["1.0"]
            or wheel_metadata.get_all("Root-Is-Purelib", []) not in [["true"], ["false"]]
            or not declared_tags
            or metadata_tags != filename_tags
        ):
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Extension WHEEL metadata must declare matching wheel tags",
                artifact=wheel.name,
            )
        requirements: list[tuple[str, Requirement]] = []
        for raw_requirement in package.get_all("Requires-Dist", []):
            try:
                requirement = Requirement(raw_requirement)
            except InvalidRequirement:
                _error(
                    "SANKA_EXTENSION_ARTIFACT_INVALID",
                    "Extension wheel requirement syntax is invalid",
                    artifact=wheel.name,
                    requirement=raw_requirement,
                )
            if requirement.url is not None:
                _error(
                    "SANKA_EXTENSION_ARTIFACT_INVALID",
                    "Extension wheel requirements cannot use direct URLs",
                    artifact=wheel.name,
                    requirement=raw_requirement,
                )
            requirements.append((raw_requirement, requirement))
        if identity[0] == _normalized_distribution(manifest.distribution):
            parser = configparser.ConfigParser(interpolation=None)
            identity_name = manifest.executable or manifest.entry_point
            assert identity_name is not None
            try:
                parser.read_string(entry_points)
                if manifest.kind == "migration":
                    assert manifest.executable is not None
                    target = parser["console_scripts"][manifest.executable]
                else:
                    assert manifest.entry_point is not None
                    target = parser[ENTRY_POINT_GROUP][manifest.entry_point]
            except (configparser.Error, KeyError) as error:
                raise ExtensionError(
                    "SANKA_EXTENSION_ARTIFACT_INVALID",
                    "Extension wheel does not declare its exact entry point",
                    details={"artifact": wheel.name, "entry_point": identity_name},
                ) from error
            if _ENTRY_POINT.fullmatch(target.strip()) is None:
                _error(
                    "SANKA_EXTENSION_ARTIFACT_INVALID",
                    "Extension entry point is invalid",
                    artifact=wheel.name,
                    entry_point=identity_name,
                )
        return tuple(requirements)

    @staticmethod
    def _validate_dependency_closure(
        manifest: Manifest,
        wheels: tuple[Wheel, ...],
        requirements: Mapping[str, tuple[tuple[str, Requirement], ...]],
    ) -> None:
        declared = {
            identity[0]: identity[1]
            for wheel in wheels
            if (identity := _wheel_identity(wheel.name)) is not None
        }
        available = {
            identity[0]
            for wheel in manifest.wheels
            if (identity := _wheel_identity(wheel.name)) is not None
        }
        artifacts = {
            identity[0]: wheel.name
            for wheel in wheels
            if (identity := _wheel_identity(wheel.name)) is not None
        }
        root = _normalized_distribution(manifest.distribution)
        requested_extras: dict[str, set[str]] = {root: set()}
        pending = [root]
        processed: set[tuple[str, tuple[str, ...]]] = set()
        while pending:
            distribution = pending.pop()
            extras = requested_extras[distribution]
            state = distribution, tuple(sorted(extras))
            if state in processed:
                continue
            processed.add(state)
            for raw_requirement, requirement in requirements.get(distribution, ()):
                try:
                    active = requirement.marker is None or any(
                        requirement.marker.evaluate({"extra": extra}) for extra in (extras or {""})
                    )
                except (KeyError, ValueError) as error:
                    raise ExtensionError(
                        "SANKA_EXTENSION_ARTIFACT_INVALID",
                        "Extension wheel requirement marker is invalid",
                        details={
                            "artifact": artifacts[distribution],
                            "requirement": raw_requirement,
                        },
                    ) from error
                if not active:
                    continue
                dependency = _normalized_distribution(requirement.name)
                if dependency not in declared:
                    if dependency in available:
                        _error(
                            "SANKA_EXTENSION_PLATFORM_UNSUPPORTED",
                            "Extension dependency has no compatible wheel for this platform",
                            extension_id=manifest.id,
                            distribution=requirement.name,
                        )
                    _error(
                        "SANKA_EXTENSION_UNDECLARED_REQUIREMENT",
                        "Extension wheel requires an undeclared distribution",
                        artifact=artifacts[distribution],
                        requirement=raw_requirement,
                    )
                if declared[dependency] not in requirement.specifier:
                    _error(
                        "SANKA_EXTENSION_REQUIREMENT_UNSATISFIED",
                        "Declared extension wheel version does not satisfy Requires-Dist",
                        artifact=artifacts[distribution],
                        requirement=raw_requirement,
                        declared_version=declared[dependency],
                    )
                first_request = dependency not in requested_extras
                previous = requested_extras.setdefault(dependency, set())
                updated = previous | set(requirement.extras)
                if first_request or updated != previous:
                    requested_extras[dependency] = updated
                    pending.append(dependency)

    def _wheel_entry_point(
        self,
        wheels: tuple[tuple[Path, str], ...],
        executable: str,
    ) -> str:
        generated: list[tuple[str, str, str]] = []
        for path, expected_sha256 in wheels:
            if _store_file_sha256(self.user_root, path) != expected_sha256:
                _error(
                    "SANKA_EXTENSION_HASH_MISMATCH",
                    "Cached locked extension wheel has changed",
                    artifact=path.name,
                )
            with (
                _store_file(self.user_root, path) as source,
                zipfile.ZipFile(cast(Any, source)) as archive,
            ):
                members = [
                    info
                    for info in archive.infolist()
                    if info.filename.endswith(".dist-info/entry_points.txt")
                ]
                if not members:
                    continue
                parser = configparser.ConfigParser(interpolation=None)
                try:
                    parser.read_string(archive.read(members[0]).decode("utf-8"))
                except (configparser.Error, UnicodeDecodeError) as error:
                    raise ExtensionError(
                        "SANKA_EXTENSION_ARTIFACT_INVALID",
                        "Extension wheel entry-point metadata is invalid",
                        details={"artifact": path.name, "reason": str(error)},
                    ) from error
                for group in ("console_scripts", "gui_scripts"):
                    if parser.has_section(group):
                        generated.extend(
                            (group, name, target.strip()) for name, target in parser.items(group)
                        )
        selected = [
            target
            for group, name, target in generated
            if group == "console_scripts" and name == executable
        ]
        if len(generated) != 1 or len(selected) != 1 or _ENTRY_POINT.fullmatch(selected[0]) is None:
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Extension wheel set must define only its exact executable entry point",
                executable=executable,
                generated_scripts=[f"{group}:{name}" for group, name, _target in generated],
            )
        return selected[0]

    def _wheel_connector_entry_points(
        self,
        wheels: tuple[tuple[Path, str], ...],
        providers: tuple[SystemSupport, ...],
    ) -> dict[str, str]:
        connector_entries: dict[str, str] = {}
        scripts: list[str] = []
        for path, expected_sha256 in wheels:
            if _store_file_sha256(self.user_root, path) != expected_sha256:
                _error(
                    "SANKA_EXTENSION_HASH_MISMATCH",
                    "Cached locked extension wheel has changed",
                    artifact=path.name,
                )
            with (
                _store_file(self.user_root, path) as source,
                zipfile.ZipFile(cast(Any, source)) as archive,
            ):
                members = [
                    info
                    for info in archive.infolist()
                    if info.filename.endswith(".dist-info/entry_points.txt")
                ]
                if not members:
                    continue
                parser = configparser.ConfigParser(interpolation=None)
                try:
                    parser.read_string(archive.read(members[0]).decode("utf-8"))
                except (configparser.Error, UnicodeDecodeError) as error:
                    raise ExtensionError(
                        "SANKA_EXTENSION_ARTIFACT_INVALID",
                        "Extension wheel entry-point metadata is invalid",
                        details={"artifact": path.name},
                    ) from error
                for group in ("console_scripts", "gui_scripts"):
                    if parser.has_section(group):
                        scripts.extend(f"{group}:{name}" for name in parser[group])
                if parser.has_section(ENTRY_POINT_GROUP):
                    for name, target in parser.items(ENTRY_POINT_GROUP):
                        if name in connector_entries:
                            _error(
                                "SANKA_EXTENSION_ARTIFACT_INVALID",
                                "Connector provider entry point is declared more than once",
                                provider=name,
                            )
                        connector_entries[name] = target.strip()
        expected = {provider.name for provider in providers}
        if (
            scripts
            or set(connector_entries) != expected
            or any(_ENTRY_POINT.fullmatch(target) is None for target in connector_entries.values())
        ):
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Connector wheel set must define only its declared providers",
                providers=sorted(connector_entries),
                generated_scripts=sorted(scripts),
            )
        return connector_entries

    def _wheel_import_records(
        self,
        wheels: tuple[tuple[Path, str], ...],
    ) -> dict[str, tuple[int, str]]:
        records: dict[str, tuple[int, str]] = {}
        installed_files: set[str] = set()
        required_directories: set[str] = set()
        for path, expected_sha256 in wheels:
            if _store_file_sha256(self.user_root, path) != expected_sha256:
                _error(
                    "SANKA_EXTENSION_HASH_MISMATCH",
                    "Cached locked extension wheel has changed",
                    artifact=path.name,
                )
            with (
                _store_file(self.user_root, path) as source,
                zipfile.ZipFile(cast(Any, source)) as archive,
            ):
                for info in archive.infolist():
                    relative = self._wheel_install_path(info, path.name)
                    name = relative.as_posix()
                    if info.is_dir() and name == ".":
                        continue
                    parents = {
                        parent.as_posix()
                        for parent in relative.parents
                        if parent != PurePosixPath(".")
                    }
                    if not info.is_dir() and name in installed_files:
                        _error(
                            "SANKA_EXTENSION_ARTIFACT_INVALID",
                            "Extension wheels install duplicate import paths",
                            path=name,
                        )
                    directories = parents | ({name} if info.is_dir() else set())
                    conflict = (
                        name
                        if not info.is_dir() and name in required_directories
                        else next(
                            (
                                directory
                                for directory in sorted(directories)
                                if directory in installed_files
                            ),
                            None,
                        )
                    )
                    if conflict is not None:
                        _error(
                            "SANKA_EXTENSION_ARTIFACT_INVALID",
                            "Extension wheels require one import path as both a file and directory",
                            path=conflict,
                        )
                    required_directories.update(directories)
                    if info.is_dir():
                        continue
                    installed_files.add(name)
                    if any(part.endswith(".dist-info") for part in relative.parts) and (
                        relative.name == "RECORD"
                    ):
                        continue
                    digest = hashlib.sha256()
                    with archive.open(info) as member:
                        for chunk in iter(lambda: member.read(1024 * 1024), b""):
                            digest.update(chunk)
                    records[name] = (info.file_size, digest.hexdigest())
        return records

    @staticmethod
    def _site_packages_relative(platform: str | None = None) -> Path:
        if (platform or os.name) == "nt":
            return Path("Lib") / "site-packages"
        return (
            Path("lib")
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )

    def _verify_environment_artifacts(
        self,
        root: Path,
        executable: str | None,
        wheels: tuple[tuple[Path, str], ...],
        *,
        providers: tuple[SystemSupport, ...] = (),
    ) -> None:
        entry_point = self._wheel_entry_point(wheels, executable) if executable else None
        if executable is None:
            self._wheel_connector_entry_points(wheels, providers)
        expected_imports = self._wheel_import_records(wheels)
        with (
            _parent_descriptor(self.user_root, root) as (parent, name),
            _directory_at(parent, name, root) as descriptor,
        ):
            assert descriptor is not None
            tree = _tree_records(
                descriptor,
                error_code="SANKA_EXTENSION_PATH",
                subject="Extension environment",
                allowed_symlinks=self._environment_symlinks(),
            )
        prefix = self._site_packages_relative().as_posix() + "/"
        actual_imports = {
            str(record["path"])[len(prefix) :]: (
                cast(int, record["size"]),
                cast(str, record["sha256"]),
            )
            for record in tree
            if record.get("type") == "file"
            and str(record.get("path", "")).startswith(prefix)
            and not (
                any(
                    part.endswith(".dist-info")
                    for part in PurePosixPath(str(record["path"])[len(prefix) :]).parts
                )
                and PurePosixPath(str(record["path"])).name
                in {"INSTALLER", "RECORD", "REQUESTED", "direct_url.json"}
            )
        }
        script_matches = True
        if executable is not None:
            assert entry_point is not None
            binary_name = "Scripts" if os.name == "nt" else "bin"
            binary_path = f"{binary_name}/{executable}"
            binary = next(
                (record for record in tree if record.get("path") == binary_path),
                None,
            )
            expected_script = _console_script(root / binary_name / "python", entry_point)
            script_matches = (
                isinstance(binary, dict)
                and binary.get("type") == "file"
                and binary.get("size") == len(expected_script)
                and binary.get("sha256") == hashlib.sha256(expected_script).hexdigest()
            )
        if actual_imports != expected_imports or not script_matches:
            _error(
                "SANKA_EXTENSION_HASH_MISMATCH",
                "Installed extension executable or importable bytes differ from verified wheels",
            )
        if os.name != "nt":
            symlinks = {
                record.get("path"): record for record in tree if record.get("type") == "symlink"
            }
            if any(
                name not in symlinks or symlinks[name].get("target") != str(target)
                for name, target in self._environment_symlinks().items()
            ):
                _error(
                    "SANKA_EXTENSION_HASH_MISMATCH",
                    "Installed extension Python launchers differ from the sealed interpreter",
                )

    def _materialize_environment(
        self,
        artifact_digest: str,
        executable: str | None,
        wheels: tuple[tuple[Path, str], ...],
        *,
        expected_digest: str | None = None,
        providers: tuple[SystemSupport, ...] = (),
    ) -> Path:
        root = self._confined(
            self.user_root,
            self.user_root / "environments" / artifact_digest,
        )
        entry_point = self._wheel_entry_point(wheels, executable) if executable else None
        if executable is None:
            self._wheel_connector_entry_points(wheels, providers)
        binary_name = "Scripts" if os.name == "nt" else "bin"
        binary = root / binary_name / executable if executable else None
        temporary = ""
        with _parent_descriptor(self.user_root, root, create=True) as (
            environments,
            environment_name,
        ):
            _require_directory_identity(root.parent, environments)
            replace_existing = False
            with _directory_at(
                environments,
                environment_name,
                root,
                missing_ok=True,
            ) as existing:
                if existing is not None:
                    try:
                        digest = content_hash(
                            _tree_records(
                                existing,
                                error_code="SANKA_EXTENSION_PATH",
                                subject="Extension environment",
                                allowed_symlinks=self._environment_symlinks(),
                            )
                        )
                    except ExtensionError as error:
                        if error.code != "SANKA_EXTENSION_PATH" or expected_digest is not None:
                            raise
                    else:
                        if digest == expected_digest:
                            try:
                                self._verify_environment_artifacts(
                                    root, executable, wheels, providers=providers
                                )
                            except ExtensionError as error:
                                if error.code != "SANKA_EXTENSION_HASH_MISMATCH":
                                    raise
                            else:
                                return root
                    replace_existing = True
            if replace_existing:
                shutil.rmtree(environment_name, dir_fd=environments)
            for _attempt in range(10):
                temporary = f"environment-{secrets.token_hex(8)}"
                try:
                    os.mkdir(temporary, mode=0o700, dir_fd=environments)
                    break
                except FileExistsError:
                    continue
            else:
                raise FileExistsError("could not allocate extension environment temporary")
            allowed = {
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "PATH",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "TMPDIR",
            }
            environment = {key: value for key, value in os.environ.items() if key in allowed}
            environment.update(
                {
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    "PYTHONNOUSERSITE": "1",
                }
            )
            try:
                if executable is None:
                    _create_venv(
                        environments,
                        temporary,
                        environment=environment,
                        cwd=self.user_root,
                        system_site_packages=False,
                    )
                else:
                    _create_venv(
                        environments,
                        temporary,
                        environment=environment,
                        cwd=self.user_root,
                    )
                _require_directory_identity(root.parent, environments)
                with _directory_at(
                    environments,
                    temporary,
                    root.parent / temporary,
                ) as temporary_descriptor:
                    assert temporary_descriptor is not None
                    _normalize_venv_launchers(temporary_descriptor)
                    _run_venv_python(
                        temporary_descriptor,
                        ["-I", "-m", "ensurepip"],
                        environment=environment,
                        cwd=self.user_root,
                    )
                    requirements_name = "requirements-hashed.txt"
                    requirements_descriptor = os.open(
                        requirements_name,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=temporary_descriptor,
                    )
                    with os.fdopen(requirements_descriptor, "w", encoding="utf-8") as output:
                        output.write(
                            "".join(
                                f"{wheel.as_uri()} --hash=sha256:{sha256}\n"
                                for wheel, sha256 in sorted(wheels, key=lambda item: str(item[0]))
                            )
                        )
                    _run_venv_python(
                        temporary_descriptor,
                        [
                            "-I",
                            "-m",
                            "pip",
                            "--isolated",
                            "install",
                            "--no-index",
                            "--no-deps",
                            "--no-compile",
                            "--require-hashes",
                            "-r",
                            requirements_name,
                        ],
                        environment=environment,
                        cwd=self.user_root,
                    )
                    os.unlink(requirements_name, dir_fd=temporary_descriptor)
                    declared = {
                        identity[0]
                        for wheel, _sha256 in wheels
                        if (identity := _wheel_identity(wheel.name)) is not None
                    }
                    bootstrap = [name for name in ("pip", "setuptools") if name not in declared]
                    if bootstrap:
                        _run_venv_python(
                            temporary_descriptor,
                            ["-I", "-m", "pip", "uninstall", "--yes", *bootstrap],
                            environment=environment,
                            cwd=self.user_root,
                        )
                    if executable is not None:
                        assert entry_point is not None
                        _rewrite_console_script(
                            temporary_descriptor,
                            executable,
                            root / "bin/python",
                            entry_point,
                        )
                    _require_directory_identity(root.parent, environments)
                    os.replace(
                        temporary,
                        environment_name,
                        src_dir_fd=environments,
                        dst_dir_fd=environments,
                    )
                    temporary = ""
                    if binary is not None:
                        assert executable is not None
                        with (
                            _directory_at(
                                temporary_descriptor,
                                binary_name,
                                binary.parent,
                            ) as bin_descriptor,
                            _regular_at(cast(int, bin_descriptor), executable, binary) as installed,
                        ):
                            assert installed is not None
            except (OSError, subprocess.CalledProcessError) as error:
                error_output = getattr(error, "stderr", None) or getattr(error, "stdout", None)
                error_output = error_output or str(error)
                if isinstance(error_output, bytes):
                    error_output = error_output.decode("utf-8", errors="replace")
                raise ExtensionError(
                    "SANKA_EXTENSION_INSTALL_FAILED",
                    "Verified extension wheels could not be installed",
                    details={"reason": error_output.strip()},
                ) from error
            finally:
                if temporary:
                    with suppress(OSError):
                        shutil.rmtree(temporary, dir_fd=environments)
        self._verify_environment_artifacts(root, executable, wheels, providers=providers)
        return root

    @_store_operation
    def add_extension(
        self,
        extension_id: str,
        *,
        marketplace: str | None = None,
        configuration: Mapping[str, Any] | None = None,
    ) -> LockEntry:
        self._ensure_official_marketplace()
        with (
            _locked(self.user_root, self._installation_path),
            _locked(self.project_root, self._project_lock_path),
        ):
            source, manifest = self._select(extension_id, marketplace)
            if not _compatible(manifest.runtime_sanka_cli, __version__):
                _error(
                    "SANKA_EXTENSION_INCOMPATIBLE",
                    "Extension is incompatible with this sanka-cli runtime",
                    extension_id=extension_id,
                    runtime=__version__,
                    required=manifest.runtime_sanka_cli,
                )
            installations = self._load_installations()
            disabled = self._load_disabled()
            self._validate_system_claims(manifest, self._load_lock(), disabled)
            selected_wheels = self._select_compatible_wheels(manifest)
            cached = tuple(self._cache_wheel(wheel) for wheel in selected_wheels)
            requirements = {}
            for path, wheel in zip(cached, selected_wheels, strict=True):
                identity = _wheel_identity(wheel.name)
                assert identity is not None
                requirements[identity[0]] = self._inspect_wheel(path, wheel, manifest)
            self._validate_dependency_closure(manifest, selected_wheels, requirements)
            verified_wheels = tuple(
                (path, wheel.sha256) for path, wheel in zip(cached, selected_wheels, strict=True)
            )
            self._wheel_import_records(verified_wheels)
            if manifest.kind == "migration":
                assert manifest.executable is not None
                self._wheel_entry_point(verified_wheels, manifest.executable)
            else:
                self._wheel_connector_entry_points(verified_wheels, manifest.providers)
            artifact_digest = self._manifest_artifact_digest(manifest)
            prior = next(
                (
                    item
                    for item in installations
                    if item.get("artifact_digest") == artifact_digest
                    and item.get("id") == manifest.id
                    and item.get("manifest_digest") == manifest.digest
                    and item.get("marketplace_identity") == source.identity
                    and item.get("snapshot_digest") == source.snapshot_digest
                    and item.get("version") == manifest.version
                ),
                None,
            )
            expected_digest = prior.get("environment_digest") if isinstance(prior, dict) else None
            if manifest.kind == "migration":
                environment = self._materialize_environment(
                    artifact_digest,
                    manifest.executable,
                    verified_wheels,
                    expected_digest=expected_digest,
                )
            else:
                environment = self._materialize_environment(
                    artifact_digest,
                    None,
                    verified_wheels,
                    expected_digest=expected_digest,
                    providers=manifest.providers,
                )
            installation = {
                "artifact_digest": artifact_digest,
                "environment": environment.relative_to(self.user_root).as_posix(),
                "environment_digest": self._environment_digest(environment),
                "id": manifest.id,
                "manifest_digest": manifest.digest,
                "marketplace_identity": source.identity,
                "snapshot_digest": source.snapshot_digest,
                "version": manifest.version,
                "wheels": [
                    {
                        "path": path.relative_to(self.user_root).as_posix(),
                        "sha256": wheel.sha256,
                    }
                    for path, wheel in zip(cached, selected_wheels, strict=True)
                ],
            }
            installations = [
                item for item in installations if item.get("artifact_digest") != artifact_digest
            ] + [installation]
            self._write_installations(installations)
            disabled.discard(extension_id)
            self._write_disabled(disabled)
            entry = LockEntry(
                id=manifest.id,
                version=manifest.version,
                marketplace_identity=source.identity,
                snapshot_digest=source.snapshot_digest,
                manifest_digest=manifest.digest,
                distribution=manifest.distribution,
                artifact_digest=artifact_digest,
                kind=manifest.kind,
                protocol_version=manifest.protocol_version,
                executable=manifest.executable,
                entry_point=manifest.entry_point,
                providers=manifest.providers,
                commands=manifest.commands,
                enabled=True,
                configuration_digest=content_hash(dict(configuration or {})),
            )
            entries = self._load_lock()
            entries[extension_id] = entry
            self._write_lock(entries)
            return entry

    @_store_operation
    def remove_extension(self, extension_id: str) -> None:
        with (
            _locked(self.user_root, self._installation_path),
            _locked(self.project_root, self._project_lock_path),
        ):
            entries = self._load_lock()
            entry = entries.pop(extension_id, None)
            disabled = self._load_disabled()
            installations = self._load_installations()
            if entry is not None:
                client = self._connector_clients.pop(entry.artifact_digest, None)
                if client is not None:
                    client.close()
                manifest = self._manifest_for_lock(entry)
                if self._manifest_artifact_digest(manifest) != entry.artifact_digest:
                    _error(
                        "SANKA_EXTENSION_IDENTITY",
                        "Locked extension artifact does not match its immutable manifest",
                        extension_id=extension_id,
                    )
                expected_installation = self._expected_installation(entry, manifest)
                same_artifact = [
                    item
                    for item in installations
                    if item.get("artifact_digest") == entry.artifact_digest
                ]
                if same_artifact and any(
                    not self._installation_matches(item, expected_installation)
                    for item in same_artifact
                ):
                    _error(
                        "SANKA_EXTENSION_IDENTITY",
                        "Installed extension provenance does not match the project lock",
                        extension_id=extension_id,
                    )
                removed = [
                    item
                    for item in installations
                    if self._installation_matches(item, expected_installation)
                ]
                installations = [item for item in installations if item not in removed]
                retained_wheels = {
                    wheel.get("path")
                    for item in installations
                    for wheel in item.get("wheels", [])
                    if isinstance(wheel, dict)
                }
                for item in removed:
                    environment = item.get("environment")
                    if isinstance(environment, str):
                        root = self._confined(self.user_root, self.user_root / environment)
                        _remove_store_tree(self.user_root, root)
                    for wheel in item.get("wheels", []):
                        if isinstance(wheel, dict) and wheel.get("path") not in retained_wheels:
                            path = self._confined(
                                self.user_root,
                                self.user_root / str(wheel.get("path", "")),
                            )
                            _unlink_store_file(self.user_root, path)
                self._write_installations(installations)
            if extension_id == DEFAULT_EXTENSION_ID:
                disabled.add(extension_id)
            self._write_disabled(disabled)
            self._write_lock(entries)

    def _snapshot_for_lock(self, entry: LockEntry) -> Path:
        return self._confined(
            self.user_root,
            self.user_root
            / "snapshots"
            / _identity_key(entry.marketplace_identity)
            / entry.snapshot_digest.removeprefix("sha256:"),
            must_exist=True,
        )

    def _environment_digest(self, root: Path) -> str:
        with (
            _parent_descriptor(self.user_root, root) as (parent, name),
            _directory_at(parent, name, root) as descriptor,
        ):
            assert descriptor is not None
            return content_hash(
                _tree_records(
                    descriptor,
                    error_code="SANKA_EXTENSION_PATH",
                    subject="Extension environment",
                    allowed_symlinks=self._environment_symlinks(),
                )
            )

    @staticmethod
    def _environment_symlinks() -> dict[str, Path]:
        interpreter = Path(sys.executable).resolve()
        return dict.fromkeys(
            (f"bin/{name}" for name in _venv_launcher_names()),
            interpreter,
        )

    def _manifest_for_lock(self, entry: LockEntry) -> Manifest:
        _records, snapshots = self._marketplace_state()
        expected_tree_digest = next(
            (
                item["tree_digest"]
                for item in snapshots
                if item["identity"] == entry.marketplace_identity
                and item["snapshot_digest"] == entry.snapshot_digest
            ),
            None,
        )
        if expected_tree_digest is None:
            _error(
                "SANKA_EXTENSION_IDENTITY",
                "Locked marketplace snapshot has no persisted tree identity",
                extension_id=entry.id,
            )
        with self._verified_snapshot(
            self._snapshot_for_lock(entry), expected_tree_digest
        ) as manifests:
            manifest = next((item for item in manifests if item.id == entry.id), None)
        if manifest is None or any(
            actual != expected
            for actual, expected in (
                (entry.version, manifest.version),
                (entry.manifest_digest, manifest.digest),
                (entry.distribution, manifest.distribution),
                (entry.kind, manifest.kind),
                (entry.protocol_version, manifest.protocol_version),
                (entry.executable, manifest.executable),
                (entry.entry_point, manifest.entry_point),
                (entry.providers, manifest.providers),
                (entry.commands, manifest.commands),
            )
        ):
            _error(
                "SANKA_EXTENSION_IDENTITY",
                "Locked extension fields do not match its immutable manifest",
                extension_id=entry.id,
            )
        return manifest

    @staticmethod
    def _manifest_artifact_digest(manifest: Manifest) -> str:
        return content_hash(
            {
                "manifest_digest": manifest.digest,
                "wheels": [wheel.sha256 for wheel in manifest.wheels],
            }
        ).removeprefix("sha256:")

    def _expected_installation(
        self,
        entry: LockEntry,
        manifest: Manifest,
    ) -> dict[str, Any]:
        selected_wheels = self._select_compatible_wheels(manifest)
        return {
            "artifact_digest": entry.artifact_digest,
            "environment": (Path("environments") / entry.artifact_digest).as_posix(),
            "id": entry.id,
            "manifest_digest": manifest.digest,
            "marketplace_identity": entry.marketplace_identity,
            "snapshot_digest": entry.snapshot_digest,
            "version": manifest.version,
            "wheels": [
                {
                    "path": (Path("cache") / "wheels" / wheel.sha256 / wheel.name).as_posix(),
                    "sha256": wheel.sha256,
                }
                for wheel in selected_wheels
            ],
        }

    @staticmethod
    def _installation_matches(value: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
        return set(value) == {*expected, "environment_digest"} and all(
            value.get(key) == item for key, item in expected.items()
        )

    @_store_operation
    def resolve_locked(self, extension_id: str) -> LockEntry:
        entry = self._load_lock().get(extension_id)
        if entry is None or not entry.enabled or extension_id in self._load_disabled():
            _error(
                "SANKA_EXTENSION_REQUIRED",
                "Extension is not enabled and locked for this project",
                extension_id=extension_id,
            )
        if not any(
            marketplace.identity == entry.marketplace_identity and marketplace.trusted
            for marketplace in self.marketplaces()
        ):
            _error(
                "SANKA_MARKETPLACE_TRUST_REQUIRED",
                "Locked extension marketplace is no longer configured and trusted",
                extension_id=extension_id,
                identity=entry.marketplace_identity,
            )
        manifest = self._manifest_for_lock(entry)
        if not _compatible(manifest.runtime_sanka_cli, __version__):
            _error(
                "SANKA_EXTENSION_INCOMPATIBLE",
                "Locked extension is incompatible with this sanka-cli runtime",
                extension_id=extension_id,
                runtime=__version__,
                required=manifest.runtime_sanka_cli,
            )
        if self._manifest_artifact_digest(manifest) != entry.artifact_digest:
            _error(
                "SANKA_EXTENSION_IDENTITY",
                "Locked extension artifact does not match its immutable manifest",
                extension_id=extension_id,
            )
        expected_installation = self._expected_installation(entry, manifest)
        installation = next(
            (
                item
                for item in self._load_installations()
                if self._installation_matches(item, expected_installation)
            ),
            None,
        )
        if installation is None:
            _error(
                "SANKA_EXTENSION_NOT_CACHED",
                "Exact locked extension installation is not cached",
                extension_id=extension_id,
                artifact_digest=entry.artifact_digest,
            )
        environment = installation.get("environment")
        if not isinstance(environment, str):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Extension installation environment is invalid",
                extension_id=extension_id,
            )
        expected_root = self.user_root / "environments" / entry.artifact_digest
        root = self._confined(
            self.user_root,
            self.user_root / environment,
            must_exist=True,
            expected=expected_root,
        )
        if manifest.kind == "migration":
            assert manifest.executable is not None
            binary = root / ("Scripts" if os.name == "nt" else "bin") / manifest.executable
            self._confined(self.user_root, binary, must_exist=True, expected=binary)
            if not binary.is_file():
                _error(
                    "SANKA_EXTENSION_NOT_CACHED",
                    "Exact locked extension executable is not cached",
                    extension_id=extension_id,
                )
        else:
            site_packages = self._connector_site_packages(root)
            self._confined(
                self.user_root,
                site_packages,
                must_exist=True,
                expected=site_packages,
            )
            if not site_packages.is_dir():
                _error(
                    "SANKA_EXTENSION_NOT_CACHED",
                    "Exact locked connector environment is not cached",
                    extension_id=extension_id,
                )
        verified_wheels: list[tuple[Path, str]] = []
        for wheel in installation.get("wheels", []):
            if not isinstance(wheel, dict) or not isinstance(wheel.get("path"), str):
                _error(
                    "SANKA_EXTENSION_STATE_INVALID",
                    "Extension installation wheel record is invalid",
                    extension_id=extension_id,
                )
            path = self._confined(
                self.user_root,
                self.user_root / wheel["path"],
                must_exist=True,
            )
            if _store_file_sha256(self.user_root, path) != wheel.get("sha256"):
                _error(
                    "SANKA_EXTENSION_HASH_MISMATCH",
                    "Cached locked extension wheel has changed",
                    extension_id=extension_id,
                    artifact=path.name,
                )
            verified_wheels.append((path, str(wheel["sha256"])))
        self._verify_environment_artifacts(
            root,
            manifest.executable,
            tuple(verified_wheels),
            providers=manifest.providers,
        )
        if self._environment_digest(root) != installation.get("environment_digest"):
            _error(
                "SANKA_EXTENSION_HASH_MISMATCH",
                "Installed locked extension environment has changed",
                extension_id=extension_id,
            )
        return entry

    @staticmethod
    def _connector_site_packages(environment: Path) -> Path:
        return environment / ExtensionStore._site_packages_relative()

    @staticmethod
    def _validate_system_claims(
        manifest: Manifest,
        entries: Mapping[str, LockEntry],
        disabled: set[str],
    ) -> None:
        """Validate activation before downloading wheels or changing installed state.

        Adding an existing disabled extension also re-enables it, so both paths
        pass through this check while holding the installation and project locks.
        """
        if manifest.kind != "connector":
            return
        from sanka.runtime.registry import HOSTED_SYSTEM_PROVIDERS

        requested = {system.name for system in manifest.providers}
        reserved = sorted(requested.intersection(HOSTED_SYSTEM_PROVIDERS))
        if reserved:
            _error(
                "SANKA_EXTENSION_SYSTEM_RESERVED",
                "These system types run through the hosted System Migration API",
                extension_id=manifest.id,
                system_types=reserved,
            )
        for entry in sorted(entries.values(), key=lambda item: item.id):
            if (
                entry.id == manifest.id
                or entry.kind != "connector"
                or not entry.enabled
                or entry.id in disabled
            ):
                continue
            overlap = sorted(requested.intersection(system.name for system in entry.providers))
            if overlap:
                _error(
                    "SANKA_EXTENSION_SYSTEM_CONFLICT",
                    "A system type already has an enabled extension; remove it first",
                    extension_id=manifest.id,
                    conflicting_extension_id=entry.id,
                    system_types=overlap,
                )

    def _system_lock(self, system_type: str) -> LockEntry:
        disabled = self._load_disabled()
        selected = [
            entry
            for entry in self._load_lock().values()
            if entry.kind == "connector"
            and entry.enabled
            and entry.id not in disabled
            and any(item.name == system_type for item in entry.providers)
        ]
        if len(selected) != 1:
            code = "SANKA_EXTENSION_REQUIRED" if not selected else "SANKA_EXTENSION_IDENTITY"
            _error(
                code,
                "System type must resolve to one enabled extension",
                system_type=system_type,
                extension_ids=sorted(entry.id for entry in selected),
            )
        return selected[0]

    def system_extension_metadata(self, system_type: str) -> dict[str, str]:
        entry = self.resolve_locked(self._system_lock(system_type).id)
        manifest = self._manifest_for_lock(entry)
        return {
            "extension_id": entry.id,
            "extension_version": entry.version,
            "package": manifest.distribution,
            "package_version": manifest.distribution_version,
        }

    def supported_systems(self) -> tuple[str, ...]:
        disabled = self._load_disabled()
        return tuple(
            sorted(
                {
                    provider.name
                    for entry in self._load_lock().values()
                    if entry.kind == "connector" and entry.enabled and entry.id not in disabled
                    for provider in entry.providers
                }
            )
        )

    @_store_operation
    def resolve_extension(self, provider: str) -> ExtensionRegistration:
        entry = self.resolve_locked(self._system_lock(provider).id)
        manifest = self._manifest_for_lock(entry)
        declared = next(item for item in manifest.providers if item.name == provider)
        environment = self._confined(
            self.user_root,
            self.user_root / "environments" / entry.artifact_digest,
            must_exist=True,
        )
        site_packages = self._connector_site_packages(environment)
        self._confined(
            self.user_root,
            site_packages,
            must_exist=True,
            expected=site_packages,
        )
        client = self._connector_clients.get(entry.artifact_digest)
        if client is None:
            client = ExtensionHostClient(sys.executable, environment=site_packages)
            self._connector_clients[entry.artifact_digest] = client
        description = client.request(provider, "describe", {})
        if (
            type(description) is not dict
            or type(description.get("roles")) is not list
            or tuple(description["roles"]) != declared.roles
        ):
            client.close()
            self._connector_clients.pop(entry.artifact_digest, None)
            _error(
                "SANKA_EXTENSION_IDENTITY",
                "Connector host roles differ from the immutable manifest",
                provider=provider,
            )
        source = (
            build_remote_extension(client, provider, "source", description=description)
            if "source" in declared.roles
            else None
        )
        destination = (
            build_remote_extension(client, provider, "destination", description=description)
            if "destination" in declared.roles
            else None
        )
        return ExtensionRegistration(
            name=provider,
            source=cast(Any, source),
            destination=cast(Any, destination),
        )

    # Compatibility method names for existing runtime consumers.
    connector_providers = supported_systems
    resolve_connector = resolve_extension


__all__ = [
    "DEFAULT_EXTENSION_ID",
    "OFFICIAL_IDENTITY",
    "OFFICIAL_SOURCE",
    "ExtensionRecord",
    "ExtensionStore",
    "LockEntry",
    "MarketplaceRecord",
    "user_extension_root",
]
