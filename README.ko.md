<h1 align="center">Hearting</h1>

<p align="center"><strong>코딩 에이전트와 일하는 방식을 하나로 — Claude Code, Codex, OpenCode 어디서든.</strong></p>

<p align="center">하팅은 에이전트가 당신의 의도에서 벗어나지 않게 합니다. 강한 모델이 계획을 세우고,<br>
싼 작업자가 작게 확인받으며 실행하고, Fleet가 모든 세션과 작업자를 실시간으로 보여 줍니다.</p>

<p align="center">
  <img alt="Claude Code: 네이티브" src="https://img.shields.io/badge/Claude_Code-native-D97757?style=flat-square">
  <img alt="Codex: 네이티브" src="https://img.shields.io/badge/Codex-native-111827?style=flat-square">
  <img alt="OpenCode: 네이티브" src="https://img.shields.io/badge/OpenCode-native-2563EB?style=flat-square">
  <img alt="플랫폼: Linux, macOS" src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS-4B5563?style=flat-square">
  <img alt="라이선스: MIT" src="https://img.shields.io/badge/license-MIT-059669?style=flat-square">
</p>

<p align="center"><a href="README.md">English</a> · <strong>한국어</strong></p>

<p align="center">
  <a href="#빠른-시작">빠른 시작</a> ·
  <a href="#fleet-모든-에이전트를-한-화면에서">Fleet</a> ·
  <a href="#권한">권한</a> ·
  <a href="https://dmlguq456.github.io/hearting/">랜딩 페이지 · 에이전트 맵 ↗</a>
</p>

> 이 문서는 유지보수 기준인 [README.md](README.md)의 한국어 번역입니다.
> 명령, 경로, 설정 키와 식별자는 영문 원문을 기준으로 합니다.

<p align="center">
  <img src="docs/fleet.svg" alt="Fleet 그룹 화면: Claude Code 메인 세션, 그 세션이 분사한 owner, 그 아래 Claude Code와 Codex에서 도는 단계 작업자 셋(실행 중, 완료, 입력 대기). 각 행에 모델, 문맥 게이지, 경과 시간이 보인다." width="100%">
</p>
<p align="center"><sub>Fleet의 그룹 화면(<code>fleet-monitor</code>): 메인 세션, 그 세션이 분사한 owner, owner가 두 하네스에 띄운 단계 작업자. 합성 데이터로 그렸습니다.</sub></p>

<!-- TODO(v4.0, Fleet F1): Fleet 단일 화면이 확정되면 이 그림을 Explorer OVERVIEW 캡처로 바꾼다. -->

## 왜 하팅인가

에이전트는 회사마다, 모델마다, 쓸 수 있는 환경마다 다릅니다. 그렇다고
에이전트 없이 일할 수는 없습니다. 하팅은 그 차이 속에서도 일하는 방식을
하나로 지켜 줍니다. 중심은 사용자이고, 에이전트가 스스로 하는 몫이 작아도
일이 흐트러지지 않게 합니다.

- **에이전트가 달라도 일관되게.** 알아서 많이 하는 에이전트도, 거의
  하지 않는 에이전트도 있습니다. 하팅은 일을 시작하기 전에 방향을 사용자와
  확인하고, 작업자마다 범위를 정해 두고, 결과를 따로 검사합니다. 그래서 일이
  에이전트의 실력이 아니라 사용자가 요청한 내용에 묶여 있습니다.
- **어디서든 하나의 방식.** Claude Code, Codex, OpenCode 어느 쪽이든, 하나만
  쓰든 셋을 같이 쓰든 같은 요청, 같은 단계, 같은 산출물, 같은 메모리로
  일합니다. Fleet가 그 전부를 한 화면에서 보여 줍니다.
- **저비용 에이전트의 목발.** 하팅이 약한 모델을 똑똑하게 만들 수는 없습니다.
  대신 약한 모델도 사용자의 의도를 끝까지 수행하게 합니다. 강한 모델이 의도와
  계획을 정하고(*frame*), 싼 작업자가 작은 단위로 확인받으며 실행합니다
  (*분사*, dispatch).
- **에이전트의 부담은 가볍게.** 에이전트가 긴 절차를 외워야만 돌아가는 단계를
  만들지 않도록 설계했습니다. 절차는 하네스가 들고 있습니다.
- **이미 있는 것부터.** 계획 전에 싼 읽기 전용 탐색이 코드, 이전 산출물,
  메모리, git 기록에 이미 정해진 것을 추려 줍니다. 계획은 모델이 우연히 아는
  지식이 아니라 거기서 출발합니다.

## 요청이 처리되는 방식

원하는 결과를 평소 말로 설명하면 됩니다. 외울 명령 이름이 없고, 출력은 대화
언어를 따릅니다.

> “로그인 API를 구현하고 테스트한 뒤 변경 보고서까지 남겨줘.”

```text
요청
  ├─ prior-art 탐색    프로젝트에 이미 있는 것: 코드, 산출물, 메모리, git 기록
  ├─ frame             강한 모델 두 갈래가 진행 경로를 제안하고, 사용자가 방향을 확인
  └─ owner             세션 하나가 요청과 완료 기준을 끝까지 붙듦
       ├─ plan ─► plan check
       ├─ execute      싼 작업자. 소스를 고치는 단계는 이것 하나
       ├─ test, review 독립 검사. 가능하면 다른 하네스에서
       └─ report       무엇을 바꾸고, 무엇을 확인했고, 무엇이 남았는지
                       … 이 모든 단계를 Fleet가 실시간으로 보여 줌
```

얼마나 많은 단계가 돌지는 요청에 따라 다릅니다. 한 줄 답이나 오타는 그 자리에서
처리합니다(`direct`). 작은 수정은 frame과 빠른 작업자 하나로 끝냅니다(`quick`).
크거나 위험한 일은 계획서와 체크리스트, 독립 검토, 마지막에는 적대적 검사나
보안 검사까지 더합니다(`standard → strong → thorough → adversarial`).

단계마다 따로 세션을 띄우고 그 단계에 맞는 모델 예산을 씁니다. 어려운 판단은
`deep`, 중요한 판단은 `balanced-deep`, 정해진 실행은 `balanced`나 `light`, 작은
보조 호출은 `mini`입니다. 이 선택은 요청 기록에 함께 남습니다. 공통 계약에는
벤더 모델 이름이 없고, 각 예산에 어떤 실제 모델을 쓸지는 사용자가 정합니다
([하네스와 모델](#하네스와-모델)).

그대로 맡길 수 있는 요청 예:

> “이 저장소를 분석하고 다음 기능을 위한 PRD를 만들어줘.”

> “논문과 실험 코드를 검토하고 재현 계획을 만들어줘.”

> “이전 결정을 찾아서 이 프로젝트의 기존 이름 규칙을 적용해줘.”

전체 진입점은 [capabilities/README.md](capabilities/README.md), 경로 선택 규칙은
[core/WORKFLOW.md](core/WORKFLOW.md)에 있습니다.

## Fleet: 모든 에이전트를 한 화면에서

에이전트를 백그라운드로 돌릴 때 가장 흔한 문제는 "모른다"는 것입니다. 아직
도는지, 멈췄는지, 내 답을 기다리는지, 끝났는지. Fleet는 이 기계의 모든
Claude Code, Codex, OpenCode 세션과 하팅이 그 세션들에서 분사한 모든 작업자에
대해 그 답을 터미널 화면 하나로 보여 줍니다.

### 한눈에 보이는 것

맨 위 줄에는 하네스별 사용량 한도 게이지와, 도는 세션·쉬는 세션·답을 기다리는
세션이 몇 개인지 나옵니다. 그 아래는 질문 하나에 화면 하나입니다.

| 화면 | 알려 주는 것 |
|---|---|
| **OVERVIEW** | 프로젝트별로 묶은 모든 세션: 하네스, 모델과 effort, 짧은 제목, 남은 문맥, 지금 하는 일, 경과 시간. 도는 중·쉬는 중·답 대기가 서로 다른 표시로 보입니다. |
| **ROUTES** | 요청마다 단계 줄 `plan › execute › test › report`와 그 아래에서 도는 작업자. |
| **QUESTIONS** | 사용자의 답이나 승인을 기다리는 모든 에이전트, 오래된 순. 하네스가 답 경로를 주면 Fleet에서 바로 답하고, 아니면 해당 세션으로 안내합니다. |
| **PEERS** | 어떤 세션이 어떤 세션을 지켜보거나 조정하는지, 마지막으로 주고받은 것. |
| **HISTORY** | 터미널 자리마다 이전 세션들. 새 세션으로 이어진 일도 따라갈 수 있습니다. |
| **RESOURCES** | 이 기계와 설정한 호스트의 CPU, 메모리, GPU별 사용량, 각 GPU 프로세스를 가진 세션. |
| **MEMORY** | 오늘 공용 메모리에 저장된 것, 프로젝트별. |
| **MODELS · SYSTEM** | 각 예산이 어떤 실제 모델로 연결되는지, 설치된 하팅 릴리스. |

세션이나 요청을 고르면 그 페이지가 열리고, 거기서 원문 출력(도구 호출과 결과를
순서대로)까지 볼 수 있습니다. 하네스를 직접 열지 않아도 됩니다.

<!-- v4.0용 핵심 화면 캡처 목록(README 설계 문서 참고):
     F1 OVERVIEW(머리 그림) · F2 ROUTES · F3 QUESTIONS · F4 세션 페이지 ·
     F5 RESOURCES · F6 원문 출력 · F7 짧은 이동 장면(15초 이하) · F8 좁은 폭.
     tools/fleet/tests/explorer_capture.py로 화면 없이 렌더하고, 가린 영어 view-model fixture를 쓰며,
     데모 데이터임을 표기한다. -->

### 화면을 믿을 수 있는 이유

- **작업이 스스로 남기는 기록으로 그립니다.** Fleet는 분사 기록부(dispatcher가
  쓰는 attempt registry)와 각 하네스의 세션 상태를 함께 읽습니다. 멈춘 작업자는 "조용해진
  부모 세션"이 아니라 멈춘 작업자로 보이고, 부모를 잃은 행도 버리지 않고
  보여 줍니다.
- **모르는 것은 모른다고.** Fleet가 읽지 못한 값은 `?`로 표시합니다. 0이나
  성공으로 바꾸지 않습니다.
- **보기만으로는 아무것도 바뀌지 않습니다.** 관찰이 작업을 끝내거나, 다시
  돌리거나, 정리하지 않습니다. Fleet는 사용자가 고른 일, 예를 들어 기다리는
  질문에 대한 답만 수행합니다.

### 명령

```bash
fleet              # Explorer 전체 화면(curses 필요)
fleet --once       # 단순 스냅숏 한 번
fleet --json       # 같은 상태를 JSON으로(스크립트용)
fleet-monitor      # 이 문서 맨 위 그림의 그룹·프로세스 화면
```

`--once`와 `--json`은 `curses`가 필요 없어서 Windows의 Git Bash에서도 됩니다.
세션 제목은 사용자의 언어를 따르며,
`${XDG_CONFIG_HOME:-~/.config}/hearting/fleet.json`의 `title_language`로 바꿀 수
있습니다.

### 알려진 한계

- Explorer가 기본 `fleet` 화면입니다. 화면 배치와 키 조작은 v4.0 전에
  마무리하는 중입니다.
- 일부 상태 단어와 활동 줄이 아직 한국어로만 나옵니다. 공통 번역표로 옮길
  예정입니다.

## 빠른 시작

### 시작 전에

- **Linux 또는 macOS.** Windows(Git Bash)는 실험적입니다.
- **Python 3.11 이상**과 그 Python용 PyYAML:
  `python3 -m pip install --user pyyaml`
- **Git**, 그리고 `curl` 또는 `wget`.
- **설치하고 로그인한 에이전트 CLI 하나 이상:** Claude Code, Codex, OpenCode 중
  하나. Claude Code 하나만으로도 됩니다.

<!-- TODO(공개 준비 5단계): 시험한 OS × 하네스 표를 넣고 Python 최소 버전을 확정한다. -->

### 설치

세 하네스 모두:

```bash
curl -fsSL https://github.com/dmlguq456/hearting/releases/latest/download/install.sh | sh
~/.local/bin/hearting runtime doctor --runtime all --strict
```

Claude Code만:

```bash
curl -fsSL https://github.com/dmlguq456/hearting/releases/latest/download/install.sh | sh -s -- --runtime claude
~/.local/bin/hearting runtime doctor --runtime claude --strict
```

설치기는 설치할 아카이브와 같은 릴리스 태그에서 오며, 먼저 그 아카이브의
SHA-256을 확인합니다. 고른 하네스에 전체 기능 묶음을 바뀌지 않는(immutable)
번들로 활성화하고, `hearting`, `fleet`, `fleet-monitor`, `mem`,
`compute-hosts`를 `~/.local/bin`에 두며, OS가 지원하면 매일 업데이트 확인을
등록합니다. 하네스의 자격 증명, 세션, 로그, 데이터베이스, 셸 시작 파일은
건드리지 않습니다. 이미 열린 셸의 `PATH`도 바꾸지 못하므로, `~/.local/bin`을
`PATH`에 넣기 전까지는 위처럼 `~/.local/bin/...` 경로를 쓰세요.

설치기가 작업자 기본 권한에 대한 안내를 한 줄 출력합니다. 첫 작업 전에
[권한](#권한)을 읽어 주세요.

### 첫 작업

Git 프로젝트 안에서 에이전트 CLI를 열고 작은 일을 맡겨 보세요.

> “인사 출력의 오타를 고치고 기존 시험을 돌린 뒤 diff를 보여줘.”

다른 터미널에서 `fleet`를 열면 진행이 보입니다. 끝나면 바뀐 내용, 확인을 위해
실행한 것, 못 한 일을 받습니다. 한 줄 수정보다 큰 일이면 계획, 근거, 보고서가
프로젝트의 `.agent_reports/` 폴더에 남습니다.

[빠른 시작 안내](QUICKSTART.ko.md)는 이 작업을 실측 시간과 함께 끝까지 따라가고,
[자주 묻는 질문](FAQ.ko.md)은 `PATH`, Python 버전, 권한을 다룹니다.

<!-- TODO(공개 준비 1단계): 예상 patch를 담은 데모 저장소를 게시하면 연결한다. -->

### 업데이트와 상태

```bash
hearting runtime status --runtime all   # 활성 릴리스, 최신 여부, 세션을 다시 열어야 하는지
hearting update                         # 새 릴리스를 준비·검증한 뒤 전환, 실패하면 되돌림
hearting auto-update status
hearting config status                  # 사용자가 소유한 모든 설정 파일과 그 상태
```

업데이트가 이미 열린 세션의 지침을 다시 읽게 하지는 않습니다. `runtime status`가
하네스마다 재호출, 새 세션, 재시작 중 무엇이 필요한지 알려 줍니다. 릴리스를
고정하거나 예약 확인을 끄려면 그 릴리스의 설치기를 쓰세요(`vX.Y.Z`를 릴리스
태그로 바꿉니다).

```bash
curl -fsSL https://github.com/dmlguq456/hearting/releases/download/vX.Y.Z/install.sh | sh -s -- --no-auto-update
```

체크섬은 전송 중 손상이나 파일 손상을 잡습니다. 배포자 진위는 이 저장소의
GitHub Release와 HTTPS에 기대며, 별도 서명은 없습니다.

## 권한

**백그라운드 Claude Code 작업자는 기본으로 bypass 모드로 돕니다.** 하팅이
백그라운드로 분사한 작업자는 `--permission-mode bypassPermissions`로 시작합니다.
권한 질문에 답할 사람이 없고, 거절이 이어지면 작업자가 일을 하다 멈추기
때문입니다. 사용자가 직접 연 세션은 사용자의 하네스 권한 설정을 그대로 씁니다.

allowlist 모드로 바꾸려면
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/dispatch-defaults.yaml`을 고칩니다.
이미 `headless:` 블록이 있으면 그 안의 값을 바꾸고, 없으면 아래 두 줄을
추가합니다.

```yaml
headless:
  claude_permission_mode: allowlist
```

- allowlist는 작업자를 `acceptEdits`와 제한된 도구 목록으로 돌립니다. 하팅
  자체 도구, 읽기 전용 git, 시험 실행기, worktree와 산출물 루트 안의 편집만
  허용합니다.
- bypass를 쓸 수 없는 곳(root나 sudo로 실행, 또는 어느 Claude 설정 범위에든
  `permissions.disableBypassPermissionsMode`가 있을 때)에서는 하팅이 알아서
  allowlist로 바꾸고 실제로 쓴 모드를 기록합니다.
- 파일이 잘못됐거나 읽을 수 없으면 배포 기본값인 bypass가 적용됩니다.
  `hearting config status`로 파일이 읽혔는지 확인하세요. 기존 블록에 합치고,
  `headless:` 키를 두 번 쓰거나 한 줄 `{…}` 형식으로 쓰지 마세요.
  `DISPATCH_DEFAULTS_CONFIG`로 다른 파일을 가리킬 수 있습니다.
- `peer-steward start`로 띄운 peer 세션도 기본은 bypass입니다. 같은 파일의
  `steward:` 아래에 `child_permission_mode: inherit`를 두면 부모 세션의 모드를
  따릅니다.
- 이 설정은 Claude Code 작업자에만 적용됩니다. Codex 작업자는 Codex의
  `workspace-write` 샌드박스 안에서 승인 질문 없이 돌고, OpenCode 작업자는
  사용자의 OpenCode 권한 설정에서 출발합니다.

Claude Code를 설치하면 설치기가 이 기본값과 파일 경로를 한 줄로 알립니다.
입력은 요구하지 않습니다.

## 산출물과 현황

일은 대화 기록만이 아니라 읽을 수 있는 파일로 남습니다. 프로젝트마다 산출물
루트 `.agent_reports/`가 하나 있고, 모든 하네스와 그 프로젝트의 모든 git
worktree가 함께 씁니다.

| 폴더 | 남는 것 |
|---|---|
| `research/`, `analysis_project/` | 코드, 논문, 문서에 대한 조사와 분석 |
| `spec/` | 현재 설계도. 개정하면 이전 판을 남깁니다 |
| `plans/<date>_<slug>/` | 작업 한 회차: 계획, 체크리스트, 개발·시험 로그, 근거, 최종 보고서 |
| `documents/`, `experiments/` | 작성한 문서, 실험 준비와 결과 |

진행 중인 요청을 따라가려면:

```bash
hearting run attempt-view <attempt-id> --follow                    # 작업자의 원문 출력. 식별자는 에이전트가 알려 줌
hearting run workflow-supervisor status --route <route-id> --json  # 요청이 지금 어느 단계에 있는지
```

일이 막혔다고 `.agent_reports/`를 지우지 마세요. 다음 단계가 거기 남은 근거를
읽습니다. 예전 `.claude_reports/` 폴더도 이전 이름으로 계속 읽힙니다.

<!-- TODO(v4 W4): 요청이 닫히거나 PR이 병합될 때 갱신되는 프로젝트 현황 한 장. 나오면 여기에 설명한다. -->
<!-- TODO(공개 준비 4단계): 경과 시간과 토큰을 담은 완료 한 줄. 모르는 값은 모름으로 남긴다. -->

## 메모리

결정과 규칙은 세션이 끝나도 남아야 합니다. 하팅은 이 기계의 모든 세션, 프로젝트,
하네스가 함께 쓰는 로컬 저장소 하나에 그것을 둡니다.

- **저장소 하나.** 전문 검색(한·중·일 문자 포함)이 되는 SQLite,
  `${XDG_DATA_HOME:-~/.local/share}/hearting/memory/` 아래.
- **무엇이 중요한지는 에이전트가 판단.** 하네스가 지원하면 프롬프트마다 짧은
  후보 제목 몇 개가 함께 오고, 에이전트는 기록을 쓰기 전에 전문을 읽습니다.
  저장소는 형식, 범위, 수명만 지킵니다.
- **이력은 남습니다.** 바뀐 결정은 옛 결정을 지우지 않고 대체(supersede)합니다.
  인계 기록은 다음 세션이 가져갈 때까지 대기 상태로 남습니다.
- **원하지 않으면 로컬에만.** 여러 기계 사이의 메모리 공유는 선택입니다
  ([Shared Memory Exchange](INSTALL_LAYOUT.md#shared-memory-exchange)).
- `mem`이 `PATH`에 있어 바로 쓸 수 있습니다. 예: `mem recall "naming convention"`.

전체 계약은 [core/MEMORY.md](core/MEMORY.md)에 있습니다.

## 하네스와 모델

| 하네스 | 하팅이 그 하네스 고유의 발견 경로로 더하는 것 |
|---|---|
| Claude Code | Skill, agent, command, hook |
| Codex | Skill, custom agent, mode, hook |
| OpenCode | Skill, agent, command, 로컬 guard plugin |

모든 하네스가 전체 기능을 봅니다. 하네스에 없는 표면은 설치기가 이유와 함께
`SKIP`으로 표시하고, 차이를 숨기지 않습니다. Claude Code 하나만으로도 지원되는
구성이며, 하네스를 둘 이상 설치하면 하팅이 작업자와 독립 검토를 여러 하네스에
나눠 둘 수 있습니다.

공통 계약은 벤더 모델이 아니라 예산 이름을 씁니다. 하네스마다 예산을 실제
모델로 잇는 사용자 소유 파일 `<runtime-home>/agent-config/models.conf`가 있고,
설치 때 한 번 만들어진 뒤 덮어쓰지 않습니다. `light` 일에 더 싼 모델을 두는
곳이 여기입니다.

```bash
hearting model set codex light <model>[@effort]
hearting model get claude context-window --json
hearting model set claude context-window 800000
```

문맥 예산 기본값은 Claude Code와 OpenCode가 800,000 토큰, Codex가
1,000,000 토큰입니다(선택한 모델의 최대치를 넘지 않음). 새 세션부터 바뀐 설정을
쓰고, 열려 있는 세션은 원래 설정을 유지합니다. 자세한 내용은
[INSTALL_LAYOUT.md](INSTALL_LAYOUT.md#user-model-configuration)에 있습니다.

## 선택 기능

아래는 기본 작업에 필요하지 않습니다.

- **herdr** — 터미널 창 관리 도구. 있으면 Fleet와 peer 세션이 쓰고, 없으면 관련
  hook이 조용히 끝납니다.
- **Cairn** — 완성된 산출물을 읽는 선택 도구. 설정하지 않으면 스스로 사용 불가라고
  알리며, 일반 작업에는 영향이 없습니다.
- **연산 호스트와 GPU** — `compute-hosts`는
  `${XDG_CONFIG_HOME:-$HOME/.config}/hearting/compute-hosts.yaml`을 읽고, 이 파일은
  주석 달린 예시로 만들어집니다. 호스트를 채우기 전에는 `template`로 표시되고,
  호스트 없이 연산을 요청하면 분명한 오류를 냅니다. 파일이 없어도 Fleet는 이
  기계의 CPU와 GPU 사용량을 보여 줍니다.
- **기계 간 메모리 교환** — [메모리](#메모리)를 보세요.

## 하팅이 하지 않는 것

| 하지 않는 것 | 대신 |
|---|---|
| 에이전트 CLI를 대체하기 | Claude Code, Codex, OpenCode는 벤더 소유 그대로이고, 하팅은 각 하네스 고유의 발견 경로로 더해집니다 |
| 자격 증명, 세션, 로그, 하네스 데이터베이스 건드리기 | 각 하네스가 소유합니다 |
| 셸 시작 파일 고치기 | `~/.local/bin`을 `PATH`에 넣는 일은 사용자가 합니다 |
| 설치만으로 GPU 작업을 예약하거나 원격 에이전트 띄우기 | 연산은 사용자가 지정한 호스트에서만 돕니다 |
| 메모리를 기본으로 어딘가에 보내기 | 교환에 참여하기 전까지 메모리는 로컬에 머뭅니다 |
| 약한 모델을 똑똑하게 만들기 | 약한 모델에게 확인된 계획과 작은 확인 단계를 줍니다 |
| 요금 추정하기 | Fleet는 하네스가 알려 주는 사용량 한도와 토큰 수를 보여 줄 뿐, 청구액이나 한도 예측이 아닙니다 |

## 하팅 개발

```bash
git clone https://github.com/dmlguq456/hearting.git ~/hearting
cd ~/hearting
./tools/install/harness.sh runtime activate --runtime all
git config core.hooksPath tools/git-hooks
```

공통 동작은 `core/`, `capabilities/`, `roles/`에서 시작합니다. `adapters/` 아래
세 어댑터는 그것을 동등하게 투영한 것이며, 유지보수 원칙은
[AGENTS.md](AGENTS.md)에 있습니다. 공통 정의를 바꾼 뒤에는 생성물을 다시 만들고
검사합니다.

```bash
python3 tools/generate.py
python3 tools/generate.py --check
./tools/check-adaptation-boundary.sh
./tools/skill-conformance/check.sh
```

`pre-push` hook이 CI와 같은 생성물 검사를 돌립니다. packaged 활성화는 변경이
남은 작업 트리에서 번들을 만들지 않으므로, 설치 수명 주기 시험은 깨끗한
체크아웃에서 돌리세요. `--mode linked`는 실시간 디버깅용입니다.

풀 리퀘스트를 열기 전에 [CONTRIBUTING.md](CONTRIBUTING.md)(영어)를 읽어 주세요.

## 문서

| 목적 | 문서 |
|---|---|
| 유지보수 원칙 | [AGENTS.md](AGENTS.md) |
| 첫 작업, 자주 묻는 질문, 기여 | [QUICKSTART.ko.md](QUICKSTART.ko.md), [FAQ.ko.md](FAQ.ko.md), [CONTRIBUTING.md](CONTRIBUTING.md) |
| 전체 구조 한눈에 | [MANUAL.md](MANUAL.md) |
| 설치와 하네스별 투영 | [INSTALL_LAYOUT.md](INSTALL_LAYOUT.md) |
| 릴리스와 버전 | [RELEASE_POLICY.md](RELEASE_POLICY.md) |
| Capability와 role | [capabilities/README.md](capabilities/README.md), [roles/README.md](roles/README.md), [roles/MODES.md](roles/MODES.md) |
| 경로 선택과 산출물 | [core/WORKFLOW.md](core/WORKFLOW.md), [core/CONVENTIONS.md](core/CONVENTIONS.md) |
| Git, worktree, 분사, Fleet | [core/OPERATIONS.md](core/OPERATIONS.md) |
| 메모리 | [core/MEMORY.md](core/MEMORY.md) |
| Hook과 설계 원칙 | [core/HOOKS.md](core/HOOKS.md), [core/DESIGN_PRINCIPLES.md](core/DESIGN_PRINCIPLES.md) |

## 라이선스

[MIT](LICENSE)
