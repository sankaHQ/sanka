# SPDX-License-Identifier: AGPL-3.0-only
"""The shared stage boundary must hold for any supplied execution adapter."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sanka.runtime.extensions.model import ExtensionError
from sanka.runtime.extensions.stage import (
    ExtensionBinding,
    ExtensionResult,
    ExtensionStageRunner,
)


def binding() -> ExtensionBinding:
    return ExtensionBinding(
        id="example/converter",
        version="1.0.0",
        manifest_digest="sha256:" + "a" * 64,
        protocol_version="sanka-extension/v1",
        commands=("apply", "plan", "scan", "test", "verify"),
        enabled=True,
    )


def request(root: Path) -> dict[str, Any]:
    return {
        "schema_version": "sanka-extension/v1",
        "request_id": "run-1-plan",
        "command": "plan",
        "project_root": str(root),
        "artifact_root": str(root),
        "extension": {
            "id": "example/converter",
            "version": "1.0.0",
            "manifest_digest": "a" * 64,
        },
        "fingerprint": {},
        "configuration": {},
        "prior_artifacts": [],
        "reviewed_plan_hash": None,
    }


def response(content: bytes) -> dict[str, Any]:
    sent = json.loads(content)
    return {
        "schema_version": sent["schema_version"],
        "request_id": sent["request_id"],
        "command": sent["command"],
        "extension": {"id": "example/converter", "version": "1.0.0"},
        "outcome": "success",
        "data": {"plan_hash": "sha256:" + "b" * 64},
        "artifacts": [],
        "limitations": ["Static verification only"],
        "next_actions": ["Review plan"],
    }


def test_stage_runs_without_a_local_extension_store(tmp_path: Path) -> None:
    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        return 0, json.dumps(response(content)).encode(), b"adapter log"

    result = ExtensionStageRunner().run(
        binding(), request(tmp_path), allowed_roots=(tmp_path,), execute=execute
    )
    assert result.outcome == "success"
    assert result.data == {"plan_hash": "sha256:" + "b" * 64}
    assert result.limitations == ("Static verification only",)
    assert result.next_actions == ("Review plan",)


@pytest.mark.parametrize("invalid", ["identity", "disabled", "schema", "command", "roots"])
def test_invalid_binding_or_request_never_reaches_adapter(tmp_path: Path, invalid: str) -> None:
    selected = binding()
    payload = request(tmp_path)
    roots = (tmp_path,)
    if invalid == "identity":
        payload["extension"]["version"] = "2.0.0"
    elif invalid == "disabled":
        selected = replace(selected, enabled=False)
    elif invalid == "schema":
        selected = replace(selected, protocol_version="unrecognized/v2")
    elif invalid == "command":
        selected = replace(selected, commands=("scan",))
    else:
        roots = ()

    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        pytest.fail("Invalid execution reached adapter")

    with pytest.raises(ExtensionError):
        ExtensionStageRunner().run(selected, payload, allowed_roots=roots, execute=execute)


@pytest.mark.parametrize("invalid", ["request", "extension", "command", "exit", "json", "utf8"])
def test_invalid_adapter_response_is_not_success(tmp_path: Path, invalid: str) -> None:
    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        value = response(content)
        if invalid == "request":
            value["request_id"] = "another-run"
        elif invalid == "extension":
            value["extension"]["version"] = "2.0.0"
        elif invalid == "command":
            value["command"] = "apply"
        if invalid == "json":
            return 0, b"diagnostic\n{}", b""
        return (
            1 if invalid == "exit" else 0,
            json.dumps(value).encode(),
            b"\xff" if invalid == "utf8" else b"",
        )

    with pytest.raises(ExtensionError):
        ExtensionStageRunner().run(
            binding(), request(tmp_path), allowed_roots=(tmp_path,), execute=execute
        )


def test_stage_rejects_adapter_output_over_limit(tmp_path: Path) -> None:
    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        return 0, json.dumps(response(content)).encode(), b"x" * (4 * 1024 * 1024)

    with pytest.raises(ExtensionError, match="output exceeded"):
        ExtensionStageRunner().run(
            binding(), request(tmp_path), allowed_roots=(tmp_path,), execute=execute
        )


def test_stage_keeps_validated_request_when_caller_mutates_it(tmp_path: Path) -> None:
    payload = request(tmp_path)

    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        payload["request_id"] = "changed-after-validation"
        return 0, json.dumps(response(content)).encode(), b""

    assert (
        ExtensionStageRunner()
        .run(binding(), payload, allowed_roots=(tmp_path,), execute=execute)
        .outcome
        == "success"
    )


@pytest.mark.parametrize("invalid", ["non-string-key", "non-finite", "cycle", "request-id"])
def test_invalid_json_request_never_reaches_adapter(tmp_path: Path, invalid: str) -> None:
    payload = request(tmp_path)
    if invalid == "non-string-key":
        payload["configuration"] = {1: "must not silently become a string key"}
    elif invalid == "non-finite":
        payload["configuration"] = {"value": float("nan")}
    elif invalid == "cycle":
        payload["configuration"]["self"] = payload
    else:
        payload["request_id"] = ""

    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        pytest.fail("Malformed request reached adapter")

    with pytest.raises(ExtensionError):
        ExtensionStageRunner().run(binding(), payload, allowed_roots=(tmp_path,), execute=execute)


@pytest.mark.parametrize("failure", [TimeoutError("deadline"), OSError("unavailable")])
def test_adapter_failure_propagates_without_retry(tmp_path: Path, failure: OSError) -> None:
    calls = 0

    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        nonlocal calls
        calls += 1
        raise failure

    with pytest.raises(ExtensionError, match="could not be started"):
        ExtensionStageRunner().run(
            binding(), request(tmp_path), allowed_roots=(tmp_path,), execute=execute
        )
    assert calls == 1


def test_stage_rejects_artifacts_outside_declared_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "artifacts"
    allowed.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")

    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        value = response(content)
        value["artifacts"] = [str(outside)]
        return 0, json.dumps(value).encode(), b""

    with pytest.raises(ExtensionError, match="outside the declared roots"):
        ExtensionStageRunner().run(
            binding(), request(tmp_path), allowed_roots=(allowed,), execute=execute
        )


def test_adapter_cannot_replace_the_declared_artifact_root(tmp_path: Path) -> None:
    allowed = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "result.json").write_text("{}")

    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        allowed.rmdir()
        allowed.symlink_to(outside, target_is_directory=True)
        value = response(content)
        value["artifacts"] = [str(allowed / "result.json")]
        return 0, json.dumps(value).encode(), b""

    with pytest.raises(ExtensionError, match="outside the declared roots"):
        ExtensionStageRunner().run(
            binding(), request(tmp_path), allowed_roots=(allowed,), execute=execute
        )


def test_extension_failure_remains_structured(tmp_path: Path) -> None:
    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        value = response(content)
        value["outcome"] = "error"
        value["error"] = {"code": "INPUT_REQUIRED", "message": "Missing ORM", "details": {}}
        return 1, json.dumps(value).encode(), b""

    result = ExtensionStageRunner().run(
        binding(), request(tmp_path), allowed_roots=(tmp_path,), execute=execute
    )
    assert result.outcome == "error"
    assert result.error == {"code": "INPUT_REQUIRED", "message": "Missing ORM", "details": {}}


def test_legacy_result_import_preserves_identity() -> None:
    from sanka.runtime.extensions.runner import ExtensionResult as PriorResult

    assert PriorResult is ExtensionResult
