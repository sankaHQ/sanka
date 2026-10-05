# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import argparse
import io
import json
import shlex
from pathlib import Path
from typing import cast

import pytest

import sanka.cli as cli
from sanka.cli import _build_parser, main
from sanka.cli._output import TerminalOutput
from sanka.runtime.extensions import ExtensionError
from sanka.runtime.extensions.runner import ExtensionResult, ExtensionRunner


@pytest.mark.parametrize("forwarded", [(), ("SANKA_GO_SOURCE_PYTHON",)])
@pytest.mark.parametrize("command", ["scan", "plan", "apply", "test"])
@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--artifact-dir", "reports and plans"],
        ["--extension-env", "SOURCE_PYTHON", "--extension-env", "DATABASE_URL"],
        ["--extension-env", "SANKA_GO_SOURCE_PYTHON"],
    ],
)
def test_next_hint_keeps_the_invocation_context(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    options: list[str],
    forwarded: tuple[str, ...],
) -> None:
    tmp_path = tmp_path / "source project"
    parser = _build_parser()
    invocation = (
        [command, "--root", str(tmp_path), "--plan-hash", "sha256:previous"]
        if command == "apply"
        else [command, str(tmp_path)]
    )
    args = parser.parse_args([*invocation, *options])
    args.root = getattr(args, "root_option", None) or args.root
    result = ExtensionResult(
        "success",
        {"plan_hash": "sha256:reviewed", "extensions": [{"id": "sanka/python-to-golang"}]},
        (),
        (),
        (),
        None,
        forwarded_env_names=forwarded,
    )
    cli._print_application_result(args, command, result, migration_state="planned")
    output = capsys.readouterr().out
    hint = next(
        line.removeprefix("next: ") for line in output.splitlines() if line.startswith("next: ")
    )
    tokens = shlex.split(hint)
    following = parser.parse_args(tokens[1:])
    assert Path(getattr(following, "root_option", None) or following.root) == tmp_path
    assert following.artifact_dir == args.artifact_dir
    assert following.extension_env == list(dict.fromkeys([*args.extension_env, *forwarded]))
    if command == "scan":
        assert following.to == "<target>"
    if command == "plan":
        assert following.plan_hash == result.data["plan_hash"]
    if not options:
        assert "--artifact-dir" not in tokens
        assert ("--extension-env" in tokens) == bool(forwarded)


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


@pytest.mark.parametrize(
    ("flag", "enabled"),
    [("--swagger-ui", True), ("--no-swagger-ui", False)],
)
@pytest.mark.parametrize("target", ["fastapi", "python-fastapi"])
def test_fastapi_swagger_plan_flag_reaches_extension_configuration(
    flag: str, enabled: bool, target: str
) -> None:
    args = _build_parser().parse_args(["plan", ".", "--to", target, flag])
    assert cli._extension_configuration(args)["swagger_ui"] is enabled


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
    presentation = {"help", "json", "compact_dsl", "no_color", "quiet", "verbose", "tui"}
    expected = {
        "scan": {"root", "settings", "artifact_dir", "extension_config", "extension_env"},
        "plan": {
            "endpoint",
            "all_endpoints",
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
            "swagger_ui",
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
            "settings",
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


def test_compact_dsl_preserves_errors_without_legacy_duplicates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = argparse.Namespace(command="verify", json=False, compact_dsl=True)
    error = ExtensionError(
        "PARITY",
        "Mismatch\nInspect details",
        details={
            "summary": {"matched": 17, "scenarios": 19},
            "failures": [{"id": "headers", "message": 'Expected "Allow"'}],
            "warnings": ["Seed protected records"],
            "report_path": "reports/full result.json",
        },
    )
    assert cli._print_cli_error(args, error, exit_code=1) == 1
    output = capsys.readouterr().out
    assert output.startswith("sanka-compact/v1 verify error failed\n")
    fields = dict(line.split("=", 1) for line in output.splitlines()[1:])
    details = json.loads(fields["error"])["details"]
    assert details == error.details
    assert output.count('"matched"') == 1
    assert "\\n" in output
    assert "\x1b" not in output


def test_compact_dsl_usage_errors_and_exclusive_formats(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["apply", "--compact-dsl"]) == 2
    assert "SANKA_USAGE" in capsys.readouterr().out
    assert main(["scan", "--json", "--compact-dsl"]) == 2
    assert "not allowed" in capsys.readouterr().out


def test_compact_plan_keeps_review_hash_and_summarizes_generated_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = argparse.Namespace(command="plan", json=False, compact_dsl=True)
    data = {
        "plan_hash": "sha256:core",
        "error": {"details": {"files": {"target_app.py": "invalid syntax"}}},
        "extension": {
            "plan_hash": "sha256:extension",
            "files": {"target_app.py": "pass\n" * 1000},
            "routes": [
                {
                    "path": "/items/",
                    "method": "GET",
                    "source": "pass\n" * 1000,
                    "classification": "native",
                    "reasons": [],
                }
            ],
        },
    }
    result = ExtensionResult("success", data, ("/tmp/plan.json",), ("Scope is limited",), (), None)
    assert cli._print_application_result(args, "plan", result, migration_state="planned") == 0
    output = capsys.readouterr().out
    assert '"files":{"target_app.py":"invalid syntax"}' in output
    assert 'plan_hash="sha256:core"' in output
    assert '"plan_hash":"sha256:extension"' in output
    assert "target_app.py" in output and "Scope is limited" in output
    assert "omitted" in output and "/tmp/plan.json" in output
    assert len(output) < 1000


@pytest.mark.parametrize("command", ["scan", "plan"])
def test_compact_inventory_keeps_decisions_and_artifacts_without_metadata_repetition(
    command: str,
) -> None:
    from copy import deepcopy

    from sanka.cli._compact import render_compact

    route = {
        "method": "POST",
        "path": "/items/",
        "native": False,
        "adaptation_reasons": [{"code": "CUSTOM_WRITE", "message": "Preserve validation"}],
        "parity_notes": [{"code": "EXACT_MESSAGE", "message": "details" * 1000}],
        "options": {"anonymous": {"actions": "metadata" * 1000}},
        "warnings": ["Requires authentication"],
    }
    data = {
        "plan_hash": "sha256:core",
        "routes": [route],
        "serializer_details": [{"fields": "fields" * 1000}],
        "view_details": [{"source": "source" * 1000}],
        "status_codes": {"HTTP_200_OK": 200},
        "fingerprint": {"sha256": "sha256:source", "evidence": ["evidence" * 1000]},
        "warnings": [{"routes": [route]}],
        "extensions": [
            {
                "extension": {"id": "example", "manifest_digest": "sha256:manifest"},
                "data": {"routes": [route], "plan_hash": "sha256:extension"},
            }
        ],
    }
    payload = {
        "command": command,
        "outcome": "success",
        "migration_state": "planned",
        "data": data,
        "artifacts": [".sanka/full.json"],
    }
    before = deepcopy(payload)
    output = render_compact(payload)
    fields = {k: json.loads(v) for k, v in (line.split("=", 1) for line in output.splitlines()[1:])}
    assert fields["routes"][0]["adaptation_reasons"] == route["adaptation_reasons"]
    assert fields["routes"][0]["native"] is False
    assert fields["routes"][0]["parity_notes"] == {"count": 1, "details": "artifacts"}
    assert fields["warnings"] == data["warnings"]
    assert fields["extensions"][0]["data"] == {"plan_hash": "sha256:extension"}
    assert fields["extensions"][0]["extension"]["manifest_digest"] == "sha256:manifest"
    assert fields["fingerprint"]["sha256"] == "sha256:source"
    assert fields["plan_hash"] == "sha256:core"
    assert fields["artifacts"] == payload["artifacts"]
    assert payload == before
    # Without a saved artifact, or on failure, metadata must remain inline.
    payload["artifacts"] = []
    assert '"actions":"metadata' in render_compact(payload)
    payload["artifacts"] = [".sanka/full.json"]
    payload["outcome"] = "error"
    assert '"actions":"metadata' in render_compact(payload)


def test_compact_keeps_diagnostics_nested_inside_descriptive_metadata() -> None:
    from sanka.cli._compact import render_compact

    data = {"serializer_details": [{"nested": {"warnings": ["Unsupported validator"]}}]}
    payload = {
        "command": "scan",
        "outcome": "success",
        "migration_state": "scanned",
        "data": data,
        "artifacts": [".sanka/scan.json"],
    }
    text = render_compact(payload)
    assert json.loads(text.splitlines()[1].split("=", 1)[1]) == data["serializer_details"]


@pytest.mark.parametrize(
    ("command", "artifact_name"),
    [
        ("test", "test.json"),
        ("test", "generated/.venv"),
        ("verify", "verify.json"),
        ("plan", "plan.json"),
    ],
)
@pytest.mark.parametrize("mode", [[], ["--json"], ["--compact-dsl"], ["--quiet"]])
def test_lifecycle_feedback_is_live_and_machine_output_stays_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    artifact_name: str,
    mode: list[str],
) -> None:
    artifact = tmp_path / "reports" / artifact_name
    visible = not mode

    class Lifecycle:
        def __init__(self, _root: Path, **kwargs: object) -> None:
            self.runner = cast(ExtensionRunner | None, kwargs.get("runner"))

        def run(self, **_kwargs: object) -> ExtensionResult:
            if self.runner and self.runner.on_stderr:
                self.runner.on_stderr(b"[sanka] Running Go ")
                self.runner.on_stderr(b"tests\nprivate child output\n")
            during = capsys.readouterr()
            assert ("Running Go tests" in during.err) is visible
            assert "private child output" not in during.err
            return ExtensionResult(
                "success",
                {"plan_hash": "sha256:core", "tests": 3, "environment": ".sanka/go"},
                (str(artifact),),
                (),
                (),
                None,
            )

        test = verify = plan = run

    monkeypatch.setattr(cli, "ApplicationLifecycle", Lifecycle)
    assert main([command, str(tmp_path), "--artifact-dir", "reports", *mode]) == 0
    result = capsys.readouterr()
    if mode == ["--json"]:
        assert json.loads(result.out)["artifacts"] == [str(artifact)]
    elif mode == ["--compact-dsl"]:
        assert result.out.startswith("sanka-compact/v1")
    elif visible and command == "plan":
        assert "Plan file  reports/plan.json" in result.out
    elif visible and command == "test":
        assert "Ran 3 tests" in result.out
        assert ("Report " in result.out) == artifact_name.endswith(".json")
    assert "\x1b" not in result.err
