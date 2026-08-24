# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from typing import Any

import pytest

from sanka.connector import (
    ConnectorRegistration,
    Credentials,
    FieldSchema,
    Inventory,
    ObjectSchema,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    SourceFilter,
    SourceObject,
    WriteOptions,
    WriteResult,
)
from sanka.runtime.engine import ExecutionError, MigrationEngine
from sanka.runtime.registry import ConnectorRegistry
from sanka.runtime.spec import EndpointSpec, MigrationSpec
from sanka.runtime.state import SqliteStateStore


class Provider:
    def __init__(self, credentials: dict[str, Credentials]) -> None:
        self.credentials = credentials
        self.calls: list[str] = []

    async def resolve(self, connection: str) -> Credentials:
        self.calls.append(connection)
        return self.credentials[connection]


class Source:
    provider = "source"
    binding_kind = "channel"

    def __init__(self) -> None:
        self.seen: list[Credentials] = []

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        self.seen.append(credentials)
        return [
            SourceObject(
                key="contacts",
                label="Contacts",
                canonical_type="contact",
                default_selected=True,
            )
        ]

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        self.seen.append(credentials)
        return Inventory(
            provider=self.provider,
            objects=[
                ObjectSchema(
                    key="contacts",
                    label="Contacts",
                    canonical_type="contact",
                    record_count=1,
                    fields=[
                        FieldSchema(key="id", label="ID"),
                        FieldSchema(key="email", label="Email"),
                    ],
                    identity_fields=["id"],
                )
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
        self.seen.append(credentials)
        return RecordPage(
            object_key=object_type,
            records=[{"id": "1", "email": "one@example.com"}],
        )


class Destination:
    provider = "destination"
    binding_kind = "channel"

    def __init__(self) -> None:
        self.seen: list[Credentials] = []

    def automatic_target_object(self, canonical_type: str) -> str | None:
        return "contacts" if canonical_type == "contact" else None

    async def inventory(
        self,
        credentials: Credentials,
        *,
        canonical_types: set[str],
    ) -> Inventory:
        self.seen.append(credentials)
        return Inventory(provider=self.provider)

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        self.seen.append(credentials)
        return WriteResult(status="created", destination_record_id=str(properties["id"]))

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        self.seen.append(credentials)
        return RelationshipWriteResult(status="linked")


async def test_engine_resolves_named_connections_without_persisting_secrets(tmp_path: Any) -> None:
    source = Source()
    destination = Destination()
    provider = Provider(
        {
            "source-connection": Credentials(
                provider="source",
                access_token="source-secret",
                settings={"api_base_url": "https://source.example"},
            ),
            "destination-connection": Credentials(
                provider="destination",
                access_token="destination-secret",
                settings={"api_base_url": "https://destination.example"},
            ),
        }
    )
    store = SqliteStateStore(tmp_path / "state.db")
    engine = MigrationEngine(
        store=store,
        registry=ConnectorRegistry(
            {
                "source": ConnectorRegistration(name="source", source=source),
                "destination": ConnectorRegistration(
                    name="destination",
                    destination=destination,
                ),
            }
        ),
        credential_provider=provider,
    )
    spec = MigrationSpec(
        source=EndpointSpec(
            type="source",
            connection="source-connection",
            options={"tenant": "tenant-a"},
        ),
        target=EndpointSpec(type="destination", connection="destination-connection"),
    )
    run_id = engine.create(spec)
    plan = await engine.plan(run_id)
    await engine.apply(run_id, plan_hash=plan.plan_hash)

    assert source.seen
    assert destination.seen
    assert all(item.access_token == "source-secret" for item in source.seen)
    assert source.seen[0].settings == {
        "api_base_url": "https://source.example",
        "tenant": "tenant-a",
    }
    assert all(item.access_token == "destination-secret" for item in destination.seen)
    persisted = store.get_run(run_id).spec_json
    assert "source-secret" not in persisted
    assert "destination-secret" not in persisted


async def test_engine_rejects_named_connection_provider_mismatch(tmp_path: Any) -> None:
    provider = Provider(
        {
            "wrong": Credentials(provider="destination", access_token="secret"),
        }
    )
    source = Source()
    engine = MigrationEngine(
        store=SqliteStateStore(tmp_path / "state.db"),
        registry=ConnectorRegistry({"source": ConnectorRegistration(name="source", source=source)}),
        credential_provider=provider,
    )
    run_id = engine.create(
        MigrationSpec(
            source=EndpointSpec(type="source", connection="wrong"),
            target=EndpointSpec(type="source", connection="wrong"),
        )
    )
    with pytest.raises(ExecutionError, match="resolved provider"):
        await engine.inspect(run_id)
