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
