# SPDX-License-Identifier: AGPL-3.0-only
"""Native generation for nested writes, carried-over create logic, and the
decimal/choice/unique validation surface. All CLI calls run in subprocesses."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PROBE = Path(__file__).parent / "nested_parity_probe.py"

SCENARIOS: list[dict[str, Any]] = [
    {"method": "GET", "path": "/api/listings/"},
    {"method": "GET", "path": "/api/listings/1/"},
    {
        "method": "POST",
        "path": "/api/listings/",
        "body": {
            "code": "LST-2",
            "state": "active",
            "entries": [
                {"sku": "SKU-C", "quantity": 3, "price": "4.50"},
                {"sku": "SKU-D", "quantity": 1, "price": "0.99"},
            ],
        },
    },
    {"method": "POST", "path": "/api/listings/", "body": {"code": "LST-3", "entries": []}},
    {
        "method": "POST",
        "path": "/api/listings/",
        "body": {
            "code": "LST-4",
            "entries": [
                {"sku": "SKU-C", "quantity": 30, "price": "1.00"},
                {"sku": "SKU-D", "quantity": 30, "price": "1.00"},
            ],
        },
    },
    {
        "method": "POST",
        "path": "/api/listings/",
        "body": {"code": "LST-1", "entries": [{"sku": "S", "quantity": 1, "price": "1.00"}]},
    },
    {
        "method": "POST",
        "path": "/api/listings/",
        "body": {
            "code": "LST-5",
            "entries": [
                {"sku": "SKU-C", "quantity": 1, "price": "1.00"},
                {"sku": "SKU-D", "quantity": 0, "price": "1.00"},
            ],
        },
    },
    {"method": "POST", "path": "/api/listings/", "body": {"code": "LST-6"}},
    {
        "method": "POST",
        "path": "/api/listings/",
        "body": {"code": "LST-7", "entries": {"sku": "X"}},
    },
    {
        "method": "POST",
        "path": "/api/listings/",
        "body": {"code": "LST-8", "entries": [{"sku": "S", "quantity": 1, "price": "12345.00"}]},
    },
    {
        "method": "POST",
        "path": "/api/listings/",
        "body": {"code": "LST-9", "entries": [{"sku": "S", "quantity": 1, "price": "1.999"}]},
    },
    {
        "method": "POST",
        "path": "/api/listings/",
        "body": {"code": "LST-10", "entries": [{"sku": "S", "quantity": 1, "price": "abc"}]},
    },
    {
        "method": "POST",
        "path": "/api/listings/",
        "body": {
            "code": "LST-11",
            "state": "archived",
            "entries": [{"sku": "S", "quantity": 1, "price": "1.00"}],
        },
    },
    {"method": "PATCH", "path": "/api/listings/1/", "body": {"note": "updated", "state": "active"}},
    {
        "method": "PATCH",
        "path": "/api/listings/1/",
        "body": {"entries": [{"sku": "S", "quantity": 0, "price": "1.00"}]},
    },
    {"method": "GET", "path": "/api/listings/999/"},
    {"method": "GET", "path": "/api/listings/abc/"},
    {"method": "DELETE", "path": "/api/listings/1/"},
    {"method": "GET", "path": "/api/listings/"},
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
def nested_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "drf_nested_project", project)
    return project


def _generate(project: Path) -> Path:
    scan = _run_cli(["scan", str(project)], project)
    assert scan.returncode == 0, scan.stderr
    plan = _run_cli(["plan", str(project), "--to", "fastapi"], project)
    assert plan.returncode == 0, plan.stderr
    assert "Native migration readiness: 100%" in plan.stdout
    applied = _run_cli(["apply", "--root", str(project)], project)
    assert applied.returncode == 0, applied.stderr
    return project / ".sanka" / "output" / "fastapi"


def test_nested_fixture_generates_carryover_output(nested_project: Path) -> None:
    output = _generate(nested_project)
    manifest = json.loads((output / "sanka-manifest.json").read_text(encoding="utf-8"))
    assert manifest["has_user_logic"] is True
    resource = manifest["resources"][0]
    assert resource["create"]["style"] == "carryover"
    assert resource["update_drops"] == ["entries"]
    entries_field = next(f for f in resource["fields"] if f["name"] == "entries")
    assert entries_field["kind"] == "nested_many"
    assert entries_field["child"]["model_class"] == "ListingItem"
    code_field = next(f for f in resource["fields"] if f["name"] == "code")
    assert code_field["unique"] is True
    assert "already exists" in code_field["unique_message"]
    user_logic = (output / "sanka_user_logic.py").read_text(encoding="utf-8")
    assert "Listing exceeds 50 total units." in user_logic
    assert "transaction.atomic" in user_logic
    assert "rest_framework" not in user_logic
    runtime_text = (output / "sanka_native.py").read_text(encoding="utf-8")
    assert "rest_framework" not in runtime_text


def test_nested_native_output_matches_drf(nested_project: Path, tmp_path: Path) -> None:
    output = _generate(nested_project)
    source = _run_probe("source", nested_project, tmp_path / "source.sqlite3")
    native = _run_probe("native", nested_project, tmp_path / "native.sqlite3", output=output)
    for index, (left, right) in enumerate(zip(source["results"], native["results"], strict=True)):
        assert left == right, f"scenario {index} ({SCENARIOS[index]}): {left} != {right}"
    assert source["database"] == native["database"]


def _plan_strategies(project: Path) -> set[str]:
    scan = _run_cli(["scan", str(project)], project)
    assert scan.returncode == 0, scan.stderr
    plan = _run_cli(["plan", str(project), "--to", "fastapi", "--json"], project)
    assert plan.returncode == 0, plan.stderr
    payload = json.loads(plan.stdout)
    return {
        route["strategy"]
        for route in payload["routes"]
        if "listings" in route["path"] and "{format}" not in route["path"]
    }


def test_create_with_unknown_helper_is_rejected(nested_project: Path) -> None:
    serializers = nested_project / "listings" / "serializers.py"
    text = serializers.read_text(encoding="utf-8")
    text = text.replace(
        "total = sum(entry.quantity for entry in listing.entries.all())",
        "total = compute_total(listing)",
    )
    serializers.write_text(text, encoding="utf-8")
    assert _plan_strategies(nested_project) == {"needs-manual-adaptation"}


def test_create_using_self_is_rejected(nested_project: Path) -> None:
    serializers = nested_project / "listings" / "serializers.py"
    text = serializers.read_text(encoding="utf-8")
    text = text.replace(
        "listing = Listing.objects.create(**validated_data)",
        "listing = Listing.objects.create(**self.initial_data)",
    )
    serializers.write_text(text, encoding="utf-8")
    assert _plan_strategies(nested_project) == {"needs-manual-adaptation"}


def test_non_idiom_update_is_rejected(nested_project: Path) -> None:
    serializers = nested_project / "listings" / "serializers.py"
    text = serializers.read_text(encoding="utf-8")
    text = text.replace(
        "return super().update(instance, validated_data)",
        "instance.refresh_from_db()\n        return super().update(instance, validated_data)",
    )
    serializers.write_text(text, encoding="utf-8")
    assert _plan_strategies(nested_project) == {"needs-manual-adaptation"}
