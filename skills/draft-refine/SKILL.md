---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: draft-refine
description: "Use only when autopilot-draft or autopilot-refine dispatches an internal strategy or draft refinement stage. Not for top-level user requests or primary capability routing."
argument-hint: "<strategy or draft name or path> [--intensity direct|quick|standard|strong|thorough|adversarial]"
metadata:
  group: sub
  fam: sub
  invocation_class: parent-invoked
  modes: []
  blurb: "Refine a draft by applying memo/review feedback to a document strategy or draft."
  use_when: "Use only when autopilot-draft or autopilot-refine dispatches an internal strategy or draft refinement stage."
  not_for: "Not for top-level user requests or primary capability routing."
---

# draft-refine

Follow the [three-tier output convention](../../core/CONVENTIONS.md#5-skill-output-convention--t1t2t3). Store review logs in `_internal/strategy_reviews/` or `_internal/draft_reviews/`. Use `_internal/versions/v{N}/` for modern snapshots and recognize legacy sibling `_v{N}.md` snapshots when already present.

## Resolve the Document

Resolve `$ARGUMENTS` to a canonical strategy or draft and any existing required audience-language companion.

- A path containing `/draft/` selects draft mode and canonical `draft.md`.
- A path containing `/strategy/` selects strategy mode and canonical `strategy.md`.
- A directory defaults to `strategy/strategy.md`.
- A Markdown file is used as supplied; recognize a sibling `draft_ko.md` or `strategy_ko.md` only when it exists or the workflow explicitly requires that legacy companion.
- Otherwise fuzzy-search with `ls -d $AGENT_ARTIFACT_OUTPUT_DIR/documents/*$ARGUMENTS* 2>/dev/null`.
  - one match → resolve its canonical strategy and existing companions
  - multiple matches → ask the user to choose
  - no match → report the resolution error

Do not generate a language-companion pair by default. Preserve the target artifact's existing or explicitly requested language and follow `<agent-home>/roles/response-policy.md` for user-facing output.

## Prepare Versioning

Before dispatching the `research/research-survey` unit, prepare every existing
canonical or companion file that the refinement will change through the active
route's write preflight. `artifact-guard.sh` calls
`utilities/artifact-snapshot.py prepare`, preserves exact pre-change bytes, and
reuses one version receipt across the artifact. If hook coverage is unavailable,
invoke the helper explicitly with `--artifact-root`, `--target`, `--route`,
`--route-id`, and `--node` before editing and require exit 0. Never scan version
numbers or use raw `mkdir`/`cp` as the snapshot mechanism.

Pass the helper-reported version, convention, snapshot paths, canonical path,
and existing companion paths to the `research/research-survey` unit. The helper
preserves a detected legacy sibling layout and uses
`_internal/versions/v{N}/<relative-path>` for modern/new layouts. A new target
has no prior state to snapshot.

## Delegate Refinement

Load [references/delegate-prompt.md](references/delegate-prompt.md), substitute its variables, and dispatch the `research/research-survey` unit with the complete prompt. Read [references/changelog-example.md](references/changelog-example.md) for the legacy-to-frontmatter example.

Check these invariants:

- The file begins with `---`.
- `changelog:` is a newest-first YAML array in frontmatter, using block scalar `|` entries rather than top-of-file HTML comments.
- Every memo is re-grounded against its source before application or override.
- An override records its reason in the changelog.

## QA Scaling

Derive rigor from the selected intensity and changed sections. At Standard+, run quality and fact review in parallel.

| Level | Condition | Quality review | Fact check | Maximum rounds |
|---|---|---|---|---|
| **Quick** | Direct quick invocation; the parent autopilot normally skips refinement | One fast spot-check | Skip | 1, no reinvocation on critical findings |
| **Light** | At most 3 sections | One fast reviewer | Skip; reviewer covers basic spot-checks | 2 |
| **Standard** | At least 4 sections | One deep reviewer | One fast fact-checker | 2 |
| **Thorough** | Major overhaul or new evidence | Two deep reviewers in parallel | One fast fact-checker | 2 |
| **Adversarial** | Imminent external review or explicit adversarial intensity | Two deep reviewers plus one external adversary | One fast fact-checker | 2 plus one external pass |

Use a fast fact-checker because source comparison is bounded matching, not creative drafting. The canonical 8-row classification table lives in `roles/units/research/fact-check.md`.

## Post-Refine Review

After the `research/research-survey` unit returns:

1. Set `{log_dir}` to the document artifact root and create the selected review directory:

   ```bash
   mkdir -p {log_dir}/_internal/strategy_reviews
   mkdir -p {log_dir}/_internal/draft_reviews
   ```

2. Invoke only the reviewers selected by the QA budget.

Quality reviewer prompt:

```text
Review changed sections for quality, cohesion, audience fit, and strategy alignment.
Document type and path: [type/path]
Changed sections: [list]
For rebuttals, verify that every reviewer point remains addressed.
Do not verify individual venue, year, metric, lineage, or classification facts; the fact-checker owns those.
Write: {log_dir}/{review_subdir}/refine_round_{N}_quality.md
Return ONLY the path and one-line verdict.
```

Fact-checker prompt for Standard or higher:

```text
Act as a fact-checker, not a narrative reviewer.
Document type and path: [type/path]
Changed sections: [list]

For every material model, venue, year, metric, lineage, or classification claim, open and compare the ground-truth source:
- $AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/paper/*.md is the primary source produced by `analyze-project --mode paper`
- open original PDFs only when the paper analysis lacks the fact
- use {artifact_root}/strategy/ or analysis/ for strategy-specific evidence

Output one table only:
| Slide/Section | Claim in deliverable | Source (file:line or section) | Match (✅/❌) | Severity (🔴/🟡) |

Do not discuss writing quality or audience fit. Limit the table to about 30 material claims when more than 10 sections changed.
Write: {log_dir}/{review_subdir}/refine_round_{N}_factcheck.md
Return ONLY the path and one-line verdict.
```

3. Branch on the combined verdict.
   - No critical finding → report both selected verdicts.
   - Quick → stop after round 1, add remaining critical findings to the artifact's localized Unresolved Issues section, and report them.
   - Quality critical → re-dispatch the `research/research-survey` unit with quality findings.
   - Fact critical → re-dispatch the `research/research-survey` unit with mandatory source re-grounding against the named analyses or PDFs.
   - Both → combine findings in one bounded correction.
4. After two rounds, write remaining issues to the localized Unresolved Issues section, report resolved and unresolved items with reasons, and tag fact residuals `[FACT-RESIDUAL]`.

## Task

Refine the document at: $ARGUMENTS
