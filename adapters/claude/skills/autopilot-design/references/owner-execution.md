# autopilot-design

> **Output locations**:
> - Direct invocation: `<artifact-root>/designs/<name>/`
> - Delegated by `autopilot-spec`: `<artifact-root>/spec/design/`
>
> Follow CONVENTIONS §5: T1 root and `design_state.yaml`; T2 phase directories such as `00_init/` and `01_refs/`; T3 `_internal/` within each phase.

## Ownership Boundary

Design leads visual decisions; code applies the resulting visual specification (DESIGN_PRINCIPLES §9).

- Design tokens have one source of truth: the file imported by the application, such as `globals.css` with `@theme` or `tokens.css`.
- `designs/` and `spec/design/` hold references, mockups, rationale, and specimens as decision records, not duplicate token sources.
- For an existing app, render and inspect the real screen rather than relying only on a mockup.
- Route direction, token, new-layout, and structural visual changes here. Keep only trivial one-property tweaks in `autopilot-code`.

## Invocation

Use this pipeline for UI or page design, slides, icons, logos, illustrations, design tokens, palettes, or multi-phase visual critique. `autopilot-spec` may delegate its design phase here automatically.

Defaults:

- `--scope mixed`; infer a narrower scope from the request and files when clear
- `--from`: resume after the last completed phase in `design_state.yaml`, otherwise start at `init`
- `--intensity`: derive verification rigor from intensity; there is no separate `--qa` axis (CONVENTIONS §1.1 (`<agent-home>/core/CONVENTIONS.md#11-verification-rigor-tiers`))

Direct boundaries:

- One component or asset may go directly to the `design/maker` unit.
- Critique-only work may go directly to the `design/critic` unit.
- Reference-only collection may go directly to the `material/web-image-search` unit.
- An explicit `/autopilot-design <args>` invocation supplies the routing choice directly.

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write user-facing output in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve code identifiers, paths, and canonical token names or values.

## Arguments

### `--scope` (auto-detected by default)

- `ui`: individual frontend components such as buttons, cards, and forms
- `webapp`: a composed screen, page, or landing experience with layout and interaction states
- `slide`: presentation visuals
- `icon`: a single icon, logo, or illustration asset
- `diagram`: architecture, flow, or relationship diagrams in Mermaid, SVG, or Excalidraw
- `mixed`: more than one of the above

Skip phases only when the scope makes them unnecessary:

- `icon`: tokens may be unnecessary; skip components when producing a single asset directly
- `diagram`: normally skip tokens and components, focusing on references, rendering, review, and handoff

### `--artifact`

- `project`: integrate into the project's `components/ui/` and token files; prefer when a stack already exists
- `standalone`: create a self-contained `preview.html` with inline CSS/JS and optional CDN dependencies; prefer when no stack exists or a portable preview is required
- Auto-detect `project` when cwd contains `package.json`, `components.json`, or `tailwind.config.*`; otherwise use `standalone`

### `--from`

Accepted phases: `init`, `refs`, `tokens`, `components`, `review`, `handoff`. When `design_state.yaml` exists, default to the phase after the last `done` entry.

### Verification Rigor

- quick-tier: review may be skipped when the selected graph permits
- standard-tier: run the standard review phase
- thorough-tier: add a `design/critic` review node and external-reference cross-checking

## Context Auto-Detection

Inspect the request and cwd before choosing a phase.

1. Find `<artifact-root>/designs/<name>/design_state.yaml` or `<artifact-root>/spec/design/design_state.yaml`.
2. If neither exists, start a new cycle at `init`.
3. If state exists, infer the earliest affected phase from the requested change:
   - environment or integration check → `init`
   - new or replaced references → `refs`
   - color, typography, spacing, radius, or shadow changes → `tokens`
   - new components or variants → `components`
   - critique-only rerun → `review`
   - refreshed imports, exports, or reproduction guidance → `handoff`
4. Honor an explicit `--from <phase>` without re-inferring it.

For a resumed cycle, preserve unaffected tokens and artifacts. Reset only downstream phase state.

Before `design-init`, apply the CONVENTIONS §6.6 intake gate when visual direction, tone, target device, design-system status, brand constraints, or artifact form are materially underspecified. Ask one structured round in the conversation language. Skip the intake round for sufficiently explicit requests and resumes.

## Pipeline and Stage Dispatch

For `standard+`, dispatch each durable phase as its own dispatch-depth-2 headless session under OPERATIONS §5.10 (`<agent-home>/core/OPERATIONS.md#510-work-isolation-and-parallel-dispatch`). The dispatch-depth-1 conductor passes artifact paths and reads verdict/status only. Each phase reconstructs context from `design_state.yaml` and prior artifacts; conversation memory is not a handoff channel. Stage sessions never redispatch.

Keep `direct`, `quick`, and artifact-free micro-stages inline. Between dispatched phases, record the confirmation verdict in `design_state.yaml` before launching the next phase.

| Stage | In-session role | Inputs | Outputs | Write class |
|---|---|---|---|---|
| Phase 0 `design-init` | orchestrator | Request + cwd | `00_init/environment_check.md`, `design_state.yaml` | init |
| Phase 1 `design-refs` | orchestrator; `material/web-image-search` unit for external search | User images, existing design assets, request | `01_refs/brief.md`, `_internal/references/` | refs |
| Phase 2 `design-tokens` | orchestrator | `01_refs/brief.md`, existing token files | `02_tokens/tokens.md`, `02_tokens/specimen.html`, `tokens.css` or `tailwind.config.ts` | tokens |
| Phase 3 `design-components` | `design/maker` unit | `02_tokens/tokens.*`, `01_refs/brief.md` | `03_components/` specs, mockups, code, and previews | components |
| Phase 4 `design-review` | `design/verifier` unit, then `design/critic` unit | Rendered `03_components/` output | `04_review/verifier.md`, `04_review/critique.md` | review; read-only against components |
| Phase 5 `design-handoff` | orchestrator | Components, reviews, and tokens | `05_handoff/handoff.md`, `05_handoff/exports/` | handoff |

Serialize shared writes. In particular, one phase at a time may update only its own `phases.<phase>` entry in `design_state.yaml` after the preceding confirmation gate.

## Paper Architecture Figures

For paper architecture figures, produce layout and composition guidance but do not claim that an LLM-authored diagram is publication-ready. Read [references/paper-figure-policy.md](paper-figure-policy.md) for the scope and handoff boundary. Other scopes complete through the rendered visual-verification loop below.

## Visual Harness

Read [references/harness.md](harness.md) for the visual feedback loop, rendering tools, verifier, design rules, scaffolds, converters, and post-write checks. `design-init` should provision the available visual harness or report its fallback; absence of one integration alone must not stop the cycle.

## Rendered Visual Verification

Complete token, component, and review phases only after rendering and inspecting the result. Valid coordinates, source code, or XML are not visual evidence.

- Load the shared design rules (`<agent-home>/roles/units/design/_design-rules.md`) and follow their render, self-critique, and bounded iteration loop for HTML/React, SVG, and diagrams.
- Inspect overlap, clipping, alignment, hierarchy, contrast, and responsive states relevant to the scope.
- Show the rendered result to the user; a text-only report is not completion evidence.
- When the primary visual integration is unavailable, use an available static renderer such as `sharp`, `rsvg`, or `mmdc`, and report the fallback.

At each phase boundary, apply the four-way confirmation contract in the conversation language: continue, revise the current phase, back-jump to an earlier phase and reset downstream state, or stop while preserving `design_state.yaml`. Do not guess when a materially different visual direction remains unresolved.

## Pipeline Execution

Read [references/pipeline-execution.md](pipeline-execution.md) for invocation arguments and exact output lists. The table below defines phase completion gates:

| Phase | Invoke | Completion gate |
|---|---|---|
| 0 | `design-init` | Environment checked and visual harness provisioned or fallback recorded; confirm whether to continue to references |
| 1 | `design-refs` | Reference brief complete; confirm whether to continue to tokens |
| 2 | `design-tokens` | Tokens and specimen rendered; confirm whether to continue to components |
| 3 | `design-components` | Components or assets rendered and inspected; confirm whether to continue to review |
| 4 | `design-review` | Verifier and critic verdicts recorded; on `needs_work`, mark review failed and return to components |
| 5 | `design-handoff` | Handoff and exports complete; final confirmation may accept, back-jump, or stop |

## Design State

```yaml
design_name: <name>
scope: ui  # or webapp/slide/icon/diagram/mixed
artifact: standalone  # or project; auto-detected from the stack
created: <date>
phases:
  init: done
  refs: done
  tokens: done
  components: done
  review: done
  handoff: pending
last_updated: <timestamp>
```

## Delegation from `autopilot-spec`

When `autopilot-spec` invokes `autopilot-design --app <name>` in Phase 2:

- write outputs under `<artifact-root>/spec/design/`
- inherit its `--intensity`, including the derived verification rigor
- after completion, set `phases.design: done` in the calling pipeline's `pipeline_state.yaml`

## Return Format

```text
<output_path> -- ✅ Phase {N} ({phase_name}) completed
```

After the full cycle:

```text
<output_path> -- ✅ Design cycle completed (handoff ready)
```

## Optional Continuity

The acting agent may retain useful, non-obvious design preferences or recurring patterns through the normal memory path when they are likely to improve future work. Do not turn examples such as density, color, typography, component patterns, or scope-specific phase choices into deterministic storage rules.

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/autopilot-design.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability autopilot-design
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `designs/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
