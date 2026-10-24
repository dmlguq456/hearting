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
