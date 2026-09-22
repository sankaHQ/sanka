# SPDX-License-Identifier: AGPL-3.0-only
import pytest
from click.testing import CliRunner

from sanka_cli.main import cli


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["scan"],
        ["plan"],
        ["extension", "list"],
        ["auth", "login"],
        ["cloud", "list"],
        ["plan", "--cloud"],
        ["plan", "--program", "example"],
        ["repair"],
        ["fix"],
        ["tui"],
    ],
)
def test_help_shortcuts_match_full_command_help(command: list[str]) -> None:
    runner = CliRunner()
    expected = runner.invoke(cli, [*command, "--help"])
    assert expected.exit_code == 0, expected.output
    shortcuts = [[*command, "-h"], [*command, "--h"]]
    if not any(part.startswith("-") for part in command):
        shortcuts.append(["help", *command])
    for args in shortcuts:
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert result.output == expected.output


def test_help_rejects_unknown_command() -> None:
    result = CliRunner().invoke(cli, ["help", "not-a-command"])
    assert result.exit_code == 2
    assert "No such command" in result.output


def test_help_cannot_forward_options_or_end_of_options() -> None:
    for args in (
        ["help", "scan", "--settings", "example"],
        ["help", "scan", "--", "--settings", "example"],
    ):
        assert CliRunner().invoke(cli, args).exit_code == 2
