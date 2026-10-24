# web-bundle — Production-Grade Single-HTML Bundle Recipe

> **What it does:** Builds a multi-file web app or UI properly with Vite, TypeScript, Tailwind, and shadcn/ui, then inlines it into one self-contained `index.html`. This is an on-premises realization of the public `web-artifacts-builder` recipe family.
> **Why this is a separate path:** The design workflow already has two single-file paths: (a) the **CDN standalone** path in `design-components` Step 4b (`https://cdn.tailwindcss.com` + esm.sh, zero build, development grade), and (b) `convert.mjs bundle`, which inlines assets from an existing HTML artifact. This recipe is the third path: a **production-grade build** with real Tailwind purging, real shadcn components, Radix, and no CDN dependency. Use it only for serious app UI and deployment-candidate artifacts.

## Choosing a Path

| Path | Fidelity | Build | Use |
|---|---|---|---|
| CDN standalone (Step 4b) | Development grade (CDN Tailwind) | None | Fast previews, mockups, diagrams, and slides |
| `convert.mjs bundle` | Inline an existing artifact | None | Make an existing `preview.html` work offline |
| **web-bundle (this recipe)** | **Production** (real Tailwind/shadcn) | Vite | Deployment-candidate web apps and UIs that must match the design system |

## Stack (`web-artifacts-builder` parity)

React 18 + TypeScript · Vite · Tailwind CSS 3.4.1 · shadcn/ui(40+) · Radix UI · path alias `@/`.

## Procedure

```bash
# 1. Create the project if needed. Inject tokens.css and tailwind.config.ts from spec/design unchanged.
npm create vite@latest <name> -- --template react-ts
cd <name> && npm i && npm i -D tailwindcss@3.4.1 postcss autoprefixer && npx tailwindcss init -p
#  shadcn/ui: npx shadcn@latest init  → add only the components you need
#  Design tokens: copy tokens.css / tailwind.config.ts from the design folder

# 2. Produce one file with vite-plugin-singlefile (all JS/CSS inline → dist/index.html).
npm i -D vite-plugin-singlefile
#  Add the plugin to vite.config.ts (see the snippet below).
npx vite build
#  → dist/index.html is a self-contained file with no external dependencies.

# 3. Copy the output into the design folder.
cp dist/index.html <design_dir>/05_handoff/exports/<name>.bundle.html
```

`vite.config.ts` snippet:
```ts
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import { viteSingleFile } from "vite-plugin-singlefile"
export default defineConfig({ plugins: [react(), viteSingleFile()], build: { cssCodeSplit: false, assetsInlineLimit: 100000000 } })
```

> **Alternative used by the original `web-artifacts-builder`:** Parcel + `parcel-resolver-tspaths` + `html-inline`, with the `@/` alias configured in `.parcelrc`. It produces the same result, but `vite-plugin-singlefile` is simpler and is therefore this recipe's default.

## Required Verification

After bundling, **always** use the adapter visual harness to run `preview(dist/index.html)` → `getConsoleLogs` (zero errors) → `screenshot` → `view_image`, or an equivalent verification path. A successful build can still render incorrectly; completion requires visual inspection.
