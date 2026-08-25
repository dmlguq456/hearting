---
name: draft-strategy
description: "Use only when autopilot-draft dispatches document strategy and evidence-plan creation. Not for top-level user requests or primary capability routing."
---

# draft-strategy

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/draft-strategy.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info draft-strategy`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Read `capabilities/draft-strategy.md` for the runtime-neutral contract.
2. Run `adapters/codex/bin/preflight.sh capability-info draft-strategy`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `draft-strategy`
- Invocation class: `parent-invoked`
- Supported modes: `rebuttal, paper, review, report, proposal, presentation`
- Argument shape: `<mode> --inputs <comma-separated-paths> --output <artifact-dir> [--intensity direct|quick|standard|strong|thorough|adversarial] <task description>`
- Portable meaning: Create an initial document strategy and evidence-based writing plan.

## Portable Contract

- Invocation semantics: Create an initial document strategy. The internal mode enum has six values: rebuttal, paper, review, report, proposal, and presentation. Autopilot-draft's form-first paper/presentation/doc modes convert doc intent from natural-language keywords (rebuttal response, review, report, proposal, or generic) into one of these direct sub-skill mode labels. For direct invocation, require the user to provide one of the six modes as the first argument. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.



## Projected Portable Details

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


## Required Guards

- Before edits: `adapters/codex/bin/preflight.sh write <file> [session-id]`
- Before capability grounding/spec-changing work: `adapters/codex/bin/preflight.sh route draft-strategy [cwd] [session-id]`
- Before spec-changing work: `adapters/codex/bin/preflight.sh capability draft-strategy [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/codex/bin/preflight.sh read <prd.md> [session-id]`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
