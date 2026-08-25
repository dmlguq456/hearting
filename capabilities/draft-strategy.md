# Capability: draft-strategy

This is the portable capability contract for `draft-strategy`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `draft-strategy` |
| Group | `sub` |
| Supported modes | `rebuttal, paper, review, report, proposal, presentation` |
| Portable meaning | Create an initial document strategy and evidence-based writing plan. |
| Argument shape | `<mode> --inputs <comma-separated-paths> --output <artifact-dir> [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial] <task description>` |

## Invocation Semantics

Create an initial document strategy. The internal mode enum has six values:
rebuttal, paper, review, report, proposal, and presentation. Autopilot-draft's
form-first paper/presentation/doc modes convert doc intent from natural-language
keywords (rebuttal response, review, report, proposal, or generic) into one of
these direct sub-skill mode labels. For direct invocation, require the user to
provide one of the six modes as the first argument.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not. Capability-specific output placement follows `core/CONVENTIONS.md` section 5 until this spec is expanded with a stricter per-capability artifact map.

## Artifact Producer Lifecycle

`draft-strategy` is a `standard+` stage worker: it never issues its own campaign or
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
| Claude Code | `adapters/claude/skills/draft-strategy/SKILL.md` and `skills/draft-strategy/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/draft-strategy/SKILL.md`, while `skills/draft-strategy/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info draft-strategy`. Use `adapters/codex/skills/draft-strategy/SKILL.md` as the native Codex Skill projection; do not consume `skills/draft-strategy/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info draft-strategy`. Use `adapters/opencode/skills/draft-strategy/SKILL.md` and `adapters/opencode/commands/draft-strategy.md` as native OpenCode projections; do not consume `skills/draft-strategy/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/draft-strategy/SKILL.md` and `adapters/claude/skills/draft-strategy/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/draft-strategy/SKILL.md`, while `skills/draft-strategy/SKILL.md` remains the compatibility reference kept for parity/drift checks.
