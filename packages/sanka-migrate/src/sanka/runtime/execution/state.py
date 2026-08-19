# SPDX-License-Identifier: AGPL-3.0-only
"""Execution state protocols: results ledger, fenced journal, attempt fence.

A sibling of :class:`sanka.runtime.state.StateStore`, not an extension. The
lifecycle store persists a run's reviewed documents (spec, inspection, plan)
behind a synchronous protocol; the family here persists the *working state*
of transfer — durable per-record results, the fenced journal entry, and the
attempt claim — and is async end-to-end, matching the production host. The
family is deliberately split three ways: a local runtime should not have to
implement queue-shaped claim semantics just to run a batch, and the ledger
is consulted from both the batch executor and the continuous reconciler
while the fence is not.

Every protocol is scope-free: no workspace, run, program, or channel id
appears in any signature. The host constructs one adapter instance per
execution and closes it over its pinned scope, exactly as the mapping
identity ledgers (:mod:`sanka.runtime.mapping.pending_relationships`)
already prescribe — upstream code has no vocabulary for switching scope.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from sanka.runtime.execution.model import (
    AttemptIdentity,
    JournalEntry,
    PairFailedIds,
    PairStatusTotal,
    RecordWriteOutcome,
)
from sanka.runtime.mapping.pending_relationships import IdentityLedger, SharedIdentityLedger

SaveOutcome = Literal["saved", "superseded", "cancelled"]
ClaimOutcome = Literal["claimed", "superseded", "cancelled"]


@runtime_checkable
class ExecutionLedger(Protocol):
    """Durable per-record results for one run. Host closes over run scope.

    Keying is (source_object, destination_object, source_record_id) — the
    production dedupe/idempotency unit: two filtered routes over one object
    pair share idempotency, so terminal lookups are pair-scoped. Route-level
    counts are derived state, rebuilt only for routes unique to their pair.
    """

    async def terminal_destination_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str | None]: ...

    async def upsert_results(self, results: Sequence[RecordWriteOutcome]) -> int:
        """Persist idempotently (upsert by key); return the number persisted.

        The engine fails closed (``SANKA_MIGRATE_RECORD_RESULT_BULK_SAVE_INCOMPLETE``)
        when the returned count differs from ``len(results)``.
        """
        ...

    async def status_totals(self) -> dict[str, int]: ...

    async def pair_status_totals(self) -> list[PairStatusTotal]: ...

    async def failed_source_ids_by_pair(self) -> list[PairFailedIds]: ...


@runtime_checkable
class ExecutionJournal(Protocol):
    """Fenced load/save of the JournalEntry for one run (+ job, when queued).

    ``save`` returns ``"superseded"`` when another job or attempt owns the
    state now, and ``"cancelled"`` when a cancellation landed first — in
    which case the implementation has already finalized the cancelled
    terminal state. ``load`` raises
    :class:`~sanka.runtime.execution.errors.ExecutionFault` with code
    ``SANKA_MIGRATE_EXECUTION_JOB_SUPERSEDED`` when the journal's job is no longer
    the owner; a cancelled entry loads normally with status ``"cancelled"``.
    """

    async def load(self) -> JournalEntry | None: ...

    async def save(self, entry: JournalEntry) -> SaveOutcome: ...


@runtime_checkable
class AttemptFence(Protocol):
    """Compare-and-set claim of an execution attempt for the journal's job.

    Production claims ``(job_id, attempt_id, attempt_number, task_run_id)``
    on the transfer state so an older queue attempt can never resume over a
    newer one. A single-process local runtime trivially claims and returns
    the current entry.
    """

    async def claim(self, attempt: AttemptIdentity) -> tuple[ClaimOutcome, JournalEntry | None]: ...


class ExecutionObserver(Protocol):
    """Side-channel for logs/metrics. Sync, fire-and-forget, no return values.

    Hosts map these onto their structured execution events, closing over
    whatever context ids they log. The default is :data:`NULL_OBSERVER`.
    """

    def record_failed(
        self, *, route_key: str, source_object: str, source_record_id: str
    ) -> None: ...

    def batch_completed(self, *, batches_completed: int, has_more: bool, stalled: bool) -> None: ...

    def route_count_failed(self, *, route_key: str) -> None: ...

    def attempt_fenced(self, *, attempt_id: str) -> None: ...


class _NullExecutionObserver:
    """No-op observer: the default when the host wires no side-channel."""

    def record_failed(self, *, route_key: str, source_object: str, source_record_id: str) -> None:
        return None

    def batch_completed(self, *, batches_completed: int, has_more: bool, stalled: bool) -> None:
        return None

    def route_count_failed(self, *, route_key: str) -> None:
        return None

    def attempt_fenced(self, *, attempt_id: str) -> None:
        return None


NULL_OBSERVER: ExecutionObserver = _NullExecutionObserver()
"""Module-level no-op observer default."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionHost:
    """Everything host-specific one execution needs, bundled once."""

    ledger: ExecutionLedger
    journal: ExecutionJournal
    fence: AttemptFence
    identity_ledger: IdentityLedger  # sanka.runtime.mapping.pending_relationships
    shared_identity_ledger: SharedIdentityLedger | None
    observer: ExecutionObserver = NULL_OBSERVER
