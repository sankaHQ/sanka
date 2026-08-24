# SPDX-License-Identifier: Apache-2.0
"""SendGrid Marketing Contacts source with export-bound resumability."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sanka.connector import (
    Credentials,
    DataError,
    FieldSchema,
    Inventory,
    Limits,
    ObjectSchema,
    ProviderTimeoutError,
    RecordPage,
    SourceFilter,
    SourceObject,
    UnsupportedFeatureError,
    ValidationFailedError,
)
from sanka_connector_sendgrid._gateway import HttpSendGridGateway, SendGridGateway

CONTACTS_OBJECT = "contacts"
CONTACT_FIELDS = (
    FieldSchema(key="id", label="SendGrid ID", required=True, writable=False, unique=True),
    FieldSchema(key="email", label="Email", required=True, unique=True),
    FieldSchema(key="first_name", label="First name"),
    FieldSchema(key="last_name", label="Last name"),
    FieldSchema(key="phone_number_id", label="Phone number ID"),
    FieldSchema(key="external_id", label="External ID", unique=True),
    FieldSchema(key="anonymous_id", label="Anonymous ID", unique=True),
    FieldSchema(key="alternate_emails", label="Alternate emails", data_type="array"),
    FieldSchema(key="address_line_1", label="Address line 1"),
    FieldSchema(key="address_line_2", label="Address line 2"),
    FieldSchema(key="city", label="City"),
    FieldSchema(key="state_province_region", label="State / province / region"),
    FieldSchema(key="postal_code", label="Postal code"),
    FieldSchema(key="country", label="Country"),
    FieldSchema(key="custom_fields", label="Custom fields", data_type="json"),
    FieldSchema(key="created_at", label="Created at", data_type="datetime"),
    FieldSchema(key="updated_at", label="Updated at", data_type="datetime"),
)


class SendGridSource:
    """Reads a stable contact export and resumes by export job plus offset."""

    provider = "sendgrid"
    binding_kind = "channel"

    def __init__(
        self,
        *,
        gateway: SendGridGateway | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        export_timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._gateway = gateway or HttpSendGridGateway()
        self._sleep = sleep
        self._monotonic = monotonic
        self._export_timeout_seconds = max(1.0, export_timeout_seconds)
        self._poll_interval_seconds = max(0.0, poll_interval_seconds)

    def limits(self) -> Limits:
        return Limits(max_read_page_size=1000, min_request_interval_ms=100)

    async def validate(self, credentials: Credentials) -> list[str]:
        await self._gateway.get_contact_summary(credentials)
        return []

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        await self._gateway.get_contact_summary(credentials)
        return [
            SourceObject(
                key=CONTACTS_OBJECT,
                label="Marketing contacts",
                canonical_type="contact",
                default_selected=True,
                automatic_target_object="contacts",
            )
        ]

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        requested = object_types or [CONTACTS_OBJECT]
        if requested != [CONTACTS_OBJECT]:
            invalid = next((value for value in requested if value != CONTACTS_OBJECT), requested[0])
            raise ValidationFailedError(f"Unknown SendGrid object type: {invalid}")
        summary = await self._gateway.get_contact_summary(credentials)
        return Inventory(
            provider=self.provider,
            connection_id=credentials.connection_id,
            objects=[
                ObjectSchema(
                    key=CONTACTS_OBJECT,
                    label="Marketing contacts",
                    canonical_type="contact",
                    record_count=_contact_count(summary),
                    fields=list(CONTACT_FIELDS),
                    identity_fields=["id"],
                )
            ],
        )

    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int:
        _validate_request(object_type, source_filter)
        return _contact_count(await self._gateway.get_contact_summary(credentials))

    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None:
        _validate_request(object_type, source_filter)
        export_id = await self._gateway.create_contact_export(credentials)
        await self._wait_until_ready(credentials, export_id)
        return export_id

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
        _validate_request(object_type, source_filter)
        if cursor:
            export_id, offset = _decode_cursor(cursor)
        else:
            export_id = await self._gateway.create_contact_export(credentials)
            await self._wait_until_ready(credentials, export_id)
            offset = 0
        return await self._page(
            credentials,
            export_id=export_id,
            offset=offset,
            field_keys=field_keys,
            limit=limit,
            bounded=False,
        )

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
        _validate_request(object_type, source_filter)
        try:
            offset = max(0, int(cursor or 0))
        except ValueError as exc:
            raise ValidationFailedError("Invalid SendGrid bounded-read cursor") from exc
        return await self._page(
            credentials,
            export_id=upper_bound,
            offset=offset,
            field_keys=field_keys,
            limit=limit,
            bounded=True,
        )

    async def count_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> int:
        _validate_request(object_type, source_filter)
        return len(await self._export_records(credentials, upper_bound))

    async def _page(
        self,
        credentials: Credentials,
        *,
        export_id: str,
        offset: int,
        field_keys: list[str],
        limit: int,
        bounded: bool,
    ) -> RecordPage:
        records = await self._export_records(credentials, export_id)
        page_size = max(1, min(int(limit or 100), 1000))
        selected = records[offset : offset + page_size]
        requested = {key for key in field_keys if key}
        requested.add("id")
        selected = [
            {key: value for key, value in row.items() if key in requested} for row in selected
        ]
        next_offset = offset + len(selected)
        has_more = next_offset < len(records)
        next_cursor = None
        if has_more:
            next_cursor = str(next_offset) if bounded else _encode_cursor(export_id, next_offset)
        return RecordPage(
            object_key=CONTACTS_OBJECT,
            records=selected,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def _export_records(
        self,
        credentials: Credentials,
        export_id: str,
    ) -> list[dict[str, Any]]:
        status = await self._wait_until_ready(credentials, export_id)
        urls = [str(url) for url in status.get("urls") or [] if str(url).strip()]
        if not urls:
            raise DataError(f"SendGrid export {export_id!r} is ready without download URLs")
        records = await self._gateway.download_contact_export(credentials, urls=urls)
        complete = [record for record in records if str(record.get("id") or "").strip()]
        return sorted(complete, key=lambda row: (str(row.get("id")), str(row.get("email"))))

    async def _wait_until_ready(
        self,
        credentials: Credentials,
        export_id: str,
    ) -> dict[str, Any]:
        deadline = self._monotonic() + self._export_timeout_seconds
        while True:
            payload = await self._gateway.get_contact_export(
                credentials,
                export_id=export_id,
            )
            status = str(payload.get("status") or "").lower()
            if status == "ready":
                return payload
            if status == "failure":
                message = str(payload.get("message") or "unknown provider failure")
                raise DataError(f"SendGrid export {export_id!r} failed: {message}")
            if status != "pending":
                raise DataError(
                    f"SendGrid export {export_id!r} returned unexpected status {status!r}"
                )
            if self._monotonic() >= deadline:
                raise ProviderTimeoutError(f"SendGrid export {export_id!r} did not become ready")
            await self._sleep(self._poll_interval_seconds)


def _contact_count(payload: dict[str, Any]) -> int:
    value = payload.get("contact_count")
    return int(value) if isinstance(value, int | float) else 0


def _validate_request(object_type: str, source_filter: SourceFilter | None) -> None:
    if object_type != CONTACTS_OBJECT:
        raise ValidationFailedError(f"Unknown SendGrid object type: {object_type}")
    if source_filter is not None:
        raise UnsupportedFeatureError(
            "SendGrid source filters are not supported; use a reviewed full contact export."
        )


def _encode_cursor(export_id: str, offset: int) -> str:
    raw = json.dumps({"export": export_id, "offset": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        export_id = str(payload["export"])
        offset = int(payload["offset"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationFailedError("Invalid SendGrid cursor") from exc
    if not export_id or offset < 0:
        raise ValidationFailedError("Invalid SendGrid cursor")
    return export_id, offset
