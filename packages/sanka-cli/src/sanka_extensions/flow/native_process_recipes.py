# SPDX-License-Identifier: Apache-2.0
"""Closed monthly billing, assignment and cross-department process declarations."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal, Self

from sanka_extensions.flow._wire import (
    FrozenJson,
    WireRecord,
    artifact_digest,
    choice,
    identifier,
    object_fields,
)
from sanka_extensions.flow.definition import JsonValue
from sanka_extensions.flow.native import _integer
from sanka_extensions.flow.native_task_recipes import (
    canonical_uuid,
    validate_task_settings,
    workspace_user_ids,
)

NATIVE_PROCESS_SCHEMA = "sanka-flow-native-business-process/v1"
BusinessProcess = Literal[
    "monthly_invoice_consolidation",
    "ticket_assignment",
    "sales_delivery_billing",
    "replenishment_purchasing",
]
_FIELDS = {
    "monthly_invoice_consolidation": {"local_hour", "billing_day", "invoice_due_days"},
    "ticket_assignment": {"assignee_ids"},
    "sales_delivery_billing": {"won_stage", "assignee_ids", "due_days", "invoice_due_days"},
    "replenishment_purchasing": {
        "inventory_id",
        "supplier_id",
        "stock_threshold",
        "reorder_quantity",
        "unit_price",
        "currency",
        "tax_rate",
        "assignee_ids",
        "due_days",
    },
}
_ROLES = {
    "monthly_invoice_consolidation": ("schedule", "draft-invoices"),
    "ticket_assignment": ("trigger", "assign-owner"),
    "sales_delivery_billing": ("trigger", "prepare-sales-delivery-billing"),
    "replenishment_purchasing": ("schedule", "prepare-purchasing"),
}
_CURRENCIES = ("JPY", "USD", "EUR", "GBP", "AUD", "CAD", "SGD", "CNY", "TWD", "KRW")


def _price(value: object) -> None:
    if type(value) is not str or not 1 <= len(value) <= 30:
        raise ValueError("unit_price requires a bounded decimal string")
    try:
        number = Decimal(value)
        if not number.is_finite() or not 0 <= number <= 1_000_000_000:
            raise ValueError("unit_price must be finite and between zero and 1000000000")
        exponent = number.as_tuple().exponent
        # Bound exponent expansion before formatting, including zero values that
        # pass the amount range check despite an enormous positive exponent.
        if type(exponent) is not int or not -6 <= exponent <= 29 or format(number, "f") != value:
            raise ValueError("unit_price requires fixed decimal notation with at most six places")
    except InvalidOperation as exc:
        raise ValueError("unit_price requires a decimal number") from exc


@dataclass(frozen=True, slots=True, init=False)
class NativeBusinessProcessWorkflow(WireRecord):
    id: str
    process: BusinessProcess
    _settings: FrozenJson = field(repr=False)

    def __init__(self, id: str, process: BusinessProcess, settings: dict[str, JsonValue]) -> None:
        identifier(id, "workflow id")
        choice(process, tuple(_FIELDS), "business process")
        values = object_fields(settings, _FIELDS[process], "business process settings")
        if process == "monthly_invoice_consolidation":
            _integer(values["local_hour"], 0, 23, "local_hour")
            _integer(values["billing_day"], 1, 28, "billing_day")
            _integer(values["invoice_due_days"], 0, 365, "invoice_due_days")
        elif process == "ticket_assignment":
            workspace_user_ids(values["assignee_ids"], "assignee_ids")
        else:
            validate_task_settings(values)
            if process == "sales_delivery_billing":
                stage = values["won_stage"]
                if type(stage) is not str or not stage.strip() or len(stage) > 200:
                    raise ValueError("won_stage requires nonblank text of at most 200 characters")
                _integer(values["invoice_due_days"], 0, 365, "invoice_due_days")
            else:
                canonical_uuid(values["inventory_id"], "inventory_id")
                canonical_uuid(values["supplier_id"], "supplier_id")
                _integer(values["reorder_quantity"], 1, 1_000_000, "reorder_quantity")
                _integer(values["tax_rate"], 0, 100, "tax_rate")
                choice(values["currency"], _CURRENCIES, "currency")
                _price(values["unit_price"])
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "process", process)
        object.__setattr__(self, "_settings", FrozenJson(values))
        for node_id in self.node_ids:
            identifier(node_id, "node id")

    @property
    def configuration(self) -> dict[str, JsonValue]:
        return {"process": self.process, "settings": self._settings.value}

    @property
    def configuration_digest(self) -> str:
        return artifact_digest(self.configuration)

    @property
    def node_ids(self) -> tuple[str, str]:
        trigger, action = _ROLES[self.process]
        return (f"{self.id}/{trigger}", f"{self.id}/{action}")

    @property
    def policies(self) -> dict[str, JsonValue]:
        values = self._settings.value
        assert type(values) is dict
        policies: dict[str, JsonValue] = {"construction": "inactive"}
        if self.process == "monthly_invoice_consolidation":
            policies.update(
                trigger="daily",
                local_hour=values["local_hour"],
                schedule_resolution="workspace_offset_at_construction",
                billing_window="previous_complete_workspace_local_calendar_month",
                eligible_run_days="selected_billing_day_and_later",
                selection="unbilled_orders_in_window",
                result="draft_invoices_grouped_by_customer",
                invoice_date="previous_month_last_day",
                due_date="invoice_date_plus_due_days",
                selection_limit=5000,
                selection_overflow="fail_before_any_write",
                partial_failure="retain_completed_groups_and_retry_remaining_unbilled_orders",
            )
        elif self.process == "ticket_assignment":
            policies.update(
                trigger="ticket.created",
                assignment="round_robin_selected_workspace_members",
                pool_order="ascending_workspace_user_id",
                existing_owner="preserve",
            )
        elif self.process == "sales_delivery_billing":
            policies.update(
                trigger="deal.stage_changed_to_selected",
                result="order_delivery_preparation_task_and_draft_invoice",
                delivery_completion_required=False,
                existing_bound_order="preserve",
                existing_invoice="preserve",
                transaction="sales_handoff_and_receipt",
                receipt_scope="workspace_workflow_node_deal_creation",
                invoice_date="workspace_local_today",
                due_date="invoice_date_plus_due_days",
            )
        else:
            policies.update(
                trigger="daily",
                local_hour=9,
                schedule_resolution="workspace_offset_at_construction",
                selection="selected_active_available_inventory_below_threshold",
                result="draft_purchase_order_and_receiving_task",
                lines="selected_inventory_item_quantity_price_supplier_and_tax",
                outstanding_purchase="skip_until_in_stock_or_archived",
                transaction="purchase_order_receiving_task_associations_and_receipt",
                receipt_scope="workspace_workflow_node_inventory_revision",
            )
        return policies

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        result: tuple[str, ...] = (
            NATIVE_PROCESS_SCHEMA,
            f"flow.native.{self.process.replace('_', '-')}/v1",
        )
        if self.process in {"monthly_invoice_consolidation", "replenishment_purchasing"}:
            result += ("flow.native.daily-run-offset-snapshot/v1",)
        return result

    @classmethod
    def from_configuration(cls, id: str, value: object) -> Self:
        data = object_fields(
            value, {"process", "settings"}, "native business process configuration"
        )
        return cls(id, data["process"], data["settings"])

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": NATIVE_PROCESS_SCHEMA,
            "id": self.id,
            **self.configuration,
            "node_ids": list(self.node_ids),
            "policies": self.policies,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = object_fields(
            value,
            {"schema_version", "id", "process", "settings", "node_ids", "policies"},
            "native business process workflow",
        )
        if data["schema_version"] != NATIVE_PROCESS_SCHEMA:
            raise ValueError("unsupported native business process schema")
        result = cls(data["id"], data["process"], data["settings"])
        if data["node_ids"] != list(result.node_ids) or artifact_digest(
            data["policies"]
        ) != artifact_digest(result.policies):
            raise ValueError("native business process identities and policies cannot be changed")
        return result
