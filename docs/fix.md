# Sanka Fix

The `fix --cloud` command is available in Sanka CLI 0.2.14 and later.

Sanka Fix is optional paid cloud verification and repair of a retained Sanka Code
migration candidate. It requires an eligible Code migration run ID, including one
created in the UI. It does not import arbitrary local folders or execute agents locally.
Availability depends on the deployed API and worker configuration.

```sh
sanka login
sanka whoami
sanka fix --cloud --workspace 78165495 --run <migration-run-id> --max-credits 1500 --wait
```

`login` prompts for the Developer API token without echoing it. Existing
`--access-token` and profile options remain supported. Fix displays the pinned
workspace, source and candidate digests, recipe, verification scope, provider/model
and worker limits. Confirmation authorizes cloud processing of repository excerpts
and a hold of at most the specified total credits. The cap is not a fixed price or
a guarantee that repairs or all application behavior will be verified.

For automation, provide all three consent arguments:

```sh
sanka --output json fix --cloud --workspace 78165495 --run <migration-run-id> \
  --max-credits 1500 --yes --idempotency-key <unique-reviewed-intent>
```

Reuse exactly the same key and inputs after any uncertain response. Interactive
submissions without a supplied key save an intent in the CLI configuration directory
before sending. Repeating the same profile, API URL, workspace, parent and cap reuses that intent
and its saved candidate without requiring eligibility to remain available. Supply a fresh explicit key only for an intentionally separate
paid attempt. Keep the saved intent if the client loses its connection; deleting it
can cause a later invocation to create a second chargeable run.

The response includes the Fix run ID, execution status, `fix_result`, UI URL and
receipt command. Without `--wait`, exit 0 means acceptance only. With `--wait`, stdout
contains one final JSON result; consent and the immediate run ID are on stderr.
The default wait limit is 900 seconds, adjustable with `--wait-timeout` (1–3600).
Ctrl-C detaches; it does not cancel paid cloud work.

| Wait exit | Meaning |
| --- | --- |
| 0 | Succeeded with `checks_passed` on the reported scope |
| 3 | Needs review, budget reached, or succeeded without a checked outcome |
| 4 | Setup or execution failure |
| 5 | Cancelled |
| 6 | Wait limit reached; cloud work may continue |
| 130 | Client detached with Ctrl-C; cloud work may continue |

Command/transport errors use exit 1; invalid arguments use exit 2. Queued or running
status never means verification passed. Reattach by reading the existing run:

```sh
sanka cloud status --workspace 78165495 <fix-run-id>
sanka cloud events --workspace 78165495 <fix-run-id>
sanka cloud receipt --workspace 78165495 <fix-run-id>
sanka cloud download --workspace 78165495 <fix-run-id> --to fixed-output.zip
sanka cloud cancel --workspace 78165495 <fix-run-id>
```

Downloads validate the declared size and SHA-256 and refuse to overwrite files.
Use the returned Sanka Code URL to review changes and create a PR for the selected
artifact. Fix does not merge or deploy a change. The original migration remains
accessible through its parent run. Existing `sanka cloud repair`, `sanka repair`
and custom-function `sanka code` semantics remain unchanged.
