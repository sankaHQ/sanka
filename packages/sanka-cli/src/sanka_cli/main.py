# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import click

from sanka_cli import __version__
from sanka_cli.commands.ai import ai
from sanka_cli.commands.auth import auth, auth_login, auth_logout, auth_status
from sanka_cli.commands.cloud import cloud
from sanka_cli.commands.code import code
from sanka_cli.commands.flow import flow
from sanka_cli.commands.functions import functions
from sanka_cli.commands.migrate import register_migration_passthroughs
from sanka_cli.commands.profiles import profiles
from sanka_cli.commands.resources import attach_resource_group
from sanka_cli.commands.skill import skill
from sanka_cli.commands.workflows import workflows
from sanka_cli.output import print_error
from sanka_cli.state import CLIState


@click.group()
@click.version_option(__version__, prog_name="sanka")
@click.option("--profile", default=None, help="Profile name to use.")
@click.option("--base-url", default=None, help="Override API base URL.")
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default=None,
    help="Output format. Defaults to table on TTY and JSON otherwise.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    profile: str | None,
    base_url: str | None,
    output: str | None,
) -> None:
    ctx.obj = CLIState(profile=profile, base_url=base_url, output=output)


cli.add_command(auth)
cli.add_command(auth_login, "login")
cli.add_command(auth_logout, "logout")
cli.add_command(auth_status, "whoami")
cli.add_command(profiles)
cli.add_command(workflows)
cli.add_command(flow)
cli.add_command(ai)
cli.add_command(functions)
cli.add_command(code)
cli.add_command(cloud)
cli.add_command(skill)

attach_resource_group(cli, "companies", "/v2/public/companies")
attach_resource_group(cli, "contacts", "/v2/public/contacts")
attach_resource_group(cli, "deals", "/v2/public/deals")
attach_resource_group(cli, "tickets", "/v2/public/tickets")

register_migration_passthroughs(cli)


@cli.command("mcp", hidden=True)
def mcp_command() -> None:
    """Explain retirement to clients still configured to launch the old server."""
    raise click.ClickException(
        "Local MCP was retired in sanka-cli 0.2.9. "
        "Configure your MCP client to use https://mcp.sanka.com/mcp instead. "
        "Connect your Sanka account when prompted."
    )


def main() -> None:
    try:
        cli(standalone_mode=False)
    except click.ClickException as exc:
        print_error(f"Error: {exc.format_message()}")
        raise SystemExit(exc.exit_code) from exc
