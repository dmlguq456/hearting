#!/usr/bin/env bash
# Regression test for mem-turn-nudge.sh (B2 turn-counter, spec v5 Cluster B).
# Fully isolated via MEM_STORE temp dir + MEM_NUDGE_INTERVAL — never touches real ~/.claude state.
# Added 2026-06-16 (Cluster B doc-sync cycle) — commit 5a9ea18 claimed standalone-verified but committed no test.
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/mem-turn-nudge.sh"
[ -f "$HOOK" ] || { echo "FAIL: hook not found at $HOOK"; exit 1; }
DISPATCH="$(cd "$(dirname "$0")" && pwd)/mem-distill-dispatch.sh"
BRIEFING="$(cd "$(dirname "$0")" && pwd)/mem-briefing-inject.sh"
[ -f "$DISPATCH" ] && [ -f "$BRIEFING" ] \
  || { echo "FAIL: sibling memory hook not found"; exit 1; }

# D-42 hermeticity: this file may itself run inside a registered worker, whose
# markers would otherwise gate every case into the silent branch (false green on
# the negative cases, false red on the positive ones). Each case sets its own.
unset AGENT_SESSION_ROLE AGENT_DISPATCH_CHILD AGENT_DISPATCH_DEPTH \
  CLAUDE_CODE_CHILD_SESSION OPENCODE_DISPATCH_SLUG FLEET_TITLE_REFRESH MEM_DISTILL

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  ❌ %s\n' "$1"; }

run() {  # $1=event $2=prompt ; uses $TMP/$N/$SID env ; echoes hook stdout
  printf '{"hook_event_name":"%s","session_id":"%s","prompt":"%s"}' "$1" "$SID" "$2" \
    | MEM_STORE="$TMP" MEM_NUDGE_INTERVAL="$N" bash "$HOOK"
}

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
N=3; SID="testsid"

# seed a memory.db with a stable mtime (simulates an already-populated store)
: > "$TMP/memory.db"; touch -d '2026-06-16 00:00:00' "$TMP/memory.db"

echo "== T1: turns 1..N-1 silent, turn N silent no-op (v7 외부화 — ENABLE unset 시 메인 주입 0) =="
out1="$(run UserPromptSubmit 'hello one')"
out2="$(run UserPromptSubmit 'hello two')"
[ -z "$out1$out2" ] && ok "turns 1..N-1 ($((N-1))) silent" || bad "turns 1..N-1 should be silent, got: $out1$out2"
out3="$(run UserPromptSubmit 'hello three')"
# v7 (spec §5.5.3 D-13): N턴 fire 시 메인 컨텍스트 주입 0. ENABLE unset(테스트 기본)이면 sibling
# dispatch 가 opt-in 게이트로 no-op → turn-nudge stdout 은 완전 빈 문자열(silent no-op). 구 v6 의
# "turn N emits nudge"(hookSpecificOutput + N턴 보간) 단언은 외부화로 제거됨.
[ -z "$out3" ] && ok "turn N → silent no-op (ENABLE unset, 메인 주입 0)" || bad "turn N should be silent no-op, got: $out3"

echo "== T1b: ENABLE=1 + non-empty delta → fire 시 sibling dispatch 도달 (lock-dir 동기 단언, RP-M1) =="
# v7 외부화 계약: ENABLE=1 이면 N턴 fire 가 self-location 으로 sibling dispatch 를 argument 모드
# 로 실제 호출한다. dispatch 는 `mkdir "$STORE/.distill-lock-$SID"` 를 spawn(&) *동기* 경로에서
# 하므로(claude 분사 전), fork 반환 시점에 lock dir 이 이미 존재 — detached-child sentinel race
# 회피(동기 신호로 단언). claude stub 을 PATH 주입해 실 claude 분사 방지.
SIDE="enablesid"                 # default 아닌 실제 SID (turn-nudge default-SID skip 회피)
PROJ_E="$(mktemp -d)"
STORE_E="$(mktemp -d)"
STUB_E="$(mktemp -d)"
printf '#!/bin/sh\nexit 0\n' > "$STUB_E/claude"; chmod +x "$STUB_E/claude"
# fixture jsonl: marker 미설정(전체 yield) → delta non-empty (distill.test.sh A-pos 방식 차용)
ENC_E="$PROJ_E/-home-fake-enable"; mkdir -p "$ENC_E"
cat > "$ENC_E/$SIDE.jsonl" <<'JSONL'
{"type":"user","message":{"role":"user","content":"enable case prompt EPSILON"},"uuid":"e1","timestamp":"te1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"enable case reply ZETA"}]},"uuid":"e2","timestamp":"te2","isSidechain":false}
JSONL
# N턴 발사 (마지막 턴에서 fire → dispatch 호출). 카운터/state 는 STORE_E 격리.
last_out=""
for i in $(seq 1 "$N"); do
  last_out="$(printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","prompt":"p%s"}' "$SIDE" "$i" \
    | MEM_STORE="$STORE_E" MEM_PROJECTS="$PROJ_E" MEM_NUDGE_INTERVAL="$N" MEM_DISTILL_ENABLE=1 \
      MEM_DISTILL_WORKER=claude \
      PATH="$STUB_E:$PATH" bash "$HOOK")"
done
[ -z "$last_out" ] \
  && ok "T1b: ENABLE=1 fire 도 메인 주입 0 (turn-nudge stdout 빈 문자열)" \
  || bad "T1b: ENABLE=1 fire stdout 비어야 함, got: $last_out"
# dispatch 가 spawn 동기 경로에서 lock dir 을 만들었으면 = turn-nudge 가 sibling dispatch 를
# argument 모드로 실제 호출 + dispatch 가 spawn 경로 진입했다는 증거 (delta non-empty 경유).
[ -d "$STORE_E/.distill-lock-$SIDE" ] \
  && ok "T1b: lock dir 존재 → sibling dispatch argument 모드 도달 + spawn 경로 진입" \
  || bad "T1b: lock dir 부재 — dispatch 미도달 또는 spawn 경로 미진입"
rmdir "$STORE_E/.distill-lock-$SIDE" 2>/dev/null || true   # detached child trap rmdir 와 race 무해
rm -rf "$PROJ_E" "$STORE_E" "$STUB_E"

echo "== T2: counter resets after firing (turn N+1 silent) =="
out4="$(run UserPromptSubmit 'hello four')"
[ -z "$out4" ] && ok "turn N+1 silent (counter reset after fire)" || bad "turn N+1 should be silent, got: $out4"

echo "== T3: memory write 는 카운터를 리셋하지 않는다 (Cluster E — 카운터는 distiller fire 때만 리셋) =="
# fresh SID + 높은 interval → fire 간섭 배제, "write-reset 여부"만 격리 검증
SID3="writenoreset"
r3() { printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","prompt":"x"}' "$SID3" | MEM_STORE="$TMP" MEM_NUDGE_INTERVAL=100 bash "$HOOK" >/dev/null 2>&1; }
r3                                                # counter -> 1
touch -d '2026-06-16 12:00:00' "$TMP/memory.db"   # 메인의 명시적 mem add(사용자 "기억해") 등 임의 memory write 시뮬
r3                                                # counter -> 2 (write 무관)
c3="$(sed -n '1p' "$TMP/.turn-state-$SID3")"
[ "$c3" = "2" ] && ok "memory write 가 카운터 리셋 안 함 (1→2) — '기억해' add 가 distiller 억제하던 버그 fix" || bad "write 는 리셋하면 안 됨; 기대 2, got: $c3"

echo "== T4: no phantom write on first turn (fresh session) =="
SID2="freshsid"
printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","prompt":"x"}' "$SID2" \
  | MEM_STORE="$TMP" MEM_NUDGE_INTERVAL="$N" bash "$HOOK" >/dev/null
fc="$(sed -n '1p' "$TMP/.turn-state-$SID2")"
[ "$fc" = "1" ] && ok "first turn → counter 1 (no phantom-write inflation)" || bad "first turn counter should be 1, got: $fc"

echo "== T5: non-UserPromptSubmit event → silent exit 0 =="
out_se="$(run SessionStart 'x')"; rc=$?
[ -z "$out_se" ] && [ "$rc" = "0" ] && ok "SessionStart → silent, exit 0" || bad "SessionStart should be silent exit0 (out='$out_se' rc=$rc)"

echo "== T6: broken stdin → exit 0 fail-safe (no crash) =="
printf 'not json at all' | MEM_STORE="$TMP" MEM_NUDGE_INTERVAL="$N" bash "$HOOK" >/dev/null 2>&1
[ "$?" = "0" ] && ok "broken stdin → exit 0" || bad "broken stdin should exit 0"

echo "== T7: stale state-file GC (3일+ 비활성 삭제) =="
old="$TMP/.turn-state-stalesession"; : > "$old"; touch -d '2026-06-10 00:00:00' "$old"  # 6 days old
run UserPromptSubmit 'trigger gc' >/dev/null
[ ! -e "$old" ] && ok "stale (>3d) state file GC'd" || bad "stale state file should be deleted"
[ -e "$TMP/.turn-state-$SID" ] && ok "fresh state file preserved by GC" || bad "fresh state file wrongly deleted"

echo "== T8: D-42 worker markers return before counter/state creation =="
worker_case=0
for assignment in \
  "AGENT_SESSION_ROLE=worker" \
  "AGENT_DISPATCH_CHILD=1" \
  "AGENT_DISPATCH_DEPTH=1" \
  "OPENCODE_DISPATCH_SLUG=worker-slug" \
  "FLEET_TITLE_REFRESH=1" \
  "MEM_DISTILL=1"; do
  worker_case=$((worker_case + 1))
  worker_sid="worker-nudge-$worker_case"
  worker_state="$TMP/.turn-state-$worker_sid"
  rm -f "$worker_state"
  worker_out="$(printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","prompt":"x"}' "$worker_sid" \
    | env "$assignment" MEM_STORE="$TMP" MEM_NUDGE_INTERVAL=1 bash "$HOOK")"
  if [ -z "$worker_out" ] && [ ! -e "$worker_state" ]; then
    ok "T8: $assignment → silent no-op, no counter state"
  else
    bad "T8: $assignment must return before counter state"
  fi
done

echo "== T9: §5.10 — runtime child-session marker alone is an interactive session =="
teammate_sid="teammate-nudge-1"
teammate_state="$TMP/.turn-state-$teammate_sid"
rm -f "$teammate_state"
printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","prompt":"x"}' "$teammate_sid" \
  | env CLAUDE_CODE_CHILD_SESSION=1 MEM_STORE="$TMP" MEM_NUDGE_INTERVAL=1 bash "$HOOK" >/dev/null
if [ -e "$teammate_state" ]; then
  ok "T9: CLAUDE_CODE_CHILD_SESSION alone keeps the turn counter running"
else
  bad "T9: teammate session (runtime marker only) was wrongly gated as a worker"
fi

echo "== T10: packaged root without legacy memory uses the XDG persistent store =="
packaged_root="$TMP/packaged-root"
xdg_data="$TMP/xdg-data"
packaged_sid="packaged-root-nudge"
mkdir -p "$packaged_root" "$xdg_data"
printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","prompt":"x"}' "$packaged_sid" \
  | env -u MEM_STORE AGENT_HOME="$packaged_root" XDG_DATA_HOME="$xdg_data" \
      MEM_NUDGE_INTERVAL=100 bash "$HOOK" >/dev/null
if [ -f "$xdg_data/hearting/memory/.turn-state-$packaged_sid" ] \
  && [ ! -e "$packaged_root/memory" ]; then
  ok "T10: immutable packaged root stays clean; counter uses XDG memory"
else
  bad "T10: packaged root was mutated or XDG counter was not created"
fi

echo "== T11: packaged distill dispatcher uses the XDG persistent store =="
distill_root="$TMP/packaged-distill-root"
distill_xdg="$TMP/distill-xdg"
mkdir -p "$distill_root" "$distill_xdg"
printf '{"session_id":"packaged-distill","cwd":"%s"}' "$TMP" \
  | env -u MEM_STORE AGENT_HOME="$distill_root" XDG_DATA_HOME="$distill_xdg" \
      MEM_DISTILL_ENABLE=1 bash "$DISPATCH" >/dev/null
if [ -d "$distill_xdg/hearting/memory" ] && [ ! -e "$distill_root/memory" ]; then
  ok "T11: packaged distill root stays clean; state uses XDG memory"
else
  bad "T11: packaged distill root was mutated or XDG store was not created"
fi

echo "== T12: packaged briefing hook uses the XDG persistent store =="
briefing_root="$TMP/packaged-briefing-root"
briefing_xdg="$TMP/briefing-xdg"
briefing_home="$TMP/briefing-home"
briefing_desk="$briefing_home/.claude"
briefing_report="$TMP/briefing-report.md"
mkdir -p "$briefing_root" "$briefing_xdg" "$briefing_desk"
printf 'briefing fixture\n' > "$briefing_report"
env -u MEM_STORE HOME="$briefing_home" AGENT_HOME="$briefing_root" \
  XDG_DATA_HOME="$briefing_xdg" MEM_BRIEFING_DESK="$briefing_desk" \
  MEM_BRIEFING_ONCALL="$briefing_report" MEM_PY=/bin/false \
  bash "$BRIEFING" --cwd "$briefing_desk" --format text >/dev/null
if find "$briefing_xdg/hearting/memory" -maxdepth 1 -name '.briefing-*' \
    -type f -print -quit 2>/dev/null | grep -q . \
  && [ ! -e "$briefing_root/memory" ]; then
  ok "T12: packaged briefing root stays clean; marker uses XDG memory"
else
  bad "T12: packaged briefing root was mutated or XDG marker was not created"
fi

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
