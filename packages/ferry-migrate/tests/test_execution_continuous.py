# SPDX-License-Identifier: AGPL-3.0-only
"""Continuous executor tests: the pinned production behaviors as OSS tests.

The named pinned behaviors ride here: stall -> reconcile -> resume, resume
beyond ten thousand batches, a fenced older attempt never writing, and the
terminal-page recheck before a stall becomes a failure. Around them sit the
mechanism tests for frozen-total route reopening, the durable result
rebuild discipline, progress/heartbeat assembly, and failure marking. No
test sleeps for real and none reads ambient time — the executor takes an
injectable ``sleep`` and ``clock``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from ferry.runtime.execution import (
    AttemptIdentity,
    BatchPage,
    ExecutionFault,
    ExecutionHost,
    ExecutionScope,
    ExecutionSnapshot,
    JournalEntry,
    PairFailedIds,
    PairStatusTotal,
    RecordWriteOutcome,
    SaveOutcome,
    apply_durable_route_results,
    dump_journal,
    durable_route_result_state,
    known_incomplete_route_keys,
    load_journal,
    normalized_attempt_identity,
    reconcile_terminal_batch_pages,
    reopen_incomplete_routes,
)
from ferry.runtime.execution.state import ClaimOutcome
from ferry.runtime.state import TERMINAL_WRITE_STATUSES

_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


# -- fakes --------------------------------------------------------------------


class InMemoryLedger:
    """Pair-keyed durable results with real summary/failed-id reads."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], RecordWriteOutcome] = {}
        self.terminal_reads: list[tuple[str, str, tuple[str, ...]]] = []

    def seed(self, *outcomes: RecordWriteOutcome) -> None:
        for outcome in outcomes:
            key = (outcome.source_object, outcome.destination_object, outcome.source_record_id)
            self.rows[key] = outcome

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
        self.seed(*results)
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
                source_object=source_object,
                destination_object=destination_object,
                status=status,
                count=count,
            )
            for (source_object, destination_object, status), count in sorted(counts.items())
        ]

    async def failed_source_ids_by_pair(self) -> list[PairFailedIds]:
        failed: dict[tuple[str, str], set[str]] = {}
        for row in self.rows.values():
            if row.status == "failed":
                pair = (row.source_object, row.destination_object)
                failed.setdefault(pair, set()).add(row.source_record_id)
        return [
            PairFailedIds(
                source_object=source_object,
                destination_object=destination_object,
                source_record_ids=frozenset(record_ids),
            )
            for (source_object, destination_object), record_ids in sorted(failed.items())
        ]


class InMemoryJournal:
    """Stores the codec-rendered report, round-tripping every load/save."""

    def __init__(self, entry: JournalEntry | None = None) -> None:
        self.report: dict[str, Any] | None = None
        self.saves: list[dict[str, Any]] = []
        self.save_outcomes: list[SaveOutcome] = []
        self.cancel_recorded = False
        self.stored_attempt_id: str | None = None
        if entry is not None:
            self.report = dump_journal(entry)
            self.stored_attempt_id = entry.attempt_id

    def record_cancellation(self) -> None:
        self.cancel_recorded = True
        if self.report is not None:
            execution = self.report.get("execution")
            execution = dict(execution) if isinstance(execution, dict) else {}
            execution["state"] = "cancelled"
            self.report = {**self.report, "status": "cancelled", "execution": execution}

    async def load(self) -> JournalEntry | None:
        if self.report is None:
            return None
        return load_journal(json.loads(json.dumps(self.report)))

    async def save(self, entry: JournalEntry) -> SaveOutcome:
        if self.save_outcomes:
            outcome = self.save_outcomes.pop(0)
            if outcome != "saved":
                return outcome
        if self.cancel_recorded and entry.status != "cancelled":
            entry.status = "cancelled"
            self.report = dump_journal(entry)
            self.saves.append(self.report)
            return "cancelled"
        if (
            self.stored_attempt_id is not None
            and entry.attempt_id is not None
            and entry.attempt_id != self.stored_attempt_id
        ):
            return "superseded"
        self.report = dump_journal(entry)
        self.saves.append(self.report)
        return "saved"


class ScriptedFence:
    def __init__(
        self,
        outcome: ClaimOutcome = "claimed",
        entry: JournalEntry | None = None,
        journal: InMemoryJournal | None = None,
    ) -> None:
        self.outcome = outcome
        self.entry = entry
        self.journal = journal
        self.claims: list[AttemptIdentity] = []

    async def claim(self, attempt: AttemptIdentity) -> tuple[ClaimOutcome, JournalEntry | None]:
        self.claims.append(attempt)
        if self.outcome != "claimed":
            return self.outcome, self.entry
        entry = self.entry
        if entry is None and self.journal is not None:
            entry = await self.journal.load()
        if entry is not None:
            entry.attempt_id = attempt.attempt_id
        if self.journal is not None:
            self.journal.stored_attempt_id = attempt.attempt_id
        return "claimed", entry


class RecordingObserver:
    def __init__(self) -> None:
        self.batches: list[tuple[int, bool, bool]] = []
        self.fenced_attempts: list[str] = []

    def record_failed(self, *, route_key: str, source_object: str, source_record_id: str) -> None:
        return None

    def batch_completed(self, *, batches_completed: int, has_more: bool, stalled: bool) -> None:
        self.batches.append((batches_completed, has_more, stalled))

    def route_count_failed(self, *, route_key: str) -> None:
        return None

    def attempt_fenced(self, *, attempt_id: str) -> None:
        self.fenced_attempts.append(attempt_id)


class NullIdentityLedger:
    async def get_destination_record_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str]:
        return {}


# -- helpers ------------------------------------------------------------------


_ACCOUNT_ROUTE = "Account|companies"
_CONTACT_ROUTE = "Contact|contacts"


def _manifest(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    rows = pairs or (("Account", "companies"),)
    return [
        {
            "routeKey": f"{source_object}|{destination_object}",
            "sourceObject": source_object,
            "destinationObject": destination_object,
            "sourceFilter": None,
        }
        for source_object, destination_object in rows
    ]


def _scope(
    *,
    manifest: list[dict[str, Any]] | None = None,
    route_totals: dict[str, int | None] | None = None,
    route_high_water_marks: dict[str, str | None] | None = None,
) -> ExecutionScope:
    rows = manifest if manifest is not None else _manifest()
    keys = [str(row["routeKey"]) for row in rows]
    return ExecutionScope(
        route_manifest=tuple(rows),
        selected_route_keys=tuple(keys),
        route_high_water_marks=dict(route_high_water_marks or {}),
        route_totals=dict(route_totals if route_totals is not None else dict.fromkeys(keys)),
    )


def _entry(
    *,
    status: str = "running",
    snapshot: ExecutionSnapshot | None = None,
    scope: ExecutionScope | None = None,
    job_id: str | None = "job-1",
    attempt_id: str | None = None,
    batches_completed: int = 0,
    started_at: str | None = None,
    resumed: bool = False,
    durable_results_attempt_id: str | None = None,
) -> JournalEntry:
    return JournalEntry(
        status=status,  # type: ignore[arg-type]
        snapshot=snapshot if snapshot is not None else ExecutionSnapshot(),
        job_id=job_id,
        attempt_id=attempt_id,
        batch_size=25,
        batches_completed=batches_completed,
        started_at=started_at,
        last_heartbeat_at=None,
        resumed=resumed,
        scope=scope,
        durable_results_attempt_id=durable_results_attempt_id,
    )


def _outcome(
    source_record_id: str,
    status: str = "created",
    *,
    source_object: str = "Account",
    destination_object: str = "companies",
    destination_record_id: str | None = "dest-1",
) -> RecordWriteOutcome:
    return RecordWriteOutcome(
        source_object=source_object,
        source_record_id=source_record_id,
        destination_object=destination_object,
        destination_record_id=destination_record_id if status != "failed" else None,
        status=status,  # type: ignore[arg-type]
        message=None,
    )


def _host(
    ledger: InMemoryLedger,
    journal: InMemoryJournal,
    fence: ScriptedFence,
    observer: RecordingObserver | None = None,
) -> ExecutionHost:
    return ExecutionHost(
        ledger=ledger,
        journal=journal,
        fence=fence,
        identity_ledger=NullIdentityLedger(),
        shared_identity_ledger=None,
        observer=observer if observer is not None else RecordingObserver(),
    )


# -- attempt identity ---------------------------------------------------------


def test_normalized_attempt_identity_defaults_derive_from_the_job() -> None:
    attempt = normalized_attempt_identity(job_id="job-9")
    assert attempt == AttemptIdentity(attempt_id="job-9:1", attempt_number=1, task_run_id="job-9")

    explicit = normalized_attempt_identity(
        job_id="job-9", attempt_id="task:3", attempt_number=3, task_run_id="task"
    )
    assert explicit.attempt_id == "task:3"
    assert explicit.task_run_id == "task"


# -- frozen-total route reopening ---------------------------------------------


def test_known_incomplete_route_keys_uses_frozen_totals() -> None:
    snapshot = ExecutionSnapshot(
        route_counts={
            _ACCOUNT_ROUTE: {"created": 3, "updated": 1, "skipped": 0, "failed": 2},
            _CONTACT_ROUTE: {"created": 5, "updated": 0, "skipped": 0, "failed": 0},
        }
    )
    entry = _entry(snapshot=snapshot)

    incomplete = known_incomplete_route_keys(
        entry,
        selected_route_keys=[_ACCOUNT_ROUTE, _CONTACT_ROUTE],
        route_totals={_ACCOUNT_ROUTE: 6, _CONTACT_ROUTE: 5},
    )

    # failed records do not count toward the frozen total; unknown totals never reopen
    assert incomplete == {_ACCOUNT_ROUTE}
    assert (
        known_incomplete_route_keys(
            entry,
            selected_route_keys=[_ACCOUNT_ROUTE],
            route_totals={_ACCOUNT_ROUTE: None},
        )
        == set()
    )


def test_known_incomplete_route_keys_falls_back_to_route_progress() -> None:
    entry = _entry()
    entry.route_progress = [
        {"routeKey": _ACCOUNT_ROUTE, "remaining": 4},
        {"routeKey": _CONTACT_ROUTE, "remaining": 0},
        {"routeKey": "Other|other", "remaining": 9},
        {"routeKey": _ACCOUNT_ROUTE + "-bad", "remaining": "nope"},
    ]

    incomplete = known_incomplete_route_keys(
        entry,
        selected_route_keys=[_ACCOUNT_ROUTE, _CONTACT_ROUTE],
    )

    assert incomplete == {_ACCOUNT_ROUTE}


def test_reopen_incomplete_routes_resumes_from_page_cursor_then_last_record_id() -> None:
    snapshot = ExecutionSnapshot(
        completed_routes={_ACCOUNT_ROUTE, _CONTACT_ROUTE},
        batch_pages={
            _ACCOUNT_ROUTE: BatchPage(
                source_record_ids=("001A", "001B"), next_cursor="cursor-b", has_more=False
            ),
            _CONTACT_ROUTE: BatchPage(
                source_record_ids=("003A", "003B"), next_cursor=None, has_more=False
            ),
        },
    )

    reopen_incomplete_routes(
        snapshot,
        route_keys={_ACCOUNT_ROUTE, _CONTACT_ROUTE},
        require_checkpoint=True,
    )

    assert snapshot.checkpoints == {_ACCOUNT_ROUTE: "cursor-b", _CONTACT_ROUTE: "003B"}
    assert snapshot.completed_routes == set()
    assert snapshot.has_more is True


def test_reopen_incomplete_routes_refuses_without_a_safe_checkpoint() -> None:
    snapshot = ExecutionSnapshot(completed_routes={_ACCOUNT_ROUTE})

    with pytest.raises(ExecutionFault) as fault:
        reopen_incomplete_routes(
            snapshot,
            route_keys={_ACCOUNT_ROUTE},
            require_checkpoint=True,
        )

    assert fault.value.code == "FERRY_SOURCE_CHECKPOINT_MISSING"
    # refused before mutating anything
    assert snapshot.checkpoints == {}
    assert snapshot.completed_routes == {_ACCOUNT_ROUTE}
    assert snapshot.has_more is False

    reopen_incomplete_routes(
        snapshot,
        route_keys={_ACCOUNT_ROUTE},
        require_checkpoint=False,
    )
    assert snapshot.checkpoints == {}
    assert snapshot.completed_routes == set()
    assert snapshot.has_more is True


# -- durable result rebuild ---------------------------------------------------


async def test_durable_route_result_state_rebuilds_unique_pair_routes_only() -> None:
    ledger = InMemoryLedger()
    ledger.seed(
        _outcome("001A", "created"),
        _outcome("001B", "updated"),
        _outcome("001C", "failed"),
        _outcome("003A", "created", source_object="Contact", destination_object="contacts"),
    )

    route_counts, failed_ids = await durable_route_result_state(
        ledger=ledger,
        route_keys_by_pair={
            ("Account", "companies"): [_ACCOUNT_ROUTE],
            ("Contact", "contacts"): ["Contact|contacts|a", "Contact|contacts|b"],
        },
    )

    assert route_counts == {_ACCOUNT_ROUTE: {"created": 1, "updated": 1, "skipped": 0, "failed": 1}}
    assert failed_ids == {_ACCOUNT_ROUTE: {"001C"}}


async def test_apply_durable_route_results_runs_once_per_attempt() -> None:
    ledger = InMemoryLedger()
    ledger.seed(_outcome("001A", "created"), _outcome("001B", "failed"))
    entry = _entry(
        snapshot=ExecutionSnapshot(
            route_counts={_ACCOUNT_ROUTE: {"created": 9, "updated": 0, "skipped": 0}}
        ),
        durable_results_attempt_id="job-1:1",
    )
    pairs = {("Account", "companies"): [_ACCOUNT_ROUTE]}

    # same attempt, counts present: the rebuild is not due
    rebuilt = await apply_durable_route_results(
        ledger=ledger, entry=entry, route_keys_by_pair=pairs, attempt_id="job-1:1"
    )
    assert rebuilt is False
    assert entry.snapshot.route_counts[_ACCOUNT_ROUTE]["created"] == 9

    # a new attempt resynchronizes from the durable ledger and stamps itself
    rebuilt = await apply_durable_route_results(
        ledger=ledger, entry=entry, route_keys_by_pair=pairs, attempt_id="job-1:2"
    )
    assert rebuilt is True
    assert entry.snapshot.route_counts[_ACCOUNT_ROUTE] == {
        "created": 1,
        "updated": 0,
        "skipped": 0,
        "failed": 1,
    }
    assert entry.snapshot.route_failed_record_ids[_ACCOUNT_ROUTE] == {"001B"}
    assert entry.durable_results_attempt_id == "job-1:2"


async def test_apply_durable_route_results_rebuilds_when_a_unique_route_has_no_counts() -> None:
    ledger = InMemoryLedger()
    ledger.seed(_outcome("001A", "created"))
    entry = _entry(durable_results_attempt_id="job-1:1")

    rebuilt = await apply_durable_route_results(
        ledger=ledger,
        entry=entry,
        route_keys_by_pair={("Account", "companies"): [_ACCOUNT_ROUTE]},
        attempt_id="job-1:1",
    )

    assert rebuilt is True
    assert entry.snapshot.route_counts[_ACCOUNT_ROUTE]["created"] == 1


# -- terminal-batch reconciliation --------------------------------------------


def _stalled_entry(
    *,
    page: BatchPage,
    checkpoint: str | None = "cursor-a",
) -> JournalEntry:
    snapshot = ExecutionSnapshot(
        checkpoints={_ACCOUNT_ROUTE: checkpoint} if checkpoint else {},
        route_counts={_ACCOUNT_ROUTE: {"created": 1, "updated": 0, "skipped": 0}},
        batch_pages={_ACCOUNT_ROUTE: page},
        has_more=True,
    )
    return _entry(snapshot=snapshot)


async def test_reconcile_advances_checkpoint_when_the_page_is_fully_terminal() -> None:
    ledger = InMemoryLedger()
    ledger.seed(_outcome("001A", "created"), _outcome("001B", "skipped"))
    entry = _stalled_entry(
        page=BatchPage(source_record_ids=("001A", "001B"), next_cursor="cursor-b", has_more=True)
    )

    reconciled = await reconcile_terminal_batch_pages(
        ledger=ledger,
        entry=entry,
        route_manifest=_manifest(),
        selected_route_keys=[_ACCOUNT_ROUTE],
    )

    assert reconciled is True
    assert entry.snapshot.checkpoints == {_ACCOUNT_ROUTE: "cursor-b"}
    assert entry.snapshot.has_more is True
    # unique-pair counts and aggregate counts rebuild from the durable ledger
    assert entry.snapshot.route_counts[_ACCOUNT_ROUTE] == {
        "created": 1,
        "updated": 0,
        "skipped": 1,
        "failed": 0,
    }
    assert entry.aggregate_counts == {"created": 1, "skipped": 1}


async def test_reconcile_completes_the_route_on_a_terminal_final_page() -> None:
    ledger = InMemoryLedger()
    ledger.seed(_outcome("001A", "created"), _outcome("001B", "updated"))
    entry = _stalled_entry(
        page=BatchPage(source_record_ids=("001A", "001B"), next_cursor=None, has_more=False)
    )

    reconciled = await reconcile_terminal_batch_pages(
        ledger=ledger,
        entry=entry,
        route_manifest=_manifest(),
        selected_route_keys=[_ACCOUNT_ROUTE],
    )

    assert reconciled is True
    assert entry.snapshot.checkpoints == {}
    assert entry.snapshot.completed_routes == {_ACCOUNT_ROUTE}
    assert entry.snapshot.has_more is False


async def test_reconcile_refuses_while_any_page_record_is_not_terminal() -> None:
    ledger = InMemoryLedger()
    ledger.seed(_outcome("001A", "created"))  # 001B has no durable row
    entry = _stalled_entry(
        page=BatchPage(source_record_ids=("001A", "001B"), next_cursor="cursor-b", has_more=True)
    )

    reconciled = await reconcile_terminal_batch_pages(
        ledger=ledger,
        entry=entry,
        route_manifest=_manifest(),
        selected_route_keys=[_ACCOUNT_ROUTE],
    )

    assert reconciled is False
    assert entry.snapshot.checkpoints == {_ACCOUNT_ROUTE: "cursor-a"}


async def test_reconcile_refuses_while_durable_failed_records_exist() -> None:
    """Terminal-page recheck: failed durable rows keep the stall for review."""

    ledger = InMemoryLedger()
    ledger.seed(_outcome("001A", "created"), _outcome("001B", "failed"))
    entry = _stalled_entry(
        page=BatchPage(source_record_ids=("001A", "001B"), next_cursor="cursor-b", has_more=True)
    )

    reconciled = await reconcile_terminal_batch_pages(
        ledger=ledger,
        entry=entry,
        route_manifest=_manifest(),
        selected_route_keys=[_ACCOUNT_ROUTE],
    )

    assert reconciled is False
    assert entry.snapshot.checkpoints == {_ACCOUNT_ROUTE: "cursor-a"}
    assert entry.snapshot.has_more is True


async def test_reconcile_refuses_on_an_empty_page_or_missing_cursor() -> None:
    ledger = InMemoryLedger()
    ledger.seed(_outcome("001A", "created"))

    empty_page = _stalled_entry(
        page=BatchPage(source_record_ids=(), next_cursor=None, has_more=False)
    )
    assert (
        await reconcile_terminal_batch_pages(
            ledger=ledger,
            entry=empty_page,
            route_manifest=_manifest(),
            selected_route_keys=[_ACCOUNT_ROUTE],
        )
        is False
    )

    more_without_cursor = _stalled_entry(
        page=BatchPage(source_record_ids=("001A",), next_cursor=None, has_more=True)
    )
    assert (
        await reconcile_terminal_batch_pages(
            ledger=ledger,
            entry=more_without_cursor,
            route_manifest=_manifest(),
            selected_route_keys=[_ACCOUNT_ROUTE],
        )
        is False
    )
    assert more_without_cursor.snapshot.checkpoints == {_ACCOUNT_ROUTE: "cursor-a"}
