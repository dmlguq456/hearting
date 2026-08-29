# Portable Hook Invariants

This document names the runtime-neutral invariants enforced by hook scripts.
It is not a hook registration file. Runtime adapters decide how to attach these
checks to their own event model.

## Verification Layers

Three distinct roles keep this contract honest. Keep the vocabulary separate —
"guard" is the enforcement mechanism, not its test.

| Layer | Role | Agent in loop? | Where |
|---|---|---|---|
| **guard** | Runtime deterministic *enforcement* — hook scripts that block, gate, or inject at the event boundary. | No | The Invariant Catalog below (`hooks/*-guard.sh`, `utilities/*-hook.sh`, adapter hook bridges). |
| **conformance** | Deterministic *verification* that guards, hook bridges, and adapters honor this contract — exact assertions, no model. Covers the `test` status class plus cross-adapter parity. | No | `hooks/portable-guards.test.sh`, `tools/check-adaptation-boundary.sh` (+ per-adapter mirrors). |
| **drill** | Behavioral *regression* — whether the agent follows the rules on a live scenario (golden set). Covers only what cannot be made deterministic. | Yes | `loops/drill/`. |

Design bias (deterministic-first, §0.5): push a check *down* this table when you
can — out of **drill** (agent behavior) into **guard** + **conformance**
(mechanism + deterministic test). Reserve drill for residue that genuinely needs
an agent in the loop. A hook's output shape is deterministic, so it belongs in
conformance, never drill.

The drill **runner** also invokes the conformance layer directly, as a separate
deterministic pre-stage — not a drill case, no agent in loop — before it runs
any behavioral case. This keeps conformance firing from depending solely on the
preflight doctor path, without blurring the conformance/drill distinction above.

## Status Classes

| Status | Meaning |
|---|---|
| `portable-check` | Core decision logic is runtime-neutral and has a CLI entry point. It may also accept Claude hook JSON for compatibility. |
| `adapter-payload-wrapper` | Primarily translates a runtime event payload into a portable decision. Needs adapter-specific wrapper for non-Claude runtimes. |
| `adapter-coupled-automation` | Depends on a concrete runtime session lifecycle, status UI, MCP, or headless worker process. Other runtimes must implement their own equivalent or mark unsupported. |
| `external-integration` | Owned by an external integration and not part of the portable contract. |
| `test` | Local regression test for a hook implementation. |

## Invariant Catalog

| Invariant | Current script | Status | Portable meaning | Non-Claude adapter requirement |
|---|---|---|---|---|
| artifact order and pre-image snapshot | `hooks/artifact-guard.sh` | `portable-check` | Writes fail closed outside the canonical artifact root (linked worktrees may not write their local `.agent_reports`/`.claude_reports` snapshot), and a route-backed write under `spec/` requires the active route to have declared `spec_touch` with a `spec/` write scope. `target-artifact` authorizes only `documents/<artifact>/**` and `research/<artifact>/**`; a non-direct refine or draft rewrite of an existing owned file invokes `utilities/artifact-snapshot.py` before the write. The broader creation-order convention remains routing convention. **Node write_scope check (C-2b):** when a route is active, any write inside the canonical root must also land within the active node's declared `write_scope`, or it fails closed with `artifact-write-outside-node-scope`. Exempt: artifact-root-direct files, dot-prefixed machine state (`.runtime/` etc.), and `_internal/` snapshots. A `write_scope`/`spec_scope` `<...>` placeholder is a single-segment wildcard at runtime; the placeholder vocabulary's truth is `capabilities/topologies.json`, and `tools/check-scope-placeholders.py` owns its closed-vocabulary build-time verification. **Applies to the `Bash` channel too**: only a literal (non-interpolated, non-glob) write target is judged (Tier A) — an interpreter-mediated or otherwise undecidable write target passes and is only observed, never blocked (Tier B). A literal target outside the canonical artifact root is never handed to node-scope matching at all; the guard does not engage. | Run `hooks/artifact-guard.sh --file <path> [--session <id>]` or `hooks/artifact-guard.sh --command <shell> [--session <id>]` before writes, or use an adapter wrapper. |
| git state safety | `hooks/git-state-guard.sh` | `portable-check` | Do not edit files in merge/rebase/cherry-pick/detached unsafe git states unless explicitly unlocked. | Run `hooks/git-state-guard.sh --file <path>` before file edits, or use an adapter wrapper. |
| worktree path isolation | `hooks/worktree-path-guard.sh` | `portable-check` | Main-task worktrees must be sibling directories `<repo>-wt/<slug>` (OPERATIONS §5.10 ②), never inside the repo. Deny the runtime-native worktree tool whose default lands in-repo, and deny a `git worktree add` whose target path is outside `<repo>-wt/`; fail open outside a git repo, on non-add worktree subcommands, and under `WORKTREE_GUARD_BYPASS=1`. | Run `hooks/worktree-path-guard.sh --tool <EnterWorktree\|Bash> [--command <cmd>] [--cwd <dir>] [--session <id>]` before worktree creation. Only the `git worktree add` path check is portable; the built-in-worktree-tool deny is Claude-native — a runtime without an EnterWorktree-style tool (Codex, OpenCode) has no such surface to deny and must not claim it (disclose in ADAPTATION_INVENTORY, no overclaim). |
| material route participation | `hooks/material-route-guard.py` | `adapter-payload-wrapper` | Material source Edit/Write-family calls and commits containing source changes fail closed unless the acting session presents a verified current `autopilot-code` route record at intensity `direct` or higher. Interactive proof is a same-session marker created only after a successful `utilities/capability-route.py compile`; registered workers may use their immutable `AGENT_ROUTE_FILE`/`AGENT_ROUTE_ID` binding. Both paths revalidate route schema/hash, registry and unit digests, cwd, source-commit lineage, and session/worker ownership. Skill/capability-grounding markers, route-card prose, foreign-session markers, and spec-significant markers are never proof. Documentation, configuration, artifact, scratch, read-only, and pure-rename work remain outside the material classifier. Added after the 2026-07-24 Cairn silent-no-route edit/commit/deploy incident. | Attach the guard before Edit/Write-family calls and Bash commits, and attach its marker mode after successful Bash route compilation. A runtime without deterministic session and pre-tool events must expose an equivalent checked wrapper or report the enforcement gap; it may not claim parity from routing prose alone. |
| spec read gate | `hooks/spec-skill-gate.sh`, `hooks/spec-read-marker.sh` | `portable-check` | Spec-changing capability calls in spec-backed projects require a current same-session read marker for at least one governing spec candidate — the root `spec/prd.md` or a one-level `spec/<slug>/prd.md` sub-spec (`_internal` snapshots excluded). Freshness is per candidate: the marker's recorded mtime must be at least that candidate's current mtime. | Run `hooks/spec-read-marker.sh --file <prd.md> [--session <id>]` after actual reads, then `hooks/spec-skill-gate.sh --skill <capability> [--cwd <dir>] [--session <id>]` before spec/code capabilities. |
| capability grounding | `hooks/capability-grounding-marker.sh`, `utilities/capability-grounding.sh` | `portable-check` | An inline entry-capability session — one that runs the capability without dispatching, so it leaves no `jobs.log` dispatch row — records its capability plus best-effort mode/intensity, so Fleet can show `capability(mode·intensity)` for it the way the dispatch options column already does for dispatched work. Capability is the exact entry-skill name (the ten `autopilot-*` entries only); mode/intensity are parsed from the invocation args (structured `--mode`/`--intensity`, else the fixed intensity vocabulary). Read-only signal keyed by session id, never blocks; the freshest invocation wins and the marker's mtime carries the same sid-reuse freshness rule as the spec marker. | Run `utilities/capability-grounding.sh record --sid <id> --capability <name> [--mode <m>] [--intensity <i>] [--cwd <dir>]` on entry-skill invocation, or attach a marker hook to the runtime's skill-invocation event. A runtime without a skill-invocation event records the grounding from the capability router itself. |
| core first gate | `hooks/core-first-guard.sh`, `hooks/core-read-marker.sh` | `portable-check` | Adapter edits require a current-session `core/*.md` read marker so adapter changes are derived from the model-neutral contract. | Run `hooks/core-read-marker.sh --file <core-doc> [--session <id>]` after actual core reads, then `hooks/core-first-guard.sh --file <adapter-target> [--session <id>]` before adapter writes. |
| memory write guard | `hooks/builtin-memory-guard.sh` | `portable-check` | Runtime-native file memory must not bypass the unified DB memory store. | Run `hooks/builtin-memory-guard.sh --file <path>` before writes, or remove the native memory feature. |
| design post-write verification | `hooks/design-postwrite.sh` | `portable-check` | Saved design HTML should get deterministic console verification. | Run `hooks/design-postwrite.sh --file <path>` after design HTML writes, or attach it to a post-write event. |
| spec sync nudge | `hooks/spec-sync-nudge.sh` | `portable-check` | In a spec-backed project, a source edit that removes a value/identifier still described in `spec/*.md` should surface those spec lines so the corresponding spec text is synced as part of the change. Read-only: emits context only, never blocks. | Run `hooks/spec-sync-nudge.sh --file <path> [--old <s>] [--new <s>] [--cwd <dir>] [--format text]` after edits, or attach it to a post-write event that supplies the edited path and old/new strings. |
| memory injection | `tools/memory/mem.py inject` | `portable-check` | Inject relevant DB memory at session start. | Run `tools/memory/mem.py inject` for text output, or `tools/memory/mem.py inject --hook` when the runtime accepts Claude-style `additionalContext`; adapters may keep automatic session-start injection opt-in when the runtime can fire start events on resume or compact. |
| memory candidate exposure and agent-owned adoption | `hooks/mem-recall-inject.sh`, `tools/memory/mem.py candidates`, `tools/memory/mem.py recall` | `portable-check` | Every eligible main prompt gets a fail-open, capsule-only lookup: active current-project/global rows, headline plus ID, maximum six and 2,400 UTF-8 bytes, no bodies or access touch. The model decides relevance and reads the full record before applying it. The bridge publishes a same-turn receipt; main-session material work fails closed when no successful probe or explicit recall-gate recovery occurred. Registered route-bound workers are exempt. | Register an adapter-native prompt bridge that supplies prompt, cwd, session, and native turn/message ID when available. Consume only the runtime's structured context field. Preserve the explicit `recall` helper for deeper search and hook-failure recovery. |
| memory distillation trigger | `hooks/mem-turn-nudge.sh`, `hooks/mem-distill-dispatch.sh` | `adapter-coupled-automation` | On an interactive main session only, periodically distill session deltas into DB memory through a no-tools worker. `AGENT_SESSION_ROLE=worker` and adapter compatibility markers make both hooks silent no-ops before counters, locks, or model calls. The shared dispatcher uses `MEM_DISTILL_WORKER=<executable>` with `<mode> <model> <prompt-file>` arguments. | Provide session transcript source (`mem.py distill --source <adapter>`), detached worker invocation, no-tools/action contract, and the same main/worker gate before automatic memory mutation. Deterministic safety hooks remain active in workers. |
| periodic curation trigger (opt-in, default off) | `utilities/mem-periodic-curate.sh` | `adapter-coupled-automation` | Optional nightly backstop for long-lived sessions where the SessionEnd curator cadence is too sparse. Gated on `MEM_PERIODIC_CURATE_ENABLE=1`; unset is a complete no-op, following the `MEM_DISTILL_ENABLE` precedent. A **single cron firing point** runs one `mem-distill-dispatch.sh periodic-curate <cwd>` per eligible project **sequentially**, sharing the ordinary D-41 slots and per-project locks — never a session-event fan-out, which is the structure behind the v18 216-worker incident. The rolling start budget does not apply to this mode (it guards concurrent hook fan-out; this loop is one bounded sequential caller, and the shared budget silently no-opped the batch tail in the 2026-08-13 field run). Projects are selected from the store DB — origins holding active records, soft-ceiling-exceeded first, then by active record count — not by directory listing, and `reattribute` is denied in this evidence-blind mode at both prompt and applier. The script refuses to run inside a worker, registered, or dispatch-child context (D-42), and `periodic-curate` mode does not advance distill markers. SessionEnd curate remains the backstop. Cron registration is documented, not installed, and the cron environment must supply what interactive settings normally provide — the dispatcher gate and a PATH holding the runtime CLI: `0 4 * * * MEM_PERIODIC_CURATE_ENABLE=1 MEM_DISTILL_ENABLE=1 PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin <agent-home>/utilities/mem-periodic-curate.sh >> <state-dir>/periodic-curate.log 2>&1`. | Provide an equivalent single-firing-point scheduler that runs project curation sequentially inside the shared bounded-worker controls, keeps the opt-in gate default-off, and preserves the main/worker boundary. |
| oncall briefing injection | `hooks/mem-briefing-inject.sh` | `portable-check` | On the dedicated agent desk, inject daily oncall report once per day. | Run `hooks/mem-briefing-inject.sh --cwd <dir> [--format text]` before prompt handling, or attach it to a prompt-submit event. |
| worklog state signal | `utilities/agent-worklog-state.sh` | `portable-check` | Surface configured `<agent-notes-root>` / `<worklog-board-app>` inventory without mutating data. | Run `utilities/agent-worklog-state.sh [cwd]` or an adapter wrapper before worklog-board or agent-notes work. |
| runtime hook output protocol | adapter hook bridges | `adapter-payload-wrapper` | Hook stdout must match the owning runtime's hook protocol exactly. Context-injection hooks emit the runtime's structured context object; side-effect-only lifecycle hooks keep stdout empty unless that runtime explicitly accepts a structured success object. Portable helper text is never forwarded as raw hook stdout. | Each adapter must document its hook output contract, test the exact stdout shape for every native hook bridge, and route diagnostic/helper text to logs or stderr only when the runtime accepts it. |
| Fleet interaction wait signal | `tools/fleet/interaction.py`, `hooks/fleet-interaction-state.py`, adapter-native lifecycle bridges | `adapter-payload-wrapper` | An unresolved user decision or approval is recorded only as an exact `harness`/`session_id` allowlisted sidecar (`schema_version`, `harness`, `session_id`, `kind`, `source`, `waiting_since`). Question text, answer choices, commands, arguments, denial reasons, model output, and tool payloads are structurally absent. Producers never own the response and keep stdout empty; loss of the observational sidecar must not affect the session. | Translate native question/approval events into the same allowlisted writer and clear them at exact resolution/turn/session boundaries. If no exact runtime event exists, report `unknown`; never infer a wait by parsing prompt prose. |
| Herdr state integration | `hooks/herdr-agent-state.sh` | `external-integration` | Publish working/idle/blocked/release state to Herdr. | Optional external integration; not a core invariant. |
| stage-dispatch reminder | `hooks/stage-dispatch-reminder.sh` | `portable-check` | SD-11: when a dispatch-depth-1 conductor at standard+ intensity is about to invoke a `code-{plan,execute,test,report}` sub-skill **in-session** (env `AGENT_DISPATCH_DEPTH=1`, `AGENT_DISPATCH_INTENSITY∈{standard,strong,thorough,adversarial}`), surface a reminder to dispatch the stage as a dispatch-depth-2 headless session instead. **Soft / non-deny** (fail-open): emits `additionalContext` only, never blocks — the hook cannot tell a legitimate headless-unavailable fallback from a mistake (§8.5.2). Recursion guard: no-op under `MEM_DISTILL=1`. | Run `hooks/stage-dispatch-reminder.sh --skill <name> [--depth <n>] [--intensity <i>]` before a Skill call, or attach it to a pre-tool Skill event. |
| native subagent default model | `hooks/subagent-model-default.sh` | `adapter-payload-wrapper` | Native subagent spawns carry the adapter-config-declared default model tier instead of silently inheriting the interactive session model (core/ADAPTATION.md §3). An explicit per-invocation choice, agent-definition pin, or intentional parent-inherit surface wins only when the resolved model is delegation-eligible. A config-declared interactive-main-only model, explicit `inherit`, fork inheritance, or an unavailable eligibility policy is a typed deny for a valid spawn request; malformed/non-actionable payloads remain silent. | Realize the same config-declared default and main-only eligibility guard through the runtime's native subagent model configuration or pre-spawn decision surface, or record the surface as unsupported; do not consume the Claude PreToolUse payload shape as configuration. |
| conductor Stop gate | `hooks/conductor-stop-gate.sh` | `portable-check` (**UNREGISTERED — historical fallback only**) | Legacy SD-14b experiment that blocks a conductor turn-end with open children and directs exact wait/harvest recovery. It is held because Stop is not a reliable asynchronous resume surface and blocking the interactive parent destroys conversational availability. | Do not register it as a normal completion path. Use an adapter-owned session supervisor or report explicit finite operator fallback. |
| registered-child completion delivery | adapter-owned session supervisor or checked single-ingress gateway | `adapter-coupled-automation` | A registered headless owner yields after registering a batch; its runtime supervisor joins the exact `parent_attempt_id` batch outside the model and resumes the same owned session once with a bounded typed receipt. A registered Claude owner keeps one realtime stream-input process for the route and submits the next receipt immediately after each non-terminal join; when a freshly verified terminal marker closes every declared terminal gate, the supervisor skips the redundant final owner turn and closes the stream before terminal row reconciliation. An explicit custom-command fallback retains per-turn `--resume`. A Claude interactive parent may instead arm one native `asyncRewake` PostToolUse hook from a successful exact owner-start receipt. It re-reads the exact current row and sealed completion evidence before rendering: every terminal receipt — success or attention — exits two, because Claude Code wakes an idle session for an `asyncRewake` hook only on exit code 2 and delivers exit-0 output no earlier than the next user interaction (corrected 2026-08-29; success additionally carries its structured notification on stdout). A launcher-managed interactive Codex session places one owner-only gateway between remote TUI and App Server. The harness installer may make this checked entry transparent for interactive commands, but plugin or hook loading after process entry is not equivalent. That gateway atomically serializes manual input with an exact completion receipt, uses `turn/start` only while idle and `turn/steer` only for a steerable active turn, and durably suppresses duplicate sealed-batch delivery. The sidecar is prelaunched before the child spawn claim, connects only to the private control socket, never subscribes upstream, never sees or answers approvals, and submits no raw child output. A send followed by an unclassified disconnect is `sent-ambiguous` and is not retried. Outside those checked entries, hooks must not simulate wake by blocking Stop, parking every tool, or injecting a synthetic user turn; the parent remains conversational and uses a disclosed finite fallback. Legacy receipts may be consumed only by exact terminal typed harvest. Runtime-native subagents are a separate surface. | Select delivery by parent runtime, never child runtime: Codex managed parent → Codex gateway; Claude interactive parent → exact owner `asyncRewake` with an exit-2 wake for every terminal receipt, plus the SessionStart/UserPromptSubmit sweep that re-delivers any SD-111 pending record at the next prompt; registered Claude owner → persistent realtime stream with a sealed-terminal fast path (checked per-turn `--resume` fallback); registered Codex headless owner → its private App Server supervisor. Keep TUI client A as the only approval owner and sidecar client B control-only. Require private socket/state paths, exact terminal+quiescent membership, durable idempotency, bounded typed context, and fail-closed ambiguity. A transparent launcher must preserve and validate the real CLI, route only interactive surfaces, repair on update, and restore exactly on uninstall. If those checks are unavailable, report fallback and the missing atomic `continueIfIdle(threadId, idempotencyKey, typedContext)`/native async-rewake primitive rather than widening Stop or PreToolUse. |

An active-turn `turn/steer` rejection is a checked non-acceptance result, not a
successful wake. The single-ingress gateway may retain that exact delivery until
idle and issue one `turn/start`; if the gateway loses that in-memory defer, its
durable `sent` row is `sent-ambiguous` and must not be replayed automatically.

## Registered Vocabulary Invariants (SD-113)

Route node id namespace reserves the `_` prefix for dispatch-internal
sentinels. A topology containing a `_`-prefixed node id fails closed at
`capability-route.py compile` with `route-node-id-reserved-prefix`.

The `delivery_intent` stamp vocabulary is a set equality with the stored
`RECIPIENT_KINDS` enum (`utilities/dispatch_pending_delivery.py`): the stamp
fires only when a row's recipient kind is a member of `RECIPIENT_KINDS`.
`parent-runtime-supervised` is not a member of that set — its completion
delivery is owned solely by the SD-78 supervisor outbox (`core/OPERATIONS.md`),
which creates no pending-delivery record for it.

Claim authority for a claimed-state pending record has two grades:
`generation-proven` — full §13.33.1-(6) authority, including expiry and route
judgment — and `deliverer-unproven`, which carries delivery authority only: it
may claim a pending or lease-expired record by `session_id`/`recipient_digest`
match alone, inject a bounded receipt, and ack, with no judgment authority. The
record must persist the grade actually used in its `claim_authority` field; a
writer that cannot persist the field must not claim.

## Adapter Rule

Adapters may reuse scripts directly only when they can supply the expected input
payload and consume the expected output decision. Otherwise, the invariant must
be wrapped or reimplemented behind an adapter-native event bridge.

Adapter hook bridges own the final runtime output protocol. A portable helper can
print human-readable status for explicit CLI use, but a native runtime hook must
not forward that text unless the runtime accepts it for that hook event. For
example, a context hook may emit `hookSpecificOutput.additionalContext` when the
runtime supports it, while a lifecycle side-effect hook such as a session-end
sync may need to perform the mutation with empty stdout or a minimal structured
success object so the runtime does not attempt to parse helper text as hook
JSON.

Codex realizes approval waits through its native `PermissionRequest` bridge and
clears them after native `PostToolUse` or a turn/session backstop. Its decision
reader accepts only structured rollout `response_item` function-call records
paired by exact call id; prompt wording is not an evidence source. Claude uses
its native question, permission, post-tool, prompt, stop, and session events.

Current Claude Code registration lives in `adapters/claude/settings.json` and
executes concrete hook projection files under `adapters/claude/hooks/` via the
runtime projection `claude_setting/hooks`.
Codex must not consume that JSON as configuration. It can run
`adapters/codex/bin/preflight.sh write <file> [session-id]` before edits
(git state, artifact order, core-first adapter edit gate, and native memory-file write checks),
`adapters/codex/bin/preflight.sh read <prd.md> [session-id]` after actual spec
reads, and `adapters/codex/bin/preflight.sh capability <name> [cwd] [session-id]`
before spec-changing capability work. It can also run
`adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]` to carry the
fuller routing contract as a worker-startup/manual subcommand, not a per-turn
injection. Use `adapters/codex/bin/preflight.sh
memory [cwd]` for plain-text memory injection; Codex automatic SessionStart
context is opt-in via `CODEX_SESSION_MEMORY_INJECT=1`.
Use `adapters/codex/bin/preflight.sh recall <query> [cwd]` when the agent
chooses to retrieve memory explicitly; it is not a prompt-hook classifier.
Use `adapters/codex/bin/preflight.sh briefing [cwd]` to surface the same
daily oncall briefing without Claude hook JSON.
Use `adapters/codex/bin/preflight.sh worklog [cwd]` to inspect the configured
agent-notes/worklog-board state read-only before touching that layer.
Use `adapters/codex/bin/preflight.sh design <file>` after design HTML writes
to run the same console verification without Claude hook JSON.
Use `adapters/codex/bin/preflight.sh distill-delta <session-id>` for Codex
transcript extraction. `CODEX_DISTILL_ENABLE=1 adapters/codex/bin/preflight.sh
distill-propose <session-id> [cwd]` can generate a constrained proposal, but it
is a manual preview surface and does not auto-apply unless the apply and
contract-accepted env gates are explicit. Codex adapter-owned `session-end` and
`turn-nudge` paths are the verified automatic realization: after the documented
read-only `codex exec` tool-free proof, they default to automatic apply and opt
out with `CODEX_DISTILL_ENABLE=0`. They run only for an interactive main;
dispatch/title/distill/loop workers make both paths silent no-ops under D-42.
Use `adapters/opencode/bin/preflight.sh distill-delta <session-id>` for
OpenCode transcript extraction through `opencode export`. OpenCode's no-tools
worker contract is verified (`opencode run --pure --agent <distiller>` with all
tools disabled), so `distill-propose` runs the worker and the plugin
`event`/`session.idle` trigger auto-distills via `preflight.sh session-end`
(debounced, enabled by default for main sessions; opt out
`OPENCODE_DISTILL_ENABLE=0`). Worker sessions keep plugin write/read guards and
liveness heartbeats but skip automatic memory context and session-idle distill.
