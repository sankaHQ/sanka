# SPDX-License-Identifier: AGPL-3.0-only
"""Textual screens for status, lifecycle stages, extensions, and cloud jobs."""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    OptionList,
    ProgressBar,
    Select,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from sanka.cli.tui.model import (
    LIFECYCLE_COMMANDS,
    ExtensionChoice,
    HistoryEntry,
    JobRef,
    Session,
    StageOutcome,
    StageRun,
    command_line,
    format_elapsed,
    parse_endpoints,
)
from sanka.cli.tui.services import TuiServices, cloud_exit_code
from sanka.cli.tui.widgets import ActivityLog, CliLine, EmptyState, KeysBar, StageHeader
from sanka_cli import __version__

_SETTLED = frozenset({"succeeded", "failed", "cancelled"})
_MENU = (
    ("scan", "s", "Scan"),
    ("plan", "p", "Plan"),
    ("apply", "a", "Apply"),
    ("test", "t", "Test"),
    ("verify", "v", "Verify"),
    ("extensions", "e", "Extensions"),
    ("marketplace", "m", "Marketplace"),
    ("cloud", "c", "Cloud / Account"),
    ("quit", "q", "Quit"),
)


def _keys_line(
    *,
    home: bool,
    run: str | None = None,
    search: bool = False,
    busy: str | None = None,
) -> str:
    if busy == "cloud":
        return (
            "s scan    p plan    a apply    t test    v verify\n"
            "e extensions    m marketplace    q quit    hosted run keeps going"
        )
    if busy == "local":
        return (
            "s scan    p plan    a apply    t test    v verify\n"
            "e extensions    m marketplace    stage running    q quit"
        )
    leave = "esc quit" if home else "esc back"
    action = f"enter {run}    " if run else ""
    find = "/ search    " if search else ""
    return (
        f"{action}s scan    p plan    a apply    t test    v verify\n"
        f"e extensions    m market  c cloud  {find}{leave}    q quit"
    )


def _next_step(history: tuple[HistoryEntry, ...]) -> str:
    succeeded = {entry.stage for entry in history if entry.outcome == "succeeded"}
    if "plan" in succeeded and "apply" not in succeeded:
        return "Next: Apply the reviewed plan."
    if "scan" in succeeded and "plan" not in succeeded:
        return "Next: Plan a migration."
    if not succeeded:
        return "Next: Scan this project."
    return "Next: Scan this project when it changes."


class AppFooter(Vertical):
    """Shortcut lines plus the equivalent agent command. Stays on every screen."""

    def compose(self) -> ComposeResult:
        yield KeysBar("")
        yield CliLine()


def _frame(root: str) -> ComposeResult:
    path = Path(root)
    home = Path.home()
    display_path = str(Path("~") / path.relative_to(home)) if path.is_relative_to(home) else root
    with Horizontal(id="brand"):
        yield Static("▀▀▀▀▀▀▀▀\n ▀▀▀▀▀▀ \n▀▀▀▀▀▀▀▀", id="brand-mark", markup=False)
        with Vertical(id="brand-details"):
            yield Static(f"Sanka v{__version__}", id="brand-name", markup=False)
            directory = Static(display_path, id="brand-directory", markup=False)
            directory.tooltip = Text(root)
            yield directory
    yield OptionList(*(Option(label, id=name) for name, _key, label in _MENU), id="menu")
    yield AppFooter(id="footer")


class SankaScreen(Screen[None]):
    @property
    def sanka(self) -> SankaApp:
        app = self.app
        assert isinstance(app, SankaApp)
        return app

    def refresh_footer(self) -> None:
        if not self.query(CliLine):
            return
        self.query_one(CliLine).show(self.sanka.session.footer_line())
        run = None
        if isinstance(self, StageScreen) and not self.review:
            run = self.command
        busy = None
        if self.sanka.session.busy:
            busy = "cloud" if isinstance(self, CloudMonitorScreen) else "local"
        self.query_one(KeysBar).update(
            _keys_line(
                home=isinstance(self, StatusScreen),
                run=run,
                search=self.can_search(),
                busy=busy,
            )
        )

    def can_search(self) -> bool:
        return False

    def open_search(self) -> None:
        return

    def on_screen_resume(self) -> None:
        self.refresh_footer()

    def _highlight_menu(self, name: str) -> None:
        menu = self.query_one("#menu", OptionList)
        for index, (item, _key, _label) in enumerate(_MENU):
            if item == name:
                menu.highlighted = index
                return

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        choice = str(event.option.id)
        if choice == "quit":
            self.sanka.exit(self.sanka.session.exit_code)
            return
        if choice == "cloud":
            self.sanka.action_cloud()
            return
        if choice == "extensions":
            self.sanka.action_extensions()
            return
        if choice == "marketplace":
            self.sanka.action_marketplace()
            return
        if choice in LIFECYCLE_COMMANDS:
            self.sanka.action_stage(choice)

    def start_catalog_refresh(self) -> None:
        if self.sanka.marketplace_current:
            return
        if self.query("#catalog-status"):
            self.query_one("#catalog-status", Static).update("Updating marketplace…")
        self.run_worker(self._upgrade_catalog, thread=True, group="marketplace")

    def _upgrade_catalog(self) -> None:
        try:
            message = self.sanka.services.upgrade_marketplace(None)
        except Exception as error:
            self.app.call_from_thread(self.notify, f"Marketplace refresh failed: {error}")
            return
        self.app.call_from_thread(self._after_catalog_refresh, message)

    def _after_catalog_refresh(self, message: str) -> None:
        self.sanka.marketplace_current = True
        screen = self.app.screen
        if not isinstance(screen, SankaScreen):
            return
        screen.notify(message)
        screen.on_catalog_refreshed()

    def on_catalog_refreshed(self) -> None:
        return


_TRUST_SCHEMA = "sanka-trusted-folders/v1"


def trusted_folders_path() -> Path:
    return Path.home() / ".sanka" / "trusted-folders.json"


def _folder_key(root: str) -> str:
    path = Path(root).expanduser()
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _trusted_roots() -> set[str]:
    path = trusted_folders_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict) or payload.get("schema_version") != _TRUST_SCHEMA:
        return set()
    roots = payload.get("roots")
    if not isinstance(roots, list):
        return set()
    return {item for item in roots if isinstance(item, str)}


def is_folder_trusted(root: str) -> bool:
    return _folder_key(root) in _trusted_roots()


def trust_folder(root: str) -> None:
    key = _folder_key(root)
    roots = sorted(_trusted_roots() | {key})
    path = trusted_folders_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": _TRUST_SCHEMA, "roots": roots}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class TrustFolderScreen(SankaScreen):
    """Ask once before Sanka reads or writes this project folder."""

    def __init__(self, root: str) -> None:
        super().__init__()
        self.root = root

    def compose(self) -> ComposeResult:
        yield Static("Accessing workspace:", classes="section-title")
        yield Static(self.root, id="trust-path")
        yield Static(
            "Quick safety check: Is this a project you created or one you trust? "
            "(Like your own code, a well-known open source project, or work from your team). "
            "If not, take a moment to review what's in this folder first.",
            classes="trust-body",
        )
        yield Static(
            "Sanka can read this folder. Plan and apply can write generated files here.",
            classes="trust-body",
        )
        yield OptionList(
            Option("No, exit", id="no"),
            Option("Yes, I trust this folder", id="yes"),
            id="trust-choice",
        )
        yield Static("Enter to confirm    Esc to cancel", id="trust-keys")

    def on_mount(self) -> None:
        choice = self.query_one("#trust-choice", OptionList)
        choice.highlighted = 0
        choice.focus()

    def on_screen_resume(self) -> None:
        return

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "trust-choice":
            return
        if str(event.option.id) == "yes":
            trust_folder(self.root)
            self.sanka.folder_trusted = True
            self.sanka.switch_screen(
                self.sanka._screen(self.sanka.start, autostart=self.sanka.autostart)
            )
            return
        self.sanka.exit(0)


class SearchModal(ModalScreen[str | None]):
    """Centered search. Enter returns the highlighted id. Esc cancels."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "choose", "Select", priority=True),
    ]

    def __init__(
        self,
        title: str,
        options: tuple[tuple[str, str], ...] = (),
        *,
        choices: tuple[ExtensionChoice, ...] = (),
    ) -> None:
        super().__init__()
        self.search_title = title
        self._options = options
        self._choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="search-modal"):
            yield Static(self.search_title, classes="section-title")
            yield Input(placeholder="Type to search", id="search-query")
            if self._choices:
                with Horizontal(id="search-filters"):
                    yield Input(placeholder="Type", id="kind")
                    yield Input(placeholder="Status", id="status-filter")
                    yield Input(placeholder="Target", id="target-filter")
            yield OptionList(id="search-results")
            yield Static("Enter to select    Esc to cancel", id="search-hint")

    def on_mount(self) -> None:
        self._show("")
        self.query_one("#search-query", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"search-query", "kind", "status-filter", "target-filter"}:
            self._show(self.query_one("#search-query", Input).value)

    def _show(self, query: str) -> None:
        listing = self.query_one("#search-results", OptionList)
        listing.clear_options()
        if self._choices:
            self._show_choices(query, listing)
        else:
            needle = query.strip().casefold()
            for option_id, prompt in self._options:
                if needle and needle not in f"{option_id} {prompt}".casefold():
                    continue
                listing.add_option(Option(prompt, id=option_id))
        if listing.option_count:
            listing.highlighted = 0

    def _show_choices(self, query: str, listing: OptionList) -> None:
        kind = self.query_one("#kind", Input).value.strip()
        status = self.query_one("#status-filter", Input).value.strip()
        target = self.query_one("#target-filter", Input).value.strip()
        needle = query.strip().casefold()
        for choice in self._choices:
            if kind and choice.kind != kind:
                continue
            label = choice.status_label.lower()
            if status and status not in choice.status and label != status.lower():
                continue
            if target and target not in choice.targets:
                continue
            prompt = f"{choice.label}  {choice.kind}  {', '.join(choice.targets) or '-'}"
            if needle and needle not in f"{choice.id} {prompt}".casefold():
                continue
            listing.add_option(Option(prompt, id=choice.id))

    def action_choose(self) -> None:
        listing = self.query_one("#search-results", OptionList)
        if listing.highlighted is None or listing.option_count == 0:
            return
        option = listing.get_option_at_index(listing.highlighted)
        self.dismiss(str(option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def action_cancel(self) -> None:
        self.dismiss(False)

    def compose(self) -> ComposeResult:
        with Vertical(id="configuration-modal"):
            with VerticalScroll():
                yield Static(self.message, id="confirm-message", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Confirm", id="yes")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class MissingExtensionScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, choices: tuple[ExtensionChoice, ...]) -> None:
        super().__init__()
        self.choices = choices

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        choice = self.choices[0] if self.choices else None
        label = choice.label if choice else "A migration"
        yield Static(f"{label} extension is required.", id="missing-title")
        if self.choices:
            yield Static(_missing_text(self.choices), id="missing-body")
        yield Button("Install extension", id="install")
        yield Button("Open extension marketplace", id="marketplace")
        yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "install" and self.choices:
            self.dismiss(self.choices[0].id)
            return
        if event.button.id == "marketplace":
            self.dismiss("marketplace")
            return
        self.dismiss(None)


class PlanConfiguration(ModalScreen[dict[str, Any] | None]):
    """Editable configuration before execution, with DRF recipe choices."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, target: str, values: dict[str, Any], *, drf: bool) -> None:
        super().__init__()
        self.target, self.values, self.drf = target, values, drf

    def compose(self) -> ComposeResult:
        with Vertical(id="configuration-modal"):
            yield Label(f"Plan configuration — {self.target}")
            with VerticalScroll():
                if self.drf:
                    yield Label("Output directory (inside this project)")
                    yield Input(
                        str(self.values.get("output", f"{self.target}-app")), id="plan-output"
                    )
                    if self.target == "fastapi":
                        for name, label, options, default in (
                            (
                                "generation",
                                "Generation layout",
                                ("minimal", "full", "update"),
                                "minimal",
                            ),
                            (
                                "strategy",
                                "Strategy: native or Django compatibility bridge",
                                ("native", "compatibility"),
                                "native",
                            ),
                            (
                                "package_manager",
                                "Generated project's package manager",
                                ("uv", "pip"),
                                "uv",
                            ),
                        ):
                            yield Label(label)
                            current = str(self.values.get(name, default))
                            choices = options if current in options else (*options, current)
                            yield Select(
                                [(v, v) for v in choices],
                                value=current,
                                allow_blank=False,
                                id=f"plan-{name}",
                            )
                    yield Label("Django settings module (optional; inferred when empty)")
                    yield Input(str(self.values.get("settings_module", "")), id="plan-settings")
                yield Label("Additional extension configuration (JSON object)")
                known = {"output", "settings_module"}
                if self.target == "fastapi":
                    known.update({"generation", "strategy", "package_manager"})
                extra = {k: v for k, v in self.values.items() if not self.drf or k not in known}
                yield TextArea(json.dumps(extra, indent=2), id="plan-extra")
            yield Static("", id="configuration-error", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Save", id="save-configuration")
                yield Button("Cancel", id="cancel-configuration")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-configuration":
            self.dismiss(None)
        elif event.button.id == "save-configuration":
            try:
                values = json.loads(self.query_one("#plan-extra", TextArea).text)
                if not isinstance(values, dict):
                    raise ValueError("Additional configuration must be a JSON object.")
                if self.drf:
                    output = self.query_one("#plan-output", Input).value.strip()
                    app = self.app
                    assert isinstance(app, SankaApp)
                    root = Path(app.session.project_root).resolve()
                    destination = (root / output).resolve()
                    if not output or destination == root or not destination.is_relative_to(root):
                        raise ValueError("Choose an output directory inside the project.")
                    values["output"] = output
                    if self.target == "fastapi":
                        for name in ("generation", "strategy", "package_manager"):
                            values[name] = self.query_one(f"#plan-{name}", Select).value
                        allowed = {
                            "generation": ("minimal", "full", "update"),
                            "strategy": ("native", "compatibility"),
                            "package_manager": ("uv", "pip"),
                        }
                        for name, choices in allowed.items():
                            if values[name] not in choices:
                                raise ValueError(f"{name}: choose {', '.join(choices)}.")
                    if destination.exists() and values.get("generation") != "update":
                        raise ValueError(
                            "Output already exists. Choose a new directory or update mode."
                        )
                    settings = self.query_one("#plan-settings", Input).value.strip()
                    if settings:
                        values["settings_module"] = settings
                self.dismiss(values)
            except (ValueError, OSError) as error:
                self.query_one("#configuration-error", Static).update(str(error))


class StatusScreen(SankaScreen):
    def compose(self) -> ComposeResult:
        yield from _frame(self.sanka.session.project_root)
        with Vertical(id="main"), VerticalScroll(id="status-detail"):
            yield Static("Loading the current project…", id="current")
            yield Static("Recent runs", classes="section-title")
            yield DataTable(id="recent")
            yield Static("", id="ledger")

    def on_mount(self) -> None:
        table = self.query_one("#recent", DataTable)
        table.cursor_type = "row"
        table.add_columns("When", "Result", "Migration", "Hash", "Execution")
        self.refresh_footer()
        self.query_one("#menu", OptionList).focus()
        self.run_worker(self._load, thread=True, exclusive=True)

    def on_screen_resume(self) -> None:
        self.sanka.session.command = "status"
        self.sanka.session.job = None
        self.refresh_footer()
        self.run_worker(self._load, thread=True, exclusive=True)

    def _load(self) -> None:
        services = self.sanka.services
        project = services.project()
        history = services.history()
        jobs = services.cloud_jobs()
        ledger = services.ledger()
        self.app.call_from_thread(self._show, project, history, jobs, ledger)

    def _show(
        self,
        project: Any,
        history: tuple[HistoryEntry, ...],
        jobs: tuple[JobRef, ...],
        ledger: tuple[str, ...],
    ) -> None:
        self._jobs = jobs
        self._history = history
        stage = self.sanka.session.stage
        migration = stage.extension_id or self.sanka.session.target or ""
        lines = [
            project.root,
            ", ".join(project.frameworks) or "unknown",
            _next_step(history),
            "",
            f"Stage     {stage.command}  {stage.phase}",
            f"Elapsed   {stage.elapsed}",
        ]
        if migration:
            lines.append(f"Migration {migration}")
        plan_hash = stage.plan_hash or self.sanka.session.plan_hash
        if plan_hash:
            lines.append(f"Plan      {plan_hash}")
        if stage.progress is not None:
            lines.append(f"Progress  {stage.progress:.0%}")
        if project.note:
            lines.append(project.note)
        if self.sanka.session.workspace:
            lines.append(f"Workspace {self.sanka.session.workspace}")
        self.query_one("#current", Static).update("\n".join(lines))
        table = self.query_one("#recent", DataTable)
        table.clear()
        self._row_kinds: list[tuple[str, int]] = []
        stamped: list[tuple[str, str, int]] = [
            *((entry.at, "local", index) for index, entry in enumerate(history)),
            *((job.started_at, "cloud", index) for index, job in enumerate(jobs)),
        ]
        for _when, kind, index in sorted(stamped, key=lambda item: item[0], reverse=True):
            if kind == "local":
                entry = history[index]
                mark = "ok" if entry.outcome == "succeeded" else "failed"
                table.add_row(
                    entry.at,
                    mark,
                    f"{entry.stage} {entry.target}".strip(),
                    entry.plan_hash,
                    entry.execution,
                )
            else:
                cloud_job = jobs[index]
                table.add_row(
                    cloud_job.started_at,
                    cloud_job.status,
                    cloud_job.stage_group,
                    cloud_job.plan_sha,
                    "cloud",
                )
            self._row_kinds.append((kind, index))
        self.query_one("#ledger", Static).update("\n".join(ledger))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self.sanka.session.busy:
            return
        kind, index = self._row_kinds[event.cursor_row]
        if kind == "cloud":
            self.sanka.session.job = self._jobs[index]
            self.app.push_screen(CloudMonitorScreen())
            return
        entry = self._history[index]
        self.sanka.session.target = entry.target
        self.sanka.session.command = entry.stage
        self.sanka.session.stage = StageRun(
            command=entry.stage,
            phase=entry.outcome,
            plan_hash=entry.plan_hash,
            message=f"{entry.stage} {entry.outcome}",
            execution=entry.execution,
            error_code=entry.error_code,
            error_message=entry.message if entry.error_code else "",
            artifacts=entry.artifacts,
            result_data={"duration": entry.duration, "configuration": entry.configuration},
        )
        self.app.push_screen(StageScreen(entry.stage, review=True))


class StageScreen(SankaScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "run", "Run", priority=True),
        Binding("escape", "leave", "Back", priority=True),
    ]

    def __init__(self, command: str, *, autostart: bool = False, review: bool = False) -> None:
        super().__init__()
        self.command = command
        self.autostart = autostart
        self.review = review
        self._input_names: list[str] = []
        self._targets: tuple[str, ...] = ()
        self._configured_target: str | None = None
        self._run = StageRun(command)

    def action_run(self) -> None:
        if self.review or not self.query("#run-stage"):
            return
        button = self.query_one("#run-stage", Button)
        if not button.display or button.disabled:
            return
        self._remember_target()
        if self.command == "apply":
            self._confirm_apply()
            return
        self._start()

    async def action_leave(self) -> None:
        if isinstance(self.focused, Input):
            if self.query("#run-stage"):
                self.query_one("#run-stage", Button).focus()
            return
        await self.sanka.action_back()

    def compose(self) -> ComposeResult:
        yield from _frame(self.sanka.session.project_root)
        with Vertical(id="main"):
            yield StageHeader(id="stage-header")
            with Horizontal(id="actions"):
                yield Button("Run", id="run-stage")
                yield Button("Configure", id="configure-stage")
                yield Button("Copy command", id="copy-command")
                yield Button("Copy hash", id="copy-hash")
            yield ProgressBar(id="progress", total=100, show_eta=False)
            yield VerticalScroll(id="setup")
            yield DataTable(id="rows")
            with VerticalScroll(id="result-scroll"):
                yield Static("", id="result", markup=False)
            yield ActivityLog(id="log", wrap=True)

    def on_mount(self) -> None:
        session = self.sanka.session
        session.command = self.command
        self._highlight_menu(self.command)
        self.query_one("#progress", ProgressBar).display = False
        self.query_one("#rows", DataTable).display = False
        self.query_one("#result-scroll").display = False
        self.query_one("#log").display = False
        if self.review:
            self._run = session.stage
            self.query_one("#setup", VerticalScroll).display = False
            self.query_one("#actions").display = False
            self.query_one("#run-stage", Button).display = False
            self.query_one(StageHeader).show_stage(session.stage, root=session.project_root)
            self._set_result(_review_text(session))
            self.refresh_footer()
            return
        session.stage = session.stages.get(self.command, StageRun(command=self.command))
        self._run = session.stage
        self.query_one("#configure-stage").display = self.command == "plan"
        self.query_one(StageHeader).show_stage(session.stage, root=session.project_root)
        self.refresh_footer()
        button = self.query_one("#run-stage", Button)
        button.label = self.command.capitalize()
        setup = self.query_one("#setup", VerticalScroll)
        if self.command == "plan":
            self._mount_plan(setup)
        elif self.command == "apply":
            self._mount_apply(setup, button)
        elif self.command == "test":
            self._set_result("Generated tests cover the generated application, not source parity.")
        elif self.command == "verify":
            self._set_result("Checks the generated application against the source.")
        elif self.command == "scan":
            self._set_result("Reads this project and recommends a migration. Nothing is written.")
        if not list(setup.children):
            setup.display = False
        if self.query("#run-stage") and self.query_one("#run-stage", Button).display:
            self.query_one("#run-stage", Button).focus()
        if self.command == "plan" and not self.sanka.marketplace_current:
            self.start_catalog_refresh()
        if self.autostart and self.command not in {"plan", "apply"}:
            self._start()
        if self._run.phase != "idle":
            self._set_result(_result_text(StageOutcome(self._run), session.target))
        self.refresh_footer()

    def on_screen_resume(self) -> None:
        self.sanka.session.command = self.command
        self.sanka.session.stage = self._run
        super().on_screen_resume()

    def _set_result(self, text: str) -> None:
        pane = self.query_one("#result-scroll", VerticalScroll)
        if not text.strip():
            pane.display = False
            return
        pane.display = True
        self.query_one("#result", Static).update(text)
        pane.scroll_home(animate=False)

    def _mount_plan(self, setup: VerticalScroll) -> None:
        self._targets = self.sanka.services.targets()
        setup.mount(Label("--to"))
        if not self._targets:
            setup.mount(EmptyState("No targets in the current marketplace.", id="target-empty"))
            self.query_one("#run-stage", Button).disabled = True
            return
        setup.mount(Button("Search", id="search"))
        setup.mount(OptionList(id="target-list"))
        self._fill_targets("")

    def on_catalog_refreshed(self) -> None:
        self._reload_targets()

    def _reload_targets(self) -> None:
        previous = self._targets
        self._targets = self.sanka.services.targets()
        if self._targets == previous:
            return
        if not self.query("#target-list"):
            if self.query("#target-empty"):
                self.query_one("#target-empty").remove()
            setup = self.query_one("#setup", VerticalScroll)
            setup.display = True
            setup.mount(Button("Search", id="search"))
            setup.mount(OptionList(id="target-list"))
            self.query_one("#run-stage", Button).disabled = False
        self._fill_targets("")

    def _mount_apply(self, setup: VerticalScroll, button: Button) -> None:
        hashes = list(self.sanka.services.plan_hashes())
        chosen = self.sanka.session.plan_hash
        if chosen and chosen not in hashes:
            hashes.insert(0, chosen)
        setup.mount(Label("--plan-hash"))
        if not hashes:
            setup.mount(EmptyState("No reviewed plan hash yet. Run plan first.", id="hash-empty"))
            button.disabled = True
            return
        setup.mount(OptionList(id="hash-list"))
        listing = self.query_one("#hash-list", OptionList)
        for value in hashes:
            listing.add_option(Option(value[:19] + "…", id=value))
        if chosen in hashes:
            listing.highlighted = hashes.index(chosen)
            button.disabled = False
        else:
            listing.highlighted = None
            button.disabled = True

    def can_search(self) -> bool:
        return self.command == "plan" and not self.review and bool(self._targets)

    def open_search(self) -> None:
        if not self._targets:
            return
        options = tuple((name, self._target_prompt(name)) for name in self._targets)
        self.app.push_screen(SearchModal("Search targets", options), self._picked_target)

    def _picked_target(self, target: str | None) -> None:
        if not target or not self.query("#target-list"):
            return
        listing = self.query_one("#target-list", OptionList)
        for index in range(listing.option_count):
            if str(listing.get_option_at_index(index).id) == target:
                listing.highlighted = index
                break
        self.sanka.session.target = target
        self.refresh_footer()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "target-list" and event.option_id:
            self.sanka.session.target = str(event.option_id)
            self.refresh_footer()
        elif event.option_list.id == "hash-list" and event.option_id:
            self.sanka.session.plan_hash = str(event.option_id)
            self.query_one("#run-stage", Button).disabled = False
            self.refresh_footer()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "target-list":
            self.sanka.session.target = str(event.option.id)
            self.refresh_footer()
            return
        if event.option_list.id == "hash-list":
            self.sanka.session.plan_hash = str(event.option.id)
            self.query_one("#run-stage", Button).disabled = False
            self.refresh_footer()
            return
        super().on_option_list_option_selected(event)

    def _fill_targets(self, query: str) -> None:
        listing = self.query_one("#target-list", OptionList)
        needle = query.strip().casefold()
        matches = [
            name
            for name in self._targets
            if needle in name.casefold() or needle in self._target_prompt(name).casefold()
        ]
        listing.clear_options()
        for name in matches:
            listing.add_option(Option(self._target_prompt(name), id=name))
        if not matches:
            self.sanka.session.target = None
            self.refresh_footer()
            return
        chosen = self.sanka.session.target
        listing.highlighted = matches.index(chosen) if chosen in matches else 0
        self._remember_target()

    def _target_prompt(self, target: str) -> str:
        choices = self.sanka.services.extensions(target=target)
        if not choices:
            return target
        details = ", ".join(f"{item.label} ({item.status_label})" for item in choices)
        return f"{target}   {details}"

    def _needs_install(self, target: str) -> tuple[ExtensionChoice, ...]:
        choices = [
            item
            for item in self.sanka.services.extensions(target=target)
            if "incompatible" not in item.status
        ]
        if any(item.status.intersection({"installed", "locked"}) for item in choices):
            return ()
        return tuple(choices)

    def _remember_target(self) -> None:
        if not self.query("#target-list"):
            return
        listing = self.query_one("#target-list", OptionList)
        if listing.highlighted is None or listing.option_count == 0:
            self.sanka.session.target = None
            return
        option = listing.get_option_at_index(listing.highlighted)
        self.sanka.session.target = str(option.id)
        self.refresh_footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "configure-stage":
            self._configure_plan()
            return
        if event.button.id == "copy-command":
            self.app.copy_to_clipboard(self.sanka.session.footer_line())
            return
        if event.button.id == "copy-hash":
            self.app.copy_to_clipboard(self._run.plan_hash or self.sanka.session.plan_hash or "")
            return
        if event.button.id == "search":
            self.open_search()
            return
        if event.button.id != "run-stage" or self.review:
            return
        if self.command == "apply":
            self._confirm_apply()
            return
        self._start()

    def _configure_plan(self) -> None:
        target = self.sanka.session.target
        if not target:
            self.notify("Choose a target first.")
            return
        choices = self.sanka.services.extensions(target=target)
        drf = any(item.id == f"sanka/drf-to-{target}" for item in choices)
        self.app.push_screen(
            PlanConfiguration(target, self.sanka.session.configuration, drf=drf),
            lambda values: self._configured(target, values),
        )

    def _configured(self, target: str, values: dict[str, Any] | None) -> None:
        if values is None:
            return
        self.sanka.session.configuration = values
        self._configured_target = target
        self._set_result("Configuration saved. Review the values with Configure, then run Plan.")
        self.refresh_footer()

    def _confirm_apply(self) -> None:
        session = self.sanka.session
        if not session.plan_hash:
            self.notify("Choose the reviewed plan hash.")
            return
        extension = self.sanka.services.used_extension_id(session.target) or "extension pending"
        if session.endpoints:
            count = str(sum(1 for item in session.endpoints if item.selected))
        else:
            count = "whole project"
        execution = "cloud" if session.stage.execution == "cloud" else "local"
        self.app.push_screen(
            ConfirmScreen(
                "\n".join(
                    (
                        f"Apply {session.plan_hash}",
                        f"Extension {extension}",
                        f"Target {session.target or '-'}",
                        f"Endpoints {count}",
                        f"Execution {execution}",
                    )
                )
            ),
            callback=lambda yes: self._start() if yes else None,
        )

    def _configuration(self) -> dict[str, Any]:
        configuration = {
            key: value
            for key, value in self.sanka.session.configuration.items()
            if key != "selected_endpoints"
        }
        for name in self._input_names:
            if self.query(f"#cfg-{name}"):
                configuration[name] = self.query_one(f"#cfg-{name}", Input).value
        return configuration

    def _start(self) -> None:
        session = self.sanka.session
        if session.busy:
            self.notify("Wait for the current stage to finish.")
            return
        if self.command == "plan":
            if not session.target:
                self.notify("Choose --to.")
                return
            choices = self.sanka.services.extensions(target=session.target)
            if (
                any(item.id == f"sanka/drf-to-{session.target}" for item in choices)
                and self._configured_target != session.target
            ):
                self._configure_plan()
                return
            pending = self._needs_install(session.target)
            if pending:
                self.app.push_screen(
                    MissingExtensionScreen(pending),
                    callback=self._installed_then_plan,
                )
                return
        if self.command == "apply" and not session.plan_hash:
            self.notify("Choose the reviewed plan hash.")
            return
        session.busy = True
        if self.query("#run-stage"):
            self.query_one("#run-stage", Button).focus()
        session.stage = StageRun(
            command=self.command,
            phase="running",
            execution="local",
            started_at=_now(),
        )
        self._run = session.stage
        self._set_result("")
        self.query_one("#rows").display = False
        self.query_one(ActivityLog).clear()
        configuration = self._configuration()
        session.configuration = configuration
        selected = (
            ()
            if self.command == "plan"
            else tuple(item.id for item in session.endpoints if item.selected)
        )
        self.query_one(StageHeader).show_stage(session.stage, root=session.project_root)
        self._show_progress(session.stage.progress)
        self.query_one(ActivityLog).append_line(f"Starting {self.command}")
        self.refresh_footer()
        self.set_interval(1, self._tick)
        self.run_worker(
            lambda: self._work(configuration, session.target, session.plan_hash, selected),
            thread=True,
            exclusive=True,
        )

    def _tick(self) -> None:
        stage = self._run
        if self.sanka.session.busy:
            self.query_one(StageHeader).show_stage(stage, root=self.sanka.session.project_root)
            self._show_progress(stage.progress)

    def _show_progress(self, fraction: float | None) -> None:
        bar = self.query_one("#progress", ProgressBar)
        if fraction is None:
            bar.display = False
            return
        bar.display = True
        bar.update(progress=fraction * 100)

    def _show_rows(self, run: StageRun) -> None:
        table = self.query_one("#rows", DataTable)
        rows: tuple[tuple[str, str, str], ...]
        if run.tests:
            rows = tuple((item.status, item.name, item.duration) for item in run.tests)
            columns = ("Status", "Name", "Duration")
        elif run.routes:
            rows = tuple((item.status, item.name, item.detail) for item in run.routes)
            columns = ("Status", "Route", "Detail")
        else:
            table.display = False
            return
        table.display = True
        table.clear(columns=True)
        table.add_columns(*columns)
        for row in rows:
            table.add_row(*row)

    def _work(
        self,
        configuration: dict[str, Any],
        target: str | None,
        plan_hash: str | None,
        endpoints: tuple[str, ...],
    ) -> None:
        def on_activity(line: str) -> None:
            self.app.call_from_thread(self._log, line)

        outcome = self.sanka.services.run_local(
            self.command,
            target=target,
            plan_hash=plan_hash,
            configuration=configuration,
            on_activity=on_activity,
            endpoints=endpoints,
        )
        self.app.call_from_thread(self._finish, outcome)

    def _log(self, line: str) -> None:
        log = self.query_one(ActivityLog)
        log.display = True
        log.append_line(line)

    def _finish(self, outcome: StageOutcome) -> None:
        session = self.sanka.session
        session.busy = False
        session.stage = outcome.run
        self._run = outcome.run
        session.stages[self.command] = outcome.run
        self.query_one("#setup").display = bool(outcome.inputs)
        self.query_one("#log").display = False
        session.exit_code = outcome.exit_code
        if outcome.run.plan_hash:
            session.plan_hash = outcome.run.plan_hash
        discovered = parse_endpoints(outcome.run.result_data)
        if discovered:
            session.endpoints = list(discovered)
        self.query_one(StageHeader).show_stage(outcome.run, root=session.project_root)
        self._show_progress(outcome.run.progress)
        self._show_rows(outcome.run)
        self.refresh_footer()
        self._set_result(_result_text(outcome, session.target))
        if outcome.inputs:
            setup = self.query_one("#setup", VerticalScroll)
            for name in outcome.inputs:
                if name not in self._input_names:
                    self._input_names.append(name)
                    setup.mount(Label(name))
                    setup.mount(Input(placeholder=name, id=f"cfg-{name}"))
            self.notify("The extension needs a few configuration values.")
        if outcome.missing:
            self.app.push_screen(
                MissingExtensionScreen(outcome.missing),
                callback=self._missing_closed,
            )

    def _installed_then_plan(self, choice: str | None) -> None:
        if choice == "marketplace":
            self.app.push_screen(MarketplaceScreen())
            return
        if not choice:
            return
        self.run_worker(lambda: self._install_then_plan(choice), thread=True)

    def _install_then_plan(self, extension_id: str) -> None:
        try:
            message = self.sanka.services.install(extension_id)
        except Exception as error:
            self.app.call_from_thread(self._log, str(error))
            return
        self.app.call_from_thread(self._log, message)
        self.app.call_from_thread(self._start)

    def _missing_closed(self, choice: str | None) -> None:
        if choice == "marketplace":
            self.app.push_screen(MarketplaceScreen())
            return
        if not choice:
            return
        self.run_worker(lambda: self._install(choice), thread=True)

    def _install(self, extension_id: str) -> None:
        try:
            message = self.sanka.services.install(extension_id)
        except Exception as error:
            message = str(error)
        self.app.call_from_thread(self._log, message)


class ExtensionListScreen(SankaScreen):
    def compose(self) -> ComposeResult:
        yield from _frame(self.sanka.session.project_root)
        with Vertical(id="main"):
            yield Static("Extensions", classes="section-title")
            yield Static("", id="catalog-status")
            with Horizontal(id="extension-actions"):
                yield Button("Search", id="search")
                yield Button("Install", id="install")
                yield Button("Details", id="details")
                yield Button("Marketplace", id="open-marketplace")
            yield DataTable(id="extensions")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("Name", "Type", "Targets", "Status", "Version")
        self._highlight_menu("extensions")
        self.refresh_footer()
        self._fill()
        self.start_catalog_refresh()
        if self.sanka.session.notice:
            self.notify(self.sanka.session.notice)
            self.sanka.session.notice = ""
        if self.sanka.session.pending_action == "add" and self.sanka.session.pending_extension_id:
            extension_id = self.sanka.session.pending_extension_id
            self.sanka.session.pending_action = None
            self.run_worker(lambda: self._install(extension_id), thread=True)
        elif (
            self.sanka.session.pending_action == "remove"
            and self.sanka.session.pending_extension_id
        ):
            extension_id = self.sanka.session.pending_extension_id
            self.sanka.session.pending_action = None
            self.app.push_screen(
                ConfirmScreen(f"Remove {extension_id}?"),
                callback=lambda yes: self._remove(extension_id) if yes else None,
            )

    def can_search(self) -> bool:
        return True

    def open_search(self) -> None:
        options = tuple(
            (
                choice.id,
                f"{choice.label}  {choice.kind}  {', '.join(choice.targets) or '-'}",
            )
            for choice in self.sanka.services.extensions("")
        )
        self.app.push_screen(SearchModal("Search extensions", options), self._picked)

    def _picked(self, extension_id: str | None) -> None:
        if not extension_id or extension_id not in self._ids:
            return
        table = self.query_one(DataTable)
        table.move_cursor(row=self._ids.index(extension_id))
        table.focus()

    def _fill(self) -> None:
        choices = self.sanka.services.extensions("")
        table = self.query_one(DataTable)
        table.clear()
        self._ids = [choice.id for choice in choices]
        for choice in choices:
            table.add_row(
                choice.label,
                choice.kind,
                ", ".join(choice.targets) or "-",
                choice.status_label,
                choice.version_label,
            )

    def _selected(self) -> str | None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._ids):
            return None
        return self._ids[row]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "search":
            self.open_search()
            return
        if event.button.id == "open-marketplace":
            self.app.push_screen(MarketplaceScreen())
            return
        extension_id = self._selected()
        if extension_id is None:
            self.notify("Select an extension.")
            return
        if event.button.id == "details":
            self.app.push_screen(ExtensionDetailScreen(extension_id))
        elif event.button.id == "install":
            self.run_worker(lambda: self._install(extension_id), thread=True)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.cursor_row < len(self._ids):
            self.app.push_screen(ExtensionDetailScreen(self._ids[event.cursor_row]))

    def _install(self, extension_id: str) -> None:
        choice = self.sanka.services.extension(extension_id)
        if choice is not None and "incompatible" in choice.status:
            self.app.call_from_thread(self.notify, "That extension is incompatible with this CLI.")
            return
        try:
            message = self.sanka.services.install(extension_id)
        except Exception as error:
            message = str(error)
        self.app.call_from_thread(self._installed, message)

    def _installed(self, message: str) -> None:
        self.notify(message)
        self._fill()

    def on_catalog_refreshed(self) -> None:
        self._fill()
        self.query_one("#catalog-status", Static).update("")

    def _remove(self, extension_id: str) -> None:
        self.run_worker(lambda: self._remove_work(extension_id), thread=True)

    def _remove_work(self, extension_id: str) -> None:
        try:
            message = self.sanka.services.remove(extension_id)
        except Exception as error:
            message = str(error)
        self.app.call_from_thread(self._installed, message)


class ExtensionDetailScreen(SankaScreen):
    def __init__(self, extension_id: str) -> None:
        super().__init__()
        self.extension_id = extension_id

    def compose(self) -> ComposeResult:
        yield from _frame(self.sanka.session.project_root)
        with Vertical(id="main"):
            yield Static("", id="detail")
            with Horizontal():
                yield Button("Install", id="install")
                yield Button("Update", id="update")
                yield Button("Enable", id="enable")
                yield Button("Disable", id="disable")
                yield Button("Uninstall", id="uninstall")

    def on_mount(self) -> None:
        self.sanka.session.command = "extension"
        self._highlight_menu("extensions")
        self.refresh_footer()
        self._show()

    def _choice(self) -> ExtensionChoice | None:
        return self.sanka.services.extension(self.extension_id)

    def _show(self) -> None:
        choice = self._choice()
        if choice is None:
            self.query_one("#detail", Static).update(f"{self.extension_id} is not in the catalog.")
            return
        used = self.sanka.services.used_extension_id(self.sanka.session.target)
        used_line = (
            "Used by the current migration."
            if used == choice.id
            else "Not the extension locked for this project."
        )
        compatibility = choice.runtime_specifier or "compatible with this sanka-cli"
        if "incompatible" in choice.status:
            compatibility = (
                f"Incompatible. Requires {choice.runtime_specifier or 'another sanka-cli'}"
            )
        self.query_one("#detail", Static).update(
            "\n".join(
                (
                    choice.label,
                    f"Id              {choice.id}",
                    f"Type            {choice.kind}",
                    f"Status          {choice.status_label}",
                    f"Version         {choice.version_label}",
                    f"Marketplace     {choice.marketplace}",
                    f"Targets         {', '.join(choice.targets) or '-'}",
                    f"Commands        {', '.join(choice.commands) or '-'}",
                    f"Digest          {choice.manifest_digest or '-'}",
                    f"Wheels          {', '.join(choice.wheels) or '-'}",
                    f"Compatibility   {compatibility}",
                    used_line,
                )
            )
        )
        installed = bool(choice.status.intersection({"installed", "locked", "update_available"}))
        incompatible = "incompatible" in choice.status
        self.query_one("#install", Button).display = not installed and not incompatible
        self.query_one("#update", Button).display = "update_available" in choice.status
        self.query_one("#enable", Button).display = "disabled" in choice.status
        self.query_one("#disable", Button).display = installed and not incompatible
        self.query_one("#uninstall", Button).display = installed or "disabled" in choice.status

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "uninstall":
            self.app.push_screen(
                ConfirmScreen(
                    f"Remove {self.extension_id}? "
                    "Removing the default extension also records it as disabled."
                ),
                callback=lambda yes: self._run("remove") if yes else None,
            )
            return
        if action == "disable":
            self._run("disable")
            return
        if action in {"install", "update", "enable"}:
            self._run("install")

    def _run(self, action: str) -> None:
        self.run_worker(lambda: self._work(action), thread=True)

    def _work(self, action: str) -> None:
        try:
            if action == "remove":
                message = self.sanka.services.remove(self.extension_id)
            elif action == "disable":
                message = self.sanka.services.disable(self.extension_id)
            else:
                message = self.sanka.services.install(self.extension_id)
        except Exception as error:
            message = str(error)
        self.app.call_from_thread(self._done, message)

    def _done(self, message: str) -> None:
        self.notify(message)
        self._show()


class MarketplaceScreen(SankaScreen):
    def compose(self) -> ComposeResult:
        yield from _frame(self.sanka.session.project_root)
        with Vertical(id="main"):
            yield Static("Extension marketplace", classes="section-title")
            yield Static("", id="catalog-status")
            with Horizontal(id="catalog-actions"):
                yield Button("Search", id="search")
                yield Button("Inspect", id="inspect")
                yield Button("Install", id="install")
            yield DataTable(id="catalog")
            yield Static("Trusted snapshots", classes="section-title")
            yield DataTable(id="snapshots")
            with Horizontal(id="snapshot-actions"):
                yield Button("Upgrade", id="upgrade")
                yield Button("Remove", id="remove-snapshot")
                yield Button("Add snapshot", id="show-add")
            with Vertical(id="snapshot-form"):
                with Horizontal():
                    yield Input(placeholder="Source URL", id="source")
                    yield Input(placeholder="Name", id="name")
                    yield Input(placeholder="Commit", id="revision")
                yield Checkbox("Trust this third-party source", id="trust")
                yield Button("Save snapshot", id="add-snapshot")

    def on_mount(self) -> None:
        self.sanka.session.command = "marketplace"
        self.refresh_footer()
        catalog = self.query_one("#catalog", DataTable)
        catalog.cursor_type = "row"
        catalog.add_columns("Name", "Type", "Targets", "Status", "Version")
        snapshots = self.query_one("#snapshots", DataTable)
        snapshots.cursor_type = "row"
        snapshots.add_columns("Name", "Identity", "Revision", "Trusted")
        self._highlight_menu("marketplace")
        self.query_one("#snapshot-form").display = False
        self._fill()
        self.start_catalog_refresh()
        if self.sanka.session.notice:
            self.notify(self.sanka.session.notice)
            self.sanka.session.notice = ""

    def can_search(self) -> bool:
        return True

    def open_search(self) -> None:
        self.app.push_screen(
            SearchModal("Search marketplace", choices=self.sanka.services.extensions("")),
            self._picked,
        )

    def _picked(self, extension_id: str | None) -> None:
        if not extension_id or extension_id not in self._ids:
            return
        table = self.query_one("#catalog", DataTable)
        table.move_cursor(row=self._ids.index(extension_id))
        table.focus()

    def _fill(self) -> None:
        choices = self.sanka.services.extensions("")
        table = self.query_one("#catalog", DataTable)
        table.clear()
        self._ids = [choice.id for choice in choices]
        for choice in choices:
            table.add_row(
                choice.label,
                choice.kind,
                ", ".join(choice.targets) or "-",
                choice.status_label,
                choice.version_label,
            )
        snapshots = self.query_one("#snapshots", DataTable)
        snapshots.clear()
        self._snapshot_names = []
        for record in self.sanka.services.marketplaces():
            snapshots.add_row(
                record.name,
                record.identity,
                record.revision or record.commit,
                "yes" if record.trusted else "no",
            )
            self._snapshot_names.append(record.name)

    def _selected(self) -> str | None:
        table = self.query_one("#catalog", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._ids):
            return None
        return self._ids[row]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "search":
            self.open_search()
            return
        if event.button.id == "show-add":
            form = self.query_one("#snapshot-form")
            form.display = not form.display
            return
        if event.button.id == "inspect":
            extension_id = self._selected()
            if extension_id:
                self.app.push_screen(ExtensionDetailScreen(extension_id))
            return
        if event.button.id == "install":
            extension_id = self._selected()
            if extension_id:
                self.run_worker(lambda: self._install(extension_id), thread=True)
            return
        if event.button.id == "add-snapshot":
            source = self.query_one("#source", Input).value.strip()
            name = self.query_one("#name", Input).value.strip() or None
            trust = self.query_one("#trust", Checkbox).value
            revision = self.query_one("#revision", Input).value.strip() or None
            self.run_worker(lambda: self._add_snapshot(source, name, trust, revision), thread=True)
            return
        if event.button.id == "upgrade":
            self.run_worker(lambda: self._upgrade(), thread=True)
            return
        if event.button.id == "remove-snapshot":
            snapshots = self.query_one("#snapshots", DataTable)
            row = snapshots.cursor_row
            if row is None or row < 0 or row >= len(self._snapshot_names):
                self.notify("Select a snapshot.")
                return
            name = self._snapshot_names[row]
            self.app.push_screen(
                ConfirmScreen(f"Remove marketplace {name}?"),
                callback=lambda yes: self._drop_snapshot(name) if yes else None,
            )

    def _install(self, extension_id: str) -> None:
        choice = self.sanka.services.extension(extension_id)
        marketplace = choice.marketplace_identity if choice else None
        try:
            message = self.sanka.services.install(extension_id, marketplace)
        except Exception as error:
            message = str(error)
        self.app.call_from_thread(self._done, message)

    def _add_snapshot(
        self,
        source: str,
        name: str | None,
        trust: bool,
        revision: str | None,
    ) -> None:
        if not source:
            self.app.call_from_thread(self.notify, "A marketplace source is required.")
            return
        try:
            message = self.sanka.services.add_marketplace(
                source,
                name=name,
                trust=trust,
                revision=revision,
            )
        except Exception as error:
            message = str(error)
        self.app.call_from_thread(self._done, message)

    def _upgrade(self) -> None:
        try:
            message = self.sanka.services.upgrade_marketplace(None)
        except Exception as error:
            message = str(error)
        self.app.call_from_thread(self._done, message)

    def _drop_snapshot(self, name: str) -> None:
        self.run_worker(lambda: self._drop(name), thread=True)

    def _drop(self, name: str) -> None:
        try:
            message = self.sanka.services.remove_marketplace(name)
        except Exception as error:
            message = str(error)
        self.app.call_from_thread(self._done, message)

    def _done(self, message: str) -> None:
        self.notify(message)
        self._fill()

    def on_catalog_refreshed(self) -> None:
        self.query_one("#catalog-status", Static).update("")
        self._fill()


class CloudMonitorScreen(SankaScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "cloud_button('refresh')", "Refresh"),
        Binding("a", "cloud_button('cloud-apply')", "Apply"),
        Binding("f", "cloud_button('cloud-fix')", "Fix"),
        Binding("b", "cloud_button('cloud-receipt')", "Receipt"),
        Binding("d", "cloud_button('cloud-download')", "Download"),
        Binding("l", "cloud_button('cloud-logs')", "Logs"),
    ]

    def action_cloud_button(self, name: str) -> None:
        button = self.query_one(f"#{name}", Button)
        if button.display and not button.disabled:
            button.press()

    def refresh_footer(self) -> None:
        if not self.query(CliLine):
            return
        self.query_one(CliLine).show(self.sanka.session.footer_line())
        actions = ["r refresh", "b receipt", "d download", "l logs"]
        for key, name in (("a", "cloud-apply"), ("f", "cloud-fix")):
            if self.query_one(f"#{name}").display:
                actions.append(f"{key} {name.removeprefix('cloud-')}")
        leave = "hosted run keeps going" if self.sanka.session.busy else "esc back"
        self.query_one(KeysBar).update("  ".join(actions) + f"\nc cloud/account  {leave}  q quit")

    def __init__(self, *, timeout_seconds: int = 900) -> None:
        super().__init__()
        self.timeout_seconds = timeout_seconds
        self._started: datetime | None = None
        self._job: JobRef | None = None

    def compose(self) -> ComposeResult:
        yield from _frame(self.sanka.session.project_root)
        with Vertical(id="main"):
            yield StageHeader(id="job-header")
            with Horizontal(id="cloud-actions"):
                yield Button("Refresh", id="refresh")
                yield Button("Detach", id="detach")
                yield Button("Cancel", id="cancel")
                yield Button("Apply", id="cloud-apply")
                yield Button("Fix", id="cloud-fix")
            with Horizontal(id="cloud-evidence-actions"):
                yield Button("Receipt", id="cloud-receipt")
                yield Button("Download", id="cloud-download")
                yield Button("Logs", id="cloud-logs")
                yield Button("Copy hash", id="cloud-copy-hash")
                yield Button("Command", id="cloud-copy-command")
            yield ProgressBar(id="progress", total=100, show_eta=False)
            with VerticalScroll(id="cloud-detail"):
                yield Static("", id="job-error", markup=False)
                yield Static("", id="job-result", markup=False)
                yield Static("", id="job-receipt", markup=False)
                yield Static("", id="job-fields", markup=False)
                yield ActivityLog(id="log", wrap=True)

    def on_mount(self) -> None:
        self._job = self.sanka.session.job
        job = self._job
        if job is None:
            self.query_one("#job-fields", Static).update("No cloud job is selected.")
            return
        self.sanka.session.command = job.command
        self._started = _parse_started(job.started_at) or _now()
        self.query_one("#progress", ProgressBar).display = False
        self._polled(job, ())
        self.refresh_footer()
        if job.route != "cloud-run":
            self.query_one("#cancel", Button).label = "Detach only"
        if job.route == "discover" and job.status in _SETTLED:
            self.run_worker(self._refresh_once, thread=True)
        if job.status not in _SETTLED:
            self.sanka.session.busy = True
            self.refresh_footer()
            self._deadline = time.monotonic() + self.timeout_seconds
            self.run_worker(self._poll, thread=True, exclusive=True)
            self.set_interval(1, self._tick_job)

    def on_screen_resume(self) -> None:
        if self._job is not None:
            self.sanka.session.job = self._job
            self.sanka.session.command = self._job.command
        self.refresh_footer()

    def _tick_job(self) -> None:
        job = self._job
        if job is None or job.status in _SETTLED:
            return
        self.query_one(StageHeader).show_job(job, elapsed=format_elapsed(self._started))

    def _show_job(self, job: JobRef, lines: tuple[str, ...]) -> None:
        self._job = job
        self.sanka.session.job = job
        self.sanka.session.command = job.command
        self.query_one(StageHeader).show_job(job, elapsed=format_elapsed(self._started))
        bar = self.query_one("#progress", ProgressBar)
        if job.progress is None:
            bar.display = False
        else:
            bar.display = True
            bar.update(progress=job.progress * 100)
        self.query_one("#cloud-apply", Button).display = (
            job.route == "code-plan"
            and job.command in {"scan", "plan"}
            and job.status == "succeeded"
            and bool(job.plan_sha)
        )
        self.query_one("#cloud-fix", Button).display = (
            job.status in _SETTLED and job.command not in {"scan", "plan"}
        )
        self.query_one("#cancel", Button).display = job.route == "cloud-run"
        self.query_one("#cancel", Button).disabled = (
            job.status in _SETTLED or job.route != "cloud-run"
        )
        self.query_one("#cloud-download", Button).disabled = job.status not in _SETTLED
        endpoints = ", ".join(job.endpoint_ids) if job.endpoint_ids else "not reported"
        self.query_one("#job-fields", Static).update(
            "\n".join(
                (
                    f"Stage           {job.stage_group}",
                    f"Status          {job.status}",
                    f"Run             {job.run_id}",
                    f"Workspace       {job.workspace}",
                    f"Extension       {job.extension_id or '-'}",
                    f"Plan SHA        {job.plan_sha or '-'}",
                    f"Started         {job.started_at or '-'}",
                    f"Task            {job.current_task or '-'}",
                    f"Endpoints       {endpoints}",
                    f"Progress        {'-' if job.progress is None else f'{job.progress:.0%}'}",
                )
            )
        )
        log = self.query_one(ActivityLog)
        for line in lines:
            log.append_line(line)
        if not lines and job.current_task:
            log.append_line(job.current_task)
        attention = {"failed", "needs_review", "evidence_unavailable"}
        error = job.last_error
        if not error and (job.status == "failed" or job.outcome in attention):
            error = job.outcome or job.status
        self.query_one("#job-error", Static).update(error)
        if job.status in _SETTLED:
            logs = (
                "Logs are available as a download now that the run has finished."
                if job.status == "succeeded"
                else "Download retained logs.txt even when verification failed."
            )
            self.query_one("#job-result", Static).update(
                "\n".join(
                    (
                        f"Result          {job.outcome or job.status}",
                        f"Receipt         {job.receipt_command}",
                        f"Download        {job.download_command}",
                        f"UI              {job.ui_url or '-'}",
                        _cloud_evidence(job),
                        logs,
                    )
                )
            )
        else:
            self.query_one("#job-result", Static).update(
                "Queued — waiting for a worker; active compute has not started."
                if job.status == "queued"
                else "Running on a hosted worker. Quit detaches; it does not cancel."
            )

    def _poll(self) -> None:
        job = self._job
        if job is None:
            return
        cursor = 0
        while not get_current_worker().is_cancelled:
            try:
                job = self.sanka.services.cloud_poll(job)
                cursor, lines = self.sanka.services.cloud_events(job, cursor)
            except Exception as error:
                self.app.call_from_thread(self._failed, str(error))
                return
            self.app.call_from_thread(self._polled, job, lines)
            if job.status in _SETTLED:
                return
            if time.monotonic() >= getattr(self, "_deadline", time.monotonic()):
                self.app.call_from_thread(self._timed_out, job)
                return
            time.sleep(2)

    def _polled(self, job: JobRef, lines: tuple[str, ...]) -> None:
        self._show_job(job, lines)
        if job.status in _SETTLED:
            self.sanka.session.busy = False
            self.sanka.session.exit_code = cloud_exit_code(job)
            self.refresh_footer()

    def _failed(self, message: str) -> None:
        self.sanka.session.busy = False
        self.sanka.session.exit_code = 1
        self.query_one("#job-error", Static).update(message)
        self.refresh_footer()

    def _timed_out(self, job: JobRef) -> None:
        self.sanka.session.busy = False
        self.sanka.session.exit_code = 6
        self.query_one("#job-result", Static).update(
            f"Still {job.status} after the wait limit. "
            f"Reattach with sanka cloud status --workspace {job.workspace} {job.run_id}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        job = self._job
        if job is not None and event.button.id in {"cloud-apply", "cloud-fix"}:
            from sanka.cli.tui.cloud import CloudAction

            self.app.push_screen(
                CloudAction(
                    "apply" if event.button.id == "cloud-apply" else "fix",
                    job.workspace,
                    job=job,
                    profile=self.sanka.session.profile,
                    base_url=self.sanka.session.base_url,
                ),
                self._continued,
            )
            return
        if job is not None and event.button.id == "cloud-copy-hash":
            self.app.copy_to_clipboard(job.plan_sha)
            return
        if job is not None and event.button.id == "cloud-copy-command":
            self.app.copy_to_clipboard(self.sanka.session.footer_line())
            return
        if job is not None and event.button.id in {"cloud-receipt", "cloud-download", "cloud-logs"}:
            action = {
                "cloud-receipt": "receipt",
                "cloud-download": "download",
                "cloud-logs": "logs",
            }[str(event.button.id)]
            self.run_worker(lambda: self._evidence_action(job, action), thread=True)
            return
        if event.button.id == "detach":
            job = self._job
            reattach = (
                f"sanka cloud status --workspace {job.workspace} {job.run_id}"
                if job is not None
                else "sanka cloud status"
            )
            self.sanka.session.busy = False
            self.sanka.session.exit_code = 130
            self.query_one("#job-result", Static).update(
                f"Detached. The hosted run keeps going.\nReattach with {reattach}"
            )
            self.notify(f"Detached. Reattach with {reattach}")
            if self.sanka.session.direct:
                print(f"Detached. Reattach with {reattach}", file=sys.stderr)
                self.app.exit(130)
            else:
                self.app.pop_screen()
            return
        if event.button.id == "refresh" and self.sanka.session.job is not None:
            self.run_worker(self._refresh_once, thread=True)
            return
        if event.button.id == "cancel":
            job = self._job
            if job is None:
                return
            self.app.push_screen(
                ConfirmScreen(f"Cancel cloud run {job.run_id}?"),
                callback=self._cancel,
            )

    def _continued(self, job: JobRef | None) -> None:
        if job is not None:
            self.sanka.session.job = job
            self.app.push_screen(CloudMonitorScreen())

    def _evidence_action(self, job: JobRef, action: str) -> None:
        try:
            args = [
                "cloud",
                "receipt" if action == "receipt" else "download",
                job.run_id,
                "--workspace",
                job.workspace,
            ]
            if action in {"download", "logs"}:
                destination = (
                    Path(self.sanka.session.project_root)
                    / f"sanka-{job.run_id}-{'logs.txt' if action == 'logs' else 'output.zip'}"
                )
                args += ["--to", str(destination)]
                if action == "logs":
                    args += ["--artifact", "logs.txt"]
            payload = self.sanka.services.cloud_command(args)
            self.app.call_from_thread(
                self.query_one("#job-receipt", Static).update, json.dumps(payload, indent=2)
            )
        except Exception as error:
            self.app.call_from_thread(self.notify, str(error))

    def _refresh_once(self) -> None:
        job = self._job
        if job is None:
            return
        try:
            job = self.sanka.services.cloud_poll(job)
            _cursor, lines = self.sanka.services.cloud_events(job, 0)
        except Exception as error:
            self.app.call_from_thread(self._failed, str(error))
            return
        self.app.call_from_thread(self._polled, job, lines)

    def _cancel(self, yes: bool | None) -> None:
        if not yes or self._job is None:
            return
        job = self._job
        self.run_worker(lambda: self._cancel_work(job), thread=True)

    def _cancel_work(self, job: JobRef) -> None:
        try:
            message = self.sanka.services.cloud_cancel(job)
        except Exception as error:
            message = str(error)
        self.app.call_from_thread(self.notify, message)


class SankaApp(App[int]):
    TITLE = "Sanka"
    ESCAPE_TO_MINIMIZE = False
    CSS = """
    Screen { layout: vertical; }
    #brand { dock: top; height: 5; padding: 1 2; color: $text; background: $surface; }
    #brand-mark { width: 8; height: 3; margin-right: 2; }
    #brand-details { width: 1fr; height: 3; }
    #brand-name { height: 1; text-style: bold; }
    #brand-directory { height: 1; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    #footer { dock: bottom; height: 3; }
    KeysBar { height: 2; background: $primary; color: $text; text-style: bold; padding: 0 1; }
    #trust-path { text-style: bold; padding: 1 1 0 1; text-wrap: wrap; }
    .trust-body { padding: 1 1 0 1; text-wrap: wrap; }
    #trust-choice { height: auto; max-height: 5; border: none; padding: 1 1; }
    #trust-keys { color: $text-muted; padding: 0 1; }
    SearchModal { align: center middle; }
    #search-modal {
        width: 64;
        height: 18;
        background: $surface;
        border: solid $primary;
        padding: 1 1;
    }
    #search-results { height: 1fr; border: none; }
    #search-filters { height: 3; }
    #search-filters Input { width: 1fr; }
    #search-hint { color: $text-muted; padding: 0 1; }
    CliLine { height: 1; background: $boost; color: $text; padding: 0 1; }
    #menu {
        dock: left; width: 18; height: 1fr; margin-top: 5;
        border: none; border-right: solid $primary;
    }
    #main { height: 1fr; width: 1fr; }
    #status-detail { height: 1fr; }
    #stage-header { height: auto; padding: 0 1; }
    Button {
        height: 1;
        width: auto;
        min-width: 8;
        border: none;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    Button:hover, Button:focus { background: $primary; }
    #cloud-actions, #cloud-evidence-actions { height: 1; }
    #job-header { height: auto; max-height: 3; }
    #cloud-detail { height: 1fr; }
    #actions { height: 1; padding: 0 1; }
    #run-stage {
        width: auto;
        margin: 0 0 1 0;
        background: $primary;
        text-style: bold;
    }
    #run-stage:disabled { background: $panel; color: $text-muted; }
    PlanConfiguration, CloudAction, ConfirmScreen { align: center middle; }
    #configuration-modal {
        width: 70; max-width: 95%; height: 90%;
        background: $surface; border: solid $primary; padding: 1;
    }
    .dialog-actions { height: 1; }
    #plan-extra { height: 6; }
    #setup { height: auto; max-height: 40%; padding: 0 1; }
    #target-list {
        height: auto;
        max-height: 9;
        border: solid $primary;
        padding: 0 1;
        background: $surface;
    }
    #result-scroll { height: 1fr; padding: 0 1; }
    #rows { height: 4; }
    #log {
        height: auto;
        max-height: 4;
        margin: 0 1 1 1;
        border: round $primary;
        padding: 0 1;
        overflow-y: auto;
    }
    #scan-hint { color: $text-muted; }
    #progress { height: 1; margin: 0 1; }
    #extensions, #catalog { height: 1fr; }
    #snapshots { height: 5; }
    #snapshot-form { height: auto; padding: 0 1; }
    #extension-actions, #catalog-actions, #snapshot-actions {
        height: 3;
        padding: 0 1;
    }
    #snapshot-form Input { width: 1fr; }
    #main Input, #trust { margin: 0 1; }
    .section-title { text-style: bold; padding: 0 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("s", "stage('scan')", "Scan"),
        Binding("p", "stage('plan')", "Plan"),
        Binding("a", "stage('apply')", "Apply"),
        Binding("t", "stage('test')", "Test"),
        Binding("v", "stage('verify')", "Verify"),
        Binding("e", "extensions", "Extensions"),
        Binding("m", "marketplace", "Marketplace"),
        Binding("c", "cloud", "Cloud / Account"),
        Binding("slash", "search", "Search"),
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        services: TuiServices,
        session: Session,
        *,
        start: str = "status",
        autostart: bool = False,
        monitor_timeout: int = 900,
        ask_trust: bool = False,
    ) -> None:
        super().__init__()
        self.services = services
        self.session = session
        self.start = start
        self.autostart = autostart
        self.monitor_timeout = monitor_timeout
        self.marketplace_current = False
        self.ask_trust = ask_trust
        self.folder_trusted = not ask_trust or is_folder_trusted(session.project_root)

    def on_mount(self) -> None:
        if self.ask_trust and not self.folder_trusted:
            self.push_screen(TrustFolderScreen(self.session.project_root))
            return
        self.push_screen(self._screen(self.start, autostart=self.autostart))

    def _screen(self, name: str, *, autostart: bool = False) -> Screen[Any]:
        if name in LIFECYCLE_COMMANDS:
            return StageScreen(name, autostart=autostart)
        if name == "extensions":
            return ExtensionListScreen()
        if name == "marketplace":
            return MarketplaceScreen()
        if name == "monitor":
            return CloudMonitorScreen(timeout_seconds=self.monitor_timeout)
        return StatusScreen()

    def _busy(self) -> bool:
        if not self.folder_trusted:
            return True
        if self.session.busy:
            self.notify("Wait for the current stage to finish or detach.")
            return True
        return False

    def action_stage(self, command: str) -> None:
        if self._busy():
            return
        if self.session.job is not None:
            self.notify(
                "This session monitors a cloud run. Quit and use "
                f"sanka {command} --cloud --help to continue in the cloud.",
                timeout=10,
            )
            return
        self.session.command = command
        self.push_screen(StageScreen(command))

    def action_cloud(self) -> None:
        if not self._busy():
            from sanka.cli.tui.cloud import CloudSetupScreen

            self.push_screen(CloudSetupScreen())

    def action_extensions(self) -> None:
        if not self._busy():
            self.session.command = "extension"
            self.push_screen(ExtensionListScreen())

    def action_marketplace(self) -> None:
        if not self._busy():
            self.session.command = "marketplace"
            self.push_screen(MarketplaceScreen())

    def action_search(self) -> None:
        screen = self.screen
        if isinstance(screen, SearchModal):
            return
        if isinstance(screen, SankaScreen) and screen.can_search():
            screen.open_search()

    async def action_back(self) -> None:
        if self.session.busy:
            self.notify("This stage is still running. Press q to quit.")
            return
        if isinstance(self.screen, StatusScreen) or len(self.screen_stack) <= 2:
            await self.action_quit()
            return
        self.pop_screen()

    async def action_quit(self) -> None:
        if self.session.busy:
            self.session.exit_code = 130
        self.exit(self.session.exit_code)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_started(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _missing_text(choices: tuple[ExtensionChoice, ...]) -> str:
    blocks = []
    for choice in choices:
        blocks.append(
            "\n".join(
                (
                    choice.id,
                    f"Version      {choice.version or '-'}",
                    f"Marketplace  {choice.marketplace or '-'}",
                    f"Add          {choice.add_command or '-'}",
                )
            )
        )
    return "\n\n".join(blocks)


def _review_text(session: Session) -> str:
    stage = session.stage
    lines = [
        f"{stage.command} {stage.phase}",
        f"Target     {session.target or '-'}",
        f"Plan hash  {stage.plan_hash or '-'}",
        "This run is finished. Start a new stage to run it again.",
    ]
    lines.append(_result_text(StageOutcome(stage), session.target))
    return "\n".join(lines)


def _result_text(outcome: StageOutcome, target: str | None) -> str:
    run = outcome.run
    lines = [run.message or run.phase]
    if run.command in {"test", "verify"}:
        lines.append(
            "Passed only within the reported scope; this is not proof of all source behavior."
            if run.phase == "succeeded"
            else "The reported checks did not establish a pass."
        )
    lines.extend(_evidence_lines(run.result_data))
    fingerprint = run.result_data.get("fingerprint")
    if isinstance(fingerprint, dict):
        languages = fingerprint.get("languages")
        frameworks = fingerprint.get("frameworks")
        if isinstance(languages, list) and languages:
            lines.append("Languages  " + ", ".join(str(item) for item in languages))
        if isinstance(frameworks, list) and frameworks:
            lines.append("Frameworks  " + ", ".join(str(item) for item in frameworks))
        evidence = fingerprint.get("evidence")
        if isinstance(evidence, list) and evidence:
            lines.append("Evidence")
            for item in evidence:
                if isinstance(item, dict):
                    kind = item.get("kind") or ""
                    path = item.get("path") or ""
                    value = item.get("value") or ""
                    lines.append(f"- {kind} {path} {value}".rstrip())
    recommendations = run.result_data.get("recommendations")
    if isinstance(recommendations, list) and recommendations:
        lines.append("Recommendations")
        for item in recommendations:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                lines.append(f"- {item['id']} {item.get('version') or ''}".rstrip())
    if run.plan_hash:
        lines.append(f"Plan hash  {run.plan_hash}")
        lines.append(command_line("apply", target=target, plan_hash=run.plan_hash))
    if run.limitations:
        lines.append("Limitations")
        lines.extend(f"- {item}" for item in run.limitations)
    if run.artifacts:
        lines.append("Artifacts")
        lines.extend(f"- {item}" for item in run.artifacts)
    if run.next_actions:
        lines.append("Next")
        lines.extend(f"- {item}" for item in run.next_actions)
    if run.error_code or run.error_message:
        lines.append(f"Error  {run.error_code}  {run.error_message}".rstrip())
    if run.error_details:
        lines.append(run.error_details)
    if run.command == "test":
        if run.tests:
            lines.append("Tests")
            lines.extend(f"{item.status:8} {item.name}  {item.duration}" for item in run.tests)
        elif run.phase == "running":
            lines.append("Waiting for the extension result.")
        else:
            lines.append("The extension did not report individual tests.")
    if run.command == "verify":
        if run.routes:
            lines.append("Endpoints")
            lines.extend(f"{item.status:8} {item.name}  {item.detail}" for item in run.routes)
        elif run.phase != "running":
            lines.append("The extension did not report per-endpoint verification.")
    if run.command == "scan" and not parse_endpoints(run.result_data):
        lines.append("The extension has not reported endpoints.")
    return "\n".join(lines)


def _evidence_lines(data: object, prefix: str = "") -> list[str]:
    lines: list[str] = []
    if not isinstance(data, dict):
        return lines
    for key, value in data.items():
        label = f"{prefix}{key}"
        if isinstance(value, dict):
            lines.extend(_evidence_lines(value, label + "."))
        elif isinstance(value, (int, float, bool)) or key in {
            "stdout",
            "stderr",
            "log",
            "test_output",
            "test_log",
            "scope",
            "output",
            "status",
            "reason",
            "verification_scope",
            "limitations",
            "risks",
            "files",
            "dropped_routes",
            "checks",
            "generated_files",
        }:
            rendered = (
                json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value)
            )
            lines.append(f"{label}: {rendered[:12000]}")
    return lines


def _cloud_evidence(job: JobRef) -> str:
    sections = []
    for key in ("plan", "http_verification", "fix_result", "failure", "receipt"):
        value = job.raw.get(key)
        if isinstance(value, dict):
            sections.append(key.replace("_", " ").title())
            sections.extend(_evidence_lines(value))
    return "\n".join(sections)
