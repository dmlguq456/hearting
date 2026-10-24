### Step 6: Pipeline summary

Always write `{strategy_folder}/pipeline_summary.md` before reporting completion.

```markdown
# Document Strategy Pipeline Summary: {task name}

- **Date**: {YYYY-MM-DD} | **Mode**: {mode} | **Format-ref**: {format_ref or "fallback-generic"} ({format_ref_source}) | **Status**: done / reviewed / draft
- **User-Refine**: {true | false}
- **Discovered inputs**: {discovered_inputs}

## Process Log
| Step | Action | Result | Notes |
|---|---|---|---|
| 0 | Scope Clarification | clarified / skipped | {questions or "--no-clarify"} |
| 1 | Material Analysis | completed | {N} files |
| 2 | draft-strategy | created | {strategy path} |
| 3 | Strategy Review (`strategy-review` node, `research/fact-check`) | memos added / no issues | {memo count} |
| 3b | draft-refine | refined / skipped | |
| 4 | Draft Generation | created | {draft path} |
| 5 | Draft Review (`quality-review` + `fact-verify` nodes) | memos added / no issues | {memo count} |
| 5b | draft-refine (draft) | refined / skipped | |
| 5.5 | finalize polish (`editorial/report` unit, mode B) | polished / skipped (rigor < standard) | |

## Artifacts
- Strategy: {primary path} | Mirror: {mirror path or "not needed"}
- Draft: {primary path} | Mirror: {mirror path or "not needed"}
- Analysis: {reviewer_analysis or ref_analysis path}
- Material Index: {path} | Strategy Review: {path} | Draft Review: {path}

## Decision Points
| Step | Decision | User Response | Action Taken |
|---|---|---|---|
| (populate from the orchestrator's decision log) |
```

If the run had no pauses, write `| - | No pause points triggered | - | - |`.

Then report in the user's communication language:

- Strategy path and a two- or three-line summary.
- Draft path and a two- or three-line summary.
- For presentation output, remind the user that PowerPoint transfer is manual.
- For peer-review output, identify the auto-discovered venue form; there are no built-in presets.

After `pipeline_summary.md` is durable, evaluate the route-sealed optional artifact-sink extension from `WORKFLOW.md §0.2`. When available, offer the canonical document through the app-neutral receipt contract; on unavailable, record `skipped/extension-unavailable` without invalidating the document.

## Safety rules

- Do not fabricate citations or results. Reference only material present in `{discovered_inputs}`.
- Treat the draft as a working artifact for user editing, not a final publication. Mark uncertainty with `[TODO: ...]`.
- For a rebuttal response, address every reviewer point. Determine `meta-only`, `reviewer-dialogue`, or `response-with-revision` from the format spec or task. If neither provides it, resolve it in Step 0.
- For a peer review, justify every score with paper evidence. A matching venue review form under `analysis_project/doc/{matching}/formats/` is mandatory.
- For other genres, record the format-spec source in strategy frontmatter so later refinement preserves it.
- For presentation work, describe visuals concretely in the localized visual block. Do not fabricate or silently insert images. This pipeline does not export PPTX.
- Briefly report the material inventory, then continue automatically.
