# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from textual.widgets import OptionList

from sanka.cli import main
from sanka.cli.tui.app import (
    SankaApp,
    StageScreen,
    StatusScreen,
    TrustFolderScreen,
    _result_text,
    is_folder_trusted,
)
from sanka.cli.tui.model import (
    ExtensionChoice,
    HistoryEntry,
    JobRef,
    MarketplaceView,
    Project,
    Session,
    StageOutcome,
    StageRun,
    parse_endpoints,
)
from sanka.cli.tui.widgets import CliLine, KeysBar
from sanka.runtime.extensions.model import ExtensionError
from sanka.runtime.extensions.store import ExtensionStore
from sanka_cli.main import cli


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.installed: list[str] = []
        self.disabled: list[str] = []
        self.catalog = (
            _choice("sanka/drf-to-fastapi", "1.2.0", installed=True, targets=("fastapi",)),
            _choice("sanka/drf-to-flask", "1.0.0", installed=True, targets=("flask",)),
            _choice("sanka/python-to-go", "0.1.0", installed=False, targets=("go",)),
        )
        self.endpoint_rows: tuple[Any, ...] = ()
        self.hashes: tuple[str, ...] = ()
        self.target_names = tuple(
            sorted({target for choice in self.catalog for target in choice.targets})
        )
        self.project_view = Project(
            root="/work/demo", frameworks=("django",), languages=("python",)
        )
        self.history_rows = (
            HistoryEntry(
                stage="verify",
                outcome="succeeded",
                target="fastapi",
                plan_hash="sha256:done",
                at="2026-09-22T00:00:00+00:00",
            ),
        )
        self.job = JobRef(
            run_id="run_01K",
            workspace="78165495",
            route="cloud-run",
            stage_group="fix",
            status="running",
            command="fix",
            title="Fix",
            plan_sha="a82f18c",
            extension_id="sanka/python-to-go",
        )

    def project(self) -> Project:
        return self.project_view

    def extensions(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        status: str | None = None,
        target: str | None = None,
    ) -> tuple[ExtensionChoice, ...]:
        needle = query.strip().lower()
        rows = []
        for choice in self.catalog:
            if kind and choice.kind != kind:
                continue
            label = choice.status_label.lower()
            if status and status not in choice.status and label != status.lower():
                continue
            if target and target not in choice.targets:
                continue
            haystack = " ".join((choice.id, choice.kind, choice.label, *choice.targets)).lower()
            if needle and needle not in haystack:
                continue
            rows.append(choice)
        return tuple(rows)

    def extension(self, extension_id: str) -> ExtensionChoice | None:
        return next((item for item in self.catalog if item.id == extension_id), None)

    def marketplaces(self) -> tuple[MarketplaceView, ...]:
        return (
            MarketplaceView(
                name="sanka",
                identity="github.com/sankaHQ/extensions",
                revision="",
                commit="abc",
                trusted=True,
            ),
        )

    def used_extension_id(self, target: str | None = None) -> str | None:
        return "sanka/drf-to-fastapi" if target in {None, "fastapi"} else None

    def targets(self) -> tuple[str, ...]:
        return self.target_names

    def plan_hashes(self) -> tuple[str, ...]:
        return self.hashes

    def saved_endpoints(self) -> tuple[Any, ...]:
        return self.endpoint_rows

    def install(self, extension_id: str, marketplace: str | None = None) -> str:
        self.installed.append(extension_id)
        self.catalog = tuple(
            replace(choice, status=frozenset({"available", "installed", "locked"}))
            if choice.id == extension_id
            else choice
            for choice in self.catalog
        )
        return f"Installed {extension_id}"

    def remove(self, extension_id: str) -> str:
        return f"Removed {extension_id}"

    def disable(self, extension_id: str) -> str:
        self.disabled.append(extension_id)
        return f"Disabled {extension_id}"

    def add_marketplace(
        self,
        source: str,
        *,
        name: str | None,
        trust: bool,
        revision: str | None,
    ) -> str:
        return f"Added {source}"

    def upgrade_marketplace(self, name: str | None) -> str:
        return "Upgraded"

    def remove_marketplace(self, name: str) -> str:
        return f"Removed {name}"

    def run_local(
        self,
        command: str,
        *,
        target: str | None,
        plan_hash: str | None,
        configuration: Mapping[str, Any],
        on_activity: Any,
        endpoints: tuple[str, ...] = (),
    ) -> StageOutcome:
        on_activity(f"Waiting on the {command} extension")
        self.calls.append(
            {
                "command": command,
                "target": target,
                "plan_hash": plan_hash,
                "configuration": dict(configuration),
                "endpoints": endpoints,
            }
        )
        data: dict[str, Any] = {"plan_hash": "sha256:planned"} if command == "plan" else {}
        if command == "verify":
            data["routes"] = [{"route_key": "GET /users", "ok": False, "failed": 1}]
        run = StageRun(
            command=command,
            phase="succeeded" if command != "verify" else "failed",
            plan_hash=str(data.get("plan_hash") or plan_hash or ""),
            message=f"{command} complete",
            result_data=data,
            extension_id="sanka/drf-to-fastapi",
        )
        if command == "verify":
            from sanka.cli.tui.model import parse_routes

            run.routes = parse_routes(data)
        return StageOutcome(run=run, exit_code=0 if command != "verify" else 1)

    def history(self) -> tuple[HistoryEntry, ...]:
        return self.history_rows

    def ledger(self) -> tuple[str, ...]:
        return ("sanka.yaml is present. The spec-file ledger has no state database yet.",)

    def cloud_jobs(self) -> tuple[JobRef, ...]:
        return ()

    def cloud_poll(self, job: JobRef) -> JobRef:
        job.status = "succeeded"
        job.outcome = "checks_passed"
        job.current_task = "checks_passed"
        return job

    def cloud_events(self, job: JobRef, cursor: int) -> tuple[int, tuple[str, ...]]:
        return cursor + 1, ("worker started",)

    def cloud_cancel(self, job: JobRef) -> str:
        return f"Cancel requested for {job.run_id}"

    def record(self, entry: HistoryEntry) -> None:
        return None


def _choice(
    extension_id: str,
    version: str,
    *,
    installed: bool,
    targets: tuple[str, ...],
) -> ExtensionChoice:
    status = frozenset({"installed", "locked"}) if installed else frozenset({"available"})
    return ExtensionChoice(
        id=extension_id,
        version=version,
        marketplace="sanka",
        marketplace_identity="github.com/sankaHQ/extensions",
        kind="migration",
        targets=targets,
        status=status,
        commands=("scan", "plan", "apply", "test", "verify"),
        runtime_specifier=">=0.2",
    )


def test_parse_endpoints_keeps_known_rows_and_drops_unknown_shapes() -> None:
    rows = parse_endpoints(
        {
            "endpoints": [
                {"method": "GET", "path": "/users", "implementation": "not_migrated"},
                {"method": "POST", "path": "/users", "implementation": "already_implemented"},
                {"method": "DELETE", "path": "/users/:id", "implementation": "skip"},
                {"method": "GET"},
                "nope",
            ]
        }
    )
    assert [(row.method, row.path, row.selected) for row in rows] == [
        ("GET", "/users", True),
        ("POST", "/users", False),
        ("DELETE", "/users/:id", False),
    ]
    assert parse_endpoints({"endpoints": "all"}) == ()


def test_history_stores_the_selected_endpoints(tmp_path: Path) -> None:
    from sanka.cli.tui.history import append_history, load_history

    append_history(
        tmp_path,
        HistoryEntry(
            stage="plan",
            outcome="succeeded",
            target="fastapi",
            plan_hash="sha256:planned",
            at="2026-09-22T00:00:00+00:00",
            endpoints=("GET /users",),
        ),
        ".sanka",
    )
    loaded = load_history(tmp_path, ".sanka")
    assert loaded[0].endpoints == ("GET /users",)


def test_scan_result_lists_fingerprint_evidence_and_errors() -> None:
    outcome = StageOutcome(
        run=StageRun(
            command="scan",
            phase="succeeded",
            message="scan success",
            result_data={
                "fingerprint": {
                    "languages": ["python"],
                    "frameworks": ["django"],
                    "evidence": [{"kind": "file", "path": "manage.py", "value": "django"}],
                },
                "recommendations": [{"id": "sanka/drf-to-fastapi", "version": "1.2.0"}],
            },
            error_code="SANKA_FAILED",
            error_message="the extension stopped",
            error_details="inputs: 1",
        )
    )
    text = _result_text(outcome, "fastapi")
    assert "python" in text
    assert "manage.py" in text
    assert "sanka/drf-to-fastapi" in text
    assert "SANKA_FAILED" in text
    assert "inputs: 1" in text


def test_tui_command_requires_a_terminal() -> None:
    result = CliRunner().invoke(cli, ["tui"])
    assert result.exit_code == 2
    assert "sanka --help" in result.output
    assert "\033" not in result.output


def _table_text(table: Any) -> str:
    rows = [table.get_row_at(index) for index in range(table.row_count)]
    return "\n".join(" ".join(str(cell) for cell in row) for row in rows)


def test_non_tty_status_does_not_open_the_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_args: object) -> int:
        raise AssertionError("tui")

    monkeypatch.setattr("sanka.cli.tui.launch.launch_local", boom)
    assert main(["status", "--file", "missing-spec.yaml"]) == 1


def test_disable_unknown_extension(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "user")
    try:
        with pytest.raises(ExtensionError) as raised:
            store.set_extension_enabled("sanka/missing", enabled=False)
    finally:
        store.close()
    assert raised.value.code == "SANKA_EXTENSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_status_shows_project_and_recent_local_run() -> None:
    services = FakeServices()
    app = SankaApp(services, Session(project_root="/work/demo"), start="status")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        current = str(app.screen.query_one("#current").content)
        assert "/work/demo" in current
        assert "verify" in _table_text(app.screen.query_one("#recent"))
        assert "sanka status --json" in str(app.screen.query_one(CliLine).content)
        assert "q quit" in str(app.screen.query_one(KeysBar).content)
        assert "esc quit" in str(app.screen.query_one(KeysBar).content)
        assert "s scan" in str(app.screen.query_one(KeysBar).content)
        assert "p plan" in str(app.screen.query_one(KeysBar).content)
        assert app.screen.query_one("#menu", OptionList).option_count == 8
        assert "Elapsed" in current
        recent = app.screen.query_one("#recent")
        recent.focus()
        await pilot.pause()
        recent.action_select_cursor()
        await pilot.pause()
        assert app.screen.query_one("#run-stage").display is False
        assert "finished" in str(app.screen.query_one("#result").content)


@pytest.mark.asyncio
async def test_escape_quits_from_the_home_screen() -> None:
    app = SankaApp(FakeServices(), Session(project_root="/work/demo"), start="status")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not app.is_running
        assert app.return_value == 0


@pytest.mark.asyncio
async def test_escape_on_scan_returns_home() -> None:
    app = SankaApp(FakeServices(), Session(project_root="/work/demo"), start="status")
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, StageScreen)
        assert app.screen.query_one("#log").display is False
        assert "Nothing is written" in str(app.screen.query_one("#result").content)
        assert "extension pending" not in str(app.screen.query_one("#stage-header").content)
        keys = app.screen.query_one(KeysBar)
        rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "Scan" in rendered
        assert "s  Scan" not in rendered
        assert "esc back    q quit" in rendered
        text = str(keys.content)
        assert "s scan" in text
        assert "p plan" in text
        assert "a apply" in text
        assert "t test" in text
        assert "v verify" in text
        assert "e extensions" in text
        assert "m marketplace" in text
        assert "enter scan" in text
        assert "esc back" in text
        assert "q quit" in text
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, StatusScreen)
        assert app.is_running
        assert "esc quit" in str(app.screen.query_one(KeysBar).content)


@pytest.mark.asyncio
async def test_escape_on_a_direct_scan_quits() -> None:
    app = SankaApp(FakeServices(), Session(project_root="/work/demo"), start="scan")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not app.is_running
        assert app.return_value == 0


@pytest.mark.asyncio
async def test_verify_fills_a_route_table() -> None:
    services = FakeServices()
    app = SankaApp(
        services,
        Session(project_root="/work/demo"),
        start="verify",
        autostart=True,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = _table_text(app.screen.query_one("#rows"))
        assert "GET /users" in table
        assert "failed" in table
        header = str(app.screen.query_one("#stage-header").content)
        assert "verify" in header


@pytest.mark.asyncio
async def test_plan_posts_the_target_and_apply_waits_for_a_hash() -> None:
    services = FakeServices()
    session = Session(
        project_root="/work/demo",
        configuration={"output": "generated", "selected_endpoints": ["GET /users"]},
    )
    app = SankaApp(services, session, start="plan")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        listing = app.screen.query_one("#target-list", OptionList)
        assert listing.option_count == 3
        await pilot.press("/")
        await pilot.pause()
        modal = app.screen.query_one("#search-modal")
        assert modal.region.x > 10
        assert modal.region.y > 2
        await pilot.press("f", "l", "a")
        await pilot.pause()
        results = app.screen.query_one("#search-results", OptionList)
        assert results.option_count == 1
        assert "flask" in str(results.get_option_at_index(0).prompt)
        await pilot.press("escape")
        await pilot.pause()
        app.screen.query_one("#target-list", OptionList).highlighted = 0
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert services.calls[0]["target"] == "fastapi"
        assert "selected_endpoints" not in services.calls[0]["configuration"]
        assert services.calls[0]["configuration"]["output"] == "generated"
        footer = str(app.screen.query_one(CliLine).content)
        assert "sanka plan --to fastapi --json" in footer

        services.hashes = ("sha256:reviewed",)
        session.plan_hash = None
        await pilot.press("a")
        await pilot.pause()
        assert app.screen.query_one("#hash-list", OptionList).option_count == 1
        app.screen.query_one("#hash-list", OptionList).highlighted = 0
        await pilot.pause()
        assert not app.screen.query_one("#run-stage").disabled
        await pilot.click("#run-stage")
        await pilot.pause()
        assert "Extension" in str(app.screen.query_one("#confirm-message").content)
        await pilot.click("#yes")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert services.calls[-1]["command"] == "apply"
        assert services.calls[-1]["plan_hash"] == "sha256:reviewed"
        assert "sanka apply --to fastapi --plan-hash sha256:reviewed --json" in str(
            app.screen.query_one(CliLine).content
        )


@pytest.mark.asyncio
async def test_plan_lists_one_marketplace_target() -> None:
    services = FakeServices()
    services.target_names = ("fastapi",)
    app = SankaApp(services, Session(project_root="/work/demo"), start="plan")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        listing = app.screen.query_one("#target-list", OptionList)
        assert listing.option_count == 1
        assert "fastapi" in str(listing.get_option_at_index(0).prompt)
        assert "Installed" in str(listing.get_option_at_index(0).prompt)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert services.calls[0]["target"] == "fastapi"
        assert services.calls[0]["endpoints"] == ()


@pytest.mark.asyncio
async def test_plan_offers_to_install_an_available_target() -> None:
    services = FakeServices()
    services.target_names = ("go",)
    app = SankaApp(services, Session(project_root="/work/demo"), start="plan")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        prompt = str(app.screen.query_one("#target-list", OptionList).get_option_at_index(0).prompt)
        assert "go" in prompt
        assert "Available" in prompt
        await pilot.press("enter")
        await pilot.pause()
        assert "python-to-go" in str(app.screen.query_one("#missing-body").content)
        await pilot.click("#install")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert services.installed == ["sanka/python-to-go"]
        assert services.calls[0]["target"] == "go"


@pytest.mark.asyncio
async def test_plan_refresh_adds_a_marketplace_target() -> None:
    services = FakeServices()
    services.target_names = ("fastapi",)

    def upgrade(_name: str | None) -> str:
        services.target_names = ("fastapi", "flask")
        return "Upgraded 1 marketplace snapshot(s)"

    services.upgrade_marketplace = upgrade  # type: ignore[method-assign]
    app = SankaApp(services, Session(project_root="/work/demo"), start="plan")
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        prompts = [
            str(app.screen.query_one("#target-list", OptionList).get_option_at_index(index).prompt)
            for index in range(app.screen.query_one("#target-list", OptionList).option_count)
        ]
        assert any(prompt.startswith("flask") for prompt in prompts)
        assert any("DRF to Flask" in prompt for prompt in prompts)


@pytest.mark.asyncio
async def test_extension_marketplace_lists_installs_and_filters() -> None:
    services = FakeServices()
    app = SankaApp(services, Session(project_root="/work/demo"), start="extensions")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        menu = app.screen.query_one("#menu", OptionList)
        assert str(menu.get_option_at_index(0).prompt) == "Scan"
        assert menu.get_option_at_index(menu.highlighted).id == "extensions"
        table = _table_text(app.screen.query_one("#extensions"))
        assert "DRF to FastAPI" in table
        assert "Python to Go" in table
        await pilot.click("#search")
        await pilot.pause()
        modal = app.screen.query_one("#search-modal")
        assert modal.region.x > 10
        assert modal.region.y > 2
        await pilot.press("g", "o", "enter")
        await pilot.pause()
        table = app.screen.query_one("#extensions")
        selected = " ".join(str(cell) for cell in table.get_row_at(table.cursor_row))
        assert "Python to Go" in selected
        assert "DRF to FastAPI" not in selected
        await pilot.click("#install")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert services.installed == ["sanka/python-to-go"]
        await pilot.press("m")
        await pilot.pause()
        titles = [str(node.content) for node in app.screen.query(".section-title")]
        assert "Extension marketplace" in titles
        menu = app.screen.query_one("#menu", OptionList)
        assert menu.get_option_at_index(menu.highlighted).id == "marketplace"


@pytest.mark.asyncio
async def test_cloud_monitor_shows_the_running_job() -> None:
    services = FakeServices()
    session = Session(
        project_root="/work/demo",
        job=services.job,
        direct=True,
        workspace="78165495",
    )
    app = SankaApp(services, session, start="monitor")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        header = str(app.screen.query_one("#job-header").content)
        assert "run_01K" in header
        assert "Fix" in header
        fields = str(app.screen.query_one("#job-fields").content)
        assert "succeeded" in fields or "checks_passed" in fields
        assert "worker started" in str(app.screen.query_one("#log").content)


@pytest.mark.asyncio
async def test_untrusted_folder_asks_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sanka.cli.tui.app.trusted_folders_path", lambda: tmp_path / "trusted.json")
    app = SankaApp(
        FakeServices(), Session(project_root="/work/demo"), start="status", ask_trust=True
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, TrustFolderScreen)
        rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "/work/demo" in rendered
        assert "Yes, I trust this folder" in rendered
        assert "No, exit" in rendered
        await pilot.press("escape")
        await pilot.pause()
        assert not app.is_running
        assert app.return_value == 0
    assert not (tmp_path / "trusted.json").exists()


@pytest.mark.asyncio
async def test_trusting_a_folder_is_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sanka.cli.tui.app.trusted_folders_path", lambda: tmp_path / "trusted.json")
    app = SankaApp(
        FakeServices(), Session(project_root="/work/demo"), start="status", ask_trust=True
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#trust-choice", OptionList).highlighted = 1
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, StatusScreen)
        assert "s scan" in str(app.screen.query_one(KeysBar).content)
    assert is_folder_trusted("/work/demo")

    again = SankaApp(
        FakeServices(), Session(project_root="/work/demo"), start="status", ask_trust=True
    )
    async with again.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(again.screen, StatusScreen)
