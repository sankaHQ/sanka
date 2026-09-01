# SPDX-License-Identifier: AGPL-3.0-only
"""Static extension marketplace discovery."""

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
    Recommendation,
    Wheel,
)

__all__ = [
    "ExtensionError",
    "Fingerprint",
    "Manifest",
    "MatchedEvidence",
    "Matcher",
    "Recommendation",
    "Wheel",
    "fingerprint_repository",
    "load_marketplace",
    "recommend",
]
