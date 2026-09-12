# Fleet의 현재 완료 계약 표시 점검

2026-09-13 · 기준 소스 `da410a0a` · 직접 점검·수정, 추가 모델 기동 없음.

## 확인한 증상과 변경

- 사용자가 보고한 세션 아래 legacy stage 줄은 Fleet 재시작 후 사라졌다. 제거 변경 `b113e35b`는 기준 main과 v2.140.0에 이미 포함되어 있었다. 현재 소스로 수집한 실제 세션 스냅샷에서도 해당 줄이 없었다. 재시작 전 프로세스의 코드·캐시 상태까지 확보한 것은 아니므로 별도 소스 회귀로 단정하지 않는다.
- 병렬 묶음은 두 leg 중 하나가 `done`, 나머지가 `pending`이면 전체를 `done`으로 표시했다. 기존 F-41d의 완료 우선 규칙과 그 기대값을 정정했다. 이제 모든 선언된 leg가 `done`이어야 묶음도 `done`이다. 실제 실행 중인 leg, 실패, 복구, 확인 의무의 표시 우선순위는 유지한다. 선언된 leg가 실행되지 않았다고 해서 완료 또는 실행 중을 추론하지 않는다.
- runtime이 미해결 terminal 충돌을 기록해도 Fleet은 marker를 근거로 완료를 표시했다. 수집기가 공통 `dispatch_attempt_policy.terminal_conflict_pending`의 판정을 `attention_reason`으로 전달하고, route view는 비활성 충돌 행을 `attention`으로 표시한다. breadcrumb에는 `!`, process view에는 `확인 필요`가 보인다. 해소 기록이 도착하면 같은 attempt의 완료 표시가 회복된다. 판독할 수 없는 충돌 근거도 정상 완료로 꾸미지 않는다.

기존 marker와 `gate_passed`는 과거의 검증된 근거로 보존한다. 현재 route의 상태·완료 개수와 구분한다. Fleet은 충돌을 해소하거나 실행·retry를 허가하지 않으며 운영 원장에 쓰지 않는다. 프로세스가 실제로 실행 중이면 기존 active 표시가 우선한다. 완료 의무와 별개인 프로세스 liveness를 바꾸지 않는다.

## 정리한 중복

- Fleet에 terminal 충돌의 별도 승인·해소 규칙을 만들지 않고 이미 쓰는 공통 정책을 소비한다.
- `_node_state`에 세 번 복제되어 있던 현재 attempt 선택 순서를 한 지역 helper로 모았다. 최신 registry 해소 결과가 과거 job 객체의 충돌 표시를 다시 살리지 않는다.
- 부분 완료를 전체 완료로 바꾸던 묶음 표시 규칙을 제거했다. 새로운 workflow 상태나 복구 큐는 추가하지 않았다. `attention`은 Fleet의 표시 상태이고 `attention_reason`은 JSON에 추가된 관측 필드다.

## 검증과 한계

- 관련 13개 검사 묶음 PASS: route, breadcrumb, gate, process view, continuation, work projection, legacy session detail, owner fallback, parity.
- Fleet 전체 99개 묶음 실행: PASS 90 / 신규 fixture·미러 오류 2 / KNOWN-FAIL 6 / XPASS 1. 신규 오류는 fixture의 필수 `key` 누락과 새 테스트의 생성 counterpart 누락이었다. 두 오류를 수정한 뒤 해당 2개 묶음 모두 PASS. 전체 99개를 다시 실행하여 전부 통과했다고 주장하지 않는다.
- 신규 7개 테스트는 세 harness의 실제 registry parser→공통 충돌 판정→route view, 동일 attempt 해소, 판독 불가, 오래된 job 객체, 2~4개 leg의 완료 조합, 가로·세로 표시를 확인한다. 운영 행이나 실제 완료 marker를 수정하지 않는다.
- 생성 projection 20개 그룹 PASS. 새 테스트의 Claude counterpart는 기존 generator가 만든 symlink이다.
- 실제 `fleet --once` 스냅샷에서 원 세션과 관계 행은 유지되고 legacy stage 줄은 없음을 확인했다. 미해결 충돌·부분 완료의 새 동작은 격리 입력으로 검증했으며 실제 운영 충돌을 새로 유발하지 않았다.
- 전체 검사 동안 별도 실제 Fleet 스냅샷도 실행했다. runner는 live-state 영역의 새 bytecode 56개를 `UNATTRIBUTED`로 기록했다. 검사 자체의 무쓰기 증명으로 취급하지 않았으며 운영 jobs/marker를 편집하지 않았다.
- 기존 KNOWN-FAIL: `test_f40_attempt_suppression`, `test_f83_dead_terminal_owner`, `test_f87_runtime_projection`, `test_f87_session_handle`, `test_f88_gpu_process_links`, `test_v20_dispatch_contract`. 기존 실패 목록의 `test_token_budget` 1건은 XPASS였다. 이 목록이나 기존 기대값을 바꾸어 전체 성공으로 만들지 않았다.

격리 검증 명령:

```bash
python3 tools/run-tests.py --select tools/fleet/tests/test_current_contract.py --select tools/fleet/tests/test_mirror_parity.py --isolation isolated --jobs 2 --timeout 120 --retries 0
python3 tools/generate.py --check
```

이번 점검은 현재 routing/completion 계약과 Fleet 표시의 연결에 한정한다. Fleet의 모든 수집기·성능·외부 API를 전수 감사했다는 뜻은 아니다. 설치 후 새 Fleet 프로세스에서 변경이 반영되며, 실행 중인 Fleet 프로세스의 Python 코드는 자동으로 교체되지 않는다.

## 배포·설치 확인

제품 변경 `24e564c0`을 main에 반영하고 [v2.140.1](https://github.com/dmlguq456/hearting/releases/tag/v2.140.1)을 게시했다. [Release 실행](https://github.com/dmlguq456/hearting/actions/runs/34715727411)은 설치·업데이트 검사와 게시 후 smoke까지 성공했다. Claude/Codex/OpenCode 로컬 설치 후 strict doctor는 모두 `fresh`, verify/update의 drift는 0이었다. 설치본의 새 7개 테스트도 PASS였으며, 변경된 Fleet 소스 4개가 커밋 bytes와 일치하고 관측한 설정·인증 파일 11개가 보존됨을 확인했다. 설치본 `fleet --once`에서도 원 세션의 legacy stage 줄은 없었다. 열린 Fleet 프로세스는 재시작해야 새 표시 코드가 적용된다.
