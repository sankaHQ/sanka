# SPDX-License-Identifier: AGPL-3.0-only
"""Durable local Flow installation ledger with short transactions and fencing."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, cast

from sanka.runtime.flow.model import (
    Claim,
    Document,
    FlowError,
    FlowPlan,
    Installation,
    OperationReceipt,
    OwnedResource,
)
from sanka.runtime.hashing import canonical_json
from sanka.runtime.private_sqlite import connect_private_sqlite

_T = TypeVar("_T")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_installations (
    id TEXT PRIMARY KEY, target TEXT NOT NULL, revision INTEGER NOT NULL,
    resources TEXT NOT NULL, active_plan TEXT, attempt TEXT,
    generation INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS flow_plans (
    installation_id TEXT NOT NULL, digest TEXT NOT NULL, document TEXT NOT NULL,
    construction TEXT, abandoned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (installation_id, digest)
);
CREATE TABLE IF NOT EXISTS flow_operations (
    installation_id TEXT NOT NULL, plan_digest TEXT NOT NULL, operation_id TEXT NOT NULL,
    request TEXT NOT NULL, request_digest TEXT NOT NULL, receipt TEXT,
    PRIMARY KEY (installation_id, plan_digest, operation_id)
);
CREATE TABLE IF NOT EXISTS flow_evidence (
    installation_id TEXT NOT NULL, plan_digest TEXT NOT NULL, kind TEXT NOT NULL,
    document TEXT NOT NULL, PRIMARY KEY (installation_id, plan_digest, kind)
);
"""


class SqliteInstallationStore:
    """One installation and target; reopening cannot reinterpret its identity.

    A lease allows recovery after an interrupted process. A newer claim fences
    every old ledger write. Target adapters must enforce this same claim at their
    mutation boundary; the ledger alone cannot fence a remote side effect.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        installation_id: str,
        target: str,
        lease_seconds: float = 120,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not installation_id or not target or lease_seconds <= 0:
            raise ValueError("Installation, target and positive lease duration are required")
        self._id, self._target = installation_id, target
        self._clock, self._lease_seconds = clock, lease_seconds
        self._lock = threading.Lock()
        self._db = connect_private_sqlite(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        try:
            self._transaction(self._initialize)
        except BaseException:
            self._db.close()
            raise

    def _initialize(self) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO flow_installations (id,target,revision,resources) "
            "VALUES (?,?,0,'[]')",
            (self._id, self._target),
        )
        self._row()

    def _transaction(self, operation: Callable[[], _T]) -> _T:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                result = operation()
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    async def _run(self, operation: Callable[[], _T]) -> _T:
        return await asyncio.to_thread(self._transaction, operation)

    def _row(self) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM flow_installations WHERE id=?", (self._id,)
        ).fetchone()
        if row is None or row["target"] != self._target:
            raise FlowError("FLOW_TARGET_MISMATCH", "Installation identity changed")
        return cast(sqlite3.Row, row)

    def _load(self) -> Installation:
        row = self._row()
        return Installation(
            self._id,
            self._target,
            row["revision"],
            tuple(OwnedResource.from_dict(r) for r in json.loads(row["resources"])),
        )

    async def load(self) -> Installation:
        return await self._run(self._load)

    def _plan(self, digest: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM flow_plans WHERE installation_id=? AND digest=?", (self._id, digest)
        ).fetchone()
        if row is None:
            raise FlowError("FLOW_PLAN_MISSING", "Persisted reviewed plan is missing")
        return cast(sqlite3.Row, row)

    async def save_plan(self, plan: FlowPlan) -> None:
        def save() -> None:
            if plan.to_dict()["installation"] != self._load().to_dict():
                raise FlowError("FLOW_INSTALLATION_STALE", "Installation changed after planning")
            self._db.execute(
                "INSERT OR IGNORE INTO flow_plans (installation_id,digest,document) VALUES (?,?,?)",
                (self._id, plan.digest, plan.document.text),
            )
            if self._plan(plan.digest)["document"] != plan.document.text:
                raise FlowError("FLOW_PLAN_CONFLICT", "Plan digest already holds different content")

        await self._run(save)

    async def claim(self, plan_digest: str, attempt_id: str) -> Claim:
        if not attempt_id:
            raise ValueError("Attempt identity is required")

        def acquire() -> Claim:
            plan = self._plan(plan_digest)
            if plan["abandoned"]:
                raise FlowError("FLOW_PLAN_DISCARDED", "Discarded plan requires fresh planning")
            planned = json.loads(plan["document"])
            if planned["blockers"]:
                raise FlowError("FLOW_PLAN_BLOCKED", "Resolve plan blockers before construction")
            row = self._row()
            if row["active_plan"] not in (None, plan_digest):
                raise FlowError(
                    "FLOW_PLAN_IN_PROGRESS", "Resume the previous plan before replacing it"
                )
            if row["attempt"] not in (None, attempt_id) and row["lease_until"] > self._clock():
                raise FlowError("FLOW_BUSY", "Another attempt holds the installation lease")
            if not plan["construction"]:
                expected = json.loads(plan["document"])["installation"]
                if expected != self._load().to_dict():
                    raise FlowError(
                        "FLOW_INSTALLATION_STALE", "Installation changed after approval"
                    )
            else:
                constructed = json.loads(plan["construction"])
                if constructed["installation"] != self._load().to_dict():
                    raise FlowError(
                        "FLOW_INSTALLATION_STALE", "A later plan replaced this construction"
                    )
            generation = row["generation"] + (
                row["attempt"] != attempt_id or row["lease_until"] <= self._clock()
            )
            self._db.execute(
                "UPDATE flow_installations SET active_plan=?,attempt=?,generation=?,lease_until=? "
                "WHERE id=?",
                (
                    plan_digest,
                    attempt_id,
                    generation,
                    self._clock() + self._lease_seconds,
                    self._id,
                ),
            )
            return Claim(plan_digest, attempt_id, generation)

        return await self._run(acquire)

    def _assert(self, claim: Claim) -> None:
        row = self._row()
        if (row["active_plan"], row["attempt"], row["generation"]) != (
            claim.plan_digest,
            claim.attempt_id,
            claim.generation,
        ) or row["lease_until"] <= self._clock():
            raise FlowError("FLOW_ATTEMPT_FENCED", "Installation attempt was superseded or expired")

    async def assert_claim(self, claim: Claim) -> None:
        await self._run(lambda: self._assert(claim))

    async def begin_operation(self, claim: Claim, operation: Document) -> OperationReceipt | None:
        def begin() -> OperationReceipt | None:
            self._assert(claim)
            payload = operation.to_dict()
            planned = json.loads(self._plan(claim.plan_digest)["document"])
            expected = next((op for op in planned["operations"] if op["id"] == payload["id"]), None)
            if expected is None or canonical_json(expected) != operation.text:
                raise FlowError(
                    "FLOW_IDEMPOTENCY_CONFLICT", "Operation differs from its reviewed plan"
                )
            self._db.execute(
                "INSERT OR IGNORE INTO flow_operations "
                "(installation_id,plan_digest,operation_id,request,request_digest) "
                "VALUES (?,?,?,?,?)",
                (self._id, claim.plan_digest, payload["id"], operation.text, operation.digest),
            )
            row = self._operation(claim, payload["id"])
            if row["request"] != operation.text:
                raise FlowError(
                    "FLOW_IDEMPOTENCY_CONFLICT", "Operation identity has different content"
                )
            return (
                OperationReceipt.from_dict(json.loads(row["receipt"])) if row["receipt"] else None
            )

        return await self._run(begin)

    def _operation(self, claim: Claim, operation_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM flow_operations "
            "WHERE installation_id=? AND plan_digest=? AND operation_id=?",
            (self._id, claim.plan_digest, operation_id),
        ).fetchone()
        if row is None:
            raise FlowError("FLOW_INTENT_MISSING", "Operation has no durable intent")
        return cast(sqlite3.Row, row)

    async def complete_operation(self, claim: Claim, receipt: OperationReceipt) -> None:
        def complete() -> None:
            self._assert(claim)
            row = self._operation(claim, receipt.operation_id)
            rendered = canonical_json(receipt.to_dict())
            if row["request_digest"] != receipt.request_digest:
                raise FlowError(
                    "FLOW_RECEIPT_MISMATCH", "Receipt does not match the intended request"
                )
            if row["receipt"] and row["receipt"] != rendered:
                raise FlowError("FLOW_RECEIPT_CONFLICT", "Operation already has another result")
            self._db.execute(
                "UPDATE flow_operations SET receipt=? "
                "WHERE installation_id=? AND plan_digest=? AND operation_id=?",
                (rendered, self._id, claim.plan_digest, receipt.operation_id),
            )

        await self._run(complete)

    async def receipts(self, plan_digest: str) -> tuple[OperationReceipt, ...]:
        def read() -> tuple[OperationReceipt, ...]:
            rows = self._db.execute(
                "SELECT receipt FROM flow_operations WHERE installation_id=? AND plan_digest=? "
                "AND receipt IS NOT NULL ORDER BY operation_id",
                (self._id, plan_digest),
            )
            return tuple(OperationReceipt.from_dict(json.loads(r["receipt"])) for r in rows)

        return await self._run(read)

    async def construction(self, plan_digest: str) -> Document | None:
        def read() -> Document | None:
            value = self._plan(plan_digest)["construction"]
            return Document(json.loads(value)) if value else None

        return await self._run(read)

    async def finish_construction(
        self, claim: Claim, resources: tuple[OwnedResource, ...], observation: Document
    ) -> Installation:
        def finish() -> Installation:
            self._assert(claim)
            existing = self._plan(claim.plan_digest)["construction"]
            if existing:
                result = json.loads(existing)
                if result["observation"] != observation.to_dict():
                    raise FlowError("FLOW_TARGET_STALE", "Constructed observation changed")
                return Installation.from_dict(result["installation"])
            planned = json.loads(self._plan(claim.plan_digest)["document"])
            for operation in planned["operations"]:
                if (
                    operation["action"] != "preserve"
                    and not self._operation(claim, operation["id"])["receipt"]
                ):
                    raise FlowError(
                        "FLOW_CONSTRUCTION_INCOMPLETE", "A planned mutation has no result"
                    )
            installation = Installation(
                self._id, self._target, self._row()["revision"] + 1, resources
            )
            result = {"installation": installation.to_dict(), "observation": observation.to_dict()}
            self._db.execute(
                "UPDATE flow_installations SET revision=?,resources=? WHERE id=?",
                (
                    installation.revision,
                    canonical_json(installation.to_dict()["resources"]),
                    self._id,
                ),
            )
            self._db.execute(
                "UPDATE flow_plans SET construction=? WHERE installation_id=? AND digest=?",
                (canonical_json(result), self._id, claim.plan_digest),
            )
            return installation

        return await self._run(finish)

    async def save_evidence(self, claim: Claim, kind: str, evidence: Document) -> None:
        if kind not in {"verification", "activation_intent", "activation"}:
            raise ValueError("Unknown evidence kind")

        def save() -> None:
            self._assert(claim)
            self._db.execute(
                "INSERT OR IGNORE INTO flow_evidence (installation_id,plan_digest,kind,document) "
                "VALUES (?,?,?,?)",
                (self._id, claim.plan_digest, kind, evidence.text),
            )
            row = self._db.execute(
                "SELECT document FROM flow_evidence "
                "WHERE installation_id=? AND plan_digest=? AND kind=?",
                (self._id, claim.plan_digest, kind),
            ).fetchone()
            if row["document"] != evidence.text:
                raise FlowError(
                    "FLOW_EVIDENCE_CONFLICT", "Evidence already exists with different content"
                )

        await self._run(save)

    async def evidence(self, plan_digest: str, kind: str) -> Document | None:
        def read() -> Document | None:
            row = self._db.execute(
                "SELECT document FROM flow_evidence "
                "WHERE installation_id=? AND plan_digest=? AND kind=?",
                (self._id, plan_digest, kind),
            ).fetchone()
            return Document(json.loads(row["document"])) if row else None

        return await self._run(read)

    async def release(self, claim: Claim) -> None:
        def release() -> None:
            self._assert(claim)
            # Incomplete construction or uncertain activation keeps this plan
            # selected. A newer plan must not race a lost activation response.
            completed = self._plan(claim.plan_digest)["construction"] is not None
            kinds = {
                row["kind"]
                for row in self._db.execute(
                    "SELECT kind FROM flow_evidence WHERE installation_id=? AND plan_digest=?",
                    (self._id, claim.plan_digest),
                )
            }
            pending_activation = "activation_intent" in kinds and "activation" not in kinds
            self._db.execute(
                "UPDATE flow_installations SET active_plan=?,attempt=NULL,lease_until=0 WHERE id=?",
                (None if completed and not pending_activation else claim.plan_digest, self._id),
            )

        await self._run(release)

    async def abandon(self, claim: Claim) -> None:
        def abandon() -> None:
            self._assert(claim)
            plan = self._plan(claim.plan_digest)
            has_results = self._db.execute(
                "SELECT 1 FROM flow_operations WHERE installation_id=? AND plan_digest=? "
                "AND receipt IS NOT NULL LIMIT 1",
                (self._id, claim.plan_digest),
            ).fetchone()
            has_evidence = self._db.execute(
                "SELECT 1 FROM flow_evidence WHERE installation_id=? AND plan_digest=? LIMIT 1",
                (self._id, claim.plan_digest),
            ).fetchone()
            if plan["construction"] or has_results or has_evidence:
                raise FlowError("FLOW_PLAN_HAS_EFFECTS", "Constructed work must be recovered")
            self._db.execute(
                "UPDATE flow_plans SET abandoned=1 WHERE installation_id=? AND digest=?",
                (self._id, claim.plan_digest),
            )
            self._db.execute(
                "UPDATE flow_installations SET active_plan=NULL,attempt=NULL,lease_until=0 "
                "WHERE id=?",
                (self._id,),
            )

        await self._run(abandon)

    async def close(self) -> None:
        def close() -> None:
            with self._lock:
                self._db.close()

        await asyncio.to_thread(close)
