# SPDX-License-Identifier: Apache-2.0
# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import hashlib
import zipfile
from unittest.mock import Mock

import httpx
import pytest
from click.testing import CliRunner

from sanka_cli.client import APIError, SankaApiClient
from sanka_cli.main import cli

RUN = "00000000-0000-4000-8000-000000000002"
SOURCE = "00000000-0000-4000-8000-000000000003"
ROOT = "/v2/migrate/cloud-runs"


@pytest.fixture
def api(monkeypatch):
    fake = Mock(return_value={"data": {"id": RUN}})
    monkeypatch.setattr("sanka_cli.runtime.request_json", fake)
    return fake


def arguments() -> list[str]:
    return [
        "cloud",
        "run",
        "--workspace",
        "10101010",
        "--source-id",
        SOURCE,
        "--sha256",
        "a" * 64,
        "--max-credits",
        "1000",
        "--idempotency-key",
        "one-reviewed-intent",
    ]


def test_declined_credit_hold_does_not_call_api(api):
    result = CliRunner().invoke(cli, arguments(), input="n\n")
    assert result.exit_code != 0
    assert "1000 credits" in result.output
    assert "100 credits per active worker-minute" in result.output
    api.assert_not_called()


def repair_arguments():
    return [
        "cloud",
        "repair",
        RUN,
        "--workspace",
        "10101010",
        "--candidate-sha256",
        "b" * 64,
        "--path",
        "app/main.py",
        "--target-gate",
        "test",
        "--max-credits",
        "1100",
        "--idempotency-key",
        "one-approved-repair",
    ]


def test_repair_pins_the_saved_failed_run_and_explicit_candidate(api):
    parent = {
        "id": RUN,
        "status": "failed",
        "request": {
            "source_id": SOURCE,
            "source_sha256": "a" * 64,
            "settings_module": "config.settings",
        },
    }
    artifacts = {"artifacts": [{"name": "output.zip", "sha256": "b" * 64}]}
    api.side_effect = [{"data": parent}, {"data": artifacts}, {"data": {"id": "repair-run"}}] * 2
    for _ in range(2):
        result = CliRunner().invoke(cli, [*repair_arguments(), "--yes"])
        assert result.exit_code == 0, result.output
    first, second = api.call_args_list[2], api.call_args_list[5]
    assert first.kwargs == second.kwargs
    assert first.kwargs["headers"] == {
        "X-Workspace-Code": "10101010",
        "Idempotency-Key": "one-approved-repair",
    }
    assert first.kwargs["json_body"]["repair"]["candidate_sha256"] == "b" * 64
    assert first.kwargs["json_body"]["source_id"] == SOURCE


@pytest.mark.parametrize(
    "path", ["tests/test_generated.py", "../app/main.py", "app/__pycache__/x.py"]
)
def test_repair_rejects_unapproved_file_scope_before_reading(api, path):
    args = repair_arguments()
    args[args.index("--path") + 1] = path
    assert CliRunner().invoke(cli, [*args, "--yes"]).exit_code != 0
    api.assert_not_called()


def test_repair_decline_or_candidate_mismatch_never_starts_paid_work(api):
    parent = {
        "id": RUN,
        "status": "failed",
        "request": {"source_id": SOURCE, "source_sha256": "a" * 64},
    }
    for digest, answer in [("b" * 64, "n\n"), ("c" * 64, "y\n")]:
        api.reset_mock()
        api.side_effect = [
            {"data": parent},
            {"data": {"artifacts": [{"name": "output.zip", "sha256": digest}]}},
        ]
        result = CliRunner().invoke(cli, repair_arguments(), input=answer)
        assert result.exit_code != 0
        assert all(call.args[1] == "GET" for call in api.call_args_list)


def test_repeated_intent_preserves_workspace_source_budget_and_key(api):
    for _ in range(2):
        result = CliRunner().invoke(cli, [*arguments(), "--yes"])
        assert result.exit_code == 0, result.output
    first, second = api.call_args_list
    assert first.args[1:] == ("POST", ROOT)
    assert first.kwargs == second.kwargs
    assert first.kwargs["headers"] == {
        "X-Workspace-Code": "10101010",
        "Idempotency-Key": "one-reviewed-intent",
    }
    assert first.kwargs["json_body"]["source_id"] == SOURCE
    assert first.kwargs["json_body"]["max_credits"] == 1000


@pytest.mark.parametrize(
    "option,value",
    [
        ("--max-credits", "0"),
        ("--max-credits", "6001"),
        ("--sha256", "invalid"),
        ("--workspace", "other"),
        ("--idempotency-key", "too\nunsafe"),
    ],
)
def test_invalid_or_unbounded_input_never_calls_api(api, option, value):
    args = arguments()
    args[args.index(option) + 1] = value
    assert CliRunner().invoke(cli, [*args, "--yes"]).exit_code != 0
    api.assert_not_called()


def test_source_is_only_uploaded_after_confirmation(api, tmp_path):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manage.py", "# fixture, never executed\n")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    api.return_value = {"data": {"id": SOURCE, "sha256": sha}}
    args = ["cloud", "upload", "--workspace", "10101010", str(archive)]
    assert CliRunner().invoke(cli, args, input="n\n").exit_code != 0
    api.assert_not_called()
    result = CliRunner().invoke(cli, [*args, "--yes"])
    assert result.exit_code == 0, result.output
    assert api.call_args.kwargs["json_body"]["sha256"] == sha
    assert api.call_args.kwargs["headers"]["X-Workspace-Code"] == "10101010"


def test_download_verifies_digest_and_never_overwrites(api, monkeypatch, tmp_path):
    content = b"verified artifact"
    sha = hashlib.sha256(content).hexdigest()
    api.return_value = {
        "data": {
            "artifacts": [
                {
                    "name": "logs.txt",
                    "size_bytes": len(content),
                    "sha256": sha,
                }
            ]
        }
    }
    binary = Mock(return_value=(b"tampered", {}))
    monkeypatch.setattr("sanka_cli.runtime.request_bytes", binary)
    destination = tmp_path / "logs.txt"
    args = [
        "cloud",
        "download",
        RUN,
        "--workspace",
        "10101010",
        "--artifact",
        "logs.txt",
        "--to",
        str(destination),
    ]
    assert CliRunner().invoke(cli, args).exit_code != 0
    assert not destination.exists()
    binary.return_value = (content, {})
    assert CliRunner().invoke(cli, args).exit_code == 0
    assert destination.read_bytes() == content
    assert binary.call_args.kwargs["headers"] == {"X-Workspace-Code": "10101010"}
    api.reset_mock()
    assert CliRunner().invoke(cli, args).exit_code != 0
    api.assert_not_called()
    assert destination.read_bytes() == content


def test_binary_client_rejects_oversized_download_and_does_not_follow_redirect():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=b"123456")

    client = SankaApiClient(base_url="https://api.example.test", access_token="test-token")
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handle), base_url=client.base_url)
    try:
        with pytest.raises(APIError, match="size limit"):
            client.request_bytes(
                "GET", "/artifact", max_bytes=5, headers={"X-Workspace-Code": "10101010"}
            )
        assert requests[0].headers["X-Workspace-Code"] == "10101010"
        assert not client.client.follow_redirects
    finally:
        client.close()
