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
    assert set(jobs) == {"build", "publish", "installer", "homebrew"}
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


def test_installer_distribution_follows_pypi_and_tests_before_publication() -> None:
    job = _workflow("publish.yml")["jobs"]["installer"]
    assert job["needs"] == "publish"
    assert job["permissions"] == {"contents": "write"}
    runs = [step.get("run", "") for step in job["steps"]]
    smoke = next(index for index, run in enumerate(runs) if "smoke_installer.py" in run)
    publish = next(index for index, run in enumerate(runs) if "gh release create" in run)
    assert smoke < publish
    assert "--verify-tag" in runs[publish]
    assert "--clobber" not in runs[publish]
    assert 'test "$(cat release/SOURCE_COMMIT)" = "$GITHUB_SHA"' in runs[publish]


def test_homebrew_candidate_is_validated_and_preserves_human_review() -> None:
    job = _workflow("publish.yml")["jobs"]["homebrew"]
    assert job["needs"] == "publish"
    assert job["permissions"] == {"contents": "read"}
    runs = [step.get("run", "") for step in job["steps"]]
    prepare = next(index for index, run in enumerate(runs) if "scripts/prepare_release.py" in run)
    validate = next(index for index, run in enumerate(runs) if "scripts/check_formula.sh" in run)
    report = next(index for index, run in enumerate(runs) if "review_required" in run)
    assert prepare < validate < report
    assert "--expected-sha256" in runs[prepare]
    assert "homebrew_base_commit" in runs[report]
    assert "gh pr merge" not in "\n".join(runs)
