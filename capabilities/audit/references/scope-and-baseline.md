### Stage A: Detect Artifact Type

1. Resolve `<artifact_path>` to an absolute directory.
2. Classify by prefix:
   - `<artifact-root>/plans/*` → **plans**;
   - `<artifact-root>/research/*` → **research**;
   - `<artifact-root>/documents/*` → **documents**;
   - otherwise stop with: `audit accepts only <artifact-root>/{plans,research,documents}/* artifacts. resolved path: {path}`.
3. Print `Detected type: {type} — {artifact short name}` in the user's communication language.

### Stage B: Determine Effective Scope

1. An explicit `--scope <value>` has priority. Map it through Stage B.2. If the mapping is N/A, warn once and return an empty aspect set.
2. Without an explicit scope, use `auto` and Stage B.1.

#### Stage B.1: Auto-Scope

Read signals in order.

**Documents:**

| Signal | Aspects | Reason |
|---|---|---|
| `pipeline_summary.md` frontmatter `mode: presentation` | facts + cross-ref + coverage + structure, including presentation-0 limits: 5–6 bullet lines, keywords ≤10 words, figures/tables ≥60%, tables ≤6×5 | Claims, card coverage, and 16:9 density |
| `mode: paper` | facts + style + cross-ref | Citation, claims, and natural integration |
| `mode: doc` with peer-review or rebuttal-response intent | structure + cross-ref | Form compliance and reviewer-point coverage |
| Other `mode: doc` | style + structure | Format and organization |
| At least ten version-history rows | all | Accumulated drift |
| Insufficient signals | all | Safe default |

**Research:**

| Signal | Aspects |
|---|---|
| Chapters `01_*.md` through `NN_*.md` plus `cards/` | all |
| `cards/` only | card integrity + cross-card |
| Chapters only | Tier consistency + coverage |

**Plans:**

| Signal | Aspects |
|---|---|
| `status: done` plus `test_logs/test_report.md` | test results + code review + semantic-deterministic consistency |
| `status: done` without test logs | code review + incomplete work + semantic-deterministic consistency |
| `status: partial` or `failed` | incomplete work + code review + semantic-deterministic consistency |
| `status: active` | incomplete work |

Print one localized line:

```text
Auto-scope: {aspect 1} + {aspect 2} + ... ({one-line reason})
```

For an explicit scope:

```text
Scope: {value} (user override)
```

#### Stage B.2: Explicit Scope Mapping

| `--scope` | documents | research | plans |
|---|---|---|---|
| `facts` | facts | card integrity | test results + incomplete work |
| `style` | style | Tier consistency | lint |
| `structure` | structure | coverage | code review |
| `cross-ref` | cross-ref | cross-card | N/A |
| `coverage` | coverage | coverage | N/A |
| `all` | facts + style + structure + cross-ref + coverage | card integrity + Tier + coverage + cross-card | test results + lint + code review + incomplete work + semantic-deterministic consistency |

Coverage detects absent claims through set difference between available cards and cited cards. A regex detector that examines only present text cannot detect omissions such as a missing timeline entry.

### Stage B.5: Minor-Log Baseline — documents and research only

Plans skip this stage. Inputs are the exact legacy `pipeline_summary.md` section `## 마이너 변경 로그 (v{N} → next major 누적)` and the newest `_internal/versions/v{N}/` snapshot.

1. Parse each minor entry's version, timestamp, `Files touched`, audit flags, trigger, and rationale.
2. Diff the latest major snapshot against the current artifact:

   ```bash
   diff -ruN _internal/versions/v{N}/ {artifact_root} \
     --exclude=_internal --exclude=pipeline_summary.md \
     > /tmp/audit_p1_diff.txt
   ```

3. Bias Stage C toward changed regions whose minor entries carry matching audit flags. An `audit-flag: none` receives normal checking without bias.
4. Store a `p1_findings` dictionary for Stage D.

Print:

```text
P1 baseline: found v{N}; ingested {count} minor entries (facts={A} / style={B} / structure={C} / cross-ref={D} / coverage={E})
```

If either source is absent:

```text
P1 baseline: skipped — no latest major snapshot or minor log. P2 only.
```
