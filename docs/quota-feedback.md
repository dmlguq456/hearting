# Quota 거절 증거와 재선택 — 2026-09-13

Claude의 실제 주간 거절이 `dead-launch-exit-1`로 남아 다음 frame 선택에서 다시 Claude가 선택됐다. 원인은 사용량 조회가 특정 `dead-…limit` 문자열만 읽은 데 있다. 초기 종료 감시자는 마지막 세 줄의 짧은 문장만 검사하므로 긴 native JSON 거절을 놓쳤다. 별도 용량 reader는 세션 게이지 중 파일 수정 시각이 가장 최근인 주간 사용률 39%를 골라 headroom 61%를 반환했다. 같은 reset을 가진 다른 파일은 99%를 담았다. 파일 갱신 시각은 서버 관측 시각이나 계정 동일성의 증거가 아니다. 61%를 현재 계정의 실제 잔여량으로 확정하지 않는다.

## 공통 책임

- launcher는 실제 자식 환경의 구독 계정·조직·runtime home 또는 명시 OAuth token 선택을 해시로 기록한다. 식별 원문과 토큰을 원장에 싣지 않는다. 계정 결속을 산출할 수 없거나 API/provider 경로이면 주간 구독 증거를 임의로 결속하지 않는다.
- `dispatch_capacity_evidence.py`가 정확한 native session의 실패와 structured rejected window를 결합한다. reset, model scope, 계정 범위가 맞을 때만 현재 선택에 적용한다. 일반 429·인증 실패·허용 이벤트·이전 턴·과도한 reset은 주간 quota 증거가 아니다.
- `usage-check.sh`의 독립 awk 판정을 없애고 같은 reader의 얇은 CLI로 바꿨다. 기존 문자형 한도도 이 reader가 처리한다. scoped native 증거를 손실된 문자 marker가 다시 전 계정/전 모델 제한으로 넓힐 수 없다. 옛 시각형 reset은 관측일 기준으로 해석해 매일 제한이 부활하지 않는다.
- owner 선택, d=2 fallback, 병렬 배치와 용량 점수가 같은 증거를 소비한다. 품질 범위와 봉인된 후보 집합은 유지한다. 주간 전체 quota에 모델만 바꾼 같은 하네스 재시도를 쓰지 않는다. reset 뒤에는 같은 route를 재개할 수 있다.
- frame 완료 게이트도 이미 확인된 계정 결속 quota를 소비한다. 다른 route에서 얻은 증거 때문에 회피한 후보를 다시 실패시킬 필요가 없다. 두 frame에 적용되는 모델 범위, 원 실패의 정리 완료, 충돌 부재, 현 route의 추가 실행 부재를 확인하고 기존 degradation 원장에 증거 attempt를 기록한다. 실제 두 frame 결과와 사용자 승인은 여전히 필요하다.
- 실행 정리와 retry 허가는 기존 공통 attempt controller/owner가 소유한다. reader가 원장을 닫거나 모델을 띄우지 않는다. harvest는 runtime quota/auth 실패를 깨진 산출물 형식으로 표시하지 않는다.

## 과거 사건과 지원 범위

원 사건에는 당시 계정 바인딩이 없다. 보고자도 계정 불변을 증명하지 못한다고 확인했으므로 `unbound` 진단으로만 보존한다. 현재 계정으로 소급 결속하는 수신증·운영 원장/marker 변경·재분사는 없다. 기존 세션의 v2.140.0 pin과 설치 current는 별개다. 신규 실행에 적용되는 수정이 과거 미결속 실패를 자동 적용한다고 주장하지 않는다.

Claude의 native quota 이벤트는 [공식 SDK의 RateLimitEvent/RateLimitInfo](https://code.claude.com/docs/en/agent-sdk/python#ratelimitinfo)와 실제 CLI stream을 대조했다. [Codex의 JSONL 실행 계약](https://developers.openai.com/codex/noninteractive)을 확인했다. 세 어댑터는 같은 책임 경로를 사용하되, Codex/OpenCode의 일반 429에 Claude의 구독 window를 붙이지 않는다. 새 계정·인증 설정은 만들거나 변경하지 않는다.

[기존 supervised capacity-retry 공백](capacity-failover-gap.md)은 별도다. 이번 작업은 native 조기 종료 증거가 다음 선택과 frame 완료까지 전달되지 않던 경로를 연결한다. supervised join이 모델 대체 retry를 호출하도록 바꾼 것은 아니다.

## 검증 기록

집중 검증은 실제 CLI 선택기·병렬 배치·d=2 fallback·frame gate·native harvest를 연결한다. 계정/조직/인증 경로 변경, 모델별 범위, reset 경과, 일반 429, 다른 session, 성공/허용 이벤트, 손실된 문자 marker의 재확대, 정리 미확인과 현 route 실행 중을 포함한다. 세 adapter 실제 main의 프로세스 기동/실패 정리 경로에서 metadata 전달도 확인한다. 공급자 quota를 추가로 소모하는 모델 canary는 실행하지 않았다.

초기 전체 검사에서 발견한 두 테스트 문제는 수정 전 main에서도 재현됐다. usage 테스트가 canonical jobs 대신 구 설치 경로를 기대하던 fixture를 교정했고 해당 baseline 행을 제거했다. capacity watchdog 테스트는 원장에 없는 캐시 문구만으로 retry를 기대하던 fixture였으므로 정확한 종료 행을 포함하고, 행이 없는 경우 거부되는 검증도 추가했다. 운영 판정을 약하게 바꾸지 않았다.

최종 isolated runner는 11개 suite 중 PASS 10, 기존 KNOWN-FAIL 1, 새 실패 0으로 종료했다. 기존 실패는 adapter suite의 Codex App Server 가용성 preview case이며 신규 세 adapter 실제 main 검증과 구분한다. 새 quota 집중 12건 및 frame gate 4건도 통과했다. 생성 20그룹과 적응 경계 검사는 통과했다. 릴리즈·설치 결과는 완료 후 아래에 기록한다. 로컬 증거는 `/tmp/quota-feedback*.tsv`, `/tmp/quota-*-final.log`, `/tmp/quota-current-inspect.json`에 보존하며 private native 로그는 공개 첨부하지 않는다.
