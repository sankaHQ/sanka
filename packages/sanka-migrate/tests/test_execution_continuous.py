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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sanka.runtime.execution import (
    BATCH_SAFETY_LIMIT_MESSAGE,
    FROZEN_TOTAL_SHORTFALL_WARNING,
    STALLED_NO_PROGRESS_WARNING,
    AttemptIdentity,
    BatchPage,
    ContinuousExecutor,
    ExecutionFault,
    ExecutionHost,
    ExecutionScope,
    ExecutionSnapshot,
    JournalEntry,
    PairFailedIds,
    PairStatusTotal,
    RecordWriteOutcome,
    SaveOutcome,
    SqliteExecutionState,
    apply_durable_route_results,
    assemble_continuous_report,
    dump_journal,
    durable_route_result_state,
    known_incomplete_route_keys,
    load_journal,
    mark_execution_failed,
    normalized_attempt_identity,
    reconcile_terminal_batch_pages,
    reopen_incomplete_routes,
)
from sanka.runtime.execution.state import ClaimOutcome, ExecutionJournal
from sanka.runtime.state import TERMINAL_WRITE_STATUSES

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
            if entry is not None:
                # the production claim persists the attempt onto the stage
                self.journal.report = dump_journal(entry)
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

    assert fault.value.code == "SANKA_MIGRATE_SOURCE_CHECKPOINT_MISSING"
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


# -- executor harness ---------------------------------------------------------


class FakeClock:
    """Deterministic clock advancing a fixed tick per reading."""

    def __init__(self, start: datetime = _NOW, tick_seconds: float = 0.0) -> None:
        self.now = start
        self.tick = timedelta(seconds=tick_seconds)
        self.readings = 0

    def __call__(self) -> datetime:
        current = self.now
        self.now = self.now + self.tick
        self.readings += 1
        return current


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class ScriptedStep:
    """Replays scripted batch results, saving each through the journal.

    Each script item is ``(mutate, has_more)`` where ``mutate`` shapes this
    batch's snapshot/entry. The step saves the entry it returns — the OSS
    ``run_batch`` + ``journal.save`` composition, and the production
    delegated-execute cadence.
    """

    def __init__(
        self,
        journal: ExecutionJournal,
        script: list[tuple[Any, bool]],
        *,
        job_id: str | None = "job-1",
        attempt_id: str | None = "job-1:1",
    ) -> None:
        self.journal = journal
        self.script = list(script)
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.calls = 0

    async def __call__(self) -> tuple[JournalEntry, bool]:
        self.calls += 1
        entry = await self.journal.load()
        if entry is None:
            entry = _entry(job_id=self.job_id, attempt_id=self.attempt_id)
        if entry.attempt_id is None:
            entry.attempt_id = self.attempt_id
        mutate, has_more = self.script.pop(0)
        mutate(entry)
        entry.snapshot.has_more = has_more
        entry.status = "running" if has_more else "completed"
        await self.journal.save(entry)
        return entry, has_more


def _executor(
    host: ExecutionHost,
    *,
    max_batches: int = 50,
    clock: FakeClock | None = None,
    sleep: FakeSleep | None = None,
    provider_control: Any = None,
) -> tuple[ContinuousExecutor, FakeSleep]:
    pause = sleep if sleep is not None else FakeSleep()
    executor = ContinuousExecutor(
        host=host,
        max_batches=max_batches,
        pause_seconds=0.25,
        sleep=pause,
        clock=clock if clock is not None else FakeClock(),
        provider_control=provider_control,
    )
    return executor, pause


_ATTEMPT = AttemptIdentity(attempt_id="job-1:1", attempt_number=1, task_run_id="job-1")


def _queued_entry(
    scope: ExecutionScope,
    *,
    batches_completed: int = 0,
    started_at: str | None = None,
    resumed: bool = False,
) -> JournalEntry:
    # The production queue persists manifest/selection/marks but never the
    # claim-time route totals; the persisted scope mirrors that.
    persisted_scope = ExecutionScope(
        route_manifest=scope.route_manifest,
        selected_route_keys=scope.selected_route_keys,
        route_high_water_marks=scope.route_high_water_marks,
        route_totals={},
    )
    return _entry(
        status="queued",
        scope=persisted_scope,
        batches_completed=batches_completed,
        started_at=started_at,
        resumed=resumed,
    )


def _write_batch(
    *record_ids: str,
    route_key: str = _ACCOUNT_ROUTE,
    ledger: InMemoryLedger,
    next_cursor: str | None = None,
    has_more: bool = False,
    status: str = "created",
) -> Any:
    """A script item: write records durably and advance the snapshot."""

    def mutate(entry: JournalEntry) -> None:
        for record_id in record_ids:
            ledger.rows[("Account", "companies", record_id)] = _outcome(record_id, status)
        counts = entry.snapshot.route_counts.setdefault(
            route_key, {"created": 0, "updated": 0, "skipped": 0}
        )
        if status in ("created", "updated", "skipped"):
            counts[status] = counts.get(status, 0) + len(record_ids)
        entry.snapshot.batch_pages = {
            route_key: BatchPage(
                source_record_ids=tuple(record_ids),
                next_cursor=next_cursor,
                has_more=has_more,
            )
        }
        if has_more and next_cursor:
            entry.snapshot.checkpoints = {route_key: next_cursor}
        else:
            entry.snapshot.checkpoints = {}
            entry.snapshot.completed_routes.add(route_key)

    return mutate


# -- executor: claim phase ----------------------------------------------------


async def test_completed_entry_short_circuits_without_claiming() -> None:
    journal = InMemoryJournal(_entry(status="completed"))
    fence = ScriptedFence(journal=journal)
    host = _host(InMemoryLedger(), journal, fence)
    executor, _ = _executor(host)

    result = await executor.run(scope=_scope(), attempt=_ATTEMPT, step=ScriptedStep(journal, []))

    assert result.status == "completed"
    assert fence.claims == []
    assert journal.saves == []


async def test_cancelled_entry_short_circuits_before_any_batch() -> None:
    entry = _entry(status="running")
    entry.extras["execution"] = {"state": "cancelled"}
    journal = InMemoryJournal(entry)
    fence = ScriptedFence(journal=journal)
    host = _host(InMemoryLedger(), journal, fence)
    executor, _ = _executor(host)
    step = ScriptedStep(journal, [])

    result = await executor.run(scope=_scope(), attempt=_ATTEMPT, step=step)

    assert _is_cancelled_report(dump_journal(result))
    assert step.calls == 0


def _is_cancelled_report(report: dict[str, Any]) -> bool:
    execution = report.get("execution")
    return report.get("status") == "cancelled" or (
        isinstance(execution, dict) and execution.get("state") == "cancelled"
    )


async def test_older_attempt_is_fenced_before_any_write() -> None:
    """Pinned: a fenced older attempt never writes."""

    scope = _scope()
    journal = InMemoryJournal(_queued_entry(scope))
    journal.stored_attempt_id = "job-1:2"  # a newer attempt owns the row
    fence = ScriptedFence(outcome="superseded")
    observer = RecordingObserver()
    host = _host(InMemoryLedger(), journal, fence, observer)
    executor, _ = _executor(host)
    step = ScriptedStep(journal, [])
    before = json.dumps(journal.report, sort_keys=True)

    with pytest.raises(ExecutionFault) as fault:
        await executor.run(scope=scope, attempt=_ATTEMPT, step=step)

    assert fault.value.code == "SANKA_MIGRATE_EXECUTION_ATTEMPT_SUPERSEDED"
    assert step.calls == 0
    assert journal.saves == []
    assert json.dumps(journal.report, sort_keys=True) == before
    assert observer.fenced_attempts == ["job-1:1"]


async def test_claim_cancelled_race_returns_the_finalized_entry() -> None:
    cancelled = _entry(status="cancelled")
    journal = InMemoryJournal(_queued_entry(_scope()))
    fence = ScriptedFence(outcome="cancelled", entry=cancelled)
    host = _host(InMemoryLedger(), journal, fence)
    executor, _ = _executor(host)

    result = await executor.run(scope=_scope(), attempt=_ATTEMPT, step=ScriptedStep(journal, []))

    assert result.status == "cancelled"


# -- executor: scope validation -----------------------------------------------


async def test_queued_entry_without_scope_is_refused_and_marked_failed() -> None:
    journal = InMemoryJournal(_entry(status="queued", scope=None))
    fence = ScriptedFence(journal=journal)
    host = _host(InMemoryLedger(), journal, fence)
    executor, _ = _executor(host)

    with pytest.raises(ExecutionFault) as fault:
        await executor.run(scope=_scope(), attempt=_ATTEMPT, step=ScriptedStep(journal, []))

    assert fault.value.code == "SANKA_MIGRATE_ROUTE_MANIFEST_MISSING"
    assert journal.report is not None
    assert journal.report["status"] == "failed"
    assert any("route manifest" in warning for warning in journal.report["warnings"])


async def test_saved_manifest_mismatch_is_refused() -> None:
    saved_scope = _scope()
    journal = InMemoryJournal(_queued_entry(saved_scope))
    fence = ScriptedFence(journal=journal)
    host = _host(InMemoryLedger(), journal, fence)
    executor, _ = _executor(host)
    changed = _scope(manifest=_manifest(("Account", "companies"), ("Contact", "contacts")))

    with pytest.raises(ExecutionFault) as fault:
        await executor.run(scope=changed, attempt=_ATTEMPT, step=ScriptedStep(journal, []))

    assert fault.value.code == "SANKA_MIGRATE_ROUTE_MANIFEST_CHANGED"
    assert journal.report is not None and journal.report["status"] == "failed"


async def test_saved_route_selection_mismatch_is_refused() -> None:
    manifest = _manifest(("Account", "companies"), ("Contact", "contacts"))
    saved = ExecutionScope(
        route_manifest=tuple(manifest),
        selected_route_keys=(_ACCOUNT_ROUTE, _CONTACT_ROUTE),
        route_high_water_marks={},
        route_totals={},
    )
    journal = InMemoryJournal(_queued_entry(saved))
    fence = ScriptedFence(journal=journal)
    host = _host(InMemoryLedger(), journal, fence)
    executor, _ = _executor(host)
    narrowed = ExecutionScope(
        route_manifest=tuple(manifest),
        selected_route_keys=(_ACCOUNT_ROUTE,),
        route_high_water_marks={},
        route_totals={},
    )

    with pytest.raises(ExecutionFault) as fault:
        await executor.run(scope=narrowed, attempt=_ATTEMPT, step=ScriptedStep(journal, []))

    assert fault.value.code == "SANKA_MIGRATE_EXECUTION_ROUTE_CHANGED"


async def test_saved_high_water_mark_mismatch_is_refused() -> None:
    saved = _scope(route_high_water_marks={_ACCOUNT_ROUTE: "2026-08-01T00:00:00Z"})
    journal = InMemoryJournal(_queued_entry(saved))
    fence = ScriptedFence(journal=journal)
    host = _host(InMemoryLedger(), journal, fence)
    executor, _ = _executor(host)
    drifted = _scope(route_high_water_marks={_ACCOUNT_ROUTE: "2026-08-02T00:00:00Z"})

    with pytest.raises(ExecutionFault) as fault:
        await executor.run(scope=drifted, attempt=_ATTEMPT, step=ScriptedStep(journal, []))

    assert fault.value.code == "SANKA_MIGRATE_EXECUTION_HIGH_WATER_MARK_INVALID"


# -- executor: the batch loop -------------------------------------------------


async def test_runs_batches_to_completion_with_heartbeats_and_pauses() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 3})
    journal = InMemoryJournal(_queued_entry(scope, started_at=_NOW.isoformat()))
    fence = ScriptedFence(journal=journal)
    observer = RecordingObserver()
    host = _host(ledger, journal, fence, observer)
    clock = FakeClock(start=_NOW + timedelta(seconds=10))
    executor, sleep = _executor(host, clock=clock)
    step = ScriptedStep(
        journal,
        [
            (
                _write_batch("001A", "001B", ledger=ledger, next_cursor="001B", has_more=True),
                True,
            ),
            (_write_batch("001C", ledger=ledger), False),
        ],
    )

    result = await executor.run(
        scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1", batch_size=2
    )

    assert result.status == "completed"
    assert result.batches_completed == 2
    assert result.snapshot.completed_routes == {_ACCOUNT_ROUTE}
    assert sleep.calls == [0.25]  # one pause between the two batches
    assert observer.batches == [(1, True, False), (2, False, False)]

    report = journal.report
    assert report is not None
    assert report["status"] == "completed"
    execution = report["execution"]
    assert execution["mode"] == "continuous"
    assert execution["state"] == "completed"
    assert execution["jobId"] == "job-1"
    assert execution["batchSize"] == 2
    assert execution["batchesCompleted"] == 2
    assert execution["startedAt"] == _NOW.isoformat()
    assert execution["lastHeartbeatAt"]
    assert execution["resumed"] is False
    assert execution["routeManifest"] == _manifest()
    assert execution["selectedRouteKeys"] == [_ACCOUNT_ROUTE]
    assert "routeTotals" not in execution  # claim-time state, not report contract
    row = report["routeProgress"][0]
    assert (row["processed"], row["total"], row["remaining"]) == (3, 3, 0)
    assert row["percent"] == 100
    assert row["status"] == "completed"
    assert report["progress"]["processed"] == 3
    assert report["progress"]["percent"] == 100


async def test_resumes_beyond_ten_thousand_batches() -> None:
    """Pinned: a resumed job continues past ten thousand completed batches."""

    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 2})
    queued = _queued_entry(
        scope, batches_completed=10_000, started_at=_NOW.isoformat(), resumed=True
    )
    journal = InMemoryJournal(queued)
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, max_batches=1_000_000, clock=FakeClock())
    step = ScriptedStep(journal, [(_write_batch("001A", "001B", ledger=ledger), False)])

    result = await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    assert result.status == "completed"
    assert result.batches_completed == 10_001
    assert result.resumed is True
    assert journal.report is not None
    assert journal.report["execution"]["batchesCompleted"] == 10_001
    assert journal.report["execution"]["resumed"] is True


async def test_batch_safety_limit_marks_the_run_failed_for_review() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 100})
    journal = InMemoryJournal(_queued_entry(scope))
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, max_batches=2, clock=FakeClock())
    cursors = iter(["001A", "001B", "001C", "001D"])

    def advancing(entry: JournalEntry) -> None:
        record_id = next(cursors)
        ledger.rows[("Account", "companies", record_id)] = _outcome(record_id)
        counts = entry.snapshot.route_counts.setdefault(
            _ACCOUNT_ROUTE, {"created": 0, "updated": 0, "skipped": 0}
        )
        counts["created"] += 1
        entry.snapshot.checkpoints = {_ACCOUNT_ROUTE: record_id}
        entry.snapshot.batch_pages = {
            _ACCOUNT_ROUTE: BatchPage(
                source_record_ids=(record_id,), next_cursor=record_id, has_more=True
            )
        }

    step = ScriptedStep(journal, [(advancing, True), (advancing, True), (advancing, True)])

    result = await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    assert step.calls == 2  # the limit stopped the loop, not the script
    assert result.status == "failed"
    assert BATCH_SAFETY_LIMIT_MESSAGE in result.snapshot.warnings
    assert journal.report is not None
    assert journal.report["status"] == "failed"
    assert journal.report["execution"]["state"] == "failed"


def _claim_newer_attempt(journal: InMemoryJournal, attempt_id: str = "job-1:2") -> None:
    """Simulate a newer attempt's CAS claim landing on the journal."""

    journal.stored_attempt_id = attempt_id
    assert journal.report is not None
    execution = dict(journal.report.get("execution") or {})
    execution["attemptId"] = attempt_id
    journal.report = {**journal.report, "execution": execution}


async def test_newer_attempt_fences_the_loop_and_its_failure_mark_never_writes() -> None:
    """Pinned: a fenced older attempt never writes over the newer owner."""

    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 100})
    journal = InMemoryJournal(_queued_entry(scope))
    fence = ScriptedFence(journal=journal)
    observer = RecordingObserver()
    host = _host(ledger, journal, fence, observer)

    class ClaimingSleep(FakeSleep):
        async def __call__(self, seconds: float) -> None:
            await super().__call__(seconds)
            _claim_newer_attempt(journal)  # a newer attempt claims between batches

    sleep = ClaimingSleep()
    executor, _ = _executor(host, clock=FakeClock(), sleep=sleep)
    step = ScriptedStep(
        journal,
        [
            (_write_batch("001A", ledger=ledger, next_cursor="001A", has_more=True), True),
            (_write_batch("001B", ledger=ledger, next_cursor="001B", has_more=True), True),
        ],
    )

    with pytest.raises(ExecutionFault) as fault:
        await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    assert fault.value.code == "SANKA_MIGRATE_EXECUTION_ATTEMPT_SUPERSEDED"
    assert step.calls == 1  # the fence stopped the loop before batch two
    assert observer.fenced_attempts == ["job-1:1"]
    # the old attempt's failure mark was fenced out: the newer attempt's
    # journal state survives untouched
    assert journal.report is not None
    assert journal.report["execution"]["attemptId"] == "job-1:2"
    assert journal.report.get("status") != "failed"


async def test_superseding_save_mid_batch_raises_job_superseded() -> None:
    """A save fenced out mid-batch surfaces the job supersession, never a write."""

    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 100})
    journal = InMemoryJournal(_queued_entry(scope))
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, clock=FakeClock())

    def first_batch(entry: JournalEntry) -> None:
        _write_batch("001A", ledger=ledger, next_cursor="001A", has_more=True)(entry)
        _claim_newer_attempt(journal)  # lands while the batch is in flight

    step = ScriptedStep(journal, [(first_batch, True)])

    with pytest.raises(ExecutionFault) as fault:
        await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    assert fault.value.code == "SANKA_MIGRATE_EXECUTION_JOB_SUPERSEDED"
    assert journal.report is not None
    assert journal.report["execution"]["attemptId"] == "job-1:2"
    assert journal.report.get("status") != "failed"


async def test_cancellation_between_batches_returns_the_cancelled_entry() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 100})
    journal = InMemoryJournal(_queued_entry(scope))
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, clock=FakeClock())

    def first_batch(entry: JournalEntry) -> None:
        _write_batch("001A", ledger=ledger, next_cursor="001A", has_more=True)(entry)
        journal.record_cancellation()

    step = ScriptedStep(journal, [(first_batch, True)])

    result = await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    assert _is_cancelled_report(dump_journal(result))
    assert step.calls == 1


async def test_save_cancelled_race_preserves_the_batch_and_returns_cancelled() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 1})
    journal = InMemoryJournal(_queued_entry(scope))
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, clock=FakeClock())

    def batch_then_cancel(entry: JournalEntry) -> None:
        _write_batch("001A", ledger=ledger)(entry)
        journal.cancel_recorded = True  # cancellation lands before the heartbeat save

    step = ScriptedStep(journal, [(batch_then_cancel, False)])

    result = await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    assert result.status == "cancelled"
    # the in-flight batch's work was finalized, not discarded
    assert journal.report is not None
    assert journal.report["status"] == "cancelled"
    assert journal.report["routeCounts"][_ACCOUNT_ROUTE]["created"] == 1


# -- executor: stall detection and reconciliation -----------------------------


def _no_progress(entry: JournalEntry) -> None:
    """A batch that reads the same page again and advances nothing."""

    entry.snapshot.batch_pages = {
        _ACCOUNT_ROUTE: BatchPage(
            source_record_ids=("001A", "001B"), next_cursor="001B", has_more=True
        )
    }


async def test_stall_without_terminal_pages_fails_for_review() -> None:
    ledger = InMemoryLedger()  # nothing durable: the page is not terminal
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 4})
    queued = _queued_entry(scope)
    queued.snapshot.checkpoints = {_ACCOUNT_ROUTE: "001B"}
    queued.snapshot.route_counts = {_ACCOUNT_ROUTE: {"created": 2, "updated": 0, "skipped": 0}}
    journal = InMemoryJournal(queued)
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, clock=FakeClock())

    def stalled_batch(entry: JournalEntry) -> None:
        entry.snapshot.checkpoints = {_ACCOUNT_ROUTE: "001B"}  # unchanged marker
        _no_progress(entry)

    step = ScriptedStep(journal, [(stalled_batch, True)])

    result = await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    assert result.status == "failed"
    assert STALLED_NO_PROGRESS_WARNING in result.snapshot.warnings
    assert journal.report is not None and journal.report["status"] == "failed"
    assert journal.report["routeProgress"][0]["status"] == "failed"
    assert journal.report["execution"]["state"] == "failed"
    assert step.calls == 1


async def test_stall_reconciles_terminal_pages_and_resumes() -> None:
    """Pinned: stall -> terminal-page reconciliation -> resume to completion."""

    ledger = InMemoryLedger()
    ledger.seed(_outcome("001A"), _outcome("001B"))  # the page is durably terminal
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 3})
    queued = _queued_entry(scope)
    queued.snapshot.checkpoints = {_ACCOUNT_ROUTE: "001B"}
    queued.snapshot.route_counts = {_ACCOUNT_ROUTE: {"created": 2, "updated": 0, "skipped": 0}}
    journal = InMemoryJournal(queued)
    fence = ScriptedFence(journal=journal)
    observer = RecordingObserver()
    host = _host(ledger, journal, fence, observer)
    executor, sleep = _executor(host, clock=FakeClock())

    def stalled_batch(entry: JournalEntry) -> None:
        entry.snapshot.checkpoints = {_ACCOUNT_ROUTE: "001B"}  # marker unchanged
        entry.snapshot.route_counts = {_ACCOUNT_ROUTE: {"created": 2, "updated": 0, "skipped": 0}}
        entry.snapshot.batch_pages = {
            _ACCOUNT_ROUTE: BatchPage(
                source_record_ids=("001A", "001B"), next_cursor="001C", has_more=True
            )
        }

    step = ScriptedStep(
        journal,
        [
            (stalled_batch, True),
            (_write_batch("001C", ledger=ledger), False),
        ],
    )

    result = await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    # reconciliation advanced the checkpoint from the terminal page evidence,
    # the loop resumed, and the run completed
    assert result.status == "completed"
    assert step.calls == 2
    assert observer.batches[0] == (1, True, False)  # not stalled after reconcile
    assert sleep.calls == [0.25]
    saved_after_stall = journal.saves[1]  # step save, then the heartbeat save
    assert saved_after_stall["checkpoints"] == {_ACCOUNT_ROUTE: "001C"}
    assert saved_after_stall["counts"] == {"created": 2}


async def test_terminal_page_with_durable_failures_is_rechecked_then_fails() -> None:
    """Pinned: the terminal-page recheck runs before a stall becomes failure."""

    ledger = InMemoryLedger()
    ledger.seed(_outcome("001A"), _outcome("001B", "failed"))
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 4})
    queued = _queued_entry(scope)
    queued.snapshot.checkpoints = {_ACCOUNT_ROUTE: "001B"}
    journal = InMemoryJournal(queued)
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, clock=FakeClock())

    def stalled_batch(entry: JournalEntry) -> None:
        entry.snapshot.checkpoints = {_ACCOUNT_ROUTE: "001B"}
        _no_progress(entry)

    step = ScriptedStep(journal, [(stalled_batch, True)])

    result = await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    # the recheck consulted the durable ledger, found the failed row, and
    # kept the stall as failed-for-review instead of silently advancing
    assert len(ledger.terminal_reads) >= 1
    assert result.status == "failed"
    assert STALLED_NO_PROGRESS_WARNING in result.snapshot.warnings


# -- executor: frozen-total reopening -----------------------------------------


async def test_frozen_total_shortfall_reopens_the_route_and_stops_for_review() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 5})
    journal = InMemoryJournal(_queued_entry(scope))
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, clock=FakeClock())
    # the source page ended (has_more False) after only two records
    step = ScriptedStep(
        journal,
        [
            (
                _write_batch("001A", "001B", ledger=ledger, next_cursor="001B", has_more=False),
                False,
            )
        ],
    )

    result = await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    assert result.status == "failed"
    assert FROZEN_TOTAL_SHORTFALL_WARNING in result.snapshot.warnings
    assert result.snapshot.completed_routes == set()
    assert result.snapshot.checkpoints == {_ACCOUNT_ROUTE: "001B"}
    assert result.snapshot.has_more is True
    assert journal.report is not None
    assert journal.report["status"] == "failed"
    assert journal.report["hasMore"] is True


async def test_frozen_total_shortfall_without_checkpoint_refuses_and_marks_failed() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 5})
    journal = InMemoryJournal(_queued_entry(scope))
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, clock=FakeClock())

    def completes_early(entry: JournalEntry) -> None:
        entry.snapshot.completed_routes.add(_ACCOUNT_ROUTE)
        entry.snapshot.checkpoints = {}
        entry.snapshot.batch_pages = {}  # no page: no safe keyset checkpoint

    step = ScriptedStep(journal, [(completes_early, False)])

    with pytest.raises(ExecutionFault) as fault:
        await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    assert fault.value.code == "SANKA_MIGRATE_SOURCE_CHECKPOINT_MISSING"
    assert journal.report is not None
    assert journal.report["status"] == "failed"


async def test_unknown_totals_never_reopen_a_completed_route() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: None})
    journal = InMemoryJournal(_queued_entry(scope))
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, clock=FakeClock())
    step = ScriptedStep(journal, [(_write_batch("001A", ledger=ledger), False)])

    result = await executor.run(scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1")

    assert result.status == "completed"
    assert result.snapshot.completed_routes == {_ACCOUNT_ROUTE}


# -- progress / heartbeat assembly --------------------------------------------


async def test_progress_rows_carry_rate_eta_and_percent_from_the_injected_clock() -> None:
    ledger = InMemoryLedger()
    manifest = _manifest(("Account", "companies"), ("Contact", "contacts"))
    scope = ExecutionScope(
        route_manifest=tuple(manifest),
        selected_route_keys=(_ACCOUNT_ROUTE, _CONTACT_ROUTE),
        route_high_water_marks={},
        route_totals={_ACCOUNT_ROUTE: 10, _CONTACT_ROUTE: None},
    )
    snapshot = ExecutionSnapshot(
        checkpoints={_ACCOUNT_ROUTE: "001E"},
        route_counts={
            _ACCOUNT_ROUTE: {"created": 3, "updated": 1, "skipped": 1, "failed": 1},
            _CONTACT_ROUTE: {"created": 2, "updated": 0, "skipped": 0},
        },
        route_pending_record_ids={_CONTACT_ROUTE: {"003A", "003B"}},
    )
    entry = _entry(snapshot=snapshot)

    await assemble_continuous_report(
        ledger=ledger,
        entry=entry,
        state="running",
        scope=scope,
        job_id="job-1",
        started_at=_NOW.isoformat(),
        batches_completed=4,
        batch_size=25,
        resumed=False,
        stalled=False,
        now=_NOW + timedelta(seconds=10),
    )

    account_row, contact_row = entry.route_progress
    assert account_row["routeKey"] == _ACCOUNT_ROUTE
    assert account_row["processed"] == 5
    assert account_row["failed"] == 1
    assert account_row["total"] == 10
    assert account_row["remaining"] == 5
    assert account_row["percent"] == 50
    assert account_row["etaSeconds"] == 10  # 5 processed / 10s => 5 remaining / 0.5/s
    assert account_row["checkpoint"] == "001E"
    assert account_row["status"] == "running"
    assert contact_row["total"] is None
    assert contact_row["remaining"] is None
    assert contact_row["percent"] is None
    assert contact_row["etaSeconds"] is None
    assert contact_row["associationPending"] == 2

    progress = entry.extras["progress"]
    assert progress["processed"] == 7
    assert progress["total"] is None  # one unknown total keeps the overall unknown
    assert progress["remaining"] is None
    assert progress["percent"] is None
    assert progress["etaSeconds"] is None


async def test_progress_falls_back_to_durable_pair_totals_for_unique_pairs() -> None:
    ledger = InMemoryLedger()
    ledger.seed(
        _outcome("001A", "created"),
        _outcome("001B", "updated"),
        _outcome("001C", "failed"),
    )
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 4})
    entry = _entry()  # no in-memory route counts at all

    await assemble_continuous_report(
        ledger=ledger,
        entry=entry,
        state="running",
        scope=scope,
        job_id="job-1",
        started_at=_NOW.isoformat(),
        batches_completed=1,
        batch_size=25,
        resumed=False,
        stalled=False,
        now=_NOW + timedelta(seconds=1),
    )

    row = entry.route_progress[0]
    assert row["processed"] == 2
    assert row["created"] == 1
    assert row["updated"] == 1
    assert row["failed"] == 1
    assert row["remaining"] == 2


async def test_failed_routes_report_failed_status_only_while_stalled() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 5})
    snapshot = ExecutionSnapshot(
        route_counts={_ACCOUNT_ROUTE: {"created": 1, "updated": 0, "skipped": 0, "failed": 2}}
    )
    entry = _entry(snapshot=snapshot)

    await assemble_continuous_report(
        ledger=ledger,
        entry=entry,
        state="failed",
        scope=scope,
        job_id="job-1",
        started_at=_NOW.isoformat(),
        batches_completed=2,
        batch_size=25,
        resumed=False,
        stalled=True,
        now=_NOW + timedelta(seconds=5),
    )

    assert entry.route_progress[0]["status"] == "failed"
    assert STALLED_NO_PROGRESS_WARNING in entry.snapshot.warnings

    # the same warning is not appended twice
    await assemble_continuous_report(
        ledger=ledger,
        entry=entry,
        state="failed",
        scope=scope,
        job_id="job-1",
        started_at=_NOW.isoformat(),
        batches_completed=3,
        batch_size=25,
        resumed=False,
        stalled=True,
        now=_NOW + timedelta(seconds=6),
    )
    assert entry.snapshot.warnings.count(STALLED_NO_PROGRESS_WARNING) == 1


async def test_provider_control_is_written_only_when_a_supplier_exists() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 1})

    entry = _entry()
    await assemble_continuous_report(
        ledger=ledger,
        entry=entry,
        state="running",
        scope=scope,
        job_id="job-1",
        started_at=_NOW.isoformat(),
        batches_completed=1,
        batch_size=25,
        resumed=False,
        stalled=False,
        now=_NOW,
    )
    assert "providerControl" not in dump_journal(entry)

    with_metrics = _entry()
    await assemble_continuous_report(
        ledger=ledger,
        entry=with_metrics,
        state="running",
        scope=scope,
        job_id="job-1",
        started_at=_NOW.isoformat(),
        batches_completed=1,
        batch_size=25,
        resumed=False,
        stalled=False,
        now=_NOW,
        provider_control=lambda: {"retryAfterSeconds": 30},
    )
    assert dump_journal(with_metrics)["providerControl"] == {"retryAfterSeconds": 30}

    with_null_metrics = _entry()
    await assemble_continuous_report(
        ledger=ledger,
        entry=with_null_metrics,
        state="running",
        scope=scope,
        job_id="job-1",
        started_at=_NOW.isoformat(),
        batches_completed=1,
        batch_size=25,
        resumed=False,
        stalled=False,
        now=_NOW,
        provider_control=lambda: None,
    )
    # the production heartbeat writes the key even when the provider has none
    report = dump_journal(with_null_metrics)
    assert "providerControl" in report and report["providerControl"] is None


async def test_heartbeat_preserves_host_envelope_keys_and_persisted_totals() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 2})
    queued = _entry(status="queued", scope=scope)  # persisted totals present (local queue)
    report = dump_journal(queued)
    report["summary"] = "host copy"
    report["generatedAt"] = "2026-08-16T00:00:00+00:00"
    report["sourceProvider"] = "salesforce"
    entry = load_journal(report)

    await assemble_continuous_report(
        ledger=ledger,
        entry=entry,
        state="running",
        scope=scope,
        job_id="job-1",
        started_at=_NOW.isoformat(),
        batches_completed=1,
        batch_size=25,
        resumed=True,
        stalled=False,
        now=_NOW,
    )

    dumped = dump_journal(entry)
    # envelope copy is untouched host property
    assert dumped["summary"] == "host copy"
    assert dumped["generatedAt"] == "2026-08-16T00:00:00+00:00"
    assert dumped["sourceProvider"] == "salesforce"
    # a locally persisted scope keeps its totals through the heartbeat
    assert dumped["execution"]["routeTotals"] == {_ACCOUNT_ROUTE: 2}
    assert dumped["execution"]["resumed"] is True
    assert "resumed" not in entry.extras.get("execution", {})


# -- failure marking ----------------------------------------------------------


async def test_mark_execution_failed_appends_the_message_and_heartbeat() -> None:
    scope = _scope()
    journal = InMemoryJournal(_queued_entry(scope))
    clock = FakeClock()

    marked = await mark_execution_failed(
        journal=journal,
        message="Source API rejected the credentials.",
        job_id="job-1",
        attempt_id="job-1:1",
        clock=clock,
    )

    assert marked is not None
    assert marked.status == "failed"
    assert journal.report is not None
    assert journal.report["status"] == "failed"
    assert journal.report["warnings"] == ["Source API rejected the credentials."]
    execution = journal.report["execution"]
    assert execution["mode"] == "continuous"
    assert execution["state"] == "failed"
    assert execution["jobId"] == "job-1"
    assert execution["lastHeartbeatAt"] == _NOW.isoformat()

    # marking again with the same message does not duplicate the warning
    again = await mark_execution_failed(
        journal=journal,
        message="Source API rejected the credentials.",
        job_id="job-1",
        attempt_id="job-1:1",
        clock=clock,
    )
    assert again is not None
    assert journal.report["warnings"] == ["Source API rejected the credentials."]


async def test_mark_execution_failed_is_fenced_by_job_and_attempt() -> None:
    scope = _scope()
    foreign_job = _queued_entry(scope)
    foreign_job.job_id = "job-9"
    journal = InMemoryJournal(foreign_job)
    assert (await mark_execution_failed(journal=journal, message="boom", job_id="job-1")) is None
    assert journal.report is not None and journal.report.get("status") == "queued"

    owned = InMemoryJournal(_queued_entry(scope))
    owned.stored_attempt_id = "job-1:2"  # a newer attempt owns the row
    assert (
        await mark_execution_failed(
            journal=owned, message="boom", job_id="job-1", attempt_id="job-1:1"
        )
    ) is None
    assert owned.report is not None and owned.report.get("status") == "queued"


async def test_mark_execution_failed_creates_a_fresh_entry_when_nothing_persisted() -> None:
    journal = InMemoryJournal()

    marked = await mark_execution_failed(
        journal=journal, message="boom", job_id="job-1", clock=FakeClock()
    )

    assert marked is not None
    assert journal.report is not None
    assert journal.report["status"] == "failed"
    assert journal.report["warnings"] == ["boom"]


# -- executor: exception paths ------------------------------------------------


async def test_step_exception_marks_the_run_failed_and_reraises() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 1})
    journal = InMemoryJournal(_queued_entry(scope))
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, clock=FakeClock())

    class Boom(RuntimeError):
        pass

    async def exploding_step() -> tuple[JournalEntry, bool]:
        raise Boom("destination write exploded")

    with pytest.raises(Boom):
        await executor.run(scope=scope, attempt=_ATTEMPT, step=exploding_step, job_id="job-1")

    assert journal.report is not None
    assert journal.report["status"] == "failed"
    assert "destination write exploded" in journal.report["warnings"]
    assert journal.report["execution"]["state"] == "failed"


async def test_step_exception_after_cancellation_returns_the_cancelled_entry() -> None:
    ledger = InMemoryLedger()
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 1})
    journal = InMemoryJournal(_queued_entry(scope))
    fence = ScriptedFence(journal=journal)
    host = _host(ledger, journal, fence)
    executor, _ = _executor(host, clock=FakeClock())

    async def cancelled_then_boom() -> tuple[JournalEntry, bool]:
        journal.record_cancellation()
        raise RuntimeError("batch interrupted by cancellation")

    result = await executor.run(
        scope=scope, attempt=_ATTEMPT, step=cancelled_then_boom, job_id="job-1"
    )

    assert _is_cancelled_report(dump_journal(result))
    assert journal.report is not None
    assert journal.report.get("status") == "cancelled"


# -- SQLite execution state end-to-end ----------------------------------------


def _sqlite_host(state: SqliteExecutionState) -> ExecutionHost:
    return ExecutionHost(
        ledger=state,
        journal=state,
        fence=state,
        identity_ledger=NullIdentityLedger(),
        shared_identity_ledger=None,
        observer=RecordingObserver(),
    )


async def test_sqlite_continuous_run_completes_and_survives_reload(tmp_path: Any) -> None:
    path = tmp_path / "sanka.db"
    state = SqliteExecutionState(path, run_id="run-1", job_id="job-1")
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 3})
    await state.save(_queued_entry(scope))

    async def batch(record_ids: tuple[str, ...], has_more: bool, cursor: str | None) -> None:
        entry = await state.load()
        assert entry is not None
        await state.upsert_results([_outcome(record_id) for record_id in record_ids])
        counts = entry.snapshot.route_counts.setdefault(
            _ACCOUNT_ROUTE, {"created": 0, "updated": 0, "skipped": 0}
        )
        counts["created"] = counts.get("created", 0) + len(record_ids)
        entry.snapshot.batch_pages = {
            _ACCOUNT_ROUTE: BatchPage(
                source_record_ids=record_ids, next_cursor=cursor, has_more=has_more
            )
        }
        if has_more and cursor:
            entry.snapshot.checkpoints = {_ACCOUNT_ROUTE: cursor}
        else:
            entry.snapshot.checkpoints = {}
            entry.snapshot.completed_routes.add(_ACCOUNT_ROUTE)
        entry.snapshot.has_more = has_more
        entry.status = "running" if has_more else "completed"
        assert await state.save(entry) == "saved"

    script = iter(
        [
            (("001A", "001B"), True, "001B"),
            (("001C",), False, None),
        ]
    )

    async def step() -> tuple[JournalEntry, bool]:
        record_ids, has_more, cursor = next(script)
        await batch(record_ids, has_more, cursor)
        entry = await state.load()
        assert entry is not None
        return entry, has_more

    executor = ContinuousExecutor(
        host=_sqlite_host(state),
        max_batches=10,
        sleep=FakeSleep(),
        clock=FakeClock(),
    )
    result = await executor.run(
        scope=scope, attempt=_ATTEMPT, step=step, job_id="job-1", batch_size=2
    )

    assert result.status == "completed"
    assert result.batches_completed == 2
    assert await state.status_totals() == {"created": 3}

    # a fresh handle over the same file sees the terminal journal state
    reloaded_state = SqliteExecutionState(path, run_id="run-1", job_id="job-1")
    reloaded = await reloaded_state.load()
    assert reloaded is not None
    assert reloaded.status == "completed"
    assert reloaded.batches_completed == 2
    assert reloaded.attempt_id == "job-1:1"
    assert reloaded.scope is not None
    assert list(reloaded.scope.selected_route_keys) == [_ACCOUNT_ROUTE]
    state.close()
    reloaded_state.close()


async def test_sqlite_fences_an_older_attempt_without_writing(tmp_path: Any) -> None:
    """Pinned: the fenced older attempt never writes — real store variant."""

    path = tmp_path / "sanka.db"
    newer_state = SqliteExecutionState(path, run_id="run-1", job_id="job-1")
    scope = _scope(route_totals={_ACCOUNT_ROUTE: 3})
    await newer_state.save(_queued_entry(scope))
    newer = AttemptIdentity(attempt_id="job-1:2", attempt_number=2, task_run_id="job-1")
    outcome, claimed = await newer_state.claim(newer)
    assert outcome == "claimed" and claimed is not None
    claimed.status = "running"
    assert await newer_state.save(claimed) == "saved"

    older_state = SqliteExecutionState(path, run_id="run-1", job_id="job-1")
    older = AttemptIdentity(attempt_id="job-1:1", attempt_number=1, task_run_id="job-1")

    async def never_step() -> tuple[JournalEntry, bool]:
        raise AssertionError("a fenced older attempt must never run a batch")

    executor = ContinuousExecutor(
        host=_sqlite_host(older_state),
        max_batches=10,
        sleep=FakeSleep(),
        clock=FakeClock(),
    )
    with pytest.raises(ExecutionFault) as fault:
        await executor.run(scope=scope, attempt=older, step=never_step, job_id="job-1")

    assert fault.value.code == "SANKA_MIGRATE_EXECUTION_ATTEMPT_SUPERSEDED"
    # the newer attempt's row is untouched: still running, still job-1:2
    surviving = await newer_state.load()
    assert surviving is not None
    assert surviving.status == "running"
    assert surviving.attempt_id == "job-1:2"
    newer_state.close()
    older_state.close()
