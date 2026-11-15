# The session panel and Fleet, in detail

The [README](../README.md#one-seat-for-every-session) shows what the panel and
Fleet are for. This page collects the keys, layouts, settings, and per-runtime
details behind them. The canonical contract is
[core/references/session-shell.md](../core/references/session-shell.md); peer
requests are specified in [core/OPERATIONS.md](../core/OPERATIONS.md)
(“Peer requests”).

- [Turning it on and off](#turning-it-on-and-off)
- [What the panel shows](#what-the-panel-shows)
- [Keys and mouse](#keys-and-mouse)
- [Narrow windows and folding](#narrow-windows-and-folding)
- [Settings](#settings)
- [Requests and messages](#requests-and-messages)
- [The LAB section](#the-lab-section)
- [Per runtime](#per-runtime)
- [Fleet details](#fleet-details)

## Turning it on and off

New installations enable a common status area and a Hearting work panel for
new Claude Code, Codex, and OpenCode terminals. Updates preserve existing
choices, including older installations without a shell policy.

```bash
hearting session shell status    # the current choice
hearting session shell disable   # new terminals launch the native CLI as before
hearting session shell enable    # turn it on again
```

Existing sessions keep running either way; installation affects new wrapped
terminals, and running hosts are not replaced. Noninteractive commands and an
unavailable shell runtime run the original CLI without starting a second
native session. Herdr, cmux, and tmux remain optional.

## What the panel shows

The default is a 40-column panel on the right of the native area. Its top
border names the session the way Fleet's row does: the state glyph, the
session number (`[4f]`), the runtime, and `⚑` for a steward. The frame shows
that state the moment the native screen changes: while the session runs it
stands lit and a light passes over the rule under the model and context, while
an answer waits the whole frame breathes red, and idle stays neutral (another
motion, or none, under [Settings](#settings)). From the top:

| Rows | What they say |
|---|---|
| `REPO`, `GIT` | The repository and its linked-worktree count (`web-api · wt 1`); the branch, then `✓` when it is level with its upstream or `↑N`/`↓N`, then any merge or rebase warning |
| `MODEL`, `CTX` | The model and effort, and the context gauge and percent |
| `TITLE`, `NOW` | Fleet's title for the session and what it is doing now, under a thin rule |
| `N QUESTIONS` | The questions waiting for you: an answer on its way (`✓ answer sent · Public only`, the first words green), then the next question. The one you can answer here reads as a button on a rose ground: its first line, its choices as numbered chips, and `↵ answer · Ctrl+]` (a rose bar at the left in mono, eight colors, or `NO_COLOR`); nothing else in the panel is filled |
| `N ROUTE ⠏2 ✕1` | Three routes, failed first, then running, waiting, finished: the mark, the title, where it stands (`2/4 execute ●`), and its age (or the doer's model, with `route line` set to `model`). A finished route stays with its result (`✓`, or `✕` and `FAIL · title`) until your next input or ten minutes |
| `N LAB` | This project's experiment board; see [The LAB section](#the-lab-section) |
| `N SESSIONS ◑1 ⠏3` | Three other sessions on this machine from Fleet's session table, the ones waiting for you first, then the running: mark, number, title, age |
| `MESSAGES +N` | Requests this session sent (`→`) or received (`◂`), then recent messages, one row each, three at most; see [Requests and messages](#requests-and-messages) |
| `MEM` | The two newest saved memory records of this repository and global ones: `◆` durable or `◇` working, its scope (`project`, `global`) and kind, its headline, its age |
| Last row | The usage windows (`5h 18% │ 7d 40%`), the fold button `»`, and the gear `⚙` |

Each section head reads `N NAME counts ─── ›`; a 40-row window keeps two or
three rows per section and a 20-row one one each, and lower windows let MEM go
first. MESSAGES and MEM keep to the bottom. A press on a section's head or rows
opens its popup (below). A failed route or experiment shows only in its own
row. Values the host could not refresh for a while are shown, not claimed:
nothing spins, and the frame takes no route state. A value that cannot be read
stays `?`.

## Keys and mouse

Ordinary keys stay with the native CLI. A section opens in a popup in the
middle of the window (the native area and the panel together), never by
itself: the question editor for QUESTIONS, Fleet's own table for ROUTE, LAB,
and SESSIONS, and the requests and messages whole for MESSAGES. While it
stands every key and press is its own, everything behind it reads in one gray,
and **Esc**, **Ctrl+]**, its `✕`, or a press outside it closes it.

| Key | What it does |
|---|---|
| **Ctrl+]** | Open a popup: the question you can answer here, else the first section with something in it; again (or **Esc**) closes it |
| **←/→** | Move to the next or previous section (its name on the popup's bottom line) |
| **Tab**, **1–9**, **t** | In a question: the next waiting question, an option (**↑/↓** too; **PgUp/PgDn** read a long question), an answer in text, including pasted text |
| **Enter** | In a question: send, once every form field is answered; the popup closes and keys go back to the native CLI. In ROUTE: the unfolded route's page, and again back to the table. In SESSIONS: answer the question of a session waiting for you (`◑`) |
| **↑/↓**, **Tab** | In ROUTE or SESSIONS: unfold the next route or session; elsewhere read on (**PgUp/PgDn** a page) |
| **p** | Fold the panel into the strip, or open a folded one (the popup closes) |
| **f** | Open Fleet in the same window; **Ctrl+]** closes it and returns |
| **r**, **w** | The MESSAGES popup; this repository's open work |
| Click | A section's head or rows open its popup; a name on the popup's bottom line moves there; the wheel reads a long table on |

Choices and drafts survive moving between questions. Once an answer is sent,
the panel keeps one line, `✓ answer sent · <choice>`, until the session has
it. A transport receipt is
shown separately from proved consumption, and expired or cancelled questions
cannot be submitted. A question that has to be answered somewhere else says
where instead of offering choices: `answer below` (in the native input),
`answer in its window`, or `answer in the parent`. Only **f** opens Fleet. In
Fleet, **Ctrl+]** on a session page opens that exact session's panel.

<p align="center">
  <img src="fleet/panel-question.png" alt="The bottom layout in a 100-column window: the panel under the native area. Its head shows session [9c], Sonnet 5.5, context 33%, the title Release notes 2.4 and NOW Choose a layout; under it 2 QUESTIONS, the first question on a rose ground with its three choices as numbered chips, the first recommended, and answer, Ctrl+] at the right; then messages and usage." width="90%">
</p>
<p align="center"><sub>The bottom layout: the question to answer here as a rose button, which <b>Ctrl+]</b> or a press opens in the popup. The conversation is illustrative; demo data.</sub></p>

## Narrow windows and folding

**»** beside the gear (**▾** when the panel is under the native area) folds it
into a strip under the native area, which gets the whole width back: a thin
rule with **▴**, then three rows: the session (state, number, project, model,
context, 5h and 7d), what to do now (an answer you owe with the question's
first line, compaction about to happen), and the sections (the routes with the
failed or running titles, an experiment board that needs a look, the other
sessions, new messages). Each row drops whole items from its end when narrow. The strip keeps its
height whatever it shows (a short window gets fewer rows: three from 24 rows,
two from 16, else one, with what to do kept longest). A press on the rule or
the session row opens the panel again (from the keyboard: **Ctrl+]**, then
**p**); a press on the row of what to do opens the question, and a press on a
group of the last row opens that section, in a popup above the strip. Where the open panel would leave the
native area under 60 columns (a 40-column panel in a terminal under 102
columns) it folds by itself and opens again with room; a choice made by hand
holds at every width. In the bottom layout (when `layout` is set to bottom, or
when you open the panel by hand in a window too narrow to hold it beside the
native area, until the window widens) the panel sits under the native area.

## Settings

**⚙** (the gear at the right end of the panel's last row) previews placement,
panel position (right or left), width, narrow-window behavior, bottom height,
auxiliary detail, route line, rule motion (`rule`, the default: the lit frame
and the light over the rule; `border`: one arc round the frame; `scanners`: a
wide wave round it; `off`), theme (terminal, light terminal, or monochrome),
and shade (none, light, or medium). Use the value arrows or keyboard arrows, then Apply; Cancel
restores the previous view, and Defaults previews the defaults. Drag the side
boundary to resize and save it. Only Hearting's own `session-shell.json` is
saved, with a byte-for-byte backup; native model and permission settings are
untouched. Existing explicit layout choices are retained.

## Requests and messages

A **request** is work handed from one session to another, with a number,
revisions, and reports. You ask in your own words (“Ask session [b2] to re-run
the tests and report back”); the agent runs the commands:

```bash
hearting run peer-steward request <session> --body-file <file>   # new request; --rev <label> supersedes, <rq-id> --cancel withdraws
hearting run peer-steward report [<rq-id>] --partial --result "<line>"   # or --reject "<why>", or a final report without --partial
hearting run peer-steward show [<rq-id>]                         # the sealed body; no id: this session's open requests
```

The line that reaches the other conversation names the request; the body
stays sealed, and the receiving model opens it with `show`. Delivery and work
are separate: a receipt does not change a request's state; only a final or
reject report, or the requester's cancel, closes it, and a partial report keeps
it open. A report is not approval, release, or a user answer. A request
follows the other session to a successor only when Hearting's lineage confirms
the handover.

In the panel, every open request has a row in MESSAGES, before the messages:
`→ [b2] Test run · partial answer`. The MESSAGES popup shows it whole: the
parties and number, `REV`, `DELIVERY` (`sent`, `delivered`, `seen`), `WORK`
(`open`, `partial answer`, `final answer`, `rejected`, `cancelled`, with what is
left), `LAST`, `REPORTS`, and the body. A closed request this session sent
stays 15 minutes after it closed, or until its last answer has been seen.

<p align="center">
  <img src="fleet/requests.png" alt="Two panels: under MESSAGES, the session that asked shows an arrow to [b2], Test run, partial answer, and a finished review; the session that was asked shows an arrow from [4f], Test run, partial answer." width="80%">
</p>

**Messages** are short notes, sent with
`hearting run peer-steward prompt <session> --body-file <file>`; reply text
addressed to another session in a conversation reaches nobody. **Watching**
(`hearting run peer-steward watch <session>`) wakes the watcher once when the
watched session finishes, stops, or needs a person. None of these needs herdr
or tmux.

How an idle session is reached differs by runtime
([core/ADAPTATION.md](../core/ADAPTATION.md), PG-58 and PG-59):

- In a terminal with the panel, the shell injects a waiting message, request
  line, or report into an idle Claude Code, Codex, or OpenCode session.
- Codex takes them through its own thread queue, with or without the panel.
- Outside the panel's terminal, Claude Code takes them at its next callback
  (an idle session at your next prompt), and OpenCode at its next idle or tool
  callback. Neither is woken at once; the request stays open and listed on
  both sides.
- A watch wakes Claude Code everywhere it was measured. Codex is woken where
  Hearting has proven its terminal; otherwise, and on OpenCode, `watch` prints
  a command to wait on (`bounded-wait` with a `join`) instead of promising a
  wake.

Fleet shows a request only as a PEERS link (sent, reported); it has no request
list yet, and you cannot send a request or message from Fleet yet.

## The LAB section

When the project has an experiment board, the LAB section stands under ROUTE:
`3 LAB` (the selected group's condition count) with its most pressing counts at
the right (`failed 1`, `stuck 1`, `unknown 1`, `waiting 1`, else `running 3`),
then three conditions, the one to look at first, each with its state, progress,
and difference from the baseline as the width allows. A press opens Fleet's
`LAB` table in a popup, the same table as on the project page, from the same
board. No experiments and no
problem means no section; a broken catalog or an unnamed run is an unconfirmed
warning, never “no experiments”. An experiment's failure never colors the
frame. The board's files join the panel's file watch, so a new record redraws
it without polling.

## Per runtime

- **Claude Code.** The panel shows the same status area as Codex and OpenCode,
  and Claude's own statusline is one row under the input. While the panel, or
  its folded strip, shows the status area, that row is a notice row that stays
  empty unless you must act now: an answer you owe, compaction about to
  happen, a red context or usage reading, or a failed or blocked route. News
  stays in the panel: messages in `MESSAGES`, a finished route's result in
  `ROUTE`. With the panel hidden the row shows the status line itself.
- **Codex.** A wrapped Codex keeps its native shared-daemon startup. The shell
  hides the footer's thread ID (it stays on the inner screen for
  identification; a Codex window without the shell still shows it), and the
  rest of that row can yield duplicate paint to the common area. Custom,
  clipped, or uncertain rows remain visible. Codex context keeps the latest
  server-reported usage; a separate `compaction imminent` label appears only
  when the exact session's current native diagnostic supports it, and
  unavailable or stale diagnostic data stays unknown. The 500K/700K
  preservation notices do not guarantee remaining time before native
  compaction.
- **OpenCode.** It uses the same panel and settings. Its own sidebar is hidden
  in the shell window (OpenCode's toggle still opens it), with Context in its
  footer and its MCP server count in the panel.

Where independent telemetry cannot provide a value, the native context display
remains as a fallback. Answers keep the existing queue and consumption
receipts; an unproved native target stays pending.

## Fleet details

- **NOW and the expanded row.** NOW is what the session is doing now (`·`
  when nothing is observed). Expand a row in OVERVIEW for LAST INPUT and
  RECEIVED: `› 1m · …` is your last confirmed input, `› ?` means the text
  cannot be confirmed (including older inputs whose first line was not kept,
  or sessions with no readable input record), and `› ·` means no human input
  was recorded. Received peer and runtime messages show their first lines
  without notification wrappers.
- **The project page.** Select a project in SESSIONS for its campaign status
  (up to three campaign summaries), backlog and work in progress, open
  requests and stages, completed results, recent small work records, its
  `LAB` table, and its sessions. Open work uses the same records as MEMORY;
  saving a memory does not count as completing a task.
- **Pages and raw output.** Select a session or a request to open its page,
  and from there its raw output: tool calls and results in order, without
  opening the runtime itself.
- **Commands.** `fleet --once` and `fleet --json` need no `curses`, so they
  also work on Windows under Git Bash; `fleet-monitor` is the earlier
  group/process view. Session titles follow your language; set
  `title_language` in `${XDG_CONFIG_HOME:-~/.config}/hearting/fleet.json` to
  override it.
- **First screen.** A newly started window draws its first screen as soon as
  the session list is read and marks the cells still loading `details wait`;
  the rest fills in as it is read.
