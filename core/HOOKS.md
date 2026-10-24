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
| capability grounding | `hooks/capability-grounding-marker.sh`, `utilities/capability-grounding.sh` | `portable-check` | An inline entry-capability session — one that runs the capability without dispatching, so it leaves no `jobs.log` dispatch row — records its capability plus best-effort mode/intensity, so Fleet can show `capability(mode·intensity)` for it the way the dispatch options column already does for dispatched work. Capability is the exact entry-skill name (the ten `autopilot-*` entries only); mode/intensity are parsed from the invocation args (structured `--mode`/`--intensity`, else the fixed intensity vocabulary). Read-only signal keyed by session id, never blocks; the freshest invocation wins and the marker's mtime carries the same sid-reuse freshness rule as the spec marker. The marker lives at `${XDG_STATE_HOME:-~/.local/state}/agent-fleet/capability-grounding/<sid>` (override `FLEET_CAPABILITY_GROUNDING_DIR`); it does not write into the harness release tree, and an older release's `<agent-home>/.capability-grounding/` is a read-only fallback only (F-<next> fleet-route-chain-r2). | Run `utilities/capability-grounding.sh record --sid <id> --capability <name> [--mode <m>] [--intensity <i>] [--cwd <dir>]` on entry-skill invocation, or attach a marker hook to the runtime's skill-invocation event. A runtime without a skill-invocation event records the grounding from the capability router itself. |
| design post-write verification | `hooks/design-postwrite.sh` | `portable-check` | Saved design HTML should get deterministic console verification. | Run `hooks/design-postwrite.sh --file <path>` after design HTML writes, or attach it to a post-write event. |
| spec read observation | `hooks/spec-read-marker.sh` | `portable-check` | Records actual spec reads for workflow evidence and status displays; never denies a tool call. | Automatic PostToolUse read observation. |
| spec sync nudge | `hooks/spec-sync-nudge.sh` | `portable-check` | In a spec-backed project, a source edit that removes a value/identifier still described in `spec/*.md` should surface those spec lines so the corresponding spec text is synced as part of the change. Read-only: emits context only, never blocks. | Run `hooks/spec-sync-nudge.sh --file <path> [--old <s>] [--new <s>] [--cwd <dir>] [--format text]` after edits, or attach it to a post-write event that supplies the edited path and old/new strings. |
| memory injection | `tools/memory/mem.py inject` | `portable-check` | Inject relevant DB memory at session start. | Run `tools/memory/mem.py inject` for text output, or `tools/memory/mem.py inject --hook` when the runtime accepts Claude-style `additionalContext`; adapters may keep automatic session-start injection opt-in when the runtime can fire start events on resume or compact. |
| memory candidate exposure and agent-owned adoption | `hooks/mem-recall-inject.sh`, `tools/memory/mem.py candidates`, `tools/memory/mem.py recall` | `portable-check` | Every eligible main prompt gets a fail-open, capsule-only lookup: active current-project/global rows, headline plus ID, maximum six and 2,400 UTF-8 bytes, no bodies or access touch. The model decides relevance and reads the full record before applying it. The bridge records a same-turn receipt without blocking material work. | Register an adapter-native prompt bridge that supplies prompt, cwd, session, and native turn/message ID when available. Consume only the runtime's structured context field. Preserve the explicit `recall` helper for deeper search and hook-failure recovery. |
| local evidence exposure | `hooks/local-evidence-inject.sh` | `portable-check` | Every eligible main session start gets a fail-open presence probe of the cwd's artifact root: research/documents/analysis counts plus nine newest paths, taken a bucket at a time so a busy one cannot hide an idle one and at most once per artifact, bounded to 2,400 UTF-8 bytes, no bodies read, no prompt classifier. Silent when empty; workers exempt. Cached and time-bounded: it may lag the store by minutes; a truncated walk reports `N+`. Realizes `roles/response-policy.md` "Local evidence before recall": questions they cover start there, not in recall. | Attach it to the runtime's session-start surface, or its nearest once-per-session equivalent (`--cwd <dir> --format text\|hook-json`), consume only the runtime's structured context field, and keep every failure as zero context. A start event that does not repeat on compaction leaves the block gone for the rest of the session, so that runtime must re-seat it by its own means. |
| session card delivery | `hooks/session-card-inject.sh`, `utilities/session_tidy.py hook` | `portable-check` | Every eligible main session start and prompt asks the shared tidy state for the card and notice this seat is due, at most once per session and compaction round, 2,400 UTF-8 bytes, silent when none. It is independent of the memory candidate probe: a short or empty prompt, a missing memory tool or zero candidates still deliver it. Workers get and consume nothing; every failure is zero context. | Register a separate command on the runtime's session-start and prompt surfaces (`start` carries the start source such as compact; a runtime with no start event delivers at the first prompt and re-emits the kept text for every model call of that turn), never inside the candidate branch or behind the memory opt-in. A compact event only marks the round. |
| oncall briefing injection | `hooks/mem-briefing-inject.sh` | `portable-check` | On the dedicated agent desk, inject daily oncall report once per day. | Run `hooks/mem-briefing-inject.sh --cwd <dir> [--format text]` before prompt handling, or attach it to a prompt-submit event. |
| worklog state signal | `utilities/agent-worklog-state.sh` | `portable-check` | Surface configured `<agent-notes-root>` / `<worklog-board-app>` inventory without mutating data. | Run `utilities/agent-worklog-state.sh [cwd]` or an adapter wrapper before worklog-board or agent-notes work. |
| runtime hook output protocol | adapter hook bridges | `adapter-payload-wrapper` | Hook stdout must match the owning runtime's hook protocol exactly. Context-injection hooks emit the runtime's structured context object; side-effect-only lifecycle hooks keep stdout empty unless that runtime explicitly accepts a structured success object. Portable helper text is never forwarded as raw hook stdout. A valid pinned harness root keeps its bridge path even when the bridge is absent; root selection never replaces a missing pinned bridge with the current projection. | Each adapter must document its hook output contract, test the exact stdout shape for every native hook bridge, and route diagnostic/helper text to logs or stderr only when the runtime accepts it. |
| Fleet interaction wait signal | `tools/fleet/interaction.py`, `hooks/fleet-interaction-state.py`, adapter-native lifecycle bridges | `adapter-payload-wrapper` | An unresolved user decision or approval is recorded only as an exact `harness`/`session_id` allowlisted sidecar (`schema_version`, `harness`, `session_id`, `kind`, `source`, `waiting_since`). Question text, answer choices, commands, arguments, denial reasons, model output, and tool payloads are structurally absent. Producers never own the response and keep stdout empty; loss of the observational sidecar must not affect the session. | Translate native question/approval events into the same allowlisted writer and clear them at exact resolution/turn/session boundaries. If no exact runtime event exists, report `unknown`; never infer a wait by parsing prompt prose. |
| marked answer-wait observation | `utilities/human_gate_observation.py`, `utilities/human_wait_state.py` | `portable-check` | A final reply opens S4 only when its plain-text block starts with `[답변 필요]` or `[결정 필요]`. Start/prompt surfaces read the private derived cache and the current seat's exact S4 file; Fleet reads its separate question-free derived projection. One requested producer refreshes S1–S4 observations. Missing or expired cache is unknown, never a confirmed zero. Reading remains advisory and never changes persistent work state. | Observe the existing Stop/idle and start/prompt events; do not add a hook or gate. Failed or busy observation leaves normal session progress untouched. |
| Herdr state integration | `hooks/herdr-agent-state.sh` | `external-integration` | Publish working/idle/blocked/release state to Herdr. | Optional external integration; not a core invariant. |
| route presence | `utilities/route_presence_gate.py`; bridges `hooks/route-presence-gate.py` (Claude), `adapters/codex/hooks/route-presence-gate.py`, `adapters/opencode/plugins/hearting-guards.js` | `portable-check` | The one routing gate (`core/WORKFLOW.md` §0.4 participation invariant). Before a session's source edit inside a git work tree, `git commit` with non-artifact changes, or long-run launch (`compute-hosts run`, `nohup|setsid … python … train|run.py`), it checks one fact: does this session's route-chain ledger (`tools/fleet/route_chain.py`, keyed by the payload session id and the environment writer identity) name any route for the folder's canonical artifact root (`utilities/artifact-root.sh`)? If not, it refuses that call with one paste-ready `capability-route.py compose --shape direct --campaign-key <recent> --slug <stem-MMDD> --cwd <work-tree>` line for one atomic, reversible inline change and points other new work to `compose --start --prompt-file <task>` without `--shape`, so the frame decides; once any route exists there, open or closed, that session×folder passes for good. No route binding, freshness, lineage, stage or proof checks. Shell commands are matched only in plain forms; ambiguous ones pass. Registered owners/workers, artifact roots, the temp directory (scratchpad), runtime homes, paths outside git, CI, dev activation and every judgement failure pass; `HEARTING_ROUTE_GATE=off` disables it. | Call `utilities/route_presence_gate.py --codex` (Codex PreToolUse JSON) or `--opencode` (stdin `{tool,args,sessionID,cwd}`, exit 1 = refuse) from the runtime's pre-tool surface for edit and shell tools; a runtime without one records the gap in its ADAPTATION. |
| stage-dispatch reminder | `hooks/stage-dispatch-reminder.sh` | `portable-check` | SD-11: when a dispatch-depth-1 conductor at standard+ intensity is about to invoke a `code-{plan,execute,test,report}` sub-skill **in-session** (env `AGENT_DISPATCH_DEPTH=1`, `AGENT_DISPATCH_INTENSITY∈{standard,strong,thorough,adversarial}`), surface a reminder to dispatch the stage as a dispatch-depth-2 headless session instead. **Soft / non-deny** (fail-open) on every harness: emits `additionalContext` only, never blocks — the hook cannot tell a legitimate headless-unavailable fallback from a mistake (§8.5.2). | Run `hooks/stage-dispatch-reminder.sh --skill <name> [--dispatch-depth <n>] [--intensity <i>]` before a Skill call, or attach it to a pre-tool Skill event. |
| native subagent default model | `hooks/subagent-model-default.sh` | `adapter-payload-wrapper` | Native subagent spawns carry the adapter-config-declared default model tier instead of silently inheriting the interactive session model (core/ADAPTATION.md §3). An explicit per-invocation choice, agent-definition pin, or intentional parent-inherit surface wins only when the resolved model is delegation-eligible. A config-declared interactive-main-only model, explicit `inherit`, fork inheritance, or an unavailable eligibility policy is a typed deny for a valid spawn request; malformed/non-actionable payloads remain silent. | Realize the same config-declared default and main-only eligibility guard through the runtime's native subagent model configuration or pre-spawn decision surface, or record the surface as unsupported; do not consume the Claude PreToolUse payload shape as configuration. |
| registered-child completion delivery | adapter-owned session supervisor or checked single-ingress gateway | `adapter-coupled-automation` | Runtime-owned carriers per parent runtime (`core/ADAPTATION.md §7`); the parent sees one printed field. | The parent's model-visible contract is one field, not the carrier taxonomy: a launch receipt that spawned a child, and every steward line that reports an armed watch, carry `parent_next=end-turn` (a carrier owns it; yield) or `parent_next=bounded-wait` with the exact executable, bounded `parent_next_command`. A steward line claims a carrier only when the hook arms from that line *and* the watch's wake is the hook, so `wake=none` and a session-printed `rearm` are both waits, never `end-turn` (`utilities/parent_next_directive.py`), and an unrecognized delivery fails closed to `bounded-wait`. Carrier selection and requirements: `core/ADAPTATION.md §7.4`. |
| dispatch activation reconciliation | `utilities/dispatch_session_sweep.activate`, `dispatch_completion_join.reconcile_pending_delivery`, `dispatch_batch_obligations.ensure_observers` | `portable-check` | On canonical session activation, reconnect exact runtime-v1 owner settlements and run the existing dispatch reconciliation actor once. That actor alone materializes pending deliveries, expires only proven-dead recipients, and prunes retained terminal records; unknown liveness and missing PID evidence stay open. Exact-attempt reconciliation remains scoped to its one attempt. | Claude, Codex, and OpenCode call the shared activation helper from their native session-start boundary. Do not run the global actor on every prompt or give observers/Fleet a mutation path. |
| Claude peer-context receipt | `hooks/peer-message-record.py`, `utilities/peer-message.py` | `adapter-payload-wrapper` | Successful synchronous `additionalContext` delivery from the native `UserPromptSubmit` or `PostToolUse` hook is recipient consumption under SD-113 §13.34.1. The hook passes the exact sealed body through the shared receive/ACK writer; CLI context rendering, failed flushes, same-SID callbacks, helpers, and mismatched or unverified transfers do not receive. | Preserve each runtime's existing native receive evidence. This Claude-only `additionalContext` contract does not authorize Codex or OpenCode to infer receipt from output. |
| late child terminal conflict | `utilities/dispatch_completion_join.close_finished_child`, `dispatch_contract.reconcile_attempt_terminal`, `dispatch_stage_advance` | `portable-check` | Before cleanup of a committed PASS, inspect only its exact terminal log. A valid late FAIL/BLOCKED adds the existing terminal conflict while preserving the original result and receipt; unreadable or mismatched evidence adds no conflict. Join, stage advance, and owner settlement hold the conflicted PASS for review. | Reapers and joiners use the shared conflict transition and consumers. Adapters do not replace a PASS with a later verdict or invent conflict from unavailable evidence. |
| hook release pinning | `hooks/run-hook.sh` | `portable-check` | A registered dispatch tree runs its hooks from the release its launch resolved (`core/OPERATIONS.md` §5.9a): with `AGENT_HOME` naming a harness root, the runner runs `$AGENT_HOME/hooks/<hook>`; a missing hook keeps that path and the interpreter's missing-file result. Without a valid harness root it runs the hook beside the runner, which is the runtime projection now. It decides nothing else and returns the hook's own exit. | Name a projected hook through `hooks/run-hook.sh <interpreter\|exec> <hook> [arg...]`, or resolve `${AGENT_HOME}` first as the Codex `hooks.json` commands do. |
| start receipt observation | `utilities/start_receipt.py`, `hooks/start-receipt-context.py` | `portable-check` | A compose/start result is saved beside the dispatch registry and names its receipt file. A post-tool observation republishes the same session's new `parent_next` once, even when shell stdout was filtered. It never starts, waits, settles, changes a carrier, or blocks work; absent identity or failed storage gives no context. | Attach the shared reader to the native post-tool context surface. Claude/Codex use structured `additionalContext`; OpenCode appends the text to the completed tool output. A runtime without that surface reads the receipt file named by the CLI result. |

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
delivery is owned solely by the SD-78 supervisor (`core/ADAPTATION.md §7.1`),
which creates no pending-delivery record for it.

Claim authority for a claimed-state pending record has two grades:
`generation-proven` — full §13.33.1-(6) authority, including expiry and route
judgment — and `deliverer-unproven`, which carries delivery authority only: it
may claim a pending or lease-expired record by `session_id`/`recipient_digest`
match alone, inject a bounded receipt, and ack, with no judgment authority. The
record must persist the grade actually used in its `claim_authority` field; a
writer that cannot persist the field must not claim.

## Current Session and Context Evidence

Codex runtime activation (install, update, or refresh) may record the current native
hash for Hearting's own projected hook definitions through App Server. It preserves
disabled decisions, leaves foreign definitions to the user's `/hooks` review, and
conditionally restores only trust leaves it still owns during deactivation. If Codex
or App Server is unavailable, activation reports one skipped-trust line and `/hooks`
remains the fallback. Claude and OpenCode have no corresponding trust-record step.
peer-steward observes forms only and never writes trust or selects an option.

A process lifetime or launch session is not necessarily the conversation selected
now. A pane report names the current full native conversation only after the
live foreground process, PID/start, tty, and foreground process group agree with
that pane. For Codex, exactly one `thread-id` is the last `[tui].status_line`
item; two fresh complete reads of the complete UUID in the last visible native
footer field can prove the selected thread when an open rollout still names the
previous conversation. An earlier UUID, title, transcript, partial or clipped
field, or hidden footer is unknown. A late callback may report only while its
session still matches this current proof. Registered workers and shared services
never gain pane ownership from an inherited pane label. When current selection
cannot be proved, leave it unknown.

Context pressure is display-only. Keep raw active usage and model capacity
separate from configured requests, the runtime's applied compaction trigger,
and any output reserve. Interpret the exact consumer for the resolved runtime
execution and account for reserve once. Disabled, unavailable, stale, or
unbound policy is unknown. Known active usage of zero remains zero, and model
capacity alone is not an applied trigger. Bind policy to the selected session
or exact child attempt, preserve raw diagnostics, and show a gauge only when
usage and applied policy share that scope. An adapter interprets a runtime's
startup inputs only when they belong to that execution. Later user settings
and another home’s catalogue cannot replace a retained startup snapshot.
Missing startup evidence remains unknown, including an older rollout whose
runtime version cannot be bound to the resumed execution. A registered
worker's private native catalogue may be captured during its initialization,
bounded by that same attempt's native session-start evidence.
An adapter interprets a runtime's
compaction policy only for runtime versions whose consumer it has verified from
that runtime's own source or binary; any other version keeps raw telemetry and
shows unknown. The same acquisition serves interactive sessions and registered
workers through each runtime's existing callback, tap, plugin, or attempt stream.

## Adapter Rule

Adapters may reuse scripts directly only when they can supply the expected input
payload and consume the expected output decision. Otherwise, the invariant must
be wrapped or reimplemented behind an adapter-native event bridge.

Adapter hook bridges own the final runtime output protocol. A portable helper can
print human-readable status for explicit CLI use, but a native runtime hook must
not forward that text unless the runtime accepts it for that hook event. For
example, a context hook may emit `hookSpecificOutput.additionalContext` when the
runtime supports it, while a lifecycle side-effect hook such as a summary or state refresh
may need to perform the mutation with empty stdout or a minimal structured
success object so the runtime does not attempt to parse helper text as hook
JSON.

Codex realizes approval waits through its native `PermissionRequest` bridge and
clears them after native `PostToolUse` or a turn/session backstop. Its decision
reader accepts only structured rollout `response_item` function-call records
paired by exact call id; prompt wording is not an evidence source. Claude uses
its native question, permission, post-tool, prompt, stop, and session events.

Write-denying hook gates are retired; no write preflight or core-read marker is required.

Codex can run
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
Memory does nothing when a session ends (D-82). Claude `SessionEnd`, the Codex
`SessionEnd` bridge and the OpenCode `session.idle` event keep only their
non-memory work (Fleet/herdr state, the final summary, pane and heartbeat). Memory
instead exchanges in the background after a successful write and after a read that
finds the last receive older than ten minutes (`tools/memory/README.md`). Worker
sessions keep spec-read observations and liveness heartbeats but skip automatic
memory context.
