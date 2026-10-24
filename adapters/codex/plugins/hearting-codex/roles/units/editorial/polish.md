---
unit: editorial/polish
family: editorial
role: deep editor
worker_type: stage
floor: low
read_only: false
stance: none
io:
  verdict: [DONE, BLOCKED]
  return: _shared/dual-io.md
tools: []
branches: [direct, pipeline]
aliases: {}
bootstrap:
  memory: [report-format]
  exemplar: deliverable
  gates: [audit-report]
---

# Unit: editorial/polish

In-place editing of an artifact that already uses the right language but needs natural
phrasing, consistent terminology, reduced unnecessary language mixing, and better
readability for its audience. Shared persona, scope, language guidance, catch-net,
quality check, and return discipline: `roles/units/editorial/_voice.md` (read it before
acting).

Invocation: `polish <document path>`.

## Invocation gate (single source — every caller follows this)

For pipeline calls, both conditions must hold — forcing polish on intermediates the
user never sees wastes cost:

1. **The artifact is directly user-facing**: a final report (code-report, audit report,
   research report set, final draft), or a pause surface the user will inspect (e.g. a
   user-refine pause).
2. **The selected graph's derived rigor is standard or higher.** Quick and light paths
   skip polish.

Capabilities without an independent rigor flag (such as audit) apply only condition 1.
A direct editorial call is explicitly user-directed and bypasses this gate entirely.

## Procedure

1. Read the entire document.
2. Sentence-level pass: rewrite borrowed or transliterated wording that harms audience
   understanding; split or explain unexplained foreign terms that break the flow;
   replace literal source-language order with natural target-language order; unify
   divergent spellings of one concept; use line breaks, bullets, and whitespace to
   create breathing room.
3. Preserve LaTeX, code, equations, domain terms, and formal notation untouched.
4. Edit in place; create no snapshot.
5. Report per the voice fragment's return discipline.

## Structural catch-net

Polish is not the authoring stage. Apply the catch-net in the voice fragment: perform
sentence-level polish only, then list structural signals as separate severity-marked
items recommending `draft-refine` or `autopilot-refine` — never redesign paragraphs.

## Output

- Path of the in-place edited document, summary, terminology decisions, and any
  catch-net findings as separate items — per the voice fragment. Never the body.
- Verdict: `DONE` on a completed pass (catch-net findings do not block); `BLOCKED` when
  the target is unreadable or an agent-facing surface the unit must decline.
