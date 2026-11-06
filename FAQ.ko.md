# 자주 묻는 질문

유지보수 기준인 [FAQ.md](FAQ.md)의 한국어 번역입니다. 두 문서가 다르면 영어판이
맞습니다. 명령, 경로, 설정 키는 번역하지 않았습니다.

## 에이전트 CLI 셋이 모두 필요한가요?

아니요. Claude Code만 쓰는 구성도 지원합니다. 같은 하네스의 다른 세션이 독립
검토를 맡을 수 있고, 하네스를 더 설치하면 작업자와 검토를 하네스에 나눠 둘 수
있습니다. [실측](QUICKSTART.ko.md#기대할-수-있는-것)에서 Claude만 설치한 환경에서
설치와 작은 수정이 끝났습니다. CI는 Ubuntu에서 세 하네스용 설치를 확인하고, 실측은
Linux에서 했습니다. macOS는 처음부터 끝까지 확인하지 않았고, Windows(Git Bash)는
실험 단계입니다.

## 백그라운드 작업자는 어떤 권한을 갖나요?

하팅이 백그라운드로 분사한 작업자는 Claude Code를 기본으로 bypass 모드
(`--permission-mode bypassPermissions`)로 돌립니다. 권한 질문에 답할 사람이
없기 때문입니다. 설치기가 이 사실을 안내합니다. 직접 연 세션은 사용자의 하네스
권한을 그대로 씁니다.

allowlist 모드를 쓰려면 `${XDG_CONFIG_HOME:-$HOME/.config}/hearting/dispatch-defaults.yaml`을
고치세요. `headless:` 블록이 이미 있으면 아래 값만 바꾸고, 없으면 두 줄을 모두
추가합니다.

```yaml
headless:
  claude_permission_mode: allowlist
```

`headless:` 키를 두 번 쓰거나 인라인 매핑을 쓰지 마세요. allowlist는 작업자를
`acceptEdits`와 제한된 도구 목록(하팅 유틸리티, 읽기 전용 git, 시험 실행기,
워크트리와 산출물 루트 안의 편집)으로 돌립니다. bypass를 쓸 수 없는 곳(root나
sudo, 어느 Claude 설정 범위에서든 `permissions.disableBypassPermissionsMode`)에서는
하팅이 알아서 allowlist로 바꾸고 쓴 모드를 기록합니다. 파일이 잘못되었거나 읽을
수 없으면 배포 기본값인 bypass가 적용됩니다. `hearting config status`로 파일이
읽혔는지 볼 수 있습니다.

`DISPATCH_DEFAULTS_CONFIG`로 다른 파일을 가리킬 수 있습니다. 이 설정은 Claude
Code 작업자에만 적용됩니다. Codex 작업자는 Codex의 `workspace-write` 샌드박스에서
승인 질문 없이 돌고, OpenCode 작업자는 사용자의 OpenCode 권한 설정에서 출발합니다.
`peer-steward start`로 띄운 peer 세션도 기본은 bypass이며, 같은 파일의 `steward:`
아래 `child_permission_mode: inherit`로 부모의 모드를 따르게 할 수 있습니다.
이 설정을 바꾸는 한 줄 명령은 없습니다.

## 동작이 거절된 이유는 무엇인가요?

기본적으로 분사 도구는 차이가 승인된 작업 안에 있으면 현재 입력으로 계속 진행합니다.
관측된 피해가 생길 동작만 거절합니다. 승인 범위 밖에 쓰거나 권한을 주는 것, 다른
경로의 작업을 건드리는 것, 이미 시작한 attempt를 다시 띄우는 것, 대체된 결과를
현재 결과로 취급하는 것입니다. 그 순간 하팅이 읽지 못한 기록은 거절이 아닙니다.
메시지가 잠시 뒤 같은 명령을 다시 실행하라고 안내합니다. 하네스나 빠진 설정이 자기
이유로 거절할 수도 있습니다. 메시지에 이유가 적혀 있습니다. 그 메시지를 에이전트에게
주면 처음 요청 안에서 이어 갑니다. 내부 명령을 배우거나 새 작업을 시작할 필요는 없습니다.

## 바뀐 내용이 내 프로젝트 폴더에 보이지 않습니다

요청이 소스 코드를 바꾸면 하팅은 보통 프로젝트 옆의 별도 git worktree
`<project>-wt/<name>`에서 자기 브랜치로 바꿉니다. 사용자의 폴더, 브랜치, 커밋하지
않은 파일은 그대로입니다. 결과에 변경이 있는 폴더와 브랜치, 커밋했는지가 나옵니다.
거기서 검토하고, 내 브랜치로 가져오고 싶을 때 에이전트에게 말하세요. 작고 되돌릴
수 있는 수정은 사용자 폴더에서 바로 하고, 하팅이 자동으로 기록합니다. 따로 시작하거나
끝낼 일은 없습니다.

## 프로젝트에 .agent_reports가 생긴 이유는 무엇인가요?

모든 하네스와 프로젝트의 모든 git worktree가 공유하는 계획, 근거, 결과, 인계를
담습니다. 옛 `.claude_reports/` 폴더는 이전 이름으로 계속 읽힙니다. 작업이 막혀도
이 폴더를 지우지 마세요.

## hearting이나 fleet를 찾을 수 없다고 나옵니다

실행 파일은 `~/.local/bin`에 있습니다. 셸 시작 파일에서 그 디렉터리를 `PATH`에
추가하세요. 하팅은 시작 파일을 고치지 않습니다. 지금 터미널에서는 전체 경로로
부르면 됩니다. 예: `~/.local/bin/hearting runtime doctor --runtime claude --strict`.

## Python은 어느 버전이 필요한가요?

Python 3.11 이상과 그 인터프리터용 PyYAML(`python3 -m pip install --user pyyaml`
또는 `python3-yaml` 같은 OS 패키지)이 필요합니다. `python3`가 3.11보다 낮으면
설치기가 분명한 메시지와 함께 멈춥니다. PyYAML은 설치하지 않으며, 빠져 있으면
`hearting runtime doctor`가 알려 줍니다. PyYAML이 없으면 계획 단계로 시작하는 일은
계획 전에 멈추고, 한 번에 끝나는 direct 수정은 그대로 돕니다.

## herdr, NAS, GPU, 외부 산출물 도구 없이 쓸 수 있나요?

네, 모두 선택 기능입니다. herdr가 없으면 peer 창, pane 제목, peer 전달, 자동
`/clear`, 질문 알림은 tmux 안에서 일할 때 tmux로 합니다(데스크톱 세션은 질문
알림을 `notify-send`로도 받습니다). 둘 다 없으면 이 기능들은 꺼지고 관련 hook은
조용히 끝나며, `hearting runtime doctor`가 그렇다고 알려 줍니다. 완성된 산출물을
외부 도구로 보내는 단계는 연결한 도구가 없으면 사용할 수 없다고 알립니다. `compute-hosts`는 호스트를 지정하기
전까지 주석 처리된 템플릿이고, 호스트 없이 연산을 요청하면 분명한 오류가 납니다.
`~/.config/hearting/memory-sync.json`으로 교환을 설정하지 않으면 메모리는 로컬에만
남습니다.

## 언어 설정이 모든 문구를 번역하나요?

거의 그렇습니다. 대화는 사용자의 언어를 따릅니다. 요청 시작 때의 경로 카드, 한 줄
방향 통지, 끝날 때의 요약, Fleet 화면처럼 하팅이 사용자에게 찍는 고정 문구는
영어와 한국어가 함께 든 공통 문구 모음 하나에서 나옵니다.
한국어를 선언하지 않으면 영어입니다. 한 번만 선언하면 됩니다.
`${XDG_CONFIG_HOME:-~/.config}/hearting/fleet.json`에 `"title_language": "ko"`를
두거나, `FLEET_NOW_LANG=Korean`을 설정하거나, Claude Code의 `language` 설정을
한국어로 둡니다. 같은 선언이 Fleet 세션 제목과 NOW 줄의 언어도 정합니다. 내부
진단처럼 에이전트만 읽는 줄은 번역하지 않습니다.

## 진행을 알 수 없다고 나오는 이유는? 원문 출력을 볼 수 있나요?

수집기가 자료를 못 얻거나 실패할 수 있습니다. Fleet는 읽지 못한 값을 `?`로
보여 줍니다. 활동이 0이라는 뜻이 아니고, 그것만으로 작업이 실패했다는 뜻도
아닙니다. 에이전트에게 실제 attempt id를 물어 보고
`hearting run attempt-view <attempt-id> --follow`를 실행하세요. `.agent_reports/`의
인계에도 무슨 일이 있었는지 남습니다.

## 작업 하나에 얼마가 드나요?

정해진 추정치는 없습니다. 시작 카드는 작업 흐름을 설명하고, 분사한 요청이 끝날
때의 요약에는 비용 한 줄(걸린 시간과 세션 토큰. 하네스가 알려 주지 않은 값은
`unknown`)이 있으며, Fleet는 하네스가 보고한 토큰 수와 사용량 창을 보여 줍니다.
어느 것도 요금이나 한도 예측이 아닙니다. 하팅 자체가 더하는 몫은 Codex에서 한
기계로 세 번씩 쟀습니다. 첫 반응이 하팅이 없을 때보다 약 0.7초 늦었고 요청마다
입력이 약 2.4K 토큰 늘었습니다. 두 턴짜리 작은 버그 수정은 하팅이 있을 때 38초,
없을 때 39초로 거의 같았습니다. 처음부터 끝까지 잰 시간 자료는 한계와 함께
[기대할 수 있는 것](QUICKSTART.ko.md#기대할-수-있는-것)에 있습니다.

## 업데이트 뒤 새 세션이 필요한 이유는?

활성 릴리스를 바꾸는 일과 열린 세션이 지침을 다시 읽는 일은 다릅니다.
`~/.local/bin/hearting runtime status --runtime all`을 실행해 하네스마다 나오는
`next=` 값(`--json`에서는 `session_action`)을 따르세요. 하네스가 모든 지침을 실시간으로 다시 읽는다고
가정하지 마세요.

## 하팅이 OpenCode가 읽는 내용을 바꾸나요?

네, 한 곳에서 바꿉니다. 하팅은 OpenCode 전역 `AGENTS.md`를 하팅 지침을 싣는
파일로 관리합니다. `~/.claude/rules/`(또는 `$CLAUDE_CONFIG_DIR/rules/`)에 Claude
Code 사용자 규칙이 있으면 그 규칙을 이 파일에서 하팅 지침보다 앞에 싣습니다. 특정 경로에만 적용되는 규칙은 빼고, 규칙 파일은 읽기만 합니다. 그
결과 규칙 본문은 OpenCode가 쓰는 모델 제공자에게도 전달됩니다. 끄려면
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/dispatch-defaults.yaml`에 아래 줄을
넣으세요.

```yaml
user_rules:
  opencode: off
```

다음 설치, 업데이트, 또는 `hearting runtime refresh --runtime opencode`가 하팅
지침만 있는 원래 상태로 되돌립니다. 그 `AGENTS.md`를 고쳐 두었다면 고친 판은
옆에 `AGENTS.md.edited-<시각>`으로 남습니다.

Codex에서는 내용이 있는 `~/.codex/AGENTS.override.md`를 직접 두고 있을 때만
하팅이 사용자 파일에 씁니다. Codex는 그 전역 파일 하나만 읽기 때문에, 하팅이 그
내용 아래에 표시된 블록 하나로 자기 지침을 덧붙입니다. 업데이트는 그 블록만 바꾸고
제거하면 그 블록만 지웁니다.

## Windows를 지원하나요?

Windows(Git Bash)는 실험 단계입니다. `fleet --once`와 `fleet --json`은 curses가
필요 없어 거기서도 동작합니다. 첫 릴리스의 범위는 Linux와 macOS입니다.

## OpenRig와는 어떤 관계인가요?

하팅은 Claude Code, Codex, OpenCode에 걸쳐 요청을 검증과 오래 남는 인계로 잇는
데 집중합니다. OpenRig의 터미널 화면과 상시 팀 운영은 가시성에서 참고할 만합니다.
비교한 것은 OpenRig v0.6.13뿐이며 현재 릴리스가 아닙니다. 어느 쪽이든 속도, 안전,
비용의 우위는 재지 않았습니다.
