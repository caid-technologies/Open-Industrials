# CAID runtime integration — 2026-10-24

CAID base: `005f6ce19f55935b4c03f01f55475daa4031388d`.
Imported implementation: Mapped-Assembly/Open-Industries
`14c5f321fb25ca117e79ebb7310719ec6c4ca17b` (including the recent local scene and
persistent-runtime changes in #51/#53).

The import preserves upstream implementation and attribution. Its GitHub issue
numbers and historical verification links refer to Mapped-Assembly unless an
owner is explicitly named. The repositories have independent histories; this
change ports their file-tree difference onto CAID rather than rewriting CAID history.

Delivered areas:

- SQLite game authority, native accounts/sessions, durable autonomous scheduling.
- Robot/sensor/material/recovery/manufacturing/combat/victory implementations and tests.
- Reusable simulation transactions, resources, jobs, clocks and authorized event
  history, with game, neutral work-cell and FIELD-LAB domains.
- Reproducible six-scenario FIELD-LAB and durable SQLite execution.
- Local SQLite scene persistence and runtime readiness/backup/recovery tooling.
- Static GLB/3D Tiles export, extended here with a new CoACD/MuJoCo physics path;
  see [Physics workspace](physics-workspace.md).

Excluded by request: `.github/workflows/publish-packages.yml`, the PyPI packaging
manifest, public npm publishing configuration and publishing instructions. Root
and material-library npm packages remain private. No release, registry publication,
production migration or deployment is performed by this change.

Existing CAID PR #80 layout handles/snapping, #56 MCP diagnostics and #60's specific
cleanroom GIF are separate from this runtime scope. Existing CAID issue #62 live
cloud verification and broader STEP acceptance still require their own evidence.
The historical Postgres game migration is retained for provenance; native SQLite
game operation neither depends on it nor imports its state.

The new physics demo is separately authored geometry. It does not relicense the
upstream FIELD-LAB/CAD assets; their existing redistribution boundary is retained.
The simulation examples remain modeled software, not real instrument validation.
