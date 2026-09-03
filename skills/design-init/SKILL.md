---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: design-init
description: "Use only when autopilot-design dispatches design environment and state initialization. Not for top-level user requests or primary capability routing."
argument-hint: "<design task description> [--scope ui|slide|icon|diagram|mixed]"
metadata:
  group: sub
  fam: sub
  invocation_class: parent-invoked
  modes: []
  blurb: "Bootstrap the design environment and state."
  use_when: "Use only when autopilot-design dispatches design environment and state initialization."
  not_for: "Not for top-level user requests or primary capability routing."
---

# design-init

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write inventory, environment reports, and user-facing guidance in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve commands, paths, native tool IDs, package names, and state-schema values.

## Pre-Check

Look for `design_state.yaml` under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/<name>/` or `$AGENT_ARTIFACT_OUTPUT_DIR/spec/design/`.

- If present, stop and report that initialization already exists; recommend the owning new-cycle or resume path rather than deleting the folder automatically.
- If absent, continue.

## Procedure

### Step 1: Resolve the Design Name

Use the explicit user input or the app name supplied by `autopilot-spec`. Ask one concise question only when the name materially affects artifact resolution and remains ambiguous.

### Step 2: Bootstrap Rendered Verification

The components, tokens, and review stages require rendered evidence. Inspect the active adapter's visual harness and local renderers first, then provision only missing, in-scope dependencies. Do not abandon the design solely because one preferred harness is absent; use a proven static fallback when live preview cannot be attached. Stop only for an actual permission, network, safety, or tool-contract blocker.

Prefer project-local or harness-local packages. Before installing an OS-global package such as `apt install librsvg2-bin`, tell the user what will change. Follow the active runtime's approval and sandbox contract for every install.

When the Claude compatibility projection owns the run and its native `design` MCP is selected, preserve this bootstrap interface:

```bash
# 1) Node 18+
node -v

# 2) Design MCP installation
test -d "$HOME/.claude/tools/design-mcp/node_modules" && echo 'design-mcp:installed' || echo 'design-mcp:needs-install'

# 3) Native MCP registration
claude mcp list 2>/dev/null | grep -q '^design:' && echo 'design-mcp:registered' || echo 'design-mcp:unregistered'
```

Provision only missing pieces:

- `needs-install` → `(cd ~/.claude/tools/design-mcp && npm install)`; this installs the lockfile-pinned Playwright, sharp, and pptxgenjs dependencies.
- Missing Chromium → `npx playwright install chromium`, only when the selected browser harness proves it is absent.
- `unregistered` → `claude mcp add design --scope user -- node ~/.claude/tools/design-mcp/server.js`.
- After registration, acknowledge that native `mcp__design__*` tools may attach only in a new session. Use `sharp`, `rsvg`, or `mmdc` static rendering for the current cycle when that limitation applies.
- Run `(cd ~/.claude/tools/design-mcp && npm run smoke)` and require its 7/7 native smoke contract before recording `design_mcp_smoke: pass`: preview, screenshot, `view_image`, console capture, computed-style `eval_js`, and multi-step capture.

Other adapters must use their own verified visual-harness or browser-render contract instead of copying the Claude registration command. Record the exact harness and evidence in `00_init/environment_check.md`.

Inspect optional scope-specific tools:

| Tool | Use | Fallback |
|---|---|---|
| Figma connector or MCP | Referenced Figma files for UI, slides, or icons | Skip when no Figma source is requested |
| shadcn/ui CLI and `components.json` | Project component installation for `--artifact project` | Recommend `pnpm dlx shadcn@latest init` when project integration requires it |
| Tailwind config or `tokens.css` | Project design tokens | Create during the token stage |
| Native image-generation capability | Logos, illustrations, thumbnails | Use the `image_slot` scaffold when generation is unavailable or out of scope |
| `sharp`, `rsvg`, `cairosvg`, or `inkscape` | Fast standalone SVG/diagram rasterization | Prefer a renderer already bundled with the harness |
| Mermaid CLI, native ID `mmdc` | Mermaid to PNG | Install `npm i -g @mermaid-js/mermaid-cli` only when Mermaid rendering is selected and the global install is authorized |

Report exact permission or network blockers; do not guess that a missing tool was installed successfully.

### Step 3: Check Optional Figma Access

For `ui`, `slide`, or `icon`, verify the active adapter's Figma connector only when a Figma file is part of the request. A Claude compatibility run may inspect its native registry with:

```bash
claude mcp list 2>/dev/null | grep -i figma
```

Record absence as `MISSING`; do not block a design that does not use Figma.

### Step 4: Inventory Existing Assets

Inspect:

- `tokens.css` or `tailwind.config.ts`
- `components/ui/` or the project-equivalent component directory
- `public/icons/` or another SVG asset directory
- prior design cycles under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/*` or `design/`

Write the result to `00_init/asset_inventory.md`. Existing project assets take precedence over remembered cross-project preferences.

### Step 5: Create `design_state.yaml`

Preserve this machine-readable schema and native IDs:

```yaml
design_name: <name>
scope: <ui|slide|icon|diagram|mixed>
created: <YYYY-MM-DD>
output_dir: <full path>
environment:
  design_mcp: <registered|installed-unregistered|needs-install>
  design_mcp_smoke: <pass|fail|skipped>
  figma_mcp: <OK|MISSING|N/A>
  shadcn: <OK|MISSING|N/A>
  tailwind: <OK|MISSING|N/A>
  image_gen_mcp: <OK|MISSING|N/A>
  svg_renderer: <sharp|rsvg|cairosvg|inkscape|bundled>
  mermaid_cli: <OK|MISSING|N/A>
  visual_verify_ready: <true|false>
existing_assets:
  tokens_file: <path or null>
  components_dir: <path or null>
  icons_dir: <path or null>
phases:
  init: done
  refs: pending
  tokens: pending
  components: pending
  review: pending
  handoff: pending
last_updated: <timestamp>
```

Set `visual_verify_ready: true` only when the selected live harness is verified or a tested static-render path can satisfy the scope. Preserve `design_mcp` fields for cross-adapter artifact compatibility even when another adapter owns rendering; explain the selected alternative in `environment_check.md`.

## Output

- `00_init/environment_check.md`
- `00_init/asset_inventory.md`
- `design_state.yaml`

## Return Format

```text
<output_dir>/00_init/ -- ✅ init completed (scope: <scope>, K tools OK, M missing)
```

When a required tool contract remains unavailable:

```text
<output_dir>/00_init/ -- ⚠️ init completed but K required tools missing — install before next phase
```

The acting agent may retain durable environment patterns when they would materially improve future setup. Do not make memory writes a completion requirement; current environment evidence and project configuration remain authoritative.
