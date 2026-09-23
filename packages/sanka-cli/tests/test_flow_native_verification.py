# SPDX-License-Identifier: AGPL-3.0-only
"""Adversarial comparator tests; fabricated readbacks are not native execution proof."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from sanka.runtime.flow.model import Claim, Document, FlowError, Installation, OwnedResource
from sanka.runtime.flow.native_verification import (
    NATIVE_PAGE_SCHEMA,
    NATIVE_RESULT_SCHEMA,
    NATIVE_SNAPSHOT_SCHEMA,
    evaluate_native_billing_scenario,
    required_native_scenarios,
)
from sanka_extensions.flow import (
    ArtifactIdentity,
    Blueprint,
    BlueprintOrigin,
    NativeBillingDelivery,
    NativeBillingFixtureManifest,
    NativeBillingFixturePage,
    NativeBillingFixturePageRef,
    NativeBillingFixtureRecord,
    NativeBillingScenario,
    NativeOrderBillingWorkflow,
    Resource,
)
from sanka_extensions.flow.native_verification import (
    NATIVE_BILLING_CASES,
    BillingCase,
    ImportStatus,
)

type FixtureRecords = dict[str, NativeBillingFixtureRecord]
type MutateRows = Callable[[list[dict[str, Any]]], object]


def profile() -> NativeOrderBillingWorkflow:
    return NativeOrderBillingWorkflow(
        "billing",
        30,
        "11111111-1111-4111-8111-111111111111",
        ArtifactIdentity("mapping", "1", "sha256:" + "a" * 64),
        "pipeline",
        ("won",),
        "2026-09-01",
        30,
    )


def business_fields(*, invoice: bool = False) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "currency": "JPY",
        "total_price": "220",
        "total_price_without_tax": "200",
        "tax": "10",
        "tax_inclusive": False,
        "line_items": [
            {
                "key": "line",
                "name": "Isolated item",
                "quantity": "2",
                "unit_price": "100",
                "tax_rate": "10",
            }
        ],
    }
    if invoice:
        fields.update(status="draft", invoice_date="2026-09-14", due_date="2026-10-14")
    return fields


class ArtifactReader:
    def __init__(self) -> None:
        self.documents: dict[str, Document] = {}
        self.reads: list[str] = []

    def put(self, name: str, value: dict[str, Any]) -> ArtifactIdentity:
        doc = Document(value)
        identity = ArtifactIdentity(name, "1", doc.digest)
        self.documents[identity.digest] = doc
        return identity

    async def read_verification_artifact(self, identity: Document, claim: Claim) -> Document:
        self.reads.append(identity.to_dict()["digest"])
        return self.documents[identity.to_dict()["digest"]]


def make_case(
    case: BillingCase = "complete",
    *,
    reader: ArtifactReader | None = None,
    native: NativeOrderBillingWorkflow | None = None,
) -> tuple[ArtifactReader, NativeOrderBillingWorkflow, NativeBillingScenario, FixtureRecords]:
    reader = reader or ArtifactReader()
    native = native or profile()
    keys = tuple(f"deal-{i:04d}" for i in range(2001 if case == "bulk" else 3))
    existing_orders, existing_invoices = keys[:2], keys[:1]
    fixture_records = {}
    for key in keys:
        order, invoice = business_fields(), business_fields(invoice=True)
        invoice["due_date"] = (
            datetime.fromisoformat(invoice["invoice_date"]).date()
            + timedelta(days=native.invoice_due_days)
        ).isoformat()
        fixture_records[key] = NativeBillingFixtureRecord(
            key,
            "customer-a",
            {"id": key, "amount": "220", "currency": "JPY"},
            order,
            invoice,
            initial_order=order if key in existing_orders else None,
            initial_invoice={**invoice, "status": "sent", "notes": "User changed this"}
            if key in existing_invoices
            else None,
        )
    refs = []
    for i in range(0, len(keys), 100):
        group = keys[i : i + 100]
        page = NativeBillingFixturePage(tuple(fixture_records[key] for key in group))
        refs.append(
            NativeBillingFixturePageRef(reader.put(f"fixture/{case}/{i}", page.to_dict()), group)
        )
    fixture = NativeBillingFixtureManifest(
        native.mapping, native.configuration_digest, existing_orders, existing_invoices, tuple(refs)
    )
    fixture_identity = reader.put(f"fixture/{case}", fixture.to_dict())
    imported = () if case == "empty" else (keys[0], keys[2]) if case == "scoped_complete" else keys
    status: ImportStatus = case if case in {"partial", "failed", "cancelled"} else "complete"
    invoiced = existing_invoices if status != "complete" or case == "empty" else imported
    first = NativeBillingDelivery(
        "attempt-1", "run-1", "2026-09-14T00:00:00Z", status, imported, invoiced
    )
    deliveries: tuple[NativeBillingDelivery, ...] = (first,)
    if case in {"retry", "handoff_retry"}:
        first = replace(
            first,
            failure="after_import" if case == "handoff_retry" else "after_invoice_commit",
            invoice_keys=existing_invoices if case == "handoff_retry" else keys[:2],
        )
        deliveries = (first, replace(first, id="attempt-2", failure="none", invoice_keys=keys))
    elif case == "repeated_schedule":
        deliveries = (
            first,
            replace(
                first,
                id="attempt-2",
                run_id="run-2",
                at=(
                    datetime.fromisoformat(first.at.replace("Z", "+00:00"))
                    + timedelta(minutes=native.interval_minutes)
                )
                .isoformat()
                .replace("+00:00", "Z"),
            ),
        )
    elif case == "overlap":
        first = replace(first, concurrent_group="group-1")
        deliveries = (first, replace(first, id="attempt-2", run_id="run-2"))
    selected = NativeBillingScenario(
        case,
        native.id,
        case,
        fixture_identity,
        native.mapping,
        native.configuration_digest,
        keys,
        existing_orders,
        existing_invoices,
        deliveries,
    )
    return reader, native, selected, fixture_records


def make_readback(
    reader: ArtifactReader,
    native: NativeOrderBillingWorkflow,
    selected: NativeBillingScenario,
    fixtures: FixtureRecords,
    *,
    delivery_id: str,
    imported: set[str],
    invoice_keys: set[str],
    mutate: MutateRows | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for key in selected.record_keys:
        fixture = fixtures[key].to_dict()
        order_id = f"order-{key}"
        orders = []
        if key in selected.existing_order_keys or key in imported:
            orders.append(
                {
                    "id": order_id,
                    "customer_id": "native-customer-a",
                    "source_endpoint_id": native.source_endpoint_id,
                    "external_id": key,
                    "fields": deepcopy(
                        fixture["order"] if key in imported else fixture["initial_order"]
                    ),
                }
            )
        invoices = []
        if key in invoice_keys:
            invoices.append(
                {
                    "id": f"invoice-{key}",
                    "customer_id": "native-customer-a",
                    "order_ids": [order_id],
                    "fields": deepcopy(
                        fixture["initial_invoice"]
                        if key in selected.existing_invoice_keys
                        else fixture["invoice"]
                    ),
                }
            )
        rows.append({"source_key": key, "orders": orders, "invoices": invoices})
    if mutate:
        mutate(rows)
    pages = []
    for i in range(0, len(rows), 100):
        page = rows[i : i + 100]
        identity = reader.put(
            f"readback/{selected.id}/{delivery_id}/{i}",
            {"schema_version": NATIVE_PAGE_SCHEMA, "records": page},
        )
        pages.append(
            {"artifact": identity.to_dict(), "record_keys": [row["source_key"] for row in page]}
        )
    return reader.put(
        f"readback/{selected.id}/{delivery_id}",
        {
            "schema_version": NATIVE_SNAPSHOT_SCHEMA,
            "scenario_digest": Document(selected.to_dict()).digest,
            "delivery_id": delivery_id,
            "isolated_namespace": "isolated-1",
            "pages": pages,
            "outside_record_ids": [],
        },
    ).to_dict()


def make_result(
    reader: ArtifactReader,
    native: NativeOrderBillingWorkflow,
    selected: NativeBillingScenario,
    fixtures: FixtureRecords,
    *,
    mutate: MutateRows | None = None,
) -> Document:
    initial = make_readback(
        reader,
        native,
        selected,
        fixtures,
        delivery_id="initial",
        imported=set(),
        invoice_keys=set(selected.existing_invoice_keys),
    )
    deliveries = []
    imported: set[str] = set()
    for index, delivery in enumerate(selected.deliveries):
        imported.update(delivery.imported_keys)
        snapshot = make_readback(
            reader,
            native,
            selected,
            fixtures,
            delivery_id=delivery.id,
            imported=imported,
            invoice_keys=set(delivery.invoice_keys),
            mutate=mutate if index == len(selected.deliveries) - 1 else None,
        )
        deliveries.append(
            {
                **{
                    key: value
                    for key, value in delivery.to_dict().items()
                    if key not in {"imported_keys", "invoice_keys"}
                },
                "runtime_run_id": f"native-{delivery.run_id}",
                "runtime_attempt_id": f"native-{delivery.id}",
                "started_at": "2026-09-14T00:00:01Z",
                "finished_at": "2026-09-14T00:00:05Z",
                "imported_orders": [
                    {"source_key": key, "order_id": f"order-{key}"}
                    for key in delivery.imported_keys
                ],
                "readback": snapshot,
            }
        )
    return Document(
        {
            "schema_version": NATIVE_RESULT_SCHEMA,
            "scenario_id": selected.id,
            "scenario_digest": Document(selected.to_dict()).digest,
            "installation_id": "installation",
            "workflow_target_id": "native-workflow",
            "mapping": selected.mapping.to_dict(),
            "fixture": selected.fixture.to_dict(),
            "configuration_digest": selected.configuration_digest,
            "isolated": True,
            "isolated_namespace": "isolated-1",
            "outcome": "passed",
            "customer_bindings": {"customer-a": "native-customer-a"},
            "initial_readback": initial,
            "deliveries": deliveries,
        }
    )


async def evaluate(
    reader: ArtifactReader,
    native: NativeOrderBillingWorkflow,
    selected: NativeBillingScenario,
    result: Document,
) -> Document:
    async def asserted(claim: Claim) -> None:
        assert claim.attempt_id == "verification"

    installation = Installation(
        "installation",
        "workspace",
        1,
        (
            OwnedResource(
                "billing", "native-workflow", "workflow", "r1", Document(native.to_dict())
            ),
        ),
    )
    return await evaluate_native_billing_scenario(
        blueprint={"resources": [Resource(native.id, "workflow", native.to_dict()).to_dict()]},
        scenario=Document(selected.to_dict()),
        installation=installation,
        result=result,
        artifacts=reader,
        claim=Claim("plan", "verification", 1),
        assert_claim=asserted,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", NATIVE_BILLING_CASES)
async def test_full_declared_readback_passes_comparator(case: BillingCase) -> None:
    reader, native, selected, fixtures = make_case(case)
    result = make_result(reader, native, selected, fixtures)
    verdict = await evaluate(reader, native, selected, result)
    assert verdict.to_dict()["outcome"] == "passed"
    assert verdict.to_dict()["checked_record_count"] == len(selected.record_keys)
    assert len(verdict.text.encode()) < 512 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_customer",
        "wrong_order",
        "money_float",
        "line_tax",
        "duplicate",
        "changed_id",
        "overwrite_existing",
    ],
)
async def test_host_pass_cannot_hide_wrong_business_readback(mutation: str) -> None:
    def corrupt(rows: list[dict[str, Any]]) -> None:
        invoice = rows[-1]["invoices"][0]
        if mutation == "wrong_customer":
            invoice["customer_id"] = "different-customer"
        elif mutation == "wrong_order":
            invoice["order_ids"] = ["outside-order"]
        elif mutation == "money_float":
            invoice["fields"]["total_price"] = 220.0
        elif mutation == "line_tax":
            invoice["fields"]["line_items"][0]["tax_rate"] = "0"
        elif mutation == "duplicate":
            rows[-1]["invoices"].append(deepcopy(invoice))
        elif mutation == "changed_id":
            rows[0]["invoices"][0]["id"] = "replacement-id"
        else:
            rows[0]["invoices"][0]["fields"] = business_fields(invoice=True)

    reader, native, selected, fixtures = make_case()
    verdict = await evaluate(
        reader, native, selected, make_result(reader, native, selected, fixtures, mutate=corrupt)
    )
    assert verdict.to_dict()["outcome"] == "failed"
    assert verdict.to_dict()["mismatch_count"] > 0


@pytest.mark.asyncio
async def test_2001st_order_cannot_be_lost_in_a_sample() -> None:
    reader, native, selected, fixtures = make_case("bulk")
    result = make_result(reader, native, selected, fixtures, mutate=lambda rows: rows.pop())
    with pytest.raises(FlowError, match="complete fixture"):
        await evaluate(reader, native, selected, result)


@pytest.mark.asyncio
async def test_page_hash_is_checked_before_using_host_readback() -> None:
    reader, native, selected, fixtures = make_case()
    result = make_result(reader, native, selected, fixtures)
    digest = result.to_dict()["initial_readback"]["digest"]
    reader.documents[digest] = Document({"passed": True})
    with pytest.raises(FlowError) as error:
        await evaluate(reader, native, selected, result)
    assert error.value.code == "FLOW_ARTIFACT_MISMATCH"


@pytest.mark.asyncio
async def test_readback_page_byte_limit_is_enforced_even_with_a_valid_hash() -> None:
    def oversized(rows: list[dict[str, Any]]) -> None:
        rows[-1]["invoices"][0]["fields"]["notes"] = "x" * (256 * 1024)

    reader, native, selected, fixtures = make_case()
    result = make_result(reader, native, selected, fixtures, mutate=oversized)
    with pytest.raises(FlowError, match="page exceeds its byte limit"):
        await evaluate(reader, native, selected, result)


@pytest.mark.asyncio
async def test_receipt_retains_distinct_artifact_identities_with_identical_content() -> None:
    reader, native, selected, fixtures = make_case("empty")
    result = make_result(reader, native, selected, fixtures)
    payload = result.to_dict()
    before = reader.documents[payload["initial_readback"]["digest"]].to_dict()["pages"][0][
        "artifact"
    ]
    after = reader.documents[payload["deliveries"][0]["readback"]["digest"]].to_dict()["pages"][0][
        "artifact"
    ]
    assert before["digest"] == after["digest"]
    assert before["id"] != after["id"]
    verdict = await evaluate(reader, native, selected, result)
    assert verdict.to_dict()["outcome"] == "passed"
    assert before in verdict.to_dict()["checked_artifacts"]
    assert after in verdict.to_dict()["checked_artifacts"]


@pytest.mark.asyncio
async def test_sequential_runs_do_not_prove_overlap() -> None:
    reader, native, selected, fixtures = make_case("overlap")
    result = make_result(reader, native, selected, fixtures).to_dict()
    result["deliveries"][1]["started_at"] = "2026-09-14T00:00:06Z"
    result["deliveries"][1]["finished_at"] = "2026-09-14T00:00:07Z"
    verdict = await evaluate(reader, native, selected, Document(result))
    assert verdict.to_dict()["outcome"] == "failed"
    assert {m["code"] for m in verdict.to_dict()["mismatches"]} == {
        "FLOW_CONCURRENT_EXECUTION_NOT_PROVEN"
    }


@pytest.mark.asyncio
async def test_retry_must_keep_runtime_run_identity() -> None:
    reader, native, selected, fixtures = make_case("retry")
    result = make_result(reader, native, selected, fixtures).to_dict()
    result["deliveries"][1]["runtime_run_id"] = "different-run"
    verdict = await evaluate(reader, native, selected, Document(result))
    assert verdict.to_dict()["outcome"] == "failed"


@pytest.mark.asyncio
async def test_scoped_complete_cannot_bill_an_existing_order_outside_import_output() -> None:
    def corrupt(rows: list[dict[str, Any]]) -> None:
        rows[1]["invoices"] = [
            {
                "id": "outside-invoice",
                "customer_id": "native-customer-a",
                "order_ids": [rows[1]["orders"][0]["id"]],
                "fields": business_fields(invoice=True),
            }
        ]

    reader, native, selected, fixtures = make_case("scoped_complete")
    verdict = await evaluate(
        reader, native, selected, make_result(reader, native, selected, fixtures, mutate=corrupt)
    )
    assert verdict.to_dict()["outcome"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("isolated", False), ("installation_id", "foreign"), ("scenario_digest", "different")],
)
async def test_wrong_scope_or_unisolated_evidence_is_rejected_before_artifact_reads(
    field: str,
    value: object,
) -> None:
    reader, native, selected, fixtures = make_case()
    result = make_result(reader, native, selected, fixtures).to_dict()
    result[field] = value
    with pytest.raises(FlowError):
        await evaluate(reader, native, selected, Document(result))
    assert reader.reads == []


def test_native_blueprint_and_plan_document_keep_complete_coverage_under_limits() -> None:
    native = profile()
    cases = tuple(make_case(case)[2] for case in NATIVE_BILLING_CASES)
    identity = ArtifactIdentity("sanka/billing", "1", "sha256:" + "b" * 64)
    candidate = Blueprint(
        "billing",
        "1",
        BlueprintOrigin("template", identity),
        identity,
        (Resource("billing", "workflow", native.to_dict()),),
        scenarios=cases,
        schema_version="sanka-flow-blueprint/v4",
    )
    assert required_native_scenarios(candidate.to_dict()) == {s.id for s in cases}
    Document({"blueprint": candidate.to_dict()})
    changed = Document(candidate.to_dict()).to_dict()
    changed["scenarios"].pop()
    with pytest.raises(FlowError) as error:
        required_native_scenarios(changed)
    assert error.value.code == "FLOW_VERIFICATION_INCOMPLETE"
