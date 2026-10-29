# AGENTS.md — OpenCode Adapter Bootstrap

This is an OpenCode adapter router, auto-loaded as `AGENTS.md` from the global
OpenCode config home. It is deliberately not also listed in the
`opencode.json(c)` `instructions` array: that would deliver the same bootstrap
twice (`core/ADAPTATION.md §6.1`). The semantic hierarchy is
`core/capabilities/roles -> {Claude, Codex, OpenCode}`; adapters are siblings.
Edit portable sources first.

## Source Order

Resolve all paths below against active `AGENT_HOME` (shared `utilities/agent-home.sh`
fallback), including explicitly activated source checkouts; never guess another
checkout from cwd or previous tasks. Read `core/CORE.md` first; load remaining documents only when the task
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
- `preflight.sh` is a shorthand, not a PATH command: it resolves to `<AGENT_HOME>/adapters/opencode/bin/preflight.sh` and no shim is installed at the harness root. Run it by that absolute path — for example `AGENT_HOME="$AGENT_HOME" bash "$AGENT_HOME/adapters/opencode/bin/preflight.sh" capability-info <capability>` — and read every `preflight.sh <command>` below as that path. Export `AGENT_HOME` to the installed root for release behavior, or to a checkout/worktree to use that tree as the active runtime.
- Portable model roles stay vendor-neutral in shared artifacts; never use vendor model names as portable semantics.
<!-- BEGIN generated from core/fragments/bootstrap-compute-hosts.md by tools/sync-bootstrap-dispatch.py; edit the source -->
- Reach compute hosts through `compute-hosts` and its inventory (`~/.config/hearting/compute-hosts.yaml`). `run` receipts include measured GPU headroom; `probe` remains available for host comparison.
<!-- END generated from core/fragments/bootstrap-compute-hosts.md -->
- Capabilities come from `capabilities/`. OpenCode-native generated Skills, commands, agents, and plugins live under `adapters/opencode/` and project through `opencode_setting/opencode-skills`, `opencode_setting/opencode-commands`, `opencode_setting/opencode-agents`, and `opencode_setting/opencode-plugins`.
- Validate native discovery with `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1`; Claude compatibility autoload must not mask missing OpenCode output.
- Run `preflight.sh capability-info <capability>` and `preflight.sh mode-info <family/mode>`; obey named `tool_contract`, `tool_contract_check`, `runtime_surface`, and `fallback`.
- Read the governing core contract before adapter changes. Spec reads may be
  recorded with `preflight.sh read <prd.md>` for workflow evidence.
- Use explicit guards when the OpenCode plugin is unavailable or untrusted. Never port Claude allowedTools, settings MCP, command, agent, or hook formats.

Detailed lifecycle and edge-case contracts live in
`adapters/opencode/README.md` and `ADAPTATION.md`.

## Command Surface

| Need | Command |
|---|---|
| lifecycle/workflow | `preflight.sh prompt-signal`, `preflight.sh briefing`, `preflight.sh worklog` |
| memory | `preflight.sh memory`, `preflight.sh recall-gate`, `preflight.sh recall` |
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

<!-- BEGIN generated from core/fragments/bootstrap-dispatch.md by tools/sync-bootstrap-dispatch.py; edit the source -->
```
hearting run capability-route compose --start --prompt-file <task> \
  [--shape direct]
```

Shape-free flags are hints. Honor decided routes (§0.2.1); person-named stages use
`--shape staged --graph <stage,…>`.

`hearting` is on PATH; `hearting run <utility>` uses set `AGENT_HOME` (dev
checkout), else installed release. Slug: task. Stream: session's latest
artifact-root stream, else folder's sole active one; otherwise `--campaign-key`
(folder/close spelling joins; refusal lists keys) or `--unassigned`. Runtime:
cycle/frames/owner, per-leg live-usage selection among sealed harnesses,
exact-attempt reuse, success/idle route/cycle closure.

Receipt: `parent_next=end-turn` ends without wait/poll/recap (runtime carrier owns
it); `parent_next=bounded-wait` runs `parent_next_command` once. Start saves
`receipt_file`; read if hooks omit `parent_next`. Missing directive is not
`end-turn`; never filter stdout.
Wake/correction: `resume_command`; BLOCKED owner:
`correction_command --message-file <file>`. At `needs-question`, compare both
briefs; follow `next_step`: `resume_command --interview <file>` registers the
question; person answers once in native question surface;
`resume_command --answers <file>` records intent, releases gate, starts owner.

Owner stage: `python3 "$AGENT_HOME/utilities/stage-dispatch-fallback.py" --node
<node> --start`; route/slug/parent/harness inherit owner route/environment.
Start each sealed parallel group as one batch. Dispatch depth 3 is forbidden.

Use this session's route; interactive `hearting run peer-steward start` only for
long work needing human answers/recovery. Output: `hearting run pane open <command>`.

Supervise/check periodically: own route/owner/resource reuses runtime receipts,
observations/hooks; if coverage is unclear, read `hearting run workflow-supervisor
status --route <id-or-file> --json` and report observations. Peer role: `hearting
run peer-steward steward on` (watch/start grants also apply). ⚑ is role, not health.
Use existing role checks/hooks; no added harness-scheduled re-invocation,
Monitor/polling or manual role pane titles.

Send: `hearting run peer-steward prompt <name-or-pane> --body-file <file>`;
reply text sends nothing.
<!-- END generated from core/fragments/bootstrap-dispatch.md -->

Route by `core/WORKFLOW.md §0.2`: semantic precedence names the capability
that owns the artifacts. The main session decides only whether the work is
direct (§0.2.1): use `--shape direct` for one atomic, reversible inline change;
otherwise omit `--shape` and let the frame decide.

Use `hearting run capability-route compose`; without `--shape`, capability,
mode, graph, and profile flags are routing hints only. Follow frame- or
person-decided shapes under §0.2.1; person-named stages use
`--shape staged --graph <stage,…>`. Flags, spec-read gate, and probes default
from the checkout. Use the preset recipe (`preflight.sh route --capability …`)
only when the request names the entry's full loop or a promotion signal or
spec-backed flow requires it. Apply §0.3. Direct routes carry the §0.4 card
through `question` on user-authored turns; received peer envelopes use one
`[경로]` line plus a scope clause, unless destructive or external-facing;
otherwise present the §0.4 five-field card before material work unless already
approved, and close material work with the first-report content required by
§0.5; its card is optional. Load full capability detail only in the acting
owner or worker.

Check `preflight.sh headless [--check] <worktree>`. `preflight.sh dispatch
--dry-run|--register|--start` runs the raw wrapper; the route commands above
reach it with the sealed tuple. Monitor
`preflight.sh liveness [jobs.log]`; harvest via `preflight.sh harvest`. For an
attempt you launched, its receipt outranks that general monitoring.
OpenCode is eligible for the registered standard+ dispatch-depth-2 path —
exact parent binding, foreground lifecycle, and supervisor parity are implemented.
Dispatch contract v3 atomically claims one stable
attempt row before spawn and starts no child for a duplicate claim. Broker v1/v2
routes are read-only migration inputs; the retired broker exposes only legacy
`status`/`stop`.

While this session is idle, the hearting plugin hands it the completion records
owed to it as its next turn (`opencode-turn`, `core/ADAPTATION.md` §7.4), and
a launch receipt then says `end-turn`; a server without that carrier gets
`bounded-wait`. Nested leg receipts remain evidence and add no child waits.

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

<!-- BEGIN generated from core/fragments/bootstrap-continuation.md by tools/sync-bootstrap-dispatch.py; edit the source -->
Keep unfinished work here or in its registered continuation; state checked fallback if unsupported. Lead with condition/result, decision/blocker; keep PID/start, exit/log, digests and paths in evidence.
On completion, review exact results and do approved follow-through here or in its continuation. Receipt/queue/ACK is not execution; report chronology. Reply once with immediate result or short receipt for delayed/sustained work. Informational notices need no reply. Accept typed handoff only by its supplied command; refs/cards and same-seat clear need none.
<!-- END generated from core/fragments/bootstrap-continuation.md -->

OpenCode's registered standard+
dispatch-depth-2 continuations use the shared external supervisor rather than a
runtime bridge. `OPERATIONS §5.12` owns the mechanics.

## Memory and Context

Main prompts receive capsule headline/IDs; read relevant records fully.
Fallback: `preflight.sh recall-gate <cwd> ...`. Workers skip it.
Call `session-tidy` when context is filling or before compact, before handing
work to another session, and when a large task ends; a new session receives the
seat's card once.

OpenCode token self-regulation remains explicitly deferred: Phase 2 automatic accounting and the isolated experiment CLI are not projected; it does not copy
Codex token-budget hooks or mutate runtime config. Ordinary lifecycle context
should be silent. Static bytes, lines, and directive counters are footprint
measures, not token or billing savings. Context pressure never lowers intensity,
dispatch/depth, model role, required input, tools/tests, safety, or validation.

Do not run drill automatically.

## Response Policy

Portable behavior contract = `roles/response-policy.md`.

- **Audience-language first** — user artifacts default to the user's current communication language unless a stronger audience/repository contract applies. Code and commits keep the repo's language.
- Keep responses concise, match promises with same-turn action, verify before asserting, and follow current conventions; expose a convention change before committing it.
- **Local evidence before recall** — answer a domain question from the repository's research/analysis/briefing artifacts first; model memory is the fallback, a memory-only answer says so and flags its risky specifics, and the §0.4 card exemption never waives this evidence check.
- **Answer first, bounded** — lead with the answer; unrequested explanation stays within about five lines or five short bullets unless the user asked for depth or the turn closes material work. Offer the rest rather than delivering it unbidden.
- **Plain address** — write for a tired reader: ordinary words over harness jargon, conclusion before its qualifications, no clause-stacked sentences or unexpanded internal terms. Other sessions use Fleet tag and pane (SID if needed), role in parentheses; without a tag, herdr name and pane, rather than internal abbreviations.
- Ask only for genuinely non-obvious or destructive choices. Continue reversible in-flow work and its implied validation, records, commit, and push. Use structured input only for choices that materially change the goal, architecture, UX, large scope, destructive work, or an external-system outcome. Continue low-risk reversible work autonomously. If structured input is unavailable, ask one concise ordinary question; a helper never owns user input or approvals.
- Under `core/OPERATIONS.md §5.11`, commit and push validated `<agent-home>` instruction, rule, hook, preflight, or status-surface changes in the same turn without a separate user signal.

## Compatibility Boundary

Claude/Codex files are sibling references, not OpenCode bootstrap input. Map
portable meaning from `core/`, `capabilities/`, and `roles/` to OpenCode
permissions, tools, lifecycle, agents, commands, Skills, and plugins.
