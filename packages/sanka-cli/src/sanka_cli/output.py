# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

console = Console()
error_console = Console(stderr=True)


def resolve_output_format(value: str | None) -> str:
    if value:
        return value
    return "table" if sys.stdout.isatty() else "json"


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None:
        return ""
    return str(value)


def print_payload(payload: Any, *, output_format: str | None = None) -> None:
    resolved_output = resolve_output_format(output_format)
    if resolved_output == "json":
        console.print_json(json=json.dumps(payload, ensure_ascii=False, default=str))
        return

    data = payload
    if isinstance(payload, dict) and "data" in payload and len(payload) <= 4:
        data = payload["data"]

    if isinstance(data, list):
        _print_rows(data)
        return
    if isinstance(data, dict):
        _print_mapping(data)
        return

    console.print(_stringify(data))


def _print_rows(rows: list[Any]) -> None:
    if not rows:
        console.print("No results.")
        return
    if not all(isinstance(row, dict) for row in rows):
        for row in rows:
            console.print(_stringify(row))
        return

    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key in seen:
                continue
            seen.add(key)
            columns.append(str(key))

    table = Table(show_header=True, header_style="bold")
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*[_stringify(row.get(column)) for column in columns])
    console.print(table)


def _print_mapping(mapping: dict[str, Any]) -> None:
    table = Table(show_header=False, box=None)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for key, value in mapping.items():
        table.add_row(str(key), _stringify(value))
    console.print(table)


def print_error(message: str) -> None:
    error_console.print(message)
