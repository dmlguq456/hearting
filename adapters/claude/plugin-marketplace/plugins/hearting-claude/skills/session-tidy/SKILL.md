---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: session-tidy
description: "Use when context fills, before compact, on handoff to another session, after a large task, or on request. Not for primary routing or memory writes."
argument-hint: "[정리] | 정리만 | 인계 <받을 세션>"
metadata:
  group: ops
  fam: ops
  invocation_class: model-support
  modes: []
  blurb: "Write a handoff card; tidy memory; clear the window."
  use_when: "Use when context fills, before compact, on handoff to another session, after a large task, or on request."
  not_for: "Not for primary routing or memory writes."
---

# session-tidy (정리)

Write the handoff card yourself, queue the memory tidy, and let the window clear itself
when it is safe; the new session then picks the work up from the card by itself. Nothing
here is required input and nothing waits for a confirmation.

Modes: `정리` (default), `정리만` (same, but keep this window) and `인계 <받을 세션>` (same
work, then pass the card to a peer session; only this calling window is cleared, never the
target). The tool is `python3 $AGENT_HOME/utilities/session_tidy.py`.

For a new successor at this seat, replace the clear sequence below with `peer-steward.py start <name> --kind <harness>` (beside this pane by default) → `handoff` → `enqueue --no-clear`; after its ACK and the predecessor's final idle result, the successor uses `peer-steward.py retire <predecessor>` (§5.14).

## Steps

1. **Card.** Write six fields yourself from the conversation you already hold, in
   the user's language, and pipe them in (about 90 lines at most). Keep these four
   continuation fields first: what is in progress, the decisions still waiting,
   what to do next, and the related
   paths, PRs and sessions (Fleet tag·pane, role in parentheses; herdr name·pane
   when untagged). Then add a few lines for today's work, pointing to its PR,
   installed version or result file, and short quotations of user instructions
   marked finished, remaining or deferred. Point to artifacts rather than copying
   their reports. The start preview still shows the bounded beginning; the rest
   is in the card file. The memory worker's `일부만 읽음(범위)` describes its own
   input, not the card you wrote.

   ```bash
   python3 "$AGENT_HOME/utilities/session_tidy.py" card <<'CARD'
   진행 중: …
   기다리는 결정: …
   다음 할 일: …
   관련: …
   오늘 한 일: … (PR·설치 버전·결과 파일)
   사용자 지시: "…" — 끝남 / 남음 / 보류
   CARD
   ```

2. **Queue the tidy.** Do not wait for it. For `정리만` add `--no-clear`; when the user
   asked to stop after tidying ("정리하고 끝"), add `--no-continue`.

   ```bash
   python3 "$AGENT_HOME/utilities/session_tidy.py" enqueue               # 정리 / 인계
   python3 "$AGENT_HOME/utilities/session_tidy.py" enqueue --no-clear    # 정리만
   python3 "$AGENT_HOME/utilities/session_tidy.py" enqueue --no-continue # 정리하고 끝
   ```

   It prints one line and returns. A detached runner reads the conversation after the
   last tidy (plus recent untidied sessions of this seat), starts one registered memory
   worker, applies its proposal through `mem tidy-apply`, and leaves one result line for
   the next session of this seat — with the command that undoes it. A long conversation is
   read from its recent end first; the line says `일부만 읽음(범위)` when an older part is
   still waiting for the next tidy. A failure leaves the
   card as it was and says so in one line; if some writes had landed or may have landed, that
   line counts them and keeps the undo command.

   Inside herdr the same call books the window's clear (`clear=scheduled`): once this turn
   has ended and the window is idle, with no form open and an empty input box, the
   harness's own new-conversation command is typed once (Claude `/clear`, Codex `/clear`,
   OpenCode `/new`) and the new session receives the card. Once the clear is confirmed and
   the new session waits with an empty box and no message yet, the same helper types
   `이어서해` once, so it carries on from the card. Neither step waits for the memory tidy.
   A prompt typed after the card cancels the clear (`clear=skipped`); a prompt typed into the
   new session first, a new card or a handoff cancels the `이어서해` quietly. Anything else
   the helper cannot decide leaves the window as it is plus one result line at the next prompt.

3. **Handoff only.** For `인계 <받을 세션>` to an already-running target, after step 2:

   ```bash
   python3 "$AGENT_HOME/utilities/session_tidy.py" handoff <target>
   ```

   Report its one line as printed (`prompted=true|failed|queued|unverified` and the exit
   code); do not retry or send the card any other way. A failed handoff does not change the
   clear of this window; they are separate results. The work now belongs to the target, so
   this window is cleared but not continued.

4. **Report one line**, from the `clear=` word of step 2 (do not start more work after a
   `clear=scheduled`):
   `clear=scheduled` → "곧 이 창이 자동으로 비워지고, 새 세션이 카드를 받아 이어서 진행합니다.";
   `clear=scheduled continue=off`, or `clear=scheduled` with a handoff → "곧 이 창이 자동으로 비워집니다.";
   `clear=off` → "이 창은 그대로 둡니다. 이제 /clear 하거나 닫아도 됩니다.";
   `clear=manual hint=<cmd>` or `clear=skipped` → "이제 <cmd> 하거나 닫아도 됩니다."
   (`<cmd>` is the hint, or `/clear` after `skipped`; OpenCode's is `/new`).

A route that was still running when the window was cleared is taken over by the new session at
the same pane: its card carries one line with the verified route and the usual resume command
(`capability-route.py start --route <file> --jobs <registry>`), the new session may resume it,
and it receives the completion. The registered launch identity does not change.

The next session at this seat receives the latest card once, at start or on its first
prompt, together with any pending result line and a short "참고할 기억" list (ids and titles
only, read with `mem show <id>` when needed). If the tidy ends after that session started, the
list arrives once at its next prompt.
