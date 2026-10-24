# draft-strategy

## Language Rule

Follow an explicit target, venue, audience, or source-artifact language first. Otherwise, write strategy artifacts and user-facing output in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve quotations, code identifiers, citations, paths, and venue-specific terms when translation would reduce precision.

## Parse Arguments

Parse `$ARGUMENTS` as follows:

- first word, `mode`: `rebuttal | paper | review | report | proposal | presentation`
- `--inputs <comma-separated-paths>`: pre-discovered artifact directories, usually `analysis_project/{paper,doc}/...` or `research/{topic}/`
- `--output <dir>`: `$AGENT_ARTIFACT_OUTPUT_DIR/documents/{date}_{name}/`
- `--intensity`: derive the `quick | light | standard | thorough | adversarial` verification tier through CONVENTIONS §1.1 (`<agent-home>/core/CONVENTIONS.md#11-verification-rigor-tiers`)
- remaining text: task description and context

## Pre-Check

Verify the expected analysis files under `{output_dir}/analysis/`:

- every mode: `material_index.md`
- rebuttal: `reviewer_analysis.md`
- paper, review, report, proposal, or presentation: `ref_analysis.md`

If a required file is missing, report that `autopilot-draft` Step 1 did not complete; do not invent the analysis.

## Mode Routing

Select one strategy template from the first argument:

| Mode | Strategy focus |
|---|---|
| `rebuttal` | Meta-review, priority matrix, and reviewer-by-reviewer response |
| `paper` | Positioning, contribution, outline, and evidence |
| `review` | Evidence-grounded peer-review response |
| `report` | Objective, findings, and section plan |
| `proposal` | Problem, approach, work plan, and impact |
| `presentation` | Audience, core message, and slide outline |

`autopilot-draft` maps its form-first `paper`, `presentation`, or `doc` route to these six labels during preflight. A `doc` request maps to rebuttal, review, report, or proposal from task intent, with report as the final fallback. A direct `draft-strategy` invocation must supply one of the six labels.

Read [references/delegate-prompt.md](delegate-prompt.md) for the complete mode mapping, six templates, paragraph-cohesion precheck, tone detection, slide conventions, and quality requirements.

## Flow

1. Parse arguments and verify `{output_dir}/analysis/`.
2. Load the complete prompt from `delegate-prompt.md` and dispatch the `research/research-survey` unit. Require it to write the strategy file directly and return only paths plus a 3–5 line summary.
3. Apply the quality and fact-check focus in `qa-review.md` when those roles are selected; the compiled graph owns dispatch, parallelism, and retry limits.
4. Create a translated companion only when an explicit second-language or external-audience contract requires it. Use the `editorial/translate` unit with `mirror.md`; do not infer a fixed mirror language from the conversation.
5. Report strategy paths, compact summary, and QA verdict in the conversation language.

## Reference Index

| File | Load when | Contents |
|---|---|---|
| `delegate-prompt.md` | Building the `research/research-survey` unit prompt | Inputs, paragraph-cohesion precheck, mode mapping, six strategy templates, tone detection, slide conventions, quality requirements, and return contract |
| `qa-review.md` | Selecting and running QA | Rigor scaling, fast fact-check rationale, reviewer and fact-checker prompts, verdict branches, and two-round cap |
| `mirror.md` | An explicit companion-language artifact is required | `editorial/translate` unit procedure, primary-language decision, and final report line |

## Task

$ARGUMENTS
