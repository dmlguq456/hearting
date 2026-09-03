# Capability: code-test

This is the portable capability contract for `code-test`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `code-test` |
| Group | `sub` |
| Supported modes | `none` |
| Portable meaning | Verify implementation results in stages and record evidence. |
| Argument shape | `<plan name, path, or test scope> [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial]` |

## Invocation Semantics

Run graduated verification after `code-execute` or on demand to verify code
correctness. Intensity-derived rigor scales final verification and test-adequacy review; it does
not force a separate parallel QA loop by itself. The capability resolves a plan path, changed-file list, or test
scope, runs the applicable test levels, stops on the first failing level, and
records durable evidence before reporting a verdict.

When the verification target includes a report spectrogram, the graduated
levels include the fail-closed figure semantic verifier against its manifest
and report. Missing exact 48 kHz full-band metadata, range-compatible claims,
shared-scale evidence, or a hash-current visual review is a failed level.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Assurance Contract

`code-test` realizes the final `verify` stage for code work. It is concrete verification, not a mandatory second QA pipeline. Intensity-derived rigor changes command breadth, evidence requirements, and whether a separate test-adequacy/security/adversarial review is opened. `quick` may run one focused verify-lite command; `standard+` runs the applicable graduated levels; `thorough|adversarial` may add adequacy, runtime-observation, security, or external adversary review only when the selected intensity/risk calls for it.


## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not.

When invoked from a `standard+` `autopilot-code` stage cycle, write verification
evidence only under `$AGENT_ARTIFACT_OUTPUT_DIR/plans/<date>_<slug>/test_logs/` and
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

## Tool Contract Mapping

Adapters with an executable verification surface should expose it as
`verification-runner`. The contract means explicit verification commands are
run through an adapter-owned launcher that records runtime metadata and can
report `unavailable` without silently pretending tests passed.

Adapters without such a launcher must still follow the portable procedure, but
they must mark the executable tool contract as unsupported or unavailable.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/code-test/SKILL.md` and `skills/code-test/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/code-test/SKILL.md`, while `skills/code-test/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info code-test`. Use `adapters/codex/skills/code-test/SKILL.md` as the native Codex Skill projection; do not consume `skills/code-test/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info code-test`. Use `adapters/opencode/skills/code-test/SKILL.md` and `adapters/opencode/commands/code-test.md` as native OpenCode projections; do not consume `skills/code-test/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/code-test/SKILL.md` and `adapters/claude/skills/code-test/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/code-test/SKILL.md`, while `skills/code-test/SKILL.md` remains the compatibility reference kept for parity/drift checks.
