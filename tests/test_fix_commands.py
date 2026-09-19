# SPDX-License-Identifier: Apache-2.0
# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest
from click.testing import CliRunner

from sanka_cli.main import cli

PARENT = "00000000-0000-4000-8000-000000000002"
CHILD = "00000000-0000-4000-8000-000000000003"


def arguments():
    return [
        "--output",
        "json",
        "fix",
        "--cloud",
        "--workspace",
        "10101010",
        "--run",
        PARENT,
        "--max-credits",
        "1500",
    ]


def eligibility():
    return {
        "parent_run_id": PARENT,
        "workspace_code": "10101010",
        "workspace_name": "Test",
        "eligible": True,
        "candidate_sha256": "b" * 64,
        "source_sha256": "a" * 64,
        "min_credits": 100,
        "max_credits": 6000,
        "verification_scope": "code-ai-http-sqlite-v1",
        "provider": "test-provider",
        "model": "test-model",
        "limits": {"timeout_seconds": 600},
    }


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr("sanka_cli.commands.fix._interactive", lambda: True)
    fake = Mock(side_effect=[eligibility(), {"id": CHILD, "status": "queued", "fix_result": None}])
    monkeypatch.setattr("sanka_cli.runtime.request_json", fake)
    monkeypatch.setattr("sanka_cli.commands.fix.config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(
        "sanka_cli.runtime.resolve_runtime",
        lambda **kw: {"profile_name": "test", "base_url": "https://test.invalid"},
    )
    return fake


@pytest.mark.parametrize("remove", ["--cloud", "--max-credits"])
def test_requires_cloud_and_cap_before_api(api, remove):
    args = arguments()
    index = args.index(remove)
    del args[index : index + (2 if remove == "--max-credits" else 1)]
    assert CliRunner().invoke(cli, args).exit_code != 0
    api.assert_not_called()


def test_automation_requires_key_before_api(api):
    assert CliRunner().invoke(cli, [*arguments(), "--yes"]).exit_code != 0
    api.assert_not_called()


def test_consent_and_pinned_submission(api):
    result = CliRunner().invoke(cli, arguments(), input="y\n")
    assert result.exit_code == 0, result.output
    assert "repository excerpts" in result.stderr
    assert "1500 total credits" in result.stderr
    assert "Test (10101010)" in result.stderr
    assert json.loads(result.stdout)["status"] == "queued"
    call = api.call_args_list[1]
    assert call.args[1:] == ("POST", f"/v2/migrate/cloud-runs/{PARENT}/fix")
    assert call.kwargs["json_body"] == {"candidate_sha256": "b" * 64, "max_credits": 1500}
    assert call.kwargs["headers"]["X-Workspace-Code"] == "10101010"


def test_declined_or_ineligible_never_submits(api):
    result = CliRunner().invoke(cli, arguments(), input="n\n")
    assert result.exit_code != 0
    assert api.call_count == 1
    api.reset_mock(side_effect=True)
    api.return_value = {**eligibility(), "eligible": False, "reason": "setup_required"}
    result = CliRunner().invoke(cli, arguments(), input="y\n")
    assert result.exit_code != 0
    assert api.call_count == 1


def test_ambiguous_response_reuses_durable_key(api):
    api.side_effect = [
        eligibility(),
        httpx.ReadTimeout("uncertain"),
        {"id": CHILD, "status": "queued"},
    ]
    first = CliRunner().invoke(cli, arguments(), input="y\n")
    second = CliRunner().invoke(cli, arguments(), input="y\n")
    assert first.exit_code != 0
    assert "Response uncertain" in first.output
    assert second.exit_code == 0
    assert api.call_args_list[1].kwargs == api.call_args_list[2].kwargs


@pytest.mark.parametrize(
    "status,outcome,code",
    [
        ("succeeded", "checks_passed", 0),
        ("succeeded", None, 3),
        ("succeeded", "needs_review", 3),
        ("failed", "budget_reached", 3),
        ("failed", "service_error", 4),
        ("succeeded", "setup_required", 4),
        ("cancelled", "cancelled", 5),
    ],
)
def test_wait_reports_terminal_outcome(api, status, outcome, code):
    api.side_effect = [
        eligibility(),
        {"id": CHILD, "status": status, "fix_result": {"outcome": outcome}},
    ]
    result = CliRunner().invoke(
        cli, [*arguments(), "--yes", "--idempotency-key", "approved-intent", "--wait"]
    )
    assert result.exit_code == code, result.output
    data = json.loads(result.stdout)
    assert data["fix_result"]["outcome"] == outcome
    assert "receipt" in data["receipt_command"]


def test_interrupt_detaches_without_cancellation(api, monkeypatch):
    monkeypatch.setattr("sanka_cli.commands.fix.time.sleep", Mock(side_effect=KeyboardInterrupt))
    result = CliRunner().invoke(
        cli, [*arguments(), "--yes", "--idempotency-key", "approved-intent", "--wait"]
    )
    assert result.exit_code == 130
    assert json.loads(result.stdout)["client_state"] == "detached"
    assert api.call_count == 2


def test_wait_timeout_is_not_success(api, monkeypatch):
    monkeypatch.setattr("sanka_cli.commands.fix.time.monotonic", Mock(side_effect=[0, 1000]))
    result = CliRunner().invoke(
        cli, [*arguments(), "--yes", "--idempotency-key", "approved-intent", "--wait"]
    )
    assert result.exit_code == 6
    assert json.loads(result.stdout)["client_state"] == "wait_timeout"


def test_auth_error_has_login_guidance_and_does_not_submit(api):
    import click

    api.side_effect = click.ClickException("401 expired")
    result = CliRunner().invoke(cli, arguments(), input="y\n")
    assert result.exit_code != 0
    assert "sanka login" in result.output
    assert api.call_count == 1


def test_login_prompts_without_echoing_token(monkeypatch):
    monkeypatch.setattr("sanka_cli.runtime.verify_access_token", Mock(return_value=({}, None)))
    monkeypatch.setattr("sanka_cli.runtime.upsert_profile", Mock())
    store = Mock()
    monkeypatch.setattr("sanka_cli.runtime.store_tokens", store)
    result = CliRunner().invoke(cli, ["login"], input="hidden-test-token\n")
    assert result.exit_code == 0, result.output
    assert "hidden-test-token" not in result.output
    assert store.call_args.kwargs["access_token"] == "hidden-test-token"


def test_headless_requires_explicit_consent(api, monkeypatch):
    monkeypatch.setattr("sanka_cli.commands.fix._interactive", lambda: False)
    result = CliRunner().invoke(cli, arguments(), input="y\n")
    assert result.exit_code == 2
    api.assert_not_called()


def test_generated_key_can_recover_explicitly_without_preflight(api):
    api.side_effect = [eligibility(), httpx.ReadTimeout("uncertain")]
    first = CliRunner().invoke(cli, arguments(), input="y\n")
    assert first.exit_code == 1
    key = api.call_args_list[1].kwargs["headers"]["Idempotency-Key"]
    api.reset_mock(side_effect=True)
    api.return_value = {"id": CHILD, "status": "running"}
    second = CliRunner().invoke(cli, [*arguments(), "--yes", "--idempotency-key", key])
    assert second.exit_code == 0, second.output
    assert api.call_count == 1
    assert api.call_args.args[1] == "POST"


@pytest.mark.parametrize("contents", ["broken", "[]", "null", "{}", '{"request": null}'])
def test_malformed_intent_fails_closed(api, tmp_path, contents):
    from sanka_cli.commands.fix import _intent_path

    binding = {
        "profile": "test",
        "base_url": "https://test.invalid",
        "workspace": "10101010",
        "parent_run_id": PARENT,
        "max_credits": 1500,
    }
    path = _intent_path(binding, None)
    path.parent.mkdir()
    path.write_text(contents)
    result = CliRunner().invoke(cli, arguments(), input="y\n")
    assert result.exit_code == 1
    assert "do not submit a new intent" in result.output
    api.assert_not_called()


def test_wait_shows_only_status_changes_on_stderr(api, monkeypatch):
    monkeypatch.setattr("sanka_cli.commands.fix.time.sleep", lambda seconds: None)
    api.side_effect = [
        eligibility(),
        {"id": CHILD, "status": "queued"},
        {"id": CHILD, "status": "running"},
        {"id": CHILD, "status": "running"},
        {"id": CHILD, "status": "succeeded", "fix_result": {"outcome": "checks_passed"}},
    ]
    result = CliRunner().invoke(
        cli, [*arguments(), "--yes", "--idempotency-key", "approved-intent", "--wait"]
    )
    assert result.exit_code == 0
    assert result.stderr.count("Fix status: running") == 1
    assert "Fix status: succeeded" in result.stderr
    assert json.loads(result.stdout)["fix_result"]["outcome"] == "checks_passed"
