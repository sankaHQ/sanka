# SPDX-License-Identifier: Apache-2.0
"""SPI conformance: a reference in-memory connector satisfies the protocols
both structurally (mypy via annotations) and at runtime (isinstance)."""

from __future__ import annotations

from typing import Any

import pytest

from ferry.connector import (
    Credentials,
    CustomObjectDefinition,
    DestinationConnector,
    Inventory,
    ObjectSchema,
    PipelineDefinition,
    PropertyDefinition,
    PropertyResult,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    ResourceResult,
    SourceConnector,
    SourceFilter,
    SourceObject,
    SupportsBatchWrites,
    SupportsBoundedCounts,
    SupportsBoundedReads,
    SupportsHighWaterMark,
    SupportsOwnerDirectory,
    SupportsPropertyProvisioning,
    SupportsResourceProvisioning,
    SupportsSchemaProvisioning,
    SupportsSnapshotBounds,
    WriteOptions,
    WriteResult,
)


class InMemorySource:
    provider = "memory"
    binding_kind = "test"

    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self._rows = rows

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        return [SourceObject(key=key, label=key.title(), canonical_type=key) for key in self._rows]

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        keys = object_types or sorted(self._rows)
        return Inventory(
            provider=self.provider,
            objects=[
                ObjectSchema(
                    key=key,
                    label=key.title(),
                    canonical_type=key,
                    record_count=len(self._rows.get(key, [])),
                )
                for key in keys
            ],
        )

    async def read_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None = None,
        source_filter: SourceFilter | None = None,
    ) -> RecordPage:
        rows = self._rows.get(object_type, [])
        start = int(cursor) if cursor else 0
        page = rows[start : start + limit]
        next_start = start + len(page)
        return RecordPage(
            object_key=object_type,
            records=[{k: row.get(k) for k in field_keys} for row in page],
            next_cursor=str(next_start) if next_start < len(rows) else None,
            has_more=next_start < len(rows),
        )


class InMemoryDestination:
    provider = "memory"
    binding_kind = "test"

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []

    def automatic_target_object(self, canonical_type: str) -> str | None:
        return canonical_type

    async def inventory(
        self,
        credentials: Credentials,
        *,
        canonical_types: set[str],
    ) -> Inventory:
        return Inventory(provider=self.provider)

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        self.written.append({"object_type": object_type, **properties})
        return WriteResult(status="created", destination_record_id=str(len(self.written)))

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        return RelationshipWriteResult(status="linked")


class SnapshotBoundedSource(InMemorySource):
    """InMemorySource plus every method of the snapshot-bounds bundle."""

    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None:
        return "2026-01-01T00:00:00Z"

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
    ) -> RecordPage:
        return await self.read_records(
            credentials,
            object_type=object_type,
            field_keys=field_keys,
            limit=limit,
            cursor=cursor,
            source_filter=source_filter,
        )

    async def count_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> int:
        return 0


class HighWaterMarkOnlySource(InMemorySource):
    """InMemorySource plus the freeze step alone — no bounded reads/counts."""

    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None:
        return None


class ProvisioningDestination(InMemoryDestination):
    """InMemoryDestination plus every method of the provisioning bundle."""

    async def reconcile_properties(
        self,
        credentials: Credentials,
        *,
        definitions: list[PropertyDefinition],
        confirm: bool,
    ) -> list[PropertyResult]:
        return [
            PropertyResult(
                source_field=definition.source_field,
                target_object=definition.target_object,
                internal_name=definition.internal_name,
                label=definition.label,
                status="would_create",
            )
            for definition in definitions
        ]

    async def reconcile_resources(
        self,
        credentials: Credentials,
        *,
        pipelines: list[PipelineDefinition],
        custom_objects: list[CustomObjectDefinition],
        confirm: bool,
    ) -> list[ResourceResult]:
        return [
            ResourceResult(
                resource_type="pipeline",
                key=definition.key,
                label=definition.label,
                status="would_create",
            )
            for definition in pipelines
        ]


class PropertyProvisioningOnlyDestination(InMemoryDestination):
    """InMemoryDestination plus property reconciliation alone."""

    async def reconcile_properties(
        self,
        credentials: Credentials,
        *,
        definitions: list[PropertyDefinition],
        confirm: bool,
    ) -> list[PropertyResult]:
        return []


CREDS = Credentials(provider="memory")


def test_reference_connectors_satisfy_base_protocols() -> None:
    source: SourceConnector = InMemorySource({"docs": []})
    destination: DestinationConnector = InMemoryDestination()
    assert isinstance(source, SourceConnector)
    assert isinstance(destination, DestinationConnector)


def test_capability_protocols_are_absent_unless_implemented() -> None:
    source = InMemorySource({"docs": []})
    destination = InMemoryDestination()
    assert not isinstance(source, SupportsSnapshotBounds)
    assert not isinstance(source, SupportsOwnerDirectory)
    assert not isinstance(destination, SupportsBatchWrites)
    assert not isinstance(source, SupportsHighWaterMark)
    assert not isinstance(source, SupportsBoundedReads)
    assert not isinstance(source, SupportsBoundedCounts)
    assert not isinstance(destination, SupportsPropertyProvisioning)
    assert not isinstance(destination, SupportsResourceProvisioning)
    assert not isinstance(destination, SupportsSchemaProvisioning)


def test_granular_refinements_track_their_single_method() -> None:
    freeze_only = HighWaterMarkOnlySource({"docs": []})
    assert isinstance(freeze_only, SupportsHighWaterMark)
    assert not isinstance(freeze_only, SupportsBoundedReads)
    assert not isinstance(freeze_only, SupportsBoundedCounts)
    assert not isinstance(freeze_only, SupportsSnapshotBounds)

    property_only = PropertyProvisioningOnlyDestination()
    assert isinstance(property_only, SupportsPropertyProvisioning)
    assert not isinstance(property_only, SupportsResourceProvisioning)
    assert not isinstance(property_only, SupportsSchemaProvisioning)


def test_bundle_implementations_satisfy_their_refinements() -> None:
    # A class carrying every method of a bundle satisfies the bundled protocol
    # and each granular refinement alike — runtimes may probe either way.
    bounded: SupportsSnapshotBounds = SnapshotBoundedSource({"docs": []})
    assert isinstance(bounded, SupportsSnapshotBounds)
    assert isinstance(bounded, SupportsHighWaterMark)
    assert isinstance(bounded, SupportsBoundedReads)
    assert isinstance(bounded, SupportsBoundedCounts)

    provisioning: SupportsSchemaProvisioning = ProvisioningDestination()
    assert isinstance(provisioning, SupportsSchemaProvisioning)
    assert isinstance(provisioning, SupportsPropertyProvisioning)
    assert isinstance(provisioning, SupportsResourceProvisioning)


@pytest.mark.asyncio
async def test_read_write_round_trip() -> None:
    source = InMemorySource({"docs": [{"id": "1", "title": "a"}, {"id": "2", "title": "b"}]})
    destination = InMemoryDestination()

    page = await source.read_records(CREDS, object_type="docs", field_keys=["id", "title"], limit=1)
    assert page.has_more and page.next_cursor == "1"

    result = await destination.write_record(
        CREDS,
        object_type="docs",
        properties=page.records[0],
        options=WriteOptions(conflict_policy="create"),
    )
    assert result.status == "created"
    assert destination.written == [{"object_type": "docs", "id": "1", "title": "a"}]
