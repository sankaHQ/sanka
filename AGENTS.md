# Shared Sanka CLI and OSS runtime

Read `docs/public-naming.md`, `docs/naming-compatibility.md`, and `CONTRIBUTING.md` before changes.

- Products are Sanka (data migrations), Sanka Flow (workflow migrations), and Sanka Code (code migrations). This repository and executable are shared interfaces.
- Extensions are capability packages. Data endpoints are configured sources/destinations. Use the same vocabulary in developer docs and code; installation never implies authentication.
- New data-access code uses `sanka_extensions.app`, `ExtensionRegistry`, `ExtensionRegistration`, `DataReader`, `DataWriter`, and `DataEndpoint` as appropriate.
- Keep the published compatibility inventory explicit. Do not mechanically rename protocol fields, URLs, error codes, stored specs, or third-party terminology.
- Keep Apache SDK/dispatcher code and AGPL runtime boundaries intact. Hosted SaaS implementations and credentials remain private to the API/jobs runtime.
- SDK source changes belong in `sankaHQ/extensions`. Synchronize the SDK and compatibility modules from a real immutable commit and verify provenance. Publish SDK changes before advancing runtime dependency pins.
- `sanka_extensions.flow` defines business requests without execution. The shared
  runtime owns future reconciliation and construction/verification/activation;
  business templates remain in Extensions. Read `docs/flow.md` before adding a
  Flow execution path. Do not imply the embedded contract is a runnable workflow.
- Preserve the existing `sanka code` function semantics; canonical custom-function commands are `sanka functions`. Code migration remains on the existing lifecycle commands.
- Use focused tests while editing. Finish review, then let the workspace PR helper run `make check` and `make build-release` as the final gate. AI-authored PRs use the workspace `sanka-pr-flow`; publication is a separate authorized action.

## Tests

Follow the workspace `test-audit` skill.

- Before adding a test, state in the PR which real bug it catches and why no
  existing test at a stronger boundary already catches it. No answer, no test.
- One owner per behaviour. User-visible behaviour is owned by a command-level
  test that runs the CLI or runtime entrypoint against real files, processes or
  a local database. Unit tests are for pure logic with real branching: parsers,
  planners, mappers, policies. Do not test the same change at several layers.
- Never write a test that reads source, workflow, Makefile, doc or config files
  as text and asserts on the text; asserts a constant, fault code, help text,
  URL literal or prompt wording verbatim; only asserts that a mock was called;
  asserts `hasattr`/`callable`/`isinstance`; asserts that a command or alias
  exists or is registered; or computes the expected value with the code under
  test.
- Do not write unit tests after the code to cover a diff. A regression test must
  fail on the pre-fix code; say so in the PR.
- Test lines added in a PR may not exceed non-test lines added unless the PR
  explains why (bug reproduction, new pure module, table-driven cases).
- Extend a table or `parametrize` row instead of copying a test. Split or trim a
  test file above 1,500 lines before adding anything to it.
- Fix a unit test slower than 0.5 s. Never add sleeps, real timers or real
  network waits.
- When a behaviour-preserving refactor breaks tests, delete or rewrite them at
  the owning boundary. Do not edit assertions to match the new implementation.
- No meta-tests that require other tests, docs listings or registrations to exist.
- Deleting a low-value test is a valid change on its own. Report test and
  non-test line counts separately in the PR.
