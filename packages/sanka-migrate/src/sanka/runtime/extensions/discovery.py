# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded static project fingerprinting and strict marketplace matching."""

from __future__ import annotations

import ast
import json
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn
from urllib.parse import urlparse

from sanka.runtime.__about__ import __version__
from sanka.runtime.extensions.model import (
    ExtensionError,
    Fingerprint,
    Manifest,
    MatchedEvidence,
    Matcher,
    Recommendation,
    Wheel,
)
from sanka.runtime.hashing import content_hash

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
COMMANDS = frozenset({"apply", "plan", "scan", "test", "verify"})
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
            if path.is_symlink() or not path.is_file():
                continue
            file_count += 1
            if file_count > MAX_FILES:
                raise ExtensionError(
                    "SANKA_FINGERPRINT_FILE_LIMIT",
                    "Repository exceeds the static fingerprint file limit",
                    details={"limit": MAX_FILES},
                )
            relative = path.relative_to(root).as_posix()
            suffix = path.suffix.lower()
            evidence.add(MatchedEvidence("file", relative, relative))
            if suffix:
                evidence.add(MatchedEvidence("file_suffix", suffix, relative))
            language = LANGUAGES.get(suffix)
            if language:
                evidence.add(MatchedEvidence("language", language, relative))
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > MAX_SOURCE_BYTES:
                continue
            try:
                data = path.read_bytes()
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
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
    digest = content_hash(
        {
            "dependencies": dependency_names,
            "evidence": [item.__dict__ for item in ordered],
            "frameworks": framework_names,
            "languages": languages,
        }
    )
    return Fingerprint(languages, framework_names, dependency_names, ordered, digest)


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


def _parse_matcher(value: Any, code: str, path: Path) -> Matcher:
    item = _object(value, {"kind", "value"}, code, path, "matcher")
    kind = item["kind"]
    matcher_value = item["value"]
    if kind not in MATCHER_KINDS or not isinstance(matcher_value, str) or not matcher_value:
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


def _load_json(path: Path, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _invalid(code, path, str(error))
    if not isinstance(payload, dict):
        _invalid(code, path, "document must be an object")
    return payload


def _load_manifest(path: Path, marketplace: str) -> Manifest:
    code = "SANKA_EXTENSION_MANIFEST_INVALID"
    payload = _object(
        _load_json(path, code),
        {
            "commands",
            "distribution",
            "id",
            "match",
            "protocol_version",
            "runtime",
            "schema_version",
            "targets",
            "version",
            "wheels",
        },
        code,
        path,
        "manifest",
    )
    if payload["schema_version"] != "sanka-extension-manifest/v1":
        _invalid(code, path, "unsupported manifest schema version")
    if not _valid_id(payload["id"]):
        _invalid(code, path, "extension id must be <owner>/<name>")
    version = payload["version"]
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        _invalid(code, path, "extension version must be exact")
    if payload["protocol_version"] != "sanka-extension/v1":
        _invalid(code, path, "unsupported extension protocol version")

    distribution = _object(
        payload["distribution"],
        {"executable", "name", "version"},
        code,
        path,
        "distribution",
    )
    if (
        not isinstance(distribution["name"], str)
        or DISTRIBUTION.fullmatch(distribution["name"]) is None
        or not isinstance(distribution["executable"], str)
        or DISTRIBUTION.fullmatch(distribution["executable"]) is None
        or distribution["version"] != version
    ):
        _invalid(code, path, "distribution metadata is invalid or version is not exact")

    commands = _string_list(payload["commands"], code=code, path=path, label="commands")
    if not set(commands).issubset(COMMANDS):
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

    runtime = _object(payload["runtime"], {"sanka_migrate"}, code, path, "runtime")
    runtime_specifier = runtime["sanka_migrate"]
    if not _valid_specifier(runtime_specifier):
        _invalid(code, path, "runtime compatibility range is invalid")

    raw_wheels = payload["wheels"]
    if not isinstance(raw_wheels, list) or not raw_wheels:
        _invalid(code, path, "wheels must be a non-empty array")
    wheels: list[Wheel] = []
    for raw_wheel in raw_wheels:
        wheel = _object(raw_wheel, {"name", "sha256", "url"}, code, path, "wheel")
        name, url, digest = wheel["name"], wheel["url"], wheel["sha256"]
        parsed_url = urlparse(url) if isinstance(url, str) else None
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".whl")
            or parsed_url is None
            or parsed_url.scheme != "https"
            or not parsed_url.netloc
            or Path(parsed_url.path).name != name
            or not isinstance(digest, str)
            or SHA256.fullmatch(digest) is None
        ):
            _invalid(code, path, "wheel metadata is invalid")
        wheels.append(Wheel(name, url, digest))
    if len({wheel.name for wheel in wheels}) != len(wheels):
        _invalid(code, path, "wheel names must be unique")
    distribution_prefix = f"{distribution['name'].replace('-', '_')}-{version}-"
    if not any(wheel.name.startswith(distribution_prefix) for wheel in wheels):
        _invalid(code, path, "manifest does not contain its exact distribution wheel")

    return Manifest(
        id=payload["id"],
        version=version,
        marketplace=marketplace,
        protocol_version=payload["protocol_version"],
        distribution=distribution["name"],
        distribution_version=distribution["version"],
        executable=distribution["executable"],
        commands=tuple(sorted(commands)),
        match_all=match_all,
        match_any=match_any,
        targets=tuple(sorted(targets)),
        runtime_sanka_migrate=runtime_specifier,
        wheels=tuple(sorted(wheels, key=lambda item: item.name)),
        digest=content_hash(payload),
    )


def load_marketplace(snapshot_root: Path) -> tuple[Manifest, ...]:
    """Load one immutable marketplace snapshot with strict path confinement."""
    root = snapshot_root.resolve()
    code = "SANKA_MARKETPLACE_INVALID"
    catalog_path = root / "marketplace.json"
    catalog = _object(
        _load_json(catalog_path, code),
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
        candidate = (root / manifest_name).resolve()
        if not candidate.is_relative_to(root):
            raise ExtensionError(
                "SANKA_MARKETPLACE_PATH_INVALID",
                "Catalog manifest path escapes the marketplace snapshot",
                details={"path": manifest_name},
            )
        manifest = _load_manifest(candidate, snapshot_root.name)
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
        if not _compatible(manifest.runtime_sanka_migrate, __version__):
            statuses.add("incompatible")
        recommendations.append(
            Recommendation(
                id=manifest.id,
                version=manifest.version,
                marketplace=manifest.marketplace,
                targets=manifest.targets,
                evidence=tuple(
                    sorted(selected, key=lambda item: (item.kind, item.value, item.path))
                ),
                status=tuple(status for status in STATUS_ORDER if status in statuses),
                add_command=f"sanka-migrate extension add {manifest.id}",
            )
        )
    return tuple(sorted(recommendations, key=lambda item: (item.id, _version_key(item.version))))
