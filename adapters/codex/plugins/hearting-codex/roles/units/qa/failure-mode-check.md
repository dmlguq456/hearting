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
