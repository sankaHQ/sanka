# SPDX-License-Identifier: AGPL-3.0-only
"""The migration engine: create → inspect → plan → apply → verify.

Safety properties (see docs/ARCHITECTURE.md tenets):

- ``inspect`` and ``plan`` never call a write method.
- ``apply`` is hash-bound: it executes exactly the persisted plan, and a
  caller-supplied ``plan_hash`` must match it.
- ``apply`` delegates batch execution to the shared
  :mod:`sanka.runtime.execution` family — the same route executor the
  production host runs — so the local engine carries relationships,
  references, owner mapping, frozen high-water-mark scope (when the source
  supports it, degrading gracefully otherwise), and attempt-exact resume:
  every batch lands in the fenced execution journal, and per-record results
  land in the pair-keyed execution ledger with a count-verified fail-closed
  write.
- Writes are idempotent through that ledger: records with a terminal status
  are skipped without a rewrite, so re-running ``apply`` (after a crash, or
  twice) converges instead of duplicating; failed records hold the route
  checkpoint and are retried on resume.
- Retryable connector errors back off exponentially (honoring
  ``retry_after_seconds``); non-retryable errors fail the record, and a
  batch that stops making progress fails the run for review.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

from sanka.connector import ConflictPolicy, ConnectorError, Credentials, SupportsRecordCounts
from sanka.connector.protocols import DestinationConnector, SourceConnector
from sanka.runtime.execution import (
    DEFAULT_VALIDATION_SAMPLE_SIZE,
    AttemptFence,
    ExecutionFault,
    ExecutionHost,
    ExecutionJournal,
    ExecutionLedger,
    ExecutionRoute,
    ExecutionScope,
    ExecutionSnapshot,
    ExecutionStatus,
    JournalEntry,
    WritePolicies,
    freeze_scope,
    normalized_attempt_identity,
    run_batch,
    validate_routes,
)
from sanka.runtime.hashing import canonical_json
from sanka.runtime.mapping.record_mapping import MappingGroup, mapping_group_key
from sanka.runtime.planner import MigrationPlan, RoutePlan, build_plan
from sanka.runtime.registry import ConnectorRegistry
from sanka.runtime.spec import EndpointSpec, MigrationSpec, resolve_env
from sanka.runtime.state import TERMINAL_WRITE_STATUSES, RunStatus, StateStore

MAX_WRITE_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 30.0
_FAILURE_MESSAGE_LIMIT = 500


class ExecutionError(RuntimeError):
    """A migration run failed; state and ledger reflect progress so far."""


class PlanMismatchError(ExecutionError):
    """The supplied plan hash does not match the persisted plan."""


@runtime_checkable
class ExecutionState(ExecutionLedger, ExecutionJournal, AttemptFence, Protocol):
    """One run's bundled execution-state family: ledger, journal, and fence."""

    def close(self) -> None: ...


@runtime_checkable
class ExecutionStateProvider(Protocol):
    """Lifecycle stores that can hand out a run's execution-state family.

    :class:`sanka.runtime.state.SqliteStateStore` provides it over the same
    SQLite file; an embedder store implements this seam over its own
    persistence to run ``apply``/``verify``.
    """

    def execution_state(self, run_id: str) -> ExecutionState: ...


class _ExecutionIdentityLedger:
    """Run-scoped identity lookups over the pair-keyed execution ledger.

    The mapping family's reference/relationship resolution asks an
    ``IdentityLedger`` for destination ids; in the local engine the durable
    execution ledger is exactly that record — terminal rows whose
    destination id is known.
    """

    def __init__(self, ledger: ExecutionLedger) -> None:
        self._ledger = ledger

    async def get_destination_record_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str]:
        terminal = await self._ledger.terminal_destination_ids(
            source_object=source_object,
            source_record_ids=source_record_ids,
            destination_object=destination_object,
        )
        return {
            source_id: destination_id
            for source_id, destination_id in terminal.items()
            if destination_id
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteVerification:
    route_key: str
    source_count: int | None
    migrated: int
    failed: int
    destination_count: int | None
    ok: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifyReport:
    run_id: str
    routes: list[RouteVerification]

    @property
    def ok(self) -> bool:
        return bool(self.routes) and all(r.ok for r in self.routes)


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectionResult:
    source_objects: list[dict[str, Any]]
    inventory: dict[str, Any]
    suggested_targets: dict[str, str | None]


class MigrationEngine:
    """Drives migration runs against a state store and connector registry."""

    def __init__(
        self,
        *,
        store: StateStore,
        registry: ConnectorRegistry,
        batch_size: int = 100,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        env: dict[str, str] | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._batch_size = batch_size
        self._sleep = sleep
        self._env = env

    @property
    def store(self) -> StateStore:
        return self._store

    # -- lifecycle ----------------------------------------------------------

    def create(self, spec: MigrationSpec, *, name: str | None = None, reuse: bool = True) -> str:
        """Create a run for ``spec``, or reuse the latest non-failed run for
        the same spec hash so plan/apply/verify across CLI invocations share
        state."""
        if reuse:
            existing = self._store.find_latest_run(spec.spec_hash)
            if existing is not None and existing.status is not RunStatus.FAILED:
                return existing.id
        run_id = uuid.uuid4().hex[:12]
        self._store.create_run(
            run_id=run_id,
            spec_json=canonical_json(spec.to_dict()),
            spec_hash=spec.spec_hash,
            name=name,
        )
        return run_id

    async def inspect(self, run_id: str) -> InspectionResult:
        spec = self._spec(run_id)
        source, source_credentials = self._source(spec)
        destination, destination_credentials = self._destination(spec)

        source_objects = await source.discover_objects(source_credentials)
        inventory = await source.inventory(source_credentials)
        suggested = {
            obj.canonical_type: destination.automatic_target_object(obj.canonical_type)
            for obj in source_objects
        }
        # Destination inventory is a read: it feeds the auto-mapper when the
        # target already has schema, and stays empty for fresh targets.
        destination_inventory = await destination.inventory(
            destination_credentials,
            canonical_types={obj.canonical_type for obj in source_objects},
        )
        result = InspectionResult(
            source_objects=[_source_object_payload(o) for o in source_objects],
            inventory=_inventory_payload(inventory),
            suggested_targets=suggested,
        )
        self._store.save_inspection(
            run_id,
            canonical_json(
                {
                    "sourceObjects": result.source_objects,
                    "inventory": result.inventory,
                    "suggestedTargets": result.suggested_targets,
                    "destinationInventory": _inventory_payload(destination_inventory),
                }
            ),
        )
        return result

    async def plan(self, run_id: str) -> MigrationPlan:
        spec = self._spec(run_id)
        run = self._store.get_run(run_id)
        if run.inspection_json is None:
            await self.inspect(run_id)
            run = self._store.get_run(run_id)
        assert run.inspection_json is not None
        inspection = json.loads(run.inspection_json)

        raw_destination_inventory = inspection.get("destinationInventory")
        plan = build_plan(
            source_provider=spec.source.type,
            target_provider=spec.target.type,
            inventory=_inventory_from_payload(inspection["inventory"]),
            source_objects=[_source_object_from_payload(o) for o in inspection["sourceObjects"]],
            suggest_target_object=inspection["suggestedTargets"],
            destination_inventory=(
                None
                if raw_destination_inventory is None
                else _inventory_from_payload(raw_destination_inventory)
            ),
        )
        self._store.save_plan(run_id, canonical_json(plan.to_payload()), plan.plan_hash)
        return plan

    async def validate(
        self,
        run_id: str,
        *,
        sample_size: int = DEFAULT_VALIDATION_SAMPLE_SIZE,
        full: bool = False,
    ) -> dict[str, Any]:
        """Write-free validation of the persisted plan against live records.

        Reads a sampled page per plan route (every page with ``full``) and
        projects the records through the reviewed mapping — the production
        dry run. The destination connector is never resolved, no execution
        state is opened, and no run status changes: per ARCHITECTURE
        tenet 2, nothing on this path can write. Returns the deterministic
        validation payload (``objects`` rows, capped ``rejects``,
        ``warnings``) from :func:`sanka.runtime.execution.validate_routes`.
        """
        run = self._store.get_run(run_id)
        if run.plan_json is None:
            raise ExecutionError(f"run {run_id!r} has no plan; run plan first")
        plan = MigrationPlan.from_payload(json.loads(run.plan_json))
        spec = self._spec(run_id)
        source, source_credentials = self._source(spec)
        try:
            return await validate_routes(
                routes=[_execution_route(route) for route in plan.routes],
                source=source,
                source_credentials=source_credentials,
                sample_size=sample_size,
                full=full,
            )
        except ConnectorError as error:
            raise ExecutionError(
                f"run {run_id!r} validation failed ({error.category}): {error}"
            ) from error
        except ExecutionFault as fault:
            raise ExecutionError(
                f"run {run_id!r} validation failed ({fault.code}): {fault}"
            ) from fault

    async def apply(self, run_id: str, *, plan_hash: str | None = None) -> None:
        run = self._store.get_run(run_id)
        if run.plan_json is None or run.plan_hash is None:
            raise ExecutionError(f"run {run_id!r} has no plan; run plan first")
        if plan_hash is not None and plan_hash != run.plan_hash:
            raise PlanMismatchError(
                f"plan hash mismatch: expected {run.plan_hash}, got {plan_hash}"
            )
        plan = MigrationPlan.from_payload(json.loads(run.plan_json))
        spec = self._spec(run_id)
        source, source_credentials = self._source(spec)
        destination, destination_credentials = self._destination(spec)
        policies = WritePolicies(conflict_policy=_conflict_policy(spec))

        state = self._execution_state(run_id)
        host = ExecutionHost(
            ledger=state,
            journal=state,
            fence=state,
            identity_ledger=_ExecutionIdentityLedger(state),
            shared_identity_ledger=None,
        )
        self._store.set_status(run_id, RunStatus.APPLYING)
        try:
            final_status = await self._drive_apply(
                run_id,
                plan,
                host=host,
                source=source,
                source_credentials=source_credentials,
                destination=destination,
                destination_credentials=destination_credentials,
                policies=policies,
            )
        except ConnectorError as error:
            self._store.set_status(run_id, RunStatus.FAILED)
            raise ExecutionError(f"run {run_id!r} failed ({error.category}): {error}") from error
        except ExecutionFault as fault:
            self._store.set_status(run_id, RunStatus.FAILED)
            raise ExecutionError(f"run {run_id!r} failed ({fault.code}): {fault}") from fault
        except ExecutionError:
            self._store.set_status(run_id, RunStatus.FAILED)
            raise
        finally:
            state.close()
        if final_status == "cancelled":
            # No v0 flow records a cancellation itself; honoring one found in
            # the journal is the additive boundary mapping for RunStatus.
            self._store.set_status(run_id, RunStatus.CANCELLED)
            raise ExecutionError(f"run {run_id!r} was cancelled")
        self._store.set_status(run_id, RunStatus.APPLIED)

    async def verify(self, run_id: str) -> VerifyReport:
        run = self._store.get_run(run_id)
        if run.plan_json is None:
            raise ExecutionError(f"run {run_id!r} has no plan; nothing to verify")
        plan = MigrationPlan.from_payload(json.loads(run.plan_json))
        spec = self._spec(run_id)
        source, source_credentials = self._source(spec)
        destination, destination_credentials = self._destination(spec)
        summary = await self._ledger_route_summary(run_id)

        destination_counts: dict[str, int] = {}
        canonical_types = {route.canonical_type for route in plan.routes}
        destination_inventory = await destination.inventory(
            destination_credentials, canonical_types=canonical_types
        )
        for obj in destination_inventory.objects:
            destination_counts[obj.key] = obj.record_count

        routes: list[RouteVerification] = []
        for route in plan.routes:
            counts = summary.get(route.route_key, {})
            migrated = sum(counts.get(status, 0) for status in TERMINAL_WRITE_STATUSES)
            failed = counts.get("failed", 0)
            source_count = await self._source_count(source, source_credentials, route)
            ok = failed == 0 and (source_count is None or migrated == source_count)
            routes.append(
                RouteVerification(
                    route_key=route.route_key,
                    source_count=source_count,
                    migrated=migrated,
                    failed=failed,
                    destination_count=destination_counts.get(route.target_object),
                    ok=ok,
                )
            )
        report = VerifyReport(run_id=run_id, routes=routes)
        if report.ok:
            self._store.set_status(run_id, RunStatus.VERIFIED)
        return report

    # -- internals ----------------------------------------------------------

    async def _drive_apply(
        self,
        run_id: str,
        plan: MigrationPlan,
        *,
        host: ExecutionHost,
        source: SourceConnector,
        source_credentials: Credentials,
        destination: DestinationConnector,
        destination_credentials: Credentials,
        policies: WritePolicies,
    ) -> ExecutionStatus:
        """Claim the run's attempt and loop batches to a terminal status.

        Single-shot local execution over the execution family: the journal
        entry is loaded (or created), the attempt fence claims it, the scope
        is frozen (or revalidated against the persisted one on resume), and
        ``run_batch`` runs until no route holds more records — saving the
        journal after every batch, so an interrupted apply resumes from the
        exact batch it stopped at. A batch that makes no progress fails the
        run for review instead of spinning.
        """
        journal = host.journal
        entry = await journal.load()
        if entry is not None and entry.status in ("completed", "cancelled"):
            return entry.status  # idempotent re-apply / cancellation honored
        attempt = normalized_attempt_identity(job_id=f"apply:{run_id}")
        claim_outcome, claimed = await host.fence.claim(attempt)
        if claim_outcome == "cancelled":
            return "cancelled"
        if claim_outcome == "superseded":
            raise ExecutionError(
                f"run {run_id!r} is owned by another execution attempt; not resuming"
            )
        if claimed is not None:
            entry = claimed

        routes = [_execution_route(route) for route in plan.routes]
        scope = await self._freeze_apply_scope(
            plan,
            saved_scope=entry.scope if entry is not None else None,
            source=source,
            source_credentials=source_credentials,
        )
        now = _utc_now()
        if entry is None:
            entry = JournalEntry(
                status="running",
                snapshot=ExecutionSnapshot(),
                job_id=None,  # manual single-shot execution
                attempt_id=attempt.attempt_id,
                batch_size=self._batch_size,
                batches_completed=0,
                started_at=now,
                last_heartbeat_at=now,
                resumed=False,
                scope=scope,
                durable_results_attempt_id=None,
            )
        else:
            entry.status = "running"
            entry.attempt_id = attempt.attempt_id
            entry.batch_size = self._batch_size
            entry.started_at = entry.started_at or now
            entry.resumed = True
            entry.scope = scope

        marks = dict(scope.route_high_water_marks)
        previous_marker = entry.snapshot.progress_marker()
        while True:
            try:
                has_more = await run_batch(
                    routes=routes,
                    source=source,
                    source_credentials=source_credentials,
                    destination=destination,
                    destination_credentials=destination_credentials,
                    policies=policies,
                    batch_size=self._batch_size,
                    snapshot=entry.snapshot,
                    host=host,
                    route_high_water_marks=marks,
                    on_missing_identity="fail",
                    retry=self._with_retries,
                )
            except (ConnectorError, ExecutionFault) as error:
                await self._save_failed(journal, entry, message=str(error))
                raise
            entry.batches_completed += 1
            marker = entry.snapshot.progress_marker()
            stalled = has_more and marker == previous_marker
            entry.status = "failed" if stalled else ("running" if has_more else "completed")
            entry.last_heartbeat_at = _utc_now()
            outcome = await journal.save(entry)
            if outcome == "cancelled":
                return "cancelled"
            if outcome == "superseded":
                raise ExecutionError(
                    f"run {run_id!r} execution state was superseded; not overwriting"
                )
            if stalled:
                failed_count = sum(
                    counts.get("failed", 0) for counts in entry.snapshot.route_counts.values()
                )
                raise ExecutionError(
                    f"run {run_id!r} stopped without progress; {failed_count} failed"
                    " record(s) need review — fix the cause and re-apply to resume"
                )
            if not has_more:
                return "completed"
            previous_marker = marker

    async def _freeze_apply_scope(
        self,
        plan: MigrationPlan,
        *,
        saved_scope: ExecutionScope | None,
        source: SourceConnector,
        source_credentials: Credentials,
    ) -> ExecutionScope:
        """Freeze (or revalidate) the bounded scope this apply is bound to.

        The first apply freezes per-route high-water marks — sources without
        the capability degrade gracefully to unbounded reads — and counts
        the frozen route totals. A resumed apply requires the persisted
        manifest to still describe the plan's routes and reuses the saved
        marks, keeping the run bound to the originally frozen candidate set.
        """
        groups: list[MappingGroup] = [
            (route.source_object, route.target_object, None, route.field_mappings)
            for route in plan.routes
        ]
        saved_marks = dict(saved_scope.route_high_water_marks) if saved_scope is not None else {}
        return await freeze_scope(
            groups=groups,
            source=source,
            source_credentials=source_credentials,
            expected_route_manifest=(
                list(saved_scope.route_manifest) if saved_scope is not None else None
            ),
            route_high_water_marks=saved_marks or None,
        )

    async def _save_failed(
        self, journal: ExecutionJournal, entry: JournalEntry, *, message: str
    ) -> None:
        """Best-effort failed journal save; never masks the original error."""
        entry.status = "failed"
        truncated = message[:_FAILURE_MESSAGE_LIMIT]
        if truncated and truncated not in entry.snapshot.warnings:
            entry.snapshot.warnings.append(truncated)
        entry.last_heartbeat_at = _utc_now()
        try:
            await journal.save(entry)
        except ExecutionFault:
            return

    async def _ledger_route_summary(self, run_id: str) -> dict[str, dict[str, int]]:
        """Per-route status counts from the pair-keyed execution ledger.

        v0 routes are filter-less, so each object pair names exactly one
        plan route. Routes only the deprecated route-keyed store ledger
        knows (state files from before the engine swap) fall back to their
        historical counts.
        """
        state = self._execution_state(run_id)
        try:
            pair_totals = await state.pair_status_totals()
        finally:
            state.close()
        summary: dict[str, dict[str, int]] = {}
        for total in pair_totals:
            route_key = mapping_group_key(total.source_object, total.destination_object, None)
            summary.setdefault(route_key, {})[total.status] = total.count
        for route_key, counts in self._store.ledger_summary(run_id).items():
            summary.setdefault(route_key, dict(counts))
        return summary

    def _execution_state(self, run_id: str) -> ExecutionState:
        if not isinstance(self._store, ExecutionStateProvider):
            raise ExecutionError(
                f"state store {type(self._store).__name__} does not provide the"
                " execution-state family; implement execution_state(run_id)"
                " (see sanka.runtime.execution) to apply or verify runs"
            )
        return self._store.execution_state(run_id)

    async def _with_retries[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        attempt = 0
        while True:
            try:
                return await operation()
            except ConnectorError as error:
                attempt += 1
                if not error.retryable or attempt >= MAX_WRITE_ATTEMPTS:
                    raise
                delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
                if error.retry_after_seconds is not None:
                    delay = max(delay, error.retry_after_seconds)
                await self._sleep(delay)

    async def _source_count(
        self, source: SourceConnector, credentials: Credentials, route: RoutePlan
    ) -> int | None:
        if isinstance(source, SupportsRecordCounts):
            return await source.count_records(credentials, object_type=route.source_object)
        return route.estimated_count if route.estimated_count > 0 else None

    def _spec(self, run_id: str) -> MigrationSpec:
        run = self._store.get_run(run_id)
        spec = MigrationSpec.from_dict(json.loads(run.spec_json))
        return resolve_env(spec, env=self._env) if self._env is not None else resolve_env(spec)

    def _source(self, spec: MigrationSpec) -> tuple[SourceConnector, Credentials]:
        return self._registry.source(spec.source.type), _credentials(spec.source)

    def _destination(self, spec: MigrationSpec) -> tuple[DestinationConnector, Credentials]:
        return self._registry.destination(spec.target.type), _credentials(spec.target)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _execution_route(route: RoutePlan) -> ExecutionRoute:
    """The executable shape of one reviewed plan route (v0: filter-less)."""
    return ExecutionRoute(
        route_key=route.route_key,
        source_object=route.source_object,
        destination_object=route.target_object,
        source_filter=None,
        fields=route.field_mappings,
        source_identity_field=route.identity_field,
    )


def _conflict_policy(spec: MigrationSpec) -> ConflictPolicy:
    value = spec.strategy.get("conflict_policy", "update_existing")
    if value not in ("create", "skip_existing", "update_existing"):
        raise ExecutionError(
            f"strategy.conflict_policy must be create, skip_existing, or"
            f" update_existing; got {value!r}"
        )
    return cast(ConflictPolicy, value)


def _credentials(endpoint: EndpointSpec) -> Credentials:
    settings: dict[str, Any] = dict(endpoint.options)
    if endpoint.connection is not None:
        settings.setdefault("connection", endpoint.connection)
    return Credentials(provider=endpoint.type, connection_id=endpoint.connection, settings=settings)


def _source_object_payload(obj: Any) -> dict[str, Any]:
    return {
        "key": obj.key,
        "label": obj.label,
        "canonicalType": obj.canonical_type,
        "defaultSelected": obj.default_selected,
        "automaticTargetObject": obj.automatic_target_object,
    }


def _source_object_from_payload(payload: dict[str, Any]) -> Any:
    from sanka.connector.schema import SourceObject

    return SourceObject(
        key=payload["key"],
        label=payload["label"],
        canonical_type=payload["canonicalType"],
        default_selected=payload["defaultSelected"],
        automatic_target_object=payload["automaticTargetObject"],
    )


def _inventory_payload(inventory: Any) -> dict[str, Any]:
    return {
        "provider": inventory.provider,
        "warnings": list(inventory.warnings),
        "objects": [
            {
                "key": obj.key,
                "label": obj.label,
                "canonicalType": obj.canonical_type,
                "recordCount": obj.record_count,
                "identityFields": list(obj.identity_fields),
                "fields": [
                    {"key": f.key, "label": f.label, "dataType": f.data_type} for f in obj.fields
                ],
            }
            for obj in inventory.objects
        ],
    }


def _inventory_from_payload(payload: dict[str, Any]) -> Any:
    from sanka.connector.schema import FieldSchema, Inventory, ObjectSchema

    return Inventory(
        provider=payload["provider"],
        warnings=list(payload["warnings"]),
        objects=[
            ObjectSchema(
                key=obj["key"],
                label=obj["label"],
                canonical_type=obj["canonicalType"],
                record_count=obj["recordCount"],
                identity_fields=list(obj["identityFields"]),
                fields=[
                    FieldSchema(key=f["key"], label=f["label"], data_type=f["dataType"])
                    for f in obj["fields"]
                ],
            )
            for obj in payload["objects"]
        ],
    )
