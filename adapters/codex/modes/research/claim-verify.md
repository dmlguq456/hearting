# Codex Research Claim Verify Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/research/claim-verify.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info research/claim-verify`.
4. Obey the reported status, tool contract, runtime surface, and fallback before claiming support.

## Codex Runtime Mapping

- Status: `tool-contract`
- Realization: `portable-with-tool-contract`
- Tool Contract: `external-claim-verification`
- Tool Contract Check: `adapters/codex/bin/preflight.sh claim-verify --check <claim>`
- Runtime Surface: `adapter-owned-claim-verify`
- Fallback: `satisfy-tool-contract-or-report-unavailable`
- Requirement: run the adapter-owned external claim verification launcher with a configured provider, or report unavailable
- Note: Codex may use the persona only after satisfying or explicitly downgrading the named tool contract.

## Use

- Use Codex file, terminal, approval, sandbox, hook, and skill surfaces.
- For `tool-contract` modes, run the named contract check before claiming the tool-backed result.
- If a required local provider or executable is unavailable, report the unavailable contract instead of silently downgrading.
- Treat `adapters/codex/modes/research/claim-verify.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/research/claim-verify.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

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
by the route nodes that carry this unit (`autopilot-research:claim-verify`,
`autopilot-draft:fact-verify`).

## Principles

1. **Try to falsify, not to collect support** (per the stance fragment,
   applied to external evidence).
2. **Default-refute.** When evidence is inadequate, the vote is `refuted`.
   Mark survive only when the claim is well-supported, current, and
   proportional to source quality.
3. **Votes come from the verifiers the node launched.** W is the number of
   peer verifiers the route actually ran on this node: 1 below the node's
   parallel-group tier, the group's peer-leg count where it widens (2 in the
   current recipes). Each verifier casts one vote per claim and never spawns
   more voters; an auxiliary leg is advisory, not a vote. You settle your
   own result from your own votes; the route's join of its peer legs
   aggregates W≥2 (below), so you never need W and never wait for voters the
   node did not launch. A claim no verifier could judge is *unverified*,
   never survived.
4. **Source quality × claim strength.** Strong claims require primary
   evidence; a weak source supporting a strong claim is a refutation ground.

## Input & Cost Gate

Material claims of the artifact (research: cards' central claims; draft: body
core claims). Priority: importance (central > supporting) × source quality
(primary > secondary > blog). Verify **every** material claim, in batches of
at most 25 taken in priority order (Tier 1 papers and user-named key claims
first). Write each batch's rows to the verdict file before starting the next,
so the file always shows which claims are judged and which remain. The batch
size bounds one pass, not coverage: never stop at 25 while claims remain.

**Resume.** When your output location already holds a verdict file for the
same artifact (an earlier attempt of this node, including one reached through
a continuation), keep its judged rows whose claim text is unchanged and
verify only the claims still unverified or new. A claim whose text changed is
judged again.

## Per-Claim Vote

Cast your own vote on each claim. Per claim:

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

Vote: `refuted: bool` + concrete evidence (with counter-source URLs)
+ confidence (high/medium/low) + counter-source when one exists. Inadequate
evidence is a refutation (default-refute). A claim you could not judge at all
(the source was unreachable, the batch was not reached) gets no vote.

**Aggregation per claim over the W launched votes:** refutations ≥ ⌈W/2⌉
→ **kill**; valid votes < ⌊W/2⌋+1 → **abstain** (unverified, does not
pass); otherwise **survive**. W=1: refuted → kill, survive → survive, no
vote → unverified. W=2: any refutation kills; survival needs both votes.
W=3 (only where three votes are actually cast; no current recipe does):
two refutations kill, two valid votes are required. Each verifier writes its
own rows and settles its own leg by its own vote; with W=2 the route
continues only after both peer legs pass, which is exactly the W=2 rule. Never
report a quorum the node cannot reach — a lone verifier's survive is a
survive, not an abstention for missing voters.

**Confidence of surviving claims:** high — multiple primary sources +
unanimous survive; medium — secondary evidence or a split vote; low — single
source or blog-grade support.

## Output — single table + refutation transparency

```
## Claim Verify (adversarial, this verifier's votes)
| Claim | Source (quality) | Votes (survive–refute) | Verdict | Confidence | Counter-evidence |
```

Mark survive (✅) / kill (🔴) / abstain (🟡 unverified) and include **every**
result, so discarded claims stay transparent (what was dropped and why). For
each killed or unverified claim, add an inline
`<!-- memo: [VERIFY] claim X — refuted by Y (URL) / unverified -->` memo in
the artifact's language so the caller can reflect it in the failure section
and confidence labels.

Return per `_shared/dual-io.md`; verdict semantics: `all-survive`,
`killed` (counts), `unverified` (count). Every claim judged and none killed is
`all-survive` (handoff PASS); any kill is `killed` (handoff FAIL, the caller
revises); claims left unjudged with none killed is `unverified` (handoff
BLOCKED, no judgment reached on those claims) — rerunning the node resumes
from the verdict file as above.

## Division vs fact-check (do not conflate)

| | fact-check | claim-verify (this unit) |
|---|---|---|
| Compares | claim ↔ local cards, *verbatim* | claim ↔ *external contradictory evidence* |
| Catches | hallucinated venue, citation drift, circular refs, card conflicts | wrong-but-cited, outdated, overreach, cherry-picked, weak-source claims |
| Method | verbatim matching (no creative judgment) | adversarial falsification + web search |
| Invoked at | standard+ | the route nodes that carry this unit |

The two are parallel complements: a fact-check ✅ can still be killed here
(card-consistent, but the card is wrong).

## Memory

Per `_shared/memory-flow.md`. Retention targets: frequently killed claim
patterns (domain over-claims, outdated SOTA); false-survival risk patterns;
trusted domain sources and counterexample search-query templates.
