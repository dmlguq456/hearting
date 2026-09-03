# Capability: autopilot-research

This is the portable capability contract for `autopilot-research`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `autopilot-research` |
| Group | `entry` |
| Supported modes | `academic, technology, market` |
| Portable meaning | Shared upfront research that surveys academic, technology, or market sources before downstream routing. |
| Argument shape | `<query> [--mode academic\|technology\|market] [--depth shallow\|medium\|deep] [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial] [--no-clarify] [--no-figures] [--from search\|analyze\|report]` |
| Execution topology | `map-reduce`; registry `capabilities/topologies.json` |
| Entry load phase | `post-approval`; owner contract `capabilities/autopilot-research.md` |

## Invocation Semantics

Shared research-survey entrypoint with three modes: academic (papers, trends,
and field mapping), technology (libraries, projects, stacks, and code
baselines), and market (market/competitor/reference-app/UX patterns).
Downstream routing: academic → autopilot-draft for papers/presentations and
autopilot-code for academic baselines; technology → autopilot-code for library
or research implementation and autopilot-spec for stack/reference decisions;
market → autopilot-draft for proposals/reports and autopilot-spec for
reference-app UX. This capability produces field intelligence only; downstream
skills create actual documents, code, or applications.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not.

Research work writes to `$AGENT_ARTIFACT_OUTPUT_DIR/research/<topic>/`.

Required public artifacts:

- `pipeline_state.yaml`: query, mode, depth, intensity, QA override, resume stage, and artifact path;
- `pipeline_summary.md`: source coverage, findings, QA result, and downstream recommendations;
- report chapters at the research root, named by mode;
- `cards/` for paper/project/company/source cards when the mode produces cards;
- `analysis_summary.md` when the analyze stage produces cross-source synthesis.

Internal artifacts belong under `_internal/`, including raw search metadata, source JSON, browser extracts, reference-chaining logs, code search notes, review records, and retry scratch files.

## Artifact Producer Lifecycle

W7C write-cutover contract (`utilities/artifact_producer.py`, registry table
`producer_lifecycle` in `capabilities/topologies.json`). The same lifecycle
binds `direct`, `quick`, and `standard+`; only the acting owner differs.

1. **begin before the first write.** After the route is compiled and bound,
   the owner (the inline session for `direct`, the dispatch-depth-1 owner for
   `quick` and `standard+`) runs `artifact_producer.py begin --artifact-root
   <root> --route <route file> --capability autopilot-research --intensity <intensity>`.
   While the cutover is inactive this returns `legacy-compat` and the legacy
   `<artifact-root>/research/` layout stays writable; once active it
   issues `campaign_id`/`cycle_id`/`producer_id` and the cycle directory
   `campaigns/<camp>/cycles/<cyc>/artifacts/` before any artifact exists.
2. **write only inside the open cycle.** Every durable artifact goes under
   `<cycle_dir>/artifacts/research/...` (`AGENT_ARTIFACT_OUTPUT_DIR`).
   `artifact_producer.py check-write` is the single allow/deny oracle used by
   `hooks/artifact-guard.sh`; an active cutover hard-denies new legacy
   top-level writes, and `shared/` is immutable in both states.
3. **stage workers join, never fork.** `standard+` stage workers receive
   `AGENT_ARTIFACT_CAMPAIGN_ID`/`CYCLE_ID`/`PRODUCER_ID`/`CYCLE_DIR`/`OUTPUT_DIR`
   from the owner (dispatch env pass-through) and call `begin --node <id>`
   on the same route, which resumes the owner's open cycle.
4. **finalize after route closure.** The owner runs `artifact_producer.py
   finalize --artifact-root <root> --cycle <cycle_id>` once the route is
   closed: it enumerates `artifacts/`, builds and validates the D-6 manifest,
   commits `manifest.json` (the commit point), applies the index, and seals
   the cycle record. Empty output leaves no lineage (D-9). `recover` rolls a
   crashed finalize forward or back from its journal.
5. **shared admission.** `research/<topic>` output stays cycle-local. It reaches `shared/research/` only through an explicit promotion: `admit-shared --kind research --promote-research --promotion-evidence <artifacts path>`; the whole `research/` bucket is never treated as shared by default.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

Minimum role mapping:

- source search and retrieval: research/material role;
- analysis and synthesis: research role;
- fact/citation verification: QA or research-review role;
- editorial cleanup of final chapters: editorial role when available;
- downstream handoff: planning role for spec/code/draft routing.

Pipeline intensity follows `core/CONVENTIONS.md §1`: `direct` has no plan stage or durable plan artifact; `quick` is one registered-headless dispatch-depth-1 one-shot conductor with its inline micro-plan plus plan-check-lite; `standard+` uses the capability's durable work-cycle plan when applicable. `plan-check` is required for every non-`direct` graph, but independent QA is not repeated after every stage by default. Verification rigor for plan-check, selected independent reviews, and final verify is derived from intensity; it does not name a model or introduce a separate stage graph.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- enforce git/worktree safety before edits;
- enforce artifact ordering before new durable artifacts;
- enforce spec-read gating when this capability changes spec-backed code or specs;
- use DB memory paths, not runtime-native memory files.

Additional research-entry gates:

- before creating either public or `_internal` research output, compile and bind the selected `autopilot-research` route; `direct` is a compiled inline node, while `quick` and `standard+` retain their registered-headless owner topology;
- treat native-subagent availability or prohibition as surface-local evidence only. At `standard+`, it never authorizes main-session execution or bypasses the registered-headless owner;
- if checked evidence reports `failure_scope=exact-worktree` with `retry_on_isolated_worktree=1`, re-isolate and re-probe or stop without writing research artifacts; a non-Git workspace does not turn the failure into inline authority;
- realize search/analyze/report breadth only from the compiled recipe and its sealed `parallel_group` members. Never invent topical axes, reviewers, or native helpers after a route failure;
- the `retrieval` and `claim-verify` groups may widen to a closed auxiliary leg at higher intensity: `retrieval` adds an `assumption-check` and `claim-verify` adds an `edge-case-check`. Auxiliary legs are advisory (non-blocking `findings`/`none`, `light` budget) and feed the arbiter's `auxiliary_findings_considered`; they never hold the stage gate alone, and at least one realized `peer` leg still carries the quality-peer gate authority. `retrieval`'s arbiter is the downstream `synthesis` node, which records the key in its own completion evidence. Nothing in the runtime tells that node it holds the role, so the owner must: **a node arbiter's dispatch prompt names the group it arbitrates and how many auxiliary legs were realized.** A worker that is never told writes a keyless artifact and is refused at its own completion gate;
- ask one scope-clarification round when the query is too broad, too short, or matches multiple modes, unless `--no-clarify` or resume mode is active;
- keep raw source metadata in `_internal/`; public reports should cite or summarize, not expose noisy scrape output;
- stop with a failed `pipeline_summary.md` when search returns no useful sources;
- for `standard` and above, verify card-level facts such as title, venue, year, citation, metric, and quoted claims against sources;
- for `adversarial`, run an independent contradiction/claim check before finalizing public-facing reports;
- do not create code, specs, apps, or prose deliverables directly; hand off to downstream capabilities after field intelligence is complete.

## Portable Procedure

1. Parse query, mode, depth, intensity, QA override, optional `--from`, and skip flags.
2. Compile and bind the selected route, then resolve or create `$AGENT_ARTIFACT_OUTPUT_DIR/research/<topic>/`; if resuming, read `pipeline_state.yaml`.
3. Infer mode when omitted and ask scope clarification when required.
4. Build search queries, including 2-3 synonym or alternate-phrase expansions.
5. Search mode-appropriate sources and write raw metadata under `_internal/`.
6. Analyze results into cards, chaining/code/source summaries, and `analysis_summary.md` as applicable.
7. Generate mode-specific report chapters.
8. Run QA verification according to level.
9. Update `pipeline_state.yaml` after each completed stage and finish with `pipeline_summary.md`.

## Mode-Specific Semantics

| Mode | Search/source emphasis | Public report set |
|---|---|---|
| `academic` | Papers, citation graphs, datasets, baselines, implementations, model resources. | briefing, landscape, core papers, baselines, technical deep dive, datasets, implementation, resources, reading guide. |
| `technology` | Standards, vendor docs, technical whitepapers, OSS implementations, deployment constraints. | briefing, landscape, standards/specs, vendor comparison, technical deep dive, deployment, implementation, resources. |
| `market` | Analyst/news/company/investor sources, product positioning, adoption and business signals. | briefing, market overview, key players, trends, opportunities. |

Mode inference should report its basis. If multiple modes match, resolve via clarification unless the user explicitly supplied `--mode`.

## Downstream Handoff

Field intelligence ends with recommendations for downstream work:

- `academic`: hand off to `autopilot-draft` for papers/presentations or `autopilot-code` for baseline implementation;
- `technology`: hand off to `autopilot-spec` for stack/reference decisions or `autopilot-code` for implementation on a selected baseline;
- `market`: hand off to `autopilot-draft` for business/report writing or `autopilot-spec` for reference-app/UX decisions.

After claim verification closes the research result, evaluate the route-sealed
optional artifact-sink extension. When available, offer the report artifact
through the app-neutral receipt contract; unavailable state records
`skipped/extension-unavailable` without invalidating the research artifact.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/autopilot-research/SKILL.md` and `skills/autopilot-research/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-research/SKILL.md`, while `skills/autopilot-research/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info autopilot-research`. Use `adapters/codex/skills/autopilot-research/SKILL.md` as the native Codex Skill projection; do not consume `skills/autopilot-research/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info autopilot-research`. Use `adapters/opencode/skills/autopilot-research/SKILL.md` and `adapters/opencode/commands/autopilot-research.md` as native OpenCode projections; do not consume `skills/autopilot-research/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/autopilot-research/SKILL.md` and `adapters/claude/skills/autopilot-research/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-research/SKILL.md`, while `skills/autopilot-research/SKILL.md` remains the compatibility reference kept for parity/drift checks.
