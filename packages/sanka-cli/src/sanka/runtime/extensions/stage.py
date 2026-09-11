# SPDX-License-Identifier: AGPL-3.0-only
"""Shared code-extension stage validation, independent of execution transport.

The caller verifies the installed artifact or immutable worker image, then supplies
its binding and an execution adapter. This module does not discover extensions,
install dependencies, authorize a run, or provide a sandbox. Adapters must enforce
time and output limits while a process is running, and terminate it on failure.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from sanka.runtime.extensions.model import LIFECYCLE_COMMANDS, ExtensionError

SCHEMA_VERSION = "sanka-extension/v1"
MAX_PROCESS_OUTPUT = 4 * 1024 * 1024


@dataclass(frozen=True)
class ExtensionBinding:
    """Identity/capabilities supplied by a trusted installation or image verifier.

    Constructing this record does not verify artifact bytes or grant permission.
    """

    id: str
    version: str
    manifest_digest: str
    protocol_version: str
    commands: tuple[str, ...]
    enabled: bool


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


class ExtensionStageRunner:
    """Validate and execute one stage through a host-supplied adapter."""

    @staticmethod
    def _validate_request(lock: ExtensionBinding, request: dict[str, Any]) -> None:
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
        _string(payload["request_id"], "request_id")
        command = _string(payload["command"], "command")
        if command not in LIFECYCLE_COMMANDS:
            _error("SANKA_EXTENSION_PROTOCOL", "Unsupported extension command")
        if (
            not isinstance(lock.commands, tuple)
            or not lock.commands
            or tuple(sorted(set(lock.commands))) != lock.commands
            or not set(lock.commands).issubset(LIFECYCLE_COMMANDS)
        ):
            _error("SANKA_EXTENSION_IDENTITY", "Extension lock capabilities are invalid")
        if command not in lock.commands:
            _error(
                "SANKA_EXTENSION_CAPABILITY_UNSUPPORTED",
                "Locked extension does not advertise the requested command",
                command=command,
                commands=list(lock.commands),
            )
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
        lock: ExtensionBinding,
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
                resolved == root or resolved.is_relative_to(root) for root in allowed_roots
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
        binding: ExtensionBinding,
        request: dict[str, Any],
        *,
        allowed_roots: tuple[Path, ...],
        execute: Callable[[bytes], tuple[int, bytes, bytes]],
    ) -> ExtensionResult:
        """Validate one stage before dispatch and its result before returning.

        ``execute`` receives exactly the validated request bytes and returns exit
        code, stdout and stderr. It must apply its host's execution restrictions;
        the completed-output check here cannot replace a streaming process limit.
        Exceptions do not trigger retries or subsequent lifecycle stages.
        """
        if not binding.enabled or binding.protocol_version != SCHEMA_VERSION:
            _error("SANKA_EXTENSION_IDENTITY", "Extension lock is disabled or incompatible")
        try:
            snapshot = _json_value(request, "request")
            self._validate_request(binding, snapshot)
            content = json.dumps(
                snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as error:
            raise ExtensionError(
                "SANKA_EXTENSION_PROTOCOL",
                "Extension request must contain only JSON values",
                details={"reason": str(error)},
            ) from error
        if not allowed_roots:
            _error("SANKA_EXTENSION_PATH", "At least one artifact root is required")
        roots = tuple(root.resolve() for root in allowed_roots)
        try:
            returncode, stdout_bytes, stderr_bytes = execute(content)
        except ExtensionError:
            raise
        except OSError as error:
            raise ExtensionError(
                "SANKA_EXTENSION_EXECUTION_FAILED",
                "Extension process could not be started",
                details={"reason": str(error)},
            ) from error
        if len(stdout_bytes) + len(stderr_bytes) > MAX_PROCESS_OUTPUT:
            _error("SANKA_EXTENSION_PROTOCOL", "Extension process output exceeded the limit")
        try:
            stdout = stdout_bytes.decode("utf-8")
            stderr_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExtensionError(
                "SANKA_EXTENSION_PROTOCOL",
                "Extension process output must be valid UTF-8",
                details={"stream": "stdout" if error.object is stdout_bytes else "stderr"},
            ) from error
        result = self._parse_response(binding, snapshot, stdout, roots)
        if (result.outcome == "success") != (returncode == 0):
            _error(
                "SANKA_EXTENSION_PROTOCOL",
                "Extension exit status does not match its response outcome",
                returncode=returncode,
            )
        return result


__all__ = ["ExtensionBinding", "ExtensionResult", "ExtensionStageRunner"]
