---
status: framed
---

## 1. Problem Statement
The local greeting helper needs the requested spelling fix and optional name.

## 2. Root-Cause / Essence
The existing helper and focused test cover this behavior; no shared API or migration is involved.

## 3. Direction Options
One admissible direction: extend the existing helper, tests and README as requested.

## 4. Direction Verdict
Use that helper. An empty name keeps the unnamed greeting; report this ordinary default in the final answer. The named and unnamed tests can falsify the choice.

## 5. Hotfix Boundary
Update behavior, tests and README together; a spelling-only edit misses the request.

## 6. Open Risks
Run the complete local test suite to check callers.

## 7. Questions only the user can answer
None.

## 8. Route proposal
```yaml
route_proposal_v1:
  summary: Extend the greeting helper and verify its callers.
  legs:
    - capability: autopilot-code
      shape: solo
      why: One bounded change in an existing module.
      done_when:
        - Named and unnamed greetings pass, README is updated, and the local suite passes.
```
