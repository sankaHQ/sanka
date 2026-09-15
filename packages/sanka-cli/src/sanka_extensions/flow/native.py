# SPDX-License-Identifier: Apache-2.0
"""Typed native order-import/billing profile; declarations never execute providers.

This first native profile supports an interval trigger, a complete order import,
and creation of draft invoices from precisely that import's output. It is not an
escape hatch for arbitrary native action payloads. Hosts must resolve the endpoint
and immutable mapping in the pinned workspace before planning or construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Self
from uuid import UUID

from sanka_extensions.flow._wire import (
    WireRecord,
    array,
    artifact_digest,
    identifier,
    object_fields,
    text,
)
from sanka_extensions.flow.definition import JsonValue
from sanka_extensions.flow.identity import ArtifactIdentity

NATIVE_ORDER_BILLING_SCHEMA = "sanka-flow-native-order-billing/v1"


def _integer(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class NativeOrderBillingWorkflow(WireRecord):
    """One explicit native capability composition with retry-preserving policies.

    The endpoint and mapping IDs are selectors, never authentication material.
    Mapping digest/revision must come from independently observed hosted state.
    No invoice step may run after failed, cancelled or partial import completion.
    Existing invoices and user edits survive repeated scheduling and retries.
    """

    id: str
    interval_minutes: int
    source_endpoint_id: str
    mapping: ArtifactIdentity
    pipeline_id: str
    deal_stage_ids: tuple[str, ...]
    updated_since: str
    invoice_due_days: int

    def __post_init__(self) -> None:
        identifier(self.id, "workflow id")
        _integer(self.interval_minutes, 1, 1440, "interval_minutes")
        endpoint = text(self.source_endpoint_id, "source_endpoint_id")
        if str(UUID(endpoint)) != endpoint:
            raise ValueError("source_endpoint_id must be a canonical UUID")
        if type(self.mapping) is not ArtifactIdentity:
            raise ValueError("mapping requires an immutable artifact identity")
        identifier(self.pipeline_id, "pipeline_id")
        if type(self.deal_stage_ids) is not tuple or not 1 <= len(self.deal_stage_ids) <= 100:
            raise ValueError("deal_stage_ids requires one to 100 exact stage IDs")
        for stage in self.deal_stage_ids:
            identifier(stage, "deal stage")
        if len(set(self.deal_stage_ids)) != len(self.deal_stage_ids):
            raise ValueError("deal_stage_ids must be unique")
        object.__setattr__(self, "deal_stage_ids", tuple(sorted(self.deal_stage_ids)))
        if (
            date.fromisoformat(text(self.updated_since, "updated_since")).isoformat()
            != self.updated_since
        ):
            raise ValueError("updated_since must use YYYY-MM-DD in UTC")
        _integer(self.invoice_due_days, 0, 365, "invoice_due_days")

    @property
    def configuration(self) -> dict[str, JsonValue]:
        """Exact executable selections; excludes generated logical identities."""
        return {
            "interval_minutes": self.interval_minutes,
            "source_endpoint_id": self.source_endpoint_id,
            "mapping": self.mapping.to_dict(),
            "pipeline_id": self.pipeline_id,
            "deal_stage_ids": list(self.deal_stage_ids),
            "updated_since": self.updated_since,
            "invoice_due_days": self.invoice_due_days,
        }

    @classmethod
    def from_configuration(cls, id: str, value: object) -> Self:
        payload = object_fields(
            value,
            {
                "interval_minutes",
                "source_endpoint_id",
                "mapping",
                "pipeline_id",
                "deal_stage_ids",
                "updated_since",
                "invoice_due_days",
            },
            "native configuration",
        )
        return cls(
            id,
            payload["interval_minutes"],
            payload["source_endpoint_id"],
            ArtifactIdentity.from_dict(payload["mapping"]),
            payload["pipeline_id"],
            array(payload["deal_stage_ids"], lambda v: identifier(v, "deal stage"), "deal stages"),
            payload["updated_since"],
            payload["invoice_due_days"],
        )

    @property
    def configuration_digest(self) -> str:
        return artifact_digest(self.configuration)

    @property
    def node_ids(self) -> tuple[str, str, str]:
        return (f"{self.id}/schedule", f"{self.id}/import-orders", f"{self.id}/draft-invoices")

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return (
            NATIVE_ORDER_BILLING_SCHEMA,
            "flow.native.complete-import-output/v1",
            "flow.native.draft-invoices-from-orders/v1",
            "flow.native.hubspot-deals-to-orders/v1",
            "flow.native.interval-minutes/v1",
            "flow.native.preserve-existing-invoices/v1",
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": NATIVE_ORDER_BILLING_SCHEMA,
            "id": self.id,
            "interval_minutes": self.interval_minutes,
            "source_endpoint_id": self.source_endpoint_id,
            "mapping": self.mapping.to_dict(),
            "pipeline_id": self.pipeline_id,
            "deal_stage_ids": list(self.deal_stage_ids),
            "updated_since": self.updated_since,
            "invoice_due_days": self.invoice_due_days,
            "node_ids": list(self.node_ids),
            "policies": {
                "import_target": "order",
                "import_completion": "complete_only",
                "invoice_source": "imported_orders_only",
                "invoice_status": "draft",
                "existing_invoice": "preserve",
                "invoice_grouping": "one_per_order",
                "construction": "inactive",
            },
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(
            value,
            {
                "schema_version",
                "id",
                "interval_minutes",
                "source_endpoint_id",
                "mapping",
                "pipeline_id",
                "deal_stage_ids",
                "updated_since",
                "invoice_due_days",
                "node_ids",
                "policies",
            },
            "native order billing workflow",
        )
        if payload["schema_version"] != NATIVE_ORDER_BILLING_SCHEMA:
            raise ValueError("unsupported native workflow schema_version")
        result = cls(
            payload["id"],
            payload["interval_minutes"],
            payload["source_endpoint_id"],
            ArtifactIdentity.from_dict(payload["mapping"]),
            payload["pipeline_id"],
            array(payload["deal_stage_ids"], lambda v: identifier(v, "deal stage"), "deal stages"),
            payload["updated_since"],
            payload["invoice_due_days"],
        )
        if (
            payload["node_ids"] != list(result.node_ids)
            or payload["policies"] != result.to_dict()["policies"]
        ):
            raise ValueError("native workflow ordering and safety policies cannot be changed")
        return result
