# Capability: design-components

This is the portable capability contract for `design-components`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `design-components` |
| Group | `sub` |
| Supported modes | `none` |
| Portable meaning | Build UI components/mockups and preview artifacts. |
| Argument shape | `<design path or app path>` |

## Invocation Semantics

Component and visual-asset creation through the design role's maker mode.
Produce shadcn/Tailwind components (`ui`), composed full-screen pages
(`webapp`), slide visual guides (`slide`), SVG icons (`icon`), or
Mermaid/direct-SVG/Excalidraw diagrams (`diagram`). Render and visually
self-verify every output through a render→read→fix loop. With
`--artifact standalone`, emit a self-contained single-file HTML preview.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not. Capability-specific output placement follows `core/CONVENTIONS.md` section 5 until this spec is expanded with a stricter per-capability artifact map.

## Artifact Producer Lifecycle

`design-components` is a `standard+` stage worker: it never issues its own campaign or
cycle. It receives the owner's open cycle through
`AGENT_ARTIFACT_CAMPAIGN_ID`/`AGENT_ARTIFACT_CYCLE_ID`/`AGENT_ARTIFACT_PRODUCER_ID`/
`AGENT_ARTIFACT_CYCLE_DIR`/`AGENT_ARTIFACT_OUTPUT_DIR` (dispatch env
pass-through), may call `utilities/artifact_producer.py begin --node <node id>`
on the same route to resume that cycle, and writes only inside
`<cycle_dir>/artifacts/<bucket>/...` within its node `write_scope`.
`artifact_producer.py check-write` (via `hooks/artifact-guard.sh`) denies any
write outside the open cycle once the cutover is active; `finalize` and
`admit-shared` belong to the owner, never to a stage worker. See
`producer_lifecycle` in `capabilities/topologies.json`.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- enforce git/worktree safety before edits;
- enforce artifact ordering before new durable artifacts;
- enforce spec-read gating when this capability changes spec-backed code or specs;
- use DB memory paths, not runtime-native memory files.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/design-components/SKILL.md` and `skills/design-components/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/design-components/SKILL.md`, while `skills/design-components/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info design-components`. Use `adapters/codex/skills/design-components/SKILL.md` as the native Codex Skill projection; do not consume `skills/design-components/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info design-components`. Use `adapters/opencode/skills/design-components/SKILL.md` and `adapters/opencode/commands/design-components.md` as native OpenCode projections; do not consume `skills/design-components/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/design-components/SKILL.md` and `adapters/claude/skills/design-components/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/design-components/SKILL.md`, while `skills/design-components/SKILL.md` remains the compatibility reference kept for parity/drift checks.
