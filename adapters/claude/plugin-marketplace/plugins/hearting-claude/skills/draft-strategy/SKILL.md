---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: draft-strategy
description: "Use only when autopilot-draft dispatches document strategy and evidence-plan creation. Not for top-level user requests or primary capability routing."
argument-hint: "<mode> --inputs <comma-separated-paths> --output <artifact-dir> [--intensity direct|quick|standard|strong|thorough|adversarial] <task description>"
metadata:
  group: sub
  fam: sub
  invocation_class: parent-invoked
  modes: ["rebuttal", "paper", "review", "report", "proposal", "presentation"]
  blurb: "Create an initial document strategy and evidence-based writing plan."
  use_when: "Use only when autopilot-draft dispatches document strategy and evidence-plan creation."
  not_for: "Not for top-level user requests or primary capability routing."
---

# draft-strategy

## Language Rule

Follow an explicit target, venue, audience, or source-artifact language first. Otherwise, write strategy artifacts and user-facing output in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve quotations, code identifiers, citations, paths, and venue-specific terms when translation would reduce precision.

## Parse Arguments

Parse `$ARGUMENTS` as follows:

- first word, `mode`: `rebuttal | paper | review | report | proposal | presentation`
- `--inputs <comma-separated-paths>`: pre-discovered artifact directories, usually `analysis_project/{paper,doc}/...` or `research/{topic}/`
- `--output <dir>`: `$AGENT_ARTIFACT_OUTPUT_DIR/documents/{date}_{name}/`
- `--intensity`: derive the `quick | light | standard | thorough | adversarial` verification tier through [CONVENTIONS §1.1](../../core/CONVENTIONS.md#11-verification-rigor-tiers)
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

Read [references/delegate-prompt.md](references/delegate-prompt.md) for the complete mode mapping, six templates, paragraph-cohesion precheck, tone detection, slide conventions, and quality requirements.

## Flow

1. Parse arguments and verify `{output_dir}/analysis/`.
2. Load the complete prompt from `references/delegate-prompt.md` and dispatch the `research/research-survey` unit. Require it to write the strategy file directly and return only paths plus a 3–5 line summary.
3. Apply QA scaling and the bounded quality/fact review in `references/qa-review.md`; run the selected reviewers in parallel and allow at most two rounds.
4. Create a translated companion only when an explicit second-language or external-audience contract requires it. Use the `editorial/translate` unit with `references/mirror.md`; do not infer a fixed mirror language from the conversation.
5. Report strategy paths, compact summary, and QA verdict in the conversation language.

## Reference Index

| File | Load when | Contents |
|---|---|---|
| `references/delegate-prompt.md` | Building the `research/research-survey` unit prompt | Inputs, paragraph-cohesion precheck, mode mapping, six strategy templates, tone detection, slide conventions, quality requirements, and return contract |
| `references/qa-review.md` | Selecting and running QA | Rigor scaling, fast fact-check rationale, reviewer and fact-checker prompts, verdict branches, and two-round cap |
| `references/mirror.md` | An explicit companion-language artifact is required | `editorial/translate` unit procedure, primary-language decision, and final report line |

## Task

$ARGUMENTS
