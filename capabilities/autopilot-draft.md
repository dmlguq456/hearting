# Capability: autopilot-draft

This is the portable capability contract for `autopilot-draft`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `autopilot-draft` |
| Group | `entry` |
| Supported modes | `paper, presentation, doc` |
| Portable meaning | Draft independent documents through strategy, verification, and editing. |
| Argument shape | `<task description> [--mode paper\|presentation\|doc] [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial] [--user-refine] [--no-clarify] [--from analyze\|strategy\|strategy-refine\|draft\|draft-refine\|finalize]` |
| Execution topology | `staged`; registry `capabilities/topologies.json` |
| Entry load phase | `post-approval`; owner contract `capabilities/autopilot-draft.md` |

## Invocation Semantics

Document draft pipeline: analyze → strategy → strategy-refine → draft →
draft-refine → finalize. In `paper` mode, “draft” means a **paste-ready
cheatsheet draft**: LaTeX-ready cards describing mutations that the user applies
to canonical `main.tex` through autopilot-apply, not blank-page body writing.
This meaning is unchanged for new and existing papers. Output-form modes are
`paper` (LaTeX academic cheatsheet), `presentation` (slide-by-slide PPT
markdown), and `doc` (Word/HWP/Markdown prose such as reports, proposals,
rebuttals, reviews, blogs, and memos). The mode is form-first; the natural
language task describes purpose/genre without a subtype enum. Discover inputs
from `<artifact-root>/{analysis_project,research}/*`; preprocess external
materials with `/analyze-project --mode {paper|doc}`. Load matching format specs
from `analysis_project/doc/{matching}/formats/` automatically.
Mode conventions live under `## Mode-Specific Conventions` (common plus paper,
presentation, or doc). Presentation mode produces Markdown only; PPTX export is
unsupported, so use PowerPoint directly.

When a draft contains generated spectrograms, finalization requires the report
figure evidence contract in `core/CONVENTIONS.md §4.1`: a semantic manifest,
the fail-closed verifier result, claim-to-range evidence, and at least one
recorded representative PNG review. A file/count/link-only check cannot satisfy
this gate.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Post-Frame Direction Gate

**One gate, raised from the frame legs (SD-123/SD-129).** A `quick+` route seals `human_gates: ["frame-review"]` and both
`frame` and `frame-alternative` continuations as that human gate, bound at
`one-shot`'s entry for `quick` and `material-strategy`'s entry for `standard+`. `frame-review` is the recipe's only human gate:
the old `user-refine-disposition` declaration is retired, because it named a
terminal-position binding that no node ever raised and no launch surface ever
checked. A route sealed before this cycle keeps its own generation's gate
name, binding and node shape and is **never retro-fitted** — the entry fence
in `utilities/dispatch_contract.py` reads only the route object it was handed.

Depth-0 launches the frame pair (both legs `top`, a pin or explicit profile wins; a route composed from an approved proposal via `--route-plan`, or as a `solo` or `staged` compose, has no frame nodes), joins both direction briefs, and builds
`shards/frame/frame-summary.json` (five fields — 방향/대안/위험/범위 변경/비용,
≤1KB) plus the **frame interview** `shards/frame/interview.json` (SD-129: a
one-sentence restatement the user confirms, a plain-language brief, and at
most `frame_interview.py`'s `QUESTION_CAP` short questions — one topic each,
2–4 options, one recommended, no harness vocabulary, only decisions the user
alone can make; `utilities/frame_interview.py validate` is the bar and
`gate --block` refuses what fails it). Depth-0 puts those questions to the
user; the receipt's `resume_command --answers <file>` validates the answers, renders
`shards/frame/intent.md`, releases the gate and starts the owner.

The owner **receives** `intent.md`'s path as an input. It raises no gate,
waits on no release, and renders no intent of its own — all of that is
finished before it is launched. `intent.md` is the agreed intent
`material-strategy` reads first (genre, audience, claim scope, evidence the
user considers in-bounds), so a strategy that contradicts a recorded decision
is a `strategy-review` blocker; pass its absolute path in the
`material-strategy` prompt as `Intent:`. `revise` re-runs the frame pair
before owner launch and `stop` cancels the prepared workflow, so
neither consumes the owner's retry boundary. A `material-strategy` start whose
entry gate is not released is refused by every launch surface
(`human-gate-unreleased`). `direct` has no gate: the depth-0 session asks its
one question of the same kind inline inside the §0.4 card step — a documented
obligation on the acting session, not a machine-checked cap, since a `direct`
route carries no gate binding. The declared `confirmation.mode` (default
`hybrid`) governs the ordered pair — blocking direction gate first, route
notice after; `core/WORKFLOW.md` §0.4 owns the user-facing card.

## Artifact Ownership

Artifact root: `core/CONVENTIONS.md §5.1`; output placement: `§5`.

## Artifact Producer Lifecycle

W7C write-cutover contract (`utilities/artifact_producer.py`, registry table
`producer_lifecycle` in `capabilities/topologies.json`). The same lifecycle
binds `direct`, `quick`, and `standard+`; only the acting owner differs.

1. **begin before the first write.** After route compile/bind, depth-0 runs
   `begin` before either frame leg starts; the later owner inherits that cycle.
   Without frame, the acting owner (inline for `direct`, depth-1 otherwise)
   begins the cycle. The command is `artifact_producer.py begin --artifact-root
   <root> --route <route file> --capability autopilot-draft --intensity <intensity>`.
   While the cutover is inactive this returns `legacy-compat` and the legacy
   `<artifact-root>/documents/` layout stays writable; once active it
   issues `campaign_id`/`cycle_id`/`producer_id` and the cycle directory
   `campaigns/<campaign-locator>/<cycle-locator>/artifacts/` before any artifact exists.
2. **write only inside the open cycle.** Every durable artifact goes under
   `<cycle_dir>/artifacts/documents/...` (`AGENT_ARTIFACT_OUTPUT_DIR`).
   `artifact_producer.py` owns cycle output paths and shared revisions.
3. **stage workers join, never fork.** `standard+` stage workers receive
   `AGENT_ARTIFACT_CAMPAIGN_ID`/`CYCLE_ID`/`PRODUCER_ID`/`CYCLE_DIR`/`OUTPUT_DIR`
   from the owner (dispatch env pass-through) and call `begin --node <id>`
   on the same route, which resumes the owner's open cycle.
4. **runtime-owned closure.** A new registered owner returns its final report;
   the shared completion controller owns workflow/route closure and exact-cycle
   sealing after PASS and process cleanup. Interrupted closure retains the
   result and transaction, retries without a model turn, and carries a recovery
   notice. `roles/worker-types/owner.md` defines this shared contract. Explicit
   close/finalize commands remain for inline work and legacy recovery.
5. **shared admission.** This capability's output is cycle-local; it is never admitted to `shared/` (only `spec`, `analysis`, and explicitly promoted `research` are shared kinds).

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

Pipeline intensity follows `core/CONVENTIONS.md §1`: `direct` has no plan stage or durable plan artifact; `quick` is one registered-headless dispatch-depth-1 one-shot conductor with its inline micro-plan plus plan-check-lite; `standard+` uses the capability's durable work-cycle plan when applicable. This recipe has no separate `plan-check` node (only `autopilot-code` declares one): `quick` checks its micro-plan inline (plan-check-lite), and `standard+` reviews through the recipe's own review stages — `strategy-review`, `quality-review`, and `fact-verify` — rather than after every stage. Verification rigor for those reviews and final verify is derived from intensity; it does not name a model or introduce a separate stage graph. `capabilities/topologies.json` (recipe plus `part_catalog`) is the one stage list; `capability-route.py stages --capability autopilot-draft` prints it.

## Guard Requirements

At `thorough+` the `quality-review` parallel group realizes a third
`assumption-check` leg with `leg_class: auxiliary`. Its arbiter is the **owner**,
not the group's anchor — the anchor runs concurrently with it. After the group
joins, the owner puts `auxiliary_findings_considered` in the merge record's
frontmatter with exactly one entry per realized auxiliary leg (adopted or
rejected, with the reason) and registers it with
`capability-route.py arbitrate --group quality-review`. Until that record exists,
`finalize` is refused at the start-gate with `auxiliary-arbitration-missing` and
the route's terminal-gate observation carries a failed
`parallel_group:quality-review` row. `core/OPERATIONS.md §5.10` owns the
transaction and its typed refusals.

When a draft consumes lab media, it consumes the shared `report_manifest.json` and
preserves its declared bundle roles, primary representation, and summary-stat bindings; for
a manifest with no `bundle` it preserves the legacy Markdown/HTML link bindings unchanged.
It does not create a second media manifest.

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- use DB memory paths, not runtime-native memory files.

## Routing Boundary

Choose draft for an independent document goal with its own audience and brief,
such as a new paper, presentation, proposal, or standalone report. The experiment's
own evaluation, comparisons, interpretation, limitations, and conclusion report
remain `autopilot-lab --mode eval`, even with fixed results and no new measurements.
Prose, Markdown, HTML, or sharing the report does not by itself create a draft
primary or a separate cycle. Use the owning capability's existing report/editorial
stage for internal writing; consume lab results here only for the independent
document goal. Corrections to an existing paper belong to refine/apply.

After finalization makes the canonical `final-artifact` durable, evaluate the
route-sealed optional artifact-sink extension. When available, offer the
artifact through the app-neutral receipt contract. When unavailable, record
`skipped/extension-unavailable`; do not fail or duplicate the finalized
document.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/autopilot-draft/SKILL.md` and `skills/autopilot-draft/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-draft/SKILL.md`, while `skills/autopilot-draft/SKILL.md` remains a generated compatibility projection sourced from `capabilities/` and kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info autopilot-draft`. Use `adapters/codex/skills/autopilot-draft/SKILL.md` as the native Codex Skill projection; do not consume `skills/autopilot-draft/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info autopilot-draft`. Use `adapters/opencode/skills/autopilot-draft/SKILL.md` and `adapters/opencode/commands/autopilot-draft.md` as native OpenCode projections; do not consume `skills/autopilot-draft/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/autopilot-draft/SKILL.md` and `adapters/claude/skills/autopilot-draft/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-draft/SKILL.md`, while `skills/autopilot-draft/SKILL.md` remains a generated compatibility projection sourced from `capabilities/` and kept for parity/drift checks.
