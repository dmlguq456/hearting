# Capability: design-refs

This is the portable capability contract for `design-refs`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `design-refs` |
| Group | `sub` |
| Supported modes | `none` |
| Portable meaning | Collect external and user-provided visual references and create a brief. |
| Argument shape | `<design task> [--design <path>] [--refs <image paths>] [--no-web]` |

## Invocation Semantics

Reference collection and briefing: gather user-provided images, external web
references through the material role's web-image-search mode, and existing
design-system assets. Write a brief that informs subsequent phases.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not. Capability-specific output placement follows `core/CONVENTIONS.md` section 5 until this spec is expanded with a stricter per-capability artifact map.

## Artifact Producer Lifecycle

`design-refs` is a `standard+` stage worker: it never issues its own campaign or
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
| Claude Code | `adapters/claude/skills/design-refs/SKILL.md` and `skills/design-refs/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/design-refs/SKILL.md`, while `skills/design-refs/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info design-refs`. Use `adapters/codex/skills/design-refs/SKILL.md` as the native Codex Skill projection; do not consume `skills/design-refs/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info design-refs`. Use `adapters/opencode/skills/design-refs/SKILL.md` and `adapters/opencode/commands/design-refs.md` as native OpenCode projections; do not consume `skills/design-refs/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/design-refs/SKILL.md` and `adapters/claude/skills/design-refs/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/design-refs/SKILL.md`, while `skills/design-refs/SKILL.md` remains the compatibility reference kept for parity/drift checks.
