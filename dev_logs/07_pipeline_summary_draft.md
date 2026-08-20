# pipeline_summary 초안 — execute 산출 (report 노드가 승격할 것)

**route write_scope 참고**: `pipeline_summary.md`는 execute의 write_scope
(`source/**`, `checklist.md`, `dev_logs/**`)에 없다 — 직접 쓰기를 시도하니
`artifact-guard.sh`가 `artifact-write-outside-node-scope`로 거부했다. checklist F7이
execute 항목에 있지만 그 파일 자체는 report 노드(또는 이후 stage)의 출력 스코프로 보인다.
그래서 이 초안을 `dev_logs/`에 대신 남긴다 — report 노드가 그대로 `pipeline_summary.md`로
승격하면 된다.

---

# pipeline_summary — fleet 표시 결함 3건 + SessionEnd 로그 누출 (execute)

route `rt-e49073ff3427a161` · node `execute` · worktree
`/home/nas/user/Uihyeop/personal/hearting-wt/fleet-display-fix` · branch
`fix/fleet-display-and-sessionend-log` · baseline `e9ff0322`

## 실행 순서 (plan §6 그대로)

(4) → (2) → (1) → (3) → PRD 등재(막힘) → 캡처·검증. plan이 지정한 순서를 그대로 따랐다.

## 커밋 (4개, 독립, push 안 함)

| 순서 | 커밋 | 항목 | 요지 |
|---|---|---|---|
| 1 | `3a45ec3c` | (4) SessionEnd stdout | 세 어댑터 `sync --json >/dev/null`, exec/exit code/stderr 보존, `MEM_DUMP_PUSH=1` 미복원 |
| 2 | `75e40cc6` | (2) codex context 게이지 | `_parse_codex_attempt_tail`에 bounded head 64KiB 추가, thread_id 512KiB 꼬리 유실 수정(F-82) |
| 3 | `a179c007` | (1) orphan 연쇄 | L1 fail-closed + L2 ParentEdgeTracker(3-tick grace) + L3 env sid 복구(F-80) |
| 4 | `2e9129dd` | (3) 헤더 구분줄 | 브레드크럼을 닫힘줄→헤더 구분줄로 이동, 자손 전부 done이어도 유지(F-81) |

각 커밋에 `adapters/claude/tools/fleet/` 미러 동기화 포함(`test_mirror_parity.py` 통과 확인 후
커밋). (2) 커밋이 최초 미러 동기화를 빠뜨렸던 것을 (1) 커밋에서 회수했다 — `dev_logs/
01_orphan_chain.md`에 기록.

## 항목별 요지 (자세한 근거는 각 `dev_logs/NN_*.md`)

- **(4)** `dev_logs/04_sessionend_stdout.md` — 세 hook 모두 stdout 억제, exit code/stderr
  보존 실측 확인. codex/opencode의 stdout이 실제 사용자 화면에 닿는지는 이 저장소 코드만으로
  확정 증명 못 함(기록만) — D3 방침대로 결과와 무관하게 일관 처리.
- **(2)** `dev_logs/02_codex_context_gauge.md` — owner 실측 로그(`codex-ctx-repro.txt`)로
  재현·수정 확인. thread_id가 `None`→`01a01e12-…`로 복구됨을 직접 재실행으로 확인.
- **(1)** `dev_logs/01_orphan_chain.md` — L1/L2/L3 설계·round 1 blocking G1(표시 필터 전이)
  처방 적용 확인. C14 실측: 180초 관측에서 진짜 세션 종료 1건을 잡아 sid 소실(0.684초, owner
  실측 범위 내)을 재확인했으나 §2.3의 더 강한 교차-세션 가설은 이번에도 반증/증명되지 않음
  (owner의 25분 null 결과와 일치) — 정직하게 기록.
- **(3)** `dev_logs/03_header_divider.md` + `_internal/renders/` 16종 — 브레드크럼 이동,
  자손 전부 done 회귀 케이스를 처음부터 막아 구현(소박한 이동을 거치지 않음). `╰───` 스텁·
  폭·코너·grain 무변경을 diff로 확인.

## 막힌 것 — PRD 등재 (E1-E5)

`dev_logs/05_prd_registration_blocked.md`에 전체 기록. 요약: route `rt-e49073ff3427a161`이
`spec_touch: false`로 봉인돼 있어 `artifact-guard.sh`가 `spec/agent-fleet-dashboard/prd.md`
write를 막았다. `drift_verdict`는 spec-significant라고 판정했지만 실제 write 권한
(`spec_touch`)은 그와 별개로 false로 굳어 있다 — plan 단계와 route 봉인 단계 사이의 불일치로
보인다. **route를 직접 고쳐 우회하지 않았다**(봉인 무결성 보존). F-80/F-81 본문·v60 헤더·
F-74~F-79 경고 문구는 plan.md §2.5/§4.4/§1.1에 이미 완성돼 있어 route가 재봉인되면 그대로
붙여넣을 수 있다.

**하류 조치 필요**: (a) route를 `spec_touch: true`로 재컴파일하거나 (b) autopilot-spec으로
분리 위임하거나 (c) 사용자에게 이 gap을 보고. 이 obligation은 D-42(메인 세션만 메모리
생명주기 소유)에 따라 dispatch worker인 이 execute가 직접 `mem add`할 수 없어, 이 문서와
`dev_logs/05_prd_registration_blocked.md`가 메인 세션/owner의 등록 근거다. **같은 이유로
`pipeline_summary.md` 파일 자체도 execute의 write_scope 밖이다(위 참고 문단) — 두 gap 모두
route/스코프 경계 문제이지 구현 누락이 아니다.**

## 테스트

`dev_logs/06_test_suite_before_after.md` — 사전(baseline, 전체 저장소 `git archive` 격리
추출) 1469 passed / 사후 1495 passed, 새 실패 0건, 차이(26)는 정확히 이번 사이클이 추가한
테스트 수와 일치. `test_token_budget.py`의 동시성 테스트 1건이 개발 중 두 차례 flaky하게
실패했다가 재실행 시 즉시 통과 — 무관 파일, 부하 민감으로 판단.

CI adaptation-boundary 5스텝(`tools/generate.py --check`, `tools/render-landing.py --check`,
`check-adaptation-boundary.sh`, `check-model-config.py`, `check-unit-config.py`) 전부 exit 0
확인. `tools/generate.py`(non-check)를 한 번 실행해 claude/codex 플러그인 projection의 파일
모드 drift를 정리했다(git diff 없음 — 내용은 이미 최신, 모드만 드리프트했던 것).

## 범위 밖 확인 (F8)

`77d0c2`(orphan 행 폭 초과), `f0bb38`(80열 harness명·세션명 붙음) 둘 다 건드리지 않았다 —
`git diff e9ff0322..HEAD --stat`로 변경 파일 목록을 확인, 두 결함과 무관한 파일만 포함.
80열 캡처에서 `f0bb38`이 before/after 양쪽에 바이트 단위로 동일하게 나타남을 직접 diff로
확인(`_internal/renders/README.md`).

## checklist 최종 상태

57개 중 51개 `[x]`. 남은 6개(E1-E5, F7) 전부 PRD 등재 gap 하나에서 파생됐다(F7 자체도 같은
write_scope 경계 문제). 소스 변경·테스트·렌더 캡처·CI 배터리·SessionEnd 실측·orphan 관측은
전부 완료.

## report 노드로 넘기는 것

1. PRD 등재 gap(위 "막힌 것" 절) — route 재봉인 또는 autopilot-spec 위임 필요.
2. 이 초안을 `pipeline_summary.md`로 승격.
3. 렌더 캡처 16종을 사용자에게 보여 확인받은 뒤 push(owner_brief.md 완료 기준 — 메인 세션이
   처리).
4. codex/opencode SessionEnd stdout이 실제 화면에 닿는지의 미확정 부분(A6) — 실제 codex/
   opencode 대화형 세션 종료로만 확정 가능, 이 execute는 비대화형이라 재현 불가.
