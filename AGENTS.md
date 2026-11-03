# Hearting Maintenance Principles

These principles apply to every agent that develops or changes Hearting itself,
equally under Claude, Codex, and OpenCode. They do not change how agents work in
other projects that use Hearting.

## Why Hearting exists

The owner stated this purpose on 2026-11-02 (the Korean original is kept in
project memory). Every change to Hearting starts from it.

1. **Consistent work regardless of agent quality.** Agents vary; some do more
   on their own, some less. Hearting must still stop actions that drift from
   the user's intent or break the existing flow, so the user stays in charge
   and the work holds together even when the agent's role is minimal.
2. **One way of working in any environment.** The agents and resources at hand
   differ by workplace and setup, yet working without agents is no longer an
   option. Hearting is how the user keeps one way of working across them;
   cross-harness support and the monitoring tools exist for this.
3. **A crutch for low-cost agents.** Hearting cannot make a weak agent smarter,
   but it should let even a slow, low-cost agent carry out the user's intent
   step by step. Acting without understanding the intent gets worse as agents
   get cheaper, and the result misses it; capable agents usually grasp the
   intent and still check before acting. Hearting closes that gap. Frame and
   dispatch are its core: a capable model settles the intent and the plan, and
   lower-cost workers carry it out in small, checked steps.
4. **Minimal cognitive load on agents.** Do not build anything that works only
   if an agent memorizes long procedures or documents.
5. **Existing assets before the agent's own knowledge.** Look first for what the
   codebase, memory, and earlier artifacts already settled or implemented, and
   for outside precedent when needed, instead of leaning on the agent's own
   knowledge, which is weakest in low-cost agents.

Judge every mechanism by whether it serves these points. Reduce what does not;
keep frame and dispatch, and fix their seams rather than cutting them.

## The standard before detailed procedure

**The harness must not become a burden on users or agents.** Work is done when
the requested task finishes without needless intervention in normal, failure,
and interruption cases — not when more checks or PASS markers exist.

- If an existing workflow, recipe, or adapter procedure repeatedly blocks
  approved work, fix, merge, or remove that procedure too. Existing structure
  is not a fixed premise.
- Do not answer each failure with another gate, proof, seal, approval, required
  input, recovery command, or owner chain. "Internal validation" or "no new
  flag" does not excuse added burden.
- When the same kind of blockage recurs, stop stacking exceptions; remove the
  duplicated decisions and shorten the path itself. Do not route back through a
  mechanism you are removing just to repair it first.
- Treat rigid recovery paths, hard cross-harness parity, and a slow or empty
  Fleet as structural problems rather than one-off fixes: follow
  [core/LOOP_ENGINEERING.md](core/LOOP_ENGINEERING.md) §7–§10 (recovery follows
  lineage, capabilities follow real work, hard parity moves outside the
  adapters, delegated work stays visible).
- Protect user approvals, data, and running work within existing normal
  behavior, without shifting extra proof or manual recovery onto agents.
- Do not add a checklist, hook, or approval step to enforce these principles.

This standard overrides detailed procedural defaults when maintaining Hearting.
Do not re-enable a gate the user has turned off. Start code changes in `core/`,
`capabilities/`, and `roles/`, which own the shared meaning, and treat the
three adapters equally. The development philosophy is
[core/LOOP_ENGINEERING.md](core/LOOP_ENGINEERING.md).
