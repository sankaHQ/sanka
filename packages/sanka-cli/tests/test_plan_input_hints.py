# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from sanka.cli import (
    PLAN_GENERATIONS,
    PLAN_ORMS,
    PLAN_PACKAGE_MANAGERS,
    PLAN_STRATEGIES,
    plan_input_hints,
)


def test_choice_inputs_offer_the_recommended_value_first() -> None:
    hints = plan_input_hints("fastapi")
    assert hints("strategy", "fastapi") == (PLAN_STRATEGIES, "native")
    assert hints("generation", "fastapi") == (PLAN_GENERATIONS, "minimal")
    assert hints("package_manager", "fastapi") == (PLAN_PACKAGE_MANAGERS, "uv")
    assert hints("orm", "fastapi") == (PLAN_ORMS, "tortoise")


def test_output_default_follows_the_selected_target() -> None:
    assert plan_input_hints(None)("output", "fastapi") == (None, ".sanka/output/fastapi")
    assert plan_input_hints("flask")("output", None) == (None, ".sanka/output/flask")
    assert plan_input_hints(None)("output", None) == (None, ".sanka/output/app")


def test_unknown_inputs_get_no_hint() -> None:
    assert plan_input_hints("fastapi")("settings_module", "fastapi") == (None, None)
