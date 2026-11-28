# code-refine

> This README summarizes the portable capability for users and maintainers. The model-neutral contract lives under `<agent-home>/capabilities/`; `SKILL.md` in this directory provides shared guidance for runtime-specific projections.

## Overview

Updates an existing plan from user annotations, plan-check feedback, or verification-failure notes without implementing it. The canonical plan remains authoritative; synchronize only companions that already exist or are explicitly required.

## Invocation

```
/code-refine <plan name or path>
```

## Plan Resolution

> The single authority for resolving `$ARGUMENTS` to a plan path is [autopilot-code/references/arguments-and-decisions.md#plan-resolution](../autopilot-code/references/arguments-and-decisions.md). Recognize canonical `plan.md` and an existing companion such as legacy `plan_ko.md`; swap between known companion paths only when both files belong to the same plan.

## Refinement — `plan/plan-author` unit

```
Refine mode. Update an existing implementation plan from user memos, plan-check feedback, or verification-failure notes.

Supplied plan: {$ARGUMENTS}
Canonical plan: {resolved plan.md path}
Existing companion plans: {resolved paths, or none}

Read the supplied and canonical plans and identify user annotations. Formats:
- <!-- memo: ... --> (standard)
- <!-- ... --> (any HTML comment)
- // ... (inline)
- [memo] ... (bracketed)
- (**...**) (parenthetical)
Do NOT treat the plan's original author-written prose as a memo.

Re-read relevant source files when needed, update the canonical plan in place, and synchronize existing required companions.
Remove memo comments after incorporating them.
Return which steps were changed and a brief summary.
```

## Rigor Scaling

Derive the verification rigor tier from the plan's selected `--intensity` context per [`CONVENTIONS.md §1.1`](../../core/CONVENTIONS.md#11-verification-rigor-tiers). This is an optional correction of a durable plan, not an automatic stage in `direct`, `solo` or `quick`.

| Rigor | Review after refinement | Correction budget |
|---|---|---|
| Quick | Direct invocation only: one fast sanity review or self-check | Record residual concerns; no repeated loop |
| Light | One focused fast review when execution could be affected | One bounded correction for blocking issues |
| Standard | One lightweight `qa/plan-review` unit pass (the `plan-check` boundary) over changed steps | At most one correction |
| Thorough | Multi-axis review only when selected by `intensity=thorough` | Up to two synthesized corrections |
| Adversarial | Thorough review plus explicit failure-mode, security, and adversarial critique when available | Fail loudly when explicitly requested and unavailable; otherwise report a fallback to Thorough |

## Selected Post-Refine Review Pass

Create `{log_dir}/_internal/plan_reviews` and run only the review action selected by the caller's graph:

- Light/Standard: one focused review — `Review changed steps. Plan: [path], Changed: [list]. Write to: {log_dir}/_internal/plan_reviews/refine_round_{N}.md`
- Thorough/Adversarial: the bounded multi-axis or adversarial review selected by intensity

**Verdict handling**:

- No 🔴 → finish and report to the user.
- Any blocking finding → re-enter the `plan/plan-author` unit for no more than the selected correction budget, then rerun only the selected check.
- Any finding remaining after the budget → add it to the plan's risk or unresolved section, preserving an existing functional compatibility heading such as `## 미해결 이슈`, and report changed steps plus resolved and unresolved issues to the caller.

---
*Portable capability contract: `<agent-home>/capabilities/code-refine.md`; shared skill guidance: `<agent-home>/skills/code-refine/SKILL.md`.*
