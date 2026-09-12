# SPDX-License-Identifier: AGPL-3.0-only
"""Installation-bound persistence port; hosts cannot switch scope mid-operation."""

from __future__ import annotations

from typing import Protocol

from sanka.runtime.flow.model import (
    Claim,
    Document,
    FlowPlan,
    Installation,
    OperationReceipt,
    OwnedResource,
)


class InstallationStore(Protocol):
    async def load(self) -> Installation: ...

    async def save_plan(self, plan: FlowPlan) -> None: ...

    async def claim(self, plan_digest: str, attempt_id: str) -> Claim: ...

    async def assert_claim(self, claim: Claim) -> None: ...

    async def begin_operation(self, claim: Claim, operation: Document) -> OperationReceipt | None:
        """Durably record intent before mutation; reject changed request bytes."""
        ...

    async def complete_operation(self, claim: Claim, receipt: OperationReceipt) -> None: ...

    async def receipts(self, plan_digest: str) -> tuple[OperationReceipt, ...]: ...

    async def construction(self, plan_digest: str) -> Document | None: ...

    async def finish_construction(
        self, claim: Claim, resources: tuple[OwnedResource, ...], observation: Document
    ) -> Installation: ...

    async def save_evidence(self, claim: Claim, kind: str, evidence: Document) -> None:
        """Compare-and-set immutable verification/activation evidence."""
        ...

    async def evidence(self, plan_digest: str, kind: str) -> Document | None: ...

    async def abandon(self, claim: Claim) -> None:
        """Retire a plan only after fenced native recovery proved zero effects.

        Reject constructed plans or recorded results. The retired digest cannot
        be claimed again, and retirement must release its installation selection.
        """
        ...

    async def release(self, claim: Claim) -> None: ...
