# Legal documents

> **Status: DRAFT.** Every document in this directory was prepared as a working
> draft for review by Sanka, Inc.'s legal counsel. Nothing here is an offer,
> and none of it is in effect until counsel approves it and it is formally
> adopted. The signing automation (CLA bot) is wired up only after that
> approval.

| Document | Purpose |
|---|---|
| [`individual-cla.md`](individual-cla.md) | Individual Contributor License Agreement — signed once by each external contributor before their first merged contribution |
| [`corporate-cla.md`](corporate-cla.md) | Corporate CLA — signed by an employer whose employees contribute on work time |
| [`commercial-license.md`](commercial-license.md) | Commercial license for the AGPL-3.0-only Sanka Migrate Runtime — alternative terms for organizations that cannot accept AGPL obligations |

## Why a CLA at all

The Sanka Migrate Runtime is dual-licensed: AGPL-3.0-only publicly, plus
commercial licenses sold by Sanka, Inc. Dual licensing requires that Sanka can
relicense the entire work. Sanka's own code poses no problem (Sanka holds the
copyright), but an external contribution accepted *without* a CLA would be
licensed to us only under AGPL-3.0 — freezing the combined work as
AGPL-3.0-only forever and breaking both the commercial runtime license and the
ability to ship contributed fixes inside proprietary Sanka Migrate Cloud. The CLAs
below grant Sanka the explicit right to relicense contributions, which is the
single clause that keeps the model working.

Drafting notes for counsel:

- Both CLAs are adapted from the Apache Software Foundation's ICLA/CCLA v2.2
  structure (the de-facto industry standard), with the grantee changed to
  Sanka, Inc. and an explicit relicensing clause added to the copyright grant
  (§2) — stock Apache language relies on the sublicensing right alone.
- Licensing model reference: the repository follows the same per-directory
  split as firecrawl/firecrawl (AGPL core + permissively licensed SDKs);
  we use Apache-2.0 rather than MIT for the SDK to carry a patent grant.
- The commercial license is a short-form template; fees, support, and any
  service-restriction carve-outs are deliberately pushed to an Order Form.
