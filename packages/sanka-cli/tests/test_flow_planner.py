# SPDX-License-Identifier: AGPL-3.0-only
"""Host-neutral Flow planning against observations and installation ownership."""

from dataclasses import dataclass
from typing import Any

import pytest

from sanka.runtime.flow.model import (
    Document,
    FlowError,
    Installation,
    ObservedResource,
    OwnedResource,
    TargetSnapshot,
)
from sanka.runtime.flow.planner import plan_reconstruction


@dataclass(frozen=True)
class Resource:
    id: str
    kind: str
    spec: dict[str, Any]
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "spec": self.spec,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class Blueprint:
    resources: tuple[Resource, ...]
    unsupported: tuple[dict[str, Any], ...] = ()
    scenarios: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "sanka-flow-blueprint/v1",
            "id": "sales",
            "revision": "v1",
            "resources": [r.to_dict() for r in self.resources],
            "unsupported": list(self.unsupported),
            "scenarios": list(self.scenarios),
        }


def snapshot(*resources: ObservedResource, revision: str = "workspace-r1") -> TargetSnapshot:
    return TargetSnapshot(
        "sanka:test-workspace",
        revision,
        resources,
        frozenset({"workflow", "property", "object"}),
        Document({"stage": "stage-id"}),
    )


def test_create_plan_is_deterministic_and_binds_observed_references() -> None:
    blueprint = Blueprint((Resource("sales", "workflow", {"name": "Sales"}),))
    installation = Installation("install", "sanka:test-workspace")
    plan = plan_reconstruction(blueprint=blueprint, installation=installation, observed=snapshot())
    again = plan_reconstruction(blueprint=blueprint, installation=installation, observed=snapshot())
    assert plan.digest == again.digest
    assert plan.applicable
    assert plan.to_dict()["construction"] == "inactive"
    assert plan.to_dict()["operations"][0]["action"] == "create"
    changed = plan_reconstruction(
        blueprint=blueprint, installation=installation, observed=snapshot(revision="r2")
    )
    assert changed.digest != plan.digest


def test_user_edit_and_template_update_merge_without_losing_baseline() -> None:
    installation = Installation(
        "install",
        "sanka:test-workspace",
        resources=(
            OwnedResource("sales", "w1", "workflow", "r1", Document({"name": "Sales", "x": 1})),
        ),
    )
    observed = snapshot(
        ObservedResource("w1", "workflow", "r2", Document({"name": "Mine", "x": 1}))
    )
    plan = plan_reconstruction(
        blueprint=Blueprint((Resource("sales", "workflow", {"name": "Sales", "x": 2}),)),
        installation=installation,
        observed=observed,
    )
    operation = plan.to_dict()["operations"][0]
    assert plan.applicable
    assert operation["configuration"] == {"name": "Mine", "x": 2}
    assert operation["desired"] == {"name": "Sales", "x": 2}
    assert operation["expected"]["revision"] == "r2"
    assert operation["preserved_paths"] == [["name"]]


def test_matching_name_does_not_establish_ownership() -> None:
    observed = snapshot(ObservedResource("unowned", "workflow", "r1", Document({"name": "Sales"})))
    plan = plan_reconstruction(
        blueprint=Blueprint((Resource("sales", "workflow", {"name": "Sales"}),)),
        installation=Installation("install", observed.target),
        observed=observed,
    )
    operation = plan.to_dict()["operations"][0]
    assert operation["action"] == "create"
    assert operation["target_id"] is None


def test_overlapping_change_and_active_in_place_update_are_blocked() -> None:
    installation = Installation(
        "install",
        "sanka:test-workspace",
        resources=(OwnedResource("sales", "w1", "workflow", "r1", Document({"x": 1, "y": 1})),),
    )
    plan = plan_reconstruction(
        blueprint=Blueprint((Resource("sales", "workflow", {"x": 2, "y": 2}),)),
        installation=installation,
        observed=snapshot(
            ObservedResource("w1", "workflow", "r2", Document({"x": 3, "y": 1}), True)
        ),
    )
    assert not plan.applicable
    assert {b["code"] for b in plan.to_dict()["blockers"]} == {
        "FLOW_FIELD_CONFLICT",
        "FLOW_ACTIVE_REPLACEMENT_UNSUPPORTED",
    }


def test_missing_owned_resource_blocks_duplicate_recreation() -> None:
    installation = Installation(
        "install",
        "sanka:test-workspace",
        resources=(OwnedResource("sales", "w1", "workflow", "r1", Document({"x": 1})),),
    )
    plan = plan_reconstruction(
        blueprint=Blueprint((Resource("sales", "workflow", {"x": 1}),)),
        installation=installation,
        observed=snapshot(),
    )
    assert not plan.applicable
    assert plan.to_dict()["blockers"][0]["code"] == "FLOW_OWNED_RESOURCE_MISSING"


def test_omission_preserves_and_removal_is_explicit_and_dependency_ordered() -> None:
    installation = Installation(
        "install",
        "sanka:test-workspace",
        resources=(
            OwnedResource("object", "o1", "object", "r1", Document({})),
            OwnedResource("sales", "w1", "workflow", "r1", Document({}), ("object",)),
        ),
    )
    observed = snapshot(
        ObservedResource("o1", "object", "r1", Document({})),
        ObservedResource("w1", "workflow", "r1", Document({})),
    )
    preserved = plan_reconstruction(
        blueprint=Blueprint(()), installation=installation, observed=observed
    )
    assert {op["action"] for op in preserved.to_dict()["operations"]} == {"preserve"}
    blocked = plan_reconstruction(
        blueprint=Blueprint(()),
        installation=installation,
        observed=observed,
        remove=("object",),
    )
    assert not blocked.applicable
    removed = plan_reconstruction(
        blueprint=Blueprint(()),
        installation=installation,
        observed=observed,
        remove=("object", "sales"),
    )
    assert removed.applicable
    assert [op["logical_id"] for op in removed.to_dict()["operations"]] == ["sales", "object"]


def test_unsupported_semantics_and_resource_kinds_block_construction() -> None:
    blueprint = Blueprint((Resource("send", "email", {}),), ({"code": "BRANCH_UNSUPPORTED"},))
    plan = plan_reconstruction(
        blueprint=blueprint,
        installation=Installation("install", "sanka:test-workspace"),
        observed=snapshot(),
    )
    assert not plan.applicable
    assert {b["code"] for b in plan.to_dict()["blockers"]} == {
        "BRANCH_UNSUPPORTED",
        "FLOW_RESOURCE_UNSUPPORTED",
    }


def test_create_dependency_order_and_cycle_rejection() -> None:
    blueprint = Blueprint(
        (Resource("sales", "workflow", {}, ("object",)), Resource("object", "object", {}))
    )
    plan = plan_reconstruction(
        blueprint=blueprint,
        installation=Installation("install", "sanka:test-workspace"),
        observed=snapshot(),
    )
    assert [op["logical_id"] for op in plan.to_dict()["operations"]] == ["object", "sales"]
    cycle = Blueprint((Resource("a", "object", {}, ("b",)), Resource("b", "object", {}, ("a",))))
    with pytest.raises(FlowError, match="cycle"):
        plan_reconstruction(
            blueprint=cycle,
            installation=Installation("install", "sanka:test-workspace"),
            observed=snapshot(),
        )


def test_cross_target_and_removing_unowned_resource_are_rejected() -> None:
    with pytest.raises(FlowError, match="targets differ"):
        plan_reconstruction(
            blueprint=Blueprint(()),
            installation=Installation("install", "other"),
            observed=snapshot(),
        )
    with pytest.raises(FlowError, match="owned resource"):
        plan_reconstruction(
            blueprint=Blueprint(()),
            installation=Installation("install", "sanka:test-workspace"),
            observed=snapshot(),
            remove=("unowned",),
        )
