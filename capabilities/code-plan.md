# Capability: code-plan

This is the portable capability contract for `code-plan`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `code-plan` |
| Group | `sub` |
| Supported modes | `none` |
| Portable meaning | Analyze code, write a detailed implementation plan, and run the plan-check gate at the rigor derived from intensity. |
| Argument shape | `<task description> [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial]` |

## Invocation Semantics

Create a detailed implementation plan based on actual codebase

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Assurance Contract

This sub-capability follows `core/CONVENTIONS.md §1`: plan-check and review rigor is derived from intensity rather than independently selected. `code-plan` is used for durable `standard+` code work cycles; `direct` skips it and `quick` is handled by one registered-headless dispatch-depth-1 one-shot conductor with its inline micro-plan plus `plan-check-lite`. Independent plan review is selected by intensity/risk and is not repeated after every sub-stage by default.


## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not. Capability-specific output placement follows `core/CONVENTIONS.md` section 5 until this spec is expanded with a stricter per-capability artifact map.

## Artifact Producer Lifecycle

`code-plan` is a `standard+` stage worker: it never issues its own campaign or
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
| Claude Code | `adapters/claude/skills/code-plan/SKILL.md` and `skills/code-plan/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/code-plan/SKILL.md`, while `skills/code-plan/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info code-plan`. Use `adapters/codex/skills/code-plan/SKILL.md` as the native Codex Skill projection; do not consume `skills/code-plan/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info code-plan`. Use `adapters/opencode/skills/code-plan/SKILL.md` and `adapters/opencode/commands/code-plan.md` as native OpenCode projections; do not consume `skills/code-plan/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/code-plan/SKILL.md` and `adapters/claude/skills/code-plan/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/code-plan/SKILL.md`, while `skills/code-plan/SKILL.md` remains the compatibility reference kept for parity/drift checks.
