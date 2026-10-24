# Mode `paper`

Analyze academic reference PDFs and produce per-paper analysis plus an integrated overview.

## Dispatch the Survey Unit

Dispatch the `research/research-survey` unit with this prompt:

```text
Analyze the target paper(s) and generate documentation. FIRST determine the purpose, because paper-mode analysis differs by purpose:

- (A) reference-survey: study external papers for citation and grounding. Follow §§1–6 for contributions, architecture, and paper-to-code mapping.
- (B) own-paper review: analyze this project's in-progress paper, usually main.tex, for camera-ready or revision work. Section 0 is the main output; §§1–6 are secondary and may be skipped. If the target is one in-progress main.tex at project root, select B automatically. Ask the user only when purpose remains ambiguous.

Scope: {$ARGUMENTS or "all"}
Date: {YYYY-MM-DD}

## Inputs
- Reference PDFs: search the current directory and common subdirectories such as `papers/`, `refs/`, and `pdfs/` for `*.pdf`. If `<scope>` is a folder path or keyword, use it; otherwise scan project root and one level below it.
- Existing paper docs: <artifact-root>/analysis_project/paper/*.md
- Existing code docs: <artifact-root>/analysis_project/code/*.md
- Source code: project source directories such as `models/`, `src/`, and `lib/`

## Procedure

### 0. Purpose B: complete content analysis of the owned paper [REQUIRED, PRIMARY OUTPUT]

When the target is the project's own in-progress `main.tex`, read the entire paper before reviewing it and write `analysis_project/paper/00_self_paper_analysis.md`. This is the primary source that autopilot-draft, the survey-unit review, and autopilot-apply must understand before acting. A shallow structure map can cause downstream agents to misidentify tables and figures, report wrong numbers, or miss duplicate labels. The 2026-05-27 incident misread `tab:VCTK_ND`, which defines evaluation-set generation, as dedicated SR training because only a structure map existed.

Do not stop at a section map or page count. Analyze content and reasoning:

1. **Section-level logic and claim-to-evidence flow:** problem framing, method design intent, and each evaluation's purpose, setup, and conclusion. Explain why the experiments appear in this order and what each one demonstrates.
2. **Validity and clarity of contributions:** determine whether body and experiments support each claimed contribution; identify exaggeration, duplication, and unsupported claims.
3. **Consistency of result interpretation:** compare prose with actual table and figure values; flag statements such as “outperforms” when any relevant cell contradicts them.
4. **Table and figure inventory:** record each item's purpose, label, referring section, and key content. Organize by reference flow and meaning, not float position.
5. **Label, number, and reference consistency:** mechanically extract PDF numbering from `main.aux` `\newlabel`, duplicate labels from `main.log` `multiply defined`, and undefined `\ref` or `\cite`. Never infer these values.
6. **Terminology and prose consistency:** abbreviation definition sites, naming drift, and clear grammatical defects.

This analysis lets the survey unit review the paper from an informed content model. Skip Section 0 for purpose A.

### 1. Read all reference PDFs
Extract each paper's core contributions, architecture, key equations, experimental findings, design constraints, and ablation results.

### 2. Read existing analysis_project/paper/ files
Identify what exists and what needs updating.

### 3. Read code docs and source
Read analysis_project/code/ and relevant source files to verify paper-to-code alignment.

### 4. Generate or update per-paper summaries
For each paper, create or update a file under `<artifact-root>/analysis_project/paper/`. Include title, venue, year, contribution, architecture, design decisions and reasons, important equations, constraining ablations, and paper-to-code mapping.

### 5. Generate or update 00_overview_and_constraints.md
This is the primary reference for `research/plan-review`.

```markdown
# Project Overview and Design Constraints

## Paper Evolution
## Paper → Code Variant Mapping
## Core Design Principles
(each principle: what it is, why it matters with paper evidence, how it maps to code)
## Architecture Constraints
Hard Constraints (must NOT be changed):
(project-specific list)
## Terminology Mapping
## Cross-Paper Relationships
```

### 6. Verify paper-to-code alignment
For each major component, verify alignment and document discrepancies or code-only features.

Write in the selected target artifact language. Preserve code identifiers and source quotations when translation would reduce precision. Return ONLY the created or updated paths and a brief summary.
```

## Post-Analysis

After the survey unit returns:

1. Relay the file paths and summary to the user.
2. Recommend reading `00_overview_and_constraints.md` first.
