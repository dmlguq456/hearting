# 빠른 시작

유지보수 기준인 [QUICKSTART.md](QUICKSTART.md)의 한국어 번역입니다. 두 문서가
다르면 영어판이 맞습니다. 명령, 경로, 설정 키는 번역하지 않았습니다.

작은 수정 하나를 설치부터 완료까지 따라가 봅니다. 하팅이 무엇인지는
[README.ko.md](README.ko.md)에 있고, 이 문서는 동작을 확인하는 가장 짧은 길입니다.

## 시작 전에

- Linux 또는 macOS. Windows(Git Bash)는 실험 단계입니다.
- Git, `curl` 또는 `wget`, 그리고 PyYAML이 설치된 Python 3.11 이상
  (`python3 -m pip install --user pyyaml` 또는 `python3-yaml` 같은 OS 패키지).
  Python 버전은 설치기가 확인합니다.
- 설치하고 로그인한 에이전트 CLI 하나 이상: Claude Code, Codex, OpenCode.
  Claude Code만 있어도 됩니다.
- herdr, 외부 산출물 도구, NAS, 원격 메모리 교환, GPU는 필요 없습니다.

지금까지 확인한 범위: CI가 Ubuntu에서 Claude Code·Codex·OpenCode용 설치를 확인하고,
아래 실측은 Linux에서 했습니다. macOS는 처음부터 끝까지 확인하지 않았고,
Windows(Git Bash)는 실험 단계입니다.

## 설치와 확인

```sh
curl -fsSL https://github.com/dmlguq456/hearting/releases/latest/download/install.sh | sh -s -- --runtime claude
~/.local/bin/hearting runtime doctor --runtime claude --strict
```

세 하네스를 모두 설치하려면 `-s -- --runtime claude`를 빼고 두 번째 명령에
`--runtime all`을 쓰세요. 설치기는 이미 열린 셸의 `PATH`를 바꾸지 못하므로, 그
디렉터리를 `PATH`에 넣기 전까지는 `~/.local/bin/...` 경로를 쓰세요.

설치기는 꼭 읽어야 할 안내를 하나 출력합니다. 백그라운드 Claude 작업자는 기본으로
bypass 모드로 돌고, 같은 안내가 allowlist로 바꾸는 파일을 알려 줍니다.
[권한](README.ko.md#권한)을 참고하세요. OpenCode도 설치하고 Claude Code 사용자
규칙이 있으면, 그 규칙이 이제 OpenCode에도 적용된다는 안내가 하나 더 나옵니다. 끄는
법은 같은 절에 있습니다.

`runtime doctor`는 PyYAML이나 herdr처럼 빠진 선택 요소가 있으면 그것과, 그것
없이 안 되는 일을 알려 줍니다. herdr에 대한 줄은 이 따라 하기와 상관없습니다.

## 오타 하나 고치기

작은 버그와 기존 시험이 있는 Git 프로젝트 안에서 에이전트 CLI를 열고 평소 말로
맡기세요.

> 인사 출력의 오타를 고치고 기존 시험을 돌린 뒤 diff를 보여줘.

하팅 명령을 직접 칠 필요는 없습니다. 에이전트는 변경을 로컬에 두고 시험을 돌린 뒤
무엇을 실행했고 무엇은 못 했는지 알려 줍니다. 시험 통과와 시험을 돌리지 못한
경우는 서로 다른 결과로 보고됩니다.

바로 해 볼 프로젝트가 없다면 [데모 파일](tools/release/fixtures/first-journey/)에서
`greet.py`와 `focused_test.py.txt`를 빈 폴더에 복사하고, `focused_test.py.txt`의
이름을 `test_greet.py`로 바꾼 뒤 새 Git 저장소에 commit하세요. `greet.py`가
`Helo` 대신 `Hello`를 돌려줄 때까지 시험이 실패합니다. 기대하는 변경은 한 줄입니다.

```diff
-    return "Helo"
+    return "Hello"
```

## 끝나는 것 보기

다른 터미널에서:

```sh
~/.local/bin/fleet --once    # 한 번의 평문 스냅샷, curses 불필요
~/.local/bin/fleet           # 실시간 전체 화면
```

작업자의 원문 출력은 에이전트에게 attempt id를 물어 보고
`hearting run attempt-view <attempt-id> --follow`로 봅니다.

## 결과 읽기

바뀐 파일, 확인 결과, 남은 일을 읽으세요. 이번처럼 작고 되돌릴 수 있는 수정은
사용자 폴더에서 바로 하고, 하팅이 자동으로 기록합니다. 더 큰 변경은 보통 프로젝트
옆의 별도 git worktree `<project>-wt/<name>`에 자기 브랜치로 있고, 사용자의 폴더는
그대로입니다. 결과에 그 폴더와 브랜치, 커밋했는지가 나옵니다. 한 줄 수정보다 큰 일이면 계획, 근거,
보고서가 프로젝트의 `.agent_reports/` 폴더에 남습니다. 작업이 막혀도 이 폴더를
지우지 마세요. 다음 단계가 그 안의 근거를 읽습니다.

## 기대할 수 있는 것

약속이 아니라 과업마다 한 번 잰 결과입니다. 아래 수치는
[`tools/release/first-journey.py`](tools/release/first-journey.md)로 잰 것입니다.
게시된 v3.82.17을 2026-11-06에 격리된 Linux 환경에 설치하고, 하네스마다 세션 하나를
돌렸으며, 확인 카드에는 답하지 않았습니다. 시간은 모델이 일한 시간뿐입니다. 로그인과
사람이 읽고 답하는 시간은 빠져 있습니다.

| 하네스 | 작은 수정(오타, 집중 시험, diff) | 중간 과업(여러 파일) |
|---|---:|---:|
| Claude Code | 20.7초 | 24.6초 |
| Codex | 22.8초 | 35.1초 |
| OpenCode | 9.5초 | 20.2초 |

- 중간 과업은 한 세션에서 오타 수정, 선택 인자와 새 시험 파일 추가, README 수정,
  전체 시험, diff 표시를 했습니다. 세 하네스 모두 그 자리에서 고쳤고, 하팅이 자동으로
  기록했습니다.
- 설치는 Claude Code만 6.9초, 세 하네스 13.0초 걸렸습니다.
- 모두 성공했지만 표본이 작습니다. 과업마다 한 번, 한 대의 기기, 하네스당 계정 하나입니다.
- 토큰 수는 모으지만 하네스마다 세는 방식이 달라(Codex 입력은 캐시 포함, Claude는
  캐시를 따로 표기) 하네스끼리 비교하면 안 됩니다.
- 구독 한도 대비 사용량은 재지 않았습니다. 한도 표시는 다른 세션과 함께 움직이고,
  토큰을 한도 %로 바꿀 수도 없습니다.

백그라운드 작업자가 일을 마치면 완료 요약에 걸린 시간과 세션 토큰이 나오고, 빠지거나
일부만 센 값은 따로 표시됩니다. 위와 같은 아주 작은 수정은 하팅이 자동으로 기록만
하므로 이 줄이 나오지 않습니다.

## 막히면

`PATH`, Python 버전, 하나의 하네스만 쓰는 구성, 권한, 거절된 동작, 알 수 없는
진행은 [자주 묻는 질문](FAQ.ko.md)을 보세요. 처음 맡긴 요청과 에이전트가 보고한
실패를 에이전트에게 그대로 알려 주세요.
