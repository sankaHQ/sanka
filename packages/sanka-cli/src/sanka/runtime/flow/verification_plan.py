# SPDX-License-Identifier: AGPL-3.0-only
"""Bind native verification to effective settings without changing ownership baselines."""

from __future__ import annotations

from typing import Any

from sanka.runtime.flow.model import Document, FlowError, FlowPlan


def validate_effective_blueprint(
    desired: Document, effective: Document, operations: list[dict[str, Any]]
) -> None:
    from sanka_extensions.flow import Blueprint, NativeBillingScenario, NativeOrderBillingWorkflow

    left, right = desired.to_dict(), effective.to_dict()
    if any(p.get("schema_version") != "sanka-flow-blueprint/v4" for p in (left, right)):
        raise FlowError(
            "FLOW_VERIFICATION_PROFILE_UNSUPPORTED", "Separate native verification requires v4"
        )
    try:
        for payload in (left, right):
            candidate = Blueprint.from_dict(payload)
            profile = NativeOrderBillingWorkflow.from_dict(candidate.resources[0].spec)
            parameters = payload["parameters"]
            if "definition_parameters" in parameters:
                selected = parameters["definition_parameters"]
                if (
                    type(selected) is not dict
                    or set(selected) != {"native_configuration", "native_verification"}
                    or Document(selected["native_configuration"]) != Document(profile.configuration)
                    or type(selected["native_verification"]) is not list
                ):
                    raise ValueError("Native request metadata differs from the generated artifact")
                requested = sorted(
                    (NativeBillingScenario.from_dict(s) for s in selected["native_verification"]),
                    key=lambda scenario: scenario.id,
                )
                if Document({"scenarios": [s.to_dict() for s in requested]}) != Document(
                    {"scenarios": [s.to_dict() for s in candidate.scenarios]}
                ):
                    raise ValueError("Native request scenarios differ from the generated artifact")
                # Only these two correlated request values can change. Target,
                # extension/template provenance, and other parameters stay pinned.
                parameters["definition_parameters"] = {}
    except (ValueError, KeyError, TypeError) as error:
        raise FlowError(
            "FLOW_VERIFICATION_CONFIGURATION_MISMATCH", "Invalid effective native artifact"
        ) from error
    desired_resources = left.pop("resources")
    actual_resources = right.pop("resources")
    left.pop("scenarios")
    right.pop("scenarios")
    if Document(left) != Document(right):
        raise FlowError(
            "FLOW_VERIFICATION_CONFIGURATION_MISMATCH", "Verification changed artifact provenance"
        )
    by_id = {operation["logical_id"]: operation for operation in operations}
    expected = [
        {**resource, "spec": by_id[resource["id"]]["configuration"]}
        for resource in desired_resources
    ]
    if Document({"resources": expected}) != Document({"resources": actual_resources}):
        raise FlowError(
            "FLOW_VERIFICATION_CONFIGURATION_MISMATCH",
            "Verification must describe the final merged workflow configuration",
        )


def verification_blueprint(plan: FlowPlan) -> dict[str, Any]:
    payload = plan.to_dict()
    if (
        payload.get("schema_version") == "sanka-flow-plan/v2"
        and not {"verification_blueprint", "verification_blueprint_digest"} <= payload.keys()
    ):
        raise FlowError("FLOW_PLAN_INVALID", "Plan v2 requires its effective verification artifact")
    desired = Document(payload["blueprint"])
    effective = Document(payload.get("verification_blueprint", payload["blueprint"]))
    if "verification_blueprint" in payload:
        if (
            payload.get("schema_version") != "sanka-flow-plan/v2"
            or payload.get("verification_blueprint_digest") != effective.digest
        ):
            raise FlowError("FLOW_PLAN_INVALID", "Effective verification artifact is not pinned")
        validate_effective_blueprint(desired, effective, payload["operations"])
    elif desired.to_dict().get("schema_version") == "sanka-flow-blueprint/v4":
        validate_effective_blueprint(desired, effective, payload["operations"])
    return effective.to_dict()
