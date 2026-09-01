# SPDX-License-Identifier: Apache-2.0
"""Release artifacts keep framework runtimes inside extensions."""

from scripts.check_release_artifacts import _runtime_boundary_errors


def test_runtime_boundary_rejects_target_dependencies_and_framework_members() -> None:
    errors = _runtime_boundary_errors(
        {"pyyaml", "sanka-connector-sdk", "fastapi", "asyncpg"},
        {
            "sanka/runtime/__init__.py",
            "sanka/runtime/frameworks/django_fastapi.py",
        },
    )

    assert errors == [
        "sanka-migrate: target dependency asyncpg leaked into core",
        "sanka-migrate: target dependency fastapi leaked into core",
        "sanka-migrate: wheel must not ship sanka/runtime/frameworks/",
    ]
