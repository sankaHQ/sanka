# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import sanka.cli as cli
from sanka.cli import _build_parser, main
from sanka.cli._output import TerminalOutput
from sanka.runtime.frameworks import (
    FrameworkMigrationError,
    apply_fastapi_plan,
    load_fastapi_plan,
    load_framework_scan,
    plan_fastapi,
    verify_fastapi_migration,
)

FIXTURE = Path(__file__).parent / "fixtures" / "drf_crud_project"


def _run_cli(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from sanka.cli import main; raise SystemExit(main(sys.argv[1:]))",
            *args,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )


def _scan(project: Path) -> None:
    result = _run_cli(project, "scan", ".", "--json")
    assert result.returncode == 0, result.stderr


@pytest.fixture
def scanned_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    monkeypatch.chdir(project)
    _scan(project)
    return project


def test_interactive_full_update_and_conflict_flow(
    scanned_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = scanned_project / "fastapi-app"
    answers = iter(("", "", str(output), "", "", ""))
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["plan", str(scanned_project)]) == 0
    rendered = capsys.readouterr().out
    assert "Choose target framework" in rendered
    assert "FastAPI" in rendered
    plan = load_fastapi_plan(scanned_project)
    assert plan.generation_mode == "full"
    assert plan.package_manager == "uv"

    generated, _routes = apply_fastapi_plan(
        scanned_project,
        plan_hash=plan.plan_hash,
    )
    assert generated == output
    for name in (
        "app/main.py",
        "app/api/router.py",
        "app/api/health.py",
        "app/core/config.py",
        "app/core/logging.py",
        "app/core/database.py",
        "tests/__init__.py",
        ".sanka/generated-manifest.json",
        ".env.example",
        "README.md",
    ):
        assert (output / name).is_file(), name
    store = (output / "app/generated/sanka_store.py").read_text(encoding="utf-8")
    assert 'modules={"models": ["app.generated.models"]}' in store
    migrated = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--run-syncdb", "--verbosity", "0"],
        cwd=scanned_project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert migrated.returncode == 0, migrated.stderr
    tested = _run_cli(scanned_project, "test", ".", "--json")
    assert tested.returncode == 0, tested.stdout + tested.stderr
    tested_payload = json.loads(tested.stdout)
    assert tested_payload["missing_dependency"] is None
    assert Path(tested_payload["python"]).resolve() == (output / ".venv/bin/python").resolve()
    verified = _run_cli(scanned_project, "verify", ".", "--json")
    assert verified.returncode == 0, verified.stdout + verified.stderr
    verified_payload = json.loads(verified.stdout)
    assert (
        Path(verified_payload["data"]["paths"]["python"]).resolve()
        == (output / ".venv/bin/python").resolve()
    )
    imported = subprocess.run(
        [sys.executable, "-c", "from app.main import app; assert app"],
        cwd=output,
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr
    assert verify_fastapi_migration(scanned_project, output=output, probe_http=False)["ok"]

    urls = scanned_project / "crud_config" / "urls.py"
    urls.write_text(
        urls.read_text(encoding="utf-8").replace(
            'router.register("gadgets", GadgetViewSet, basename="gadget")',
            'router.register("gadgets", GadgetViewSet, basename="gadget")\n'
            'router.register("widgets", GadgetViewSet, basename="widget")',
        ),
        encoding="utf-8",
    )
    _scan(scanned_project)
    update = plan_fastapi(
        scanned_project,
        output=str(output),
        generation_mode="update",
    )
    assert update.target_generation_mode == "full"
    assert any(item.action == "unchanged" for item in update.file_operations)
    apply_fastapi_plan(scanned_project, plan_hash=update.plan_hash)
    manifest = json.loads((output / "sanka-manifest.json").read_text(encoding="utf-8"))
    assert any(route["path"] == "/api/widgets/" for route in manifest["routes"])

    config = output / "app/core/config.py"
    config.write_text(config.read_text(encoding="utf-8") + "# user edit\n", encoding="utf-8")
    conflicted = plan_fastapi(
        scanned_project,
        output=str(output),
        generation_mode="update",
    )
    assert any(
        item.path == "app/core/config.py" and item.action == "conflict"
        for item in conflicted.file_operations
    )
    with pytest.raises(FrameworkMigrationError, match="user-modified files"):
        apply_fastapi_plan(scanned_project, plan_hash=conflicted.plan_hash)

    apply_fastapi_plan(scanned_project, plan_hash=conflicted.plan_hash, force=True)
    drift_plan = plan_fastapi(
        scanned_project,
        output=str(output),
        generation_mode="update",
    )
    logging_file = output / "app/core/logging.py"
    logging_file.write_text(
        logging_file.read_text(encoding="utf-8") + "# changed after plan\n",
        encoding="utf-8",
    )
    with pytest.raises(FrameworkMigrationError, match="changed after planning"):
        apply_fastapi_plan(scanned_project, plan_hash=drift_plan.plan_hash)


def test_scan_drives_database_free_generation(scanned_project: Path) -> None:
    scan = load_framework_scan(scanned_project)
    api_root = next(route for route in scan.routes if route.native and route.serializer is None)
    database_free = replace(scan, routes=(api_root,), scan_hash="").with_hash()
    scan_path = scanned_project / ".sanka" / "scan.json"
    scan_path.write_text(json.dumps(database_free.to_dict()), encoding="utf-8")
    output = scanned_project / "database-free"

    plan = plan_fastapi(
        scanned_project,
        output=str(output),
        generation_mode="full",
    )
    assert plan.database_required is False
    assert plan.sql_engine == "none"
    apply_fastapi_plan(scanned_project, plan_hash=plan.plan_hash)

    assert not (output / "app/core/database.py").exists()
    assert not (output / "app/generated/sanka_store.py").exists()
    assert "SANKA_DATABASE_URL" not in (output / ".env.example").read_text(encoding="utf-8")
    assert "lifespan=" not in (output / "app/main.py").read_text(encoding="utf-8")
    requirements = (output / "requirements.txt").read_text(encoding="utf-8")
    assert all(name not in requirements for name in ("tortoise", "sqlalchemy", "psycopg"))


def test_full_compatibility_project_is_importable(scanned_project: Path) -> None:
    output = scanned_project / "compatibility-app"
    plan = plan_fastapi(
        scanned_project,
        output=str(output),
        generation_mode="full",
        strategy="compatibility",
    )
    apply_fastapi_plan(scanned_project, plan_hash=plan.plan_hash)

    imported = subprocess.run(
        [sys.executable, "-c", "from app.main import app; assert app"],
        cwd=output,
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr
    assert (output / "app/generated/sanka_compat.py").is_file()
    assert not (output / "app/core/database.py").exists()


def test_non_tty_and_json_contract(
    scanned_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    assert main(["plan", str(scanned_project)]) == 2
    assert "--to fastapi" in capsys.readouterr().err

    assert (
        main(
            [
                "plan",
                str(scanned_project),
                "--to",
                "fastapi",
                "--generation",
                "minimal",
                "--package-manager",
                "pip",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["schema_version"] == "sanka-cli/v1"
    assert payload["command"] == "plan"
    assert payload["data"]["generation_mode"] == "minimal"
    assert payload["data"]["package_manager"] == "pip"
    assert "\033[" not in output.out + output.err


def test_terminal_color_and_spinner_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    class TtyBuffer(io.StringIO):
        def isatty(self) -> bool:
            return True

    stdout = TtyBuffer()
    stderr = TtyBuffer()
    terminal = TerminalOutput(stdout=stdout, stderr=stderr)
    terminal.heading("Plan")
    terminal.success("complete")
    with terminal.spinner("Scanning"):
        pass

    assert "\033[36m" in stdout.getvalue()
    assert "✓ OK" in stdout.getvalue()
    assert stderr.getvalue().endswith("\r\033[2K")

    monkeypatch.setenv("NO_COLOR", "1")
    plain = TtyBuffer()
    TerminalOutput(stdout=plain, stderr=TtyBuffer()).heading("Plan")
    assert "\033[" not in plain.getvalue()


def _command_paths(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> list[list[str]]:
    paths: list[list[str]] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, child in action.choices.items():
            path = [*prefix, name]
            paths.append(path)
            paths.extend(_command_paths(child, tuple(path)))
    return paths


def test_help_is_available_for_every_registered_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as top:
        main(["--help"])
    assert top.value.code == 0
    capsys.readouterr()
    for path in _command_paths(_build_parser()):
        with pytest.raises(SystemExit) as help_exit:
            main([*path, "-h"])
        assert help_exit.value.code == 0, path
        assert "-h, --help" in capsys.readouterr().out
