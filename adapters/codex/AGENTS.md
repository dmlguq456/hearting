# AGENTS.md — Codex Adapter Bootstrap

This is a Codex adapter router, not the portable source of truth. The semantic
hierarchy is `core/capabilities/roles -> {Claude, Codex, OpenCode}`; adapters
are siblings and none is another's reference implementation. Edit core first.

## Source Order

Codex has already loaded this file through the global instruction chain. Resolve
`<agent-home>` from the active `AGENT_HOME`, falling back to
`${CODEX_HOME:-$HOME/.codex}/hearting` and the shared `utilities/agent-home.sh`
resolver. Interpret every harness path below relative to that root.
Don't probe `<cwd>/core/CORE.md` or report it missing.

Read `<agent-home>/core/CORE.md` first; load the remaining documents only when
the task touches the named domain.

1. `<agent-home>/core/CORE.md`
2. `<agent-home>/core/WORKFLOW.md` for routing and tracked work
3. `<agent-home>/core/CONVENTIONS.md` for intensity, QA, roles, artifacts, and Skill rules
4. `<agent-home>/core/OPERATIONS.md` for git, worktrees, locks, and dispatch
5. `<agent-home>/core/MEMORY.md` for memory
6. `<agent-home>/capabilities/README.md`, `<agent-home>/roles/README.md`, and `<agent-home>/roles/MODES.md`

For runtime-surface or parity changes, verify current official Codex/Claude
documentation, then inspect the local realization. Separate runtime support,
local projection, and parity gaps; plan a checked fallback.

## Runtime Mapping

- `AGENT_HOME` is the installed harness root. Resolve artifacts through `utilities/artifact-root.sh`; linked worktrees write the primary checkout's `.agent_reports/`, and legacy `.claude_reports/` is only a fallback.
- Portable model roles remain vendor-neutral. Resolve them with `preflight.sh role <portable-role|role-profile|pipeline-stage>`.
<!-- BEGIN generated from core/fragments/bootstrap-compute-hosts.md by tools/sync-bootstrap-dispatch.py; edit the source -->
- Reach compute hosts through `compute-hosts` and its inventory (`~/.config/hearting/compute-hosts.yaml`). `run` receipts include measured GPU headroom; `probe` remains available for host comparison.
<!-- END generated from core/fragments/bootstrap-compute-hosts.md -->
- Capabilities come from `capabilities/`; Codex-native generated Skills/plugin, agents, and modes live under `adapters/codex/`. Expose them through `codex_setting/codex-plugin-marketplace`, `codex_setting/codex-agents`, and `codex_setting/codex-modes`.
- Hooks are Codex bridges under `codex_setting/codex-hooks`; never project Claude settings, commands, hooks, or allowedTools.
- Before using a capability or mode, run `adapters/codex/bin/preflight.sh capability-info <capability>` or `preflight.sh mode-info <family/mode>` and obey named `tool_contract`, `tool_contract_check`, `runtime_surface`, and `fallback`.
- Read the governing core contract before adapter changes. Spec reads may be
  recorded with `preflight.sh read <prd.md>` for workflow evidence.
- Spec reads and design saves retain observational hook coverage.

Detailed lifecycle and edge-case contracts live in `adapters/codex/README.md`
and `ADAPTATION.md`; command output is authoritative for current support.

## Command Surface

| Need | Command |
|---|---|
| lifecycle | `preflight.sh prompt-signal` |
| workflow/context | `preflight.sh status`, `preflight.sh briefing`, `preflight.sh worklog` |
| memory | `preflight.sh memory`, `preflight.sh recall-gate`, `preflight.sh recall` |
| token/UI | `preflight.sh token-budget`, `preflight.sh ui-info`, `preflight.sh tui-config` |
| delegation/QA | `preflight.sh subagent-info --check`, `preflight.sh qa-policy <level> [code|research|doc|general]` |
| readiness/loops | `preflight.sh doctor [--runtime]`, `preflight.sh loop-info <oncall|note|study|drill|runtime-watch>` |
| dispatch control | `preflight.sh dispatch-wait --attempt-id <id> --max 300..600` (operator recovery only), `preflight.sh liveness`, `preflight.sh harvest`, `preflight.sh dispatch-reconcile` |
| dispatch readiness | `preflight.sh dispatch-readiness --worktree <path> --jobs <jobs.log> --owner-harness <h>... --child-harness <h>... --output <evidence.json>` |
| install | `install-runtime-projection.sh [--install-plugin] [--skills-mode native|plugin|both]`, `check-runtime-projection.sh`, `preflight.sh runtime-projection --require-hook-trust` |

Keep Codex `/statusline` responsible for model, context, token, limit, and session footer fields. `preflight.sh status` is an on-demand harness snapshot, including git dirty/worktree/dead-branch risks. Runtime config remains user-owned; strict projection checks read authoritative App Server `hooks/list` current-hash trust and never rewrite user trust state.
The recommended footer fragment is `codex_setting/codex-config/tui-statusline.toml`; apply it only through explicit `preflight.sh tui-config`.

Interactive completion uses Codex's native queue, addressed to the caller's
`CODEX_THREAD_ID`. The interactive launcher and managed gateway are retired.
The exact-batch sidecar checks consumed and pending messages before sending;
ambiguous sends may repeat. Only an exact Hearting item may restart an interrupted
parent. The TUI owns questions and approvals; headless owners keep their separate
supervisor. Default mode asks via waiting `request_user_input` (`default_mode_request_user_input`); it submits empty at 120 s (countdown from 60 s; typing cancels). Empty is no decision: restate at turn end (Fleet keeps `answer needed`); `_async`: fallback.
Arbitrary detached shell output still does not auto-resume. For non-dispatch
long-running work, obey `preflight.sh loop-info runtime-watch` and its explicit automatic-follow-up-impossible fallback
instead of ending with a detached completion promise.

The session uses one canonical `AGENT_DISPATCH_JOBS`.
Treat it as immutable at every dispatch depth; never reconstruct a registry from
`$AGENT_HOME/.dispatch/jobs.log`, because packaged `$AGENT_HOME` is versioned source.

## Tool Contracts

Before claiming support, run the relevant check:

- `preflight.sh visual-harness <file.html>`
- `preflight.sh browser-fetch --check <url>`
- `preflight.sh data-script --check <script.py>`
- `preflight.sh figure-gen --check <script.py>`
- `figure-gen --verify-report <manifest.json> <report.md>`
- `preflight.sh pdf-extract --check <file.pdf>`
- `preflight.sh web-image-search --check <query>`
- `preflight.sh verification-runner --timeout <seconds> -- <command>`
- `preflight.sh claim-verify --check <claim>`
- `preflight.sh permissions` and `preflight.sh mcp [--check]`

Exit 69 means the local tool contract is unavailable; use the reported fallback
or mark the adapter row unverified/unsupported. Never borrow a Claude-native
tool to claim Codex parity.

## Dispatch

Route by `core/WORKFLOW.md §0.2`. The main session decides only whether the work is
direct (§0.2.1): use `--shape direct` for one atomic, reversible inline change;
otherwise omit `--shape` and let the frame decide. Apply §0.3. Use `hearting run
capability-route compose`. For unapproved material work, use the §0.4 card.
Close material work with the first-report content
required by §0.5; its card is optional.

<!-- BEGIN generated from core/fragments/bootstrap-dispatch.md by tools/sync-bootstrap-dispatch.py; edit the source -->
```
hearting run capability-route compose --start --prompt-file <task> \
  [--shape direct] [--campaign-key <stream>]
```

Shape-free flags are hints. Follow decided routes under §0.2.1; person-named
stages use `--shape staged --graph <stage,…>`.

`hearting` is on PATH. `hearting run <utility>` uses `AGENT_HOME` when set (dev
activation: that checkout), else the installed release. The slug comes from the
task. The stream defaults to this session's latest artifact-root stream, else
this folder's only active one; otherwise use `--campaign-key` (a folder name or
close spelling joins that stream; refusal lists keys) or `--unassigned`. The
runtime prepares the cycle, selects each leg's harness from live usage within
sealed candidates, starts frames and owner, reuses exact attempts, and closes
the route and cycle at success or idle.

Follow the receipt. `parent_next=end-turn`: end with no wait, poll or recap;
a runtime carrier owns it. `parent_next=bounded-wait`: run `parent_next_command`
once. Start saves `receipt_file`; read it if hooks omit `parent_next`.
No directive is not `end-turn`; never filter stdout.
After wake/correction, run `resume_command`; answer a BLOCKED owner with `correction_command
--message-file <file>`. At `needs-question`, compare the two frame briefs and
follow the receipt's `next_step`: `resume_command --interview <file>` registers
the question, the person answers it once in the native question surface, and
`resume_command --answers <file>` records the intent, releases the gate and
starts the owner.

Inside an owner, a stage is `python3 "$AGENT_HOME/utilities/stage-dispatch-fallback.py"
--node <node> --start`: route, slug, parent and harness come from the owner's
route and environment, and a member of a sealed parallel group starts its
whole group in one batch. Dispatch depth 3 is forbidden.

Default to this session's route; start an interactive peer with `hearting run
peer-steward start` only for long work needing human answers or recovery.
Command output panes use `hearting run pane open <command>`.

Message another session with `hearting run peer-steward prompt
<name-or-pane> --body-file <file>`; reply text addressed to it reaches nobody.
<!-- END generated from core/fragments/bootstrap-dispatch.md -->

Harness selection reads the user-owned `dispatch-defaults.yaml` over
`profiles/dispatch-defaults.yaml`; `core/ADAPTATION.md` owns the cascade.
Capacity never crosses a quality band unless the relief threshold is met, and
OpenCode is not a default deep peer.

Check `preflight.sh headless [--check] [--require-hook-trust] <worktree>`.
`preflight.sh dispatch --dry-run|--register|--start [--require-hook-trust]`
runs the raw wrapper; the route commands above reach it with the sealed tuple.
A direct interactive launch selects
completion by the parent runtime: a Codex parent uses its
native queue and a Claude parent uses Claude resume. Native queue
delivery does not force Stop/PreToolUse trust. Keep the parent
conversational; a wait is never an in-model `sleep`/liveness loop.
`--allow-unmanaged-parent-poll` stays operator-only.
Legacy stamped Stop state permits only one exact terminal
`--status all --attempt-id` harvest.
Dispatch contract v3 atomically claims one stable
attempt row before spawn and starts no child for a duplicate claim. A standard+
Codex dispatch-depth-1 owner receives workspace-write network access for this purpose;
dispatch-depth-2 workers do not. The retired broker exposes only legacy `status`/`stop`.

`standard+` uses a dispatch-depth-1 capability owner and, when separable, dispatch-depth-2
`code-plan -> code-execute -> code-test -> code-report` stage workers.
`direct` is inline; `quick` is one registered-headless dispatch-depth-1 one-shot conductor. Record an inline exception in plan metrics. After integration,
verification, and push, use `preflight.sh worktree-cleanup --check` before
`--apply`; SessionEnd/Stop never cleans worktrees.

For `autopilot-code`, `capability-info` and `route` print the portable pipeline contract (`code-plan>code-execute>code-test>code-report` for `standard+`).
Use native subagents only after `preflight.sh subagent-info --check`; native
subagents and registered headless workers remain distinct. A restriction on
one surface never silently extends to the other. Preserve model role, intensity,
depth, tests, safety, and validation on fallback.

## Tracked-Workflow Continuation

Process exit is not workflow completion (`core/WORKFLOW.md §0.6`). A workflow is
complete only when every declared terminal node holds its completion gate. Every
non-terminal stage declares `inline-next`, `supervised`, `human-gate`, or
`monitor`; a detached resource run must be `supervised` and can never be
terminal, and a graph that breaks this is refused at route compile and at launch.

<!-- BEGIN generated from core/fragments/bootstrap-continuation.md by tools/sync-bootstrap-dispatch.py; edit the source -->
Keep unfinished work here or in its registered continuation; state checked fallback if unsupported. Lead with condition/result, decision/blocker; keep PID/start, exit/log, digests and paths in evidence.
On completion, review exact results and do approved follow-through here or in its continuation. Receipt/queue/ACK is not execution; report chronology. Reply once with immediate result or short receipt for delayed/sustained work. Informational notices need no reply. Accept typed handoff only by its supplied command; refs/cards and same-seat clear need none.
<!-- END generated from core/fragments/bootstrap-continuation.md -->

The native queue never substitutes for a continuation.

## Memory and Context

Memory semantics belong to the acting agent. Each eligible main prompt receives
bounded capsule headline-and-ID candidates. Ignore unrelated candidates and
read a relevant record in full before use. If the prompt hook is unavailable,
record `recall` or `skip` with `preflight.sh recall-gate <cwd> ...`. Retrieve
full pending obligations before applying or consuming them. Workers do not run
the main prompt probe or other main memory lifecycle. Call the `session-tidy`
Skill when context is filling or before compact, before handing work to another
session, and when a large task ends; a new session receives the seat's card once.

`preflight.sh token-budget` exposes exact-session telemetry. The normal, unknown, repeated-band, and validated-native states inject zero bytes; a verified
tight/critical transition may emit one directive of at most 240 UTF-8 bytes.
Pressure changes optional response prose only—never intensity, dispatch/depth,
model role, required input, tools/tests, safety, validation, or guards.
`token-budget-experiment.py` is production-disabled; static bytes and counters
are not token, billing, savings, cost, or ROI estimates. Native budget config is
read-only unless the user explicitly opts into a separately validated feature.

Do not run drill automatically. Do not edit runtime-owned credentials, sessions,
logs, caches, databases, or `$CODEX_HOME/config.toml`.

## Response Policy

Portable behavior contract = `roles/response-policy.md`.

- **Audience-language first** — user artifacts default to the user's current communication language unless a stronger audience/repository contract applies. Code and commits keep the repo's language.
- Keep responses concise, match promises with same-turn action, verify before asserting, and follow current conventions; expose a convention change before committing it.
- **Local evidence before recall** — repo research/analysis/briefing artifacts answer domain questions first; memory-only answers say so and flag risky items; §0.4 card exemption never waives this check.
- **Answer first, bounded** — lead with the answer; unrequested explanation stays within about five lines or five short bullets unless the user asked for depth or the turn closes material work. Offer the rest rather than delivering it unbidden.
- **Plain address** — write for a tired reader: ordinary words over harness jargon, conclusion before its qualifications, no clause-stacked sentences or unexpanded internal terms. Other sessions use Fleet tag and pane (SID if needed), role in parentheses; without a tag, herdr name and pane, rather than internal abbreviations.
- Ask only for genuinely non-obvious or destructive choices. Continue reversible in-flow work and its implied validation, records, commit, and push. Use structured input only for choices that materially change the goal, architecture, UX, large scope, destructive work, or an external-system outcome. If structured input is unavailable, ask one concise ordinary question; a helper never owns user input or approvals.
- Under `core/OPERATIONS.md §5.11`, commit and push validated `<agent-home>` instruction, rule, hook, preflight, or status-surface changes in the same turn without a separate user signal.

## Compatibility Boundary

Claude/OpenCode files are sibling references, not Codex bootstrap input. Portable
meaning comes from `core/`, `capabilities/`, and `roles/`; map it to Codex
tools, approval, sandbox, lifecycle, and discovery.
