# Autopilot-* Routing Map — Agent-Facing Core

> A compact map for the main agent to route a task request to capabilities and roles. Each adapter maps them to its runtime-native skills, commands, agents, or profiles. Do not force symmetry; separate work according to its nature.
>
> The root `README.md` owns the user-facing meaning map and entry list. `CONVENTIONS.md` owns QA, model, and folder definitions. This document contains routing tables only, avoiding duplicated narrative and invocation examples.

---

## 0. Invariants — One Router and the Artifact Order Convention

This is the single routing contract for spec-backed projects that contain `.agent_reports/spec`, with legacy `.claude_reports` compatibility. Read it on demand when the adapter's status or reminder surface indicates routing is due; hooks expose runtime state but do not replace or eagerly inject this contract.

Every task first passes through the work-nature map in §2. Direct work, runtime plugins, and built-in Skills are used only where this router places them. Adapter and runtime projection work also remains core-first: establish the portable invariant in `core/`, read its governing document, then change adapter or generated output. A read marker enforces order but is not a substitute for review.

After the §0.1 read-only exemption and §0.2 semantic precedence, a request that
clearly matches one manifest `entry-router` positive trigger and none of that
entry's exclusion boundary uses that entry as the primary route. If multiple
entries match, resolve them through §0.2 and the work-nature map instead of
silently dropping to capability-free work.

Capability-free inline work is limited to read-only orientation, status, a
simple factual or explanatory answer that requests no new durable artifact, or
an explicit conversational/no-files constraint.
`direct` is an intensity inside the selected entry route, not a bypass around entry routing.

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

This boundary was strengthened after a 2026-07-14 incident where a context
recovery request in a spec-backed project was routed to `analyze-project` before
its existing legacy artifact root and memory-linked artifacts were read.

**Hard artifact order:**

```text
[code] research / analyze-project(code) → autopilot-spec (spec/) → autopilot-code (plans/)
[docs] research / analyze-project(paper or doc) → autopilot-draft → autopilot-refine
```

- **No code without a spec:** if a code request has no `spec/`, run `autopilot-spec` first. A one-off throwaway is the only exception; repeated work graduates to a spec.
- **No spec without prior evidence:** if neither `research/` nor `analysis_project/` grounds the spec, run `autopilot-research` or `analyze-project` first. Enforce this more strongly in unfamiliar domains and for new intent.
- **Mechanical enforcement:** `artifact-guard.sh` fail-closes writes outside the canonical artifact root and, for a route-backed write under `spec/`, requires the active route to have declared `spec_touch` with a `spec/` write scope. For route-backed refine work, `target-artifact` resolves only to `documents/<artifact>/**` and `research/<artifact>/**`; before a major existing-file rewrite the guard invokes the deterministic snapshot helper. The artifact-creation order above is convention plus routing reminders, not a mechanical block; it does not block edits to existing artifacts or source either way.

**The owning capability also owns revisions.** The routing reminder and convention govern edits; `artifact-guard.sh` does not track per-artifact edit history.

| Artifact | Sole update path | Version location |
|---|---|---|
| `spec/` blueprint | `autopilot-spec` update | `_internal/versions/v{N}/` |
| code work under `plans/` | `autopilot-code` | `plans/<date>_<slug>/` |
| documents | `autopilot-draft` or `autopilot-refine` | `_internal/versions/v{N}/` for major refinement; minor history in `pipeline_summary.md` |
| experiments | `autopilot-lab` | `_RUNLOG.md` |
| DB records with `type=profile` | `analyze-user` or `post-it --scope user` | changelog inside the record body |

This document plus the runtime adapter bootstrap is the routing source of truth. Violation signals include ad-hoc artifact edits, code before its gates, or updating an artifact through a capability that does not own it.

### 0.2. Semantic Primary Routing

Choose the primary capability from the core new work the request performs, not
from the artifact the user names or the surface verb such as "update", "fix",
or "정리". A request that ends in "update the report" still has its primary
decided by what must newly happen before any report can change.

Precedence, highest first:

1. New empirical work — training, checkpoint reevaluation or analysis,
   metric or ablation computation, plot/figure generation, or audio/media
   artifact generation — makes `autopilot-lab` the primary capability
   (`eval` for checkpoint-centered work, `setup` for new training).
2. With no new empirical work, correcting only the wording, structure, or
   errors of an existing document makes `autopilot-refine` the primary.
3. A change to requirements, evaluation policy, or any blueprint surface adds
   `autopilot-spec` update as a secondary spec-sync step; it never replaces
   the execution primary.
4. Formal report prose assembly routes through `autopilot-draft` or the owning
   capability's draft handoff as a secondary step.
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
| "Reevaluate the model on a new test set and update the report" | `autopilot-lab --mode eval` | refine/draft document pass; `autopilot-spec` on policy change; optional artifact sink |
| "Fix only the typos and sentences in REPORT.md" | `autopilot-refine` | — |
| "Change the evaluation mixing policy to unscaled and reevaluate" | `autopilot-lab --mode eval` | `autopilot-spec` update; neither replaces the other |

Added after a 2026-07-14 incident where a checkpoint reevaluation with report
regeneration was routed to `autopilot-refine` as primary from its surface
artifact and the entire evaluation ran inline in the main session.

### 0.3. Pre-Execution Gate for Long-Running Work

Before starting a long-running command, GPU or checkpoint evaluation, bulk
figure/media generation, or a full report regeneration, the main session
answers this gate; it does not enter long-running execution inline without it:

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

For material work, the main agent proposes the route and the user confirms the
intent before capability execution begins. The agent fills every field from
the request and recovered context; the user reviews a completed proposal rather
than recalling capability names, invocation syntax, or pipeline options.

Render labels in the user's communication language while preserving these five
fields and this order. In Korean, the canonical card is:

```text
[실행 확인]

작업: <무엇을 어떤 결과로 만들지>
이유: <현재 배경과 작업이 필요한 이유>
경로: <primary entry capability> · <mode/intensity> — <선택 이유>
범위: <포함 범위와 중요한 제외 대상>
완료: <산출물과 검증 기준>

→ 진행 / 수정: <틀린 부분> / 중단
```

When the runtime exposes a native structured-question surface (Claude
`AskUserQuestion`, Codex `request_user_input`), deliver this confirmation
through it: the five fields form the question body and the options are exactly
진행 (recommended) / 수정 / 중단. The plain-text card above is the fallback when
no such surface exists or it fails. The structured form changes only the
delivery surface — never the five fields, their order, the one-time approval
semantics, or the exemptions below.

Keep each value to one line. Do not include alternative menus, internal
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
artifacts. `direct` is an explicit route shown in the card with its
reason, not a silent no-route decision. A current or immediately preceding user
instruction that already approves the same route and scope satisfies the gate;
do not repeat the card. After approval, capability-owned stages, validation,
records, commits, dispatch, and handoffs proceed without further confirmation.
Reconfirm only a material change to the primary capability, scope, completion
criterion, destructive risk, or touched external system.

Material source work has an additional deterministic participation invariant:
before a source Edit/Write-family mutation or a commit containing source
changes, the acting session must hold a current, cwd-bound route record emitted
by `utilities/capability-route.py compile`. For code, that route is
`autopilot-code` at no less than `direct`. A Skill invocation, the prose card,
an earlier session's record, or a stale record from another cwd is not route
participation. Hotfixes do not bypass this floor. This invariant was hardened
after the 2026-07-24 Cairn incident in which a route card was shown but no
route was entered and the feature was edited, committed, and deployed through
silent no-route work.

Before approval, choose from compact manifest routing metadata and §0.2; do not
load the full entry Skill body or its references merely to propose a route. At
`standard+`, the dispatch-depth-1 owner reads the selected capability contract and each
dispatch-depth-2 worker reads only its stage contract. `direct` or `quick` acting
sessions read the detail they need after approval. If a runtime automatically
injects a selected Skill body into main, do not duplicate that read; record the
runtime limitation rather than claiming total-token savings.

Entry routers therefore have two deterministic load phases: manifest-owned
metadata before approval, then the selected portable owner contract after
approval. A router may expose one direct owner-reference index, but no
pre-approval reference may contain execution procedure. The confirmation is
one-time for an unchanged approved route and scope.

### 0.5. Post-Execution Completion Report

After a material-work attempt, the user-facing main agent closes the flow with
a fully populated five-field report card. Render labels in the user's
communication language while preserving these five fields and this order. In
Korean, the canonical card is:

```text
[완료 보고]

작업: <수행한 작업>
결과: <완료 | 부분 완료 | 실패 | 차단 — 실제 결과>
검증: <실행한 검증과 결과>
산출물: <사용자가 확인할 경로 또는 없음>
남은 사항: <미완료 항목, 위험, 차단 요인 또는 없음>
```

Keep each value to one line. `결과` names the honest outcome status —
completed, partial, failed, or blocked, localized for the audience — and must
not present partial, failed, or blocked work as completed. Use `없음` (or its
audience-language equivalent) when there is no artifact or remaining item.

For dispatched or long-running work, main emits this card only after it has
synchronously waited or polled for terminal state, harvested the result and
worker artifact, integrated it when authorized, and verified the final state.
A worker handoff, background-process exit, or stage verdict alone is not task
completion. Read-only orientation, simple factual answers, and status-only
replies are exempt and use concise prose instead.

Reporting completion is not the same as recording it. A compiled route states
that work began; nothing else states that it ended, and `complete` closes only a
registered attempt in the jobs registry, so inline and `direct` work leaves no
closure at all. Close the route in the same turn as the card:

```text
python3 utilities/capability-route.py close --route <route.json> [--commit <sha>] [--summary <line>]
python3 utilities/capability-route.py status --artifact-root <dir> --open-only
```

`close` writes an outcome sidecar beside the immutable route record — the record
itself cannot carry the closure, because `route_hash` covers every other field.
`status --open-only` then answers "what was started and never finished" directly.
An unclosed route is indistinguishable from abandoned work, and the same applies
to the other things a finished attempt leaves behind: a merged worktree and
branch, a spec `pipeline_state.yaml` still naming a live phase, and a memory
handoff still `pending` after its obligation is met. Close what the attempt
opened, or the next session pays for it in reconstruction.

### 0.6. Tracked-Workflow Lifecycle and Continuation

A process exiting is not a workflow completing. **Workflow completion is the
state in which every terminal node of the approved stage DAG holds its
completion gate and no human gate remains open.** An intermediate stage that
succeeded — a training run that reached its last epoch, a green CI check, a
worker that returned `PASS` — is stage evidence, never workflow completion. No
acting agent, dispatch-depth-1 owner, supervisor, or runtime lifecycle hook may
declare completion from an intermediate success.

A steward's idle-notify subscription is observation, not a continuation; it never
satisfies the registered-continuation obligation below (`core/OPERATIONS.md §5.14`).

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

**Every non-terminal stage declares exactly one continuation.** A stage graph
that leaves a stage with no way to reach the next one is the defect this
contract exists to prevent, so the declaration is mechanical, not editorial:

| Continuation | Meaning |
|---|---|
| `inline-next` | the same checked payload runs the next stage before it returns |
| `supervised` | a registered continuation supervisor observes child termination and starts the next stage exactly once |
| `human-gate` | an explicit human gate named in the recipe's `human_gates` blocks the successor |
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

Grounded by the 2026-08-04 BC_ResNet_tf incident: training and its hard-negative
loop finished, the wrapper contained no evaluation stage, the documentation
named "separate eval" with no owner or trigger, the resource runner had no
completion callback, the resource row was invisible to Fleet, and the acting
agent ended its turn with no follow-up mechanism registered. Each of those five
is now a mechanically checked condition rather than a convention.

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

`↻` marks an iteration point. Common post-work capabilities are read-only `audit` and Markdown correction through `autopilot-refine`. Cross-project capabilities are `analyze-user` and `post-it --scope user`.

## 1.1. Pipeline Intensity Routing

Autopilot entrypoints choose `intensity`; verification rigor is derived from it under `CONVENTIONS §1.1`, not from a separate `--qa` axis. Intensity selects the stage graph and dispatch depth, while derived rigor scales plan checks, selected independent review, and final verification.

| Request shape | Default | Routing |
|---|---|---|
| One-off answer, typo, rename, or explicit no-artifact work | `direct` | No plan stage, plan check, or durable plan |
| Small localized change that misses at least one atomic-direct predicate and has no promotion signal | `quick` | Registered-headless dispatch-depth-1 one-shot conductor with orient-lite, micro-plan, plan-check-lite, focused verification, and concise report; no dispatch depth 2 |
| Work with a promotion signal or separable durable stages | `standard` | Durable plan/checklist; a `deep` dispatch-depth-1 conductor dispatches capability-defined stages with file-only handoff and realizes only registry-declared parallel groups, normally a two-leg asymmetric framing group |
| Important multi-file or risk-bearing work | `strong` | A `deep` owner plus the declared plan/review groups; selected high-value anchors may widen to a third profile/perspective leg while other groups remain width two |
| Complex cross-domain or cross-harness work | `thorough` | Bounded dispatch-depth-2 perspective and verifier workers |
| High-stakes, irreversible, security, or external-facing work | `adversarial` | Thorough plus an explicit adversary, failure-mode, or security pass |

Only `direct` has no plan. Every other autopilot graph includes a plan check, but independent QA is not repeated after every sub-stage by default. Independent passes use route-declared bounded groups with cross-harness first and model-profile/perspective asymmetry where useful; every review — down to a `direct`/`quick` self-check — carries the refute-by-default adversarial stance. `CONVENTIONS §1` is canonical for the graph.

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
role. A source-code Edit/Write or a commit containing source changes may not:
even an atomic hotfix enters an explicit `autopilot-code` route at `direct` and
produces the current-session route record required by §0.4. Use heavier
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

`autopilot-spec refine` identifies the impact list, confirms it, and updates it atomically. If `autopilot-code` detects spec impact, it plans the bundle, confirms, and jumps back to `autopilot-spec`. After the final code report, Step 7 updates `analysis_project`: edit small changes directly or run incremental `/analyze-project --mode code --skip-qa` for large ones.

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
| `autopilot-code` | Direct is dispatch-depth-0 inline. Quick is one `balanced-deep` registered-headless dispatch-depth-1 one-shot conductor. Every `standard+` owner is `deep`; at `standard`, it dispatches framing as `balanced-deep + light` cross-harness legs before planning. At `strong+`, framing adds a deep contrarian leg and plan/implementation-review open asymmetric declared groups; `thorough+` adds implementation-risk and failure-mode legs. Planning, implementation, test, report, and task-aware review remain separate file-handoff stages. |
| `autopilot-code` in app mode | General code flow plus design critique at plan review and after render, DB migration safety, and automatic deploy after an authorized push |
| `autopilot-draft` | Material figure/data/reference work, writing implementation, editorial polish, and research fact-check |
| `autopilot-refine` | Reuse the draft roles plus editorial review |
| `autopilot-lab` | Setup uses research plan review, implementation scaffold, and QA smoke tests. Evaluation uses functional QA, figure generation, and research survey; at `standard+`, checkpoint evaluation, media generation, report assembly, and independent verification dispatch as stage workers under the eval execution topology in `capabilities/autopilot-lab.md`. The actual long-running training run is asynchronous and human-gated through RUNLOG ⏳ rather than a stage-worker dispatch. |
| `analyze-user` | Cross-project material collection plus editorial review |

For every durable stage at `standard+`, use an independent headless session under `OPERATIONS §5.10`; the named team roles run inside that session, and the dispatch-depth-1 conductor passes only artifact paths. Direct stays dispatch depth 0 and quick stays one registered-headless dispatch-depth-1 one-shot conductor.

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

**Producer lifecycle (W7C).** Every folder above is a bucket inside one producer cycle once the write-cutover is active: `begin` issues the campaign/cycle/producer IDs before the first write, artifacts land under `campaigns/<camp>/cycles/<cyc>/artifacts/<bucket>/…`, stage workers join the owner's open cycle through the `AGENT_ARTIFACT_*` environment, and `finalize` commits `manifest.json` after route closure. Legacy top-level writes are allowed only in the pre-activation compatibility window. See `core/CORE.md §3`.

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
3. **Check spec drift before code:** compare the request with `spec/prd.md`. A route, schema/entity, UI-flow, external integration, migration, or existing code drift is spec-significant and routes through `autopilot-spec` update; when the transaction changes an existing PRD, the helper preserves its exact pre-image under `_internal/versions/v{N}/`. Proceed autonomously and report when drift is clear; ask when it is genuinely ambiguous. Record “no spec impact” for within-spec implementation details. `autopilot-code` repeats this verdict in preflight Step 0 as a backstop.
4. **Run `autopilot-code`:** intensity selects the graph. Direct performs inline production plus sanity/report. Quick uses one registered-headless dispatch-depth-1 one-shot conductor for micro-plan, plan-check-lite, focused verification, and report with no dispatch depth 2. Only `standard+` creates a durable `plans/<date>_<slug>/` cycle. Derived rigor never creates a separate plan cycle by itself.

These rules close three gaps: a broken trail caused by over-creating plans for quick work, spec drift that bypasses versioned spec update, and blind editing in a new session. Both `autopilot-spec` and `autopilot-code` are iterable; post-build change is another invocation of the same capability, not a new workflow family.
# Capability route topology

Every entry capability resolves through `capabilities/topologies.json`, the machine-readable execution-topology source. Intensity, topology class, worker kind, transport, DAG nodes, write scopes, promotion signals, and completion gates remain separate axes. `utilities/capability-route.py` compiles an immutable route bound to the registry digest, source commit, physical absolute working directory, artifact root, and transport evidence. Adapters may project compact summaries and pointers, but must not copy the graph into bootstrap or Skill metadata.

The route compiler is **enforced** (promoted from report-only, 2026-07-22): every node
references a unit in `roles/units/`, and routing happens at entry only — a
dispatch-depth-2 worker never routes and never selects another worker. Enumerated
recipes are curated fast paths. For a request no recipe enumerates, the entry composes a
node graph from the same unit catalog (**compose-on-demand**): the composed graph passes
the same validator, is hash-sealed exactly like a recipe route, is marked
`composed: true`, and still requires the §0.4 route card. Composition changes route
*shape only* — it never bypasses the §0.1 spec/artifact-order gates, never grants
dispatch depth 3, and never substitutes for a capability's own completion gates.
