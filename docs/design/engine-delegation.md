# Engine delegation: one execution runtime

Phase 3 PR C design — sanka-api's record-runtime execution mechanics delegate
to `ferry.runtime`, so execution semantics live once, in this repository.

- **Status**: design, not implemented. No production code changes ride with
  this document.
- **Anchors**: every `file:line` reference below was verified against
  sanka-api `origin/main` @ `19afb0c4b` (post #2807/#2811/#2813/#2814) and
  ferry `main` @ `32db881`. Line numbers drift with unrelated commits; the
  symbol names do not.
- **Prior art this design extends**: #2813 made the OSS mapping stack
  (`ferry.runtime.mapping`) the single source of mapping semantics, with
  sanka-api keeping thin delegation shims and an error-shape bridge
  (`app/service/ferry/oss_bridge.py`). #2814 put the OSS connector SPI
  between the engine and the production adapters
  (`app/service/ferry/sdk_adapters.py`). This document designs the same move
  for the execution mechanics themselves.

## 1. Where the two engines stand

**sanka-api** (`app/service/ferry/record_runtime.py`, 3,437 lines, plus the
pure helpers in `app/service/ferry/execution_state.py`, 558 lines) runs
production migrations. Its `FerryRecordRuntime` already speaks the OSS
connector SPI (`ferry.connector` imports at `record_runtime.py:27-39`) and
already keeps control-plane concerns behind injected protocols
(`FerryAccessGate` at `record_runtime.py:155-164`,
`FerryProgramExecutionEntitlementGate` at `:167-175`,
`FerryInventoryScanCompletionHook` at `:178-187`). What it has that the OSS
engine lacks is the production execution machinery:

- continuous execution with per-route keyset checkpoints and frozen
  high-water marks (`execute` at `record_runtime.py:1355-2171`,
  `execute_continuously` at `:2455-2747`);
- Hatchet attempt claiming with `attempt_id = f"{task_run_id}:{attempt_number}"`
  (`app/jobs/ferry/execution.py:73-78`, claim at
  `record_runtime.py:2490-2518`);
- durable per-record result rows behind `FerryRuntimeRepository`
  (`record_runtime.py:190-393`, backed by `data_ferryrecordresult` with
  workspace/run scoping);
- terminal-batch reconciliation and route reopening when a source page ends
  before the frozen total (`record_runtime.py:2873-2966`,
  `execution_state.py:384-459`);
- heartbeat progress reports with per-route ETA
  (`record_runtime.py:3019-3212`);
- deferred relationship linking with cross-run identity resolution (already
  delegated to `ferry.runtime.mapping.pending_relationships` behind
  `IdentityLedger` / `SharedIdentityLedger`).

**ferry** (`packages/ferry-migrate/src/ferry/runtime/engine.py`, 507 lines)
runs the local single-shot lifecycle: `create → inspect → plan → apply →
verify` over a sync `StateStore` protocol (`state.py:69-95`) with a SQLite
reference implementation (`state.py:135-263`). `apply` is hash-bound
(`engine.py:191-194`), page-checkpointed (`engine.py:271-305`), and
idempotent through the identity ledger (`engine.py:274`, `:298-301`), but it
is single-attempt, has no relationship/reference/owner phases, no frozen
scope, no reconciliation against route totals, and no fencing.

**Direction of travel** (binding): production mechanics move upstream into
`ferry.runtime` as protocol-parameterized components; sanka-api then
delegates. The OSS engine must not grow Sanka concepts — workspace, program,
Hatchet, billing — those stay behind protocols the host implements. The
precedent is already in-tree: `ferry.runtime.mapping.pending_relationships`
keeps workspace/run/program/channel scoping out of the runtime ("callers
pass an `IdentityLedger` … that close[s] over whatever scope the host
runtime uses", `pending_relationships.py:15-21`), and sanka-api's
`app/service/ferry/pending_relationships.py` closes its adapters over the
pinned ids. Every protocol in this design follows that rule.

## 2. Inventory of record-runtime execution mechanics

Classification: **(a)** generic — moves upstream as-is (parameterized only by
connectors, credentials, and plain data); **(b)** generic-but-parameterizable —
moves upstream behind a new protocol; **(c)** Sanka control plane — stays in
sanka-api.

| # | Mechanic | What it does | Anchors (sanka-api) | Sanka couplings today | Class |
|---|---|---|---|---|---|
| 1 | Route-page write phase | Read one source page (bounded or plain), skip terminal records, map to destination properties, single/batch write, collect per-record states | `record_runtime.py:1470-1773` | repository (terminal ids), pydantic request models, `AppError` | (a) |
| 2 | Reference (scalar FK) resolution | Collect referenced source ids per pair, resolve through the identity ledger, block on pending parents | `record_runtime.py:1600-1625`, `:1657-1707` | repository via `resolve_destination_record_ids` (already ledger-protocol'd upstream) | (a) |
| 3 | Relationship phase + pending parking | Build relationship writes, batch or single dispatch, park failures as pending dicts keyed by `pending_relationship_key` | `record_runtime.py:1775-1904` | none beyond the ledgers (formats already owned upstream) | (a) |
| 4 | Concurrent-attempt failed→skipped repair | After a failed write, re-read terminal ids; convert to `skipped` when an active attempt already committed the record | `record_runtime.py:1933-1956` | repository terminal-id read | (a) |
| 5 | Route counters, failed-id dedupe, cursor advance | `created/updated/skipped` counted once per record, `failed` tracked as a deduped id set, checkpoint advances only past terminal records | `record_runtime.py:1957-2023` | none (pure over page state) | (a) |
| 6 | Durable pair-result rebuild | Rebuild route counts and failed-id sets from durable result rows, restricted to routes that are unique for their (source, destination) pair | `record_runtime.py:2968-3017`, trigger `:1445-1464` | repository summaries | (a) |
| 7 | Terminal-batch page reconciliation | When a batch stalls, prove every page record is terminal, then advance/complete checkpoints from durable state | `record_runtime.py:2873-2966` | repository terminal-id + summary reads | (a) |
| 8 | Continuous batch loop | Fenced loop: per-batch supersede/cancel checks, stall detection via progress marker, batch safety limit (1,000,000, `:152`), 0.25 s pause | `record_runtime.py:2566-2747` | stage reads (fencing), Hatchet ids | (a) loop / (b) fencing |
| 9 | Route reopening on frozen-total shortfall | `Σ(created,updated,skipped) < frozen total` reopens the route from the last safe keyset checkpoint; refuses when no checkpoint exists | `execution_state.py:384-419`, `:422-459`; call sites `record_runtime.py:2624-2644` | none (pure) | (a) |
| 10 | Progress / ETA / heartbeat report assembly | Per-route processed/remaining/percent/ETA rows, overall progress, stall warnings, `lastHeartbeatAt` | `record_runtime.py:3019-3212` | repository pair summaries for unique-pair fallback (`:3064-3082`); `retry_metrics` provider control (`:3160-3167`) | (a) |
| 11 | Scope freeze (high-water marks + frozen totals) | Freeze per-route HWMs at queue time; count the frozen set with bounded counts | `record_runtime.py:2846-2871`, `:2770-2844` | SDK capability refinements (now upstream: `ferry.connector.protocols` `SupportsHighWaterMark:158`, `SupportsBoundedReads:171`, `SupportsBoundedCounts:188`) | (a) |
| 12 | Snapshot codec + manifest canonicalization | Normalize checkpoints/counts/pending/failed/batch-page dicts, canonicalize + verify route manifests, resolve route selection, remap legacy source-keyed checkpoints, validate saved HWM maps | `execution_state.py:69-160`, `:223-305`, `:308-381`, `:462-558` | pydantic `FerrySourceFilter`, `AppError` | (a) |
| 13 | Dry-run route sampling | Per route: read a sample, map, collect capped rejects with deduped reasons; never touches a destination adapter | `record_runtime.py:1174-1273` (core), `execution_state.py:26-66` (reject shaping) | stage-report envelope (stays) | (a) |
| 14 | Owner-directory acquisition | Lazily build destination/source owner directories once per execution via `SupportsOwnerDirectory` | `record_runtime.py:1468-1485` | none (mapping itself already upstream) | (a) |
| 15 | Heartbeat freshness test | "Active job" = heartbeat within 5 minutes | `execution_state.py:491-498` | none (pure) | (a) |
| 16 | Attempt claiming | Compare-and-set claim of `(job_id, attempt_id, attempt_number, task_run_id)` on the transfer stage | `record_runtime.py:2490-2518`; repository `:256-266` | Hatchet identity, stage rows | (b) → `AttemptFence` |
| 17 | Fenced snapshot persistence + cancelled race | Job-fenced stage save; on fence loss, distinguish cancelled (finalize with cancelled report) from superseded | `record_runtime.py:2099-2153`, `:2665-2690`; repository `:245-254`; concrete `app/repository/ferry/repository.py:1344` | stage rows, report envelope | (b) → `ExecutionJournal` |
| 18 | Durable record results | Terminal-id page reads, count-verified bulk upsert, aggregate and per-pair summaries, failed-id listing | repository protocol `record_runtime.py:303-384`; fail-closed check `:1918-1932` | `data_ferryrecordresult`, workspace/run scoping | (b) → `ExecutionLedger` |
| 19 | Cross-run identity resolution | Run-scoped + program-shared destination-id lookup with ambiguity blocking | `app/service/ferry/pending_relationships.py` (adapter); upstream `ferry/runtime/mapping/pending_relationships.py:43-64` | **already upstream** — the pattern this design copies | (b) done |
| 20 | Structured execution events | `ferry.transfer.record_failed`, queue/complete/fail logs with workspace/run context | `record_runtime.py:1963-1972`, `:2361-2368`, `:2835-2842` | `app.core.logging`, ctx ids | (b) → `ExecutionObserver` |
| 21 | Cancellation observation | Stage/execution-state cancelled test, polled between batches | `execution_state.py:462-472`; polls `record_runtime.py:2583-2584` | stage rows | (b) → journal `load()` status |
| 22 | Access gate | Feature flag + product line + platform/object permission checks | protocol `record_runtime.py:155-164`; impl `runtime_service.py:596-626` | flags, permissions | (c) |
| 23 | Execution entitlement | Billing gate (402) before destination writes | protocol `record_runtime.py:167-175`; impl `runtime_service.py:510-550` | plans/billing | (c) |
| 24 | Scan-pricing hook | Post-scan program pricing side-effect | protocol `record_runtime.py:178-187`; impl `runtime_service.py:552-594` | programs, pricing | (c) |
| 25 | Confirm gate + effective options | `confirm` required before writes; merge saved run execution options (owner/email policies) | `record_runtime.py:1368-1389`; `execution_state.py:163-220` | run rows (`runtime_config`) | (c) |
| 26 | Scan orchestration | Queued/running/failed inventory-stage lifecycle, supersede checks, Hatchet submission | `record_runtime.py:803-1150` | schedulers, stage rows | (c) (schema-scan core may later fold into `engine.inspect`, `engine.py:127-160`) |
| 27 | Continuous start/cancel endpoints | Resume dedupe via heartbeat, HWM freeze trigger, queue + scheduler submit, cancel with in-flight-batch preservation | `record_runtime.py:2173-2374`, `:2376-2453` | schedulers, auth, run status | (c) |
| 28 | Destination property/resource provisioning + persistence | Reconcile properties/resources, persist results into the inventory stage report; HubSpot schema-admin credential vetting (portal pinning, scope checks) | `record_runtime.py:485-801` | credentials manager, `HUBSPOT_SCHEMA_ADMIN_REQUIRED_SCOPES`, stage reports | (c) |
| 29 | Run binding, status transitions, serialization | Channel-bound run requirement; `running`/`needs_review` transitions; run/stage DTO serialization | `record_runtime.py:396-411`, `:2162-2166`, `:3421-3437` | run/stage rows, API DTOs | (c) |
| 30 | Route-manifest stage persistence | Load mapping fields from the map stage, persist the canonical manifest back, refuse reconstruction after durable progress | `record_runtime.py:3266-3332`, `:3334-3376` | stage rows (the *checks* it calls are class (a), item 12) | (c) |
| 31 | Hatchet job wrapper | Task entry, attempt identity derivation, failure marking on final attempt | `app/jobs/ferry/execution.py` | Hatchet | (c) |
| 32 | Credentials + adapter wiring | Channel-credential resolution, provider-pair support checks, SPI bridging | `record_runtime.py:414-436`, `:3391-3409`; `sdk_adapters.py` | channels, integrations | (c) |
| 33 | Data-platform runtime | Postgres→ClickHouse ClickPipes replication | `app/service/ferry/data_platform_runtime.py` | ClickPipes, preflight | (c) permanently (see §7) |

Tally: **15 × (a)**, **6 × (b)** (one already done), **12 × (c)**.

## 3. The seam: `ferry.runtime.execution`

### 3.1 Design rule

Upstream protocols are **scope-free**. No protocol below accepts a
`workspace_id`, `run_id`, `program_id`, or channel id — the host constructs
one adapter instance per execution and closes it over the pinned ids, exactly
as `pending_relationships.py:15-21` already prescribes and as sanka-api's
`resolve_destination_record_ids` adapter already does. This is not just
tidiness: it makes the internal workspace-safety invariant ("one migration
execution belongs to exactly one internal workspace UUID" — the sanka
workspace runbook `docs/operations/customer-migration-workspace-safety.md`)
structurally unbreakable inside upstream code, because upstream code has no
vocabulary for switching scope.

### 3.2 Relation to `StateStore`: sibling, not extension

Recommendation: a **sibling protocol family** in a new
`ferry.runtime.execution` package, not an extension of
`ferry.runtime.state.StateStore`.

Justification:

1. **Different jobs.** `StateStore` (`state.py:69-95`) persists the run
   *lifecycle documents* — spec, inspection, plan (with hash), status. The
   execution family persists the *working state* of transfer — checkpoints,
   counters, pending relationships, attempt fences, per-record results.
   sanka-api has no spec/plan documents in its data model (its manifest
   lives in the map-stage report), so extending `StateStore` would force
   sanka to fake spec persistence it does not have.
2. **Sync vs async.** `StateStore` is synchronous by contract and its SQLite
   implementation is synchronous. Production execution state is async
   end-to-end (`FerryRuntimeRepository` is all `async def`,
   `record_runtime.py:190-393`). Growing `StateStore` with ~10 async methods
   would break every existing implementer and split the protocol's calling
   convention.
3. **One engine, two hosts.** With siblings, the SQLite reference store
   implements *both* families over the same file (lifecycle rows stay as
   they are; execution rows are new tables), and sanka-api implements only
   the execution family over `FerryRuntimeRepository`. `MigrationEngine`
   keeps `StateStore` for lifecycle and adopts the execution family for
   `apply` (PR F-5 in §6).

Shared vocabulary stays shared: `TERMINAL_WRITE_STATUSES`
(`state.py:30`) is imported by the execution package, not duplicated.

### 3.3 Errors

Same pattern as `ferry.runtime.mapping.errors.MappingError`: production
error codes verbatim, no HTTP envelope; sanka-api's `oss_bridge` maps codes
to the exact `AppError` status codes (extending the existing
`STATUS_BY_MAPPING_ERROR_CODE` table pattern, `oss_bridge.py:46-74`).

```python
# ferry/runtime/execution/errors.py  (AGPL-3.0-only)
class ExecutionFault(RuntimeError):
    """An execution invariant failed; carries the production error code."""

    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = dict(details) if details else {}
```

Codes preserved verbatim (409/422 mapping stays host-side):
`FERRY_EXECUTION_JOB_SUPERSEDED`, `FERRY_EXECUTION_ATTEMPT_SUPERSEDED`,
`FERRY_ROUTE_MANIFEST_MISSING`, `FERRY_ROUTE_MANIFEST_INVALID`,
`FERRY_ROUTE_MANIFEST_CHANGED`, `FERRY_ROUTE_CHECKPOINT_MISMATCH`,
`FERRY_EXECUTION_HIGH_WATER_MARK_INVALID`, `FERRY_SOURCE_CHECKPOINT_MISSING`,
`FERRY_SOURCE_HIGH_WATER_MARK_UNSUPPORTED`,
`FERRY_EXECUTION_ROUTE_INVALID`, `FERRY_EXECUTION_ROUTE_CHANGED`,
`FERRY_REQUIRED_REFERENCE_SOURCE_FIELD_EMPTY`,
`FERRY_REFERENCE_SOURCE_ID_AMBIGUOUS`, `FERRY_REFERENCE_TARGET_PENDING`,
`FERRY_EMPTY_DESTINATION_RECORD`, `FERRY_OWNER_MAPPING_UNSUPPORTED`,
`FERRY_RECORD_RESULT_BULK_SAVE_INCOMPLETE`.

### 3.4 The working-state model

```python
# ferry/runtime/execution/model.py  (AGPL-3.0-only)
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from ferry.connector import SourceFilter
from ferry.runtime.hashing import content_hash
from ferry.runtime.mapping.model import MigrationMappingField
from ferry.runtime.mapping.pending_relationships import PendingRelationshipsByRoute

ExecutionStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
WriteStatus = Literal["created", "updated", "skipped", "failed"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionRoute:
    """One executable route: the unit checkpoints and counters key on."""

    route_key: str                      # mapping_group_key(source, destination, filter)
    source_object: str
    destination_object: str
    source_filter: SourceFilter | None
    fields: list[MigrationMappingField]


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
    (`checkpoints`, `completedRoutes`, `routeCounts`, `routePendingRecordIds`,
    `routePendingRelationships`, `routeFailedRecordIds`, `batchPages`,
    `hasMore`) — the codec in report_codec.py round-trips these spellings
    byte-identically.
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
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionScope:
    """The approved, frozen scope one continuous job is bound to."""

    route_manifest: tuple[Mapping[str, Any], ...]   # canonical, sorted rows
    selected_route_keys: tuple[str, ...]
    route_high_water_marks: Mapping[str, str | None]
    route_totals: Mapping[str, int | None]

    @property
    def scope_hash(self) -> str:
        """sha256 over the canonical scope — the runbook's candidate-set hash."""
        return content_hash({
            "routeManifest": list(self.route_manifest),
            "selectedRouteKeys": list(self.selected_route_keys),
            "routeHighWaterMarks": dict(self.route_high_water_marks),
        })


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptIdentity:
    attempt_id: str          # f"{task_run_id}:{attempt_number}"
    attempt_number: int
    task_run_id: str


@dataclass(kw_only=True)
class JournalEntry:
    """Everything the journal persists atomically for one run's transfer."""

    status: ExecutionStatus
    snapshot: ExecutionSnapshot
    job_id: str | None                  # None = manual single-batch execution
    attempt_id: str | None
    batch_size: int
    batches_completed: int
    started_at: str | None              # ISO-8601 UTC
    last_heartbeat_at: str | None
    resumed: bool
    scope: ExecutionScope | None
    durable_results_attempt_id: str | None
    aggregate_counts: dict[str, int] = field(default_factory=dict)
    route_progress: list[dict[str, Any]] = field(default_factory=list)
    provider_control: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)   # host envelope keys, round-tripped
```

Notes:

- `ExecutionSnapshot` ↔ report-payload conversion absorbs the normalizers at
  `execution_state.py:501-558` (checkpoints, route counts, pending ids,
  failed ids, progress marker) and `:308-381` (`_prepare_route_state`
  legacy-key remapping) into `report_codec.py`. The camelCase spellings are
  already an upstream responsibility for pending relationships
  (`pending_relationships.py:9-14`); this extends the same ownership to the
  rest of the mechanics keys.
- `extras` is how the host's envelope keys (`summary`, `generatedAt`,
  `sourceProvider`, policy echoes, …) survive a load→save round trip without
  the upstream code knowing them. Ownership of copy strings stays host-side
  (§8 Q1).

### 3.5 The protocol family

```python
# ferry/runtime/execution/state.py  (AGPL-3.0-only)
from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

SaveOutcome = Literal["saved", "superseded", "cancelled"]
ClaimOutcome = Literal["claimed", "superseded", "cancelled"]


@runtime_checkable
class ExecutionLedger(Protocol):
    """Durable per-record results for one run. Host closes over run scope.

    Keying is (source_object, destination_object, source_record_id) — the
    production dedupe/idempotency unit (`get_terminal_destination_record_ids`
    is pair-scoped, sanka-api record_runtime.py:312-320). Route-level counts
    are derived state, rebuilt only for routes unique to their pair.
    """

    async def terminal_destination_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str | None]: ...

    async def upsert_results(self, results: Sequence[RecordWriteOutcome]) -> int:
        """Returns the number persisted. The engine fails closed
        (FERRY_RECORD_RESULT_BULK_SAVE_INCOMPLETE) when it differs from
        len(results) — preserving record_runtime.py:1918-1932."""
        ...

    async def status_totals(self) -> dict[str, int]: ...

    async def pair_status_totals(self) -> list[PairStatusTotal]: ...

    async def failed_source_ids_by_pair(self) -> list[PairFailedIds]: ...


@runtime_checkable
class ExecutionJournal(Protocol):
    """Fenced load/save of the JournalEntry for one run (+ job, when queued).

    `save` returns "superseded" when another job or attempt owns the state
    now, and "cancelled" when a cancellation landed first — in which case the
    implementation has already finalized the cancelled terminal state
    (production: update_transfer_stage_for_job returning None, then the
    cancelled-race path record_runtime.py:2108-2153 /
    finalize_cancelled_transfer_execution,
    app/repository/ferry/repository.py:1344).
    `load` raises ExecutionFault(FERRY_EXECUTION_JOB_SUPERSEDED) when the
    journal's job is no longer the owner; a cancelled entry loads normally
    with status "cancelled".
    """

    async def load(self) -> JournalEntry | None: ...

    async def save(self, entry: JournalEntry) -> SaveOutcome: ...


@runtime_checkable
class AttemptFence(Protocol):
    """Compare-and-set claim of an execution attempt for the journal's job.

    Production: claim_transfer_execution_attempt
    (record_runtime.py:256-266, used at :2490-2518). The local runtime is
    single-process; its fence trivially claims and returns the current entry.
    """

    async def claim(self, attempt: AttemptIdentity) -> tuple[ClaimOutcome, JournalEntry | None]: ...


class ExecutionObserver(Protocol):
    """Side-channel for logs/metrics. Sync, fire-and-forget, no return values.

    The sanka adapter maps these onto the structured events the engine emits
    today (ferry.transfer.record_failed at record_runtime.py:1963-1972, the
    queue/fail/complete events, and the route-count-failed warning at
    :2835-2842), closing over workspace/run context ids.
    """

    def record_failed(self, *, route_key: str, source_object: str, source_record_id: str) -> None: ...
    def batch_completed(self, *, batches_completed: int, has_more: bool, stalled: bool) -> None: ...
    def route_count_failed(self, *, route_key: str) -> None: ...
    def attempt_fenced(self, *, attempt_id: str) -> None: ...


NULL_OBSERVER: ExecutionObserver  # module-level no-op default


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionHost:
    """Everything host-specific one execution needs, bundled once."""

    ledger: ExecutionLedger
    journal: ExecutionJournal
    fence: AttemptFence
    identity_ledger: IdentityLedger                      # ferry.runtime.mapping.pending_relationships
    shared_identity_ledger: SharedIdentityLedger | None
    observer: ExecutionObserver = NULL_OBSERVER
```

This family is the `RouteExecutionState` seam, deliberately split three ways
(`ExecutionLedger` / `ExecutionJournal` / `AttemptFence`) instead of one
protocol: the local runtime should not have to implement Hatchet-shaped
claim semantics just to run a batch, and the ledger is consulted from both
the batch executor and the continuous reconciler while the fence is not.

### 3.6 Sanka-side adapters (sketch)

One adapter module (`app/service/ferry/execution_host.py`, new) constructs
the `ExecutionHost` per execution, closing over the pinned ids — the same
shape `pending_relationships.py` already uses for its ledgers:

```python
class SankaExecutionLedger:
    def __init__(self, *, repository: FerryRuntimeRepository, workspace_id: str, run_id: str) -> None: ...

    async def terminal_destination_ids(self, *, source_object, source_record_ids, destination_object):
        return await self._repository.get_terminal_destination_record_ids(   # record_runtime.py:312-320
            workspace_id=self._workspace_id, run_id=self._run_id,
            source_object=source_object, source_record_ids=source_record_ids,
            destination_object=destination_object,
        )

    async def upsert_results(self, results):
        return await self._repository.upsert_record_results(                 # :356-362
            workspace_id=self._workspace_id, run_id=self._run_id,
            records=[_result_row(outcome) for outcome in results],           # exact dict spelling of :1906-1917
        )

    async def status_totals(self):
        return await self._repository.summarize_record_results(...)         # :364-369

    async def pair_status_totals(self):
        rows = await self._repository.summarize_record_results_by_route(...) # :371-376 (rows are pair-level)
        return [PairStatusTotal(...) for row in rows]

    async def failed_source_ids_by_pair(self):
        rows = await self._repository.list_record_result_source_ids_by_route_status(  # :378-384
            ..., statuses=["failed"],
        )
        return [PairFailedIds(...) for row in rows]


class SankaExecutionJournal:
    """Owns the transfer stage report envelope.

    load(): get_stage("transfer") → job fence check → codec parse → JournalEntry
            (raises FERRY_EXECUTION_JOB_SUPERSEDED when another job owns it).
    save(): codec render (mechanics keys byte-identical; envelope copy from
            status→summary table) →
            job_id set:  update_transfer_stage_for_job (record_runtime.py:245-254);
                         on None: re-read; cancelled → finalize_cancelled_transfer_execution
                         with the cancelled report (:2124-2147), return "cancelled";
                         else return "superseded".
            job_id None: upsert_stage (the manual single-batch path, :2154-2161), return "saved".
    """


class SankaAttemptFence:
    """claim(): claim_transfer_execution_attempt (:2492-2500);
    on None: re-read; cancelled → ("cancelled", entry); else ("superseded", None) —
    exactly the branch at :2501-2518."""
```

The run-status transitions (`running`/`needs_review`,
`record_runtime.py:2162-2166`) and summary copy stay in `record_runtime.py`
around the delegation call — they are API surface, not mechanics.

### 3.7 Local runtime implementation (sketch)

`ferry/runtime/execution/local.py` implements all three protocols over the
same SQLite file the `SqliteStateStore` uses:

```sql
CREATE TABLE IF NOT EXISTS execution_results (      -- ExecutionLedger (pair-keyed)
    run_id TEXT NOT NULL,
    source_object TEXT NOT NULL,
    destination_object TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    destination_record_id TEXT,
    status TEXT NOT NULL,
    message TEXT,
    PRIMARY KEY (run_id, source_object, destination_object, source_record_id)
);
CREATE TABLE IF NOT EXISTS execution_journal (      -- ExecutionJournal + AttemptFence
    run_id TEXT PRIMARY KEY,
    job_id TEXT,
    attempt_id TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 0,
    task_run_id TEXT,
    status TEXT NOT NULL,
    entry_json TEXT NOT NULL,                        -- codec-rendered JournalEntry
    updated_at TEXT NOT NULL
);
```

The async protocol methods are `async def` over synchronous sqlite3 calls
(single-process, no real awaits) — same policy the sync `SqliteStateStore`
takes today, now behind the async signatures the family requires. The fence
is a straight UPDATE … WHERE guarded by `(job_id, attempt_id)`; in a
single-process CLI run it never loses, but crash-resume across CLI
invocations gets exact attempt semantics for free. The existing
`checkpoints`/`ledger` tables (`state.py:112-127`) stay serving the v0
engine until PR F-5 swaps `apply` onto this family.

### 3.8 Where the two sides' shapes genuinely conflict

1. **Sync `StateStore` vs async production repository.** Resolved by the
   sibling family being async (§3.2); the v0 engine adopts it in PR F-5 and
   `StateStore` keeps its sync contract for lifecycle documents.
2. **Ledger keying: route vs pair.** OSS `ledger` rows key on
   `(run_id, route_key, source_record_id)` (`state.py:119-127`); production
   durable results key on the object pair
   (`get_terminal_destination_record_ids` takes source/destination objects,
   `record_runtime.py:312-320`), because two filtered routes over one pair
   share idempotency. Resolution: `ExecutionLedger` is **pair-keyed**
   (production semantics win); route-level counts are working state in the
   snapshot, and the durable rebuild is restricted to unique-pair routes
   exactly as `record_runtime.py:2975-2979` does. The v0 planner only emits
   filter-less routes (`planner.py:187` passes `source_filter=None`), so its
   route and pair are the same records; PR F-5 migrates the local table.
3. **Missing-identity records.** Production silently drops records without a
   source id (`record_runtime.py:1639-1641`); the v0 engine fabricates a
   synthetic failed ledger entry (`engine.py:320-329`). Resolution: explicit
   parameter `on_missing_identity: Literal["drop", "fail"]` on the batch
   executor. sanka-api passes `"drop"` (behavior preservation, pinned by the
   contract suite); the OSS engine keeps `"fail"` (stricter default is right
   for a tool whose verify step reconciles counts).
4. **Status vocabularies.** Journal `ExecutionStatus` is the production
   stage vocabulary (`queued/running/completed/failed/cancelled`). The OSS
   `RunStatus` (`state.py:33-40`) stays untouched for lifecycle; PR F-5 adds
   `CANCELLED` (additive) and maps `running → APPLYING`,
   `completed → APPLIED` at the engine boundary.
5. **Error taxonomy.** Production raises `AppError` with HTTP status;
   upstream raises `ExecutionFault` with code only; sanka's `oss_bridge`
   restores the exact `AppError` (extends `oss_bridge.py:46-74`). Verified
   deterministic: every execution-fault code above maps to exactly one
   status in current production raise sites.
6. **`WriteOptions` construction.** The v0 engine takes identity targets
   from the plan (`engine.py:275-279`); production derives them per route
   from the mapping fields (`record_runtime.py:1631-1637`, via
   `destination_identity_fields`, already upstream). The batch executor
   derives from `ExecutionRoute.fields` — both hosts satisfy it, no
   signature conflict.

## 4. Component decomposition

New package `ferry/runtime/execution/` in `ferry-migrate` (AGPL-3.0-only).
LOC bounds are production-source lines per implementation PR (tests
excluded, themselves bounded by porting the pinned sanka behaviors listed in
§6 tripwires).

| Module | Responsibility | Inputs → outputs | Absorbs (sanka-api anchors) | Est. LOC |
|---|---|---|---|---|
| `errors.py` | `ExecutionFault` + code catalog | — | error codes scattered through `record_runtime.py` (§3.3 list) | ~60 |
| `model.py` | §3.4 dataclasses; progress marker; scope hash | — | `execution_state.py:549-558` (marker); shapes of `:1466`, `:1519-1528`, `:1561-1570` | ~240 |
| `state.py` | §3.5 protocols, outcomes, `ExecutionHost`, `NULL_OBSERVER` | — | repository-protocol shapes `record_runtime.py:245-266`, `:303-384` | ~160 |
| `report_codec.py` | `ExecutionSnapshot`/`JournalEntry` ↔ report-payload dicts, byte-identical spellings; manifest canonicalization + equality checks; HWM-map validation; legacy checkpoint remap; heartbeat freshness | payload dict ↔ model | `execution_state.py:69-160`, `:223-305`, `:308-381`, `:462-558` | ~380 |
| `local.py` | `SqliteExecutionState`: all three protocols over SQLite (§3.7) | sqlite path → host impls | new (schema §3.7) | ~220 |
| `scope.py` | Freeze scope: per-route HWMs via `SupportsHighWaterMark`; frozen totals via `SupportsBoundedCounts`/`SupportsRecordCounts`; scope validation against a saved manifest | connectors + routes → `ExecutionScope` | `record_runtime.py:2846-2871`, `:2770-2844` (minus the map-stage read `:2779-2790`, which stays host-side) | ~170 |
| `routes.py` | `run_batch(...)`: one pass over selected routes — page read (bounded when scoped), completed-route pending retry, terminal skip, mapping, reference resolution, owner mapping, single/batch writes, relationship phase + pending parking, count-verified ledger upsert, failed→skipped repair, counters/failed-set dedupe, checkpoint advance, batch-page capture | `ExecutionRoute`s + connectors + credentials + `WriteOptions`/policies + snapshot + host → mutated snapshot + `has_more` | `record_runtime.py:1465-2033` (write phase `:1470-1773`, relationship phase `:1775-1904`, persistence + repair `:1906-1956`, bookkeeping `:1957-2023`), owner acquisition `:1468-1485` | ~580 |
| `continuous.py` | `ContinuousExecutor.run(...)`: fenced batch loop — journal polls for supersede/cancel, delegate one batch to a `BatchStep`, stall detection via progress marker, terminal-batch reconciliation, frozen-total reopening, durable rebuild, progress/ETA report build, heartbeat journal save, pause + batch safety limit, failure marking | `ExecutionHost` + `ExecutionScope` + `AttemptIdentity` + `BatchStep` → terminal `JournalEntry` | `record_runtime.py:2455-2747` (loop `:2566-2747`), `:2873-2966`, `:2968-3017`, `:3019-3212` (pure assembly), `:3214-3264` (failure marking), `execution_state.py:384-459` | ~520 |
| `validate.py` (later) | Write-free route sampling: per-route sample read, mapping, capped rejects with deduped reasons; signature admits **no destination connector and no ledger writes** | routes + `SourceConnector` + credentials + limits → validation report rows | `record_runtime.py:1174-1273`, `execution_state.py:26-66` | ~170 |

`BatchStep` is the small protocol that lets continuous execution wrap either
host's batch path:

```python
class BatchStep(Protocol):
    """Run one batch and persist it; return the saved state."""

    async def __call__(self) -> tuple[JournalEntry, bool]: ...   # (entry, has_more)
```

In sanka-api, the `BatchStep` is the delegated `execute()` (which saves via
the journal itself, preserving the two-saves-per-batch cadence of
`record_runtime.py:2100` + `:2665`); in the OSS engine it is
`run_batch` + `journal.save`.

What each sanka module becomes after delegation:

- `execution_state.py` → delegation shim over `report_codec.py` (the exact
  pattern of `record_mapping.py` post-#2813), re-exporting the same names.
- `record_runtime.py` keeps: the class, its constructor and control-plane
  protocols, scan orchestration, provisioning, start/cancel endpoints,
  confirm/entitlement gates, run-status transitions, stage envelopes — and
  calls `run_batch` / `ContinuousExecutor` through the `ExecutionHost`
  adapters. Rough residue ≈ 1,700-1,900 lines.

## 5. Safety invariants at the protocol level

Mapped against the internal workspace-safety runbook
(`docs/operations/customer-migration-workspace-safety.md` in the sanka
workspace) and `docs/ARCHITECTURE.md` tenets 2-4.

| Invariant | Today (anchors) | At the seam |
|---|---|---|
| **One execution, one workspace** (runbook "Required pinned identifiers") | Every repository call carries `workspace_id`/`run_id` explicitly (`record_runtime.py:190-393`) | Protocols are scope-free (§3.1); the host adapter closes over the pinned UUIDs at construction. Upstream code cannot express a workspace switch. |
| **Hash-bound scope** (tenet 3; runbook "deterministic high-water mark and candidate-set hash") | Manifest canonicalized + compared on every queue/claim/batch (`execution_state.py:69-160`; `record_runtime.py:2524-2551`, `:3292-3301`); HWM map validated against the manifest (`execution_state.py:271-305`); OSS plan hash (`engine.py:191-194`) | `ExecutionScope` is frozen and carries `scope_hash` (`content_hash` over manifest + selection + HWMs, `hashing.py:32-35`). `ContinuousExecutor` re-derives the scope from the claimed journal entry and refuses on any mismatch (`FERRY_ROUTE_MANIFEST_CHANGED` / `FERRY_EXECUTION_HIGH_WATER_MARK_INVALID`). Structural comparison is preserved for parity; the hash gives approvals a stable handle (§8 Q3). |
| **Exact-ID pilot sets** (runbook "A pilot must use an exact saved ID set (and its hash)") | Operator procedure only; product contract = frozen HWM + bounded reads/counts + reopen-to-total | `ExecutionScope` grows a tagged variant `ExactIdScope(candidate_ids_by_route, candidate_hash)` (PR F-6): `run_batch` intersects every page with the candidate set, refuses completion until the ledger covers exactly the candidate ids, and the reconciliation identity is checked against `len(candidate_ids)`. New capability, sequenced after parity — not smuggled into the behavior-preserving swap. |
| **Write-free validation** (tenet 2; runbook "do not call a destination adapter") | `dry_run` discards the destination connector and never invokes it (`record_runtime.py:1166` binds only the source; the loop at `:1192-1273` performs source reads and pure mapping only) | `validate.py`'s signature admits no `DestinationConnector` and no `ExecutionLedger` — write-freedom is visible in the type, not a convention. |
| **Attempt idempotency** (`attempt_id = task_run_id:attempt_number`) | Derivation `app/jobs/ferry/execution.py:73-78`; CAS claim `record_runtime.py:2490-2518`; per-batch fence re-check `:2585-2590`; durable-rebuild-per-attempt marker `durableResultsAttemptId` `:1445-1464`, `:2097`; failed→skipped repair `:1933-1956`; idempotent ledger upsert (OSS `state.py:224-243`) | `AttemptFence.claim` returns `claimed/superseded/cancelled`; `ExecutionJournal.save` returns the same dispositions so a fenced-out writer can never clobber a newer attempt; `ExecutionLedger.upsert_results` is upsert-by-key and count-verified (fail closed on shortfall); `JournalEntry.durable_results_attempt_id` preserves the rebuild-once-per-attempt marker. |
| **Reconciliation identity** (runbook `created + updated + skipped + failed = candidate count`) | Frozen totals per route (`record_runtime.py:2770-2844`); a route may complete only when `Σ(created, updated, skipped) = frozen total` — shortfall reopens it from a safe keyset checkpoint (`execution_state.py:384-459`, `record_runtime.py:2624-2644`); `failed` is a deduped id set counted disjointly (`:1975-1985`) and any stall surfaces as `failed` status for review (`:2645`, `:2662-2663`) | `ContinuousExecutor` takes `route_totals` inside the scope and will not emit `status="completed"` while any selected route's terminal count is short; the durable authority is `ExecutionLedger.pair_status_totals` (not in-memory counters); failed records keep the run in `failed`-for-review rather than silently closing. The identity is checked where the runbook checks it: terminal counts + failed set against the frozen candidate count. |
| **Fail-closed persistence** | `record_runtime.py:1918-1932` raises when the bulk save persisted fewer rows than the batch produced | Preserved as a protocol contract on `upsert_results` (§3.5) — the engine, not the adapter, enforces it, so every host inherits it. |
| **No resumption of superseded work** (runbook "Never resume an old partial run…") | Job/attempt supersede checks before and during every batch (`record_runtime.py:2473-2478`, `:2567-2590`) | `ExecutionJournal.load` raises `FERRY_EXECUTION_JOB_SUPERSEDED` for a non-owning job; every save is disposition-checked. |

## 6. Migration sequence

Rules of the road for every PR below:

- **Delivery mechanism** sanka-side: revendor the wheels per
  `vendor/ferry/PROVENANCE.txt` (pinned ferry commit, `uv build` from a
  clean worktree, sha256s recorded). Ferry-side PRs land first; the matching
  sanka PR pins their commit.
- **Tripwires** sanka-side always include:
  `tests/unit/service/ferry/test_runtime_service.py` (≈70 pinned behaviors,
  4,069 lines) and the #2811 characterization suite
  `tests/api/test_ferry_record_runtime_contract_api.py` (1,048 lines pinning
  HTTP codes, envelopes, and load-bearing report keys), run with bounded
  workers and diffed against the known-failures baseline. Ferry-side always
  include `packages/ferry-migrate/tests/` (engine e2e, pg→CH flagship e2e,
  state store, planner) plus the import-boundary and license scripts.
- **Rollback** sanka-side is always `git revert` + redeploy: every PR is
  code-only, and the codec contract (§3.4) keeps persisted stage-report
  spellings and result-row shapes byte-identical, so state written before,
  during, and after any PR is mutually readable. No schema migrations until
  F-5 (ferry-local only, additive).
- **Bound**: ≤ ~600 production-source LOC per PR.

| # | Side | Scope | Tripwire | Rollback |
|---|---|---|---|---|
| S-1 | sanka | Revendor wheels at ferry `32db881`+; swap the engine's capability probes to the SDK refinements that already landed there (commit `887c58a`: `ferry.connector.protocols` `:158`, `:171`, `:188`, `:279`); delete the now-duplicate local protocols `sdk_adapters.py:87-141` (`SupportsHighWaterMark`/`SupportsBoundedReads`/`SupportsBoundedCounts`/`SupportsPropertyProvisioning` — signatures verified identical). The SDK also ships `SupportsResourceProvisioning` (`protocols.py:292`), but its signature speaks SDK `PipelineDefinition`/`CustomObjectDefinition` while the sanka bridge still passes pydantic inputs (`sdk_adapters.py:144-158`, pass-through documented at `:27-33`); `runtime_checkable` isinstance checks method presence only, so the probe may switch either way — keep the local refinement until the dialect closes (§8 Q2). Independently valuable: one protocol set, and the vendored SDK gains the provisioning fields (`b98231e`) that cleanup needs. | `test_sdk_adapters.py`, unit runtime suite, contract suite | revert (wheels revert with the commit) |
| F-1 | ferry | `ferry.runtime.execution` seam v1: `errors.py`, `model.py`, `state.py` (§3.3-3.5) + unit tests. Pure additions; no engine change. | new tests; existing suites untouched | revert (additive) |
| F-2 | ferry | `report_codec.py` + `local.py` (§3.4 codec, §3.7 store) + round-trip tests built from real production report fixtures (checkpoints, legacy source-keyed checkpoints, pending relationships, batch pages). | codec round-trip property tests; `test_state_store.py` untouched | revert (additive; new tables only) |
| S-2 | sanka | `execution_state.py` becomes a delegation shim over `report_codec.py` (the #2813 `record_mapping.py` pattern), re-exporting the same underscore names; `oss_bridge.py` gains the execution-fault→`AppError` status table (§3.3). Byte-identical report dicts by codec contract. | full unit runtime suite (report-key assertions), contract suite | revert shim to the pure implementation |
| F-3 | ferry | `scope.py` + `routes.py` (§4): `run_batch` with write, reference, owner, relationship, repair, and bookkeeping phases; ports the pinned sanka behaviors as OSS tests (per-route checkpoints, batch partial failure not advancing past a failed record, relationship batching preserving the record checkpoint, terminal-record no-rewrite, count-verified fail-closed persistence). | new routes/scope tests; engine e2e untouched | revert (additive) |
| S-3 | sanka | `record_runtime.execute()` delegates its route loop (`:1465-2033`) to `run_batch` through the `ExecutionHost` adapters (§3.6, new `execution_host.py`); report assembly for the single-batch path rides the codec. `on_missing_identity="drop"`. | execute-family unit tests (`test_execute_*`, ≈40), contract suite, reconciliation tests (`test_execute_batch_partial_failure_does_not_advance_past_failed_record`, `test_execute_uses_destination_batch_write_and_persists_each_result`, `test_execute_fails_closed_when_bulk_result_persistence_is_incomplete`, `test_execute_does_not_rewrite_terminal_record_without_destination_id`) | revert; persisted formats unchanged |
| F-4 | ferry | `continuous.py` (§4): `ContinuousExecutor`, `BatchStep`, stall detection, terminal-batch reconciliation, frozen-total reopening, durable rebuild, progress/heartbeat assembly, failure marking; production defaults as constructor params (`max_batches=1_000_000`, `pause_seconds=0.25`, injectable `sleep`/clock). | new continuous tests porting the pinned behaviors (stall→reopen→resume, ten-thousand-batch resume, fenced older attempt, terminal-page recheck before stall failure) | revert (additive) |
| S-4 | sanka | `execute_continuously` (`:2455-2747`), `_reconcile_terminal_batch_pages` (`:2873-2966`), `_durable_route_result_state` (`:2968-3017`), and `_continuous_execution_report` (`:3019-3212`) delegate to `ContinuousExecutor`; `SankaAttemptFence` + journal own the claim/cancel races; **fix the latent protocol gap**: add `finalize_cancelled_transfer_execution` to `FerryRuntimeRepository` (called at `record_runtime.py:2126`, declared only on the concrete repository `app/repository/ferry/repository.py:1344`, absent from the protocol `:190-393`). | continuous-family unit tests (`test_continuous_*`, ≈15 incl. `test_older_hatchet_attempt_is_fenced_before_destination_writes`, `test_terminal_page_is_rechecked_before_stall_failure`, `test_continuous_execution_can_resume_beyond_ten_thousand_batches`), contract suite | revert; formats unchanged |
| F-5 | ferry | v0 `MigrationEngine.apply` adopts the family: `_apply_route`/`_write_page` (`engine.py:260-392`) replaced by `run_batch` + `SqliteExecutionState`; `RunStatus` gains `CANCELLED` (additive); local `ledger` table migrates to the pair-keyed `execution_results` (v0 routes are filter-less, `planner.py:187`, so the rewrite is mechanical). The OSS engine gains relationships, references, owner mapping, bounded scope, and attempt-exact resume — the concrete payoff of "execution semantics live once". | `test_engine_e2e.py`, `test_engine_postgres_e2e.py`, `test_engine_pg_to_clickhouse_e2e.py`, CLI tests | revert; keep the old `ledger` table until one release after |
| F-6 | ferry | New capability, post-parity: `validate.py` (write-free sampling; optional CLI `ferry validate`) and `ExactIdScope` in `scope.py` (§5). | new tests; e2e untouched | revert (additive) |
| S-5 | sanka | `dry_run` core delegates to `validate.py`; optionally adopt `ExactIdScope` as the product-level exact-scope pilot contract the workspace runbook currently implements by operator procedure. | dry-run unit family, contract suite | revert |

Sequencing properties: S-1 and F-1 are independently useful and small; the
single-batch swap (S-3) lands before the continuous swap (S-4) to keep the
first destination-writing change on the lower-volume manual path; every
sanka swap is preceded by the ferry PR that its wheels pin, and no PR mixes
"move code upstream" with "change behavior" — behavior changes (F-6/S-5)
are explicitly labeled new capability.

## 7. Non-goals — permanently internal

These stay in sanka-api. The engine sees them only through the §3 protocols,
and upstreaming them would violate the one-way dependency this repo's
architecture pins (hosts depend on the runtime; the runtime knows no host).

- **Hatchet specifics** — task registration, `run_no_wait` submission,
  ctx-id propagation, attempt-identity derivation
  (`app/service/ferry/execution_scheduler.py`,
  `app/service/ferry/scan_scheduler.py`, `app/jobs/ferry/execution.py`).
  The runtime's contract is `AttemptIdentity` + the fence; *which* queue
  produced the attempt is invisible by design.
- **Program/billing hooks** — execution entitlement (402s), pack pricing,
  scan-pricing side effects (`runtime_service.py:510-594`,
  `app/service/ferry/plans.py`, `billing.py`). Commercial policy is not
  execution semantics.
- **Access control** — feature flags, product-line access, workspace object
  permissions (`runtime_service.py:596-626`).
- **Channel credentials and provider registries** —
  `FerryChannelCredentialManager`, `FerryAdapterRegistry`, and the pydantic
  adapter dialect behind `sdk_adapters.py`. The runtime receives resolved
  `Credentials` and SPI connectors, never a credential store.
- **HubSpot schema-admin vetting** — portal pinning and scope checks for
  custom-object provisioning (`record_runtime.py:641-707`) encode Sanka's
  operational credential policy, not migration semantics.
- **Stage-report envelopes and API DTOs** — summary copy, serializers, run
  status transitions, and the REST surface pinned by the contract suite.
- **data_platform runtime** — the Postgres→ClickHouse ClickPipes path
  (`app/service/ferry/data_platform_runtime.py`) is a different execution
  model (replication service orchestration, not record movement) with its
  own repository methods (`record_runtime.py:386-393`); folding it into the
  record engine would blur tenet 1 (finite migrations) for no reuse gain.

## 8. Open questions

1. **Envelope ownership: how much of the report does the codec own?**
   Mechanics keys are upstream (§3.4); but `summary` strings, `generatedAt`,
   and provider echoes are pinned verbatim by the contract suite.
   *Recommendation*: envelope copy stays host-side behind a
   status→copy table in the sanka journal adapter; the codec round-trips
   unknown keys through `JournalEntry.extras` untouched. Revisit only if a
   second host needs the same copy.
2. **Closing the `reconcile_resources` pydantic pass-through.** Ferry
   `b98231e` added stage `probability` and custom-object `properties` to the
   SDK provisioning types, removing the reason for the pass-through
   documented at `sdk_adapters.py:27-33` and the pydantic-signature
   refinement at `:144-158`. *Recommendation*: a small dedicated sanka PR
   after S-1 converting through the SDK dataclasses; keep it off the
   delegation critical path since provisioning is not execution mechanics.
3. **Persisting `scope_hash` in production stage reports.** Deriving it
   (§3.4) changes nothing; persisting it adds a report key (a contract-suite
   change) but gives approvals and the runbook's read-back gates a stable
   artifact. *Recommendation*: derive-only through S-4; persist as an
   additive key in S-5 alongside `ExactIdScope` adoption, updating the
   characterization tests in the same PR deliberately.
4. **`ExactIdScope` productization timing.** The runbook's exact-scope pilot
   is currently operator procedure. Landing it as engine capability (F-6) is
   cheap once `run_batch` exists, but the sanka-side execution contract
   (where candidate sets are saved, hashed, and approved) is product design.
   *Recommendation*: build F-6 upstream on schedule; let the sanka adoption
   (S-5) wait for the migration-ops owner to specify the approval surface.
5. **Journal granularity in the local store.** One journal row per run
   (matching production's one-transfer-stage-per-run) vs per job.
   *Recommendation*: per run with a `job_id` column (§3.7) — supersede
   semantics then mirror production's job fencing exactly.
6. **Does `providerControl` (destination `retry_metrics`,
   `record_runtime.py:3160-3167`) belong in the report or the observer?**
   *Recommendation*: keep it in the report for parity (the UI reads it);
   revisit when the observer grows a metrics consumer.
7. **Wheel cadence vs PyPI.** Every sanka PR revendors pinned wheels today;
   once `ferry-migrate` publishes to PyPI at OSS launch, the pin becomes a
   version requirement. *Recommendation*: keep vendoring through S-4 (exact
   sha256 provenance during the swap), switch to PyPI pins afterwards.
