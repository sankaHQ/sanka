# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from sanka.connector import FieldSchema, Inventory, ObjectSchema, SourceFilter, SourceObject
from sanka.runtime.mapping.model import MigrationMappingField, ValueMapEntry
from sanka.runtime.planner import MigrationPlan, RoutePlan, build_plan

SOURCE_OBJECTS = [
    SourceObject(key="contacts", label="Contacts", canonical_type="contacts", default_selected=True)
]

SOURCE_INVENTORY = Inventory(
    provider="salesforce",
    objects=[
        ObjectSchema(
            key="contacts",
            label="Contacts",
            canonical_type="contacts",
            record_count=2,
            identity_fields=["sf_id"],
            fields=[
                FieldSchema(key="sf_id", label="Record ID"),
                FieldSchema(key="email", label="Email", data_type="email"),
                FieldSchema(key="full_name", label="Full Name"),
                FieldSchema(key="annual_revenue", label="Annual Revenue", data_type="string"),
            ],
        )
    ],
)


def _destination(email_unique: bool = True) -> Inventory:
    return Inventory(
        provider="hubspot",
        objects=[
            ObjectSchema(
                key="contacts",
                label="Contacts",
                canonical_type="contacts",
                fields=[
                    FieldSchema(key="email", label="Email", data_type="email", unique=email_unique),
                    FieldSchema(key="fullname", label="Full Name"),
                    FieldSchema(key="annualrevenue", label="Annual Revenue", data_type="number"),
                ],
            )
        ],
    )


def test_auto_mapping_when_destination_has_schema() -> None:
    plan = build_plan(
        source_provider="salesforce",
        target_provider="hubspot",
        inventory=SOURCE_INVENTORY,
        source_objects=SOURCE_OBJECTS,
        suggest_target_object={"contacts": "contacts"},
        destination_inventory=_destination(),
    )

    assert len(plan.routes) == 1
    route = plan.routes[0]
    assert route.mapping_origin == "auto"
    assert route.route_key == "contacts|contacts"
    mapped = {m.source_field: m for m in route.field_mappings}
    assert mapped["contacts.email"].target_field == "email"
    assert mapped["contacts.email"].identity is True
    assert mapped["contacts.full_name"].target_field == "fullname"
    revenue = mapped["contacts.annual_revenue"]
    assert revenue.target_field == "annualrevenue"
    assert revenue.transform_rule == "number_parse"  # string source -> number target
    assert "contacts.sf_id" not in mapped  # nothing plausible to map it onto
    assert route.identity_target_fields == ["email"]
    assert route.identity_field == "sf_id"  # source-side ledger identity is unchanged
    assert plan.coverage["sourceFields"] == 4
    assert plan.coverage["mapped"] == 3


def test_auto_mapping_without_destination_identity_warns() -> None:
    plan = build_plan(
        source_provider="salesforce",
        target_provider="hubspot",
        inventory=SOURCE_INVENTORY,
        source_objects=SOURCE_OBJECTS,
        suggest_target_object={"contacts": "contacts"},
        destination_inventory=_destination(email_unique=False),
    )
    assert plan.routes[0].identity_target_fields == []
    assert any("no destination identity" in w for w in plan.warnings)


def test_identity_fallback_without_destination_schema() -> None:
    plan = build_plan(
        source_provider="salesforce",
        target_provider="sqlite",
        inventory=SOURCE_INVENTORY,
        source_objects=SOURCE_OBJECTS,
        suggest_target_object={"contacts": "contacts"},
        destination_inventory=Inventory(provider="sqlite"),
    )
    route = plan.routes[0]
    assert route.mapping_origin == "identity"
    assert [m.target_field for m in route.field_mappings] == [
        "sf_id",
        "email",
        "full_name",
        "annual_revenue",
    ]
    identity = {m.source_field: m.identity for m in route.field_mappings}
    assert identity["contacts.sf_id"] is True
    assert route.identity_target_fields == ["sf_id"]


def test_plan_payload_round_trips_and_hashes_stably() -> None:
    plan = build_plan(
        source_provider="salesforce",
        target_provider="hubspot",
        inventory=SOURCE_INVENTORY,
        source_objects=SOURCE_OBJECTS,
        suggest_target_object={"contacts": "contacts"},
        destination_inventory=_destination(),
    )
    restored = MigrationPlan.from_payload(plan.to_payload())
    assert restored == plan
    assert restored.plan_hash == plan.plan_hash


def test_rich_mapping_fields_survive_the_payload_round_trip() -> None:
    plan = MigrationPlan(
        source_provider="a",
        target_provider="b",
        routes=[
            RoutePlan(
                route_key="deals|deals|closed=equals:true",
                source_object="deals",
                target_object="deals",
                canonical_type="deals",
                identity_field="id",
                identity_target_fields=["external_id"],
                field_mappings=[
                    MigrationMappingField(
                        source_field="deals.stage",
                        target_object="deals",
                        target_field="dealstage",
                        source_filter=SourceFilter(field="closed", value=True),
                        value_map=[ValueMapEntry(when={"stage": "won"}, value="closedwon")],
                        unmapped_value_policy="preserve",
                    ),
                    MigrationMappingField(
                        source_field="deals.company",
                        target_object="deals",
                        target_field="company",
                        mapping_kind="relationship",
                        source_reference_object="accounts",
                        target_reference_object="companies",
                        relationship_mode="single",
                        association_category="HUBSPOT_DEFINED",
                        association_type_id=5,
                    ),
                ],
                estimated_count=10,
                mapping_origin="auto",
            )
        ],
    )
    restored = MigrationPlan.from_payload(plan.to_payload())
    assert restored == plan
    assert restored.plan_hash == plan.plan_hash
