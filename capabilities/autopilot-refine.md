# Capability: autopilot-refine

This is the portable capability contract for `autopilot-refine`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `autopilot-refine` |
| Group | `entry` |
| Supported modes | `none` |
| Portable meaning | Correct and update existing document/research artifacts while preserving snapshots and change history. |
| Argument shape | `"<prompt>" [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial] [--review-only \| --memo <file>] [--confirm] [--no-fact-check] [--no-style-audit]` |
| Execution topology | `transactional-owner`; registry `capabilities/topologies.json` |
| Entry load phase | `post-approval`; owner contract `capabilities/autopilot-refine.md` |

## Invocation Semantics

Autopilot family — post-creation iteration pipeline for research and doc artifacts (NOT code). Prompt-driven: target artifact identified via prompt fuzzy match against `<artifact-root>/{research,documents}/*`, then auto-discovers the artifact's file structure, plans edits, shows a diff preview in chat, and on user confirm applies edits with versioning + integrated history logging in `pipeline_summary.md` (single source of truth — no separate CHANGELOG). Default intensity is `quick` (one registered conductor). Its sealed `inline_human_gates` declares `preview-disposition`, bound at the one-shot terminal boundary and enforced before any target-artifact write. The conductor writes `reviews/refine/preview.md`, raises and awaits the gate, then applies within the same attempt only after the person releases it; escalate intensity to `standard|strong|thorough|adversarial` for multi-round review, fact-check, or external adversary work. Optional `--memo <file>` falls back to file-memo style for deferred reviews.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Post-Frame Direction Gate

**One gate, raised from the frame legs (SD-123/SD-129).** A `quick+` route seals `human_gates: ["frame-review", "preview-disposition"]` and both
`frame` and `frame-alternative` continuations as that human gate, bound at
`one-shot`'s entry for `quick` and `review`'s entry for `standard+`. `frame-review` is the recipe's one **direction** gate.

**Preview approval stays (user decision, 2026-09-10).** `preview-disposition`
is not a direction: it is the approval refine was built around — plan edits,
show the diff preview, apply only on the user's confirm. `review` keeps its
`human-gate` continuation naming `preview-disposition`, bound at
`transaction`'s entry, and the gate is fenced (`FENCED_HUMAN_GATES`), so
`transaction` cannot start before a release. After `review`, the owner writes
the diff preview to `reviews/refine/preview.md`, raises the gate with
`workflow-supervisor.py gate --route <route> --gate preview-disposition --block
--artifact <absolute path to preview.md>`, and waits on
`workflow-supervisor.py await-release --route <route> --gate preview-disposition`
(exit 0 proceed, 3 revise, 4 stop) — never polling, never self-releasing.
Depth-0 shows the preview and records `workflow-supervisor.py release --route
<route> --gate preview-disposition --decision proceed|revise|stop --actor user`.
A registered owner cannot release `preview-disposition` itself, including
when an older raise recorded no release authority.
Before this change the gate was declared but never fenced and no document told
an owner to raise it, so no refine route ever actually waited on it. A route
sealed before this cycle keeps its own generation's gate name, binding and
node shape and is **never retro-fitted** — the entry fence in
`utilities/dispatch_contract.py` reads only the route object it was handed.

Depth-0 launches the frame pair, joins both direction briefs, and builds
`shards/frame/frame-summary.json` (five fields — 방향/대안/위험/범위 변경/비용,
≤1KB) plus the **frame interview** `shards/frame/interview.json` (SD-129: a
one-sentence restatement the user confirms, a plain-language brief, and at
most `frame_interview.py`'s `QUESTION_CAP` short questions — one topic each,
2–4 options, one recommended, no harness vocabulary, only decisions the user
alone can make; `utilities/frame_interview.py validate` is the bar and
`gate --block` refuses what fails it). For this capability the restatement
names the target artifact and the scope of change the user is authorizing.
Depth-0 puts those questions to the user, records the answers with
`workflow-supervisor.py release --gate frame-review --decision
proceed|revise|stop --answers <file>`, and renders `shards/frame/intent.md`
with `frame_interview.py render-intent`.

The owner **receives** `intent.md`'s path as an input. Depth-0 has already
finished the frame interview and rendered intent. The owner later raises and
waits for the separate preview approval before applying edits. `intent.md` is the agreed intent `review`
reads first, so a verdict that proposes edits outside the recorded scope is a
blocking finding rather than a silent change; pass its absolute path in the
`review` prompt as `Intent:`. `revise` re-runs the frame pair before owner
launch and `stop` cancels the prepared workflow, so neither consumes the
owner's retry boundary. A `review` start whose entry gate is not released is
refused by every launch surface (`human-gate-unreleased`). The diff preview
`review` produces stays a report the owner shows before `transaction` applies
it; this remains a separate human approval gate. `direct` has no gate: the depth-0
session asks its one question of the same kind inline inside the §0.4 card
step — a documented obligation on the acting session, not a machine-checked
cap, since a `direct` route carries no gate binding. The declared
`confirmation.mode` (default `hybrid`) governs the ordered pair — blocking
direction gate first, route notice after; `core/WORKFLOW.md` §0.4 owns the
user-facing card.

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not. Capability-specific output placement follows `core/CONVENTIONS.md` section 5 until this spec is expanded with a stricter per-capability artifact map.

## Artifact Producer Lifecycle

W7C write-cutover contract (`utilities/artifact_producer.py`, registry table
`producer_lifecycle` in `capabilities/topologies.json`). The same lifecycle
binds `direct`, `quick`, and `standard+`; only the acting owner differs.

1. **begin before the first write.** After route compile/bind, depth-0 runs
   `begin` before either frame leg starts; the later owner inherits that cycle.
   Without frame, the acting owner (inline for `direct`, depth-1 otherwise)
   begins the cycle. The command is `artifact_producer.py begin --artifact-root
   <root> --route <route file> --capability autopilot-refine --intensity <intensity>`.
   While the cutover is inactive this returns `legacy-compat` and the legacy
   `<artifact-root>/target/` layout stays writable; once active it
   issues `campaign_id`/`cycle_id`/`producer_id` and the cycle directory
   `campaigns/<campaign-locator>/<cycle-locator>/artifacts/` before any artifact exists.
2. **write only inside the open cycle.** Every durable artifact goes under
   `<cycle_dir>/artifacts/target/...` (`AGENT_ARTIFACT_OUTPUT_DIR`).
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
5. **shared admission.** This capability's output is cycle-local; it is never admitted to `shared/` (only `spec`, `analysis`, and explicitly promoted `research` are shared kinds).

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

Pipeline intensity follows `core/CONVENTIONS.md §1`: `direct` has no plan stage or durable plan artifact; `quick` is one registered-headless dispatch-depth-1 one-shot conductor with its inline micro-plan plus plan-check-lite; `standard+` uses the capability's durable work-cycle plan when applicable. `plan-check` is required for every non-`direct` graph, but independent QA is not repeated after every stage by default. Verification rigor for plan-check, selected independent reviews, and final verify is derived from intensity; it does not name a model or introduce a separate stage graph.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- enforce git/worktree safety before edits;
- enforce artifact ordering before new durable artifacts;
- enforce spec-read gating when this capability changes spec-backed code or specs;
- use DB memory paths, not runtime-native memory files.

## Routing Boundary

`autopilot-refine` corrects and updates existing document and research
artifacts. A direct minor edit updates history without a snapshot. Every
non-direct major rewrite of an existing file is pre-snapshotted by the artifact
write guard into one route-bound `_internal/versions/v{N}/` directory; the
model does not allocate or copy versions. The abstract `target-artifact` write
scope resolves only to `documents/<artifact>/**` and `research/<artifact>/**`.
It never owns new empirical work: under `WORKFLOW §0.2`, a request
that also requires reevaluation, new metrics, or new figure/media generation
routes that work to `autopilot-lab` (or the owning execution capability) as
primary, with refine as a secondary document pass over the finalized results.
Blueprint or evaluation-policy changes belong to `autopilot-spec` update.

After the confirmed transaction, evaluate the route-sealed optional
artifact-sink extension with the canonical revised artifact—not the snapshot or
diff preview. When available, offer it through the app-neutral receipt
contract. When unavailable, record `skipped/extension-unavailable` and leave
refinement complete.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/autopilot-refine/SKILL.md` and `skills/autopilot-refine/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-refine/SKILL.md`, while `skills/autopilot-refine/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info autopilot-refine`. Use `adapters/codex/skills/autopilot-refine/SKILL.md` as the native Codex Skill projection; do not consume `skills/autopilot-refine/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info autopilot-refine`. Use `adapters/opencode/skills/autopilot-refine/SKILL.md` and `adapters/opencode/commands/autopilot-refine.md` as native OpenCode projections; do not consume `skills/autopilot-refine/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/autopilot-refine/SKILL.md` and `adapters/claude/skills/autopilot-refine/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-refine/SKILL.md`, while `skills/autopilot-refine/SKILL.md` remains the compatibility reference kept for parity/drift checks.
