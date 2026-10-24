# design-review

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write critique, summaries, and the user-facing verdict in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve schema keys, check IDs, paths, scores, and status literals.

## Resolve and Check State

Find the applicable `design_state.yaml` and require `phases.components: done`. Review the available source and rendered evidence, including `03_components/*.tsx`, validated screenshots, standalone previews, or an explicit user-provided mockup.

## Two-Gate Review

Run two distinct gates:

1. **Verifier**: detect breakage, console errors, layout failures, and contract violations.
2. **Critic**: assess the visual and experiential quality of output that passed the hard gate.

### Step 1: Verifier Gate

Dispatch the `design/verifier` unit as the `visual-verify` sibling review node, in an independent context.

Preserve this two-layer result schema:

- `verdict: done | needs_work`
- `breakage: has_errors | none`
- `vision_passrate: <float>` as passed `[vis]` checks divided by total `[vis]` checks
- `status: verified | needs_review | needs_iteration | failed | unavailable`
- `layer1_checks[]`: every `[det]` item as `{id, passed, reason}`
- `layer2_checks[]`: every `[vis]` item as `{id, dimension, passed, reason}`
- `gaps[]`: failed `{dimension, reason}` entries when `needs_work`
- `round_cap_hit: true` when a retry ceiling is reached

On PASS, return only `verdict`, `breakage`, and `vision_passrate` on one line. On FAIL, return textual gap reasons and breakage only. Do not return the verifier's screenshots to the orchestration context.

Use this prompt:

```text
Verify 03_components/preview.html or slides.html with the full TWO-LAYER rubric.

Layer 1, zero tolerance:
- console.errors_zero
- layout.no_overflow
- no_overlap
- no_zero_box
- components.token_contract

Layer 2, visual:
- layout.hierarchy_present
- color.palette_consistent
- typography.role_consistent
- content.intent_match
- content.no_slop_filler
- components.structure_sound

PASS → return one line with verdict, breakage, and vision_passrate.
FAIL → return gap reasons and breakage as text only.
Do not return screenshots outside the verifier context.
```

This skill owns the retry ceilings:

- console/error re-verification: at most 3 calls, native constant `MAX_DONE_ERROR_ROUNDS = 3`
- verifier → maker correction → re-verification: at most 2 rounds, benchmark contract `BENCHMARKS.md:24`

At the ceiling, return the last real verifier state with `round_cap_hit: true`; never coerce it to `done`.

If the verifier returns `needs_work`, set `design_state.yaml` `phases.review: failed`, report the failure, recommend rerunning the components stage, and stop before the critic gate.

### Step 2: Render the Critic Input

The critic must inspect rendered images rather than source coordinates alone. Follow the visual self-verification loop (`<agent-home>/roles/units/design/_design-rules.md`) for the applicable UI/webapp, slide, icon, or diagram scope.

If rendering is unavailable, state that limitation in the critique and review only what was actually observed. Do not claim visual inspection without rendered evidence.

### Step 3: Critic Gate

Dispatch the `design/critic` unit as the `critic-review` sibling review node:

```text
Run a visual review for <design_name>.
Target: 03_components/ or the resolved design material
Brief: 01_refs/brief.md
Tokens: 02_tokens/tokens.md

Review six axes:
1. hierarchy — natural eye flow and correct emphasis
2. alignment and spacing — consistency and breathing room
3. accessibility — WCAG AA contrast, keyboard navigation, focus indication, alt text
4. responsiveness — mobile, tablet, and desktop breakpoints
5. UX flow — loading, error, empty, undo, and cancel states
6. tone consistency — token compliance and fit with other components

Write 04_review/critique.md with 5–7 material findings grouped as 🔴, 🟡, or 🟢, plus a separate strengths section.
```

### Step 4: Interpret the Critique

- Any 🔴: report axis, component or file, and correction direction; reroute implementation to the components stage or maker mode.
- Only 🟡: allow handoff while clearly reporting residual issues.
- Only 🟢: pass.

### Step 5: Update State

- verifier `needs_work` or at least one critic 🔴 → `phases.review: failed`
- verifier `done` and no critic 🔴 → `phases.review: done` and `verifier: passed`

Write `04_review/verifier.md` with `breakage`, `vision_passrate`, `status`, every layer-1 and layer-2 check, and failed reasons.

When `vision_passrate` is 0.85–0.99, preserve `status: needs_review` with `verdict: done`: pass the phase but expose each failed `[vis]` reason like a critic 🟡. When `round_cap_hit: true`, report that the cap was reached and the review did not converge. Never hide regression evidence.

## Output

- `04_review/verifier.md`
- `04_review/critique.md`
- `04_review/summary.md`

## Return Format

```text
<design_path>/04_review/ -- ✅ review passed (M minor, K praise) [vision_passrate=X.XX, status=verified]
```

```text
<design_path>/04_review/ -- 🔴 N major issues found — see critique.md [breakage=has_errors|none, vision_passrate=X.XX]
```

When `round_cap_hit: true`, append the selected-language equivalent of `(round cap reached — final status: <status>, not converged)`.

The acting agent may retain durable UX or critique patterns when they are genuinely useful. Do not make memory writes a completion requirement.
