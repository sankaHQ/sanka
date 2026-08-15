# ferry-migrate

The [Ferry](https://github.com/sankaHQ/ferry) Migration Runtime: the migration
lifecycle (`create → inspect → plan → apply → verify`), planner, execution
engine (batching, throttling, retries, checkpoints, resume, identity ledger),
local state store, verification framework, and the `ferry` CLI.

Licensed **AGPL-3.0-only**; commercial licenses are available from Sanka for
embedding without AGPL obligations. Connector authors should depend on
[`ferry-connector-sdk`](../ferry-connector-sdk/) (Apache-2.0) instead — never
on this package.

**Status: pre-release scaffold.** The engine and CLI lifecycle commands land
in Phase 2; today `ferry --version` is the extent of it.
