# SPDX-License-Identifier: AGPL-3.0-only
"""SqliteExecutionState: ledger, journal, and fence over one SQLite file."""

from __future__ import annotations

from pathlib import Path

import pytest

from sanka.runtime.execution import (
    AttemptFence,
    AttemptIdentity,
    ExecutionFault,
    ExecutionJournal,
    ExecutionLedger,
    ExecutionSnapshot,
    JournalEntry,
    RecordWriteOutcome,
    SqliteExecutionState,
    WriteStatus,
)
from sanka.runtime.state import RunStatus, SqliteStateStore

ROUTE = "accounts|companies"


def _store(
    tmp_path: Path, *, run_id: str = "run-1", job_id: str | None = None
) -> SqliteExecutionState:
    return SqliteExecutionState(tmp_path / "sanka.db", run_id=run_id, job_id=job_id)


def _outcome(
    source_id: str,
    *,
    status: WriteStatus = "created",
    destination_id: str | None = "d-1",
    message: str | None = None,
    source_object: str = "accounts",
    destination_object: str = "companies",
) -> RecordWriteOutcome:
    return RecordWriteOutcome(
        source_object=source_object,
        source_record_id=source_id,
        destination_object=destination_object,
        destination_record_id=destination_id,
        status=status,
        message=message,
    )


def _entry(
    *,
    status: str = "running",
    job_id: str | None = "job-1",
    attempt_id: str | None = None,
) -> JournalEntry:
    return JournalEntry(
        status=status,  # type: ignore[arg-type]
        snapshot=ExecutionSnapshot(checkpoints={ROUTE: "cursor-1"}, has_more=True),
        job_id=job_id,
        attempt_id=attempt_id,
        batch_size=100,
        batches_completed=1,
        started_at="2026-08-16T00:00:00+00:00",
        last_heartbeat_at="2026-08-16T00:05:00+00:00",
        resumed=False,
        scope=None,
        durable_results_attempt_id=None,
    )


# -- protocol conformance ------------------------------------------------------


def test_sqlite_execution_state_satisfies_the_protocol_family(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.close()
    # isinstance narrowing would hide close(); check on an erased alias.
    candidate: object = store
    assert isinstance(candidate, ExecutionLedger)
    assert isinstance(candidate, ExecutionJournal)
    assert isinstance(candidate, AttemptFence)


def test_shares_the_file_with_the_v0_state_store(tmp_path: Path) -> None:
    path = tmp_path / "sanka.db"
    v0 = SqliteStateStore(path)
    v0.create_run(run_id="run-1", spec_json="{}", spec_hash="sha256:0", name=None)
    store = SqliteExecutionState(path, run_id="run-1")
    assert v0.get_run("run-1").status is RunStatus.CREATED  # v0 tables untouched
    store.close()
    v0.close()


# -- ExecutionLedger -----------------------------------------------------------


async def test_upsert_and_terminal_destination_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    persisted = await store.upsert_results(
        [
            _outcome("s-1", status="created", destination_id="d-1"),
            _outcome("s-2", status="failed", destination_id=None),
            _outcome("s-3", status="skipped", destination_id=None),
        ]
    )
    assert persisted == 3

    terminal = await store.terminal_destination_ids(
        source_object="accounts",
        source_record_ids=["s-1", "s-2", "s-3", "s-1", "s-9", ""],
        destination_object="companies",
    )
    assert terminal == {"s-1": "d-1", "s-3": None}  # failed excluded; None id kept

    assert (
        await store.terminal_destination_ids(
            source_object="accounts", source_record_ids=[], destination_object="companies"
        )
        == {}
    )
    assert await store.upsert_results([]) == 0
    store.close()


async def test_upsert_is_idempotent_by_pair_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    batch = [_outcome("s-1"), _outcome("s-2", status="failed", destination_id=None)]
    assert await store.upsert_results(batch) == 2
    assert await store.upsert_results(batch) == 2  # same key set, still count-complete
    assert await store.status_totals() == {"created": 1, "failed": 1}
    store.close()


async def test_terminal_statuses_are_sticky(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.upsert_results([_outcome("s-1", status="created", destination_id="d-1")])

    # An incoming failure can never downgrade a terminal row: status,
    # destination id, and message all survive (the production CASE arms).
    await store.upsert_results(
        [_outcome("s-1", status="failed", destination_id=None, message="late failure")]
    )
    assert await store.status_totals() == {"created": 1}
    terminal = await store.terminal_destination_ids(
        source_object="accounts", source_record_ids=["s-1"], destination_object="companies"
    )
    assert terminal == {"s-1": "d-1"}
    assert await store.failed_source_ids_by_pair() == []

    # A failed row is retryable: the repaired terminal result replaces it.
    await store.upsert_results([_outcome("s-2", status="failed", destination_id=None)])
    await store.upsert_results([_outcome("s-2", status="updated", destination_id="d-2")])
    assert await store.status_totals() == {"created": 1, "updated": 1}
    store.close()


async def test_destination_id_is_never_nulled_out(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.upsert_results([_outcome("s-1", status="failed", destination_id="d-1")])
    await store.upsert_results([_outcome("s-1", status="failed", destination_id=None)])
    # Still failed (not terminal), but the known destination id is kept.
    await store.upsert_results([_outcome("s-1", status="skipped", destination_id=None)])
    terminal = await store.terminal_destination_ids(
        source_object="accounts", source_record_ids=["s-1"], destination_object="companies"
    )
    assert terminal == {"s-1": "d-1"}
    store.close()


async def test_pair_level_totals_and_failed_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.upsert_results(
        [
            _outcome("s-1", status="created"),
            _outcome("s-2", status="failed", destination_id=None),
            _outcome("s-3", status="failed", destination_id=None),
            _outcome(
                "s-1",
                status="failed",
                destination_id=None,
                source_object="contacts",
                destination_object="contacts",
            ),
        ]
    )

    totals = await store.pair_status_totals()
    assert [
        (row.source_object, row.destination_object, row.status, row.count) for row in totals
    ] == [
        ("accounts", "companies", "created", 1),
        ("accounts", "companies", "failed", 2),
        ("contacts", "contacts", "failed", 1),
    ]

    failed = await store.failed_source_ids_by_pair()
    assert [(row.source_object, row.destination_object) for row in failed] == [
        ("accounts", "companies"),
        ("contacts", "contacts"),
    ]
    assert failed[0].source_record_ids == frozenset({"s-2", "s-3"})
    assert failed[1].source_record_ids == frozenset({"s-1"})
    store.close()


async def test_ledger_rows_are_scoped_to_the_run(tmp_path: Path) -> None:
    first = _store(tmp_path, run_id="run-1")
    second = _store(tmp_path, run_id="run-2")
    await first.upsert_results([_outcome("s-1")])
    assert await second.status_totals() == {}
    assert (
        await second.terminal_destination_ids(
            source_object="accounts", source_record_ids=["s-1"], destination_object="companies"
        )
        == {}
    )
    first.close()
    second.close()


async def test_short_persisted_count_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(store, "_upsert_rows_sync", lambda rows: len(rows) - 1)
    with pytest.raises(ExecutionFault) as excinfo:
        await store.upsert_results([_outcome("s-1"), _outcome("s-2")])
    assert excinfo.value.code == "SANKA_MIGRATE_RECORD_RESULT_BULK_SAVE_INCOMPLETE"
    assert excinfo.value.details == {"expectedCount": 2, "savedCount": 1}
    store.close()


# -- ExecutionJournal ----------------------------------------------------------


async def test_journal_round_trips_the_entry(tmp_path: Path) -> None:
    store = _store(tmp_path, job_id="job-1")
    assert await store.load() is None

    entry = _entry()
    entry.extras["summary"] = "host copy"
    assert await store.save(entry) == "saved"

    loaded = await store.load()
    assert loaded is not None
    assert loaded.status == "running"
    assert loaded.job_id == "job-1"
    assert loaded.snapshot.checkpoints == {ROUTE: "cursor-1"}
    assert loaded.snapshot.has_more is True
    assert loaded.batch_size == 100
    assert loaded.extras["summary"] == "host copy"
    store.close()


async def test_save_finalizes_after_a_recorded_cancellation(tmp_path: Path) -> None:
    store = _store(tmp_path, job_id="job-1")
    await store.save(_entry(status="running"))
    assert await store.save(_entry(status="cancelled")) == "saved"  # the cancel itself

    in_flight = _entry(status="running")
    in_flight.snapshot.checkpoints[ROUTE] = "cursor-2"
    assert await store.save(in_flight) == "cancelled"

    finalized = await store.load()
    assert finalized is not None
    assert finalized.status == "cancelled"  # terminal state preserved...
    assert finalized.snapshot.checkpoints == {ROUTE: "cursor-2"}  # ...with the batch's work
    store.close()


async def test_execution_state_cancellation_also_counts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cancelled_via_state = _entry(status="failed")
    cancelled_via_state.extras["execution"] = {"state": "cancelled"}
    await store.save(cancelled_via_state)
    assert await store.save(_entry(status="running")) == "cancelled"
    store.close()


async def test_saving_a_cancelled_entry_over_a_cancellation_stays_saved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.save(_entry(status="cancelled"))
    assert await store.save(_entry(status="cancelled")) == "saved"
    store.close()


async def test_journal_is_fenced_against_another_job(tmp_path: Path) -> None:
    owner = _store(tmp_path, job_id="job-1")
    await owner.save(_entry(job_id="job-1"))

    other = _store(tmp_path, job_id="job-2")
    assert await other.save(_entry(job_id="job-2")) == "superseded"
    with pytest.raises(ExecutionFault) as excinfo:
        await other.load()
    assert excinfo.value.code == "SANKA_MIGRATE_EXECUTION_JOB_SUPERSEDED"

    # The manual (unbound) owner is not job-fenced: a fresh queue may
    # legitimately replace the previous job's journal.
    unbound = _store(tmp_path)
    replacement = _entry(job_id="job-2", status="queued")
    assert await unbound.save(replacement) == "saved"
    reloaded = await unbound.load()
    assert reloaded is not None and reloaded.job_id == "job-2"
    owner.close()
    other.close()
    unbound.close()


# -- AttemptFence --------------------------------------------------------------


def _attempt(number: int, task_run_id: str = "task-1") -> AttemptIdentity:
    return AttemptIdentity(
        attempt_id=f"{task_run_id}:{number}", attempt_number=number, task_run_id=task_run_id
    )


async def test_claim_on_an_empty_journal_is_trivial(tmp_path: Path) -> None:
    store = _store(tmp_path, job_id="job-1")
    outcome, entry = await store.claim(_attempt(1))
    assert outcome == "claimed"
    assert entry is None
    store.close()


async def test_claim_stamps_the_attempt_onto_the_stored_entry(tmp_path: Path) -> None:
    store = _store(tmp_path, job_id="job-1")
    await store.save(_entry(status="queued"))

    outcome, entry = await store.claim(_attempt(1))
    assert outcome == "claimed"
    assert entry is not None
    assert entry.attempt_id == "task-1:1"
    assert entry.last_heartbeat_at is not None

    reloaded = await store.load()
    assert reloaded is not None and reloaded.attempt_id == "task-1:1"
    store.close()


async def test_older_attempt_is_fenced_and_reclaim_is_allowed(tmp_path: Path) -> None:
    store = _store(tmp_path, job_id="job-1")
    await store.save(_entry(status="queued"))
    assert (await store.claim(_attempt(2)))[0] == "claimed"

    # A crash-resumed process claiming with a stale attempt number loses.
    resumed = _store(tmp_path, job_id="job-1")
    assert await resumed.claim(_attempt(1)) == ("superseded", None)
    # Re-claiming the exact same attempt id wins (production CAS arm one).
    assert (await resumed.claim(_attempt(2)))[0] == "claimed"
    # A newer attempt number wins (production CAS arm two).
    assert (await resumed.claim(_attempt(3)))[0] == "claimed"
    store.close()
    resumed.close()


async def test_fenced_writer_cannot_save_over_a_newer_attempt(tmp_path: Path) -> None:
    first = _store(tmp_path, job_id="job-1")
    await first.save(_entry(status="queued"))
    _, first_entry = await first.claim(_attempt(1))
    assert first_entry is not None

    second = _store(tmp_path, job_id="job-1")
    assert (await second.claim(_attempt(2)))[0] == "claimed"

    assert await first.save(first_entry) == "superseded"
    current = await second.load()
    assert current is not None and current.attempt_id == "task-1:2"
    first.close()
    second.close()


async def test_claim_on_a_cancelled_journal_returns_the_entry(tmp_path: Path) -> None:
    store = _store(tmp_path, job_id="job-1")
    await store.save(_entry(status="cancelled"))
    outcome, entry = await store.claim(_attempt(1))
    assert outcome == "cancelled"
    assert entry is not None and entry.status == "cancelled"
    store.close()


async def test_claim_on_a_completed_journal_is_superseded(tmp_path: Path) -> None:
    store = _store(tmp_path, job_id="job-1")
    await store.save(_entry(status="completed"))
    assert await store.claim(_attempt(1)) == ("superseded", None)
    store.close()


async def test_claim_is_job_fenced(tmp_path: Path) -> None:
    owner = _store(tmp_path, job_id="job-1")
    await owner.save(_entry(job_id="job-1", status="queued"))
    other = _store(tmp_path, job_id="job-2")
    assert await other.claim(_attempt(1)) == ("superseded", None)
    owner.close()
    other.close()
