# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

import pytest
from sanka_connector_markdown import CONNECTOR, MarkdownSource

from sanka.connector import ConfigurationError, Credentials


def _credentials(root: Path) -> Credentials:
    return Credentials(provider="markdown", settings={"connection": str(root)})


def _write_content(root: Path) -> None:
    (root / "a.md").write_text(
        "---\ntitle: A\npublished: true\nviews: 10\n---\nAlpha body\n", encoding="utf-8"
    )
    (root / "b.md").write_text(
        "---\ntitle: B\nviews: many\ntags:\n  - x\n  - y\n---\nBeta body\n", encoding="utf-8"
    )
    (root / "c.md").write_text("Plain body, no frontmatter\n", encoding="utf-8")


async def test_inventory_reports_fields_counts_and_warnings(tmp_path: Path) -> None:
    _write_content(tmp_path)
    (tmp_path / "broken.md").write_text("---\ntitle: [unclosed\n---\nbody\n", encoding="utf-8")

    inventory = await MarkdownSource().inventory(_credentials(tmp_path))

    assert len(inventory.objects) == 1
    documents = inventory.objects[0]
    assert documents.record_count == 4
    assert documents.identity_fields == ["path"]
    field_keys = [f.key for f in documents.fields]
    assert field_keys[:3] == ["path", "slug", "content"]
    assert {"title", "published", "views", "tags"} <= set(field_keys)
    assert any("mixed types" in w and "views" in w for w in inventory.warnings)
    assert any("unparseable frontmatter" in w for w in inventory.warnings)


async def test_read_records_paginates_deterministically(tmp_path: Path) -> None:
    _write_content(tmp_path)
    source = MarkdownSource()
    credentials = _credentials(tmp_path)

    first = await source.read_records(
        credentials, object_type="documents", field_keys=["path", "title", "content"], limit=2
    )
    assert [r["path"] for r in first.records] == ["a.md", "b.md"]
    assert first.has_more and first.next_cursor == "2"

    second = await source.read_records(
        credentials,
        object_type="documents",
        field_keys=["path", "title", "content"],
        limit=2,
        cursor=first.next_cursor,
    )
    assert [r["path"] for r in second.records] == ["c.md"]
    assert not second.has_more and second.next_cursor is None
    assert second.records[0]["title"] is None
    assert "Plain body" in second.records[0]["content"]


async def test_count_capability_and_registration(tmp_path: Path) -> None:
    _write_content(tmp_path)
    count = await MarkdownSource().count_records(_credentials(tmp_path), object_type="documents")
    assert count == 3
    assert CONNECTOR.name == "markdown"
    assert CONNECTOR.source is not None and CONNECTOR.destination is None


async def test_missing_directory_is_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        await MarkdownSource().inventory(_credentials(tmp_path / "nope"))
