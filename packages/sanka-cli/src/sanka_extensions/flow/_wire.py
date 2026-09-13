# SPDX-License-Identifier: Apache-2.0
"""Strict JSON helpers shared by the versioned Flow artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Self, cast

from sanka_extensions.flow.definition import JsonValue


def text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > 1024:
        raise ValueError(
            f"{label} must be a nonblank bounded string without surrounding whitespace"
        )
    return value


def identifier(value: object, label: str) -> str:
    value = text(value, label)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value) is None:
        raise ValueError(f"{label} must be a stable logical identifier")
    return value


def choice(value: object, values: tuple[str, ...], label: str) -> str:
    if type(value) is not str or value not in values:
        raise ValueError(f"unsupported {label}: expected one of {values}")
    return value


def boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def object_fields(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} has unexpected or missing fields; expected {sorted(fields)}")
    return cast(dict[str, Any], value)


def array[T](value: object, decode: Callable[[object], T], label: str) -> tuple[T, ...]:
    if type(value) is not list:
        raise ValueError(f"{label} must be an array")
    return tuple(decode(item) for item in value)


def instances[T](values: tuple[T, ...], kind: type[T], label: str) -> tuple[T, ...]:
    if type(values) is not tuple or any(type(item) is not kind for item in values):
        raise ValueError(f"{label} must be a tuple of {kind.__name__}")
    return values


def unique[T](values: tuple[T, ...], key: Callable[[T], str], label: str) -> tuple[T, ...]:
    keys = [key(item) for item in values]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{label} must have unique identities")
    return tuple(sorted(values, key=key))


def identifiers(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError(f"{label} must be a tuple")
    for value in values:
        identifier(value, label)
    return unique(values, lambda item: item, label)


def _validate_json(value: object, ancestors: set[int]) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) in (dict, list):
        if id(value) in ancestors:
            raise ValueError("JSON values must not contain cycles")
        ancestors.add(id(value))
        try:
            children: Iterable[object]
            if isinstance(value, dict):
                if any(type(key) is not str for key in value):
                    raise ValueError("JSON object keys must be strings")
                children = value.values()
            else:
                children = cast(list[object], value)
            for child in children:
                _validate_json(child, ancestors)
        finally:
            ancestors.remove(id(value))
        return
    raise ValueError("only finite JSON values are supported")


def canonical_json(value: object) -> str:
    _validate_json(value, set())
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def artifact_digest(value: object) -> str:
    """Hash exact JSON meaning independently of dictionary insertion order."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class FrozenJson:
    _json: str

    def __init__(self, value: object) -> None:
        object.__setattr__(self, "_json", canonical_json(value))

    @property
    def value(self) -> JsonValue:
        return cast(JsonValue, json.loads(self._json))


class WireRecord:
    """Common interface only; each contract owns its exact field validation."""

    def to_dict(self) -> dict[str, JsonValue]:
        raise NotImplementedError

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raise NotImplementedError
