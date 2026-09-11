# SPDX-License-Identifier: AGPL-3.0-only
"""Execution working-state model: routes, results, snapshot, scope, attempt.

The dataclasses here carry the working state of one run's transfer — the
mechanics the persisted transfer report holds (per-route checkpoints and
counters, pending relationships and record ids, failed-id sets, batch
pages) — plus the frozen scope a continuous job is bound to and the attempt
identity that fences it. Shapes are faithful to the production execution
state; the camelCase report spellings remain the persisted contract, owned
by the report codec that round-trips them byte-identically.

Vocabulary note: :data:`WriteStatus` is the durable per-record result
vocabulary (``failed`` included), matching the SDK's ``BatchWriteStatus``;
:data:`ExecutionStatus` is the production transfer-stage vocabulary, a
sibling of (not a replacement for) the lifecycle ``RunStatus`` in
:mod:`sanka.runtime.state`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from sanka.runtime.hashing import content_hash
from sanka.runtime.mapping.model import MigrationMappingField
from sanka.runtime.mapping.pending_relationships import PendingRelationshipsByRoute
from sanka_extensions.systems import SourceFilter

ExecutionStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
WriteStatus = Literal["created", "updated", "skipped", "failed"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionRoute:
    """One executable route: the unit checkpoints and counters key on."""

    route_key: str  # mapping_group_key(source, destination, filter)
    source_object: str
    destination_object: str
    source_filter: SourceFilter | None
    fields: list[MigrationMappingField]
    source_identity_field: str | None = None
    """Source field carrying the record identity the ledger keys on.

    ``None`` keeps the production convention (the raw ``Id``/``id`` record
    keys). The open planner's routes name an explicit identity field per
    source schema (``RoutePlan.identity_field``), which rides here so batch
    execution keys records exactly as the reviewed plan does.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordWriteOutcome:
    """One durable per-record result row (pair-keyed, not route-keyed)."""

    source_object: str
    source_record_id: str
    destination_object: str
    destination_record_id: str | None
    status: WriteStatus
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PairStatusTotal:
    source_object: str
    destination_object: str
    status: str
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PairFailedIds:
    source_object: str
    destination_object: str
    source_record_ids: frozenset[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class BatchPage:
    """The last source page seen per route — reconciliation evidence."""

    source_record_ids: tuple[str, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(kw_only=True)
class ExecutionSnapshot:
    """The mutable working state of one run's transfer.

    Field-for-field the persisted transfer-report mechanics keys
    (``checkpoints``, ``completedRoutes``, ``routeCounts``,
    ``routePendingRecordIds``, ``routePendingRelationships``,
    ``routeFailedRecordIds``, ``batchPages``, ``hasMore``) — the report
    codec round-trips these spellings byte-identically.
    """

    checkpoints: dict[str, str] = field(default_factory=dict)
    completed_routes: set[str] = field(default_factory=set)
    route_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    route_pending_record_ids: dict[str, set[str]] = field(default_factory=dict)
    route_pending_relationships: PendingRelationshipsByRoute = field(default_factory=dict)
    route_failed_record_ids: dict[str, set[str]] = field(default_factory=dict)
    batch_pages: dict[str, BatchPage] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    has_more: bool = False

    def progress_marker(self) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
        """Stall detector: (sorted checkpoints, sorted completed routes)."""
        return tuple(sorted(self.checkpoints.items())), tuple(sorted(self.completed_routes))


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionScope:
    """The approved, frozen scope one continuous job is bound to."""

    route_manifest: tuple[Mapping[str, Any], ...]  # canonical, sorted rows
    selected_route_keys: tuple[str, ...]
    route_high_water_marks: Mapping[str, str | None]
    route_totals: Mapping[str, int | None]

    @property
    def scope_hash(self) -> str:
        """sha256 over the canonical scope — the candidate-set hash approvals bind to."""
        return content_hash(
            {
                "routeManifest": list(self.route_manifest),
                "selectedRouteKeys": list(self.selected_route_keys),
                "routeHighWaterMarks": dict(self.route_high_water_marks),
            }
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptIdentity:
    attempt_id: str  # f"{task_run_id}:{attempt_number}"
    attempt_number: int
    task_run_id: str


@dataclass(kw_only=True)
class JournalEntry:
    """Everything the journal persists atomically for one run's transfer."""

    status: ExecutionStatus
    snapshot: ExecutionSnapshot
    job_id: str | None  # None = manual single-batch execution
    attempt_id: str | None
    batch_size: int
    batches_completed: int
    started_at: str | None  # ISO-8601 UTC
    last_heartbeat_at: str | None
    resumed: bool
    scope: ExecutionScope | None
    durable_results_attempt_id: str | None
    aggregate_counts: dict[str, int] = field(default_factory=dict)
    route_progress: list[dict[str, Any]] = field(default_factory=list)
    provider_control: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)  # host envelope keys, round-tripped
