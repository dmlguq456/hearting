---
name: code-test
description: "Use only when autopilot-code dispatches implementation verification and evidence recording. Not for top-level user requests or primary capability routing."
---

# code-test

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/code-test.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info code-test`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Read `capabilities/code-test.md` for the runtime-neutral contract.
2. Run `adapters/codex/bin/preflight.sh capability-info code-test`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `code-test`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<plan name, path, or test scope> [--intensity direct|quick|standard|strong|thorough|adversarial]`
- Portable meaning: Verify implementation results in stages and record evidence.

## Portable Contract

- Invocation semantics: Run graduated verification after `code-execute` or on demand to verify code correctness. Intensity-derived rigor scales final verification and test-adequacy review; it does not force a separate parallel QA loop by itself. The capability resolves a plan path, changed-file list, or test scope, runs the applicable test levels, stops on the first failing level, and records durable evidence before reporting a verdict. When the verification target includes a report spectrogram, the graduated levels include the fail-closed figure semantic verifier against its manifest and report. Missing exact 48 kHz full-band metadata, range-compatible claims, shared-scale evidence, or a hash-current visual review is a failed level. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.



## Projected Portable Details

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not.

When invoked from a `standard+` `autopilot-code` stage cycle, write verification
evidence only under `<artifact-root>/plans/<date>_<slug>/test_logs/` and
`_internal/test_reviews/`. Return the final verdict to `code-report`, which owns
`pipeline_summary.md`; the `code-test` stage must not write that report-owned
artifact. Standalone invocations should create or reuse an appropriate
`plans/<date>_<slug>/` work-cycle directory and may update their own standalone
summary when no `code-report` stage exists.

Required evidence:

- test target resolution: plan path, changed-file list, or inferred scope;
- command log or explicit skip/block reason for each attempted level;
- failing level and first actionable error when verification fails;
- final one-line verdict suitable for handoff to `code-report`.

## Artifact Producer Lifecycle

`code-test` is a `standard+` stage worker: it never issues its own campaign or
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

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`.
Concrete model names, subagent frontmatter, and runtime-specific tool lists
belong in adapter files.

Minimum role mapping:

- verification: QA role using `roles/units/qa/test.md`;
- review: optional QA reviewer for test adequacy when selected by QA/intensity risk;
- reporting: editorial/reporting role only for user-facing summary polish, not
  for changing the test verdict.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- enforce git/worktree safety before edits;
- enforce artifact ordering before new durable artifacts;
- enforce spec-read gating when this capability changes spec-backed code or specs;
- use DB memory paths, not runtime-native memory files.

Additional test-entry gates:

- run `roles/units/qa/test.md` semantics or the adapter-native projection of
  that mode before claiming verification;
- if the adapter reports a `verification-runner` tool contract, run its
  contract check or report unavailable;
- do not modify source files while acting in `code-test`;
- do not claim independent QA review unless a separate QA role, headless worker,
  or external reviewer actually ran.

## Portable Procedure

1. Resolve the verification target:
   - if a plan path is provided, read the plan's verification section and the
     corresponding checklist/changed-file evidence;
   - if changed files are provided, use them directly;
   - otherwise infer recent changed files from git state and report the
     inference.
2. Select the applicable graduated levels from `roles/units/qa/test.md`:
   syntax, import, smoke, functional, integration, and behavioral runtime
   observation for user-facing surfaces.
   If changed outputs include a spectrogram report, also run the fail-closed
   figure semantic verifier against its manifest and report.
3. Run each applicable level in order and stop on the first failure.
4. Record commands, outputs or excerpts, skips, blockers, and the first
   actionable failure in `test_logs/`.
5. Emit the final verdict as a handoff to `code-report`; in a `standard+` stage
   cycle, `code-report` alone updates `pipeline_summary.md`. When no
   `code-report` stage exists, a standalone invocation may update its own
   standalone work-cycle summary.
6. Return a concise report path plus verdict to the caller.


## Required Guards

- Before edits: `adapters/codex/bin/preflight.sh write <file> [session-id]`
- Before capability grounding/spec-changing work: `adapters/codex/bin/preflight.sh route code-test [cwd] [session-id]`
- Before spec-changing work: `adapters/codex/bin/preflight.sh capability code-test [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/codex/bin/preflight.sh read <prd.md> [session-id]`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
