# Capacity failover gap

이 문서는 adaptation-boundary census 대상이 아니다(도구·스크립트만 census된다).

producer는 `dispatch_supervisor_terminal.py:164,182`와 `dispatch-progress.py:472,490`이고, 소비 함수는 `stage-dispatch-fallback.py:651`의 `capacity_context`와 `:1239`의 `capacity_retry`다. 전경 caller는 `:1582,:1713`, supervised join은 reconcile-only 세 지점이며 호출자 0이다. discriminator ledger의 `consumer_entry_point`는 SD-59 `failure_class=capacity`에서만 빈 값이다. SD-114 §(3) consumer execution context가 필요하다. runtime 배선/fixture는 `MA-W3-C11-REENTRY`로 인계한다.
