# SPDX-License-Identifier: Apache-2.0
"""The strict JSON contract shared by Sanka extension subprocesses."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = "sanka-extension/v1"
COMMANDS = frozenset({"scan", "plan", "apply", "test", "verify"})

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


@dataclass(frozen=True)
class ExtensionFailure:
    code: str
    message: str
    details: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtensionRequest:
    request_id: str
    command: str
    project_root: str
    artifact_root: str
    extension_id: str
    extension_version: str
    manifest_digest: str
    fingerprint: dict[str, JsonValue]
    configuration: dict[str, JsonValue]
    prior_artifacts: tuple[str, ...]
    reviewed_plan_hash: str | None


@dataclass(frozen=True)
class ExtensionResponse:
    request_id: str
    command: str
    extension_id: str
    extension_version: str
    outcome: str
    data: dict[str, JsonValue]
    artifacts: tuple[str, ...]
    limitations: tuple[str, ...]
    next_actions: tuple[str, ...]
    error: ExtensionFailure | None = None


def _object(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must be an object")
    return value


def _required_object(value: object, field_name: str, keys: set[str]) -> dict[str, object]:
    result = _object(value, field_name)
    missing = keys - set(result)
    if missing:
        raise ValueError(f"{field_name}.{sorted(missing)[0]} is required")
    if unexpected := set(result) - keys:
        raise ValueError(f"{field_name}.{sorted(unexpected)[0]} is unexpected")
    return result


def _string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _absolute_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f"{field_name} must be an absolute path")
    return str(Path(value).resolve())


def _digest(value: object, field_name: str) -> str:
    digest = _string(value, field_name)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return digest


def _json_value(value: object, field_name: str, ancestors: set[int]) -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError(f"{field_name} must contain only JSON values")
    if isinstance(value, list):
        identity = id(value)
        if identity in ancestors:
            raise ValueError(f"{field_name} must contain only JSON values")
        ancestors.add(identity)
        try:
            return [
                _json_value(item, f"{field_name}[{index}]", ancestors)
                for index, item in enumerate(value)
            ]
        finally:
            ancestors.remove(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in ancestors or not all(isinstance(key, str) for key in value):
            raise ValueError(f"{field_name} must contain only JSON values")
        ancestors.add(identity)
        try:
            return {
                key: _json_value(item, f"{field_name}.{key}", ancestors)
                for key, item in value.items()
            }
        finally:
            ancestors.remove(identity)
    raise ValueError(f"{field_name} must contain only JSON values")


def _json_object(value: object, field_name: str) -> dict[str, JsonValue]:
    parsed = _json_value(value, field_name, set())
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be an object")
    return parsed


def _string_array(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_string(item, f"{field_name}[{index}]"))
    return tuple(result)


def decode_request(value: object) -> ExtensionRequest:
    payload = _required_object(
        value,
        "request",
        {
            "schema_version",
            "request_id",
            "command",
            "project_root",
            "artifact_root",
            "extension",
            "fingerprint",
            "configuration",
            "prior_artifacts",
            "reviewed_plan_hash",
        },
    )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    command = _string(payload["command"], "command")
    if command not in COMMANDS:
        raise ValueError(f"command must be one of {', '.join(sorted(COMMANDS))}")
    extension = _required_object(
        payload["extension"], "extension", {"id", "version", "manifest_digest"}
    )
    reviewed_plan_hash = payload["reviewed_plan_hash"]
    if reviewed_plan_hash is not None:
        reviewed_plan_hash = _string(reviewed_plan_hash, "reviewed_plan_hash")
    return ExtensionRequest(
        request_id=_string(payload["request_id"], "request_id"),
        command=command,
        project_root=_absolute_path(payload["project_root"], "project_root"),
        artifact_root=_absolute_path(payload["artifact_root"], "artifact_root"),
        extension_id=_string(extension["id"], "extension.id"),
        extension_version=_string(extension["version"], "extension.version"),
        manifest_digest=_digest(extension["manifest_digest"], "extension.manifest_digest"),
        fingerprint=_json_object(payload["fingerprint"], "fingerprint"),
        configuration=_json_object(payload["configuration"], "configuration"),
        prior_artifacts=_string_array(payload["prior_artifacts"], "prior_artifacts"),
        reviewed_plan_hash=reviewed_plan_hash,
    )


def _request_payload(request: ExtensionRequest) -> dict[str, JsonValue]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request.request_id,
        "command": request.command,
        "project_root": request.project_root,
        "artifact_root": request.artifact_root,
        "extension": {
            "id": request.extension_id,
            "version": request.extension_version,
            "manifest_digest": request.manifest_digest,
        },
        "fingerprint": request.fingerprint,
        "configuration": request.configuration,
        "prior_artifacts": list(request.prior_artifacts),
        "reviewed_plan_hash": request.reviewed_plan_hash,
    }


def encode_request(request: ExtensionRequest) -> dict[str, JsonValue]:
    if not isinstance(request, ExtensionRequest):
        raise ValueError("request must be an ExtensionRequest")
    return _request_payload(decode_request(_request_payload(request)))


def _response_payload(response: ExtensionResponse) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "schema_version": SCHEMA_VERSION,
        "request_id": response.request_id,
        "command": response.command,
        "extension": {"id": response.extension_id, "version": response.extension_version},
        "outcome": response.outcome,
        "data": response.data,
        "artifacts": list(response.artifacts),
        "limitations": list(response.limitations),
        "next_actions": list(response.next_actions),
    }
    if response.error is not None:
        payload["error"] = {
            "code": response.error.code,
            "message": response.error.message,
            "details": response.error.details,
        }
    return payload


def decode_response(value: object) -> ExtensionResponse:
    payload = _object(value, "response")
    expected_keys = {
        "schema_version",
        "request_id",
        "command",
        "extension",
        "outcome",
        "data",
        "artifacts",
        "limitations",
        "next_actions",
    }
    outcome = payload.get("outcome")
    if outcome == "error" and "error" not in payload:
        raise ValueError("error is required for error outcomes")
    if outcome == "error":
        expected_keys.add("error")
    if set(payload) != expected_keys:
        raise ValueError("response has unexpected or missing fields")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    command = _string(payload["command"], "command")
    if command not in COMMANDS:
        raise ValueError(f"command must be one of {', '.join(sorted(COMMANDS))}")
    extension = _required_object(payload["extension"], "extension", {"id", "version"})
    if outcome not in {"success", "error"}:
        raise ValueError("outcome must be success or error")
    error: ExtensionFailure | None = None
    if outcome == "error":
        error_payload = _required_object(payload["error"], "error", {"code", "message", "details"})
        error = ExtensionFailure(
            code=_string(error_payload["code"], "error.code"),
            message=_string(error_payload["message"], "error.message"),
            details=_json_object(error_payload["details"], "error.details"),
        )
    return ExtensionResponse(
        request_id=_string(payload["request_id"], "request_id"),
        command=command,
        extension_id=_string(extension["id"], "extension.id"),
        extension_version=_string(extension["version"], "extension.version"),
        outcome=outcome,
        data=_json_object(payload["data"], "data"),
        artifacts=_string_array(payload["artifacts"], "artifacts"),
        limitations=_string_array(payload["limitations"], "limitations"),
        next_actions=_string_array(payload["next_actions"], "next_actions"),
        error=error,
    )


def encode_response(response: ExtensionResponse) -> dict[str, JsonValue]:
    if not isinstance(response, ExtensionResponse):
        raise ValueError("response must be an ExtensionResponse")
    return _response_payload(decode_response(_response_payload(response)))


def success_response(
    request: ExtensionRequest,
    *,
    data: dict[str, JsonValue],
    artifacts: Sequence[str] = (),
    limitations: Sequence[str] = (),
    next_actions: Sequence[str] = (),
) -> ExtensionResponse:
    return ExtensionResponse(
        request_id=request.request_id,
        command=request.command,
        extension_id=request.extension_id,
        extension_version=request.extension_version,
        outcome="success",
        data=data,
        artifacts=tuple(artifacts),
        limitations=tuple(limitations),
        next_actions=tuple(next_actions),
    )


def failure_response(
    request: ExtensionRequest,
    *,
    code: str,
    message: str,
    details: dict[str, JsonValue] | None = None,
) -> ExtensionResponse:
    return ExtensionResponse(
        request_id=request.request_id,
        command=request.command,
        extension_id=request.extension_id,
        extension_version=request.extension_version,
        outcome="error",
        data={},
        artifacts=(),
        limitations=(),
        next_actions=(),
        error=ExtensionFailure(
            code=code,
            message=message,
            details={} if details is None else details,
        ),
    )
