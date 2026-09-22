# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from textual.widgets import DataTable, Input, OptionList, Static

from sanka.cli import main
from sanka.cli.tui.app import (
    CloudMonitorScreen,
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
from sanka_cli import __version__
from sanka_cli.main import cli


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_brand_header_stays_visible_and_renders_paths_literally(theme: str) -> None:
    root = "/work/[bold]project[/bold]/" + "long-directory/" * 8
    app = SankaApp(FakeServices(), Session(project_root=root), start="status")
    app.theme = theme
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        header = app.screen.query_one("#brand")
        assert header.region.y == 0
        assert header.region.height == 5
        assert f"Sanka v{__version__}" in str(app.screen.query_one("#brand-name", Static).content)
        directory = app.screen.query_one("#brand-directory", Static)
        assert str(directory.content) == root
        assert str(directory.tooltip) == root
        assert app.screen.query_one("#menu").region.y >= header.region.bottom
        assert app.screen.query_one("#footer").region.bottom <= 24
        await pilot.press("s")
        await pilot.pause()
        assert app.screen.query_one("#brand").region.height == 5


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.installed: list[str] = []
        self.disabled: list[str] = []
        self.catalog: tuple[ExtensionChoice, ...] = (
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

    def cloud_command(self, args: list[str]) -> dict[str, Any]:
        self.calls.append({"cloud_args": args})
        return {"held": 10, "charged": 3, "released": 7}

    def cloud_identity(self, workspace: str) -> dict[str, Any]:
        return {"email": "test@example.invalid", "selected_workspace": workspace}

    def cloud_login(self, token: str) -> dict[str, Any]:
        return {"email": "test@example.invalid"}

    def fix_eligibility(self, job: JobRef) -> dict[str, Any]:
        return {
            "eligible": True,
            "parent_run_id": job.run_id,
            "workspace_code": job.workspace,
            "min_credits": 1001,
            "max_credits": 6000,
            "verification_scope": "test and verify",
        }

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
        current = str(app.screen.query_one("#current", Static).content)
        assert "/work/demo" in current
        assert "Next: Scan this project when it changes." in current
        assert "No workspace is selected" not in current
        assert "verify" in _table_text(app.screen.query_one("#recent"))
        assert "sanka status --json" in str(app.screen.query_one(CliLine).content)
        assert "q quit" in str(app.screen.query_one(KeysBar).content)
        assert "esc quit" in str(app.screen.query_one(KeysBar).content)
        assert "s scan" in str(app.screen.query_one(KeysBar).content)
        assert "p plan" in str(app.screen.query_one(KeysBar).content)
        assert app.screen.query_one("#menu", OptionList).option_count == 9
        assert "Elapsed" in current
        recent = app.screen.query_one("#recent", DataTable)
        recent.focus()
        await pilot.pause()
        recent.action_select_cursor()
        await pilot.pause()
        assert app.screen.query_one("#run-stage").display is False
        assert "finished" in str(app.screen.query_one("#result", Static).content)


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
        assert "Nothing is written" in str(app.screen.query_one("#result", Static).content)
        assert "extension pending" not in str(app.screen.query_one("#stage-header", Static).content)
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
        assert "m market" in text
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
        header = str(app.screen.query_one("#stage-header", Static).content)
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
        await pilot.pause()
        await pilot.click("#save-configuration")
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert services.calls[0]["target"] == "fastapi"
        assert "selected_endpoints" not in services.calls[0]["configuration"]
        assert services.calls[0]["configuration"]["output"] == "generated"
        footer = str(app.screen.query_one(CliLine).content)
        assert "sanka plan /work/demo" in footer
        assert "--to fastapi" in footer
        assert "--extension-config" in footer

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
        assert "Extension" in str(app.screen.query_one("#confirm-message", Static).content)
        await pilot.click("#yes")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert services.calls[-1]["command"] == "apply"
        assert services.calls[-1]["plan_hash"] == "sha256:reviewed"
        assert "--plan-hash sha256:reviewed" in str(app.screen.query_one(CliLine).content)


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
        await pilot.pause()
        await pilot.click("#save-configuration")
        await pilot.pause()
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
        assert "python-to-go" in str(app.screen.query_one("#missing-body", Static).content)
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

    services.upgrade_marketplace = upgrade  # type: ignore[assignment]
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
        highlighted = menu.highlighted
        assert highlighted is not None
        assert menu.get_option_at_index(highlighted).id == "extensions"
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
        extensions = app.screen.query_one("#extensions", DataTable)
        selected = " ".join(str(cell) for cell in extensions.get_row_at(extensions.cursor_row))
        assert "Python to Go" in selected
        assert "DRF to FastAPI" not in selected
        await pilot.click("#install")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert services.installed == ["sanka/python-to-go"]
        await pilot.press("m")
        await pilot.pause()
        titles = [str(node.content) for node in app.screen.query(Static)]
        assert "Extension marketplace" in titles
        menu = app.screen.query_one("#menu", OptionList)
        highlighted = menu.highlighted
        assert highlighted is not None
        assert menu.get_option_at_index(highlighted).id == "marketplace"


@pytest.mark.asyncio
async def test_marketplace_search_filters_by_target() -> None:
    app = SankaApp(FakeServices(), Session(project_root="/work/demo"), start="marketplace")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("/")
        await pilot.pause()
        assert app.screen.query_one("#search-filters")
        app.screen.query_one("#target-filter", Input).value = "go"
        await pilot.pause()
        results = app.screen.query_one("#search-results", OptionList)
        assert results.option_count == 1
        assert "Python to Go" in str(results.get_option_at_index(0).prompt)
        await pilot.press("escape")
        await pilot.pause()
        assert "DRF to FastAPI" in _table_text(app.screen.query_one("#catalog"))


@pytest.mark.asyncio
async def test_running_stage_footer_hides_escape_back() -> None:
    app = SankaApp(FakeServices(), Session(project_root="/work/demo"), start="plan")
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, StageScreen)
        app.session.busy = True
        app.screen.refresh_footer()
        text = str(app.screen.query_one(KeysBar).content)
        assert "esc back" not in text
        assert "stage running" in text
        assert "q quit" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome,code",
    [
        ("succeeded", "checks_passed", 0),
        ("succeeded", "needs_review", 3),
        ("failed", "service_error", 4),
        ("cancelled", "cancelled", 5),
    ],
)
async def test_settled_cloud_session_preserves_result_and_blocks_local_stages(
    status: str,
    outcome: str,
    code: int,
) -> None:
    services = FakeServices()
    services.job.status = status
    services.job.outcome = outcome
    services.job.raw = {"status": status, "fix_result": {"outcome": outcome}}
    session = Session(project_root="/work/demo", job=services.job, direct=True)
    app = SankaApp(services, session, start="monitor")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert session.exit_code == code
        for command in ("scan", "plan", "apply", "test", "verify"):
            app.action_stage(command)
            await pilot.pause()
            assert isinstance(app.screen, CloudMonitorScreen)
        assert services.calls == []
        await pilot.press("q")
        assert app.return_value == code


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
        header = str(app.screen.query_one("#job-header", Static).content)
        assert "run_01K" in header
        assert "Fix" in header
        fields = str(app.screen.query_one("#job-fields", Static).content)
        assert "succeeded" in fields or "checks_passed" in fields
        assert "worker started" in str(app.screen.query_one("#log", Static).content)


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


def test_failed_attempt_is_recorded_with_safe_configuration(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from sanka.cli.tui.services import HostServices

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ExtensionError("SANKA_FINGERPRINT_STALE", "Source changed")

    monkeypatch.setattr("sanka.cli.tui.services._dispatch_lifecycle", fail)
    service = HostServices(tmp_path)
    outcome = service.run_local(
        "test",
        target="fastapi",
        plan_hash=None,
        configuration={"output": "generated", "access_token": "must-not-be-recorded"},
        on_activity=lambda text: None,
    )
    assert outcome.exit_code == 1
    entry = service.history()[0]
    assert entry.error_code == "SANKA_FINGERPRINT_STALE"
    assert entry.message == "Source changed"
    assert entry.duration == "00:00"
    assert entry.configuration == {"output": "generated"}
    assert "must-not-be-recorded" not in (tmp_path / ".sanka/tui-history.json").read_text()


def test_result_keeps_aggregate_evidence_and_scope() -> None:
    result = _result_text(
        StageOutcome(
            StageRun(
                "verify",
                phase="succeeded",
                result_data={
                    "tests": 7,
                    "http": {"passed": 2, "total": 2},
                    "dropped_alias_routes": 7,
                    "stdout": "Ran 7 tests. OK",
                    "limitations": ["JSON routes only"],
                },
            )
        ),
        "fastapi",
    )
    for value in (
        "tests: 7",
        "http.passed: 2",
        "http.total: 2",
        "dropped_alias_routes: 7",
        "Ran 7 tests. OK",
        "reported scope",
        "JSON routes only",
    ):
        assert value in result


@pytest.mark.asyncio
async def test_plan_configuration_precedes_execution_and_is_editable(tmp_path: Path) -> None:
    from textual.widgets import Button

    from sanka.cli.tui.app import PlanConfiguration

    service = FakeServices()
    app = SankaApp(service, Session(project_root=str(tmp_path)), start="plan")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PlanConfiguration)
        assert not service.calls
        app.screen.query_one("#plan-output", Input).value = "../outside"
        await pilot.click("#save-configuration")
        await pilot.pause()
        assert "inside the project" in str(
            app.screen.query_one("#configuration-error", Static).content
        )
        app.screen.query_one("#plan-output", Input).value = "generated"
        await pilot.pause(0.6)
        await pilot.click("#save-configuration")
        await pilot.pause()
        await pilot.click("#run-stage")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert service.calls[0]["configuration"]["generation"] == "minimal"
        assert app.screen.query_one("#setup").display is False
        assert app.screen.query_one("#result-scroll").region.height >= 4
        assert app.screen.query_one("#configure-stage", Button).region.bottom <= 21
        await pilot.click("#configure-stage")
        await pilot.pause()
        assert isinstance(app.screen, PlanConfiguration)
        assert app.screen.query_one("#plan-output", Input).value == "generated"


@pytest.mark.asyncio
async def test_cloud_actions_fit_small_terminal_and_declined_apply_does_not_submit() -> None:
    from textual.widgets import Button

    from sanka.cli.tui.app import ConfirmScreen
    from sanka.cli.tui.cloud import CloudAction

    service = FakeServices()
    job = JobRef(
        run_id="00000000-0000-0000-0000-000000000001",
        workspace="10483816",
        route="code-plan",
        stage_group="scan+plan",
        command="plan",
        status="succeeded",
        plan_sha="a" * 64,
        raw={"plan": {"summary": {"files": ["main.py"], "risks": ["JSON only"]}}},
    )
    app = SankaApp(service, Session(project_root="/work/demo", job=job), start="monitor")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        for name in ("cloud-apply", "cloud-download", "cloud-receipt"):
            button = app.screen.query_one(f"#{name}", Button)
            assert button.region.right <= 80
            assert button.region.bottom <= 21
        assert "main.py" in str(app.screen.query_one("#job-result", Static).content)
        await pilot.click("#cloud-apply")
        await pilot.pause()
        assert isinstance(app.screen, CloudAction)
        app.screen.query_one("#credit-cap", Input).value = "100"
        await pilot.click("#review-cloud")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert "10483816" in str(app.screen.query_one("#confirm-message", Static).content)
        await pilot.click("#no")
        await pilot.pause()
        assert not service.calls


@pytest.mark.asyncio
async def test_fix_eligibility_and_budget_are_checked_without_submission() -> None:
    from sanka.cli.tui.cloud import CloudAction

    service = FakeServices()
    app = SankaApp(service, Session(project_root="/work/demo"), start="status")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(CloudAction("fix", service.job.workspace, job=service.job))
        await pilot.pause()
        await app.workers.wait_for_complete()
        app.screen.query_one("#credit-cap", Input).value = "100"
        await pilot.click("#review-cloud")
        await pilot.pause()
        assert "1001-6000" in str(app.screen.query_one("#cloud-action-error", Static).content)
        assert not service.calls
        await pilot.press("escape")
        assert not service.calls


@pytest.mark.asyncio
async def test_cloud_setup_pins_connected_workspace() -> None:
    from sanka.cli.tui.cloud import CloudAction, CloudSetupScreen

    service = FakeServices()
    app = SankaApp(service, Session(project_root="/work/demo"), start="status")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CloudSetupScreen)
        app.screen.query_one("#cloud-workspace", Input).value = "10483816"
        await pilot.click("#connect-cloud")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.session.workspace == "10483816"
        # Editing a field cannot silently switch the verified workspace.
        app.screen.query_one("#cloud-workspace", Input).value = "99999999"
        await pilot.click("#cloud-scan")
        await pilot.pause()
        assert isinstance(app.screen, CloudAction)
        assert app.screen.workspace == "10483816"
        assert not service.calls


@pytest.mark.asyncio
async def test_cloud_apply_submission_uses_pinned_identity_and_saved_retry_key() -> None:
    from sanka.cli.tui.cloud import CloudAction

    class FailedSubmission(FakeServices):
        def cloud_command(self, args: list[str]) -> dict[str, Any]:
            self.calls.append({"args": list(args)})
            raise ValueError("Response uncertain")

    service = FailedSubmission()
    job = JobRef(
        run_id="00000000-0000-0000-0000-000000000001",
        workspace="10483816",
        route="code-plan",
        stage_group="scan+plan",
        status="succeeded",
        command="plan",
        plan_sha="a" * 64,
    )
    app = SankaApp(service, Session(project_root="/work/demo"), start="status")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        action = CloudAction("apply", job.workspace, job=job)
        app.push_screen(action)
        await pilot.pause()
        action.query_one("#credit-cap", Input).value = "100"
        await pilot.click("#review-cloud")
        await pilot.pause()
        await pilot.click("#yes")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert service.calls[0]["args"] == [
            "apply",
            "--cloud",
            "--workspace",
            "10483816",
            "--max-credits",
            "100",
            "--idempotency-key",
            action.key,
            "--run",
            job.run_id,
            "--plan-hash",
            job.plan_sha,
            "--yes",
        ]
        assert action.query_one("#credit-cap", Input).disabled
        assert action.query_one("#intent-key", Input).disabled
        # Even a programmatic field change cannot create a different paid retry.
        action.query_one("#credit-cap", Input).value = "200"
        assert action._args() == service.calls[0]["args"][:-1]


def test_copied_local_commands_parse_with_project_and_configuration(tmp_path: Path) -> None:
    import shlex

    from sanka.cli import _build_parser

    session = Session(
        project_root=str(tmp_path / "project with spaces"),
        target="fastapi",
        plan_hash="sha256:reviewed",
        configuration={"output": "generated output"},
    )
    for stage in ("scan", "plan", "apply", "test", "verify"):
        session.command = stage
        args = _build_parser().parse_args(shlex.split(session.footer_line())[1:])
        assert (getattr(args, "root_option", None) or args.root) == session.project_root
        assert args.extension_config == ['{"output": "generated output"}']


def test_cloud_completion_requires_stage_evidence() -> None:
    from sanka.cli.tui.services import _job_from_code

    operation = {"run": {"id": "run-1", "status": "succeeded"}, "operation": "execute"}
    job = _job_from_code("verify", operation, "12345678", title="Cloud")
    assert job.outcome == "needs_review"
    assert job.last_error == "needs_review"
    plan = _job_from_code("plan", {**operation, "operation": "prepare"}, "12345678", title="Cloud")
    assert plan.outcome == "evidence_unavailable"
