# SPDX-License-Identifier: Apache-2.0
"""HubSpot source connector.

Discovers the four standard CRM objects (companies, contacts, deals,
tickets) plus every unarchived custom object schema, inventories properties
and record counts, and reads records through the CRM search endpoint with
its opaque ``after`` cursor. ``associations.<type>`` field keys are resolved
per page through the v4 batch association read and land on each record as a
list of target ids under the same key.

Source-side filters are not supported yet and raise
:class:`sanka.connector.UnsupportedFeatureError` rather than being silently
ignored.
"""

from __future__ import annotations

from typing import Any

from sanka.connector import (
    Credentials,
    Inventory,
    RecordPage,
    SourceFilter,
    SourceObject,
    UnsupportedFeatureError,
)
from sanka_connector_hubspot._base import (
    OBJECTS_BY_CANONICAL_TYPE,
    HubSpotGateway,
    bounded_map,
    hubspot_errors,
    inventory_object,
)


class HubSpotSource:
    provider = "hubspot"
    binding_kind = "channel"

    def __init__(self, *, gateway: HubSpotGateway | None = None) -> None:
        self._gateway = gateway or HubSpotGateway()

    async def discover_objects(
        self,
        credentials: Credentials,
    ) -> list[SourceObject]:
        async with hubspot_errors():
            return await self._discover_objects(credentials)

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        async with hubspot_errors():
            requested = object_types or sorted(OBJECTS_BY_CANONICAL_TYPE.values())
            reverse = {value: key for key, value in OBJECTS_BY_CANONICAL_TYPE.items()}
            discovered = {item.key: item for item in await self._discover_objects(credentials)}
            objects = await bounded_map(
                requested,
                lambda object_type: inventory_object(
                    self._gateway,
                    credentials,
                    object_type=object_type,
                    canonical_type=(
                        discovered[object_type].canonical_type
                        if object_type in discovered
                        else reverse.get(object_type, object_type)
                    ),
                    label=(
                        discovered[object_type].label if object_type in discovered else object_type
                    ),
                ),
            )
            return Inventory(
                provider=self.provider,
                connection_id=credentials.connection_id,
                objects=objects,
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
        if source_filter is not None:
            raise UnsupportedFeatureError(
                "HubSpot source filters are not supported by Sanka yet.",
                details={"code": "SANKA_MIGRATE_SOURCE_FILTER_UNSUPPORTED"},
            )
        async with hubspot_errors():
            association_types = sorted(
                {key.split(".", 1)[1] for key in field_keys if key.startswith("associations.")}
            )
            property_keys = [key for key in field_keys if not key.startswith("associations.")]
            payload: dict[str, Any] = {
                "limit": max(1, min(int(limit or 100), 200)),
                "properties": sorted(set(property_keys)),
            }
            if cursor:
                payload["after"] = cursor
            response = await self._gateway.search_crm_objects(
                credentials,
                object_type=object_type,
                payload=payload,
            )
            raw_records = response.get("results")
            records = []
            if isinstance(raw_records, list):
                for raw in raw_records:
                    if not isinstance(raw, dict):
                        continue
                    record_id = str(raw.get("id") or "").strip()
                    properties = raw.get("properties")
                    if record_id:
                        records.append(
                            {
                                "id": record_id,
                                **(properties if isinstance(properties, dict) else {}),
                            }
                        )
            record_by_id = {str(record["id"]): record for record in records}
            for association_type in association_types:
                associations = await self._gateway.batch_read_crm_associations(
                    credentials,
                    from_object=object_type,
                    to_object=association_type,
                    from_ids=list(record_by_id),
                )
                for source_id, targets in associations.items():
                    record = record_by_id.get(str(source_id))
                    if record is None:
                        continue
                    record[f"associations.{association_type}"] = [
                        str(target.get("toObjectId") or target.get("id") or "")
                        for target in targets
                        if isinstance(target, dict)
                        and (target.get("toObjectId") or target.get("id"))
                    ]
            paging = response.get("paging")
            next_page = paging.get("next") if isinstance(paging, dict) else None
            next_cursor = (
                str(next_page.get("after") or "").strip() if isinstance(next_page, dict) else ""
            )
            return RecordPage(
                object_key=object_type,
                records=records,
                next_cursor=next_cursor or None,
                has_more=bool(next_cursor),
            )

    async def _discover_objects(
        self,
        credentials: Credentials,
    ) -> list[SourceObject]:
        options = {
            object_type: SourceObject(
                key=object_type,
                label=object_type.title(),
                canonical_type=canonical_type,
                default_selected=True,
            )
            for canonical_type, object_type in OBJECTS_BY_CANONICAL_TYPE.items()
        }
        schemas = await self._gateway.list_crm_schemas(credentials)
        rows = schemas.get("results")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("archived") is True:
                continue
            object_type = str(row.get("objectTypeId") or row.get("name") or "").strip()
            if not object_type or object_type in options:
                continue
            labels = row.get("labels")
            label = (
                str(labels.get("plural") or labels.get("singular") or object_type)
                if isinstance(labels, dict)
                else object_type
            )
            canonical_type = str(row.get("name") or object_type).strip().lower()
            options[object_type] = SourceObject(
                key=object_type,
                label=label,
                canonical_type=canonical_type,
                custom=True,
            )
        return sorted(
            options.values(),
            key=lambda item: (not item.default_selected, item.label.lower()),
        )
