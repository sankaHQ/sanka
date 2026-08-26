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
existing Python environment:

```bash
python -m pip install --pre sanka-migrate

sanka scan
sanka plan --to fastapi
sanka apply            # prompts for Tortoise / SQLAlchemy / psycopg when interactive
sanka test
sanka verify
```

The distribution name is `sanka-migrate`; the primary console command is
`sanka`. The older `sanka-migrate` console command remains an alias so existing
automation does not break.

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
  idiom, pagination, filters, other overridden viewset methods).
  Verification fails while these remain.

`sanka apply` verifies the current scan and canonical plan hashes. For an
approval workflow or CI gate, pass the exact reviewed hash explicitly with
`sanka apply --plan-hash sha256:...`.

Planning never modifies application source or destination code.

### `sanka apply`

Apply requires the reviewed plan hash when supplied and refuses stale or
modified plans. It writes a standalone generated application to
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

In compatibility mode the native SQL files are replaced by `sanka_compat.py`,
the in-process Django dispatcher.

Source files are never overwritten. `sanka apply --bench-candidate <dir>`
additionally emits a Sanka Migration Bench candidate (overlay plus
`candidate.yaml`) from the reviewed native plan, so the tool-neutral benchmark
can grade the exact generated output.

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
permanent negative control. `sanka apply --bench-candidate` produces the
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
