# Capability: unit

This is the portable contract for `unit`, the compiler-internal capability behind a route that is one unit and nothing else. It is not a Claude Skill file, not a native command, and not an owner persona: no person and no model invokes it by name. It is reached through `capability-route.py compose --unit <family/name> --prompt-file <brief>`.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `unit` |
| Group | `sub` |
| Supported modes | `none` |
| Portable meaning | Compiler-internal one-unit route: one read-only unit runs as a single leg the depth-0 session launches itself, with no owner; its kind is `unit`, its field comes from the unit head or --field. |
| Argument shape | `compose --unit <family/name> [--field <field>] --prompt-file <brief>` |

## Shape

The route has no intensity: one unit does one job and nothing checks it (its record keeps `quick` as a compatibility value, so the route reads as it always did). It is sealed with one node and no owner node:

| Node | Kind | Runs as | Model |
|---|---|---|---|
| `unit` | map-worker, the named unit | a registered depth-1 attempt, launched by the depth-0 session (`launch_authority: depth-0`) | the grade of the unit head's `floor` (`near-zero`/`low` → `light`, `moderate` → `balanced`, `high` → `balanced-deep`, `highest` → `deep`); `--profile` overrides it; `top` is not available |

The node's unit, role and worker type are the unit head's. The recipe holds only the template (`capabilities/topologies.json`, recipe `unit`), so the expected node of a sealed route is read from the recipe and the head, not from a table of node ids.

## Arguments

- `--unit <family/name>` names the kind itself. Nothing else has to be typed; an argument that contradicts it (`--capability` other than `unit`, `--shape` other than `solo`, `--intensity` (a unit route has no intensity; an old command's `--intensity quick` is accepted and ignored), `--graph`, `--route-plan`, `--signal`, `--execution-scope`) is refused by name (`compose-unit-conflict`). To have the work checked, open `--shape solo` or `staged` with `--intensity`; to judge more deeply, raise the model depth with `--profile`.
- A name that is not a unit of the catalog is refused with the list (`compose-unit-unknown`).
- v1 opens read-only units (`read_only: true`). A unit that writes is refused with its reason (`compose-unit-writes`).
- `--start --prompt-file <brief>` starts the unit's one worker with the brief (see Launch and end). Without `--start` the route is only planned and sealed.

## Field

The route records a field like every route. It is the unit head's `field:` (`field_source: preset`), or `--field` (`explicit`). A unit head writes `field:` only where the field is clear (`dev/*`, `qa/code-review`, `qa/security-review` → `code`; `qa/ml-debug` → `lab`; `design/*` → `design`; `editorial/polish`, `editorial/review`, `editorial/translate` → `docs`; `research/research-survey` → `research`). A unit without one is refused, naming what is missing, unless `--field` is given (`compose-field-required`).

## Scope

The node writes only under the cycle's `units/unit/**` (one report). A read-only unit creates no new worktree: it reads the caller's.

## Launch and end

- **Launch.** The depth-0 session (no owner above it) starts the leg through the same path that starts a frame leg: `compose --start --unit <family/name> --prompt-file <brief>` seals the route and starts the one attempt, `capability-route.py start --route <id>` starts or follows it again, and a second start launches nothing twice. A leg ended on a negative verdict or a pass may be asked for another turn (below). The worker's first instruction is the unit's text (stance and body), the dispatch facts, a few of the kernel's safety lines (its role, guards and scope, what it leaves to the supervisor, scratch), the brief as written, the node's declared scope and how to end; nothing the owner reads is added. The model grade is the one a staged route gives that unit (the grade the shipped recipes declare for it; `--profile` wins).
- **End.** The launch seals `workflow_completion=runtime-v1` on the leg, so the completion controller closes the route and seals the cycle when the worker ends, as it does for an owner. No close or finalize command is run by hand.
- **Result.** The completion notice that wakes the launching session carries the route's sealed summary: the verdict line, the review's blocking findings (a `Blocking findings` or `병합을 막는 것` section reads as remaining items) and the report path.
- **A review that finds blocking issues** ends `verdict: FAIL` with its report, which opens (its first non-heading line) on the closed verdict `Final verdict: changes requested — head <sha>` (`최종 판정: 수정 후 병합 — head <sha>`; the worker's first instruction says so; where there is no git repository the ` — head <sha>` is left out and the verdict still counts). The runtime reads that end as it reads a finished audit's negative judgment: a pass whose assessment of the subject is negative, so the route closes, the cycle seals and the notice carries the same summary as any other end (its result line says the review completed with findings). Only a `BLOCKED` end (the work could not be done), an end with no readable report, or a FAIL whose report does not open on that verdict (empty, says no review was done, declares a pass) is offered again as a new route (`compose-again`). In the blocking-findings section of a report, a heading or line that only says there is none (`막는 지적 없음`, `none`) lists nothing; anything else stays a remaining item.

## A worker that starts a unit route

A registered worker (an owner, a stage worker, another unit worker) may open a unit route as well: the same `compose --start --unit <family/name> --prompt-file <brief>`, run in its shell. The depth of a route is counted from the session that opened it, so the leg of a unit route is a depth-1 leg wherever it was opened from; no depth number forbids the start. What bounds a chain of workers each starting another is the worker governor's `dispatch` class cap and start budget (`utilities/model-worker-governor.py`): the start no slot fits is refused in the governor's own typed form (`waiting-capacity`, `refusal`, `worker_class`), and a refused start arms no automatic later start from inside a worker.

- **Below its caller.** The leg's registry row names the worker that started it (`caller_attempt_id`). Fleet and the panel hang it below that worker's row (one indented row, not counted as a route), and the worker's end stops what it still runs (below).
- **The result goes to the caller.** A worker is no session and has no notice to wake for, so the leg is delivered by the polling carrier: the receipt prints a bounded wait (`start --route <id> --wait`), and that command, run by the caller, returns the route's sealed summary (verdict, blocking findings, report path) once the leg has ended. Only the worker that started a live leg follows it; nobody else adopts it. It repeats until the leg ends (one wait is one start window).
- **If the caller ends first** the called leg is stopped with it (`dispatch-reap-watch.py` runs `route_parent_close.stop_called_workers` when the worker's process is gone): the row closes as the same typed cancellation a stop makes (`failure_class=cancelled`, `reconcile_reason=caller-ended`), and everything below it is stopped too. Its result would reach no one, so nothing is left running and nothing is delivered. A leg that had already ended stands.
- **What it may do.** The leg is a read-only unit: its scope is its own `units/unit/**` report, its first instruction carries the kernel's safety lines (no merge, release, installation, cleanup or PR; scratch of its own), and a unit that writes is still refused (`compose-unit-writes`). Starting a leg therefore widens nothing of the caller's scope.
- **Where it cannot start.** A Codex worker whose sandbox has no network (every Codex worker but a standard+ owner) cannot start a registered child that would have to reach its model: the readiness check refuses (`quick-headless-unavailable`, the existing typed refusal) and nothing starts.

## The record-free form: `--ephemeral`

`compose --start --unit <family/name> --ephemeral --prompt-file <brief>` is the same unit route that leaves nothing in the project's artifact folder or campaign records. Its route file, cycle and report live in a scratch artifact root under the project's runtime directory (`<runtime_dir>/ephemeral/<key>/.agent_reports`, one per project, `<key>` being the first 12 hex digits of the sha256 of the project's artifact root path, marked by `.ephemeral`; `--ephemeral` takes no `--artifact-root`). While it runs it is a registry row like any other (Fleet and the panel show it). Its sealed summary is the answer itself: the report text, one block, cut at 6,000 characters with `[answer cut at N of M characters; read the rest in <report>]`; the notice that wakes a session, and the wait result a worker reads, carry it as `Answer:`. A report with no text is no answer: that end is read as an invalid end (`empty-answer`), nothing is delivered as done, and the unit can be asked again. Its workers count under the project's model-worker governor, with every other worker (the scratch root names the project in its `.ephemeral` marker, which counts only when the named path's key is the folder's own name, and a worker that starts another keeps the one scratch root): one set of caps, no second pool. A start refused to a worker, by the governor or because the marker does not verify, leaves no open route or cycle behind.

## Continue a turn: `correct --route`

A unit route that ended on a verdict can take another turn, and the worker keeps what it learned: `capability-route.py correct --route <route id> --message-file <text> [--request-id <id>]`, run by the session that started the route (or, for a leg a worker started, that worker), keeps the text and, once the last turn has ended and the route has closed, starts **a new attempt of the same route and node in a new cycle** that wakes the previous turn's harness session (`resume`: Claude `--resume`, Codex `thread/resume`, OpenCode `--session`) and ends and closes the route again. A turn is therefore one attempt with its own row, its own report folder (the turn's cycle is a child of the previous turn's, in the same work stream, because the artifact index takes a second cycle of a route only as the child of its first; the earlier cycle and its report are left as they are; the closure record of the earlier turn is set aside as `<route id>.superseded-<time>.outcome.json`, never deleted) and its own completion notice, which says `Turn N … (session: resumed | fresh:<reason>)`.

- **The route's end is the last turn's.** A turn a later turn has continued has nothing left to settle: its closure record was set aside, so the completion reading (`start --route`, the recovery view, a late end receipt) treats it as not applicable, and the route reads as completed when its last turn has. Every turn that ends on a verdict gets the same success notice with its summary; a turn that ends without one stays an attention notice and `start --route` offers `compose-again`.
- **Receipt.** `state`, `request_id`, `duplicate`, `retained`, `unit_attempt_id`, `turn`, `continues_attempt_id`, `session`, and the turn's own `turn_tokens` (`turn_tokens_complete: false` is a lower bound). The row carries `continues_attempt_id`, `turn` and `session`; `session_id` names the Claude session.
- **While a turn runs** (or has ended but the route has not closed yet) the text is only kept (`retained: true`); the next `start --route <id>`, which the end notice's `resume_command` is, starts the turn. The same text sent twice (or the same `--request-id`) is one request; one id with another text is refused (`correction-id-content-conflict`). A text that already ran as a turn is not started again and its old result is not handed back as new: the receipt is `state: already-asked` and says so (send a different text or pass a new `--request-id` to ask for another turn). A worker that started the leg follows the new turn with `start --route <id> --wait`. Nothing waits in the background: between turns the route has no process and no governor slot, and a turn the governor has no slot for is `waiting-capacity` as any start.
- **`session`.** `resumed` when the previous turn's session file is in its worker home (it is copied, alone, into the new turn's home; the new turn's own permissions, sandbox and model are given again, so a read-only unit stays read-only). Otherwise `fresh:<reason>` and the turn starts a new session that is told the original brief, the previous turn's sealed summary and the text: `session-missing` (the home or session is gone, e.g. removed after its window), `harness-changed`, `store-unsupported` (OpenCode before V2), `resume-refused` (Codex without its App Server, where no session is kept). A Codex thread that cannot be woken (`thread/resume` refused, or another thread answered) ends the turn as an error rather than starting a fresh thread without the brief; the end notice carries a `Reason:` line with the recorded reason.
- **How long the home is kept.** The last turn of a unit route keeps its worker home while its parent can still ask: a leg a worker started, while that worker's row is live, and a leg the main session started, while that session lives, in both cases for at most the route's unused-route window (`route_autoclose.IDLE_SECONDS`, seven days); once that session is gone, for the ordinary one hour (`route_autoclose.QUIET_SECONDS`), and for the same unused-route window when its liveness cannot be told. A turn a later turn has continued, and a route closed automatically, keep the ordinary window.
- **Where it is refused.** `--ephemeral` routes (`correct-unit-ephemeral-unsupported`), routes bound to a request grouping, which includes a leg of an approved route plan, and routes that name their own parent cycle (`unit-continue-request-bound-route`: their cycle folder or parent is fixed), a caller that is not the route's parent (`not-admitted`), a route whose last turn ended without a verdict (`unit-continue-last-turn-no-verdict`: the route never closes, so nothing is kept; the receipt carries the compose-again command, as the end notice does), and a route with no first turn yet. A unit route needs `--route` (or the leg's attempt id): a `correct` with no argument still means an owner. `correct` with no text shows the turns so far.
- **First instruction.** The first turn's instruction is unchanged; only a continued turn's instruction names the turn: a woken session gets this turn's dispatch facts (the new output folder) and the text, a fresh one the brief, the previous result and the text.

## Completion

The `unit-complete` completion gate is the existing `custom` contract row: the unit's own `io.verdict` and its report. A unit route's worker ends with the three-line handoff of the worker kernel; the report is `units/unit/report.md` in the cycle.
