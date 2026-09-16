# SPDX-License-Identifier: Apache-2.0
"""Use the user's workspace-scoped Sanka GitHub connection, never GitHub secrets."""

from __future__ import annotations

import re
import webbrowser
from typing import Any

import click

from sanka_cli import runtime
from sanka_cli.commands.cloud import WORKSPACE, _data, _read
from sanka_cli.state import CLIState

SOURCE_ROOT = "/v2/migrate/code-sources"


def connection_url(workspace: str) -> str:
    return f"https://code.sanka.com/{workspace}?edit=source"


def pages(state: CLIState, workspace: str, kind: str, **params: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _data(_read(state, workspace, f"{SOURCE_ROOT}/github/{kind}", page=page, **params))
        result.extend({**item, "page": page} for item in data[kind])
        next_page = data.get("next_page")
        if next_page is None:
            return result
        if type(next_page) is not int or not page < next_page <= 1000:
            raise click.ClickException("GitHub returned invalid pagination")
        page = next_page


def select_repository(
    state: CLIState, workspace: str, repository: str, branch: str | None
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise click.BadParameter("use owner/repository", param_hint="--github")
    status = _data(_read(state, workspace, f"{SOURCE_ROOT}/status"))
    if not status.get("github_connected"):
        raise click.ClickException(
            f"Connect GitHub for this workspace and token owner: {connection_url(workspace)}"
        )
    for installation in pages(state, workspace, "installations"):
        for repo in pages(state, workspace, "repositories", installation_id=installation["id"]):
            if repo["full_name"].lower() != repository.lower():
                continue
            selection = {
                "installation_id": installation["id"],
                "repository_id": repo["id"],
                "repository_page": repo["page"],
            }
            wanted = branch or repo["default_branch"]
            for item in pages(state, workspace, "branches", **selection):
                if item["name"] == wanted:
                    return {**selection, "branch": wanted, "revision": item["sha"]}
            raise click.ClickException(f"Branch {wanted} is not available in {repository}")
    raise click.ClickException(
        "Repository is not accessible to this Sanka GitHub connection. Run sanka "
        "cloud github connect."
    )


@click.group()
def github() -> None:
    """Configure or inspect the GitHub connection used by Sanka Code."""


@github.command("connect")
@WORKSPACE
@click.option("--open", "open_browser", is_flag=True, help="Open Sanka Code to connect GitHub.")
@click.pass_obj
def connect(state: CLIState, workspace: str, open_browser: bool) -> None:
    """Connect in Sanka Code with the same user as your CLI token, then return here."""
    status = _data(_read(state, workspace, f"{SOURCE_ROOT}/status"))
    url = connection_url(workspace)
    if open_browser:
        webbrowser.open(url)
    runtime.emit_payload(
        {
            **status,
            "connect_url": url,
            "instructions": "Sign in as the CLI token owner, select GitHub "
            "repository and Connect GitHub. Grant access to the intended "
            "repositories, then retry the CLI command.",
        },
        state,
    )


@github.command("repositories")
@WORKSPACE
@click.pass_obj
def repositories(state: CLIState, workspace: str) -> None:
    """List repositories available through the existing Sanka GitHub App installation."""
    result: list[dict[str, Any]] = []
    for installation in pages(state, workspace, "installations"):
        result.extend(
            {**repo, "installation_id": installation["id"]}
            for repo in pages(state, workspace, "repositories", installation_id=installation["id"])
        )
    runtime.emit_payload({"repositories": result}, state)
