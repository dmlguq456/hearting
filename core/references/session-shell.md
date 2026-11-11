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
reach native; with settings, the Fleet explorer or a question editor focused,
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

The approved v3.3 shell opens as a right HEARTING work panel when space permits
(left by choice), with bottom placement at narrow widths. All four edges belong
to Hearting and share its representative state color; never frame the native
terminal itself. By default the panel uses the terminal's neutral background,
without tinted fills; a chosen theme or shade recolors only the panel itself.
Text and glyphs accompany state colors in every theme. Native permission UI remains native; idle alone means
neither work complete nor failed. Unknown evidence stays unknown.
The same facts and available panel dimensions use the same content order and
editing controls in all three harnesses, including startup and restoration.
The panel follows the 2026-11-11 unified layout (the C3 draft the person
chose). From the top: the header (`HEARTING ─── ⚙ Settings`); `[number]
project (git)`, with Git (branch and dirty mark, `🚧 N`, ahead/behind and the
existing merge warnings) dim in parentheses and no folder or branch glyph;
model and effort, then `context` with its gauge and percent (two rows beside
the native area, one `│`-separated row with the line above at the bottom);
running jobs; `◑답 대기 N` and the session's title; any question; the route
section; the messages section; and `5h … │ 7d …` last. Groups are divided by
`│`, stages joined by `›`. Fixed words are English (`context`, `route`,
`Settings`, `messages`, stage names, `5h`/`7d`); only real content and
language-dependent text is Korean, and `◑답 대기 N` keeps its existing words.
The panel paints no clock and no last-input row. A value that was not
observed is left out, never painted as `?` or an estimate. When rows run
short, the question and the top groups stay; spacer rows, then messages, then
opened details, then route rows give way (and, only for a question's rows, the
route section, running jobs and 5h/7d). Variable-width text (titles, message
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
The route section has no box: a titled rule (`── ⏳ route N ──`) above and a
rule below, shared with the messages section when both show. Each current
route (the ones the statusline counts) is one folded row: Fleet's state glyph
and judgment (`⠏` working, `●` waiting, `◑` blocked, `✕` stopped, `✓` done),
title, harness, context percent and elapsed time (`route line` = `time`, the
default) or title, harness, model and context percent (`model`). The bottom
layout fits model, effort, a five-cell gauge, the current tool or stage and the
elapsed time on the row whatever the setting. Pressing a row opens it in place
(model, effort and gauge, `plan ✓ › execute ● › review` when there are several
stages, `>_ tool` while working, `+N agent`) and pressing it again folds it.
More than five routes end in `+N more`, which opens Fleet. These values come
from the answer-wait producer's existing pass: the shared line helper adds each
route row's job values (harness, display model, effort, context, tool, active
sub-agents and an absolute start time) from that same snapshot, and counts
the route states into the statusline label (`⏳ route 3 ⠏2 ✕1`). A job's
context is its `ctx_pct`, else its tokens over its window when both are
known, else left out. The display is as fresh as that cache. Pressing the
section's heading opens the existing read-only route evidence pages.
The messages section shows the latest three peer messages, one row each (`←`
received from the exact session's retained received inputs, `→` sent from this
session's own ledger shard and its 200-character summary), cut with `…`;
pressing a row shows its whole retained text in place. Nothing new is stored.
A press on a route or message row is display only: it opens or folds the row
and leaves focus, `empty` and the native input frontier as they were.
Put each answer choice on
its own line, wrapping within that choice; do not reserve a standalone
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
narrow-window behavior, auxiliary detail, theme, shade and `route line`
(`time`, the default, or `model`). One placement rule
serves every harness. Themes are `terminal` (the default: the shared palette),
`light` (darker hues for a light terminal) and `mono` (no hue; weight, glyphs
and words remain). Shade is `none` (the default), `light` or `medium`: the
panel's own background, painted with an explicit text color so it reads on
either terminal background, in 256-color terminals and never under `NO_COLOR`.
Neither recolors native space: the native area and Claude's status row keep
their colors (Claude's notice row takes the theme, not the shade). An unknown position, theme or shade falls back to its own
default without disabling the shell, and so does an unknown `route line`.
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
observation, painted in the compact status row as `MCP connected/total`
(red while one has failed or needs authentication). No Hearting section is
drawn inside OpenCode's sidebar. Panel placement never changes question authority.

The current question,
choices or free-text affordance and exact request state are readable before
focus; an empty question section reserves no space. Retain choices and drafts
by request ID, revision and question ID when moving with Tab or leaving focus.
Enter submits only explicit, complete answers. Keep a single active action.
Collect every current form draft at that one submit; moving with Tab does not
require Enter in each field and an edited draft supersedes its earlier value.
Secret answers remain masked while selected, submitted, unfocused or consumed;
only the unchanged canonical response carries their actual value.
Pending transport and proved consumption are distinct, and unavailable, expired
or cancelled questions offer no active choices. Long questions remain readable
with focused scrolling and an explicit overflow count. Use the bounded configured row budget,
preserve at least six native rows where possible, and retain the shared status
fields. The former rows/3 estimate does
not hide a question or require another user decision at 80 columns.
It shows the same session's routes, questions, received peers, repo work and
last real human input. Only session-owned GPU work warrants a resource row.
`Ctrl+]` enters block focus; Escape or `Ctrl+]` returns to native input. Ordinary
typing, including digits, Enter, Tab and action letters, remains native outside
that focus. The focused question editor uses existing `questions list/answer`
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
━━━━━━──── 58% │ ◑답 대기 1 │ ⏳ route 3 ⠏2 ✕1 │ 5h 12%(↻ 2h) 7d 66%(↻ 5d)`;
Claude's second (`>_ running:`) and third (tidy notice) rows are unchanged.
Narrow layouts shorten words before numeric values, routes and waits (the
answer wait shortens to `◑ N`). Its title and guidance use Fleet's
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
paints every former Hearting field with that formatter on every callback; the
panel never shortens it. Its first callback at session start reads the same
shared Git observation as later callbacks, so folder, branch and model show
before the first input; context and quota readings Claude has not reported yet
(it sends them after its first response) are left out, as the native statusline
did, rather than shown as `?`. Only while the panel actually paints the groups
that row carries (number, project and Git, context, the answer wait, the route
count, 5h/7d and running jobs) for the callback's exact session does the shell blank Claude's
statusline area, the rows directly under the input box, when their text equals
the callback's latest recorded output. A screen without that input box, such
as the Ctrl+O transcript, keeps every row. Claude has no supported way to
remove those rows, so as many rows remain as the complete status takes
(measured: two at 133 and 80 columns, three with a running job); keeping every
value visible comes first. The first of
them is a notice row, empty unless the person must know something now, and any
other stays blank. One function picks a single item by priority from existing
values: an answer this session owes, then an already computed warning
(compaction imminent, a context or usage reading at Fleet's red level, a
failed, blocked or attention-needing route), then news that arrived since the
last human input within ten minutes (a received peer report or a completed
route result). It reuses Fleet's glyphs and colors and adds no collection,
setting or judgment. A hidden, collapsed, focused or not-yet-bound panel, a
lost display host and unwrapped sessions show the complete native rows
unchanged. Unwrapped displays remain intact.
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

At a short bottom answer editor all harnesses prioritize the same question/
choices/draft/control rows; session rows take only the rows left after them.
