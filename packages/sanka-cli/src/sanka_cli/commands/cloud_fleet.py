# SPDX-License-Identifier: Apache-2.0
"""Fleet orchestration client; workers and wallet logic stay hosted."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import UUID

import click

import sanka_cli.runtime as runtime
from sanka_cli.certificates import read_json
from sanka_cli.state import CLIState

ROOT = "/v2/migrate/cloud-fleets"
WORKSPACE = click.option("--workspace", required=True, help="Exact workspace code (8 digits).")
FLEET = click.argument("fleet_id", type=click.UUID)
CAP = click.option("--max-credits", required=True, type=click.IntRange(1, 160000))
CONCURRENCY = click.option("--concurrency", required=True, type=click.IntRange(1, 5))
KEY = click.option(
    "--idempotency-key",
    required=True,
    help="Reuse with identical inputs after an uncertain response.",
)
YES = click.option(
    "--yes", is_flag=True, help="Confirm selected repositories and their aggregate credit hold."
)


def request(
    state: CLIState,
    workspace: str,
    method: str,
    path: str,
    *,
    key: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    # Resolve shared Cloud argument validation after Click has loaded both command groups.
    from sanka_cli.commands.cloud import _headers, _intent_key

    headers = _headers(workspace)
    if key is not None:
        headers["Idempotency-Key"] = _intent_key(key)
    return runtime.request_json(state, method, path, headers=headers, **kwargs)


def manifest_items(path: Path, cap: int) -> list[dict[str, Any]]:
    items = read_json(path, 256 * 1024)
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError("Manifest must be a JSON array of 1-20 reviewed repository entries")
    seen, repositories, total = set(), set(), 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"key", "repository", "revision", "request"}:
            raise ValueError("Each entry needs key, repository, revision and a Cloud Run request")
        key, repository, revision, run = (
            item["key"],
            item["repository"],
            item["revision"],
            item["request"],
        )
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", key)
            or key in seen
            or not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", repository)
            or repository in repositories
            or not isinstance(revision, str)
            or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision)
            or not isinstance(run, dict)
        ):
            raise ValueError(
                "Repository identities must be unique and revisions must be exact commit SHAs"
            )
        if not isinstance(run.get("source_id"), str):
            raise ValueError("Each child must select an uploaded source UUID")
        UUID(run["source_id"])
        if not isinstance(run["source_sha256"], str) or not re.fullmatch(
            r"[a-f0-9]{64}", run["source_sha256"]
        ):
            raise ValueError("Each child must pin the uploaded source digest")
        premium = 2000 if run.get("certification") else 1000 if run.get("repair") else 0
        credits = run.get("max_credits")
        if type(credits) is not int or not premium < credits <= premium + 6000:
            raise ValueError(
                "Each child cap must contain its success fee and 1-6,000 compute credits"
            )
        total += credits
        seen.add(key)
        repositories.add(repository)
    if total != cap:
        raise ValueError(f"The parent cap must equal its child caps ({total} credits)")
    return items


@click.group()
def fleet() -> None:
    """Run selected repositories under one total cap and shared concurrency limit."""


@fleet.command("create")
@WORKSPACE
@click.option(
    "--manifest", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@CAP
@CONCURRENCY
@KEY
@YES
@click.pass_obj
def create(
    state: CLIState,
    workspace: str,
    manifest: Path,
    max_credits: int,
    concurrency: int,
    idempotency_key: str,
    yes: bool,
) -> None:
    """Reserve all selected child caps atomically before any repository runs."""
    from sanka_cli.commands.cloud import _headers, _intent_key

    _headers(workspace)
    _intent_key(idempotency_key)
    try:
        items = manifest_items(manifest, max_credits)
    except (ValueError, KeyError, TypeError, OSError, RecursionError) as error:
        raise click.ClickException(str(error)) from error
    if not yes:
        for item in items:
            child = item["request"]
            click.echo(
                f"{item['key']}: {item['repository']}@{item['revision']} | "
                f"source {child['source_id']} SHA-256 {child['source_sha256']} | "
                f"cap {child['max_credits']} credits"
            )
        click.confirm(
            f"Run these {len(items)} repositories in workspace {workspace}? "
            f"Reserve {max_credits} credits total "
            f"with at most {concurrency} active children. "
            "Each child's success fees are inside its cap",
            abort=True,
        )
    runtime.emit_payload(
        request(
            state,
            workspace,
            "POST",
            ROOT,
            key=idempotency_key,
            json_body={"items": items, "max_credits": max_credits, "concurrency": concurrency},
        ),
        state,
    )


@fleet.command("list")
@WORKSPACE
@click.option("--cursor", type=click.UUID, default=None)
@click.pass_obj
def list_fleets(state: CLIState, workspace: str, cursor: Any) -> None:
    runtime.emit_payload(
        request(
            state,
            workspace,
            "GET",
            ROOT,
            params={"limit": 10, **({"cursor": str(cursor)} if cursor else {})},
        ),
        state,
    )


@fleet.command("status")
@WORKSPACE
@FLEET
@click.pass_obj
def status(state: CLIState, workspace: str, fleet_id: Any) -> None:
    """Read partial child results, receipts and the total held/charged/released balance."""
    runtime.emit_payload(request(state, workspace, "GET", f"{ROOT}/{fleet_id}"), state)


@fleet.command("cancel")
@WORKSPACE
@FLEET
@click.pass_obj
def cancel(state: CLIState, workspace: str, fleet_id: Any) -> None:
    """Cancel remaining children; completed work stays intact and active compute can be charged."""
    runtime.emit_payload(request(state, workspace, "POST", f"{ROOT}/{fleet_id}/cancel"), state)


@fleet.command("retry")
@WORKSPACE
@FLEET
@click.option(
    "--item",
    "item_keys",
    multiple=True,
    required=True,
    help="Exact failed child key; repeat to select more.",
)
@CAP
@CONCURRENCY
@KEY
@YES
@click.pass_obj
def retry(
    state: CLIState,
    workspace: str,
    fleet_id: Any,
    item_keys: tuple[str, ...],
    max_credits: int,
    concurrency: int,
    idempotency_key: str,
    yes: bool,
) -> None:
    """Create a new Fleet for selected failed children without rerunning completed work."""
    from sanka_cli.commands.cloud import _data, _intent_key

    _intent_key(idempotency_key)
    original = _data(request(state, workspace, "GET", f"{ROOT}/{fleet_id}"))
    if original.get("id") != str(fleet_id) or not original.get("completed_at"):
        raise click.ClickException("The selected Fleet must be settled before retry")
    children = {item["key"]: item for item in original.get("items", [])}
    if len(set(item_keys)) != len(item_keys) or any(
        key not in children or children[key]["run"]["status"] != "failed" for key in item_keys
    ):
        raise click.ClickException("Select only unique failed child keys")
    if sum(children[key]["run"]["request"]["max_credits"] for key in item_keys) != max_credits:
        raise click.ClickException("Retry cap must equal the selected failed child caps")
    if not yes:
        for key in item_keys:
            item = children[key]
            click.echo(
                f"{key}: {item['repository']}@{item['revision']} | "
                f"original run {item['run']['id']} | "
                f"cap {item['run']['request']['max_credits']} credits"
            )
        click.confirm(
            f"Retry {', '.join(item_keys)} from Fleet {fleet_id} in workspace {workspace}? "
            f"Reserve {max_credits} new credits with concurrency {concurrency}. "
            "Previous receipts remain unchanged",
            abort=True,
        )
    runtime.emit_payload(
        request(
            state,
            workspace,
            "POST",
            f"{ROOT}/{fleet_id}/retry",
            key=idempotency_key,
            json_body={
                "item_keys": list(item_keys),
                "max_credits": max_credits,
                "concurrency": concurrency,
            },
        ),
        state,
    )
