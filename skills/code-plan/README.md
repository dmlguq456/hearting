# code-plan

> This README summarizes the portable capability for users and maintainers. The model-neutral contract lives under `<agent-home>/capabilities/`; `SKILL.md` in this directory provides shared guidance for runtime-specific projections.

## Overview

Creates a detailed implementation plan grounded in the real codebase. It runs planning as the `plan/plan-author` unit, writes the canonical `plan.md` in the selected artifact language, runs the QA loop, and synchronizes a language companion only when one is explicitly required or already belongs to the artifact set.

## Invocation

```
/code-plan <task description> [--intensity direct|quick|standard|strong|thorough|adversarial]
```

> **Caller note**: Planning benefits from `high` or `xhigh` effort. Lower effort may miss cross-file call sites.

## Language Rule

- User-facing output follows the user's communication language and should read naturally rather than as a literal translation.
- The canonical plan follows an explicit artifact or audience language; otherwise it follows the user's communication language.

## Pre-Check — Existing Plan State Gate

Search `<artifact-root>/plans/` for a similar plan and branch on its frontmatter status:

| Existing state | Handling |
|---|---|
| `active` (Critical — always ask) | Ask whether to continue the active plan or create a new one. Do not proceed until answered. |
| `done`/`failed` | Record it as a reference and create a new plan without pausing. |
| `partial` | Read `failed_steps` and create a new plan covering only the failed or dependent steps without pausing. |

## Planning — `plan/plan-author` unit

```
Plan mode. Create a new implementation plan.

Task: {$ARGUMENTS}
Save canonical plan to: <artifact-root>/plans/{YYYY-MM-DD}_{short-task-name}/plan.md
Save execution checklist to: <artifact-root>/plans/{YYYY-MM-DD}_{short-task-name}/checklist.md
Date: {YYYY-MM-DD}
{If done/failed/partial plan exists: "Reference previous plan: [path], status: [status]"}
{If partial: "Failed steps from previous execution: [list]"}
```

The `plan/plan-author` unit writes the plan file directly. The orchestrator receives only its path and summary.

## Plan-Check Assurance

`code-plan` is used for the durable code-work cycles of a `team` route. `direct` skips it; a `solo` route (`quick`) is one registered-headless dispatch-depth-1 owner with an inline micro-plan and plan-check-lite. The rigor tier, derived from `--intensity` per [`CONVENTIONS.md §1.1`](../../core/CONVENTIONS.md#11-verification-rigor-tiers), scales the plan-check budget but does not create the stage graph.

| Rigor tier | Action |
|---|---|
| quick | Direct invocation only; one fast sanity check and no repeated fix loop |
| light | One focused fast review or self-check |
| standard | One lightweight independent plan review, with at most one correction |
| thorough | Multi-axis/dispatch-depth-2 review only when selected by intensity |
| adversarial | Selected thorough budget plus adversarial, failure-mode, and security critique when available |

Record findings that remain after the selected budget in the plan's risk/unresolved section and return them to the caller.

## Optional Language Companion

After the review loop, create or update a language companion only when the user requests one, an external audience requires one, or the existing artifact set already contains one. Legacy `plan_ko.md` remains supported but is not created by default.

```
Translate mode. Canonical plan file: {plan_path}.
Target language and companion path: {target_language}, {companion_path}.
Produce a complete translation, preserve code identifiers, and return only the file path.
```

Report the canonical plan path, any companion path, a summary, and the QA verdict in the user's communication language.

---
*Portable capability contract: `<agent-home>/capabilities/code-plan.md`; shared skill guidance: `<agent-home>/skills/code-plan/SKILL.md`.*
