# SPDX-License-Identifier: Apache-2.0
"""Run Sanka Migration Bench against this checkout's converter output.

For every benchmark task, copy the pinned fixture source, run the real
four-command lifecycle (scan / plan / apply --bench-candidate), and grade the
untouched candidate with the tool-neutral evaluator from the sanka-bench
checkout. Fails when any candidate is not fully migrated — this is the
converter's regression gate.
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
            steps = (
                ["scan", str(project)],
                ["plan", str(project), "--to", "fastapi"],
                ["apply", "--root", str(project), "--bench-candidate", str(candidate)],
            )
            failed = False
            for step in steps:
                outcome = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; from sanka.cli import main; sys.exit(main(sys.argv[1:]))",
                        *step,
                    ],
                    cwd=project,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if outcome.returncode != 0:
                    failures.append(f"{task}: sanka {step[0]} failed: {outcome.stderr.strip()}")
                    failed = True
                    break
            if failed:
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
            if result.get("fully_migrated") is True:
                print(f"{task}: fully migrated ({gates})")
            else:
                failures.append(f"{task}: NOT fully migrated ({gates})")
                for error in result.get("errors", [])[:5]:
                    failures.append(f"{task}:   {error}")
    if failures:
        print("bench gate FAILED:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"bench gate passed: {len(tasks)} task(s) fully migrated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
