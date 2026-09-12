# Capability: autopilot-design

This is the portable capability contract for `autopilot-design`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `autopilot-design` |
| Group | `entry` |
| Supported modes | `none` |
| Portable meaning | Visual-design pipeline coordinating references→tokens→components→review→handoff. |
| Argument shape | `<design task or app path> [--scope ui\|webapp\|slide\|icon\|diagram\|mixed] [--artifact standalone\|project] [--from <phase>] [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial]` |
| Execution topology | `staged`; registry `capabilities/topologies.json` |
| Entry load phase | `post-approval`; owner contract `capabilities/autopilot-design.md` |

## Invocation Semantics

Unified design pipeline — orchestrates design-init → design-refs → design-tokens → design-components → design-review → design-handoff. For visual artifacts across UI/UX, slides, diagrams, icons, logos. Can be invoked standalone or auto-delegated from autopilot-spec Phase 2. Distinct from autopilot-draft (text-only documents) — autopilot-design handles visual deliverables. A runtime design harness must render every output for visual self-verification (preview/screenshot/console/eval_js/view_image where supported), run a separate-context verifier gate for console/layout breakage, apply shared design rules and reusable scaffold assets, and support PDF/PPTX/single-HTML bundle export where available. Outputs can be a self-contained single-file HTML preview viewable without any project stack.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Post-Frame Direction Gate

**One gate, raised from the frame legs (SD-123/SD-129).** A `quick+` route seals `human_gates: ["frame-review"]` and both
`frame` and `frame-alternative` continuations as that human gate, bound at
`one-shot`'s entry for `quick` and `refs`'s entry for `standard+`. `frame-review` is the recipe's only human gate: the old
`direction-confirmation` continuation on `refs` is retired, because a route
must have exactly one place a person is asked for direction. A route sealed
before this cycle keeps its own generation's gate name, binding and node
shape and is **never retro-fitted** — the entry fence in
`utilities/dispatch_contract.py` reads only the route object it was handed.

Depth-0 launches the frame pair, joins both direction briefs, and builds
`designs/<cycle>/01_refs/frame-summary.json` (five fields — 방향/대안/위험/범위
변경/비용, ≤1KB) plus the **frame interview**
`designs/<cycle>/01_refs/frame/interview.json` (SD-129: a one-sentence
restatement the user confirms, a plain-language brief, and at most
`frame_interview.py`'s `QUESTION_CAP` short questions — one topic each, 2–4
options, one recommended, no harness vocabulary, only decisions the user alone
can make; `utilities/frame_interview.py validate` is the bar and `gate --block`
refuses what fails it). Depth-0 puts those questions to the user, records the
answers with `workflow-supervisor.py release --gate frame-review --decision
proceed|revise|stop --answers <file>`, and renders
`designs/<cycle>/01_refs/frame/intent.md` with `frame_interview.py
render-intent`.

The owner **receives** `intent.md`'s path as an input. It raises no gate,
waits on no release, and renders no intent of its own — all of that is
finished before it is launched. `intent.md` is the agreed intent `refs` reads
first (genre/audience/claim scope collapse to design's own fields — 방향/대안/
위험/범위 변경/비용 — plus recorded Decisions), so a reference brief that
contradicts a recorded decision is a `design-review` blocker; pass its
absolute path in the `refs` prompt as `Intent:`. `revise` re-runs the frame
pair before owner launch and `stop` cancels the prepared workflow,
so neither consumes the owner's retry boundary. A `refs` start whose entry
gate is not released is refused by every launch surface
(`human-gate-unreleased`). `direct` has no gate: the depth-0 session asks its
one question of the same kind inline inside the §0.4 card step — a documented
obligation on the acting session, not a machine-checked cap, since a `direct`
route carries no gate binding.

The declared `confirmation.mode` (default `hybrid`) governs the ordered pair —
blocking direction gate first, route notice after; `core/WORKFLOW.md` §0.4 owns
the user-facing card.

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
   <root> --route <route file> --capability autopilot-design --intensity <intensity>`.
   While the cutover is inactive this returns `legacy-compat` and the legacy
   `<artifact-root>/designs/` layout stays writable; once active it
   issues `campaign_id`/`cycle_id`/`producer_id` and the cycle directory
   `campaigns/<campaign-locator>/<cycle-locator>/artifacts/` before any artifact exists.
2. **write only inside the open cycle.** Every durable artifact goes under
   `<cycle_dir>/artifacts/designs/...` (`AGENT_ARTIFACT_OUTPUT_DIR`).
   `artifact_producer.py check-write` is the single allow/deny oracle used by
   `hooks/artifact-guard.sh`; an active cutover hard-denies new legacy
   top-level writes, and `shared/` is immutable in both states.
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

Pipeline intensity follows `core/CONVENTIONS.md §1`: `direct` has no plan stage or durable plan artifact; `quick` is one registered-headless dispatch-depth-1 one-shot conductor with its inline micro-plan plus plan-check-lite; `standard+` uses the capability's durable work-cycle plan when applicable. `plan-check` is required for every non-`direct` graph, but independent QA is not repeated after every stage by default. Verification rigor for plan-check, selected independent reviews, and final verify is derived from intensity; it does not name a model or introduce a separate stage graph.

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
| Claude Code | `adapters/claude/skills/autopilot-design/SKILL.md` and `skills/autopilot-design/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-design/SKILL.md`, while `skills/autopilot-design/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info autopilot-design`. Use `adapters/codex/skills/autopilot-design/SKILL.md` as the native Codex Skill projection; do not consume `skills/autopilot-design/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info autopilot-design`. Use `adapters/opencode/skills/autopilot-design/SKILL.md` and `adapters/opencode/commands/autopilot-design.md` as native OpenCode projections; do not consume `skills/autopilot-design/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/autopilot-design/SKILL.md` and `adapters/claude/skills/autopilot-design/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-design/SKILL.md`, while `skills/autopilot-design/SKILL.md` remains the compatibility reference kept for parity/drift checks.
