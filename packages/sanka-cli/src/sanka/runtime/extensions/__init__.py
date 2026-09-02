# SPDX-License-Identifier: AGPL-3.0-only
"""Static extension marketplace discovery."""

from typing import TYPE_CHECKING, Any

from sanka.runtime.extensions.discovery import (
    fingerprint_repository,
    load_marketplace,
    recommend,
)
from sanka.runtime.extensions.model import (
    ExtensionError,
    Fingerprint,
    Manifest,
    MatchedEvidence,
    Matcher,
    Provider,
    Recommendation,
    Wheel,
)

if TYPE_CHECKING:
    from sanka.runtime.extensions.runner import ExtensionResult, ExtensionRunner

__all__ = [
    "ExtensionError",
    "ExtensionResult",
    "ExtensionRunner",
    "Fingerprint",
    "Manifest",
    "MatchedEvidence",
    "Matcher",
    "Provider",
    "Recommendation",
    "Wheel",
    "fingerprint_repository",
    "load_marketplace",
    "recommend",
]


def __getattr__(name: str) -> Any:
    if name in {"ExtensionResult", "ExtensionRunner"}:
        from sanka.runtime.extensions.runner import ExtensionResult, ExtensionRunner

        return {"ExtensionResult": ExtensionResult, "ExtensionRunner": ExtensionRunner}[name]
    raise AttributeError(name)
