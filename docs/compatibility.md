# CLI and extension compatibility

This table records the verified CLI 0.2.12 release combination. It is not a promise
that arbitrary CLI, SDK and extension versions can be mixed.

| Component | Verified identity | Evidence |
| --- | --- | --- |
| CLI | `sanka-cli==0.2.12`, Python 3.12 | Public fresh installation and upgrade on macOS/Linux |
| Embedded Sanka Extension SDK | `0.1.0a4`, source `b52bf22f60b2a3704bf0414d609c3e3f767bcd41` | Immutable SDK provenance and standalone/embedded Flow wheel acceptance |
| Official marketplace | `extensions-v0.1.0a22`, source `37873d18970e7ffe4c55bfa1663e7c4c36fd4d12` | Published wheel hashes and locked catalog identity |
| DRF to FastAPI | `sanka/drf-to-fastapi` 0.1.0a10 | Public example scan with and without activation; bounded native plan; exact Extensions regression |
| Data access | `sanka-connector/v1` | Published Markdown/SQLite bounded planning acceptance |
| Code conversion | `sanka-extension/v1` | Shared stage identity/artifact validation and quickstart acceptance |
| Flow generation contract | `sanka-flow-extension/v1` | Wheel isolation, capability and schema validation; not native workflow execution |

The SDK snapshot and marketplace snapshot have different responsibilities and
need not have matching versions. Do not upgrade the embedded SDK by manually
installing a separate SDK into the isolated CLI environment.

Published CLI 0.2.13 embeds SDK `0.1.0a5` from Extensions commit
`878b416898dd5c19b61b818a34b9a12443ebdc31`. The current source adopts published
SDK `0.1.0a7` from `78dbdc6b6e1b73c1486abb404e8858b2c9756ae8`; this does not
change either published CLI release. Its conformance tests retain the original
a4 v1/v2 and a5 v3 generator wheels and add standalone a7 host and generator
environments. The runtime still admits only v1/v2/v3 generation. Native v3
verification and activation remain unavailable; v4/v5 execution requires separate
runtime and hosted adapter changes.

The separate `sanka/business-flows` 0.1.0a1 candidate requires CLI 0.2.13. Its
real-wheel acceptance covers HubSpot order/billing definition generation and
inactive planning. It does not install a native provider adapter or migrate
existing Studio workflows. The default Data/Code marketplace remains unchanged.

The CLI rejects unsupported protocols, invalid locks and mismatched artifact
identities. A version number alone does not prove compatibility. Keep project
locks unchanged during ordinary CLI upgrades, and explicitly adopt an extension
update only after reviewing its release and a new plan:

```bash
uv tool upgrade --python 3.12 sanka-cli
sanka extension marketplace upgrade official
sanka extension add sanka/drf-to-fastapi
sanka scan .
```

Use the actual marketplace name from `sanka extension marketplace list` if it is
not `official`. For `SANKA_EXTENSION_LOCK_INVALID`, restore the last known-good
project lock from version control and check the CLI version and extension's
published protocol before explicitly reinstalling. Do not edit protocol fields
or digests to force a lock to load. An artifact hash mismatch requires matching
published bytes or a reviewed new release, not disabling verification.

The [naming compatibility inventory](naming-compatibility.md) records retained
imports and aliases. The [installation guide](install.md) covers shell selection
and package-manager recovery. The [release procedure](releasing.md) owns the
checks needed to add another verified release combination.
