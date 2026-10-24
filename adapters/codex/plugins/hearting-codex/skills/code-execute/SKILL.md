---
name: code-execute
description: "Use only when autopilot-code dispatches the implementation stage for an approved plan. Not for top-level user requests or primary capability routing."
---

# code-execute

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/code-execute.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info code-execute`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Read `capabilities/code-execute.md` for the runtime-neutral contract.
2. Run `adapters/codex/bin/preflight.sh capability-info code-execute`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `code-execute`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<plan name or path>`
- Portable meaning: Execute a plan step by step, delegate implementation to the development role, and record an execution log.

## Portable Contract

- Invocation semantics: Execute an implementation plan with progress tracking Put environments, caches, external checkouts, temporary work and training checkpoints under the supplied `runtime_dir` or an established external project location. Keep reports/configs and compact provenance in artifacts; use `runtime_dir` or system temporary storage for pytest and tool scratch data. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.



## Projected Portable Details

## Artifact Ownership

Artifact root: `core/CONVENTIONS.md §5.1`; output placement: `§5`.

## Artifact Producer Lifecycle

`code-execute` is a `standard+` stage worker: it never issues its own campaign or
cycle. It receives the owner's open cycle through
`AGENT_ARTIFACT_CAMPAIGN_ID`/`AGENT_ARTIFACT_CYCLE_ID`/`AGENT_ARTIFACT_PRODUCER_ID`/
`AGENT_ARTIFACT_CYCLE_DIR`/`AGENT_ARTIFACT_OUTPUT_DIR` (dispatch env
pass-through), may call `utilities/artifact_producer.py begin --node <node id>`
on the same route to resume that cycle, and writes durable artifacts only inside
`<cycle_dir>/artifacts/<bucket>/...` within its artifact output scope.
`artifact_producer.py` owns the open cycle and its output paths; `finalize` and
`admit-shared` belong to the owner, never to a stage worker. See
`producer_lifecycle` in `capabilities/topologies.json`.

The route's `source/**`, `source-alternative/**`, and `tests/**` write scopes
refer to files in the assigned `route.cwd` worktree. They are not paths beneath
the artifact directory. Declared durable outputs and artifact scopes resolve
under the canonical cycle output directory. A normal single-session mutation
node sealed with `commit_expected: true` commits its own validated changes;
declared sub-session slices remain no-commit and leave the commit to their
owner after the stage gate.

For a `direct` inline route, the depth-0 interactive caller uses the single
public finish command and its exact receipt under `core/WORKFLOW.md §0.5`.
The same section defines current-session OPEN-route reuse after a proved merge
into an integration worktree. Registered owners and workers retain their
runtime terminal path. Stage-dispatch §13.61 and artifact-path-contract §43
govern the proof and the cycle's completion record; a finalized cycle's files stay editable (artifact-path-contract §45).

An index-lock diagnosis is attached only after the original Git failure,
through the shared read-only helper in core/OPERATIONS.md §5.9.
Keep the original result and partial state; never repair or retry the failed
command automatically.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- use DB memory paths, not runtime-native memory files.

## Portable Procedure

### Recover only edits owned by the failed work

Recovery is limited to edits that the failed step, phase, or attempt actually made and that fall within its assigned source scope. A permitted path, safety commit, changed-path list, or `launch_head` comparison helps identify candidates but does not make the file or its full contents task-owned. Include uncommitted edits when evaluating recovery; do not infer ownership from the Git index or commit boundary.

Use the existing old/new step logs, their edit order, and recorded file location or surrounding context to compare each edit with the current contents. Reverse only edits whose ownership and location are clear, in reverse edit order. A matching string alone is not enough when it is repeated, context has changed, or another edit overlaps it. If an edit is ambiguous or missing from the log, preserve it. Also preserve an earlier edit when reversing it would depend on an ambiguous later edit.

Preserve unrelated edits in the same file and in other files, changes from earlier successful steps, staged user content, and untracked files. Do not use a whole-tree or whole-file restore, checkout, reset, stash, clean, or commit-level revert as a fallback. Do not change the index to make it match the worktree; if staged task edits may remain, say recovery is partial and hand off that state.

For files created, deleted, or renamed by the task, reverse the operation only when the current state of every affected path and any surviving contents are clearly attributable to the task. Do not delete a task-created file after unrelated content was added, restore over a recreated path, or replace an untracked rename destination.

When only part of the work can be reversed, or ownership is unclear, preserve the uncertain contents. Use the existing dev log, checklist, and failure summary to state what was reversed, what remains, why it could not be distinguished, the observed index/worktree state, and which dependent steps were skipped. Do not claim complete recovery. Resume by comparing the current contents with those records so an already-reversed edit is not applied again. Continue only with steps independent of the remaining changes; otherwise stop through the existing failure or partial-work handoff.

Apply this procedure to phase recovery, total failure, cascading errors, failed debug verification, and an exhausted dev test retry. A parallel sub-session slice retains its no-Git-write and no-rollback boundary and hands recovery to its owner.


## Workflow Evidence

- Before capability grounding/spec-changing work: `adapters/codex/bin/preflight.sh route code-execute [cwd] [session-id]`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
