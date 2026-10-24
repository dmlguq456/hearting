> **Artifact layout:** follow [CONVENTIONS §5](../../../core/CONVENTIONS.md#5-skill-output-convention--t1t2t3). `plans/<date>_<slug>/` is the standalone default. Routed stages use their sealed node outputs and write scope; the cycle output directory is only the base. `direct` has no plan artifact. `quick` uses an inline micro-plan and writes a durable plan only when policy requires one.
>
> **Artifact root:** resolve with `utilities/artifact-root.sh` (CONVENTIONS §5.1).

## Context Auto-Detection

At invocation, inspect the current directory, artifact root, and spec. Read:

| Source | Purpose | Priority |
|---|---|---|
| `mem profile 07_coding_convention` | Cross-project coding defaults | Second; fallback only |
| `<artifact-root>/analysis_project/code/experiment_conventions.md` | Project-specific conventions | First; wins conflicts |
| `<artifact-root>/spec/prd.md` | Product or system blueprint | Activates spec-mode logic |
| `spec/design/05_handoff/handoff.md`, `design/02_tokens/tokens.md`, `design/design_state.yaml` | App tokens, components, and token version | First for UI implementation and design critique |
| `experiment_readiness.md`, `cleanup_candidates.md`, `similar_models.md` | Experiment-readiness cleanup | Read when the task asks for experiment preparation |

### Detect Spec

| Condition | Action |
|---|---|
| `<artifact-root>/spec/pipeline_state.yaml` exists | Read spec and activate every mode named in its `mode` array. |
| Absent | Use normal dev/debug behavior with lightweight inference from project files. |

Standalone output remains `plans/<date>_<slug>/`; routed output follows the sealed node. Report spec detection and mode in one localized line.

### Mode-Specific Logic

| Mode | Additional behavior |
|---|---|
| **app** | Dispatch the `design/critic` unit for UI changes; never auto-run destructive DB migration; account for post-push CI/CD; consume the design-owned token source rather than redefining hex or spacing inline. Route substantial direction, token, layout, or structural decisions through autopilot-design; allow only trivial visual tweaks directly. |
| **library** | Analyze semver impact, export consistency, and usage-example updates for public API changes. |
| **api** | Check endpoint, body, error, and spec-contract consistency; review auth changes; explain rate-limit migration. |
| **cli** | Check command, option, I/O format, and exit-code consistency. |
| **research** | Update reproduction commands for train/eval entry-point changes, synchronize config changes with spec, and verify expected metrics where possible. |

Activate all matching logic for multimode specs.

## Mandatory Preflight

### Intake Gate

For new, underspecified dev work with irreversible decisions, run the one-round intake gate from [CONVENTIONS §6.6](../../../core/CONVENTIONS.md#66-autopilot-intake-gate). Skip it for debug, resumes, and work already governed by a complete spec.

### 0a. Git Working State

Run [OPERATIONS §5.9](../../../core/OPERATIONS.md#59-git-working-state-preflight) before spec triage, before edits, and before every commit or write-back.

- Merge, rebase, cherry-pick, or detached HEAD → **STOP**, report, and never abort automatically.
- Same branch in another worktree, upstream ahead, or unrelated dirty state → **WARN**.
- Remember entry HEAD. If HEAD changes or a new `MERGE_HEAD` appears before commit, stop.
- Non-git and single-checkout contexts pass harmlessly.

If the gate reports `DONE-BRANCH` for a new task, branch from the latest base rather than stacking work on a merged branch:

```bash
git fetch origin && git switch -c <slug> origin/<base>
```

### Worktree and Dispatch Scale

Follow [OPERATIONS §5.10](../../../core/OPERATIONS.md#510-work-isolation-and-parallel-dispatch):

- Every feature, new module, or multi-file change uses a worktree and task branch regardless of intensity. Only a typo or one-line isolated edit belongs directly in main.
- After creating the worktree, dispatch the whole autopilot-code cycle into a dispatch-depth-1 headless session. Main scouts, dispatches, and harvests; it does not split one pipeline between dispatch depth 0 and headless execution.
- Inside the dispatch-depth-1 conductor, standard+ dispatches code-plan, code-execute, code-test, and code-report as file-handoff dispatch-depth-2 sessions. File contracts preserve continuity. The conductor reads only verdicts and gate state.
- Keep micro-stages with no durable artifact, such as a one-line plan-check, inline. direct and quick remain inline at their intended layer.
- A new independent request uses another worktree when files do not overlap; overlapping work queues behind the active branch. Merge selection remains with the user or main harvest flow.

### 0b. Spec-Significance Triage

When `spec/` exists, compare the request with `spec/prd.md` and relevant contract files before planning:

- **spec-significant:** route, schema, entity field, UI flow, external service, stack, migration, or existing spec drift. Run autopilot-spec update through `spec-transaction.py` first; the helper snapshots the exact old PRD automatically when it changes. Then implement against the new spec. Ask only when classification is ambiguous.
- **within-spec:** bug fix, refactor, or implementation detail. Continue.

Write one verdict to chat and plan or logs before code-plan:

```text
spec-significance: within-spec (implementation detail; no spec impact)
spec-significance: SPEC-SIGNIFICANT (data_model: Task.category) → update autopilot-spec first
```

Together, entry triage, reverse-drift detection, and post-change impact detection keep code and spec synchronized in both directions.

## Reverse Drift Before Work

Before editing, determine whether spec or design changed after the previous code cycle:

| Compare | Action |
|---|---|
| `spec/pipeline_state.yaml last_updated` versus latest `plans/<date>_*/` cycle | If spec is newer, reread `prd.md`. |
| `design_state.yaml tokens_version` and `tokens_updated` versus the version in the previous plan log | If tokens are newer, reread `tokens.md`, `tokens.css`, and the relevant `design_summary.md` entry. |

Report detected changes and continue. In app mode, propose a grouped token update when the latest design tokens are not reflected in code. Record the consumed token version in the current plan or dev log.

## Lightweight Inference Without Spec

- UI framework in `package.json` → app-like concerns.
- `package.json` `bin` or argparse → CLI concerns.
- `configs/*.yaml` plus notebooks → research concerns.

This is a partial fallback. Recommend autopilot-spec when full mode behavior is required. PRD, stack, skeleton, environment, migration guidance, and deployment setup belong to autopilot-spec; autopilot-code owns source changes.

## Detect Post-Change Spec Impact

| Code change | Affected spec |
|---|---|
| Endpoint, request body, or error | `api_contract.md`, component diagram, optional sequence diagram |
| Entity or schema field | `data_model.md`, optional ER diagram, component diagram |
| Page route or UI flow | `ui_flow.md`, optional activity and component diagrams |
| External SDK | auth contract, deployment diagram, `deploy_record.md`, `.env.example` |
| Major dependency or framework change | `stack_decision.md`, component and deployment diagrams |
| Lifecycle state model | `data_model.md`, optional state diagram |

Show one grouped update plan and let the user choose: update through autopilot-spec now, finish code and record deferred drift, or explicitly accept drift. autopilot-code never edits spec directly.

## Experiment-Readiness Branch

For natural-language requests to prepare research code before a lab cycle, read `cleanup_candidates.md`, `experiment_readiness.md`, and `experiment_conventions.md`, then plan cleanup, refactoring, and readiness as one dev task. Preserved multilingual input examples include `"실험 ready 상태로 정리"`, `"lab 시작 전 정돈"`, `"unused 코드 제거"`, and `"main.py 를 train.py / eval.py 분리"`.

```text
analyze-project --mode code
  → cleanup_candidates.md / experiment_readiness.md
  → autopilot-code experiment-readiness cleanup
  → optional analyze-project refresh
  → autopilot-lab
```

Prepend project-local config, layer, and prefix conventions to planning and execution.

## Default Routing

The main agent infers options, summarizes them once where confirmation is required by the bootstrap, then invokes the Skill.

Preserve these multilingual user-input examples as routing signals:

- dev: `"X 기능 만들어줘"`, `"Y 추가해줘"`, `"Z 구현해줘"`, `"이 모듈 리팩토링"`, `"이어서 진행"`, `"다음 stage 부터"`;
- debug: `"이 에러 디버그해봐"`, `"X 가 안 돌아"`, `"왜 안 되지"`, plus error logs and tracebacks.

Infer `--mode`, `--intensity`, and `--from` from scope and state. Keep `--user-refine` off unless the user explicitly requests a review pause. Direct slash invocation skips option confirmation.

Bypass the full pipeline for one-line edits or renames and for a single static review; route those directly to the appropriate implementation or QA role.
