# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pathlib import Path

from sanka.cli._summary import application_summary


def test_scan_summary_lists_versions_counts_and_hash() -> None:
    lines, hint = application_summary(
        "scan",
        {
            "python_version": "3.12.13",
            "django_version": "5.2.17",
            "drf_version": "3.18.1",
            "database": {"vendor": "sqlite", "name": "db.sqlite3"},
            "routes": [{}] * 14,
            "serializers": ["orders.serializers.OrderSerializer"],
            "models": ["orders.models.Order"],
            "permissions": ["rest_framework.permissions.AllowAny"],
            "test_files": 0,
            "skipped_routes": [],
            "scan_hash": "sha256:" + "a" * 64,
            "extensions": [{"id": "sanka/drf-to-fastapi", "targets": ["fastapi"]}],
        },
    )
    assert lines == [
        "Detected",
        "  Python     3.12.13",
        "  Django     5.2.17",
        "  DRF        3.18.1",
        "  Database   sqlite (db.sqlite3)",
        "",
        "Application",
        "  14 routes",
        "  1 serializer",
        "  1 model",
        "  1 permission class",
        "  0 test files",
        "",
        "scan hash: sha256:" + "a" * 64,
    ]
    assert hint == "sanka plan . --to fastapi"


def test_scan_summary_without_a_single_target_keeps_a_placeholder() -> None:
    lines, hint = application_summary("scan", {"routes": [], "scan_hash": "sha256:" + "b" * 64})
    assert lines == ["Application", "  0 routes", "", "scan hash: sha256:" + "b" * 64]
    assert hint == "sanka plan . --to <target>"


def test_plan_summary_shows_strategy_route_split_and_apply_hint(tmp_path: Path) -> None:
    plan_hash = "sha256:" + "c" * 64
    lines, hint = application_summary(
        "plan",
        {
            "source_framework": "django-rest-framework",
            "target_framework": "fastapi",
            "mode": "native",
            "sql_engine": "tortoise",
            "generation_mode": "minimal",
            "package_manager": "uv",
            "default_output": str(tmp_path / ".sanka" / "output" / "fastapi"),
            "routes": [{}] * 14,
            "native_routes": 7,
            "dropped_alias_routes": 7,
            "needs_adaptation_routes": 0,
            "native_eligible_routes": 7,
            "readiness": 1.0,
            "plan_hash": plan_hash,
        },
        root=tmp_path,
    )
    assert lines == [
        "DRF → FastAPI plan",
        "  Strategy   native · Tortoise ORM · minimal generation · uv",
        "  Output     .sanka/output/fastapi",
        "",
        "Routes (14)",
        "  7 generated natively",
        "  7 format-suffix aliases dropped (disclosed)",
        "  0 need manual adaptation",
        "  readiness 100% (7/7 non-alias routes)",
    ]
    assert hint == f"sanka apply --root . --plan-hash {plan_hash}"


def test_apply_test_and_verify_summaries(tmp_path: Path) -> None:
    output = tmp_path / ".sanka" / "output" / "fastapi"
    apply_lines, apply_hint = application_summary(
        "apply",
        {"routes_generated": 7, "output": str(output), "sql_engine": "tortoise", "mode": "native"},
        root=tmp_path,
    )
    assert apply_lines == [
        "Generated",
        "  7 native routes",
        "  Output     .sanka/output/fastapi",
        "  Engine     Tortoise ORM",
    ]
    assert apply_hint == "sanka test ."

    test_lines, test_hint = application_summary(
        "test",
        {"log": "...\nRan 7 tests in 0.5s\n\nOK\n", "environment": str(output / ".venv")},
        root=tmp_path,
    )
    assert test_lines == [
        "Generated app tests",
        "  Ran 7 tests",
        "  Env        .sanka/output/fastapi/.venv",
    ]
    assert test_hint == "sanka verify ."

    verify_lines, verify_hint = application_summary(
        "verify",
        {
            "mode": "native",
            "routes": {
                "planned": 14,
                "generated": 7,
                "dropped": ["GET /api/orders.{format}/"] * 7,
                "needs_adaptation": [],
                "missing": [],
                "extra": [],
            },
            "http": {"enabled": True, "probed": 2, "passed": 2, "failed": []},
        },
    )
    assert verify_lines == [
        "Verified (native)",
        "  routes   7 generated · 7 aliases dropped · 0 need adaptation · 0 missing · 0 extra",
        "  http     2/2 probes passed",
    ]
    assert verify_hint is None


def test_summary_tolerates_unknown_shapes() -> None:
    assert application_summary("verify", {}) == ([], None)
    assert application_summary("apply", {"unexpected": True}) == ([], "sanka test .")
    assert application_summary("status", {"anything": 1}) == ([], None)
    assert application_summary("scan", "not a mapping") == ([], None)  # type: ignore[arg-type]


def test_scan_hint_derives_the_target_from_the_extension_id() -> None:
    _lines, hint = application_summary(
        "scan",
        {
            "routes": [],
            "extensions": [{"extension": {"id": "sanka/drf-to-fastapi", "version": "0.1.0a10"}}],
        },
    )
    assert hint == "sanka plan . --to fastapi"

    _lines, ambiguous = application_summary(
        "scan",
        {
            "routes": [],
            "extensions": [
                {"extension": {"id": "sanka/drf-to-fastapi"}},
                {"extension": {"id": "sanka/drf-to-flask"}},
            ],
        },
    )
    assert ambiguous == "sanka plan . --to <target>"
