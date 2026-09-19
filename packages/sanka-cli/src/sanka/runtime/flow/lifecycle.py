# SPDX-License-Identifier: AGPL-3.0-only
"""Construct, read back, verify and explicitly activate one reviewed Flow plan."""

from __future__ import annotations

import asyncio
import math
from contextlib import suppress
from typing import Any, cast

from sanka.runtime.flow.host import FlowTarget
from sanka.runtime.flow.model import (
    Claim,
    Document,
    FlowError,
    FlowPlan,
    Installation,
    OperationReceipt,
    OwnedResource,
)
from sanka.runtime.flow.state import InstallationStore
from sanka.runtime.flow.verification import evaluate_scenario, required_scenarios
from sanka.runtime.flow.verification_plan import verification_blueprint


def _same(expected: Document, actual: Document) -> None:
    if expected.digest != actual.digest:
        raise FlowError("FLOW_TARGET_STALE", "Target configuration or reference revision changed")


def _require_verification_profile(plan: FlowPlan) -> None:
    blueprint = verification_blueprint(plan)
    version = blueprint.get("schema_version")
    if version not in {
        "sanka-flow-blueprint/v1",
        "sanka-flow-blueprint/v2",
        "sanka-flow-blueprint/v4",
    }:
        raise FlowError(
            "FLOW_VERIFICATION_PROFILE_UNSUPPORTED",
            "This Flow profile has no native scenario verifier; "
            "verification and activation are unavailable",
        )
    if version == "sanka-flow-blueprint/v4":
        required_scenarios(blueprint)


def _resource_ids(observation: Document) -> tuple[str, ...]:
    return tuple(r["target_id"] for r in observation.to_dict()["resources"])


async def _observe(host: FlowTarget, expected: Document) -> Document:
    return Document((await host.observe(_resource_ids(expected))).to_dict())


def _advance(expected: Document, operation: Document, receipt: OperationReceipt) -> Document:
    op = operation.to_dict()
    if (receipt.operation_id, receipt.request_digest) != (op["id"], operation.digest):
        raise FlowError("FLOW_RECEIPT_MISMATCH", "Target returned a receipt for different input")
    observed = expected.to_dict()
    resources = {r["target_id"]: r for r in observed["resources"]}
    if not receipt.target_revision:
        raise FlowError("FLOW_RECEIPT_MISMATCH", "Target receipt has no resulting revision")
    if op["action"] == "remove":
        if receipt.resource is not None:
            raise FlowError("FLOW_RECEIPT_MISMATCH", "Removal receipt still contains a resource")
        resources.pop(op["target_id"], None)
    else:
        resource = receipt.resource
        if resource is None or not resource.target_id or not resource.revision:
            raise FlowError("FLOW_RECEIPT_MISMATCH", "Construction returned no resource identity")
        if resource.kind != op["kind"] or resource.configuration != Document(op["configuration"]):
            raise FlowError("FLOW_RECEIPT_MISMATCH", "Readback differs from reviewed configuration")
        if resource.kind == "workflow" and resource.active:
            raise FlowError("FLOW_CONSTRUCTION_ACTIVE", "Constructed workflow must remain inactive")
        if op["action"] == "update" and resource.target_id != op["target_id"]:
            raise FlowError("FLOW_RECEIPT_MISMATCH", "Update changed resource identity")
        if op["action"] == "create" and resource.target_id in resources:
            raise FlowError(
                "FLOW_OWNERSHIP_CONFLICT", "Create attempted to adopt an existing resource"
            )
        resources[resource.target_id] = resource.to_dict()
    observed["resources"] = sorted(resources.values(), key=lambda r: r["target_id"])
    observed["revision"] = receipt.target_revision
    return Document(observed)


async def _release(store: InstallationStore, claim: Claim) -> None:
    try:
        await store.release(claim)
    except FlowError as error:
        if error.code != "FLOW_ATTEMPT_FENCED":
            raise


class FlowLifecycle:
    def __init__(
        self,
        store: InstallationStore,
        target: FlowTarget,
        *,
        verification_renewal_seconds: float = 30,
    ) -> None:
        if (
            type(verification_renewal_seconds) not in {int, float}
            or not math.isfinite(verification_renewal_seconds)
            or not 0 < verification_renewal_seconds <= 60
        ):
            raise ValueError(
                "verification lease renewal interval must be finite and within (0, 60]"
            )
        self.store, self.target = store, target
        self._verification_renewal_seconds = verification_renewal_seconds

    def _check(self, plan: FlowPlan, approved_digest: str) -> None:
        if plan.digest != approved_digest:
            raise FlowError(
                "FLOW_APPROVAL_MISMATCH", "Approval must identify the exact reviewed plan"
            )
        if not plan.applicable:
            raise FlowError(
                "FLOW_PLAN_BLOCKED", "Resolve every unsupported semantic or conflict first"
            )
        if self.target.target != plan.to_dict()["installation"]["target"]:
            raise FlowError("FLOW_TARGET_MISMATCH", "Host target differs from the reviewed plan")
        verification_blueprint(plan)

    async def construct(self, plan: FlowPlan, *, approved_digest: str, attempt_id: str) -> Document:
        self._check(plan, approved_digest)
        claim = await self.store.claim(plan.digest, attempt_id)
        try:
            finished = await self.store.construction(plan.digest)
            if finished is not None:
                expected = Document(finished.to_dict()["observation"])
                _same(expected, await _observe(self.target, expected))
                return finished
            expected = Document(plan.to_dict()["observed"])
            bindings = {r.logical_id: r.target_id for r in (await self.store.load()).resources}
            for raw_operation in plan.to_dict()["operations"]:
                operation = Document(raw_operation)
                if raw_operation["action"] == "preserve":
                    continue
                # Renew between bounded operations. A target must also enforce
                # this token at its transaction/conditional-write boundary.
                claim = await self.store.claim(plan.digest, attempt_id)
                receipt = await self.store.begin_operation(claim, operation)
                if receipt is None:
                    recovery = await self.target.recover(operation, claim)
                    if recovery.status == "unknown":
                        raise FlowError(
                            "FLOW_OUTCOME_UNKNOWN", "Read back the uncertain operation before retry"
                        )
                    if recovery.status == "found":
                        if recovery.receipt is None:
                            raise FlowError(
                                "FLOW_RECEIPT_MISMATCH", "Recovery did not identify its result"
                            )
                        receipt = recovery.receipt
                    elif recovery.status == "absent" and recovery.receipt is None:
                        _same(expected, await _observe(self.target, expected))
                        await self.store.assert_claim(claim)
                        receipt = await self.target.construct(
                            operation, expected, Document(bindings), claim
                        )
                    else:
                        raise FlowError(
                            "FLOW_RECEIPT_MISMATCH", "Target returned invalid recovery state"
                        )
                    # Validate before persisting a terminal result.
                    _advance(expected, operation, receipt)
                    await self.store.complete_operation(claim, receipt)
                expected = _advance(expected, operation, receipt)
                if receipt.resource is None:
                    bindings.pop(raw_operation["logical_id"], None)
                else:
                    bindings[raw_operation["logical_id"]] = receipt.resource.target_id
            _same(expected, await _observe(self.target, expected))
            current = {r["target_id"]: r for r in expected.to_dict()["resources"]}
            resources: list[OwnedResource] = []
            for operation in plan.to_dict()["operations"]:
                if operation["action"] == "remove":
                    continue
                target_id = bindings[operation["logical_id"]]
                result = current[target_id]
                resources.append(
                    OwnedResource(
                        operation["logical_id"],
                        target_id,
                        operation["kind"],
                        result["revision"],
                        Document(operation["desired"]),
                        tuple(operation["depends_on"]),
                    )
                )
            await self.store.finish_construction(claim, tuple(resources), expected)
            construction = await self.store.construction(plan.digest)
            assert construction is not None
            return construction
        finally:
            await _release(self.store, claim)

    async def verify(self, plan: FlowPlan, *, attempt_id: str) -> Document:
        _require_verification_profile(plan)
        self._check(plan, plan.digest)
        claim = await self.store.claim(plan.digest, attempt_id)
        try:
            construction = await self.store.construction(plan.digest)
            if construction is None:
                raise FlowError(
                    "FLOW_NOT_CONSTRUCTED", "Construct the reviewed plan before verification"
                )
            expected = Document(construction.to_dict()["observation"])
            _same(expected, await _observe(self.target, expected))
            previous = await self.store.evidence(plan.digest, "verification")
            if previous is not None:
                return previous
            blueprint = verification_blueprint(plan)
            native = blueprint.get("schema_version") == "sanka-flow-blueprint/v4"
            scenarios = blueprint.get("scenarios", [])
            required = required_scenarios(blueprint)
            installation = Installation.from_dict(construction.to_dict()["installation"])
            results: list[dict[str, Any]] = []
            for raw_scenario in scenarios:
                scenario = Document(raw_scenario)
                claim = await self.store.claim(plan.digest, attempt_id)
                if native:
                    evaluated = await self._verify_native(blueprint, scenario, installation, claim)
                else:
                    result = await self.target.run_scenario(scenario, installation, claim)
                    evaluated = evaluate_scenario(
                        blueprint=blueprint,
                        scenario=scenario,
                        installation=installation,
                        result=result,
                    )
                results.append(evaluated.to_dict())
                _same(expected, await _observe(self.target, expected))
            passed = {r["scenario_id"] for r in results if r["outcome"] == "passed"}
            evidence = Document(
                {
                    "schema_version": "sanka-flow-verification/v2"
                    if native
                    else "sanka-flow-verification/v1",
                    **(
                        {
                            "profile": "flow.native.order-billing-verification/v1",
                            "verification_blueprint_digest": Document(blueprint).digest,
                        }
                        if native
                        else {}
                    ),
                    "plan_digest": plan.digest,
                    "installation": installation.to_dict(),
                    "observation_digest": expected.digest,
                    "outcome": "passed" if required <= passed else "failed",
                    "results": results,
                }
            )
            await self.store.save_evidence(claim, "verification", evidence)
            return evidence
        finally:
            await _release(self.store, claim)

    async def _verify_native(
        self,
        blueprint: dict[str, Any],
        scenario: Document,
        installation: Installation,
        claim: Claim,
    ) -> Document:
        from sanka.runtime.flow.native_verification import (
            NativeVerificationArtifacts,
            evaluate_native_billing_scenario,
        )

        if not callable(getattr(self.target, "read_verification_artifact", None)):
            raise FlowError(
                "FLOW_VERIFICATION_PROFILE_UNSUPPORTED",
                "Native verification requires a scoped immutable artifact adapter",
            )

        async def execute() -> Document:
            result = await self.target.run_scenario(scenario, installation, claim)
            return await evaluate_native_billing_scenario(
                blueprint=blueprint,
                scenario=scenario,
                installation=installation,
                result=result,
                artifacts=cast(NativeVerificationArtifacts, self.target),
                claim=claim,
                assert_claim=self.store.assert_claim,
            )

        task = asyncio.create_task(execute())
        try:
            while True:
                completed, _ = await asyncio.wait(
                    {task}, timeout=self._verification_renewal_seconds
                )
                await self.store.assert_claim(claim)
                if completed:
                    return await task
                renewed = await self.store.claim(claim.plan_digest, claim.attempt_id)
                if renewed != claim:
                    await _release(self.store, renewed)
                    raise FlowError(
                        "FLOW_ATTEMPT_FENCED", "Native verification cannot resume an expired claim"
                    )
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            elif not task.cancelled():
                # Retrieve a completed exception without replacing a fencing
                # failure raised while checking its result.
                task.exception()

    async def discard(self, plan: FlowPlan, *, attempt_id: str) -> Document:
        """Allow fresh planning only when this attempt provably had no effects.

        This is not rollback. A found or uncertain native result must be recovered
        through construction; it cannot be forgotten by discarding its ledger.
        """
        self._check(plan, plan.digest)
        claim = await self.store.claim(plan.digest, attempt_id)
        try:
            if await self.store.construction(plan.digest) or await self.store.receipts(plan.digest):
                raise FlowError("FLOW_PLAN_HAS_EFFECTS", "Constructed work must be recovered")
            for raw_operation in plan.to_dict()["operations"]:
                if raw_operation["action"] == "preserve":
                    continue
                claim = await self.store.claim(plan.digest, attempt_id)
                recovery = await self.target.recover(Document(raw_operation), claim)
                if recovery.status == "unknown":
                    raise FlowError("FLOW_OUTCOME_UNKNOWN", "An uncertain operation blocks discard")
                if recovery.status != "absent" or recovery.receipt is not None:
                    raise FlowError("FLOW_PLAN_HAS_EFFECTS", "Native results must be recovered")
            await self.store.abandon(claim)
            return Document(
                {
                    "plan_digest": plan.digest,
                    "installation_id": plan.to_dict()["installation"]["id"],
                    "outcome": "discarded",
                }
            )
        finally:
            await _release(self.store, claim)

    async def activate(
        self,
        plan: FlowPlan,
        *,
        approved_verification_digest: str,
        attempt_id: str,
    ) -> Document:
        _require_verification_profile(plan)
        self._check(plan, plan.digest)
        claim = await self.store.claim(plan.digest, attempt_id)
        try:
            construction = await self.store.construction(plan.digest)
            verification = await self.store.evidence(plan.digest, "verification")
            if construction is None or verification is None:
                raise FlowError("FLOW_NOT_VERIFIED", "Activation requires completed verification")
            expected = Document(construction.to_dict()["observation"])
            verified = verification.to_dict()
            if (
                verification.digest != approved_verification_digest
                or verified["outcome"] != "passed"
                or verified["plan_digest"] != plan.digest
                or verified["observation_digest"] != expected.digest
                or verified["installation"] != construction.to_dict()["installation"]
            ):
                raise FlowError(
                    "FLOW_NOT_VERIFIED", "Approval must identify this exact verified revision"
                )
            if plan.to_dict()["blueprint"].get("schema_version") == "sanka-flow-blueprint/v4":
                self._validate_native_verification(plan, verified)
            previous = await self.store.evidence(plan.digest, "activation")
            if previous is not None:
                active = Document(previous.to_dict()["observation"])
                _same(active, await _observe(self.target, active))
                return previous
            intent = Document(
                {
                    "schema_version": "sanka-flow-activation/v1",
                    "plan_digest": plan.digest,
                    "verification_digest": verification.digest,
                    "observation_digest": expected.digest,
                    "installation": construction.to_dict()["installation"],
                    "workflow_target_ids": sorted(
                        r["target_id"]
                        for r in construction.to_dict()["installation"]["resources"]
                        if r["logical_id"]
                        in {
                            resource["id"]
                            for resource in plan.to_dict()["blueprint"]["resources"]
                            if resource["kind"] == "workflow"
                        }
                    ),
                }
            )
            await self.store.save_evidence(claim, "activation_intent", intent)
            recovery = await self.target.recover_activation(intent, claim)
            if recovery.status == "unknown":
                raise FlowError(
                    "FLOW_OUTCOME_UNKNOWN", "Read back the uncertain activation before retry"
                )
            if recovery.status == "found":
                if recovery.observation is None:
                    raise FlowError(
                        "FLOW_RECEIPT_MISMATCH", "Activation recovery is missing readback"
                    )
                active = recovery.observation
            elif recovery.status == "absent" and recovery.observation is None:
                _same(expected, await _observe(self.target, expected))
                await self.store.assert_claim(claim)
                active = await self.target.activate(intent, expected, claim)
            else:
                raise FlowError("FLOW_RECEIPT_MISMATCH", "Invalid activation recovery state")
            self._validate_activation(
                expected, active, set(intent.to_dict()["workflow_target_ids"])
            )
            _same(active, await _observe(self.target, active))
            receipt = Document({"intent_digest": intent.digest, "observation": active.to_dict()})
            await self.store.save_evidence(claim, "activation", receipt)
            return receipt
        finally:
            await _release(self.store, claim)

    @staticmethod
    def _validate_native_verification(plan: FlowPlan, verified: dict[str, Any]) -> None:
        from sanka_extensions.flow import ArtifactIdentity

        blueprint = verification_blueprint(plan)
        required = required_scenarios(blueprint)
        scenarios = {s["id"]: s for s in blueprint["scenarios"]}
        results = verified.get("results")
        profile = "flow.native.order-billing-verification/v1"
        if (
            verified.get("schema_version") != "sanka-flow-verification/v2"
            or verified.get("profile") != profile
            or verified.get("verification_blueprint_digest") != Document(blueprint).digest
            or type(results) is not list
            or len(results) != len(required)
            or any(type(r) is not dict for r in results)
            or any(type(r.get("scenario_id")) is not str for r in results)
            or {r.get("scenario_id") for r in results} != required
        ):
            raise FlowError(
                "FLOW_NOT_VERIFIED", "Activation requires complete native verification evidence"
            )
        for result in results:
            selected = scenarios[result["scenario_id"]]
            native_result = result.get("native_result")
            artifacts = result.get("checked_artifacts")
            try:
                if type(artifacts) is not list or not artifacts:
                    raise ValueError("Missing checked artifacts")
                checked = {ArtifactIdentity.from_dict(a) for a in artifacts}
                if type(native_result) is not dict:
                    raise ValueError("Missing native result")
                deliveries = native_result.get("deliveries")
                if type(deliveries) is not list or any(type(d) is not dict for d in deliveries):
                    raise ValueError("Missing native deliveries")
                required_artifacts = {
                    ArtifactIdentity.from_dict(selected["fixture"]),
                    ArtifactIdentity.from_dict(native_result["initial_readback"]),
                    *(ArtifactIdentity.from_dict(d["readback"]) for d in deliveries),
                }
                if not required_artifacts <= checked:
                    raise ValueError("Native readbacks were not checked")
            except (ValueError, KeyError, TypeError) as error:
                raise FlowError(
                    "FLOW_NOT_VERIFIED", "Native evidence has invalid checked artifacts"
                ) from error
            if (
                result.get("profile") != profile
                or result.get("outcome") != "passed"
                or type(result.get("mismatch_count")) is not int
                or result.get("mismatch_count") != 0
                or result.get("mismatches") != []
                or type(result.get("checked_record_count")) is not int
                or result.get("checked_record_count") != len(selected["record_keys"])
                or native_result.get("scenario_digest") != Document(selected).digest
                or native_result.get("scenario_id") != selected["id"]
                or native_result.get("schema_version") != "sanka-flow-native-billing-result/v1"
                or native_result.get("fixture") != selected["fixture"]
                or native_result.get("mapping") != selected["mapping"]
                or native_result.get("configuration_digest") != selected["configuration_digest"]
                or native_result.get("isolated") is not True
                or native_result.get("outcome") != "passed"
                or native_result.get("installation_id") != verified["installation"]["id"]
                or native_result.get("workflow_target_id")
                != next(
                    r["target_id"]
                    for r in verified["installation"]["resources"]
                    if r["logical_id"] == selected["workflow_id"]
                )
                or [d.get("id") for d in deliveries] != [d["id"] for d in selected["deliveries"]]
            ):
                raise FlowError(
                    "FLOW_NOT_VERIFIED",
                    "Native evidence is incomplete or belongs to another scenario",
                )

    @staticmethod
    def _validate_activation(expected: Document, active: Document, workflow_ids: set[str]) -> None:
        before, after = expected.to_dict(), active.to_dict()
        old = {r["target_id"]: r for r in before.pop("resources")}
        new = {r["target_id"]: r for r in after.pop("resources")}
        before.pop("revision")
        after.pop("revision")
        if Document(before) != Document(after) or old.keys() != new.keys():
            raise FlowError(
                "FLOW_ACTIVATION_MISMATCH", "Activation changed target scope or references"
            )
        for target_id in old:
            left, right = dict(old[target_id]), dict(new[target_id])
            if target_id in workflow_ids:
                if right["active"] is not True:
                    raise FlowError("FLOW_ACTIVATION_MISMATCH", "Workflow was not activated")
                if not isinstance(right["revision"], str) or not right["revision"]:
                    raise FlowError("FLOW_ACTIVATION_MISMATCH", "Activated revision is missing")
                left.pop("active"), right.pop("active")
                left.pop("revision"), right.pop("revision")
            if Document(left) != Document(right):
                raise FlowError(
                    "FLOW_ACTIVATION_MISMATCH", "Activation changed unverified configuration"
                )
