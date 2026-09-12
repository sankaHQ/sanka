# SPDX-License-Identifier: AGPL-3.0-only
"""Batch route executor tests: the pinned production behaviors as OSS tests.

Each test ports a behavior the production suite pins for the single-batch
route loop: per-route checkpoints for filtered routes, batch partial failure
never advancing past a failed record, destination batch writes persisting
every result, terminal records never rewritten, count-verified fail-closed
persistence, reference resolution before the write, owner mapping through
the destination directory, and scope-bounded reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from sanka.runtime.execution import (
    ExecutionFault,
    ExecutionHost,
    ExecutionRoute,
    ExecutionSnapshot,
    JournalEntry,
    PairFailedIds,
    PairStatusTotal,
    RecordWriteOutcome,
    SaveOutcome,
    WritePolicies,
    execution_routes,
    run_batch,
)
from sanka.runtime.execution.model import AttemptIdentity
from sanka.runtime.execution.state import ClaimOutcome
from sanka.runtime.mapping import (
    MappingError,
    MigrationMappingField,
    mapping_groups,
    pending_relationship,
    pending_relationship_key,
)
from sanka.runtime.state import TERMINAL_WRITE_STATUSES
from sanka_extensions.data import (
    BatchRelationshipWriteResult,
    BatchWriteInput,
    BatchWriteResult,
    Credentials,
    OwnerProfile,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    SourceFilter,
    SourceObject,
    WriteOptions,
    WriteResult,
)

_CREDENTIALS = Credentials(provider="fake")


# -- fakes --------------------------------------------------------------------


class FakeSource:
    provider = "fake-source"
    binding_kind = "api_token"

    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self.records = records if records is not None else [{"Id": "001A", "Name": "Acme"}]
        self.read_requests: list[dict[str, Any]] = []

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        return []

    async def inventory(
        self, credentials: Credentials, *, object_types: list[str] | None = None
    ) -> Any:
        raise NotImplementedError

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
        self.read_requests.append(
            {
                "object_type": object_type,
                "field_keys": field_keys,
                "limit": limit,
                "cursor": cursor,
                "source_filter": source_filter,
            }
        )
        return RecordPage(object_key=object_type, records=[dict(r) for r in self.records])


class SourceWithOwners(FakeSource):
    def __init__(
        self,
        records: list[dict[str, Any]] | None = None,
        owners: list[OwnerProfile] | None = None,
    ) -> None:
        super().__init__(records)
        self.owners = owners or []

    async def list_owners(self, credentials: Credentials) -> list[OwnerProfile]:
        return list(self.owners)


class BoundedSource(FakeSource):
    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        super().__init__(records)
        self.bounded_requests: list[dict[str, Any]] = []

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
        self.bounded_requests.append(
            {"object_type": object_type, "cursor": cursor, "upper_bound": upper_bound}
        )
        return RecordPage(object_key=object_type, records=[dict(r) for r in self.records])


class FakeDestination:
    provider = "fake-destination"
    binding_kind = "api_token"

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.write_options: list[WriteOptions] = []
        self.relationships: list[RelationshipWrite] = []
        self._write_counter = 0

    def automatic_target_object(self, canonical_type: str) -> str | None:
        return None

    async def inventory(self, credentials: Credentials, *, canonical_types: set[str]) -> Any:
        raise NotImplementedError

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        self.writes.append(dict(properties))
        self.write_options.append(options)
        self._write_counter += 1
        return WriteResult(status="created", destination_record_id=f"dest-{self._write_counter}")

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        self.relationships.append(relationship)
        return RelationshipWriteResult(status="linked")


class DestinationWithOwners(FakeDestination):
    def __init__(self, owners: list[OwnerProfile] | None = None) -> None:
        super().__init__()
        self.owners = (
            owners
            if owners is not None
            else [OwnerProfile(id="owner-42", email="ada@example.com", active=True)]
        )

    async def list_owners(self, credentials: Credentials) -> list[OwnerProfile]:
        return list(self.owners)


class BatchDestination(FakeDestination):
    """Batch-capable destination; the single-record path must stay unused."""

    def __init__(self) -> None:
        super().__init__()
        self.batch_writes: list[list[tuple[str, dict[str, Any]]]] = []

    async def write_record(self, *args: Any, **kwargs: Any) -> WriteResult:
        raise AssertionError("record-at-a-time destination write must not be used")

    async def write_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[BatchWriteInput],
        options: WriteOptions,
    ) -> list[BatchWriteResult]:
        self.write_options.append(options)
        self.batch_writes.append([(r.trace_id, dict(r.properties)) for r in records])
        return [
            BatchWriteResult(
                trace_id=record.trace_id,
                status="created",
                destination_record_id=f"dest-{record.trace_id}",
            )
            for record in records
        ]


class InMemoryLedger:
    """Pair-keyed durable results with recorded calls, like the production fake."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], RecordWriteOutcome] = {}
        self.upsert_calls: list[list[RecordWriteOutcome]] = []
        self.terminal_reads: list[tuple[str, str, tuple[str, ...]]] = []

    def seed(self, outcome: RecordWriteOutcome) -> None:
        self.rows[(outcome.source_object, outcome.destination_object, outcome.source_record_id)] = (
            outcome
        )

    async def terminal_destination_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str | None]:
        self.terminal_reads.append((source_object, destination_object, tuple(source_record_ids)))
        return {
            source_id: row.destination_record_id
            for source_id in source_record_ids
            if (row := self.rows.get((source_object, destination_object, source_id))) is not None
            and row.status in TERMINAL_WRITE_STATUSES
        }

    async def upsert_results(self, results: Sequence[RecordWriteOutcome]) -> int:
        self.upsert_calls.append(list(results))
        for outcome in results:
            key = (outcome.source_object, outcome.destination_object, outcome.source_record_id)
            self.rows[key] = outcome
        return len(results)

    async def status_totals(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for row in self.rows.values():
            totals[row.status] = totals.get(row.status, 0) + 1
        return totals

    async def pair_status_totals(self) -> list[PairStatusTotal]:
        return []

    async def failed_source_ids_by_pair(self) -> list[PairFailedIds]:
        return []


class ShortLedger(InMemoryLedger):
    """Persists everything but reports one row short — the fail-closed trigger."""

    async def upsert_results(self, results: Sequence[RecordWriteOutcome]) -> int:
        saved = await super().upsert_results(results)
        return max(0, saved - 1)


class InMemoryIdentityLedger:
    def __init__(self, ids: dict[tuple[str, str, str], str] | None = None) -> None:
        self.ids = dict(ids or {})
        self.requests: list[tuple[str, str, tuple[str, ...]]] = []

    async def get_destination_record_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str]:
        self.requests.append((source_object, destination_object, tuple(source_record_ids)))
        return {
            source_id: self.ids[(source_object, source_id, destination_object)]
            for source_id in source_record_ids
            if (source_object, source_id, destination_object) in self.ids
        }


class SharedIdentityCandidates:
    def __init__(self, candidates: dict[tuple[str, str, str], list[str]]) -> None:
        self.candidates = dict(candidates)
        self.requests: list[tuple[str, str, tuple[str, ...]]] = []

    async def get_shared_destination_record_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, list[str]]:
        self.requests.append((source_object, destination_object, tuple(source_record_ids)))
        return {
            source_id: self.candidates[(source_object, source_id, destination_object)]
            for source_id in source_record_ids
            if (source_object, source_id, destination_object) in self.candidates
        }


class NullJournal:
    async def load(self) -> JournalEntry | None:
        return None

    async def save(self, entry: JournalEntry) -> SaveOutcome:
        return "saved"


class NullFence:
    async def claim(self, attempt: AttemptIdentity) -> tuple[ClaimOutcome, JournalEntry | None]:
        return "claimed", None


class RecordingObserver:
    def __init__(self) -> None:
        self.failed_records: list[tuple[str, str, str]] = []

    def record_failed(self, *, route_key: str, source_object: str, source_record_id: str) -> None:
        self.failed_records.append((route_key, source_object, source_record_id))

    def batch_completed(self, *, batches_completed: int, has_more: bool, stalled: bool) -> None:
        return None

    def route_count_failed(self, *, route_key: str) -> None:
        return None

    def attempt_fenced(self, *, attempt_id: str) -> None:
        return None


# -- helpers ------------------------------------------------------------------


def _host(
    ledger: InMemoryLedger | None = None,
    *,
    identity: InMemoryIdentityLedger | None = None,
    shared: SharedIdentityCandidates | None = None,
    observer: RecordingObserver | None = None,
) -> ExecutionHost:
    return ExecutionHost(
        ledger=ledger if ledger is not None else InMemoryLedger(),
        journal=NullJournal(),
        fence=NullFence(),
        identity_ledger=identity if identity is not None else InMemoryIdentityLedger(),
        shared_identity_ledger=shared,
        observer=observer if observer is not None else RecordingObserver(),
    )


def _policies(**overrides: Any) -> WritePolicies:
    values: dict[str, Any] = {"conflict_policy": "create", **overrides}
    return WritePolicies(**values)


def _name_route() -> list[ExecutionRoute]:
    return execution_routes(
        mapping_groups(
            [
                MigrationMappingField(
                    source_field="Account.Name", target_object="companies", target_field="name"
                )
            ]
        )
    )


def _routes(fields: list[MigrationMappingField]) -> list[ExecutionRoute]:
    return execution_routes(mapping_groups(fields))


async def _run(
    routes: Sequence[ExecutionRoute],
    source: FakeSource,
    destination: FakeDestination,
    *,
    host: ExecutionHost | None = None,
    snapshot: ExecutionSnapshot | None = None,
    policies: WritePolicies | None = None,
    batch_size: int = 200,
    route_high_water_marks: dict[str, str | None] | None = None,
    on_missing_identity: Any = "fail",
) -> tuple[ExecutionSnapshot, bool]:
    snapshot = snapshot if snapshot is not None else ExecutionSnapshot()
    has_more = await run_batch(
        routes=routes,
        source=source,
        source_credentials=_CREDENTIALS,
        destination=destination,
        destination_credentials=_CREDENTIALS,
        policies=policies if policies is not None else _policies(),
        batch_size=batch_size,
        snapshot=snapshot,
        host=host if host is not None else _host(),
        route_high_water_marks=route_high_water_marks,
        on_missing_identity=on_missing_identity,
    )
    return snapshot, has_more


# -- write phase --------------------------------------------------------------


async def test_run_batch_writes_a_mapped_record_and_completes_the_route() -> None:
    source = FakeSource()
    destination = FakeDestination()
    ledger = InMemoryLedger()

    snapshot, has_more = await _run(_name_route(), source, destination, host=_host(ledger))

    assert has_more is False
    assert snapshot.has_more is False
    assert destination.writes == [{"name": "Acme"}]
    assert snapshot.completed_routes == {"Account|companies"}
    assert snapshot.checkpoints == {}
    assert snapshot.route_counts == {
        "Account|companies": {"created": 1, "updated": 0, "skipped": 0}
    }
    row = ledger.rows[("Account", "companies", "001A")]
    assert row.status == "created"
    assert row.destination_record_id == "dest-1"


async def test_run_batch_saves_independent_checkpoints_for_filtered_routes() -> None:
    class PaginatedSource(FakeSource):
        async def read_records(self, *args: Any, **kwargs: Any) -> RecordPage:
            page = await super().read_records(*args, **kwargs)
            return RecordPage(
                object_key=page.object_key,
                records=page.records,
                next_cursor="001B",
                has_more=True,
            )

    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.Name",
                target_object="companies",
                target_field="name",
                source_filter=SourceFilter(field="IsPersonAccount", value=False),
            ),
            MigrationMappingField(
                source_field="Account.Name",
                target_object="contacts",
                target_field="firstname",
                source_filter=SourceFilter(field="IsPersonAccount", value=True),
            ),
        ]
    )
    source = PaginatedSource()

    snapshot, has_more = await _run(routes, source, FakeDestination(), batch_size=25)

    assert has_more is True
    assert len(snapshot.checkpoints) == 2
    assert set(snapshot.checkpoints.values()) == {"001B"}
    assert any("IsPersonAccount=equals:false" in key for key in snapshot.checkpoints)
    assert any("IsPersonAccount=equals:true" in key for key in snapshot.checkpoints)
    assert [request["source_filter"] for request in source.read_requests] == [
        SourceFilter(field="IsPersonAccount", value=False),
        SourceFilter(field="IsPersonAccount", value=True),
    ]
    assert snapshot.batch_pages[
        "Account|companies|IsPersonAccount=equals:false"
    ].source_record_ids == ("001A",)


async def test_run_batch_uses_destination_batch_write_and_persists_each_result() -> None:
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme"}, {"Id": "001B", "Name": "Beta"}])
    destination = BatchDestination()
    ledger = InMemoryLedger()

    snapshot, has_more = await _run(_name_route(), source, destination, host=_host(ledger))

    assert has_more is False
    assert destination.batch_writes == [[("001A", {"name": "Acme"}), ("001B", {"name": "Beta"})]]
    assert len(ledger.upsert_calls) == 1
    assert {source_id: row.status for (_, _, source_id), row in ledger.rows.items()} == {
        "001A": "created",
        "001B": "created",
    }
    assert ledger.rows[("Account", "companies", "001A")].destination_record_id == "dest-001A"
    assert ledger.rows[("Account", "companies", "001B")].destination_record_id == "dest-001B"
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_partial_failure_does_not_advance_past_failed_record() -> None:
    class PartiallyFailingDestination(BatchDestination):
        async def write_records(
            self,
            credentials: Credentials,
            *,
            object_type: str,
            records: list[BatchWriteInput],
            options: WriteOptions,
        ) -> list[BatchWriteResult]:
            return [
                BatchWriteResult(
                    trace_id=records[0].trace_id,
                    status="failed",
                    message="Provider rejected this row.",
                ),
                BatchWriteResult(
                    trace_id=records[1].trace_id,
                    status="created",
                    destination_record_id="dest-001B",
                ),
            ]

    source = FakeSource(records=[{"Id": "001A", "Name": "Acme"}, {"Id": "001B", "Name": "Beta"}])
    ledger = InMemoryLedger()
    observer = RecordingObserver()

    snapshot, has_more = await _run(
        _name_route(),
        source,
        PartiallyFailingDestination(),
        host=_host(ledger, observer=observer),
    )

    assert has_more is True
    assert snapshot.checkpoints == {}
    assert snapshot.completed_routes == set()
    assert snapshot.route_failed_record_ids == {"Account|companies": {"001A"}}
    assert snapshot.route_counts["Account|companies"] == {
        "created": 1,
        "updated": 0,
        "skipped": 0,
        "failed": 1,
    }
    assert ledger.rows[("Account", "companies", "001A")].status == "failed"
    assert ledger.rows[("Account", "companies", "001B")].status == "created"
    assert observer.failed_records == [("Account|companies", "Account", "001A")]


async def test_run_batch_recovers_a_previously_failed_record_once_it_writes() -> None:
    source = FakeSource()
    ledger = InMemoryLedger()
    snapshot = ExecutionSnapshot(
        route_counts={"Account|companies": {"created": 0, "updated": 0, "skipped": 0, "failed": 1}},
        route_failed_record_ids={"Account|companies": {"001A"}},
    )

    snapshot, has_more = await _run(
        _name_route(), source, FakeDestination(), host=_host(ledger), snapshot=snapshot
    )

    assert has_more is False
    assert snapshot.route_failed_record_ids == {"Account|companies": set()}
    assert snapshot.route_counts["Account|companies"] == {
        "created": 1,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
    }
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_does_not_rewrite_terminal_record_without_destination_id() -> None:
    ledger = InMemoryLedger()
    ledger.seed(
        RecordWriteOutcome(
            source_object="Account",
            source_record_id="001A",
            destination_object="companies",
            destination_record_id=None,
            status="skipped",
        )
    )
    destination = FakeDestination()

    snapshot, has_more = await _run(_name_route(), FakeSource(), destination, host=_host(ledger))

    assert has_more is False
    assert destination.writes == []
    assert ledger.upsert_calls == [[]]
    assert snapshot.completed_routes == {"Account|companies"}
    assert snapshot.route_counts == {
        "Account|companies": {"created": 0, "updated": 0, "skipped": 0}
    }


async def test_run_batch_fails_closed_when_bulk_result_persistence_is_incomplete() -> None:
    ledger = ShortLedger()

    with pytest.raises(ExecutionFault) as exc:
        await _run(_name_route(), FakeSource(), FakeDestination(), host=_host(ledger))

    assert exc.value.code == "SANKA_MIGRATE_RECORD_RESULT_BULK_SAVE_INCOMPLETE"
    assert exc.value.details == {"expectedCount": 1, "savedCount": 0}


async def test_run_batch_resolves_reference_mapping_before_destination_write() -> None:
    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.Name", target_object="companies", target_field="name"
            ),
            MigrationMappingField(
                source_field="Account.ParentId",
                source_reference_object="Account",
                target_object="companies",
                target_field="hs_product_id",
                target_reference_object="products",
                mapping_kind="reference",
                required=True,
            ),
        ]
    )
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme", "ParentId": "001P"}])
    destination = FakeDestination()
    identity = InMemoryIdentityLedger({("Account", "001P", "products"): "product-parent"})

    snapshot, has_more = await _run(routes, source, destination, host=_host(identity=identity))

    assert has_more is False
    assert destination.writes == [{"name": "Acme", "hs_product_id": "product-parent"}]
    assert identity.requests == [("Account", "products", ("001P",))]
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_fails_the_record_when_reference_parent_is_pending() -> None:
    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.Name", target_object="companies", target_field="name"
            ),
            MigrationMappingField(
                source_field="Account.ParentId",
                source_reference_object="Account",
                target_object="companies",
                target_field="hs_product_id",
                target_reference_object="products",
                mapping_kind="reference",
                required=True,
            ),
        ]
    )
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme", "ParentId": "001P"}])
    destination = FakeDestination()
    ledger = InMemoryLedger()

    snapshot, has_more = await _run(routes, source, destination, host=_host(ledger))

    assert has_more is True
    assert destination.writes == []
    row = ledger.rows[("Account", "companies", "001A")]
    assert row.status == "failed"
    assert row.message is not None
    assert "Referenced destination record has not been written yet." in row.message
    assert snapshot.route_failed_record_ids == {"Account|companies": {"001A"}}
    assert snapshot.completed_routes == set()


async def test_run_batch_stops_before_writes_for_ambiguous_shared_relationship() -> None:
    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.Name", target_object="companies", target_field="name"
            ),
            MigrationMappingField(
                source_field="Account.ParentId",
                source_reference_object="Account",
                target_object="companies",
                target_field="default",
                target_reference_object="companies",
                mapping_kind="relationship",
            ),
        ]
    )
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme", "ParentId": "001P"}])
    destination = FakeDestination()
    ledger = InMemoryLedger()
    shared = SharedIdentityCandidates(
        {("Account", "001P", "companies"): ["dest-parent-one", "dest-parent-two"]}
    )

    with pytest.raises(MappingError) as exc:
        await _run(routes, source, destination, host=_host(ledger, shared=shared))

    assert exc.value.code == "SANKA_MIGRATE_RELATIONSHIP_TARGET_AMBIGUOUS"
    assert destination.writes == []
    assert ledger.upsert_calls == []


async def test_run_batch_maps_owner_email_to_destination_owner_id() -> None:
    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.Name", target_object="companies", target_field="name"
            ),
            MigrationMappingField(
                source_field="Account.OwnerEmail",
                target_object="companies",
                target_field="hubspot_owner_id",
                mapping_kind="owner",
            ),
        ]
    )
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme", "OwnerEmail": "ada@example.com"}])
    destination = DestinationWithOwners()

    _snapshot, has_more = await _run(routes, source, destination)

    assert has_more is False
    assert destination.writes == [{"name": "Acme", "hubspot_owner_id": "owner-42"}]


async def test_run_batch_resolves_owner_id_through_the_source_directory() -> None:
    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.Name", target_object="companies", target_field="name"
            ),
            MigrationMappingField(
                source_field="Account.OwnerId",
                target_object="companies",
                target_field="hubspot_owner_id",
                mapping_kind="owner",
            ),
        ]
    )
    source = SourceWithOwners(
        records=[{"Id": "001A", "Name": "Acme", "OwnerId": "005A"}],
        owners=[OwnerProfile(id="005A", email="ada@example.com", active=True, name="Ada")],
    )
    destination = DestinationWithOwners()

    _snapshot, _has_more = await _run(routes, source, destination)

    assert destination.writes == [{"name": "Acme", "hubspot_owner_id": "owner-42"}]


async def test_run_batch_leaves_owner_empty_when_source_owner_is_unresolved() -> None:
    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.Name", target_object="companies", target_field="name"
            ),
            MigrationMappingField(
                source_field="Account.OwnerId",
                target_object="companies",
                target_field="hubspot_owner_id",
                mapping_kind="owner",
            ),
        ]
    )
    source = SourceWithOwners(
        records=[{"Id": "001A", "Name": "Acme", "OwnerId": "005-INACTIVE"}],
        owners=[],
    )
    destination = DestinationWithOwners()

    _snapshot, _has_more = await _run(
        routes, source, destination, policies=_policies(missing_owner_policy="leave_empty")
    )

    assert destination.writes == [{"name": "Acme"}]


async def test_run_batch_requires_owner_directory_support() -> None:
    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.OwnerEmail",
                target_object="companies",
                target_field="hubspot_owner_id",
                mapping_kind="owner",
            ),
        ]
    )

    with pytest.raises(ExecutionFault) as exc:
        await _run(routes, FakeSource(), FakeDestination())

    assert exc.value.code == "SANKA_MIGRATE_OWNER_MAPPING_UNSUPPORTED"


async def test_run_batch_passes_route_identity_fields_to_the_destination() -> None:
    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.Id",
                target_object="2-123",
                target_field="salesforce_record_id",
                identity=True,
            ),
            MigrationMappingField(
                source_field="Account.Name", target_object="2-123", target_field="name"
            ),
        ]
    )
    destination = FakeDestination()

    await _run(routes, FakeSource(), destination)

    assert [options.identity_fields for options in destination.write_options] == [
        ["salesforce_record_id"]
    ]


async def test_run_batch_skips_completed_routes_without_reading() -> None:
    source = FakeSource()
    snapshot = ExecutionSnapshot(completed_routes={"Account|companies"})

    snapshot, has_more = await _run(_name_route(), source, FakeDestination(), snapshot=snapshot)

    assert has_more is False
    assert source.read_requests == []
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_runs_only_the_selected_routes() -> None:
    fields = [
        MigrationMappingField(
            source_field="Account.Name", target_object="companies", target_field="name"
        ),
        MigrationMappingField(
            source_field="Contact.Name", target_object="contacts", target_field="lastname"
        ),
    ]
    routes = execution_routes(mapping_groups(fields), ["Account|companies"])
    source = FakeSource()

    snapshot, _has_more = await _run(routes, source, FakeDestination())

    assert [request["object_type"] for request in source.read_requests] == ["Account"]
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_reads_bounded_pages_inside_a_frozen_scope() -> None:
    source = BoundedSource()

    snapshot, _has_more = await _run(
        _name_route(),
        source,
        FakeDestination(),
        route_high_water_marks={"Account|companies": "001Z"},
    )

    assert source.read_requests == []
    assert source.bounded_requests == [
        {"object_type": "Account", "cursor": None, "upper_bound": "001Z"}
    ]
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_refuses_a_frozen_scope_without_bounded_reads() -> None:
    with pytest.raises(ExecutionFault) as exc:
        await _run(
            _name_route(),
            FakeSource(),
            FakeDestination(),
            route_high_water_marks={"Account|companies": "001Z"},
        )

    assert exc.value.code == "SANKA_MIGRATE_SOURCE_HIGH_WATER_MARK_UNSUPPORTED"


async def test_run_batch_completes_a_route_whose_frozen_mark_is_none() -> None:
    source = FakeSource()

    snapshot, has_more = await _run(
        _name_route(),
        source,
        FakeDestination(),
        route_high_water_marks={"Account|companies": None},
    )

    assert has_more is False
    assert source.read_requests == []
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_resumes_from_the_legacy_source_keyed_checkpoint() -> None:
    source = FakeSource()
    snapshot = ExecutionSnapshot(checkpoints={"Account": "0009"})

    await _run(_name_route(), source, FakeDestination(), snapshot=snapshot)

    assert source.read_requests[0]["cursor"] == "0009"


async def test_run_batch_ignores_the_legacy_checkpoint_for_filtered_routes() -> None:
    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.Name",
                target_object="companies",
                target_field="name",
                source_filter=SourceFilter(field="IsPersonAccount", value=False),
            )
        ]
    )
    source = FakeSource()
    snapshot = ExecutionSnapshot(checkpoints={"Account": "0009"})

    await _run(routes, source, FakeDestination(), snapshot=snapshot)

    assert source.read_requests[0]["cursor"] is None


async def test_run_batch_drops_records_without_identity_when_asked() -> None:
    source = FakeSource(records=[{"Id": "", "Name": "NoId"}, {"Id": "001B", "Name": "Beta"}])
    ledger = InMemoryLedger()

    snapshot, has_more = await _run(
        _name_route(), source, FakeDestination(), host=_host(ledger), on_missing_identity="drop"
    )

    assert has_more is False
    assert sorted(ledger.rows) == [("Account", "companies", "001B")]
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_fails_records_without_identity_by_default() -> None:
    source = FakeSource(records=[{"Id": "", "Name": "NoId"}, {"Id": "001B", "Name": "Beta"}])
    ledger = InMemoryLedger()

    snapshot, has_more = await _run(_name_route(), source, FakeDestination(), host=_host(ledger))

    assert has_more is True
    assert snapshot.checkpoints == {}
    assert ledger.rows[("Account", "companies", "missing-identity:0")].status == "failed"
    assert ledger.rows[("Account", "companies", "001B")].status == "created"
    assert snapshot.route_failed_record_ids == {"Account|companies": {"missing-identity:0"}}
    assert snapshot.completed_routes == set()


# -- relationship phase -------------------------------------------------------


def _relationship_fields() -> list[MigrationMappingField]:
    return [
        MigrationMappingField(
            source_field="Account.Name", target_object="companies", target_field="name"
        ),
        MigrationMappingField(
            source_field="Account.ParentId",
            source_reference_object="Account",
            target_object="companies",
            target_field="default",
            target_reference_object="companies",
            mapping_kind="relationship",
            association_category="USER_DEFINED",
            association_type_id=173,
        ),
    ]


def _parked(source_record_id: str, destination_record_id: str | None) -> dict[str, Any]:
    return pending_relationship(
        source_record_id=source_record_id,
        destination_object="companies",
        destination_record_id=destination_record_id,
        relationship_field="default",
        related_source_object="Account",
        related_source_record_id="001P",
        related_destination_object="companies",
        relationship_mode="default",
        association_category="USER_DEFINED",
        association_type_id=173,
    )


class BatchRelationshipDestination(BatchDestination):
    """Batch relationship writer; single association writes must stay unused."""

    def __init__(self) -> None:
        super().__init__()
        self.relationship_batches: list[list[RelationshipWrite]] = []

    async def write_relationship(self, *args: Any, **kwargs: Any) -> RelationshipWriteResult:
        raise AssertionError("record-at-a-time association write must not be used")

    async def write_relationships(
        self,
        credentials: Credentials,
        *,
        relationships: list[RelationshipWrite],
    ) -> list[BatchRelationshipWriteResult]:
        self.relationship_batches.append(list(relationships))
        return [
            BatchRelationshipWriteResult(trace_id=write.trace_id, status="linked")
            for write in relationships
        ]


class PendingRelationshipBatchDestination(BatchRelationshipDestination):
    async def write_relationships(
        self,
        credentials: Credentials,
        *,
        relationships: list[RelationshipWrite],
    ) -> list[BatchRelationshipWriteResult]:
        self.relationship_batches.append(list(relationships))
        return [
            BatchRelationshipWriteResult(
                trace_id=write.trace_id,
                status="failed",
                message="association unavailable",
            )
            for write in relationships
        ]


class FailingRelationshipDestination(FakeDestination):
    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        self.relationships.append(relationship)
        raise RuntimeError("association unavailable")


class RelationshipOnlyDestination(FakeDestination):
    """Association reconciliation must not read pages or write records."""

    def __init__(self) -> None:
        super().__init__()
        self.relationship_batches: list[list[RelationshipWrite]] = []

    async def write_record(self, *args: Any, **kwargs: Any) -> WriteResult:
        raise AssertionError("association reconciliation must not write scalar records")

    async def write_records(self, *args: Any, **kwargs: Any) -> list[BatchWriteResult]:
        raise AssertionError("association reconciliation must not write scalar batches")

    async def write_relationships(
        self,
        credentials: Credentials,
        *,
        relationships: list[RelationshipWrite],
    ) -> list[BatchRelationshipWriteResult]:
        self.relationship_batches.append(list(relationships))
        return [
            BatchRelationshipWriteResult(trace_id=write.trace_id, status="linked")
            for write in relationships
        ]


class RefusingSource(FakeSource):
    async def read_records(self, *args: Any, **kwargs: Any) -> RecordPage:
        raise AssertionError("association reconciliation must not read source pages")


async def test_run_batch_links_relationship_after_resolving_destination_ids() -> None:
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme", "ParentId": "001P"}])
    destination = FakeDestination()
    identity = InMemoryIdentityLedger({("Account", "001P", "companies"): "dest-parent"})

    snapshot, has_more = await _run(
        _routes(_relationship_fields()), source, destination, host=_host(identity=identity)
    )

    assert has_more is False
    assert destination.writes == [{"name": "Acme"}]
    assert len(destination.relationships) == 1
    write = destination.relationships[0]
    assert write.trace_id == "001A:0:0"
    assert write.object_type == "companies"
    assert write.record_id == "dest-1"
    assert write.relationship_field == "default"
    assert write.related_object_type == "companies"
    assert write.related_record_id == "dest-parent"
    assert write.relationship_mode == "default"
    assert write.association_category == "USER_DEFINED"
    assert write.association_type_id == 173
    assert identity.requests == [("Account", "companies", ("001P",))]
    assert snapshot.completed_routes == {"Account|companies"}
    assert snapshot.route_pending_relationships == {"Account|companies": {}}


async def test_run_batch_links_relationship_from_shared_run_candidates() -> None:
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme", "ParentId": "001P"}])
    destination = FakeDestination()
    shared = SharedIdentityCandidates({("Account", "001P", "companies"): ["dest-parent"]})

    _snapshot, _has_more = await _run(
        _routes(_relationship_fields()), source, destination, host=_host(shared=shared)
    )

    assert [write.related_record_id for write in destination.relationships] == ["dest-parent"]
    assert shared.requests == [("Account", "companies", ("001P",))]


async def test_run_batch_batches_relationships_and_preserves_record_checkpoint() -> None:
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme", "ParentId": "001P"}])
    destination = BatchRelationshipDestination()
    ledger = InMemoryLedger()
    identity = InMemoryIdentityLedger({("Account", "001P", "companies"): "dest-parent"})

    snapshot, has_more = await _run(
        _routes(_relationship_fields()), source, destination, host=_host(ledger, identity=identity)
    )

    assert has_more is False
    assert ledger.rows[("Account", "companies", "001A")].status == "created"
    assert len(destination.relationship_batches) == 1
    write = destination.relationship_batches[0][0]
    assert write.record_id == "dest-001A"
    assert write.related_record_id == "dest-parent"
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_advances_checkpoint_when_first_relationship_is_pending() -> None:
    class PagedSource(FakeSource):
        async def read_records(self, *args: Any, **kwargs: Any) -> RecordPage:
            page = await super().read_records(*args, **kwargs)
            return RecordPage(
                object_key=page.object_key,
                records=page.records,
                next_cursor="001A",
                has_more=True,
            )

    source = PagedSource(records=[{"Id": "001A", "Name": "Acme", "ParentId": "001P"}])
    destination = PendingRelationshipBatchDestination()
    ledger = InMemoryLedger()
    identity = InMemoryIdentityLedger({("Account", "001P", "companies"): "dest-parent"})

    snapshot, has_more = await _run(
        _routes(_relationship_fields()),
        source,
        destination,
        host=_host(ledger, identity=identity),
        batch_size=1,
    )

    assert has_more is True
    assert snapshot.checkpoints == {"Account|companies": "001A"}
    assert snapshot.route_failed_record_ids == {"Account|companies": set()}
    assert snapshot.route_pending_record_ids == {"Account|companies": {"001A"}}
    pending = list(snapshot.route_pending_relationships["Account|companies"].values())
    assert len(pending) == 1
    assert pending[0]["sourceRecordId"] == "001A"
    assert pending[0]["destinationRecordId"] == "dest-001A"
    assert pending[0]["relatedSourceRecordId"] == "001P"
    assert pending[0]["relatedDestinationObject"] == "companies"
    assert destination.batch_writes == [[("001A", {"name": "Acme"})]]
    row = ledger.rows[("Account", "companies", "001A")]
    assert row.status == "created"
    assert row.message is not None
    assert "Relationship pending: association unavailable" in row.message


async def test_run_batch_retries_parked_relationships_on_a_completed_route() -> None:
    parked = _parked("001A", "dest-001A")
    snapshot = ExecutionSnapshot(
        completed_routes={"Account|companies"},
        route_pending_relationships={
            "Account|companies": {pending_relationship_key(parked): parked}
        },
        route_pending_record_ids={"Account|companies": {"001A"}},
    )
    source = RefusingSource()
    destination = RelationshipOnlyDestination()
    identity = InMemoryIdentityLedger({("Account", "001P", "companies"): "dest-parent"})

    snapshot, has_more = await _run(
        _routes(_relationship_fields()),
        source,
        destination,
        host=_host(identity=identity),
        snapshot=snapshot,
    )

    assert has_more is False
    assert destination.writes == []
    assert len(destination.relationship_batches) == 1
    write = destination.relationship_batches[0][0]
    assert write.record_id == "dest-001A"
    assert write.related_record_id == "dest-parent"
    assert snapshot.route_pending_relationships == {"Account|companies": {}}
    assert snapshot.route_pending_record_ids == {"Account|companies": set()}
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_retries_parked_relationships_excluding_the_current_page() -> None:
    parked_on_page = _parked("001A", "dest-001A")
    parked_off_page = _parked("002B", "dest-002B")
    snapshot = ExecutionSnapshot(
        route_pending_relationships={
            "Account|companies": {
                pending_relationship_key(parked_on_page): parked_on_page,
                pending_relationship_key(parked_off_page): parked_off_page,
            }
        },
        route_pending_record_ids={"Account|companies": {"001A", "002B"}},
    )
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme", "ParentId": "001P"}])
    destination = FakeDestination()
    identity = InMemoryIdentityLedger({("Account", "001P", "companies"): "dest-parent"})

    snapshot, has_more = await _run(
        _routes(_relationship_fields()),
        source,
        destination,
        host=_host(identity=identity),
        snapshot=snapshot,
    )

    assert has_more is False
    # The off-page parked link retried first; the on-page record relinked
    # fresh after its rewrite, never through the parked entry.
    assert [write.record_id for write in destination.relationships] == ["dest-002B", "dest-1"]
    assert snapshot.route_pending_relationships == {"Account|companies": {}}
    assert snapshot.route_pending_record_ids == {"Account|companies": set()}
    assert snapshot.completed_routes == {"Account|companies"}


async def test_run_batch_parks_a_failed_relationship_and_keeps_the_scalar_write() -> None:
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme", "ParentId": "001P"}])
    destination = FailingRelationshipDestination()
    ledger = InMemoryLedger()
    identity = InMemoryIdentityLedger({("Account", "001P", "companies"): "dest-parent"})

    snapshot, has_more = await _run(
        _routes(_relationship_fields()), source, destination, host=_host(ledger, identity=identity)
    )

    assert has_more is False
    assert snapshot.completed_routes == {"Account|companies"}
    row = ledger.rows[("Account", "companies", "001A")]
    assert row.status == "created"
    assert row.message is not None
    assert "Relationship pending: association unavailable" in row.message
    assert snapshot.route_pending_record_ids == {"Account|companies": {"001A"}}
    pending = list(snapshot.route_pending_relationships["Account|companies"].values())
    assert len(pending) == 1
    assert pending[0]["sourceRecordId"] == "001A"
    assert pending[0]["destinationRecordId"] == "dest-1"
    # The failed link was attempted in the relationship phase and once more
    # by the end-of-route retry before staying parked.
    assert len(destination.relationships) == 2


async def test_run_batch_parks_a_relationship_when_the_write_returns_no_id() -> None:
    class NoIdDestination(FakeDestination):
        async def write_record(
            self,
            credentials: Credentials,
            *,
            object_type: str,
            properties: dict[str, Any],
            options: WriteOptions,
        ) -> WriteResult:
            self.writes.append(dict(properties))
            return WriteResult(status="created", destination_record_id=None)

    source = FakeSource(records=[{"Id": "001A", "Name": "Acme", "ParentId": "001P"}])
    destination = NoIdDestination()
    ledger = InMemoryLedger()
    identity = InMemoryIdentityLedger({("Account", "001P", "companies"): "dest-parent"})

    snapshot, has_more = await _run(
        _routes(_relationship_fields()), source, destination, host=_host(ledger, identity=identity)
    )

    assert has_more is False
    assert destination.relationships == []
    row = ledger.rows[("Account", "companies", "001A")]
    assert row.status == "created"
    assert row.message is not None
    assert "Relationship pending: Destination record id is missing." in row.message
    pending = list(snapshot.route_pending_relationships["Account|companies"].values())
    assert len(pending) == 1
    assert pending[0]["destinationRecordId"] is None
    assert snapshot.route_pending_record_ids == {"Account|companies": {"001A"}}


# -- repair and durable bookkeeping -------------------------------------------


async def test_run_batch_repairs_a_failed_write_committed_by_an_active_attempt() -> None:
    class FailingWriteDestination(FakeDestination):
        async def write_record(self, *args: Any, **kwargs: Any) -> WriteResult:
            raise RuntimeError("provider timeout")

    class ConcurrentTerminalLedger(InMemoryLedger):
        """Another active attempt commits 001A between the write and the re-read."""

        async def upsert_results(self, results: Sequence[RecordWriteOutcome]) -> int:
            count = await super().upsert_results(results)
            self.rows[("Account", "companies", "001A")] = RecordWriteOutcome(
                source_object="Account",
                source_record_id="001A",
                destination_object="companies",
                destination_record_id="dest-other-attempt",
                status="created",
            )
            return count

    ledger = ConcurrentTerminalLedger()
    observer = RecordingObserver()

    snapshot, has_more = await _run(
        _name_route(),
        FakeSource(),
        FailingWriteDestination(),
        host=_host(ledger, observer=observer),
    )

    assert has_more is False
    assert snapshot.completed_routes == {"Account|companies"}
    assert snapshot.route_counts["Account|companies"] == {
        "created": 0,
        "updated": 0,
        "skipped": 1,
    }
    assert snapshot.route_failed_record_ids == {"Account|companies": set()}
    assert observer.failed_records == []
    assert ledger.rows[("Account", "companies", "001A")].destination_record_id == (
        "dest-other-attempt"
    )
    # The re-read happened exactly once, and only because a write failed.
    assert ledger.terminal_reads == [
        ("Account", "companies", ("001A",)),
        ("Account", "companies", ("001A",)),
    ]


async def test_run_batch_uses_page_scoped_terminal_reads_and_bulk_persistence() -> None:
    source_records = [
        {"Id": f"001-{index:03d}", "Name": f"Company {index}"} for index in range(200)
    ]
    source = FakeSource(records=source_records)
    ledger = InMemoryLedger()

    _snapshot, has_more = await _run(_name_route(), source, BatchDestination(), host=_host(ledger))

    assert has_more is False
    assert ledger.terminal_reads == [
        ("Account", "companies", tuple(str(record["Id"]) for record in source_records))
    ]
    assert len(ledger.upsert_calls) == 1
    assert len(ledger.upsert_calls[0]) == 200
    assert ledger.upsert_calls[0][0].source_record_id == "001-000"


async def test_run_batch_keeps_failed_records_retryable() -> None:
    class AlwaysFailingDestination(FakeDestination):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def write_record(self, *args: Any, **kwargs: Any) -> WriteResult:
            self.attempts += 1
            raise RuntimeError("provider rejected the write")

    destination = AlwaysFailingDestination()
    ledger = InMemoryLedger()
    host = _host(ledger)
    snapshot = ExecutionSnapshot()

    snapshot, first_has_more = await _run(
        _name_route(), FakeSource(), destination, host=host, snapshot=snapshot
    )
    snapshot, second_has_more = await _run(
        _name_route(), FakeSource(), destination, host=host, snapshot=snapshot
    )

    assert first_has_more is True
    assert second_has_more is True
    assert destination.attempts == 2
    assert snapshot.route_counts["Account|companies"]["failed"] == 1
    assert snapshot.route_failed_record_ids == {"Account|companies": {"001A"}}
    assert snapshot.completed_routes == set()
