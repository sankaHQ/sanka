# Install Sanka

## macOS and Linux

Download the installer from the latest published Sanka release, then run it:

```bash
curl -fLsS https://github.com/sankaHQ/sanka/releases/latest/download/install.sh -o /tmp/sanka-install.sh
sh /tmp/sanka-install.sh
sanka --help
sanka doctor
```

The installer downloads a checksum-verified, pinned uv bootstrap and manages
Python 3.12 privately under `~/.local/share/sanka/cli`. It validates the installed
CLI version, package metadata, Python version and help command before switching
`~/.local/bin/sanka` to the new environment. Previous environments are retained.
It requires `curl` and `sha256sum` or `shasum`; system Python and administrator
access are not required. Repeating the installer verifies the selected
version. `--version X.Y.Z` selects an exact published version.

Existing package-manager executables are never overwritten. If one is detected,
the installer prints its path and the owning package manager's upgrade command.
`--coexist` explicitly permits a second installation in a different bin directory.
`SANKA_INSTALL_DIR` and `SANKA_BIN_DIR` select absolute custom directories.
The installer reports which executable PATH selects and prints the precise PATH
command when needed. It does not edit shell profiles. Existing zsh sessions may
need `rehash`; bash sessions may need `hash -r`. Shell aliases must be updated by
their owner. Save a printed PATH change in your shell profile if it is needed in
future terminals. `sanka doctor --json` also reports these conflicts.

## Homebrew

```bash
brew trust --formula sankahq/cli/sanka
brew install sankaHQ/cli/sanka
sanka --help
```

Homebrew supplies the correct Python runtime. Upgrade with `brew update` followed
by `brew upgrade sankaHQ/cli/sanka`. Each release records whether PyPI is published
and the matching Homebrew update is still awaiting review. Check `sanka --version`
when following version-specific instructions. `doctor` requires CLI 0.2.11+.

## uv, including Windows

Install uv using its [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/).
On macOS/Linux the command is:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Follow uv's printed shell setup instructions, then install Sanka with an explicit
Python version. uv downloads Python when needed and isolates the CLI environment:

```bash
uv tool install --python 3.12 sanka-cli
sanka --help
sanka doctor
```

Use `uv tool upgrade sanka-cli --python 3.12` for upgrades. If uv reports an exact
version pin, use `uv tool install --python 3.12 sanka-cli@latest` to replace the pin.
Avoid maintaining both
a Homebrew and uv installation on the same PATH.

## pip in an existing Python environment

Use a Python 3.12+ virtual environment that you manage. For example:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install sanka-cli
```

Bare `pip` can belong to an older Python even when a newer Python is installed.
`pip --version` shows the interpreter it uses. Python 3.9 produces “No matching
distribution found” because pip filters incompatible packages before Sanka runs.
Upgrading pip does not change its Python interpreter. For project-local code
migrations, install the source application's dependencies in its own compatible
environment; installing the isolated CLI does not install those dependencies.

## Diagnose an installation

### Update an existing quickstart project

After upgrading the CLI, refresh its official catalog from the project directory.
For a project set up with `sanka extension add sanka/drf-to-fastapi`:

```bash
sanka extension marketplace upgrade official
sanka extension add sanka/drf-to-fastapi
sanka scan .
```

The catalog refresh preserves project locks. The explicit `extension add` selects
and locks the new DRF extension for this project. Review a new plan before applying
it. If you gave the official marketplace a different name, use the name shown by
`sanka extension marketplace list` in the first command.

### Check the selected executable

`sanka doctor` is read-only and works without a token, keychain access or network.
It reports the running version, executable, Python runtime, all executable PATH
candidates, duplicate installations and recovery commands. It does not execute
other discovered binaries or claim an installed extension is authenticated.

```bash
sanka doctor --json
sanka doctor --expected-version 0.3.0
```

JSON uses `sanka-doctor/v1`. Errors (unsupported runtime or unexpected version)
exit 1. PATH warnings exit 0 and remain explicit in `status` and `checks`. A child
process cannot inspect the parent shell's command cache or aliases; the report
states this limitation and supplies shell refresh commands.

### The upgraded CLI still has an old version or no doctor command

Installing or upgrading with uv changes uv's copy, not a separate Homebrew copy.
Your shell profile can put that older executable first even after uv succeeds.
Compare the command selected by your shell with uv's installed command:

```bash
command -v sanka
sanka --version
"$(uv tool dir --bin)/sanka" --version
"$(uv tool dir --bin)/sanka" doctor
```

If you intend to use uv, select its executable directory before other copies:

```bash
export PATH="$(uv tool dir --bin):$PATH"
rehash  # zsh; use hash -r in bash
sanka --version
sanka doctor
```

For new terminals, keep the same PATH order after other PATH changes in your
shell profile. In zsh, `.zshrc` is read after `.zshenv`, so an earlier setting
alone may be overridden. If `sanka` is an alias or function, inspect it with
`type sanka` and update that definition too; doctor cannot inspect its parent
shell's aliases or cached commands.

A duplicate-installation warning means doctor selected the current CLI but found
another copy on PATH. If you keep uv, remove the redundant Homebrew formula
through `brew uninstall sanka`, then clear the shell cache and run doctor again.
Do not delete package-manager files manually. Keeping both copies is supported,
but each must be upgraded through its own package manager.
