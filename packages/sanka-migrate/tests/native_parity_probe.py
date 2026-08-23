# SPDX-License-Identifier: AGPL-3.0-only
"""Serve fixture scenarios against one side of a native migration.

Executed as a fresh subprocess per side so Django can be configured exactly
once per process. Both sides seed the same rows and replay the same scenario
sequence; the test compares the emitted responses and final database state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


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
    os.environ["DJANGO_SETTINGS_MODULE"] = "crud_config.settings"
    sys.path.insert(0, str(project))

    import django  # type: ignore[import-untyped]

    django.setup()
    from django.core.management import call_command  # type: ignore[import-untyped]

    call_command("migrate", interactive=False, verbosity=0, run_syncdb=True)
    from inventory.models import Gadget  # type: ignore[import-not-found]

    Gadget.objects.all().delete()
    Gadget.objects.create(id=1, name="Alpha", quantity=3, notes="")

    scenarios: list[dict[str, Any]] = json.loads(args.scenarios)
    if args.mode == "source":
        results = _run_source(scenarios)
    else:
        if not args.output:
            raise SystemExit("--output is required in native mode")
        results = _run_native(Path(args.output).resolve(), scenarios)

    from django.db import connection  # type: ignore[import-untyped]

    connection.close()
    payload = {
        "results": results,
        "database": list(Gadget.objects.order_by("id").values("id", "name", "quantity", "notes")),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


def _run_source(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from django.test import Client  # type: ignore[import-untyped]

    client = Client()
    results = []
    for scenario in scenarios:
        data = _raw_body(scenario)
        response = client.generic(
            str(scenario["method"]),
            str(scenario["path"]),
            data=data,
            content_type="application/json",
        )
        results.append({"status": response.status_code, "body": _body(bytes(response.content))})
    return results


def _run_native(output: Path, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sys.path.insert(0, str(output))
    from importlib import import_module

    from fastapi.testclient import TestClient

    app = import_module("app").app
    client = TestClient(app, follow_redirects=False)
    results = []
    for scenario in scenarios:
        response = client.request(
            str(scenario["method"]),
            str(scenario["path"]),
            content=_raw_body(scenario),
            headers={"content-type": "application/json"},
        )
        results.append({"status": response.status_code, "body": _body(response.content)})
    return results


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
