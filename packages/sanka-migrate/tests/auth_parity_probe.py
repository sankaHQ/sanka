# SPDX-License-Identifier: AGPL-3.0-only
"""Serve auth-fixture scenarios against one side of a native migration.

Mirrors ``native_parity_probe`` for the token-auth fixture: seeds users,
tokens, and bulletins, replays the same scenario sequence on either the DRF
source or the generated native app, and always captures the ``Allow`` and
``WWW-Authenticate`` headers into the compared responses.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

CAPTURED_HEADERS = ("allow", "www-authenticate")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("source", "native"), required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--database", required=True)
    parser.add_argument("--scenarios", required=True)
    args = parser.parse_args()

    project = Path(args.project).resolve()
    os.environ["SANKA_TEST_DB"] = str(Path(args.database).resolve())
    os.environ["DJANGO_SETTINGS_MODULE"] = "board_config.settings"
    sys.path.insert(0, str(project))

    import django  # type: ignore[import-untyped]

    django.setup()
    from django.core.management import call_command  # type: ignore[import-untyped]

    call_command("migrate", interactive=False, verbosity=0, run_syncdb=True)
    from bulletins.models import Bulletin  # type: ignore[import-not-found]
    from django.contrib.auth.models import User  # type: ignore[import-untyped]
    from rest_framework.authtoken.models import Token  # type: ignore[import-untyped]

    Bulletin.objects.all().delete()
    Token.objects.all().delete()
    User.objects.all().delete()
    alice = User.objects.create(id=1, username="alice")
    bob = User.objects.create(id=2, username="bob")
    carol = User.objects.create(id=3, username="carol", is_active=False)
    Token.objects.create(user=alice, key="a" * 40)
    Token.objects.create(user=bob, key="b" * 40)
    Token.objects.create(user=carol, key="c" * 40)
    Bulletin.objects.create(id=1, author=alice, title="First", body="hello")
    Bulletin.objects.create(id=2, author=bob, title="Second", body="world")

    scenarios: list[dict[str, Any]] = json.loads(args.scenarios)
    if args.mode == "source":
        results = _run_source(scenarios)
    else:
        if not args.output:
            raise SystemExit("--output is required in native mode")
        from django.db import connection  # type: ignore[import-untyped]

        connection.close()
        results = _run_native(Path(args.output).resolve(), scenarios)

    from django.db import connection  # type: ignore[import-untyped]

    connection.close()
    payload = {
        "results": results,
        "database": list(
            Bulletin.objects.order_by("id").values("id", "author_id", "title", "body")
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


def _run_source(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from django.test import Client  # type: ignore[import-untyped]

    client = Client()
    results = []
    for scenario in scenarios:
        headers = _headers(scenario)
        django_headers = {
            "HTTP_" + key.upper().replace("-", "_"): value for key, value in headers.items()
        }
        response = client.generic(
            str(scenario["method"]),
            str(scenario["path"]),
            data=_raw_body(scenario),
            content_type="application/json",
            **django_headers,
        )
        results.append(
            {
                "status": response.status_code,
                "body": _body(bytes(response.content)),
                "headers": {name: str(response.get(name, "")) for name in CAPTURED_HEADERS},
            }
        )
    return results


def _run_native(output: Path, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sys.path.insert(0, str(output))
    from importlib import import_module

    from fastapi.testclient import TestClient

    app = import_module("app").app
    with TestClient(app, follow_redirects=False) as client:
        results = []
        for scenario in scenarios:
            response = client.request(
                str(scenario["method"]),
                str(scenario["path"]),
                content=_raw_body(scenario),
                headers={"content-type": "application/json", **_headers(scenario)},
            )
            results.append(
                {
                    "status": response.status_code,
                    "body": _body(response.content),
                    "headers": {name: response.headers.get(name, "") for name in CAPTURED_HEADERS},
                }
            )
        return results


def _headers(scenario: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in dict(scenario.get("headers") or {}).items()}


def _raw_body(scenario: dict[str, Any]) -> str:
    if "raw_body" in scenario:
        return str(scenario["raw_body"])
    body = scenario.get("body")
    return json.dumps(body) if body is not None else ""


def _body(content: bytes) -> Any:
    if not content:
        return None
    try:
        return json.loads(content)
    except (ValueError, json.JSONDecodeError):
        return content.decode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
