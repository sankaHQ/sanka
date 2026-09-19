# SPDX-License-Identifier: AGPL-3.0-only
"""Compare native scenario readback with the reviewed declarative expectations.

This evaluates expected value bindings only. Triggers, conditions and actions
must run in the target's existing executor, behind isolated records/adapters.
"""

from __future__ import annotations

from typing import Any

from sanka.runtime.flow.model import Document, FlowError, Installation


def required_scenarios(blueprint: dict[str, Any]) -> set[str]:
    if blueprint.get("schema_version") == "sanka-flow-blueprint/v4":
        from sanka.runtime.flow.native_verification import required_native_scenarios

        return required_native_scenarios(blueprint)
    scenarios = blueprint.get("scenarios", [])
    workflows = {r["id"] for r in blueprint["resources"] if r["kind"] == "workflow"}
    if len({s["id"] for s in scenarios}) != len(scenarios):
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Scenario identities must be unique")
    for workflow_id in workflows:
        cases = {
            s.get("case")
            for s in scenarios
            if s.get("workflow_id") == workflow_id and s.get("required", True)
        }
        if not {"no_match", "match", "retry"} <= cases:
            raise FlowError(
                "FLOW_VERIFICATION_INCOMPLETE",
                "Workflow verification requires scenarios for no_match, match and retry",
            )
    for scenario in scenarios:
        events = scenario.get("events", [])
        case = scenario.get("case")
        count = scenario.get("expected_created_count")
        if (
            scenario.get("workflow_id") not in workflows
            or case not in {"no_match", "match", "retry"}
            or type(count) is not int
            or count != (0 if case == "no_match" else 1)
            or not events
            or (case != "retry" and len(events) != 1)
            or (
                case == "retry"
                and (len(events) < 2 or any(Document(e) != Document(events[0]) for e in events))
            )
        ):
            raise FlowError("FLOW_SCENARIO_MISMATCH", "Invalid reviewed scenario coverage")
    return {s["id"] for s in scenarios if s.get("required", True)}


def _binding(
    binding: dict[str, Any],
    event: dict[str, Any],
    references: dict[str, dict[str, Any]],
    record_bindings: dict[str, str],
) -> Any:
    kind = binding["kind"]
    if kind == "literal":
        return binding["value"]
    if kind == "event_record":
        return record_bindings[event["record_id"]]
    if kind == "event_field":
        fields = event[binding["phase"]]
        if binding["field_ref"] not in fields:
            raise FlowError("FLOW_SCENARIO_MISMATCH", "Expected event field is absent")
        return fields[binding["field_ref"]]
    if kind == "reference":
        ref = references.get(binding["reference_id"])
        if ref and ref["kind"] == "record" and ref["binding"] == "existing":
            return ref["key"]
    raise FlowError("FLOW_SCENARIO_MISMATCH", "Unsupported expected value binding")


def evaluate_scenario(
    *,
    blueprint: dict[str, Any],
    scenario: Document,
    installation: Installation,
    result: Document,
) -> Document:
    """Retain raw readback and add a runtime-owned verdict and mismatch list.

    ``created_records`` must enumerate every record created by this scenario's
    native action, including all retry deliveries; it is not a selected sample.
    Fields and associations use the Blueprint reference IDs, whose exact native
    bindings are already pinned in the plan's observed context.
    """
    request, payload = scenario.to_dict(), result.to_dict()
    workflow_target = next(
        r.target_id for r in installation.resources if r.logical_id == request["workflow_id"]
    )
    if (
        payload.get("scenario_id") != request["id"]
        or payload.get("scenario_digest") != scenario.digest
        or payload.get("installation_id") != installation.id
        or payload.get("workflow_target_id") != workflow_target
    ):
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Scenario result belongs to different input")
    if payload.get("isolated") is not True:
        raise FlowError(
            "FLOW_VERIFICATION_UNSAFE", "Verification must use isolated records and adapters"
        )
    if payload.get("outcome") not in {"passed", "failed", "skipped"}:
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Scenario result has an invalid outcome")
    mismatches: list[dict[str, Any]] = []
    record_bindings = payload.get("event_record_bindings")
    event_record_ids = {event["record_id"] for event in request["events"]}
    if (
        type(record_bindings) is not dict
        or set(record_bindings) != event_record_ids
        or any(type(value) is not str or not value for value in record_bindings.values())
        or len(set(record_bindings.values())) != len(record_bindings)
    ):
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Isolated event record bindings are invalid")
    expected_deliveries = [Document(e).digest for e in request["events"]]
    if payload.get("completed_event_digests") != expected_deliveries:
        mismatches.append({"code": "FLOW_EVENT_DELIVERY_MISMATCH"})
    records = payload.get("created_records")
    if type(records) is not list or any(type(r) is not dict for r in records):
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Native record readback is missing")
    ids = [r.get("id") for r in records]
    if any(type(record_id) is not str or not record_id for record_id in ids):
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Native record identity is missing")
    if len(set(ids)) != len(ids) or len(records) != request["expected_created_count"]:
        mismatches.append({"code": "FLOW_CREATED_COUNT_MISMATCH"})
    workflow = next(r for r in blueprint["resources"] if r["id"] == request["workflow_id"])
    actions = [n["spec"] for n in workflow["spec"]["nodes"] if n["kind"] == "action"]
    if len(actions) != 1:
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Expected exactly one supported native action")
    references = {ref["id"]: ref for ref in blueprint.get("references", [])}
    event = request["events"][0]
    for record in records:
        if record.get("object_ref") != actions[0]["object_ref"]:
            mismatches.append({"code": "FLOW_CREATED_OBJECT_MISMATCH", "record_id": record["id"]})
        fields, associations = record.get("fields"), record.get("associations")
        if type(fields) is not dict or type(associations) is not dict:
            raise FlowError("FLOW_SCENARIO_MISMATCH", "Native fields or associations are missing")
        for field in request["expected_fields"]:
            ref = field["field_ref"]
            expected = _binding(field["value"], event, references, record_bindings)
            if ref not in fields or Document({"v": fields[ref]}) != Document({"v": expected}):
                mismatches.append({"code": "FLOW_FIELD_MISMATCH", "field_ref": ref})
        for association in request["expected_associations"]:
            ref = association["relationship_ref"]
            expected = _binding(association["record"], event, references, record_bindings)
            # Exact relationship readback also catches linking a second/wrong
            # Deal. Optional associations may be absent, but may not be wrong.
            if (ref not in associations and association.get("required", True)) or (
                ref in associations
                and Document({"v": associations[ref]}) != Document({"v": [expected]})
            ):
                mismatches.append({"code": "FLOW_ASSOCIATION_MISMATCH", "relationship_ref": ref})
    return Document(
        {
            "scenario_id": request["id"],
            "outcome": "passed" if payload["outcome"] == "passed" and not mismatches else "failed",
            "mismatches": mismatches,
            "native_result": payload,
        }
    )
