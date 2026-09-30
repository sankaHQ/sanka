# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sys
from dataclasses import dataclass

import click


@dataclass
class CLIState:
    profile: str | None
    base_url: str | None
    output: str | None
    tui: bool = False


def validate_tui(
    state: CLIState | None = None,
    *,
    requested: bool = False,
    machine_output: bool = False,
    supported: bool = True,
    terminal: bool | None = None,
) -> bool:
    """Validate explicit UI intent before any command side effects."""
    if not (requested or (state and state.tui)):
        return False
    if not supported:
        raise click.UsageError(
            "This command has no TUI. Omit --tui; use sanka tui for the dashboard."
        )
    if machine_output or (state and state.output == "json"):
        raise click.UsageError("--tui cannot be combined with JSON or compact output.")
    if not (sys.stdin.isatty() and sys.stdout.isatty() if terminal is None else terminal):
        raise click.UsageError("--tui needs a terminal. Omit --tui; run sanka --help.")
    return True


def _tui_intent(ctx: click.Context, _parameter: click.Parameter, value: bool) -> None:
    if value:
        ctx.ensure_object(CLIState).tui = True


tui_option = click.option(
    "--tui",
    is_flag=True,
    expose_value=False,
    callback=_tui_intent,
    help="Open the interactive TUI (requires a terminal; CLI is the default).",
)
