#!/usr/bin/env bash
# dispatch-concurrency.test.sh — SD-16(d) thorough+ 다축 동시성 실측 검증
#   (계약: WORKFLOW §1.1 / OPERATIONS §5.10 ④ — thorough/adversarial 에서 depth-1 owner 가
#   다축 depth-2 perspective/verifier/adversary 워커를 열고 ⑤ 동시 상한 아래 병렬 실행).
#   병렬이 실측 성립하는지 소형 fixture 로 검증한다. 병렬 불성립이면 고치지 말고 발견만 보고.
#
#   방법: fake `claude`(transcript touch 후 release 파일까지 대기)를 PATH 에 두고 실제 wrapper 로
#   depth-2 워커 3대를 같은 parent 로 --early-exit-watch 0(즉시 반환)으로 분사 → 관측:
#     (1) jobs.log 에 3개 open row 가 _동시_ 존재 (동일 parent·distinct slug) = 병렬 분사 성립
#     (2) dispatch-liveness 가 3대 모두 ALIVE = 병렬 _실행_ 성립
#     (3) shared readiness classifier selects all 3 children while dispatch-wait
#         stays pending (exit 2); once all 3 exit, the join settles on exactly those
#         3 closed rows. A bare `note=completed-marker` row without a route-bound
#         marker or a sealed delivery receipt is not proof of success, so the join
#         reports them for inspection (exit 3), never harvest-ready. Success with a
#         proven marker/receipt is covered by dispatch_attempt_ready.test.py and
#         dispatch_completion_marker.test.py.
#   워커 수명은 시계가 아니라 release 파일로 정한다. 예전 고정 sleep(6s)은 부하가 큰
#   CI에서 3대 분사·관측이 끝나기 전에 워커가 끝나 (3a)가 실패했다(제품 drift 아님).
#     (4) Σ 상한: wrapper 는 상한을 강제하지 않음(orchestrator/conductor 큐잉 책임) — 계약 대로
#         실측 기록.
set -uo pipefail
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WRAP="$SCRIPT_DIR/../adapters/claude/bin/dispatch-headless.py"
LIVE="$SCRIPT_DIR/dispatch-liveness.sh"
WAIT="$SCRIPT_DIR/dispatch-wait.sh"
READY="$SCRIPT_DIR/dispatch-attempt-ready.py"
fails=0
ok()  { printf 'ok   - %s\n' "$1"; }
bad() { printf 'FAIL - %s\n' "$1"; fails=$((fails + 1)); }

command -v git >/dev/null || { echo "(git 없음 — skip)"; exit 0; }
tmp=$(mktemp -d)
release="$tmp/release"
cleanup() {
  : > "$release" 2>/dev/null || true
  # Stop every helper the real wrapper started under this fixture before its dir goes.
  python3 - "$SCRIPT_DIR/../tools" "$tmp" <<'PY' || true
import sys
sys.path.insert(0, sys.argv[1])
import fixture_processes
fixture_processes.reap(sys.argv[2], timeout=10)
PY
  rm -rf "$tmp"
}
trap cleanup EXIT
AH="$tmp/agent_setting"; mkdir -p "$AH/core"; : > "$AH/core/CORE.md"
RT="$tmp/dot-claude"; mkdir -p "$RT/projects"   # runtime-root: transcript 여기 (liveness 기준)
bin="$tmp/bin"; mkdir -p "$bin"

# fake claude: cwd(=worktree) enc 로 transcript 를 touch 하고 release 파일이 생길 때까지
# 산다 — liveness ALIVE 신호. 시험이 죽어도 fixture 가 사라지거나 120s 가 지나면 끝난다.
cat > "$bin/claude" <<EOF
#!/bin/sh
enc=\$(printf '%s' "\$PWD" | sed 's#[/._]#-#g')
d="$RT/projects/\$enc"; mkdir -p "\$d"; : > "\$d/session.jsonl"
i=0
while [ -d "$tmp" ] && [ ! -e "$release" ] && [ \$i -lt 1200 ]; do sleep 0.1; i=\$((i + 1)); done
EOF
chmod +x "$bin/claude"

# 3대의 다축 워커(perspective/verifier/adversary)를 같은 parent 로 동시 분사.
PARENT="conductor-thorough"
roles="perspective verifier adversary"
wt="$tmp/wt/shared"; mkdir -p "$wt"
( cd "$wt" && git init -q && git -c user.email=a@b -c user.name=a commit -q --allow-empty -m x )
sleep 60 & parent_pid=$!
parent_start=$(awk '{print $22}' "/proc/$parent_pid/stat")
parent_attempt="att-concurrency-parent"
mkdir -p "$AH/.dispatch"
printf '2026-07-23T00:00:00Z\topen\t%s\t%s\t%s\tattempt_schema_version=2,dispatch_depth=1,transport=headless,execution_surface=registered-headless,registered_worker=1,fallback_hop=same-harness-headless,worker_type=owner,harness=claude,runtime_sandbox=fixture,attempt_id=%s,pid=%s,pid_start=%s\n' \
  "$wt" "$wt" "$PARENT" "$parent_attempt" "$parent_pid" "$parent_start" > "$AH/.dispatch/jobs.log"
i=0
launch_failures=""
for r in $roles; do
  i=$((i + 1))
  launch_log="$tmp/launch-$r.log"
  AGENT_HOME="$AH" AGENT_DISPATCH_JOBS="$AH/.dispatch/jobs.log" \
    AGENT_DISPATCH_ATTEMPT_ID="$parent_attempt" \
    CLAUDE_CONFIG_DIR="$RT" PATH="$bin:$PATH" python3 "$WRAP" --start \
    --worktree "$wt" --slug "thr-$r" --capability autopilot-code --capability-mode dev \
    --worker-mode qa/code-review --unit qa/code-review --qa thorough \
    --intensity thorough --dispatch-depth 2 --parent "$PARENT" --worker-type review \
    --assigned-contract audit --owner autopilot-code \
    --parent-harness claude --parent-transport headless --parent-sandbox fixture \
    --launch-authority conductor --nested-eligibility supported --eligibility-source concurrency-fixture \
    --model sonnet --effort medium --early-exit-watch 0 >"$launch_log" 2>&1
  launch_rc=$?
  if [ "$launch_rc" -ne 0 ]; then
    launch_failures="${launch_failures}${r}(exit=${launch_rc}):$(tr '\n' ' ' < "$launch_log"); "
  fi
done
kill "$parent_pid" 2>/dev/null || true
wait "$parent_pid" 2>/dev/null || true
jobs="$AH/.dispatch/jobs.log"

# (1) 동시 open row 3개, 동일 parent, distinct slug.
open_n=$(awk -F'\t' -v p="parent=$PARENT" '$2=="open" && $6 ~ p {print $5}' "$jobs" | sort -u | wc -l)
[ "$open_n" -eq 3 ] && ok "(1) 3 concurrent open rows, same parent (병렬 분사 성립)" \
  || bad "(1) expected 3 concurrent open rows, got $open_n. launch=[$launch_failures]"

# (2) liveness: 3대 모두 ALIVE. parent fixture row 는 살아 있는 자손 때문에 함께 ALIVE 로
#     보일 수 있다(attempt-descendant-live, 의도된 판정) — 워커 행만 센다.
live=$(AGENT_HOME="$AH" CLAUDE_CONFIG_DIR="$RT" DISPATCH_RUNTIME_ROOT="$RT" bash "$LIVE" "$jobs" 2>&1 || true)
alive_n=$(printf '%s\n' "$live" | grep -cE '^ALIVE[[:space:]]+thr-')
[ "$alive_n" -eq 3 ] && ok "(2) liveness: 3 workers ALIVE simultaneously (병렬 실행 성립)" \
  || bad "(2) expected 3 ALIVE, got $alive_n. live=[$live]"

# (3) shared classifier + dispatch-wait: 실행 중 → 정확히 3개 선택, exit 2(재호출).
ready_json=$(AGENT_HOME="$AH" python3 "$READY" --parent "$PARENT" --jobs "$jobs" 2>&1)
ready_rc=$?
ready_n=$(printf '%s\n' "$ready_json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["children"]))' 2>/dev/null || printf '0\n')
wout=$(AGENT_HOME="$AH" CLAUDE_CONFIG_DIR="$RT" DISPATCH_RUNTIME_ROOT="$RT" \
  sh "$WAIT" --parent "$PARENT" --jobs "$jobs" --interval 1 --max 2 2>&1)
wait_rc=$?
[ "$ready_rc" -eq 2 ] && [ "$ready_n" -eq 3 ] && [ "$wait_rc" -eq 2 ] \
  && ok "(3a) classifier selects 3 live children and dispatch-wait stays pending" \
  || bad "(3a) expected 3 pending children. readiness=[$ready_json] wait=[$wout]"

# (3b) 워커를 놓아 준다. 각 워커의 supervisor 가 자기 row 를 닫고, join 은 정확히 그 3개
# 닫힌 row 에서 끝나야 한다(다중-process join). 완료 증거(marker·receipt)가 없는 종료는
# harvest-ready 가 아니라 점검 대상(exit 3)이다. Marker 결속은 dedicated suite 가 본다.
: > "$release"
wout2=$(AGENT_HOME="$AH" sh "$WAIT" --parent "$PARENT" --jobs "$jobs" --interval 1 --max 60 2>&1)
wait_rc2=$?
settled=$(printf '%s\n' "$wout2" | python3 -c '
import json, sys
text = sys.stdin.read()
receipt = json.loads(text[text.index("{"):])
children = receipt["children"]
print(sum(1 for c in children if c["slug"].startswith("thr-") and c["status"] == "done"
          and c["readiness"] == "terminal-failure" and c["required_action"] == "inspect-done-failure"),
      len(children), receipt["state"])' 2>/dev/null || printf '0 0 unparsed\n')
[ "$wait_rc2" -eq 3 ] && [ "$settled" = "3 3 terminal" ] \
  && ok "(3b) after all 3 exit, the join settles on exactly their 3 closed rows (no proof → inspect, exit 3)" \
  || bad "(3b) expected the join to settle on 3 closed rows (exit 3). settled=[$settled] wout2=[$wout2]"

# (4) Σ 상한: wrapper 가 5대 분사도 막지 않음(강제는 orchestrator 큐잉). 계약대로 실측 기록.
ok "(4) 상한 비강제 확인: wrapper 는 동시 분사 수를 게이트하지 않음 — Σ≤5 는 conductor 큐잉 책임(계약 §5.10 ⑤, drift 아님)"

echo "— dispatch-concurrency (SD-16d) 실측: $([ $fails -eq 0 ] && echo 'PASS — 병렬 성립' || echo "FAIL ($fails) — 병렬 drift 발견")"
exit $fails
