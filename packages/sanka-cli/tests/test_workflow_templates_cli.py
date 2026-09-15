# SPDX-License-Identifier: AGPL-3.0-only
# mypy: disable-error-code="no-untyped-def,type-arg"
"""Hosted template creation requires one immutable review and preserves raw create."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from click.testing import CliRunner

from sanka_cli.main import cli

REQUEST = "e434ca67-7653-4fbf-b3b0-d970c217927f"
DIGEST = "sha256:" + "a" * 64
TEMPLATE = "billing.hubspot-deal-invoices"


@pytest.fixture
def template_request(tmp_path, monkeypatch):
    config = tmp_path / "billing.json"
    config.write_text(json.dumps({"invoice_due_days": 45}), encoding="utf-8")
    calls = []
    override = {}

    def request(state, method, path, *, json_body):
        calls.append((method, path, json_body))
        if path.endswith("/plan"):
            return {
                "success": True,
                "data": {
                    "request_id": json_body["request_id"],
                    "template_id": TEMPLATE,
                    "template_version": 1,
                    "operation": "create",
                    "workflow_id": None,
                    "construction": "inactive",
                    "applicable": True,
                    "parameters": json_body["parameters"],
                    "plan_digest": DIGEST,
                    **override,
                },
            }
        return {"success": True, "data": {"workflow_id": "workflow-1", "status": "constructed"}}

    monkeypatch.setattr("sanka_cli.runtime.request_json", request)
    return (
        [
            "--output",
            "json",
            "workflows",
            "create",
            "--template",
            TEMPLATE,
            "--config",
            str(config),
        ],
        calls,
        override,
    )


def test_unattended_default_previews_without_creating(template_request):
    args, calls, _ = template_request
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["data"]
    assert str(UUID(payload["request_id"])) == payload["request_id"]
    assert payload["parameters"] == {"invoice_due_days": 45}
    assert len(calls) == 1 and calls[0][1].endswith("/templates/plan")
    assert "--request-id " + payload["request_id"] in result.stderr
    assert "--approve-plan " + DIGEST in result.stderr


def test_approved_digest_uses_original_request_and_constructs_once(template_request):
    args, calls, _ = template_request
    result = CliRunner().invoke(cli, [*args, "--request-id", REQUEST, "--approve-plan", DIGEST])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["workflow_id"] == "workflow-1"
    assert [call[1] for call in calls] == [
        "/v2/public/workflows/templates/plan",
        "/v2/public/workflows/templates/use",
    ]
    assert calls[0][2]["request_id"] == REQUEST
    assert calls[1][2] == {"request_id": REQUEST, "plan_digest": DIGEST}


@pytest.mark.parametrize(
    "override",
    [
        {"request_id": "foreign"},
        {"template_id": "foreign"},
        {"template_version": 2},
        {"operation": "update"},
        {"workflow_id": "existing"},
        {"construction": "active"},
        {"plan_digest": "invalid"},
        {"plan_digest": "sha256:" + "b" * 64},
        {"applicable": False},
    ],
)
def test_changed_or_blocked_plan_never_constructs(template_request, override):
    args, calls, response = template_request
    response.update(override)
    result = CliRunner().invoke(cli, [*args, "--request-id", REQUEST, "--approve-plan", DIGEST])
    assert result.exit_code == 1
    assert len(calls) == 1


@pytest.mark.parametrize(
    "extra",
    [
        ["--approve-plan", DIGEST],
        ["--request-id", REQUEST, "--approve-plan", "bad"],
        ["--request-id", REQUEST, "--approve-plan", DIGEST, "--plan-only"],
        ["--data", "{}"],
    ],
)
def test_invalid_approval_options_fail_before_network(template_request, extra):
    args, calls, _ = template_request
    result = CliRunner().invoke(cli, [*args, *extra])
    assert result.exit_code == 2
    assert calls == []


def test_plan_only_returns_one_machine_readable_plan(template_request):
    args, calls, _ = template_request
    result = CliRunner().invoke(cli, [*args, "--request-id", REQUEST, "--plan-only"])
    assert result.exit_code == 0 and len(calls) == 1
    assert json.loads(result.stdout)["data"]["request_id"] == REQUEST
    assert not result.stderr


@pytest.mark.parametrize("choice,expected_calls", [("y\n", 2), ("n\n", 1)])
def test_interactive_create_shows_review_before_explicit_choice(
    template_request, monkeypatch, choice, expected_calls
):
    args, calls, _ = template_request
    monkeypatch.setattr(
        "sanka_cli.commands.workflow_templates.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)),
    )
    monkeypatch.setattr(
        "sanka_cli.commands.workflow_templates.resolve_output_format", lambda _: "table"
    )
    result = CliRunner().invoke(cli, args, input=choice)
    assert result.exit_code == 0, result.output
    assert len(calls) == expected_calls
    assert '"invoice_due_days": 45' in result.stderr
    assert "Construct this inactive workflow?" in result.stderr
    json.loads(result.stdout)


def test_existing_raw_workflow_create_uses_unchanged_endpoint(template_request):
    _, calls, _ = template_request
    result = CliRunner().invoke(
        cli, ["--output", "json", "workflows", "create", "--data", '{"title":"Existing"}']
    )
    assert result.exit_code == 0, result.output
    assert calls == [("POST", "/v2/public/workflows", {"title": "Existing"})]
