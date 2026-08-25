# analyze-user

Cross-project user-profile entrypoint. Extract reusable patterns from the user's prior figures, writing, presentations, analyses, domain work, and code, then persist them as DB `type=profile` records. The pipeline is `discover → analyze → verify → qa → output → summary`, with adversarial-tier verification (`intensity=adversarial`). This file defines routing and invocation; load the relevant reference only when its detailed procedure or template is needed.

> **Output location**: DB `type=profile` records, read with `python3 <agent-home>/tools/memory/mem.py profile <stem>` or `mem profile <stem>`. The seven stems range from `01_paper_figure_style` through `07_coding_convention`. Use `_internal/` only as temporary scratch space for source indexes, QA reviews, and pipeline state; the DB remains the source of truth. The artifact-root 3-tier convention does not apply directly, but keep durable profile content separate from internal logs.

> **Workspace assumption**: This is a _cross-project_ task and is not limited to the current cwd. Analyze only sources explicitly supplied with `--source <path>` or otherwise placed in scope by the user. If no source is available, do not guess or scan hardcoded personal paths; report that no input was found and show how to provide one. Persist output only as DB `type=profile` records, not as profile files.

## Default Invocation Rule

The main agent may route here when a request clearly asks to derive or update a cross-project preference profile. Configure options from the request and current artifacts, then follow the portable pause/autonomy policy; do not require a runtime-specific invocation ceremony.

### Example Requests

- "사용자 프로필 갱신해줘" / "내 figure 스타일 분석해줘"
- "내가 만든 발표 자료들 분석해" / "내 paper 작성 톤 추출해줘"
- "user_profile 업데이트" / "내 작업 성향 정리"
- "내 코딩 컨벤션 정리" / "model 폴더 패턴 추출" / "preferred layer 추출"
- After completing a new paper, presentation, report, or model: "이번 자료도 프로필에 반영해줘"

### Default Options

- `<aspect>`: infer the intended aspect from the request; use `all` only when no single aspect is clearly intended.
- `--mode`: default to `update`; use `init` only when the user explicitly asks to rebuild the profile from scratch.
- `--from`: when `pipeline_state.yaml` exists, resume after the last successful phase.
- `--user-refine`: **off** (global §2 — ON only when explicitly signaled).

### Direct-Edit Boundary

- For a single short note, call `/post-it --scope user <aspect> add <text>` and splice it into the manual user-notes section, whose exact legacy DB heading is `## 사용자 수동 메모`.
- For a localized update to one aspect, update the DB record through `/post-it --scope user <aspect>`; never edit a profile file because the DB is the source of truth.
- An explicit `/analyze-user <args>` invocation supplies the routing choice directly; proceed without an additional routing confirmation.

## Language Rule

- Follow an explicit artifact or audience language when provided. Otherwise, use the conversation language for user-facing output and profile-record prose.
- Preserve code, file paths, identifiers, and domain expressions when translation would reduce precision.
- Match chat tone to the conversation; keep the user-profile body concise, declarative, and report-like in the selected language.

## Core Invariants

- **Fixed adversarial QA** — expose no separate rigor flag. Phase 4 always uses four independent review axes: source coverage, pattern accuracy, fact-checking, and external adversarial review. Profiles propagate into later agent behavior, so this verification budget is mandatory.
- **Output is a DB `type=profile` record** — write with `mem add durable profile <body> --scope global --source user-profile:<stem>` (source-keyed UPSERT that preserves identity) and read with `mem profile <stem>` (rowid-descending, newest-wins tie-break).
- **Two-writer contract for the manual user-notes section** — `analyze-user update` and `/post-it --scope user` both write the exact legacy `## 사용자 수동 메모` section in the same `user-profile:<stem>` record. Read through `mem profile <stem>` before splicing. Do not use a raw query, which can select a stale duplicate and orphan promoted notes.
- **Read back each stem after writing** — immediately run `mem profile <stem>` and verify the merged result; fail loudly on mismatch.
- **No hardcoded source paths** — analyze locations supplied by the user through `--source`. If none are supplied or otherwise in scope, produce no inferred data and give one line of guidance.

## Argument & Mode Overview

`<aspect> [--source <path>] [--mode init|update] [--from discover|analyze|verify|qa|output|summary] [--user-refine]`

- **`<aspect>`** (required) — `figure`, `writing`, `presentation`, `analysis`, `domain`, `coding_convention`, or `all`. See `arguments-and-decisions.md` for the aspect-to-stem mapping (`01_paper_figure_style` through `07_coding_convention`) and source-type table.
- **`--mode`** — default to `update` for incremental accumulation; `init` performs a complete reset after snapshotting the existing body.
- **`--from`** — resume at `discover`, `analyze`, `verify`, `qa`, `output`, `repro`, or `summary`.
- **`--user-refine`** — pause immediately before Phase 5 only when the user explicitly requests a review point.
- **QA** — adversarial fixed (no flag).

For the complete aspect table, source mapping, argument semantics, decision defaults, and resume-state schema, read `arguments-and-decisions.md`.

## Pipeline Overview (6 Phases)

| Phase | Content |
|---|---|
| 1 Source Discovery | Discover, classify, and index sources; convert docx, pptx, and hwpx inputs to a PDF-and-PNG representation when needed |
| 2 Aspect Analysis | 2.1 three parallel `research/research-survey` unit shards (composed map-reduce) → 2.2 consensus synthesis (confidence 1.0/0.6/0.3) |
| 3 Cross-reference | Check consistency across aspects such as color, font, terminology, metrics, and layers |
| 3.5 Prior-version dialectic | `--mode update` only — thesis/antithesis → synthesis (confirm/refine/contradict/new) |
| 4 Multi-agent QA | Run four adversarial review axes in parallel: coverage, accuracy, fact-checking, and external review |
| 5 Output | Write the verified draft to the DB profile record (target: 7-10K tokens) |
| 5b pptx object extraction | Figure aspect only — export an SVG vector-object library |
| 6 Pipeline Summary | Append the run summary to `_internal/pipeline_summary.md` |

Read `pipeline-phases.md` for each phase's complete procedure, agent prompt, conversion command, QA reviewer contract, and output template.

## Reference Index

| File | When to load (mandatory) | Content |
|---|---|---|
| `arguments-and-decisions.md` | When interpreting arguments, defaults, or a resume point | `<aspect>` table, `--source`, `--mode init\|update`, fixed adversarial QA, `--from`, `--user-refine`, decision defaults, and `pipeline_state.yaml` resume schema |
| `pipeline-phases.md` | When running the six-phase pipeline (required) | Source discovery and conversion; aspect extraction and consensus; cross-reference validation; prior-version dialectic; multi-agent QA; output generation; pptx object extraction; pipeline summary |
| `integration-and-usage.md` | When selecting downstream profile consumers or memory integration | Role-reference matrix, memory relationship table, invocation examples, and update-frequency recommendations |

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/analyze-user.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability analyze-user
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `user_profile/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/user_profile/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
