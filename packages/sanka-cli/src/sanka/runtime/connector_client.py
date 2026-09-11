# SPDX-License-Identifier: AGPL-3.0-only
"""Persistent bounded client and typed proxies for connector hosts."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from sanka.runtime.connector_host import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PROTOCOL,
    HostError,
    decode_value,
    encode_value,
)
from sanka.runtime.extensions.model import ExtensionError
from sanka_data import (
    BatchRelationshipWriteResult,
    BatchWriteInput,
    BatchWriteResult,
    Credentials,
    CustomObjectDefinition,
    Inventory,
    Limits,
    OwnerProfile,
    PipelineDefinition,
    PropertyDefinition,
    PropertyResult,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    ResourceResult,
    SourceFilter,
    SourceObject,
    SystemIdentity,
    SystemReader,
    SystemWriter,
    WriteOptions,
    WriteResult,
)


def _error(code: str, message: str, **details: Any) -> ExtensionError:
    return ExtensionError(code, f"{code}: {message}", details=details)


def _minimal_environment() -> dict[str, str]:
    allowed = {"LANG", "LC_ALL", "LC_CTYPE", "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


class DataExtensionHostClient:
    """One locked request stream to one verified connector environment."""

    def __init__(
        self,
        python: str | os.PathLike[str] = sys.executable,
        *,
        environment: Path,
        timeout: float = 30.0,
    ) -> None:
        self.python = Path(python)
        self.environment = environment
        self.timeout = timeout
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._stdout: queue.Queue[bytes | None] = queue.Queue()
        self._stderr: queue.Queue[bytes | None] = queue.Queue()
        self._request_id = 0

    def _start(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is not None:
            return_code = process.poll()
            if return_code is None:
                return process
            self.close()
            raise _error(
                "SANKA_CONNECTOR_EXIT",
                "connector host exited between requests",
                return_code=return_code,
            )
        try:
            process = subprocess.Popen(
                [
                    str(self.python),
                    "-I",
                    "-B",
                    "-m",
                    "sanka.runtime.connector_host",
                    "--site-packages",
                    str(self.environment),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.environment,
                env=_minimal_environment(),
            )
        except OSError as error:
            raise _error("SANKA_CONNECTOR_START", "connector host could not be started") from error
        self._process = process
        self._stdout = queue.Queue()
        self._stderr = queue.Queue()
        assert process.stdout is not None and process.stderr is not None
        stdout = self._stdout
        stderr = self._stderr
        threading.Thread(
            target=self._read_stdout,
            args=(process.stdout, stdout),
            daemon=True,
            name="sanka-connector-stdout",
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(process.stderr, stderr),
            daemon=True,
            name="sanka-connector-stderr",
        ).start()
        try:
            unsolicited = self._stdout.get(timeout=min(1.0, self.timeout))
        except queue.Empty:
            pass
        else:
            self.close()
            if unsolicited:
                raise _error(
                    "SANKA_CONNECTOR_UNSOLICITED",
                    "connector host sent output before a request",
                )
            raise _error("SANKA_CONNECTOR_EXIT", "connector host exited during startup")
        return process

    def _read_stdout(self, stream: Any, output: queue.Queue[bytes | None]) -> None:
        while True:
            line = stream.readline(MAX_RESPONSE_BYTES + 2)
            output.put(line or None)
            if not line:
                return

    def _read_stderr(self, stream: Any, output: queue.Queue[bytes | None]) -> None:
        while True:
            chunk = os.read(stream.fileno(), 4096)
            output.put(chunk or None)
            if not chunk:
                return

    def _raise_if_stderr(self) -> None:
        try:
            output = self._stderr.get(timeout=0.01)
        except queue.Empty:
            return
        if output:
            self.close()
            raise _error(
                "SANKA_CONNECTOR_STDERR",
                "connector host wrote to stderr; output was discarded",
            )

    def request(self, provider: str, operation: str, payload: dict[str, Any]) -> Any:
        with self._lock:
            process = self._start()
            if not self._stdout.empty():
                self.close()
                raise _error(
                    "SANKA_CONNECTOR_UNSOLICITED", "connector host sent unsolicited output"
                )
            if process.poll() is not None:
                self.close()
                raise _error("SANKA_CONNECTOR_EXIT", "connector host exited before the request")
            self._raise_if_stderr()
            self._request_id += 1
            request_id = self._request_id
            try:
                encoded_payload = encode_value(payload)
                encoded = json.dumps(
                    {
                        "protocol_version": PROTOCOL,
                        "id": request_id,
                        "provider": provider,
                        "operation": operation,
                        "payload": encoded_payload,
                    },
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            except (HostError, TypeError, ValueError) as error:
                raise _error(
                    "SANKA_CONNECTOR_PAYLOAD", "connector request payload is invalid"
                ) from error
            if len(encoded) > MAX_REQUEST_BYTES:
                raise _error("SANKA_CONNECTOR_REQUEST_LIMIT", "connector request exceeds the limit")
            try:
                assert process.stdin is not None
                process.stdin.write(encoded + b"\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                self.close()
                raise _error("SANKA_CONNECTOR_EXIT", "connector host closed its input") from error
            try:
                line = self._stdout.get(timeout=self.timeout)
            except queue.Empty as error:
                self.close()
                raise _error(
                    "SANKA_CONNECTOR_TIMEOUT", "connector host request timed out"
                ) from error
            self._raise_if_stderr()
            if line is None:
                return_code = process.poll()
                self.close()
                raise _error(
                    "SANKA_CONNECTOR_EXIT",
                    "connector host exited without a response",
                    return_code=return_code,
                )
            if len(line) > MAX_RESPONSE_BYTES + 1 or not line.endswith(b"\n"):
                self.close()
                raise _error(
                    "SANKA_CONNECTOR_RESPONSE_LIMIT", "connector response exceeds the limit"
                )
            try:
                response = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self.close()
                raise _error(
                    "SANKA_CONNECTOR_JSON", "connector response JSON is malformed"
                ) from error
            if type(response) is not dict:
                self.close()
                raise _error("SANKA_CONNECTOR_PROTOCOL", "connector response must be an object")
            if response.get("protocol_version") != PROTOCOL:
                self.close()
                raise _error("SANKA_CONNECTOR_PROTOCOL", "connector host protocol mismatch")
            if type(response.get("id")) is not int or response["id"] != request_id:
                self.close()
                raise _error("SANKA_CONNECTOR_PROTOCOL", "connector response id mismatch")
            if response.get("ok") is True and set(response) == {
                "protocol_version",
                "id",
                "ok",
                "result",
            }:
                try:
                    return decode_value(response["result"])
                except HostError as error:
                    self.close()
                    raise _error(
                        "SANKA_CONNECTOR_PAYLOAD", "connector response payload is invalid"
                    ) from error
            if response.get("ok") is False and set(response) == {
                "protocol_version",
                "id",
                "ok",
                "error",
            }:
                remote = response["error"]
                if (
                    type(remote) is dict
                    and set(remote) == {"code", "message"}
                    and type(remote.get("code")) is str
                    and type(remote.get("message")) is str
                ):
                    raise _error(remote["code"], remote["message"])
            self.close()
            raise _error("SANKA_CONNECTOR_PROTOCOL", "connector response fields are invalid")

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            if process is None:
                return
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            self._stdout = queue.Queue()
            self._stderr = queue.Queue()

    def __enter__(self) -> DataExtensionHostClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _RemoteConnector:
    def __init__(
        self,
        client: DataExtensionHostClient,
        provider: str,
        role: str,
        binding_kind: str,
    ) -> None:
        self._client = client
        self.provider = provider
        self.role = role
        self.binding_kind = binding_kind

    def _request(self, operation: str, payload: Mapping[str, Any]) -> Any:
        return self._client.request(self.provider, operation, {"role": self.role, **payload})

    async def _async_request(self, operation: str, payload: Mapping[str, Any]) -> Any:
        return await asyncio.to_thread(self._request, operation, payload)


class _RemoteSource(_RemoteConnector):
    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        return cast(
            list[SourceObject],
            await self._async_request("discover_objects", {"credentials": credentials}),
        )

    async def inventory(
        self, credentials: Credentials, *, object_types: list[str] | None = None
    ) -> Inventory:
        return cast(
            Inventory,
            await self._async_request(
                "inventory", {"credentials": credentials, "object_types": object_types}
            ),
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
        return cast(
            RecordPage,
            await self._async_request(
                "read_records",
                {
                    "credentials": credentials,
                    "object_type": object_type,
                    "field_keys": field_keys,
                    "limit": limit,
                    "cursor": cursor,
                    "source_filter": source_filter,
                },
            ),
        )


class _RemoteDestination(_RemoteConnector):
    def automatic_target_object(self, canonical_type: str) -> str | None:
        return cast(
            str | None, self._request("automatic_target_object", {"canonical_type": canonical_type})
        )

    async def inventory(self, credentials: Credentials, *, canonical_types: set[str]) -> Inventory:
        return cast(
            Inventory,
            await self._async_request(
                "inventory", {"credentials": credentials, "canonical_types": canonical_types}
            ),
        )

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        return cast(
            WriteResult,
            await self._async_request(
                "write_record",
                {
                    "credentials": credentials,
                    "object_type": object_type,
                    "properties": properties,
                    "options": options,
                },
            ),
        )

    async def write_relationship(
        self, credentials: Credentials, *, relationship: RelationshipWrite
    ) -> RelationshipWriteResult:
        return cast(
            RelationshipWriteResult,
            await self._async_request(
                "write_relationship", {"credentials": credentials, "relationship": relationship}
            ),
        )


class _IdentityInspection(_RemoteConnector):
    async def inspect(self, credentials: Credentials) -> SystemIdentity:
        return cast(
            SystemIdentity, await self._async_request("inspect", {"credentials": credentials})
        )


class _ConfigValidation(_RemoteConnector):
    async def validate(self, credentials: Credentials) -> list[str]:
        return cast(list[str], await self._async_request("validate", {"credentials": credentials}))


class _Limits(_RemoteConnector):
    def limits(self) -> Limits:
        return cast(Limits, self._request("limits", {}))


class _RecordCounts(_RemoteConnector):
    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int:
        return cast(
            int,
            await self._async_request(
                "count_records",
                {
                    "credentials": credentials,
                    "object_type": object_type,
                    "source_filter": source_filter,
                },
            ),
        )


class _HighWaterMark(_RemoteConnector):
    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None:
        return cast(
            str | None,
            await self._async_request(
                "high_water_mark",
                {
                    "credentials": credentials,
                    "object_type": object_type,
                    "source_filter": source_filter,
                },
            ),
        )


class _BoundedReads(_RemoteConnector):
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
        return cast(
            RecordPage,
            await self._async_request(
                "read_records_bounded",
                {
                    "credentials": credentials,
                    "object_type": object_type,
                    "field_keys": field_keys,
                    "limit": limit,
                    "cursor": cursor,
                    "source_filter": source_filter,
                    "upper_bound": upper_bound,
                },
            ),
        )


class _BoundedCounts(_RemoteConnector):
    async def count_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> int:
        return cast(
            int,
            await self._async_request(
                "count_records_bounded",
                {
                    "credentials": credentials,
                    "object_type": object_type,
                    "source_filter": source_filter,
                    "upper_bound": upper_bound,
                },
            ),
        )


class _OwnerDirectory(_RemoteConnector):
    async def list_owners(self, credentials: Credentials) -> list[OwnerProfile]:
        return cast(
            list[OwnerProfile],
            await self._async_request("list_owners", {"credentials": credentials}),
        )


class _BatchWrites(_RemoteConnector):
    async def write_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[BatchWriteInput],
        options: WriteOptions,
    ) -> list[BatchWriteResult]:
        return cast(
            list[BatchWriteResult],
            await self._async_request(
                "write_records",
                {
                    "credentials": credentials,
                    "object_type": object_type,
                    "records": records,
                    "options": options,
                },
            ),
        )


class _BatchRelationshipWrites(_RemoteConnector):
    async def write_relationships(
        self, credentials: Credentials, *, relationships: list[RelationshipWrite]
    ) -> list[BatchRelationshipWriteResult]:
        return cast(
            list[BatchRelationshipWriteResult],
            await self._async_request(
                "write_relationships", {"credentials": credentials, "relationships": relationships}
            ),
        )


class _PropertyProvisioning(_RemoteConnector):
    async def reconcile_properties(
        self,
        credentials: Credentials,
        *,
        definitions: list[PropertyDefinition],
        confirm: bool,
    ) -> list[PropertyResult]:
        return cast(
            list[PropertyResult],
            await self._async_request(
                "reconcile_properties",
                {"credentials": credentials, "definitions": definitions, "confirm": confirm},
            ),
        )


class _ResourceProvisioning(_RemoteConnector):
    async def reconcile_resources(
        self,
        credentials: Credentials,
        *,
        pipelines: list[PipelineDefinition],
        custom_objects: list[CustomObjectDefinition],
        confirm: bool,
    ) -> list[ResourceResult]:
        return cast(
            list[ResourceResult],
            await self._async_request(
                "reconcile_resources",
                {
                    "credentials": credentials,
                    "pipelines": pipelines,
                    "custom_objects": custom_objects,
                    "confirm": confirm,
                },
            ),
        )


class _RetryMetrics(_RemoteConnector):
    def retry_metrics(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._request("retry_metrics", {}))


_MIXINS = {
    "SupportsIdentityInspection": _IdentityInspection,
    "SupportsConfigValidation": _ConfigValidation,
    "SupportsLimits": _Limits,
    "SupportsRecordCounts": _RecordCounts,
    "SupportsHighWaterMark": _HighWaterMark,
    "SupportsBoundedReads": _BoundedReads,
    "SupportsBoundedCounts": _BoundedCounts,
    "SupportsOwnerDirectory": _OwnerDirectory,
    "SupportsBatchWrites": _BatchWrites,
    "SupportsBatchRelationshipWrites": _BatchRelationshipWrites,
    "SupportsPropertyProvisioning": _PropertyProvisioning,
    "SupportsResourceProvisioning": _ResourceProvisioning,
    "SupportsRetryMetrics": _RetryMetrics,
}
_COMMON_CAPABILITIES = {
    "SupportsIdentityInspection",
    "SupportsConfigValidation",
    "SupportsLimits",
    "SupportsOwnerDirectory",
    "SupportsRetryMetrics",
}
_ROLE_CAPABILITIES = {
    "source": _COMMON_CAPABILITIES
    | {
        "SupportsRecordCounts",
        "SupportsHighWaterMark",
        "SupportsBoundedReads",
        "SupportsBoundedCounts",
        "SupportsSnapshotBounds",
    },
    "destination": _COMMON_CAPABILITIES
    | {
        "SupportsBatchWrites",
        "SupportsBatchRelationshipWrites",
        "SupportsPropertyProvisioning",
        "SupportsResourceProvisioning",
        "SupportsSchemaProvisioning",
    },
}


def _valid_description(description: Any, role: str) -> bool:
    if type(description) is not dict or set(description) != {
        "roles",
        "capabilities",
        "binding_kinds",
    }:
        return False
    roles = description["roles"]
    capabilities = description["capabilities"]
    binding_kinds = description["binding_kinds"]
    if (
        type(roles) is not list
        or roles not in (["source"], ["destination"], ["source", "destination"])
        or role not in roles
        or type(capabilities) is not dict
        or set(capabilities) != set(roles)
        or type(binding_kinds) is not dict
        or set(binding_kinds) != set(roles)
    ):
        return False
    for connector_role in roles:
        names = capabilities[connector_role]
        binding_kind = binding_kinds[connector_role]
        if (
            type(names) is not list
            or any(type(name) is not str for name in names)
            or len(names) != len(set(names))
            or any(name not in _ROLE_CAPABILITIES[connector_role] for name in names)
            or type(binding_kind) is not str
            or not binding_kind
        ):
            return False
    return True


def build_remote_data_extension(
    client: DataExtensionHostClient,
    provider: str,
    role: str,
    *,
    description: dict[str, Any] | None = None,
) -> SystemReader | SystemWriter:
    """Build a structural proxy with only capabilities the host advertised."""
    description = description or cast(dict[str, Any], client.request(provider, "describe", {}))
    if not _valid_description(description, role):
        raise _error("SANKA_CONNECTOR_CAPABILITY", "connector description is invalid")
    capabilities = description["capabilities"]
    binding_kinds = description["binding_kinds"]
    base = _RemoteSource if role == "source" else _RemoteDestination
    mixins = tuple(_MIXINS[name] for name in capabilities[role] if name in _MIXINS)
    proxy_type = type(f"Remote{provider.title()}{role.title()}", (base, *mixins), {})
    return cast(
        SystemReader | SystemWriter,
        proxy_type(client, provider, role, binding_kinds[role]),
    )


__all__ = ["DataExtensionHostClient", "build_remote_data_extension"]

# Compatibility imports for existing clients.
ConnectorHostClient = DataExtensionHostClient
build_remote_connector = build_remote_data_extension
