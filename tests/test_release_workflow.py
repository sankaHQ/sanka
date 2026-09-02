# SPDX-License-Identifier: Apache-2.0
"""Release workflows publish one verified ``sanka-cli`` artifact set."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
PUBLISH_ACTION = "pypa/gh-action-pypi-publish@"


def _workflow(name: str) -> dict[str, Any]:
    loaded = yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(loaded, dict)
    return loaded


def _uses_steps(job: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if str(step.get("uses", "")).startswith(prefix)]


def test_publish_workflow_has_one_package_and_job_scoped_oidc() -> None:
    workflow = _workflow("publish.yml")
    inputs = workflow["on"]["workflow_dispatch"].get("inputs", {})
    jobs = workflow["jobs"]

    assert set(inputs) == {"confirmation"}
    assert set(jobs) == {"build", "publish"}
    assert "id-token" not in jobs["build"].get("permissions", {})
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    assert jobs["publish"]["environment"] == "pypi"

    source = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assert "sanka-migrate" not in source
    assert source.count(PUBLISH_ACTION) == 1


def test_publish_job_downloads_and_hash_checks_the_build_artifact() -> None:
    jobs = _workflow("publish.yml")["jobs"]
    build = jobs["build"]
    publish = jobs["publish"]
    uploads = _uses_steps(build, "actions/upload-artifact@")
    downloads = _uses_steps(publish, "actions/download-artifact@")
    publishers = _uses_steps(publish, PUBLISH_ACTION)

    assert len(uploads) == len(downloads) == len(publishers) == 1
    assert uploads[0]["with"]["name"] == downloads[0]["with"]["name"]
    assert uploads[0]["with"]["path"] == "sanka/release/"
    assert downloads[0]["with"]["path"] == "release/"
    assert publishers[0]["with"]["packages-dir"] == "release/"
    verification_steps = [
        step for step in publish["steps"] if "sha256sum --check SHA256SUMS" in step.get("run", "")
    ]
    assert len(verification_steps) == 1
    assert "rm SHA256SUMS SOURCE_COMMIT" in verification_steps[0]["run"]


def test_ci_and_publish_do_not_require_private_cross_repo_checkout() -> None:
    for workflow_name in ("ci.yml", "publish.yml"):
        workflow = _workflow(workflow_name)
        job_name = "check" if workflow_name == "ci.yml" else "build"
        job = workflow["jobs"][job_name]
        extension_checkouts = [
            step
            for step in _uses_steps(job, "actions/checkout@")
            if step.get("with", {}).get("repository") == "sankaHQ/extensions"
        ]

        assert extension_checkouts == []
        assert "SANKA_CONNECTOR_SDK_SOURCE" not in job.get("env", {})
        assert job["defaults"]["run"]["working-directory"] == "sanka"


def test_ci_and_publish_install_optional_mcp_dependencies() -> None:
    for workflow_name in ("ci.yml", "publish.yml"):
        workflow = _workflow(workflow_name)
        job_name = "check" if workflow_name == "ci.yml" else "build"
        runs = [step.get("run") for step in workflow["jobs"][job_name]["steps"]]

        assert "uv sync --frozen --all-packages --all-extras" in runs


def test_ci_defers_connector_e2e_until_marketplace_artifacts_exist() -> None:
    job = _workflow("ci.yml")["jobs"]["check"]

    assert "services" not in job
    assert "SANKA_MIGRATE_TEST_POSTGRES_DSN" not in job.get("env", {})
    assert "SANKA_MIGRATE_TEST_CLICKHOUSE_URL" not in job.get("env", {})


def test_private_bench_credentials_only_run_on_trusted_main() -> None:
    workflow = _workflow("bench.yml")
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
