# Prompt Template for the `research/research-survey` Unit Dispatch

Dispatch the `research/research-survey` unit with the prompt below. Substitute `{...}` variables, then use it verbatim; this is the actual worker prompt, not documentation.

```
Refine mode (versioned + ref-grounded). Update an existing document {doc_type} based on user memos and review feedback.

Canonical {doc_type} file (current/latest): {canonical_path}
Existing required companion files: {companion_paths or none}
Previous version archive (immutable, already created and verified by the route-bound snapshot helper):
- Modern: `{artifact_root}/_internal/versions/v{prev_version}/{relative-subdir}/{filename}` (where `{relative-subdir}` is `strategy/` or `draft/`)
- Legacy: `{path.parent}/{path.stem}_v{prev_version}.md` for each existing file, only if the artifact already used `_v{N}.md` siblings and lacks `_internal/`
Convention mode: {modern | legacy}
Next version: v{next_version}

## Memo Detection
Read the canonical {doc_type} and every existing required companion, then find all user memos. Memos can appear in any of these formats:
- `<!-- memo: ... -->` (standard memo tag)
- `<!-- ... -->` (HTML comment — treat any HTML comment as a user memo, EXCEPT a **legacy** `<!-- CHANGELOG (auto-managed by draft-refine ... -->` block at top-of-file. Such legacy blocks are NOT memos; they must be **migrated** into the frontmatter `changelog:` array and then deleted from the body (see "Changelog (frontmatter `changelog:` array)" below).
- `// ...` (inline comment)
- `[memo] ...` (bracketed annotation)
- `(**...**)` (parenthetical note)
- Any other text marked as a user annotation. Do NOT treat the document's original author-written prose as a memo.

## MANDATORY Ref-Grounding Per Memo (CRITICAL — quality requirement)

For EACH memo found, before applying any change:
1. **Identify the relevant source(s)** the memo pertains to:
   - Paper analyses (`<artifact-root>/analysis_project/paper/*.md`) — for citation, venue, score, NFE, RTF, dataset facts (single source of truth, produced by `/analyze-project --mode paper`)
   - Strategy document (`{artifact_root}/strategy/strategy.md`) — for narrative arc, slide outline alignment
   - Analysis files (`{artifact_root}/analysis/*.md`) — for audience, key messages, visual strategy
   - Original PDFs (in user's source folder if available) — only for nuanced claims requiring re-reading; paper analyses are preferred
2. **Re-read the identified source** before applying the change. Do not rely on the memo's claim alone.
3. **Compare memo claim vs source**:
   - If memo agrees with source → apply the change as memo suggests.
   - If memo CONFLICTS with source → **override the memo, keep the original draft text, and document the conflict in changelog**. Do NOT silently propagate user error.
   - If source is ambiguous → apply the change but flag in changelog with `[CAUTION: source ambiguous]`.
4. **Record source verified** in the changelog entry: `[verified cards/2020_Hu_DCCRN.md]` or `[strategy section 4 confirmed]`.

For draft refinement: also cross-check against the strategy document at `{artifact_root}/strategy/` to verify the draft faithfully reflects all strategy points.

## Output Versioning

1. **Write the new content to the canonical file and every existing required companion** (`{canonical_path}`, `{companion_paths}`) — these always represent the latest version.
2. The pre-edit snapshot is already written by `utilities/artifact-snapshot.py` during Pre-Refine setup — to either `_internal/versions/v{prev_version}/...` (modern) or `{file}_v{prev_version}.md` (legacy). Do not allocate, copy, or modify a snapshot at output time.
3. **Remove all memo comments** (HTML comments, `// ...`, `[memo] ...`, etc.) from the new version. EXCEPT preserve/update the frontmatter `changelog:` array (see below).

## Changelog (frontmatter `changelog:` array — NEVER an HTML comment)

The changelog is stored as a **YAML array** inside the file's frontmatter, NOT as a top-of-file `<!-- CHANGELOG -->` HTML comment.

**Why this form is mandatory** (do not regress to the HTML-comment form):
- A `<!-- ... -->` block placed above the frontmatter pushes `---` off line 1. Markdown previewers (VS Code, GitHub, Obsidian, Jupyter) require frontmatter to start at line 1; otherwise they render `---` as horizontal rules and YAML keys as plain prose, breaking preview.
- YAML arrays are structured data, parseable by tools (audit, downstream scripts).
- The frontmatter is hidden by previewers; the body renders cleanly.

### File-level invariant

The file MUST begin with `---` on line 1. Nothing — no HTML comment, no blank line, no prose — may precede the frontmatter open delimiter.

### Format

```yaml
---
{existing domain keys preserved verbatim: type, venue, status, date, tone, ...}
changelog:
  - version: v{next_version}
    date: "{YYYY-MM-DDTHH:MM}"
    applied: {N}
    overridden: {M}
    entries:
      - |
        [Slide N | Section X] [verified <source>]: <one-line description of change>
      - |
        [Slide N | Section X] [verified <source>]: <change>
      - |
        [Slide N | Section X] [OVERRIDDEN — memo conflicted with <source>]: <reason>
  - version: v{next_version - 1}
    date: "{YYYY-MM-DD or YYYY-MM-DDTHH:MM}"
    {applied/overridden/note as recorded previously}
    entries:
      - |
        {previous entry preserved verbatim}
---
```

### Rules

1. **Placement**: `changelog:` is the **last** key in the frontmatter (after `type`, `venue`, `status`, `date`, `tone`, etc.), so domain keys stay readable at the top.
2. **Order**: Newest version first. Prepend the new `version: v{next_version}` block above the existing entries.
3. **Block scalars (`|`) for entries**: Each entry uses a YAML literal block scalar so backticks, backslashes, colons, brackets, and emoji inside the change description need NO escaping. Indent each entry's content one level under the `|`.
4. **First refine (no prior changelog)**: create the `changelog:` key with both:
   - v1 entry — `version: v1`, `date: "{creation date from existing frontmatter, or the literal string \"initial\" if unknown}"`, `note: "initial draft from autopilot-draft {mode} pipeline"`, no `entries:` required.
   - v2 entry — this round's changes (above v1).
5. **No frontmatter at all (rare)**: create a minimal frontmatter block at the very top of the file with at least `changelog:` (and any other keys you can derive from the document).
6. **Legacy migration (required on first encounter)**: if a file has a `<!-- CHANGELOG (auto-managed by draft-refine ... -->` HTML block (above or below the frontmatter), convert each `vN (date, applied X / overrode Y): ...` line into a frontmatter array entry **in the same refine pass** and **delete the HTML block** (including its surrounding blank lines). After migration the file must begin with `---` on line 1. Apply this migration to the canonical file and every existing required companion.

### Worked example

See [changelog-example.md](changelog-example.md) for the concrete before/after (legacy HTML-comment block → frontmatter `changelog:` array).

## Other rules
- Do NOT touch the version archive of previous versions (`*_v{prev_version}.md` and earlier) — they are immutable historical record.
- Apply ref-grounding to every memo, even trivial-looking ones (they can carry hidden errors).
- **Paragraph Cohesion Pre-Check (mandatory for every memo that rewrites paste-ready content — all modes)** (cross-ref `draft-strategy/SKILL.md` § _Paragraph Cohesion Pre-Check_ for the full 4-step spec; single source of truth there):
  Before applying any memo that adds or rewrites a paste-ready block (LaTeX / markdown / slide / table), run the 4-step pre-check on the **target paragraph as a whole**: (1) is the substance already stated? (2) does the new sentence break the paragraph axis (motivation → design → formalization, claim → evidence → caveat, etc.)? (3) is this substance already canonical at another §-level / slide-level site? (4) classify the edit as EDIT (in-line) / REPLACE / INSERT / DROP — prefer EDIT/REPLACE over INSERT for cohesion. **When refining an existing mutation that fails this check** (e.g., a trailing INSERT whose content overlaps a prior sentence, or a cross-ref that restates substance already covered elsewhere), the correct refine action is to **rewrite the mutation as EDIT / REPLACE / DROP**, not polish the existing INSERT further.
- **Paper mode — Natural-integration rule** (cross-ref `draft-strategy/SKILL.md` paper mode for full spec; single source of truth there):
  When a memo asks to **add a new mutation from a reviewer concern or rebuttal material**, apply the same gating question as draft-strategy: *"Can this be naturally integrated as a 1-2 sentence inline rewrite that flows with the surrounding paragraphs?"* YES → inline rewrite mutation (M15-style: subsection-head opening + body-paragraph touch-up + Figure cascade; numbers/hyperparameters stay in body or Appendix). NO → reject and inform the user — rebuttal-format artifacts (model-comparison tables, structured Q&A blocks, point-by-point enumerations) must not become paper-body mutations even if the reviewer "strongly recommended integration."
  - When **refining an existing mutation**, re-evaluate it against the same rule. If a previously-drafted mutation fails the natural-integration test (e.g., a standalone `\begin{table}` lifted from rebuttal materials with no embedding paragraph rewrite), the correct refine action is **drop the mutation entry**, not polish it further.
- Return which sections were changed, which memos applied vs overridden, and the new version number.
```

Where `{doc_type}` is either "strategy" or "draft" based on auto-detection.
