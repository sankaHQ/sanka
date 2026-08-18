# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pathlib import Path

import pytest

from sanka.runtime.state import LedgerEntry, RunStatus, SqliteStateStore, StateError


def _store(tmp_path: Path) -> SqliteStateStore:
    return SqliteStateStore(tmp_path / "state.db")


def test_run_lifecycle_and_lookup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.create_run(run_id="r1", spec_json="{}", spec_hash="sha256:a", name=None)
    assert run.status is RunStatus.CREATED

    store.save_inspection("r1", "{}")
    store.save_plan("r1", "{}", "sha256:plan")
    reloaded = store.get_run("r1")
    assert reloaded.status is RunStatus.PLANNED
    assert reloaded.plan_hash == "sha256:plan"

    assert store.find_latest_run("sha256:a") is not None
    assert store.find_latest_run("sha256:other") is None
    with pytest.raises(StateError):
        store.get_run("missing")
    with pytest.raises(StateError):
        store.set_status("missing", RunStatus.FAILED)


def test_checkpoints_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_run(run_id="r1", spec_json="{}", spec_hash="sha256:a", name=None)
    assert store.get_checkpoint("r1", "docs->docs") == (None, False)
    store.save_checkpoint("r1", "docs->docs", "42", False)
    assert store.get_checkpoint("r1", "docs->docs") == ("42", False)
    store.save_checkpoint("r1", "docs->docs", None, True)
    assert store.get_checkpoint("r1", "docs->docs") == (None, True)


def test_ledger_upsert_terminal_filtering_and_summary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_run(run_id="r1", spec_json="{}", spec_hash="sha256:a", name=None)
    route = "docs->docs"
    store.record_results(
        "r1",
        route,
        [
            LedgerEntry(source_record_id="a", status="failed", message="boom"),
            LedgerEntry(source_record_id="b", status="created", destination_record_id="1"),
            LedgerEntry(source_record_id="c", status="skipped"),
        ],
    )
    assert store.terminal_source_ids("r1", route) == {"b", "c"}

    # A retried record moves failed -> created via upsert.
    store.record_results(
        "r1",
        route,
        [LedgerEntry(source_record_id="a", status="created", destination_record_id="2")],
    )
    assert store.terminal_source_ids("r1", route) == {"a", "b", "c"}
    assert store.ledger_summary("r1") == {route: {"created": 2, "skipped": 1}}
