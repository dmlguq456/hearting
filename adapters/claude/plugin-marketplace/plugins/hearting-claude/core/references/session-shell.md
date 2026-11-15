# Optional session shell

**Optional session shell.** Hearting may wrap an interactive native terminal
with a shared status line and an expanded session block. New global installations enable
this feature by default for Claude, Codex and OpenCode. Updates preserve existing
choices, including an absent policy on older installations. Use `hearting session
shell disable` to restore native launch behavior, or `hearting session shell
enable` to enable it again. Noninteractive commands
and an unavailable shell runtime execute the original CLI without starting a
second native session. Herdr, cmux and tmux remain optional attachments.

A wrapped pane is its native session's seat. The one attachment whose PTY owner
(with its exact birth) leads the pane's foreground names the native process and
session; retire, session tidy and agent discovery use that join once instead of
re-proving it per step. Draft, form and footer judgments read the native screen
the host parses, not the framed pane, and the host's lifecycle state stands in
where a runtime keeps no turn record of its own. The seat is exiting, not gone,
until its PTY owner leaves the pane. When OpenCode returns to its home route the
seat stays published as session `-`, which no session lookup can match.
Protections stay: an empty composer and idle turn before any input, Codex input
only to the thread the TUI currently selects, Claude's own record naming the
attachment's session, and no seat for a service or registered worker.

The delivery address is the exact `(harness, native session ID)`, never a pane
or a display tag. A Fleet tag selects that identity once; subsequent tag or
attachment changes cannot retarget a retained message. The shell publishes its
current native selection and process birth through the existing session record.
An idle wake requires the same live identity, an empty composer, no open form,
and an unchanged input generation. Human drafts and concurrent typing survive
the operation. A judged key typed into a shell seat's pane (a tidy `/compact`,
a continue) goes in only while the host's screen answer says the outer keys
reach native; with settings, the Fleet explorer or the question popup open,
or no answer, there is no screen to type into. Only a raw relay reads the pane
itself. Reading that screen and typing into it ask the same `focused` answer;
reading a trust reason only reads it and its ingress, and never picks a trust
answer. A window started on v4.18.5–v4.18.8 runs a host whose `focused` stays
false with the Fleet explorer open, so there a tidy or continue key can go in
while the explorer is open; restarting the window brings a host that refuses
it. A host without the `screen` answer gets no typed key, the same skip, card
kept, as before. A refused tidy is not retried by a new wait: a manual one
ends `skipped` with its card kept; an automatic one is judged again only while
its card request is still pending, and a refusal after the card was accepted
ends `unverified`/`compact-pending` for that work span (the next idle is
`not-rearmed`, the card stays). Keyboard bytes remain ordered behind the admitted paste and its
Enter, and are released once Enter is written even if a lifecycle callback is
lost. That transport boundary never acknowledges peer consumption.
The launcher retains the PTY master and original native process outside the
display worker. VT, scratch-display, layout and display-host failures degrade
to a plain relay of that same process; they never restart the CLI or request
manual recovery. Normal key bytes still go directly from the display worker
to the same PTY, without an extra broker hop.
A running window follows a new install without restarting its CLI. Each
display host compares the installed link it was started from
(`HEARTING_SHELL_HOST_PATH`, never resolved) with its own resolved file; it
notices a change from a watch on the link's directory and at each idle
transition, with no timer; a successor that took over keeps the same watch. A look that
ends without asking is not lost: a reason that comes during a look is taken up when it ends, and
the second's pause after an attempt that ended unsafe (or a reading the backend did not answer)
ends in one more look. While a newer host is installed and not yet taken,
the tidy-notice slot shows one lowest-priority line (`session_shell.upgrade_pending`),
never over another notice; beside the native area the panel's thin rule that parts the
session's content from the head goes with it, and nothing else on the screen changes.
A popup stands over the window: the panel under it keeps its notice slot.
The host asks the PTY owner for a successor only at a safe moment: idle, an
empty composer, no open form or answerable question, no popup, settings,
Fleet explorer or panel focus, and no delivery, held bytes, paste, drag or
action worker. Keys that reached native since the last submitted turn leave the
composer unproven (an erased draft, a form answered with Enter, history-scroll
keys, a turn stopped without a stop callback), so the switch then follows the
next submitted turn. A start or selection (a resume picker's Enter, `/clear`,
OpenCode's plugin reload) counts the bytes up to the last Enter as submitted, as
delivery readiness does; keys after that Enter still wait for the next submitted turn.
A window with no selected session (OpenCode on its home route) has
no such proof and waits for one; OpenCode's plugin reload (deselect, then select of the
same session, no keys between) keeps an open popup. The owner starts the successor from the same installed path. The old host stops
reading, withdraws its binding and writes a private seed: the native screen
with every line above the viewport, cursor, modes, scroll region and display
state. The successor restores it, takes the same `control.sock` path by an
atomic rename, registers, paints only the differences, and reads the PTY and
keyboard only after the owner commits the switch. The native process, its
conversation, the draft and pending deliveries stay; success shows nothing. A
failure or timeout before the commit resumes the old host as it was (its
display is painted whole once, and the successor's stderr never reaches the
terminal), and keys
typed meanwhile wait in the terminal buffer for whichever host reads next. An old
host that loses its own display while frozen falls to the plain relay and ends its
side of the hand-over channel, so the owner drops the successor before it reads;
a `retire` that was already sent still ends that host. The PTY and the keys never
have two readers.
After three failures toward one installed file the window stops asking and
keeps the waiting line. When the seed cannot carry the lines above the viewport
(over 8 MiB, or their serialization fails), the switch still happens with the
visible screen and shows `session_shell.upgrade_scrollback` until the next
submitted prompt; an unreadable scroll region keeps the old host. What a person
scrolls back to is native's own history view (same process) or the outer
terminal's history from before the shell entered the alternate screen, which
neither host leaves. A raw relay window is not revived. The protocol version is
`HEARTING_SHELL_SUCCESSION`; the PTY owner never changes during a window's life,
so a later host speaks the version of the owner that started it or does not
succeed. A window started before this protocol (v4.20.0 and earlier) is reopened
once; later installs need no reopen. The host sees the install through the link
rather than a signal from the installer, so no install path has to find live
windows; where the link's directory cannot be watched, only the idle check
remains.
A session reopened under the same native session ID (Claude `--resume`, Codex
`resume`, OpenCode `-s`) takes back the routes it left running. The reopening itself
(the SessionStart hook with source `resume`, or OpenCode's selected-TUI resume report;
never the steward's periodic resume pass) starts, once per resume, a detached
`utilities/session_route_resume.py` that runs `capability-route.py start --route`
for every route this session's route-chain ledger names whose file exists, has no
outcome, and has work this session launched still open in the dispatch registry:
the latest owner attempt is `open`/`running` (a route without an owner: any of its
attempts). Oldest first, this is the start a person used to run by hand, so a live
owner is reused, a dead one is replaced, and the existing gates still refuse. A
route that was only composed, or whose owner ended (`done`, `killed`, `cancelled`),
waits for a person's own `start`. Each result is kept under the
session state's `route-resume/`. Only the last started route's receipt reaches
the next prompt; the others show in the panel and Fleet. Resume aliases are a
display join and are not read. An automatic display switch keeps the native
process and needs none of this.
During an admitted paste, its delayed Enter and held human bytes belong to the
retained PTY owner. The display publishes a held count only after those bytes
are retained outside its own memory. Input returns to the direct path after
the owner drains that transaction; host death cannot discard or replay it.
Unknown readiness retains the existing pending delivery. Select
one verified transport for a message; an uncertain submission never triggers
an alternate send. Codex keeps its native queue and exact receive evidence.
Native start, prompt and turn-completion callbacks update display identity and
state for Claude and Codex alike; they never replace the Codex queue with input.
The native callback supplies its session ID before a passive registry may exist.
Its nearest verified native process and birth must still belong to this wrapped
terminal; a worker or service callback cannot select another terminal's session.
For a shared Codex daemon callback, reuse the native MCP origin's live TUI
PID/thread proof and its current unique selection. Read the attachment only
from that TUI's own initial environment; never inherit the daemon's first
client socket. Its PID/birth and exact wrapped PTY checks still apply. After
`/resume`, retained roots are membership, not selection. The existing strict
native footer judgment may select one of those roots from two unchanged raw
PTY viewport observations, joined to that same live TUI's fresh native origin.
The host frame and that judgment accept the same footer forms, including
Codex 0.163's status line with no help row. The frame keeps every whole or
clipped thread-id of an accepted row in place and drops nothing, so whether ids
repeat or conflict is decided once, by that judgment. Two conditions now admit
a root that was refused before, both only when the exact process-native proof
names that same thread: a repaint between the two reads (same rows and input
epoch, only the revision advanced), and a membership lookup that returns
nothing. An empty lookup does not tell a TUI without a daemon from a failed
observation, so it is never the ground; the independent native proof is.
This reuses the existing attachment and selection event; projected status text,
model/context telemetry and an arbitrary screen UUID never supply authority.
Unknown, conflicting, worker, remote or changed evidence grants no attachment or input.
Preserve native shared startup: do not insert a footer configuration override
that silently selects embedded mode. Existing explicit native modes keep
their own behavior and the existing pending queue semantics.

Startup uses the running display host's Node ABI to validate the existing
prepared assets, without an auxiliary Node version probe. Native execution
retains the existing display-ready boundary, exact binding and same-child
input/relay ownership.

The approved v3.3 shell opens as a right Hearting work panel (left by choice,
or under the native area by choice). Spec E9 rule 49 folds and unfolds it: the
fold button `»` (`«` for a left panel, `▾` under the native area, the bottom
layout or a side panel opened where it cannot stand beside) on the panel's last
row inside folds it. Folded there is one form, by itself or by hand, beside or
under: a strip under the native area, which keeps the whole width (the native
is told its new size through the same PTY resize a window resize uses). The
strip is a thin rule in the messages' line color with `▴` at its right end,
then three rows, each the most pressing first and giving way whole item by
whole item from its end: the session (the state glyph and number, the project,
model and effort, `CTX` and its gauge, 5h, 7d; short of cells the reset times
go first, then the effort, the project, 5h, the model windows, 7d), what to do
now (`◑답 대기 N · ` and the question's first line, cut with `…` before the wait
goes, then its choices where cells are left, whole or else the first one with its name
cut by `…` and the rest counted (`· 1 Name… +3`), never the count alone;
compaction imminent; a tidy notice), and the sections' groups, each its count, its dark gray
name tag and its most pressing count (`3 ROUTE ⠏2 ✕1 · ✕ title │ 4 LAB 실패 1 │ 5 SESSIONS ◑2 │
MESSAGES ←2`): the routes with the failed routes' titles (else the running ones') after them, the
experiment board only while it needs a look, the sessions on this machine once their table was
read, and the kept messages received since the person's last input (`←N`); short of cells the
titles give way first, then the groups from the last. The session row carries the Git branch
after the project where cells are left (` (branch)`, twelve cells at most, the first value to
go). Its height follows the window's
rows alone, never what it shows, so a question coming or going never moves the
native area: three rows from 24 window rows (the native keeps 20 or more), two
from 16, else one; short of rows the routes row goes first, then the session
row, so what to do stays to the last, and a row with nothing on it gives its
place to the next. Labels are dark gray upper case; there is no frame, gear,
beat or moving rule, and only the state glyph turns. While the strip paints its
session row, Claude's row under its input keeps the notice (what to do now) as
with the panel open, so no value shows twice; the strip carries no Git or job
row. A press on the rule or the session row opens the panel; a press on the row of what to do
while it carries the question opens the question popup (that row then stands on the question's
rose fill across the strip, or leads with the rose bar where no hue shows), and a press on a group of the routes row
opens that section's popup, above the strip (the strip stays and the native keeps its width).
**Ctrl+]** always opens a popup and every popup names **`p`**, which folds or opens the panel: the
keyboard alone unfolds a folded panel (Ctrl+], then `p`). Where the open panel
would leave the native area under 60 columns it folds by itself, and opens again
once there are three columns more (63), so a window dragged across the line does
not jump back and forth; that one constant replaces the earlier minimum terminal width and
bottom fallback. It judges only until the person chooses, and the person's last
choice holds at every width: folded by hand it stays folded however wide the
window grows, opened by hand it stays open however the window is resized. An
open settings editor or a drag
of the panel's edge keeps it open; when that ends, a width the person set (the
drag, or a new width applied in the settings editor) counts as opening it by
hand, and otherwise (the editor closed) the width is judged once again. The
popups keep nothing open: they stand over the window whether the panel
is open or folded. Folding by hand closes an open settings
editor first; a press made while the panel is being drawn for another placement
is dropped. The fold choice lasts while the window is up
(it rides a host hand-over) and is not saved. A panel opened by hand where it
cannot stand beside the native area (under 48 native columns or 8 rows) opens
under it, and stands beside again, still open, once the window is wide enough.
All four edges belong
to Hearting and share its representative state color; never frame the native
terminal itself. By default the panel uses the terminal's neutral background,
without tinted fills; a chosen theme or shade recolors only the panel itself.
Text and glyphs accompany state colors in every theme. Native permission UI remains native; idle alone means
neither work complete nor failed. Unknown evidence stays unknown.
The same facts and available panel dimensions use the same content order and
editing controls in all three harnesses, including startup and restoration.
The panel follows the 2026-11-11 unified layout (the C3 draft the person
chose), placed by the C6 rule: what is always there sits at the top, what
comes and goes below it; the sections stand from the top one after another, and
MESSAGES and MEM keep to the panel's bottom, right above the `5h`/`7d` row, so the
rows left over stand between the sections above them and MESSAGES (the person's own
word, 2026-11-11, restored 2026-11-15; without MESSAGES MEM keeps to the bottom, with
neither the rows left over are below the last section). The top border carries Fleet's row head for this
session on the left: the state glyph (a spinner while it works or an answer is
being typed in for the person, `●` idle, `◑` waiting on a dialog or question),
the session number (`[e9]`), the harness name in its Fleet hue and, for a
steward, Fleet's `⚑` (a steward's number takes Fleet's steward color, fixed,
as on Fleet's session row); the state has no word, its glyph and number share the
state's color, and the three glyph shapes still differ in `mono` and under
`NO_COLOR`. Short of cells the steward mark goes first, then the harness, then
the number; the glyph stays. Right of that head the border is the line alone:
the gear `⚙` stands at the right end of the panel's last row inside, three cells
right of the fold button, beside 5h/7d and the model windows (`5h … │ 7d … │
fable 70%`, the model word in its model color), and that row stays however
short rows run; short of cells it gives way whole value by whole value: the
reset times, then 5h, then the model values, then 7d. Claude's statusline
callback carries only its 5h and 7d windows: the same credential-bound account
cache's model windows stay beside them while that cache is fresh, never an older
reading shown as now (a callback that carries its own wins). That cache's existing
refresh is asked for two thirds into its fresh window, and the panel's own
projection wakes it once when due, so the value stays without Fleet; a refresh
that keeps failing still leaves the value out once the window ends. The state shown is the host's
own observation; hook events confirm and correct it, and input readiness and
every gate keep the hooks' lifecycle state. Claude's REPL writes its UI state
into its session record as it paints (`busy` while it shows progress, `waiting`
with a dialog or question open, `idle`); the host reads that record the moment
its watched file changes, only for this exact process and session. A `waiting`
is an answer wait only for the person's own reasons (`waitingFor`: `permission
prompt`, `input needed`, `worker request`, `sandbox request`, `goal proposal`);
a menu, settings or side dialog (`dialog open`) says nothing about the turn and
the previous reading stands, and an unlisted reason is left to the hooks. Codex
appends each turn's start and end to its rollout, read with the projection;
before any hook after a start or resume only its idle is taken (a last turn
start may be a turn cut off mid-way), and a turn seen starting after that reads
as running. After an approval the question screen leaving (the readiness
judgment's own form test) means the turn goes on; it is asked about for as long
as the hooks wait, and a question seen again turns the panel back to waiting.
OpenCode's plugin reports its native events, an answered or approved request
included (a rejected one resumes nothing); a turn stopped by the person (Esc) is
idle, a failed one keeps its wait. An Enter alone never claims a turn. Before any reading or event after
start, resume, `/clear` or a session switch the state is left blank. The whole
outer frame takes the state's color in the same paint: running and waiting
Fleet's session keys, idle and an unknown state the neutral frame gray (the
terminal's dim on an unknown face), no hue in `mono`; it stands still that way
with `rule motion` off, in `mono`, under `NO_COLOR`, in eight colors and on the
settings screen. Otherwise this session's own state (not a route's) moves the open panel, beside the
native area and under it alike (the folded strip has no frame: only its state glyph turns), as the
setting `rule motion` picks (11-15). While the session runs (an answer being typed in included):
`rule` (the default) keeps the outer frame still and lit one step below the running color (187;
`BORDER_LIT`) and passes a light over the thin head rule: its middle five cells brightest and four
on each side one shade down each, out past one end and back in 2.6 seconds, then the dark rule
rests one second and the light comes in again on the same side (a panel whose head rule gave way
for rows, and the bottom layout, which has none, move nothing but the glyph); `border` sends one
bright arc round the frame clockwise, its head the running color and its tail four shades down
into the neutral frame gray, a quarter of the frame long, once round every six seconds (the cells
of the head's words are passed behind); `scanners` lets a wide wave flow round the frame clockwise,
no edge anywhere: three waves share the frame (fewer where one would be narrower than 24 cells), a
place rises and falls once every 5.6 seconds, ten shades in 24-bit color (the four of the running
ramp in 256 colors; fewer on a narrow wave, so neighbouring cells differ by one shade at most), its
darkest never the still gray; `off` moves nothing. The motion starts with the state: on the very
paint that shows the session running the arc's head stands right after the head's words and the
rule's light is coming in. While an answer or a confirmation waits the whole frame breathes red
under every value but `off`, never blinking, once every 4.8 seconds on a cosine through six shades
in 24-bit color (when `COLORTERM` says the terminal takes it) or three in 256 colors, one shade at a
time, each for an equal stretch, starting at the frame's waiting red, and the head rule stays the
plain thin rule; the steward `⚑` keeps its own blink. Idle and an unknown state keep the still
neutral frame under every value. Every value is a constant in one place in `panel-view.cjs`; those
a person tunes stand at its top (the rule's light `RULE_PEAK` 5, `RULE_FADE` 4, `RULE_SCAN` 26
frames, `RULE_REST` 10 frames, the lit frame `BORDER_LIT` 1; the wave `WAVE_COUNT` 3, `WAVE_LAP` 56
frames, `WAVE_STEPS` 10, `WAVE_FLOOR` 0.3, `WAVE_MIN` 24; the arc `BORDER_ARC` 1/4, `BORDER_LAP` 60
frames), the breath and its shade count with the other `BORDER_*`, the colors in `FACES.*.orbit`,
`scan`, `waveRGB`, `breath`, `breathRGB`. A route's state stays in
the route box: a failed current route turns that box's line to the failure
color and a running one pulses `N ROUTE` (`route N`); the question's count and `◑` keep their rose. An experiment's failure never colors the frame.
`HEARTING` names the settings screen alone. What runs shares Fleet's one beat
(its 100 ms spinner frame and one-second blink): this session's spinner, a
working route's mark, the running stage, the open route count and a steward's
`⚑` (Fleet's steward pink, lit and receding; bold and plain without color), which
blinks even while the session is idle; the steward's number does not blink (plain
weight without color), and the moving frame. The host keeps that beat
only while the painted panel shows one of them and wakes at the next frame
anything painted changes (a spinner's frame, the scanners' next step, a breath's
next shade); a spinner turn swaps only its own cells, the moving frame sends
only its changed cells (a run at a time, the cursor kept where it was), the
blink repaints the panel once a second (a panel with only a blink wakes once a
second and changes only the blinking row), and an idle panel with nothing to
blink paints nothing between native frames.
Then the metadata, one block. Beside the native area the repository and Git rows
start with dim labels in the label column of `TITLE` and `NOW` (`REPO`, `GIT`;
narrower than 30 cells inside the labels go first): `REPO   hearting · wt 12`,
the repository name in its color with no trailing `/` and the worktree count
dim (none for zero or an unknown count), and `GIT    main ✓`, all dim with no
parentheses: the branch and dirty mark cut at 20 cells (32 with 60 cells
inside), then `✓` when ahead and behind are both known to be 0, else `↑N`/`↓N`
(nothing when unknown: no upstream to compare with), then the existing merge
warnings and `💀merged`; no Git row when nothing is left; no folder or branch
glyph. Only `↑N` and `↓N` take a color, Fleet's own (`lvl_g` green, `lvl_r`
red), on the Git row, the bottom layout's head and Claude's statusline alike;
mono and eight colors paint them dim as the other Git words, and `NO_COLOR`
keeps the words with no color. The bottom layout and Claude's statusline keep the repository name
ending in `/` (`hearting/`, the mark only on a whole name) with Git dim in
parentheses (`(main ✓ 🚧 3)`, the same branch cut and `✓`; Claude's statusline
cuts at the width its lines are shown at, its bash fallback by `COLUMNS`),
cut with `…` within that row and absent without a Git reading; Fleet's own Git
words are unchanged. Beside the
native area one empty row then parts the repository and Git from the model
(without Git, the repository from the model);
model and effort, then `context` with its gauge and percent (two rows beside
the native area, one `│`-separated row with the repo row at the bottom). Beside
the native area these two rows take the same dim labels, `MODEL` and `CTX`
(`MODEL  Fable 5.2 high`, `MODEL  Checking` before the model is known;
`CTX    ━━━━──── 41%`, the label standing for the word `context`); short of
cells the model name is cut with `…` and the effort stays whole, and narrower
than 30 cells inside they go with the other labels;
running jobs and the repo's open work (`◌ N open · headline`, giving way with
the jobs). Beside the native area one thin rule (the messages rules' color)
then parts it from this session's content, with or without a session title (a session
that has none yet, any harness, keeps the rule, and no empty row follows it): the thin `─` in every state; under the
default `rule motion` a light passes over it while the session runs (above), otherwise
it stands still (the session's state is told by the outer frame and the head's state
glyph). The
content: `TITLE` and `NOW` (Fleet's words, dim,
in one label column; the values start in one column and their next rows start
there too; narrower than 30 cells the labels go first), Fleet's title (bold,
in the session's harness color, Fleet's `hb_*`, on every row; plain without
color, the weight kept without color as the steward's `⚑`) and NOW (bright
while the session works or waits), each on as many rows as it needs, then a
tidy notice; one empty row then parts the head from the sections. The bottom
layout keeps the metadata row (or rows) first and one content row
(`title │ NOW …`, each keeping at least half of what is left).

Under the head the panel is a few rows per section (11-15, popup design E), each
a press away from its popup, where the section's own table reads whole: in order
QUESTIONS, the route box, LAB, SESSIONS, MESSAGES and MEM (beside the native area all of
them; under it QUESTIONS, the route box and MESSAGES). Every other section's head has one form:
its count where it has one, its name tag in dark gray upper case, its counts by
state, a thin rule, and `›` (`3 QUESTIONS ──── ›`,
`4 LAB ──── 실패 1 대기 1 ›` with LAB's counts at the right, `5 SESSIONS ◑2 ⠏1 ●2 ──── ›`,
`MESSAGES +3 ──── ›`; `MEM ────` has no popup and no `›`; under the native area
`messages` keeps its word). The routes stand in the one thin rounded box, as
v4.36.4 drew it (restored 11-15: the person had asked for the routes to open a
popup, not for the box to go), its title in its top line (`╭─ 4 ROUTE │ 2 REQUEST ─╮`,
under the native area `route 4 │ request 2`). Nothing opens in place: a press on a
head or on any row of its section opens its popup (any row of a route, its workers'
box too, the routes popup with that route unfolded; the box's top line the routes
popup; a request row the MESSAGES popup; a session's row with that session).

- QUESTIONS: the count is the answer wait's (the `◑답 대기 N` row is merged into
  it, in the question's rose), then the answers on their way (one line each),
  the next question's summary (its first row `◑ …` and its choices on one row
  `1 label (recommended) · 2 … · +2`, or its first two rows) and, for a wait
  answered elsewhere, where (`1 QUESTIONS · answer below`). The question a press
  answers here, now, reads as one button (11-15, panel polish 1): its rows stand on
  the rose fill across the panel's inside width (`FACES.*.fill`, 52 on a dark or
  unknown face, 224 on a light one), its first row bright, its choices numbered
  chips (` 1 label `; short of cells the last chips leave for their count `+N`, and where not
  even the first fits beside the count it shows with its name cut by `…`, its number kept,
  the rest counted: the first choice always shows), and under them `↵ answer · Ctrl+]` in rose at the right; a
  press on any of them opens the question popup. Short of rows the action row goes
  first, then the choices; the first row stays to the last. Mono, eight colors and
  `NO_COLOR` lead those rows with a rose bar `▌` instead. Nothing else in the panel
  is painted: not a wait answered elsewhere, not an answer on its way.
- The route box (spec rules 23-25, 34, 36, 38, 43, 48, 57): the routes the statusline
  counts, then the recent results the producer keeps after them, then the requests
  this session sent (→) or received (◂, `✓` closed) with their status and age, every
  one of them a row (no `+N more`); laid out at the box's inner width. Beside the
  native area a folded route reads who / context and time / what / which kind. Who:
  the glyph, the doer's recorded role as a dim label in one column (`owner`, `frame`
  for a frame or frame-alternative leg, `worker` for a stage worker; empty when the
  record names none), then harness, model, effort and a one-step route's open tool
  (only where it fits), in the colors of Fleet's ROUTES row (`⠏ owner  claude Opus 5.5
  xhigh`). Context and time, two cells in: the context gauge (the head's gauge and
  level colors, about eight cells in 40) and percent, and the elapsed time at the right
  edge, each in its own column; an unknown context leaves its cells empty, and with
  neither value the row is left out. What: two cells in, its title on at most two rows,
  the second ending in `…`. Short of cells effort goes first, then the tool, then the
  harness; the role and the model stay, and a value is shown whole or not at all. When
  the producer names the owner and the stage workers running now apart
  (`workers_live`), the first row is always the owner's, the context row starts with a
  dim `CTX` in the role column with the gauge under the harness and the route's time
  right after the percent (`CTX    ━━━━──── 52% · 2h 10m`; short of cells the bar
  shortens, then goes, then the time; the percent stays), and the owner's live workers
  stand in one rounded box under its rows, two cells further in (its left line under
  the owner's `CTX` and title, its right line where the route's rows end): each
  worker's first row (its mark, `worker`, harness, model, effort) in the box's top line
  (`╭─ ⠸ worker claude Sonnet 5.5 ─╮`, a later worker's in the line between, `├─ … ─┤`),
  then its `CTX` row (its own time after its percent) and its stage, open tool and
  target. A fourth row says which kind of route it is and where it stands, dim:
  Fleet's kind word (`code`, `lab`, `frame`), then `one-shot` and what it is on now
  (`Edit host.cjs 3m`), or `staged 3/7` with the current stage and its mark, or with no
  stage running the next stage and what it waits for (`code · staged 3/7 · execute ●`).
  A request row: its mark, the other session, the title and the status (`· open`,
  `· partial answer`, …), then how long ago; short of cells the age goes, then the
  title is cut with `…`, the other session and the status stay. Every open request is
  listed; a closed one sent by this session stays 15 minutes after it closed, or until
  its last answer or cancel notice has been seen (at most 7 days), and then only `show`
  lists it; a request whose message is not confirmed yet stays with `not confirmed`. The
  bottom layout keeps one row per route: title, harness·model·effort, context, then the
  kind (`code · staged 3/7 execute ●`, a failed one `staged 2/3 execute ✕`), then the
  open tool, and the elapsed time at the right edge (short of cells the kind word goes,
  then the tool, then the word `staged`). The box's line is the route line's color (the
  failure color while a current route has failed), its count pulses while a route
  runs, and no empty row parts two routes; with no rows yet the producer's own label
  stands over a `Checking` row, and a known empty list draws no box. The `route line`
  setting picks what a route keeps in its one-row form short of cells: `time` the
  elapsed time, `model` its context.
- LAB (spec rule 59): this project's experiment board (`utilities/experiment_board.py`),
  worded by `tools/fleet/session_lab.py` with the Fleet LAB table's own cells:
  three conditions in the board's order of what to look at (failed, stuck,
  unconfirmed, not comparable, waiting, then the best latest value), each
  CONDITION, STATE, PROGRESS and DIFF; an unknown PROGRESS is Fleet's `?`. A value
  is whole or its column leaves; only a name ends in `…`. Without conditions the
  goal or why there is none stands, and a note (a catalog not read, another group
  to look at, unnamed runs) takes a row the conditions leave. No experiments and
  no problem: no section. An experiment's failure never colors the frame, the
  glyph or the folded strip. Its catalog and run-record folders join the panel's
  file watch last (no poller): a folder is watched whole (a path ending in the
  separator), so any record written, replaced, added or removed in it redraws, at
  most once a second with the last change always drawn; a folder made again at
  the same path is watched again, a failing watch made again at most once a
  second; past four folders the rest are an unconfirmed note. A board that cannot
  be read or worded is LAB's own unconfirmed state and keeps its watch.
- SESSIONS: the sessions on this machine from Fleet's session table, the ones a
  person must answer first (every session Fleet's table counts as waiting for an answer,
  the `◑` gates and the `●` of a native decision alike), then the running, then the idle, this window's own
  left out of the rows and of the head's counts alike (the head and the rows count
  the same sessions; a count is one glyph in one color, so the waiting `●` is counted apart from
  the idle `●`, the waiting groups first); each its state glyph, `[number]`, TITLE (in its harness's bold hue)
  and AGE. Its values come from the sessions popup's own reading (below), made
  while another session waits for the person (and once when that ends) or when
  the popup opens; with no reading yet the section is not drawn.
- MESSAGES: the recent messages, one row each (`← [e7] text`), three at most;
  `+N` counts the rest. The requests stand once, in the route box (above); the
  popup reads both whole, the requests first (a request's numbers, reports and
  sealed body).
- MEM: the newest saved memory records of this repository and the global ones
  together (11-15), newest first, two at most, from the journal Fleet reads (the
  newest `add`/`supersede` rows of its project, and the global ones of the same
  read), one row each saying what the record is as the memory system records it:
  `◆` durable or `◇` working (`·` for a record naming no tier), then dim its scope
  (`project`, `global`) and its kind, the stored headline (else the start of its
  snippet), its age at the right. Short of cells the headline is cut first, then
  the kind leaves, then the scope (the mark and the age stay); no section without
  a journal row.

The panel paints no clock and no last-input row. A value that was not
observed is left out, never painted as `?` or an estimate (LAB's `?` is Fleet's
own word for an unknown value). When rows run short they give way in this order:
empty rows and a value's further rows (title, NOW, a route's title); every other
section down to two rows, then to one, the lowest first (MEM, MESSAGES, SESSIONS,
LAB; the question's summary keeps one row and the guidance line goes); the
structural rows (the empty row inside the metadata, the head's rule); then the
route box's own steps, every route at the same step (v4.36.4, spec rules 34 (b), 38,
48 (c)): the context rows fold into the first rows (percent and time at the right,
no gauge) and the workers take one row each, the workers go (the kind row ends with
`· worker N`), the kind rows go, every route takes one row; then the box shows fewer
routes and scrolls in place (the wheel over it), down to one, its border counting
what it hides (`↑2`, `↓3`; it takes the rows of its tallest window, so the wheel moves
nothing else); then Git; then the route box (v4.36.4's place for it, before the running
jobs); the running jobs; then MEM gives its row, then its head, then MESSAGES,
SESSIONS and LAB the same; the question's head and first row stay to the last;
5h/7d last of all. Rows a later step frees beyond its need go back to the steps
before it, the most important first. A section is placed whole or not at all; the
rows left over stand above MESSAGES and MEM (which keep to the bottom, with their
press targets), or under the last section when there are none.
Groups are divided by `│`, stages joined by `›`. Fixed words are English
(`context`, `route`, `messages`, stage names, `5h`/`7d`, the section names
`QUESTIONS`, `ROUTE`, `LAB`, `SESSIONS`, `MESSAGES`, `MEM` with their counts
first, Fleet's tab names); only real content and language-dependent text follows
the screen language: the answer wait reads `◑답 대기 N` on a Korean screen and
`◑ to answer N` otherwise, Fleet's same strings. Times use Fleet's spaced form
(`45m`, `3h 20m`, `3d 0h`) for elapsed time and quota resets alike.
Variable-width text (titles, message
bodies) is cut with `…` before fixed-shape values (percent, glyphs, elapsed
time), in two-cell Hangul and emoji as well. Claude's exact statusline callback
remains the source of its values; the panel shows them in the same groups as
the other harnesses.
Fields already painted in a native-owned status area are omitted from the compact duplicate.
Physical exceptions are explicit: Claude's reserved native status row and
OpenCode's native Context/footer and native scroll gutter.
Native-owned spaces are not recolored or resized to imitate another harness.
Question interiors have one blank cell on both sides. Route state and shared
verification codes use the operator language; missing records remain absent or
unknown. Shorten displayed heads to seven characters and wrap route evidence at
token boundaries, omitting empty fragments rather than repeating separators.

The header keeps settings and observed model/effort in view even before session
binding completes. Native-selected display evidence is independent of question
or route binding; configured defaults never masquerade as observed selection.
An official startup callback's active-model field may travel with its existing
birth-checked lifecycle event, without another observation pass or event. This
display field never selects a session or authorizes input.
A native source that exists but was not read (an I/O error, a replacement
during the read, or a failed read of a readable OpenCode database) keeps the panel's last model and context for the same live
session, as Fleet's native reader does; the next projection reads it again.
Another session, a new process, an absent or refused source and an ended
process still clear them, and an unobserved value is never made up.
Beside the native area the two cells between the native and the panel take the
native's own background at that edge when it paints one (OpenCode paints its
whole area): the native's area then reaches the panel's frame and the panel
stands with the same space on both sides (inside, one cell between the frame
and the text on every row, in every harness); a native on the terminal's own
background keeps them plain. The panel's own background does not change.
The route box's count is the producer's whole current-route count (`4 ROUTE`,
under the native area `route 4`; the producer's own label alone, with Fleet's
running, waiting and stopped counts after it, `4 ROUTE ⠏2 ●1 ✕1`, over a `Checking`
row while its rows are not read yet; a count the producer sent no rows for stands
in the box's lower line). Each current route (the ones the statusline counts) and
the recent results the producer keeps after them carry Fleet's route marker and
judgment (`⠏` while anything of it runs, its owner or a worker; `●` waiting, with
nothing running or on a person; `✕` stopped; `✓` done, a result that did not pass
`✕`; `◌` no response, nothing of it running). No press in the panel opens Fleet;
Fleet opens only from its own shortcut or the popups' `f`. These values come
from the answer-wait producer's existing pass: the shared line helper reads
Fleet's own route records from that pass and adds each row's marker and the
values of who does the work now (the working worker of the shown stage, else
the owner: harness, display model, effort, context, tool, active sub-agents;
with nothing of the route running, the owner attempt's harness, model and
effort as it recorded them, without context, tool or sub-agents)
plus an absolute start time, and counts running, waiting and stopped routes
into the statusline label (`route 3 ⠏2 ✕1`) by Fleet's own rule, over every
current route of that pass rather than the kept rows; a route
its supervisor closed is in neither count. Context is Fleet's own gauge
percent, else tokens over a known window, else the exact token count
(`250K`); a stopped job shows none. Before an attempt's own result the window
is its own evidence (usage above 200K is the 1M window), else the window its
launch requested (`CLAUDE_CODE_AUTO_COMPACT_WINDOW` in its launch env, 100K..1M)
when its settings env sets no other one and the model running is the one the
launch chose (this path is a dispatch worker's, the route rows'; a seat keeps the
native statusline's own value); `CLAUDE_CODE_DISABLE_1M_CONTEXT` holds 200K. That
variable is Claude's compaction threshold (the smaller of it and the model's own
window): a model the launch requested a window for is assumed to run with it. A start,
resume, `/clear` or switch keeps this seat's own values drawn (the folder, Git
and usage); session values clear until read again. A `/clear` (the native start
hook's own `source`) keeps the same process and model, so the model and effort
row also stays as last observed while its context starts empty; a start, a
resume or a switch reads the model anew (a configured value never stands in for
an observed selection). The judgment lives in one place
(`tools/fleet/session_line.py`) and uses Fleet's shared functions once they
exist. The display is as fresh as that cache; the routes popup reads Fleet's own
table and route page (owner, evidence summary, paths) for them.
The MESSAGES section shows the latest peer messages (`←` received
from the exact session's retained received inputs, `→` sent from this session's
own ledger shard and its 200-character summary), one row each (the arrow, the
other session's number, the text); the MESSAGES popup reads each with its head (the
arrow in its direction's color, the other session's number, then dim harness,
`HH:MM` today or `MM-DD`, the record's kind and a sent one's delivery state:
`delivered`/`sent` dim, `not delivered` (the recipient was never resolved; no age) in the caution color,
`failed` red) and, two cells in, the text in the body color from its first
meaningful words (leading bracket frames and heading marks left out, nothing
else changed), the newest first. Three show in the panel at most, however many
rows are left (`MESSAGES +2` counts the rest); short of rows fewer show from the
oldest. Two limits differ: the source keeps a received input's first 384
characters and a sent message's 200-character summary, never the whole payload;
the popup never cuts that retained text further: a word wider than a row, such
as a long URL or unbroken Hangul, runs on to the next rows by character cells,
keeping wide characters and combining marks whole. Nothing new is stored.
A press on a route, request or message row opens its popup (above) and nothing
in place; while no popup stands focus, `empty` and the native input frontier stay
as they were.
Put each answer choice on
its own line, wrapping within that choice; every choice row leads with a
two-cell column for the mark. In the question popup ↑↓ move the chosen
choice: its first row carries `▸` in this session's harness color, bold, and
every row of it (each row of a wrapped one) has its words bold in the bright
body color, while every
other choice keeps the mark's column blank and reads dim grey (without color
the mark and the weight alone tell them apart). Enter
sends it, as Claude's own question does; it starts on the recommended choice,
else the first. Number keys pick a choice and `t` types an answer as before;
the guidance reads `↑↓ choose · Enter send · t text` (on a Korean screen
`↑↓ · Enter 보내기 · t 답 입력`, then `Tab 다음 · Esc/Ctrl+] 돌아가기`). A decision
(approve/reject/defer) highlights nothing until ↑↓ or a number picks one, so
Enter alone still sends nothing. With the popup closed, arrows and Enter stay
the native's. Do not reserve a standalone
session/age row. Hide an empty result section. Use the
existing operator language setting and catalog. No last prompt is guessed
into a goal. Old peers and repo backlog stay in Fleet.
Route details reuse the common route-visibility projection for shape, basis,
tests, review, CI and completion evidence. Missing evidence is not a passing
check. Bounded detail pages remain readable at narrow widths; opening them
does not collect again or authorize an action.
The panel paints no thread ID: sessions are told apart by their number and
title. Inside the shell, Codex's footer field `thread-id` (the first
`[tui].status_line` item) belongs to the shell and is not shown: a footer row
holding only the ID (or the ID and a clip mark) is blanked, and otherwise only its leading ID cells are
(with a working spinner, and an untitled thread's title, which repeats the ID).
The value stays on the inner screen, where shell-wrapped identification and
peer delivery read it. The rest of that row may yield duplicate paint only to
the panel's visible model, compact status and session rows, all actually
painted. Exact native SID, the help row beneath (or, while a draft hides it,
the composer just above), an unchanged configuration
with `thread-id` first and matching native values are still required. Display
availability does not establish selection or input authority. A Codex window
started without the shell keeps the ID visible, since that footer row is then
the only place Hearting reads it.

The header's settings change only Hearting's existing session-shell policy:
side/bottom layout, panel position (right, the default, or left), side width,
narrow-window behavior, auxiliary detail, theme, shade, `route line`
(`time`, the default, or `model`) and `rule motion` (`rule`, the default, `border`,
`scanners` or `off`, above; the `on` saved before 11-15 reads as `rule`, and the file is
not rewritten for it). One placement rule
serves every harness. Themes are `terminal` (the default: the shared palette),
`light` (a light terminal: the reviewed deep hues) and `mono` (no hue; weight,
glyphs and words remain). Shade is `none` (the default), `light` or `medium`:
a neutral grey panel background (dark face 234/235, light face 255/254), only
on a known face, in 256-color terminals and never under `NO_COLOR`. The face is
light for the `light` theme, else the terminal's background where it is
already known (`COLORFGBG`; nothing is asked); on an unknown background `terminal` and
`mono` keep the terminal's own and paint no shade. On a shade nothing is dimmed
on top: secondary text and messages take 248/241, body text 253/237, the panel
frame 244/243 while idle or unknown, messages rules 242/245; the question's border,
`◑` and `›` are 174/131 on every colored theme.
Neither recolors native space: the native area and Claude's status row keep
their colors (Claude's notice row takes the theme, not the shade). An unknown position, theme or shade falls back to its own
default without disabling the shell, and so does an unknown `route line` or
`rule motion`.
The same side width changes by dragging its boundary or by value controls.
Apply saves after backing up the existing file; Cancel discards the preview;
Defaults previews the defaults. Never edit native model or permission config.
During an admitted peer paste, keys for a visible shell editor remain with that
editor. Only native-directed human bytes join the retained delivery queue;
leaving shell focus preserves their order behind the paste's delayed Enter.
An accepted answer's read-only receipt view is not an editable draft. It may
remain focused while the existing guarded native transport delivers that answer;
pending API calls, editable questions, changed questions and unknown receipts
retain the normal input-occupied behavior. Native identity, idle/composer and
effect-time checks still apply without a manual Escape step to wake the CLI.
Respect a minimum native area; use configurable bounded bottom height at 80
columns and preserve the same child through resize. No new global hotkey.
OpenCode uses the same right (or chosen left) and bottom HEARTING panel as
Claude and Codex, with the same renderer, settings (layout, position, width,
theme, shade) and content order. The
shell window hides OpenCode's own sidebar through its official CLI-config
merge (`OPENCODE_CLI_CONFIG_CONTENT` with `session.sidebar: "hide"`, given only
to the started shell's native child), so two right rails never appear; an
explicit `session.sidebar` in that variable wins, and OpenCode's own toggle
still opens its sidebar on demand. Context stays in OpenCode's footer, and the
plugin reports the MCP server counts its sidebar listed as a display-only
observation, painted with the model and context group as `MCP
connected/total` (red while one has failed or needs authentication), at every
panel height. No Hearting section is
drawn inside OpenCode's sidebar. Panel placement never changes question authority.

The current question's summary (its first row, and its choices on one row or
its second row) and exact request state are readable in the panel before the
popup opens; the popup reads it whole (its body, every choice, a text or secret
answer, `i/N` with Tab). An empty questions section reserves no space. Retain choices and drafts
by request ID, revision and question ID when moving with Tab or leaving focus.
Enter submits only explicit, complete answers. Keep a single active action.
A settled question (answered, expired, cancelled) stays to show its receipt
while the editor is focused; once focus returns to the native it is let go and
no longer asked for, and an answered registered question leaves the shared
answer-wait view the moment it completes, so the panel's count and question
follow the answer at once. Publishing re-reads each registered question's
source, and a source that is read decides: an open question stays, even one
answered before and registered again under the same id; one that is not open
(answered, expired, cancelled, removed, or replaced by its session's next
question) leaves. Only when a source cannot be read just then do the answers
already completed decide: a question that completion took out stays out, even
from a collection read before the answer, until its source is read; any other
stays as the collection read it, with the publication marked incomplete, until
the next collection.
Collect every current form draft at that one submit; moving with Tab does not
require Enter in each field and an edited draft supersedes its earlier value.
Secret answers remain masked while selected, submitted, unfocused or consumed;
only the unchanged canonical response carries their actual value.
Pending transport and proved consumption are distinct, and questions not
answerable here, expired or cancelled offer no active choices. A wait not
answerable here never reads "answer unavailable" or its machine reason: its
reason picks what to do, `answer below` (`아래 입력창에 답`) in this session's own
panel or `answer in its window` (`그 창에서 답`) from Fleet or another session for
a marker question with no bound reply surface, a missing exact session binding,
an unsupported native request and any reason not named here; `answer in the
parent` (`부모 창에서 답`) for a blocked worker's answer whose parent could not be
confirmed; `sending` (`보내는 중`) while another answer is on its way; `closed`
(`닫힘`) for a request already gone (an existing state word such as expired
stays). The head carries the short form under 64 columns and the body row the
whole words. Every count of answer waits (Fleet's head and session rows, the
`questions list` head, the status line, the panel's `◑ N`, the notice line under
the input and the folded strip) counts every wait a person must answer, wherever
it is answered: a question the panel answers, a Claude native request, a marker
question and a human gate. Only a blocked worker's wait (its parent agent's) and
a finished one (closed, replaced, expired, an answer on its way) are not counted.
The questions section holds what the person can answer here now (answerable
ones first; `Ctrl+]` opens the popup on the first of them); a counted wait answered in another
place is one dim guidance line beside the count saying where
(`1 QUESTIONS · answer below`), and the notice line names `Ctrl+]` only when the
panel answers one. A blocked worker's wait is in no count or question block (the
route row shows the route blocked), and it closes once the route closes or its
parent holds the worker's exact acknowledged completion receipt. Once the person
sends an answer it has left their hands: while it travels (the existing answer
record's `submitted`/`received`) the popup closes and the question is one line
(`✓ answer sent · <what was sent>`, which stands for rule 53's chosen choice;
`✓ answer sent` in the passing green of a route's `✓`, the `·` dim, what was sent
in plain text, a secret `•••`),
`Ctrl+]` no longer stands on it, and every count above leaves it out (Fleet lists
it as sent); the line goes when its session takes the answer, and a failed
delivery brings the question back with its existing word. Answering returns
focus to the native input, and one lone Esc or Enter within ten seconds of the
answer is taken by the shell (never the native's interrupt of a running turn,
never a submit of what stands in the native input).
A registered question belongs to its session id: the same session in a new
process (a native resume, a restart, a compaction) still answers it, and only
another session that took the seat ends it. Long questions remain readable
in the popup (PgUp/PgDn while choosing, the wheel over it) with an explicit
overflow count. Use the bounded configured row budget,
preserve at least six native rows where possible, and retain the shared status
fields. The former rows/3 estimate does
not hide a question or require another user decision at 80 columns.
It shows the same session's routes, questions, received peers, repo work and
last real human input. Only session-owned GPU work warrants a resource row.
`Ctrl+]` always opens a popup in the middle of the window (the native area and
the panel together; above the folded strip, never over it): the question popup
while a question waits to be answered here, else the first section with
something in it. A press on a panel section (its head or a row) or on a group of
the folded strip opens that section's popup, and a new question, a failed
experiment or another session's wait never opens one. The popups are four
kinds over one frame and one set of keys: QUESTIONS (the question editor),
ROUTE, LAB and SESSIONS (Fleet's own tables: the backend's `popup` reading draws
Fleet's `draw_routes` with one route unfolded, Fleet's route page, `draw_lab`
and `draw_overview(session_table=True)` into a grid of the popup's width and
sends the painted cells, Fleet's row tints left out; narrow, Fleet's columns
leave as Fleet leaves them), and MESSAGES (the requests and messages whole, then
the peer delivery reading `r`). The bottom line lists the sections with
something in them, the current one bright; ←→ (or a press on a name) moves to
the next or previous one. In a table ↑↓, Tab and Shift+Tab unfold the next or
previous route or session (Fleet's `▶`) or read on a row; Enter opens the
unfolded route's page (Fleet's route page: what is to do, the verification lines
with the route's paths, the stage frame) and again goes back to the table; Enter
on a session that waits for the person (`◑`) opens its question in the question
editor (below); PgUp/PgDn and the wheel read a longer table on (its place,
`3/20`, leads the keys row; ↑↓ and Tab keep the unfolded row in view, and a table longer than the rows the popup carries, or than the answer the host reads, shows the rows that fit and ends with one dim line for the rest in the entrance's words, `외 N개`); `p` folds or opens the panel (the popup closes so
the panel is seen), `r` the messages, `w` the open work, `f` Fleet; the keys row
names them, broken between its pieces (` · `) where the popup is narrow, never inside a word
(the question popup's keys and the table and MESSAGES popups' keys are one rule, `keyRows`; the
closing key `Esc/Ctrl+]` stays to the last, and short of rows the pieces before it go first from
the end backward). A standing
table is read again at the events that change it, never on a timer: the routes
when this session's route rows change (the shared answer-wait cache's existing
watch), LAB when its board's watched files change, the sessions when their
record folders change (Claude's `sessions/`, the Hearting registry's codex and
opencode folders: watched whole only while the sessions popup stands) or another
session's answer waits change; one reading at a time, in its own worker (a
person's read never takes the answer's action slot; a read past its 12 seconds ends then, an answer's does not). In the sessions popup,
Enter on another session's `◑` row reads that session's questions into the
question editor apart from this session's own (its number leads the title,
`[12] › ◑ 1 QUESTIONS · 1/1 · 답 필요`); the answer goes once, for that question
id, with that session as the exact owner the questions API binds
(`expected_owner`; this window's own Codex attachment speaks only for its own
questions), and a wait with no owner binding to answer it here reads `answer in
its window` (`그 창에서 답`) with no choice, a wait with no question to read at all included. The question, its number and its answer are the session Enter was pressed on. Going to that session, restarting or
tidying it stay Fleet's (`f`). While it
stands every key and press is its own (none reaches the native, a native that
reads the mouse included; whatever follows an Esc, in the same read or within its
25 ms, other than `[` or `O` (an arrow or function key) is dropped, neither closing
it nor reaching the native: Alt+letter, Shift/Alt+Enter, Esc then a control byte, and a
second Esc closes it and is dropped; a paste is the popup's and does nothing, its end mark `ESC[201~` read whole wherever it is split, and a paste that never ends is left by Esc, `Ctrl+]` or a press outside; the popup's input state, a paste under way, a key's front and the Esc waited on, ends with the popup, closed or opened again); a lone Escape,
`Ctrl+]`, its `✕` or a press outside it closes it
and returns to the native input and cursor (the drag and release that follow that
press are the popup's too; one lone Esc within a second of the Esc that closed it is
the shell's, never the native's; a key that starts with Esc, such as Alt+letter or
Shift+Enter, reaches the native whole), and a draft stays for the next opening. When the
question being read is replaced by another with nothing typed, `question changed`
shows once and the first Enter after only clears it. Ordinary typing, including
digits, Enter, Tab and action letters, remains native while it is closed. The
popup is one layer over the host's own screen: the native's output under it
keeps arriving in the host's copy, every paint lays the popup again in the same
synchronized frame (a moving frame or spinner under it leaves it whole), closing
writes its rows again from that copy, and the PTY keeps its size. While a popup
stands (11-15, panel polish 3) everything outside it, the native area, the panel
and the strip, reads in one dark gray without any other color, weight or ground
(`FACES.*.veil`, 239 on a dark or unknown face, 250 on a light one): each row the
host writes anyway is written with that gray, so output under it keeps arriving
veiled at the frames it would have come at, and nothing in the panel turns (the
moving frame is a still gray line). Laying or lifting the veil writes every row
whole once; closing shows the screen as it is now in its own colors. The native
is never told (no size change, no focus report). Mono, eight colors and `NO_COLOR`
veil nothing; the popup's frame line is bright there instead. Where it veils, the
popup reads apart from what it covers (11-15, the person: "팝업이 여전히 구분이 잘
안되는데? 배경 컬러도 다르고 ... 폰트가 좀 더 밝아야"): every cell of its frame stands on
its own ground (`FACES.*.popup`, 236 on a dark or unknown face, 254 on a light one; a
part with a ground of its own keeps it), its line in `popupLine` (250 / 240), its title
and plain words in the brightest text color (`bright`, 255 / 232), and a word drawn dim
or gray (Fleet's dim table cells, the keys, the other sections' names) never fainter
than `popupFloor` (247 / 241); a hue (a state, a harness, the question's rose) keeps
its color at full strength. The empty cells round the frame keep the terminal's own,
and closing it leaves no ground behind. One frame
serves every popup: a thin rounded line one empty cell from what it covers,
the title and `✕` on its top line, the sections on its bottom line (the current
one bright) and the keys on the row above; text 76 cells wide, a table the window
less 4 (116 at most), both the window less 4 under 80 columns and the whole
window under 28; its content's height, at most the rows above the strip less 2,
paging inside. A low window gives up the empty rows round the frame first (under
8 rows), then its bottom line (3 rows or fewer), so a 20×6 window still shows the
content. A popup that is not drawn (while the host is handed over) owns no keys. The focused question editor uses existing `questions list/answer`
choice, text and decision operations; original questions, exact binding, claims
and receipts remain with that common API. Marked Codex questions use the same
board, never a new question store. Runtime notices that carry these answers
select the same verified native attachment before consulting optional panes;
claims, producer bindings and exact-consumption receipts remain unchanged. Route, peer, repo and tidy actions use their
existing entry points. A changed selection cannot retarget an in-flight action.
Fleet's absence does not prevent an existing peer runner from
waking a shell. Shell display and input transport add no model instructions.


The common line (the collapsed panel row and Claude's statusline, inside and
outside the shell) reads `[e9] hearting (main 🚧 3) │ Fable 5.2 xhigh │ context
━━━━━━──── 58% │ ◑답 대기 1 │ route 3 ⠏2 ✕1 │ 5h 12%(↻ 2h) 7d 66%(↻ 5d)`;
outside the shell Claude's second (`>_ running:`) and third (tidy notice) rows are
unchanged; inside it Claude's statusline is that one line (below).
Narrow layouts shorten words before numeric values, routes and waits (the
answer wait shortens to `◑ N`); running jobs at the line's end go whole before
another group shortens. Where even the shortened line does not fit, it keeps what
the person must act on, the most urgent last to go: the answer wait, compaction
imminent, a context or usage reading at Fleet's red level, then the route count;
calm readings go, and the kept readings lose their reset time, then the context
gauge, before any of them goes. Its title and guidance use Fleet's
shared message catalog and language preference. Fleet owns the values, palette, glyph meanings, separators and
unknown-value rules. Native display callbacks and exact-session telemetry are
valid sources for these display values, including Claude's statusline JSON.
They never establish input readiness or add anything to the model's input.
An observed non-Git directory is `no-git`; unavailable Git evidence stays `?`.
While wrapped, the former Hearting Claude/OpenCode decorations yield to this
line. Unwrapped sessions keep their existing displays. Codex keeps native input,
execution and permission UI; verified common fields may replace duplicate paint.

Event-driven updates use native lifecycle and the existing record changes,
without tuning a polling interval. Fleet requests its narrow display reader on
file/lifecycle notifications or an adopted snapshot, never on an independent
periodic source probe. Validation compares native-to-common update
lag (median/p95) for end-of-turn context/model, route creation/stage/closure and
question creation/answer across all three harnesses. The target is at most one
additional second; keyboard latency stays within the prototype's +2–11 ms.
A value that cannot meet this is compared with retaining that native field.
While wrapped, Claude's panel shows the common status area in the same place
and with the same formatter as Codex and OpenCode. Claude keeps its reserved
statusline row and keeps running its callback, which stays the value source and
paints the former Hearting fields with that formatter on every callback, as one
row at the native width: the common line with the running jobs at its end
(`>_ name`), shortened by the common line's own narrow-width rule. Its first
callback at session start reads the same shared Git observation as later
callbacks, so folder, branch and model show before the first input; context and quota readings Claude has not reported yet
(it sends them after its first response) are left out, as the native statusline
did, rather than shown as `?`. Only while the panel actually paints the groups
that row carries (number and project, Git, context, the answer wait, the route
count, 5h/7d and running jobs) for the callback's exact session does the shell blank Claude's
statusline area, the rows directly under the input box, when their text equals
the callback's latest recorded output. A group the panel paints counts even
when its row is cut to the panel's width or wrapped; only a group the panel
does not paint at all (dropped for rows, settings) keeps the
native rows, so no value leaves the screen. Folded, the strip takes the row's
place while it paints its session row (it carries no Git or job row), so the
values show once and the row keeps the notice; a strip short of rows down to
what to do alone leaves the native rows. A native screen too short for the whole
record (the bottom layout's ten native rows) shows only its first rows, running
to the screen's last line; those rows are the recorded ones and are blanked
too. A screen without that input box, such
as the Ctrl+O transcript, keeps every row. Claude keeps as many rows as the
callback's output has, and at least one while a statusline is set; the one row
leaves two rows under the input box, this row and Claude's own mode row, at
every width and with running jobs. That row is the notice
row, empty unless the person must do something now. One function picks a single
item by priority from existing values: an answer this session owes, then
compaction imminent, then a context or usage reading at Fleet's red level, then
a failed, blocked or attention-needing route. News and release lines are not on
it: a received peer report stands in MESSAGES, a finished route's result in the
route box (the producer's recent results follow the current routes), and a
waiting release or a switch without scrollback in the panel's tidy-notice slot.
A finished result's row leads its title with the result word (`FAIL · title`),
takes Fleet's failure mark `✕` and color when the result did not pass, and leaves
the rows after the person's next input or ten minutes, the bound the notice row's
news used.
It reuses Fleet's glyphs and colors and adds no collection, setting or judgment.
A hidden (Fleet over the window), focused or not-yet-bound panel and a lost display host show
that native row unchanged; unwrapped sessions keep their complete native rows.
Unwrapped displays remain intact.
First display and updates are compared beside native and Fleet. Last-known
values and event observations belong in their shared source, not a block-only
store.
The marker did not survive Native Ink, so no native row is removed or inferred
from geometry; blanking matches exact recorded text only. Claude's status row
follows its native callback cadence; block updates retain their event-driven
contract. Parity compares Claude's panel status with its native row.
First paint reuses the latest display observation before optional collection.
Native terminal context is presentation evidence only, never input authority.
Fleet may consume that same narrow display observation between its normal
collection passes. The observation is bound to the current native session and
process birth; it contains no prompt, question or answer body. Updating context
or model text does not admit a new session, refresh action authority, complete a
collection generation or schedule another detail pass. Basic collection and its
CPU protection keep their existing cadence.
OpenCode's visible footer context percentage owns its common-line percentage too.
Wrapped Codex keeps its original native arguments, settings and permission flags.
OpenCode's Hearting route footer yields to the shell. The wrapped Codex viewport
reads its native footer unchanged. Concealment requires the unchanged managed
default preference, exact native SID and matching values for all seven managed
items. The work panel must actually paint its model, compact status and session
rows for that same selected session. Custom,
clipped, stale or ambiguous content stays visible. This affects
painting, not the native VT, its coordinates or its input/identity observations.
Person-requested answers use the existing separate action worker so a slow
submission cannot stall native input, readiness or event-driven display values.
An answer originating in a wrapped Codex terminal retains that exact attachment
in its existing private answer claim. Reuse the lifecycle bridge's current-root
proof before accepting it and at the existing native effect boundary, including
retained delivery retries. These authority reads refresh MCP origins even for
already loaded roots; loaded membership alone does not prove current selection.
An unknown selection, including multiple roots without that native viewport
and process evidence, withholds the answer;
an old displayed question, a later question or a waiting worker grants nothing.
This restriction belongs to the wrapped answer, not ordinary full-session-ID
queue delivery. Existing queue and consumption receipts remain unchanged, and
an already consumed answer can still settle after the terminal changes.
The block reserves only the rows it paints. Hidden guidance and empty sections
reserve no rows; native-owned spacing remains under the native renderer.
Inside a complete Herdr pane context, an eligible wrapped session uses Herdr's
official `HERDR_AGENT` foreground-wrapper hint. Keep an explicit operator hint,
leave unwrapped/noninteractive ingress unchanged, and never export it globally.
The hint must be present in the wrapper's initial exec environment so Herdr can
use its native manifests and integration authority. Do not imitate `herdr:`
reports with a Hearting-owned source. Native identity and input guards stay intact.
Preserve the native terminal-title signal through the VT attachment as well.
Herdr's native manifests use that signal for activity and its original title.
Forward normalized, bounded title text only; never forward control characters
inside a title as another terminal command.
A pane address may use that same native attachment only when its birth-bound
owner is the pane's current foreground group. Recheck that join immediately
before delivery; keep the existing composer/lifecycle and Codex queue guards.

Before the first lifecycle callback, a clipped Codex footer may supply only a
read address to the existing loaded `thread/read` display observation. The live
inner process/birth and its existing control socket are checked before and after
the bounded read. The reply supplies model/effort only; it never selects a SID,
publishes an attachment, subscribes/resumes a thread or authorizes input. An
unloaded/foreign/changed/unavailable response stays unknown. The native-output
event triggers this read; it creates no polling loop or additional collector.

In a short window every harness's question popup keeps the same question,
choices, draft and key rows, paging the rest; the panel under it keeps its own.
