# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import NoReturn

import click

from sanka_cli.commands.cloud_migrate import run_cloud_command
from sanka_cli.commands.code_cloud import STAGES, invoke
from sanka_cli.state import CLIState

# Top-level local migration verbs. Kept flat so `sanka scan` and
# `sanka companies list` coexist in one binary.
MIGRATION_COMMANDS: dict[str, str] = {
    "scan": "inspect a source application and write its semantic scan artifact",
    "plan": "inspect source/target and produce a reviewable plan",
    "validate": "validate sampled source records against the plan without writing",
    "apply": "execute the reviewed plan (resumable)",
    "test": "generate and run tests for the converted application",
    "verify": "verify the target against the source and ledger",
    "status": "show run status and ledger counts",
    "migrate": "plan + apply + verify in one go",
    "connect": "inspect installed extension support; does not authenticate a system",
    "assess": "submit a free migration assessment",
    "extension": "manage data and code extensions",
}

HYBRID_CLOUD_COMMANDS = {"plan", "apply", "status", "verify"}
CLOUD_ONLY_COMMANDS: dict[str, str] = {
    "repair": "retry failed cloud records without changing the approved plan",
    "review": "read the cloud destination and issue a signed review",
    "pause": "pause an active cloud migration at a durable checkpoint",
    "resume": "resume a paused or failed cloud migration",
    "cancel": "permanently cancel a cloud migration",
}

APP_LOCAL_COMMANDS = ("validate", "migrate", "connect")
APP_HYBRID_COMMANDS = ("plan", "apply", "status", "verify")


@click.group()
def app() -> None:
    """Plan, run, and verify Sanka App data migrations."""


def _legacy_notice(name: str) -> None:
    click.echo(f"Deprecated: use `sanka app {name}` for data migrations.", err=True)


def _has_file_option(args: tuple[str, ...]) -> bool:
    boundary = args.index("--") if "--" in args else len(args)
    return any(
        arg in {"-f", "--file"} or arg.startswith(("-f=", "--file=")) for arg in args[:boundary]
    )


class _ForwardingCommand(click.Command):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        boundary = args.index("--") if "--" in args else len(args)
        args = ["--help" if arg in {"-h", "--h"} else arg for arg in args[:boundary]] + args[
            boundary:
        ]
        if ctx.parent and ctx.parent.info_name == "app" and "--cloud" in args[:boundary]:
            raise click.UsageError("Use --program or --migration for hosted Sanka App runs")
        if (
            self.name in STAGES
            and not (ctx.parent and ctx.parent.info_name == "app")
            and "--cloud" in args[:boundary]
        ):
            if any(
                a == "--program"
                or a.startswith("--program=")
                or a == "--migration"
                or a.startswith("--migration=")
                for a in args[:boundary]
            ):
                raise click.UsageError(
                    "--cloud Code stages cannot be combined with --program or --migration"
                )
            ctx.meta["sanka.code_cloud"] = True
            args = list(args)
            args.remove("--cloud")
        ctx.meta["sanka.raw_args"] = tuple(args)
        return super().parse_args(ctx, args)


def register_migration_passthroughs(cli: click.Group) -> None:
    for name in APP_LOCAL_COMMANDS:
        app.add_command(_build_passthrough(name, MIGRATION_COMMANDS[name], product="app"))
    for name in APP_HYBRID_COMMANDS:
        app.add_command(_build_hybrid(name, MIGRATION_COMMANDS[name], product="app"))
    for name, help_text in CLOUD_ONLY_COMMANDS.items():
        app.add_command(_build_cloud_only(name, help_text))
    cli.add_command(app)
    for name, help_text in MIGRATION_COMMANDS.items():
        if name in HYBRID_CLOUD_COMMANDS:
            cli.add_command(
                _build_hybrid(
                    name,
                    help_text,
                    product="code" if name != "status" else "auto",
                    hidden=name == "status",
                )
            )
        else:
            cli.add_command(_build_passthrough(name, help_text, hidden=name in APP_LOCAL_COMMANDS))
    for name, help_text in CLOUD_ONLY_COMMANDS.items():
        cli.add_command(_build_cloud_only(name, help_text, legacy=True))


def _build_passthrough(
    name: str, help_text: str, *, product: str = "auto", hidden: bool = False
) -> click.Command:
    @click.command(
        name,
        cls=_ForwardingCommand,
        help=help_text,
        hidden=hidden,
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
        if ctx.meta.get("sanka.code_cloud"):
            invoke(name, ctx.obj, ctx.meta["sanka.raw_args"])
            return
        if hidden:
            _legacy_notice(name)
        _run_local(name, ctx.meta["sanka.raw_args"], api_base=ctx.obj.base_url, product=product)

    return passthrough


def _build_hybrid(
    name: str, help_text: str, *, product: str, hidden: bool = False
) -> click.Command:
    @click.command(
        name,
        cls=_ForwardingCommand,
        help=help_text,
        hidden=hidden,
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
        if program_id == "" or migration_id == "":
            raise click.UsageError("--program and --migration require non-empty IDs")
        if program_id is not None or migration_id is not None:
            if product != "app":
                _legacy_notice(name)
            run_cloud_command(
                name,
                ctx.obj,
                program_id=program_id,
                migration_id=migration_id,
                args=args,
            )
            return
        if ctx.meta.get("sanka.code_cloud"):
            invoke(name, ctx.obj, ctx.meta["sanka.raw_args"])
            return
        raw_args = ctx.meta["sanka.raw_args"]
        if product == "code" and _has_file_option(raw_args):
            _legacy_notice(name)
            _run_local(name, raw_args, api_base=ctx.obj.base_url, product="app")
            return
        if hidden:
            _legacy_notice(name)
        _run_local(name, raw_args, api_base=ctx.obj.base_url, product=product)

    return hybrid


def _build_cloud_only(name: str, help_text: str, *, legacy: bool = False) -> click.Command:
    @click.command(
        name,
        cls=_ForwardingCommand,
        help=help_text,
        hidden=legacy,
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
        if legacy:
            _legacy_notice(name)
        run_cloud_command(
            name,
            state,
            program_id=program_id,
            migration_id=migration_id,
            args=args,
        )

    return cloud_only


def _run_local(
    name: str, args: tuple[str, ...], *, api_base: str | None = None, product: str = "auto"
) -> NoReturn:
    from sanka.cli import main as local_main

    raise SystemExit(local_main([name, *args], api_base=api_base, product=product))
