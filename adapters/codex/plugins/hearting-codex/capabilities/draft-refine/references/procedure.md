# draft-refine

Follow the three-tier output convention (`<agent-home>/core/CONVENTIONS.md#5-skill-output-convention--t1t2t3`). Store review logs in `_internal/strategy_reviews/` or `_internal/draft_reviews/`. Use `_internal/versions/v{N}/` for modern snapshots and recognize legacy sibling `_v{N}.md` snapshots when already present.

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

Version snapshots are managed by `utilities/artifact-snapshot.py` for the
active refinement. Existing version receipts remain the source of snapshot paths.

Pass the helper-reported version, convention, snapshot paths, canonical path,
and existing companion paths to the `research/research-survey` unit. The helper
preserves a detected legacy sibling layout and uses
`_internal/versions/v{N}/<relative-path>` for modern/new layouts. A new target
has no prior state to snapshot.

## Delegate Refinement

Load [references/delegate-prompt.md](delegate-prompt.md), substitute its variables, and dispatch the `research/research-survey` unit with the complete prompt. Read [references/changelog-example.md](changelog-example.md) for the legacy-to-frontmatter example.

Check these invariants:

- The file begins with `---`.
- `changelog:` is a newest-first YAML array in frontmatter, using block scalar `|` entries rather than top-of-file HTML comments.
- Every memo is re-grounded against its source before application or override.
- An override records its reason in the changelog.

## Assurance

Use the selected intensity and compiled stage graph for verification rigor,
reviewer roles, and retry behavior. This procedure does not select reviewers
from section count or add review passes.

## Review handoff

The selected review node owns review dispatch and returns findings through the
existing pipeline handoff. The owner/runtime controls retry lineage and
completion.


## Task

Refine the document at: $ARGUMENTS
