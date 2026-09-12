# SPDX-License-Identifier: AGPL-3.0-only
"""Repair orchestration with fake host ports; no application code is executed."""

from __future__ import annotations

from typing import Any

import pytest

from sanka.runtime.extensions.code_repair import (
    CodeChecks,
    CodeRepairLifecycle,
    CodeRepairRun,
    CodeRepairSnapshot,
)
from sanka.runtime.extensions.model import ExtensionError

HASH = "sha256:" + "a" * 64
CHANGED = "sha256:" + "b" * 64
PATCH = "sha256:" + "c" * 64


def checks(*, passed: bool, tests: int = 3, verify: bool = True) -> CodeChecks:
    return CodeChecks.from_dict(
        {
            "test": {"passed": passed, "tests": tests, "expected_tests": 3, "log": "fixture"},
            "verify": {"passed": verify, "routes": [{"path": "/fixture"}]},
            "context": {"fixture": True},
        }
    )


class Fixture:
    def __init__(self) -> None:
        self.protected = HASH
        self.selected = HASH
        self.events: list[str] = []
        self.before = checks(passed=False)
        self.after = checks(passed=True)
        self.checked = 0
        self.applied = 0
        self.change_at: str | None = None

    def snapshot(self) -> CodeRepairSnapshot:
        return CodeRepairSnapshot(self.protected, self.selected)

    def prepare(self) -> int:
        self.events.append("prepare")
        return 3

    def freeze(self) -> None:
        self.events.append("freeze")

    def check(self) -> CodeChecks:
        self.checked += 1
        phase = "before" if self.checked == 1 else "after"
        self.events.append(phase)
        if self.change_at == phase:
            self.protected = CHANGED
        if self.change_at == phase + "_selected":
            self.selected = PATCH
        return self.before if phase == "before" else self.after

    def apply(self, before: CodeChecks) -> str:
        assert before == self.before
        self.applied += 1
        self.events.append("patch")
        self.selected = CHANGED
        if self.change_at == "patch":
            self.protected = CHANGED
        return PATCH

    def run(self, **overrides: Any) -> CodeRepairRun:
        arguments: dict[str, Any] = {
            "target_gate": "test",
            "snapshot": self.snapshot,
            "prepare": self.prepare,
            "freeze": self.freeze,
            "check": self.check,
            "apply_patch": self.apply,
        }
        return CodeRepairLifecycle().run(**(arguments | overrides))


def test_one_patch_preserves_candidate_and_requires_both_regression_gates() -> None:
    fixture = Fixture()
    result = fixture.run()
    assert result.outcome == "repaired"
    assert fixture.events == ["prepare", "freeze", "before", "patch", "after"]
    assert fixture.applied == 1
    assert result.patch_hash == PATCH
    assert result.evidence is not None
    assert result.evidence.to_dict() == {
        "target_gate": "test",
        "before_passed": False,
        "after_passed": True,
        "regression_passed": True,
        "regression_tests": 3,
        "checks_unchanged": True,
        "edit_scope_respected": True,
    }


def test_already_passing_target_never_requests_a_patch() -> None:
    fixture = Fixture()
    fixture.before = checks(passed=True)
    result = fixture.run()
    assert result.outcome == "already_passed"
    assert fixture.applied == 0 and fixture.checked == 1


@pytest.mark.parametrize("regression", ["test", "verify", "missing_tests", "extra_tests"])
def test_requested_target_passing_does_not_hide_other_regressions(regression: str) -> None:
    fixture = Fixture()
    fixture.after = checks(
        passed=regression != "test",
        verify=regression != "verify",
        tests={"missing_tests": 2, "extra_tests": 4}.get(regression, 3),
    )
    result = fixture.run()
    assert result.outcome == "failed"
    assert result.evidence is not None
    assert not result.evidence.regression_passed
    assert fixture.applied == 1


@pytest.mark.parametrize(
    "change_at", ["before", "before_selected", "patch", "after", "after_selected"]
)
def test_changed_protected_or_selected_inputs_stop_repair(change_at: str) -> None:
    fixture = Fixture()
    fixture.change_at = change_at
    result = fixture.run()
    assert result.outcome == "scope_violation"
    assert fixture.applied == (0 if change_at.startswith("before") else 1)
    if change_at == "patch":
        assert fixture.checked == 1


def test_prepare_cannot_rewrite_selected_application_files() -> None:
    fixture = Fixture()

    def prepare() -> int:
        fixture.selected = CHANGED
        return 3

    assert fixture.run(prepare=prepare).outcome == "scope_violation"
    assert fixture.applied == fixture.checked == 0


def test_verify_repair_still_requires_all_tests() -> None:
    fixture = Fixture()
    fixture.before = checks(passed=True, verify=False)
    result = fixture.run(target_gate="verify")
    assert result.outcome == "repaired"
    assert result.evidence is not None
    assert result.evidence.target_gate == "verify"


@pytest.mark.parametrize("phase", ["prepare", "freeze", "check", "apply_patch"])
def test_host_failure_never_retries(phase: str) -> None:
    fixture = Fixture()
    calls = []

    def fail(*_args: Any) -> Any:
        calls.append(phase)
        raise TimeoutError("host deadline")

    with pytest.raises(TimeoutError):
        fixture.run(**{phase: fail})
    assert calls == [phase]


@pytest.mark.parametrize("invalid", ["target", "count", "hash", "snapshot", "unchanged_patch"])
def test_invalid_host_contract_stops_without_success(invalid: str) -> None:
    fixture = Fixture()
    overrides: dict[str, dict[str, Any]] = {
        "target": {"target_gate": "apply"},
        "count": {"prepare": lambda: True},
        "hash": {"apply_patch": lambda _before: "not-a-sha256"},
        "snapshot": {"snapshot": lambda: CodeRepairSnapshot("bad", HASH)},
        "unchanged_patch": {"apply_patch": lambda _before: PATCH},
    }
    with pytest.raises(ExtensionError):
        fixture.run(**overrides[invalid])


@pytest.mark.parametrize("field,value", [("passed", 1), ("tests", -1), ("expected_tests", False)])
def test_check_contract_rejects_invalid_gate_values(field: str, value: Any) -> None:
    document = checks(passed=False).to_dict()
    document["test"][field] = value
    with pytest.raises(ExtensionError):
        CodeChecks.from_dict(document)


def test_check_observer_gets_independent_copies_and_failure_stops_before_patch() -> None:
    fixture = Fixture()

    def observe(_phase: str, result: CodeChecks) -> None:
        document = result.to_dict()
        document["test"]["passed"] = True
        raise OSError("audit store failed")

    with pytest.raises(OSError):
        fixture.run(on_check=observe)
    assert fixture.applied == 0
    assert not fixture.before.to_dict()["test"]["passed"]
