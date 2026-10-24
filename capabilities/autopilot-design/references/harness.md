# Visual Harness

The pipeline centers on a feedback loop in which the agent renders its own artifact, inspects pixels, and corrects defects.

| # | Component | Location | Responsibility |
|---|---|---|---|
| 2 | **Verifier unit** | `design/verifier` unit, dispatched as a sibling review node | Independent two-layer breakage gate and vision pass rate: zero-tolerance console, layout, and token checks followed by visual-consistency assessment |
| 3 | **Design rules** | `roles/units/design/_design-rules.md` | Prompt contract for slop avoidance, visual defaults, scale, HTML conventions, and variants |

When the active adapter lacks visual tooling, use the existing static-render fallback when it satisfies the requested scope; missing optional tooling does not by itself stop the pipeline. Claude-specific setup is documented in `adapters/claude/references/design-runtime.md`.
