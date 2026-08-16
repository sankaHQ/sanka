# SPDX-License-Identifier: AGPL-3.0-only
"""Engine-level coverage for the capabilities the execution-family adoption
(F-5) grants ``MigrationEngine.apply``: reference and relationship mappings
resolved through the execution identity ledger, owner mapping through the
destination owner directory, attempt-exact resume from the fenced journal
plus the pair-keyed ledger, frozen high-water-mark scope with saved-mark
reuse on resume, the preserved missing-identity failure semantics, and the
additive ``RunStatus.CANCELLED`` boundary mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ferry.connector import (
    BatchWriteInput,
    BatchWriteResult,
    ConnectorError,
    ConnectorRegistration,
    Credentials,
    ErrorCategory,
    FieldSchema,
    Inventory,
    ObjectSchema,
    OwnerProfile,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    SourceFilter,
    SourceObject,
    WriteOptions,
    WriteResult,
)
from ferry.runtime.engine import ExecutionError, MigrationEngine
from ferry.runtime.execution import ExecutionSnapshot, JournalEntry
from ferry.runtime.mapping.model import MigrationMappingField
from ferry.runtime.mapping.record_mapping import mapping_group_key
from ferry.runtime.planner import MigrationPlan, RoutePlan
from ferry.runtime.registry import ConnectorRegistry
from ferry.runtime.spec import EndpointSpec, MigrationSpec
from ferry.runtime.state import RunStatus, SqliteStateStore

# -- in-memory connectors ------------------------------------------------------


class MemorySource:
    """Keyset-paginated in-memory source (cursor = last record id)."""

    provider = "memsrc"
    binding_kind = "database"

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.tables = tables
        self.read_requests: list[tuple[str, str | None]] = []
        self.fail_at_cursor: str | None = None

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        return [
            SourceObject(key=table, label=table, canonical_type=table, default_selected=True)
            for table in sorted(self.tables)
        ]

    async def inventory(
        self, credentials: Credentials, *, object_types: list[str] | None = None
    ) -> Inventory:
        return Inventory(
            provider=self.provider,
            objects=[
                ObjectSchema(
                    key=table,
                    label=table,
                    canonical_type=table,
                    record_count=len(records),
                    fields=[
                        FieldSchema(key=key, label=key)
                        for key in sorted({key for record in records for key in record})
                    ],
                    identity_fields=["id"],
                )
                for table, records in sorted(self.tables.items())
            ],
        )

    async def read_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None = None,
        source_filter: SourceFilter | None = None,
    ) -> RecordPage:
        return self._page(object_type, field_keys, limit, cursor, upper_bound=None)

    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int:
        return len(self.tables[object_type])

    def _page(
        self,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None,
        upper_bound: str | None,
    ) -> RecordPage:
        self.read_requests.append((object_type, cursor))
        if self.fail_at_cursor is not None and cursor == self.fail_at_cursor:
            raise ConnectorError(
                "simulated source outage",
                category=ErrorCategory.TRANSIENT,
                retryable=False,
            )
        rows = sorted(self.tables[object_type], key=lambda record: str(record["id"]))
        if cursor is not None:
            rows = [record for record in rows if str(record["id"]) > cursor]
        if upper_bound is not None:
            rows = [record for record in rows if str(record["id"]) <= upper_bound]
        page, rest = rows[: max(1, limit)], rows[max(1, limit) :]
        return RecordPage(
            object_key=object_type,
            records=[{key: record.get(key) for key in field_keys} for record in page],
            next_cursor=str(page[-1]["id"]) if rest else None,
            has_more=bool(rest),
        )


class BoundedMemorySource(MemorySource):
    """Adds the high-water-mark family so apply freezes a bounded scope."""

    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None:
        rows = self.tables[object_type]
        return max((str(record["id"]) for record in rows), default=None)

    async def read_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None = None,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> RecordPage:
        self.bounded_requests.append((object_type, cursor, upper_bound))
        return self._page(object_type, field_keys, limit, cursor, upper_bound=upper_bound)

    async def count_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> int:
        rows = self.tables[object_type]
        return sum(1 for record in rows if str(record["id"]) <= upper_bound)

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__(tables)
        self.bounded_requests: list[tuple[str, str | None, str]] = []


class MemoryDestination:
    """Identity-upserting in-memory destination with relationship recording."""

    provider = "memdst"
    binding_kind = "database"

    def __init__(self, owners: list[OwnerProfile] | None = None) -> None:
        self.rows: dict[str, dict[str, dict[str, Any]]] = {}
        self.write_calls: list[tuple[str, dict[str, Any]]] = []
        self.relationship_writes: list[RelationshipWrite] = []
        self._owners = owners
        self._counter = 0

    def automatic_target_object(self, canonical_type: str) -> str | None:
        return canonical_type

    async def inventory(self, credentials: Credentials, *, canonical_types: set[str]) -> Inventory:
        return Inventory(
            provider=self.provider,
            objects=[
                ObjectSchema(
                    key=table,
                    label=table,
                    canonical_type=table,
                    record_count=len(rows),
                    fields=[],
                )
                for table, rows in sorted(self.rows.items())
            ],
        )

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        self.write_calls.append((object_type, dict(properties)))
        table = self.rows.setdefault(object_type, {})
        for identity_field in options.identity_fields or []:
            if identity_field not in properties:
                continue
            for destination_id, existing in table.items():
                if existing.get(identity_field) == properties[identity_field]:
                    table[destination_id] = {**existing, **properties}
                    return WriteResult(status="updated", destination_record_id=destination_id)
        self._counter += 1
        destination_id = f"D{self._counter}"
        table[destination_id] = dict(properties)
        return WriteResult(status="created", destination_record_id=destination_id)

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        self.relationship_writes.append(relationship)
        return RelationshipWriteResult(status="linked")


class OwnerMemoryDestination(MemoryDestination):
    async def list_owners(self, credentials: Credentials) -> list[OwnerProfile]:
        return list(self._owners or [])


class BatchMemoryDestination(MemoryDestination):
    """Batch-capable destination; the record-at-a-time path must stay unused."""

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        raise AssertionError("record-at-a-time destination write must not be used")

    async def write_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[BatchWriteInput],
        options: WriteOptions,
    ) -> list[BatchWriteResult]:
        results: list[BatchWriteResult] = []
        for record in records:
            self.write_calls.append((object_type, dict(record.properties)))
            self._counter += 1
            destination_id = f"D{self._counter}"
            self.rows.setdefault(object_type, {})[destination_id] = dict(record.properties)
            results.append(
                BatchWriteResult(
                    trace_id=record.trace_id,
                    status="created",
                    destination_record_id=destination_id,
                )
            )
        return results


# -- scenario plumbing ---------------------------------------------------------


def _engine(
    tmp_path: Path,
    source: MemorySource,
    destination: MemoryDestination,
    *,
    batch_size: int = 100,
) -> MigrationEngine:
    registry = ConnectorRegistry(
        {
            "memsrc": ConnectorRegistration(name="memsrc", source=source),
            "memdst": ConnectorRegistration(name="memdst", destination=destination),
        }
    )
    return MigrationEngine(
        store=SqliteStateStore(tmp_path / "state.db"),
        registry=registry,
        batch_size=batch_size,
    )


def _spec() -> MigrationSpec:
    return MigrationSpec(source=EndpointSpec(type="memsrc"), target=EndpointSpec(type="memdst"))


def _scalar_route(
    table: str,
    field_keys: list[str],
    *,
    extra_fields: list[MigrationMappingField] | None = None,
    estimated_count: int = 0,
) -> RoutePlan:
    fields = [
        MigrationMappingField(
            source_field=f"{table}.{key}",
            target_object=table,
            target_field=key,
            identity=True if key == "id" else None,
        )
        for key in field_keys
    ]
    return RoutePlan(
        route_key=mapping_group_key(table, table, None),
        source_object=table,
        target_object=table,
        canonical_type=table,
        identity_field="id",
        identity_target_fields=["id"],
        field_mappings=[*fields, *(extra_fields or [])],
        estimated_count=estimated_count,
        mapping_origin="identity",
    )


def _create_run(engine: MigrationEngine, routes: list[RoutePlan]) -> str:
    from ferry.runtime.hashing import canonical_json

    plan = MigrationPlan(source_provider="memsrc", target_provider="memdst", routes=routes)
    run_id = engine.create(_spec())
    engine.store.save_plan(run_id, canonical_json(plan.to_payload()), plan.plan_hash)
    return run_id


# -- new-capability coverage ---------------------------------------------------


async def test_apply_resolves_references_and_writes_relationships(tmp_path: Path) -> None:
    """A reference FK is rewritten to the destination id and a relationship
    is linked through the execution identity ledger — capabilities the
    pre-adoption engine did not have."""
    source = MemorySource(
        {
            "authors": [{"id": "a1", "name": "Ada"}, {"id": "a2", "name": "Grace"}],
            "posts": [{"id": "p1", "title": "Hello", "author_id": "a1", "coauthor_id": "a2"}],
        }
    )
    destination = MemoryDestination()
    engine = _engine(tmp_path, source, destination)
    run_id = _create_run(
        engine,
        [
            _scalar_route("authors", ["id", "name"], estimated_count=2),
            _scalar_route(
                "posts",
                ["id", "title"],
                estimated_count=1,
                extra_fields=[
                    MigrationMappingField(
                        source_field="posts.author_id",
                        target_object="posts",
                        target_field="author_id",
                        mapping_kind="reference",
                        source_reference_object="authors",
                        target_reference_object="authors",
                    ),
                    MigrationMappingField(
                        source_field="posts.coauthor_id",
                        target_object="posts",
                        target_field="coauthors",
                        mapping_kind="relationship",
                        source_reference_object="authors",
                        target_reference_object="authors",
                    ),
                ],
            ),
        ],
    )

    await engine.apply(run_id)

    authors = destination.rows["authors"]
    ada_id = next(dest_id for dest_id, props in authors.items() if props["name"] == "Ada")
    grace_id = next(dest_id for dest_id, props in authors.items() if props["name"] == "Grace")
    (post_id, post) = next(iter(destination.rows["posts"].items()))
    assert post["author_id"] == ada_id  # rewritten to the destination id
    assert post["author_id"] != "a1"
    link = destination.relationship_writes[0]
    assert (link.object_type, link.record_id) == ("posts", post_id)
    assert (link.relationship_field, link.related_record_id) == ("coauthors", grace_id)

    report = await engine.verify(run_id)
    assert report.ok
    assert {route.route_key: route.migrated for route in report.routes} == {
        "authors|authors": 2,
        "posts|posts": 1,
    }


async def test_apply_maps_owners_through_the_destination_directory(tmp_path: Path) -> None:
    source = MemorySource(
        {"contacts": [{"id": "c1", "name": "Nia", "owner_email": " Ada@Example.com "}]}
    )
    destination = OwnerMemoryDestination(
        owners=[OwnerProfile(id="owner-42", email="ada@example.com", active=True)]
    )
    engine = _engine(tmp_path, source, destination)
    owner_field = MigrationMappingField(
        source_field="contacts.owner_email",
        target_object="contacts",
        target_field="owner_id",
        mapping_kind="owner",
    )
    run_id = _create_run(
        engine,
        [_scalar_route("contacts", ["id", "name"], extra_fields=[owner_field], estimated_count=1)],
    )

    await engine.apply(run_id)

    (contact,) = destination.rows["contacts"].values()
    assert contact["owner_id"] == "owner-42"
    assert engine.store.get_run(run_id).status is RunStatus.APPLIED


async def test_interrupted_apply_resumes_attempt_exact(tmp_path: Path) -> None:
    """A mid-apply failure resumes from the journal checkpoint: pages already
    written are never re-read or re-written, and the run converges."""
    records = [{"id": f"r{n}", "name": f"row {n}"} for n in range(1, 5)]
    source = MemorySource({"records": records})
    destination = MemoryDestination()
    engine = _engine(tmp_path, source, destination, batch_size=2)
    run_id = _create_run(engine, [_scalar_route("records", ["id", "name"], estimated_count=4)])

    source.fail_at_cursor = "r2"  # the second page read fails
    with pytest.raises(ExecutionError, match="transient"):
        await engine.apply(run_id)
    assert engine.store.get_run(run_id).status is RunStatus.FAILED
    assert len(destination.write_calls) == 2  # first page landed durably
    assert engine.store.ledger_summary(run_id) == {"records|records": {"created": 2}}

    source.fail_at_cursor = None
    await engine.apply(run_id)

    assert engine.store.get_run(run_id).status is RunStatus.APPLIED
    # The resumed apply read from the saved checkpoint, not from the start.
    assert source.read_requests[-1] == ("records", "r2")
    assert ("records", None) not in source.read_requests[2:]
    # Each record was written exactly once across both attempts.
    assert len(destination.write_calls) == 4
    assert engine.store.ledger_summary(run_id) == {"records|records": {"created": 4}}
    report = await engine.verify(run_id)
    assert report.ok and report.routes[0].migrated == 4


async def test_resumed_apply_stays_bound_to_the_frozen_high_water_mark(tmp_path: Path) -> None:
    """With a mark-capable source the scope freezes at first apply and a
    resume reuses the saved marks: records added after the freeze stay out."""
    records = [{"id": f"r{n}", "name": f"row {n}"} for n in range(1, 5)]
    source = BoundedMemorySource({"records": records})
    destination = MemoryDestination()
    engine = _engine(tmp_path, source, destination, batch_size=2)
    run_id = _create_run(engine, [_scalar_route("records", ["id", "name"], estimated_count=4)])

    source.fail_at_cursor = "r2"
    with pytest.raises(ExecutionError, match="transient"):
        await engine.apply(run_id)

    source.fail_at_cursor = None
    source.tables["records"].append({"id": "r5", "name": "added after the freeze"})
    await engine.apply(run_id)

    assert engine.store.get_run(run_id).status is RunStatus.APPLIED
    # Every read was bounded by the frozen mark, including the resumed ones.
    assert source.bounded_requests
    assert {upper for (_object, _cursor, upper) in source.bounded_requests} == {"r4"}
    migrated_ids = {props["id"] for props in destination.rows["records"].values()}
    assert migrated_ids == {"r1", "r2", "r3", "r4"}  # r5 is outside the scope
    assert engine.store.ledger_summary(run_id) == {"records|records": {"created": 4}}


async def test_missing_identity_records_fail_and_hold_the_run(tmp_path: Path) -> None:
    """The pre-adoption engine fabricated a failed ledger entry for records
    without an identity value; the adopted engine keeps that strictness
    (``on_missing_identity="fail"``) and fails the run for review."""
    source = MemorySource({"records": [{"id": "", "name": "ghost"}]})
    destination = MemoryDestination()
    engine = _engine(tmp_path, source, destination)
    run_id = _create_run(engine, [_scalar_route("records", ["id", "name"], estimated_count=1)])

    with pytest.raises(ExecutionError, match="1 failed record"):
        await engine.apply(run_id)

    assert engine.store.get_run(run_id).status is RunStatus.FAILED
    assert destination.write_calls == []
    assert engine.store.ledger_summary(run_id) == {"records|records": {"failed": 1}}


async def test_apply_uses_destination_batch_writes(tmp_path: Path) -> None:
    """A batch-capable destination gets one batch write per page through the
    engine (the flagship ClickHouse path), and the pair ledger still keys
    results by the source identity."""
    records = [{"id": f"r{n}", "name": f"row {n}"} for n in range(1, 4)]
    source = MemorySource({"records": records})
    destination = BatchMemoryDestination()
    engine = _engine(tmp_path, source, destination, batch_size=2)
    run_id = _create_run(engine, [_scalar_route("records", ["id", "name"], estimated_count=3)])

    await engine.apply(run_id)

    assert engine.store.get_run(run_id).status is RunStatus.APPLIED
    assert len(destination.write_calls) == 3
    assert engine.store.ledger_summary(run_id) == {"records|records": {"created": 3}}
    assert engine.store.terminal_source_ids(run_id, "records|records") == {"r1", "r2", "r3"}
    report = await engine.verify(run_id)
    assert report.ok and report.routes[0].migrated == 3


async def test_apply_honors_a_cancellation_recorded_in_the_journal(tmp_path: Path) -> None:
    """No v0 flow issues a cancellation; when the journal already records
    one, apply maps it onto the additive RunStatus.CANCELLED and writes
    nothing."""
    source = MemorySource({"records": [{"id": "r1", "name": "row"}]})
    destination = MemoryDestination()
    engine = _engine(tmp_path, source, destination)
    run_id = _create_run(engine, [_scalar_route("records", ["id", "name"], estimated_count=1)])

    store = engine.store
    assert isinstance(store, SqliteStateStore)
    state = store.execution_state(run_id)
    try:
        await state.save(
            JournalEntry(
                status="cancelled",
                snapshot=ExecutionSnapshot(),
                job_id=None,
                attempt_id=None,
                batch_size=1,
                batches_completed=0,
                started_at=None,
                last_heartbeat_at=None,
                resumed=False,
                scope=None,
                durable_results_attempt_id=None,
            )
        )
    finally:
        state.close()

    with pytest.raises(ExecutionError, match="cancelled"):
        await engine.apply(run_id)

    assert engine.store.get_run(run_id).status is RunStatus.CANCELLED
    assert destination.write_calls == []
