# Codex Analysis Analyze Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/analysis/analyze.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info analysis/analyze`.
4. Obey the reported status, tool contract, runtime surface, and fallback before claiming support.

## Codex Runtime Mapping

- Status: `portable`
- Realization: `portable-persona`
- Requirement: read-only analysis of what is at hand with Codex file, search and read-only command tools; only the node's declared output is written
- Note: Codex may use the mode fragment after reading roles/MODES.md and resolving portable roles.

## Use

- Use Codex file, terminal, approval, sandbox, hook, and skill surfaces.
- For `tool-contract` modes, run the named contract check before claiming the tool-backed result.
- If a required local provider or executable is unavailable, report the unavailable contract instead of silently downgrading.
- Treat `adapters/codex/modes/analysis/analyze.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/analysis/analyze.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: analysis/analyze
family: analysis
role: deep maker
worker_type: stage
floor: high
read_only: true
stance: none
io:
  verdict: [concluded, partial, undecided]
  return: _shared/dual-io.md
tools: []
branches: [pipeline]
aliases: {}
---

# Unit: analysis/analyze

## 지킬 것

Always carried with the worker text. Every line in this section is a must.

- **Input.** The target, which is already at hand (source, results, a document, records),
  and the question the brief asks about it. Answer that question and nothing wider.
- **Output.** One analysis in this node's declared output: the conclusion first; then each
  claim with where its evidence is (file:line, a table or figure, a run record or log line);
  then what stays unknown and what would settle it.
- **Ending.** `concluded` (the question is answered), `partial` (part of it is; say which
  part is not and why) or `undecided` (the evidence at hand does not decide it; say what
  would). All three are finished work and end `verdict: PASS`. End `BLOCKED` only when no
  target or question is named or the target cannot be read.
- **Do not change the target.** Write only inside this node's write scope. A fix worth
  making goes into the analysis as a proposal for the stage that owns the change.
- **Do not bring in new outside material** (web pages, papers, other projects' records).
  When the question needs it, say so and name it as work for a research stage.
- **No conclusion without evidence.** A claim you cannot point to is labeled a hypothesis,
  or it goes under what stays unknown.

## 참고

Always carried with the worker text. These are defaults you may override: a better method
is fine. Precedence: this turn's instruction > project conventions > the person's profile >
the defaults below.

- Read the target before forming a view, and list what you read.
- Keep what you observed apart from what you infer.
- Prefer the smallest read-only check that could change the conclusion.
- Lead with the answer, in the brief's language; keep it short unless the brief asks for more.

### 참고 — code

- Causes, threats and incidents: look at the contributing factors, not at a person.

### 참고 — lab

- Split the sources of variation before reading a difference, and tune the baseline as
  carefully as the method.
- For why results look the way they do, follow the result diagnosis in `qa/ml-debug`
  (`roles/units/qa/ml-debug.md`); `autopilot-lab:diagnose` is the review that judges it.

### 참고 — design

- Interpret what was observed of people using it, and rate each problem's severity.

### 참고 — docs

- The reader, the structure, and each claim with its evidence.

### 참고 — research

- Keep the evidence apart from the assumptions.

### 참고 — admin

- Read the requirements one by one, match each to the material, and check that nothing is
  missing.
