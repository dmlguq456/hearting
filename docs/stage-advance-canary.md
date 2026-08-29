이 문서는 adaptation-boundary census 대상이 아니다(도구·스크립트만 census된다).

# SD-110 stage advance canary

## 두 축과 분모

정적 recipe 축과 live route 축은 세는 모집단이 달라 분모를 공유하지 않는다. 아래 수치는
전부 `tools/stage-advance-census.py` 출력이며, 다른 근거로 주장하지 않는다.

```
python3 tools/stage-advance-census.py \
  --routes <artifact-root>/.runtime/routes \
  --topologies capabilities/topologies.json --json
```

정적 recipe 축(`capabilities/topologies.json`, schema 10): `standard_plus` node 53개 중
continuation 선언 40, terminal 13, `runtime-eligible` 35 / `model-required` 18 /
`unsealed` 0이며 13개 capability 중 12개가 staged graph를 가진다. 이 여섯 숫자는
`tools/stage_advance_census.test.py::test_repo_topology_pins_the_documented_recipe_axis`가
고정한다.

live route 축(2026-08-30 read-only snapshot, `.agent_reports/.runtime/routes`): route 파일
430개(같은 디렉터리의 `*.outcome.json` receipt는 route가 아니므로 분모에서 제외한다),
node 1246개, 봉인 node 141 = `runtime-eligible` 119 / `model-required` 22, `unsealed` 1105,
봉인 route 22개(5.12%), invalid 0이다. 봉인 route의 파일 mtime 기준 최초 2026-08-28T02:54:55Z,
최종 2026-08-29T15:00:29Z, span 1.504일, 14.63 route/일이다. 봉인 시각을 route 자체가
기록하지 않으므로 이 창은 `basis=file-mtime` 관측이다.

corpus가 없거나 topology가 recipe를 담지 않거나 route가 topology에 없는 `advance_class`를
쓰면 census는 report를 만들지 않고 fail-closed로 종료한다(exit 65/66). 병목은 flag가 아니라
compile 봉인 커버리지다: live node의 88.7%가 아직 `unsealed`다.

## on/off 조건

최근 24시간 standard+ continuation node 봉인율 95% 이상, runtime-eligible n≥20, 두
capability×intensity strata, malformed 0, generate check와 기존 stage-advance 38 tests
통과일 때만 명시적 `--enable-stage-advance`로 첫 20개를 수집한다. 중복 successor·gate
전진·route/hash/class 불일치 1건은 즉시 off, operational failure/timeout 2/20이면 off다.
rollback은 다음 launch에서 flag 제거, pending 자동 전진 금지, manual owner continuation
복귀, snapshot·failure event 보존이다.
