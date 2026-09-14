# Foundation reconciliation — CLI 0.2.12

The shared interfaces and stage validator are shipped. PR87 and PR88 are
superseded by release PR89 (`439d9496d91f6da199691274e3bc248024839f88`), which is
an ancestor of CLI 0.2.12. They were closed without merging their obsolete
release branches.

The PR88 head `4910084ac901ba04b7d5e2aa0d06033d00ca9f0c` and PR89 have identical
shared stage implementation and tests. Their remaining tree differences add the
Code lifecycle/repair and artifact handling, with corresponding docs and tests.
PR87's interface/SDK changes are retained in that same release; the later diff
adds shared stage validation and Code lifecycle integration. Commit ancestry
alone cannot prove this because the release was squash-merged.

Current verification:

- PyPI, public installer and Homebrew are synchronized at 0.2.12.
- Isolated CLI/source environments and explicit extension upgrades pass public
  quickstart scan/plan acceptance.
- Interactive zsh/bash acceptance reproduces an older executable shadowing the
  current installation, verifies PATH/cache recovery, and checks fresh shells.
- The [compatibility table](compatibility.md) bounds release claims to tested
  versions and protocols.

Studio template generation, native workflow execution and additional business
workflows remain with the companion Studio work. Open CLI PR95 and Extensions
PR41 are not silently included in this foundation reconciliation.
