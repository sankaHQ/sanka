# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import importlib.util
import sys

import pytest
from click.testing import CliRunner

from sanka_cli.main import cli, main


def test_root_help_does_not_offer_a_local_mcp_server() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "\n  mcp " not in result.output
    assert importlib.util.find_spec("sanka_cli.mcp") is None


def test_legacy_mcp_launch_exits_without_protocol_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["sanka", "mcp"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    assert captured.out == ""
    assert "Local MCP was retired in sanka-cli 0.2.9" in captured.err
    assert "https://mcp.sanka.com/mcp" in captured.err
    assert "Connect your Sanka account" in captured.err


def test_root_version_uses_unified_public_version() -> None:
    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert result.output == "sanka, version 0.3.1\n"
