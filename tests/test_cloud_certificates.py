# SPDX-License-Identifier: Apache-2.0
# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import base64
import json
from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sanka_cli.certificates import canonical_bytes
from sanka_cli.main import cli

RUN = "00000000-0000-4000-8000-000000000002"
SOURCE = "00000000-0000-4000-8000-000000000003"


@pytest.fixture
def api(monkeypatch):
    fake = Mock()
    monkeypatch.setattr("sanka_cli.runtime.request_json", fake)
    return fake


def arguments(cases):
    return [
        "cloud",
        "certify",
        RUN,
        "--workspace",
        "10101010",
        "--candidate-sha256",
        "b" * 64,
        "--cases",
        str(cases),
        "--max-credits",
        "2100",
        "--idempotency-key",
        "reviewed-certificate",
    ]


def parent():
    return {
        "data": {
            "id": RUN,
            "status": "succeeded",
            "request": {
                "source_id": SOURCE,
                "source_sha256": "a" * 64,
                "settings_module": "config.settings",
            },
        }
    }


def test_certificate_decline_and_unsafe_cases_never_start_paid_work(api, tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"id": "read", "method": "GET", "path": "/api/tasks/"}]))
    api.side_effect = [
        parent(),
        {"data": {"artifacts": [{"name": "output.zip", "sha256": "b" * 64}]}},
    ]
    result = CliRunner().invoke(cli, arguments(cases), input="n\n")
    assert result.exit_code != 0
    assert "2,000" in result.output and "2100 credits" in result.output
    assert all(call.args[1] == "GET" for call in api.call_args_list)
    api.reset_mock()
    cases.write_text(
        json.dumps([{"id": "unsafe", "method": "GET", "path": "https://outside.example/"}])
    )
    result = CliRunner().invoke(cli, [*arguments(cases), "--yes"])
    assert result.exit_code != 0
    api.assert_not_called()


def test_retried_certificate_keeps_candidate_scope_workspace_and_key(api, tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"id": "read", "method": "GET", "path": "/api/tasks/"}]))
    api.side_effect = [
        parent(),
        {"data": {"artifacts": [{"name": "output.zip", "sha256": "b" * 64}]}},
        {"data": {"id": "certification-run"}},
    ] * 2
    for _ in range(2):
        result = CliRunner().invoke(cli, [*arguments(cases), "--yes"])
        assert result.exit_code == 0, result.output
    first, second = api.call_args_list[2], api.call_args_list[5]
    assert first.kwargs == second.kwargs
    assert first.kwargs["headers"] == {
        "X-Workspace-Code": "10101010",
        "Idempotency-Key": "reviewed-certificate",
    }
    assert first.kwargs["json_body"]["verification_profile"] == "independent-http-replay-v1"
    assert first.kwargs["json_body"]["certification"]["scenarios"] == [
        {"id": "read", "method": "GET", "path": "/api/tasks/", "json_body": None}
    ]


def signed_fixture():
    key = Ed25519PrivateKey.generate()
    payload = {
        "schema_version": "developer-certificate-v1",
        "issuer": "https://api-v2.sanka.com",
        "run_id": RUN,
        "workspace_id": SOURCE,
        "evidence": {
            "profile": "independent-http-replay-v1",
            "tested_routes": ["GET /api/tasks/"],
            "untested_routes": ["POST /api/tasks/"],
        },
        "limitations": [
            "Only listed requests were verified",
            "署名の検証だけでは失効を確認できません",
        ],
    }
    signed = {
        "algorithm": "Ed25519",
        "encoding": "sorted-ascii-json-v1",
        "key_id": "test-issuer",
        "payload": payload,
        "signature_base64": base64.b64encode(key.sign(canonical_bytes(payload))).decode(),
    }
    keys = {
        "keys": [
            {
                "algorithm": "Ed25519",
                "key_id": "test-issuer",
                "public_key_base64": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
            }
        ]
    }
    return {"certificate": signed, "revoked_at": None}, keys


def test_offline_verification_requires_explicit_trusted_keys_and_reports_revocation_unknown(
    api, tmp_path
):
    document, keys = signed_fixture()
    path, key_path = tmp_path / "certificate.json", tmp_path / "keys.json"
    path.write_text(json.dumps(document))
    key_path.write_text(json.dumps(keys))
    args = [
        "--output",
        "json",
        "cloud",
        "certificate-verify",
        str(path),
        "--trusted-keys",
        str(key_path),
    ]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "not_checked_offline" in result.output
    assert "POST /api/tasks/" in result.output
    api.assert_not_called()
    document["certificate"]["payload"]["run_id"] = SOURCE
    path.write_text(json.dumps(document))
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0 and "signature is invalid" in result.output


def test_online_verification_checks_current_status_and_rejects_revocation(api, tmp_path):
    document, keys = signed_fixture()
    path = tmp_path / "certificate.json"
    path.write_text(json.dumps(document))
    args = ["cloud", "certificate-verify", str(path), "--workspace", "10101010"]
    api.side_effect = [{"data": keys}, {"data": {**document, "revoked_at": "2026-09-08T00:00:00Z"}}]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0 and "revoked" in result.output
    assert all(
        call.kwargs["headers"] == {"X-Workspace-Code": "10101010"} for call in api.call_args_list
    )
    api.side_effect = [{"data": keys}, {"data": document}]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0 and "active_at_check" in result.output


def test_embedded_self_signed_key_is_not_an_issuer_trust_source(api, tmp_path):
    document, _ = signed_fixture()
    _, unrelated_keys = signed_fixture()
    path, key_path = tmp_path / "certificate.json", tmp_path / "keys.json"
    path.write_text(json.dumps(document))
    key_path.write_text(json.dumps(unrelated_keys))
    result = CliRunner().invoke(
        cli, ["cloud", "certificate-verify", str(path), "--trusted-keys", str(key_path)]
    )
    assert result.exit_code != 0 and "signature is invalid" in result.output
    api.assert_not_called()


def test_certificate_download_preserves_existing_files(api, tmp_path):
    path = tmp_path / "certificate.json"
    path.write_text("existing")
    result = CliRunner().invoke(
        cli, ["cloud", "certificate", RUN, "--workspace", "10101010", "--to", str(path)]
    )
    assert result.exit_code != 0
    api.assert_not_called()
    assert path.read_text() == "existing"
