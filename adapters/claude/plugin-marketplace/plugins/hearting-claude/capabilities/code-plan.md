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


When a composed graph omits the plan producer, plan-check reads the `plan.md`
compose sealed for it from an earlier cycle (`input_sources`); an explicit
`--reviewed-evidence` file still takes precedence. A current plan gate
supplies this binding automatically in the full graph. The runtime binds the
input to the attempt before execution; prompt text is not input authority.
Corrections use SD-161 input revisions when there is no producer marker, keeping
review admission separate from PASS and retaining the cap+1 verdict ceiling.

## Slices Block

When the plan splits into 2–4 packages that own exactly disjoint files and can each finish on their own, `plan.md` carries exactly one fenced block whose info string is `slices`: a JSON list of `{"id", "files", "verify", "brief"?, "adapter"?, "expected_round_trips"?}` (`files` are exact paths, no globs; `verify` is one line). The owner's `dispatch-batch.py --slices <plan.md>` reads it and runs the slices in parallel. A plan that cannot be split that way carries no block and runs as one session; a duplicated or malformed block also falls back to one session.

## Artifact Ownership

Artifact root: `core/CONVENTIONS.md §5.1`; output placement: `§5`.

## Artifact Producer Lifecycle

`code-plan` is a `standard+` stage worker: it never issues its own campaign or
cycle. It receives the owner's open cycle through
`AGENT_ARTIFACT_CAMPAIGN_ID`/`AGENT_ARTIFACT_CYCLE_ID`/`AGENT_ARTIFACT_PRODUCER_ID`/
`AGENT_ARTIFACT_CYCLE_DIR`/`AGENT_ARTIFACT_OUTPUT_DIR` (dispatch env
pass-through) and writes to the selected node's declared outputs within its
`write_scope`.
`artifact_producer.py` owns the open cycle and its output paths; `finalize` and
`admit-shared` belong to the owner, never to a stage worker. See
`producer_lifecycle` in `capabilities/topologies.json`.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- use DB memory paths, not runtime-native memory files.

Portable procedure and reference documents are owned under `capabilities/code-plan/references/` when present. Claude, Codex, and OpenCode Skill bodies are generated projections of this capability and use the same logical procedure read-set.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/code-plan/SKILL.md` and `skills/code-plan/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/code-plan/SKILL.md`, while `skills/code-plan/SKILL.md` remains a generated compatibility projection sourced from `capabilities/` and kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info code-plan`. Use `adapters/codex/skills/code-plan/SKILL.md` as the native Codex Skill projection; do not consume `skills/code-plan/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info code-plan`. Use `adapters/opencode/skills/code-plan/SKILL.md` and `adapters/opencode/commands/code-plan.md` as native OpenCode projections; do not consume `skills/code-plan/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/code-plan/SKILL.md` and `adapters/claude/skills/code-plan/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/code-plan/SKILL.md`, while `skills/code-plan/SKILL.md` remains a generated compatibility projection sourced from `capabilities/` and kept for parity/drift checks.
