# SPDX-License-Identifier: AGPL-3.0-only
"""Flow lifecycle guarantees; fake target outcomes are not business-equivalence proof."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_flow_planner import Blueprint, Resource, snapshot

from sanka.runtime.flow.host import ActivationRecovery, Recovery
from sanka.runtime.flow.lifecycle import FlowLifecycle
from sanka.runtime.flow.model import (
    Claim,
    Document,
    FlowError,
    FlowPlan,
    Installation,
    ObservedResource,
    OperationReceipt,
    TargetSnapshot,
)
from sanka.runtime.flow.planner import plan_reconstruction
from sanka.runtime.flow.sqlite import SqliteInstallationStore


class Target:
    target = "sanka:test-workspace"

    def __init__(self, store: SqliteInstallationStore) -> None:
        self.store = store
        self.resources: dict[str, ObservedResource] = {}
        self.revision = 1
        self.receipts: dict[str, OperationReceipt] = {}
        self.activation: Document | None = None
        self.writes = self.activations = self.scenarios = 0
        self.interrupt_after_write = False
        self.interrupt_after_activation = False
        self.unknown = False
        self.unsafe_active = False
        self.scenario_outcome = "passed"
        self.wrong_scenario = False
        self.isolated = True
        self.activation_changes_configuration = False

    async def observe(self, target_ids: tuple[str, ...]) -> TargetSnapshot:
        return snapshot(
            *sorted(self.resources.values(), key=lambda r: r.target_id),
            revision=f"workspace-r{self.revision}",
        )

    async def recover(self, operation: Document, claim: Claim) -> Recovery:
        receipt = self.receipts.get(operation.to_dict()["id"])
        if self.unknown:
            return Recovery("unknown")
        return Recovery("found", receipt) if receipt else Recovery("absent")

    async def construct(
        self, operation: Document, expected: Document, bindings: Document, claim: Claim
    ) -> OperationReceipt:
        await self.store.assert_claim(claim)
        assert Document((await self.observe(())).to_dict()) == expected
        op = operation.to_dict()
        # No await between this check and effect in the control fake. Production
        # adapters must enforce these preconditions in their native transaction.
        self.writes += 1
        self.revision += 1
        if op["action"] == "remove":
            self.resources.pop(op["target_id"], None)
            resource = None
        else:
            target_id = op["target_id"] or f"target-{op['logical_id']}"
            resource = ObservedResource(
                target_id,
                op["kind"],
                f"r{self.revision}",
                Document(op["configuration"]),
                self.unsafe_active,
            )
            self.resources[target_id] = resource
            for dependency in op["depends_on"]:
                assert dependency in bindings.to_dict()
        receipt = OperationReceipt(
            op["id"], operation.digest, resource, f"workspace-r{self.revision}"
        )
        self.receipts[op["id"]] = receipt
        if self.interrupt_after_write:
            self.interrupt_after_write = False
            raise TimeoutError("Process lost the response after target commit")
        return receipt

    async def run_scenario(
        self, scenario: Document, installation: Installation, claim: Claim
    ) -> Document:
        self.scenarios += 1
        return Document(
            {
                "scenario_id": "wrong" if self.wrong_scenario else scenario.to_dict()["id"],
                "scenario_digest": scenario.digest,
                "installation_id": installation.id,
                "workflow_target_id": "target-sales",
                "outcome": self.scenario_outcome,
                "isolated": self.isolated,
                "event_record_bindings": {"deal-1": "native-deal-1"},
                "completed_event_digests": [
                    Document(event).digest for event in scenario.to_dict()["events"]
                ],
                "created_records": (
                    [{"id": "quote-1", "object_ref": "quote", "fields": {}, "associations": {}}]
                    if scenario.to_dict()["expected_created_count"]
                    else []
                ),
                "evidence": {"kind": "control-test-double"},
            }
        )

    async def recover_activation(self, intent: Document, claim: Claim) -> ActivationRecovery:
        if self.unknown:
            return ActivationRecovery("unknown")
        return (
            ActivationRecovery("found", self.activation)
            if self.activation
            else ActivationRecovery("absent")
        )

    async def activate(self, intent: Document, expected: Document, claim: Claim) -> Document:
        await self.store.assert_claim(claim)
        assert Document((await self.observe(())).to_dict()) == expected
        self.activations += 1
        self.revision += 1
        for target_id in intent.to_dict()["workflow_target_ids"]:
            resource = self.resources[target_id]
            self.resources[target_id] = replace(resource, active=True, revision=f"r{self.revision}")
        if self.activation_changes_configuration:
            key = next(iter(self.resources))
            self.resources[key] = replace(
                self.resources[key], configuration=Document({"unverified": True})
            )
        self.activation = Document((await self.observe(())).to_dict())
        if self.interrupt_after_activation:
            self.interrupt_after_activation = False
            raise TimeoutError("Activation response lost")
        return self.activation


async def setup(
    tmp_path: Path, *, dependencies: bool = False, scenarios: bool = True
) -> tuple[SqliteInstallationStore, Target, FlowPlan, FlowLifecycle]:
    store = SqliteInstallationStore(
        tmp_path / "private" / "flow.db", installation_id="install", target="sanka:test-workspace"
    )
    graph = {"nodes": [{"kind": "action", "spec": {"object_ref": "quote"}}]}
    resources: tuple[Resource, ...] = (Resource("sales", "workflow", graph),)
    if dependencies:
        resources = (
            Resource("sales", "workflow", graph, ("field",)),
            Resource("field", "property", {"name": "Stage"}),
        )
    cases: tuple[dict[str, Any], ...] = (
        tuple(
            {
                "id": case,
                "workflow_id": "sales",
                "case": case,
                "required": True,
                "events": [{"id": "event-1", "record_id": "deal-1", "before": {}, "after": {}}]
                * (2 if case == "retry" else 1),
                "expected_created_count": 0 if case == "no_match" else 1,
                "expected_fields": [],
                "expected_associations": [],
            }
            for case in ("no_match", "match", "retry")
        )
        if scenarios
        else ()
    )
    plan = plan_reconstruction(
        blueprint=Blueprint(resources, scenarios=cases),
        installation=await store.load(),
        observed=snapshot(),
    )
    await store.save_plan(plan)
    target = Target(store)
    return store, target, plan, FlowLifecycle(store, target)


async def test_constructs_inactive_and_repeat_preserves_ids(tmp_path: Path) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    first = await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="first")
    second = await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="again")
    assert first == second
    assert target.writes == 1
    assert not target.resources["target-sales"].active
    assert target.activations == 0
    await store.close()


async def test_lost_write_response_recovers_original_ids_and_resumes_dependencies(
    tmp_path: Path,
) -> None:
    store, target, plan, lifecycle = await setup(tmp_path, dependencies=True)
    target.interrupt_after_write = True
    with pytest.raises(TimeoutError):
        await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="first")
    assert target.writes == 1
    await store.close()
    reopened = SqliteInstallationStore(
        tmp_path / "private" / "flow.db", installation_id="install", target="sanka:test-workspace"
    )
    target.store = reopened
    resumed = FlowLifecycle(reopened, target)
    result = await resumed.construct(plan, approved_digest=plan.digest, attempt_id="resume")
    assert target.writes == 2
    assert len(result.to_dict()["installation"]["resources"]) == 2
    assert set(target.resources) == {"target-sales", "target-field"}
    await reopened.close()


async def test_unknown_outcome_stops_without_resending(tmp_path: Path) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    target.unknown = True
    with pytest.raises(FlowError, match="uncertain operation"):
        await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="first")
    assert target.writes == 0
    await store.close()


async def test_stale_zero_effect_plan_can_be_discarded_before_fresh_planning(
    tmp_path: Path,
) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    target.revision += 1
    with pytest.raises(FlowError, match="revision changed"):
        await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="stale")
    assert target.writes == 0
    discarded = await lifecycle.discard(plan, attempt_id="discard")
    assert discarded.to_dict()["outcome"] == "discarded"
    with pytest.raises(FlowError, match="Discarded plan"):
        await store.claim(plan.digest, "retry-old")
    fresh = plan_reconstruction(
        blueprint=Blueprint((Resource("sales", "workflow", {"name": "Sales"}),)),
        installation=await store.load(),
        observed=await target.observe(()),
    )
    await store.save_plan(fresh)
    await lifecycle.construct(fresh, approved_digest=fresh.digest, attempt_id="fresh")
    assert target.writes == 1
    await store.close()


@pytest.mark.parametrize("mode", ["uncertain", "lost-response", "constructed"])
async def test_discard_cannot_forget_uncertain_or_existing_native_effects(
    tmp_path: Path, mode: str
) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    if mode == "uncertain":
        target.unknown = True
    elif mode == "lost-response":
        target.interrupt_after_write = True
        with pytest.raises(TimeoutError):
            await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="first")
    else:
        await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="first")
    with pytest.raises(FlowError, match=r"blocks discard|must be recovered"):
        await lifecycle.discard(plan, attempt_id="discard")
    target.unknown = False
    await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="recover")
    assert target.writes == 1
    await store.close()


async def test_stale_target_or_wrong_plan_approval_blocks_all_writes(tmp_path: Path) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    with pytest.raises(FlowError, match="exact reviewed plan"):
        await lifecycle.construct(plan, approved_digest="wrong", attempt_id="first")
    target.revision += 1
    with pytest.raises(FlowError, match="revision changed"):
        await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="first")
    assert target.writes == 0
    await store.close()


async def test_active_construction_is_rejected_as_invalid_host_receipt(tmp_path: Path) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    target.unsafe_active = True
    with pytest.raises(FlowError, match="remain inactive"):
        await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="first")
    assert await store.construction(plan.digest) is None
    await store.close()


async def test_verification_and_activation_require_exact_complete_evidence(tmp_path: Path) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="construct")
    with pytest.raises(FlowError, match="completed verification"):
        await lifecycle.activate(plan, approved_verification_digest="none", attempt_id="early")
    verification = await lifecycle.verify(plan, attempt_id="verify")
    assert verification.to_dict()["outcome"] == "passed"
    assert target.scenarios == 3
    assert not target.resources["target-sales"].active
    with pytest.raises(FlowError, match="exact verified revision"):
        await lifecycle.activate(plan, approved_verification_digest="other", attempt_id="wrong")
    activated = await lifecycle.activate(
        plan, approved_verification_digest=verification.digest, attempt_id="activate"
    )
    repeated = await lifecycle.activate(
        plan, approved_verification_digest=verification.digest, attempt_id="repeat"
    )
    assert activated == repeated
    assert target.activations == 1
    assert target.resources["target-sales"].active
    await store.close()


@pytest.mark.parametrize("outcome", ["failed", "skipped"])
async def test_failed_or_skipped_required_scenario_blocks_activation(
    tmp_path: Path, outcome: str
) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="construct")
    target.scenario_outcome = outcome
    verification = await lifecycle.verify(plan, attempt_id="verify")
    assert verification.to_dict()["outcome"] == "failed"
    with pytest.raises(FlowError, match="exact verified revision"):
        await lifecycle.activate(
            plan, approved_verification_digest=verification.digest, attempt_id="activate"
        )
    assert target.activations == 0
    await store.close()


async def test_missing_scenarios_wrong_identity_and_live_scenarios_cannot_verify(
    tmp_path: Path,
) -> None:
    store, _target, plan, lifecycle = await setup(tmp_path, scenarios=False)
    await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="construct")
    with pytest.raises(FlowError, match="requires scenarios"):
        await lifecycle.verify(plan, attempt_id="verify")
    await store.close()


@pytest.mark.parametrize("mode", ["identity", "isolation"])
async def test_scenario_identity_and_isolation_are_checked(tmp_path: Path, mode: str) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="construct")
    target.wrong_scenario = mode == "identity"
    target.isolated = mode != "isolation"
    with pytest.raises(FlowError):
        await lifecycle.verify(plan, attempt_id="verify")
    assert await store.evidence(plan.digest, "verification") is None
    await store.close()


async def test_changed_target_invalidates_verification_before_activation(tmp_path: Path) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="construct")
    verification = await lifecycle.verify(plan, attempt_id="verify")
    target.resources["target-sales"] = replace(
        target.resources["target-sales"], configuration=Document({"name": "User edit"})
    )
    with pytest.raises(FlowError, match="revision changed"):
        await lifecycle.activate(
            plan, approved_verification_digest=verification.digest, attempt_id="activate"
        )
    assert target.activations == 0
    await store.close()


async def test_lost_activation_response_is_recovered_without_second_activation(
    tmp_path: Path,
) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="construct")
    verification = await lifecycle.verify(plan, attempt_id="verify")
    target.interrupt_after_activation = True
    with pytest.raises(TimeoutError):
        await lifecycle.activate(
            plan, approved_verification_digest=verification.digest, attempt_id="activate"
        )
    later = plan_reconstruction(
        blueprint=Blueprint(()),
        installation=await store.load(),
        observed=await target.observe(()),
    )
    await store.save_plan(later)
    with pytest.raises(FlowError, match="previous plan"):
        await store.claim(later.digest, "competing-plan")
    await lifecycle.activate(
        plan, approved_verification_digest=verification.digest, attempt_id="recover"
    )
    assert target.activations == 1
    await store.close()


async def test_activation_cannot_rewrite_configuration(tmp_path: Path) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="construct")
    verification = await lifecycle.verify(plan, attempt_id="verify")
    target.activation_changes_configuration = True
    with pytest.raises(FlowError, match="unverified configuration"):
        await lifecycle.activate(
            plan, approved_verification_digest=verification.digest, attempt_id="activate"
        )
    assert await store.evidence(plan.digest, "activation") is None
    await store.close()


@pytest.mark.parametrize("schema", ["sanka-flow-blueprint/v3", "unknown-profile/v99"])
async def test_unimplemented_verification_profile_never_runs_or_activates(
    tmp_path: Path, schema: str
) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    payload = plan.to_dict()
    payload["blueprint"]["schema_version"] = schema
    candidate = FlowPlan(Document(payload))
    # This check must run before claims, store writes or target scenario calls.
    # In particular, a saved/fabricated legacy success receipt cannot promote
    # a native scheduled/batch profile into a verified installation.
    with pytest.raises(FlowError) as verification:
        await lifecycle.verify(candidate, attempt_id="native-verify")
    assert verification.value.code == "FLOW_VERIFICATION_PROFILE_UNSUPPORTED"
    with pytest.raises(FlowError) as activation:
        await lifecycle.activate(
            candidate, approved_verification_digest="legacy-success", attempt_id="native-activate"
        )
    assert activation.value.code == "FLOW_VERIFICATION_PROFILE_UNSUPPORTED"
    assert target.scenarios == target.activations == target.writes == 0
    assert await store.evidence(candidate.digest, "verification") is None
