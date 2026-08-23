# SPDX-License-Identifier: Apache-2.0
# Django REST Framework to FastAPI migration

Sanka's first application-migration recipe creates a verified transition from
Django REST Framework (DRF) to FastAPI. Two strategies share one scan:

- **`native` (default)** generates a genuinely native FastAPI request layer
  for the supported envelope. The generated serving process keeps Django for
  the ORM only — DRF never loads into it. Routes outside the envelope are
  reported as needing manual adaptation, never silently bridged.
- **`compatibility`** generates the strangler bridge: a real FastAPI route
  graph that dispatches every route into the existing Django application
  in-process. It preserves behavior for the whole surface but still serves
  through DRF, so it can never support the claim that DRF was replaced.

## The four-command contract

Run these commands from the Django repository root and from the project's
existing Python environment:

```bash
python -m pip install --pre sanka-migrate

sanka scan
sanka plan --to fastapi
sanka apply
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
bridge), lists retained components and manual adaptations, and binds the
result to the exact scan hash. The canonical artifact is
`.sanka/plan-fastapi.json`.

In native mode every route receives one of four dispositions:

- `native-fastapi-crud` — default-behavior `ModelViewSet` CRUD over a
  `ModelSerializer` whose field semantics the scan captured (including the
  exact DRF error strings, rendered live), optionally guarded by DRF
  `TokenAuthentication` + `IsAuthenticated`, one structurally recognized
  owner-or-read-only object permission, and the
  `serializer.save(field=self.request.user)` perform_create idiom;
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
| `app.py` | FastAPI ASGI entrypoint |
| `sanka_native.py` | Native request layer: FastAPI routes, DRF-parity validation, Django ORM access |
| `sanka_settings.py` | Serving settings — the original settings with the DRF request layer removed |
| `sanka-manifest.json` | Exact scan hash, plan hash, routes, captured field semantics, dropped aliases |
| `requirements.txt` | Target server dependencies |
| `README.md` | Run guidance |

In compatibility mode `sanka_native.py` and `sanka_settings.py` are replaced
by `sanka_compat.py`, the in-process Django dispatcher.

Source files are never overwritten. `sanka apply --bench-candidate <dir>`
additionally emits a Sanka Migration Bench candidate (overlay plus
`candidate.yaml`) from the reviewed native plan, so the tool-neutral benchmark
can grade the exact generated output.

### `sanka verify`

The default verifier checks four layers:

1. the scan artifact still matches its canonical hash;
2. the plan still matches the scan and its reviewed hash;
3. the generated manifest contains exactly the automatically planned routes;
4. generated Python compiles, and parameter-free GET/HEAD routes return the
same status, content type, and body through DRF and FastAPI.

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

- Django models and migrations;
- Django ORM;
- synchronous `transaction.atomic` handlers (Django does not currently support
  transactions in async mode);
- Celery and other existing background workers.

### Generated natively (native strategy)

- default-behavior `ModelViewSet` CRUD routes over `ModelSerializer` fields
  with captured semantics (integer, char, and read-only primary-key relation
  fields today: required, null, blank, trim, min/max bounds, defaults, and
  the exact rendered DRF error strings, including validation, 404, JSON
  parse, and `Allow` header behavior);
- DRF `TokenAuthentication` with `IsAuthenticated`: the serving process reads
  the retained token table directly (raw quoted-identifier lookup, no DRF
  import), reproducing the 401 variants, `WWW-Authenticate`, and
  inactive-user behavior with strings probed from the live installation;
- owner-or-read-only object permissions, recognized structurally from the
  permission class's AST (the canonical safe-methods short-circuit plus an
  ownership comparison) — arbitrary permission logic is never guessed;
- `perform_create` author injection matched from the
  `serializer.save(field=self.request.user)` idiom;
- the router API root;
- generated handlers are synchronous so the retained ORM runs in FastAPI's
  worker threads, never on the event loop.

The source DRF application and its test suite stay intact in the repository;
only the serving process stops loading DRF. Routes outside the envelope fail
verification until a human adapts them.

### Detected and bridged automatically (compatibility strategy)

- `APIView` methods;
- DRF `ViewSet` and `ModelViewSet` router routes;
- standard CRUD actions;
- `@action` routes;
- Django path converters and standard DRF named-regex parameters.

### Not claimed yet

- native generation for session or custom authentication, permission logic
  beyond the recognized owner idiom, pagination, filters, custom actions,
  nested serializers, or writable relation fields;
- automatic conversion of arbitrary serializer/business logic to Pydantic;
- Django templates, Admin, Channels, GraphQL, or ORM replacement;
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
