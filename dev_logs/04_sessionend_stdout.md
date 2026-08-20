# (4) SessionEnd stdout 누출 — 구현 로그

## 변경
- `adapters/claude/settings.json:317` SessionEnd 3번째 hook: `exec python3 … sync --json` →
  `exec python3 … sync --json >/dev/null`. `exec` 유지, stderr는 리다이렉트하지 않아 그대로 보존.
- `adapters/codex/bin/preflight.sh:384`: `sync --json` → `sync --json >/dev/null`, 기존
  `|| sync_status=$?`는 그대로 두어 exit code 계약 보존.
- `adapters/opencode/bin/preflight.sh:813`: `sync --json` → `sync --json >/dev/null`이면서
  기존 `2>/dev/null`(stderr 억제)을 제거 — opencode만 stderr를 막고 stdout을 흘리는 정확히
  거꾸로 된 상태였다. `|| true`는 유지.

## MEM_DUMP_PUSH=1 미복원 확인 (A4)
`grep -rn "MEM_DUMP_PUSH=1" adapters/` — 세 hook 파일 어디에도 나타나지 않음. `ADAPTATION.md`
문서 서술에만 존재(경고성 deprecated alias 설명). 복원하지 않았다.

## --json/exit code 계약 확인 (A5)
세 곳 모두 `sync --json` 플래그 유지. codex는 `|| sync_status=$?`, opencode는 `|| true`로
exit code 소비 경로 불변. `exec`는 claude만 사용(원래도 그랬음) — 리다이렉션이 exit 상태를
바꾸지 않으므로 계약에 영향 없음.

## A6 — codex/opencode stdout이 실제 사용자 화면에 닿는가 (기록)

**codex**: `adapters/codex/hooks/run-hook.sh`가 `sessionend-lifecycle.py`를 `exec`하고, 그
파이썬 브릿지가 `preflight.sh session-end`를 호출한다. `ADAPTATION.md:284-292`에 따르면
Codex `SessionEnd` 브릿지의 사용자 대면 산출은 `hookSpecificOutput.additionalContext` 계열
구조화 출력이지, 훅 스크립트의 raw stdout을 터미널에 그대로 흘리는 방식이 아니다(Claude의
`sh -c '... exec ...'` 훅과 달리, Codex 훅 러너가 stdout을 감싼다). 즉 codex 경로는 애초에
이 누출 경로에 덜 노출돼 있었을 가능성이 높다 — 다만 코드로 직접 캡처 여부를 증명하지는
못했다(러너 구현은 Codex 자체 소유, 이 저장소 밖).

**opencode**: `ADAPTATION.md:218,281`에 따르면 `preflight session-end`는 `session.idle`
이벤트 훅이 **detached**로 띄우는 프로세스다. detached 프로세스의 stdout은 애초에 사용자의
foreground 화면과 분리돼 있을 가능성이 높다.

**결론(D3 그대로 적용)**: 두 런타임 모두 stdout이 실제로 화면에 닿는지 이 저장소 코드만으로
확정 증명은 못 했지만, plan §5.2/D3 방침대로 **결과와 무관하게 세 어댑터를 일관 처리**했다.
opencode의 `2>/dev/null` 제거는 그 자체로 진단 회귀(현재 방향이 기존에 거꾸로였음)를
바로잡으므로 독자적으로 정당하다.

## A7 — 실측 (checklist 항목, 실제 세션 종료 관측)
execute 노드는 대화형 세션이 아니므로 **직접 SessionEnd를 트리거해 화면을 관측할 수 없다**.
Claude 경로는 hook 명령 자체를 셸에서 재현해 stdout이 실제로 억제되는지 확인했다(아래).
codex/opencode는 실제 대화형 세션 종료가 필요해 이 노드에서 재현 불가 — **정직하게
"실측 불가"로 기록**한다. test 노드가 가능하면 보완하라.

### Claude hook 명령 직접 재현
```
$ sh -c 'exec python3 "$HOME/.claude/tools/memory/mem.py" sync --json >/dev/null'; echo "exit=$?"
```
실행 결과(worktree에서 실제로 실행함): stdout 무출력(터미널에 아무 것도 찍히지 않음),
`echo "exit=$?"` → `exit=1`. `mem.py sync --json`이 로컬 store 상태에 따라 exit 1(원격/지연
작업 남음 등)을 낸 것이며, 리다이렉션이 그 exit code를 그대로 승계했음을 실측으로 확인했다
— `exec cmd >/dev/null`은 cmd의 exit code를 셸의 exit code로 그대로 승계한다(POSIX 의미론).
