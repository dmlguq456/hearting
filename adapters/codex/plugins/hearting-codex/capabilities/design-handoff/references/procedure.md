# design-handoff

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write the handoff and user-facing report in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve code, paths, commands, component names, token names, and native format IDs.

## Resolve and Check State

Find `design_state.yaml` for the design.

- Require `phases.review: done`.
- When `--intensity quick` explicitly skipped review, accept `phases.components: done` instead.
- Refuse handoff when review status is `failed`; report the blocking findings and recommend returning to the owning design stage.

## Procedure

### Step 1: Inventory Deliverables

Collect:

- `02_tokens/tokens.md` and the actual generated token-file paths
- the complete `03_components/` inventory
- the required standalone preview and validated screenshot:
  - `ui`, `webapp`, `icon`, or `diagram` → `03_components/preview.html`
  - `slide` → `03_components/slides/slides.html`
- accepted issues from `04_review/critique.md`

Place the browser-openable preview at the top of the handoff. The only preview exception is a paper architecture figure intentionally handed to the user as a PPTX/layout workflow rather than generated as a final render.

### Step 1.5: Export Delivery Formats

When the request and scope need formats beyond the browser preview, use the converter provided by the active adapter or tool projection. Its interface is:

```bash
node <design-converter>/convert.mjs pdf    <preview/slides>.html [out.pdf]     # deck: one slide per page
node <design-converter>/convert.mjs bundle <preview>.html        [out.html]    # inline all assets for offline use
node <design-converter>/convert.mjs pptx   <slides>.html         [out.pptx]    # full-bleed slide PNGs plus speaker notes
```

A Claude compatibility projection may expose the same native converter at `~/.claude/tools/design-mcp/convert.mjs`; preserve these equivalent commands when that adapter owns the run:

```bash
node ~/.claude/tools/design-mcp/convert.mjs pdf    <preview/slides>.html [out.pdf]
node ~/.claude/tools/design-mcp/convert.mjs bundle <preview>.html        [out.html]
node ~/.claude/tools/design-mcp/convert.mjs pptx   <slides>.html         [out.pptx]
```

- `slide`: normally offer PDF and PPTX.
- `webapp` or `ui`: offer an offline bundle.
- `icon` or `diagram`: normally retain PNG/SVG.
- Store generated files under `05_handoff/exports/` and list them in the handoff.

### Step 2: Write `handoff.md`

Write `05_handoff/handoff.md` using the selected artifact language and this schema:

````markdown
# Design Handoff — <name>

**Completed**: <date>
**Scope**: <ui|slide|icon|diagram|mixed>
**Cycle**: <N>

---

## Preview

> Required for every scope except a paper architecture figure explicitly handed off as a user-owned PPTX/layout workflow.

| File | Location | Validated screenshot |
|---|---|---|
| `preview.html` or `slides.html` | `03_components/preview.html` or `03_components/slides/slides.html` | `<rendered PNG path>` |

The file opens in a browser without the project stack. Record the visual harness, screenshot evidence, and verifier result.

## Exports

| Format | File | Generation path |
|---|---|---|
| PDF | `05_handoff/exports/<name>.pdf` | `convert.mjs pdf`; one deck slide per page |
| PPTX | `05_handoff/exports/<name>.pptx` | `convert.mjs pptx` for slide scope |
| Inline HTML bundle | `05_handoff/exports/<name>.bundle.html` | `convert.mjs bundle` |
| Production UI/webapp bundle | `05_handoff/exports/<name>.bundle.html` | real Tailwind/shadcn build following [`tools/web-bundle`](../../../tools/web-bundle/README.md) |

## Tokens

| File | Location | Usage |
|---|---|---|
| `tokens.css` | `<project>/styles/tokens.css` | `@import` from `app/globals.css` |
| `tailwind.config.ts` | `<project>/tailwind.config.ts` | applied by the project build |

**Token version**: `v{N}`, from `design_state.yaml.tokens_version`, updated `<date>`. `autopilot-code` compares this version with implementation state during reverse-drift checks. Record history in `design_summary.md`.

Key tokens:
- Brand color: `--color-brand-500` (`#F97316`)
- Sans font: Inter
- Spacing: 4-point grid

See `02_tokens/tokens.md` for rationale.

## Components

| Name | Location | Props | Usage |
|---|---|---|---|
| `TaskRow` | `components/ui/task-row.tsx` | `{ task, onComplete }` | `03_components/task-row.md` |
| ... | ... | ... | ... |

See `03_components/<name>.md` for each complete specification.

## Frontend Implementation Guide

```tsx
import '@/styles/tokens.css'
import { TaskRow } from '@/components/ui/task-row'

export default function TasksPage() {
  return <TaskRow task={...} onComplete={...} />
}
```

## Implementation Notes

- Dark mode: `tokens.css` uses `prefers-color-scheme: dark` when specified by the design.
- Responsive behavior: components are mobile-first with desktop changes from the configured `md:` breakpoint.
- Accessibility: every interactive control has an `aria-label` or visible label.
- Accepted issues: N; see `04_review/critique.md`.

## Next Cycle

- When delegated by `autopilot-spec`, return to its build phase.
- When invoked directly, recommend `autopilot-code` for implementation.
- Token change → rerun `design-tokens`.
- Component addition → rerun `design-components`.
- New cycle → run `autopilot-design <design> --from refs`.
````

Replace example values with the actual design. Do not claim dark-mode, responsive, accessibility, token, or export behavior unless the source and rendered evidence support it.

### Step 3: Hand Off to the Caller

- Delegated by `autopilot-spec`: return the artifact path and set its `pipeline_state.yaml` field `phases.design: done`.
- Direct invocation: present `handoff.md`, the validated preview, exports, accepted issues, and the recommended next action.

### Step 4: Update State

Set `design_state.yaml` to:

```yaml
phases:
  handoff: done
```

This completes the design cycle.

## Output

- `05_handoff/handoff.md`
- optional `05_handoff/exports/`
- the caller-facing path and preview evidence

## Return Format

```text
<design_path>/05_handoff/handoff.md -- ✅ design cycle completed (cycle <N>)
```

When delegated by `autopilot-spec`:

```text
<app_path>/design/05_handoff/handoff.md -- ✅ handed off to autopilot-spec build phase
```

The acting agent may retain durable handoff friction or accepted-issue patterns when they are genuinely useful. Do not make memory writes a completion requirement; explicit project and audience contracts remain authoritative.
