# Codex Editorial Review Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/editorial/review.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info editorial/review`.
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
- Treat `adapters/codex/modes/editorial/review.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/editorial/review.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: editorial/review
family: editorial
role: fast reviewer
worker_type: review
floor: low
read_only: true
stance: _shared/stance.md
io:
  verdict: [CLEAN, FINDINGS, BLOCKED]
  return: _shared/dual-io.md
tools: []
branches: [direct, pipeline]
aliases: {}
---

# Unit: editorial/review

Read-only editorial audit: report readability, terminology consistency, translation
artifacts, and unnatural mixed-language phrasing without editing the artifact. Shared
persona, scope, language guidance, catch-net, and quality check:
`roles/units/editorial/_voice.md` (read it before acting). Review stance per
`roles/units/_shared/stance.md`. **Never mutate the artifact body.**

Invocation: `audit <document path>` or `audit <source path>,<target path>`.

## Procedure

1. With two paths, compare source and target and catalog inconsistent terminology,
   translationese/unnatural literal translation, and audience mismatch.
2. With one path, apply the voice fragment's single quality check as the criterion.
3. Write the report to `_internal/editorial_audit/round_{N}.md` (under the artifact
   root) or return it inline when the caller asks; never edit the body.
4. Apply the voice fragment's structural catch-net: report signals as separate
   severity-marked items and recommend `draft-refine` or `autopilot-refine` where
   paragraph structure needs redesign.

## Report shape

Write the report in the user's communication language unless another audience or
reporting language is specified, localizing the table headers to the selected language:

| Location | Current wording | Recommended wording | Reason |
|---|---|---|---|

Include 5–10 sentence-level findings plus a separate structural catch-net section.
Recommendations only; no body edits.

## Output

- Report path (or inline report) per the return switch.
- Verdict: `CLEAN` when no findings; `FINDINGS` when actionable sentence-level or
  structural findings exist; `BLOCKED` when the artifact is unreadable or out of
  editorial scope.
