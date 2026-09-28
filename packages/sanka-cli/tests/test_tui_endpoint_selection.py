# SPDX-License-Identifier: AGPL-3.0-only
import pytest
from textual.widgets import Checkbox, Input
from tui_helpers import FakeServices

from sanka.cli.tui.app import EndpointSelection, SankaApp
from sanka.cli.tui.model import EndpointChoice, Session


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (100, 35)])
async def test_endpoint_picker_keeps_generated_rows_when_filtering_and_bulk_clearing(
    size: tuple[int, int],
) -> None:
    app = SankaApp(FakeServices(), Session(project_root="/work"), start="status")
    choices = [
        EndpointChoice("GET /one", "GET", "/one", "already_implemented", True, True),
        EndpointChoice("POST /one", "POST", "/one", "not_migrated", True),
        EndpointChoice("GET /two", "GET", "/two", "not_migrated", True),
        EndpointChoice("GET /manual", "GET", "/manual", "unknown", False, blocked=True),
    ]
    selected: list[list[str] | None] = []
    async with app.run_test(size=size) as pilot:
        app.push_screen(EndpointSelection(choices), selected.append)
        await pilot.pause()
        app.screen.query_one("#endpoint-filter", Input).value = "POST"
        await pilot.click("#endpoints-none")
        assert app.screen.query_one("#endpoint-0", Checkbox).value
        assert app.screen.query_one("#endpoint-0", Checkbox).disabled
        assert not app.screen.query_one("#endpoint-2", Checkbox).value
        await pilot.click("#endpoints-all")
        assert app.screen.query_one("#endpoint-2", Checkbox).value
        assert not app.screen.query_one("#endpoint-3", Checkbox).value
        await pilot.click("#endpoint-1")
        await pilot.click("#endpoints-save")
        await pilot.pause()
    assert selected == [["GET /one", "GET /two"]]
