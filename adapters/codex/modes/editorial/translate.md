# Codex Editorial Translate Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/editorial/translate.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info editorial/translate`.
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
- Treat `adapters/codex/modes/editorial/translate.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/editorial/translate.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: editorial/translate
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
---

# Unit: editorial/translate

Meaning-preserving translation that creates a target-language mirror of an artifact.
Shared persona, scope, language guidance, quality check, and return discipline:
`roles/units/editorial/_voice.md` (read it before acting).

Invocation: `translate <source path> → <target path>`, any language pair.

**When**: only when the artifact's primary language differs from the requested target.
Explicit publication language, external audience, or artifact contract wins; otherwise
the target is the user's current communication language.

## Procedure

1. Read the entire source before translating — never start by converting paragraph by
   paragraph.
2. Understand one section at a time, then write the target-language prose from meaning,
   fresh — never carry over source word order or connectors (no 1:1 literal
   translation).
3. Self-check terminology and mixed-language consistency (grep) against the voice
   fragment's language rules.
4. Finish only when the target passes the single quality check: it reads naturally in
   one pass without consulting the source.

## Output

- Create the new target-language mirror; return its path plus the summary and
  terminology decisions per the voice fragment's return discipline. Never the body.
- Verdict: `DONE` on a completed mirror; `BLOCKED` when the source is unreadable or the
  target is an agent-facing surface the unit must decline.

Translate creates a mirror. `editorial/polish` instead edits an already
target-language artifact in place (no snapshot).
