---
unit: research/fact-check
family: research
role: fast fact-checker
worker_type: review
floor: near-zero
read_only: true
stance: none
io:
  verdict: [no-issues, memos-added, conflicts-found]
  return: _shared/dual-io.md
tools: []
branches: []
aliases: {}
---

# Unit: research/fact-check

Narrow verbatim matching only — no creative judgment. This is the selected
source-check gate for research, draft, refinement, and their strategy stages
when claims, citations, or cards are actually in scope. Internal provenance
only: whether a claim matches the local cards. Adversarial external truth
verification is research/claim-verify's layer — a claim can match a card while
the card itself is wrong.

## Classification rule (single source of truth)

| Source type | Meaning | Verdict |
|---|---|---|
| `cards-verbatim` | The claim value (venue string / number / metric / year) appears verbatim in the matched card body or metadata fields | ✅ allowed |
| `cards-name-only` | The model or author exists in a card but the specific venue/year/metric is not verbatim present | 🟡 caution; reverify externally (web search/fetch) |
| `external-marker` | The claim is explicitly marked unverified/external in the artifact (`[?]`, `[unverified]`, "not in cards") | 🟡 caution; reverify |
| `external-reverified` | A 🟡 was confirmed externally with a logged authoritative URL | ✅ allowed post-reverification |
| `conflict` | A card contains a *different* value (e.g. card "IWAENC 2024" vs claim "IS 2024") | 🔴 fail |
| `no-match` | No card contains the claim | 🔴 fail |
| `ambiguous` | Several candidate cards, no single best match | 🟡 caution |
| `circular-ref` | Draft and strategy cite each other rather than cards | 🔴 fail (architecture violation) |

## Verification rules (CRITICAL)

1. **Name-only match is never sufficient.** If a card has only the name and
   the venue/year/metric is not verbatim, the result is 🟡 — card *existence*
   alone never verifies a value.
2. **Circular references are forbidden.** Never use a strategy document's
   venue-mapping table as ground truth to pass a draft claim; validate draft
   and strategy each *directly against cards*. (Incident: a wrong venue passed
   two layers because the strategy checker accepted a name-only match and the
   draft checker mirrored the strategy.)
3. **Section-heading context cross-check (MANDATORY).** Cross-check each
   claim's nearest enclosing section heading (H1–H3) token set against the
   matched card's classification token set using conflict pairs such as:
   - {deep learning, neural, DNN} ↔ {classical, statistical, signal
     processing, non-learning}
   - {denoising, noise reduction} ↔ {dereverberation, reverb} ↔ {BWE,
     bandwidth extension} ↔ {GSR, general restoration, universal SE}
   - {single-task, sub-task} ↔ {universal, multi-task, GSR}
   On conflict, emit 🔴 (e.g. a classical method silently listed under a
   deep-learning heading). The seed pairs are a mechanism default; the user's
   task-taxonomy and preference context lives in
   `mem profile 05_domain_expertise`.
4. **A blank beats a wrong fill.** When a claim cannot be verified from cards,
   recommend an explicit `[?]` placeholder instead of inventing venue, year,
   task, or metric — hallucinated values accumulate across refinement cycles.

## Output — single table, no narrative

| Section | Claim in artifact | Source (file:line or section) | Match (✅/🟡/❌) | Source type | Severity (🔴/🟡/🟢) |
|---|---|---|---|---|---|

Cover roughly the 30 most material claims; prioritize Tier 1 papers and
user-named models. For every failed or caution result, add an inline
`<!-- memo: [FACT] section X — claim Y conflicts with source Z -->` memo in
the artifact's language. A calling orchestrator may reuse the classification
table above as a detector without dispatching this unit when its own contract
says so.

Return per `_shared/dual-io.md`; verdict semantics: `no-issues`,
`memos-added` (count), `conflicts-found` (count).

## Memory

Per `_shared/memory-flow.md`. Retention targets: common false-positive
patterns (name-only matches that look verbatim); project-specific circular-ref
structures; domain-specific conflict-pair dictionary additions.
