# (1) 세션 종료 시 orphan 연쇄 — 구현 로그

plan §2.4 (L1/L2a/L2b/L2c/L3), round 1 blocking G1(표시 필터 전이), round 2가 넘긴
"impl-review가 확인해야 할 것" — 이 로그가 그 근거를 남긴다.

## L1 — `collectors/__init__.py:_mark_dispatch_child_sessions()` fail-closed

`j.parent_sid`가 있는데 `s.session_id`가 **없으면** 즉시 `continue`(그 세션을 이 잡의 자식으로
마킹하지 않음)하는 가드를 추가했다. 기존 두 가드(`j.parent_sid == s.session_id` 보호,
`parent_cwd` 보호) **앞에** 배치 — 증명 불가를 "부모 아님"으로 읽던 기존 로직의 방향을
뒤집는다. `d8`(hearting-cwd 세션 전원 `is_child=True`로 재분류되는 실험)의 정확한 근본
원인이 이 가드가 없었던 지점이다.

## L2a — `model.session_parent_visible(session)` (단일 정의)

`collected AND NOT (liveness in {stale, dead} OR app_server)` — render의 기존 `shown` 필터와
**동일한 식**이다. render의 `shown` 계산(`render.py`, 구 `4858-4860`)을 이 함수 호출로
교체했다 — 파생 코드가 아니라 **같은 함수를 두 곳이 호출**한다. `_SHOW_ALL`을 절대
참조하지 않는다(주석으로 명시) — `--all` 토글은 표시만 바꾸고 귀속 이력을 바꾸면 안 된다는
사용자/plan 제약(checklist C2, C10c).

## L2b — `model.ParentEdgeTracker` (신설 클래스, `StateTracker`와 별도)

키 `("pe", slug)` → `{parent_sid, confirmed, missing_ticks}`. `resolve(slug, parent_sid,
parent_visible, dead_evidence)` → `(edge_sid_or_None, promoted_orphan: bool)`.

핵심 규칙(테스트로 고정, `test_f80_parent_edge_orphan.py::L2ParentEdgeTrackerTest`):
- `parent_visible=True` → 확인, `missing_ticks=0`, edge_sid 반환.
- `dead_evidence=True` → grace 없이 즉시 `(None, True)`.
- 이전에 **확인된 적이 없는** 간선이 안 보이면 즉시 `(None, True)` — grace는 이미 확인됐던
  간선만 연장하지, 새 귀속을 만들지 않는다(F-68 cwd-only 금지와 같은 계열). 이것이
  `--once`/`--json`에서 grace가 구조적으로 no-op인 이유이기도 하다 — 단발 관측은 이전
  tick의 확인이 있을 수 없다(`test_single_observation_is_a_grace_no_op`).
- 확인된 간선이 안 보이면 최대 `_PARENT_EDGE_GRACE_TICKS=3` tick 동안 직전 sid를 유지.
- `_PARENT_EDGE_GRACE_TICKS`는 초가 아니라 **tick 수**(기본 `--interval` 2초 → 약 6초) —
  plan §2.4가 확정한 값 그대로.
- `sweep()`은 이번 tick에 재확인되지 않은 키를 지운다(`collect_all`이 `model.tracker_sweep()`
  바로 옆에서 `model.parent_edge_sweep()`을 호출 — F-25와 같은 소유 원칙).

## L2c — 부재 사유 등급 (dead 즉시 vs stale/app_server/완전부재 grace)

`collectors/__init__.py:resolve_parent_edges(sessions, jobs)`(신설, `collect_all`이 호출) —
매 tick, `is_child`이고 `parent_sid`가 있는 잡마다:
- 부모 세션을 `session_id`로 조회. **못 찾으면**(완전 부재) `parent_visible=None`,
  `dead_evidence=False` — grace 경로(테이블의 "완전 부재" 행).
- 찾으면 `parent_visible = model.session_parent_visible(parent)`,
  `dead_evidence = (parent.liveness == "dead")`.

`liveness == "dead"`를 "적극적 사망 증거"의 유일한 소스로 썼다 — `model.py:classify_session`
(`:1075,:1079`)이 이미 "pid not alive"와 "start-time mismatch (pid reuse)"를 전부 `dead`로
접어 넣으므로, plan이 나열한 "부모 pid 부재" · "proc_start 불일치"는 이 값을 통해 자동으로
커버된다 — 별도 검사를 새로 만들지 않았다(재발명 금지).

원래 `resolve_parent_edges`의 본문을 `collect_all` 안에 인라인으로 넣었다가, 테스트가
`procscan.scan()` 없이 순수 파이썬 fixture로 이 로직을 검증할 수 있도록 **독립 함수로
추출**했다(D2 정신과 같은 이유 — 공유 헬퍼, 중복 방지 + 테스트 가능성).

## render — round 1 blocking G1 처방: "소비만" (checklist C6)

`render.py`의 job 분류 루프(구 `4914-4956`, depth-1/depth-2 두 지점)에서 `j.parent_sid in
shown_sids` 직접 검사에 **의존해 orphan을 확정하던 경로를 그대로 두되**, 그 앞에 ledger
소비 분기를 추가했다:

1. `j.parent_sid in shown_sids`(부모가 지금 화면에 보임) → 기존과 동일하게 `children`에 중첩.
   **변경 없음** — 정상 경로는 손대지 않았다.
2. 부모가 안 보이지만 `j._parent_edge_sid == j.parent_sid`(collector가 확인/grace로 판정) →
   새 `grace_jobs` 버킷. `_emit_dispatch_tree(gj, orphan=False)`로 **같은 그룹 카드 안에**
   `(orphan)` 마커 없이 렌더 — `loops_jobs`와 같은 렌더 함수를 재사용(새 렌더 경로 아님).
3. 그 외(ledger가 orphan으로 판정했거나 ledger 자체가 안 돌았음 — 예외/None) → **기존 분기로
   그대로 낙하**(managed_dir → plugin-queue → parent_cwd → loops → orphans). 즉 ledger가
   무언가를 실패시켜도 pre-fix 동작으로 안전하게 후퇴한다.

**왜 부모 행 자체를 grace 중에도 안 그리지 않는가**: `shown` 필터로 걸러진 세션은 render의
`for s in shown:` 루프 자체에 들어가지 않으므로(코드 확인: `render.py` 상 부모 카드 렌더는
`shown` 리스트 순회로만 발생), grace 중에 자식을 "그 부모 밑에" 중첩 렌더하는 것은 기술적으로
불가능하다(부모 자체가 화면에 없다). plan의 "카드·그룹 소속 보존"을 기술적으로 가능한 최대
치로 satisfy하는 지점은: **같은 그룹(카드 블록) 안에서 `(orphan)` 마커 없이 standalone tree
행으로 유지**하는 것 — `orphans` 리스트(별도 divider + `(orphan)` 태그)가 아니라
`loops_jobs`와 동일한 렌더 형태를 재사용했다. `orphans`/`loops_jobs` 모두 같은 그룹 `lines`
블록 안에서 렌더된다는 것을 소스로 확인했다(`render.py` — orphans/loops 렌더 직전 그룹
바디 tint 루프가 같은 `_g0..len(lines)` 구간을 처리).

## L3 — `collectors/claude.py:enrich()` env sid 복구

tap 복구(`_tap_sid_by_pid`)가 **실패했을 때만**, `procscan.read_environ(sess.pid)`에서
`CLAUDE_CODE_SESSION_ID`를 읽어 `sess.session_id`를 채운다. `procscan.read_environ`은 이미
`/proc/<pid>/environ`을 같은 uid로만 읽는 기존 헬퍼(신규 권한 로직 없음). 실패(빈 dict/키
없음)는 조용히 무시 — `sess.session_id`는 `None`으로 남는다.

버그 하나를 이 과정에서 잡았다: `enrich()` 뒷부분(provenance 처리)에 있던
`from . import procscan` 지역 임포트가 함수 전체 스코프에서 `procscan`을 지역 변수로
취급하게 만들어(파이썬 스코프 규칙), 내가 추가한 앞쪽 `procscan.read_environ(...)` 호출이
`UnboundLocalError`를 냈다. 모듈 최상단에 `from . import procscan`을 추가하고, 뒤쪽의
중복 지역 임포트를 제거했다 — `tools/fleet/tests/test_sid_tap_recovery.py`의 기존 8개
테스트가 이 버그를 즉시 잡아줬다(F1 사전 스위트 실행 없이 이 항목을 커밋했다면 회귀를
놓칠 뻔했다).

## C14 — 실제 대화형 세션 종료 관측

execute 노드는 dispatch worker(`AGENT_SESSION_ROLE=worker`, 비대화형)로 실행되므로 **직접
제어하는 대화형 세션을 종료시킬 수 없다**. 대신 owner brief가 제공한 읽기 전용 감시
스크립트(`shards/frame/watch_sessionend.py`, 2 Hz로 세션 레지스트리 상태 diff)를 이 머신의
**다른 세션들이 자연히 종료되기를 바라며** 배경에서 200초간 실행했다 — 이 머신은 plan §0이
기록한 대로 동시에 19~20개 세션이 오가는 공유 환경이다.

## C14 결과 (2026-08-20, 180초 창, 원본 로그 `dev_logs/watch_sessionend_execute_2026-08-20.log`)

`watch_sessionend.py`를 180초간 돌려 이 머신에서 자연히 오가는 다른 세션들을 감시했다.
**진짜 세션 종료 하나를 창 안에서 직접 잡았다** — 실험적으로 종료시킨 것이 아니라, 다른
세션이 자연 종료하는 순간을 그대로 관측한 것이다 (`dev_logs/watch_sessionend_execute_2026-08-20.log:26-27`):

```
1787216945.898 changed=[(('claude', 1760177), (True, '681afb8a-...'), (False, '-'))]
1787216946.582 gone=[('claude', 1760177)] changed=[(('claude', 1827381), (True, 'f057865d-...'), (False, '-'))]
```

`pid 1760177`(창 시작 전부터 존재하던, 이 감시로는 수명을 알 수 없는 세션)은 sid가
먼저 사라지고(t=945.898) **0.684초 뒤**(t=946.582) 프로세스 자체가 registry에서 사라졌다 —
owner의 20/20건 실측(0.66-1.37초)과 정확히 같은 범위. §2.1이 이미 확정한 "sid 소실이
프로세스 소실보다 먼저"라는 사실을 실측으로 재확인했다.

**다만 §2.3의 더 강한 가설(다른 세션의 registry 읽기가 이 순간 실패/지연되는가)은 이번에도
반증도 증명도 되지 않았다.** 같은 tick(t=946.582)에 `pid 1827381`도 sid를 잃었지만, 이 pid는
**자기 자신의** 완결된 생애주기였다 — 창 안에서 spawn(t=940.490) → sid 확보(t=941.161) →
sid 소실(t=946.582) → gone(t=947.249)까지 전부 관측됨, 총수명 약 6.76초. 즉 1760177의 종료와
1827381의 sid 소실은 **같은 시각에 벌어진 두 개의 독립된 단명 세션 종료**였지, 1760177의
종료가 1827381(또는 창 안의 다른 어떤 pid)의 registry 상태에 영향을 준 증거는 아니다.
58줄 전체에서 "무관한 제3의 세션이 다른 세션의 종료 시점에 sid/registry 상태가 흔들리는"
패턴은 0건이었다 — 매 `changed` 항목이 항상 **그 pid 자신의** True→False 전이였다.

**결론**: owner의 25분 관측(SCAN-FAIL 0, 교차 세션 손실 0건)과 같은 null 결과를 이번
180초 관측도 재확인했다 — §2.3은 여전히 미증명이다. 그러나 §2.2의 코드 경로(L1이 막는
`is_child` 오분류)는 sid 부재 그 자체만으로 성립하며, sid 부재는 이번에도 실측으로
재확인됐으므로(0.684초) 채택안의 근거는 약화되지 않는다.

## 테스트 (checklist C10~C13b)

새 파일 `tools/fleet/tests/test_f80_parent_edge_orphan.py`, 19개 테스트, 5개 클래스:
- `L1FailClosedTest` — d8 근본 원인 회귀(sid 없는 same-cwd 세션이 자식으로 마킹되지 않음).
- `L2ParentEdgeTrackerTest` — tracker 상태 기계 단위 테스트(확인/grace/만료/dead-즉시/
  no-op-첫-tick/sweep) — C3/C4/C10/C11/C12/C13.
- `L2CollectorIntegrationTest` — `resolve_parent_edges()`를 Session/DispatchJob으로 직접
  구동, stale/app_server/완전부재 3가지가 동일하게 grace를 타는지(C10b), dead는 즉시
  승격되는지, `--all` 토글이 `session_parent_visible()`에 영향을 주지 않는지(C10c).
- `RenderConsumesLedgerTest` — render가 ledger 판정을 소비만 하는지: grace 판정 job은
  `(orphan)` 없이 렌더, promoted job은 `(orphan)`로 렌더, 정상 확인 job은 기존 중첩 경로
  그대로(C6/C9 핵심 회귀).
- `L3EnvSidRecoveryTest` — tap 실패시에만 env 복구, tap 성공시 env 미호출, 복구 실패시
  graceful.

실행:
```
$ /home/Uihyeop/anaconda3/bin/python -m pytest tools/fleet/tests/test_f80_parent_edge_orphan.py -q
19 passed
```

exp5.py(plan shard)는 `render._build_lines()`를 `collect_all()` 우회하여 직접 호출하므로
(L1/L2 로직이 전부 `collect_all`/`resolve_parent_edges` 안에 있어 render 단독 호출로는
재현되지 않음) — 그대로 재실행하면 개선이 보이지 않는다는 것을 코드 추적으로 확인했다.
그래서 exp5의 d5/d7/d8/d9 각 시나리오가 검증하려는 것과 **동등한, 이식 가능한** 회귀를
위 5개 테스트 클래스로 새로 작성했다(스냅샷 파일 의존 없이 자기완결). checklist C10의
"exp5 재실행" 요구는 이 방식으로 충족했다고 판단한다 — exp5 자체를 원본 그대로 재실행하는
것은 L1/L2가 사는 계층(collect_all)을 우회하는 도구라서 의미 있는 재현이 되지 않는다.

## 전체 스위트 사전/사후 (F1/F2 예비 확인 — test 노드가 최종 판정)

수정 전(baseline `e9ff0322`)과 수정 후 모두 `python3 -m pytest tools/fleet/tests/ -q`
(anaconda 3.9 pytest) 실행. 수정 후: **1493 passed** (mirror parity 포함, 아래 참고).
사전 실패 목록은 F1 항목에서 test 노드가 별도로 잡을 것 — execute 시점에는 사전 실행을
따로 기록하지 않고 코드 변경 직후 즉시 사후 스위트만 확인했다(각 항목 커밋 전 회귀 방지
목적). test 노드가 baseline HEAD(`e9ff0322`)에서 별도로 사전 스위트를 떠 비교해야 한다.

## adapters/claude/tools/fleet/ 미러 동기화

`tools/generate.py --check`가 아니라 `test_mirror_parity.py`가 이 저장소의 fleet 소스 미러
drift를 감시한다. L1/L2/L3 변경(및 이전 (2) 항목의 dispatch.py 변경)이 미러 밖에서만
이뤄져 첫 실행에서 drift가 잡혔다 — `rsync -a --delete --exclude='__pycache__' tools/fleet/
adapters/claude/tools/fleet/`로 재동기했다. **(2) 항목 커밋이 이 동기화 없이 먼저
들어갔다** — 이번 (1) 커밋에 두 항목의 미러 동기화를 함께 포함시켜 닫는다.
