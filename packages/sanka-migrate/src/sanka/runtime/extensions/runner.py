# SPDX-License-Identifier: AGPL-3.0-only
"""Direct-argv execution for the versioned extension subprocess protocol."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from sanka.runtime.extensions.model import ExtensionError
from sanka.runtime.extensions.store import DEFAULT_EXTENSION_ID, LockEntry, user_extension_root

SCHEMA_VERSION = "sanka-extension/v1"
COMMANDS = frozenset({"apply", "plan", "scan", "test", "verify"})
SAFE_ENV = ("LANG", "LC_ALL", "PATH", "PYTHONUTF8", "TMPDIR", "VIRTUAL_ENV")
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAX_PROCESS_OUTPUT = 4 * 1024 * 1024


@dataclass(frozen=True)
class ExtensionResult:
    outcome: str
    data: dict[str, Any]
    artifacts: tuple[str, ...]
    limitations: tuple[str, ...]
    next_actions: tuple[str, ...]
    error: dict[str, Any] | None


def _error(code: str, message: str, **details: Any) -> NoReturn:
    raise ExtensionError(code, message, details=details)


def _json_value(value: object, field: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        _error("SANKA_EXTENSION_PROTOCOL", f"{field} contains a non-finite number")
    if isinstance(value, list):
        return [_json_value(item, f"{field}[]") for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item, f"{field}.{key}") for key, item in value.items()}
    _error("SANKA_EXTENSION_PROTOCOL", f"{field} must contain only JSON values")


def _object(value: object, field: str, keys: set[str]) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or not all(isinstance(key, str) for key in value)
    ):
        _error("SANKA_EXTENSION_PROTOCOL", f"{field} has unexpected or missing fields")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        _error("SANKA_EXTENSION_PROTOCOL", f"{field} must be a non-empty string")
    return value


def _string_array(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _error("SANKA_EXTENSION_PROTOCOL", f"{field} must be an array")
    return tuple(_string(item, f"{field}[]") for item in value)


class ExtensionRunner:
    """Run one verified lock through exactly one JSON request and response."""

    def __init__(
        self,
        *,
        user_root: Path | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.user_root = (user_root or user_extension_root()).expanduser().resolve()
        self.timeout_seconds = timeout_seconds

    def _executable(self, lock: LockEntry) -> Path:
        configured = Path(lock.executable)
        if configured.is_absolute():
            executable = configured
        elif lock.id == DEFAULT_EXTENSION_ID:
            executable = Path(sys.executable).resolve().parent / lock.executable
        else:
            executable = (
                self.user_root
                / "environments"
                / lock.artifact_digest
                / ("Scripts" if os.name == "nt" else "bin")
                / lock.executable
            )
        try:
            status = executable.lstat()
        except OSError as error:
            raise ExtensionError(
                "SANKA_EXTENSION_NOT_CACHED",
                "Exact locked extension executable is unavailable",
                details={"path": str(executable), "reason": str(error)},
            ) from error
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            _error(
                "SANKA_EXTENSION_PATH",
                "Locked extension executable must be a regular file",
                path=str(executable),
            )
        return executable.resolve()

    @staticmethod
    def _validate_request(lock: LockEntry, request: dict[str, Any]) -> None:
        expected = {
            "artifact_root",
            "command",
            "configuration",
            "extension",
            "fingerprint",
            "prior_artifacts",
            "project_root",
            "request_id",
            "reviewed_plan_hash",
            "schema_version",
        }
        payload = _object(request, "request", expected)
        if payload["schema_version"] != SCHEMA_VERSION:
            _error("SANKA_EXTENSION_PROTOCOL", "Unsupported extension request schema")
        command = _string(payload["command"], "command")
        if command not in COMMANDS:
            _error("SANKA_EXTENSION_PROTOCOL", "Unsupported extension command")
        extension = _object(payload["extension"], "extension", {"id", "version", "manifest_digest"})
        identity = (
            extension["id"],
            extension["version"],
            extension["manifest_digest"],
        )
        expected_identity = (
            lock.id,
            lock.version,
            lock.manifest_digest.removeprefix("sha256:"),
        )
        if identity != expected_identity:
            _error("SANKA_EXTENSION_IDENTITY", "Extension request does not match its lock")
        for field in ("project_root", "artifact_root"):
            path = Path(_string(payload[field], field))
            if not path.is_absolute():
                _error("SANKA_EXTENSION_PATH", f"{field} must be absolute")
        _json_value(payload["fingerprint"], "fingerprint")
        _json_value(payload["configuration"], "configuration")
        _string_array(payload["prior_artifacts"], "prior_artifacts")
        reviewed = payload["reviewed_plan_hash"]
        if reviewed is not None:
            _string(reviewed, "reviewed_plan_hash")

    @staticmethod
    def _parse_response(
        lock: LockEntry,
        request: dict[str, Any],
        stdout: str,
        allowed_roots: tuple[Path, ...],
    ) -> ExtensionResult:
        try:
            raw = json.loads(
                stdout,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ExtensionError(
                "SANKA_EXTENSION_PROTOCOL",
                "Extension stdout must contain exactly one JSON document",
                details={"reason": str(error)},
            ) from error
        if not isinstance(raw, dict):
            _error("SANKA_EXTENSION_PROTOCOL", "Extension response must be an object")
        outcome = raw.get("outcome")
        expected = {
            "artifacts",
            "command",
            "data",
            "extension",
            "limitations",
            "next_actions",
            "outcome",
            "request_id",
            "schema_version",
        }
        if outcome == "error":
            expected.add("error")
        payload = _object(raw, "response", expected)
        if payload["schema_version"] != SCHEMA_VERSION or outcome not in {"success", "error"}:
            _error("SANKA_EXTENSION_PROTOCOL", "Extension response schema or outcome is invalid")
        extension = _object(payload["extension"], "extension", {"id", "version"})
        if (
            payload["request_id"] != request["request_id"]
            or payload["command"] != request["command"]
            or extension.get("id") != lock.id
            or extension.get("version") != lock.version
        ):
            _error("SANKA_EXTENSION_IDENTITY", "Extension response identity does not match request")
        data = _json_value(payload["data"], "data")
        if not isinstance(data, dict):
            _error("SANKA_EXTENSION_PROTOCOL", "response.data must be an object")
        roots = tuple(root.resolve() for root in allowed_roots)
        artifacts: list[str] = []
        for raw_path in _string_array(payload["artifacts"], "artifacts"):
            path = Path(raw_path)
            if not path.is_absolute():
                _error(
                    "SANKA_EXTENSION_PATH",
                    "Extension artifacts must be absolute",
                    path=raw_path,
                )
            try:
                resolved = path.resolve(strict=True)
                linked = path.is_symlink()
            except (OSError, RuntimeError) as error:
                raise ExtensionError(
                    "SANKA_EXTENSION_PATH",
                    "Extension artifact cannot be resolved",
                    details={"path": raw_path, "reason": str(error)},
                ) from error
            if linked or not any(
                resolved == root or resolved.is_relative_to(root) for root in roots
            ):
                _error(
                    "SANKA_EXTENSION_PATH",
                    "Extension artifact is outside the declared roots",
                    path=raw_path,
                )
            artifacts.append(str(resolved))
        if len(artifacts) != len(set(artifacts)):
            _error("SANKA_EXTENSION_PROTOCOL", "Extension artifacts must be unique")
        error_payload: dict[str, Any] | None = None
        if outcome == "error":
            structured = _object(payload["error"], "error", {"code", "message", "details"})
            details = _json_value(structured["details"], "error.details")
            if not isinstance(details, dict):
                _error("SANKA_EXTENSION_PROTOCOL", "error.details must be an object")
            error_payload = {
                "code": _string(structured["code"], "error.code"),
                "message": _string(structured["message"], "error.message"),
                "details": details,
            }
        return ExtensionResult(
            outcome=outcome,
            data=data,
            artifacts=tuple(artifacts),
            limitations=_string_array(payload["limitations"], "limitations"),
            next_actions=_string_array(payload["next_actions"], "next_actions"),
            error=error_payload,
        )

    def run(
        self,
        lock: LockEntry,
        request: dict[str, Any],
        *,
        allowed_roots: tuple[Path, ...],
        explicit_env_names: tuple[str, ...] = (),
    ) -> ExtensionResult:
        if not lock.enabled or lock.protocol_version != SCHEMA_VERSION:
            _error("SANKA_EXTENSION_IDENTITY", "Extension lock is disabled or incompatible")
        self._validate_request(lock, request)
        if not allowed_roots:
            _error("SANKA_EXTENSION_PATH", "At least one artifact root is required")
        if len(explicit_env_names) != len(set(explicit_env_names)) or any(
            ENVIRONMENT_NAME.fullmatch(name) is None for name in explicit_env_names
        ):
            _error("SANKA_EXTENSION_ENVIRONMENT", "Explicit environment names are invalid")
        environment = {name: os.environ[name] for name in SAFE_ENV if name in os.environ}
        environment.update(
            {name: os.environ[name] for name in explicit_env_names if name in os.environ}
        )
        try:
            completed = subprocess.run(
                [str(self._executable(lock))],
                input=json.dumps(
                    request,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                ),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                cwd=request["project_root"],
                env=environment,
            )
        except subprocess.TimeoutExpired as error:
            raise ExtensionError(
                "SANKA_EXTENSION_TIMEOUT",
                "Extension execution timed out",
                details={"timeout_seconds": self.timeout_seconds},
            ) from error
        except OSError as error:
            raise ExtensionError(
                "SANKA_EXTENSION_EXECUTION_FAILED",
                "Extension process could not be started",
                details={"reason": str(error)},
            ) from error
        if (
            len(completed.stdout.encode()) > MAX_PROCESS_OUTPUT
            or len(completed.stderr.encode()) > MAX_PROCESS_OUTPUT
        ):
            _error("SANKA_EXTENSION_PROTOCOL", "Extension process output exceeded the limit")
        result = self._parse_response(lock, request, completed.stdout, allowed_roots)
        if (result.outcome == "success") != (completed.returncode == 0):
            _error(
                "SANKA_EXTENSION_PROTOCOL",
                "Extension exit status does not match its response outcome",
                returncode=completed.returncode,
            )
        return result


__all__ = ["ExtensionResult", "ExtensionRunner"]
