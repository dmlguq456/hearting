# Capability: analyze-project

This is the portable capability contract for `analyze-project`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `analyze-project` |
| Group | `pre` |
| Supported modes | `code, paper, doc` |
| Portable meaning | Creates persistent analysis of existing code, papers, or documents; initial analysis defaults here unless read-only/no-file or another primary applies. |
| Argument shape | `[--mode code\|paper\|doc] [<scope/target/input-folder>] [--skip-qa]` |
| Entry load phase | `post-approval`; owner contract `capabilities/analyze-project.md` |

## Invocation Semantics

Pre-work analysis capability — analyzes the project's primary materials and
writes structured artifacts to `$AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/`. Invoke it
when the user explicitly asks to analyze existing code, a paper, or document
materials and no usable persistent analysis exists, when existing analysis is
demonstrably stale for the requested downstream work, or when the user asks to
refresh it. An explicit analysis request defaults to persistent output unless
the user asks for conversational/read-only analysis or no files. A request only
to understand the current project, recover prior context, resume work, or report
status remains read-only
orientation and is not an `analyze-project` trigger by itself. When analysis
already exists, read it before deciding that reanalysis is needed.

That orientation starts with one targeted, agent-chosen memory recall; reads a
shortened relevant hit in full by record ID; prefers `.agent_reports/` and uses
`.claude_reports/` only when the canonical root is absent; then reads the
newest report/experiment artifact with its current PRD/spec before primary
code or data. Resolve drift as latest spec or user confirmation, durable
project fact, latest experiment contract, then legacy document, and report the
conflict instead of silently selecting the older value.

Three modes are available: code (codebase), paper (academic PDFs), and doc
(miscellaneous document materials such as reviewer comments, format templates,
samples, and internal notes). Mode auto-detects between code and doc when
omitted; paper requires explicit `--mode paper`. Output is the persistent input
source for downstream `autopilot-{draft,code,research}` capabilities.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not. Capability-specific output placement follows `core/CONVENTIONS.md` section 5 until this spec is expanded with a stricter per-capability artifact map.

## Artifact Producer Lifecycle

W7C write-cutover contract (`utilities/artifact_producer.py`, registry table
`producer_lifecycle` in `capabilities/topologies.json`). The same lifecycle
binds `direct`, `quick`, and `standard+`; only the acting owner differs.

1. **begin before the first write.** After the route is compiled and bound,
   the owner (the inline session for `direct`, the dispatch-depth-1 owner for
   `quick` and `standard+`) runs `artifact_producer.py begin --artifact-root
   <root> --route <route file> --capability analyze-project --intensity <intensity>`.
   While the cutover is inactive this returns `legacy-compat` and the legacy
   `<artifact-root>/analysis_project/` layout stays writable; once active it
   issues `campaign_id`/`cycle_id`/`producer_id` and the cycle directory
   `campaigns/<campaign-locator>/<cycle-locator>/artifacts/` before any artifact exists.
2. **write only inside the open cycle.** Every durable artifact goes under
   `<cycle_dir>/artifacts/analysis_project/...` (`AGENT_ARTIFACT_OUTPUT_DIR`).
   `artifact_producer.py check-write` is the single allow/deny oracle used by
   `hooks/artifact-guard.sh`; an active cutover hard-denies new legacy
   top-level writes, and `shared/` is immutable in both states.
3. **stage workers join, never fork.** `standard+` stage workers receive
   `AGENT_ARTIFACT_CAMPAIGN_ID`/`CYCLE_ID`/`PRODUCER_ID`/`CYCLE_DIR`/`OUTPUT_DIR`
   from the owner (dispatch env pass-through) and call `begin --node <id>`
   on the same route, which resumes the owner's open cycle.
4. **finalize after route closure.** The owner runs `artifact_producer.py
   finalize --artifact-root <root> --cycle <cycle_id>` once the route is
   closed: it enumerates `artifacts/`, builds and validates the D-6 manifest,
   commits `manifest.json` (the commit point), applies the index, and seals
   the cycle record. Empty output leaves no lineage (D-9). `recover` rolls a
   crashed finalize forward or back from its journal.
5. **shared admission.** `analysis_project` output is admitted to `shared/analysis/` by `admit-shared --kind analysis` after the cycle is sealed (canonical shared kind).

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- enforce git/worktree safety before edits;
- enforce artifact ordering before new durable artifacts;
- enforce spec-read gating when this capability changes spec-backed code or specs;
- use DB memory paths, not runtime-native memory files.

## Routing Boundary

Before invocation, follow `core/WORKFLOW.md §0.1`: run one targeted,
agent-chosen memory recall and read any shortened relevant hit in full by
record ID; prefer `.agent_reports/`, falling back to `.claude_reports/` only
when the canonical root is absent; then inspect the newest report and
experiment artifacts plus current PRD/spec before checking primary code or
data. This order is context recovery, not persistent reanalysis.

For read-only orientation, do not invoke this capability and do not create or
update `analysis_project/`. Follow relevant memory paths and resolve drift with
this precedence: latest specification or user-confirmed decision, durable
project fact, latest experiment contract, then legacy document. Report a
conflict instead of silently combining or selecting the older value.

Artifact absence alone is not a trigger. New empirical evaluation belongs to
`autopilot-lab`, new external evidence surveys to `autopilot-research`, source
implementation to `autopilot-code`, and inspection of completed work to
`audit`. Apply those semantic primaries before this initial-analysis default.

After persistent analysis becomes durable, evaluate the optional artifact-sink
extension from `WORKFLOW §0.2`. When the sink is available, offer the canonical
analysis artifact through the app-neutral receipt contract. When it is absent,
record `skipped/extension-unavailable` without changing analysis completion.
Because `analyze-project` is a pre-capability rather than an entry recipe, this
extension point lives in this portable contract instead of
`capabilities/topologies.json`.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/analyze-project/SKILL.md` and `skills/analyze-project/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/analyze-project/SKILL.md`, while `skills/analyze-project/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info analyze-project`. Use `adapters/codex/skills/analyze-project/SKILL.md` as the native Codex Skill projection; do not consume `skills/analyze-project/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info analyze-project`. Use `adapters/opencode/skills/analyze-project/SKILL.md` and `adapters/opencode/commands/analyze-project.md` as native OpenCode projections; do not consume `skills/analyze-project/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/analyze-project/SKILL.md` and `adapters/claude/skills/analyze-project/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/analyze-project/SKILL.md`, while `skills/analyze-project/SKILL.md` remains the compatibility reference kept for parity/drift checks.
