# SPDX-License-Identifier: AGPL-3.0-only
"""Core-owned application lifecycle dispatched through immutable extension locks."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

from sanka.runtime.extensions.discovery import fingerprint_repository
from sanka.runtime.extensions.model import (
    ExtensionError,
    Fingerprint,
    MatchedEvidence,
    Recommendation,
)
from sanka.runtime.extensions.runner import (
    ExtensionResult,
    ExtensionRunner,
    _canonical_environment_names,
)
from sanka.runtime.extensions.store import DEFAULT_EXTENSION_ID, ExtensionStore, LockEntry
from sanka.runtime.hashing import content_hash
from sanka.runtime.safe_local_io import (
    ensure_safe_directory,
    safe_read_bytes,
    safe_read_text,
    safe_write_text,
)

SCAN_SCHEMA = "sanka-application-scan/v1"
PLAN_SCHEMA = "sanka-application-plan/v1"
MAX_ARTIFACT_FILES = 20_000
PHASE_CONFIGURATION = frozenset(
    {"bench_candidate", "cases", "force", "gap_report_only", "min_readiness", "no_http"}
)
type Prompt = Callable[[str, tuple[str, ...] | None], str | None]


def _error(code: str, message: str, **details: Any) -> NoReturn:
    raise ExtensionError(code, message, details=details)


def _evidence(value: MatchedEvidence) -> dict[str, str]:
    return {"kind": value.kind, "path": value.path, "value": value.value}


def _fingerprint(value: Fingerprint) -> dict[str, Any]:
    return {
        "dependencies": list(value.dependencies),
        "evidence": [_evidence(item) for item in value.evidence],
        "frameworks": list(value.frameworks),
        "hash": value.hash,
        "languages": list(value.languages),
    }


def _recommendation(value: Recommendation) -> dict[str, Any]:
    return {
        "add_command": value.add_command,
        "evidence": [_evidence(item) for item in value.evidence],
        "id": value.id,
        "marketplace": value.marketplace,
        "marketplace_identity": value.marketplace_identity,
        "snapshot_digest": value.snapshot_digest,
        "manifest_digest": value.manifest_digest,
        "commands": list(value.commands),
        "status": list(value.status),
        "targets": list(value.targets),
        "version": value.version,
    }


def _serialized_recommendations(values: tuple[Recommendation, ...]) -> list[dict[str, Any]]:
    return sorted(
        (_recommendation(value) for value in values),
        key=lambda value: json.dumps(value, separators=(",", ":"), sort_keys=True),
    )


def _normalized_json_object(value: Mapping[str, Any] | None) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            dict(value or {}),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExtensionError(
            "SANKA_EXTENSION_CONFIGURATION_INVALID",
            "Extension configuration must be a JSON object",
            details={"reason": str(error)},
        ) from error
    if not isinstance(decoded, dict):
        _error(
            "SANKA_EXTENSION_CONFIGURATION_INVALID",
            "Extension configuration must be a JSON object",
        )
    return decoded


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    return safe_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )


def _read_json(path: Path, schema: str) -> dict[str, Any]:
    try:
        payload = json.loads(safe_read_text(path, max_bytes=16 * 1024 * 1024))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExtensionError(
            "SANKA_EXTENSION_IDENTITY",
            "Core extension lifecycle artifact is missing or invalid",
            details={"path": str(path), "reason": str(error)},
        ) from error
    if not isinstance(payload, dict) or payload.get("schema_version") != schema:
        _error(
            "SANKA_EXTENSION_IDENTITY",
            "Core extension lifecycle artifact has an unsupported schema",
            path=str(path),
        )
    return payload


def _file_digest(path: Path) -> str:
    return hashlib.sha256(safe_read_bytes(path)).hexdigest()


def _artifact_digest(path: Path) -> str:
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


class ApplicationLifecycle:
    """Keep the lifecycle generic while extensions own target behavior."""

    def __init__(
        self,
        project_root: Path,
        *,
        artifact_dir: str | Path = ".sanka",
        store: ExtensionStore | None = None,
        runner: ExtensionRunner | None = None,
        interactive: bool = False,
        prompt: Prompt | None = None,
    ) -> None:
        self.project_root = project_root.expanduser().resolve(strict=True)
        artifact = Path(artifact_dir)
        self.artifact_root = (
            artifact if artifact.is_absolute() else self.project_root / artifact
        ).resolve()
        self.store = store or ExtensionStore(self.project_root)
        user_root = getattr(self.store, "user_root", None)
        self.runner = runner or ExtensionRunner(user_root=user_root)
        self.interactive = interactive
        self.prompt = prompt

    def _prompt(self, label: str, choices: tuple[str, ...] | None = None) -> str | None:
        if not self.interactive or self.prompt is None:
            return None
        return self.prompt(label, choices)

    def _recommendations(self, fingerprint: Fingerprint) -> tuple[Recommendation, ...]:
        return self.store.recommendations(fingerprint)

    @staticmethod
    def _enabled(values: tuple[Recommendation, ...]) -> tuple[Recommendation, ...]:
        return tuple(
            item
            for item in values
            if "locked" in item.status
            and "disabled" not in item.status
            and "incompatible" not in item.status
        )

    def _required(
        self,
        fingerprint: Fingerprint,
        recommendations: tuple[Recommendation, ...],
    ) -> NoReturn:
        _error(
            "SANKA_EXTENSION_REQUIRED",
            "A compatible migration extension must be installed and enabled",
            fingerprint=_fingerprint(fingerprint),
            recommendations=[_recommendation(item) for item in recommendations],
        )

    def _ensure_enabled(
        self,
        fingerprint: Fingerprint,
        recommendations: tuple[Recommendation, ...],
    ) -> tuple[Recommendation, ...]:
        if self._enabled(recommendations) or not recommendations:
            return recommendations
        default = next(
            (
                item
                for item in recommendations
                if item.id == DEFAULT_EXTENSION_ID
                and "disabled" not in item.status
                and "incompatible" not in item.status
            ),
            None,
        )
        if default is not None:
            try:
                self.store.add_extension(default.id, marketplace=default.marketplace_identity)
            except ExtensionError as error:
                if error.code != "SANKA_EXTENSION_NOT_CACHED":
                    raise
            else:
                recommendations = self._recommendations(fingerprint)
                if self._enabled(recommendations):
                    return recommendations
        eligible = tuple(item for item in recommendations if "incompatible" not in item.status)
        labels = tuple(f"{item.id} ({item.marketplace_identity})" for item in eligible)
        selected = self._prompt("Choose an extension to install", ("decline", *labels))
        if selected and selected != "decline":
            chosen = next(
                (item for item, label in zip(eligible, labels, strict=True) if label == selected),
                None,
            )
            if chosen is None:
                _error(
                    "SANKA_EXTENSION_SELECTION_INVALID",
                    "Selected extension is not one of the recommendations",
                    selection=selected,
                )
            self.store.add_extension(chosen.id, marketplace=chosen.marketplace_identity)
            recommendations = self._recommendations(fingerprint)
            if self._enabled(recommendations):
                return recommendations
        self._required(fingerprint, recommendations)

    def _extension_root(self, lock: LockEntry) -> Path:
        owner, name = lock.id.split("/", 1)
        return ensure_safe_directory(self.artifact_root / "extensions" / owner / name)

    @staticmethod
    def _verify_selection(lock: LockEntry, recommendation: Recommendation) -> None:
        if any(
            actual != expected
            for actual, expected in (
                (lock.id, recommendation.id),
                (lock.version, recommendation.version),
                (lock.marketplace_identity, recommendation.marketplace_identity),
                (lock.snapshot_digest, recommendation.snapshot_digest),
                (lock.manifest_digest, recommendation.manifest_digest),
                (lock.commands, recommendation.commands),
            )
        ):
            _error(
                "SANKA_EXTENSION_IDENTITY",
                "Resolved extension does not match its exact recommendation",
                extension_id=recommendation.id,
            )

    def _request(
        self,
        lock: LockEntry,
        command: str,
        fingerprint: Fingerprint,
        configuration: dict[str, Any],
        *,
        prior_artifacts: tuple[str, ...] = (),
        reviewed_plan_hash: str | None = None,
    ) -> dict[str, Any]:
        return {
            "artifact_root": str(self._extension_root(lock)),
            "command": command,
            "configuration": configuration,
            "extension": {
                "id": lock.id,
                "manifest_digest": lock.manifest_digest.removeprefix("sha256:"),
                "version": lock.version,
            },
            "fingerprint": _fingerprint(fingerprint),
            "prior_artifacts": list(prior_artifacts),
            "project_root": str(self.project_root),
            "request_id": uuid.uuid4().hex,
            "reviewed_plan_hash": reviewed_plan_hash,
            "schema_version": "sanka-extension/v1",
        }

    @staticmethod
    def _raise_failure(result: ExtensionResult) -> NoReturn:
        error = result.error or {
            "code": "SANKA_EXTENSION_EXECUTION_FAILED",
            "message": "Extension returned an unsuccessful outcome",
            "details": {},
        }
        raise ExtensionError(
            str(error["code"]),
            str(error["message"]),
            details=error.get("details") if isinstance(error.get("details"), dict) else {},
        )

    @staticmethod
    def _roots(
        configuration: Mapping[str, Any], extension_root: Path, project_root: Path
    ) -> tuple[Path, ...]:
        roots = [extension_root]
        for name in ("output", "bench_candidate"):
            value = configuration.get(name)
            if isinstance(value, str) and value:
                path = Path(value)
                roots.append((path if path.is_absolute() else project_root / path).resolve())
        return tuple(roots)

    def scan(
        self,
        *,
        configuration: Mapping[str, Any] | None = None,
        explicit_env_names: tuple[str, ...] = (),
    ) -> ExtensionResult:
        explicit_env_names = _canonical_environment_names(explicit_env_names)
        fingerprint = fingerprint_repository(self.project_root)
        self._ensure_enabled(
            fingerprint,
            self._recommendations(fingerprint),
        )
        normalized = _normalized_json_object(configuration)
        with self.store.execution_guard():
            recommendations = self._recommendations(fingerprint)
            if recommendations and not self._enabled(recommendations):
                self._required(fingerprint, recommendations)
            return self._scan_locked(
                fingerprint,
                recommendations,
                self._scan_locks(recommendations),
                normalized,
                explicit_env_names,
            )

    def _scan_locks(
        self,
        recommendations: tuple[Recommendation, ...],
    ) -> tuple[LockEntry, ...]:
        locks: list[LockEntry] = []
        for recommendation in self._enabled(recommendations):
            if "scan" not in recommendation.commands:
                continue
            lock = self.store.resolve_locked(recommendation.id)
            self._verify_selection(lock, recommendation)
            locks.append(lock)
        return tuple(locks)

    def _scan_locked(
        self,
        fingerprint: Fingerprint,
        recommendations: tuple[Recommendation, ...],
        locks: tuple[LockEntry, ...],
        normalized: dict[str, Any],
        explicit_env_names: tuple[str, ...],
    ) -> ExtensionResult:
        extension_results: list[tuple[LockEntry, ExtensionResult]] = []
        for lock in locks:
            request = self._request(lock, "scan", fingerprint, normalized)
            extension_root = Path(request["artifact_root"])
            with self.store.execution_lease(lock) as executable_fd:
                extension_result = self.runner.run(
                    lock,
                    request,
                    allowed_roots=(extension_root,),
                    explicit_env_names=explicit_env_names,
                    executable_fd=executable_fd,
                )
            if extension_result.outcome != "success":
                self._raise_failure(extension_result)
            extension_results.append((lock, extension_result))
        data: dict[str, Any] = {
            "extensions": [
                {"data": result.data, "extension": lock.to_dict()}
                for lock, result in extension_results
            ]
        }
        if len(extension_results) == 1:
            extension_result = extension_results[0][1]
            data.update(extension_result.data)
            data["extension"] = extension_result.data
        data["fingerprint"] = _fingerprint(fingerprint)
        data["recommendations"] = _serialized_recommendations(recommendations)
        scan_payload = {
            "schema_version": SCAN_SCHEMA,
            "configuration": normalized,
            "explicit_env_names": list(explicit_env_names),
            **data,
        }
        scan_path = _write_json(self.artifact_root / "scan.json", scan_payload)
        return ExtensionResult(
            outcome="success",
            data=data,
            artifacts=(
                str(scan_path),
                *(artifact for _lock, result in extension_results for artifact in result.artifacts),
            ),
            limitations=tuple(
                limitation
                for _lock, result in extension_results
                for limitation in result.limitations
            ),
            next_actions=tuple(
                action for _lock, result in extension_results for action in result.next_actions
            ),
            error=None,
        )

    def _current_scan_locked(
        self,
        fingerprint: Fingerprint,
        recommendations: tuple[Recommendation, ...],
        normalized: dict[str, Any],
        explicit_env_names: tuple[str, ...],
    ) -> None:
        locks = self._scan_locks(recommendations)
        expected_locks = [lock.to_dict() for lock in locks]
        expected_recommendations = _serialized_recommendations(recommendations)
        try:
            scan = _read_json(self.artifact_root / "scan.json", SCAN_SCHEMA)
        except ExtensionError:
            pass
        else:
            saved = scan.get("fingerprint")
            extensions = scan.get("extensions")
            saved_locks = (
                [item.get("extension") for item in extensions]
                if isinstance(extensions, list)
                and all(isinstance(item, dict) for item in extensions)
                else None
            )
            if (
                isinstance(saved, dict)
                and saved.get("hash") == fingerprint.hash
                and scan.get("configuration") == normalized
                and scan.get("explicit_env_names") == list(explicit_env_names)
                and saved_locks == expected_locks
                and scan.get("recommendations") == expected_recommendations
                and not explicit_env_names
            ):
                return
        self._scan_locked(
            fingerprint,
            recommendations,
            locks,
            normalized,
            explicit_env_names,
        )

    def plan(
        self,
        *,
        target: str | None,
        configuration: Mapping[str, Any] | None = None,
        explicit_env_names: tuple[str, ...] = (),
    ) -> ExtensionResult:
        explicit_env_names = _canonical_environment_names(explicit_env_names)
        normalized = _normalized_json_object(configuration)
        fingerprint = fingerprint_repository(self.project_root)
        self._ensure_enabled(
            fingerprint,
            self._recommendations(fingerprint),
        )
        with self.store.execution_guard():
            recommendations = self._recommendations(fingerprint)
            if recommendations and not self._enabled(recommendations):
                self._required(fingerprint, recommendations)
            self._current_scan_locked(
                fingerprint,
                recommendations,
                normalized,
                explicit_env_names,
            )
            return self._plan_locked(
                fingerprint,
                recommendations,
                target,
                normalized,
                explicit_env_names,
            )

    def _plan_locked(
        self,
        fingerprint: Fingerprint,
        recommendations: tuple[Recommendation, ...],
        target: str | None,
        normalized: dict[str, Any],
        explicit_env_names: tuple[str, ...],
    ) -> ExtensionResult:
        enabled = self._enabled(recommendations)
        targets = tuple(sorted({target for item in enabled for target in item.targets}))
        selected_target = target or self._prompt("Choose a migration target", targets)
        if selected_target is None:
            _error(
                "SANKA_EXTENSION_TARGET_REQUIRED",
                "A migration target is required outside an interactive terminal",
                targets=list(targets),
            )
        selected = tuple(item for item in enabled if selected_target in item.targets)
        if not selected:
            _error(
                "SANKA_EXTENSION_REQUIRED",
                "No enabled extension advertises the selected target",
                target=selected_target,
                recommendations=[_recommendation(item) for item in recommendations],
            )
        if len(selected) != 1:
            _error(
                "SANKA_EXTENSION_AMBIGUOUS",
                "More than one enabled extension advertises the selected target",
                target=selected_target,
                extensions=[item.id for item in selected],
            )
        lock = self.store.resolve_locked(selected[0].id)
        self._verify_selection(lock, selected[0])
        request = self._request(lock, "plan", fingerprint, normalized)
        extension_root = Path(request["artifact_root"])
        seen_inputs: set[str] = set()
        while True:
            with self.store.execution_lease(lock) as executable_fd:
                result = self.runner.run(
                    lock,
                    request,
                    allowed_roots=self._roots(normalized, extension_root, self.project_root),
                    explicit_env_names=explicit_env_names,
                    executable_fd=executable_fd,
                )
            if result.outcome == "success":
                break
            details = (result.error or {}).get("details")
            inputs = details.get("inputs") if isinstance(details, dict) else None
            if (
                not self.interactive
                or (result.error or {}).get("code") != "SANKA_EXTENSION_INPUT_REQUIRED"
                or not isinstance(inputs, list)
                or any(
                    not isinstance(name, str) or not name or name in seen_inputs for name in inputs
                )
            ):
                self._raise_failure(result)
            for name in inputs:
                answer = self._prompt(f"Extension configuration: {name}")
                if answer is None:
                    self._raise_failure(result)
                normalized[name] = answer
                seen_inputs.add(name)
            request["configuration"] = normalized
        extension_plan_hash = result.data.get("plan_hash")
        if not isinstance(extension_plan_hash, str) or not extension_plan_hash:
            _error(
                "SANKA_EXTENSION_PROTOCOL",
                "Extension plan response must include its plan_hash",
            )
        digests = {artifact: _artifact_digest(Path(artifact)) for artifact in result.artifacts}
        payload: dict[str, Any] = {
            "artifact_digests": digests,
            "artifacts": sorted(result.artifacts),
            "configuration": normalized,
            "extension": lock.to_dict(),
            "extension_plan": result.data,
            "fingerprint_hash": fingerprint.hash,
            "limitations": sorted(result.limitations),
            "schema_version": PLAN_SCHEMA,
        }
        payload["plan_hash"] = content_hash(payload)
        plan_path = _write_json(self.artifact_root / "plan.json", payload)
        data = dict(result.data)
        data["extension"] = result.data
        data["plan_hash"] = payload["plan_hash"]
        return ExtensionResult(
            outcome="success",
            data=data,
            artifacts=(str(plan_path), *result.artifacts),
            limitations=result.limitations,
            next_actions=result.next_actions,
            error=None,
        )

    def _load_plan(self) -> dict[str, Any]:
        payload = _read_json(self.artifact_root / "plan.json", PLAN_SCHEMA)
        reviewed_hash = payload.get("plan_hash")
        unsigned = {key: value for key, value in payload.items() if key != "plan_hash"}
        if not isinstance(reviewed_hash, str) or content_hash(unsigned) != reviewed_hash:
            _error("SANKA_EXTENSION_IDENTITY", "Core application plan hash is invalid")
        return payload

    def _dispatch(
        self,
        command: str,
        *,
        reviewed_plan_hash: str | None = None,
        configuration: Mapping[str, Any] | None = None,
        explicit_env_names: tuple[str, ...] = (),
    ) -> ExtensionResult:
        with self.store.execution_guard():
            return self._dispatch_locked(
                command,
                reviewed_plan_hash=reviewed_plan_hash,
                configuration=configuration,
                explicit_env_names=explicit_env_names,
            )

    def _dispatch_locked(
        self,
        command: str,
        *,
        reviewed_plan_hash: str | None = None,
        configuration: Mapping[str, Any] | None = None,
        explicit_env_names: tuple[str, ...] = (),
    ) -> ExtensionResult:
        plan = self._load_plan()
        core_hash = plan["plan_hash"]
        if reviewed_plan_hash is not None and reviewed_plan_hash != core_hash:
            _error(
                "SANKA_EXTENSION_PLAN_HASH_MISMATCH",
                "Reviewed application plan hash does not match",
                current=core_hash,
                reviewed=reviewed_plan_hash,
            )
        fingerprint = fingerprint_repository(self.project_root)
        if fingerprint.hash != plan.get("fingerprint_hash"):
            _error(
                "SANKA_FINGERPRINT_STALE",
                "Source repository changed after the application plan was reviewed",
                current=fingerprint.hash,
                reviewed=plan.get("fingerprint_hash"),
            )
        locked = plan.get("extension")
        if not isinstance(locked, dict) or not isinstance(locked.get("id"), str):
            _error("SANKA_EXTENSION_IDENTITY", "Core application plan lock is invalid")
        lock = self.store.resolve_locked(locked["id"])
        if lock.to_dict() != locked:
            _error("SANKA_EXTENSION_IDENTITY", "Project extension lock changed after planning")
        artifacts = plan.get("artifacts")
        digests = plan.get("artifact_digests")
        if not isinstance(artifacts, list) or not isinstance(digests, dict):
            _error("SANKA_EXTENSION_IDENTITY", "Reviewed extension artifacts are invalid")
        for artifact in artifacts:
            if not isinstance(artifact, str) or digests.get(artifact) != _artifact_digest(
                Path(artifact)
            ):
                _error(
                    "SANKA_EXTENSION_IDENTITY",
                    "Reviewed extension artifact changed after planning",
                    artifact=artifact,
                )
        base = plan.get("configuration")
        if not isinstance(base, dict):
            _error("SANKA_EXTENSION_IDENTITY", "Reviewed extension configuration is invalid")
        normalized = _normalized_json_object(configuration)
        for name, value in normalized.items():
            if name not in PHASE_CONFIGURATION and (name not in base or base[name] != value):
                _error(
                    "SANKA_EXTENSION_PLAN_HASH_MISMATCH",
                    "Extension configuration changed after planning",
                    field=name,
                )
        merged = {**base, **normalized}
        extension_plan = plan.get("extension_plan")
        if not isinstance(extension_plan, dict) or not isinstance(
            extension_plan.get("plan_hash"), str
        ):
            _error("SANKA_EXTENSION_IDENTITY", "Reviewed extension plan is invalid")
        merged["extension_plan_hash"] = extension_plan["plan_hash"]
        request = self._request(
            lock,
            command,
            fingerprint,
            merged,
            prior_artifacts=tuple(artifacts),
            reviewed_plan_hash=core_hash,
        )
        extension_root = Path(request["artifact_root"])
        with self.store.execution_lease(lock) as executable_fd:
            result = self.runner.run(
                lock,
                request,
                allowed_roots=self._roots(merged, extension_root, self.project_root),
                explicit_env_names=explicit_env_names,
                executable_fd=executable_fd,
            )
        if result.outcome != "success":
            self._raise_failure(result)
        data = dict(result.data)
        data["extension"] = result.data
        data["plan_hash"] = core_hash
        return ExtensionResult(
            outcome="success",
            data=data,
            artifacts=result.artifacts,
            limitations=result.limitations,
            next_actions=result.next_actions,
            error=None,
        )

    def apply(
        self,
        *,
        reviewed_plan_hash: str,
        configuration: Mapping[str, Any] | None = None,
        explicit_env_names: tuple[str, ...] = (),
    ) -> ExtensionResult:
        return self._dispatch(
            "apply",
            reviewed_plan_hash=reviewed_plan_hash,
            configuration=configuration,
            explicit_env_names=explicit_env_names,
        )

    def test(
        self,
        *,
        configuration: Mapping[str, Any] | None = None,
        explicit_env_names: tuple[str, ...] = (),
    ) -> ExtensionResult:
        return self._dispatch(
            "test",
            configuration=configuration,
            explicit_env_names=explicit_env_names,
        )

    def verify(
        self,
        *,
        configuration: Mapping[str, Any] | None = None,
        explicit_env_names: tuple[str, ...] = (),
    ) -> ExtensionResult:
        return self._dispatch(
            "verify",
            configuration=configuration,
            explicit_env_names=explicit_env_names,
        )


__all__ = ["ApplicationLifecycle"]
