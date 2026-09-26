# 캠페인 닫기와 재개

캠페인 닫기는 종결 조건이 충족됐다고 판단한 에이전트가 남기는 명시적 event다. 사이클이 모두 봉인됐다는 사실만으로 자동 종료하지 않는다. 닫힌 뒤에도 같은 작업을 이어갈 수 있으며, 다시 시작하면 새 event를 추가해 열린 상태로 되돌린다.

## 운영 순서

`campaign-status`로 목표, criterion, 사이클 disposition, 닫기 가능 여부와 실행 명령을 확인한다. 기본 criterion인 `every cycle sealed with a manifest`를 쓰는 캠페인은 충족된 종결 조건을 한 문장으로 `--reason`에 적는다.

```sh
python3 "$AGENT_HOME/utilities/artifact_producer.py" campaign-status \
  --artifact-root /absolute/project/.agent_reports \
  --campaign /absolute/project/.agent_reports/campaigns/example/campaign.json

python3 "$AGENT_HOME/utilities/artifact_producer.py" campaign-close \
  --artifact-root /absolute/project/.agent_reports \
  --campaign /absolute/project/.agent_reports/campaigns/example/campaign.json \
  --reason "the agreed release output is complete"
```

`--campaign`에는 ID 또는 `campaign.json` 경로를 준다. 의미 있는 criterion이 이미 기록됐다면 `--reason`은 생략할 수 있고, 그 criterion 문장이 event에 기록된다. 이유는 종결 기록이며 별도의 확인 절차가 아니다. 잘못 닫은 캠페인은 다음 명령으로 다시 연다.

```sh
python3 "$AGENT_HOME/utilities/artifact_producer.py" campaign-reopen \
  --artifact-root /absolute/project/.agent_reports \
  --campaign /absolute/project/.agent_reports/campaigns/example/campaign.json \
  --reason "the work needs another cycle"
```

닫힌 캠페인의 key, ID 또는 parent cycle을 선택해 `begin`하면 자동으로 재개하고 새 사이클을 같은 campaign에 붙인다. `compose`는 닫힌 key에 `(닫힌 캠페인 재개)`를 표시한다. 같은 key로 닫힌 캠페인이 여럿이면 `campaign-key-reopen-ambiguous`로 거부되며, `abandoned`와 `superseded`는 재개하지 않는다.

## Event와 복구

새 기록은 `campaigns/<locator>/campaign.events/<NNNNNN>.json`에 순번대로 추가한다. 기존 `campaign.satisfied.json`은 sequence 1의 읽기 전용 기록으로 취급한다. 닫기와 재개는 D-11 event envelope를 사용하고, 재개 event는 직전 종결 event ID를 가리킨다. 기록 파일 발행이 commit point이며, 이전 event를 수정하거나 지우지 않는다.

한 검증 fold가 event 순서로 상태를 계산한다. 종결 뒤 재개되면 현재 상태는 active이고, 마지막 event가 종결이면 satisfied다. `campaign.json`의 `state`, `satisfied_on`, `satisfaction_event_id`만 fold 결과를 projection한다. 읽기는 projection 지연을 허용하고, `campaign-recover`는 이미 commit된 event로 상태와 색인을 복구한다.

일반 metadata 수정은 닫힌 campaign에서도 가능하며 상태 필드는 fold 결과를 보존해야 한다. 상태 event가 없는데 닫힌 상태를 기록하거나, event stream의 번호·전이·root·campaign이 맞지 않으면 typed 오류로 거부한다. snapshot 검증은 membership, sealed manifest, index와 artifact bytes, 열린 route, drift를 계속 확인한다. `abandoned` 사이클은 성공으로 바뀌지 않으며 `residual=0`은 종료 기준이 아니다.

일반적인 오류와 다음 행동:

- `campaign-close-reason-required`: 기본 criterion의 실제 종결 조건을 `--reason`으로 기록한다.
- `campaign-cycle-provisional-active` / `campaign-cycle-not-sealed`: 해당 route와 cycle의 봉인 상태를 확인한다.
- `campaign-membership-drift` / `campaign-index-mismatch` / `campaign-artifact-mismatch`: 현재 기록과 immutable evidence의 차이를 조사한다.
- `campaign-event-sequence-invalid` / `campaign-event-invalid` / `campaign-event-transition-invalid`: stream을 직접 고치지 말고 원본과 복구 결과를 보존한다.
- `campaign-close-committed-recovery-required` 또는 `campaign-reopen-committed-recovery-required`: 출력된 `campaign-recover` 명령을 실행한다.

## 릴리스 호환성

새 reader는 v1 파일과 v2 stream을 모두 읽는다. 옛 reader는 v2 event를 알지 못한다. 그러므로 해당 artifact root의 writer와 reader를 새 릴리스로 옮긴 뒤 v2 닫기와 재개를 운영한다. 새 릴리스가 v1 campaign을 재개하면 projection은 v1 snapshot과 달라지므로 옛 reader는 `campaign-projection-conflict`를 낼 수 있다. 새 릴리스가 닫은 campaign은 옛 reader에서 닫힌 것으로 보여 다음 `begin`이 거부된다.

## 과거 관측

2026-09-13의 BC 관측에서는 16개 cycle이 sealed였고 completed 14개, abandoned 2개로 표시됐다. 그 당시 설치본은 명시 확인을 요구했으며, 이번 정비 요청을 해당 campaign의 완료로 처리하지 않았다. 이는 과거 릴리스 동작의 기록이며 현재 운영 절차를 정의하지 않는다.
