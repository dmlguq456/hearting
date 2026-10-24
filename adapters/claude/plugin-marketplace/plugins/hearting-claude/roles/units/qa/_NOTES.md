# qa units — authoring residue (review required)

Content mined from the legacy sources that found no home in a unit body, plus recorded
merge divergences. Sources: `roles/modes/qa/*.md` (EN), `adapters/claude/agent-modes/qa/*.md`
(KO), `adapters/claude/agents/qa-team.md` (team file).

1. **Security confidence bar 7-vs-8 drift — resolved to 8.**
   `adapters/claude/agent-modes/qa/security-review.md:56` adopts confidence 7–10 (with
   4–6 "needs investigation"), while `roles/modes/qa/security-review.md:27` keeps only
   8–10. Per the locked decision the unit resolves to **8** and drops the
   "needs investigation" middle band entirely (below 8 = drop). The KO 4–6 band is
   therefore intentionally not carried.

2. **`/code-review ultra` user-owned escalation.** `adapters/claude/agents/qa-team.md:40`:
   the user may invoke the cloud multi-agent tier above the harness's adversarial QA
   combination; the router could not invoke it itself. This is a surface-specific,
   user-owned escalation path — not portable unit behavior. Needs a home in the entry
   skill or Claude adapter doc when the team file is deleted (workstream B/C).

3. **Router mode-selection trigger table.** `adapters/claude/agents/qa-team.md:22-29`.
   Routing lives in the entry skill only (spec v3 §1). Trigger semantics were carried
   into each unit as scope/entry lines, but the selection table itself must re-home
   into the entry recipes (workstream B), or the qa entry coverage silently degrades.

4. **Native runtime config of the team agent.** `adapters/claude/agents/qa-team.md:1-11`
   (`tools:` list, adapter-configured model, `color: red`, `memory: project`, name `품질관리팀`).
   Dropped by design: model literals are guard violations, concrete tool/write config is
   node-owned, and the team identity becomes the `family: qa` label.

5. **Model-role recommendations.** `adapters/claude/agents/qa-team.md:33-38` maps modes
   to fast/deep reviewer with adapter-configured tier defaults. The portable part is
   captured in each unit's `role:` (fast reviewer: code-review, plan-review, test,
   data-curate; deep reviewer: ml-debug, security-review). The adapter defaults resolve
   via per-adapter models.conf and were intentionally not restated.

6. **security-review RE provenance pointer.**
   `adapters/claude/agent-modes/qa/security-review.md:7`: design source = on-prem port of
   the built-in `/security-review`; RE doc at
   `<nas-archive>/claude-meta-spec/reverse_engineering/security-review.md`. Provenance
   only, not behavior — recorded here so the pointer is not lost when agent-modes die.

7. **test Level-5 concrete literals.** `adapters/claude/agent-modes/qa/test.md:26`
   names `run.py` and a 600 s/10-minute session as the integration example, and `:33`
   names the `verifier-*`/`run-*` adapter handle prefixes. The unit generalizes to
   "project-level entry point" / "adapter-provided verifier and run handles" and keeps
   the 60 s and 10-minute timeouts and the ~15-min cold-start timebox. The literal
   handle-name prefixes (`verifier-*`, `run-*`) and `run.py` naming are adapter/project
   vocabulary — confirm the compiled adapter overlays still surface them.

8. **test Level 5b incident provenance dates.**
   `adapters/claude/agent-modes/qa/test.md:36-37` ties candidate-set completeness and
   input-modality fidelity to the 2026-06-12 picker/wheel incidents. The rules are
   carried in the unit; the incident dates/history are recorded here only.

9. **Team-file Common Rules coverage.** `adapters/claude/agents/qa-team.md:42-48`:
   read-only nature → `read_only: true` on every unit; spec-drift check
   (`spec/pipeline_state.yaml` → `spec/prd.md`) → placed in code-review, plan-review
   (the KO/team sources scope it to code-review/plan-review/test; the test unit does
   NOT carry it — its verification targets come from the plan, and figure semantics
   have a dedicated level; flag if reviewers want it restored there); 5–7 finding
   limit → code-review and plan-review bodies; "one mode per invocation" → obsolete
   (one unit per dispatch node by construction); language rule → covered by
   response-policy plus the localization note in `_shared/triage-output.md`.

10. **`_review_rules.md` disposition.** `adapters/claude/agent-modes/qa/_review_rules.md`
    maps fully onto `_shared/stance.md` (adversarial stance incl. the test
    BLOCKED/FAIL-over-PASS clause, carried in the test unit body),
    `_shared/triage-output.md` (severity skeleton), and `_shared/dual-io.md` (one-line
    return). No unplaced remainder.

## Open uncertainties for review

- `worker_type` for ml-debug and data-curate is set to `review`; `support` is arguable
  (they aid dev flow rather than gate a stage). Validator kind↔worker_type mapping
  (workstream B) should confirm.
- `io.verdict` semantic token sets for ml-debug (`diagnosed|inconclusive`) and
  data-curate (`clean|findings`) are newly minted — legacy sources had report shapes
  but no explicit verdict tokens for these two.
- `branches` for ml-debug/data-curate set to `[direct]` only — legacy sources show no
  pipeline (file-path one-line-return) call site; restore `pipeline` if a recipe
  dispatches them as stages.
