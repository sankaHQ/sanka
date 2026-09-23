# SPDX-License-Identifier: Apache-2.0
"""Paged input/oracle artifacts for native billing verification, without I/O.

Fixture pages are admitted by the host before a plan is approved. Source values
must be synthetic, and expectations must be independent of the importer being
tested. The artifact hash covers all data. Native IDs are allocated in isolation
and bound to source/customer keys by the host; IDs are never inferred from names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Self, cast

from sanka_extensions.flow._wire import (
    FrozenJson,
    WireRecord,
    array,
    artifact_digest,
    boolean,
    canonical_json,
    choice,
    identifier,
    identifiers,
    instances,
    object_fields,
    text,
    unique,
)
from sanka_extensions.flow.definition import JsonValue
from sanka_extensions.flow.identity import ArtifactIdentity
from sanka_extensions.flow.native_verification import NativeBillingScenario

NATIVE_BILLING_FIXTURE_PAGE = "sanka-flow-native-billing-fixture-page/v1"
NATIVE_BILLING_FIXTURE_MANIFEST = "sanka-flow-native-billing-fixture/v1"
MAX_FIXTURE_PAGE_RECORDS = 100
MAX_FIXTURE_PAGE_BYTES = 256 * 1024


def decimal_value(value: object, label: str) -> str:
    """Compare money and quantities as canonical decimal strings, never floats."""
    if type(value) is not str or not value or len(value) > 64:
        raise ValueError(f"{label} requires a bounded canonical decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} requires a canonical decimal string") from error
    if not number.is_finite() or abs(number.adjusted()) > 32:
        raise ValueError(f"{label} requires a bounded finite decimal")
    canonical = format(number, "f") if number else "0"
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if value != canonical:
        raise ValueError(f"{label} requires a canonical decimal string")
    return value


def billing_fields(value: object, *, invoice: bool, existing: bool = False) -> dict[str, JsonValue]:
    if type(value) is not dict:
        raise ValueError("billing expectation fields must be an object")
    required = {
        "currency",
        "total_price",
        "total_price_without_tax",
        "tax",
        "tax_inclusive",
        "line_items",
    }
    if invoice:
        required |= {"status", "invoice_date", "due_date"}
    if not required <= value.keys():
        raise ValueError(
            "billing expectations must assert currency, totals, tax, lines and invoice dates"
        )
    for key in value:
        identifier(key, "expected field")
    currency = text(value["currency"], "currency")
    if (
        len(currency) != 3
        or not currency.isascii()
        or not currency.isupper()
        or not currency.isalpha()
    ):
        raise ValueError("currency must be a three-letter uppercase code")
    for key in ("total_price", "total_price_without_tax", "tax"):
        decimal_value(value[key], key)
    boolean(value["tax_inclusive"], "tax_inclusive")
    lines = value["line_items"]
    if type(lines) is not list or not 1 <= len(lines) <= 100:
        raise ValueError("billing expectations require one to 100 complete line items")
    line_keys: list[str] = []
    for line in lines:
        if (
            type(line) is not dict
            or not {"key", "name", "quantity", "unit_price", "tax_rate"} <= line.keys()
        ):
            raise ValueError(
                "line expectations must assert identity, name, quantity, price and tax"
            )
        line_keys.append(identifier(line["key"], "line key"))
        text(line["name"], "line name")
        for key in ("quantity", "unit_price", "tax_rate"):
            decimal_value(line[key], key)
    if len(line_keys) != len(set(line_keys)):
        raise ValueError("expected line keys must be unique")
    if invoice:
        if existing:
            text(value["status"], "existing invoice status")
        elif value["status"] != "draft":
            raise ValueError("new invoices must remain draft")
        for key in ("invoice_date", "due_date"):
            stamp = text(value[key], key)
            if date.fromisoformat(stamp).isoformat() != stamp:
                raise ValueError("invoice dates must use YYYY-MM-DD")
    return cast(dict[str, JsonValue], FrozenJson(value).value)


@dataclass(frozen=True, slots=True, init=False)
class NativeBillingFixtureRecord(WireRecord):
    source_key: str
    customer_key: str
    _source: FrozenJson = field(repr=False)
    _order: FrozenJson = field(repr=False)
    _invoice: FrozenJson = field(repr=False)
    _initial_order: FrozenJson = field(repr=False)
    _initial_invoice: FrozenJson = field(repr=False)

    def __init__(
        self,
        source_key: str,
        customer_key: str,
        source: dict[str, JsonValue],
        order: dict[str, JsonValue],
        invoice: dict[str, JsonValue],
        initial_order: dict[str, JsonValue] | None = None,
        initial_invoice: dict[str, JsonValue] | None = None,
    ) -> None:
        identifier(source_key, "source key")
        identifier(customer_key, "customer key")
        if type(source) is not dict or not source:
            raise ValueError("native fixture requires explicit source records")
        if initial_invoice is not None and initial_order is None:
            raise ValueError("existing invoice fixture requires its existing Order")
        if initial_invoice is not None and FrozenJson(initial_invoice) == FrozenJson(invoice):
            raise ValueError("existing invoice fixture must exercise a preserved user change")
        for key, value in (
            ("source_key", source_key),
            ("customer_key", customer_key),
            ("_source", FrozenJson(source)),
            ("_order", FrozenJson(billing_fields(order, invoice=False))),
            ("_invoice", FrozenJson(billing_fields(invoice, invoice=True))),
            (
                "_initial_order",
                FrozenJson(
                    billing_fields(initial_order, invoice=False)
                    if initial_order is not None
                    else None
                ),
            ),
            (
                "_initial_invoice",
                FrozenJson(
                    billing_fields(initial_invoice, invoice=True, existing=True)
                    if initial_invoice is not None
                    else None
                ),
            ),
        ):
            object.__setattr__(self, key, value)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source_key": self.source_key,
            "customer_key": self.customer_key,
            "source": self._source.value,
            "order": self._order.value,
            "invoice": self._invoice.value,
            "initial_order": self._initial_order.value,
            "initial_invoice": self._initial_invoice.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(
            **object_fields(
                value,
                {
                    "source_key",
                    "customer_key",
                    "source",
                    "order",
                    "invoice",
                    "initial_order",
                    "initial_invoice",
                },
                "native fixture record",
            )
        )


@dataclass(frozen=True, slots=True)
class NativeBillingFixturePage(WireRecord):
    records: tuple[NativeBillingFixtureRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "records",
            unique(
                instances(self.records, NativeBillingFixtureRecord, "fixture records"),
                lambda r: r.source_key,
                "fixture records",
            ),
        )
        if not 1 <= len(self.records) <= MAX_FIXTURE_PAGE_RECORDS:
            raise ValueError("native fixture page must contain one to 100 records")
        if len(canonical_json(self.to_dict()).encode("utf-8")) > MAX_FIXTURE_PAGE_BYTES:
            raise ValueError("native fixture page exceeds its byte limit")

    @property
    def digest(self) -> str:
        return artifact_digest(self.to_dict())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": NATIVE_BILLING_FIXTURE_PAGE,
            "records": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"schema_version", "records"}, "native fixture page")
        choice(
            payload["schema_version"], (NATIVE_BILLING_FIXTURE_PAGE,), "native fixture page schema"
        )
        return cls(array(payload["records"], NativeBillingFixtureRecord.from_dict, "records"))


@dataclass(frozen=True, slots=True)
class NativeBillingFixturePageRef(WireRecord):
    artifact: ArtifactIdentity
    record_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.artifact) is not ArtifactIdentity:
            raise ValueError("fixture page requires immutable artifact identity")
        object.__setattr__(self, "record_keys", identifiers(self.record_keys, "page record keys"))
        if not 1 <= len(self.record_keys) <= MAX_FIXTURE_PAGE_RECORDS:
            raise ValueError("fixture page membership must contain one to 100 records")

    def validate_page(self, page: NativeBillingFixturePage) -> None:
        if type(page) is not NativeBillingFixturePage or page.digest != self.artifact.digest:
            raise ValueError("fixture page differs from the pinned artifact")
        if tuple(r.source_key for r in page.records) != self.record_keys:
            raise ValueError("fixture page differs from declared complete membership")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"artifact": self.artifact.to_dict(), "record_keys": list(self.record_keys)}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"artifact", "record_keys"}, "fixture page reference")
        return cls(
            ArtifactIdentity.from_dict(payload["artifact"]),
            array(payload["record_keys"], lambda v: identifier(v, "source key"), "record keys"),
        )


@dataclass(frozen=True, slots=True)
class NativeBillingFixtureManifest(WireRecord):
    mapping: ArtifactIdentity
    configuration_digest: str
    existing_order_keys: tuple[str, ...]
    existing_invoice_keys: tuple[str, ...]
    pages: tuple[NativeBillingFixturePageRef, ...]

    def __post_init__(self) -> None:
        if type(self.mapping) is not ArtifactIdentity:
            raise ValueError("fixture requires an immutable mapping identity")
        ArtifactIdentity("configuration", "1", self.configuration_digest)
        for name in ("existing_order_keys", "existing_invoice_keys"):
            object.__setattr__(self, name, identifiers(getattr(self, name), name))
        instances(self.pages, NativeBillingFixturePageRef, "fixture pages")
        if not 1 <= len(self.pages) <= 10000:
            raise ValueError("fixture manifest requires bounded nonempty pages")
        keys = [key for page in self.pages for key in page.record_keys]
        if not 2 <= len(keys) <= 10000 or len(set(keys)) != len(keys):
            raise ValueError("fixture page membership must be complete and disjoint")
        if len({p.artifact.id for p in self.pages}) != len(self.pages):
            raise ValueError("fixture page artifact identities must be unique")
        if not set(self.existing_invoice_keys) <= set(self.existing_order_keys) <= set(keys):
            raise ValueError("existing records must resolve within the fixture")

    @property
    def digest(self) -> str:
        return artifact_digest(self.to_dict())

    def validate_for(self, scenario: NativeBillingScenario) -> None:
        if (
            self.digest != scenario.fixture.digest
            or self.mapping != scenario.mapping
            or self.configuration_digest != scenario.configuration_digest
        ):
            raise ValueError("fixture manifest differs from the admitted scenario")
        if tuple(sorted(k for p in self.pages for k in p.record_keys)) != scenario.record_keys:
            raise ValueError("fixture manifest differs from complete scenario membership")
        if (
            self.existing_order_keys != scenario.existing_order_keys
            or self.existing_invoice_keys != scenario.existing_invoice_keys
        ):
            raise ValueError("fixture baseline differs from the admitted scenario")

    def validate_page(self, page_index: int, page: NativeBillingFixturePage) -> None:
        if type(page_index) is not int or not 0 <= page_index < len(self.pages):
            raise ValueError("fixture page index is outside its manifest")
        self.pages[page_index].validate_page(page)
        for record in page.records:
            values = record.to_dict()
            if (values["initial_order"] is not None) != (
                record.source_key in self.existing_order_keys
            ):
                raise ValueError("fixture Order baseline differs from its manifest")
            if (values["initial_invoice"] is not None) != (
                record.source_key in self.existing_invoice_keys
            ):
                raise ValueError("fixture invoice baseline differs from its manifest")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": NATIVE_BILLING_FIXTURE_MANIFEST,
            "mapping": self.mapping.to_dict(),
            "configuration_digest": self.configuration_digest,
            "existing_order_keys": list(self.existing_order_keys),
            "existing_invoice_keys": list(self.existing_invoice_keys),
            "pages": [page.to_dict() for page in self.pages],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(
            value,
            {
                "schema_version",
                "mapping",
                "configuration_digest",
                "existing_order_keys",
                "existing_invoice_keys",
                "pages",
            },
            "native fixture manifest",
        )
        choice(
            payload["schema_version"], (NATIVE_BILLING_FIXTURE_MANIFEST,), "native fixture schema"
        )
        return cls(
            ArtifactIdentity.from_dict(payload["mapping"]),
            payload["configuration_digest"],
            array(
                payload["existing_order_keys"],
                lambda v: identifier(v, "source key"),
                "existing Order keys",
            ),
            array(
                payload["existing_invoice_keys"],
                lambda v: identifier(v, "source key"),
                "existing invoice keys",
            ),
            array(payload["pages"], NativeBillingFixturePageRef.from_dict, "fixture pages"),
        )
