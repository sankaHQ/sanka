# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable extension discovery records and stable errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ExtensionError(RuntimeError):
    """An extension boundary failed with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details: dict[str, Any] = dict(details) if details else {}


@dataclass(frozen=True)
class MatchedEvidence:
    kind: str
    value: str
    path: str


@dataclass(frozen=True)
class Matcher:
    kind: str
    value: str


@dataclass(frozen=True)
class Wheel:
    name: str
    url: str
    sha256: str


@dataclass(frozen=True)
class Fingerprint:
    languages: tuple[str, ...]
    frameworks: tuple[str, ...]
    dependencies: tuple[str, ...]
    evidence: tuple[MatchedEvidence, ...]
    hash: str


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    marketplace: str
    protocol_version: str
    distribution: str
    distribution_version: str
    executable: str
    commands: tuple[str, ...]
    match_all: tuple[Matcher, ...]
    match_any: tuple[Matcher, ...]
    targets: tuple[str, ...]
    runtime_sanka_migrate: str
    wheels: tuple[Wheel, ...]
    digest: str


@dataclass(frozen=True)
class Recommendation:
    id: str
    version: str
    marketplace: str
    targets: tuple[str, ...]
    evidence: tuple[MatchedEvidence, ...]
    status: tuple[str, ...]
    add_command: str
