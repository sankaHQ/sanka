# SPDX-License-Identifier: AGPL-3.0-only
"""Workspace-pinned cloud setup and reviewed CLI handoffs."""

from __future__ import annotations

import json
import shlex
from typing import Any, ClassVar
from uuid import uuid4

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from sanka.cli.tui.app import ConfirmScreen, SankaApp, SankaScreen, _frame
from sanka.cli.tui.model import JobRef
from sanka.cli.tui.services import _job_from_code, job_from_fix


class CloudAction(ModalScreen[JobRef | None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Back")]

    def __init__(
        self,
        stage: str,
        workspace: str,
        *,
        job: JobRef | None = None,
        source: str = "",
        target: str = "fastapi",
        profile: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__()
        self.stage, self.workspace, self.job = stage, workspace, job
        self.source, self.target, self.profile = source, target, profile
        self.base_url = base_url
        self.key = str(uuid4())
        self.eligibility: dict[str, Any] = {}
        self.submitting = False
        self.approved_args: list[str] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="configuration-modal"):
            yield Label(f"Cloud {self.stage} — workspace {self.workspace}")
            with VerticalScroll():
                yield Static(self.job.run_id if self.job else self.source, markup=False)
                if self.job and self.stage == "apply":
                    yield Static(f"Reviewed plan: {self.job.plan_sha}", markup=False)
                yield Static(
                    "Checking eligibility…"
                    if self.stage == "fix"
                    else "Source runs in Sanka Cloud. Unused reserved credits are released.",
                    id="cloud-scope",
                    markup=False,
                )
                yield Label("Maximum total credits to reserve")
                yield Input("", placeholder="Enter a credit cap", id="credit-cap", type="integer")
                yield Label(
                    "Retry key — keep this key and identical inputs after an uncertain response"
                )
                yield Input(self.key, id="intent-key")
                yield Static("", id="cloud-command", markup=False)
            yield Static("", id="cloud-action-error", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Review", id="review-cloud", disabled=self.stage == "fix")
                yield Button("Copy command", id="copy-cloud-command")
                yield Button("Back", id="close-cloud")

    def on_mount(self) -> None:
        if self.stage == "fix":
            self.run_worker(self._eligibility, thread=True)

    def _eligibility(self) -> None:
        try:
            assert self.job is not None
            app = self.app
            assert isinstance(app, SankaApp)
            result = app.services.fix_eligibility(self.job)
            self.app.call_from_thread(self._show_eligibility, result)
        except Exception as error:
            self.app.call_from_thread(self._error, str(error))

    def _show_eligibility(self, result: dict[str, Any]) -> None:
        self.eligibility = result
        self.query_one("#cloud-scope", Static).update(json.dumps(result, indent=2))
        self.query_one("#review-cloud", Button).disabled = not result.get("eligible", False)

    def _args(self) -> list[str]:
        if self.approved_args is not None:
            return list(self.approved_args)
        cap = int(self.query_one("#credit-cap", Input).value)
        minimum = int(self.eligibility.get("min_credits", 1))
        maximum = int(self.eligibility.get("max_credits", 6000))
        if not minimum <= cap <= maximum:
            raise ValueError(f"Credit cap must be {minimum}-{maximum}.")
        key = self.query_one("#intent-key", Input).value.strip()
        if not key:
            raise ValueError("A retry key is required.")
        args = [
            self.stage,
            "--cloud",
            "--workspace",
            self.workspace,
            "--max-credits",
            str(cap),
            "--idempotency-key",
            key,
        ]
        if self.stage == "scan":
            args += [self.source, "--to", self.target]
        elif self.job is not None:
            args += ["--run", self.job.run_id]
            if self.stage == "apply":
                if not self.job.plan_sha or self.job.status != "succeeded":
                    raise ValueError("Apply requires a successful reviewed cloud plan.")
                args += ["--plan-hash", self.job.plan_sha]
        return args

    def action_close(self) -> None:
        if not self.submitting:
            self.dismiss(None)

    def _error(self, message: str) -> None:
        self.query_one("#cloud-action-error", Static).update(message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-cloud":
            self.action_close()
            return
        if self.submitting:
            return
        try:
            args = self._args()
        except ValueError as error:
            self._error(str(error))
            return
        prefix = ["sanka", *(["--profile", self.profile] if self.profile else [])]
        if self.base_url:
            prefix += ["--base-url", self.base_url]
        command = shlex.join(prefix + args)
        self.query_one("#cloud-command", Static).update(command)
        if event.button.id == "copy-cloud-command":
            self.app.copy_to_clipboard(command)
        elif event.button.id == "review-cloud":
            self.app.push_screen(
                ConfirmScreen(
                    f"{command}\n\nReserve up to {args[args.index('--max-credits') + 1]} credits "
                    f"in workspace {self.workspace}?\n"
                    "This sends source to Sanka Cloud. Success is not guaranteed."
                ),
                lambda yes: self._submit(args) if yes else None,
            )

    def _submit(self, args: list[str]) -> None:
        if self.submitting:
            return
        self.submitting = True
        self.approved_args = list(args)
        self.query_one("#credit-cap", Input).disabled = True
        self.query_one("#intent-key", Input).disabled = True
        self.query_one("#review-cloud", Button).disabled = True
        self.run_worker(lambda: self._work(args), thread=True)

    def _work(self, args: list[str]) -> None:
        try:
            app = self.app
            assert isinstance(app, SankaApp)
            payload = app.services.cloud_command([*args, "--yes"])
            job = (
                job_from_fix(payload, self.workspace)
                if self.stage == "fix"
                else _job_from_code(self.stage, payload, self.workspace, title="Cloud")
            )
            self.app.call_from_thread(self.dismiss, job)
        except Exception as error:
            self.app.call_from_thread(self._submission_failed, str(error))

    def _submission_failed(self, message: str) -> None:
        self.submitting = False
        self.query_one("#review-cloud", Button).disabled = False
        self._error(message + "\nRetry with this same key and unchanged inputs.")


class CloudSetupScreen(SankaScreen):
    def compose(self) -> ComposeResult:
        yield from _frame(self.sanka.session.project_root)
        with Vertical(id="main"):
            with Horizontal(id="actions"):
                yield Button("Sign in", id="sign-in")
                yield Button("Connect", id="connect-cloud")
                yield Button("Scan / Plan", id="cloud-scan", disabled=True)
            with VerticalScroll(id="cloud-setup-detail"):
                yield Static(
                    "Sign in to Sanka, then create a token in Developers → API. "
                    "Paste it below; it is verified and saved in the CLI credential store.",
                    markup=False,
                )
                yield Input(
                    password=True,
                    placeholder="API token (leave empty to use saved login)",
                    id="api-token",
                )
                yield Label("Workspace code — all cloud operations stay pinned to this workspace")
                yield Input(self.sanka.session.workspace or "", id="cloud-workspace")
                yield Label("Source directory or ZIP (uploaded only after confirmation)")
                yield Input(self.sanka.session.project_root, id="cloud-source")
                yield Label("Target")
                yield Select(
                    [("FastAPI", "fastapi"), ("Flask", "flask")],
                    value="fastapi",
                    allow_blank=False,
                    id="cloud-target",
                )
                yield Static(
                    "Not connected. No cloud operation has been submitted.",
                    id="cloud-account",
                    markup=False,
                )

    def on_mount(self) -> None:
        self.refresh_footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("recent-cloud-"):
            self._monitor(list(self._jobs.values())[int(event.button.id.rsplit("-", 1)[1])])
        elif event.button.id == "sign-in":
            self.app.open_url("https://code.sanka.com")
        elif event.button.id == "connect-cloud":
            workspace = self.query_one("#cloud-workspace", Input).value.strip()
            if not workspace.isdigit():
                self.query_one("#cloud-account", Static).update("Enter the numeric workspace code.")
                return
            token = self.query_one("#api-token", Input).value
            self.query_one("#api-token", Input).value = ""
            self.query_one("#cloud-scan", Button).disabled = True
            self.query_one("#connect-cloud", Button).disabled = True
            self.run_worker(lambda: self._connect(workspace, token), thread=True)
        elif event.button.id == "cloud-scan":
            pinned_workspace = self.sanka.session.workspace
            if pinned_workspace is None:
                return
            self.app.push_screen(
                CloudAction(
                    "scan",
                    pinned_workspace,
                    source=self.query_one("#cloud-source", Input).value,
                    target=str(self.query_one("#cloud-target", Select).value),
                    profile=self.sanka.session.profile,
                    base_url=self.sanka.session.base_url,
                ),
                self._monitor,
            )

    def _connect(self, workspace: str, token: str) -> None:
        try:
            if token:
                self.sanka.services.cloud_login(token)
            identity = self.sanka.services.cloud_identity(workspace)
            jobs = self.sanka.services.cloud_jobs()
            self.app.call_from_thread(self._connected, workspace, identity, jobs)
        except Exception as error:
            self.app.call_from_thread(self._failed, str(error))

    def _failed(self, message: str) -> None:
        self.query_one("#connect-cloud", Button).disabled = False
        self.query_one("#cloud-account", Static).update(message)

    def _connected(
        self, workspace: str, identity: dict[str, Any], jobs: tuple[JobRef, ...]
    ) -> None:
        self.sanka.session.workspace = workspace
        self.sanka.session.profile = str(identity.get("profile") or "default")
        self.query_one("#connect-cloud", Button).disabled = False
        self.query_one("#cloud-scan", Button).disabled = False
        fields = {
            key: identity[key]
            for key in ("email", "username", "profile", "selected_workspace")
            if key in identity
        }
        self.query_one("#cloud-account", Static).update(
            json.dumps(fields, indent=2)
            + "\nCredits held/charged/released are shown in each run's Receipt.\n"
            "Scan prepares a plan. Apply requires reviewing its hash and a separate credit cap."
        )
        self._jobs = {job.run_id: job for job in jobs}
        for button in self.query(".recent-cloud-job"):
            button.remove()
        for index, job in enumerate(jobs):
            self.query_one("#cloud-setup-detail", VerticalScroll).mount(
                Button(
                    f"{job.status} {job.stage_group} {job.run_id}",
                    id=f"recent-cloud-{index}",
                    classes="recent-cloud-job",
                )
            )

    def _monitor(self, job: JobRef | None) -> None:
        if job is None:
            return
        from sanka.cli.tui.app import CloudMonitorScreen

        self.sanka.session.job = job
        self.app.push_screen(CloudMonitorScreen())
