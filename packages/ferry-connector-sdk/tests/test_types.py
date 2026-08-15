# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from ferry.connector import (
    ConnectorError,
    Credentials,
    ErrorCategory,
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
