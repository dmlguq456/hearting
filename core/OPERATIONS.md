# Operations — Git, Worktree, Dispatch, and Push (canonical)

> Git operations—locks, preflight, worktree dispatch, and `<agent-home>` pushes—differ from artifact conventions and therefore live here. Preserve section numbers and headings because Skills, drills, and hooks link to anchors such as `OPERATIONS.md#59-…`. This is the single source for git operations.

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
- **Component transactions:** `--spec-root` and the route's existing component spec scopes select the seed automatically; a literal `<component>` placeholder does not name a component. Standard+ begin prepares the declared components and their version history before research/review. Direct begin leaves seeding to its transaction, which selects the component through `--spec-root` and preserves the exact PRD pre-image with the same helper. Seed file copies use the existing atomic writer, so a failed write leaves no partial destination and the same seed can retry its pinned base. The existing base receipt retains the scope and each completed seed, so retries or another component transaction retain edits and deletions. A whole-tree transaction retains its current meaning. Scoped admission verifies the sealed source's component boundary for initial, exact-base, and stale-base publication, overlays only its selected components onto the verified base, and uses the existing merge and latest CAS to preserve components outside that scope. Their absence in a scoped candidate is not a deletion; actual deletions and overlapping changes within the scope keep the existing conflict rules. No extra selector, proof file, or drop flag is needed for an ordinary component update.
- **Shared spec admission:** the first seed records its exact reference/base revision and seed completion in `artifacts/spec/_internal/shared-base.json`; retries retain that base and preserve edits and deletions after seed completion. Admission verifies the sealed candidate, its base, and latest under its lock. D-122 automatically merges changes to disjoint files/components or Markdown sections; overlapping changes require review with their locations. The sealed source stays immutable: the derived publication records all inputs, source inventory, merge policy and output proof, while publication/recovery retains the latest-revision CAS. Older/manual cycles without a receipt supply their actual base using `--base-revision <id>` (`none` for initial publication). Exact retries validate and reuse the published revision without rewinding latest; old journals lacking base/source proof stay unresolved for inspection.
- **Spec stage order:** the shared producer owner `begin` finishes its spec seed after releasing admission's lock and before any standard+ research or review worker runs. It uses the spec lock and pins the chosen reference/revision in the existing receipt; `prd-transaction` reuses that receipt before applying the reviewed change. This is one runtime sequence for Claude, Codex, and OpenCode, without a separate agent seeding step. The later transaction still supports an already-open cycle whose begin predated this sequence.
- **Route declaration:** a route that can touch any `spec/**` path declares `spec_touch=true`. Before lock acquisition the conductor runs §5.9 git-state checks. Missing declaration or a route/node scope mismatch is a structured failure tied to the route id.
- **Contention:** a nonblocking acquisition first reports `BLOCKED`, then waits. After acquiring it re-reads the latest spec and version chain and enters the next version. The chain is one per spec, not one per layout: the next `v{N}` is `max+1` over the current spec root, the legacy `spec/`, every cycle-held spec tree (`artifacts/spec` in both cycle layouts, symlinks followed, and the W7H residue home `artifacts/_internal/spec`), and every `shared/spec` revision of every reference; a component spec follows its own `<slug>/` chain, and a root that cannot be walked is a typed refusal (`version-chain-unenumerable`), never a low number. It never retries a previously computed `v{N}` and never overwrites an existing snapshot.

Acquire immediately before `autopilot-spec` Step 3 or update mode, `autopilot-code` state/summary writes, or a spec-drift update. The helper holds the lock around the supplied transaction command, prepares and verifies the prior PRD bytes itself, and exports `AGENT_SPEC_NEXT_VERSION` only after the latest version is re-read under lock. The caller supplies only the transaction command:

```bash
REPORTS_DIR=$("${AGENT_HOME:-$HOME/hearting}/utilities/artifact-root.sh" "$PWD") || exit
python3 "${AGENT_HOME:-$HOME/hearting}/utilities/spec-transaction.py" run \
  --artifact-root "$REPORTS_DIR" --worktree "$(pwd -P)" \
  --route "$ROUTE_RECORD" --node "$ROUTE_NODE" --wait-timeout 600 -- \
  sh ./the-owning-capability-transaction.sh
```

Exit 3 means the bounded wait expired; report the current owner and leave every spec surface unchanged; the next run allocates a fresh version number. The lock is an `flock` on the artifact root's `.pipeline-lock`, so deleting that file while a transaction holds it lets a second transaction write at the same time.

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

Dispatch requires a branch in every route worktree, including spec-only
routes whose artifacts live outside the checkout. `compose --start` creates it
for source-changing work started from a primary checkout (`<repo>-wt/<slug>`,
WORKFLOW compose defaults); elsewhere create it with
`git worktree add -b <new-branch> <path> <base>`. For an existing detached task
worktree, `git switch -c <new-branch>` inside that worktree preserves its current
HEAD and uncommitted work. Dispatch refuses a detached HEAD as `unsafe-git-state`
and names that command.

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
if [ -n "$op" ];        then echo "STOP: $op is in progress; apply OPERATIONS §5.9 conflict-resolution authority before editing"; fi
if [ "$br" = DETACHED ];then echo "STOP: detached HEAD($head); check out a branch before risking a lost commit"; fi
[ -n "$elsewhere" ] && echo "WARN: branch '$br' is also checked out at $elsewhere"
[ "${ahead_behind%%	*}" -gt 0 ] 2>/dev/null && echo "WARN: upstream is ${ahead_behind%%	*} commits ahead; integrate before continuing"
[ -n "$merged_in" ] && echo "DONE-BRANCH: '$br' is zero commits ahead of origin/$def; start new work from the latest base: git switch -c <new-slug> origin/$def"
echo "state: branch=$br head=$head base=$def dirty=$(git status --porcelain 2>/dev/null|wc -l|tr -d ' ')"
```

- **STOP and conflict-resolution authority:** halt ordinary edits and commits during merge, rebase, cherry-pick, or detached HEAD. Never auto-abort or force-checkout. The agent resolves the conflict itself: read both sides' commits, keep what each side intended, and name each resolved path and the side or combination kept in the commit message. Ask the user only when the two sides choose different user-visible behavior and neither commit history nor the task says which one is wanted.
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

A registered dispatch tree runs on the release its root launch resolved: the
adapter wrapper resolves `AGENT_HOME` once to the real release path
(`sealed_launch_home`) and passes that one value to every descendant, the row's
`launch_home`, its allow rules, its owner Codex home, and its hooks (Claude hook
commands run `hooks/run-hook.sh`, which takes the hook from `AGENT_HOME` when set, as
the Codex hook commands do); a later pointer change moves none of them. An
interactive depth-0 session resolves the installed release per call
(`hearting run`, printed commands), so each new route starts on the release
installed when it starts. Instructions, skills and plugins are what each runtime
loaded when its process started. Runtime-owned credentials, sessions, logs,
caches, databases, and Codex `config.toml` remain outside this activation
boundary.

Route validation distinguishes immutable code identity from path-bound state
identity. A resolved-path alias of one code root is always the same root. Two
different physical roots may compare equal only at an explicitly code-root-only
site and only when both are complete managed-release copies with regular-file
release metadata, matching `RELEASE_VERSION`, the same version and archive
SHA-256, and identical sealed release revision plus code-anchor digest. A
missing or malformed marker, a changed anchor or release revision, or a
different release fails closed. This exception never applies to cwd, artifact
root, jobs registry, completion/log/heartbeat state, runtime home, or any other
mutable or path-owned surface; the general agent-home/path equivalence helper
remains resolved-path-only. Launch-tuple start validation keeps its exact
sealed path and identity checks, except where the installed release moved from
the managed release a route was sealed under to another verified managed release
copy: that is where the launch runs, not the work, so its release roots and the
registry's release fields are neither refused nor reported
(`route_authority.release_moved`), and each launch row keeps the `launch_home`
it ran from; a replacement on another release likewise follows the release in
the values the runtime derives from it, and a permission path into the harness
tree compares the same whether it names the release directory, another release of
the same managed install, or its `<share>/hearting/current` pointer; any other
checkout stays a different path (`route_authority.granted_permissions`). Route close keeps its existing integrity
and currentness policy. This compile-time exception changes neither lifecycle
fence and cannot hide session-root drift.

### §5.10. Work Isolation and Parallel Dispatch

The shared `status` snapshot displays exact-attempt observations alongside
registry words: PID/start identity, exit/sentinel evidence, log modification
time, and the declared result artifact. It reuses the Fleet classifier and
does not reconcile or mutate work. An exited process without verified
completion is `exited`, not workflow success; missing evidence is unknown.
Large snapshots name their sampled scope, so a bounded display never implies
that omitted jobs have ended.

Dispatch same-work identity gates are opt-in: only `HEARTING_GATES=on`
enforces sealed-versus-current route, parent, lineage, runtime and review
bindings. Otherwise `utilities/hearting_gates.py` emits `hearting: gate-off`
and execution uses current inputs. Input/schema errors, missing files, path
and symlink safety, lock ownership and process-liveness checks remain enforced.
Historical records are preserved; bypassing identity comparisons does not
rewrite them or turn a live process into completed work.

Completion evidence has two moments. First close proves the current readable
evidence for the exact route, owner/attempt and terminal claim; an unreadable
or conflicting record cannot establish completion. Once the existing outcome
and terminal claim record that proof, later edits, moves, or deletion of the
referenced report are history about the completed work, not a reason to rerun
or reapprove it. This does not waive marker, registry, claim, route identity,
or process-cleanup checks. With same-work gates off, deletion of evidence for
an already completed review follows the same recorded-history rule; the first
completion still requires readable evidence, and gates on retain their live
evidence check.

Each SD item below keeps the rule an agent acts on; its full decision record is verbatim in `core/ADAPTATION.md §8`.

Adapter and projection changes follow the same core-first order as other portable work: establish and read the governing `core/` contract before adapter edits. A generated projection's determinism covers its file mode, not only its bytes: a generator that writes plugin JSON (`hooks.json`, `plugin.json`, marketplace manifests) fixes the mode to `0644` on every write regardless of process umask, and its `--check` counterpart fails a foreign mode as a stale projection alongside a content mismatch (S-5d, owner-supervisor-liveness — a reproducible regenerate cycle flips `hooks.json` away from `0644`; the first mutating syscall was not isolated, so the fix enforces the invariant rather than only diagnosing it).

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

An optional `allocation.owner_order` in the selected dispatch defaults orders eligible owners within each quality band. Explicit choices and pins, runtime/account eligibility, the balanced usage gate, relief promotion and all-gated headroom recovery retain precedence. This order is sealed into new routes and applies only to `worker_type=owner`; frame, review and stage worker allocation retain their existing policies. Without it, owner allocation also retains its existing behavior. Harness eligibility comes from the selected user policy, so an explicitly configured OpenCode deep profile may participate in that policy's deep band.

**Main-session role contract.** The dispatch-depth-0 main session is the context owner, router, orchestrator, and final integrator — not the default executor of every stage. It directly owns: memory and existing artifact-root recovery (`.agent_reports/`, legacy `.claude_reports/`); user-intent and artifact-state reconstruction; compact-metadata primary/secondary capability selection under `WORKFLOW §0.2`; the completed route-confirmation card under `WORKFLOW §0.4`; spec, guard, worktree, and currentness checks; work decomposition and write-ownership decisions; worker dispatch with registration, liveness watching, and harvest; cross-stage conflict and semantic decisions; final consistency integration of metrics, documents, and notes; and the user-facing response. Before confirmation, main does not preload the full entry Skill or its references. At `standard+`, the dispatch-depth-1 capability owner reads that contract, and each dispatch-depth-2 worker reads only its assigned stage contract. When a stage is separable, main does not take on inline: long experiment or evaluation execution, repeated checkpoint inference, bulk figure/media generation, report HTML assembly, mechanical document synchronization, independent verification/QA, or implementation work with a clear file boundary against other stages.

**Main/worker bootstrap boundary.** Every registered headless dispatch and repo-owned background model caller exports `AGENT_SESSION_ROLE=worker` before launch. Legacy adapter markers remain worker evidence and fail closed. The portable worker input is `roles/worker-bootstrap.md` plus exactly one `roles/worker-types/{owner,stage,review,support}.md` fragment and the assigned capability/stage contract. `worker_type` selects only that bootstrap fragment; `capability` plus `assigned_contract`/`route_node` select the work contract; `model_role` selects behavior; route-sealed `model_profile` selects the execution budget. These axes never select or rename a bootstrap. `worker_role` is legacy read-only metadata. Workers retain deterministic safety, permission, scope, route validation, handoff, liveness, and verification guards, but the harness does not add main response policy, entry confirmation, automatic memory/briefing injection, SessionEnd sync, Fleet title/token/UI context, the full capability catalog, unrelated stage contracts, integration, merge, push, cleanup, or user-facing explanation. A worker never manufactures a main session. This boundary applies to dispatch depth 1, dispatch depth 2, loop/drill invocations, title workers, and runtime-native subagents; dispatch depth, bootstrap type, assigned contract, model role, and model profile remain separate.

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
| A new independent request while work is active | Dispatch it to a new worktree alongside the first job |

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

Dispatch state stays outside every release, so pruning a release cannot delete
it and the path that sealed `launch_compatibility_tuple` values and open rows
depend on stays the same.

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

Release pruning is evidence-bound. `harness update` retains every superseded
release whose migration or retention-containment proof is incomplete. An
operator may run `harness update --force-prune-unproven` only after reviewing
the proposed loss: before deleting each unproven candidate, the updater
appends its non-recoverable gap to the canonical `inventory/gaps.jsonl` with
`discovered_by=forced-prune`; a failed gap commit deletes nothing. The flag
does not override current/activation retention, `_release_in_use` or
`_succeed_dispatch_state`: open rows and routes whose resolved `launch_home`
names the release, and live processes whose `AGENT_HOME` names it, still keep
it. It is never selected automatically. Re-running the command when the
requested release is already current still performs this checked prune pass.

Before delivery, the supervisor atomically commits the bounded receipt payload,
deterministic receipt id and digest, exact attempt set, and row revisions. A
restart reuses that committed payload and identity. The receiving runtime
acknowledges the exact receipt after its turn completes; a different receipt
cannot be consumed by a stale turn. Harvest inspects or reconciles the worker
record and does not acknowledge notification delivery. Neither a missing harvest
command nor an unchanged worker row authorizes repeated model turns or owner
termination. The common controller commits exact terminal evidence before
delivering completion and retains unresolved closure, child recovery, and parent
notice. Launch dependencies, write authorization, and explicit terminal cleanup
scopes own operation permissions. Existing human gates retain approval authority.

The shared parent resolver owns harness, session, and cwd; adapters consume its
result. A registered parent advertises end-turn only with its exact live supervisor
lease. Claude and OpenCode use the same CLI session controller with native runtime
drivers; the controller owns join, terminal commit, resume, and receipt acknowledgement. A selected child and Git checkout ancestry cannot substitute for parent
identity. The parent runtime supplies its native session identity independently of the
child adapter. A harness runtime clears the inherited `AGENT_DISPATCH_CALLER_HARNESS` and every other
harness's native session ID from its own tool commands; its own native session ID, set by
the runtime itself, then identifies it. It does this through its installed native config
surface, so a name or ID inherited from whichever process started it (a pane, a launcher,
or a shared service such as the Codex app-server daemon) does not survive into its tool
commands, and no harness exports a name that could go stale in a process its shell starts.
On a host where only some harnesses carry the config the result is an ambiguous caller,
not a wrong one. The shared resolver follows an explicit name (a per-command override or
a registered worker's marker) and refuses an unnamed mixed environment; launchers carry no
identity logic. Dispatch carries the issued producer cycle and a concrete output
directory in both environment and prompt. The producer record owns that path;
write admission and completion publication use the same cycle binding. A missing
cycle environment can be recovered from the route's producer record. Refusals
name the correct output directory, and node scopes are relative to that directory.
Another open cycle or a matching filename suffix does not grant write authority.

An OpenCode child inside a Codex owner's workspace sandbox receives per-attempt
XDG data/cache/state/config directories beneath the worktree. User configuration
and existing authentication are linked for reading; generated dependency state
stays in the attempt directory. The
adapter prepares these paths before admission and reports preparation failures
before spawning; the model does not diagnose or retry missing runtime storage.

**Dispatch responsibility:** execution, semantic outcome, and notification are
separate facts.

| Decision | Accountable component | Required follow-through |
|---|---|---|
| Start and execution lifetime | Claimed execution boundary | Publish the actual runner identity, enforce its finite budget, and account for governed descendants before releasing resources. |
| Completion | Exact terminal writer under the jobs lock | Preserve the committed result. A later process observation cannot turn success into failure. |
| Wait, recovery, and retry eligibility | Shared attempt policy and supervision controller | Reconcile exact evidence, retry only a settled retryable failure, and transfer an unresolved decision to the parent through durable delivery. |
| Failure cleanup and supervisor exit | Execution boundary, with the exact post-exit watcher as recovery owner | Finish or retain an explicit cleanup obligation. |
| User notification | Shared pending-delivery record and recipient runtime carrier | Keep the obligation until accepted or explicitly handed back. A display update or expired polling interval is not delivery. |

A blocked transition owes either a bounded recovery action or an actionable
parent notice naming the unresolved attempts and the responsible component.
An elapsed join interval is a checkpoint, not child death or owner failure.
Duplicate observations converge on the existing obligation; successful results
and already accepted notifications are not replayed as retries. Read-only
queries neither cancel work nor acquire these responsibilities.

An awaited receipt contains only the exact route-bound batch, including its
verified owner-route advance; unrelated attempts under the same parent retain
their own execution and cleanup responsibility. A terminal row proven never
launched by the durable launch fence does not hold replacement settlement open,
while a claim, PID, start identity, or unavailable proof remains protected.
Successful frame delivery stays owed until the recipient's exact route decision
has been consumed; emitting the notice or closing the route alone is not
consumption evidence.

`dispatch_attempt_policy.py` is the shared decision table. Terminal writers,
join/harvest, and the retry claimant consume it; the jobs lock admits at most
one automatic successor for an exact `automatic_retry_of` predecessor. Only a
transport failure (a death, a runtime error, a capacity stop) is such a
predecessor; a worker's readable `FAIL` or `BLOCKED` is its result, so the next
launch of that node is new work on capped and uncapped nodes alike. An
explicit new review round remains a workflow decision. Stage boundaries specify
inputs and outcomes; they do not themselves imply another process launch.
Conflicting terminal evidence preserves that result and receipt while pausing
automatic consumption. `dispatch-registry.py resolve-terminal-conflict` previews
the exact row and prints its `apply_command`, which carries that row's digest
(`--expected-row-sha256`); the parent's review report (`--review-evidence <report>`)
releases consumption.
A different conflict invalidates that disposition; classifier ranks grant no override.

`dispatch_supervision.wait_for_batch` owns repeated join checkpoints across
session supervisors, serial chains, and the managed completion carrier. It
keeps execution alive, schedules exact recovery, and uses the existing
pending-delivery queue for `kind=supervision` notices. A notice names the exact
unresolved attempts and read-only diagnosis command. The recipient explains the
blockage and asks for a disposition if evidence cannot resolve it; notification
acceptance never cancels an attempt or authorizes retry. Gate and supervision
notices share claim/send/acceptance mechanics, with separate semantic validators.
A recovery receipt binds the work and parent; its courier proves the current
connection generation at claim time. Claim counts are audit data, not a delivery
cutoff. Couriers own backoff, one-wake bounds, and transport no-resend evidence.
A settled attempt suppresses a late recovery notice. On controller exit, the
exact orphan watcher retains state unless cleanup is proven or the unresolved
decision has been durably handed to its parent.

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
2. **Execution and naming:** create the worktree with `git worktree add <path> -b <slug> origin/<base>` using §5.9 base selection. The sole canonical path is the sibling directory `<repo>-wt/<slug>`, such as `Foo-wt/<slug>` for `Foo`.
   - **Source-only worktree:** immediately resolve the primary checkout's
     canonical artifact root. Dispatch wrappers inject it as
     `AGENT_ARTIFACT_ROOT`, include it in prompt/registry metadata, and open
      only that external path through runtime-native scoped access (Claude/Codex
      `--add-dir`; OpenCode exact `permission.external_directory` rule).
      A granted read-only root opens the same way with edits denied on OpenCode;
      an overlapping writable root keeps its write rule.
     Task output belongs in that canonical root.
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
   - At `standard+`, the dispatch-depth-1 owner is a thin conductor. It dispatches compiled `code-plan`, `plan-check`, `code-execute`, `impl-review`, `code-test`, and `code-report` nodes through dispatch-depth-2 headless sessions, reads verdict/status metadata rather than stage bodies, and passes context only through files. A route stage is the semantic work and completion-gate unit; a worker session is only an execution-capacity unit. They are not one-to-one. The owner may keep a stage in one session or declare bounded first-class sub-sessions below the same route node when scope size, context pressure, or round-trip cost warrants it. That choice is owner discretion and does not require route recompilation. A declared `parallel_group` replaces member-level starts with one exact `dispatch-batch --parallel-group` transaction. Before any stage, the owner verifies that the artifact root and `spec/` exist.
   - Artifact relocation uses that same authority to protect live work. A no-live history move does not transfer parent authority, and historical redirects never authorize root-local writes.
   - **Route authority:** `utilities/route_authority.py` is the one place that answers who continues a route (the parent session and its successor), on which harness (sealed selection pins), how many attempts of which kind (sub-session standing, retry links, round budget, the result envelope), and what it may access (the execution access grant). Callers read these answers from it instead of keeping their own copy; the round budget (`review_round_cap`) and the access grant (`execution_access`) keep their implementation modules and are reached through it.
   - **Stage-session separation:** every planned sub-session carries a stable `subsession_id`, ordered index/count, serial-or-parallel mode, fixed file list, narrow verification command, expected round trips, phase brief, and worker-state ledger. It retains the parent route id/hash/node, stage scope, and gate, but records `stage_authority=0`: it may produce a bounded handoff and terminal attempt result, never publish or satisfy the stage completion marker. The dispatch-depth-1 owner publishes exactly one stage marker only after all declared sub-sessions are semantic-terminal, execution-quiescent, and their combined stage evidence meets the original gate. Planned subdivision consumes no gate-failure retry budget. A later gate failure may open only a gap session containing unfinished items from the prior handoff; it is a retry, not retroactive subdivision. The runtime records a sub-session opened after the node's latest settled round failed or ended `BLOCKED` as `gap-retry` whatever its manifest says, and a dry-run preview of a declared sub-session reads the same no-stage-authority admission as its launch; the round cap keeps binding full-stage rounds only.
   - **Auxiliary legs are advisory, never gate-holding.** A declared `parallel_group` leg with `leg_class: auxiliary` widens the group with one closed narrow check on the `light` budget. Its unit verdict enum carries no blocking token, so an auxiliary finding can never satisfy or fail the stage gate alone — it exists to feed the arbiter's `auxiliary_findings_considered` merge (the completion gate compares the merged array length against the realized auxiliary leg count). The `all` join policy is a separate axis: it joins every realized leg, including auxiliary ones, but joining evidence is not the same as letting an auxiliary verdict block. At least one realized **peer** leg must land on a quality-peer harness (SD-100 ①) whenever the user's policy defines one (an unavailable defined peer still refuses; a policy that defines none — for example OpenCode-only — proceeds and records `sole-gate-non-peer-harness` instead of refusing); auxiliary legs may legitimately use any eligible harness including OpenCode.
   - **Who arbitrates, and when.** The arbiter of an auxiliary-bearing group is never the group's own anchor — the anchor is a sibling that runs *concurrently* with the auxiliary leg and cannot have read its output. The arbiter follows the anchor's kind: a `review-worker` anchor is merged by the owner (conductor); a `map-worker` anchor is read by its declared downstream consumer node; a `pipeline-stage` anchor by its direct downstream `review-worker`. A **node** arbiter carries `auxiliary_findings_considered` in its own completion evidence, with exactly one entry per realized auxiliary leg it arbitrates (summed when it arbitrates more than one group). For an **owner-merge** arbiter the owner waits for the group to join, writes the merge record with `auxiliary_findings_considered` in its frontmatter, and registers it:

     ```
     python3 utilities/capability-route.py arbitrate \
       --route <route-file> --group <group_id> --evidence <merge record>
     ```

     The transaction is fail-closed and each refusal is typed: `auxiliary-group-unknown`, `auxiliary-group-has-no-auxiliary-leg`, `auxiliary-arbiter-is-node` (a node arbiter owns it instead), `auxiliary-arbitration-before-join` (some realized leg still has no completion marker), and a length/absence refusal on the array itself. It writes one write-once `<group>.arbitration.json`; an identical re-registration is idempotent and a different one conflicts. Until it exists, any node that depends on a member of that group is refused at the wrapper start-gate with `auxiliary-arbitration-missing` and the route's terminal-gate observation carries a failed `parallel_group:<group_id>` row, so `terminal_gate_proven` stays false. A group whose arbiter cannot be resolved at all is a different event and both surfaces name it `auxiliary-arbiter-unresolved`: it is a route-integrity failure that `arbitrate` cannot clear, and it refuses only the completions of the nodes that would have arbitrated it, never every unrelated node's. Closing an unarbitrated route is still allowed — it closes honestly as unproven, not as complete.
   - **Sub-session scheduling and mutation:** serial sub-sessions form one declared chain and should be registered/joined as one batch so runtime completion resumes the owner once, after the whole chain. A chain runner starts each exact registered attempt only after its predecessor is terminal and quiescent. A start refused before spawning for a reason waiting may fix (`PRELAUNCH_PROCESS_BLOCK_REASONS`, its row still registered only) is started again at a backed-off interval within the prelaunch grace (`PRELAUNCH_WAIT_GRACE_SECONDS`), each refusal recorded in the advance record; past the grace the refusal stands and closes the rest of the chain as before. Parallel sub-sessions use the existing sealed parallel-group transaction and require provably disjoint fixed-file ownership. Mutating overlap is serial even when analysis or verification can run in parallel. During a declared sub-session chain, first-parent descendant `HEAD` movement is accepted under the same lineage proof as an in-place mutation retry; it neither recompiles the route nor spends retry budget. A native runtime subagent may assist inside one sub-session only within that sub-session's fixed files and stage scope, with serial mutation, summary-only return, and no gate authority; unsupported adapters use the checked registered-headless or inline fallback without claiming native parity.
   - **The parallel-subdivision surface an owner actually calls.** One command reads the plan's single `slices` block, writes and proves the manifest and phase briefs, reserves the slots, registers and starts the slices; the gate that follows is typed too:

     ```
     python3 utilities/dispatch-batch.py --parallel-group <node> --slices <plan.md> --start
     python3 utilities/capability-route.py complete --route <route-file> --node <node> \
       --evidence <stage evidence> --jobs <registry> --subsession-manifest <chain.json>
     ```

     The start receipt carries `chain_manifest` and the exact `next_command` for the gate. Slices run in the manifest worktree: the route cwd, or — while the same-work gates are off (`HEARTING_GATES`, default off) — a linked worktree of the same repository (`--worktree`, default the caller's worktree when it qualifies). Each slice binds to exactly the owner whose attempt id it inherited (`AGENT_DISPATCH_ATTEMPT_ID`, forwarded as `--parent-attempt-id`), so a same-slug owner cannot be picked by accident. `--subdivision-manifest <chain.json>` and `stage-session-chain.py plan-slices` remain as the lower-level surface the one-command path is built on. When the plan declares no `slices` block, the block is malformed or duplicated, the owner id is missing, or disjointness cannot be proven, the command stops before any slot, row or child exists and prints the ordinary single-session receipt with a `next_action` — the exact `stage-dispatch-fallback.py` command that runs the stage as one session. That is not a failure.

     A hand-written manifest names, per session, the leg it runs as, with `node` (the realized leg's route node id) or `leg_index`; a session that names neither is refused rather than matched by list position. A subdivision that cannot be proven disjoint and in-scope does not raise — the batch prints a typed receipt `{"state": "single-session-required", "reason": "subdivision-disjointness-unproven"}`, exits 0, leaves one SD-93 ledger row, and the owner then runs the node as one ordinary session. That state is the owner's signal to stop treating the stage as split; nothing else consumes it.

     Admission records a worktree baseline keyed by the manifest hash, and the stage gate measures against it. This is what makes the post-hoc diff-scope audit a statement about the slices rather than about the whole worktree, and it is why the same manifest can be re-admitted and re-completed idempotently. The gate refuses with `subdivision-baseline-missing` when no admission baseline exists, `subdivision-commit-attempted` when `HEAD` left the baseline commit's first-parent line or a lineage-clean commit carries a slice's `fixed_files` (parallel slices are no-commit workers), and `subdivision-scope-violation` when a change outside the declared union appeared after admission, whether it is still uncommitted or already in a commit. Each refusal writes an SD-93 ledger row and no marker. **The order is fixed: the owner closes the stage gate first and commits after.** Committing the slices' work before the gate is refused, because at the gate that commit is indistinguishable from a slice having committed; committing after it is ordinary, and replaying the same gate on the same manifest and evidence then resumes the published marker instead of re-auditing a worktree that has legitimately moved on. **Declared limit of that judgement:** the gate tells an owner commit from a slice commit by what the commit carries, so a pre-gate commit that carries no slice's `fixed_files` and leaves every file's content identical to the admission baseline is judged neither — not a slice commit, because it carries none of the declared union, and not a scope violation, because its content still matches the baseline — and it passes. In that one shape SD-103's no-commit rule is stated but not enforced. It is the accepted cost of not judging by HEAD movement alone, which refused the owner's own commit against a write-once baseline and an unrewindable HEAD, with no recovery path. After a failed slice, the owner derives the gap-retry chain from the failed slices alone (`stage_session_contract.derive_gap_retry_manifest`); it carries exactly those slices' `fixed_files` and their leg binding, and never re-opens a successful sibling's. When a serial chain stops, the runtime writes its rest itself (`dispatch_subsession_advance.chain_continuation`): a session that ran and did not pass runs again as a gap-retry whose brief adds how that attempt ended, what its handoff recorded as done and the leg's done-when items it left unmet, the never-started sessions follow, and a passed session is not repeated. The owner's chain notice names that manifest and the one `stage-session-chain.py start --manifest <file>` command (the owner is its own `--parent`).
   - **Phase brief, state ledger, and scope stop:** each sub-session reads a compact phase brief plus the previous bounded handoff instead of reloading the full specification by default. The phase brief assigns the work; `narrow_verify` limits its verification command, not the execution of that work. A parallel slice's brief names the plan, the worktree, the fixed files, its verify command and the rule that it makes no git writes (no add/commit/checkout/restore/stash/reset/rollback; git reads run with `GIT_OPTIONAL_LOCKS=0`, which the launch also exports), leaving `checklist.md` and the dev log to the owner. It persists `_internal/state/<attempt_id>.md` with the current slice, completed items, exact next command, invariants, and forbidden files. The ledger is flushed at least every three material edits and after every verification round trip. Pre-compact must validate and flush it; post-compact must re-read it before another edit. A missing or stale required ledger fails closed. The fixed file list is an execution fence: discovering a necessary file outside it stops the session with a handoff to the owner, which may create another sub-session. Wide mechanical edits use a codemod plus bounded diff verification instead of expanding an individual session ad hoc.
   - **Separability under SD-17:** dispatch is mandatory when the stage output contract is complete and its edit surface is not boundary-coupled through shared semantic anchors or sequential boundary assertions. A non-separable stage may run inline only if the reason is recorded in `plans/<slug>/_internal/metrics.md`; missing evidence is a contract violation. Parallelize separable census or independent file groups in-session. `hooks/stage-dispatch-reminder.sh` only reminds a dispatch-depth-1 conductor of this; it does not deny an in-session stage on any harness.
   - Dispatch-depth-2 review helpers are read-only by default. The code route opens framing at width two for `standard`, widens framing to three and opens width-two plan/implementation-review groups at `strong+`, and adds declared third implementation-risk/failure-mode legs at `thorough+`. Other capability groups stay at their registry-declared width. Every leg is a sibling under the same owner, has a sealed role/profile/perspective and disjoint write scope, and joins before continuation. No worker fan-out, undeclared breadth, or dispatch depth 3 is permitted. Stage-worker ownership remains disjoint: `code-plan` owns plan artifacts; `code-execute` alone mutates source; `code-test` owns test evidence while source stays read-only; `code-report` owns the final report and locked summary.
   - The number of concurrent workers is set by the model-worker governor's `dispatch` class limit (`utilities/model-worker-governor.py`, `CLASS_LIMITS`); there is no separate per-conductor process cap. Each stage pipeline is sequential, and in-session implementation-team workers do not count. Dispatches beyond the governor limit are queued or refused by the governor.
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
   - For a route-bound job, the compiler selects and seals both portable model role and model profile; wrappers resolve the profile through adapter config and reject trailing model/effort replacement. A model or effort different from the profile default is chosen once at `compose --pin <owner|frame|worker>=<harness>[:<model>[@<effort>]]`, sealed into the route (`selection_pins`, inherited by continuations), moved later only by the route's parent with `start --pin` (recorded beside the route), and read by the wrapper from the bound route with those changes applied (`model_source=pin`, `model_pin_status=applied|harness-only|harness-mismatch|none`); a checked capacity retry may still replace a pinned model and then reports `model_source=pin+capacity` with `model_pin=`. A sealed worker or owner pin also beats an explicit `--adapter` while the pinned harness is usable (supported by the checked tuple and under no active usage limit); the replaced request is recorded as `explicit_adapter=`, and a pinned harness that cannot run keeps the request and reports `harness-mismatch`. A sealed worker pin also bounds `stage-dispatch-fallback` descent: only the pinned harness's headless candidates may take that step (native-subagent/inline only when the owner runs on that harness); other harnesses are recorded `skipped-worker-pin`, and when the pinned harness cannot take the step the chain stops with `reason=worker-pin-unavailable` and one plain detail line instead of moving the step to another tool. Top (main-session-only) models are accepted for `frame` only; an owner or worker pin naming one keeps the tool and drops the model with `warning=pin-dropped`. Non-route direct/lifecycle surfaces retain their explicit model/inheritance contracts. Registered substantive owner/stage/review nodes reject `mini`. The orchestrator chooses harness placement subject to sealed diversity axes.
   - **Cross-harness routing under SD-16:** before dispatch, query each harness through `utilities/usage-check.sh`, which reports `ok`, `limited(reset)`, or `unknown`. The cascade is explicit target (including an explicit pin), hard eligibility, sealed affinity/policy, the balanced usage gate, quality band, then allocation ordering (target shares scaled by the optional `allocation.harness_weights`, e.g. OpenCode `0.3`). A frame leg reads the `harness_policy` sealed on its own node; an explicit harness outside the profile's bands is followed while it is enabled. Unknown gauges pass the balanced gate optimistically and use the neutral ordering share, while account-bound native quota rejections (through their exact reset and model scope) and legacy death markers from `usage-check.sh` remain hard exclusions; under `capacity-aware`, unknown/zero headroom is excluded. Allocation order is compiled and sealed into the route (`dispatch_allocation`, `dispatch_defaults_digest`); `verify` never reloads live config. A user prohibition is stronger than any signal. A policy change is complete only when the allocation ledger (`utilities/dispatch_allocation_receipt.py list|summary`) shows the new field on a real attempt, not when the file validates.
   - **Nested child-spawn eligibility under SD-48:** root headless readiness,
     runtime-native subagent readiness, and a conductor's ability to launch a
     registered dispatch-depth-2 child are separate runtime surfaces. Before
     compiling a route with dispatch-depth-2 nodes, bind checked evidence for
     `(parent_harness,parent_transport,parent_sandbox,child_harness,launch_authority)`.
     Only `supported` is eligible; `unknown` and `unsupported` fail closed.
     Dispatch contract v3 requires `launch_authority=conductor`; logical
     ownership remains `dispatch_depth=2,parent=<conductor>`.
   - **Owner binding 거부 안내:** `owner-route-binding-tuple-invalid`는 현재/기대 dispatch depth, worker type, raw route-file 유무와 위반 조건 각각을 출력한다. owner 환경에서 depth-2 자식을 직접 wrapper에 넘긴 호출자는 `stage-dispatch-fallback.py --node <node> --start`를 사용한다(route·slug·parent는 owner의 현재 route와 환경에서 채우고, 봉인된 병렬 그룹의 구성원은 그룹 전체를 batch 한 번으로 띄운다). 이 호출자가 raw `--route-file`을 직접 추가하거나 owner 환경을 수동 제거하도록 안내하지 않는다. 정식 launcher 내부의 node binding 전달은 유지한다.
   - **Checked fallback chain under SD-50:** a standard+ stage ranks checked direct-headless candidates through its sealed quality bands, then falls through to `native-subagent -> inline`. The conductor invokes eligible adapter wrappers in that sealed order and proceeds to the next candidate only after a recorded launch failure. Native and inline degradation record skipped candidates, failure classes, registry attempt ids, allocation/headroom evidence, and assurance compensation without claiming Fleet parity.
   - **Direct headless launch under SD-61~63:** dispatch contract v3 has no resident launch broker: a standard+ dispatch-depth-1 conductor invokes the checked adapter wrapper directly for every dispatch-depth-2 attempt. A duplicate or already-started claim never creates another child. Production dispatch never calls the retired broker's `ensure`, `request`, or `serve`.
   - **Execution access is separate from permission posture:** an optional `execution_access_v1` file declares exact writable roots, read roots, and network need for one launch. Lab owner start/resume also prepares the initialized compute-hosts inventory's exact `run_root` through that request; this default is limited to lab owners. Explicit data roots use the existing request file, which lab preparation validates and preserves while adding run storage. Missing/template inventory supplies no guessed root, and invalid or broad roots retain pre-launch refusal ([usage and limits](../docs/lab-execution-access.md)). Without an explicit file, normal start/resume preparation carries what the approved task names through owner and child launches: its explicitly named target set, and the roots `execution_access.derive_task_access` derives from the sealed task text. Every existing absolute folder the text names (a file names its folder) is a read root; only a clause of the start card's `범위:` (`Scope:`) field that says it writes there and says nothing that reads or excludes supplies a write root, and only for the owner node; any other scope path is read, an excluded one is neither, and a doubtful reading falls toward less access. Sensitive paths (credentials, keys, user and runtime settings, system areas) and roots that are missing, too broad, or already granted are skipped; a derived root adds no refusal, a node derives once and keeps that result on resume, and each root names its source line in the request `justification` and the route's prepared `binding.json`. The route's parent may hand its next owner another request: the existing `AGENT_DISPATCH_EXECUTION_ACCESS_FILE` when it runs `start`, `correct` or `compose --start` becomes one `access` row in the route's append-only change record (`route_authority.record_access_change`, with who gave it and its source; a first derived request is a `derived` row), and the next owner, a replacement owner and their children launch with the latest row (`access_in_force`). Another session's request is not recorded, and one that does not validate meets the launch as before. Artifact-relative target-table input resolves from the route's current canonical artifact root (without repeating the `.agent_reports` prefix), while source-relative input stays under route `cwd`. If an explicit approved target cannot be prepared, start/resume returns its existing typed execution-access error before invoking a wrapper; it does not mark grants ready or fall back to default grants. Each child remains within the exact live parent's effective grant. The wrapper accepts the request through the existing `--execution-access-file` or `AGENT_DISPATCH_EXECUTION_ACCESS_FILE` surface, validates and hashes it before registration or model spawn, and refuses malformed, broad, symlink-escaping, parent-expanding, or unsupported requests instead of retrying without the request. When preparation yields no request, existing grants and argv stay byte-for-byte. Runtime projection is honest rather than flag-shaped, and each adapter declares it (`adapters/<harness>/config/harness-capabilities.json` `access`): Codex uses `--add-dir` for `codex exec` and `--writable-root` for App Server supervision; current Claude and OpenCode path controls are `tool-permission`, not OS sandboxes, and their network grade is `none`. Read roots are projected on every harness: the Codex sandbox reads everywhere and writes only to its writable roots, Claude adds each root with an Edit deny rule, OpenCode grants `external_directory` read-only; the grant records the guarantee (`read_enforcement`), and a read root that holds or lies inside a writable area stays writable there (`read-only-root-writable`). A boolean Codex network grant is not a host allowlist. Requests never merge with approval keys, enable blanket bypass/full-access, edit user config, or widen a child's inherited parent boundary. A nonessential herdr status observation that fails with `PermissionDenied` does not change the source result or block artifact completion; it grants no socket or runtime directory access.
     Normal start publishes the attempt's canonical effective grant with either its exact node route tuple or its validated node-less owner binding; the live parent's row supplies the same tuple when a child reads that grant. A worker dry-run with an access request resolves the same exact live parent and effective grant as start; a prospective dry-run without an access request keeps its existing behavior.
   - **GPU lab execution sandbox:** `autopilot-lab` execution and validation nodes (`smoke`, `full-run`, `run-verify`, including their parallel legs) select the existing Codex `danger-full-access` mode for their registered owner and node launches even without a promotion signal. Explicit typed GPU resources (`resource_class=gpu`, or the existing `gpu` promotion signal on a resource runner) keep the same selection. One selection feeds prospective readiness, sealed parent sandbox tuples and exec/App Server execution; caller sandbox choices and forced settings take precedence. Normal code, frames and non-execution lab children such as plan, report and handoff retain their defaults. Full access removes Codex filesystem and network enforcement; approved `enforcement_required=any` roots remain a logical parent/child request boundary, recorded with grade `none` and unmet enforcement. Strict OS enforcement requests retain refusal. Compose and launch receipts show the selection and the device limitation; an outer or managed runtime constraint still applies. GPU availability is measured by a read-only device query, rather than inferred from the selected mode. User configuration and existing routes remain untouched.
   - **Codex owner write advisory (SD-162):** mutating routes show a read-only sandbox/Git/external-path advisory in compose, explain and start/resume receipts; the Codex wrapper reports its applied sandbox and existing grants before registration. Primary protected `.git` and the linked-worktree owner's narrow Git grant remain distinct from the declared sub-session no-commit contract; this notice neither grants access nor changes approval, launch decisions, or user settings.
   - **Registered headless permission posture:** a registered Claude worker or owner is a `claude -p` turn. Without a start flag `claude -p` inherits `permissions.defaultMode` from the settings scopes (managed, project-local, project, user), and only when nothing is configured does it start in `default` (Manual) — the plan's built-in `auto` default applies to interactive sessions, not to `-p`. The harness's own settings template writes `permissions.defaultMode: "auto"` into `~/.claude/settings.json`, so a Hearting-launched `claude -p` inherited auto mode with the permission classifier. A non-interactive run has no prompt to fall back to when the classifier blocks repeatedly, so a headless owner whose harness commands are blocked in sequence dies as a cascade of denials with no typed cause. Hearting-launched sessions are unattended by design, so the Claude wrapper pins the start mode itself. The shipped default `headless.claude_permission_mode: bypass` in `profiles/dispatch-defaults.yaml` (user copy `~/.config/hearting/dispatch-defaults.yaml`; per-launch override `--permission-mode` / `CLAUDE_DISPATCH_PERMISSION_MODE`) adds `--permission-mode bypassPermissions` to every registered `claude -p` and to the session-resume supervisor's first and resumed turns. `allowlist` is the opt-out and the automatic fallback where bypass cannot apply — root/sudo, or `permissions.disableBypassPermissionsMode: "disable"` read from **any** settings scope the runtime honours (managed, `<worktree>/.claude/settings.local.json`, `<worktree>/.claude/settings.json`, user) — and it pins `--permission-mode acceptEdits` (file edits and common filesystem commands inside the working directory and the added artifact root are approved without the classifier) plus `--allowedTools` naming what a registered worker contractually needs and nothing broader: the harness utilities (`capability-route`, `dispatch-node`, `dispatch-batch`, `dispatch-owner`, `dispatch-harvest`, `stage-dispatch-fallback`, `artifact_producer`, `spec-transaction`) under the sealed `AGENT_HOME`, read-only git, `git add`/`git commit` for a commit-expected owner or single-session stage, the harness test runners, and `Edit(//<worktree>/**)` / `Edit(//<artifact-root>/**)` (Edit rules also govern Write). The same allow rules ride along under `bypass` too — they have no effect while bypass holds and take over if the runtime demotes the session. The applied posture is evidence, not intent: the wrapper records `permission_mode=`, `permission_mode_reason=`, and `permission_inherited_mode=` (the settings `defaultMode` the turn would otherwise have started in) in the attempt row and its receipt. Deny rules apply in every mode, so the proven-fatal async deny (`--disallowedTools`, SD-71/78) is unchanged. Codex parity already holds — `codex exec` runs with `approval_policy=never` (it never prompts) while the sandbox stays the security boundary, and `--dangerously-bypass-approvals-and-sandbox` is never used on this registered-dispatch surface. That prohibition is scoped to registered dispatch: a **managed interactive** Codex session the harness launches (bare `codex`, `resume`, `fork`, through `utilities/codex-launcher.py`) does start in bypass by default, the interactive-surface counterpart of `steward.child_permission_mode` rather than of this key, with `AGENT_CODEX_INTERACTIVE_PERMISSION_MODE=inherit` and any caller-stated posture as its opt-outs (`adapters/codex/ADAPTATION.md`). OpenCode parity is the wrapper's `permission` config (`external_directory` deny plus scoped allow): the headless auto-reject that truncates a session is avoided by `deny`, not by a bypass.
   - **Checked evidence probe — whose parent and worktree are being probed:** create the final isolated source worktree first, then run every standard+ eligibility probe from and against that exact canonical path. `utilities/nested-dispatch-eligibility.py` emits one tuple per child harness, and every tuple seals `checked_worktree`; `capability-route.py compile` rejects a tuple whose path differs from the route `cwd`. Evidence from the primary checkout, a staging directory, or a worktree that will later be replaced is never reusable for the final route. Every `--parent-*` value describes **the process that will launch the dispatch-depth-2 node — the dispatch-depth-1 registered-headless capability owner — not the session running the probe.** A dispatch-depth-0 caller sealing its own runtime is the recurring failure mode, so all three fields resolve or fail closed rather than defaulting to the caller: `--parent-transport` defaults to `auto` and is always `headless` for a dispatch-depth-2 tuple (an explicit `interactive` is canonical vocabulary for the caller and a contradiction here); `--parent-sandbox` defaults to `auto` and resolves the parent harness wrapper's canonical `AGENT_DISPATCH_CURRENT_SANDBOX` export; `--parent-harness` stays required because the owner's adapter is a later `dispatch-owner` decision, and its `auto` resolves only inside a wrapper that already exports its identity. A probe running inside an active Codex owner still requires its checked `AGENT_NESTED_HEADLESS_NETWORK=1` marker. A dispatch-depth-0 caller checking a Codex owner that has not started uses the explicit `--prospective-standard-owner --jobs <canonical-jobs.log>` mode instead; that mode proves the exact registry and lock are writable, evaluates the same standard+ dispatch-depth-1 owner predicate used by the Codex launcher, labels the evidence as prospective, and never fabricates the runtime marker. A missing marker without that mode means only that the active-owner runtime is unconfirmed, not that an explicit Codex dispatch-depth-1 selection is unsupported. `utilities/dispatch-readiness.py` is the required depth-0 pre-owner surface: it applies that prospective mode automatically with the exact registry and atomically emits the complete checked evidence object, so callers never assemble tuples manually. Calling the raw active-owner surface without an active marker is typed `prospective-owner-check-required`, not a runtime-global network failure. The same parent fields, exact worktree, canonical registry, and final owner sandbox projection are cross-checked before launch, so a wrong subject or unwritable registry fails before an owner is paid for. Because the owner's adapter and the sealed `parent_harness` must agree, pass the compiled route to `dispatch-owner --route-evidence <route.json>`; that selector-only option constrains the adapter cascade to the harnesses the checked tuples actually probed and never crosses the wrapper boundary. Sealing dispatch-depth-0 values instead blocks hop 1 and 2 for every node and every adapter with `dispatch-evidence-parent-runtime-mismatch` or `parent-attempt-not-found`; it never authorizes inline execution.
   - **Failure scope is a fallback axis:** a checked tuple carries `failure_scope`, `codex_command`, and `retry_on_isolated_worktree`. `failure_scope=runtime-global` may remove that runtime from the normal checked fallback chain. `failure_scope=exact-worktree` with `retry_on_isolated_worktree=1` instead means the runtime command exists but the probed filesystem shape is unusable; route compilation stops with a re-isolation/re-probe requirement and must not select another harness, native helper, inline execution, or `danger-full-access` as if the runtime were globally unavailable. The canonical Codex example is a user-owned `.codex` non-directory in one checkout: preserve it, create the final clean isolated worktree, and re-run the probe there.
   - **Namespace-safe launch lifecycle under SD-72:** `stage-dispatch-fallback` selects the child lifecycle from the actual launcher scope, and a wrapper may promote `detached` to `foreground-scoped` when its own scope is transient — that is normal selection and costs no attempt or retry budget. Native subagents do not substitute for either lifecycle. Timeout or signal termination closes only the exact attempt row with its typed cause; a zero process exit is only an observation and succeeds solely when an exact completion marker or typed terminal handoff proves it.
   - **Successor readiness and parallel launch under SD-79/80/89:** a completion marker is semantic stage evidence, not proof that the governed process has released its lease; successor readiness is a checked gate (`prior-attempt-still-live` / `prior-attempt-unverifiable` block it) and is never replaced by a fixed sleep, a delayed marker, or a larger cap. An immutable `parallel_group` of 2–4 route-declared siblings starts only through one `dispatch-batch --parallel-group` transaction; omit `--log-dir` (the canonical `logs/` default) and copy evidence to cycle artifacts through a separate collector. Individual group-member `register`/`start` fails before row or process creation. SD-160 accepts separate executions with distinct personas on the same harness without degradation. Usage gates take precedence over optional harness diversity; historical sealed cross-harness axes remain provenance, while launch, completion and receipt consumers apply the same effective persona policy. Model-profile/perspective and actual harness realization remain recorded. OpenCode is eligible for registered standard+ dispatch-depth-2 dispatch: it implements exact parent binding, foreground lifecycle, and supervisor snapshot parity; its quick/relief surfaces remain a separate authorization path and do not substitute for this parity.
   - **Immediate limit-death handling under SD-15:** wrappers watch briefly after launch; a child that exits immediately on session, usage, or authentication limits is marked `done note=dead-<reason>` (plus `reset=<time>` when known). Launchers bind the authentication scope; the shared native-evidence reader feeds later selection without rewriting old rows. Unbound historical quota is diagnostic only. Wrappers do not retry; the existing owner/fallback controller retains cleanup and sealed quality-policy checks for redispatch.
   - **Canonical global attempt registry under SD-49 (amended by SD-112 §13.33.2-(8)):** `AGENT_DISPATCH_JOBS` is the sole canonical dispatch registry, resolved once at dispatch depth 0 from the install shape (never inside the active release tree) and passed immutably to every descendant. Its parent directory is the canonical dispatch state root — no reader reconstructs it as `$AGENT_HOME/.dispatch`. For a nested launch, `--jobs` may only repeat that inherited absolute path; a cycle-local override fails closed. Cycle-local files are audit mirrors, never authority.
   - **SD-156 mutation-node in-place retry and declared sub-session lineage:** after an execute failure or partial completion, a mutation node may be redispatched on the same immutable route. `worker-route-guard.py` reads one shared lineage verdict (`capability-route.source_lineage_verdict`) against the sealed `source_commit`, for every node, regardless of position or prior registry attempts. `exact`/`descendant` `HEAD` both pass and are recorded onto the attempt row (`launch_head`, `source_commit_sealed`, `source_commit_distance`, `source_commit_branch`); a declared planned sub-session under a mutation node is covered by the same verdict, with no separate lineage proof to carry, and its `stage_authority=0` keeps it apart from gate retry accounting. A `diverged` `HEAD` is refused (`route-source-commit-mismatch`) with two named recoveries: return to the sealed line of work (switch back to the sealed branch, or use reflog to restore the sealed commit), or compose a new route with `--parent-cycle <current cycle>`; an `unverifiable` tree (mid-merge, detached, git timeout, non-repo) fails under its own reason and does not count as a pass. Do not recompile or re-pin the route, and never use `git reset --hard` to restore it. An SD-104 continuation now unconditionally re-pins to the observed `HEAD` when the verdict is `exact`/`descendant` of the inherited pin — the old decline branch that kept the inherited pin whenever a re-run node already had a lineage attempt is gone, since the guard itself adjudicates every moved `HEAD` the same way regardless of position; a `diverged` continuation source refuses outright (`continuation-source-commit-diverged`), and an `unverifiable` one leaves the inherited pin untouched. A continuation publish that lands inside an ancestor's still-open producer cycle binds into it: `bind_continuation_cycle` judges the new route as a D-120 cycle admission against that open cycle immediately after publish and appends an audit record (`route_bindings[]`) only when admission allows; a refusal leaves the publish standing and rides the reason along as a publish-time advisory, distinct from a write-time refusal (SD-155, artifact-path-contract D-120).
   - **Fleet observation:** tell the user once that Fleet shows background work, unless they already use it. Execution and pending delivery are independent: the supervisor records `running-turn` with its unacknowledged outbox and owns acknowledgement/recovery. Fleet consumes exact runtime/tool evidence for activity; observation confers no completion, cleanup, retry, or delivery authority. Unverified and stale evidence remain unknown. One collector projects each snapshot from the displayed rows' bindings and directly scoped artifacts. One live producer owns each refresh through its eventual result; collection delay and last-success age are visible while the TUI stays responsive. Settled failure permits retry; elapsed time alone does not replace a live producer.
   - **Stealth-death guard:** the adapter liveness wrapper diagnoses a silent child: Codex `adapters/codex/bin/preflight.sh liveness [jobs.log]`, OpenCode `adapters/opencode/bin/preflight.sh liveness [jobs.log]`, or Claude/shared `utilities/dispatch-liveness.sh [jobs.log]`. They report `ALIVE`, `SUSPECT`, `DEAD`, or `EXITED`; exit 3 means at least one suspicious or unharvested job. An exact-attempt wait or harvest surface owns normal diagnosis; a parked parent never tails a child transcript/log or searches source/artifacts for progress. Raw inspection is permitted only after that child row is terminal/closed or after an explicit operator recovery override. Exact recorded `pid` plus `/proc/<pid>/cmdline` is the strongest signal. An attempt carrying canonical identity never falls through to cwd-wide transcript activity when exact evidence is stale or terminal. Transcript or DB mtime is a fallback only for legacy identity-less rows because workers sharing a worktree can make each other's directories look fresh; path-based `pgrep` is rejected as false-positive prone. For interactive Codex sessions, a validated exact `task_started`/`task_complete`/`turn_aborted` lifecycle outranks rollout mtime; terminal lifecycle makes the live TUI idle immediately and mtime is consulted only when lifecycle is unavailable or ambiguous.
   - **Completion delivery is runtime-owned (SD-14/78/92/97/113):** a registered owner is launched under an adapter supervisor and the parent obeys the launch receipt's `parent_next` — `end-turn` (a carrier wakes this session once; start nothing) or `bounded-wait` (run the printed `parent_next_command` once). Which carrier delivers, and how, is specified in `core/ADAPTATION.md §7`, never in the model's prompt.
   - **Post-exit parent-bound reconcile under SD-64/71/77:** a conductor that dies mid-pipeline is reconciled by a non-model watcher bound to its exact PID/start. Reconcile first preserves any exact completion marker or typed terminal handoff, then classifies exact process identity before consulting worktree integration state. An exact-PID death closes the row (`dead-exact-pid`, or `dead-parent-orphaned` with one bounded cascade over its open direct children). The watcher settles evidence and publishes the recovery obligation; a bound runtime controller automatically replaces a proven-dead stage, frame leg, or owner once under SD-157. A canonical, logical-node family claim precedes registration and fenced spawn and survives route changes, repeated calls and lost replies. Live or unobservable attempts, live/unobservable owned children, pending terminal commits, and human stop decisions forbid replacement. Preserve original failure rows, completed stage/frame evidence and same-scope human answers. A usage-limit death (`dead-capacity`) is a pause, not a replacement budget: while the usage gate reports the harness limited, `start` returns `waiting-capacity` and launches nothing; after it clears, one `start` resumes the owner under the installed runtime (a sealed-release difference is a same-work diagnostic unless gates are on). When the pause names its reset time (`retry_at`), the runtime runs that `start` itself shortly after it and leaves the session one notice of the result, which repeats the `start` command when that `start` failed (`utilities/capacity_auto_resume.py`; at most three automatic resumes in a row per route). An owner its launcher closed before spawning (`launch_outcome=never-launched`, for example an admission lock still held after the bounded launch wait) is likewise a pause: the `start` that saw it reports that nothing started, the runtime runs the next `start` itself about a minute later under the same bound and notice, and that `start` launches the same work again under its own family without spending the replacement budget. An owner that ended `BLOCKED` is waiting for an answer, not dead: `start` reports `owner-blocked` with the owner's report and its `correction_command`. The person's answer sent there is kept (`retained`), and the same `correct` call runs the shared `start`, which launches one replacement owner on the same route that receives the answer first (`death_kind=corrected`). An owner that ended with a readable FAIL (its own final envelope said FAIL) is answered the same way with a fix a person approved: the replacement reruns the stage that makes the fix and the checks after it, and each failed check the fix answers gets its one closure-check round within the verdict ceiling cap + 1; when every failed check already used that round, no replacement starts and the receipt says `replacement-fix-round-spent`. A readable FAIL is not retried automatically. When the route's parent moved the owner pin (`start --pin owner=<harness>`) before that replacement was claimed, the claim names the new harness and the replacement takes the ordinary owner launch there with the same task, route, worktree and access request instead of replaying the old harness's command. Like a usage-limit stop it is a pause: each answer may continue the work once, and it spends no replacement budget. An owner parked at a declared human gate keeps its own release path. Any other replacement failure ends in needs-attention with no further automatic launch. Capture the original task and validated launch tuple before the first registration and render the replacement under its own identity. The controller must carry the effective attempt mapping through exact join, supervisor parking and delivery; an old failed attempt must not be announced again after replacement success. An owner resumes its current verified active route and open cycle when valid, retaining completion markers and the existing gate journal; a new route is required only when the existing identity cannot be reused safely. A missing upstream is typed `no-upstream-configured` and never blocks registry hygiene.
   - **Commit responsibility follows the sealed route:** a normal single-session mutation node with `commit_expected: true` commits its own validated work in the assigned worktree, including tracked documentation and configuration examples it changed. Source ownership follows assigned scope and tracked content, not filename extension. A declared sub-session (`subsession_id` / `stage_authority=0`) never commits; its owner closes the stage gate and commits after. Add only exact validated owned paths; artifact shadows, runtime state, and unrelated dirty files are excluded. For linked worktrees, wrappers grant commit-expected workers only the per-worktree Git metadata directory plus common `objects`, `refs`, and `logs`; common `config`, hooks, and the rest of `.git` remain outside the grant. Runtime sandbox support must be reported from an applied sandbox measurement, not inferred from argv or a full-access session.
   - **Completion marker bound to the exact attempt row under SD-70:** completing a node takes the canonical registry path and the current exact attempt id (`complete --jobs <registry> --attempt-id <id>`), not just the route/node pair. A registered worker completing its own node may omit both: with no attempt axes stated, `complete` reads `AGENT_DISPATCH_ATTEMPT_ID` when that registry row is this node's attempt, and `AGENT_DISPATCH_JOBS` once an attempt is named; an explicit value takes precedence, and the inline axes form takes nothing from the environment. An omitted `--jobs` on the other dispatch utilities takes `AGENT_DISPATCH_JOBS` when it is set; with nothing inherited, `--jobs` stays required. Completion never breadth-closes a prior `BLOCKED` row or a later retry. Marker write and row close are idempotent under retry; if the row close fails after the marker is written, the command returns a structured nonzero — rerun it rather than editing the registry.
   - **A sub-session slice reaches its own terminal (SD-130):** a `stage_authority=0` sub-session slice holds no stage gate authority, so `capability-route.py complete` refuses it (`subsession-has-no-stage-gate-authority`); a slice closes through the completion join instead and grants no marker eligibility. A slice may only start on a sealed chain: `dispatch-node.py --subsession-id --action start` requires the persisted chain manifest to name it, else `subsession-chain-manifest-unsealed`.
   - **Review verdict is a result, not a worker death (SD-94 owner-closure extension, SD-153 round budget):** a readable exact review FAIL is a completed blocking round, never a dead worker. The shared `review_round_cap.round_budget` is the one admission decision every registered launch surface (`dispatch-node.py`, `dispatch-batch.py`, `stage-dispatch-fallback.py`) and this owner-closure eligibility check now read: its budget unit is the **verdict round** (`classify_round_row` recognizes a success note, a blocking review, or a non-review explicit FAIL — not a crashed, capacity-dead, or invalid-envelope attempt, which is `verdict-less` and does not spend the cap by itself), and `B=2` consecutive verdict-less rounds bind the node to `verdictless-bound` (a BLOCKED round whose unmet `done_when` items, read from its `<artifact>.items.json`, are a strict subset of the previous BLOCKED round's starts that count again; items that cannot be read change nothing) instead of a third silent registered attempt. The shared `complete` writer admits an exhausted-round owner disposition only after verifying review identity, an exhausted verdict-round budget, every blocking finding's linked evidence, and the destination cycle. `dispatch_contract.owner_closure_shape` names the one closure shape a completion marker carries — `continuation` (`stage_authority=owner-closure`) or `registered-review` (`review_gate_closure=owner-closure` with `review_independence=owner-overridden`) — and every terminal-identity reader (`_marker_identity_row`, exact-terminal proof) consumes that shape instead of re-deriving it by hand. For an official continuation, the same operation verifies immutable source-route/hash and node-contract lineage, counts inherited rounds without resetting their verdict-round budget, and publishes an explicitly owner-overridden gate only in the continuation. Its current-cycle record cites the source reviews; source review rows, FAIL results, markers and sealed payloads remain unchanged. `complete --check` evaluates owner-closure admission without publication or state writes; normal completion rechecks before committing. A refusal reports the exact unsatisfied obligation.
   - **Gate-evidence revision is a marker transition, not a rewrite (SD-154):** once a completion marker's live evidence changes after publication, `dispatch_contract.gate_currency` is the one place that recomputes the evidence digest and walks a revision chain (`completion_marker_is_current` is now a one-line delegate to it, so its existing boolean call sites are unchanged); its `revised-unrecorded` state is the only one a person or the runtime may act on — every other non-`current` state (`superseded`, `completion-evidence-unreadable`, any `integrity-broken:*`) is a kept refusal, not something to record over. `capability-route.py revise --route <route> --node <node> --evidence <evidence-path> --basis review-findings|user-direction|owner-correction` is the one writer (`publish_revision_locked`): it publishes marker `k+1` with `stage_authority=revision` and, in the same critical section, tombstones every downstream node's canonical marker (`state=superseded-by-upstream-revision`) so a stale downstream marker is not read as current. Every kept integrity refusal raises before any marker is touched. `dispatch-node.py`'s `admit_round` auto-records this revision (`recorded_by=runtime-auto`) before admitting a dependent node's next round whenever that node's last round was a blocking FAIL that basis-verifies against the `revised-unrecorded` upstream; when that basis verification cannot run itself, the revision stays unrecorded and the dependent's launch is refused `completion-evidence-revised-unrecorded` with `next_action` naming the `revise` command above. After a round budget is exhausted, a node whose last verdict round was a blocking FAIL and whose FAIL a revision names as its `answers` is admitted exactly one more round, `round_kind=closure-check` — spent by that verdict attempt itself, not a standing exception to the cap. That refusal belongs to the opt-in gates (`HEARTING_GATES=on`). With gates off, the default, an evidence edit after its marker is not refused and prints no warning: the operations that proceed on the completion — `continuation`, a stage start, `complete`, a replacement owner's claim — keep it as one history line (`<node>.evidence-changes.jsonl` beside the marker: when, by whom, in which command, why, and the digest it replaced), and the closed route's `revisions` list it as `basis=automatic`. Reads (dry-run, `status`, Fleet) write nothing. `revise` stays the manual record and judges "unchanged" by that same comparison in both modes: the evidence it names against the digest the marker recorded (`revision-evidence-unchanged` only when they are equal).
   - **Review input and closure preview (SD-161):** plan-check inputs have exact attempt-bound path/digest records. Separate input revisions admit bounded review without publishing a PASS marker; the lineage verdict ceiling is cap+1. An admitted verdict correction uses its stable round identity, not the prior worker's death/capacity replacement budget; original retry links remain historical. Current-route and ancestor owner-closure share the completion writer's read-only preview proof.
   - **A review gate names its reviewer, and a self-review is degraded, not refused (SD-OPEN-41(b), SD-94 extension):** the completion marker of a `review-worker` node records `reviewer_kind` (`registered-worker` | `native-subagent` | `owner-inline`), `review_independence`, and the reviewer's identity; name it with `complete --reviewer-attempt <att>` (row must carry `worker_type=review`) or `--reviewer-subagent <transcript>` (readable file, digest recorded). A failed claim downgrades to `owner-inline` with a typed `reviewer_downgrade_reason` and never refuses; the `§0.5` completion card must then say the gate was not independently reviewed.
   - **An independent reviewer can be a registered review worker (SD-OPEN-40):** `dispatch-owner.py` accepts `--dispatch-depth 1 --worker-type review` alongside the owner tuple, and then requires `--unit` to name a catalog persona (never `_kernel/owner` or `_kernel/resource`) and refuses `--route-evidence` — a route node's reviewer is launched by stage dispatch with its node binding, and one node must have one launch path. The owner tuple is unchanged, including its refusal of a caller-supplied `--unit`.
   - **Legacy preview recovery:** an actual launch refused for an unraised preview gate may raise its question outside the jobs lock after proving the current predecessor marker, exact attempt readiness, and artifact. The launch stays refused; dry-run/preclaim stay read-only. Missing proof or carrier yields a typed recovery failure.
   - **Route-free reviewer report binding:** a registered depth-1 review may write durable output only when the caller opts into `--review-output` and the canonical jobs registry proves one unique open `worker_type=review` row with the sealed attempt/depth/transport/surface/unit/capability/worktree/artifact-root/cycle/producer axes and the exact output digest. The wrapper validates the cycle and its sealed route before registry mutation, and jobs carries only a canonical URL-safe base64 locator rather than a raw path whose comma, tab, or newline could change metadata fields. The binding authorizes that one `artifacts/plans/**` report file; it never fabricates route or marker evidence, promotes source-write rights, or permits sibling/foreign targets. A reviewer without the opt-in remains stdout-only. After the fenced process identity is published, only report launches drop the jobs lock for producer admission; they then reacquire it and revalidate the exact row, identity, status, and parent before releasing the launch fence, while ordinary launches retain the prior lock boundary. Schema-v2 report leases combine the exact launch identity with a summary-owned flock under the artifact root, so authorization and completed/abandoned sealing depend on the governed lifetime even when the worker PID is not visible across namespaces. Missing, released, expired, malformed, tampered, foreign-replay, or unlocked evidence fails closed; legacy schema-v1 leases retain their read-only compatibility behavior and are not report-write evidence. For detached reviews, the registered identity remains the finite watchdog; governor reservation transfer is proved against its separately sealed fence/runner child. On timeout, TERM, INT, or admission failure, the watchdog drains the exact child group and attempt-tagged descendants (including new sessions) before releasing the review lease. Unknown namespace or process evidence never authorizes cleanup or lease release. The launcher allows this bounded cleanup to finish before escalation; Linux parent-death signalling alone is not descendant-group cleanup.
   - **Supervisor exact terminal reconcile:** every registered owner supervisor exit classifies its final runtime envelope and process result, then atomically reconciles only its exact attempt before reporting success or typed failure. Capacity, auth, protocol, missing-result, signal/exit, and valid handoff outcomes cannot leave the row open. If the supervisor cannot reach its own finalizer, the exact post-exit owner watcher runs the same classifier-backed closure; neither path breadth-closes a slug/worktree retry.
   - **Terminal authority and actionable receipt under SD-97:** the terminal writer commits one result; later contradictory observations preserve that result and its receipt, hold automatic consumption, and create an exact review obligation. `resolve-terminal-conflict` records the reviewed evidence before consumption resumes; repeated reviewed observations do not reopen it. Receipt schema v2 gives every joined child exactly one `required_action` — `complete-open`, `inspect-done-failure`, or `advance-completed` — and every consumer acts on that same action, so a terminal row cannot become an unharvestable `matched=0` receipt. A stage child still open at delivery whose process has exited with a readable PASS envelope is settled by the runtime first (`dispatch_completion_join.settle_open_pass`), so its receipt carries the result rather than `complete-open`.
   - **Owner route binding and duplicate launch receipt under SD-97:** `dispatch-owner --route-evidence` fills the owner-tuple flags a route-backed call omits (`route_defaults=` in the receipt; a cwd, capability, mode, or intensity that contradicts the route is refused typed), and verifies the route against cwd, capability, mode, intensity, hash, and owner harness; an owner is never fabricated as a route node. An exact duplicate claim starts zero children and reports `launch_state=existing-active|existing-dead|existing-unverified|existing-completed` (the open-row word comes from the shared process verdict, with one plain `note=` line for dead/unverified); a caller requiring a new start must branch on it.
   - **Post-launch owner-route lifecycle under SD-97:** a registered dispatch-depth-1 owner may start without route evidence and compile generation 0 after launch; the compiler attaches that immutable route to the exact owner attempt through a separate atomic lifecycle record and never rewrites the route or the launch-sealed tuple. The owner route advances only through verified generation `n -> n+1` edges adopted by a registered depth-2 child row; a childless candidate is inert.
   - **Fleet owner-lineage projection under SD-97:** Fleet projects the same verified current owner generation; two real successors or unverifiable lineage stay the typed `multiple-owner-routes` ambiguity, and timestamp ordering or "latest route" heuristics are forbidden. Display-parent resolution consumes the exact managed-session binding before classifying a missing visible thread as orphan; the collector owns that decision and rendering consumes it without changing completion delivery identity.
   - **Declared runtime requirements and lifecycle evidence under SD-97:** a node may declare only registry-known `runtime_requirements`; `loopback-listen` means localhost bind while outbound network remains denied, and a runtime that cannot express that reports `loopback-only-unsupported` and uses the checked main/inline handoff rather than widening outbound access.
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
5. **Context:** when coordination records pressure the main context, propose a session-tidy handoff card under the global continuity rule.

**SD-91 current Codex override:** the projected Codex Stop bridge silently reads
only enough payload to clear one exact interaction marker. It reads no registry,
starts no subprocess or SessionEnd work, and emits no continuation. This
supersedes older `native-Stop` and ordinary interactive park wording in this
section; those shapes are migration history only.

SD-92 completion binds to the calling `CODEX_THREAD_ID`.
The interactive launcher and gateway are retired. Native queue
submission is at-least-once; pending/history checks suppress known duplicates.
Interrupted-parent restart requires one exact pending Hearting item. Transport
refusal preserves the delivery obligation independently of child completion.
The native TUI owns subscriptions, questions, and approvals. Headless owners keep
their separate supervisor. See ADAPTATION §7.1 for the carrier contract.

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

Registered one-shot Codex `exec` workers use `--ephemeral`: their stdout JSONL,
attempt records and completion markers remain durable, while Codex does not save
a resumable session. When the existing App Server availability probe succeeds,
registered stage and review workers instead run exactly one turn on an
ephemeral App Server thread and record same-thread, same-turn numeric usage in
the exact attempt JSONL. If that pre-inference probe fails, they retain the raw
`codex exec --ephemeral --json` path. Both paths keep stdout JSONL, attempt
records, and completion markers durable; route continuation validates those
harness records rather than resuming a worker rollout. Active context comes
exclusively from observed last usage and its context window; missing or invalid
values remain unknown. The App Server owner keeps its existing supervised
live ephemeral thread across turns. This policy does not change owner
supervision or interactive sessions.

Same-host foreground recovery uses the exact harness-native terminal handoff and
the existing process receipt and completion writer. A complete Claude success
result includes the finished turn; a PASS string, a missing leader, or an
unreadable same-UID descendant is insufficient. A process born before the
recorded child cannot be its descendant, but a readable positive attempt tag
takes priority. Recovery preserves the live owner, successful siblings and original
execution evidence. Replacement launchers retain the selected foreground
watchdog and cleanup grace rather than applying the detached admission timeout;
automatic replacements also retain the route's allocation usage gate. A tap's
write time is not a new usage observation: one reset window cannot gain headroom
from a lower stale value being written again.

### §5.10a. Completion Delivery (model-visible contract)

The parent carries one field, not the carrier taxonomy: every launch receipt that
spawned a child, and every steward line that reports an armed watch, ends with
`parent_next=end-turn` (a runtime carrier owns the wake — end the turn, start no
wait, poll, re-arm, or recap) or `parent_next=bounded-wait` with the exact bounded
`parent_next_command` to run once. An absent directive is not `end-turn`; never
filter launch stdout. Completion names the next authorized action; normal success
requires no harvest. For new registered owners, the completion controller also
closes the workflow and route and finalizes the exact cycle (its completion record).
For completed official specs, that path invokes checked shared admission with
the actual seed/reference/base and existing CAS; normal completion retry recovers
interrupted admission. Missing/conflicting publication remains separately retryable
and observable; sealed PASS stays intact, with no new model execution.
The completion receipt reports shared revision identity
when admitted. Its representative is the official root/component PRD rather
than the REPORT used to prove terminal PASS. Pending closure preserves
PASS and carries a supervision notice with exact transaction recovery. Carrier mechanics — the Claude `asyncRewake` hook, the Codex native
queue and sidecar, human-gate-in-flight wakes, receipt schema, refusal classes,
and recovery — are runtime-owned and live in `core/ADAPTATION.md §7`.
Runtime completion carriers report a bounded native log line for skipped delivery,
claims and prompt admission, with the session and reason. Observing transport never
changes the settled result or grants another execution.

A replay that verifies the exact closed outcome, finalized cycle, sealed owner
handoff and quiescent children reports completed work. A missing or stale progress
ledger does not overturn those settled facts. Parent delivery facts remain
information beside the completion; unfinished settlement or publication still
reports its own obligation.

### §5.10b. Frame — Launch, Join, Interview

For code/design/draft/refine/spec at quick+, `compose --start` (and a later
`start`) launches `frame` and `frame-alternative` before the owner through the
`dispatch-owner.py` selector. The route supplies depth, type, unit, model, and
output context; the selector picks each leg's harness from live usage within the
sealed candidates, prepares or resumes the route's cycle, and passes its exact
paths and producer environment to the child. `dispatch-node --node frame` uses
the same selector, including parent delivery. The launch gate requires separate
attempts with distinct primary/alternative personas, on any supported harnesses.
Same-harness placement is normal; legacy `harness_diversity` seals describe the
supported inventory without overriding the effective SD-160 persona policy. A
`top` anchor refused for rate limits or exiting with zero artifacts gets **one**
`deep` retry, recorded as `frame_profile_degraded=top→deep`; no quota query.

Owner launch requires both current markers and user approval, standard included.
Missing legs or disagreement require a user decision. At `needs-question`
depth-0 compares the two briefs and runs the receipt's `resume_command
--interview <file>`, which raises `frame-review` with the §0.4 interview, even
at zero questions; after the person answers, `resume_command --answers <file>`
validates the answers, renders the intent, releases the gate, and starts the
owner with `Intent: <absolute path>`.

New refine starts carry complete or report scope in the existing start choice.
Report ends after review and the preview, before source snapshot or apply;
complete proceeds through the existing review and verification without asking
for a second entry approval. Older sealed quick routes retain their
`preview-disposition` release and current-preview digest checks;
`capabilities/autopilot-refine.md` owns the commands.

### §5.11. Commit and Push Policy for `<agent-home>`

After validating changes to instructions, rules, hooks, preflight, or runtime status surfaces under `<agent-home>`, commit and push them in the same turn without a separate user signal. A work repository's push is separate and remains subject to its deployment gate.

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

**Registry selection is workflow-ledger authority.** When a supervisor command
or any adapter launch fence receives an explicit `--jobs`, the ledger is always
`<jobs-parent>/workflow/<route-id>` even if the raising and releasing actors
inherit different `AGENT_WORKFLOW_ROOT` or `AGENT_DISPATCH_JOBS` values. Every
explicit registry must already be an absolute, readable, regular, non-symlink
file; an invalid authority is a typed refusal before any ledger directory is
created. Every
route-scoped JSON result reports `ledger_root`, `ledger_root_source`,
`workflow_root`, and `jobs_path`. Without explicit jobs, the legacy explicit
`AGENT_WORKFLOW_ROOT` remains authoritative, then inherited
`AGENT_DISPATCH_JOBS`, then the installed agent-home fallback; callers may no
longer mistake a silent `CREATED` result from another root for the route state.
A registered worker's `gate --block` on every harness takes its registry from
`--jobs` or, when omitted, `AGENT_DISPATCH_JOBS` as an explicit registry, and is
refused before it mutates the ledger or creates delivery when it has neither;
read-only no-jobs status/await/release compatibility remains available.

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

For the explicit `resume-run,run-verify` graph, `resource-runner start` also
arms the shared supervisor and starts its watch, without a model waiting on the
payload. The same route's normal `start` is the claimed successor: before the
resource succeeds it reports resource readiness/liveness, and afterwards it
starts only the independent verifier. The receipt distinguishes the resource,
watch and verification identities. A repeated exact launch returns the existing
run; a changed launch or another run for that route/node cannot duplicate it.
Exit failure, a missing sentinel or unverifiable identity starts no verifier.
Verification completion retains the existing parent-delivery receipt and its
supported fallback; watch startup or queue acceptance alone is not receipt.

**Managed completion resumes a parent thread once per batch.** A registered
batch's parent thread is resumed exactly one time when the whole batch is
semantically terminal and execution-quiescent, under the existing
completion-delivery contract above. When no managed completion surface is
available, the workflow uses a checked external supervisor rather than a model
sleep loop, a fixed delay, or an arbitrary detached shell that claims to finish
the work.

**A completion watch owns one unfinishable-watch budget, not an unbounded
retry.** A registered completion sidecar's own `--timeout` is its watch
deadline, not only its per-join timeout: once that deadline is reached after
a join returns no result, the watch records one `watch-deadline` supervision
notice, gives its notice courier a bounded window to claim and acknowledge
it, and exits `retryable` without ever calling `deliver` — a human or the
next launch resumes the watch. The same sidecar also gives up sooner, before
its deadline, once its own delivery gateway is provably unreachable (a hard
connection failure, not merely "not yet ready") and every monitored attempt
is already terminal, recording a `receiver-unavailable` notice instead.
Neither path deletes a completion-lock file, kills a live process, or
retries the underlying work; both leave the committed PASS and route state
exactly as found. A proven-permanent finishing block — a later attempt
already claimed the same route node, or the route's own completion marker no
longer matches its recorded evidence — is typed `blocked`, distinct from
ordinary in-flight `pending`: the watch stops re-driving settlement for that
one attempt (retrying cannot resolve it) while a distinct `closure-blocked`
notice tells a human to inspect and recover it.

**Visibility is a requirement, not a nicety.** Independently of capability,
Fleet and the status surfaces expose the workflow, its current stage, its child
resource jobs, resource class and identity, last update, next stage, and
failure reason. Fleet connects resource children by exact `parent_attempt_id`,
including existing `route`/`node` registries. A verified working resource lights
its declared route node; a supervised parked owner shows that node and
`resource-parked` instead of presenting an old model summary as current work.
Resource liveness stays independent of the owner's model activity, and a missing
progress declaration leaves only elapsed time and liveness (2026-10-07 SR eval-run).
A worker card shows its assigned work, not every node in the
route: a depth-1 frame owns only its exact frame node; the later owner owns the
execution stages, excluding the separate pre-owner frame pair. Depth alone does
not confer ownership of a pipeline. Card progress uses the same assigned scope;
the route overview retains the complete graph and its independent total.
Where a runtime cannot render a resource row directly, it shows
the supervising owner and links the child registry;
`workflow-supervisor.py status` is the portable projection that any surface may
read. An ordinary detached process is never run invisibly on the user's behalf.

---
# Governed workers and detached resources

All repo-launched model-backed workers pass through `utilities/model-worker-governor.py`, which applies a global cap, per-class caps, a per-class rolling start budget, kill switch, and witness-proven abandoned-lease recovery. The start budget is per class rather than one shared pool (per 10 minutes: `dispatch` 40, `title` 12, `loop` 4; override one class with `AGENT_MODEL_WORKER_START_BUDGET_<CLASS>`, or `dispatch` alone with the legacy `AGENT_MODEL_WORKER_START_BUDGET`), so a background class's own periodic starts cannot exhaust the budget a new dispatch launch needs. Every start is recorded in `start_records` (class, label, pid); the shared `state.json` keeps `schema_version` 2 and its legacy `starts` float list as the `dispatch` pool alone, including any unattributed float a still-sealed old release writes there, so the file stays readable by both releases across an install. Its shared state lives under the canonical artifact root so the main checkout and linked workers use one writable governor. A registered dispatch launch reserves its slots atomically before any registry row or model process is created; a parallel batch reserves its exact declared N legs in one locked operation on first start, so insufficient total/class/start-budget capacity creates zero partial rows and zero model processes. An idempotent recovery may reserve one missing leg only after all other N-1 manifest-bound rows are proven active or completed. Each reserved dispatch runner claims one opaque reservation and releases it after its command exits; parallel-group provenance survives reservation-to-claim transfer and is copied into the immutable attempt row. Unused reservations are cancelled or pruned with their exact owner PID/start identity. Governor PID and group scans preserve `inaccessible`/`incomplete` as an occupied, unreleasable state instead of pruning a lease or reservation as dead; only a complete empty group releases descendant-held capacity. Other worker classes atomically acquire their lease in the governed runner. The legacy non-consuming `check` remains diagnostic only and is never a launch authorization. A launched worker inherits the same governor root before it can dispatch a child. This does not modify runtime-owned native subagent limits. A standard+ cycle's concurrent slot occupancy is dispatch-depth-1 owner 1 plus a parallel group's 2–4 legs, so its peak is 3 at `standard` and 4–5 at `strong+`; the `dispatch` class cap of 24 keeps several concurrent standard+ cycles moving: each owner holds its own slot while its workers run, so a tight cap would leave no room for their workers. Storm protection is the rolling start budget and the usage gates, not this cap. The global cap of 30 leaves 6 slots for the non-dispatch background classes (`title`, `loop`). The per-class cap sum (24+4+2=30) equals the global cap (30): each class keeps its own ceiling, but the global cap is meant to be the real bottleneck under load, and `AGENT_MODEL_WORKER_CLASS_LIMIT_<CLASS>` overrides one class's cap when that priority balance needs to shift.

The standalone governor treats an inherited relative `AGENT_ARTIFACT_ROOT` as
a legacy hint, then resolves the canonical project root from its cwd. Primary
and linked callers share that root while absolute artifact overrides and
intentional governor overrides keep their existing meaning. The shared
artifact-root resolver retains its write-location protections.

Registered model-backed jobs stay within the dispatching workflow. Its runtime
owns admission, waiting, delivery acknowledgement, and exact process cleanup;
the model interprets results and continues the authorized work. OS detachment
changes transport rather than transferring these responsibilities. Non-model
resource jobs use the separate `resource-runner.py` lifecycle and its exact
PID/start/group, command, cwd, log, and registry identity for reattachment.


Admission recovery is bounded: only a global/class-cap refusal from `acquire` or `reserve` triggers one `reclaim(root)` transaction after the failed admission has released its state lock. Only `reclaimed_count > 0` permits one complete admission retry. Kill switches, identity/validation errors, and rolling start budgets are never bypassed; start history is not refunded. `check` and refusal messages never scan witnesses. Reclaim returns only leases whose exact witness permits an exclusive nonblocking lock; live claimants, inherited descendant handles, foreign namespaces, and unprovable legacy records stay protected. No PID-only removal, state reset, or cap increase substitutes for this proof. A start-budget or cap refusal is typed on the governor's stdout (`refusal`, `retry_after_seconds`, `frees_at`) and carried onto the adapter's refusal receipt; when the refusal created no registry row, `start --wait` treats this as `waiting-capacity`, not a failed attempt, and waits at most one start window before relaunching the same attempt id exactly once, returning without arming another wait if refused again.

Each registered dispatch attempt also owns its summary lifecycle. While the governed worker remains behind its launch fence, the selected adapter starts one non-model summary supervisor bound to the exact attempt id, log path, and worker PID/start identity; the same registry transaction publishes that owner identity before releasing the worker. The supervisor requests one early summary, at most one ordinary update per 600 seconds (off the priority lane) while the exact worker lives, and one final update after log quiescence, then exits without completion, signal, retry, or launch authority. Initial and final requests may each use one durable `(harness, session, phase)` admission ticket when the ordinary rolling refresh budget is exhausted, but never bypass the provider kill switch, per-session lock, governor, or global concurrency cap. `dispatch-reconcile --apply` may idempotently restore a missing supervisor only for one open, exact, live attempt. An extinct registered namespace-local row from a pre-receipt runtime may be removed from the active Fleet set only through `dispatch-reconcile --attempt <id> --cancel-receiptless-namespace --apply`: this exact operator action records `failure_class=cancelled`, writes no PASS, marker, or reap receipt, and deliberately leaves successor readiness fail-closed. Fleet's explicit kill path likewise closes only the selected exact attempt as a typed cancellation; its wrapper remains responsible for the genuine post-exit receipt. Fleet is otherwise a pure observer of registry and stored summary sidecars: starting, refreshing, or closing Fleet never creates provider work. Interactive sessions use their runtime lifecycle bridge as the summary producer and follow the same bounded admission rules.

Fleet's session title is the last successful Fleet summary title, retained across
refresh failures; without one the title is empty. Native runtime titles, derived
names, slugs, cwd basenames and the first prompt are not replacement titles.
Explicit user names remain valid overrides, and tags identify otherwise blank
rows. Codex's unmarked thread_name is automatic/ambiguous, so only the existing
explicit Hearting name registry overrides its Fleet title. The title names the session's subject,
while NOW names its current activity. User-owned `hearting/fleet.json`
`title_language` defaults to `auto`, using NOW's existing operator-language
selection. This changes neither provider selection nor refresh admission/cadence.

An exact operator `dispatch-reconcile --attempt <id> --apply` may also retire
a registered depth-1 owner whose launch fence remains unclaimed, with no
worker identity or log content, when its normalized sealed owner binding
names a normally closed, terminal-unproven route. The existing registry CAS
rechecks the row, binding, route and outcome bytes and rejects launch or live
attempt evidence. This is a logical cancellation only: it preserves the
original unproven outcome, creates no PASS, process-death or quiescence receipt,
signals no process, and neither cascades children nor delivers unrelated
pending attempts. Missing, changed, advanced or still-open route bindings and
launched owners retain the existing reconciliation rules. Fleet may remove
the cancelled row from its active set without granting successor readiness.

**Exact reconcile stays attempt-scoped.** A normal `dispatch-reconcile
--attempt <id> --apply` covers that attempt, its process drain, and its
related join-recovery; unrelated pending deliveries stay with the
selector-less bulk `reconcile --apply` maintenance path.

Progress is observed by the runtime from exact native tool identifiers and
completion states, scoped file changes, and bounded verification leases.
A quiet window creates a durable `no-progress` supervision notice, not a signal,
failed row, or retry. The launcher's finite execution budget retains timeout and
cleanup authority; progress recovery leaves a delayed success intact.
Per-tool bookkeeping is runtime-owned. Optional heartbeats
are work hints: test→tool is legal, and a terminal heartbeat cannot commit a
successful result. Prose, log mtime, and repeated tool events grant no progress.
Only declared sub-sessions receive the chain ledger/helper instructions.

A committed result and process cleanup are separate obligations. The runtime
join retains every owned attempt, including `done` rows, until the exact process
group and tagged descendants are settled. `dispatch-reconcile --attempt <id>
--apply` uses the same bounded cleanup proof authority on terminal rows; it
preserves the result, marker, and delivery receipt and grants no retry credit.
An exact authenticated portable cleanup receipt remains settled when a later
same-namespace tag scan cannot read an unrelated process and the current owned
group is empty; any visible live leader, group member or tagged
descendant takes precedence. Missing or mismatched proof remains pending. A
terminal frame batch whose cleanup stays unverifiable reports needs-attention
instead of promising automatic delivery from already completed workers.
For the exact route-free registered dispatch-depth-1 support tuple
`ops/session-tidy-memory` / `session-tidy-memory`, the post-exit join, reaper,
and normal reconcile consumer settle the existing semantic terminal result
after exact process quiescence. The shared log classifier's `process_exit` is
log interpretation, not a measured OS exit status or an added success condition.
The native verdict and typed evidence retain their existing meaning;
`artifact: -` is a valid handoff and does not grant
route, review, or owner completion authority. PASS is `completed-supervisor`
with `failure_class=pass`; FAIL, BLOCKED, and typed runtime, capacity, auth, or
contract results retain their existing failure note and evidence. An absent or
incomplete result stays unproven; process disappearance alone is not a semantic
result. Existing terminal history and explicit stop remain unchanged.
Missing observation keeps the cleanup obligation and its durable supervision
notice open. A later proof settles it without a new model turn or cancellation.
Historical artifact and residue seals remain audit evidence: a valid artifact
never proves that a live descendant stopped.
A residue seal is not a permanent veto: once every recorded residue process is
gone, the join recovery tick and `dispatch-registry.py reconcile` repeat the
watcher's own group and tagged-descendant observation and record the same clean
drain proof, after which the existing close path runs. An owner supervisor
started from an older release keeps its old join, so release such a row once with
`hearting run dispatch-registry reconcile --jobs <jobs.log> --attempt <attempt-id> --apply`
from the current release (it changes nothing while a residue process still
runs). The launcher/watchdog owns signals; join/reconcile own proof recovery,
bounded typed observer-error diagnostics and notification, not guessed process
death. The shared controller retains the same
batch across observation failures.

A terminal `capability-owner` node is executed by the bound depth-1 owner.
The shared terminal observer consumes that owner's exact native PASS, readable
output, declared prerequisite proofs and quiescence. It does not require a
second child or a synthetic worker marker. The terminal claim binds the same
evidence digest for closure, replay and downstream consumption. The outcome's
recorded terminal rows remain the completion snapshot when a closed cycle is
refreshed; an owner row without a worker marker is read from that verified
outcome, while worker marker absence remains unproven. A missing
prerequisite keeps the executing owner responsible before exit; after exit,
`dispatch_terminal_commit.py inspect --jobs <jobs> --attempt <id>` diagnoses the
retained obligation and `finish` retries closure without running a model.
Public start distinguishes pending closure from a running owner.

Frame diversity uses the sealed eligible candidates and exact execution results.
If every alternative harness has a settled native quota failure on this route,
the two completed attempts on the remaining harness may proceed with recorded
degradation. Generic failure, an unknown process, or another route's failure
does not establish unavailability. The accepted pair still goes through the
same user question and release gate.

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
signalling.

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

For `stage-session-chain.py`, `registered=1` is a boolean for successful
registration of the entire `registered_sessions=N` batch, not the number of
rows registered; `started=1` and `child_spawned=1` prove only the manifest's
first sub-session was launched, because the runtime supervisor owns later
chain advances.
The supervisor accepts only exact child rows whose durable launch fence records
`launch_started=1`. An empty or register-only runtime wait receives one bounded
same-session correction requiring `--start` and the three-field receipt; repeated
absence then fails closed.
The one exception is a sealed serial chain: registered-only successors behind its started frontier are the supervisor's to start, so they are neither joined nor corrected, and a chain of two or more sessions registers or starts only while its owner's supervisor lease is proven held (`subsession-chain-advance-unsupervised` or `-supervision-unproven` otherwise).

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

A live direct SSH launch may cross the process boundary without forwarding its
Claude Code, Codex, or OpenCode session variable. In that case the probe may
form a transient exact bridge only by joining a stable same-EUID local `ssh`
process's allowlisted unique session identity and established socket four-tuple
to the remote GPU ancestry's exact OpenSSH `SSH_CONNECTION` four-tuple. Both
sides are bounded and PID/start-stable. Distinct owners for one tuple,
unreadable procfs, an SSH process that owns a Unix listener (including a
detected ControlMaster), proxy/NAT rewriting, or a disconnected transport
fails closed. Connection addresses are correlation evidence only and never
enter Fleet's public process
model. This bridge exists only while the direct SSH connection is live; it does
not replace the detached-process claim below.

`claim <host> <pid> [--harness <runtime> --session <id>]` (default: the calling
session, when it is one unique harness session) is the narrow bridge for
an already detached `nohup`/`setsid` process whose runtime ancestor and session
environment no longer survive. It writes only to the shared run root's
`.process-owners.json`, never to the inventory. Creation revalidates the remote
root PID's start time, effective UID, and command-line SHA-256; every later
probe revalidates the same tuple and requires the current GPU process ancestry
to contain that exact root. A valid claim takes precedence over inherited
session environment on that ancestry, allowing a live job to change displayed
session ownership without restarting it. Conflicting valid claims remain
ambiguous. A stale/reused PID, changed command, ambiguous
claim, missing ancestry, or unreadable `/proc` remains unattributed. Cwd, PID
number alone, and transcript text are never ownership evidence.
Fleet adds a compact process name in parentheses to each GPU item under the
exact session when a live process is directly owned by a valid persistent
claim. The name comes from an explicit command/config value; ambiguous names
remain command-based. Multiple processes on one GPU share that GPU item, and
registered jobs/runs keep their existing presentation. An expired probe sample
loses its session GPU items. This read-only projection neither creates a
resource-run registry entry nor changes the process lifecycle.
An observed remote GPU process whose exact job owner is a resource child's
`parent_attempt_id` joins that child's owner GPU line, like an exact run id.
GPU processes already drawn under a dispatch owner are omitted from parent
session GPU lines in every harness; other processes and resources still
waiting to acquire a GPU remain visible.
A live GPU process that no working registered resource run (same pid and start,
or same process group on the Fleet host) and no drawn GPU line (session or
dispatch job row) shows appears once under its `project_of(cwd)` project card,
tagged `미등록` (or the owner's `job:`/`run:` label when the probe found owner
evidence), with its GPU, VRAM, and running time; a process whose cwd cannot be read goes to
`(unknown)`. The cwd only places the card and is never ownership evidence. The
row uses the same probe sample, is read-only, adds no registry entry, and
disappears with an expired sample.

Fleet may enrich a working registered resource's GPU process with read-only
training progress from its existing `run.json` and exact arm directory's
`progress.json`. The observation binds the registered wrapper and producer child by their
PID, start time, command hash and same-EUID process-group connection across the
bounded reads. The child's actual config argument and cwd go through the normal
lab config resolver; only bytes matching the arm's recorded config hash can
supply an explicit `training.attempts` denominator. Attempt, successful and
skipped counters remain separate units. A finite loss is labelled last batch
only when its attempt matches the current counter. Progress-file age remains
separate from log heartbeat age, including while validation pauses updates.
Unknown provenance, units or shapes retain the existing raw progress; no
registry repair, producer output requirement, project-specific exception or
training restart is involved. JSON and both Fleet views consume the same
identity-bound projection. A schedule epoch may be derived only when the same
verified config explicitly supplies positive integer `training.epochs`,
`blocks_per_epoch` and `updates_per_block` whose product equals `training.attempts`.
Its partial, boundary and complete states count attempted-update intervals,
not dataset passes. Structured progress and existing tqdm output share one
compact human row: Epoch, phase caption (`training-updates` as `TRAIN`), percentage and matching count fraction,
observed ETA, then original metric names. A schedule Epoch pairs with attempts
inside that interval; zero starts at zero and an exact boundary retains the
completed interval's full count. Absent or ambiguous cadence omits Epoch and
uses the overall attempt budget. Structured loss remains last batch, with scientific
notation only in the human view and full JSON precision preserved. Unit basis,
state, success/skip breakdown and progress age remain in full JSON; the primary
row uses the existing stalled-age warning rather than forcing fresh-age or
basis explanations onto it. Original phase/metric names are preserved, and
combined arm totals, ETA, speed and epoch-mean loss are not inferred.

`run` measures the selected host through the existing bounded probe before
launch, including for `--dry-run`. Its receipt and run metadata carry the
observation time, GPU free memory and utilization. When the probe observes a
GPU with zero utilization and no compute processes, it suggests the one with
the most free memory. This is a snapshot, not a reservation: the caller's host
and `--gpus` choice remain unchanged. An unavailable probe is reported as
unknown and never blocks launch; no separate pre-launch `probe` is required.

`run` starts a command detached under a stable run id and writes its log and
exit code beneath the shared run root, so the session that launched the work
may end long before it finishes and any later session on any host that mounts
that root can follow it by id through `runs`, `tail`, and `stop`. A run id
doubles as the remote `tmux` session name, and a second launch within the same
second takes a fresh directory rather than overwriting the first one's log.
Before the payload starts, the launcher removes the four allowlisted runtime
session variables and restores only one unique, validated launcher identity.
The payload's exact `HEARTING_COMPUTE_RUN_ID` is then an attribution boundary:
the process probe may use exact job, run, session, and harness evidence at or
below that boundary, but it ignores ancestry above it because a long-lived
remote shell or tmux server can retain an unrelated original `/proc` environment.
A missing or conflicting launcher identity therefore yields no session link.
Processes without this managed-run marker retain the generic all-ancestry,
ambiguity-fail-closed rule; cwd, title, transcript, and nearest-session guessing
remain forbidden. The launcher also forwards provenance the runtime actually
knows — the pinned harness release (`AGENT_HOME`, resolved through the existing
agent-home chain even when the launcher env lacks it) and, only when the
launcher itself runs inside a registered attempt, that attempt id as a
runtime-provided observation label rather than a registry authority claim — across the
local/SSH/tmux/setsid boundary, recorded in the run's `meta.json` beside the
untouched conda-selection `env` field. The selection is allowlisted:
credentials, registry paths, session variables, and arbitrary env stay on the
launcher side. Both keys are exported on every run, as the validated value or as
the empty string when the launcher cannot validate one (unknown, not fabricated),
so a stale remote-shell or tmux-server environment cannot leak a foreign value
and a payload that reads `os.environ[...]` still runs. `meta.json` also records the launcher's session when
it is one unique harness session. These values are observability provenance, not
execution permission or data protection.

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
Started from an interactive session, `run` records that session and its latest
route in `meta.json` and starts a detached completion watch: when the run's exit
code appears, the session gets one notice through its harness carrier
(`session_notice`), with the exit code and the `compute-hosts tail` command.
Started by a registered owner or worker inside a route, the notice goes to the
session that started that route's owner (its parent in the jobs registry),
naming the launching attempt.

### §5.14. Peer-Session Steering (steward role)

Registered owner corrections belong to the execution supervisor, through
`capability-route.py correct --attempt-id <id> --message-file <file>` (omit the
file to inspect). The exact attempt binds one durable input receipt; repeating
a request ID returns that receipt. Every registered dispatch-depth-1 owner —
quick and solo as well as standard+ — runs under its harness supervisor, so the
same command reaches all of them. Registration opens the input channel: a
correction sent after `--register` and before the first consumer attaches is
queued and handed to the first turn (a relaunch of a never-claimed row keeps
that queue). Codex uses its existing App Server connection to steer an active
turn; Claude and OpenCode retain the input for the next turn of the same
session. Pending input takes precedence over automatic stage advancement and
terminal closure. A transport receipt proves delivery, not implementation; an
interrupted send remains unknown (`delivery-unknown`) and is handed back through
the existing supervision notice carrier. A host whose supervisor probe does not
report support runs the owner as the existing one-shot, creates no input state,
and `correct` reports `owner-input-unsupported` before queueing; an owner
already running under an older supervisor behaves the same way.
An owner that already ended `BLOCKED`, or with a readable FAIL a person answers with an
approved fix, keeps the answer (`retained`), and `correct`
continues its route in the same call through a replacement owner that receives it
(see the SD-157 replacement rule in §5.10). When the answer comes from a session
that is not the route's parent, only the parent may launch that replacement: the
answer stays kept and the parent receives one `answer-awaiting-parent` supervision
notice. The parent's carrier that delivers it (the shared prompt sweep or the
Claude rewake) starts the route once under that session's identity
(`dispatch_supervision.continue_for_parent`) and the notice says so; only when
nothing could be started does it name the route's start command. Other ended owners still report
`owner-input-unavailable-retain-correction`. An answer sent with an older attempt id
follows the replacement lineage to the owner doing the work now.
Corrections preserve route, completion and cleanup evidence; completed-prefix
reuse uses the existing `continuation` compiler rather than a fresh recipe.

Newly compiled owner continuation budgets use one finite workload formula that
accounts for declared nodes, unique retry boundaries, the review-round cap and
terminal nodes. The separate workload floor applies only to newly derived
budgets; valid older sealed budgets remain valid. The reserved continuation is
for terminal handoff after ordinary work is exhausted and does not itself
guarantee an unfinished report node.

A steward is informally depth −1; not a dispatch depth. This role carries no launch,
gate, write, or approval authority over the session it addresses — its peer messages are
advisory context between two already-running sessions, and the append-only ledger below
is the only record of them (SD-122 §13.37.2-(1)). Six invariants govern the role:

Starting a peer honors the requested project in the launched runtime: Codex receives
`--cd`, interactive OpenCode receives its positional project, and Claude changes the
idle pane shell's cwd before the agent starts. Any cwd or PATH bootstrap waits for an
observed final visible shell prompt, including Powerline, within the existing bound
(seconds converted to native milliseconds). Readiness includes the native foreground
shell; past screen prompts are not readiness. Unknown, busy, occupied, form, and native
trust screens receive no typed input; native trust remains a human decision and is
reported as a wait reason, separate from whether the process was started. A fresh
Codex start the steward launches runs Embedded (`--no-daemon`) unless the caller
stated otherwise, so the new TUI owns its rollout fd and same-cwd simultaneous
starts stay exactly attributable; time-candidate threads stay out of Embedded
attribution (unknown until its own fd proves it), and resume/fork keep theirs.

Same-seat succession uses `peer-steward.py start <name> --kind <harness>`,
which with no `--pane` or `--beside` opens the successor to the right of the
calling pane (`HERDR_PANE_ID`) in the same tab without focus, in the Git primary
checkout (the calling cwd outside Git) unless `--cwd` names another. The new pane's
split-cwd and launcher preparation finish under one fixed
monotonic deadline, including a stable foreground-shell snapshot after bootstrap,
before its single start request; caller-provided panes keep their existing path.
Uncertain readiness leaves the new pane retained with its reuse command in the receipt.
The predecessor hands over its card and documents, receives the successor's ACK,
leaves its last result and becomes idle; the successor then uses `retire <predecessor>` to send
one normal exit action (Claude `/exit` + Enter; Codex/OpenCode Ctrl+D) and close
that pane only after its original shell returns and the recorded agent PID/start
identity is gone. Prompt helpers in that shell's foreground group do not count
as a live agent; unreadable identity remains unconfirmed. A refused start closes
its own newly split pane only when the same shell is agent-free and its visible
screen is unchanged from the stable snapshot recorded before native start.
Retirement refuses busy sessions, open forms, drafts, unknown process identity
or an unconfirmed shell return; it has no retry, forced kill or forced close.
When the retire succeeds from the pane started beside the predecessor, the
predecessor's open depth-1 routes pass to the successor, on any harness: it may
`start` and `correct` them, and their stored completion records reach it
(`retire ... handover=<n>`). Registry rows, their registered parent and worker
identities stay as they are; cards and notices are not moved.
An owner's terminal row and an acknowledged completion message do not close its
route. Its verified open route still passes to the successor until the route's
outcome is committed; an acknowledged message is never delivered again.

**S1** no execution authority — a steward never edits the target root's source, artifact,
registry, or spec, and never asks the target session to do what its own session refused
or blocked (cross-session permission laundering forbidden); a blocked action reflects
back to the steward's own user, never to the peer.

**S2** no baton — canonical state is the artifact root, the dispatch state root, and
memory. Receiving a message never changes route, gate, registry, or workflow state; the
receiver treats it as data, not an instruction, and judges it against its own card, gate,
and evidence.

**S3** no polling — watching is a checked steward watch (`utilities/peer-steward.py watch`
→ a detached watcher, separate from the session's lifetime, that waits on `herdr agent
wait` exactly once and leaves a disk receipt; no self-written sleep/poll loop), a bounded
foreground `utilities/peer-steward.py wait`, a one-shot idle-notify subscription (Claude
`notify_when_idle`, secondary), or a registered continuation supervisor/monitor.
`ListAgents` loops, "is it done yet?" messages, sleep loops, and periodic recaps are
forbidden for completion watching. Foreground launch and retirement observations
use a fixed monotonic deadline without renewing it or retrying a launch. A wait,
watch, or subscription is observation, not a §0.6 continuation; a
tracked workflow's obligation is unchanged.

**Backgrounding `wait` is not a completion-detection path.** A background
Bash `wait` lives only as long as the session's tool task. Completion
detection is `watch`: the watcher outlives the session, the receipt outlives the watcher,
and the wake is the adapter carrier's job. The registered owner attempt's `asyncRewake`
rule is a different surface and still applies only to the exact owner attempt the
registry binds to the session.

**S4** observed evidence — a claim follows §5.12's standard: PID identity, sentinel/exit
evidence, log mtime, and declared artifacts. Message text, a `ListAgents` status word, or
a bare registry status word is never evidence alone.

**S5** gate relay — a steward may summarize a user gate for its own user and carry the
response back, but never grants the approval itself; the session that received the
user's response directly owns the release record.

**S6** ledger — every message sent or received is a typed record in the `peer-messages/`
ledger under the dispatch state root. The full body is never stored, only a bounded
summary (first line, ≤200 characters) and a body digest.

Every record uses `kind ∈ {watch, steer, handoff, gate-relay, notice}`: `watch` marks an
idle-notify subscription, `steer`/`handoff`/`gate-relay` are inferred from an explicit
`[steer]`/`[handoff]`/`[gate]` first-line prefix (absent prefix defaults to `steer`), and
`notice` is the receiver's own self-authored receipt record.

Ledger path: `<dispatch-state-root>/peer-messages/<YYYY-MM>/<from_session_id>.jsonl`, one
JSON object per line, schema `peer_message_v1`: `schema_version=1`, a 16-hex `message_id`
(sha256 of `from_sid|to|ts|summary`), `ts`, `from{harness,session_id,project}`,
`to{harness,session_id|name}`, `kind`, `summary` (hard-truncated to 200 chars, never the
full message), `body_sha256` (sha256 of the complete body, never itself stored),
`delivery{surface,status,receipt}`, `refs[]`. No field carries the message body.
An undelivered transfer withheld for a form, a user draft or an unreadable input
box retains its bounded body in the private
runtime `peer-messages/pending/` directory (0700, payload/lock files 0600).
It is bound to the immutable transfer ref, actual sender/recipient and digest,
not a public ledger field. Receipt removes the private body and retains the ref
for replay deduplication; unknown, stale or ambiguous identities keep their binding.
Late redelivery sends its delay notice and the sealed message in one prompt.
The notice is transport context: receivers may remove only that recognized
prefix with the matching transfer ref before checking the unchanged body digest.
The same recipient binding and idempotent receive record apply.
Existing receiver callbacks also carry deferred messages while the recipient
is working: Claude uses hook context, Codex its native queue, and OpenCode its
native context API. They never type into a form or user draft. Hook context
emission is queued delivery, not confirmed receipt; it retains the private body
until the ordinary exact-ref receive path observes it. Existing callbacks warn
the original sender once when a transfer remains without confirmed receipt for an
hour. The notice attempt is claimed before output; ambiguous output is retained
without automatic repeat. No additional watcher, model turn, approval or
operator input is required.

Peer-only native history reads keep the exact client ID and body checks while
reducing oversized pages. A summary miss is not absence: the checked turn range
includes complete item inspection, using bounded item pagination for a large turn.
Unavailable, malformed or incomplete history still permits no enqueue. This does
not change the completion courier's existing at-least-once defaults or frame cap.
OpenCode self-publication uses the normal callback SID and an exact top-level
Session returned by its SDK. Wrapped and data-style SDK responses share that
validation; timeout/error diagnostics and retries occur only on normal callbacks.
OpenCode cold bootstrap binds this native process's pure explicit-session TUI
invocation to the same SDK-verified root in its directory. Continue (including
mixed explicit-session/continue), fork, ambiguous selectors and foreign roots are
ineligible origins. One real startup event allows one created publisher child;
all later refreshes use ordinary increasing sequences, with startup replay and
synthetic new-session events excluded. Origin state, shared sequence and the
actual-child slot survive constructor recreation; disposal retires old SDK results
and preserves spent/invalidated origins. Normal callbacks coalesce without waiting
for a live publisher; actual exit frees its slot, observation timeout/pipe closure
does not. Initial selection proves neither continuous UI selection nor a peer
recipient. Unsupported origins and unknown publication remain unverified without
a manual report or another user step.
Existing host logs separate plugin/callback entry, SDK validation and the publisher's
guard/CLI outcomes with bounded metadata only. A returned publisher command, including
exit 0, is an attempt observation; it does not prove identity publication or receipt.
Neither environment/pane labels nor a queued/persisted message prove receipt.

**Runtime table** (herdr-unified). Native peer messaging is not extended to Codex; the common
control lever across Claude and Codex depth-0 sessions is the herdr agent API (`herdr
agent start|prompt|read|wait`), because `ListAgents` never surfaces a Codex session
(`SendMessage` cannot reach it) and herdr panes are not raw tmux panes:

| Runtime | Watched side (wait target) | Steward side (wake) | Send (secondary) | Receive | Ledger | Status |
|---|---|---|---|---|---|---|
| Claude | `herdr agent wait <target>` — measured | detached `watch` + `PostToolUse(Bash)` `asyncRewake` hook `peer-steward-rewake.py` (exit 2 wakes) — **spec-only** until a live-session measurement; backgrounding `wait` is not a completion path. Hook death or session restart falls back to the next prompt's un-acked-receipt sweep | `peer-steward.py prompt` (herdr, F-100c primary) · native `SendMessage` — reaches inline mid-turn (measured), lost to approval wait/expiry when idle (measured) → secondary | `<cross-session-message>` injection · herdr prompt with the `(peer-from: …)` trailer | `PostToolUse(SendMessage)` hook + `UserPromptSubmit` hook (`notice` for both envelopes; un-acked watch receipts swept) + `peer-steward.py` record | watched measured · send/receive measured · steward wake spec-only |
| Codex | `herdr agent wait <target>` — measured (idle matches immediately); `--until working/idle --timeout` can time out after completion — read the pane instead | detached `watch` + next-turn receipt pull; no wake carrier — unknown (no capability claim before measurement, P-7) | `peer-steward.py prompt` (herdr, F-100c) — native path retired (`codex queue` / gateway `steer` op not implemented) | herdr prompt with the `(peer-from: …)` trailer → `userprompt-lifecycle.py` writes the `notice` | `peer-steward.py` record + Codex hook `notice` | send/receive measured / steward wake unknown |
| OpenCode | `herdr agent wait <target>` — unknown (pending P-6) | unknown | `peer-steward.py prompt` (herdr, F-100c) | herdr prompt with the `(peer-from: …)` trailer → `hearting-guards.js` `chat.message` writes the `notice` | `peer-steward.py` record + plugin `notice` | send measured / receive pending live measurement / steward unknown |

Identity on the ledger (F-100c): every steward record carries `to.session_id` resolved
through `herdr agent get` (Claude UUID, Codex thread id; herdr reports no id for
OpenCode) and `from.name` (the sender's registry name, Claude
only today). Fleet joins sent/recv counts and the `← <name> · kind · age` subtitle on the
exact session id, so a child row shows its steward on every harness; the pane pid probe
(`herdr pane process-info`) is what places an OpenCode session in its herdr pane. The
steward marker under `<dispatch-state-root>/peer-steward/` is a **role** flag, not a
side effect of talking: it is raised only by `peer-steward.py
steward on` (`source=explicit`), by `peer-steward.py wait`/`watch` once herdr answered
about a real target (`source=watch`; a mistyped target leaves no flag), or by a
`peer-steward.py start` that launched a session (`source=start`). No `record` path and
no send raises it — not a steer/handoff/gate-relay and not a `SendMessage` with `notify_when_idle`, which the
Claude hook records as `kind=watch`. A session taking the role runs `peer-steward.py
steward on` once. Fleet renders an evidenced marker
as the bold-yellow tag badge and ignores one whose entries carry no such source;
`peer-message prune-steward-markers [--apply]` lists/removes those, and `peer-steward.py steward off` (or `peer-message release`)
clears a marker.

**Prompt submission is verified, never assumed.** `peer-steward.py prompt`
prints `prompted=true` only after the submission was observed, and every other verdict is typed:
`failed` (exit 1), `queued` (exit 3, our text still sits in the target's input), `unverified`
(exit 5, nothing could be observed) — never `true` from herdr's exit code alone. Before any send the
visible pane is scanned for a selection/permission form (AskUserQuestion, permission prompt) in
every state. A `blocked` target, an open form, a user draft or an unreadable input
box receives no keyboard input. Claude/OpenCode input is read again immediately
before submission; Codex's existing native queue branch does not type into the box. Its
undelivered message is retained as a bounded private runtime payload tied to the
existing immutable transfer ref, sender, exact recipient session and body digest;
the public ledger continues to store only digest/summary. A still-pending retry
of that same sender/recipient/body reuses the ref. A fork, changed SID or different
body has a separate transfer. Queue acceptance is `queued`, ambiguous transport is
`unverified`, and neither is delivery or consumption. Only exact native history
or the existing receiver's observation of the bound ref acknowledges receipt.
Normal idle/receive callbacks and the next steward prompt retry unsent rows only
when the input box is readable and empty, preserving the exact recipient and claim.
Session-tidy's unsent continue stays in its existing booking until that same idle
callback, and is cancelled by newer user input or a changed card as before.
Callback stdout/system context alone is not an ack.
An unavailable transport or callback without an actual ack keeps the payload
pending and reports that limitation, without a watcher, forced key or new gate
(text typed into an open form would be lost and the Enter would answer the form). A target that is not working is sent with `herdr agent prompt --wait --until working`
(bound clamped to herdr's 5000 ms stall floor) and counts only once its state flips (`agent_prompt_stalled` →
`failed reason=agent-prompt-stalled`). A working target, or a herdr `timeout`, is settled by
`_verify_after_send`: the target transcript first (`verify=transcript-arrival`, Claude only),
then the prompt box — but only when `herdr agent explain` actually read it
(`region=prompt_box_body`; a working Claude pane is explained by its terminal title and an
OpenCode pane by `rule: none`, neither is a box read → `verify=prompt-box-unavailable`,
`unverified`); our own first line still in a read box is `queued`. Verification
never retries Enter: that could submit a user's newer draft. Already accepted or
ambiguous submissions are not resent.
A dim `❯ …` line in an *empty* box is Claude Code's prompt suggestion, not unsubmitted text;
the target transcript settles it. `--no-verify`
keeps the legacy exit-code report; `--wait-idle-ms N` defers a send to a working/blocked target.
**Every pane prompt goes through this wrapper.** herdr's own server log records `cli:agent:prompt` with
no target pane and no caller, so the wrapper writes one ledger row per send whatever the outcome: `to.pane`
(the resolved herdr pane), `to.session_id`, `from.session_id`, `ts`, `body_sha256`, and the verdict as
`delivery.receipt` (`prompted=… state_before=… verify=… herdr_rc=… ms=… reason=…`). Harness code and a
session's own Bash never call `herdr agent prompt`, `herdr pane send-text`, `send-keys` or `pane run`
directly (`peer_steward.test.py` asserts it).

Same-behavior guarantees are still not claimed: the watched-side herdr realization is
measured for both Claude and Codex, and **no runtime's steward-side wake is measured** —
the Claude carrier becomes `measured` only after a live-session capture, not before. The
memory handoff channel is pull-only (next prompt's `UserPromptSubmit` candidate or `mem
recall`), so it is not a wake path either. Gateway `steer`/`watch-idle` ops are **not
implemented**. The primary completion-report path is the
detached watch receipt plus the adapter wake (the watching side reads screen/disk
directly); `SendMessage` is secondary.

**Detached watch (model-visible).** `peer-steward.py watch <target>` returns one typed
`state=armed` line carrying the same `parent_next` directive a launch receipt does;
`join`/`status`/`rearm`/`ack` read the watcher's disk receipt, and every line that is
not a hook-armed watch (`wake=none`, a dedupe hit, a session-printed `rearm`) prints
`bounded-wait` with a bounded `join <watch_id>`. Watcher, lock, receipt, and the Claude
wake hook are runtime-owned (`core/ADAPTATION.md §7.3`).

Realization (Claude, measured): sending `SendMessage` fires; `PostToolUse(SendMessage)`
writes one `peer_message_v1` record before the send completes; it lands in the ledger,
one JSONL file per sender per month; the receiver's `UserPromptSubmit` hook writes its
own `notice` record by its own verifiable session id; a read-only Fleet collector
projects the ledger into per-session sent/recv counts and a bounded last-received
summary, never scanning the ledger from a write path; the projection renders as an
additive session badge/subtitle, never widening or reflowing an existing row.

Peer-message holds and expiries are permission-mode effects on **both** ends:
`SendMessage` is a tool call gated by the sender's permission mode, so an unattended prompting-mode
sender expires; and a receiving session that is not in bypass mode holds an inbound peer message for
its own user's approval before its Claude sees it. A running session cannot be raised to bypass (the
shift+tab cycle has no bypass step), so the launch-time flag is the only deterministic point —
`peer-steward.py start` applies it. A stewarding session dispatches registered work from its own
session by default; an interactive child session is reserved for long-running work that needs human
answers or recovery.

Probes P-1 through P-5 (Codex `queue` reachability, managed-gateway reachability, gateway
`steer` single-ingress, idle republish, OpenCode message queue) are closed by decision:
those capabilities are not implemented. The two probes that remain — P-6 (does herdr detect an
OpenCode agent and does `herdr agent wait` return for it) and P-7 (does a Codex steward
actually recover a detached-watch receipt on its next turn, with no wake carrier) — land their
receipts at `spec/stage-dispatch/_internal/research/peer-session-probe/P-<n>.md`; no
adapter claims a working Codex-steward or OpenCode realization until its receipt exists.
