# SPDX-License-Identifier: Apache-2.0
"""Closed native recipe families; no handlers, providers or executable payloads.

Each discriminator selects fixed business semantics and exact setting fields.
The host implements those semantics through its existing native actions. These
declarations alone never prove execution, duplicate prevention or activation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Self

from sanka_extensions.flow._wire import (
    FrozenJson,
    WireRecord,
    array,
    artifact_digest,
    boolean,
    choice,
    identifier,
    object_fields,
    text,
)
from sanka_extensions.flow.definition import JsonValue
from sanka_extensions.flow.native import _integer

NATIVE_CONVERSION_SCHEMA = "sanka-flow-native-record-conversion/v1"
NATIVE_APPROVAL_SCHEMA = "sanka-flow-native-source-approval/v1"
Conversion = Literal[
    "deal_to_estimate",
    "won_deal_to_order",
    "order_to_invoice",
    "purchase_order_to_bill",
    "order_to_stock_out",
]
ApprovalSubject = Literal["purchase_order", "expense", "absence"]

_CONVERSIONS = (
    "deal_to_estimate",
    "won_deal_to_order",
    "order_to_invoice",
    "purchase_order_to_bill",
    "order_to_stock_out",
)
_SUBJECTS = ("purchase_order", "expense", "absence")
_SETTINGS = {
    "deal_to_estimate": set(),
    "won_deal_to_order": {"won_stage"},
    "order_to_invoice": {"invoice_notes"},
    "purchase_order_to_bill": {"use_po_date", "bill_due_days", "bill_notes"},
    "order_to_stock_out": {
        "rotate_inventory",
        "subtract_from_components",
        "require_all_components_in_stock",
    },
}
_ROLES = {
    "deal_to_estimate": "draft-estimate",
    "won_deal_to_order": "create-order",
    "order_to_invoice": "draft-invoice",
    "purchase_order_to_bill": "draft-bill",
    "order_to_stock_out": "stock-out",
}


def _notes(value: object, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if type(value) is not str or len(value) > 2000:
        raise ValueError(f"{label} must be text of at most 2000 characters")


@dataclass(frozen=True, slots=True, init=False)
class NativeRecordConversionWorkflow(WireRecord):
    id: str
    conversion: Conversion
    _settings: FrozenJson = field(repr=False)

    def __init__(self, id: str, conversion: Conversion, settings: dict[str, JsonValue]) -> None:
        identifier(id, "workflow id")
        choice(conversion, _CONVERSIONS, "record conversion")
        values = object_fields(settings, _SETTINGS[conversion], "conversion settings")
        if conversion == "won_deal_to_order":
            _notes(values["won_stage"], "won_stage")
            if not values["won_stage"].strip():
                raise ValueError("won_stage must select a nonblank exact stage")
        elif conversion == "order_to_invoice":
            _notes(values["invoice_notes"], "invoice_notes", nullable=True)
        elif conversion == "purchase_order_to_bill":
            boolean(values["use_po_date"], "use_po_date")
            _integer(values["bill_due_days"], 0, 365, "bill_due_days")
            _notes(values["bill_notes"], "bill_notes")
        elif conversion == "order_to_stock_out":
            for key, value in values.items():
                boolean(value, key)
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "conversion", conversion)
        object.__setattr__(self, "_settings", FrozenJson(values))
        for node_id in self.node_ids:
            identifier(node_id, "node id")

    @property
    def configuration(self) -> dict[str, JsonValue]:
        return {"conversion": self.conversion, "settings": self._settings.value}

    @property
    def configuration_digest(self) -> str:
        return artifact_digest(self.configuration)

    @property
    def node_ids(self) -> tuple[str, str]:
        return (f"{self.id}/trigger", f"{self.id}/{_ROLES[self.conversion]}")

    @property
    def policies(self) -> dict[str, JsonValue]:
        common: dict[str, JsonValue] = {"construction": "inactive", "source": "trigger_record"}
        match self.conversion:
            case "deal_to_estimate":
                specific = {
                    "trigger": "deal.created",
                    "result": "draft_estimate",
                    "customer": "required_from_deal",
                    "amount_and_lines": "not_copied",
                }
            case "won_deal_to_order":
                specific = {
                    "trigger": "deal.stage_changed_to_selected",
                    "result": "draft_order",
                    "customer_and_lines": "from_deal",
                    "existing_bound_order": "preserve",
                }
            case "order_to_invoice":
                specific = {
                    "trigger": "order.created",
                    "result": "draft_invoice",
                    "billing_options": "native_conversion_defaults",
                }
            case "purchase_order_to_bill":
                specific = {
                    "trigger": "purchase_order.created",
                    "result": "draft_supplier_bill",
                    "supplier_and_lines": "from_purchase_order",
                    "existing_linked_bill": "skip",
                    "date_fallback": "workspace_local_today",
                }
            case "order_to_stock_out":
                specific = {
                    "trigger": "order.created",
                    "result": "stock_out",
                    "quantities": "order_lines",
                    "transaction_date": "execution_time",
                    "missing_inventory": "do_not_create",
                    "source_association": "required",
                }
        return {**common, **specific}

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return (NATIVE_CONVERSION_SCHEMA, f"flow.native.{self.conversion.replace('_', '-')}/v1")

    @classmethod
    def from_configuration(cls, id: str, value: object) -> Self:
        data = object_fields(value, {"conversion", "settings"}, "native conversion configuration")
        return cls(id, data["conversion"], data["settings"])

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": NATIVE_CONVERSION_SCHEMA,
            "id": self.id,
            **self.configuration,
            "node_ids": list(self.node_ids),
            "policies": self.policies,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = object_fields(
            value,
            {"schema_version", "id", "conversion", "settings", "node_ids", "policies"},
            "native conversion workflow",
        )
        if data["schema_version"] != NATIVE_CONVERSION_SCHEMA:
            raise ValueError("unsupported native conversion schema")
        result = cls(data["id"], data["conversion"], data["settings"])
        if data["node_ids"] != list(result.node_ids) or data["policies"] != result.policies:
            raise ValueError("native conversion identities and policies cannot be changed")
        return result


def _approver(value: object) -> str:
    selected = text(value, "approver ID")
    if not selected.isascii() or not selected.isdecimal() or not 0 < int(selected) < 2**63:
        raise ValueError("approver IDs must be positive workspace-user IDs")
    if str(int(selected)) != selected:
        raise ValueError("approver IDs must use canonical decimal spelling")
    return selected


@dataclass(frozen=True, slots=True)
class NativeSourceApprovalWorkflow(WireRecord):
    id: str
    subject: ApprovalSubject
    approver_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.id, "workflow id")
        choice(self.subject, _SUBJECTS, "approval subject")
        if type(self.approver_ids) is not tuple or not 1 <= len(self.approver_ids) <= 100:
            raise ValueError("select one to 100 ordered approver IDs")
        for selected in self.approver_ids:
            _approver(selected)
        if len(set(self.approver_ids)) != len(self.approver_ids):
            raise ValueError("approver IDs must be unique")
        for node_id in self.node_ids:
            identifier(node_id, "node id")

    @property
    def configuration(self) -> dict[str, JsonValue]:
        return {"subject": self.subject, "approver_ids": list(self.approver_ids)}

    @property
    def configuration_digest(self) -> str:
        return artifact_digest(self.configuration)

    @property
    def node_ids(self) -> tuple[str, str]:
        return (f"{self.id}/trigger", f"{self.id}/review")

    @property
    def policies(self) -> dict[str, JsonValue]:
        return {
            "construction": "inactive",
            "trigger": "record.created",
            "source": "durable_trigger_record",
            "approvals": "selected_sequence",
            "decision_target": "workflow_history",
            "source_status": "unchanged",
        }

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return (
            NATIVE_APPROVAL_SCHEMA,
            "flow.native.ordered-source-approval/v1",
            f"flow.native.{self.subject.replace('_', '-')}-created/v1",
        )

    @classmethod
    def from_configuration(cls, id: str, value: object) -> Self:
        data = object_fields(value, {"subject", "approver_ids"}, "native approval configuration")
        return cls(id, data["subject"], array(data["approver_ids"], _approver, "approver IDs"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": NATIVE_APPROVAL_SCHEMA,
            "id": self.id,
            **self.configuration,
            "node_ids": list(self.node_ids),
            "policies": self.policies,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = object_fields(
            value,
            {"schema_version", "id", "subject", "approver_ids", "node_ids", "policies"},
            "native approval workflow",
        )
        if data["schema_version"] != NATIVE_APPROVAL_SCHEMA:
            raise ValueError("unsupported native approval schema")
        result = cls(
            data["id"], data["subject"], array(data["approver_ids"], _approver, "approver IDs")
        )
        if data["node_ids"] != list(result.node_ids) or data["policies"] != result.policies:
            raise ValueError("native approval identities and policies cannot be changed")
        return result
