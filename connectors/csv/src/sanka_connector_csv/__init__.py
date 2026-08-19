# SPDX-License-Identifier: Apache-2.0
"""CSV file source connector.

One ``.csv`` (or ``.tsv``) file is one migratable object: the sanitized file
stem is the object key, the header row defines the fields, and every data row
becomes one record. Rows are read in file order so pagination cursors are
deterministic. Values are passed through as the strings the file contains —
number/boolean inference informs the inventory schema only. A column named
``id`` (case-insensitive) is the identity; otherwise a synthetic ``row``
field (the 1-based data-row index) is exposed and used, with a warning.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sanka.connector import (
    ConfigurationError,
    ConnectorRegistration,
    Credentials,
    DataError,
    FieldSchema,
    Inventory,
    ObjectSchema,
    RecordPage,
    SourceFilter,
    SourceObject,
)

_ROW_FIELD = "row"
_KEY_SANITIZER = re.compile(r"[^a-z0-9_]+")
_SNIFF_DELIMITERS = ",\t;|"
_SNIFF_SAMPLE_CHARS = 8192


@dataclass(frozen=True, slots=True, kw_only=True)
class _CsvFile:
    key: str
    label: str
    headers: list[str]
    rows: list[dict[str, str]]

    @property
    def identity_header(self) -> str | None:
        """The header acting as the identity, or ``None`` when synthesized."""
        return next((header for header in self.headers if header.lower() == "id"), None)


class CsvSource:
    provider = "csv"
    binding_kind = "files"

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        parsed = self._load(credentials)
        return [
            SourceObject(
                key=parsed.key,
                label=parsed.label,
                canonical_type=parsed.key,
                default_selected=True,
            )
        ]

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        parsed = self._load(credentials)
        synthesize = parsed.identity_header is None
        column_types = _infer_column_types(parsed)

        warnings: list[str] = []
        fields: list[FieldSchema] = []
        if synthesize:
            fields.append(
                FieldSchema(
                    key=_ROW_FIELD, label="Row", data_type="number", required=True, unique=True
                )
            )
            warnings.append(
                "no 'id' column; the 1-based row index is exposed as field "
                f"{_ROW_FIELD!r} and synthesized as the identity"
            )
        emitted: set[str] = set()
        for header in parsed.headers:
            if header in emitted:
                warnings.append(f"duplicate column {header!r}; the last occurrence wins")
                continue
            emitted.add(header)
            if synthesize and header == _ROW_FIELD:
                warnings.append(
                    f"column {_ROW_FIELD!r} collides with the synthesized identity field; ignored"
                )
                continue
            observed = column_types.get(header, set())
            if len(observed) > 1:
                warnings.append(
                    f"column {header!r} has mixed types ({', '.join(sorted(observed))})"
                )
            fields.append(FieldSchema(key=header, label=header, data_type=_pick_type(observed)))

        identity = parsed.identity_header
        return Inventory(
            provider=self.provider,
            connection_id=credentials.connection_id,
            objects=[
                ObjectSchema(
                    key=parsed.key,
                    label=parsed.label,
                    canonical_type=parsed.key,
                    record_count=len(parsed.rows),
                    fields=fields,
                    identity_fields=[identity if identity is not None else _ROW_FIELD],
                )
            ],
            warnings=warnings,
        )

    async def read_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None = None,
        source_filter: SourceFilter | None = None,
    ) -> RecordPage:
        parsed = self._load(credentials)
        if object_type != parsed.key:
            raise DataError(f"csv source has no object type {object_type!r}")
        synthesize = parsed.identity_header is None
        start = int(cursor) if cursor else 0
        page = parsed.rows[start : start + max(1, limit)]
        next_start = start + len(page)
        records: list[dict[str, Any]] = []
        for offset, row in enumerate(page):
            full: dict[str, Any] = dict(row)
            if synthesize:
                full[_ROW_FIELD] = start + offset + 1
            records.append({key: full.get(key) for key in field_keys})
        return RecordPage(
            object_key=object_type,
            records=records,
            next_cursor=str(next_start) if next_start < len(parsed.rows) else None,
            has_more=next_start < len(parsed.rows),
        )

    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int:
        parsed = self._load(credentials)
        if object_type != parsed.key:
            raise DataError(f"csv source has no object type {object_type!r}")
        return len(parsed.rows)

    # -- internals ----------------------------------------------------------

    def _load(self, credentials: Credentials) -> _CsvFile:
        path = self._path(credentials)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as error:
            raise DataError(f"csv source file is not UTF-8 text: {path}") from error
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(path, text))
        rows = [row for row in reader if row]
        if not rows:
            raise ConfigurationError(f"csv source file has no header row: {path}")
        headers = [header.strip() for header in rows[0]]
        if any(not header for header in headers):
            raise ConfigurationError(f"csv source header row has empty column names: {path}")
        return _CsvFile(
            key=_object_key(path.stem),
            label=path.stem,
            headers=headers,
            rows=[dict(zip(headers, row, strict=False)) for row in rows[1:]],
        )

    def _path(self, credentials: Credentials) -> Path:
        raw = credentials.settings.get("connection") or credentials.settings.get("path")
        if not raw:
            raise ConfigurationError("csv source needs a file path (connection)")
        path = Path(str(raw)).expanduser()
        if not path.is_file():
            raise ConfigurationError(f"csv source file not found: {path}")
        return path


def _object_key(stem: str) -> str:
    normalized = _KEY_SANITIZER.sub("_", stem.strip().lower()).strip("_")
    if not normalized:
        raise ConfigurationError(f"cannot derive an object key from file name {stem!r}")
    return normalized


def _delimiter(path: Path, text: str) -> str:
    sample = text[:_SNIFF_SAMPLE_CHARS]
    try:
        return csv.Sniffer().sniff(sample, delimiters=_SNIFF_DELIMITERS).delimiter
    except csv.Error:
        return "\t" if path.suffix.lower() == ".tsv" else ","


def _infer_column_types(parsed: _CsvFile) -> dict[str, set[str]]:
    types: dict[str, set[str]] = {}
    for row in parsed.rows:
        for header, cell in row.items():
            stripped = cell.strip()
            if not stripped:
                continue
            types.setdefault(header, set()).add(_cell_type(stripped))
    return types


def _cell_type(text: str) -> str:
    if text.lower() in ("true", "false"):
        return "boolean"
    try:
        float(text)
    except ValueError:
        return "string"
    return "number"


def _pick_type(types: set[str]) -> str:
    return next(iter(types)) if len(types) == 1 else "string"


CONNECTOR = ConnectorRegistration(name="csv", source=CsvSource())
