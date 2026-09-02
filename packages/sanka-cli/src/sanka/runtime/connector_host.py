# SPDX-License-Identifier: AGPL-3.0-only
"""Isolated host for verified marketplace connector environments."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import sys
from collections.abc import Coroutine
from dataclasses import fields, is_dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Any, NoReturn, cast

from sanka_connector import (
    ENTRY_POINT_GROUP,
    BatchRelationshipWriteResult,
    BatchWriteInput,
    BatchWriteResult,
    ConnectorRegistration,
    Credentials,
    CustomObjectDefinition,
    CustomObjectProperty,
    DestinationConnector,
    FieldSchema,
    Inventory,
    Limits,
    ObjectSchema,
    OwnerProfile,
    PipelineDefinition,
    PipelineStage,
    PropertyDefinition,
    PropertyResult,
    ProviderIdentity,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    ResourceResult,
    SourceConnector,
    SourceFilter,
    SourceObject,
    SupportsBatchRelationshipWrites,
    SupportsBatchWrites,
    SupportsBoundedCounts,
    SupportsBoundedReads,
    SupportsConfigValidation,
    SupportsHighWaterMark,
    SupportsIdentityInspection,
    SupportsLimits,
    SupportsOwnerDirectory,
    SupportsPropertyProvisioning,
    SupportsRecordCounts,
    SupportsResourceProvisioning,
    SupportsRetryMetrics,
    SupportsSchemaProvisioning,
    SupportsSnapshotBounds,
    WriteOptions,
    WriteResult,
)

PROTOCOL = "sanka-connector/v1"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
OPERATIONS = frozenset(
    {
        "describe",
        "discover_objects",
        "inventory",
        "read_records",
        "automatic_target_object",
        "write_record",
        "write_relationship",
        "inspect",
        "validate",
        "limits",
        "count_records",
        "high_water_mark",
        "read_records_bounded",
        "count_records_bounded",
        "list_owners",
        "write_records",
        "write_relationships",
        "reconcile_properties",
        "reconcile_resources",
        "retry_metrics",
    }
)


class HostError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_WIRE_TYPES = {
    item.__name__: item
    for item in (
        BatchRelationshipWriteResult,
        BatchWriteInput,
        BatchWriteResult,
        Credentials,
        CustomObjectDefinition,
        CustomObjectProperty,
        FieldSchema,
        Inventory,
        Limits,
        ObjectSchema,
        OwnerProfile,
        PipelineDefinition,
        PipelineStage,
        PropertyDefinition,
        PropertyResult,
        ProviderIdentity,
        RecordPage,
        RelationshipWrite,
        RelationshipWriteResult,
        ResourceResult,
        SourceFilter,
        SourceObject,
        WriteOptions,
        WriteResult,
    )
}


def encode_value(value: Any) -> Any:
    """Encode only JSON primitives and the public connector SPI dataclasses."""
    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise HostError("SANKA_CONNECTOR_PAYLOAD", "non-finite numbers are not supported")
        return value
    if value_type in {list, tuple}:
        return [encode_value(item) for item in value]
    if value_type in {set, frozenset}:
        try:
            items = sorted(value)
        except TypeError as error:
            raise HostError(
                "SANKA_CONNECTOR_PAYLOAD", "set payload values must share one ordered type"
            ) from error
        return {
            "__sanka_type__": "set",
            "items": [encode_value(item) for item in items],
        }
    if value_type is dict:
        if any(type(key) is not str for key in value):
            raise HostError("SANKA_CONNECTOR_PAYLOAD", "object keys must be strings")
        return {key: encode_value(item) for key, item in value.items()}
    name = value_type.__name__
    if is_dataclass(value) and _WIRE_TYPES.get(name) is value_type:
        return {
            "__sanka_type__": name,
            "fields": {
                field.name: encode_value(getattr(value, field.name)) for field in fields(value)
            },
        }
    raise HostError("SANKA_CONNECTOR_PAYLOAD", "unsupported connector payload value")


def decode_value(value: Any) -> Any:
    """Decode the corresponding bounded connector wire value."""
    if value is None or type(value) in {bool, int, float, str}:
        if type(value) is float and not math.isfinite(value):
            raise HostError("SANKA_CONNECTOR_PAYLOAD", "non-finite numbers are not supported")
        return value
    if type(value) is list:
        return [decode_value(item) for item in value]
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise HostError("SANKA_CONNECTOR_PAYLOAD", "connector payload is not JSON-safe")
    marker = value.get("__sanka_type__")
    if marker is None:
        return {key: decode_value(item) for key, item in value.items()}
    if marker == "set" and set(value) == {"__sanka_type__", "items"}:
        items = value["items"]
        if type(items) is not list:
            raise HostError("SANKA_CONNECTOR_PAYLOAD", "set payload is invalid")
        try:
            return {decode_value(item) for item in items}
        except TypeError as error:
            raise HostError("SANKA_CONNECTOR_PAYLOAD", "set payload is invalid") from error
    value_type = _WIRE_TYPES.get(marker) if type(marker) is str else None
    raw_fields = value.get("fields")
    if (
        value_type is None
        or set(value) != {"__sanka_type__", "fields"}
        or type(raw_fields) is not dict
    ):
        raise HostError("SANKA_CONNECTOR_PAYLOAD", "typed connector payload is invalid")
    expected = {field.name for field in fields(value_type)}
    if set(raw_fields) != expected:
        raise HostError("SANKA_CONNECTOR_PAYLOAD", "typed connector fields are invalid")
    try:
        return value_type(**{key: decode_value(item) for key, item in raw_fields.items()})
    except (TypeError, ValueError) as error:
        raise HostError("SANKA_CONNECTOR_PAYLOAD", "typed connector value is invalid") from error


_CAPABILITIES: dict[str, tuple[type[Any], str | None]] = {
    "SupportsIdentityInspection": (SupportsIdentityInspection, "inspect"),
    "SupportsConfigValidation": (SupportsConfigValidation, "validate"),
    "SupportsLimits": (SupportsLimits, "limits"),
    "SupportsRecordCounts": (SupportsRecordCounts, "count_records"),
    "SupportsHighWaterMark": (SupportsHighWaterMark, "high_water_mark"),
    "SupportsBoundedReads": (SupportsBoundedReads, "read_records_bounded"),
    "SupportsBoundedCounts": (SupportsBoundedCounts, "count_records_bounded"),
    "SupportsOwnerDirectory": (SupportsOwnerDirectory, "list_owners"),
    "SupportsBatchWrites": (SupportsBatchWrites, "write_records"),
    "SupportsBatchRelationshipWrites": (
        SupportsBatchRelationshipWrites,
        "write_relationships",
    ),
    "SupportsPropertyProvisioning": (
        SupportsPropertyProvisioning,
        "reconcile_properties",
    ),
    "SupportsResourceProvisioning": (
        SupportsResourceProvisioning,
        "reconcile_resources",
    ),
    "SupportsRetryMetrics": (SupportsRetryMetrics, "retry_metrics"),
    "SupportsSnapshotBounds": (SupportsSnapshotBounds, None),
    "SupportsSchemaProvisioning": (SupportsSchemaProvisioning, None),
}
_OPERATION_CAPABILITY = {
    operation: protocol for protocol, operation in _CAPABILITIES.values() if operation is not None
}
_SOURCE_OPERATIONS = {
    "discover_objects",
    "read_records",
    "count_records",
    "high_water_mark",
    "read_records_bounded",
    "count_records_bounded",
}
_DESTINATION_OPERATIONS = {
    "automatic_target_object",
    "write_record",
    "write_relationship",
    "write_records",
    "write_relationships",
    "reconcile_properties",
    "reconcile_resources",
}
_COMMON_OPERATIONS = {
    "inventory",
    "inspect",
    "validate",
    "limits",
    "list_owners",
    "retry_metrics",
}


def _fail(code: str, message: str) -> NoReturn:
    raise HostError(code, message)


def _site_packages(value: str) -> Path:
    candidate = Path(value)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise HostError(
            "SANKA_CONNECTOR_ENVIRONMENT", "connector environment is unavailable"
        ) from error
    if (
        not candidate.is_absolute()
        or candidate.is_symlink()
        or resolved != candidate
        or not candidate.is_dir()
    ):
        _fail("SANKA_CONNECTOR_ENVIRONMENT", "connector environment is not an exact directory")
    return candidate


def load_registrations(site_packages: Path) -> dict[str, ConnectorRegistration]:
    """Load registrations only from distributions in the verified environment."""
    sys.path.insert(0, str(site_packages))
    registrations: dict[str, ConnectorRegistration] = {}
    for distribution in distributions(path=[str(site_packages)]):
        for entry in distribution.entry_points:
            if entry.group != ENTRY_POINT_GROUP:
                continue
            if entry.name in registrations:
                _fail("SANKA_CONNECTOR_PROVIDER", "connector provider is declared more than once")
            try:
                registration = entry.load()
            except Exception as error:
                raise HostError(
                    "SANKA_CONNECTOR_LOAD", "connector registration could not be loaded"
                ) from error
            if (
                not isinstance(registration, ConnectorRegistration)
                or registration.name != entry.name
            ):
                _fail("SANKA_CONNECTOR_PROVIDER", "connector registration identity is invalid")
            registrations[entry.name] = registration
    return registrations


def _describe(registration: ConnectorRegistration) -> dict[str, Any]:
    roles: list[str] = []
    capabilities: dict[str, list[str]] = {}
    binding_kinds: dict[str, str] = {}
    for role in ("source", "destination"):
        connector = getattr(registration, role)
        if connector is None:
            continue
        expected = SourceConnector if role == "source" else DestinationConnector
        if not isinstance(connector, expected) or connector.provider != registration.name:
            _fail("SANKA_CONNECTOR_PROVIDER", "connector registration role is invalid")
        roles.append(role)
        binding_kinds[role] = connector.binding_kind
        capabilities[role] = [
            name
            for name, (protocol, _operation) in _CAPABILITIES.items()
            if isinstance(connector, protocol)
        ]
    return {"roles": roles, "binding_kinds": binding_kinds, "capabilities": capabilities}


def invoke_registration(
    registration: ConnectorRegistration,
    operation: str,
    payload: dict[str, Any],
) -> Any:
    if operation == "describe":
        if payload:
            _fail("SANKA_CONNECTOR_PAYLOAD", "describe payload must be empty")
        return _describe(registration)
    role = payload.pop("role", None)
    if role is None:
        if operation in _SOURCE_OPERATIONS:
            role = "source"
        elif operation in _DESTINATION_OPERATIONS:
            role = "destination"
    if role not in {"source", "destination"}:
        _fail("SANKA_CONNECTOR_CAPABILITY", "connector role is required")
    connector = registration.source if role == "source" else registration.destination
    if connector is None:
        _fail("SANKA_CONNECTOR_CAPABILITY", "connector does not expose the requested role")
    base = SourceConnector if role == "source" else DestinationConnector
    if not isinstance(connector, base):
        _fail("SANKA_CONNECTOR_CAPABILITY", "connector role does not implement its base protocol")
    allowed = _SOURCE_OPERATIONS if role == "source" else _DESTINATION_OPERATIONS
    if operation not in allowed | _COMMON_OPERATIONS:
        _fail("SANKA_CONNECTOR_CAPABILITY", "operation is unavailable for the connector role")
    capability = _OPERATION_CAPABILITY.get(operation)
    if capability is not None and not isinstance(connector, capability):
        _fail("SANKA_CONNECTOR_CAPABILITY", "connector does not implement the requested operation")
    try:
        method = getattr(connector, operation)
        result = method(**payload)
        if inspect.isawaitable(result):
            result = asyncio.run(cast(Coroutine[Any, Any, Any], result))
        return result
    except Exception as error:
        raise HostError("SANKA_CONNECTOR_OPERATION", "connector operation failed") from error


def handle(
    request: dict[str, Any], registrations: dict[str, ConnectorRegistration]
) -> dict[str, Any]:
    if set(request) != {"protocol_version", "id", "provider", "operation", "payload"}:
        _fail("SANKA_CONNECTOR_PROTOCOL", "connector request fields are invalid")
    if request["protocol_version"] != PROTOCOL:
        _fail("SANKA_CONNECTOR_PROTOCOL", "connector protocol version is unsupported")
    request_id = request["id"]
    provider = request["provider"]
    operation = request["operation"]
    if type(request_id) is not int or request_id < 1:
        _fail("SANKA_CONNECTOR_PROTOCOL", "connector request id is invalid")
    if type(provider) is not str or not provider or provider.strip().lower() != provider:
        _fail("SANKA_CONNECTOR_PROVIDER", "connector provider identity is invalid")
    if type(operation) is not str or operation not in OPERATIONS:
        _fail("SANKA_CONNECTOR_OPERATION", "connector operation is unsupported")
    registration = registrations.get(provider)
    if registration is None:
        _fail("SANKA_CONNECTOR_PROVIDER", "connector provider is not declared")
    payload = decode_value(request["payload"])
    if type(payload) is not dict:
        _fail("SANKA_CONNECTOR_PAYLOAD", "connector payload must be an object")
    return {
        "protocol_version": PROTOCOL,
        "id": request_id,
        "ok": True,
        "result": encode_value(invoke_registration(registration, operation, payload)),
    }


def _error_response(request_id: int, error: HostError) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL,
        "id": request_id,
        "ok": False,
        "error": {"code": error.code, "message": str(error)},
    }


def _write(response: dict[str, Any]) -> None:
    encoded = json.dumps(response, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_RESPONSE_BYTES:
        encoded = json.dumps(
            _error_response(
                response.get("id", 0),
                HostError("SANKA_CONNECTOR_RESPONSE_LIMIT", "connector response exceeds the limit"),
            ),
            separators=(",", ":"),
        ).encode()
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def serve(site_packages: Path) -> int:
    try:
        registrations = load_registrations(site_packages)
    except HostError as error:
        _write(_error_response(0, error))
        return 1
    while True:
        line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 2)
        if not line:
            return 0
        request_id = 0
        try:
            if len(line) > MAX_REQUEST_BYTES + 1 or not line.endswith(b"\n"):
                _fail("SANKA_CONNECTOR_REQUEST_LIMIT", "connector request exceeds the limit")
            request = json.loads(line)
            if type(request) is not dict:
                _fail("SANKA_CONNECTOR_PROTOCOL", "connector request must be an object")
            if type(request.get("id")) is int:
                request_id = request["id"]
            response = handle(request, registrations)
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = _error_response(
                request_id,
                HostError("SANKA_CONNECTOR_JSON", "connector request JSON is malformed"),
            )
        except HostError as error:
            response = _error_response(request_id, error)
        except Exception:
            response = _error_response(
                request_id,
                HostError("SANKA_CONNECTOR_OPERATION", "connector host request failed"),
            )
        _write(response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--site-packages", required=True)
    arguments = parser.parse_args(argv)
    try:
        site_packages = _site_packages(arguments.site_packages)
    except HostError as error:
        _write(_error_response(0, error))
        return 1
    return serve(site_packages)


if __name__ == "__main__":
    raise SystemExit(main())
