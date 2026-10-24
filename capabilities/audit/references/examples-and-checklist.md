## Examples

```text
# Full document-artifact audit
/audit 2026-05-06_se-seminar-tfrestormer

# Facts-only audit after many refine cycles
/audit 2026-05-06_se-seminar-tfrestormer --scope facts

# Research-card consistency audit
/audit speech-enhancement-trends --scope facts

# Static plan audit without test execution
/audit 2026-05-11_audit-skill-infra --scope all --read-only

# Report only; do not dispatch fixes
/audit 2026-05-06_se-seminar-tfrestormer --report-only
```

## Post-Audit Checklist

Stage E dispatches correction automatically unless `--report-only` is set. After a report-only audit:

1. Any 🔴 issue → invoke `/autopilot-refine "<fix prompt suggested by audit log>"` or `/autopilot-code --mode dev "<fix>"`.
2. 🟡 only → defer or batch-fix by user judgment.
3. Clean → no further action.
