# SPDX-License-Identifier: Apache-2.0
"""Credential types and the provider protocol.

The runtime resolves a named connection through a :class:`CredentialProvider`
and hands the resulting :class:`Credentials` to connector methods. Secret
fields are excluded from ``repr`` so credentials never leak through logs,
reports, or exception messages by accident — but any string field may still be
sensitive; never log credentials wholesale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True, kw_only=True)
class Credentials:
    """Resolved credentials for one connection.

    OAuth-style providers use the token/client fields; databases and files
    carry connection strings, paths, and hosts in ``settings``. ``settings``
    values may be sensitive even though only the token fields are hidden from
    ``repr`` — treat the whole object as secret material.
    """

    provider: str
    connection_id: str | None = None
    account_id: str | None = None
    account_username: str | None = None
    access_token: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    client_id: str | None = field(default=None, repr=False)
    client_secret: str | None = field(default=None, repr=False)
    redirect_uri: str | None = None
    settings: dict[str, Any] = field(default_factory=dict, repr=False)


@runtime_checkable
class CredentialProvider(Protocol):
    """Resolves a connection name to credentials.

    Local runtimes read config files or environment variables; hosted runtimes
    resolve managed OAuth connections. Implementations must never log or
    persist decrypted secrets outside their own boundary.
    """

    async def resolve(self, connection: str) -> Credentials: ...


@runtime_checkable
class SupportsCredentialRefresh(Protocol):
    """Optional: a provider that can refresh expired credentials in place."""

    async def refresh(self, credentials: Credentials) -> Credentials: ...
