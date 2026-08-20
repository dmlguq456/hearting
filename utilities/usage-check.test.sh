#!/usr/bin/env bash
# usage-check.test.sh — SD-16(a) 사용량 조회 헬퍼 회귀.
#   증명: dead-limit 마커 → limited(reset), 마커 없음 → ok, jobs.log 부재 → unknown,
#   reset 없는 window 밖(오래된) 마커 → ok(만료), known future reset → limited, harness 스코프 필터.
set -uo pipefail
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
UC="$SCRIPT_DIR/usage-check.sh"
fails=0
ok()  { printf 'ok   - %s\n' "$1"; }
bad() { printf 'FAIL - %s\n' "$1"; fails=$((fails + 1)); }

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
AH="$tmp/agent_setting"; mkdir -p "$AH/.dispatch" "$AH/core"; : > "$AH/core/CORE.md"
# usage-check.sh now honours an inherited AGENT_DISPATCH_JOBS ahead of the
# fixture's AGENT_HOME-relative jobs.log, so a dispatch-worker environment
# leaks the real shared registry into every case below. Isolate it exactly
# like hooks/portable-guards.test.sh does (U-7 / test-round X-1).
unset AGENT_DISPATCH_JOBS
jobs="$AH/.dispatch/jobs.log"
now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
old_iso=$(date -u -d '10 hours ago' +%Y-%m-%dT%H:%M:%SZ)
# SD-16e: reset 값을 실행 시각에 비의존이 되도록 now 상대로 동적 산출한다.
fut_clock=$(date -d '+2 hours' +%H:%M)          # 아직 안 온 리셋 → limited 유지
mk30_iso=$(date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
past_clock=$(date -d '10 minutes ago' +%H:%M)   # 마커(30m 전) 이후·now 이전 → 경과 → ok
mk90_iso=$(date -u -d '90 minutes ago' +%Y-%m-%dT%H:%M:%SZ)  # unknown-reset 60m 창 밖·300m 창 안

# Case: fresh claude dead-session-limit marker, reset not yet passed → limited(reset)
printf '%s\tdone\tr\tw\ts1\tcapability=code-plan,depth=2,harness=claude,parent=cx,note=dead-session-limit,reset=%s\n' "$now_iso" "$fut_clock" > "$jobs"
out=$(AGENT_HOME="$AH" bash "$UC" --harness claude 2>&1 | grep -v "^bias ")
[ "$out" = "claude limited($fut_clock)" ] && ok "fresh dead-limit, reset未경과 → limited(reset)" || bad "expected 'claude limited($fut_clock)', got [$out]"

# Case (SD-16e): marker within window but reset= already passed → ok(expired)
printf '%s\tdone\tr\tw\ts1b\tcapability=code-plan,depth=2,harness=claude,note=dead-session-limit,reset=%s\n' "$mk30_iso" "$past_clock" > "$jobs"
out=$(AGENT_HOME="$AH" bash "$UC" --harness claude 2>&1 | grep -v "^bias ")
[ "$out" = "claude ok" ] && ok "reset= passed within window → ok(expired)" || bad "expected 'claude ok' (expired reset), got [$out]"

# Case (SD-16e): dead marker with NO reset=, within UNKNOWN_WINDOW_MIN → limited(unknown-reset)
printf '%s\tdone\tr\tw\ts1c\tcapability=code-plan,depth=2,harness=claude,note=dead-session-limit\n' "$now_iso" > "$jobs"
out=$(AGENT_HOME="$AH" bash "$UC" --harness claude 2>&1 | grep -v "^bias ")
[ "$out" = "claude limited(unknown-reset)" ] && ok "no reset=, recent → limited(unknown-reset)" || bad "expected 'claude limited(unknown-reset)', got [$out]"

# Case (SD-16e): dead marker with NO reset=, past UNKNOWN window but within WINDOW → ok (downgrade)
printf '%s\tdone\tr\tw\ts1d\tcapability=code-plan,depth=2,harness=claude,note=dead-session-limit\n' "$mk90_iso" > "$jobs"
out=$(AGENT_HOME="$AH" bash "$UC" --harness claude 2>&1 | grep -v "^bias ")
[ "$out" = "claude ok" ] && ok "no reset=, beyond unknown-window → ok(downgrade)" || bad "expected 'claude ok' (unknown-reset downgrade), got [$out]"

# Case: no marker → ok
printf '%s\topen\tr\tw\ts2\tcapability=code-plan,depth=2,harness=claude,parent=cx\n' "$now_iso" > "$jobs"
out=$(AGENT_HOME="$AH" bash "$UC" --harness claude 2>&1 | grep -v "^bias ")
[ "$out" = "claude ok" ] && ok "no marker → ok" || bad "expected 'claude ok', got [$out]"

# Case: jobs.log missing → unknown
out=$(AGENT_HOME="$AH" bash "$UC" --harness claude --jobs "$tmp/none.log" 2>&1 | grep -v "^bias ")
[ "$out" = "claude unknown" ] && ok "missing jobs.log → unknown" || bad "expected 'claude unknown', got [$out]"

# Case (runtime-currentness 2026-07-13): old marker but known future reset → still limited.
printf '%s\tdone\tr\tw\ts3\tcapability=code-plan,depth=2,harness=claude,note=dead-session-limit,reset=%s\n' "$old_iso" "$fut_clock" > "$jobs"
out=$(AGENT_HOME="$AH" bash "$UC" --harness claude 2>&1 | grep -v "^bias ")
[ "$out" = "claude limited($fut_clock)" ] && ok "old marker + future reset → limited(reset)" || bad "expected 'claude limited($fut_clock)', got [$out]"

# Case: old marker without reset → bounded unknown window expired → ok
printf '%s\tdone\tr\tw\ts3b\tcapability=code-plan,depth=2,harness=claude,note=dead-session-limit\n' "$old_iso" > "$jobs"
out=$(AGENT_HOME="$AH" bash "$UC" --harness claude 2>&1 | grep -v "^bias ")
[ "$out" = "claude ok" ] && ok "old marker without reset → ok(downgrade)" || bad "expected 'claude ok', got [$out]"

# Case: harness scope — codex marker must not leak into claude
printf '%s\tdone\tr\tw\ts4\tcapability=code-plan,depth=2,harness=codex,owner_harness=codex,note=dead-usage-limit,reset=noon\n' "$now_iso" > "$jobs"
out=$(AGENT_HOME="$AH" bash "$UC" --harness all 2>&1)
echo "$out" | grep -q '^claude ok$' && echo "$out" | grep -q '^codex limited(noon)$' \
  && ok "harness scope: claude ok / codex limited" || bad "harness scope wrong: [$out]"

# Case (2026-08-19/20 실측 오탐): cross-harness child — a codex child under a
# claude owner (owner_harness=claude) hits its limit; the marker belongs to
# codex only. Before the fix, owner_harness= matched claude AND the unanchored
# regex made "owner_harness=claude" substring-match "harness=claude".
printf '%s\tdone\tr\tw\ts5\tcapability=research,depth=2,harness=codex,owner_harness=claude,note=dead-session-limit\n' "$now_iso" > "$jobs"
out=$(AGENT_HOME="$AH" bash "$UC" --harness all 2>&1)
echo "$out" | grep -q '^claude ok$' && echo "$out" | grep -q '^codex limited(unknown-reset)$' \
  && ok "cross-harness child limit: claude ok / codex limited" || bad "cross-harness child attribution wrong: [$out]"

# Case (legacy fallback): row with owner_harness= only (no harness=) still
# attributes the marker to that harness.
printf '%s\tdone\tr\tw\ts6\tcapability=code-plan,depth=1,owner_harness=claude,note=dead-session-limit\n' "$now_iso" > "$jobs"
out=$(AGENT_HOME="$AH" bash "$UC" --harness claude 2>&1 | grep -v "^bias ")
[ "$out" = "claude limited(unknown-reset)" ] && ok "legacy owner_harness-only row → limited" || bad "expected 'claude limited(unknown-reset)' (legacy fallback), got [$out]"

# Case: bias default line present and defaults to neutral auto
out=$(AGENT_HOME="$AH" bash "$UC" --harness claude 2>&1)
echo "$out" | grep -q '^bias auto$' && ok "bias default → auto" || bad "expected 'bias auto' line, got [$out]"

# Case: bias overridable via HARNESS_CAPACITY_BIAS (가변 전제 — 하드코드 금지)
out=$(AGENT_HOME="$AH" HARNESS_CAPACITY_BIAS=codex bash "$UC" --harness claude 2>&1)
echo "$out" | grep -q '^bias codex$' && ok "bias env override → codex" || bad "expected 'bias codex' line, got [$out]"

echo "— usage-check conformance: $([ $fails -eq 0 ] && echo PASS || echo "FAIL ($fails)")"
exit $fails
