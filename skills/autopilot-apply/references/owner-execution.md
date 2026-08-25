# autopilot-apply

> **Output convention**: This skill edits real source files outside `<artifact-root>/`. Store its own logs and snapshots under the cheatsheet artifact's `_internal/apply/` directory (CONVENTIONS.md §5 (`<agent-home>/core/CONVENTIONS.md#5-skill-output-convention--t1t2t3`), T3).

## Position in the Autopilot Family

`autopilot-draft` produces a document-change plan as a cheatsheet. `autopilot-apply` supplies the missing execute-and-verify stage by applying that existing plan to the real source and compiling the result.

| Track | Plan or draft | Apply and verify |
|---|---|---|
| Code | `autopilot-spec` / `code-plan` | `autopilot-code` (execute + test) |
| Document | `autopilot-draft` → cheatsheet | **`autopilot-apply`** (apply + compile) |

- Treat the cheatsheet as the plan. Do not create or reinterpret a plan here.
- Treat build/compile plus rendered diff review as the document equivalent of code verification.
- Keep `--target` extensible; only `latex` is currently implemented.

## Default Invocation Rule

Route here when the user asks to apply an existing cheatsheet to real document source and verify the result. Because this changes source files, summarize the resolved cheatsheet, source, and isolation strategy before entering the pipeline unless the explicit invocation already supplies them.

Examples:

- "Apply this cheatsheet to main.tex and compile it."
- "Apply these mutations to the paper source and verify the build."
- "Apply the cheatsheet from this document artifact to the real LaTeX source."

Defaults:

- `--target latex`
- `--isolation branch`; recommend `worktree` for large or risky changes
- Infer `--source` from the cheatsheet verification section; ask only when no unique source can be resolved
- Resolve the cheatsheet by fuzzy matching `<artifact-root>/documents/*`

An explicit `/autopilot-apply <args>` invocation supplies the routing choice directly. For requests outside this scope, use the single authority in [When Not to Use](#when-not-to-use).

## Scope

- **Target**: a git-tracked real source file outside `<artifact-root>/`; for `--target latex`, this is a `*.tex` file.
- **Change source**: an existing cheatsheet under `<artifact-root>/documents/*/draft/draft*.md` produced by `autopilot-draft` paper mode.
- **Boundary**: apply only the recorded mutations. Do not add new changes or revise the cheatsheet.

## Preconditions

Enforce all checks in Stage A. If any check fails, do not create a branch, edit a file, or commit.

1. **Git tracking**: `--source` must resolve inside a git repository and be tracked. Otherwise, explain that the repository needs an initial commit before retrying.
2. **Clean working state**: apply OPERATIONS §5.9 (`<agent-home>/core/OPERATIONS.md#59-git-working-state-preflight`). Abort on dirty source edits, an active merge/rebase/cherry-pick, or detached HEAD. Report the condition; do not abort the user's operation automatically.
3. **Unique cheatsheet**: resolve exactly one fuzzy match. List multiple matches and report zero matches with guidance.
4. **Build tools**: for `latex`, require `latexmk` or `pdflatex`. Recommend `latexdiff`, but fall back to text diff when it is unavailable.

## Target Contract

| Target | Build gate | Rendered-diff review | Source glob |
|---|---|---|---|
| `latex` | `latexmk -pdf -interaction=nonstopmode -outdir="$BUILD_OUT"` or a two-pass `pdflatex` flow with bibtex/biber | Compile `latexdiff <base> <head>`; fall back to `git diff` on failure | `*.tex` |

Compile the complete project so `\input`, local `.sty`, and `.bib` dependencies are included. Prefer `latexmk` for multipass bibliography handling.

Create one local build directory per run with `BUILD_OUT=$(mktemp -d /tmp/lw-apply.XXXXXX)` and pass it to every baseline, gate, and latexdiff compile. This keeps network-mounted sources fast, avoids polluting the repository with build products, and makes baseline comparisons valid. Copy only the final latexdiff PDF to `_internal/apply/latexdiff.pdf`.

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write user-facing summaries and reports in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve source text and identifiers unless the cheatsheet explicitly changes them.

## Pipeline

### Stage A — Preflight

1. **Resolve the cheatsheet** by fuzzy matching prompt terms against `<artifact-root>/documents/*`, then select `draft/draft.md` or another matching `draft/draft*.md`.
2. **Resolve the source** from explicit `--source` first, then from the cheatsheet's final verification checklist or `WHERE` anchors. If several plausible files remain, ask in the conversation language; if no answer arrives, proceed only when one candidate is clearly strongest.
3. **Run all precondition checks** and stop on failure.
4. **Parse mutations** by M-label into `(M-id, where_anchor, classification, old_or_locator, new_block, reason)`. Map cheatsheet tiers to `MECH`, `SEM`, or `STRUCT`.
5. **Enter isolation**:
   - Choose a live base using OPERATIONS §5.9 (`<agent-home>/core/OPERATIONS.md#59-git-working-state-preflight`). If the current branch is already merged or its upstream is ahead, create the apply branch from the latest base rather than a stale HEAD.
   - For `--isolation branch`, create `apply/{cheatsheet-short-name}` and record the base commit.
   - For `--isolation worktree`, create a separate worktree from the selected base.
6. **Compile the baseline before editing**. Create `BUILD_OUT`, run one full build, and record existing errors, warnings, and undefined references in `_internal/apply/baseline.log`.

### Stage B — Apply

Apply mutations in cheatsheet order. Create one commit per successful mutation with `apply: {M-id} {one-line reason}` so individual changes remain revertible.

- Apply `MECH` and `SEM` mutations automatically on the isolated branch or worktree.
- Do not apply `STRUCT` mutations. Add them to the skip list for explicit review and continue with safe mutations.
- Locate every change through an exact anchor match. Do not use `replace_all`.
- If an anchor or source block does not match, skip only that mutation, record the reason, and continue. Prefer a gap over an incorrect edit.

### Stage C — Verify

1. **Compile gate**: rebuild in the same `BUILD_OUT` and compare against the baseline. Gate on new errors, new undefined references, and new `multiply defined` label warnings. Do not add a repeated visual-layout gate here; page limits, footnote splits, widows/orphans, and overfull layout belong to document review.
   - With zero new gated findings, pass.
   - With new findings, identify the responsible mutation, revert its commit, add it to the skip list, and rebuild.
   - If the mutation cannot be isolated or no PDF is produced, fail loudly and halt while preserving the isolated branch for inspection.
2. **Rendered diff**: generate and compile `latexdiff <base>.tex <head>.tex`, then copy the PDF to `_internal/apply/latexdiff.pdf`. If latexdiff fails on complex macros or tables, provide `git diff base..head -- '*.tex'` instead; this fallback does not itself block handback.

### Stage D — Handback

Do not merge automatically. Return the verified branch or worktree for user review. Keep the report to at most eight lines in the selected user-facing language:

```text
✓ autopilot-apply — {cheatsheet} → {source}
• branch: apply/{name} (base {short-hash})
• applied: {N} mutations, skipped: {K}
• compile: {0 or N} new errors against baseline
• rendered diff: _internal/apply/latexdiff.pdf
• skipped: M{id}(STRUCT), M{id}(anchor mismatch) ...

Review options:
  git merge apply/{name}
  git diff main..apply/{name}
  git revert <commit>
  git branch -D apply/{name}
```

At handback, synchronize the draft artifact without rewriting the cheatsheet's plan content:

1. **Cheatsheet status**: mark successfully applied mutation checkboxes complete. Keep skipped mutations pending with a one-line reason. Remove withdrawn or false-positive mutations from the user-facing cheatsheet and record them in `_internal/apply/apply_log.md`.
2. **Pipeline history**: append one row under `## Apply History` in `pipeline_summary.md`: `{date} | branch {name} ({base}..{head}) | applied {N} / skipped {K} | new compile errors {n} | mutations: {ids}`.
3. **Apply evidence**: write mutation status and commit hashes to `_internal/apply/apply_log.md`, resume state to `apply_state.yaml`, and compile evidence to `baseline.log` and `postfix.log`.

### Resume with `--from <stage>`

Resume at `preflight`, `apply`, `verify`, or `handback`. Restore branch, base commit, source, and cheatsheet paths from `_internal/apply/apply_state.yaml`.

```yaml
pipeline: autopilot-apply
target: latex
cheatsheet: <path>
source: <path-to-real-source>
isolation: branch
branch: apply/<name>
base_commit: <hash>
applied: [M3, M5, M7]
skipped: [{id: M9, reason: STRUCT}]
last_completed_stage: verify
```

## Constraints

- Edit and compile only on the isolated branch or worktree; leave the canonical checkout unchanged until the user merges.
- Hand back only a branch that passes the compile gate.
- Never skip the baseline compile.
- Apply mutations only at exact anchors; record mismatches instead of guessing.
- Do not create, reinterpret, or expand the plan. Route incorrect cheatsheets through `autopilot-refine` or `autopilot-draft` first.
- Never merge automatically; merge remains the review checkpoint.

## Examples

```text
/autopilot-apply "apply the tfrestormer camera-ready cheatsheet" --source latex/main.tex
/autopilot-apply "apply the ICML rebuttal mutations" --source paper/main.tex --isolation worktree
/autopilot-apply "tfrestormer cheatsheet" --from verify
```

## When Not to Use

- To revise the cheatsheet itself → `/autopilot-refine` or `/autopilot-draft --from draft`
- To edit Markdown artifacts under `<artifact-root>/` → `/autopilot-refine`
- To change a codebase → `/autopilot-code`
- When no cheatsheet exists → `/autopilot-draft --mode paper` first

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/autopilot-apply.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability autopilot-apply
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `apply-log/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/apply-log/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
