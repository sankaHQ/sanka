# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from sanka.connector import (
    ConnectorError,
    Credentials,
    CustomObjectDefinition,
    CustomObjectProperty,
    ErrorCategory,
    PipelineStage,
    RateLimitError,
    SchemaMismatchError,
    SourceFilter,
)


def test_credentials_repr_hides_secrets() -> None:
    credentials = Credentials(
        provider="hubspot",
        connection_id="conn-1",
        access_token="tok-SECRET",
        refresh_token="ref-SECRET",
        client_secret="cs-SECRET",
        settings={"dsn": "postgres://user:pw-SECRET@host/db"},
    )
    rendered = repr(credentials)
    assert "SECRET" not in rendered
    assert "hubspot" in rendered


def test_error_retryable_defaults_follow_category() -> None:
    assert RateLimitError("slow down", retry_after_seconds=1.5).retryable
    assert not SchemaMismatchError("missing enum value").retryable
    generic = ConnectorError("boom", category=ErrorCategory.TRANSIENT)
    assert generic.retryable
    pinned = ConnectorError("boom", category=ErrorCategory.TRANSIENT, retryable=False)
    assert not pinned.retryable


def test_source_filter_requires_field() -> None:
    with pytest.raises(ValueError):
        SourceFilter(field="   ")


def test_pipeline_stage_probability_defaults_to_none() -> None:
    assert PipelineStage(key="new", label="New").probability is None


@pytest.mark.parametrize("probability", [0.0, 0.25, 1.0])
def test_pipeline_stage_probability_accepts_unit_interval(probability: float) -> None:
    assert PipelineStage(key="won", label="Won", probability=probability).probability == probability


@pytest.mark.parametrize("probability", [-0.01, 1.01, 7.0])
def test_pipeline_stage_probability_rejects_out_of_bounds(probability: float) -> None:
    with pytest.raises(ValueError):
        PipelineStage(key="won", label="Won", probability=probability)


def test_custom_object_property_flag_defaults() -> None:
    prop = CustomObjectProperty(
        source_field="OrderNumber",
        internal_name="order_number",
        label="Order number",
    )
    assert prop.source_type is None
    assert prop.required is False
    assert prop.unique is False
    assert prop.searchable is False


def test_custom_object_definition_properties_default_to_empty() -> None:
    definition = CustomObjectDefinition(
        key="orders",
        source_object="Order",
        internal_name="orders",
        singular_label="Order",
        plural_label="Orders",
        primary_display_property="order_number",
    )
    assert definition.properties == []
    assert definition.associated_objects == []
