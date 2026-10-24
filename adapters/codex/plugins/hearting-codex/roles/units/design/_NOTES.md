# design family — authoring residue (review required; nothing here may drop silently)

Sources merged: `roles/modes/design/{_design_rules,maker,critic,verifier}.md` (EN),
`adapters/claude/agent-modes/design/{_design_rules,maker,critic,verifier}.md` (KO),
`adapters/claude/agents/design-team.md` (team file). The LAW fragment
`roles/units/design/_design-rules.md` is a verbatim copy of the EN
`roles/modes/design/_design_rules.md` per the coordination instruction.

## 1. KO `_design_rules.md` detail richer than the copied EN LAW fragment

The EN copy condenses several KO-only concrete contracts. Decide whether they re-enter
the LAW fragment or stay adapter-side:

- **Font altitude lists** (KO `_design_rules.md:48-53`): code/tech = JetBrains Mono ·
  Fira Code · Space Grotesk; editorial = Playfair Display · Crimson Pro · Fraunces;
  startup/brand = Clash Display · Satoshi · Cabinet Grotesk; pairing = high contrast
  (display+mono, serif+geometric sans); weight extremes 100/200 vs 800/900 (avoid
  400/500); size jumps 3x+. EN keeps only the abstract rule ("context-specific family,
  high-contrast pairing, deliberate weight extremes").
- **Neutral chroma cap** (KO `:39`): white/black backgrounds kept subtle, chroma ≤ 0.02.
  EN says only "subtle".
- **Concrete rasterizer commands** (KO `:25`, KO maker `:43`): `sharp` one-liner with
  `density:160`, `rsvg-convert`, `mmdc` for mermaid. EN names the tools without
  invocations.
- **Bundle parity detail** (KO `:62-63`): develop multi-file (Vite+TS+Tailwind), final
  self-contained single `bundle.html` via Parcel + html-inline; reference implementation
  = public `anthropics/skills` `web-artifacts-builder`. EN condenses to "create a
  self-contained bundle only when the output contract requests one".
- **Slop blocklist provenance** (KO `:29`): "public DESIGN.md verbatim" source note.

## 2. Model-tier escalation prose not representable in single-role frontmatter

`design-team.md:62-65`: maker = deep maker but "use a fast implementer or reviewer only
for mechanical token or icon replacement"; critic = fast reviewer, "escalate to deep
reviewer for nuanced UX critique"; plus the vendor default "Claude adapter default:
opus" (a model literal — excluded by guard; belongs to the per-adapter models.conf).
Units carry one portable role name each; per-invocation escalation/downgrade is
route/node-owned. Topology (WS B) must decide whether these become branch-level role
overrides or drop.

## 3. Router frontmatter that dies with the team file

`design-team.md:1-11`: native tool list (Glob…WebFetch), adapter-configured model,
`color: pink`, `memory: project`, `metadata.modes`. Not portable; concrete
tool grants and memory scope are node/surface-owned. Nothing re-homed by design.

## 4. Language rule second clause

`design-team.md:15-18`: "keep design tokens, color names, font families, component
names, code identifiers, and file paths in their established technical form". The
response-policy pointer is kernel-owned; this technical-form clause found no unit home
(maker Output covers only rationale language). Consider adding to the LAW fragment.

## 5. Profile loading homed to maker only

`design-team.md:51-59` loaded `01_paper_figure_style`, `03_presentation_strategy`,
`05_domain_expertise` at router level for all three modes; the unit catalog homes them
in `maker.md` (creation is where they bind). Review whether critic/verifier need
`05_domain_expertise` for caption/label terminology judgment. The concrete invocation
`python3 <agent-home>/tools/memory/mem.py profile …` was abstracted to "the acting
surface's memory tooling"; also the updates-flow note "updates via /analyze-user or
/post-it --scope user" (`design-team.md:59`) was not restated (surface-owned).

## 6. Critic plan-review call-site coupling

KO `critic.md:28,32`: trigger named as "autopilot-code Step 2, task_type=ui/visual" and
log path `{log_dir}/_internal/plan_reviews/design_review.md` consumed by code-refine.
The unit keeps the log path and `[<axis>]` prefix but names the seat generically
("autopilot-code plan stage"); exact step wiring belongs to the recipe/topology (WS B).

## 7. Verifier provenance/history not restated (semantics preserved)

KO `verifier.md:8` (V7 intent, M4 §7 OCD parity, 2026-06-23 — 12 `*_match` items
redefined reference-less), `:61` (OCD `verify-ui-kit-visual-parity.ts:337` derive
pattern), `:87-88` (OCD `normalizeChecks:226-237`, `judge-visual-parity.ts:46`
lean-false pattern), `:161` (drill case `cases_growing/g8_design_verifier_breakage/`).
The behavioral rules (derived score, always-full table, unanswered=false, lean-false,
drill floor = production fallback floor) are all in the unit; the OCD/drill provenance
lives only here.

## 8. Verifier caller naming

KO `verifier.md:28,74-82`: iterate cap and `needs_review` reason-surfacing are assigned
to caller "design-review" (Step 5 verdict key). Unit says "the caller" generically;
WS B must wire the design-review recipe to own the cap and the needs_review exposure.

## 9. Verifier postwrite-hook linkage

KO `verifier.md:38`: `console.errors_zero` MCP-free path noted as "same script as the
postwrite hook". Adapter-hook linkage not restated in the portable unit; keep in
adapter docs.

## 10. Maker figure-policy provenance

KO `maker.md:20`: the layout-guide-only rule is date-stamped "2026-05-28 policy" with
rationale (LLM element-level recomposition also falls under the user-craft limit —
infinite regress). Rule preserved in the unit; date/rationale only here.

## 11. EN/KO iteration-count phrasing

EN `_design_rules.md:22` "three to five useful iterations" vs KO "최대 3-5 회전"
(hard cap). Unit bodies follow the LAW fragment (EN, "up to"); no semantic conflict
judged, recorded for completeness.
