# SPDX-License-Identifier: AGPL-3.0-only
"""The TUI uses the same conditional values as plain CLI Plan settings."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Input, Select
from tui_helpers import DECLARATION, FakeServices

from sanka.cli.tui.app import PlanConfiguration, SankaApp
from sanka.cli.tui.model import Session
from sanka.runtime.extensions.settings import parse_settings


@pytest.mark.asyncio
async def test_settings_remain_visible_for_either_database_choice(tmp_path: Path) -> None:
    declaration: dict[str, Any] = deepcopy(DECLARATION)
    declaration["settings"][1]["when"] = {"orm": ["django", "sqlalchemy"]}
    declaration["settings"][0]["choices"].append(
        {"value": "none", "label": {"en": "None", "ja": "なし"}}
    )
    declaration["settings"].append(
        {
            "id": "source_file",
            "stage": "plan",
            "type": "path",
            "default": "app.py",
            "optional": True,
            "label": {"en": "Entrypoint", "ja": "エントリーポイント"},
        }
    )
    service = FakeServices()
    service.settings = {"sanka/drf-to-fastapi": parse_settings(declaration)}
    app = SankaApp(service, Session(project_root=str(tmp_path)), start="plan")
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PlanConfiguration)
        assert app.screen.query_one("#plan-field-generation").display is True
        app.screen.query_one("#plan-set-orm", Select).value = "sqlalchemy"
        await pilot.pause()
        assert app.screen.query_one("#plan-field-generation").display is True
        app.screen.query_one("#save-configuration", Button).press()
        await pilot.pause()
        await pilot.click("#run-stage")
        await app.workers.wait_for_complete()
        assert service.calls[0]["configuration"]["generation"] == "minimal"
        await pilot.click("#configure-stage")
        await pilot.pause()
        app.screen.query_one("#plan-set-orm", Select).value = "none"
        app.screen.query_one("#plan-set-source_file", Input).value = ""
        await pilot.pause()
        app.screen.query_one("#save-configuration", Button).press()
        await pilot.pause()
        assert "generation" not in app.session.configuration
        assert "source_file" not in app.session.configuration
