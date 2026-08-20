# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

import pytest

from sanka.connector import ConfigurationError, Credentials, DataError, SupportsRecordCounts
from sanka_connector_csv import CONNECTOR, CsvSource


def _credentials(path: Path) -> Credentials:
    return Credentials(provider="csv", settings={"connection": str(path)})


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


async def test_delimiter_sniffing_csv_and_tsv(tmp_path: Path) -> None:
    semicolons = _write(tmp_path / "orders.csv", "id;total\n1;9.99\n2;12.50\n")
    orders = (await CsvSource().inventory(_credentials(semicolons))).objects[0]
    assert orders.key == "orders"
    assert [f.key for f in orders.fields] == ["id", "total"]
    assert orders.record_count == 2

    tabs = _write(tmp_path / "Items List.tsv", "sku\tqty\nA-1\t3\nB-2\t5\n")
    items = (await CsvSource().inventory(_credentials(tabs))).objects[0]
    assert items.key == "items_list"
    assert items.label == "Items List"
    assert [f.key for f in items.fields] == ["row", "sku", "qty"]
    assert items.record_count == 2


async def test_single_column_files_fall_back_to_default_delimiters(tmp_path: Path) -> None:
    plain = _write(tmp_path / "names.csv", "name\nalpha\nbeta\n")
    page = await CsvSource().read_records(
        _credentials(plain), object_type="names", field_keys=["name"], limit=10
    )
    assert [r["name"] for r in page.records] == ["alpha", "beta"]

    tsv = _write(tmp_path / "names.tsv", "name\ngamma\n")
    page = await CsvSource().read_records(
        _credentials(tsv), object_type="names", field_keys=["name"], limit=10
    )
    assert [r["name"] for r in page.records] == ["gamma"]


async def test_inventory_with_id_column_infers_types(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "products.csv",
        "ID,name,price,active,mixed\n1,Widget,9.99,true,1\n2,Gadget,12,false,x\n3,Gizmo,,TRUE,\n",
    )
    inventory = await CsvSource().inventory(_credentials(path))
    products = inventory.objects[0]
    assert products.key == "products"
    assert products.record_count == 3
    assert products.identity_fields == ["ID"]
    assert {f.key: f.data_type for f in products.fields} == {
        "ID": "number",
        "name": "string",
        "price": "number",
        "active": "boolean",
        "mixed": "string",
    }
    assert any("mixed" in w and "mixed types" in w for w in inventory.warnings)
    assert not any("synthesized" in w for w in inventory.warnings)


async def test_inventory_without_id_synthesizes_row_identity(tmp_path: Path) -> None:
    path = _write(tmp_path / "notes.csv", "title,body\nA,alpha\nB,beta\n")
    inventory = await CsvSource().inventory(_credentials(path))
    notes = inventory.objects[0]
    assert notes.identity_fields == ["row"]
    assert notes.fields[0].key == "row"
    assert notes.fields[0].data_type == "number"
    assert any("synthesized" in w for w in inventory.warnings)


async def test_read_records_paginates_deterministically(tmp_path: Path) -> None:
    path = _write(tmp_path / "notes.csv", "title,body\nA,alpha\nB,beta\nC,gamma\n")
    source = CsvSource()
    credentials = _credentials(path)

    first = await source.read_records(
        credentials, object_type="notes", field_keys=["row", "title", "missing"], limit=2
    )
    assert first.records == [
        {"row": 1, "title": "A", "missing": None},
        {"row": 2, "title": "B", "missing": None},
    ]
    assert first.has_more and first.next_cursor == "2"

    second = await source.read_records(
        credentials,
        object_type="notes",
        field_keys=["row", "title", "missing"],
        limit=2,
        cursor=first.next_cursor,
    )
    assert second.records == [{"row": 3, "title": "C", "missing": None}]
    assert not second.has_more and second.next_cursor is None

    with pytest.raises(DataError):
        await source.read_records(credentials, object_type="other", field_keys=["title"], limit=1)


async def test_record_values_stay_strings(tmp_path: Path) -> None:
    path = _write(tmp_path / "products.csv", "id,price,active\n1,9.99,true\n")
    page = await CsvSource().read_records(
        _credentials(path), object_type="products", field_keys=["id", "price", "active"], limit=1
    )
    assert page.records == [{"id": "1", "price": "9.99", "active": "true"}]


async def test_count_capability(tmp_path: Path) -> None:
    path = _write(tmp_path / "notes.csv", "title\nA\nB\nC\n")
    source = CsvSource()
    assert await source.count_records(_credentials(path), object_type="notes") == 3
    assert isinstance(source, SupportsRecordCounts)


async def test_missing_and_empty_files_are_configuration_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        await CsvSource().inventory(_credentials(tmp_path / "absent.csv"))
    empty = _write(tmp_path / "empty.csv", "")
    with pytest.raises(ConfigurationError):
        await CsvSource().inventory(_credentials(empty))


def test_registration_shape() -> None:
    assert CONNECTOR.name == "csv"
    assert CONNECTOR.source is not None
    assert CONNECTOR.destination is None
