# autopilot-draft

Document-drafting entrypoint for paper, presentation, and prose work. Close the `analyze → strategy → strategy-refine → draft → draft-refine → finalize` pipeline. This file defines routing and stage contracts; load a reference only when its detailed procedure is needed.

## First Principle: the Draft Is an Edit Plan

The output is not the final document. It is a draft cheatsheet—a mutation or edit plan that the user or `autopilot-apply` can apply to canonical source such as `main.tex`. This applies to every mode.

- `autopilot-apply` applies the cheatsheet to real source and verifies the build.
- `draft-refine` and `autopilot-refine` improve the cheatsheet, not the final source document.
- In the code analogy, draft corresponds to `code-plan` and apply corresponds to `code-execute`.

> **Output convention**: Follow CONVENTIONS §5 (`<agent-home>/core/CONVENTIONS.md#5-skill-output-convention--t1t2t3`). Store reviewer logs under `_internal/strategy_reviews/` and `_internal/draft_reviews/`, and snapshots under `_internal/versions/v{N}/strategy/` and `_internal/versions/v{N}/draft/`. Before `draft-refine` rewrites an existing file, the route write preflight must obtain the deterministic `artifact-snapshot.py` receipt; do not allocate versions or copy snapshots manually.

## Invocation

Route here when the user asks for a substantial paper, presentation, report, proposal, rebuttal, review, chapter, article, or other document draft that should proceed through strategy and verification.

Defaults:

- Infer `--mode paper|presentation|doc` from the requested output form; default to `doc` only when form remains unclear.
- Choose `--intensity` from scope and risk. Verification rigor is derived from intensity; there is no separate `--qa` axis (CONVENTIONS §1.1).
- Keep `--user-refine` off unless the user explicitly requests a pause.
- Keep clarification enabled unless `--no-clarify` is supplied.

Direct boundaries:

- One-paragraph polish, notation cleanup, or localized wording → the `editorial/polish` unit
- Structural or drift inspection without edits → `/audit`
- Minor changes to an existing document artifact → the minor path in `/autopilot-refine`
- An explicit `/autopilot-draft <args>` invocation supplies the routing choice directly

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write user-facing reports and cheatsheet prose in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve source quotations, code, paths, identifiers, and citation text when translation would reduce precision. Do not create a fixed language mirror unless the user or target workflow requests one.

Resolve `<artifact-root>` by preferring `.agent_reports` and falling back to legacy `.claude_reports`: CONVENTIONS §5.1 (`<agent-home>/core/CONVENTIONS.md#51-workspace-assumption`).

## Arguments

```text
<task description> [--mode paper|presentation|doc] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine] [--no-clarify] [--from analyze|strategy|strategy-refine|draft|draft-refine|finalize]
```

- `--mode`: select the output form. Genres such as rebuttal, review, report, and proposal remain natural-language task descriptions within `doc` mode.
- `--intensity`: choose the pipeline graph and derive its verification rigor.
- `--user-refine`: pause only at an explicitly requested review point.
- `--no-clarify`: skip Step 0 scope clarification.
- `--from <stage>`: resume an existing artifact identified by path or fuzzy short name. Restore mode, intensity, discovered inputs, and `user_refine` from `pipeline_state.yaml`; explicit CLI flags take precedence.
- After removing flags, treat the remaining text as the task description. Discover inputs from `<artifact-root>/{analysis_project,research}/`; there is no `--refs` flag.

Read `invocation-and-args.md` for complete parsing and resolution rules.

## Pipeline

Write the artifact under `<artifact-root>/documents/{YYYY-MM-DD}_{short-name}/`.

| Resume stage | Step | Contract | Reference |
|---|---|---|---|
| pre | Pre-flight Validation | Validate mode, input, and format specification before creating directories or invoking a sub-capability | `pipeline-steps.md` |
| — | Step 0 Scope Clarification | Resolve materially ambiguous scope with 2-4 concise questions; skip with `--no-clarify` | `pipeline-steps.md` |
| `analyze` | Step 1 Material Analysis | Inventory references and write mode-specific analysis under `analysis/` | `pipeline-steps.md` |
| `strategy` | Step 2 `draft-strategy` | Produce the strategy and required `## Style Guide` | `pipeline-steps.md` |
| `strategy-refine` | Step 3 Strategy Review | Dispatch the `strategy-review` sibling node (`research/fact-check` unit); call `draft-refine` only when revision is needed | `review-and-qa.md` |
| `draft` | Step 4 Draft Generation | Discover or extract figures and produce the cheatsheet draft through the `research/research-survey` unit; add an audience-language mirror only when required | `pipeline-steps.md` |
| — | Step 4b Factual Detector | Run the orchestrator-side factual scan selected by the graph | `pipeline-steps.md` |
| `draft-refine` | Step 5 Draft Review | Dispatch the `quality-review` (`editorial/review`) and `fact-verify` (`research/claim-verify`) sibling nodes; call `draft-refine` when revision is needed | `review-and-qa.md` |
| — | Step 5.5 Editorial Polish | Apply the finalize polish pass (`editorial/report` unit) for `standard+` | `pipeline-steps.md` |
| `finalize` | Step 6 Pipeline Summary | Write `pipeline_summary.md` and report the result in the selected user-facing language | `summary-and-safety.md` |

The Step 4 drafting prompt embeds tone propagation, mode-specific conventions, draft structure, and quality requirements. Treat `convention-{common,paper,presentation,doc}.md` as the single source for those conventions.

## Safety Essentials

Read `summary-and-safety.md` for the complete rules. Always enforce these invariants:

- Do not fabricate citations, data, or results. Cite only materials present in `{discovered_inputs}`; mark uncertainty or missing content as `[TODO: ...]`.
- Treat the output as a working cheatsheet, not a final document or an already-applied source change.
- For rebuttal-response intent, answer every reviewer point; omissions are critical errors.
- For peer-review intent, ground scores in the paper and require a format specification during preflight.
- In presentation mode, do not claim to insert unavailable figures. Describe the required visual explicitly and leave PPTX conversion to the selected downstream tool or user workflow.

## Reference Index

| File | When to load (mandatory) | Content |
|---|---|---|
| `invocation-and-args.md` | When parsing arguments, defaults, resume state, or artifact structure | Mode inference, input discovery, intensity-derived rigor, `--user-refine`, `--from`, format-spec resolution, decision defaults, `pipeline_state.yaml`, input-source convention, artifact structure |
| `pipeline-steps.md` | When running preflight or Steps 0, 1, 2, 4, 4b, or 5.5 | Orchestration, figure discovery and extraction, image-quality policy, path convention, drafting prompt, optional language mirror, factual detector, editorial polish |
| `review-and-qa.md` | When running Step 3 or Step 5 | Rigor-scaled reviewer axes, domain/content, methodology/writing, style, cross-reference/coverage, quality review, and fact-checking prompts |
| `summary-and-safety.md` | At Step 6 or another terminal state | Pipeline-summary template, process log, artifacts, decision points, and complete safety rules |
| `convention-common.md` | At Step 4 for every output form | Shared drafting rules |
| `convention-paper.md` | At Step 4 for paper mode | Academic and paste-ready paper contract |
| `convention-presentation.md` | At Step 4 for presentation mode | Slide drafting contract |
| `convention-doc.md` | At Step 4 for document mode | Word, HWP, and Markdown prose contract |

## Task

$ARGUMENTS

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/autopilot-draft.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability autopilot-draft
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `documents/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/documents/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
