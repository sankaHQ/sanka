# SPDX-License-Identifier: AGPL-3.0-only
"""Run state: the store protocol and the SQLite reference implementation.

The lifecycle tables carry a migration run:

- **runs** — spec (canonical JSON + hash), lifecycle status, the persisted
  inspection and plan documents (plan hash included — approvals bind to it).
- **checkpoints** — per-route resume cursor and completion flag (legacy;
  the engine now checkpoints through the execution journal).
- **ledger** — one row per (route, source record). **Deprecated**: the
  engine's durable per-record results moved to the pair-keyed
  ``execution_results`` table owned by
  :class:`sanka.runtime.execution.local.SqliteExecutionState` (v0 routes
  are filter-less, so a route and its object pair name the same records).
  The table is still created and readable — and rows written by an older
  release keep surfacing through :meth:`SqliteStateStore.ledger_summary` /
  :meth:`SqliteStateStore.terminal_source_ids` — for one release after the
  engine swap, after which it becomes purely historical.

Embedders (e.g. a hosted control plane) implement :class:`StateStore` over
their own persistence; the SQLite store is the local-runtime default and
also hands the engine its execution-state family (ledger/journal/fence)
over the same file via :meth:`SqliteStateStore.execution_state`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sanka.runtime.mapping.record_mapping import mapping_group_key
from sanka.runtime.private_sqlite import connect_private_sqlite
from sanka_data.records import BatchWriteStatus

if TYPE_CHECKING:
    from sanka.runtime.execution.local import SqliteExecutionState

TERMINAL_WRITE_STATUSES: frozenset[str] = frozenset({"created", "updated", "skipped"})


class RunStatus(StrEnum):
    CREATED = "created"
    INSPECTED = "inspected"
    PLANNED = "planned"
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"
    CANCELLED = "cancelled"
    """A cancellation recorded by the execution journal won the run.

    Additive for the engine's execution-family adoption: no v0 flow issues a
    cancellation itself; the engine maps a journal that already records one
    onto this lifecycle status at the apply boundary.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class RunRecord:
    id: str
    name: str | None
    spec_json: str
    spec_hash: str
    status: RunStatus
    inspection_json: str | None
    plan_json: str | None
    plan_hash: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class LedgerEntry:
    source_record_id: str
    status: BatchWriteStatus
    destination_record_id: str | None = None
    message: str | None = None


class StateError(RuntimeError):
    """The store was asked for something inconsistent with its state."""


@runtime_checkable
class StateStore(Protocol):
    def create_run(
        self, *, run_id: str, spec_json: str, spec_hash: str, name: str | None
    ) -> RunRecord: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def find_latest_run(self, spec_hash: str) -> RunRecord | None: ...

    def set_status(self, run_id: str, status: RunStatus) -> None: ...

    def save_inspection(self, run_id: str, inspection_json: str) -> None: ...

    def save_plan(self, run_id: str, plan_json: str, plan_hash: str) -> None: ...

    def get_checkpoint(self, run_id: str, route_key: str) -> tuple[str | None, bool]:
        """Deprecated: the engine resumes from the execution journal now."""
        ...

    def save_checkpoint(self, run_id: str, route_key: str, cursor: str | None, done: bool) -> None:
        """Deprecated: the engine checkpoints through the execution journal now."""
        ...

    def record_results(self, run_id: str, route_key: str, entries: list[LedgerEntry]) -> None:
        """Deprecated: the engine persists results through the pair-keyed
        execution ledger (:class:`sanka.runtime.execution.ExecutionLedger`)."""
        ...

    def terminal_source_ids(self, run_id: str, route_key: str) -> set[str]:
        """Deprecated: engine-internal reads moved to the execution ledger's
        ``terminal_destination_ids``; kept for embedder compatibility."""
        ...

    def ledger_summary(self, run_id: str) -> dict[str, dict[str, int]]:
        """Deprecated: engine-internal reads moved to the execution ledger's
        ``pair_status_totals``; kept for embedder compatibility."""
        ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    name TEXT,
    spec_json TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    inspection_json TEXT,
    plan_json TEXT,
    plan_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_spec_hash ON runs (spec_hash, created_at);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL,
    route_key TEXT NOT NULL,
    cursor TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, route_key)
);
CREATE TABLE IF NOT EXISTS ledger (
    run_id TEXT NOT NULL,
    route_key TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    destination_record_id TEXT,
    status TEXT NOT NULL,
    message TEXT,
    PRIMARY KEY (run_id, route_key, source_record_id)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SqliteStateStore:
    """Single-process local state store backed by one SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn = connect_private_sqlite(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def execution_state(self, run_id: str, *, job_id: str | None = None) -> SqliteExecutionState:
        """The run's execution-state family (ledger/journal/fence) over this file.

        Implements the engine's execution-state seam: the pair-keyed
        ``execution_results`` ledger and the fenced ``execution_journal``
        live in additive tables inside the same SQLite file (see
        :mod:`sanka.runtime.execution.local`). Callers own the returned
        handle and must ``close()`` it.
        """
        from sanka.runtime.execution.local import SqliteExecutionState

        return SqliteExecutionState(self._path, run_id=run_id, job_id=job_id)

    # -- runs ---------------------------------------------------------------

    def create_run(
        self, *, run_id: str, spec_json: str, spec_hash: str, name: str | None
    ) -> RunRecord:
        json.loads(spec_json)  # must already be valid canonical JSON
        now = _now()
        self._conn.execute(
            "INSERT INTO runs (id, name, spec_json, spec_hash, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, name, spec_json, spec_hash, RunStatus.CREATED, now, now),
        )
        self._conn.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise StateError(f"run {run_id!r} does not exist")
        return _run_from_row(row)

    def find_latest_run(self, spec_hash: str) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE spec_hash = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (spec_hash,),
        ).fetchone()
        return None if row is None else _run_from_row(row)

    def set_status(self, run_id: str, status: RunStatus) -> None:
        self._touch(run_id, "status = ?", (status,))

    def save_inspection(self, run_id: str, inspection_json: str) -> None:
        self._touch(
            run_id,
            "inspection_json = ?, status = ?",
            (inspection_json, RunStatus.INSPECTED),
        )

    def save_plan(self, run_id: str, plan_json: str, plan_hash: str) -> None:
        self._touch(
            run_id,
            "plan_json = ?, plan_hash = ?, status = ?",
            (plan_json, plan_hash, RunStatus.PLANNED),
        )

    def _touch(self, run_id: str, assignments: str, params: tuple[object, ...]) -> None:
        updated = self._conn.execute(
            f"UPDATE runs SET {assignments}, updated_at = ? WHERE id = ?",
            (*params, _now(), run_id),
        )
        if updated.rowcount == 0:
            raise StateError(f"run {run_id!r} does not exist")
        self._conn.commit()

    # -- checkpoints --------------------------------------------------------

    def get_checkpoint(self, run_id: str, route_key: str) -> tuple[str | None, bool]:
        row = self._conn.execute(
            "SELECT cursor, done FROM checkpoints WHERE run_id = ? AND route_key = ?",
            (run_id, route_key),
        ).fetchone()
        if row is None:
            return None, False
        return row["cursor"], bool(row["done"])

    def save_checkpoint(self, run_id: str, route_key: str, cursor: str | None, done: bool) -> None:
        self._conn.execute(
            "INSERT INTO checkpoints (run_id, route_key, cursor, done) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (run_id, route_key) DO UPDATE SET cursor = ?, done = ?",
            (run_id, route_key, cursor, int(done), cursor, int(done)),
        )
        self._conn.commit()

    # -- ledger (deprecated: superseded by the pair-keyed execution ledger) --

    def record_results(self, run_id: str, route_key: str, entries: list[LedgerEntry]) -> None:
        """Deprecated: the engine writes through the execution ledger now.

        Kept fully functional for one release so external callers and the
        revert path keep a working route-keyed ledger.
        """
        self._conn.executemany(
            "INSERT INTO ledger"
            " (run_id, route_key, source_record_id, destination_record_id, status, message)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (run_id, route_key, source_record_id)"
            " DO UPDATE SET destination_record_id = excluded.destination_record_id,"
            " status = excluded.status, message = excluded.message",
            [
                (
                    run_id,
                    route_key,
                    e.source_record_id,
                    e.destination_record_id,
                    e.status,
                    e.message,
                )
                for e in entries
            ],
        )
        self._conn.commit()

    def terminal_source_ids(self, run_id: str, route_key: str) -> set[str]:
        """Deprecated read, kept truthful across the ledger migration.

        Unions the legacy route-keyed rows with the pair-keyed execution
        ledger (a v0 route key is ``source|destination``, so the pair is
        recoverable from the key) — runs applied before and after the engine
        swap both resolve.
        """
        rows = self._conn.execute(
            "SELECT source_record_id FROM ledger"
            " WHERE run_id = ? AND route_key = ? AND status IN ('created', 'updated', 'skipped')",
            (run_id, route_key),
        ).fetchall()
        ids = {str(row["source_record_id"]) for row in rows}
        pair = route_key.split("|")
        if len(pair) >= 2:
            try:
                pair_rows = self._conn.execute(
                    "SELECT source_record_id FROM execution_results"
                    " WHERE run_id = ? AND source_object = ? AND destination_object = ?"
                    " AND status IN ('created', 'updated', 'skipped')",
                    (run_id, pair[0], pair[1]),
                ).fetchall()
            except sqlite3.OperationalError:  # execution tables not created yet
                pair_rows = []
            ids.update(str(row["source_record_id"]) for row in pair_rows)
        return ids

    def ledger_summary(self, run_id: str) -> dict[str, dict[str, int]]:
        """Deprecated read, kept truthful across the ledger migration.

        Routes the engine has written through the pair-keyed execution
        ledger report from there (the durable authority); routes only the
        legacy ``ledger`` table knows (state files from before the engine
        swap) keep reporting their historical counts.
        """
        rows = self._conn.execute(
            "SELECT route_key, status, COUNT(*) AS n FROM ledger"
            " WHERE run_id = ? GROUP BY route_key, status",
            (run_id,),
        ).fetchall()
        summary: dict[str, dict[str, int]] = {}
        for row in rows:
            summary.setdefault(row["route_key"], {})[row["status"]] = row["n"]
        summary.update(self._execution_result_summary(run_id))
        return summary

    def _execution_result_summary(self, run_id: str) -> dict[str, dict[str, int]]:
        """Per-route counts from the pair-keyed execution ledger.

        v0 plans emit filter-less routes (route key ``source|destination``),
        so each object pair maps back to exactly one plan route key.
        """
        try:
            rows = self._conn.execute(
                "SELECT source_object, destination_object, status, COUNT(*) AS n"
                " FROM execution_results WHERE run_id = ?"
                " GROUP BY source_object, destination_object, status",
                (run_id,),
            ).fetchall()
        except sqlite3.OperationalError:  # execution tables not created yet
            return {}
        summary: dict[str, dict[str, int]] = {}
        for row in rows:
            route_key = mapping_group_key(
                str(row["source_object"]), str(row["destination_object"]), None
            )
            summary.setdefault(route_key, {})[str(row["status"])] = int(row["n"])
        return summary


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        name=row["name"],
        spec_json=row["spec_json"],
        spec_hash=row["spec_hash"],
        status=RunStatus(row["status"]),
        inspection_json=row["inspection_json"],
        plan_json=row["plan_json"],
        plan_hash=row["plan_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
