#!/usr/bin/env bash
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../../../tools/run-tests.py" --direct-run bash "$0" "$@"
# dispatch-headless.sd15.test.sh — SD-15 (OPERATIONS §5.10 ⑨) limit-사망 즉시 감지 회귀.
#   증명: (1) scan_death 패턴/ reset 추출 (2) launch 직후 조기 limit-death → row done,
#   note=dead-<reason>,reset=<x> 로 즉시 마감 + reset 캐시 기록 (3) clean 조기 exit(비-limit)
#   는 row 를 건드리지 않음 (open 유지).
set -uo pipefail
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WRAP="$SCRIPT_DIR/dispatch-headless.py"
fails=0
ok()  { printf 'ok   - %s\n' "$1"; }
bad() { printf 'FAIL - %s\n' "$1"; fails=$((fails + 1)); }

# --- Unit: scan_death ---
u=$(python3 - "$WRAP" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("dh", sys.argv[1])
dh = importlib.util.module_from_spec(spec); spec.loader.exec_module(dh)
cases = {
  "Selected model is at capacity": ("capacity", ""),
  "You've hit your session limit · resets 3pm": ("session-limit", "3pm"),
  "Error: usage limit reached, resets at 5:30pm": ("usage-limit", "5:30pm"),
  "invalid api key provided": ("auth", ""),
  "all good, work complete": None,
}
bad = 0
for text, want in cases.items():
    got = dh.scan_death(text)
    if got != want:
        print(f"MISMATCH {text!r}: got {got} want {want}"); bad += 1
prose = "The implementation discusses Selected model is at capacity handling " + ("x" * 180)
if dh.scan_anchored_death(prose) is not None:
    print("MISMATCH capacity report prose was treated as terminal"); bad += 1
short_prose = "Handled Selected model is at capacity errors."
if dh.scan_anchored_death(short_prose) is not None:
    print("MISMATCH short capacity report prose was treated as terminal"); bad += 1
if dh.scan_anchored_death('{"type":"error","message":"Selected model is at capacity"}') != ("capacity", ""):
    print("MISMATCH structured terminal capacity event was missed"); bad += 1
print("SCAN_OK" if bad == 0 else "SCAN_FAIL")
PY
)
echo "$u" | grep -q SCAN_OK && ok "scan_death patterns + reset extraction" || { bad "scan_death: $u"; }

command -v git >/dev/null || { echo "(git 없음 — skip launch cases)"; exit $fails; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
AH="$tmp/agent_setting"; mkdir -p "$AH/core"; : > "$AH/core/CORE.md"
bin="$tmp/bin"; mkdir -p "$bin"

launch() { # $1=fake-claude-body $2=slug $3=watch
  printf '%s' "$1" > "$bin/claude"; chmod +x "$bin/claude"
  wt="$tmp/wt/$2"; mkdir -p "$wt"
  ( cd "$wt" && git init -q && git -c user.email=a@b -c user.name=a commit -q --allow-empty -m x )
  sleep 60 & parent_pid=$!
  parent_start=$(awk '{print $22}' "/proc/$parent_pid/stat")
  parent_attempt="att-parent-$2-fixture"
  mkdir -p "$AH/.dispatch"
  printf '2026-07-23T00:00:00Z\topen\t%s\t%s\tcx\tattempt_schema_version=2,dispatch_depth=1,transport=headless,execution_surface=registered-headless,registered_worker=1,fallback_hop=same-harness-headless,worker_type=owner,harness=claude,runtime_sandbox=fixture,attempt_id=%s,pid=%s,pid_start=%s\n' \
    "$wt" "$wt" "$parent_attempt" "$parent_pid" "$parent_start" >> "$AH/.dispatch/jobs.log"
  AGENT_HOME="$AH" AGENT_DISPATCH_JOBS="$AH/.dispatch/jobs.log" \
    AGENT_DISPATCH_ATTEMPT_ID="$parent_attempt" PATH="$bin:$PATH" python3 "$WRAP" --start \
    --worktree "$wt" --slug "$2" --capability autopilot-code --capability-mode dev \
    --worker-mode dev/backend --unit dev/backend --qa standard \
    --intensity standard --dispatch-depth 2 --parent cx --worker-type stage \
    --assigned-contract code-plan --owner autopilot-code \
    --parent-harness claude --parent-transport headless --parent-sandbox fixture \
    --launch-authority conductor --nested-eligibility supported --eligibility-source sd15-fixture \
    --model sonnet --effort medium --early-exit-watch "$3" 2>&1
  rc=$?
  kill "$parent_pid" 2>/dev/null || true
  wait "$parent_pid" 2>/dev/null || true
  return "$rc"
}

# --- Case: limit-death closes row ---
out=$(launch "#!/bin/sh
echo \"You've hit your session limit · resets 3pm\"
exit 1" limit1 6)
echo "$out" | grep -q 'early_death=session-limit' \
  && grep -q $'\tdone\t.*note=dead-session-limit,reset=3pm' "$AH/.dispatch/jobs.log" \
  && ok "limit-death → row done,note=dead-session-limit,reset=3pm" \
  || bad "limit-death row not closed. out=[$out] jobs=[$(cat "$AH/.dispatch/jobs.log")]"
[ -f "$AH/.dispatch/usage-reset.claude" ] && ok "reset cache written" || bad "no reset cache"

# --- Case: capacity is a distinct exact-row failure class and does not update usage reset.
before_reset=$(cat "$AH/.dispatch/usage-reset.claude")
out=$(launch "#!/bin/sh
echo 'Selected model is at capacity'
exit 1" capacity1 4)
echo "$out" | grep -q 'early_death=capacity' \
  && awk -F'\t' '$2=="done" && $5=="capacity1" && $6 ~ /(^|,)model=sonnet(,|$)/ && $6 ~ /(^|,)note=dead-capacity(,|$)/ && $6 ~ /(^|,)failure_class=capacity(,|$)/ { found=1 } END { exit !found }' "$AH/.dispatch/jobs.log" \
  && [ "$(cat "$AH/.dispatch/usage-reset.claude")" = "$before_reset" ] \
  && ok "capacity death → exact row dead-capacity without usage-reset pollution" \
  || bad "capacity death contract failed. out=[$out]"

# --- Case: clean fast exit does NOT close row ---
out=$(launch "#!/bin/sh
echo \"governor=\$AGENT_MODEL_GOVERNOR_ROOT\"
echo ok done
exit 0" clean1 4)
echo "$out" | grep -q 'early_death=-' \
  && awk -F'\t' '$5=="clean1"{print $2}' "$AH/.dispatch/jobs.log" | grep -qx open \
  && grep -q '/.agent_reports/.runtime/model-worker-governor' "$AH/.dispatch/logs/clean1."*.claude.jsonl \
  && ok "clean fast exit → row stays open (normal harvest owns it)" \
  || bad "clean exit or inherited governor root invalid. out=[$out]"

echo "— dispatch-headless SD-15 conformance: $([ $fails -eq 0 ] && echo PASS || echo "FAIL ($fails)")"
exit $fails
