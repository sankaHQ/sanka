# Shared Sanka CLI and OSS runtime

Read `docs/public-naming.md`, `docs/naming-compatibility.md`, and `CONTRIBUTING.md` before changes.

- Products are Sanka (system/data migrations), Sanka Flow (workflow migrations), and Sanka Code (code migrations). This repository and executable are shared interfaces.
- Extensions are capability packages. Systems are configured endpoints/accounts. Use the same vocabulary in developer docs and code; installation never implies authentication.
- New data-access code uses `sanka_data`, `DataExtensionRegistry`, `DataExtensionRegistration`, `SystemReader`, `SystemWriter`, and `SystemConfig` as appropriate.
- Keep the published compatibility inventory explicit. Do not mechanically rename protocol fields, URLs, error codes, stored specs, or third-party terminology.
- Keep Apache SDK/dispatcher code and AGPL runtime boundaries intact. Hosted SaaS implementations and credentials remain private to the API/jobs runtime.
- SDK source changes belong in `sankaHQ/extensions`. Synchronize both SDK namespaces from a real immutable commit and verify provenance. Publish SDK changes before advancing runtime dependency pins.
- Preserve the existing `sanka code` function semantics; canonical custom-function commands are `sanka functions`. Code migration remains on the existing lifecycle commands.
- Use focused tests while editing. Finish review, then let the workspace PR helper run `make check` and `make build-release` as the final gate. AI-authored PRs use the workspace `sanka-pr-flow`; publication is a separate authorized action.
