# SPDX-License-Identifier: Apache-2.0
# Django REST Framework to FastAPI migration

Sanka's first application-migration recipe creates a verified transition from
Django REST Framework (DRF) to FastAPI. Two strategies share one scan:

- **`native` (default)** generates a genuinely native **async** FastAPI
  request layer for the supported envelope. Persistence is async SQL against
  the existing Django tables (Tortoise ORM by default — closest to Django;
  SQLAlchemy 2.0 or psycopg3 on request). Django is not imported at serve
  time. Routes outside the envelope are reported as needing manual
  adaptation, never silently bridged.
- **`compatibility`** generates the strangler bridge: a real FastAPI route
  graph that dispatches every route into the existing Django application
  in-process. It preserves behavior for the whole surface but still serves
  through DRF, so it can never support the claim that DRF was replaced.

## The five-command contract

Run these commands from the Django repository root and from the project's
existing Python 3.12+ environment:

```bash
python -m pip install sanka-cli sanka-migrate

sanka scan
sanka plan --to fastapi
sanka apply --plan-hash sha256:<hash-from-plan>  # exact hash printed by plan
sanka test
sanka verify
```

The lightweight `sanka-cli` distribution owns the `sanka` console command. The
`sanka-migrate` distribution owns the local engine and its explicit
`sanka-migrate` command; after both packages are installed, `sanka` delegates
these local lifecycle verbs to the engine.

## What each command proves

### `sanka scan`

Sanka boots Django with the project's `DJANGO_SETTINGS_MODULE` and inspects the
resolved URL graph. Runtime resolution matters because DRF routers and
`@action` decorators create routes that a text search cannot reliably recover.
Because Django startup imports installed applications and runs
`AppConfig.ready()`, scan should be run in the same reviewed development or CI
environment used for the project's ordinary management commands. Sanka itself
writes only `.sanka/scan.json`, but it cannot make application startup hooks
side-effect-free.

The scan records:

- Python, Django, and DRF versions;
- database vendor and name (never a password);
- HTTP methods and normalized paths;
- view classes and actions;
- serializer, model, authentication, and permission classes when declared;
- configured Django middleware and structured native-adaptation reasons per
  route (code, feature, and explanation);
- synchronous transaction markers visible on the view class;
- source locations, test-file count, and unsupported dynamic route patterns.

The canonical artifact is `.sanka/scan.json`. `sanka scan --json` prints the
same machine-readable application IR for agents and other tools.

### `sanka plan --to fastapi`

The plan classifies every discovered route, records the selected strategy
(`--strategy native` is the default; `--strategy compatibility` selects the
bridge), records the async SQL engine (`--orm tortoise` is the default and
recommended; `sqlalchemy` and `psycopg` are the other choices), lists
retained components and manual adaptations, and binds the result to the
exact scan hash. The canonical artifact is `.sanka/plan-fastapi.json`.

In native mode every route receives one of four dispositions:

- `native-fastapi-crud` — default-behavior `ModelViewSet` CRUD over a
  `ModelSerializer` whose field semantics the scan captured (including the
  exact DRF error strings, rendered live), optionally guarded by DRF
  `TokenAuthentication` + `IsAuthenticated`, one structurally recognized
  owner-or-read-only object permission, and the
  `serializer.save(field=self.request.user)` perform_create idiom; writable
  `many=True` nested child serializers are supported when the author's
  `create()` is the recognized nested-write shape (the generated app inserts
  the parent row and children through async SQL on the existing tables) and
  `update()` matches the drop-children idiom;
- `native-fastapi-api-root` — the router API root, regenerated from the
  captured link table;
- `dropped-format-suffix-alias` — DRF's `.{format}` alias routes are dropped
  as a disclosed contract change; clients negotiate content types with
  headers instead;
- `needs-manual-adaptation` — everything else (APIViews, custom actions,
  non-token authentication, permission logic beyond the recognized owner
  idiom, pagination, filters, other overridden viewset methods, or middleware
  outside the known-safe allowlist). Every such route includes
  `adaptation_reasons` in scan and plan JSON. Unsupported middleware does not
  mask the route-specific reason discovered by the rest of the native-envelope
  checks. The terminal groups the most common reason codes instead of returning
  a silent zero. Verification fails while these remain.

The exact middleware allowlist covers Django's `SecurityMiddleware`,
`SessionMiddleware`, `CommonMiddleware`, `CsrfViewMiddleware`,
`AuthenticationMiddleware`, `MessageMiddleware`, and `XFrameOptionsMiddleware`.
WhiteNoise, CORS, project-specific subclasses, and similarly named middleware
remain outside the allowlist because native output cannot reproduce their
settings faithfully; matching is by the full import path.

Native migration readiness is the number of `native-fastapi-crud` and
`native-fastapi-api-root` routes divided by scanned routes after excluding
format-suffix aliases from the denominator. A
`dropped-format-suffix-alias` is a disclosed removal, not generated code, so it
does not receive native automation credit. Alias drops are reported separately
as a count and share of scanned routes. Compatibility readiness continues to
measure routes emitted by the bridge.

`sanka apply` verifies the current scan and canonical plan hashes and always
requires the exact reviewed hash with `--plan-hash`.

Planning never modifies application source or destination code.

### `sanka apply`

Apply requires the reviewed plan hash and refuses stale or modified plans. It
writes a standalone generated application to
`.sanka/output/fastapi` by default and refuses to overwrite a non-empty output
without `--force`.

The generated native application contains:

| File | Purpose |
|---|---|
| `app.py` | Async FastAPI ASGI entrypoint with `@app.get` / `@app.post` routes |
| `sanka_native.py` | DRF-parity validation used by those routes |
| `sanka_store.py` | Async SQL (Tortoise, SQLAlchemy, or psycopg) over the existing tables |
| `models.py` | Tortoise or SQLAlchemy models mapped onto Django `db_table` names (not emitted for psycopg) |
| `sanka-manifest.json` | Exact scan hash, plan hash, routes, captured field semantics, database vendor, SQL engine, dropped aliases |
| `requirements.txt` | Target server dependencies (FastAPI + the chosen SQL engine) |
| `README.md` | Run guidance |

`sanka apply` prompts for the SQL engine when stdin is a TTY and `--orm` was
not passed. Non-interactive runs use the engine recorded in the plan
(Tortoise unless `sanka plan --to fastapi --orm …` chose otherwise).
`psycopg` is refused unless scan captured a PostgreSQL database.

Generated Tortoise applications require Tortoise ORM 1.1 or later. Their
lifespan initialization enables Tortoise's ASGI global-context fallback so
request handlers keep the initialized context even when the ASGI server runs
lifespan and requests in different tasks.

In compatibility mode the native SQL files are replaced by `sanka_compat.py`,
the in-process Django dispatcher. Requests and responses are forwarded as ASGI
streams rather than buffered in memory.

Native output validates configured Django `ALLOWED_HOSTS`, reproduces the
captured SecurityMiddleware/X-Frame header subset, and reads JSON bodies only
after authentication through a streaming size cap. The default cap is 1 MiB;
set `SANKA_MAX_REQUEST_BODY_BYTES` in the generated application's environment
to choose another positive byte limit.

Source files are never overwritten. `sanka apply --plan-hash <hash> --bench-candidate <dir>`
additionally emits a Sanka Migration Bench candidate (overlay plus
`candidate.yaml`) from the reviewed native plan. The benchmark contract keeps
Django for ORM access, so this projection uses generated DRF-free Django
settings and the retained Django ORM while normal `sanka apply` output keeps
the selected async SQL engine. This lets the tool-neutral benchmark grade the
same generated FastAPI route contract in its fixed fixture environment.

Native apply is readiness-aware. By default, a plan below 50% native readiness
does not produce a partial application: Sanka writes `GAP-REPORT.md`, the
reviewed `plan-fastapi.json`, and a structured `gap-report.json` instead. The
report enumerates unsupported and unscanned URL patterns plus the route,
redirect/header, native-serving, and database-parity checks that remain. Use
`--min-readiness PCT` to raise the gate. Passing `--min-readiness 0` is the
explicit opt-in for a partial scaffold; `--gap-report-only` always abstains.

A generated benchmark candidate also carries the gap artifacts and a
`verify-report.json`. These diagnostics never turn partial success into a
completed migration: `sanka verify` and Sanka Migration Bench still require
every route and hard gate.

### `sanka test`

After apply, Sanka writes `test_generated.py` beside the FastAPI app and runs
it with `python -m unittest`. The tests import the generated app through
FastAPI's `TestClient` (lifespan included) and cover:

- OpenAPI is served and lists every generated route;
- native API roots return JSON objects;
- native list / missing-pk / empty-create status codes (401 when the view
  requires a token, otherwise 200 / 404 / 400);
- a create → retrieve → delete round-trip when the source database is SQLite
  (run against an isolated copy, so the developer's file is not mutated).

`sanka test` is the generated API's unit suite. It does not compare FastAPI to
DRF; that remains `sanka verify`.

### `sanka verify`

The default verifier checks four layers:

1. the scan artifact still matches its canonical hash;
2. the plan still matches the scan and its reviewed hash;
3. the generated manifest contains exactly the automatically planned routes;
4. generated Python compiles and native output does not import Django; parameter-free GET/HEAD routes return the
same status, content type, and body through DRF and FastAPI. The generated
app must be able to import its SQL engine (`pip install -r requirements.txt`).

Add concrete parameterized read cases in `.sanka/verify-cases.json`:

```json
{
  "cases": [
    {
      "method": "GET",
      "path": "/api/customers/42/",
      "headers": {"Authorization": "Bearer test-fixture-token"}
    }
  ]
}
```

Sanka compares JSON structurally and also checks status, content type, `Allow`,
`Location`, and `WWW-Authenticate` behavior. Cases accept only GET, HEAD, and
OPTIONS; Sanka refuses to replay writes automatically against a developer's
database.

Automatic HTTP probes are deliberately read-only. Parameterized and mutating
requests require project-specific fixtures before they can be claimed as
behaviorally verified. `sanka verify --no-http` runs integrity and route checks
only.

## Strategy boundaries

### Retained deliberately (both strategies)

- Django **tables** produced by the source project's models and migrations
  (native serving reuses them; it does not rewrite schema);
- Celery and other existing background workers.

Compatibility mode additionally retains the Django ORM, authentication,
permissions, and DRF handlers behind the bridge.

### Generated natively (native strategy)

- default-behavior `ModelViewSet` CRUD routes over `ModelSerializer` fields
  with captured semantics (integer, char, decimal, choice, unique, read-only
  primary-key relation, and writable `many=True` nested child fields:
  required, null, blank, trim, min/max bounds, digit/precision limits,
  choices, defaults, and the exact rendered DRF error strings — including
  DRF's index-keyed nested error format, validation, 404, JSON parse, and
  `Allow` header behavior);
- **async handlers** and an async SQL store (Tortoise recommended, or
  SQLAlchemy / psycopg) mapped onto captured `db_table` / column names;
- **nested writes** regenerated as parent-plus-children SQL when the author's
  `create()` only names the application's models, `django.db.transaction`, or
  `serializers.ValidationError`. Extra business rules inside `create()` are
  not translated. Overridden `update()` must match the drop-children idiom
  (`validated_data.pop("<child>")` then `super().update(...)`);
- DRF `TokenAuthentication` with `IsAuthenticated`: the serving process reads
  the retained token table directly (raw quoted-identifier lookup, no DRF
  import), reproducing the 401 variants, `WWW-Authenticate`, and
  inactive-user behavior with strings probed from the live installation;
- owner-or-read-only object permissions, recognized structurally from the
  permission class's AST (the canonical safe-methods short-circuit plus an
  ownership comparison) — arbitrary permission logic is never guessed;
- `perform_create` author injection matched from the
  `serializer.save(field=self.request.user)` idiom;
- the router API root.

The source DRF application and its test suite stay intact in the repository;
only the serving process stops loading Django and DRF. Routes outside the
envelope fail verification until a human adapts them.

### Detected and bridged automatically (compatibility strategy)

- `APIView` methods;
- DRF `ViewSet` and `ModelViewSet` router routes;
- standard CRUD actions;
- `@action` routes;
- Django path converters and standard DRF named-regex parameters.

### Not claimed yet

- native generation for session or custom authentication, permission logic
  beyond the recognized owner idiom, pagination, filters, custom actions,
  middleware outside the exact known-safe allowlist (project-specific global
  behavior is not silently discarded),
  write logic whose free names reach beyond models/transaction/ValidationError,
  or writable non-nested relation fields;
- automatic conversion of arbitrary serializer/business logic to Pydantic;
- Django templates, Admin, Channels, GraphQL, or translating arbitrary
  `create()` business rules beyond nested parent/child inserts;
- semantic verification of mutating or parameterized requests without
  fixtures;
- compatibility for arbitrary custom regex URL patterns.

Unsupported route patterns stay in the plan as explicit adaptation risks. Sanka
does not silently call a partial generation complete.

## Launch acceptance gate

The tool-neutral acceptance suite is Sanka Migration Bench
(`sankaHQ/sanka-bench`). Its native-target gate is decided by recorded serving
evidence from a guarded process, so only output whose serving path genuinely
excludes DRF can pass; the compatibility bridge is pinned there as a
permanent negative control. `sanka apply --plan-hash <hash> --bench-candidate`
produces the
candidate the benchmark grades.

Do not publish a numerical compatibility or time-saved claim until a pinned
public reference repository proves it. The launch packet must record:

- source repository commit;
- Python, Django, DRF, FastAPI, and Sanka versions;
- scan and plan hashes;
- routes discovered, generated, automatically probed, and fixture-probed;
- every skipped or manually adapted route;
- clean-checkout commands and elapsed time.

"In hours, not months" becomes a measured claim only after that benchmark.
