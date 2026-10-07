## Role-to-Profile Lookup

Each role runs the relevant `mem profile <stem>` commands at work start. The DB record is the source of truth; this agent-centered view mirrors the aspect-centered canonical matrix in [`MEMORY.md §7.6`](../../../core/MEMORY.md). If they drift, MEMORY wins.

| Unit family | Profiles | Purpose |
|---|---|---|
| `material/*` units | `01_paper_figure_style`, `03_presentation_strategy`, `04_analysis_methodology`, `05_domain_expertise` | Figures, slides, data analysis, captions, and domain abbreviations |
| `design/*` units | `01_paper_figure_style`, `03_presentation_strategy`, `05_domain_expertise` | UI mockups, slide visuals, diagrams, and domain terms |
| `research/*` units | `01_paper_figure_style`, `02_paper_writing_style`, `04_analysis_methodology`, `05_domain_expertise` | Figure citation, prose, validation method, and terminology |
| `editorial/*` units | `01_paper_figure_style`, `02_paper_writing_style`, `03_presentation_strategy`, `04_analysis_methodology`, `05_domain_expertise` | User-facing captions, papers, presentations, analytical prose, and terminology |
| `plan/*` units | `02_paper_writing_style`, `04_analysis_methodology`, `05_domain_expertise`, `07_coding_convention` | Plan tone, verification patterns, domain terms, and code conventions |
| `dev/*` units | `04_analysis_methodology`, `05_domain_expertise`, `07_coding_convention` | Metrics, verification, identifier terminology, structure, config, prefixes, and layers; project-local `experiment_conventions.md` wins |
| main agent | `04_analysis_methodology`, `05_domain_expertise`, `07_coding_convention` | Analytical replies, user terminology, and code defaults for lab, spec, and code pipelines |

Aspect 06, conversational meta rules, is main-agent-only because subagents do not speak directly to the user. Its profile record remains the default `mem profile-append` collaboration target. Aspect 07 applies only to implementation, planning, and main-agent code work. Each role normally reads three to five relevant profiles. These lookups are encoded in the unit definitions.

## Relationship to Project Memory

| | Project memory | User profile |
|---|---|---|
| Store | Unified DB working or durable project scope | DB records with `type=profile`, global scope |
| Scope | Per project | Cross-project user defaults |
| Accumulation | Agent- or user-directed as context warrants | Explicit `/analyze-user` or `mem profile-append` |
| Shape | Short feedback, preferences, facts, and handoffs | Structured pattern catalog |
| Update cadence | As needed | Profile cycles |
| QA | Raw or workflow-specific | Multi-reviewer refined |

Project memory holds contextual project knowledge. Profile records hold verified cross-project patterns for figure, writing, presentation, analysis, domain, and coding behavior. Semantic storage and retrieval remain agent judgments rather than fixed phrase rules.

## Examples

```text
/analyze-user figure --source ~/docs/presentation/
```

Analyze the figure aspect and include the presentation folder.

```text
/analyze-user all --mode init
```

Reinitialize all aspects after initial setup or a long gap.

```text
/analyze-user --from qa --user-refine
```

Resume at QA and pause once before Phase 5 for user memos.

```text
/analyze-user coding_convention --source ~/path/to/NN_Zoo --source ~/path/to/other_repo
```

Analyze coding conventions from user-supplied repositories. Recursively discover model, train, config, and notebook patterns; never hardcode a source path.

## Suggested Update Cadence

- Initial setup: `/analyze-user all --mode init` after enough representative material exists, such as five or more papers.
- After a new paper, presentation, or report: update only the relevant aspect.
- After completing a new model or repository: `/analyze-user coding_convention --source <new-repo>`.
- After six months or more without updates: revalidate with `/analyze-user all`.

Every invocation uses four adversarial reviewers, so control cost through invocation frequency rather than a weaker QA mode.
