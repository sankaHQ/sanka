# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only local stage history. Cloud runs stay a live API read."""

from __future__ import annotations

import json
from pathlib import Path

from sanka.cli.tui.model import HistoryEntry

HISTORY_SCHEMA = "sanka-tui-history/v1"
_LIMIT = 20


def history_path(root: Path, artifact_dir: str = ".sanka") -> Path:
    directory = Path(artifact_dir)
    if not directory.is_absolute():
        directory = root / directory
    return directory / "tui-history.json"


def load_history(root: Path, artifact_dir: str = ".sanka") -> tuple[HistoryEntry, ...]:
    path = history_path(root, artifact_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict) or payload.get("schema_version") != HISTORY_SCHEMA:
        return ()
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return ()
    loaded = [item for item in (HistoryEntry.from_dict(entry) for entry in entries) if item]
    return tuple(loaded[:_LIMIT])


def append_history(
    root: Path, entry: HistoryEntry, artifact_dir: str = ".sanka"
) -> tuple[HistoryEntry, ...]:
    current = list(load_history(root, artifact_dir))
    current.insert(0, entry)
    kept = current[:_LIMIT]
    path = history_path(root, artifact_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": HISTORY_SCHEMA,
        "entries": [item.to_dict() for item in kept],
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return tuple(kept)
