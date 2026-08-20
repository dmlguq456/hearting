# (3) stage 체인 → 헤더 구분줄 — 구현 로그

plan §4.2-4.4, checklist D1-D11.

## 변경

`tools/fleet/render.py`:

1. **`_dispatch_rail_label_layout(box_width, label_segs)`** 신설 — `_dispatch_box_bottom`이
   쓰던 우flush 배치 산수(run/lead_run 계산, budget 초과 시 미표시)를 뽑아낸 공유 헬퍼.
   `_dispatch_box_bottom`과 `_dispatch_box_divider` 둘 다 이 헬퍼만 호출한다 — plan D2가
   요구한 "중복 없는 공유 헬퍼".
2. **`_dispatch_box_divider(box_width, key, run_key=None, label_segs=None)`** — 라벨 인자
   추가. 라벨이 있으면 `_dispatch_box_bottom`과 똑같은 방식(우flush, tail 3/lead 2 예산,
   `run_key`는 룰, `key`는 코너)으로 배치한다.
3. **`_dispatch_box_bottom`** — 그대로 유지하되 라벨 배치 로직만 새 헬퍼를 호출하도록
   교체(동작 동일, 순수 리팩터). **`╰───` 4칸 스텁·폭·코너·grain은 한 글자도 바뀌지
   않았다** — before/after 캡처에서 닫힘줄 자체가 바이트 단위로 동일함을 확인.
4. 호출부(카드 조립 루프):
   - `route_label`(구 `bottom_label`) 계산을 구분줄 삽입 **앞으로** 이동.
   - 삽입 조건을 `if header_end < len(lines) or route_label:`로 확장 — 자손이 전부 접혀
     `header_end == len(lines)`이어도 브레드크럼이 있으면 구분줄을 그린다. 이것이 plan
     §4.2가 "대비 없이 옮기면 사라진다"고 경고한 정확한 지점이라 처음부터 이 게이트로
     구현했다(소박한 이동을 거치지 않음).
   - `_dispatch_box_bottom` 호출은 `label_segs=None`으로 고정 — 기존에 이미 있던
     `if not label_w ...` 분기를 타는 것이므로 새 코드가 아니다(plan §4.3 rule 4).

`_RAIL_DIV_LEFT`의 주석("drawn only when the card HAS descendants")이 더 이상 정확하지
않아 갱신했다 — 코드가 아니라 주석만.

## 폭 예산 (D1)

`bottom_label_budget(box_width)`를 그대로 승계했다 — 새 예산 함수를 만들지 않았다. 구분줄과
닫힘줄이 같은 헬퍼(`_dispatch_rail_label_layout`)를 쓰므로 라벨이 어느 줄에 있든 같은 열에서
우flush된다(F-75 기준선 불변).

## grain/F-37 (D6/D7)

`_dispatch_box_divider`는 여전히 `rule_key = run_key or key`(룰은 `run_key`, 코너는 `key`)를
쓴다 — 인자를 늘렸을 뿐 grain 계약은 그대로다. `route_seq and len(route_seq) > 1` 가드도
그대로 유지했으므로 한 노드짜리 route는 여전히 브레드크럼 없음(F-37 single-render 불변) —
`test_one_node_route_leaves_the_rail_bare`가 여전히 통과.

## 테스트 (D8/D9)

`tools/fleet/tests/test_f75_bottom_rail.py`:
- 기존 `test_route_rides_the_close_rail_not_the_owner_row`를
  `test_route_rides_the_header_divider_not_the_owner_row_or_close_rail`로 갱신 —
  프로덕션 동작이 F-81로 바뀐 것을 반영(브레드크럼이 구분줄에, 닫힘줄은 항상 라벨 없음).
  기존 `BottomRailGeometryTest`(순수 `_dispatch_box_bottom` 지오메트리 테스트)는 함수
  시그니처/동작이 안 바뀌었으므로 **그대로 통과** — 손대지 않았다.
- 신설 `test_breadcrumb_survives_when_every_descendant_folded_to_done`(D8) — 자손이
  전부 `done`으로 접혀도(`header_end == len(lines)`) 구분줄이 그려지고 브레드크럼을
  나른다는 것을 직접 단언. plan §4.2가 경고한 회귀의 정확한 반증.
- 신설 `test_breadcrumb_appears_exactly_once_when_descendants_are_live`(D9) — 자손이
  살아있는 카드에서 브레드크럼 문자열이 화면 전체에 정확히 1회만 나타남을 단언(구분줄에만,
  닫힘줄에 중복 없음).

파일 헤더 docstring도 F-75/F-81 두 계약을 구분해서 다시 썼다 — `BottomRailGeometryTest`가
왜 여전히 `_dispatch_box_bottom`을 직접 호출해도 유효한지 설명.

실행:
```
$ /home/Uihyeop/anaconda3/bin/python -m pytest tools/fleet/tests/test_f75_bottom_rail.py -q
26 passed
```

## 렌더 캡처 16종 (D10/D11)

`_internal/renders/`(plan 아티팩트 루트) — `dev_logs/render_capture.py`(이 로그와 함께 커밋)로
생성. 스크립트는 plan의 `shards/frame/render_probe.py`/`exp5.py`를 쓰지 않는다 — 그 파일들은
write_scope 밖(`_internal/repro`, `shards/`)이고 snap.json 의존이라 자기완결이 아니다. 대신
Session/DispatchJob을 직접 구성해 `render._build_lines()`를 호출하는 독립 스크립트를 새로
작성했다.

**before**: `git archive e9ff0322 -- tools/fleet | tar -x`로 baseline 커밋의 `tools/fleet`을
격리된 임시 디렉터리(`/tmp/fleet-baseline`)에 추출해 그 경로로 import — 워크트리 상태를
건드리지 않고 진짜 baseline 코드를 실행했다(`git worktree add`는 이 저장소의 OPERATIONS §5.10
가드가 `<repo>-wt/<slug>` 컨벤션 밖 경로를 막아 사용 불가했다).

**after**: 이 시점의 `tools/fleet`(항목 1/2/3 전부 반영).

축: 140/80열 × tint on/off × {자손 working(desc-live), 자손 done(desc-done, 접힘)} = 16개.
`_internal/renders/README.md`에 diff 요약과 80열 기존 결함(f0bb38) 무관 확인을 적었다.

핵심 diff(140열, tint-off, desc-live):
```diff
- ▍  ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
+ ▍  ├────────────────────────────────────────────────────────── frame ✓ › plan ✓ › plan-check ✓ › execute › impl-review › test › report ───┤
- ▍  ╰────────────────────────────────────────────────────────── frame ✓ › plan ✓ › plan-check ✓ › execute › impl-review › test › report ───╯
+ ▍  ╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

**80열 결함 무관 확인**: `after_80_*`와 `before_80_*`에서 `f0bb38`(harness/세션명 붙음,
`⠦ claude codecap-owner-a`)이 **바이트 단위로 동일하게** 나타난다 — 이번 변경 때문이 아니다.
`77d0c2`(orphan 폭 초과)는 이 fixture에 orphan 행이 없어 해당 없음.

## D5 — 사용자 확정 준수 확인

before/after 두 캡처 세트 모두에서 `╰` 코너, 4칸 스텁(`╰───`), 폭을 diff했다 — 라벨 유무를
제외하면 **완전히 동일**함을 위 diff로 확인했다(닫힘줄 자체의 변경분은 diff에 전혀 없다,
라벨이 빠진 자리만 `─`로 채워짐).
