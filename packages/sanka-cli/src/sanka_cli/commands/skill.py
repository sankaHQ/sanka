# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from importlib import resources
from pathlib import Path

import click

import sanka_cli.runtime as runtime
from sanka_cli.state import CLIState

SKILL_NAME = "sanka-cli"
HARNESSES = ("claude", "codex")
CONFIG_ROOTS = {
    "claude": ("CLAUDE_CONFIG_DIR", ".claude"),
    "codex": ("CODEX_HOME", ".codex"),
}


def _skill_content() -> bytes:
    return resources.files("sanka_cli").joinpath("skills", SKILL_NAME, "SKILL.md").read_bytes()


def _target_directory(harness: str, scope: str, project_directory: Path) -> Path:
    environment_name, default_directory = CONFIG_ROOTS[harness]
    if scope == "project":
        root = project_directory.resolve() / f".{harness}"
    else:
        configured_root = os.environ.get(environment_name)
        root = (
            Path(configured_root).expanduser()
            if configured_root
            else Path.home() / default_directory
        )
    return root / "skills" / SKILL_NAME


def _reject_project_symlinks(project_directory: Path, target: Path) -> None:
    current = project_directory.resolve()
    for part in (*target.relative_to(current).parts, "SKILL.md"):
        current /= part
        if current.is_symlink():
            raise click.ClickException(
                f"Refusing project installation through symbolic link: {current}"
            )


def _planned_status(target: Path, content: bytes, *, force: bool) -> str:
    skill_file = target / "SKILL.md"
    try:
        if skill_file.is_file() and skill_file.read_bytes() == content:
            return "unchanged"
        if target.exists() and not force:
            raise click.ClickException(
                f"{target} already exists with different content; "
                "rerun with --force to replace SKILL.md."
            )
        return "updated" if target.exists() else "installed"
    except OSError as exc:
        raise click.ClickException(f"Could not inspect {target}: {exc}") from exc


def _write_skill(target: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target, prefix=".SKILL.md.", delete=False
        ) as file:
            file.write(content)
            temporary_path = Path(file.name)
        os.replace(temporary_path, target / "SKILL.md")
    except OSError as exc:
        raise click.ClickException(f"Could not install {SKILL_NAME} at {target}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@click.group()
def skill() -> None:
    """Install Sanka skills into supported AI coding harnesses."""


@skill.command("install")
@click.argument("harness", required=False, type=click.Choice(HARNESSES))
@click.option("--scope", type=click.Choice(["project", "global"]), default=None)
@click.option(
    "--project-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
)
@click.option("--force", is_flag=True, help="Replace a different installed SKILL.md.")
@click.pass_obj
def install_skill(
    state: CLIState,
    harness: str | None,
    scope: str | None,
    project_dir: Path,
    force: bool,
) -> None:
    """Install the sanka-cli skill for Claude Code, Codex, or detected harnesses."""
    harnesses = [harness] if harness else [name for name in HARNESSES if shutil.which(name)]
    if not harnesses:
        raise click.ClickException(
            "No supported harness detected. Pass `claude` or `codex` explicitly."
        )
    resolved_scope = scope or click.prompt(
        "Install scope",
        type=click.Choice(["project", "global"]),
        default="project",
        err=True,
    )
    content = _skill_content()
    plans: list[tuple[str, Path, str]] = []
    for harness_name in harnesses:
        target = _target_directory(harness_name, resolved_scope, project_dir)
        if resolved_scope == "project":
            _reject_project_symlinks(project_dir, target)
        plans.append((harness_name, target, _planned_status(target, content, force=force)))

    installations = []
    for harness_name, target, status in plans:
        if status != "unchanged":
            _write_skill(target, content)
        installations.append(
            {
                "harness": harness_name,
                "path": str(target),
                "status": status,
            }
        )
    runtime.emit_payload(
        {
            "skill": SKILL_NAME,
            "scope": resolved_scope,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "installations": installations,
        },
        state,
    )
