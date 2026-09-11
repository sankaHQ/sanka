# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from sanka.runtime.mapping import (
    AssociationCategory,
    MappingError,
    MigrationMappingField,
    ValueMapEntry,
    apply_transform,
    destination_identity_fields,
    destination_properties,
    mapping_group_key,
    mapping_groups,
    mapping_route_manifest,
    relationship_source_ids,
    source_field_keys,
)
from sanka_extensions.systems import SourceFilter


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-09T20:35:49.000+0000", 1783629349000),
        ("2026-07-09T20:35:49Z", 1783629349000),
        (1783629349000, 1783629349000),
    ],
)
def test_hubspot_datetime_ms_transform(value: object, expected: int) -> None:
    assert apply_transform(value, "hubspot_datetime_ms") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-09", 1783555200000),
        ("2026-07-09T20:35:49.000+0000", 1783555200000),
    ],
)
def test_hubspot_date_ms_transform(value: object, expected: int) -> None:
    assert apply_transform(value, "hubspot_date_ms") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2023年04月期", 1680307200000),
        ("2026年10月期", 1790812800000),
        (" 2026年4月期 ", 1775001600000),
    ],
)
def test_hubspot_date_ms_transform_accepts_japanese_year_month_period(
    value: object,
    expected: int,
) -> None:
    assert apply_transform(value, "hubspot_date_ms") == expected


def test_hubspot_date_ms_transform_rejects_invalid_japanese_year_month_period() -> None:
    field = MigrationMappingField(
        source_field="Registration_course__c.endDate__c",
        target_object="2-410",
        target_field="registration_enddate__c",
        transform_rule="hubspot_date_ms",
    )

    with pytest.raises(MappingError) as exc_info:
        destination_properties({"endDate__c": "2026年13月期"}, [field])

    assert exc_info.value.code == "SANKA_MIGRATE_MAPPING_VALUE_INVALID"
    assert exc_info.value.details == {
        "sourceField": "Registration_course__c.endDate__c",
        "targetField": "registration_enddate__c",
    }


@pytest.mark.parametrize(
    ("value", "rule", "expected"),
    [
        ("  Acme Inc  ", "trim", "Acme Inc"),
        ("1,234.50", "number_parse", 1234.5),
        ("1,234", "number_parse", 1234),
        ("Yes", "boolean_map", True),
        ("off", "boolean_map", False),
        (True, "boolean_map", True),
        (" 2026-07-09 ", "date_parse", "2026-07-09"),
        (None, None, None),
        ("as-is", "", "as-is"),
    ],
)
def test_scalar_transforms(value: object, rule: str | None, expected: object) -> None:
    assert apply_transform(value, rule) == expected


def test_unknown_transform_rule_is_rejected() -> None:
    with pytest.raises(MappingError) as exc_info:
        apply_transform("value", "uppercase")

    assert exc_info.value.code == "SANKA_MIGRATE_TRANSFORM_UNSUPPORTED"


def test_json_stringify_transform_preserves_compound_salesforce_value() -> None:
    assert (
        apply_transform(
            {"city": "Tokyo", "country": "Japan", "latitude": None},
            "json_stringify",
        )
        == '{"city":"Tokyo","country":"Japan","latitude":null}'
    )


def test_destination_properties_maps_composite_source_values() -> None:
    fields = [
        MigrationMappingField(
            source_field="Opportunity.StageName",
            target_object="deals",
            target_field="dealstage",
            value_map=[
                ValueMapEntry(
                    when={
                        "Opportunity.RecordTypeId": "new-business",
                        "Opportunity.StageName": "Discovery",
                    },
                    value="new-business-discovery",
                ),
                ValueMapEntry(
                    when={
                        "Opportunity.RecordTypeId": "renewal",
                        "Opportunity.StageName": "Discovery",
                    },
                    value="renewal-discovery",
                ),
            ],
        )
    ]

    properties = destination_properties(
        {"RecordTypeId": "renewal", "StageName": "Discovery"},
        fields,
    )

    assert properties == {"dealstage": "renewal-discovery"}


def test_destination_properties_rejects_unmapped_value_by_default() -> None:
    field = MigrationMappingField(
        source_field="Opportunity.RecordTypeId",
        target_object="deals",
        target_field="pipeline",
        value_map=[ValueMapEntry(when={"RecordTypeId": "new-business"}, value="pipeline-1")],
    )

    with pytest.raises(MappingError) as exc_info:
        destination_properties({"RecordTypeId": "unknown"}, [field])

    assert exc_info.value.code == "SANKA_MIGRATE_VALUE_MAP_UNMATCHED"
    assert exc_info.value.details == {
        "sourceField": "Opportunity.RecordTypeId",
        "targetField": "pipeline",
    }


def test_destination_properties_rejects_ambiguous_value_map_matches() -> None:
    field = MigrationMappingField(
        source_field="Opportunity.StageName",
        target_object="deals",
        target_field="dealstage",
        value_map=[
            ValueMapEntry(when={"StageName": "Discovery"}, value="stage-1"),
            ValueMapEntry(when={"RecordTypeId": "renewal"}, value="stage-2"),
        ],
    )

    with pytest.raises(MappingError) as exc_info:
        destination_properties(
            {"StageName": "Discovery", "RecordTypeId": "renewal"},
            [field],
        )

    assert exc_info.value.code == "SANKA_MIGRATE_VALUE_MAP_AMBIGUOUS"


def test_destination_properties_can_omit_unmapped_value() -> None:
    field = MigrationMappingField(
        source_field="Opportunity.RecordTypeId",
        target_object="deals",
        target_field="pipeline",
        value_map=[ValueMapEntry(when={"RecordTypeId": "new-business"}, value="pipeline-1")],
        unmapped_value_policy="omit",
    )

    assert destination_properties({"RecordTypeId": "unknown"}, [field]) == {}


def test_destination_properties_can_preserve_unmapped_value() -> None:
    field = MigrationMappingField(
        source_field="Opportunity.RecordTypeId",
        target_object="deals",
        target_field="pipeline",
        value_map=[ValueMapEntry(when={"RecordTypeId": "new-business"}, value="pipeline-1")],
        unmapped_value_policy="preserve",
    )

    assert destination_properties({"RecordTypeId": "unknown"}, [field]) == {"pipeline": "unknown"}


def test_destination_properties_requires_required_source_values() -> None:
    field = MigrationMappingField(
        source_field="Account.Name",
        target_object="companies",
        target_field="name",
        required=True,
    )

    with pytest.raises(MappingError) as exc_info:
        destination_properties({"Name": None}, [field])

    assert exc_info.value.code == "SANKA_MIGRATE_REQUIRED_SOURCE_FIELD_EMPTY"
    assert exc_info.value.details == {
        "sourceField": "Account.Name",
        "targetField": "name",
    }


def test_destination_properties_wraps_invalid_transform_values() -> None:
    field = MigrationMappingField(
        source_field="Account.Employees",
        target_object="companies",
        target_field="numberofemployees",
        transform_rule="number_parse",
    )

    with pytest.raises(MappingError) as exc_info:
        destination_properties({"Employees": "many"}, [field])

    assert exc_info.value.code == "SANKA_MIGRATE_MAPPING_VALUE_INVALID"
    assert exc_info.value.details == {
        "sourceField": "Account.Employees",
        "targetField": "numberofemployees",
    }


def test_source_field_keys_include_composite_value_map_dependencies() -> None:
    field = MigrationMappingField(
        source_field="Opportunity.StageName",
        target_object="deals",
        target_field="dealstage",
        value_map=[
            ValueMapEntry(
                when={
                    "Opportunity.RecordTypeId": "new-business",
                    "StageName": "Discovery",
                },
                value="new-business-discovery",
            )
        ],
    )

    assert source_field_keys([field]) == ["RecordTypeId", "StageName"]


def test_mapping_groups_keep_filtered_routes_separate() -> None:
    fields = [
        MigrationMappingField(
            source_field="Account.Name",
            target_object="companies",
            target_field="name",
            source_filter=SourceFilter(
                field="IsPersonAccount",
                operator="equals",
                value=False,
            ),
        ),
        MigrationMappingField(
            source_field="Account.Name",
            target_object="contacts",
            target_field="firstname",
            source_filter=SourceFilter(
                field="IsPersonAccount",
                operator="equals",
                value=True,
            ),
        ),
    ]

    groups = mapping_groups(fields)

    assert [
        (source, target, source_filter.value)
        for source, target, source_filter, _ in groups
        if source_filter is not None
    ] == [
        ("Account", "companies", False),
        ("Account", "contacts", True),
    ]
    assert mapping_group_key(*groups[0][:3]) != mapping_group_key(*groups[1][:3])


def test_route_manifest_freezes_explicit_identity_fields() -> None:
    groups = mapping_groups(
        [
            MigrationMappingField(
                source_field="Course__c.Id",
                target_object="2-410",
                target_field="salesforce_record_id",
                identity=True,
            ),
            MigrationMappingField(
                source_field="Course__c.Name",
                target_object="2-410",
                target_field="name",
            ),
        ]
    )

    assert mapping_route_manifest(groups) == [
        {
            "routeKey": "Course__c|2-410",
            "sourceObject": "Course__c",
            "destinationObject": "2-410",
            "sourceFilter": None,
            "identityFields": ["salesforce_record_id"],
        }
    ]


def test_route_manifest_includes_source_filter_payload() -> None:
    groups = mapping_groups(
        [
            MigrationMappingField(
                source_field="Account.Name",
                target_object="companies",
                target_field="name",
                source_filter=SourceFilter(field="IsPersonAccount", value=False),
            )
        ]
    )

    assert mapping_route_manifest(groups) == [
        {
            "routeKey": "Account|companies|IsPersonAccount=equals:false",
            "sourceObject": "Account",
            "destinationObject": "companies",
            "sourceFilter": {"field": "IsPersonAccount", "operator": "equals", "value": False},
        }
    ]


def test_destination_identity_fields_skip_relationship_mappings() -> None:
    fields = [
        MigrationMappingField(
            source_field="Course__c.Id",
            target_object="2-410",
            target_field="salesforce_record_id",
            identity=True,
        ),
        MigrationMappingField(
            source_field="Course__c.Program__c",
            target_object="2-410",
            target_field="program",
            source_reference_object="Program__c",
            target_reference_object="2-411",
            mapping_kind="relationship",
            identity=True,
        ),
    ]

    assert destination_identity_fields(fields) == ["salesforce_record_id"]


def test_relationship_source_ids_normalize_and_deduplicate() -> None:
    field = MigrationMappingField(
        source_field="Order.LineItems",
        target_object="orders",
        target_field="line_items",
        source_reference_object="LineItem",
        target_reference_object="line_items",
        mapping_kind="relationship",
    )

    assert relationship_source_ids(
        {
            "LineItems": [
                {"target_record_id": "li-1"},
                {"id": "li-2"},
                {"value": "li-3"},
                "li-1",
                "  li-4  ",
                None,
                "",
            ]
        },
        field,
    ) == ["li-1", "li-2", "li-3", "li-4"]
    assert relationship_source_ids({"LineItems": "li-9"}, field) == ["li-9"]


def test_relationship_mapping_accepts_explicit_association_type() -> None:
    field = MigrationMappingField(
        source_field="Project__c.Estimate__c",
        source_reference_object="Estimate__c",
        target_object="2-300",
        target_field="project_estimate",
        target_reference_object="2-301",
        mapping_kind="relationship",
        association_category="USER_DEFINED",
        association_type_id=204,
    )

    assert field.association_category == "USER_DEFINED"
    assert field.association_type_id == 204


@pytest.mark.parametrize(
    ("association_category", "association_type_id"),
    [("USER_DEFINED", None), (None, 204)],
)
def test_relationship_mapping_rejects_partial_association_type(
    association_category: AssociationCategory | None,
    association_type_id: int | None,
) -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        MigrationMappingField(
            source_field="Project__c.Estimate__c",
            source_reference_object="Estimate__c",
            target_object="2-300",
            target_field="project_estimate",
            target_reference_object="2-301",
            mapping_kind="relationship",
            association_category=association_category,
            association_type_id=association_type_id,
        )


def test_scalar_mapping_rejects_association_type() -> None:
    with pytest.raises(ValueError, match="only valid for relationship mappings"):
        MigrationMappingField(
            source_field="Project__c.Name",
            target_object="2-300",
            target_field="name",
            association_category="USER_DEFINED",
            association_type_id=204,
        )


def test_reference_mapping_requires_both_reference_objects() -> None:
    with pytest.raises(ValueError, match="target_reference_object is required"):
        MigrationMappingField(
            source_field="Task__c.Product__c",
            source_reference_object="Product__c",
            target_object="line_items",
            target_field="hs_product_id",
            mapping_kind="reference",
        )


def test_reference_mapping_rejects_association_type() -> None:
    with pytest.raises(ValueError, match="not valid for reference mappings"):
        MigrationMappingField(
            source_field="Task__c.Product__c",
            source_reference_object="Product__c",
            target_object="line_items",
            target_field="hs_product_id",
            target_reference_object="products",
            mapping_kind="reference",
            association_category="USER_DEFINED",
            association_type_id=204,
        )


def test_value_map_is_rejected_for_non_scalar_mappings() -> None:
    with pytest.raises(ValueError, match="only valid for scalar mappings"):
        MigrationMappingField(
            source_field="Contact.OwnerEmail",
            target_object="contacts",
            target_field="hubspot_owner_id",
            mapping_kind="owner",
            value_map=[ValueMapEntry(when={"OwnerEmail": "a@example.com"}, value="1")],
        )


def test_value_map_predicates_must_be_unique() -> None:
    with pytest.raises(ValueError, match="predicates must be unique"):
        MigrationMappingField(
            source_field="Opportunity.StageName",
            target_object="deals",
            target_field="dealstage",
            value_map=[
                ValueMapEntry(when={"StageName": "Discovery"}, value="stage-1"),
                ValueMapEntry(when={"StageName": "Discovery"}, value="stage-2"),
            ],
        )


def test_value_map_entry_normalizes_predicate_field_names() -> None:
    entry = ValueMapEntry(when={"  StageName  ": "Discovery"}, value="stage-1")

    assert entry.when == {"StageName": "Discovery"}

    with pytest.raises(ValueError, match="must not be empty"):
        ValueMapEntry(when={"   ": "Discovery"}, value="stage-1")
    with pytest.raises(ValueError, match="must be unique"):
        ValueMapEntry(when={"StageName": "a", " StageName ": "b"}, value="stage-1")
