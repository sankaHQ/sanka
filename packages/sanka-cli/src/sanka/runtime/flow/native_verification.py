# SPDX-License-Identifier: AGPL-3.0-only
"""Compare complete native billing evidence against immutable admitted oracles.

The host executes the existing native workflow and stores immutable readback
pages. This module performs no import, conversion, scheduling or provider I/O.
It reads every pinned page, checks exact membership/identity/fields, and produces
the verdict. Paged source/readback artifacts avoid copying thousands of full
records into the bounded lifecycle ledger.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sanka.runtime.flow.model import Claim, Document, FlowError, Installation
from sanka_extensions.flow import (
    ArtifactIdentity,
    Blueprint,
    NativeBillingFixtureManifest,
    NativeBillingFixturePage,
    NativeBillingScenario,
    NativeOrderBillingWorkflow,
)
from sanka_extensions.flow.native_verification import NATIVE_BILLING_VERIFICATION, utc_clock

NATIVE_RESULT_SCHEMA = "sanka-flow-native-billing-result/v1"
NATIVE_SNAPSHOT_SCHEMA = "sanka-flow-native-billing-readback/v1"
NATIVE_PAGE_SCHEMA = "sanka-flow-native-billing-readback-page/v1"


class NativeVerificationArtifacts(Protocol):
    async def read_verification_artifact(self, identity: Document, claim: Claim) -> Document:
        """Read one workspace/installation-scoped immutable artifact by identity.

        This is a private artifact lookup, never a caller-supplied URL fetch.
        The runtime hashes its complete contents before trusting them. The host
        must retain the checked oracle and native readback for the evidence's
        lifetime, enforce the active claim, and bound each read.
        """
        ...


def required_native_scenarios(blueprint: dict[str, Any]) -> set[str]:
    try:
        candidate = Blueprint.from_dict(blueprint)
    except (ValueError, KeyError, TypeError) as error:
        raise FlowError(
            "FLOW_VERIFICATION_INCOMPLETE", "Invalid native verification coverage"
        ) from error
    if candidate.schema_version != "sanka-flow-blueprint/v4":
        raise FlowError(
            "FLOW_VERIFICATION_PROFILE_UNSUPPORTED", "Native verification requires Blueprint v4"
        )
    return {scenario.id for scenario in candidate.scenarios if scenario.required}


def _object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise FlowError("FLOW_SCENARIO_MISMATCH", f"{label} has missing or unexpected fields")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 256:
        raise FlowError("FLOW_SCENARIO_MISMATCH", f"{label} has no bounded identity")
    return value


def _clock(value: object) -> datetime:
    try:
        if type(value) is not str or not value.endswith("Z") or len(value) > 40:
            raise ValueError("clock")
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo != UTC:
            raise ValueError("clock")
        return instant
    except ValueError as error:
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Native timing requires UTC readback") from error


class _Comparison:
    def __init__(self) -> None:
        self.mismatches: list[dict[str, Any]] = []
        self.count = 0

    def check(self, condition: bool, code: str, **detail: Any) -> None:
        if not condition:
            self.count += 1
            # Retain a bounded diagnostic list, while still checking every row.
            if len(self.mismatches) < 100:
                self.mismatches.append({"code": code, **detail})

    def fields(self, actual: object, expected: object, *, key: str, kind: str) -> None:
        if type(actual) is not dict or type(expected) is not dict:
            raise FlowError("FLOW_SCENARIO_MISMATCH", "Native business fields are missing")
        for field, value in expected.items():
            self.check(
                field in actual
                and Document({"value": actual[field]}) == Document({"value": value}),
                "FLOW_FIELD_MISMATCH",
                source_key=key,
                kind=kind,
                field=field,
            )


async def evaluate_native_billing_scenario(
    *,
    blueprint: dict[str, Any],
    scenario: Document,
    installation: Installation,
    result: Document,
    artifacts: NativeVerificationArtifacts,
    claim: Claim,
    assert_claim: Callable[[Claim], Awaitable[None]],
) -> Document:
    """Evaluate one real native result; counts and hashes are not host verdicts."""
    try:
        selected = NativeBillingScenario.from_dict(scenario.to_dict())
        resource = next(r for r in blueprint["resources"] if r["id"] == selected.workflow_id)
        profile = NativeOrderBillingWorkflow.from_dict(resource["spec"])
        target_id = next(
            r.target_id for r in installation.resources if r.logical_id == selected.workflow_id
        )
    except (ValueError, KeyError, StopIteration, TypeError) as error:
        raise FlowError(
            "FLOW_SCENARIO_MISMATCH", "Native scenario does not resolve to its workflow"
        ) from error
    if (
        selected.mapping != profile.mapping
        or selected.configuration_digest != profile.configuration_digest
    ):
        raise FlowError(
            "FLOW_SCENARIO_MISMATCH", "Native scenario uses another mapping or configuration"
        )
    payload = result.to_dict()
    if any(
        payload.get(key) != expected
        for key, expected in {
            "schema_version": NATIVE_RESULT_SCHEMA,
            "scenario_id": selected.id,
            "scenario_digest": scenario.digest,
            "installation_id": installation.id,
            "workflow_target_id": target_id,
            "mapping": selected.mapping.to_dict(),
            "fixture": selected.fixture.to_dict(),
            "configuration_digest": selected.configuration_digest,
        }.items()
    ):
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Native evidence belongs to different input")
    if payload.get("isolated") is not True:
        raise FlowError("FLOW_VERIFICATION_UNSAFE", "Native verification must be isolated")
    if payload.get("outcome") not in {"passed", "failed", "skipped"}:
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Invalid native outcome")
    if payload["outcome"] != "passed":
        return Document(
            {
                "profile": NATIVE_BILLING_VERIFICATION,
                "scenario_id": selected.id,
                "outcome": "failed",
                "mismatches": [{"code": "FLOW_NATIVE_CHECK_INCOMPLETE"}],
                "mismatch_count": 1,
                "native_result": payload,
                "checked_artifacts": [],
            }
        )

    checked: dict[ArtifactIdentity, dict[str, Any]] = {}

    async def read(raw_identity: object) -> Document:
        try:
            identity = ArtifactIdentity.from_dict(raw_identity)
        except ValueError as error:
            raise FlowError(
                "FLOW_SCENARIO_MISMATCH", "Native evidence has no immutable artifact identity"
            ) from error
        await assert_claim(claim)
        document = Document(
            (
                await artifacts.read_verification_artifact(Document(identity.to_dict()), claim)
            ).to_dict()
        )
        if document.digest != identity.digest:
            raise FlowError(
                "FLOW_ARTIFACT_MISMATCH", "Native artifact contents differ from the pinned digest"
            )
        await assert_claim(claim)
        checked[identity] = identity.to_dict()
        return document

    try:
        manifest = NativeBillingFixtureManifest.from_dict(
            (await read(selected.fixture.to_dict())).to_dict()
        )
        manifest.validate_for(selected)
        fixtures: dict[str, dict[str, Any]] = {}
        for index, page_ref in enumerate(manifest.pages):
            page = NativeBillingFixturePage.from_dict(
                (await read(page_ref.artifact.to_dict())).to_dict()
            )
            manifest.validate_page(index, page)
            fixtures.update({record.source_key: record.to_dict() for record in page.records})
    except ValueError as error:
        raise FlowError(
            "FLOW_FIXTURE_MISMATCH", "Native fixture oracle failed admission"
        ) from error

    comparison = _Comparison()
    customer_bindings = payload.get("customer_bindings")
    customer_keys = {fixture["customer_key"] for fixture in fixtures.values()}
    if type(customer_bindings) is not dict or set(customer_bindings) != customer_keys:
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Native customer bindings are incomplete")
    for native_id in customer_bindings.values():
        _identifier(native_id, "native customer")
    if len(set(customer_bindings.values())) != len(customer_bindings):
        raise FlowError(
            "FLOW_SCENARIO_MISMATCH", "Distinct fixture customers share a native identity"
        )
    namespace = _identifier(payload.get("isolated_namespace"), "isolated namespace")

    async def snapshot(raw_identity: object, delivery_id: str) -> dict[str, dict[str, Any]]:
        current = _object(
            (await read(raw_identity)).to_dict(),
            {
                "schema_version",
                "scenario_digest",
                "delivery_id",
                "isolated_namespace",
                "pages",
                "outside_record_ids",
            },
            "native readback manifest",
        )
        if any(
            current[key] != expected
            for key, expected in {
                "schema_version": NATIVE_SNAPSHOT_SCHEMA,
                "scenario_digest": scenario.digest,
                "delivery_id": delivery_id,
                "isolated_namespace": namespace,
            }.items()
        ):
            raise FlowError(
                "FLOW_SCENARIO_MISMATCH", "Native readback belongs to different execution"
            )
        if type(current["outside_record_ids"]) is not list:
            raise FlowError("FLOW_SCENARIO_MISMATCH", "Native effect inventory is missing")
        comparison.check(
            not current["outside_record_ids"], "FLOW_OUTSIDE_BILLING_SCOPE", delivery_id=delivery_id
        )
        pages = current["pages"]
        if type(pages) is not list or not 1 <= len(pages) <= len(selected.record_keys):
            raise FlowError(
                "FLOW_SCENARIO_MISMATCH", "Native readback pages are missing or unbounded"
            )
        rows: dict[str, dict[str, Any]] = {}
        for ref in pages:
            ref = _object(ref, {"artifact", "record_keys"}, "native readback page reference")
            keys = ref["record_keys"]
            if (
                type(keys) is not list
                or not 1 <= len(keys) <= 100
                or any(type(k) is not str for k in keys)
                or len(set(keys)) != len(keys)
            ):
                raise FlowError("FLOW_SCENARIO_MISMATCH", "Native page membership is invalid")
            page_document = await read(ref["artifact"])
            if len(page_document.text.encode("utf-8")) > 256 * 1024:
                raise FlowError(
                    "FLOW_SCENARIO_MISMATCH", "Native readback page exceeds its byte limit"
                )
            page = _object(
                page_document.to_dict(),
                {"schema_version", "records"},
                "native readback page",
            )
            if page["schema_version"] != NATIVE_PAGE_SCHEMA or type(page["records"]) is not list:
                raise FlowError("FLOW_SCENARIO_MISMATCH", "Invalid native readback page")
            actual_keys: list[str] = []
            for raw_row in page["records"]:
                row = _object(raw_row, {"source_key", "orders", "invoices"}, "native readback row")
                key = _identifier(row["source_key"], "native source key")
                if key in rows or key not in fixtures:
                    raise FlowError(
                        "FLOW_SCENARIO_MISMATCH",
                        "Native readback repeats or substitutes a source record",
                    )
                if type(row["orders"]) is not list or type(row["invoices"]) is not list:
                    raise FlowError(
                        "FLOW_SCENARIO_MISMATCH",
                        "Native readback omits complete order/invoice sets",
                    )
                actual_keys.append(key)
                rows[key] = row
            if sorted(actual_keys) != sorted(keys):
                raise FlowError(
                    "FLOW_SCENARIO_MISMATCH",
                    "Native readback page is a truncated or substituted sample",
                )
        if set(rows) != set(fixtures):
            raise FlowError(
                "FLOW_SCENARIO_MISMATCH", "Native readback does not cover the complete fixture"
            )
        return rows

    order_ids: dict[str, str] = {}
    invoice_ids: dict[str, str] = {}
    initial = await snapshot(payload.get("initial_readback"), "initial")

    def compare_rows(
        rows: dict[str, dict[str, Any]],
        *,
        imported: set[str],
        invoiced: set[str],
        initial_state: bool = False,
    ) -> None:
        present_orders = set(selected.existing_order_keys) | imported
        all_order_ids: set[str] = set()
        all_invoice_ids: set[str] = set()
        for key, fixture in fixtures.items():
            row = rows[key]
            customer_id = customer_bindings[fixture["customer_key"]]
            comparison.check(
                len(row["orders"]) == (1 if key in present_orders else 0),
                "FLOW_ORDER_COUNT_MISMATCH",
                source_key=key,
            )
            comparison.check(
                len(row["invoices"]) == (1 if key in invoiced else 0),
                "FLOW_INVOICE_COUNT_MISMATCH",
                source_key=key,
            )
            for raw_order in row["orders"]:
                order = _object(
                    raw_order,
                    {"id", "fields", "customer_id", "source_endpoint_id", "external_id"},
                    "native Order",
                )
                identity = _identifier(order["id"], "Order")
                comparison.check(
                    identity not in all_order_ids, "FLOW_ORDER_IDENTITY_COLLISION", source_key=key
                )
                all_order_ids.add(identity)
                comparison.check(
                    order_ids.setdefault(key, identity) == identity,
                    "FLOW_ORDER_IDENTITY_CHANGED",
                    source_key=key,
                )
                comparison.check(
                    order["customer_id"] == customer_id,
                    "FLOW_ORDER_CUSTOMER_MISMATCH",
                    source_key=key,
                )
                external_id = fixture["source"].get("id")
                if type(external_id) is not str or not external_id:
                    raise FlowError(
                        "FLOW_FIXTURE_MISMATCH",
                        "Native fixture source must declare an exact external record ID",
                    )
                comparison.check(
                    order["source_endpoint_id"] == profile.source_endpoint_id
                    and order["external_id"] == external_id,
                    "FLOW_ORDER_SOURCE_MISMATCH",
                    source_key=key,
                )
                expected = fixture["order"] if key in imported else fixture["initial_order"]
                if expected is not None:
                    comparison.fields(order["fields"], expected, key=key, kind="order")
            for raw_invoice in row["invoices"]:
                invoice = _object(
                    raw_invoice, {"id", "fields", "customer_id", "order_ids"}, "native Invoice"
                )
                identity = _identifier(invoice["id"], "Invoice")
                comparison.check(
                    identity not in all_invoice_ids,
                    "FLOW_INVOICE_IDENTITY_COLLISION",
                    source_key=key,
                )
                all_invoice_ids.add(identity)
                comparison.check(
                    invoice_ids.setdefault(key, identity) == identity,
                    "FLOW_INVOICE_IDENTITY_CHANGED",
                    source_key=key,
                )
                comparison.check(
                    invoice["customer_id"] == customer_id,
                    "FLOW_INVOICE_CUSTOMER_MISMATCH",
                    source_key=key,
                )
                comparison.check(
                    key in order_ids and invoice["order_ids"] == [order_ids[key]],
                    "FLOW_INVOICE_ORDER_MISMATCH",
                    source_key=key,
                )
                expected = (
                    fixture["initial_invoice"]
                    if key in selected.existing_invoice_keys
                    else fixture["invoice"]
                )
                comparison.fields(invoice["fields"], expected, key=key, kind="invoice")
                if not initial_state and key in selected.existing_invoice_keys:
                    comparison.check(
                        Document({"records": row["invoices"]})
                        == Document({"records": initial[key]["invoices"]}),
                        "FLOW_EXISTING_INVOICE_CHANGED",
                        source_key=key,
                    )

    compare_rows(
        initial, imported=set(), invoiced=set(selected.existing_invoice_keys), initial_state=True
    )
    deliveries = payload.get("deliveries")
    if type(deliveries) is not list or len(deliveries) != len(selected.deliveries):
        raise FlowError("FLOW_SCENARIO_MISMATCH", "Native delivery evidence is incomplete")
    imported: set[str] = set()
    run_bindings: dict[str, str] = {}
    attempt_ids: set[str] = set()
    concurrent_times: list[tuple[datetime, datetime]] = []
    for expected, actual in zip(selected.deliveries, deliveries, strict=True):
        actual = _object(
            actual,
            {
                "id",
                "run_id",
                "at",
                "import_status",
                "failure",
                "concurrent_group",
                "runtime_run_id",
                "runtime_attempt_id",
                "started_at",
                "finished_at",
                "imported_orders",
                "readback",
            },
            "native delivery evidence",
        )
        for key in ("id", "run_id", "at", "import_status", "failure", "concurrent_group"):
            comparison.check(
                actual[key] == getattr(expected, key),
                "FLOW_DELIVERY_MISMATCH",
                delivery_id=expected.id,
                field=key,
            )
        runtime_run = _identifier(actual["runtime_run_id"], "native run")
        attempt = _identifier(actual["runtime_attempt_id"], "native attempt")
        comparison.check(
            run_bindings.setdefault(expected.run_id, runtime_run) == runtime_run,
            "FLOW_RETRY_RUN_CHANGED",
            delivery_id=expected.id,
        )
        comparison.check(
            attempt not in attempt_ids, "FLOW_ATTEMPT_NOT_EXECUTED", delivery_id=expected.id
        )
        attempt_ids.add(attempt)
        start, finish = _clock(actual["started_at"]), _clock(actual["finished_at"])
        comparison.check(start < finish, "FLOW_NATIVE_TIMING_MISMATCH", delivery_id=expected.id)
        if expected.concurrent_group:
            concurrent_times.append((start, finish))
        membership = actual["imported_orders"]
        if type(membership) is not list:
            raise FlowError(
                "FLOW_SCENARIO_MISMATCH", "Native complete import membership is missing"
            )
        imported_ids: dict[str, str] = {}
        for raw in membership:
            entry = _object(raw, {"source_key", "order_id"}, "native import output")
            key = _identifier(entry["source_key"], "import source key")
            identity = _identifier(entry["order_id"], "import Order ID")
            if key in imported_ids:
                raise FlowError(
                    "FLOW_SCENARIO_MISMATCH", "Native import output repeats a source key"
                )
            imported_ids[key] = identity
        comparison.check(
            set(imported_ids) == set(expected.imported_keys),
            "FLOW_IMPORT_MEMBERSHIP_MISMATCH",
            delivery_id=expected.id,
        )
        imported.update(expected.imported_keys)
        current = await snapshot(actual["readback"], expected.id)
        compare_rows(current, imported=imported, invoiced=set(expected.invoice_keys))
        for key, identity in imported_ids.items():
            comparison.check(
                order_ids.get(key) == identity, "FLOW_IMPORT_ORDER_BINDING_MISMATCH", source_key=key
            )
    comparison.check(
        len(set(run_bindings.values())) == len(run_bindings), "FLOW_DISTINCT_RUNS_NOT_EXECUTED"
    )
    if selected.case == "overlap":
        comparison.check(
            len(concurrent_times) == 2
            and max(t[0] for t in concurrent_times) < min(t[1] for t in concurrent_times),
            "FLOW_CONCURRENT_EXECUTION_NOT_PROVEN",
        )
    for key in set(selected.deliveries[-1].invoice_keys) - set(selected.existing_invoice_keys):
        first_delivery = next(d for d in selected.deliveries if key in d.invoice_keys)
        created_on = utc_clock(first_delivery.at).date()
        expected_fields = fixtures[key]["invoice"]
        comparison.check(
            expected_fields["invoice_date"] == created_on.isoformat()
            and expected_fields["due_date"]
            == (created_on + timedelta(days=profile.invoice_due_days)).isoformat(),
            "FLOW_INVOICE_DATE_ORACLE_MISMATCH",
            source_key=key,
        )
    await assert_claim(claim)
    return Document(
        {
            "profile": NATIVE_BILLING_VERIFICATION,
            "scenario_id": selected.id,
            "outcome": "passed" if comparison.count == 0 else "failed",
            "mismatches": comparison.mismatches,
            "mismatch_count": comparison.count,
            "checked_record_count": len(fixtures),
            "checked_artifacts": list(checked.values()),
            "native_result": payload,
        }
    )
