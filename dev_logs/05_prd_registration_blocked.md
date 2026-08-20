# PRD 등재 (E1-E5) — 막힘, 정직하게 기록

## 무엇을 시도했나

plan.md §2.5(F-80 본문)·§4.4(F-81 본문)·§1.1(백필 판단·F-74~F-79 경고 문구)을 그대로
`spec/agent-fleet-dashboard/prd.md`에 삽입하려 했다 — v59 확정 결정 블록(§"## 확정 결정
(v59...)") 직후, "## Next — v34 implementation handoff" 앞에 새 "## 확정 결정 (v60 승격...)"
섹션을 넣는 편집을 준비했다(본문은 plan.md에 이미 확정된 문구 그대로).

## 왜 막혔나

`Edit` 시도가 `artifact-guard.sh` 훅에서 거부됐다:

```
material 작업인데 route 미선언 (silent no-route). direct 실행도 선택된 capability route를
먼저 compile/bind해야 한다. [reason=route-capability-not-accepted]
{"reason": "capability-artifact-route-required",
 "route_file": ".../.runtime/routes/rt-e49073ff3427a161.json",
 "route_id": "rt-e49073ff3427a161", "status": "blocked",
 "target": ".../spec/agent-fleet-dashboard/prd.md"}
```

이 route의 봉인된 레코드(`rt-e49073ff3427a161.json`)를 직접 읽어 원인을 확인했다:

```json
"spec_touch": false,
...
"tracked_gate_evidence": {
  "spec_read": {"satisfied": true, "source": ".../prd.md"},
  "drift_verdict": "spec-significant: (3) stage-chain header placement and (1)
    orphan-promotion debounce change display/attribution contract; ...",
  ...
}
```

`artifact-guard.sh:141`이 spec 경로 write를 허용하는 조건은 정확히
`route.get("spec_touch") is True and any(root=="spec" or root.startswith("spec/") for root
in roots)`다. 이 route는 **`spec_touch: false`로 봉인**됐다 — `drift_verdict`가
spec-significant라고 명시적으로 판정했음에도, 실제 write 권한(`spec_touch`)은 그 판정과
별도로 `false`로 굳어 있다. plan.md §1/§6과 owner_brief.md는 "이번 사이클 안에서 PRD 등재를
포함시킨다"고 명시했지만, **route 자체가 그 결정을 반영하지 못한 채 봉인**됐다 — plan
단계와 route 봉인 단계 사이의 불일치로 보인다.

## 왜 우회하지 않았나

- kernel 지시: "Preserve permission, safety, git-state, artifact-root, liveness, and
  verification guards." route/write_scope는 "immutable... Revalidate them when a runtime
  guard requires it; do not reselect them."
- 봉인된 route JSON을 직접 고쳐 `spec_touch: true`로 바꾸는 것은 execute 권한 밖의 route
  재선택이다 — 이 gap을 "직접 수정해서 통과"시키는 것은 route 무결성 자체를 깨는 행위이므로
  하지 않았다.

## 무엇이 준비돼 있는가 (하류가 바로 쓸 수 있도록)

F-80/F-81 본문·v60 헤더 줄·F-74~F-79 공백 경고 문구는 **plan.md §2.5/§4.4/§1.1에 이미 완성된
형태로 있다** — 그대로 복사해 넣으면 되는 상태다. 내가 준비했던 정확한 삽입 지점과 전체
diff 텍스트는 아래에 남긴다(하류가 route를 spec_touch:true로 재봉인하거나 autopilot-spec으로
넘긴 뒤 그대로 적용할 수 있게).

**삽입 위치**: `prd.md`의 `- **F-73c (세션 상세 금지)**: ...` 문단 끝(파일 기준 약 1126행)과
`## Next — v34 implementation handoff` 사이.

**삽입 내용**: plan.md §1.1(백필 판단 문단 → PRD 경고줄로 재구성) + §2.5(F-80 전문) +
§4.4(F-81 전문). 정확한 텍스트는 plan.md에 있으므로 여기 재복사하지 않는다(단일 정본 유지 —
plan.md가 이미 정본이다).

## checklist E1-E5 상태

- [ ] E1 F-80 추가 — **막힘**(route spec_touch=false)
- [ ] E2 F-81 추가 — **막힘**
- [ ] E3 v60 헤더 줄 — **막힘**
- [ ] E4 F-74~F-79 공백 경고 — **막힘**
- [x] E5 백필을 이번 범위에 넣지 않는다는 판단은 기록했다(§1.1 재확인, 위 절) — 이 항목만은
      "PRD에 쓰는" 행위가 아니라 "판단을 문서화"하는 행위라 dev_logs에 남기는 것으로 완료.
      **새 obligation 등록**은 D-42(메인 세션만 메모리 생명주기 소유)에 따라 이 dispatch
      worker가 직접 `mem add`할 권한이 없다 — 아래 pipeline_summary.md와 이 로그가 메인
      세션/owner가 obligation을 등록하기 위한 근거 자료다.

## 하류로 넘기는 조치

1. **route 재봉인**: `rt-e49073ff3427a161`을 `spec_touch: true` + write_scope에 `spec/`
   포함으로 재컴파일하거나,
2. **autopilot-spec 위임**: 이번 사이클 밖으로 분리해 spec capability가 정식으로 F-80/F-81을
   흡수하게 하거나,
3. owner/report 노드가 이 gap을 사용자에게 명시적으로 보고하고 처리 방법을 확정.

(1)(2)(3) 항목의 소스 변경·테스트·렌더 캡처는 이 gap과 **무관하게 이미 완료·커밋**됐다 —
PRD 등재만 별도로 막혔다.
