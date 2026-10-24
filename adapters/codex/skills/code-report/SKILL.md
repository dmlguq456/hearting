---
name: code-report
description: "Use only when autopilot-code dispatches the final code-cycle reporting stage. Not for top-level user requests or primary capability routing."
---

# code-report

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/code-report.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info code-report`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Read `capabilities/code-report.md` for the runtime-neutral contract.
2. Run `adapters/codex/bin/preflight.sh capability-info code-report`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `code-report`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<plan name or path>`
- Portable meaning: Assemble code-cycle results into a user-facing report.

## Portable Contract

- Invocation semantics: Generate a detailed change report from plan + dev logs — focuses on key changes, principles, and insights for future reference. When it embeds or cites a generated spectrogram, completion also requires a passing semantic manifest, range-compatible claim evidence, and a hash-current representative PNG review. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.



## Projected Portable Details

## Artifact Ownership

Artifact root: `core/CONVENTIONS.md §5.1`.

In a `standard+` `autopilot-code` stage cycle, `code-report` owns
`final_report.md`, `analysis_project/code/`, and `pipeline_summary.md` (using the
shared lock where required). It consumes plan, checklist, development, and test
evidence but must not rewrite source or another stage's evidence class. This is
the report half of the stage ownership contract in `core/OPERATIONS.md` §5.10.

The report stage does not publish a task branch or PR. The capability owner
receives task-publication capacity from the route's sealed complete entry scope
and performs publication only when those actions are part of the actual request.
Integration and merge stay with the supervisor.

The stage returns the canonical `final_report.md` source path to the
dispatch-depth-1 capability owner. It does not independently invoke an artifact
sink. After the report terminal, the owner evaluates the route-sealed optional
extension: an available sink receives the app-neutral receipt and an
unavailable probe records `skipped/extension-unavailable`.

When the report references generated spectrograms, consume the semantic
manifest, verifier result, and representative visual-review evidence from the
test stage. Do not publish band-sensitive claims or mark the report complete
when that gate is missing or failing.

## Artifact Producer Lifecycle

`code-report` is a `standard+` stage worker: it never issues its own campaign or
cycle. It receives the owner's open cycle through
`AGENT_ARTIFACT_CAMPAIGN_ID`/`AGENT_ARTIFACT_CYCLE_ID`/`AGENT_ARTIFACT_PRODUCER_ID`/
`AGENT_ARTIFACT_CYCLE_DIR`/`AGENT_ARTIFACT_OUTPUT_DIR` (dispatch env
pass-through); the resolver applies the report node's declared outputs and
write scope.
`artifact_producer.py` owns the open cycle and its output paths; `finalize` and
`admit-shared` belong to the owner, never to a stage worker. See
`producer_lifecycle` in `capabilities/topologies.json`.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- use DB memory paths, not runtime-native memory files.

Portable procedure and reference documents are owned under `capabilities/code-report/references/` when present. Claude, Codex, and OpenCode Skill bodies are generated projections of this capability and use the same logical procedure read-set.

## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/code-report/references/procedure.md`

## Workflow Evidence

- Before capability grounding/spec-changing work: `adapters/codex/bin/preflight.sh route code-report [cwd] [session-id]`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
