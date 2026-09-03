# SPDX-License-Identifier: AGPL-3.0-only
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest

import sanka.cli as cli
import sanka.connector
import sanka.runtime
from sanka.cli import DEFAULT_SPEC_FILE, DEFAULT_STATE_FILE, main
from sanka.cli._research import SankaMigrateApiClient, SankaMigrateApiError, signup_url
from sanka.runtime.engine import MigrationEngine

pytestmark = pytest.mark.usefixtures("trusted_connector_discovery")


def test_compatibility_namespace_exposes_sdk_and_runtime() -> None:
    # Existing integrations keep the sanka.connector import while the Apache
    # SDK is distributed independently as sanka-connector-sdk.
    assert sanka.connector.__version__
    assert sanka.runtime.__version__


def test_version_flag_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_default_paths_use_public_project_names() -> None:
    assert DEFAULT_SPEC_FILE == "sanka.yaml"
    assert DEFAULT_STATE_FILE == ".sanka/migrate/state.db"


def test_no_args_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "migrations with a finish line" in output
    # `validate` joined the subcommand set in F-6; argparse renders the choices
    # line from the full set, so this is the one pre-existing assertion the
    # additive subcommand forces to grow.
    assert (
        "{scan,plan,validate,apply,test,verify,status,migrate,connect,research,assess,extension}"
        in output
    )


def test_extension_management_uses_stable_json_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, object]] = []

    class Record:
        def __init__(self, kind: str) -> None:
            self.kind = kind

        def to_dict(self) -> dict[str, object]:
            return {"kind": self.kind}

    class Store:
        def __init__(self, project_root: Path) -> None:
            assert project_root == tmp_path

        def marketplaces(self) -> tuple[Record, ...]:
            calls.append(("marketplace_list", None))
            return (Record("marketplace"),)

        def add_marketplace(self, source: str, *, name: str | None, trust: bool) -> Record:
            calls.append(("marketplace_add", (source, name, trust)))
            return Record("marketplace")

        def upgrade_marketplace(self, name: str | None) -> tuple[Record, ...]:
            calls.append(("marketplace_upgrade", name))
            return (Record("marketplace"),)

        def remove_marketplace(self, name: str) -> Record:
            calls.append(("marketplace_remove", name))
            return Record("marketplace")

        def list_extensions(self) -> tuple[Record, ...]:
            calls.append(("list", None))
            return (Record("extension"),)

        def add_extension(self, extension_id: str, *, marketplace: str | None) -> Record:
            calls.append(("add", (extension_id, marketplace)))
            return Record("lock")

        def remove_extension(self, extension_id: str) -> None:
            calls.append(("remove", extension_id))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "ExtensionStore", Store)
    commands = [
        (["extension", "list", "--json"], "list", "extension"),
        (
            ["extension", "add", "example/demo", "--marketplace", "fixtures", "--json"],
            "add",
            "lock",
        ),
        (["extension", "remove", "example/demo", "--json"], "remove", None),
        (
            [
                "extension",
                "marketplace",
                "add",
                "https://example.invalid/extensions.git",
                "--name",
                "fixtures",
                "--trust",
                "--json",
            ],
            "marketplace_add",
            "marketplace",
        ),
        (
            ["extension", "marketplace", "list", "--json"],
            "marketplace_list",
            "marketplace",
        ),
        (
            ["extension", "marketplace", "upgrade", "fixtures", "--json"],
            "marketplace_upgrade",
            "marketplace",
        ),
        (
            ["extension", "marketplace", "remove", "fixtures", "--json"],
            "marketplace_remove",
            "marketplace",
        ),
    ]
    for arguments, operation, kind in commands:
        assert main(arguments) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == "sanka-cli/v1"
        assert payload["command"] == "extension"
        assert payload["outcome"] == "success"
        assert payload["migration_state"] == "not_started"
        record = {"id": "example/demo"} if kind is None else {"kind": kind}
        assert payload["data"] == {"operation": operation, "records": [record]}

    assert calls == [
        ("list", None),
        ("add", ("example/demo", "fixtures")),
        ("remove", "example/demo"),
        (
            "marketplace_add",
            ("https://example.invalid/extensions.git", "fixtures", True),
        ),
        ("marketplace_list", None),
        ("marketplace_upgrade", "fixtures"),
        ("marketplace_remove", "fixtures"),
    ]


def test_extension_errors_preserve_stable_code_and_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "marketplace"
    source.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SANKA_HOME", str(tmp_path / "sanka-home"))

    assert (
        main(
            [
                "extension",
                "marketplace",
                "add",
                str(source),
                "--name",
                "third-party",
                "--json",
            ]
        )
        == 1
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "extension"
    assert payload["data"]["error"]["code"] == "SANKA_MARKETPLACE_TRUST_REQUIRED"
    assert payload["data"]["error"]["details"] == {
        "identity": f"local:{source.resolve().as_posix()}"
    }


def test_extension_invalid_store_path_emits_one_clean_json_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid_home = tmp_path / "sanka-home"
    invalid_home.write_text("not a directory", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SANKA_HOME", str(invalid_home))

    assert main(["extension", "list", "--json"]) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert captured.out.count('"schema_version"') == 1
    assert payload["schema_version"] == "sanka-cli/v1"
    assert payload["command"] == "extension"
    assert payload["data"]["error"]["code"] == "SANKA_EXTENSION_PATH"


def test_sdk_command_parser_errors_use_the_versioned_json_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["apply", "--json"]) == 2

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["schema_version"] == "sanka-cli/v1"
    assert payload["command"] == "apply"
    assert payload["outcome"] == "error"
    assert payload["migration_state"] == "not_started"
    assert payload["data"]["error"]["code"] == "SANKA_USAGE"
    assert "--plan-hash" in payload["data"]["error"]["message"]


def test_connect_reports_an_installed_provider(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["connect", "markdown"]) == 0

    output = capsys.readouterr().out
    assert "markdown: ready (source)" in output
    assert "provided by installed package sanka-connector-markdown" in output


def test_connect_json_normalizes_postgresql(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["connect", "postgresql", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "provider": "postgres",
        "roles": ["source", "destination"],
        "installed": True,
        "package": "sanka-connector-postgres",
    }


def test_connect_rejects_an_unknown_provider(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["connect", "not-a-provider"]) == 1
    error = capsys.readouterr().err
    assert "no installed connector" in error
    assert "sanka extension add sanka/not-a-provider" in error


@pytest.mark.parametrize(
    ("provider", "label"),
    [("hubspot", "HubSpot"), ("salesforce", "Salesforce"), ("sendgrid", "SendGrid")],
)
def test_connect_routes_system_providers_to_the_hosted_api(
    provider: str, label: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["connect", provider]) == 1

    error = capsys.readouterr().err
    assert label in error
    assert "hosted System Migration API" in error
    assert f"sanka-connector-{provider}" not in error


# -- local validation ---------------------------------------------------------


def _spec_file(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    """A markdown -> sqlite spec on disk; returns (spec_file, db, base args)."""
    content = tmp_path / "content"
    content.mkdir(parents=True, exist_ok=True)
    (content / "a.md").write_text("---\ntitle: A\n---\nAlpha body\n", encoding="utf-8")
    (content / "b.md").write_text("---\ntitle: B\n---\nBeta body\n", encoding="utf-8")
    db = tmp_path / "out.db"
    spec_file = tmp_path / "sanka.yaml"
    spec_file.write_text(
        f"source:\n  type: markdown\n  connection: {content}\n"
        f"target:\n  type: sqlite\n  connection: {db}\n",
        encoding="utf-8",
    )
    return spec_file, db, ["-f", str(spec_file), "--state", str(tmp_path / "state.db")]


def test_spec_lifecycle_uses_the_versioned_json_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _spec_file_path, _db, base = _spec_file(tmp_path)

    assert main(["plan", "--json", *base]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["schema_version"] == "sanka-cli/v1"
    assert planned["command"] == "plan"
    assert planned["outcome"] == "success"
    assert planned["migration_state"] == "planned"
    plan_hash = planned["data"]["plan_hash"]

    assert main(["apply", "--json", *base, "--plan-hash", plan_hash]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["schema_version"] == "sanka-cli/v1"
    assert applied["command"] == "apply"
    assert applied["data"]["plan_hash"] == plan_hash
    assert applied["migration_state"] == "applied_not_verified"

    assert main(["verify", "--json", *base]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["schema_version"] == "sanka-cli/v1"
    assert verified["command"] == "verify"
    assert verified["outcome"] == "success"
    assert verified["migration_state"] == "verified_within_scope"
    assert verified["data"]["ok"] is True


def test_spec_verify_failure_keeps_report_in_structured_error_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _spec_file_path, _db, base = _spec_file(tmp_path)

    assert main(["plan", "--json", *base]) == 0
    planned = json.loads(capsys.readouterr().out)

    assert main(["verify", "--json", *base]) == 1
    failed = json.loads(capsys.readouterr().out)

    assert failed["schema_version"] == "sanka-cli/v1"
    assert failed["command"] == "verify"
    assert failed["outcome"] == "error"
    assert failed["migration_state"] == "verification_failed"
    error = failed["data"]["error"]
    assert isinstance(error, dict)
    assert isinstance(error.get("code"), str) and error["code"]
    assert isinstance(error.get("message"), str) and error["message"]
    assert "details" not in error or isinstance(error["details"], dict)
    assert failed["data"]["run_id"] == planned["data"]["run_id"]
    assert failed["data"]["ok"] is False
    assert failed["data"]["routes"] == [
        {
            "destination_count": None,
            "failed": 0,
            "migrated": 0,
            "ok": False,
            "route_key": "documents|documents",
            "source_count": 2,
        }
    ]


def test_explicit_data_spec_wins_over_application_artifacts_for_all_shared_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _spec_file_path, _db, base = _spec_file(tmp_path)
    root = tmp_path / "application"
    artifact_root = root / ".sanka"
    artifact_root.mkdir(parents=True)

    assert main(["plan", str(root), "--json", *base]) == 0
    first_plan = json.loads(capsys.readouterr().out)
    plan_hash = first_plan["data"]["plan_hash"]
    (artifact_root / "scan.json").write_text("{}", encoding="utf-8")
    (artifact_root / "plan.json").write_text("{}", encoding="utf-8")

    assert main(["plan", str(root), "--json", *base]) == 0
    assert json.loads(capsys.readouterr().out)["migration_state"] == "planned"
    assert main(["apply", "--root", str(root), "--json", *base, "--plan-hash", plan_hash]) == 0
    assert json.loads(capsys.readouterr().out)["migration_state"] == "applied_not_verified"
    assert main(["verify", str(root), "--json", *base]) == 0
    assert json.loads(capsys.readouterr().out)["migration_state"] == "verified_within_scope"


def test_validate_help_lists_its_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["validate", "--help"])
    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "--sample" in output
    assert "--full" in output
    assert "--json" in output
    assert "without writing" in output


def test_validate_requires_a_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _spec_file_path, db, base = _spec_file(tmp_path)

    assert main(["validate", *base]) == 1

    captured = capsys.readouterr()
    assert "run `sanka plan` first" in captured.err
    assert not db.exists()


def test_validate_is_write_free_and_exits_zero_when_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _spec_file_path, db, base = _spec_file(tmp_path)

    assert main(["plan", *base]) == 0
    assert main(["validate", *base]) == 0

    output = capsys.readouterr().out
    assert "validation OK (write-free)" in output
    assert "documents -> documents: sampled=2 valid=2 invalid=0" in output
    # Write-freedom, observed end to end: the destination was never created.
    assert not db.exists()

    # ...while apply on the same spec does create it.
    plan_hash = sqlite3.connect(base[-1]).execute("SELECT plan_hash FROM runs").fetchone()[0]
    assert plan_hash
    assert main(["apply", *base, "--plan-hash", plan_hash]) == 0
    assert db.exists()
    count = sqlite3.connect(db).execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert count == 2


def test_validate_json_prints_the_deterministic_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _spec_file_path, _db, base = _spec_file(tmp_path)
    assert main(["plan", *base]) == 0
    capsys.readouterr()

    assert main(["validate", "--json", *base]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"] == []
    assert payload["rejects"] == []
    (row,) = payload["objects"]
    assert row["sourceObject"] == "documents"
    assert row["sampled"] == 2


def test_validate_exits_nonzero_when_invalid_records_exist(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _spec_file_path, _db, base = _spec_file(tmp_path)
    assert main(["plan", *base]) == 0

    async def invalid_payload(
        self: MigrationEngine, run_id: str, *, sample_size: int = 10, full: bool = False
    ) -> dict[str, Any]:
        return {
            "objects": [
                {
                    "sourceObject": "documents",
                    "destinationObject": "documents",
                    "sourceFilter": None,
                    "sampled": 2,
                    "valid": 1,
                    "invalid": 1,
                    "mappedFields": 3,
                    "invalidReasons": [
                        {
                            "code": "SANKA_MIGRATE_REQUIRED_SOURCE_FIELD_EMPTY",
                            "message": "Required source field is empty.",
                            "sourceField": "documents.title",
                            "targetField": "title",
                            "count": 1,
                        }
                    ],
                }
            ],
            "rejects": [
                {
                    "sourceObject": "documents",
                    "destinationObject": "documents",
                    "routeKey": "documents|documents",
                    "sourceRecordId": "b.md",
                    "code": "SANKA_MIGRATE_REQUIRED_SOURCE_FIELD_EMPTY",
                    "message": "Required source field is empty.",
                    "sourceField": "documents.title",
                    "targetField": "title",
                }
            ],
            "warnings": [],
        }

    monkeypatch.setattr(MigrationEngine, "validate", invalid_payload)
    capsys.readouterr()

    assert main(["validate", *base]) == 1

    output = capsys.readouterr().out
    assert "validation FAILED (write-free)" in output
    assert "sampled=2 valid=1 invalid=1  [INVALID]" in output
    assert (
        "1x SANKA_MIGRATE_REQUIRED_SOURCE_FIELD_EMPTY: Required source field is empty."
        "  (documents.title -> title)" in output
    )


def test_validate_rejects_a_non_positive_sample(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _spec_file_path, _db, base = _spec_file(tmp_path)
    assert main(["plan", *base]) == 0

    assert main(["validate", "--sample", "0", *base]) == 1

    assert "SANKA_MIGRATE_DRY_RUN_LIMIT_INVALID" in capsys.readouterr().err


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode()
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._body


def test_research_client_uses_public_branded_base_and_unwraps_data() -> None:
    requests: list[tuple[Request, float]] = []

    @contextmanager
    def opener(request: Request, *, timeout: float) -> Iterator[_FakeResponse]:
        requests.append((request, timeout))
        yield _FakeResponse({"success": True, "data": {"events": [], "dataset": {}}})

    client = SankaMigrateApiClient(opener=opener)
    data = client.research_eol(
        product=None,
        category="erp-migration",
        event_type="shutdown",
        after="2027-01",
        before=None,
        locale="ja",
    )

    assert data == {"events": [], "dataset": {}}
    request, timeout = requests[0]
    parsed = urlparse(request.full_url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://api.sanka.com/v2/migrate/research/eol"
    )
    assert parse_qs(parsed.query) == {
        "category": ["erp-migration"],
        "type": ["shutdown"],
        "after": ["2027-01"],
        "locale": ["ja"],
    }
    assert request.get_header("User-agent") == "sanka-cli/0.2.1"
    assert timeout == 10.0


def test_research_client_surfaces_standard_error_and_retry_after() -> None:
    def opener(request: Request, *, timeout: float) -> Any:
        body = BytesIO(
            json.dumps(
                {
                    "success": False,
                    "error": {"code": "SANKA_MIGRATE_RATE_LIMITED", "message": "Slow down."},
                }
            ).encode()
        )
        headers = Message()
        headers["Retry-After"] = "60"
        raise HTTPError(request.full_url, 429, "Too Many Requests", headers, body)

    client = SankaMigrateApiClient(opener=opener)

    with pytest.raises(SankaMigrateApiError) as excinfo:
        client.research_tco(product=None, category=None, locale=None)

    assert excinfo.value.code == "SANKA_MIGRATE_RATE_LIMITED"
    assert excinfo.value.retry_after == "60"


class _FakeResearchClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def research_eol(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("eol", kwargs))
        return {
            "events": [
                {
                    "product": {
                        "slug": "opsgenie",
                        "name": {"en": "Opsgenie", "ja": "オプスジーニー"},
                    },
                    "date": "2027-04-05",
                    "event_type": "shutdown",
                    "change": "Support ends",
                    "citations": [
                        {
                            "kind": "vendor_primary",
                            "source_url": "https://vendor.example/eol",
                            "verified_on": "2026-08-09",
                        }
                    ],
                }
            ],
            "dataset": {"name": "eol", "version": "2026-08-19.eol0001"},
            "attribution": {
                "name": "Sanka Research",
                "url": "https://sanka.com/docs/migrate/eol/",
                "license": "CC BY 4.0",
            },
        }

    def research_tco(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("tco", kwargs))
        return {"benchmarks": [], "dataset": {}, "attribution": {}}

    def research_compare(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("compare", kwargs))
        return {"platforms": [], "dataset": {}, "attribution": {}}

    def assess(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("assess", payload))
        return {"assessment_id": "7c9d1e42-0000-4000-8000-000000000000", "status": "submitted"}


def test_research_cli_renders_citations_and_attribution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeResearchClient()
    monkeypatch.setattr("sanka.cli._research_client", lambda: fake)

    assert main(["research", "eol", "--after", "2027-01", "--lang", "en"]) == 0

    output = capsys.readouterr().out
    assert "Opsgenie" in output
    assert "Support ends [1]" in output
    assert "https://vendor.example/eol  (verified 2026-08-09)" in output
    assert output.rstrip().endswith(
        "Sanka Research — https://sanka.com/docs/migrate/eol/  (CC BY 4.0)"
    )
    assert fake.calls == [
        (
            "eol",
            {
                "product": None,
                "category": None,
                "event_type": None,
                "after": "2027-01",
                "before": None,
                "locale": "en",
            },
        )
    ]


def test_research_json_is_unwrapped_and_empty_result_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeResearchClient()
    monkeypatch.setattr("sanka.cli._research_client", lambda: fake)

    assert main(["research", "tco", "--json"]) == 2

    assert json.loads(capsys.readouterr().out) == {
        "benchmarks": [],
        "dataset": {},
        "attribution": {},
    }


def test_assess_waits_honestly_and_prints_branded_handoff(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeResearchClient()
    times = iter((1_000.0, 1_003.0))
    monkeypatch.setattr("sanka.cli._research_client", lambda: fake)
    monkeypatch.setattr("sanka.cli.time.time", lambda: next(times))

    assert main(["assess", "--source", "SAP ECC", "--lang", "en"]) == 0

    output = capsys.readouterr().out
    assert "assessment submitted: 7c9d1e42-0000-4000-8000-000000000000" in output
    assert "%2Fmigrate-onboarding" in output
    assert ("fer" + "ry") not in output.casefold()
    kind, payload = fake.calls[0]
    assert kind == "assess"
    assert payload["source"] == "SAP ECC"
    assert payload["attribution"] == {"channel": "cli"}
    assert payload["website"] == ""
    assert payload["form_started_at"] == 1_000_000


def test_signup_url_has_no_retired_public_name() -> None:
    url = signup_url("abc-123", lang="ja")

    assert url.startswith("https://app.sanka.com/ja/signup?")
    assert "migrate-onboarding" in url
    assert ("fer" + "ry") not in url.casefold()
