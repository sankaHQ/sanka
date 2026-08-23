# SPDX-License-Identifier: Apache-2.0
# Django REST Framework to FastAPI compatibility migration

Sanka's first application-migration recipe creates a verified transition from
Django REST Framework (DRF) to FastAPI. Version 0.1 is intentionally
compatibility-first: it gives the target a real FastAPI route graph while
preserving the existing Django behavior behind that graph.

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

The plan classifies every discovered route, records the compatibility strategy,
lists retained components and manual adaptations, and binds the result to the
exact scan hash. The canonical artifact is `.sanka/plan-fastapi.json`.

`sanka apply` verifies the current scan and canonical plan hashes. For an
approval workflow or CI gate, pass the exact reviewed hash explicitly with
`sanka apply --plan-hash sha256:...`.

Planning never modifies application source or destination code.

### `sanka apply`

Apply requires the reviewed plan hash when supplied and refuses stale or
modified plans. It writes a standalone generated application to
`.sanka/output/fastapi` by default and refuses to overwrite a non-empty output
without `--force`.

The generated application contains:

| File | Purpose |
|---|---|
| `app.py` | FastAPI ASGI entrypoint |
| `sanka_compat.py` | In-process Django compatibility dispatcher |
| `sanka-manifest.json` | Exact scan hash, plan hash, routes, and source reference |
| `requirements.txt` | Target server dependencies |
| `README.md` | Run and incremental-replacement guidance |

Source files are never overwritten. Teams can replace one compatibility route
at a time with native FastAPI code and rerun verification after every change.

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

## Version 0.1 compatibility boundary

### Retained deliberately

- Django models and migrations;
- Django ORM;
- synchronous `transaction.atomic` handlers (Django does not currently support
  transactions in async mode);
- Django authentication and permissions;
- Celery and other existing background workers;
- DRF handlers behind the generated bridge, until replaced route by route.

### Detected and bridged automatically

- `APIView` methods;
- DRF `ViewSet` and `ModelViewSet` router routes;
- standard CRUD actions;
- `@action` routes;
- Django path converters and standard DRF named-regex parameters.

### Not claimed by version 0.1

- automatic removal of the DRF dependency;
- automatic conversion of arbitrary serializer/business logic to Pydantic;
- Django templates, Admin, Channels, GraphQL, or ORM replacement;
- semantic verification of mutating or parameterized requests without fixtures;
- compatibility for arbitrary custom regex URL patterns.

Unsupported route patterns stay in the plan as explicit adaptation risks. Sanka
does not silently call a partial generation complete.

## Launch acceptance gate

Do not publish a numerical compatibility or time-saved claim until a pinned
public reference repository proves it. The launch packet must record:

- source repository commit;
- Python, Django, DRF, FastAPI, and Sanka versions;
- scan and plan hashes;
- routes discovered, generated, automatically probed, and fixture-probed;
- every skipped or manually adapted route;
- clean-checkout commands and elapsed time.

"In hours, not months" becomes a measured claim only after that benchmark.
