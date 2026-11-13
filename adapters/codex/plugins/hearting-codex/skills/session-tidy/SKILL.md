---
name: session-tidy
description: "Use when context fills, before compact, on handoff to another session, after a large task, or on request. Not for primary routing or memory writes."
---

# session-tidy

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/session-tidy.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info session-tidy`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Read `capabilities/session-tidy.md` for the runtime-neutral contract.
2. Run `adapters/codex/bin/preflight.sh capability-info session-tidy`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `session-tidy`
- Invocation class: `model-support`
- Supported modes: `none`
- Argument shape: `[정리] | 정리만 | 인계 <받을 세션>`
- Portable meaning: Write a handoff card; tidy memory; compact the same session.

## Portable Contract

- Invocation semantics: Whenever a seat card is injected, its fixed header instructs the receiving session to start its first reply, before any tool call, with one summary in the user's language, about five lines beginning with `원래 목표`, then `진행 중 · 기다리는 결정 · 다음 할 일 · 관련 세션`. Preserve dated `사용자 요구 원문 발췌` and `요구사항·PRD 위치` verbatim; read their card-history source when the bounded card refers to it. If the user first gives a different instruction instead of continuing, skip the summary and follow only that instruction. This guidance survives body truncation within the 2,400-byte injection limit, including bundled memory references; it is absent from late memory-only injections and peer handoff messages. For interactive sessions, the logical card, memory result and supported continuation address is the exact `(harness, native session/thread ID)`. Pane, endpoint and attachment data describe optional physical transport and never replace that identity. A different native ID may receive work only through an existing confirmed task or handover lineage. Missing or ambiguous identity does not borrow a project-latest card; an absent or changed pane limits only operations that require its exact physical binding. Wrapped Claude and Codex panes reuse the existing birth-bound attachment to the inner native session at discovery and immediately before an input effect. The outer foreground process is transport, not a replacement session identity. Existing endpoint, current-root, ambiguity and input-readiness checks apply unchanged; Claude's caller, inner PID/start and native SID must agree on that same pane and socket. Native card storage remains available without a physical pane. Claude Code, Codex, and OpenCode give the interactive main session a non-blocking notice based on the latest request's input usage: strictly above 500,000 tokens recommends `/session-tidy`, and strictly above 700,000 asks for tidying now. Each threshold is emitted once per native session ID; a jump across both emits the urgent notice once and consumes both. Compaction does not reset these notices. Owners, workers, auxiliary/reviewer sessions, and subagents are excluded. These are work-preservation notices, not a guarantee of remaining context or time before native compaction; they do not change the runtime's compaction policy. When the work folder has an experiment catalog (`utilities/experiment_catalog.py`), the writer is given its group names, goals and condition names with the writing guide and calls each condition by that catalog name; the saved card then adds the catalog name next to a run id, a `run:row`, a former name or an alias of 3+ characters that the writer left bare. Quoted text, code, paths and URLs are not touched, and an ambiguous token stays as written. Native compaction does none of this (below). Native runtime compaction is not a session-tidy invocation. PreCompact adds no card instructions; PostCompact records the exact native session's compaction boundary only. It creates no card, memory-tidy queue, card request or continuation. Never block compaction or ask the model to repeat its summary. Manual session-tidy and handoff behavior below is unchanged by it. Automatic idle tidy has one shared policy for Claude, Codex and OpenCode. The optional `${XDG_CONFIG_HOME:-~/.config}/hearting/session-tidy.json` keeps `idle.enabled=true`, `idle.wait_seconds=21600`, `idle.min_tokens=500000` and `idle.cache_margin_seconds=60` as its defaults. The existing `{'idle':{'enabled':false}}` switch disables it; installation never enables an operator-disabled policy. Unknown latest input usage is ineligible. The wait is a configured operating default, not a verified provider cache lifetime. The six-hour default tidies after the cache has cooled rather than just before expiry. `cache_margin_seconds` remains readable for compatibility and is recorded in the booking, but has no scheduling effect and does not shorten the wait. Idle means no activity for that wait, counted from the end of the latest user, peer, notification, completion or runtime-resume turn; Hearting's own tidy-request turn is excluded. The default context floor matches the recommendation notice's 500,000-token value. Each actual user-work interval permits one automatic request, including failed or uncertain requests. Runtime notifications, other sessions' messages, status lines and Hearting's own requests never rearm it. Other completed turns only move an unclaimed idle deadline; they do not create another automatic request in the same user-work interval. New activity after a request is claimed cancels its remaining automatic input without rearming the consumed interval; the saved card and memory work remain available. Recognize the runtime's transport wrapping of an exact recorded self-request without treating the wrapper as user input. Keep this classification after booking expiry. A newer compaction boundary cancels an unsent idle booking; pre-compaction usage cannot qualify the shortened conversation. Only an actual new user prompt rearms it. Use the existing session ledger and booking; add no gate or state store. Unresolved clear lineage does not block this same-session operation. A source with a confirmed successor remains ineligible. At the due time, the existing memory helper prepares the external handoff card and memory actions in the same private output. It reads the caller's conversation and complete previous card, follows the Card Format below, and keeps its existing deep editor role. The main conversation receives no card request or card-body reply. The runner saves the card for that exact native session before requesting native compaction: `/compact` in Claude and Codex, the equivalent command in OpenCode. Never use `/clear` or `/new` for idle tidy. Preserve scrollback, the native session and its background agents. Native background agents alone do not postpone idle tidy or select a special adapter path. A session responsible for an open registered route or owner/worker attempt is not idle: use the same route projection as its status line and Fleet to withhold both booking and input while that work remains open. An absent target session or incomplete route observation is unknown, so it withholds input too. After observations, recheck the current booking and its activity/claim before the existing writer sends; an old copy cannot permit a cancelled request. A running foreground turn, a nonempty composer or an open question/approval prevents either input. Physical binding and input readiness are transport safety only; success is established solely by the stored card and a later exact-session compaction boundary in native history. A screen label, home view, changed pane or transport ACK is never compaction evidence. Interrupted observation resumes from the same booking without typing either request again. After successful card persistence and compaction, show only one short localized line, such as '쉬는 동안 정리: 카드 저장, 대화 압축'. Do not send a status or continue prompt, restart work, or show skipped/cancelled attempts. Keep operation IDs, byte coverage and long conditions in internal evidence, out of this notice. Claude installation manages `idleCompaction:false` independently of normal auto-compaction and restores its prior value on uninstall. When an exact native identity is available, each session keeps its own card independently of the current project or pane seat; one session never replaces another's card. A different native identity receives a card or result only through the existing confirmed lineage and receipt rules. Calls without an exact identity retain the shared project fallback behavior, with author checks and no inferred recipient. Every card follows the single Card Format below. Manual cards use the main session's conversation; automatic idle cards use the helper's recorded input. The core comes first; an automatically selected module follows. Dated user wording and PRD locations remain verbatim, including in the existing card history when they exceed the bounded display. A manually invoked tidy writes its handoff card and queues memory work in one tool call (`utilities/session_tidy.py tidy`), followed by the command's short `say=` reply. `card`, `enqueue`, `--no-clear`, `--no-continue` and peer handoff keep their existing meaning. Manual memory-tidy results retain their undo and partial-coverage details. Automatic idle work needs no main-session Skill, tool or model turn. Its existing booking and memory queue bind the helper output to the unchanged session, activity frontier and card generation. A late helper never replaces a newer manual card or writes after cancellation or new activity. Repeated callbacks reuse that same queue and accepted card. Memory application can finish after card persistence and does not hold up compaction when it fails. Idle work does not publish its internal operation/coverage/undo text as a second user notification. Only the saved-card plus native-compaction result emits the short human notice, through the existing adapter output and notice receipt handling, with no model turn. Manual and idle tidy end the same way: one native `/compact` in the same session, for Claude, Codex and OpenCode alike. No tidy types `/clear` or `/new`, so the session id, its Fleet number, its supervisor role, its native background agents and its pending completion notices stay where they are, and the earlier conversation stays visible above the compaction. Inside herdr or tmux, `enqueue` also books that compaction for the calling window (`utilities/session_tidy_clear.py`, a detached helper that never waits for the memory tidy): once the turn has ended and the window is idle, `utilities/peer-steward.py clear` types `/compact` once, only when no person prompt was submitted after the card (runtime task notifications do not count), no form is open and the input box reads as empty. The compaction counts as confirmed only from that session's own native history after the claim (Claude's `compact_boundary` row, Codex's `compacted` row, or the session's PostCompact/compaction ledger row); a screen label or transport ACK is not evidence. The PostCompact of a booked compaction writes no card and queues no second memory tidy. After a confirmed compaction a manual booking asks `utilities/peer-steward.py continue` once for the same session. It preserves the booking, claim, card generation, seat, unchanged prompt count and expiry. An exact-native seat with a supported attachment keeps that bounded path: it rechecks the booked process, idle state, form and empty input, then writes the no-trailer continue command once to that session. Other targets create one no-trailer private pending ref bound to the same SID; the runtime's native carrier processes it and exact native receipt confirms consumption. Neither path targets an inherited pane, retries a send or invents a wake. The card reaches that continue turn because the compaction starts a new card epoch for the same session. A newer prompt, changed card, handoff, expired booking, changed SID or unavailable native callback leaves the booking pending or skipped under the existing result rules. `정리만` (`enqueue --no-clear`) keeps the window as it is and cancels a pending booking; `enqueue --no-continue` compacts without the continue action. Outside herdr or tmux the session prints the manual `/compact` command. A handoff compacts only the calling window, never the target, and that window does not continue from a card marked as handed off; failed delivery preserves the card for normal continuation. Running work stays with the session: there is no successor, so a tidy takes no dispatch handover snapshot and records no succession or handover row. Registered attempts keep their registered parent, waits and pending completions are delivered to that same session by the existing carriers, and the card carries the verified route and existing resume command in one line as before. Existing snapshot, succession and handover rows written by earlier tidy clears remain readable through the lineage rules below; a person's own `/clear` or `/new` still receives the seat card in the new conversation (the card is due to a different session), creates no lineage edge, and leaves running work with the registered parent. Pane seats include the exact Herdr socket when one is known, so matching pane labels on separate endpoints keep separate session histories. OpenCode session observation uses the foreground TUI's current PID-birth-bound selection. A delayed start callback cannot replace the seat after the native selection has moved. Start reports retain the native sequence/source and provider-supplied transcript when available. Herdr resume argv is the original launcher array from the proven current process; missing fields stay omitted, and saved argv is restoration data without execution or owner authority. Human input displays use the exact-session prompt frontier after its existing source classification. `prompt_display(seat, harness, sid, current=...)` is the shared reader for Fleet and an optional session-shell panel. Its human row means the person's last typed input; peer, runtime and tidy input stay separate. Display text grants no input readiness, ownership, lineage or approval, and is excluded from public snapshots and exports. An inherited pane label never grants access to that pane's card or input. A runtime that provides an exact, live native session target may instead keep a separate seat for that target and compact it through its official attachment command in a private terminal. The booking binds the runtime process, start time and native job identity; compact and continue revalidate those bindings, the prompt count, card generation, idle state and empty input. The same session at that target then receives the card on its continue turn. Other adapters use this same evidence rule: without an implemented exact target transport they keep their checked pane path and manual fallback. Claude's messaging inbox carries text, not session commands; an inbox message containing `/compact` or `/clear` is never evidence of either. Confirmed tidy succession and same-seat handover edges form one lineage for both previous-conversation display and pending-result delivery. A resume that moves panes remains connected by its recorded session id. A fork leaves its source and ancestors unresolved; independent descendants retain their own confirmed suffix and directly started jobs. Reverse discovery stops before an unresolved origin. A merge in that conversation lineage, self-loop, or cycle invalidates its whole connected component. Accepted retire transfers also name notice recipients, rather than previous conversations: several retired sources may converge on one recipient without making that recipient or its normal tidy suffix ambiguous. Different forward recipients, cycles and a legacy source whose harness is ambiguous remain unresolved. Exact registered-job ownership keeps its separate binding checks. Incomplete observation cannot grant authority. Open records stored under a predecessor's session key may be delivered to the unique confirmed same-harness chain end, while storage, delivery id, claim, and acknowledgement stay under the original key. An explicit registered-job binding to another recipient takes precedence. A unique exact job binding retains that authority across a fork; conflicting bindings on different arms remain unresolved regardless of row order or age. Serial exact handoffs retain their existing authority. Storage enumeration never substitutes for the current per-record recipient judgment. Derived recipient inboxes from the previous authority semantics are rebuilt through normal reconciliation. Only tidy-created succession and handover rows are lineage edges; manual `/clear`, chronological neighbors, pane aliases, and card text do not create edges. Unclear liveness or lineage remains unresolved. Existing activation, prompt, and runner sweeps discover open records that have no retained duty. New notices retain a duty when written so an idle recipient can be woken. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable. ### Read-only prompt display Fleet reuses the existing prompt-source judgment and exact native-session frontier. Keep a bounded first line and timestamp for the last real prompt and recent received peer/runtime/Hearting inputs in that same frontier. Generated inputs retain the last real prompt; an unknown or different session supplies no substitute. This is display data only: it changes no idle rearming, wait, claim, delivery or completion decision. Readers reuse file-generation caches and never rescan conversations on a rendering tick or create another store.



## Projected Portable Details

## Artifact Ownership

Artifact root: `core/CONVENTIONS.md §5.1`; output placement: `§5`.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- state lives under `${XDG_STATE_HOME:-~/.local/state}/hearting/session-tidy/` (directories 0700, files 0600), never in the artifact root. The one exception is the memory worker's exchange folder `<artifact root>/.runtime/session-tidy/<batch>/` (0700; the input copy, the prompt and the one `actions.json` it writes): its sandbox allows the artifact root the checked wrapper launches it with and not the state folder. The runner reads that file back without following a link, checks it, copies the checked result into state and deletes the folder (a failed batch keeps only the worker's answer until it is pruned); sandboxes and permissions are never widened for it;
- tidy `/compact` input goes only through `utilities/peer-steward.py clear`, at most once after the existing safety checks; exact-native seats may use their bounded attachment for one no-trailer continue write to the booked process after rechecking the empty input and form state, while other targets use one sealed pending ref through the native carrier and exact receipt; neither path targets an inherited pane;
- the memory worker never calls a memory write command; only `mem tidy-apply` writes, and it never deletes.
- existing archived runtime-compact cards put a delivered last reply before the summary and prefer it over conflicting summary claims of non-delivery or non-completion; preserve post-summary turns separately. Some recent turns may remain verbatim depending on runtime: do not infer delivery from an omitted summary turn, name the last turn the summary covers when known, and say when the preserved transcript needs checking. Never assume the tail is visible or available.

## Portable Procedure

Modes are `정리` (default: compact this session and continue), `정리만` (keep
this window as it is), and `인계 <받을 세션>` (pass the card to an
already-running peer; only this window may compact). Use
`python3 $AGENT_HOME/utilities/session_tidy.py tidy`; add `--no-clear` for
`정리만`, `--no-continue` when asked to stop after tidying, and
`--handoff <받을 세션>` for a peer handoff. Card writing, queueing and optional
handoff keep their existing one-call behavior. Reply with the command's `say=`
line; report its `prompted=` line for handoff and do not retry an uncertain send.

`정리만` / `--no-clear` saves the card and queues the existing memory work while
keeping the conversation uncompacted. Saving does not shrink its context, disable native
automatic compaction or establish that compaction was avoided.

A supervisor or user may run the same transfer for a session it names:
`python3 $AGENT_HOME/utilities/session_tidy.py handoff --from <세션> --kind <하네스>`.
It names only the source session and the successor harness; the runtime starts
the successor beside the source, delivers the current card (or, when the card is
older or missing, the source conversation's transcript location), and records
the same logical handover. Acceptance and retirement stay the existing ones. A
working source is handed over immediately; its window closes after the work
finishes. A start failure leaves no card generation, lineage row, pending
message, or retry notice. Only live windows are eligible, and the supervisor
role is not moved. Report the command's one result line.

A nonce-bounded idle card request is answered with the card itself
between the request's marker lines, with no tool call. The turn-end observer
stores that reply and queues the tidy. For a peer successor at this seat, use
`peer-steward.py start <name> --kind <harness>`, then the existing tidy command
with `--no-clear --handoff <name>`. The successor retires the predecessor only
after its ACK and the predecessor's final idle result, using the existing
`peer-steward.py retire <predecessor>` flow.

Write the card yourself in the user's language from the conversation already
held, using the Card Format below. Do not add a classification turn or ask the
user which format to use. Before real work, reread the complete card, any full
source it references in card-history, and the PRD current-requirements section.
Preserve the original goal and dated user wording across generations; append
new wording without re-summarizing and remove only what the user replaces.

### Card Format

This block is the single format definition used by the shared writer and the
three generated adapter instructions. It is embedded contract data, not a new
operator configuration. Labels in the user's language may use the read aliases.
The core's order is fixed. Confirmed responsibility takes precedence over the
main route capability, which takes precedence over an unambiguous work-folder
hint; uncertainty selects no module. The same card-writing response makes the
choice from context already known. No classifier call, question, required input,
role authority, or adapter-specific rule is introduced.

```json session-card-format
{
  "core": [
    {"id": "original_goal", "label": "원래 목표", "aliases": ["Original goal"]},
    {"id": "in_progress", "label": "진행 중", "aliases": ["Work in progress", "In progress"]},
    {"id": "waiting_decisions", "label": "기다리는 결정", "aliases": ["Decisions waiting", "Waiting decisions"]},
    {"id": "next_actions", "label": "다음 할 일", "aliases": ["Next steps", "Next actions"]},
    {"id": "related", "label": "관련 세션", "aliases": ["관련", "Related sessions", "Related"]}
  ],
  "preserved": [
    {"id": "user_request_quote", "label": "사용자 요구 원문 발췌", "aliases": ["Original user wording", "User request quotes"]},
    {"id": "requirements_prd_location", "label": "요구사항·PRD 위치", "aliases": ["Requirements / PRD location", "PRD location"]}
  ],
  "legacy": [
    {"id": "today", "label": "오늘 한 일", "aliases": ["Today's work"]},
    {"id": "user_instructions", "label": "사용자 지시", "aliases": ["User instructions"]}
  ],
  "selection_order": ["confirmed_role", "main_route_capability", "work_folder"],
  "modules": [
    {"id": "supervision", "label": "지휘·감독", "roles": ["supervision", "supervisor", "steward", "지휘", "감독"], "capabilities": [], "folders": [], "keep": "assignee/session, route, last verified state/time, result path, next action; when it supervises experiments, the experiment catalog location and each condition by its catalog name"},
    {"id": "publication", "label": "출판·릴리스", "roles": ["publication", "release", "publisher", "출판", "릴리스"], "capabilities": ["autopilot-ship"], "folders": ["releases"], "keep": "pending PR/head/CI run, dependency order, approved merge/install scope, installed version/receipt, queue path"},
    {"id": "code", "label": "코드 구현", "roles": ["code", "implementation", "developer", "코드", "구현"], "capabilities": ["autopilot-code"], "folders": [], "keep": "repo/worktree/branch/head, dirty/conflict state, change goal, verified tests/failures, PR/CI reference"},
    {"id": "research", "label": "연구·실험", "roles": ["research", "experiment", "researcher", "연구", "실험"], "capabilities": ["autopilot-research", "autopilot-lab"], "folders": ["research", "experiments"], "keep": "run/host/GPU/state, config/checkpoint, input/truth/scoring conditions and paths, metrics/units, unknowns; experiment catalog location, one-line goal, and each condition by its catalog name (run id in parentheses)"},
    {"id": "document", "label": "문서 작성", "roles": ["document", "writer", "editor", "문서", "작성"], "capabilities": ["autopilot-draft", "autopilot-refine", "autopilot-apply"], "folders": ["documents", "papers"], "keep": "audience, current deliverable/revision/source, tracked changes, remaining sections/evidence, delivery state/time"},
    {"id": "operations", "label": "운영", "roles": ["operations", "operator", "운영"], "capabilities": [], "folders": ["ops"], "keep": "service/host/version, last run/failure/cost/usage time, next action/hold conditions, relevant backup/restore path"}
  ]
}
```

Put only the chosen module after the five core sections, followed by the dated
user-request excerpts and PRD location. Unknown work uses the core plus any
existing preserved excerpts/PRD; it invents no module or missing facts. Keep
unique instructions, restrictions, unresolved obligations and failed/interrupted
work with its actual next action. Keep the source description and comparison
condition before its identifier, and observed session number, pane and exact
native ID together. Unknown identity stays unknown and grants no authority.
Completed chronology is a reference to an existing result or card history,
not another narrative copy. Mark the latest verified result and its time;
do not carry an old 'not reported' status over a preserved delivered reply.

The general card body stays within 8,192 UTF-8 bytes. Overflow is kept as the
same generation's complete original in the existing `card-history/<card>/`
JSON record before the bounded latest card is written. The latest card names
that exact source. Never replace that full source with its bounded copy on the
next write. Preserve every core heading and every recognized preserved/module/legacy
heading present in the source, including user instructions. Reserve these
sections first and divide content space fairly: satisfy short sections in full,
then share their unused space among the remaining sections. A large original
goal never consumes the working state, decisions, next action or instructions
budget. Retain whole sections or complete lines within each share, with an
explicit source reference for the rest. A single oversized quote/path line moves intact to that
source; never split a word, UTF-8 character, path, or line to fill space. A field
not present in the original is unknown, not invented. Existing history retention
continues to apply. Old nine-section cards remain readable, including `관련`,
`오늘 한 일` and `사용자 지시`; no migration or new state store is required.
The 2,400-byte injection remains a preview with its full-card location and uses
complete lines too; this changes neither its event nor idle scheduling/compaction.

Continue with the existing enqueue and handoff flow. An ordinary peer message or card grants no successor authority and has no session-tidy accept command. A same-session compaction needs no peer acceptance; earlier confirmed same-seat clears keep their recorded snapshot and handover ledger rows. For a typed cross-seat session-tidy handoff, read the card and referenced documents, then use only the exact receiver command supplied in the sealed handoff body. Explicit acceptance revalidates the native receiver, replays the existing logical record, and retains reply and retirement separately; it does not infer reading from delivery. The retained reply and predecessor retire keep their existing duties and exact identities. Delivery, queue consumption and ACK are receipt facts, not acceptance; the original requester, predecessor, successor, and completion storage identities remain unchanged.

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
it does not read or send the target's composer. The tidy `/compact` retains its
existing foreground, form, draft and same-session checks.

A peer held during the idle card turn or compaction retains its existing
message obligation. Completing the exact session's compaction wakes that
carrier without a status or continue prompt. The existing booking serializes
input admission: a claimed compaction defers peer input until its boundary;
peer input admitted first follows the ordinary new-input rules. The completed
booking and unchanged prompt frontier can corroborate idle after compaction
when a pane's working label lags; identity, foreground, form and draft checks
still apply. Activity after the actual native compaction boundary remains busy,
even if the booking's completion is recorded later; an unknown boundary keeps
the normal working guard. Claude, Codex and OpenCode use this same rule.

The optional seat-lineage reader is a read-only projection of existing ledger,
card history and transcript locations. Card injection may include one
transcript line for the predecessor that an explicit handover edge names, never
a merely adjacent session; the complete hook output, including guidance,
notices, resume data and memory references, remains within the existing 2,400
UTF-8 byte limit.

When a new session starts from a confirmed handover (including a tidy clear
recorded before tidy switched to compaction) and has a predecessor, show the person a short, non-blocking way to open that predecessor
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
An explicitly named or current native identity is resolved before inherited
pane hints. If its pane is unavailable or has moved, the same confirmed
predecessor is read through the existing local output path; pane placement is
optional display only and does not create lineage.
An unbooked manual `/clear` remains an unconfirmed chronological neighbor and
does not show the predecessor notice.


## Workflow Evidence

- Before capability grounding/spec-changing work: `adapters/codex/bin/preflight.sh route session-tidy [cwd] [session-id]`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
