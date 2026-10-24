# Capability: route-frame

This is the portable contract for `route-frame`, the compiler-internal capability behind the framed route shape. It is not a Claude Skill file, not a native command, and not an owner persona: no person and no model invokes it by name. It is reached through `capability-route.py compose --start --prompt-file <task>` with `--shape` omitted or explicitly `framed`.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `route-frame` |
| Group | `sub` |
| Supported modes | `none` |
| Portable meaning | Compiler-internal framed front end: two independent top-tier frame legs by default; an explicit quick/direct request may use one deep leg when no protected path, multiple adapter paths, or spec/ship hint requires both; ends at a model-less route-decision terminal. |
| Argument shape | `compose --start --prompt-file <task> [--shape framed]` |

## Shape

A framed route is sealed at intensity `standard`. With no intensity it runs two independent top-tier frame legs by default. Explicit `standard` or higher also runs both. Explicit `quick` or `direct` runs one deep leg only when the request has no protected repository path, multiple adapter paths, or spec/ship capability hint; otherwise it keeps both legs:

| Node | Kind | Runs as | Model |
|---|---|---|---|
| `frame` | map-worker, unit `plan/frame` | registered depth-1 attempt, launched by the depth-0 session | `deep` alone, `top` beside the alternative |
| `frame-alternative` | map-worker, unit `plan/frame` | only with both legs: a separate registered depth-1 attempt, never a batch with its sibling | `top` |
| `route-decision` | runtime-terminal | the runtime itself; no attempt, no unit, no role | none |

Two legs never read each other's brief. The terminal depends on every frame leg and carries the one `frame-review` human gate at its entry. There is no owner node and no owner launch.

An owner pin applies only to the owner, not to frame legs; automatically
selected two-leg frames prefer different harnesses, using one harness only
when it is the only usable family or the person explicitly pins the frame,
with that reason shown on the card.

## What the runtime does

1. Before `start` launches the frame legs as separate registrations, it prepares
   the cwd the frame will read. A failed preparation records a one-line reason
   and continues from the original cwd. Dirty or unmerged implicit linked
   worktrees may be retained for reading; this does not approve a source leg to
   work there, which still needs the existing proposed `cwd` approval. The
   frame's newly allocated worktree is carried into its first source-changing
   leg. The frame prompt names the basis it reads in one line.
2. `start` joins the frame legs.
3. The existing single frame interview (`needs-interview`, then `needs-question`) asks the person once.
4. On `proceed` the runtime fixes one `route_decision_v1` record, completes the terminal with that record as its evidence, closes the route and finalizes the producer cycle.
5. `revise` and `stop` behave as they do for every other frame route.

The record has an immutable `decision` part with its canonical digest and a separate, monotonic `first_leg` part. The record is the terminal's completion evidence; it adds no approval, no required input and no recovery command.

## Hints

`--capability`, `--capability-mode`, `--graph` and `--profile` given with `--shape framed` are recorded as `routing_hints` in the work request. They are never sealed as the route's capability and never refused as an argument conflict. `--pin`, `--campaign-key`, `--parent-cycle` and `--unassigned` apply as for any route.

## Completion

The `route-decision` completion gate is the existing `custom` contract row: the runtime terminal marker over the fixed `route_decision_v1` record. It is accepted only by the exact-shape check for this capability; no other capability may declare a runtime terminal.

## Artifact Ownership

The record is written under the producer cycle as `shards/frame/route-decision.json`. Frame briefs are `shards/frame/direction-brief.md` and `shards/frame-alternative/direction-brief.md`. Durable output stays under the canonical artifact root.
