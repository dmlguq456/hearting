# autopilot-refine — Examples, constraints, and checklists

Invocation examples, application constraints, exclusions, and the post-apply checklist referenced by the router in `../SKILL.md`.

## Constraints

- **Prefer a blank over a false fill.** When Stage B.5 marks a claim `⚠ Unverified` and neither the artifact's `cards/` nor cross-research `<artifact-root>/research/*/cards/` confirms it, leave the claim blank or mark it `[?]` instead of inferring an answer. Do this even when the prompt appears to require a value. A placeholder is cheap; a fabricated venue, year, or task compounds across later refine cycles.
- **Do not add edits silently.** Stage D may apply only changes shown in the Stage C diff or auto-mode summary. If application exposes a new issue, skip that edit, record it under `Skipped` in `## v{N} 변경 사항`, and do not introduce a new proposal beyond the original list.
- **Version every major application.** The route-bound helper allocates and creates the snapshot before every non-direct apply. Direct minor edits and `--review-only` create no snapshot; minor edits still append their detailed history entry.
- **Treat cards as the primary research source.** For taxonomy, definition, or coverage prompts, reread `cards/*.md` and cite them in the reasoning.
- **Do not automatically rename historical citations.** Preserve published paper titles, baseline names, and challenge names. List relevant cases as intentionally untouched in Stage C.
- **Announce cross-artifact ripple effects without propagating them.** When a research change affects a downstream document artifact, record it in `Downstream sync needed` under `## v{N} 변경 사항`. The user must invoke `/autopilot-refine` on the dependent document separately.
- **Use the STRUCT escape hatch.** Halt and recommend a heavier pipeline for structural changes; do not attempt structural rewrites here.

## Examples

```text
# Default chat loop with diff preview; infer the artifact from the prompt.
/autopilot-refine "speech-enhancement-trends에서 General Restoration과 Universal SE를 task family로 통합"
# Fuzzy-match speech-enhancement-trends, show the diff, and end the turn.
# User: "all"
# Apply, snapshot to _internal/versions/v1/, and add the v2 row and change section to pipeline_summary.md.

# Auto-apply through the prompt signal, without another flag.
/autopilot-refine "speech-enhancement-trends Year×Paradigm heatmap의 2026년 칸 채우기. 확인 없이 자동 적용."

# Review only; include the artifact identifier in the prompt.
/autopilot-refine "speech-enhancement-trends에서 최신 카드 5편이 분류표에 누락됐는지 검토" --review-only

# Memo mode for a deferred review.
/autopilot-refine --memo .../review_memo.md "2026-05-06_se-seminar-tfrestormer 메모 반영"

# Document artifact, detected from the prompt keyword.
/autopilot-refine "se-seminar-tfrestormer draft Slide 4 task family 표를 4행으로 변경"

# Higher rigor before application; rigor is derived from intensity.
/autopilot-refine "se-seminar-tfrestormer 결론 챕터 wording 다듬기" --intensity standard
```

The Korean text above is intentional quoted multilingual request data; preserve it verbatim when validating invocation behavior.

## When not to use

- For a single-file typo or cosmetic edit, use direct `Edit`.
- For code artifacts, use `/code-refine`, `/code-execute`, or `/autopilot-code`.
- For whole-axis structural redesign, use `/autopilot-research --from analyze` or `/autopilot-draft --from strategy`.
- For a deferred review over hours or days, use this skill's `--memo <file>` form. `/draft-refine` is an internal autopilot-draft sub-skill, not a user entry point.

## Post-apply checklist

After a successful application:

1. When `Downstream sync needed: Yes`, run `/autopilot-refine "{dependent_artifact_name} pipeline_summary v{N} 반영"` for each dependent artifact.
2. Commit only validated, exactly owned paths when the current actor contract grants commit authority; a no-commit slice leaves the action to its stage owner.
3. If this skill contract changed, regenerate affected native projections and run `tools/skill-conformance/check.sh` and `tools/check-adaptation-boundary.sh`.
