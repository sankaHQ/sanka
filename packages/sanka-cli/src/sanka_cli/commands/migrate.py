# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import NoReturn

import click

from sanka_cli.commands.cloud_migrate import run_cloud_command
from sanka_cli.state import CLIState

# Top-level local migration verbs. Kept flat so `sanka scan` and
# `sanka companies list` coexist in one binary.
MIGRATION_COMMANDS: dict[str, str] = {
    "scan": "inspect a source application and write its semantic scan artifact",
    "plan": "inspect source/target and produce a reviewable plan",
    "validate": "validate sampled source records against the plan without writing",
    "apply": "execute the reviewed plan (resumable)",
    "test": "generate and run unit tests for the created FastAPI app",
    "verify": "verify the target against the source and ledger",
    "status": "show run status and ledger counts",
    "migrate": "plan + apply + verify in one go",
    "connect": "select a built-in provider and show its supported migration roles",
    "research": "query cited Sanka lifecycle, cost, and comparison research",
    "assess": "submit a free migration assessment",
    "extension": "manage local migration extensions",
}

HYBRID_CLOUD_COMMANDS = {"plan", "apply", "status", "verify"}
CLOUD_ONLY_COMMANDS: dict[str, str] = {
    "repair": "retry failed cloud records without changing the approved plan",
    "review": "read the cloud destination and issue a signed review",
    "pause": "pause an active cloud migration at a durable checkpoint",
    "resume": "resume a paused or failed cloud migration",
    "cancel": "permanently cancel a cloud migration",
}


class _ForwardingCommand(click.Command):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.meta["sanka.raw_args"] = tuple(args)
        return super().parse_args(ctx, args)


def register_migration_passthroughs(cli: click.Group) -> None:
    for name, help_text in MIGRATION_COMMANDS.items():
        if name in HYBRID_CLOUD_COMMANDS:
            cli.add_command(_build_hybrid(name, help_text))
        else:
            cli.add_command(_build_passthrough(name, help_text))
    for name, help_text in CLOUD_ONLY_COMMANDS.items():
        cli.add_command(_build_cloud_only(name, help_text))


def _build_passthrough(name: str, help_text: str) -> click.Command:
    @click.command(
        name,
        cls=_ForwardingCommand,
        help=help_text,
        # Forward everything verbatim so the local parser renders its own usage.
        context_settings={
            "ignore_unknown_options": True,
            "allow_extra_args": True,
            "help_option_names": [],
        },
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_context
    def passthrough(ctx: click.Context, args: tuple[str, ...]) -> None:
        _run_local(name, ctx.meta["sanka.raw_args"])

    return passthrough


def _build_hybrid(name: str, help_text: str) -> click.Command:
    @click.command(
        name,
        cls=_ForwardingCommand,
        help=f"{help_text} (local; cloud with --program)",
        context_settings={
            "ignore_unknown_options": True,
            "allow_extra_args": True,
            "help_option_names": [],
        },
    )
    @click.option("--program", "program_id", default=None, help="Sanka Cloud Program ID.")
    @click.option("--migration", "migration_id", default=None, help="Cloud migration ID.")
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_context
    def hybrid(
        ctx: click.Context,
        program_id: str | None,
        migration_id: str | None,
        args: tuple[str, ...],
    ) -> None:
        if program_id or migration_id:
            run_cloud_command(
                name,
                ctx.obj,
                program_id=program_id,
                migration_id=migration_id,
                args=args,
            )
            return
        _run_local(name, ctx.meta["sanka.raw_args"])

    return hybrid


def _build_cloud_only(name: str, help_text: str) -> click.Command:
    @click.command(
        name,
        help=help_text,
        context_settings={
            "ignore_unknown_options": True,
            "allow_extra_args": True,
            "help_option_names": [],
        },
    )
    @click.option("--program", "program_id", default=None, help="Sanka Cloud Program ID.")
    @click.option("--migration", "migration_id", default=None, help="Cloud migration ID.")
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_obj
    def cloud_only(
        state: CLIState,
        program_id: str | None,
        migration_id: str | None,
        args: tuple[str, ...],
    ) -> None:
        run_cloud_command(
            name,
            state,
            program_id=program_id,
            migration_id=migration_id,
            args=args,
        )

    return cloud_only


def _run_local(name: str, args: tuple[str, ...]) -> NoReturn:
    from sanka.cli import main as local_main

    raise SystemExit(local_main([name, *args]))
