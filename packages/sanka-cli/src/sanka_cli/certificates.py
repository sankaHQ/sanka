# SPDX-License-Identifier: Apache-2.0
"""Public certificate input and verification helpers; no hosted execution code."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def read_json(path: Path, maximum: int = 192 * 1024) -> Any:
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("JSON file exceeds its size limit")
    return json.loads(data)


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    cases = read_json(path, 48 * 1024)
    if not isinstance(cases, list) or not 1 <= len(cases) <= 50:
        raise ValueError("Cases must be a JSON array containing 1 to 50 requests")
    seen, normalized = set(), []
    for case in cases:
        if not isinstance(case, dict) or set(case) - {"id", "method", "path", "json_body"}:
            raise ValueError("Each case accepts only id, method, path and json_body")
        identifier, method, value = case.get("id"), case.get("method"), case.get("path")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier)
            or identifier in seen
            or method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            or not isinstance(value, str)
            or not 1 <= len(value) <= 1000
        ):
            raise ValueError("Each case needs a unique ID, supported method and local HTTP path")
        seen.add(identifier)
        parsed, decoded = urlsplit(value), unquote(value)
        if (
            not value.startswith("/")
            or value.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or "\\" in decoded
            or any(ord(char) < 32 or ord(char) > 126 for char in decoded)
            or ".." in decoded.split("?", 1)[0].split("/")
        ):
            raise ValueError("Case paths must be local ASCII HTTP paths without traversal")
        body = case.get("json_body")
        if len(canonical_bytes({"body": body})) > 8192 or (method == "GET" and body is not None):
            raise ValueError("Case bodies are limited to 8 KiB; GET cannot have a body")
        normalized.append({"id": identifier, "method": method, "path": value, "json_body": body})
    if len(canonical_bytes(normalized)) > 47 * 1024:
        raise ValueError("The normalized certificate scope exceeds its size limit")
    return normalized


def verify_certificate(document: dict[str, Any], key_document: dict[str, Any]) -> dict[str, Any]:
    """Verify using keys supplied by the caller's chosen trusted source only."""
    signed = document.get("certificate", document)
    key_document = key_document.get("data", key_document)
    try:
        payload = signed["payload"]
        if (
            signed["algorithm"] != "Ed25519"
            or signed["encoding"] != "sorted-ascii-json-v1"
            or payload["schema_version"] != "developer-certificate-v1"
            or payload["issuer"] != "https://api-v2.sanka.com"
            or not isinstance(payload, dict)
        ):
            raise ValueError("Unsupported certificate issuer or signature format")
        matches = [row for row in key_document["keys"] if row["key_id"] == signed["key_id"]]
        if len(matches) != 1 or matches[0].get("algorithm") != "Ed25519":
            raise ValueError("The signing key is not in the selected trusted key ring")
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(matches[0]["public_key_base64"], validate=True)
        )
        key.verify(
            base64.b64decode(signed["signature_base64"], validate=True), canonical_bytes(payload)
        )
    except InvalidSignature as error:
        raise ValueError("Certificate signature is invalid") from error
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Certificate or trusted key ring has an invalid format") from error
    return payload
