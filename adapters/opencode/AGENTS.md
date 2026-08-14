# AGENTS.md — OpenCode Adapter Bootstrap

This is an OpenCode adapter router, auto-loaded as `AGENTS.md` from the global
OpenCode config home. It is deliberately not also listed in the
`opencode.json(c)` `instructions` array: that would deliver the same bootstrap
twice (`core/ADAPTATION.md §6.1`). The semantic hierarchy is
`core/capabilities/roles -> {Claude, Codex, OpenCode}`; adapters are siblings.
Edit portable sources first.

## Source Order

Read `core/CORE.md` first; load the remaining documents only when the task
touches the named domain.

1. `core/CORE.md`
2. `core/WORKFLOW.md` for routing and tracked work
3. `core/CONVENTIONS.md` for intensity, QA, roles, artifacts, and Skill rules
4. `core/OPERATIONS.md` for git, worktrees, locks, and dispatch
5. `core/MEMORY.md`
6. `capabilities/README.md`, `roles/README.md`, and `roles/MODES.md`

For runtime-surface or parity changes, verify current official documentation,
then inspect local projection and fallback. Never infer support from another
adapter.

## Runtime Mapping

- `AGENT_HOME` is the installed harness root. Resolve the canonical artifact root with `utilities/artifact-root.sh`; linked worktrees write the primary checkout's `.agent_reports/`, and legacy `.claude_reports/` is only a fallback.
- Portable model roles stay vendor-neutral in shared artifacts; never use vendor model names as portable semantics.
- Capabilities come from `capabilities/`. OpenCode-native generated Skills, commands, agents, and plugins live under `adapters/opencode/` and project through `opencode_setting/opencode-skills`, `opencode_setting/opencode-commands`, `opencode_setting/opencode-agents`, and `opencode_setting/opencode-plugins`.
- Validate native discovery with `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1`; Claude compatibility autoload must not mask missing OpenCode output.
- Run `preflight.sh capability-info <capability>` and `preflight.sh mode-info <family/mode>`; obey named `tool_contract`, `tool_contract_check`, `runtime_surface`, and `fallback`.
- Before edits run `preflight.sh write <file> [session-id]`. Read portable core first for `adapters/**`; mark core/spec reads with `preflight.sh read <file> [session-id]`; run `preflight.sh capability <name> [cwd] [session-id]` for spec changes.
- Material source work needs route participation, not a card alone: before a source write or a commit containing source changes, hold a current cwd-bound route record (`preflight.sh material-route check`), at least `autopilot-code direct` for code. Hotfixes do not bypass it.
- Use explicit guards when the OpenCode plugin is unavailable or untrusted. Never port Claude allowedTools, settings MCP, command, agent, or hook formats.

Detailed lifecycle and edge-case contracts live in
`adapters/opencode/README.md` and `ADAPTATION.md`.

## Command Surface

| Need | Command |
|---|---|
| lifecycle/workflow | `preflight.sh prompt-signal`, `preflight.sh briefing`, `preflight.sh worklog` |
| memory | `preflight.sh memory`, `preflight.sh recall-gate`, `preflight.sh recall`, `preflight.sh distill-delta`, `preflight.sh distill-propose` |
| readiness/loops | `preflight.sh status`, `preflight.sh doctor`, `preflight.sh loop-info <oncall|note|study|drill|runtime-watch>` |
| QA | `preflight.sh qa-policy <level> [code|research|doc|general]` |
| runtime | `preflight.sh permissions`, `preflight.sh mcp [--check]` |

Main lifecycle does not run for workers. Runtime-owned credentials, sessions,
logs, caches, databases, and config stay outside this repo.

## Tool Contracts

Before claiming support, run:

- `preflight.sh visual-harness <file.html>`
- `preflight.sh browser-fetch --check <url>`
- `preflight.sh data-script --check <script.py>`
- `preflight.sh figure-gen --check <script.py>`
- `figure-gen --verify-report <manifest.json> <report.md>`
- `preflight.sh pdf-extract --check <file.pdf>`
- `preflight.sh web-image-search --check <query>`
- `preflight.sh verification-runner --timeout <seconds> -- <command>`
- `preflight.sh claim-verify --check <claim>`

Exit 69 means unavailable; use the reported fallback or keep the adapter row
partial. OpenCode native UI/config owns model and context fields.

## Dispatch

Route by `core/WORKFLOW.md §0.2`: when a request matches one manifest
`entry-router` trigger and no exclusion, that entry is the primary route
and `direct` sets intensity, not routing. Apply §0.3 and present the
five-field card in §0.4 before material work unless scope and route are
already approved, and close material work with the five-field completion card
in §0.5. Load full capability detail only in the acting owner or worker.

Check `preflight.sh headless [--check] <worktree>`. Launch only registered jobs
through `preflight.sh dispatch --dry-run|--register|--start` with the complete
tuple in `core/OPERATIONS.md`. Keep `capability_mode` separate from a non-owner
`worker_mode`, which must equal its portable `unit`; a dispatch-depth-1 owner is
`_kernel/owner` with no worker mode. `worker_role` and legacy `mode` are
read-only metadata, not bootstrap identity. Monitor
`preflight.sh liveness [jobs.log]`; harvest via `preflight.sh harvest`.
Conductors use `dispatch-chain` for ordinary checked dispatch-depth-2 nodes. A sealed
2–4-way `parallel_group` uses one `dispatch-batch --parallel-group` call;
OpenCode is eligible for that registered standard+ dispatch-depth-2 path —
exact parent binding, foreground lifecycle, and supervisor parity are implemented.
Dispatch contract v3 atomically claims one stable
attempt row before spawn and starts no child for a duplicate claim. Broker v1/v2
routes are read-only migration inputs; the retired broker exposes only legacy
`status`/`stop`.

`standard+` uses a dispatch-depth-1 capability owner and separable dispatch-depth-2
`code-plan -> code-execute -> code-test -> code-report` workers. `direct` is
inline; `quick` is one registered-headless dispatch-depth-1 one-shot conductor; dispatch depth 3 is forbidden. Record
inline exceptions in plan metrics. After merge, integrated verification, and
push, use `preflight.sh worktree-cleanup --check` before `--apply`. A
`session.idle` or other session-end event never owns destructive worktree
cleanup; it may expose diagnostics only.

Keep native agent delegation distinct from registered headless work; a
restriction on one surface never silently extends to the other. Before
delegating to a native subagent, verify it against its declared
`opencode_setting/opencode-agents` file and the runtime's current agent list —
never assume a Codex or Claude agent surface exists here. The
main/orchestrator chooses portable roles and concrete model settings per job,
and preserves model role, intensity, depth, tests, safety, and validation on
fallback.

## Tracked-Workflow Continuation

Process exit is not workflow completion (`core/WORKFLOW.md §0.6`): a workflow is
complete only when every declared terminal node holds its completion gate. Every
non-terminal stage declares `inline-next`, `supervised`, `human-gate`, or
`monitor`; a detached resource run must be `supervised` and can never be
terminal. Route compile and launch refuse a graph that breaks this.

Do not end a turn while a stage has no registered continuation: arm
`utilities/workflow-supervisor.py`, dispatch the next stage, or record the human
gate in the same turn, and otherwise say plainly that automatic follow-up is
impossible and name the checked fallback. OpenCode's registered standard+
dispatch-depth-2 continuations use the shared external supervisor rather than a
runtime bridge. `OPERATIONS §5.12` owns the mechanics.

## Memory and Context

Main prompts receive capsule headline/IDs; read relevant records fully.
Fallback: `preflight.sh recall-gate <cwd> ...`. Workers skip it.

OpenCode token self-regulation remains explicitly deferred: Phase 2 automatic accounting and the isolated experiment CLI are not projected; it does not copy
Codex token-budget hooks or mutate runtime config. Ordinary lifecycle context
should be silent. Static bytes, lines, and directive counters are footprint
measures, not token or billing savings. Context pressure never lowers intensity,
dispatch/depth, model role, required input, tools/tests, safety, or validation.

Do not run drill automatically.

## Response Policy

Portable behavior contract = `roles/response-policy.md`.

- **Audience-language first** — user artifacts default to the user's current communication language unless a stronger audience/repository contract applies.
- Keep responses concise, match promises with same-turn action, verify before asserting, and follow current conventions; expose a convention change before committing it.
- **Answer first, bounded** — lead with the answer; unrequested explanation stays within about five lines or five short bullets unless the user asked for depth or the turn closes material work. Offer the rest rather than delivering it unbidden.
- **Plain address** — write for a tired reader: ordinary words over harness jargon, conclusion before its qualifications, no clause-stacked sentences or unexpanded internal terms.
- Ask only for genuinely non-obvious or destructive choices. Continue reversible in-flow work and its implied validation, records, commit, and push. Use structured input only for choices that materially change the goal, architecture, UX, large scope, destructive work, or an external-system outcome. Continue low-risk reversible work autonomously. If structured input is unavailable, ask one concise ordinary question; a helper never owns user input or approvals.
- Under `core/OPERATIONS.md §5.11`, commit and push validated `<agent-home>` instruction, rule, hook, preflight, or status-surface changes in the same turn without a separate user signal.

## Compatibility Boundary

Claude/Codex files are sibling references, not OpenCode bootstrap input. Map
portable meaning from `core/`, `capabilities/`, and `roles/` to OpenCode
permissions, tools, lifecycle, agents, commands, Skills, and plugins.
