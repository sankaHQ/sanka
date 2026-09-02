# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

import click

import sanka_cli.runtime as runtime
from sanka_cli.state import CLIState


@click.group()
def ai() -> None:
    """AI commands."""


@ai.group("score")
def ai_score() -> None:
    """Score records."""


def _score_command(object_type: str) -> click.Command:
    @click.command(name=object_type)
    @click.argument("record_id")
    @click.option("--score-model-id", default=None)
    @click.pass_obj
    def command(
        state: CLIState,
        record_id: str,
        score_model_id: str | None,
    ) -> None:
        body: dict[str, Any] = {
            "object_type": object_type,
            "record_id": record_id,
        }
        if score_model_id:
            body["score_model_id"] = score_model_id
        payload = runtime.request_json(state, "POST", "/v2/score", json_body=body)
        runtime.emit_payload(payload, state)

    return command


ai_score.add_command(_score_command("company"))
ai_score.add_command(_score_command("deal"))


@ai.group("enrich")
def ai_enrich() -> None:
    """Enrich records."""


@ai_enrich.command("company")
@click.argument("record_id", required=False)
@click.option("--force-refresh", is_flag=True, default=False)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--custom-field-map", default=None, help="JSON string or @file.json")
@click.option("--seed-name", default=None)
@click.option("--seed-url", default=None)
@click.option("--seed-external-id", default=None)
@click.pass_obj
def ai_enrich_company(
    state: CLIState,
    record_id: str | None,
    force_refresh: bool,
    dry_run: bool,
    custom_field_map: str | None,
    seed_name: str | None,
    seed_url: str | None,
    seed_external_id: str | None,
) -> None:
    has_seed = bool(seed_name or seed_url or seed_external_id)
    if record_id and has_seed:
        raise click.ClickException("Use either record_id or seed fields, not both")
    if not record_id and not has_seed:
        raise click.ClickException("record_id or a seed field is required")
    if has_seed and not dry_run:
        raise click.ClickException("--dry-run is required when seed fields are provided")
    if has_seed and custom_field_map:
        raise click.ClickException("--custom-field-map is only supported with record_id")

    body: dict[str, Any] = {
        "object_type": "company",
        "dry_run": dry_run,
        "force_refresh": force_refresh,
    }
    if record_id:
        body["record_id"] = record_id
        if custom_field_map:
            body["custom_field_map"] = runtime.parse_json_input(custom_field_map)
    else:
        body["seed"] = {
            key: value
            for key, value in {
                "name": seed_name,
                "url": seed_url,
                "external_id": seed_external_id,
            }.items()
            if value
        }

    payload = runtime.request_json(state, "POST", "/v2/enrich", json_body=body)
    runtime.emit_payload(payload, state)
