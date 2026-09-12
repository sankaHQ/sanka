# SPDX-License-Identifier: AGPL-3.0-only
"""Code migration sequencing shared by hosts with different execution transports.

Hosts verify extension installations/images, fingerprint the execution inputs,
authorize the resulting plan, and supply bounded execution and receipt storage.
The runtime owns stage order, immutable plan handoff and fail-closed progression.
There is no implicit approval, retry, sandbox or certificate issuance here.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

from sanka.runtime.extensions.artifacts import artifact_digest
from sanka.runtime.extensions.model import ExtensionError
from sanka.runtime.extensions.stage import (
    SCHEMA_VERSION,
    ExtensionBinding,
    ExtensionResult,
    ExtensionStageRunner,
    _json_value,
)
from sanka.runtime.hashing import content_hash

CODE_STAGES = ("scan", "plan", "apply", "test", "verify")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
type Execute = Callable[[bytes], tuple[int, bytes, bytes]]


def _error(code: str, message: str, **details: Any) -> NoReturn:
    raise ExtensionError(code, message, details=details)


def _encode(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        _error("SANKA_EXTENSION_PROTOCOL", f"{name} must be a SHA-256 digest")
    return value


@dataclass(frozen=True)
class CodeStage:
    """Immutable validated response; decoding gives observers their own copy."""

    command: str
    response: bytes

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.response))


@dataclass(frozen=True)
class CodePlan:
    plan_hash: str
    extension_plan_hash: str
    document: bytes

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.document))


@dataclass(frozen=True)
class CodeRun:
    outcome: Literal["success", "planned", "error"]
    stages: tuple[CodeStage, ...]
    plan: CodePlan | None


def _receipt(request: dict[str, Any], result: ExtensionResult) -> CodeStage:
    document = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request["request_id"],
        "command": request["command"],
        "extension": {key: request["extension"][key] for key in ("id", "version")},
        "outcome": result.outcome,
        "data": result.data,
        "artifacts": list(result.artifacts),
        "limitations": list(result.limitations),
        "next_actions": list(result.next_actions),
    }
    if result.error is not None:
        document["error"] = result.error
    return CodeStage(request["command"], _encode(document))


class CodeLifecycle:
    def run(
        self,
        *,
        binding: ExtensionBinding,
        project_root: Path,
        artifact_root: Path,
        configuration: Mapping[str, Any],
        allowed_roots: tuple[Path, ...],
        snapshot_inputs: Callable[[], str],
        execute: Execute,
        approve_plan: Callable[[CodePlan], str | None],
        on_stage: Callable[[CodeStage], None] | None = None,
        on_stage_start: Callable[[str], None] | None = None,
        request_id_prefix: str | None = None,
    ) -> CodeRun:
        """Run one fixed lifecycle; hosts retain transport limits and authorization.

        ``snapshot_inputs`` hashes every source/configuration input that execution
        may consume. It must read current state, not return an unchanged claim ID.
        Observers may persist receipts; observer failures stop execution. Returning
        None from ``approve_plan`` ends at planning. Continuing later requires a
        fresh lifecycle and approval; this method does not restore a saved run.
        """
        prefix = uuid.uuid4().hex if request_id_prefix is None else request_id_prefix
        if not isinstance(prefix, str) or not prefix.strip():
            _error("SANKA_EXTENSION_PROTOCOL", "A request identity is required")
        if not set(CODE_STAGES).issubset(binding.commands):
            _error("SANKA_EXTENSION_CAPABILITY_UNSUPPORTED", "All five Code stages are required")
        try:
            normalized = _json_value(dict(configuration), "configuration")
        except (TypeError, ValueError, RecursionError) as error:
            raise ExtensionError("SANKA_EXTENSION_PROTOCOL", "Invalid configuration") from error
        if "extension_plan_hash" in normalized:
            _error("SANKA_EXTENSION_PROTOCOL", "The runtime owns the extension plan hash")
        source = project_root.resolve()
        artifacts = artifact_root.resolve()
        roots = tuple(root.resolve() for root in allowed_roots)
        initial_input = _digest(snapshot_inputs(), "input snapshot")
        plan: CodePlan | None = None
        reviewed_artifacts: tuple[tuple[str, str], ...] = ()
        receipts: list[CodeStage] = []

        def unchanged() -> None:
            if (
                project_root.resolve() != source
                or artifact_root.resolve() != artifacts
                or tuple(root.resolve() for root in allowed_roots) != roots
            ):
                _error("SANKA_EXTENSION_PATH", "Execution roots changed during the lifecycle")
            if _digest(snapshot_inputs(), "input snapshot") != initial_input:
                _error("SANKA_FINGERPRINT_STALE", "Execution inputs changed during the lifecycle")
            for path, expected in reviewed_artifacts:
                if artifact_digest(Path(path)) != expected:
                    _error("SANKA_EXTENSION_IDENTITY", "Reviewed plan artifact changed", path=path)

        for command in CODE_STAGES:
            if on_stage_start is not None:
                on_stage_start(command)
            unchanged()
            request = {
                "schema_version": SCHEMA_VERSION,
                "request_id": f"{prefix}-{command}",
                "command": command,
                "project_root": str(source),
                "artifact_root": str(artifacts),
                "extension": {
                    "id": binding.id,
                    "version": binding.version,
                    "manifest_digest": binding.manifest_digest.removeprefix("sha256:"),
                },
                "fingerprint": {"hash": initial_input},
                "configuration": {
                    **normalized,
                    **({"extension_plan_hash": plan.extension_plan_hash} if plan else {}),
                },
                "prior_artifacts": [path for path, _ in reviewed_artifacts],
                "reviewed_plan_hash": plan.plan_hash if plan else None,
            }
            result = ExtensionStageRunner().run(
                binding, request, allowed_roots=roots, execute=execute
            )
            receipt = _receipt(request, result)
            receipts.append(receipt)
            if command == "plan" and result.outcome == "success":
                extension_hash = _digest(result.data.get("plan_hash"), "extension plan hash")
                reviewed_artifacts = tuple(
                    (path, artifact_digest(Path(path))) for path in result.artifacts
                )
                document = {
                    "schema_version": "sanka-code-plan/v1",
                    "extension": request["extension"],
                    "protocol_version": binding.protocol_version,
                    "commands": list(binding.commands),
                    "project_root": str(source),
                    "artifact_root": str(artifacts),
                    "allowed_roots": [str(root) for root in roots],
                    "input_hash": initial_input,
                    "configuration": normalized,
                    "extension_plan": result.data,
                    "artifact_digests": dict(reviewed_artifacts),
                }
                plan = CodePlan(content_hash(document), extension_hash, _encode(document))
            if on_stage is not None:
                on_stage(receipt)
            if result.outcome != "success":
                return CodeRun("error", tuple(receipts), plan)
            unchanged()
            if command == "plan":
                assert plan is not None
                approved = approve_plan(plan)
                if approved is None:
                    return CodeRun("planned", tuple(receipts), plan)
                if approved != plan.plan_hash:
                    _error("SANKA_EXTENSION_PLAN_HASH_MISMATCH", "The reviewed plan does not match")
                unchanged()
        return CodeRun("success", tuple(receipts), plan)


__all__ = ["CodeLifecycle", "CodePlan", "CodeRun", "CodeStage"]
