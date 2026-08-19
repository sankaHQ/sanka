# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-ID pilot scope tests.

The safety runbook's exact-ID pilot ("an exact saved ID set and its hash")
as engine capability: the scope builder canonicalizes and hashes candidate
sets and refuses drifted or mis-enumerated ones; ``run_batch`` verifies the
candidate hash before touching anything, intersects every source page with
the candidate set, and refuses route completion until the execution ledger
covers every candidate id.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from sanka.connector import (
    Credentials,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    SourceFilter,
    SourceObject,
    WriteOptions,
    WriteResult,
)
from sanka.runtime.execution import (
    EXACT_CANDIDATE_HASH_MISMATCH_CODE,
    EXACT_SCOPE_COVERAGE_WARNING,
    ExactIdScope,
    ExecutionFault,
    ExecutionHost,
    ExecutionScope,
    ExecutionSnapshot,
    JournalEntry,
    PairFailedIds,
    PairStatusTotal,
    RecordWriteOutcome,
    SaveOutcome,
    WritePolicies,
    exact_candidate_hash,
    exact_id_scope,
    execution_routes,
    run_batch,
)
from sanka.runtime.execution.model import AttemptIdentity
from sanka.runtime.execution.state import ClaimOutcome
from sanka.runtime.mapping import MigrationMappingField, mapping_groups
from sanka.runtime.mapping.record_mapping import MappingGroup
from sanka.runtime.state import TERMINAL_WRITE_STATUSES

_CREDENTIALS = Credentials(provider="fake")


def _two_route_groups() -> list[MappingGroup]:
    return mapping_groups(
        [
            MigrationMappingField(
                source_field="Account.Name", target_object="companies", target_field="name"
            ),
            MigrationMappingField(
                source_field="Contact.LastName", target_object="contacts", target_field="lastname"
            ),
        ]
    )


# -- builder canonicalization -------------------------------------------------


def test_exact_id_scope_freezes_canonical_candidates_and_totals() -> None:
    scope = exact_id_scope(
        groups=_two_route_groups(),
        candidate_ids_by_route={
            "Contact|contacts": ["003B", " 003A ", "003B", ""],
            "Account|companies": ["001A"],
        },
    )

    assert isinstance(scope, ExecutionScope)  # a tagged variant, not a sibling
    assert scope.selected_route_keys == ("Account|companies", "Contact|contacts")
    assert scope.candidate_ids_by_route == {
        "Account|companies": ("001A",),
        "Contact|contacts": ("003A", "003B"),
    }
    assert scope.route_totals == {"Account|companies": 1, "Contact|contacts": 2}
    assert scope.route_high_water_marks == {}
    assert scope.candidate_hash.startswith("sha256:")
    assert scope.scope_hash.startswith("sha256:")
    scope.verify_candidate_hash()  # a builder-made scope always verifies


def test_exact_candidate_hash_is_order_insensitive_and_content_sensitive() -> None:
    baseline = exact_candidate_hash({"a|b": ["2", "1"], "c|d": ["9"]})

    assert baseline == exact_candidate_hash({"c|d": ["9"], "a|b": ["1", "2", " 1 "]})
    assert baseline != exact_candidate_hash({"a|b": ["2", "1"], "c|d": ["8"]})
    assert baseline != exact_candidate_hash({"a|b": ["2", "1"]})


def test_exact_id_scope_verifies_an_expected_candidate_hash() -> None:
    approved = exact_candidate_hash({"Account|companies": ["001A", "001B"]})

    scope = exact_id_scope(
        groups=_two_route_groups(),
        candidate_ids_by_route={"Account|companies": ["001B", "001A"]},
        expected_candidate_hash=approved,
    )
    assert scope.candidate_hash == approved
    assert scope.selected_route_keys == ("Account|companies",)

    with pytest.raises(ExecutionFault) as excinfo:
        exact_id_scope(
            groups=_two_route_groups(),
            candidate_ids_by_route={"Account|companies": ["001A", "001C"]},
            expected_candidate_hash=approved,
        )
    assert excinfo.value.code == EXACT_CANDIDATE_HASH_MISMATCH_CODE
    assert excinfo.value.details["expectedCandidateHash"] == approved
    assert excinfo.value.details["candidateHash"] != approved


# -- builder refusals ---------------------------------------------------------


def test_exact_id_scope_requires_candidates_for_exactly_the_selected_routes() -> None:
    with pytest.raises(ExecutionFault) as missing:
        exact_id_scope(
            groups=_two_route_groups(),
            candidate_ids_by_route={"Account|companies": ["001A"]},
            requested_route_keys=["Account|companies", "Contact|contacts"],
        )
    assert missing.value.code == "SANKA_MIGRATE_EXECUTION_ROUTE_INVALID"
    assert missing.value.details == {"routeKeysWithoutCandidates": ["Contact|contacts"]}

    with pytest.raises(ExecutionFault) as extra:
        exact_id_scope(
            groups=_two_route_groups(),
            candidate_ids_by_route={
                "Account|companies": ["001A"],
                "Contact|contacts": ["003A"],
            },
            requested_route_keys=["Account|companies"],
        )
    assert extra.value.code == "SANKA_MIGRATE_EXECUTION_ROUTE_INVALID"
    assert extra.value.details == {"unselectedCandidateRouteKeys": ["Contact|contacts"]}


def test_exact_id_scope_refuses_routes_outside_the_reviewed_mapping() -> None:
    with pytest.raises(ExecutionFault) as excinfo:
        exact_id_scope(
            groups=_two_route_groups(),
            candidate_ids_by_route={"Lead|leads": ["00QA"]},
        )
    assert excinfo.value.code == "SANKA_MIGRATE_EXECUTION_ROUTE_INVALID"
    assert excinfo.value.details == {"unknownRouteKeys": ["Lead|leads"]}


def test_exact_id_scope_refuses_empty_maps_and_string_candidates() -> None:
    with pytest.raises(ExecutionFault) as empty:
        exact_id_scope(groups=_two_route_groups(), candidate_ids_by_route={})
    assert empty.value.code == "SANKA_MIGRATE_EXECUTION_ROUTE_INVALID"

    with pytest.raises(ExecutionFault) as stringly:
        exact_id_scope(
            groups=_two_route_groups(),
            candidate_ids_by_route={"Account|companies": "001A"},
        )
    assert stringly.value.code == "SANKA_MIGRATE_EXECUTION_ROUTE_INVALID"
    assert stringly.value.details == {"routeKey": "Account|companies"}


def test_verify_candidate_hash_refuses_a_drifted_hand_built_scope() -> None:
    built = exact_id_scope(
        groups=_two_route_groups(),
        candidate_ids_by_route={"Account|companies": ["001A"]},
    )
    tampered = ExactIdScope(
        route_manifest=built.route_manifest,
        selected_route_keys=built.selected_route_keys,
        route_high_water_marks=built.route_high_water_marks,
        route_totals=built.route_totals,
        candidate_ids_by_route={"Account|companies": ("001A", "001Z")},
        candidate_hash=built.candidate_hash,
    )

    with pytest.raises(ExecutionFault) as excinfo:
        tampered.verify_candidate_hash()

    assert excinfo.value.code == EXACT_CANDIDATE_HASH_MISMATCH_CODE
    assert excinfo.value.details["candidateHash"] == built.candidate_hash


# -- run_batch intersection ---------------------------------------------------


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
        self.read_requests.append({"object_type": object_type, "cursor": cursor})
        return RecordPage(object_key=object_type, records=[dict(r) for r in self.records])


class FakeDestination:
    provider = "fake-destination"
    binding_kind = "api_token"

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
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
        self._write_counter += 1
        return WriteResult(status="created", destination_record_id=f"dest-{self._write_counter}")

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        return RelationshipWriteResult(status="linked")


class InMemoryLedger:
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


class InMemoryIdentityLedger:
    async def get_destination_record_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str]:
        return {}


class NullJournal:
    async def load(self) -> JournalEntry | None:
        return None

    async def save(self, entry: JournalEntry) -> SaveOutcome:
        return "saved"


class NullFence:
    async def claim(self, attempt: AttemptIdentity) -> tuple[ClaimOutcome, JournalEntry | None]:
        return "claimed", None


def _host(ledger: InMemoryLedger | None = None) -> ExecutionHost:
    return ExecutionHost(
        ledger=ledger if ledger is not None else InMemoryLedger(),
        journal=NullJournal(),
        fence=NullFence(),
        identity_ledger=InMemoryIdentityLedger(),
        shared_identity_ledger=None,
    )


def _account_groups() -> list[MappingGroup]:
    return mapping_groups(
        [
            MigrationMappingField(
                source_field="Account.Name", target_object="companies", target_field="name"
            )
        ]
    )


def _account_scope(candidate_ids: list[str]) -> ExactIdScope:
    return exact_id_scope(
        groups=_account_groups(),
        candidate_ids_by_route={"Account|companies": candidate_ids},
    )


async def _run(
    source: FakeSource,
    destination: FakeDestination,
    *,
    exact_scope: ExactIdScope | None,
    host: ExecutionHost | None = None,
    snapshot: ExecutionSnapshot | None = None,
    route_high_water_marks: dict[str, str | None] | None = None,
) -> tuple[ExecutionSnapshot, bool]:
    snapshot = snapshot if snapshot is not None else ExecutionSnapshot()
    has_more = await run_batch(
        routes=execution_routes(_account_groups()),
        source=source,
        source_credentials=_CREDENTIALS,
        destination=destination,
        destination_credentials=_CREDENTIALS,
        policies=WritePolicies(conflict_policy="create"),
        batch_size=200,
        snapshot=snapshot,
        host=host if host is not None else _host(),
        route_high_water_marks=route_high_water_marks,
        exact_scope=exact_scope,
    )
    return snapshot, has_more


async def test_run_batch_intersects_every_page_with_the_candidate_set() -> None:
    source = FakeSource(
        records=[
            {"Id": "001A", "Name": "Acme"},
            {"Id": "001B", "Name": "Beta"},
            {"Id": "001C", "Name": "Gamma"},
        ]
    )
    destination = FakeDestination()
    ledger = InMemoryLedger()
    scope = _account_scope(["001A", "001C"])

    snapshot, has_more = await _run(source, destination, exact_scope=scope, host=_host(ledger))

    assert has_more is False
    assert destination.writes == [{"name": "Acme"}, {"name": "Gamma"}]
    assert {key[2] for key in ledger.rows} == {"001A", "001C"}  # 001B never reached the ledger
    assert snapshot.route_counts == {
        "Account|companies": {"created": 2, "updated": 0, "skipped": 0}
    }
    assert snapshot.batch_pages["Account|companies"].source_record_ids == ("001A", "001C")
    assert snapshot.completed_routes == {"Account|companies"}
    assert snapshot.warnings == []


async def test_run_batch_refuses_a_drifted_candidate_hash_before_touching_anything() -> None:
    built = _account_scope(["001A"])
    drifted = ExactIdScope(
        route_manifest=built.route_manifest,
        selected_route_keys=built.selected_route_keys,
        route_high_water_marks=built.route_high_water_marks,
        route_totals=built.route_totals,
        candidate_ids_by_route={"Account|companies": ("001A", "001B")},
        candidate_hash=built.candidate_hash,
    )
    source = FakeSource()
    destination = FakeDestination()
    ledger = InMemoryLedger()

    with pytest.raises(ExecutionFault) as excinfo:
        await _run(source, destination, exact_scope=drifted, host=_host(ledger))

    assert excinfo.value.code == EXACT_CANDIDATE_HASH_MISMATCH_CODE
    assert source.read_requests == []
    assert destination.writes == []
    assert ledger.upsert_calls == []


async def test_run_batch_refuses_a_route_outside_the_exact_scope() -> None:
    contact_scope = exact_id_scope(
        groups=mapping_groups(
            [
                MigrationMappingField(
                    source_field="Contact.LastName",
                    target_object="contacts",
                    target_field="lastname",
                )
            ]
        ),
        candidate_ids_by_route={"Contact|contacts": ["003A"]},
    )
    source = FakeSource()
    destination = FakeDestination()

    with pytest.raises(ExecutionFault) as excinfo:
        await _run(source, destination, exact_scope=contact_scope)

    assert excinfo.value.code == "SANKA_MIGRATE_EXECUTION_ROUTE_INVALID"
    assert excinfo.value.details == {"routeKey": "Account|companies"}
    assert source.read_requests == []
    assert destination.writes == []


async def test_run_batch_refuses_completion_until_the_ledger_covers_every_candidate() -> None:
    def source() -> FakeSource:
        return FakeSource(records=[{"Id": "001A", "Name": "Acme"}, {"Id": "001B", "Name": "Beta"}])

    destination = FakeDestination()
    ledger = InMemoryLedger()
    scope = _account_scope(["001A", "001B", "001-GHOST"])
    assert scope.route_totals == {"Account|companies": 3}  # reconciliation identity

    snapshot, has_more = await _run(source(), destination, exact_scope=scope, host=_host(ledger))

    expected_warning = EXACT_SCOPE_COVERAGE_WARNING.format(
        route_key="Account|companies", uncovered_count=1, candidate_count=3
    )
    assert has_more is True
    assert snapshot.completed_routes == set()
    assert snapshot.checkpoints == {}
    assert snapshot.warnings == [expected_warning]
    assert destination.writes == [{"name": "Acme"}, {"name": "Beta"}]

    # A second refused batch replaces its warning instead of accumulating.
    snapshot, has_more = await _run(
        source(), destination, exact_scope=scope, host=_host(ledger), snapshot=snapshot
    )
    assert has_more is True
    assert snapshot.warnings == [expected_warning]
    assert destination.writes == [{"name": "Acme"}, {"name": "Beta"}]  # terminal skip, no rewrite

    # Once the ledger covers the missing candidate, the route may complete.
    ledger.seed(
        RecordWriteOutcome(
            source_object="Account",
            source_record_id="001-GHOST",
            destination_object="companies",
            destination_record_id="dest-ghost",
            status="created",
        )
    )
    snapshot, has_more = await _run(
        source(), destination, exact_scope=scope, host=_host(ledger), snapshot=snapshot
    )
    assert has_more is False
    assert snapshot.completed_routes == {"Account|companies"}
    assert snapshot.warnings == []
    assert snapshot.route_counts == {
        "Account|companies": {"created": 2, "updated": 0, "skipped": 0}
    }


async def test_run_batch_completes_an_empty_candidate_set_without_reading() -> None:
    source = FakeSource()
    destination = FakeDestination()

    snapshot, has_more = await _run(source, destination, exact_scope=_account_scope([]))

    assert has_more is False
    assert snapshot.completed_routes == {"Account|companies"}
    assert source.read_requests == []
    assert destination.writes == []


async def test_exact_scope_overrides_a_frozen_none_high_water_mark() -> None:
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme"}])
    destination = FakeDestination()
    ledger = InMemoryLedger()

    snapshot, has_more = await _run(
        source,
        destination,
        exact_scope=_account_scope(["001A"]),
        host=_host(ledger),
        route_high_water_marks={"Account|companies": None},
    )

    assert has_more is False
    assert source.read_requests != []  # the candidate set, not the mark, is the authority
    assert destination.writes == [{"name": "Acme"}]
    assert snapshot.completed_routes == {"Account|companies"}


async def test_records_without_identity_are_outside_any_exact_scope() -> None:
    source = FakeSource(records=[{"Name": "NoIdentity"}, {"Id": "001A", "Name": "Acme"}])
    destination = FakeDestination()
    ledger = InMemoryLedger()

    snapshot, has_more = await _run(
        source, destination, exact_scope=_account_scope(["001A"]), host=_host(ledger)
    )

    # on_missing_identity="fail" never fires: an identity-less record can
    # never be a candidate, so it is filtered, not failed.
    assert has_more is False
    assert destination.writes == [{"name": "Acme"}]
    assert snapshot.route_counts == {
        "Account|companies": {"created": 1, "updated": 0, "skipped": 0}
    }
    assert snapshot.route_failed_record_ids == {"Account|companies": set()}
