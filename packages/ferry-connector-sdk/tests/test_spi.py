# SPDX-License-Identifier: Apache-2.0
"""SPI conformance: a reference in-memory connector satisfies the protocols
both structurally (mypy via annotations) and at runtime (isinstance)."""

from __future__ import annotations

from typing import Any

import pytest

from ferry.connector import (
    Credentials,
    DestinationConnector,
    Inventory,
    ObjectSchema,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    SourceConnector,
    SourceFilter,
    SourceObject,
    SupportsBatchWrites,
    SupportsOwnerDirectory,
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
