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

import sanka.connector
import sanka.runtime
from sanka.cli import DEFAULT_SPEC_FILE, DEFAULT_STATE_FILE, main
from sanka.cli._research import SankaMigrateApiClient, SankaMigrateApiError, signup_url
from sanka.runtime.engine import MigrationEngine


def test_bundled_namespace_exposes_interface_and_runtime() -> None:
    # The one sanka-migrate distribution keeps the Apache connector API and
    # AGPL runtime importable through the same public namespace.
    assert sanka.connector.__version__
    assert sanka.runtime.__version__


def test_version_flag_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_default_paths_use_public_project_names() -> None:
    assert DEFAULT_SPEC_FILE == "sanka-migrate.yaml"
    assert DEFAULT_STATE_FILE == ".sanka/migrate/state.db"


def test_no_args_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "verify finite migrations" in output
    # `validate` joined the subcommand set in F-6; argparse renders the choices
    # line from the full set, so this is the one pre-existing assertion the
    # additive subcommand forces to grow.
    assert "{plan,validate,apply,verify,status,migrate,connect,research,assess}" in output


def test_connect_reports_a_bundled_provider(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["connect", "hubspot"]) == 0

    output = capsys.readouterr().out
    assert "hubspot: ready (source, destination)" in output
    assert "no connector plugin is required" in output


def test_connect_json_normalizes_postgresql(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["connect", "postgresql", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "provider": "postgres",
        "roles": ["source", "destination"],
        "bundled": True,
    }


def test_connect_rejects_an_unknown_provider(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["connect", "not-a-provider"]) == 1
    assert "no connector installed" in capsys.readouterr().err


# -- sanka-migrate validate ---------------------------------------------------


def _spec_file(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    """A markdown -> sqlite spec on disk; returns (spec_file, db, base args)."""
    content = tmp_path / "content"
    content.mkdir(parents=True, exist_ok=True)
    (content / "a.md").write_text("---\ntitle: A\n---\nAlpha body\n", encoding="utf-8")
    (content / "b.md").write_text("---\ntitle: B\n---\nBeta body\n", encoding="utf-8")
    db = tmp_path / "out.db"
    spec_file = tmp_path / "sanka-migrate.yaml"
    spec_file.write_text(
        f"source:\n  type: markdown\n  connection: {content}\n"
        f"target:\n  type: sqlite\n  connection: {db}\n",
        encoding="utf-8",
    )
    return spec_file, db, ["-f", str(spec_file), "--state", str(tmp_path / "state.db")]


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
    assert "run `sanka-migrate plan` first" in captured.err
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
    assert main(["apply", *base]) == 0
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
                "name": "Sanka Migrate Research",
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
        "Sanka Migrate Research — https://sanka.com/docs/migrate/eol/  (CC BY 4.0)"
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
