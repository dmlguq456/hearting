# Codex Qa Edge Case Check Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/qa/edge-case-check.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info qa/edge-case-check`.
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
- Treat `adapters/codex/modes/qa/edge-case-check.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/qa/edge-case-check.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: qa/edge-case-check
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

# Unit: qa/edge-case-check

Advisory edge-case sweep for a parallel peer group. Runs as an auxiliary leg:
its verdict is structurally non-blocking — the enum holds only
`findings`/`none`, so it can surface boundary risk without ever holding the
stage's completion gate alone.

## Scope

Probe boundary conditions: empty/degenerate inputs, off-by-one and rounding
edges, failure under resource exhaustion, and state transitions at the borders
of the stated contract. Report each as a finding with its evidence.

## Output

- `none` when no boundary case needs a second look.
- `findings` when edge cases deserve the arbiter's attention. Findings feed
  the peer group's `auxiliary_findings_considered` merge; they can never block
  the stage by themselves.
