# SPDX-License-Identifier: Apache-2.0
"""Declarative Sanka Flow artifacts; this module never executes a workflow."""

from sanka_extensions.flow._wire import artifact_digest
from sanka_extensions.flow.blueprint import (
    BLUEPRINT_SCHEMA_VERSION,
    BLUEPRINT_V2_SCHEMA_VERSION,
    BLUEPRINT_V3_SCHEMA_VERSION,
    BLUEPRINT_V4_SCHEMA_VERSION,
    BLUEPRINT_V5_SCHEMA_VERSION,
    Blueprint,
    Resource,
)
from sanka_extensions.flow.definition import (
    SCHEMA_VERSION,
    FlowDefinition,
    create,
    decode_definition,
    encode_definition,
)
from sanka_extensions.flow.graph import (
    Action,
    AssociationMapping,
    Condition,
    FieldMapping,
    RecordIdentity,
    Trigger,
    ValueBinding,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
)
from sanka_extensions.flow.identity import (
    ArtifactIdentity,
    BlueprintOrigin,
    Mapping,
    Reference,
    UnsupportedFinding,
)
from sanka_extensions.flow.native import NativeOrderBillingWorkflow
from sanka_extensions.flow.native_fixtures import (
    NativeBillingFixtureManifest,
    NativeBillingFixturePage,
    NativeBillingFixturePageRef,
    NativeBillingFixtureRecord,
)
from sanka_extensions.flow.native_process_recipes import NativeBusinessProcessWorkflow
from sanka_extensions.flow.native_recipes import (
    NativeRecordConversionWorkflow,
    NativeSourceApprovalWorkflow,
)
from sanka_extensions.flow.native_task_recipes import NativeAssignedTaskWorkflow
from sanka_extensions.flow.native_verification import NativeBillingDelivery, NativeBillingScenario
from sanka_extensions.flow.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    BlueprintRequest,
    BlueprintResponse,
    FlowCapability,
    FlowExtensionFailure,
    ReferenceRequirement,
    TargetIdentity,
    ValueRequirement,
    decode_message,
    encode_message,
)
from sanka_extensions.flow.scenario import Scenario, ScenarioEvent
from sanka_extensions.flow.source import (
    SOURCE_SCHEMA_VERSION,
    SourceEdge,
    SourceNode,
    SourceSnapshot,
)

__all__ = [
    "BLUEPRINT_SCHEMA_VERSION",
    "BLUEPRINT_V2_SCHEMA_VERSION",
    "BLUEPRINT_V3_SCHEMA_VERSION",
    "BLUEPRINT_V4_SCHEMA_VERSION",
    "BLUEPRINT_V5_SCHEMA_VERSION",
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "SCHEMA_VERSION",
    "SOURCE_SCHEMA_VERSION",
    "Action",
    "ArtifactIdentity",
    "AssociationMapping",
    "Blueprint",
    "BlueprintOrigin",
    "BlueprintRequest",
    "BlueprintResponse",
    "Condition",
    "FieldMapping",
    "FlowCapability",
    "FlowDefinition",
    "FlowExtensionFailure",
    "Mapping",
    "NativeAssignedTaskWorkflow",
    "NativeBillingDelivery",
    "NativeBillingFixtureManifest",
    "NativeBillingFixturePage",
    "NativeBillingFixturePageRef",
    "NativeBillingFixtureRecord",
    "NativeBillingScenario",
    "NativeBusinessProcessWorkflow",
    "NativeOrderBillingWorkflow",
    "NativeRecordConversionWorkflow",
    "NativeSourceApprovalWorkflow",
    "RecordIdentity",
    "Reference",
    "ReferenceRequirement",
    "Resource",
    "Scenario",
    "ScenarioEvent",
    "SourceEdge",
    "SourceNode",
    "SourceSnapshot",
    "TargetIdentity",
    "Trigger",
    "UnsupportedFinding",
    "ValueBinding",
    "ValueRequirement",
    "WorkflowEdge",
    "WorkflowGraph",
    "WorkflowNode",
    "artifact_digest",
    "create",
    "decode_definition",
    "decode_message",
    "encode_definition",
    "encode_message",
]
