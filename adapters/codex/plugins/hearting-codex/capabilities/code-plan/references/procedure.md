# code-plan

Use the deepest eligible planning profile selected by the active adapter when cross-file reasoning or call-site analysis would benefit from it.

> **Stage-session entry (`standard+` dispatch, spec/stage-dispatch SD-2)**: Run in-session or as an isolated dispatch-depth-2 stage worker dispatched by the `autopilot-code` conductor. Inputs are the task and paths supplied by the route; never depend on prior-stage conversation. The stage uses its node's declared outputs and write scope. Independent plan review is the sibling `plan-check` node, not an in-stage delegation.

> **Language rule**: Follow the audience and artifact language contract in [arguments-and-decisions.md#language-rule](../../autopilot-code/references/arguments-and-decisions.md). Write the canonical plan in the selected artifact language; do not generate a language mirror merely because the skill source is English or the conversation uses a particular language.

## Pre-Check

For standalone planning, search `$AGENT_ARTIFACT_OUTPUT_DIR/plans/` for a similar plan and branch on its frontmatter status:

- `active`: Ask whether to continue the active plan or create a new one. Do not proceed until that genuine choice is resolved.
- `done` or `failed`: Note it as a reference and create a new plan without pausing.
- `partial`: Read `failed_steps` and create a new plan covering only those failed or dependent steps without pausing.

Record any user-facing pause for `pipeline_summary.md` Decision Points.

## Delegate Planning

Run the `plan/plan-author` unit with this task, adapted only for the selected artifact language and known prior-plan state:

```text
Plan mode. Create a new implementation plan.

Task: {$ARGUMENTS}
Intent: {path supplied by the route when present — its Decisions bind the plan}
Save canonical plan and checklist to: the plan node's declared outputs
Artifact language: {selected audience or conversation language}
Date: {YYYY-MM-DD}
{If a done/failed/partial plan exists: "Reference previous plan: [path], status: [status]"}
{If partial: "Failed steps from previous execution: [list from plan frontmatter failed_steps]"}
```

Plan procedure, plan structure, and the single-line return contract are owned by the `plan/plan-author` unit persona; do not restate them in the prompt. The stage orchestrator receives only the unit's return line (`{path} -- {verdict}`); plan content stays in the file.

## Slices Block

Add a `slices` block only when the work splits into 2–4 packages that own exactly disjoint files and each can finish on its own. The block is one fenced code block whose info string is `slices`, holding a JSON list; the owner's `dispatch-batch.py --slices <plan.md>` reads it and starts the packages in parallel:

````text
```slices
[
  {"id": "core", "files": ["utilities/a.py", "utilities/a.test.py"], "verify": "python3 utilities/a.test.py"},
  {"id": "docs", "files": ["core/OPERATIONS.md"], "verify": "grep -q 'new text' core/OPERATIONS.md", "brief": "only the §5.10 paragraph"}
]
```
````

- Required per slice: `files` (exact paths, no globs, relative to the worktree) and `verify` (one line). Optional: `id` (`[a-z0-9-]`), `brief`, `adapter`, `expected_round_trips`.
- Write exactly one block. If the packages share a file, or one cannot finish without another, write no block: the stage then runs as one session. Do not invent a split to use the block.

## Plan-Check Assurance

Derive verification rigor from the caller's `--intensity` and plan risk under CONVENTIONS §1.1 (`<agent-home>/core/CONVENTIONS.md#11-verification-rigor-tiers`). Rigor does not select this stage: `code-plan` runs only after the caller chooses a durable `standard+` graph. `direct` skips it; `quick` uses a one-shot worker with an inline micro-plan and plan-check-lite.

Use the supplied `plan.md` path to locate its sibling checklist and logs. The independent `plan-check` node owns its review output.

| Rigor | Plan-check action | Correction budget |
|---|---|---|
| `quick` | Normally unreachable; if invoked directly, run one fast sanity review or self-check | Record residual concerns; no repeated loop |
| `light` | One focused fast review or equivalent self-check | One pass only when an issue blocks execution |
| `standard` | One independent review for feasibility, missing steps, and concrete verification commands | At most one correction |
| `thorough` | Deeper or multi-axis review when explicitly selected by the graph | Up to two corrections after synthesizing reviews |
| `adversarial` | Thorough review plus failure-mode, security, and adversarial critique when the adapter proves availability | Explicit unavailable requests fail loudly; automatic escalation falls back to thorough |

After the `plan/plan-author` unit returns:

1. Check whether the selected graph requires independent review. Otherwise run an inline plan-check.
2. When independent review is required, the conductor dispatches the `plan-check` sibling node (unit `qa/plan-review`), which writes its declared review output. Use bounded separate reviewers only when the owner-worker graph and rigor select them.
3. If blocking issues exist, run **one batched correction** through the `code-refine` boundary: close every 🔴 of `round_{N}.md` together, including the follow-on gaps the review named. Then dispatch the re-review as the next ordinary round — the shared `review_round_cap.round_budget` admits it like any other round, `round_kind=correction`, with no separate "round N+1" special case — carrying a `Round protocol` block naming the round number and `round_{N}.md`; it is a closure check (prior 🔴 closed? delta clean?), not a fresh independent pass. Never redispatch the full `plan` node for a correction. The runtime auto-records the plan's own revision marker (`recorded_by=runtime-auto`) before admitting that round whenever `plan-check`'s last round was a blocking FAIL whose finding basis-verifies against the plan's gate evidence (SD-154 rule 8); when that basis verification cannot run itself, the revision stays unrecorded and `plan-check`'s launch is refused `completion-evidence-revised-unrecorded` with `next_action` naming `capability-route.py revise --route <route> --node plan --evidence <evidence-path> --basis review-findings|user-direction|owner-correction` — run it, or record the revision another way, before redispatching. Budget: at most one correction at standard or the selected thorough/adversarial budget; do not loop solely because rigor is high.
4. If concerns remain after the budget (unit = **verdict round** — a crashed or capacity-dead round never spends it, and two verdict-less rounds in a row bind the node to `verdictless-bound` instead, per `CONVENTIONS §1.1`), `plan-check` may still admit exactly one further round as a `closure-check` — never a second ordinary correction — when the last blocking round's FAIL is exactly the one a revision names as its answer. Once that is also unavailable, add the remaining concerns to the plan's risk or unresolved section and continue only when the caller can safely own the risk. When the last round still reported blocking findings, use the closure evidence path supplied by the route and complete `plan-check` against the last blocking attempt row; the gate refuses it while budget remains.

Record any user-facing pause, including active-plan ambiguity, for the pipeline summary.

## Optional Audience-Language Companion

The canonical `plan.md` should normally be sufficient because its prose already follows the selected artifact language while code identifiers and paths remain unchanged. Create or update a companion only when the user explicitly requests a second language, an external audience contract requires it, or an existing workflow depends on a legacy companion such as `plan_ko.md`.

When a companion is required, dispatch the `editorial/translate` unit (a conditional companion node appended at compile) to translate from canonical `plan.md` while preserving code identifiers, file paths, library names, step numbering, and semantics. Use the existing project naming convention for the output. Consult memory for writing preferences only when the acting agent judges the retrieved preference relevant; project and explicit audience requirements take precedence.

Report the canonical plan path, any requested companion path, a compact summary, and the QA verdict in the conversation language.

## Task

$ARGUMENTS
