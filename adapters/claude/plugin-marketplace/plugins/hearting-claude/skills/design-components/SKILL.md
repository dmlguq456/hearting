---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: design-components
description: "Use only when autopilot-design dispatches component, mockup, or preview construction. Not for top-level user requests or primary capability routing."
argument-hint: "<design path or app path>"
metadata:
  group: sub
  fam: sub
  invocation_class: parent-invoked
  modes: []
  blurb: "Build UI components/mockups and preview artifacts."
  use_when: "Use only when autopilot-design dispatches component, mockup, or preview construction."
  not_for: "Not for top-level user requests or primary capability routing."
---

# design-components

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write specifications, preview labels, and user-facing reports in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve code identifiers, native tool IDs, paths, component names, and design-token names.

## Resolve and Check State

Find `design_state.yaml` under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/<name>/` or the app's `design/` directory.

- Require `phases.tokens: done` unless scope is `icon` or `diagram`.
- Require readable `01_refs/brief.md`.

## Procedure

### Step 1: Load Brief, Tokens, and Scaffold

- Read `01_refs/brief.md` for intent and tone.
- Read `02_tokens/tokens.md` as the token source of truth.
- Reuse the closest scaffold from `<agent-home>/scaffolds/` and copy it into the design folder before customization:
  - slide → `deck_stage/deck_stage.html`; always base decks on `deck_stage`
  - variants → `tweaks_panel/` rather than duplicating files
  - mockups → `device_frames/`
  - option comparisons → `design_canvas/`
  - image placeholders → `image_slot/`

### Step 2: Build by Scope

#### `scope=ui`

Extract the required component list from the PRD or explicit request. Run the `design/maker` unit (this node's own unit) with this task:

```text
Build UI components.
Brief: 01_refs/brief.md
Tokens: 02_tokens/tokens.css or the selected Tailwind config
Required components: [TaskRow, TaskForm, EmptyState, ...]

For each component:
- use a shadcn/ui base with Tailwind customization when appropriate
- specify props
- show one-page usage
- cover accessibility

Write:
- 03_components/<component>.tsx
- 03_components/<component>.md
```

The `.tsx` file is the working React component; the `.md` file records props, usage, and accessibility notes.

#### `scope=slide`

Run the `design/maker` unit with this task:

```text
Build presentation slides from the brief and tokens.
For every slide, define layout, color roles, typography hierarchy, and emphasis pattern.

Base the deck on <agent-home>/scaffolds/deck_stage/deck_stage.html.
Fill one <section class="slide"> per slide and preserve automatic scaling, keyboard navigation, PDF behavior, and speaker-note slots.
Use body text of at least 24px at 1920×1080.
Every slide must be renderable; do not produce only a Markdown guide.

Write:
- 03_components/slides/slide_<N>.md
- 03_components/slides/slides.html

Render and inspect every slide through the Step 4 visual loop before completion.
```

#### `scope=icon`

Run the `design/maker` unit with this task:

```text
Create icons or a logo.
- Prefer an existing Lucide or Iconify match.
- Otherwise write SVG directly.
- Use the active image-generation capability only when a complex logo or illustration requires raster generation.

Write:
- 03_components/icons/<name>.svg
- 03_components/icons/index.md
```

#### `scope=diagram`

Run the `design/maker` unit with this task:

```text
Create a diagram.
- simple flow, sequence, or architecture → Mermaid
- relationship graph, matrix, or workflow → SVG; for many-to-many relationships prefer matrices, lanes, or orthogonal gutter routing over crossing arrows
- free sketch → Excalidraw
- paper architecture figure that follows the user's deck format → layout guidance only: block list with labels, role colors, positions, flow, emphasis, and a Markdown sketch; hand off the final drawing through assets/figure/svg/ and figure_ppt/

Write:
- 03_components/diagrams/<name>.svg|.mmd|.excalidraw|.md
- a validation PNG when the output is renderable

Render and inspect the output through Step 4. A paper-architecture wireframe receives a lighter check because it is a handoff guide, not the final figure.
```

### Step 3: Integrate UI Code

For `scope=ui`, install selected shadcn/ui components only after the applicable confirmation:

```bash
pnpm dlx shadcn@latest add button card dialog
```

Place integrated code under the project root `components/ui/`. Keep customization and usage guidance under `03_components/`.

### Step 4: Rendered Visual Verification

Completion requires rendering and visual inspection; coordinates or source code alone are not evidence. Follow `roles/units/design/maker.md` and the [visual self-verification loop](../../roles/units/design/_design-rules.md).

Use the active adapter's equivalent of `preview` → `getConsoleLogs` → `screenshot` → `view_image`. Inspect every applicable state or slide for overlap, clipping, alignment, hierarchy, spacing, and color-role errors. Iterate up to 3–5 times when needed. Present the validated render through the available user-facing preview surface.

When a live browser harness is unavailable, use the supported static renderer such as `sharp`, `rsvg`, or `mmdc`, and report the limitation. Do not claim live-preview or rendered verification without evidence.

### Step 4b: Standalone Preview

When `--artifact standalone` is selected or no project stack exists, produce one self-contained browser-openable file per scope:

- `ui` or `webapp` → `03_components/preview.html` with inline `<style>` and, only when needed, CDN Tailwind/React from `https://cdn.tailwindcss.com` or `esm.sh`
- `slide` → `03_components/slides/slides.html`, one `<section>` per slide with inline CSS
- `icon` or `diagram` → `03_components/preview.html`, an inline labeled SVG gallery or diagram with legend

Render and screenshot this file, then return its path and visual evidence.

For a production-candidate UI/webapp bundle, use real Tailwind purge and actual shadcn/Radix components to build one dependency-free `index.html`; follow [`tools/web-bundle/README.md`](../../tools/web-bundle/README.md). A CDN preview is development-grade only. Verify the production bundle through the same visual loop.

### Step 4c: Polish Check

After rendering, confirm:

1. one clear focal point
2. consistent alignment and spacing rhythm
3. sufficient breathing room
4. hover, active, empty, and loading states where relevant
5. token-consistent color roles
6. distinct heading, body, and caption hierarchy

Return to Step 4 when any applicable criterion fails.

### Step 5: Update State

Update `design_state.yaml` with these functional fields:

```yaml
phases:
  components: done
components_dir: 03_components/
preview: <preview.html path or screenshot>
verified_visually: true
```

## Output

- `03_components/` with scope-specific code, specs, assets, and preview
- integrated project components only after confirmation

## Return Format

```text
<design_path>/03_components/ -- ✅ components ready (N components / K assets)
```

The acting agent may retain durable component or preference patterns when they are genuinely useful for future work. Do not make memory writes a completion requirement; explicit project conventions remain authoritative.
