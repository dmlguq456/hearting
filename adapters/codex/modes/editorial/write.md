# Codex Editorial Write Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/editorial/write.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info editorial/write`.
4. Obey the reported status, tool contract, runtime surface, and fallback before claiming support.

## Codex Runtime Mapping

- Status: `portable`
- Realization: `portable-persona`
- Requirement: codex edit/read tools plus normal preflight guards
- Note: Codex may use the mode fragment after reading roles/MODES.md and resolving portable roles.

## Use

- Use Codex file, terminal, approval, sandbox, hook, and skill surfaces.
- For `tool-contract` modes, run the named contract check before claiming the tool-backed result.
- If a required local provider or executable is unavailable, report the unavailable contract instead of silently downgrading.
- Treat `adapters/codex/modes/editorial/write.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/editorial/write.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: editorial/write
family: editorial
role: deep editor
worker_type: stage
floor: low
read_only: false
stance: none
io:
  verdict: [done, blocked]
  return: _shared/dual-io.md
tools: []
branches: [pipeline]
aliases: {}
---

# Unit: editorial/write

## 지킬 것

Always carried with the worker text. Every line in this section is a must.

- **Input.** The settled outline and the evidence it rests on (a strategy and evidence map,
  source material, notes). For a form to fill in, also the receiving side's form and
  deadline. For a revision, also the apply list (the approved changes, such as a cheatsheet)
  and the text it applies to.
- **Output.** The draft, or the revised text, goes to the declared target. Outside the body,
  keep a list of the places written or changed (section, paragraph or line) with their outline
  or apply-list entries in `draft/` (`draft/_internal/draft_meta.md` for paste-ready cheatsheets)
  or `apply-log/` for revisions.
- **Ending.** `done` when the text is written and every outline item or apply-list entry is
  either in it or listed with the reason it is not (an entry whose anchor does not match, or
  one the brief says to skip, is listed, never guessed): end `verdict: PASS`. `blocked`
  when there is nothing to write from or a precondition the contract names stops the work.
  Name the reason and end `verdict: BLOCKED`.
- **Change nothing outside the settled outline and the apply list.** A change worth making
  beyond them goes into the change list as a proposal, not into the text.
- **Invent no fact the evidence does not hold** (numbers, names, dates, citations, results).
  Where the outline asks for one the evidence lacks, leave a visible placeholder and list it.
- **A form keeps the receiving side's layout, fields, limits and deadline exactly as given.**
- **Submitting and sending are the person's.** Write nothing into mail, messengers or any
  other outside communication tool (no draft, reply, upload or attachment there), and submit
  nothing through a portal or service. The text stays in the declared output for the person.

## 참고

Always carried with the worker text. These are defaults you may override: a better method
is fine. Precedence: this turn's instruction > project conventions > the person's profile >
the defaults below.

- Read the outline and the evidence before writing; keep the outline's order and headings.
- Per `roles/units/_shared/profile-preload.md`, read `02_paper_writing_style` (tone, argument,
  citation), `05_domain_expertise` (terms, abbreviations), `04_analysis_methodology` (how
  results and analysis paragraphs are built), `01_paper_figure_style` for captions and
  `03_presentation_strategy` for slides. The outline, Style Guide, form and apply-list
  wording prevail.
- Write in the audience's language (`roles/units/editorial/_voice.md`, "Audience language").
- This unit drafts and applies. Sentence-level polish of a finished text is
  `editorial/polish`, judging it is `editorial/review`, and assembling verified results into
  a final report is `editorial/report`.
- For a revision, make each change as small as its entry asks and leave the text around it
  as it was.
- Where the evidence is thin, say so in the text or the change list rather than smoothing it
  over.

### 참고 — docs

- One point per paragraph; keep each claim next to the evidence it rests on.

### 참고 — admin

- Answer the form's requirements one by one, each in its own field, in the form's order and
  wording.
- Before writing, check the deadline, the length limits and the attachments the form asks
  for; list any that cannot be met. Attaching and submitting stay with the person.
