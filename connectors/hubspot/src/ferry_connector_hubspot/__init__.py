# SPDX-License-Identifier: Apache-2.0
"""HubSpot connector: source + destination roles for Ferry migrations.

A port of Sanka's production HubSpot migration adapter. Authentication is a
bearer token in ``Credentials.access_token`` (HubSpot private-app token, or
an OAuth access token kept fresh by the caller's credential provider). The
source reads the standard CRM objects and custom object schemas through the
search API; the destination writes with conflict policies, 100-record
batches reconciled by ``objectWriteTraceId``, association batches, schema
provisioning with a pure dry run, and a paced, retrying provider-control
loop. See the package README for the detailed semantics.
"""

from __future__ import annotations

from ferry.connector import ConnectorRegistration
from ferry_connector_hubspot._destination import HubSpotDestination
from ferry_connector_hubspot._source import HubSpotSource

__all__ = ["CONNECTOR", "HubSpotDestination", "HubSpotSource"]

CONNECTOR = ConnectorRegistration(
    name="hubspot",
    source=HubSpotSource(),
    destination=HubSpotDestination(),
)
