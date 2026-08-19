# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP and rendering helpers for the public Sanka Migrate research surface."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_API_BASE = "https://api.sanka.com/v2/migrate"
DEFAULT_SIGNUP_BASE = "https://app.sanka.com"
DEFAULT_TIMEOUT_SECONDS = 10.0


class _HttpResponse(Protocol):
    headers: Mapping[str, str]

    def read(self) -> bytes: ...


_Opener = Callable[..., Any]


class SankaMigrateApiError(Exception):
    """A transport or standard-envelope error returned by the public API."""

    def __init__(self, code: str, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


class SankaMigrateApiClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: _Opener = urlopen,
    ) -> None:
        configured_base = base_url or os.environ.get("SANKA_MIGRATE_API_BASE") or DEFAULT_API_BASE
        self._base_url = configured_base.rstrip("/")
        self._timeout = timeout
        self._opener = opener

    def research_eol(
        self,
        *,
        product: str | None,
        category: str | None,
        event_type: str | None,
        after: str | None,
        before: str | None,
        locale: str | None,
    ) -> dict[str, Any]:
        path = "/research/eol"
        if product:
            path += f"/{quote(product, safe='')}"
        return self._request(
            "GET",
            path,
            query={
                "category": category,
                "type": event_type,
                "after": after,
                "before": before,
                "locale": locale,
            },
        )

    def research_tco(
        self,
        *,
        product: str | None,
        category: str | None,
        locale: str | None,
    ) -> dict[str, Any]:
        path = "/research/tco"
        if product:
            path += f"/{quote(product, safe='')}"
        return self._request(
            "GET",
            path,
            query={"category": category, "locale": locale},
        )

    def research_compare(
        self,
        *,
        category: str,
        platforms: str | None,
        locale: str | None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/research/compare/{quote(category, safe='')}",
            query={"platforms": platforms, "locale": locale},
        )

    def assess(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/assessments", payload=payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str | None] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = {key: value for key, value in (query or {}).items() if value not in (None, "")}
        suffix = f"?{urlencode(params)}" if params else ""
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "User-Agent": "sanka-migrate"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}{suffix}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                envelope = _decode_json(response.read())
        except HTTPError as error:
            envelope = _decode_json(error.read(), fallback={})
            details = _error_details(envelope)
            retry_after = error.headers.get("Retry-After") if error.headers else None
            raise SankaMigrateApiError(
                details["code"],
                details["message"],
                retry_after=retry_after,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise SankaMigrateApiError(
                "SANKA_MIGRATE_API_UNAVAILABLE",
                f"Sanka Migrate API request failed: {error}",
            ) from error
        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            details = _error_details(envelope)
            raise SankaMigrateApiError(details["code"], details["message"])
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise SankaMigrateApiError(
                "SANKA_MIGRATE_API_INVALID_RESPONSE",
                "Sanka Migrate API returned an invalid response.",
            )
        return cast(dict[str, Any], data)


def signup_url(assessment_id: str, *, lang: str) -> str:
    base = (os.environ.get("SANKA_MIGRATE_SIGNUP_BASE") or DEFAULT_SIGNUP_BASE).rstrip("/")
    signup_path = "/ja/signup" if lang == "ja" else "/register"
    handoff = urlencode({"lang": lang, "assessment_id": assessment_id})
    return f"{base}{signup_path}?{urlencode({'next': f'/migrate-onboarding?{handoff}'})}"


def print_research(data: dict[str, Any], *, kind: str, locale: str | None) -> int:
    if kind == "eol":
        rows = _eol_rows(data)
        _print_eol(rows, locale=locale)
    elif kind == "tco":
        rows = _tco_rows(data)
        _print_tco(rows, locale=locale)
    else:
        rows = list(data.get("platforms") or [])
        _print_compare(rows, locale=locale)
    _print_attribution(data.get("attribution"))
    return 0 if rows else 2


def _decode_json(raw: bytes, *, fallback: Any = None) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback


def _error_details(envelope: Any) -> dict[str, str]:
    error = envelope.get("error") if isinstance(envelope, dict) else None
    if not isinstance(error, dict):
        return {
            "code": "SANKA_MIGRATE_API_ERROR",
            "message": "Sanka Migrate API request failed.",
        }
    return {
        "code": str(error.get("code") or "SANKA_MIGRATE_API_ERROR"),
        "message": str(error.get("message") or "Sanka Migrate API request failed."),
    }


def _localized(value: Any, locale: str | None) -> str:
    if isinstance(value, dict):
        selected = value.get(locale or "en") or value.get("en") or value.get("ja") or ""
        return str(selected)
    return str(value or "")


def _eol_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    events = data.get("events")
    if isinstance(events, list):
        return [row for row in events if isinstance(row, dict)]
    product = data.get("product")
    if not isinstance(product, dict):
        return []
    slug = str(product.get("slug") or product.get("name") or "")
    return [
        {**row, "product": row.get("product") or {"slug": slug, "name": product.get("name")}}
        for row in product.get("events") or []
        if isinstance(row, dict)
    ]


def _tco_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    benchmarks = data.get("benchmarks")
    if isinstance(benchmarks, list):
        return [row for row in benchmarks if isinstance(row, dict)]
    product = data.get("product")
    if isinstance(product, dict):
        return [row for row in product.get("plans") or [] if isinstance(row, dict)]
    return []


def _print_eol(rows: list[dict[str, Any]], *, locale: str | None) -> None:
    sources: list[tuple[str, str]] = []
    for row in rows:
        product = row.get("product")
        if isinstance(product, dict):
            product_name = _localized(product.get("name"), locale) or str(product.get("slug") or "")
        else:
            product_name = product or ""
        source_indexes = _source_indexes(row.get("citations"), sources)
        references = "" if not source_indexes else " " + "".join(f"[{n}]" for n in source_indexes)
        print(
            f"{row.get('date') or ''!s:<10}  {product_name!s:<24}  "
            f"{row.get('event_type') or row.get('type') or ''!s:<18}  "
            f"{_localized(row.get('change'), locale)}{references}"
        )
    _print_sources(sources)


def _print_tco(rows: list[dict[str, Any]], *, locale: str | None) -> None:
    sources: list[tuple[str, str]] = []
    for row in rows:
        source_indexes = _source_indexes(row.get("citations"), sources)
        references = "" if not source_indexes else " " + "".join(f"[{n}]" for n in source_indexes)
        amount = row.get("price_per_unit")
        price = "n/a" if amount in (None, "") else f"{row.get('currency', '')} {amount}"
        platform_name = _localized(row.get("platform_name"), locale) or str(
            row.get("product") or ""
        )
        print(
            f"{platform_name:<24}  "
            f"{row.get('plan_name') or ''!s:<24}  {price:<16}  "
            f"{row.get('billing_unit') or ''!s:<16}  "
            f"{_localized(row.get('notes'), locale)}{references}"
        )
    _print_sources(sources)


def _print_compare(rows: list[dict[str, Any]], *, locale: str | None) -> None:
    sources: list[tuple[str, str]] = []
    for platform in rows:
        name = _localized(platform.get("name"), locale) or str(platform.get("slug") or "")
        print(name)
        groups = cast(
            dict[str, Any],
            platform.get("facts") if isinstance(platform.get("facts"), dict) else {},
        )
        for group in ("export", "import", "watch"):
            for fact in groups.get(group) or []:
                if not isinstance(fact, dict):
                    continue
                source_indexes = _source_indexes(fact.get("citations"), sources)
                references = (
                    "" if not source_indexes else " " + "".join(f"[{n}]" for n in source_indexes)
                )
                print(f"  {group:<6} {_localized(fact.get('claim'), locale)}{references}")
    _print_sources(sources)


def _source_indexes(value: Any, sources: list[tuple[str, str]]) -> list[int]:
    indexes: list[int] = []
    for citation in value or []:
        if not isinstance(citation, dict) or citation.get("kind") != "vendor_primary":
            continue
        item = (str(citation.get("source_url") or ""), str(citation.get("verified_on") or ""))
        if not item[0]:
            continue
        if item not in sources:
            sources.append(item)
        indexes.append(sources.index(item) + 1)
    return indexes


def _print_sources(sources: list[tuple[str, str]]) -> None:
    if not sources:
        return
    print("\nsources:")
    for index, (url, verified_on) in enumerate(sources, start=1):
        suffix = f"  (verified {verified_on})" if verified_on else ""
        print(f"  [{index}] {url}{suffix}")


def _print_attribution(value: Any) -> None:
    if not isinstance(value, dict):
        return
    name = value.get("name") or "Sanka Migrate Research"
    url = value.get("url") or "https://sanka.com/docs/migrate/"
    license_name = value.get("license") or "CC BY 4.0"
    print(f"\n{name} — {url}  ({license_name})")


__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_SIGNUP_BASE",
    "SankaMigrateApiClient",
    "SankaMigrateApiError",
    "print_research",
    "signup_url",
]
