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
(`HEARTING_SHELL_HOST_PATH`, never resolved, including the Herdr hint's re-exec of the
entry) with its own resolved file; it
notices a change from a watch on the link's directory and at each idle
transition, with no timer; a successor that took over keeps the same watch. A look that
ends without asking is not lost: a reason that comes during a look is taken up when it ends, and
the second's pause after an attempt that ended unsafe (or a reading the backend did not answer)
ends in one more look. While a newer host is installed and not yet taken,
the tidy-notice slot shows one lowest-priority line (`session_shell.upgrade_pending`, dim and
without `⚠`, as a notice that leaves the person nothing to do), never over another notice; beside the native area the panel's thin rule that parts the
session's content from the head goes with it, and nothing else on the screen changes.
A popup stands over the window: the panel under it keeps its notice slot.
The host asks the PTY owner for a successor only at a safe moment: idle, an
empty composer, no open form, no popup, settings,
Fleet explorer or panel focus, and no delivery, held bytes, paste, drag or
action worker. A question waiting on the panel does not hold it (the new host draws the
QUESTIONS box again and the same keys answer it); one being answered does, by the panel focus or a popup. Keys that reached native since the last submitted turn leave the
composer unproven (an erased draft, a form answered with Enter, a local command such
as `/model` whose menu opened and closed, history-scroll keys, a turn stopped without
a stop callback) until the native has painted after them and a reading at least two
seconds after the last of them sees that newer screen idle with an empty composer;
delivery readiness settles them the same way. The two seconds are compared at a reading
that comes anyway (a courier pass's status, an idle transition), never waited for: right
after a person's Enter the composer is empty before that turn's prompt callback comes, and
a slow native repaints its old screen before the keys' own. A draft or form on that screen
keeps them unproven. Keys and reports that cannot put a character into an empty composer
(a lone Esc, Left, Home and page keys, a click the native reads, the terminal's own
replies), when nothing else came since the composer was last seen empty, owe no repaint:
they change nothing on screen, so after their two quiet seconds the reading alone settles
them (11-16: a window kept messages for hours after an Esc). Keys that may have typed, or
brought a past prompt or a suggestion back (Up, Down, Right, End), and the native has not
painted after (an unechoed CSI u key) wait for its next paint or
submitted turn; a focus report is no key and restarts no quiet time. OpenCode's keys always
wait for the next submitted turn: its focus can leave the composer while the composer still
reads empty (a dialog such as `/models` over the transcript, its terminal pane). For
delivery a Claude session record that says `shell` (the turn ended while a background shell
it started still runs) is at rest as `idle` is; tidy and Fleet read that record as before.
A start or selection (a resume picker's Enter, `/clear`,
OpenCode's plugin reload) counts the bytes up to the last Enter as submitted, as
delivery readiness does; keys after that Enter are settled as above.
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
fold button `▾` at the right end of the panel's last row inside folds it (11-16, the person:
"접는게 하단 접힘인데, 여전히 화살표가 우측이고, 위치 자체도 직관적이지 않음": it points where the panel
folds to, the strip under the native area, in every placement, and stands where the strip's `▴`
opens it again, so folding and opening are one place). Folded there is one form, by itself or by hand, beside or
under: a strip under the native area, which keeps the whole width (the native
is told its new size through the same PTY resize a window resize uses). The
strip is one row (11-15, as the person changed rule 49 (c): "전체를 한줄로만"), whatever the
window's height and whatever it shows, so a question coming or going never moves the native
area: the session (the state glyph and number, the project and its Git branch where cells are
left, ` (branch)`, twelve cells at most, the model and effort, `CTX` and its gauge, 5h, 7d) │
`◑답 대기 N` in the question's rose while a person must answer (the wait alone: no question words
or choices; compaction imminent and a tidy notice stand in the same place) │ the routes
(`3 ROUTE ⠏2 ✕1`, no titles) │ `MESSAGES` with the sessions the recent messages came from or went
to (`← [e7] → [73]`: each way and session once, the newest first, `+N` for those that do not
fit), and `▴` at its right end; a group with nothing in it does not stand. Short of cells it gives
way whole value by whole value, in this order: the reset times, the branch, the effort, the model
windows, 7d, 5h, OpenCode's MCP count, the model, the messages' sessions (their count stays,
`MESSAGES 3`), MESSAGES, the project, the windows at Fleet's red level (11-16, review of #1662: they outlast the
other windows, the backend marks their parts `level: red`; the last of them goes first, so the 5h stays longest),
the notices, the CTX bar (its percent stays), the routes'
counts by state, and narrower than 44 cells `CTX` itself; past that last step the cell before `▴`
is taken too, and then the session's number gives way (11-15, review A5: from 30 cells the wait and
`N ROUTE` stand whole beside each other); the glyph, the wait, `N ROUTE` and `▴` stay, and nothing
wraps. Labels are dark gray upper case; there is no frame, gear, beat or moving rule, and only the
state glyph turns and, while an answer waits, `◑답 대기 N` blinks (below). The strip has no status button. While the strip paints its
session, Claude's row under its input is the shell's one line (spec rule 60) as with the panel open, so no value
shows twice; the strip carries no Git or job row. What the old notice row said stands on the strip: the answer wait
(`◑답 대기 N`, never given up), compaction imminent (the notices), a red usage window (kept as above), a failed
route (`✕N` after `N ROUTE`); a red context reading is the `CTX` percent, in its color. A press on the wait opens the question popup, on the routes the routes popup, on MESSAGES
its popup (above the strip: the strip stays and the native keeps its width); anywhere else, and
`▴`, opens the panel.
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
MESSAGES and MEM keep to the panel's bottom, above the `5h`/`7d` row (beside the native area one thin
rule, a section head's rule in its color, parts them from that row: 11-16, the person: "MEM과 가장
하단을 구분하는 줄이 필요"; short of rows it goes before any content row), so the
rows left over stand between the sections above them and MESSAGES (the person's own
word, 2026-11-11, restored 2026-11-15; without MESSAGES MEM keeps to the bottom, with
neither the rows left over are below the last section). The top border carries Fleet's row head for this
session on the left: the state glyph (a spinner while it works or an answer is
being typed in for the person, `●` idle, `◑` waiting on a dialog or question),
the session number (`[e9]`), the harness name in its Fleet hue and, for a
steward, Fleet's `⚑` followed by two cells before the line (a steward's number takes Fleet's steward color, fixed,
as on Fleet's session row); the state has no word, its glyph and number share the
state's color (11-18: but a spinning glyph is in the session's harness color, `hb_*`: Claude 173, Codex 147, OpenCode 153; the same for every
working spinner on a line that names its harness: a route's owner and worker rows and a frame leg here, Fleet's SESSIONS rows, route rows and
PEERS cards; a route's first-line `●`, a stopped, waiting or finished mark and a line with no known harness keep the state's color), and the three
glyph shapes still differ in `mono` and under `NO_COLOR`. Short of cells the steward mark goes first, then the harness, then
the number; the glyph stays. Right of that head the border is the line alone:
the gear `⚙` stands on the panel's last row inside, three cells left of the fold button `▾`
at its right end, beside 5h/7d and the model windows (`5h … │ 7d … │
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
native area and under it alike (the folded strip has no frame: only its state glyph turns, and its wait blinks), as the
setting `rule motion` picks (11-15). While the session runs (an answer being typed in included):
`rule` (the default) keeps the outer frame still and lit one step below the running color (187;
`BORDER_LIT`) and passes a light over the thin head rule: its middle five cells brightest and four
on each side one shade down each (`FACES.*.scan`, 11-16, the person: "너무 과하게 은은해서 보이지도 않음": the
gray ramp, no longer the lit frame's vanilla whose khaki steps sank beside it and whose last step stood
an L* of 4 from the still rule; on a dark or unknown face 255 in the middle, a step brighter than the
body text, then 252, 249, 246 and 243, an L* of about 11 apart; on a light one 236, 238, 240, 242, 244), out past one end and back in 2.6 seconds (11-16, the person: "스캐너가 가속도가 붙는 방식이 아니네? 등속으로 오가는거 좀
거슬리네": its place is a cosine of the time, so it slows toward the end it turns at and runs fastest in the middle, like a
pendulum; `RULE_EASE` at the top of `panel-view.cjs` is the one value, 1 the cosine, 0 the steady speed of before), then the dark rule
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
neutral frame under every value. What waits for the person's answer blinks (11-15, the person: "대기 중인건
점멸을 시키자" / "질문만"), whatever the session's state: between two reds, `FACES.*.waitBlink` (217 then
95 on a dark or unknown face, an L* gap of 34; 88 then 167 on a light one, a gap of 28; the person
tunes them on the installed copy), never off and always readable, on Fleet's own one-second beat from
the clock (`render_cells._blink_phase`; `WAIT_BLINK` 10 spinner frames: the lit red through the even
second, the other through the odd one; 11-16, the person on v4.40.0: "점멸은 안하는데" — 174 and 131, an
L* gap of 15, changing every 2.4 seconds on the breath's beat, did not read as a blink) and on the tick
that turns it, which runs folded too and wakes for nothing while no answer waits. It is the red
letters of the question answered here (its first row, its choices' row and `↵ answer · Ctrl+]`), the
`◑` of one answered elsewhere and the guide line's count (`1 QUESTIONS · answer below`, its count in
the wait's red), and the folded strip's `◑답 대기 N`; never the head's state glyph, the section head's
count, the route box, an answer on its way or anything in a popup. A beat writes those cells alone
(the moving frame's cells, a run at a time). Nothing blinks with `rule motion` `off`, in `mono`, under
`NO_COLOR`, in eight colors or on the settings screen. Every value is a constant in one place in `panel-view.cjs`; those
a person tunes stand at its top (the blink's `WAIT_BLINK` 10 frames; the rule's light `RULE_PEAK` 5, `RULE_FADE` 4, `RULE_SCAN` 26
frames, `RULE_REST` 10 frames, the lit frame `BORDER_LIT` 1; the wave `WAVE_COUNT` 3, `WAVE_LAP` 56
frames, `WAVE_STEPS` 10, `WAVE_FLOOR` 0.3, `WAVE_MIN` 24; the arc `BORDER_ARC` 1/4, `BORDER_LAP` 60
frames), the breath and its shade count with the other `BORDER_*`, the colors in `FACES.*.orbit`,
`scan`, `waveRGB`, `breath`, `breathRGB`, the blink's two reds in `FACES.*.waitBlink`. 11-18: while a session works (every `rule motion`
kind) those three tone ramps are its harness's own, `FACES.*.ramp.<harness>` (Claude 173, Codex 147, OpenCode 153 and their lit
tints on the dark face, their deep pairs 166, 25, 31 on the light one): the same number of steps, the same motion and speed; a waiting
session's reds, a window with no known harness (the vanilla ramp), mono and `NO_COLOR` are unchanged. A route's state stays in
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
dim (none for zero or an unknown count), (11-21: the row has no status button any more; the STATUS and BACKLOG parts stand under the REPO group, below, and the bottom layout's first row ends in two buttons),
and `GIT    main ✓`, all dim with no
parentheses: the branch and dirty mark cut at 20 cells (32 with 60 cells
inside), then `✓` when ahead and behind are both known to be 0, else `↑N`/`↓N`
(nothing when unknown: no upstream to compare with), then the existing merge
warnings and `💀merged`; no Git row when nothing is left; no folder or branch
glyph. Directly under `GIT` stands `PEERS` (11-17, the person's PEERS sketch; the label column of
`REPO`/`GIT`): `PEERS  [c7] ⚑  ─ [d4] [3b]`, the supervising session, its flag, two cells, a dash, then the sessions it
supervises, in that order from every window; a supervised window shows only its own group
(`[c7] ⚑  ─ [d4]`), a window that is both shows two groups joined by ` · `. Each number wears its session's
harness color: this window's own the lit color, bold; every other session's the same hue one step back and plain (11-18, "this window's
number should show at a glance": on the dark and unknown faces the USAGE names' own values, `FACES.*.harnessName`; on the light face
`FACES.light.peerOther`, the lighter step, Claude 173, Codex 68, OpenCode 66, so it is never this window's deep hue; each drawn as that
face's fixed color, never the terminal's dim; with no color, weight alone, as before); the flag is Fleet's steward pink, the dash and the dot dim; it shows no
session state. Short of cells the last numbers go from the back and the count of the ones a group lost stands
inside that group (`[3b] ⚑  ─ +2`), never after another group's; a group keeps its head (`[x] ⚑  ─`) while that fits, else the
group goes whole and no count is put on the groups left; a window with no relation has no row. The relation
is the one Fleet already uses (registered steward relations between live sessions of the same repository), handed
over by the shared cache producer as `session_peers` (at most as many keys as the cache keeps, cut at the producer):
no new read and no new file. The numbers are plain text, no
press. The bottom layout carries the row after Git and gives it up with the optional rows. Only `↑N` and `↓N` take a color, Fleet's own (`lvl_g` green, `lvl_r`
red), on the Git row, the bottom layout's head and Claude's statusline alike;
mono and eight colors paint them dim as the other Git words, and `NO_COLOR`
keeps the words with no color. The bottom layout and Claude's statusline keep the repository name
ending in `/` (`hearting/`, the mark only on a whole name) with Git dim in
parentheses (`(main ✓ 🚧 3)`, the same branch cut and `✓`; Claude's statusline
cuts at the width its lines are shown at, its bash fallback by `COLUMNS`),
cut with `…` within that row and absent without a Git reading; Fleet's own Git
words are unchanged. Beside the
native area one empty row then parts the repository, Git and peers from the model
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
while the session works or waits; 11-17: a press on any row of it opens the NOW popup, in the bottom layout the
row it shares with the title; its last row ends in ` ›` only where that row has the room, else the row's end is
`…›`, so the mark never adds a row, folds a title or moves another row), each on as many rows as it needs, then a
tidy notice (11-16, the person: "알림이 알림이 아니라 경고 같아서": a result that ended well and leaves the
person nothing to do, which its writer marks `done`, is one or two dim rows without `⚠`, the machine's
own words left out of the panel's line (the `[tidy]` tag, the batch id, the undo command, a byte or rowid
range) while the line the session receives stays whole; a notice that leaves the person something to do,
a failure, a compaction whose continue could not be typed, or one written without the mark keeps `⚠` in
the caution color on up to three rows; the folded strip by the same rule); one empty row then parts the
head from the sections. The bottom
layout keeps the metadata row (or rows) first and one content row
(`title │ NOW …`, each keeping at least half of what is left).

Two groups (11-20, the person: "REPO에 엮어서 구분을 하나 더 두는게 맞을듯" / "REPO ---- SESSION ----"), beside the native area. REPO is the repository's: the
REPO row, which is the group's head (`REPO   hearting · wt 12 ────`: the word in the label column, the rule dim in a section head's color running to the row's end,
no button; narrower than 30 cells inside the word stands first as `REPO `), and under
it GIT, PEERS, then (11-21, the person: "버튼을 REPO의 아래에 각각을 두고 +? 숫자 띄우고 백로그랑 현황을 나열하자" / "각각 3줄 정도씩 목록으로 만들고 추가분을 +? 형식으로")
the **STATUS part** and the **BACKLOG part**: each a section head (`STATUS +2 ────── ›`, the section heads' own shape, the word in the title role, `+N` in the MESSAGES head's
color and place, `›` last; a press opens that popup) and its first three items, one row each, cut with `…` (`! ` in the status list's yellow before what waits for the person,
`· ` dim before the rest; a press on an item opens the popup standing on that item). The STATUS items are the status list screen's own order, what waits for the person and
then what is in progress (`session_status.panel_items`, from `parse_listing`, the one reading the list screen uses; the recent and the folded count nothing); the BACKLOG items
are `<artifact root>/backlog.md`'s, this window's campaign group (the `##` heading that is, letter for letter, the name of the folder this window's status document is in) first,
then the other groups in the document's order. `+N` is the count of the items of the whole document not shown (nothing for zero; a `+` after it, `+2997+`, where the file is longer than is read: the first 1 MiB is read and what was read is counted; `+` alone where nothing is hidden by the rows and there is more); no item: the head alone; no document, or one that
cannot be read: the word plain dim, no item, a press still opens the popup; no artifact root: neither part. Short of rows (the same `give` order as the other sections, no
budget of its own): the BACKLOG items go first (3, 2, 1, none: `+N` grows by as many), then the order MESSAGES and MEM already had down to one row each, then the STATUS items;
where the three it must not touch would still give a row, the SESSION head and the rule above the title go, then MESSAGES and MEM (rows, then heads), then Git, PEERS and the
two heads together (a head never stands without Git), so NOW, QUESTIONS and the route box keep every row they had without the parts. The bottom layout has no list and no more rows:
its first row ends in `STATUS +N ›  BACKLOG +N ›` (`N` the whole count, nothing for zero; short of cells the counts go, then the words, then the row is cut; the rest of the row opens the
status popup). The two parts' items come with the status button's value, in the same read and the same worker, only where a document changed (the host sends the documents' path,
modification time and size back as `have`; an unchanged one is neither read nor parsed again); the panel's drawing reads no file. SESSION is this session's: its head `SESSION ────` stands where the empty row between the two groups of
metadata stood and goes with it short of rows, then MODEL, CTX, TITLE, NOW, QUESTIONS, the route box, MESSAGES, MEM. RESOURCES and USAGE keep their own heads, place and shape
at the panel's bottom (they read under SESSION; they belong to neither group). The bottom layout has no group heads: its first row carries the repository and the session together (and the two buttons),
then GIT, PEERS, then the title and NOW row and the route box. The REPO row is the group's head in every width; there is no head row of its own over it.

Under the head the panel is a few rows per section (11-15, popup design E), each
a press away from its popup, where the section's own table reads whole: in order
QUESTIONS, the route box, MESSAGES and MEM (beside the native area all of
them; under it QUESTIONS, the route box and MESSAGES). The panel draws no LAB and no SESSIONS
section (11-15, the person: the experiment board and the machine's sessions read in Fleet's own
LAB and session tables, which stay; the panel no longer reads the board or the session table, nor
watches their files). Every other section's head has one form:
its count where it has one, its name tag in dark gray upper case, its counts by
state, a thin rule, and `›` (`3 QUESTIONS ──── ›`,
`MESSAGES +3 ──── ›`, `MEM ──── ›` (11-20: MEM has a popup since); under the native area
`messages` keeps its word). The routes stand in the one thin rounded box, as
v4.36.4 drew it (restored 11-15: the person had asked for the routes to open a
popup, not for the box to go), its title in its top line (`╭─ 2 ROUTE ─╮`,
under the native area `route 2`; 11-17: the title counts the routes alone, `ROUTE` where only requests
stand, and the `PEER REQUEST` word is gone from it, the requests being a zone of the box, below). Nothing opens in place: a press on a
head or on any row of its section opens its popup (11-19, the person: "라우트를 눌렀을때 fleet에서 처럼 스테이지별 단계가
시각화되었으면 한다": any row of a route, its workers' box too, that route's page, Fleet's stage boxes; until its own
reading comes, the stage boxes the panel's values already draw (its top rule ends in the route's form word, `team` or `solo`, as in the route's bracket, never `staged`), and a route with no stage line its panel box; the
box's top line the routes popup, the table; a request row the MESSAGES popup).

- QUESTIONS (11-16, the person: "질문은 이전처럼 그냥 붉은색 블록으로 감싸는게 모양은 제일 나아" / "붉은 바운더리
  말이야"): one thin rounded box like the route box's (v4.33.1's question box, restored: no ground is painted), its
  name in the top line (`╭─ 3 QUESTIONS › ─────╮`, the count in the rose, `QUESTIONS` dim, `›`), the rows below inside
  it laid out at the box's inner width (nothing cut by the border). The line is the rose (`accent`) where this window
  has a question to answer, dim where only an answer on its way or a wait answered elsewhere stands; mono, eight colors
  and `NO_COLOR` tell the two by the line's weight (undimmed against dim). While an answer is wanted here the box's line
  blinks between the two reds of the waiting blink (`FACES.*.waitBlink`, Fleet's one-second beat) and the letters
  inside stand still; a `◑` and a count of a wait answered elsewhere keep their own letter blink. A section with
  nothing to show inside it stays its head row, and short of rows the box's two lines go after the content rows
  (the section then reads as below). A press anywhere on the box opens the question popup. The count is the answer wait's (the `◑답 대기 N` row is merged into
  it, in the question's rose), then the answers sent (one line each, ten minutes from when it was sent),
  the next question's summary (its first row `◑ …` and its choices on one row
  `1 label (recommended) · 2 … · +2`, or its first two rows) and, for a wait
  answered elsewhere, where (`1 QUESTIONS · answer below`). The question a press
  answers here, now, was drawn in rose letters from 11-15 ("그냥 질문은 글자 자체를 붉은 색으로"); on 11-16 the person
  took that back ("G1에서 질문 바운더리만 붉게 하고, 내용은 흰색으로 해"): the red is the box's line alone
  (`accent`, 174 on a dark or unknown face, 131 on a light one), and its words, its `◑` and its choices row
  inside the box are the body color (253 / 237), never dimmed and no weight added, on no ground of their own
  (no fill, no chip, no bar); `↵ answer · Ctrl+]` keeps its rose at the right, the dim parts (the ` · `, the count, a
  recommendation's mark, the answers sent, the guide line) their dim. Folded back to the plain section (short of
  rows) the letters are red and blink as in 11-15. The
  choices row is text, `1 label · 2 label` (the ` · ` and the count `+N` dim; short of cells
  the last choices leave for their count, and where not even the first fits beside the count
  it shows with its name cut by `…`, its number kept, the rest counted: the first choice always
  shows), and under them `↵ answer · Ctrl+]` in rose at the right; a press on any of them opens
  the question popup. Short of rows the action row goes first, then the choices; the first row
  stays to the last. Eight colors, mono and `NO_COLOR` tell a question to answer here from the rest by the box
  line's weight. Nothing else is red but the box's line, a wait answered elsewhere's `◑` and the guide line's
  count (11-15: what waits blinks, above): not an answer on its way, not the guide line's words or
  `질문 변경됨`.
- The route box (spec rules 23-25, 34, 36, 38, 43, 48, 57): the routes the statusline
  counts, then the recent results the producer keeps after them, then the requests
  this session sent (→) or received (◂, `✓` closed) with their status and age, every
  one of them shown (no `+N more`); laid out at the box's inner width. 11-17: the box is one box
  of three zones, each under a named rule drawn inside it (`─ direct ───`, `─ dispatch ───`,
  `─ request ───`, in this order; the names are English in every language; it replaces the one
  `├────┤` rule that parted routes from requests since 11-16). `direct` is what this session does
  itself (its route line has no `(direct)` bracket: `● code  title`), `dispatch` the routes this
  session started (lines as before: owner, stage line, workers), `request` the peer requests it sent
  or received (lines and presses as before). A zone's name and its mark (dot or arrow) have their own
  color: direct orange (208), dispatch violet, request aqua, none a harness color (the harnesses' own are Claude's softer clay orange 173, 13 apart
  by CIEDE2000, its light-face pair 166; Codex's lavender 147, light-face pair 25, the Sonnet family's own step on purpose; OpenCode's pale sky 153, light-face pair 31), a status color of the box (working green,
  idle yellow, failure coral, the spinner's vanilla) or the steward pink (an 8-color terminal has none left over: the zones read in
  the default color and the white there). A
  zone with no rows has no rule, no rows at all is the empty box; a route a request opened stands in
  direct or dispatch like any other. Fleet's session page lists its routes in the same zones and order
  (` ─ direct ─`, ` ─ dispatch ─`, ` ─ request ─`; its section head counts routes alone), has the PEERS
  line directly under its title row, before MODEL (the numbers of other sessions open that session's page;
  the panel's are plain text), keeps its older bottom row (`PEERS  [e4] · [a3]   PEERS tab ›`: the sessions a link joins to
  this one, messages too, and the way to their tab) whole where the window has no supervision and, where it has, leaves off the
  numbers the top line names (the tab link stays on it where the top line has no room for it), and reads a route staged
  into a single stage as the panel does (`code(team·standard) · 1/1: execute`, was `code (execute ●)`);
  Fleet's ROUTES and OVERVIEW tables are unchanged. Short of rows the content rows give way first (the steps
  below) and a zone's rule stays while one row of that zone shows, also in a box that scrolls. Beside the
  native area a route reads, by who set each row (11-17, the person's route row sketch 5):
  its first row is a state mark and the kind of work with its form and intensity (`● code(team·standard)`: the
  kind word is the one bold word of the route, in the color of its field (11-18: `code` 69, `lab` 214,
  `design` 205, `docs` 180, `research` 38 in 256 colors; the dark counterparts 26, 130, 162, 94, 24 on the light face;
  a route with no such field hue keeps the body color), never dim, in mono, eight colors and `NO_COLOR`
  as the bold body word; the intensity stands in brackets in the same hue at its weight (`quick` dim, `standard` plain,
  `strong`, `thorough`, `adversarial` bold; as dim brackets where no hue is shown) and nothing else goes in them; a
  direct route's word stands there when the record names no intensity; the bracket holds two axes (11-19, the person, in order: "code안에 어떤것인지 의미를 알려주는게 좋지 않아?", "code(unit) 이렇게
  쓰는건 이상한가?", "근데 강도랑은 좀 다르잖아. 어쨌든 전체 절차를 다 밟는 정식 라우트냐 아니면 한가지 일만 수행하는 건지", "unit, one-shot,
  staged을 괄호로 넣어야하나 싶은데", "unit도 어떤 모델을 부르냐에 따라서 강도는 정해질 수 있는 거잖아", "강도도 표기해 항상", "unit과 one-shot의 의미가 딱 들으면 잘 구분이 되나? staged도 그 framed랑 함께 잘 표현이 되는 단어인가? 전반적으로 전향적 검토가 필요해", "team도 괜찮은데?"): the FORM, read as who makes the
  work (`unit` one unit called and one thing done, no owner; `solo` the owner alone doing the whole procedure of its kind; `team` the owner and a worker
  for each stage; English in every language, decided once in the backend and carried as `kind_form`; the command's `--shape solo|staged` is the
  `solo` and `team` shown here), and after a `·` the
  INTENSITY, always written for `solo` and `team` (`● code(solo·quick)`, `● lab(team·strong)`); the color and weight of
  the whole bracket are the intensity's (as above). A `unit` route has no intensity on screen (11-20, the person: "unit에는 강도를 표시안하는게 맞겠네": it does one
  thing and has no procedure to weigh, so `quick` was always the same blank value; the record keeps it, only the text leaves it out): `● code(unit)`, its
  bracket the plain weight in the field's hue (dim where the field has no hue), as any bracket that holds no intensity. A record with no intensity reads the form alone, a route with no form (a frame, an older
  record) its intensity alone, an older unit route that records no field its bare `unit`, a `direct` route none, and a unit a worker called no
  bracket. Short of cells the `·intensity` tail goes first, then the bracket as before. The stage line never leads with `staged`
  (`code(team·standard) · 3/6: execute`; the stage box's title `3/6: execute`); the route's time stands at the right end of this
  row, the middle text going first short of cells, then the chain behind and the intensity, the kind word and the time
  staying), the chain of kinds it
  continues leading it (`frame ✓ → code(team·standard)`: what is behind dim with `✓`, this one bold;
  `research ✓ → draft ✓ → code(team·standard)`), then its title two cells in on at most two rows
  (the second ending in `…`), then the owner two cells in (the mark, the doer's recorded role as
  a dim label in one column, then harness, model and effort, no tool: `● owner  claude Opus 5.5 xhigh`; 11-18, the person:
  "CTX는 게이지를 포함해서 아래줄로 내리자. 그리고 NOW를 두줄까지는 허용치를 늘려주고 (owner, worker 둘다)": under it, four
  cells in, its `CTX ━━━━━───── 41%` row (the gauge at the panel's width and its number, then ` · 12m` only where the time is that
  session's own: a worker's, never the owner's, whose time is the route's and stands at the end of the route's first row; short of
  cells the gauge narrows to the number, then the time, then the word `CTX` go), under that the session's NOW on at most two rows
  four cells in (the value Fleet's session rows carry for that session, handed over by the cache producer from the job it already
  read: no read of its own; broken between words, a word wider than a row by cells so no row is wider than the box; the second row
  ends in `…` where it runs over; none, no row, where the session has none; a session whose values are not known leads its `CTX`
  row with its mark), and, for a route with a stage list, the stage line (`staged 3/7: execute`,
  `map-reduce 2/3: analyze`: the route's own shape word, none when the record names none, then the
  position; a failed stage keeps `✕`, a done one `✓`; with no stage running the next stage and
  what it waits for). A route whose owner is not known keeps its `CTX` row; its time is on the first row.
  The owner's mark spins while it works alone and is a dim `●` while its workers run. A route its owner
  does alone has no stage line. An owner's live workers (`workers_live`) stand under it, each as a session of its own
  (its first row with its mark, `worker`, harness, model and effort; its `CTX ━━──────── 22% · 12m` row; its NOW on up to two
  rows; the stage name and the open tool are not drawn: the stage line has the stage, 11-16's stage color is that line's).
  11-18, the person: "worker 부를떄 stage ~ 부분을 블록의 상단에 그려서 그냥 worker는 박스에 넣어도 되겠는데?" / "박스 여부는 내가
  골라가면서 볼 수 있게 그냥 옵션에 좀 넣어줘": the setting `worker box` (`closed`, the default; `open`; `none`, in the settings editor
  and `session-shell.json` as `worker_box`, previewed at once and saved with Apply) puts the workers of the route's stage in one box
  under the owner, two cells in, the stage text (`staged 3/6: execute`, the current stage's name blinking as it did) on its top
  line and no stage row apart: `closed` has four dim borders (the right one in one column whatever the text's widths), `open`
  only the top line and the left side, `none` the stage row and the workers hung by `↳` four cells in (their `CTX` and NOW eight
  cells in), as before. A route with no workers has no box: its stage text is on its first line (`· 3/6: execute`) where the name has room there, and where it does not (the default width among them) its stage row `staged 3/6: execute` stands under the owner (11-18, round 5); a frame route's legs have none either. Where the closed
  box's text would be under 24 cells wide it is the open one; short of rows its closing line goes first. A frame route has no owner yet: `● frame → ??`, its two legs (`frame`,
  `frame-alt`) hung straight under it two cells in, and, once both are done and the direction
  is asked for, `◑ frame → ?? · waiting for direction` (not blinking); the person's choice
  continues the same route in the same place (`frame ✓ → code(standard)`), no new route stands
  beside it. Exactly one thing blinks per route, on Fleet's beat: the current stage's name while a
  stage line stands (so no `●` follows it), else the first row's mark. The role column is the
  route's own width. Short of cells the values go from the back, whole: the chain behind, then the
  intensity (`● code`), the stage line's word `staged` (`3/7: execute`), effort, the
  harness; the role and the model stay. Short of rows, in this order: the closed box's closing line, each NOW's second row, each
  `CTX` row (its number joins the session's first row at the right edge, the one-row fold), each NOW's first row, then the stage line
  goes (the workers' count, `· worker 2`, ends it), then the workers go, then every route takes one row.
  A request (11-16, the person: "양식을 맞추자. [6c] claude opus 5.5 xhigh 이런식으로") reads as a
  route's doer: beside the native area its first row is its mark (→ sent, ◂ received, `✓`/`✕`
  closed), the other session's Fleet number, then that session's harness, model and effort in
  a route row's colors (`◂ [6c] claude Opus 5.5 xhigh`; the model and effort only as far as the
  session values Fleet's shared producer already read for the session displays know them, so
  `[6c] claude` where they are unknown, and a session that is gone keeps its name alone; 11-18, the person: "세션 id 외에도 repo
  위치까지 request는 표시를 해주는게 좋겠네": the other session's repository name, dim, right after its number
  (`[c2] other-repo`), the one Fleet puts on the session, not drawn for a session of this window's own repository or where it is not
  known; it stays after the harness, model, effort and age have gone, the title going before it), with
  the age at the right edge where a route keeps its time; two cells in, the title (`…` where it
  goes on) where a route's title stands; then the status, dim, where a route's kind row stands
  (`open`, `partial answer`, a sent one not yet seen `sent`/`delivered`, or `awaiting delivery` and why while its
  message has not gone in (11-17, `peer-steward show`'s words), a rejected or cancelled one in the failure color). Short of rows the status joins the title's row when the kind rows
  go (`title · open`), and the request takes the one-row form when every route does: mark,
  number, harness, model, effort, title, ` · ` status, the age at the right edge. Short of
  cells the age goes first, then the effort, then the model; then the title is cut with `…`
  (past that the harness goes, then the title); the other session and the status stay. Under
  the native area and in the MESSAGES popup's head a request takes that one-row form (one
  drawing, `requestForms`). Every open request is listed; a closed one stays 15 minutes after
  it closed, sent or received (11-16, the person: "패널에 요청 받은 기록이 안뜨네?": the receiving
  window shows it answered, `✓`, dim); a sent one also until its last answer or cancel notice
  has been seen (at most 7 days), and then only `show` lists it; a request whose message is
  not confirmed yet stays with `not confirmed`. The
  bottom layout keeps one row per route: title, harness·model·effort, context, then the
  kind (`code(standard) · staged 3/7: execute`, a failed one `code(standard) · staged 2/3:
  execute ✕`, `research(standard) · map-reduce 2/3: analyze`, a route with no stage list `lab(quick)`), then the open tool, and the elapsed
  time at the right edge (short of cells the kind word goes, then the tool, then the word
  `staged`). The box's line is the route line's color (the
  failure color while a current route has failed), its count pulses while a route
  runs, and no empty row parts two routes; with no rows yet the producer's own label
  stands over a `Checking` row, and a known empty list draws no box. The `route line`
  setting picks what a route keeps in its one-row form short of cells: `time` the
  elapsed time, `model` its context.
- MESSAGES: the recent messages, two rows each as v4.36.4 drew them (spec rule 31, restored 11-16, the
  person: "메시지 형식은 퇴행했네? 최대 5개 정도로 늘리고, 그전에 원래 형식으로 돌려"): the head (the arrow in
  its direction's color, the other session's number, then dim harness, time, kind and a sent one's
  delivery state, which for a message that has not gone in is `awaiting delivery · why` (11-17): the first word stays whole
  and the why goes on as far as the row reaches, ending in `…`; short of cells kind, harness and time go first) and, two cells in, the text's first
  line in the body color (`…` where it goes on); three at most (`MESSAGES_MAX`; five until 11-20, the person: "대신 MESSAGE도 최대 3개
  정도로 다시 줄이고"), `+N` counts the rest. A message whose
  other end is this session itself (a session-tidy notice to its own seat; 11-16, the person: "메시지에 스스로 보내는
  메시지가 잡히는건 좀 불필요하지 않나?") is no message between two sessions: the MESSAGES rows, their popup, the folded
  strip's count and Fleet's session page leave it out (`peer_message_rows`, the one place they read them; Fleet's links
  already need two different ends), and the tidy line still says it. The requests stand once, in the route box (above); the
  popup reads both whole, the requests first (a request's numbers, reports and
  sealed body; a markdown table in the body drawn as aligned columns by Fleet's one table rule,
  `transcript.table_parts`, its other lines wrapped as before). A received message comes as one line
  (the backend joins its text), so it has no table. A received peer message is listed whatever carried it in (its own
  prompt, a hook comment added to another prompt, a tool boundary, a native queue): the receive path keeps its one
  display line in `peer-received/<key>.json` beside the prompt frontier (11-17), and runtime notices keep their own
  eight places, so a run of completion notices never pushes it out.
- MEM: the newest saved memory records of this repository and the global ones
  together (11-15), newest first, three at most (`MEM_MAX`, five until 11-18, the person: "MEM는 최대 최근 3개만 띄워도 되겠다"), from the journal Fleet
  reads (the newest `add`/`supersede` rows of its project, and the global ones of the same read). 11-20 (the person: "메모리 내용은 한줄로 바꾸고 (지금은 두줄이네)
  메시지 처럼 팝업이 가능하게"): one row a record, as a message's one-row form (spec rule 46 (b) drew two rows from 11-16): its mark (`◆` durable,
  `◇` working, `·` for a record naming no tier), its age and its kind dim, then the stored headline (else the start of its snippet) in the body
  color, `…` where it goes on (`◆ 10m decision headline`; short of cells the kind goes first, where the headline would be left under 14 cells; the
  mark and the age stay). The head is `MEM ──── ›`: a press on it, or on a record (`popup:MEM:<n>`), opens the MEM popup. Short of rows MEM shows two
  records, then one, after the messages took the one-row form; no section without a journal row.
  The MEM popup is the MESSAGES popup's frame, keys and press rules (the table popup: `←→` section, PgUp/PgDn, Esc), its rows drawn from the records the
  panel already holds (`memory_items`, `panelView.memoryBody`; no reading, no new kind of popup): each record its dim head (`◆ project · decision · 10m`:
  mark, scope, kind, age), its headline whole in the body color, and its body under it, dim: the snippet the journal keeps (`session_panel.MEMORY_BODY` =
  600 characters, one line of printable text; nothing where it is the headline itself or a `superseded_by=` pointer; a body that goes on from the headline
  is read once, whole). The record pressed is the first on screen where the popup is longer than its rows. Nothing else is drawn: no session record, no
  other project's text. The popup's name stands on the bottom line after MESSAGES.

- USAGE (spec rule 59, 11-16, the person: "그 라우트는 다른 하네스도 쓰니까 usage에 3가지를 전부 띄우는게 좋겠어" / "구분줄을
  USAGE로 해서 넣자"): the section at the panel's bottom, its name row `USAGE ────` (as MESSAGES' and MEM's, no `›`: it
  opens nothing; the thin rule that parted MEM from the last row became it), then one row per harness in Fleet's header
  order (Claude, Codex, OpenCode): the harness's name in its Fleet hue one step down (11-16, "하네스 이름도 어둡게 원래 테마 컬러를": claude 130, codex 104, opencode
  110 on a dark or unknown face, 166, 68, 31 on a light one, `FACES.*.harnessName`; none in mono, eight colors or `NO_COLOR`), then the windows that harness reports as the last row showed
  them (`5h 12%(↻ 2h) │ 7d 66%`, Fleet's level colors; a window not reported is left out; a harness with none reported reads a
  dim `?`). The values are the usage records Fleet already keeps (`~/.local/state/agent-fleet/usage/<harness>.json`, read
  by `usage_cache`; this window's own harness keeps the values its panel observed, a Claude callback included); nothing is
  fetched, and a change to any of the three records redraws the section where the 5h/7d row was redrawn. The last row
  carries the gear and the fold button at its right end; the last harness's row stands there while it fits beside them,
  else it keeps the row above and the buttons stand alone. Every harness window shows the same three rows. A row
  gives way short of cells in the last row's order (the reset times, 5h, the model windows from the last, 7d), each value
  whole. Under the native area it stands as beside it, as rows are left. The folded strip is unchanged.
- RESOURCES (11-18, the person: "패널 제일 하단에 RESOURCES도 띄우자. USAGE 위에 그리고 어떤 세션이 잡고 있는지만 추가로 표시하고"): beside the
  native area, the section right above USAGE, its name row `RESOURCES ────` (no `›`: it opens nothing), then for each host (this
  machine first, then the configured hosts, eight at most) one row, its name and `CPU 34%  MEM 61%`, and under it every GPU of the host in its number's order side by side (`  GPU0 92% 21/24G [3b]  GPU1 0% 0/24G  GPU3 58%
  14/24G [c7]`: no gauge of the use since 11-18 ("게이지는 없애자 ... 가로로 나열"); the use is its number, in Fleet's level color (green, yellow from 50, red
  from 80); the memory; and the Fleet numbers of the sessions that hold it, empty where the producer does not know them; a GPU nothing runs on
  is the same group with the sample's own values and no session number, the whole of it `dim`, the use too and not in a level's color, since 11-19:
  "빈 GPU도 일관성있게 표시해줘. 그냥 0% 0/48GB 이런식으로 해놔. 컬러만 좀 어둡게 하면 되잖아. 0%에"; the host row has no count of them any more), two cells in and two cells
  apart, one grid for the whole section since 11-19 ("그리고 GPU 표시를 행과 열 간격을 맞춰."): every group of every host the same width (the label left-aligned to the longest label, the use and the memory right-aligned to the widest, the session numbers left-aligned after them and blank where a GPU has none, the last group of a row not padded behind), the same number of groups on every row, one form for all groups (memory, then use go together), and the host rows aligned the same way (the name column as wide as the longest name, each stat's value right-aligned; MEM then CPU are given up for the section before a name is cut), onto the next row where they do not fit and never splitting one GPU's group; a host that could not be reached keeps its name alone, a host with no
  GPU is its one row. A memory gauge stands right before the memory number since 11-19 (the person, on v4.59.6's panel: "가로로 길어지면 RESOURCE에
  게이지도 넣어주면 좋고"; `GPU0  93% ━━━━━━━━─ 21/24G [3b]`) and only where it costs no row: the rows come first, the gauge is what the width leaves. It is
  Fleet's VRAM gauge (the memory ratio's fill by `render_gpu._gauge_segs`, the level color by `_resource_level_key`; a GPU nothing runs on has all
  of it `dim`, an unknown memory is Fleet's empty track), drawn by the producing side at each width 4 to 10 (`gauge4` … `gauge10`, as a route's
  context gauge is) and picked, never refilled, by the panel; the same width for every group of the section, the widest 4 to 10 the room allows.
  It stands only where the rows are as many as without it and it has 4 cells at least; the groups to a row may be fewer than they would be (a
  width for three where every host has two GPUs gives each row two and the rest to the gauge; the count that gives the widest gauge with as
  many rows wins). Not in the forms without the memory, not on the host rows; where it cannot stand the section is as it was without it. Short of cells a GPU's group gives up its memory, then its use, and keeps the session number (every group takes the
  form that lets the widest stand alone); a host row gives up `MEM`, then `CPU`; if that is still short the name is cut with `…` (to 12 cells at least). The values are Fleet's own
  compute-host sample (`compute_hosts.collect`, the sample its RESOURCES view draws): a probe costs the servers an SSH read each, so the
  resident producer takes it only while a side panel that draws the section is open (the host's projection request carries `side`;
  `panel_projection` then touches `.resources-wanted`, at most every 20 seconds, and the producer reads no host once it is 90 seconds old),
  at most once in 60 seconds however many windows are open, in one short-lived thread its pass never waits for, and not at all while Fleet
  is open (Fleet's own ten-second pump leaves each sample in `resource-sample.json` beside the cache, and a sample younger than the period,
  whoever took it, stands in for a probe). It keeps the last result in the cache (`resources`, bounded:
  eight hosts, sixteen GPUs each); the panel reads that cache only, never a host, so what it shows is at most about two minutes old (the
  sample's period, the pass that follows it, the probe's own time) and is not drawn once it is older than two and a half minutes. A window with
  no producer, no configured host and no local sample has no section. The existing `MEM` section is the saved memory records, not
  the machine's memory, so nothing is merged with it. Short of rows it gives way before any other part of the panel (its GPU rows,
  then its host rows, the name row with the last of them), so the sections above and USAGE stand as they do without it.
- The window's last line (spec rule 60, 11-16, the person: "맨 아래줄이 비면이 아니고 항상 비우라고 그냥" / "디렉토리 경로가 뜨던데,
  그걸 모든 셸에서 통일해도 좋겠네. 대신 hearting version은 우측으로 붙이고"): in every harness the row at the bottom of the
  native screen where the harness drew its own line (Claude's statusline rows, Codex's footer row with its thread id and
  fields, OpenCode's bottom bar with its folder, context and cost) is the shell's one line, always: the working folder
  at the left (the home folder as `~`, cut from the front with `…`), a dim `hearting v<release>` at the right end (the
  release folder this window runs from; a newer installed release waiting puts its words, `새 판 대기` /
  `New release waiting`, in its place). What the harness drew there is not shown; the shell paints over the row in its
  painted copy only and keeps reading the native screen (Codex's thread id, OpenCode's context) as before, and Codex's
  `status_line` setting is left as it is. OpenCode has no setting or plugin slot that hides its bottom bar (checked in
  the installed `@opencode-ai/plugin` types and the documentation), so the shell's own paint is the way. Only what the line
  alone showed moves: OpenCode's cost (from the session record Fleet reads) stands after the context in that window's
  panel (`CTX ━━━━──── 41% │ $1.23`). A window started without the shell is untouched. The row is found only in the
  harness's own layout, and what is not found is not covered (review of #1662): Codex's footer is the status row and the help
  row (or the one thread-id status row) after the composer's last `›` row, the status row opening with a word, a number or a
  path, not a sign (`↳ queued`, `⏎ to send`, `■ stream error`) and not a draft line that begins with an id; OpenCode's bar is the
  row with `ctrl+p commands` and the folder row right above it, never a `/`- or `~`-led row without that bar.

The panel paints no clock and no last-input row. A value that was not
observed is left out, never painted as `?` or an estimate. When rows run short they give way in this order:
RESOURCES (11-18: its GPU rows, then its host rows and name; the idle count goes with its host row); empty rows and a value's further rows
(title, NOW, a route's title); then (11-20, the person: "MESSAGE, MEM은 좀 더 세로길이 따라서 과감하게 표시 줄을 줄여버려 1~2줄 수준으로" and "NOW·QUESTIONS·ROUTE가
MESSAGES·MEM보다 먼저 자리를 받게") MESSAGES and MEM go down before any other section gives a row, in this order: MEM's
every message takes the one-row form (`← [e7] text`); MEM shows two records;
MESSAGES two messages; MEM one record; MESSAGES one message (`+N` on its head counts the rest): from six rows (a record is one row since 11-20) down to two rows, then one;
then the question's action row, its summary's second row and the guidance line;
the structural rows (the empty row inside the metadata, the head's rule; the question box's two border lines); then the
route box's own steps (11-17: its zone rules are not among them, they stay while a row of the zone shows), every
route at the same step (v4.36.4, spec rules 34 (b), 38,
48 (c); 11-18): the workers' closed box becomes the open one, each NOW's second row goes, each `CTX` row goes (its number joins the first row at the right
edge), each NOW's first row goes, the workers take one row each, the workers go (the kind row ends with
`· worker N`), the kind rows go, every route takes one row; then the box shows fewer
routes and scrolls in place (the wheel over it), down to one, its border counting
what it hides (`↑2`, `↓3`; it takes the rows of its tallest window, so the wheel moves
nothing else); then Git; then MEM gives its row, then its head, then (11-16) the USAGE section's rows of the
harnesses that are not this window's (the last first) and its name row, then MESSAGES gives its row and its
head; then the route box (11-20: after MESSAGES and MEM; v4.36.4 had it before the
running jobs and them before MESSAGES); the running jobs; the question's head and first row stay to the last (its box's two border lines go after its content
rows, before the structural rows below them); this window's own usage row last of all. Rows a later step frees beyond its need go back to the steps
before it, the most important first. A section is placed whole or not at all; the
rows left over stand above MESSAGES and MEM (which keep to the bottom, with their
press targets), or under the last section when there are none.
Groups are divided by `│`, stages joined by `›`. Fixed words are English
(`context`, `route`, `messages`, stage names, `5h`/`7d`, the section names
`QUESTIONS`, `ROUTE`, `MESSAGES`, `MEM` with their counts
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
Physical exceptions are explicit: the native scroll gutter (and, in a window the shell does not host, each harness's own
last line; inside the shell it is the shell's line, spec rule 60).
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
under the native area `route 4`; routes alone, never the requests, 11-17; the producer's own label alone, with Fleet's
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
own ledger shard and its 200-character summary), two rows each (the head and the text's first
line, below); the MESSAGES popup reads each with its head (the
arrow in its direction's color, the other session's number, then dim harness,
`HH:MM` today or `MM-DD`, the record's kind and a sent one's delivery state:
`delivered`/`sent` dim, `not delivered` (the recipient was never resolved; no age) in the caution color,
`failed` red; a sent one whose pending row is still `pending` reads `awaiting delivery` and why, `peer-steward show`'s
words, whole in the popup, on a row of its own where the head's row is too short) and, two cells in, the text in the body color from its first
meaningful words (leading bracket frames and heading marks left out, nothing
else changed), the newest first; the panel draws the same head and that text's first line. Five
show in the panel at most, however many rows are left (`MESSAGES +2` counts the rest); short of
rows they take the one-row form, then fewer show from the
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
two-cell column for the mark. 11-17 (the person: "질문이랑 선지가 잘 구분되게" and "구분선 위에 여백도 하나 두고"): a question with choices ends its words, then one empty row, then a thin line in the popup frame's own color (dim where the frame is bright), then its choices, each number bold in the question's rose (the `◑` mark's color) with two cells before the words; a question answered in words has neither the line nor the empty row, and short of rows the empty row goes first and the line stays. The panel's choices row (`1 label · 2 label`) wears the same rose on its numbers, and Fleet's unfolded question the same empty row, line and numbers. In the question popup ↑↓ move the chosen
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
(`time`, the default, or `model`), `worker box` (11-18: `closed`, the default, `open` or `none`: how the ROUTE box draws a
stage's workers, above; the person asked for it by name) and `rule motion` (`rule`, the default, `border`,
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
their colors (Claude's own statusline row, where the panel is not painted, takes the theme, not the shade). An unknown position, theme or shade falls back to its own
default without disabling the shell, and so does an unknown `route line`, `worker box` or
`rule motion`. On a shade a kind word's bracket keeps its field's hue and its weight step (11-18).
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
popup opens; the popup reads it whole (its body, a markdown table in it drawn as aligned
columns by Fleet's one table rule, every choice, a text or secret answer, `i/N` with Tab). An empty questions section reserves no space. Retain choices and drafts
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
`questions list` head, the status line, the panel's `◑ N` and the folded strip) counts every wait a person must answer, wherever
it is answered: a question the panel answers, a Claude native request, a marker
question and a human gate. Only a blocked worker's wait (its parent agent's) and
a finished one (closed, replaced, expired, an answer on its way) are not counted.
The questions section holds what the person can answer here now (answerable
ones first; `Ctrl+]` opens the popup on the first of them); a counted wait answered in another
place is one dim guidance line beside the count saying where
(`1 QUESTIONS · answer below`), and the question box names `Ctrl+]` only when the
panel answers one. A blocked worker's wait is in no count or question block (the
route row shows the route blocked), and it closes once the route closes or its
parent holds the worker's exact acknowledged completion receipt. Once the person
sends an answer it has left their hands: while it travels (the existing answer
record's `submitted`/`received`) the popup closes and the question is one line
(`✓ answer sent · <what was sent>`, which stands for rule 53's chosen choice;
`✓ answer sent` in the passing green of a route's `✓`, the `·` dim, what was sent
in plain text, a secret `•••`),
`Ctrl+]` no longer stands on it, and every count above leaves it out (Fleet lists
it as sent); the line stays ten minutes from when it was sent (11-16, the person: "답 보냄도 한
5~10분은 유지하도록 해두자"; `SENT_KEEP_MS` in `block-input.cjs`), its session having the answer or not,
and goes at the first update or paint after that (no timer of its own), and a failed
delivery brings the question back with its existing word. Until its session takes
it, the line says so: `✓ answer sent · <what was sent> · awaiting delivery`, and,
when its session rests (Fleet's idle, unused or stale) and only the person can let it in,
why: a window whose shell runs an older release than its launch path names
(`New release waiting`, the window's own line) or a Claude window outside the shell
takes it when that window is reopened or typed in once. A session still working takes it
when its turn ends, and one whose state is not known gives no reason either.
A message or request still on its way has the same kind of reason on the sender's side: each
courier pass keeps on its pending row (`held`) the word the receiving host gave for keeping it
(`busy`, `form`, `draft`, `paste`, `unsettled`, `unobserved`) or that the window is outside the
shell or on an older release, and `peer-steward show <request>` and the one-hour notice to the
sender say `awaiting delivery` with it (`peer_request.delivery_word`). The panel's MESSAGES sent row and the route box's
request line say the same (11-17): the backend reads the pending row of each visible sent message once (at most eight, no
collection of its own, the file watched) and words it with `peer.delivery.waiting` and `peer_request.hold_words`; Fleet's
session page and the NOW popup's message rows read the same function. A row from before the reason was kept reads the word
alone; a message that went in, was seen or failed reads as before.
Fleet's QUESTIONS row and the panel's line draw the same parts from the same function
(`session_block.sent_answer_line`): a secret answer `•••`, no control or direction
characters; nothing offers to send it again. Answering returns
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
the folded strip opens that section's popup, and a new question or a failed
route never opens one. The popups are seven
kinds over one frame and one set of keys: STATUS (11-15, design 2, 11-21: the STATUS head under the REPO group; the table
popup's frame, width and keys; its title `STATUS <campaign> · <age>`, `STATUS` alone without a document.
11-21 (the person chose design C, "정할 것 먼저"): for the campaign document Hearting generates (its first line is the marker
`<!-- hearting:campaign-document v1 -->`, read whole) the popup opens on a **list screen**, of the document's own words and nothing
else (`session_status.popup_doc`; the host lays it out, `status-list.cjs`): the head (title, `updated MM-DD HH:MM` — how long ago is the
title band's, said once — and the one line folded to two rows; a document older than Fleet's own limit of three days adds `! … old document`
to that line, a time that cannot be read says so), then `기다리는 것` (every bullet, each marked `!`, the group's other lines dim under them)
and `하는 중` (the first three, then `all N ›` for the rest; `all N` opens every one), the two named by `parse_groups`' own word reading, then
every other heading as one folded line `› <heading> <count>` in the document's order (a heading the screen does not know is one of them), and
`full document ›`. Only the first heading of each of the two kinds comes first, as in `parse_groups`: a second `기다리는 것` is one of the folded
groups. A group the document has not is not drawn; a waiting list of zero is its heading with `0` and the document's own note. Nothing is counted
off: every bullet of a group is on the screen. A list that alone is more than an answer may weigh (`POPUP_ANSWER_BYTES`) is left out, and the popup
then draws the page as it did before this. The keys that choose a line are short ids (a digest of the heading, of the words, an order number for
the same words twice), never the words, so a heading of any length, or the same heading twice, is chosen and opened as the one it is.
↑↓ and Tab (Shift+Tab back) choose a line (`> ` and the chosen ground tell it), Enter or a press opens it: a folded heading opens in place
(`▾`; again folds it), `all N ›` opens the whole running list, an item whose row was cut with `…` opens in that row's place as its whole words
folded to the width (`> !` on the first row, the following rows in the words' own column; again makes it one row), an item that fits its row has
nothing to open and Enter changes nothing, and `full document ›` is the screen the popup had before this (Fleet's status lines, then the whole
document: tables, fences and the lines outside any list are read there), Esc or Enter going back to the list on the line it left and a second Esc
closing; its one row of keys is the popups' own, which say Esc once. ←→ are still the sections and `p` `f` `r` `w`,
Esc, Ctrl+], `✕` and a press outside are unchanged; PgUp/PgDn and the wheel read a longer list on. A document read again while the popup
stands keeps the chosen line on the same words (else the top) and opens the same groups by their heading (headings of the same name are joined in order: the Nth of them is the one the chosen line and the open state follow). The first screen is the list alone
(`part: list`) and asks nothing of the document's rows or of Fleet: the document read whole is its own answer (`part: doc`, the rows and budget
it always had, whatever the list weighs), asked for when `full document ›` is opened and kept for the next entry, with Fleet's lines after it; a
page read again while that screen stands asks for both again, as the popup always did. Every other document (a hand-written
page, none, one that cannot be read, one cut at 64 KiB) draws the rows it always drew, in the one answer: Fleet's status lines for the project as
its project page draws them, an empty row, then the whole document's first 64 KiB as Fleet's cards, fences as written and markdown tables
aligned, `session_status.popup_rows`; without a document Fleet's `fleet.repo.no_status` and where it would
stand. The words the screen puts in (`updated`, `all N`, `full document`, the keys) are English and come the way the panel's do
(`ui_text`, the panel labels); the document's own are not changed. `utilities/status_popup_picture.py` draws these screens as text from one
document in an isolated environment (its widths are a plain rule, not the terminal's tables). The value, `session_status.read`, is read in its own worker at the first projection, when this
session's route list changes and at an event of the existing file watch on the paths it names, never
per paint, and the rows it read stand until such an event or another width; a read that failed or ran out its time is asked
again at the next projection event, one worker at a time. The popup reads in two parts: the document at once, from the value the button
already read (`session_status.held`, a path only inside the folder's own artifact root; for the generated page the first answer is its list screen
alone and the document's rows are asked for apart, when `full document ›` is opened), then Fleet's lines, which stand above it when
they arrive (for a page with a list screen, after the document read whole is asked for); a popup read that failed shows Fleet's `fleet.output.unreadable`, dim, and is read again at the next opening (a document it
cannot read is that same failure, not a `?`), and the Fleet part holds no place of the document's: a popup opened again while it is out
stands at once, and its lines are kept only for the width and value they were asked at), BACKLOG (11-21: the status popup's list screen over
`<artifact root>/backlog.md`, drawn by the same `status-list.cjs` with the same keys and the same frame; its answer is the status popup's own (`part: list` / `doc`, no Fleet part;
`session_status.backlog_view`, `session_shell._status_popup(kind="backlog")`). The document's form (written by another task, only read here): a title, `갱신 YYYY-MM-DD HH:MM`, then `##` headings, each a campaign folder name or `Unassigned`, and under
each the top-level `- ` bullets, one item each (the artifact entrance's own counting rule, `parse_backlog`, checked against `artifact_entrance._backlog` on the same texts, not the status page's looser reading: a heading is `##` at the very start of
the line followed by a blank or the end of it (an indented `##` and `###` are none); a `- ` before the first `##` is none; `*`, `+`, `-<tab>` and numbered lists are none; an indented line and a sub-bullet are none;
`- - -` and a `- ` inside an HTML comment are counted; a code fence (up to three spaces in, whatever follows) is not read, closes on the same character and the same or a longer length only, and one never closed runs to
the end; `갱신 YYYY-MM-DD HH:MM` (ASCII digits, one blank, the line's own surrounding blanks cut) is read before the first `##` only, and only where the date and the time are real, shown as `updated MM-DD HH:MM`, with `! … old document` after three days). The indented lines under an item
(`date:`, `source:`, `note:`) are its detail. The list screen is the title, `updated …`, this window's campaign group open on top (its heading and its items), every other group one folded line
`› <heading> N` in the document's order, then `full document ›` (the document read whole, its first 64 KiB, `… 64 KiB` after a longer one). **The list answer carries titles only** (and the groups and
counts): an item with indented lines under it is `lazy`, and opening it (Enter or a press; again closes) asks for its lines (`part: detail`, `open` the item's key; one `⠏` stands under it until they come), so a
backlog of 256 KiB and 500 items stands as a list. The list's answer may weigh 200 KiB (`BACKLOG_ANSWER_BYTES`; the host reads 256 KiB of an answer); a document whose titles alone pass it draws
the most that fit, then one line `… N more in backlog.md`; a file longer than the 1 MiB read limit adds `… the file is longer than is read here: more items follow`. PgUp/PgDn on a list screen (the status
popup's too) move the chosen line a screen on, the window following it. The headings are the document's words as written, never invented. The words Hearting puts there (`BACKLOG`, `updated`, `old document`, `full document`,
`No backlog document yet.`) are English whatever the language. No document, a folder or link of that name, one that cannot be read: the popup's one dim line, no list.
A document of another form (no headings, no `- ` bullets) shows as much as is read and no item is none. The title is `BACKLOG · <age>`.),
QUESTIONS (the question editor),
ROUTE (Fleet's own table: the backend's `popup` reading draws
Fleet's `draw_routes` with one route unfolded and Fleet's route page into a grid of
the popup's width and sends the painted cells, Fleet's row tints left out; narrow,
Fleet's columns leave as Fleet leaves them), NOW (11-17, below), MESSAGES (the requests and messages whole, then
the peer delivery reading `r`) and MEM (11-20: the records whole, drawn from the panel's own values as MESSAGES' are, no reading). The bottom line lists the sections with
something in them in the panel's order, `STATUS · BACKLOG · NOW · QUESTIONS · ROUTE · MESSAGES · MEM` (STATUS and BACKLOG in English like the rest,
there while the parts stand; NOW while the panel holds a NOW; `Ctrl+]` still opens what it opened before, never NOW), the current one bright; ←→ (or a press on a name) moves to
the next or previous one. A popup row reaches the terminal with every control character (C0, DEL and
C1) as a space. In a table ↑↓, Tab and Shift+Tab unfold the next or
previous route (Fleet's `▶`) or read on a row; Enter opens the
unfolded route's page (Fleet's route page: what is to do, the verification lines
with the route's paths, the stage frame) and again goes back to the table; PgUp/PgDn and the wheel read a longer table on (its place,
`3/20`, leads the keys row; ↑↓ and Tab keep the unfolded row in view, and a table longer than the rows the popup carries, or than the answer the host reads, shows the rows that fit and ends with one dim line for the rest in the entrance's words, `외 N개`); `p` folds or opens the panel (the popup closes so
the panel is seen), `r` the messages, `w` the open work, `f` Fleet; the keys row
names them, broken between its pieces (` · `) where the popup is narrow, never inside a word
(the question popup's keys and the table and MESSAGES popups' keys are one rule, `keyRows`; the
closing key `Esc/Ctrl+]` stays to the last, and short of rows the pieces before it go first from
the end backward). A standing
table is read again at the events that change it, never on a timer: the routes
when this session's route rows change (the shared answer-wait cache's existing
watch); one reading at a time, in its own worker (a
person's read never takes the answer's action slot; a read past its 12 seconds ends then, an answer's does not).
NOW popup (11-17): this session's story, read in its own worker when the popup opens (never per paint, no
poller, no new file read by the projection): the backend's `popup` reading of kind `now` draws only what
`tools/fleet/explorer/timeline.py` gives (`rows`, `fold`, `passage`; `scroll_steps` is not called) in the
panel's own style keys and sends the painted cells with a note per row. It opens at once, with the NOW the panel
holds on its own row until the list arrives. The list is oldest first and opens on the current NOW, the bottom
row; a line is the time (5 cells), two cells, the mark and the text: a prompt `>` in its harness color (`hb_*`)
and bright text, `> 답 ·` for a prompt that answered a question; a NOW two cells further in, dim, the current one
bright with `지금` at the right; a route `⠏` start, `✓` or `✕` end with its time, a question `◑`, a message `←`
`→`, each in the mark and color the panel already uses; a date row `오늘 11-16 ───` and a dim mark for a clear,
compaction or handover (`●` `○` `◆`). A line's text is the layer's own masked, bounded one; a text too long for
the popup's inside is cut with `…` and the time, mark and `지금` stay. A list taller than the popup's rows is folded
by `timeline.fold` (each prompt its last NOW and `+N 접음`, the prompt stood on open, date and divider rows
never folded; the list is read again as the row stood on moves, by the one backend the popup holds while it
stands: it ends with the popup, another section or the window, and a read it cannot answer is made the one-shot way,
so a key starts no process; keys that come while a folded list is read again wait for it and step over it in order);
one that fits is whole; no record is one dim line. The event marks (`route`, the screen's word for a question,
a message's kind) are filled to the widest one so the text of every event row starts in one column. An answer
past the host's 60 KB leaves its oldest rows off at a group's start (a date, a divider or a prompt) and says so on
its first row (`… 이전 N줄`, dim). ↑↓ move the row stood on (the chosen tint, reversed where there is no hue), PgUp/PgDn a page of rows;
Enter on a prompt or NOW row reads that part of the conversation in the popup (`timeline.passage`, drawn as the
harness draws it; a part that cannot be read is one dim line in words from the layer's reason) and Enter again is
back on that row (keys `Enter 목록으로 돌아가기`); on a route row Enter opens that route's page (11-19), on a
question or message row that section's popup. No key or press goes to the native window and the window never
moves. Keys row: `↑↓ · Enter 그 대목 읽기 · ←→ 구획 · …`. The folded strip and a window too small for the panel's NOW row
reach it as every popup is reached: `Ctrl+]`, then ←→.
The question popup answers this session's own questions; another session's wait is answered in
its own window or through Fleet (`f`), which also goes to that session, restarts or tidies it. While it
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
its color at full strength. Inside a popup there are two grounds, the popup's and the chosen
choice's (11-15, the person: "질문 선지 선택 효과를 틴트로 해 명확하게"): in the question popup
every row of the chosen choice (each row of a wrapped one), and while this question is answered
here the answer being typed or picked (`› …`), stands on `FACES.*.chosen` (24 on a dark or unknown
face, 153 on a light one) across its content's cells; `▸` and the bold words stay, no part keeps a
ground or a reversed cell of its own inside it, and the cell between it and each side line keeps the
popup's ground. A decision has none until ↑↓ or a number picks one; a question not answerable here
has none. Mono, eight colors and `NO_COLOR` reverse those same cells instead. The popup's ground
also runs one cell past each side line on the frame's rows (11-15, the person: "좌우가 더 좁거든?
스페이스바 한칸 정도 여백을 더"), where the window has that cell (under 28 columns the frame takes the
whole window and there is none); the empty rows above and below keep the terminal's own (the host
writes such a cell plain first, so a wide character it cuts in half leaves its other half in the
terminal's own look), and closing it leaves no ground behind. One frame
serves every popup: a thin rounded line one empty cell from what it covers,
the title and `✕` on its top line, the sections on its bottom line (the current
one bright) and the keys on the row above; the question popup the window less 4 (132 at most; 11-18, "가독성이 안좋다": it was 76),
a table the window
less 4 (116 at most; the routes popup 196 at most: 11-18, the read for it takes at most 200 columns), all the window less 4 under 80 columns and the whole
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
did, rather than shown as `?`. For the callback's exact session the shell blanks Claude's
statusline area, the rows directly under the input box, when their text equals
the callback's latest recorded output. (11-16, spec rule 60: the rows are blanked
whatever the panel paints, and the first of them is the shell's one line; only a settings
screen or Fleet over the window keeps the native rows.) Folded, the strip takes the row's
place (its one row always paints the session; it carries no Git or job row), so the
values show once and the row is the shell's line. A native screen too short for the whole
record (the bottom layout's ten native rows) shows only its first rows, running
to the screen's last line; those rows are the recorded ones and are blanked
too. A session with a name carries it in the input box's upper border (rule glyphs around one blank-set-off piece
of text, at either end or between); that is still the input box. A screen without that input box, such
as the Ctrl+O transcript, keeps every row. Claude keeps as many rows as the
callback's output has, and at least one while a statusline is set; the one row
leaves two rows under the input box, this row and Claude's own mode row, at
every width and with running jobs. That row is, since 11-16, the shell's one line (spec rule 60: the working folder and `hearting v<release>`); what was the notice
row stood here until then (an answer this session owes, compaction imminent, a context or usage reading at Fleet's
red level, a failed or blocked route). Those four now stand in the panel and on the folded strip (the answer wait,
the notices, the red window, `✕N`; see the strip's order above), and where the panel is not painted Claude's own
statusline shows them (the common line's own order: the wait, compaction, a red reading, then the rest). The Python
`session_panel.notice_line` and the projection's `notice` field are no longer read by the host; they stay as they are.
News and release lines were never on that row: a received peer report stands in MESSAGES, a finished route's result in the
route box (the producer's recent results follow the current routes), and a
waiting release or a switch without scrollback in the panel's tidy-notice slot.
A finished result's row leads its title with the result word (`FAIL · title`),
takes Fleet's failure mark `✕` and color when the result did not pass, and leaves
the rows after the person's next input or ten minutes.
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
