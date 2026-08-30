# Operations — Git, Worktree, Dispatch, and Push (canonical)

> Split from `CONVENTIONS.md` on 2026-06-23 when front- and back-half contracts were separated. Git operations—locks, preflight, worktree dispatch, and `<agent-home>` pushes—differ from artifact conventions and therefore live here. Preserve section numbers and headings because Skills, drills, and hooks link to anchors such as `OPERATIONS.md#59-…`. This is the single source for git operations.

## §5.8. Pipeline Lock — Guarding a Shared Artifact Root Across Worktrees

Every project has one canonical artifact root—`.agent_reports`, with legacy
`.claude_reports` compatibility—resolved by
`utilities/artifact-root.sh <cwd>`. Linked task worktrees are source-only:
their tracked artifact snapshots are read-only, while all workers share the
primary checkout's canonical root through `AGENT_ARTIFACT_ROOT`. Simultaneous
writes to shared `spec/prd.md`, `pipeline_state.yaml`,
`pipeline_summary.md`, or the `_internal/versions/v{N}/` chain can lose updates
or allocate the same version twice. `plans/<cycle>/` is path-separated by
cycle and does not require this lock.

- **Lock file:** `<artifact-root>/.pipeline-lock`, visible to all worktrees. The holder keeps an OS advisory lock for the full transaction, so process exit releases ownership without a stale-age override.
- **Protected scope:** the complete spec transaction is one atomic sequence while the lock is held: re-read latest state → allocate the next version and persist the exact current `prd.md` pre-image → run the owning `prd.md` + `pipeline_state.yaml` + `pipeline_summary.md` update → retain and verify the snapshot only when `prd.md` changed. The helper prepares the pre-image before the child command, so an interrupted overwrite cannot lose the prior PRD. Initial creation and no-op updates create no version. Reads outside a transaction and path-separated plan writes do not lock.
- **Route declaration:** a route that can touch any `spec/**` path declares `spec_touch=true`. Before lock acquisition the conductor runs §5.9 git-state checks. Missing declaration or a route/node scope mismatch is a structured failure tied to the route id.
- **Contention:** a nonblocking acquisition first reports `BLOCKED`, then waits. After acquiring it re-reads the latest spec and version chain and enters the next version. It never retries a previously computed `v{N}` and never overwrites an existing snapshot.

Acquire immediately before `autopilot-spec` Step 3 or update mode, `autopilot-code` state/summary writes, or a spec-drift update. The helper holds the lock around the supplied transaction command, prepares and verifies the prior PRD bytes itself, and exports `AGENT_SPEC_NEXT_VERSION` only after the latest version is re-read under lock. Callers must not create or validate the snapshot themselves:

```bash
REPORTS_DIR=$("${AGENT_HOME:-$HOME/hearting}/utilities/artifact-root.sh" "$PWD") || exit
python3 "${AGENT_HOME:-$HOME/hearting}/utilities/spec-transaction.py" run \
  --artifact-root "$REPORTS_DIR" --worktree "$(pwd -P)" \
  --route "$ROUTE_RECORD" --node "$ROUTE_NODE" --wait-timeout 600 -- \
  sh ./the-owning-capability-transaction.sh
```

Exit 3 means the bounded wait expired; report the current owner and leave every spec surface unchanged. Do not delete the lock or reuse the version number.

`--require-snapshot` is a deprecated compatibility flag and has no authority;
snapshot enforcement is unconditional whenever an existing `prd.md` changes.
An existing `v{N}/prd.md` must be byte-identical to the captured pre-image, and
an empty `v{N}/` never satisfies the transaction.

The helper releases automatically after normal completion, interruption, or error. The transaction command must fail before its first canonical write if all four output paths cannot be completed.

For a read-only check before touching the spec:

```bash
REPORTS_DIR=$("${AGENT_HOME:-$HOME/hearting}/utilities/artifact-root.sh" "$PWD") || exit
[ -s "$REPORTS_DIR/.pipeline-lock" ] && cat "$REPORTS_DIR/.pipeline-lock" || echo "no active edit"
```

In a single-checkout environment the same helper and sequence still apply.
With linked worktrees, the canonical resolver makes one lock visible without
replacing a tracked directory with a symlink.

### §5.9. Git Working-State Preflight

The §5.8 lock protects only artifact writes. It does not detect an active merge or rebase, dirty files, detached HEAD, or the same branch in another worktree. A code-mutating capability, canonically `autopilot-code`, checks once before editing and again before every commit or write-back.

```bash
# Run before code edits and every commit. On STOP, halt and report.
GD=$(git rev-parse --git-dir 2>/dev/null) || { echo "OK non-git"; return 0 2>/dev/null||exit 0; }
op=; [ -f "$GD/MERGE_HEAD" ] && op=merge
{ [ -d "$GD/rebase-merge" ] || [ -d "$GD/rebase-apply" ]; } && op=rebase
[ -f "$GD/CHERRY_PICK_HEAD" ] && op=cherry-pick
br=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo DETACHED)
head=$(git rev-parse --short HEAD 2>/dev/null)
ahead_behind=$(git rev-list --left-right --count @{u}...HEAD 2>/dev/null)
elsewhere=$(git worktree list --porcelain 2>/dev/null | awk -v b="$br" '/^worktree /{w=$2} /^branch /{if($2=="refs/heads/"b && w!=ENVIRON["PWD"]) print w}')
def=$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@'); def=${def:-main}
git fetch -q origin "$def" 2>/dev/null
merged_in=$( [ "$br" != DETACHED ] && [ "$br" != "$def" ] && [ "$(git rev-list --count origin/$def..HEAD 2>/dev/null)" = 0 ] && echo yes )
if [ -n "$op" ];        then echo "STOP: $op is in progress; resolve it or abort explicitly before continuing"; fi
if [ "$br" = DETACHED ];then echo "STOP: detached HEAD($head); check out a branch before risking a lost commit"; fi
[ -n "$elsewhere" ] && echo "WARN: branch '$br' is also checked out at $elsewhere"
[ "${ahead_behind%%	*}" -gt 0 ] 2>/dev/null && echo "WARN: upstream is ${ahead_behind%%	*} commits ahead; integrate before continuing"
[ -n "$merged_in" ] && echo "DONE-BRANCH: '$br' is zero commits ahead of origin/$def; start new work from the latest base: git switch -c <new-slug> origin/$def"
echo "state: branch=$br head=$head base=$def dirty=$(git status --porcelain 2>/dev/null|wc -l|tr -d ' ')"
```

- **STOP:** halt edits and commits during merge, rebase, cherry-pick, or detached HEAD and ask for handling. Never auto-abort or force-checkout. `hooks/git-state-guard.sh` hard-denies Edit and Write calls during an operation, including direct paths outside ceremony. `$GITDIR/CLAUDE_MERGE_EDIT_OK` is allowed only when the user explicitly requests conflict resolution; an agent must not invent that permission.
- **WARN:** report one line for the same branch in another worktree, upstream movement, or pre-existing session-independent dirt, then decide how to proceed.
- **DONE-BRANCH:** after a branch is merged into base it is finished. At a new work cycle, a non-base branch that is zero commits ahead and is not a just-created branch for this task must be replaced with `git fetch origin && git switch -c <slug> origin/$def`. This applies to direct edits too; uncommitted work on a dead branch is already drift.
- **Periodic recheck:** remember the entry `head`. Before each commit, stop if `head` changed underneath the session or a new `MERGE_HEAD` appeared. Non-git and single-checkout paths pass harmlessly.

### §5.9a. Immutable Runtime Activation and Session Pinning

The active harness used by an interactive runtime is an immutable packaged
release or content-addressed local snapshot. This is also the maintainer
default: a mutable checkout is development input, not the active runtime.
Linked activation is an explicit debug-only exception and must report that it
cannot guarantee session consistency. An update publishes and verifies a new
root, then atomically changes only the pointer used by new sessions.

At process entry each runtime resolves that pointer once, exports the exact
real `AGENT_HOME` and its runtime identity, and passes both unchanged to hooks,
managed-entry, and registered children. A running session never follows a
later pointer change. Runtime-owned credentials, sessions, logs, caches,
databases, and Codex `config.toml` remain outside this activation boundary.

### §5.10. Work Isolation and Parallel Dispatch

Adapter and projection changes follow the same core-first order as other portable work: establish and read the governing `core/` contract before adapter edits. Read and write markers enforce that gate but do not replace review. A generated projection's determinism covers its file mode, not only its bytes: a generator that writes plugin JSON (`hooks.json`, `plugin.json`, marketplace manifests) fixes the mode to `0644` on every write regardless of process umask, and its `--check` counterpart fails a foreign mode as a stale projection alongside a content mismatch (S-5d, owner-supervisor-liveness — a reproducible regenerate cycle flips `hooks.json` away from `0644`; the first mutating syscall was not isolated, so the fix enforces the invariant rather than only diagnosing it).

Actual edits, tests, and QA run in isolated worktrees while the main session
handles triage, dispatch, harvest, and reporting. Portable `dispatch_depth`
describes logical route ownership independently of transport, process ancestry,
runtime-native nesting, and registered-worker status:

- **dispatch depth 0:** user-facing main or orchestrator;
- **dispatch depth 1:** capability-owner route node;
- **dispatch depth 2:** bounded planning, verification, perspective,
  adversarial, or pipeline-stage route node opened by a `standard+` owner;
- `direct` runs inline at dispatch depth 0; `quick` uses exactly one
  registered-headless dispatch-depth-1 owner; dispatch depth 3 or greater is
  forbidden.

The portable role for a `standard+` dispatch-depth-1 owner is `deep orchestrator`. The retained `orchestrator` role is balanced mechanical coordination of already decided commands, paths, and states; they are not aliases.

**Main-session role contract.** The dispatch-depth-0 main session is the context owner, router, orchestrator, and final integrator — not the default executor of every stage. It directly owns: memory and existing artifact-root recovery (`.agent_reports/`, legacy `.claude_reports/`); user-intent and artifact-state reconstruction; compact-metadata primary/secondary capability selection under `WORKFLOW §0.2`; the completed route-confirmation card under `WORKFLOW §0.4`; spec, guard, worktree, and currentness checks; work decomposition and write-ownership decisions; worker dispatch with registration, liveness watching, and harvest; cross-stage conflict and semantic decisions; final consistency integration of metrics, documents, and notes; and the user-facing response. Before confirmation, main does not preload the full entry Skill or its references. At `standard+`, the dispatch-depth-1 capability owner reads that contract, and each dispatch-depth-2 worker reads only its assigned stage contract. When a stage is separable, main does not take on inline: long experiment or evaluation execution, repeated checkpoint inference, bulk figure/media generation, report HTML assembly, mechanical document synchronization, independent verification/QA, or implementation work with a clear file boundary against other stages.

**Main/worker bootstrap boundary.** Every registered headless dispatch and repo-owned background model caller exports `AGENT_SESSION_ROLE=worker` before launch. Legacy adapter markers remain worker evidence and fail closed. The portable worker input is `roles/worker-bootstrap.md` plus exactly one `roles/worker-types/{owner,stage,review,support}.md` fragment and the assigned capability/stage contract. `worker_type` selects only that bootstrap fragment; `capability` plus `assigned_contract`/`route_node` select the work contract; `model_role` selects behavior; route-sealed `model_profile` selects the execution budget. These axes never select or rename a bootstrap. `worker_role` is legacy read-only metadata. Workers retain deterministic safety, permission, scope, route validation, handoff, liveness, and verification guards, but the harness does not add main response policy, entry confirmation, automatic memory/briefing/turn-nudge/distill, SessionEnd sync/curation, Fleet title/token/UI context, the full capability catalog, unrelated stage contracts, integration, merge, push, cleanup, or user-facing explanation. A worker never manufactures a main session, and its shutdown never launches a curator. This boundary applies to dispatch depth 1, dispatch depth 2, loop/drill invocations, title/distill workers, and runtime-native subagents; dispatch depth, bootstrap type, assigned contract, model role, and model profile remain separate.

Worker lifecycle suppression, harness-controlled prompt isolation, and physical runtime masking are separate support claims. An adapter removes any explicit full-main-bootstrap read it controls. If a runtime automatically inherits project instructions and offers no verified per-worker disable switch, document that residual input and use the minimal typed overlay as the checked fallback; never label it fully masked. Worker details go to the canonical artifact. The final worker output is exactly `artifact: <path|->`, `verdict: PASS|FAIL|BLOCKED`, and `blocker: none|<one line>` on three lines. Material registered work requires an artifact; `-` is limited to atomic read-only support.

**Inline exceptions.** Main or a dispatch-depth-1 owner may run such work inline only when at least one holds: the work is `direct` scale or a micro-stage inside the checked `quick` one-shot owner; file or state boundaries make it genuinely non-separable; every route-sealed registered-headless candidate has a typed hard-unavailable result and the compiled fallback policy reaches inline; the work is tightly coupled to external GPU or process state a worker cannot reach; the user explicitly requires main-session execution; or the stage is so small that dispatch overhead clearly exceeds it. A native-subagent restriction is surface-local and is never evidence that registered headless is unavailable. A failed hook-trust or worktree-safety check is a stop/re-isolate condition, not inline authority; `failure_scope=exact-worktree,retry_on_isolated_worktree=1` retains the stricter fail-close rule below. Running `standard+` separable work inline without recording the concrete reason — in `plans/<slug>/_internal/metrics.md` for code cycles, or the experiment `_RUNLOG`/`_internal` for lab cycles — is a contract violation; this generalizes SD-17 beyond code stages.

**Durable capability participation.** A capability route is required before the first durable write under a capability-owned artifact bucket, including `direct`; direct means an inline node in a compiled route, not route absence. A route card, Skill/capability grounding marker, `pipeline_state.yaml` field such as `execution: inline`, or a failed native-subagent probe is not participation proof. Main sessions bind the checked route to the exact session/cwd; registered workers use their immutable route environment. Standard+ artifact writes therefore fail closed when route compilation fails, before public or `_internal` output can be used as retroactive authority.

**Delegation surfaces are distinct.** Use four exact runtime nouns. A **Codex
native subagent** is a Codex child agent thread governed by Codex-native agent
settings such as `agents.max_depth`. A **Claude subagent** is a runtime-native
child within one Claude session that returns its result to the caller. Together,
and only together, those are *runtime-native subagents*. A **Claude agent-team
teammate session** is a separate peer Claude Code session that can communicate
with teammates; it is not a Claude subagent. A **registered headless worker
session** is a separately launched wrapper process (`claude -p`, `codex exec`,
or the checked OpenCode equivalent) bound to an immutable route/node/attempt,
the canonical registry, liveness, and completion gates. Team membership,
runtime-native child status, and route dispatch depth never imply registered
worker status. A restriction on one surface must not be silently extended to
another. Concretely, a main-session lifecycle predicate decides worker status
only from markers the harness itself plants at launch (`AGENT_SESSION_ROLE=worker`,
`AGENT_DISPATCH_CHILD=1`, `AGENT_DISPATCH_DEPTH`). A runtime-owned session marker
such as `CLAUDE_CODE_CHILD_SESSION`, which a runtime injects into every child
process an ordinary interactive session spawns — hooks included — also appears in
agent-team teammate sessions, so it is never standalone worker evidence. A
dispatch launcher may keep exporting such a marker for observation collectors
that read initial process environments; that producer role does not make it a
lifecycle predicate term.

| Scale | Handling |
|---|---|
| One-off typo, one line, or `direct` work | Work directly in the main working tree |
| Small work routed to `quick` because atomic-direct predicates are incomplete and no promotion signal is present | Registered-headless dispatch-depth-1 one-shot conductor in an isolated worktree |
| Work promoted to `standard+` by durable scope, shared-contract, resource, resume, verifier, or separability signals | Use a worktree and task branch from the latest base. Features, new modules, and multi-file edits always use a branch; ambiguity resolves toward a branch. Separable multi-file or feature work at `standard+` must use headless dispatch. Team delegation and inline micro-stages are limited to `quick` and genuinely microscopic stages. |
| A new independent request while work is active | Dispatch immediately to a new worktree; do not wait for the first job |

**Token-pressure non-interference:** token or context pressure cannot downshift this table, remove a required stage or depth, skip liveness and registry handling, or weaken worktree, write, spec, sandbox, approval, safety, validation, security, or accessibility guards. Unknown or exceeded budgets preserve the pipeline and surface degraded availability. Only unrequested optional exploration and user-facing verbosity may shrink.

Dispatch rules:

**Owner-supervisor liveness correction (SD-78/86/91/92/97).** Codex
`app-server-supervised` and Claude `session-resume-supervised` owner rows both
seal and hold the same canonical exact-attempt `flock-v1` lease while the outer
supervisor lives. The shared classifier combines a held lease with
`parked|deliverable|recovery` phase and reports `parked-supervised`; Fleet and
the orphan watcher consume that verdict, so an inner turn exit is never
owner-death evidence. Missing, foreign, nonce-mismatched, symlinked, unlocked,
or terminal-row lease evidence fails closed. Phase edges append outer PID/start
and before/after phase to the attempt-scoped transition audit. This shared rule
supersedes the Codex-only lease wording later in this section.

**`parent-runtime-supervised` completion delivery (SD-113).** A row whose
`parent_completion_delivery = parent-runtime-supervised` never gets a
pending-delivery record — its completion delivery is owned solely by the
SD-78 supervisor outbox above, not by the `delivery_intent`/`RECIPIENT_KINDS`
stamp path in `core/HOOKS.md`.

`AGENT_DISPATCH_JOBS` is the sole canonical dispatch registry. Its default
fallback (SD-112 §13.33.2) is the canonical dispatch state root's `jobs.log`:
`${XDG_STATE_HOME:-$HOME/.local/state}/hearting/dispatch`, or the
installer-owned `HARNESS_STATE_ROOT` override, resolved by `stable_state_root()`
(`tools/install/distribution.py`, mirrored by `utilities/dispatch_contract.py`
`resolve_dispatch_state_root()`). This root is **not inside the active harness
release tree**: an installed Codex bundle source instead derives the
activation-owned mutable `<runtime-home>/.harness/dispatch/jobs.log` from
`<runtime-home>/.harness/bundles/<id>/source` (unchanged by SD-112), and a
shared `hearting/releases/<version>` source or a maintainer checkout now
default to the stable root; a maintainer checkout keeps a checkout-relative
`.dispatch/jobs.log` only as an explicit isolated opt-in or as a migration
source/fixture path.

**This supersedes the 2026-08 "a shared release keeps its release-relative
`.dispatch/jobs.log`" decision (the former state-root chain (3)).** That
decision assumed a user-writable release-relative root whose *content*
release rotation could carry forward into the successor release; a
2026-08-27 fault showed managed-release pruning could instead delete live
dispatch state, because succession moves file content but never the
release-relative *path identity* that sealed `launch_compatibility_tuple`
values and open rows already depend on.

A bounded, versioned migration (`run_dispatch_state_migration()`, M0 preflight
through M6 delta sweep, `tools/install/distribution.py`) promotes the stable
root without rewriting any sealed `launch_compatibility_tuple` or open row: it
journals a versioned migration-alias record instead, and
`revalidate_launch_compatibility` accepts the alias only at the resolver
stage, once the record is `completed` and its digest verifies — this rescues
a pre-start route too, since it does not depend on a completion marker
existing. Until the legacy read window closes (two consecutive supported
releases *and* zero legacy-bound open writers/delta/read-hits), readers
consult up to three roots read-only, deduplicated:
`(stable_state_root(), <active-release>/.dispatch, <agent_home>/.dispatch)`.
The pre-SD-112 succession-carry mechanism — `_cleanup_releases`'s row-wise,
terminal-precedence merge of a candidate release's registry into the live one
(`_succeed_dispatch_state`), and its `launch_home=`-based open-row protection —
remains as the fail-closed safety net that keeps a release alive while any
legacy-bound state has not yet been proven quiescent; it is a
backward-compatible carry path for legacy release-relative state, not the
primary resolution chain. That carry is still row-wise and monotonic: it never
reverts a terminal attempt row to open, and a merge that cannot prove that
invariant writes nothing and keeps the candidate release. Succession is not a
sufficient condition for deletion: before pruning a release, `_cleanup_releases`
also checks both the candidate release's own registry and the live release's
registry for an open row whose `launch_home=` names that candidate, and keeps
the release if either check finds one or the evidence cannot be read — a
release a live attempt still references is never pruned. Explicit or
inherited registries inside a bundle's versioned `source` tree are rejected
with `versioned-source-registry-fallback`. Completion, logs, watchdog,
heartbeat, and supervisor state continue to derive only from the accepted
registry's parent.

Before delivery, the supervisor atomically commits the bounded receipt payload,
deterministic receipt id and digest, exact attempt set, and row revisions. A
restart reuses that committed payload and identity. The guard and prompt treat
copied status/action as hints and select the current exact row action. Successful
`complete-open` or `inspect-done-failure` harvest consumes only that attempt once;
`advance-completed` is consumed after current-row revalidation. Partial batch
consumption preserves the same receipt identity, and state/outbox removal before
all applicable actions succeed is forbidden.

A checked verification runner records its exact attempt/route/node, live
PID/start/leader-PGID, actual argv digest, start, and bounded deadline beside the
canonical registry. Only a live, unexpired, exact binding whose current
`/proc/<pid>/cmdline` digest matches the lease pauses watchdog quiet-window
accumulation. Stale, foreign, reused-PID, changed-command, malformed, or expired
leases fall back to ordinary no-progress handling, and the runner removes the
lease when the command exits.

Normal and capacity fallback attempt hashes include the exact
`parent_attempt_id`. Retries under one parent remain idempotent; a successor
owner generation receives a distinct attempt even when it reuses the same slug.
A legacy hash collision is diagnostic
`attempt-identity-parent-generation-conflict`, never launch evidence.

1. **Overlap triage:** if a new request is likely to touch the same files as an active job, queue it behind that job on the same branch. Otherwise it may run in parallel.
2. **Execution and naming:** create the worktree with `git worktree add <path> -b <slug> origin/<base>` using §5.9 base selection. The sole canonical path is the sibling directory `<repo>-wt/<slug>`, such as `Foo-wt/<slug>` for `Foo`; do not invent `<repo>_worktrees/`. `worktree-path-guard` hard-enforces this naming for `git worktree add`, while `WORKTREE_GUARD_BYPASS=1`, non-add subcommands, and non-git contexts fail open.
   - **Source-only worktree:** immediately resolve the primary checkout's
     canonical artifact root. Dispatch wrappers inject it as
     `AGENT_ARTIFACT_ROOT`, include it in prompt/registry metadata, and open
     only that external path through runtime-native scoped access (Claude/Codex
     `--add-dir`; OpenCode exact `permission.external_directory` rule).
     Writes to the task worktree's `.agent_reports/**` or
     `.claude_reports/**` snapshot fail closed.
     Only the topology-sealed `autopilot-lab` `publish` node (`lab-publish`)
     resolves the create-once Hearting `REPORT_BUNDLE_ROOT` setting and projects
     that exact directory as an external writable root. Setup, media, report,
     independent verification, and sync stages receive no bundle-root grant.
     Wrappers never widen access to its parent or embed the absolute root in
     artifact-sink receipts.
   - **Light team delegation:** open a team agent in the background and name the work root in its prompt. The main session opens QA against that same path. Use only for small, fast iterations.
   - **Quick one-shot:** compile one dispatch-depth-1 conductor and launch it only
     through a checked registered-headless wrapper. Its micro-stages stay inline
     inside that worker, it opens no dispatch-depth-2 child, and any mutating
     quick job uses an isolated worktree. Unsupported or exhausted checked
     headless candidates fail as `quick-headless-unavailable` or
     `quick-registered-headless-exhausted`; quick never degrades to a native
     subagent, teammate, interactive wrapper, or inline attempt. The wrapper
     continues to encode this conductor with the compatibility owner worker
     type and `_kernel/owner`; standard+ capability ownership is a distinct
     semantic responsibility.
   - **Full headless ceremony:** launch an adapter-specific headless main in the worktree. It acts as a complete main for that runtime, including team roles, hooks or preflight, and plan artifacts. The adapter owns noninteractive tool and permission setup and documents its cost realization. The top-level dispatch is a dispatch-depth-1 capability owner that returns only synthesis to main.
   - At `standard+`, the dispatch-depth-1 owner is a thin conductor. It dispatches compiled `frame`, `code-plan`, `plan-check`, `code-execute`, `impl-review`, `code-test`, and `code-report` nodes through dispatch-depth-2 headless sessions, reads verdict/status metadata rather than stage bodies, and passes context only through files. A route stage is the semantic work and completion-gate unit; a worker session is only an execution-capacity unit. They are not one-to-one. The owner may keep a stage in one session or declare bounded first-class sub-sessions below the same route node when scope size, context pressure, or round-trip cost warrants it. That choice is owner discretion and does not require route recompilation. A declared `parallel_group` replaces member-level starts with one exact `dispatch-batch --parallel-group` transaction. Before any stage, the owner verifies that the artifact root and `spec/` exist.
   - **Stage-session separation:** every planned sub-session carries a stable `subsession_id`, ordered index/count, serial-or-parallel mode, fixed file list, narrow verification command, expected round trips, phase brief, and worker-state ledger. It retains the parent route id/hash/node, stage scope, and gate, but records `stage_authority=0`: it may produce a bounded handoff and terminal attempt result, never publish or satisfy the stage completion marker. The dispatch-depth-1 owner publishes exactly one stage marker only after all declared sub-sessions are semantic-terminal, execution-quiescent, and their combined stage evidence meets the original gate. Planned subdivision consumes no gate-failure retry budget. A later gate failure may open only a gap session containing unfinished items from the prior handoff; it is a retry, not retroactive subdivision.
   - **Auxiliary legs are advisory, never gate-holding.** A declared `parallel_group` leg with `leg_class: auxiliary` widens the group with one closed narrow check on the `light` budget. Its unit verdict enum carries no blocking token, so an auxiliary finding can never satisfy or fail the stage gate alone — it exists to feed the arbiter's `auxiliary_findings_considered` merge (the completion gate compares the merged array length against the realized auxiliary leg count). The `all` join policy is a separate axis: it joins every realized leg, including auxiliary ones, but joining evidence is not the same as letting an auxiliary verdict block. At least one realized **peer** leg must land on a quality-peer harness (SD-100 ①); auxiliary legs may legitimately use any eligible harness including OpenCode.
   - **Who arbitrates, and when.** The arbiter of an auxiliary-bearing group is never the group's own anchor — the anchor is a sibling that runs *concurrently* with the auxiliary leg and cannot have read its output. The arbiter follows the anchor's kind: a `review-worker` anchor is merged by the owner (conductor); a `map-worker` anchor is read by its declared downstream consumer node; a `pipeline-stage` anchor by its direct downstream `review-worker`. A **node** arbiter carries `auxiliary_findings_considered` in its own completion evidence, with exactly one entry per realized auxiliary leg it arbitrates (summed when it arbitrates more than one group). For an **owner-merge** arbiter the owner waits for the group to join, writes the merge record with `auxiliary_findings_considered` in its frontmatter, and registers it:

     ```
     python3 utilities/capability-route.py arbitrate \
       --route <route-file> --group <group_id> --evidence <merge record>
     ```

     The transaction is fail-closed and each refusal is typed: `auxiliary-group-unknown`, `auxiliary-group-has-no-auxiliary-leg`, `auxiliary-arbiter-is-node` (a node arbiter owns it instead), `auxiliary-arbitration-before-join` (some realized leg still has no completion marker), and a length/absence refusal on the array itself. It writes one write-once `<group>.arbitration.json`; an identical re-registration is idempotent and a different one conflicts. Until it exists, any node that depends on a member of that group is refused at the wrapper start-gate with `auxiliary-arbitration-missing` and the route's terminal-gate observation carries a failed `parallel_group:<group_id>` row, so `terminal_gate_proven` stays false. A group whose arbiter cannot be resolved at all is a different event and both surfaces name it `auxiliary-arbiter-unresolved`: it is a route-integrity failure that `arbitrate` cannot clear, and it refuses only the completions of the nodes that would have arbitrated it, never every unrelated node's. Closing an unarbitrated route is still allowed — it closes honestly as unproven, not as complete.
   - **Sub-session scheduling and mutation:** serial sub-sessions form one declared chain and should be registered/joined as one batch so runtime completion resumes the owner once, after the whole chain. A chain runner starts each exact registered attempt only after its predecessor is terminal and quiescent. Parallel sub-sessions use the existing sealed parallel-group transaction and require provably disjoint fixed-file ownership. Mutating overlap is serial even when analysis or verification can run in parallel. During a declared sub-session chain, first-parent descendant `HEAD` movement is accepted under the same lineage proof as an in-place mutation retry; it neither recompiles the route nor spends retry budget. A native runtime subagent may assist inside one sub-session only within that sub-session's fixed files and stage scope, with serial mutation, summary-only return, and no gate authority; unsupported adapters use the checked registered-headless or inline fallback without claiming native parity.
   - **The parallel-subdivision surface an owner actually calls.** A declared subdivision is passed to the group transaction as a manifest, and both its failure mode and its gate are typed:

     ```
     python3 utilities/dispatch-batch.py --parallel-group <node> --route <route-file> \
       --parent <owner slug> --slug-prefix <prefix> --subdivision-manifest <chain.json> --start
     python3 utilities/capability-route.py complete --route <route-file> --node <node> \
       --evidence <stage evidence> --jobs <registry> --subsession-manifest <chain.json>
     ```

     Each manifest session names the leg it runs as, with `node` (the realized leg's route node id) or `leg_index`; a session that names neither is refused rather than matched by list position. A subdivision that cannot be proven disjoint and in-scope does not raise — the batch prints a typed receipt `{"state": "single-session-required", "reason": "subdivision-disjointness-unproven"}`, exits 0, leaves one SD-93 ledger row, and the owner then runs the node as one ordinary session. That state is the owner's signal to stop treating the stage as split; nothing else consumes it.

     Admission records a worktree baseline keyed by the manifest hash, and the stage gate measures against it. This is what makes the post-hoc diff-scope audit a statement about the slices rather than about the whole worktree, and it is why the same manifest can be re-admitted and re-completed idempotently. The gate refuses with `subdivision-baseline-missing` when no admission baseline exists, `subdivision-commit-attempted` when `HEAD` left the baseline commit's first-parent line or a lineage-clean commit carries a slice's `fixed_files` (parallel slices are no-commit workers), and `subdivision-scope-violation` when a change outside the declared union appeared after admission, whether it is still uncommitted or already in a commit. Each refusal writes an SD-93 ledger row and no marker. **The order is fixed: the owner closes the stage gate first and commits after.** Committing the slices' work before the gate is refused, because at the gate that commit is indistinguishable from a slice having committed; committing after it is ordinary, and replaying the same gate on the same manifest and evidence then resumes the published marker instead of re-auditing a worktree that has legitimately moved on. **Declared limit of that judgement:** the gate tells an owner commit from a slice commit by what the commit carries, so a pre-gate commit that carries no slice's `fixed_files` and leaves every file's content identical to the admission baseline is judged neither — not a slice commit, because it carries none of the declared union, and not a scope violation, because its content still matches the baseline — and it passes. In that one shape SD-103's no-commit rule is stated but not enforced. It is the accepted cost of not judging by HEAD movement alone, which refused the owner's own commit against a write-once baseline and an unrewindable HEAD, with no recovery path. After a failed slice, the owner derives the gap-retry chain from the failed slices alone (`stage_session_contract.derive_gap_retry_manifest`); it carries exactly those slices' `fixed_files` and their leg binding, and never re-opens a successful sibling's.
   - **Phase brief, state ledger, and scope stop:** each sub-session reads a compact phase brief plus the previous bounded handoff instead of reloading the full specification by default. It persists `_internal/state/<attempt_id>.md` with the current slice, completed items, exact next command, invariants, and forbidden files. The ledger is flushed at least every three material edits and after every verification round trip. Pre-compact must validate and flush it; post-compact must re-read it before another edit. A missing or stale required ledger fails closed. The fixed file list is an execution fence: discovering a necessary file outside it stops the session with a handoff to the owner, which may create another sub-session. Wide mechanical edits use a codemod plus bounded diff verification instead of expanding an individual session ad hoc.
   - **Separability under SD-17:** dispatch is mandatory when the stage output contract is complete and its edit surface is not boundary-coupled through shared semantic anchors or sequential boundary assertions. A non-separable stage may run inline only if the reason is recorded in `plans/<slug>/_internal/metrics.md`; missing evidence is a contract violation. Parallelize separable census or independent file groups in-session. Self-modification of dispatch infrastructure additionally requires the orchestrator opt-out `STAGE_DISPATCH_INLINE_OK`.
   - Dispatch-depth-2 review helpers are read-only by default. The code route opens framing at width two for `standard`, widens framing to three and opens width-two plan/implementation-review groups at `strong+`, and adds declared third implementation-risk/failure-mode legs at `thorough+`. Other capability groups stay at their registry-declared width. Every leg is a sibling under the same owner, has a sealed role/profile/perspective and disjoint write scope, and joins before continuation. No worker fan-out, undeclared breadth, or dispatch depth 3 is permitted. Stage-worker ownership remains disjoint: `code-plan` owns plan artifacts; `code-execute` alone mutates source; `code-test` owns test evidence while source stays read-only; `code-report` owns the final report and locked summary.
   - The default concurrency cap is five. Count `Σ(active conductors + active stage workers per conductor) ≤ 5`; each stage pipeline is sequential, and in-session implementation-team workers do not count. Queue dispatches that would exceed the cap.
   - Every dispatch prompt exposes capability, `capability_mode`, assigned
     contract/route node, portable unit, QA, intensity, dispatch depth, parent
     slug, parent session ID, worker type, model role, and owner so the adapter
     UI and registry can identify it. `capability_mode` is validated against the
     entry capability catalog and sealed route. An adapter `worker_mode` is only
     a non-owner compatibility projection of an exact non-reserved unit; it is
     absent for the canonical owner tuple
     `worker_type=owner,unit=_kernel/owner,assigned_contract=<capability>`.
     Owner+stage-persona, capability-mode/route, and worker-mode/unit
     contradictions fail before prompt or registry materialization. New writers
     emit separate `capability_mode=` and optional `worker_mode=` metadata and
     never emit overloaded `mode=`. A legacy `--mode`/`mode=` may be read by
     deterministic scalar-versus-slash shape only and never overrides canonical
     fields. `worker_role` remains legacy read-only identity metadata.
   - For a route-bound job, the compiler selects and seals both portable model role and model profile; wrappers resolve the profile through adapter config and reject trailing model/effort replacement. Non-route direct/lifecycle surfaces retain their explicit model/inheritance contracts. Registered substantive owner/stage/review nodes reject `mini`. The orchestrator chooses harness placement subject to sealed diversity axes.
   - **Cross-harness routing under SD-16:** before dispatch, query each harness through `utilities/usage-check.sh`, which reports `ok`, `limited(reset)`, or `unknown`. The cascade is explicit target, hard eligibility, sealed affinity/policy, the balanced usage gate, quality band, then allocation ordering; optional `depth_affinity`, `depth_affinity_weight`, and `usage_headroom_exponent` default to `{}`, `0.5`, and `1`. In the shipped `balanced` strategy, candidates in the same band blend the last bounded window of exact registered attempts with fresh remaining headroom through one continuous deficit key: headroom defines each peer's target share and the recent count records how much of that share it already consumed. Equal headroom therefore reduces to exact recent-attempt round-robin, while a widening headroom gap moves the rank continuously instead of adding another threshold. The configured 90% usage boundary (`allocation.usage_gate_used_percent`, configurable 0..100) partitions candidates across quality bands, not within one: while any ungated candidate (including one with an unknown gauge) is hard-eligible, no gated candidate is selected in any band, regardless of band precedence; it does not replace explicit target or typed hard eligibility, which still win the cascade. Within a gate class the quality band, relief promotion, `last_resort`, sealed affinity, and the continuous deficit key are unchanged. If every candidate is gated, maximum fresh headroom is compared across all bands before quality-band or affinity ordering; existing ordering breaks only equal-headroom ties. Unknown gauges pass the balanced gate optimistically and use the neutral ordering share, while exact death markers from `usage-check.sh` remain hard exclusions. The legacy `capacity-aware` strategy remains valid: it excludes unknown/zero headroom and orders by fresh headroom, then count and declared order; its relief promotion semantics are unchanged. Quality bands, sealed affinity/policy/fallback_hops, and D-41 caps are invariant, and balanced acts only at compile-time allocation ordering. `HARNESS_CAPACITY_BIAS` may reorder a band but never crosses its quality boundary. A user prohibition is stronger than both signals. Schema-v3 shipped defaults use Claude/Codex/OpenCode as light/mini rotation peers, while deep and balanced-deep keep OpenCode last-resort. `${XDG_CONFIG_HOME:-~/.config}/hearting/dispatch-defaults.yaml` remains user-owned, and installation creates it once without overwriting it. Once a route is compiled its depth-2 nodes carry sealed `harness_affinity` and `harness_policy` snapshots and the record top-level carries `dispatch_defaults_digest` plus `dispatch_allocation`; `verify` checks the hash seal and never reloads live config. This is kept separate from `registry_digest`. Two observability rules keep a configured policy from silently not applying (2026-08-29): `dispatch-defaults.py validate` prints non-fatal `warning=` lines when the user-owned file runs a strategy other than the shipped default or carries an optional key its strategy never reads (`utilities/dispatch_allocation.py inert_allocation_keys` is the one table of which keys each strategy honors; under `capacity-aware`, `usage_gate_used_percent` and `usage_headroom_exponent` are inert and `depth_affinity_weight` is only a headroom-margin tie-break), and install, `harness verify`, and `harness config status` surface those lines as the `drift` state. Every realized depth-2 allocation — single-stage fallback and each parallel-batch leg — also appends one row to `<dispatch state root>/allocation/<route_id>.jsonl` (`utilities/dispatch_allocation_receipt.py list|summary --since 7d`) carrying the sealed strategy, preferred harness, rank, headroom, counts, inert keys, and the chosen child keyed by `attempt_id`; the stdout receipt names the row as `allocation_receipt=`/`allocation_ledger=`. A policy change is complete only when this ledger shows the new field on a real attempt, not when the file validates.
   - **Nested child-spawn eligibility under SD-48:** root headless readiness,
     runtime-native subagent readiness, and a conductor's ability to launch a
     registered dispatch-depth-2 child are separate runtime surfaces. Before
     compiling a route with dispatch-depth-2 nodes, bind checked evidence for
     `(parent_harness,parent_transport,parent_sandbox,child_harness,launch_authority)`.
     Only `supported` is eligible; `unknown` and `unsupported` fail closed.
     Dispatch contract v3 requires `launch_authority=conductor`; logical
     ownership remains `dispatch_depth=2,parent=<conductor>`.
   - **Checked fallback chain under SD-50:** a standard+ stage ranks checked direct-headless candidates through its sealed quality bands, then falls through to `native-subagent -> inline`. A candidate still records whether it is `same-harness-headless` or `cross-harness-headless`; those labels describe the selected tuple and fallback trace, not a durable preference that can override explicit choice, hard eligibility, sealed affinity, or schema-v3 quality/capacity allocation. Every headless candidate carries the same route id/node, write scope, completion gate, logical parent, stable attempt identity, and checked tuple evidence. The conductor invokes eligible adapter wrappers in that sealed/ranked order and proceeds only after a recorded launch failure. Native and inline degradation record skipped candidates, failure classes, registry attempt ids, allocation/headroom evidence, and assurance compensation without claiming Fleet parity.
   - **Direct headless launch under SD-61~63:** dispatch contract v3 has no resident launch broker, request spool, broker heartbeat, broker lease, or broker fencing identity. A standard+ dispatch-depth-1 conductor invokes the checked adapter wrapper directly for every same- or cross-harness dispatch-depth-2 attempt. The selected checked tuple's parent harness, transport, and sandbox must equal the actual launching wrapper identity exported in `AGENT_DISPATCH_CURRENT_*`; a missing partial identity or mismatch fails before child wrapper invocation. The canonical registry first records the stable attempt as `launch_claimed=0`. The checked wrapper then spawns a parent-death-safe pre-exec fence while holding the registry lock and atomically publishes the complete PID/start/namespace/leader-PGID identity together with the only `launch_claimed=1` transition. The fence records `launch_started=1` under the same exact row immediately before payload `exec`; a launcher lost before spawn leaves a retryable registered row, and a dead fence that never recorded start may be reset only after exact process-group quiescence. A duplicate or already-started claim never creates another child. A Codex dispatch-depth-1 capability owner running with `workspace-write` receives `sandbox_workspace_write.network_access=true`, `AGENT_NESTED_HEADLESS_NETWORK=1`, and a worktree-local writable `CODEX_HOME`; that home links existing auth/config read-only and keeps mutable nested session state inside the owner sandbox. Dispatch-depth-2 workers do not inherit the network widening. Contract-v1/v2 route and broker state are read-only migration inputs. The broker utility may expose diagnostic `status` and idempotent `stop` for one compatibility release, but production dispatch never calls `ensure`, `request`, or `serve`.
   - **Checked evidence probe — whose parent and worktree are being probed:** create the final isolated source worktree first, then run every standard+ eligibility probe from and against that exact canonical path. `utilities/nested-dispatch-eligibility.py` emits one tuple per child harness, and every tuple seals `checked_worktree`; `capability-route.py compile` rejects a tuple whose path differs from the route `cwd`. Evidence from the primary checkout, a staging directory, or a worktree that will later be replaced is never reusable for the final route. Every `--parent-*` value describes **the process that will launch the dispatch-depth-2 node — the dispatch-depth-1 registered-headless capability owner — not the session running the probe.** A dispatch-depth-0 caller sealing its own runtime is the recurring failure mode, so all three fields resolve or fail closed rather than defaulting to the caller: `--parent-transport` defaults to `auto` and is always `headless` for a dispatch-depth-2 tuple (an explicit `interactive` is canonical vocabulary for the caller and a contradiction here); `--parent-sandbox` defaults to `auto` and resolves the parent harness wrapper's canonical `AGENT_DISPATCH_CURRENT_SANDBOX` export; `--parent-harness` stays required because the owner's adapter is a later `dispatch-owner` decision, and its `auto` resolves only inside a wrapper that already exports its identity. A probe running inside an active Codex owner still requires its checked `AGENT_NESTED_HEADLESS_NETWORK=1` marker. A dispatch-depth-0 caller checking a Codex owner that has not started uses the explicit `--prospective-standard-owner --jobs <canonical-jobs.log>` mode instead; that mode proves the exact registry and lock are writable, evaluates the same standard+ dispatch-depth-1 owner predicate used by the Codex launcher, labels the evidence as prospective, and never fabricates the runtime marker. A missing marker without that mode means only that the active-owner runtime is unconfirmed, not that an explicit Codex dispatch-depth-1 selection is unsupported. `utilities/dispatch-readiness.py` is the required depth-0 pre-owner surface: it applies that prospective mode automatically with the exact registry and atomically emits the complete checked evidence object, so callers never assemble tuples manually. Calling the raw active-owner surface without an active marker is typed `prospective-owner-check-required`, not a runtime-global network failure. The same parent fields, exact worktree, canonical registry, and final owner sandbox projection are cross-checked before launch, so a wrong subject or unwritable registry fails before an owner is paid for. Because the owner's adapter and the sealed `parent_harness` must agree, pass the compiled route to `dispatch-owner --route-evidence <route.json>`; that selector-only option constrains the adapter cascade to the harnesses the checked tuples actually probed and never crosses the wrapper boundary. Sealing dispatch-depth-0 values instead blocks hop 1 and 2 for every node and every adapter with `dispatch-evidence-parent-runtime-mismatch` or `parent-attempt-not-found`; it never authorizes inline execution (2026-07-31 sandbox field, 2026-08-04 transport field).
   - **Failure scope is a fallback axis:** a checked tuple carries `failure_scope`, `codex_command`, and `retry_on_isolated_worktree`. `failure_scope=runtime-global` may remove that runtime from the normal checked fallback chain. `failure_scope=exact-worktree` with `retry_on_isolated_worktree=1` instead means the runtime command exists but the probed filesystem shape is unusable; route compilation stops with a re-isolation/re-probe requirement and must not select another harness, native helper, inline execution, or `danger-full-access` as if the runtime were globally unavailable. The canonical Codex example is a user-owned `.codex` non-directory in one checkout: preserve it, create the final clean isolated worktree, and re-run the probe there.
   - **Namespace-safe launch lifecycle under SD-72:** `dispatch-chain` selects the child lifecycle from the actual launcher scope for both same- and cross-harness candidates. Because an adapter wrapper may enter a narrower transient namespace after that selection, the incoming lifecycle is provisional: every wrapper re-evaluates its own scope before registry reservation or attempt creation and atomically promotes `detached` to `foreground-scoped` when the actual scope is transient. That pre-registration promotion is normal selection, consumes no attempt or retry budget, and is recorded with both selector observations; `dead-nested-sandbox-lifetime` remains only a legacy/recovery classification for a caller that bypasses the checked wrappers. In a transient PID namespace, the wrapper keeps its call alive until the child exits, forwards INT/TERM/HUP only after two adjacent exact PID/start/group-leader checks, and retains parent-death coupling for the fenced child. Outside a transient namespace the lifecycle remains `detached`; the existing spawn-then-watch/poll behavior is unchanged. `AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN=1` selects `detached` only when the launcher's own observed scope is host-like and its sealed parent sandbox is not a checked `codex`/`headless`/`workspace-write` sandbox; otherwise both selection and wrapper reselection promote to `foreground-scoped` and record `launch_lifecycle_override=rejected` with reselection `override-rejected-transient-scope`. Separately, a `registered_worker=1`, `pid_scope=namespace-local` row whose recorded observer namespace is provably absent from the host, with no terminal envelope, no completion marker, and no attempt-tagged descendant, is sealed as a typed cancelled terminal that releases the owner for one SD-106 same-node retry and never satisfies a general SD-79 successor gate. Native subagents do not substitute for either lifecycle. Timeout or signal termination closes only the exact attempt row with its typed cause; a zero process exit is only an observation and is successful solely when an exact completion marker or typed terminal handoff proves it. An exact Codex `turn.completed` or Claude stream-json `result` handoff with `BLOCKED`/`FAIL` closes that attempt before fallback; an anchored bwrap mount failure is `dead-sandbox-init`. A dispatch-depth-1 wrapper exports its exact self slug; `dispatch-chain` defaults the logical parent to that value and rejects an explicit mismatch before registration. Before a dispatch-depth-2 claim, the wrapper resolves one open exact parent attempt in the same repo/worktree and seals `parent_attempt_id`. A namespace-visible exact PID/start is the primary live-parent proof. A Codex `app-server-supervised` owner additionally holds one exact-attempt `flock-v1` liveness lease at the canonical registry-relative path `.dispatch/supervisor-state/<attempt_id>.lease` for its full active lifetime. Only when the parent's process classifier is `unverifiable` because the current tool observer cannot establish authoritative PID-namespace identity may that currently held lock satisfy the live-parent gate. The row must be open, identify a registered headless dispatch-depth-1 Codex owner with `completion_delivery=app-server-supervised`, declare the exact lease kind, canonical attempt-derived path, and a per-attempt nonce matched by the locked file payload, and retain the same repo/worktree/runtime identity. Missing, malformed, foreign, nonce-mismatched, symlinked, or unlocked lease evidence fails closed; a stale file with a free lock is not liveness, and a held lock never overrides terminal status, exact quiescence, PID reuse, or another positive death signal. This lease is neither a broker/request lease nor launch authority, fencing authority, completion evidence, or signal authority. Immediately before launch, again before fence release, and throughout a foreground-scoped child wait, the wrapper revalidates the same exact parent through PID evidence or that narrow lease fallback; parent loss closes the unreleased fence or tears down the direct child. Launch releases only after proving aligned procfs/PID namespace evidence, a non-zombie start identity, and `pgid == pid`; incomplete identity closes the unreleased fence without executing payload. Process-group observation is three-state (`populated`, `empty`, `unverifiable`), so procfs denial or malformed/incomplete scans never become quiescence, reap proof, or signal authority. A foreground post-wait receipt is bound to the exact PID/start/observer-namespace/leader-PGID tuple and remains consumable from another namespace, while a currently observable live exact PID still overrides it. A spawned child records both its namespace-visible PID and, when `/proc` exposes it, its outer-namespace PID/start identity. Fleet must still surface a legacy, malformed, or unverifiable unmatched dispatch-depth-2 row as an orphan instead of dropping it; legacy or invalid rows never receive cascade signal authority. For a foreground Codex child already contained by a checked Codex `headless/workspace-write` parent, the inner Codex sandbox is disabled (`danger-full-access`) to avoid unsupported nested mount setup; the outer sandbox remains the security boundary, so this changes no filesystem or network authority and the effective runtime sandbox is recorded. The dispatch tuple uses the canonical transport word `headless`; adapter runtime-surface labels such as `codex-exec-headless` are not tuple values. If an inner sandbox remains enabled, a worktree `.codex` mount destination must be a directory. The checked nested-eligibility probe reports that shape as `unsupported` so the tuple never claims readiness the runtime cannot deliver (SD-48), and the wrapper independently fails before registration. A standard+ Codex owner grants that outer sandbox write access only to the existing harness `.core-grounding` and Claude `session-env` scratch directories, the primary `$AGENT_HOME/.spec-grounding` directory (created safely if absent — a spec-backed owner must be able to record its own PRD-read marker, not only its SD-69 mutation workers), the canonical dispatch state root (the parent directory of the resolved `AGENT_DISPATCH_JOBS`, never `$AGENT_HOME/.dispatch` directly), and the dispatch summary-owner state root (`$XDG_STATE_HOME/agent-fleet/titles/.dispatch-owners`, created safely if absent — without it every dispatch-depth-2 launch from inside the owner sandbox dies at the pre-release fence as `summary-owner-launch-failed`/`never-launched`; observed 2026-08-06 eiren-m3a), plus — for a commit-expected linked-worktree run under SD-69 — the exact primary Git metadata directories (the per-worktree git dir and the common dir's `objects`/`refs`/`logs`, never `.git` itself), in addition to its established scoped roots, preserving adapter write gates and cross-harness Claude Bash initialization without widening either runtime home. This grant set is not owner-exclusive: an ordinary registered `dispatch_depth==2` Codex worker (route-bound, launched without `nested_headless_network`) receives the same `.core-grounding` and canonical-dispatch-state-root writable-root entries independent of the owner-only network-widening gate, because that worker also runs the same portable-guard hooks and must be able to record its own core/spec-read markers; only the network-widening grant itself (`AGENT_NESTED_HEADLESS_NETWORK`) stays owner-only. The grant root and the root the launched child actually writes to are computed from the same sealed `AGENT_HOME` value the parent resolved and passed into the child's environment — no wrapper recomputes agent home from its own physical install location after launch.
   - **Successor readiness and parallel launch under SD-79/80/89:** a completion marker and its exact terminal row are semantic stage evidence, not proof that the governed process has released its lease. A registered predecessor is successor-ready only after its marker is current, its exact row is terminal, no conflicting active retry exists, no live or unverifiable non-terminal sibling attempt of the same route and node remains, and the recorded outer governor process is quiescent. A live sibling blocks readiness as `prior-attempt-still-live`; an unverifiable one blocks it as `prior-attempt-unverifiable`. Live exact identity, or a live process carrying that attempt's identity that has escaped the recorded leader's process group, is `draining` and always overrides a stored receipt. PID reuse, zombie, verified disappearance, or an explicit atomic `never-launched` outcome may prove quiescence directly. Once predecessor readiness has already bound a current completion marker to its exact terminal row, or the retry gate has selected an exact terminal sibling row, a complete wrapper-issued post-exit receipt is namespace-portable: `foreground-scoped` requires `governed-process-reaped`, while `detached` requires `governed-process-group-drained`; both bind the recorded PID/start/observer-namespace/leader-PGID tuple and require `pgid-empty-v1` with the same PGID, and the detached form additionally requires an `attempt-tagged-empty-v1` scan made in that recorded observer namespace. The stored receipt may therefore prove quiescence after that observer namespace has disappeared; it does not make a later foreign empty scan authoritative, and a partial receipt, a receipt outside those exact terminal gates, an accessible live tagged process, or an incomplete local scan still fails closed. A namespace-local, non-authoritative attempt whose attempt-tagged process set is provably empty in the observer's PID namespace may close as `dead-namespace-absent` independent of heartbeat freshness, per SD-58 speech-is-not-liveness. Completion gate, runtime join, polling wait, and fallback/progress watchers use this shared classification and never replace it with a fixed sleep, a delayed marker, or a larger cap. A supervised owner, native-Stop session batch, or explicitly reported polling fallback keeps its own exact terminal non-quiescent children parked until the completion-delivery boundary resolves them. The ordinary unstamped interactive pre-tool park is narrower and is not a readiness oracle: it parks only exact latest `open|running` child rows, while terminal live/unverifiable rows remain visible and continue to fail successor, join, wait, fallback, and cleanup gates without freezing unrelated local tools. The sequential plan/plan-check/execute DAG stays sequential around every parallel join. An immutable `parallel_group` contains exactly 2–4 route-declared siblings and starts only through one `dispatch-batch --parallel-group` transaction. The batch verifies route, parent generation, dependencies, width, leg indexes, disjoint scopes, sealed model profiles/perspectives, and checked harness evidence; records required and realized independence axes separately; seals all stable attempts into one schema-v2 manifest; reserves every absent first-start leg atomically; and launches their wrappers concurrently. An explicit `--log-dir` is admitted only inside the registry-owned dispatch state root and fails before row or process creation as `log-dir-outside-dispatch-state-root`; omit it for the canonical `logs/` default, and copy evidence to cycle artifacts through a separate collector. Schema-v2 manifests admit Claude, Codex, and OpenCode legs under the same reservation, launch, join, and receipt rules; no adapter-specific manifest allowlist may narrow that portable set. `cross-harness` requires at least two harness families, not one distinct harness per leg. A caller may explicitly accept typed same-harness degradation, but model-profile/perspective realization remains recorded. Every opaque reservation binds the exact manifest, route/node/parent/attempt/harness/hop/ordinal/profile/perspective/leg index. Batch minting requires a one-shot capability bound to the exact `dispatch-batch.py` parent PID/start and interpreter/script slot. A single missing-leg recovery proves every other N-1 manifest member active or completed and seals the sorted peer-set digest; missing, duplicate, foreign, terminal-failed, or incomplete peers reserve zero slots. Full-N capacity shortage likewise creates zero rows and zero model processes. Individual group-member `register`/`start` through `dispatch-node`, `dispatch-chain`, a wrapper, or fallback fails before row/process creation. Idempotent repeats classify exact active/completed rows without consuming capacity. In a transient PID namespace all newly started foreground-scoped wrappers remain alive in the same checked batch call; elsewhere detached lifecycle remains available. The batch emits bounded per-leg receipts and creates no model turn, daemon, broker, worker fan-out, or extra dispatch depth. Schema-v1 exact two-way manifests and `replica_group`/`--replica-group` remain read/CLI aliases for one migration window; new routes and receipts are canonical `parallel_group`. OpenCode is eligible for registered standard+ dispatch-depth-2 dispatch: it implements exact parent binding, foreground lifecycle, and supervisor snapshot parity; its quick/relief surfaces remain a separate authorization path and do not substitute for this parity.
   - **Immediate limit-death handling under SD-15:** wrappers watch briefly after launch. If a child exits immediately on session, usage, or authentication limits, mark its row `done` with `note=dead-<reason>` and, when available, `reset=<time>`. Liveness also recognizes anchored short CLI error lines at the end of logs, but a fresh completion or activity transcript wins over a report that merely discusses limits. Wrappers do not retry; the orchestrator chooses redispatch or failover.
   - **Canonical global attempt registry under SD-49 (amended by SD-112 §13.33.2-(8)):** dispatch depth 0 resolves the canonical dispatch state root once. This root is determined by the active runtime's install shape and is **not inside the active harness release tree**: an installed checkout resolves a stable per-user state root, a Codex bundle resolves activation-owned mutable root, and only an explicit isolated development checkout uses a checkout-relative path (or an explicit root fixture path). The resolved registry path is passed immutably as `AGENT_DISPATCH_JOBS` to every descendant, and that file's parent directory is the canonical dispatch state root — no reader reconstructs it as `$AGENT_HOME/.dispatch`. **This amendment supersedes the 2026-08 "shared release keeps chain-3" decision**, made before managed-release pruning was observed deleting live dispatch state (2026-08-27); it resolves the prior contradiction between this paragraph and §5.9a's "never reconstruct any dispatch state path as `$AGENT_HOME/.dispatch/...`" in §5.9a's favor. Invoking an adapter wrapper from a linked worktree does not make that worktree the agent home: a valid explicit `AGENT_HOME` wins, otherwise the adapter resolves the installed canonical harness; its source checkout is only a standalone fallback. For a nested launch, `--jobs` may only repeat that inherited absolute path; a cycle-local override is non-authoritative and fails closed. Every actual start first writes one global registered-only row, then transitions its exact claim only with the complete fenced process publication described above. General stable identities bind route/node, logical parent, target harness, and fallback ordinal; replica batches bind the exact parent generation and deliberately exclude display slug/prefix. A duplicate or already-started attempt starts zero children. A global open/lock failure returns `global-registry-unwritable` with zero children and no local-only row. Optional cycle-local files are audit mirrors, never authority. Existing current-contract local-only rows are reconciled by exact `attempt_id` idempotently while preserving timestamp, status, and failure note; legacy or invalid rows remain read-only diagnostics and are never reconciled, mutated, or signalled. The six tab-separated row fields remain `<ISO-time>`, status, repo, worktree, slug, and pipe; status words remain only `open`, `running`, and `done`. Each registered row also seals `launch_home=<resolved launch agent home>` in the pipe so readers (for example Fleet) locate that launch's default `.dispatch/logs` stream directory from the row itself instead of inferring install layout; the key is optional on legacy rows, and readers fall back to their existing root heuristics when it is absent. That canonical registry file's parent directory is the canonical dispatch state root: completion markers, logs, heartbeats, watchdog files, supervisor-state, homes, broker, degradation, workflow, and index/journal state all live under it, derived by one function and never reconstructed as `$AGENT_HOME/.dispatch`. `launch_home` keeps its existing narrower meaning as a legacy row anchor and legacy log-root heuristic only; it is not the dispatch state root and a new row's state root is always derived from the registry path, not stored as a separate field.
   - **SD-67 mutation-node in-place retry and declared sub-session lineage:** after an execute failure or partial completion, a mutation node may be redispatched on the same immutable route. A moved `HEAD` is accepted only when that node is declared in `resume_retry_boundaries`, the bound canonical global registry has a different prior attempt for the same route and node, and `HEAD` is a first-parent descendant of the route's original `source_commit`. A declared planned sub-session under that node supplies the same prior-attempt lineage proof and is accepted without route recompilation; its `stage_authority=0` keeps this separate from gate retry accounting. Missing or unreadable evidence and divergent history retain exact-match rejection. Do not recompile or re-pin the route, and never use `git reset --hard` to restore it. This grants neither an automatic gate retry nor extra retry budget, and does not change SD-65 downstream-node lineage handling.
   - **Fleet notice:** after starting background work, tell the user once that Fleet is the live cross-harness dashboard for stage and liveness status. Its quality depends on complete argv and registry metadata; do not repeat the notice when the user already has Fleet open.
   - **Stealth-death guard:** never wait indefinitely on completion notifications. Use the adapter liveness wrapper: Codex `adapters/codex/bin/preflight.sh liveness [jobs.log]`, OpenCode `adapters/opencode/bin/preflight.sh liveness [jobs.log]`, or Claude/shared `utilities/dispatch-liveness.sh [jobs.log]`. They report `ALIVE`, `SUSPECT`, `DEAD`, or `EXITED`; exit 3 means at least one suspicious or unharvested job. An exact-attempt wait or harvest surface owns normal diagnosis; a parked parent never tails a child transcript/log or searches source/artifacts for progress. Raw inspection is permitted only after that child row is terminal/closed or after an explicit operator recovery override. Exact recorded `pid` plus `/proc/<pid>/cmdline` is the strongest signal. An attempt carrying canonical identity never falls through to cwd-wide transcript activity when exact evidence is stale or terminal. Transcript or DB mtime is a fallback only for legacy identity-less rows because workers sharing a worktree can make each other's directories look fresh; path-based `pgrep` is rejected as false-positive prone. For interactive Codex sessions, a validated exact `task_started`/`task_complete`/`turn_aborted` lifecycle outranks rollout mtime; terminal lifecycle makes the live TUI idle immediately and mtime is consulted only when lifecycle is unavailable or ambiguous.
   - **Runtime-owned completion delivery under SD-14/78:** a registered `standard+` headless owner is launched under an adapter supervisor, not as an unresumable one-shot model turn. The model registers every separable child in the current batch and yields `runtime_wait: registered-children`; the supervisor snapshots only current v2 rows sealed to `parent_attempt_id=$AGENT_DISPATCH_ATTEMPT_ID`, joins every parallel attempt through canonical liveness outside the model/tool loop, and sends the same session exactly one bounded typed receipt when the whole batch is semantically terminal **and execution-quiescent**, or requires typed attention. Child output, transcript text, artifact bodies, source, git state, and liveness prose never enter that receipt. Codex realizes the bridge with one ephemeral App Server thread and repeated `turn/start` after `turn/completed`; a registered Claude owner realizes its internal batch bridge with one `--session-id` followed by `--resume`. An interactive Claude parent uses a separate `PostToolUse(Bash)` `asyncRewake` bridge: only a successful exact `dispatch-owner --start` bound to the same Claude session may arm it, proved either by that start's stdout receipt or — when the caller filtered that stdout away — by the one lock-written registry row carrying the same session, `worker_type=owner`, dispatch depth 1, `parent_completion_delivery=claude-parent-runtime`, and claimed/started evidence, still open inside a bounded recent window; zero or several candidate rows arm nothing, because absence beats misattribution. It watches that one owner attempt to terminal quiescence outside the model, and exits once with a bounded exact-attempt receipt. It never launches a visible background `dispatch-wait`, Monitor, progress recap, or periodic re-arm; explicit `poll-fallback` remains the only model-owned wait. Intermediate turn/result events are withheld from the terminal handoff, and only the final exact three-line envelope is exposed as terminal. Before every model turn the supervisor atomically publishes an attempt-scoped schema-v2 phase state: `parked`, `deliverable`, `running-turn`, `recovery`, or `terminal`. While an undelivered child is open or terminal-but-draining, the native pre-tool policy admits only one exact same-parent `dispatch-batch --action start` for a declared parallel group (or a non-group exact `dispatch-node --action start`), so a first child cannot prevent its checked siblings from registering. Once any delivered child remains open or draining, the policy admits only exact typed harvest for that delivered batch. Both phases reject model waits, raw inspection, liveness, unrelated tools, and shell composition; missing/invalid phase state is recovery-only exact harvest. Codex enforces this through its projected hook and Claude through a command-scoped `--settings` PreToolUse bridge without mutating user-owned runtime settings. Multiple sequential route batches repeat this one-resume transaction. A bounded join timeout is an internal repark checkpoint: it emits no model receipt, does not update the delivered set or consume continuation budget, and makes the same supervisor rejoin the same sealed child set. A second, distinct internal repark checkpoint (SD-119) advances a serial sub-session chain registered under `utilities/stage-session-chain.py`: once the joined child is a chain participant and terminal, the supervisor claims and starts the chain's next index itself, folds the closed predecessor into the delivered set so it is never re-surfaced, and rejoins — again with no model turn and no continuation spend — until the chain either completes (falls through to the ordinary route-level flow) or the joined child carries no chain metadata at all. Only terminal-and-quiescent or a typed attention condition is actionable. An exception with owned open children preserves state and lease in `recovery`; only terminal-and-quiescent completion removes them. A dispatch-depth-0 interactive Codex parent has a separate native realization: a direct registered dispatch-depth-1 attempt bound to the actual `CODEX_THREAD_ID` seals `parent_completion_delivery=codex-stop-hook`; `launch_claimed=0` registration alone never parks the parent, and a start requires the **current exact Stop and PreToolUse hook definitions** to be trusted before it may claim or spawn the process. Immediately after successful spawn, the wrapper atomically binds the exact attempt into hashed-session pending state, then the parent ends its model turn. Stop follows that immutable set even if an orphan watcher has already changed a child row to `done`, joins it outside the model, publishes the delivered phase, and returns one bounded `decision=block` continuation only when exact harvest is ready. While undelivered, PreToolUse admits no model tool—including `dispatch-wait`; after delivery it admits only exact-attempt harvest with `--status all`, so an `open`→`done` watcher transition cannot invalidate the continuation. A valid harvest consumes its exact receipt, and the final receipt removes the session state. A bounded Stop timeout yields one minimal end-turn/re-enter instruction rather than a polling tool loop. Foreign, legacy, registered-only, untrusted, or unstamped rows never enter this path and retain the explicitly reported polling/recovery contract. Runtime support is probed before launch: forced supervised mode fails closed, interactive native Stop delivery fails before spawn when current-hash trust cannot be proved, while other unavailable same-session bridges may use the explicitly reported `poll-fallback` (`dispatch-wait --attempt-id <id> --max 300..600`). After a supervisor has started, protocol/session failure never replays the assignment through a one-shot fallback. Arbitrary detached shell output still does not auto-resume; only the checked completion-delivery surfaces above do. Parent ownership remains exact, foreign or stale rows never wake the owner, and post-exit orphan reconcile remains mandatory and independent.
   - **Codex launch-publication settle under SD-14/78:** an App Server turn may deliver the exact `runtime_wait: registered-children` sentinel in the narrow interval after atomic child registration but before every fenced wrapper has appended `launch_started=1`. The Codex owner supervisor therefore performs one short, bounded reread of only undelivered exact-parent rows before issuing `registration-required`. A batch that reaches the existing durable `launch_started=1` fence during that settle window parks and joins normally without consuming a continuation or replaying the dispatch; a row that remains registered-only still receives the existing bounded correction. The settle loop holds no registry lock, accepts no artifact, transcript, PID guess, or stale delivered row as launch proof, and never starts or retries a child itself.
   - **Managed interactive Codex boundary under SD-92 (supersedes SD-91 and the interactive clauses of SD-83 and the preceding SD-78 paragraph):** automatic completion delivery is a new-session boundary entered through `utilities/codex-managed-entry.py`; an existing TUI is never hot-upgraded. A user-authorized harness install may make that boundary transparent by installing a reversible launcher for interactive `codex`, `resume`, and `fork`, while preserving the resolved real Codex command and passing every non-interactive or administrative subcommand through unchanged. Plugin metadata or a lifecycle hook alone never claims launcher ownership because both load after process entry. One private owner-only gateway is the sole upstream App Server client for that thread; remote TUI client A owns subscriptions, transcript display, and every approval response, while completion sidecar client B may use only the gateway's private control socket and never connects upstream or acquires approval authority. The gateway serializes manual input and completion delivery under one atomic thread-state claim: completion starts one `turn/start` only when the thread is idle, or one `turn/steer` when a live turn accepts steering. A durable sealed-batch ledger treats `prepared` as retryable, an accepted receipt as replayable without another wake, and an upstream disconnect after send as `sent-ambiguous` with no automatic resend. `clientUserMessageId` is metadata only, not the deduplication primitive. A direct registered dispatch-depth-1 sidecar is launched after immutable registration but before the worker spawn claim, waits for that exact `launch_claimed=1`, then joins only the exact terminal and quiescent batch and submits one bounded typed receipt with no raw child output; absence of an exact launch fails closed. Parent runtime selects the wake adapter independently of child runtime: a Codex parent uses this managed gateway for Codex or Claude children, while a Claude parent keeps its Claude async-rewake/`--resume` supervisor for either child. Managed Codex completion uses neither a Stop continuation prompt nor an all-tool PreToolUse parent park, so the interactive parent remains available while children run. Managed launches also probe the exact effective `default_mode_request_user_input` feature row and, when supported, process-locally enable it in both the App Server and remote TUI children without writing user config; a per-launch disable wins across both processes, and unsupported probing warns once then launches without injection. The same gateway observes only typed `(threadId, requestId)` identity and time for `item/tool/requestUserInput`, publishes content-free `codex-appserver` evidence, and clears only its own evidence on an exact response, `serverRequest/resolved`, turn completion/interruption, or disconnect. It forwards every RPC unchanged and never renders, answers, approves, blocks, or owns user input; the TUI remains sole input and approval owner. Unmanaged clients remain unknown without a real producer, while rollout parsing remains legacy fallback only. An unmanaged interactive Codex parent cannot register or start a new detached dispatch-depth-1 owner through the portable owner selector: the selected child adapter must retain the actual caller runtime and fail before registry mutation or spawn with `managed-entry-required`. The low-level operator-only `--allow-unmanaged-parent-poll` escape hatch preserves a disclosed finite recovery path, is forbidden by `dispatch-owner`, and is never selected automatically by a model route. Sessions with an already-open legacy attempt or trusted `codex-stop-hook` state retain only finite migration/recovery behavior, and exact terminal `--status all --attempt-id` harvest may consume one legacy receipt. Open, stale, foreign, older-attempt, broad-selector, raw-output, and synthetic user/developer-message paths have no wake authority. A registered Codex headless owner continues to use its separate private App Server supervisor. Installer ownership must be manifest-backed, update-repairable, collision-safe, and exactly reversible on uninstall; private runtime state and the real CLI binding fail closed when validation is unavailable. Protocol ambiguity remains fail-closed and is reported as the upstream `continueIfIdle(threadId, idempotencyKey, typedContext)`/native async-rewake gap.
   - **Post-exit parent-bound reconcile under SD-64/71/77:** a conductor can die mid-pipeline (session end, crash, limit) leaving either a plain stale owner row or registered children/unstarted successor nodes orphaned. Since a dispatch-depth-1 owner is normally registered before route compilation, its route context is derived deterministically from exact child rows in the same repo/worktree, including terminal children; conflicting route tuples fail closed. The deterministic orphan classification is: exact conductor attempt death (`pid`+`pid_start` mismatch or gone) AND at least one route completion node without a marker AND (any open child row OR an un-started successor node whose predecessors are all marked). Each registered dispatch-depth-1 owner launch starts one non-model watcher bound to its exact PID/start-time and attempt; it exits when the row is already terminal, or after every exact owner exit invokes the general attempt reconciler. This closes a childless pre-route model/auth/limit failure as `dead-exact-pid` instead of leaving a Fleet-invisible `open` row, while a true orphan takes the bounded cascade below. This avoids a polling daemon and does not consume a model-worker slot. Reconcile first preserves any exact completion marker or typed terminal handoff, then classifies exact process identity before consulting worktree integration state. A missing configured upstream is typed `no-upstream-configured` and affects only push-sync/cleanup eligibility; it never blocks registry hygiene. A host-visible PID/start mismatch, or a namespace-local row's verified outer PID/start mismatch, is conclusive death evidence even when the recorded PID now names a different live process. For an open namespace-local child, Fleet and reconcile use this evidence order: an exact child terminal marker or receipt; authoritative positive child PID/start or a surviving attempt-tagged process; authoritative child PID death; proven parent extinction for an eligible foreground-scoped child; an authoritative empty attempt-tagged scan; an exact fresh heartbeat; then unknown/unverifiable. A fresh heartbeat therefore cannot override proven parent extinction, but a terminal parent word alone proves nothing. The parent exception requires one current registered depth-2 foreground child and one unique current terminal depth-1 owner bound by exact parent attempt, slug, repository, physical worktree, and conflict-free route context, plus either a durable owner exit receipt, authoritative owner quiescence, or watcher-observed exact owner PID/start extinction. Watcher evidence is usable only when its observer PID namespace equals both the current observer and the parent's recorded launch observer (legacy host-visible rows may omit the latter); inaccessible or malformed procfs is never extinction. A detached or ordinary namespace-local worker that lacks this complete proof remains visible and may still use its exact heartbeat. Reconcile then closes an orphan conductor `note=dead-parent-orphaned` and performs one bounded cascade over only open direct children sealed to that `parent_attempt_id`. An exact host-visible child process group is TERM→bounded-grace→KILL reaped only after PID/start and PGID-leader revalidation; an already-gone child row or a registered/claimed row with no atomically published PID is closed as `dead-parent-exited`. A foreground namespace-local child covered by the exact parent-extinction exception is reconciled as `dead-parent-terminated` without a signal; it does not grant signal authority over an unverifiable PID. PID reuse is never signalled: a start-time mismatch proves the recorded child has exited and permits only `dead-parent-exited` row closure. Missing identity on a live or unverifiable process, route conflict, non-group-leader targets, namespace-local rows without the exact parent exception, and legacy live rows are never signalled and remain visible for dispatch-depth-0 handling. The watcher never starts a replacement, retry, successor, or route advance; the resume boundary remains a dispatch-depth-0 decision.
   - **Codex linked-worktree mutation stages are no-commit workers under SD-69; owners are commit-expected:** the dispatch-depth-2 boundary is contractual, not a sandbox impossibility — parallel stages must not race `HEAD`, a stage must never claim a commit the runtime did not make, and the route's `source_commit` holds unmoved until stage end; a trusted dispatch-depth-0 or Claude boundary — normally the owning dispatch-depth-1 owner — commits after the stage's own PASS gate and confirms diff attribution before doing so. Such a stage worker's only writable roots beyond the task worktree and the canonical artifact root are the exact primary `$AGENT_HOME/.spec-grounding` directory (created safely if absent) — never all of agent home, and never `.git`. A dispatch-depth-1 owner in a linked worktree is commit-expected instead: Codex resolves and allows a linked worktree's real git dir on its own only under its default `~/.codex` home, and every dispatched run uses a custom masked `CODEX_HOME`, so without an explicit grant the owner's `git commit` dies on `index.lock` with EROFS (verified codex-cli 0.148.0, 2026-08-21; the earlier "protected even when other roots are writable / widening is never an accepted fix" reading is retired — an explicit writable root for the resolved git dir does take effect). The wrapper therefore grants a commit-expected linked-worktree run exactly the primary Git metadata directories a commit touches — the per-worktree git dir plus the common dir's `objects`, `refs`, and `logs` — and nothing else of `.git`, so `hooks/` and `config` stay read-only and a sandboxed worker cannot plant code a later unsandboxed session would execute.
   - **Completion marker bound to the exact attempt row under SD-70:** completing a node takes the canonical registry (`jobs.log`) path and the current exact attempt id, not just the route/node pair. It writes the completion marker and an immutable per-attempt linkage atomically first, then idempotently closes only that one attempt row `done note=completed-marker` with the marker as evidence — it never breadth-closes a prior `BLOCKED` row or a later live retry of the same node. A canonical latest-link sibling may be retained for compatibility, but a retry cannot overwrite the immutable linkage used to repair an earlier attempt. Marker write and row close are each idempotent under retry. If the row close fails after the marker is written, the marker is preserved and the command returns a structured nonzero rather than silently succeeding or discarding evidence; reconcile later repairs only that exact marker-backed stale row, never any other row for the route/node.
   - **Supervisor exact terminal reconcile:** every registered owner supervisor exit classifies its final runtime envelope and process result, then atomically reconciles only its exact attempt before reporting success or typed failure. Capacity, auth, protocol, missing-result, signal/exit, and valid handoff outcomes cannot leave the row open. If the supervisor cannot reach its own finalizer, the exact post-exit owner watcher runs the same classifier-backed closure; neither path breadth-closes a slug/worktree retry.
   - **Terminal authority and actionable receipt under SD-97:** a runtime supervisor's exact final `turn.completed` or Claude `result` handoff outranks a wrapper-side foreground tail observation for the same attempt. A stronger later observation may repair the row while preserving the prior note, source, and failure class as conflict evidence; equal-authority contradictory verdicts close as `dead-terminal-conflict` and never manufacture PASS from artifacts or tests. A terminal registry row with a complete foreground reap or detached group-drain receipt is execution-quiescent even when the observer namespace has exited; a stale summary/UI heartbeat cannot override that exact post-exit proof. Receipt schema v2 gives every joined child exactly one `required_action`: `complete-open`, `inspect-done-failure`, or `advance-completed`. The registered-owner supervisors, managed Codex gateway, Claude async-rewake bridge, pre-tool guard, and harvest selector consume that same action and status, so a terminal row cannot become an unharvestable `matched=0` receipt. Only an actionable model resume consumes the supervisor continuation limit; registry-only preparation and delivery bookkeeping do not. The default limit is route-derived rather than a fixed constant: use the larger of the compatibility floor and the bound route's declared node count plus one retry slot for each unique `resume_retry_boundaries` node. An explicit positive owner-launch override may replace that value. Missing, unreadable, mismatched, or unbound route evidence retains the finite compatibility floor; it never creates an unlimited supervisor. A declared 13+ continuation chain therefore reaches terminal report harvest and final handoff, while attempts beyond the declared chain plus retry headroom remain `continuation-limit-exceeded`.
   - **Owner route binding and duplicate launch receipt under SD-97:** `dispatch-owner --route-evidence` verifies the sealed route against cwd, capability, mode, intensity, route hash, and selected owner harness, then forwards an owner-level binding. Each adapter wrapper revalidates it and exports `AGENT_ROUTE_FILE` and `AGENT_ROUTE_ID` with an empty `AGENT_ROUTE_NODE`; an owner is never fabricated as a route node. This lets the owner use the declared inline fallback while retaining the material route guard. An exact duplicate claim still starts zero children and stays idempotent, but every wrapper emits `launch_state=existing-active|existing-completed` instead of a silent success-shaped no-op; batch callers may accept the typed existing state, while a caller requiring a new start must branch explicitly.
   - **Post-launch owner-route lifecycle under SD-97:** a registered dispatch-depth-1 owner may legitimately start without route evidence and compile generation 0 after launch. The compiler then attaches that immutable route to the exact owner attempt through a separate atomic lifecycle record; it never rewrites the route or the launch-sealed registry tuple. Publication holds the canonical registry lock and verifies the current unique owner row, active state, attempt schema, worker/depth/surface, parent session, owner harness, physical worktree/repository, capability/mode, artifact root, route id/hash, generation, and route owner identity. Exact replay is idempotent; a partial launch binding, a second generation-0 candidate, hash drift, unrelated owner/worktree/session, or a close/start race fails closed. Continuation publication records an immutable target-keyed candidate under its exact predecessor only after the successor route exists, but compilation alone does not advance the binding: adoption additionally requires a current-contract registered depth-2 child row for that exact owner, route file/id/hash, worktree/capability/mode/artifact tuple, and `launch_started=1`. A childless candidate is inert and may coexist with a later candidate; exactly one child-adopted target advances the current owner route, while two child-adopted targets are a competing-successor conflict. The current owner route advances monotonically through exact generation `n -> n+1` edges whose source hash, successor hash, supersession metadata, owner identity, and worktree contract all verify; downgrade, gap, competing active successor, tampered lineage, or an unrelated route cannot take over the binding. Consumers resolve the launch binding or post-launch attachment first and then this verified edge chain, so retained child evidence preserves the current generation across restart.
   - **Fleet owner-lineage projection under SD-97:** group, process, and JSON views use the same authoritative current owner generation. A valid explicit launch/attachment binding is folded through the verified successor chain even while a superseded source route remains open. When no explicit binding record exists (including legacy attempts), Fleet may recover only a single linear chain formed from exact owner-linked child attempts and terminal attempt evidence, with every route hash, generation, source/successor edge, owner/worktree identity, and reuse contract verified. A compiled route with no owner-linked child evidence is not an active stage candidate. Two real successors, disconnected or unverifiable lineage, or conflicting explicit evidence remains the typed `multiple-owner-routes` ambiguity; timestamp ordering and "latest route" heuristics are forbidden.
   - **Declared runtime requirements and lifecycle evidence under SD-97:** a node may declare only registry-known `runtime_requirements`. `loopback-listen` means localhost bind while outbound network remains denied; a runtime that exposes only a broad network boolean reports `loopback-only-unsupported` and uses the checked main/inline handoff rather than widening outbound access. Every registered row records the requested launch lifecycle plus bounded selector evidence (source, NSpid width, and PID-1 class) so an intermittent nested-sandbox lifetime failure is diagnosable without transcript inspection.
3. **Merge and cleanup belong to main or the orchestrator:** merge only after an explicit user signal or while harvesting a background job that main dispatched. Do not self-merge the current turn's substantive branch; finish with the branch and a concise report while preserving main unless the user has already authorized integration. Review `git diff main...<branch>`, skip regressions or duplicated work, resolve conflicts by interpreting both intents rather than choosing a side automatically, stop when ambiguity would revert an established result, and verify the integrated build. “Merge everything” means merge all valid work selectively, not blindly accept every diff.
   - After merge, integrated verification, and a successful push of the
     integration ref, main automatically runs
     `utilities/worktree-cleanup.py --check --worktree <path>` and then the
     same command with `--apply` when eligible. The state machine blocks the
     primary worktree, dirty/untracked state, Git operations, locks, unmerged
     HEADs, an integration ref not synchronized with its upstream, and active
     exact job PIDs or process cwd. It never uses `--force`, and the branch is
     retained as a rollback point.
     A repository with no remote may instead pass the explicit
     `--repository-mode local-only`; this skips only the upstream/push-sync
     gate and records `integration_upstream=local-only`. It does not infer
     local-only from a missing upstream and does not weaken clean, merged,
     inactive-process, lock, or Git-operation gates.
   - Cleanup does not copy or harvest agent artifacts: workers wrote them to
     the canonical root from the start. A stale matching open registry row is
     reconciled to `done,note=cleanup-merged` only after every other safety
     gate passes. `--all-eligible` considers only worktrees referenced by the
     selected jobs registry; `git worktree lock` is an explicit keep veto.
   - Runtime lifecycle events such as Claude `SessionEnd`, Codex `Stop`, or
     OpenCode `session.idle` do not prove merge/push completion and must never
     perform destructive cleanup. They may expose diagnostics only.
4. **Shared artifacts:** route writes to shared artifact-root files through the §5.8 lock. `plans/<slug>/` remains path-separated and noncontending.
5. **Context:** when coordination records pressure the main context, propose a post-it handoff under the global continuity rule.

**SD-91 current Codex override:** the projected Codex Stop bridge silently reads
only enough payload to clear one exact interaction marker. It reads no registry,
starts no subprocess or SessionEnd work, and emits no continuation. This
supersedes older `native-Stop` and ordinary interactive park wording in this
section; those shapes are migration history only.

The SD-92 advanced-thread clause accepts only a gateway-witnessed fork lineage
or an exact same-thread resume. An unrelated thread switch remains
`managed-gateway-not-ready` and cannot inherit the predecessor's completion.

**SD-110 runtime-owned deterministic stage advance.** At an eligible-linear
boundary — completion gate proven, exactly one non-terminal runnable
successor, predecessor `commit_expected: false`, delivery consumer
negotiated receipt schema v3, supervisor phase parked with no owned or
delivered-open child, successor lifecycle detached — the per-process session
supervisor (Claude session-resume, Codex App Server) closes the completion
gate and starts the successor itself, and the owner model does not resume
for that boundary. Every other boundary and every refusal leaves today's
path unchanged: the model resumes exactly once, receives the ordinary
delivery receipt, and performs gate close, dispatch, merge, arbitration, or
commit itself. The runtime advance authority is exactly three things — a
runnable-successor census, a checked successor start, and a crash-idempotent
transaction between them — never a new launch authority: it calls the same
checked wrapper a model turn would call, with the same argument shape, and
holds no git, merge, push, worktree-cleanup, or user-facing-report
authority. The one start surface is `stage-dispatch-fallback.py --start`;
`dispatch-node.py --action start` is not wired as a runtime-advance caller in
this cycle, and `dispatch-batch.py --parallel-group` is out of scope for
runtime advance entirely. Delivery stays receipt-vocabulary-compatible: v1/v2
consumers see a byte-identical receipt and the advance does not proceed for
them; only a negotiated v3 consumer can ever receive the separate
`stage_advance` block, and the negotiation that permits eligibility (2)6 and
the negotiation that gates that block's delivery are the same single
decision, never two independently toggled ones.

### §5.10a. Completion Delivery Clarifications (SD-92/97)

- The interactive Claude `asyncRewake` bridge recognizes both an exact
  `dispatch-owner --start` and the quick one-shot
  `dispatch-node --action start` surface. Neither command is wake authority by
  itself: arming still requires exactly one recent, same-session,
  claimed-and-started depth-1 `worker_type=owner` row stamped
  `parent_completion_delivery=claude-parent-runtime`; zero, multiple, stale,
  foreign, or non-owner candidates arm nothing.
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
  `core/OPERATIONS.md:383-385` are unchanged, and the existing pre-status
  typed reasons (`managed-entry-not-enabled`, `managed-parent-runtime-mismatch`,
  `managed-parent-harness-mismatch`, `managed-parent-thread-mismatch`,
  `managed-control-missing`, the `managed-control-*`/
  `managed-state-directory-unsafe` socket reasons, and the `managed-status-*`
  framing reasons) are disjoint from this set and stay unchanged.

### §5.11. Commit and Push Policy for `<agent-home>`

After validating changes to instructions, rules, hooks, preflight, or runtime status surfaces under `<agent-home>`, commit and push them in the same turn without a separate user signal. This policy was ratified on 2026-06-12. A work repository's push is separate and remains subject to its deployment gate.

### §5.12. Continuation Supervisor and Tracked-Workflow Completion

`WORKFLOW §0.6` defines the portable state machine and the four continuation
kinds. This section owns the mechanics, and it applies to every tracked
workflow — lab, code, ship, spec/research, CI and check cycles, external-state
monitors, loops, registered workers, and detached resource jobs alike.

**One supervisor, not per-capability copies.** `utilities/workflow-supervisor.py`
is the single continuation implementation. A capability declares its stage
graph, terminal nodes, and human gates in `capabilities/topologies.json`; it
never reimplements continuation. The supervisor is a non-model process: it
holds no model turn, opens no dispatch depth, and has no launch authority
beyond the successor its sealed route already declares.

**Advance evidence is four-part and fail-closed.** Before a supervisor may
start a successor it proves, for the predecessor: exact process identity
(recorded `pid` plus `/proc` start time plus command-line hash, so a reused PID
is a mismatch rather than liveness), a terminal exit result, a sentinel or
typed terminal handoff carrying that result, and the existence of the
predecessor's declared output artifacts. Missing, unreadable, or
namespace-unverifiable evidence is not success. A nonzero exit, a `FAIL`/
`BLOCKED` verdict, or an absent declared artifact records `FAILED_RETRYABLE` or
`FAILED_TERMINAL` and starts nothing downstream.

**Exactly-once is claim-based, not schedule-based.** The successor key is
derived from the sealed `route_hash`, the predecessor node, the predecessor's
exact terminal identity, and the successor node. The supervisor takes the
route-scoped lock and creates that claim with `O_CREAT|O_EXCL`; the creator
starts the successor and every other observer — a concurrent duplicate
supervisor, a restarted one, an operator-run poll — reads the existing claim and
starts nothing. Claim files are durable, so restart recovery is replay from the
append-only journal plus the on-disk claim set: a supervisor resumes at the last
confirmed stage and never re-fires a stage it already claimed. Two supervisors
watching the same route therefore create one downstream job, not two.

**Resource-job lifecycle is registry-owned.** `resource-runner start` records
the run under a sentinel wrapper that persists the payload's exit status even if
every observer dies, and records the launching registered attempt as
`parent_attempt_id` so the parent/child relation between a registered headless
worker and its detached resource child is explicit. Any observation that finds
the process gone — `reap`, `status`, a supervisor poll, or a Fleet scan through
the shared classifier — atomically persists the terminal row: `succeeded` or
`failed`, `exit_code`, `ended_at`, and the workflow state. A stale `running`
row is a defect, not a state; `working` is only ever recomputed from exact
identity and is never read from the stored status word.

**Managed completion resumes a parent thread once per batch.** A registered
batch's parent thread is resumed exactly one time when the whole batch is
semantically terminal and execution-quiescent, under the existing
completion-delivery contract above. When no managed completion surface is
available, the workflow uses a checked external supervisor rather than a model
sleep loop, a fixed delay, or an arbitrary detached shell that claims to finish
the work.

**Visibility is a requirement, not a nicety.** Independently of capability,
Fleet and the status surfaces expose the workflow, its current stage, its child
resource jobs, resource class and identity, last update, next stage, and
failure reason. Where a runtime cannot render a resource row directly, it shows
the supervising owner and links the child registry;
`workflow-supervisor.py status` is the portable projection that any surface may
read. An ordinary detached process is never run invisibly on the user's behalf.

---
# Governed workers and detached resources

All repo-launched model-backed workers pass through `utilities/model-worker-governor.py`, which applies a global cap, per-class caps, rolling start budget, kill switch, and PID/starttime stale-lease cleanup. Its shared state lives under the canonical artifact root so the main checkout and linked workers use one writable governor. A registered dispatch launch reserves its slots atomically before any registry row or model process is created; a parallel batch reserves its exact declared N legs in one locked operation on first start, so insufficient total/class/start-budget capacity creates zero partial rows and zero model processes. An idempotent recovery may reserve one missing leg only after all other N-1 manifest-bound rows are proven active or completed. Each reserved dispatch runner claims one opaque reservation and releases it after its command exits; parallel-group provenance survives reservation-to-claim transfer and is copied into the immutable attempt row. Unused reservations are cancelled or pruned with their exact owner PID/start identity. Governor PID and group scans preserve `inaccessible`/`incomplete` as an occupied, unreleasable state instead of pruning a lease or reservation as dead; only a complete empty group releases descendant-held capacity. Other worker classes atomically acquire their lease in the governed runner. The legacy non-consuming `check` remains diagnostic only and is never a launch authorization. A launched worker inherits the same governor root before it can dispatch a child. This does not modify runtime-owned native subagent limits. A standard+ cycle's concurrent slot occupancy is dispatch-depth-1 owner 1 plus a parallel group's 2–4 legs, so its peak is 3 at `standard` and 4–5 at `strong+`; the `dispatch` class cap of 8 is the minimum that lets two standard+ cycles run at once (3×2=6, 4×2=8), and the global cap of 12 leaves 4 slots for the non-dispatch background classes (`title`, `distill`, `loop`). The per-class cap sum (8+1+4+2=15) deliberately exceeds the global cap (12): each class keeps its own ceiling, but the global cap is meant to be the real bottleneck under load, and `AGENT_MODEL_WORKER_CLASS_LIMIT_<CLASS>` overrides one class's cap when that priority balance needs to shift.

Registered model-backed jobs remain owned by the dispatching session even when the runtime launcher uses a background OS process. The main or dispatch-depth-1 conductor launches, polls, harvests, and integrates the job in the same task flow; an absent OS parent does not grant an independent lifecycle or permit the orchestrator to end early. Only long-running non-model resource jobs use the independent `utilities/resource-runner.py` lifecycle. Reattachment and signals for those resource jobs require PID, process start time, process group, command identity, absolute cwd, log, and run-registry identity rather than PID alone.

Each registered dispatch attempt also owns its summary lifecycle. While the governed worker remains behind its launch fence, the selected adapter starts one non-model summary supervisor bound to the exact attempt id, log path, and worker PID/start identity; the same registry transaction publishes that owner identity before releasing the worker. The supervisor requests one early summary, ordinary debounced updates while the exact worker lives, and one final update after log quiescence, then exits without completion, signal, retry, or launch authority. Initial and final requests may each use one durable `(harness, session, phase)` admission ticket when the ordinary rolling refresh budget is exhausted, but never bypass the provider kill switch, per-session lock, governor, or global concurrency cap. `dispatch-reconcile --apply` may idempotently restore a missing supervisor only for one open, exact, live attempt. An extinct registered namespace-local row from a pre-receipt runtime may be removed from the active Fleet set only through `dispatch-reconcile --attempt <id> --cancel-receiptless-namespace --apply`: this exact operator action records `failure_class=cancelled`, writes no PASS, marker, or reap receipt, and deliberately leaves successor readiness fail-closed. Fleet's explicit kill path likewise closes only the selected exact attempt as a typed cancellation; its wrapper remains responsible for the genuine post-exit receipt. Fleet is otherwise a pure observer of registry and stored summary sidecars: starting, refreshing, or closing Fleet never creates provider work. Interactive sessions use their runtime lifecycle bridge as the summary producer and follow the same bounded admission rules.

A post-exit receipt can become permanently unissuable. The detached drain
receipt requires `attempt-tagged-empty-v1`, so one process that escapes the
governed process group while still carrying the attempt tag blocks that proof
forever: `reconcile` answers `terminal-draining` with
`marker-missing-post-exit-receipt-incomplete`, the completion join answers
`process-unverifiable`, and a worker that reported PASS and wrote its artifact
has no checked way back. `dispatch-reconcile --attempt <id>
--seal-artifact-proof-receipt --apply` is the recovery, and it is completion
evidence rather than a cancellation: it seals a typed
`receipt-superseded-by-artifact-proof` substitute only when the artifact named by
the exact terminal PASS envelope still hashes to the digest the worker itself
recorded at its own last `stage-heartbeat --phase artifact`, the governed process
is dead, and the observing session is in the namespace that recorded that PID.
The seal writes no marker and no verdict of its own; the row still closes through
the ordinary `reconcile` or `capability-route.py complete` path afterwards. Any
unprovable link in that chain is a typed refusal, never a fail-open close, and a
row that already carries a genuine receipt is refused as
`post-exit-receipt-present`. Only this seal lets a sealed proof outrank a live
attempt-tagged process, and only at a terminal gate — an unsealed row keeps the
ordinary veto.

Detached resource runs are first-class lab/resource jobs, not registered agent
dispatches and not members of `jobs.log`. Every `resource-runner start`
atomically registers its absolute run-registry path in the harness-owned global
index `<agent-home>/.dispatch/resource-runs.index.json`; an existing registry is
imported without restarting its processes through `resource-runner index
--registry <resource-runs.json>`. Fleet and status surfaces discover every
indexed registry and fail soft per index, registry, and row. They never trust a
stored `status=running`: current liveness is recomputed as `working` only when
the recorded `pid`, `/proc` start time, and command-line hash all match;
verified process absence is `exited`, and identity mismatch or incomplete
identity is `stale`. Each run remains a separate row even when cwd/project is
shared. Default views hide `exited` and `stale` resource rows while `--all`
restores them. Stop or any other signal-capable control must revalidate that
same exact identity plus the recorded process-group leader immediately before
signalling. This contract was promoted after live GPU training was invisible
to Fleet on 2026-08-04 despite a complete experiment-local registry.

### Managed dispatch registry and wait receipts

A managed interactive parent selects its canonical `AGENT_DISPATCH_JOBS` at
entry, so a dispatch-depth-1 owner cannot replace it; an explicit path may only
be a realpath-equivalent alias. Never reconstruct this path, or any other
dispatch state path (completion markers, logs, heartbeats, watchdog,
supervisor-state, homes, broker, degradations, workflow, index/journal state),
as `$AGENT_HOME/.dispatch/...` inside an activated session: packaged
`$AGENT_HOME` is immutable versioned source, not the enrolled runtime-state
home, and release rotation physically removes it
(`tools/install/distribution.py` `_cleanup_releases`). Agent home and dispatch
state root are separate concepts with separate canonical resolvers — exactly
one resolver per concept, per runtime. A registered dispatch parent seals the
agent home value it resolved into the child's environment; no descendant
wrapper or hook recomputes agent home from its own physical install location
once launched.

The model may yield `runtime_wait: registered-children` only after each checked
`--start` receipt reports `registered=1`, `started=1`, and `child_spawned=1`.
`check=ok`, a dry-run identifier, or a register-only row is never launch evidence.
The supervisor accepts only exact child rows whose durable launch fence records
`launch_started=1`. An empty or register-only runtime wait receives one bounded
same-session correction requiring `--start` and the three-field receipt; repeated
absence then fails closed.

### §5.13. Operator Compute Hosts

Sessions run on one machine while training and evaluation belong on whichever
host holds the right GPUs. The installed `compute-hosts` command delegates to
`utilities/compute-hosts.py` and owns that boundary so
neither half is rediscovered per session.

The static half — addresses, ports, environment roots, and the shared run root
— lives in one user-owned file at
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/compute-hosts.yaml`, alongside the
other cross-runtime policy files; install seeds it once as a commented template
and neither install nor update ever rewrites it. `harness config status` shows
its state next to the other user-owned config surfaces. The
launcher is shared across runtimes, repairs only an exact owned link, preserves
foreign collisions, and is removed only by a full uninstall. That file
is byte-identical on every host: which entry is the local machine is discovered
by matching its declared `hostname`, not written down, so promoting a different
machine to session host is a change of habit rather than an edit on every
server. An inventory label and a system hostname need not agree, which is why
the match is against a declared field. Live state is never recorded: `list` and
`probe` measure reachability, CPU utilization, and GPU utilization/free memory
at the moment they are asked.

`claim <host> <pid> --harness <runtime> --session <id>` is the narrow bridge for
an already detached `nohup`/`setsid` process whose runtime ancestor and session
environment no longer survive. It writes only to the shared run root's
`.process-owners.json`, never to the inventory. Creation revalidates the remote
root PID's start time, effective UID, and command-line SHA-256; every later
probe revalidates the same tuple and requires the current GPU process ancestry
to contain that exact root. A stale/reused PID, changed command, ambiguous
claim, missing ancestry, or unreadable `/proc` remains unattributed. Cwd, PID
number alone, and transcript text are never ownership evidence.

`run` starts a command detached under a stable run id and writes its log and
exit code beneath the shared run root, so the session that launched the work
may end long before it finishes and any later session on any host that mounts
that root can follow it by id through `runs`, `tail`, and `stop`. A run id
doubles as the remote `tmux` session name, and a second launch within the same
second takes a fresh directory rather than overwriting the first one's log.

This is deliberately not dispatch: no capability, registry, attempt, or
completion gate is involved, and the harness never chooses a host on its own.
The acting agent names the host.

It is also not the registry-owned resource-job lifecycle above. That one tracks
a detached local process against its launching attempt, so a conductor can
poll, harvest, and integrate it inside one task flow. `compute-hosts run`
answers a different question — *which machine* — and deliberately keeps no
attempt binding, because the work usually outlives the session that started it.
Use the resource-job lifecycle when a registered attempt must own the run;
use `compute-hosts` when the run belongs on another machine. A run that needs
both is a resource job whose payload is a `compute-hosts run` invocation.
