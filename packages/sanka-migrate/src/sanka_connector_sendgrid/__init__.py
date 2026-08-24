# SPDX-License-Identifier: Apache-2.0
"""Twilio SendGrid Marketing Contacts source connector for Sanka."""

from __future__ import annotations

from sanka.connector import ConnectorRegistration
from sanka_connector_sendgrid._gateway import HttpSendGridGateway, SendGridGateway
from sanka_connector_sendgrid._source import SendGridSource

__all__ = ["CONNECTOR", "HttpSendGridGateway", "SendGridGateway", "SendGridSource"]

CONNECTOR = ConnectorRegistration(name="sendgrid", source=SendGridSource())
