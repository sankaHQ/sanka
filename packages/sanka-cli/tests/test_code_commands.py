"""``sanka code`` command behaviour, against a recording fake of the public API."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from sanka_cli.bundle import build_bundle
from sanka_cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class FakeApi:
    """Records requests and replays canned responses keyed by (METHOD, path)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[tuple[str, str], dict[str, Any]] = {}
        self.binary: dict[str, tuple[bytes, dict[str, str]]] = {}

    def request_json(self, _state, method, path, *, params=None, json_body=None):
        self.calls.append({"method": method, "path": path, "params": params, "body": json_body})
        return self.responses.get((method, path), {"data": {}})

    def request_bytes(self, _state, method, path, *, params=None):
        self.calls.append({"method": method, "path": path, "params": params})
        if path not in self.binary:
            raise AssertionError(f"no binary response registered for {path}")
        return self.binary[path]

    def body_for(self, method: str, path: str) -> dict[str, Any]:
        for call in self.calls:
            if call["method"] == method and call["path"] == path:
                return call.get("body") or {}
        raise AssertionError(f"{method} {path} was never called")

    def paths(self) -> list[str]:
        return [f"{c['method']} {c['path']}" for c in self.calls]


@pytest.fixture
def api(monkeypatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr("sanka_cli.runtime.request_json", fake.request_json)
    monkeypatch.setattr("sanka_cli.runtime.request_bytes", fake.request_bytes)
    return fake


def _init(runner: CliRunner, directory: Path, *, slug: str = "enrich", runtime: str = "node"):
    result = runner.invoke(
        cli,
        ["code", "init", "--slug", slug, "--runtime", runtime, "--dir", str(directory)],
    )
    assert result.exit_code == 0, result.output
    return result


class TestInit:
    def test_scaffolds_a_node_function(self, runner: CliRunner, tmp_path: Path) -> None:
        _init(runner, tmp_path)

        manifest = json.loads((tmp_path / "sanka.json").read_text())
        assert manifest["slug"] == "enrich"
        assert manifest["runtime"] == "node22"
        assert manifest["entry"] == "index.js"
        assert (tmp_path / "index.js").exists()
        assert (tmp_path / ".sankaignore").exists()

    def test_scaffolds_a_python_function(self, runner: CliRunner, tmp_path: Path) -> None:
        _init(runner, tmp_path, runtime="python")

        manifest = json.loads((tmp_path / "sanka.json").read_text())
        assert manifest["runtime"] == "python312"
        assert manifest["entry"] == "main.py"
        assert (tmp_path / "main.py").exists()

    def test_refuses_to_clobber_an_existing_manifest(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _init(runner, tmp_path)

        result = runner.invoke(cli, ["code", "init", "--slug", "other", "--dir", str(tmp_path)])

        assert result.exit_code != 0
        assert json.loads((tmp_path / "sanka.json").read_text())["slug"] == "enrich"

    def test_scaffolded_function_bundles_cleanly(self, runner: CliRunner, tmp_path: Path) -> None:
        """init -> push must work with no manual editing in between."""
        _init(runner, tmp_path)

        built = build_bundle(tmp_path)

        assert set(built.paths) == {"sanka.json", "index.js"}


class TestPush:
    def test_uploads_the_bundle_with_its_digest(
        self, runner: CliRunner, api: FakeApi, tmp_path: Path
    ) -> None:
        _init(runner, tmp_path)
        expected = build_bundle(tmp_path)

        result = runner.invoke(cli, ["code", "push", "--dir", str(tmp_path)])

        assert result.exit_code == 0, result.output
        body = api.body_for("POST", "/v2/public/code/functions/enrich/versions")
        assert body["content_sha256"] == expected.sha256
        assert base64.b64decode(body["bundle_base64"]) == expected.raw
        assert body["activate"] is False

    def test_activate_flag_is_forwarded(
        self, runner: CliRunner, api: FakeApi, tmp_path: Path
    ) -> None:
        _init(runner, tmp_path)

        runner.invoke(cli, ["code", "push", "--dir", str(tmp_path), "--activate"])

        body = api.body_for("POST", "/v2/public/code/functions/enrich/versions")
        assert body["activate"] is True

    def test_message_becomes_the_change_summary(
        self, runner: CliRunner, api: FakeApi, tmp_path: Path
    ) -> None:
        _init(runner, tmp_path)

        runner.invoke(cli, ["code", "push", "--dir", str(tmp_path), "-m", "add tier logic"])

        body = api.body_for("POST", "/v2/public/code/functions/enrich/versions")
        assert body["change_summary"] == "add tier logic"

    def test_dry_run_uploads_nothing(self, runner: CliRunner, api: FakeApi, tmp_path: Path) -> None:
        _init(runner, tmp_path)

        result = runner.invoke(
            cli,
            ["--output", "json", "code", "push", "--dir", str(tmp_path), "--dry-run"],
        )

        assert result.exit_code == 0, result.output
        assert api.calls == []
        assert json.loads(result.output)["uploaded"] is False

    def test_missing_manifest_is_a_clean_error(
        self, runner: CliRunner, api: FakeApi, tmp_path: Path
    ) -> None:
        result = runner.invoke(cli, ["code", "push", "--dir", str(tmp_path)])

        assert result.exit_code != 0
        assert "sanka code init" in result.output
        assert api.calls == []


class TestPullAndDiff:
    def _register_bundle(self, api: FakeApi, tmp_path: Path) -> str:
        source = tmp_path / "src"
        source.mkdir()
        (source / "sanka.json").write_text(
            json.dumps({"schemaVersion": 1, "slug": "enrich", "runtime": "node22"})
        )
        (source / "index.js").write_text("export const main = () => ({});")
        built = build_bundle(source)
        api.binary["/v2/public/code/functions/enrich/versions/3/bundle"] = (
            built.raw,
            {"X-Sanka-Content-Sha256": built.sha256},
        )
        api.responses[("GET", "/v2/public/code/functions/enrich")] = {
            "data": {
                "function": {"latest_version": 3},
                "aliases": [{"alias": "live", "version": 3}],
            }
        }
        return built.sha256

    def test_pull_writes_the_deployed_source(
        self, runner: CliRunner, api: FakeApi, tmp_path: Path
    ) -> None:
        self._register_bundle(api, tmp_path)
        out = tmp_path / "out"

        result = runner.invoke(cli, ["code", "pull", "enrich", "--dir", str(out)])

        assert result.exit_code == 0, result.output
        assert (out / "index.js").exists()
        assert (out / "sanka.json").exists()

    def test_pull_defaults_to_the_live_version(
        self, runner: CliRunner, api: FakeApi, tmp_path: Path
    ) -> None:
        self._register_bundle(api, tmp_path)

        runner.invoke(cli, ["code", "pull", "enrich", "--dir", str(tmp_path / "out")])

        assert "GET /v2/public/code/functions/enrich/versions/3/bundle" in api.paths()

    def test_diff_reports_in_sync_for_an_identical_tree(
        self, runner: CliRunner, api: FakeApi, tmp_path: Path
    ) -> None:
        digest = self._register_bundle(api, tmp_path)

        result = runner.invoke(
            cli, ["--output", "json", "code", "diff", "--dir", str(tmp_path / "src")]
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["in_sync"] is True
        assert payload["local_sha256"] == digest

    def test_diff_exits_nonzero_when_the_tree_has_drifted(
        self, runner: CliRunner, api: FakeApi, tmp_path: Path
    ) -> None:
        """Nonzero exit is what makes this usable as a CI drift check."""
        self._register_bundle(api, tmp_path)
        (tmp_path / "src" / "index.js").write_text("export const main = () => ({changed:1});")

        result = runner.invoke(
            cli, ["--output", "json", "code", "diff", "--dir", str(tmp_path / "src")]
        )

        assert result.exit_code == 1
        assert json.loads(result.output)["in_sync"] is False

    def test_pull_rejects_a_bundle_whose_digest_does_not_match(
        self, runner: CliRunner, api: FakeApi, tmp_path: Path
    ) -> None:
        self._register_bundle(api, tmp_path)
        raw, _ = api.binary["/v2/public/code/functions/enrich/versions/3/bundle"]
        api.binary["/v2/public/code/functions/enrich/versions/3/bundle"] = (
            raw,
            {"X-Sanka-Content-Sha256": "0" * 64},
        )

        result = runner.invoke(cli, ["code", "pull", "enrich", "--dir", str(tmp_path / "out")])

        assert result.exit_code != 0
        assert "does not match" in result.output


class TestAliases:
    def test_deploy_moves_the_named_alias(self, runner: CliRunner, api: FakeApi) -> None:
        result = runner.invoke(cli, ["code", "deploy", "enrich", "--version", "4"])

        assert result.exit_code == 0, result.output
        assert api.body_for("PUT", "/v2/public/code/functions/enrich/aliases/live") == {
            "version": 4
        }

    def test_rollback_defaults_to_the_previous_version(
        self, runner: CliRunner, api: FakeApi
    ) -> None:
        api.responses[("GET", "/v2/public/code/functions/enrich/versions")] = {
            "data": {"versions": [{"version": 7}, {"version": 6}]}
        }

        result = runner.invoke(cli, ["code", "rollback", "enrich"])

        assert result.exit_code == 0, result.output
        assert api.body_for("PUT", "/v2/public/code/functions/enrich/aliases/live") == {
            "version": 6
        }

    def test_rollback_without_history_is_a_clean_error(
        self, runner: CliRunner, api: FakeApi
    ) -> None:
        api.responses[("GET", "/v2/public/code/functions/enrich/versions")] = {
            "data": {"versions": [{"version": 1}]}
        }

        result = runner.invoke(cli, ["code", "rollback", "enrich"])

        assert result.exit_code != 0
        assert "no earlier version" in result.output


class TestSourceLock:
    def test_lock_sets_git_ownership(self, runner: CliRunner, api: FakeApi) -> None:
        runner.invoke(cli, ["code", "lock", "enrich"])

        assert api.body_for("PATCH", "/v2/public/code/functions/enrich") == {"source_mode": "git"}

    def test_unlock_requires_confirmation(self, runner: CliRunner, api: FakeApi) -> None:
        """Handing code ownership back to the UI should not be a single keystroke."""
        result = runner.invoke(cli, ["code", "unlock", "enrich"], input="n\n")

        assert result.exit_code != 0
        assert api.calls == []

    def test_unlock_proceeds_when_confirmed(self, runner: CliRunner, api: FakeApi) -> None:
        result = runner.invoke(cli, ["code", "unlock", "enrich"], input="y\n")

        assert result.exit_code == 0, result.output
        assert api.body_for("PATCH", "/v2/public/code/functions/enrich") == {"source_mode": "ui"}


class TestSecrets:
    def test_set_prompts_without_echoing(self, runner: CliRunner, api: FakeApi) -> None:
        result = runner.invoke(
            cli, ["code", "secrets", "set", "enrich", "API_KEY"], input="s3cret\n"
        )

        assert result.exit_code == 0, result.output
        assert "s3cret" not in result.output
        body = api.body_for("PUT", "/v2/public/code/functions/enrich/secrets/API_KEY")
        assert body == {"name": "API_KEY", "value": "s3cret"}

    def test_rm_deletes(self, runner: CliRunner, api: FakeApi) -> None:
        result = runner.invoke(cli, ["code", "secrets", "rm", "enrich", "API_KEY"])

        assert result.exit_code == 0, result.output
        assert "DELETE /v2/public/code/functions/enrich/secrets/API_KEY" in api.paths()


class TestCreate:
    def test_defaults_to_git_ownership(
        self, runner: CliRunner, api: FakeApi, tmp_path: Path
    ) -> None:
        """Creating from a repo means the repo owns the code."""
        _init(runner, tmp_path)

        runner.invoke(cli, ["code", "create", "--dir", str(tmp_path)])

        body = api.body_for("POST", "/v2/public/code/functions")
        assert body["source_mode"] == "git"
        assert body["slug"] == "enrich"
        assert body["runtime"] == "node22"
