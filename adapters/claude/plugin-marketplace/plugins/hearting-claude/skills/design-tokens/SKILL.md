---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: design-tokens
description: "Use only when autopilot-design dispatches design-token definition or revision. Not for top-level user requests or primary capability routing."
argument-hint: "<design path or app path>"
metadata:
  group: sub
  fam: sub
  invocation_class: parent-invoked
  modes: []
  blurb: "Define design tokens such as color, typography, and spacing."
  use_when: "Use only when autopilot-design dispatches design-token definition or revision."
  not_for: "Not for top-level user requests or primary capability routing."
---

# design-tokens

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write rationale, specimen labels, and user-facing reports in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve canonical token names, values, CSS identifiers, paths, and native tool IDs.

## Resolve and Check State

Find `design_state.yaml` under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/<name>/` or `$AGENT_ARTIFACT_OUTPUT_DIR/spec/design/`.

- Require `phases.refs: done`; do not invent tokens without a brief.
- Read `00_init/asset_inventory.md` and locate existing `tokens.css`, `tailwind.config.ts`, `app/globals.css`, or equivalent project-owned token files.

## Single Token Contract

The canonical token source is the file the application actually imports: one of `<project_root>/app/globals.css` with `@theme`, `styles/tokens.css`, `tailwind.config.ts`, or the stack-equivalent source. The design workflow owns edits to that file; code workflows consume it.

Keep only rationale and visual evidence under `02_tokens/`: `tokens.md` and the specimen. Do not create a second competing token-value source. Set `design_state.tokens_path` to the actual application file.

Seed from the codebase before designing. Extract colors, fonts, spacing, radii, and repeated inline hex or pixel values from the real token file and components. Promote the current implementation into an explicit contract, then refine it; do not start from a blank palette when a working system exists.

- Existing canonical tokens: extend by default, preserving keys and adding values directly to the application file.
- Explicit redesign: snapshot the current system under `_internal/versions/v{N}/` before replacement.

## Procedure

### Step 1: Read the Brief

Extract color direction, typography direction, tone, mood, audience, accessibility, and compatibility requirements from `01_refs/brief.md`.

### Step 2: Record Design Decisions

Write `02_tokens/tokens.md` with Color Palette (Brand, Neutral, Semantic), Typography (family and scale), Spacing, Radius, Shadow, and Motion. Record each value and one concise rationale. See [references/tokens-exemplar.md](references/tokens-exemplar.md) for the complete exemplar.

### Step 3: Render the Token Specimen

Before components consume the tokens, create the self-contained `02_tokens/specimen.html` with inline `<style>` and no build dependency:

- color swatches with hex values and foreground/background WCAG contrast ratios; label body text at least 4.5:1 and large text at least 3:1
- the complete `xs` through `2xl` type scale with line heights
- visual rulers or boxes for spacing, radius, and shadow steps

Render the specimen, inspect the resulting image, critique contrast, harmony, collisions, and uneven scale jumps, adjust tokens, and rerender until clean. Use the active adapter's equivalent of `preview` → `screenshot` → `view_image` under the [visual self-verification loop](../../roles/units/design/_design-rules.md). When the native design MCP owns the run, preserve `mcp__design__eval_js` for `getComputedStyle` evidence.

This specimen-consume gate is mandatory: components may consume the tokens only after rendered verification succeeds.

### Step 4: Write the Canonical Token File

Write or extend CSS variables in `tokens.css`, `@theme` in `app/globals.css`, or `tailwind.config.ts` according to the actual stack and selected single source. Use [references/templates.md](references/templates.md). Apply project-file changes only after the applicable confirmation.

### Step 5: Snapshot and Version Changes

Before a major overwrite or extension when a prior token system exists:

1. Copy the previous `02_tokens/tokens.md` and canonical token file into `_internal/versions/v{N}/`, where `N` is one more than the current maximum.
2. Extend while preserving keys, or replace only for an explicitly selected redesign.
3. Append a narrative to `design_summary.md` with changed tokens, old → new values, rationale, and date. This is the only design-cycle change-history source; do not add a separate CHANGELOG.
4. Ask the user only when a genuine token conflict requires a design choice.

Classify one or two small token adjustments as minor: skip the snapshot and append to the `design_summary.md` minor log. Treat a palette or scale redesign or a new axis as major. After at least five accumulated minor changes, recommend `/audit` without running it automatically.

### Step 6: Update State

Preserve these fields in `design_state.yaml`:

```yaml
phases:
  tokens: done
tokens_path: <actual application token file>
tokens_version: v{N}
tokens_updated: <date>
specimen: 02_tokens/specimen.html
tokens_verified_visually: true
```

`tokens_version` and `tokens_updated` are the reverse-drift anchors consumed by `autopilot-code`.

## Output

- `02_tokens/tokens.md`
- `02_tokens/specimen.html`
- the canonical project token file, such as `tokens.css`, `app/globals.css`, or `tailwind.config.ts`
- `_internal/versions/v{N}/` for a major change
- `design_summary.md`

## Return Format

```text
<design_path>/02_tokens/ -- ✅ tokens decided (N colors, K type scale, M spacing)
```

For an extension:

```text
<design_path>/02_tokens/ -- ✅ tokens extended (+K new tokens, existing preserved)
```

The acting agent may retain durable token preferences when they are genuinely useful. Do not make memory writes a completion requirement; the canonical application file and explicit project requirements remain authoritative.
