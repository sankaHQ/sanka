# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from sanka.runtime.mapping import (
    MappingError,
    pending_relationship,
    pending_relationship_key,
    pending_relationship_source_ids,
    report_pending_relationships,
    resolve_destination_record_ids,
    retry_pending_relationships,
)
from sanka_data import (
    BatchRelationshipWriteResult,
    Credentials,
    Inventory,
    RelationshipWrite,
    RelationshipWriteResult,
    WriteOptions,
    WriteResult,
)
from sanka_data.records import BatchRelationshipStatus

_CREDENTIALS = Credentials(provider="stub-crm")


class Ledger:
    """In-memory run-scoped identity ledger."""

    def __init__(self, ids: dict[tuple[str, str, str], str]) -> None:
        self.ids = ids
        self.requests: list[tuple[str, tuple[str, ...], str]] = []

    async def get_destination_record_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str]:
        self.requests.append((source_object, tuple(source_record_ids), destination_object))
        return {
            source_id: self.ids[(source_object, source_id, destination_object)]
            for source_id in source_record_ids
            if (source_object, source_id, destination_object) in self.ids
        }


class SharedLedger:
    """In-memory cross-run candidate ledger."""

    def __init__(self, candidates: dict[str, list[str]]) -> None:
        self.candidates = candidates
        self.requests: list[tuple[str, tuple[str, ...], str]] = []

    async def get_shared_destination_record_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, list[str]]:
        self.requests.append((source_object, tuple(source_record_ids), destination_object))
        return {
            source_id: self.candidates[source_id]
            for source_id in source_record_ids
            if source_id in self.candidates
        }


class SingleWriteDestination:
    """Destination connector without the batch relationship capability."""

    provider = "stub-crm"
    binding_kind = "test"

    def __init__(self, *, fail_record_ids: set[str] | None = None) -> None:
        self.fail_record_ids = fail_record_ids or set()
        self.writes: list[RelationshipWrite] = []

    def automatic_target_object(self, canonical_type: str) -> str | None:
        return None

    async def inventory(
        self,
        credentials: Credentials,
        *,
        canonical_types: set[str],
    ) -> Inventory:
        raise AssertionError("inventory must not be called")

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        raise AssertionError("write_record must not be called")

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        if relationship.record_id in self.fail_record_ids:
            raise RuntimeError("provider rejected the relationship")
        self.writes.append(relationship)
        return RelationshipWriteResult(status="linked")


class BatchDestination(SingleWriteDestination):
    """Destination connector advertising batch relationship writes."""

    def __init__(
        self,
        *,
        fail_record_ids: set[str] | None = None,
        skip_record_ids: set[str] | None = None,
        raise_on_batch: bool = False,
    ) -> None:
        super().__init__()
        self.batch_fail_record_ids = fail_record_ids or set()
        self.skip_record_ids = skip_record_ids or set()
        self.raise_on_batch = raise_on_batch
        self.batches: list[list[RelationshipWrite]] = []

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        raise AssertionError("batch-capable destinations must not receive single writes")

    async def write_relationships(
        self,
        credentials: Credentials,
        *,
        relationships: list[RelationshipWrite],
    ) -> list[BatchRelationshipWriteResult]:
        if self.raise_on_batch:
            raise RuntimeError("provider batch endpoint failed")
        self.batches.append(relationships)
        results: list[BatchRelationshipWriteResult] = []
        for relationship in relationships:
            status: BatchRelationshipStatus = "linked"
            if relationship.record_id in self.batch_fail_record_ids:
                status = "failed"
            elif relationship.record_id in self.skip_record_ids:
                status = "skipped"
            results.append(
                BatchRelationshipWriteResult(trace_id=relationship.trace_id, status=status)
            )
        return results


def _pending(
    *,
    source_record_id: str = "001A",
    destination_record_id: str | None = "dest-001A",
    relationship_field: str = "parent_company",
    related_source_record_id: str = "001P",
) -> dict[str, Any]:
    return pending_relationship(
        source_record_id=source_record_id,
        destination_object="companies",
        destination_record_id=destination_record_id,
        relationship_field=relationship_field,
        related_source_object="Account",
        related_source_record_id=related_source_record_id,
        related_destination_object="companies",
        relationship_mode="default",
        association_category=None,
        association_type_id=None,
    )


def _keyed(*relationships: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {pending_relationship_key(relationship): relationship for relationship in relationships}


def test_pending_relationship_key_ignores_destination_record_id() -> None:
    parked = _pending(destination_record_id=None)
    written = _pending(destination_record_id="dest-001A")
    other_field = _pending(relationship_field="billing_company")

    assert pending_relationship_key(parked) == pending_relationship_key(written)
    assert pending_relationship_key(parked) != pending_relationship_key(other_field)


def test_report_pending_relationships_parses_and_skips_invalid_entries() -> None:
    valid = _pending()
    report = {
        "routePendingRelationships": {
            "Account|companies": [
                valid,
                dict(valid),  # duplicate collapses onto the same key
                {"sourceRecordId": "001B"},  # missing required keys
                "not-a-dict",
            ],
            "  ": [valid],
            "Account|contacts": "not-a-list",
        }
    }

    normalized = report_pending_relationships(report)

    assert list(normalized) == ["Account|companies"]
    route = normalized["Account|companies"]
    assert list(route.values()) == [valid]
    assert pending_relationship_source_ids(route) == {"001A"}


def test_report_pending_relationships_requires_route_payload() -> None:
    assert report_pending_relationships({}) == {}
    assert report_pending_relationships({"routePendingRelationships": []}) == {}


async def test_resolve_uses_run_scoped_ledger_first() -> None:
    ledger = Ledger({("Account", "001P", "companies"): "dest-001P"})
    shared = SharedLedger({"001P": ["other-dest"]})

    resolved = await resolve_destination_record_ids(
        ledger=ledger,
        shared_ledger=shared,
        source_object="Account",
        source_record_ids=["001P", "001P", ""],
        destination_object="companies",
    )

    assert resolved == {"001P": "dest-001P"}
    assert ledger.requests == [("Account", ("001P",), "companies")]
    assert shared.requests == []


async def test_resolve_falls_back_to_shared_ledger_for_missing_ids() -> None:
    ledger = Ledger({("Account", "001P", "companies"): "dest-001P"})
    shared = SharedLedger({"001Q": ["shared-001Q"]})

    resolved = await resolve_destination_record_ids(
        ledger=ledger,
        shared_ledger=shared,
        source_object="Account",
        source_record_ids=["001P", "001Q", "001R"],
        destination_object="companies",
    )

    assert resolved == {"001P": "dest-001P", "001Q": "shared-001Q"}
    assert shared.requests == [("Account", ("001Q", "001R"), "companies")]


async def test_resolve_without_shared_ledger_returns_partial_map() -> None:
    ledger = Ledger({("Account", "001P", "companies"): "dest-001P"})

    resolved = await resolve_destination_record_ids(
        ledger=ledger,
        source_object="Account",
        source_record_ids=["001P", "001Q"],
        destination_object="companies",
    )

    assert resolved == {"001P": "dest-001P"}


async def test_resolve_rejects_ambiguous_shared_candidates() -> None:
    ledger = Ledger({})
    shared = SharedLedger({"001Q": ["shared-1", "shared-2"]})

    with pytest.raises(MappingError) as exc_info:
        await resolve_destination_record_ids(
            ledger=ledger,
            shared_ledger=shared,
            source_object="Account",
            source_record_ids=["001Q"],
            destination_object="companies",
        )

    assert exc_info.value.code == "SANKA_MIGRATE_RELATIONSHIP_TARGET_AMBIGUOUS"
    assert exc_info.value.details == {
        "sourceReferenceObject": "Account",
        "targetReferenceObject": "companies",
        "ambiguousSourceCount": 1,
    }


async def test_retry_links_via_single_writes_and_keeps_failures() -> None:
    ledger = Ledger(
        {
            ("Account", "001P", "companies"): "dest-001P",
            ("Account", "001Q", "companies"): "dest-001Q",
        }
    )
    destination = SingleWriteDestination(fail_record_ids={"dest-001B"})
    linked = _pending(source_record_id="001A", destination_record_id="dest-001A")
    failing = _pending(
        source_record_id="001B",
        destination_record_id="dest-001B",
        related_source_record_id="001Q",
    )
    pending = _keyed(linked, failing)

    remaining = await retry_pending_relationships(
        ledger=ledger,
        destination=destination,
        credentials=_CREDENTIALS,
        pending_relationships=pending,
    )

    assert list(remaining.values()) == [failing]
    assert destination.writes == [
        RelationshipWrite(
            trace_id="pending:0",
            object_type="companies",
            record_id="dest-001A",
            relationship_field="parent_company",
            related_object_type="companies",
            related_record_id="dest-001P",
            relationship_mode="default",
            association_category=None,
            association_type_id=None,
        )
    ]


async def test_retry_uses_batch_capability_when_available() -> None:
    ledger = Ledger({("Account", "001P", "companies"): "dest-001P"})
    destination = BatchDestination(
        fail_record_ids={"dest-001B"},
        skip_record_ids={"dest-001C"},
    )
    linked = _pending(source_record_id="001A", destination_record_id="dest-001A")
    failing = _pending(source_record_id="001B", destination_record_id="dest-001B")
    skipped = _pending(source_record_id="001C", destination_record_id="dest-001C")
    pending = _keyed(linked, failing, skipped)

    remaining = await retry_pending_relationships(
        ledger=ledger,
        destination=destination,
        credentials=_CREDENTIALS,
        pending_relationships=pending,
    )

    assert list(remaining.values()) == [failing]
    assert [
        [relationship.record_id for relationship in batch] for batch in destination.batches
    ] == [["dest-001A", "dest-001B", "dest-001C"]]


async def test_retry_skips_excluded_and_unresolved_relationships() -> None:
    ledger = Ledger({("Account", "001P", "companies"): "dest-001P"})
    destination = SingleWriteDestination()
    excluded = _pending(source_record_id="001A", destination_record_id="dest-001A")
    parked = _pending(source_record_id="001B", destination_record_id=None)
    unresolved = _pending(
        source_record_id="001C",
        destination_record_id="dest-001C",
        related_source_record_id="001X",
    )
    pending = _keyed(excluded, parked, unresolved)

    remaining = await retry_pending_relationships(
        ledger=ledger,
        destination=destination,
        credentials=_CREDENTIALS,
        pending_relationships=pending,
        excluded_source_record_ids={"001A"},
    )

    assert remaining == pending
    assert destination.writes == []


async def test_retry_keeps_everything_when_batch_writer_fails() -> None:
    ledger = Ledger({("Account", "001P", "companies"): "dest-001P"})
    destination = BatchDestination(raise_on_batch=True)
    pending = _keyed(
        _pending(source_record_id="001A", destination_record_id="dest-001A"),
        _pending(source_record_id="001B", destination_record_id="dest-001B"),
    )

    remaining = await retry_pending_relationships(
        ledger=ledger,
        destination=destination,
        credentials=_CREDENTIALS,
        pending_relationships=pending,
    )

    assert remaining == pending
