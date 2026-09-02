# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sys

import pytest
from click.testing import CliRunner

from sanka_cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_mcp_without_extra_has_install_hint(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, "sanka_cli.mcp.server", raising=False)
    monkeypatch.setitem(sys.modules, "mcp", None)

    result = runner.invoke(cli, ["mcp"])

    assert result.exit_code == 1
    assert "uv tool install 'sanka-cli[mcp]'" in result.output


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
