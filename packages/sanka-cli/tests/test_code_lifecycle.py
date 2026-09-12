# SPDX-License-Identifier: AGPL-3.0-only
"""Code sequencing with fake transports; no repository migration is executed."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sanka.runtime.extensions.code_lifecycle import CodeLifecycle, CodePlan, CodeStage
from sanka.runtime.extensions.model import ExtensionError
from sanka.runtime.extensions.stage import ExtensionBinding

COMMANDS = ["scan", "plan", "apply", "test", "verify"]
PLAN_HASH = "sha256:" + "b" * 64


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.input = self.source / "input.txt"
        self.input.write_text("reviewed input")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.binding = ExtensionBinding(
            id="example/converter",
            version="1.0.0",
            manifest_digest="a" * 64,
            protocol_version="sanka-extension/v1",
            commands=tuple(sorted(COMMANDS)),
            enabled=True,
        )
        self.configuration = {"output": str(self.output), "options": {"mode": "reviewed"}}
        self.calls: list[dict[str, Any]] = []
        self.events: list[CodeStage] = []
        self.plans: list[CodePlan] = []
        self.fail_at: str | None = None
        self.plan_hash: Any = PLAN_HASH

    def snapshot(self) -> str:
        return "sha256:" + hashlib.sha256(self.input.read_bytes()).hexdigest()

    def approve(self, plan: CodePlan) -> str:
        self.plans.append(plan)
        return plan.plan_hash

    def execute(self, content: bytes) -> tuple[int, bytes, bytes]:
        request = json.loads(content)
        self.calls.append(request)
        command = request["command"]
        error = command == self.fail_at
        artifact = self.artifacts / f"{command}.json"
        artifact.write_text(json.dumps({"command": command}))
        response = {
            "schema_version": request["schema_version"],
            "request_id": request["request_id"],
            "command": command,
            "extension": {"id": self.binding.id, "version": self.binding.version},
            "outcome": "error" if error else "success",
            "data": {"plan_hash": self.plan_hash} if command == "plan" else {"stage": command},
            "artifacts": [str(artifact)],
            "limitations": ["Fixture transport; no migration executed"],
            "next_actions": [],
        }
        if error:
            response["error"] = {"code": "CONVERSION_FAILED", "message": "failed", "details": {}}
        return int(error), json.dumps(response).encode(), b"fixture log"

    def run(self, **overrides: Any):
        arguments = {
            "binding": self.binding,
            "project_root": self.source,
            "artifact_root": self.artifacts,
            "configuration": self.configuration,
            "allowed_roots": (self.artifacts, self.output),
            "snapshot_inputs": self.snapshot,
            "execute": self.execute,
            "approve_plan": self.approve,
            "on_stage": self.events.append,
            "request_id_prefix": "run-1",
        }
        return CodeLifecycle().run(**(arguments | overrides))


def test_success_requires_five_stages_and_exact_plan_handoff(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    result = fixture.run()

    assert result.outcome == "success"
    assert [item.command for item in result.stages] == COMMANDS
    assert fixture.events == list(result.stages)
    assert [request["command"] for request in fixture.calls] == COMMANDS
    assert len(fixture.plans) == 1
    assert result.plan == fixture.plans[0]
    assert result.plan.extension_plan_hash == PLAN_HASH
    for request in fixture.calls[:2]:
        assert request["reviewed_plan_hash"] is None
        assert "extension_plan_hash" not in request["configuration"]
    for request in fixture.calls[2:]:
        assert request["reviewed_plan_hash"] == result.plan.plan_hash
        assert request["configuration"]["extension_plan_hash"] == PLAN_HASH
        assert request["prior_artifacts"] == [str(fixture.artifacts / "plan.json")]


def test_missing_approval_stops_at_plan(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    result = fixture.run(approve_plan=lambda _: None)
    assert result.outcome == "planned"
    assert [request["command"] for request in fixture.calls] == ["scan", "plan"]


def test_different_reviewed_hash_never_reaches_apply(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    with pytest.raises(ExtensionError, match="reviewed plan"):
        fixture.run(approve_plan=lambda _: "sha256:" + "c" * 64)
    assert [request["command"] for request in fixture.calls] == ["scan", "plan"]


@pytest.mark.parametrize("stage", COMMANDS)
def test_failure_preserves_stage_receipt_and_stops_without_retry(
    tmp_path: Path, stage: str
) -> None:
    fixture = Fixture(tmp_path)
    fixture.fail_at = stage
    result = fixture.run()
    assert result.outcome == "error"
    assert [item.command for item in result.stages] == COMMANDS[: COMMANDS.index(stage) + 1]
    assert result.stages[-1].to_dict()["error"]["code"] == "CONVERSION_FAILED"
    assert len(fixture.calls) == len(result.stages)


@pytest.mark.parametrize("value", [None, "", "sha256:", "sha256:short", "b" * 64, True])
def test_invalid_extension_plan_hash_cannot_be_approved(tmp_path: Path, value: Any) -> None:
    fixture = Fixture(tmp_path)
    fixture.plan_hash = value
    with pytest.raises(ExtensionError, match="plan hash"):
        fixture.run()
    assert not fixture.plans
    assert [request["command"] for request in fixture.calls] == ["scan", "plan"]


@pytest.mark.parametrize("changed", ["source", "plan", "root"])
def test_changes_during_approval_invalidate_execution(tmp_path: Path, changed: str) -> None:
    fixture = Fixture(tmp_path)

    def approve(plan: CodePlan) -> str:
        if changed == "source":
            fixture.input.write_text("unreviewed input")
        elif changed == "plan":
            (fixture.artifacts / "plan.json").write_text("unreviewed plan")
        else:
            moved = fixture.root / "moved"
            fixture.artifacts.rename(moved)
            fixture.artifacts.symlink_to(moved, target_is_directory=True)
        return plan.plan_hash

    with pytest.raises(ExtensionError):
        fixture.run(approve_plan=approve)
    assert [request["command"] for request in fixture.calls] == ["scan", "plan"]


def test_receipt_and_configuration_mutation_cannot_change_the_plan(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)

    def observe(stage: CodeStage) -> None:
        fixture.configuration["options"]["mode"] = "caller changed"
        stage.to_dict()["data"]["plan_hash"] = "unreviewed"

    result = fixture.run(on_stage=observe)
    assert result.outcome == "success"
    assert result.plan.extension_plan_hash == PLAN_HASH
    assert all(
        request["configuration"]["options"]["mode"] == "reviewed" for request in fixture.calls
    )


@pytest.mark.parametrize("after", ["scan", "apply", "verify"])
def test_changed_input_stops_even_after_final_stage(tmp_path: Path, after: str) -> None:
    fixture = Fixture(tmp_path)

    def observe(stage: CodeStage) -> None:
        if stage.command == after:
            fixture.input.write_text("changed by transport")

    with pytest.raises(ExtensionError, match="input"):
        fixture.run(on_stage=observe)
    assert len(fixture.calls) == COMMANDS.index(after) + 1


def test_callback_failure_stops_without_retry(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)

    def observe(stage: CodeStage) -> None:
        if stage.command == "apply":
            raise OSError("receipt store unavailable")

    with pytest.raises(OSError, match="receipt store"):
        fixture.run(on_stage=observe)
    assert [request["command"] for request in fixture.calls] == ["scan", "plan", "apply"]


@pytest.mark.parametrize("invalid", ["capability", "input_digest", "request_id", "configuration"])
def test_invalid_run_cannot_reach_the_transport(tmp_path: Path, invalid: str) -> None:
    fixture = Fixture(tmp_path)
    overrides = {
        "capability": {"binding": replace(fixture.binding, commands=("scan",))},
        "input_digest": {"snapshot_inputs": lambda: "not-a-digest"},
        "request_id": {"request_id_prefix": ""},
        "configuration": {"configuration": {"extension_plan_hash": PLAN_HASH}},
    }
    with pytest.raises(ExtensionError):
        fixture.run(**overrides[invalid])
    assert not fixture.calls


def test_protocol_identity_failure_stops_before_following_stage(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)

    def execute(content: bytes) -> tuple[int, bytes, bytes]:
        exit_code, stdout, stderr = fixture.execute(content)
        response = json.loads(stdout)
        response["request_id"] = "another-run"
        return exit_code, json.dumps(response).encode(), stderr

    with pytest.raises(ExtensionError, match="identity"):
        fixture.run(execute=execute)
    assert len(fixture.calls) == 1
    assert not fixture.events
