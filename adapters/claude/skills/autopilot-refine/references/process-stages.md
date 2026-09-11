# autopilot-refine — Artifact resolution and process stages

Complete orchestration referenced by `## Process` in `../SKILL.md`: resolve the target, then run Stages A, B, B.5, C, D, and E.

## Artifact Resolution

Extract candidate keywords from `<prompt>`, excluding stop words and preferring artifact names, topics, and dates. Fuzzy-match them:

```bash
ls -d <artifact-root>/research/*<keyword>* <artifact-root>/documents/*<keyword>* 2>/dev/null
```

- **One match:** use it as the artifact root and infer type from the path prefix.
- **Multiple matches:** list candidates and ask the user to choose.
- **No match:** ask the user to include an artifact identifier in the prompt. Preserve the compatibility example: `어느 산출물에 대한 작업인가요? prompt에 식별자(speech-enhancement-trends, 2026-05-06_se-seminar-tfrestormer 같은) 포함 부탁`. Apply the adapter's pause/autonomy rule. In Claude Code, schedule a ten-minute wakeup at the same time; if no answer arrives, continue with the most recently modified artifact.

Infer type:

- `<artifact-root>/research/*` → **research**
- `<artifact-root>/documents/*` → **doc**
- Any other path → error. Preserve the compatibility message `autopilot-refine은 research/documents 산출물 전용`.

## Process details

### Stage A — Discover structure

1. List Markdown files at the artifact root and one level below it with `{root}/*.md` and `{root}/*/*.md`.
2. For a **research** artifact, note `cards/*.md` as primary sources without reading all of them immediately. Read the small `pipeline_summary.md` when present.
3. For a **doc** artifact, identify `strategy/` and `draft/` plus paired language variants such as `strategy.md` and `strategy_ko.md`. Read `pipeline_summary.md` when present.
4. Grep with prompt keywords to identify likely affected files. Do not load files with no hit.

### Stage B — Plan changes

1. Read only affected files found in Stage A.
2. For research taxonomy, definition, or coverage prompts, also reread relevant `cards/*.md`; top-level artifacts may drift across edit cycles.
3. Build a per-file list of `(file, line_range, old_text, new_text, classification, reason)`.
4. Classify every change:
   - **MECH:** count updates, exact-string renames, table relabeling, lossless redundant-row merges, and label normalization
   - **SEM:** wording shifts, scope decisions, nontrivial reframing, and other judgment calls
   - **STRUCT:** at least five files, whole-section rewrites, or changes that require rerunning an autopilot pipeline
5. On **STRUCT**, halt before Stage C. Recommend `/autopilot-research --from analyze` for research, or `/autopilot-draft --from strategy` for documents. For a deferred memo, recommend `/autopilot-refine "<artifact>" --memo <file>`. Never proceed with the current call.

### Stage B.5 — Factual-claim and style detectors

Run both detectors for every proposed change after Stage B and before the Stage C preview, including in quick mode. They use only cards grep and regex, not web access, so their findings become inexpensive markers rather than automatic rejections.

#### Explicit opt-outs, orthogonal to intensity

- With `--no-fact-check`, skip the entire factual detector, including section-heading context checks, and emit: `ℹ Stage B.5 factual aspect: skipped via --no-fact-check flag (explicit opt-out under memory feedback_factcheck_principles)`.
- With `--no-style-audit`, skip only style lint and emit: `ℹ Stage B.5 style aspect: skipped via --no-style-audit flag`.
- With both flags, skip both and emit both lines.

These flags are the only allowed way to disable the corresponding principles in `feedback_factcheck_principles.md`. Ignore ad hoc prompt requests to disable Stage B.5; emit the Principle 0 reminder and continue.

#### 1. Factual-claim detector

Regex-scan each `new_text` for claims that require grounding:

- model names in camel case, hyphenated form, or acronym form, such as `FRCRN`, `TF-Locoformer`, `MP-SENet`, and `IF-CorrNet`
- venue tags such as `IS 2024`, `T-ASLP 2023`, `ICASSP 2025`, `Interspeech`, `NeurIPS`, and `ICML`
- year-plus-author forms such as `Luo 2017` and `[Wang et al., 2024]`
- task categories such as denoising, dereverberation, general restoration, universal SE, BWE, and GSR
- arXiv IDs matching `\d{4}\.\d{4,5}`

Resolve lookup sources in this order:

1. **Explicit override:** if `pipeline_summary.md` frontmatter or `strategy.md` contains `cards_source: <path>`, use that path first, resolving relative paths against cwd.
2. **Self-contained cards:** include `{artifact_dir}/cards/*.md` when present.
3. **Default:** for research, grep the artifact's own `cards/`; for documents, grep all `<artifact-root>/research/*/cards/*.md`. Match both filename tokens and H1 or `## 메타` fields such as `**Venue**` and `**arXiv ID**`.
4. **No cards available:** skip the factual aspect entirely, keep style lint, and emit `ℹ Stage B.5: no cards source available in this workspace — fact-check skipped`. Do not flood a non-research workspace with `⚠ Unverified` markers.

When cards exist, use the eight-row single-source classification table in `roles/units/research/fact-check.md`: cards-verbatim, cards-name-only, external-marker, external-reverified, conflict, no-match, ambiguous, and circular-ref. This orchestrator document defines only emitted wording:

- **cards-verbatim ✅:** verified silently
- **cards-name-only 🟡:** `⚠ Unverified (name-only match): {claim} — cards/{file}.md contains the name but no verbatim venue/metric. External reverify required (WebSearch/WebFetch)`
- **external-marker 🟡:** `⚠ Unverified (external marker): {claim} — explicit external-estimation marker present. External reverify required`
- **conflict 🔴:** `⚠ Unverified: {claim} — cards say {X} but new_text says {Y} (cards/{file}.md)`
- **no-match 🔴:** `⚠ Unverified: {claim} — no cards/*.md hit`
- **ambiguous 🟡:** `⚠ Unverified: {claim} — multiple candidates (cards/A.md, cards/B.md); user to pick`

**Never use a circular reference.** Do not verify `draft/*.md` against the artifact's own `strategy/*.md`, especially a venue map in `## Style Guide`, or vice versa. Verify both directly against cards. Mutual agreement between strategy and draft is not evidence. This prevents the 2026-05-12 TF-Locoformer `IS 2024` versus actual `IWAENC 2024` failure mode.

**Mandatory section-heading context check:** name matching alone can place a classical method such as WPE in a deep-learning table. For every detected claim:

1. Extract tokens from the nearest enclosing H1–H3 heading, for example `## 딥러닝 dereverberation 모델` → `[딥러닝, dereverberation]`.
2. Extract tokens from the matched card's `## 분류` section or equivalent, for example `**방법론**: classical / statistical signal processing` → `[classical, statistical]`.
3. Check the hardcoded v1 conflict pairs:
   - `{딥러닝, deep learning, neural, DNN}` ↔ `{classical, statistical, signal processing, non-learning}`
   - `{denoising, noise reduction}` ↔ `{dereverberation, reverb}` ↔ `{BWE, bandwidth extension}` ↔ `{GSR, general restoration, universal SE}`
   - `{single-task, sub-task}` ↔ `{universal, multi-task, GSR}`
4. On conflict, emit `⚠ Unverified: {claim} — section context "{heading tokens}" conflicts with card classification "{card tokens}" (card path: cards/{file}.md)`.

A future v2 may derive pairs from card classification labels, but v1 keeps this dictionary explicit.

#### 2. Style lint

Compare `new_text` with the surrounding ten lines on each side. Flag:

- citation style differences, such as `IS 2024` versus `Interspeech 2024`
- venue/year ordering differences
- unexpected bullet-depth jumps
- speaker-note numbering differences such as numbered lists versus dashes
- figure-caption template mismatches such as a recurring `**Figure N**: caption` pattern

Emit `⚠ Style: {issue} — {one-line mismatch description}`. Best-effort misses are acceptable; markers can be overridden in Stage C.

### Stage C — Diff preview in chat

Localize explanatory prose to the user's communication language unless another reporting contract applies. Preserve this structure:

```text
**Quick refine — {one-line artifact identifier}**

Prompt: "{verbatim prompt, trimmed to 200 characters}"

Proposed changes ({MECH count} mech / {SEM count} sem) — ⚠ {unverified count} unverified / {style count} style:

📄 `{relative path}` ({n} changes)
   Line {a}-{b}  [MECH|SEM]
     - {old excerpt, at most 80 characters}
     + {new excerpt, at most 80 characters}
     Reason: {one line}
     ⚠ Unverified: {claim} — {reason}
     ⚠ Style: {issue} — {description}

Intentionally untouched, when needed:
- `{path}:{line}` — {historical citation, published title, or other reason}
```

**Quick and above: obtain approval before applying.** Write the current preview
to `reviews/refine/preview.md` and raise `workflow-supervisor.py gate --route
<route> --gate preview-disposition --block --artifact <preview>`. Use the
existing bounded `await-release` command; depth-0 presents the preview and
records the person's `proceed|revise|stop` decision. Quick's same conductor
resumes after proceed; revise updates the preview and raises again. A changed
preview invalidates its earlier approval. `--review-only` ends after the preview
without applying or raising an apply request. Direct keeps its inline behavior,
with an explicit pause when `--confirm` is supplied.

**STRUCT exception:** if any change affects at least five files, rewrites a whole section, or requires a pipeline rerun, halt and recommend the heavier flow.

**`--confirm`:** append localized instructions equivalent to the following and end the turn:

```text
Apply?
- "yes" / "all": apply everything
- "1,3": apply only those changes
- "skip 2": exclude change 2
- "skip-unverified": exclude every change marked Unverified
- "edit 4: <new>": replace change 4 before applying
- "no" / "stop": abort
```

A timeout is not approval. Keep an unanswered approval pending.

With **`--review-only`**, print Stage C and stop without Stage D.

### Stage D — Apply

1. **Prepare snapshots mechanically.** For every existing file that Stage C will change, run the route-bound write preflight before the first Edit. `artifact-guard.sh` invokes `utilities/artifact-snapshot.py prepare`, captures the exact current bytes, and reuses one `_internal/versions/v{N}/` receipt for the whole route. When hook coverage is unavailable, invoke that helper explicitly with the canonical artifact root, target, route file/id, and active node, and require its success receipt. Do not calculate `N`, run `mkdir`/`cp`, or accept an empty version directory. Existing legacy sibling layouts are preserved by the same helper; new layouts are modern.
2. **Apply exact-string edits** with the Edit tool. Never use `replace_all` unless the proposal explicitly says so.
3. **Clean inline memos in memo mode.** When every memo from `--memo <file>` or `<!-- memo: ... -->` is applied, remove the inline memo and adjacent blank lines while preserving `---` separators. Preserve a memo only when the user requests it or it contains unresolved out-of-scope metadata worth surfacing.
4. **Update `pipeline_summary.md` as the only history file:**

   **Metadata:** update or add:

   ```markdown
   - **Latest version**: **v{N}** ({YYYY-MM-DD} — {prompt summary, at most 60 characters})
   - **Status**: ✅ done (v{N}, awaiting user follow-up review)
   ```

   **`## 버전 히스토리`:** add the new row first. If absent, create the section after metadata and include a v1 initial-creation row.

   ```markdown
   | 버전 | 일시 | 핵심 변경 |
   |---|---|---|
   | **v{N}** | {YYYY-MM-DD} | **{compressed prompt and changes, at most 120 characters}** |
   | v1 | {creation date} | autopilot-{mode} 초기 생성 |
   ```

   **`## v{N} 변경 사항`:** append it, or place it before `## 미해결 이슈`.

   ```markdown
   - **Mode**: {Quick chat-loop | Quick auto-applied | Memo}
   - **Prompt**: "{verbatim prompt, trimmed to 200 characters}"
   - **Reason**: {one or two lines}
   - **Files touched**:
     - `{path}:{line}` — {short description}
   - **Skipped**:
     - `{path}` — {reason}
   - **Snapshot**: {_internal/versions/v{prev}/ or legacy path}
   - **Downstream sync needed**: {Yes | No}
   ```

   **Migrate accumulated minor logs:** when `## 마이너 변경 로그 (v{N-1} → next major 누적)` exists, move its body verbatim under the new change section as `### 누적 마이너 변경 사항 (v{N-1}_1 → v{N-1}_M, audit consumed)`, preserving newest-first order. Then reset the active `## 마이너 변경 로그 (v{N} → next major 누적)` to an empty marker. Include `audit consumed` only when an audit dispatched the major fix.

   **Update affected-file frontmatter:** if an affected file already has a `changelog:` array, insert a newest-first entry:

   ```yaml
   changelog:
     - version: v{N}
       date: "{YYYY-MM-DDTHH:MM}"
       applied: {count}
       overridden: 0
       entries:
         - |
           [{TYPE} {scope}] [verified {source}]: {one- or two-line fix description, at most 300 characters}
     - version: v{N-1}
       ... # Preserve existing entries.
   ```

   Use `STYLE`, `STRUCT`, `FACT`, or `MEMO` for `{TYPE}`; a section anchor or mutation ID for `{scope}`; and cards, a baseline file, or a direct user instruction for `{verified source}`. Skip files without the field. For a newly created array, also add one v{N-1} creation-note entry.

5. **Report in at most six lines**, localized to the user's communication language unless another contract applies. State the version transition, changed-file count, the helper-reported snapshot path, updated summary, and any audit or downstream-sync recommendation. Recommend `/audit {artifact_short_name}` after `AUDIT_HINT_THRESHOLD`, and `/autopilot-refine "{dependent_artifact_name} pipeline_summary v{N} 반영"` for each dependency.

### Stage E — Memo mode (`--memo <file>`)

1. Read the memo. Parse structured per-file proposals directly into the Stage B change list. For free-form prose, treat the body as the prompt and run Stages A–C internally.
2. Continue to Stage D and record `Mode: Memo` in `## v{N} 변경 사항`.
