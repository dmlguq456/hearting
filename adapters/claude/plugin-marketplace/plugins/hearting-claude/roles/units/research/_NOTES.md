# research family — authoring residue & merge decisions (for review)

Sources merged per unit: `roles/modes/research/<m>.md` (EN),
`adapters/claude/agent-modes/research/<m>.md` (KO),
`adapters/claude/agents/research-team.md` (team file). Items below are either
(a) load-bearing content deliberately NOT placed in a unit body, or (b)
semantic EN/KO divergences resolved during merge. Nothing was dropped
silently.

## Not placed (needs a home or explicit retirement)

1. **Mode-selection trigger table + caller step lists**
   (`adapters/claude/agents/research-team.md:37-43`; also
   `adapters/claude/agent-modes/research/fact-check.md:38-44` "호출자 매핑").
   Concrete call sites ("fact-check: autopilot-draft Steps 3 & 5,
   autopilot-research Step 4b, autopilot-refine Stage B.5, post-strategy/
   post-refine"; "claim-verify: adversarial autopilot-research Step 4b,
   adversarial draft/refine") are ROUTING content — owned by
   topologies/recipes (workstream B), not unit bodies. Units keep only the
   division-of-labor semantics. B must confirm these call sites are encoded in
   recipes; the fact-check note that autopilot-refine Stage B.5 is an
   orchestrator-side detector reusing the classification table without
   dispatch WAS kept in the unit body.
2. **Cross-project user-profile loading** (`research-team.md:48-55`) was
   team-wide; placed only in plan-review and research-survey. Not added to
   fact-check (near-zero floor, verbatim-only) or claim-verify (vote workers).
   Confirm this scoping.
3. **Team Decision Rules** (`research-team.md:64-72`) placed only in
   research-survey (the maker unit); review units annotate rather than
   decide. "Align ambiguous research details with the source paper's method"
   is preserved there.
4. **Team Language Rule** (`research-team.md:15-21`) placed in research-survey
   only; the review units carry their own memo-language rules (artifact's
   language). Confirm no other consumer needed the full rule.
5. **Memory provenance refs**: KO fact-check cites memory file
   `feedback_factcheck_external_reverify.md`
   (`agent-modes/research/fact-check.md:21`) and the dated incident
   "2026-05-12 TF-Locoformer: card `IWAENC 2024` vs claim `IS 2024`, error
   survived two layers" (`fact-check.md:22`). Unit body keeps the generalized
   incident; date/paper/file pointers preserved here only.
6. **claim-verify design provenance** (`agent-modes/research/claim-verify.md:7`):
   ported from the built-in deep-research 3-vote adversarial verify; RE doc at
   `<nas-archive>/claude-meta-spec/reverse_engineering/deep-research.md`.
   External path, not restated in the portable unit.
7. **Team frontmatter runtime metadata** (`research-team.md:1-11`): tools
   list, adapter-configured model, color, `memory: project`. Harness runtime config
   dies with the team agent; the model literal is intentionally NOT carried
   (guard violation) — role names only.
8. **Verbatim strings abbreviated**: Google Scholar full User-Agent literal
   (`agent-modes/research/research-survey.md:26`) → "browser User-Agent";
   Korean degradation notices "레퍼런스 체이닝 미완료" / "코드 검색 미완료"
   (`research-survey.md:169-170`) → English canonical labels, localized at
   render time.

## EN/KO divergences resolved

9. **plan-review multi-axis trigger**: EN "selected by
   `intensity=thorough|adversarial`, optionally scaled by `--qa`" vs KO
   "called by `--qa thorough+`" — resolved to EN (more recent dispatch
   contract; KO reflects the retired native-team call path).
10. **research-survey access ladder rung 4**: EN "arXiv PDF" vs KO "arXiv
    abstract page" — merged as "abstract page or PDF" under the shared 60 s
    per-paper timeout.
11. **research-survey reading depth**: EN conflates recommendation grades with
    reading depth (">100 citations: must read"); KO separates reading depth
    (citations>10 → full read) from user-facing recommendation grades —
    resolved to KO's two-axis model (stricter, more precise), with EN's "Tier
    1 ≥ skim" correction kept.
12. **fact-check stance**: declared `stance: none` although it is a review
    unit — the verbatim-only contract ("no creative judgment") contradicts
    the stance fragment's construct-breaking-scenarios instruction. The
    not-proven ≠ pass principle survives locally as "a blank beats a wrong
    fill" + name-only-never-passes. Needs reviewer sign-off.
13. **read_only for review units**: plan-review/fact-check/claim-verify are
    `read_only: true` as NATURE despite writing `<!-- memo -->` annotations
    and review logs — annotation targets/log paths are node-granted
    write_scope, artifact substance is never altered. Flagging for validator
    semantics review (workstream B).
