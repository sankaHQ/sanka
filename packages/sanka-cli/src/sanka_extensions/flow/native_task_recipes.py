# SPDX-License-Identifier: Apache-2.0
"""Closed assigned-task recipes; selectors never contain queries or native code.

The fixed policies describe the current hosted implementations. In particular,
daily triggers store the workspace's UTC offset at construction; they do not
promise automatic daylight-saving rescheduling. Authorization, source selection,
receipts and task writes belong to the host, not this declarative SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Self
from uuid import UUID

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

NATIVE_TASK_SCHEMA = "sanka-flow-native-assigned-tasks/v1"
TaskRecipe = Literal[
    "stalled_deal_follow_up",
    "overdue_invoice_reminder",
    "supplier_payment_reminder",
    "low_stock_replenishment",
    "stock_discrepancy_review",
    "expense_reimbursement_preparation",
    "expense_accounting_review",
    "missing_expense_receipt",
    "project_task_checklist",
    "milestone_reminder",
    "overdue_task_escalation",
    "unresolved_ticket_escalation",
    "ticket_resolution_follow_up",
    "employee_onboarding",
    "missing_attendance",
]

_FIELDS = {
    "stalled_deal_follow_up": {"local_hour", "open_stages", "stale_days"},
    "overdue_invoice_reminder": {"local_hour", "overdue_days"},
    "supplier_payment_reminder": {"local_hour", "days_before_due"},
    "low_stock_replenishment": {"local_hour", "stock_threshold"},
    "stock_discrepancy_review": {"local_hour", "difference_tolerance"},
    "expense_reimbursement_preparation": {"local_hour"},
    "expense_accounting_review": set(),
    "missing_expense_receipt": {"local_hour", "wait_days"},
    "project_task_checklist": {"task_titles"},
    "milestone_reminder": {"task_names", "open_statuses", "days_before_due"},
    "overdue_task_escalation": {"open_statuses", "overdue_days"},
    "unresolved_ticket_escalation": {"stale_days"},
    "ticket_resolution_follow_up": {"wait_days"},
    "employee_onboarding": {"task_titles", "days_before_start", "catch_up_days"},
    "missing_attendance": {"employee_ids", "weekdays", "excluded_dates", "lookback_days"},
}
_INTEGER_LIMITS = {
    "local_hour": (0, 23),
    "due_days": (1, 365),
    "stale_days": (1, 365),
    "overdue_days": (1, 365),
    "days_before_due": (0, 365),
    "wait_days": (1, 365),
    "stock_threshold": (0, 1_000_000),
    "difference_tolerance": (0, 1_000_000),
    "days_before_start": (0, 365),
    "catch_up_days": (0, 365),
    "lookback_days": (1, 31),
}
_SELECTIONS = {
    "stalled_deal_follow_up": "active_deals_in_selected_stages_unchanged_since_cutoff",
    "overdue_invoice_reminder": "sent_or_dunning_invoices_overdue_with_outstanding_balance",
    "supplier_payment_reminder": "received_or_approved_outstanding_bills_due_by_cutoff",
    "low_stock_replenishment": "active_available_inventory_below_threshold",
    "stock_discrepancy_review": "active_inventory_bucket_mismatch_or_negative_over_tolerance",
    "expense_reimbursement_preparation": "approved_expenses_with_outstanding_balance",
    "expense_accounting_review": "approved_or_reimbursed_expenses",
    "missing_expense_receipt": "submitted_or_approved_expenses_older_than_cutoff_without_files",
    "project_task_checklist": "created_project_unless_completed",
    "milestone_reminder": "selected_task_names_and_open_statuses_due_by_cutoff",
    "overdue_task_escalation": "selected_open_task_statuses_overdue_by_cutoff",
    "unresolved_ticket_escalation": "active_unresolved_tickets_unchanged_since_cutoff",
    "ticket_resolution_follow_up": "active_resolved_tickets_resolved_before_cutoff",
    "employee_onboarding": "active_employees_in_selected_start_date_window",
    "missing_attendance": "selected_employees_missing_entries_on_completed_selected_workdays",
}
_OPEN_TASK_GUARD = frozenset(
    {
        "low_stock_replenishment",
        "stock_discrepancy_review",
        "expense_reimbursement_preparation",
        "expense_accounting_review",
        "missing_expense_receipt",
        "unresolved_ticket_escalation",
        "ticket_resolution_follow_up",
    }
)


def canonical_string_list(
    value: object, label: str, *, minimum: int = 1, maximum: int = 100, length: int = 200
) -> list[str]:
    """Require already-resolved values; never silently trim, deduplicate or sort."""
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} requires {minimum} to {maximum} values")
    if any(
        type(item) is not str or not item or item.strip() != item or len(item) > length
        for item in value
    ):
        raise ValueError(f"{label} requires canonical nonblank text up to {length} characters")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must be unique")
    return value


def workspace_user_ids(value: object, label: str) -> list[str]:
    values = canonical_string_list(value, label, length=19)
    for item in values:
        if (
            not item.isascii()
            or not item.isdecimal()
            or not 0 < int(item) < 2**63
            or str(int(item)) != item
        ):
            raise ValueError(f"{label} requires canonical positive workspace-user IDs")
    return values


def canonical_uuid(value: object, label: str) -> str:
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError(f"{label} requires a canonical UUID")
    return value


def validate_task_settings(values: dict[str, JsonValue]) -> None:
    workspace_user_ids(values["assignee_ids"], "assignee_ids")
    for key, bounds in _INTEGER_LIMITS.items():
        if key in values:
            _integer(values[key], *bounds, key)
    for key in ("task_titles", "task_names", "open_statuses"):
        if key in values:
            canonical_string_list(values[key], key, maximum=50 if key == "open_statuses" else 20)
    if "open_stages" in values:
        for item in canonical_string_list(values["open_stages"], "open_stages"):
            canonical_uuid(item, "open stage")
    if "employee_ids" in values:
        workspace_user_ids(values["employee_ids"], "employee_ids")
        for weekday in canonical_string_list(values["weekdays"], "weekdays", maximum=7):
            choice(weekday, ("mon", "tue", "wed", "thu", "fri", "sat", "sun"), "weekday")
        for selected in canonical_string_list(
            values["excluded_dates"], "excluded_dates", minimum=0
        ):
            if date.fromisoformat(selected).isoformat() != selected:
                raise ValueError("excluded_dates must use YYYY-MM-DD")


@dataclass(frozen=True, slots=True, init=False)
class NativeAssignedTaskWorkflow(WireRecord):
    id: str
    task: TaskRecipe
    _settings: FrozenJson = field(repr=False)

    def __init__(self, id: str, task: TaskRecipe, settings: dict[str, JsonValue]) -> None:
        identifier(id, "workflow id")
        choice(task, tuple(_FIELDS), "assigned task recipe")
        values = object_fields(
            settings, _FIELDS[task] | {"assignee_ids", "due_days"}, "task settings"
        )
        validate_task_settings(values)
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "_settings", FrozenJson(values))
        for node_id in self.node_ids:
            identifier(node_id, "node id")

    @property
    def configuration(self) -> dict[str, JsonValue]:
        return {"task": self.task, "settings": self._settings.value}

    @property
    def configuration_digest(self) -> str:
        return artifact_digest(self.configuration)

    @property
    def node_ids(self) -> tuple[str, str]:
        trigger = "trigger" if self.task == "project_task_checklist" else "schedule"
        return (f"{self.id}/{trigger}", f"{self.id}/create-tasks")

    @property
    def policies(self) -> dict[str, JsonValue]:
        values = self._settings.value
        assert type(values) is dict
        checklist = self.task == "project_task_checklist"
        policies: dict[str, JsonValue] = {
            "construction": "inactive",
            "result": "assigned_tasks",
            "selection": _SELECTIONS[self.task],
            "source_status": "unchanged",
            "external_delivery": "none",
            "trigger": "project.created" if checklist else "daily",
            "task_dates": "execution_local_today_plus_due_days",
            "receipt_scope": "workspace_workflow_node_source_revision",
            "transaction": "complete_source_checklist_and_receipt"
            if self.task in {"project_task_checklist", "employee_onboarding"}
            else "source_task_and_receipt",
        }
        if not checklist:
            policies.update(
                local_hour=values.get("local_hour", 9),
                schedule_resolution="workspace_offset_at_construction",
                batch_limit=500,
                batch_unit="employees" if self.task == "employee_onboarding" else "tasks",
                batch_overflow="retain_committed_sources_and_retry_remaining",
            )
        if self.task in _OPEN_TASK_GUARD:
            policies["existing_open_source_task"] = "skip"
        if self.task in {"milestone_reminder", "overdue_task_escalation"}:
            policies["exclude_tasks"] = "completed_projects_and_follow_up_generated_tasks"
        if self.task == "missing_attendance":
            policies.update(
                exclude_workdays="selected_dates_attendance_and_approved_leave",
                visibility="complete_attendance_and_absence_read_scope",
                source_revision="completed_local_workday",
            )
        return policies

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        result: tuple[str, ...] = (
            NATIVE_TASK_SCHEMA,
            f"flow.native.{self.task.replace('_', '-')}/v1",
            "flow.native.source-task-receipts/v1",
        )
        if self.task != "project_task_checklist":
            result += ("flow.native.daily-run-offset-snapshot/v1",)
        return result

    @classmethod
    def from_configuration(cls, id: str, value: object) -> Self:
        data = object_fields(value, {"task", "settings"}, "native task configuration")
        return cls(id, data["task"], data["settings"])

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": NATIVE_TASK_SCHEMA,
            "id": self.id,
            **self.configuration,
            "node_ids": list(self.node_ids),
            "policies": self.policies,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = object_fields(
            value,
            {"schema_version", "id", "task", "settings", "node_ids", "policies"},
            "native task workflow",
        )
        if data["schema_version"] != NATIVE_TASK_SCHEMA:
            raise ValueError("unsupported native task schema")
        result = cls(data["id"], data["task"], data["settings"])
        if data["node_ids"] != list(result.node_ids) or artifact_digest(
            data["policies"]
        ) != artifact_digest(result.policies):
            raise ValueError("native task identities and policies cannot be changed")
        return result
