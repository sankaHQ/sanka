# SPDX-License-Identifier: AGPL-3.0-only
"""Native generation for the token-auth + object-permission envelope.

Every ``sanka`` invocation runs in a fresh subprocess (one Django project per
process; see test_native_fastapi)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PROBE = Path(__file__).parent / "auth_parity_probe.py"

ALICE = {"Authorization": "Token " + "a" * 40}
INACTIVE = {"Authorization": "Token " + "c" * 40}

SCENARIOS: list[dict[str, Any]] = [
    {"method": "GET", "path": "/api/bulletins/"},
    {"method": "GET", "path": "/api/bulletins/", "headers": {"Authorization": "Token " + "x" * 40}},
    {"method": "GET", "path": "/api/bulletins/", "headers": {"Authorization": "Token"}},
    {"method": "GET", "path": "/api/bulletins/", "headers": {"Authorization": "Token a b"}},
    {"method": "GET", "path": "/api/bulletins/", "headers": INACTIVE},
    {"method": "GET", "path": "/api/bulletins/", "headers": ALICE},
    {"method": "GET", "path": "/api/"},
    {
        "method": "POST",
        "path": "/api/bulletins/",
        "headers": ALICE,
        "body": {"title": "Third", "body": "fresh"},
    },
    {"method": "POST", "path": "/api/bulletins/", "body": {"title": "Nope"}},
    {"method": "POST", "path": "/api/bulletins/", "headers": ALICE, "body": {"title": ""}},
    {"method": "GET", "path": "/api/bulletins/2/", "headers": ALICE},
    {"method": "PATCH", "path": "/api/bulletins/2/", "headers": ALICE, "body": {"title": "hax"}},
    {"method": "DELETE", "path": "/api/bulletins/2/", "headers": ALICE},
    {
        "method": "PUT",
        "path": "/api/bulletins/1/",
        "headers": ALICE,
        "body": {"title": "First2", "body": "b"},
    },
    {"method": "PATCH", "path": "/api/bulletins/999/", "headers": ALICE, "body": {"title": "x"}},
    {"method": "GET", "path": "/api/bulletins/abc/", "headers": ALICE},
    {"method": "DELETE", "path": "/api/bulletins/1/", "headers": ALICE},
    {"method": "GET", "path": "/api/bulletins/", "headers": ALICE},
]


def _clean_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("SANKA_TEST_DB", None)
    return env


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from sanka.cli import main; sys.exit(main(sys.argv[1:]))",
            *args,
        ],
        cwd=cwd,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _plan_hash(project: Path) -> str:
    payload = json.loads((project / ".sanka" / "plan-fastapi.json").read_text())
    return str(payload["plan_hash"])


def _run_probe(
    mode: str, project: Path, database: Path, *, output: Path | None = None
) -> dict[str, Any]:
    argv = [
        sys.executable,
        str(PROBE),
        "--mode",
        mode,
        "--project",
        str(project),
        "--database",
        str(database),
        "--scenarios",
        json.dumps(SCENARIOS),
    ]
    if output is not None:
        argv.extend(["--output", str(output)])
    outcome = subprocess.run(
        argv,
        cwd=project,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert outcome.returncode == 0, outcome.stderr
    lines = [line for line in outcome.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return payload


@pytest.fixture
def auth_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "drf_auth_project", project)
    return project


def _generate(project: Path) -> Path:
    scan = _run_cli(["scan", str(project)], project)
    assert scan.returncode == 0, scan.stderr
    plan = _run_cli(["plan", str(project), "--to", "fastapi"], project)
    assert plan.returncode == 0, plan.stderr
    assert "Native migration readiness: 100%" in plan.stdout
    applied = _run_cli(
        ["apply", "--root", str(project), "--plan-hash", _plan_hash(project)], project
    )
    assert applied.returncode == 0, applied.stderr
    return project / ".sanka" / "output" / "fastapi"


def test_auth_fixture_generates_native_output(auth_project: Path) -> None:
    output = _generate(auth_project)
    manifest = json.loads((output / "sanka-manifest.json").read_text(encoding="utf-8"))
    resource = manifest["resources"][0]
    auth = resource["auth"]
    assert auth["token_keyword"] == "Token"
    assert auth["token_db_table"] == "authtoken_token"
    assert auth["owner_attname"] == "author_id"
    assert auth["inject_owner_attname"] == "author_id"
    assert auth["messages"]["no_credentials"] == "Authentication credentials were not provided."
    assert auth["messages"]["www_authenticate"] == "Token"
    author_field = next(f for f in resource["fields"] if f["name"] == "author")
    assert author_field["kind"] == "related_pk"
    assert author_field["attname"] == "author_id"
    runtime_text = (output / "sanka_native.py").read_text(encoding="utf-8")
    assert "rest_framework" not in runtime_text
    assert "django.setup" not in runtime_text
    assert "import django" not in runtime_text
    assert not (output / "sanka_settings.py").exists()


def test_auth_native_output_matches_drf(auth_project: Path, tmp_path: Path) -> None:
    output = _generate(auth_project)
    source = _run_probe("source", auth_project, tmp_path / "source.sqlite3")
    native = _run_probe("native", auth_project, tmp_path / "native.sqlite3", output=output)
    for index, (left, right) in enumerate(zip(source["results"], native["results"], strict=True)):
        assert left == right, f"scenario {index} ({SCENARIOS[index]}): {left} != {right}"
    assert source["database"] == native["database"]


def test_arbitrary_permission_logic_stays_outside_the_envelope(
    auth_project: Path,
) -> None:
    permissions = auth_project / "bulletins" / "permissions.py"
    permissions.write_text(
        permissions.read_text(encoding="utf-8").replace(
            "return obj.author_id == request.user.id",
            "return bool(request.user.is_staff)",
        ),
        encoding="utf-8",
    )
    scan = _run_cli(["scan", str(auth_project)], auth_project)
    assert scan.returncode == 0, scan.stderr
    plan = _run_cli(["plan", str(auth_project), "--to", "fastapi", "--json"], auth_project)
    assert plan.returncode == 0, plan.stderr
    payload = json.loads(plan.stdout)
    viewset_routes = [
        route
        for route in payload["routes"]
        if "bulletins" in route["path"] and "{format}" not in route["path"]
    ]
    assert viewset_routes
    assert all(route["strategy"] == "needs-manual-adaptation" for route in viewset_routes)
