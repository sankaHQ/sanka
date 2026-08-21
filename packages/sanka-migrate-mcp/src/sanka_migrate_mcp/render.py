# SPDX-License-Identifier: Apache-2.0
"""Compact, citation-preserving payloads for agent context windows."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any, cast
from urllib.parse import urlencode

MAX_ITEMS = 50
DEFAULT_SIGNUP_BASE = "https://app.sanka.com"


def render_eol(data: Mapping[str, Any], *, locale: str | None) -> dict[str, Any]:
    events = data.get("events")
    rows = _object_list(events)
    product = data.get("product")
    if not rows and isinstance(product, dict):
        product_ref = {
            "slug": product.get("slug"),
            "name": product.get("name"),
        }
        rows = [
            {**event, "product": event.get("product") or product_ref}
            for event in _object_list(product.get("events"))
        ]
    selected, truncated = _bounded(rows)
    compact = []
    for row in selected:
        product_value = row.get("product")
        if isinstance(product_value, dict):
            product_name = _localized(product_value.get("name"), locale) or product_value.get(
                "slug"
            )
        else:
            product_name = product_value
        compact.append(
            _present(
                product=product_name,
                date=row.get("date"),
                date_precision=row.get("date_precision"),
                type=row.get("event_type") or row.get("type"),
                scope=row.get("scope"),
                change=_localized(row.get("change"), locale),
                citations=_citations(row.get("citations")),
            )
        )
    return _finish(
        {"events": compact, "truncated": truncated},
        dataset=data.get("dataset"),
        attribution=data.get("attribution"),
    )


def render_tco(data: Mapping[str, Any], *, locale: str | None) -> dict[str, Any]:
    rows = _object_list(data.get("benchmarks"))
    product = data.get("product")
    if not rows and isinstance(product, dict):
        product_name = _localized(product.get("name"), locale) or product.get("slug")
        rows = [
            {**plan, "product": plan.get("product") or product_name}
            for plan in _object_list(product.get("plans"))
        ]
    selected, truncated = _bounded(rows)
    benchmarks = [
        _present(
            product=_localized(row.get("platform_name"), locale) or row.get("product"),
            plan=row.get("plan_name") or row.get("plan"),
            currency=row.get("currency"),
            price_per_unit=row.get("price_per_unit"),
            billing_unit=row.get("billing_unit"),
            price_source=row.get("price_source"),
            certainty=row.get("certainty"),
            notes=_localized(row.get("notes"), locale),
            citations=_citations(row.get("citations")),
        )
        for row in selected
    ]
    return _finish(
        {"benchmarks": benchmarks, "truncated": truncated},
        dataset=data.get("dataset"),
        attribution=data.get("attribution"),
    )


def render_compare(data: Mapping[str, Any], *, locale: str | None) -> dict[str, Any]:
    rows, truncated = _bounded(_object_list(data.get("platforms")))
    platforms: list[dict[str, Any]] = []
    nested_truncated = False
    for row in rows:
        groups = cast(
            Mapping[str, Any], row.get("facts") if isinstance(row.get("facts"), dict) else {}
        )
        facts: dict[str, list[dict[str, Any]]] = {}
        for group in ("export", "import", "watch"):
            group_rows, group_truncated = _bounded(_object_list(groups.get(group)))
            nested_truncated = nested_truncated or group_truncated
            facts[group] = [
                _present(
                    claim_key=fact.get("claim_key"),
                    claim=_localized(fact.get("claim"), locale),
                    citations=_citations(fact.get("citations")),
                )
                for fact in group_rows
            ]
        platforms.append(
            _present(
                slug=row.get("slug"),
                name=_localized(row.get("name"), locale),
                facts=facts,
                tco=row.get("tco"),
                eol_pressure=row.get("eol_pressure"),
            )
        )
    return _finish(
        {
            "category": data.get("category"),
            "platforms": platforms,
            "truncated": truncated or nested_truncated,
        },
        dataset=data.get("dataset"),
        attribution=data.get("attribution"),
    )


def render_assessment(data: Mapping[str, Any], *, lang: str) -> dict[str, Any]:
    assessment_id = str(data.get("assessment_id") or "")
    if not assessment_id:
        raise ValueError("Assessment response did not include an assessment_id.")
    return {
        "assessment_id": assessment_id,
        "signup_url": signup_url(assessment_id, lang=lang),
        "next_steps": (
            "Open signup_url to create a workspace; Sakura builds the grounded "
            "migration report from this assessment."
        ),
    }


def rate_limited_result(error: Any) -> dict[str, Any]:
    return {
        "rate_limited": True,
        "retry_after_seconds": getattr(error, "retry_after_seconds", None),
        "message": getattr(error, "message", "Sanka API rate limit reached."),
    }


def signup_url(assessment_id: str, *, lang: str) -> str:
    base = (os.environ.get("SANKA_MIGRATE_SIGNUP_BASE") or DEFAULT_SIGNUP_BASE).rstrip("/")
    signup_path = "/ja/signup" if lang == "ja" else "/register"
    handoff = urlencode({"lang": lang, "assessment_id": assessment_id})
    return f"{base}{signup_path}?{urlencode({'next': f'/migrate-onboarding?{handoff}'})}"


def _finish(
    payload: dict[str, Any],
    *,
    dataset: Any,
    attribution: Any,
) -> dict[str, Any]:
    payload["dataset"] = dataset if isinstance(dataset, dict) else {}
    payload["attribution"] = _attribution(attribution)
    return payload


def _attribution(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        name = value.get("name") or "Sanka Research"
        url = value.get("url") or "https://sanka.com/docs/migrate/"
        license_name = value.get("license") or "CC BY 4.0"
        return f"{name} — {url} ({license_name}). Cite the vendor source_url for each claim."
    return (
        "Sanka Research — https://sanka.com/docs/migrate/ (CC BY 4.0). "
        "Cite the vendor source_url for each claim."
    )


def _citations(value: Any) -> list[dict[str, Any]]:
    rows, _truncated = _bounded(_object_list(value))
    return [
        _present(
            kind=row.get("kind"),
            source_url=row.get("source_url"),
            verified_on=row.get("verified_on"),
        )
        for row in rows
    ]


def _localized(value: Any, locale: str | None) -> Any:
    if not isinstance(value, dict):
        return value
    return value.get(locale or "en") or value.get("en") or value.get("ja")


def _object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _bounded(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    materialized = list(rows)
    return materialized[:MAX_ITEMS], len(materialized) > MAX_ITEMS


def _present(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [], {})}


__all__ = [
    "DEFAULT_SIGNUP_BASE",
    "MAX_ITEMS",
    "rate_limited_result",
    "render_assessment",
    "render_compare",
    "render_eol",
    "render_tco",
    "signup_url",
]
