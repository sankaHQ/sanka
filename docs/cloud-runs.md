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
