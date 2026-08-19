# SPDX-License-Identifier: Apache-2.0
"""Salesforce connector: a source role for Sanka Migrate migrations.

Reads a Salesforce org through the REST API: sObject discovery and describes,
exact record counts, an active-user owner directory, and keyset-paginated
SOQL reads on ``Id`` with snapshot bounds. Source role only — the production
adapter this ports never wrote to Salesforce, and neither does this connector.

Credentials carry the OAuth access token in ``access_token`` and the org's
instance URL in ``settings["instance_url"]`` (both required). When
``refresh_token`` + ``client_id`` + ``client_secret`` are also present,
expired access tokens refresh automatically. See the package README for the
detailed pagination, filtering, and error-mapping semantics.
"""

from __future__ import annotations

from sanka.connector import ConnectorRegistration
from sanka_connector_salesforce._gateway import (
    HttpSalesforceGateway,
    SalesforceGateway,
    TokenRefreshResult,
)
from sanka_connector_salesforce._source import SalesforceSource

__all__ = [
    "CONNECTOR",
    "HttpSalesforceGateway",
    "SalesforceGateway",
    "SalesforceSource",
    "TokenRefreshResult",
]

CONNECTOR = ConnectorRegistration(name="salesforce", source=SalesforceSource())
