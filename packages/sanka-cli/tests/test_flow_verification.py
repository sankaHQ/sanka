# SPDX-License-Identifier: AGPL-3.0-only
"""Native scenario evidence must substantiate each expected business result."""

from typing import Any

import pytest

from sanka.runtime.flow.model import Document, FlowError, Installation, OwnedResource
from sanka.runtime.flow.verification import evaluate_scenario, required_scenarios


def inputs() -> tuple[dict[str, Any], Document, Installation, dict[str, Any]]:
    event = {
        "id": "event-1",
        "record_id": "deal-1",
        "before": {"stage": "open"},
        "after": {"stage": "quote", "amount": 123},
    }
    scenario = Document(
        {
            "id": "retry",
            "workflow_id": "sales",
            "case": "retry",
            "events": [event, event],
            "required": True,
            "expected_created_count": 1,
            "expected_fields": [
                {"field_ref": "status", "value": {"kind": "literal", "value": "draft"}},
                {
                    "field_ref": "amount",
                    "value": {
                        "kind": "event_field",
                        "phase": "after",
                        "field_ref": "amount",
                    },
                },
                {"field_ref": "approved", "value": {"kind": "literal", "value": False}},
                {"field_ref": "nullable", "value": {"kind": "literal", "value": None}},
            ],
            "expected_associations": [
                {"relationship_ref": "deal", "record": {"kind": "event_record"}, "required": True},
                {
                    "relationship_ref": "owner",
                    "record": {
                        "kind": "reference",
                        "reference_id": "owner-record",
                    },
                    "required": True,
                },
            ],
        }
    )
    blueprint = {
        "resources": [
            {
                "id": "sales",
                "kind": "workflow",
                "spec": {
                    "nodes": [{"kind": "action", "spec": {"object_ref": "quote"}}],
                },
            }
        ],
        "references": [
            {"id": "owner-record", "kind": "record", "key": "user-1", "binding": "existing"}
        ],
        "scenarios": [scenario.to_dict()],
    }
    installation = Installation(
        "installation-1",
        "test",
        resources=(OwnedResource("sales", "workflow-1", "workflow", "r1", Document({})),),
    )
    result = {
        "scenario_id": "retry",
        "scenario_digest": scenario.digest,
        "installation_id": installation.id,
        "workflow_target_id": "workflow-1",
        "outcome": "passed",
        "isolated": True,
        "event_record_bindings": {"deal-1": "native-deal-1"},
        "completed_event_digests": [Document(event).digest, Document(event).digest],
        "created_records": [
            {
                "id": "quote-1",
                "object_ref": "quote",
                "fields": {"status": "draft", "amount": 123, "approved": False, "nullable": None},
                "associations": {"deal": ["native-deal-1"], "owner": ["user-1"]},
            }
        ],
    }
    return blueprint, scenario, installation, result


def test_shared_comparator_accepts_exact_native_readback() -> None:
    blueprint, scenario, installation, result = inputs()
    evidence = evaluate_scenario(
        blueprint=blueprint, scenario=scenario, installation=installation, result=Document(result)
    )
    assert evidence.to_dict()["outcome"] == "passed"
    assert evidence.to_dict()["native_result"] == result


@pytest.mark.parametrize(
    "mode",
    [
        "wrong-value",
        "number-for-bool",
        "missing-null",
        "wrong-object",
        "missing-association",
        "extra-association",
        "duplicate-record",
        "duplicate-id",
        "missing-retry",
        "wrong-owner",
        "logical-record-id",
    ],
)
def test_native_pass_cannot_override_incorrect_record_readback(mode: str) -> None:
    blueprint, scenario, installation, result = inputs()
    record = result["created_records"][0]
    if mode == "wrong-value":
        record["fields"]["amount"] = "123"
    elif mode == "number-for-bool":
        record["fields"]["approved"] = 0
    elif mode == "missing-null":
        record["fields"].pop("nullable")
    elif mode == "wrong-object":
        record["object_ref"] = "invoice"
    elif mode == "missing-association":
        record["associations"].pop("deal")
    elif mode == "extra-association":
        record["associations"]["deal"].append("deal-2")
    elif mode == "wrong-owner":
        record["associations"]["owner"] = ["user-2"]
    elif mode == "logical-record-id":
        record["associations"]["deal"] = ["deal-1"]
    elif mode in {"duplicate-record", "duplicate-id"}:
        result["created_records"].append(
            dict(record, id="quote-2" if mode == "duplicate-record" else "quote-1")
        )
    elif mode == "missing-retry":
        result["completed_event_digests"].pop()
    evidence = evaluate_scenario(
        blueprint=blueprint, scenario=scenario, installation=installation, result=Document(result)
    )
    assert evidence.to_dict()["outcome"] == "failed"
    assert evidence.to_dict()["mismatches"]


def test_required_scenarios_cannot_omit_no_match_or_skip_retry() -> None:
    blueprint, scenario, _installation, _result = inputs()
    with pytest.raises(FlowError, match="no_match, match and retry"):
        required_scenarios(blueprint)
    scenarios = []
    for case in ("no_match", "match", "retry"):
        value = scenario.to_dict()
        value.update(id=case, case=case, expected_created_count=0 if case == "no_match" else 1)
        if case != "retry":
            value["events"] = value["events"][:1]
        scenarios.append(value)
    blueprint["scenarios"] = scenarios
    assert required_scenarios(blueprint) == {"no_match", "match", "retry"}
    scenarios[-1]["events"] = scenarios[-1]["events"][:1]
    with pytest.raises(FlowError, match="Invalid reviewed scenario"):
        required_scenarios(blueprint)


@pytest.mark.parametrize("bindings", [None, {}, {"deal-1": ""}, {"other": "native-deal-1"}])
def test_native_result_must_bind_the_exact_isolated_event_record(bindings: Any) -> None:
    blueprint, scenario, installation, result = inputs()
    result["event_record_bindings"] = bindings
    with pytest.raises(FlowError, match="record bindings"):
        evaluate_scenario(
            blueprint=blueprint,
            scenario=scenario,
            installation=installation,
            result=Document(result),
        )
