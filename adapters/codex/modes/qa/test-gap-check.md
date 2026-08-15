# Codex Qa Test Gap Check Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/qa/test-gap-check.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info qa/test-gap-check`.
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
- Treat `adapters/codex/modes/qa/test-gap-check.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/qa/test-gap-check.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: qa/test-gap-check
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

# Unit: qa/test-gap-check

Advisory test-gap sweep for a parallel peer group. Runs as an auxiliary leg: its
verdict is structurally non-blocking — the enum holds only `findings`/`none`,
so it can surface coverage gaps without ever holding the stage's completion
gate alone.

## Scope

Compare the peer group's primary artifact against its verification claims:
missing tests for new paths, assertions that cannot fail, coverage of the
failure modes the artifact names, and verification that is asserted but not
demonstrated. Report each as a finding with its evidence.

## Output

- `none` when verification coverage is consistent with the artifact's claims.
- `findings` when test gaps deserve the arbiter's attention. Findings feed the
  peer group's `auxiliary_findings_considered` merge; they can never block the
  stage by themselves.
