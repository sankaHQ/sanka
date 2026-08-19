# SPDX-License-Identifier: AGPL-3.0-only
"""Migration-as-code: the declarative migration specification.

A spec names a source and a target (by connector type and connection
reference), plus strategy and verification options. Specs are meant to be
committed to Git, reviewed, and rerun — so they must never contain secrets:

- option keys that look secret-bearing are rejected outright;
- ``$VAR`` / ``${VAR}`` string values are environment references, resolved
  only at execution time via :func:`resolve_env`;
- ``spec_hash`` is computed over the *unresolved* spec, so hashes and the
  approvals bound to them never incorporate secret material.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sanka.runtime.hashing import content_hash

_SECRET_KEY_PATTERN = re.compile(r"password|secret|token|credential|api_key", re.IGNORECASE)
_ENV_REF_PATTERN = re.compile(
    r"^\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))$"
)


class SpecError(ValueError):
    """A migration spec is structurally invalid or unsafe."""


def _reject_secret_keys(mapping: Mapping[str, Any], *, path: str) -> None:
    for key, value in mapping.items():
        if _SECRET_KEY_PATTERN.search(key):
            raise SpecError(
                f"{path}.{key} looks secret-bearing; specs must reference secrets "
                "via $ENV_VAR values or named connections, never contain them"
            )
        if isinstance(value, Mapping):
            _reject_secret_keys(value, path=f"{path}.{key}")


@dataclass(frozen=True, slots=True, kw_only=True)
class EndpointSpec:
    """One side of a migration: a connector type plus how to reach it."""

    type: str
    connection: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.type.strip():
            raise SpecError("endpoint.type is required")
        _reject_secret_keys(self.options, path=f"{self.type}.options")

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "connection": self.connection, "options": dict(self.options)}


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationSpec:
    source: EndpointSpec
    target: EndpointSpec
    strategy: dict[str, Any] = field(default_factory=dict)
    verify: dict[str, Any] = field(default_factory=dict)
    name: str | None = None

    def __post_init__(self) -> None:
        _reject_secret_keys(self.strategy, path="strategy")
        _reject_secret_keys(self.verify, path="verify")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "strategy": dict(self.strategy),
            "verify": dict(self.verify),
        }

    @property
    def spec_hash(self) -> str:
        """Canonical hash of the unresolved spec (never of resolved secrets)."""
        return content_hash(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MigrationSpec:
        try:
            source = data["source"]
            target = data["target"]
        except KeyError as exc:
            raise SpecError(f"spec is missing required section: {exc.args[0]}") from exc
        return cls(
            source=_endpoint_from(source, section="source"),
            target=_endpoint_from(target, section="target"),
            strategy=dict(data.get("strategy") or {}),
            verify=dict(data.get("verify") or {}),
            name=data.get("name"),
        )

    @classmethod
    def from_yaml(cls, text: str) -> MigrationSpec:
        import yaml

        data = yaml.safe_load(text)
        if not isinstance(data, Mapping):
            raise SpecError("spec YAML must be a mapping")
        return cls.from_dict(data)


def _endpoint_from(data: Any, *, section: str) -> EndpointSpec:
    if isinstance(data, str):
        return EndpointSpec(type=data)
    if not isinstance(data, Mapping):
        raise SpecError(f"{section} must be a mapping or a connector type string")
    if "type" not in data:
        raise SpecError(f"{section}.type is required")
    known = {"type", "connection", "options"}
    options = dict(data.get("options") or {})
    for key, value in data.items():
        if key not in known:
            options[key] = value
    return EndpointSpec(
        type=str(data["type"]),
        connection=data.get("connection"),
        options=options,
    )


def resolve_env(
    spec: MigrationSpec,
    *,
    env: Mapping[str, str] | None = None,
) -> MigrationSpec:
    """Return a copy with ``$VAR`` / ``${VAR}`` string values substituted.

    Resolution happens at execution time only. The resolved copy may hold
    secret values (e.g. DSNs); hash and persist the *original* spec, never
    the resolved one.
    """
    environment = os.environ if env is None else env

    def substitute(value: Any, *, path: str) -> Any:
        if isinstance(value, str):
            match = _ENV_REF_PATTERN.match(value)
            if match is None:
                return value
            variable = match.group("braced") or match.group("plain")
            if variable not in environment:
                raise SpecError(f"{path} references undefined environment variable {variable!r}")
            return environment[variable]
        if isinstance(value, Mapping):
            return {key: substitute(item, path=f"{path}.{key}") for key, item in value.items()}
        if isinstance(value, list):
            return [substitute(item, path=f"{path}[{i}]") for i, item in enumerate(value)]
        return value

    def resolve_endpoint(endpoint: EndpointSpec, *, section: str) -> EndpointSpec:
        return EndpointSpec(
            type=endpoint.type,
            connection=substitute(endpoint.connection, path=f"{section}.connection"),
            options=substitute(endpoint.options, path=f"{section}.options"),
        )

    return MigrationSpec(
        source=resolve_endpoint(spec.source, section="source"),
        target=resolve_endpoint(spec.target, section="target"),
        strategy=substitute(spec.strategy, path="strategy"),
        verify=substitute(spec.verify, path="verify"),
        name=spec.name,
    )
