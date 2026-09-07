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

```bash
python3 "$AGENT_HOME/utilities/dispatch-readiness.py" \
  --worktree "$WORKTREE" --jobs "$CANONICAL_JOBS" \
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
to the launch, `dispatch-owner --route-evidence "$ROUTE_FILE" --start ...`, so the adapter
cascade cannot select a harness the tuples never probed. Getting any of this wrong fails route
compilation or owner selection before material work; it must never exhaust checked hops and then
silently run the route inline.
For a Codex owner the generator automatically applies the prospective standard-owner network
and exact registry check. A raw depth-0 `nested-headless` call without that context reports
`prospective-owner-check-required`; it is not evidence that nested networking is unavailable.
Once the Codex owner is running, its internal probe instead requires the launcher's actual
network marker.
Dispatch every durable node through `utilities/dispatch-node.py`; it binds the route identity,
node, write scope, completion gate, exact fallback tuple, and current attempt axes to the
selected adapter wrapper:

```bash
STAGE_OUTPUT=$(python3 "$AGENT_HOME/utilities/dispatch-node.py" \
  --route "$ROUTE_FILE" --node "$NODE_ID" --adapter "$STAGE_ADAPTER" \
  --action start --slug "$STAGE_SLUG" --qa "$QA" \
  --parent "$CONDUCTOR_SLUG" --prompt-text "$STAGE_PROMPT" \
  -- --jobs "$CANONICAL_JOBS")
printf '%s\n' "$STAGE_OUTPUT"
ATTEMPT_ID=$(printf '%s\n' "$STAGE_OUTPUT" | sed -n 's/^attempt_id=//p' | tail -1)
test -n "$ATTEMPT_ID"
```

For a sealed 2–4-node `parallel_group`, replace every member-level launch with one
atomic batch call; the command returns all stable attempt IDs in its bounded JSON receipt:

```bash
BATCH_OUTPUT=$(python3 "$AGENT_HOME/utilities/dispatch-batch.py" \
  --route "$ROUTE_FILE" --parallel-group "$PARALLEL_GROUP" --action start \
  --slug-prefix "$CONDUCTOR_SLUG" --parent "$CONDUCTOR_SLUG" --qa "$QA" \
  --jobs "$CANONICAL_JOBS" --prompt-text "$STAGE_PROMPT")
printf '%s\n' "$BATCH_OUTPUT"
```

Sequential `dispatch-node.py` or `dispatch-chain` calls are not a parallel batch:
admission and wrapper starts would no longer be one transaction. Individual group-member
`register`/`start` calls therefore fail closed. The batch seals the complete N-way group,
exact parent generation, profile, perspective, leg index, and required/realized independence
axes. On an idempotent repeat it consumes no capacity for exact active/completed legs. A
single missing-leg recovery proves every other N-1 member; any larger partial launch is denied.

Keep `ROUTE_FILE`, `CANONICAL_JOBS`, `NODE_ID`, and the captured `ATTEMPT_ID` together until
that node's completion transaction succeeds. Never dispatch standard+ with a raw wrapper
command that omits the route record.

The prompt carries only subskill name, absolute input paths, output contract, intensity, and slug. It never carries plan bodies or prior-stage conversation. Each stage reads files; the conductor reads only verdict and gate state. Register every stage in `.dispatch/jobs.log` and keep conductor plus active stages at or below five processes. Runtime-owned completion observes exact liveness outside the model; the conductor never adds a recurring monitor. One-line or no-artifact micro-stages stay inline.

#### Runtime-Owned Batch Join

After registering every separable child in the current batch, end the turn with
`runtime_wait: registered-children`. The adapter supervisor waits outside the model
until every exact `parent_attempt_id` child is closed or ready for typed harvest, then
resumes the same Codex thread or Claude session once with a bounded receipt. Do not
call liveness, inspect raw child output, or do parallel work while parked.

Only when the wrapper reports `completion_delivery=poll-fallback`, use the checked
legacy wait in the same turn:

```bash
sh <agent-home>/utilities/dispatch-wait.sh --parent <conductor-slug>
# exit 0 = done and harvest
# exit 2 = still alive; continue the same bounded fallback wait
# exit 3 = suspect or dead; diagnose and redispatch
```

After judging a stage's artifact contract complete, publish its captured exact-attempt completion before
dispatching the next stage. The completion transaction writes the immutable marker/link and
closes only that attempt row; never mark a routed row `done` first:

```bash
python3 <agent-home>/utilities/capability-route.py complete \
  --route <route-file> --node <node-id> --evidence <stage terminal artifact> \
  --jobs <canonical-jobs.log> --attempt-id <exact-attempt-id>
```

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
re-isolate/re-probe stop and never reaches this list. For case 3, record reasoning in
`plans/<slug>/_internal/metrics.md`; an unrecorded inline standard+ run violates the contract.
Still parallelize separable census or disjoint file groups. Dispatch-infrastructure
self-modification requires the explicit `STAGE_DISPATCH_INLINE_OK` opt-out.

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
  --capability autopilot-code --capability-mode debug --qa standard \
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

When implementation or reporting requires result plots, experiment-log visualization, or result tables, the code-execute or code-report worker records the need in its artifact. The enumerated autopilot-code recipe compiles no `material/*` node, so the owner satisfies the need per the WORKFLOW compose-on-demand doctrine (§0.2.1, `capability-route.py compose`; an autopilot-code graph carries no `material/*` stage id, so a material node is composed through `utilities/compose-route.py` with an explicit unit): a composed route extension node bound to the matching `material/*` unit (e.g. `material/figure-gen`, `material/data-script`) that passes the same validator and hash-seal as a recipe route — or, for narrow throwaway scaffolding only, an ephemeral native helper with no unit semantics. Training and experiment execution remain in autopilot-code; the material units own postprocessing. Record generated asset paths in the relevant dev log.

### Higher-Intensity Perspective Extensions

Registry-v6 widens only selected leverage points. `strong` uses a 3-way frame group and
2-way plan/implementation-review groups. `thorough|adversarial` keep the 3-way frame and
widen plan plus implementation review to three legs. Their model profiles and perspectives
are deliberately asymmetric. Any additional security/material/specialist node still needs
a validated compose-on-demand extension with disjoint output and a completion gate; never
create an undeclared child or alter a sealed group width.

### Step 0: frame (2-way at standard, 3-way at strong+)

Skip for direct and quick (orient-lite carries the framing posture inline). Every
compiled standard+ route opens with `parallel_group=frame`: `frame` and
`frame-alternative` at standard, plus `frame-contrarian` at strong+. Framing expands from `standard` —
not `strong` — because the direction decision is the point of maximum downstream
leverage (user directive 2026-07-24: an early direction error cascades into
hotfix/patch work and cost blowups).

Dispatch the group with one checked `utilities/dispatch-batch.py --action start
--parallel-group frame` call. It atomically reserves every absent first-start leg and
starts wrappers concurrently. Cross-harness requires at least two harness families across
the group; `balanced-deep`, `light`, and (at strong+) `deep` profiles plus distinct framing
perspectives provide asymmetric exploration. A typed same-harness degradation must be
explicit and recorded. The legs work blind and write separate direction briefs
(`direction-brief.md`, `direction-brief.alternative.md`, and when selected
`direction-brief.contrarian.md`) containing a problem statement,
root-cause/essence evidence, 2–3 direction options with trade-offs, and a
committed direction verdict with rejected alternatives.

Publish each leg's exact completion from its brief. There is no conductor-level
merge for framing: `plan` is record-bound to every group marker, reads every brief,
and must record which direction it adopts (and why, when the legs disagree —
disagreement between briefs is signal, not an error).

Then the post-frame direction gate (owner-execution.md "Post-Frame Direction
Gate"): build `shards/frame/frame-summary.json` and the plain-language
interview `shards/frame/interview.json`, raise `frame-review` with the
interview as the artifact, wait on `workflow-supervisor.py await-release`, and
on `proceed` render `shards/frame/intent.md` from the recorded answers. `plan`
does not start before that — every launch surface refuses it.

### Step 1: code-plan

Skip for direct. quick uses an inline micro-plan. For standard+, first verify the SD-13 precondition: the repository has an artifact root and `spec/`. Then dispatch:

```bash
AGENT_HOME=$(utilities/agent-home.sh)
NODE_ID=plan
STAGE_ADAPTER=claude # choose claude, codex, or opencode from checked route evidence
STAGE_SLUG="${CONDUCTOR_SLUG}-plan"
STAGE_PROMPT="<sub-skill contract + absolute input paths (Intent: shards/frame/intent.md first, then every direction brief) + output contract + slug>"
# Run the route-bound dispatch transaction from "Standard+ Stage Dispatch",
# capture ATTEMPT_ID, then use that same value for `capability-route.py complete`.
```

On the normal supervised path, yield `runtime_wait: registered-children` and consume
the typed receipt on resume. Only for an explicitly reported `poll-fallback`:

```bash
sh "$AGENT_HOME/utilities/dispatch-wait.sh" --parent <cycle-slug>
```

After the typed receipt (or fallback exit 0), read only plan status and paths. A terminal recovery receipt permits checked exact-attempt diagnosis; raw transcript inspection still requires a terminal/closed row or explicit operator recovery. Direct and the quick one-shot owner keep their declared inline plan; standard+ invokes `code-plan` in-session only after the compiled fallback policy reaches inline under the closed rules above.

For standard+, the plan stage must also write `plan_slices.json` beside `plan.md` and `checklist.md`. It is the closed machine contract consumed by execute: `schema_version=1`, `route_node="execute"`, `decision` (`slices` or `serial`), `serial_reason`, and `slices`. A `slices` decision has 2..`max_slices` exact fixed-file entries; a `serial` decision has no slices and one of the three allowed serial reasons. The human-readable slice prose in `plan.md` is not machine input.

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

**Subdivision check (SD-103) — automatic at execute start.**
`execute` is the one node that carries a sealed `subdivision` permission
(`min_intensity: standard`, `max_slices: 4`, `disjointness: exact-fixed-files`). The
execute start point, `utilities/stage-dispatch-fallback.py`, must query that permission on
both `--register` and `--start` before candidate or attempt selection, after the
parent-identity fences the ordinary path already passes. It reads exactly one plan: the
explicit `--plan-slices <path>` when supplied, otherwise `plan_slices.json` in the plans
bucket of this route's producer cycle (a continuation also tries its source route), and
then the legacy top-level `plans/` bucket; it does not search other locations, and
`_scratch` is never one of them. A route with no readable plan artifact is a typed serial
decision naming the attempted source in `subdivision_plan_source=`. The
wrapper records exactly one `subdivision_decision=` and `subdivision_decision_id=` in
`subdivision/<route_id>.jsonl`, using `not-eligible`, `considered-declined`, `admitted`,
or `refused`. Missing/serial plans and typed refusals are normal "single session" answers,
but they are never silent. A successful batch preserves the existing receipt keys and
emits `attempt_id=` for the representative slice plus `attempt_ids=` for the batch.

For an admitted `slices` plan, the wrapper mints the chain and slice ids, writes one phase
brief per slice, proves exact files/worktree/write-scope containment and disjointness, then
uses the existing `dispatch-batch.py --parallel-group execute --subdivision-manifest
<chain.json> --action register|start` transaction. Close the single stage gate with
`capability-route.py complete --subsession-manifest <chain.json>`. Slices are no-commit
workers; close the gate first, commit after. A typed refusal or serial declaration means
ordinary single-session execution and must be recorded in the dev log. A slice with a
non-worktree `base` remains refused (`scope-unproven`). `core/OPERATIONS.md §5.10` owns
the manifest, baseline, and refusal vocabulary; there is no remaining manual transcription
step.

Read plan frontmatter after harvest:

- `done` → impl-review, then Step 4;
- `partial` → impl-review, then Step 4 for completed work;
- `failed` → source has been rolled back. Write failed `pipeline_summary.md`, report, and stop before test or report.

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
2. Same-route in-place retry (SD-67): do not restore or roll back source and never run `git reset --hard`. Redispatch code-execute in place on the unchanged route with a new attempt identity (the prior attempt row is the lineage evidence); `worker-route-guard.py` accepts the resulting moved `HEAD` only when the node is declared in the route's `resume_retry_boundaries`, the bound canonical global registry holds a different prior attempt for the same route/node, and `HEAD` is a first-parent descendant of the route's `source_commit`. Never recompile or re-pin the route to manufacture this evidence. That covers this same-route retry; an SD-104 continuation is a new successor route and pins the resume-time `HEAD` as its own `source_commit` (SD-128), but it declines that re-pin — keeping the inherited pin — whenever any node it will re-run mutates the worktree and already has an attempt anywhere in the route's continuation lineage, and it declines whenever that evidence cannot be proved absent (a registry it cannot prove is the lineage's own, or one holding no rows for that lineage). A decline keeps the inherited pin for the whole route, so every node at or before the mutation node meets a moved `HEAD` against an unchanged pin. Since SD-133 the guard reads retry evidence across the route's whole lineage, so an ancestor's attempt on that node is found and SD-67's conditions decide the launch — a continuation can carry an SD-67 retry. In-place re-dispatch on the original route remains available and is still the simpler move when the route is still yours to re-run.
3. Append the preserved compatibility memo literal at affected steps:

   ```html
   <!-- memo: [테스트 실패] code-test 실패. 상세: test_logs/test_report.md, _internal/test_reviews/. 대안 필요. -->
   ```

4. Reset checklist marks to `[ ]`.
5. Pause for explicit user refine, or invoke one bounded code-refine.
6. Redispatch code-execute, then code-test.
7. On pass, continue. On a second failure, roll back, write a failed summary noting both attempts, and stop before code-report.

### Step 5: code-report

For standard+, run the route-bound dispatch transaction with `NODE_ID=report`; its route node
selects `assigned_contract=code-report` and the writer role. Pass plan, checklist, dev logs,
test logs, and review paths, retain the emitted attempt ID, yield for the typed receipt, and publish exact completion
from the final report. Use the closed fallback when needed.

### Merge-preparation projection gate

Run `python3 tools/generate.py`, then the projected/deferred census, then `python3 tools/generate.py --check`, then `./tools/check-adaptation-boundary.sh`, in that order. On failure return to source. Update both harness lists in `tools/check-adaptation-boundary.sh`; `skills/` is canonical and `adapters/claude/skills/` plus the plugin tree are generated by `generate.py`. Never edit generated projections manually.

The boundary run is a separate step because `tools/sync-missing-projections.py` (the `generate.py` member that fills Claude counterparts) covers `loops/`, `scaffolds/`, `tools/memory`, `tools/install`, `tools/integrations`, and `tools/fleet` — **not** top-level `tools/`. A new `tools/<file>` therefore needs its `adapters/claude/tools/<file>` symlink (`../../../tools/<file>`) created by hand until that domain is automated; `generate.py --check` will not tell you, but `pre-push` and CI will.

### Step 6: Pipeline Summary

Before writing shared singleton files such as `pipeline_summary.md` or `pipeline_state.yaml`, acquire the OPERATIONS §5.8 `.pipeline-lock` and release it immediately after the write. Spec updates also use their owning Skill's lock. `plans/<cycle>/` remains path-separated. On lock exit 3, stop the write and report.

Write the dev-mode summary, then report its path and a two- or three-line verdict.

### Step 7: Refresh `analysis_project/code/`

After reporting, inspect changed files from dev logs or `git diff <safety-commit>..HEAD --name-only`.

| Change | Route |
|---|---|
| At most three files in one module; function, class, signature, rename, one-line, or small logic change; only Interface Reference affected | **A — direct edit:** update the module document's Interface Reference and at most one short Role/body line. |
| Four or more files; module or model-folder add/delete/rename; broad cleanup; config mechanism, preferred layer, train/eval split, seed, or reproducibility change | **B — invoke analyze-project:** run incremental code mode with `--skip-qa`. |

Ambiguity defaults to B.

```bash
/analyze-project --mode code --skip-qa
```

Report how many analysis artifacts changed. Skip Step 7 only on explicit input such as `"분석 자료 update skip"` or `"--no-analyze-update"`. Apply the same logic after debug fixes; they usually take route A.
