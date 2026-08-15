# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from ferry.connector import FieldSchema, Inventory, ObjectSchema
from ferry.runtime.mapping import generate_mapping_candidates


def _object(
    *,
    key: str,
    canonical_type: str,
    fields: list[tuple[str, str, str]],
) -> ObjectSchema:
    return ObjectSchema(
        key=key,
        label=key,
        canonical_type=canonical_type,
        record_count=3,
        fields=[
            FieldSchema(key=field_key, label=label, data_type=data_type)
            for field_key, label, data_type in fields
        ],
    )


def test_mapping_candidates_match_provider_neutral_schema_names() -> None:
    source = Inventory(
        provider="crm-a",
        connection_id="source-connection",
        objects=[
            _object(
                key="Organization",
                canonical_type="company",
                fields=[
                    ("Name", "Organization Name", "string"),
                    ("Website", "Website", "url"),
                ],
            )
        ],
    )
    destination = Inventory(
        provider="crm-b",
        connection_id="destination-connection",
        objects=[
            _object(
                key="companies",
                canonical_type="company",
                fields=[
                    ("name", "Company name", "string"),
                    ("website", "Website URL", "string"),
                ],
            )
        ],
    )

    candidates = generate_mapping_candidates(source, destination)

    assert [(row.source_field, row.target_field) for row in candidates.fields] == [
        ("Organization.Name", "name"),
        ("Organization.Website", "website"),
    ]
    assert candidates.coverage["mapped"] == 2
    assert candidates.coverage["sourceFields"] == 2


def test_mapping_candidates_do_not_cross_canonical_object_boundaries() -> None:
    source = Inventory(
        provider="crm-a",
        connection_id="source-connection",
        objects=[
            _object(
                key="Person",
                canonical_type="contact",
                fields=[("Name", "Name", "string")],
            )
        ],
    )
    destination = Inventory(
        provider="crm-b",
        connection_id="destination-connection",
        objects=[
            _object(
                key="companies",
                canonical_type="company",
                fields=[("name", "Name", "string")],
            )
        ],
    )

    candidates = generate_mapping_candidates(source, destination)

    assert candidates.fields == []
    assert candidates.coverage["mapped"] == 0


def test_mapping_candidates_mark_owner_email_matches() -> None:
    source = Inventory(
        provider="crm-a",
        connection_id="source-connection",
        objects=[
            _object(
                key="Contact",
                canonical_type="contact",
                fields=[("OwnerEmail", "Owner email", "email")],
            )
        ],
    )
    destination = Inventory(
        provider="crm-b",
        connection_id="destination-connection",
        objects=[
            _object(
                key="contacts",
                canonical_type="contact",
                fields=[("ownerid", "Owner", "lookup")],
            )
        ],
    )

    candidates = generate_mapping_candidates(source, destination)

    assert candidates.fields[0].mapping_kind == "owner"


def test_mapping_candidates_mark_unique_destination_property_as_identity() -> None:
    source = Inventory(
        provider="salesforce",
        connection_id="source-connection",
        objects=[
            _object(
                key="Course__c",
                canonical_type="course",
                fields=[("Id", "Salesforce Record ID", "string")],
            )
        ],
    )
    destination = Inventory(
        provider="hubspot",
        connection_id="destination-connection",
        objects=[
            ObjectSchema(
                key="2-410",
                label="Courses",
                canonical_type="course",
                record_count=0,
                fields=[
                    FieldSchema(
                        key="salesforce_record_id",
                        label="Salesforce Record ID",
                        data_type="string",
                        unique=True,
                    )
                ],
                identity_fields=["salesforce_record_id"],
            )
        ],
    )

    candidates = generate_mapping_candidates(source, destination)

    assert candidates.fields[0].identity is True


def test_mapping_candidates_assign_destination_fields_one_to_one() -> None:
    source = Inventory(
        provider="crm-a",
        connection_id="source-connection",
        objects=[
            _object(
                key="Organization",
                canonical_type="company",
                fields=[
                    ("Name", "Name", "string"),
                    ("CompanyName", "Name", "string"),
                ],
            )
        ],
    )
    destination = Inventory(
        provider="crm-b",
        connection_id="destination-connection",
        objects=[
            _object(
                key="companies",
                canonical_type="company",
                fields=[("name", "Name", "string")],
            )
        ],
    )

    candidates = generate_mapping_candidates(source, destination)

    assert [(row.source_field, row.target_field) for row in candidates.fields] == [
        ("Organization.Name", "name"),
    ]


def test_mapping_candidates_skip_low_scores_and_flag_incompatible_types() -> None:
    source = Inventory(
        provider="crm-a",
        connection_id="source-connection",
        objects=[
            _object(
                key="Organization",
                canonical_type="company",
                fields=[
                    ("EmployeeCount", "Employee count", "string"),
                    ("Notes", "Internal notes", "string"),
                ],
            )
        ],
    )
    destination = Inventory(
        provider="crm-b",
        connection_id="destination-connection",
        objects=[
            _object(
                key="companies",
                canonical_type="company",
                fields=[
                    ("employee_count", "Employee count", "number"),
                    ("domain", "Company domain", "string"),
                ],
            )
        ],
    )

    candidates = generate_mapping_candidates(source, destination)

    assert [(row.source_field, row.target_field) for row in candidates.fields] == [
        ("Organization.EmployeeCount", "employee_count"),
    ]
    assert candidates.fields[0].transform_rule == "number_parse"
    assert candidates.coverage == {
        "sourceFields": 2,
        "mapped": 1,
        "required": 0,
        "incompatible": 1,
    }
