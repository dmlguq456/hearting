---
name: session-tidy
description: "Use when context fills, before compact, on handoff to another session, after a large task, or on request. Not for primary routing or memory writes."
metadata:
  portable_source: capabilities/session-tidy.md
  adapter: opencode
  invocation_class: model-support
---

# session-tidy

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/session-tidy.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info session-tidy`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/session-tidy.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info session-tidy`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `session-tidy`
- Invocation class: `model-support`
- Supported modes: `none`
- Argument shape: `[정리] | 정리만 | 인계 <받을 세션>`
- Portable meaning: Write a handoff card; tidy memory; clear the window.

## Portable Contract

- Invocation semantics: The main-authored card keeps work in progress, waiting decisions, the next step and related references first. Below those four fields, add '오늘 한 일' (today's work, with PR/version/result pointers) and '사용자 지시' (brief user quotations marked finished, remaining or deferred), within about 90 lines. Write it from the conversation the main session already holds. The calling main session writes its own handoff card (`utilities/session_tidy.py card`), then `enqueue` returns at once while a detached runner (`utilities/session_tidy_runner.py`) starts one registered memory worker (`ops/session-tidy-memory`), applies its closed `add`/`supersede`/`reinforce` proposal through `mem tidy-apply`, and leaves one result line (with the undo command) for the seat's next session. The runner reads the conversation tail first: the newest unread part of each record goes to the worker, only a successful apply marks that part as read, and an older unread front stays pending for the next tidy; a record that was only partly read says so (`일부만 읽음(범위)`) and is never reported as 'nothing new'. The same run also refreshes the card's '참고할 기억' list (record ids and titles from what the batch really wrote plus the related existing records, at most 8 and 1,200 bytes), which the next session at the seat receives once with the card or, if the tidy ends later, at its next prompt. `handoff <target>` delivers the card to a peer session through `utilities/peer-steward.py prompt` only and reports its typed verdict. There is no required input and no confirmation step; a failure keeps the card and the watermarks unchanged and leaves one warning line, which counts any writes that had landed or may have landed and then carries the undo command. Clearing and continuing are part of the default flow. Inside herdr, `enqueue` also books the calling window's clear (`utilities/session_tidy_clear.py`, a detached helper that never waits for the memory tidy): once the turn has ended and the window is idle, `utilities/peer-steward.py clear` types the harness's own new-conversation command once (Claude `/clear`, Codex `/clear`, OpenCode `/new`), only when no prompt was submitted after the card, no form is open and the input box reads as empty. A clear counts as confirmed only with evidence the conversation changed (the new session's start hook, its session id from the pane's process or herdr, a new thread id on Codex's status line — hearting's Codex status line shows `thread-title`, the thread id until the thread has a title — or OpenCode's home screen). After a confirmed clear the same helper asks `utilities/peer-steward.py continue` once, which types the continue prompt `이어서해` (no trailer) into the new session only when it is the session the clear started and has had no message yet (Claude: the pane's process names it and its start hook took the card; Codex: its status line shows it and no rollout exists for it, so this prompt is its first message and its start hook delivers the card; OpenCode: its home screen), it is idle, no form is open, the input box is empty, no prompt was submitted since the card and the card is still the booked one and was not handed to a peer. It claims the booking (`pending` → `sending`) right before the one send and never re-sends or retries an Enter. Anything either step cannot decide leaves the window alone and one result line (`cleared=…`, `continued=true|skipped|failed|unverified`; a success is silent, and a continue skipped because the person typed first, left a draft or wrote a new card is silent too). `정리만` (`enqueue --no-clear`) keeps the window and cancels a pending booking; `enqueue --no-continue` (the user asked to stop after tidying) clears without the continue prompt. Outside herdr the session prints the harness's manual command instead (Claude `/clear`, Codex `/clear`, OpenCode `/new`). A handoff clears only the calling window, never the target, its failure is separate from the clear, and it marks the card as handed off, so the calling window is not continued from it. Running work follows the window. `enqueue` also snapshots the registered depth-1 owner/frame attempts the tidying session answers for (the runtime reads them from the dispatch registry; a card's text grants nothing). When the next session at the same herdr pane starts through a confirmed clear (a `clear` start source or the clear booking's own observation; for OpenCode, its first message in a new session), `session_tidy.py hook` appends one handover row (old session → new session, with the bindings) to the seat ledger. From then on the new session may resume those attempts (`work_start`, replacement authority, the Claude wake re-arm, the interactive frame handback) and receives their pending completions (Claude/Codex prompt sweeps, the Codex queue target), while the registry row keeps its registered `parent_sid`, receipt, digest and gate identity and the pending records stay stored, claimed and acked under that registered parent. One direction (old → new), the same pane and harness only, never old → two sessions at once; a further clear hands over only through the new session's own next tidy. The card carries the verified route and the existing resume command in one line. Without a handover every call behaves as before. OpenCode direct owners still complete through the poll fallback: the new session can resume them, but there is no turn carrier for their completion. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.



## Projected Portable Details

## Artifact Ownership

Artifact root: `core/CONVENTIONS.md §5.1`; output placement: `§5`.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- state lives under `${XDG_STATE_HOME:-~/.local/state}/hearting/session-tidy/` (directories 0700, files 0600), never in the artifact root. The one exception is the memory worker's exchange folder `<artifact root>/.runtime/session-tidy/<batch>/` (0700; the input copy, the prompt and the one `actions.json` it writes): its sandbox allows the artifact root the checked wrapper launches it with and not the state folder. The runner reads that file back without following a link, checks it, copies the checked result into state and deletes the folder (a failed batch keeps only the worker's answer until it is pruned); sandboxes and permissions are never widened for it;
- pane input goes only through `utilities/peer-steward.py` (the clear is its `clear` command and the continue prompt its `continue` command, each typed at most once and never retried);
- the memory worker never calls a memory write command; only `mem tidy-apply` writes, and it never deletes.

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

Example fields (use the user's language):

```text
진행 중: …
기다리는 결정: …
다음 할 일: …
관련: …
오늘 한 일: … (PR·설치 버전·결과 파일)
사용자 지시: "…" — 끝남 / 남음 / 보류
```

Continue with the existing enqueue and handoff flow.


## Workflow Evidence

- For workflow state: `adapters/opencode/bin/preflight.sh status [cwd] [session-id]` and `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
