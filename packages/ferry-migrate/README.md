# sanka-migrate

The [Sanka Migrate](https://github.com/sankaHQ/sanka-migrate) Runtime: the migration
lifecycle (`create → inspect → plan → apply → verify`), planner, execution
engine (batching, throttling, retries, checkpoints, resume, identity ledger),
local state store, verification framework, and the `sanka-migrate` CLI. The
`ferry` executable remains a compatibility alias.

Licensed **AGPL-3.0-only**; commercial licenses are available from
Sanka, Inc. for embedding without AGPL obligations. Connector authors should depend on
[`sanka-migrate-connector-sdk`](../ferry-connector-sdk/) (Apache-2.0) instead — never
on this package.

**Status: pre-release.** Migration-as-code specs (`ferry.runtime.spec`, YAML +
programmatic, with env-reference resolution and secret-key rejection) and
canonical plan hashing (`ferry.runtime.hashing`) are in place, together with
the engine, state store, and CLI lifecycle commands. APIs may still change
before the first stable release.
