# SPDX-License-Identifier: AGPL-3.0-only
"""Apply reviewed mapping fields to source records.

Faithful port of the production record-mapping module: mapping groups, route
manifests, scalar transforms, value maps, and unmapped-value policies. Route
keys, transform semantics, and error codes are production contracts — drift
here is a bug.

Route-manifest dicts use camelCase keys (``routeKey``, ``sourceObject``, …):
that is the persisted run-report format shared with the production runtime,
not a wire alias — do not rename. ``hubspot_datetime_ms``/``hubspot_date_ms``
are destination-format transform names (HubSpot stores datetimes as epoch
milliseconds), not Sanka coupling.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

from sanka.connector import SourceFilter
from sanka.runtime.mapping.errors import MappingError
from sanka.runtime.mapping.model import MigrationMappingField

MappingGroup = tuple[str, str, SourceFilter | None, list[MigrationMappingField]]
MappingRouteManifest = list[dict[str, Any]]
_OMIT_MAPPED_VALUE = object()


def _source_filter_key(source_filter: SourceFilter | None) -> tuple[str, str, bool] | None:
    if source_filter is None:
        return None
    return (source_filter.field, source_filter.operator, source_filter.value)


def _source_filter_payload(source_filter: SourceFilter) -> dict[str, Any]:
    return {
        "field": source_filter.field,
        "operator": source_filter.operator,
        "value": source_filter.value,
    }


def mapping_group_key(
    source_object: str,
    target_object: str,
    source_filter: SourceFilter | None,
) -> str:
    if source_filter is None:
        return f"{source_object}|{target_object}"
    value = "true" if source_filter.value else "false"
    return f"{source_object}|{target_object}|{source_filter.field}={source_filter.operator}:{value}"


def mapping_route_manifest(groups: list[MappingGroup]) -> MappingRouteManifest:
    """Freeze the exact source predicate that defines every executable route."""

    return [
        {
            "routeKey": mapping_group_key(source_object, target_object, source_filter),
            "sourceObject": source_object,
            "destinationObject": target_object,
            "sourceFilter": (
                _source_filter_payload(source_filter) if source_filter is not None else None
            ),
            **(
                {
                    "identityFields": sorted(
                        {
                            field.target_field
                            for field in fields
                            if field.identity is True
                            and field.mapping_kind not in {"relationship", "reference"}
                        }
                    )
                }
                if any(
                    field.identity is True
                    and field.mapping_kind not in {"relationship", "reference"}
                    for field in fields
                )
                else {}
            ),
        }
        for source_object, target_object, source_filter, fields in groups
    ]


def destination_identity_fields(fields: list[MigrationMappingField]) -> list[str]:
    return sorted(
        {
            field.target_field
            for field in fields
            if field.identity is True and field.mapping_kind not in {"relationship", "reference"}
        }
    )


def mapping_groups(fields: list[MigrationMappingField]) -> list[MappingGroup]:
    grouped: dict[
        tuple[str, str, tuple[str, str, bool] | None],
        list[MigrationMappingField],
    ] = defaultdict(list)
    for field in fields:
        source_parts = str(field.source_field or "").split(".", 1)
        if len(source_parts) != 2 or not source_parts[0] or not source_parts[1]:
            continue
        target_object = str(field.target_object or "").strip()
        target_field = str(field.target_field or "").strip()
        if not target_object or not target_field:
            continue
        grouped[(source_parts[0], target_object, _source_filter_key(field.source_filter))].append(
            field
        )
    return [
        (source_object, target_object, rows[0].source_filter, rows)
        for (source_object, target_object, _filter_key), rows in sorted(
            grouped.items(),
            key=lambda item: (
                item[0][0],
                item[0][1],
                str(item[0][2] or ""),
            ),
        )
    ]


def source_field_keys(fields: list[MigrationMappingField]) -> list[str]:
    source_keys = {
        field.source_field.split(".", 1)[1] for field in fields if "." in field.source_field
    }
    source_keys.update(
        predicate_field.split(".", 1)[-1]
        for field in fields
        for entry in field.value_map
        for predicate_field in entry.when
    )
    return sorted(source_keys)


def destination_properties(
    record: dict[str, Any],
    fields: list[MigrationMappingField],
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for field in fields:
        if field.mapping_kind in {"relationship", "reference"}:
            continue
        source_key = field.source_field.split(".", 1)[-1]
        value = record.get(source_key)
        if value is None:
            if field.required:
                raise MappingError(
                    "Required source field is empty.",
                    code="SANKA_MIGRATE_REQUIRED_SOURCE_FIELD_EMPTY",
                    details={
                        "sourceField": field.source_field,
                        "targetField": field.target_field,
                    },
                )
            continue
        try:
            mapped_value = apply_transform(value, field.transform_rule)
            if field.value_map:
                mapped_value = _apply_value_map(
                    record,
                    field=field,
                    original_value=mapped_value,
                )
            if mapped_value is not _OMIT_MAPPED_VALUE:
                properties[field.target_field] = mapped_value
        except MappingError as exc:
            raise MappingError(
                exc.message,
                code=exc.code,
                details={
                    **exc.details,
                    "sourceField": field.source_field,
                    "targetField": field.target_field,
                },
            ) from exc
        except (TypeError, ValueError) as exc:
            raise MappingError(
                "Mapped source value could not be transformed.",
                code="SANKA_MIGRATE_MAPPING_VALUE_INVALID",
                details={
                    "sourceField": field.source_field,
                    "targetField": field.target_field,
                },
            ) from exc
    return properties


def _apply_value_map(
    record: dict[str, Any],
    *,
    field: MigrationMappingField,
    original_value: Any,
) -> Any:
    matches = [
        entry
        for entry in field.value_map
        if all(
            _record_value(record, predicate_field) == expected
            for predicate_field, expected in entry.when.items()
        )
    ]
    if len(matches) == 1:
        return matches[0].value
    if len(matches) > 1:
        raise MappingError(
            "More than one Sanka Migrate value-map predicate matched the source record.",
            code="SANKA_MIGRATE_VALUE_MAP_AMBIGUOUS",
        )
    if field.unmapped_value_policy == "preserve":
        return original_value
    if field.unmapped_value_policy == "omit":
        return _OMIT_MAPPED_VALUE
    raise MappingError(
        "No reviewed Sanka Migrate value-map predicate matched the source record.",
        code="SANKA_MIGRATE_VALUE_MAP_UNMATCHED",
    )


def _record_value(record: dict[str, Any], source_field: str) -> Any:
    if source_field in record:
        return record[source_field]
    return record.get(source_field.split(".", 1)[-1])


def relationship_source_ids(record: dict[str, Any], field: MigrationMappingField) -> list[str]:
    source_key = field.source_field.split(".", 1)[-1]
    value = record.get(source_key)
    values = value if isinstance(value, list | tuple | set) else [value]
    normalized: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("target_record_id") or item.get("id") or item.get("value")
        item_id = str(item or "").strip()
        if item_id and item_id not in normalized:
            normalized.append(item_id)
    return normalized


def apply_transform(value: Any, transform_rule: str | None) -> Any:
    rule = str(transform_rule or "").strip().lower()
    if not rule:
        return value
    if rule == "trim":
        return str(value).strip()
    if rule == "number_parse":
        normalized = str(value).strip().replace(",", "")
        parsed = float(normalized)
        return int(parsed) if parsed.is_integer() else parsed
    if rule == "boolean_map":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
    if rule == "date_parse":
        return str(value).strip()
    if rule == "hubspot_datetime_ms":
        return _epoch_milliseconds(_datetime_value(value))
    if rule == "hubspot_date_ms":
        parsed_date = _date_value(value)
        midnight = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
        return _epoch_milliseconds(midnight)
    if rule == "json_stringify":
        if isinstance(value, str):
            return value.strip()
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    raise MappingError(
        f"Unsupported Sanka Migrate transform rule: {rule}",
        code="SANKA_MIGRATE_TRANSFORM_UNSUPPORTED",
    )


def _datetime_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        numeric = float(value)
        seconds = numeric / 1000 if abs(numeric) >= 100_000_000_000 else numeric
        parsed = datetime.fromtimestamp(seconds, tz=UTC)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("datetime value is empty")
        if text.lstrip("+-").isdigit():
            return _datetime_value(int(text))
        normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _date_value(value: Any) -> date:
    if isinstance(value, datetime):
        return _datetime_value(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return _datetime_value(value).date()
    text = str(value or "").strip()
    if not text:
        raise ValueError("date value is empty")
    if text.lstrip("+-").isdigit():
        return _datetime_value(int(text)).date()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return _datetime_value(text).date()


def _epoch_milliseconds(value: datetime) -> int:
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000
