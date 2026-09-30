# SPDX-License-Identifier: AGPL-3.0-only
"""The host refuses silent whole-project fallback and changed reviewed selections."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_extension_lifecycle import FakeRunner, FakeStore, _lifecycle, _project

from sanka.runtime.extensions import ExtensionError
from sanka.runtime.extensions.runner import ExtensionResult
from sanka.runtime.extensions.store import LockEntry


def test_cli_convenience_uses_checked_scope_and_saved_configuration(tmp_path: Path) -> None:
    import json

    from sanka.runtime.extensions.lifecycle import saved_configuration

    class Runner(FakeRunner):
        def run(self, lock: LockEntry, request: dict[str, Any], **kwargs: Any) -> ExtensionResult:
            result = super().run(lock, request, **kwargs)
            if request["command"] != "plan":
                return result
            selected = request["configuration"].get("selected_endpoints", ["GET /one", "POST /one"])
            if "GET /one" not in selected:
                raise ExtensionError("SANKA_SCOPE", "Generated endpoints must remain selected")
            return replace(
                result,
                data=result.data
                | {
                    "endpoint_scope": {
                        "schema": "sanka.endpoint-scope/v1",
                        "effective_ids": sorted(selected),
                        "retained_ids": ["GET /one"],
                        "endpoints": [
                            {"id": key, "blocked": False} for key in ["GET /one", "POST /one"]
                        ],
                    }
                },
            )

    project = _project(tmp_path)
    lifecycle = _lifecycle(project, FakeStore(installed=True), Runner())
    planned = lifecycle.plan(
        target="fastapi", configuration={"output": "generated", "selected_endpoints": ["GET /one"]}
    )
    second = lifecycle.plan(target=None, endpoint_ids=("POST /one",))
    assert second.data["endpoint_scope"]["effective_ids"] == ["GET /one", "POST /one"]
    assert saved_configuration(project, ".sanka") == {
        "target": "fastapi",
        "output": "generated",
        "selected_endpoints": ["GET /one", "POST /one"],
    }
    assert planned.data["plan_hash"] != second.data["plan_hash"]
    with pytest.raises(ExtensionError):
        lifecycle.plan(
            target="fastapi",
            endpoint_ids=("POST /one",),
            configuration={"selected_endpoints": ["GET /one"]},
        )
    with pytest.raises(ExtensionError):
        lifecycle.plan(target="fastapi", configuration={"selected_endpoints": ["POST /one"]})
    path = project / ".sanka" / "plan.json"
    value = json.loads(path.read_text())
    value["configuration"]["output"] = "tampered"
    path.write_text(json.dumps(value))
    assert saved_configuration(project, ".sanka") == {}


@pytest.mark.parametrize("honored", [False, True])
def test_endpoint_plan_contract_and_review_binding(tmp_path: Path, honored: bool) -> None:
    class Runner(FakeRunner):
        def run(self, lock: LockEntry, request: dict[str, Any], **kwargs: Any) -> ExtensionResult:
            result = super().run(lock, request, **kwargs)
            if request["command"] == "plan" and honored:
                return replace(
                    result,
                    data=result.data
                    | {
                        "endpoint_scope": {
                            "schema": "sanka.endpoint-scope/v1",
                            "effective_ids": ["GET /one"],
                        }
                    },
                )
            return result

    project = _project(tmp_path)
    lifecycle = _lifecycle(project, FakeStore(installed=True), Runner())
    if not honored:
        with pytest.raises(ExtensionError):
            lifecycle.plan(target="fastapi", configuration={"selected_endpoints": ["GET /one"]})
        assert not (project / ".sanka" / "plan.json").exists()
        return
    planned = lifecycle.plan(target="fastapi", configuration={"selected_endpoints": ["GET /one"]})
    with pytest.raises(ExtensionError):
        lifecycle.apply(
            reviewed_plan_hash=planned.data["plan_hash"],
            configuration={"selected_endpoints": ["GET /two"]},
        )
    with pytest.raises(ExtensionError):
        lifecycle.verify(
            configuration={"scenarios": "cases.json", "selected_endpoints": ["GET /two"]}
        )
    assert lifecycle.apply(reviewed_plan_hash=planned.data["plan_hash"]).outcome == "success"


def test_changed_output_has_the_same_cli_and_tui_scope(tmp_path: Path, monkeypatch: Any) -> None:
    import sanka.cli as local
    from sanka.cli.tui import launch
    from sanka.cli.tui.services import _dispatch_lifecycle

    class Runner(FakeRunner):
        def run(self, lock: LockEntry, request: dict[str, Any], **kwargs: Any) -> ExtensionResult:
            result = super().run(lock, request, **kwargs)
            if request["command"] == "plan":
                result = replace(
                    result,
                    data=result.data
                    | {
                        "endpoint_scope": {
                            "schema": "sanka.endpoint-scope/v1",
                            "effective_ids": request["configuration"].get(
                                "selected_endpoints", ["GET /one", "GET /two"]
                            ),
                        }
                    },
                )
            return result

    project = _project(tmp_path)
    runner = Runner()
    lifecycle = _lifecycle(project, FakeStore(installed=True), runner)
    lifecycle.plan(
        target="fastapi", configuration={"output": "old", "selected_endpoints": ["GET /one"]}
    )

    def inspect(app: Any) -> int:
        assert app.session.configuration["output"] == "new"
        assert "selected_endpoints" not in app.session.configuration
        _dispatch_lifecycle(
            lifecycle,
            "plan",
            target=app.session.target,
            plan_hash=None,
            configuration=app.session.configuration,
        )
        assert "selected_endpoints" not in runner.calls[-1][1]["configuration"]
        return 0

    monkeypatch.setattr(launch, "_run", inspect)
    launch.launch_local(
        local._build_parser().parse_args(
            [
                "plan",
                str(project),
                "--to",
                "fastapi",
                "--output",
                "new",
                "--tui",
            ]
        )
    )
