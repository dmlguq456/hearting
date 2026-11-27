## Examples

```text
# Full document-artifact audit
/audit 2026-05-06_se-seminar-tfenhancer

# Facts-only audit after many refine cycles
/audit 2026-05-06_se-seminar-tfenhancer --scope facts

# Research-card consistency audit
/audit speech-enhancement-trends --scope facts

# Static plan audit without test execution
/audit 2026-05-11_audit-skill-infra --scope all --read-only

# Method, results and conclusion of one version in one request (one route, one report)
/audit 2026-11-20_demo-paper --scope all
  v3 §2 Method, §4 Results, §5 Conclusion: does §2 describe what §4 ran,
  and does every claim in §5 rest on a table in §4? Report each section.

# Compatibility spelling; audits always end with the report
/audit 2026-05-06_se-seminar-tfenhancer --report-only
```

## Post-Audit Checklist

The audit ends with the report. If corrections are explicitly requested, use the existing code or refine flow with the report as input:

1. Any 🔴 issue → invoke `/autopilot-refine "<fix prompt suggested by audit log>"` or `/autopilot-code --mode dev "<fix>"`.
2. 🟡 only → defer or batch-fix by user judgment.
3. Clean → no further action.
