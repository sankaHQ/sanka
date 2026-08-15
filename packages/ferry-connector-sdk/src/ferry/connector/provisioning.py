# SPDX-License-Identifier: Apache-2.0
"""Destination provisioning types for schema reconciliation.

Destinations that can create missing properties, pipelines, or custom object
schemas (PRD remediation level 2) implement ``SupportsSchemaProvisioning``
with these inputs/results. ``confirm=False`` is a dry run: results report
``would_create`` without mutating the destination.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PropertyProvisionStatus = Literal[
    "existing", "would_create", "created", "conflict", "unsupported", "failed"
]
ResourceProvisionStatus = Literal["existing", "would_create", "created", "conflict", "failed"]


@dataclass(frozen=True, slots=True, kw_only=True)
class PropertyDefinition:
    """A destination property to ensure exists, derived from a source field."""

    source_field: str
    target_object: str
    internal_name: str
    label: str
    source_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PropertyResult:
    source_field: str
    target_object: str
    internal_name: str
    label: str
    status: PropertyProvisionStatus
    source_type: str | None = None
    target_type: str | None = None
    field_type: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineStage:
    key: str
    label: str
    display_order: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineDefinition:
    key: str
    label: str
    object_type: str = "deals"
    display_order: int = 0
    stages: list[PipelineStage] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class CustomObjectDefinition:
    key: str
    source_object: str
    internal_name: str
    singular_label: str
    plural_label: str
    primary_display_property: str
    associated_objects: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceResult:
    resource_type: Literal["pipeline", "custom_object"]
    key: str
    label: str
    status: ResourceProvisionStatus
    provider_id: str | None = None
    stage_ids: dict[str, str] = field(default_factory=dict)
    message: str | None = None
