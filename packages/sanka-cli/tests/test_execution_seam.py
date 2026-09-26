# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from sanka.runtime.execution import (
    NULL_OBSERVER,
    AttemptFence,
    AttemptIdentity,
    ClaimOutcome,
    ExecutionFault,
    ExecutionHost,
    ExecutionJournal,
    ExecutionLedger,
    ExecutionObserver,
    ExecutionRoute,
    ExecutionScope,
    ExecutionSnapshot,
    ExecutionStatus,
    JournalEntry,
    PairFailedIds,
    PairStatusTotal,
    RecordWriteOutcome,
    SaveOutcome,
    WriteStatus,
)
from sanka.runtime.mapping import (
    MigrationMappingField,
    mapping_group_key,
    resolve_destination_record_ids,
)
from sanka.runtime.state import TERMINAL_WRITE_STATUSES
from sanka_extensions.app import SourceFilter

_FIELD = MigrationMappingField(source_field="email", target_object="contacts", target_field="email")


# -- minimal in-memory fakes --------------------------------------------------


class InMemoryLedger:
    """Pair-keyed durable results, terminal-filtered like production."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], RecordWriteOutcome] = {}

    async def terminal_destination_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str | None]:
        return {
            source_id: row.destination_record_id
            for source_id in source_record_ids
            if (row := self.rows.get((source_object, destination_object, source_id))) is not None
            and row.status in TERMINAL_WRITE_STATUSES
        }

    async def upsert_results(self, results: Sequence[RecordWriteOutcome]) -> int:
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
        counts: dict[tuple[str, str, str], int] = {}
        for row in self.rows.values():
            key = (row.source_object, row.destination_object, row.status)
            counts[key] = counts.get(key, 0) + 1
        return [
            PairStatusTotal(
                source_object=source, destination_object=destination, status=status, count=count
            )
            for (source, destination, status), count in sorted(counts.items())
        ]

    async def failed_source_ids_by_pair(self) -> list[PairFailedIds]:
        failed: dict[tuple[str, str], set[str]] = {}
        for row in self.rows.values():
            if row.status == "failed":
                failed.setdefault((row.source_object, row.destination_object), set()).add(
                    row.source_record_id
                )
        return [
            PairFailedIds(
                source_object=source,
                destination_object=destination,
                source_record_ids=frozenset(ids),
            )
            for (source, destination), ids in sorted(failed.items())
        ]


class InMemoryJournal:
    def __init__(self, entry: JournalEntry | None = None) -> None:
        self.entry = entry
        self.saved: list[JournalEntry] = []

    async def load(self) -> JournalEntry | None:
        return self.entry

    async def save(self, entry: JournalEntry) -> SaveOutcome:
        self.entry = entry
        self.saved.append(entry)
        return "saved"


class InMemoryFence:
    def __init__(self, journal: InMemoryJournal) -> None:
        self.journal = journal
        self.claims: list[AttemptIdentity] = []

    async def claim(self, attempt: AttemptIdentity) -> tuple[ClaimOutcome, JournalEntry | None]:
        self.claims.append(attempt)
        return "claimed", self.journal.entry


class InMemoryIdentityLedger:
    """Satisfies sanka.runtime.mapping's run-scoped IdentityLedger protocol."""

    def __init__(self, ids: dict[tuple[str, str, str], str]) -> None:
        self.ids = ids

    async def get_destination_record_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str]:
        return {
            source_id: self.ids[(source_object, source_id, destination_object)]
            for source_id in source_record_ids
            if (source_object, source_id, destination_object) in self.ids
        }


# -- helpers ------------------------------------------------------------------


def _outcome(
    source_id: str,
    *,
    status: WriteStatus = "created",
    destination_id: str | None = "d-1",
) -> RecordWriteOutcome:
    return RecordWriteOutcome(
        source_object="contacts",
        source_record_id=source_id,
        destination_object="contacts",
        destination_record_id=destination_id,
        status=status,
    )


def _route() -> ExecutionRoute:
    return ExecutionRoute(
        route_key=mapping_group_key("contacts", "contacts", None),
        source_object="contacts",
        destination_object="contacts",
        source_filter=None,
        fields=[_FIELD],
    )


_MANIFEST_ROW: dict[str, Any] = {
    "routeKey": "contacts|contacts",
    "sourceObject": "contacts",
    "destinationObject": "contacts",
}


def _scope(
    *,
    manifest: tuple[Mapping[str, Any], ...] = (_MANIFEST_ROW,),
    selected: tuple[str, ...] = ("contacts|contacts",),
    high_water_marks: Mapping[str, str | None] | None = None,
    totals: Mapping[str, int | None] | None = None,
) -> ExecutionScope:
    return ExecutionScope(
        route_manifest=manifest,
        selected_route_keys=selected,
        route_high_water_marks=(
            {"contacts|contacts": "2026-08-01T00:00:00+00:00"}
            if high_water_marks is None
            else high_water_marks
        ),
        route_totals={"contacts|contacts": 42} if totals is None else totals,
    )


def _journal_entry(*, status: ExecutionStatus = "running") -> JournalEntry:
    return JournalEntry(
        status=status,
        snapshot=ExecutionSnapshot(),
        job_id="job-1",
        attempt_id="task-1:1",
        batch_size=200,
        batches_completed=3,
        started_at="2026-08-16T00:00:00+00:00",
        last_heartbeat_at="2026-08-16T00:05:00+00:00",
        resumed=False,
        scope=None,
        durable_results_attempt_id=None,
    )


# -- errors -------------------------------------------------------------------


def test_execution_fault_carries_code_and_copied_details() -> None:
    details = {"routeKey": "contacts|contacts"}
    fault = ExecutionFault(
        "route manifest changed", code="SANKA_MIGRATE_ROUTE_MANIFEST_CHANGED", details=details
    )
    assert isinstance(fault, RuntimeError)
    assert str(fault) == "route manifest changed"
    assert fault.message == "route manifest changed"
    assert fault.code == "SANKA_MIGRATE_ROUTE_MANIFEST_CHANGED"
    assert fault.details == {"routeKey": "contacts|contacts"}

    details["routeKey"] = "mutated"
    assert fault.details == {"routeKey": "contacts|contacts"}

    assert ExecutionFault("bare", code="SANKA_MIGRATE_EXECUTION_ROUTE_INVALID").details == {}


# -- model --------------------------------------------------------------------


def test_execution_route_reuses_sdk_and_mapping_types() -> None:
    source_filter = SourceFilter(field="is_active")
    route = ExecutionRoute(
        route_key=mapping_group_key("contacts", "contacts", source_filter),
        source_object="contacts",
        destination_object="contacts",
        source_filter=source_filter,
        fields=[_FIELD],
    )
    assert route.route_key == "contacts|contacts|is_active=equals:true"
    assert route.source_filter is source_filter
    assert route.fields[0].mapping_kind == "scalar"


def test_snapshot_defaults_are_independent_and_mutable() -> None:
    first = ExecutionSnapshot()
    second = ExecutionSnapshot()
    first.checkpoints["contacts|contacts"] = "cursor-1"
    first.completed_routes.add("contacts|contacts")
    first.route_counts.setdefault("contacts|contacts", {})["created"] = 1
    first.route_pending_record_ids.setdefault("contacts|contacts", set()).add("s-1")
    first.route_failed_record_ids.setdefault("contacts|contacts", set()).add("s-2")
    first.warnings.append("route count failed")
    first.has_more = True

    assert second.checkpoints == {}
    assert second.completed_routes == set()
    assert second.route_counts == {}
    assert second.route_pending_record_ids == {}
    assert second.route_pending_relationships == {}
    assert second.route_failed_record_ids == {}
    assert second.batch_pages == {}
    assert second.warnings == []
    assert second.has_more is False


def test_progress_marker_is_order_independent() -> None:
    first = ExecutionSnapshot(
        checkpoints={"b|b": "2", "a|a": "1"},
        completed_routes={"b|b", "a|a"},
    )
    second = ExecutionSnapshot(
        checkpoints={"a|a": "1", "b|b": "2"},
        completed_routes={"a|a", "b|b"},
    )
    assert first.progress_marker() == second.progress_marker()
    assert first.progress_marker() == ((("a|a", "1"), ("b|b", "2")), ("a|a", "b|b"))


def test_progress_marker_detects_checkpoint_and_completion_change() -> None:
    snapshot = ExecutionSnapshot(checkpoints={"a|a": "1"})
    stalled_marker = snapshot.progress_marker()

    snapshot.checkpoints["a|a"] = "2"
    advanced_marker = snapshot.progress_marker()
    assert advanced_marker != stalled_marker

    snapshot.completed_routes.add("a|a")
    assert snapshot.progress_marker() != advanced_marker


def test_scope_hash_is_deterministic_across_insertion_order() -> None:
    first = _scope(high_water_marks={"a|a": "2026-01-01", "b|b": None})
    second = _scope(high_water_marks={"b|b": None, "a|a": "2026-01-01"})
    assert first.scope_hash == second.scope_hash
    assert first.scope_hash.startswith("sha256:")
    assert first.scope_hash == first.scope_hash  # stable across property reads


def test_scope_hash_is_sensitive_to_manifest_selection_and_high_water_marks() -> None:
    base = _scope()
    changed_hwm = _scope(high_water_marks={"contacts|contacts": "2027-01-01T00:00:00+00:00"})
    changed_selection = _scope(selected=())
    reordered_selection = _scope(selected=("b|b", "a|a"))
    ordered_selection = _scope(selected=("a|a", "b|b"))
    changed_manifest = _scope(manifest=({**_MANIFEST_ROW, "sourceObject": "companies"},))

    assert base.scope_hash != changed_hwm.scope_hash
    assert base.scope_hash != changed_selection.scope_hash
    assert base.scope_hash != changed_manifest.scope_hash
    assert reordered_selection.scope_hash != ordered_selection.scope_hash


def test_scope_hash_ignores_route_totals() -> None:
    assert (
        _scope(totals={"contacts|contacts": 10}).scope_hash
        == _scope(totals={"contacts|contacts": 999_999}).scope_hash
    )


def test_journal_entry_defaults_are_independent() -> None:
    first = _journal_entry()
    second = _journal_entry()
    first.aggregate_counts["created"] = 1
    first.route_progress.append({"routeKey": "contacts|contacts"})
    first.extras["summary"] = "host copy"

    assert second.aggregate_counts == {}
    assert second.route_progress == []
    assert second.extras == {}
    assert second.provider_control is None


# -- protocol family ----------------------------------------------------------


async def test_ledger_round_trip_terminal_ids_and_summaries() -> None:
    ledger = InMemoryLedger()
    persisted = await ledger.upsert_results(
        [
            _outcome("s-1", status="created", destination_id="d-1"),
            _outcome("s-2", status="failed", destination_id=None),
            _outcome("s-3", status="skipped", destination_id="d-3"),
        ]
    )
    assert persisted == 3

    terminal = await ledger.terminal_destination_ids(
        source_object="contacts",
        source_record_ids=["s-1", "s-2", "s-3", "s-9"],
        destination_object="contacts",
    )
    assert terminal == {"s-1": "d-1", "s-3": "d-3"}

    assert await ledger.status_totals() == {"created": 1, "failed": 1, "skipped": 1}
    assert await ledger.pair_status_totals() == [
        PairStatusTotal(
            source_object="contacts", destination_object="contacts", status="created", count=1
        ),
        PairStatusTotal(
            source_object="contacts", destination_object="contacts", status="failed", count=1
        ),
        PairStatusTotal(
            source_object="contacts", destination_object="contacts", status="skipped", count=1
        ),
    ]
    assert await ledger.failed_source_ids_by_pair() == [
        PairFailedIds(
            source_object="contacts",
            destination_object="contacts",
            source_record_ids=frozenset({"s-2"}),
        )
    ]

    # Upsert-by-key: a failed record repaired to created leaves one row, now terminal.
    repaired = [_outcome("s-2", status="created", destination_id="d-2")]
    assert await ledger.upsert_results(repaired) == 1
    assert await ledger.status_totals() == {"created": 2, "skipped": 1}
    assert await ledger.failed_source_ids_by_pair() == []


async def test_journal_save_and_load_round_trip() -> None:
    journal = InMemoryJournal()
    assert await journal.load() is None
    entry = _journal_entry()
    assert await journal.save(entry) == "saved"
    assert await journal.load() is entry


async def test_attempt_fence_claims_and_returns_current_entry() -> None:
    entry = _journal_entry()
    journal = InMemoryJournal(entry)
    fence = InMemoryFence(journal)
    attempt = AttemptIdentity(attempt_id="task-1:2", attempt_number=2, task_run_id="task-1")

    outcome, claimed = await fence.claim(attempt)
    assert outcome == "claimed"
    assert claimed is entry
    assert fence.claims == [attempt]
    assert attempt.attempt_id == f"{attempt.task_run_id}:{attempt.attempt_number}"


def test_null_observer_is_a_silent_no_op() -> None:
    # Sync fire-and-forget: every event accepts its kwargs and does nothing.
    observer: ExecutionObserver = NULL_OBSERVER
    observer.record_failed(
        route_key="contacts|contacts", source_object="contacts", source_record_id="s-1"
    )
    observer.batch_completed(batches_completed=1, has_more=True, stalled=False)
    observer.route_count_failed(route_key="contacts|contacts")
    observer.attempt_fenced(attempt_id="task-1:1")


async def test_execution_host_bundles_protocols_with_mapping_ledgers() -> None:
    journal = InMemoryJournal()
    host = ExecutionHost(
        ledger=InMemoryLedger(),
        journal=journal,
        fence=InMemoryFence(journal),
        identity_ledger=InMemoryIdentityLedger({("contacts", "s-1", "contacts"): "d-1"}),
        shared_identity_ledger=None,
    )
    assert host.observer is NULL_OBSERVER
    assert isinstance(host.ledger, ExecutionLedger)
    assert isinstance(host.journal, ExecutionJournal)
    assert isinstance(host.fence, AttemptFence)

    resolved = await resolve_destination_record_ids(
        ledger=host.identity_ledger,
        shared_ledger=host.shared_identity_ledger,
        source_object="contacts",
        source_record_ids=["s-1", "s-2"],
        destination_object="contacts",
    )
    assert resolved == {"s-1": "d-1"}

    with pytest.raises(dataclasses.FrozenInstanceError):
        host.observer = NULL_OBSERVER  # type: ignore[misc]
