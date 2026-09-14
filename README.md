<div align="center">

<img src="https://raw.githubusercontent.com/sankaHQ/sanka/main/docs/assets/sanka-logo.png" alt="Sanka" width="120" height="120">

# Sanka

The open source runtime to migrate DRF to FastAPI (and more to come): scan the
source, review an immutable plan, apply that exact plan, and verify the result.
One `sanka` executable covers everything you need for migration projects - Open source and available as a [hosted service](https://sanka.com/developer/). Python 3.12 or newer is required.

[![PyPI](https://img.shields.io/pypi/v/sanka-cli)](https://pypi.org/project/sanka-cli/)
[![Python](https://img.shields.io/pypi/pyversions/sanka-cli)](https://pypi.org/project/sanka-cli/)
[![License](https://img.shields.io/badge/license-Apache--2.0%20AND%20AGPL--3.0-blue)](LICENSE)
[![Benchmarks](https://img.shields.io/badge/benchmarks-sanka.com%2Fbench-ff5a1f)](https://sanka.com/bench)

</div>

---

For the hosted repository execution with a credit limit, see
[Cloud Runs](docs/cloud-runs.md). The `sanka cloud` commands use the workspace's
developer API token; the hosted service must be enabled separately.

## Why Sanka?

- **The migration actually finishes**: `verify` reconciles the generated result against the source — an exit code alone is never treated as completion evidence
- **Review once, apply exactly that**: `apply` requires the exact reviewed plan hash, so nothing drifts between what you approved and what ran
- **Measurably better than the model alone**: +11 points aggregate across eight frontier models on 17 matched migrations — the weaker the model, the bigger the gain ([see benchmarks](#benchmarks))
- **Local by default**: `scan`, `plan`, `apply`, `test` and `verify` never require a Sanka token
- **Verified extensions, isolated execution**: every wheel is checked against its manifest, URL, SHA-256 digest and runtime constraint, then run in an isolated child environment — PyPI is not a fallback
- **Built for agents**: add `--json` to any command for one machine-readable `sanka-cli/v1` document — no scraping human output
- **Open source**: Apache-2.0 tooling around an AGPL-3.0 migration engine

## Benchmarks

Every model gets better at migrations with Sanka, and the models that need the
most help gain the most. Same 17 migrations, same prompts, isolated workspaces —
the only difference is the CLI.

| Model | Model only | + Sanka CLI | Δ |
| --- | --- | --- | --- |
| GPT-6 Astra (high) | 16/17 · 94.1% | **17/17 · 100.0%** | +5.9 |
| Claude Opus 5 (high) | 17/17 · 100.0% | 17/17 · 100.0% | — |
| Claude Sonnet 5 (high) | 12/17 · 70.6% | **16/17 · 94.1%** | +23.5 |
| GPT-5.6 Terra (high) | 12/17 · 70.6% | **15/17 · 88.2%** | +17.6 |
| GPT-5.6 Sol (high) | 15/17 · 88.2% | 15/17 · 88.2% | — |
| GLM 5.3 Flash (high) | 11/17 · 64.7% | **14/17 · 82.4%** | +17.6 |
| GPT-5.6 Luna (high) | 13/17 · 76.5% | 13/17 · 76.5% | — |
| DeepSeek V4 Flash (high) | 5/17 · 29.4% | **9/17 · 52.9%** | +23.5 |
| **Aggregate** | **101/136 · 74.3%** | **116/136 · 85.3%** | **+11.0** |

The suite is 17 migrations — 11 Django REST Framework to FastAPI and 6 to Flask —
run at high reasoning effort. A pass requires all eight grading gates. These are
synthetic repository fixtures, not production migrations. Measured on
`sanka-cli` 0.2.7 with extensions 0.1.0a16; the latest set substitutes the eight
CLI upload-task reruns, so it is not a fresh full-suite pass@1 run.

Full leaderboard, per-task results, cost and token efficiency, and the raw JSON:
**[sanka.com/bench](https://sanka.com/bench)**. The evaluator, tasks and baselines
are open source at [sankaHQ/bench](https://github.com/sankaHQ/bench).

---

## Install

On macOS/Linux, use the Sanka installer, which manages Python for you:

```bash
curl -fLsS https://github.com/sankaHQ/sanka/releases/latest/download/install.sh -o /tmp/sanka-install.sh
sh /tmp/sanka-install.sh
sanka --help
sanka doctor
```

With [uv installed](https://docs.astral.sh/uv/getting-started/installation/),
including on Windows, explicitly select the supported Python runtime:

```bash
uv tool install --python 3.12 sanka-cli
sanka --help
```

The macOS/Linux uv prerequisite is `curl -LsSf https://astral.sh/uv/install.sh | sh`;
follow its printed shell setup instructions. Advanced pip users should use a
Python 3.12+ virtual environment and `python -m pip install sanka-cli`.
Bare `pip` can select an older system interpreter and report “No matching distribution found.”
The installer first ships with CLI 0.2.11. See [installation and recovery](docs/install.md)
for Homebrew upgrades, PATH conflicts, and project environments.

## Extensions

The base installation contains no provider or framework implementation.
Official extensions are immutable GitHub release wheels described by the
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions) marketplace.
Configure its trusted snapshot, then install only the components required by a
migration:

```bash
sanka extension marketplace add https://github.com/sankaHQ/extensions.git --name sanka
sanka extension add sanka/drf-to-fastapi
```

Sanka verifies each manifest, URL, SHA-256 digest, runtime constraint, and
wheel identity before installing it in an isolated environment. PyPI is not a
extension fallback. The official catalog defaults to the published bundle pinned by this
CLI release, never the development branch. Upgrade the CLI, then run
`sanka extension marketplace upgrade` to adopt its catalog without changing project
pins. To inspect a candidate or retain a particular catalog, add a separately named
marketplace with `--revision FULL_COMMIT_SHA`; upgrades retain that exact revision.
Explicit revisions may refer to unpublished artifacts, which still fail installation.

Project pins live in `.sanka/extensions.lock`; marketplace
snapshots and verified artifacts live under `~/.sanka/extensions` or
`$SANKA_HOME/extensions`.

## Local lifecycle

A migration runs in five steps:

```bash
cd my-django-app
sanka scan .
sanka plan .
sanka apply --plan-hash sha256:<hash-from-plan>
sanka test .
sanka verify .
```

`scan` and `plan` do not write to the destination. `apply` requires the exact
reviewed plan hash. `verify` reconciles the generated or transferred result;
an exit code alone is not completion evidence. Add `--json` for one
`sanka-cli/v1` machine-readable document.

New to the CLI? Start with the
[quickstart](https://sanka.com/docs/developers/quickstart/cli/). See the
[DRF to FastAPI guide](docs/django-to-fastapi.md) for the supported native and
compatibility envelopes.

## Local and hosted commands

Authentication is selected after command routing:

| Surface | Execution | Sanka token |
|---|---|---|
| `scan`, `plan`, `validate`, `apply`, `test`, `verify`, `status`, `migrate`, `connect`, `extension` | local runtime or verified extension | never |
| `plan`, `apply`, or `verify` with an explicit cloud selector | hosted migration API | required |
| `auth`, resources, workflows, AI, `functions` (custom functions) | hosted Sanka API | required where the command already requires it |
| research and assessment | public hosted API | not required |

Missing hosted credentials do not block local help, inspection, planning,
extension management, testing, or verification.

The Python and Node `sanka-sdk` migration adapters are thin local subprocess
clients. They invoke `sanka <command> ... --json`; they do not install the CLI,
reimplement migration behavior, or silently switch local commands to the
hosted API.

## SDKs

Drive the same lifecycle from your own code:

- **Python** — `pip install sanka-sdk` · [SDK reference](https://sanka.com/docs/developers/sdk-python/) · [quickstart](https://sanka.com/docs/developers/quickstart/python/)
- **Node.js** — `npm install sanka-sdk` · [SDK reference](https://sanka.com/docs/developers/sdk-node/) · [quickstart](https://sanka.com/docs/developers/quickstart/nodejs/)

## Licensing

The wheel declares `Apache-2.0 AND AGPL-3.0-only` and contains both license
texts plus `NOTICE`:

| Source zone | License |
|---|---|
| `packages/sanka-cli/src/sanka_cli` | Apache-2.0 |
| `packages/sanka-cli/src/sanka_extensions` (including the SDK compatibility modules) | Apache-2.0 |
| `packages/sanka-cli/src/sanka` | AGPL-3.0-only |
| `scripts`, `tests`, and `docs` | Apache-2.0 |

The canonical Apache Sanka Extension SDK and provider sources remain in
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions). See
[LICENSE](LICENSE), [architecture](docs/ARCHITECTURE.md), and
[dependency review](docs/dependency-licenses.md).

## Development

```bash
uv sync --frozen --all-packages
make check
make build-release
```

`make check` verifies the Sanka Extension SDK against
the immutable `extensions` commit recorded in `scripts/check_connector_sdk_sync.py`.
The check validates both the canonical facade and compatibility implementation;
`--upstream-repo` can point to an existing clone containing the pinned commit.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a PR. Open or reuse an
issue first, agree on the scope of substantial changes with a maintainer, then
link your PR to that issue. The guide also explains development checks and the
repository's license boundaries.
