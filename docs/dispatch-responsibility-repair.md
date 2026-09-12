# 분사 책임 구조 수리 — 검증 기록

현재 상태: 공통 실행·완료·재시도·정리·통보 책임을 연결했고, 실제 Codex light owner와 OpenAI Luna를 쓰는 OpenCode light owner 모두 자식 실행부터 workflow/route 종료와 cycle 봉인까지 확인했다. OpenCode 리뷰의 동일 Codex 부모 success 자동 전달도 확인했다. 마지막 OpenCode owner 실측에서 발견한 합의 내용 누락과 수동 대기의 조기 실패 판정은 공통 입력·완료 경로에서 수정하고 회귀검사를 통과했다. 이 두 후속 수정의 근거는 실제 기록 재생과 테스트이며 새 모델 왕복 PASS라고 부르지 않는다. 소스 3b9ea255를 통합 트리와 로컬 main에 병합했다. 원격 main 푸시·릴리즈·설치는 사용자 확인 전 보류한다.

아래는 HEAD별 진행·실패 기록이다. 각 절의 당시 대기/미검증 상태를 최종 상태로 읽지 않도록 최신 실측과 후속 수정은 마지막 두 절에 모았다. 정상 운송과 작업 내용의 정확성은 따로 판정한다.

사용자가 지적한 문제는 개별 어댑터의 기능 부족을 넘어선다. 여러 관측자가 실행 상태를 각각 판정하면서 재시도와 거부 권한을 갖고, 복구가 실패했을 때 누가 작업을 유지하거나 사용자에게 돌려줄지는 빠져 있었다. 과거 2026-09-01 복잡도 진단과 이번 Cairn·직렬 chain·리뷰 실측에서 같은 형태가 반복됐다. 이번에는 기존 수정을 유지하면서 결정 권한과 후속 책임을 공통 코드에 모았다.

## 책임과 제거한 중복

| 구간 | 최종 책임 | 변경 |
|---|---|---|
| 합의한 작업 전달 | 공통 worker bootstrap | 해제된 frame gate의 기록된 이해·답변을 owner와 후속 worker에 직접 전달한다. 계획 노드나 오너의 수동 prompt 복사에 의존하지 않으며 역할 preset이 작업 범위를 대신하지 않는다. |
| 실행·수명·실패 정리 | 실행 경계와 finite watchdog | 실제 runner/fence와 등록 watchdog 신원을 구분한다. 자손 정리 증명 뒤 lease를 반환한다. 관측이 부족하면 정리 의무를 유지한다. |
| 완료 확정 | jobs 잠금 안의 exact terminal writer | 프로세스 종료와 성공을 구분하고, 확정된 결과를 후속 관측이 뒤집지 못한다. `dispatch_attempt_policy`가 의미 결과와 남은 정리 의무를 분리한다. |
| 재시도 | 동일 jobs 잠금의 retry claimant | 감시자는 exact predecessor를 제안한다. 실제 등록 시 확정 결과·정리 증거·동일 실패의 기존 후속 시도를 다시 확인한다. 명시적인 새 리뷰 round는 별개다. |
| 대기·복구 | 공통 join과 `dispatch_supervision.wait_for_batch` | Claude/Codex supervisor와 serial driver의 대기 횟수 초과 사망 루프 세 곳을 없앴다. 수동 bounded wait의 별도 성공 note 목록도 제거하고 같은 terminal writer·정리 복구·현재 소비 판정을 사용한다. 관측 도구 실패에도 작업을 유지하며 복구·부모 인계를 연결한다. |
| 감독자 종료 | exact orphan watcher | helper 반환만으로 상태를 지우던 경로를 없앴다. 정리가 확인되지 않으면 상태와 부모 인계 기록을 남긴다. |
| 사용자 통보 | 기존 pending-delivery 큐와 부모 runtime carrier | 새로운 별도 큐를 만들지 않았다. human-gate와 supervision은 같은 claim/send/acceptance 운송을 사용하며 판단 의미는 구분한다. 알림 수신은 작업 완료가 아니다. |

완료 영수증 키와 digest의 수동 복사 네 곳을 공통 `dispatch_receipt_identity`로 합쳤다. 임시 jobs 원장을 만들어 외부 liveness 명령의 exit 3을 완료 근거로 소비하던 join 우회 경로를 제거했다. Codex에만 있던 receiptless 복구 루프도 공통 join으로 이동했다. 기존 `process-exited` 캐시가 확정 결과를 건너뛰어 중복 retry를 만드는 경로는 앞선 세 커밋에서 수정했고 이번 통합에 포함했다.

직렬 체인 원 작업과 Fleet R1 표시는 앞서 `45d7567d`·`98b4269a`로 수정하고 v2.139.1에 설치된 별도 완료 건이다. serial의 1..16 한도와 parallel의 max_slices=4를 구분한 기존 결정을 유지했다. OpenCode child는 허용하지만 OpenCode owner의 serial supervision은 여전히 지원하지 않으며 single-session fallback을 안내한다. 이번 수리가 모든 런타임에서 같은 실행 방식을 만든다고 주장하지 않는다.

단계는 입력과 결과를 정의한다. 별도 프로세스 기동 여부는 실행 방식의 선택이다. 이번 수리는 사용자 지시대로 정식 분사 없이 직접 수행했다. 독립 검토와 QA 결과는 보존했다.

## 검증 근거

- 공통 수정: `704ae7cf` → `cc1791ba` → `de632e5c` → `705f042e`.
- 리뷰 수명·완료 전달: `be684cf8`까지 포함. 21분 실측의 원본 source는 `56746fb4`이며, 이후 수정의 실측이라고 바꾸어 쓰지 않는다.
- frame: `daa82355` + `7db4ce09`(실제 모델 role/profile 전달) + `5929e741`(route/cycle→frame→질문→owner 순서 및 상대 산출물 기준) 포함. 실제 두 하네스 완료·자동 wake, 승인 전 owner 거부, 실제 답변 후 release·owner 입력 읽기를 확인했다. workflow closure도 아래 후속 교정으로 완료했다. 새 standard 오너 검증은 남아 있다.
- 통합 경로: `/home/nas/user/Uihyeop/personal/hearting-wt/dispatch-responsibility-integration`, 실측과 최종 회귀의 고정 HEAD `6b7d19a5`. 후속 소스 교정 `47d5e42e`는 별도 작업 트리에서 검증했고, 실측 종료·증거 고정 뒤 통합했다. 이전 `a337969b` 16개 검사 묶음과 `e2667929` 6개 검사 묶음은 해당 HEAD의 증거로 따로 보존한다.
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
| 승인 전 owner 기동 금지와 실제 질문 | 같은 frame r2, human-gate-not-raised / child_spawned=0, 정식 frame-review raise | 확인. 원 부모의 실제 답변, release와 owner intent 읽기까지 확인했다. |
| 지연 관측→통보→같은 attempt 정상 완료 | HEAD 6b7d19a5, 새 private parent 01a08e7f-750c-7291-8e5b-f08ec638e2de (wB:pG), dispatch 호출에만 completion timeout 60초, 실제 자식 150초/finite watchdog 600초 이상 | supervision·실제 쓰기 PASS, 정상 completion FAIL. canonical success가 부모 attention으로 바뀌었다. 가짜 age 또는 운영 row 편집 없음. |
| quick owner intent 읽기·workflow closure | 아래 원답변/closure 증거 | PASS. standard의 새 Codex/OpenCode 오너 검증은 미완료. |

frame 증거: `.agent_reports/campaigns/2026-09-10_frame-bootstrap-layer/2026-09-11_frame-live-parent-canary-r2/artifacts/dev_logs/r2-parent-wakes-observed.{json,txt}`.

실제 지연 canary는 살아 있는 프로세스의 정상적인 지연이다. 이것을 PID namespace의 관측 불가 실측이라고 부르지 않는다. 관측 불가·감독자 종료·중복 재시도·실패 정리는 각 격리 정책 및 실프로세스 검사 근거로 구분한다. 실제 OpenCode 사용자 수신은 이번에 새로 검증했다고 주장하지 않는다.

## 실제 완료 오배송에서 제거한 중복 판단

6b 실측 attempt `att-a1b4ca9c1b174701be782c02f38709f9`는 150.000102947초 hold 뒤 보고서를 썼다(778 bytes, SHA256 `78006e7276e7f20eb7fe12423cd1b2596f7767deaa9e6b59224215bb6778a60a`). 실제 부모는 03:32:57.204Z에 `join-deadline`을 받았고, 03:36:37.312Z에는 성공 영수증을 attention으로 바꾼 완료 알림을 받았다. 인쇄된 `--failure-detail` 수확 명령도 PASS 행이라 거부됐다. 원장·과거 영수증·보고서는 수정하지 않았다.

원인은 전달 snapshot이 marker/supervisor/subsession만 별도로 성공 판정하여 terminal writer의 `completed-review` 성공 영수증을 버린 것이다. 생산자별 boolean 두 개를 전달자마다 운반하던 구조를 없애고, 정확한 attempt·부모·하네스·digest에 결속된 기존 봉인 영수증을 공통 완료 근거로 소비한다. 전달자는 현재 행/process CAS, 자손 정리, 미해결 충돌을 계속 확인한다. 쓰기 권한 검사를 종료 후 다시 실행하지 않는다. 닫힌 리뷰의 쓰기 lease가 끝났다는 이유로 이미 확정한 성공을 뒤집지 않는다. 영수증 도입 전 행의 기존 proof는 호환 읽기 경계에만 남긴다.

실프로세스 검사를 terminal close에서 끝내지 않고 current snapshot→전달 영수증까지 연장하자, registry reconcile이 먼저 닫는 경우 `failure_class=pass`를 누락하는 두 번째 경로도 드러났다. reaper/join/registry가 같은 review terminal evidence를 기록하도록 합쳤다. Claude/Codex supervisor, gateway, rewake의 자동 attention 안내는 일반 exact `--status done` 수확을 사용한다. `--failure-detail`은 명시적 실패 진단에만 남겨 PASS의 정리·충돌 의무도 조회할 수 있게 한다.

후속 교정 검사: responsibility 14 / contract 223(skip 1) / join 113 / registry 95 / review lifecycle 19 / serial supervisor 30 / managed completion 14 / gateway 45 / sweep 17 / rewake 160 / route consumption 12 / Claude supervisor 71 / Codex supervisor 33 PASS. 생성 projection 20개와 adaptation boundary PASS. 로그: `/tmp/completion-consumer-final/`.

정확한 6b 실측 기록: `.agent_reports/campaigns/2026-09-10_review-lease-watchdog/2026-09-11_supervision-live/artifacts/dev_logs/supervision-live-observation.json`. 이 실패를 고정한 뒤 새 source·부모·cycle로 동일 60초 join/150초 hold 경로를 재검증했다. 다음 절의 09b 결과는 별도 실제 시도의 성공이며 6b 실패를 덮어쓰지 않는다.

## 09b 실제 재검증과 후속 책임 경계

새 부모 `01a08e9b-2977-7ac0-b3ba-01e534889fcc`, Luna attempt `att-5d1fa4e292ea4da78fb27d461696d61f`는 같은 source `09b687ac`에서 04:05:49.467Z supervision과 04:08:24.746Z 정상 completion을 각각 자동 수신했다. 원장의 `completed-review/pass`, 봉인 success, 부모의 `success/advance-completed/done`이 일치했다. hold는 150.000073초, 보고서는 218 bytes / SHA256 `80fb9b2abcc960c18fa6f00efac2ff933e6c7d8cc611b9fe002e8b1722b1fbdf`다. 기동 후 수동 부모 입력은 없었다.

부모는 이미 승인된 별도 읽기 검증을 다시 질문했다. 04:10:46.143Z 빈 답변 반환 뒤 자율 재개했으나 잘못된 attempt 문자열과 끝의 점 인자로 두 번 실패한 뒤 04:11:38.448Z 정상 수확했다. [33]의 별도 정상 수확도 1회 있어 전체 성공 읽기는 2회다. 자동 두 전달·실제 쓰기·회수 가능성은 PASS이며, 무오류 자율 후속이나 전체 관측자의 단 한 번 읽기는 PASS가 아니다. 정상 completion은 원래 추가 수확을 요구하지 않는다. 별도 검증을 제품의 새 마무리 의무로 바꾸지 않았다.

원본: `.agent_reports/campaigns/2026-09-10_review-lease-watchdog/2026-09-11_supervision-live-r2/artifacts/dev_logs/supervision-live-r2-observation.json`. 최종 관측 04:14:05.387Z source clean, 시험 자식·watchdog·sidecar·reaper 종료를 기록했다. 시험 부모 두 개는 증거 cutoff 뒤 `/exit`로 정리했으며 실제 frame 승인 대기 부모는 보존했다.

이 후속 재질문의 원인이 운송 문구였다고 확정하지 않는다. 다만 `Run only these commands`를 운송자가 새 권한 제한처럼 전달하는 것은 책임 범위를 넘는다. 공통 command projection과 후속 안내로 세 batch renderer 및 rewake의 수동 명령 조립을 합쳤다. 정확한 실행 인자에는 문장 끝 마침표가 섞이지 않는다. 완료 기록의 미처리 동작만 안내하고 기존 승인과 human gate가 계속 권한을 소유한다. 문구 변경만으로 모델의 무오류 행동을 증명했다고 주장하지 않는다.

후속 지시 공통화 검증: join 114 / Claude supervisor 71 / Codex supervisor 33 / gateway 45 / rewake 160 / generated 20 / adaptation boundary PASS (`/tmp/completion-context-fixed/`). 첫 검사는 rewake 명령 뒤 설명 결합과 Claude의 공통 실행 표면 설명 누락을 잡았고, 실제 명령 줄 분리 및 하네스 선택 불변 설명을 공통 함수에 유지한 뒤 재검증했다. 이 검증은 09b 실제 수신과 별도 소스 근거다.

6b 실측 행의 격리 복사본으로도 09b snapshot→delivery success를 확인했다. 그 복사본에 실제 terminal CAS로 충돌을 기록하면 committed proof와 봉인 receipt bytes를 유지하면서 attention으로 소비를 보류했다(`/tmp/completion-conflict-consumption.json`). 운영 원장은 수정하지 않았다.

## 사용자 답변 대기와 질문 창의 수명

사용자가 frame 질문의 자동 만료를 지적했다. 설치된 `codex-cli 0.153.4`의 실제 생성 schema와 같은 release tag의 구현을 확인했다. 기본 모드는 `isBlocking=false`를 발급하고 TUI는 60초 비표시 유예 + 60초 countdown 뒤 `answers={}`를 반환한다. `autoResolutionMs`는 이 버전에서 deprecated이며 null로만 바꿔서는 타이머가 꺼지지 않는다. 근거: [0.153.4 질문 handler](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/tools/handlers/request_user_input.rs), [동일 버전 TUI 타이머](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/tui/src/bottom_pane/request_user_input/mod.rs). 생성 schema는 `/tmp/codex-question-schema-01534/ToolRequestUserInputParams.json`에 보존했다.

공통 workflow 계약에 사용자 결정은 시간 경과나 빈 답변으로 확정하지 않는다고 명시했다. 결정 대기와 질문 복구는 gate owner가 유지하며 독립적으로 승인된 작업은 계속한다. Codex gateway는 유효한 질문 요청의 native 대기 정책만 `isBlocking=true`, 구버전 `autoResolutionMs=null`로 투영한다. 질문 ID·내용·선택지·실제 답변과 직접 취소는 그대로 통과시키고 자체 답변이나 별도 승인 주체를 만들지 않는다. gateway의 Fleet 표시가 종료되어도 workflow gate는 별도로 유지된다. 해당 기능을 제어할 수 없는 클라이언트에는 나중에 답할 수 있는 일반 대화 질문을 남긴다.

검증: 실제 socket을 통과하는 최신·구버전·이미 무기한인 요청, ID/내용 보존, 응답 합성 없음, 실제 답변/빈 취소 응답 전달을 포함한 gateway 46건 PASS. managed entry, human gate receipt, generated projection 20개와 adaptation boundary를 함께 검사했다(`/tmp/question-wait-checks/`, `/tmp/question-wait-gateway.log`). 모델 기동이나 실제 TUI 2분 대기는 수행하지 않았다. 이 수정은 소스 동작과 protocol 검증이며, 실행 중인 pF 또는 이 대화 창의 제한을 해제했다고 주장하지 않는다. main/release/install은 계속 보류한다.

사용자는 만료된 질문을 원래 [53] 부모에서 다시 표시하라고 요청했다. [09]가 자기 대화에 복제한 질문은 잘못된 복구였으며 승인 근거에서 제외했다. 원 부모 `01a08e56-2fec-7ae3-8898-765dae020431`의 실제 native `request_user_input` 호출 `call_DL4XuinF6cneXzz7xsZlpMTI`는 05:16:33.080Z에 같은 두 질문을 표시했고, 05:16:40.585Z 실제 tool output에서 `understanding_confirmed=["예 (Recommended)"]`, `record_style=["다시 검사 (Recommended)"]`를 받았다. 원본 rollout과 실제 화면을 읽어 확인했으며 [09]/[33]은 답변을 대신 입력하지 않았다. 같은 route `rt-f94885fba268775b`의 gate만 이어가며 새 route·다른 cycle의 승인을 만들지 않는다. 원 pF 프로세스는 여전히 7db4ce09를 실행하므로 이 재표시를 de4b8601 시간 제한 수정의 실측으로 부르지 않는다.

## 검증의 오류도 보존

`dispatch_completion_marker` 4건은 설치된 release에서 compile한 뒤 fixture runtime으로 start하여 표식 검사 전 root mismatch로 실패했다. 실제 guard stderr에서 runtime/launch/jobs 차이를 확인하고 compile과 adapter 검증이 같은 fixture 환경을 사용하도록 고쳤다. 거부 기대값을 삭제하거나 운영 기동 검사를 우회하지 않았다. 수정 후 30건 PASS (`/tmp/terminal-commit-responsibility-check/dispatch_completion_marker-fixed.log`).

`workflow_supervisor` 1건의 빠른 `exit 7` fixture는 PID 신원을 읽기 전에 종료될 수 있었다. stdin pipe로 실제 자식을 신원 캡처 후 종료시키도록 고쳤고, exit·정리·보호 검사는 유지했다. 재실행 123건 PASS.

## 런타임 지원과 실현 범위

Codex의 기존 App Server turn/start·turn/steer 운송과 Claude의 기존 asyncRewake/다음 prompt sweep을 재사용했다. 공식 문서상 start와 steer의 역할, asyncRewake의 exit 2 및 비동기 hook 수명은 서로 다르다. 실행 방식을 통일했다고 주장하지 않는다. 공통인 것은 실행 결과·정리·재시도·전달 책임이다. OpenCode의 prompt carrier도 같은 큐와 의미 검사를 소비하며 실제 런타임 수신 여부는 실측된 범위만 별도로 기록한다.

참고: [Codex App Server](https://learn.chatgpt.com/docs/app-server), [Claude hooks](https://code.claude.com/docs/en/hooks). 2026-09-11 공식 문서를 확인했다.


## 라우팅 선택과 마무리 책임 정리

사용자의 완료 기준은 “기본값은 선택을 돕고, 하네스는 내부 절차를 대신 책임진다”이다. 모델·단계 preset을 맞추기 위한 거부와 재조립을 사용자에게 떠넘기는 것도 이번 수리의 결함 범위에 포함했다. 문서 바이트 감소를 인지 부담 감소의 증명으로 삼지 않는다.

- `16047ba9`: intensity별 owner 모델 **강제 비교**를 제거했다. compile과 verify가 `_resolve_owner_profile` 하나로 요구를 해석하고, 요구가 없을 때만 기존 기본값을 쓴다. quick의 한 프로세스와 owner가 서로 다른 모델 선택을 갖던 경계도 합쳤다. QA·실행 단계와 모델 선택을 분리하며 새 전역 설정은 만들지 않았다.
- 같은 커밋의 공통 workflow writer가 terminal gate 확인 후 필요한 합법적 전이를 끝까지 수행한다. 전체 전이를 먼저 검증하고, 실패·사람 판단·취소는 보존한다. 중간 append 뒤 중단되거나 명령을 다시 호출해도 journal의 남은 부분만 수행한다. 호출자에게 수동 상태 변경 순서를 요구하던 부담을 제거했다.
- `compose --shape staged`는 기본 recipe를 바로 발급한다. 전체 그래프를 쓰기 위해 `compile`로 바꾸거나 node 이름을 나열할 필요가 없다. 명시적인 `--graph plan,test,report`는 그대로 유지한다. 선택하지 않은 plan-check를 강제로 추가하지 않고, 그 검토 소비자를 요구하던 inherited parallel preset을 제외·기록한다.
- `auxiliary_arbiter`를 선언한 검토 gate는 보조 의견을 처리할 수 있는 기능이다. 실제 선택된 보조 그룹이 있을 때만 그 의무가 생긴다. 검토 단위를 단독 재사용할 때 원래 preset의 producer까지 강제하던 역방향 검사를 제거했다. 선택된 그룹의 실제 검토·증거 의무는 검증한다.
- 같은 사용자 gate를 올리는 독립 frame 두 다리를 조립기가 직렬로 연결해 자신의 gate에 막히게 하던 간선을 수정했다. 두 다리는 이전 의존성을 공유하고, 다음 작업은 두 결과를 모두 기다린다.

실제 quick 원답변 증거: `/tmp/frame-quick-original-answer-owner-observation.json`. 원답변 05:16:40.585Z → release 05:20:20.698Z → owner `att-5061090efcf74f1ab538f99ac7c075de`의 실제 intent 읽기 05:21:17.707Z → 부모 success 05:23:40.620Z. 합계 Markdown 독립 실행 exit 0. 기존 Claude owner/profile을 유지한 과거 시도이며 새로운 light 테스트로 계산하지 않는다.

실제 closure 증거: `/tmp/frame-quick-terminal-closure-observation.json`. 수정 source `16047ba93e067fb029f951367ea22d584b0bdad0`의 정상 `workflow-supervisor complete`가 원 route `rt-f94885fba268775b`를 COMPLETE로 기록했다. journal 3→6, 성공 상태 전이 3개 추가. 반복 호출 exit 0이며 journal 불변. 원 route·답변·intent·검증 산출물 바이트 불변, 모델 재기동 0. 이후 정상 producer finalize도 같은 PID의 단일 실행으로 exit 0: cycle completed/storage sealed/lineage committed, artifact 15개. manifest SHA256 `dbff683c78241a28e1edd71eb98c1981430f0b7a7174cffb71b1e98af77ed40d`가 producer 및 admission 색인과 일치했다. `/tmp/frame-quick-cycle-finalized-observation.json`과 원 cycle의 `artifacts/dev_logs/quick-closure-evidence.json`에 보존했다.

최종 소스 검사: route 387 / compose CLI 17 / profile demand 24 / workflow 127 / topology 45 PASS. 생성 20그룹·adaptation boundary PASS. quick/standard owner light를 실제 selector→세 adapter parser/resolver로 연결한 6경로도 통과했다(`/tmp/surface-owner-adapter-parity.json`); 모델을 호출한 실측으로 계산하지 않는다. 로그 `/tmp/surface-final-*.log`.

수정 전 ce519에서도 재현되는 기존 fixture 실패 3종을 별도 확인했다(`/tmp/closure-baseline-failures.log`). terminal fixture는 현재 완료 writer의 `note=completed-marker`를 빠뜨렸고, 두 continuation fixture는 읽을 수 없는 원장에서 받은 차단 결과를 새 route로 가정했다. 실제 완료 형식을 넣고 차단·source 보존·새 node 0을 검증하도록 기대값을 수정했다. 운영 판단을 느슨하게 바꿔 테스트를 통과시킨 것이 아니다.

## 부모가 소유하는 완료 전달 경로

4bc standard 실측의 OpenCode frame은 실제 Codex 부모 아래에서도 `parent-identity-unmatched/poll-fallback`을 출력했다. 자식 어댑터 이름을 부모 실행 환경으로 간주한 구현이었다. `dispatch_parent_completion.py`가 세 어댑터의 부모 확인·전달 경로 선택·등록된 경로 유지·기동 전 전달자 준비를 함께 소유한다. 자식별 세 분기와 두 sidecar 구현을 공통 함수로 교체했다. OpenCode 자식에도 실제 Codex 부모 gateway를 연결하며, witnessed thread successor도 같은 함수가 반영한다. sidecar와 gateway에 남아 있던 두 하네스 목록은 공통 하네스 목록을 사용한다. OpenCode 부모의 native 자동 wake를 구현했다고 주장하지 않으며 그 부모에는 기존의 명시적 유한 대기 경로가 남는다.

검증: 세 실제 어댑터 main에서 정확한 행 등록→전달자 준비 실패→자식 기동 0·예약 반환·실패 종결을 확인했다. 부모×자식×worker type, fork 후 부모 변경, 등록 뒤 운송 변경 거부, OpenCode 자식의 실제 sidecar 프로세스→제어 socket 및 gateway→mock App Server 전달도 검사했다. 공통 8 / Codex adapter 58 / Claude adapter 46 / OpenCode adapter 28 / managed completion 14 / gateway 47 PASS, generated 20·adaptation boundary PASS (`/tmp/parent-delivery-check-*.log`). 모델 부모의 새 자동 수신 실측은 별도 잔여다.

세 adapter suite의 과거 owner identity fixture 오류는 수정 전 4bc에서도 각각 동일한 3개 오류로 재현했다(`/tmp/parent-delivery-baseline-*.log`). 존재하지 않는 route 경로를 의도적으로 넣는 identity 분류 검사에 frame gate를 외부 경계로 명시했다. 실제 main의 등록/기동/종료 관측은 유지했고 새 운송 검사는 실제 main에서 수행한다. main/release/install은 계속 보류한다.

## 정리 대기와 알림 소비의 충돌 제거

join에 남아 있던 예외는 유효한 완료 marker가 있으면 살아 있는 tagged 자손을 `quiescent`로 바꾸어 ready를 만들었다. 뒤쪽 전달 판정은 실제 자손을 다시 관측해 attention을 만들었다. 이 치환을 제거하여 동일한 공통 결정이 실제 자손 정리까지 기다리게 했다. 성공 기록은 보존하고, 실행 경계가 정리를 소유하며, 오래 걸리면 기존 감독자의 유한 관측과 durable notice가 사용자에게 이어진다. 실제 자손 프로세스를 유지한 동안 pending, 종료한 뒤 같은 행/marker bytes에서 success/advance-completed가 나오는 회귀를 추가했다.

4bc Codex owner는 일반 exact `--status done` 수확 후에도 `supervisor-outbox-consume-failed`를 받았다. harvest가 옛 `--failure-detail` 플래그를 별도 성공 조건으로 쓰고 있었기 때문이다. 최초 교정 39e65d0e에서는 상세 출력과 읽기 성공을 분리했다(join 114 / harvest 20 / contract 223(skip 1) PASS). 뒤의 오너 종료 증거를 확인한 최종 수정에서는 harvest의 알림 소비 자체를 제거했다. 읽기·종결과 알림 전달 확인을 같은 명령의 성공 여부로 묶을 이유가 없다.

4bc의 최초 Codex frame attention은 marker 게시 직후 전달됐으며, 사후 같은 행의 현재 판정은 success였다. 당시 process snapshot은 확보하지 못했으므로 이 실측의 정확한 원인이 위 자손 치환이었다고 확정하지 않는다.

## 알림 수신 확인을 런타임 책임으로 정리

4bc 오너는 06:12:24.972Z에 `identical-redelivery-bound:3`으로 종료됐다. 모델이 수확을 실행했어도 알림 소비가 실패하자 감독자가 같은 알림을 세 번 보내고 오너를 버렸다. 최종 구현은 두 감독자의 명령 허용 사전 검사·동일 알림 삼진 종료·미종결 자식에 대한 모델 재촉 후 종료를 제거한다. `registered-parent-park`는 명시적인 terminal cleanup scope만 집행한다. 일반 작업에 별도의 알림 기반 도구 허용 목록을 적용하지 않는다.

공통 `acknowledge_supervisor_delivery`가 수신 턴 완료 뒤 정확한 알림만 멱등 확인한다. worker 결과·marker·재시도 권한을 변경하지 않으며, 다른 알림으로 교체되었으면 이전 턴의 확인은 거부한다. 모델은 수확 명령으로 runtime outbox를 조작하지 않는다. 미종결 자식은 `wait_for_child_settlement`가 기존 공통 attempt policy와 정확한 reconcile을 사용해 이어받는다. 불명확한 관측이 반복되면 기존 durable notice를 전달하고 감독자가 계속 기다린다. 모델 재호출·토큰 소비·오너 사망으로 관측 실패를 대신하지 않는다. 실제 harvest를 관측하지 않은 `exact_harvest_ns`는 null로 둔다.

실제 fake-runtime 프로세스를 사용한 Codex/Claude 회귀에서 알림 수신 1회 뒤 모델 수확 0회로 정상 종료한다. 관측 불가 자식은 두 모델 턴(최초+알림) 뒤 감독자가 생존하며 notice를 생성하고, fixture가 정확한 종결·정리 증거를 제공하면 모델 추가 턴 없이 끝난다. 명시적인 정리 권한 제한은 별도 회귀로 유지한다. 이 검사는 모델 기반 owner canary 완료 주장과 구분한다.

최종 회귀: Codex supervisor 34 / Claude supervisor 69 / join 115 / harvest 20 / supervision 14 / cleanup hook 7 PASS. `open`과 `done`인 관측 불가 자식을 각각 검사했고, 일반·상세 harvest가 알림 상태를 바꾸지 않는 것을 실제 CLI로 확인했다. 과거 fake join이 기록하던 근거 없는 done 행에는 실제 자식 프로세스를 띄우지 않은 fixture의 quiescence를 명시했다. generated projections와 adaptation boundary도 PASS. 로그는 `/tmp/runtime-ack-complete-*.log`, `/tmp/runtime-ack-final-*.log`, `/tmp/runtime-ack-generated.log`, `/tmp/runtime-ack-boundary-final.log`에 있다. 실측 4bc의 마지막 test 정리 미입증은 이 fixture 통과로 해소됐다고 주장하지 않는다.

## 잘못된 cycle 쓰기 증거 복구

OpenCode 부모 canary의 Codex frame `att-ea04186aba80442fadd029cc04556d67`는 다른 cycle의 `direction-brief.md`를 덮어썼다. 원래 Codex rollout의 118/139행 native patches를 순서 적용한 5227bytes가 원 marker SHA256 `4d78735e0ec62ff00e71e601b31d22194cf203409e7aa28752993b598095ca47`와 정확히 일치하여 원문을 복구했다. 덮인 4201bytes와 패치·복구 영수증은 원 cycle `artifacts/dev_logs/direction-brief-restoration/`에 보존했다. 원장·marker 수정과 재기동은 없고, 이후 잘못된 쓰기는 정상 검증으로 인정하지 않는다. 환경 변수 미전달이라는 보고의 probe 정규식은 `AGENT_ARTIFACT_*`를 매칭하지 않으므로 그 원인 주장은 추가 확인이 필요하다. 다른 cycle 쓰기 자체는 실제 파일과 marker hash로 확인됐다.

## 부모 신원·출력 위치·실행 저장소의 책임

세 어댑터의 부모 session 기본값과 depth-0 frame 질문 생성이 공통 native identity resolver를 사용한다. OpenCode 부모 아래 Codex 다리에서 부모 ID가 사라지던 누락을 제거했다. local frame handback은 실제 OpenCode 부모가 정식 질문을 표시하는 경로이며, 비동기 자동 wake 지원 주장과 구분한다. workflow 129, selector 77, 부모 운송 10 및 실제 세 adapter parser의 부모 조합 검사가 통과했다.

세 어댑터가 같은 producer 환경 전달 함수를 사용하고 실제 출력 경로를 prompt에도 넣는다. producer `require_cycle_output`을 쓰기와 completion publish가 함께 사용하며, 환경이 빠져도 route의 기존 producer record에서 binding을 찾는다. 열린 다른 cycle의 같은 `shards/frame/**` 경로는 거부하고 정확한 output directory를 안내한다. artifact guard의 루트 아래 임의 suffix 일치도 cycle-relative 일치로 교체했다. 새 검사는 서로 열린 두 cycle의 같은 파일명, 잘못된 output 힌트, cycle env 누락, marker 생성 전 거부를 포함한다.

OpenCode 실행 저장소는 [공식 XDG 구현](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/core/src/global.ts), [설정 로더](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/config/config.ts), [인증 경로](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/auth/index.ts)를 확인했다. Codex owner 아래 OpenCode adapter가 attempt별 data/cache/state/config를 worktree에 준비하고 기존 auth와 설정을 복사하지 않고 연결한다. 설정 옆에 자동 생성하는 `.gitignore`·npm 의존성 파일도 private directory에 남긴다. 실제 `codex sandbox -P :workspace`에서 외부 fixture 경로는 EROFS/exit 1이었다. 최초 data/cache/state 준비만으로 paths/startup은 통과했으나 config load는 `.gitignore` 쓰기로 실패해 그 증거도 보존했다. 최종 설정 투영 후 `debug config` exit 0, 원래 `fixture/light` 모델 선택 유지, 원래 설정/auth bytes 불변을 확인했다. 근거 `/tmp/opencode-nested-runtime-observation.json`. 모델 기반 owner 왕복 검증을 대체하는 증거는 아니다.

producer 전체 155, capability route 387, adapter Codex 58/Claude 46/OpenCode 30, worker bootstrap/prompt 검사가 통과했다. 원본 HEAD 32d4e143을 메모리에 로드한 기준 실행에서도 producer 실패 11건을 재현했다. terminal fixture가 공식 completion writer를 거치지 않아 marker와 원장이 달랐고, lease fixture는 PID가 없는 witness-only record와 역전된 시간값으로 실제 holder 종료를 대신했다. 정식 complete와 실제 PID/start/PGID 및 witness를 가진 자식의 종료를 사용하는 fixture로 수정했다. A의 lease 운영 함수는 변경하지 않았다. 로그 `/tmp/producer-32d-baseline.log`, `/tmp/producer-context-final.log`, `/tmp/parent-scope-*.log`.

## 종료 뒤 정리와 진행 관측의 단일 책임

`done`을 곧바로 `already-terminal`로 반환하던 복구 경로를 제거했다. 공통 `resolve_attempt_cleanup`를 registry reconcile과 join이 사용한다. 정확한 terminal 행에 대해 신호 없이 프로세스 그룹·태그 자손·namespace 증거를 확인하고, 증명된 정리만 기존 원장에 CAS로 기록한다. 확정 결과·marker·기존 전달 receipt는 보존하며 cancellation receipt나 retry claim을 만들지 않는다. 프로세스가 살아 있거나 관측이 부족하면 같은 감독 의무와 부모 통보가 남는다. 실제 host 관측이 끝났지만 namespace만 달랐던 경우에는 기존 extinction 증명을 사용할 수 있도록 도달 불가능했던 조건도 교정했다. 불완전한 `/proc` 스캔은 여전히 증거가 아니다.

산출물 해시와 옛 tagged-residue 표식이 살아 있는 자손을 무시하게 만들던 세 경로를 제거했다. 이 표식은 과거 출력·잔류 진단으로 보존하지만 정리 증거로 소비하지 않는다. 실제 살아 있는 자식과 watcher가 남긴 residue, 자식 종료 뒤 정리, unknown 관측의 행 불변, 성공 결과 바이트 보존·반복 멱등성·재시도 권한 부재를 검사했다. 실제 4bc 마지막 test의 과거 namespace 관측 부족은 코드 수정만으로 해소됐다고 주장하지 않는다.

세 어댑터에 반복되던 “매 도구 호출 뒤 stage-heartbeat 실행” 지시를 제거했다. 런타임이 exact 로그의 Codex item, Claude tool use/result, OpenCode tool 상태에서 도구 ID·상태만 관측한다. 본문·도구 출력·mtime·반복 이벤트는 진행으로 세지 않고, 긴 텍스트 때문에 마지막 도구가 bounded tail에서 사라져도 이전 관측을 유지한다. phase는 현재 작업이므로 test→tool 왕복이 가능하다. terminal heartbeat가 성공 행을 만드는 registry/Fleet의 별도 writer도 제거했다.

정체 감시가 `dead-no-progress`를 쓰고 작업에 신호를 보내던 경로 역시 제거했다. quiet window는 기존 durable supervision 큐에 `no-progress` 통보를 남기며 실행 경계의 실제 시간 상한과 정리 권한을 대신하지 않는다. 살아 있는 작업의 행·PID를 유지하고, 같은 통보를 중복 발행하지 않으며, 진행 재개 뒤 정상 성공 commit을 그대로 받는 실프로세스 시험을 추가했다.

인지 부담은 지시문 압축만으로 처리하지 않았다. 일반 worker kernel에서 serial chain·ledger·native helper 절차를 분리해 기존 sub-session prompt 투영이 해당 워커에만 붙인다. 평상시 모델의 행동은 작업 수행·산출물·최종 판정으로 줄고, 알림 확인·진행 관측·정리 재시도는 런타임이 소유한다. worker kernel은 5595→3226 UTF-8 bytes이며 비용/토큰 절감의 실측값으로 해석하지 않는다. 기존 byte·directive 상한과 모델 설정은 올리지 않았다.

검증: contract 226(skip 1), join 116, registry 95, progress 34, supervision 14, reaper 11, worker bootstrap 14, sub-session runtime 4, 실제 3adapter prompt 2, Fleet state 42/dispatch 134 및 adapter Codex 58/Claude 46/OpenCode 30 PASS. 변경 전 “살아 있는 residue도 정리 완료”, “정체면 kill”, “모델 heartbeat 명령 필수”를 고정하던 테스트는 새 책임 계약에 맞춰 행 보존·신호 부재·실제 정리·도구 관측을 검사한다. 로그 `/tmp/cleanup-*.log`, `/tmp/runtime-observation-*.log`, `/tmp/runtime-final-*.log`, `/tmp/responsibility-final-*.log`. 중간 실패와 최종 교정은 별도 로그로 남겼으며 새 실제 모델 owner 왕복은 아직 이 결과에 포함하지 않는다.

전체 fallback 69 PASS(75.953초), review watchdog integration 17 / lifecycle 23 / Claude supervisor 69 PASS를 추가 확인했다. Codex supervisor 복구 픽스처는 원래 행 상태가 없는 Namespace만 넘겨 새 terminal cleanup 분기에서 실패했으므로 실제 open 행 상태를 명시했다. 전체 portable guards는 473 PASS/1 FAIL이었다. 유일 실패는 async session-end 시험과 foreground distill 시험이 동일한 fixture governor의 1개 슬롯을 경쟁한 것이며 stderr가 `distill class cap reached`를 기록했다. 운영 governor나 상한은 변경하지 않고 독립 시험의 상태 root를 분리했다. 이 중간 전체 실패와 후속 재검증을 구분한다.


## 4ef 실측에서 남은 부모 판정과 완료 확정 공백

전체 portable guards 최종 재실행은 474 PASS / 0 FAIL이다(`/tmp/runtime-responsibility-portable-guards-final.log`). 시험용 governor root 분리 전 473/1 결과도 위에 보존했다. main의 artifact_manifest 호환 수정 f6d05cbb를 병합한 고정 4efdddf0에서 두 실제 owner canary를 시작했으며, 진행 중인 이 소스는 변경하지 않는다.

Codex 부모 r3 `01a08f84-b216-7d11-b91a-daba2fa7a057`, route `rt-41a62718e41a4619`에서 OpenCode frame `att-951d96ca230d42599108ac8c2e839706`은 동일 parent_sid에도 poll-fallback이었다. 선택기는 `AGENT_DISPATCH_CALLER_HARNESS=codex`를 넘겼지만 OpenCode parser가 이를 무시하고 선택된 child의 OWNER_HARNESS를 부모로 읽었다. 별도로 parent_cwd를 Git primary checkout으로 바꾸는 추론이 남아 있었다. 부모 harness/session/cwd 기본값을 공통 resolver로 합쳤고 세 adapter의 별도 harness 판정, Codex 전용 cwd reader, 세 Git back-map을 제거했다. 명시 경로→실제 native session 경로→호출 cwd 순서이며 Git 관계로 부모의 위치를 만들어 내지 않는다. 실제 parser 및 실제 main의 carrier-before-spawn 검사에서 호출자와 선택된 자식 하네스가 다른 조합을 검사한다.

같은 실행의 Codex frame `att-ea0d96e78f3b4324bdb805ead2354c71`은 08:15:51.275Z PASS 종료 뒤 08:15:54.341Z 부모에게 readiness=ready/status=open/attention/complete-open으로 전달됐다. 부모의 호환 harvest 뒤 row/receipt가 success였다는 사실과 구분한다. 이는 프로세스 종료를 기다리는 join과 terminal writer의 실행 책임이 갈라진 경쟁이다. 공통 runtime join이 기존 exact writer로 종결을 확정하고 canonical row를 다시 읽은 뒤 ready를 반환하도록 연결했다. 쓰기 거부나 성공 반환 뒤 미확정은 terminal-commit-pending으로 유지하며 진단·기존 deadline notice와 런타임 재시도를 남긴다. 읽기 전용 진단 명령은 기존대로 원장을 쓰지 않는다. 확정된 결과는 재분류하지 않고 부정 handoff는 부정 결과로 종결한다.

OpenCode fresh parent `ses_f70775274ffeX48D6xfVUd5b1s`, route `rt-f061d15f2bd3622f`는 pJ에서 같은 4ef 소스를 사용한다. Codex frame `att-8a8a0ceb63714914aaca68a14cbb5eb6`와 OpenCode frame `att-de6dfc61dd2048aa99dd5c22d7a3d1c1`의 실제 등록/시작을 확인했다. OpenCode 부모의 bounded-wait는 공개된 fallback이며 자동 wake PASS로 바꾸지 않는다. 두 owner의 최종 왕복 및 위 후속 교정의 실측은 아직 완료 근거가 아니다.


후속 집중 검증은 부모 운송 12 / parent cwd 10 / join 120 / managed completion 14 / Claude supervisor 69 / Codex supervisor 34 / adapter Codex 58·Claude 46·OpenCode 30 PASS다. 생성 projection 20그룹, adaptation boundary, 기존 surface budget도 PASS다. 로그 `/tmp/parent-caller-*.log`, `/tmp/terminal-commit-*.log`, `/tmp/parent-terminal-*.log`. ready 직전 writer 실패·성공 반환 뒤 실제 미확정·이미 확정된 행·실제 부정 handoff를 각각 검사했고, 어댑터 main 시험은 parent-harness 수동 주입 없이 선택기가 내보내는 caller/owner 환경으로 carrier-before-spawn을 확인한다.

pJ는 native `chatcmpl-tool-9c9df05f68854c3b`의 실제 “맞음 / sum() 집계 (추천)” 답변(1789115071747ms), 정식 frame-review release, OpenCode d=1 owner `att-acbd1881d14644bdaac832f8a0904b0b` light/GLM 기동, 첫 native read의 이번 cycle agreed intent(1789115114093ms)를 확인했다. 승인 전 owner 거부 `child_spawned=0`도 보존했다. `/tmp/f-r3/question-release-owner-read.json`과 native export가 원문 증거다. 아직 test/report 및 owner의 최종 수신·종결 합격은 아니다.


호출자 신원의 수명도 한 번의 admission으로 제한했다. 실제 worker 진입 시 공통 `worker_runtime_identity`가 current/caller를 실행 하네스로 바꾸므로, 이후 d=2 자식이 depth-0 조부모의 하네스를 자기 부모로 읽지 않는다. 세 adapter의 실제 main에서 이 전환을 확인하고 3×3×3 부모·owner·후속 자식 조합을 검사한다. 부모 선택의 새 거부 규칙을 추가하지 않았다.


## OpenCode 자동 재개 약속과 실제 책임자의 불일치

4ef의 OpenCode owner `att-acbd1881d14644bdaac832f8a0904b0b`는 단일 `opencode run`이었다. d2 test `att-df61b920346298c56d5ca9c0be8c456bba3da8410a6c8e06`의 실제 start receipt가 `parent-runtime-supervised` / `carrier-session-supervisor` / `end-turn`을 출력했지만 이를 수확하고 같은 owner를 재개할 프로세스는 없었다. owner는 1789115440373ms에 “report follows on wake” BLOCKED handoff를 남겼고 report는 시작되지 않았다. test 산출물 PASS와 owner/route 성공은 구분한다. 두 시도의 quiescence와 원문은 canonical cycle의 `artifacts/dev_logs/false-carrier-canary/` 및 `/tmp/f-r3/false-carrier-observation.json`에 보존했다. 원 route/cycle은 미완료 상태이며 별도 성공으로 닫지 않았다.

등록 신분을 감독 능력으로 취급하던 공통 분기를 제거했다. 세 어댑터가 exact parent의 동일 nonce lease가 실제 보유 중인지 확인하며, 없으면 실행 가능한 bounded-wait receipt를 준다. OpenCode standard+ owner는 기존 CLI 감독 루프에 연결한다. 새 native driver는 관측한 실제 `sessionID`를 attempt에 결속하고 `opencode run --session`으로 동일 세션을 재개하는 실행 수단만 담당한다. 종료 판단·join·receipt ACK·재시도·실패 정리는 새 어댑터에 복제하지 않았다. 기존 Claude supervisor 파일명은 호환 경로로 유지한다. 일반 owner continuation을 추가한 것이며 OpenCode serial-chain owner나 deterministic stage advance를 지원한다고 확대하지 않는다.

모델 없는 실제 프로세스 시험에서 동일 native session 재개, 원 세션 바인딩 보존, 종료 직전 stop 누락의 성공 금지, capacity 오류 보존, timeout의 native process reap, 세 하네스의 실제 lease 획득/반납, 실제 shared controller의 child join→동일 세션 재개→완료 commit/terminal read를 검사했다. 기존 테스트 세 건이 '자식 환경변수만으로 자동 wake를 약속한다'는 잘못된 가정을 고정했으므로 실제 감독 증거와 fallback을 검사하도록 바꿨다. native controller 9 / parent 13 / Claude supervisor 69 / Codex supervisor 34 / contract 226(skip1) / terminal 33 / classifier 10 / join 120 / adapter OpenCode30·Claude46·Codex58 PASS, generated-projections·boundary·기존 surface budget PASS다. 로그는 `/tmp/opencode-*-tests.log`, `/tmp/opencode-*-final.log`, `/tmp/opencode-controller-{generated,boundary,surface}.log`에 있다. 새로운 실제 OpenCode owner 왕복은 아직 미검증이다.

4ef Codex r3는 owner `att-47e83888b32b42348b2b0a957a8dc0f2`의 실제 success 수신 08:29:36.764Z, 다섯 exact row quiescent, workflow COMPLETE 및 route close 08:30:41.309Z를 확인했다. producer는 root33의 정상 상대 primary 교정 CLI로 08:41:59Z completed/sealed, artifacts 14, manifest `sha256:f828f8ff93142c2bfe758ba95c719ae9f1499798331c803beb8c54eb8281a888`이며 cycle/index/manifest가 일치한다. 부모의 진행 중 finalizer TERM 및 45초 재실행 만료, root33의 절대 primary 입력 exit65 뒤 상대 경로 교정 exit0는 별도 실패 이력이다. 초기 frame attention/fallback도 그대로 보존한다. 무개입 완주나 전체 parity PASS를 주장하지 않는다.


81112e11의 실제 재검증은 사용량 steer로 보류했다. OpenCode Go headroom 3%를 현재 공통 capacity reader로 확인했으며, `usage-check.sh`의 `ok`는 잔여량이 아니라 최근 거절 marker가 없다는 뜻이므로 이를 여유로 해석하지 않았다. pJ r4는 route `rt-47f0fcedce751998`만 발급하고 bind/begin/child 이전에 중단했으며, root33의 새 Luna parent `01a08fb3-ce47-7832-8635-d1b73ac21679`도 OpenCode review 기동 전에 보류했다. 두 검증 모두 신규 attempt 0건을 canonical jobs에서 확인했다. 준비 부모 자체의 모델 사용은 있었으며 '모델 사용 0'이라고 주장하지 않는다. OpenCode 실제 owner 재개와 수정된 cross-harness 부모 success 수신은 여전히 남은 합격선이다. main/release/install은 보류한다.


## OpenAI Luna로 실제 검증 재개

사용자의 명시적인 provider 변경 지시에 따라 사용자 소유 OpenCode 모델 매핑에서 light·mini를 `openai/gpt-5.6-luna`로 변경했다. balanced는 기존 규칙대로 light에 투영되며 실제 resolver도 같은 모델을 반환했다. 나머지 프로파일과 credentials, 소스 기본값은 변경하지 않았다. 원 설정은 `/tmp/opencode-models-before-openai-luna-20260911.conf`, 변경 hash와 확인 결과는 `/tmp/opencode-openai-luna-config-observation.json`에 보존했다. Go 잔여량을 OpenAI 잔여량으로 해석하지 않는다. 두 실행 하네스가 같은 모델/provider를 사용하는 검증이며 모델 간 독립성을 주장하지 않는다.

소스 81112e11을 고정하고 pJ fresh OpenCode 부모 `ses_f6fd4341effexujfUk7zuotRfR`에서 r5를 시작했다. route `rt-d3a5acca2fb8820e`의 Codex frame `att-b6fba78ed2ab490ca19320cd1b672349`와 OpenCode frame `att-dd6c697a6faa471590e07ed8286fc414`는 모두 실제 light 모델로 기동했다. 후자는 OpenAI Luna의 실제 PASS와 process-group drained를 확인했다. 부모는 공개된 bounded-wait 경로를 사용한다. 이 시점의 frame 기동/부분 완료는 d1 owner의 같은 세션 재개나 전체 완료를 뜻하지 않는다.

root33은 기존 Luna 부모 [61]을 재사용한다. 보고서용 compose에 direct shape와 standard intensity를 함께 전달해 기동 전 exit64를 받은 입력 오류는 `/tmp/opencode-review-receipt-r1-openai-preparation-error.json`에 보존했다. 보고서 route의 intensity를 direct로 교정하고, 별개인 실제 route-free review는 standard/light로 유지해 한 건의 실제 OpenCode→Codex 부모 완료 전달을 이어간다. 이 준비 오류 시점의 OpenCode attempt는 0건이다.


교정한 실제 리뷰 `att-cff2ba1e36684d309ddd1be5a0e7b213`는 보고서 쓰기(check-write allow, sum=3/exit0) 뒤 11:29:13.806Z에 동일 부모 `01a08fb3-ce47-7832-8635-d1b73ac21679`로 success/registry-closed/advance-completed를 자동 전달했다. 수동 harvest는 완료 조건이 아니다. exact attempt log와 결속한 native session `ses_f6fc7ffe2ffevcVuG3luTA65il`의 assistant 5개에서 provider=openai/model=gpt-5.6-luna를 확인했다. 근거는 `2026-09-11_opencode-review-receipt-r1/artifacts/dev_logs/root33-corrected-receipt-observation.json` 및 `root33-digest-model-clarification.json`이다.

`review_output_digest`는 attempt/cycle/producer/output 위치의 identity tuple hash이다. 실제 tuple 재계산 `sha256:18bb0e6345e2a160053e59afcf096dbfc96477e8df2c84d6055cb227325b436f`는 원장과 일치한다. 보고서 내용은 별도 관측 snapshot(3038bytes, SHA256 `434edc76e1c9dd4c48f15595bede0dcd83c9f56363b885f6d75bdbe7d4eb2db0`)이며, identity digest를 불변 terminal content hash라고 해석하지 않는다. 이전 `att-1d29ae31c5054f17bf13bc00be900673`에 부모 운용 brief를 잘못 전달한 입력 오류, root33의 exact watchdog SIGTERM, 정리 증명과 11:24:01.785Z 실패 알림은 보존한다. 교정 리뷰의 운송 성공은 그 실패나 전체 owner parity를 덮지 않는다. pJ r5는 두 frame PASS 및 승인 전 owner 거부 뒤 원 native 질문(`call_fmnMlz8l0Kyu1OWwVKwFohyK`)의 실제 답변을 기다리며 source811을 고정한다.


리뷰 검증 route `rt-5d4f828bf0e265db`는 inline complete→close를 정상 완료했고, 상대 primary를 사용한 단일 finalize가 11:41:24Z에 cycle `cyc_b97be936e128d2b518909ba6349d4e76`을 completed/sealed로 마쳤다. manifest `sha256:c7fd43dd25bce503081126f61d4f851586642f25c8d5c39ed602141310735698`와 4개 산출물의 bytes/digest가 일치한다. 시험 부모 [61]도 정상 /exit 뒤 socket 세 개 소멸과 shell 복귀를 확인했다. 최종 보고서는 쓰기·동일 부모 success·실제 native model만 검증 범위로 삼는다. 근거 `/tmp/opencode-review-receipt-r1-closure-observation.json`.

## OpenCode d1 owner의 실제 재개와 마감

고정 81112e11, route `rt-d3a5acca2fb8820e`, cycle `cyc_7428fb551d8861996ff714b2ef26f09d`에서 실제 부모 `ses_f6fd4341effexujfUk7zuotRfR`가 원 native 질문의 “둘 다 기록” 답변을 받았다. 11:36:14.309249Z release 뒤 OpenCode owner `att-946200843af74f628a186747bcd7e0a1`가 원 intent를 읽었다. owner의 첫 native 실행과 test 완료 뒤 재개는 모두 같은 `ses_f6fbed18bffeyREdrzA5YRlW7T`이며 실제 provider/model은 OpenAI/gpt-5.6-luna다. 공통 감독자가 test 결과를 확정하고 동일 세션을 한 번 재개해 report를 실행했다. report 종료 후에는 이미 선언된 terminal node가 충족되어 공통 terminal fast path로 owner를 닫았다. 세 번째 모델 턴의 결과라고 주장하지 않는다.

두 frame·owner·test·report의 다섯 exact attempt는 모두 done/pass와 governed-process-group-drained를 확인했다. workflow COMPLETE→route close→단일 producer finalize가 끝나 cycle은 completed/sealed, 산출물 11개, manifest `sha256:21462a5185e476de44416311626ea563b7bc74ece1a1b8741af232984fa1f594`다. manifest의 11개 bytes/digest를 실제 파일과 대조했다. pJ 부모는 관측 종료 후 정상 /exit했고 12:08:05.744031Z shell 복귀·live agent 부재를 확인했다. pK 리뷰 부모와 pJ owner 부모의 소스 freeze를 모두 해제했다.

OpenCode depth-0는 공개된 bounded-wait를 사용했고 owner 대기는 한 번 호출해 exit0으로 끝났다. 이것을 자동 wake라고 부르지 않는다. 앞선 Codex frame의 bounded-wait는 PASS와 프로세스 종료 후에도 terminal-unclosed/exit3을 반환했다. 또한 실제 owner는 합의한 intent를 읽었지만 생성된 test prompt에는 그 내용이 없었다. test는 `git diff HEAD~1`을 추정해 넓은 소스 검증을 했고 report는 사용자가 선택한 Python alias 실패 기록을 누락했다. 따라서 실행·동일 세션 재개·종결은 PASS지만 작업 범위 준수는 FAIL이다. 기존 marker와 test/report 산출물은 고치지 않았다. 봉인된 `artifacts/dev_logs/transport-verification.md`가 이 범위를 명시한다.

근거는 `2026-09-11_frame-opencode-owner-light-r5` cycle의 봉인된 산출물과 `/tmp/f-r5/{parent-native-final-export,owner-native-final-export,bounded-wait-observation,root09-closure-observation,parent-shutdown-observation}.json`이다. 관측 종료 후 lease probe가 이미 풀린 결과는 살아 있던 동안의 lease 보유 증거라고 소급하지 않는다.

## 마지막 실측에서 제거한 입력·완료 판정의 이중 책임

합의 내용의 전달을 모델의 수동 prompt 복사에 맡기던 경로를 공통 `worker_bootstrap.released_task_prompt`로 바꿨다. 검증된 route identity로 기존 frame-review journal의 해제 기록과 실제 답변을 읽고, 기존 intent renderer로 owner·stage·review 입력에 넣는다. 계획 노드가 없어도 같은 범위와 선택이 전달되고 명시적인 stage assignment는 그대로 유지한다. 새 gate나 필수 CLI 입력은 없다. frame 두 다리는 독립 입력을 유지하며, 다른 cycle의 환경변수·최근 디렉터리·Git 이력으로 작업을 추정하지 않는다. 기록된 입력이 손상됐으면 정확한 복구 경로를 보고한다. QA preset의 무조건적인 `git diff HEAD~1` 기본 작업도 제거했다.

실제 r5 test prompt는 `interpreter-alias` 결정이 없었다. 같은 원 journal과 답변을 세 실제 어댑터 renderer에 재생한 결과 모두 사용자 선택 “최종 test/report에 python alias 실패와 python3 성공을 함께 남긴다”와 확정된 작업을 포함했다. `/tmp/f-r5/corrected-task-context/observation.json`에 원 prompt SHA 및 교정 출력의 대조를 남겼다. 이는 모델 없는 실제 입력 재생이며 새 owner 실측은 아니다. 기존 journal의 답변·interview 참조 계약을 사용하며 새 불변 내용 snapshot을 도입했다고 주장하지 않는다.

수동 대기의 등록 시도 성공 note 목록과 terminal-unclosed 실패 판정을 삭제했다. `dispatch-attempt-ready`와 `dispatch-wait`는 자동 감독자가 쓰는 공통 join의 terminal commit·정리 복구를 호출하고, 실제 행을 다시 읽은 뒤 공통 delivery classification으로 성공을 소비한다. watcher/settlement helper가 성공을 반환해도 행이 닫히지 않았으면 계속 pending이다. 관측 부족은 정리 의무와 진단을 남기고, 확정 PASS와 충돌한 관측은 기존 결과 bytes를 보존하면서 성공 소비를 보류한다. 호환 supervisor proof도 공통 reader에서 실제 d1 owner에만 적용하므로 stage가 supervisor note를 주장해 우회하지 못한다. 일반 읽기 CLI는 원장을 바꾸지 않으며 운영 bounded wait만 기존 writer를 사용한다. exit0이면 추가 harvest 의무가 없다.

최종 회귀: 실제 3adapter prompt matrix 6, bootstrap 14, frame interview 31, adapter Codex 58/Claude 46/OpenCode 30, readiness 14, join 120, contract 226(skip1), shell bounded-wait conformance 모두 PASS. 실제 r5 완료 owner 행도 read-only readiness에서 ready/registry-closed/advance-completed였다. 생성 projection 20개·적응 경계·기존 surface budget 및 diff 공백 검사를 통과했다. OpenCode fresh-registry preview에 release 기록이 없는 경우를 뒤늦게 발견해 입력 주입 없이 기존 preview를 유지하도록 교정했으며 최초 실패 로그도 남겼다. 공통 판정으로 옮기면서 드러난 잘못된 legacy supervisor proof와 미봉인 slice fixture도 실제 writer 계약으로 정정했다. 로그 `/tmp/released-task-*-final.log`, `/tmp/released-task-*-tests.log`, `/tmp/shared-wait-*-final.log`, `/tmp/shared-wait-contract-tests.log`, `/tmp/context-wait-{generation,boundary}-final.log`.

남은 지원 경계는 OpenCode depth-0의 명시적 bounded polling과 OpenCode serial-chain owner/deterministic advance 미지원이다. 두 모델 실행 하네스에 같은 OpenAI Luna를 쓴 결과를 모델 간 독립성으로 표현하지 않는다. 과거 4bc 시도의 관측 불가 자손이 자동 정리됐다는 운영 주장은 하지 않는다. 새로운 정상·지연·중복 재시도·관측 불가·감독자 종료 계약의 근거를 각각 구분했고, release/install은 별도 사용자 확인 전 보류한다.

최종 통합 확인: 3b9ea255의 깨끗한 통합 트리에서 prompt 6 / readiness 14 / join 120을 다시 통과했다(`/tmp/integration-3b9-{prompt,ready,join}.log`). PR #17의 수정 브랜치를 푸시했고 로컬 main도 같은 소스로 fast-forward했다. 원격 main은 f6d05cbb에 유지했으며, primary checkout의 기존 미추적 `dist/`는 보존했다. 이 기록 이후 문서만 고친 커밋은 위 소스 검증의 의미를 바꾸지 않는다.

## 2026-09-12 — 사용자 완료 기준으로 검증 재개

“배포만 남았다”는 판정을 철회하고, 간단한 작업 지시에서 합의한 결과와 마감까지 도달하는 실제 사용 흐름을 다시 검증한다. 기존 부모에게 전달했던 긴 명령 조립 지침은 성공 근거의 한계였으므로 재사용하지 않는다. 신규 부모에는 작업·하네스/모델 선택·검증 소스 root만 준다. source 수리는 계속 direct/inline이며 새 Claude 모델 호출과 전역 설치는 하지 않는다.

진입부 재점검에서 intensity→owner 비교 외에 `resolve_profile_demand`가 별도의 등급 하한을 강제하고 명시 모델에도 요구서 JSON을 강제한 것을 확인했다. 명시한 알려진 모델 프로파일이 추천 행렬보다 우선하도록 공통 resolver를 정정했다. `compose --profile light`가 모든 모델 노드와 owner의 기존 explicit selection map을 작성하고, 노드별 명시 선택은 그보다 우선한다. 새 policy 객체나 대체 발급 경로를 만들지 않았다. 선택이 없으면 기존 추천·기본값을 쓰며, 알려지지 않은 모델·변조·실제 runtime 지원·top의 기존 실행 범위 검사는 남는다. 기존 허용 선택의 영수증 표현과 이미 발급된 route bytes는 유지한다.

core/WORKFLOW·CONVENTIONS·capability·owner reference에 남은 “standard owner는 deep”, 생략된 plan 산출물 요구, 수동 intent 복사 지시도 정정했다. 초기 model/demand suite 실패는 과거 하한 거부를 기대하던 시험과 명시 선택이 없는 ad-hoc 구분 회귀였다. 전자는 새로운 사용자 요구에 맞춰 실제 선택 및 seal 검증으로 바꾸고, 후자는 암묵적 기본값을 사용자 명시 선택으로 잘못 기록하지 않도록 고쳤다. model 28 / demand 25 / route 387 / compose CLI 17 / adapter Codex58·Claude46·OpenCode30 및 생성20·경계·기존 surface 예산 PASS. 실제 CLI의 `--profile light`가 별도 demand 파일 없이 frame/test/report 및 owner에 전달되는 경로도 포함한다. 로그 `/tmp/proof-explicit-profile-*.log`, `/tmp/proof-surface-*.log`. 실측 합격은 아직 이 소스 검사에 포함하지 않는다.
