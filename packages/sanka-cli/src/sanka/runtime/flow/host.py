# SPDX-License-Identifier: AGPL-3.0-only
"""Target service and scenario ports for Flow; never an automation engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from sanka.runtime.flow.model import (
    Claim,
    Document,
    Installation,
    OperationReceipt,
    TargetSnapshot,
)


@dataclass(frozen=True, slots=True)
class Recovery:
    status: Literal["absent", "found", "unknown"]
    receipt: OperationReceipt | None = None


@dataclass(frozen=True, slots=True)
class ActivationRecovery:
    status: Literal["absent", "found", "unknown"]
    observation: Document | None = None


class FlowTarget(Protocol):
    """A host adapter closed over one authenticated target and installation.

    Every mutation must atomically enforce its claim and observed preconditions,
    and durably deduplicate the operation before returning. A read immediately
    before an unconditional remote write is insufficient. Recovery returns absent
    only when an older attempt cannot subsequently commit that operation; unknown
    leaves the plan interrupted. Workflow construction must remain inactive.
    """

    @property
    def target(self) -> str: ...

    async def observe(self, target_ids: tuple[str, ...]) -> TargetSnapshot: ...

    async def recover(self, operation: Document, claim: Claim) -> Recovery: ...

    async def construct(
        self,
        operation: Document,
        expected: Document,
        bindings: Document,
        claim: Claim,
    ) -> OperationReceipt: ...

    async def run_scenario(
        self,
        scenario: Document,
        installation: Installation,
        claim: Claim,
    ) -> Document:
        """Exercise the target's native semantics with isolated records/adapters.

        Return scenario_id, scenario_digest, installation_id, workflow_target_id,
        outcome (passed|failed|skipped), isolated=true, completed_event_digests in
        delivery order, event_record_bindings mapping original scenario record IDs
        to actual isolated native record IDs, and created_records containing id,
        object_ref, fields and associations. Enumerate all native records created across deliveries,
        including retries; associations map reference IDs to actual record IDs.
        The shared runtime compares actual readback with expected counts/values.
        No live messages, payments or provider writes are allowed. This port must
        use native execution rather than a parallel business-action executor.
        """
        ...

    async def recover_activation(self, intent: Document, claim: Claim) -> ActivationRecovery: ...

    async def activate(self, intent: Document, expected: Document, claim: Claim) -> Document:
        """Atomically activate only the observed and verified workflow revisions."""
        ...
