# SPDX-License-Identifier: Apache-2.0
# mypy: disable-error-code="no-untyped-def"
"""Studio and CLI share native generation without an installation lifecycle."""

import json
from unittest.mock import Mock
from uuid import UUID

import pytest
from click.testing import CliRunner

from sanka_cli.main import cli

WORKSPACE = "88004411-0000-4000-8000-000000000001"
WORKFLOW = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REQUEST = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PREFIX = ["flow", "--workspace", WORKSPACE]


@pytest.fixture
def api(monkeypatch):
    fake = Mock(
        return_value={
            "success": True,
            "data": {
                "workspace_id": WORKSPACE,
                "categories": [{"id": "sales", "title": "Sales"}, {"id": "workspace"}],
                "templates": [
                    {"id": "sales.deal-to-estimate", "category_id": "sales"},
                    {"id": WORKFLOW, "category_id": "workspace"},
                ],
                "workflow_id": WORKFLOW,
            },
        }
    )
    monkeypatch.setattr("sanka_cli.runtime.request_json", fake)
    return fake


def test_flow_help_exposes_category_template_generation_without_name_or_history():
    result = CliRunner().invoke(cli, [*PREFIX, "--help"])
    assert result.exit_code == 0
    assert all(command in result.output for command in ("categories", "templates", "generate"))
    assert "--name" not in result.output
    assert "construct" not in result.output


def test_categories_are_read_only_and_pinned(api):
    result = CliRunner().invoke(cli, [*PREFIX, "categories"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["categories"][0]["id"] == "sales"
    assert api.call_args.args[1:] == ("GET", "/v2/workflows/templates")
    assert api.call_args.kwargs["headers"] == {
        "X-Workspace-Code": WORKSPACE,
        "X-Sanka-Expected-Workspace-ID": WORKSPACE,
    }


def test_select_sales_only_returns_its_templates(api):
    result = CliRunner().invoke(cli, [*PREFIX, "templates", "--category", "sales"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["templates"] == [
        {"id": "sales.deal-to-estimate", "category_id": "sales"}
    ]
    assert api.call_args.args[1] == "GET"


def test_unknown_category_is_rejected_without_writes(api):
    result = CliRunner().invoke(cli, [*PREFIX, "templates", "--category", "invented"])
    assert result.exit_code != 0
    assert api.call_args.args[1] == "GET"


def test_generate_uses_existing_template_api_and_stable_retry_identity(api):
    arguments = [
        *PREFIX,
        "generate",
        "sales.deal-to-estimate",
        "--request-id",
        REQUEST,
        "--language",
        "ja",
    ]
    for _ in range(2):
        result = CliRunner().invoke(cli, arguments)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["data"]["workflow_id"] == WORKFLOW
        assert REQUEST in result.stderr
    assert api.call_args_list[0] == api.call_args_list[1]
    assert api.call_args.args[1:] == ("POST", "/v2/workflows/templates/use")
    assert api.call_args.kwargs["json_body"] == {
        "template_id": "sales.deal-to-estimate",
        "request_id": REQUEST,
        "language": "ja",
    }


def test_generate_without_name_supplies_and_reports_an_identity(api):
    result = CliRunner().invoke(cli, [*PREFIX, "generate", "sales.deal-to-estimate"])
    assert result.exit_code == 0, result.output
    identity = api.call_args.kwargs["json_body"]["request_id"]
    assert str(UUID(identity)) == identity
    assert identity in result.stderr
    assert json.loads(result.stdout)["data"]["request_id"] == identity


@pytest.mark.parametrize(
    "arguments",
    [
        ["generate"],
        ["generate", ""],
        ["generate", "sales.deal-to-estimate", "--request-id", "invalid"],
    ],
)
def test_invalid_input_never_writes(api, arguments):
    result = CliRunner().invoke(cli, [*PREFIX, *arguments])
    assert result.exit_code != 0
    api.assert_not_called()


@pytest.mark.parametrize("operation", ["categories", "templates", "generate"])
def test_rejects_wrong_workspace_responses(api, operation):
    api.return_value = {"data": {"workspace_id": "other", "workflow_id": WORKFLOW}}
    args = [*PREFIX, operation] + (["sales.deal-to-estimate"] if operation == "generate" else [])
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0
    assert "does not match" in result.output


def test_rejects_missing_native_workflow_id(api):
    api.return_value = {"data": {"workspace_id": WORKSPACE}}
    result = CliRunner().invoke(cli, [*PREFIX, "generate", "sales.deal-to-estimate"])
    assert result.exit_code != 0
    assert "did not identify" in result.output
