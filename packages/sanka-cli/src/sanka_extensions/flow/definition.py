# SPDX-License-Identifier: Apache-2.0
"""Versioned requests for constructing business configurations.

Policy requirements are fixed in this schema version. A runtime must implement
them before accepting these requests; accepting the JSON is not enforcement.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import cast

SCHEMA_VERSION = "sanka-flow-definition/v1"

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None

_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:/[a-z][a-z0-9]*(?:-[a-z0-9]+)*)?")


def _policies() -> dict[str, JsonValue]:
    return {
        "reapply": {
            "user_changes": "preserve",
            "conflicts": "require_resolution",
            "ownership": "installation",
            "removals": "explicit_plan",
        },
        "lifecycle": {
            "stages": ["construct", "verify", "activate"],
            "new_automations": "disabled_until_activation",
            "activation": "explicit_verified_revision",
        },
    }


def _validate_json(value: object, ancestors: set[int]) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError("parameters must contain only finite JSON values")
    if isinstance(value, (list, dict)):
        if id(value) in ancestors:
            raise ValueError("parameters must not contain cycles")
        ancestors.add(id(value))
        try:
            values: Iterable[object]
            if isinstance(value, dict):
                if not all(isinstance(key, str) for key in value):
                    raise ValueError("parameter object keys must be strings")
                values = value.values()
            else:
                values = value
            for item in values:
                _validate_json(item, ancestors)
        finally:
            ancestors.remove(id(value))
        return
    raise ValueError("parameters must contain only JSON values")


def _parameters_json(value: object) -> str:
    if not isinstance(value, dict):
        raise ValueError("parameters must be an object")
    _validate_json(value, set())
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True, init=False)
class FlowDefinition:
    """An immutable, unresolved request; not an installed or active business flow.

    Parameters must contain configuration only. Credentials are resolved by the
    executing runtime from separately configured system references.
    """

    type: str
    _parameters: str = field(repr=False)

    def __init__(self, *, type: str, parameters: dict[str, JsonValue] | None = None) -> None:
        if not isinstance(type, str) or len(type) > 127 or not _TYPE_PATTERN.fullmatch(type):
            raise ValueError("type must be a lowercase kebab-case name or publisher/name")
        object.__setattr__(self, "type", type)
        object.__setattr__(
            self, "_parameters", _parameters_json({} if parameters is None else parameters)
        )

    @property
    def parameters(self) -> dict[str, JsonValue]:
        """Return a copy; editing it cannot alter an existing definition."""
        return cast(dict[str, JsonValue], json.loads(self._parameters))

    @property
    def policies(self) -> dict[str, JsonValue]:
        """Return the runtime requirements fixed by this schema version."""
        return _policies()


def create(*, type: str, parameters: dict[str, JsonValue] | None = None) -> FlowDefinition:
    """Describe a business flow without resolving extensions or causing side effects.

    The type is an unresolved selector, not a built-in template. The runtime must
    resolve it to one verified extension and pin that identity in a reviewed plan.
    """
    return FlowDefinition(type=type, parameters=parameters)


def encode_definition(definition: FlowDefinition) -> dict[str, JsonValue]:
    """Produce a standalone JSON-compatible request, including mandatory policies."""
    if not isinstance(definition, FlowDefinition):
        raise ValueError("definition must be a FlowDefinition")
    return {
        "schema_version": SCHEMA_VERSION,
        "type": definition.type,
        "parameters": definition.parameters,
        "policies": definition.policies,
    }


def decode_definition(value: object) -> FlowDefinition:
    """Reject unsupported versions, unknown fields and weakened lifecycle policies."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "type",
        "parameters",
        "policies",
    }:
        raise ValueError("definition has unexpected or missing fields")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    if value["policies"] != _policies():
        raise ValueError("policies must preserve user changes and require verified activation")
    # Validate before casting so malformed external JSON never reaches consumers.
    parameters = cast(dict[str, JsonValue], json.loads(_parameters_json(value["parameters"])))
    return FlowDefinition(type=value["type"], parameters=parameters)
