# Shared Sanka CLI and OSS runtime

Read `docs/public-naming.md`, `docs/naming-compatibility.md`, and `CONTRIBUTING.md` before changes.

- Products are Sanka (data migrations), Sanka Flow (workflow migrations), and Sanka Code (code migrations). This repository and executable are shared interfaces.
- Extensions are capability packages. Data endpoints are configured sources/destinations. Use the same vocabulary in developer docs and code; installation never implies authentication.
- New data-access code uses `sanka_extensions.data`, `ExtensionRegistry`, `ExtensionRegistration`, `DataReader`, `DataWriter`, and `DataEndpoint` as appropriate.
- Keep the published compatibility inventory explicit. Do not mechanically rename protocol fields, URLs, error codes, stored specs, or third-party terminology.
- Keep Apache SDK/dispatcher code and AGPL runtime boundaries intact. Hosted SaaS implementations and credentials remain private to the API/jobs runtime.
- SDK source changes belong in `sankaHQ/extensions`. Synchronize the SDK and compatibility modules from a real immutable commit and verify provenance. Publish SDK changes before advancing runtime dependency pins.
- `sanka_extensions.flow` defines business requests without execution. The shared
  runtime owns future reconciliation and construction/verification/activation;
  business templates remain in Extensions. Read `docs/flow.md` before adding a
  Flow execution path. Do not imply the embedded contract is a runnable workflow.
- Preserve the existing `sanka code` function semantics; canonical custom-function commands are `sanka functions`. Code migration remains on the existing lifecycle commands.
- Use focused tests while editing. Finish review, then let the workspace PR helper run `make check` and `make build-release` as the final gate. AI-authored PRs use the workspace `sanka-pr-flow`; publication is a separate authorized action.
