# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from sanka.cli import _print_framework_test, main
from sanka.runtime.frameworks import load_fastapi_plan, load_framework_scan
from sanka.runtime.frameworks.fastapi_tests import _missing_generated_dependency

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
    assert "next: sanka verify" in test_output
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
    assert "Verified paths" in verify_output
    assert f"Source app:    {drf_project}" in verify_output
    assert f"Generated app: {output}" in verify_output
    assert f"Manifest:      {output / 'sanka-manifest.json'}" in verify_output
    assert "10 / 10 generated" in verify_output
    assert "8 / 8 source-vs-generated probes matched" in verify_output
    assert "Compatibility bridge verification: complete" in verify_output

    with pytest.raises(SystemExit):
        main(["apply", "--root", str(drf_project)])
    assert "--plan-hash" in capsys.readouterr().err

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
    unsupported_plan_output = capsys.readouterr().out
    assert "Needs adaptation\n  1 endpoints" in unsupported_plan_output
    unsupported_plan = load_fastapi_plan(drf_project)
    assert (
        main(
            [
                "apply",
                "--root",
                str(drf_project),
                "--force",
                "--plan-hash",
                unsupported_plan.plan_hash,
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["verify", "--root", str(drf_project), "--no-http"]) == 1
    incomplete_output = capsys.readouterr().out
    assert "9 / 10 generated" in incomplete_output
    assert "skipped with --no-http" in incomplete_output
    assert "Needs adaptation" in incomplete_output
    assert "Compatibility bridge verification: FAILED" in incomplete_output


def test_failed_generated_test_reports_missing_dependency_without_verify_next_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    requirements = tmp_path / "requirements.txt"
    _print_framework_test(
        {
            "ok": False,
            "file": str(tmp_path / "test_generated.py"),
            "tests": 1,
            "allow_writes": False,
            "log": "ModuleNotFoundError: No module named 'tortoise'",
            "missing_dependency": {
                "module": "tortoise",
                "package": "tortoise-orm",
                "requirements": str(requirements),
            },
        }
    )

    output = capsys.readouterr().out
    assert "Generated API tests: FAILED" in output
    assert "Generated app dependency is missing: tortoise" in output
    assert "package `tortoise-orm`" in output
    assert "sanka apply --plan-hash <hash> --force\n  sanka test" in output
    assert "next: sanka verify" not in output


def test_compatibility_stream_propagates_failure_after_response_start(
    drf_project: Path,
) -> None:
    assert main(["scan", str(drf_project)]) == 0
    assert main(["plan", str(drf_project), "--to", "fastapi", "--strategy", "compatibility"]) == 0
    plan = load_fastapi_plan(drf_project)
    assert main(["apply", "--root", str(drf_project), "--plan-hash", plan.plan_hash]) == 0
    output = drf_project / ".sanka" / "output" / "fastapi"
    script = """
import asyncio
from starlette.requests import Request
import sanka_compat as bridge

async def failing_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"partial", "more_body": True})
    raise RuntimeError("failed after response start")

async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}

async def check():
    bridge.DJANGO_APP = failing_app
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/", "raw_path": b"/",
        "query_string": b"", "root_path": "", "headers": [],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    response = await bridge._dispatch(Request(scope, receive), "GET")

    async def consume():
        return [chunk async for chunk in response.body_iterator]

    try:
        await asyncio.wait_for(consume(), timeout=1)
    except RuntimeError as error:
        assert str(error) == "failed after response start"
    else:
        raise AssertionError("stream failure was swallowed")

asyncio.run(check())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_generated_test_extracts_actionable_missing_dependency(tmp_path: Path) -> None:
    dependency = _missing_generated_dependency(
        "ModuleNotFoundError: No module named 'tortoise.backends'", tmp_path
    )

    assert dependency == {
        "module": "tortoise",
        "package": "tortoise-orm",
        "requirements": str(tmp_path / "requirements.txt"),
    }
    assert (
        _missing_generated_dependency(
            "ModuleNotFoundError: No module named 'project_source'", tmp_path
        )
        is None
    )


def test_failed_generated_test_without_missing_dependency_does_not_suggest_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _print_framework_test(
        {
            "ok": False,
            "file": str(tmp_path / "test_generated.py"),
            "tests": 2,
            "allow_writes": False,
            "log": "AssertionError: response status differed",
            "missing_dependency": None,
        }
    )

    output = capsys.readouterr().out
    assert "Fix the test failure above, then rerun:\n  sanka test" in output
    assert "next: sanka verify" not in output
