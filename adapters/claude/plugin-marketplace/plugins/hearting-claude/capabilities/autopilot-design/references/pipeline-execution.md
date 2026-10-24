# Pipeline Execution — Phases 0–5

> This file expands the phase and stage-worker overview in `SKILL.md`. The checkable `[CONFIRM Gate]` wording and its four response branches remain in `SKILL.md`.

The main agent orchestrates by invoking each Skill directly.

### Phase 0: design-init

If `design_state.yaml` is absent or `--from init` is set, invoke `design-init` with the design task.

`design-init` provisions the Design MCP: `npm install`, `claude mcp add design --scope user`, and `npm run smoke`. Install missing local dependencies and continue; report an operating-system-wide installation in one line.

Outputs: `00_init/environment_check.md` and `design_state.yaml`.

### Phase 1: design-refs

Invoke `design-refs` with the task and optional image paths. Collect:

- user-provided images by attachment or path;
- external references, optionally through dispatching the `material/web-image-search` unit;
- existing design systems, paper figures, and prior-cycle assets.

Outputs: `01_refs/brief.md` and `_internal/references/` containing images, URLs, and notes.

### Phase 2: design-tokens

The `icon` scope may skip this phase. Otherwise invoke `design-tokens` with the design path.

Outputs:

- `02_tokens/tokens.md` with design rationale;
- `02_tokens/specimen.html` showing palette, typography, and spacing. Render and inspect it before components consume the tokens;
- `02_tokens/tokens.css` or `tailwind.config.ts`, the live token source.

Extend an existing token source; do not overwrite it wholesale.

### Phase 3: design-components

Invoke `design-components` with the design path. That stage runs as the `design/maker` unit, which must render, inspect, and revise its own result.

Outputs under `03_components/` vary by scope:

- `ui`: shadcn/ui and custom components;
- `webapp`: page composition plus full-screen interactive `preview.html`;
- `slide`: a Markdown visual guide, render of every slide—or one contact sheet for a large deck—and self-contained `slides.html` with one section per slide;
- `icon`: SVG or image;
- `diagram`: Mermaid, SVG, or Excalidraw plus rendered PNG.

With `--artifact standalone`, also emit a self-contained `preview.html`. Present rendered images with the artifact to preserve live-preview parity.

### Phase 4: design-review

Skip at `--intensity quick`; otherwise invoke `design-review` with the design path.

It runs two gates:

1. The `design/visual-verify` sibling node (unit `design/verifier`) independently checks console, layout, and token breakage, then reports `vision_passrate`.
2. The `design/critic-review` sibling node (unit `design/critic`) inspects rendered images across six quality axes; the two nodes run concurrently with disjoint scopes.

Outputs: `04_review/verifier.md` with verdict, breakage, pass rate, status, and failure reasons; plus `04_review/critique.md`.

On 🔴 or verifier `needs_work`, set `design_state.yaml` `phases.review: failed`, report the failure, and recommend rerunning the component phase. Verifier blocks breakage before critic evaluates quality.

### Phase 5: design-handoff

Invoke `design-handoff` with the design path.

Outputs:

- `05_handoff/handoff.md` with component and token locations, frontend import paths, and reproduction guidance;
- `05_handoff/exports/`, when requested and scope-appropriate, containing PDF, self-contained HTML, or PPTX from `convert.mjs`;
- when called by autopilot-spec, return the result path to the caller.
