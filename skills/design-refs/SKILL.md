---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: design-refs
description: "Use only when autopilot-design dispatches visual-reference collection and brief creation. Not for top-level user requests or primary capability routing."
argument-hint: "<design task> [--design <path>] [--refs <image paths>] [--no-web]"
metadata:
  group: sub
  fam: sub
  invocation_class: parent-invoked
  modes: []
  blurb: "Collect external and user-provided visual references and create a brief."
  use_when: "Use only when autopilot-design dispatches visual-reference collection and brief creation."
  not_for: "Not for top-level user requests or primary capability routing."
---

# design-refs

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write the brief, captions, and user-facing report in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve paths, URLs, query literals, tool IDs, and source titles.

## Resolve the Design

1. Use explicit `--design <path>`.
2. Otherwise select the latest applicable `design_state.yaml` under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/` or `$AGENT_ARTIFACT_OUTPUT_DIR/spec/*/design/`.
3. If no state exists, report that `design-init` must run first.

## Procedure

Do not start without enough context to avoid generic output. When brand, design system, references, and tone are all absent, run one focused clarification round covering product context, the intended variation, tone, length, audience, and constraints. Skip this for a small follow-up. Prefer an explicit gap over an invented brief.

### Step 1: Organize User Material

Process `--refs <paths>` and user attachments:

- JPG, PNG, or WebP → copy or symlink under `01_refs/_internal/user_provided/`
- URL → append to `01_refs/_internal/references_url.md`
- text brief → write `01_refs/brief_input.md`

Preserve originals and record whether an item was copied or linked.

### Step 2: Collect Optional External References

Unless `--no-web` is set, derive focused image-search queries from scope. Useful patterns include:

- `ui`: `<feature> dashboard UI inspiration`, `<feature> mobile app UI`
- `slide`: `<topic> presentation slide design`
- `icon`: `<concept> icon set minimalist`
- `diagram`: `<concept> architecture diagram`

Dispatch the `material/web-image-search` unit:

```text
Design brief: <brief>
Search queries: <queries>
max_results: 5 per query
Output: <design_path>/01_refs/_internal/web_references/
For each result record thumbnail, URL, caption, and source.
```

Use references only for analysis and style direction. Do not copy protected artwork or imply ownership.

### Step 3: Review Existing Assets

Read `00_init/asset_inventory.md`. Summarize the current token and component style, then identify which properties must remain compatible with the new work.

### Step 4: Consult Relevant User Context When Useful

For paper figures or presentation-heavy work, the acting agent may consult established visual preferences when they would materially improve the brief:

```bash
python3 <agent-home>/tools/memory/mem.py profile 01_paper_figure_style
python3 <agent-home>/tools/memory/mem.py profile 03_presentation_strategy
```

Do not make these reads mandatory. Explicit task, audience, venue, and project assets take precedence over remembered preferences.

### Step 5: Write the Brief

Write `01_refs/brief.md` in the selected artifact language using this structure:

```markdown
# Design Brief — <name>

## Intent
What the user wants to create in 1–2 lines.

## User and Audience
Who will use or view it.

## Scope
ui / slide / icon / diagram / mixed

## Tone and Mood
- 3–5 keywords such as minimal, playful, technical, or warm
- evidence from user notes and inspected references

## References
### User Provided
- <path>: short description
### External
- <url>: short description
### Existing Assets
- <path>: short description

## Constraints
- existing-token compatibility: required / optional / new system
- responsive range: mobile-first / equivalent
- accessibility: at least WCAG AA
- venue style when producing a paper figure

## Input to the Next Phase
- core mood: ...
- color direction: ...
- typography direction: ...
```

### Step 6: Update State

Update `design_state.yaml` with:

```yaml
phases:
  refs: done
brief_path: 01_refs/brief.md
```

## Output

- `01_refs/brief.md`
- `01_refs/_internal/user_provided/`
- `01_refs/_internal/web_references/`
- `01_refs/_internal/references_url.md`

## Return Format

```text
<design_path>/01_refs/ -- ✅ brief ready (N user refs + M web refs)
```

The acting agent may retain durable tone or reference patterns when they are genuinely useful. Do not make memory writes a completion requirement.
