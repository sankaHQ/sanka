# SPDX-License-Identifier: AGPL-3.0-only
"""Model-facing presentation of the existing result, with JSON-escaped values."""

from __future__ import annotations

import json
from typing import Any


def _summary(value: Any, *, path: tuple[str, ...] = (), artifacts: bool = False) -> Any:
    if isinstance(value, dict):
        if path in {("files",), ("extension", "files")} and artifacts:
            return {"names": sorted(value), "omitted_file_contents": len(value)}
        result = {}
        for name, item in value.items():
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
                result[name] = _summary(item, path=(*path, name), artifacts=artifacts)
        return result
    if isinstance(value, list):
        return [_summary(item, path=(*path, "[]"), artifacts=artifacts) for item in value]
    return value


def render_compact(payload: dict[str, Any]) -> str:
    """One header, then key=JSON lines. Never truncate failures, warnings or hashes."""
    lines = [" ".join(str(payload[k]) for k in ("command", "outcome", "migration_state"))]
    lines[0] = "sanka-compact/v1 " + lines[0]
    data = _summary(payload["data"], artifacts=bool(payload.get("artifacts")))
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
