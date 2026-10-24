# Lab 실행 저장소와 명시적 데이터 접근

`part_catalog` row에 compute 실행 필요를 선언한 origin stage가 있는 route의 owner를 시작하거나
재개하면 초기화된 compute-hosts inventory의 정확한 `run_root`가 기존
`execution_access_v1` 요청에 포함된다. 이 요구는 `capabilities/topologies.json`의
`part_catalog`에서 origin stage의 row 한 곳에 선언되며 sealed node의 stage identity로 조회한다. borrowed
stage도 같은 선언을 사용한다. Inventory는
`COMPUTE_HOSTS_CONFIG`, 또는 `${XDG_CONFIG_HOME:-~/.config}/hearting/compute-hosts.yaml`에서
기존 loader로 읽는다. GPU 작업과 compute-hosts resource 실행의 로그·PID 저장소를
owner의 정상 접근 범위 안에 두기 위한 기본값이다. compute stage가 없는 일반
code owner와 frame에는 이 기본값을 추가하지 않는다. Resource child는 실제 부모
grant 안에서만 접근한다.

GPU 실행 범위가 있는 lab owner는 inventory 폴더를 읽기 전용으로 받고, 별도 명시 요청이
없으면 compute-hosts SSH 네트워크 필요도 같은 요청에 포함된다. OpenCode 작업자 홈은
XDG 경로를 격리하기 전에 기존 `COMPUTE_HOSTS_CONFIG` 입력에 사용자 inventory의 정확한
경로를 유지한다. 파일을 복사하거나 사용자 설정을 바꾸지 않는다. 기존 준비 파일이 있는
route도 정상 start·correction에서 기존 도출 root를 유지한 채 이 실행 기본값을 준비한다.
비실행 child에는 이 자동 요청을 더하지 않으며 child의 실제 요청은 부모 grant를 넘을 수 없다.
Claude·OpenCode의 `network_allowed`는 논리적 허용이며 OS 네트워크 격리 보장이 아니다.

Inventory가 없거나 주석 템플릿 상태라면 경로를 추측하지 않는다. 잘못된 inventory,
홈 전체 같은 넓은 root, symlink로 넓은 root에 도달하는 요청은 기존 검증에서 거부한다.
준비 과정은 inventory를 읽고 요청 파일만 만들며 실제 `run_root`를 만들거나 작업을
시작하지 않는다.

## 외부 데이터 경로 지정

수동 요청 파일이 없으면 start·resume이 봉인된 작업문에서 요청을 만든다
(`execution_access.derive_task_access`).

- 읽기 루트: 작업문에 나온 절대경로 중 실제로 있는 폴더. 파일이면 그 폴더다. 작업문에서 지정한 JSON 목록의 `roots`에 있는 `artifact_root_path` 또는 절대경로 문자열도 읽기 입력이다. 설치된 앱의 `ops/` 입력 목록과 하팅 코드·문서도 읽을 수 있다. 읽기는 최대 256개, 쓰기는 기존 최대 16개다. 입력 목록은 64 KiB, 도출한 요청과 binding은 4 MiB까지 안전하게 읽고 저장한다. 소스 폴더의 링크가 설치 소스 영역 밖으로 빠지면 자동 허용하지 않는다. 이미 쓰기를 승인한 개발 체크아웃에는 읽기 전용 제한을 덧씌우지 않는다.
- 쓰기 루트: 시작 카드의 `범위:`(`Scope:`) 칸에서 쓰기 표현(쓰기·저장·수정·생성·write·save 등)이 있고
  읽기·제외 표현은 없는 구절의 기존 폴더만, owner에만 준다. 경로 뒤 괄호나 쉼표 뒤의 경로 없는 구절
  (`(제외)`, `(읽기 전용)`, `, 읽기만`)은 바로 앞 경로에 붙는다. 쓰기 표현이 없거나 읽기·쓰기가 섞인
  경로는 읽기, 제외 표현(제외·금지·않·off-limits 등)이 처음 나온 경로부터는 읽기에서도 뺀다. 해석이 애매하면
  권한이 작은 쪽으로 정한다. 작업문의 다른 곳에 나온 경로는 쓰기 루트가 되지 않는다.
- 빼는 것: credentials·키·`~/.ssh` 같은 홈의 숨김 폴더·사용자/런타임 설정·시스템 영역, 없는 경로,
  너무 넓은 경로, worktree·artifact root처럼 이미 쓸 수 있는 곳.
- 근거: 루트마다 요청의 `justification`에 출처(범위 칸/작업문, 몇째 줄, 그렇게 정한 표현, 그 구절)가 남는다.
  준비된 `binding.json`의 `derivation`에는 받은 루트와 뺀 경로·이유가 남는다.
- 같은 route 노드는 처음 준비한 결과를 resume에서도 그대로 쓴다. 이전 설치본이 빠뜨린 읽기 입력은 정상 start에서 한 번 보완한다. 과거 요청과 읽기·쓰기 판정을 먼저 보존한 뒤 남은 읽기 용량만 채우며 이후에는 보완한 목록을 재사용한다. 도출한 루트가 검증을 통과하지 못하면
  그 루트들만 빼고 시작하며, 도출 때문에 시작이 거절되지는 않는다.

수동 요청 파일은 계속 받으며, 있으면 도출보다 우선한다. 다음 경로는 예시이며
실제 승인된 경로로 바꿔야 한다.

```json
{
  "schema_version": 1,
  "writable_roots": ["/data/project/train-valid"],
  "read_roots": [],
  "network": {"required": false, "reason": "", "hosts": []},
  "enforcement_required": "os-sandbox",
  "justification": {
    "/data/project/train-valid": "사용자가 승인한 데이터 처리 및 결과 저장 경로"
  }
}
```

파일을 저장한 뒤 기존 start 또는 resume 명령을 실행하는 세션에 전달한다.

```sh
export AGENT_DISPATCH_EXECUTION_ACCESS_FILE=/absolute/path/lab-access.json
# 기존 preflight.sh compose --start ... 또는 그 receipt의 resume_command 실행
```

Lab 준비는 `load_request`로 이 파일을 검증하고 inventory의 `run_root`만 더한 사본을
기존 dispatch state의 route별 `execution-access` 폴더에 저장한다. 원본 파일은 수정하지
않는다. 시작·재개에 같은 입력을 유지하면 같은 준비 파일을 재사용한다.

열린 route에서 부모가 요청을 바꾸려면 새 파일을 같은 환경 변수로 주고 그 route의
`start`(또는 `correct`)를 실행한다. route의 현 부모일 때만 route 옆 변경 기록에 `access`
1행이 남고(누가 줬는지와 출처 포함, 첫 도출 요청은 `derived` 행), 다음 owner·교체 owner와
그 자식은 가장 최근 행의 요청으로 시작한다. lab owner는 그 요청에 `run_root`를 더한 별도
준비 파일을 쓴다. 다른 세션의 요청은 기록되지 않는다. 명시적 JSON은 preview ROOTS 입력보다
우선한다. 낮은 수준의 직접 dispatch에서도 기존 `--execution-access-file <JSON>`을
쓸 수 있다. 이 직접 경로에서는 필요한 모든 root를 해당 JSON에 명시한다.

Owner 전달과 child 전달은 기존 환경 변수·CLI·effective-grant 경로를 사용한다.
Child 요청이 부모의 실제 파일 또는 네트워크 범위를 넘으면 모델을 시작하기 전에
거부된다. 별도의 승인 절차나 전역 permission 설정은 필요하지 않다.

## 적용 수준과 현재 한계

### GPU compute 부품의 실행 sandbox

카탈로그 부품이 `execution_needs: compute-hosts`를 선언하면 Codex owner와 그
compute 부품에 기존 `danger-full-access`를 선택한다. 이 범위에는 lab의 smoke,
full-run, run-verify뿐 아니라 eval/resume과 code route가 빌려 쓴 compute 부품,
extra stage, parallel leg도 포함된다. 카탈로그 선언이 compute 판단의 기준이다.
기존 명시 `resource_class=gpu`와 resource-runner의 `gpu` promotion signal도
호환 선택으로 유지하며 작업 설명의 자연어는 해석하지 않는다. 일반 code, frame,
계획·metrics·report 같은 비-compute child는 기본값을 유지한다.

새 route의 한 선택은 prospective readiness, sealed `codex_execution_sandbox`,
실제 exec/App Server 실행, GPU lease admission에 함께 쓰인다. 이미 봉인된 route는
seal의 GPU 범위와 sandbox를 유지한다. seal이 없는 역사 route는 기존 owner sandbox
선택을 유지하며, checked Codex parent tuple만으로 sandbox를 바꾸거나 compute 접근과
GPU lease를 소급해서 만들지 않는다. 같은
cycle의 lab 또는 code suffix owner는 완료 prefix의 seal을 이어받으며 그 아래
비-compute child는 기본값을 유지한다. Caller의 기존 `--sandbox`,
`CODEX_DISPATCH_SANDBOX`, `CODEX_DISPATCH_SANDBOX_FORCE` precedence는 보존하고
사용자 전역 설정은 수정하지 않는다.

Codex 0.160.0의 Linux sandbox에서 NVIDIA 장치가 숨겨지는 것을 실제 조회로
확인했다. `/dev` read로 해결되지 않았고 장치 write profile은 bwrap panic으로
종료됐다. 같은 설치의 full access에서는 `nvidia-smi -L` 조회가 성공했다.
이는 학습 성공이나 다른 외부 sandbox의 장치 지원을 보증하지 않는다.

이 선택에는 filesystem/network OS enforcement가 **없다**. 이 범위에서
`enforcement_required=any`인 요청만 논리적 root/child≤parent 경계로 수용하고
grade `none`, `file-enforcement-none`, `network-enforcement-none`을 기록한다.
Network 요청은 부모의 논리적 허용 범위 안에서 `granted-unenforced`로 남는다.
`os-sandbox` 필수 요청, read-only에서 쓰기 요청, 부모 밖 요청은 기존대로 거부한다.
Strict OS enforcement가 필요하면 기존 workspace 실행이나 compute-hosts resource
경로를 이용한다. Compose/explain/start와 launch receipt에는 실제 선택·적용 수준이
표시되며, 외부 sandbox와 관리된 runtime 제약은 계속 적용된다.

공식 문서도 full access가 파일·네트워크 경계를 제거한다고 설명한다.
[Codex sandbox](https://learn.chatgpt.com/docs/sandboxing),
[Codex permissions](https://learn.chatgpt.com/docs/permissions),
[Codex App Server](https://learn.chatgpt.com/docs/app-server)

Codex의 `workspace-write`에서는 writable root를 OS sandbox에 적용한다. 로컬
`codex-cli 0.160.0`의 exec는 `--add-dir`, Hearting App Server runner는
`--writable-root`로 같은 검증 결과를 전달한다. 일반 code 실행의 기존 기본 경로와
request가 없는 builder argv는 유지한다. 공식 Codex 설정도 파일 경로별 permission과
workspace 확장을 지원한다. [Codex permissions 설정](https://learn.chatgpt.com/docs/config-file/config-reference)

`read_roots`는 세 하네스 모두 읽기 전용으로 투영한다(어댑터 선언표 `access`). Codex sandbox는
어디든 읽고 writable root에만 쓰며, Claude는 루트마다 `--add-dir`와 Edit 거부 규칙,
OpenCode는 `external_directory` 읽기 전용이다. 실제 보장 수준은 grant의 `read_enforcement`에 남는다.

SSH 등 네트워크가 필요하면 기존 `network.required=true`와 `reason`을 명시하고 기존
owner 네트워크 정책을 따른다. `network.hosts`는 현재 호스트별 격리를 보장하지 않는다.
`any`에서는 미적용 상태를 보고하고, 호스트 제한과 `os-sandbox`를 함께 요구하면 거부한다.
Codex 공식 문서 역시 호스트별 네트워크 규칙을 적용하려면 proxy enforcement가 필요하다고
설명한다. [Codex 네트워크 설정](https://learn.chatgpt.com/docs/config-file/config-reference)

Claude·OpenCode에서는 같은 요청을 기존 tool-permission 수준으로 적용하며 OS sandbox와
동등하다고 보고하지 않는다. Claude의 additional directory 권한과 sandbox는 별도
설정이다. [Claude permissions](https://code.claude.com/docs/en/permissions)

## Continuation의 checked evidence 갱신

이미 실행 중인 부모의 sandbox가 원 tuple과 다르면 기존 continuation 명령에
선택적으로 `--dispatch-evidence /absolute/path/checked-evidence.json`을 전달할 수 있다.
Compile/compose와 같은 exact-worktree·parent identity 검증으로 evidence를 확인하고,
새 continuation의 tuple과 해당 fallback 선택에만 반영한다. 원 route와 완료 증거,
단위·승인 범위·cycle lineage는 보존한다. Override가 없으면 원 evidence를 그대로 쓴다.
이 입력은 sandbox를 바꾸거나 자동 probe·권한 승격을 수행하지 않는다. 새 정상 GPU
compose/start는 실행 선택과 probe/tuple을 함께 준비하므로 이 복구 입력이 필요 없다.
