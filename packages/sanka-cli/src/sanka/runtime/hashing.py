# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical hashing for plans, selections, and specs.

Approvals bind to hashes: ``apply`` takes the hash of the reviewed plan, and
changing anything reviewed must change the hash. Canonical form is strict
JSON — sorted keys, minimal separators, no NaN/Infinity, UTF-8 — so the same
logical content always yields the same hash regardless of construction order.
Only JSON-native types are hashable; anything else is a ``TypeError`` by
design (coerce deliberately at the call site, never implicitly here).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

HASH_PREFIX = "sha256:"


def canonical_json(value: Any) -> str:
    """Render ``value`` (JSON-native types only) in canonical form."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    """``sha256:<hex>`` over the canonical JSON form of ``value``."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"
