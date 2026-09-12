# SPDX-License-Identifier: AGPL-3.0-only
"""Three-way configuration reconciliation without mutating user-owned edits.

Objects merge by field. Lists are atomic because their order can encode workflow
semantics; guessing a list identity would risk removing or reordering user work.
Missing fields, nulls, booleans and numbers retain their distinct JSON meanings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sanka.runtime.flow.model import Document
from sanka.runtime.hashing import canonical_json

_MISSING = object()


def _equal(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    return canonical_json(left) == canonical_json(right)


def _field_state(value: Any) -> dict[str, Any]:
    if value is _MISSING:
        return {"present": False}
    return {"present": True, "value": value}


@dataclass(frozen=True, slots=True)
class FieldConflict:
    path: tuple[str, ...]
    _document: str

    def to_dict(self) -> dict[str, Any]:
        return dict(json.loads(self._document))


@dataclass(frozen=True, slots=True)
class MergeResult:
    _configuration: str
    conflicts: tuple[FieldConflict, ...]
    preserved_paths: tuple[tuple[str, ...], ...]

    @property
    def configuration(self) -> dict[str, Any]:
        return dict(json.loads(self._configuration))


def merge_configuration(
    *, previous: dict[str, Any], current: dict[str, Any], desired: dict[str, Any]
) -> MergeResult:
    """Propose a merge; conflicts retain current values and must block apply.

    Callers own resource identity and revision checks. This function reconciles
    only one already-owned resource's configuration, never name-based adoption.
    """
    # Snapshot the inputs before traversal; canonical JSON also rejects NaN and
    # non-serializable values. Field paths stay structured, including dots/slashes.
    before, observed, wanted = (Document(value).to_dict() for value in (previous, current, desired))
    conflicts: list[FieldConflict] = []
    preserved: list[tuple[str, ...]] = []

    def merge(old: Any, now: Any, new: Any, path: tuple[str, ...]) -> Any:
        if _equal(now, new):
            return now
        if _equal(old, now):
            return new
        if _equal(old, new):
            if isinstance(old, dict) and isinstance(now, dict):
                # Preserve precise field paths even when the whole desired
                # object is unchanged; plans can explain each preserved edit.
                return merge_fields(old, now, new, path)
            preserved.append(path)
            return now
        if all(isinstance(value, dict) for value in (old, now, new)):
            return merge_fields(old, now, new, path)
        conflicts.append(
            FieldConflict(
                path,
                canonical_json(
                    {
                        "path": list(path),
                        "previous": _field_state(old),
                        "current": _field_state(now),
                        "desired": _field_state(new),
                    }
                ),
            )
        )
        return now

    def merge_fields(
        old: dict[str, Any], now: dict[str, Any], new: dict[str, Any], path: tuple[str, ...]
    ) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key in sorted(old.keys() | now.keys() | new.keys()):
            value = merge(
                old.get(key, _MISSING), now.get(key, _MISSING), new.get(key, _MISSING), (*path, key)
            )
            if value is not _MISSING:
                output[key] = value
        return output

    configuration = merge_fields(before, observed, wanted, ())
    return MergeResult(canonical_json(configuration), tuple(conflicts), tuple(preserved))
