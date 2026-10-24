# §doc — Word, HWP, and Markdown prose

> These rules apply to autopilot-draft `--mode doc` in addition to `convention-common.md`.

The user template or venue format specification determines the document structure. This file supplies genre defaults only. Prefer the natural-language genre intent in the task and the user template in `analysis_project/doc/{matching}/formats/`.

## Shared rules

- Choose tone, tense, and artifact language for the audience and venue. Reports generally use past tense, proposals future tense, and rebuttal responses a deliberate mixture.
- Follow the user template. Use a generic fallback only when no template exists.
- Present quantitative metrics in a table when that improves comparison.

## Technical report, mid-project report, post-mortem, or quarterly report

Prefer the organization, institution, or lab template. Without one, use: Executive Summary; Background; Method; Results and Analysis; Discussion; Recommendations; Appendix.

- Mark time-varying assets with `snapshot YYYY-MM-DD` so later retraining or reporting cycles remain interpretable.
- Structure a post-mortem as timeline, root cause, fix, and preventive measures.

## Grant or business proposal

Prefer the funding-body template. NRF, NSF, Horizon, and industry-academic offices differ in page limits, required sections, and evaluation criteria, so preprocessing the relevant material into `analysis_project/doc/{matching}/formats/` is strongly recommended.

Without a template, use: Motivation; Approach; Preliminary Results; Timeline and Milestones; Budget; Impact; Risks.

## Rebuttal response

Prefer the venue rebuttal format, including length limit and subtype: `meta-only`, `reviewer-dialogue`, or `response-with-revision`. Ask the user how to proceed if no format is available.

Respond point by point using acknowledgment → core argument → evidence → conclusion. Every reviewer point requires a response; omission is a critical error.

Camera-ready integration belongs to the natural-integration rule in `convention-paper.md`. A rebuttal response and a paper-body revision are different genres.

## Peer review

The venue review form is mandatory. Abort preflight when `analysis_project/doc/{matching}/formats/` has no applicable form. Do not maintain built-in presets because forms change across OpenReview, ACL ARR, IEEE conferences, journals, and review years.

Justify scores with specific evidence from the paper and cite the relevant section, figure, or table. Keep the tone professional and constructive.
