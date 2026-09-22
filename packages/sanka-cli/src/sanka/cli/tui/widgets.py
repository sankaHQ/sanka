# SPDX-License-Identifier: AGPL-3.0-only
"""Shared Textual widgets for stage, extension, and job screens."""

from __future__ import annotations

from datetime import UTC, datetime

from textual.containers import Vertical
from textual.widgets import Static

from sanka.cli.tui.model import JobRef, StageRun, format_elapsed


class KeysBar(Static):
    """Always-visible command keys, including how to leave."""


class CliLine(Static):
    """Equivalent agent command for the screen that is open."""

    def show(self, line: str) -> None:
        self.update(line)


class EmptyState(Static):
    pass


_RUNNING = {
    "scan": "Scanning",
    "plan": "Planning",
    "apply": "Applying",
    "test": "Testing",
    "verify": "Verifying",
}


class StageHeader(Static):
    def show_stage(self, run: StageRun, *, root: str = "") -> None:
        phase = _RUNNING.get(run.command, "Running") if run.phase == "running" else run.phase
        bits = [run.command, phase, run.execution]
        elapsed = format_elapsed(run.started_at)
        if elapsed:
            bits.append(elapsed)
        if run.progress is not None:
            bits.append(f"{run.progress:.0%}")
        lines = ["  ".join(bits)]
        if root:
            lines.append(root)
        meta = [item for item in (run.extension_id, run.plan_hash) if item]
        if meta:
            lines.append("  ".join(meta))
        self.update("\n".join(lines))

    def show_job(self, job: JobRef, *, elapsed: str = "") -> None:
        progress = "" if job.progress is None else f"  {job.progress:.0%}"
        self.update(
            f"{job.title}  {job.stage_group}  {job.status}  {elapsed}{progress}\n"
            f"{job.run_id}  {job.extension_id or 'extension pending'}  "
            f"{job.plan_sha or 'no plan hash yet'}"
        )


class ActivityLog(Static):
    def __init__(
        self,
        *,
        wrap: bool = True,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__("", name=name, id=id, classes=classes)
        self._lines: list[str] = []
        if wrap:
            self.styles.text_wrap = "wrap"

    def append_line(self, text: str) -> None:
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        self._lines.append(f"{stamp}  {text}")
        self.update("\n".join(self._lines))
        self.scroll_end(animate=False)


class KeyValue(Vertical):
    def show(self, rows: tuple[tuple[str, str], ...]) -> None:
        self.remove_children()
        for label, value in rows:
            self.mount(Static(f"{label}  {value}"))
