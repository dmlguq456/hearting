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
