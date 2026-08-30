# Security Policy

## Supported versions

Sanka is currently pre-1.0. Security fixes are provided for the latest
published prerelease only. Older prereleases do not receive security backports.

## Reporting a vulnerability

Do not open a public GitHub issue for a suspected vulnerability.

Email `hey@sanka.com` with a subject beginning `[Security][sanka]`. Include the
affected package and version, reproduction steps, expected impact, and any
suggested mitigation. Do not include real customer data, access tokens,
passwords, or other credentials.

We will use the reporting channel to coordinate validation, remediation, and
responsible disclosure. No response or resolution time is guaranteed by this
policy.

## Security boundaries

This repository contains the local migration runtime, CLI engine, generated
application tooling, and standalone MCP package. Source records, application
code, schemas, and generated HTTP traffic are treated as untrusted inputs.

Reviewed plan hashes, exact candidate sets, configured source scope, credential
references, and local state permissions are security boundaries. Mutating
operations must fail closed when those boundaries cannot be verified.

Dynamic Django startup and source HTTP probes run in a separate OS-contained
worker with no network, no ambient credentials, restricted filesystem reads,
and writes limited to a disposable directory. On platforms where Sanka cannot
establish that boundary, dynamic inspection fails closed unless the operator
explicitly marks the source repository trusted. Generated code and dependency
metadata are authenticated with a machine-local integrity key before test or
verification, and generated dependencies run from a fresh disposable
environment rather than a mutable environment inside the output directory.

The hosted Sanka API and its private job runtime are maintained separately.
Report suspected hosted-service vulnerabilities through the same private
contact.
