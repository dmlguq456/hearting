# Capability: unit

This is the portable contract for `unit`, the compiler-internal capability behind a route that is one unit and nothing else. It is not a Claude Skill file, not a native command, and not an owner persona: no person and no model invokes it by name. It is reached through `capability-route.py compose --unit <family/name> --prompt-file <brief>`.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `unit` |
| Group | `sub` |
| Supported modes | `none` |
| Portable meaning | Compiler-internal one-unit route: one read-only unit runs as a single leg the depth-0 session launches itself, with no owner; its kind is `unit`, its field comes from the unit head or --field. |
| Argument shape | `compose --unit <family/name> [--field <field>] --prompt-file <brief>` |

## Shape

The route is sealed at intensity `quick` with one node. There is no owner node:

| Node | Kind | Runs as | Model |
|---|---|---|---|
| `unit` | map-worker, the named unit | a registered depth-1 attempt, launched by the depth-0 session (`launch_authority: depth-0`) | the grade of the unit head's `floor` (`near-zero`/`low` → `light`, `moderate` → `balanced`, `high` → `balanced-deep`, `highest` → `deep`); `--profile` overrides it; `top` is not available |

The node's unit, role and worker type are the unit head's. The recipe holds only the template (`capabilities/topologies.json`, recipe `unit`), so the expected node of a sealed route is read from the recipe and the head, not from a table of node ids.

## Arguments

- `--unit <family/name>` names the kind itself. Nothing else has to be typed; an argument that contradicts it (`--capability` other than `unit`, `--shape` other than `solo`, `--intensity` other than `quick`, `--graph`, `--route-plan`, `--signal`, `--execution-scope`) is refused by name (`compose-unit-conflict`).
- A name that is not a unit of the catalog is refused with the list (`compose-unit-unknown`).
- v1 opens read-only units (`read_only: true`). A unit that writes is refused with its reason (`compose-unit-writes`).
- `--start` is not available yet: the route is planned and sealed, and `--unit` with `--start` is refused by name (`compose-unit-start-unavailable`).

## Field

The route records a field like every route. It is the unit head's `field:` (`field_source: preset`), or `--field` (`explicit`). A unit head writes `field:` only where the field is clear (`dev/*`, `qa/code-review`, `qa/security-review` → `code`; `qa/ml-debug` → `lab`; `design/*` → `design`; `editorial/polish`, `editorial/review`, `editorial/translate` → `docs`; `research/research-survey` → `research`). A unit without one is refused, naming what is missing, unless `--field` is given (`compose-field-required`).

## Scope

The node writes only under the cycle's `units/unit/**` (one report). A read-only unit creates no new worktree: it reads the caller's.

## Completion

The `unit-complete` completion gate is the existing `custom` contract row: the unit's own `io.verdict` and its report. Launching and closing a unit route are not part of this contract yet.
