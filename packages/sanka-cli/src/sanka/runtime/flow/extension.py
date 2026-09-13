# SPDX-License-Identifier: AGPL-3.0-only
"""Verified Flow generation through its own protocol and an isolated environment.

The SDK owns wire validation. This host owns trust, artifact identity, process
limits and target/input correlation. A venv and temporary working directory are
process isolation, not an operating-system filesystem or network sandbox.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Protocol, cast

from sanka.runtime.extensions.model import ExtensionError, FlowCapability
from sanka.runtime.extensions.runner import ExtensionRunner
from sanka.runtime.extensions.store import ExtensionStore, LockEntry
from sanka.runtime.flow.model import BlueprintInput, Document, FlowError

PROTOCOL_VERSION = "sanka-flow-extension/v1"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class FlowCodec(Protocol):
    """Host-supplied, released SDK contract; never supplied by the selected extension."""

    def prepare(self, request: Document, capability: FlowCapability) -> Document: ...

    def decode(self, response: Document, request: Document) -> BlueprintInput | None: ...


class SDKFlowCodec:
    """Load the canonical SDK lazily; older embedded SDKs fail before execution."""

    @staticmethod
    def _protocol() -> Any:
        try:
            return importlib.import_module("sanka_extensions.flow.protocol")
        except ImportError as error:
            raise ExtensionError(
                "SANKA_FLOW_SDK_REQUIRED",
                "Flow generation requires a runtime with the released Flow protocol SDK",
            ) from error

    def prepare(self, request: Document, capability: FlowCapability) -> Document:
        protocol = self._protocol()
        parsed = protocol.BlueprintRequest.from_dict(request.to_dict())
        protocol.FlowCapability.from_dict(capability.to_dict()).validate_request(parsed)
        return Document(parsed.to_dict())

    def decode(self, response: Document, request: Document) -> BlueprintInput | None:
        protocol = self._protocol()
        parsed = protocol.BlueprintResponse.from_dict(response.to_dict())
        parsed.validate_for(protocol.BlueprintRequest.from_dict(request.to_dict()))
        return cast(BlueprintInput | None, parsed.blueprint)


def _response_document(stdout: bytes) -> Document:
    def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate Flow response field")
            result[key] = value
        return result

    if len(stdout) > MAX_MESSAGE_BYTES:
        raise ValueError("Flow response exceeds its byte limit")
    return Document(json.loads(stdout.decode("utf-8"), object_pairs_hook=unique_fields))


class FlowExtensionRunner(ExtensionRunner):
    """Generate one Blueprint from an explicit installed Flow extension identity."""

    def __init__(
        self,
        store: ExtensionStore,
        *,
        codec: FlowCodec | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(user_root=store.user_root, timeout_seconds=timeout_seconds)
        self.store = store
        self.codec = codec or SDKFlowCodec()

    def _generate_process(
        self, lock: LockEntry, request: Document, executable_fd: int
    ) -> tuple[int, bytes, bytes]:
        executable = self._executable(lock)
        opened, linked = os.fstat(executable_fd), executable.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not stat.S_ISREG(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise ExtensionError(
                "SANKA_EXTENSION_PATH", "Flow executable changed after verification"
            )
        command = [str(executable.parent / "python"), "-I", "-B", f"/dev/fd/{executable_fd}"]
        content = (request.text + "\n").encode("utf-8")
        if len(content) > MAX_MESSAGE_BYTES:
            raise ValueError("Flow request exceeds its byte limit")
        with tempfile.TemporaryDirectory(prefix="sanka-flow-") as temporary:
            temporary = str(Path(temporary).resolve())
            return self._execute(
                command,
                content,
                cwd=temporary,
                environment={
                    "LANG": "C.UTF-8",
                    "PYTHONUTF8": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PATH": os.defpath,
                    "HOME": temporary,
                    "TMPDIR": temporary,
                },
                pass_fds=(executable_fd,),
            )

    def generate(
        self,
        extension_id: str,
        *,
        request_id: str,
        definition: dict[str, Any],
        target: dict[str, Any],
        references: list[dict[str, Any]],
        values: dict[str, Any],
    ) -> BlueprintInput:
        """Resolve no implicit type/name match and perform no native construction."""
        try:
            with self.store.execution_guard():
                lock, manifest = self.store.flow_manifest(extension_id)
                capability = next(
                    (item for item in manifest.capabilities if item.type == definition.get("type")),
                    None,
                )
                if capability is None:
                    raise ExtensionError(
                        "SANKA_FLOW_CAPABILITY_UNSUPPORTED",
                        "Selected Flow extension does not declare this definition type",
                    )
                extension = {
                    "id": lock.id,
                    "revision": lock.version,
                    "digest": lock.manifest_digest,
                }
                request = self.codec.prepare(
                    Document(
                        {
                            "schema_version": PROTOCOL_VERSION,
                            "request_id": request_id,
                            "operation": "blueprint",
                            "extension": extension,
                            "definition": definition,
                            "target": target,
                            "references": references,
                            "values": values,
                            "template": capability.template.to_dict(),
                            "output_schema": capability.output_schema,
                        }
                    ),
                    capability,
                )
                prepared = request.to_dict()
                # The SDK canonicalizes this set; its ordering is not a target change.
                expected_target = dict(target)
                if "capabilities" in expected_target:
                    expected_target["capabilities"] = sorted(expected_target["capabilities"])
                for field, expected in (
                    ("extension", extension),
                    ("target", expected_target),
                    ("definition", definition),
                    ("values", values),
                    ("template", capability.template.to_dict()),
                    ("output_schema", capability.output_schema),
                ):
                    if Document({"value": prepared[field]}) != Document({"value": expected}):
                        raise ValueError("Flow SDK changed requested generation input")
                with self.store.execution_lease(lock) as executable_fd:
                    exit_code, stdout, _stderr = self._generate_process(
                        lock, request, executable_fd
                    )
                if self.store.resolve_locked(lock.id) != lock:
                    raise ValueError("Flow installation changed during generation")
                response = _response_document(stdout)
                payload = response.to_dict()
                if set(payload) != {
                    "schema_version",
                    "request_id",
                    "request_digest",
                    "operation",
                    "extension",
                    "outcome",
                    "blueprint",
                    "error",
                } or any(
                    payload.get(field) != expected
                    for field, expected in (
                        ("schema_version", PROTOCOL_VERSION),
                        ("operation", "blueprint"),
                        ("request_id", request_id),
                        ("request_digest", request.digest),
                        ("extension", extension),
                    )
                ):
                    raise ValueError("Flow response does not match the exact request")
                if (payload["outcome"], exit_code) not in (("success", 0), ("error", 1)):
                    raise ValueError("Flow process status and protocol outcome differ")
                blueprint = self.codec.decode(response, request)
                if blueprint is None:
                    raise ExtensionError(
                        "SANKA_FLOW_GENERATION_FAILED",
                        "Flow extension could not resolve the requested Blueprint",
                        details={"extension_id": lock.id, "error": payload["error"]},
                    )
                output = blueprint.to_dict()
                origin = output.get("origin", {})
                if (
                    output.get("schema_version") != capability.output_schema
                    or output.get("extension") != extension
                    or origin.get("kind") != "template"
                    or origin.get("identity") != capability.template.to_dict()
                    or output.get("references") != prepared["references"]
                    or output.get("mappings") != []
                    or Document(output.get("parameters", {}))
                    != Document(
                        {
                            "target": prepared["target"],
                            "values": values,
                            "definition_parameters": definition.get("parameters"),
                        }
                    )
                ):
                    raise ValueError(
                        "Flow Blueprint changed requested provenance or target bindings"
                    )
                return blueprint
        except ExtensionError:
            raise
        except OSError as error:
            raise ExtensionError(
                "SANKA_FLOW_EXTENSION_IO", "Flow extension process could not complete"
            ) from error
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
            FlowError,
        ) as error:
            raise ExtensionError(
                "SANKA_FLOW_EXTENSION_PROTOCOL", "Flow extension request or response is invalid"
            ) from error


__all__ = ["FlowCodec", "FlowExtensionRunner", "SDKFlowCodec"]
