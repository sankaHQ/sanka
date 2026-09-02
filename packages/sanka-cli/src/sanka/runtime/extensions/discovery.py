# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded static project fingerprinting and strict marketplace matching."""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import sys
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn
from urllib.parse import urlparse

from sanka.runtime.extensions.model import (
    LIFECYCLE_COMMANDS,
    ExtensionError,
    Fingerprint,
    Manifest,
    MatchedEvidence,
    Matcher,
    Provider,
    Recommendation,
    Wheel,
)
from sanka.runtime.hashing import content_hash
from sanka_cli import __version__

MAX_FILES = 20_000
MAX_SOURCE_BYTES = 1024 * 1024
IGNORED_DIRECTORIES = frozenset({".git", ".venv", "node_modules", ".sanka"})
LANGUAGES = {
    ".go": "go",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".ts": "typescript",
    ".tsx": "typescript",
}
MATCHER_KINDS = frozenset(
    {"dependency", "file", "file_suffix", "framework", "language", "static_import"}
)
STATUS_ORDER = (
    "available",
    "installed",
    "locked",
    "update_available",
    "incompatible",
    "disabled",
)
STATE_VALUES = frozenset(STATUS_ORDER) - {"available", "incompatible"}
IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
DISTRIBUTION = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
IMPORT = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
VERSION = re.compile(r"(?P<release>\d+(?:\.\d+){1,2})(?:(?P<pre>a|b|rc)(?P<pre_number>\d+))?")
SPECIFIER = re.compile(r"(<=|>=|==|<|>)(.+)")
SHA256 = re.compile(r"[0-9a-f]{64}")
PYTHON_TAG = re.compile(
    r"(?:py|cp|pp|pypy)\d+[A-Za-z0-9_]*(?:\.(?:py|cp|pp|pypy)\d+[A-Za-z0-9_]*)*"
)
WHEEL_TAG = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
BUILD_TAG = re.compile(r"\d[A-Za-z0-9_]*")


def _error(code: str, message: str, *, path: Path | None = None, reason: str = "") -> NoReturn:
    details: dict[str, Any] = {}
    if path is not None:
        details["path"] = path.as_posix()
    if reason:
        details["reason"] = reason
    raise ExtensionError(code, message, details=details)


def _metadata_error(root: Path, path: Path, error: Exception | str) -> NoReturn:
    _error(
        "SANKA_FINGERPRINT_METADATA_INVALID",
        "Repository dependency metadata is invalid",
        path=path.relative_to(root),
        reason=str(error),
    )


def _dependency_name(value: str) -> str | None:
    candidate = value.split("#", 1)[0].strip()
    if not candidate or candidate.startswith("-"):
        return None
    scoped = re.match(r"@[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", candidate)
    if scoped is not None:
        return scoped.group(0).lower()
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate)
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def _dependencies_from_requirements(text: str) -> set[str]:
    return {name for line in text.splitlines() if (name := _dependency_name(line)) is not None}


def _dependencies_from_pyproject(root: Path, path: Path, data: bytes) -> set[str]:
    try:
        payload = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        _metadata_error(root, path, error)
    dependencies: set[str] = set()
    project = payload.get("project", {})
    if not isinstance(project, dict):
        _metadata_error(root, path, "project must be a table")
    groups = [project.get("dependencies", [])]
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        _metadata_error(root, path, "project.optional-dependencies must be a table")
    groups.extend(optional.values())
    poetry = payload.get("tool", {})
    if isinstance(poetry, dict):
        poetry = poetry.get("poetry", {})
        if isinstance(poetry, dict):
            poetry_dependencies = poetry.get("dependencies", {})
            if not isinstance(poetry_dependencies, dict):
                _metadata_error(root, path, "tool.poetry.dependencies must be a table")
            dependencies.update(
                normalized
                for name in poetry_dependencies
                if name.lower() != "python" and (normalized := _dependency_name(name)) is not None
            )
    for group in groups:
        if not isinstance(group, list) or any(not isinstance(item, str) for item in group):
            _metadata_error(root, path, "dependency groups must contain only strings")
        dependencies.update(name for item in group if (name := _dependency_name(item)) is not None)
    return dependencies


def _dependencies_from_package_json(root: Path, path: Path, text: str) -> set[str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        _metadata_error(root, path, error)
    if not isinstance(payload, dict):
        _metadata_error(root, path, "package.json must be an object")
    dependencies: set[str] = set()
    for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        group = payload.get(field, {})
        if not isinstance(group, dict) or any(not isinstance(name, str) for name in group):
            _metadata_error(root, path, f"{field} must be an object")
        dependencies.update(name for item in group if (name := _dependency_name(item)) is not None)
    return dependencies


def _python_imports(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    return imports


def _stat_identity(item: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _read_source(path: Path) -> tuple[bool, bytes | None]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        path_before = os.lstat(path)
        if not stat.S_ISREG(path_before.st_mode):
            return False, None
        descriptor = os.open(path, flags)
    except OSError:
        return False, None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (path_before.st_dev, path_before.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            return False, None
        chunks: list[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        path_after = os.lstat(path)
    except OSError:
        return False, None
    finally:
        os.close(descriptor)
    if len({_stat_identity(item) for item in (path_before, before, after, path_after)}) != 1:
        return False, None
    data = b"".join(chunks)
    return True, data if len(data) <= MAX_SOURCE_BYTES else None


def fingerprint_repository(root: Path) -> Fingerprint:
    """Build a deterministic project fingerprint without importing project code."""
    if root.is_symlink() or not root.is_dir():
        _error("SANKA_FINGERPRINT_ROOT_INVALID", "Repository root must be a real directory")
    root = root.resolve()
    evidence: set[MatchedEvidence] = set()
    dependencies: set[str] = set()
    imports: set[str] = set()
    file_count = 0

    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if name not in IGNORED_DIRECTORIES and not (current_path / name).is_symlink()
        )
        for name in sorted(files):
            path = current_path / name
            file_count += 1
            if file_count > MAX_FILES:
                raise ExtensionError(
                    "SANKA_FINGERPRINT_FILE_LIMIT",
                    "Repository exceeds the static fingerprint file limit",
                    details={"limit": MAX_FILES},
                )
            regular, data = _read_source(path)
            if not regular:
                continue
            relative = path.relative_to(root).as_posix()
            suffix = path.suffix.lower()
            evidence.add(MatchedEvidence("file", relative, relative))
            if suffix:
                evidence.add(MatchedEvidence("file_suffix", suffix, relative))
            language = LANGUAGES.get(suffix)
            if language:
                evidence.add(MatchedEvidence("language", language, relative))
            if data is None:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            discovered: set[str] = set()
            if suffix == ".txt" and (
                name.startswith("requirements") or path.parent.name == "requirements"
            ):
                discovered = _dependencies_from_requirements(text)
            elif name == "pyproject.toml":
                discovered = _dependencies_from_pyproject(root, path, data)
            elif name == "package.json":
                discovered = _dependencies_from_package_json(root, path, text)
            for dependency in discovered:
                dependencies.add(dependency)
                evidence.add(MatchedEvidence("dependency", dependency, relative))
            if suffix == ".py":
                for imported in _python_imports(text):
                    imports.add(imported)
                    evidence.add(MatchedEvidence("static_import", imported, relative))

    frameworks: set[str] = set()
    if "djangorestframework" in dependencies or "rest_framework" in imports:
        frameworks.add("django-rest-framework")
        sources = [
            item.path
            for item in evidence
            if (item.kind, item.value)
            in {
                ("dependency", "djangorestframework"),
                ("static_import", "rest_framework"),
            }
        ]
        evidence.update(
            MatchedEvidence("framework", "django-rest-framework", path) for path in sources
        )

    ordered = tuple(sorted(evidence, key=lambda item: (item.kind, item.value, item.path)))
    languages = tuple(sorted({item.value for item in ordered if item.kind == "language"}))
    framework_names = tuple(sorted(frameworks))
    dependency_names = tuple(sorted(dependencies))
    fingerprint_hash = content_hash(
        {
            "dependencies": dependency_names,
            "evidence": [item.__dict__ for item in ordered],
            "frameworks": framework_names,
            "languages": languages,
        }
    )
    return Fingerprint(languages, framework_names, dependency_names, ordered, fingerprint_hash)


def _invalid(code: str, path: Path, reason: str) -> NoReturn:
    _error(code, "Extension marketplace metadata is invalid", path=path, reason=reason)


def _object(value: Any, keys: set[str], code: str, path: Path, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _invalid(code, path, f"{label} must contain exactly {sorted(keys)}")
    return value


def _string_list(
    value: Any,
    *,
    code: str,
    path: Path,
    label: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or (not value and not allow_empty)
        or len(value) != len(set(value))
    ):
        _invalid(code, path, f"{label} must be a unique string array")
    return tuple(value)


def _valid_id(value: Any) -> bool:
    if not isinstance(value, str) or value.count("/") != 1:
        return False
    owner, name = value.split("/")
    return bool(IDENTIFIER.fullmatch(owner) and IDENTIFIER.fullmatch(name))


def _version_key(value: str) -> tuple[int, int, int, int, int]:
    match = VERSION.fullmatch(value)
    if match is None:
        raise ValueError(value)
    release = tuple(int(part) for part in match.group("release").split("."))
    major, minor, patch = (*release, *([0] * (3 - len(release))))
    stages = {None: 3, "a": 0, "b": 1, "rc": 2}
    return major, minor, patch, stages[match.group("pre")], int(match.group("pre_number") or 0)


def _valid_specifier(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    for item in value.split(","):
        match = SPECIFIER.fullmatch(item)
        if match is None or VERSION.fullmatch(match.group(2)) is None:
            return False
    return True


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "_", value).lower()


def _wheel_identity(name: str) -> tuple[str, str] | None:
    if not name.endswith(".whl"):
        return None
    parts = name.removesuffix(".whl").split("-")
    if len(parts) not in {5, 6}:
        return None
    distribution, version = parts[:2]
    if (
        DISTRIBUTION.fullmatch(distribution) is None
        or VERSION.fullmatch(version) is None
        or (len(parts) == 6 and BUILD_TAG.fullmatch(parts[2]) is None)
        or PYTHON_TAG.fullmatch(parts[-3]) is None
        or WHEEL_TAG.fullmatch(parts[-2]) is None
        or WHEEL_TAG.fullmatch(parts[-1]) is None
    ):
        return None
    return _normalized_distribution(distribution), version


def _parse_matcher(value: Any, code: str, path: Path) -> Matcher:
    item = _object(value, {"kind", "value"}, code, path, "matcher")
    kind = item["kind"]
    matcher_value = item["value"]
    if (
        not isinstance(kind, str)
        or kind not in MATCHER_KINDS
        or not isinstance(matcher_value, str)
        or not matcher_value
    ):
        _invalid(code, path, "matcher kind or value is unsupported")
    if kind == "file":
        pure = PurePosixPath(matcher_value)
        if pure.is_absolute() or ".." in pure.parts:
            _invalid(code, path, "file matcher must be a confined relative path")
    elif kind == "file_suffix" and (not matcher_value.startswith(".") or "/" in matcher_value):
        _invalid(code, path, "file suffix matcher is invalid")
    elif kind == "dependency" and _dependency_name(matcher_value) != matcher_value:
        _invalid(code, path, "dependency matcher must be normalized")
    elif kind == "static_import" and IMPORT.fullmatch(matcher_value) is None:
        _invalid(code, path, "static import matcher is invalid")
    elif kind in {"framework", "language"} and IDENTIFIER.fullmatch(matcher_value) is None:
        _invalid(code, path, f"{kind} matcher is invalid")
    return Matcher(kind, matcher_value)


def _load_json(path: Path, code: str, *, data: bytes | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8") if data is None else data.decode("utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _invalid(code, path, str(error))
    if not isinstance(payload, dict):
        _invalid(code, path, "document must be an object")
    return payload


def _load_manifest(path: Path, marketplace: str, *, data: bytes | None = None) -> Manifest:
    code = "SANKA_EXTENSION_MANIFEST_INVALID"
    raw = _load_json(path, code, data=data)
    kind = raw.get("kind")
    common = {
        "distribution",
        "id",
        "kind",
        "protocol_version",
        "runtime",
        "schema_version",
        "version",
        "wheels",
    }
    if raw.get("schema_version") != "sanka-extension-manifest/v2":
        _invalid(code, path, "unsupported manifest schema version")
    if kind == "migration":
        keys = common | {"commands", "match", "targets"}
    elif kind == "connector":
        keys = common | {"providers"}
    else:
        _invalid(code, path, "manifest kind is unsupported")
    payload = _object(
        raw,
        keys,
        code,
        path,
        "manifest",
    )
    if not _valid_id(payload["id"]):
        _invalid(code, path, "extension id must be <owner>/<name>")
    version = payload["version"]
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        _invalid(code, path, "extension version must be exact")
    protocol = "sanka-extension/v1" if kind == "migration" else "sanka-connector/v1"
    if payload["protocol_version"] != protocol:
        _invalid(code, path, "manifest kind and protocol version differ")

    distribution_key = "executable" if kind == "migration" else "entry_point"
    distribution = _object(
        payload["distribution"],
        {distribution_key, "name", "version"},
        code,
        path,
        "distribution",
    )
    if (
        not isinstance(distribution["name"], str)
        or DISTRIBUTION.fullmatch(distribution["name"]) is None
        or not isinstance(distribution[distribution_key], str)
        or DISTRIBUTION.fullmatch(distribution[distribution_key]) is None
        or distribution["version"] != version
    ):
        _invalid(code, path, "distribution metadata is invalid or version is not exact")

    commands: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    match_all: tuple[Matcher, ...] = ()
    match_any: tuple[Matcher, ...] = ()
    providers: tuple[Provider, ...] = ()
    if kind == "migration":
        commands = _string_list(payload["commands"], code=code, path=path, label="commands")
        if not set(commands).issubset(LIFECYCLE_COMMANDS):
            _invalid(code, path, "manifest contains an unsupported command")
        targets = _string_list(payload["targets"], code=code, path=path, label="targets")
        if any(IDENTIFIER.fullmatch(target) is None for target in targets):
            _invalid(code, path, "manifest target is invalid")
        match = _object(payload["match"], {"all", "any"}, code, path, "match")
        all_values = match["all"]
        any_values = match["any"]
        if not isinstance(all_values, list) or not isinstance(any_values, list):
            _invalid(code, path, "match all and any must be arrays")
        match_all = tuple(_parse_matcher(item, code, path) for item in all_values)
        match_any = tuple(_parse_matcher(item, code, path) for item in any_values)
        if not match_all and not match_any:
            _invalid(code, path, "manifest must declare at least one matcher")
        if len(set(match_all)) != len(match_all) or len(set(match_any)) != len(match_any):
            _invalid(code, path, "manifest matchers must be unique")
    else:
        raw_providers = payload["providers"]
        if not isinstance(raw_providers, list) or not raw_providers:
            _invalid(code, path, "providers must be a non-empty array")
        parsed_providers: list[Provider] = []
        for raw_provider in raw_providers:
            provider = _object(raw_provider, {"name", "roles"}, code, path, "provider")
            name = provider["name"]
            roles = _string_list(provider["roles"], code=code, path=path, label="roles")
            if (
                not isinstance(name, str)
                or IDENTIFIER.fullmatch(name) is None
                or not set(roles).issubset({"source", "destination"})
            ):
                _invalid(code, path, "provider name or role is unsupported")
            parsed_providers.append(
                Provider(
                    name,
                    tuple(role for role in ("source", "destination") if role in roles),
                )
            )
        if len({provider.name for provider in parsed_providers}) != len(parsed_providers):
            _invalid(code, path, "provider names must be unique")
        if distribution["entry_point"] not in {provider.name for provider in parsed_providers}:
            _invalid(code, path, "connector entry point must name a declared provider")
        providers = tuple(sorted(parsed_providers, key=lambda provider: provider.name))

    runtime = _object(payload["runtime"], {"sanka_cli"}, code, path, "runtime")
    runtime_specifier = runtime["sanka_cli"]
    if not _valid_specifier(runtime_specifier):
        _invalid(code, path, "runtime compatibility range is invalid")

    raw_wheels = payload["wheels"]
    if not isinstance(raw_wheels, list) or not raw_wheels:
        _invalid(code, path, "wheels must be a non-empty array")
    wheels: list[Wheel] = []
    wheel_identities: list[tuple[str, str]] = []
    for raw_wheel in raw_wheels:
        wheel = _object(raw_wheel, {"name", "sha256", "url"}, code, path, "wheel")
        name, url, digest = wheel["name"], wheel["url"], wheel["sha256"]
        try:
            parsed_url = urlparse(url) if isinstance(url, str) else None
            url_name = Path(parsed_url.path).name if parsed_url is not None else None
        except (TypeError, ValueError):
            _invalid(code, path, "wheel URL is invalid")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".whl")
            or parsed_url is None
            or parsed_url.scheme != "https"
            or not parsed_url.netloc
            or url_name != name
            or not isinstance(digest, str)
            or SHA256.fullmatch(digest) is None
        ):
            _invalid(code, path, "wheel metadata is invalid")
        identity = _wheel_identity(name)
        if identity is None:
            _invalid(code, path, "wheel filename is invalid")
        wheels.append(Wheel(name, url, digest))
        wheel_identities.append(identity)
    if len({wheel.name for wheel in wheels}) != len(wheels):
        _invalid(code, path, "wheel names must be unique")
    expected_identity = (_normalized_distribution(distribution["name"]), version)
    if expected_identity not in wheel_identities:
        _invalid(code, path, "manifest does not contain its exact distribution wheel")

    return Manifest(
        id=payload["id"],
        version=version,
        marketplace=marketplace,
        kind=kind,
        protocol_version=payload["protocol_version"],
        distribution=distribution["name"],
        distribution_version=distribution["version"],
        executable=distribution.get("executable"),
        entry_point=distribution.get("entry_point"),
        providers=providers,
        commands=tuple(sorted(commands)),
        match_all=match_all,
        match_any=match_any,
        targets=tuple(sorted(targets)),
        runtime_sanka_cli=runtime_specifier,
        wheels=tuple(sorted(wheels, key=lambda item: item.name)),
        digest=content_hash(payload),
    )


def _confined_marketplace_path(root: Path, candidate: Path, display: str) -> Path:
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ExtensionError(
            "SANKA_MARKETPLACE_PATH_INVALID",
            "Marketplace path cannot be resolved inside the snapshot",
            details={"path": display},
        ) from error
    if not resolved.is_relative_to(root):
        raise ExtensionError(
            "SANKA_MARKETPLACE_PATH_INVALID",
            "Marketplace path escapes the snapshot",
            details={"path": display},
        )
    return resolved


def _close_descriptors(descriptors: Iterable[int]) -> None:
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


def _descriptor_bytes(
    root_descriptor: int, relative: PurePosixPath, path: Path, code: str
) -> bytes:
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} or "\\" in part or "\0" in part for part in relative.parts)
    ):
        _invalid(code, path, "marketplace path is not confined to its snapshot")
    parent = root_descriptor
    opened: list[int] = []
    try:
        for part in relative.parts[:-1]:
            parent = os.open(
                part,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            opened.append(parent)
        descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        opened.append(descriptor)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            _invalid(code, path, "marketplace document must be one regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    except ExtensionError:
        raise
    except OSError as error:
        _invalid(code, path, str(error))
    finally:
        _close_descriptors(reversed(opened))


def load_marketplace(
    snapshot_root: Path, *, root_descriptor: int | None = None
) -> tuple[Manifest, ...]:
    """Load one immutable marketplace snapshot with strict path confinement."""
    code = "SANKA_MARKETPLACE_INVALID"
    if root_descriptor is None:
        try:
            root = snapshot_root.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise ExtensionError(
                "SANKA_MARKETPLACE_PATH_INVALID",
                "Marketplace snapshot root cannot be resolved",
                details={"path": snapshot_root.as_posix()},
            ) from error
        catalog_path = _confined_marketplace_path(
            root, root / "marketplace.json", "marketplace.json"
        )
        catalog_data = None
    else:
        root = snapshot_root
        catalog_path = root / "marketplace.json"
        catalog_data = _descriptor_bytes(
            root_descriptor,
            PurePosixPath("marketplace.json"),
            catalog_path,
            code,
        )
    catalog = _object(
        _load_json(catalog_path, code, data=catalog_data),
        {"extensions", "schema_version"},
        code,
        catalog_path,
        "catalog",
    )
    if catalog["schema_version"] != "sanka-marketplace/v1":
        _invalid(code, catalog_path, "unsupported marketplace schema version")
    entries = catalog["extensions"]
    if not isinstance(entries, list):
        _invalid(code, catalog_path, "extensions must be an array")
    manifests: list[Manifest] = []
    seen: set[str] = set()
    for raw_entry in entries:
        entry = _object(raw_entry, {"id", "manifest"}, code, catalog_path, "catalog entry")
        extension_id = entry["id"]
        manifest_name = entry["manifest"]
        if not _valid_id(extension_id) or not isinstance(manifest_name, str) or not manifest_name:
            _invalid(code, catalog_path, "catalog entry is invalid")
        if extension_id in seen:
            _invalid(code, catalog_path, "catalog extension ids must be unique")
        seen.add(extension_id)
        if root_descriptor is None:
            candidate = _confined_marketplace_path(root, root / manifest_name, manifest_name)
            manifest_data = None
        else:
            relative = PurePosixPath(manifest_name)
            candidate = root / relative
            manifest_data = _descriptor_bytes(root_descriptor, relative, candidate, code)
        manifest = _load_manifest(candidate, snapshot_root.name, data=manifest_data)
        if manifest.id != extension_id:
            _invalid(code, catalog_path, "catalog and manifest extension ids differ")
        manifests.append(manifest)
    return tuple(sorted(manifests, key=lambda item: (item.id, _version_key(item.version))))


def _compatible(specifier: str, version: str) -> bool:
    current = _version_key(version)
    for item in specifier.split(","):
        match = SPECIFIER.fullmatch(item)
        if match is None:
            return False
        expected = _version_key(match.group(2))
        operator = match.group(1)
        if operator == "==" and current != expected:
            return False
        if operator == ">=" and current < expected:
            return False
        if operator == ">" and current <= expected:
            return False
        if operator == "<=" and current > expected:
            return False
        if operator == "<" and current >= expected:
            return False
    return True


def recommend(
    fingerprint: Fingerprint,
    manifests: Iterable[Manifest],
    state: Mapping[str, str],
) -> tuple[Recommendation, ...]:
    """Match fingerprint evidence with one-level all/any manifest semantics."""
    evidence_by_matcher: dict[tuple[str, str], list[MatchedEvidence]] = {}
    for evidence in fingerprint.evidence:
        evidence_by_matcher.setdefault((evidence.kind, evidence.value), []).append(evidence)
    recommendations: list[Recommendation] = []
    for manifest in manifests:
        if manifest.kind != "migration":
            continue
        all_matches = [
            evidence_by_matcher.get((matcher.kind, matcher.value), [])
            for matcher in manifest.match_all
        ]
        any_matches = [
            evidence_by_matcher.get((matcher.kind, matcher.value), [])
            for matcher in manifest.match_any
        ]
        if any(not matches for matches in all_matches) or (
            manifest.match_any and not any(any_matches)
        ):
            continue
        selected = {matches[0] for matches in all_matches if matches}
        selected.update(matches[0] for matches in any_matches if matches)
        statuses = {"available"}
        current_state = state.get(manifest.id)
        if current_state in STATE_VALUES:
            statuses.add(current_state)
        if not _compatible(manifest.runtime_sanka_cli, __version__):
            statuses.add("incompatible")
        recommendations.append(
            Recommendation(
                id=manifest.id,
                version=manifest.version,
                marketplace=manifest.marketplace,
                marketplace_identity=manifest.marketplace,
                snapshot_digest="",
                manifest_digest=manifest.digest,
                commands=manifest.commands,
                targets=manifest.targets,
                evidence=tuple(
                    sorted(selected, key=lambda item: (item.kind, item.value, item.path))
                ),
                status=tuple(status for status in STATUS_ORDER if status in statuses),
                add_command=f"sanka-migrate extension add {manifest.id}",
            )
        )
    return tuple(sorted(recommendations, key=lambda item: (item.id, _version_key(item.version))))
