# plan family — authoring residue (for review)

Single-source re-home: the plan family has NO `roles/modes/plan/` EN mode files and NO
`adapters/claude/agent-modes/plan/` KO copies (the team agent declared `modes: []`, so
the agent body WAS the persona). The only mined source is
`adapters/claude/agents/plan-team.md`; there are therefore no EN/KO semantic
divergences to reconcile. Residue items:

1. **Native frontmatter dropped** (`adapters/claude/agents/plan-team.md:1-11`):
   - Adapter-configured deep-tier model — model literal omitted because it is forbidden in units. The unit carries only the
     portable role name `deep maker`; concrete model resolves via per-adapter
     models.conf. Reviewers should confirm `deep maker` maps to the tier previously
     configured for this stage.
   - `tools: Glob, Grep, Read, Write, Edit` — a concrete tool grant is node/surface
     owned, not unit-owned. Recorded here so workstream B can carry the grant onto
     the topology node for this unit.
   - `color: blue`, `name: 기획팀` — Claude-native presentation metadata; the team
     name survives only as `family: plan`.
   - `memory: project` — Claude-native agent-memory scope binding. The unit body now
     references `_shared/memory-flow.md`; the project-scope binding itself is a
     surface/runtime concern and must be re-realized by the dispatch surface if agent
     memory for this unit is to persist.

2. **Caller restriction moved to routing** (`adapters/claude/agents/plan-team.md:3`):
   the description's contract "Called from code-plan and code-refine skills — not
   directly by the user" is a routing/topology constraint, not persona text. It is
   summarized in the unit body ("dispatched, never user-invoked directly") but the
   enforceable restriction belongs to the entry/topology layer (workstream B).

3. **Adapter-flavored path in Language Rule** (`adapters/claude/agents/plan-team.md:16-19`):
   the unit body keeps the pointer
   `<agent-home>/capabilities/autopilot-code/references/arguments-and-decisions.md#language-rule`
   verbatim because that file is the declared single source. Note `skills/` is a
   Claude compatibility mirror; if the language-rule source relocates during this
   migration, this ref (and `roles/response-policy.md`) must be re-pointed.

4. **"Update your agent memory" heading semantics**
   (`adapters/claude/agents/plan-team.md:147-153`): the original heading is a
   Claude-native agent-memory trigger phrase. Content was carried into the unit's
   Memory section via `_shared/memory-flow.md` plus the four concrete retention
   targets; nothing dropped, but the native trigger-phrase behavior (automatic memory
   directory wiring) depends on the surface realization noted in item 1.

## Two-layer move, turn 9-1 (2026-11-25) — placement log: plan/plan-author, plan/frame

No line was removed; old `##` headings became `###`; one position word changed with the move
("plan-document schema below" became "above" in the plan branch, which now sits after the
schema). `plan/plan-author` — 지킬 것: the `eval-spec.md` sentence (the file an
`autopilot-lab:eval-spec` dispatch writes and what it holds), Language Rule, Intent First
(decisions the plan never contradicts), Branch Selection, the Plan-Document Schema, plan-branch
steps 4–6 (the plan file at the prompt's path or the node's declared plan output, no companion
during the QA loop, the return), the whole refine-qa, refine-memos and translate branches
(in-place update, removing processed memos, the companion rules, the change-history section,
no inferred locale or output path, the return), Safety Rules, Constraints. `code-plan` and
`code-refine` tell their prompts not to restate these lines, so the unit text is their only
carrier. 참고: the role paragraph, Cross-Project User Profiles, plan-branch steps 1–3 (reading
and analysis; the `Branch — plan` heading is repeated in both layers), memory. The
`eval-spec.md` sentence was cut from its role-paragraph line at the sentence boundary; no word
changed (fix round 1, 2026-11-25).

`plan/frame` — 지킬 것: the Independence Contract (work blind), the whole Branch — frame
procedure (output path and cycle rules), the Direction-Brief Schema including the route
proposal block and its `size`/`shape` rules, Constraints, Language Rule. 참고: the role and
"why this stage exists" opening, memory. The 참고 is thin on purpose: this unit's every
section is an output or location rule.

## Field reference, first application (2026-11-25) — plan/plan-author

This unit runs under `code` (code-plan, code-refine) and `lab` (`autopilot-lab:eval-spec`). No word
changed. The role paragraph was cut at a sentence boundary: "You analyze source code and produce
detailed, accurate implementation plans." and "This unit authors the plan artifact inside the
code-plan/code-refine stages." moved under `### 참고 — code`; the specialist sentence, the project
instruction-file pointer and "it is dispatched, never user-invoked directly." (fix 1, 2026-11-25:
it is field-independent; only its first letter became a capital) stay common. A route whose field is unknown renders the body as written.
A `lab` plan gets the lab fundamentals; its eval-spec line stays in 지킬 것. The profile preloads,
plan steps 1–3 and Memory were not split (a memory read or write would change, which this step
does not do).
