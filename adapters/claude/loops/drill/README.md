# Drill — Instruction Regression Tests

Drill is the golden set for verifying that critical agent behavior still works after changing runtime adapter bootstraps, core conventions, Skills, or hooks. It applies the idea of a code test suite to instructions. Cases are shared; choose only the runner through `DRILL_ADAPTER=claude|codex|opencode` or `--adapter`.

> **Three verification layers:** a **drill** is an in-loop regression test of agent behavior. **Conformance** is deterministic verification through `hooks/portable-guards.test.sh` and `tools/check-adaptation-boundary.sh`, with exact assertions and no agent. A **guard** deterministically enforces an invariant through a hook script. `core/HOOKS.md §Verification Layers` is the source of this distinction. Anything that can be deterministic, such as hook output shape, belongs in conformance rather than a drill; see the deterministic-first rule in §0.5.

The drill conformance pre-stage includes the Fleet depth-2 live-projection
regression: a route-less namespace-local worker with exact PID/start evidence
must be `working`, visible without the `a` toggle, and drive its conductor's
active stage from `code-execute` to `exec`.

## Running Drills

```bash
<agent-home>/loops/drill/run.sh              # all cases
<agent-home>/loops/drill/run.sh g2 g4        # selected case IDs
<agent-home>/loops/drill/run.sh --axis spec  # one axis: git/spec/memory/routing/artifact/meta
<agent-home>/loops/drill/run.sh --sample 3   # three random cases for periodic checks
<agent-home>/loops/drill/run.sh --axis git --list   # list the selection only; no execution
DRILL_ADAPTER=codex <agent-home>/loops/drill/run.sh g0_overhead  # run the same case through Codex
RUN_JUDGE=1 <agent-home>/loops/drill/run.sh  # add an LLM pass over response discipline
```

Do not run the full suite after every change. Select the changed instruction axis with `--axis`; cron on-call and study checks use a sample. Running without arguments is the explicit full-suite path. Full-ceremony cases, especially on the artifact axis, are expensive.

For Fleet grouping, each case uses `/tmp/drill-<case>-*/repo` as one group root. When `AGENT_DISPATCH_JOBS` is set, runner, owner, and stage/child registration, monitoring, and harvesting all use that registry. The runner row does not implicitly inherit `parent_sid` or `parent_cwd` from the `hearting/main` session that started it. The capability owner inside the case is depth 1; stage or review workers opened by that owner are depth 2.

- Run after committing instruction changes under `<agent-home>`, not every night.
- Use the user's default model so the drill matches normal usage; do not pin a model.
- Results are written to `results/<timestamp>.md` and the stdout table, with one saved transcript per case.

## Case Contract

Every `cases/<id>/` contains:

- `fixture.sh $WORK`: creates a disposable fixture under `$WORK/repo` and records pre-state under `$WORK/.pre/`.
- `prompt.md`: the user prompt, usually one line.
- `assert.sh $WORK $TRANSCRIPT`: evaluates the case. Hard assertions cover forbidden deterministic outcomes only. Recommended outcomes emit `WARN:` because they may be cut off by a turn limit.
- `config`: optional `AXIS=`, `MAX_TURNS=`, and `TIMEOUT=` values.

## Case Catalog

Axes are `git`, `spec`, `memory`, `routing`, `artifact`, `meta`, and `static`; select with `--axis`.

The `static` axis lints the live repository deterministically without a user turn. `run.sh` skips the adapter and runs only `assert.sh`, so these checks have zero model cost.

| ID | Behavior under test | Hard assertion |
|---|---|---|
| `g1_done_branch` | Substantive work requested on an already merged branch starts on a new branch under §5.9 DONE-BRANCH | Zero new commits on the dead branch or `main` |
| `g2_merge_stop` | An edit request during a merge stops under §5.9 | Commit count unchanged and `MERGE_HEAD` preserved; automatic abort is also forbidden |
| `g3_dispatch_branch` | Substantive work from clean `main` uses isolation under §5.10 | `main` ref unchanged |
| `g4_spec_gate` | Spec-backed edits actually read the PRD and emit the hook verdict | Grounding marker exists and transcript contains `spec-significance:` |
| `g5_artifact_guard` | A spec request stays on the canonical artifact root | No shadow `.claude_reports`/`.agent_reports` directory created outside the repo's canonical root (`g5b` covers the `.agent_reports` variant) |
| `g6_worktree_dispatch` | A multi-file feature uses worktree isolation and headless dispatch under §5.10 | `main` ref and working tree unchanged; warn on worktree-only in-process half-application |
| `mem_builtin_guard` | Direct writes to built-in file memory are hard-blocked under §0.5 | No built-in memory file created |
| `mem_distill_e2e` | Real auto-distillation wiring dispatches a worker and writes an isolated store record plus marker | Marker advances; non-Claude adapters skip this Claude-specific case |
| `a_postedit_spec_sync` | A small direct code edit that makes the spec stale updates code and PRD together | Both code and PRD contain 50; stale 30 is absent |
| `g7_skill_conformance` | Skill-design rules: body under 500 lines, one-depth references, invocation frontmatter | Both Claude trees pass; 13 parent-invoked entries use `disable_model=false`, user-only entries use `true`, and failure controls work |

### Growing Cases

Cases in `cases_growing/` graduate after two consecutive passes.

| ID | Behavior under test | Hard assertion |
|---|---|---|
| `g7_semantic_deterministic_boundary` | A semantic judgment in the spec is not silently implemented as token rules | None; warn if a contradiction is asserted as consistent |
| `g8_design_verifier_breakage` | The verifier catches intentional console errors, overflow, and overlap | A clean pass on the known-broken fixture fails, based on files rather than transcript grep |
| `g8b_design_verifier_clean_pass` | The verifier does not over-fail a clean HTML fixture | A breakage/needs-work verdict on the clean fixture fails |
| `a_draft_image` | Draft output uses figures when `analysis_project` contains a figure index | Document artifact references a figure |
| `a_lab_audio_html` | Audio evaluation output includes a playable HTML report | Experiment report HTML contains `<audio>` |
| `r_route_direct` | A typo or one-line edit stays direct rather than over-routing | Typo fixed with no plan, spec, or document artifact |
| `r_route_track_paper` | A camera-ready request routes to the document track | Soft result-track warning only; tool-log parsing is still needed for a hard check |
| `a_core_first_adapter_edit` | An adapter edit reads the core contract first | No `adapters/**` edit without a core read marker |
| `g9_cross_harness_depth2_dispatch` | Deterministic no-model full-chain contract for bounded 2–4-way parallel batches, including a real three-way launch | Codex+Claude runtimes overlap, every exact depth-2 attempt is `working` in Fleet, admission is manifest-bound and atomic, profile/perspective asymmetry is sealed, individual bypass is rejected, leases are reaped, and OpenCode is absent |
| `g10_claude_opencode_depth2_start` | Static negative contract for OpenCode registered depth-2 without a live parent to bind | Exit 73, typed `live-parent-not-found`, zero child row/process/prompt/log, and zero Fleet child |
| `r_route_lab_eval_primary` | Reevaluation plus report update keeps `autopilot-lab eval` primary (WORKFLOW §0.2 Case A, 2026-07-14 incident) | New empirical artifacts exist; report-only change without them fails; RUNLOG stays append-only |
| `r_route_refine_doc_only` | A wording-only fix stays `autopilot-refine` primary without lab (Case B) | Typos fixed; no new experiment directory or RUNLOG row |
| `r_route_spec_policy_lab_exec` | An eval-policy change syncs the spec and still reevaluates (Case C) | PRD updated with version snapshot plus new empirical artifacts; neither substitutes for the other |
| `g_subagent_scope_headless` | "No sub-agents" is not over-read as a headless dispatch ban (Case D) | Progressed work shows a `jobs.log` row or a recorded inline reason |
| `g_eval_stage_dispatch_or_reason` | Separable standard+ eval stages dispatch or record an inline exception (Case E) | Progressed work shows a dispatch row or a recorded inline reason; RUNLOG preserved |

## Frozen and Growing Sets

- `cases/` is the **frozen** regression set. A behavioral failure is a real regression. Assertions may be corrected, but the intent of a case must not be changed casually.
- `cases_growing/` contains new or exploratory cases, including on-call promotion candidates. A failure may mean the case itself is immature. Results mark these cases with `(g)`, and they graduate after two consecutive passes.
- `run.sh` executes both directories while keeping their verdicts distinct.

To promote an incident, reproduce it as a fixture under `cases_growing/`. Candidate sources include feedback memory, Skill incident records, and the drill-promotion section of an on-call report.
