# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from sanka.cli import main
from sanka.runtime.frameworks import load_fastapi_plan, load_framework_scan

FIXTURE = Path(__file__).parent / "fixtures" / "drf_project"


@pytest.fixture
def drf_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    monkeypatch.chdir(project)
    return project


def test_five_command_drf_to_fastapi_lifecycle(
    drf_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scan", str(drf_project)]) == 0
    scan_output = capsys.readouterr().out
    assert "Django" in scan_output
    assert "DRF" in scan_output
    assert "10 endpoints" in scan_output
    assert "1 serializers" in scan_output
    assert "1 models" in scan_output
    assert "1 permissions" in scan_output
    assert "1 custom actions" in scan_output
    assert "sanka plan --to fastapi" in scan_output

    scan = load_framework_scan(drf_project)
    assert {route.key for route in scan.routes} == {
        "GET /api/health/",
        "GET /api/error/",
        "GET /api/search/",
        "GET /api/projects/",
        "POST /api/projects/",
        "GET /api/projects/{pk}/",
        "PUT /api/projects/{pk}/",
        "PATCH /api/projects/{pk}/",
        "DELETE /api/projects/{pk}/",
        "GET /api/projects/featured/",
    }
    assert scan.serializers == ("reference_api.serializers.ProjectSerializer",)
    assert scan.models == ("reference_api.models.Project",)
    assert scan.permissions == ("rest_framework.permissions.IsAuthenticated",)
    assert scan.authentication == ("reference_api.views.ReferenceHeaderAuthentication",)
    assert next(route for route in scan.routes if route.operation == "featured").transactional
    assert not next(route for route in scan.routes if route.operation == "list").transactional

    assert main(["plan", str(drf_project), "--to", "fastapi", "--strategy", "compatibility"]) == 0
    plan_output = capsys.readouterr().out
    assert "DRF → FastAPI Migration Plan" in plan_output
    assert "Bridge generation readiness: 100%" in plan_output

    plan = load_fastapi_plan(drf_project)
    assert main(["apply", "--root", str(drf_project), "--plan-hash", plan.plan_hash]) == 0
    apply_output = capsys.readouterr().out
    assert "generated 10 FastAPI routes" in apply_output
    assert "next: sanka test" in apply_output

    output = drf_project / ".sanka" / "output" / "fastapi"
    assert (output / "app.py").is_file()
    manifest = json.loads((output / "sanka-manifest.json").read_text(encoding="utf-8"))
    assert manifest["plan_hash"] == plan.plan_hash

    assert main(["test", "--root", str(drf_project), "--to", "fastapi"]) == 0
    test_output = capsys.readouterr().out
    assert "Generated API tests: OK" in test_output
    assert (output / "test_generated.py").is_file()

    cases = drf_project / ".sanka" / "verify-cases.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "method": "GET",
                        "path": "/api/projects/",
                        "headers": {"X-Reference-User": "launch-test"},
                    },
                    {
                        "method": "GET",
                        "path": "/api/projects/p-1/",
                        "headers": {"X-Reference-User": "launch-test"},
                    },
                    {"method": "GET", "path": "/api/search/?page=2"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert main(["verify", "--root", str(drf_project)]) == 0
    verify_output = capsys.readouterr().out
    assert "10 / 10 generated" in verify_output
    assert "8 / 8 read-only routes compatible" in verify_output
    assert "Compatibility bridge verification: complete" in verify_output

    assert main(["apply", "--root", str(drf_project)]) == 1
    assert "output is not empty" in capsys.readouterr().err

    assert main(["apply", "--root", str(drf_project), "--plan-hash", "sha256:wrong"]) == 1
    assert "does not match current plan" in capsys.readouterr().err

    unsupported = replace(
        scan,
        routes=(replace(scan.routes[0], supported=False), *scan.routes[1:]),
        scan_hash="",
    ).with_hash()
    (drf_project / ".sanka" / "scan.json").write_text(
        json.dumps(unsupported.to_dict()),
        encoding="utf-8",
    )
    assert main(["plan", str(drf_project), "--to", "fastapi", "--strategy", "compatibility"]) == 0
    assert main(["apply", "--root", str(drf_project), "--force"]) == 0
    capsys.readouterr()

    assert main(["verify", "--root", str(drf_project), "--no-http"]) == 1
    incomplete_output = capsys.readouterr().out
    assert "9 / 10 generated" in incomplete_output
    assert "Needs adaptation" in incomplete_output
    assert "Compatibility bridge verification: FAILED" in incomplete_output
