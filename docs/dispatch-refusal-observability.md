이 문서는 adaptation-boundary census 대상이 아니다(도구·스크립트만 census된다).

# Dispatch refusal observability

SD-48 read-only census는 `<dispatch_state_root>/launch-tuple/`와 `_report/`를 읽어 attempt와 기록을 구분한다. schema v1은 `attempt_rows`, `eligibility_failure_nonempty`, `launch_tuple_rejections`, `launch_tuple_unrecorded`, `rejection_classes`, `interpretation`을 낸다. 판정은 `no-rejection-observed`, `rejection-recorded-outside-attempt`, `evidence-write-gap`, `mixed`다.

향후 제안 경로는 `refusal-observations/<route_id>.jsonl`이며 schema_version, event_id, route/attempt/sd/surface/refusal_class/evidence_ref/writer를 둔다. event_id는 sha256 조합, O_APPEND+flock과 250ms deadline, report-only/no-consumer다. SD-39 detached identity, SD-42 structured nudge mismatch, SD-65 route-source-commit-mismatch writer 후보는 spec 승인 전 구현하지 않는다. typed blocker: `MA-W3-C9-OBS-SPEC`.
