# Capacity failover gap

이 문서는 adaptation-boundary census 대상이 아니다(도구·스크립트만 census된다).

producer는 `dispatch_supervisor_terminal.py:164,182`와 `dispatch-progress.py:472,490`이고, 소비 함수는 `stage-dispatch-fallback.py:651`의 `capacity_context`와 `:1239`의 `capacity_retry`다. 전경 caller는 `_dispatch` 안의 `:1582,:1713` 두 곳뿐이고, supervised join 경로는 reconcile-only 세 지점이며 `capacity_retry` 호출자가 0이다. 즉 SD-59 retry는 전경 dispatch에서만 도달 가능하고, supervised join으로 들어온 capacity 실패는 소비되지 않는다.

`tools/dispatch-discriminators.tsv`는 SD-93 `rejection_class` 닫힌 enum(`dispatch_launch_tuple.REJECTION_CLASSES`) 전용 원장이다. SD-59 `failure_class=capacity`는 이 enum의 값이 아니므로 원장의 대상이 아니고 행이 없다. 원장 세 행의 `consumer_entry_point`는 모두 채워져 있어야 하며 빈칸은 허용되지 않는다 — 빈칸을 "선언된 갭"으로 읽는 해석은 성립하지 않는다. 위의 supervised-join caller 0이 실제 갭이고, `utilities/dispatch_discriminators.test.py`가 (a) 원장에 빈 `consumer_entry_point`가 없다는 것, (b) `capacity`가 원장/enum 어디에도 없다는 것, (c) `capacity_retry` 호출자가 `_dispatch` 안 두 곳뿐이라는 것을 각각 단언한다.

SD-114 §(3) consumer execution context가 필요하다. runtime 배선/fixture는 `MA-W3-C11-REENTRY`로 인계한다.
