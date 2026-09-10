# SPDX-License-Identifier: AGPL-3.0-only
"""Model-facing presentation of the existing result, with JSON-escaped values."""

from __future__ import annotations

import json
from typing import Any

_DIAGNOSTICS = {"error", "failures", "warnings", "risks", "coverage_issues", "limitations"}


def _has_diagnostics(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (name in _DIAGNOSTICS and bool(item)) or _has_diagnostics(item)
            for name, item in value.items()
        )
    return isinstance(value, list) and any(_has_diagnostics(item) for item in value)


def _summary(
    value: Any, *, path: tuple[str, ...] = (), artifacts: bool = False, inventory: bool = False
) -> Any:
    if isinstance(value, dict):
        if path in {("files",), ("extension", "files")} and artifacts:
            return {"names": sorted(value), "omitted_file_contents": len(value)}
        result: dict[str, Any] = {}
        for name, item in value.items():
            if inventory and artifacts:
                if not path and name == "extensions" and isinstance(item, list):
                    item = [
                        {
                            **entry,
                            "data": {
                                k: v
                                for k, v in entry["data"].items()
                                if k not in value or value[k] != v
                            },
                        }
                        if isinstance(entry, dict) and isinstance(entry.get("data"), dict)
                        else entry
                        for entry in item
                    ]
                owner = path in {(), ("extension",), ("extensions", "[]", "data")}
                route = len(path) >= 2 and path[-2:] == ("routes", "[]")
                if (
                    (
                        (owner and name in {"serializer_details", "view_details", "status_codes"})
                        or (route and name in {"parity_notes", "options"})
                        or (path == ("fingerprint",) and name == "evidence")
                    )
                    and isinstance(item, (dict, list))
                    and not _has_diagnostics(item)
                ):
                    result[name] = {"count": len(item), "details": "artifacts"}
                    continue
            if name == "extension" and isinstance(item, dict):
                # Core lifecycle responses repeat extension fields; retain differing hashes.
                item = {k: v for k, v in item.items() if k not in value or value[k] != v}
                if not item:
                    continue
            if (
                name == "source"
                and {"classification", "method"} <= value.keys()
                and isinstance(item, str)
                and artifacts
            ):
                result["omitted_source_characters"] = len(item)
            else:
                result[name] = _summary(
                    item,
                    path=(*path, name),
                    artifacts=artifacts,
                    inventory=inventory and name not in _DIAGNOSTICS,
                )
        return result
    if isinstance(value, list):
        return [
            _summary(item, path=(*path, "[]"), artifacts=artifacts, inventory=inventory)
            for item in value
        ]
    return value


def render_compact(payload: dict[str, Any]) -> str:
    """One header, then key=JSON lines. Never truncate failures, warnings or hashes."""
    lines = [" ".join(str(payload[k]) for k in ("command", "outcome", "migration_state"))]
    lines[0] = "sanka-compact/v1 " + lines[0]
    data = _summary(
        payload["data"],
        artifacts=bool(payload.get("artifacts")),
        inventory=payload["command"] in {"scan", "plan"} and payload["outcome"] == "success",
    )
    for key, value in data.items():
        # Escape even extension-defined keys so a newline cannot forge another record.
        label = key if key.isidentifier() else json.dumps(key, ensure_ascii=False)
        lines.append(label + "=" + json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    for key in ("artifacts", "limitations", "next_actions"):
        if payload.get(key):
            lines.append(
                key + "=" + json.dumps(payload[key], ensure_ascii=False, separators=(",", ":"))
            )
    return "\n".join(lines)
