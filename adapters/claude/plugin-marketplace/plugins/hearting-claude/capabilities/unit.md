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
- `--start --prompt-file <brief>` starts the unit's one worker with the brief (see Launch and end). Without `--start` the route is only planned and sealed.

## Field

The route records a field like every route. It is the unit head's `field:` (`field_source: preset`), or `--field` (`explicit`). A unit head writes `field:` only where the field is clear (`dev/*`, `qa/code-review`, `qa/security-review` → `code`; `qa/ml-debug` → `lab`; `design/*` → `design`; `editorial/polish`, `editorial/review`, `editorial/translate` → `docs`; `research/research-survey` → `research`). A unit without one is refused, naming what is missing, unless `--field` is given (`compose-field-required`).

## Scope

The node writes only under the cycle's `units/unit/**` (one report). A read-only unit creates no new worktree: it reads the caller's.

## Launch and end

- **Launch.** The depth-0 session (no owner above it) starts the leg through the same path that starts a frame leg: `compose --start --unit <family/name> --prompt-file <brief>` seals the route and starts the one attempt, `capability-route.py start --route <id>` starts or follows it again, and a second start launches nothing twice. The worker's first instruction is the unit's text (stance and body), the dispatch facts, a few of the kernel's safety lines (its role, guards and scope, what it leaves to the supervisor, scratch), the brief as written, the node's declared scope and how to end; nothing the owner reads is added. The model grade is the one a staged route gives that unit (the grade the shipped recipes declare for it; `--profile` wins).
- **End.** The launch seals `workflow_completion=runtime-v1` on the leg, so the completion controller closes the route and seals the cycle when the worker ends, as it does for an owner. No close or finalize command is run by hand.
- **Result.** The completion notice that wakes the launching session carries the route's sealed summary: the verdict line, the review's blocking findings (a `Blocking findings` or `병합을 막는 것` section reads as remaining items) and the report path.
- **A review that finds blocking issues** ends `verdict: FAIL` with its report, which opens (its first non-heading line) on the closed verdict `Final verdict: changes requested — head <sha>` (`최종 판정: 수정 후 병합 — head <sha>`; the worker's first instruction says so; where there is no git repository the ` — head <sha>` is left out and the verdict still counts). The runtime reads that end as it reads a finished audit's negative judgment: a pass whose assessment of the subject is negative, so the route closes, the cycle seals and the notice carries the same summary as any other end (its result line says the review completed with findings). Only a `BLOCKED` end (the work could not be done), an end with no readable report, or a FAIL whose report does not open on that verdict (empty, says no review was done, declares a pass) is offered again as a new route (`compose-again`). In the blocking-findings section of a report, a heading or line that only says there is none (`막는 지적 없음`, `none`) lists nothing; anything else stays a remaining item.

## Completion

The `unit-complete` completion gate is the existing `custom` contract row: the unit's own `io.verdict` and its report. A unit route's worker ends with the three-line handoff of the worker kernel; the report is `units/unit/report.md` in the cycle.
