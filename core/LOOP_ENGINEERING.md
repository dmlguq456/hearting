# Hearting Development Philosophy — Loop Engineering

Hearting owns the loop that carries a user's intent all the way to a verified
result and its handoff. It is judged not by how many mechanisms restrict
execution, but by its ability to finish work without losing it in normal,
failure, and interruption cases.

Here Loop Engineering is the name for Hearting's development principles. It
does not mean adopting a particular outside methodology. It adds no procedure to
read or submit for each new task. Maintainers apply it when judging whether a
structure or a change is sound.

## 0. Three standing goals

The user set these as Hearting's core goals (2026-10-06). Sections 1–6
describe how to reach them; judge any structure or change against these first.

1. **Light dispatch surface.** An agent that dispatches work states only the
   goal, inputs, scope, and completion condition. Whatever the runtime already
   knows or can settle on its own — provenance, paths, parent and identity,
   default model and harness, access, retry and recovery commands — Hearting
   fills in. (See §2.)
2. **Free but systematic planning at frame.** The frame stage shapes the plan
   to the task instead of forcing a fixed template, fitting the work like a
   tailored suit, yet the plan names its stages, parallel parts, verification,
   and completion conditions clearly enough to run, resume, and hand off
   without deciding them again. Recipes and presets are only default shapes.
3. **Harness-independent consistency.** The same request means the same thing,
   is judged the same way, and finishes the same way on Claude, Codex, and
   OpenCode. Decisions live once in the shared layer; adapters only translate
   execution. There are no per-harness exception branches or duplicated
   judgments. (See §3 and §5.)

For durable completion delivery, an exact native receipt remains valid after
the transport claim expires or changes owner. All carriers reuse that receipt
through the delivery's existing immutable identity and `ack_received` CAS; an
ACK write failure leaves the verified receipt usable for suppressing another
send while settlement is retried naturally. Output, process exit, queue
acceptance, or an incomplete native-history read is not receipt evidence.
Receiving a notice settles delivery only: it does not complete the work or its
follow-up. When native receipt evidence is absent or unknown, emission remains
recoverable through the existing fallback.

## 1. The unit of a feature is a responsibility carried to the end

`intent → execution → observation → result settlement → follow-up or recovery → closure → handoff`

Each transition has exactly one decision owner. Several observers may collect
evidence, but none of them may overturn a success or create a retry on its own.
When responsibility for work changes hands, what was handed over and who
received it must be recorded. The original owner keeps its obligations until
the handoff is confirmed. Process exit, report writing, task completion, and
user notification are different facts.

| Decision | Responsibility that must carry through |
|---|---|
| Allow execution | Bind the exact execution identity to its lifetime manager. |
| Defer or refuse execution | Return the cause, the remaining obligations, the owner, and the supported next action. |
| Settle completion | Check content, evidence, and cleanup state, and preserve the settled result. |
| Retry | Check the existing result and actual cleanup first, and prevent duplicate execution. |
| Observation failure or stall | Preserve the unsettled state and keep the responsibility to re-observe or hand off to a person. |
| Closure | Leave unfinished follow-up stages, artifact sealing, and failure cleanup in a recoverable state. |
| Notification | Distinguish support, queue acceptance, delivery, and receipt, and connect a follow-up handoff path on failure. |

Recording an owner's name does not by itself implement this responsibility. The
real execution path must be bound to that owner, and the obligations must be
recoverable after exit or restart.

## 2. Keep the surface simple and the defaults replaceable

Users and agents state the goal, inputs, scope, and completion condition.
Hearting assembles paths, state, call order, and recovery commands. Do not make
anyone repeat the same content across several flags and documents to satisfy an
internal contract.

Capabilities and recipes are useful default configurations, not grounds for
forcing the same stages on every task. Keep work stages such as planning and
verification separate from dispatching a separate process. Dispatch when
independent review or parallel work pays off, and keep the same result and
recovery responsibility when direct execution is allowed.

Model, profile, and capacity policies are defaults too. An easy task may choose
light, and an explicit choice is honored within the supported range. Explain
constraints such as authentication, real availability, approval boundaries, and
evidence integrity separately. Do not silently change a choice, and do not turn
a simple preset difference into "cannot run".

## 3. One shared meaning, runtime-specific execution

Claude, Codex, and OpenCode are equal adapters. The shared layer owns the
meaning of input, completion, retry, cleanup, and handoff. Process start,
resume, and notification mechanisms are implemented to fit each runtime. No
harness is the reference implementation for another.

Do not mark an unsupported transport as supported. Confirm the available
fallback path and who takes the next action. Matching surface names or ledger
states are not evidence of equal responsibility.

## 4. Do not dress uncertainty up as failure or success

A process that cannot be observed is unknown. Do not declare it dead because it
is invisible from another PID namespace or because its log is late. A stall
alert deadline is a deadline to re-observe and notify, not authority to end or
retry the work. The workload declares the progress standard for long work; a
uniform time cap does not replace it.

Preserve settled PASS and FAIL results. A later conflict may hold back current
consumption but never overwrites a past result. Follow-up work connects exact
lineage and current evidence instead of rewriting the original. Reusing existing
work is distinct from producing a new verification PASS.

An observation can establish that an exact input was consumed, or expose a
concrete conflict. An unavailable or incomplete observation establishes neither
consumption nor absence. First-send eligibility comes from the existing pending
record and its claim boundary; after an attempt, the existing delivery lineage
owns further observation. Do not turn uncertain history into a new retry rule.

Observational callbacks must not delay the operation they observe. Provider
telemetry must let the runtime consume its original response without waiting
for a cloned body or its reader cancellation.

## 5. Remove duplicated decisions before stacking exceptions

A repair does not end at letting the one failing branch pass. Find the writers,
readers, watchers, and adapters that make the same decision and gather them
into one shared judgment and transition. When a new path replaces an old one,
also remove the duplicate implementation and the caller procedures it made
unnecessary. Do not hide a responsibility gap by removing every guard or by only
adding new refusal rules.

Read-only work shared during one input may reuse that input's parsed snapshot.
A shared registry view may live across scan passes while its file generation is
unchanged; each pass still checks the current generation and re-observes any
other input it depends on. The snapshot is not authority for a state-changing
action: claims, acknowledgements, handovers, and sends recheck the current row
and binding under their existing lock. Recovery callbacks reconnect the
existing owner of work; they do not repeat work already owned by its live
runner.
The runner keeps its lease while any accepted local or external duty remains;
an unrelated callback must not replace that waiting owner and discard its
observation state. Code modules use their normal process lifetime, while
the state and authority those functions observe remain fresh inputs.
An observer lease transfer publishes the successor's existing PID/start record
under the duty's existing update lock before that successor can claim it.
The inherited descriptor and that record carry the transfer without another
CLI input or a second recovery owner.

When several consumers need the same expensive read, the existing runner may
publish one narrow, identity-bound observation for them to reuse. Each detached
watch still owns its native result, watch id, deadline, receipt, and ack; a
shared projection only answers readiness and never settles another watch. A
consumer with a missing or stale projection reconnects to its existing duty
and observes again without replaying work already owned by its live runner.

Session hooks request that existing recovery runner and return; they do not
replay global settlement history before delivering and acknowledging the
session's own notices. The runner retains observation state across requests.
An idle foreground waiter observes its exact parent's current row without a
writer lock, and checks process-group cleanup only after its leader exits.
Registry mutation keeps its fresh authority check inside the existing short
writer section; observation must remain safe when its caller already holds
that same registry lock.
After native idle is confirmed, a watcher with positively pending registered
work may keep waiting on a fresh observation of that work and the current
exact pane census. It needs native readiness again when that work stops
blocking or its identity becomes uncertain. An unrelated pane event alone
must not restart the native readiness probes while completion is impossible.

Long-lived observers may retain a bounded projection of an immutable result
while every file generation it depended on remains current. The projection
must name those dependencies, and a missing, unreadable, retargeted, or
unstable input discards the prior result instead of turning it into authority.
Mutable writer documents stay separate from historical read projections.
Append-only reuse is limited to a continuously observed file whose supported
writer appends records; a partial byte tail remains data, not a row, until its
newline arrives. Replacements and uncertain writes rebuild the view. Reusing
parsed bytes avoids I/O; reusing derived decisions additionally requires
unchanged dependencies, while live recipients, leases, claims, processes, and
state transitions remain fresh observations.

Documentation explains behavior and preserves the reasons for decisions. Do not
build the product on a protocol that only ends cleanly when a model perfectly
remembers long operating documents.

## 6. Prove completion through the consumer path

Verification of a fix covers not only normal completion but also late results,
duplicate retries, unobservable state, supervisor exit and restart, delivery
failure, and a busy owner. Choose regressions that fit the change's risk, and
do not claim real receipt from registration or a passing fixture. Any needed
live measurement connects the exact execution, result, and receipt.

A completion report states the behavior fixed, the duplicate decisions and
manual procedures removed, the final owner, and the boundary between what was
actually verified and what was not. Keep the versions of source, release,
installed copy, and running session distinct. Measure needless model turns,
restarts, manual intervention, and boundary wait time, but do not present a
smaller document or fewer lines of code as token or cost savings.

## 7. Recovery follows lineage, not replay

Between an original attempt and its recovery, work changes hands in ordinary
ways: a supervisor seat is handed over, an owner is replaced, the parent moves a
harness or model pin, a predecessor fails or is paused by a guard, or only an
observation step fails. A retry, replacement, continuation, or resume accepts
these changes through one shared lineage rule. It does not rebuild the original
record from current values and refuse at the first difference.

The shared recovery judgment reads stored inputs before reconstructing missing
legacy inputs from their original sealed route and exact original rows. Current
selection and authority are checked afresh at registration and launch. Work,
review subject, fixed inputs, session scope and granted addresses remain bound;
parent, owner and runtime selection follow their recorded lineage. A claim alone
does not adopt a continuation. Process quiescence and terminal result availability
are separate observations consumed by resume, reconciliation and settlement alike.

Exact comparison protects sealed evidence from tampering; it is not a reason to
refuse the normal ways work moves. When a recovery path still refuses, the
refusal names what differs and the supported next step. A state with no
supported way forward is a defect in the path, not a task for the operator.

An attempt registration is not work that reached the launch fence. Only a
never-started registration closed as cancelled under the registry lock is
withdrawn from round censuses. An open registration reserves its round until
claimed or closed, and an actual pre-launch failure remains a verdict-less
round. Timeout cleanup closes only that exact row while the never-started proof
still holds under the registry lock; claimed, identified, started, and
unobservable rows remain with reconciliation.

A caller-identity refusal records who may not launch; it does not settle the
existing recovery duty. An explicit start may re-enter the same exact claim
when its successor has no row or has launch-fence proof that it never started,
then must pass the unchanged parent and launch checks. During that same fenced
launch transaction, its exact successor row may exist before `launch_started`;
only the existing admission and spawn fence can carry it forward. Read the
unlaunched hop from that source-row proof even when an older claim recorded
another death kind. The next owner receives every answer still unconsumed by
the semantic owner, while claim-pinned answer identities and digests remain
the minimum integrity check. Keep this lineage and handoff meaning in the
shared layer for all runtimes.

## 8. Let real work reshape capabilities and routes

Capabilities, shapes, and routes are revised from the record of real work, not
defended as fixed contracts. Periodically read what users and agents actually
ran, where they worked around a capability, where work stalled, and where a
person had to step in. Widen a capability that work keeps stretching, merge or
drop one that is rarely used or keeps blocking, and loosen a route rule that
refuses approved work. Changing the registry or route rules must not strand
routes that are already running.

## 9. Move hard parity outside the adapters

When a behavior has to be built separately inside each runtime and keeps
diverging — waking a parent, watching a session, session identity, pane
placement, remote compute access — build it once in a runtime-neutral layer
outside the adapters and let all three harnesses use it, as herdr with
`peer-steward` does for sessions, `compute-hosts` for remote GPUs, and Fleet for
observation. Adapters then only translate. Prefer one outer mechanism that every
harness shares over three inner realizations kept in step by hand. Shared
meaning stays in the core (§3); a shared mechanism may live outside it.

## 10. The operator sees delegated work at a glance

Fleet is how a person sees the work they delegated, and for dispatched sessions
it is often the only way. It opens quickly (first observed state in about five seconds,
measured from launch; a loading frame does not count)
and shows, for every live session and dispatched owner, what is happening now:
the model turn, the running command, or the resource the owner is waiting on
with its host, GPU, and progress. An empty NOW while work is running is a
defect. A slower or emptier view is a regression even when every check passes.
Publish observed session identity, execution/wait reasons and resources first;
campaign titles, route details and installation/git metadata fill asynchronously.
Unknown observations stay unknown, and slow details must not stop basic refresh.
Project activity includes running resources nested under an idle owner. A file
update is labelled as a file update, never as completion. Recent results show
the recorded task, result and event time; an unproven gate is not a failed task.
Work names share one display rule across recent results, resource rows and GPU
links: prefer the existing cycle easy title or one-line summary, then its
recorded title and campaign title. Without a usable title, show a short readable
name with locator dates and execution suffixes removed. Read titles in the
existing detail pass; never wait for them before publishing basic observations.
Display labels use existing compact route-to-cycle filename hints and the bounded
cycle inventory. Validate the selected record's exact route and normal campaign
metadata; do not deserialize or retain full artifact-admission documents for
labels. Generic metadata readers retain their existing document contracts.
The name does not repeat the same command or execution identity on its line.
Keep technical terms, capability/stage identifiers and short keyboard actions
in their original notation. Fleet's own labels and legends use short English
or symbols; session titles, summaries and memory content remain as written.
Relative observation times use `1m ago`, `2h ago` or `3d ago` at the end of
the text, with their suffix reserved when clipping. Execution durations retain
their existing format. Previous subjects use dim color without a label prefix;
project results use a result glyph, title and age. Repeated nonblocking resource
diagnostics share one counted row per kind/reason, while blocking observations
remain individually visible. These display changes add no rows, flags or
settings and preserve gauges, percentages and column anchors at narrow widths.
Do not put a translation beside the same original term.
Current execution or wait observations precede generated text. Session symbols
already show state; NOW does not repeat a state word, and the context gauge and
current subject need no labels. Mark a subject as previous only when its known
successful generation predates a new work boundary or exceeds the existing
24-hour title window; idle state or an unknown timestamp cannot prove this.
Summaries remain previous text with their own age, including sentences that
say work is running or complete. The symbol legend keeps its explanations.
An incomplete observation cannot establish absence: publish the last observed
work together with the failed source, its last success and the reason it is
unconfirmed. Basic and detail refresh share this health vocabulary. Publish
route evidence and its projection in the same snapshot; rendering consumes
that snapshot without rereading collector globals or redoing the judgment.
Pane display updates belong to the existing detail pass; assigning a live
session's number must not wait for pane metadata writes before basic publication.
Delayed display writes check the pane's current exact session identity and
assigned number; a changed or unconfirmed target receives no old metadata.

Answer waits are one read-only projection of existing sources: exact native
interaction sidecars (S1), raised human gates (S2), ended owners whose result
already waits for a person (S3), and explicitly marked final replies (S4).
Display surfaces read only the private derived cache; one demand-driven producer
refreshes it from the existing sources. A missing or expired cache is unknown,
not a confirmed zero, and S4 on the current seat is read directly so an exact
prompt close is visible. Each source closes only through its existing answer,
gate, successor, or exact seat-input rule. S3 includes only route-bound BLOCKED
owners without a later owner attempt on that same route. Every source is current
for 72 hours, older through seven days, then omitted; `total` counts current
items and `older` counts the older window. The private session projection may
retain question text for its intended session and notification consumers. Fleet
reads a separate derived projection whose schema contains no question, choices,
recommendation, seat key, or free-form health reason. Incomplete health remains
unknown in both projections, never a confirmed zero. Totals, session indexes and
notifications count current items only. Unknown seat attribution remains in the
global current total. Reading does not claim, resume, settle, or mutate a job.
S4 signals and derived caches are observational, private state; folding never
changes source state and adds no hook, gate, or user setting.

Fleet's interactive view keeps shared status, usage, compute/GPU observations,
and its controls outside the scrolling work-list viewport. The list body owns
its offset, selection-follow, page movement, and wheel movement; the flat line
builder remains independent of that offset. When a terminal is too short to
show everything, preserve the footer and current status first, then usage,
then the available compute/GPU rows; omit decoration and reading legends before
those observations. A zero-height body has no list hit targets.

Mouse actions address only cells successfully painted in the current frame.
Refresh, resize, fold, scroll, and view/layout changes invalidate the previous
hit map before another event is applied. Fold controls own only their section;
folding a repository hides its session/work body while leaving shared status and
compute observations visible. Session actions retain F-27 confirmation and
identity checks. Fleet's current approved default enables ordinary curses mouse
input when `FLEET_MOUSE` is unset or exactly `1`; any explicitly set other value
keeps the prior opt-out behavior. This default differs from the shared Fleet
PRD's opt-in policy pending its separately deferred revision. Each harness
preserves the same display and safety meaning, with terminal-specific input
support and limitations recorded by the implementation.
Terminal input decoding never waits inside the frame loop for the rest of a key
or mouse packet. Incomplete sequences are consumed incrementally, and only
complete valid events are dispatched; the mouse default, explicit opt-out,
keyboard behavior, and click safety remain intact.
Already buffered bytes of one incomplete event are drained with bounded,
nonblocking reads before another full render. Missing bytes still yield to the
frame loop, and decoding stops at the first complete event so queued actions
retain their order and confirmation boundary.
Once an SGR mouse prefix is recognized, malformed contents are consumed through
their terminator or a short internal expiry and are never replayed as commands.
Unsupported button, modifier, motion, and wheel combinations are inert. Pending,
ready, or discarded decoder input is not idle for install-follow; expiry returns
the reader to idle so refresh and follow continue without user recovery.
Diagnostics retain the reader's kind, source and blocking evidence. A missing
historical resource reference is not a broken dispatch log or a current execution
failure. Memory rows describe a stored change with its content and observed origin;
storage classifications never impersonate a current user decision.
A process name proves a runtime, not a conversation: one shared process-role
observation distinguishes terminal sessions, registered workers and unbound services.
Services produce no session row. Current session identity joins live PID/start,
the pane selection and recorded conversation continuity once; title, state and
context consume that same result. Conflicting identities and absent work bindings
stay unknown rather than borrowing a neighboring transcript or artifact directory.
An exact live PID/start native current-session record takes precedence over
historical statusline taps and delayed pane session labels after an in-process
resume or clear. A proven different current session blocks an automated continue
even when a delayed pane label still matches the booked session.
Historical continuity cannot override an explicit native
selection back to an earlier conversation. Without such a current record,
recorded directed continuity resolves claims; unrelated claims remain unknown.
File recency and launch arguments never prove a current selection. Project grouping uses the
identified session's own current directory (native registry, transcript/rollout,
or session database), with process cwd only as the fallback. Pane/process
attribution observes the process directory before applying that display value.
Routine ledger folding preserves the recorded native transition direction.
The normal view retains observed run/configuration names and explains session,
device and stage symbols separately. Names, current actions and controls share
the terminal's width budget; internal identifiers belong in detail. Support
work carries its caller's observed session/seat independently of execution and
delivery authority, and is labelled project support when that link is unproven.
Legends explain statuses and roles in short English, including project
activity and expanded GPU commands; a copied dot or arrow must retain its meaning.
GPU occupancy names the exact observed sessions once each, using their explicit
name or verified alias and current Fleet tag. Run/job names and identifiers never
stand in for a session; observed occupation without an exact session is `미귀속`,
not an empty GPU. Process commands and measurement freshness retain their meaning.
Raw owner identifiers remain available in structured output.
Expanded GPU process and reservation rows omit task descriptions and run IDs;
the session identity and observed command identify the occupation. Reservation
times remain facts, while task/run metadata stays in structured output.
When a session label has only its Fleet tag or short session ID, show its
observed repository before that identifier instead of the harness name. Peer
communication uses compact directional labels inline in the existing session
rows: an on-board Fleet tag or harness-qualified short SID identifies the
endpoint, while the exact full `(harness, session_id)` owns the join. Same-peer
opposite directions may combine; different peers keep both directions visible
at narrow widths. Colliding tags are harness-qualified and same-harness tag or
SID-prefix collisions retain a distinguishing SID suffix. Peer labels do not
add repository, name, kind or age detail;
the existing structured output retains it. Role and same-repository supervisor
meaning continue to come from the existing exact collector evidence. Resolve
linked worktrees to their parent repository through the existing grouping
resolver for session and GPU labels. Missing repository evidence leaves the
identifier alone; another session or workload name never supplies it. Explicit
session names and verified aliases keep priority.
GPU sampling deadlines cover SSH connection, both bounded NVIDIA queries and
process attribution, with room in the outer collection deadline. Slow sampling
stays in the independent GPU refresh pump and does not delay basic refresh or
input. A genuinely expired query still reports the timeout and unknown metrics.

## Basis and current scope

These principles condense the completion standard the user repeatedly corrected
in September 2026. An earlier structural diagnosis pointed at boundary costs and
operating procedure leaking to the model, and the dispatch responsibility repair
record keeps both the real completions and the failures. Adopting this document
does not mean unrepaired paths are complete. Each change must prove itself
against the standards above.


Sections 7–10 restate a memo the user wrote in Hearting's v1 era, made standing
direction on 2026-10-09. On that day seven defects of the §7 kind (PR #427–#432
and related) were each fixed by a case-specific allowance, one Fleet render took
46 seconds, and three dispatched owners waiting on resources showed an empty NOW.
The Korean original and the memo text are kept with the internal documents.
