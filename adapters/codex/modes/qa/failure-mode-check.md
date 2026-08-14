# Codex Qa Failure Mode Check Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/qa/failure-mode-check.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info qa/failure-mode-check`.
4. Obey the reported status, tool contract, runtime surface, and fallback before claiming support.

## Codex Runtime Mapping

- Status: `portable`
- Realization: `portable-persona`
- Requirement: read-only review with Codex file/test tools
- Note: Codex may use the mode fragment after reading roles/MODES.md and resolving portable roles.

## Use

- Use Codex file, terminal, approval, sandbox, hook, and skill surfaces.
- Run `adapters/codex/bin/preflight.sh write <file> [session-id]` before edits.
- For `tool-contract` modes, run the named contract check before claiming the tool-backed result.
- If a required local provider or executable is unavailable, report the unavailable contract instead of silently downgrading.
- Treat `adapters/codex/modes/qa/failure-mode-check.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/qa/failure-mode-check.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: qa/failure-mode-check
family: qa
role: fast reviewer
worker_type: review
floor: moderate
read_only: true
stance: _shared/stance.md
io:
  verdict: [findings, none]
  return: _shared/dual-io.md
tools: []
branches: [pipeline]
aliases: {}
---

# Unit: qa/failure-mode-check

Advisory failure-mode review for a parallel peer group. Runs as an auxiliary
leg: its verdict is structurally non-blocking — the enum holds only
`findings`/`none`, so it can surface failure modes without ever holding the
stage's completion gate alone.

## Scope

Enumerate plausible failure modes of the proposed approach: crash paths,
partial-failure handling, degraded-input behavior, and what happens when a
downstream consumer is absent or late. Report each as a finding with its
evidence and, where possible, the mitigation already present.

## Output

- `none` when no failure mode needs a second look.
- `findings` when failure modes deserve the arbiter's attention. Findings feed
  the peer group's `auxiliary_findings_considered` merge; they can never block
  the stage by themselves.
