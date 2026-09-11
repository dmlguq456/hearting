# 분사 책임 구조 수리 — 검증 기록

상태: 소스 통합 HEAD 6b7d19a535dcd3dea130f72a71f2eacfcbc0789c에서 회귀검사와 실제 지연 통보 검증 중. frame 사용자의 이해 확인·기록 방식 답변은 대기 중이다. 전체 완료, main 병합·푸시, 릴리즈, 설치를 주장하지 않는다.

사용자가 지적한 문제는 개별 어댑터의 기능 부족을 넘어선다. 여러 관측자가 실행 상태를 각각 판정하면서 재시도와 거부 권한을 갖고, 복구가 실패했을 때 누가 작업을 유지하거나 사용자에게 돌려줄지는 빠져 있었다. 과거 2026-09-01 복잡도 진단과 이번 Cairn·직렬 chain·리뷰 실측에서 같은 형태가 반복됐다. 이번에는 기존 수정을 유지하면서 결정 권한과 후속 책임을 공통 코드에 모았다.

## 책임과 제거한 중복

| 구간 | 최종 책임 | 변경 |
|---|---|---|
| 실행·수명·실패 정리 | 실행 경계와 finite watchdog | 실제 runner/fence와 등록 watchdog 신원을 구분한다. 자손 정리 증명 뒤 lease를 반환한다. 관측이 부족하면 정리 의무를 유지한다. |
| 완료 확정 | jobs 잠금 안의 exact terminal writer | 프로세스 종료와 성공을 구분하고, 확정된 결과를 후속 관측이 뒤집지 못한다. `dispatch_attempt_policy`가 의미 결과와 남은 정리 의무를 분리한다. |
| 재시도 | 동일 jobs 잠금의 retry claimant | 감시자는 exact predecessor를 제안한다. 실제 등록 시 확정 결과·정리 증거·동일 실패의 기존 후속 시도를 다시 확인한다. 명시적인 새 리뷰 round는 별개다. |
| 대기·복구 | 공통 `dispatch_supervision.wait_for_batch` | Claude/Codex supervisor와 serial driver의 대기 횟수 초과 사망 루프 세 곳을 없앴다. 관측 도구 실패에도 작업을 유지하며 복구·부모 인계를 연결한다. |
| 감독자 종료 | exact orphan watcher | helper 반환만으로 상태를 지우던 경로를 없앴다. 정리가 확인되지 않으면 상태와 부모 인계 기록을 남긴다. |
| 사용자 통보 | 기존 pending-delivery 큐와 부모 runtime carrier | 새로운 별도 큐를 만들지 않았다. human-gate와 supervision은 같은 claim/send/acceptance 운송을 사용하며 판단 의미는 구분한다. 알림 수신은 작업 완료가 아니다. |

완료 영수증 키와 digest의 수동 복사 네 곳을 공통 `dispatch_receipt_identity`로 합쳤다. 임시 jobs 원장을 만들어 외부 liveness 명령의 exit 3을 완료 근거로 소비하던 join 우회 경로를 제거했다. Codex에만 있던 receiptless 복구 루프도 공통 join으로 이동했다. 기존 `process-exited` 캐시가 확정 결과를 건너뛰어 중복 retry를 만드는 경로는 앞선 세 커밋에서 수정했고 이번 통합에 포함했다.

직렬 체인 원 작업과 Fleet R1 표시는 앞서 `45d7567d`·`98b4269a`로 수정하고 v2.139.1에 설치된 별도 완료 건이다. serial의 1..16 한도와 parallel의 max_slices=4를 구분한 기존 결정을 유지했다. OpenCode child는 허용하지만 OpenCode owner의 serial supervision은 여전히 지원하지 않으며 single-session fallback을 안내한다. 이번 수리가 모든 런타임에서 같은 실행 방식을 만든다고 주장하지 않는다.

단계는 입력과 결과를 정의한다. 별도 프로세스 기동 여부는 실행 방식의 선택이다. 이번 수리는 사용자 지시대로 정식 분사 없이 직접 수행했다. 독립 검토와 QA 결과는 보존했다.

## 검증 근거

- 공통 수정: `704ae7cf` → `cc1791ba` → `de632e5c` → `705f042e`.
- 리뷰 수명·완료 전달: `be684cf8`까지 포함. 21분 실측의 원본 source는 `56746fb4`이며, 이후 수정의 실측이라고 바꾸어 쓰지 않는다.
- frame: `daa82355` + `7db4ce09`(실제 모델 role/profile 전달) + `5929e741`(route/cycle→frame→질문→owner 순서 및 상대 산출물 기준) 포함. 실제 두 하네스 완료·자동 wake와 승인 전 owner 거부를 확인했으며, release 후 owner 입력 읽기와 standard 왕복은 아직 합격을 주장하지 않는다.
- 통합 경로: `/home/nas/user/Uihyeop/personal/hearting-wt/dispatch-responsibility-integration`, 실측과 최종 회귀의 고정 HEAD `6b7d19a5`. 후속 소스 교정 `47d5e42e`는 별도 작업 트리에서 검증했고, 실측이 끝난 뒤 통합한다. 이전 `a337969b` 16개 검사 묶음과 `e2667929` 6개 검사 묶음은 해당 HEAD의 증거로 따로 보존한다.
- 705f042e 고정 검증: fallback 69, contract 198(skip 1), 공통 책임 10, managed completion 14, gateway 42, orphan 7 PASS. 생성 projection 20개 PASS. 최초 adaptation 검사에서 OPERATIONS 지시 수 1개 초과를 확인했고, 통합 문서의 중복 설명을 삭제해 기존 상한 안으로 복구했다.
- 공통 책임 시험은 확정 성공+지연/관측 불가, 정리 미확정 terminal 행, 두 프로세스의 동일 실패 retry 경쟁, 등록 후 첫 기동 경쟁, retry 제안 뒤 성공 확정, controller 재시작, 오래된 알림 억제, 관측 도구 실패 후 회복을 확인한다.
- 실제 socket gateway 시험은 살아 있는 fixture process를 두고 supervision context 전달·중복 억제·행 불변을 확인한다. 이는 실제 모델/TUI 수신 시험과 구분한다.
- [33]의 실제 d=1 리뷰 `att-29dfc6a3a3dc4b88b65fff8e4452e289`는 1268.92560412초에 보고서를 작성했고 실제 부모 `01a08e11-2b08-7393-a36d-702aeca9d6bb`가 attention receipt를 받았다. 그 리뷰의 verdict는 FAIL(major 3건)이었고, 해당 수정은 be684cf8에 반영됐다.

긴 회귀검사를 돌리는 동안 소스를 수정하여 runtime snapshot 불일치를 만든 1회 실행은 실패 자료로 보존했다(`/tmp/structural-fallback.log`). 이후 고정 소스의 격리 실행은 69건 모두 통과했다(`/tmp/responsibility-705f042e/fallback.log`). 테스트의 판정 기준이나 실행 중 원장 데이터를 바꾸어 성공으로 만든 것은 아니다.

고정 `6b7d19a5` 검사: responsibility 14 / contract 221(skip 1) / join 113 / registry 95 / human gate recovery 41 / completion marker 30 / serial advance 26 / worker guard 47 / managed completion 14 / gateway 45 / session sweep 17 / rewake 160 PASS. 생성 projection 20개와 adaptation boundary PASS. route consumption 12건 중 실패가 있었으며 아래 교정으로 해결했다. 로그: `/tmp/responsibility-final-6b7d19a5/`.

교정 `47d5e42e`: 보존된 semantic marker 읽기에 전체 실행 종료 증명을 추가 요구한 것은 범위 확대 회귀였다. 읽기 경로는 기록된 unresolved conflict를 공통 `completion_conflict_attempt`로 확인하고, 실제 실행 기동은 기존 `completion_attempt_readiness`의 증명을 계속 요구하도록 분리했다. 기존 기대값을 바꾸지 않았다. route consumption 12 / responsibility 14 / contract 221 / join 113 및 생성 20·경계 PASS. 로그: `/tmp/consumption-correction-check/`.


## 이번에 제거한 추가 막다른 경로

- `0338df73`: 실제 d=1 행에는 없던 `session_generation` 필드를 fixture가 만들어 놓았음을 실측 원장에서 확인했다. 통보 의무는 attempt/parent/batch에 묶고, 전달자는 실제 gateway와 접속한 뒤 claim 시 현재 수신 세대를 결속한다. 접속 실패가 통보 생성 실패나 영구 미전달이 되지 않는다.
- `e2667929`: 전달 claim 횟수 8회 초과 시 영구 거부하던 경로와 같은 기준의 sweep 필터를 삭제했다. claim은 전달 소유권 이동 횟수이며 실제 전송 횟수가 아니다. 기존 live lease 배제·전송 간격·gateway 전송 이력은 유지한다. 전송 전 9회 실패 후 1회 전달, 13회 모호한 조회 후 재전송 없음, 만료 claim 10개 뒤 재수신을 검사했다. 상한 숫자를 올린 변경이 아니다.
- `3e260260`: 분류기별 우선순위로 이미 확정된 terminal 결과와 충돌 판정을 덮어쓰던 가지를 제거했다. PASS/FAIL 및 기존 영수증은 그대로 두고, 충돌이 있으면 공통 판단이 다음 단계·재시도·직렬 기동을 보류한다. route 종료/재사용도 같은 완료 준비 검사를 소비한다.
- 충돌 보류에는 복구가 연결된다. `dispatch-registry.py resolve-terminal-conflict --jobs <jobs> --attempt <id>`는 읽기만 하며 전체 충돌과 정확한 원장 행 hash를 보여 준다. 오너가 증거를 검토한 뒤 `--review-evidence <report> --expected-row-sha256 <hash> --apply`로 처분을 기록한다. 원장 잠금 안에서 동일 행을 재확인하고 원래 결과를 보존한 채 소비를 재개한다. 하나의 시도 안에 각 관측과 검토 증거를 보존하므로 이미 검토한 A가 미해결 B를 숨기거나, B 검토 뒤 A가 다시 작업을 막지 않는다.

확정 결과와 현재 소비 허가는 서로 다르다. 충돌 알림을 받았다는 사실도 충돌을 해소하거나 작업을 완료하지 않는다. 이 검토 CLI는 운영 원장에 실행하지 않았으며 격리 fixture에서만 검증했다.

## 실제 실행 근거와 아직 남은 합격선

| 항목 | 근거 | 판정 |
|---|---|---|
| 21분 이상 d=1 리뷰 쓰기와 부모 통보 | HEAD 56746fb4, att-29dfc6a3a3dc4b88b65fff8e4452e289, 쓰기 1268.92560412초, 부모 01a08e11-2b08-7393-a36d-702aeca9d6bb | 실제 정상 경로 PASS. 리뷰 지적 3건은 be684cf8에서 수정. |
| frame 두 하네스와 두 자동 wake | HEAD 7db4ce09, route rt-f94885fba268775b, Codex att-a4e566dfb0ab444f9341bc9f14eb69d1 + Claude att-223896abecdf4178a5d10a9e0713b121, 동일 부모 01a08e56-2fec-7ae3-8898-765dae020431 | actual marker 2개·wake 2회 확인. |
| 승인 전 owner 기동 금지와 실제 질문 | 같은 frame r2, human-gate-not-raised / child_spawned=0, 정식 frame-review raise | 확인. 실제 사용자 답변을 대리하지 않으며 release 이후 부분은 대기. |
| 지연 관측→통보→같은 attempt 정상 완료 | HEAD 6b7d19a5, 새 private parent 01a08e7f-750c-7291-8e5b-f08ec638e2de (wB:pG), dispatch 호출에만 completion timeout 60초, 실제 자식 150초/finite watchdog 600초 이상 | 진행 중. 가짜 age 또는 운영 row 편집 없음. |
| frame standard 왕복 및 최종 owner의 intent 읽기 | 별도 실측 필요 | 미합격/미완료. |

frame 증거: `.agent_reports/campaigns/2026-09-10_frame-bootstrap-layer/2026-09-11_frame-live-parent-canary-r2/artifacts/dev_logs/r2-parent-wakes-observed.{json,txt}`.

실제 지연 canary는 살아 있는 프로세스의 정상적인 지연이다. 이것을 PID namespace의 관측 불가 실측이라고 부르지 않는다. 관측 불가·감독자 종료·중복 재시도·실패 정리는 각 격리 정책 및 실프로세스 검사 근거로 구분한다. 실제 OpenCode 사용자 수신은 이번에 새로 검증했다고 주장하지 않는다.

## 검증의 오류도 보존

`dispatch_completion_marker` 4건은 설치된 release에서 compile한 뒤 fixture runtime으로 start하여 표식 검사 전 root mismatch로 실패했다. 실제 guard stderr에서 runtime/launch/jobs 차이를 확인하고 compile과 adapter 검증이 같은 fixture 환경을 사용하도록 고쳤다. 거부 기대값을 삭제하거나 운영 기동 검사를 우회하지 않았다. 수정 후 30건 PASS (`/tmp/terminal-commit-responsibility-check/dispatch_completion_marker-fixed.log`).

`workflow_supervisor` 1건의 빠른 `exit 7` fixture는 PID 신원을 읽기 전에 종료될 수 있었다. stdin pipe로 실제 자식을 신원 캡처 후 종료시키도록 고쳤고, exit·정리·보호 검사는 유지했다. 재실행 123건 PASS.

## 런타임 지원과 실현 범위

Codex의 기존 App Server turn/start·turn/steer 운송과 Claude의 기존 asyncRewake/다음 prompt sweep을 재사용했다. 공식 문서상 start와 steer의 역할, asyncRewake의 exit 2 및 비동기 hook 수명은 서로 다르다. 실행 방식을 통일했다고 주장하지 않는다. 공통인 것은 실행 결과·정리·재시도·전달 책임이다. OpenCode의 prompt carrier도 같은 큐와 의미 검사를 소비하며 실제 런타임 수신 여부는 실측된 범위만 별도로 기록한다.

참고: [Codex App Server](https://learn.chatgpt.com/docs/app-server), [Claude hooks](https://code.claude.com/docs/en/hooks). 2026-09-11 공식 문서를 확인했다.
