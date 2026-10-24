# code-refine

> **Plan resolution**: Treat [arguments-and-decisions.md#plan-resolution](../../autopilot-code/references/arguments-and-decisions.md) as the single authority for resolving `$ARGUMENTS`. Recognize both canonical `plan.md` and any existing companion such as legacy `plan_ko.md`; swap between known companion paths only when both files actually belong to the same plan.

> **Language rule**: Follow the audience and artifact language contract in [arguments-and-decisions.md#language-rule](../../autopilot-code/references/arguments-and-decisions.md). Preserve the canonical plan's selected language and synchronize only companions that already exist or are explicitly required.

## Delegate Refinement

Re-enter the `plan/plan-author` unit boundary with this task:

```text
Refine mode. Update an existing implementation plan from user memos, plan-check feedback, or verification-failure notes.

Supplied plan: {$ARGUMENTS}
Canonical plan: {resolved plan.md path}
Existing companion plans: {resolved paths, or none}

Read the supplied and canonical plan files and identify user memos per your refine-mode memo forms; additionally treat any HTML comment as a user memo. Do not treat original plan prose as a memo. Then follow your refine-mode procedure and return format.
```

Memo forms, in-place update, companion synchronization, and the single-line return contract are owned by the `plan/plan-author` unit persona; do not restate them in the prompt.

## Refine Assurance

Derive verification rigor from the plan's selected `--intensity` context under CONVENTIONS §1.1 (`<agent-home>/core/CONVENTIONS.md#11-verification-rigor-tiers`). This capability is an optional correction of a durable plan; it is not an automatic stage in `direct` or `quick`.

| Rigor | Review after refinement | Correction budget |
|---|---|---|
| `quick` | Direct invocation only: one fast sanity review or self-check | Record residual concerns; no repeated loop |
| `light` | One focused fast review when changed steps could affect execution | One bounded correction for blocking issues |
| `standard` | One lightweight `qa/plan-review` unit pass (the `plan-check` boundary) over changed steps | At most one correction |
| `thorough` | Multi-axis review only when selected by `intensity=thorough` | Up to two synthesized corrections |
| `adversarial` | Thorough review plus explicit failure-mode, security, and adversarial critique when available | Explicit unavailable requests fail loudly; automatic escalation falls back to thorough and reports it |

After the `plan/plan-author` unit returns, run only the review action selected by the caller's graph. Do not open a repeated QA loop merely because the rigor tier is high. A refinement is one **verdict round** against the caller's shared `review_round_cap.round_budget` (`CONVENTIONS §1.1`); a crashed or capacity-dead prior round never spent it, so this refinement may still be the first real correction even when it is not the first attempt. Add unresolved concerns to the plan's risk or unresolved section after the selected budget and report them to the caller.

## Task

Refine the plan at: $ARGUMENTS
