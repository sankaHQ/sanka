# SPDX-License-Identifier: AGPL-3.0-only
"""Plan settings that an installed extension declares inside its own wheel.

The declaration is package data (``<package>/sanka-extension-settings.json``) because
``sanka-extension-manifest/v2`` accepts only its exact keys. A missing or invalid
declaration is ignored so the caller keeps its built-in form.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SETTINGS_FILE = "sanka-extension-settings.json"
SCHEMA_VERSION = "sanka-extension-settings/v1"
MAX_SETTINGS_BYTES = 64 * 1024
TYPES = {"choice", "boolean", "integer", "text", "path"}
STAGES = {"scan", "plan", "apply", "test", "verify"}
_ENTRY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*/" + re.escape(SETTINGS_FILE))

Value = str | bool | int | None


@dataclass(frozen=True, slots=True)
class Choice:
    value: str
    label: dict[str, str]


@dataclass(frozen=True, slots=True)
class Setting:
    id: str
    stage: str
    type: str
    default: Value
    label: dict[str, str]
    description: dict[str, str] | None = None
    choices: tuple[Choice, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    optional: bool = False
    advanced: bool = False
    when: tuple[tuple[str, Value], ...] = ()

    def text(self, field: dict[str, str] | None, locale: str = "en") -> str:
        return (field or {}).get(locale) or (field or {}).get("en", "")

    def visible(self, values: dict[str, Any]) -> bool:
        return all(values.get(key) == value for key, value in self.when)


@dataclass(frozen=True, slots=True)
class ExtensionSettings:
    name: dict[str, str]
    settings: tuple[Setting, ...]

    def for_stage(self, stage: str) -> tuple[Setting, ...]:
        return tuple(item for item in self.settings if item.stage == stage)


def _label(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key), str) and value[key] for key in ("en", "ja")
    ):
        raise ValueError(f"{field} needs English and Japanese text")
    return {"en": value["en"], "ja": value["ja"]}


def parse_settings(document: Any) -> ExtensionSettings:
    """Validate one declaration; raises ValueError on anything a form cannot render."""
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unknown settings schema")
    display = document.get("display")
    name = _label(display.get("name") if isinstance(display, dict) else None, "display.name")
    raw = document.get("settings")
    if not isinstance(raw, list):
        raise ValueError("settings must be a list")
    ids = [item.get("id") for item in raw if isinstance(item, dict)]
    if len(ids) != len(raw) or len(set(ids)) != len(ids):
        raise ValueError("setting ids must be unique")
    settings = []
    for item in raw:
        key, kind, default = item.get("id"), item.get("type"), item.get("default")
        if not isinstance(key, str) or kind not in TYPES or item.get("stage") not in STAGES:
            raise ValueError(f"{key}: id, type and stage are required")
        optional = item.get("optional") is True
        choices: tuple[Choice, ...] = ()
        if kind == "choice":
            raw_choices = item.get("choices")
            if not isinstance(raw_choices, list) or not raw_choices:
                raise ValueError(f"{key}: choices are required")
            choices = tuple(
                Choice(value=c["value"], label=_label(c.get("label"), f"{key} choice"))
                for c in raw_choices
                if isinstance(c, dict) and isinstance(c.get("value"), str)
            )
            values = [choice.value for choice in choices]
            if len(choices) != len(raw_choices) or len(set(values)) != len(values):
                raise ValueError(f"{key}: choices need unique string values")
            if default not in values:
                raise ValueError(f"{key}: default must be one of its choices")
        elif kind == "boolean" and not isinstance(default, bool):
            raise ValueError(f"{key}: default must be a boolean")
        elif kind == "integer":
            low, high = item.get("minimum"), item.get("maximum")
            if (
                not (type(default) is int and type(low) is int and type(high) is int)
                or not low <= default <= high
            ):
                raise ValueError(f"{key}: default must sit within minimum and maximum")
        elif kind in {"text", "path"} and not (
            isinstance(default, str) or (default is None and optional)
        ):
            raise ValueError(f"{key}: default must be a string unless optional")
        when = item.get("when", {})
        if not isinstance(when, dict) or any(k not in ids or k == key for k in when):
            raise ValueError(f"{key}: when must refer to other settings")
        settings.append(
            Setting(
                id=key,
                stage=item["stage"],
                type=kind,
                default=default,
                label=_label(item.get("label"), f"{key} label"),
                description=(
                    _label(item["description"], f"{key} description")
                    if "description" in item
                    else None
                ),
                choices=choices,
                minimum=item.get("minimum"),
                maximum=item.get("maximum"),
                optional=optional,
                advanced=item.get("advanced") is True,
                when=tuple(sorted(when.items())),
            )
        )
    return ExtensionSettings(name=name, settings=tuple(settings))


def read_wheel_settings(path: Path, sha256: str) -> ExtensionSettings | None:
    """The declaration inside one verified cached wheel, or None when it has none."""
    try:
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != sha256:
            return None
        with zipfile.ZipFile(path) as archive:
            entries = [info for info in archive.infolist() if _ENTRY.fullmatch(info.filename)]
            if len(entries) != 1 or entries[0].file_size > MAX_SETTINGS_BYTES:
                return None
            raw = archive.read(entries[0])
        return parse_settings(json.loads(raw))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        return None
