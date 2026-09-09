# Identity fields are part of the execution manifest

Mapping generates `identityFields` from scalar identity mappings. The report
codec must preserve these fields through validation, queued execution and journal
round trips. Previously it discarded them as unknown metadata, causing hosted
planning to reject its own dry-run manifest as inconsistent with mapping.

Canonicalization now validates nonblank exact string field names and sorts and
deduplicates the list. An absent or empty list represents no explicit identities.
Changed or removed identities fail manifest matching before execution, just as
changed routes and filters do. Unknown unrelated keys remain ignored.

Old queued manifests without identity metadata cannot match a current mapping
that declares identities; regenerate and review that execution plan. Do not
silently restore execution approval from an unbound legacy manifest.

This source change requires a new approved runtime package release before package
consumers receive it. The hosted API currently pins `sanka-migrate==0.1.0a6`;
its separate fingerprint-gated historical report adapter does not depend on
publishing this change or silently upgrading that dependency.
