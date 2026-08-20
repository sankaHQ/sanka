# SPDX-License-Identifier: Apache-2.0
"""The Sanka Migrate connector SPI: base protocols plus optional capability protocols.

Base protocols carry the minimum a connector must implement. Everything else
is an optional capability protocol the runtime discovers with ``isinstance``
(all protocols here are ``runtime_checkable``) — never with ``getattr``. A
connector advertises a capability by implementing the protocol; the runtime
degrades gracefully when one is absent.

PRD-verb mapping: ``inspect`` → :class:`SupportsIdentityInspection`,
``discover_schema`` → ``discover_objects``/``inventory``, ``read`` →
``read_records``, ``write`` → ``write_record`` (+ batch capabilities),
``capabilities``/``limits`` → capability protocols + :class:`SupportsLimits`,
``validate`` → :class:`SupportsConfigValidation`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sanka.connector.credentials import Credentials
from sanka.connector.provisioning import (
    CustomObjectDefinition,
    PipelineDefinition,
    PropertyDefinition,
    PropertyResult,
    ResourceResult,
)
from sanka.connector.records import (
    BatchRelationshipWriteResult,
    BatchWriteInput,
    BatchWriteResult,
    OwnerProfile,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    SourceFilter,
    WriteOptions,
    WriteResult,
)
from sanka.connector.schema import Inventory, ProviderIdentity, SourceObject


@runtime_checkable
class SourceConnector(Protocol):
    """Minimum contract for reading a system out."""

    provider: str
    binding_kind: str

    async def discover_objects(
        self,
        credentials: Credentials,
    ) -> list[SourceObject]: ...

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory: ...

    async def read_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None = None,
        source_filter: SourceFilter | None = None,
    ) -> RecordPage: ...


@runtime_checkable
class DestinationConnector(Protocol):
    """Minimum contract for writing a system in."""

    provider: str
    binding_kind: str

    def automatic_target_object(self, canonical_type: str) -> str | None: ...

    async def inventory(
        self,
        credentials: Credentials,
        *,
        canonical_types: set[str],
    ) -> Inventory: ...

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult: ...

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult: ...


# --------------------------------------------------------------------------
# Optional capability protocols
# --------------------------------------------------------------------------


@runtime_checkable
class SupportsIdentityInspection(Protocol):
    """Verify and read back the identity behind a connection (safety tenet:
    runs are pinned to a verified identity before any mutation)."""

    async def inspect(self, credentials: Credentials) -> ProviderIdentity: ...


@runtime_checkable
class SupportsConfigValidation(Protocol):
    """Self-check connector configuration/reachability without mutating."""

    async def validate(self, credentials: Credentials) -> list[str]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Limits:
    """Provider limits the engine should respect; ``None`` means unknown."""

    max_read_page_size: int | None = None
    max_write_batch_size: int | None = None
    max_relationship_batch_size: int | None = None
    min_request_interval_ms: int | None = None


@runtime_checkable
class SupportsLimits(Protocol):
    def limits(self) -> Limits: ...


@runtime_checkable
class SupportsRecordCounts(Protocol):
    """Source: exact record counts per object/filter."""

    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int: ...


@runtime_checkable
class SupportsHighWaterMark(Protocol):
    """:class:`SupportsSnapshotBounds` refined to the freeze step alone."""

    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None: ...


@runtime_checkable
class SupportsBoundedReads(Protocol):
    """:class:`SupportsSnapshotBounds` refined to bounded reads alone."""

    async def read_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None = None,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> RecordPage: ...


@runtime_checkable
class SupportsBoundedCounts(Protocol):
    """:class:`SupportsSnapshotBounds` refined to bounded counts alone."""

    async def count_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> int: ...


@runtime_checkable
class SupportsSnapshotBounds(Protocol):
    """Source: freeze a run's scope at a high-water mark and read/count within
    it — the basis of exact-scope, resumable execution.

    The union of :class:`SupportsHighWaterMark`, :class:`SupportsBoundedReads`
    and :class:`SupportsBoundedCounts`: a connector implementing all three
    methods satisfies the refinements and this bundle alike. Runtimes may
    probe per method via the refinements; connectors that can only offer a
    subset implement just the matching refinements."""

    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None: ...

    async def read_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None = None,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> RecordPage: ...

    async def count_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> int: ...


@runtime_checkable
class SupportsOwnerDirectory(Protocol):
    """Either side: list users for owner/assignee mapping."""

    async def list_owners(
        self,
        credentials: Credentials,
    ) -> list[OwnerProfile]: ...


@runtime_checkable
class SupportsBatchWrites(Protocol):
    """Destination: batched record writes with per-item trace reconciliation."""

    async def write_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[BatchWriteInput],
        options: WriteOptions,
    ) -> list[BatchWriteResult]: ...


@runtime_checkable
class SupportsBatchRelationshipWrites(Protocol):
    """Destination: batched relationship writes."""

    async def write_relationships(
        self,
        credentials: Credentials,
        *,
        relationships: list[RelationshipWrite],
    ) -> list[BatchRelationshipWriteResult]: ...


@runtime_checkable
class SupportsPropertyProvisioning(Protocol):
    """:class:`SupportsSchemaProvisioning` refined to property reconciliation."""

    async def reconcile_properties(
        self,
        credentials: Credentials,
        *,
        definitions: list[PropertyDefinition],
        confirm: bool,
    ) -> list[PropertyResult]: ...


@runtime_checkable
class SupportsResourceProvisioning(Protocol):
    """:class:`SupportsSchemaProvisioning` refined to resource (pipeline /
    custom object) reconciliation."""

    async def reconcile_resources(
        self,
        credentials: Credentials,
        *,
        pipelines: list[PipelineDefinition],
        custom_objects: list[CustomObjectDefinition],
        confirm: bool,
    ) -> list[ResourceResult]: ...


@runtime_checkable
class SupportsSchemaProvisioning(Protocol):
    """Destination: create missing properties/pipelines/custom objects.
    ``confirm=False`` must be a pure dry run.

    The union of :class:`SupportsPropertyProvisioning` and
    :class:`SupportsResourceProvisioning`: a connector implementing both
    methods satisfies the refinements and this bundle alike. Runtimes may
    probe per method via the refinements; connectors that can only offer one
    side implement just the matching refinement."""

    async def reconcile_properties(
        self,
        credentials: Credentials,
        *,
        definitions: list[PropertyDefinition],
        confirm: bool,
    ) -> list[PropertyResult]: ...

    async def reconcile_resources(
        self,
        credentials: Credentials,
        *,
        pipelines: list[PipelineDefinition],
        custom_objects: list[CustomObjectDefinition],
        confirm: bool,
    ) -> list[ResourceResult]: ...


@runtime_checkable
class SupportsRetryMetrics(Protocol):
    """Either side: expose transport retry/throttle counters for run reports."""

    def retry_metrics(self) -> dict[str, Any]: ...
