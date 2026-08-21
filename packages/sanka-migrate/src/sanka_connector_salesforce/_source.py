# SPDX-License-Identifier: Apache-2.0
"""Salesforce source connector.

A faithful port of the production Salesforce → Sanka source adapter. Objects
are discovered from the org's sObject catalog (queryable, non-deprecated
types), inventoried with a REST describe plus ``SELECT COUNT()``, and read
with keyset SOQL pagination on ``Id`` (``WHERE Id > cursor [AND Id <= bound]
ORDER BY Id ASC LIMIT n``, page size clamped to 1-200). Identity is always
``Id``. All reads go through the ``queryAll`` endpoint, so archived and
recycle-bin records are included — exactly like the production adapter.

Capabilities: exact counts (:class:`sanka.connector.SupportsRecordCounts`),
snapshot bounds on the maximum ``Id``
(:class:`sanka.connector.SupportsSnapshotBounds`), and the active-user
directory (:class:`sanka.connector.SupportsOwnerDirectory`). Source filters
support ``equals`` on a boolean field, never on ``Id``. Object, field, cursor,
and bound values are validated against strict character classes before they
are interpolated into SOQL — invalid input raises
:class:`sanka.connector.ValidationFailedError` instead of reaching the org.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from sanka.connector import (
    ConnectorError,
    Credentials,
    FieldSchema,
    Inventory,
    ObjectSchema,
    OwnerProfile,
    RecordPage,
    SourceFilter,
    SourceObject,
    UnsupportedFeatureError,
    ValidationFailedError,
)
from sanka_connector_salesforce._gateway import HttpSalesforceGateway, SalesforceGateway

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")

_DEFAULT_OBJECTS = ("Account", "Contact", "Opportunity", "Lead")
_CANONICAL_OBJECTS = {
    "account": "company",
    "contact": "contact",
    "lead": "contact",
    "opportunity": "deal",
    "case": "ticket",
}


async def _bounded_map[InputT, OutputT](
    values: Iterable[InputT],
    worker: Callable[[InputT], Awaitable[OutputT]],
    *,
    limit: int = 4,
) -> list[OutputT]:
    """Run provider calls concurrently without creating an unbounded API burst."""

    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(value: InputT) -> OutputT:
        async with semaphore:
            return await worker(value)

    return list(await asyncio.gather(*(run(value) for value in values)))


def _identifier(value: str, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValidationFailedError(f"Invalid Salesforce {label}: {normalized or 'missing'}")
    return normalized


def _record_count(payload: dict[str, Any]) -> int:
    records = payload.get("records")
    if isinstance(records, list) and records and isinstance(records[0], dict):
        value = records[0].get("expr0")
        if isinstance(value, int | float):
            return int(value)
    total_size = payload.get("totalSize")
    if isinstance(total_size, int):
        return total_size
    return 0


class SalesforceSource:
    """Reads a Salesforce org as a Sanka migration source."""

    provider = "salesforce"
    binding_kind = "channel"

    def __init__(self, *, gateway: SalesforceGateway | None = None) -> None:
        self._gateway = gateway or HttpSalesforceGateway()

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        rows = await self._gateway.list_sobjects(credentials)
        options: list[SourceObject] = []
        for row in rows:
            key = str(row.get("name") or "").strip()
            if (
                not key
                or row.get("queryable") is not True
                or row.get("deprecatedAndHidden") is True
            ):
                continue
            options.append(
                SourceObject(
                    key=key,
                    label=str(row.get("label") or key),
                    canonical_type=_CANONICAL_OBJECTS.get(key.lower(), key.lower()),
                    default_selected=key in _DEFAULT_OBJECTS,
                    custom=bool(row.get("custom")) or key.endswith("__c"),
                )
            )
        return sorted(options, key=lambda item: (not item.default_selected, item.label.lower()))

    async def list_owners(self, credentials: Credentials) -> list[OwnerProfile]:
        users = await self._gateway.list_active_users(credentials)
        profiles: list[OwnerProfile] = []
        for user in users:
            owner_id = str(user.get("Id") or "").strip()
            email = str(user.get("Email") or "").strip()
            if not owner_id or not email:
                continue
            profiles.append(
                OwnerProfile(
                    id=owner_id,
                    email=email,
                    active=user.get("IsActive") is not False,
                    name=str(user.get("Name") or "").strip() or None,
                )
            )
        return profiles

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        requested_objects = object_types or list(_DEFAULT_OBJECTS)

        async def scan_object(
            raw_object_type: str,
        ) -> tuple[ObjectSchema | None, str | None]:
            object_type = _identifier(raw_object_type, label="object type")
            try:
                description = await self._gateway.describe_object(
                    credentials,
                    object_type=object_type,
                )
                count_payload = await self._gateway.query(
                    credentials,
                    soql=f"SELECT COUNT() FROM {object_type}",
                    query_all=True,
                )
            except ConnectorError as exc:
                return None, f"{object_type}: {exc}"
            raw_fields = description.get("fields")
            fields = (
                [
                    FieldSchema(
                        key=str(field.get("name") or ""),
                        label=str(field.get("label") or field.get("name") or ""),
                        data_type=str(field.get("type") or "string"),
                        required=field.get("nillable") is False,
                        writable=field.get("createable") is not False,
                        unique=bool(field.get("unique")),
                        metadata={
                            "calculated": bool(field.get("calculated")),
                            "referenceTo": field.get("referenceTo") or [],
                        },
                    )
                    for field in raw_fields
                    if isinstance(field, dict) and str(field.get("name") or "").strip()
                ]
                if isinstance(raw_fields, list)
                else []
            )
            return (
                ObjectSchema(
                    key=object_type,
                    label=str(description.get("label") or object_type),
                    canonical_type=_CANONICAL_OBJECTS.get(object_type.lower(), object_type.lower()),
                    record_count=_record_count(count_payload),
                    fields=fields,
                    identity_fields=["Id"],
                ),
                None,
            )

        results = await _bounded_map(requested_objects, scan_object)
        objects = [item for item, _warning in results if item is not None]
        warnings = [warning for _item, warning in results if warning]
        return Inventory(
            provider=self.provider,
            connection_id=credentials.connection_id,
            objects=objects,
            warnings=warnings,
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
        return await self._read_records(
            credentials,
            object_type=object_type,
            field_keys=field_keys,
            limit=limit,
            cursor=cursor,
            source_filter=source_filter,
            upper_bound=None,
        )

    # -- capabilities -------------------------------------------------------

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
        return await self._read_records(
            credentials,
            object_type=object_type,
            field_keys=field_keys,
            limit=limit,
            cursor=cursor,
            source_filter=source_filter,
            upper_bound=upper_bound,
        )

    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int:
        normalized_object = _identifier(object_type, label="object type")
        where_clause = ""
        if source_filter is not None:
            normalized_filter_field = _validated_filter_field(source_filter)
            filter_value = "true" if source_filter.value else "false"
            where_clause = f" WHERE {normalized_filter_field} = {filter_value}"
        payload = await self._gateway.query(
            credentials,
            soql=f"SELECT COUNT() FROM {normalized_object}{where_clause}",
            query_all=True,
        )
        return _record_count(payload)

    async def count_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> int:
        normalized_object = _identifier(object_type, label="object type")
        predicates: list[str] = []
        if source_filter is not None:
            normalized_filter_field = _validated_filter_field(source_filter)
            filter_value = "true" if source_filter.value else "false"
            predicates.append(f"{normalized_filter_field} = {filter_value}")
        normalized_upper_bound = str(upper_bound).strip()
        if not _CURSOR_RE.fullmatch(normalized_upper_bound):
            raise ValidationFailedError("Invalid Salesforce migration upper bound")
        predicates.append(f"Id <= '{normalized_upper_bound}'")
        payload = await self._gateway.query(
            credentials,
            soql=(f"SELECT COUNT() FROM {normalized_object} WHERE {' AND '.join(predicates)}"),
            query_all=True,
        )
        return _record_count(payload)

    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None:
        normalized_object = _identifier(object_type, label="object type")
        where_clause = ""
        if source_filter is not None:
            normalized_filter_field = _validated_filter_field(source_filter)
            filter_value = "true" if source_filter.value else "false"
            where_clause = f" WHERE {normalized_filter_field} = {filter_value}"
        payload = await self._gateway.query(
            credentials,
            soql=(f"SELECT Id FROM {normalized_object}{where_clause} ORDER BY Id DESC LIMIT 1"),
            query_all=True,
        )
        for record in payload.get("records") or []:
            record_id = str(record.get("Id") or "").strip() if isinstance(record, dict) else ""
            if record_id:
                return record_id
        return None

    # -- internals ----------------------------------------------------------

    async def _read_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None,
        source_filter: SourceFilter | None,
        upper_bound: str | None,
    ) -> RecordPage:
        normalized_object = _identifier(object_type, label="object type")
        normalized_fields = sorted(
            {
                _identifier(field, label="field")
                for field in field_keys
                if str(field or "").strip() and str(field).strip() != "Id"
            }
        )
        normalized_filter_field: str | None = None
        if source_filter is not None:
            normalized_filter_field = _validated_filter_field(source_filter)
            normalized_fields = sorted({*normalized_fields, normalized_filter_field})
        selected_fields = ["Id", *normalized_fields]
        safe_limit = max(1, min(int(limit or 100), 200))
        predicates: list[str] = []
        if source_filter is not None and normalized_filter_field is not None:
            filter_value = "true" if source_filter.value else "false"
            predicates.append(f"{normalized_filter_field} = {filter_value}")
        if cursor:
            normalized_cursor = str(cursor).strip()
            if not _CURSOR_RE.fullmatch(normalized_cursor):
                raise ValidationFailedError("Invalid Salesforce migration cursor")
            predicates.append(f"Id > '{normalized_cursor}'")
        if upper_bound:
            normalized_upper_bound = str(upper_bound).strip()
            if not _CURSOR_RE.fullmatch(normalized_upper_bound):
                raise ValidationFailedError("Invalid Salesforce migration upper bound")
            predicates.append(f"Id <= '{normalized_upper_bound}'")
        where_clause = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        soql = (
            f"SELECT {', '.join(selected_fields)} FROM {normalized_object}"
            f"{where_clause} ORDER BY Id ASC LIMIT {safe_limit}"
        )
        payload = await self._gateway.query(
            credentials,
            soql=soql,
            query_all=True,
        )
        records: list[dict[str, Any]] = [
            record
            for record in (payload.get("records") or [])
            if isinstance(record, dict) and str(record.get("Id") or "").strip()
        ]
        # Salesforce can split a query response before the SOQL LIMIT when a
        # wide projection reaches its response-size boundary. In that case the
        # first response contains fewer than ``safe_limit`` rows while
        # ``done`` is false and ``nextRecordsUrl`` is present. Sanka uses an Id
        # keyset cursor instead of Salesforce's opaque locator, so advancing
        # from the last returned Id remains deterministic across retries.
        provider_has_more = payload.get("done") is False or bool(payload.get("nextRecordsUrl"))
        has_more = bool(records) and (len(records) >= safe_limit or provider_has_more)
        next_cursor = str(records[-1].get("Id")) if has_more and records else None
        return RecordPage(
            object_key=normalized_object,
            records=records,
            next_cursor=next_cursor,
            has_more=has_more,
        )


def _validated_filter_field(source_filter: SourceFilter) -> str:
    """Validate a source filter and return its normalized field name."""
    if source_filter.operator != "equals":
        raise UnsupportedFeatureError("Unsupported Salesforce migration source filter")
    normalized_filter_field = _identifier(source_filter.field, label="filter field")
    if normalized_filter_field == "Id":
        raise ValidationFailedError("Salesforce boolean migration source filters cannot target Id")
    return normalized_filter_field


if TYPE_CHECKING:
    from sanka.connector import (
        SourceConnector,
        SupportsOwnerDirectory,
        SupportsRecordCounts,
        SupportsSnapshotBounds,
    )

    _protocol_source: SourceConnector = SalesforceSource()
    _protocol_counts: SupportsRecordCounts = SalesforceSource()
    _protocol_bounds: SupportsSnapshotBounds = SalesforceSource()
    _protocol_owners: SupportsOwnerDirectory = SalesforceSource()
