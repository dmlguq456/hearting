## Pipeline Summary Template

Write `{log_dir}/pipeline_summary.md` as the first action on every terminal state—success, partial, failure, or stop—before reporting to the user.

This is a process log and artifact index, not change analysis; code-report owns change analysis. Populate Decision Points from recorded pauses. If none, use `| - | No gated decisions triggered | - | - |`.

```markdown
# {mode_title}: {task_or_error_name}

- **Date**: {YYYY-MM-DD}
- **Status**: done / partial / failed{debug: " / unresolved"}
{mode_specific_fields}
- **User-Refine**: {true | false}

## Process Log
| Step | Skill/Action | Result | Notes |
|---|---|---|---|
{mode_specific_rows}

## Artifacts
{mode_specific_artifacts}

## Decision Points
| Step | Decision | User Response | Action Taken |
|---|---|---|---|
```

| Field | dev | debug |
|---|---|---|
| Title | `Pipeline Summary` | `Debug Pipeline Summary` |
| Extra headers | `Plan: {en_plan_path}` | `Error`, `Root Cause`, `Fix Plan`, and `Attempts` |
| Process rows | Steps 1–5 plus 4R retry | Steps 1–6; Step 1 is diagnosis and Step 3 has no separate row |
| Artifacts | `plan/`, `dev_logs/`, `test_logs/`, `_internal/{plan_reviews,dev_reviews,test_reviews}/`, final report | Same minus research artifacts |

## Safety

- A catastrophic execution failure, `status: failed`, stops immediately after writing the summary.
- Record in the existing Process Log or Notes which task-owned edits were recovered, which remain, why any edit was ambiguous, the observed index/worktree state, and any dependent checks skipped. A failed status does not mean the source was fully restored.
- Run tests only when they are safe and independent of unresolved changes. If a test depends on ambiguous or remaining edits, skip it and record why.
- Stage-local checks stay small, independent QA runs only when selected by the graph, and final verification remains concrete.
- In debug mode, fix only the bug; do not refactor adjacent code or change behavior that already worked.
- Ask the user only when multiple root causes remain plausible.
- For environment failures, report environment repair steps and do not edit code.
