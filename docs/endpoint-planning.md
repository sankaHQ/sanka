# Endpoint selection

Backend extensions can report `sanka.endpoint-scope/v1` during planning. Supported
candidates are DRF to Flask, DRF to FastAPI, Python to Go (Fiber, chi, mux, Gin),
and TypeScript to Rust. This is HTTP route selection, not data endpoint selection.

Commands use CLI by default. First run Plan with the required configuration to
see the captured endpoint IDs, counts and reviewed hash. For example:

```bash
sanka plan . --to python-fastapi --generation full --strategy native --package-manager uv --output fastapi-app
sanka plan . --to python-fastapi --endpoint 'GET /api/gadgets/' --endpoint 'POST /api/gadgets/'
sanka apply --root . --plan-hash sha256:<hash printed by the selected plan>
sanka test .
sanka verify .
```

Use exact IDs printed by your extension. Flask may report regex-shaped IDs;
do not substitute a browser URL. `--all-endpoints` selects all supported IDs.
It cannot be combined with `--endpoint` or explicit `selected_endpoints` JSON.
Each fresh convenience selection replaces prior optional choices and includes
receipt/file-checked Generated endpoints. Review the new hash before applying.
CLI output lists selected, retained and omitted counts, endpoint states, and the
exact next Apply command. Generated never implies Verified.

## Optional TUI

Use `sanka tui` or `sanka plan . --to python-fastapi --tui`. Run Plan to capture the inventory, choose **Endpoints**, then review the new plan.
The picker supports individual checkboxes, method/path filtering, Select all and
Deselect all. Bulk actions include hidden rows. Generated rows stay checked and
locked; manual gaps are disabled. An empty selection cannot be applied.

The whole-project default is unchanged until the user chooses a subset. An older
extension that ignores an explicit selection is rejected before apply. After
changing scope, target or output, review a new plan before applying. A saved
selection is restored on restart; run Plan again to refresh ownership status.

## Generated is different from verified

An endpoint is labelled **Generated** only after successful apply and matching
hashes for the receipt's owned files. A directory, scan or old plan is insufficient.
Modified or missing generated files block automatic regeneration and are preserved.
Unowned files are preserved when adding endpoints to the same output directory.
Previously generated endpoints cannot be removed through the picker.

Verification remains a separate lifecycle stage, with its own reported scenarios
and limitations. The Generated label never claims parity or migration readiness.
Plan and subsequent stage summaries show selected, retained and omitted counts.

## CLI contract

Pass `selected_endpoints` in `--extension-config`, using IDs returned in the plan:

```json
{"selected_endpoints": ["GET /api/gadgets/", "POST /api/gadgets/"]}
```

IDs are opaque extension values. Do not rewrite parameters or trailing slashes.
With no saved selection, absence selects all. A valid saved plan restores its
configuration in either interface; explicit values win. An explicit empty list, duplicate, unknown ID or removal of
a retained endpoint is rejected. Apply, test and verify cannot change the reviewed
selection. Explicit scenario replay is also review-bound for scoped plans.

Shared installed declarations provide defaults and required-input hints. Common
DRF settings have flags; every declared field can be supplied in the existing
`--extension-config` JSON object. For Go, for example:

```bash
sanka plan . --to go-fiber --extension-config '{"source_framework":"flask","source_file":"app.py","database_layer":"none"}' --endpoint 'GET /health'
```

The CLI asks for missing required inputs only on an interactive terminal. Pipes
and JSON runs fail promptly with the missing keys. Apply, Test and Verify use the
reviewed settings; they cannot silently change scope. Changing target or output
requires a new plan. Forward only required environment names with repeated
`--extension-env NAME` on each CLI stage or when starting an optional TUI session.

Required authentication, serializers, models and database schema remain available
even when fewer HTTP routes are generated. Shared capture/security failures remain
blocking. Framework-provided HEAD/OPTIONS behavior remains part of its normal
HTTP semantics; these are not independent business-handler migrations.

## Deliberate limits

- Incremental generation adds endpoints from the same captured contract. Changes
  to retained handlers or shared dependencies require reconciliation or a new
  output directory. Shared dependency checks are conservative.
- Tests and explicit scenario replay retain their existing scope. No scenario or
  prerequisite is silently skipped because an endpoint was deselected. Supply
  scenarios for the selected surface; a passing subset does not prove the app.
- An edited generated candidate can still be verified with the extension's repair
  workflow, but it cannot be automatically overwritten or labelled generated from
  an outdated receipt.
- Extensions without a scope inventory continue their existing whole-project
  workflow. Business Flow, data migration and non-HTTP extensions have no picker.

The CLI and matching extension candidates must both be installed. Source checks
alone do not establish that these versions have been publicly released.
