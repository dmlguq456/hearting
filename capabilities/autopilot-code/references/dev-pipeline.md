## Pipeline: Mode dev

Select the stage graph from `--intensity` before QA. `direct` performs produce plus sanity/report without this durable pipeline. `quick` uses one registered-headless dispatch-depth-1 one-shot conductor with an inline micro-plan, plan-check-lite, and focused verification. standard+ follows the durable pipeline below.

The compiled standard+ route always carries the `plan-check` review node; select its unit by risk axis: UI or visual risk → the `design/critic` unit; research or domain risk → the `research/plan-review` unit; construction quality (the compiled default) → the `qa/plan-review` unit. Each runs as a sibling review node dispatched by the owner per the compiled route.

### Standard+ Stage Dispatch

The dispatch-depth-1 owner is a thin conductor. The approved pre-execution gate must already
have compiled an immutable standard+ route file with checked headless evidence. If that route
file or the canonical jobs path is unavailable, stop instead of launching an unbound row.

#### Sealing the checked evidence (before the owner exists)

Create the final isolated source worktree before collecting any standard+ route evidence.
Run every probe against that exact absolute path; each tuple records it as
`checked_worktree`, and the route compiler requires it to equal the route `cwd`. Evidence
from the primary checkout, a staging worktree, or a worktree that was later replaced is not
reusable for the final route.

Each tuple answers "may the parent of a dispatch-depth-2 node spawn that child?" Use the
readiness generator so the caller never has to reproduce the prospective-owner context or
assemble the evidence schema by hand.

Automatic `compose` and post-frame proposal assembly inspect every enabled prospective owner
against the selected child pool and seal each actual owner/worktree probe. Keep a manual
`--owner-harness` invocation scoped to diagnosing that exact target; automatic selection uses
the sealed evidence and the existing allocation policy.

```bash
python3 "$AGENT_HOME/utilities/dispatch-readiness.py" \
  --worktree "$WORKTREE" \
  --owner-harness "$OWNER_HARNESS" \
  --child-harness claude --child-harness codex \
  --output "$DISPATCH_EVIDENCE"
```

Every tuple also carries `failure_scope`, `codex_command`, and
`retry_on_isolated_worktree`. A Codex worktree-local collision such as a non-directory
`.codex` reports `failure_scope=exact-worktree`, `codex_command=ok`, and
`retry_on_isolated_worktree=1`. That result requires a clean isolated worktree and a fresh
probe; it must not remove Codex from global eligibility, select a Claude/native/inline
fallback, mutate the user's `.codex`, or weaken the sandbox. Only a
`failure_scope=runtime-global` result can make the runtime globally unavailable.

`--parent-transport` and `--parent-sandbox` default to `auto`. A dispatch-depth-2 tuple's parent is
always a registered-headless owner, so passing your own `interactive` transport is rejected at
the probe, again at `capability-route.py compile`, and again at launch. `--parent-harness` must
be the adapter the owner will actually run as; bind that decision by passing the compiled route
to the launch, `dispatch-owner --start --route-evidence <route_file> --prompt-file <brief>` (the
route fills the rest), so the adapter cascade cannot select a harness the tuples never probed. Getting any of this wrong fails route
compilation or owner selection before material work, never a silent inline run.
For a Codex owner the generator automatically applies the prospective standard-owner network
and exact registry check. A raw depth-0 `nested-headless` call without that context reports
`prospective-owner-check-required`; it is not evidence that nested networking is unavailable.
Once the Codex owner is running, its internal probe instead requires the launcher's actual
network marker.
Dispatch every durable node through `utilities/stage-dispatch-fallback.py`; it binds the
route identity, node, write scope, completion gate, exact fallback tuple, and current attempt
axes, and picks the stage's harness from the sealed pins and live usage. Route, slug, parent
and registry come from this owner's current route and environment:

```bash
STAGE_OUTPUT=$(python3 "$AGENT_HOME/utilities/stage-dispatch-fallback.py" \
  --node "$NODE_ID" --start --prompt-file "$STAGE_PROMPT_FILE")
printf '%s\n' "$STAGE_OUTPUT"
ATTEMPT_ID=$(printf '%s\n' "$STAGE_OUTPUT" | sed -n 's/^attempt_id=//p' | tail -1)
test -n "$ATTEMPT_ID"
```

A member of a sealed 2–4-node `parallel_group` starts its whole group: the same command
runs one atomic `dispatch-batch.py` call, whose bounded JSON receipt returns every stable
attempt ID. The batch directly is the same call:

```bash
BATCH_OUTPUT=$(python3 "$AGENT_HOME/utilities/dispatch-batch.py" \
  --parallel-group "$PARALLEL_GROUP" --start --prompt-text "$(cat "$STAGE_PROMPT_FILE")")
printf '%s\n' "$BATCH_OUTPUT"
```

Separate member starts are not a parallel batch: admission and wrapper starts would no
longer be one transaction, so a member-level `register` fails closed. The batch seals the complete N-way group,
exact parent generation, profile, perspective, leg index, and required/realized independence
axes. On an idempotent repeat it consumes no capacity for exact active/completed legs. A
single missing-leg recovery proves every other N-1 member; any larger partial launch is denied.

Keep `ROUTE_FILE`, `NODE_ID`, and the captured `ATTEMPT_ID` together until that node's
completion transaction succeeds. Dispatch tools inherit the immutable canonical registry
through `AGENT_DISPATCH_JOBS`; do not reconstruct or cache a second registry path. Never
dispatch standard+ with a raw wrapper command that omits the route record.

The runtime adds the node's declared scope and the absolute paths of inputs compose took from an earlier cycle to every stage assignment, with or without `--prompt-file`; a prompt file carries only subskill name, input paths, output contract, intensity, and slug. It never carries plan bodies or prior-stage conversation. Each stage reads files; the conductor reads only verdict and gate state. Register every stage in the sealed registry; the model-worker governor, not the conductor, bounds how many run at once. Runtime-owned completion observes exact liveness outside the model; the conductor never adds a recurring monitor. One-line or no-artifact micro-stages stay inline.

#### Runtime-Owned Batch Join

After registering every separable child in the current batch, end the turn with
`runtime_wait: registered-children`. The adapter supervisor waits outside the model
until every exact `parent_attempt_id` child is closed or ready for typed harvest, then
resumes the same Codex thread or Claude session once with a bounded receipt. Do not
call liveness, inspect raw child output, or do parallel work while parked.

Obey the launch receipt: `parent_next=end-turn` yields; `parent_next=bounded-wait`
runs the printed `parent_next_command` once.

After judging a stage's artifact contract complete, publish its captured exact-attempt completion before
dispatching the next stage. The completion transaction writes the immutable marker/link and
closes only that attempt row; never mark a routed row `done` first:

```bash
python3 <agent-home>/utilities/capability-route.py complete \
  --route <route-file> --node <node-id> --evidence <stage terminal artifact> \
  --jobs <canonical-jobs.log> --attempt-id <exact-attempt-id>
```

A stage worker completing its own node may omit `--jobs` and `--attempt-id`: `complete` reads
`AGENT_DISPATCH_JOBS` and `AGENT_DISPATCH_ATTEMPT_ID` when that row is this node's attempt.

The marker lands at `<agent-home>/.dispatch/completion/<route_id>/<node_id>.json`. Evidence is
the stage's contractual terminal artifact (`plan.md`, the final dev log, the test verdict,
`final_report.md`). The pass judgement stays semantic and belongs to the conductor; the marker
only makes its *result* deterministic. A record-bound `--start` for a node whose `depends_on`
markers are absent fails closed with `completion-marker-missing` and spawns nothing. Marker
absence is *no claim*, never a failure.

#### Closed Inline Fallback

Run standard+ stages in-session only when:

1. it is a micro-stage inside the checked quick one-shot owner;
2. every route-sealed registered-headless candidate has a typed hard-unavailable result and the compiled fallback policy reaches inline; or
3. the conductor judges the edit nonseparable because file artifacts cannot carry a boundary-coupled semantic contract.

Native-subagent prohibition is not a headless failure. An exact-worktree retry result is a
re-isolate/re-probe stop and never reaches this list. Neither does a route-state refusal
(`completion-marker-missing`, `completion-evidence-revised-unrecorded`, `review-round-budget-exhausted`,
`review-verdictless-bound`, `round-unsettled` — `dispatch_contract.ROUTE_STATE_REFUSAL_REASONS`): the
fallback stops with `child_spawned=0` and reports the reason's own `next_action` (SD-154/defect #2)
instead of degrading to an inline run of a node whose own recorded state, not runtime availability, is
what is blocking it — most often `capability-route.py revise --route <route> --node <node> --evidence
<evidence-path> --basis review-findings|user-direction|owner-correction` when the gate's evidence
changed after its marker published (opt-in gates only; with gates off, the default, the edit is
kept as history and the node proceeds). For case 3, record reasoning in the
selected node's declared output; an unrecorded inline standard+ run violates the contract.
Still parallelize separable census or disjoint file groups. The Claude stage-dispatch hook
only reminds you of this list; it does not deny the in-session run.

**An inline stage still owes its marker, and the same command writes it.** A stage run
in-session has no registry row, so the `--jobs --attempt-id` recipe above cannot apply — but
`complete` does not require one. State the axes the run actually had instead:

```bash
python3 <agent-home>/utilities/capability-route.py complete \
  --route <route-file> --node <node-id> --evidence <stage terminal artifact> \
  --attempt-id <stable id for this inline run> \
  --dispatch-depth <the node's own dispatch_depth> \
  --transport headless --execution-surface inline \
  --registered-worker 0 --fallback-hop inline
```

No `--jobs`. The marker this publishes is current and opens the dependency gate for the next
stage exactly like a dispatched one; `registered_worker=0` is what tells the readiness check
there is no process to verify. Skipping this is the single most expensive mistake available
here: the next stage is refused for the missing dependency, its fallback descends to inline,
that run publishes no marker either, and **one marker-less stage forces the whole remainder of
the route inline** — measured on route `rt-b2d68cbf14d31c62`, eight nodes and zero markers.

`--execution-surface inline` with `dispatch_depth > 0` requires `--fallback-hop inline`; the
contract refuses the mismatched combination rather than recording a surface the run did not have.

#### A Review Node Says Who Reviewed

A `review-worker` node's completion has one extra obligation: name the reviewer.
The marker records `reviewer_kind`, `review_independence`, and the reviewer's
identity, and none of it is taken on the caller's word.

When a *different* registered attempt produced the verdict, name it — the row is
looked up in `--jobs` and must carry `worker_type=review`:

```bash
python3 <agent-home>/utilities/capability-route.py complete \
  --route <route-file> --node <review-node-id> --evidence <review artifact> \
  --jobs <canonical-jobs.log> --attempt-id <the completing attempt> \
  --reviewer-attempt <the review worker's attempt id>
```

When a native subagent reviewed, name its transcript instead. A subagent with a
recorded identity counts as independent review; the digest is what keeps that
claim checkable later:

```bash
python3 <agent-home>/utilities/capability-route.py complete \
  --route <route-file> --node <review-node-id> --evidence <review artifact> \
  --jobs <canonical-jobs.log> --attempt-id <the completing attempt> \
  --reviewer-subagent <transcript path>
```

A named reviewer attempt must have reviewed *this* gate, not merely hold the
job title: the row must not be a sub-session slice, must — if it is bound to a
route at all — be bound to this route and node, and must have reached a terminal
verdict (`done`, and not a `dead-*` note; `completed-review-blocking` counts,
because a FAIL is a produced verdict). An ad-hoc SD-OPEN-40 reviewer carries no
route binding and stays admissible.

Name nothing and the completing attempt is the reviewer, which is independent
only when its own registry row says `worker_type=review`. Everything else — no
claim on an inline completion, a claimed row that is absent, foreign, a
sub-session, not a review worker, or never produced a verdict; an unreadable
transcript; no `--jobs` to adjudicate with — **is recorded as
`reviewer_kind=owner-inline`, `review_independence=degraded`, with a typed
`reviewer_downgrade_reason`. It is never refused.** The node completes, the next
one proceeds, and the degradation travels: `complete` prints
`completed-review-degraded` on stderr, the row carries the same axes, the closed
outcome lists the node under `review_independence_degraded`, and the §0.5
completion card has to say that gate was not independently reviewed.

The SD-94 owner-closure path — the owner ruling over a review that returned
FAIL — is recorded `review_independence=owner-overridden` with
`review_gate_closure=owner-closure`, and is listed alongside the degraded ones.
It is the most literal case of "reviewed" meaning "the owner decided", and the
row it closes genuinely belongs to a review worker, so without this it would
have read as independent.

Naming a reviewer *after* the node completed is a no-op: provenance is not part
of marker identity, so the call replays the existing marker and prints
`reviewer-claim-ignored-on-replay` rather than recording the late claim.

To make the degraded path an exception rather than the norm, launch the reviewer
as a registered review worker instead of an owner:

```bash
python3 <agent-home>/utilities/dispatch-owner.py --adapter <harness> --start \
  --worktree <worktree> --slug <slug> \
  --capability autopilot-code --capability-mode debug \
  --intensity standard --dispatch-depth 1 --worker-type review \
  --unit qa/code-review --model-role qa/code-review \
  --assigned-contract autopilot-code --owner <slug> --model-profile deep \
  --prompt-file <review brief>
```

`--unit` is required for a review tuple and may not be a `_kernel/*` unit;
`--route-evidence` is refused, because a review node that belongs to a route is
launched by stage dispatch with its node binding.

#### Usage-Aware Cross-Harness Routing

Before dispatch, run `sh <agent-home>/utilities/usage-check.sh`. It reports per-harness `ok`, `limited(<reset>)`, or `unknown`; `ok` means no known block, not guaranteed capacity. Avoid limited runtimes. An automatic/model-selected recovery also requires known positive capacity; unknown capacity is not positive availability. A user's explicit `--adapter` override may retain its separately audited unknown-capacity path when route evidence and hard eligibility still permit it. Otherwise default to the free cross-harness posture (OPERATIONS §5.10 SD-16, 2026-07-24): with two or more eligible harnesses, spread consecutive stage nodes across model families rather than homing to the conductor's harness, always place test or review on a different family than implementation, and record a task-fit or limit reason when a run intentionally keeps every node on one harness. Preserve `dispatch_depth`, `parent`, `worker_type`, `assigned_contract`, `model_role`, `harness`, `owner_harness`, and `parent_sid` metadata across runtimes.

If the user disables a harness, do not run its auth, capacity, or headless probe. Emit that child tuple through `nested-dispatch-eligibility.py --user-disabled`; the sealed `unsupported/user-disabled` evidence excludes it from automatic recovery and explicit owner selection.

If a stage dies immediately from usage, session, or authentication limits, the wrapper closes its row as `done,note=dead-<reason>` and records reset time when known. The wrapper does not retry; the conductor decides redispatch or cross-harness failover.

### Optional Material Delegation

When implementation or reporting requires result plots, experiment-log visualization, or result tables, the code-execute or code-report worker records the need in its artifact. The enumerated autopilot-code recipe compiles no `material/*` node, so the owner satisfies the need per the WORKFLOW compose-on-demand doctrine (§0.2.1, `capability-route.py compose`; an autopilot-code graph carries no `material/*` stage id, so a material node is composed through `utilities/compose-route.py` with an explicit unit): a composed route extension node bound to the matching `material/*` unit (e.g. `material/figure-gen`, `material/data-script`) that passes the same validator and hash-seal as a recipe route — or, for narrow throwaway scaffolding only, an ephemeral native helper with no unit semantics. Experiment setup, training, and evaluation belong to `autopilot-lab` (`core/WORKFLOW.md §0.2`); a code route that needs a training or benchmark run inside its own cycle borrows `autopilot-lab:smoke` and `autopilot-lab:full-run` in `--graph` (the run is written under `parts/autopilot-lab/<stage>/` and keeps the resource runner's attestation, provenance, governor, and `supervised` conditions). The material units own postprocessing. Record generated asset paths in the relevant dev log.

### Higher-Intensity Perspective Extensions

Registry-v6 widens only selected leverage points: `strong` uses 2-way plan and
implementation-review groups; `thorough|adversarial` widen plan plus implementation
review to three legs. Frame is not part of this widening — every intensity from
`quick` up gets exactly two frame legs, fixed (see Step 0). Both run `top` with distinct
perspectives. Any additional security/material/specialist
node still needs a validated compose-on-demand extension with disjoint output and a
completion gate; never create an undeclared child or alter a sealed group width.

### Step 0: frame (already done, by depth-0, before owner launch)

Skip only for `direct` (orient-lite carries the framing posture inline). For `quick`
and every `standard+` intensity, the direction gate already ran (owner-execution.md
"Pre-Owner Direction Gate"): depth-0 itself launched exactly two frame legs — `frame`
and `frame-alternative`, both `top` (a pin with a profile or an explicit profile wins; a
route made from a proposal via `--route-plan`, or as a `solo` or `staged` compose, has no frame nodes) — as its own
bootstrap layer, after route binding and producer begin but before owner launch. There is no third frame leg
at any intensity; `strong+` widening applies only to plan and implementation review
(above), never to frame. See `core/WORKFLOW.md` for the full depth-0 procedure.

Depth-0 joined both legs, interviewed the user, released `frame-review`, and rendered
`shards/frame/intent.md` before launching you. You receive that path as an input and
pass it to `plan` as `Intent:`; you raise and await nothing for this gate. `plan`
cannot start on an unreleased gate — every launch surface refuses it.

### Step 1: code-plan

Skip for direct. quick uses an inline micro-plan. For standard+, first verify the SD-13 precondition: the repository has an artifact root and `spec/`. Then dispatch:

```bash
AGENT_HOME=$(utilities/agent-home.sh)
NODE_ID=plan
STAGE_PROMPT_FILE=<file holding: sub-skill contract + absolute input paths (Intent: shards/frame/intent.md first, then every direction brief) + output contract>
# Run the route-bound dispatch transaction from "Standard+ Stage Dispatch",
# capture ATTEMPT_ID, then use that same value for `capability-route.py complete`.
```

On the normal supervised path, yield `runtime_wait: registered-children` and consume
the typed receipt on resume; a `parent_next=bounded-wait` receipt runs its printed
command once.

After the typed receipt (or fallback exit 0), read only plan status and paths. A terminal recovery receipt permits checked exact-attempt diagnosis; raw transcript inspection still requires a terminal/closed row or explicit operator recovery. Direct and the quick one-shot owner keep their declared inline plan; standard+ invokes `code-plan` in-session only after the compiled fallback policy reaches inline under the closed rules above.

At `strong` the compiled route contains `plan` (`deep`) and `plan-alternative`
(`balanced-deep`). At `thorough|adversarial` it also contains the light
`plan-implementation-risk` scout. Every leg reads all direction briefs and writes
its suffix-isolated plan/checklist. Dispatch the sealed group through one
`dispatch-batch --parallel-group plan` call. No leg is the final plan until Step 2
arbitrates.

### Step 2: plan-check and Optional Refinement

Only durable standard+ graphs use this step. direct has none; quick already completed plan-check-lite.

The compiled standard+ route contains the `plan-check` review node unconditionally (default unit `qa/plan-review`, completion gate `code-plan-check`); `execute` is record-bound to its completion marker and cannot start without it. Intensity scales the review's depth and reviewer role/family, never whether the node runs.

At `strong` and above `plan-check` is additionally the plan arbiter: it is
record-bound to every declared plan leg, reads their suffix-isolated plan/checklist
artifacts, and its memo names the winning leg plus any grafts worth taking from the
others. When a non-anchor leg wins or a graft is required, materialize the final `plan.md` through the existing bounded
`code-refine` path before `execute` — `plan-check` itself stays read-only, and
`execute` consumes `plan.md` only.

1. Resolve `en_plan_path`, `ko_plan_path`, and `log_dir`.
2. Run the route-bound dispatch transaction with `NODE_ID=plan-check`: the node reads `plan.md` and `checklist.md` and writes `_internal/plan_reviews/round_1.md`. Poll, then publish exact completion from the review memo.
3. **At `thorough` and above, arbitrate the `plan-check` group after the join.** The
   compiled route carries auxiliary check legs beside `plan-check`, and `execute`
   cannot start until someone has been recorded as having read them. Do this only
   once every leg of the group holds its completion marker — the transaction is
   refused as `auxiliary-arbitration-before-join` while any sibling is still open,
   which is what makes it impossible to satisfy from inside a concurrent leg.
   1. Put `auxiliary_findings_considered` in the merge memo's frontmatter, with
      **exactly one entry per realized auxiliary leg** (`adopted`, `rejected`, or
      the equally short word that says what the merge did with that leg's
      findings). A wrong count is refused with the count it wanted.
   2. Register it:
      ```bash
      python3 "$AGENT_HOME/utilities/capability-route.py" arbitrate \
        --route "$ROUTE_FILE" --group plan-check --evidence <merge-memo path>
      ```
   The receipt names the resolved arbiter. `auxiliary-arbiter-is-node:<node>` means
   this group's arbiter is a route node, not the owner: that node records
   `auxiliary_findings_considered` in its own completion evidence instead, and its
   dispatch prompt must name the group it arbitrates and how many auxiliary legs
   were realized. Skipping this step does not fail here — it fails later, when
   `execute` refuses to start with `auxiliary-arbitration-missing`.
4. Read only the memo verdict. If it reports blocking findings, pause when `--user-refine` is set; otherwise run one `code-refine` within the correction budget.
5. On a clean memo (or after the bounded refinement), continue to Step 3.

### Step 3: code-execute

For standard+, run the route-bound dispatch transaction with `NODE_ID=execute`; its route node
selects `assigned_contract=code-execute` and the portable implementer role. Pass the absolute
`plan.md` path, retain the emitted attempt ID, yield for the typed receipt, and publish exact completion. Fallback
to in-session only under the closed rules above.

**Subdivision check (SD-103) — perform it explicitly before the single-session dispatch.**
`execute` is the one node that carries a sealed `subdivision` permission
(`min_intensity: standard`, `max_slices: 4`, `disjointness: exact-fixed-files`). The firing
condition is the plan's own declaration: when `plan.md` carries one `slices` block (2–4
packages with exactly disjoint files, each able to finish independently), do not default to
one long serial session. Run one command:
`python3 "$AGENT_HOME/utilities/dispatch-batch.py" --parallel-group execute --slices <plan.md> --start`.
It reads the block, mints the chain and slice ids, writes one phase brief per slice, proves
exact files, worktree and write-scope containment and pairwise disjointness with the same
`load_manifest` the admission re-proves, reserves the slots, and registers and starts the
slices. Slices run in the route cwd or, while the same-work gates are off, in a linked
worktree of the same repository, and each binds to this owner by the inherited attempt id.
Close the single stage gate with the receipt's `next_command`
(`capability-route.py complete --route <route-file> --node execute --evidence <stage
evidence> --jobs <registry> --subsession-manifest <chain_manifest>`). Slices are no-commit
workers; close the gate first, commit after. A `single-session-required` receipt (no
`slices` block, a malformed or duplicated block, no inherited owner attempt id, or
disjointness not proven) means "run this stage as one ordinary session": proceed with the
receipt's `next_action` and record the reason; it is not a failure and no slot, row or
child was created. A slice that declares a non-worktree `base` is still refused
(`scope-unproven`, SD-119 R5 unlanded). When the plan's packages share files, subdivision is
unavailable by contract — a declared serial sub-session split under the same route node is
then the sanctioned way to bound session length. Record which of the three shapes (parallel
slices / serial split / single session) was chosen and why in the dev log. The lower-level
`stage-session-chain.py plan-slices` and `--subdivision-manifest` surfaces still work.
`core/OPERATIONS.md §5.10` owns the manifest, baseline, and refusal vocabulary.

Read plan frontmatter after harvest:

- `done` → impl-review, then Step 4;
- `partial` → impl-review, then Step 4 for completed work;
- `failed` → execute did not complete successfully; recovery may be partial when some edits were ambiguous. Write a failed `pipeline_summary.md` with the remaining state and stop before test or report.

The compiled route also contains the `impl-review` review node unconditionally (unit
`qa/code-review`, completion gate `code-impl-review`) between `execute` and `test`; `test`
is record-bound to its completion marker. After publishing execute's completion, run the
route-bound dispatch transaction with `NODE_ID=impl-review`: it reads the plan, source
diff, and dev logs, writes `_internal/dev_reviews/phase_review.md`, and stays read-only.
Publish its exact completion, read only the memo verdict, and route blocking findings
through the bounded refine/retry path in Step 4 — never an inline hotfix.

At `strong` the route runs `impl-review` (`balanced-deep`) plus
`impl-review-alternative` (`light`). At `thorough|adversarial` it adds the deep
`impl-review-failure-mode` perspective. Dispatch the sealed group through one
`dispatch-batch --parallel-group impl-review` call. Cross-harness-first placement and the
profile/perspective axes are independently reported; a same-harness fallback is typed
degradation, not silent parity. Merge at verdict level only — the stricter verdict wins and
blocking findings are unioned — and do not proceed past the gate until every selected leg's
verdict is read and the merge is recorded.

### Step 4: code-test

For standard+, run the route-bound dispatch transaction with `NODE_ID=test`; its route node
selects `assigned_contract=code-test` and the reviewer role. strong+ may select a deeper reviewer.
Pass plan verification and checklist paths, retain the emitted attempt ID, yield for the typed receipt, and publish exact
completion from `test_logs/test_report.md`. code-test is read-only and never hotfixes.

quick reports verify-lite failure without retry. Other graphs may open at most one pipeline-level retry:

1. Record the verdict; detailed context remains in `test_logs/test_report.md` and `_internal/test_reviews/` for code-refine.
2. Same-route in-place retry (SD-156): do not restore or roll back source and never run `git reset --hard`. Redispatch code-execute in place on the unchanged route with a new attempt identity; `worker-route-guard.py` reads one lineage verdict (`source_lineage_verdict`) against the sealed `source_commit` for every node, regardless of node position or prior registry attempts. `exact` and `descendant` HEAD both pass — a moved `HEAD` needs no retry evidence anymore — and the observed commit, its distance, and its branch are recorded onto the attempt row (`launch_head`, `source_commit_sealed`, `source_commit_distance`, `source_commit_branch`). A `diverged` HEAD is refused (`route-source-commit-mismatch`) with two named recoveries: return to the sealed line of work (switch back to the sealed branch, or use reflog to restore the sealed commit), or compose a new route with `--parent-cycle <current cycle>`. An unverifiable tree (mid-merge, detached, timeout, non-repo) is refused under its own reason and never read as a pass. An SD-104 continuation always re-pins to the observed HEAD when it is `exact`/`descendant` of the inherited pin — there is no longer a decline branch that keeps the old pin; a `diverged` continuation source refuses outright (`continuation-source-commit-diverged`), and an `unverifiable` one leaves the inherited pin untouched. Downstream of `execute`, an impl-review/test/report prompt's diff base is `execute`'s own most recent terminal attempt's recorded `launch_head` — not everything since the sealed `source_commit`, which may include a prior package's commits in a serial execute chain; `pre_node_commits` is the first-parent commits from the sealed `source_commit` up to and including that `diff_base`. In-place re-dispatch on the original route remains available and is still the simpler move when the route is still yours to re-run.
3. Append the preserved compatibility memo literal at affected steps:

   ```html
   <!-- memo: [테스트 실패] code-test 실패. 상세: test_logs/test_report.md, _internal/test_reviews/. 대안 필요. -->
   ```

4. Reset checklist marks to `[ ]`.
5. Pause for explicit user refine, or invoke one bounded code-refine.
6. Redispatch code-execute, then code-test.
7. On pass, continue. On a second failure, apply the shared content-ownership recovery procedure in `capabilities/code-execute.md#portable-procedure` to the implementation edits recorded across the attempts. Reverse only clearly attributable edits; preserve unrelated or ambiguous contents, including same-file, staged, and untracked changes. Write a failed summary noting both attempts, recovered and remaining edits, the reason for any uncertainty, the observed index/worktree state, and checks skipped because of that state; stop before code-report.

### Step 5: code-report

For standard+, run the route-bound dispatch transaction with `NODE_ID=report`; its route node
selects `assigned_contract=code-report` and the writer role. Pass plan, checklist, dev logs,
test logs, and review paths, retain the emitted attempt ID, yield for the typed receipt, and publish exact completion
from the final report. Use the closed fallback when needed.

### Merge-preparation projection gate

Run `python3 tools/generate.py`, then the projected/deferred census, then `python3 tools/generate.py --check`, then `./tools/check-adaptation-boundary.sh`, in that order. On failure return to source. Update both harness lists in `tools/check-adaptation-boundary.sh`; `skills/` is canonical and `adapters/claude/skills/` plus the plugin tree are generated by `generate.py`. Never edit generated projections manually.

The boundary run is a separate step because `tools/sync-missing-projections.py` (the `generate.py` member that fills Claude counterparts) covers `loops/`, `scaffolds/`, `tools/memory`, `tools/install`, `tools/integrations`, and `tools/fleet` — **not** top-level `tools/`. A new `tools/<file>` therefore needs its `adapters/claude/tools/<file>` symlink (`../../../tools/<file>`) created by hand until that domain is automated; `generate.py --check` will not tell you, but `pre-push` and CI will.

### Step 6: Pipeline Summary

Before writing shared singleton files such as `pipeline_summary.md` or `pipeline_state.yaml`, acquire the OPERATIONS §5.8 `.pipeline-lock` and release it immediately after the write. Spec updates also use their owning Skill's lock. Cycle ownership remains path-separated. On lock exit 3, stop the write and report.

Write the dev-mode summary, then report its path and a two- or three-line verdict.

### Step 7: Refresh `analysis_project/code/`

After reporting, inspect changed files from dev logs or `git diff <execute-launch_head>..HEAD --name-only`, using execute's recorded `launch_head` as the diff base.

| Change | Route |
|---|---|
| At most three files in one module; function, class, signature, rename, one-line, or small logic change; only Interface Reference affected | **A — direct edit:** update the module document's Interface Reference and at most one short Role/body line. |
| Four or more files; module or model-folder add/delete/rename; broad cleanup; config mechanism, preferred layer, train/eval split, seed, or reproducibility change | **B — invoke analyze-project:** run incremental code mode with `--skip-qa`. |

Ambiguity defaults to B.

```bash
/analyze-project --mode code --skip-qa
```

Report how many analysis artifacts changed. Skip Step 7 only on explicit input such as `"분석 자료 update skip"` or `"--no-analyze-update"`. Apply the same logic after debug fixes; they usually take route A.
