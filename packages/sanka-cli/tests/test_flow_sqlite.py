# SPDX-License-Identifier: AGPL-3.0-only
"""Crash recovery, immutable operation receipts and concurrent installation claims."""

from pathlib import Path

import pytest
from test_flow_planner import Blueprint, Resource, snapshot

from sanka.runtime.flow.model import (
    Document,
    FlowError,
    Installation,
    ObservedResource,
    OperationReceipt,
    OwnedResource,
)
from sanka.runtime.flow.planner import plan_reconstruction
from sanka.runtime.flow.sqlite import SqliteInstallationStore


async def test_reopen_preserves_intent_receipt_and_installation(tmp_path: Path) -> None:
    path = tmp_path / "private" / "flow.db"
    store = SqliteInstallationStore(path, installation_id="install", target="sanka:test-workspace")
    plan = plan_reconstruction(
        blueprint=Blueprint((Resource("sales", "workflow", {"name": "Sales"}),)),
        installation=await store.load(),
        observed=snapshot(),
    )
    await store.save_plan(plan)
    claim = await store.claim(plan.digest, "attempt-one")
    operation = Document(plan.to_dict()["operations"][0])
    assert await store.begin_operation(claim, operation) is None
    resource = ObservedResource("w1", "workflow", "r2", Document({"name": "Sales"}))
    receipt = OperationReceipt(
        operation.to_dict()["id"], operation.digest, resource, "workspace-r2"
    )
    await store.complete_operation(claim, receipt)
    await store.release(claim)
    await store.close()

    reopened = SqliteInstallationStore(
        path, installation_id="install", target="sanka:test-workspace"
    )
    resumed = await reopened.claim(plan.digest, "attempt-two")
    assert resumed.generation > claim.generation
    assert await reopened.begin_operation(resumed, operation) == receipt
    owned = (OwnedResource("sales", "w1", "workflow", "r2", Document({"name": "Sales"})),)
    result = await reopened.finish_construction(
        resumed, owned, Document(snapshot(resource).to_dict())
    )
    assert result.revision == 1
    assert (await reopened.load()).resources == owned
    construction = await reopened.construction(plan.digest)
    assert construction is not None
    assert construction.to_dict()["installation"] == result.to_dict()
    await reopened.release(resumed)
    await reopened.close()


async def test_live_claim_blocks_other_worker_then_expired_claim_is_fenced(tmp_path: Path) -> None:
    now = [1.0]
    first = SqliteInstallationStore(
        tmp_path / "private" / "flow.db",
        installation_id="install",
        target="sanka:test-workspace",
        clock=lambda: now[0],
    )
    second = SqliteInstallationStore(
        tmp_path / "private" / "flow.db",
        installation_id="install",
        target="sanka:test-workspace",
        clock=lambda: now[0],
    )
    plan = plan_reconstruction(
        blueprint=Blueprint(()), installation=await first.load(), observed=snapshot()
    )
    await first.save_plan(plan)
    claim = await first.claim(plan.digest, "first")
    with pytest.raises(FlowError, match="lease"):
        await second.claim(plan.digest, "second")
    now[0] = 122
    replacement = await second.claim(plan.digest, "second")
    assert replacement.generation > claim.generation
    with pytest.raises(FlowError, match="superseded"):
        await first.assert_claim(claim)
    await first.close()
    await second.close()


async def test_same_attempt_reclaim_after_expiry_changes_generation(tmp_path: Path) -> None:
    now = [1.0]
    store = SqliteInstallationStore(
        tmp_path / "private" / "flow.db",
        installation_id="install",
        target="sanka:test-workspace",
        clock=lambda: now[0],
    )
    plan = plan_reconstruction(
        blueprint=Blueprint(()), installation=await store.load(), observed=snapshot()
    )
    await store.save_plan(plan)
    old = await store.claim(plan.digest, "attempt")
    now[0] = 122
    new = await store.claim(plan.digest, "attempt")
    assert new.generation > old.generation
    with pytest.raises(FlowError):
        await store.assert_claim(old)
    await store.close()


async def test_unfinished_plan_cannot_be_replaced_after_release(tmp_path: Path) -> None:
    store = SqliteInstallationStore(
        tmp_path / "private" / "flow.db", installation_id="install", target="sanka:test-workspace"
    )
    first = plan_reconstruction(
        blueprint=Blueprint(()), installation=await store.load(), observed=snapshot()
    )
    second = plan_reconstruction(
        blueprint=Blueprint(()), installation=await store.load(), observed=snapshot(revision="new")
    )
    await store.save_plan(first)
    await store.save_plan(second)
    claim = await store.claim(first.digest, "attempt")
    await store.release(claim)
    with pytest.raises(FlowError, match="previous plan"):
        await store.claim(second.digest, "next")
    await store.close()


async def test_receipt_requires_exact_reviewed_intent_and_cannot_change(tmp_path: Path) -> None:
    store = SqliteInstallationStore(
        tmp_path / "private" / "flow.db", installation_id="install", target="sanka:test-workspace"
    )
    plan = plan_reconstruction(
        blueprint=Blueprint((Resource("sales", "workflow", {}),)),
        installation=await store.load(),
        observed=snapshot(),
    )
    await store.save_plan(plan)
    claim = await store.claim(plan.digest, "attempt")
    operation = Document(plan.to_dict()["operations"][0])
    changed = operation.to_dict()
    changed["configuration"] = {"different": True}
    with pytest.raises(FlowError, match="reviewed plan"):
        await store.begin_operation(claim, Document(changed))
    await store.begin_operation(claim, operation)
    receipt = OperationReceipt(operation.to_dict()["id"], operation.digest, None, "r2")
    await store.complete_operation(claim, receipt)
    with pytest.raises(FlowError, match="another result"):
        await store.complete_operation(
            claim, OperationReceipt(receipt.operation_id, receipt.request_digest, None, "r3")
        )
    await store.close()


async def test_stale_plan_and_different_target_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "private" / "flow.db"
    store = SqliteInstallationStore(path, installation_id="install", target="sanka:test-workspace")
    wrong = plan_reconstruction(
        blueprint=Blueprint(()),
        installation=Installation("other", "sanka:test-workspace"),
        observed=snapshot(),
    )
    with pytest.raises(FlowError, match="changed after planning"):
        await store.save_plan(wrong)
    await store.close()
    with pytest.raises(FlowError, match="identity changed"):
        SqliteInstallationStore(path, installation_id="install", target="sanka:other-workspace")
