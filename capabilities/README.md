# Portable Capability Catalog

This directory is the runtime-neutral capability layer. It describes what each
capability means, what artifacts it owns, and which portable roles it may use.
It is not a Claude Skill registry. Per-capability contracts live in
`capabilities/<capability>.md`; this README is the catalog index.

Claude, Codex, and OpenCode realize this catalog as sibling adapter outputs;
none is the source or completion proxy for another. Claude owns concrete Skill
files under `adapters/claude/skills/`, Codex owns generated native Skill/plugin
projections, and OpenCode owns generated native Skill/command projections.
Historical `skills/` files remain Claude compatibility references only.

## Capability Contract

Each capability has:

- an identifier;
- a capability group;
- supported modes;
- invocation semantics in runtime-neutral terms;
- artifact ownership;
- required role families;
- adapter realization notes.

For each manifest `entry-router`, discovery and execution are separate
surfaces. The compact pre-approval router carries routing metadata; after
approval, the selected portable capability contract and its one owner
reference govern execution. Assigned stage workers read only their stage
contracts. Parent-invoked and model-support capabilities never enter the
primary-router candidate set.

Autopilot entry capabilities additionally follow `core/CONVENTIONS.md §1`:

- `intensity` is the primary stage-graph selector;
- `direct` has no plan stage, no plan-check, and no durable plan artifact;
- `quick+` has at least a small `plan-check` gate;
- stage-local gates stay cheap and only check next-stage readiness;
- independent QA is not repeated after every stage by default;
- final `verify` remains capability-specific and concrete;
- Verification rigor for `plan-check`, selected independent reviews, and `verify` is derived from intensity; there is no separate user-facing `--qa` axis.

Runtime-specific details stay out of portable capability meaning:

- Claude Skill frontmatter and folder layout;
- slash command names;
- Claude hook names, `ScheduleWakeup`, statusline, or MCP registration details;
- concrete model names;
- CLI-specific external reviewer commands.

## Catalog
<!-- GENERATED: harness-manifest.json -->

| Capability | Group | Modes | Portable spec | Portable meaning | Claude realization | Codex realization | OpenCode realization |
|---|---|---|---|---|---|---|---|
| `analyze-project` | pre | code, paper, doc | [`analyze-project.md`](analyze-project.md) | Creates persistent analysis of existing code, papers, or documents; initial analysis defaults here unless read-only/no-file or another primary applies. | `adapters/claude/skills/analyze-project/SKILL.md` | `adapters/codex/skills/analyze-project/SKILL.md` | `adapters/opencode/skills/analyze-project/SKILL.md`; `adapters/opencode/commands/analyze-project.md` |
| `analyze-user` | pre | init, update | [`analyze-user.md`](analyze-user.md) | Create or update a cross-project user-preference profile from coding, writing, and analysis patterns. | `adapters/claude/skills/analyze-user/SKILL.md` | `adapters/codex/skills/analyze-user/SKILL.md` | `adapters/opencode/skills/analyze-user/SKILL.md`; `adapters/opencode/commands/analyze-user.md` |
| `artifact-projection-read` | support | - | [`artifact-projection-read.md`](artifact-projection-read.md) | Read-only lookup of Cairn artifact projections through the canonical W3a contract. | `adapters/claude/skills/artifact-projection-read/SKILL.md` | `adapters/codex/skills/artifact-projection-read/SKILL.md` | `adapters/opencode/skills/artifact-projection-read/SKILL.md`; `adapters/opencode/commands/artifact-projection-read.md` |
| `audit` | ops | - | [`audit.md`](audit.md) | Read-oriented post-run inspection for artifact drift, inconsistency, and omissions. | `adapters/claude/skills/audit/SKILL.md` | `adapters/codex/skills/audit/SKILL.md` | `adapters/opencode/skills/audit/SKILL.md`; `adapters/opencode/commands/audit.md` |
| `autopilot-apply` | entry | - | [`autopilot-apply.md`](autopilot-apply.md) | Apply a cheatsheet draft to the real source artifact and verify the result. | `adapters/claude/skills/autopilot-apply/SKILL.md` | `adapters/codex/skills/autopilot-apply/SKILL.md` | `adapters/opencode/skills/autopilot-apply/SKILL.md`; `adapters/opencode/commands/autopilot-apply.md` |
| `autopilot-code` | entry | dev, debug, audit | [`autopilot-code.md`](autopilot-code.md) | Code-work entrypoint that detects spec context and closes the plan→execute→test→report loop. | `adapters/claude/skills/autopilot-code/SKILL.md` | `adapters/codex/skills/autopilot-code/SKILL.md` | `adapters/opencode/skills/autopilot-code/SKILL.md`; `adapters/opencode/commands/autopilot-code.md` |
| `autopilot-design` | entry | - | [`autopilot-design.md`](autopilot-design.md) | Visual-design pipeline coordinating references→tokens→components→review→handoff. | `adapters/claude/skills/autopilot-design/SKILL.md` | `adapters/codex/skills/autopilot-design/SKILL.md` | `adapters/opencode/skills/autopilot-design/SKILL.md`; `adapters/opencode/commands/autopilot-design.md` |
| `autopilot-draft` | entry | paper, presentation, doc | [`autopilot-draft.md`](autopilot-draft.md) | Document-drafting pipeline that produces an applicable artifact through strategy, drafting, verification, and editing. | `adapters/claude/skills/autopilot-draft/SKILL.md` | `adapters/codex/skills/autopilot-draft/SKILL.md` | `adapters/opencode/skills/autopilot-draft/SKILL.md`; `adapters/opencode/commands/autopilot-draft.md` |
| `autopilot-lab` | entry | setup, eval | [`autopilot-lab.md`](autopilot-lab.md) | Rapid experiment prototyping around training setup and checkpoint evaluation/analysis. | `adapters/claude/skills/autopilot-lab/SKILL.md` | `adapters/codex/skills/autopilot-lab/SKILL.md` | `adapters/opencode/skills/autopilot-lab/SKILL.md`; `adapters/opencode/commands/autopilot-lab.md` |
| `autopilot-refine` | entry | - | [`autopilot-refine.md`](autopilot-refine.md) | Correct and update existing document/research artifacts while preserving snapshots and change history. | `adapters/claude/skills/autopilot-refine/SKILL.md` | `adapters/codex/skills/autopilot-refine/SKILL.md` | `adapters/opencode/skills/autopilot-refine/SKILL.md`; `adapters/opencode/commands/autopilot-refine.md` |
| `autopilot-research` | entry | academic, technology, market | [`autopilot-research.md`](autopilot-research.md) | Shared upfront research that surveys academic, technology, or market sources before downstream routing. | `adapters/claude/skills/autopilot-research/SKILL.md` | `adapters/codex/skills/autopilot-research/SKILL.md` | `adapters/opencode/skills/autopilot-research/SKILL.md`; `adapters/opencode/commands/autopilot-research.md` |
| `autopilot-ship` | entry | - | [`autopilot-ship.md`](autopilot-ship.md) | Prepare application deployment/release setup and a ship checklist. | `adapters/claude/skills/autopilot-ship/SKILL.md` | `adapters/codex/skills/autopilot-ship/SKILL.md` | `adapters/opencode/skills/autopilot-ship/SKILL.md`; `adapters/opencode/commands/autopilot-ship.md` |
| `autopilot-spec` | entry | app, library, api, cli, research, update | [`autopilot-spec.md`](autopilot-spec.md) | Create or update requirements/blueprints while keeping `prd.md` as the only spec-change path. | `adapters/claude/skills/autopilot-spec/SKILL.md` | `adapters/codex/skills/autopilot-spec/SKILL.md` | `adapters/opencode/skills/autopilot-spec/SKILL.md`; `adapters/opencode/commands/autopilot-spec.md` |
| `code-execute` | sub | - | [`code-execute.md`](code-execute.md) | Execute a plan step by step, delegate implementation to the development role, and record an execution log. | `adapters/claude/skills/code-execute/SKILL.md` | `adapters/codex/skills/code-execute/SKILL.md` | `adapters/opencode/skills/code-execute/SKILL.md`; `adapters/opencode/commands/code-execute.md` |
| `code-plan` | sub | - | [`code-plan.md`](code-plan.md) | Analyze code, write a detailed implementation plan, and run the plan-check gate at the rigor derived from intensity. | `adapters/claude/skills/code-plan/SKILL.md` | `adapters/codex/skills/code-plan/SKILL.md` | `adapters/opencode/skills/code-plan/SKILL.md`; `adapters/opencode/commands/code-plan.md` |
| `code-refine` | sub | - | [`code-refine.md`](code-refine.md) | Revise an existing plan using user notes, plan-check feedback, and verification-failure notes. | `adapters/claude/skills/code-refine/SKILL.md` | `adapters/codex/skills/code-refine/SKILL.md` | `adapters/opencode/skills/code-refine/SKILL.md`; `adapters/opencode/commands/code-refine.md` |
| `code-report` | sub | - | [`code-report.md`](code-report.md) | Assemble code-cycle results into a user-facing report. | `adapters/claude/skills/code-report/SKILL.md` | `adapters/codex/skills/code-report/SKILL.md` | `adapters/opencode/skills/code-report/SKILL.md`; `adapters/opencode/commands/code-report.md` |
| `code-test` | sub | - | [`code-test.md`](code-test.md) | Verify implementation results in stages and record evidence. | `adapters/claude/skills/code-test/SKILL.md` | `adapters/codex/skills/code-test/SKILL.md` | `adapters/opencode/skills/code-test/SKILL.md`; `adapters/opencode/commands/code-test.md` |
| `design-components` | sub | - | [`design-components.md`](design-components.md) | Build UI components/mockups and preview artifacts. | `adapters/claude/skills/design-components/SKILL.md` | `adapters/codex/skills/design-components/SKILL.md` | `adapters/opencode/skills/design-components/SKILL.md`; `adapters/opencode/commands/design-components.md` |
| `design-handoff` | sub | - | [`design-handoff.md`](design-handoff.md) | Package design results as assets and specifications for development handoff. | `adapters/claude/skills/design-handoff/SKILL.md` | `adapters/codex/skills/design-handoff/SKILL.md` | `adapters/opencode/skills/design-handoff/SKILL.md`; `adapters/opencode/commands/design-handoff.md` |
| `design-init` | sub | - | [`design-init.md`](design-init.md) | Bootstrap the design environment and state. | `adapters/claude/skills/design-init/SKILL.md` | `adapters/codex/skills/design-init/SKILL.md` | `adapters/opencode/skills/design-init/SKILL.md`; `adapters/opencode/commands/design-init.md` |
| `design-refs` | sub | - | [`design-refs.md`](design-refs.md) | Collect external and user-provided visual references and create a brief. | `adapters/claude/skills/design-refs/SKILL.md` | `adapters/codex/skills/design-refs/SKILL.md` | `adapters/opencode/skills/design-refs/SKILL.md`; `adapters/opencode/commands/design-refs.md` |
| `design-review` | sub | - | [`design-review.md`](design-review.md) | Review design output for quality, token-contract compliance, and breakage. | `adapters/claude/skills/design-review/SKILL.md` | `adapters/codex/skills/design-review/SKILL.md` | `adapters/opencode/skills/design-review/SKILL.md`; `adapters/opencode/commands/design-review.md` |
| `design-tokens` | sub | - | [`design-tokens.md`](design-tokens.md) | Define design tokens such as color, typography, and spacing. | `adapters/claude/skills/design-tokens/SKILL.md` | `adapters/codex/skills/design-tokens/SKILL.md` | `adapters/opencode/skills/design-tokens/SKILL.md`; `adapters/opencode/commands/design-tokens.md` |
| `draft-refine` | sub | - | [`draft-refine.md`](draft-refine.md) | Refine a draft by applying memo/review feedback to a document strategy or draft. | `adapters/claude/skills/draft-refine/SKILL.md` | `adapters/codex/skills/draft-refine/SKILL.md` | `adapters/opencode/skills/draft-refine/SKILL.md`; `adapters/opencode/commands/draft-refine.md` |
| `draft-strategy` | sub | rebuttal, paper, review, report, proposal, presentation | [`draft-strategy.md`](draft-strategy.md) | Create an initial document strategy and evidence-based writing plan. | `adapters/claude/skills/draft-strategy/SKILL.md` | `adapters/codex/skills/draft-strategy/SKILL.md` | `adapters/opencode/skills/draft-strategy/SKILL.md`; `adapters/opencode/commands/draft-strategy.md` |
| `post-it` | ops | - | [`post-it.md`](post-it.md) | Store project/cross-project notes and handoffs in working memory. | `adapters/claude/skills/post-it/SKILL.md` | `adapters/codex/skills/post-it/SKILL.md` | `adapters/opencode/skills/post-it/SKILL.md`; `adapters/opencode/commands/post-it.md` |

## Adapter Requirements

An adapter that supports capabilities must document:

- how a user invokes the capability;
- whether confirmation is automatic, required, or unsupported;
- how the adapter discovers artifact roots;
- how it loads the portable roles in `roles/`;
- which deterministic guards it can enforce;
- where durable output is written;
- how unsupported sub-capabilities are reported.

If an adapter cannot support a capability, it must say so explicitly and offer a
fallback path instead of silently treating a Claude Skill file as native.
