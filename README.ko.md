<h1 align="center">Hearting</h1>

<p align="center"><strong>Claude Code, Codex, OpenCode에서 이어지는 하나의 완결된 에이전트 워크플로.</strong></p>

<p align="center">Claude Code, Codex, OpenCode를 위한 로컬 우선 워크플로 계층입니다.</p>

<p align="center">
  <img alt="Claude Code: 네이티브" src="https://img.shields.io/badge/Claude_Code-native-D97757?style=flat-square">
  <img alt="Codex: 네이티브" src="https://img.shields.io/badge/Codex-native-111827?style=flat-square">
  <img alt="OpenCode: 네이티브" src="https://img.shields.io/badge/OpenCode-native-2563EB?style=flat-square">
  <img alt="설치: 관리형 릴리스" src="https://img.shields.io/badge/installation-one--line_release-059669?style=flat-square">
</p>

<p align="center"><a href="README.md">English</a> · <strong>한국어</strong></p>

<p align="center"><a href="https://dmlguq456.github.io/hearting/"><strong>랜딩 페이지 · 에이전트 맵 ↗</strong></a></p>

> 이 문서는 유지보수 기준인 [README.md](README.md)의 한국어 번역입니다.
> 명령, 경로, 식별자와 기계 판독 계약은 영문 원문을 기준으로 합니다.

Hearting은 지원되는 코딩 에이전트 런타임에서 조사, 계획, 구현, 검증
작업을 일관되게 마무리합니다. 이는 **특정 단일 런타임을 위한 설정이
아닙니다**. 공유 계약은 한 번만 정의하고, 각 런타임이 실제로 발견하는
네이티브 Skill, Agent, hook, mode, command 표면에만 투영합니다.

```text
"로그인 API를 구현하고 테스트한 뒤 변경 보고서까지 남겨줘."
                                  ↓
       계획 → 실행 → 테스트 → 보고 + 지속 가능한 근거
                                  ↓
            그 모든 단계를 `fleet`에서 실시간으로 확인
```

## Hearting을 쓰는 이유

- **작업 사이클 전체를 닫습니다.** 조사, 명세, 계획, 구현, 테스트, 보고서와
  지속 가능한 근거가 한 흐름으로 이어집니다.
- **세 런타임에서 하나의 계약을 유지합니다.** 공유 동작을 Claude Code,
  Codex, OpenCode가 실제로 발견하는 표면에 투영합니다.
- **필요한 노드에 필요한 모델만 씁니다.** 단계의 의미와 위험이 execution
  profile을 결정하며, route 컴파일 시점에 봉인됩니다. 뒤에 붙이는 model
  플래그로 바꿀 수 없습니다.
- **실행 중인 작업을 눈으로 봅니다.** `fleet`은 모든 런타임의 대화형
  session과 dispatch된 worker를 하나의 라이브 트리로 보여줍니다.
- **무엇이 실행 중인지 확인합니다.** 활성 release 또는 checkout,
  revision, freshness, duplicate와 필요한 session action을 검사합니다.
- **한 번 활성화하면 harness 전체입니다.** 모든 런타임이 manifest가 정의한
  전체 capability 집합을 그대로 발견합니다 — 나뉜 부분집합이나 별도 setup은
  없습니다.
- **결정을 안전하게 이어갑니다.** Durable memory와 실행 가능한 guard가 기존
  convention을 보존하고 spec, artifact, git, projection 경계를 확인합니다.

## 빠른 시작

### 요구 사항

- Python 3.10 이상
- `curl` 또는 `wget`
- 활성화하려는 각 런타임의 CLI

### 설치

```bash
curl -fsSL https://github.com/dmlguq456/hearting/releases/latest/download/install.sh | sh
~/.local/bin/hearting runtime doctor --runtime all --strict
```

installer와 distribution logic은 동일한 immutable Release tag에서 오며, 그 exact
tag의 archive를 SHA-256으로 확인한 뒤 설치합니다. 세 런타임에 전체 capability
집합의 불변 packaged bundle을 활성화하고, OS가 지원하면
user-level 일일 update 확인도 등록합니다. Runtime credential, session, log,
database는 건드리지 않습니다.

Codex가 설치되어 있으면 같은 transaction이 복구 가능한
`$CODEX_HOME/.harness/bin/codex` 보호 ingress도 설치합니다. 평범하게 `codex`, `codex resume`,
`codex fork`를 실행하면 자동으로 harness-managed App Server로 들어가고,
`codex exec`, plugin 관리, login 등 비대화형 명령은 기록해 둔 실제 CLI로
그대로 전달됩니다. Update는 launcher를 복구하며 `hearting uninstall codex`는
설치 전 command binding을 정확히 복원합니다. 프로필 PATH 변경은 명시적인
`--profile-policy manage` 권한이 필요하며 현재 터미널은 바꾸지 않습니다.
`legacy-inplace-v1`은 명시적인 제한 모드에서만 가능합니다. Codex가 아직 없으면 이 단계만
skip으로 보고하고 나중에 runtime refresh로 적용할 수 있습니다.

`~/.local/bin`을 `PATH`에 넣은 뒤에는 다음처럼 관리합니다.

```bash
hearting runtime status --runtime all
hearting update
hearting auto-update status
hearting runtime doctor --runtime all --strict

fleet          # 라이브 cross-harness 대시보드, 단순 스냅숏은 --once
```

installer는 `~/.local/bin`에 `hearting`과 `harness`를 함께 놓습니다. 같은
launcher의 두 이름이라 예전 이름으로 쓰던 것이 그대로 동작합니다. `fleet`
launcher와 공용 `compute-hosts` operator launcher도 함께 설치합니다. 전체 화면
라이브 뷰에는 `curses`가 필요하지만 `fleet --once`와 `fleet --json`은 필요
없어서, Python이 도는 곳이면 스크립팅과 스냅숏이 그대로 동작합니다.

`compute-hosts`는 host를 명시적으로 선택하며 사용자가 소유한
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/compute-hosts.yaml`을 읽습니다.
설치와 update는 이 inventory를 만들거나 바꾸지 않고, shell startup 파일도
수정하지 않습니다. 자동 GPU scheduler나 remote agent dispatch는 없습니다.
외부 PATH 파일과 symlink는 보존되며, 전체 uninstall만 소유한 공용 link를
제거하고 부분 runtime uninstall은 이를 유지합니다.

`hearting update`는 새 release를 staging에서 검증한 뒤 active pointer를
전환하고 실패하면 이전 release로 rollback합니다. 이미 열린 agent session이
자동으로 지침을 다시 읽는 것은 아니므로 update 뒤 `runtime status`에서
재호출, 새 session 또는 restart 필요 여부를 확인하세요.

Checksum sidecar는 전송 또는 asset 손상을 탐지합니다. Publisher 진위의 신뢰
경계는 이 저장소의 GitHub Release와 HTTPS account이며 독립 서명은 아닙니다.

Version 고정 또는 자동 확인 제외:

```bash
curl -fsSL https://github.com/dmlguq456/hearting/releases/download/v2.0.0/install.sh | sh -s -- --no-auto-update
```

## 자연어로 사용하기

명령 이름을 외울 필요가 없습니다. 원하는 결과와 제약을 평소 사용하는
소통 언어로 설명하세요. 런타임 네이티브 Skill이 관련 파이프라인을 선택하고,
사용자향 출력은 이 README의 언어를 물려받지 않고 대화, 대상 독자 또는
산출물 언어를 따릅니다.

> “이 저장소를 분석하고 다음 기능을 위한 PRD를 만들어줘.”

> “로그인 API를 구현하고 테스트한 뒤 변경 보고서까지 남겨줘.”

> “논문과 실험 코드를 검토하고 재현 계획을 만들어줘.”

> “현재 화면을 렌더링하고 디자인을 다듬은 뒤 개발 handoff를 만들어줘.”

> “이전 결정을 찾아서 이 프로젝트의 기존 naming convention을 적용해줘.”

전체 진입점은 [capabilities/README.md](capabilities/README.md), portable role
모델은 [roles/README.md](roles/README.md)를 참고하세요.

## 함대 전체를 한눈에

`fleet`은 dispatcher가 기록하는 바로 그 attempt registry 위의 라이브 뷰입니다.
세 런타임의 대화형 session과 dispatch된 worker를 **하나의 트리**로 보여줍니다 —
대시보드 세 개를 서로 맞춰볼 필요가 없습니다.

<p align="center">
  <img src="docs/fleet.svg" alt="fleet — a live cross-harness view. An owner session dispatches execute, impl-review and failure-mode workers at depth two, each with its own sealed model profile and context gauge." width="100%">
</p>

가운데를 위에서 아래로 훑으면 dispatch 계약이 한 그림에 들어옵니다. 밝은 행이
depth 0의 메인 session입니다. 그 아래 레일에 붙은 것이 depth 1로 분사된 owner —
봉인된 route와 `mp:deep`, 그리고 `execute`에 체크가 찍힌 stage 파이프라인을
달고 있습니다. 다시 그 아래, 한 단계 더 어두운 것들이 그 owner가 띄운 depth 2
stage worker입니다. `mp:light`로 도는 `code-execute`는 아직 실행 중,
`impl-review`는 다른 harness에서 완료, `failure-mode`는 입력 대기로 blocked.
**depth가 밝기로 읽히기 때문에** 연결선 하나 없이도 3단 트리가 그대로 보입니다.

각 행은 어느 harness에서 도는지, 그 노드의 route가 봉인한 model profile이
무엇인지, context가 얼마나 남았는지, 얼마나 오래 붙잡고 있는지를 함께 보여줍니다.
멈춘 worker가 "조용해진 session"이 아니라 **worker로** 드러나고, 고아 행도
버리지 않고 표시합니다.

전체 화면 뷰에는 `curses`가 필요합니다. `fleet --once`는 단순 스냅숏을,
`fleet --json`은 같은 상태를 기계 판독 형태로 내보내므로 Python이 도는 곳이면
어디서든 동작합니다 — Git Bash 위의 네이티브 Windows 포함.

## 요청을 실제로 처리하는 것들

문장 하나를 넣으면 라우팅된 파이프라인이 나옵니다. 경로 선택, 모델 선택,
검증 강도, 근거 기록은 harness의 일이며, 각 결정은 즉흥적으로 정해지지 않고
기록으로 남습니다.

| 계층 | 하는 일 |
|---|---|
| **라우팅된 capability** | 26개 capability 위의 12개 entry router. 실질적 작업 전에 에이전트가 task, reason, route, scope, completion 다섯 필드의 route card를 제시합니다. 명령 이름과 플래그를 외우는 대신 채워진 제안을 승인합니다. |
| **Intensity 사다리** | `direct → quick → standard → strong → thorough → adversarial`가 stage graph와 dispatch depth를 결정합니다. 검증 강도는 별도 축이 아니라 intensity에서 파생되며, 토큰 압박으로 낮출 수 없습니다. |
| **봉인된 cross-harness dispatch** | `standard+`에서 각 stage는 role, model profile, 서로 겹치지 않는 write scope가 봉인된 별도 session입니다. 2~4개 leg의 병렬 그룹은 정확히 한 번의 트랜잭션으로 시작하며, 용량이 모자라면 registry 행도 모델 프로세스도 0개입니다. leg는 기본적으로 서로 다른 harness 계열에 분산됩니다. dispatch depth 3은 금지입니다. |
| **노드별 모델 티어** | `deep`, `balanced-deep`, `light`, `mini`는 서로 다른 동작점이며 컴파일 시점에 노드마다 봉인됩니다. 공유 계약은 vendor 모델명을 쓰지 않고, 어댑터가 실제 모델로 매핑합니다. |
| **Fleet** | 같은 attempt registry 위의 라이브 대시보드. 세 런타임의 대화형 session과 dispatch된 worker를 하나의 트리에 상태, harness, 봉인된 profile, context 게이지, 토큰 집계와 함께 보여줍니다. 고아 행도 버리지 않고 드러냅니다. |
| **선의가 아닌 guard** | hook 39개, 그중 5개는 hard block. write scope, spec 읽기, artifact root, git 상태, memory 경로는 리뷰가 아니라 도구 호출 **전에** 거부됩니다. 해당 작업 디렉터리에 컴파일된 route가 없는 소스 편집은 hotfix라도 거부됩니다. |
| **고정된 artifact 체계** | 코드는 `research / analyze-project → spec → plans`, 문서는 `research → draft → refine`. 프로젝트마다 `.agent_reports/` 루트 하나, artifact마다 소유 capability 하나이며 spec 개정은 이전 버전을 스냅숏으로 남깁니다. |
| **단일 memory 저장소** | 모든 session, 프로젝트, 런타임을 가로지르는 SQLite + FTS5. 바뀐 결정은 삭제가 아니라 supersede되고, handoff는 명시적으로 소비될 때까지 pending으로 남습니다. |

leg가 의도한 표면에서 시작하지 못하면 검사된 사슬
`same-harness-headless → cross-harness-headless → native-subagent → inline`을
따라 강등되며, route id, write scope, completion gate, attempt identity는
그대로 유지됩니다. 강등은 실패 분류와 함께 기록되고 절대 조용히 넘어가지
않습니다.

같은 구조를 그림으로 보려면
[랜딩 페이지와 에이전트 맵](https://dmlguq456.github.io/hearting/)을
참고하세요.

## 작동 방식

```text
                       harness-manifest.json
                        capability · role
                               │
             ┌─────────────────┼─────────────────┐
             │                 │                 │
      Claude Code native   Codex native    OpenCode native
      skills / agents      skills / agents  skills / agents
      hooks / commands     hooks / modes    commands / plugins
             └─────────────────┼─────────────────┘
                               │
              activate · status · refresh · doctor
```

| 계층 | 책임 |
|---|---|
| `core/` | Workflow, artifact, assurance, memory, git/worktree 계약 |
| `harness-manifest.json` | Capability, role, mode의 canonical machine contract |
| `capabilities/`, `roles/` | 사람이 읽는 portable behavior source |
| `adapters/` | 각 런타임의 네이티브 projection과 bridge |
| `tools/install/` | 런타임 소유 상태를 건드리지 않는 activation lifecycle |
| `.agent_reports/` | Spec, plan, test evidence, handoff를 위한 project artifact |

Managed release가 일반 사용자 기본값입니다. `linked`는 maintainer mode로
남습니다. Checkout 변경은 discovery path에 즉시 나타나며 release updater는
그 checkout을 fetch, pull, repoint하지 않습니다. 파일 노출과 지침 재로딩은
서로 다른 문제이므로 `runtime status`는 각 런타임에 재호출, 새 session 또는
restart 중 무엇이 필요한지 `session_action`으로 알려줍니다.

## 런타임 지원

| 런타임 | `linked` projection | `packaged` projection |
|---|---|---|
| Claude Code | Skill, Agent, command, hook | 동일한 네이티브 표면의 불변 bundle |
| Codex | Skill, custom agent, mode, hook | 동일한 네이티브 표면의 불변 bundle |
| OpenCode | Skill, Agent, command, local guard plugin | 동일한 네이티브 표면의 불변 bundle |

런타임 차이는 숨기지 않고 보고합니다. installer는 지원하지 않는 표면을
이유와 함께 `SKIP`으로 표시하며 credential, session, database, log, 외부
cache는 소유하지 않습니다. 자세한 매핑은
[INSTALL_LAYOUT.md](INSTALL_LAYOUT.md)를 참고하세요.

## Harness 개발

Maintainer는 managed release 대신 live checkout을 사용할 수 있습니다.

```bash
git clone https://github.com/dmlguq456/hearting.git ~/hearting
cd ~/hearting
./tools/install/harness.sh runtime activate --runtime all --mode linked
```

Clone마다 한 번, 저장소가 제공하는 Git hook을 켭니다.

```bash
git config core.hooksPath tools/git-hooks
```

`pre-push`는 CI가 돌리는 것과 동일한 생성물 검사를 수행하고 실패할 push를
거부합니다. 커밋이 공개되고 release workflow가 태그를 붙이기 전에 drift를
잡습니다. `git push --no-verify`로 우회할 수 있습니다.

공유 정의를 변경한 후에는 모든 생성 projection을 갱신하고 drift를
검사합니다.

```bash
python3 tools/generate.py
python3 tools/generate.py --check

./tools/generated-projections.test.sh
./tools/install/projection-completeness.test.sh
./tools/install/runtime-activation.test.sh
./tools/skill-conformance/check.sh
./tools/check-adaptation-boundary.sh
adapters/codex/bin/preflight.sh doctor
```

`tools/generate.py`는 모든 core projection의 단일 build/check 진입점입니다 —
런타임 어댑터 metadata, 운영자 hub, 공개 랜딩 표면까지 포함합니다. lifecycle
테스트는 clean 작업 트리에서 실행하세요. packaged activation은 dirty
저장소에서 bundle 생성을 의도적으로 거부합니다.

Marketplace bundle 생성은 이 경로에 포함되지 않습니다. 루트 README의 가치
제안과 설명은 사람이 관리하며, machine contract와 runtime projection만
자동으로 생성됩니다.

## 문서

| 목적 | 문서 |
|---|---|
| 전체 사용 안내 | [MANUAL.md](MANUAL.md) |
| 설치와 런타임 projection | [INSTALL_LAYOUT.md](INSTALL_LAYOUT.md) |
| 릴리스 기준과 SemVer 자동화 | [RELEASE_POLICY.md](RELEASE_POLICY.md) |
| Capability와 role | [capabilities/README.md](capabilities/README.md), [roles/README.md](roles/README.md), [roles/MODES.md](roles/MODES.md) |
| Routing과 artifact | [core/WORKFLOW.md](core/WORKFLOW.md), [core/CONVENTIONS.md](core/CONVENTIONS.md) |
| Git, worktree, dispatch | [core/OPERATIONS.md](core/OPERATIONS.md) |
| Memory와 recall | [core/MEMORY.md](core/MEMORY.md) |
| Hook과 design principle | [core/HOOKS.md](core/HOOKS.md), [core/DESIGN_PRINCIPLES.md](core/DESIGN_PRINCIPLES.md) |

## 라이선스

[MIT](LICENSE)
