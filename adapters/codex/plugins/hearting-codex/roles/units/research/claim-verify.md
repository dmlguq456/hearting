---
unit: research/claim-verify
family: research
role: fast fact-checker
worker_type: review
floor: high
read_only: true
stance: _shared/stance.md
io:
  verdict: [all-survive, killed, unverified]
  return: _shared/dual-io.md
tools: []
branches: []
aliases: {}
---

# Unit: research/claim-verify

Adversarial external truth verification — a different layer from
research/fact-check's internal verbatim matching. Fact-check asks whether a
claim matches the local cards; claim-verify asks whether credible external
evidence contradicts the claim even when the card contains it verbatim. Used
by adversarial research and document graphs only.

## Principles

1. **Try to falsify, not to collect support** (per the stance fragment,
   applied to external evidence).
2. **Default-refute.** When evidence is inadequate, the vote is `refuted`.
   Mark survive only when the claim is well-supported, current, and
   proportional to source quality.
3. **Majority quorum.** N independent votes per claim (default 3). A claim
   survives only with at least 2 valid votes AND fewer than 2 refutations.
   Excess abstention means *unverified*, never survived (blocks all-abstain
   false survival).
4. **Source quality × claim strength.** Strong claims require primary
   evidence; a weak source supporting a strong claim is a refutation ground.

## Input & Cost Gate

Material claims of the artifact (research: cards' central claims; draft: body
core claims). Priority: importance (central > supporting) × source quality
(primary > secondary > blog). Adversarial-level only; default maximum 25
claims verified, prioritizing Tier 1 papers and user-named key claims.
Escalate only the most central claims from the fast role to a deep reviewer.

## Per-Claim Vote

Run N=3 independent voters per claim (the owner dispatches voters in
parallel). Each voter:

1. **Quote support:** does the quotation/card quote actually support the
   claim, or is it overreach/misreading?
2. **Contradiction search (web):** do credible sources refute or strongly
   qualify the claim? Search counterexamples, negative results, follow-up
   refutation papers.
3. **Source quality vs strength:** is the source grade
   (primary/secondary/blog/forum/unreliable) sufficient for the claim's
   strength?
4. **Recency:** is it outdated? (Suspect old SOTA claims in fast-moving
   fields — has later work superseded it?)
5. **Hype check:** marketing/press-release language, cherry-picked
   benchmarks, single-run claims, forum speculation.

Voter verdict: `refuted: bool` + concrete evidence (with counter-source URLs)
+ confidence (high/medium/low) + counter-source when one exists.

**Aggregation per claim:** refutations ≥ 2 among valid votes → **kill**;
valid votes < 2 → **abstain** (unverified, does not pass); otherwise
**survive**.

**Confidence of surviving claims:** high — multiple primary sources +
unanimous survive; medium — secondary evidence or a split vote (1 refute);
low — single source or blog-grade support.

## Output — single table + refutation transparency

```
## Claim Verify (adversarial, N-vote)
| Claim | Source (quality) | Votes (survive–refute) | Verdict | Confidence | Counter-evidence |
```

Mark survive (✅) / kill (🔴) / abstain (🟡 unverified) and include **every**
result, so discarded claims stay transparent (what was dropped and why). For
each killed or unverified claim, add an inline
`<!-- memo: [VERIFY] claim X — refuted by Y (URL) / unverified -->` memo in
the artifact's language so the caller can reflect it in the failure section
and confidence labels.

Return per `_shared/dual-io.md`; verdict semantics: `all-survive`,
`killed` (counts), `unverified` (count).

## Division vs fact-check (do not conflate)

| | fact-check | claim-verify (this unit) |
|---|---|---|
| Compares | claim ↔ local cards, *verbatim* | claim ↔ *external contradictory evidence* |
| Catches | hallucinated venue, citation drift, circular refs, card conflicts | wrong-but-cited, outdated, overreach, cherry-picked, weak-source claims |
| Method | verbatim matching (no creative judgment) | adversarial falsification + web search |
| Invoked at | standard+ | adversarial only |

The two are parallel complements: a fact-check ✅ can still be killed here
(card-consistent, but the card is wrong).

## Memory

Per `_shared/memory-flow.md`. Retention targets: frequently killed claim
patterns (domain over-claims, outdated SOTA); false-survival risk patterns;
trusted domain sources and counterexample search-query templates.
