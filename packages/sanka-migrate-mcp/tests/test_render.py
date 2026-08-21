# SPDX-License-Identifier: Apache-2.0
"""Agent-facing payload shaping stays bounded and citation preserving."""

from __future__ import annotations

from sanka_migrate_mcp.render import (
    MAX_ITEMS,
    render_assessment,
    render_compare,
    render_eol,
)


def _citation(index: int) -> dict[str, str]:
    return {
        "kind": "vendor_primary",
        "source_url": f"https://vendor.example/{index}",
        "verified_on": "2026-08-19",
    }


def test_eol_is_bounded_localized_and_keeps_terminal_attribution() -> None:
    data = {
        "events": [
            {
                "product": {"name": {"en": "Example", "ja": "例"}},
                "date": f"2027-01-{(index % 28) + 1:02d}",
                "event_type": "shutdown",
                "change": {"en": f"Change {index}", "ja": f"変更 {index}"},
                "citations": [_citation(index)],
            }
            for index in range(MAX_ITEMS + 1)
        ],
        "dataset": {"name": "eol", "version": "test"},
        "attribution": {
            "name": "Sanka Research",
            "url": "https://sanka.com/docs/migrate/eol/",
            "license": "CC BY 4.0",
        },
    }

    result = render_eol(data, locale="ja")

    assert len(result["events"]) == MAX_ITEMS
    assert result["truncated"] is True
    assert result["events"][0]["product"] == "例"
    assert result["events"][0]["change"] == "変更 0"
    assert result["events"][0]["citations"] == [_citation(0)]
    assert list(result)[-1] == "attribution"
    assert "Cite the vendor source_url" in result["attribution"]


def test_compare_bounds_nested_fact_lists_and_preserves_context() -> None:
    facts = [
        {
            "claim_key": f"claim-{index}",
            "claim": {"en": f"Claim {index}", "ja": f"主張 {index}"},
            "citations": [_citation(index)],
        }
        for index in range(MAX_ITEMS + 1)
    ]
    result = render_compare(
        {
            "category": "crm-migration",
            "platforms": [
                {
                    "slug": "example",
                    "name": {"en": "Example", "ja": "例"},
                    "facts": {"export": facts, "import": [], "watch": []},
                }
            ],
            "dataset": {"name": "compare", "version": "test"},
        },
        locale="en",
    )

    assert result["category"] == "crm-migration"
    assert result["truncated"] is True
    assert len(result["platforms"][0]["facts"]["export"]) == MAX_ITEMS
    assert result["platforms"][0]["facts"]["export"][0]["citations"] == [_citation(0)]


def test_assessment_returns_branded_handoff_without_starting_a_migration() -> None:
    result = render_assessment({"assessment_id": "assessment-123"}, lang="ja")

    assert result["assessment_id"] == "assessment-123"
    assert result["signup_url"].startswith("https://app.sanka.com/ja/signup?")
    assert "migrate-onboarding" in result["signup_url"]
    assert "assessment-123" in result["signup_url"]
    assert "builds the grounded migration report" in result["next_steps"]
