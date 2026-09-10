## Language Rule

User-facing artifacts follow `<agent-home>/roles/response-policy.md`: explicit target, audience, and existing-artifact language first, then the user's current communication language. This is the code-track language source for plan, refine, execute, test, and report; it imposes no fixed locale.

## Argument Parsing

### `--mode` — required

- `--mode dev` — development pipeline; default when omitted.
- `--mode debug` — runtime-error diagnosis and fix.
- `--mode audit` — whole-codebase or application inspection, parallel review, triage, low-risk verified corrections, and flags for risky findings. See `Pipeline: Mode audit`.
- Omitted → use `dev` and warn once in the user's communication language.
- Invalid value → report the error and stop.

### `--from <step>`

- dev supports `plan`, `refine`, `execute`, `test`, and `report`.
- `plan` = Step 1 code-plan; `refine` = Step 2 code-refine and review memos; `execute` = Step 3; `test` = Step 4; `report` = Step 5.
- debug does not support resume and always starts with diagnosis. Warn and ignore `--from` when combined with debug.

### `--intensity <level>`

Intensity selects the stage graph; see [CONVENTIONS §1](../../../core/CONVENTIONS.md#1-pipeline-intensity-stage-graph-and-assurance-canonical).

- `direct`: intake → produce → sanity/report; no code-plan, plan-check, or durable plan.
- `quick`: intake → orient-lite → micro-plan → plan-check-lite → produce → verify-lite → report; no independent QA after every stage.
- `standard`: frame already ran as a fixed two-leg depth-0 bootstrap (anchor one tier above the owner via `model_profile.frame_profile_for_owner`, never a fixed profile name in prose) before this graph starts → durable code-plan → plan-check → code-execute → impl-review → code-test → code-report.
- `strong`: run separate 2-way plan (`plan` + `plan-alternative`) and implementation-review (`impl-review` + `impl-review-alternative`) groups before their declared convergence points; frame stays the same fixed two legs as `standard` — no third frame leg.
- `thorough` and `adversarial`: widen plan plus implementation review to three legs by adding the sealed implementation-risk and failure-mode perspectives; frame still has no third leg at any intensity. Every width/profile/perspective comes from registry-v6; no owner invents a leg.

Every non-direct graph has a plan-check, but expensive independent QA does not repeat after every substage.

### Verification Rigor

Rigor is derived from intensity and is not a separate `--qa` axis; see [CONVENTIONS §1.1](../../../core/CONVENTIONS.md#11-verification-rigor-tiers).

- `direct` → none/light
- `quick` → quick
- `standard` or `strong` → standard
- `thorough` → thorough
- `adversarial` → adversarial

Rigor changes plan-check, selected review, and final code-test depth; it does not select a graph. Dispatch-depth-2 dispatch belongs to the standard+ owner-worker graph, not rigor alone. direct and quick do not open dispatch depth 2 without explicit escalation.

The code track has no card/PDF fact-checker. Its ground truth is source, tests, runtime behavior, API and CLI surfaces, and security review. Security-sensitive auth, secrets, external input, API-contract, or deserialization work may add `roles/units/qa/security-review.md`; claim that pass only when it ran.

High-stakes wording may justify raising intensity to strong, thorough, or adversarial. Durable standard+ cycles store intensity and derived rigor in plan frontmatter or pipeline state. Mid-cycle changes affect later checks but do not rewrite completed graph stages.

### `--user-refine` — explicit opt-in only

Default false. The orchestrator must not add this flag unless the original request explicitly includes it or an equivalent signal such as `"사용자 검토 끼워"` or `"memo 추가하게 멈춰줘"`.

In dev mode, pause after review or failure memos are written and before code-refine:

1. Do not invoke code-refine.
2. Set plan frontmatter `user_refine: true` and `paused_at_stage: refine`.
3. Report the memo path and localized resume instruction: `/autopilot-code --mode dev --from refine <plan-name>`.
4. Exit without writing `pipeline_summary.md`; pause is not a terminal state.

Debug normally skips research review, so ignore the flag with one warning. On `--from refine`, skip Step 1 and invoke code-refine, then continue. Preserve `user_refine` from canonical plan frontmatter when the flag is absent on resume.

After removing flags, remaining text is the task description, plan name, or error description. For dev resume at Step 2 or later, it must identify a plan rather than a new task.

## Decision Defaults

The pipeline pauses only for genuine ambiguity or an explicit user-refine request.

| Decision point | Default |
|---|---|
| code-test failure | Open at most one bounded dev retry when the selected graph permits it. |
| Catastrophic `plan status: failed` | Stop and report without retry. |
| Final retry failure | Write failed summary and stop. |
| Many plan-review memos | Refine automatically unless `--user-refine` is set. |
| Existing active plan | Always ask whether to resume or create a new plan. |
| Existing done or failed plan | Create a new plan and reference the old one. |
| Existing partial plan | Create a new plan for `failed_steps`. |
| Debug diagnosis unambiguous | Proceed automatically. |
| Debug diagnosis ambiguous | Always list candidates and ask which to investigate. |
| Debug verification fails | Roll back and report. |
| Environment issue | Report repair steps; do not edit code. |

Record actual pauses in the summary's Decision Points table. Do not log every automatic branch as a gated decision.

## Plan Resolution

Resolve `$ARG` to a plan file:

1. `.md` suffix → use directly.
2. Directory → append `/plan.md`.
3. Otherwise search all projects: `ls -d <artifact-root>/plans/*/*$ARG* 2>/dev/null`.
   - One match → `{match}/plan.md`.
   - Multiple → prefer a folder without `_audit` or `_fix_`; ask if still ambiguous.
   - None → report an error.
