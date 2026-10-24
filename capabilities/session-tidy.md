# Capability: session-tidy

This is the portable capability contract for `session-tidy`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `session-tidy` |
| Group | `ops` |
| Supported modes | `none` |
| Portable meaning | Write a handoff card; tidy memory; clear the window. |
| Argument shape | `[정리] \| 정리만 \| 인계 <받을 세션>` |

## Invocation Semantics

Whenever a seat card is injected, its fixed header instructs the receiving session to
start its first reply, before any tool call, with one summary in the user's language,
about five lines using the three headings `하던 일 · 남은 일 · 다음 할 일`, then continue.
If the user first gives a different instruction instead of continuing, skip the summary
and follow only that instruction. This guidance survives body truncation within the
2,400-byte injection limit, including bundled memory references; it is absent from
late memory-only injections and peer handoff messages.

Claude Code, Codex, and OpenCode give the interactive main session a non-blocking notice based on the latest request's input usage: strictly above 500,000 tokens recommends `/session-tidy`, and strictly above 700,000 asks for tidying now. Each threshold is emitted once per native session ID; a jump across both emits the urgent notice once and consumes both. Compaction does not reset these notices. Owners, workers, auxiliary/reviewer sessions, and subagents are excluded. These notices do not change the runtime's compaction policy.

Runtime compaction counts as a session tidy invocation by the main session. Each completed summary may be saved to that session's own seat card and queues memory tidying; a shared project fallback seat only queues memory tidying, so one session's compaction never replaces another session's card. Preserve memory tidying under the existing stable operation ID in the durable queue before any card request or clear booking. Queue acceptance is independent of card-request delivery, booking expiry, cancellation, or clear eligibility; retries and normal hook reconciliation reuse that same queue item. Keep its source card generation unchanged. A card can authorize clear only through an exact recorded acceptance relation for its seat, harness, session, claim, operation, queue item, arrival and newly written card.

A nonempty automatic Claude or OpenCode summary saved to its own verified seat may use the existing tidy booking. For Codex remote-V2, an explicitly automatic compaction may use that same booking only after the exact main turn is confirmed complete: the bounded helper then sends one claim-bound request through the verified Codex pane, and the main session writes its real card and enqueues the matching operation. No empty or fabricated remote-V2 summary is used. Claude/OpenCode compact bookings and Codex card-request bookings wait for transcript turn-end evidence; idle alone is insufficient, and a compact booking may wait up to three hours. A matching card and durable queue receipt are required before handover snapshot and clear. Compact-origin clear keeps the existing continue behavior. Manual or unknown compaction, failed card persistence, a project fallback/outside-herdr seat, workers or sub-sessions, a changed session/input/card, or an uncertain card-request send never trigger automatic clear. Codex uses the verified pane transport; this does not add native attachment support. Never block or delay compaction to complete tidying. Worker sessions are excluded.

An optional idle tidy starts only from a main-session turn-end/idle event with a known latest-request input-context count at or above `idle.min_tokens`; unknown usage and stale or different-session usage are ineligible. The shared policy is `${XDG_CONFIG_HOME:-~/.config}/hearting/session-tidy.json`. When absent it uses `idle.enabled=true`, `idle.wait_seconds=3240`, `idle.min_tokens=200000`, and `idle.cache_margin_seconds=60`; `overrides.<harness>.<provider>` may override those idle fields. Invalid configuration withholds automatic idle work. The 54-minute wait is an operating default based on one user's observation, not a cache-TTL guarantee. Current adapter payloads do not expose a verified request cache expiry, so the configured idle wait is used and `cache_margin_seconds` is recorded but not applied. Provider, auth route and cache TTL remain unknown where configuration/request evidence cannot establish them. This policy does not change provider cache settings or restore automatic distillation.

At the due time, the existing helper and `utilities/peer-steward.py idle-card-request` may send one claim-bound request only to the same verified main session with an idle screen, empty input, no form/approval, and unchanged prompt/card identity. Preserve enough claim identity and input-frontier evidence in the existing seat ledger to classify a delayed or duplicate self-request after its booking is terminal or expired; this classification never restores clear permission. The main session writes its own card and queues the existing tidy; the helper clears only after the matching card and durable queue receipt are accepted. Codex automatic remote-V2 compaction reuses this same request, claim, card, enqueue, and helper progression after transcript-confirmed turn completion; it does not use idle token eligibility or create a second scheduler. An uncertain send is never retried. Any real prompt, busy/unknown target, draft, form, approval, missing token count for idle work, worker session or other-session identity prevents the send or cancels the booking. After an idle clear, the successor receives one short card-status request and waits for the next user input; it does not receive `이어서해` and does not restart work. Compact-origin continuation keeps its existing behavior, and compaction during an idle-origin booking does not change its status-only follow-up. The status reply, its Stop/idle event and duplicate callbacks do not arm another idle tidy. Only an actual user prompt in the exact successor session rearms that session; a delayed self-request, status event, duplicate callback or event from another session does not. A later actual user prompt remains eligible after an uncertain or failed status send, and a new session in the same pane does not inherit an older session's suppression. Manual tidy and ordinary compact clear/continue keep their existing behavior.

At a shared project fallback seat each session keeps its own card, so one session's card never replaces another's; a different session receives only the seat's newest card, once, and only if it first appeared after that card's author was last active.

The main-authored card keeps work in progress, waiting decisions, the next step
and related references first. Below those four fields, add "오늘 한 일"
(today's work, with PR/version/result pointers) and "사용자 지시" (brief user
quotations marked finished, remaining or deferred), within about 90 lines.
Write it from the conversation the main session already holds.

The calling main session writes its own handoff card and queues the tidy in one tool call (`utilities/session_tidy.py tidy`: the card is written first, and only a written card goes on to the existing `enqueue` and optional `handoff` for the same caller; `card` and `enqueue` stay available separately with the same meaning). A manual tidy therefore costs one tool call and one short closing reply that repeats the command's `say=` line. Automatic idle and compact card requests need no Skill and no tool: their own text asks for one reply whose first and last lines are the request's nonce markers with the card fields between them. At turn end the shared observer reads that final reply from the harness's own turn-end evidence (Claude and Codex Stop input `last_assistant_message`; OpenCode's newest assistant message only when it finished with `stop`) and, only for the exact claimed request that is still eligible, with both markers intact and no prompt after the request, writes the card and runs the same enqueue, so the clear follows the existing acceptance rules. An unmarked, cut-off, late or mismatched reply writes no card and books no clear; if the session ran `tidy` itself, the already accepted request is left as it is. `enqueue` returns at once while a detached runner (`utilities/session_tidy_runner.py`) starts one registered memory worker (`ops/session-tidy-memory`), applies its closed `add`/`supersede`/`reinforce` proposal through `mem tidy-apply`, and leaves one result line (with the undo command) for the seat's next session. The runner reads the conversation tail first: the newest unread part of each record goes to the worker, only a successful apply marks that part as read, and an older unread front stays pending for the next tidy; a record that was only partly read says so (`일부만 읽음(범위)`) and is never reported as "nothing new". The same run also refreshes the card's "참고할 기억" list (record ids and titles from what the batch really wrote plus the related existing records, at most 8 and 1,200 bytes), which the next session at the seat receives once with the card or, if the tidy ends later, at its next prompt. `handoff <target>` delivers the card to a peer session through `utilities/peer-steward.py prompt` only and reports its typed verdict. There is no required input and no confirmation step; a failure keeps the card and the watermarks unchanged and leaves one warning line, which counts any writes that had landed or may have landed and then carries the undo command.

Clearing and continuing are part of the default flow. Inside herdr, `enqueue` also books the calling window's clear (`utilities/session_tidy_clear.py`, a detached helper that never waits for the memory tidy): once the turn has ended and the window is idle, `utilities/peer-steward.py clear` types the harness's own new-conversation command once (Claude `/clear`, Codex `/clear`, OpenCode `/new`), only when no prompt was submitted after the card, no form is open and the input box reads as empty. A clear counts as confirmed only with evidence the conversation changed (the new session's start hook, its session id from the pane's process or herdr, a new thread id on Codex's status line — hearting's Codex status line shows `thread-title`, the thread id until the thread has a title — or OpenCode's home screen). After a confirmed clear, the helper asks `utilities/peer-steward.py continue` once for the exact new session. It preserves the booking, claim, card generation, seat, unchanged prompt count and expiry. An exact-native seat with a supported attachment keeps that bounded path: it rechecks the booked process, idle state, form and empty input, then writes the no-trailer continue command once to that session. Other targets create one no-trailer private pending ref bound to the new SID; the runtime's native carrier processes it and exact native receipt confirms consumption. Neither path targets an inherited pane, retries a send or invents a wake. A newer prompt, changed card, handoff, expired booking, changed SID or unavailable native callback leaves the booking pending or skipped under the existing result rules. `정리만` (`enqueue --no-clear`) keeps the window and cancels a pending booking; `enqueue --no-continue` clears without the continue action. Outside herdr the session prints the harness's manual clear command (Claude `/clear`, Codex `/clear`, OpenCode `/new`). A handoff clears only the calling window, never the target. Confirmed or retained delivery marks the card as handed off so that window does not continue from it; failed delivery preserves the card for normal continuation.

Running work follows the window. `enqueue` also snapshots registered depth-1 owner/frame attempts the tidying session answers for (the runtime reads them from the dispatch registry; a card's text grants nothing). The handover is capped at 12 distinct logical jobs, keyed by normalized registry path, route, route hash and node; open/running jobs are selected first, followed by the most recently encountered eligible finished jobs. Every eligible attempt and registered parent binding for each selected job stays in the flat schema-1 snapshot and ledger, so their row count can exceed 12. Repeated attempts of one job do not use extra job slots. When the next session at the same herdr pane starts through a confirmed clear (a `clear` start source or the clear booking's own observation; for OpenCode, its first message in a new session), `session_tidy.py hook` appends one handover row (old session → new session, with the bindings) to the seat ledger. A retry registered between snapshot and confirmed clear is added only when it belongs to a logical job already selected in that snapshot and passes the existing handover authority check. From then on the new session may resume those attempts (`work_start`, replacement authority, the Claude wake re-arm, the interactive frame handback) and receives their pending completions (Claude/Codex prompt sweeps, the Codex queue target), while the registry row keeps its registered `parent_sid`, receipt, digest and gate identity and the pending records stay stored, claimed and acked under that registered parent. One direction (old → new), the same pane and harness only, never old → two sessions at once; a further clear hands over only through the new session's own next tidy, and the session at the end of that chain answers for every attempt an earlier row bound unless a later row handed it elsewhere (delivery and resume authority read the same chain). The card carries the verified route and the existing resume command in one line. Without a handover every call behaves as before. OpenCode direct owners still complete through the poll fallback: the new session can resume them, but there is no turn carrier for their completion.

Pane seats include the exact Herdr socket when one is known, so matching pane
labels on separate endpoints keep separate session histories. OpenCode clear
and successor observation use the foreground TUI's current PID-birth-bound
selection. A delayed start callback cannot replace the seat after the native
selection has moved. Start reports retain the native sequence/source and
provider-supplied transcript when available. Herdr resume argv is the original
launcher array from the proven current process; missing fields stay omitted,
and saved argv is restoration data without execution or owner authority.

An inherited pane label never grants access to that pane's card or input. A
runtime that provides an exact, live native session target may instead keep a
separate seat for that target and clear it through its official attachment
command in a private terminal. The booking binds the runtime process, start
time and native job identity; clear and continue revalidate those bindings,
the prompt count, card generation, idle state and empty input. A confirmed new
conversation at that same target receives the card and existing work handover.
Other adapters use this same evidence rule: without an implemented exact
target transport they keep their checked pane path and manual fallback.
Claude's messaging inbox carries text, not new-conversation commands; an inbox
message containing `/clear` is never evidence that the conversation cleared.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Portable Procedure

Write the card yourself in the user's language, using the conversation you
already hold. Keep the four continuation fields first: work in progress,
decisions waiting, the next step, and related paths, PRs and sessions. Add
"today's work" and "user instructions" below them, keeping the whole card
within about 90 lines. Summarize today's work in a few lines and point to the
PR, installed version or result file. Quote each user instruction briefly and
mark it finished, remaining or deferred. The start preview remains the bounded
beginning of the card; read the full file for the rest. The memory worker's
"partly read" line describes its separate conversation input, not this card.
When naming work in any of those existing fields, keep the source description
and the comparison condition that distinguishes it, then add the original
identifier as a reference. Preserve document/revision and checkpoint links
when they affect meaning. Record failed, interrupted and unfinished work in
the same fields with its actual status and next action; do not guess what an
unexplained identifier means.

Example fields (use the user's language):

```text
진행 중: …
기다리는 결정: …
다음 할 일: …
관련: …
오늘 한 일: … (PR·설치 버전·결과 파일)
사용자 지시: "…" — 끝남 / 남음 / 보류
```

Continue with the existing enqueue and handoff flow. An ordinary peer message or card grants no successor authority and has no session-tidy accept command. A confirmed same-seat clear continues through the existing seat snapshot and handover ledger without peer acceptance. For a typed cross-seat session-tidy handoff, read the card and referenced documents, then use only the exact receiver command supplied by shared guidance. Delivery, queue consumption and ACK are receipt facts, not acceptance; the existing explicit acceptance still revalidates the current receiver and preserves the original requester, predecessor and successor lifetimes and completion delivery.

The same-seat snapshot carries already valid steward evidence and unresolved exact watch
references separately from registered bindings, including when the binding list is empty.
The common handover ledger preserves the original evidence generation, target, watch ID,
and registered storage/receipt identity while resolving the confirmed current holder.
Marker bytes are never copied to a successor session. A card, ordinary message, registered
batch or display alias cannot create the role. Release takes priority for role continuity,
while an accepted unresolved watch remains owned until its existing settlement or receipt
condition is met.

Peer messages and `이어서해` select the exact native session through the
existing harness carrier. A queued message remains owned by its pending record
until that session's native consumer records the exact reference. Unsupported
native wake keeps the existing pending delivery for the next native callback;
it does not read or send the target's composer. Clear retains its existing
foreground, form, draft, and successor checks.

The optional seat-lineage reader is a read-only projection of existing ledger,
card history and transcript locations. Card injection may include one
transcript line for the predecessor that an explicit handover edge names, never
a merely adjacent session; the complete hook output, including guidance,
notices, resume data and memory references, remains within the existing 2,400
UTF-8 byte limit.

When a new session starts from a clear or confirmed handover and has a
predecessor, show the person a short, non-blocking way to open that predecessor
read-only. Keep this human notice separate from model context and from the
2,400-byte injection budget. In herdr, use the managed `prefix+shift+h` command
when that exact command is installed and available; elsewhere show
`hearting run session_history`. Identify the predecessor and state whether its
original transcript is available, missing or unreadable. Never present a card
or generated summary as the original conversation. The viewer starts with the
explicitly confirmed predecessor, supports older generations, and reports
uncertain lineage rather than promoting a chronological neighbor to a confirmed
handover. Reading does not resume or modify a session. If the native transcript
is absent, unreadable or partial, say so and preserve that status. When no
terminal surface can be opened, print the read-only command/output fallback.
An unbooked manual `/clear` remains an unconfirmed chronological neighbor and
does not show the predecessor notice.

## Artifact Ownership

Artifact root: `core/CONVENTIONS.md §5.1`; output placement: `§5`.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- state lives under `${XDG_STATE_HOME:-~/.local/state}/hearting/session-tidy/` (directories 0700, files 0600), never in the artifact root. The one exception is the memory worker's exchange folder `<artifact root>/.runtime/session-tidy/<batch>/` (0700; the input copy, the prompt and the one `actions.json` it writes): its sandbox allows the artifact root the checked wrapper launches it with and not the state folder. The runner reads that file back without following a link, checks it, copies the checked result into state and deletes the folder (a failed batch keeps only the worker's answer until it is pruned); sandboxes and permissions are never widened for it;
- clear input goes only through `utilities/peer-steward.py clear`, at most once after the existing safety checks; exact-native seats may use their bounded attachment for one no-trailer continue write to the booked process after rechecking the empty input and form state, while other targets use one sealed pending ref through the native carrier and exact receipt; neither path targets an inherited pane;
- the memory worker never calls a memory write command; only `mem tidy-apply` writes, and it never deletes.
- compact cards put a delivered last reply before the summary and prefer it over conflicting summary claims of non-delivery or non-completion; preserve post-summary turns separately. Some recent turns may remain verbatim depending on runtime: do not infer delivery from an omitted summary turn, name the last turn the summary covers when known, and say when the preserved transcript needs checking. Never assume the tail is visible or available.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/session-tidy/SKILL.md` and `skills/session-tidy/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/session-tidy/SKILL.md`, while `skills/session-tidy/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info session-tidy`. Use `adapters/codex/skills/session-tidy/SKILL.md` as the native Codex Skill projection; do not consume `skills/session-tidy/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info session-tidy`. Use `adapters/opencode/skills/session-tidy/SKILL.md` and `adapters/opencode/commands/session-tidy.md` as native OpenCode projections; do not consume `skills/session-tidy/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/session-tidy/SKILL.md` and `adapters/claude/skills/session-tidy/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/session-tidy/SKILL.md`, while `skills/session-tidy/SKILL.md` remains the compatibility reference kept for parity/drift checks.

Only a valid current typed cross-seat handoff receives the existing explicit
read-and-accept command. It is not user approval and does not change registered
completion identity; mere delivery never accepts the handoff.
