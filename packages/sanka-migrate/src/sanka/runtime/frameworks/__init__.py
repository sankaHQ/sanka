# SPDX-License-Identifier: AGPL-3.0-only
"""Framework migration recipes exposed by the Sanka CLI."""

from sanka.runtime.frameworks.django_fastapi import (
    DEFAULT_ARTIFACT_DIR,
    DEFAULT_FASTAPI_OUTPUT,
    FrameworkMigrationError,
    apply_fastapi_plan,
    load_fastapi_plan,
    load_framework_scan,
    plan_fastapi,
    scan_django,
    verify_fastapi_migration,
)

__all__ = [
    "DEFAULT_ARTIFACT_DIR",
    "DEFAULT_FASTAPI_OUTPUT",
    "FrameworkMigrationError",
    "apply_fastapi_plan",
    "load_fastapi_plan",
    "load_framework_scan",
    "plan_fastapi",
    "scan_django",
    "verify_fastapi_migration",
]
