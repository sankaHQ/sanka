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
