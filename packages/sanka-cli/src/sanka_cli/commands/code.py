# SPDX-License-Identifier: Apache-2.0
"""Compatibility CLI group for the former custom-function command namespace."""

import click

from sanka_cli.commands.functions import functions


@click.group(hidden=True)
def code() -> None:
    """Compatibility alias for functions; Sanka Code migrations use scan/plan/apply."""
    click.echo(
        "Use `sanka functions` for custom functions; `sanka code` is a compatibility alias.",
        err=True,
    )


for name, command in functions.commands.items():
    code.add_command(command, name)
