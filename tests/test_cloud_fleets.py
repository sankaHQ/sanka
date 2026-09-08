# SPDX-License-Identifier: Apache-2.0
# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import hashlib
import json
import zipfile
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from sanka_cli.main import cli

FLEET = "00000000-0000-4000-8000-000000000001"
SOURCE = "00000000-0000-4000-8000-000000000002"
ROOT = "/v2/migrate/cloud-fleets"


@pytest.fixture
def api(monkeypatch):
    fake = Mock(return_value={"data": {"id": FLEET}})
    monkeypatch.setattr("sanka_cli.runtime.request_json", fake)
    return fake


def items():
    return [
        {
            "key": key,
            "repository": f"team/{key}",
            "revision": str(index + 1) * 40,
            "request": {"source_id": SOURCE, "source_sha256": "a" * 64, "max_credits": 100},
        }
        for index, key in enumerate(["one", "two", "three"])
    ]


def create_args(path):
    return [
        "cloud",
        "fleet",
        "create",
        "--manifest",
        str(path),
        "--workspace",
        "10101010",
        "--max-credits",
        "300",
        "--concurrency",
        "2",
        "--idempotency-key",
        "reviewed-fleet-one",
    ]


def parent():
    return {
        "id": FLEET,
        "status": "partial",
        "completed_at": "2026-09-08T00:00:00Z",
        "items": [
            {
                **item,
                "run": {
                    "id": SOURCE,
                    "status": "failed" if item["key"] == "two" else "succeeded",
                    "request": item["request"],
                },
            }
            for item in items()
        ],
    }


def retry_args():
    return [
        "cloud",
        "fleet",
        "retry",
        FLEET,
        "--workspace",
        "10101010",
        "--item",
        "two",
        "--max-credits",
        "100",
        "--concurrency",
        "1",
        "--idempotency-key",
        "reviewed-fleet-retry",
    ]


def test_reviewed_children_caps_and_idempotency_survive_repeated_create(api, tmp_path):
    manifest = tmp_path / "items.json"
    manifest.write_text(json.dumps(items()))
    declined = CliRunner().invoke(cli, create_args(manifest), input="n\n")
    assert declined.exit_code != 0
    assert "300 credits total" in declined.output
    for item in items():
        assert f"{item['repository']}@{item['revision']}" in declined.output
    assert declined.output.count("cap 100 credits") == 3
    api.assert_not_called()
    for _ in range(2):
        result = CliRunner().invoke(cli, [*create_args(manifest), "--yes"])
        assert result.exit_code == 0, result.output
    first, second = api.call_args_list
    assert first == second
    assert first.args[1:] == ("POST", ROOT)
    assert first.kwargs["headers"] == {
        "X-Workspace-Code": "10101010",
        "Idempotency-Key": "reviewed-fleet-one",
    }
    assert first.kwargs["json_body"] == {"items": items(), "max_credits": 300, "concurrency": 2}


@pytest.mark.parametrize(
    "change", ["duplicate", "revision", "source", "cap", "sha", "boolean", "oversize"]
)
def test_invalid_fleet_never_contacts_api(api, tmp_path, change):
    selected = items()
    if change == "duplicate":
        selected[1] = selected[0]
    elif change == "revision":
        selected[0]["revision"] = "main"
    elif change == "source":
        selected[0]["request"]["source_id"] = 1
    elif change == "cap":
        selected[0]["request"]["max_credits"] = 99
    elif change == "sha":
        selected[0]["request"]["source_sha256"] = "a" * 63
    elif change == "boolean":
        selected[0]["request"]["max_credits"] = True
    manifest = tmp_path / "invalid.json"
    manifest.write_text(json.dumps(selected) if change != "oversize" else "a" * (256 * 1024 + 1))
    result = CliRunner().invoke(cli, [*create_args(manifest), "--yes"])
    assert result.exit_code != 0
    assert "Error:" in result.output
    api.assert_not_called()


def test_selective_retry_reads_original_and_preserves_it_after_an_uncertain_request(api):
    original = parent()
    api.side_effect = [{"data": original}, {"data": {"id": "retry"}}] * 2
    for _ in range(2):
        result = CliRunner().invoke(cli, [*retry_args(), "--yes"])
        assert result.exit_code == 0, result.output
    assert api.call_args_list[1] == api.call_args_list[3]
    assert api.call_args_list[1].args[1:] == ("POST", f"{ROOT}/{FLEET}/retry")
    assert api.call_args_list[1].kwargs["json_body"] == {
        "item_keys": ["two"],
        "max_credits": 100,
        "concurrency": 1,
    }
    assert original == parent()


@pytest.mark.parametrize("change", ["succeeded", "duplicate", "cap", "unsettled", "decline"])
def test_retry_never_charges_unselected_or_unsettled_children(api, change):
    args, original = retry_args(), parent()
    if change == "succeeded":
        args[args.index("--item") + 1] = "one"
    elif change == "duplicate":
        args.extend(["--item", "two"])
    elif change == "cap":
        args[args.index("--max-credits") + 1] = "99"
    elif change == "unsettled":
        original["completed_at"] = None
    api.return_value = {"data": original}
    result = CliRunner().invoke(cli, args if change == "decline" else [*args, "--yes"], input="n\n")
    assert result.exit_code != 0
    assert api.call_count == 1
    assert api.call_args.args[1:] == ("GET", f"{ROOT}/{FLEET}")
    assert api.call_args.kwargs["headers"] == {"X-Workspace-Code": "10101010"}


def test_pinned_source_revision_is_uploaded_and_read_back(api, tmp_path):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manage.py", "# fixture, never executed\n")
    sha, revision = hashlib.sha256(archive.read_bytes()).hexdigest(), "f" * 40
    api.return_value = {"data": {"id": SOURCE, "sha256": sha, "revision": revision}}
    args = [
        "cloud",
        "upload",
        str(archive),
        "--workspace",
        "10101010",
        "--revision",
        revision,
        "--yes",
    ]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert api.call_args.kwargs["json_body"]["revision"] == revision
    api.return_value["data"]["revision"] = "a" * 40
    assert "does not match" in CliRunner().invoke(cli, args).output


@pytest.mark.parametrize(
    "command,method,suffix",
    [("list", "GET", ""), ("status", "GET", f"/{FLEET}"), ("cancel", "POST", f"/{FLEET}/cancel")],
)
def test_fleet_reads_and_cancel_pin_the_workspace(api, command, method, suffix):
    args = ["cloud", "fleet", command, "--workspace", "10101010"]
    if command != "list":
        args.append(FLEET)
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert api.call_args.args[1:] == (method, f"{ROOT}{suffix}")
    assert api.call_args.kwargs["headers"] == {"X-Workspace-Code": "10101010"}
