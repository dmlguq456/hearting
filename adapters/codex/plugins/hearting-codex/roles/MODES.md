# Unit Catalog Doctrine

> The former `roles/modes/` persona inventory and the `adapters/claude/agent-modes/`
> divergent copy are re-homed (2026-07-22, user decision: 승격+재홈) into the single
> portable **unit catalog** at `roles/units/<family>/<unit>.md`. This file is the
> doctrine layer; `roles/units/_schema.md` is the authoring contract.

## What a unit is

A unit is the single declaration of one dispatchable behavior atom — persona body plus a
machine frontmatter (portable role, worker type, floor, read-only nature, I/O verdict
semantics, stance/fragment refs, tool contracts). Units are executed as
dispatch-depth-2 workers selected by the compiled capability route
(`capabilities/topologies.json` → `utilities/capability-route.py` →
`utilities/worker_bootstrap.py`, which appends the unit BODY to the kernel and
worker-type overlay). `family` is a grouping label; no runtime team agent exists on any
harness. Per-harness native agents are kernel helpers only (e.g. `memory-scout`).

## Universal Review Stance

Every review- or verification-type unit operates under the refute-by-default adversarial
stance, single-sourced at `roles/units/_shared/stance.md` and anchored in
`core/CONVENTIONS.md §1.1` — regardless of the intensity that dispatched it. A unit file
may reinforce the stance in its own words but never lowers it. Two declared shapes,
already encoded in the fragment and the unit contracts:

- `qa/security-review` reports only high-confidence findings (confidence 8–10); naming
  zero findings is its output contract, and its silence means "no HIGH/MEDIUM found",
  never "proven safe".
- `research/fact-check` declares `stance: none`: it is a verbatim-comparison contract
  (cards vs. sources) with its own strict mismatch tables; its adversarial counterpart is
  `research/claim-verify`, which is default-refute with quorum aggregation.

This stance is a posture inside whatever check runs — distinct from the separate
independent adversary *pass* that only higher intensities add, which is realized by
dispatching the relevant review persona into a separate execution. The same harness
is sufficient (SD-160); usage-aware allocation does not require a cross-harness pass.

## Adapter realization

Adapters consume the catalog, never fork it:

- **Dispatch (all harnesses):** the worker bootstrap reads `roles/units/<unit>.md`
  directly (stdlib-only hot path); `--unit` flows from the sealed route node through
  `dispatch-node.py` / `stage-dispatch-fallback.py` to each adapter wrapper.
- **Codex:** `adapters/codex/bin/sync-native-modes.py` generates native mode guides from
  unit bodies (unit-bearing manifest modes only); `mode-map.sh` resolves
  `roles/units/<family>/<name>.md`.
- **OpenCode:** `mode-map.sh` resolves the same catalog path; support/fallback metadata
  stays adapter-owned.
- An adapter must not claim a unit is supported unless it provides the unit's named tool
  contracts (or documented fallbacks) and reports unsupported combinations clearly.

Localization is a projection concern: the catalog is English-canonical; user-facing
output language follows `roles/response-policy.md`.
