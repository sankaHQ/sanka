# SPDX-License-Identifier: Apache-2.0
"""Run Sanka Migration Bench against this checkout's converter output.

For every benchmark task, copy the pinned fixture source, run the real
lifecycle (scan / plan / apply --bench-candidate), and grade the untouched
candidate with the tool-neutral evaluator from the sanka-bench checkout.

The gate is readiness-aware, because the bench suite deliberately contains
tasks outside the native envelope:

- readiness 100%: the candidate must be fully migrated (the converter's
  regression gate, unchanged);
- readiness below 50%: default apply must refuse cleanly and leave a gap
  report — the safe low-readiness behavior is itself under regression;
- partial readiness: after proving the default refusal when applicable, the
  gate explicitly opts in with ``--min-readiness 0``. That partial candidate
  must boot and evaluate with its gaps disclosed; full migration is not
  required, silent breakage is still a failure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Readiness-aware abstention still needs a regression floor. Without this
# reviewed envelope, a converter bug that drops a task from 100% to 90% could
# be mislabeled as an expected partial migration merely because the remaining
# scaffold boots. Eligible-route totals identify the exact fixture contract;
# native counts are minimums so genuine converter improvements remain allowed.
EXPECTED_ROUTE_ENVELOPE = {
    "drf-fastapi-001": (7, 7),
    "drf-fastapi-002": (7, 7),
    "drf-fastapi-003": (7, 7),
    "drf-fastapi-004": (7, 13),
    "drf-fastapi-005": (1, 8),
    "drf-fastapi-006": (1, 7),
    "drf-fastapi-007": (1, 7),
    "drf-fastapi-008": (1, 29),
    "drf-fastapi-009": (0, 12),
    "drf-fastapi-010": (0, 5),
}


def _readiness_envelope_error(task: str, native_routes: int, eligible_routes: int) -> str | None:
    expected = EXPECTED_ROUTE_ENVELOPE.get(task)
    if expected is None:
        return f"{task}: missing reviewed readiness envelope; update EXPECTED_ROUTE_ENVELOPE"
    minimum_native, expected_eligible = expected
    if eligible_routes != expected_eligible:
        return (
            f"{task}: eligible route count changed from {expected_eligible} to "
            f"{eligible_routes}; review the fixture and readiness envelope"
        )
    if native_routes < minimum_native:
        return (
            f"{task}: native readiness regressed from at least "
            f"{minimum_native}/{expected_eligible} to {native_routes}/{eligible_routes}"
        )
    return None


def _discover_tasks(bench_dir: Path) -> tuple[str, ...]:
    lane = bench_dir / "tasks" / "drf-fastapi"
    return tuple(sorted(entry.name for entry in lane.iterdir() if (entry / "task.yaml").is_file()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench-dir", required=True, help="path to a sanka-bench checkout")
    parser.add_argument("--runner", default="local", choices=("local", "docker"))
    args = parser.parse_args()
    bench_dir = Path(args.bench_dir).resolve()
    if not (bench_dir / "pyproject.toml").is_file():
        print(f"sanka-bench checkout not found at {bench_dir}", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("SANKA_TEST_DB", None)
    tasks = _discover_tasks(bench_dir)
    if not tasks:
        print(f"no benchmark tasks found in {bench_dir}", file=sys.stderr)
        return 2
    failures: list[str] = []
    for task in tasks:
        source = bench_dir / "tasks" / "drf-fastapi" / task / "source"
        if not source.is_dir():
            failures.append(f"{task}: fixture source missing in bench checkout")
            continue
        with tempfile.TemporaryDirectory(prefix=f"sanka-bench-gate-{task}-") as temp:
            project = Path(temp) / "project"
            candidate = Path(temp) / "candidate"
            result_path = Path(temp) / "result.json"
            shutil.copytree(source, project)

            def _sanka(step: list[str], cwd: Path = project) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; from sanka.cli import main; sys.exit(main(sys.argv[1:]))",
                        *step,
                    ],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )

            failed = False
            for step in (
                ["scan", str(project)],
                ["plan", str(project), "--to", "fastapi"],
            ):
                outcome = _sanka(step)
                if outcome.returncode != 0:
                    failures.append(f"{task}: sanka {step[0]} failed: {outcome.stderr.strip()}")
                    failed = True
                    break
            if failed:
                continue
            plan_payload = json.loads(
                (project / ".sanka" / "plan-fastapi.json").read_text(encoding="utf-8")
            )
            native_routes = int(plan_payload["native_routes"])
            eligible_routes = int(plan_payload["native_eligible_routes"])
            readiness = float(plan_payload["readiness"])
            if envelope_error := _readiness_envelope_error(task, native_routes, eligible_routes):
                failures.append(envelope_error)
                continue
            apply_step = [
                "apply",
                "--root",
                str(project),
                "--plan-hash",
                str(plan_payload["plan_hash"]),
                "--bench-candidate",
                str(candidate),
            ]
            applied = _sanka(apply_step)
            if readiness < 0.5:
                if applied.returncode == 0:
                    failures.append(f"{task}: default apply generated below the 50% readiness gate")
                    continue
                if not (candidate / "GAP-REPORT.md").is_file():
                    failures.append(
                        f"{task}: low-readiness apply refused without a gap report: "
                        f"{applied.stderr.strip() or applied.stdout.strip()}"
                    )
                    continue
            if native_routes == 0:
                print(
                    f"{task}: outside the native envelope "
                    f"(readiness {readiness:.0%}); refusal + gap report verified"
                )
                continue
            if readiness < 0.5:
                applied = _sanka([*apply_step, "--min-readiness", "0"])
            if applied.returncode != 0:
                failures.append(f"{task}: sanka apply failed: {applied.stderr.strip()}")
                continue
            evaluated = subprocess.run(
                [
                    "uv",
                    "run",
                    "--project",
                    str(bench_dir),
                    "sanka-bench",
                    "evaluate",
                    "--runner",
                    args.runner,
                    "--task",
                    str(bench_dir / "tasks" / "drf-fastapi" / task),
                    "--candidate",
                    str(candidate),
                    "--output",
                    str(result_path),
                ],
                cwd=bench_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=1200,
                check=False,
            )
            if evaluated.returncode != 0 or not result_path.is_file():
                detail = evaluated.stderr.strip() or evaluated.stdout.strip()
                failures.append(f"{task}: evaluator failed: {detail}")
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            gates = ", ".join(
                f"{name}={'ok' if value else 'FAIL'}"
                for name, value in sorted(result["hard_gates"].items())
            )
            if readiness == 1.0:
                if result.get("fully_migrated") is True:
                    print(f"{task}: fully migrated ({gates})")
                else:
                    failures.append(f"{task}: NOT fully migrated ({gates})")
                    for error in result.get("errors", [])[:5]:
                        failures.append(f"{task}:   {error}")
            elif result["hard_gates"].get("target_boot") is True:
                print(
                    f"{task}: partial envelope (readiness {readiness:.0%}); "
                    f"candidate boots and evaluates with gaps disclosed ({gates})"
                )
            else:
                failures.append(
                    f"{task}: partial candidate (readiness {readiness:.0%}) "
                    f"failed to boot under evaluation ({gates})"
                )
                for error in result.get("errors", [])[:5]:
                    failures.append(f"{task}:   {error}")
    if failures:
        print("bench gate FAILED:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"bench gate passed: {len(tasks)} task(s) within expectation for their readiness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
