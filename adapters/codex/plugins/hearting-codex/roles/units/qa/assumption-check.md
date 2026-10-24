---
unit: qa/assumption-check
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

# Unit: qa/assumption-check

Advisory assumption cross-check for a parallel peer group. Runs as an auxiliary
leg: its verdict is structurally non-blocking — the enum holds only
`findings`/`none`, so it can surface unchecked assumptions without ever holding
the stage's completion gate alone.

## Scope

Check the plan's or artifact's stated assumptions: inputs believed present,
invariants taken for granted, environmental preconditions, and scope edges.
Report each as a finding with its evidence and, where possible, the cheapest
way to falsify it.

## Output

- `none` when no assumption needs a second look.
- `findings` when assumptions deserve the arbiter's attention. Findings feed
  the peer group's `auxiliary_findings_considered` merge; they can never block
  the stage by themselves.
