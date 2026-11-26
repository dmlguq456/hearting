# Artifact metadata and the history line (v1)

Campaigns and cycles carry an easy title, a one-line summary, branch tags, kind tags,
and a short ID. Everything below is **producer-owned metadata about existing
artifacts**: it never changes a manifest, a route, `campaign.json`, a cycle boundary, or
the old display declarations. **Nothing here is a gate, a required input, an approval,
or a duty of an agent or a user.** Without any of these files every reader falls back to
the old display declaration and then to the folder name.

One writer path (`utilities/artifact_meta.py`) and one recorder
(`utilities/artifact_history.py`) serve the background review, a person, and an agent,
identically for Claude, Codex, and OpenCode.

## Public files (Cairn reads these)

### `campaigns/<campaign-locator>/meta.json` — `artifact-meta/v1`

```json
{
  "schema_version": 1,
  "contract": "artifact-meta/v1",
  "artifact_root_id": "root_11111111111111111111111111111111",
  "campaign_id": "camp_22222222222222222222222222222222",
  "campaign": {
    "short_id": "CMD-03",
    "aliases": ["OPS-02"],
    "title": "명령어 인식 모델 TTS 화자 추가 (V8)",
    "summary": "네 조건 중 집 적응 모델이 가장 균형 잡힌 결과를 보임",
    "branches": ["CMD", "TTS"],
    "kinds": ["학습"],
    "source": {
      "short_id": {"by": "rule", "at": "2026-10-01T09:00:00Z"},
      "title": {"by": "model", "at": "2026-10-01T09:00:00Z"},
      "summary": {"by": "model", "at": "2026-10-01T09:00:00Z"},
      "branches": {"by": "model", "at": "2026-10-01T09:00:00Z"},
      "kinds": {"by": "model", "at": "2026-10-01T09:00:00Z"}
    }
  },
  "cycles": {
    "cyc_33333333333333333333333333333333": {
      "short_id": "CMD-03.4",
      "aliases": [],
      "title": "집 적응 모델 비교",
      "summary": "집 환경에서 네 조건의 오류를 비교함",
      "branches": ["CMD"],
      "kinds": ["평가"],
      "source": {
        "short_id": {"by": "rule", "at": "2026-10-01T09:00:00Z"},
        "title": {"by": "model", "at": "2026-10-01T09:00:00Z"},
        "summary": {"by": "model", "at": "2026-10-01T09:00:00Z"},
        "branches": {"by": "model", "at": "2026-10-01T09:00:00Z"},
        "kinds": {"by": "model", "at": "2026-10-01T09:00:00Z"}
      }
    }
  }
}
```

UTF-8, `ensure_ascii=False`, NFC strings, last byte LF, replaced atomically. Every field
of an entry is optional; the writer adds fields as they become known.

- **Limits.** `title` 1–120 characters, one line. `summary` ≤ 400 characters, one line
  (empty is allowed). `aliases`, `branches`, and `kinds` each ≤ 16 entries without
  duplicates. All text is NFC with no leading/trailing whitespace and no control
  characters. A branch code is 2–5 uppercase ASCII letters; the first branch is the
  representative. `kinds` is a subset of the fixed list 학습, 데이터, 평가, 문서, 운영,
  조사, 배포 (the list is a code constant, not a file). Short IDs: campaign `CODE-nn`
  (two or more digits), cycle `CODE-nn.k`.
- **Identity.** `artifact_root_id` and `campaign_id` equal the root and the campaign of
  the folder the file sits in. `cycles` holds only cycles the producer currently
  assigns to that campaign.
- **Presentation mark (optional).** The campaign entry may carry
  `presentation_kind: "archive_bundle"` — the single allowed value; absence means an
  ordinary campaign. While present, top-level `repository_id` is required and must
  equal the root's `repository_id`, exactly bound together with `artifact_root_id`
  and `campaign_id`. A cycle entry must never carry `presentation_kind`. A wrong enum,
  a wrong type, a cycle-scoped value, or a mismatched ID is a `presentation-*`
  error: the campaign's own meta is skipped with that reason while lifecycle state
  keeps folding underneath it. The writer is the official
  `set --presentation-kind archive_bundle` (a person or an agent may record the
  classification evidence in the existing optional `--reason`; the background
  model judgement never creates it); readers
  skip an invalid mark without repairing it.

```json
{
  "schema_version": 1,
  "contract": "artifact-meta/v1",
  "artifact_root_id": "root_11111111111111111111111111111111",
  "repository_id": "repo_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "campaign_id": "camp_22222222222222222222222222222222",
  "campaign": {
    "short_id": "CMD-03",
    "presentation_kind": "archive_bundle",
    "title": "명령어 인식 모델 TTS 화자 추가 (V8)"
  },
  "cycles": {}
}
```
- **Readers** check only the types and limits of the fields they know and **ignore
  unknown fields** (a later `groups` field does not break them). A campaign that breaks
  a rule has only its own meta skipped, with a warning; other campaigns and the
  old-declaration fallback go on. A writer never overwrites such a file to "repair" it,
  keeps unknown fields it finds, and adds none.
- **Title priority.** Valid `meta.json` title → `campaign-display-titles.json` entry →
  folder name. The old declaration is only read, never written or deleted.

### `.runtime/artifact-producer/v1/project-meta.json` — `artifact-project-meta/v1`

```json
{
  "schema_version": 1,
  "contract": "artifact-project-meta/v1",
  "artifact_root_id": "root_11111111111111111111111111111111",
  "display_name": "명령어 인식 연구",
  "branches": [
    {"code": "CMD", "label": "명령어 모델", "note": "에어컨 명령어 인식"},
    {"code": "TTS", "label": "TTS 데이터", "note": "합성 화자 데이터"},
    {"code": "ETC", "label": "기타", "note": "기존 갈래에 맞지 않는 작업"}
  ]
}
```

Exactly these five top-level fields; the fixed kinds, sources, and counters are not in
the file. The vocabulary holds at most **12 general branches**; the reserved fallback
`ETC` (label `기타`) is not counted, so the file has at most 13 entries (public limit 16).
A project with no vocabulary gets its starter list from the first judgement. When the
cap is reached, the writer replaces a proposed new code by `ETC`. The writer limits a
label to 40 and a note to 120 characters (stricter than the contract, which only needs
strings). Without a project file the default display name is the project folder name.

### `.runtime/artifact-producer/v1/experiment-catalog.json` — `experiment-catalog/v1`

What each experiment condition is called, what it changes, what it is compared with, and what
the goal and deciding metric are. One file per project root, next to `project-meta.json`.

```json
{
  "schema_version": 1,
  "contract": "experiment-catalog/v1",
  "artifact_root_id": "root_11111111111111111111111111111111",
  "revision": 3,
  "updated_at": "2026-11-13T10:00:00Z",
  "terms": [{"term": "fix", "meaning": "화자 수 2명 고정 모델(원고의 기준)", "note": ""}],
  "groups": [{"id": "grp_0123456789ab", "display_name": "AMI 가변 화자 개선",
              "goal": "AMI Dev에서 var WER이 fix 19.99%보다 낮은가",
              "metric": {"key": "wer", "unit": "%", "direction": "min", "split": "validation",
                         "scoring": "AMI Dev, oracle VAD"},
              "target": {"kind": "fixed", "label": "fix", "value": 19.99, "split": "validation",
                         "scoring": "AMI Dev, oracle VAD"},
              "status": "running"}],
  "conditions": [{"id": "cnd_0123456789ab", "group": "grp_0123456789ab",
                  "display_name": "0.3초 이하 짧은 발화 유지, 최대 10명", "aliases": ["retain030"],
                  "former_names": [], "changes": "short_speech.mode=retain, max_n_spks=10",
                  "compared_to": null, "problem": "짧은 발화에서 화자 누락",
                  "refs": {"runs": [{"run_id": "mpwav-…-retain030-n10", "row_id": null}],
                           "output_dirs": [], "configs": []}}],
  "merged": []
}
```

- **Owns names, goals, and comparison meaning only.** Progress, ETA, metric points, and
  stagnation stay in the public run record (`resource-runs/<run_id>.json`); the catalog never
  copies them. A run joins a condition through `refs.runs` (`run_id`, optional `row_id`).
- **Not `project-meta.json`.** The branch vocabulary classifies work and is closed at five
  fields; condition names change during a run. **Not `shared/`.** That folder holds admitted,
  immutable revisions and is protected by the locator and metadata amendments, so a file that
  changes while a condition is renamed does not belong under it.
- **One write path**: `utilities/experiment_catalog.py` under the root admission lock, one
  atomic replace, and one `kind=meta` history event (`target.type=project`) per changed place.
  Two writers at once both land (each mutation runs on the latest file inside the lock). A
  rename keeps the old name in `former_names`, so notices and cards written earlier still
  resolve; earlier notices are not rewritten.
- **Project boundary.** The file carries `artifact_root_id`. A file whose id is not this root's
  (copied from another project, or the root has no identity) reads as invalid
  (`foreign-root` / `root-identity-missing`): it is never used for names and never written.
  A broken file is never overwritten to "repair" it. Readers ignore unknown fields and a
  writer keeps the ones it finds.
- **A gap is a warning, never a gate.** `lint` and `status` report a missing name, a name equal
  to a run id or file name, a duplicate name, a missing goal, metric, unit, or target, with the
  place in the file (`conditions[<id>].display_name`) and exit 0. Nothing here stops a run, a
  write, or an answer. A name, alias, former name, or run id that two conditions answer to
  resolves to neither (`name-overlap`): a write by it is refused naming both places, and the
  condition id always works.
- **Comparison.** `status` gives a difference only when both values are the condition's latest
  value of the declared metric on a comparable basis: a fixed baseline must declare the same
  split and scoring as the metric; a condition baseline must be declared by its own group with
  the same metric key, unit, direction, split, and scoring, and be at the same epoch. A percent
  difference is in points (`%p`) with the relative change kept separate. A best value is never
  mixed into a latest comparison. Anything else is `no-data` or `not-comparable` with the
  reason; no number is invented. A linked row is read on its own: a row that has ended is not
  running and has no expected time, and the run's progress or ETA never stands in for the row's.
- Commands: `python3 utilities/experiment_catalog.py show|status|lint|leaderboard|register|rename|merge|link|add-term`
  (`--artifact-root ROOT` or `--path PROJECT`; writes take `--dry-run`).
- Advice: `python3 utilities/experiment_catalog.py scan [--file F]` reads a text on stdin or from a file and
  reports run ids, aliases, and former names the text uses without the condition's catalog name (JSON, exit 0;
  it never blocks, rewrites, or asks). It stays quiet when unsure: one-letter, plain-word, number, and version
  aliases (`E`, `base`, `0.3`, `v2`) and tokens in code, quotes, paths, URLs, file names, or commands.

### `현황.md` / `STATUS.md` — the project status document

One short page a person reads first to see where a project stands. Fleet puts its one
line on the project's OVERVIEW rule and its parts at the top of the project page.

**The campaign document is made by code. A main session does not edit it by hand.** The
entrance refresh worker (`utilities/campaign_document.py`, one turn after a begin, any
close or a seal; the generator leaves a close that is not proven out of the document) writes `campaigns/<campaign>/campaign.md` for each campaign that
moved: the sections `## 하는 중` → `## 최근에 끝난 것` (→ `## 나중에 할 것`,
not yet written), one line per piece of work, routes of one PR joined. It creates the file or
replaces one whose first line is its marker `<!-- hearting:campaign-document v1 -->`; a
`campaign.md` without the marker, a link, a directory and a hand-written `현황.md` /
`STATUS.md` are never written. Nothing waits on it: without a document, Fleet builds the line
from the routes, questions and results it already shows. **The document says nothing about what
waits for a person's answer: that is a registered question, and a question stands on the
question screens only** (the panel's QUESTIONS, Fleet's question board). An older generated
document that still has a `## 기다리는 것` section is read without it, and the document is
written again as the work moves, without the section.

- **Record pages.** Each line of the document ends `[기록 N ›](<records/<key>.md>)`, a link to
  the record page of that work: `campaigns/<campaign>/records/<key>.md`, where `<key>` is
  `pr-<N>` or, with no PR, the folder name of the work's first route (an ASCII letter or digit
  first, then letters, digits, `.`, `_` or `-`, at most 200 characters; any other name gets no
  link). The
  same code writes it: first line `<!-- hearting:campaign-record v1 -->`, one line per record
  (a route, a publish, or the supervisor's `RV-…PR<N>….md` review file, found by name and not
  counted in `N`; a review route's rounds share its one line). It writes the pages of the lines the document shows and
  only when their text changed; it never deletes a page, and a file of that name without the
  marker, a link or a `records` that is not a folder is left as it is and its line has no link.
  A route that closed with a commit (`head_commit` differs from `source_commit`) but no PR
  number is joined to the pull request merge that brought its head in: the first
  `Merge pull request #N` between its head and `origin/HEAD`, `origin/main` or `main` whose
  first parent is not already a descendant of the head. A head on that base's own first-parent
  line has no PR: a commit pushed straight to `main` (even when a longer branch later took
  `main` in by a pull request and came back) or a head that is itself a merge there. A head that
  went through a longer branch is joined to its own pull request into that branch, and a merge
  with three or more parents counts by its subject. Only a full 40-hex `head_commit` and
  `source_commit` are asked; a pass makes at most 40 `git log` calls (its `rev-parse` base
  lookups are not counted) and one timeout ends git for that pass; read only, no fetch. A
  number already known is never changed. `records/` is not a cycle folder:
  the locator, tidy, closeout and residue passes skip it as they skip `journals/`.
- **Where.** `campaigns/<campaign-locator>/campaign.md`, directly in the campaign
  folder, made by the code above. Fleet prefers these campaign pages, reads up to three
  newest summaries on the repo page, and keeps the newest summary on OVERVIEW. A
  hand-written `현황.md` / `STATUS.md` left in the same folder stays but is not shown once
  a `campaign.md` is there, and nothing edits it. Editing a page in place is observed even
  when the campaign directory's timestamp is old.
- **Legacy documents.** With no campaign page, Fleet reads status documents directly
  in a cycle's `artifacts/`, not the chronological research folders. When
  `finalize` picks the
  primary itself, this file is the cycle's primary (`현황.md` first, then `STATUS.md`,
  after the official spec PRD; an explicit `--primary` still wins), and its row carries
  `audience: "user"`; Fleet's project line reads the same file
  (`core/CORE.md` §3, primary selection). Names match NFC-normalized and case-folded. A
  file at the cycle root (`campaigns/<campaign>/<cycle>/현황.md`) makes the seal fail
  with `file-outside-artifacts` and is not read; copies under `_internal/` or
  `versions/`, or deeper inside `artifacts/`, are not read either. These cycle documents
  are legacy: nothing keeps them current now (a finalized cycle still takes writes, D-123).
- **`campaign.md` first.** A campaign folder's `campaign.md` is opened by name before
  any older name (no folder listing, no ownership marker required); too large or unreadable,
  it is unknown and no older name stands in for it.
- **One selection rule.** Fleet and the artifact entrance use the common campaign-document
  reader. Conflicting campaign-root `STATUS.md` / `현황.md` candidates remain ambiguous;
  neither mtime nor a legacy copy resolves that conflict. Unreadable, oversized or symlinked
  canonical candidates remain unknown. Legacy-only pages (a hand-written `현황.md` /
  `STATUS.md`, or one in a cycle's `artifacts/`) keep their read compatibility; the runtime
  never edits them.
- **Other campaign documents.** Optional handoffs and topic Markdown documents
  also live directly under the campaign. The common entrance lists them as
  campaign documents, with their existing ownership and locator; it creates no
  cycle or authority ID for them. Missing identity remains unknown. This does
  not move historical research/cycle documents or require a new page.
- **Reading.** Readers take the one-line paragraph (else the first paragraph above the first
  section that is not the `갱신`/`Updated` stamp, else the title), count the top-level
  bullets under the decisions heading of a hand-written page (`## 결정 필요`, `## Decisions needed`;
  a generated `campaign.md` has none, and a `## 기다리는 것` left in an older one is not counted)
  and take the first bullet under the next heading (`## 나중에 할 것`, `## 다음`, `## Next`).
  A section that records decisions already made (`## 사용자 결정 기록`, `## Decision log`) is
  not counted. The file's modification time is when the status last changed. Fleet shows the
  age in front of a document older than three days. The shell's status popup opens a generated
  `campaign.md` on a list of its sections' own words (what is in progress first, the
  other headings folded, `full document ›` for the whole page) and any other page as it always
  drew it (`core/references/session-shell.md`); a section it does not know is a folded line,
  so a new heading never breaks it.

### Root `README.md` / `index.html` — the read entrance

Eight files directly under the artifact root, written by `utilities/artifact_entrance.py`:
`README.md` and `index.html` (the entrance), `all-campaigns.md` / `all-campaigns.html`
(every campaign), `all-spec.md` / `all-spec.html` (every spec topic, plus the contents line below) and
`recent-route-reports.md` / `recent-route-reports.html` (the recent route reports). They are a derived reading surface, not canonical records: admission,
census and residue never take them as input, and deleting them is safe — the next publish
recreates them. They are classified `C-RT` in `core/CORE.md` §3.

- **When.** Right after a seal (the first seal and a re-seal refresh alike), and whenever
  `hearting reports` or `hearting open` runs. No other write path touches them.
- **Content.** The entrance has up to four sections, top to bottom: **spec** (the 3 most
  recently changed spec topics, each with the day it was last changed and its title, and
  one "N more" line, N being the topics not shown, absent when none is hidden, that opens
  `all-spec.*`), **campaigns** (the 3 most recent campaigns with date, title and the
  campaign's one-line summary from `meta.json`, then one "All N campaigns" line, N being
  every campaign, that opens `all-campaigns.*`), **backlog** (one line that opens the
  root's `backlog.md`, see the next section; its meta is the total, the count of each kind, the
  count in Discuss and the file's `갱신` date (`6 total · FIX 2 · TASK 1 · NOTE 1 · TBD 2 · Discuss 1`,
  `TBD n` only where some job has no kind; a document with no kind written on any line says `FIX ? · TASK ? · NOTE ?` and its `TBD`); there is no section when that regular file is absent or a link, and
  `Counts unavailable` instead of the counts when it cannot be read or is over 1 MiB) and **records** (a link to the recent
  route reports page and a link to the ID index `campaigns/INDEX.md`), plus the root
  totals. The line counts are the constants `ENTRANCE_SPEC_LINES` and
  `ENTRANCE_CAMPAIGN_LINES` (not settings). Under the spec, campaigns and backlog titles one
  explanatory line says what the section is (the spec line carries the topic count), and
  the "recent route reports" line carries one line saying the page is not usually opened;
  none of these lines is numbered. `all-spec.*` lists every spec topic in the entrance's
  form and order (date, linked title) with a link back to the entrance, or "No documents
  yet." when there is none. A spec topic is a component's `prd.md`, or the top `prd.md`
  of a reference that has no component folder (titled by its first heading, else the
  reference's title). The top `prd.md` of a reference with components is a table of
  contents, not a topic: it is not counted in "N topics" or "N more" and only
  `all-spec.*` shows it, as one line above the topics (its first heading, else
  "Contents"; the day follows the topic rule). The recent
  route reports page lists the 20 most recent user-facing documents as a bulleted list
  (date, title, one-line summary, "+N more"; no numbers, which belong to the entrance's
  lines), with a link back to the entrance. Every link is a
  relative path; there is no absolute path, host name, user name, script or external
  resource in any file.
- **Reading rule.** A document row marked `audience: "user"` is user-facing; a row without
  that key (an older sealed cycle) follows the same name rule described above. The
  representative document per cycle is ranked by name only (`현황.md`/`STATUS.md`, then
  `final_report.*`, `report.*`, `summary.md`, `audit-report.md`, `prd.md`), never by a
  manifest `primary`. Manifests are read newest first, up to 300 or 16 MiB each; there is
  no full scan and no cache.
- **Row sources.** Rows come from sealed cycles today; a source of rows that are not cycle
  folders plugs in through `artifact_entrance.ROW_SOURCES`, and its rows join the same
  list. The spec topic rows (`canonical-topic`) go to the spec section (a reference's
  contents `prd.md` only to `all-spec.*`); every other row goes to the recent route
  reports page.
- **Commands.** `hearting reports` prints the entrance's numbered lines in the terminal
  (campaign summaries included; the derived chapters are named by their `.md` file) and
  `hearting open <number|words>` opens that line (a spec topic, a campaign folder, a page)
  or, with no argument, the browser entrance. A lone number that is not an entrance line
  prints one line on stderr and exits 1; it is never searched as a word. Words search the route reports and the spec
  topics; when several match, the list is printed without numbers and the words are to be
  narrowed. Both names are reserved by the one launcher (`tools/install/harness.sh`) in
  every runtime and are not installer subcommands.
- **Write order.** The chapters the entrance points at are written first and `README.md`
  and `index.html` last, so an interrupted publish never leaves an entrance pointing at a
  chapter that was not written.
- **Ownership.** Each file starts with `<!-- hearting:artifact-entrance v1 -->` (an HTML
  file keeps `<!DOCTYPE html>` above it). A same-named file without that marker, or a
  symbolic link at that name, is never overwritten, followed, or moved — it stays its
  owner's; this holds for all eight files. Rewriting is byte-deterministic: identical root
  content and declared language produce identical bytes.
- **First record.** When a root first shows a user-facing document, one call with a
  cycle id writes `.runtime/artifact-entrance.json`
  (`{"schema": 1, "first_cycle_id", "first_cycle_path"}`) once; it is never rewritten. A
  completion summary whose primary report lies in that cycle adds one "Artifact list:
  `<root>/README.md`" line above its artifact line. Only the first user-facing cycle
  qualifies, so a root whose entrance was made by hand from the CLI stays quiet.

### `backlog.md` — what was put off

- **Place.** One file at the artifact root: `<artifact root>/backlog.md`, for the whole repo.
- **Form.** A title line, then `갱신 YYYY-MM-DD HH:MM`, then one `## <campaign folder name>`
  heading per campaign, with `## Unassigned` for work with no campaign. A job is one top-level
  `- ` bullet (a title line of at most 60 columns) with optional indented `date:`, `source:`,
  `note:`, `kind:`, `area:` and `state:` lines (when it came up, where from, what the person said
  then, what kind of work it is, where in the repo it mainly lands, how far it is).
  The title line carries no tag, status or owner mark; the kind, area and state are those optional
  lines alone.

  ```text
  ## 2026-11-02_v4-open
  - Resuming a route loses its stage
    date: 2026-11-26
    kind: fix
    area: Routing
    state: discuss
    source: the person, while resuming
    note: what was seen, and what the person said then
  ```
- **`kind:` line.** An indented line under a job, `kind: <value>`; the value is `fix` (the flow or
  result in use keeps a problem, however it is to be solved), `task` (something to do: an
  improvement, a new piece, a test, a clean-up, a look into something; an observation that now has
  something to check is a task) or `note` (worth knowing, with no follow-up). The earlier value
  `improve` is read as `task`. Case and the blanks around the value are not read. A job with no
  `kind:` line, a value that is none of these, or two different kinds is unsorted (shown `TBD`):
  nothing is refused, warned or gated, and a document with no `kind:` line at all opens as before,
  every job unsorted. A `kind:` line counts only under a job (not under a heading or other text,
  not inside a code fence).
- **`area:` line.** One area per job, the part of the repo the work mainly lands in (where it
  spans several, the one to look at first). Case and runs of blanks are folded, so `routing` and
  `Routing` are one group; the first spelling the document writes is the group's name. A job with
  no `area:` line, an empty one or two different areas stands in `No area`. A repo keeps to a
  handful of names (six to ten); a new name is simply a new group.
- **`state:` line.** `discuss` (the direction is to be settled together; there is no concrete
  question to put yet), `ready` (the next step is written down; starting it follows the normal
  flow) or `waiting` (it waits on something that can be named: an earlier job, an environment, a
  time). No line, another value or two different values is no state (shown `—`). Once a concrete
  choice or confirmation needs the person's answer, it is not put here:
  register it as a question, which stands on the question screens (the status document does not
  list it).
- **`date:` line.** `YYYY-MM-DD`, the day the job came up; a date that does not exist or two
  different dates is no date (shown `—`). A wrong value in any of these lines leaves only that
  cell undecided; the document still opens.
- **Counting rule.** Lines are read by their shape, not by what Markdown makes of them (lines end
  as Python's `str.splitlines` ends them). A job is a line whose first two characters are `- `,
  below the first heading line and outside a code fence.
  - A heading line is `##` followed by a space, a tab or the end of the line, after at most three
    spaces: ` ## C` is a heading, four spaces make it no heading, and `###` is none.
  - A `- ` line before the first heading is not a job. Neither is any line that does not start
    with `- ` at its left edge: `*`, `+` and numbered items, `-` with no space after it,
    indented lines, sub-bullets, `> - `.
  - Markdown's meaning is not read: `- - -`, and a `- ` line inside an HTML comment, each count
    as a job.
  - A fence line is at most three spaces, then three or more of one character, `` ` `` or `~`;
    the rest of an opening line is not read. A fence closes only with a line of the same
    character, at least as long, followed by nothing but whitespace; an unclosed fence runs to
    the end of the file. Fence lines and everything between them are not counted and are no
    heading.
- **`갱신` line.** Read only before the first heading line and outside a code fence, in the shape
  `갱신 YYYY-MM-DD HH:MM` (ASCII digits) with a date and time that exist; surrounding
  whitespace is ignored. The first line of that shape and a real time is the date; any other
  line, or `갱신 2026-13-45 99:99`, is no date.
- **Who and when.** The session that puts a job off adds one line, at that moment, under the
  campaign's heading (and refreshes `갱신`), with its `date:` line and its `kind:` line then, and
  an `area:` and a `state:` line where they are known (each left out when it is not known). A
  person writes it by hand.
- **Done.** The line is deleted. The file holds open work only.
- **Readers.** The panel's BACKLOG part, its board and the entrance only read it; no code writes
  it. All three read the jobs and their lines by this one rule (`utilities/backlog_items.py`) and
  count each kind (`FIX`, `TASK`, `NOTE`, `TBD`) and state the same. The entrance reads it by
  name, never through a link, at most 1 MiB, the panel's own limit. No command, setting or step is
  added.

### Optional local artifact viewer

`hearting view` serves the existing entrance and artifact files read-only on
`127.0.0.1:8765` by default in the foreground; Ctrl-C stops it. If that port
is occupied, `hearting view --port 8766` selects another port, or `--port 0`
asks the OS for a free one. Use the URL printed by the viewer; custom ports
are not discovered by `hearting open`. It uses only the Python
standard library and never generates or updates the entrance. Directory browsing
is available when the entrance is absent. HTTP access excludes `_internal`,
`.runtime` and `_scratch` at every depth and rejects symbolic links and special
files. Host and path validation and descriptor-relative no-follow reads protect
the boundary. These HTTP exclusions do not change artifact classification.

While a viewer for the same root is running, `hearting open` additionally shows
its local URL. The original path remains available and all entrance operations
continue without the optional viewer. Entrance files retain relative links. HTML
is served unchanged in a sandbox; absolute filesystem links inside existing HTML
are not rewritten. Markdown is rendered at its original URL with a raw view.

### Shared spec revisions and the latest view

A spec revision's `files[]`, `file_count`, `byte_size` and `content_digest`
describe its complete logical inventory. Physical storage is a separate fact.
Full-tree revisions remain readable. Schema 1 `stored_in` maps a logical path
to the same path in an earlier revision; schema 2 maps it to
`{revision_id, source_path}`. Each source is a regular file in an earlier
revision of the same reference, with the declared digest and size. A source
names actual bytes, not another storage reference. Logical revision identity
and the physical provider's identity remain distinct.

The common reader resolves inventory, files, transaction seeds and history
before a delta writer is introduced. Missing, corrupt, linked or ambiguous
storage remains an integrity error, rather than a successful partial tree.
Unselected components, exact preimages, explicit deletion, merge/CAS and
publication proof keep their existing meanings. New storage leaves old
immutable revisions untouched and measures physical and logical size separately.

`shared/spec/<ref>/current/` is a derived latest view, maintained by the normal
writer with atomic file replacements and the latest ID/content digest. It is
neither authoring source nor historical proof. A missing or drifting view reads
through the verified immutable revision. Read-only lookup does not repair it;
view failure does not overturn an already admitted revision. Directory-copy
and external mirror consumers need the logical reader before delta publication.
Canonical-topic entrance rows use the existing reference/component and logical
revision, retaining unknown ownership instead of deriving new IDs from paths.

`artifact_reader.py resolve --artifact-root ROOT --path <logical-path>` returns
the logical revision and verified physical source separately. `spec-dir` and
`bucket-dirs --bucket spec` expose `logical_files` with their exact source paths,
digests and sizes, plus `physical_complete`. Directory locations are not a promise
of a full physical copy. File and directory-copy consumers use these addresses;
they must not infer a revision ID from `current` or copy a sparse revision blindly.
The viewer serves the same logical files and directory inventory without
materializing them. Exact read markers continue to name immutable PRD sources.
Changed public files store full bytes; new history slots can reuse a matching
old PRD at a different source path. Single-component logical behavior is preserved
while versioned physical storage and its separate size metrics can change.

## Internal files (no reader duty)

- `.runtime/artifact-producer/v1/artifact-meta-state.json` (`artifact-meta-state/v1`):
  per-branch and per-campaign number high-water, every issued short ID with its owner,
  and the source of project fields.
- `.runtime/artifact-producer/v1/artifact-meta-transactions/`: at most one write-ahead
  intent per write, plus staging for history lines (see *Write path*).

```json
{"schema_version":1,"contract":"artifact-meta-state/v1","artifact_root_id":"root_11111111111111111111111111111111","branch_high_water":{"CMD":3,"OPS":2},"cycle_high_water":{"camp_22222222222222222222222222222222":4},"project_source":{"display_name":{"by":"human","at":"2026-10-01T09:00:00Z"},"branches.CMD":{"by":"agent","at":"2026-10-01T09:00:00Z"}},"issued":{"CMD-03":{"kind":"campaign","id":"camp_22222222222222222222222222222222"},"OPS-02":{"kind":"campaign","id":"camp_22222222222222222222222222222222"},"CMD-03.4":{"kind":"cycle","id":"cyc_33333333333333333333333333333333"}}}
```

## Short IDs

- Campaign = representative branch code + (highest number ever used with that code) + 1.
  Cycle = campaign ID + `.` + the next number inside that campaign (start order when
  several are numbered together). A removed, renamed, merged, or replaced ID is never
  issued again to anyone else; its owner may take it back.
- The ID changes when a person edits it, when the representative branch changes (the new
  branch's next number), or when a cycle's current campaign changes (the entry follows,
  with the new campaign's next number). The old ID goes to `aliases`. Only the newest 16
  aliases stay public; every issued ID stays in the internal state, so ownership is kept
  and Cairn can find an ID by `aliases` only within those 16.
- The stable `camp_…`/`cyc_…` is the identity; links never use a short ID.
- A missing state is rebuilt from the public files and from the history lines; a damaged
  state is a typed failure and is not replaced.

## Who wrote a field (`source`)

`source.<field> = {by: rule|model|human|agent, at: UTC RFC 3339}` per field; session and
reason live only in the history line. The model never overwrites a field whose source is
`human` or `agent`, or whose source is unknown; it fills the other fields. `release`
(`--field title` …) keeps the value and hands the field back to the model (`short_id`
goes back to `rule`); the next ordinary judgement refills it. A rewrite with an
unchanged value changes neither value, source, time, nor history; a person who sets a
model-written value to the same text only takes it over (the source becomes `human`, one
history line for the source). A short ID fixed by a
person also holds the model's change of the representative branch that would renumber it.

An old display-declaration title carries no source, so it counts as a person's title: the
first writer copies it into `meta.json` as `human` and the automatic review never overwrites it
(`release` hands it back). A declaration that cannot be read protects every campaign's
title the same way. The one exception is the explicit, supervised backfill
(`artifact_workflow_group_review.py sweep --campaign … --replace-legacy-titles`, refused with
`--auto`): it shows the old title to the model as `previous_title` and lets a plain title
replace a title that only the old declaration holds (none in `meta.json` yet, or the copied
`human` value still equal to it). The history line keeps the old value; a title a person set
through `artifact_meta.py` differs from it and stays protected.

## The history line: the one recorder

`utilities/artifact_history.py`: `make_event(...)` builds and validates one event,
`publish_events_locked(root, events)` publishes events and **assumes the producer
admission lock is held** (a caller already inside the lock, such as cycle-flex, calls it
there). `publish_events(root, events)` is the thin wrapper for a caller outside the lock:
it takes the same lock, then calls the locked one. Every change to producer metadata
records its meaning with these; lifecycle, file, and flow changes (the sealing-removal
work, `cycle-flex-spec`) use the same functions. There is no second recorder and no
second change signal: the same lines are what Cairn watches.

Location: `.runtime/artifact-producer/v1/history/YYYY-MM/<event_id>.jsonl` (UTC month of
`at`). One file holds exactly one LF-terminated JSON line. A published file is never
modified, truncated, renamed, or deleted; a new month starts a new directory; there is
no destructive rotation or retention. File names and directory order are **not** a
global sequence; use `at`. Re-publishing an event with the same bytes is harmless (no new
file), the same `event_id` with other bytes is a conflict.

`history/LATEST.json` = `{"event_id", "at", "count"}`: the last event a publish created, its
`at`, and the number of event files. It is rewritten (same-directory temp file, fsync,
atomic rename) inside the admission lock after a publish created new files, and not at
all when nothing new was written. It is a convenience signal for a watcher: if that update
fails the publish still succeeds and the next publish brings it back in step.

A publish that fails raises `HistoryPublishError` (a `HistoryError` with `.code`:
`admission-lock-required`, `admission-busy`, `event-invalid`, `event-conflict`,
`event-duplicate`, `history-unreadable`, `history-write-failed`); nothing is hidden. The
caller decides: cycle-flex leaves its manifest unchanged so the next change attempt finds the
same change again; a command keeps the unpublished line (`history_pending`) and the next
trigger publishes it again, which is safe because of the identical-bytes rule.

Keys, in this order: `schema_version`, `contract`, `event_id`, `transaction_id`, `at`,
`actor`, `kind`, `target`, `operation`, `field`, `before`, `after`, `reason`.

- `actor` = `{by, session, harness, route, attempt}`: `by` = `rule` | `model` | `human` |
  `agent`; the other four are plain tokens or `null` and none is required. `actor_from_env()`
  gives the default: an agent session environment (`AGENT_DISPATCH_ATTEMPT_ID`,
  `AGENT_DISPATCH_CURRENT_HARNESS`, `AGENT_ROUTE_ID`, …) → `by=agent` with those markers
  (`session` = the attempt id), otherwise `human` (`actor_from_env(default_by=…)` changes
  that fallback). A change the runtime merely observed passes `by=rule` itself with the
  session and route it saw (`make_actor("rule", session=…, route=…)`).
- `kind` = `meta` | `group` | `flow` | `artifact` | `lifecycle`. `target` = `{type, id, path}`
  with `type` = `campaign` | `cycle` | `project` | `group` | `flow` | `artifact` and a
  root-relative POSIX `path`.
- `lifecycle` (target `cycle` or `campaign`) has `field` = `state` | `campaign` (membership
  move) | `parent` | `disposition` (discard/supersede mark) | `path` (folder name or
  location) | `primary` (representative artifact re-pointed, short `{value}` refs). A cycle close is `operation=update`, `field=state`, `after.value` =
  `{state: completed|abandoned, manifest_digest, revision_id, files, excluded}`; a campaign
  close/reopen is `update` of `state` with plain state names; a delete is `operation=delete`,
  `field=state`, `before.value` = `{manifest_digest: sha256:…|null, path}`, `after.value` null.
  A file added, changed, or deleted after a close stays `kind=artifact`, `target.type=artifact`,
  `field` = the file's relative path, `before`/`after` = digest and size.
- `operation` = `add` | `update` | `move` | `delete`. `field` is a field path
  (`campaign.title`, `cycles.<cycle_id>.summary`, `branches.CMD`, `groups.<group_id>`) or,
  for a file, its root-relative path.
- `before` / `after` = `{"value": <JSON>}` when the canonical JSON (sorted keys, compact)
  is at most 512 bytes, otherwise `{"digest": "sha256:…", "bytes": n}`; file bytes are
  always digest and size; absent is `{"value": null}`.
- `transaction_id` (`htxn_…`) ties the lines of one change together; `event_id` is
  `hevt_…`. `reason` is the caller's sentence, otherwise the command or trigger name.

```json
{"schema_version":1,"contract":"artifact-history/v1","event_id":"hevt_44444444444444444444444444444444","transaction_id":"htxn_55555555555555555555555555555555","at":"2026-10-01T09:00:00Z","actor":{"by":"agent","session":"att-3521eef711234e77a156c5d972e7f628","harness":null,"route":null,"attempt":null},"kind":"meta","target":{"type":"campaign","id":"camp_22222222222222222222222222222222","path":"campaigns/2026-10-01_example/meta.json"},"operation":"update","field":"campaign.title","before":{"value":"명령어 모델 실험"},"after":{"value":"명령어 모델 집 적응 비교"},"reason":"사용자 요청에 따른 제목 수정"}
```

A cycle close:

```json
{"schema_version":1,"contract":"artifact-history/v1","event_id":"hevt_66666666666666666666666666666666","transaction_id":"htxn_77777777777777777777777777777777","at":"2026-10-01T10:30:00Z","actor":{"by":"agent","session":"att-3521eef711234e77a156c5d972e7f628","harness":"claude","route":"rt-379f8a5e9ad09c54","attempt":"att-3521eef711234e77a156c5d972e7f628"},"kind":"lifecycle","target":{"type":"cycle","id":"cyc_33333333333333333333333333333333","path":"campaigns/2026-10-01_example/2026-10-01_first-cycle"},"operation":"update","field":"state","before":{"value":"open"},"after":{"value":{"state":"completed","manifest_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","revision_id":"rev_20261001T103000Z","files":12,"excluded":0}},"reason":"route rt-379f8a5e9ad09c54 closed the cycle"}
```

DESIGN §6.1 fixed only "an append-only line under `history/` with when, who, what,
before/after, and why". The one-file-per-event layout, the key list, and the id prefixes
above are this document's concretization of that unspecified part, not a change to any
agreed field. A change line is published after the changed files are in place, so a
reader that sees a line finds the new state.

## One command for people and agents

`python3 utilities/artifact_meta.py <subcommand> --artifact-root ROOT` (JSON on stdout;
a refusal is `{"status":"blocked","code":…}` and exit 65). Stable IDs are the default
selector; a short ID or alias works when it names one owner.

| Subcommand | Meaning |
|---|---|
| `show --campaign ID [--cycle ID]` / `show --project` | Values, sources, aliases. |
| `set --campaign ID [--cycle ID] [--title] [--summary] [--branches A,B] [--kinds 학습,…] [--short-id ID] [--presentation-kind archive_bundle] [--by human\|agent] [--reason] [--session] [--dry-run]` | Change the given fields; each changed field is one history line. `--presentation-kind` is campaign-only (refused with `--cycle`) and official marks only. |
| `set --project --display-name TEXT` | Project display name. |
| `release --campaign ID [--cycle ID] --field F … [--dry-run]` | Hand a field back to the model; the value stays. |
| `branches list` / `add --code --label [--note]` / `import --input FILE` / `rename --code OLD [--new-code NEW] [--label] [--note]` / `merge --from OLD --into DEST` / `remove --code CODE` | Vocabulary. `import` takes `{"branches":[…]}` once, adds only, and is a no-op on rerun. A rename or merge rewrites every tag and, for a changed representative, the IDs. `remove` takes only unused codes; `ETC` cannot be renamed, merged away, or removed. |

`--by`, `--reason`, and `--session` are optional. The command has no way to move a cycle,
edit a manifest, or change a route.

Experiment condition names and their comparison basis have their own command,
`python3 utilities/experiment_catalog.py` (see the `experiment-catalog/v1` section above).

## The one background review

Fleet titles/NOW and this review share one text-only provider call. Each uses
a short dedicated system instruction, no tools, hooks, MCP or user/project
bootstrap, and the resolved profile's effort/variant as well as its model.
Authentication still uses the subscription CLI; provider allocation stays the
same. Fleet's ordinary working-session refresh interval is five minutes;
registered summaries retain their existing initial/periodic/final lifecycle.
The first title/NOW and the existing final refresh retain priority through the
provider's rolling-start admission. Ordinary refreshes and metadata reviews
leave up to four starts in that same title-class window for priority calls.
The default rolling budget is 24 starts per ten minutes; the four-worker
concurrency cap and user overrides still apply.
A missing NOW keeps that priority on the existing scheduler's retry. Once a
NOW exists, periodic retries and title-language repairs remain ordinary calls,
including when a failed repair clears the title. Admission failures remain
visible in `summary_error`. No new retry loop is added.
In auto language mode, native command metadata is not user intent. A main
session's observed user language travels in the existing summary-source
provenance; a worker with no user-language signal uses the latest such main
observation for its title and NOW. No setting or sidecar field is added.

`artifact_workflow_group_review.py` (the existing job; see
[WORKFLOW_GROUPS.md](WORKFLOW_GROUPS.md)) decides, with **one model call per campaign per
sweep**, the title, summary, branches, and kinds of the campaign and of every target
cycle. Code, not the model, owns IDs, sources, times, counters, the vocabulary check, and
history. The model chooses tags from the vocabulary or proposes at most one new branch (up
to twelve when the vocabulary is empty). The review does not read or write
`workflow-groups.json`; existing declarations stay as they are. Past cycles are never
filled in automatically.

A failed call, an invalid answer, an unreadable `meta.json`, a conflict, or a busy lock writes
no metadata, vocabulary, number state, or history line and is retried at a
later seal under the existing retry rules; the seal itself is never affected. (The review's
own judgement record and counters keep working as before.) `--dry-run` writes nothing at all.
`HEARTING_WORKFLOW_GROUP_REVIEW=off` disables the whole automatic job;
`HEARTING_CAMPAIGN_TITLE_AUTO=off` keeps only the automatic **campaign title** out of it
(everything else is still judged); explicit sweeps and a person's `set` ignore it.

## Write path and failure semantics

Every write — judgement, `set`, `release`, vocabulary — runs under the producer admission
lock: re-read current files, apply the rules (protected fields, vocabulary, numbers),
write **one intent file** holding the exact new bytes and the history lines (the commit
point), replace each file whose digest still matches the intent, publish the lines
(existing identical lines are skipped), remove the intent. A write that stops or loses
the disk after the commit point is **finished by the next write** (roll-forward; nothing
is rolled back, no model is called again, no line is duplicated). A write that fails before
the commit point leaves no trace. If another writer changed one of the files, nothing is
replaced and the stale intent is dropped with a typed `intent-conflict`; the other writer's
file is kept.

`show`, `branches list`, `--dry-run`, and every reader never recover, never take the lock,
and never create a file; they only report `pending_recovery`. Limits: several files cannot be
replaced as one atomic step, so between the first and last replacement of an interrupted
write a reader may see the new `meta.json` before the new number state; a disk that keeps
failing or a foreign writer is reported with a typed code and retried at the next write.

## Journal records

The common journal row has `kind=journal-entry`, opaque `artifact_id` and
`revision_id`, a root-relative `path`, exact `anchor`, `record_target`,
`result_targets`, original `session`/`turn`/`at`, `status`, and `coverage`.
Partial scans and unreadable/corrupt evidence remain partial/unknown; they do
not disappear into an empty-success result. Result versions are commit/hash or
unknown, never inferred from a path or mtime. A versioned journal finish binds
one immutable entry revision digest. Appending another entry cannot change that
proof. Confirmed corrections append provenance and keep historical revisions;
hand-edited drift is reported without inventing a correction event.
