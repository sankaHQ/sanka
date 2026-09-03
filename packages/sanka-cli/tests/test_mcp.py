# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import builtins
import sys
from collections.abc import Mapping, Sequence
from types import ModuleType

import pytest
from click.testing import CliRunner

from sanka_cli.main import cli, main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_root_version_uses_unified_public_version(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert result.output == "sanka, version 0.2.2\n"


def test_mcp_client_uses_the_cli_version_in_its_user_agent() -> None:
    from sanka_cli.mcp.client import SankaMigrateApiClient

    client = SankaMigrateApiClient()

    assert client._client.headers["User-Agent"] == "sanka-cli/0.2.2"


def test_mcp_without_extra_has_install_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delitem(sys.modules, "sanka_cli.mcp.server", raising=False)
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setattr(sys, "argv", ["sanka", "mcp"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    assert captured.out == ""
    assert captured.err == "Error: Install MCP support with: uv tool install 'sanka-cli[mcp]'\n"


def test_mcp_reraises_transitive_module_not_found(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_import = builtins.__import__

    def fail_transitive_import(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = None,
        level: int = 0,
    ) -> ModuleType:
        if name == "sanka_cli.mcp.server":
            raise ModuleNotFoundError(
                "No module named 'transitive_dependency'", name="transitive_dependency"
            )
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "sanka_cli.mcp.server", raising=False)
    monkeypatch.setattr(builtins, "__import__", fail_transitive_import)

    result = runner.invoke(cli, ["mcp"])

    assert isinstance(result.exception, ModuleNotFoundError)
    assert result.exception.name == "transitive_dependency"
    assert "Install MCP support" not in result.output


@pytest.mark.asyncio
async def test_mcp_tool_names() -> None:
    from sanka_cli.mcp import server

    tools = {tool.name for tool in await server.mcp.list_tools()}

    assert tools == {
        "sanka_research_eol",
        "sanka_research_tco",
        "sanka_research_compare",
        "sanka_assess",
    }
