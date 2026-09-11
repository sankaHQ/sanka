# Sanka architecture

The OSS CLI/runtime is shared by Sanka, Sanka Flow and Sanka Code. Extensions own
the SDK and reusable system, business and code capabilities. Private cloud
authentication, workspace services and hosted SaaS implementations remain in
`sanka-api`; screens remain in `sanka-react`. The embedded Flow definition contract
does not yet provide a Flow execution path; see [Flow runtime ownership](flow.md).

This repository owns one `sanka-cli` distribution and one `sanka` executable.
The Sanka Extension SDK and independently installed extensions
live in [`sankaHQ/extensions`](https://github.com/sankaHQ/extensions).

## Package and license zones

| Source zone | License | Responsibility |
|---|---|---|
| `packages/sanka-cli/src/sanka_cli` | Apache-2.0 | top-level dispatcher, hosted commands, authentication, output, optional MCP integration |
| `packages/sanka-cli/src/sanka_extensions` (including the SDK compatibility modules) | Apache-2.0 | synchronized Sanka Extension SDK used by the local engine |
| `packages/sanka-cli/src/sanka` | AGPL-3.0-only | migration runtime, planner, execution, verification, extension manager, extension host |

The wheel declares `Apache-2.0 AND AGPL-3.0-only`, contains both license texts
and `NOTICE`, and exposes exactly `sanka = sanka_cli.main:main`.
`scripts/check_license_headers.py` enforces the source zones.
`scripts/check_connector_sdk_sync.py` byte-compares the embedded Sanka Extension SDK
with the exact reviewed extensions checkout. `scripts/check_import_boundaries.py`
prevents the embedded SDK and MCP integration from importing the AGPL runtime.

## Command boundary

The Click dispatcher selects local or hosted execution before checking
authentication:

| Command surface | Execution | Authentication |
|---|---|---|
| local lifecycle, `connect`, and `extension` | in-process runtime or verified child component | no Sanka token |
| explicit cloud migration selectors | hosted migration API | existing hosted token |
| hosted resources, workflows, AI, `functions` (custom functions) | hosted API | existing command rule |
| public research and assessment | public API | credential-free |
| `mcp` | local stdio server | public tools remain credential-free |

Local lifecycle arguments are forwarded directly to the mature parser. The
default migration spec is `sanka.yaml`; there is no obsolete filename or
executable fallback.

## Marketplace and extension host

The official marketplace publishes immutable wheels as GitHub release assets,
never as runtime-resolved PyPI packages. Manifests pin exact HTTPS URLs,
SHA-256 digests, package identities, entry points, protocols, and compatible
`sanka-cli` versions. A project lock records the selected marketplace snapshot
and artifacts.

Extensions for code conversion execute through `sanka-extension/v1`. Extensions
that read or write systems retain their typed `sanka.connectors` registrations but load only in a verified
isolated environment. The main process communicates with one persistent child
through `sanka-connector/v1`; it never imports marketplace provider code.
Credentials reach only the selected host and are not written to manifests,
locks, logs, or protocol errors.

Hosted providers such as HubSpot, Salesforce, and SendGrid remain in Sanka's
managed service and are not local marketplace components.

## Hosted-product boundary

Proprietary control-plane code, customer data, credentials, production
configuration, and hosted provider clients remain outside this repository.
The open-source package neither vendors private modules nor silently falls back
to them for documented local behavior.

## Design tenets

1. A migration completes; Sanka is not a continuous ETL or CDC service.
2. Inspection, validation, and planning are destination-write-free.
3. Apply is bound to an explicit scope and the exact reviewed plan hash.
4. Checkpoints and identity-ledger writes make interrupted work resumable and
   idempotent.
5. Verification reconciles the reviewed source scope with target readback.
6. Optional system-access behavior is expressed through typed capability
   protocols, not ad hoc attribute checks.

Generated destination dependencies belong to the generated project. Provider
drivers belong to GitHub marketplace wheels. Neither belongs in the core
`sanka-cli` dependency set.
