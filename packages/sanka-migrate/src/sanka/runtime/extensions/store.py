# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted immutable marketplace snapshots and hash-bound extension installs."""

from __future__ import annotations

import configparser
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import venv
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

from sanka.runtime.__about__ import __version__
from sanka.runtime.extensions.discovery import (
    STATUS_ORDER,
    _compatible,
    _normalized_distribution,
    _wheel_identity,
    load_marketplace,
)
from sanka.runtime.extensions.model import ExtensionError, Manifest, Wheel
from sanka.runtime.hashing import content_hash

OFFICIAL_IDENTITY = "github.com/sankaHQ/extensions"
OFFICIAL_SOURCE = "https://github.com/sankaHQ/extensions.git"
DEFAULT_EXTENSION_ID = "sanka/drf-to-fastapi"
MAX_WHEEL_BYTES = 128 * 1024 * 1024
MAX_WHEEL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_WHEEL_METADATA_BYTES = 1024 * 1024
MARKETPLACE_SCHEMA = "sanka-extension-marketplaces/v1"
INSTALLATION_SCHEMA = "sanka-extension-installations/v1"
LOCK_SCHEMA = "sanka-extension-lock/v1"
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_ENTRY_POINT = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def user_extension_root() -> Path:
    override = os.environ.get("SANKA_HOME")
    return (Path(override) if override else Path.home() / ".sanka") / "extensions"


def _error(code: str, message: str, **details: Any) -> NoReturn:
    raise ExtensionError(code, message, details=details)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity_key(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()


def _marketplace_sort_key(value: dict[str, Any]) -> str:
    return str(value.get("name"))


def _installation_sort_key(value: dict[str, Any]) -> str:
    return str(value.get("artifact_digest"))


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        item = path.lstat()
        if stat.S_ISLNK(item.st_mode) or not (path.is_dir() or stat.S_ISREG(item.st_mode)):
            _error(
                "SANKA_MARKETPLACE_PATH_INVALID",
                "Marketplace snapshots may contain only real directories and regular files",
                path=relative,
            )
        digest.update(("d\0" if path.is_dir() else "f\0").encode())
        digest.update(relative.encode())
        digest.update(b"\0")
        if path.is_file():
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


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
    targets: tuple[str, ...]
    status: tuple[str, ...]
    wheels: tuple[Wheel, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
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
    executable: str
    enabled: bool
    configuration_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _locked(root: Path, path: Path) -> Iterator[None]:
    try:
        parent = path.parent.resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ExtensionError(
            "SANKA_EXTENSION_PATH",
            "Extension lock parent cannot be resolved inside its store",
            details={"path": str(path.parent)},
        ) from error
    if not parent.is_relative_to(root):
        _error("SANKA_EXTENSION_PATH", "Extension lock escapes its store", path=str(path))
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        _error("SANKA_EXTENSION_PATH", "Extension lock cannot be a symlink", path=str(lock_path))
    parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension lock must be a regular file",
                path=str(lock_path),
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class ExtensionStore:
    """One stdlib-only trust, snapshot, cache, and project-lock boundary."""

    def __init__(self, project_root: Path, user_root: Path | None = None) -> None:
        self.project_root = self._real_root(project_root, "project")
        self.user_root = self._real_root(user_root or user_extension_root(), "user")
        self._marketplace_path = self.user_root / "marketplaces.json"
        self._installation_path = self.user_root / "installations.json"
        self._disabled_path = self.user_root / "disabled.json"
        self._project_lock_path = self.project_root / ".sanka" / "extensions.lock"

    @staticmethod
    def _real_root(path: Path, label: str) -> Path:
        path = path.expanduser()
        if path.is_symlink():
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension store root cannot be a symlink",
                store=label,
                path=str(path),
            )
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension store root must be a directory",
                store=label,
                path=str(path),
            )
        return path.resolve()

    @staticmethod
    def _confined(root: Path, candidate: Path, *, must_exist: bool = False) -> Path:
        try:
            resolved = candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError, ValueError) as error:
            raise ExtensionError(
                "SANKA_EXTENSION_PATH",
                "Extension path cannot be resolved inside its store",
                details={"path": str(candidate)},
            ) from error
        if not resolved.is_relative_to(root):
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension path escapes its store",
                path=str(candidate),
            )
        return resolved

    @staticmethod
    def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return default
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
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
        if path.is_symlink():
            _error(
                "SANKA_EXTENSION_PATH",
                "Extension state path cannot be a symlink",
                path=str(path),
            )
        cls._confined(root, path)
        return path

    def _raw_marketplaces(self) -> list[dict[str, Any]]:
        path = self._state_path(self.user_root, self._marketplace_path)
        payload = self._load_json(
            path,
            {"schema_version": MARKETPLACE_SCHEMA, "marketplaces": []},
        )
        if payload.get("schema_version") != MARKETPLACE_SCHEMA or not isinstance(
            payload.get("marketplaces"), list
        ):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace configuration is invalid",
                path=str(self._marketplace_path),
            )
        records = payload["marketplaces"]
        if any(not isinstance(item, dict) for item in records):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace configuration records must be objects",
                path=str(self._marketplace_path),
            )
        for item in records:
            self._record(item)
        names = [item["name"] for item in records]
        if len(names) != len(set(names)):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace configuration contains duplicate names",
                path=str(self._marketplace_path),
            )
        return cast(list[dict[str, Any]], records)

    def _record(self, value: Mapping[str, Any]) -> MarketplaceRecord:
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
        }
        if set(value) != keys or not isinstance(value.get("snapshot_root"), str):
            _error(
                "SANKA_EXTENSION_STATE_INVALID",
                "Marketplace record is invalid",
                path=str(self._marketplace_path),
            )
        strings = ("identity", "kind", "name", "snapshot_digest", "source")
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
            snapshot_root=root,
        )

    def marketplaces(self) -> tuple[MarketplaceRecord, ...]:
        return tuple(
            sorted(
                (self._record(item) for item in self._raw_marketplaces()),
                key=lambda item: item.name,
            )
        )

    def _snapshot_destination(self, identity: str, digest: str) -> Path:
        return self._confined(
            self.user_root,
            self.user_root / "snapshots" / _identity_key(identity) / digest.removeprefix("sha256:"),
        )

    def _place_snapshot(self, source: Path, identity: str, digest: str) -> Path:
        destination = self._snapshot_destination(identity, digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(source)
            return destination
        os.replace(source, destination)
        return destination

    def _snapshot_local(self, source: Path, identity: str) -> tuple[Path, str]:
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
            return self._place_snapshot(staging, identity, digest), digest
        finally:
            shutil.rmtree(staging.parent, ignore_errors=True)

    def _snapshot_git(self, source: str, identity: str) -> tuple[Path, str]:
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
            _tree_digest(checkout)
            load_marketplace(checkout)
            return self._place_snapshot(checkout, identity, commit), commit
        except subprocess.CalledProcessError as error:
            raise ExtensionError(
                "SANKA_MARKETPLACE_GIT_FAILED",
                "Marketplace Git snapshot failed",
                details={"source": source, "reason": error.stderr.strip()},
            ) from error
        finally:
            shutil.rmtree(parent, ignore_errors=True)

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
        with _locked(self.user_root, self._marketplace_path):
            records = self._raw_marketplaces()
            if any(item.get("name") == chosen_name for item in records):
                _error(
                    "SANKA_MARKETPLACE_EXISTS",
                    "Marketplace name is already configured",
                    name=chosen_name,
                )
            if kind == "git":
                root, digest = self._snapshot_git(fetch_source, identity)
                commit, content_digest = digest, None
            else:
                root, digest = self._snapshot_local(Path(fetch_source), identity)
                commit, content_digest = None, digest
            load_marketplace(root)
            raw = {
                "content_digest": content_digest,
                "identity": identity,
                "kind": kind,
                "name": chosen_name,
                "resolved_commit": commit,
                "snapshot_digest": digest,
                "snapshot_root": root.relative_to(self.user_root).as_posix(),
                "source": fetch_source,
                "trusted": trusted,
            }
            records.append(raw)
            records.sort(key=_marketplace_sort_key)
            _atomic_json(
                self._marketplace_path,
                {
                    "schema_version": MARKETPLACE_SCHEMA,
                    "marketplaces": records,
                },
            )
            return self._record(raw)

    def upgrade_marketplace(self, name: str | None = None) -> tuple[MarketplaceRecord, ...]:
        with _locked(self.user_root, self._marketplace_path):
            records = self._raw_marketplaces()
            selected = [item for item in records if name is None or item.get("name") == name]
            if not selected:
                _error(
                    "SANKA_MARKETPLACE_NOT_FOUND",
                    "Marketplace is not configured",
                    name=name,
                )
            upgraded: list[MarketplaceRecord] = []
            for raw in selected:
                kind = raw["kind"]
                identity = raw["identity"]
                source = raw["source"]
                if kind == "git":
                    root, digest = self._snapshot_git(source, identity)
                    raw["resolved_commit"], raw["content_digest"] = digest, None
                elif kind == "local":
                    root, digest = self._snapshot_local(Path(source), identity)
                    raw["resolved_commit"], raw["content_digest"] = None, digest
                else:
                    _error(
                        "SANKA_EXTENSION_STATE_INVALID",
                        "Marketplace source kind is invalid",
                        name=raw.get("name"),
                    )
                raw["snapshot_digest"] = digest
                raw["snapshot_root"] = root.relative_to(self.user_root).as_posix()
                upgraded.append(self._record(raw))
            records.sort(key=_marketplace_sort_key)
            _atomic_json(
                self._marketplace_path,
                {
                    "schema_version": MARKETPLACE_SCHEMA,
                    "marketplaces": records,
                },
            )
            return tuple(sorted(upgraded, key=lambda item: item.name))

    def remove_marketplace(self, name: str) -> MarketplaceRecord:
        with (
            _locked(self.user_root, self._marketplace_path),
            _locked(self.project_root, self._project_lock_path),
        ):
            records = self._raw_marketplaces()
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
            _atomic_json(
                self._marketplace_path,
                {"schema_version": MARKETPLACE_SCHEMA, "marketplaces": records},
            )
            return self._record(raw)

    def _load_installations(self) -> list[dict[str, Any]]:
        path = self._state_path(self.user_root, self._installation_path)
        payload = self._load_json(
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
                or re.fullmatch(r"sha256:[0-9a-f]{64}", item["manifest_digest"]) is None
                or not isinstance(item["wheels"], list)
                or not item["wheels"]
            ):
                _error(
                    "SANKA_EXTENSION_STATE_INVALID",
                    "Extension installation record is invalid",
                    path=str(self._installation_path),
                )
            self._confined(self.user_root, self.user_root / item["environment"])
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
                self._confined(self.user_root, self.user_root / wheel["path"])
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
            self._installation_path,
            {
                "schema_version": INSTALLATION_SCHEMA,
                "installations": values,
            },
        )

    def _write_disabled(self, values: set[str]) -> None:
        _atomic_json(
            self._disabled_path,
            {"schema_version": "sanka-extension-disabled/v1", "extensions": sorted(values)},
        )

    def _load_lock(self) -> dict[str, LockEntry]:
        path = self._state_path(self.project_root, self._project_lock_path)
        payload = self._load_json(path, {"schema_version": LOCK_SCHEMA, "extensions": []})
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
            if any(
                not isinstance(value[field], str) or not value[field]
                for field in expected - {"enabled"}
            ) or not isinstance(value["enabled"], bool):
                _error(
                    "SANKA_EXTENSION_LOCK_INVALID",
                    "Project extension lock fields are invalid",
                    path=str(path),
                )
            entry = LockEntry(**value)
            if (
                not re.fullmatch(r"sha256:[0-9a-f]{64}", entry.manifest_digest)
                or not re.fullmatch(r"[0-9a-f]{64}", entry.artifact_digest)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", entry.configuration_digest)
                or not (
                    re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", entry.snapshot_digest)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", entry.snapshot_digest)
                )
                or entry.protocol_version != "sanka-extension/v1"
                or "/" in entry.executable
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
            values.extend((record, manifest) for manifest in load_marketplace(record.snapshot_root))
        return values

    def list_extensions(self) -> tuple[ExtensionRecord, ...]:
        installations = self._load_installations()
        disabled = self._load_disabled()
        locks = self._load_lock()
        records: list[ExtensionRecord] = []
        for marketplace, manifest in self._catalog():
            statuses = {"available"}
            lock = locks.get(manifest.id)
            installed = any(item.get("id") == manifest.id for item in installations)
            if manifest.id == DEFAULT_EXTENSION_ID and manifest.id not in disabled:
                installed = (
                    installed or self._default_distribution(manifest, required=False) is not None
                )
            if installed and manifest.id not in disabled:
                statuses.add("installed")
            if lock and lock.enabled:
                statuses.add("locked")
                if (
                    lock.version != manifest.version
                    or lock.snapshot_digest != marketplace.snapshot_digest
                    or lock.manifest_digest != manifest.digest
                ):
                    statuses.add("update_available")
            if not _compatible(manifest.runtime_sanka_migrate, __version__):
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
                    targets=manifest.targets,
                    status=tuple(item for item in STATUS_ORDER if item in statuses),
                    wheels=manifest.wheels,
                )
            )
        return tuple(sorted(records, key=lambda item: (item.id, item.version, item.marketplace)))

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

    def _cache_wheel(self, wheel: Wheel) -> Path:
        if not wheel.name.endswith(".whl") or _wheel_identity(wheel.name) is None:
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Only valid Python wheels may be installed",
                artifact=wheel.name,
            )
        destination = self._wheel_cache_path(wheel)
        if destination.exists():
            if _sha256_file(destination) != wheel.sha256:
                _error(
                    "SANKA_EXTENSION_HASH_MISMATCH",
                    "Cached extension wheel does not match its declared SHA-256",
                    artifact=wheel.name,
                )
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            try:
                with urlopen(wheel.url, timeout=30) as response:
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
                    with temporary.open("wb") as output:
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
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

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

    def _inspect_wheel(
        self,
        path: Path,
        wheel: Wheel,
        manifest: Manifest,
        declared: set[str],
    ) -> None:
        identity = _wheel_identity(wheel.name)
        if identity is None:
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Only valid Python wheels may be installed",
                artifact=wheel.name,
            )
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if (
                    any(
                        info.flag_bits & 0x1
                        or PurePosixPath(info.filename).is_absolute()
                        or ".." in PurePosixPath(info.filename).parts
                        for info in members
                    )
                    or sum(info.file_size for info in members) > MAX_WHEEL_UNCOMPRESSED_BYTES
                ):
                    _error(
                        "SANKA_EXTENSION_ARTIFACT_INVALID",
                        "Extension wheel members are unsafe",
                        artifact=wheel.name,
                    )
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
        expected_tag = "-".join(wheel.name.removesuffix(".whl").split("-")[-3:])
        if (
            wheel_metadata.get_all("Wheel-Version", []) != ["1.0"]
            or wheel_metadata.get_all("Root-Is-Purelib", []) != ["true"]
            or expected_tag not in wheel_metadata.get_all("Tag", [])
        ):
            _error(
                "SANKA_EXTENSION_ARTIFACT_INVALID",
                "Extension WHEEL metadata must declare a purelib wheel with matching tags",
                artifact=wheel.name,
            )
        for requirement in package.get_all("Requires-Dist", []):
            match = _REQUIREMENT_NAME.match(requirement)
            normalized = _normalized_distribution(match.group(0)) if match else ""
            if normalized not in declared:
                _error(
                    "SANKA_EXTENSION_UNDECLARED_REQUIREMENT",
                    "Extension wheel requires an undeclared distribution",
                    artifact=wheel.name,
                    requirement=requirement,
                )
        if identity[0] == _normalized_distribution(manifest.distribution):
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(entry_points)
                target = parser["console_scripts"][manifest.executable]
            except (configparser.Error, KeyError) as error:
                raise ExtensionError(
                    "SANKA_EXTENSION_ARTIFACT_INVALID",
                    "Extension wheel does not declare its executable entry point",
                    details={"artifact": wheel.name, "executable": manifest.executable},
                ) from error
            if _ENTRY_POINT.fullmatch(target.strip()) is None:
                _error(
                    "SANKA_EXTENSION_ARTIFACT_INVALID",
                    "Extension executable entry point is invalid",
                    artifact=wheel.name,
                    executable=manifest.executable,
                )

    def _materialize_environment(
        self,
        artifact_digest: str,
        executable: str,
        wheels: tuple[Path, ...],
    ) -> Path:
        root = self._confined(
            self.user_root,
            self.user_root / "environments" / artifact_digest,
        )
        binary = root / ("Scripts" if os.name == "nt" else "bin") / executable
        if root.exists():
            if not binary.is_file():
                _error(
                    "SANKA_EXTENSION_NOT_CACHED",
                    "Cached extension environment is incomplete",
                    artifact_digest=artifact_digest,
                )
            return root
        root.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="environment-", dir=root.parent))
        try:
            venv.EnvBuilder(with_pip=True, system_site_packages=True).create(temporary)
            python = temporary / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            requirements = temporary / "requirements-hashed.txt"
            requirements.write_text(
                "".join(
                    f"{wheel.as_uri()} --hash=sha256:{_sha256_file(wheel)}\n"
                    for wheel in sorted(wheels)
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--require-hashes",
                    "-r",
                    str(requirements),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            requirements.unlink()
            os.replace(temporary, root)
        except (OSError, subprocess.CalledProcessError) as error:
            output = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            raise ExtensionError(
                "SANKA_EXTENSION_INSTALL_FAILED",
                "Verified extension wheels could not be installed",
                details={"reason": output.strip()},
            ) from error
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        if not binary.is_file():
            _error(
                "SANKA_EXTENSION_INSTALL_FAILED",
                "Installed extension executable is missing",
                executable=executable,
            )
        return root

    @staticmethod
    def _default_distribution(manifest: Manifest, *, required: bool) -> Any | None:
        try:
            distribution = metadata.distribution(manifest.distribution)
        except metadata.PackageNotFoundError:
            if required:
                _error(
                    "SANKA_EXTENSION_NOT_CACHED",
                    "Default extension distribution is not installed",
                    distribution=manifest.distribution,
                )
            return None
        if (
            _normalized_distribution(distribution.metadata.get("Name", ""))
            != _normalized_distribution(manifest.distribution)
            or distribution.version != manifest.distribution_version
        ):
            if required:
                _error(
                    "SANKA_EXTENSION_IDENTITY",
                    "Installed default extension does not match the marketplace manifest",
                    distribution=manifest.distribution,
                )
            return None
        return distribution

    @staticmethod
    def _distribution_digest(distribution: Any) -> str:
        files = distribution.files
        if not files:
            _error(
                "SANKA_EXTENSION_IDENTITY",
                "Installed default extension does not expose verifiable files",
            )
        digest = hashlib.sha256()
        for relative in sorted(files, key=lambda item: str(item)):
            path = Path(distribution.locate_file(relative))
            if path.is_symlink() or not path.is_file():
                _error(
                    "SANKA_EXTENSION_IDENTITY",
                    "Installed default extension contains an unverifiable file",
                    path=str(relative),
                )
            digest.update(str(relative).replace(os.sep, "/").encode())
            digest.update(b"\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    def add_extension(
        self,
        extension_id: str,
        *,
        marketplace: str | None = None,
        configuration: Mapping[str, Any] | None = None,
    ) -> LockEntry:
        with (
            _locked(self.user_root, self._installation_path),
            _locked(self.project_root, self._project_lock_path),
        ):
            source, manifest = self._select(extension_id, marketplace)
            if not _compatible(manifest.runtime_sanka_migrate, __version__):
                _error(
                    "SANKA_EXTENSION_INCOMPATIBLE",
                    "Extension is incompatible with this sanka-migrate runtime",
                    extension_id=extension_id,
                    runtime=__version__,
                    required=manifest.runtime_sanka_migrate,
                )
            installations = self._load_installations()
            disabled = self._load_disabled()
            if extension_id == DEFAULT_EXTENSION_ID:
                distribution = self._default_distribution(manifest, required=True)
                artifact_digest = self._distribution_digest(distribution)
            else:
                wheel_identities = tuple(
                    identity
                    for wheel in manifest.wheels
                    if (identity := _wheel_identity(wheel.name)) is not None
                )
                declared = {identity[0] for identity in wheel_identities}
                if len(wheel_identities) != len(manifest.wheels) or len(declared) != len(
                    wheel_identities
                ):
                    _error(
                        "SANKA_EXTENSION_ARTIFACT_INVALID",
                        "Extension wheel set contains an invalid or duplicate distribution",
                        extension_id=extension_id,
                    )
                cached = tuple(self._cache_wheel(wheel) for wheel in manifest.wheels)
                for path, wheel in zip(cached, manifest.wheels, strict=True):
                    self._inspect_wheel(path, wheel, manifest, declared)
                artifact_digest = content_hash(
                    {
                        "manifest_digest": manifest.digest,
                        "wheels": [wheel.sha256 for wheel in manifest.wheels],
                    }
                ).removeprefix("sha256:")
                environment = self._materialize_environment(
                    artifact_digest, manifest.executable, cached
                )
                installation = {
                    "artifact_digest": artifact_digest,
                    "environment": environment.relative_to(self.user_root).as_posix(),
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
                        for path, wheel in zip(cached, manifest.wheels, strict=True)
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
                protocol_version=manifest.protocol_version,
                executable=manifest.executable,
                enabled=True,
                configuration_digest=content_hash(dict(configuration or {})),
            )
            entries = self._load_lock()
            entries[extension_id] = entry
            self._write_lock(entries)
            return entry

    def remove_extension(self, extension_id: str) -> None:
        with (
            _locked(self.user_root, self._installation_path),
            _locked(self.project_root, self._project_lock_path),
        ):
            entries = self._load_lock()
            entry = entries.pop(extension_id, None)
            disabled = self._load_disabled()
            installations = self._load_installations()
            if extension_id == DEFAULT_EXTENSION_ID:
                disabled.add(extension_id)
            elif entry is not None:
                removed = [
                    item
                    for item in installations
                    if item.get("artifact_digest") == entry.artifact_digest
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
                        if root.exists():
                            shutil.rmtree(root)
                    for wheel in item.get("wheels", []):
                        if isinstance(wheel, dict) and wheel.get("path") not in retained_wheels:
                            path = self._confined(
                                self.user_root,
                                self.user_root / str(wheel.get("path", "")),
                            )
                            path.unlink(missing_ok=True)
                self._write_installations(installations)
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
        snapshot = self._snapshot_for_lock(entry)
        manifest = next(
            (
                item
                for item in load_marketplace(snapshot)
                if item.id == entry.id
                and item.version == entry.version
                and item.digest == entry.manifest_digest
            ),
            None,
        )
        if manifest is None:
            _error(
                "SANKA_EXTENSION_IDENTITY",
                "Locked extension manifest does not match its immutable snapshot",
                extension_id=extension_id,
            )
        if extension_id == DEFAULT_EXTENSION_ID:
            distribution = self._default_distribution(manifest, required=True)
            if self._distribution_digest(distribution) != entry.artifact_digest:
                _error(
                    "SANKA_EXTENSION_HASH_MISMATCH",
                    "Installed default extension content has changed",
                    extension_id=extension_id,
                )
            return entry
        installation = next(
            (
                item
                for item in self._load_installations()
                if item.get("artifact_digest") == entry.artifact_digest
                and item.get("id") == entry.id
                and item.get("version") == entry.version
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
        root = self._confined(self.user_root, self.user_root / environment, must_exist=True)
        binary = root / ("Scripts" if os.name == "nt" else "bin") / entry.executable
        if not binary.is_file():
            _error(
                "SANKA_EXTENSION_NOT_CACHED",
                "Exact locked extension executable is not cached",
                extension_id=extension_id,
            )
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
            if _sha256_file(path) != wheel.get("sha256"):
                _error(
                    "SANKA_EXTENSION_HASH_MISMATCH",
                    "Cached locked extension wheel has changed",
                    extension_id=extension_id,
                    artifact=path.name,
                )
        return entry


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
