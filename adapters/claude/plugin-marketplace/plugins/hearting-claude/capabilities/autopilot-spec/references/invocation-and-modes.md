## Argument Parsing

### `--mode` (auto-detect by default)

| Value | Meaning |
|---|---|
| `auto` (default) | Infer one or more modes from the request and code signals |
| `app` | App spec — PRD + stack + scaffolding + skeleton |
| `library` | Library spec — public API + usage examples + compatibility/versioning + module structure |
| `api` | API service spec — endpoints + auth + data model |
| `cli` | CLI spec — commands + options + input/output |
| `research` | Experiment-roadmap spec — step ladder + decision protocols + evidence lineage, over a reproduction contract (entry points + configs + reproduction commands + expected metrics) |
| Comma-separated values such as `app,library` | Multiple modes, each with an independent section in one PRD |

### Verification rigor (derived from intensity)

- Verification rigor is not an independent `--qa` axis. It is derived deterministically from `--intensity` (standard tier by default); [CONVENTIONS.md §1.1](../../../core/CONVENTIONS.md#11-verification-rigor-tiers) is the single authority for tiers and mappings.
- Recommend `--intensity quick` for small spec tweaks in update mode. It skips refinement and permits only one round. Run the update path instead of editing ad hoc so required snapshot and log behavior remains intact.

### `--user-refine`

- After drafting the PRD, collect user notes and run the refinement loop, backing up to `_internal/refine_v{N}.md`.

## Automatic Mode Signals (`auto`)

| Signal | Inferred mode |
|---|---|
| `bin` in `package.json`, `entry_points` in `setup.py`, or `[project.scripts]` in `pyproject.toml` | **cli** |
| `main`/`exports` in `package.json`, or `[project]` in `pyproject.toml` plus explicit exports in `__init__.py` | **library** |
| Import of `argparse`, `click`, `commander`, or `typer` | **cli** |
| `configs/*.yaml` plus training/evaluation metric output, or `*.ipynb` | **research** |
| Next.js, Expo, SvelteKit, Astro, or Vite with a React framework | **app** |
| FastAPI, Express, or Hono with no UI | **api** |
| Request keywords | Mode indicated by the request |

Show a one-screen confirmation in the user's communication language before proceeding:

```
=== Mode inference ===
- Request "정돈·공개" + existing-code analysis:
  · train.py / eval.py + argparse   → cli ✓
  · configs/ + training metrics    → research ✓
  · exports in models/__init__.py  → library ✓ (optional)

Combined mode: research + cli (+ optional library). Proceed?
(proceed / modify — add or remove a mode / choose one mode / stop)
```

## Context Auto-Detection

At invocation, inspect the request and cwd to distinguish a new run from re-entry, then load these materials automatically:

| Material | Purpose | Priority |
|---|---|---|
| `mem profile 07_coding_convention` (`python3 <agent-home>/tools/memory/mem.py profile 07_coding_convention`) | User's cross-project conventions: model directories, configs, prefixes, preferred layers, frameworks | Second: cross-project default/fallback |
| `<artifact-root>/analysis_project/code/experiment_conventions.md` | Actual conventions for this project | **First**: wins on conflict; memory profile 07 only fills gaps |
| `<artifact-root>/analysis_project/code/similar_models.md` | Similarity among models in this project | First reference-source candidate for scaffold Phase 0 |
| `<artifact-root>/research/<topic>/` | External repository cards and Quick verify commands from `07_resources` | Second reference-source candidate for Phase 0 and verification source for Phase 1.5 |

### Step 1 — Inspect `pipeline_state.yaml`

| Detection | Handling |
|---|---|
| No `<artifact-root>/spec/pipeline_state.yaml` | **New** — start at Step 1 and create `spec/` |
| `<artifact-root>/spec/pipeline_state.yaml` exists | **Re-entry** — read `phases:`, classify the request, and resume the relevant step as refinement v{N+1} |

The target is `<artifact-root>/spec/` under the cwd: one repository, one spec. In the monorepo exception with multiple `spec/<component>/` directories, identify the component from the request and cwd.

### Step 2 — Map a Re-Entry Request to a Step

The signal strings below are quoted multilingual examples, not fixed trigger phrases. Infer intent semantically.

| Example request | Inferred step | Flow |
|---|---|---|
| "스택 바꾸자" / "Vercel 대신 Cloudflare" / "framework 교체" | Step 2: reselect stack candidates | refinement v{N+1}; invalidate downstream steps |
| "Y endpoint 추가" / "data model 의 X entity 필드 변경" / "ui flow 의 X 화면" | Step 3a: core PRD mode | refine and update the coupled `api_contract`, `data_model`, and Component diagram |
| "Component diagram 손보자" / "Deployment 자리 추가" | Step 3b: Architecture Diagrams | refinement v{N+1} |
| "복합 mode 추가 — Y mode 도" / "이 spec 에 cli 도 같이" | Step 3c: another combined-mode section | refine and add the mode |
| "skeleton 다시 — ref 바꾸자" / "ref repo 다른 자리" | Step 4 Phase 0: reference source | rerun Phases 1, 1.5, 2, and 3 |
| "skeleton 의 train.py 수정" / "scaffold 결과 손보자" | Step 4 Phase 2: `dev/new-lib` unit | rerun Phases 2 and 3 |

> Requests such as "Vercel 셋업", "배포 셋업", "env 변경", or "도메인 연결" route to the separate [`autopilot-ship`](../../autopilot-ship.md) skill.

### Step 3 — Show One Confirmation Screen

Render this template naturally in the user's communication language:

```
=== autopilot-spec invocation ===
Project: <name>
Output: spec/ (existing spec found) or (missing — new spec)
Phases on re-entry: spec=done, scaffolding=done, dev=in_progress
Request: "<user request>"
→ Inference: <step / new run> — refinement v{N+1} on re-entry

Proceed? (proceed / choose another step / create a new spec / stop)
```

New versus re-entry detection works without an explicit `--from` option by inspecting the request and cwd. Honor an explicit `--from <step>` when supplied.

### Update Mode — Canonical Path for Existing Specs

This skill treats initial creation and existing-spec updates as equally first-class capabilities. **Every spec change that updates `prd.md` must pass through update mode**, whether requested directly, detected as drift by autopilot-code, or initiated by a WORKFLOW §7/adapter post-run correction. Do not edit `prd.md` ad hoc; update mode preserves versioning and prevents drift or loss.

Update mode performs three operations as one helper-owned transaction:

1. Enter `utilities/spec-transaction.py run`; it re-reads the latest version under `.pipeline-lock` and prepares the exact current `prd.md` bytes before the child command.
2. Update `spec/prd.md`, the always-current T1 file. If it changed, the helper retains and byte-verifies `spec/_internal/versions/v{N}/prd.md`; initial creation and no-op updates create no snapshot. Do not create or check the snapshot manually. Rewrite the affected sections in place; delete superseded content instead of annotating it.
3. Record the change narrative (reasons, quoted user wording, triggers, supersede tables) in `pipeline_summary.md`, never in the PRD body. Synchronize affected adjacent files (`data_model.md`, `api_contract.md`, `ui_flow.md`, `stack.md`) and Architecture Diagrams inside the same transaction using the Step 3.5 coupled-update logic.

> Update mode is not a separate mode label. It activates automatically on re-entry when `pipeline_state.yaml` exists. The five modes (`app`, `library`, `api`, `cli`, `research`) describe the spec type; update describes an operation. They are orthogonal, so update the existing spec's original mode sections. In `research` mode the typical update is a roadmap advance: close a step with its verdict and evidence links, then re-plan the remaining ladder in the same transaction.

**Route post-run drift from WORKFLOW §7 or an adapter bootstrap here**:

- When autopilot-code detects a spec-affecting change—new endpoint, schema/data-model change, changed entity meaning, UI-flow change, migration, or external integration—it back-jumps into update mode.
- **CLEAR drift** with unambiguous intent and scope: proceed autonomously and report in one line.
- **AMBIGUOUS drift** with multiple interpretations or unclear impact: ask before proceeding; do not guess.

For a small spec tweak, `--intensity quick` uses one registered-headless dispatch-depth-1 one-shot conductor with only an inline micro-plan, plan-check-lite, and verify-lite. Durable snapshots and logs are created only when the update actually changes `prd.md`; quick intensity alone does not force plan, log, or snapshot artifacts.

### Refinement v{N+1} Versioning

This is the update-mode mechanism above. Spec versioning reuses the document-track mechanism in [CONVENTIONS §5.4.3](../../../core/CONVENTIONS.md#5-skill-output-convention--t1t2t3): `prd.md` is always the current T1 file.

- **Any change to an existing `prd.md`** — the transaction helper preserves its exact pre-image at `spec/_internal/versions/v{N}/prd.md`, then verifies the snapshot after the child command.
- **Minor edit** — use the same transaction/snapshot path, add a minor-log entry to `pipeline_summary.md`, and after five accumulated minor edits emit an `/audit` chat alert. Minor means lighter review/history, not snapshot exemption.
- **Initial creation or no-op update** — create no snapshot and consume no version.
- Do not ask the user to manage versions manually.
