# Portable Role Profiles

This directory describes runtime-neutral delegation roles. It is not a native
agent registry for any one tool.

Claude Code realizes these profiles as native Agent files under
`adapters/claude/agents/`. Codex and OpenCode read this directory for role
meaning, then map each role to their own model, tool, and delegation mechanism
through adapter-owned wrappers or native agent surfaces.

## Role Catalog
<!-- GENERATED: harness-manifest.json -->

| Family | Unit | Portable model role | Worker type | Floor | Catalog source |
|---|---|---|---|---|---|
| `design` | `design/critic` | `fast reviewer` | `review` | `high` | `roles/units/design/critic.md` |
| `design` | `design/maker` | `deep maker` | `stage` | `highest` | `roles/units/design/maker.md` |
| `design` | `design/verifier` | `fast reviewer` | `review` | `near-zero` | `roles/units/design/verifier.md` |
| `dev` | `dev/backend` | `fast implementer` | `stage` | `low` | `roles/units/dev/backend.md` |
| `dev` | `dev/frontend` | `fast implementer` | `stage` | `low` | `roles/units/dev/frontend.md` |
| `dev` | `dev/new-lib` | `fast implementer` | `stage` | `low` | `roles/units/dev/new-lib.md` |
| `dev` | `dev/refactor` | `fast implementer` | `stage` | `low` | `roles/units/dev/refactor.md` |
| `editorial` | `editorial/polish` | `deep editor` | `stage` | `low` | `roles/units/editorial/polish.md` |
| `editorial` | `editorial/report` | `fast writer` | `stage` | `low` | `roles/units/editorial/report.md` |
| `editorial` | `editorial/review` | `fast reviewer` | `review` | `low` | `roles/units/editorial/review.md` |
| `editorial` | `editorial/translate` | `deep editor` | `stage` | `low` | `roles/units/editorial/translate.md` |
| `material` | `material/browser-fetch` | `fast tool worker` | `support` | `near-zero` | `roles/units/material/browser-fetch.md` |
| `material` | `material/data-script` | `deep maker` | `stage` | `low` | `roles/units/material/data-script.md` |
| `material` | `material/figure-gen` | `deep maker` | `stage` | `low` | `roles/units/material/figure-gen.md` |
| `material` | `material/pdf-extract` | `fast tool worker` | `support` | `near-zero` | `roles/units/material/pdf-extract.md` |
| `material` | `material/web-image-search` | `fast tool worker` | `support` | `near-zero` | `roles/units/material/web-image-search.md` |
| `plan` | `plan/frame` | `deep maker` | `stage` | `highest` | `roles/units/plan/frame.md` |
| `plan` | `plan/plan-author` | `deep maker` | `stage` | `highest` | `roles/units/plan/plan-author.md` |
| `qa` | `qa/assumption-check` | `fast reviewer` | `review` | `moderate` | `roles/units/qa/assumption-check.md` |
| `qa` | `qa/code-review` | `fast reviewer` | `review` | `moderate` | `roles/units/qa/code-review.md` |
| `qa` | `qa/data-curate` | `fast reviewer` | `review` | `low` | `roles/units/qa/data-curate.md` |
| `qa` | `qa/edge-case-check` | `fast reviewer` | `review` | `moderate` | `roles/units/qa/edge-case-check.md` |
| `qa` | `qa/failure-mode-check` | `fast reviewer` | `review` | `moderate` | `roles/units/qa/failure-mode-check.md` |
| `qa` | `qa/ml-debug` | `deep reviewer` | `review` | `high` | `roles/units/qa/ml-debug.md` |
| `qa` | `qa/plan-review` | `fast reviewer` | `review` | `moderate` | `roles/units/qa/plan-review.md` |
| `qa` | `qa/security-review` | `deep reviewer` | `review` | `moderate` | `roles/units/qa/security-review.md` |
| `qa` | `qa/simplicity-check` | `fast reviewer` | `review` | `moderate` | `roles/units/qa/simplicity-check.md` |
| `qa` | `qa/test` | `fast reviewer` | `review` | `near-zero` | `roles/units/qa/test.md` |
| `qa` | `qa/test-gap-check` | `fast reviewer` | `review` | `moderate` | `roles/units/qa/test-gap-check.md` |
| `research` | `research/claim-verify` | `fast fact-checker` | `review` | `high` | `roles/units/research/claim-verify.md` |
| `research` | `research/fact-check` | `fast fact-checker` | `review` | `near-zero` | `roles/units/research/fact-check.md` |
| `research` | `research/plan-review` | `deep reviewer` | `review` | `highest` | `roles/units/research/plan-review.md` |
| `research` | `research/research-survey` | `deep maker` | `stage` | `high` | `roles/units/research/research-survey.md` |

## Adapter Requirements

An adapter that supports role delegation must document:

- how a role is invoked;
- what tools are available to that role;
- how mode personas under `roles/modes/` are loaded or approximated;
- how portable behavior roles and route-sealed execution profiles map independently to concrete model/effort or variant settings through the single adapter config source (`adapters/<adapter>/config/models.conf`), including reduced-granularity reporting and the substantive-`mini` deny;
- where role output is written when a skill requires durable review logs;
- what happens when a role is unavailable.

Concrete model names do not belong in this directory. Use the portable model
roles and execution profiles from `core/CONVENTIONS.md` and let adapter config
define concrete mapping.

## Behavior Contract (separate from the role catalog)

Beyond the delegation-role catalog above, this directory also holds the
portable **response behavior contract** for the main agent's own responses:

- `response-policy.md` — the runtime-neutral minimum behavior contract
  (concise output, promise–action match, verify-before-assert, convention
  adherence, pause/autonomy, follow-through). The three adapter bootstraps
  reference it as the single source for these portable clauses and add their
  own locale-specific voice (tone, sentence endings) on top. `core/DESIGN_PRINCIPLES.md`
  points here for the portable layer; the adapter bootstrap owns the runtime
  specialization.

This is a behavior contract, not an agent role — it does not appear in the
Role Catalog table and has no per-adapter native agent file.
