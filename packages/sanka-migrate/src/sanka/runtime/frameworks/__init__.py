# SPDX-License-Identifier: AGPL-3.0-only
"""Framework migration recipes exposed by the Sanka CLI."""

from sanka.runtime.frameworks.django_fastapi import (
    COMPATIBILITY_STRATEGY,
    DEFAULT_ARTIFACT_DIR,
    DEFAULT_FASTAPI_OUTPUT,
    NATIVE_STRATEGY,
    FrameworkMigrationError,
    apply_fastapi_plan,
    load_fastapi_plan,
    load_framework_scan,
    plan_fastapi,
    scan_django,
    verify_fastapi_migration,
    write_bench_candidate,
)
from sanka.runtime.frameworks.fastapi_tests import test_fastapi_app

__all__ = [
    "COMPATIBILITY_STRATEGY",
    "DEFAULT_ARTIFACT_DIR",
    "DEFAULT_FASTAPI_OUTPUT",
    "NATIVE_STRATEGY",
    "FrameworkMigrationError",
    "apply_fastapi_plan",
    "load_fastapi_plan",
    "load_framework_scan",
    "plan_fastapi",
    "scan_django",
    "test_fastapi_app",
    "verify_fastapi_migration",
    "write_bench_candidate",
]
