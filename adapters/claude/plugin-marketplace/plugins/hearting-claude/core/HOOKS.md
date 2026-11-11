# Portable Hook Invariants

This document names the runtime-neutral invariants enforced by hook scripts.
It is not a hook registration file. Runtime adapters decide how to attach these
checks to their own event model.

## Verification Layers

Prompt discovery reuses code modules within its process and the existing compact
lineage projection within a read pass. A recipient with no incoming edge or
logical binding has only its own storage scope; a history notice with no confirmed incoming edge
needs no full activity history. Claims and acknowledgements still observe current
authority at their existing effect boundaries. These rules apply to all three
adapters and introduce no additional persistent cache or input.
Independent context probes may overlap within a prompt, preserving context
order and committing their deferred consumption only after the outer output.
A foreground runtime with no controlling terminal keeps its session identity
without searching unrelated panes. Shared background services still resolve the
exact foreground native binding; a terminal hint alone remains insufficient.

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

Post-edit context must reach the runtime's native result, including OpenCode V2
`event.result.content`. Preserve the completed result's original content and
metadata when appending context; failed tool events remain unchanged.

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
| capability grounding | `hooks/capability-grounding-marker.sh`, `utilities/capability-grounding.sh` | `portable-check` | An inline entry-capability session — one that runs the capability without dispatching, so it leaves no `jobs.log` dispatch row — records its capability plus best-effort mode/intensity, so Fleet can show `capability(mode·intensity)` for it the way the dispatch options column already does for dispatched work. It is recorded when the model loads an entry skill and when the person starts one by name (`/autopilot-code …`, the only start an explicit-only entry router has). Capability is the exact entry-skill name (the ten `autopilot-*` entries only); mode/intensity are parsed from the invocation args (structured `--mode`/`--intensity`, else the fixed intensity vocabulary). Read-only signal keyed by session id, never blocks; the freshest invocation wins and the marker's mtime carries the same sid-reuse freshness rule as the spec marker. The marker lives at `${XDG_STATE_HOME:-~/.local/state}/agent-fleet/capability-grounding/<sid>` (override `FLEET_CAPABILITY_GROUNDING_DIR`); it does not write into the harness release tree, and an older release's `<agent-home>/.capability-grounding/` is a read-only fallback only (F-<next> fleet-route-chain-r2). | Run `utilities/capability-grounding.sh record --sid <id> --capability <name> [--mode <m>] [--intensity <i>] [--cwd <dir>]` on entry-skill invocation, or attach a marker hook to the runtime's skill-invocation event and to the person's named start (Claude `PostToolUse[Skill]` and `UserPromptSubmit`; OpenCode `tool.execute.after` on `skill` and the generated `/autopilot-*` command body at `chat.message`). A runtime without a skill-invocation event records the grounding from the capability router itself. |
| design post-write verification | `hooks/design-postwrite.sh` | `portable-check` | Saved design HTML should get deterministic console verification. | Run `hooks/design-postwrite.sh --file <path>` after design HTML writes, or attach it to a post-write event. |
| spec read observation | `hooks/spec-read-marker.sh` | `portable-check` | Records actual spec reads for workflow evidence and status displays; never denies a tool call. | Automatic PostToolUse read observation. |
| spec sync nudge | `hooks/spec-sync-nudge.sh` | `portable-check` | Resolve the artifact root and canonical spec with `artifact-root.sh` → `artifact_reader.py spec-dir`; normalize edit and apply_patch payloads, then surface only removed values/identifiers still described in the spec and absent from the post-edit file. Prose filtering, removed-token calculation, and post-edit filtering run before helper subprocesses. Read-only, fail-open context only; never blocks. The shared hook has a 6s aggregate subprocess deadline; `SPEC_SYNC_DEADLINE_MS` may lower it and is clamped at 6s, below the Claude 10s, Codex hook 10s → bridge 8s, and OpenCode 8s ceilings. | Claude, Codex and OpenCode post-edit adapters pass native input to the shared hook; stdin `--format text` is adapter-neutral and Claude JSON remains available. |
| memory injection | `tools/memory/mem.py inject` | `portable-check` | Inject relevant DB memory at session start. | Run `tools/memory/mem.py inject` for text output, or `tools/memory/mem.py inject --hook` when the runtime accepts Claude-style `additionalContext`; interactive main sessions receive the same bounded summary by default, including native resume/clear/compact start events; Codex users can disable it with `CODEX_SESSION_MEMORY_INJECT=0` or, for a shared app-server daemon whose hooks keep the daemon's start environment, `"codex_session_memory_inject": false` in the shared memory policy file; start events drained before one request (resume then an immediate compact) each carry the summary, so that request may hold two copies (accepted; see core/MEMORY.md). |
| memory candidate exposure and agent-owned adoption | `hooks/mem-recall-inject.sh`, `tools/memory/mem.py candidates`, `tools/memory/mem.py recall` | `portable-check` | Every eligible main prompt gets a fail-open capsule lookup by default: active current-project/global rows, headline plus ID, maximum six and 1,200 UTF-8 bytes. Only explicit `MEM_CANDIDATE_BODY=1` permits the bounded one-candidate short-body exception defined in MEMORY §7.4. The model decides relevance and reads the full record before applying it. The bridge records a same-turn receipt without blocking material work. | Register an adapter-native prompt bridge that supplies prompt, cwd, session, and native turn/message ID when available. Consume only the runtime's structured context field. Preserve the explicit `recall` helper for deeper search and hook-failure recovery. |
| local evidence exposure | `hooks/local-evidence-inject.sh` | `portable-check` | The first eligible main prompt after session start probes the cwd artifact root: research/documents/analysis counts and nine newest paths, round-robin across buckets, at most once per artifact, bounded to 2,400 UTF-8 bytes with no body reads or prompt classifier. Silent when empty; workers exempt; cached and time-bounded, with `N+` for truncated walks. Realizes `roles/response-policy.md` "Local evidence before recall". | Compute at the first prompt, preserve its context, and re-seat it after resume/clear/compact through the same prompt path. Session start keeps projection and lifecycle recovery without rendering or consuming this prompt context. Upgrades remove only the old owned SessionStart evidence/sweep registrations; prompt registrations and user hooks remain. Commit the existing consumption receipt only after output; OpenCode caches and replays it for model calls. Manual `--cwd <dir> --format text\|hook-json` remains available. |
| session card delivery | `hooks/session-card-inject.sh`, `utilities/session_tidy.py hook` | `portable-check` | Each eligible main prompt asks the shared tidy state for the card and notice due, at most once per session and compaction round, within 2,400 UTF-8 bytes. Independent of memory candidates: short/empty prompts, a missing memory tool or zero candidates still receive the card. Workers get and consume nothing; failures are zero context. | Start records identity, start source, clear/resume/compact lineage and lifecycle recovery only. The receiving prompt renders the card together with pending deliveries and local evidence; consume only after the outer output succeeds. A compact event only marks the round. OpenCode receives at chat.message and replays the kept text for model calls of that turn. |
| oncall briefing injection | `hooks/mem-briefing-inject.sh` | `portable-check` | On the dedicated agent desk, inject daily oncall report once per day. | Run `hooks/mem-briefing-inject.sh --cwd <dir> [--format text]` before prompt handling, or attach it to a prompt-submit event. |
| worklog state signal | `utilities/agent-worklog-state.sh` | `portable-check` | Surface configured `<agent-notes-root>` / `<worklog-board-app>` inventory without mutating data. | Run `utilities/agent-worklog-state.sh [cwd]` or an adapter wrapper before worklog-board or agent-notes work. |
| runtime hook output protocol | adapter hook bridges | `adapter-payload-wrapper` | Hook stdout must match the owning runtime's hook protocol exactly. Context-injection hooks emit the runtime's structured context object; side-effect-only lifecycle hooks keep stdout empty unless that runtime explicitly accepts a structured success object. Portable helper text is never forwarded as raw hook stdout. A valid pinned harness root keeps its bridge path even when the bridge is absent; root selection never replaces a missing pinned bridge with the current projection. | Each adapter must document its hook output contract, test the exact stdout shape for every native hook bridge, and route diagnostic/helper text to logs or stderr only when the runtime accepts it. |
| Fleet interaction wait signal | `tools/fleet/interaction.py`, `hooks/fleet-interaction-state.py`, adapter-native lifecycle bridges | `adapter-payload-wrapper` | An unresolved user decision or approval is recorded only as an exact `harness`/`session_id` allowlisted sidecar (`schema_version`, `harness`, `session_id`, `kind`, `source`, `waiting_since`), with optional `request_id`, `turn_id`, and `item_id` copied only from the same exact native event. Question text, answer choices, commands, arguments, denial reasons, model output, and tool payloads are structurally absent. Producers never own the response and keep stdout empty; loss of the observational sidecar must not affect the session. | Translate native question/approval events into the same allowlisted writer and clear them at exact resolution/turn/session boundaries. Populate optional identity refs only when that runtime event supplies them; if exact identity is unavailable, report `unknown` and never infer a wait by parsing prompt prose. |
| Selected Fleet interaction detail and response | `utilities/human_gate_observation.py`, `utilities/human_wait_state.py`, `tools/fleet/interaction.py`, runtime-owned request bridges | `portable-check` | The existing wait markers and current/older list counts remain body-free. Opening one current QUESTIONS item may fetch only the detail needed to answer that exact request and keep its unsent draft in private, owner-only local state. Bind it to the exact available session and request identity; a message question also binds its question ID and sequence. Keep bodies, choices, recommendations, approval context and drafts out of list/count projections, shared summaries, logs and exports. Distinguish `submitted`, `received`, and `applied`; only an exact consumed receipt or nonempty direct human answer closes the same message question. Keep an unanswered draft until explicit handling or observed cancellation/replacement; elapsed time and empty input never decide. | The selected detail is local and on demand. Submit through the existing exact carrier for a current message question or the runtime's actual response surface for a native request, once per identity. A runtime without that surface keeps its native wait open and reports the fallback. Never answer through arbitrary PTY text or treat observation hooks as response authority. |
| marked answer-wait observation | `utilities/human_gate_observation.py`, `utilities/human_wait_state.py` | `portable-check` | Explicit message registration owns its private S4 source; marked final replies remain a compatible observational entry. Start/prompt surfaces read the private derived cache and the current seat's exact S4 file; Fleet reads its separate question-free derived projection. One requested producer refreshes S1–S4 observations. Missing or expired cache is unknown, never a confirmed zero. Reading remains advisory and never changes persistent work state. | Observe the existing Stop/idle and start/prompt events; do not add a hook or gate. Failed or busy observation leaves normal session progress untouched. |
| Herdr state integration | `hooks/herdr-agent-state.sh` | `external-integration` | Publish working/idle/blocked/release state to Herdr. | Optional external integration; not a core invariant. |
| route presence | `utilities/route_presence_gate.py`; bridges `hooks/route-presence-gate.py` (Claude), `adapters/codex/hooks/route-presence-gate.py`, `adapters/opencode/plugins/hearting-guards.js` | `portable-check` | Ordinary main-session edits and commits in the current Git worktree pass without a route check (§0.2.1 tiny-fix recording). Other-worktree edits and long-run launches retain the existing route-existence check and exemptions, including /tmp; native permissions and installed/shared-checkout protection remain independent. No size classifier or new gate is added. | Use the existing native turn/tool bridges; optional payload clues fall back to unknown observations. |
| automatic change record | `utilities/turn_change_record.py`; bridges `hooks/turn-change-record.py` (Claude `UserPromptSubmit`/`Stop`), `adapters/codex/hooks/userprompt-lifecycle.py` and `stop-lifecycle.py`, `adapters/opencode/plugins/hearting-guards.js` (`chat.message`/`session.idle`, child sessions skipped) | `portable-check` | Existing detached turn-start/end steps record unrouted source changes, bounded current-turn native command exit/failure/unknown facts, final reply and native references, with no agent start/finish, question, model or per-tool subprocess. Stop only hands optional native clues to the child. COMPLETE means record saved, not semantic task success. Preserve existing queue/locks/retries, prior-turn late-end state, old payloads and user off switch. Active explicit workflows own their work and retain bounded supplementary session observations without a completion claim; proven closed historical routes do not suppress later automatic records. Snapshot entries, path samples and evidence remain bounded, with total counts and explicit unknown/truncation markers. Real Git projects under /tmp count; artifacts/runtime/CI/registered workers remain excluded. Promotion links the start snapshot and observed evidence without changing source. Fleet/continuation/tidy read these as exact-session supplementary evidence, never default-route authority. Attribution remains observed Git status/HEAD differences plus commits, with concurrent-session caveats; missing start or missing end is unknown. | Use the existing native turn/tool bridges; optional payload clues fall back to unknown observations. |
| stage-dispatch reminder | `hooks/stage-dispatch-reminder.sh` | `portable-check` | SD-11: when a dispatch-depth-1 conductor at standard+ intensity is about to invoke a `code-{plan,execute,test,report}` sub-skill **in-session** (env `AGENT_DISPATCH_DEPTH=1`, `AGENT_DISPATCH_INTENSITY∈{standard,strong,thorough,adversarial}`), surface a reminder to dispatch the stage as a dispatch-depth-2 headless session instead. **Soft / non-deny** (fail-open) on every harness: emits `additionalContext` only, never blocks — the hook cannot tell a legitimate headless-unavailable fallback from a mistake (§8.5.2). | Run `hooks/stage-dispatch-reminder.sh --skill <name> [--dispatch-depth <n>] [--intensity <i>]` before a Skill call, or attach it to a pre-tool Skill event. |
| native subagent default model | `hooks/subagent-model-default.sh` | `adapter-payload-wrapper` | Native subagent spawns carry the adapter-config-declared default model tier instead of silently inheriting the interactive session model (core/ADAPTATION.md §3). An explicit per-invocation choice, agent-definition pin, or intentional parent-inherit surface wins only when the resolved model is delegation-eligible. A config-declared interactive-main-only model, explicit `inherit`, fork inheritance, or an unavailable eligibility policy is a typed deny for a valid spawn request; malformed/non-actionable payloads remain silent. | Realize the same config-declared default and main-only eligibility guard through the runtime's native subagent model configuration or pre-spawn decision surface, or record the surface as unsupported; do not consume the Claude PreToolUse payload shape as configuration. |
| registered-child completion delivery | adapter-owned session supervisor or checked single-ingress gateway | `adapter-coupled-automation` | Runtime-owned carriers per parent runtime (`core/ADAPTATION.md §7`); the parent sees one printed field. | The parent's model-visible contract is one field, not the carrier taxonomy: a launch receipt that spawned a child, and every steward line that reports an armed watch, carry `parent_next=end-turn` (a carrier owns it; yield) or `parent_next=bounded-wait` with the exact executable, bounded `parent_next_command`. A steward line claims a carrier only when it is the armed `watch` line *and* the watch's wake names one (`hook` on Claude, `queue` on a Codex steward whose native queue socket exists), so `wake=none` and a session-printed `rearm` are both waits, never `end-turn` (`utilities/parent_next_directive.py`), and an unrecognized delivery fails closed to `bounded-wait`. Carrier selection and requirements: `core/ADAPTATION.md §7.4`. |
| interactive and registered transient capacity continuation | `utilities/codex_capacity_resume.py`; registered Codex owners use `utilities/codex-app-server-supervisor.py` | `adapter-coupled-automation` | A structured transient overload continues on the same thread after 60/120/240/480/960 seconds, with at most five consecutive retries that made no real work. Registered Codex owners retry within the live supervisor and keep the same thread; usage/429 limits, auth, cancellation, semantic FAIL/BLOCKED, and other errors retain their existing terminal behavior. Real work starts a fresh retry chain. | Interactive Codex continues through its native queue observer. Registered Codex uses the same structured overload allowlist and delay schedule in its transport. Claude and OpenCode remain not realized: no verified structured native overload signal was found in the available fixtures and source, so their existing failure path remains. |
| dispatch activation reconciliation | `utilities/dispatch_session_sweep.activate`, `dispatch_completion_join.reconcile_pending_delivery`, `dispatch_batch_obligations.ensure_observers` | `portable-check` | On canonical session activation, reconnect exact runtime-v1 owner settlements and request the existing detached reconciliation actor once. The request only asks the shared obligation runner to run; the runner's periodic pass is what invokes `reconcile_pending_delivery`, and no hook callback calls the global actor itself. Hooks select and render recorded notices, checkpoints/refusals, and exact completion receipts from the runner-published recipient inbox and run states, reusing unchanged observations within that invocation; only recorded evidence suppresses stale notices and unavailable evidence stays recoverable. Heavy settlement/publication observation belongs to the controller. That actor materializes pending deliveries and expires only proven-dead recipients; retained terminal records are pruned by the same bounded prune that the post-exit reaper's HOME retention pass runs (core/OPERATIONS.md, per-attempt home retention); unknown liveness and missing PID evidence stay open. Exact-attempt reconciliation remains scoped to its one attempt. | Claude, Codex, and OpenCode call the shared activation helper from their native session-start boundary. Keep hooks fail-open and retain delivery duties without synchronous controller work. Do not run the global actor on every prompt or give observers/Fleet a mutation path. |
| steward periodic check and per-repository limit | `utilities/steward_check.py` (duty `steward-check` on the shared obligation runner), `utilities/peer-message.py` `grant_steward`, `tools/fleet/collectors/steward.py` `repo_supervision` | `portable-check` | A steward role carries one `steward-check` duty. The existing runner sends a session Fleet flags as steward one `session_notice` per configured interval (`steward.check_interval_seconds`, default 7200) with no input or Stop, merges while the notice is undelivered, has no catch-up, names concrete next actions (waiting targets paired with the repository's open records from `fleet.session_work.observe` that the shared `memory.open_stage` classification keeps open, records awaiting a user decision to raise rather than assign, peer requests still queued for delivery to the holder or a live target) whenever work remains and then gives no release advice — repeating each interval only while a target is live and unfinished, otherwise once per changed state — and sends only a role with no remaining work that is idle or all-finished one short release reminder, then no further model turn while that state is unchanged. Only `peer-steward steward check` records `last_checked`. Release, holder change or proven session end (per-harness proof as in core/OPERATIONS.md: Claude registry census, Codex open rollout, OpenCode client selection and runtime lifetime) retires the duty and its undelivered notice. `grant_steward` admits a new holder only below `steward.max_supervisors_per_repo` (default 2) per git common dir, counted by `repo_supervision` (the same count Fleet shows), under one grant lock inside the seat lock; existing holders, same-session re-grants and confirmed successors use no new slot, proven-ended sessions free theirs, unknown keeps counting, and a refusal writes no evidence while watch/start continue. | No hook event, per-session process or cron; the three lifecycle bridges reach it through `__ensure-obligations`. |
| native peer-context receipt | `hooks/peer-message-record.py` (Claude), `adapters/opencode/plugins/hearting-guards.js` `tool.execute.after` (OpenCode), `utilities/peer-message.py` | `adapter-payload-wrapper` | Successful synchronous `additionalContext` delivery from the native `UserPromptSubmit` or `PostToolUse` hook is recipient consumption under SD-113 §13.34.1. The hook passes the exact sealed body through the shared receive/ACK writer; CLI context rendering, failed flushes, same-SID callbacks, helpers, and mismatched or unverified transfers do not receive. Reading and claiming before the flush share one budget from hook spawn (2.5 s of the 10 s timeout); rows it does not reach keep their on-disk state for the next boundary, the Stop path or the runner. After the flush the hook must end before the timeout, because a cancelled hook's output is discarded after its rows were received: watch ACKs run in a child with a hard timeout, and a flushed run exits 8.5 s after spawn. | Preserve each runtime's existing native receive evidence. OpenCode realizes the same post-tool boundary for a completed tool: its plugin runs `peer-message.py context --event PostToolUse --native-delivery` as an asynchronous child (the shared OpenCode 2 service keeps serving every other window) after each completed main-session tool and appends the printed context to that tool's result, so a report that arrives while the session works reaches the model during the work (measured on OpenCode 2.0.32, 2026-11-10). One complete printed line is the flush: it is appended even if the child is killed afterwards, and a partial line appends and receives nothing. Unlike Claude, a tool that ends in error status keeps its own error result in OpenCode 2, so the plugin does not ask the helper there; those bodies stay pending for the next completed tool or the idle courier. An open question or permission keeps bodies pending. Codex does not infer peer-message receipt from output; it hands its owed Hearting records to the running turn through the separate in-turn carrier row. Claude's `Stop` (`peer-message-record.py stop`) continues the same turn with the owed Hearting records still pending (`decision: block`), ACKing only what the reason shows whole; when nothing more is owed it applies the in-turn carrier row's one follow-through pass with the same shared state, keyed by Claude's `prompt_id`. |
| Codex in-turn carrier | `utilities/codex_turn_carrier.py`; bridge `adapters/codex/hooks/turn-carrier.py` (`PostToolUse` `post-tool`, `Stop` `stop`), fed by `userprompt-lifecycle.py` | `adapter-coupled-automation` | Same meaning as Claude's wake and context carriers (`core/ADAPTATION.md` §7.4): records owed to the exact thread reach the running turn at the next tool boundary (`additionalContext`) or at `Stop` (`decision: block` continues the same turn), rendered by the shared `delivery_context` and ACKed as the prompt path does; un-acked steward watch receipts are included at `Stop`. Once per turn, a received required action that is more than reading (or a peer report prompt whose sealed transfer `prompt_origin` verifies for this thread; a quoted trailer is not one, nor a body whose first line is `[report]` or `[notice]`) with no tool run after it continues the turn with that action; `codex_turn_carrier.follow_through` is the one decision Claude's `Stop` also applies. Only ACKable records are rendered and each once per turn, so continuation always ends; output is not cut, and only records whose text fits the budget whole (a sealed batch as one unit) are ACKed, the rest staying pending. A completion without the exact running turn is left unclaimed; a notice in the same root is still carried. No waiting, parking, tool block, question or input. While a Codex turn runs, the peer courier leaves an ordinary session notice to this carrier (`core/ADAPTATION.md` §7.4). Workers are skipped; any failure is silent and leaves the records to the native queue and the next prompt. | Realize the same meaning through the runtime's documented same-turn context and continuation surfaces; Claude keeps `asyncRewake` exit-2 and synchronous `additionalContext`, OpenCode its `opencode-turn` plugin carrier. |
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

Native form identity uses the fields the runtime actually supplies. A form that
has no native turn ID stays bound to its exact native session, request, current
schema and owning connection; never manufacture a turn ID from its form ID.
Transports that require a native turn keep that requirement. The selected private
detail and explicit response still revalidate the current request before one
action; neither a display alias nor a workflow prefix substitutes for that proof.

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
that pane. For Codex, exactly one `thread-id` is the first `[tui].status_line`
item, so a narrow pane clips trailing telemetry rather than the UUID; installs
not yet refreshed keep it last. Two fresh complete reads of the complete UUID
in the first or last visible native footer field can prove the selected thread
when an open rollout still names the previous conversation. A UUID in a middle
field, title, transcript, partial or clipped field, two different complete
UUIDs at the two ends, or hidden footer is unknown; a clipped trailing UUID
voids a first field it is not a prefix of. A late callback may report only
while its session still matches this current proof. Registered workers and
shared services never gain pane ownership from an inherited pane label. When
current selection cannot be proved, leave it unknown.

The native harness and full session/thread ID identify the same conversation in
Herdr, Fleet, and peer delivery. Short display numbers are aliases allocated by
the shared session-tag policy; a pane, PID, or temporary display alias cannot
become another session address or grant delivery authority. Current native
selection evidence is consumed consistently even when process startup or rollout
evidence is missing or names an earlier selection. Keep the physical ownership
and fresh native-selection checks above; an unproved identity stays unknown.
A clipped or unconfigured footer supplies no identity and does not invalidate
independent process-native proof.

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
context is enabled by default; `CODEX_SESSION_MEMORY_INJECT=0` disables it and
`CODEX_SESSION_MEMORY_INJECT=1` retains its explicit enable meaning. A shared
app-server daemon's hooks see the daemon's start environment, so use
`"codex_session_memory_inject": false` in
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/memory-sync.json` (or `--no-daemon`).
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

### Native resume recovery

The shared session hook records native resume starts and retains interrupted
work through the existing ordinary notice courier. Claude, Codex and OpenCode
use the same completion/user-wait exclusions and one-send semantics described
in [session-tidy](../capabilities/session-tidy.md#interrupted-resume).
