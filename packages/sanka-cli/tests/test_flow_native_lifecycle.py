# SPDX-License-Identifier: AGPL-3.0-only
"""Native lifecycle control tests; synthetic artifacts do not prove hosted execution."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_flow_lifecycle import Target
from test_flow_native_verification import (
    ArtifactReader,
    FixtureRecords,
    make_case,
    make_result,
    profile,
)
from test_flow_planner import snapshot

from sanka.runtime.flow.lifecycle import FlowLifecycle
from sanka.runtime.flow.model import (
    Claim,
    Document,
    FlowError,
    FlowPlan,
    Installation,
    TargetSnapshot,
)
from sanka.runtime.flow.planner import plan_reconstruction
from sanka.runtime.flow.sqlite import SqliteInstallationStore
from sanka.runtime.flow.verification_plan import (
    validate_effective_blueprint,
    verification_blueprint,
)
from sanka_extensions.flow import (
    ArtifactIdentity,
    Blueprint,
    BlueprintOrigin,
    NativeBillingScenario,
    NativeOrderBillingWorkflow,
    Resource,
)
from sanka_extensions.flow.native_verification import NATIVE_BILLING_CASES

type NativeCases = dict[
    str, tuple[NativeOrderBillingWorkflow, NativeBillingScenario, FixtureRecords]
]


class NativeTarget(Target):
    def __init__(
        self,
        store: SqliteInstallationStore,
        reader: ArtifactReader,
        cases: NativeCases,
    ) -> None:
        super().__init__(store)
        self.reader, self.cases = reader, cases
        self.before_scenario: Callable[[], Awaitable[object]] | None = None
        self.after_result: Callable[[dict[str, Any]], None] | None = None

    async def run_scenario(
        self,
        scenario: Document,
        installation: Installation,
        claim: Claim,
    ) -> Document:
        await self.store.assert_claim(claim)
        self.scenarios += 1
        if self.before_scenario:
            await self.before_scenario()
        native, selected, fixtures = self.cases[scenario.to_dict()["id"]]
        result = make_result(self.reader, native, selected, fixtures).to_dict()
        result["installation_id"] = installation.id
        result["workflow_target_id"] = "target-billing"
        result["outcome"] = self.scenario_outcome
        if self.after_result:
            self.after_result(result)
        return Document(result)

    async def read_verification_artifact(self, identity: Document, claim: Claim) -> Document:
        await self.store.assert_claim(claim)
        return await self.reader.read_verification_artifact(identity, claim)


async def setup(
    tmp_path: Path,
    *,
    clock: Callable[[], float] = time.time,
    renewal: float = 30,
) -> tuple[SqliteInstallationStore, NativeTarget, FlowPlan, FlowLifecycle]:
    store = SqliteInstallationStore(
        tmp_path / "native.db", installation_id="installation", target=Target.target, clock=clock
    )
    reader = ArtifactReader()
    blueprint, cases = make_blueprint(reader, profile())
    plan = plan_reconstruction(
        blueprint=blueprint, installation=await store.load(), observed=snapshot()
    )
    # Exercise the actual bounded plan and ledger, including the 2,001-record case.
    assert FlowPlan(Document(plan.to_dict())).digest == plan.digest
    await store.save_plan(plan)
    target = NativeTarget(store, reader, cases)
    lifecycle = FlowLifecycle(store, target, verification_renewal_seconds=renewal)
    await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="construct")
    return store, target, plan, lifecycle


def make_blueprint(
    reader: ArtifactReader,
    native: NativeOrderBillingWorkflow,
) -> tuple[Blueprint, NativeCases]:
    cases: NativeCases = {}
    for case in NATIVE_BILLING_CASES:
        _, _, selected, fixtures = make_case(case, reader=reader, native=native)
        cases[case] = (native, selected, fixtures)
    identity = ArtifactIdentity("sanka/billing", "1", "sha256:" + "b" * 64)
    blueprint = Blueprint(
        "billing",
        "1",
        BlueprintOrigin("template", identity),
        identity,
        (Resource("billing", "workflow", native.to_dict()),),
        parameters={
            "target": {"id": Target.target, "revision": "1"},
            "values": {},
            "definition_parameters": {
                "native_configuration": native.configuration,
                "native_verification": [value[1].to_dict() for value in cases.values()],
            },
        },
        scenarios=tuple(value[1] for value in cases.values()),
        schema_version="sanka-flow-blueprint/v4",
    )
    return blueprint, cases


async def test_complete_native_lifecycle_persists_all_cases_and_recovers_activation(
    tmp_path: Path,
) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    try:
        verified = await lifecycle.verify(plan, attempt_id="verify")
        payload = verified.to_dict()
        assert payload["schema_version"] == "sanka-flow-verification/v2"
        assert payload["outcome"] == "passed"
        assert len(payload["results"]) == len(NATIVE_BILLING_CASES)
        assert (
            next(r for r in payload["results"] if r["scenario_id"] == "bulk")[
                "checked_record_count"
            ]
            == 2001
        )
        assert await store.evidence(plan.digest, "verification") == verified
        # Cached verification cannot repeat imports or invoice creation.
        assert await lifecycle.verify(plan, attempt_id="retry-verify") == verified
        assert target.scenarios == len(NATIVE_BILLING_CASES)
        target.interrupt_after_activation = True
        with pytest.raises(TimeoutError):
            await lifecycle.activate(
                plan, approved_verification_digest=verified.digest, attempt_id="activate"
            )
        receipt = await lifecycle.activate(
            plan, approved_verification_digest=verified.digest, attempt_id="recover"
        )
        assert receipt == await lifecycle.activate(
            plan, approved_verification_digest=verified.digest, attempt_id="repeat"
        )
        assert target.activations == 1
        assert target.resources["target-billing"].active
    finally:
        await store.close()


@pytest.mark.parametrize("outcome", ["failed", "skipped"])
async def test_failed_or_skipped_native_execution_cannot_activate(
    tmp_path: Path, outcome: str
) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    try:
        target.scenario_outcome = outcome
        verified = await lifecycle.verify(plan, attempt_id="verify")
        assert verified.to_dict()["outcome"] == "failed"
        with pytest.raises(FlowError) as error:
            await lifecycle.activate(
                plan, approved_verification_digest=verified.digest, attempt_id="activate"
            )
        assert error.value.code == "FLOW_NOT_VERIFIED"
        assert target.activations == 0
    finally:
        await store.close()


@pytest.mark.parametrize("field", ["mapping", "configuration_digest"])
async def test_changed_native_mapping_or_configuration_cannot_produce_receipt(
    tmp_path: Path, field: str
) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    try:
        target.after_result = lambda result: result.update({field: "stale"})
        with pytest.raises(FlowError) as error:
            await lifecycle.verify(plan, attempt_id="verify")
        assert error.value.code == "FLOW_SCENARIO_MISMATCH"
        assert await store.evidence(plan.digest, "verification") is None
    finally:
        await store.close()


async def test_changed_native_graph_rejects_completed_verification(tmp_path: Path) -> None:
    store, target, plan, lifecycle = await setup(tmp_path)
    try:
        verified = await lifecycle.verify(plan, attempt_id="verify")
        original = target.resources["target-billing"]
        target.resources["target-billing"] = replace(original, revision="user-edited")
        with pytest.raises(FlowError) as error:
            await lifecycle.activate(
                plan, approved_verification_digest=verified.digest, attempt_id="activate"
            )
        assert error.value.code == "FLOW_TARGET_STALE"
        assert target.activations == 0
    finally:
        await store.close()


async def test_native_verification_renews_one_generation_while_host_is_running(
    tmp_path: Path,
) -> None:
    store, target, plan, lifecycle = await setup(tmp_path, renewal=0.001)
    original_claim = store.claim
    renewed = asyncio.Event()
    claims = []

    async def tracked_claim(plan_digest: str, attempt_id: str) -> Claim:
        claim = await original_claim(plan_digest, attempt_id)
        claims.append(claim)
        if len(claims) >= 3:
            renewed.set()
        return claim

    store.claim = tracked_claim  # type: ignore[method-assign]
    target.before_scenario = renewed.wait
    try:
        verified = await asyncio.wait_for(lifecycle.verify(plan, attempt_id="verify"), 20)
        assert verified.to_dict()["outcome"] == "passed"
        assert len(claims) > len(NATIVE_BILLING_CASES) + 1
        assert len({claim.generation for claim in claims}) == 1
    finally:
        await store.close()


async def test_expired_claim_cancels_native_work_without_resurrection(tmp_path: Path) -> None:
    now = [1000.0]
    store, target, plan, lifecycle = await setup(tmp_path, clock=lambda: now[0], renewal=0.001)
    cancelled = asyncio.Event()

    async def exceed_lease() -> None:
        now[0] += 121
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    target.before_scenario = exceed_lease
    try:
        with pytest.raises(FlowError) as error:
            await asyncio.wait_for(lifecycle.verify(plan, attempt_id="verify"), 5)
        assert error.value.code == "FLOW_ATTEMPT_FENCED"
        assert cancelled.is_set()
        assert await store.evidence(plan.digest, "verification") is None
    finally:
        await store.close()


async def test_expired_claim_between_scenarios_cannot_resume_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    store, target, plan, lifecycle = await setup(tmp_path, clock=lambda: now[0])
    original_observe = target.observe

    async def slow_readback(target_ids: tuple[str, ...]) -> TargetSnapshot:
        observed = await original_observe(target_ids)
        if target.scenarios == 1:
            now[0] += 121
        return observed

    monkeypatch.setattr(target, "observe", slow_readback)
    try:
        with pytest.raises(FlowError) as error:
            await lifecycle.verify(plan, attempt_id="verify")
        assert error.value.code == "FLOW_ATTEMPT_FENCED"
        assert target.scenarios == 1
        assert await store.evidence(plan.digest, "verification") is None
    finally:
        await store.close()


@pytest.mark.parametrize("interval", [True, 0, -1, 61, float("nan"), float("inf"), "30"])
def test_invalid_verification_renewal_interval_fails_at_construction(interval: Any) -> None:
    with pytest.raises(ValueError, match="renewal interval"):
        FlowLifecycle(cast(Any, None), cast(Any, None), verification_renewal_seconds=interval)


async def test_old_and_malformed_native_receipts_cannot_authorize_activation(
    tmp_path: Path,
) -> None:
    store, _, plan, lifecycle = await setup(tmp_path)
    try:
        verified = await lifecycle.verify(plan, attempt_id="verify")
        mutations: list[Callable[[dict[str, Any]], object]] = [
            lambda p: p.update(schema_version="sanka-flow-verification/v1"),
            lambda p: p["results"].pop(),
            lambda p: p["results"][0].update(scenario_id=[]),
            lambda p: p["results"][0].update(mismatch_count=False),
            lambda p: p["results"][0].update(checked_artifacts=True),
            lambda p: p["results"][0].update(checked_artifacts=[]),
            lambda p: p["results"][0]["native_result"].update(configuration_digest="stale"),
            lambda p: p["results"][0]["native_result"].update(deliveries=[None]),
            lambda p: p["results"][0]["native_result"].update(workflow_target_id="foreign"),
        ]
        for mutate in mutations:
            payload = verified.to_dict()
            mutate(payload)
            with pytest.raises(FlowError) as error:
                lifecycle._validate_native_verification(plan, payload)
            assert error.value.code == "FLOW_NOT_VERIFIED"
    finally:
        await store.close()


async def test_completed_native_error_does_not_mask_expired_claim(tmp_path: Path) -> None:
    now = [1000.0]
    store, target, plan, lifecycle = await setup(tmp_path, clock=lambda: now[0], renewal=0.001)

    async def expired_error() -> None:
        now[0] += 121
        raise RuntimeError("Host failed after its lease expired")

    target.before_scenario = expired_error
    try:
        with pytest.raises(FlowError) as error:
            await lifecycle.verify(plan, attempt_id="verify")
        assert error.value.code == "FLOW_ATTEMPT_FENCED"
        assert await store.evidence(plan.digest, "verification") is None
    finally:
        await store.close()


async def test_managed_update_verifies_user_settings_without_adopting_them_as_baseline(
    tmp_path: Path,
) -> None:
    store, target, _, lifecycle = await setup(tmp_path)
    try:
        user_profile = replace(profile(), invoice_due_days=45)
        original = target.resources["target-billing"]
        target.resources["target-billing"] = replace(
            original, revision="user-edit", configuration=Document(user_profile.to_dict())
        )
        target.revision += 1
        desired_profile = replace(profile(), interval_minutes=60)
        desired, desired_cases = make_blueprint(target.reader, desired_profile)
        installation, observed = await store.load(), await target.observe(())
        blocked = plan_reconstruction(
            blueprint=desired, installation=installation, observed=observed
        )
        assert not blocked.applicable
        assert {b["code"] for b in blocked.to_dict()["blockers"]} == {
            "FLOW_VERIFICATION_CONFIGURATION_MISMATCH"
        }
        with pytest.raises(FlowError):
            await lifecycle.construct(blocked, approved_digest=blocked.digest, attempt_id="blocked")
        effective_profile = replace(desired_profile, invoice_due_days=45)
        effective, cases = make_blueprint(target.reader, effective_profile)
        plan = plan_reconstruction(
            blueprint=desired,
            verification_blueprint=effective,
            installation=installation,
            observed=observed,
        )
        assert plan.applicable
        assert plan.to_dict()["schema_version"] == "sanka-flow-plan/v2"
        await store.save_plan(plan)
        await lifecycle.construct(plan, approved_digest=plan.digest, attempt_id="update")
        assert target.resources["target-billing"].configuration == Document(
            effective_profile.to_dict()
        )
        assert (await store.load()).resources[0].desired == Document(desired_profile.to_dict())
        target.cases = desired_cases
        with pytest.raises(FlowError) as stale:
            await lifecycle.verify(plan, attempt_id="stale-original-profile")
        assert stale.value.code == "FLOW_SCENARIO_MISMATCH"
        assert await store.evidence(plan.digest, "verification") is None
        target.cases = cases
        verified = await lifecycle.verify(plan, attempt_id="verify-effective")
        assert verified.to_dict()["outcome"] == "passed"
        assert (
            verified.to_dict()["verification_blueprint_digest"]
            == Document(effective.to_dict()).digest
        )
        # A later template edit overlaps the preserved user value and must conflict.
        later, _ = make_blueprint(target.reader, replace(desired_profile, invoice_due_days=60))
        conflicted = plan_reconstruction(
            blueprint=later, installation=await store.load(), observed=await target.observe(())
        )
        assert "FLOW_FIELD_CONFLICT" in {b["code"] for b in conflicted.to_dict()["blockers"]}
        assert target.writes == 2
        await lifecycle.activate(
            plan, approved_verification_digest=verified.digest, attempt_id="activate-effective"
        )
        assert target.resources["target-billing"].active
        assert (await store.load()).resources[0].desired == Document(desired_profile.to_dict())
    finally:
        await store.close()


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["origin"]["identity"].update(id="sanka/different-template"),
        lambda p: p["parameters"]["target"].update(id="foreign"),
        lambda p: p["parameters"]["definition_parameters"]["native_configuration"].update(
            invoice_due_days=60
        ),
        lambda p: p["parameters"]["definition_parameters"]["native_configuration"].update(
            interval_minutes=30.0
        ),
        lambda p: p["parameters"]["definition_parameters"]["native_verification"].pop(),
    ],
)
def test_effective_artifact_rejects_provenance_and_request_metadata_changes(
    change: Callable[[dict[str, Any]], object],
) -> None:
    desired, _ = make_blueprint(ArtifactReader(), profile())
    effective = desired.to_dict()
    change(effective)
    with pytest.raises(FlowError) as error:
        validate_effective_blueprint(
            Document(desired.to_dict()),
            Document(effective),
            [{"logical_id": "billing", "configuration": profile().to_dict()}],
        )
    assert error.value.code == "FLOW_VERIFICATION_CONFIGURATION_MISMATCH"


def test_effective_artifact_digest_is_part_of_the_reviewed_plan() -> None:
    blueprint, _ = make_blueprint(ArtifactReader(), profile())
    plan = plan_reconstruction(
        blueprint=blueprint,
        verification_blueprint=blueprint,
        installation=Installation("installation", Target.target),
        observed=snapshot(),
    )
    payload = plan.to_dict()
    payload["verification_blueprint_digest"] = "sha256:" + "0" * 64
    with pytest.raises(FlowError) as error:
        verification_blueprint(FlowPlan(Document(payload)))
    assert error.value.code == "FLOW_PLAN_INVALID"
    for missing in ("verification_blueprint", "verification_blueprint_digest"):
        payload = plan.to_dict()
        del payload[missing]
        with pytest.raises(FlowError) as incomplete:
            verification_blueprint(FlowPlan(Document(payload)))
        assert incomplete.value.code == "FLOW_PLAN_INVALID"
