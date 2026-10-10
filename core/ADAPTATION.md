# Adaptation Contract

This document defines how the neutral harness becomes a runtime-specific setting.
It is the boundary contract for `claude_setting/`, `codex_setting/`,
`opencode_setting/`, and future runtime projections.

## 1. Source Categories

Every file in this repo must fall into one category.

| Category | Meaning | Examples | Runtime projection rule |
|---|---|---|---|
| Portable source | Runtime-neutral semantics. Describes what must happen, not how a vendor runtime invokes it. | `core/`, portable parts of `tools/`, portable guard algorithms | May be symlinked into adapters if the runtime can read plain files |
| Adapter source | Runtime-specific representation of portable semantics. | `adapters/claude/CLAUDE.md`, `adapters/claude/settings.json`, `adapters/claude/commands/` | Projected into that runtime home |
| Adapter projection | Versioned mirror that exposes adapter source under runtime-expected names. | `claude_setting/`, `codex_setting/`, `opencode_setting/` | Symlink or generated output only; no independent semantics |
| Compatibility reference | Historical source kept for parity/drift checks after an adapter-owned realization exists. | `skills/` byte-equivalent to `adapters/claude/skills/` | Not projected as portable source; guarded against drift |
| Compatibility passthrough | Legacy file still consumed directly by a runtime before a true portable/adapted split exists. | Mixed shared hooks or utilities not yet split into invariant + adapter wrapper | Allowed only with an explicit debt note in the adapter |
| Runtime state | Tool-owned mutable local state. | `<runtime-home>/projects`, credentials, session logs, caches, DB files | Never committed to this repo |
| Improvement evidence state | Incidents, candidate fixtures, proposal evidence, approval references, and version-bound realization records. It is not active harness source. | `${XDG_STATE_HOME:-~/.local/state}/hearting/improvement` | Never projected or runtime-discovered; adopted source changes use a separate spec/code/release cycle |
| Continuity state | Cross-project agent worklog/notes data that survives sessions but is not harness source. | `<agent-notes-root>/cards`, `_layer2`, `_triage`, `digests`, `oncall`, `study` | Never committed to this repo; may be versioned in a separate notes/data repo |
| Local board app state | Worklog-board local app workspace, generated output, DB/cache, dispatch logs, and worktrees. | `<worklog-board-app>/.cache`, `.next`, `.dispatch`, `.env*`, `node_modules`, `<worklog-board-app>-wt/` | Never committed to this repo |

Project files and adapter runtime state need not share a filesystem. If a
child runtime requires user-owned state but the project filesystem maps
runtime directories to a different UID, its adapter uses the existing canonical
dispatch state root for that state, within the owner's normal writable scope.
It preserves the project location and foreign directory, links existing auth/config read-only,
and does not ask the caller to change ownership, permissions, or launch flags.

## 2. Adapter Rule

An adapter must not claim support for a surface unless it provides one of:

1. A native adapter file.
2. A generated file with a documented source.
3. An explicit compatibility reference or passthrough entry and the reason it is safe.

Plain symlinks are acceptable only as a projection mechanism. They are not proof
that adaptation is complete.

### 2.0. Sibling-Adapter Completion

**A portable change touches all adapters at once. Never deliver, commit, or
report a portable change for one adapter and leave the siblings for "later" — a
single-adapter split is the failure this section exists to prevent, not a normal
increment.** `core/`, `capabilities/`, and `roles/` are the semantic source.
Claude, Codex, OpenCode, and future adapters are equal sibling realizations below
that source; no adapter is the reference implementation or parent of another adapter.

A shared change follows one transaction:

1. update or confirm the portable invariant;
2. generate or edit every applicable sibling realization in the SAME unit of work
   (`generate.py` projects all three — never hand-mirror one and skip the rest);
3. verify each runtime's active discovery surface and required fallback
   (`check-adaptation-boundary.sh` audits all three adapters, not just Claude);
4. report the overall result as `PARTIAL` while any applicable row is deferred,
   unsupported without fallback, or unverified; report `GREEN` only after all
   applicable rows pass.

A sibling that reaches the portable source directly — e.g. Codex/OpenCode invoke
`$AGENT_HOME/utilities/<tool>` through their preflight wrapper instead of holding
an adapter-owned mirror — is a *covered* row, but only when that reachability is
measured, never assumed. State per-adapter coverage from evidence: never tell the
user one adapter is done and the others are "separate work" without having
checked all three first.

Generated output is not exempt from semantic, discovery, or footprint checks.
Runtime syntax may differ, but observable behavior, quality floors, and failure
reporting must remain equivalent.

## 2.1 Runtime Distribution Seam

Installing or exposing the harness in a runtime is its own adaptation seam. A
runtime surface is supported only when the adapter can name the runtime-native
entrypoint and prove that the runtime will discover it.

Use this order when adding a runtime surface:

1. Define the portable invariant in `core/`, `capabilities/`, or `roles/`.
2. Describe the runtime surface as data: kind, destination, invocation syntax,
   conversion rule, hook/config surface, and unsupported fallback.
3. Generate or maintain adapter-owned concrete output from the portable source.
4. Verify runtime discoverability or explicitly mark the surface unsupported.

An adapter must fail closed for unknown or undocumented runtime features. Do not
assume a Claude Code surface exists elsewhere because the purpose is similar.
For example, a runtime with native status, command, skill, hook, or plugin
support should use that native surface first; harness-specific gaps should be
bridged by adapter wrappers.

External reference: GSD Core
(`https://github.com/open-gsd/gsd-core`) uses the same seam shape: canonical
workflow files are transformed into runtime-specific artifacts, while Claude
plugin manifests and Codex skills are concrete runtime projections rather than
portable source. This repo should follow the pattern, not the exact file layout.

## 2.2 Runtime Currentness and Parity Claims

Before answering, planning, or editing adapter projection behavior for modern
runtime surfaces, verify the current runtime documentation and recent practice
instead of inferring from another adapter or from local harness state.

- **Claude Code / Codex surface questions require fresh external research**:
  read the current official documentation first, then inspect local adapter
  realization. Use community posts, issues, or examples only as secondary
  evidence for real-world gaps or practices, and label them as such.
- **Separate existence from parity**: if a runtime supports a feature in some
  form, still state whether it is equivalent to the other adapter's feature.
  Include concrete parity gaps such as model pinning, tool restriction,
  permission inheritance, session/worktree isolation, hook lifecycle, discovery,
  UI visibility, and noninteractive/headless behavior.
- **Separate a report from its exit code**: a diagnostic that consumes another tool's structured report
  must read that report, not only the producer's exit status. Validate the report against a named
  accepted shape; classify a missing, unparseable, or shape-violating report as *unknown* and fail
  closed rather than as a measured pass or a measured absence. When a producer packs several
  independent verdicts into one status, emit one check per verdict. Surface a bounded cause — a fixed
  maximum number of lines and characters, drawn from the single invocation already made — never the raw
  report and never a second run.
- **Plan with verification**: when a projection change depends on a runtime
  capability, the implementation plan must include a current-doc citation or
  note, a local runtime/projection check, and a fallback if the feature is
  unavailable, buggy, or unsupported in this adapter.

## 2.3 Proposal-Gated Runtime Improvement

An improvement proposal adopts a portable invariant, not a permanent runtime
implementation. The evidence loop may observe, reproduce, draft, and compare a
candidate, but it must not edit active source, generated projections, installed
plugins, or runtime-owned config. Adoption is a separate spec/code/release
cycle.

Each runtime realization is version-bound. A runtime, plugin, documentation, or
active-provider fingerprint change requires revalidation; it does not inherit a
past approval. If a native feature satisfies the fixture, retire the custom
realization while preserving the portable invariant and any required fallback.
Semantic conflicts are reviewed, never auto-merged. The operational state
contract is `loops/improvement.md`.

## 3. Portable Role Model

Portable docs use role names, not vendor model names:

| Portable role | Meaning |
|---|---|
| `fast reviewer` | Broad, low-latency review: coverage, style, cross-reference, formatting, simple consistency |
| `fast fact-checker` | Narrow source comparison: citations, years, metrics, verbatim matching |
| `fast writer` | Short mechanical assembly from verified artifacts |
| `deep editor` | Reader-facing prose written for a person to read: final reports, polish, translation |
| `fast implementer` | Routine implementation and refactoring |
| `deep reviewer` | Architecture, methodology, safety, domain correctness, high-risk review |
| `deep maker` | High-judgment creation: planning, synthesis, visual/editorial craft |
| `deep orchestrator` | High-judgment conductor: stage gates, failover, and evidence synthesis for `standard+` dispatch-depth-1 work |
| `external adversary` | Independent reviewer with different model/runtime/process assumptions |
| `orchestrator` | Balanced mechanical coordination of already-decided tooling, paths, and report assembly; not a deep-conductor alias |

Adapters map two independent portable axes: `model_role` describes behavior, while `model_profile` (`deep|balanced-deep|balanced|light|mini`) is the sealed result of the judgment-demand × execution-scope resolver. Each adapter declares concrete models, effort/variant projections, profile granularity, and interactive-main-only families in `adapters/<adapter>/config/models.conf`; every resolver, wrapper, generated agent, lifecycle worker, and documentation table derives from that single source. A route-bound job carries both sealed axes and rejects trailing model/effort replacement; the only sanctioned model choice is the route-sealed `compose --pin` (wrappers report `model_source=pin`) and the checked capacity retry. `mini` is unavailable to substantive registered dispatch-depth-1/2 owners, stages, and reviewers. A profile may share another profile's concrete model as long as the resulting execution points stay distinct; the ladder is a set of operating points, not a set of models. An adapter lacking a verified effort/variant distinction may collapse only the explicitly documented operating point (OpenCode balanced to light, and — when an adapter declares no tier for a profile such as OpenCode `deep` — that profile onto its named tier, e.g. `collapsed-deep-to-balanced-deep`) with reduced-granularity metadata; leaving a tier out of the shipped config is how an adapter says "not for this profile", and the shipped `dispatch-defaults.yaml` leaves that harness out of the profile's band to match. Non-route surfaces may retain checked explicit selection or inheritance when the resulting model is execution-surface eligible; main-only or unprovable inheritance is a typed deny.

Adapter and projection edits are derived core-first: change the portable invariant in
`core/` first, read that governing core document in the current session, then update
the adapter realization and generated projection. A runtime marker proves the read
gate only; it does not replace this source-order review.

Identity publishers retain bounded launch errors and may use one bounded fallback
with the same invocation after a failed launch. A live launched helper must not
be duplicated. Helper exit success records an attempt, not authoritative identity
publication or peer receipt. An OpenCode TUI's actual current session selection
is an independent provenance input only when read inside that TUI from the
native route and bound to the same TUI process lifecycle; it is never estimated
from a callback session id, an SDK-first root, timing, pane labels, or daemon
environment.

Pane identity follows native session-start/resume events: publishers carry the
actual start source with the current ID. Fleet joins predecessor/current IDs from
that pane's existing same-harness, same-repository seat history for display only;
another live session keeps its own ID. These aliases grant no report, wake or write
authority and add no state ledger, user confirmation or caller flag.

OpenCode identity and completion helpers resolve a live harness root at invocation
time if their import-time release has been removed, using the existing core root
order. They preserve the exact session and inherited dispatch registry across that
fallback. Carrier logs name a missing helper or directory, claim and prompt result;
consecutive identical skip observations remain one line.

On OpenCode, non-route deep roles use a deep tier when the selected whole-file model configuration declares one, and otherwise retain the existing balanced-deep fallback. Explicit role edits target that same resolved tier. Registered jobs continue to resolve their sealed model profile independently of role families.

## 4. Capability Model

Registered workers must be able to read their assigned portable contract from
the sealed agent home's `capabilities/` directory. Invocation-local external
path permissions may expose that directory and its canonical target for reads;
they must not expose the whole agent home or grant contract edits. Existing
worker write guards remain in force. A spec-read marker records an observed
read and does not deliver file contents or prove that a contract was read.

Worker runtime defaults must not index the full working directory before the
first turn. OpenCode worker homes disable native snapshots in both their private
config and the invocation's inline config (which overrides project config).
Git and the existing Hearting artifact/checkpoint records retain work evidence;
the worker has no OpenCode UI undo snapshot. Interactive user config is preserved.

A portable capability describes:

- trigger semantics;
- required inputs and artifact roots;
- output contract;
- Verification rigor (intensity-derived) semantics;
- delegation roles using the portable role model;
- deterministic guards and side effects;
- recovery and audit requirements.

A runtime skill/slash command/native instruction describes:

- how that runtime invokes the capability;
- which tools are available;
- how subagents or reviewers are spawned;
- how confirmation, pause, and user input work;
- how hook events are attached;
- runtime-specific file formats and frontmatter.

Current `skills/*/SKILL.md` files are compatibility references. Claude Code
consumes adapter-owned concrete files under
`adapters/claude/skills/*/SKILL.md`. Portable capability meaning belongs in
`capabilities/`.

## 5. Hook Model

Portable hook semantics are named by invariant:

| Invariant | Portable meaning |
|---|---|
| artifact order | New artifacts must be created in the allowed dependency order |
| git state safety | Do not edit during merge/rebase/cherry-pick/detached unsafe states |
| memory recall/inject | Inject relevant memory and expose bounded recall candidates |
| worklog state signal | Surface the configured notes root and board app status without moving or mutating data |
| peer-session steering ledger | Write one append-only, body-free `peer_message_v1` record per outbound and inbound cross-session message (`OPERATIONS §5.14`) |

Adapters decide whether each invariant is enforced by native hook, wrapper,
manual preflight, or unsupported fallback. Realized (steward role, `OPERATIONS §5.14`,
v56 herdr-unified): Claude — `PostToolUse(SendMessage)` + `UserPromptSubmit` (measured,
`hooks/peer-message-record.py`) for the ledger, `utilities/peer-steward.py wait` →
`herdr agent wait` (measured) for bounded foreground watching, and for detached watching
`utilities/peer-steward.py watch/join/status/rearm/ack` plus two carriers — a
`PostToolUse(Bash)` `asyncRewake` hook (`hooks/peer-steward-rewake.py`, exit 2 wakes,
spec-only until a live-session measurement) and a `UserPromptSubmit` sweep of un-acked
receipts in the same `hooks/peer-message-record.py` (fail-soft, ≤5 lines of
`additionalContext`). The carrier reaches watch state only through the utility's
subcommands, never through the state files, so a runtime without a wake carrier keeps the
same schema. Codex — `herdr agent wait` watching is measured; the portable
`watch/join/status/rearm/ack` subcommands work, but there is no wake carrier and next-turn
receipt recovery is unmeasured (P-7); carrier parity is a separate decision. The managed
gateway's `steer`/`watch-idle` ops are **not implemented** (closed-by-decision, P-1 through
P-5 retired, not pending). OpenCode — `unknown`, pending probe P-6.

## 6. Projection Invariant

Runtime homes keep their expected names. Common docs describe this generically;
adapter docs own the concrete runtime-home paths and bootstrap filenames:

```text
<runtime-home>/<adapter-bootstrap>
<runtime-home>/<runtime-settings>
<runtime-home>/<runtime-command-or-skill-surface>/
```

Those paths may symlink into versioned projection directories such as
`claude_setting/`, `codex_setting/`, or `opencode_setting/`. The projection
directory must make it clear whether each entry is native adapter output,
portable passthrough, or compatibility debt.

**Projection completeness**: a cross-adapter guard that checks whether every
portable source item has a corresponding adapter-side projection must
**enumerate the source domain** (iterate the actual current entries) rather
than assert a hardcoded list fixed at authoring time — a hardcoded list stops
catching new entries the moment the source domain grows, silently reopening
the exact gap the guard exists to close. This applies at minimum to agents,
hook events, tools, utilities, and scaffolds. Any intentional exclusion from
projection belongs in an explicit exemption or name-mapping list next to the
guard, never as a silent omission, so every excluded entry is a declared
decision rather than an accidental leak.

### 6.1. Active Context Budget

Progressive disclosure applies to runtime bootstraps and discovery metadata,
not only Skill bodies. The bootstrap is a router: source order, hard invariants,
and runtime entrypoints stay resident; detailed lifecycle explanations,
examples, and edge cases live in adapter README/ADAPTATION documents or command
help loaded on demand.

- Each always-loaded adapter bootstrap is at most `16,384` UTF-8 bytes.
- Each active Skill metadata discovery surface is at most `7,000` characters,
  including its concrete local Skill paths. Regression baselines normalize
  those paths relative to the surface root so checkout location is not source
  growth.
- Activating two surfaces with the same Skill names is a duplicate-discovery
  failure, not extra assurance.
- The same always-loaded bootstrap must reach a session exactly once. When a
  runtime both auto-loads a bootstrap filename and reads a configured
  instruction list, an adapter picks one carrier and keeps the other empty;
  runtimes commonly dedupe instruction sources by resolved path, so one file
  behind two absolute paths — a symlink and its target, or two projections of
  it — is injected twice rather than deduped. Verification asserts the number
  of carriers, not merely that some carrier exists.
- A stored surface baseline warns on growth greater than five percent; the same
  change records a reviewed rationale and updates the baseline.
- Model-visible surface budget: the nine documents an agent reads to route
  and dispatch work (`core/{CORE,WORKFLOW,CONVENTIONS,OPERATIONS,HOOKS,MEMORY}.md`,
  `adapters/claude/CLAUDE.md`, the `autopilot-code` `dev-pipeline` and
  `owner-execution` references) carry sealed per-file byte and directive caps
  in `tools/surface-budget.json` and total ceilings in
  `tools/check-surface-budget.py`. **Directive (rule) caps are fail-closed;
  byte caps are advisory** — going over one prints an `ADVISORY` line and does
  not fail, because the reduction is about what an agent must hold in mind,
  counted in rules, and byte caps that force prose to be squeezed are not.
  Rule caps are per file and independent: a change that adds rules to any one
  surface fails the boundary check even when another shrinks. Each cap sits one ordinary edit above its measurement —
  3% of the bytes, and two directives or 3%, whichever is larger — so a normal
  change has room to land; caps sealed at the exact measurement made all nine
  surfaces permanently full and pushed growth into skipping the gate instead.
  The margin is finite and cannot be widened by repetition: a reseal takes it
  from the current measurement, never from the previous cap. Growing a file
  past its cap means resealing in the same change with a recorded `--reason`,
  and a reseal is refused outright when the sealed rule caps would exceed the
  code rule ceiling. Reductions are locked in by resealing downward. The ceiling is
  lowered only in a commit that lands a measured reduction and is never raised
  by editing the budget file.
- Ordinary, unknown, and repeated hook states inject zero bytes. A verified
  pressure-band transition may emit one compact directive of at most 240 UTF-8
  bytes.

These are footprint controls, not token or billing estimators. Static bytes,
code lines, directive counts, and monotonic runtime counters must not be
converted into savings claims. A production savings claim requires at least 30
paired real sessions and separates input, cache creation, output, and billable
cost. Synthetic fixtures prove regression behavior only.

## 7. Completion Delivery Carriers (runtime-owned)

The model-visible contract is one printed field (`core/OPERATIONS.md §5.10a`,
`core/HOOKS.md` "registered-child completion delivery"): a parent obeys
`parent_next=end-turn|bounded-wait`. Everything below is how the runtime
honours that field. It was moved here verbatim on 2026-09-09 from
`core/OPERATIONS.md` §5.10/§5.10a/§5.14 and `core/HOOKS.md` so that no agent
prompt carries the carrier taxonomy (dispatch-complexity diagnosis
`rrev_c5dd77e9` R3); the SD references and the leaf
`utilities/parent_next_directive.py` are unchanged.

### 7.1. Registered owner supervision (SD-14/78/92/113)

Parent close uses the same shared cancellation controller for Claude, Codex
and OpenCode, whether the owner is in a model turn or parked on children.
Native turn/session interruption alone is not route closure: exact OS process
identity and owned-child cleanup are the checked common fallback. Supervisors,
completion joins and post-exit observers read the existing cancellation intent
before delivering gates, reconciling results or starting another turn/stage.
The caller's existing `close` performs termination and closure; resources stay
live unless its optional `--stop-resources` selects the exactly linked runs.
An interrupted close is continued by the existing execution observers. No
adapter adds a cancellation command, reason requirement or approval step.

Owner corrections are accepted independently of delivery. Codex active-turn
steering is only accepted after a matching turn response; Claude and OpenCode
next-turn transports deliver on a later owner turn. A parked owner receives a
queued correction once the children running now finish or require attention;
a serial sub-session chain starts no further sub-session while a correction
waits (`drive_serial_chain` `allow_advance`), so the wait is the running
sub-session, not the rest of the chain. The shared `correct` response must
describe this delay and state
that it does not wake or cancel the owner; acceptance is not proof of delivery
or application.

- **Runtime-owned completion delivery under SD-14/78:** a registered `standard+` headless owner is launched under an adapter supervisor, not as an unresumable one-shot model turn. The model registers every separable child in the current batch and yields `runtime_wait: registered-children`; the supervisor snapshots only current v2 rows sealed to `parent_attempt_id=$AGENT_DISPATCH_ATTEMPT_ID`, joins every parallel attempt through canonical liveness outside the model/tool loop, and sends the same session exactly one bounded typed receipt when the whole batch is semantically terminal **and execution-quiescent**, or requires typed attention. Child output, transcript text, artifact bodies, source, git state, and liveness prose never enter that receipt. Codex realizes the bridge with one ephemeral App Server thread and repeated `turn/start` after `turn/completed`; a registered Claude owner realizes its internal batch bridge with one `--session-id` followed by `--resume`. An interactive Claude parent uses a separate `PostToolUse(Bash)` `asyncRewake` bridge: only an owner attempt whose lock-written row in the session's trusted registry (inherited `AGENT_DISPATCH_JOBS`, else the installed canonical registry) carries the same session, `worker_type=owner`, dispatch depth 1, `parent_completion_delivery=claude-parent-runtime`, and claimed/started evidence may arm it — a start receipt on stdout names the candidate, the row proves it, and a receipt naming any other file binds nothing. Each Bash call may take one unclaimed row started inside a bounded recent window through the arm ledger (one waiter per attempt; a wave of starts arms one per call, oldest first) or re-take a claim of its own that lapsed, whatever the row's age, and a row nobody can prove arms nothing, because absence beats misattribution. It watches that one owner attempt to terminal quiescence outside the model, and exits once with a bounded exact-attempt receipt. It never launches a visible background `dispatch-wait`, Monitor, progress recap, or periodic re-arm; explicit `poll-fallback` remains the only model-owned wait. Intermediate turn/result events are withheld from the terminal handoff, and only the final exact three-line envelope is exposed as terminal. Before every model turn the supervisor atomically publishes an attempt-scoped schema-v2 phase state: `parked`, `deliverable`, `running-turn`, `recovery`, or `terminal`. While an undelivered child is open or terminal-but-draining, the native pre-tool policy admits only one exact same-parent `dispatch-batch --action start` for a declared parallel group (or a non-group exact `dispatch-node --action start`), so a first child cannot prevent its checked siblings from registering. Once any delivered child remains open or draining, the policy admits only exact typed harvest for that delivered batch. Both phases reject model waits, raw inspection, liveness, unrelated tools, and shell composition; missing/invalid phase state is recovery-only exact harvest. Codex enforces this through its projected hook and Claude through a command-scoped `--settings` PreToolUse bridge without mutating user-owned runtime settings. Multiple sequential route batches repeat this one-resume transaction. A bounded join timeout is an internal repark checkpoint: it emits no model receipt, does not update the delivered set or consume continuation budget, and makes the same supervisor rejoin the same sealed child set. A second, distinct internal repark checkpoint (SD-119) advances a serial sub-session chain registered under `utilities/stage-session-chain.py`: once the joined child is a chain participant and terminal, the supervisor claims and starts the chain's next index itself, folds the closed predecessor into the delivered set so it is never re-surfaced, and rejoins — again with no model turn and no continuation spend — until the chain either completes (falls through to the ordinary route-level flow) or the joined child carries no chain metadata at all. Only terminal-and-quiescent or a typed attention condition is actionable. An exception with owned open children preserves state and lease in `recovery`; only terminal-and-quiescent completion removes them. A dispatch-depth-0 interactive Codex parent has a separate native realization: a direct registered dispatch-depth-1 attempt bound to the actual `CODEX_THREAD_ID` seals `parent_completion_delivery=codex-stop-hook`; `launch_claimed=0` registration alone never parks the parent, and a start requires the **current exact Stop and PreToolUse hook definitions** to be trusted before it may claim or spawn the process. Immediately after successful spawn, the wrapper atomically binds the exact attempt into hashed-session pending state, then the parent ends its model turn. Stop follows that immutable set even if an orphan watcher has already changed a child row to `done`, joins it outside the model, publishes the delivered phase, and returns one bounded `decision=block` continuation only when exact harvest is ready. While undelivered, PreToolUse admits no model tool—including `dispatch-wait`; after delivery it admits only exact-attempt harvest with `--status all`, so an `open`→`done` watcher transition cannot invalidate the continuation. A valid harvest consumes its exact receipt, and the final receipt removes the session state. A bounded Stop timeout yields one minimal end-turn/re-enter instruction rather than a polling tool loop. Foreign, legacy, registered-only, untrusted, or unstamped rows never enter this path and retain the explicitly reported polling/recovery contract; the recent-window bound governs a *new* claim, while a claim this session already holds re-arms whatever the row's age or terminal state, until the wake it owes is delivered. Runtime support is probed before launch: forced supervised mode fails closed, interactive native Stop delivery fails before spawn when current-hash trust cannot be proved, while other unavailable same-session bridges may use the explicitly reported `poll-fallback` (`dispatch-wait --attempt-id <id> --max 300..600`). After a supervisor has started, protocol/session failure never replays the assignment through a one-shot fallback. Arbitrary detached shell output still does not auto-resume; only the checked completion-delivery surfaces above do. Parent ownership remains exact, foreign or stale rows never wake the owner, and post-exit orphan reconcile remains mandatory and independent. Execution supervision is separate from route binding: a registered quick/solo depth-1 owner also runs under its harness supervisor when the host probe reports support (OpenCode, which has no probe, always does), carries its sealed `route_*` tuple without a standard `owner_route_*` binding, and so accepts `capability-route.py correct` from registration on; an unsupported host keeps the one-shot launch with no input state.
- **Serial-chain realization (SD-119):** Claude, Codex, and OpenCode owner supervisors use the shared reconcile-before-advance driver, reset repark bounds per successor, and deliver one aggregate completion through their existing runtime path. OpenCode's headless wrapper invokes the common supervisor with `--runtime-harness opencode`; this establishes common serial-chain semantics, not measured native receipt parity. The sealed proof binds the canonical pointer/original digest and exact bidirectional parent rows. Completed sub-sessions are delivery-success rows, while refused chains close only proven never-started successors and report the bounded refusal notice. An owner turn that ends with its final handoff closes the successors it registered and never started the same way (`dispatch_completion_join.cancel_unstarted_chain_successors`), so a chain a correction stopped ends with the owner's result rather than a demand to start them; a post-exit cascade records a never-started child as `launch_outcome=never-launched`, so its cleanup settles.

- **Launch-publication settle under SD-14/78:** an owner turn may end in the narrow interval after atomic child registration but before every fenced wrapper has appended `launch_started=1`. Every owner supervisor (Codex App Server, and the Claude session supervisor that also runs OpenCode owners) therefore performs one short, bounded reread of only undelivered exact-parent rows before issuing `registration-required`, whether or not the turn ended with `runtime_wait: registered-children`; the sentinel matters only when no child is registered yet. A batch that reaches the existing durable `launch_started=1` fence during that settle window parks and joins normally without consuming a continuation or replaying the dispatch; a row that remains registered-only still receives the existing bounded correction. The settle loop (`dispatch_completion_join.settle_runtime_wait_children`) holds no registry lock, accepts no artifact, transcript, PID guess, or stale delivered row as launch proof, and never starts or retries a child itself.

- **Native interactive Codex completion (SD-92 v105/v115):** the caller's
  `CODEX_THREAD_ID` is the parent identity. The interactive launcher and managed
  gateway are retired; installation restores only manifest-owned launcher paths
  transactionally and preserves modified or foreign successors. Existing panels
  can use their native upstream queue endpoint until they exit. Registered
  headless owners retain their separate App Server supervisor.
- The exact-batch sidecar uses `thread/queue/add` with a stable
  `clientUserMessageId`. Delivery is **at-least-once**: successful submission is
  accepted without an item identifier; ambiguous sends may be repeated. Before
  every send/retry it checks exact consumed history and `thread/queue/list`.
  Pending or consumed items suppress resend. The durable Hearting ledger remains
  open until exact consumption or explicit parent harvest is proven; queue
  acceptance alone never means the parent processed a result.
- An interrupted parent is restarted with `thread/queue/start` only for one
  unambiguous pending Hearting item. Human-only queues are untouched. A refusal
  remains pending and visible for recovery; the sidecar never calls
  `thread/resume`, subscribes, changes settings, or answers approvals.
- The native TUI owns questions and approvals. Default-mode unanswered questions
  may proceed with empty answers after 120 seconds; plan-mode questions remain
  pending. Empty answers are not user decisions. No gateway question rewriting
  remains. Fleet uses rollout/panel evidence for question display. Structured
  async question acceptance is not an answer: retain its content-free wait
  through ordinary activity and turn completion until every exact question ID
  is replied to, the request fails, or its originating turn is interrupted.
  A new turn or a completion message alone does not answer an async question.
- Child runtime does not select the parent carrier: Codex parents use the native
  queue for every child harness, while Claude parents keep their existing
  runtime carrier. Stop continuation and model-owned parking remain retired.
  Native queue behavior is checked against installed Codex; undocumented runtime
  failures retain the durable pending record and an exact operator recovery path.

**`parent-runtime-supervised` completion delivery (SD-113).** A row whose
`parent_completion_delivery = parent-runtime-supervised` never gets a
pending-delivery record — its completion delivery is owned solely by the
SD-78 supervisor above (§7.1), not by the `delivery_intent`/`RECIPIENT_KINDS`
stamp path in `core/HOOKS.md`.

### 7.2. Completion delivery clarifications (SD-92/97, SD-123/129)

- **Human gate in flight (SD-123 (8), SD-129).** While the armed Claude
  `asyncRewake` hook or the runtime-owned Codex completion sidecar waits on an
  open owner attempt it also watches, once per interval, for
  a pending gate record addressed to this session and raised by that attempt;
  when one appears the hook spends its single wake immediately (exit 2,
  `owner=waiting-or-parked`), leaves the record `sent-ambiguous` (the wake is
  speculative, so the next-prompt sweep can still re-deliver it once if the
  wake was lost; the release retires it either way), and tells the session to
  put the `[방향 확인]` card and interview questions to the user and record
  the answer with `workflow-supervisor.py release`. The hook records which
  gate record it spent that wake on (arm ledger, below), and the first Bash
  call this session makes after that record closes — the `release` command
  itself retires it (`retire_gate_delivery`); the next-prompt sweep acks it —
  re-arms a new hook process on the owner's completion exactly as the start
  did, whatever that command was. So the release is still the second arming
  event. A record closed elsewhere — a release from another pane, one the
  supervisor refused as already released (SD-OPEN-48: the owner released its
  own gate and is running towards a completion that still owes the session a
  wake), or the sweep's ack — re-arms only at the bound session's *next* Bash
  call; a session that makes none is served by the next-prompt sweep, not by
  a wake. No command text or JSON output is read. A route with no started
  open owner (the owner ended at the gate, or was refused at start) arms
  nothing; the `UserPromptSubmit` sweep still delivers the pending record at
  the next prompt. A raise seals `release_authority` (`depth-0` for an
  interview gate, a binding that declares it, or an artifact that declares it
  about itself; `any` otherwise): a registered headless owner's `release` /
  `gate --release` of a `depth-0` gate is refused typed
  (`gate-release-authority-refused`), and an artifact that calls itself an
  interview under another schema is refused at the raise
  (`interview-schema-unsupported`). While waiting, an announced-but-unclaimable gate record never spins
  the hook: the probe skips records whose reclaim budget is spent (the release
  expires such a record as `receipt-row-superseded`) and sleeps one interval
  after an empty announce, under the same overall deadline; it reads the
  registry once and rescans the recipient directory only when a record was
  written or a lease it saw has expired. The launch fence
  (`dispatch_contract.completion_marker_gate`) refuses a node whose entry gate
  is unreleased only for gates that some node of the route raises through its
  continuation **and** that an owner contract implements
  (`dispatch_contract.FENCED_HUMAN_GATES`, today `frame-review`). The fenced
  set is unchanged, but every `autopilot-*` recipe now raises `frame-review`
  from its bootstrap frame legs and fences it at its own first work node, so
  that binding is a required step wherever those recipes declare it. A gate
  that some other topology only declares — with no node raising it through a
  continuation and no owner contract implementing it — still is not.
  A depth-0 session waits on `workflow-supervisor.py await-release`
  (bounded, read-only), and every launch surface refuses to start a node whose
  entry gate is not released (`human-gate-unreleased`/`human-gate-not-raised`).
  An owner that ends `BLOCKED` at a gate it raised after it started, while the
  nodes that gate holds back have not begun, is parked, not failed
  (`dispatch_replacement.owner_parked_gate`, a read-only judgement over its typed
  handoff, the route journal, completion markers, and stage rows). `resume_command`
  reports it as `waiting-human-gate` with the exact release command. A person's
  `release --decision proceed` then reports `owner_continuation` after the ledger
  write: `owner-live` when an owner is still running, the shared `start` receipt when
  it started the continuation owner (bounded by `OWNER_CONTINUATION_TIMEOUT_SECONDS`,
  100), or `not-started` with a `resume_command` that finishes the same start later.
  Only a person's release starts it; a registered headless owner's release does not.
  The continuation is the family's one automatic replacement, so
  `recovery_instructions` tells every replacement or continuation owner to keep
  waiting on the bounded `await-release` at a later gate instead of parking. A
  replacement owner's launch receives the route's own open cycle environment, so
  its producer binding is published like the original owner's. The
  Claude wake and the shared completion follow-up describe a parked owner as
  paused at the gate, not failed. A revise or stop recorded while the owner is
  parked starts nothing automatically.
  An owner that ended `BLOCKED` outside any declared gate is waiting for an answer:
  `resume_command` reports `owner-blocked`, and `capability-route.py correct` keeps
  the answer and continues the route in the same call. The continuation is a
  replacement owner whose shared `recovery_instructions` carry the answer
  (`dispatch_replacement` `corrected`), the same on every adapter.
  A Codex delivery uses a distinct strict `human-gate` receipt and `hg-dlv-*`
  gateway identity. It binds the route id/hash/file, gate raise epoch, exact
  live owner attempt and sealed batch, immutable registry, recipient thread and
  gateway epoch, artifact, journal, and release authority before claim; the
  gateway validates the same receipt before one start/steer and never interprets
  it as a completion receipt.
  If a malformed historical route or terminal owner makes a `pending` or
  `sent-ambiguous` record impossible to release, an operator may preview its
  cancellation and then repeat with `--apply`:
  `python3 utilities/workflow-supervisor.py recover-gate-delivery --route
  <route.json> --gate <gate> --delivery-id <delivery-id> --recipient
  <parent-session-id> --source-attempt-id <att-id> --raise-epoch <n> --actor
  <operator-id> --reason <audit-reason> --jobs <canonical-jobs.log> [--apply]`.
  Recovery accepts only that exact route/gate/epoch/delivery/recipient/source
  tuple and a terminal registered owner; it rejects a claimed carrier, a live
  owner or different live gate, records an audited `expired` state atomically,
  and never writes a release, deletes the route/registry/marker/record, or turns
  the source attempt into PASS.
- **Supervision wake handoff.** A supervision warning does not wait for a
  human gate release. After emitting its in-wait wake, the Claude carrier
  returns only its own supervision-notice claims to the existing queue. The
  synchronous prompt sweep on the awakened turn can consume or retire them
  immediately, allowing that turn's next Bash call to re-arm the same attempt
  for completion. At the end of a turn, the same async carrier also runs on
  Claude `Stop` and may re-take only an existing arm bound to that session;
  it never first-arms a fresh row there. Thus an attention turn with no Bash
  call still resumes the exact completion wait, while a live holder or an
  ended arm stays quiet. The async emit is not an acknowledgement; a lost wake stays
  recoverable. Human-gate notices retain their existing speculative lease and
  release semantics, and another carrier's successor claim is preserved.
- The interactive Claude `asyncRewake` bridge never reads the Bash command
  text (2026-09-09; six consecutive reviews had each found a new hole in the
  shell parsing that decided which owner a command had started). It
  identifies the owner from what the launch wrote: the start receipt's
  `attempt_id` (`status=start`, `started=1`, `parent_session_id` = this
  session, `job_registry`), else the registry's open, claimed-and-started
  depth-1 `worker_type=owner` rows stamped
  `parent_completion_delivery=claude-parent-runtime` and bound to this
  session — a receipt-named attempt is re-proven by that row, and only the
  trusted registry is read: the inherited `AGENT_DISPATCH_JOBS` when set (an
  unusable value trusts nothing), else the canonical one; the owner selector
  refuses an explicit `--jobs` elsewhere before spawn
  (`explicit-jobs-outside-parent-registry`). An arm ledger under the registry's state root
  (`rewake-arms/<attempt_id>.json`: flock, holder process identity, state
  `waiting`/`gate-wake-sent`/`lapsed`/`ended` — `ended` sealed only after
  the receipt went out or another carrier took the completion; at most eight
  arms; `ended` records pruned after seven days, lapsed or provably-dead
  holders after thirty, a holder that is alive or unobservable from this PID
  namespace never) makes arming exactly-once within one PID namespace: every
  `PostToolUse(Bash)` of the session — an ordinary `git status` included — may
  take one unclaimed fresh open row (oldest first, started inside the arm
  window), so a wave of starts arms one waiter per call, a filtered start
  receipt costs nothing while the row is open, any launcher prefix works, and
  a foreign command that merely mentions the utility cannot re-arm an attempt
  already watched. A claim whose holder died (session restart, `--resume`),
  lapsed (timeout, bridge error), or spent its wake on a gate record that has
  since closed is re-taken regardless of row age, even after the row ran to
  `done` (the wake is still owed); `ended` never is. An owner that finished
  before any claim existed is the next-prompt sweep's. Open gates are folded
  only into a receipt this process actually emits and acked only after it
  went out; a hook that lost the completion claim leaves them for the sweep.
  A start whose receipt proves `started=1` but whose attempt no hook process
  holds emits one typed `not-armed` notice naming the explicit poll-fallback.
  Each carrier exit appends a best-effort diagnostic line beside its arm in
  `<attempt_id>.exits.jsonl`, preserving time, holder/arm identity, wait reason
  and raw return code, exception or TERM/HUP signal across re-arms. These
  observations never decide completion or delivery; SIGKILL cannot be recorded
  by the killed carrier.
- Managed receipt schema v2 binds the one canonical absolute `job_registry`
  supplied by its completion sidecar. The gateway includes it in the delivery
  digest and names it with `--jobs` in every actionable harvest command, so
  packaged `AGENT_HOME` is never used to reconstruct the registry and an exact
  receipt cannot become `matched=0` by selecting another state root. The receipt
  remains bounded to 2,048 UTF-8 bytes; the complete typed context has its own
  finite bound.
- An SD-92 managed-gateway readiness refusal carries exactly one typed
  `reason_class` from a closed five-member set —
  `expected-thread-not-witnessed`, `lineage-mismatch`, `tui-disconnected`,
  `approval-owner-mismatch`, `upstream-client-count-invalid` — chosen by
  evaluating the conditions in a fixed documented order so exactly one class
  applies. This is diagnosis only: the portable aggregate outcome token
  `managed-gateway-not-ready` and the advanced-thread acceptance rule at
  `core/OPERATIONS.md` §5.10 ("SD-92 advanced-thread clause") are unchanged, and the existing pre-status
  typed reasons (`managed-entry-not-enabled`, `managed-parent-runtime-mismatch`,
  `managed-parent-harness-mismatch`, `managed-parent-thread-mismatch`,
  `managed-control-missing`, the `managed-control-*`/
  `managed-state-directory-unsafe` socket reasons, and the `managed-status-*`
  framing reasons) are disjoint from this set and stay unchanged.

### 7.3. Detached steward watch carrier (SD-122)

**Detached watch realization.** `peer-steward.py watch <target>` persists one logical
obligation with exact herdr server, pane, harness, session and until-set identity before
starting its observer. Each herdr wait has a finite budget; timeout and unavailable
are checkpoints, not fulfillment. The existing observer continues after those checkpoints
with capped backoff until the exact condition is met or the process stops. Existing command,
session-start and reconnect paths claim the same duty and replace only observer identity,
so tab moves do not require manual rearm and same-name pane/session reuse cannot inherit
the old duty. A tab is a current location hint. The duty remains pending until its exact
observable condition is satisfied, explicitly canceled, or safely handed through its
recorded identity lineage. `join`, `status`, and Fleet report the duty separately from an
observer receipt. Wake remains adapter-specific and any unmeasured native receipt remains
unmeasured; no always-on daemon or model polling loop is introduced. Watch observations
continue to use bounded herdr calls. Idle/done receipts require the shared
completion-readiness policy: exact registered-parent session or confirmed handover
bindings take precedence over native idle, without requiring a parent-pane field.
Peer input uses the same policy with an input purpose: a parent's idle native
turn can receive messages while its registered children continue. Only work
executed by that pane withholds input; exact foreground attempt tags distinguish
execution from parent or handover bindings. Completion watches and retire keep
all bindings. After installation, the existing reconnect callback resumes accepted
message duties with the activated code and keeps retrying after a form or draft clears,
even while a legacy runner holds its lock. One current runner uses a stable lock;
the sealed transfer claim and receipt still prevent duplicate submission. Retire
processing also holds the legacy runner lock, so the two generations cannot perform
session cleanup together. No runner is interrupted to reconnect message delivery.
Non-completion conditions such as working retain their native watch meaning.
Duplicate watch requests resolve the current server/pane/harness/session before
reusing a duty; a new session gets a separate duty while the old one is preserved.
Message observation and transport keep the accepted server selected throughout.
Retire observations update only their current phase under the existing duty lock;
stale observations cannot undo an exit claim or reopen a completed/cancelled duty.

On Claude, `PostToolUse(Bash)` `asyncRewake` hook `peer-steward-rewake.py` arms only from a
same-session armed line with `wake=hook` whose receipt sits under the canonical state root,
`join`s within one deadline computed at hook entry, acks, and exits 2. Its survival across
user interrupt, compaction, and session end is **unmeasured**, which is why receipt
durability and the fallback carrier are mandatory: the `UserPromptSubmit` sweep surfaces up
to five un-acked receipts for the current session and acks them, so a dead hook or a
restarted session loses nothing. Wake is at-least-once and display is idempotent — the ack
file is created `O_EXCL` by whichever carrier gets there first.

### 7.4. Carrier taxonomy and selection

Startup/resume context is not a processing turn. `dispatch_session_sweep.activate`
reads notices without a claim or acknowledgement and reconnects the existing
peer courier, using the adapter's declared carrier and exact receiving session.
Claude SessionStart, Codex SessionStart and OpenCode session creation use that
same rule. A real prompt or accepted native turn acknowledges the notice.
The courier follows confirmed session handover, retains messages behind drafts
or forms, and never relaunches a missing parent or types into a shell pane.
Unreceived notices stay visible in Fleet until a recipient processes them.

A registered headless owner yields after registering a batch; its runtime supervisor joins the exact `parent_attempt_id` batch outside the model and resumes the same owned session once with a bounded typed receipt. A registered Claude owner keeps one realtime stream-input process for the route and submits the next receipt immediately after each non-terminal join; when a freshly verified terminal marker closes every declared terminal gate, the supervisor skips the redundant final owner turn and closes the stream before terminal row reconciliation. An explicit custom-command fallback retains per-turn `--resume`. A Claude interactive parent may instead arm one native `asyncRewake` PostToolUse hook from a successful exact owner-start receipt, or from a successful exact steward watch armed in the same session (`utilities/peer-steward.py watch`, SD-122 §13.37.2-(10)); either way the hook owns exactly one arming event, verifies it against the session that produced it, and never widens to another attempt or watch. It re-reads the exact current row and sealed completion evidence before rendering: every terminal receipt — success or attention — exits two, because Claude Code wakes an idle session for an `asyncRewake` hook only on exit code 2 and delivers exit-0 output no earlier than the next user interaction (corrected 2026-08-29; success additionally carries its structured notification on stdout). A launcher-managed interactive Codex session places one owner-only gateway between remote TUI and App Server. The harness installer may make this checked entry transparent for interactive commands, but plugin or hook loading after process entry is not equivalent. That gateway atomically serializes manual input with an exact completion receipt, uses `turn/start` only while idle and `turn/steer` only for a steerable active turn, and durably suppresses duplicate sealed-batch delivery. The sidecar is prelaunched before the child spawn claim, connects only to the private control socket, never subscribes upstream, never sees or answers approvals, and submits no raw child output. A send followed by an unclassified disconnect is `sent-ambiguous` and is not retried. Outside those checked entries, hooks must not simulate wake by blocking Stop, parking every tool, or injecting a synthetic user turn; the parent remains conversational and uses a disclosed finite fallback. Legacy receipts may be consumed only by exact terminal typed harvest. Runtime-native subagents are a separate surface.

Shared parent-delivery selection, immutable registration checks, and pre-spawn sidecar arming belong to `utilities/dispatch_parent_completion.py` across all three adapters. A child adapter never infers the parent runtime from its own name. What a parent runtime carries is data its adapter declares in `adapters/<harness>/config/harness-capabilities.json` (`parent_completion`: the carrier, what proves it reaches the parent, and the fallback when it cannot); the shared selection reads that declaration instead of branching on a harness name. Select delivery by parent runtime: Codex managed parent → Codex gateway; Claude interactive parent → exact owner or exact steward-watch `asyncRewake` with an exit-2 wake for every terminal receipt, plus the SessionStart/UserPromptSubmit sweep that re-delivers any SD-111 pending record or un-acked watch receipt at the next prompt; OpenCode interactive parent → its plugin's turn carrier (`opencode-turn`), which, while the session is idle, hands the records owed to it to the session's next turn through `promptAsync` and acks them once OpenCode takes the turn — its tool commands name that carrier in `AGENT_PARENT_COMPLETION_CARRIER`, so a server that predates the carrier keeps `poll-fallback`; all three render the delivered records with the one shared `dispatch_session_sweep.delivery_context`; registered Claude owner → persistent realtime stream with a sealed-terminal fast path (checked per-turn `--resume` fallback); registered Codex headless owner → its private App Server supervisor. Keep TUI client A as the only approval owner and sidecar client B control-only. Require private socket/state paths, exact terminal+quiescent membership, durable idempotency, bounded typed context, and fail-closed ambiguity. A transparent launcher must preserve and validate the real CLI, route only interactive surfaces, repair on update, and restore exactly on uninstall. If those checks are unavailable, report fallback and the missing atomic `continueIfIdle(threadId, idempotencyKey, typedContext)`/native async-rewake primitive rather than widening Stop or PreToolUse.

A Claude owner's async-rewake carrier retries a timed-out readiness probe within
its existing absolute deadline. A transient probe timeout does not end the wait
or require another user prompt; it grants no completion or replacement authority.

The parent supervisor's awaited snapshot is the verified route generation and
its exact current batch (including a validated owner-route advance). Same-parent
attempts belonging to another route stay outside that receipt and keep their
independent cleanup ownership; receipt equality checks remain exact. Never-started
settlement shares the durable launch-fence proof across join, partition, and
terminal closure. Frame notices remain pending through an ambiguous send and are
considered consumed only after the recipient's exact route decision is recorded.

## 8. Dispatch Decision Record (SD ledger, runtime-owned)

Each entry is the full original `core/OPERATIONS.md §5.10` item, moved here
verbatim on 2026-09-09 (dispatch-complexity diagnosis `rrev_c5dd77e9` R2): the
rule an agent acts on stays in §5.10 as one to three sentences; the mechanics,
evidence order, and incident history that justified it live here for the
runtime maintainer. Nothing in this section is a prompt-carried rule. SD
references are unchanged; "above"/"below" inside an entry refer to the
pre-move §5.10 order, which this list preserves.

### 8.1. Cross-harness routing under SD-16

before dispatch, query each harness through `utilities/usage-check.sh`, which reports `ok`, `limited(reset)`, or `unknown`. The cascade is explicit target, hard eligibility, sealed affinity/policy, the balanced usage gate, quality band, then allocation ordering; optional `depth_affinity`, `depth_affinity_weight`, and `usage_headroom_exponent` default to `{}`, `0.5`, and `1`. In the shipped `balanced` strategy, candidates in the same band blend the last bounded window of exact registered attempts with fresh remaining headroom through one continuous deficit key: headroom defines each peer's target share and the recent count records how much of that share it already consumed. Equal headroom therefore reduces to exact recent-attempt round-robin, while a widening headroom gap moves the rank continuously instead of adding another threshold. The configured 90% usage boundary (`allocation.usage_gate_used_percent`, configurable 0..100) partitions candidates across quality bands, not within one: while any ungated candidate (including one with an unknown gauge) is hard-eligible, no gated candidate is selected in any band, regardless of band precedence; it does not replace explicit target or typed hard eligibility, which still win the cascade. Within a gate class the quality band, relief promotion, `last_resort`, sealed affinity, and the continuous deficit key are unchanged. If every candidate is gated, maximum fresh headroom is compared across all bands before quality-band or affinity ordering; existing ordering breaks only equal-headroom ties. Unknown gauges pass the balanced gate optimistically and use the neutral ordering share, while exact death markers from `usage-check.sh` remain hard exclusions. The legacy `capacity-aware` strategy remains valid: it excludes unknown/zero headroom and orders by fresh headroom, then count and declared order; its relief promotion semantics are unchanged. Quality bands, sealed affinity/policy/fallback_hops, and D-41 caps are invariant, and balanced acts only at compile-time allocation ordering. `HARNESS_CAPACITY_BIAS` may reorder a band but never crosses its quality boundary. A user prohibition is stronger than both signals. Schema-v3 shipped defaults use Claude/Codex/OpenCode as light/mini rotation peers, while deep and balanced-deep keep OpenCode last-resort. `${XDG_CONFIG_HOME:-~/.config}/hearting/dispatch-defaults.yaml` remains user-owned, and installation creates it once without overwriting it. Once a route is compiled its depth-2 nodes carry sealed `harness_affinity` and `harness_policy` snapshots and the record top-level carries `dispatch_defaults_digest` plus `dispatch_allocation`; `verify` checks the hash seal and never reloads live config. This is kept separate from `registry_digest`. Two observability rules keep a configured policy from silently not applying (2026-08-29): `dispatch-defaults.py validate` prints non-fatal `warning=` lines when the user-owned file runs a strategy other than the shipped default or carries an optional key its strategy never reads (`utilities/dispatch_allocation.py inert_allocation_keys` is the one table of which keys each strategy honors; under `capacity-aware`, `usage_gate_used_percent` and `usage_headroom_exponent` are inert and `depth_affinity_weight` is only a headroom-margin tie-break), and install, `harness verify`, and `harness config status` surface those lines as the `drift` state. Every realized depth-2 allocation — single-stage fallback and each parallel-batch leg — also appends one row to `<dispatch state root>/allocation/<route_id>.jsonl` (`utilities/dispatch_allocation_receipt.py list|summary --since 7d`) carrying the sealed strategy, preferred harness, rank, headroom, counts, inert keys, and the chosen child keyed by `attempt_id`; the stdout receipt names the row as `allocation_receipt=`/`allocation_ledger=`. A policy change is complete only when this ledger shows the new field on a real attempt, not when the file validates.

SD-160 compatibility is exact: new routes seal `persona_independence_contract_version: 1`; an older route retains its JSON/hash and is accepted only when its whole registry digest matches the prior projection obtained by restoring the removed cross-harness group axes. Unit-catalog drift and any registry edit that reaches the route's own capability or a shared section remain refusals; an edit confined to another capability leaves the route current through its sealed `capability_registry_digest`. Legacy frame personas are the existing primary/alternative roles; new routes seal those perspectives explicitly. This does not enable general stale-registry launch.

### 8.2. Checked fallback chain under SD-50

a standard+ stage ranks checked direct-headless candidates through its sealed quality bands, then falls through to `native-subagent -> inline`. A candidate still records whether it is `same-harness-headless` or `cross-harness-headless`; those labels describe the selected tuple and fallback trace, not a durable preference that can override explicit choice, hard eligibility, sealed affinity, or schema-v3 quality/capacity allocation. Every headless candidate carries the same route id/node, write scope, completion gate, logical parent, stable attempt identity, and checked tuple evidence. The conductor invokes eligible adapter wrappers in that sealed/ranked order and proceeds only after a recorded launch failure. Native and inline degradation record skipped candidates, failure classes, registry attempt ids, allocation/headroom evidence, and assurance compensation without claiming Fleet parity.

### 8.3. Direct headless launch under SD-61~63

dispatch contract v3 has no resident launch broker, request spool, broker heartbeat, broker lease, or broker fencing identity. A standard+ dispatch-depth-1 conductor invokes the checked adapter wrapper directly for every same- or cross-harness dispatch-depth-2 attempt. The selected checked tuple's parent harness, transport, and sandbox must equal the actual launching wrapper identity exported in `AGENT_DISPATCH_CURRENT_*`; a missing partial identity or mismatch fails before child wrapper invocation. The canonical registry first records the stable attempt as `launch_claimed=0`. The checked wrapper then spawns a parent-death-safe pre-exec fence while holding the registry lock and atomically publishes the complete PID/start/namespace/leader-PGID identity together with the only `launch_claimed=1` transition. The fence records `launch_started=1` under the same exact row immediately before payload `exec`; a launcher lost before spawn leaves a retryable registered row, and a dead fence that never recorded start may be reset only after exact process-group quiescence. A duplicate or already-started claim never creates another child. A Codex dispatch-depth-1 capability owner running with `workspace-write` receives `sandbox_workspace_write.network_access=true`, `AGENT_NESTED_HEADLESS_NETWORK=1`, and a dispatch-state writable `CODEX_HOME`; that home links existing auth/config read-only and keeps mutable nested session state inside the owner sandbox. Dispatch-depth-2 workers do not inherit the network widening. Contract-v1/v2 route and broker state are read-only migration inputs. The broker utility may expose diagnostic `status` and idempotent `stop` for one compatibility release, but production dispatch never calls `ensure`, `request`, or `serve`.

### 8.4. Namespace-safe launch lifecycle under SD-72

`dispatch-chain` selects the child lifecycle from the actual launcher scope for both same- and cross-harness candidates. Because an adapter wrapper may enter a narrower transient namespace after that selection, the incoming lifecycle is provisional: every wrapper re-evaluates its own scope before registry reservation or attempt creation and atomically promotes `detached` to `foreground-scoped` when the actual scope is transient. That pre-registration promotion is normal selection, consumes no attempt or retry budget, and is recorded with both selector observations; `dead-nested-sandbox-lifetime` remains only a legacy/recovery classification for a caller that bypasses the checked wrappers. In a transient PID namespace, the wrapper keeps its call alive until the child exits, forwards INT/TERM/HUP only after two adjacent exact PID/start/group-leader checks, and retains parent-death coupling for the fenced child. The launcher that hosts such a wrapper (`dispatch-chain`, `dispatch-node`, `dispatch-owner`, `dispatch-batch`) first prints one plain stderr notice that the worker runs inside this call; on INT/TERM/HUP it hands the signal to the wrapper and waits one bounded grace window for the wrapper to stop the worker and close the row as `dead-interrupted` (`failure_class=runtime`, not a launch-tuple failure, not a runtime auto-replacement) instead of killing it mid-cleanup, and the chain then tries no further hop; the next explicit `--start` retries the same tuple once under a successor attempt id. Outside a transient namespace the lifecycle remains `detached`; the existing spawn-then-watch/poll behavior is unchanged. `AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN=1` selects `detached` only when the launcher's own observed scope is host-like and its sealed parent sandbox is not a checked `codex`/`headless`/`workspace-write` sandbox; otherwise both selection and wrapper reselection promote to `foreground-scoped` and record `launch_lifecycle_override=rejected` with reselection `override-rejected-transient-scope`. A host-like observer that finds both recorded PID namespaces (`pid_observer_ns`, `pid_ns`) absent from the host and, in a complete `/proc` walk, no process carrying the attempt tag treats the attempt as exactly dead (`namespace-extinct`): reconcile closes it `dead-namespace-absent`, and the same shared process verdict drives join/supervision, `--start` duplicate receipts and the chain's pre-start settlement, SD-157 replacement, and cleanup. A sandboxed observer cannot see sibling namespaces and keeps `unverifiable`; the host runtime decides those rows. Separately, a `registered_worker=1`, `pid_scope=namespace-local` row whose recorded observer namespace is provably absent from the host, with no terminal envelope, no completion marker, and no attempt-tagged descendant, is sealed as a typed cancelled terminal that releases the owner for one SD-106 same-node retry and never satisfies a general SD-79 successor gate. Native subagents do not substitute for either lifecycle. Timeout or signal termination closes only the exact attempt row with its typed cause; a zero process exit is only an observation and is successful solely when an exact completion marker or typed terminal handoff proves it. An exact Codex `turn.completed` or Claude stream-json `result` handoff with `BLOCKED`/`FAIL` closes that attempt before fallback; a failed Codex turn's structured `TurnError` (`codexErrorInfo`/`additionalDetails`) is classified through the one shared `classify_runtime_failure` helper Claude and OpenCode also use (capacity/auth/runtime, `serverOverloaded` staying runtime rather than capacity) and recorded as a length-capped `dispatch.supervisor.turn.failed` log line so the live classifier and the post-hoc log reader agree; an anchored bwrap mount failure is `dead-sandbox-init`. A dispatch-depth-1 wrapper exports its exact self slug; `dispatch-chain` and `dispatch-batch` default the logical parent to that value; an explicit `--parent` that differs is replaced by it before registration with one plain stderr notice line, never a refusal. Before a dispatch-depth-2 claim, the wrapper resolves one open exact parent attempt in the same repo/worktree and seals `parent_attempt_id`. A namespace-visible exact PID/start is the primary live-parent proof. A Codex `app-server-supervised` owner additionally holds one exact-attempt `flock-v1` liveness lease at the canonical registry-relative path `.dispatch/supervisor-state/<attempt_id>.lease` for its full active lifetime. Only when the parent's process classifier is `unverifiable` because the current tool observer cannot establish authoritative PID-namespace identity may that currently held lock satisfy the live-parent gate. The row must be open, identify a registered headless dispatch-depth-1 Codex owner with `completion_delivery=app-server-supervised`, declare the exact lease kind, canonical attempt-derived path, and a per-attempt nonce matched by the locked file payload, and retain the same repo/worktree/runtime identity. Missing, malformed, foreign, nonce-mismatched, symlinked, or unlocked lease evidence fails closed; a stale file with a free lock is not liveness, and a held lock never overrides terminal status, exact quiescence, PID reuse, or another positive death signal. This lease is neither a broker/request lease nor launch authority, fencing authority, completion evidence, or signal authority. Immediately before launch, again before fence release, and throughout a foreground-scoped child wait, the wrapper revalidates the same exact parent through PID evidence or that narrow lease fallback; parent loss closes the unreleased fence or tears down the direct child. Launch releases only after proving aligned procfs/PID namespace evidence, a non-zombie start identity, and `pgid == pid`; incomplete identity closes the unreleased fence without executing payload. Process-group observation is three-state (`populated`, `empty`, `unverifiable`), so procfs denial or malformed/incomplete scans never become quiescence, reap proof, or signal authority. A foreground post-wait receipt is bound to the exact PID/start/observer-namespace/leader-PGID tuple and remains consumable from another namespace, while a currently observable live exact PID still overrides it. A spawned child records both its namespace-visible PID and, when `/proc` exposes it, its outer-namespace PID/start identity. Fleet must still surface a legacy, malformed, or unverifiable unmatched dispatch-depth-2 row as an orphan instead of dropping it; legacy or invalid rows never receive cascade signal authority. For a foreground Codex child already contained by a checked Codex `headless/workspace-write` parent, the inner Codex sandbox is disabled (`danger-full-access`) to avoid unsupported nested mount setup; the outer sandbox remains the security boundary, so this changes no filesystem or network authority and the effective runtime sandbox is recorded. An inherited execution-access request is graded against that exact live parent’s digest-bound effective record, with every requested and default writable root remaining inside the parent grant. The child record and receipt retain the actual inner sandbox and identify the enclosing parent OS boundary; disabled inner enforcement is never presented as a newly projected sandbox. This path applies only to a workspace-write child selection; read-only, unrelated full-access, unknown parent enforcement, and parent-expanding requests remain refused. The dispatch tuple uses the canonical transport word `headless`; adapter runtime-surface labels such as `codex-exec-headless` are not tuple values. If an inner sandbox remains enabled, a worktree `.codex` mount destination must be a directory. The checked nested-eligibility probe reports that shape as `unsupported` so the tuple never claims readiness the runtime cannot deliver (SD-48), and the wrapper independently fails before registration. A standard+ Codex owner grants that outer sandbox write access only to the existing harness `.core-grounding` and Claude `session-env` scratch directories, the primary `$AGENT_HOME/.spec-grounding` directory (created safely if absent — a spec-backed owner must be able to record its own PRD-read marker, not only its SD-69 mutation workers), the canonical dispatch state root (the parent directory of the resolved `AGENT_DISPATCH_JOBS`, never `$AGENT_HOME/.dispatch` directly), and the dispatch summary-owner state root (`$XDG_STATE_HOME/agent-fleet/titles/.dispatch-owners`, created safely if absent — without it every dispatch-depth-2 launch from inside the owner sandbox dies at the pre-release fence as `summary-owner-launch-failed`/`never-launched`; observed 2026-08-06 eiren-m3a), plus — for a commit-expected linked-worktree run under SD-69 — the exact primary Git metadata directories (the per-worktree git dir and the common dir's `objects`/`refs`/`logs`, never `.git` itself) — and, in a primary checkout (git dir equals common dir), for an owner or commit-expected stage only through a native named permission profile, `.git` writable with its existing `config`, `hooks`, `config.worktree`, and `info` read-only (no grant at all when the runtime lacks named profiles) — in addition to its established scoped roots, preserving adapter write gates and cross-harness Claude Bash initialization without widening either runtime home. This grant set is not owner-exclusive: an ordinary registered `dispatch_depth==2` Codex worker (route-bound, launched without `nested_headless_network`) receives the same `.core-grounding` and canonical-dispatch-state-root writable-root entries independent of the owner-only network-widening gate, because that worker also runs the same portable-guard hooks and must be able to record its own core/spec-read markers; only the network-widening grant itself (`AGENT_NESTED_HEADLESS_NETWORK`) stays owner-only. The grant root and the root the launched child actually writes to are computed from the same sealed `AGENT_HOME` value the parent resolved and passed into the child's environment — no wrapper recomputes agent home from its own physical install location after launch.

### 8.5. Successor readiness and parallel launch under SD-79/80/89

a completion marker and its exact terminal row are semantic stage evidence, not proof that the governed process has released its lease. A registered predecessor is successor-ready only after its marker is current, its exact row is terminal, no conflicting active retry exists, no live or unverifiable non-terminal sibling attempt of the same route and node remains, and the recorded outer governor process is quiescent. A live sibling blocks readiness as `prior-attempt-still-live`; an unverifiable one blocks it as `prior-attempt-unverifiable`. Live exact identity, or a live process carrying that attempt's identity that has escaped the recorded leader's process group, is `draining` and always overrides a stored receipt. PID reuse, zombie, verified disappearance, or an explicit atomic `never-launched` outcome may prove quiescence directly. Once predecessor readiness has already bound a current completion marker to its exact terminal row, or the retry gate has selected an exact terminal sibling row, a complete wrapper-issued post-exit receipt is namespace-portable: `foreground-scoped` requires `governed-process-reaped`, while `detached` requires `governed-process-group-drained`; both bind the recorded PID/start/observer-namespace/leader-PGID tuple and require `pgid-empty-v1` with the same PGID, and the detached form additionally requires an `attempt-tagged-empty-v1` scan made in that recorded observer namespace. The stored receipt may therefore prove quiescence after that observer namespace has disappeared; it does not make a later foreign empty scan authoritative, and a partial receipt, a receipt outside those exact terminal gates, an accessible live tagged process, or an incomplete local scan still fails closed. A namespace-local, non-authoritative attempt whose attempt-tagged process set is provably empty in the observer's PID namespace may close as `dead-namespace-absent` independent of heartbeat freshness, per SD-58 speech-is-not-liveness. The same note closes an attempt whose recorded namespaces are extinct on the host (host-like observer, complete walk, no tagged process anywhere); that verdict needs no post-exit receipt because nothing is left to publish one. Completion gate, runtime join, polling wait, and fallback/progress watchers use this shared classification and never replace it with a fixed sleep, a delayed marker, or a larger cap. A supervised owner, native-Stop session batch, or explicitly reported polling fallback keeps its own exact terminal non-quiescent children parked until the completion-delivery boundary resolves them. The ordinary unstamped interactive pre-tool park is narrower and is not a readiness oracle: it parks only exact latest `open|running` child rows, while terminal live/unverifiable rows remain visible and continue to fail successor, join, wait, fallback, and cleanup gates without freezing unrelated local tools. The sequential plan/plan-check/execute DAG stays sequential around every parallel join. An immutable `parallel_group` contains exactly 2–4 route-declared siblings and starts only through one `dispatch-batch --parallel-group` transaction. The batch verifies route, parent generation, dependencies, width, leg indexes, disjoint scopes, sealed model profiles/perspectives, and checked harness evidence; records required and realized independence axes separately; seals all stable attempts into one schema-v2 manifest; reserves every absent first-start leg atomically; and launches their wrappers concurrently. An explicit `--log-dir` is admitted only inside the registry-owned dispatch state root and fails before row or process creation as `log-dir-outside-dispatch-state-root`; omit it for the canonical `logs/` default, and copy evidence to cycle artifacts through a separate collector. Schema-v2 manifests admit Claude, Codex, and OpenCode legs under the same reservation, launch, join, and receipt rules; no adapter-specific manifest allowlist may narrow that portable set. SD-160 accepts separate executions with distinct personas on the same harness without degradation. Usage gates take precedence over optional harness diversity; historical sealed cross-harness axes remain provenance, while launch, completion and receipt consumers apply the same effective persona policy. Model-profile/perspective and actual harness realization remain recorded. Every opaque reservation binds the exact manifest, route/node/parent/attempt/harness/hop/ordinal/profile/perspective/leg index. Batch minting requires a one-shot capability bound to the exact `dispatch-batch.py` parent PID/start and interpreter/script slot. A single missing-leg recovery proves every other N-1 manifest member active or completed and seals the sorted peer-set digest; missing, duplicate, foreign, terminal-failed, or incomplete peers reserve zero slots. Full-N capacity shortage likewise creates zero rows and zero model processes. Individual group-member `register`/`start` through `dispatch-node`, `dispatch-chain`, a wrapper, or fallback fails before row/process creation. Idempotent repeats classify exact active/completed rows without consuming capacity. In a transient PID namespace all newly started foreground-scoped wrappers remain alive in the same checked batch call; elsewhere detached lifecycle remains available. The batch emits bounded per-leg receipts and creates no model turn, daemon, broker, worker fan-out, or extra dispatch depth. Schema-v1 exact two-way manifests and `replica_group`/`--replica-group` remain read/CLI aliases for one migration window; new routes and receipts are canonical `parallel_group`. OpenCode is eligible for registered standard+ dispatch-depth-2 dispatch: it implements exact parent binding, foreground lifecycle, and supervisor snapshot parity; its quick/relief surfaces remain a separate authorization path and do not substitute for this parity.

### 8.6. Immediate limit-death handling under SD-15

wrappers watch briefly after launch. If a child exits immediately on session, usage, or authentication limits, mark its row `done` with `note=dead-<reason>` and, when available, `reset=<time>`. Liveness also recognizes anchored short CLI error lines at the end of logs, but a fresh completion or activity transcript wins over a report that merely discusses limits. Wrappers do not retry; the orchestrator chooses redispatch or failover.

### 8.7. Canonical global attempt registry under SD-49 (amended by SD-112 §13.33.2-(8))

dispatch depth 0 resolves the canonical dispatch state root once. This root is determined by the active runtime's install shape and is **not inside the active harness release tree**: an installed checkout resolves a stable per-user state root, a Codex bundle resolves activation-owned mutable root, and only an explicit isolated development checkout uses a checkout-relative path (or an explicit root fixture path). The resolved registry path is passed immutably as `AGENT_DISPATCH_JOBS` to every descendant, and that file's parent directory is the canonical dispatch state root — no reader reconstructs it as `$AGENT_HOME/.dispatch`. **This amendment supersedes the 2026-08 "shared release keeps chain-3" decision**, made before managed-release pruning was observed deleting live dispatch state (2026-08-27); it resolves the prior contradiction between this paragraph and §5.9a's "never reconstruct any dispatch state path as `$AGENT_HOME/.dispatch/...`" in §5.9a's favor. Invoking an adapter wrapper from a linked worktree does not make that worktree the agent home: a valid explicit `AGENT_HOME` wins, otherwise the adapter resolves the installed canonical harness; its source checkout is only a standalone fallback. For a nested launch, `--jobs` may only repeat that inherited absolute path; a cycle-local override is non-authoritative and fails closed. Every actual start first writes one global registered-only row, then transitions its exact claim only with the complete fenced process publication described above. General stable identities bind route/node, logical parent, target harness, and fallback ordinal; replica batches bind the exact parent generation and deliberately exclude display slug/prefix. A duplicate or already-started attempt starts zero children. A global open/lock failure returns `global-registry-unwritable` with zero children and no local-only row. Optional cycle-local files are audit mirrors, never authority. Existing current-contract local-only rows are reconciled by exact `attempt_id` idempotently while preserving timestamp, status, and failure note; legacy or invalid rows remain read-only diagnostics and are never reconciled, mutated, or signalled. The six tab-separated row fields remain `<ISO-time>`, status, repo, worktree, slug, and pipe; status words remain only `open`, `running`, and `done`. Each registered row also seals `launch_home=<resolved launch agent home>` in the pipe so readers (for example Fleet) locate that launch's default `.dispatch/logs` stream directory from the row itself instead of inferring install layout; the key is optional on legacy rows, and readers fall back to their existing root heuristics when it is absent. That canonical registry file's parent directory is the canonical dispatch state root: completion markers, logs, heartbeats, watchdog files, supervisor-state, homes, broker, degradation, workflow, and index/journal state all live under it, derived by one function and never reconstructed as `$AGENT_HOME/.dispatch`. `launch_home` keeps its existing narrower meaning as a legacy row anchor and legacy log-root heuristic only; it is not the dispatch state root and a new row's state root is always derived from the registry path, not stored as a separate field.

### 8.8. SD-156 mutation-node in-place retry and declared sub-session lineage

after an execute failure or partial completion, a mutation node may be redispatched on the same immutable route. `worker-route-guard.py` reads one shared lineage verdict (`capability-route.source_lineage_verdict`) against the route's sealed `source_commit`, for every node, regardless of position or prior registry attempts — this retires the SD-65/SD-67/SD-128/SD-133 position-and-retry-evidence branches the guard used to require (a node declared in `resume_retry_boundaries`, a different prior registry attempt for that route and node, first-parent-descendant checked with its own git call). The probe (`rev-parse --git-dir`, `rev-list --first-parent HEAD`, and — only when `HEAD` differs from the sealed commit — an in-progress-operation and branch check) returns one of four kinds: `exact` (HEAD is the sealed commit), `descendant` (HEAD is a first-parent descendant of it — the mutation-worktree, mid-cycle-progress shape SD-67/SD-107 named), `diverged` (HEAD is neither), or `unverifiable` (the question could not be asked at all — mid-merge, detached, git timeout, non-repo). `exact`/`descendant` both pass, any node, any position, and the observed commit, its distance, and its branch are recorded onto the attempt row (`launch_head`, `source_commit_sealed`, `source_commit_distance`, `source_commit_branch`). A declared planned sub-session under a mutation node is covered by the same verdict, needs no separate prior-attempt lineage proof, and its `stage_authority=0` keeps it apart from gate retry accounting. `diverged` is refused (`route-source-commit-mismatch`) with two named recoveries: return to the sealed line of work (switch back to the sealed branch, or use reflog to restore the sealed commit), or compose a new route with `--parent-cycle <current cycle>`. `unverifiable` fails under its own reason (`unsafe-git-operation`/`unsafe-git-state` keep the guard's existing vocabulary; any other reason is the new `source-lineage-unverifiable` token) and is never read as a pass. Do not recompile or re-pin the route, and never use `git reset --hard` to restore it — that prohibition is about an in-place retry on this same immutable route. An SD-104 continuation now **always** re-pins to the observed `HEAD` (as its own `source_commit`) when the verdict is `exact`/`descendant` of the inherited pin — `_continuation_source_commit` no longer declines and keeps the old pin; there is no longer a "mutation retry" for it to protect by refusing to move the pin, since `worker-route-guard.py` is the one place that adjudicates a moved `HEAD` now, on the same lineage verdict, regardless of node position or prior registry attempts. A `diverged` continuation source refuses outright (`continuation-source-commit-diverged`); an `unverifiable` one leaves the inherited pin untouched, exactly as an unresolvable `git rev-parse HEAD` did before. The lineage question is answered from the live worktree, not an authentication boundary — nothing here validates that the observed commits are semantically this route's own history beyond the first-parent walk itself. This grants neither an automatic gate retry nor extra retry budget, and does not change SD-65 downstream-node lineage handling.

### 8.9. Post-exit parent-bound reconcile under SD-64/71/77

a conductor can die mid-pipeline (session end, crash, limit) leaving either a plain stale owner row or registered children/unstarted successor nodes orphaned. Since a dispatch-depth-1 owner is normally registered before route compilation, its route context is derived deterministically from exact child rows in the same repo/worktree, including terminal children; conflicting route tuples fail closed. The deterministic orphan classification is: exact conductor attempt death (`pid`+`pid_start` mismatch or gone) AND at least one route completion node without a marker AND (any open child row OR an un-started successor node whose predecessors are all marked). Each registered dispatch-depth-1 owner launch starts one non-model watcher bound to its exact PID/start-time and attempt; it exits when the row is already terminal, or after every exact owner exit invokes the general attempt reconciler. This closes a childless pre-route model/auth/limit failure as `dead-exact-pid` instead of leaving a Fleet-invisible `open` row, while a true orphan takes the bounded cascade below. This avoids a polling daemon and does not consume a model-worker slot. Reconcile first preserves any exact completion marker or typed terminal handoff, then classifies exact process identity before consulting worktree integration state. A missing configured upstream is typed `no-upstream-configured` and affects only push-sync/cleanup eligibility; it never blocks registry hygiene. A host-visible PID/start mismatch, or a namespace-local row's verified outer PID/start mismatch, is conclusive death evidence even when the recorded PID now names a different live process. For an open namespace-local child, Fleet and reconcile use this evidence order: an exact child terminal marker or receipt; authoritative positive child PID/start or a surviving attempt-tagged process; authoritative child PID death; proven parent extinction for an eligible foreground-scoped child; an authoritative empty attempt-tagged scan; both recorded PID namespaces extinct on the host with no tagged process in a complete host walk; a fresh worker heartbeat (the launcher's `phase=launch` seed is not liveness); then unknown/unverifiable. A fresh heartbeat therefore cannot override proven parent extinction, but a terminal parent word alone proves nothing. The parent exception requires one current registered depth-2 foreground child and one unique current terminal depth-1 owner bound by exact parent attempt, slug, repository, physical worktree, and conflict-free route context, plus either a durable owner exit receipt, authoritative owner quiescence, or watcher-observed exact owner PID/start extinction. Watcher evidence is usable only when its observer PID namespace equals both the current observer and the parent's recorded launch observer (legacy host-visible rows may omit the latter); inaccessible or malformed procfs is never extinction. A detached or ordinary namespace-local worker that lacks this complete proof remains visible and may still use its exact heartbeat. Reconcile then closes an orphan conductor `note=dead-parent-orphaned` and performs one bounded cascade over only open direct children sealed to that `parent_attempt_id`. An exact host-visible child process group is TERM→bounded-grace→KILL reaped only after PID/start and PGID-leader revalidation; an already-gone child row or a registered/claimed row with no atomically published PID is closed as `dead-parent-exited`. A foreground namespace-local child covered by the exact parent-extinction exception is reconciled as `dead-parent-terminated` without a signal; it does not grant signal authority over an unverifiable PID. PID reuse is never signalled: a start-time mismatch proves the recorded child has exited and permits only `dead-parent-exited` row closure. Missing identity on a live or unverifiable process, route conflict, non-group-leader targets, namespace-local rows without the exact parent exception, and legacy live rows are never signalled and remain visible for dispatch-depth-0 handling. The watcher never starts a replacement, retry, successor, or route advance; the resume boundary remains a dispatch-depth-0 decision.

### 8.10. Linked-worktree commit responsibility follows the sealed route

A single-session mutation stage whose sealed route declares `commit_expected: true` edits the assigned source worktree and commits its validated changes. Declared parallel sub-sessions (`subsession_id` or `stage_authority=0`) remain no-commit; their owner closes the stage gate and then commits. This applies equally to Claude, Codex, and OpenCode. Artifact output paths never redirect source edits into an artifact overlay.

Commit-expected Codex owners and stages receive only the per-worktree Git metadata directory and common `objects`, `refs`, and `logs`; common `config` and `hooks` remain read-only. Modern Codex protects resolved Git metadata even when legacy `--add-dir` / `writableRoots` list it (measured with codex-cli 0.160.0). When the native named-permissions surface is available, the adapter projects those existing exact grants into an invocation-local profile extending `:workspace`, without overriding it again through legacy thread/turn sandbox fields. Older runtimes retain the legacy grant projection; profile discovery never adds a launch refusal or requires user configuration. Actual sandbox execution, rather than argv, establishes commit support.

Nested Codex homes live under the canonical registry's state directory (`homes/codex/<worktree-hash>.<release-hash>`, one per worktree and release so owners of two releases in one worktree never re-link each other's home), outside the source checkout. Existing auth/config are linked read-only; new launches never create a source-local `.dispatch/codex-home` link. Legacy links remain readable for liveness without being recreated.

### 8.11. Completion marker bound to the exact attempt row under SD-70

completing a node takes the canonical registry (`jobs.log`) path and the current exact attempt id, not just the route/node pair. It writes the completion marker and an immutable per-attempt linkage atomically first, then idempotently closes only that one attempt row `done note=completed-marker` with the marker as evidence — it never breadth-closes a prior `BLOCKED` row or a later live retry of the same node. A canonical latest-link sibling may be retained for compatibility, but a retry cannot overwrite the immutable linkage used to repair an earlier attempt. Marker write and row close are each idempotent under retry. If the row close fails after the marker is written, the marker is preserved and the command returns a structured nonzero rather than silently succeeding or discarding evidence; reconcile later repairs only that exact marker-backed stale row, never any other row for the route/node.

### 8.12. A sub-session slice reaches its own terminal (SD-130)

`capability-route.py complete` is one transaction that publishes a node's stage marker **and** closes its exact registry row, and a `stage_authority=0` slice needs the second without the first — it holds no stage gate authority, so `complete` refuses it (`subsession-has-no-stage-gate-authority`) before touching the row. A slice therefore closes through `dispatch_completion_join.close_finished_child`, which seals `done note=completed-subsession failure_class=pass classifier_source=completion-join-subsession-terminal-v1` from the same evidence every other closure requires: a quiescent process, a valid terminal envelope, and a readable in-root artifact. A live or unverifiable process keeps the row open (`subsession-not-quiescent`); an envelope naming no readable artifact still closes `dead-invalid-envelope`. `completed-subsession` joins `SUCCESS_NOTES` — the one definition of "this note says the attempt succeeded", owned by `dispatch_contract` — so `complete_subsession_stage` can aggregate the chain. It grants **no** marker eligibility: SD-94's marker-eligible test and the `supervisor_terminal` predicate ask *which producer* closed the row, stay bound to `completed-supervisor`, and are never widened. Before this note a slice had no terminal at all and every success was booked `dead-route-completion-rejected failure_class=contract`, which then stalled the whole declared chain (observed 2026-09-03, `att-174d9f66…`, chain indexes 2–5 never launched). Paired with it, a slice may only **start** on a sealed chain: `dispatch-node.py --subsession-id --action start` requires the persisted manifest at `<state-root>/session_chains/<chain_id>.json` to name that `subsession_id` at that index with that attempt id, else `subsession-chain-manifest-unsealed` (`child_spawned=0`, exit 64). `register` is not gated — the manifest is persisted after the register loop and before index 1 starts.

A slower path exists alongside `dead-route-completion-rejected`: `dispatch_completion_join.close_finished_child`/`dispatch_stage_advance`'s gate close both drive the `capability-route.py complete` subprocess through `complete_route_with_budget`, which distinguishes a genuine typed refusal (`completion-rejected`, one immediate retry) from a transport failure whose outcome is unknown (`completion-transient:<Exc>`, backed off 15/30/60/120/240s up to a 600s budget). Only a transient failure that never resolves within that budget closes the row `note=completion-deferred failure_class=infrastructure classifier_source=registered-wrapper-completion-transient-v1` — a typed deferral, never `dead-*`, because the transport is what failed, not the work. `capability-route.py complete`'s marker-eligible test admits a `completion-deferred` row the same way it already admits a `completed-supervisor` one, so a later `complete` call publishes the marker and appends `note=completed-marker` onto the same row without re-closing it; `failure_class` stays `infrastructure` because DR-1 only fills an empty class. Until that marker exists, the row is not yet a success: `dispatch_attempt_policy.deferred_completion`/`verdict_pass`/`success_note` are the one place every consumer (the join decision loop, the completion gate, the subsession/frame/owner terminal checks, the partial-continuation peer proof) reads that state from, instead of the bare `failure_class == "pass"`/`note in SUCCESS_NOTES` checks each used to carry independently — `SUCCESS_NOTES` itself never gains `completion-deferred`. A `done` row still carrying `completion-deferred` is re-driven, not skipped: the join loop treats `decide_attempt`'s `complete` action as pending and calls settlement again, and `close_finished_child` runs the completion command again for it instead of taking the ordinary already-closed shortcut. `close_wrapper_pass` never re-closes a row already deferred by an earlier budget exhaustion — a second transient failure on it returns the reason without touching the registry, so it cannot manufacture a terminal conflict against its own prior deferral. The completion lock (`_exclusive_lock` on `.<node>.completion.lock`, always 0 bytes) is never deleted; a killed holder's flock is released by the kernel on process exit, so the next `complete` acquires it immediately.

### 8.13. Review verdict is a result, not a worker death (SD-94 owner-closure extension)

a valid terminal handoff from a `worker_type=review` node that reports `verdict: FAIL` **and** names a readable in-root review artifact closes its exact row `done note=completed-review-blocking` — the reviewer completed its contract by recording blocking findings. `failure_class` keeps the verdict axis unchanged, and the foreground wrapper tail, the supervisor join (`dispatch_completion_join.close_finished_child`), the fallback wrapper's terminal race, and the registry reconcile carrier (`dispatch-registry.py classify`) all classify it identically; each of them seals the artifact the reviewer named as `review_artifact_b64`. It is a typed completion: the fallback chain neither spends the harness tuple nor descends to another hop for it (the launch receipt reports `terminal_note`/`review_verdict` instead), the owner's delivery receipt still says `inspect-done-failure`, and Fleet shows the row as done with that note. It is never `dead-worker-fail` — that note, and every other `dead-*` note, stays reserved for a worker that did not finish (crash, auth, limit, protocol, missing or malformed envelope, `FAIL` without a readable artifact, or any non-review node). The round budget (`CONVENTIONS §1.1`) still counts the row: a blocking round is a spent round. Such a row becomes marker-eligible through exactly one evidence-bound path — `complete --jobs --attempt-id <that row>` whose `--evidence` is an owner-closure record — and the gate admits it only when (a) the node kind is `review-worker` and the row's `worker_type` is `review`; (b) no review round of the node is still `open`/`running` (`owner-closure-round-still-open`) and the *terminated* rounds alone exhaust the node's round budget for the route's intensity (`owner-closure-round-budget-not-exhausted`: while budget remains, a correction round is the answer, not a ruling); (c) the node has no canonical completion marker from another attempt (`owner-closure-node-already-complete` — SD-70's one-node-one-attempt binding); (d) the exact attempt log re-inspects as a valid `FAIL` handoff with a readable in-root artifact (`owner-closure-review-artifact-unverifiable`); (e) the evidence path is registry-safe (no `,` `=` tab newline or control character: `owner-closure-evidence-path-unsafe`), is inside the route's artifact root, is named `*.owner-closure.md`, is not the review artifact itself, and carries frontmatter `verdict: closed-by-owner` and `node: <node_id>` (plus a matching `gate:` when present) with no duplicated key; and (f) its body names every `completed-review-blocking` attempt id of that route node and the review artifact's basename as whole tokens. The marker's evidence is the closure record; the row's closure facts (`gate_closure=owner-closure`, `owner_closure=<path>`, `review_artifact_b64`) are sealed through the same sanitizing terminal-evidence writer as every other terminal value, before the marker is published, and only then `note=completed-marker` is appended; the row keeps `done` and never gains `failure_class=pass`. Every refusal is typed `owner-closure-*`; a `dead-*` row, a missing record, a bare flag, or a record that names no attempt keeps the SD-94 fail-closed refusal. Observed 2026-09-02 on three cycles whose verification gate could not close because a productive review was booked as a dead worker: rt-1b8f7f609bdb4090 plan-check rounds 1–2 (owner closure recorded in `_internal/plan_reviews/round_2.owner-closure.md`), rt-c744d4d89c7fe1e4 plan-check round 1, rt-23728d301917bcc0 impl-review.

### 8.14. A review gate names its reviewer, and a self-review is degraded, not refused (SD-OPEN-41(b), SD-94 extension)

the completion marker of a `review-worker` node carries `reviewer_kind` (`registered-worker` | `native-subagent` | `owner-inline`), `review_independence` (`independent` | `degraded`), and the reviewer's identity — an attempt id, or a native-subagent transcript path with its sha256. The kind is adjudicated against evidence, never accepted on the caller's word. A `complete --reviewer-attempt <att>` claim is verified against the row in `--jobs`: the row must exist and carry `worker_type=review`. A `complete --reviewer-subagent <transcript>` claim requires a readable regular file, and the recorded digest is what makes the identity checkable afterwards; per the user's rule a native subagent with a recorded identity **is** independent review. With no claim the completing attempt is the reviewer, which is independent only when its own row says `worker_type=review` — `registered_worker=1` alone was never proof of that. Every failed claim (row absent, wrong `worker_type`, unreadable transcript, no registry to adjudicate with) **downgrades to `owner-inline` with a typed `reviewer_downgrade_reason`; it never refuses.** Refusal was tried and withdrawn (SD-132): every review node seals `native-subagent` and `inline` as its last two fallback hops, so a guard that refuses them deadlocks the dependent node forever. A degraded gate proceeds, and the fact travels with it: the row carries the same three axes beside `note=completed-marker`, `complete` prints `completed-review-degraded` on stderr, the closed outcome carries `review_independence` per node plus `review_independence_degraded`, and the `§0.5` completion card must say the gate was not independently reviewed. The note stays `completed-marker` on purpose — `dispatch_contract.marker_attempt_readiness` and `complete`'s own already-closed branch read that exact literal to mean "this row terminated with a marker", so spelling the degradation into `note` would make an idempotent second `complete` refuse the row it had just closed. A review node completed before these fields existed reports `unrecorded` rather than being read as independent. Measured 2026-09-06 over canonical markers under the dispatch state root's `completion/` (excluding `*.attempt.json` and history siblings): the total depends on the predicate — 67 whose route record still declares `kind=review-worker`, 2,911 whose node id merely contains "review" — so quote the count with its predicate. Stable across both: the axes already separate registered from inline, **12** are inline, and among those nothing separated an owner ruling on its own work from a subagent that actually reviewed it.

### 8.15. Terminal authority and actionable receipt under SD-97

the terminal writer commits one result for the exact attempt. Later contradictory observations preserve the committed result and receipt, hold automatic consumption, and create a review obligation through the shared supervision notice. `dispatch-registry.py resolve-terminal-conflict` previews every unresolved observation; applying an exact row revision with a review report releases consumption while preserving the result. The attempt retains each review so a repeated observation cannot reopen it or hide another unresolved conflict. A terminal registry row with a complete foreground reap or detached group-drain receipt is execution-quiescent even when the observer namespace has exited; a stale summary/UI heartbeat cannot override that exact post-exit proof. Receipt schema v2 gives every joined child exactly one `required_action`: `complete-open`, `inspect-done-failure`, or `advance-completed`. The registered-owner supervisors, managed Codex gateway, Claude async-rewake bridge, pre-tool guard, and harvest selector consume that same action and status, so a terminal row cannot become an unharvestable `matched=0` receipt. Only an actionable model resume consumes the supervisor continuation limit; registry-only preparation and delivery bookkeeping do not. The default limit is route-derived rather than a fixed constant: use the larger of the compatibility floor and the bound route's declared node count plus one retry slot for each unique `resume_retry_boundaries` node. An explicit positive owner-launch override may replace that value. Missing, unreadable, mismatched, or unbound route evidence retains the finite compatibility floor; it never creates an unlimited supervisor. “Retains” includes D47-6's terminal reserve (`reserved_remaining >= 1`): the floor path is never discarded or reduced to a zero-reserve budget merely because route binding failed. A declared 13+ continuation chain therefore reaches terminal report harvest and final handoff, while attempts beyond the declared chain plus retry headroom remain `continuation-limit-exceeded`.

### 8.16. Owner route binding and duplicate launch receipt under SD-97

`dispatch-owner --route-evidence` verifies the sealed route against cwd, capability, mode, intensity, route hash, and selected owner harness, then forwards an owner-level binding. Each adapter wrapper revalidates it and exports `AGENT_ROUTE_FILE` and `AGENT_ROUTE_ID` with an empty `AGENT_ROUTE_NODE`; an owner is never fabricated as a route node. This lets the owner use the declared inline fallback while retaining the material route guard. An exact duplicate claim still starts zero children and stays idempotent, but every wrapper emits `launch_state=existing-active|existing-dead|existing-unverified|existing-completed` instead of a silent success-shaped no-op (an open row is classified by the shared process verdict; `existing-dead`/`existing-unverified` add one plain `note=` line); batch callers may accept the typed existing state, while a caller requiring a new start must branch explicitly.

### 8.17. Post-launch owner-route lifecycle under SD-97

a registered dispatch-depth-1 owner may legitimately start without route evidence and compile generation 0 after launch. The compiler then attaches that immutable route to the exact owner attempt through a separate atomic lifecycle record; it never rewrites the route or the launch-sealed registry tuple. Publication holds the canonical registry lock and verifies the current unique owner row, active state, attempt schema, worker/depth/surface, parent session, owner harness, physical worktree/repository, capability/mode, artifact root, route id/hash, generation, and route owner identity. Exact replay is idempotent; a partial launch binding, a second generation-0 candidate, hash drift, unrelated owner/worktree/session, or a close/start race fails closed. Continuation publication records an immutable target-keyed candidate under its exact predecessor only after the successor route exists, but compilation alone does not advance the binding: adoption additionally requires a current-contract registered depth-2 child row for that exact owner, route file/id/hash, worktree/capability/mode/artifact tuple, and `launch_started=1`. A childless candidate is inert and may coexist with a later candidate; exactly one child-adopted target advances the current owner route, while two child-adopted targets are a competing-successor conflict. The current owner route advances monotonically through exact generation `n -> n+1` edges whose source hash, successor hash, supersession metadata, owner identity, and worktree contract all verify; downgrade, gap, competing active successor, tampered lineage, or an unrelated route cannot take over the binding. Consumers resolve the launch binding or post-launch attachment first and then this verified edge chain, so retained child evidence preserves the current generation across restart.

### 8.18. Fleet owner-lineage projection under SD-97

group, process, and JSON views use the same authoritative current owner generation. A valid explicit launch/attachment binding is folded through the verified successor chain even while a superseded source route remains open. When no explicit binding record exists (including legacy attempts), Fleet may recover only a single linear chain formed from exact owner-linked child attempts and terminal attempt evidence, with every route hash, generation, source/successor edge, owner/worktree identity, and reuse contract verified. A compiled route with no owner-linked child evidence is not an active stage candidate. Two real successors, disconnected or unverifiable lineage, or conflicting explicit evidence remains the typed `multiple-owner-routes` ambiguity; timestamp ordering and "latest route" heuristics are forbidden.

### 8.19. Declared runtime requirements and lifecycle evidence under SD-97

a node may declare only registry-known `runtime_requirements`. `loopback-listen` means localhost bind while outbound network remains denied; a runtime that exposes only a broad network boolean reports `loopback-only-unsupported` and uses the checked main/inline handoff rather than widening outbound access. Every registered row records the requested launch lifecycle plus bounded selector evidence (source, NSpid width, and PID-1 class) so an intermittent nested-sandbox lifetime failure is diagnosable without transcript inspection.
