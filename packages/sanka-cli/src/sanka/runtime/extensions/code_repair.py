# SPDX-License-Identifier: AGPL-3.0-only
"""One bounded Code repair attempt, independent of model and execution hosts.

Hosts authorize and verify the candidate, prepare/freeze its test harness, hash
the current protected/selected files, execute checks and apply an approved patch.
This lifecycle never regenerates application output or issues a certificate.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

from sanka.runtime.extensions.code_lifecycle import _digest, _encode, _error
from sanka.runtime.extensions.stage import _json_value

type Gate = Literal["test", "verify"]
type Outcome = Literal["repaired", "failed", "already_passed", "scope_violation"]
MAX_TESTS = 1_000_000
MAX_CHECK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CodeChecks:
    """Validated, immutable host check evidence; context remains host-defined JSON."""

    document: bytes

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CodeChecks:
        data = _json_value(dict(value), "checks")
        if set(data) - {"test", "verify", "context"} or not {"test", "verify"} <= data.keys():
            _error("SANKA_EXTENSION_PROTOCOL", "Invalid Code repair check fields")
        test, verify = data["test"], data["verify"]
        if not isinstance(test, dict) or not isinstance(verify, dict):
            _error("SANKA_EXTENSION_PROTOCOL", "Code repair gates must be objects")
        if type(test.get("passed")) is not bool or type(verify.get("passed")) is not bool:
            _error("SANKA_EXTENSION_PROTOCOL", "Code repair gates require boolean results")
        for name, minimum in (("tests", 0), ("expected_tests", 1)):
            count = test.get(name)
            if type(count) is not int or not minimum <= count <= MAX_TESTS:
                _error("SANKA_EXTENSION_PROTOCOL", "Invalid Code repair test count", field=name)
        document = _encode(data)
        if len(document) > MAX_CHECK_BYTES:
            _error("SANKA_EXTENSION_PROTOCOL", "Code repair check evidence exceeds its limit")
        return cls(document)

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.document))

    @property
    def tests(self) -> int:
        return cast(int, self.to_dict()["test"]["tests"])

    @property
    def expected_tests(self) -> int:
        return cast(int, self.to_dict()["test"]["expected_tests"])

    def passed(self, gate: Gate, expected: int) -> bool:
        data = self.to_dict()
        if gate == "verify":
            return cast(bool, data["verify"]["passed"])
        return bool(data["test"]["passed"] and self.tests == self.expected_tests == expected)


@dataclass(frozen=True)
class CodeRepairSnapshot:
    """Live content digests; protection includes all inputs outside the edit scope."""

    protected_hash: str
    selected_hash: str

    def __post_init__(self) -> None:
        _digest(self.protected_hash, "protected snapshot")
        _digest(self.selected_hash, "selected snapshot")


@dataclass(frozen=True)
class CodeRepairEvidence:
    target_gate: Gate
    before_passed: bool
    after_passed: bool
    regression_passed: bool
    regression_tests: int
    checks_unchanged: bool
    edit_scope_respected: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CodeRepairRun:
    outcome: Outcome
    before: CodeChecks | None = None
    after: CodeChecks | None = None
    patch_hash: str | None = None
    evidence: CodeRepairEvidence | None = None


class CodeRepairLifecycle:
    def run(
        self,
        *,
        target_gate: Gate,
        snapshot: Callable[[], CodeRepairSnapshot],
        prepare: Callable[[], int],
        freeze: Callable[[], None],
        check: Callable[[], CodeChecks],
        apply_patch: Callable[[CodeChecks], str],
        on_check: Callable[[Literal["before", "after"], CodeChecks], None] | None = None,
    ) -> CodeRepairRun:
        """Revalidate one exact candidate, apply at most one patch, then check again.

        The host must pin authorization, extension/image/plan identity and source;
        enforce deadlines/cancellation; freeze protected files against candidate
        processes; and validate exact allowed file hashes before writing a patch.
        Snapshots must read current content, never a constant request identity.
        Model selection, patch bytes, claims, billing and receipts remain host-owned.
        A host/observer error propagates without a retry or another patch request.
        """
        if target_gate not in {"test", "verify"}:
            _error("SANKA_EXTENSION_PROTOCOL", "Unsupported Code repair target")
        initial = snapshot()
        expected = prepare()
        if type(expected) is not int or not 1 <= expected <= MAX_TESTS:
            _error("SANKA_EXTENSION_PROTOCOL", "Invalid prepared Code repair test count")
        freeze()
        frozen = snapshot()
        if initial.selected_hash != frozen.selected_hash:
            return CodeRepairRun("scope_violation")

        def checked(phase: Literal["before", "after"]) -> CodeChecks:
            # Revalidate even explicitly constructed records at the host boundary.
            result = CodeChecks.from_dict(check().to_dict())
            if on_check is not None:
                on_check(phase, result)
            return result

        before = checked("before")
        if snapshot() != frozen or before.expected_tests != expected:
            return CodeRepairRun("scope_violation", before=before)
        if before.passed(target_gate, expected):
            return CodeRepairRun("already_passed", before=before)

        patch_hash = _digest(apply_patch(before), "repair patch")
        patched = snapshot()
        if patched.protected_hash != frozen.protected_hash:
            return CodeRepairRun("scope_violation", before=before, patch_hash=patch_hash)
        if patched.selected_hash == frozen.selected_hash:
            _error("SANKA_EXTENSION_PROTOCOL", "Code repair did not change its selected files")

        after = checked("after")
        final = snapshot()
        unchanged = final.protected_hash == frozen.protected_hash
        edits_match = final.selected_hash == patched.selected_hash
        regression = after.passed("test", expected) and after.passed("verify", expected)
        evidence = CodeRepairEvidence(
            target_gate=target_gate,
            before_passed=False,
            after_passed=after.passed(target_gate, expected),
            regression_passed=regression,
            regression_tests=after.tests,
            checks_unchanged=unchanged,
            edit_scope_respected=edits_match,
        )
        outcome: Outcome = (
            "scope_violation"
            if not unchanged or not edits_match
            else "repaired"
            if regression
            else "failed"
        )
        return CodeRepairRun(outcome, before, after, patch_hash, evidence)


__all__ = [
    "CodeChecks",
    "CodeRepairEvidence",
    "CodeRepairLifecycle",
    "CodeRepairRun",
    "CodeRepairSnapshot",
]
