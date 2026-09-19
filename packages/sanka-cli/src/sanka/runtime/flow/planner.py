# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic resource planning using explicit installation ownership."""

from __future__ import annotations

from typing import Any

from sanka.runtime.flow.merge import merge_configuration
from sanka.runtime.flow.model import (
    BlueprintInput,
    Document,
    FlowError,
    FlowPlan,
    Installation,
    TargetSnapshot,
)
from sanka.runtime.flow.verification_plan import validate_effective_blueprint
from sanka.runtime.hashing import content_hash


def _order(resources: dict[str, dict[str, Any]]) -> list[str]:
    result: list[str] = []
    pending = set(resources)
    while pending:
        ready = sorted(key for key in pending if not (set(resources[key]["depends_on"]) & pending))
        if not ready:
            raise FlowError("FLOW_DEPENDENCY_CYCLE", "Resource dependencies contain a cycle")
        result.extend(ready)
        pending.difference_update(ready)
    return result


def plan_reconstruction(
    *,
    blueprint: BlueprintInput,
    installation: Installation,
    observed: TargetSnapshot,
    remove: tuple[str, ...] = (),
    verification_blueprint: BlueprintInput | None = None,
) -> FlowPlan:
    """Plan a validated SDK Blueprint against one host-scoped observation.

    The host must normalize/validate the SDK Blueprint and populate observation
    blockers for unsupported workflow semantics, unresolved references, occupied
    names and permissions before calling this planner. The runtime additionally
    enforces resource ownership, field conflicts and safe lifecycle transitions.
    Omission never authorizes resource deletion; ``remove`` is explicit.
    """
    if not installation.id or installation.target != observed.target:
        raise FlowError("FLOW_TARGET_MISMATCH", "Installation and observation targets differ")
    document = Document(blueprint.to_dict())
    desired: dict[str, dict[str, Any]] = {
        r.id: {"id": r.id, "kind": r.kind, "spec": r.spec, "depends_on": list(r.depends_on)}
        for r in blueprint.resources
    }
    if len(desired) != len(blueprint.resources):
        raise FlowError("FLOW_DUPLICATE_ID", "Blueprint resource IDs must be unique")
    for key, resource in desired.items():
        if not key or not resource["kind"] or not isinstance(resource["spec"], dict):
            raise FlowError("FLOW_INVALID_RESOURCE", "Resource ID, kind and spec are required")
        if set(resource["depends_on"]) - desired.keys():
            raise FlowError("FLOW_DEPENDENCY_MISSING", f"Unresolved dependencies for {key}")
    # Ensure the reviewed serialized artifact is exactly the resource set used
    # to calculate operations, even when a caller implements the structural port.
    serialized = document.to_dict()
    expected_resources = {r["id"]: r for r in serialized.get("resources", [])}
    if len(expected_resources) != len(serialized.get("resources", [])):
        raise FlowError("FLOW_DUPLICATE_ID", "Serialized Blueprint resource IDs must be unique")
    if content_hash(expected_resources) != content_hash(desired):
        raise FlowError("FLOW_BLUEPRINT_MISMATCH", "Serialized Blueprint resources differ")
    owned = {r.logical_id: r for r in installation.resources}
    current = {r.target_id: r for r in observed.resources}
    if len(owned) != len(installation.resources) or len(current) != len(observed.resources):
        raise FlowError("FLOW_DUPLICATE_ID", "Observed and owned resource IDs must be unique")
    if len({r.target_id for r in owned.values()}) != len(owned):
        raise FlowError("FLOW_OWNERSHIP_CONFLICT", "One target resource has multiple owners")
    removals = set(remove)
    if removals - owned.keys() or removals & desired.keys():
        raise FlowError(
            "FLOW_REMOVE_SCOPE", "Removal requires an owned resource absent from desired"
        )
    blockers = [b.to_dict() for b in observed.blockers]
    blockers.extend(serialized.get("unsupported", []))
    operations: list[dict[str, Any]] = []
    scope = {
        "blueprint": document.digest,
        "installation": content_hash(installation.to_dict()),
        "observed": content_hash(observed.to_dict()),
    }

    def block(code: str, logical_id: str, **details: Any) -> None:
        blockers.append({"code": code, "logical_id": logical_id, **details})

    for logical_id in _order(desired):
        resource = desired[logical_id]
        owner = owned.get(logical_id)
        target = current.get(owner.target_id) if owner else None
        operation: dict[str, Any] = {
            "logical_id": logical_id,
            "kind": resource["kind"],
            "action": "create" if owner is None else "preserve",
            "target_id": owner.target_id if owner else None,
            "expected": target.to_dict() if target else None,
            "desired": resource["spec"],
            "configuration": resource["spec"],
            "depends_on": resource["depends_on"],
            "preserved_paths": [],
        }
        if resource["kind"] not in observed.supported_kinds:
            block("FLOW_RESOURCE_UNSUPPORTED", logical_id, kind=resource["kind"])
        if owner is not None:
            if target is None:
                block("FLOW_OWNED_RESOURCE_MISSING", logical_id, target_id=owner.target_id)
            elif owner.kind != resource["kind"] or target.kind != owner.kind:
                block("FLOW_RESOURCE_KIND_CHANGED", logical_id)
            else:
                merged = merge_configuration(
                    previous=owner.desired.to_dict(),
                    current=target.configuration.to_dict(),
                    desired=resource["spec"],
                )
                operation["configuration"] = merged.configuration
                operation["preserved_paths"] = [list(path) for path in merged.preserved_paths]
                for conflict in merged.conflicts:
                    block("FLOW_FIELD_CONFLICT", logical_id, **conflict.to_dict())
                if content_hash(merged.configuration) != target.configuration.digest:
                    operation["action"] = "update"
                    if target.active:
                        # Initial target adapter supports fresh/inactive graphs.
                        # Never substitute an in-place write for staged cutover.
                        block("FLOW_ACTIVE_REPLACEMENT_UNSUPPORTED", logical_id)
        operations.append(operation)

    omitted = {
        key: {"depends_on": list(value.depends_on)}
        for key, value in owned.items()
        if key not in desired
    }
    for logical_id in reversed(_order(omitted)):
        owner = owned[logical_id]
        target = current.get(owner.target_id)
        deleting = logical_id in removals
        if deleting:
            dependents = sorted(
                key
                for key in (owned.keys() | desired.keys()) - removals
                if logical_id
                in (desired[key]["depends_on"] if key in desired else owned[key].depends_on)
            )
            if dependents:
                block("FLOW_RESOURCE_IN_USE", logical_id, dependents=dependents)
            if target and target.active:
                block("FLOW_ACTIVE_REMOVAL_UNSUPPORTED", logical_id)
            if target and target.configuration.digest != owner.desired.digest:
                block("FLOW_REMOVE_USER_EDITS", logical_id)
        operations.append(
            {
                "logical_id": logical_id,
                "kind": owner.kind,
                "action": "remove" if deleting else "preserve",
                "target_id": owner.target_id,
                "expected": target.to_dict() if target else None,
                "desired": None if deleting else owner.desired.to_dict(),
                "configuration": target.configuration.to_dict() if target else None,
                "depends_on": list(owner.depends_on),
                "preserved_paths": [],
            }
        )
        if target is None and not deleting:
            block("FLOW_OWNED_RESOURCE_MISSING", logical_id, target_id=owner.target_id)

    effective = Document(verification_blueprint.to_dict()) if verification_blueprint else None
    if effective is not None:
        validate_effective_blueprint(document, effective, operations)
        scope["verification_blueprint"] = effective.digest
    elif serialized.get("schema_version") == "sanka-flow-blueprint/v4":
        try:
            validate_effective_blueprint(document, document, operations)
        except FlowError as error:
            blockers.append({"code": error.code, "detail": str(error)})
    for operation in operations:
        operation["id"] = content_hash({"scope": scope, "operation": operation})
    return FlowPlan(
        Document(
            {
                "schema_version": "sanka-flow-plan/v2" if effective else "sanka-flow-plan/v1",
                **(
                    {
                        "verification_blueprint": effective.to_dict(),
                        "verification_blueprint_digest": effective.digest,
                    }
                    if effective
                    else {}
                ),
                "blueprint": serialized,
                "blueprint_digest": document.digest,
                "installation": installation.to_dict(),
                "observed": observed.to_dict(),
                "operations": operations,
                "blockers": blockers,
                "construction": "inactive",
                "activation": "explicit_verified_revision",
            }
        )
    )
