# SPDX-License-Identifier: Apache-2.0
"""Shared internals for the HubSpot connector.

The HTTP gateway (a faithful port of the production Sanka adapter's gateway
surface, reduced to the endpoints the connector uses), the HubSpot → Sanka Migrate
error mapping, and the helpers both roles share: canonical-type ↔ object-type
tables, custom-object schema matching, property-type mapping, and inventory
assembly.

Authentication is a bearer token in :attr:`sanka.connector.Credentials.access_token`
— a HubSpot private-app token, or an OAuth access token something else keeps
fresh. The connector never performs OAuth refresh itself: it is stateless per
call, and token refresh belongs to the runtime's credential provider
(:class:`sanka.connector.SupportsCredentialRefresh`).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any, Final
from urllib.parse import quote

import httpx

from sanka.connector import (
    AuthenticationError,
    ConflictError,
    ConnectorError,
    Credentials,
    ErrorCategory,
    FieldSchema,
    NotFoundError,
    ObjectSchema,
    PermissionDeniedError,
    ProviderTimeoutError,
    RateLimitError,
    TransientProviderError,
    ValidationFailedError,
)

HUBSPOT_API_BASE_URL: Final = "https://api.hubapi.com"
HUBSPOT_OWNERS_URL: Final = f"{HUBSPOT_API_BASE_URL}/crm/v3/owners/"
HUBSPOT_CRM_OBJECTS_URL: Final = f"{HUBSPOT_API_BASE_URL}/crm/v3/objects/{{object_type}}"
HUBSPOT_CRM_OBJECT_SEARCH_URL: Final = (
    f"{HUBSPOT_API_BASE_URL}/crm/v3/objects/{{object_type}}/search"
)
HUBSPOT_CRM_SCHEMAS_URL: Final = f"{HUBSPOT_API_BASE_URL}/crm/v3/schemas"
HUBSPOT_CRM_PROPERTIES_URL: Final = f"{HUBSPOT_API_BASE_URL}/crm/v3/properties/{{object_type}}"
HUBSPOT_CRM_PIPELINES_URL: Final = f"{HUBSPOT_API_BASE_URL}/crm/v3/pipelines/{{object_type}}"

HUBSPOT_BATCH_READ_LIMIT: Final = 100

#: Standard HubSpot object types by Sanka Migrate canonical type.
OBJECTS_BY_CANONICAL_TYPE: Final[dict[str, str]] = {
    "company": "companies",
    "contact": "contacts",
    "deal": "deals",
    "ticket": "tickets",
}
DEFAULT_CANONICAL_TYPES: Final = frozenset(OBJECTS_BY_CANONICAL_TYPE)

#: Natural identity fields per standard object type (unique properties from
#: the live schema are merged in during inventory).
IDENTITY_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "companies": ("domain",),
    "contacts": ("email",),
}

#: Default property groups for inline property creation on standard objects.
PROPERTY_DEFAULT_GROUPS: Final[dict[str, str]] = {
    "companies": "companyinformation",
    "contacts": "contactinformation",
    "deals": "dealinformation",
    "tickets": "ticketinformation",
}


class HubSpotRequestError(RuntimeError):
    """Internal transport-level failure raised by :class:`HubSpotGateway`.

    Carries the HTTP status and any ``Retry-After`` hint so the destination's
    provider-control loop can decide whether and how long to retry. Escaping
    instances are converted at each public SPI boundary with
    :func:`mapped_error`; the message format (``HubSpot returned HTTP
    {status}: {body}``) is load-bearing — the invalid-email fallback parses
    it.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def mapped_error(error: HubSpotRequestError) -> ConnectorError:
    """Map an internal gateway failure onto the Sanka Migrate error taxonomy."""
    message = str(error)
    status = error.status_code
    details: dict[str, Any] = {}
    if status is not None:
        details["statusCode"] = status
    cause = error.__cause__
    if isinstance(cause, httpx.TimeoutException):
        return ProviderTimeoutError(message, details=details)
    if isinstance(cause, httpx.TransportError):
        return TransientProviderError(message, details=details)
    if status == 401:
        return AuthenticationError(
            message,
            remediation="Check that the HubSpot access token is valid, unexpired, and unrevoked.",
            details=details,
        )
    if status == 403:
        return PermissionDeniedError(
            message,
            remediation=(
                "Grant the missing HubSpot scope to the private app "
                "(or reconnect with it) and retry."
            ),
            details=details,
        )
    if status == 404:
        return NotFoundError(message, details=details)
    if status == 408:
        return ProviderTimeoutError(message, details=details)
    if status == 409:
        return ConflictError(message, details=details)
    if status in {423, 429}:
        return RateLimitError(
            message,
            retry_after_seconds=error.retry_after_seconds,
            details=details,
        )
    if status is not None and status >= 500:
        return TransientProviderError(message, details=details)
    if status is not None:
        return ValidationFailedError(message, details=details)
    return ConnectorError(message, category=ErrorCategory.UNKNOWN, details=details)


@asynccontextmanager
async def hubspot_errors() -> AsyncIterator[None]:
    """Re-raise internal gateway failures as Sanka Migrate connector errors.

    Wraps only the outermost public SPI methods: the connector's internals
    (retry control, the invalid-email fallback, 409 reconciliation) inspect
    the raw :class:`HubSpotRequestError` and must see it unmapped.
    """
    try:
        yield
    except HubSpotRequestError as error:
        raise mapped_error(error) from error


def require_access_token(credentials: Credentials) -> str:
    token = str(credentials.access_token or "").strip()
    if not token:
        raise AuthenticationError(
            "HubSpot access token is missing",
            remediation=(
                "Provide a HubSpot private-app token (or a valid OAuth access token) "
                "in Credentials.access_token."
            ),
        )
    return token


async def bounded_map[InputT, OutputT](
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


class HubSpotGateway:
    """Thin async HTTP client for the HubSpot CRM v3/v4 endpoints Sanka Migrate uses.

    One :class:`httpx.AsyncClient` per request (the production-proven shape —
    no cross-call connection state), with an injectable transport so tests
    can use :class:`httpx.MockTransport`. Errors surface as
    :class:`HubSpotRequestError` with the status code and any ``Retry-After``
    hint attached.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def list_owners(
        self,
        credentials: Credentials,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        access_token = require_access_token(credentials)
        owners: list[dict[str, Any]] = []
        next_url: str | None = HUBSPOT_OWNERS_URL
        hop = 0
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            while next_url and hop < 20:
                hop += 1
                try:
                    response = await client.get(
                        next_url,
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Accept": "application/json",
                        },
                        params=(
                            {"limit": max(1, min(int(limit or 100), 100)), "archived": "false"}
                            if next_url == HUBSPOT_OWNERS_URL
                            else None
                        ),
                    )
                except httpx.HTTPError as exc:
                    raise HubSpotRequestError(str(exc)) from exc

                if response.status_code >= 400:
                    raise HubSpotRequestError(
                        _hubspot_error_message(response),
                        status_code=response.status_code,
                        retry_after_seconds=_retry_after_seconds(response),
                    )
                data = response.json()
                results = data.get("results", [])
                if isinstance(results, list):
                    owners.extend(row for row in results if isinstance(row, dict))
                next_url = data.get("paging", {}).get("next", {}).get("link")
        return owners

    async def search_crm_objects(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "POST",
            HUBSPOT_CRM_OBJECT_SEARCH_URL.format(object_type=object_type),
            json=payload,
        )

    async def create_crm_object(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "POST",
            HUBSPOT_CRM_OBJECTS_URL.format(object_type=object_type),
            json={"properties": properties},
        )

    async def update_crm_object(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        record_id: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "PATCH",
            f"{HUBSPOT_CRM_OBJECTS_URL.format(object_type=object_type)}/{record_id}",
            json={"properties": properties},
        )

    async def batch_create_crm_objects(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        inputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "POST",
            f"{HUBSPOT_CRM_OBJECTS_URL.format(object_type=object_type)}/batch/create",
            json={"inputs": inputs},
        )

    async def batch_update_crm_objects(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        updates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "POST",
            f"{HUBSPOT_CRM_OBJECTS_URL.format(object_type=object_type)}/batch/update",
            json={"inputs": updates},
        )

    async def batch_upsert_crm_objects(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        inputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "POST",
            f"{HUBSPOT_CRM_OBJECTS_URL.format(object_type=object_type)}/batch/upsert",
            json={"inputs": inputs},
        )

    async def batch_create_crm_associations(
        self,
        credentials: Credentials,
        *,
        from_object: str,
        to_object: str,
        inputs: list[dict[str, Any]],
        use_default: bool = False,
    ) -> dict[str, Any]:
        suffix = "batch/associate/default" if use_default else "batch/create"
        return await self._request_json(
            credentials,
            "POST",
            f"{HUBSPOT_API_BASE_URL}/crm/v4/associations/{from_object}/{to_object}/{suffix}",
            json={"inputs": inputs},
        )

    async def batch_read_crm_associations(
        self,
        credentials: Credentials,
        *,
        from_object: str,
        to_object: str,
        from_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        access_token = require_access_token(credentials)
        normalized_from_object = str(from_object or "").strip().strip("/")
        normalized_to_object = str(to_object or "").strip().strip("/")
        if not normalized_from_object or not normalized_to_object:
            raise HubSpotRequestError("HubSpot association object types are required")
        normalized_ids = _unique_ids(from_ids)
        if not normalized_ids:
            return {}

        results: dict[str, list[dict[str, Any]]] = {record_id: [] for record_id in normalized_ids}
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            for chunk in _chunks(normalized_ids, HUBSPOT_BATCH_READ_LIMIT):
                try:
                    response = await client.post(
                        (
                            f"{HUBSPOT_API_BASE_URL}/crm/v4/associations/"
                            f"{normalized_from_object}/{normalized_to_object}/batch/read"
                        ),
                        headers=_json_headers(access_token),
                        json={"inputs": [{"id": record_id} for record_id in chunk]},
                    )
                except httpx.HTTPError as exc:
                    raise HubSpotRequestError(str(exc)) from exc
                if response.status_code >= 400:
                    raise HubSpotRequestError(
                        _hubspot_error_message(response),
                        status_code=response.status_code,
                        retry_after_seconds=_retry_after_seconds(response),
                    )
                for source_id, targets in _batch_association_results(response.json()).items():
                    results.setdefault(source_id, []).extend(targets)
        return results

    async def list_crm_properties(
        self,
        credentials: Credentials,
        *,
        object_type: str,
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "GET",
            HUBSPOT_CRM_PROPERTIES_URL.format(object_type=object_type),
            params={"archived": "false"},
        )

    async def create_crm_property(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "POST",
            HUBSPOT_CRM_PROPERTIES_URL.format(object_type=object_type),
            json=payload,
        )

    async def list_crm_schemas(
        self,
        credentials: Credentials,
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "GET",
            HUBSPOT_CRM_SCHEMAS_URL,
            params={"archived": "false"},
        )

    async def get_crm_schema(
        self,
        credentials: Credentials,
        *,
        object_type: str,
    ) -> dict[str, Any]:
        encoded_object_type = quote(str(object_type or ""), safe="")
        return await self._request_json(
            credentials,
            "GET",
            f"{HUBSPOT_CRM_SCHEMAS_URL}/{encoded_object_type}",
        )

    async def create_crm_schema(
        self,
        credentials: Credentials,
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "POST",
            HUBSPOT_CRM_SCHEMAS_URL,
            json=payload,
        )

    async def list_pipelines(
        self,
        credentials: Credentials,
        *,
        object_type: str,
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "GET",
            HUBSPOT_CRM_PIPELINES_URL.format(object_type=object_type),
            params={"archived": "false"},
        )

    async def create_pipeline(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request_json(
            credentials,
            "POST",
            HUBSPOT_CRM_PIPELINES_URL.format(object_type=object_type),
            json=payload,
        )

    async def _request_json(
        self,
        credentials: Credentials,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        access_token = require_access_token(credentials)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers=_json_headers(access_token),
                    json=json,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise HubSpotRequestError(str(exc)) from exc

        if response.status_code >= 400:
            raise HubSpotRequestError(
                _hubspot_error_message(response),
                status_code=response.status_code,
                retry_after_seconds=_retry_after_seconds(response),
            )
        if response.status_code == 204 or not response.content:
            return {"ok": True, "status_code": response.status_code}
        data = response.json()
        if isinstance(data, dict):
            data.setdefault("ok", True)
            data.setdefault("status_code", response.status_code)
            return data
        return {"ok": True, "status_code": response.status_code, "response": data}


def _json_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _hubspot_error_message(response: httpx.Response) -> str:
    text = response.text.strip()
    if response.status_code == 403 and (
        "INSUFFICIENT_SCOPES" in text or "insufficient" in text.lower()
    ):
        return (
            "Missing HubSpot scope. Reconnect HubSpot with the required automation, CRM, "
            "or marketing scope for this operation."
        )
    if text:
        return f"HubSpot returned HTTP {response.status_code}: {text[:500]}"
    return f"HubSpot returned HTTP {response.status_code}"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = str(response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _unique_ids(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _chunks(values: list[str], size: int) -> list[list[str]]:
    chunk_size = max(1, int(size or 1))
    return [values[index : index + chunk_size] for index in range(0, len(values), chunk_size)]


def _batch_association_results(payload: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    results = payload.get("results")
    if not isinstance(results, list):
        return {}
    for result in results:
        if not isinstance(result, dict):
            continue
        from_id = (
            ((result.get("from") or {}).get("id") if isinstance(result.get("from"), dict) else None)
            or result.get("fromObjectId")
            or result.get("id")
        )
        source_id = str(from_id or "").strip()
        if not source_id:
            continue
        targets = result.get("to")
        if not isinstance(targets, list):
            targets = result.get("results")
        if not isinstance(targets, list):
            targets = []
        normalized[source_id] = [
            target
            for target in (_normalize_batch_association_target(target) for target in targets)
            if target is not None
        ]
    return normalized


def _normalize_batch_association_target(target: Any) -> dict[str, Any] | None:
    if not isinstance(target, dict):
        return None
    target_id = (
        target.get("toObjectId")
        or target.get("id")
        or ((target.get("to") or {}).get("id") if isinstance(target.get("to"), dict) else None)
    )
    if not target_id:
        return None
    association_types = list(target.get("associationTypes") or [])
    labels = [
        str(item.get("label") or "").strip()
        for item in association_types
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    ]
    return {
        "id": str(target_id),
        "labels": labels,
        "association_types": association_types,
    }


# --------------------------------------------------------------------------
# Schema helpers shared by both roles
# --------------------------------------------------------------------------


def custom_object_match_token(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.endswith("__c"):
        normalized = normalized[:-3]
    return re.sub(r"[^a-z0-9]+", "", normalized)


def custom_schema_matches(
    schemas: list[dict[str, Any]],
    canonical_type: str,
) -> list[dict[str, Any]]:
    requested = custom_object_match_token(canonical_type)
    if not requested:
        return []
    matches: list[dict[str, Any]] = []
    for schema in schemas:
        raw_labels = schema.get("labels")
        labels: dict[str, Any] = raw_labels if isinstance(raw_labels, dict) else {}
        tokens = {
            custom_object_match_token(schema.get("name")),
            custom_object_match_token(labels.get("singular")),
            custom_object_match_token(labels.get("plural")),
        }
        if requested in tokens and str(schema.get("objectTypeId") or "").strip():
            matches.append(schema)
    return matches


def hubspot_property_type(source_type: str | None) -> tuple[str, str]:
    """Map a source field type onto a HubSpot property ``(type, fieldType)``."""
    normalized = str(source_type or "").strip().lower()
    if normalized in {"bool", "boolean", "checkbox"}:
        return "bool", "booleancheckbox"
    if normalized in {
        "currency",
        "decimal",
        "double",
        "int",
        "integer",
        "long",
        "number",
        "percent",
    }:
        return "number", "number"
    if normalized == "date":
        return "date", "date"
    if normalized in {"datetime", "timestamp"}:
        return "datetime", "date"
    if normalized in {"textarea", "longtextarea", "long_text"}:
        return "string", "textarea"
    if normalized in {"phone", "phonenumber"}:
        return "string", "phonenumber"
    # Picklist values are not included in the Sanka Migrate inventory yet. Preserve the
    # source value as text instead of creating an empty HubSpot enumeration.
    return "string", "text"


def boolean_property_options() -> list[dict[str, Any]]:
    return [
        {
            "label": "True",
            "value": "true",
            "displayOrder": 0,
            "hidden": False,
        },
        {
            "label": "False",
            "value": "false",
            "displayOrder": 1,
            "hidden": False,
        },
    ]


def writable_property(property_row: dict[str, Any]) -> bool:
    modification = property_row.get("modificationMetadata")
    if isinstance(modification, dict) and modification.get("readOnlyValue") is True:
        return False
    return property_row.get("calculated") is not True and property_row.get("hidden") is not True


async def inventory_object(
    gateway: HubSpotGateway,
    credentials: Credentials,
    *,
    object_type: str,
    canonical_type: str,
    label: str,
) -> ObjectSchema:
    properties_payload = await gateway.list_crm_properties(
        credentials,
        object_type=object_type,
    )
    count_payload = await gateway.search_crm_objects(
        credentials,
        object_type=object_type,
        payload={"limit": 1, "properties": []},
    )
    raw_properties = properties_payload.get("results")
    fields = (
        [
            FieldSchema(
                key=str(item.get("name") or ""),
                label=str(item.get("label") or item.get("name") or ""),
                data_type=str(item.get("type") or item.get("fieldType") or "string"),
                writable=writable_property(item),
                unique=bool(item.get("hasUniqueValue")),
                metadata={"fieldType": item.get("fieldType")},
            )
            for item in raw_properties
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if isinstance(raw_properties, list)
        else []
    )
    return ObjectSchema(
        key=object_type,
        label=label,
        canonical_type=canonical_type,
        record_count=int(count_payload.get("total") or 0),
        fields=fields,
        identity_fields=sorted(
            {
                *IDENTITY_FIELDS.get(object_type, ()),
                *(field.key for field in fields if field.unique),
            }
        ),
    )
