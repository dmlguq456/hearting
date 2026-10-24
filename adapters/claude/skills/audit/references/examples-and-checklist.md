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

# Compatibility spelling; audits always end with the report
/audit 2026-05-06_se-seminar-tfrestormer --report-only
```

## Post-Audit Checklist

The audit ends with the report. If corrections are explicitly requested, use the existing code or refine flow with the report as input:

1. Any 🔴 issue → invoke `/autopilot-refine "<fix prompt suggested by audit log>"` or `/autopilot-code --mode dev "<fix>"`.
2. 🟡 only → defer or batch-fix by user judgment.
3. Clean → no further action.
