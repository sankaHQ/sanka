# SPDX-License-Identifier: Apache-2.0
"""NetSuite source connector.

Reads a NetSuite account through SuiteQL over a fixed ERP object registry:
customers, vendors, inventory items, sales orders, and invoices. Entities and
items map to their own SuiteQL tables; sales orders and invoices share the
``transaction`` table, discriminated by its ``type`` column (``SalesOrd`` /
``CustInvc``) — exactly the documented SuiteQL data model. Column names are
the lowercase SuiteQL projections, and identity is always ``id`` (NetSuite's
numeric internal id).

``discover_objects`` returns the fixed registry (no metadata-catalog call);
``inventory`` runs ``SELECT COUNT(*)`` per object — concurrently, capped at 4
in-flight calls — with per-object failures becoming inventory warnings.
``read_records`` keyset-paginates on ``id`` (``WHERE [type = '…' AND]
[id > cursor] [AND id <= bound] ORDER BY id``, page size clamped to 1-1000
and enforced through SuiteQL's ``limit`` parameter), so retries resume
deterministically from the last returned id.

Capabilities: exact counts (:class:`sanka.connector.SupportsRecordCounts`)
and snapshot bounds on the maximum ``id``
(:class:`sanka.connector.SupportsSnapshotBounds`). Source filters are not
supported yet and raise :class:`sanka.connector.UnsupportedFeatureError`.
Object keys, field keys, cursors, and bounds are validated against strict
character classes before they are interpolated into SuiteQL — invalid input
raises :class:`sanka.connector.ValidationFailedError` instead of reaching the
account.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sanka.connector import (
    ConnectorError,
    Credentials,
    FieldSchema,
    Inventory,
    ObjectSchema,
    RecordPage,
    SourceFilter,
    SourceObject,
    UnsupportedFeatureError,
    ValidationFailedError,
)
from sanka_connector_netsuite._gateway import HttpNetSuiteGateway, NetSuiteGateway

_CURSOR_RE = re.compile(r"^[0-9]{1,18}$")


@dataclass(frozen=True, slots=True, kw_only=True)
class _ObjectDefinition:
    key: str
    label: str
    table: str
    canonical_type: str
    default_selected: bool
    type_predicate: str | None
    fields: tuple[tuple[str, str, str, bool], ...]  # key, label, data_type, required


_COMMON_ENTITY_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("id", "Internal ID", "text", True),
    ("entityid", "Entity ID", "text", True),
    ("companyname", "Company Name", "text", False),
    ("email", "Email", "email", False),
    ("phone", "Phone", "phone", False),
    ("balance", "Balance", "currency", False),
    ("subsidiary", "Subsidiary", "reference", False),
    ("currency", "Currency", "reference", False),
    ("isinactive", "Inactive", "checkbox", False),
)

_TRANSACTION_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("id", "Internal ID", "text", True),
    ("tranid", "Document Number", "text", True),
    ("entity", "Entity", "reference", False),
    ("trandate", "Date", "date", False),
    ("status", "Status", "text", False),
    ("foreigntotal", "Amount", "currency", False),
    ("currency", "Currency", "reference", False),
    ("memo", "Memo", "text", False),
)

_OBJECTS: dict[str, _ObjectDefinition] = {
    definition.key: definition
    for definition in (
        _ObjectDefinition(
            key="customer",
            label="Customers",
            table="customer",
            canonical_type="company",
            default_selected=True,
            type_predicate=None,
            fields=_COMMON_ENTITY_FIELDS,
        ),
        _ObjectDefinition(
            key="vendor",
            label="Vendors",
            table="vendor",
            canonical_type="vendor",
            default_selected=False,
            type_predicate=None,
            fields=_COMMON_ENTITY_FIELDS,
        ),
        _ObjectDefinition(
            key="inventoryItem",
            label="Inventory Items",
            table="item",
            canonical_type="item",
            default_selected=False,
            type_predicate=None,
            fields=(
                ("id", "Internal ID", "text", True),
                ("itemid", "Item ID", "text", True),
                ("displayname", "Display Name", "text", False),
                ("baseprice", "Base Price", "currency", False),
                ("quantityavailable", "Quantity Available", "float", False),
                ("isinactive", "Inactive", "checkbox", False),
            ),
        ),
        _ObjectDefinition(
            key="salesOrder",
            label="Sales Orders",
            table="transaction",
            canonical_type="salesorder",
            default_selected=False,
            type_predicate="type = 'SalesOrd'",
            fields=_TRANSACTION_FIELDS,
        ),
        _ObjectDefinition(
            key="invoice",
            label="Invoices",
            table="transaction",
            canonical_type="invoice",
            default_selected=False,
            type_predicate="type = 'CustInvc'",
            fields=(
                *_TRANSACTION_FIELDS,
                ("duedate", "Due Date", "date", False),
                ("amountremaining", "Amount Remaining", "currency", False),
            ),
        ),
    )
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


def _definition(object_type: str) -> _ObjectDefinition:
    normalized = str(object_type or "").strip()
    definition = _OBJECTS.get(normalized)
    if definition is None:
        known = ", ".join(sorted(_OBJECTS))
        raise ValidationFailedError(
            f"Unknown NetSuite object type: {normalized or 'missing'} (expected one of {known})"
        )
    return definition


def _validated_cursor(value: str, *, label: str) -> str:
    normalized = str(value).strip()
    if not _CURSOR_RE.fullmatch(normalized):
        raise ValidationFailedError(f"Invalid NetSuite migration {label}")
    return normalized


def _count_value(payload: dict[str, Any]) -> int:
    items = payload.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        for value in items[0].values():
            if isinstance(value, int | float):
                return int(value)
    total_results = payload.get("totalResults")
    if isinstance(total_results, int):
        return total_results
    return 0


def _rejected_filter(source_filter: SourceFilter | None) -> None:
    if source_filter is not None:
        raise UnsupportedFeatureError("NetSuite migration source filters are not supported yet")


class NetSuiteSource:
    """Reads a NetSuite account as a Sanka migration source."""

    provider = "netsuite"
    binding_kind = "channel"

    def __init__(self, *, gateway: NetSuiteGateway | None = None) -> None:
        self._gateway = gateway or HttpNetSuiteGateway()

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        del credentials  # The demo-scoped object registry is fixed.
        options = [
            SourceObject(
                key=definition.key,
                label=definition.label,
                canonical_type=definition.canonical_type,
                default_selected=definition.default_selected,
                custom=False,
            )
            for definition in _OBJECTS.values()
        ]
        return sorted(options, key=lambda item: (not item.default_selected, item.label.lower()))

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        requested_objects = object_types or list(_OBJECTS)

        async def scan_object(
            raw_object_type: str,
        ) -> tuple[ObjectSchema | None, str | None]:
            try:
                definition = _definition(raw_object_type)
                payload = await self._gateway.suiteql(
                    credentials,
                    query=self._count_query(definition, upper_bound=None),
                    limit=1,
                )
            except (ConnectorError, ValidationFailedError) as exc:
                return None, f"{raw_object_type}: {exc}"
            return (
                ObjectSchema(
                    key=definition.key,
                    label=definition.label,
                    canonical_type=definition.canonical_type,
                    record_count=_count_value(payload),
                    fields=[
                        FieldSchema(
                            key=key,
                            label=label,
                            data_type=data_type,
                            required=required,
                            writable=False,
                            unique=key == "id",
                            metadata={"table": definition.table},
                        )
                        for key, label, data_type, required in definition.fields
                    ],
                    identity_fields=["id"],
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
        _rejected_filter(source_filter)
        definition = _definition(object_type)
        payload = await self._gateway.suiteql(
            credentials,
            query=self._count_query(definition, upper_bound=None),
            limit=1,
        )
        return _count_value(payload)

    async def count_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> int:
        _rejected_filter(source_filter)
        definition = _definition(object_type)
        normalized_bound = _validated_cursor(upper_bound, label="upper bound")
        payload = await self._gateway.suiteql(
            credentials,
            query=self._count_query(definition, upper_bound=normalized_bound),
            limit=1,
        )
        return _count_value(payload)

    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None:
        _rejected_filter(source_filter)
        definition = _definition(object_type)
        where_clause = self._where_clause(definition, cursor=None, upper_bound=None)
        payload = await self._gateway.suiteql(
            credentials,
            query=f"SELECT id FROM {definition.table}{where_clause} ORDER BY id DESC",
            limit=1,
        )
        for record in payload.get("items") or []:
            record_id = str(record.get("id") or "").strip() if isinstance(record, dict) else ""
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
        _rejected_filter(source_filter)
        definition = _definition(object_type)
        known_fields = {key for key, _label, _data_type, _required in definition.fields}
        requested = {str(field or "").strip() for field in field_keys} - {"", "id"}
        unknown = sorted(requested - known_fields)
        if unknown:
            raise ValidationFailedError(
                f"Unknown NetSuite {definition.key} field(s): {', '.join(unknown)}"
            )
        selected_fields = ["id", *sorted(requested)]
        safe_limit = max(1, min(int(limit or 100), 1000))
        normalized_cursor = _validated_cursor(cursor, label="cursor") if cursor else None
        normalized_bound = (
            _validated_cursor(upper_bound, label="upper bound") if upper_bound else None
        )
        where_clause = self._where_clause(
            definition,
            cursor=normalized_cursor,
            upper_bound=normalized_bound,
        )
        query = (
            f"SELECT {', '.join(selected_fields)} FROM {definition.table}{where_clause} ORDER BY id"
        )
        payload = await self._gateway.suiteql(credentials, query=query, limit=safe_limit)
        records: list[dict[str, Any]] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict) or not str(item.get("id") or "").strip():
                continue
            # The SuiteQL envelope decorates every row with a ``links`` list;
            # it is transport metadata, not source data.
            records.append({key: value for key, value in item.items() if key != "links"})
        provider_has_more = payload.get("hasMore") is True
        has_more = bool(records) and (len(records) >= safe_limit or provider_has_more)
        next_cursor = str(records[-1].get("id")) if has_more and records else None
        return RecordPage(
            object_key=definition.key,
            records=records,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _count_query(self, definition: _ObjectDefinition, *, upper_bound: str | None) -> str:
        where_clause = self._where_clause(definition, cursor=None, upper_bound=upper_bound)
        return f"SELECT COUNT(*) FROM {definition.table}{where_clause}"

    def _where_clause(
        self,
        definition: _ObjectDefinition,
        *,
        cursor: str | None,
        upper_bound: str | None,
    ) -> str:
        predicates: list[str] = []
        if definition.type_predicate is not None:
            predicates.append(definition.type_predicate)
        if cursor:
            predicates.append(f"id > {cursor}")
        if upper_bound:
            predicates.append(f"id <= {upper_bound}")
        return f" WHERE {' AND '.join(predicates)}" if predicates else ""


if TYPE_CHECKING:
    from sanka.connector import (
        SourceConnector,
        SupportsRecordCounts,
        SupportsSnapshotBounds,
    )

    _protocol_source: SourceConnector = NetSuiteSource()
    _protocol_counts: SupportsRecordCounts = NetSuiteSource()
    _protocol_bounds: SupportsSnapshotBounds = NetSuiteSource()
