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
| `fast writer` | Assembly from verified artifacts |
| `fast implementer` | Routine implementation and refactoring |
| `deep reviewer` | Architecture, methodology, safety, domain correctness, high-risk review |
| `deep maker` | High-judgment creation: planning, synthesis, visual/editorial craft |
| `deep orchestrator` | High-judgment conductor: stage gates, failover, and evidence synthesis for `standard+` dispatch-depth-1 work |
| `external adversary` | Independent reviewer with different model/runtime/process assumptions |
| `orchestrator` | Balanced mechanical coordination of already-decided tooling, paths, and report assembly; not a deep-conductor alias |

Adapters map two independent portable axes: `model_role` describes behavior, while `model_profile` (`deep|balanced-deep|balanced|light|mini`) is the sealed result of the judgment-demand × execution-scope resolver. Each adapter declares concrete models, effort/variant projections, profile granularity, and interactive-main-only families in `adapters/<adapter>/config/models.conf`; every resolver, wrapper, generated agent, lifecycle worker, and documentation table derives from that single source. A route-bound job carries both sealed axes and rejects trailing model/effort replacement. `mini` is unavailable to substantive registered dispatch-depth-1/2 owners, stages, and reviewers. A profile may share another profile's concrete model as long as the resulting execution points stay distinct; the ladder is a set of operating points, not a set of models. An adapter lacking a verified effort/variant distinction may collapse only the explicitly documented operating point (OpenCode balanced to light) with reduced-granularity metadata. Non-route surfaces may retain checked explicit selection or inheritance when the resulting model is execution-surface eligible; main-only or unprovable inheritance is a typed deny.

Adapter and projection edits are derived core-first: change the portable invariant in
`core/` first, read that governing core document in the current session, then update
the adapter realization and generated projection. A runtime marker proves the read
gate only; it does not replace this source-order review.

## 4. Capability Model

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
| spec read gate | Spec-backed work must read the current blueprint before changing code/spec |
| core first gate | Adapter edits must be grounded in an actual current-session read of the relevant core contract |
| memory write guard | Runtime-native memory files must not bypass the unified memory store |
| memory recall/inject/distill | Inject relevant memory and optionally distill session deltas |
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
- A stored surface baseline rejects growth greater than five percent unless the
  same change records a reviewed rationale and updates the budget.
- Model-visible surface budget: the nine documents an agent reads to route
  and dispatch work (`core/{CORE,WORKFLOW,CONVENTIONS,OPERATIONS,HOOKS,MEMORY}.md`,
  `adapters/claude/CLAUDE.md`, the `autopilot-code` `dev-pipeline` and
  `owner-execution` references) carry sealed per-file byte and directive caps
  in `tools/surface-budget.json` and a total ceiling in
  `tools/check-surface-budget.py`. Caps are per file and independent: a
  change that grows any one of them fails the boundary check even when
  another shrinks. Each cap sits one ordinary edit above its measurement —
  3% of the bytes, and two directives or 3%, whichever is larger — so a normal
  change has room to land; caps sealed at the exact measurement made all nine
  surfaces permanently full and pushed growth into skipping the gate instead.
  The margin is finite and cannot be widened by repetition: a reseal takes it
  from the current measurement, never from the previous cap. Growing a file
  past its cap means resealing in the same change with a recorded `--reason`,
  and a reseal is refused outright when the sealed caps would exceed the code
  ceiling. Reductions are locked in by resealing downward. The ceiling is
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

- **Runtime-owned completion delivery under SD-14/78:** a registered `standard+` headless owner is launched under an adapter supervisor, not as an unresumable one-shot model turn. The model registers every separable child in the current batch and yields `runtime_wait: registered-children`; the supervisor snapshots only current v2 rows sealed to `parent_attempt_id=$AGENT_DISPATCH_ATTEMPT_ID`, joins every parallel attempt through canonical liveness outside the model/tool loop, and sends the same session exactly one bounded typed receipt when the whole batch is semantically terminal **and execution-quiescent**, or requires typed attention. Child output, transcript text, artifact bodies, source, git state, and liveness prose never enter that receipt. Codex realizes the bridge with one ephemeral App Server thread and repeated `turn/start` after `turn/completed`; a registered Claude owner realizes its internal batch bridge with one `--session-id` followed by `--resume`. An interactive Claude parent uses a separate `PostToolUse(Bash)` `asyncRewake` bridge: only an owner attempt whose lock-written row in the session's trusted registry (inherited `AGENT_DISPATCH_JOBS`, else the installed canonical registry) carries the same session, `worker_type=owner`, dispatch depth 1, `parent_completion_delivery=claude-parent-runtime`, and claimed/started evidence may arm it — a start receipt on stdout names the candidate, the row proves it, and a receipt naming any other file binds nothing. Each Bash call may take one unclaimed row started inside a bounded recent window through the arm ledger (one waiter per attempt; a wave of starts arms one per call, oldest first) or re-take a claim of its own that lapsed, whatever the row's age, and a row nobody can prove arms nothing, because absence beats misattribution. It watches that one owner attempt to terminal quiescence outside the model, and exits once with a bounded exact-attempt receipt. It never launches a visible background `dispatch-wait`, Monitor, progress recap, or periodic re-arm; explicit `poll-fallback` remains the only model-owned wait. Intermediate turn/result events are withheld from the terminal handoff, and only the final exact three-line envelope is exposed as terminal. Before every model turn the supervisor atomically publishes an attempt-scoped schema-v2 phase state: `parked`, `deliverable`, `running-turn`, `recovery`, or `terminal`. While an undelivered child is open or terminal-but-draining, the native pre-tool policy admits only one exact same-parent `dispatch-batch --action start` for a declared parallel group (or a non-group exact `dispatch-node --action start`), so a first child cannot prevent its checked siblings from registering. Once any delivered child remains open or draining, the policy admits only exact typed harvest for that delivered batch. Both phases reject model waits, raw inspection, liveness, unrelated tools, and shell composition; missing/invalid phase state is recovery-only exact harvest. Codex enforces this through its projected hook and Claude through a command-scoped `--settings` PreToolUse bridge without mutating user-owned runtime settings. Multiple sequential route batches repeat this one-resume transaction. A bounded join timeout is an internal repark checkpoint: it emits no model receipt, does not update the delivered set or consume continuation budget, and makes the same supervisor rejoin the same sealed child set. A second, distinct internal repark checkpoint (SD-119) advances a serial sub-session chain registered under `utilities/stage-session-chain.py`: once the joined child is a chain participant and terminal, the supervisor claims and starts the chain's next index itself, folds the closed predecessor into the delivered set so it is never re-surfaced, and rejoins — again with no model turn and no continuation spend — until the chain either completes (falls through to the ordinary route-level flow) or the joined child carries no chain metadata at all. Only terminal-and-quiescent or a typed attention condition is actionable. An exception with owned open children preserves state and lease in `recovery`; only terminal-and-quiescent completion removes them. A dispatch-depth-0 interactive Codex parent has a separate native realization: a direct registered dispatch-depth-1 attempt bound to the actual `CODEX_THREAD_ID` seals `parent_completion_delivery=codex-stop-hook`; `launch_claimed=0` registration alone never parks the parent, and a start requires the **current exact Stop and PreToolUse hook definitions** to be trusted before it may claim or spawn the process. Immediately after successful spawn, the wrapper atomically binds the exact attempt into hashed-session pending state, then the parent ends its model turn. Stop follows that immutable set even if an orphan watcher has already changed a child row to `done`, joins it outside the model, publishes the delivered phase, and returns one bounded `decision=block` continuation only when exact harvest is ready. While undelivered, PreToolUse admits no model tool—including `dispatch-wait`; after delivery it admits only exact-attempt harvest with `--status all`, so an `open`→`done` watcher transition cannot invalidate the continuation. A valid harvest consumes its exact receipt, and the final receipt removes the session state. A bounded Stop timeout yields one minimal end-turn/re-enter instruction rather than a polling tool loop. Foreign, legacy, registered-only, untrusted, or unstamped rows never enter this path and retain the explicitly reported polling/recovery contract; the recent-window bound governs a *new* claim, while a claim this session already holds re-arms whatever the row's age or terminal state, until the wake it owes is delivered. Runtime support is probed before launch: forced supervised mode fails closed, interactive native Stop delivery fails before spawn when current-hash trust cannot be proved, while other unavailable same-session bridges may use the explicitly reported `poll-fallback` (`dispatch-wait --attempt-id <id> --max 300..600`). After a supervisor has started, protocol/session failure never replays the assignment through a one-shot fallback. Arbitrary detached shell output still does not auto-resume; only the checked completion-delivery surfaces above do. Parent ownership remains exact, foreign or stale rows never wake the owner, and post-exit orphan reconcile remains mandatory and independent.
- **Serial-chain realization (SD-119):** Claude and Codex owner supervisors use the shared reconcile-before-advance driver, reset repark bounds per successor, and deliver one aggregate wake; the sealed proof binds the canonical pointer/original digest and exact bidirectional parent rows. OpenCode owner supervision is unsupported and fails closed; its checked registered-headless or inline fallback must not claim parity. Completed sub-sessions are delivery-success rows, while refused chains close only proven never-started successors and report the bounded refusal notice.

- **Codex launch-publication settle under SD-14/78:** an App Server turn may deliver the exact `runtime_wait: registered-children` sentinel in the narrow interval after atomic child registration but before every fenced wrapper has appended `launch_started=1`. The Codex owner supervisor therefore performs one short, bounded reread of only undelivered exact-parent rows before issuing `registration-required`. A batch that reaches the existing durable `launch_started=1` fence during that settle window parks and joins normally without consuming a continuation or replaying the dispatch; a row that remains registered-only still receives the existing bounded correction. The settle loop holds no registry lock, accepts no artifact, transcript, PID guess, or stale delivered row as launch proof, and never starts or retries a child itself.

- **Managed interactive Codex boundary under SD-92 (supersedes SD-91 and the interactive clauses of SD-83 and the preceding SD-78 paragraph):** automatic completion delivery is a new-session boundary entered through `utilities/codex-managed-entry.py`; an existing TUI is never hot-upgraded. A user-authorized harness install may make that boundary transparent by installing a reversible launcher for interactive `codex`, `resume`, and `fork`, while preserving the resolved real Codex command and passing every non-interactive or administrative subcommand through unchanged. Plugin metadata or a lifecycle hook alone never claims launcher ownership because both load after process entry. One private owner-only gateway is the sole upstream App Server client for that thread; remote TUI client A owns subscriptions, transcript display, and every approval response, while completion sidecar client B may use only the gateway's private control socket and never connects upstream or acquires approval authority. The gateway serializes manual input and completion delivery under one atomic thread-state claim: completion starts one `turn/start` only when the thread is idle, or one `turn/steer` when a live turn accepts steering. A durable sealed-batch ledger treats `prepared` as retryable, an accepted receipt as replayable without another wake, and an upstream disconnect after send as `sent-ambiguous` with no automatic resend. `clientUserMessageId` is metadata only, not the deduplication primitive. A direct registered dispatch-depth-1 sidecar is launched after immutable registration but before the worker spawn claim, waits for that exact `launch_claimed=1`, then joins only the exact terminal and quiescent batch and submits one bounded typed receipt with no raw child output; absence of an exact launch fails closed. Parent runtime selects the wake adapter independently of child runtime: a Codex parent uses this managed gateway for Codex or Claude children, while a Claude parent keeps its Claude async-rewake/`--resume` supervisor for either child. Managed Codex completion uses neither a Stop continuation prompt nor an all-tool PreToolUse parent park, so the interactive parent remains available while children run. Managed launches also probe the exact effective `default_mode_request_user_input` feature row and, when supported, process-locally enable it in both the App Server and remote TUI children without writing user config; a per-launch disable wins across both processes, and unsupported probing warns once then launches without injection. The same gateway observes only typed `(threadId, requestId)` identity and time for `item/tool/requestUserInput`, publishes content-free `codex-appserver` evidence, and clears only its own evidence on an exact response, `serverRequest/resolved`, turn completion/interruption, or disconnect. It forwards every RPC unchanged and never renders, answers, approves, blocks, or owns user input; the TUI remains sole input and approval owner. Unmanaged clients remain unknown without a real producer, while rollout parsing remains legacy fallback only. An unmanaged interactive Codex parent cannot register or start a new detached dispatch-depth-1 owner through the portable owner selector: the selected child adapter must retain the actual caller runtime and fail before registry mutation or spawn with `managed-entry-required`. The low-level operator-only `--allow-unmanaged-parent-poll` escape hatch preserves a disclosed finite recovery path, is forbidden by `dispatch-owner`, and is never selected automatically by a model route. Sessions with an already-open legacy attempt or trusted `codex-stop-hook` state retain only finite migration/recovery behavior, and exact terminal `--status all --attempt-id` harvest may consume one legacy receipt. Open, stale, foreign, older-attempt, broad-selector, raw-output, and synthetic user/developer-message paths have no wake authority. A registered Codex headless owner continues to use its separate private App Server supervisor. Installer ownership must be manifest-backed, update-repairable, collision-safe, and exactly reversible on uninstall; private runtime state and the real CLI binding fail closed when validation is unavailable. Protocol ambiguity remains fail-closed and is reported as the upstream `continueIfIdle(threadId, idempotencyKey, typedContext)`/native async-rewake gap.

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
  `owner=alive-waiting`), leaves the record `sent-ambiguous` (the wake is
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

**Detached watch realization.** `peer-steward.py watch <target>` takes a blocking exclusive lock on the dedupe
claim, checks the target once with `herdr agent get`, takes the watch lock, spawns one
`setsid` watcher holding that same lock, writes an immutable arm record carrying the
watcher's `{pid, pid_start}`, records `kind=watch … receipt=<watch_id>`, and returns a
typed `state=armed` line without waiting. Every steward line reporting an armed watch
carries the same `parent_next` directive a launch receipt does, and claims `end-turn` only
when the hook arms from *that* line and the watch's wake is the hook; every other line —
`wake=none`, a dedupe hit, a session-printed `rearm` — prints `bounded-wait` with a bounded
`join <watch_id>`. The watcher calls `herdr agent wait` exactly
once, writes `peer_watch_receipt_v1` atomically, records `kind=notice status=received`,
and releases the lock only by exiting — the receipt rename strictly precedes exit. `join`
waits on that lock (a kernel wait, never a poll), so it returns on the watcher's exit
event; it reports `watcher-dead` only when there is no receipt **and** the arm record's
PID identity fails, because lock acquisition alone would misread spawn latency as death.
`status` reports `armed|alive|receipt|acked`, where `alive` needs pid, `/proc` start ticks,
and lock possession together. `rearm` replaces only a dead un-receipted watch, always under
a new `watch_id` so receipt and ack paths never overlap. The receipt carries no screen or
message text: per S4 the steward reads `herdr agent read` and disk itself (idle ≠ done).

On Claude, `PostToolUse(Bash)` `asyncRewake` hook `peer-steward-rewake.py` arms only from a
same-session armed line with `wake=hook` whose receipt sits under the canonical state root,
`join`s within one deadline computed at hook entry, acks, and exits 2. Its survival across
user interrupt, compaction, and session end is **unmeasured**, which is why receipt
durability and the fallback carrier are mandatory: the `UserPromptSubmit` sweep surfaces up
to five un-acked receipts for the current session and acks them, so a dead hook or a
restarted session loses nothing. Wake is at-least-once and display is idempotent — the ack
file is created `O_EXCL` by whichever carrier gets there first.

### 7.4. Carrier taxonomy and selection

A registered headless owner yields after registering a batch; its runtime supervisor joins the exact `parent_attempt_id` batch outside the model and resumes the same owned session once with a bounded typed receipt. A registered Claude owner keeps one realtime stream-input process for the route and submits the next receipt immediately after each non-terminal join; when a freshly verified terminal marker closes every declared terminal gate, the supervisor skips the redundant final owner turn and closes the stream before terminal row reconciliation. An explicit custom-command fallback retains per-turn `--resume`. A Claude interactive parent may instead arm one native `asyncRewake` PostToolUse hook from a successful exact owner-start receipt, or from a successful exact steward watch armed in the same session (`utilities/peer-steward.py watch`, SD-122 §13.37.2-(10)); either way the hook owns exactly one arming event, verifies it against the session that produced it, and never widens to another attempt or watch. It re-reads the exact current row and sealed completion evidence before rendering: every terminal receipt — success or attention — exits two, because Claude Code wakes an idle session for an `asyncRewake` hook only on exit code 2 and delivers exit-0 output no earlier than the next user interaction (corrected 2026-08-29; success additionally carries its structured notification on stdout). A launcher-managed interactive Codex session places one owner-only gateway between remote TUI and App Server. The harness installer may make this checked entry transparent for interactive commands, but plugin or hook loading after process entry is not equivalent. That gateway atomically serializes manual input with an exact completion receipt, uses `turn/start` only while idle and `turn/steer` only for a steerable active turn, and durably suppresses duplicate sealed-batch delivery. The sidecar is prelaunched before the child spawn claim, connects only to the private control socket, never subscribes upstream, never sees or answers approvals, and submits no raw child output. A send followed by an unclassified disconnect is `sent-ambiguous` and is not retried. Outside those checked entries, hooks must not simulate wake by blocking Stop, parking every tool, or injecting a synthetic user turn; the parent remains conversational and uses a disclosed finite fallback. Legacy receipts may be consumed only by exact terminal typed harvest. Runtime-native subagents are a separate surface.

Select delivery by parent runtime, never child runtime: Codex managed parent → Codex gateway; Claude interactive parent → exact owner or exact steward-watch `asyncRewake` with an exit-2 wake for every terminal receipt, plus the SessionStart/UserPromptSubmit sweep that re-delivers any SD-111 pending record or un-acked watch receipt at the next prompt; registered Claude owner → persistent realtime stream with a sealed-terminal fast path (checked per-turn `--resume` fallback); registered Codex headless owner → its private App Server supervisor. Keep TUI client A as the only approval owner and sidecar client B control-only. Require private socket/state paths, exact terminal+quiescent membership, durable idempotency, bounded typed context, and fail-closed ambiguity. A transparent launcher must preserve and validate the real CLI, route only interactive surfaces, repair on update, and restore exactly on uninstall. If those checks are unavailable, report fallback and the missing atomic `continueIfIdle(threadId, idempotencyKey, typedContext)`/native async-rewake primitive rather than widening Stop or PreToolUse.

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

### 8.2. Checked fallback chain under SD-50

a standard+ stage ranks checked direct-headless candidates through its sealed quality bands, then falls through to `native-subagent -> inline`. A candidate still records whether it is `same-harness-headless` or `cross-harness-headless`; those labels describe the selected tuple and fallback trace, not a durable preference that can override explicit choice, hard eligibility, sealed affinity, or schema-v3 quality/capacity allocation. Every headless candidate carries the same route id/node, write scope, completion gate, logical parent, stable attempt identity, and checked tuple evidence. The conductor invokes eligible adapter wrappers in that sealed/ranked order and proceeds only after a recorded launch failure. Native and inline degradation record skipped candidates, failure classes, registry attempt ids, allocation/headroom evidence, and assurance compensation without claiming Fleet parity.

### 8.3. Direct headless launch under SD-61~63

dispatch contract v3 has no resident launch broker, request spool, broker heartbeat, broker lease, or broker fencing identity. A standard+ dispatch-depth-1 conductor invokes the checked adapter wrapper directly for every same- or cross-harness dispatch-depth-2 attempt. The selected checked tuple's parent harness, transport, and sandbox must equal the actual launching wrapper identity exported in `AGENT_DISPATCH_CURRENT_*`; a missing partial identity or mismatch fails before child wrapper invocation. The canonical registry first records the stable attempt as `launch_claimed=0`. The checked wrapper then spawns a parent-death-safe pre-exec fence while holding the registry lock and atomically publishes the complete PID/start/namespace/leader-PGID identity together with the only `launch_claimed=1` transition. The fence records `launch_started=1` under the same exact row immediately before payload `exec`; a launcher lost before spawn leaves a retryable registered row, and a dead fence that never recorded start may be reset only after exact process-group quiescence. A duplicate or already-started claim never creates another child. A Codex dispatch-depth-1 capability owner running with `workspace-write` receives `sandbox_workspace_write.network_access=true`, `AGENT_NESTED_HEADLESS_NETWORK=1`, and a worktree-local writable `CODEX_HOME`; that home links existing auth/config read-only and keeps mutable nested session state inside the owner sandbox. Dispatch-depth-2 workers do not inherit the network widening. Contract-v1/v2 route and broker state are read-only migration inputs. The broker utility may expose diagnostic `status` and idempotent `stop` for one compatibility release, but production dispatch never calls `ensure`, `request`, or `serve`.

### 8.4. Namespace-safe launch lifecycle under SD-72

`dispatch-chain` selects the child lifecycle from the actual launcher scope for both same- and cross-harness candidates. Because an adapter wrapper may enter a narrower transient namespace after that selection, the incoming lifecycle is provisional: every wrapper re-evaluates its own scope before registry reservation or attempt creation and atomically promotes `detached` to `foreground-scoped` when the actual scope is transient. That pre-registration promotion is normal selection, consumes no attempt or retry budget, and is recorded with both selector observations; `dead-nested-sandbox-lifetime` remains only a legacy/recovery classification for a caller that bypasses the checked wrappers. In a transient PID namespace, the wrapper keeps its call alive until the child exits, forwards INT/TERM/HUP only after two adjacent exact PID/start/group-leader checks, and retains parent-death coupling for the fenced child. Outside a transient namespace the lifecycle remains `detached`; the existing spawn-then-watch/poll behavior is unchanged. `AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN=1` selects `detached` only when the launcher's own observed scope is host-like and its sealed parent sandbox is not a checked `codex`/`headless`/`workspace-write` sandbox; otherwise both selection and wrapper reselection promote to `foreground-scoped` and record `launch_lifecycle_override=rejected` with reselection `override-rejected-transient-scope`. Separately, a `registered_worker=1`, `pid_scope=namespace-local` row whose recorded observer namespace is provably absent from the host, with no terminal envelope, no completion marker, and no attempt-tagged descendant, is sealed as a typed cancelled terminal that releases the owner for one SD-106 same-node retry and never satisfies a general SD-79 successor gate. Native subagents do not substitute for either lifecycle. Timeout or signal termination closes only the exact attempt row with its typed cause; a zero process exit is only an observation and is successful solely when an exact completion marker or typed terminal handoff proves it. An exact Codex `turn.completed` or Claude stream-json `result` handoff with `BLOCKED`/`FAIL` closes that attempt before fallback; an anchored bwrap mount failure is `dead-sandbox-init`. A dispatch-depth-1 wrapper exports its exact self slug; `dispatch-chain` defaults the logical parent to that value and rejects an explicit mismatch before registration. Before a dispatch-depth-2 claim, the wrapper resolves one open exact parent attempt in the same repo/worktree and seals `parent_attempt_id`. A namespace-visible exact PID/start is the primary live-parent proof. A Codex `app-server-supervised` owner additionally holds one exact-attempt `flock-v1` liveness lease at the canonical registry-relative path `.dispatch/supervisor-state/<attempt_id>.lease` for its full active lifetime. Only when the parent's process classifier is `unverifiable` because the current tool observer cannot establish authoritative PID-namespace identity may that currently held lock satisfy the live-parent gate. The row must be open, identify a registered headless dispatch-depth-1 Codex owner with `completion_delivery=app-server-supervised`, declare the exact lease kind, canonical attempt-derived path, and a per-attempt nonce matched by the locked file payload, and retain the same repo/worktree/runtime identity. Missing, malformed, foreign, nonce-mismatched, symlinked, or unlocked lease evidence fails closed; a stale file with a free lock is not liveness, and a held lock never overrides terminal status, exact quiescence, PID reuse, or another positive death signal. This lease is neither a broker/request lease nor launch authority, fencing authority, completion evidence, or signal authority. Immediately before launch, again before fence release, and throughout a foreground-scoped child wait, the wrapper revalidates the same exact parent through PID evidence or that narrow lease fallback; parent loss closes the unreleased fence or tears down the direct child. Launch releases only after proving aligned procfs/PID namespace evidence, a non-zombie start identity, and `pgid == pid`; incomplete identity closes the unreleased fence without executing payload. Process-group observation is three-state (`populated`, `empty`, `unverifiable`), so procfs denial or malformed/incomplete scans never become quiescence, reap proof, or signal authority. A foreground post-wait receipt is bound to the exact PID/start/observer-namespace/leader-PGID tuple and remains consumable from another namespace, while a currently observable live exact PID still overrides it. A spawned child records both its namespace-visible PID and, when `/proc` exposes it, its outer-namespace PID/start identity. Fleet must still surface a legacy, malformed, or unverifiable unmatched dispatch-depth-2 row as an orphan instead of dropping it; legacy or invalid rows never receive cascade signal authority. For a foreground Codex child already contained by a checked Codex `headless/workspace-write` parent, the inner Codex sandbox is disabled (`danger-full-access`) to avoid unsupported nested mount setup; the outer sandbox remains the security boundary, so this changes no filesystem or network authority and the effective runtime sandbox is recorded. The dispatch tuple uses the canonical transport word `headless`; adapter runtime-surface labels such as `codex-exec-headless` are not tuple values. If an inner sandbox remains enabled, a worktree `.codex` mount destination must be a directory. The checked nested-eligibility probe reports that shape as `unsupported` so the tuple never claims readiness the runtime cannot deliver (SD-48), and the wrapper independently fails before registration. A standard+ Codex owner grants that outer sandbox write access only to the existing harness `.core-grounding` and Claude `session-env` scratch directories, the primary `$AGENT_HOME/.spec-grounding` directory (created safely if absent — a spec-backed owner must be able to record its own PRD-read marker, not only its SD-69 mutation workers), the canonical dispatch state root (the parent directory of the resolved `AGENT_DISPATCH_JOBS`, never `$AGENT_HOME/.dispatch` directly), and the dispatch summary-owner state root (`$XDG_STATE_HOME/agent-fleet/titles/.dispatch-owners`, created safely if absent — without it every dispatch-depth-2 launch from inside the owner sandbox dies at the pre-release fence as `summary-owner-launch-failed`/`never-launched`; observed 2026-08-06 eiren-m3a), plus — for a commit-expected linked-worktree run under SD-69 — the exact primary Git metadata directories (the per-worktree git dir and the common dir's `objects`/`refs`/`logs`, never `.git` itself), in addition to its established scoped roots, preserving adapter write gates and cross-harness Claude Bash initialization without widening either runtime home. This grant set is not owner-exclusive: an ordinary registered `dispatch_depth==2` Codex worker (route-bound, launched without `nested_headless_network`) receives the same `.core-grounding` and canonical-dispatch-state-root writable-root entries independent of the owner-only network-widening gate, because that worker also runs the same portable-guard hooks and must be able to record its own core/spec-read markers; only the network-widening grant itself (`AGENT_NESTED_HEADLESS_NETWORK`) stays owner-only. The grant root and the root the launched child actually writes to are computed from the same sealed `AGENT_HOME` value the parent resolved and passed into the child's environment — no wrapper recomputes agent home from its own physical install location after launch.

### 8.5. Successor readiness and parallel launch under SD-79/80/89

a completion marker and its exact terminal row are semantic stage evidence, not proof that the governed process has released its lease. A registered predecessor is successor-ready only after its marker is current, its exact row is terminal, no conflicting active retry exists, no live or unverifiable non-terminal sibling attempt of the same route and node remains, and the recorded outer governor process is quiescent. A live sibling blocks readiness as `prior-attempt-still-live`; an unverifiable one blocks it as `prior-attempt-unverifiable`. Live exact identity, or a live process carrying that attempt's identity that has escaped the recorded leader's process group, is `draining` and always overrides a stored receipt. PID reuse, zombie, verified disappearance, or an explicit atomic `never-launched` outcome may prove quiescence directly. Once predecessor readiness has already bound a current completion marker to its exact terminal row, or the retry gate has selected an exact terminal sibling row, a complete wrapper-issued post-exit receipt is namespace-portable: `foreground-scoped` requires `governed-process-reaped`, while `detached` requires `governed-process-group-drained`; both bind the recorded PID/start/observer-namespace/leader-PGID tuple and require `pgid-empty-v1` with the same PGID, and the detached form additionally requires an `attempt-tagged-empty-v1` scan made in that recorded observer namespace. The stored receipt may therefore prove quiescence after that observer namespace has disappeared; it does not make a later foreign empty scan authoritative, and a partial receipt, a receipt outside those exact terminal gates, an accessible live tagged process, or an incomplete local scan still fails closed. A namespace-local, non-authoritative attempt whose attempt-tagged process set is provably empty in the observer's PID namespace may close as `dead-namespace-absent` independent of heartbeat freshness, per SD-58 speech-is-not-liveness. Completion gate, runtime join, polling wait, and fallback/progress watchers use this shared classification and never replace it with a fixed sleep, a delayed marker, or a larger cap. A supervised owner, native-Stop session batch, or explicitly reported polling fallback keeps its own exact terminal non-quiescent children parked until the completion-delivery boundary resolves them. The ordinary unstamped interactive pre-tool park is narrower and is not a readiness oracle: it parks only exact latest `open|running` child rows, while terminal live/unverifiable rows remain visible and continue to fail successor, join, wait, fallback, and cleanup gates without freezing unrelated local tools. The sequential plan/plan-check/execute DAG stays sequential around every parallel join. An immutable `parallel_group` contains exactly 2–4 route-declared siblings and starts only through one `dispatch-batch --parallel-group` transaction. The batch verifies route, parent generation, dependencies, width, leg indexes, disjoint scopes, sealed model profiles/perspectives, and checked harness evidence; records required and realized independence axes separately; seals all stable attempts into one schema-v2 manifest; reserves every absent first-start leg atomically; and launches their wrappers concurrently. An explicit `--log-dir` is admitted only inside the registry-owned dispatch state root and fails before row or process creation as `log-dir-outside-dispatch-state-root`; omit it for the canonical `logs/` default, and copy evidence to cycle artifacts through a separate collector. Schema-v2 manifests admit Claude, Codex, and OpenCode legs under the same reservation, launch, join, and receipt rules; no adapter-specific manifest allowlist may narrow that portable set. `cross-harness` requires at least two harness families, not one distinct harness per leg. A caller may explicitly accept typed same-harness degradation, but model-profile/perspective realization remains recorded. Every opaque reservation binds the exact manifest, route/node/parent/attempt/harness/hop/ordinal/profile/perspective/leg index. Batch minting requires a one-shot capability bound to the exact `dispatch-batch.py` parent PID/start and interpreter/script slot. A single missing-leg recovery proves every other N-1 manifest member active or completed and seals the sorted peer-set digest; missing, duplicate, foreign, terminal-failed, or incomplete peers reserve zero slots. Full-N capacity shortage likewise creates zero rows and zero model processes. Individual group-member `register`/`start` through `dispatch-node`, `dispatch-chain`, a wrapper, or fallback fails before row/process creation. Idempotent repeats classify exact active/completed rows without consuming capacity. In a transient PID namespace all newly started foreground-scoped wrappers remain alive in the same checked batch call; elsewhere detached lifecycle remains available. The batch emits bounded per-leg receipts and creates no model turn, daemon, broker, worker fan-out, or extra dispatch depth. Schema-v1 exact two-way manifests and `replica_group`/`--replica-group` remain read/CLI aliases for one migration window; new routes and receipts are canonical `parallel_group`. OpenCode is eligible for registered standard+ dispatch-depth-2 dispatch: it implements exact parent binding, foreground lifecycle, and supervisor snapshot parity; its quick/relief surfaces remain a separate authorization path and do not substitute for this parity.

### 8.6. Immediate limit-death handling under SD-15

wrappers watch briefly after launch. If a child exits immediately on session, usage, or authentication limits, mark its row `done` with `note=dead-<reason>` and, when available, `reset=<time>`. Liveness also recognizes anchored short CLI error lines at the end of logs, but a fresh completion or activity transcript wins over a report that merely discusses limits. Wrappers do not retry; the orchestrator chooses redispatch or failover.

### 8.7. Canonical global attempt registry under SD-49 (amended by SD-112 §13.33.2-(8))

dispatch depth 0 resolves the canonical dispatch state root once. This root is determined by the active runtime's install shape and is **not inside the active harness release tree**: an installed checkout resolves a stable per-user state root, a Codex bundle resolves activation-owned mutable root, and only an explicit isolated development checkout uses a checkout-relative path (or an explicit root fixture path). The resolved registry path is passed immutably as `AGENT_DISPATCH_JOBS` to every descendant, and that file's parent directory is the canonical dispatch state root — no reader reconstructs it as `$AGENT_HOME/.dispatch`. **This amendment supersedes the 2026-08 "shared release keeps chain-3" decision**, made before managed-release pruning was observed deleting live dispatch state (2026-08-27); it resolves the prior contradiction between this paragraph and §5.9a's "never reconstruct any dispatch state path as `$AGENT_HOME/.dispatch/...`" in §5.9a's favor. Invoking an adapter wrapper from a linked worktree does not make that worktree the agent home: a valid explicit `AGENT_HOME` wins, otherwise the adapter resolves the installed canonical harness; its source checkout is only a standalone fallback. For a nested launch, `--jobs` may only repeat that inherited absolute path; a cycle-local override is non-authoritative and fails closed. Every actual start first writes one global registered-only row, then transitions its exact claim only with the complete fenced process publication described above. General stable identities bind route/node, logical parent, target harness, and fallback ordinal; replica batches bind the exact parent generation and deliberately exclude display slug/prefix. A duplicate or already-started attempt starts zero children. A global open/lock failure returns `global-registry-unwritable` with zero children and no local-only row. Optional cycle-local files are audit mirrors, never authority. Existing current-contract local-only rows are reconciled by exact `attempt_id` idempotently while preserving timestamp, status, and failure note; legacy or invalid rows remain read-only diagnostics and are never reconciled, mutated, or signalled. The six tab-separated row fields remain `<ISO-time>`, status, repo, worktree, slug, and pipe; status words remain only `open`, `running`, and `done`. Each registered row also seals `launch_home=<resolved launch agent home>` in the pipe so readers (for example Fleet) locate that launch's default `.dispatch/logs` stream directory from the row itself instead of inferring install layout; the key is optional on legacy rows, and readers fall back to their existing root heuristics when it is absent. That canonical registry file's parent directory is the canonical dispatch state root: completion markers, logs, heartbeats, watchdog files, supervisor-state, homes, broker, degradation, workflow, and index/journal state all live under it, derived by one function and never reconstructed as `$AGENT_HOME/.dispatch`. `launch_home` keeps its existing narrower meaning as a legacy row anchor and legacy log-root heuristic only; it is not the dispatch state root and a new row's state root is always derived from the registry path, not stored as a separate field.

### 8.8. SD-67 mutation-node in-place retry and declared sub-session lineage

after an execute failure or partial completion, a mutation node may be redispatched on the same immutable route. A moved `HEAD` is accepted only when that node is declared in `resume_retry_boundaries`, the bound canonical global registry has a different prior attempt for the same route and node, and `HEAD` is a first-parent descendant of the route's original `source_commit`. A declared planned sub-session under that node supplies the same prior-attempt lineage proof and is accepted without route recompilation; its `stage_authority=0` keeps this separate from gate retry accounting. Missing or unreadable evidence and divergent history retain exact-match rejection. Do not recompile or re-pin the route, and never use `git reset --hard` to restore it — that prohibition is about an in-place retry on this same immutable route. An SD-104 continuation is a new successor route and pins the resume-time `HEAD` as its own `source_commit` (SD-128), which is what keeps the pin and the sealed `grounding_roots.cwd` naming one commit; it **declines** that re-pin and keeps the inherited pin whenever any node it will re-run mutates the worktree and already has an attempt anywhere in the route's continuation lineage — the ancestors named by `source_route_id` and `supersession_edges`, not only the immediate predecessor, since a declined continuation records no attempts of its own. An absent row is read as evidence only when the registry is provably the lineage's own (`exact` or digest-verified `aliased` resolution) and actually holds rows for that lineage; anything else — a compat-window substitution, a deleted or truncated `jobs.log` — declines. A decline keeps the inherited pin for the **whole route**, so every node at or before the mutation node meets a moved `HEAD` against an unchanged pin. Since SD-133 that is **adjudicated, not refused**: the guard reads retry evidence across the route's sealed lineage (`route_id` + `source_route_id` + every `supersession_edges` ancestor), so the ancestor attempt that *is* the retry evidence is now findable and SD-67's three conditions decide the launch. A continuation may therefore carry an SD-67 retry; before SD-133 it could not, and in-place re-dispatch on the original route was the only recourse. The lineage is read from the record and is not an authentication boundary — `route_hash` is unkeyed and the named ancestors are not semantically validated, exactly like `resume_retry_boundaries` and `source_commit` beside them. This grants neither an automatic gate retry nor extra retry budget, and does not change SD-65 downstream-node lineage handling.

### 8.9. Post-exit parent-bound reconcile under SD-64/71/77

a conductor can die mid-pipeline (session end, crash, limit) leaving either a plain stale owner row or registered children/unstarted successor nodes orphaned. Since a dispatch-depth-1 owner is normally registered before route compilation, its route context is derived deterministically from exact child rows in the same repo/worktree, including terminal children; conflicting route tuples fail closed. The deterministic orphan classification is: exact conductor attempt death (`pid`+`pid_start` mismatch or gone) AND at least one route completion node without a marker AND (any open child row OR an un-started successor node whose predecessors are all marked). Each registered dispatch-depth-1 owner launch starts one non-model watcher bound to its exact PID/start-time and attempt; it exits when the row is already terminal, or after every exact owner exit invokes the general attempt reconciler. This closes a childless pre-route model/auth/limit failure as `dead-exact-pid` instead of leaving a Fleet-invisible `open` row, while a true orphan takes the bounded cascade below. This avoids a polling daemon and does not consume a model-worker slot. Reconcile first preserves any exact completion marker or typed terminal handoff, then classifies exact process identity before consulting worktree integration state. A missing configured upstream is typed `no-upstream-configured` and affects only push-sync/cleanup eligibility; it never blocks registry hygiene. A host-visible PID/start mismatch, or a namespace-local row's verified outer PID/start mismatch, is conclusive death evidence even when the recorded PID now names a different live process. For an open namespace-local child, Fleet and reconcile use this evidence order: an exact child terminal marker or receipt; authoritative positive child PID/start or a surviving attempt-tagged process; authoritative child PID death; proven parent extinction for an eligible foreground-scoped child; an authoritative empty attempt-tagged scan; an exact fresh heartbeat; then unknown/unverifiable. A fresh heartbeat therefore cannot override proven parent extinction, but a terminal parent word alone proves nothing. The parent exception requires one current registered depth-2 foreground child and one unique current terminal depth-1 owner bound by exact parent attempt, slug, repository, physical worktree, and conflict-free route context, plus either a durable owner exit receipt, authoritative owner quiescence, or watcher-observed exact owner PID/start extinction. Watcher evidence is usable only when its observer PID namespace equals both the current observer and the parent's recorded launch observer (legacy host-visible rows may omit the latter); inaccessible or malformed procfs is never extinction. A detached or ordinary namespace-local worker that lacks this complete proof remains visible and may still use its exact heartbeat. Reconcile then closes an orphan conductor `note=dead-parent-orphaned` and performs one bounded cascade over only open direct children sealed to that `parent_attempt_id`. An exact host-visible child process group is TERM→bounded-grace→KILL reaped only after PID/start and PGID-leader revalidation; an already-gone child row or a registered/claimed row with no atomically published PID is closed as `dead-parent-exited`. A foreground namespace-local child covered by the exact parent-extinction exception is reconciled as `dead-parent-terminated` without a signal; it does not grant signal authority over an unverifiable PID. PID reuse is never signalled: a start-time mismatch proves the recorded child has exited and permits only `dead-parent-exited` row closure. Missing identity on a live or unverifiable process, route conflict, non-group-leader targets, namespace-local rows without the exact parent exception, and legacy live rows are never signalled and remain visible for dispatch-depth-0 handling. The watcher never starts a replacement, retry, successor, or route advance; the resume boundary remains a dispatch-depth-0 decision.

### 8.10. Codex linked-worktree mutation stages are no-commit workers under SD-69; owners are commit-expected

the dispatch-depth-2 boundary is contractual, not a sandbox impossibility — parallel stages must not race `HEAD`, a stage must never claim a commit the runtime did not make, and the route's `source_commit` holds unmoved until stage end; a trusted dispatch-depth-0 or Claude boundary — normally the owning dispatch-depth-1 owner — commits after the stage's own PASS gate and confirms diff attribution before doing so. Such a stage worker's only writable roots beyond the task worktree and the canonical artifact root are the exact primary `$AGENT_HOME/.spec-grounding` directory (created safely if absent) — never all of agent home, and never `.git`. A dispatch-depth-1 owner in a linked worktree is commit-expected instead: Codex resolves and allows a linked worktree's real git dir on its own only under its default `~/.codex` home, and every dispatched run uses a custom masked `CODEX_HOME`, so without an explicit grant the owner's `git commit` dies on `index.lock` with EROFS (verified codex-cli 0.148.0, 2026-08-21; the earlier "protected even when other roots are writable / widening is never an accepted fix" reading is retired — an explicit writable root for the resolved git dir does take effect). The wrapper therefore grants a commit-expected linked-worktree run exactly the primary Git metadata directories a commit touches — the per-worktree git dir plus the common dir's `objects`, `refs`, and `logs` — and nothing else of `.git`, so `hooks/` and `config` stay read-only and a sandboxed worker cannot plant code a later unsandboxed session would execute.

### 8.11. Completion marker bound to the exact attempt row under SD-70

completing a node takes the canonical registry (`jobs.log`) path and the current exact attempt id, not just the route/node pair. It writes the completion marker and an immutable per-attempt linkage atomically first, then idempotently closes only that one attempt row `done note=completed-marker` with the marker as evidence — it never breadth-closes a prior `BLOCKED` row or a later live retry of the same node. A canonical latest-link sibling may be retained for compatibility, but a retry cannot overwrite the immutable linkage used to repair an earlier attempt. Marker write and row close are each idempotent under retry. If the row close fails after the marker is written, the marker is preserved and the command returns a structured nonzero rather than silently succeeding or discarding evidence; reconcile later repairs only that exact marker-backed stale row, never any other row for the route/node.

### 8.12. A sub-session slice reaches its own terminal (SD-130)

`capability-route.py complete` is one transaction that publishes a node's stage marker **and** closes its exact registry row, and a `stage_authority=0` slice needs the second without the first — it holds no stage gate authority, so `complete` refuses it (`subsession-has-no-stage-gate-authority`) before touching the row. A slice therefore closes through `dispatch_completion_join.close_finished_child`, which seals `done note=completed-subsession failure_class=pass classifier_source=completion-join-subsession-terminal-v1` from the same evidence every other closure requires: a quiescent process, a valid terminal envelope, and a readable in-root artifact. A live or unverifiable process keeps the row open (`subsession-not-quiescent`); an envelope naming no readable artifact still closes `dead-invalid-envelope`. `completed-subsession` joins `SUCCESS_NOTES` — the one definition of "this note says the attempt succeeded", owned by `dispatch_contract` — so `complete_subsession_stage` can aggregate the chain. It grants **no** marker eligibility: SD-94's marker-eligible test and the `supervisor_terminal` predicate ask *which producer* closed the row, stay bound to `completed-supervisor`, and are never widened. Before this note a slice had no terminal at all and every success was booked `dead-route-completion-rejected failure_class=contract`, which then stalled the whole declared chain (observed 2026-09-03, `att-174d9f66…`, chain indexes 2–5 never launched). Paired with it, a slice may only **start** on a sealed chain: `dispatch-node.py --subsession-id --action start` requires the persisted manifest at `<state-root>/session_chains/<chain_id>.json` to name that `subsession_id` at that index with that attempt id, else `subsession-chain-manifest-unsealed` (`child_spawned=0`, exit 64). `register` is not gated — the manifest is persisted after the register loop and before index 1 starts.

### 8.13. Review verdict is a result, not a worker death (SD-94 owner-closure extension)

a valid terminal handoff from a `worker_type=review` node that reports `verdict: FAIL` **and** names a readable in-root review artifact closes its exact row `done note=completed-review-blocking` — the reviewer completed its contract by recording blocking findings. `failure_class` keeps the verdict axis unchanged, and the foreground wrapper tail, the supervisor join (`dispatch_completion_join.close_finished_child`), the fallback wrapper's terminal race, and the registry reconcile carrier (`dispatch-registry.py classify`) all classify it identically; each of them seals the artifact the reviewer named as `review_artifact_b64`. It is a typed completion: the fallback chain neither spends the harness tuple nor descends to another hop for it (the launch receipt reports `terminal_note`/`review_verdict` instead), the owner's delivery receipt still says `inspect-done-failure`, and Fleet shows the row as done with that note. It is never `dead-worker-fail` — that note, and every other `dead-*` note, stays reserved for a worker that did not finish (crash, auth, limit, protocol, missing or malformed envelope, `FAIL` without a readable artifact, or any non-review node). The round budget (`CONVENTIONS §1.1`) still counts the row: a blocking round is a spent round. Such a row becomes marker-eligible through exactly one evidence-bound path — `complete --jobs --attempt-id <that row>` whose `--evidence` is an owner-closure record — and the gate admits it only when (a) the node kind is `review-worker` and the row's `worker_type` is `review`; (b) no review round of the node is still `open`/`running` (`owner-closure-round-still-open`) and the *terminated* rounds alone exhaust the node's round budget for the route's intensity (`owner-closure-round-budget-not-exhausted`: while budget remains, a correction round is the answer, not a ruling); (c) the node has no canonical completion marker from another attempt (`owner-closure-node-already-complete` — SD-70's one-node-one-attempt binding); (d) the exact attempt log re-inspects as a valid `FAIL` handoff with a readable in-root artifact (`owner-closure-review-artifact-unverifiable`); (e) the evidence path is registry-safe (no `,` `=` tab newline or control character: `owner-closure-evidence-path-unsafe`), is inside the route's artifact root, is named `*.owner-closure.md`, is not the review artifact itself, and carries frontmatter `verdict: closed-by-owner` and `node: <node_id>` (plus a matching `gate:` when present) with no duplicated key; and (f) its body names every `completed-review-blocking` attempt id of that route node and the review artifact's basename as whole tokens. The marker's evidence is the closure record; the row's closure facts (`gate_closure=owner-closure`, `owner_closure=<path>`, `review_artifact_b64`) are sealed through the same sanitizing terminal-evidence writer as every other terminal value, before the marker is published, and only then `note=completed-marker` is appended; the row keeps `done` and never gains `failure_class=pass`. Every refusal is typed `owner-closure-*`; a `dead-*` row, a missing record, a bare flag, or a record that names no attempt keeps the SD-94 fail-closed refusal. Observed 2026-09-02 on three cycles whose verification gate could not close because a productive review was booked as a dead worker: rt-1b8f7f609bdb4090 plan-check rounds 1–2 (owner closure recorded in `_internal/plan_reviews/round_2.owner-closure.md`), rt-c744d4d89c7fe1e4 plan-check round 1, rt-23728d301917bcc0 impl-review.

### 8.14. A review gate names its reviewer, and a self-review is degraded, not refused (SD-OPEN-41(b), SD-94 extension)

the completion marker of a `review-worker` node carries `reviewer_kind` (`registered-worker` | `native-subagent` | `owner-inline`), `review_independence` (`independent` | `degraded`), and the reviewer's identity — an attempt id, or a native-subagent transcript path with its sha256. The kind is adjudicated against evidence, never accepted on the caller's word. A `complete --reviewer-attempt <att>` claim is verified against the row in `--jobs`: the row must exist and carry `worker_type=review`. A `complete --reviewer-subagent <transcript>` claim requires a readable regular file, and the recorded digest is what makes the identity checkable afterwards; per the user's rule a native subagent with a recorded identity **is** independent review. With no claim the completing attempt is the reviewer, which is independent only when its own row says `worker_type=review` — `registered_worker=1` alone was never proof of that. Every failed claim (row absent, wrong `worker_type`, unreadable transcript, no registry to adjudicate with) **downgrades to `owner-inline` with a typed `reviewer_downgrade_reason`; it never refuses.** Refusal was tried and withdrawn (SD-132): every review node seals `native-subagent` and `inline` as its last two fallback hops, so a guard that refuses them deadlocks the dependent node forever. A degraded gate proceeds, and the fact travels with it: the row carries the same three axes beside `note=completed-marker`, `complete` prints `completed-review-degraded` on stderr, the closed outcome carries `review_independence` per node plus `review_independence_degraded`, and the `§0.5` completion card must say the gate was not independently reviewed. The note stays `completed-marker` on purpose — `dispatch_contract.marker_attempt_readiness` and `complete`'s own already-closed branch read that exact literal to mean "this row terminated with a marker", so spelling the degradation into `note` would make an idempotent second `complete` refuse the row it had just closed. A review node completed before these fields existed reports `unrecorded` rather than being read as independent. Measured 2026-09-06 over canonical markers under the dispatch state root's `completion/` (excluding `*.attempt.json` and history siblings): the total depends on the predicate — 67 whose route record still declares `kind=review-worker`, 2,911 whose node id merely contains "review" — so quote the count with its predicate. Stable across both: the axes already separate registered from inline, **12** are inline, and among those nothing separated an owner ruling on its own work from a subagent that actually reviewed it.

### 8.15. Terminal authority and actionable receipt under SD-97

a runtime supervisor's exact final `turn.completed` or Claude `result` handoff outranks a wrapper-side foreground tail observation for the same attempt. A stronger later observation may repair the row while preserving the prior note, source, and failure class as conflict evidence; equal-authority contradictory verdicts close as `dead-terminal-conflict` and never manufacture PASS from artifacts or tests. A terminal registry row with a complete foreground reap or detached group-drain receipt is execution-quiescent even when the observer namespace has exited; a stale summary/UI heartbeat cannot override that exact post-exit proof. Receipt schema v2 gives every joined child exactly one `required_action`: `complete-open`, `inspect-done-failure`, or `advance-completed`. The registered-owner supervisors, managed Codex gateway, Claude async-rewake bridge, pre-tool guard, and harvest selector consume that same action and status, so a terminal row cannot become an unharvestable `matched=0` receipt. Only an actionable model resume consumes the supervisor continuation limit; registry-only preparation and delivery bookkeeping do not. The default limit is route-derived rather than a fixed constant: use the larger of the compatibility floor and the bound route's declared node count plus one retry slot for each unique `resume_retry_boundaries` node. An explicit positive owner-launch override may replace that value. Missing, unreadable, mismatched, or unbound route evidence retains the finite compatibility floor; it never creates an unlimited supervisor. “Retains” includes D47-6's terminal reserve (`reserved_remaining >= 1`): the floor path is never discarded or reduced to a zero-reserve budget merely because route binding failed. A declared 13+ continuation chain therefore reaches terminal report harvest and final handoff, while attempts beyond the declared chain plus retry headroom remain `continuation-limit-exceeded`.

### 8.16. Owner route binding and duplicate launch receipt under SD-97

`dispatch-owner --route-evidence` verifies the sealed route against cwd, capability, mode, intensity, route hash, and selected owner harness, then forwards an owner-level binding. Each adapter wrapper revalidates it and exports `AGENT_ROUTE_FILE` and `AGENT_ROUTE_ID` with an empty `AGENT_ROUTE_NODE`; an owner is never fabricated as a route node. This lets the owner use the declared inline fallback while retaining the material route guard. An exact duplicate claim still starts zero children and stays idempotent, but every wrapper emits `launch_state=existing-active|existing-completed` instead of a silent success-shaped no-op; batch callers may accept the typed existing state, while a caller requiring a new start must branch explicitly.

### 8.17. Post-launch owner-route lifecycle under SD-97

a registered dispatch-depth-1 owner may legitimately start without route evidence and compile generation 0 after launch. The compiler then attaches that immutable route to the exact owner attempt through a separate atomic lifecycle record; it never rewrites the route or the launch-sealed registry tuple. Publication holds the canonical registry lock and verifies the current unique owner row, active state, attempt schema, worker/depth/surface, parent session, owner harness, physical worktree/repository, capability/mode, artifact root, route id/hash, generation, and route owner identity. Exact replay is idempotent; a partial launch binding, a second generation-0 candidate, hash drift, unrelated owner/worktree/session, or a close/start race fails closed. Continuation publication records an immutable target-keyed candidate under its exact predecessor only after the successor route exists, but compilation alone does not advance the binding: adoption additionally requires a current-contract registered depth-2 child row for that exact owner, route file/id/hash, worktree/capability/mode/artifact tuple, and `launch_started=1`. A childless candidate is inert and may coexist with a later candidate; exactly one child-adopted target advances the current owner route, while two child-adopted targets are a competing-successor conflict. The current owner route advances monotonically through exact generation `n -> n+1` edges whose source hash, successor hash, supersession metadata, owner identity, and worktree contract all verify; downgrade, gap, competing active successor, tampered lineage, or an unrelated route cannot take over the binding. Consumers resolve the launch binding or post-launch attachment first and then this verified edge chain, so retained child evidence preserves the current generation across restart.

### 8.18. Fleet owner-lineage projection under SD-97

group, process, and JSON views use the same authoritative current owner generation. A valid explicit launch/attachment binding is folded through the verified successor chain even while a superseded source route remains open. When no explicit binding record exists (including legacy attempts), Fleet may recover only a single linear chain formed from exact owner-linked child attempts and terminal attempt evidence, with every route hash, generation, source/successor edge, owner/worktree identity, and reuse contract verified. A compiled route with no owner-linked child evidence is not an active stage candidate. Two real successors, disconnected or unverifiable lineage, or conflicting explicit evidence remains the typed `multiple-owner-routes` ambiguity; timestamp ordering and "latest route" heuristics are forbidden.

### 8.19. Declared runtime requirements and lifecycle evidence under SD-97

a node may declare only registry-known `runtime_requirements`. `loopback-listen` means localhost bind while outbound network remains denied; a runtime that exposes only a broad network boolean reports `loopback-only-unsupported` and uses the checked main/inline handoff rather than widening outbound access. Every registered row records the requested launch lifecycle plus bounded selector evidence (source, NSpid width, and PID-1 class) so an intermittent nested-sandbox lifetime failure is diagnosable without transcript inspection.
