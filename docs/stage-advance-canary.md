이 문서는 adaptation-boundary census 대상이 아니다(도구·스크립트만 census된다).

# SD-110 stage advance canary

정적 recipe 분모는 40→25→14→13이며 live route 분모 114/10과 다르다. 2026-08-30 read-only snapshot은 856 routes, sealed 22(2.6%), node 119/22/1104, 최초 봉인 2026-08-28 11:54, 14.6 route/일이다. 병목은 flag가 아니라 compile 봉인 커버리지다.

최근 24시간 standard+ continuation node 봉인율 95% 이상, runtime-eligible n≥20, 두 capability×intensity strata, malformed 0, generate check와 기존 stage-advance 38 tests 통과일 때만 명시적 `--enable-stage-advance`로 첫 20개를 수집한다. 중복 successor·gate 전진·route/hash/class 불일치 1건은 즉시 off, operational failure/timeout 2/20이면 off다. rollback은 다음 launch에서 flag 제거, pending 자동 전진 금지, manual owner continuation 복귀, snapshot·failure event 보존이다.
