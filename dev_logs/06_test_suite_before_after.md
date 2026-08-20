# F1/F2 — fleet 스위트 사전/사후 (execute 시점 예비 확인, test 노드가 최종 판정)

## 사전 (baseline `e9ff0322`, 전체 저장소를 `git archive`로 `/tmp/fleet-baseline-full`에
격리 추출 — 워크트리 상태를 건드리지 않음. 부분 추출(`tools/fleet`만)은 `dispatch.py`의
`_ROOT` 상위 탐색과 `test_f17_title_refresh.py`/`test_f48_producers.py`/
`test_token_budget.py`의 `adapters/codex/hooks/*.py` 직접 read가 저장소 나머지 트리를
요구해 실패했다 — 전체 저장소 추출로 교체)

```
$ cd /tmp/fleet-baseline-full && python -m pytest tools/fleet/tests/ -q
1469 passed in 66.37s
```

**사전 실패 0건.**

## 사후 (이 execute의 최종 상태 — 항목 1/2/3/4 커밋 전부 반영)

```
$ python -m pytest tools/fleet/tests/ -q
1495 passed in 71.87s
```

**새 실패 0건.** `1495 - 1469 = 26`은 정확히 이번 사이클이 추가한 테스트 수와 일치한다:
`test_f82_codex_attempt_head.py` 5개 + `test_f80_parent_edge_orphan.py` 19개 +
`test_f75_bottom_rail.py`의 신설 2개(D8/D9 회귀) = 26.

## f17 계열 환경 민감 실패 (plan §7 언급)

이번 실행에서는 f17 계열 실패가 사전/사후 모두 0건이었다. 개발 도중 별도로
`test_token_budget.py::AccountingTest::test_store_rejects_extra_fields_and_serializes_concurrent_updates`
가 두 차례 flaky하게 실패했다가(동시성 테스트, 머신 부하 민감) 재실행 시 즉시 통과했다 —
내 diff가 건드리지 않는 파일(`test_token_budget.py`는 이번 변경과 무관)이므로 이번 4건과
무관한 환경 잡음으로 판단했다.

## test 노드가 확인해야 할 것

이 로그는 execute 시점의 예비 확인이다. plan §7/checklist F1-F2가 요구하는 최종 판정
(baseline과 최종 커밋을 **같은 머신·같은 타이밍**에서 비교)은 test 노드가 독립적으로
재실행해 증거로 남겨야 한다.
