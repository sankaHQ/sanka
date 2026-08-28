# SPDX-License-Identifier: AGPL-3.0-only
"""Native DRF→FastAPI generation lifecycle.

Every ``sanka`` invocation here runs in a fresh subprocess: the lifecycle
configures Django globally per process, and this suite must not poison (or be
poisoned by) the in-process compatibility-bridge tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sanka.runtime.frameworks.django_fastapi import _unsupported_middleware
from sanka.runtime.hashing import content_hash

FIXTURES = Path(__file__).parent / "fixtures"
PROBE = Path(__file__).parent / "native_parity_probe.py"
# Exact settings.MIDDLEWARE values captured by the 2026-08-28 nine-app OSS rescan.
REAL_WORLD_MIDDLEWARE_STACKS: dict[str, list[str]] = json.loads(
    (FIXTURES / "oss_drf_middleware_stacks.json").read_text(encoding="utf-8")
)
SAFE_MIDDLEWARE_STACK = (
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
)
EXPECTED_UNSUPPORTED_MIDDLEWARE_COUNTS = {
    "styleguide-example": 2,
    "django-crm": 3,
    "care": 4,
    "djangoforapis": 1,
    "readthedocs": 10,
    "netbox": 7,
    "defectdojo": 12,
    "peering-manager": 3,
    "kitsune": 28,
}

SCENARIOS: list[dict[str, Any]] = [
    {"method": "GET", "path": "/api/gadgets/"},
    {"method": "POST", "path": "/api/gadgets/", "body": {"name": "Beta", "quantity": 7}},
    {"method": "PATCH", "path": "/api/gadgets/1/", "body": {"quantity": 9}},
    {"method": "POST", "path": "/api/gadgets/", "body": {"name": "Bad", "quantity": -1}},
    {"method": "POST", "path": "/api/gadgets/", "body": {"name": "", "quantity": 1}},
    {"method": "POST", "path": "/api/gadgets/", "body": {"quantity": 2}},
    {"method": "POST", "path": "/api/gadgets/", "body": {"name": "X", "quantity": "12"}},
    {"method": "POST", "path": "/api/gadgets/", "body": {"name": "Y", "quantity": "7.5"}},
    {"method": "GET", "path": "/api/gadgets/999/"},
    {"method": "GET", "path": "/api/gadgets/abc/"},
    {
        "method": "PUT",
        "path": "/api/gadgets/2/",
        "body": {"name": "Beta2", "quantity": 1, "notes": "ok"},
    },
    {"method": "PUT", "path": "/api/gadgets/2/", "body": {"name": "OnlyName"}},
    {"method": "DELETE", "path": "/api/gadgets/3/"},
    {"method": "GET", "path": "/api/"},
    {"method": "POST", "path": "/api/gadgets/", "raw_body": "{"},
    {"method": "POST", "path": "/api/gadgets/", "body": {"name": "Padded ", "quantity": 0}},
    {"method": "GET", "path": "/api/gadgets/"},
]


def _clean_env() -> dict[str, str]:
    """Subprocess environment without Django state leaked by in-process tests."""
    import os

    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("SANKA_TEST_DB", None)
    return env


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = _clean_env()
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from sanka.cli import main; sys.exit(main(sys.argv[1:]))",
            *args,
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _run_probe(
    mode: str,
    project: Path,
    database: Path,
    *,
    output: Path | None = None,
) -> dict[str, Any]:
    env = _clean_env()
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
        argv, cwd=project, env=env, capture_output=True, text=True, timeout=180, check=False
    )
    assert outcome.returncode == 0, outcome.stderr
    lines = [line for line in outcome.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return payload


def _set_middleware(settings: Path, middleware: list[str] | tuple[str, ...]) -> None:
    source = settings.read_text(encoding="utf-8")
    if "MIDDLEWARE: list[str] = []" in source:
        source = source.replace("MIDDLEWARE: list[str] = []", f"MIDDLEWARE = {list(middleware)!r}")
    else:
        source = source.replace("MIDDLEWARE = []", f"MIDDLEWARE = {list(middleware)!r}")
    settings.write_text(source, encoding="utf-8")


@pytest.fixture
def crud_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "drf_crud_project", project)
    return project


def _generate(project: Path) -> Path:
    scan = _run_cli(["scan", str(project)], project)
    assert scan.returncode == 0, scan.stderr
    plan = _run_cli(["plan", str(project), "--to", "fastapi"], project)
    assert plan.returncode == 0, plan.stderr
    assert "DRF → FastAPI Migration Plan (native)" in plan.stdout
    assert "Native migration readiness: 100%" in plan.stdout
    applied = _run_cli(["apply", "--root", str(project)], project)
    assert applied.returncode == 0, applied.stderr
    assert "native FastAPI routes" in applied.stdout
    return project / ".sanka" / "output" / "fastapi"


def test_native_lifecycle_generates_verifiable_output(crud_project: Path) -> None:
    output = _generate(crud_project)
    for name in ("app.py", "sanka_native.py", "sanka_store.py", "models.py", "sanka-manifest.json"):
        assert (output / name).is_file()
    assert not (output / "sanka_settings.py").exists()
    manifest = json.loads((output / "sanka-manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "native"
    assert manifest["sql_engine"] == "tortoise"
    generated_keys = {f"{route['method']} {route['path']}" for route in manifest["routes"]}
    assert "GET /api/gadgets/" in generated_keys
    assert "GET /api/" in generated_keys
    assert all("{format}" not in key for key in generated_keys)
    assert manifest["dropped_routes"]
    runtime_text = (output / "sanka_native.py").read_text(encoding="utf-8")
    for forbidden in (
        "rest_framework",
        "get_asgi_application",
        "django.core.asgi",
        "_dispatch",
        "django.setup",
        "import django",
    ):
        assert forbidden not in runtime_text
    store_text = (output / "sanka_store.py").read_text(encoding="utf-8")
    assert "Tortoise" in store_text
    assert "_enable_global_fallback=True" in store_text
    assert "import django" not in store_text
    requirements = (output / "requirements.txt").read_text(encoding="utf-8")
    assert "tortoise-orm>=1.1,<2" in requirements
    app_text = (output / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/api/gadgets/")' in app_text
    assert '@app.post("/api/gadgets/")' in app_text
    assert '@app.get("/api/gadgets/{pk}/")' in app_text
    assert "add_api_route" not in app_text
    assert "add_api_route" not in runtime_text
    assert "async def list_gadget(" in app_text
    assert "async def create_gadget(" in app_text
    assert "await native.handle" in app_text
    assert "lifespan" in app_text
    assert "finally:" in app_text

    # A real project has a migrated database before verification; the fixture
    # starts from a fresh copy, so create its schema first.
    migrated = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--run-syncdb", "--verbosity", "0"],
        cwd=crud_project,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert migrated.returncode == 0, migrated.stderr
    tested = _run_cli(["test", "--root", str(crud_project), "--to", "fastapi"], crud_project)
    assert tested.returncode == 0, tested.stdout + tested.stderr
    assert "Generated API tests: OK" in tested.stdout
    assert (output / "test_generated.py").is_file()
    generated_tests = (output / "test_generated.py").read_text(encoding="utf-8")
    assert "test_gadgetviewset_create_roundtrip" in generated_tests
    assert "import django" not in generated_tests
    verified = _run_cli(["verify", "--root", str(crud_project)], crud_project)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "Native migration verification: complete" in verified.stdout


def test_native_output_matches_drf_behavior_and_database(
    crud_project: Path, tmp_path: Path
) -> None:
    output = _generate(crud_project)
    source = _run_probe("source", crud_project, tmp_path / "source.sqlite3")
    native = _run_probe("native", crud_project, tmp_path / "native.sqlite3", output=output)
    for index, (left, right) in enumerate(zip(source["results"], native["results"], strict=True)):
        assert left == right, f"scenario {index} ({SCENARIOS[index]}): {left} != {right}"
    assert source["database"] == native["database"]


def test_native_apply_is_deterministic(crud_project: Path) -> None:
    first = _generate(crud_project)
    contents = {path.name: path.read_bytes() for path in sorted(first.iterdir()) if path.is_file()}
    applied = _run_cli(["apply", "--root", str(crud_project), "--force"], crud_project)
    assert applied.returncode == 0, applied.stderr
    for path in sorted(first.iterdir()):
        if path.is_file():
            assert path.read_bytes() == contents[path.name], path.name


def test_bench_candidate_emission(crud_project: Path) -> None:
    _generate(crud_project)
    applied = _run_cli(
        ["apply", "--root", str(crud_project), "--force", "--bench-candidate", "candidate"],
        crud_project,
    )
    assert applied.returncode == 0, applied.stderr
    candidate = crud_project / "candidate"
    assert (candidate / "candidate.yaml").is_file()
    assert (candidate / "overlay" / "target_app.py").is_file()
    manifest = json.loads(
        (candidate / "overlay" / "sanka-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["source_root"] == "."
    assert manifest["entrypoint"] == "target_app.py"
    assert manifest["sql_engine"] == "django"
    assert (candidate / "overlay" / "sanka_settings.py").is_file()
    assert not (candidate / "overlay" / "models.py").exists()
    store = (candidate / "overlay" / "sanka_store.py").read_text(encoding="utf-8")
    assert "retained Django ORM" in store
    assert "from tortoise" not in store
    requirements = (candidate / "overlay" / "requirements.txt").read_text(encoding="utf-8")
    assert "django" in requirements
    assert "tortoise" not in requirements
    text = (candidate / "candidate.yaml").read_text(encoding="utf-8")
    assert "schema_version: sanka-bench/candidate/v0.1" in text
    assert "producer: sanka" in text


def test_native_plan_refuses_routes_outside_the_envelope(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "drf_project", project)
    scan = _run_cli(["scan", str(project)], project)
    assert scan.returncode == 0, scan.stderr
    plan = _run_cli(["plan", str(project), "--to", "fastapi", "--json"], project)
    assert plan.returncode == 0, plan.stderr
    payload = json.loads(plan.stdout)
    assert payload["mode"] == "native"
    # APIViews, a custom action, and an IsAuthenticated viewset are all outside
    # the native envelope: nothing may be silently bridged.
    assert payload["automatic_routes"] == 0
    assert all(route["strategy"] == "needs-manual-adaptation" for route in payload["routes"])
    assert all(route["adaptation_reasons"] for route in payload["routes"])
    reason_codes = {
        reason["code"] for route in payload["routes"] for reason in route["adaptation_reasons"]
    }
    assert "SANKA_DRF_VIEW_KIND_UNSUPPORTED" in reason_codes
    assert "SANKA_DRF_CUSTOM_ACTION_UNSUPPORTED" in reason_codes


@pytest.mark.parametrize("app", sorted(EXPECTED_UNSUPPORTED_MIDDLEWARE_COUNTS))
def test_real_world_middleware_stacks_only_reject_classes_outside_allowlist(app: str) -> None:
    stack = tuple(REAL_WORLD_MIDDLEWARE_STACKS[app])
    unsupported = _unsupported_middleware(stack)
    assert len(unsupported) == EXPECTED_UNSUPPORTED_MIDDLEWARE_COUNTS[app]
    assert tuple(item for item in stack if item not in unsupported) == tuple(
        item for item in stack if item in SAFE_MIDDLEWARE_STACK
    )


def test_native_plan_allows_known_safe_middleware_stack(crud_project: Path) -> None:
    _set_middleware(crud_project / "crud_config" / "settings.py", SAFE_MIDDLEWARE_STACK)
    scan = _run_cli(["scan", str(crud_project)], crud_project)
    assert scan.returncode == 0, scan.stderr
    plan = _run_cli(["plan", str(crud_project), "--to", "fastapi", "--json"], crud_project)
    assert plan.returncode == 0, plan.stderr
    payload = json.loads(plan.stdout)
    assert payload["native_routes"] == payload["native_eligible_routes"]
    assert payload["readiness"] == 1.0
    assert all(
        reason["code"] != "SANKA_DRF_MIDDLEWARE_UNSUPPORTED"
        for route in payload["routes"]
        for reason in route["adaptation_reasons"]
    )


def test_native_plan_continues_past_unsupported_middleware(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "drf_project", project)
    _set_middleware(
        project / "config" / "settings.py", REAL_WORLD_MIDDLEWARE_STACKS["djangoforapis"]
    )
    scan = _run_cli(["scan", str(project)], project)
    assert scan.returncode == 0, scan.stderr
    plan = _run_cli(["plan", str(project), "--to", "fastapi", "--json"], project)
    assert plan.returncode == 0, plan.stderr
    payload = json.loads(plan.stdout)

    def reason_codes(path: str) -> list[str]:
        route = next(route for route in payload["routes"] if route["path"] == path)
        return [reason["code"] for reason in route["adaptation_reasons"]]

    assert reason_codes("/api/health/") == [
        "SANKA_DRF_MIDDLEWARE_UNSUPPORTED",
        "SANKA_DRF_VIEW_KIND_UNSUPPORTED",
    ]
    assert reason_codes("/api/projects/") == [
        "SANKA_DRF_MIDDLEWARE_UNSUPPORTED",
        "SANKA_DRF_VIEWSET_OVERRIDES_UNSUPPORTED",
    ]
    assert reason_codes("/api/projects/featured/") == [
        "SANKA_DRF_MIDDLEWARE_UNSUPPORTED",
        "SANKA_DRF_CUSTOM_ACTION_UNSUPPORTED",
    ]
    manual = [
        route for route in payload["routes"] if route["strategy"] == "needs-manual-adaptation"
    ]
    assert all(
        any(reason["code"] != "SANKA_DRF_MIDDLEWARE_UNSUPPORTED" for reason in reasons)
        for route in manual
        if (reasons := route["adaptation_reasons"])
    )

    compatibility = _run_cli(
        [
            "plan",
            str(project),
            "--to",
            "fastapi",
            "--strategy",
            "compatibility",
            "--json",
        ],
        project,
    )
    assert compatibility.returncode == 0, compatibility.stderr
    compatibility_payload = json.loads(compatibility.stdout)
    assert compatibility_payload["automatic_routes"] == len(compatibility_payload["routes"])
    assert compatibility_payload["readiness"] == 1.0


def test_native_plan_explains_unsupported_middleware_and_separates_alias_drops(
    crud_project: Path,
) -> None:
    settings = crud_project / "crud_config" / "settings.py"
    _set_middleware(
        settings,
        REAL_WORLD_MIDDLEWARE_STACKS["styleguide-example"],
    )
    scan = _run_cli(["scan", str(crud_project)], crud_project)
    assert scan.returncode == 0, scan.stderr
    plan_json = _run_cli(["plan", str(crud_project), "--to", "fastapi", "--json"], crud_project)
    assert plan_json.returncode == 0, plan_json.stderr
    payload = json.loads(plan_json.stdout)
    assert payload["automatic_routes"] == 0
    assert payload["native_routes"] == 0
    assert payload["native_eligible_routes"] > 0
    assert payload["dropped_alias_routes"] > 0
    assert payload["readiness"] == 0.0
    assert payload["alias_drop_rate"] > 0.0
    manual = [
        route for route in payload["routes"] if route["strategy"] == "needs-manual-adaptation"
    ]
    assert manual
    assert all(
        any(
            reason["code"] == "SANKA_DRF_MIDDLEWARE_UNSUPPORTED"
            and reason["feature"] == "middleware"
            for reason in route["adaptation_reasons"]
        )
        for route in manual
    )
    aliases = [
        route for route in payload["routes"] if route["strategy"] == "dropped-format-suffix-alias"
    ]
    assert aliases
    assert all(not route["adaptation_reasons"] for route in aliases)

    plan_text = _run_cli(["plan", str(crud_project), "--to", "fastapi"], crud_project)
    assert plan_text.returncode == 0, plan_text.stderr
    assert "Native migration readiness: 0%" in plan_text.stdout
    assert "Format-suffix aliases dropped:" in plan_text.stdout
    assert "SANKA_DRF_MIDDLEWARE_UNSUPPORTED (middleware)" in plan_text.stdout


def test_native_plan_explains_middleware_in_legacy_scan(crud_project: Path) -> None:
    settings = crud_project / "crud_config" / "settings.py"
    _set_middleware(
        settings,
        REAL_WORLD_MIDDLEWARE_STACKS["djangoforapis"],
    )
    scan = _run_cli(["scan", str(crud_project)], crud_project)
    assert scan.returncode == 0, scan.stderr
    scan_path = crud_project / ".sanka" / "scan.json"
    payload = json.loads(scan_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    for route in payload["routes"]:
        route.pop("adaptation_reasons", None)
    hash_payload = dict(payload)
    hash_payload.pop("scan_hash", None)
    payload["scan_hash"] = content_hash(hash_payload)
    scan_path.write_text(json.dumps(payload), encoding="utf-8")

    plan = _run_cli(["plan", str(crud_project), "--to", "fastapi", "--json"], crud_project)
    assert plan.returncode == 0, plan.stderr
    planned = json.loads(plan.stdout)
    manual = [
        route for route in planned["routes"] if route["strategy"] == "needs-manual-adaptation"
    ]
    assert manual
    assert all(
        route["adaptation_reasons"][0]["code"] == "SANKA_DRF_MIDDLEWARE_UNSUPPORTED"
        for route in manual
    )


def test_apply_sqlalchemy_and_rejects_psycopg_on_sqlite(crud_project: Path) -> None:
    scan = _run_cli(["scan", str(crud_project)], crud_project)
    assert scan.returncode == 0, scan.stderr
    plan = _run_cli(
        ["plan", str(crud_project), "--to", "fastapi", "--orm", "sqlalchemy"], crud_project
    )
    assert plan.returncode == 0, plan.stderr
    applied = _run_cli(["apply", "--root", str(crud_project), "--orm", "sqlalchemy"], crud_project)
    assert applied.returncode == 0, applied.stderr
    output = crud_project / ".sanka" / "output" / "fastapi"
    store = (output / "sanka_store.py").read_text(encoding="utf-8")
    assert "sqlalchemy" in store
    assert "Tortoise" not in store
    refused = _run_cli(
        ["apply", "--root", str(crud_project), "--force", "--orm", "psycopg"], crud_project
    )
    assert refused.returncode == 1, refused.stdout
    assert "PostgreSQL" in refused.stderr
