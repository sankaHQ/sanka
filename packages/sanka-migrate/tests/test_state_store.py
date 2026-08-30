# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
import sqlite3
import stat
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from sanka.runtime.private_sqlite import connect_private_sqlite
from sanka.runtime.state import LedgerEntry, RunStatus, SqliteStateStore, StateError


def _store(tmp_path: Path) -> SqliteStateStore:
    return SqliteStateStore(tmp_path / "state.db")


def _hold_private_state_lock(path: str, control: Connection) -> None:
    connection = connect_private_sqlite(path)
    control.send("ready")
    control.recv()
    connection.close()


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


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_state_database_is_private(tmp_path: Path) -> None:
    state_dir = tmp_path / "nested" / "state"
    store = SqliteStateStore(state_dir / "state.db")
    store.close()
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((state_dir / "state.db").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_shared_state_directory_is_rejected_without_chmod(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(PermissionError, match="group/world"):
        SqliteStateStore(shared / "state.db")
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


def test_state_database_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target.write_text("sentinel", encoding="utf-8")
    link = tmp_path / "state.db"
    link.symlink_to(target)
    with pytest.raises(OSError, match="symbolic link"):
        SqliteStateStore(link)
    assert target.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_state_database_sidecar_symlink_is_rejected(suffix: str, tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target.write_text("sentinel", encoding="utf-8")
    sidecar = Path(f"{tmp_path / 'state.db'}{suffix}")
    sidecar.symlink_to(target)

    with pytest.raises(OSError, match="symbolic link"):
        SqliteStateStore(tmp_path / "state.db")

    assert target.read_text(encoding="utf-8") == "sentinel"


def test_state_directory_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "state"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match="symbolic link"):
        SqliteStateStore(link / "state.db")
    assert not (target / "state.db").exists()


def test_state_connection_never_opens_canonical_path(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    connection = connect_private_sqlite(state_path)
    opened_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    connection.execute("CREATE TABLE bound (value TEXT)")
    connection.commit()
    connection.close()

    assert state_path.exists()
    assert opened_path != state_path


def test_second_process_cannot_open_the_same_private_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    context = get_context("spawn")
    parent_control, child_control = context.Pipe()
    process = context.Process(
        target=_hold_private_state_lock,
        args=(str(state_path), child_control),
    )
    process.start()
    try:
        assert parent_control.recv() == "ready"
        with pytest.raises(OSError, match="already open in another process"):
            connect_private_sqlite(state_path)
    finally:
        parent_control.send("stop")
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    assert process.exitcode == 0


def test_private_state_rejects_external_generation_change(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    connection = connect_private_sqlite(state_path)
    connection.execute("CREATE TABLE owned (value TEXT)")
    connection.commit()

    with sqlite3.connect(state_path) as external:
        external.execute("CREATE TABLE external (value TEXT)")

    connection.execute("INSERT INTO owned VALUES ('pending')")
    with pytest.raises(OSError, match="changed outside this process"):
        connection.commit()
    connection.close()


def test_private_state_rejects_replaced_lock_inode(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    connection = connect_private_sqlite(state_path)
    connection.execute("CREATE TABLE owned (value TEXT)")
    connection.commit()
    lock_path = tmp_path / ".state.db.sanka.lock"
    lock_path.unlink()
    lock_path.write_bytes(b"replacement")

    connection.execute("INSERT INTO owned VALUES ('pending')")
    with pytest.raises(OSError, match="lock changed outside this process"):
        connection.commit()
    connection.close()


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
