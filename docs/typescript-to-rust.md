# SPDX-License-Identifier: Apache-2.0
# TypeScript to Rust migration (experimental)

`sanka/typescript-to-rust` is a Sanka Code extension that turns a bounded Express
application into a native axum service and proves the result by replaying the
source and the candidate against the same scenarios. It is developed in
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions/tree/main/packages/sanka-extension-typescript-to-rust)
and is **experimental**: it is not listed in the marketplace catalog, so
`sanka extension add` cannot install it yet. Run it from its built wheel through
the `sanka-extension/v1` subprocess protocol (`sanka-extension-typescript-to-rust`)
or from the extensions checkout. Nothing here is a production cutover claim.

## What it migrates today

Source: Express 4 or 5, TypeScript, one application module (`src/app.ts` by
default) with an optional `server.ts`/`index.ts` launcher. Target: an axum 0.8
crate with a pinned `Cargo.lock` and `rust-toolchain.toml` (Rust 1.93.1).

- **Literal JSON GET endpoints.** `app.get(path, (req, res) => res.json({...}))`
  with literal statuses; the crate serves the exact `JSON.stringify` bytes.
- **PostgreSQL schema baseline and bounded reads** (`database_layer: sqlx`).
  A flat `schema.sql` (`integer`, `bigint`, `serial`, `boolean`, `text`,
  `varchar(n)`; one integer primary key per table) and the matching exported
  row types in `src/models.ts` become `src/models.rs`, a reversible sqlx
  baseline under `migrations/` with an empty-schema guard, and a `migrate up|down`
  binary. Reads are `pool.query("SELECT <all columns> FROM t ORDER BY <pk> LIMIT $1", [n])`.
- **Lookups and single-table writes.** `GET /things/:id`, `POST` create,
  `PATCH` update, `PUT` replace and `DELETE` delete qualify when the handler
  equals the canonical idiom rendered from the model (validation with 400
  `{ "error": "invalid request body" }`, 404 `{ "error": "not found" }`, 409
  `{ "error": "conflict" }` on unique violations, `INSERT … RETURNING`,
  `UPDATE … CASE WHEN` presence flags for partial updates, 204 deletes). The
  comparison is by syntax shape, so formatting never matters; anything else is a
  gap that names the operation and model.

Everything outside this envelope is reported as a gap with its file and line.
Gaps block `apply`. Not claimed: authentication, middleware other than
`express.json()`, query parameters, transactions, `bigint` request fields,
Fastify and Hono sources.

## The five commands

The extension speaks the same five-command contract as every Sanka Code
extension. Configuration keys: `source_framework` (`express`), `target_framework`
or `target` (`axum`), `source_file`, `database_layer` (`none` or `sqlx`),
`schema_file`, `models_file`, and `extension_plan_hash` for `apply`.

| Command | Proves |
| --- | --- |
| `scan` | The static capture of routes, models and gaps. Sources are parsed by a vendored TypeScript compiler; nothing is imported or executed. |
| `plan` | Deterministic generated files plus a `plan_hash`; byte-identical across checkout locations. |
| `apply` | Writes the crate only for the reviewed hash (`reviewed_plan_hash` and `extension_plan_hash`), refuses drift and existing output. |
| `test` | Builds the candidate with `cargo test --locked` and drives every scenario through the router in-process; checks expected statuses and JSON media types. |
| `verify` | Additionally runs the transpiled Express source in Node.js 22 on a Unix domain socket and compares status, media type, JSON body and, with a database layer, every captured table's rows and identity sequences after each scenario. |

Scenarios follow the shared `sanka-http-replay` contract: a `sanka-verify.json`
in the project root (the hosted spelling is accepted) or, when absent, a
deterministic default sequence derived from the captured contract. Write
contracts reset both fixture databases to the captured baseline first, which is
why they must be dedicated: `SANKA_RUST_TARGET_TEST_DATABASE_URL` and
`SANKA_RUST_SOURCE_TEST_DATABASE_URL` are read explicitly and an ambient
`DATABASE_URL` is never adopted.

## Toolchains

- Node.js 20 or later for `scan`, `plan` and `apply`; Node.js 22 for `verify`
  (`SANKA_NODE` points at it), with the project's `express` and `pg` under
  `node_modules` or `SANKA_NODE_TOOLS`.
- Rust 1.93.1 through rustup for `test` and `verify`; `CARGO_TARGET_DIR` reuses
  build artifacts across runs; `SANKA_RUST_OFFLINE=1` forbids crate downloads.
- PostgreSQL fixture databases for `database_layer: sqlx`.

## Disclosed non-parity

Express defaults not reproduced: case-insensitive and non-strict (trailing
slash) routing, `X-Powered-By`, `ETag`, `charset=utf-8` on `Content-Type`.
Malformed or non-object JSON bodies get Express's HTML 400 but a JSON 400 from
the crate; bodies above Express's 100 kB limit differ (413 versus 400); ids
outside the primary key's integer range and values longer than a `varchar(n)`
column fail differently. Replay compares only the captured scenarios and the
captured tables; it is declared coverage, not certification.

## Acceptance

The workspace plan in `sanka-project/plan/sanka-code-paths/typescript-to-rust.md`
tracks milestones; the extension's own CI job runs the gated cargo, Node.js and
PostgreSQL suites on every change. A Sanka Migration Bench lane (`express-axum`)
and the hosted `express-to-axum` recipe are planned but not shipped.
