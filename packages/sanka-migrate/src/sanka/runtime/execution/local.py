# SPDX-License-Identifier: AGPL-3.0-only
"""Local execution state: the three §3.5 protocols over one SQLite file.

:class:`SqliteExecutionState` implements :class:`ExecutionLedger`,
:class:`ExecutionJournal`, and :class:`AttemptFence` for the local runtime,
in two additive tables inside the same SQLite file the v0
:class:`sanka.runtime.state.SqliteStateStore` uses. The v0 ``checkpoints``
and ``ledger`` tables are untouched — they keep serving the v0 engine until
PR F-5 swaps ``apply`` onto this family.

Semantics mirror the production repository:

- ``execution_results`` is **pair-keyed** —
  ``(run_id, source_object, destination_object, source_record_id)`` — the
  production dedupe/idempotency unit. Upserts are terminal-sticky: a row
  whose status is in :data:`~sanka.runtime.state.TERMINAL_WRITE_STATUSES`
  keeps its status, its destination id is never nulled out
  (``COALESCE``), and its message survives an incoming ``failed``. The
  persisted count is verified and a shortfall fails closed with
  ``SANKA_MIGRATE_RECORD_RESULT_BULK_SAVE_INCOMPLETE`` (the engine re-checks the
  returned count for host stores generally; the local store also enforces
  its own write).
- ``execution_journal`` holds one codec-rendered :class:`JournalEntry` per
  run plus the job/attempt owner columns the fence claims. ``save`` is
  single-owner local semantics: it returns ``"saved"`` unless a
  cancellation was recorded (then the implementation finalizes the
  cancelled terminal state and returns ``"cancelled"``) or another job or
  attempt took the row over across CLI invocations (``"superseded"``).
- The fence is a compare-and-set claim guarded by the stored
  ``(attempt_id, attempt_number)`` exactly like the production claim: an
  attempt wins when it re-claims its own id or carries a higher attempt
  number; claiming is only possible while the journal is in an active
  status (``queued``/``running``/``failed``). In a single-process CLI run
  it never loses; crash-resume across invocations gets exact attempt
  semantics for free.

The async protocol methods run the synchronous ``sqlite3`` calls on
``asyncio.to_thread`` workers (the clickhouse-connector precedent); a lock
serializes access to the shared connection.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from sanka.runtime.execution.errors import ExecutionFault
from sanka.runtime.execution.model import (
    AttemptIdentity,
    JournalEntry,
    PairFailedIds,
    PairStatusTotal,
    RecordWriteOutcome,
)
from sanka.runtime.execution.report_codec import dump_journal, load_journal
from sanka.runtime.execution.state import ClaimOutcome, SaveOutcome
from sanka.runtime.state import TERMINAL_WRITE_STATUSES

_T = TypeVar("_T")

_TERMINAL_SQL = ", ".join(f"'{status}'" for status in sorted(TERMINAL_WRITE_STATUSES))

_CLAIMABLE_STATUSES = frozenset({"queued", "running", "failed"})
"""Journal statuses an attempt may claim — the production CAS guard."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_results (
    run_id TEXT NOT NULL,
    source_object TEXT NOT NULL,
    destination_object TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    destination_record_id TEXT,
    status TEXT NOT NULL,
    message TEXT,
    PRIMARY KEY (run_id, source_object, destination_object, source_record_id)
);
CREATE TABLE IF NOT EXISTS execution_journal (
    run_id TEXT PRIMARY KEY,
    job_id TEXT,
    attempt_id TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 0,
    task_run_id TEXT,
    status TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_UPSERT_RESULTS_SQL = f"""
INSERT INTO execution_results (
    run_id, source_object, destination_object, source_record_id,
    destination_record_id, status, message
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (run_id, source_object, destination_object, source_record_id)
DO UPDATE SET
    destination_record_id = COALESCE(
        excluded.destination_record_id, execution_results.destination_record_id
    ),
    status = CASE
        WHEN execution_results.status IN ({_TERMINAL_SQL})
            THEN execution_results.status
        ELSE excluded.status
    END,
    message = CASE
        WHEN execution_results.status IN ({_TERMINAL_SQL})
             AND excluded.status = 'failed'
            THEN execution_results.message
        ELSE excluded.message
    END
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _entry_records_cancellation(status: str, entry_payload: dict[str, Any]) -> bool:
    """A cancellation is recorded on the entry status or its execution state."""

    if status == "cancelled":
        return True
    execution = entry_payload.get("execution")
    return isinstance(execution, dict) and execution.get("state") == "cancelled"


class SqliteExecutionState:
    """One run's execution ledger, journal, and attempt fence over SQLite.

    The host constructs one instance per execution and closes it over its
    pinned ``run_id`` — the protocols themselves stay scope-free. ``job_id``
    binds the journal handle to the owning continuous job; ``None`` is the
    manual single-batch owner. The same file may simultaneously back a
    :class:`sanka.runtime.state.SqliteStateStore` — the tables are disjoint.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        job_id: str | None = None,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._run_id = str(run_id)
        self._job_id = str(job_id) if job_id is not None else None
        self._claimed: AttemptIdentity | None = None
        self._lock = threading.Lock()
        # Protocol methods hop threads through asyncio.to_thread; the lock
        # serializes every transaction on this single shared connection.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def _run(self, fn: Callable[[], _T]) -> _T:
        def locked() -> _T:
            with self._lock, self._conn:
                return fn()

        return await asyncio.to_thread(locked)

    # -- ExecutionLedger ----------------------------------------------------

    async def terminal_destination_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str | None]:
        normalized_ids = list(dict.fromkeys(str(item) for item in source_record_ids if str(item)))
        if not normalized_ids:
            return {}

        def query() -> dict[str, str | None]:
            placeholders = ", ".join("?" for _ in normalized_ids)
            rows = self._conn.execute(
                "SELECT source_record_id, destination_record_id FROM execution_results"
                " WHERE run_id = ? AND source_object = ? AND destination_object = ?"
                f" AND source_record_id IN ({placeholders})"
                f" AND status IN ({_TERMINAL_SQL})",
                (self._run_id, str(source_object), str(destination_object), *normalized_ids),
            ).fetchall()
            return {
                str(row["source_record_id"]): (
                    str(row["destination_record_id"])
                    if row["destination_record_id"] is not None
                    else None
                )
                for row in rows
            }

        return await self._run(query)

    async def upsert_results(self, results: Sequence[RecordWriteOutcome]) -> int:
        if not results:
            return 0
        rows = [
            (
                self._run_id,
                str(outcome.source_object),
                str(outcome.destination_object),
                str(outcome.source_record_id),
                outcome.destination_record_id,
                str(outcome.status),
                outcome.message,
            )
            for outcome in results
        ]
        persisted = await self._run(lambda: self._upsert_rows_sync(rows))
        if persisted != len(results):
            raise ExecutionFault(
                "Sanka did not persist every destination result in the current batch.",
                code="SANKA_MIGRATE_RECORD_RESULT_BULK_SAVE_INCOMPLETE",
                details={"expectedCount": len(results), "savedCount": persisted},
            )
        return persisted

    def _upsert_rows_sync(self, rows: list[tuple[Any, ...]]) -> int:
        cursor = self._conn.executemany(_UPSERT_RESULTS_SQL, rows)
        return int(cursor.rowcount)

    async def status_totals(self) -> dict[str, int]:
        def query() -> dict[str, int]:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM execution_results"
                " WHERE run_id = ? GROUP BY status",
                (self._run_id,),
            ).fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}

        return await self._run(query)

    async def pair_status_totals(self) -> list[PairStatusTotal]:
        def query() -> list[PairStatusTotal]:
            rows = self._conn.execute(
                "SELECT source_object, destination_object, status, COUNT(*) AS count"
                " FROM execution_results WHERE run_id = ?"
                " GROUP BY source_object, destination_object, status"
                " ORDER BY source_object, destination_object, status",
                (self._run_id,),
            ).fetchall()
            return [
                PairStatusTotal(
                    source_object=str(row["source_object"]),
                    destination_object=str(row["destination_object"]),
                    status=str(row["status"]),
                    count=int(row["count"]),
                )
                for row in rows
            ]

        return await self._run(query)

    async def failed_source_ids_by_pair(self) -> list[PairFailedIds]:
        def query() -> list[PairFailedIds]:
            rows = self._conn.execute(
                "SELECT source_object, destination_object, source_record_id"
                " FROM execution_results WHERE run_id = ? AND status = 'failed'"
                " ORDER BY source_object, destination_object, source_record_id",
                (self._run_id,),
            ).fetchall()
            failed: dict[tuple[str, str], set[str]] = {}
            for row in rows:
                pair = (str(row["source_object"]), str(row["destination_object"]))
                failed.setdefault(pair, set()).add(str(row["source_record_id"]))
            return [
                PairFailedIds(
                    source_object=source_object,
                    destination_object=destination_object,
                    source_record_ids=frozenset(record_ids),
                )
                for (source_object, destination_object), record_ids in sorted(failed.items())
            ]

        return await self._run(query)

    # -- ExecutionJournal ---------------------------------------------------

    async def load(self) -> JournalEntry | None:
        def query() -> JournalEntry | None:
            row = self._journal_row_sync()
            if row is None:
                return None
            if self._job_id is not None and row["job_id"] not in (None, self._job_id):
                raise ExecutionFault(
                    "Sanka execution job was superseded.",
                    code="SANKA_MIGRATE_EXECUTION_JOB_SUPERSEDED",
                )
            return load_journal(json.loads(row["entry_json"]))

        return await self._run(query)

    async def save(self, entry: JournalEntry) -> SaveOutcome:
        def write() -> SaveOutcome:
            row = self._journal_row_sync()
            if row is not None:
                if self._job_id is not None and row["job_id"] not in (None, self._job_id):
                    return "superseded"
                bound_attempt = (
                    self._claimed.attempt_id if self._claimed is not None else entry.attempt_id
                )
                if (
                    bound_attempt is not None
                    and row["attempt_id"] not in (None, bound_attempt)
                    and int(row["attempt_number"] or 0) > 0
                ):
                    return "superseded"
                stored_payload = json.loads(row["entry_json"])
                if (
                    _entry_records_cancellation(str(row["status"]), stored_payload)
                    and entry.status != "cancelled"
                ):
                    # A cancellation landed first: finalize the cancelled
                    # terminal state, keeping the in-flight batch's work.
                    self._write_entry_sync(replace(entry, status="cancelled"))
                    return "cancelled"
            self._write_entry_sync(entry)
            return "saved"

        return await self._run(write)

    def _journal_row_sync(self) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM execution_journal WHERE run_id = ?",
            (self._run_id,),
        ).fetchone()
        return row

    def _write_entry_sync(self, entry: JournalEntry) -> None:
        attempt_number = 0
        task_run_id = None
        if self._claimed is not None and self._claimed.attempt_id == entry.attempt_id:
            attempt_number = self._claimed.attempt_number
            task_run_id = self._claimed.task_run_id
        else:
            row = self._journal_row_sync()
            if row is not None and row["attempt_id"] == entry.attempt_id:
                attempt_number = int(row["attempt_number"] or 0)
                task_run_id = row["task_run_id"]
        self._conn.execute(
            "INSERT INTO execution_journal"
            " (run_id, job_id, attempt_id, attempt_number, task_run_id, status,"
            " entry_json, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (run_id) DO UPDATE SET"
            " job_id = excluded.job_id, attempt_id = excluded.attempt_id,"
            " attempt_number = excluded.attempt_number,"
            " task_run_id = excluded.task_run_id, status = excluded.status,"
            " entry_json = excluded.entry_json, updated_at = excluded.updated_at",
            (
                self._run_id,
                entry.job_id,
                entry.attempt_id,
                attempt_number,
                task_run_id,
                entry.status,
                json.dumps(dump_journal(entry)),
                _now(),
            ),
        )

    # -- AttemptFence -------------------------------------------------------

    async def claim(self, attempt: AttemptIdentity) -> tuple[ClaimOutcome, JournalEntry | None]:
        def cas() -> tuple[ClaimOutcome, JournalEntry | None]:
            row = self._journal_row_sync()
            if row is None:
                # Nothing queued yet: the single-process runtime claims
                # trivially; the attempt is persisted on the first save.
                self._claimed = attempt
                return "claimed", None
            stored_payload = json.loads(row["entry_json"])
            entry = load_journal(stored_payload)
            if _entry_records_cancellation(str(row["status"]), stored_payload):
                return "cancelled", entry
            if self._job_id is not None and row["job_id"] not in (None, self._job_id):
                return "superseded", None
            if str(row["status"]) not in _CLAIMABLE_STATUSES:
                return "superseded", None
            stored_attempt_id = row["attempt_id"]
            stored_attempt_number = int(row["attempt_number"] or 0)
            if not (
                stored_attempt_id in (None, attempt.attempt_id)
                or stored_attempt_number < attempt.attempt_number
            ):
                return "superseded", None
            entry.attempt_id = attempt.attempt_id
            entry.last_heartbeat_at = _now()
            self._conn.execute(
                "UPDATE execution_journal SET attempt_id = ?, attempt_number = ?,"
                " task_run_id = ?, entry_json = ?, updated_at = ? WHERE run_id = ?",
                (
                    attempt.attempt_id,
                    attempt.attempt_number,
                    attempt.task_run_id,
                    json.dumps(dump_journal(entry)),
                    _now(),
                    self._run_id,
                ),
            )
            self._claimed = attempt
            return "claimed", entry

        return await self._run(cas)
