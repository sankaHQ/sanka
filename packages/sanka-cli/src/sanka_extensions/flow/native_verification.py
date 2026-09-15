# SPDX-License-Identifier: Apache-2.0
"""Versioned native billing checks, with immutable host-owned fixture artifacts.

These are declarations, not an executor or provider adapter. The host admits a
fixture oracle for the exact mapping before planning. Fixture pages contain real
input data and independently specified expected records; they are kept outside
the Blueprint so large native readbacks need not be duplicated in every document.
The runtime must read and hash-check every page and compare every record. A host
success flag, a sample, or an unchecked fixture hash cannot satisfy this contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Self

from sanka_extensions.flow._wire import (
    WireRecord,
    array,
    boolean,
    choice,
    identifier,
    identifiers,
    instances,
    object_fields,
    text,
)
from sanka_extensions.flow.definition import JsonValue
from sanka_extensions.flow.identity import ArtifactIdentity
from sanka_extensions.flow.native import NativeOrderBillingWorkflow

NATIVE_BILLING_VERIFICATION = "flow.native.order-billing-verification/v1"
NATIVE_BILLING_SCENARIO = "sanka-flow-native-billing-scenario/v1"
type BillingCase = Literal[
    "complete",
    "scoped_complete",
    "empty",
    "partial",
    "failed",
    "cancelled",
    "retry",
    "handoff_retry",
    "repeated_schedule",
    "overlap",
    "bulk",
]
NATIVE_BILLING_CASES: tuple[BillingCase, ...] = (
    "complete",
    "scoped_complete",
    "empty",
    "partial",
    "failed",
    "cancelled",
    "retry",
    "handoff_retry",
    "repeated_schedule",
    "overlap",
    "bulk",
)
type ImportStatus = Literal["complete", "partial", "failed", "cancelled"]
type BillingFailure = Literal["none", "after_import", "after_invoice_commit"]


def utc_clock(value: object) -> datetime:
    stamp = text(value, "delivery clock")
    try:
        parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise ValueError("delivery clock must be a canonical UTC timestamp") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != stamp:
        raise ValueError("delivery clock must be a canonical UTC timestamp")
    return parsed


@dataclass(frozen=True, slots=True)
class NativeBillingDelivery(WireRecord):
    """One native attempt; retries share run_id, new schedules do not.

    ``invoice_keys`` is the complete cumulative invoice membership after this
    attempt, including seeded invoices. Concurrent attempts are compared after
    their common group joins; host evidence must additionally prove overlapping
    native execution. A failure point describes bounded harness fault injection,
    never a script/hook to execute.
    """

    id: str
    run_id: str
    at: str
    import_status: ImportStatus
    imported_keys: tuple[str, ...]
    invoice_keys: tuple[str, ...]
    failure: BillingFailure = "none"
    concurrent_group: str | None = None

    def __post_init__(self) -> None:
        identifier(self.id, "delivery id")
        identifier(self.run_id, "delivery run_id")
        utc_clock(self.at)
        choice(self.import_status, ("complete", "partial", "failed", "cancelled"), "import status")
        choice(self.failure, ("none", "after_import", "after_invoice_commit"), "failure point")
        if self.concurrent_group is not None:
            identifier(self.concurrent_group, "concurrent group")
        for name in ("imported_keys", "invoice_keys"):
            keys = identifiers(getattr(self, name), name)
            if len(keys) > 10000:
                raise ValueError("native delivery membership must be bounded to 10000 records")
            object.__setattr__(self, name, keys)
        if self.import_status != "complete" and self.failure != "none":
            raise ValueError("incomplete imports cannot reach a billing failure point")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "at": self.at,
            "import_status": self.import_status,
            "imported_keys": list(self.imported_keys),
            "invoice_keys": list(self.invoice_keys),
            "failure": self.failure,
            "concurrent_group": self.concurrent_group,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(
            value,
            {
                "id",
                "run_id",
                "at",
                "import_status",
                "imported_keys",
                "invoice_keys",
                "failure",
                "concurrent_group",
            },
            "native billing delivery",
        )
        return cls(
            payload["id"],
            payload["run_id"],
            payload["at"],
            payload["import_status"],
            array(payload["imported_keys"], lambda v: identifier(v, "source key"), "imported keys"),
            array(payload["invoice_keys"], lambda v: identifier(v, "source key"), "invoice keys"),
            payload["failure"],
            payload["concurrent_group"],
        )


@dataclass(frozen=True, slots=True)
class NativeBillingScenario(WireRecord):
    """A bounded native check pinned to independently admitted fixture contents.

    The fixture manifest must enumerate exactly record_keys and its page hashes.
    Existing invoice rows must be linked to existing Orders before execution.
    Every scenario has an isolated namespace; keys never identify live records.
    """

    id: str
    workflow_id: str
    case: BillingCase
    fixture: ArtifactIdentity
    mapping: ArtifactIdentity
    configuration_digest: str
    record_keys: tuple[str, ...]
    existing_order_keys: tuple[str, ...]
    existing_invoice_keys: tuple[str, ...]
    deliveries: tuple[NativeBillingDelivery, ...]
    required: bool = True

    def __post_init__(self) -> None:
        identifier(self.id, "scenario id")
        identifier(self.workflow_id, "scenario workflow_id")
        choice(self.case, NATIVE_BILLING_CASES, "native billing case")
        boolean(self.required, "scenario required")
        if type(self.fixture) is not ArtifactIdentity or type(self.mapping) is not ArtifactIdentity:
            raise ValueError("native billing requires immutable fixture and mapping identities")
        ArtifactIdentity("configuration", "1", self.configuration_digest)
        for name in ("record_keys", "existing_order_keys", "existing_invoice_keys"):
            object.__setattr__(self, name, identifiers(getattr(self, name), name))
        keys = set(self.record_keys)
        if not 3 <= len(keys) <= 10000:
            raise ValueError("native fixture must contain three to 10000 records")
        if not set(self.existing_invoice_keys) <= set(self.existing_order_keys) <= keys:
            raise ValueError("seeded invoices require seeded Orders from the exact fixture")
        if (
            not self.existing_invoice_keys
            or not (keys - set(self.existing_order_keys))
            or not (set(self.existing_order_keys) - set(self.existing_invoice_keys))
        ):
            raise ValueError(
                "native checks require billed and unbilled existing Orders and a new Order"
            )
        instances(self.deliveries, NativeBillingDelivery, "native deliveries")
        if not 1 <= len(self.deliveries) <= 8:
            raise ValueError("native scenario requires one to eight deliveries")
        if len({d.id for d in self.deliveries}) != len(self.deliveries):
            raise ValueError("native delivery attempt identities must be unique")
        previous = set(self.existing_invoice_keys)
        for delivery in self.deliveries:
            imported, billed = set(delivery.imported_keys), set(delivery.invoice_keys)
            if not imported <= keys or not billed <= keys or not previous <= billed:
                raise ValueError(
                    "delivery cannot introduce outside records or remove existing invoices"
                )
            eligible = previous | imported
            if delivery.import_status != "complete" or delivery.failure == "after_import":
                if billed != previous:
                    raise ValueError(
                        "incomplete import or interrupted handoff cannot create invoices"
                    )
            elif delivery.failure == "after_invoice_commit":
                if not previous < billed <= eligible:
                    raise ValueError("invoice retry must exercise an actual committed invoice")
            elif billed != eligible:
                raise ValueError(
                    "complete import requires precisely one invoice for each imported Order"
                )
            previous = billed
        self._validate_case()

    def _validate_case(self) -> None:
        first, last = self.deliveries[0], self.deliveries[-1]
        multi = self.case in {"retry", "handoff_retry", "repeated_schedule", "overlap"}
        if len(self.deliveries) != (2 if multi else 1):
            raise ValueError("native case has the wrong number of deliveries")
        if self.case in {"partial", "failed", "cancelled"}:
            if first.import_status != self.case or not first.imported_keys:
                raise ValueError(
                    "incomplete case must exercise its status after some Orders were imported"
                )
            if not (set(first.imported_keys) - set(self.existing_order_keys)) or not (
                set(first.imported_keys)
                & (set(self.existing_order_keys) - set(self.existing_invoice_keys))
            ):
                raise ValueError(
                    "incomplete case must include newly imported and existing unbilled Orders"
                )
        elif any(d.import_status != "complete" for d in self.deliveries):
            raise ValueError("this case requires complete native imports")
        if self.case == "empty":
            if first.imported_keys:
                raise ValueError("empty case cannot import any Orders")
        elif self.case == "scoped_complete":
            if (
                not set(first.imported_keys) < set(self.record_keys)
                or not (set(first.imported_keys) - set(self.existing_order_keys))
                or not (
                    set(self.existing_order_keys)
                    - set(self.existing_invoice_keys)
                    - set(first.imported_keys)
                )
            ):
                raise ValueError(
                    "scoped output must leave an existing unbilled Order outside a nonempty import"
                )
        elif self.case not in {"partial", "failed", "cancelled"} and any(
            set(d.imported_keys) != set(self.record_keys) for d in self.deliveries
        ):
            raise ValueError("this case must exercise the complete declared fixture membership")
        if self.case == "bulk" and len(self.record_keys) < 2001:
            raise ValueError("bulk verification must cross the 2000-record diagnostic boundary")
        if self.case in {"retry", "handoff_retry"}:
            if first.run_id != last.run_id or first.at != last.at:
                raise ValueError("retry must resume the same run and scheduled clock")
            expected_failure = (
                "after_import" if self.case == "handoff_retry" else "after_invoice_commit"
            )
            if first.failure != expected_failure or last.failure != "none":
                raise ValueError("retry must recover an interrupted committed invoice")
        elif any(d.failure != "none" for d in self.deliveries):
            raise ValueError("failure injection belongs to the explicit retry case")
        if self.case in {"repeated_schedule", "overlap"} and first.run_id == last.run_id:
            raise ValueError("new schedules and overlapping execution require distinct runs")
        if self.case == "repeated_schedule" and utc_clock(last.at) <= utc_clock(first.at):
            raise ValueError("repeated schedule must use a later scheduled clock")
        if self.case == "overlap":
            if not first.concurrent_group or first.concurrent_group != last.concurrent_group:
                raise ValueError("overlap must require a common concurrent native execution group")
            if first.at != last.at:
                raise ValueError("overlap requires the same scheduled clock")
        elif any(d.concurrent_group is not None for d in self.deliveries):
            raise ValueError("concurrent groups belong to the explicit overlap case")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": NATIVE_BILLING_SCENARIO,
            "id": self.id,
            "workflow_id": self.workflow_id,
            "case": self.case,
            "fixture": self.fixture.to_dict(),
            "mapping": self.mapping.to_dict(),
            "configuration_digest": self.configuration_digest,
            "record_keys": list(self.record_keys),
            "existing_order_keys": list(self.existing_order_keys),
            "existing_invoice_keys": list(self.existing_invoice_keys),
            "deliveries": [d.to_dict() for d in self.deliveries],
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(
            value,
            {
                "schema_version",
                "id",
                "workflow_id",
                "case",
                "fixture",
                "mapping",
                "configuration_digest",
                "record_keys",
                "existing_order_keys",
                "existing_invoice_keys",
                "deliveries",
                "required",
            },
            "native billing scenario",
        )
        choice(payload["schema_version"], (NATIVE_BILLING_SCENARIO,), "native scenario schema")
        return cls(
            payload["id"],
            payload["workflow_id"],
            payload["case"],
            ArtifactIdentity.from_dict(payload["fixture"]),
            ArtifactIdentity.from_dict(payload["mapping"]),
            payload["configuration_digest"],
            array(payload["record_keys"], lambda v: identifier(v, "source key"), "record keys"),
            array(
                payload["existing_order_keys"],
                lambda v: identifier(v, "source key"),
                "existing Order keys",
            ),
            array(
                payload["existing_invoice_keys"],
                lambda v: identifier(v, "source key"),
                "existing invoice keys",
            ),
            array(payload["deliveries"], NativeBillingDelivery.from_dict, "deliveries"),
            payload["required"],
        )


def validate_billing_scenarios(
    workflow: NativeOrderBillingWorkflow, scenarios: tuple[NativeBillingScenario, ...]
) -> None:
    instances(scenarios, NativeBillingScenario, "native billing scenarios")
    if len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("native scenario identities must be unique")
    if len(scenarios) != len(NATIVE_BILLING_CASES) or {
        s.case for s in scenarios if s.required
    } != set(NATIVE_BILLING_CASES):
        raise ValueError("native billing coverage requires every case exactly once")
    for scenario in scenarios:
        if scenario.workflow_id != workflow.id:
            raise ValueError("native scenario must reference its workflow")
        if (
            scenario.configuration_digest != workflow.configuration_digest
            or scenario.mapping != workflow.mapping
        ):
            raise ValueError("native fixture configuration or mapping differs from the workflow")
        if scenario.case == "repeated_schedule":
            first, last = scenario.deliveries
            if (
                utc_clock(last.at) - utc_clock(first.at)
            ).total_seconds() != workflow.interval_minutes * 60:
                raise ValueError("repeated schedule must exercise the configured interval")
