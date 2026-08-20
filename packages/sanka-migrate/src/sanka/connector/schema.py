# SPDX-License-Identifier: Apache-2.0
"""Schema and inventory types — the discovery half of the migration graph.

``discover_objects`` answers "what can be migrated"; ``inventory`` answers
"what exactly exists" (objects, fields, counts, identity fields). Both sides
of a migration produce these, and the planner maps between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldSchema:
    key: str
    label: str
    data_type: str = "string"
    required: bool = False
    writable: bool = True
    unique: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ObjectSchema:
    """One migratable object type with its fields and volume."""

    key: str
    label: str
    canonical_type: str
    record_count: int = 0
    fields: list[FieldSchema] = field(default_factory=list)
    identity_fields: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class Inventory:
    """The inspected state of one side of a migration."""

    provider: str
    connection_id: str | None = None
    objects: list[ObjectSchema] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceObject:
    """A discoverable source object type, before full inventory.

    ``automatic_target_object`` is the connector's suggested destination
    object for this type; the planner may override it.
    """

    key: str
    label: str
    canonical_type: str
    default_selected: bool = False
    custom: bool = False
    queryable: bool = True
    automatic_target_object: str | None = None
    managed_mapping_required: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderIdentity:
    """Verified identity of the system behind a connection.

    Used for identity readback before any mutation: the operator (or agent)
    must see which account/host/tenant a run is actually pinned to.
    """

    provider: str
    account_id: str | None = None
    account_label: str | None = None
    host: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
