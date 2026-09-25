# SPDX-License-Identifier: AGPL-3.0-only
# mypy: disable-error-code="no-untyped-def,type-arg"
from __future__ import annotations

import json
from typing import ClassVar

import pytest
from click.testing import CliRunner

from sanka_cli import config as cli_config
from sanka_cli.main import cli


def test_migration_passthrough_delegates_in_process(runner: CliRunner, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_main(argv, *, api_base=None, product="auto"):
        captured["argv"] = argv
        captured["product"] = product
        return 0

    monkeypatch.setattr("sanka.cli.main", _fake_main)
    result = runner.invoke(cli, ["scan", ".", "--json"])
    assert result.exit_code == 0, result.output
    assert captured["argv"] == ["scan", ".", "--json"]
    assert captured["product"] == "auto"


def test_migration_passthrough_forwards_help(runner: CliRunner, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_main(argv, *, api_base=None, product="auto"):
        captured["argv"] = argv
        captured["product"] = product
        return 0

    monkeypatch.setattr("sanka.cli.main", _fake_main)
    result = runner.invoke(cli, ["plan", "--help"])
    assert result.exit_code == 0, result.output
    assert captured["argv"] == ["plan", "--help"]
    assert captured["product"] == "code"


def test_app_local_routes_to_data_parser(runner: CliRunner, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_main(argv, *, api_base=None, product="auto"):
        captured["argv"] = argv
        captured["product"] = product
        return 0

    monkeypatch.setattr("sanka.cli.main", _fake_main)
    result = runner.invoke(cli, ["app", "plan", "--file", "custom.yaml"])
    assert result.exit_code == 0, result.output
    assert captured == {"argv": ["plan", "--file", "custom.yaml"], "product": "app"}


def test_app_help_keeps_code_options_separate(runner: CliRunner, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sanka.yaml").write_text("source: example\n", encoding="utf-8")

    app_help = runner.invoke(cli, ["app", "plan", "--help"])
    assert app_help.exit_code == 0, app_help.output
    assert "usage: sanka app plan" in app_help.output
    assert "--to" not in app_help.output

    code_help = runner.invoke(cli, ["plan", "--help"])
    assert code_help.exit_code == 0, code_help.output
    assert "--to" in code_help.output

    selected: list[str] = []

    def _fake_main(argv, *, api_base=None, product="auto"):
        selected.append(product)
        return 0

    monkeypatch.setattr("sanka.cli.main", _fake_main)
    code_plan = runner.invoke(cli, ["plan", "--json"])
    assert code_plan.exit_code == 0, code_plan.output
    assert selected == ["code"]


def test_legacy_file_alias_warns_and_routes_to_app(runner: CliRunner, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_main(argv, *, api_base=None, product="auto"):
        captured["product"] = product
        return 0

    monkeypatch.setattr("sanka.cli.main", _fake_main)
    result = runner.invoke(cli, ["plan", "--file", "custom.yaml"])
    assert result.exit_code == 0, result.output
    assert "Deprecated: use `sanka app plan`" in result.output
    assert captured["product"] == "app"


def test_empty_app_selector_never_runs_local(runner: CliRunner, monkeypatch) -> None:
    monkeypatch.setattr(
        "sanka.cli.main",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("local run")),
    )
    result = runner.invoke(cli, ["app", "apply", "--program", ""])
    assert result.exit_code == 2
    assert "require non-empty IDs" in result.output


@pytest.mark.parametrize(
    ("command", "tail"),
    [
        ("scan", ["--", "--help"]),
        ("verify", ["--", "--program", "literal"]),
    ],
)
def test_local_passthrough_preserves_double_dash(
    command: str, tail: list[str], runner: CliRunner, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def _fake_main(argv, *, api_base=None, product="auto"):
        captured["argv"] = argv
        captured["product"] = product
        return 0

    monkeypatch.setattr("sanka.cli.main", _fake_main)
    result = runner.invoke(cli, [command, *tail])

    assert result.exit_code == 0, result.output
    assert captured["argv"] == [command, *tail]


@pytest.mark.parametrize(
    "command",
    [
        "scan",
        "plan",
        "validate",
        "apply",
        "test",
        "verify",
        "status",
        "migrate",
        "connect",
        "extension",
        "assess",
    ],
)
def test_local_help_does_not_load_credentials(command: str, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.setattr(
        "sanka_cli.config.get_tokens",
        lambda *_: (_ for _ in ()).throw(AssertionError("credentials loaded")),
    )
    result = runner.invoke(cli, [command, "--help"])
    assert result.exit_code == 0, result.output


def test_cloud_plan_loads_credentials(monkeypatch, runner: CliRunner) -> None:
    monkeypatch.delenv("SANKA_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SANKA_BASE_URL", raising=False)
    monkeypatch.setattr(
        "sanka_cli.config.get_tokens",
        lambda *_: {"access_token": None, "refresh_token": None},
    )
    result = runner.invoke(cli, ["app", "plan", "--program", "program-1"])
    assert result.exit_code == 1
    assert "No access token configured" in result.output


def test_migration_commands_do_not_collide_with_api_commands() -> None:
    from sanka_cli.commands.migrate import MIGRATION_COMMANDS

    registered = set(cli.commands)
    assert set(MIGRATION_COMMANDS) <= registered
    api_commands = registered - set(MIGRATION_COMMANDS)
    assert set(MIGRATION_COMMANDS).isdisjoint(api_commands)


def test_cloud_plan_creates_first_program_migration_and_waits(
    runner: CliRunner, monkeypatch
) -> None:
    calls: list[tuple[str, str, dict]] = []
    responses = iter(
        [
            {"data": {"id": "program-1", "migration_ids": []}},
            {"data": {"id": "migration-1", "status": "created"}},
            {"data": {"id": "migration-1", "status": "planning"}},
            {
                "data": {
                    "id": "migration-1",
                    "status": "requires_approval",
                }
            },
            {
                "data": {
                    "migration_id": "migration-1",
                    "plan_hash": "sha256:plan-1",
                    "risk_level": "low",
                    "summary": {"records_estimated": 12},
                }
            },
        ]
    )

    def fake_request(_state, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return next(responses)

    monkeypatch.setattr("sanka_cli.runtime.request_json", fake_request)
    result = runner.invoke(
        cli,
        ["--output", "json", "app", "plan", "--program", "program-1"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["plan"]["plan_hash"] == "sha256:plan-1"
    assert payload["next"] == ("sanka app apply --program program-1 --migration migration-1")
    assert calls == [
        ("GET", "/v2/migrate/programs/program-1", {}),
        (
            "POST",
            "/v2/migrate/programs/program-1/migrations",
            {"json_body": {}},
        ),
        (
            "POST",
            "/v2/migrate/migrations/migration-1/plan",
            {"json_body": {"sample_size": 10, "force": False}},
        ),
        ("GET", "/v2/migrate/migrations/migration-1", {}),
        ("GET", "/v2/migrate/migrations/migration-1/plan", {}),
    ]


def test_cloud_apply_binds_current_plan_hash(runner: CliRunner, monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    responses = iter(
        [
            {"data": {"id": "program-1", "migration_ids": ["migration-1"]}},
            {
                "data": {
                    "id": "migration-1",
                    "status": "requires_approval",
                }
            },
            {
                "data": {
                    "migration_id": "migration-1",
                    "plan_hash": "sha256:plan-1",
                }
            },
            {"data": {"id": "migration-1", "status": "applying"}},
        ]
    )

    def fake_request(_state, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return next(responses)

    monkeypatch.setattr("sanka_cli.runtime.request_json", fake_request)
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "app",
            "apply",
            "--program",
            "program-1",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["migration"]["status"] == "applying"
    method, path, kwargs = calls[-1]
    assert (method, path) == (
        "POST",
        "/v2/migrate/migrations/migration-1/apply",
    )
    assert kwargs["json_body"] == {"plan_hash": "sha256:plan-1"}
    assert kwargs["headers"]["Idempotency-Key"].startswith("sanka-cli-")


def test_cloud_program_with_multiple_migrations_requires_selector(
    runner: CliRunner, monkeypatch
) -> None:
    monkeypatch.setattr(
        "sanka_cli.runtime.request_json",
        lambda *_args, **_kwargs: {
            "data": {
                "id": "program-1",
                "migration_ids": ["migration-1", "migration-2"],
            }
        },
    )

    result = runner.invoke(cli, ["app", "apply", "--program", "program-1", "--yes"])

    assert result.exit_code == 1
    assert "multiple migrations" in result.output
    assert "--migration" in result.output


def test_cloud_status_program_reads_all_linked_migrations(runner: CliRunner, monkeypatch) -> None:
    def fake_request(_state, _method, path, **_kwargs):
        if path.endswith("/programs/program-1"):
            return {
                "data": {
                    "id": "program-1",
                    "migration_ids": ["migration-1", "migration-2"],
                }
            }
        migration_id = path.rsplit("/", 1)[-1]
        return {"data": {"id": migration_id, "status": "applied"}}

    monkeypatch.setattr("sanka_cli.runtime.request_json", fake_request)
    result = runner.invoke(
        cli,
        ["--output", "json", "app", "status", "--program", "program-1"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [item["id"] for item in payload["migrations"]] == [
        "migration-1",
        "migration-2",
    ]


def test_top_level_auth_aliases_are_registered() -> None:
    assert {"login", "logout", "whoami"} <= set(cli.commands)


def test_version_matches_pyproject() -> None:
    import tomllib
    from pathlib import Path

    from sanka_cli import __version__

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as handle:
        project_version = tomllib.load(handle)["project"]["version"]
    assert __version__ == project_version


class _FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_keyring(monkeypatch) -> _FakeKeyring:
    from sanka_cli import config as cli_config

    keyring = _FakeKeyring()
    monkeypatch.setattr(cli_config, "keyring", keyring)
    return keyring


def test_auth_login_status_and_logout(
    runner: CliRunner,
    fake_keyring: _FakeKeyring,
    monkeypatch,
    tmp_path,
) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    verified: dict[str, str] = {}

    def _fake_verify(*, base_url: str, access_token: str):
        verified["base_url"] = base_url
        verified["access_token"] = access_token
        return (
            {
                "workspace_name": "Acme",
                "workspace_code": "12345678",
                "email": "dev@example.com",
                "token_name": "CLI",
                "permission_level": "admin",
            },
            None,
        )

    monkeypatch.setattr("sanka_cli.runtime.verify_access_token", _fake_verify)
    login_result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "auth",
            "login",
            "--access-token",
            "access-1",
            "--refresh-token",
            "refresh-1",
            "--base-url",
            "https://cli.example.com",
        ],
        env=env,
    )
    assert login_result.exit_code == 0, login_result.output
    assert verified == {
        "base_url": "https://cli.example.com",
        "access_token": "access-1",
    }
    login_payload = json.loads(login_result.output)
    assert login_payload["message"] == "logged_in"
    assert login_payload["workspace"] == "Acme (12345678)"
    assert login_payload["user"] == "dev@example.com"
    assert login_payload["token"] == "CLI"
    assert login_payload["permission_level"] == "admin"
    assert "warning" not in login_payload
    assert fake_keyring.values[("sanka-cli", "default:access_token")] == "access-1"
    assert fake_keyring.values[("sanka-cli", "default:refresh_token")] == "refresh-1"

    monkeypatch.setattr(
        "sanka_cli.runtime.request_json",
        lambda *_args, **_kwargs: {
            "data": {
                "auth_mode": "developer_api_token",
                "token_name": "CLI",
            }
        },
    )
    status_result = runner.invoke(
        cli,
        ["--output", "json", "auth", "status"],
        env=env,
    )
    assert status_result.exit_code == 0, status_result.output
    status_payload = json.loads(status_result.output)
    assert status_payload["auth_mode"] == "developer_api_token"
    assert status_payload["profile"] == "default"
    assert status_payload["base_url"] == "https://cli.example.com"

    logout_result = runner.invoke(
        cli,
        ["--output", "json", "auth", "logout"],
        env=env,
    )
    assert logout_result.exit_code == 0, logout_result.output
    assert ("sanka-cli", "default:access_token") not in fake_keyring.values
    assert ("sanka-cli", "default:refresh_token") not in fake_keyring.values


def test_resolve_runtime_prefers_env_access_token_without_keyring(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("SANKA_ACCESS_TOKEN", "env-access")

    def _unexpected_get_tokens(profile_name: str) -> dict[str, str | None]:
        raise AssertionError(f"get_tokens should not run for {profile_name}")

    monkeypatch.setattr(cli_config, "get_tokens", _unexpected_get_tokens)
    resolved = cli_config.resolve_runtime()
    assert resolved["access_token"] == "env-access"
    assert resolved["refresh_token"] is None
    assert resolved["token_source"] == "env"


def test_auth_status_surfaces_keychain_error_cleanly(
    runner: CliRunner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "sanka_cli.runtime.resolve_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(
            cli_config.CredentialStoreError("Keychain access is blocked")
        ),
    )

    result = runner.invoke(cli, ["auth", "status"])
    assert result.exit_code == 1
    assert "Keychain access is blocked" in result.output


def test_auth_login_rejected_token_saves_nothing(
    runner: CliRunner,
    fake_keyring: _FakeKeyring,
    monkeypatch,
    tmp_path,
) -> None:
    from sanka_cli import runtime as cli_runtime

    def _fake_verify(*, base_url: str, access_token: str):
        del access_token
        raise cli_runtime.TokenVerificationError(
            f"{base_url} rejected this access token (HTTP 401: Unauthorized).",
            status_code=401,
        )

    monkeypatch.setattr("sanka_cli.runtime.verify_access_token", _fake_verify)
    result = runner.invoke(
        cli,
        ["--output", "json", "auth", "login", "--access-token", "bad-token"],
        env={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    assert result.exit_code == 1
    assert "rejected this access token" in result.output
    assert "Nothing was saved" in result.output
    assert fake_keyring.values == {}
    assert not (tmp_path / "sanka" / "config.toml").exists()


def test_auth_login_unreachable_api_saves_with_warning(
    runner: CliRunner,
    fake_keyring: _FakeKeyring,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "sanka_cli.runtime.verify_access_token",
        lambda **_kwargs: (
            None,
            "token saved without verification: could not reach "
            "https://api.sanka.com (ConnectError)",
        ),
    )
    result = runner.invoke(
        cli,
        ["--output", "json", "auth", "login", "--access-token", "access-1"],
        env={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["message"] == "saved"
    assert "could not reach" in payload["warning"]
    assert fake_keyring.values[("sanka-cli", "default:access_token")] == "access-1"


def test_profiles_list_and_use(
    runner: CliRunner,
    fake_keyring: _FakeKeyring,
    monkeypatch,
    tmp_path,
) -> None:
    _ = fake_keyring
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    monkeypatch.setattr(
        "sanka_cli.runtime.verify_access_token",
        lambda **_kwargs: ({"workspace_name": "Acme"}, None),
    )

    result_default = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "auth",
            "login",
            "--access-token",
            "access-default",
        ],
        env=env,
    )
    assert result_default.exit_code == 0, result_default.output

    result_prod = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "auth",
            "login",
            "--profile",
            "prod",
            "--access-token",
            "access-prod",
            "--refresh-token",
            "refresh-prod",
            "--base-url",
            "https://prod.example.com",
        ],
        env=env,
    )
    assert result_prod.exit_code == 0, result_prod.output

    list_result = runner.invoke(
        cli,
        ["--output", "json", "profiles", "list"],
        env=env,
    )
    assert list_result.exit_code == 0, list_result.output
    profiles_payload = json.loads(list_result.output)["data"]
    assert {row["name"] for row in profiles_payload} == {"default", "prod"}

    use_result = runner.invoke(
        cli,
        ["--output", "json", "profiles", "use", "prod"],
        env=env,
    )
    assert use_result.exit_code == 0, use_result.output
    use_payload = json.loads(use_result.output)
    assert use_payload["active_profile"] == "prod"


@pytest.mark.parametrize(
    (
        "resource",
        "command_args",
        "expected_method",
        "expected_path",
        "expected_params",
        "expected_json",
    ),
    [
        (
            "companies",
            ["list", "--page", "2", "--limit", "10"],
            "GET",
            "/v2/public/companies",
            {"page": 2, "limit": 10},
            None,
        ),
        (
            "companies",
            ["get", "company-1", "--external-id", "COMP-1"],
            "GET",
            "/v2/public/companies/company-1",
            {"external_id": "COMP-1"},
            None,
        ),
        (
            "companies",
            ["create", "--data", '{"name":"Acme"}'],
            "POST",
            "/v2/public/companies",
            None,
            {"name": "Acme"},
        ),
        (
            "companies",
            [
                "update",
                "company-1",
                "--data",
                '{"name":"Acme 2"}',
                "--external-id",
                "COMP-1",
            ],
            "PUT",
            "/v2/public/companies/company-1",
            {"external_id": "COMP-1"},
            {"name": "Acme 2"},
        ),
        (
            "companies",
            ["delete", "company-1", "--external-id", "COMP-1"],
            "DELETE",
            "/v2/public/companies/company-1",
            {"external_id": "COMP-1"},
            None,
        ),
        (
            "contacts",
            ["list"],
            "GET",
            "/v2/public/contacts",
            {"page": 1, "limit": 50},
            None,
        ),
        (
            "contacts",
            ["get", "contact-1"],
            "GET",
            "/v2/public/contacts/contact-1",
            None,
            None,
        ),
        (
            "contacts",
            ["create", "--data", '{"first_name":"Ada"}'],
            "POST",
            "/v2/public/contacts",
            None,
            {"first_name": "Ada"},
        ),
        (
            "contacts",
            ["update", "contact-1", "--data", '{"first_name":"Ada 2"}'],
            "PUT",
            "/v2/public/contacts/contact-1",
            None,
            {"first_name": "Ada 2"},
        ),
        (
            "contacts",
            ["delete", "contact-1"],
            "DELETE",
            "/v2/public/contacts/contact-1",
            None,
            None,
        ),
        ("deals", ["list"], "GET", "/v2/public/deals", {"page": 1, "limit": 50}, None),
        ("deals", ["get", "deal-1"], "GET", "/v2/public/deals/deal-1", None, None),
        (
            "deals",
            ["create", "--data", '{"title":"Deal"}'],
            "POST",
            "/v2/public/deals",
            None,
            {"title": "Deal"},
        ),
        (
            "deals",
            ["update", "deal-1", "--data", '{"title":"Deal 2"}'],
            "PUT",
            "/v2/public/deals/deal-1",
            None,
            {"title": "Deal 2"},
        ),
        (
            "deals",
            ["delete", "deal-1"],
            "DELETE",
            "/v2/public/deals/deal-1",
            None,
            None,
        ),
        (
            "tickets",
            ["list"],
            "GET",
            "/v2/public/tickets",
            {"page": 1, "limit": 50},
            None,
        ),
        (
            "tickets",
            ["get", "ticket-1"],
            "GET",
            "/v2/public/tickets/ticket-1",
            None,
            None,
        ),
        (
            "tickets",
            ["create", "--data", '{"external_id":"T-1","title":"Ticket"}'],
            "POST",
            "/v2/public/tickets",
            None,
            {"external_id": "T-1", "title": "Ticket"},
        ),
        (
            "tickets",
            ["update", "ticket-1", "--data", '{"title":"Ticket 2"}'],
            "PUT",
            "/v2/public/tickets/ticket-1",
            None,
            {"title": "Ticket 2"},
        ),
        (
            "tickets",
            ["delete", "ticket-1"],
            "DELETE",
            "/v2/public/tickets/ticket-1",
            None,
            None,
        ),
    ],
)
def test_resource_commands_route_expected_requests(
    runner: CliRunner,
    monkeypatch,
    resource: str,
    command_args: list[str],
    expected_method: str,
    expected_path: str,
    expected_params: dict | None,
    expected_json: dict | None,
) -> None:
    captured: dict[str, object] = {}

    def _fake_request(_state, method, path, *, params=None, json_body=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        captured["json_body"] = json_body
        return {"ok": True}

    monkeypatch.setattr("sanka_cli.runtime.request_json", _fake_request)
    result = runner.invoke(
        cli,
        ["--output", "json", resource, *command_args],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "method": expected_method,
        "path": expected_path,
        "params": expected_params,
        "json_body": expected_json,
    }


def test_workflow_commands_route_expected_requests_and_wait(
    runner: CliRunner,
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []
    responses = [
        {"data": {"run_id": "run-1", "status": "running"}},
        {"data": {"run_id": "run-1", "status": "running"}},
        {"data": {"run_id": "run-1", "status": "success"}},
    ]

    def _fake_request(_state, method, path, *, params=None, json_body=None):
        del params, json_body
        calls.append((method, path))
        if path == "/v2/public/workflows":
            return {"ok": True}
        return responses.pop(0)

    monkeypatch.setattr("sanka_cli.runtime.request_json", _fake_request)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    create_result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "workflows",
            "create",
            "--data",
            '{"external_id":"WF-1","name":"Workflow"}',
        ],
    )
    assert create_result.exit_code == 0, create_result.output

    update_result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "workflows",
            "update",
            "--data",
            '{"external_id":"WF-1","name":"Workflow 2"}',
        ],
    )
    assert update_result.exit_code == 0, update_result.output

    run_result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "workflows",
            "run",
            "WF-1",
            "--wait",
            "--poll-interval",
            "0.01",
            "--timeout",
            "1",
        ],
    )
    assert run_result.exit_code == 0, run_result.output
    assert calls == [
        ("POST", "/v2/public/workflows"),
        ("POST", "/v2/public/workflows"),
        ("POST", "/v2/public/workflows/WF-1/run"),
        ("GET", "/v2/public/workflow-runs/run-1"),
        ("GET", "/v2/public/workflow-runs/run-1"),
    ]


@pytest.mark.parametrize(
    ("command_args", "expected_body"),
    [
        (
            ["ai", "score", "company", "company-1"],
            {"object_type": "company", "record_id": "company-1"},
        ),
        (
            ["ai", "score", "deal", "deal-1", "--score-model-id", "model-1"],
            {"object_type": "deal", "record_id": "deal-1", "score_model_id": "model-1"},
        ),
    ],
)
def test_ai_score_commands_build_payloads(
    runner: CliRunner,
    monkeypatch,
    command_args: list[str],
    expected_body: dict,
) -> None:
    captured: dict[str, object] = {}

    def _fake_request(_state, method, path, *, params=None, json_body=None):
        del params
        captured["method"] = method
        captured["path"] = path
        captured["json_body"] = json_body
        return {"ok": True}

    monkeypatch.setattr("sanka_cli.runtime.request_json", _fake_request)
    result = runner.invoke(cli, ["--output", "json", *command_args])

    assert result.exit_code == 0, result.output
    assert captured == {
        "method": "POST",
        "path": "/v2/score",
        "json_body": expected_body,
    }


def test_ai_enrich_company_builds_record_and_seed_payloads(
    runner: CliRunner,
    monkeypatch,
    tmp_path,
) -> None:
    custom_field_map_path = tmp_path / "custom-field-map.json"
    custom_field_map_path.write_text('{"segment":"field_1"}', encoding="utf-8")
    captured_bodies: list[dict] = []

    def _fake_request(_state, method, path, *, params=None, json_body=None):
        del params
        assert method == "POST"
        assert path == "/v2/enrich"
        captured_bodies.append(json_body or {})
        return {"ok": True}

    monkeypatch.setattr("sanka_cli.runtime.request_json", _fake_request)

    record_result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "ai",
            "enrich",
            "company",
            "company-1",
            "--force-refresh",
            "--custom-field-map",
            f"@{custom_field_map_path}",
        ],
    )
    assert record_result.exit_code == 0, record_result.output

    seed_result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "ai",
            "enrich",
            "company",
            "--seed-name",
            "Acme",
            "--seed-url",
            "https://acme.example.com",
            "--seed-external-id",
            "seed-1",
            "--dry-run",
        ],
    )
    assert seed_result.exit_code == 0, seed_result.output

    assert captured_bodies == [
        {
            "object_type": "company",
            "dry_run": False,
            "force_refresh": True,
            "record_id": "company-1",
            "custom_field_map": {"segment": "field_1"},
        },
        {
            "object_type": "company",
            "dry_run": True,
            "force_refresh": False,
            "seed": {
                "name": "Acme",
                "url": "https://acme.example.com",
                "external_id": "seed-1",
            },
        },
    ]


def test_api_error_parses_v2_error_envelope() -> None:
    import httpx

    from sanka_cli.client import APIError, SankaApiClient

    client = SankaApiClient(base_url="https://cli.example.com", access_token="t")
    response = httpx.Response(
        401,
        json={
            "success": False,
            "error": {
                "code": "INVALID_AUTHENTICATION",
                "message": "Invalid authentication credentials",
                "details": None,
            },
            "meta": {"ctx_id": "ctx_123"},
        },
    )
    with pytest.raises(APIError) as excinfo:
        client._raise_for_response(response)
    client.close()
    assert excinfo.value.message == "Invalid authentication credentials"
    assert excinfo.value.ctx_id == "ctx_123"
    assert excinfo.value.display_message() == (
        "Invalid authentication credentials (ctx_id=ctx_123)"
    )


class _FakeVerifyClient:
    outcome: object = None
    instances: ClassVar[list[_FakeVerifyClient]] = []

    def __init__(self, *, base_url: str, access_token: str) -> None:
        self.base_url = base_url
        self.access_token = access_token
        self.closed = False
        type(self).instances.append(self)

    def request_json(self, method: str, path: str, **_kwargs):
        assert (method, path) == ("GET", "/v2/public/auth/whoami")
        outcome = type(self).outcome
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_verify_client(monkeypatch) -> type[_FakeVerifyClient]:
    from sanka_cli import runtime as cli_runtime

    _FakeVerifyClient.instances = []
    monkeypatch.setattr(cli_runtime, "SankaApiClient", _FakeVerifyClient)
    return _FakeVerifyClient


def test_verify_access_token_returns_identity(fake_verify_client) -> None:
    from sanka_cli import runtime as cli_runtime

    fake_verify_client.outcome = {"data": {"workspace_name": "Acme"}}
    identity, warning = cli_runtime.verify_access_token(
        base_url="https://cli.example.com",
        access_token="access-1",
    )
    assert identity == {"workspace_name": "Acme"}
    assert warning is None
    assert fake_verify_client.instances[0].closed


def test_verify_access_token_maps_auth_rejection(fake_verify_client) -> None:
    from sanka_cli import runtime as cli_runtime
    from sanka_cli.client import APIError

    fake_verify_client.outcome = APIError(status_code=401, message="Unauthorized")
    with pytest.raises(cli_runtime.TokenVerificationError) as excinfo:
        cli_runtime.verify_access_token(
            base_url="https://cli.example.com",
            access_token="bad-token",
        )
    assert excinfo.value.status_code == 401
    assert "rejected this access token" in str(excinfo.value)
    assert fake_verify_client.instances[0].closed


def test_verify_access_token_warns_on_server_error(fake_verify_client) -> None:
    from sanka_cli import runtime as cli_runtime
    from sanka_cli.client import APIError

    fake_verify_client.outcome = APIError(status_code=503, message="Unavailable")
    identity, warning = cli_runtime.verify_access_token(
        base_url="https://cli.example.com",
        access_token="access-1",
    )
    assert identity is None
    assert warning is not None
    assert "HTTP 503" in warning


def test_verify_access_token_warns_when_unreachable(fake_verify_client) -> None:
    import httpx

    from sanka_cli import runtime as cli_runtime

    fake_verify_client.outcome = httpx.ConnectError("connection refused")
    identity, warning = cli_runtime.verify_access_token(
        base_url="https://cli.example.com",
        access_token="access-1",
    )
    assert identity is None
    assert warning is not None
    assert "could not reach https://cli.example.com" in warning
    assert "ConnectError" in warning


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("https://staging.example", "https://staging.example/v2/migrate/assessments"),
        (
            "https://staging.example/prefix/",
            "https://staging.example/prefix/v2/migrate/assessments",
        ),
        (
            "https://staging.example/v2/migrate/",
            "https://staging.example/v2/migrate/assessments",
        ),
        (None, "https://legacy.example/migration-service/assessments"),
    ],
)
def test_assessment_requests_use_global_api_override_before_legacy_environment(
    runner: CliRunner,
    monkeypatch,
    override,
    expected,
) -> None:
    import io
    import json

    import sanka.cli as local_cli
    from sanka.cli._research import SankaMigrateApiClient

    requests = []

    def opener(request, *, timeout):
        requests.append(request)
        return io.BytesIO(
            json.dumps(
                {"success": True, "data": {"assessment_id": "abc-123", "status": "submitted"}}
            ).encode()
        )

    monkeypatch.setenv("SANKA_MIGRATE_API_BASE", "https://legacy.example/migration-service/")
    monkeypatch.setattr(
        local_cli,
        "SankaMigrateApiClient",
        lambda **kwargs: SankaMigrateApiClient(opener=opener, **kwargs),
    )
    # The assessment command pads to two seconds of form time; skip the wait.
    monkeypatch.setattr("sanka.cli.time.time", lambda: 10_000.0)
    prefix = ["--base-url", override] if override else []
    result = runner.invoke(cli, [*prefix, "assess", "--source", "SAP ECC", "--json"])
    assert result.exit_code == 0, result.output
    assert requests[0].full_url == expected
    assert requests[0].get_method() == "POST"
    assert requests[0].get_header("Authorization") is None
