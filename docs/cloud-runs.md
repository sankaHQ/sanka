# Hosted repository runs

`sanka cloud` is a client for the optional Sanka Developer cloud service. It uses
your existing `sanka login` profile and requires an exact workspace code on every
command. The service must be enabled by the workspace operator. Local `scan`,
`plan`, `apply`, `test`, and `verify` behavior is unchanged.

The initial recipe is DRF to FastAPI using a pinned, offline Python environment.
An ordinary run executes generated tests and static checks. Independent HTTP
certification is a separate, explicitly requested run. Neither profile installs
arbitrary project dependencies.
Database-backed route tests need a synthetic or sanitized SQLite test database
in the ZIP and Django settings that reference it. An empty in-memory database
has no tables for those tests. Remote databases are unreachable; omit their
credentials from the archive.

1. Prepare and review a ZIP containing the repository files you intend to upload.
   For tracked files at a specific commit, `git archive --format=zip HEAD -o source.zip`
   is one option. ZIPs are limited to 8 MiB compressed and 64 MiB expanded. Source,
   logs, and output are retained for seven days.
2. Run `sanka cloud upload source.zip --workspace WORKSPACE_CODE`. Confirm the
   displayed workspace, filename, size, and digest. This uploads source without
   starting paid compute. Save the returned source ID and SHA-256.
3. Run the command below with that exact source ID and digest. Confirm the credit
   limit, or use `--yes` for an already reviewed automation request.

```sh
sanka cloud run --workspace WORKSPACE_CODE \
  --source-id SOURCE_UUID --sha256 SOURCE_SHA256 \
  --max-credits 1000 --timeout-seconds 600 \
  --idempotency-key my-first-reviewed-cloud-run
```

The workspace wallet reserves the full limit before queueing. Active compute
costs 100 credits per worker-minute on one standard 2 CPU / 4 GiB worker; fractional
usage rounds up once to a whole credit. Queueing is free. Unused credits are
released, customer-code failure bills elapsed compute, and infrastructure failure
is free for the affected attempt. The worker stops at the earlier credit or time
limit. Insufficient spendable credit returns an error before queueing.

If a create request has an uncertain network result, repeat it with the **same
source ID, digest, options, and idempotency key**. Do not upload again for that
retry. A new intentional run uses a new key; a changed request with a previously
used key is rejected. Status polling does not start new runs.

```sh
sanka cloud list --workspace WORKSPACE_CODE
sanka cloud status RUN_UUID --workspace WORKSPACE_CODE
sanka cloud events RUN_UUID --workspace WORKSPACE_CODE
sanka cloud cancel RUN_UUID --workspace WORKSPACE_CODE
sanka cloud receipt RUN_UUID --workspace WORKSPACE_CODE
sanka cloud download RUN_UUID --workspace WORKSPACE_CODE --to output.zip
sanka cloud download RUN_UUID --workspace WORKSPACE_CODE --artifact logs.txt --to logs.txt
```

`events` returns a cursor for the next page. Logs and output become available on
completion. Downloads verify the size and SHA-256 before writing a new file and
never replace an existing path. Receipts show reserved, charged, and released
credits separately, together with the immutable input, image, extension, and
verification scope. Cancelling a queued run releases the full hold; cancelling
an active run can still charge its elapsed compute.

Developer API tokens need `migrate:cloud:read` and `migrate:cloud:write` scopes;
resource-limited ingestion tokens cannot start Cloud Runs. The API rejects a
workspace code that differs from the token's workspace.

## Repair a failed candidate

When Repair is enabled, select the exact output digest from a failed run and the
application files that may change. One attempt uses the configured model and the
fixed generated-test/static-verification profile. It cannot edit tests or
configuration, add dependencies, or reach external networks.

```sh
sanka cloud repair FAILED_RUN_UUID --workspace WORKSPACE_CODE \
  --candidate-sha256 OUTPUT_SHA256 --target-gate test \
  --path app/main.py --max-credits 2000 --timeout-seconds 600 \
  --idempotency-key my-reviewed-repair
```

Repeat `--path` to allow up to 20 existing Python files under `app/` (32 KiB per
file). The command reads the failed run and its candidate before asking you to
confirm the hold. The cap is 1,001–7,000 credits and includes a 1,000-credit
success premium; only the remaining cap funds compute. Failed or cancelled repairs
have no premium. Reuse the same options and key after an uncertain response.

Use the returned repair run ID with the same `status`, `events`, `cancel`,
`receipt`, and `download` commands. The output ZIP includes `repair.patch` and
`repair-result.json` under `artifacts/`, with the failing check before repair and
the same checks afterward. `--artifact repair-response.json` downloads the saved
model patch. Passing generated tests and static checks is not an independent
behavior-parity certificate.

## Certify a selected HTTP scope

When certification is enabled, use an existing run's retained candidate and its
original source. The original run must specify a Django settings module and a
synthetic or sanitized SQLite database in the source archive. Review a JSON array
of 1–50 requests, with unique IDs and local paths, and save it as `cases.json`:

```json
[
  {"id": "list-items", "method": "GET", "path": "/api/items/"},
  {"id": "create-item", "method": "POST", "path": "/api/items/", "json_body": {"name": "Example"}}
]
```

Use paths and request bodies from your application. Requests run in order against
the source and candidate, each with its own fresh database copy, in an isolated
worker. Generated checks must pass, HTTP responses must match, no case may return
a server error, and at least one case must succeed. The certificate records tested
and untested declared routes; it does not establish behavior outside those cases.

```sh
sanka cloud certify RUN_UUID --workspace WORKSPACE_CODE \
  --candidate-sha256 OUTPUT_SHA256 --cases cases.json \
  --max-credits 3000 --timeout-seconds 600 \
  --idempotency-key my-reviewed-certification
sanka cloud certificate CERTIFICATION_RUN_UUID --workspace WORKSPACE_CODE --to certificate.json
sanka cloud certificate-verify certificate.json --workspace WORKSPACE_CODE
```

The cap is 2,001–8,000 credits, including a 2,000-credit fee only when the signed
certificate is issued. The remaining cap funds compute at the standard rate.
Failed or cancelled verification has no certificate fee. Use the same reviewed
inputs and idempotency key after an uncertain create response. Normal run status,
cancellation, events, receipts, and artifact downloads also apply.

Certificates retain their signed evidence after source artifacts expire. Online
verification uses the service's published Ed25519 key ring and checks the current
certificate and revocation state in the specified workspace. For offline signature
verification, supply an independently trusted issuer key ring:

```sh
sanka cloud certificate-verify certificate.json --trusted-keys trusted-issuer-keys.json
sanka cloud certificate-revoke CERTIFICATION_RUN_UUID --workspace WORKSPACE_CODE \
  --reason "The covered behavior is no longer approved"
```

The key ring has a `keys` array whose entries contain `key_id`, `algorithm`
(`Ed25519`), and `public_key_base64`; obtain it from the authenticated
`/api/v2/migrate/cloud-runs/certificate-keys` endpoint. Do not trust a key merely
because it accompanies a certificate. Offline verification reports revocation
as `not_checked_offline`. Revocation is permanent and preserves the original
signature and evidence.

## Run a selected repository Fleet

When Fleet is enabled, prepare an exact ZIP and full commit revision for each
repository. Upload each ZIP once with its revision:

```sh
git archive --format=zip COMMIT_SHA -o source.zip
sanka cloud upload source.zip --workspace WORKSPACE_CODE --revision COMMIT_SHA
```

The service binds your declared revision to the immutable ZIP digest. It does not
independently verify that the ZIP represents that Git commit. Use the returned
source ID and SHA-256 in a reviewed JSON array, saved as `fleet-items.json`:

```json
[
  {
    "key": "orders",
    "repository": "team/orders",
    "revision": "1111111111111111111111111111111111111111",
    "request": {
      "source_id": "00000000-0000-4000-8000-000000000001",
      "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "max_credits": 1000,
      "timeout_seconds": 600,
      "settings_module": "config.settings"
    }
  }
]
```

Replace these example identities with your uploaded source. Select 1–20
repositories, each with a unique repository name and item key. Revision values
must have 40 or 64 lowercase hexadecimal characters. A child `request` accepts
the same compute, Repair or certification fields as a single Cloud Run, including
explicit candidate digests and approved file or HTTP scopes. Source and candidate
retention must cover the entire Fleet execution window.

```sh
sanka cloud fleet create --workspace WORKSPACE_CODE --manifest fleet-items.json \
  --max-credits 1000 --concurrency 2 --idempotency-key my-reviewed-fleet
sanka cloud fleet list --workspace WORKSPACE_CODE
sanka cloud fleet status FLEET_UUID --workspace WORKSPACE_CODE
sanka cloud fleet cancel FLEET_UUID --workspace WORKSPACE_CODE
```

The total cap must equal the sum of all child caps. The service reserves all
children in one transaction before any child becomes eligible to run. Insufficient
credits rejects the whole Fleet with no child reservations. The parent has no
additional execution charge. Each child retains its ordinary compute rate,
success or issuance fee, credit limit, cancellation behavior and separate receipt.
The concurrency limit is 1–5 children; the workspace also has a shared five-run
capacity across all Fleets and ordinary runs. Children waiting for capacity are
free. Their queue timeout starts when they receive a slot.

`status` reports partial progress and per-repository run IDs, revisions and
receipts. It separates the initial reservation, charged compute, success or
issuance credits, released credits and still-reserved credits. Use a child run ID
with the ordinary `status`, `events`, `receipt`, `certificate` and `download`
commands. Cancellation releases queued children and requests cancellation of
active children; completed results and receipts remain unchanged.

After every child has settled, retry only the failed keys you explicitly select:

```sh
sanka cloud fleet retry FLEET_UUID --workspace WORKSPACE_CODE --item orders \
  --max-credits 1000 --concurrency 1 --idempotency-key my-reviewed-fleet-retry
```

Repeat `--item` for more failures. Confirm a new total cap equal to those selected
child caps. The new Fleet reuses their selected source and run request and links to
their original run IDs. Successful and cancelled children cannot be retried by
this command. Original receipts stay unchanged. For an uncertain create or retry
response, repeat the identical request with the same idempotency key; a new
intentional attempt requires a new key. The console provides the same repository
review, aggregate limits, partial receipts, cancellation and selective retry flow.


## Code migrations: Scan → Plan → Apply → Test → Verify

Code stages use the same workspace-scoped operations as Sanka Code. Authenticate
with `sanka auth login` using a token for the intended workspace. Named-scope tokens
need `migrate:cloud:read` and `migrate:cloud:write`; existing cloud access and workspace
permissions still apply. Always pass the eight-digit workspace code explicitly.

```sh
# Directory (defaults to .); choose fastapi or flask.
sanka scan --cloud . --workspace 10483816 --to flask \
  --max-credits 1000 --idempotency-key project-scan-001 --yes --wait

# Inspect the returned preparation run and review its plan/hash.
sanka plan --cloud --workspace 10483816 --run PREPARE_RUN_ID

# Substitute the exact reviewed plan hash. This starts conversion.
sanka apply --cloud --workspace 10483816 --run PREPARE_RUN_ID \
  --plan-hash PLAN_SHA256 --max-credits 1000 \
  --idempotency-key project-apply-001 --yes --wait

sanka test --cloud --workspace 10483816 --run EXECUTION_RUN_ID
sanka verify --cloud --workspace 10483816 --run EXECUTION_RUN_ID
```

`scan` runs **Scan + Plan** together and stops for approval. `apply` runs
**Apply + Test + deterministic Verify** together against that approved source and
plan. `plan`, `test`, and `verify` read the corresponding operation's evidence;
they never start another paid operation. Without `--cloud`, local behavior is
unchanged. Existing data migration commands with `--program` are separate and
cannot be combined with these Code flags. Sanka Fix remains an optional follow-up
through `sanka fix --cloud --run EXECUTION_RUN_ID` with its own consent and budget.

### Source selection

Replace `.` in the scan command with a ZIP path, or use one of:

```sh
sanka cloud github connect --workspace 10483816 --open
sanka cloud github repositories --workspace 10483816
sanka scan --cloud --github hira29/sanka-code-drf-test --ref main \
  --workspace 10483816 --to fastapi --max-credits 1000 \
  --idempotency-key github-scan-001 --yes --wait
```

Connect GitHub in Sanka Code as the **same user who owns the CLI token**. Grant the
Sanka Code GitHub App access to the repository. No GitHub client secret or personal
access token belongs in CLI configuration. Imports resolve the selected branch
and pin its commit; subsequent branch changes cannot silently change a retry.
The connection command opens the production Sanka Code site.

An existing hosted source can instead use `--source-id SOURCE_ID --sha256 SHA256`.
For Django projects, pass `--settings-module project.settings` when needed.

Directory packaging excludes `.env`, `.env.*`, `.git`, `.ssh`, `.aws`, `.venv`,
`venv`, `node_modules`, `__pycache__`, `.sanka`, and `.next`. It does not execute
project files or apply `.gitignore`; inspect other files before consenting to an
upload. ZIPs containing excluded files, unsafe paths, links, or special files are
rejected. Limits are 8 MiB compressed, 64 MiB expanded, and 20,000 entries. Uploaded
sources follow Sanka Code's seven-day retention. `--yes` consents to upload and the
explicit credit cap; it is required for noninteractive submissions.

### Retry and results

Scan and Apply require an idempotency key. The CLI saves the exact request in its
private config directory **before** submitting paid work. After a timeout, retry
with identical inputs and the same key; do not use a new key for an uncertain
request. Changed source bytes or approval inputs require a new, deliberate intent.

`--wait` polls without starting more work. Interrupting it detaches the CLI and
leaves the cloud run intact. Use `sanka cloud status`, `events`, `receipt`, `cancel`,
and `download` with the returned run ID for follow-up (see their `--help`).
Review/wait exit codes are: 0 completed, 3 evidence missing or review required,
4 failed, 5 cancelled, 6 still queued/running. A background Scan/Apply returns 0
when accepted, not when the operation is complete. Verify only reports `passed`
when nonempty HTTP scenarios all match; a successful worker alone is insufficient.

These commands require an API release exposing `/v2/migrate/code-plans` and
`/v2/migrate/code-sources`. They do not fall back to another server or local execution
when the configured API lacks these endpoints.
