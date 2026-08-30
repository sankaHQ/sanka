# SPDX-License-Identifier: Apache-2.0
"""The release workflow must keep bootstrap and steady-state authority separate."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
PUBLISH_ACTION = "pypa/gh-action-pypi-publish@"


def _expected_projects() -> set[str]:
    module = ast.parse((ROOT / "scripts" / "check_release_artifacts.py").read_text())
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "EXPECTED_LICENSES"
            for target in statement.targets
        ):
            licenses = ast.literal_eval(statement.value)
            return set(licenses)
    raise AssertionError("EXPECTED_LICENSES is missing")


def _workflow() -> dict[str, Any]:
    loaded = yaml.load(
        (ROOT / ".github" / "workflows" / "publish.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(loaded, dict)
    return loaded


def _bench_workflow() -> dict[str, Any]:
    loaded = yaml.load(
        (ROOT / ".github" / "workflows" / "bench.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(loaded, dict)
    return loaded


def _publish_step(job: dict[str, Any]) -> dict[str, Any]:
    steps = job["steps"]
    matches = [step for step in steps if str(step.get("uses", "")).startswith(PUBLISH_ACTION)]
    assert len(matches) == 1
    return matches[0]


def test_publish_workflow_exposes_each_bootstrap_package() -> None:
    workflow = _workflow()
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]

    assert set(inputs["package"]["options"]) == _expected_projects()
    assert {"bootstrap-testpypi", "bootstrap-pypi"}.issubset(inputs["target"]["options"])


def test_publish_workflow_scopes_normal_and_bootstrap_artifacts() -> None:
    jobs = _workflow()["jobs"]

    for job_name in ("publish-testpypi", "publish-pypi"):
        assert _publish_step(jobs[job_name])["with"]["packages-dir"] == "release/all/"

    for job_name in ("bootstrap-testpypi", "bootstrap-pypi"):
        job = jobs[job_name]
        assert "SANKA_MIGRATE_BOOTSTRAP_ENABLED" in job["if"]
        assert "inputs.package" in job["environment"]
        assert _publish_step(job)["with"]["packages-dir"] == (
            "release/packages/${{ inputs.package }}/"
        )


def test_only_publish_jobs_receive_oidc_permission() -> None:
    jobs = _workflow()["jobs"]

    assert "id-token" not in jobs["build"].get("permissions", {})
    for job_name, job in jobs.items():
        if job_name == "build":
            continue
        assert job["permissions"] == {"id-token": "write"}


def test_private_bench_credentials_only_run_on_trusted_main() -> None:
    workflow = _bench_workflow()
    triggers = workflow["on"]
    assert "pull_request" not in triggers
    assert triggers == {"push": {"branches": ["main"]}}

    steps = workflow["jobs"]["bench"]["steps"]
    secret_checkouts = [
        step
        for step in steps
        if step.get("with", {}).get("token") == "${{ secrets.SANKA_BENCH_TOKEN }}"
    ]
    assert len(secret_checkouts) == 1
    assert secret_checkouts[0]["with"]["persist-credentials"] == "false"
    for step in steps:
        uses = step.get("uses")
        if uses is not None:
            assert "@v" not in uses
