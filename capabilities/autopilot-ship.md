# Capability: autopilot-ship

This is the portable capability contract for `autopilot-ship`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `autopilot-ship` |
| Group | `entry` |
| Supported modes | `none` |
| Portable meaning | Prepare application deployment/release setup and a ship checklist. |
| Argument shape | `<task description (optional)> [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial]` |
| Execution topology | `transactional-owner`; registry `capabilities/topologies.json` |
| Entry load phase | `post-approval`; owner contract `capabilities/autopilot-ship.md` |

## Invocation Semantics

Application deployment-setup entrypoint for projects with an existing `spec/`
and substantially complete functionality. Guide the first ship setup,
environment, domain, and migration deployment; select hosting (Vercel, Fly,
Railway, Cloudflare, or EAS); create CI/CD files, `.env.example`, domain guidance,
and a deployment record. The user runs real deployment commands; this skill
provides guidance only. Keep it distinct from autopilot-spec's initial
spec/skeleton work. It may be rerun for environment changes, added domains, or
production migration deployment.

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
   <root> --route <route file> --capability autopilot-ship --intensity <intensity>`.
   While the cutover is inactive this returns `legacy-compat` and the legacy
   `<artifact-root>/release-config/` layout stays writable; once active it
   issues `campaign_id`/`cycle_id`/`producer_id` and the cycle directory
   `campaigns/<camp>/cycles/<cyc>/artifacts/` before any artifact exists.
2. **write only inside the open cycle.** Every durable artifact goes under
   `<cycle_dir>/artifacts/release-config/...` (`AGENT_ARTIFACT_OUTPUT_DIR`).
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

## Stage Graph and Deploy Authorization

The `standard+` graph is `release-setup → {security-review, release-review} →
deploy → post-deploy-verify`. Both readiness reviews declare a `human-gate`
continuation naming `deploy-authorization`, which binds to `deploy` as an entry
gate: readiness passing never starts a deployment by itself. `deploy` records the
authorized deployment, and `post-deploy-verify` is the terminal node — the
workflow is not complete when the deploy command returns, only when the
post-deploy verification gate holds.

At `adversarial` the `security-review` group realizes a third
`failure-mode-check` leg with `leg_class: auxiliary`. Its arbiter is the
**owner**, not the group's anchor — the anchor runs concurrently with it. After
the group joins, the owner puts `auxiliary_findings_considered` in the merge
record's frontmatter with exactly one entry per realized auxiliary leg (adopted
or rejected, with the reason) and registers it with
`capability-route.py arbitrate --group security-review`. `deploy` is a
`capability-owner` node, so it does not pass a wrapper start-gate: here the
enforcement is the route's terminal-gate observation, which carries a failed
`parallel_group:security-review` row and holds `terminal_gate_proven` false
until the record exists. The human `deploy-authorization` gate is a separate
axis and does not substitute for it. `core/OPERATIONS.md §5.10` owns the
transaction and its typed refusals.

**Open, and owned by the spec, not by this document.** `deploy` is the one node
in the registry that takes an irreversible external action, and the terminal-gate
row is a POST-HOC observation, not a pre-action gate: an unarbitrated
`security-review` leaves `terminal_gate_proven` false and blocks publication
*after* the deploy has already gone out. Nothing reads the `failure-mode-check`
findings before the irreversible step. Closing that needs a gate shape the
current contract does not have — a `capability-owner` node passes no wrapper
start-gate — so it is a spec decision about gate authority, not something to
invent here. Until it is decided, the owner checks the arbitration record itself
before running `deploy`; that is procedure, and this paragraph says plainly that
it is not enforcement.

Before this graph existed, `deploy-authorization` was declared with no node that
realized it and both readiness reviews were terminal, so a ship route could
"complete" with nothing deployed and nothing verified. `core/WORKFLOW.md §0.6`
now rejects a declared human gate that binds to no node.

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
| Claude Code | `adapters/claude/skills/autopilot-ship/SKILL.md` and `skills/autopilot-ship/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-ship/SKILL.md`, while `skills/autopilot-ship/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info autopilot-ship`. Use `adapters/codex/skills/autopilot-ship/SKILL.md` as the native Codex Skill projection; do not consume `skills/autopilot-ship/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info autopilot-ship`. Use `adapters/opencode/skills/autopilot-ship/SKILL.md` and `adapters/opencode/commands/autopilot-ship.md` as native OpenCode projections; do not consume `skills/autopilot-ship/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/autopilot-ship/SKILL.md` and `adapters/claude/skills/autopilot-ship/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-ship/SKILL.md`, while `skills/autopilot-ship/SKILL.md` remains the compatibility reference kept for parity/drift checks.
