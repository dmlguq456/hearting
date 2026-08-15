---
unit: qa/simplicity-check
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

# Unit: qa/simplicity-check

Advisory simplicity cross-check for a parallel peer group. Runs as an auxiliary
leg: its verdict is structurally non-blocking — the enum holds only
`findings`/`none`, so it can surface complexity without ever holding the
stage's completion gate alone.

## Scope

Read the peer group's primary artifact for avoidable complexity: gold-plating,
unneeded indirection, duplicated mechanism, and scope that outruns the stated
contract. Report each as a finding with its evidence and a concrete simpler
alternative where one exists.

## Output

- `none` when the artifact stays within its necessary complexity.
- `findings` when simplicity issues deserve the arbiter's attention. Findings
  feed the peer group's `auxiliary_findings_considered` merge; they can never
  block the stage by themselves.
