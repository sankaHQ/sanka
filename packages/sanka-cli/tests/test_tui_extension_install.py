# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from textual.widgets import Button, ProgressBar, Static
from tui_helpers import FakeServices, _choice

from sanka.cli.tui.app import SankaApp, StageScreen
from sanka.cli.tui.model import Session, StageOutcome, StageRun


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint, fails",
    [
        ("scan", False),
        ("scan", True),
        ("plan", False),
        ("extensions", False),
        ("marketplace", False),
        ("detail", False),
        ("update", False),
        ("pending", False),
    ],
)
async def test_extension_install_blocks_stages_until_environment_is_ready(
    tmp_path: Path,
    entrypoint: str,
    fails: bool,
) -> None:
    started, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    ready, scanned = tmp_path / "extension-ready", tmp_path / "scan-result"

    class Services(FakeServices):
        def install(self, extension_id: str, marketplace: str | None = None) -> str:
            loop.call_soon_threadsafe(started.set)
            release.wait()
            if fails:
                raise OSError("Could not prepare the extension environment")
            ready.write_text(extension_id)
            return super().install(extension_id, marketplace)

        def run_local(self, command: str, **kwargs: Any) -> StageOutcome:
            if not ready.exists():
                assert command == "scan"
                return StageOutcome(
                    StageRun(command=command, phase="failed"),
                    exit_code=1,
                    missing=self.catalog,
                )
            scanned.write_text(command)
            return super().run_local(command, **kwargs)

    services = Services()
    choice = _choice("sanka/python-to-go", "1.0.0", installed=False, targets=("go",))
    if entrypoint == "update":
        choice = replace(choice, status=frozenset({"available", "update_available"}))
    services.catalog, services.target_names = (choice,), ("go",)
    session = Session(project_root=str(tmp_path), target="go")
    if entrypoint == "pending":
        session.pending_action, session.pending_extension_id = "add", choice.id
    start = "extensions" if entrypoint in {"detail", "update", "pending"} else entrypoint
    app = SankaApp(services, session, start=start, autostart=entrypoint == "scan")
    app.marketplace_current = True
    async with app.run_test(size=(80, 24)) as pilot:
        try:
            if entrypoint != "pending":
                await app.workers.wait_for_complete()
                await pilot.pause()
                if entrypoint == "plan":
                    await pilot.press("enter")
                elif entrypoint in {"detail", "update"}:
                    await pilot.click("#details")
                if entrypoint == "update":
                    await pilot.click("#update")
                    await pilot.click("#yes")
                else:
                    await pilot.click("#install")
            await started.wait()
            await pilot.pause()
            assert session.busy
            progress = app.screen.query_one("#install-progress", ProgressBar)
            assert progress.display and progress.region.height > 0
            assert app.screen.query_one("#install-close", Button).disabled
            installing = app.screen
            await pilot.press("s", "p", "a", "escape", "q", "enter")
            assert app.screen is installing and app.is_running
            for screen in app.screen_stack:
                if isinstance(screen, StageScreen):
                    screen.action_run()
            assert not scanned.exists()
            assert not ready.exists()
        finally:
            release.set()
            await app.workers.wait_for_complete()
        await pilot.pause()
        assert not session.busy
        assert not app.screen.query_one("#install-progress", ProgressBar).display
        assert not app.screen.query_one("#install-close", Button).disabled
        if fails:
            assert not ready.exists() and not scanned.exists()
            assert "Could not prepare" in str(
                app.screen.query_one("#install-status", Static).content
            )
        await pilot.click("#install-close")
        await app.workers.wait_for_complete()
        if fails:
            fails = False
            await pilot.click("#run-stage")
            await app.workers.wait_for_complete()
            await pilot.click("#install")
            await app.workers.wait_for_complete()
            await pilot.click("#install-close")
        assert ready.read_text() == choice.id
        if entrypoint == "scan":
            await pilot.click("#run-stage")
            await app.workers.wait_for_complete()
        if entrypoint in {"scan", "plan"}:
            assert scanned.read_text() == entrypoint
            assert session.stage.phase == "succeeded"
