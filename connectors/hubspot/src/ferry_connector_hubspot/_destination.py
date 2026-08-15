# SPDX-License-Identifier: Apache-2.0
"""HubSpot destination connector.

A faithful port of the production Sanka HubSpot Ferry adapter: conflict
policies via identity search, 100-record batch create/update/upsert with
``objectWriteTraceId`` reconciliation, v4 association batches, inline
property / pipeline / custom-object provisioning with a pure dry run, the
invalid-email fallback for contacts, and a single-lock provider-control loop
(min-interval pacing, five attempts, ``Retry-After``-aware backoff capped at
30 seconds, retry metrics).

Signature adaptations versus the production adapter (semantics preserved):

- The SPI bundles ``conflict_policy`` / ``identity_fields`` /
  ``invalid_email_policy`` / ``invalid_email_audit_field`` into
  :class:`ferry.connector.WriteOptions` instead of separate keyword
  arguments.
- :class:`ferry.connector.PipelineStage` carries no per-stage probability, so
  created deal-pipeline stages get a deterministic linear probability ramp
  (first stage ``0.0`` … last stage ``1.0``) — HubSpot requires one — and
  compatibility checks ignore probability. Adjust probabilities in HubSpot
  after provisioning if they matter.
- :class:`ferry.connector.CustomObjectDefinition` carries no property list,
  so created custom-object schemas contain exactly one text property (the
  primary display property, which HubSpot requires); compatibility checks
  labels, the primary display property, and associated objects.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

import httpx

from ferry.connector import (
    BatchRelationshipWriteResult,
    BatchWriteInput,
    BatchWriteResult,
    ConflictPolicy,
    Credentials,
    CustomObjectDefinition,
    DataError,
    InvalidEmailPolicy,
    Inventory,
    OwnerProfile,
    PipelineDefinition,
    PropertyDefinition,
    PropertyResult,
    RelationshipWrite,
    RelationshipWriteResult,
    ResourceResult,
    TransientProviderError,
    ValidationFailedError,
    WriteOptions,
    WriteResult,
)
from ferry_connector_hubspot._base import (
    DEFAULT_CANONICAL_TYPES,
    IDENTITY_FIELDS,
    OBJECTS_BY_CANONICAL_TYPE,
    PROPERTY_DEFAULT_GROUPS,
    HubSpotGateway,
    HubSpotRequestError,
    boolean_property_options,
    bounded_map,
    custom_schema_matches,
    hubspot_errors,
    hubspot_property_type,
    inventory_object,
)

_HUBSPOT_OBJECT_BATCH_SIZE = 100
_HUBSPOT_IDENTITY_SEARCH_SIZE = 50
_HUBSPOT_ASSOCIATION_BATCH_SIZE = 100


def _existing_property_result(
    definition: PropertyDefinition,
    existing: dict[str, Any],
) -> PropertyResult:
    expected_type, expected_field_type = hubspot_property_type(definition.source_type)
    actual_type = str(existing.get("type") or "string")
    actual_field_type = str(existing.get("fieldType") or "text")
    # HubSpot fieldType controls presentation, while type controls stored-value
    # validation. In particular, an enumeration is not interchangeable with a
    # free-form string because its values must already exist as options.
    compatible = actual_type == expected_type
    return PropertyResult(
        source_field=definition.source_field,
        target_object=definition.target_object,
        internal_name=definition.internal_name,
        label=str(existing.get("label") or definition.label),
        source_type=definition.source_type,
        target_type=actual_type,
        field_type=actual_field_type,
        status="existing" if compatible else "conflict",
        message=(
            None
            if compatible
            else (
                f"Existing HubSpot property type is {actual_type}/{actual_field_type}; "
                f"the source requires {expected_type}/{expected_field_type}."
            )
        ),
    )


def _stage_probability(index: int, count: int) -> float:
    """Deterministic linear ramp used because the SDK carries no probability."""
    if count <= 1:
        return 1.0
    return round(index / (count - 1), 4)


def _pipeline_payload(definition: PipelineDefinition) -> dict[str, Any]:
    count = len(definition.stages)
    return {
        "label": definition.label,
        "displayOrder": definition.display_order,
        "stages": [
            {
                "label": stage.label,
                "displayOrder": stage.display_order,
                "metadata": {"probability": str(float(_stage_probability(index, count)))},
            }
            for index, stage in enumerate(definition.stages)
        ],
    }


def _pipeline_stage_ids(
    definition: PipelineDefinition,
    provider_pipeline: dict[str, Any],
) -> dict[str, str]:
    raw_stages = provider_pipeline.get("stages")
    stages = [item for item in raw_stages or [] if isinstance(item, dict)]
    by_label = {
        str(item.get("label") or "").strip().casefold(): str(item.get("id") or "").strip()
        for item in stages
        if str(item.get("label") or "").strip() and str(item.get("id") or "").strip()
    }
    return {
        stage.key: by_label[stage.label.casefold()]
        for stage in definition.stages
        if stage.label.casefold() in by_label
    }


def _pipeline_is_compatible(
    definition: PipelineDefinition,
    provider_pipeline: dict[str, Any],
) -> bool:
    # Stage probabilities are not part of the SDK definition, so unlike the
    # production adapter this compatibility check ignores them.
    raw_stages = provider_pipeline.get("stages")
    provider_stages = [item for item in raw_stages or [] if isinstance(item, dict)]
    if len(provider_stages) != len(definition.stages):
        return False
    by_label = {str(item.get("label") or "").strip().casefold(): item for item in provider_stages}
    for stage in definition.stages:
        provider_stage = by_label.get(stage.label.casefold())
        if provider_stage is None:
            return False
        if int(provider_stage.get("displayOrder") or 0) != stage.display_order:
            return False
    return True


def _custom_object_schema_payload(
    definition: CustomObjectDefinition,
) -> dict[str, Any]:
    # The SDK definition carries no property list, so the created schema holds
    # exactly one text property: the primary display property HubSpot requires.
    # Additional properties are provisioned separately or created in HubSpot.
    display_label = (
        definition.primary_display_property.replace("_", " ").strip().title()
        or definition.primary_display_property
    )
    return {
        "name": definition.internal_name,
        "labels": {
            "singular": definition.singular_label,
            "plural": definition.plural_label,
        },
        "primaryDisplayProperty": definition.primary_display_property,
        "associatedObjects": definition.associated_objects,
        "properties": [
            {
                "name": definition.primary_display_property,
                "label": display_label,
                "type": "string",
                "fieldType": "text",
                "hasUniqueValue": False,
                "displayOrder": 0,
                "description": f"Migrated from {definition.source_object} with Ferry.",
            }
        ],
        "requiredProperties": [],
        "searchableProperties": [],
        "secondaryDisplayProperties": [],
    }


def _custom_object_schema_is_compatible(
    definition: CustomObjectDefinition,
    provider_schema: dict[str, Any],
) -> bool:
    labels = provider_schema.get("labels")
    if not isinstance(labels, dict):
        return False
    if str(labels.get("singular") or "").strip() != definition.singular_label:
        return False
    if str(labels.get("plural") or "").strip() != definition.plural_label:
        return False
    if (
        str(provider_schema.get("primaryDisplayProperty") or "").strip()
        != definition.primary_display_property
    ):
        return False
    raw_properties = provider_schema.get("properties")
    provider_properties = {
        str(item.get("name") or "").strip()
        for item in raw_properties or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    if definition.primary_display_property not in provider_properties:
        return False
    associated_objects = provider_schema.get("associatedObjects")
    return not (
        isinstance(associated_objects, list)
        and set(associated_objects) != set(definition.associated_objects)
    )


def _contact_email_fallback_kind(error: HubSpotRequestError) -> str | None:
    message = str(error).strip()
    if error.status_code == 400 and "INVALID_EMAIL" in message.upper():
        return "invalid"
    if error.status_code in {400, 409}:
        provider_message = message
        response_prefix = f"HubSpot returned HTTP {error.status_code}:"
        if message.startswith(response_prefix):
            try:
                payload = json.loads(message.removeprefix(response_prefix).strip())
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                provider_message = str(payload.get("message") or "").strip()
        if provider_message.casefold() == "contact already exists":
            return "duplicate"
    return None


def _contact_email_fallback_properties(
    *,
    error: HubSpotRequestError,
    object_type: str,
    properties: dict[str, Any],
    identity_fields: list[str],
    policy: InvalidEmailPolicy,
    audit_field: str | None,
) -> dict[str, Any] | None:
    if (
        policy != "leave_empty"
        or object_type != "contacts"
        or _contact_email_fallback_kind(error) is None
    ):
        return None
    normalized_audit_field = str(audit_field or "").strip()
    if not normalized_audit_field or normalized_audit_field == "email":
        raise ValidationFailedError(
            "An audit property is required before Ferry can omit an invalid HubSpot email.",
            details={"code": "FERRY_INVALID_EMAIL_AUDIT_FIELD_REQUIRED"},
        ) from error
    if normalized_audit_field in identity_fields:
        raise ValidationFailedError(
            "The email audit property must not overwrite a reviewed identity field.",
            details={
                "code": "FERRY_INVALID_EMAIL_AUDIT_FIELD_IDENTITY_CONFLICT",
                "objectType": object_type,
                "auditField": normalized_audit_field,
            },
        ) from error
    email_value = properties.get("email")
    if email_value in {None, ""}:
        return None
    alternate_identity_fields = [
        field
        for field in identity_fields
        if field != "email" and properties.get(field) not in {None, ""}
    ]
    if not alternate_identity_fields:
        raise ValidationFailedError(
            "An alternate identity is required before Ferry can omit an invalid HubSpot email.",
            details={
                "code": "FERRY_INVALID_EMAIL_ALTERNATE_IDENTITY_REQUIRED",
                "objectType": object_type,
            },
        ) from error
    fallback = dict(properties)
    fallback.pop("email", None)
    fallback[normalized_audit_field] = email_value
    return fallback


def _contact_email_fallback_message(
    *,
    kind: str,
    status: str,
) -> str:
    if kind == "duplicate":
        return (
            "HubSpot email uniqueness conflict: Ferry matched an existing record with an "
            "alternate identity and left it unchanged."
            if status == "skipped"
            else (
                "HubSpot email uniqueness conflict: the standard email was omitted and "
                "preserved in the configured audit property."
            )
        )
    return (
        "HubSpot rejected the standard email value; Ferry matched an existing record "
        "with an alternate identity and left it unchanged."
        if status == "skipped"
        else (
            "HubSpot rejected the standard email value; Ferry preserved it in the "
            "configured audit property and continued with an alternate identity."
        )
    )


def _batch_error_trace_ids(error: dict[str, Any]) -> list[str]:
    context = error.get("context")
    if not isinstance(context, dict):
        return []
    raw = context.get("objectWriteTraceId")
    values = raw if isinstance(raw, list) else [raw]
    return [str(value).strip() for value in values if str(value or "").strip()]


def _batch_write_outcomes(
    payload: dict[str, Any],
    *,
    records: list[BatchWriteInput],
    success_status: Literal["created", "updated"],
    destination_ids: dict[str, str] | None = None,
    match_fields: Sequence[str] | None = None,
    derive_new_status: bool = False,
) -> dict[str, BatchWriteResult]:
    records_by_trace = {record.trace_id: record for record in records}
    outcomes: dict[str, BatchWriteResult] = {}
    errors = payload.get("errors")
    for error in errors if isinstance(errors, list) else []:
        if not isinstance(error, dict):
            continue
        message = str(error.get("message") or "HubSpot batch write failed")[:500]
        for trace_id in _batch_error_trace_ids(error):
            if trace_id in records_by_trace:
                outcomes[trace_id] = BatchWriteResult(
                    trace_id=trace_id,
                    status="failed",
                    message=message,
                )

    unmatched_traces = [record.trace_id for record in records if record.trace_id not in outcomes]
    rows = payload.get("results")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        trace_id = str(row.get("objectWriteTraceId") or "").strip()
        row_destination_id = str(row.get("id") or "").strip()
        if trace_id not in records_by_trace and row_destination_id and destination_ids:
            destination_matches = [
                candidate
                for candidate in unmatched_traces
                if destination_ids.get(candidate) == row_destination_id
            ]
            if len(destination_matches) == 1:
                trace_id = destination_matches[0]
        if trace_id not in records_by_trace:
            properties = row.get("properties")
            if isinstance(properties, dict) and match_fields:
                candidates: list[str] = []
                for candidate in unmatched_traces:
                    source_properties = records_by_trace[candidate].properties
                    comparable = [
                        key
                        for key in match_fields
                        if key in properties and key in source_properties
                    ]
                    if comparable and all(
                        source_properties[key] == properties[key] for key in comparable
                    ):
                        candidates.append(candidate)
                if len(candidates) == 1:
                    trace_id = candidates[0]
        if trace_id not in records_by_trace or trace_id in outcomes:
            continue
        destination_record_id = str(
            row_destination_id or (destination_ids or {}).get(trace_id) or ""
        ).strip()
        if not destination_record_id:
            outcomes[trace_id] = BatchWriteResult(
                trace_id=trace_id,
                status="failed",
                message="HubSpot batch response did not include a destination record id.",
            )
            continue
        status: Literal["created", "updated", "skipped", "failed"] = success_status
        if derive_new_status:
            status = "created" if row.get("new") is True else "updated"
        outcomes[trace_id] = BatchWriteResult(
            trace_id=trace_id,
            status=status,
            destination_record_id=destination_record_id,
        )
        unmatched_traces = [candidate for candidate in unmatched_traces if candidate != trace_id]

    for trace_id in records_by_trace:
        outcomes.setdefault(
            trace_id,
            BatchWriteResult(
                trace_id=trace_id,
                status="failed",
                message="HubSpot batch response did not account for this record.",
            ),
        )
    return outcomes


class HubSpotDestination:
    provider = "hubspot"
    binding_kind = "channel"

    def __init__(
        self,
        *,
        gateway: HubSpotGateway | None = None,
        min_interval_seconds: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gateway = gateway or HubSpotGateway()
        self._min_interval_seconds = max(0.0, min_interval_seconds)
        self._sleep = sleep
        self._monotonic = monotonic
        self._next_request_at = 0.0
        self._request_lock = asyncio.Lock()
        self._metrics: dict[str, Any] = {
            "requests": 0,
            "retries": 0,
            "rateLimitRetries": 0,
            "throttleWaitMs": 0,
            "lastRetryAt": None,
        }

    def automatic_target_object(self, canonical_type: str) -> str | None:
        return OBJECTS_BY_CANONICAL_TYPE.get(canonical_type)

    async def inventory(
        self,
        credentials: Credentials,
        *,
        canonical_types: set[str],
    ) -> Inventory:
        async with hubspot_errors():
            warnings: list[str] = []
            requested_types = canonical_types or DEFAULT_CANONICAL_TYPES
            scan_requests: list[tuple[str, str, str]] = []
            unmanaged_types: list[str] = []
            for canonical_type in sorted(requested_types):
                object_type = OBJECTS_BY_CANONICAL_TYPE.get(canonical_type)
                if not object_type:
                    unmanaged_types.append(canonical_type)
                    continue
                scan_requests.append((canonical_type, object_type, object_type))
            if unmanaged_types:
                schema_payload = await self._with_provider_control(
                    lambda: self._gateway.list_crm_schemas(credentials),
                    retry_transient=True,
                )
                raw_schemas = schema_payload.get("results")
                schemas = [item for item in raw_schemas or [] if isinstance(item, dict)]
                for canonical_type in unmanaged_types:
                    matches = custom_schema_matches(schemas, canonical_type)
                    if len(matches) == 1:
                        schema = matches[0]
                        raw_labels = schema.get("labels")
                        labels: dict[str, Any] = raw_labels if isinstance(raw_labels, dict) else {}
                        scan_requests.append(
                            (
                                canonical_type,
                                str(schema.get("objectTypeId") or "").strip(),
                                str(labels.get("singular") or schema.get("name") or canonical_type),
                            )
                        )
                        continue
                    if len(matches) > 1:
                        warnings.append(
                            f"HubSpot custom object mapping for {canonical_type} is ambiguous."
                        )
                        continue
                    warnings.append(
                        f"HubSpot does not have a default {canonical_type} object mapping."
                    )
            objects = await bounded_map(
                scan_requests,
                lambda item: inventory_object(
                    self._gateway,
                    credentials,
                    object_type=item[1],
                    canonical_type=item[0],
                    label=item[2],
                ),
            )
            return Inventory(
                provider=self.provider,
                connection_id=credentials.connection_id,
                objects=objects,
                warnings=warnings,
            )

    async def reconcile_resources(
        self,
        credentials: Credentials,
        *,
        pipelines: list[PipelineDefinition],
        custom_objects: list[CustomObjectDefinition],
        confirm: bool,
    ) -> list[ResourceResult]:
        async with hubspot_errors():
            return await self._reconcile_resources(
                credentials,
                pipelines=pipelines,
                custom_objects=custom_objects,
                confirm=confirm,
            )

    async def _reconcile_resources(
        self,
        credentials: Credentials,
        *,
        pipelines: list[PipelineDefinition],
        custom_objects: list[CustomObjectDefinition],
        confirm: bool,
    ) -> list[ResourceResult]:
        results: list[ResourceResult] = []
        pipelines_by_object: dict[str, list[PipelineDefinition]] = defaultdict(list)
        for definition in pipelines:
            pipelines_by_object[definition.object_type].append(definition)
        for object_type, definitions in pipelines_by_object.items():
            payload = await self._with_provider_control(
                partial(
                    self._gateway.list_pipelines,
                    credentials,
                    object_type=object_type,
                ),
                retry_transient=True,
            )
            raw_pipelines = payload.get("results")
            provider_pipelines = [item for item in raw_pipelines or [] if isinstance(item, dict)]
            for definition in definitions:
                matches = [
                    item
                    for item in provider_pipelines
                    if str(item.get("label") or "").strip().casefold()
                    == definition.label.casefold()
                ]
                if len(matches) > 1:
                    results.append(
                        ResourceResult(
                            resource_type="pipeline",
                            key=definition.key,
                            label=definition.label,
                            status="conflict",
                            message="Multiple destination pipelines use the requested label.",
                        )
                    )
                    continue
                if matches:
                    existing = matches[0]
                    provider_id = str(existing.get("id") or "").strip() or None
                    results.append(
                        ResourceResult(
                            resource_type="pipeline",
                            key=definition.key,
                            label=definition.label,
                            status=(
                                "existing"
                                if _pipeline_is_compatible(definition, existing)
                                else "conflict"
                            ),
                            provider_id=provider_id,
                            stage_ids=_pipeline_stage_ids(definition, existing),
                            message=(
                                None
                                if _pipeline_is_compatible(definition, existing)
                                else "The existing pipeline stages do not match the reviewed plan."
                            ),
                        )
                    )
                    continue
                if not confirm:
                    results.append(
                        ResourceResult(
                            resource_type="pipeline",
                            key=definition.key,
                            label=definition.label,
                            status="would_create",
                        )
                    )
                    continue
                try:
                    created = await self._with_provider_control(
                        partial(
                            self._gateway.create_pipeline,
                            credentials,
                            object_type=definition.object_type,
                            payload=_pipeline_payload(definition),
                        ),
                        retry_transient=True,
                    )
                except HubSpotRequestError as exc:
                    results.append(
                        ResourceResult(
                            resource_type="pipeline",
                            key=definition.key,
                            label=definition.label,
                            status="failed",
                            message=(
                                "HubSpot pipeline creation failed "
                                f"(HTTP {exc.status_code or 'unknown'})."
                            ),
                        )
                    )
                    continue
                provider_id_created = str(created.get("id") or "").strip()
                stage_ids = _pipeline_stage_ids(definition, created)
                if not provider_id_created or len(stage_ids) != len(definition.stages):
                    results.append(
                        ResourceResult(
                            resource_type="pipeline",
                            key=definition.key,
                            label=definition.label,
                            status="failed",
                            provider_id=provider_id_created or None,
                            stage_ids=stage_ids,
                            message=(
                                "HubSpot did not return the created pipeline and every stage ID."
                            ),
                        )
                    )
                    continue
                results.append(
                    ResourceResult(
                        resource_type="pipeline",
                        key=definition.key,
                        label=definition.label,
                        status="created",
                        provider_id=provider_id_created,
                        stage_ids=stage_ids,
                    )
                )

        if custom_objects:
            schema_payload = await self._with_provider_control(
                lambda: self._gateway.list_crm_schemas(credentials),
                retry_transient=True,
            )
            raw_schemas = schema_payload.get("results")
            schemas = [item for item in raw_schemas or [] if isinstance(item, dict)]
            for custom_definition in custom_objects:
                matches = [
                    item
                    for item in schemas
                    if str(item.get("name") or "").strip().casefold()
                    == custom_definition.internal_name.casefold()
                ]
                if len(matches) > 1:
                    results.append(
                        ResourceResult(
                            resource_type="custom_object",
                            key=custom_definition.key,
                            label=custom_definition.singular_label,
                            status="conflict",
                            message=(
                                "Multiple custom object schemas use the requested internal name."
                            ),
                        )
                    )
                    continue
                if matches:
                    provider_id = str(matches[0].get("objectTypeId") or "").strip()
                    existing_schema = await self._with_provider_control(
                        partial(
                            self._gateway.get_crm_schema,
                            credentials,
                            object_type=provider_id,
                        ),
                        retry_transient=True,
                    )
                    compatible = _custom_object_schema_is_compatible(
                        custom_definition, existing_schema
                    )
                    results.append(
                        ResourceResult(
                            resource_type="custom_object",
                            key=custom_definition.key,
                            label=custom_definition.singular_label,
                            status="existing" if compatible else "conflict",
                            provider_id=provider_id or None,
                            message=(
                                None
                                if compatible
                                else (
                                    "The existing custom object schema does not match "
                                    "the reviewed plan."
                                )
                            ),
                        )
                    )
                    continue
                if not confirm:
                    results.append(
                        ResourceResult(
                            resource_type="custom_object",
                            key=custom_definition.key,
                            label=custom_definition.singular_label,
                            status="would_create",
                        )
                    )
                    continue
                try:
                    created = await self._with_provider_control(
                        partial(
                            self._gateway.create_crm_schema,
                            credentials,
                            payload=_custom_object_schema_payload(custom_definition),
                        ),
                        retry_transient=True,
                    )
                except HubSpotRequestError as exc:
                    results.append(
                        ResourceResult(
                            resource_type="custom_object",
                            key=custom_definition.key,
                            label=custom_definition.singular_label,
                            status="failed",
                            message=(
                                "HubSpot custom object schema creation failed "
                                f"(HTTP {exc.status_code or 'unknown'})."
                            ),
                        )
                    )
                    continue
                created_provider_id = str(created.get("objectTypeId") or "").strip()
                results.append(
                    ResourceResult(
                        resource_type="custom_object",
                        key=custom_definition.key,
                        label=custom_definition.singular_label,
                        status="created" if created_provider_id else "failed",
                        provider_id=created_provider_id or None,
                        message=(
                            None
                            if created_provider_id
                            else "HubSpot did not return the created custom object type ID."
                        ),
                    )
                )
        return results

    async def reconcile_properties(
        self,
        credentials: Credentials,
        *,
        definitions: list[PropertyDefinition],
        confirm: bool,
    ) -> list[PropertyResult]:
        async with hubspot_errors():
            return await self._reconcile_properties(
                credentials,
                definitions=definitions,
                confirm=confirm,
            )

    async def _reconcile_properties(
        self,
        credentials: Credentials,
        *,
        definitions: list[PropertyDefinition],
        confirm: bool,
    ) -> list[PropertyResult]:
        definitions_by_object: dict[str, list[tuple[int, PropertyDefinition]]] = {}
        results: list[PropertyResult | None] = [None] * len(definitions)
        for index, definition in enumerate(definitions):
            if definition.target_object not in PROPERTY_DEFAULT_GROUPS:
                results[index] = PropertyResult(
                    source_field=definition.source_field,
                    target_object=definition.target_object,
                    internal_name=definition.internal_name,
                    label=definition.label,
                    source_type=definition.source_type,
                    status="unsupported",
                    message="Inline property creation currently supports standard HubSpot objects.",
                )
                continue
            definitions_by_object.setdefault(definition.target_object, []).append(
                (index, definition)
            )

        for object_type, indexed_definitions in definitions_by_object.items():
            payload = await self._with_provider_control(
                partial(
                    self._gateway.list_crm_properties,
                    credentials,
                    object_type=object_type,
                ),
                retry_transient=True,
            )
            raw_properties = payload.get("results")
            existing_by_name = {
                str(item.get("name") or "").strip().lower(): item
                for item in raw_properties or []
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            }
            requested_name_counts: dict[str, int] = {}
            for _, definition in indexed_definitions:
                requested_name_counts[definition.internal_name] = (
                    requested_name_counts.get(definition.internal_name, 0) + 1
                )
            for index, definition in indexed_definitions:
                if requested_name_counts[definition.internal_name] > 1:
                    results[index] = PropertyResult(
                        source_field=definition.source_field,
                        target_object=definition.target_object,
                        internal_name=definition.internal_name,
                        label=definition.label,
                        source_type=definition.source_type,
                        status="conflict",
                        message=(
                            "Another requested source field uses the same HubSpot internal name."
                        ),
                    )
                    continue
                existing = existing_by_name.get(definition.internal_name)
                if existing is not None:
                    results[index] = _existing_property_result(
                        definition,
                        existing,
                    )
                    continue
                target_type, field_type = hubspot_property_type(definition.source_type)
                if not confirm:
                    results[index] = PropertyResult(
                        source_field=definition.source_field,
                        target_object=definition.target_object,
                        internal_name=definition.internal_name,
                        label=definition.label,
                        source_type=definition.source_type,
                        target_type=target_type,
                        field_type=field_type,
                        status="would_create",
                    )
                    continue
                property_payload = {
                    "groupName": PROPERTY_DEFAULT_GROUPS[object_type],
                    "name": definition.internal_name,
                    "label": definition.label,
                    "type": target_type,
                    "fieldType": field_type,
                    "description": f"Migrated from {definition.source_field} with Ferry.",
                    "hidden": False,
                }
                if target_type == "bool":
                    property_payload["options"] = boolean_property_options()
                try:
                    created = await self._with_provider_control(
                        partial(
                            self._gateway.create_crm_property,
                            credentials,
                            object_type=object_type,
                            payload=property_payload,
                        ),
                        retry_transient=True,
                    )
                except HubSpotRequestError as exc:
                    results[index] = PropertyResult(
                        source_field=definition.source_field,
                        target_object=definition.target_object,
                        internal_name=definition.internal_name,
                        label=definition.label,
                        source_type=definition.source_type,
                        target_type=target_type,
                        field_type=field_type,
                        status="failed",
                        message=(
                            "HubSpot property creation failed "
                            f"(HTTP {exc.status_code or 'unknown'})."
                        ),
                    )
                    continue
                existing_by_name[definition.internal_name] = created
                results[index] = PropertyResult(
                    source_field=definition.source_field,
                    target_object=definition.target_object,
                    internal_name=str(created.get("name") or definition.internal_name),
                    label=str(created.get("label") or definition.label),
                    source_type=definition.source_type,
                    target_type=str(created.get("type") or target_type),
                    field_type=str(created.get("fieldType") or field_type),
                    status="created",
                )

        return [result for result in results if result is not None]

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        async with hubspot_errors():
            return await self._write_record_with_policies(
                credentials,
                object_type=object_type,
                properties=properties,
                conflict_policy=options.conflict_policy,
                identity_fields=options.identity_fields,
                invalid_email_policy=options.invalid_email_policy,
                invalid_email_audit_field=options.invalid_email_audit_field,
            )

    async def _write_record_with_policies(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        conflict_policy: ConflictPolicy,
        identity_fields: list[str] | None = None,
        invalid_email_policy: InvalidEmailPolicy = "block",
        invalid_email_audit_field: str | None = None,
    ) -> WriteResult:
        effective_identity_fields = list(
            dict.fromkeys(identity_fields or IDENTITY_FIELDS.get(object_type, ()))
        )
        is_custom_object = object_type not in OBJECTS_BY_CANONICAL_TYPE.values()
        if conflict_policy != "create" and is_custom_object and not effective_identity_fields:
            raise ValidationFailedError(
                "A custom HubSpot object route requires an explicit identity field "
                "before existing records can be handled safely.",
                details={
                    "code": "FERRY_DESTINATION_IDENTITY_REQUIRED",
                    "objectType": object_type,
                },
            )
        if (
            conflict_policy != "create"
            and is_custom_object
            and not any(
                properties.get(field) not in {None, ""} for field in effective_identity_fields
            )
        ):
            raise ValidationFailedError(
                "A custom HubSpot object record requires a non-empty identity value "
                "before existing records can be handled safely.",
                details={
                    "code": "FERRY_DESTINATION_IDENTITY_VALUE_REQUIRED",
                    "objectType": object_type,
                    "identityFields": effective_identity_fields,
                },
            )
        retry_transient = conflict_policy in {"skip_existing", "update_existing"} and any(
            properties.get(field) not in {None, ""} for field in effective_identity_fields
        )
        try:
            return await self._with_provider_control(
                lambda: self._write_record_once(
                    credentials,
                    object_type=object_type,
                    properties=properties,
                    conflict_policy=conflict_policy,
                    identity_fields=effective_identity_fields,
                ),
                retry_transient=retry_transient,
            )
        except HubSpotRequestError as exc:
            fallback_kind = _contact_email_fallback_kind(exc)
            fallback_properties = _contact_email_fallback_properties(
                error=exc,
                object_type=object_type,
                properties=properties,
                identity_fields=effective_identity_fields,
                policy=invalid_email_policy,
                audit_field=invalid_email_audit_field,
            )
            if fallback_properties is None:
                raise
            result = await self._with_provider_control(
                lambda: self._write_record_once(
                    credentials,
                    object_type=object_type,
                    properties=fallback_properties,
                    conflict_policy=conflict_policy,
                    identity_fields=effective_identity_fields,
                ),
                retry_transient=True,
            )
            message = _contact_email_fallback_message(
                kind=str(fallback_kind),
                status=result.status,
            )
            return replace(result, message=message)

    async def write_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[BatchWriteInput],
        options: WriteOptions,
    ) -> list[BatchWriteResult]:
        async with hubspot_errors():
            return await self._write_records_with_policies(
                credentials,
                object_type=object_type,
                records=records,
                conflict_policy=options.conflict_policy,
                identity_fields=options.identity_fields,
                invalid_email_policy=options.invalid_email_policy,
                invalid_email_audit_field=options.invalid_email_audit_field,
            )

    async def _write_records_with_policies(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[BatchWriteInput],
        conflict_policy: ConflictPolicy,
        identity_fields: list[str] | None = None,
        invalid_email_policy: InvalidEmailPolicy = "block",
        invalid_email_audit_field: str | None = None,
    ) -> list[BatchWriteResult]:
        if not records:
            return []
        trace_ids = [record.trace_id for record in records]
        if len(set(trace_ids)) != len(trace_ids):
            raise ValidationFailedError(
                "Ferry destination batch trace ids must be unique.",
                details={"code": "FERRY_DESTINATION_BATCH_TRACE_DUPLICATE"},
            )
        effective_identity_fields = list(
            dict.fromkeys(identity_fields or IDENTITY_FIELDS.get(object_type, ()))
        )
        outcomes: dict[str, BatchWriteResult] = {}
        valid_records: list[BatchWriteInput] = []
        for record in records:
            try:
                self._validate_record_identity(
                    object_type=object_type,
                    properties=record.properties,
                    conflict_policy=conflict_policy,
                    identity_fields=effective_identity_fields,
                )
            except Exception as exc:
                outcomes[record.trace_id] = BatchWriteResult(
                    trace_id=record.trace_id,
                    status="failed",
                    message=str(exc)[:500],
                )
            else:
                valid_records.append(record)

        if conflict_policy == "update_existing" and object_type == "contacts":
            email_upserts = [
                record
                for record in valid_records
                if "email" in effective_identity_fields
                and record.properties.get("email") not in {None, ""}
            ]
            if email_upserts:
                outcomes.update(
                    await self._batch_upsert_by_identity(
                        credentials,
                        object_type=object_type,
                        records=email_upserts,
                        identity_field="email",
                        invalid_email_policy=invalid_email_policy,
                        invalid_email_audit_field=invalid_email_audit_field,
                        identity_fields=effective_identity_fields,
                    )
                )
                handled = {record.trace_id for record in email_upserts}
                valid_records = [
                    record for record in valid_records if record.trace_id not in handled
                ]

        existing_by_trace: dict[str, str] = {}
        if conflict_policy != "create" and valid_records:
            existing_by_trace = await self._find_existing_records(
                credentials,
                object_type=object_type,
                records=valid_records,
                identity_fields=effective_identity_fields,
            )
        pending_create: list[BatchWriteInput] = []
        pending_update: list[tuple[BatchWriteInput, str]] = []
        for record in valid_records:
            existing_id = existing_by_trace.get(record.trace_id)
            if existing_id and conflict_policy == "skip_existing":
                outcomes[record.trace_id] = BatchWriteResult(
                    trace_id=record.trace_id,
                    status="skipped",
                    destination_record_id=existing_id,
                    message="Existing destination record matched an identity field.",
                )
            elif existing_id and conflict_policy == "update_existing":
                pending_update.append((record, existing_id))
            else:
                pending_create.append(record)

        if pending_update:
            outcomes.update(
                await self._batch_update_records(
                    credentials,
                    object_type=object_type,
                    records=pending_update,
                )
            )
        if pending_create:
            outcomes.update(
                await self._batch_create_records(
                    credentials,
                    object_type=object_type,
                    records=pending_create,
                    conflict_policy=conflict_policy,
                    identity_fields=effective_identity_fields,
                    invalid_email_policy=invalid_email_policy,
                    invalid_email_audit_field=invalid_email_audit_field,
                )
            )
        return [
            outcomes.get(record.trace_id)
            or BatchWriteResult(
                trace_id=record.trace_id,
                status="failed",
                message="HubSpot batch response did not account for this record.",
            )
            for record in records
        ]

    async def _write_record_once(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        conflict_policy: ConflictPolicy,
        identity_fields: list[str],
    ) -> WriteResult:
        existing_id = await self._find_existing_record(
            credentials,
            object_type=object_type,
            properties=properties,
            identity_fields=identity_fields,
        )
        if existing_id and conflict_policy == "skip_existing":
            return WriteResult(
                status="skipped",
                destination_record_id=existing_id,
                message="Existing destination record matched an identity field.",
            )
        if existing_id and conflict_policy == "update_existing":
            await self._gateway.update_crm_object(
                credentials,
                object_type=object_type,
                record_id=existing_id,
                properties=properties,
            )
            return WriteResult(
                status="updated",
                destination_record_id=existing_id,
            )
        response = await self._gateway.create_crm_object(
            credentials,
            object_type=object_type,
            properties=properties,
        )
        destination_record_id = str(response.get("id") or "").strip()
        if not destination_record_id:
            raise DataError(
                "HubSpot did not return an id for the created record.",
                details={"code": "FERRY_DESTINATION_RECORD_ID_MISSING"},
            )
        return WriteResult(
            status="created",
            destination_record_id=destination_record_id,
        )

    def _validate_record_identity(
        self,
        *,
        object_type: str,
        properties: dict[str, Any],
        conflict_policy: ConflictPolicy,
        identity_fields: list[str],
    ) -> None:
        if conflict_policy == "create" or object_type in OBJECTS_BY_CANONICAL_TYPE.values():
            return
        if not identity_fields:
            raise ValidationFailedError(
                "A custom HubSpot object route requires an explicit identity field "
                "before existing records can be handled safely.",
                details={
                    "code": "FERRY_DESTINATION_IDENTITY_REQUIRED",
                    "objectType": object_type,
                },
            )
        if not any(properties.get(field) not in {None, ""} for field in identity_fields):
            raise ValidationFailedError(
                "A custom HubSpot object record requires a non-empty identity value "
                "before existing records can be handled safely.",
                details={
                    "code": "FERRY_DESTINATION_IDENTITY_VALUE_REQUIRED",
                    "objectType": object_type,
                    "identityFields": identity_fields,
                },
            )

    async def _find_existing_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[BatchWriteInput],
        identity_fields: list[str],
    ) -> dict[str, str]:
        matches: dict[str, str] = {}
        for field in identity_fields:
            traces_by_value: dict[str, list[str]] = defaultdict(list)
            for record in records:
                if record.trace_id in matches:
                    continue
                value = record.properties.get(field)
                if value not in {None, ""}:
                    traces_by_value[str(value)].append(record.trace_id)
            values = list(traces_by_value)
            for index in range(0, len(values), _HUBSPOT_IDENTITY_SEARCH_SIZE):
                chunk = values[index : index + _HUBSPOT_IDENTITY_SEARCH_SIZE]
                search_payload: dict[str, Any] = {
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": field,
                                    "operator": "IN",
                                    "values": chunk,
                                }
                            ]
                        }
                    ],
                    "limit": 200,
                    "properties": [field],
                }
                payload = await self._with_provider_control(
                    partial(
                        self._gateway.search_crm_objects,
                        credentials,
                        object_type=object_type,
                        payload=search_payload,
                    ),
                    retry_transient=True,
                )
                rows = payload.get("results")
                for row in rows if isinstance(rows, list) else []:
                    if not isinstance(row, dict):
                        continue
                    destination_id = str(row.get("id") or "").strip()
                    properties = row.get("properties")
                    value = (
                        str(properties.get(field) or "").strip()
                        if isinstance(properties, dict)
                        else ""
                    )
                    if not destination_id or value not in traces_by_value:
                        continue
                    for trace_id in traces_by_value[value]:
                        matches.setdefault(trace_id, destination_id)
        return matches

    async def _batch_create_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[BatchWriteInput],
        conflict_policy: ConflictPolicy,
        identity_fields: list[str],
        invalid_email_policy: InvalidEmailPolicy,
        invalid_email_audit_field: str | None,
    ) -> dict[str, BatchWriteResult]:
        outcomes: dict[str, BatchWriteResult] = {}
        for index in range(0, len(records), _HUBSPOT_OBJECT_BATCH_SIZE):
            chunk = records[index : index + _HUBSPOT_OBJECT_BATCH_SIZE]
            retry_transient = conflict_policy != "create" and all(
                any(record.properties.get(field) not in {None, ""} for field in identity_fields)
                for record in chunk
            )
            try:
                payload = await self._with_provider_control(
                    partial(
                        self._gateway.batch_create_crm_objects,
                        credentials,
                        object_type=object_type,
                        inputs=[
                            {
                                "objectWriteTraceId": record.trace_id,
                                "properties": record.properties,
                            }
                            for record in chunk
                        ],
                    ),
                    retry_transient=retry_transient,
                )
            except HubSpotRequestError as exc:
                if exc.status_code != 409 or conflict_policy == "create":
                    raise
                reconciled = await self._find_existing_records(
                    credentials,
                    object_type=object_type,
                    records=chunk,
                    identity_fields=identity_fields,
                )
                for record in chunk:
                    destination_id = reconciled.get(record.trace_id)
                    if destination_id:
                        if conflict_policy == "update_existing":
                            await self._with_provider_control(
                                partial(
                                    self._gateway.update_crm_object,
                                    credentials,
                                    object_type=object_type,
                                    record_id=destination_id,
                                    properties=record.properties,
                                ),
                                retry_transient=True,
                            )
                        outcomes[record.trace_id] = BatchWriteResult(
                            trace_id=record.trace_id,
                            status="skipped" if conflict_policy == "skip_existing" else "updated",
                            destination_record_id=destination_id,
                            message="A concurrent HubSpot create was reconciled by identity.",
                        )
                        continue
                    try:
                        result = await self._write_record_with_policies(
                            credentials,
                            object_type=object_type,
                            properties=record.properties,
                            conflict_policy=conflict_policy,
                            identity_fields=identity_fields,
                            invalid_email_policy=invalid_email_policy,
                            invalid_email_audit_field=invalid_email_audit_field,
                        )
                    except Exception as record_exc:
                        outcomes[record.trace_id] = BatchWriteResult(
                            trace_id=record.trace_id,
                            status="failed",
                            message=str(record_exc)[:500],
                        )
                    else:
                        outcomes[record.trace_id] = BatchWriteResult(
                            trace_id=record.trace_id,
                            status=result.status,
                            destination_record_id=result.destination_record_id,
                            message=result.message,
                        )
                continue
            chunk_outcomes = _batch_write_outcomes(
                payload,
                records=chunk,
                success_status="created",
                match_fields=identity_fields,
            )
            for record in chunk:
                outcome = chunk_outcomes.get(record.trace_id)
                if outcome is None or outcome.status != "failed":
                    if outcome is not None:
                        outcomes[record.trace_id] = outcome
                    continue
                fallback_error = HubSpotRequestError(
                    outcome.message or "HubSpot batch create failed",
                    status_code=400,
                )
                fallback_kind = _contact_email_fallback_kind(fallback_error)
                fallback_properties = _contact_email_fallback_properties(
                    error=fallback_error,
                    object_type=object_type,
                    properties=record.properties,
                    identity_fields=identity_fields,
                    policy=invalid_email_policy,
                    audit_field=invalid_email_audit_field,
                )
                if fallback_properties is None:
                    outcomes[record.trace_id] = outcome
                    continue
                try:
                    fallback_result = await self._with_provider_control(
                        partial(
                            self._write_record_once,
                            credentials,
                            object_type=object_type,
                            properties=fallback_properties,
                            conflict_policy=conflict_policy,
                            identity_fields=identity_fields,
                        ),
                        retry_transient=True,
                    )
                except Exception as exc:
                    outcomes[record.trace_id] = BatchWriteResult(
                        trace_id=record.trace_id,
                        status="failed",
                        message=str(exc)[:500],
                    )
                else:
                    outcomes[record.trace_id] = BatchWriteResult(
                        trace_id=record.trace_id,
                        status=fallback_result.status,
                        destination_record_id=fallback_result.destination_record_id,
                        message=_contact_email_fallback_message(
                            kind=str(fallback_kind),
                            status=fallback_result.status,
                        ),
                    )
        return outcomes

    async def _batch_update_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[tuple[BatchWriteInput, str]],
    ) -> dict[str, BatchWriteResult]:
        outcomes: dict[str, BatchWriteResult] = {}
        for index in range(0, len(records), _HUBSPOT_OBJECT_BATCH_SIZE):
            chunk = records[index : index + _HUBSPOT_OBJECT_BATCH_SIZE]
            payload = await self._with_provider_control(
                partial(
                    self._gateway.batch_update_crm_objects,
                    credentials,
                    object_type=object_type,
                    updates=[
                        {
                            "id": destination_id,
                            "objectWriteTraceId": record.trace_id,
                            "properties": record.properties,
                        }
                        for record, destination_id in chunk
                    ],
                ),
                retry_transient=True,
            )
            outcomes.update(
                _batch_write_outcomes(
                    payload,
                    records=[record for record, _destination_id in chunk],
                    success_status="updated",
                    destination_ids={
                        record.trace_id: destination_id for record, destination_id in chunk
                    },
                )
            )
        return outcomes

    async def _batch_upsert_by_identity(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[BatchWriteInput],
        identity_field: str,
        invalid_email_policy: InvalidEmailPolicy,
        invalid_email_audit_field: str | None,
        identity_fields: list[str],
    ) -> dict[str, BatchWriteResult]:
        outcomes: dict[str, BatchWriteResult] = {}
        for index in range(0, len(records), _HUBSPOT_OBJECT_BATCH_SIZE):
            chunk = records[index : index + _HUBSPOT_OBJECT_BATCH_SIZE]
            try:
                payload = await self._with_provider_control(
                    partial(
                        self._gateway.batch_upsert_crm_objects,
                        credentials,
                        object_type=object_type,
                        inputs=[
                            {
                                "id": str(record.properties[identity_field]),
                                "idProperty": identity_field,
                                "objectWriteTraceId": record.trace_id,
                                "properties": {
                                    key: value
                                    for key, value in record.properties.items()
                                    if key != identity_field
                                },
                            }
                            for record in chunk
                        ],
                    ),
                    retry_transient=True,
                )
            except HubSpotRequestError as exc:
                if exc.status_code != 409:
                    raise
                for record in chunk:
                    try:
                        result = await self._write_record_with_policies(
                            credentials,
                            object_type=object_type,
                            properties=record.properties,
                            conflict_policy="update_existing",
                            identity_fields=identity_fields,
                            invalid_email_policy=invalid_email_policy,
                            invalid_email_audit_field=invalid_email_audit_field,
                        )
                    except Exception as record_exc:
                        outcomes[record.trace_id] = BatchWriteResult(
                            trace_id=record.trace_id,
                            status="failed",
                            message=str(record_exc)[:500],
                        )
                    else:
                        outcomes[record.trace_id] = BatchWriteResult(
                            trace_id=record.trace_id,
                            status=result.status,
                            destination_record_id=result.destination_record_id,
                            message=result.message,
                        )
                continue
            chunk_outcomes = _batch_write_outcomes(
                payload,
                records=chunk,
                success_status="updated",
                match_fields=[identity_field],
                derive_new_status=True,
            )
            failed_records = [
                record
                for record in chunk
                if chunk_outcomes.get(record.trace_id) is not None
                and chunk_outcomes[record.trace_id].status == "failed"
            ]
            for record in failed_records:
                outcome = chunk_outcomes[record.trace_id]
                fallback_error = HubSpotRequestError(
                    outcome.message or "HubSpot batch upsert failed",
                    status_code=400,
                )
                fallback_kind = _contact_email_fallback_kind(fallback_error)
                fallback_properties = _contact_email_fallback_properties(
                    error=fallback_error,
                    object_type=object_type,
                    properties=record.properties,
                    identity_fields=identity_fields,
                    policy=invalid_email_policy,
                    audit_field=invalid_email_audit_field,
                )
                if fallback_properties is None:
                    continue
                try:
                    fallback = await self._with_provider_control(
                        partial(
                            self._write_record_once,
                            credentials,
                            object_type=object_type,
                            properties=fallback_properties,
                            conflict_policy="update_existing",
                            identity_fields=identity_fields,
                        ),
                        retry_transient=True,
                    )
                except Exception as exc:
                    chunk_outcomes[record.trace_id] = BatchWriteResult(
                        trace_id=record.trace_id,
                        status="failed",
                        message=str(exc)[:500],
                    )
                else:
                    chunk_outcomes[record.trace_id] = BatchWriteResult(
                        trace_id=record.trace_id,
                        status=fallback.status,
                        destination_record_id=fallback.destination_record_id,
                        message=_contact_email_fallback_message(
                            kind=str(fallback_kind),
                            status=fallback.status,
                        ),
                    )
            outcomes.update(chunk_outcomes)
        return outcomes

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        if (relationship.association_category is None) != (
            relationship.association_type_id is None
        ):
            raise ValidationFailedError(
                "HubSpot association category and type id must be provided together.",
                details={"code": "FERRY_HUBSPOT_ASSOCIATION_TYPE_INVALID"},
            )
        async with hubspot_errors():
            return await self._with_provider_control(
                lambda: self._write_relationship_once(
                    credentials,
                    object_type=relationship.object_type,
                    record_id=relationship.record_id,
                    related_object_type=relationship.related_object_type,
                    related_record_id=relationship.related_record_id,
                    association_category=relationship.association_category,
                    association_type_id=relationship.association_type_id,
                ),
                retry_transient=True,
            )

    async def write_relationships(
        self,
        credentials: Credentials,
        *,
        relationships: list[RelationshipWrite],
    ) -> list[BatchRelationshipWriteResult]:
        if not relationships:
            return []
        outcomes: dict[str, BatchRelationshipWriteResult] = {}
        groups: dict[
            tuple[str, str, str | None, int | None],
            list[RelationshipWrite],
        ] = defaultdict(list)
        for relationship in relationships:
            if (relationship.association_category is None) != (
                relationship.association_type_id is None
            ):
                outcomes[relationship.trace_id] = BatchRelationshipWriteResult(
                    trace_id=relationship.trace_id,
                    status="failed",
                    message="HubSpot association category and type id must be provided together.",
                )
                continue
            groups[
                (
                    relationship.object_type,
                    relationship.related_object_type,
                    relationship.association_category,
                    relationship.association_type_id,
                )
            ].append(relationship)

        for (
            object_type,
            related_object_type,
            association_category,
            association_type_id,
        ), grouped_relationships in groups.items():
            for index in range(0, len(grouped_relationships), _HUBSPOT_ASSOCIATION_BATCH_SIZE):
                chunk = grouped_relationships[index : index + _HUBSPOT_ASSOCIATION_BATCH_SIZE]
                inputs: list[dict[str, Any]] = []
                for relationship in chunk:
                    row: dict[str, Any] = {
                        "from": {"id": relationship.record_id},
                        "to": {"id": relationship.related_record_id},
                    }
                    if association_category is not None and association_type_id is not None:
                        row["types"] = [
                            {
                                "associationCategory": association_category,
                                "associationTypeId": association_type_id,
                            }
                        ]
                    inputs.append(row)
                try:
                    payload = await self._with_provider_control(
                        partial(
                            self._gateway.batch_create_crm_associations,
                            credentials,
                            from_object=object_type,
                            to_object=related_object_type,
                            inputs=inputs,
                            use_default=association_category is None,
                        ),
                        retry_transient=True,
                    )
                    errors = payload.get("errors") if isinstance(payload, dict) else None
                    if isinstance(errors, list) and errors:
                        message = str(
                            next(
                                (
                                    error.get("message")
                                    for error in errors
                                    if isinstance(error, dict) and error.get("message")
                                ),
                                "HubSpot association batch returned one or more errors.",
                            )
                        )[:500]
                        raise TransientProviderError(
                            message,
                            retryable=False,
                            details={"code": "FERRY_HUBSPOT_ASSOCIATION_BATCH_FAILED"},
                        )
                except Exception as exc:
                    for relationship in chunk:
                        outcomes[relationship.trace_id] = BatchRelationshipWriteResult(
                            trace_id=relationship.trace_id,
                            status="failed",
                            message=str(exc)[:500],
                        )
                else:
                    for relationship in chunk:
                        outcomes[relationship.trace_id] = BatchRelationshipWriteResult(
                            trace_id=relationship.trace_id,
                            status="linked",
                        )
        return [
            outcomes.get(relationship.trace_id)
            or BatchRelationshipWriteResult(
                trace_id=relationship.trace_id,
                status="failed",
                message="HubSpot association batch did not account for this relationship.",
            )
            for relationship in relationships
        ]

    async def _write_relationship_once(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        record_id: str,
        related_object_type: str,
        related_record_id: str,
        association_category: str | None,
        association_type_id: int | None,
    ) -> RelationshipWriteResult:
        association_types = (
            [
                {
                    "associationCategory": association_category,
                    "associationTypeId": association_type_id,
                }
            ]
            if association_category is not None and association_type_id is not None
            else None
        )
        association_input: dict[str, Any] = {
            "from": {"id": record_id},
            "to": {"id": related_record_id},
        }
        if association_types is not None:
            association_input["types"] = association_types
        await self._gateway.batch_create_crm_associations(
            credentials,
            from_object=object_type,
            to_object=related_object_type,
            inputs=[association_input],
            use_default=association_types is None,
        )
        return RelationshipWriteResult(status="linked")

    async def _with_provider_control[T](
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        retry_transient: bool,
    ) -> T:
        async with self._request_lock:
            for attempt in range(5):
                now = self._monotonic()
                throttle_wait = max(0.0, self._next_request_at - now)
                if throttle_wait:
                    self._metrics["throttleWaitMs"] += round(throttle_wait * 1000)
                    await self._sleep(throttle_wait)
                self._next_request_at = self._monotonic() + self._min_interval_seconds
                self._metrics["requests"] += 1
                try:
                    return await operation()
                except HubSpotRequestError as exc:
                    status_code = exc.status_code
                    retryable_transport = isinstance(exc.__cause__, httpx.TransportError)
                    retryable_rate_limit = status_code in {423, 429}
                    retryable_transient = retry_transient and (
                        status_code in {500, 502, 503, 504} or retryable_transport
                    )
                    if attempt >= 4 or (not retryable_rate_limit and not retryable_transient):
                        raise
                    retry_after = exc.retry_after_seconds
                    wait_seconds = (
                        max(self._min_interval_seconds, retry_after)
                        if retry_after is not None
                        else (2.0 if status_code == 423 else min(30.0, float(2**attempt)))
                    )
                    self._metrics["retries"] += 1
                    if status_code == 429:
                        self._metrics["rateLimitRetries"] += 1
                    self._metrics["lastRetryAt"] = datetime.now(UTC).isoformat()
                    self._next_request_at = self._monotonic() + wait_seconds
            raise RuntimeError("HubSpot retry loop exhausted")

    def retry_metrics(self) -> dict[str, Any]:
        return dict(self._metrics)

    async def list_owners(
        self,
        credentials: Credentials,
    ) -> list[OwnerProfile]:
        async with hubspot_errors():
            owners = await self._gateway.list_owners(credentials)
        profiles = []
        for owner in owners:
            owner_id = str(owner.get("id") or "").strip()
            email = str(owner.get("email") or "").strip()
            if owner_id and email:
                name = " ".join(
                    value
                    for value in (
                        str(owner.get("firstName") or "").strip(),
                        str(owner.get("lastName") or "").strip(),
                    )
                    if value
                )
                profiles.append(
                    OwnerProfile(
                        id=owner_id,
                        email=email,
                        active=not bool(owner.get("archived")),
                        name=name or None,
                    )
                )
        return profiles

    async def _find_existing_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        identity_fields: list[str],
    ) -> str | None:
        for field in identity_fields:
            value = properties.get(field)
            if value in {None, ""}:
                continue
            payload = await self._gateway.search_crm_objects(
                credentials,
                object_type=object_type,
                payload={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": field,
                                    "operator": "EQ",
                                    "value": str(value),
                                }
                            ]
                        }
                    ],
                    "limit": 1,
                    "properties": [field],
                },
            )
            results = payload.get("results")
            if isinstance(results, list) and results and isinstance(results[0], dict):
                existing_id = str(results[0].get("id") or "").strip()
                if existing_id:
                    return existing_id
        return None
