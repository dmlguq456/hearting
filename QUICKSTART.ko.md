# 빠른 시작

유지보수 기준인 [QUICKSTART.md](QUICKSTART.md)의 한국어 번역입니다. 두 문서가
다르면 영어판이 맞습니다. 명령, 경로, 설정 키는 번역하지 않았습니다.

작은 수정 하나를 설치부터 완료까지 따라가 봅니다. 하팅이 무엇인지는
[README.ko.md](README.ko.md)에 있고, 이 문서는 동작을 확인하는 가장 짧은 길입니다.

## 시작 전에

- Linux 또는 macOS. Windows(Git Bash)는 실험 단계입니다.
- Git, `curl` 또는 `wget`, 그리고 PyYAML이 설치된 Python 3.11 이상
  (`python3 -m pip install --user pyyaml`).
- 설치하고 로그인한 에이전트 CLI 하나 이상: Claude Code, Codex, OpenCode.
  Claude Code만 있어도 됩니다.
- herdr, Cairn, NAS, 원격 메모리 교환, GPU는 필요 없습니다.

## 설치와 확인

```sh
curl -fsSL https://github.com/dmlguq456/hearting/releases/latest/download/install.sh | sh -s -- --runtime claude
~/.local/bin/hearting runtime doctor --runtime claude --strict
```

세 하네스를 모두 설치하려면 `-s -- --runtime claude`를 빼고 두 번째 명령에
`--runtime all`을 쓰세요. 설치기는 이미 열린 셸의 `PATH`를 바꾸지 못하므로, 그
디렉터리를 `PATH`에 넣기 전까지는 `~/.local/bin/...` 경로를 쓰세요.

설치기는 안내를 한 줄 출력합니다. 백그라운드 Claude 작업자는 기본으로 bypass
모드로 돌고, 같은 안내가 allowlist로 바꾸는 파일을 알려 줍니다.
[권한](README.ko.md#권한)을 참고하세요.

## 오타 하나 고치기

작은 버그와 기존 시험이 있는 Git 프로젝트 안에서 에이전트 CLI를 열고 평소 말로
맡기세요.

> 인사 출력의 오타를 고치고 기존 시험을 돌린 뒤 diff를 보여줘.

하팅 명령을 직접 칠 필요는 없습니다. 에이전트는 변경을 로컬에 두고 시험을 돌린 뒤
무엇을 실행했고 무엇은 못 했는지 알려 줍니다. 시험 통과와 시험을 돌리지 못한
경우는 서로 다른 결과로 보고됩니다.

## 끝나는 것 보기

다른 터미널에서:

```sh
~/.local/bin/fleet --once    # 한 번의 평문 스냅샷, curses 불필요
~/.local/bin/fleet           # 실시간 전체 화면
```

작업자의 원문 출력은 에이전트에게 attempt id를 물어 보고
`hearting run attempt-view <attempt-id> --follow`로 봅니다.

## 결과 읽기

바뀐 파일, 확인 결과, 남은 일을 읽으세요. 한 줄 수정보다 큰 일이면 계획, 근거,
보고서가 프로젝트의 `.agent_reports/` 폴더에 남습니다. 작업이 막혀도 이 폴더를
지우지 마세요. 다음 단계가 그 안의 근거를 읽습니다.

## 기대할 수 있는 것

약속이 아니라 한 번 잰 결과입니다. 아래 수치는
[`tools/release/first-journey.py`](tools/release/first-journey.md)로 잰 것입니다.
게시된 v3.66.2를 격리된 Linux 환경에 설치하고, 하네스마다 세션 하나를 돌렸으며,
확인 카드에는 답하지 않았습니다. 시간은 모델이 일한 시간뿐입니다. 로그인과
사람이 읽고 답하는 시간은 빠져 있습니다.

| 하네스 | 작은 수정(오타, 집중 시험, diff) | 중간 과업(여러 파일) |
|---|---:|---:|
| Claude Code | 33.7초 (재실행 27.5초) | 47.8초 |
| Codex | 58.1초 (재실행 60.8초) | 142.3초 |
| OpenCode | 45.0초 | 165.7초 |

- 중간 과업은 한 세션에서 오타 수정, 선택 인자와 새 시험 파일 추가, README 수정,
  전체 시험, diff 표시를 했습니다.
- 설치는 Claude Code만 6.4초, 세 하네스 13.4초 걸렸습니다.
- 모두 성공했지만 표본이 작습니다. 몇 번의 실행, 한 대의 기기, 하네스당 계정 하나입니다.
- 토큰 수는 모으지만 하네스마다 세는 방식이 달라(Codex 입력은 캐시 포함, Claude는
  캐시를 따로 표기) 하네스끼리 비교하면 안 됩니다.
- 구독 한도 대비 사용량은 재지 않았습니다. 한도 표시는 다른 세션과 함께 움직이고,
  토큰을 한도 %로 바꿀 수도 없습니다.

## 막히면

`PATH`, Python 버전, 하나의 하네스만 쓰는 구성, 권한, 거절된 동작, 알 수 없는
진행은 [자주 묻는 질문](FAQ.ko.md)을 보세요. 처음 맡긴 요청과 에이전트가 보고한
실패를 에이전트에게 그대로 알려 주세요.
