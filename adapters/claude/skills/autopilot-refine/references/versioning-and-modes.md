# autopilot-refine — Versioning and mode details

Details referenced by `## Default Invocation Rule`, `## Verification rigor`, and `## Mode Forms` in `../SKILL.md`: minor logs, major application, the rationale for the split, adversarial propagation, default/STRUCT behavior, and tunable constants.

### Minor-log entry format

For every minor change, add one row to the `## 버전 히스토리` table and one detailed block to `## 마이너 변경 로그 (v{N} → next major 누적)` in `pipeline_summary.md`, newest first.

**Version-history row:**

```markdown
| v{N}_M | YYYY-MM-DD | (minor) one-line summary, at most 120 characters |
```

**Minor-log entry:** create the section before `## 미해결 이슈` or immediately after the table when absent.

```markdown
### v{N}_M — YYYY-MM-DD HH:MM
- **Trigger**: one verbatim line from the user prompt, at most 80 characters
- **Scope**: minor (direct Edit, no snapshot)
- **Rationale**: why none of the three major criteria applies
- **Files touched**:
  - `relative/path/file1.md` — one or two lines describing the edit
  - `relative/path/file2.md` — one or two lines describing the edit
- **Cross-ref / deps**: related mutations, labels, or downstream artifacts; use `—` when absent
- **Audit-flag**: one of `facts`, `style`, `structure`, `cross-ref`, or `coverage`; use `—` when none applies
- **Reversibility**: exact edit location and original wording, or `git revert or last major snapshot reference`
```

The **Audit-flag** lets audit collate the first half of its dual-perspective review efficiently: changes since the last major version.

**Affected-file frontmatter `changelog:` entry:** when the file already defines a `changelog:` array, insert the new entry first.

```yaml
changelog:
  - version: v{N}_M
    date: "{YYYY-MM-DDTHH:MM}"
    applied: {count}
    overridden: 0
    entries:
      - |
        [MINOR {scope}] [direct Edit]: {one-line summary, at most 200 characters}
  - version: v{N}_{M-1}
    ... # Preserve all existing entries.
```

Skip files without a frontmatter `changelog:` field. This duplicates the pipeline summary deliberately: the in-file frontmatter preserves Git-tracked lineage even if the summary is damaged or the file is viewed through a cross-artifact reference.

### Major application

When automatic invocation selects a major refine flow:

1. Run Stages A–D at the selected intensity and derived rigor: investigate, preview, mechanically snapshot, and apply.
2. Require the route-bound snapshot receipt created by `artifact-guard.sh` / `utilities/artifact-snapshot.py`; never allocate or copy `_internal/versions/v{N+1}/` manually.
3. Move the entire active `## 마이너 변경 로그 (v{N} → next major)` body verbatim into `### 누적 마이너 변경 사항 (v{N}_1 → v{N}_M)` under the new `## v{N+1} 변경 사항`, then clear the active minor-log section.
4. Add a major row to `## 버전 히스토리`: `| **v{N+1}** | ... | (major) ... |`.

### Why split minor and major paths

Most daily edits are entry-level changes. Running a full QA, snapshot, and version-bump ceremony for every minor edit costs more than it returns. Traceability remains complete because every minor entry records its trigger, files, audit flag, and reversal path in `pipeline_summary.md`. Once accumulated minor changes cross the threshold, audit reviews them in a batch from two perspectives: the diff from the last major snapshot and general principles. Reserve major ceremony for genuine review boundaries, structural redesign, or pipeline re-entry.

### Adversarial-tier propagation

At `adversarial` intensity, after thorough reviewers return, spawn the external adversary (the review unit dispatched to the codex harness via `stage-dispatch-fallback`) with:

1. the proposed diff;
2. artifact intent from `pipeline_summary.md`; and
3. ground truth from `cards/*.md` for research, or `analysis/*.md` plus the current strategy and draft for documents.

Surface external findings beside internal findings before any confirmation step. Mark a blocking external issue as `⚠ External: <issue>` so the user can apply, revise, or abort deliberately.

### Mode-form notes

> **Application approval:** quick+ requires the separate preview gate immediately before edits. Snapshot/version history preserves the preimage after approval; it does not substitute for approval. `--review-only` makes no target write.

> **STRUCT halt:** never auto-apply a change that affects at least five files, rewrites a whole section, or requires rerunning an autopilot pipeline. Halt and recommend `/autopilot-research --from analyze` or `/autopilot-draft --from strategy`.

### Tunable constants

| Constant | Default | Description |
|---|---|---|
| `AUDIT_HINT_THRESHOLD` | 5 | Refine-cycle count after which Stage D recommends `/audit`. Raise it, for example to 10, to reduce reminders; set it to `0` to disable them. |
