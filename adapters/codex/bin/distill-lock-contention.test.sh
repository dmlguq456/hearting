#!/usr/bin/env sh
# curate must not silently lose the per-session lock to a live turn-nudge
# increment: nothing retries a skipped curate.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
WORKER="$ROOT/adapters/codex/bin/distill-worker.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' 0 HUP INT TERM
store="$T/store"; mkdir -p "$store" "$T/home" "$T/project" "$T/bin" "$T/sessions"
sid="lock-contention-fixture"
lock="$store/.codex-distill-lock-$sid"

# The worker exits before the lock when the CLI is absent or the delta is empty.
printf '#!/bin/sh\nexit 0\n' > "$T/bin/codex"; chmod +x "$T/bin/codex"
cat > "$T/sessions/rollout-$sid.jsonl" <<'ROLLOUT'
{"type":"event_msg","timestamp":"2026-09-22T00:00:00Z","payload":{"type":"user_message","id":"u1","message":"LOCKFIXTURE question"}}
{"type":"response_item","timestamp":"2026-09-22T00:00:01Z","payload":{"type":"message","id":"a1","role":"assistant","content":[{"type":"output_text","text":"LOCKFIXTURE answer"}]}}
ROLLOUT

run() {  # $1=mode $2=lock-wait ; prints "rc=<n>" and stderr
  mode=$1
  env -u AGENT_DISPATCH_JOBS HOME="$T/home" XDG_CONFIG_HOME="$T/xdg" \
      XDG_DATA_HOME="$T/data" XDG_STATE_HOME="$T/state" XDG_CACHE_HOME="$T/cache" \
      CODEX_HOME="$T/codex" CLAUDE_CONFIG_DIR="$T/claude" TMPDIR="$T" \
      MEM_STORE="$store" CODEX_DISTILL_LOCK_WAIT="$2" \
      CODEX_DISTILL_ENABLE=1 CODEX_SESSIONS="$T/sessions" \
      PATH="$T/bin:$PATH" \
      sh "$WORKER" "$sid" "$T/project" "$mode" 2>"$T/err" >/dev/null && rc=0 || rc=$?
  printf 'rc=%s\n' "$rc"
}

# A held lock stands in for a detached increment that has not finished.
mkdir "$lock"

# increment: losing the race is harmless, so it still skips immediately.
start=$(date +%s)
run increment 30 >/dev/null
elapsed=$(( $(date +%s) - start ))
grep -q 'another distill in progress' "$T/err" || { echo "FAIL: increment did not report the skip"; cat "$T/err"; exit 1; }
[ "$elapsed" -le 2 ] || { echo "FAIL: increment waited ${elapsed}s instead of skipping"; exit 1; }

# curate: waits for the bound before giving up, rather than skipping at once.
start=$(date +%s)
run curate 3 >/dev/null
elapsed=$(( $(date +%s) - start ))
grep -q 'another distill in progress' "$T/err" || { echo "FAIL: curate did not report the skip"; cat "$T/err"; exit 1; }
[ "$elapsed" -ge 3 ] || { echo "FAIL: curate gave up after ${elapsed}s without waiting out its bound"; exit 1; }

# curate proceeds once the increment releases the lock inside the bound.
( sleep 2; rmdir "$lock" ) &
releaser=$!
start=$(date +%s)
run curate 30 >/dev/null
elapsed=$(( $(date +%s) - start ))
wait "$releaser" 2>/dev/null || true
grep -q 'another distill in progress' "$T/err" && { echo "FAIL: curate skipped a lock that was released in time"; cat "$T/err"; exit 1; }
grep -q 'curate waited' "$T/err" || { echo "FAIL: curate did not report the wait it performed"; cat "$T/err"; exit 1; }
[ "$elapsed" -ge 2 ] || { echo "FAIL: curate did not actually wait"; exit 1; }
printf 'distill-lock-contention: PASS\n'
