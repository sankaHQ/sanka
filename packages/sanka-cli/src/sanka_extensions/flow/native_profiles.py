# SPDX-License-Identifier: Apache-2.0
"""Closed native profile dispatch, never an extension-supplied executable hook."""

from sanka_extensions.flow.native import NATIVE_ORDER_BILLING_SCHEMA, NativeOrderBillingWorkflow
from sanka_extensions.flow.native_process_recipes import (
    NATIVE_PROCESS_SCHEMA,
    NativeBusinessProcessWorkflow,
)
from sanka_extensions.flow.native_recipes import (
    NATIVE_APPROVAL_SCHEMA,
    NATIVE_CONVERSION_SCHEMA,
    NativeRecordConversionWorkflow,
    NativeSourceApprovalWorkflow,
)
from sanka_extensions.flow.native_task_recipes import NATIVE_TASK_SCHEMA, NativeAssignedTaskWorkflow

type NativeWorkflow = (
    NativeOrderBillingWorkflow
    | NativeRecordConversionWorkflow
    | NativeSourceApprovalWorkflow
    | NativeAssignedTaskWorkflow
    | NativeBusinessProcessWorkflow
)

NATIVE_PROFILE_SCHEMAS = frozenset(
    (
        NATIVE_ORDER_BILLING_SCHEMA,
        NATIVE_CONVERSION_SCHEMA,
        NATIVE_APPROVAL_SCHEMA,
        NATIVE_TASK_SCHEMA,
        NATIVE_PROCESS_SCHEMA,
    )
)


def decode_native_workflow(value: object) -> NativeWorkflow:
    if type(value) is not dict:
        raise ValueError("native workflow must be an object")
    match value.get("schema_version"):
        case "sanka-flow-native-order-billing/v1":
            return NativeOrderBillingWorkflow.from_dict(value)
        case "sanka-flow-native-record-conversion/v1":
            return NativeRecordConversionWorkflow.from_dict(value)
        case "sanka-flow-native-source-approval/v1":
            return NativeSourceApprovalWorkflow.from_dict(value)
        case "sanka-flow-native-assigned-tasks/v1":
            return NativeAssignedTaskWorkflow.from_dict(value)
        case "sanka-flow-native-business-process/v1":
            return NativeBusinessProcessWorkflow.from_dict(value)
        case _:
            raise ValueError("unsupported native workflow profile")
