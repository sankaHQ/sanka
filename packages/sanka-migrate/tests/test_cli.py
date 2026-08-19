# SPDX-License-Identifier: AGPL-3.0-only
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import sanka.connector
import sanka.runtime
from sanka.cli import DEFAULT_SPEC_FILE, DEFAULT_STATE_FILE, main
from sanka.runtime.engine import MigrationEngine


def test_namespace_merges_across_packages() -> None:
    # sanka.connector (Apache-2.0 dist) and sanka.runtime (AGPL dist) are
    # separately installed package portions; both must import together.
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
    assert "{plan,validate,apply,verify,status,migrate}" in output


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
