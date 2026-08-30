# SPDX-License-Identifier: Apache-2.0
"""Regression floors for the readiness-aware Sanka Bench gate."""

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
_readiness_envelope_error = cast(
    Callable[[str, int, int], str | None],
    runpy.run_path(str(ROOT / "scripts" / "run_bench.py"))["_readiness_envelope_error"],
)


def test_readiness_envelope_accepts_reviewed_floor_and_improvement() -> None:
    assert _readiness_envelope_error("drf-fastapi-008", 1, 29) is None
    assert _readiness_envelope_error("drf-fastapi-008", 7, 29) is None


def test_readiness_envelope_rejects_native_regression() -> None:
    assert _readiness_envelope_error("drf-fastapi-004", 6, 13) == (
        "drf-fastapi-004: native readiness regressed from at least 7/13 to 6/13"
    )


def test_readiness_envelope_rejects_fixture_drift() -> None:
    assert _readiness_envelope_error("drf-fastapi-008", 1, 30) == (
        "drf-fastapi-008: eligible route count changed from 29 to 30; "
        "review the fixture and readiness envelope"
    )


def test_readiness_envelope_requires_new_tasks_to_be_reviewed() -> None:
    assert _readiness_envelope_error("drf-fastapi-011", 0, 10) == (
        "drf-fastapi-011: missing reviewed readiness envelope; update EXPECTED_ROUTE_ENVELOPE"
    )
