# SPDX-License-Identifier: Apache-2.0
# mypy: disable-error-code="no-untyped-def"
import io
import zipfile
from unittest.mock import Mock

import click
import httpx
import pytest
from click.testing import CliRunner

from sanka_cli.cloud_source import package_source
from sanka_cli.main import cli


@pytest.mark.parametrize("stage", ["scan", "plan", "apply", "test", "verify"])
def test_cloud_stage_help(stage):
    result = CliRunner().invoke(cli, [stage, "--cloud", "--help"])
    assert result.exit_code == 0, result.output
    assert "--workspace" in result.output
    assert "cloud" in result.output.lower()


RUN = "00000000-0000-4000-8000-000000000002"
SOURCE = "00000000-0000-4000-8000-000000000003"


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = Mock()
    monkeypatch.setattr("sanka_cli.runtime.request_json", fake)
    monkeypatch.setattr(
        "sanka_cli.commands.code_cloud.config_path", lambda: tmp_path / "config.toml"
    )
    monkeypatch.setattr(
        "sanka_cli.runtime.resolve_runtime",
        lambda **kw: {"profile_name": "test", "base_url": "https://test.invalid"},
    )
    return fake


def args(stage):
    return ["--output", "json", stage, "--cloud", "--workspace", "10101010"]


def paid():
    return ["--max-credits", "1000", "--idempotency-key", "migration-intent", "--yes"]


def operation(kind="prepare", status="queued", **extra):
    return {"operation": kind, "run": {"id": RUN, "status": status}, **extra}


def test_upload_retry_preserves_source(api, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("print(1)")
    import hashlib

    digest = hashlib.sha256(package_source(project)).hexdigest()
    api.side_effect = [
        {"source": {"id": SOURCE, "sha256": digest}},
        {"recipes": [{"id": "drf-to-flask", "enabled": True, "recipe_sha256": "b" * 64}]},
        httpx.ReadTimeout("uncertain"),
        operation(),
    ]
    command = [*args("scan"), str(project), "--to", "flask", *paid()]
    assert "Response uncertain" in CliRunner().invoke(cli, command).output
    result = CliRunner().invoke(cli, command)
    assert result.exit_code == 0, result.output
    assert api.call_count == 4
    assert api.call_args_list[2] == api.call_args_list[3]
    body = api.call_args.kwargs["json_body"]
    assert body["source_id"] == SOURCE and body["source_sha256"] == digest
    assert body["recipe"] == "drf-to-flask"
    (project / "app.py").write_text("print(2)")
    assert "different" in CliRunner().invoke(cli, command).output
    assert api.call_count == 4


@pytest.mark.parametrize(
    "stage,kind", [("plan", "prepare"), ("test", "execute"), ("verify", "execute")]
)
def test_review_never_submits(api, stage, kind):
    api.return_value = operation(
        kind,
        "succeeded",
        plan={"plan_sha256": "a" * 64},
        http_verification={"status": "passed", "total": 2, "matched": 2},
    )
    result = CliRunner().invoke(cli, [*args(stage), "--run", RUN])
    assert result.exit_code == 0, result.output
    assert api.call_count == 1 and api.call_args.args[1] == "GET"


@pytest.mark.parametrize(
    "verification,expected",
    [
        (None, 3),
        ({"status": "passed", "total": 0, "matched": 0}, 3),
        ({"status": "passed", "total": 2, "matched": 1}, 3),
        ({"status": "passed", "total": 2, "matched": 2}, 0),
    ],
)
def test_verify_requires_http_evidence(api, verification, expected):
    api.return_value = operation("execute", "succeeded", http_verification=verification)
    result = CliRunner().invoke(cli, [*args("verify"), "--run", RUN])
    assert result.exit_code == expected, result.output


def test_apply_requires_reviewed_hash(api):
    api.return_value = operation("execute")
    command = [*args("apply"), "--run", RUN, *paid()]
    assert CliRunner().invoke(cli, command).exit_code != 0
    api.assert_not_called()
    result = CliRunner().invoke(cli, [*command, "--plan-hash", "a" * 64])
    assert result.exit_code == 0, result.output
    assert api.call_args.args[2].endswith(f"/{RUN}/runs")
    assert api.call_args.kwargs["json_body"]["approved_plan_sha256"] == "a" * 64


def test_github_pins_revision(api):
    api.side_effect = [
        {"github_connected": True},
        {"installations": [{"id": 7}]},
        {"repositories": [], "next_page": 2},
        {"repositories": [{"id": 9, "full_name": "hira29/test", "default_branch": "main"}]},
        {"branches": [{"name": "main", "sha": "c" * 40}]},
        {"source": {"id": SOURCE, "sha256": "a" * 64}},
        {"recipes": [{"id": "drf-to-fastapi", "enabled": True, "recipe_sha256": "b" * 64}]},
        operation(),
    ]
    result = CliRunner().invoke(cli, [*args("scan"), "--github", "hira29/test", *paid()])
    assert result.exit_code == 0, result.output
    assert api.call_args_list[5].kwargs["json_body"] == {
        "installation_id": 7,
        "repository_id": 9,
        "repository_page": 2,
        "branch": "main",
        "revision": "c" * 40,
    }


def test_package_is_stable_and_excludes_secrets(tmp_path):
    (tmp_path / "app.py").write_text("pass")
    (tmp_path / ".env").write_text("secret")
    first = package_source(tmp_path)
    assert first == package_source(tmp_path)
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == ["app.py"]
    (tmp_path / "link").symlink_to(tmp_path / "app.py")
    with pytest.raises(click.ClickException):
        package_source(tmp_path)


@pytest.mark.parametrize("name", ["../outside", ".env.local", "/absolute", "foo/.ssh/key"])
def test_unsafe_zip_rejected(tmp_path, name):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, "secret")
    with pytest.raises(click.ClickException):
        package_source(path)


@pytest.mark.parametrize("status,code", [("failed", 4), ("cancelled", 5)])
def test_replayed_terminal_submission_has_failure_exit(api, status, code):
    api.return_value = operation("execute", status)
    result = CliRunner().invoke(
        cli, [*args("apply"), "--run", RUN, "--plan-hash", "a" * 64, *paid()]
    )
    assert result.exit_code == code, result.output
