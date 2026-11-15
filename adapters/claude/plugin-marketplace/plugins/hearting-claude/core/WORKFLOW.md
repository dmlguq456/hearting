# Autopilot-* Routing Map — Agent-Facing Core

> A compact map for the main agent to route a task request to capabilities and roles. Each adapter maps them to its runtime-native skills, commands, agents, or profiles. Do not force symmetry; separate work according to its nature.
>
> The root `README.md` owns the user-facing meaning map and entry list. `CONVENTIONS.md` owns QA, model, and folder definitions. This document contains routing tables only, avoiding duplicated narrative and invocation examples.

---

## 0. Invariants — One Router and the Artifact Order Convention

This is the single routing contract for spec-backed projects that contain `.agent_reports/spec`, with legacy `.claude_reports` compatibility. Read it on demand when the adapter's status or reminder surface indicates routing is due; hooks expose runtime state but do not replace or eagerly inject this contract.

Every task first passes through the work-nature map in §2. Direct work, runtime plugins, and built-in Skills are used only where this router places them. Adapter and runtime projection work also remains core-first: establish the portable invariant in `core/`, read its governing document, then change adapter or generated output.

After the §0.1 read-only exemption and §0.2 semantic precedence, a request that
clearly matches one manifest `entry-router` positive trigger and none of that
entry's exclusion boundary uses that entry as the primary route. If multiple
entries match, resolve them through §0.2 and the work-nature map instead of
silently dropping to capability-free work.

Capability-free inline work is limited to read-only orientation, status, a
simple factual or explanatory answer that requests no new durable artifact, or
an explicit conversational/no-files constraint, or the tiny-fix path in §0.2.1.
The tiny-fix path in §0.2.1 records an inline change automatically; explicit `direct` remains an intensity inside an entry route.

Before material work or read-only context recovery proceeds, confirm that the
current main-session prompt received its bounded capsule candidate probe. The
probe searches mechanically but does not decide relevance; inspect a candidate's
full record before applying it and ignore unrelated candidates. If the prompt
hook is unavailable or failed, record `recall` with one focused query or `skip`
with a short contextual reason through `mem recall-gate`. Neither path stores the
raw prompt, classifies it, or prescribes topic categories. Registered route-bound
workers do not run this main-session lifecycle and remain exempt from its receipt.

### 0.1. Read-Only Orientation Before Capability Routing

Before selecting a capability or Skill, distinguish read-only orientation from
work that creates or refreshes a persistent artifact. A request whose desired
outcome is to understand the project, recover prior context, resume from the
current state, or report status is orientation when it does not also ask for a
new analysis or a modification. These examples describe intent; they are not a
keyword classifier.

Read-only orientation invokes no capability and writes no artifact. Recover
context in this order:

1. Record `recall` at the memory opportunity gate with one targeted query from
   the task, then search before broad discovery. This is an agent judgment for
   orientation, not a prompt-keyword classifier. A shortened, ellipsized, or
   otherwise insufficient hit is only an index: read the full body by record ID
   before using it as evidence. Record `applied` or `miss` against the gate id
   after the evidence decision.
2. Use the adapter status surface and `utilities/artifact-root.sh` to resolve
   the project-wide canonical artifact root. In a linked worktree, ignore its
   tracked artifact snapshot and read the primary worktree's canonical root.
   Prefer canonical `.agent_reports/`; only when it is absent, use an existing
   legacy `.claude_reports/`.
3. Read existing state before a broad source census: the newest relevant
   `pipeline_summary.md`, `pipeline_state.yaml`, `summary.md`, `REPORT.md`, or
   `STORY.md`; the latest experiment contract and `experiments/_RUNLOG.md`;
   and the current `spec/prd.md` or task-specific specification. Read only the
   subset needed to orient, and follow any relevant pointers from memory.
4. Inspect primary code, data, and raw logs only when recovered contracts leave
   a material question unanswered or must be checked against live behavior.

Resolve conflicts with this evidence precedence:

```text
latest specification or user-confirmed decision
  > durable project fact
  > latest experiment contract
  > legacy document
```

Live primary behavior is validation evidence, not permission to silently
rewrite an explicit current contract. When a lower-priority source differs,
report the drift and identify both sources; do not merge their meanings or
quietly choose the legacy value.

An explicit request to analyze existing primary code, paper, or document
materials selects `analyze-project` as the default primary when no usable
persistent analysis exists. Treat the analysis request as artifact-producing
unless the user explicitly asks for conversational/read-only analysis or no
files. Existing analysis that is demonstrably stale for downstream work, or an
explicit refresh request, also makes `analyze-project` eligible. Artifact
absence by itself never selects the capability, and empirical evaluation,
external research, implementation, and completed-artifact inspection retain
their §0.2/work-nature primaries. When analysis already exists, read it before
deciding that reanalysis is needed.


**Hard artifact order:**

```text
[code] research / analyze-project(code) → autopilot-spec (spec/) → autopilot-code (plans/)
[docs] research / analyze-project(paper or doc) → autopilot-draft → autopilot-refine
```

- **Spec-backed implementation:** if a code request has no `spec/`, run `autopilot-spec` first, except a known-scope local completion unit under §0.2.1 (including a small new feature) or a one-off throwaway. Repeated new behavior graduates to a spec.
- **No spec without prior evidence:** if neither `research/` nor `analysis_project/` grounds the spec, run `autopilot-research` or `analyze-project` first. Enforce this more strongly in unfamiliar domains and for new intent.
- Hook write gates are retired; artifact creation order remains a routing convention.

**The owning capability also owns revisions.** The routing reminder and convention govern edits.

| Artifact | Sole update path | Version location |
|---|---|---|
| `spec/` blueprint | `autopilot-spec` update | `_internal/versions/v{N}/` |
| code work under `plans/` | `autopilot-code` | `plans/<date>_<slug>/` |
| documents | `autopilot-draft` or `autopilot-refine` | `_internal/versions/v{N}/` for major refinement; minor history in `pipeline_summary.md` |
| experiments | `autopilot-lab` | `_RUNLOG.md` |
| DB records with `type=profile` | `analyze-user` or `mem profile-append` | changelog inside the record body |

This document plus the runtime adapter bootstrap is the routing source of truth. Violation signals include ad-hoc artifact edits, code before its gates, or updating an artifact through a capability that does not own it.

### 0.2. Semantic Primary Routing

Choose the primary capability from the purpose of the work the request performs, not
from the artifact the user names or the surface verb such as "update", "fix",
or "정리". A request that ends in "update the report" still has its primary
decided by the evaluation, implementation, or independent document goal it serves.

Precedence, highest first:

1. Experiment evaluation and its result report belong to `autopilot-lab`
   (`eval` for evaluation, analysis, and result reporting; `setup` for new
   training). This includes new inference, synthetic failure reproduction,
   metric/ablation computation, and figures or media that produce new empirical
   comparisons. It also includes interpretation, limitations, listening
   comparisons, and conclusions from already fixed results, without new
   measurements. Prose, Markdown, or HTML does not change this ownership.
   Reuse existing metrics, checkpoints, and media when sufficient; select only
   needed stages, including the existing `report` / `editorial/report` stage,
   under §0.2.1. Reporting alone requires neither a new training/inference run,
   the full eval loop, nor a separate draft cycle. Preserve the selected
   intensity, verification, and experiment lineage.
2. With no new empirical work, correcting only the wording, structure, or
   errors of an existing document makes `autopilot-refine` the primary
   (`autopilot-apply` for an approved patching guide). Re-rendering fixed data
   with different size, fonts, layout, captions, or table order is document
   correction or work within the existing artifact owner, not new empirical
   evaluation. Updating a paper with finalized metrics stays refine/apply;
   interpreting and reporting the experiment itself stays lab/eval.
3. A change to requirements, evaluation policy, or any blueprint surface adds
   the owning route's spec transaction as a secondary spec-sync step; it never
   replaces the execution primary. For `autopilot-code`, that is the same-route
   `spec-transaction` before implementation, not a return to `autopilot-spec`.
4. `autopilot-draft` owns a new independent document goal: a paper,
   presentation, proposal, or report with its own audience and document brief.
   Writing inside an experiment report uses lab's report/editorial support
   without changing primary or creating a separate cycle for prose.
5. Durable result routing may offer the canonical artifact to the optional
   `artifact-sink` extension, always secondary and last. The portable harness
   owns only the closed `artifact.completed` receipt and a local registration
   check; it has no note, DB, credential, routing, or UI semantics.

   Extension absence is normal and silent. A registered handler that reports
   unavailable produces `skipped/extension-unavailable`; an activated handler
   failure is `failed/artifact-sink` and remains retryable without invalidating
   the primary result. Hooks are not activation authority. The extension owns
   product-specific setup guidance, identity, upsert behavior, and publication.

   A report-bundle offer uses receipt schema v2 containing only the common
   event/status/timestamp envelope plus `bundle_id`, `version`, and `entrypoint`
   (`report/index.html`). It omits v1 `source_path`, `source_capability`, and
   `project_root`; the three bundle fields are all-or-none. Neither an absolute
   bundle path nor file payload is passed to the sink. Offers without bundle
   metadata remain the exact receipt v1 contract for compatibility.

   | Primary capability | optional artifact-sink policy |
   |---|---|
   | `autopilot-code`, `autopilot-draft`, `autopilot-lab`, `autopilot-refine`, `autopilot-research` | Topology-sealed after the declared durable terminal; offered only while an extension handler is registered and available. |
   | `analyze-project` | Same optional extension offer after persistent analysis completes; this pre-capability has no entry-recipe topology row. |
   | `autopilot-apply`, `autopilot-design`, `autopilot-ship`, `autopilot-spec` | No automatic offer until the recipe declares a concrete durable source output. |
6. A secondary capability must never substitute for the primary execution
   capability, and the primary never absorbs a secondary's artifact ownership.

| Request shape | Primary | Secondary |
|---|---|---|
| "Reevaluate the model on a new test set and update the report" | `autopilot-lab --mode eval` | existing lab report stage; `autopilot-spec` on policy change; optional artifact sink |
| "DSC final evaluation: infer with two fixed checkpoints, reuse frozen aggregates, and report the comparison" | `autopilot-lab --mode eval` | existing media/report stages |
| "Report the experiment's conclusions and limitations from fixed metrics; no new inference" | `autopilot-lab --mode eval` | existing report/editorial support only as needed |
| "DSC analysis: synthesize cases to reproduce the field failure" | `autopilot-lab --mode eval` | analyze-project for existing code/graphs when useful |
| "TF paper: reduce figure height and reorder table columns using the same data" | `autopilot-refine` | apply an approved guide, or keep the existing artifact owner |
| "Put finalized experiment metrics into the existing paper" | `autopilot-refine` | `autopilot-apply` for an approved guide |
| "Write an independent paper or presentation from the evaluation" | `autopilot-draft` | consume existing lab results |
| "Implement a reusable evaluation driver or fix the HTML generator's path bug" | `autopilot-code` | lab only if a separate empirical evaluation is requested |
| "Fix only the typos and sentences in REPORT.md" | `autopilot-refine` | — |
| "Change the evaluation mixing policy to unscaled and reevaluate" | `autopilot-lab --mode eval` | `autopilot-spec` update; neither replaces the other |

An "analysis" label does not select a capability: reading existing code or
papers uses analyze-project/research as appropriate; new synthesis, failure
reproduction, or model comparison uses lab/eval; implementing a reusable
evaluation program uses code. Small experiment-support scripts remain within
lab's existing implementation stages and do not automatically open a code cycle.
Existing-result analysis, evaluation, and comparison therefore select
`autopilot-lab --capability-mode eval` under this same purpose rule. The parent
defaults to the named **Existing-result analysis** composition in
[the lab contract](../capabilities/autopilot-lab.md#existing-result-analysis-eval-default-composition):
`metrics → diagnose → report`, adding `independent-verify` at strong or higher
with its declared group and automatic owner close. Include `eval-run` only when
the request needs new raw results, using its existing inputs and write scope.
Choose capability and sequence from the goal in every folder. Git diagnosis
decides source isolation and execution placement; it does not identify code work
or prove that a task fits one inline result. Omitted shape uses the existing
frame in both Git and non-Git folders. The frame selects the primary capability,
parts, inputs and handoffs. A report assembled from existing materials,
including mail and attachments, normally uses `autopilot-draft/doc` with
`analyze-project/doc` for source analysis.

A small, known document correction still uses the existing direct path in the
current session, with the capability chosen by the main session and normal
inline `finish`. It needs no frame pair, Git start question, initialization,
worktree or folder memory. An explicit capability/mode and an approved
route-plan remain binding. Legacy explicit direct starts may retain their
conservative purpose hint or unresolved storage fallback; that fallback does
not establish the work's meaning. A Git direct start retains its existing
default. An explicitly requested new Git repository retains its ordinary
preparation/approval path. No new classifier, question or required argument is
introduced.
Selection and compilation use the same task bytes, including a one-shot stdin input.
An explicitly selected worker shape, including a route-plan leg whose own shape is not direct, is a
worker request. In a non-Git folder, a compiled artifact-only worker route may run in the
bound folder; a route whose compiled write scope mutates source is refused with the reason
and the next action (`git init` and restart, or continue this session with `--shape direct`).
The classification comes from the compiled write scope, not a capability-name list.
A later compose in a Git repository follows the
existing Git behavior.
Likewise, code review within an implementation/debug cycle belongs to
`autopilot-code --mode audit`; independent inspection of completed work belongs
to `audit`. Both may inspect code; purpose, not file type, decides the owner.

Collecting existing artifacts into a delivery archive uses
`autopilot-ship --mode package`: reuse the delivery template, named model
versions, requested input/output list, and existing reports; check the included
files, relative links, versions, and archive. This is a delivery goal, without
an automatic deployment, installation, security/release-review loop, new
evaluation, or report rewrite. Keep the sample list within the request: nearby
datasets, historical models, and a larger earlier delivery are not implicit
inputs. A later approval of a larger batch applies only to that batch.
Missing sample inference is lab/eval work for those samples, with packaging as
the delivery step; implementing a reusable packager is code work. Ship without
a mode retains its existing deployment/release behavior (`default`).

| Delivery request | Primary | Boundary |
|---|---|---|
| "Reuse the delivery template, named model and requested samples/outputs/reports; make a ZIP" | `autopilot-ship --mode package` | Existing results first; only requested files and minimal link/version/archive checks |
| "Infer on the received field samples and one or two simulations, then package them" | `autopilot-lab --mode eval` | Only missing requested inference; ship/package for delivery, no full-dataset expansion |
| "Fix the reusable archive builder" | `autopilot-code` | Program implementation, not packaging an existing result |
| "Write an independent presentation for the recipient" | `autopilot-draft` | New document goal, not file collection |
| "Deploy the application to production" | `autopilot-ship` | Obtain deploy approval at route start; retain post-deploy verification |

For a legacy explicit non-Git direct start only, task classification is conservative: clear research,
fixed-result evaluation, reusable code, document correction or drafting, project analysis,
and packaging use their corresponding existing capabilities. Generic analysis, conflicting
purposes, negation, and quoted examples stay unresolved; they add no required question or flag.

For multi-step document work, keep the frame's capability, graph, inputs,
outputs, writer responsibilities and unfinished handoffs in the existing route
plan. Artifact-only stages may use the existing non-Git worker placement. A
step that edits an original document stays in a direct leg in the current
session; it does not collapse the other staged legs. Format tools such as HWPX
belong to draft/refine production and checks. Running a format tool does not
finish material review, integration or another person's missing draft.


### 0.2.1. Shape Before Preset (SD-135)

Compose and start show intensity and its source, sealed node profiles, and the
initial node/worker-dispatch/resource counts before launch. Unspecified intensity
adds one nonblocking suggestion to consider `strong` for judgment or root-cause
analysis, except framed routes which already start with two top-tier frame legs; intensity does not change model profiles. These observations add no
approval or required input. New compose records retain the two input sources in
the existing `selection_basis`; old routes display `sealed-route` when the
original explicit/default choice was not recorded. Worker estimates count stage
and frame legs, excluding the separately shown owner and any retries or later
routes chosen by a frame.

framed는 기본 두 갈래다. 작고 국소적인 일이면 `--intensity quick`을 붙일
수 있고, 과제가 공통 규칙·spec·여러 어댑터를 건드리면 하팅이 두 갈래로
지킨다.

The precedence above decides which capability **owns the artifacts** of a
request. It does not oblige the session to run that entry's whole recipe.
An entry's recipe is an assembly preset: the default when nothing else is
chosen and an example of how its parts fit together. The route is the sealed
stage list, assembled from the part catalog (`capabilities/topologies.json`
recipes plus `part_catalog`, SD-165).
The main session judges the existing seven direct predicates by **one local
completion unit**, rather than separate edits or verbs:
one reversible result for a feature or defect; known scope, whose location and
impact a short read of the source, callers and tests establishes; focused
verification with a known runner and expected result; no change to contracts
shared by components, adapters or external consumers (API, schema, security
boundary or PRD decisions); no long-running resource work; no independent
artifact handoff; and no independent verifier. A requested test run is the
session's own verification, not an independent verifier.

A small new feature or fix and its source, regression tests and README form
the same completion unit. A new file is not scope expansion by itself; a
small component's full suite can be focused verification. Its README explains
the feature rather than creating an independent artifact handoff. `fix`,
`outcome` and `edit` name this same scope. File, line and verb counts
are not classifiers. Reading known related files to confirm scope differs
from exploring several modules to find the cause.

A unit that satisfies these predicates and finishes in the current cwd's Git
worktree needs only reading, editing, focused checking and an ordinary final
reply. Existing turn hooks record it automatically; no task file, compose,
finish, separate capability-info call, question or classification call is
required. A new worktree or scope exploration uses the explicit route instead.

Use the same scope judgment for a small non-Git document correction, through
the existing direct compose/finish path in that folder. The main session
chooses the existing capability; the person need not name one. Material
collection, multiple drafts, integration and independent review use the frame.

If the work grows, compose before the next expanded action. The new route
links the current turn's start snapshot, observed changes and native evidence;
preparation preserves existing edits and user changes, without automatic
stash, reset or overwrite. Its workflow owns completion from there. A main
session's mistaken small-work judgment never disables the automatic record:
it retains total counts, bounded path samples with truncation/unknown markers,
observed command outcomes and the final reply.
`COMPLETE` for that automatic route means the record was saved, not that the
work or its tests passed. Missing, interrupted and running results stay unknown.

Explicit `compose --start --shape direct --prompt-file <task>` still prepares
an isolated worktree and an inline route; `compose --shape direct` records work
already made in place. Their receipt and explicit finish contracts are unchanged.
Otherwise use `compose --start --prompt-file <task>` with no `--shape`; the
frame chooses the capability, stages, and solo or staged shape, judging each
leg's optional `size` (`one-change`, `one-pass`, `multi-stage`) first; an
omitted shape follows it. Direct has no review-worker path of its own: work that
needs an independent review is not direct (omit `--shape`, or `staged`), and a
light check of a direct change may use the harness's own native subagent
(decision 2026-11-03).

| Main decision | Command | Result |
|---|---|---|
| One reversible local completion unit in the current Git worktree, including a small feature with its tests and README | read → edit → check → reply | automatic record; no start/finish round trip |
| Explicit direct source change needing preparation | `compose --start --shape direct --prompt-file <task>` | isolated worktree, inline node, dispatch depth 0 |
| One reversible local completion unit already made in place | `compose --shape direct` | records the in-place change as an inline node |
| Other new work | `compose --start --prompt-file <task>` | framed; frame proposes the smallest route and the existing interview confirms it |

Examples of the same boundary:

- **Direct:** one small local feature with its tests and README.
- **Plan first:** unrelated tasks bundled together, a new structure to choose,
  or cause exploration across several modules.
- **Full frame:** shared contracts, external API/schema changes, migrations,
  security or data risk, even when the edit is short.

`solo` means one registered owner; `staged` means separate stages. Shape
omission starts a framed route. `--capability`, `--capability-mode`, `--graph`,
and `--profile` without a shape are routing hints recorded for the frame, not
sealed choices. `--pin` applies as usual.

Skip a new frame only when a frame or the person already decided the shape:
resume a stopped route, follow the runtime-printed `next_leg`, continue within
an approved frame decision of the same campaign, or follow the person's
explicit capability and stages. The main session judges whether follow-up
stays within that approved scope. Its own judgment that work is already
decided is not an exception. Use the decided shape explicitly; a person-named
stage list uses `--shape staged --graph <stage,…>`. A route made from a
proposal (`compose --route-plan <record>#<i>`, a command the runtime prints)
has no frame nodes, and neither has a `solo` or `staged` compose: those shapes
compile without the recipe's frame pair (a route sealed earlier keeps the
frame nodes it was sealed with).

A `framed` route runs two `top` legs by default; the simplified one-leg route runs at `deep`. A preset recipe's own frame pair is also `top`: the route seals each leg profile and a concrete model pin is preserved. `frame-alternative` prefers another harness only when one is ungated in the same quality band (`CONVENTIONS.md §1.1`). Shared verification may rotate only `frame-alternative` for the same-harness, same-model case described in `CONVENTIONS.md §1.1`, keeping its route-sealed profile. Without an eligible, usage-available alternative, the leg runs immediately at its sealed profile and records why. A frame-rule `top` that stops at a usage limit is still retried once at `deep` through the SD-157 replacement lineage, and that capacity lowering is recorded separately. The `[경로]` card of `framed` adds the compiler's two lines: "frame이 방향과 경로를 조립해 제안합니다" and the cost line, "비용: frame 한 갈래 · 방향 확인 질문 1회" or, with both legs, "비용: 최상위 모델 두 갈래 · 방향 확인 질문 1회".

**Staged assembly guidance.** Staged assembly guidance is for frame brief
authors and routes whose capability and stages the person explicitly named.
These advisory points add no prerequisite or required stage:

- Name `--capability` and `--capability-mode`, and select only the needed parts.
  Reuse sufficient existing results. For a new measurement, include a node whose
  inputs and sealed `write_scope` cover it; the lab `metrics` node writes only
  `metrics.jsonl` and `summary-stats.json`, so it cannot create new raw results.
- Keep wording, labels, and layout corrections inside the already-approved work
  with its existing artifact owner and cycle, using only the checks the change
  needs. Preserve lab verification for scientific changes and spec sync
  for requirement or evaluation-policy changes; a separate document goal still
  follows §0.2. Name the decided capability explicitly: the ordinary
  `autopilot-code` default does not classify the request.
- In a fresh frameless graph, campaign history can still supply ordinary partial
  outputs, but it does not supply frame briefs. Reuse frame briefs through an
  explicitly selected parent or `--route-plan` proposal; the owner continues to
  judge whether that frame's scope covers the work.
- Choose `--intensity` for the work's purpose and, when needed,
  `--explicit-profiles` for judgment or verification nodes. Check the selection
  reasons and actual node/leg profiles in the compose output.
- Use `compose --explain` to check the displayed stage/worker/resource scale.
  At `strong` or higher, default to the eligible declared groups on retained
  anchors, with two-way as the baseline unless the registry widens it. Check
  realized legs and omission notices, including `terminal-anchor`; the label
  alone does not retain a group. Group selection stays registry-only.

At `strong` or higher, a subgraph ending in an eligible declared review or map
group receives an automatic owner closing node. It reads every realized leg's
result and writes only the route's closing summary. When it arbitrates auxiliary
legs, its assignment names the realized groups and the required
`auxiliary_findings_considered` frontmatter entries, derived from the sealed route.
It does not publish, sync, or change the preceding artifacts. The group anchor stays non-terminal and
the owner holds the terminal completion gate. Compose displays this addition
alongside omission notices. If a retained group lacks its review consumer or
auxiliary arbiter, compose restores the unique, safe downstream node declared
by that group's recipe, including borrowed-part paths and inputs. It inserts
the node directly after the anchor and preserves the caller's selected order;
it adds no publication, synchronization, resource run, or source mutation.
The assignment identifies any realized auxiliary legs and their existing
consideration obligation. Compose names every automatic addition. Ambiguous,
unavailable, or conflicting completions remain omissions with a warning.
Standard and lower routes and already sealed graphs keep their existing shape.

**Frame procedure at depth-0 (`framed`).** `start` launches the frame legs
(separate launches, no owner) and returns `needs-interview` with
`route_proposal_review`: each brief's validated proposal or
`proposal:none(<reason>)`, whether the two are `equal`, the start-approval parts
each leg would carry, and `frame_downgrade` (node, original and actual profile,
cause, attempt) when a leg ran one profile lower. Its `interview_template`
already holds the route question (`route-choice`: yes-no when the two proposals
are equal or only one is valid, choice when they differ, each option marked with
`"proposal": <frame node>`; "다르게" is the existing off-menu answer) and one
yes-no question per start-approval part (`<key>-leg<leg>`, the approving option
marked `"approves": true`). Depth-0 words them in the person's language (§0.4)
and adds the direction questions beside them, all inside `QUESTION_CAP`; a later
leg's approval question may be left out, and that leg then keeps its gate. When
the interview is submitted the runtime builds `route_proposals`
`{question, by_option}` from the marks and the validated proposals and records
it with the question. An interview that writes `route_proposals` itself is read
as written and offers only routes a brief proposed. After the answers, the
same `start` records the decision, compiles and starts the first leg only
(`--route-plan <record>#0`, `--parent-cycle <frame cycle>`), and closes the frame
route once that start receipt exists; its receipt is the first leg's own.
With no valid, chosen and approved proposal it records `selected: none` and
ends with `required_action=compose-route`; the session then picks the route as
as before. When a route made from a proposal closes, its completion receipt,
a replayed `start`, a direct `finish` receipt and route status carry `next_leg`
(`index`, the leg, a `compose_command` that includes `--start`). The person
approved every leg in the one interview, so a `start` that reads a finished leg
starts the next one itself and returns that leg's receipt with `plan_advanced`;
each started leg is one row of the plan cursor
(`.runtime/framed-decision/<frame route>.plan-cursor.jsonl`), so a replay or a
successor session on the same resume line reuses it. A plan approved with
`execution_scope: report` stops after its leg with `next_leg` as information, and
a next leg that could not be prepared is named in `plan_advance` beside it. The
main session may still change the plan or stop; §0.4 says when a card comes first.
A replayed `start` of the closed frame route answers for the furthest leg already
started from its decision, so after the last leg it reports that leg's completion
and no `next_leg`. A leg's optional `done_when` (sealed as `d1`, `d2`…), `verify`,
`hands_over` and approved `parallel` slices, and the brief whose route the person
chose, reach the owner and every stage worker of that leg from the sealed decision.
The owner may use `parallel` to assign workers or permitted sub-sessions; these
notes are owner guidance, not route nodes or parallel groups, and a slice without
a result is unfinished. A `qa/*` stage records
each item beside its artifact as `<artifact>.items.json` (`leg_items_v1`, read by
`route_plan.read_leg_items`). A leg may name its existing worktree with optional
`cwd`; validation, first launch, later legs and the printed compose command use
that folder and check dispatch there. Relative paths are based on the frame's
original cwd. Omission keeps the existing worktree preparation and inheritance;
this adds no question, flag or required input.
An explicit `--route-plan` is binding: read/validation errors stop compose before
preparation or publication. Absolute record paths may name another artifact root;
relative paths stay inside the selected root. A manual first leg's receipt lists
the existing complete/close/finalize commands for its frame, retaining the decision.
An unanswered interview can be corrected in its existing round; recorded answers
and decisions are immutable. Native full and partial replies commit under the
same interview lock and belong only to the exact registered interview bytes.
Request quotes come from this compose's human prompt
boundary; unavailable provenance omits the quote. Framed display separates the
requested width, bootstrap intensity and eventual approved leg intensity.
For an explicitly requested new repository, the existing route question includes
its destination and preparation (at most two notice lines). After approval the
runtime creates only an absent folder, initializes Git and its empty baseline,
and records path/time in the route evidence and first leg task. Existing folders
stay untouched and use the same interview's manual-preparation/direct choices.
Preparation retains the approved physical parent binding; directory creation and
Git initialization use pinned directories so a replaced ancestor cannot redirect
writes outside that binding. The binding stays open through first-leg compilation,
publication and launch, including resumed snapshots. A prepared first leg keeps
that repository; a replaced path cannot turn it into an existing-repository
worktree allocation. These are runtime checks within the existing approved start.
The common launcher passes the opened source descriptor and uses that directory
as the wrapper's actual process cwd. Adapters, launch fences, governors and
supervisors carry that binding to native child startup and resume. Durable route,
registry and receipt paths remain canonical; execution keeps its bound physical
cwd. A fresh process reopens only the recorded physical identity
when its inherited descriptors are unavailable.
Trusted launcher hops inherit only the source descriptor. Native model processes
start in the bound kernel cwd with directory descriptors closed; parent and Git
directory handles remain in the trusted launcher. Supervised directory
requests use the verified native process cwd for that process's lifetime.
Later legs that keep this same dedicated new-repository destination reuse its
physical binding and repository instead of allocating another worktree. An
explicitly different destination keeps its existing selection and preparation.
Compose records optional source-directory and Git common-directory identities.
An existing explicit recovery destination uses its own recorded identity for
the same descriptor handoff, preserving its original creation provenance.
Frame-time corrections stay on that route, appear in its existing Fleet detail,
and reach the interview and first owner. The retained history has no reader-only
size cutoff: bounded delivery takes recent conditions first and reports omitted
or shortened content with a reference to the full history in receipts, interviews,
owner tasks and the existing Fleet detail. Explicit conflicting output paths in
interview wording and `done_when` appear as receipt advisories.
A leg's `extra_stages` entry becomes node
`plan-<id>` right after its `after` stage, shaped on a catalogue check or
measurement stage of the same unit (one that writes no source; its gate, review
budget and start approval) and writing under
`parts/plan/<id>/`; one that cannot be placed is named in `read_notes`.

For execution, use `compose --campaign-key <stream> --start --prompt-file
<task>` with no `--shape` for framed work or an explicit decided shape. Use
`--shape direct` only for one reversible local completion unit (§0.2.1). The slug comes from the task; the
campaign defaults to this session's latest stream in the same artifact root
(including temporary directories and returning from a prepared worktree to
the primary checkout; another session or artifact root supplies no default),
else to the one active stream of the routes sealed for this folder, and otherwise
is an existing or new stream key, `--parent-cycle`, or an explicit `--unassigned`
(the refusal names this folder's streams first, then the root's active keys; a
folder name or close spelling of one active key joins it); `--profile light` or `--owner <harness>` is an explicit choice.
The task file contains the requested work, not instructions for running the
parent. The runtime seals it, prepares its cycle, starts the frame pair when
declared, and returns one receipt. Reuse that receipt's `resume_command` after
wakes or input corrections. It reuses exact attempts, carries partial admission
obligations, and never retries a failed attempt merely because it was called
again. Follow this receipt's top-level `parent_next`/`parent_next_command` while
work is running; nested launch receipts are evidence, not extra child waits. An expired bounded wait
returns `needs-attention`: report the pending work; runtime watchers retain
execution/cleanup responsibility and the deadline grants no retry authority.
`needs-interview` means both frames have been checked: compare their results,
fill the returned semantic template, and use `start --route <file> --interview
<question.json>`. It registers the gate before returning `needs-question`.
Native acknowledgement, timeout, or empty answers keep this question pending;
they carry no approval. The `needs-question` receipt retains the registered
question and choices for ordinary conversation when the native box closes.
Restate that block at turn end and wait for the person's actual reply; a plain
resume reuses the pending question rather than opening another native box.
After the actual structured or typed answer, use the same command with `--answers <answers.json>`;
it records intent, releases the gate, and starts the owner. Already answered
questions can supply both files without asking again. `--decision revise|stop`
records those choices; repeated answers reuse the recorded decision.
The acting parent records the answer's source in the optional typed `actor_kind`
field: `user`, `supervisor`, `automatic`, `headless-owner`, or `unknown`. Mark
`user` only for the person's actual reply; a file without a source is `unknown`,
not a user decision. This declaration is provenance, not native authentication.
`frame-review` takes the person's answer or a supervisor session's answer on
the person's behalf (`supervisor`); the intent records it as
`agreed-on-behalf`. A supervisor's yes does not start a `deploy`, `handback`
or `preview` part: its leg starts and that part keeps its own gate for the
person. Other person-only gates and start approvals accept only user answers;
other allowed releases preserve their source in the journal and intent. A
registered worker cannot use this field to bypass its existing release
restrictions.
Runtime settlement closes the route and cycle before owner success is delivered.
Direct start also prepares the inline cycle and returns its `artifact_env` for
work products written during the task; `finish` does not need it.
Replaying start on a
proven closed route returns its outcome without launching or reopening work.
The model does not assemble launch tuples or artifact variables, harvest, or
finalize ordinary work. Recovery commands name the exact
attempt; a changed scope still belongs to the user.

Without `--start`, `compose` returns the selected stages, profiles, human gates
and canonical `route_file`; full evidence stays in that file. `--help-all`
documents the advanced machine-compatible inputs, including `--full-record`.
`compose` preserves the chosen stages and derives their dependencies. Inherited
parallel presets that do not fit the selected graph are omitted; missing preset
stages are not mandatory. The sealed result shows the realized stages and omitted
defaults before execution. Input/output, explicit human gates, and terminal proof
remain binding contracts for the selected work.
`compile` remains the low-level explicit interface. Callers need not switch to it
to obtain a complete recipe. `compose` fills omitted flags from the checkout:
cwd (for a source-changing `--start` from a primary checkout, the worktree
`<repo>-wt/<slug>` on a collision-free branch and path, based on the commit SHA
resolved by a successful fetch of the current remote default. If the repository
has no remotes, preparation uses the primary worktree's current `HEAD` commit
without fetching; an unresolved `HEAD` fails before publication. This applies
regardless of shape, explicit cwd, or supplied dispatch evidence. An explicitly
approved task linked worktree is reused with its changes. A framed start
prepares the frame's read cwd too: an implicit linked cwd with dirty or unmerged
work is retained for reading, but a source-changing leg still needs the existing
proposed `cwd` approval to work there; clean, merged linked worktrees can be
allocated afresh. A newly allocated frame worktree is carried into its first
source-changing leg, while retained or explicitly approved linked worktrees
keep their established approval behavior. Exact saved route
snapshots/cursors/receipts and official continuations resume before any new
preparation; an existing plan cursor wins over allocation. Source-changing
preparation failure leaves that start unpublished and retryable through the
same request. If framed-start preparation fails, frame start records a one-line
reason and continues from the original cwd. Preview, explain, non-start
`compose`, and low-level `compile` remain read-only), artifact root
(`utilities/artifact-root.sh`), tracking and workflow mode by shape, a
default drift verdict, the spec-read gate (it refuses with
`compose-spec-read-required` when the artifact root's governing legacy
`spec/prd.md` is selected and this session's read hook has not recorded reading
it unchanged, unless the caller named what was read; the governing candidates
come from `artifact_cutover.prd_candidates`, so a shared revision supersedes a
legacy copy and only adds one `[경로]` card line with its path. A checkout PRD
outside the artifact root is informational and is not a marker-gate input;
absence evidence distinguishes that checkout file from a governing PRD), and both
eligibility probes (`dispatch-readiness`). The result is sealed, verified,
bound, and guarded exactly like a recipe route: `selection.route_origin`
records `compose` or `preset`, `selection.shape` records the shape, a staged
route is `composed: true` with its recipe embedded, and every staged node
keeps its unit, gate, write scope, model profile, and permissions from the
owning capability's recipe. A stage id may name a unit from the node's
declared `unit_choices` (`execute:dev/refactor`). A `capability:stage` token
(`autopilot-research:retrieval`) borrows a shareable part of another recipe:
it keeps its unit and gate, is written under `parts/<capability>/<stage>/`
inside the host's artifact scope, and seals the name mapping as `part_io`; the
host capability still owns the artifacts, and a token that is not a registered
shareable part is the same unknown-node refusal as any other. A human gate a kept node
raises is rebound to the entry of the node that now follows it and dropped
when nothing follows; a declared parallel group survives only when its anchor
is kept and is not the new terminal. Composition changes route *shape* only —
the write, spec, and core gates, the dispatch-depth-3 ban, and every
completion gate are unchanged, and a preset-free route is still a route the
§0.4 entry confirmation and explicit-workflow participation rule apply to.

For an approved source-changing `--start`, the shared preparation boundary
resolves the selected graph first, prepares a new independent worktree when the
caller is in a primary checkout, then collects eligibility and launch evidence
from that final cwd before sealing or publishing. `compose` runs the same probe an owner launch re-checks,
so it is called from the dispatch-depth-0 session in the final worktree; inside
a registered owner (`AGENT_DISPATCH_DEPTH` ≠ 0) it needs `--dispatch-evidence`
or `--registered-headless-evidence` from the parent, exactly like `compile`.
The stderr `[경로]` notice (§0.4) keeps the campaign choice observed before
compose starts, and accompanies an admitted first leg. When the owner that
started is not the harness the work asked for (`--owner`/owner pin), the notice
adds one line naming the request, the harness and model that started, and why
(`담당 변경: 요청 codex → opencode <model>(deep) — codex 사용 한도 소진(<reset> 재설정)`),
and the per-harness owner advisory follows the harness that started. The
switch itself is unchanged and needs no approval. A no-wait snapshot is
pending; only an elapsed bounded wait is a timeout.

Start receipts for open work include `runtime.run_command` and, when the adapter
provides one, `runtime.preflight_command`, using the already resolved harness root.
Reuse those prefixes without rediscovering the installation; `capability-info`
and tool availability checks retain their existing meaning.

### 0.3. Pre-Execution Gate for Long-Running Work

Before starting a long-running command, GPU or checkpoint evaluation, bulk
figure/media generation, or a full report regeneration, compose displays these
seven answers from the selected route. Unobserved meaning or runtime limits
remain explicitly unknown; the display is not an approval or a new launch gate.
The main session resolves any material unknown from the task's existing evidence:

1. What is the semantic primary capability under §0.2?
2. Does the work create new empirical output?
3. Is the intensity `standard+`?
4. Are two or more separable stages present under `OPERATIONS §5.10`
   separability?
5. If main intends to run anything inline, which recorded exception applies?
6. Have native sub-agent limits and headless worker limits been checked as
   separate surfaces (`OPERATIONS §5.10` delegation surfaces)?
7. Does the plan preserve existing experiment lineage and the append-only
   `_RUNLOG`?

A gate answer that selects dispatch follows `OPERATIONS §5.10` registry and
liveness rules; an inline answer for `standard+` separable work requires the
recorded reason.

### 0.4. Primary Entry Confirmation

For material work, the main agent shows the intent sealed at compose and the
user confirms it before capability execution begins. The agent writes the
prompt file's leading intent lines from the request and recovered context
(Sealed intent, below); compose adds the person's words explicitly quoted
in that route's task and prints them as
`[의도]` lines. The card shows those same values, so the user reviews a
completed proposal rather than recalling capability names, invocation syntax,
or pipeline options, and the card and the route say the same thing.

Render labels in the user's communication language while preserving these
fields and this order. In Korean, the canonical card is:

```text
[의도 확인]

원문: "<compose가 붙인 사람 말 발췌>"
의도: <무엇을 어떤 결과로 만들지>
범위: <포함 범위 — 결과를 쓸 폴더는 경로로>
안 할 것: <중요한 제외 대상 또는 없음>
완료: <산출물과 검증 기준>
경로: <primary entry capability> · <mode/intensity> — <선택 이유>
시작 승인 (필요 시): <선택한 경로가 표시한 승인 대상 부품 — 이 leg의 실행 범위>

→ 진행 / 수정: <틀린 부분> / 중단
```

In English, the same fields in the same order are:

```text
[Intent confirmation]

Original: "<the excerpt of the person's words compose kept>"
Intent: <what to produce, and the expected result>
Scope: <what is included — name the folder the result is written to as a path>
Not doing: <the important exclusions, or none>
Done: <deliverables and verification criteria>
Route: <primary entry capability> · <mode/intensity> — <why this route>
Start approval (if needed): <the marked part this route asks approval for — this leg's execution scope>

→ Proceed / Edit: <what is wrong> / Stop
```

The card follows the user's communication language; the shared UI-text
selection (`tools/i18n/ui_text.py`) governs only text the tools print, such as
the `[경로]` notice, whose marker prints as `[Route]` in English.

`원문` (Original) appears only when the task explicitly quotes the person's words;
an absent or uncertain quotation is omitted, including old unbound excerpts.
`경로` (Route) is
information, not a second question. `범위` (Scope) stays the approved scope that
execution access reads for an owner's write roots (`OPERATIONS` execution
access). `compose --explain` prints the same `[의도]` lines without writing
anything, so the card can come before `compose --start`. When the runtime
exposes a native structured-question surface (Claude `AskUserQuestion`, Codex
`request_user_input`, OpenCode `question`), deliver this confirmation through
it: the card's fields form the question body; the optional start-approval line
is scope metadata, and the options are exactly 진행 (recommended) / 수정 / 중단
(in English Proceed (recommended) / Edit / Stop). The plain-text card above is the fallback when
no such surface exists or it fails. The structured form changes only the
delivery surface — never the fields, their order, the one-time approval
semantics, or the exemptions below.

Keep each confirmation value to one line. Include the start-approval line only
when the route carries marked parts; it is not an additional required answer.
Do not include alternative menus, internal
sub-Skills, or extended reasoning. The card applies when any of these observable
conditions holds:

1. source, document, configuration, or a durable artifact will be created or
   changed;
2. two or more of research, analysis, implementation, or verification are
   needed;
3. the work requires a test, build, deploy, external-system mutation, or
   separate correctness evidence; or
4. a spec-backed project will create or update a capability-owned artifact.

Read-only orientation, status reporting, explanations, and simple factual
answers are exempt. Card exemption is not evidence exemption: an explanatory
or factual answer still follows `roles/response-policy.md` "Local evidence
before recall" when the repository holds covering research or document
artifacts. Tiny fixes under §0.2.1 use automatic evidence and an ordinary reply; an
explicit `direct` route still appears in the card with its reason.

The optional `시작 승인` line appears when the selected route carries parts
marked with `start_approval`. Fold the choice into this existing confirmation:
complete the shown scope, or run it through its report point and stop. The
report points are lab scaffold+smoke, ship's two preparation reviews, refine's
review+preview, and apply's isolated apply+verify. Report is a normal execution
choice, distinct from decline, off-menu, and no answer. It does not start the
excluded tail or next leg. The same scope and answer stay in the existing
intent, decision record, or work request; no owner asks for another entry
approval mid-run. New compiles finish their chosen scope without an intermediate
approval wait. Existing sealed routes retain their gates and continuation.

**Small-work confirmation (SD-136).** User-authored turns receive the card even
for `direct` or `solo` (`quick`). A received peer envelope (the existing
`(peer-from: …)` trailer or notice path) uses one non-blocking `[경로]` line
`compose` prints (capability · shape · route id · human gates) plus one clause
of scope and proceeds in the same turn; a steward cannot approve for the user.
Compose's notice and receipt name this confirmation method from the current
turn's existing peer-receive observation. Peer origin is known only when the
current prompt boundary is bound to the exact validated transfer or native
Claude envelope it contains. A previous receipt or a later notice cannot
classify this prompt. Runtime completion transfers retain their producer
binding and runtime source independently of the courier. Unknown or mixed
origin uses the card method;
the label is observational, with existing approval and routing unchanged.
`confirmation.small_work` and its sealed value retain their existing route
metadata meaning, without changing this distinction. The card stays blocking,
whatever the sealed value, when the work is destructive (data, history, or
worktree loss), mutates an external system, deploys, or the user asked to be
asked. The
route-participation rule below applies to explicit workflows; small local
completion units satisfying §0.2.1 use the automatic record, and the 0–1 / 1–3 inline questions of the frame
interview (below) are asked only when they exist. A current or immediately preceding user
instruction that already approves the same route and scope satisfies the gate;
do not repeat the card. After approval, capability-owned stages, validation,
records, commits, dispatch, and handoffs proceed without further confirmation.
Reconfirm only a material change to the primary capability, scope, completion
criterion, destructive risk, or touched external system.

**Sealed intent.** The prompt file's optional leading lines `의도:` /
`Intent:`, `범위:` / `Scope:`, `안 할 것:` / `Not doing:` (up to three,
`;`-separated) and `완료:` / `Done:` are the intent; they are read from the
sealed request text whenever needed, and without an intent line the request's
first sentence is a marked draft. When a route is first written, the runtime
may keep the person's words explicitly quoted in its assignment in
`.runtime/intent/<route id>.json` beside the route, outside its identity, with
credentials masked and the first write kept. A latest session utterance is not
proof of this task's intent. Old unbound excerpts remain stored but are not
displayed. Every leg uses its confirmed frame assignment, not the session. Nothing is
required or refused. The `[경로]` notice shows these as `[의도]` lines, and a
route without a frame-review release hands them to its owner and every worker.
Questions and gates are unchanged.

Source work remains traceable through an explicit workflow or the §0.2.1
automatic record. Ordinary main-session edits and commits in the current Git
worktree pass without a route-presence refusal. The hook does not reclassify
size. Other-worktree edits and long-running launches retain the existing route
check; installed-release/shared-checkout protections and native permissions
remain independent. The historical /tmp gate exemption remains; only the
recorder includes real /tmp Git projects. Registered workers, artifact/runtime roots, CI, dev
activation and judgment failures retain their exemptions.

At turn end the existing detached recorder stores changes, bounded native
command termination facts, and the final reply with source references. It uses
the existing automatic direct route in its associated campaign (otherwise the unassigned container) and appends
later changing turns. Open explicit workflows also retain bounded exact-session observations for
Fleet and continuation, without closing or taking over their routes. The route-side
`.held.md` has a 256 KiB total cap as well as its 64 KiB append cap; oldest
history is trimmed first with a visible truncation marker. Git snapshots run
outside the session state lock, and results apply only to their matching turn.
Unreadable or superseded observations stay unknown. Active
explicit workflows keep their procedure; proven
closed historical routes no longer suppress new automatic records, and unknown
route state is not treated as closed. Actual Git projects under `/tmp` record
too. `HEARTING_AUTO_RECORD=off` still disables recording; no model, per-tool
process, question, refusal or required answer format is added. Automatic records
supplement Fleet, continuation and tidy readers for the exact native session or
confirmed lineage; they never replace an explicit route's default or authority.

Compute-host storage access follows the `execution_needs` declaration on the
registered part catalog row in `capabilities/topologies.json`. Native and
borrowed route nodes resolve the same row from their sealed stage identity;
the need is not copied into sealed nodes or inferred from the outer
capability, node name, or task text. Routes without a declared compute stage
retain their ordinary execution access. The `host-process` need is a separate
vocabulary entry for a GPU-free resource payload: it grants no compute-host
storage or GPU scope and only selects that node's payload launch.

Before approval, choose from compact manifest routing metadata and §0.2; do not
load the full entry Skill body or its references merely to propose a route. At
`standard+`, the dispatch-depth-1 owner reads the selected capability contract and each
dispatch-depth-2 worker reads only its stage contract. `direct` or `quick` acting
sessions read `capabilities/<name>.md` directly after approval; the entry Skill is explicit-only. If a runtime automatically
injects a selected Skill body into main, do not duplicate that read; record the
runtime limitation rather than claiming total-token savings.

**Two-stage confirmation (SD-123).** For the five frame recipes at `quick+`,
depth-0 submits `compose --start`; the runtime first compiles/binds the route,
issues its producer cycle, and launches the two frame nodes. These steps authorize frame artifacts, not owner
execution. After both briefs arrive, depth-0 presents this blocking direction
card and the interview. User approval precedes the owner and the §0.4 notice:

```text
[방향 확인]

방향: <채택한 방향 한 줄>
대안: <기각한 대안과 이유>
위험: <frame이 찾은 최대 위험·가정>
범위 변경: <처음 요청 대비 증감, 없으면 "없음">
비용: <frame 실소비·하네스 구성과 남은 단계·강도>

→ 진행(권장) / 수정: <틀린 부분> / 중단
```

Deliver this card through the native question tool (Claude `AskUserQuestion`,
Codex `request_user_input`, OpenCode `question`) when one is
available, the plain-text form otherwise — the same fallback rule as the §0.4
card. Only once the direction is confirmed does the owner start, and the §0.4
gate then arrives as a non-blocking `[실행 통지]` — the same card fields, in
order. It announces the route the confirmed direction produced; it does not
re-ask a direction the user has already settled.
`confirmation.mode` (`profiles/dispatch-defaults.yaml`, default `hybrid`)
governs that ordered pair: `hybrid` is the shape above (blocking direction
gate first, notice after), `both` makes the later notice blocking as well, and
`post-frame-only` drops the notice entirely. A route compiled before this
cycle keeps `frame.continuation=inline-next` and is never retro-fitted onto
this gate.

**The frame interview (SD-129).** The `[방향 확인]` card is not the whole
gate. The gate record names its exact cycle-local interview: a one-sentence
restatement of what the user wants, a plain-language brief, and the few
decisions the frame legs could not settle without the user. The depth-0
session builds that record from the joined frame legs and does the interview
itself, and never leaves it to a helper.

Scale a frame brief to the decision that remains. A small task whose direction
is already fixed by the request and evidence keeps the eight-section schema
with concise evidence and the sole admissible direction; it needs no invented
alternatives. Ordinary defaults that leave the plan unchanged are chosen by
the agent, recorded in the brief and carried into the executing leg's final
answer, rather than becoming interview questions. Decisions that change the
goal, scope, architecture, cost or an external outcome still go to the person.
Larger or unsettled work retains its full direction comparison, independent
perspectives and model grades.

The interview brief may leave `affected` empty when the direction brief does
not identify affected users or systems; unspecified scope stays unspecified.
For answers, set `actor_kind: user` only for the person's actual reply, or
`actor_kind: supervisor` when the depth-0 supervisor answers on the person's
behalf (including adopting a recommendation). The answer template keeps
`actor_kind: unknown` until its source is explicitly recorded; the separate
`supervisor_answers_template` already carries `actor_kind: supervisor`. Unmarked answers
remain unknown, and registered workers cannot claim either release authority.
A supervisor answer releases frame-review with its source preserved in intent;
person-only start approvals keep their existing gates. A direction that crosses
the intent's `안 할 것` (§0.4: the chosen proposal's summary or parts say they do
one of its items, apart from clauses that keep away from it or hand it over) is
the person's to choose: a supervisor answer choosing it is not recorded and the
same registered question goes to the person. This meaning is shared
by Claude, Codex and OpenCode.

When there is nothing to choose — both frame legs return valid, equal
proposals, no start approval is in scope, and neither brief's section 7 lists a
question only the user can answer — the runtime records that interview itself as
the depth-0 session's on-behalf answer (`supervisor`, basis
`automatic-agreement`), releases frame-review through the same commands, and
the first leg's start receipt (each repeat too) carries a one-line direction
notice (`[Direction notice]`, `[방향 통지]` when Korean is declared) the session
shows in its reply; nothing waits. A hand-written answer cannot
carry that basis. Every other case asks as below.

The interview proceeds in this order:

1. Ask first whether the restatement is right — that sentence, verbatim,
   with 예 / 아니오(고쳐 말하기) — and record a correction in the user's words.
2. Put the `[방향 확인]` five-field summary as the card above.
3. Ask the understanding sentence first, then the registered interview
   questions, in one native question call when the tool accepts them all;
   otherwise continue with the remaining questions verbatim in the next call,
   and the runtime combines the calls of one interview. Use Claude `AskUserQuestion`,
   Codex `request_user_input`, or OpenCode `question`, one topic
   per question, the recommended option first and labelled
   (권장), each option with its one-line meaning; never paraphrase a question
   into harness vocabulary, and never add questions the interview does not
   carry. A tired reader must be able to answer without opening the plan.
4. The native tool's reply is kept by the harness's post-tool hook beside the
   registered question (`answers.native.json`; the restatement asked verbatim,
   its first option confirming it — a partial reply waits beside it until every
   registered question has an answer), so `resume_command` alone continues. A
   reply given in conversation goes into the returned `answers_template` and
   `start --route <file> --answers <file>`. The runtime renders intent and
   records the release that authorizes owner launch. A repeated submission
   reuses it.

`OPERATIONS §5.10b` owns selector mechanics and the one-time `top`→`deep`
demotion; the public work entry owns launch calls and artifact context. Both legs
are always waited for — past the hard limit depth-0 stops and asks, rather
than proceeding on one — and differing direction verdicts go side by side,
nothing downstream starting until the user picks one. Step 1 is asked even
when the interview carries zero questions; `understanding_confirmed` records
that answer.

Question counts are bounded by intensity, and `QUESTION_CAP` in
`utilities/frame_interview.py` owns those numbers — do not restate them here.
The validator refuses an interview that breaks the plain-language rules
before it reaches anyone; the cap and the wording rules are machine-checked at
the raise for every route carrying the gate, `quick` included, while steps 1–3
above stay obligations on the acting session that nothing checks mechanically.
Only code/design/draft/refine/spec recipes carry the frame pair at `quick` and
above, before the owner, when they run as a preset (`compile`); the `framed`
shape (§0.2.1) runs the same pair ahead of any non-direct work, and a `solo` or
`staged` compose has none. Other recipes retain their topology. `direct` asks its
question inline in the §0.4 card and records the answer in the plan or work log.
The recorded answers become `shards/frame/intent.md`, rendered by depth-0.
The runtime supplies the released task and decisions to the owner and every
subsequent worker, including graphs without `plan`. The assigned stage narrows
that task; a role preset does not replace it. Workers cite applicable question
ids and report decisions they cannot honor. An interview gate may be raised at most twice
per route (`round` ≤ 2).

**The framed interview is the one confirmation (SD-164).** For `framed` work
the interview above replaces the §0.4 card and the `[방향 확인]` card: one route
question and the start-approval questions sit inside the same interview. Its
answers cover the route and the start approvals of the parts the person actually
saw in it, for every leg of the selected proposal: each leg composed from the
same decision seals the same execution scope, and a start approval given for that
leg removes its intermediate wait. A later leg's unapproved part, a `next_leg`
the main session changed, and a deploy under `small_work_confirmation=notice`
still go through the card above; neither the owner nor a worker waits for an entry
approval mid-run.

Entry routers therefore have two deterministic load phases: manifest-owned
metadata before approval, then the selected portable owner contract after
approval. A router may expose one direct owner-reference index, but no
pre-approval reference may contain execution procedure. The confirmation is
one-time for an unchanged approved route and scope.

An existing decision that fixes the work carries any actual understanding-check
answer through the existing stage input without an extra frame or second
approval; an absent answer stays absent.

### 0.5. Post-Execution Completion Report

After a material-work attempt, the user-facing main agent's first close gives
the result in one or two sentences, names any remaining failure, unfinished
work, or user-impacting limit, asks for at most one decision the person must
make in the self-contained form of `roles/response-policy.md` Decision requests
(a clear recommended follow-up is reported as proceeding, not asked), and gives
one pointer to the detailed record. Preserve unknowns and explicitly requested
output or format. Render any labels in the
user's communication language. In Korean, one optional presentation is:

```text
[완료 보고]
<결과 한두 문장; 남은 사항과 필요한 결정이 있으면 포함>
기록: <상세 내용>
```

An optional decision request has this shape:

```text
[결정 필요] <무엇을 정하는지 한 문장>
- <안 1>: <고르면 달라지는 것>
- <안 2>: <고르면 달라지는 것>
추천: <안> — <이유 한 줄>
답이 없으면: <기다리는 일>은 멈춰 있고, <다른 일>은 계속합니다.
```

Preserve the honest outcome (completed, partial, failed, or blocked), what the
person needs to know about what remains, and the decision they need to make.
Keep detailed changes, comparisons, checks, deployment
state, and supporting evidence in the record; the runtime summary is preserved
there and carried as agent context, not text to reproduce for the person.
Deployment and operating claims require observed evidence. Preserve explicit
user-requested content and output formats even when they differ from the default.
Owner correction receipts distinguish delivery from verified application and say
that a one-shot owner's next turn is not guaranteed. Completion reports count
inputs without delivery evidence (`미반영 correct N건` in Korean), separating
unknown delivery and application. All three adapters share this behavior.
A continuation or recovery notice resumes that same original task; its receipt
adds context but does not replace, narrow, or discharge the user's requested
content, evidence, or output format.
Writing a report file does not by itself complete the user-facing report.

**Runtime completion summary.** When the runtime seals a registered owner's
completion it also writes `completion-summary.md`, once, beside the owner
envelope: the intent (§0.4) with any task-bound quotation, the result (one leg of a
plan says a later leg remains), one cost line (time from the route's first
registered attempt to the seal, and the session tokens its owner, workers and
retries wrote into their own attempt logs, summed; a session that wrote none is
counted as `unknown`, and one whose started turn or step left no count makes
the total `at least`; the line shows token counts, not a share of a plan), the
owner harness and model that ran when they differ from the requested owner, what
changed, what was checked, what stayed
unverified or remains, nodes closed without an independent review, and the
artifact path, in the shared UI-text catalog's language (English by default,
Korean when declared). Section meaning, rather than ordinal numbers, capability
or filename, determines its summary field. Mixed or unknown headings stay
unclassified and linked to the report; missing extracted checks or remaining
work mean unknown, not absent. Existing report formats remain valid. The Claude
wake receipt, Codex queue item, and OpenCode carrier preserve this text for
agent context and record keeping; it does not change the short first-close
contract above.

If any review gate of the work closed without an independent reviewer's PASS —
the route outcome's `review_independence_degraded`, or a
`completed-review-degraded` line from `complete` — `검증` must say so and name
the node. That list covers both `degraded` (the owner reviewed its own work) and
`owner-overridden` (the owner ruled over a review that returned FAIL). Either is
a real result and neither blocks the route, but reporting it as review would
make "reviewed" mean nothing (`OPERATIONS §5.10`, SD-OPEN-41(b)). Within the
`degraded` case, a marker whose `round_census.closure_class` reads
`owner-override-unlinked` names the sharper shape — an inline completion that
landed directly over an unresolved blocking FAIL without the evidence-linked
owner-closure procedure — and `검증` should say so by that name (SD-153 rule 5,
SD-134 A75-9).

For dispatched work, the parent follows the runtime's next-action receipt and
reads the completed artifact to accept the results, limits, and remaining duties.
Reuse valid verification and independent-review results; a stage, person, folder,
or interruption change alone does not require another audit or suite. Check only
uncovered integration changes, unresolved failures, or required unknowns. A broad
rerun needs a concrete broad impact not covered by existing results. Current
stage judgments still apply to their current subject; an old PASS marker does
not become current through evidence reuse. When the requested outcome has
sufficient evidence, advance to the authorized handoff, integration, or closure.
Normal success needs no harvest.
A process exit or intermediate stage verdict alone is not task completion.
Read-only orientation and status replies use concise prose instead.

New registered owners carry `workflow_completion=runtime-v1`. Their completion
controller owns the exact workflow/route/cycle closing transaction after PASS
and child cleanup. It records COMPLETE only after sealing, retries interrupted
closure without a model turn and sends a recovery notice while closure remains
pending. The owner and parent have no separate close/finalize procedure.

The runtime closes a route once nobody works on it; no session has to
remember to. `compose` (within the composed route's own campaign) closes an
open route whose Claude
session this host saw end and that then stayed quiet for an hour, or that saw
no writes for a week; `campaign-close` first sweeps the same way and then also
closes its campaign's member routes
that belong to the closing session or stayed quiet for an hour. Routes of the
calling session, routes a dispatch attempt, a pending owner settlement or an
unended resource run still holds, routes waiting on a human gate and routes
whose cycle a live process holds open stay open, as does everything when that
evidence cannot be read. The closure claims no proof: it records what the
completion markers show, seals the cycle completed only when that proof holds
and abandoned otherwise, and names its reason in `autoclose`. A session that
returns to such a route is not refused: `finish` reports it closed, and
`start` returns the compose command that begins the work again.
`campaign-status` is a pure read: it reports folded state, pending projections,
and close refusals without sweeping routes, reconciling the root, healing the
index, or flushing history.

A session finishes its explicitly composed `direct` inline route to record real evidence.
The tiny-fix path has no agent-facing finish command; its recorder saves the evidence.
Supply a readable, nonempty regular file anywhere, or an http(s) URL, and the
final summary. A new small direct route records evidence in its campaign daily
journal (CONVENTIONS “Small-work daily journal”), binding an immutable entry
revision with a versioned receipt. Existing cycle-bound routes keep their cycle
proof. Shared-spec authoring and other cycle-requiring contracts retain logical
cycles; small planned direct work links its journal entry to the request. For a cycle-bound route, finish
records that evidence inside the route's own cycle (a
file byte for byte, a URL as a one-line record) under a runtime name, and that
copy is what the terminal proof, manifest and producer binding name. If the
route has no cycle yet because its start was interrupted, finish prepares it
exactly as direct start does; no `AGENT_ARTIFACT_*` value is needed. Symbolic
links, directories and empty files are refused, and a claimed finish keeps
its copy as recorded: changed bytes are `finish-evidence-drift`, and the same
bytes resume. The command records terminal proof, closes the route, finalizes
that cycle and returns a receipt only after rechecking all three. A partial
transaction remains `finish-pending` and is resumed with the same intent.
The synthetic `source-scoped` authority does not make unrelated tracked edits
a finish veto; concrete declared files, directories and path patterns remain
guarded, with tracked status read again under the finish lock. The returned
result may report unscoped tracked dirt as an observation, without attributing
authorship or changing the stored receipt. Finish preserves the worktree and
index, while its evidence, route/session identity, commit lineage and replay
checks remain in force. Review fixes pushed to the same PR branch after the
finish are recorded by the same command: the finishing session, the same
evidence and a commit that descends from the recorded one add an update (new
commit and summary) beside the finish, which stays as it was; anything else
still conflicts.

```text
python3 utilities/capability-route.py finish [--route <route.json|rt-id>] --evidence <file|http(s)-url> --summary-file <file> [--commit <full-sha>]
```

Without `--route` it finishes this session's latest route under the cwd's
artifact root.
An explicit route ID also uses the existing registry/route-chain lookup when
the caller is in another folder. Every `execute-inline` receipt names the
actual execution cwd, including a worktree prepared for a frame leg.
Git finish prefers a proven changed caller head when the route checkout still
has only its starting tree and a clean working tree/index; explicit commits remain binding.
Execution edits keep their existing tracked-dirt observation and concrete-scope
checks even when a commit is read from another checkout. Caller substitution
uses Git common-directory identity as proof, independently of HEAD lookup.
Compose's optional directory provenance supports a missing checkout only when
the caller still belongs to that recorded repository. An ancestor shared with a
different clone is insufficient; an unavailable observation stays unknown.
Receipts name the
commit source and warn when it comes from a different checkout or has the
unchanged starting tree, without adding a completion gate. Normal execution
checkout results need no warning. Non-Git finish keeps its null commit.

Ineligible inline routes and legacy recovery also finish with the one command
above (it completes the terminal marker, closes the route allowing an unproven
gate, and finalizes the cycle when one is bound); the explicit guarded
completion and close steps below keep working for recovery consoles:

```text
python3 utilities/capability-route.py close --route <route.json> [--commit <sha>] [--summary <line>]
python3 utilities/capability-route.py status [--artifact-root <dir>] --open-only
python3 utilities/capability-route.py status <route id | route file | attempt id>
```

`status` defaults the artifact root from the cwd, and each open row carries its
`resume_command`. Given one route or attempt, it reports that route and its
registry attempts. `start`, `finish`, `close`, `correct` and `status` each take a
route id, route file or attempt id; `close` given an attempt that is not its route's
owner stops that attempt alone and leaves the route open (OPERATIONS "Parent close").
That command names a canonical route by its ID
(`start --route rt-<16 hex>`); `start` finds the record under the cwd's artifact
root, `AGENT_ARTIFACT_ROOT`, the jobs registry or a session's route-chain ledger,
and still takes a route file. A `start` without `--route` continues this session's newest
open route (its route-chain ledger), or else the one open route sealed for the
cwd (or, with none there, for the worktree compose made for it, `<repo>-wt/<slug>`);
with none or several
it lists them and starts nothing.

`close` may preserve `terminal_gate_proven=false`; later exact completion on the
same route is consumed by complete/finish/close/finalize (artifact-path-contract
§46). An owner-pin handoff settles the predecessor's execution while preserving
its cycle and the preserved resource runs of the nodes it resumes for the hash-bound successor; it does not mean the task was abandoned.
Historical `abandoned` is reconsidered only when that complete handoff lineage
and the successor's exact terminal binding verify. Identity conflicts, live or
unknown work, explicit stop/CANCELLED, and other abandoned/autoclose outcomes
remain unpromoted. `--allow-unproven` keeps its meaning.

For producer-backed work, the controller's transaction is **terminal proof →
route close → cycle finalize (the completion record) → workflow COMPLETE**. Eligible direct inline
work uses `finish`; legacy recovery uses complete, close and finalize in that
order. For completed `autopilot-spec`, the same runtime completion/retry path
admits the official spec through the producer's existing shared-admission API
after finalize; a completed `autopilot-code` cycle whose spec transaction changed
its seeded spec does the same. Admission is not a terminal gate: publication
failure preserves the closed route, sealed cycle and PASS, and does not hold the
completion either. The completion is delivered once, its sealed summary names
the unpublished spec on one line, and the next normal `start` of the same route
retries admission from the original source/base. Status derives missing publication
from cycle-to-shared revision lineage, including cycles completed before this
integration. Research promotion remains explicit; abandoned/open work is not
published automatically.
The spec's user-facing representative is its actual canonical PRD (root or
declared component), while a terminal REPORT remains completion evidence.
Explicit valid primary choices retain their authority; a terminal artifact
pointer alone is not a user primary choice. With no official PRD, retain the
honest existing artifact fallback and expose the missing spec publication.
Shared admission applies only to shared kinds after finalize; it is never a
prerequisite for the terminal marker.
Finalize records the cycle's completion as of that moment and does not lock the
cycle: its files stay editable, movable and deletable afterwards (artifact-path-contract §45).
Open-route finalize is provisionally `active`; exact completion and cleanup let
finalize or refresh publish a new `completed` revision for that cycle, even with
unchanged payload, preserving prior history (artifact-path-contract §46).
Explicit abandoned cycles remain unpromoted.

During the same session, a verified OPEN route for the same named campaign may
also govern writes in a related integration worktree. The guard requires the
same canonical repository and artifact root plus proof that the route branch
was actually merged: an exact merge command, matching in-progress
`MERGE_HEAD`, or a descendant of the exact merge or recorded fast-forward.
It keeps the route's source, scope, core, spec and recall checks. A sibling
branch with only a shared ancestor, a different campaign, or a closed route
requires a new route; integration reuse needs no manual bind command.

`close` writes an outcome sidecar beside the immutable route record — the record
itself cannot carry the closure, because `route_hash` covers every other field.
`status --open-only` then answers "what was started and never finished" directly.
An unclosed route is indistinguishable from abandoned work, and the same applies
to the other things a finished attempt leaves behind: a merged worktree and
branch, a spec `pipeline_state.yaml` still naming a live phase, and a memory
handoff still `pending` after its obligation is met. Registered delivery
reconciliation expires only the exact record whose original recipient has
positive dead PID/start evidence; a closed identity-matched route settles its
bound duty as not-required while retaining the record. Unknown liveness and a
live successor remain pending, and unchanged observations are not rewritten.
Close what the attempt opened, or the next session pays for it in reconstruction.

### 0.6. Tracked-Workflow Lifecycle and Continuation

A process exiting is not a workflow completing. **Workflow completion is the
state in which every terminal node of the approved stage DAG holds its
completion gate and no human gate remains open.** An intermediate stage that
succeeded — a training run that reached its last epoch, a green CI check, a
worker that returned `PASS` — is stage evidence, never workflow completion. No
acting agent, dispatch-depth-1 owner, supervisor, or runtime lifecycle hook may
declare completion from an intermediate success. Receiving an experiment result
is not interpreting it: the owner or supervisor that receives it interprets it
before proposing another run (`capabilities/autopilot-lab.md`).

Committed success and remaining cleanup are separate facts. A marker does not
make a live descendant quiescent: the execution boundary retains cleanup,
and the common supervisor retains the wait or recovery notice until it ends.
For an open resource row, an exit sentinel has the same boundary: a populated
or unobservable recorded process group remains pending; exact prior-boot
evidence or a complete empty-group observation can establish execution end.
Exact inspection reads the worker's result; requesting more failure detail is
optional and cannot become a second completion gate.

A retryable nonzero resource exit may still carry exact saved outputs for its
declared non-resource verifier. The ordinary owner start can admit that
saved-only input after checking the original run, sentinel, process-group
quiescence, config receipt and output bytes. The resource remains failed until
the normal downstream independent-PASS settlement records consumption; no
resource-success marker is created to open the verifier.

On the same OPEN route, the current owner may start a new supervised resource
retry with `resource-runner start --compute-run <run-id>` after a completed
`compute-hosts` execution. Before creating the retry row, the runtime checks
the sealed command, inputs and provenance, exit evidence, output receipts, and
fresh remote process-group quiescence; it preserves the prior failure row and
sentinel. The owner's normal start admits verification separately; compute
exit is execution evidence only. The same command adopts a run that an owner of
this route launched directly with `compute-hosts run` (route provenance, no
resource row) when it is this route's finished, unadopted run; other evidence
is recorded, not required (see `core/OPERATIONS.md`).

When an owner is replaced on the same route, its launched replacement may
observe and consume the predecessor's supervised-resource result through the
shared replacement and owner-route lineage. The resource record and receipt
stay attached to the owner that launched it. The current owner's input lease,
pending corrections, exact resource evidence and the existing once-only
successor claim are rechecked at ordinary start; an unlaunched claim does not
transfer consumption authority.
The runtime acknowledges a delivered notification after its receiving turn
finishes. Claude PostToolUse events with a nonempty `agent_id` belong to a
subagent or teammate even when `session_id` names the parent conversation; they
record outbound SendMessage calls but leave inbound parent context pending for
the main session. `agent_type` alone does not identify that case. Delivery does
not restrict the owner's tools or prove stage success;
launch dependencies, write authorization and terminal cleanup scopes own those
decisions. Repeating a notification cannot declare the owner abandoned.

A steward's idle-notify subscription is observation, not a continuation; the
registered-continuation obligation below still applies (`core/OPERATIONS.md §5.14`).
The Claude gate carrier may claim, emit, or settle a gate only while it still
holds the arm and effective seat; a handed-away carrier leaves the gate record
for its successor.

This section is capability-independent. `autopilot-lab`, `autopilot-code`,
`autopilot-ship`, spec/research, CI and GitHub check cycles, external-state
monitors, detached resource processes, registered workers, and loop-driven work
all use the one state machine below. A capability's stage graph *extends* this
contract by declaring nodes, gates, and continuations; it never redefines what
continuation or completion mean.

**Common states.** `capabilities/topologies.json` is the machine-readable
vocabulary and `utilities/workflow_state.py` is its executable form:

```text
CREATED → READY → RUNNING → STAGE_SUCCEEDED → NEXT_REGISTERED
        → NEXT_RUNNING → TERMINAL_VERIFY → COMPLETE
```

Failure states are `BLOCKED_HUMAN_GATE`, `FAILED_RETRYABLE`,
`FAILED_TERMINAL`, and `CANCELLED`. `COMPLETE` is reachable only from
`TERMINAL_VERIFY`, and `TERMINAL_VERIFY` only once every declared terminal node
carries its completion marker — so a workflow cannot become `COMPLETE` before
its terminal node. `BLOCKED_HUMAN_GATE` never advances automatically; only an
explicit human release returns it to `RUNNING`. `FAILED_*` never advances a
downstream stage.

The completion writer validates and records the remaining legal path through
`STAGE_SUCCEEDED` and `TERMINAL_VERIFY` to `COMPLETE`. Repeated or interrupted
closure resumes from the journal. Markers cannot erase unresolved human gates,
failures, or cancellation.
If an owned resource exited successfully but its declared output arrived after
the watch recorded it missing, exact owner settlement rechecks the same resource
and the now-present output and records that resolved observation in the journal.
It neither reruns the resource nor launches a successor; actual resource failure,
changed evidence, and missing outputs remain unresolved.

**Every non-terminal stage declares exactly one continuation.** A stage graph
that leaves a stage with no way to reach the next one is the defect this
contract exists to prevent, so the declaration is mechanical, not editorial:

| Continuation | Meaning |
|---|---|
| `inline-next` | the same checked payload runs the next stage before it returns |
| `supervised` | a registered continuation supervisor observes child termination and starts the next stage exactly once |
| `human-gate` | an explicit human gate named in the recipe's `human_gates` blocks the successor — e.g. each recipe's depth-1 bootstrap frame pair continues into gate `frame-review`, which fences that recipe's first work node and releases with `workflow-supervisor.py release --gate frame-review --decision proceed\|revise\|stop` (§0.4, SD-123) |
| `monitor` | a checked monitor waits on an external state change and reports a typed condition match |

A detached resource process can never continue itself, so a `resource-runner`
node must declare `supervised` and may never be a terminal node. A node with no
dependents must declare `terminal: true` with its `terminal_gate`; a node with
dependents must declare a continuation and must not declare `terminal`. Every
declared human gate binds to the exact node it gates (`entry` before that node,
`terminal` after the terminal node). **A recipe or composed graph that violates
any of these is rejected at route compile, and a launch bound to such a graph is
rejected before the process starts** — the refusal is the point, because the
alternative is silent abandonment.

An already verified run's same-code/config resume, epoch extension or repeat is
the smaller `autopilot-lab` graph `resume-run,run-verify`, including from a
`direct` or `solo` request. It has no pre-run frame, owner, scaffold, smoke or
new human decision. The resource starts once under the existing sentinel and
supervisor; only after its successful, evidenced exit does the existing
depth-1 one-shot execute the independent verification. This is the verification
surface of the small graph, not a fresh training recipe. New code, data or model
structure still uses the setup recipe. The graph records only the caller's
explicit approved verified-run scope; prose and command paths are not scope evidence.
Runtime identity stays in the resource registry and ledger. The supervisor reads
producer artifacts without replacing or synthesizing them; missing declared output
blocks verification.

A supervised route owner starts its ordinary detached resource through the same
runner. A sandboxed Codex owner tool records only the exact launch intent;
its outer controller owns the observable watch and exit wrapper, and runs the
payload with the owner's selected native sandbox. A GPU-free long-running host
measurement, benchmark, browser test or memory-capped command borrows
`autopilot-lab:host-run` (`execution_needs: host-process`, a detached supervised
resource-runner with no GPU lease, start approval or smoke attestation): the
Codex owner and its model children stay at their ordinary `workspace-write`, and
only that registered node's controller-built payload command runs as a host
process, so a payload-level `systemd-run --user --scope -p MemoryMax=… -p
MemorySwapMax=0` can reach the user manager. A caller or forced sandbox keeps
precedence, and an already delivered strict `os-sandbox` request is refused at
start instead of being weakened; no new gate, approval or input is added. Intent admission is not a
payload start. An exact controller-owned wrapper awaiting its parent's reap is
nonterminal; an independent watch cannot settle it from empty cmdline or sentinel
alone. Other runner paths retain their existing launch boundary. The
watch is ready before the once-only payload release. The owner yields the model
turn with `runtime_wait: registered-children`; the existing controller waits for
the exact resource outside that turn and resumes the same native owner from a
bounded receipt. Resource receipts are separate from registered model children.
After pending user corrections, the watch records the exact resource result and
returns it to the same owner. For a `supervised-owner` resource, even exit 0 with
all declared files present is only resource success: polling does not write the
stage completion marker or claim a successor. The owner can continue with another
approved resource in the same node. When the owner finishes that node's approved
scope, its ordinary `start` of the declared immediate successor passes through
the existing shared admission gate; that gate rechecks the live owner, resource,
pending corrections, and output evidence, then records stage success and claims
the successor once. This uses the existing start path and adds no close command
or required proof. Owner terminal settlement can reconcile an already-finished
node, without starting a successor. Exit and the stage marker are neither
verification PASS nor workflow completion.

A resource watch lasts while its recorded PID/start/command identity is alive,
without a fixed elapsed-time ceiling. The existing start/resume can reattach a
lost or expired watch to the same run without releasing a payload again. Explicit
parent cancellation still suppresses the preserved resource's successors.

One supervised owner stage may use resources through the existing runner. The
normal owner selects each semantic operation within the route's approved scope.
The runtime checks that the sealed route/resource node and existing owner or
controller authority still match, then binds the selected request and payload
at their actual layers: exact request bytes, local command record and bridge
hash; separately parsed remote `compute-hosts run` argv, cwd, explicit
environment, payload and input hashes. Owner-input id/digest/source/target and
accepted/completed turns remain provenance; freeform text is not rewritten as
operation JSON and is not parsed as a new approval condition. A route, run
name, manifest, or completed input-delivery receipt alone does not authorize a
second operation. A retry of the same operation still waits for its exact prior
execution to end and become quiescent. Every execution keeps its own registry
row, process/compute identity, watch, receipt, output evidence, and settlement
duty; one execution's PASS or failure cannot replace another's result. The
supervisor admits the node's existing successor only after every current
authorized operation and declared output duty is settled, and claims that
successor once. Historical records and unacknowledged receipts remain bound to
their original executions.

The legacy `<node>.json` armed watch remains readable. Its flat view may expose
a queued operation during the owner's next normal model turn; parked waits
leave it unchanged. Prior watch bytes and every execution remain lossless in
the current index/archive. Current readers validate both the old-readable
projection and the full current operation binding. Additional execution watches
remain outside the legacy flat `armed/*.json` view. Cached-consumer support is
demonstrated by its real launch, result-delivery, and aggregate-admission path;
additive files alone leave that support unproven.

Human decisions have no elapsed-time default. A question window closing or an
empty response does not release, reject, or cancel the durable gate. Its owner
keeps the exact question and gate available for a later real answer; independent
authorized work can continue. A question surface that submits an empty answer
after a timeout does not answer the gate; the question stays for a later real
answer. Otherwise the owner leaves a plain-text question for a later reply
instead of repeatedly opening expiring prompts.
An owner that raises a gate calls the bounded `await-release` once; when the gate
is still blocked it ends its turn with `verdict: BLOCKED`, naming the gate as its
blocker. That parked stop is a pause, not a failure, and no model turn runs while
the person decides. `resume_command` then reports `waiting-human-gate` with the
exact release command, and a person's `release --decision proceed` starts one
continuation owner when no owner is live. That continuation uses the route's one
automatic replacement, so a replacement or continuation owner, which its prompt's
recovery context identifies, keeps calling the bounded `await-release` at a later
gate instead of parking. A revise or stop recorded while the owner is parked keeps
its meaning and starts nothing automatically.
An owner that ends `BLOCKED` or with a malformed final handoff, outside any declared
gate and with no kept answer, is continued once by the runtime in a replacement owner
that receives the original result and its report as observations, not as an answer.
Only a real user decision reaches a person: the continuation names it and ends
`BLOCKED`, and `resume_command` then reports `owner-blocked`; the answer sent with
`correct` starts one replacement owner that receives it. A readable FAIL keeps
waiting for an approved fix. An owner that ended `PASS` with declared stages undone is
continued the same way once: its PASS stays as recorded, and the replacement receives the
undone stages as observations. No gate, close, or recompose is needed.

A continuation route projects this contract onto its suffix. It drops a gate
whose gated entry node was cut, never rebinds a retained raising continuation to
an arbitrary successor, and never treats a reused predecessor's completion as a
human decision. If the suffix keeps the gated entry but cuts its raiser, the
builder may remove the runtime gate only after sealing and revalidating the exact
source route, gate, raise epoch, proceed decision, journal authority, and
raise/release entry digests; otherwise continuation compilation fails closed.

**Acting-agent obligation.** Do not end a turn while a tracked workflow has a
non-terminal stage with no registered continuation. Before the turn ends,
either the continuation is registered (supervisor armed, next stage dispatched,
human gate recorded, or monitor armed) or the same turn states plainly that
automatic follow-up is impossible and names the checked fallback the user can
run. A promise to act "when it finishes" is not a continuation. Status claims
cross-check PID identity, sentinel/exit evidence, log modification time, and
declared artifacts; a registry status word alone is not state. Reaching a
terminal condition never widens authority: stop only for a human gate or a
genuinely new external permission.

`OPERATIONS §5.12` owns the supervisor, resource-lifecycle, and Fleet
projection mechanics; `CONVENTIONS §3` carries the cross-document invariants.

## 1. Four Tracks

```text
[research and experiment] research / analyze-project(code) → autopilot-spec ↻ → autopilot-code ↻ → autopilot-lab ↻
[library and CLI]         analyze-project → autopilot-spec ↻ → autopilot-code ↻
[documents]               research / analyze-project(paper or doc) → autopilot-draft → autopilot-refine ↻ → autopilot-apply
[apps]                    autopilot-spec ↻ → autopilot-design → autopilot-code ↻ → autopilot-ship ↻
```

`↻` marks an iteration point. Common post-work capabilities are read-only `audit` and Markdown correction through `autopilot-refine`. Cross-project profile updates go through `analyze-user` and `mem profile-append`.

## 1.1. Pipeline Intensity Routing

Autopilot entrypoints choose `intensity`; verification rigor is derived from it under `CONVENTIONS §1.1`, not from a separate `--qa` axis. Intensity selects the stage graph and dispatch depth, while derived rigor scales plan checks, selected independent review, and final verification.

| Request shape | Default | Routing |
|---|---|---|
| One-off answer, typo, rename, or explicit no-artifact work | `direct` | No plan stage, plan check, or durable plan |
| Small localized change that misses at least one atomic-direct predicate and has no promotion signal | `quick` | A depth-0-run bootstrap layer runs first — two frame legs with distinct personas and usage-aware harness selection, joined and interviewed directly by the depth-0 session, both `top`. Then a registered-headless dispatch-depth-1 one-shot conductor with orient-lite, micro-plan, plan-check-lite, focused verification, and concise report; no dispatch depth 2 |
| Work with a promotion signal or separable durable stages | `standard` | Same depth-0-run bootstrap frame layer as `quick`, then durable plan/checklist; a dispatch-depth-1 conductor (default `deep`) dispatches selected stages with file-only handoff and realizes only registry-declared parallel groups (plan/implementation-review, not frame) |
| Important multi-file or risk-bearing work | `strong` | A `deep` owner plus the declared plan/review groups; selected high-value anchors may widen to a third profile/perspective leg while other groups remain width two |
| Complex cross-domain or cross-harness work | `thorough` | Bounded dispatch-depth-2 perspective and verifier workers |
| High-stakes, irreversible, security, or external-facing work | `adversarial` | Thorough plus an explicit adversary, failure-mode, or security pass |

The quick row describes the lighter procedure, not a smaller deliverable. The original request, approved endpoint, and assigned scope remain the completion boundary: examples and concise reports do not replace requested investigation, results, or verification. A report endpoint ends with the approved report; a bounded stage finishes only its assigned work and reports any actual unfinished duty through the existing outcome semantics.

Only `direct` has no plan. Every other autopilot graph includes a plan check, but independent QA is not repeated after every sub-stage by default. Independent passes use route-declared bounded groups with separate execution contexts and distinct personas, retaining model-profile/perspective asymmetry where declared; every review — down to a `direct`/`quick` self-check — carries the refute-by-default adversarial stance. `CONVENTIONS §1` is canonical for the graph.

## 2. Work-Nature Map

| Work | Prior research or analysis | New intent or blueprint | New or existing asset work |
|---|---|---|---|
| Documents: papers, presentations, reports, proposals, rebuttals | academic or market research plus `analyze-project` in paper/doc mode | `autopilot-draft` | `autopilot-refine` |
| Code: libraries, research, apps, CLI, and API | academic or technical research plus `analyze-project(code)` | `autopilot-spec` in app/library/api/cli/research/composite/auto mode | `autopilot-code`, routed by spec mode |
| ML or one-shot experiment prototype | Four code-analysis inputs: experiment conventions, readiness, cleanup, and similar models | No spec for a fast cycle | Iterative `autopilot-lab`, graduating to `autopilot-code` |
| Visual assets and design | — | `autopilot-design` for a new design-first cycle | Substantial direction, token, layout, structure, or built-app design evolution goes through `autopilot-design`, updating the token contract and code from a real render. Only a trivial tweak goes directly through `autopilot-code`. Design tokens are the single contract under `DESIGN_PRINCIPLES §9`. |
| User profile | — | `analyze-user init` | `analyze-user update` |

One-line prose/config edits, pure renames, cleanup, and one-off reviews that need
no plan or log may bypass autopilot and use direct editing or the implementation
role. Small local completion units satisfying §0.2.1, including a feature's
source, tests and README, use automatic recording. Source edits or commits
outside that scope enter the explicit `autopilot-code` route required by §0.4. Use heavier
autopilot tiers only when work needs their tracking or accumulated artifacts.
`DESIGN_PRINCIPLES §4` and each capability's quick tier define minor versus
major. When one request spans several rows of this map, resolve the primary with
the §0.2 semantic precedence.

## 3. `autopilot-spec` Modes

| Mode | Use | Scaffold: PRD plus skeleton |
|---|---|---|
| `app` | User application such as Next.js or Expo | Component and Deployment diagrams plus application skeleton |
| `library` | Public npm, pip, or crate package | Packaging config and public API skeleton following reference exports |
| `api` | Backend API without UI | Component and Deployment diagrams plus FastAPI or Express router skeleton |
| `cli` | Command-line tool | argparse or typer entry plus command skeleton |
| `research` | Experiment roadmap (step ladder plus decision protocols) and reproducibility | train/eval/config and model skeleton plus Phase 1.5 checkpoint preflight |
| composite or `auto` | Multiple aspects or inferred mode | Common contract plus independent sections per selected mode, with confirmation after inference |

Reference priority is internal `similar_models` or `--ref`, then `research/<topic>/code_resources`, then generic scaffolds. Prepend conventions from `analysis_project/code/experiment_conventions.md`; fall back to `mem profile 07_coding_convention`, with project-local conventions winning conflicts.

## 4. Atomic PRD Updates

When a code or intent change affects the spec, update every affected surface in one transaction. `CONVENTIONS §6.3a` is the mapping source of truth.

| Change | Affected surfaces |
|---|---|
| Endpoint, request/response body, or error | API contract, Component, and optionally Sequence |
| DB entity or field | Data model, backend Component, and optionally ER |
| UI flow | UI flow, frontend Component, and optionally Activity |
| External service integration | API auth contract, Deployment, deploy record, and `.env.example` |
| Stack replacement | Stack decision, Component, and Deployment |
| Public API change in a library | Public API, examples, semver impact, and module-dependency Component |
| CLI command or option change | Commands, options, exit codes, README examples, and command-tree Component |

`autopilot-spec refine` identifies the impact list, confirms it, and updates it atomically. When `autopilot-code` detects spec impact, its plan or code owner updates the affected component PRD before implementation through the same route: run `spec-transaction --node execute` inside `code-execute` for standard+, use the one-shot transaction for quick, and apply it inline for direct. It does not start a separate spec route or reconfirm the unchanged scope. After the final code report, Step 7 updates `analysis_project`: edit small changes directly or run incremental `/analyze-project --mode code --skip-qa` for large ones.

## 5. Entrypoint-to-Worker Routing

The main agent proposes one primary entrypoint under §0.2, the user confirms it
under §0.4, and internal routing is automatic. Portable model roles come from
`CONVENTIONS §2`.

| Entry | Internal routing |
|---|---|
| `autopilot-research` | Research-survey and fact-check roles plus browser-fetch, PDF-extract, and web-image-search material roles |
| `analyze-project` | One capability analyzing code, paper, or document mode itself |
| `autopilot-spec` | Planning role for PRD, material role for research import, and setup logic for hosting and CI/CD |
| `autopilot-design` | Design maker and critic plus material web-image-search |
| `autopilot-code` | Direct is dispatch-depth-0 inline. From `quick`, depth-0 first runs the two-leg frame bootstrap (see above). Quick defaults to `balanced-deep` and standard+ to `deep`; explicit profile choices take precedence. Each uses one registered dispatch-depth-1 owner. At `strong+`, plan/implementation-review open asymmetric declared groups; `thorough+` adds implementation-risk and failure-mode legs. Planning, implementation, test, report, and task-aware review remain separate file-handoff stages. |
| `autopilot-code` in app mode | General code flow plus design critique at plan review and after render, DB migration safety, and automatic deploy after an authorized push |
| `autopilot-draft` | Material figure/data/reference work, writing implementation, editorial polish, and research fact-check |
| `autopilot-refine` | Reuse the draft roles plus editorial review |
| `autopilot-lab` | Setup uses research plan review, implementation scaffold, and QA smoke tests. Evaluation uses functional QA, figure generation, and research survey; at `standard+`, checkpoint evaluation, media generation, report assembly, and independent verification dispatch as stage workers under the eval execution topology in `capabilities/autopilot-lab.md`. The actual long-running training run is asynchronous and supervised through RUNLOG ⏳ rather than a stage-worker dispatch. |
| `analyze-user` | Cross-project material collection plus editorial review |

For every durable stage at `standard+`, use an independent headless session under `OPERATIONS §5.10`; the named team roles run inside that session, and the dispatch-depth-1 conductor passes only artifact paths. Direct stays dispatch depth 0, and a depth-0-run bootstrap layer — two frame legs with distinct personas and usage-aware harness selection, joined and interviewed directly by the depth-0 session, both `top` — runs ahead of quick, which stays one registered-headless dispatch-depth-1 one-shot conductor.

Each entrypoint is an explicit unit of intent. The §0.4 confirmation is the
single top-level route handshake. Capability-local review controls such as
revise into v2, back-jump, `--confirm`, or `--user-refine` remain opt-in and do
not repeat that handshake. Ask a separate question only when intent is genuinely
ambiguous after presenting the completed proposal. The runtime adapter bootstrap
owns concrete invocation syntax.

## 6. Artifact Folders

Code uses sibling `spec/` and `plans/` buckets.

| Kind | Folder |
|---|---|
| Code blueprint | `spec/`: current `prd.md`, `stack.md`, optional `design/`, `ship.md`, `pipeline_state.yaml`, and prior specs under `_internal/versions/v{N}/` |
| Code work | `plans/<date>_<slug>/`: plans, dev logs, test logs, and `_internal`, regardless of whether a spec exists |
| Experiment prototype | `experiments/<date>_<slug>/` plus `experiments/_RUNLOG.md` |
| Document | `documents/<date>_<name>/` |
| Prior research and analysis | `research/<topic>/` and `analysis_project/<mode>/` |

Numeric prefixes such as `00_`, `01_`, `02_`, and `05_` are retired. Use plain names inside `spec/`, separating user-facing files from machine-oriented `_internal/`. The spec transaction helper snapshots the exact prior `prd.md` automatically whenever an existing PRD changes, regardless of intensity; initial creation and no-op updates do not allocate a version. See `CONVENTIONS §§5 and 6.5`.

**Producer lifecycle (W7C).** Except small-work daily journals, every process folder above is a bucket inside one producer cycle once the write-cutover is active: `begin` issues the campaign/cycle/producer IDs before the first write, artifacts land under `campaigns/<campaign-locator>/<cycle-locator>/artifacts/<bucket>/…`, stage workers join the owner's open cycle through the `AGENT_ARTIFACT_*` environment, and `finalize` commits `manifest.json` after route closure. Before that, `artifact_producer.py checkpoint` publishes a live open cycle's interim manifest — the same schema with `cycle.state: open` — at `.runtime/artifact-producer/v1/open-manifests/<cyc>.json`; stage completion, supervisor polls and session turn ends trigger it automatically (at most once per 15 minutes per cycle, weights and oversized trees excluded), and `finalize` keeps its artifact IDs, then removes it. Legacy top-level writes are allowed only in the pre-activation compatibility window. See `core/CORE.md §3`.

**One request folder.** A verified frame decision and the execution routes that
seal its `route_plan` share the first frame's physical folder. Each execution
keeps its own logical cycle, input pins, route authority, completion and
immutable manifest history. Its declared stage outputs live in that folder's
`_internal/executions/<cycle-id>/artifacts/`; this is a payload scope, not a
second campaign child or permission to write the frame's evidence. The common
locator discovers those scopes from the verified decision binding. Compatible route
continuations still use D-120; a different capability or intensity keeps a
separate logical cycle. Small planned direct execution uses a journal entry
linked to the request. Existing separate folders stay where they are.

## 6.1. Cross-Project Continuity Layer

`<agent-notes-root>` is separate from each project's artifact root. The artifact root holds research, spec, plans, documents, and experiments for one project; the notes root reads across projects and presents Layer 1 and Layer 2 continuity state.

| Layer | Owner | Example | Update path |
|---|---|---|---|
| `<agent-notes-root>/cards/` | User | Layer 1 task and project cards | Worklog-board UI or direct user edit |
| `<agent-notes-root>/_triage` | Retired review history | Read-only legacy records | Preserved until daemon cleanup |
| `<agent-notes-root>/digests`, `oncall`, `study`, `manual` | Loops and operators | Digests, reports, and manuals | Loop or board UI |

`_layer2/`, the two active queues, the retired `_triage` history, and the local board DB are mutable runtime or user state and must not be committed to the harness repository. They may live in a separate notes repository, still independent of harness core and adapters. `<worklog-board-app>` displays this root and processes feedback or review. Changes to the app belong to `autopilot-code` in the app repository; harness migration must not move or delete board data.

## 7. Routing Changes After the Initial Build

In a spec-backed project, a later fix or feature—especially in a new session—must not start with an ad-hoc edit. Follow understand existing artifacts → analyze → spec → implementation.

0. **Understand existing artifacts first:** follow the read-only orientation order in §0.1 before editing or choosing a capability, then read `spec/prd.md`, `pipeline_state.yaml`, and recent `plans/*`. Reading the spec that governs the declared work scope — root `prd.md` or the relevant `spec/<slug>/prd.md` — is a hard gate in a spec-backed cwd; which candidate governs remains agent judgment, recorded via route-record `spec_read.source`. Adapter-native markers and gates deny entry to spec-changing capabilities when a current spec of this project has not been read in the current session or has changed since the read.
1. **Refresh analysis when needed:** if `analysis_project/code/` is stale or the domain is unfamiliar, run incremental `analyze-project --mode code` first.
2. **Require a spec:** when absent, route to `autopilot-spec` before development. A single throwaway is the only exception, and repetition should graduate to a spec.
3. **Check spec drift before code:** compare the request with `spec/prd.md` and its relevant component PRD. A route, schema/entity, UI-flow, external integration, migration, or existing code drift is spec-significant. The plan or code owner updates that component PRD before implementation through the same route: run `spec-transaction --node execute` inside `code-execute` for standard+, use the one-shot transaction for quick, and apply it inline for direct. `spec_touch` remains scope-derived capacity, and only actual seeded PRD authoring changes publish through normal route completion; the code `final_report` remains cycle-local primary. The transaction preserves the exact pre-image under `_internal/versions/v{N}/` and retains its revision, CAS, transaction, and snapshot checks. Proceed autonomously when drift is clear; ask only when classification is genuinely ambiguous. An explicit user deferral is the sole exception. Record “no spec impact” for within-spec implementation details. `autopilot-code` repeats this verdict in preflight Step 0 as a backstop.
4. **Run `autopilot-code`:** intensity selects the graph. Direct performs inline production plus sanity/report. Quick uses one registered-headless dispatch-depth-1 one-shot conductor for micro-plan, plan-check-lite, focused verification, and report with no dispatch depth 2. Only `standard+` creates a durable `plans/<date>_<slug>/` cycle. Derived rigor never creates a separate plan cycle by itself.

These rules close three gaps: a broken trail caused by over-creating plans for quick work, spec drift that bypasses versioned spec update, and blind editing in a new session. Both `autopilot-spec` and `autopilot-code` are iterable; post-build change is another invocation of the same capability, not a new workflow family.
# Capability route topology

Every entry capability resolves through `capabilities/topologies.json`, the machine-readable execution-topology source. Intensity, topology class, worker kind, transport, DAG nodes, write scopes, promotion signals, and completion gates remain separate axes. `utilities/capability-route.py` compiles an immutable route bound to the registry digest (plus a `capability_registry_digest` over only the parts that route derives from: shared sections, its own capability's recipes and the gate contracts they cite, so an edit to another capability does not make it stale), source commit, physical absolute working directory, artifact root, and transport evidence. Adapters may project compact summaries and pointers, but must not copy the graph into bootstrap or Skill metadata.

The route compiler is **enforced**: every node
references a unit in `roles/units/`, and routing happens at entry only — a
dispatch-depth-2 worker never routes and never selects another worker. Enumerated
recipes are curated fast paths, not the default. For a request no recipe fits, the
entry composes its own route from the same catalog (**compose-on-demand**, §0.2.1):
`capability-route.py compose` defaults to `framed`; `--shape direct` selects
inline work. Explicit `solo` and `staged` shapes remain available for decisions
already made by a frame or the person; `--shape staged --graph <stage,…>`
selects that decided subgraph. Both use the same
validator and seal; a subgraph embeds its recipe as `composed: true`. Confirmation
follows §0.4 (`[경로]` for small work, the card or SD-123 pair otherwise).
Composition changes route *shape only* — it never bypasses the §0.1
spec/artifact-order gates, never grants dispatch depth 3, and never substitutes for a
capability's own completion gates.

A received completion is a responsibility to read the exact result and perform
the already approved review/follow-through this turn, or run its existing
registered continuation. Receipt, queue consumption and ACK are not execution.
Report when work actually started, including any delay before a user reminder.

