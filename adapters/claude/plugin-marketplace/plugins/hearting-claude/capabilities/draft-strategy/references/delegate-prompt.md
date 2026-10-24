## Dispatch the research/research-survey Unit
Dispatch the `research/research-survey` unit with the following prompt:

````
Document strategy mode. Create an initial {mode} strategy document.

Mode: {mode}
Task: {task description}
Date: {YYYY-MM-DD}
Pre-discovered input paths (from autopilot-draft): {inputs_paths_list}
Analysis directory: {output_dir}/analysis/
Artifact language: {selected target or audience language}
Save canonical strategy to: {output_dir}/strategy/strategy.md
Target venues (for academic modes): NeurIPS, ICML, ICLR, ICASSP, Interspeech, IEEE/ACM T-ASLP

## Inputs
1. Read all analysis files in the analysis directory (material_index.md, reviewer_analysis.md or ref_analysis.md)
2. Read pre-analyzed materials from the discovered input paths (each path is a `analysis_project/{paper,doc}/{name}/` or `research/{topic}/` artifact — use the structured analysis files there, NOT external raw PDFs)
3. Consider venue-specific conventions and expectations (for academic modes: rebuttal/paper/review) or domain best practices and industry standards (for professional modes: report/proposal/presentation)

## Paragraph Cohesion Pre-Check (mandatory before every paste-ready block — **all modes**)

This rule applies to **every doc-strategy mode** (rebuttal / paper / review / report / proposal / presentation). Before writing any paste-ready block (LaTeX / markdown / slide bullet / table-row), run this **4-step self-check on the target paragraph as a whole**:

1. **Is the substance already stated?** — read every existing sentence in the target paragraph. If the same content (terminology, framing, claim, cross-ref) is already there, **do NOT add a separate sentence**; route through in-line edit instead.
2. **What is the paragraph axis?** — most well-formed paragraphs follow a clear axis (e.g., *motivation → design → formalization*, *claim → evidence → caveat*, *context → contribution → comparison*). Verify the new sentence does not break that transition. Insertions immediately before/after equation blocks, figure cascades, or table introductions require extra care.
3. **Cross-section redundancy** — if another paragraph (§-level / slide-level) already covers the substance, **DROP** or compress to a minimal cross-ref. Same substance must not be repeated at multiple canonical sites.
4. **Choose edit type** — combine results of 1-3 into one of four classifications, ordered by cohesion strength:
   - **EDIT (in-line parenthetical / word insertion / reword)** — terminology, cross-ref, or alternative folds naturally into an existing sentence. **Highest cohesion; prefer this whenever possible**.
   - **REPLACE** — rewrite an existing sentence to absorb the new substance, preserving the narrative arc.
   - **INSERT (new sentence)** — only when the paragraph has zero overlapping content AND a natural slot that does not break the axis exists.
   - **DROP** — another paragraph/section already states it → delete from the new mutation list.

**Anti-patterns (forbidden — auto-reject the mutation)**:
- *Mechanical "INSERT after sentence X"* without analyzing the paragraph flow — especially right before equation blocks, figure cascades, or table introductions, which breaks the narrative.
- *BEFORE / AFTER diffs where AFTER is longer than BEFORE* due to cross-ref bloat or restated substance — contradicts the compression intent.
- *Same substance repeated at §-level* (one paragraph states X, another paragraph re-states X via cross-ref) — keep exactly one canonical site; others must use cross-ref or DROP.
- *Trailing sentence appended after a "design choice" sentence that already names the choice* — the new content overlaps with the design-choice sentence and should be folded in via EDIT, not appended.

**Why**: every paste-ready mutation must be designed against the target paragraph's full cohesion/coherence, not in isolation; this 4-step pre-check operationalizes that judgment. Provenance (the dated camera-ready incident and the user's verbatim feedback) is held by the memory refs in the `mem profile 02_paper_writing_style` header (`[[feedback-paragraph-cohesion-pre-check]]`), not restated here.

## Mode Mapping — Alignment with autopilot-draft's Three Modes

Map autopilot-draft's three form-first modes to this sub-skill's six strategy templates:

| autopilot-draft mode | Example task-description terms (document intent) | Sub-skill mode label | Strategy template heading |
|---|---|---|---|
| `paper` | No intent branch | `paper` | `### If mode = paper` |
| `presentation` | No intent branch | `presentation` | `### If mode = presentation` |
| `doc` | rebuttal · 응답 · OpenReview · reviewer · 반박 | `rebuttal` | `### If mode = rebuttal` |
| `doc` | peer review · 심사 · 검토 의견 | `review` | `### If mode = review` |
| `doc` | 보고서 · report · 진행 · 결과 · status · 중간보고 | `report` | `### If mode = report` |
| `doc` | 제안서 · proposal · grant · RFP | `proposal` | `### If mode = proposal` |
| `doc` | Other intent, such as memo · blog · 일반 prose | `report` (default fallback) | `### If mode = report` |

During autopilot-draft Pre-flight Step 2 Input Discovery, infer document intent semantically from the task description, using the multilingual terms above only as examples. Convert that intent directly into one of the six sub-skill mode labels and pass it as the first argument. Do not pass autopilot-draft's raw `--mode doc` after conversion.

For direct `/draft-strategy` invocation, the user supplies one of the six modes as the first argument. When autopilot-draft cannot infer a clear document intent, default to `report`.

## Mode-Specific Instructions

### If mode = rebuttal
```markdown
---
type: rebuttal
venue: {venue if known, or "TBD"}
status: draft
date: {YYYY-MM-DD}
---

# Rebuttal Strategy: {paper title}

## 1. Meta-Review Summary
- Overall reviewer consensus (positive/mixed/negative), common themes
- Key strengths acknowledged (leverage these), critical weaknesses (prioritize these)
- Estimated acceptance likelihood and what would tip the balance

## 2. Response Priority Matrix
| Priority | Reviewer | Point | Severity | Category | Strategy |
|---|---|---|---|---|---|

Categories: Methodology, Experiments, Writing, Novelty, Comparison, Theory, Reproducibility
Strategies: Defend, Acknowledge+Mitigate, Partial-Agree+Extend, Concede+Revise, Clarify

## 3. Reviewer-by-Reviewer Detailed Strategy

### Reviewer {N} — Score: {X}, Confidence: {Y}
#### Sentiment: {positive/neutral/negative/hostile}
#### Key Leverage Points
#### Point-by-Point Response

##### R{N}.{M}: {summary of reviewer point}
- **Severity**: Critical / Major / Minor / Nitpick
- **Category**: {category}
- **Strategy**: {strategy type}
- **Core Argument**: {1-2 sentence rebuttal logic}
- **Supporting Evidence**: {what data/results/citations support the rebuttal}
- **Tone**: {defensive → neutral → concessive spectrum}
- **Draft Response Outline**:
  - Opening: {acknowledgment or framing}
  - Body: {main argument}
  - Evidence: {specific results to cite}
  - Closing: {what was done / will be done}

## 4. Additional Experiments Plan
| Experiment | Purpose | Addresses Points | Feasibility (days) | Priority |
|---|---|---|---|---|

## 5. Paper Revision Plan
| Section | Proposed Change | Addresses | Priority |
|---|---|---|---|

## 6. Response Tone & Style Guidelines
- Overall tone, per-reviewer adjustments, phrases to use/avoid, word count budget

## 7. Risk Assessment
- Weakest defense points, potential follow-up questions, contingency if experiments fail, AC perspective
```

### If mode = paper
```markdown
---
type: paper / venue: {target venue} / status: draft / date: {YYYY-MM-DD}
---
# Paper Writing Strategy: {topic}
## 1. Positioning Analysis — research gap, how this fills it, differentiation from closest work
## 2. Contribution Statement — primary (1 sentence), secondary, scope limitations
## 3. Paper Outline — section-by-section with key points and target page allocation
## 4. Key Arguments & Evidence — per section: argument + supporting evidence
## 5. Related Work Strategy — framing prior work to highlight novelty
## 6. Experiment Design Strategy — experiments proving claims, baselines, ablations
## 7. Risk & Weakness Mitigation — anticipated reviewer concerns and preemptive responses
## 8. Venue-Specific Considerations — page limits, formatting, reviewer expectations
```

#### Natural-integration rule for paper-body mutations (camera-ready / major revision)

> **First apply the top-level [Paragraph Cohesion Pre-Check](#paragraph-cohesion-pre-check-mandatory-before-every-paste-ready-block--all-modes) (4-step) to choose between EDIT / REPLACE / INSERT / DROP.** The paper-body extension below adds rebuttal-material-specific gates on top.

When converting **reviewer concerns / rebuttal materials → paper-body mutations**, apply this **single gating question** before writing any paste-ready block:

> **"Can this reviewer point be naturally integrated into the existing paragraph flow as a 1-2 sentence inline rewrite?"**

- **YES → inline rewrite mutation** (M15-style — good pattern):
  - Label the subsection heading + add 1-2 opening sentences that introduce the framing.
  - Reword the existing paragraph's first sentence to remove redundancy with the new opening (no orphan additions).
  - Connect Figure references in cascade (overall → architectural → unit-module — single zoom-in path).
  - Defer experimental numbers / hyperparameters / verbatim table citations to body paragraphs or Appendix — never inject them into opening / intro paragraphs.
- **NO → drop (or defer to Appendix)** (M11-style — bad pattern, must reject):
  - Anything that exists only as a **rebuttal-format artifact** — model-by-model comparison tables, structured Q&A blocks, point-by-point response paragraphs, "we acknowledge X, Y, Z" enumerations — does NOT belong as a paper-body mutation. Even if the reviewer "strongly recommended integration," a rebuttal-format block pasted verbatim into a camera-ready section will read as awkward and out-of-flow.
  - Mark the mutation as **dropped** in the strategy, with a one-line reason ("rebuttal-format — not naturally integrable into body flow"). If a fragment of the rebuttal material *can* be salvaged as inline body wording, extract only that fragment as an inline rewrite; do NOT preserve the surrounding structure.

**Hard-fail check** — when drafting mutation entries, an entry MUST be rejected if any of the following holds (these are signals that the mutation is mechanically copying a rebuttal artifact rather than integrating naturally):
1. The paste-ready block is a **standalone `\begin{table}` or `\begin{itemize}` enumeration** sourced from rebuttal materials, with no embedding paragraph rewriting around it.
2. The paste-ready block contains verbatim **experiment numbers** ($x.xx \to y.yy$ migrations, hyperparameter listings, dataset enumerations) inside what should be an introductory / framing paragraph.
3. The paste-ready block is **a new `\paragraph{...}` INSERT** that the existing surrounding text does not bridge to — the surrounding paragraphs would still read identically with or without the inserted block, indicating it sits in isolation rather than weaving into the flow.

**Why**: mechanically converting every reviewer concern into a paper-body mutation produces rebuttal-format inserts that read as out-of-flow; the natural-integration rule above operationalizes the user's standing judgment against that. Provenance (the dated camera-ready episode and the user's verbatim rejection) is held by the memory refs in the `mem profile 02_paper_writing_style` header (`[[feedback-paper-body-rewrite-pattern]]`), not restated here.

#### Paste-Ready Cheatsheet Format — Separate User and Tracking Surfaces

When paper mode produces a card-based cheatsheet for camera-ready, major revision, or explicit `subtype: camera-ready-paste-ready`, the post-approval [paste-ready cheatsheet contract](../../autopilot-draft/references/convention-paper.md#paste-ready-cheatsheet-contract) is the single authority for draft-body format. Design the mutation list, paste order, and branch points on the assumption that the later draft will follow that contract.

Keep the strategy's mutation list card-based: one entry contains one location line, one paste-ready LaTeX line or reference, and a short reason. Group tracking metadata such as reviewer mapping, dependency tables, and wording invariants into separate sections such as §Reviewer mapping and §Dependency map, away from the body flow. During draft generation, move those tracking sections into `_internal/draft_meta.md`; do not place them beside body entries.

### If mode = review
```markdown
---
type: review / status: draft / date: {YYYY-MM-DD}
---
# Paper/Document Review: {title}
## Summary / ## Strengths / ## Weaknesses / ## Questions for Authors
## Missing References / ## Minor Issues / ## Overall Assessment & Recommendation / ## Confidence
```

### If mode = report
```markdown
---
type: report / status: draft / date: {YYYY-MM-DD} / tone: {administrative | default}
---
# Report Strategy: {topic}
## 1. Objective & Scope — what the report aims to establish, audience, constraints
## 2. Key Findings Summary — top 3-5 findings with evidence strength rating
## 3. Analysis Framework — methodology for organizing and presenting findings
## 4. Section Plan — section-by-section outline with key points and evidence mapping
## 5. Data & Evidence Inventory — what data supports each finding, gaps to note
## 6. Recommendations Strategy — how to frame actionable recommendations
## 7. Risk & Limitations — caveats, data quality issues, scope limitations
```

### If mode = proposal
```markdown
---
type: proposal / status: draft / date: {YYYY-MM-DD} / tone: {administrative | default}
---
# Proposal Strategy: {topic}
## 1. Problem Statement — clear articulation of the problem and its significance
## 2. Prior Art & Context — what exists, why it's insufficient
## 3. Proposed Approach — technical plan, innovation, differentiation
## 4. Feasibility Evidence — preliminary results, related successes, team capability
## 5. Work Plan — phases, milestones, deliverables, timeline
## 6. Resource Plan — team, budget, equipment, data needs
## 7. Expected Impact — outcomes, metrics for success, broader implications
## 8. Risk Mitigation — key risks and contingency plans
```

### If mode = presentation
> Note: This strategy document serves as the direct slide production guide. No separate draft is generated for presentation mode.

```markdown
---
type: presentation / status: draft / date: {YYYY-MM-DD} / tone: {administrative | default}
---
# Presentation Strategy: {topic}
## 1. Audience Analysis — who they are, what they know, what they need
## 2. Core Message — one sentence the audience should remember (**administrative tone: replace with "Presentation purpose — what the speaker is reporting"; no marketing one-liner**)
## 3. Story Arc — narrative structure (hook → problem → solution → evidence → call-to-action) (**administrative tone: replace with simple "Information flow — what is presented in what order"; NO Hook / NO Call-to-Action**)
## 4. Slide Outline — slide-by-slide plan with key visuals and talking points
## 5. Key Visuals Strategy — diagrams, charts, demos to include
## 6. Anticipated Questions — likely Q&A and prepared responses
## 7. Time Allocation — per-section timing budget
## 8. Delivery Notes — tone, pacing, emphasis points
```

### Tone Auto-Detection (modes: report / proposal / presentation) — **FIRST step before drafting any of the three above templates**

Infer audience tone from the task description and set `tone:` in the strategy frontmatter. **The detected tone propagates to draft generation in autopilot-draft Step 4** — the draft must respect the same constraints.

| Tone | Detection signals | Style constraints |
|---|---|---|
| **administrative** | The task semantically indicates an administrative audience. Multilingual examples include "위원회 / 심의 / 사전심의 / 산학협력단 / **정부** / 정부 기관 / 규제 / 검토 요청 / 보고 / 평가단 / 이사회 / 감사 / 사업 보고 / 사후 보고" and "committee / regulatory / **government** / agency review / compliance / board review / pre-screening / audit / status report"; these are examples, not fixed trigger phrases. The speaker is a student or researcher reporting **upward** to decision-makers, not pitching to peers or selling. The goal is information delivery and a review request, not persuasion, sales, or an investor pitch. | **Plain factual delivery, objective information.** AVOID: marketing superlatives ("genuinely novel", "sole occupied axis", "global rights asset", "world-first"), "X strengths summary" framing, "core message" / "Hook → Call-to-Action" arc, heroic asks ("Approve to secure as global asset"), decision-options box (approve/conditional/hold), animated narrative voice. PREFER: simple fact lists, status updates, calm review request ("검토 부탁드립니다" / "kindly request the committee's review"). Speaker stance: **neutral reporter, not advocate**. |
| **default** | Otherwise — academic conference talk, internal R&D briefing, industry pitch, sales/marketing presentation, investor IR | Existing pitch-deck patterns apply (Hook, Core Message, Story Arc, Call-to-Action, persuasive framing). |

**Why this matters**: Korean administrative presentations (산학협력단 사전심의, 정부 위원회 보고, 규제 기관 사후 보고 등) follow a fundamentally different convention from English-language pitch decks. Without this guard, default LLM patterns lean toward US/English pitch-deck superlatives ("compelling contribution", "single-axis advantage", "secured as a global asset") that are uncomfortable for student presenters and inappropriate for the audience. The same applies to administrative reports/proposals submitted to government agencies or institutional review boards.

### Slide Format Conventions (mode: presentation) — **mandatory in slide outline (Section 4)**

When the strategy includes a slide-by-slide outline (presentation mode Section 4), each slide entry MUST follow these formatting principles. **The same conventions propagate to draft generation in autopilot-draft Step 4.**

**1. Chapter visualization in slide headers**
- Every body-slide title prefixed with `[Ch.N 챕터명]` (Korean) or `[Ch.N Chapter-name]` (English)
- Chapter-transition slides marked with `[Ch.N 챕터명 — 시작]` / `[~ start]`
- Each slide includes a `**챕터**: N. 챕터명 (M장 중 K번째)` meta line below the title — chapter-transition slides add "**챕터 전환**" annotation
- Visual placeholder's first line: `- **상단 헤더 띠**: "N. 챕터명"` (per format-ref Korean industry-academia standard deck) — chapter-transition slides add "Ch.X와 색상/strength를 다르게 — 챕터 전환 시각 신호"

**2. Visual placeholder concreteness**
- AVOID vague terms: "X 카드", "Y 도식", "적절한 시각화", "comparison chart"
- PREFER: (a) diagram type + (b) component list + (c) color/layout hints
- Example: ❌ "학회 위상 카드" → ✅ "NeurIPS / ICLR / ICML 3-row table (h5-index 컬럼 + acceptance-rate 컬럼)"

**3. Table column header clarity**
- AVOID abbreviations or ambiguous headers (e.g., "비교 1위" — comparison with what?)
- PREFER: full Korean/English noun phrases with clear semantic units; add a 1-line column-meaning footnote above the table if helpful

**4. Foreign-language quote → Korean keyword gloss** (mandatory for non-AI audiences)
- Whenever a slide contains an English quote (paper review, citation, technical term), add a Korean appeal-commentary box directly below:
  ```
  > "English quote..."
  > — Source

  📌 **핵심 키워드 — "X"**: 한국어 풀이 1문장
  ```

**5. Speaker notes default = empty**
- The strategy outline and the initial draft leave speaker notes empty by default
- Only generate speaker notes when the user explicitly requests as a separate post-polish step
- Reason: speaker notes drift with slide-content edits; auto-fill produces wasted regeneration cost in iterative refinement

**6. No body-bullet ↔ visual redundancy**
- The same fact should NOT appear both in body bullets AND in the visual placeholder
- Body bullets = "what the speaker says"; visual = "what the audience sees at-a-glance"
- If both express the same fact, simplify one of the two

**7. Slide-number consistency on insertion/deletion**
- When inserting/removing a slide, update ALL of the following in the same edit pass:
  - (a) All subsequent slide numbers (`Slide N+1`, `Slide N+2`, ...)
  - (b) Contents slide's chapter slide-counts ("Ch.N (M장)")
  - (c) Changelog entry inside the frontmatter `changelog:` array (per `draft-refine` convention — never a top-of-file HTML comment, which breaks markdown preview when frontmatter is present)
  - (d) Time-budget line in the file's top guide
  - (e) Cross-references in other slides ("Slide M의 ...")
  - (f) Chapter meta lines ("M장 중 K번째")

## Quality Requirements
Every reviewer point must appear in rebuttal strategy (missing a point is a critical error). Severity classification must be justified. All citations must reference actual materials in the discovered input paths (analysis_project/{paper,doc}/, research/{topic}/) — do NOT fabricate. Strategy must be actionable with specific plans, not vague advice. For academic modes (rebuttal/paper/review): apply venue-specific norms (e.g., NeurIPS rebuttal length limits, ICASSP culture). For professional modes (report/proposal/presentation): apply industry best practices relevant to the domain.

Write the strategy file directly. Return ONLY the file path and a 3-5 line summary of the strategy. Do NOT return the strategy content itself.
````

The agent writes the strategy file directly; the orchestrator only receives paths and a summary.
