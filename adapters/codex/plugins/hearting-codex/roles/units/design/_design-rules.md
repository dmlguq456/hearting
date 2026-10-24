# Shared Design Rules

> Maker, critic, and verifier read this prompt-level contract before work. The runtime adapter supplies concrete rendering tools.

## Principles

1. Visual feedback is the core loop. Render, capture, inspect, critique, and iterate after every meaningful build.
2. Separate verification from creation so an independent context checks console, screenshot, and layout without maker bias.
3. Start from brand, design-system, and reference context. When consequential direction is missing, align with the user before building.
4. Declare color, type, spacing, layout, and motion as a system before implementation.
5. Every element needs a reason; avoid filler content, arbitrary statistics, and decorative icon noise.
6. Fixed canvases such as slides and video letterbox themselves through viewport-aware scaling.

## Required Visual Loop

For every renderable HTML, React, SVG, or diagram artifact:

1. Render through the adapter visual harness, capture a screenshot, and collect console errors in the same session.
2. Fix console and page errors before visual critique.
3. Where supported, capture responsive viewports and interaction states, crop suspicious regions, and inspect computed contrast and boxes. When unavailable, report the single-snapshot limit honestly.
4. Inspect penetration, overlap, alignment, spacing, hierarchy, color roles, clipping, and empty space. Cross-check suspicious points with DOM measurements when the runtime exposes them.
5. Revise and rerender for up to three to five useful iterations.
6. Report observations rather than coordinate claims and present the rendered image to the user where the calling flow supports it.

Scope guidance:

- UI/web apps: inspect components and full-page composition, mobile and desktop, and loading/error/empty plus relevant interaction states.
- Slides: render every slide through the deck scaffold; no slide may remain an unrendered guide.
- Icons: rasterize SVG at sufficient density and inspect at enlarged scale.
- Diagrams: rasterize SVG or Mermaid and inspect crossings, overlaps, and labels.

If the full visual harness is unavailable, use a static rasterizer for SVG or diagrams and describe exactly what was and was not inspected. HTML interaction and console verification require a browser-backed harness.

## Avoid Generic AI Visual Convergence

Avoid default purple gradients, aggressive gradient backgrounds, rounded containers with arbitrary left accent borders, uniform rounded-card repetition, excessive centered layout, timid evenly distributed color, emoji as non-brand decoration, and unconsidered default fonts such as Inter, Roboto, Arial, Open Sans, Lato, `system-ui`, or overused Fraunces. Do not fake imagery with hand-drawn SVG; reserve a clearly labeled image slot instead. Ask before inventing additional sections or content.

When no design system or references exist, align on warm, cool, or neutral tone; use one to three readable fonts; limit accents to zero to two coherent OKLCH colors; and extend from brand color rather than improvising a palette.

## Conceptual Altitude

Before building, describe four decisions in targeted language rather than generic “balanced modern” defaults:

1. **Typography:** choose a context-specific family and high-contrast pairing, with deliberate weight extremes and clear size jumps. Avoid converging on a new substitute default.
2. **Color/theme:** one tonal direction and restrained OKLCH accents.
3. **Motion:** only meaningful entry, emphasis, and state transitions.
4. **Spatial composition:** hierarchy through whitespace and grid; flat color often beats a gratuitous gradient.

Use a concrete cultural, brand, or interface reference when a direction remains vague.

## Stack and Bundle

For component or web-app artifacts, prefer the project's established stack. Where no stack exists, the supported reference set includes React, Tailwind, shadcn/ui, Radix, Recharts, Lucide, Three.js, and Motion as appropriate rather than mandatory dependencies. Develop multi-file applications normally, then create a self-contained bundle only when the output contract requests one.

## Scale and HTML

- Slide body text is at least 24 px; print body at least 12 pt; mobile touch targets at least 44 px.
- Wrap fixed 1920×1080 canvases in a full-viewport stage with scaled letterboxing and controls outside the scale transform.
- Close every non-void HTML element explicitly, quote attributes, and avoid non-void self-closing syntax.
- Use flex/grid and `gap` for UI groups.
- Avoid `scrollIntoView` in SPA and deck code; use a controlled scroll method.
- Split files that exceed roughly 1,000 lines into importable components.

## Variants

Add a requested variation as a tweak in the original artifact rather than proliferating files. Use the tweaks-panel scaffold and three or four curated swatches instead of a free-form color picker.
