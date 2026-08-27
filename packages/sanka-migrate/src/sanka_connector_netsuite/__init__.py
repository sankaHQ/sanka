# SPDX-License-Identifier: Apache-2.0
"""NetSuite connector: a source role for Sanka migrations.

Reads a NetSuite account through SuiteQL: a fixed ERP object registry
(customers, vendors, inventory items, sales orders, invoices), exact record
counts, and keyset-paginated reads on the numeric ``id`` with snapshot
bounds. Source role only — the SuiteQL surface this consumes is read-only,
and so is this connector.

Credentials carry the bearer token in ``access_token`` and the account's API
base URL in ``settings["api_base_url"]`` (for example
``https://<account>.suitetalk.api.netsuite.com``; both required unless
``client_id`` + ``client_secret`` are supplied for the OAuth 2.0
client-credentials grant, in which case the token is minted and refreshed
automatically). Optional ``settings["token_url"]`` overrides the grant
endpoint. See the package README for the detailed pagination, query, and
error-mapping semantics.
"""

from __future__ import annotations

from sanka.connector import ConnectorRegistration
from sanka_connector_netsuite._gateway import (
    HttpNetSuiteGateway,
    NetSuiteGateway,
    netsuite_api_base_url,
)
from sanka_connector_netsuite._source import NetSuiteSource

__all__ = [
    "CONNECTOR",
    "HttpNetSuiteGateway",
    "NetSuiteGateway",
    "NetSuiteSource",
    "netsuite_api_base_url",
]

CONNECTOR = ConnectorRegistration(name="netsuite", source=NetSuiteSource())
