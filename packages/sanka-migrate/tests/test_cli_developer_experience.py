# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import cast

import pytest

import sanka.cli as cli
from sanka.cli import _build_parser, main
from sanka.cli._output import TerminalOutput
from sanka.runtime.extensions import ExtensionError
from sanka.runtime.extensions.runner import ExtensionResult


def test_extension_help_exposes_only_the_management_command_tree(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["extension", "--help"])
    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "{add,list,remove,marketplace}" in output
    assert "manage migration extensions" in output

    with pytest.raises(SystemExit) as raised:
        main(["extension", "marketplace", "add", "--help"])
    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "--name" in output
    assert "--trust" in output
    assert "--json" in output


def test_extension_parser_errors_keep_the_extension_outer_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["extension", "add", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "extension"
    assert payload["outcome"] == "error"
    assert payload["data"]["error"]["code"] == "SANKA_USAGE"


def test_generic_extension_configuration_reaches_the_application_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class Lifecycle:
        def __init__(self, root: Path, **kwargs: object) -> None:
            captured["root"] = root
            captured["init"] = kwargs

        def plan(self, **kwargs: object) -> ExtensionResult:
            captured["plan"] = kwargs
            return ExtensionResult("success", {"plan_hash": "sha256:core"}, (), (), (), None)

    monkeypatch.setattr(cli, "ApplicationLifecycle", Lifecycle)

    assert (
        main(
            [
                "plan",
                str(tmp_path),
                "--to",
                "flask",
                "--generation",
                "full",
                "--output",
                "generated",
                "--extension-config",
                '{"custom":1}',
                "--extension-env",
                "DJANGO_SECRET_KEY",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["schema_version"] == "sanka-cli/v1"
    assert payload["command"] == "plan"
    assert payload["data"]["plan_hash"] == "sha256:core"
    assert captured["plan"] == {
        "configuration": {"custom": 1, "generation": "full", "output": "generated"},
        "explicit_env_names": ("DJANGO_SECRET_KEY",),
        "target": "flask",
    }
    assert "\033[" not in output.out + output.err


def test_clean_root_plan_selects_the_application_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    class Lifecycle:
        def __init__(self, _root: Path, **_kwargs: object) -> None:
            pass

        def plan(self, **_kwargs: object) -> ExtensionResult:
            nonlocal called
            called = True
            return ExtensionResult("success", {"plan_hash": "sha256:core"}, (), (), (), None)

    monkeypatch.setattr(cli, "ApplicationLifecycle", Lifecycle)

    assert main(["plan", str(tmp_path), "--json"]) == 0
    assert called is True
    assert json.loads(capsys.readouterr().out)["data"]["plan_hash"] == "sha256:core"


def test_missing_non_interactive_target_is_a_structured_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Lifecycle:
        def __init__(self, _root: Path, **_kwargs: object) -> None:
            pass

        def plan(self, **_kwargs: object) -> ExtensionResult:
            raise ExtensionError(
                "SANKA_EXTENSION_TARGET_REQUIRED",
                "target required",
                details={"targets": ["fastapi", "flask"]},
            )

    monkeypatch.setattr(cli, "ApplicationLifecycle", Lifecycle)

    assert main(["plan", str(tmp_path), "--to", "fastapi", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "error"
    assert payload["migration_state"] == "not_started"
    assert payload["data"]["error"] == {
        "code": "SANKA_EXTENSION_TARGET_REQUIRED",
        "details": {"targets": ["fastapi", "flask"]},
        "message": "target required",
    }


def test_real_install_prompt_declines_by_default_and_preserves_selected_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = (
        "decline",
        "vendor/demo (marketplace-one)",
        "vendor/demo (marketplace-two)",
    )
    answers = iter(["", "3"])
    monkeypatch.setattr("builtins.input", lambda _label: next(answers))

    assert cli._lifecycle_prompt("Choose an extension to install", choices) == "decline"
    assert cli._lifecycle_prompt("Choose an extension to install", choices) == choices[2]


def test_extension_config_requires_a_json_object(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", ".", "--extension-config", "[]", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["error"]["code"] == "SANKA_USAGE"
    assert "JSON object" in payload["data"]["error"]["message"]


def test_compatibility_flags_do_not_add_target_defaults() -> None:
    args = _build_parser().parse_args(["plan", ".", "--to", "fastapi"])

    assert cli._extension_configuration(args) == {}


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


def _command_parser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return cast(argparse.ArgumentParser, action.choices[name])
    raise AssertionError(f"command parser not found: {name}")


def test_sdk_command_functional_options_are_explicit() -> None:
    parser = _build_parser()
    presentation = {"help", "json", "no_color", "quiet", "verbose"}
    expected = {
        "scan": {"root", "settings", "artifact_dir", "extension_config", "extension_env"},
        "plan": {
            "root",
            "file",
            "state",
            "to",
            "strategy",
            "artifact_dir",
            "output",
            "generation",
            "package_manager",
            "orm",
            "extension_config",
            "extension_env",
        },
        "apply": {
            "plan_hash",
            "root",
            "file",
            "state",
            "to",
            "artifact_dir",
            "output",
            "force",
            "orm",
            "min_readiness",
            "gap_report_only",
            "bench_candidate",
            "extension_config",
            "extension_env",
        },
        "test": {
            "root",
            "file",
            "state",
            "to",
            "artifact_dir",
            "output",
            "extension_config",
            "extension_env",
        },
        "verify": {
            "root",
            "file",
            "state",
            "to",
            "artifact_dir",
            "output",
            "cases",
            "no_http",
            "scenarios",
            "candidate",
            "entrypoint",
            "db_env",
            "seed",
            "ignore_tables",
            "all_headers",
            "edge_probes",
            "python",
            "candidate_python",
            "extension_config",
            "extension_env",
        },
    }

    for command, command_expected in expected.items():
        actual = {
            "root" if action.dest == "root_option" else action.dest
            for action in _command_parser(parser, command)._actions
            if action.dest not in presentation
        }
        assert actual == command_expected, command


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


def test_verify_replay_flags_reach_the_extension_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class Lifecycle:
        def __init__(self, root: Path, **kwargs: object) -> None:
            captured["root"] = root

        def verify(self, **kwargs: object) -> ExtensionResult:
            captured["verify"] = kwargs
            return ExtensionResult("success", {"ok": True}, (), (), (), None)

    monkeypatch.setattr(cli, "ApplicationLifecycle", Lifecycle)
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text("[]", encoding="utf-8")
    seed = tmp_path / "seed.py"
    seed.write_text("", encoding="utf-8")
    assert (
        main(
            [
                "verify",
                str(tmp_path),
                "--to",
                "fastapi",
                "--scenarios",
                str(scenarios),
                "--candidate",
                str(tmp_path),
                "--entrypoint",
                "target_app.py",
                "--db-env",
                "BENCH_DB_PATH",
                "--seed",
                str(seed),
                "--ignore-table",
                "django_session",
                "--ignore-table",
                "audit_log",
                "--all-headers",
                "--edge-probes",
                "--source-python",
                "/opt/source/.venv/bin/python",
                "--candidate-python",
                "/opt/candidate/.venv/bin/python",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "verify"
    configuration = captured["verify"]["configuration"]  # type: ignore[index]
    assert configuration["scenarios"] == str(scenarios)
    assert configuration["candidate"] == str(tmp_path)
    assert configuration["entrypoint"] == "target_app.py"
    assert configuration["db_env"] == "BENCH_DB_PATH"
    assert configuration["seed"] == str(seed)
    assert configuration["ignore_tables"] == ["django_session", "audit_log"]
    assert configuration["all_headers"] is True
    assert configuration["edge_probes"] is True
    assert configuration["python"] == "/opt/source/.venv/bin/python"
    assert configuration["candidate_python"] == "/opt/candidate/.venv/bin/python"
    assert "no_http" not in configuration
