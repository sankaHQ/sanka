# SPDX-License-Identifier: Apache-2.0
"""Integration tests against a live SuiteQL endpoint.

Strictly read-only. Point these at a NetSuite account or at a
contract-compatible isolated environment (for example the public Sanka demo
simulator with a browser-minted token). They skip cleanly without the
environment variables.
"""

from __future__ import annotations

import os

import pytest

from sanka.connector import Credentials
from sanka_connector_netsuite import NetSuiteSource

API_BASE_URL = os.environ.get("SANKA_MIGRATE_TEST_NETSUITE_API_BASE_URL", "")
ACCESS_TOKEN = os.environ.get("SANKA_MIGRATE_TEST_NETSUITE_ACCESS_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not API_BASE_URL or not ACCESS_TOKEN,
    reason=(
        "set SANKA_MIGRATE_TEST_NETSUITE_API_BASE_URL and"
        " SANKA_MIGRATE_TEST_NETSUITE_ACCESS_TOKEN to run NetSuite integration tests"
    ),
)


@pytest.fixture
def credentials() -> Credentials:
    return Credentials(
        provider="netsuite",
        connection_id="integration-test",
        access_token=ACCESS_TOKEN,
        settings={"api_base_url": API_BASE_URL},
    )


@pytest.fixture
def source() -> NetSuiteSource:
    return NetSuiteSource()


async def test_inventory_reports_the_fixed_registry(
    source: NetSuiteSource,
    credentials: Credentials,
) -> None:
    inventory = await source.inventory(credentials, object_types=["customer"])
    assert inventory.provider == "netsuite"
    assert inventory.warnings == []
    (customer,) = inventory.objects
    assert customer.key == "customer"
    assert customer.record_count > 0
    assert customer.identity_fields == ["id"]


async def test_reads_paginate_deterministically(
    source: NetSuiteSource,
    credentials: Credentials,
) -> None:
    first = await source.read_records(
        credentials,
        object_type="customer",
        field_keys=["entityid", "companyname"],
        limit=5,
    )
    assert first.records
    assert all(str(record.get("id") or "").strip() for record in first.records)
    if first.has_more:
        assert first.next_cursor is not None
        second = await source.read_records(
            credentials,
            object_type="customer",
            field_keys=["entityid", "companyname"],
            limit=5,
            cursor=first.next_cursor,
        )
        first_ids = {str(record["id"]) for record in first.records}
        second_ids = {str(record["id"]) for record in second.records}
        assert not first_ids & second_ids


async def test_counts_and_high_water_mark_agree_with_reads(
    source: NetSuiteSource,
    credentials: Credentials,
) -> None:
    count = await source.count_records(credentials, object_type="salesOrder")
    mark = await source.high_water_mark(credentials, object_type="salesOrder")
    assert count >= 0
    if count > 0:
        assert mark is not None
        bounded = await source.count_records_bounded(
            credentials,
            object_type="salesOrder",
            upper_bound=mark,
        )
        assert bounded == count
