# Changelog worked example — legacy → frontmatter array migration

Before (legacy, broken preview):

```markdown
<!-- CHANGELOG (auto-managed by draft-refine — do NOT edit manually)
v2 (2026-05-14T14:00, applied 22 memos / overrode 0 memos):
  - [M25 QUALITY 🔴] [verified .bib L366]: `\citep{defossez2023}` → `\citep{defossez2023high}`
v1 (2026-05-14, initial draft from autopilot-draft paper pipeline): camera-ready cheatsheet ...
-->

---
type: paper
status: draft
date: 2026-05-14
---

# Camera-Ready Cheatsheet ...
```

After (frontmatter array, preview-safe):

```markdown
---
type: paper
status: draft
date: 2026-05-14
changelog:
  - version: v2
    date: "2026-05-14T14:00"
    applied: 22
    overridden: 0
    entries:
      - |
        [M25 QUALITY 🔴] [verified .bib L366]: `\citep{defossez2023}` → `\citep{defossez2023high}`
  - version: v1
    date: "2026-05-14"
    note: "initial draft from autopilot-draft paper pipeline"
    entries:
      - |
        camera-ready cheatsheet ...
---

# Camera-Ready Cheatsheet ...
```
