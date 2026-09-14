# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.check_extension_compatibility import BASELINE_VERSION, validate_report


def evidence() -> dict[str, Any]:
    return {
        "status": "passed",
        "candidate": {"sha256": "a" * 64, "version": "0.2.13"},
        "lock_sha256_before": "b" * 64,
        "lock_sha256_after": "b" * 64,
        "upgrade_from": BASELINE_VERSION,
        "installed_cli": "0.2.13",
    }


def test_acceptance_requires_exact_candidate_and_preserved_lock() -> None:
    original = evidence()
    validate_report(original, "a" * 64)
    mutations = [
        {"status": "failed", "stage": "candidate scan", "error": "protocol mismatch"},
        {"candidate": {"sha256": "c" * 64, "version": "0.2.13"}},
        {"lock_sha256_after": "c" * 64},
        {"lock_sha256_before": None, "lock_sha256_after": None},
        {"upgrade_from": "0.0.1"},
        {"installed_cli": "0.2.12"},
    ]
    for changed in mutations:
        report = copy.deepcopy(original) | changed
        with pytest.raises(ValueError):
            validate_report(report, "a" * 64)


def test_publication_requires_both_platforms_and_the_same_staged_wheel() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/publish.yml").read_text())
    gate = workflow["jobs"]["extension-compatibility"]
    assert "extension-compatibility" in workflow["jobs"]["publish"]["needs"]
    assert gate["needs"] == "build"
    assert set(gate["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "macos-latest"}
    assert "if" not in gate and not gate.get("continue-on-error")
    steps = gate["steps"]
    downloads = [step for step in steps if "actions/download-artifact@" in step.get("uses", "")]
    assert downloads[0]["with"]["name"] == "sanka-cli-${{ github.ref_name }}"
    runs = [step.get("run", "") for step in steps]
    assert any("SOURCE_COMMIT" in run and "--check SHA256SUMS" in run for run in runs)
    acceptance = next(step for step in steps if "make extension-acceptance" in step.get("run", ""))
    assert "release/sanka_cli-*-py3-none-any.whl" in acceptance["run"]
    assert "if" not in acceptance and not acceptance.get("continue-on-error")
