# CLAUDE.md — Claude Adapter Bootstrap

This is the Claude Code adapter bootstrap, not the portable source of truth.
The semantic hierarchy is `core/capabilities/roles -> {Claude, Codex, OpenCode}`;
the three adapters are siblings. Edit portable sources first.

## Source Order

Read `core/CORE.md` first; load the remaining documents only when the task
touches the named domain.

1. `core/CORE.md`
2. `core/WORKFLOW.md` for routing and tracked work
3. `core/CONVENTIONS.md` for intensity, QA, roles, artifacts, and Skill rules
4. `core/OPERATIONS.md` for git, worktrees, locks, and dispatch
5. `core/MEMORY.md` for memory
6. `capabilities/README.md`, `roles/README.md`, and `roles/MODES.md` for task behavior

For runtime-surface or parity changes, verify current official documentation,
then inspect the local realization and its fallback. Never infer support from
another adapter.

## Runtime Router

- Treat `AGENT_HOME` as the installed harness root.
- Resolve the canonical artifact root with `utilities/artifact-root.sh`; linked worktrees write the primary checkout's `.agent_reports/`, and legacy `.claude_reports/` is only a fallback.
- Use portable model roles, never vendor model names, in shared artifacts.
- Repo-root `skills/` is the canonical Skill authoring tree; `tools/sync-entry-skill-layer.py` projects it into `adapters/claude/skills/` (generated — do not hand-edit the projection). Claude-native hooks, commands, settings, and kernel helper agents live under `adapters/claude/`; behavior personas live in the portable unit catalog `roles/units/`.
- Before adapter edits, read the governing core contract and run the applicable write guard. Before spec changes, read the current PRD and use the spec capability gate.
- Run deterministic guards directly when hook execution is unavailable or untrusted.
- Task-specific detail is progressively disclosed through the selected Skill and adapter README/ADAPTATION docs; do not preload unrelated procedures.
- Call the six runtime-root-sensitive utilities (`capability-route`, `artifact_producer`, `spec-transaction`, `dispatch-owner`, `dispatch-batch`, `dispatch-node`) through the installed `$AGENT_HOME`; a checkout-relative call to one of them is allowed only under dev activation (`AGENT_HOME` is that checkout itself), enforced by `hooks/runtime-root-guard.sh`.
- Peer-session steering (`OPERATIONS §5.14`): watch a peer depth-0 session with the checked `utilities/peer-steward.py wait` (wraps `herdr agent wait`, no self-written poll loop); `peer-steward.py start` defaults a launched child session to `bypass` permissions (`dispatch-defaults.yaml` `steward.child_permission_mode`, opt-out is `inherit`).

## Routing and Execution

Route by `core/WORKFLOW.md §0.2`: when a request matches one manifest
`entry-router` trigger and no exclusion, that entry is the primary route
and `direct` sets intensity, not routing. Apply §0.3 and present the
five-field card in §0.4 before material work unless scope and route are
already approved — deliver it through `AskUserQuestion` (five fields as the
question body, options 진행(권장)/수정/중단; plain-text card only as fallback) —
and close material work with the five-field completion card
in §0.5. Load full capability detail only in the acting owner or worker; spec
work also requires the spec-read gate.

For `autopilot-code`, `direct` is inline, `quick` is one registered dispatch-depth-1
owner, and `standard+` follows `code-plan -> code-execute -> code-test ->
code-report` under `core/OPERATIONS.md §5.10`. Dispatch depth 3 is forbidden.

After an interactive `dispatch-owner --start` reports
`parent_completion_delivery=claude-parent-runtime`, the projected
`PostToolUse(Bash)` `asyncRewake` hook owns that exact owner attempt until one
terminal receipt. Do not launch or re-arm Background Bash, `Monitor`, liveness,
or `dispatch-wait`, and do not emit periodic progress recaps. End the turn after
the ordinary one-time launch notice; the native hook wakes this session once.
Only an explicit `poll-fallback` authorizes a model-owned bounded wait.

Checked wrappers keep `capability_mode` separate from a non-owner
`worker_mode`, which must equal its portable `unit`. A dispatch-depth-1 owner is
`_kernel/owner` with no worker mode; contradictory owner/stage tuples fail
before prompt, registry, or spawn. Legacy `mode` is read-only compatibility
data. Use `stage-dispatch-fallback.py` for ordinary standard+ dispatch-depth-2 work and one
`dispatch-batch.py --parallel-group` call for each sealed 2–4-way group. Contract v3 claims one
stable attempt before spawn; the retired broker only supports `status`/`stop`.

Keep native agents distinct from registered headless worker dispatch; a restriction on one surface never silently extends to the other. Preserve model role, intensity, depth, tests, safety, and validation on fallback. Do not run drill automatically. A Codex job the openai-codex plugin detaches after its foreground timeout runs in the plugin's own queue, never in jobs.log; Fleet shows it only as a read-only plugin-queue row. Launch substantial Codex delegation that needs attempt-grade tracking or gates through registered dispatch instead.

## Tracked-Workflow Continuation

Process exit is not workflow completion (`core/WORKFLOW.md §0.6`). A workflow is
complete only when every declared terminal node holds its completion gate. Every
non-terminal stage declares `inline-next`, `supervised`, `human-gate`, or
`monitor`; a detached resource run must be `supervised` and can never be
terminal. A graph that breaks this is refused at `capability-route.py compile`
and at launch.

Do not end a turn while a tracked workflow has a stage with no registered
continuation. Arm the shared supervisor
(`utilities/workflow-supervisor.py arm|poll|watch|status|complete`), dispatch the
next stage, or record the human gate in the same turn; when none is possible,
say so plainly and name the checked fallback. Report state from PID identity,
sentinel/exit evidence, log modification time, and declared artifacts, never from
a registry status word alone. `OPERATIONS §5.12` owns the mechanics.

## Runtime Lifecycle

Claude hooks realize portable invariants for workflow signals, write/spec/core gates, memory, and design checks. Use explicit wrappers when a hook cannot be trusted. Main-session memory lifecycle and distillation do not run for workers. Session end never owns destructive worktree cleanup.

Use `statusline.sh` only for runtime status. Harness detail remains available through the adapter tools and docs. Runtime-owned credentials, sessions, logs, caches, databases, and config stay outside this repo.

## Context and Memory

Each eligible main prompt receives bounded capsule headline-and-ID candidates.
Ignore unrelated candidates and read a relevant record in full before use. If
the prompt hook is unavailable, record `recall` or `skip` with
`mem recall-gate`. Retrieve full pending obligations before applying or
consuming them. Workers do not run this main-session probe.

Context pressure is orthogonal to quality and stage graph. Ordinary hook states stay silent. Static bytes, code lines, and directive counts are footprint measures, not token or billing savings. `core/ADAPTATION.md §6.1` owns budgets; real savings claims require paired production sessions.

## Response Policy

Portable behavior contract = `roles/response-policy.md`.

- **Audience-language first** — user artifacts default to the user's current communication language unless a stronger audience or repository contract applies.
- Keep responses concise and match promises with same-turn action.
- **Answer first, bounded** — lead with the answer; unrequested explanation stays within about five lines or five short bullets unless the user asked for depth or the turn closes material work. Offer the rest rather than delivering it unbidden.
- **Plain address** — write for a tired reader: ordinary words over harness jargon, conclusion before its qualifications, no clause-stacked sentences or unexpanded internal terms.
- Verify before asserting and follow existing conventions.
- **Local evidence before recall** — answer a domain question from the repository's research/analysis/briefing artifacts first; model memory is the fallback, a memory-only answer says so and flags its risky specifics, and the §0.4 card exemption never waives this evidence check.
- Ask only for genuinely non-obvious or destructive choices; proceed with the recommended reversible path when no answer is needed. Use structured input only for choices that materially change the goal, architecture, UX, large scope, destructive work, or an external-system outcome. Continue low-risk reversible work autonomously. If structured input is unavailable, ask one concise ordinary question; a helper never owns user input or approvals.
- In an active “do X” flow, implied records, validation, commit, and push follow without repeated confirmation.

Claude-specific realization: keep work grounded in current files, expose changes before committing, and commit/push validated harness changes in the same turn under `core/OPERATIONS.md §5.11`.

## Compatibility Boundary

Codex and OpenCode files are sibling implementation references, never Claude bootstrap input. Portable meaning comes from `core/`, `capabilities/`, and `roles/`; map that meaning to Claude-native runtime surfaces.
