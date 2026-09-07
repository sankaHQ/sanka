# Hosted repository runs

`sanka cloud` is a client for the optional Sanka Developer cloud service. It uses
your existing `sanka login` profile and requires an exact workspace code on every
command. The service must be enabled by the workspace operator. Local `scan`,
`plan`, `apply`, `test`, and `verify` behavior is unchanged.

The initial recipe is DRF to FastAPI using a pinned, offline Python environment.
It runs generated tests and static checks. It does not issue an independent
certificate, perform HTTP replay, or install arbitrary project dependencies.

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
