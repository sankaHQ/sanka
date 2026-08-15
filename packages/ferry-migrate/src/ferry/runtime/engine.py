# SPDX-License-Identifier: AGPL-3.0-only
"""The migration engine: create → inspect → plan → apply → verify.

Safety properties (see docs/ARCHITECTURE.md tenets):

- ``inspect`` and ``plan`` never call a write method.
- ``apply`` is hash-bound: it executes exactly the persisted plan, and a
  caller-supplied ``plan_hash`` must match it.
- Writes are idempotent through the identity ledger: records with a terminal
  status are filtered out before writing, so re-running ``apply`` (after a
  crash, or twice) converges instead of duplicating.
- Progress checkpoints per page; resume picks up from the saved cursor.
- Retryable connector errors back off exponentially (honoring
  ``retry_after_seconds``); non-retryable errors fail the run cleanly.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from ferry.connector import (
    BatchWriteInput,
    BatchWriteResult,
    ConflictPolicy,
    ConnectorError,
    Credentials,
    SupportsBatchWrites,
    SupportsRecordCounts,
    WriteOptions,
    WriteResult,
)
from ferry.connector.protocols import DestinationConnector, SourceConnector
from ferry.runtime.hashing import canonical_json
from ferry.runtime.planner import MigrationPlan, RoutePlan, build_plan
from ferry.runtime.registry import ConnectorRegistry
from ferry.runtime.spec import EndpointSpec, MigrationSpec, resolve_env
from ferry.runtime.state import TERMINAL_WRITE_STATUSES, LedgerEntry, RunStatus, StateStore

MAX_WRITE_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 30.0


class ExecutionError(RuntimeError):
    """A migration run failed; state and ledger reflect progress so far."""


class PlanMismatchError(ExecutionError):
    """The supplied plan hash does not match the persisted plan."""


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
        destination, _ = self._destination(spec)

        source_objects = await source.discover_objects(source_credentials)
        inventory = await source.inventory(source_credentials)
        suggested = {
            obj.canonical_type: destination.automatic_target_object(obj.canonical_type)
            for obj in source_objects
        }
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

        plan = build_plan(
            source_provider=spec.source.type,
            target_provider=spec.target.type,
            inventory=_inventory_from_payload(inspection["inventory"]),
            source_objects=[_source_object_from_payload(o) for o in inspection["sourceObjects"]],
            suggest_target_object=inspection["suggestedTargets"],
        )
        self._store.save_plan(run_id, canonical_json(plan.to_payload()), plan.plan_hash)
        return plan

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
        conflict_policy = _conflict_policy(spec)

        self._store.set_status(run_id, RunStatus.APPLYING)
        try:
            for route in plan.routes:
                await self._apply_route(
                    run_id,
                    route,
                    source=source,
                    source_credentials=source_credentials,
                    destination=destination,
                    destination_credentials=destination_credentials,
                    conflict_policy=conflict_policy,
                )
        except ConnectorError as error:
            self._store.set_status(run_id, RunStatus.FAILED)
            raise ExecutionError(f"run {run_id!r} failed ({error.category}): {error}") from error
        self._store.set_status(run_id, RunStatus.APPLIED)

    async def verify(self, run_id: str) -> VerifyReport:
        run = self._store.get_run(run_id)
        if run.plan_json is None:
            raise ExecutionError(f"run {run_id!r} has no plan; nothing to verify")
        plan = MigrationPlan.from_payload(json.loads(run.plan_json))
        spec = self._spec(run_id)
        source, source_credentials = self._source(spec)
        destination, destination_credentials = self._destination(spec)
        summary = self._store.ledger_summary(run_id)

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

    async def _apply_route(
        self,
        run_id: str,
        route: RoutePlan,
        *,
        source: SourceConnector,
        source_credentials: Credentials,
        destination: DestinationConnector,
        destination_credentials: Credentials,
        conflict_policy: ConflictPolicy,
    ) -> None:
        cursor, done = self._store.get_checkpoint(run_id, route.route_key)
        if done:
            return
        terminal = self._store.terminal_source_ids(run_id, route.route_key)
        field_keys = [m.source_field for m in route.field_mappings]
        options = WriteOptions(
            conflict_policy=conflict_policy,
            identity_fields=[route.identity_target_field],
        )

        while True:
            page = await source.read_records(
                source_credentials,
                object_type=route.source_object,
                field_keys=field_keys,
                limit=self._batch_size,
                cursor=cursor,
            )
            entries = await self._write_page(
                page.records,
                route,
                terminal=terminal,
                destination=destination,
                destination_credentials=destination_credentials,
                options=options,
            )
            if entries:
                self._store.record_results(run_id, route.route_key, entries)
                terminal.update(
                    e.source_record_id for e in entries if e.status in TERMINAL_WRITE_STATUSES
                )
            cursor = page.next_cursor
            self._store.save_checkpoint(run_id, route.route_key, cursor, not page.has_more)
            if not page.has_more:
                return

    async def _write_page(
        self,
        records: list[dict[str, Any]],
        route: RoutePlan,
        *,
        terminal: set[str],
        destination: DestinationConnector,
        destination_credentials: Credentials,
        options: WriteOptions,
    ) -> list[LedgerEntry]:
        entries: list[LedgerEntry] = []
        pending: list[tuple[str, dict[str, Any]]] = []
        for record in records:
            raw_identity = record.get(route.identity_field)
            if raw_identity is None or str(raw_identity) == "":
                entries.append(
                    LedgerEntry(
                        source_record_id=f"missing-identity:{len(entries)}",
                        status="failed",
                        message=f"record missing identity field {route.identity_field!r}",
                    )
                )
                continue
            source_id = str(raw_identity)
            if source_id in terminal:
                continue
            properties = {
                m.target_field: record.get(m.source_field)
                for m in route.field_mappings
                if m.source_field in record
            }
            pending.append((source_id, properties))

        if not pending:
            return entries

        if isinstance(destination, SupportsBatchWrites):

            async def write_batch() -> list[BatchWriteResult]:
                return await destination.write_records(
                    destination_credentials,
                    object_type=route.target_object,
                    records=[
                        BatchWriteInput(trace_id=source_id, properties=properties)
                        for source_id, properties in pending
                    ],
                    options=options,
                )

            results = await self._with_retries(write_batch)
            entries.extend(
                LedgerEntry(
                    source_record_id=result.trace_id,
                    status=result.status,
                    destination_record_id=result.destination_record_id,
                    message=result.message,
                )
                for result in results
            )
        else:
            for source_id, properties in pending:

                async def write_one(bound: dict[str, Any] = properties) -> WriteResult:
                    return await destination.write_record(
                        destination_credentials,
                        object_type=route.target_object,
                        properties=bound,
                        options=options,
                    )

                result = await self._with_retries(write_one)
                entries.append(
                    LedgerEntry(
                        source_record_id=source_id,
                        status=result.status,
                        destination_record_id=result.destination_record_id,
                        message=result.message,
                    )
                )
        return entries

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
    from ferry.connector.schema import SourceObject

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
    from ferry.connector.schema import FieldSchema, Inventory, ObjectSchema

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
