#!/usr/bin/env bash
# Standalone test for mem-distill-dispatch.sh (spec v8 §5.5 D-12/D-13/D-14).
# Fully isolated via MEM_STORE + MEM_PROJECTS temp dirs — never touches the real store (~/agent_setting/memory).
# Real worker spawn is ALWAYS avoided via MEM_DISTILL_WORKER=claude plus a PATH-injected stub.
# Covers Phase-3 Verification ②③④⑥ from plan: 2026-06-16_distiller-v7-hardening/plan/plan.md
#
# ⚠️ ⑤ (live D-14 no-tools acceptance probe) is OUT OF SCOPE here (this file = stubs only, never real claude).
#    ⑤ merge-gate is now the no-tools acceptance (separate live script, `--disallowedTools` probe) that
#    verifies date>>file is blocked in the REAL operating settings.json env (blanket Bash allow + skipAutoPermissionPrompt
#    + skipDangerousModePermissionPrompt + defaultMode:auto all live) — disallow>allow precedence proved in-env.
#    allowlist probe approach (v7) is deprecated — v7 --allowedTools was empirically inert.
#    Stub tests (a)/(b) below cover JSON-lines parsing and injection-in-body at unit level;
#    live acceptance gate (Verification ①) is out of scope here (stubs only, never real claude).
#    DEFERRED (before flipping MEM_DISTILL_ENABLE=1 — D5 hard gate): R1 env-inheritance, ghost-marker,
#    R7 (mem sync double-absorb / herdr state pollution) one-time live checks; plus optional clean-deny
#    improvement. HARD RULE: ENABLE=1 FORBIDDEN until the deferred live checks pass.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DISPATCH="$ROOT/hooks/mem-distill-dispatch.sh"
TURNNUDGE="$ROOT/hooks/mem-turn-nudge.sh"
MEM="$ROOT/tools/memory/mem.py"
APPLIER="$ROOT/tools/memory/apply-distill-actions.py"
CLAUDE_DISPATCH="$ROOT/adapters/claude/hooks/mem-distill-dispatch.sh"
CODEX_DISTILL="$ROOT/adapters/codex/bin/distill-worker.sh"
OPENCODE_DISTILL="$ROOT/adapters/opencode/bin/distill-worker.sh"
[ -f "$DISPATCH" ] || { echo "FAIL: dispatch hook not found at $DISPATCH"; exit 1; }
[ -f "$TURNNUDGE" ] || { echo "FAIL: turn-nudge hook not found at $TURNNUDGE"; exit 1; }
# dispatch 가 *이 worktree* 의 mem.py 를 쓰도록 강제 (라이브 ~/.claude 는 pre-γ — curate-snapshot/prune 부재).
export MEM_PY="$MEM"
export MEM_DISTILL_WORKER=claude
# D-42 hermeticity: this file may itself run inside a registered worker, whose
# markers would otherwise gate every case into the silent branch. Each case sets
# its own markers inline (mirrors hooks/portable-guards.test.sh).
unset AGENT_SESSION_ROLE AGENT_DISPATCH_CHILD AGENT_DISPATCH_DEPTH \
  CLAUDE_CODE_CHILD_SESSION OPENCODE_DISPATCH_SLUG FLEET_TITLE_REFRESH MEM_DISTILL

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  ❌ %s\n' "$1"; }

# ---- isolated store/projects ----
STORE="$(mktemp -d)"; PROJ="$(mktemp -d)"
STUBBIN="$(mktemp -d)"      # sentinel stub (touch CLAUDE_CALLED iff claude invoked)
STUBCAP="$(mktemp -d)"      # argv-capture stub (echoes argv to ARGV file)
# 단일 EXIT trap + 누적 cleanup 배열: bash 의 trap EXIT 는 append 아니라 replace 이므로,
# 신규 테스트(a/M3/M4/b)는 새 trap 을 선언하는 대신 CLEANUP 에 경로를 append 한다 (누수 방지).
CLEANUP=("$STORE" "$PROJ" "$STUBBIN" "$STUBCAP")
trap 'rm -rf "${CLEANUP[@]}"; rm -f "${SENTINEL_B:-}"' EXIT
export MEM_STORE="$STORE" MEM_PROJECTS="$PROJ"
# Isolate shared model-worker admission from live sessions and other tests.
export AGENT_MODEL_GOVERNOR_ROOT="$STORE/.test-model-governor"

# RP-M4 decision: stubs kept INLINE (not extracted to hooks/test-helpers/dispatch-stub.sh).
# Rationale — each stub is 2 lines; the two test files' isolation setups differ (this file uses
# per-stub temp dirs + sentinel/argv variants, distill.test.sh shares one TMPSTUB), so a shared
# source would add coupling for negligible LOC savings. Extraction cost > benefit here.

# sentinel worker stub: marks invocation (used by ②③④ to detect spawn/no-spawn)
printf '#!/bin/sh\ntouch "%s/CLAUDE_CALLED"\n' "$STUBBIN" > "$STUBBIN/claude"
chmod +x "$STUBBIN/claude"

# argv-capture worker stub: appends its full argv (one per line) to ARGV (used by ⑥)
printf '#!/bin/sh\nfor a in "$@"; do printf "%%s\\n" "$a"; done >> "%s/ARGV"\n' "$STUBCAP" > "$STUBCAP/claude"
chmod +x "$STUBCAP/claude"

# ---- fixture jsonl helper: writes a 2-msg session so delta is non-empty (marker unset → full yield) ----
mkfix() {  # $1=sid
  local sid="$1" enc
  # Most legacy cases test one independent dispatch contract. Reset the new
  # rolling budget between those cases; the dedicated storm case below keeps it.
  rm -rf "$STORE"/.distill-budget-* 2>/dev/null || true
  enc="$PROJ/-home-fake-$sid"; mkdir -p "$enc"
  cat > "$enc/$sid.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"dispatch test prompt $sid"},"uuid":"${sid}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"dispatch test reply $sid"}]},"uuid":"${sid}a1","timestamp":"t2","isSidechain":false}
JSONL
}

# ============================================================
# ② 양쪽 호출모드 STORE 기반 marker/lock materialize (RP-M3 — path materialization, not narrative)
#    stdin-JSON(SessionEnd) 과 argument(turn-counter) 가 같은 STORE-resolve 규칙을 쓰는지 단언.
#    두 호출을 *별도 sid* 로 격리 — ②a 의 detached child trap-rmdir 이 ②b 의 같은-sid lock 을
#    늦게 지우는 race 를 차단(A-neg sentinel 격리와 동형). 규칙이 같으므로 같은 sid 면 같은 경로(자명).
# ============================================================
echo "== ② 양쪽 호출모드(stdin-JSON + argument) → STORE 기반 marker/lock materialize (sid 격리) =="
SID2="dispatchsid2"
mkfix "$SID2"
# 두 호출 다 delta non-empty 전제 확인
delta2="$(python3 "$MEM" distill "$SID2")"
[ -n "${delta2//[[:space:]]/}" ] && ok "②: non-empty delta (양 모드 분사 전제)" || bad "②: delta empty — fixture 이상"

# (a) stdin-JSON 모드 (SessionEnd 경로)
echo "{\"session_id\":\"$SID2\",\"cwd\":\"/tmp\"}" \
  | MEM_DISTILL_ENABLE=1 PATH="$STUBBIN:$PATH" bash "$DISPATCH"
[ -d "$STORE/.distill-lock-$SID2" ] \
  && ok "②a stdin-JSON: lock dir = \$STORE/.distill-lock-$SID2 (MEM_STORE 기반 materialize)" \
  || bad "②a stdin-JSON: lock dir 미생성"
# marker: v8 에서 dispatch 의 detached child 가 --advance 를 실행한다. 테스트의 수동 --advance 는
# child 와 idempotent (같은 sid → 같은 last uuid), stub-less ② 경로의 marker materialize 결정성을
# 위해 유지한다. 경로 규칙이 STORE 기반으로 materialize 되는지만 직접 확인(명시적).
python3 "$MEM" distill "$SID2" --advance >/dev/null 2>&1
[ -f "$STORE/.distill-state-$SID2" ] \
  && ok "②a stdin-JSON: marker path = \$STORE/.distill-state-$SID2 materialize (child advances; manual --advance idempotent)" \
  || bad "②a stdin-JSON: marker 경로 불일치"
# ②a 정리 (lock — child trap 과 무관하게 동기 제거)
rmdir "$STORE/.distill-lock-$SID2" 2>/dev/null || true

# (b) argument 모드 (turn-counter 경로) — 별도 sid 로 격리(②a detached child 의 late trap-rmdir 이
#     이 lock 을 못 건드리게, race-free). stdin 모드와 동일 STORE-resolve 규칙을 쓰는지 단언.
SID2B="dispatchsid2b"
mkfix "$SID2B"
delta2b="$(python3 "$MEM" distill "$SID2B")"
[ -n "${delta2b//[[:space:]]/}" ] && ok "②: argument 모드 delta non-empty(분사 전제)" || bad "②: argument fixture delta empty"
MEM_DISTILL_ENABLE=1 PATH="$STUBBIN:$PATH" bash "$DISPATCH" distill "$SID2B" "/tmp"
[ -d "$STORE/.distill-lock-$SID2B" ] \
  && ok "②b argument: lock dir = \$STORE/.distill-lock-$SID2B (stdin 모드와 동일 STORE-resolve 규칙)" \
  || bad "②b argument: lock dir 미생성 — argument 모드가 stdin 모드와 STORE-resolve 분기"
rmdir "$STORE/.distill-lock-$SID2B" 2>/dev/null || true

# ============================================================
# ③ lock 동시 1개 (pre-existing lock ⇒ skip; absent ⇒ spawn-path)
# ============================================================
echo "== ③ lock 동시 1개 — 사전 lock 존재 시 skip, 제거 후 재호출 시 spawn 경로 진입 =="
SID3="dispatchsid3"
mkfix "$SID3"
STUBLOCK="$(mktemp -d)"; CLEANUP+=("$STUBLOCK")
printf '#!/bin/sh\ntouch "%s/CLAUDE_CALLED"\n' "$STUBLOCK" > "$STUBLOCK/claude"; chmod +x "$STUBLOCK/claude"
# 도는 distiller 모사: lock 사전 생성
mkdir -p "$STORE/.distill-lock-$SID3"
rm -f "$STUBLOCK/CLAUDE_CALLED"
rc3=0
echo "{\"session_id\":\"$SID3\",\"cwd\":\"/tmp\"}" \
  | MEM_DISTILL_ENABLE=1 PATH="$STUBLOCK:$PATH" bash "$DISPATCH" || rc3=$?
[ "$rc3" = "0" ] && ok "③ 사전 lock 존재 → exit 0 (skip)" || bad "③ 사전 lock skip 시 exit code: $rc3"
[ ! -e "$STUBLOCK/CLAUDE_CALLED" ] \
  && ok "③ 사전 lock 존재 → sentinel ABSENT (분사 skip, 동시 1개)" \
  || bad "③ 사전 lock 임에도 claude 분사됨 (lock skip 실패)"
# lock 제거 후 재호출 → spawn 경로 진입 (동기 lock 재생성으로 단언, race-free)
rmdir "$STORE/.distill-lock-$SID3" 2>/dev/null || true
MEM_DISTILL_ENABLE=1 PATH="$STUBLOCK:$PATH" bash "$DISPATCH" distill "$SID3" "/tmp"
[ -d "$STORE/.distill-lock-$SID3" ] \
  && ok "③ lock 제거 후 재호출 → spawn 경로 진입 (lock dir 재생성)" \
  || bad "③ lock 제거 후 재호출 — spawn 경로 미진입 (lock dir 부재)"
rmdir "$STORE/.distill-lock-$SID3" 2>/dev/null || true

# ============================================================
# ④ 재귀가드 양 hook 양 모드 (MEM_DISTILL=1 ⇒ 즉시 exit 0, spawn 0)
# ============================================================
echo "== ④ 재귀가드 — MEM_DISTILL=1 시 dispatch(양 모드)·turn-nudge 즉시 exit 0, sentinel ABSENT =="
SID4="dispatchsid4"
mkfix "$SID4"
STUBRECUR="$(mktemp -d)"; CLEANUP+=("$STUBRECUR")
printf '#!/bin/sh\ntouch "%s/CLAUDE_CALLED"\n' "$STUBRECUR" > "$STUBRECUR/claude"; chmod +x "$STUBRECUR/claude"

# dispatch stdin-JSON 모드
rm -f "$STUBRECUR/CLAUDE_CALLED"
rc4a=0
echo "{\"session_id\":\"$SID4\",\"cwd\":\"/tmp\"}" \
  | MEM_DISTILL=1 MEM_DISTILL_ENABLE=1 PATH="$STUBRECUR:$PATH" bash "$DISPATCH" || rc4a=$?
[ "$rc4a" = "0" ] && ok "④ dispatch stdin-JSON: 재귀가드 exit 0" || bad "④ dispatch stdin-JSON exit: $rc4a"
[ ! -e "$STUBRECUR/CLAUDE_CALLED" ] \
  && ok "④ dispatch stdin-JSON: MEM_DISTILL=1 → sentinel ABSENT" \
  || bad "④ dispatch stdin-JSON: 재귀가드 실패 (sentinel PRESENT)"

# dispatch argument 모드
rm -f "$STUBRECUR/CLAUDE_CALLED"
rc4b=0
MEM_DISTILL=1 MEM_DISTILL_ENABLE=1 PATH="$STUBRECUR:$PATH" bash "$DISPATCH" distill "$SID4" "/tmp" || rc4b=$?
[ "$rc4b" = "0" ] && ok "④ dispatch argument: 재귀가드 exit 0" || bad "④ dispatch argument exit: $rc4b"
[ ! -e "$STUBRECUR/CLAUDE_CALLED" ] \
  && ok "④ dispatch argument: MEM_DISTILL=1 → sentinel ABSENT" \
  || bad "④ dispatch argument: 재귀가드 실패 (sentinel PRESENT)"

# turn-nudge (stdin 파이프 주입 — guard-path drain + exit0 under pipefail 확인, Step 2.1 RP-M5)
rm -f "$STUBRECUR/CLAUDE_CALLED"
rc4c=0
printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","prompt":"x"}' "$SID4" \
  | MEM_DISTILL=1 MEM_DISTILL_ENABLE=1 MEM_STORE="$STORE" MEM_NUDGE_INTERVAL=1 \
    PATH="$STUBRECUR:$PATH" bash "$TURNNUDGE" || rc4c=$?
[ "$rc4c" = "0" ] \
  && ok "④ turn-nudge: MEM_DISTILL=1 + stdin 파이프 → drain + exit 0 (pipefail 무탈)" \
  || bad "④ turn-nudge: 재귀가드 exit code under pipefail: $rc4c"
[ ! -e "$STUBRECUR/CLAUDE_CALLED" ] \
  && ok "④ turn-nudge: MEM_DISTILL=1 → sentinel ABSENT (재분사 차단)" \
  || bad "④ turn-nudge: 재귀가드 실패 (sentinel PRESENT)"

# ============================================================
# D-42 main/worker boundary — every compatibility marker fails closed
# ============================================================
echo "== D-42 worker boundary — all worker markers skip before state/model work =="
STUBWORKER="$(mktemp -d)"; CLEANUP+=("$STUBWORKER")
printf '#!/bin/sh\ntouch "%s/CLAUDE_CALLED"\n' "$STUBWORKER" > "$STUBWORKER/claude"; chmod +x "$STUBWORKER/claude"
worker_case=0
for assignment in \
  "AGENT_SESSION_ROLE=worker" \
  "AGENT_DISPATCH_CHILD=1" \
  "AGENT_DISPATCH_DEPTH=1" \
  "OPENCODE_DISPATCH_SLUG=worker-slug" \
  "FLEET_TITLE_REFRESH=1"; do
  worker_case=$((worker_case + 1))
  workersid="worker-boundary-$worker_case"
  mkfix "$workersid"
  rm -f "$STUBWORKER/CLAUDE_CALLED"
  rm -rf "$STORE/.distill-lock-$workersid" "$STORE/.distill-state-$workersid"
  rc_worker=0
  env "$assignment" MEM_DISTILL_ENABLE=1 PATH="$STUBWORKER:$PATH" \
    bash "$DISPATCH" distill "$workersid" "/tmp" || rc_worker=$?
  if [ "$rc_worker" = "0" ] \
    && [ ! -e "$STUBWORKER/CLAUDE_CALLED" ] \
    && [ ! -e "$STORE/.distill-lock-$workersid" ] \
    && [ ! -e "$STORE/.distill-state-$workersid" ]; then
    ok "D-42 portable dispatcher: $assignment → silent no-op before state/model"
  else
    bad "D-42 portable dispatcher: $assignment escaped worker boundary"
  fi
done

# §5.10 regression: a runtime child-session marker alone is an interactive teammate
# session, not a registered worker — the lifecycle must still run for it.
teammate_sid="teammate-boundary-1"
mkfix "$teammate_sid"
rm -f "$STUBWORKER/CLAUDE_CALLED"
rm -rf "$STORE/.distill-lock-$teammate_sid" "$STORE/.distill-state-$teammate_sid"
CLAUDE_CODE_CHILD_SESSION=1 MEM_DISTILL_ENABLE=1 PATH="$STUBWORKER:$PATH" \
  bash "$DISPATCH" distill "$teammate_sid" "/tmp" || true
if [ -e "$STUBWORKER/CLAUDE_CALLED" ] || [ -e "$STORE/.distill-lock-$teammate_sid" ]; then
  ok "§5.10: CLAUDE_CODE_CHILD_SESSION alone does not suppress distillation"
else
  bad "§5.10: teammate session (runtime marker only) was wrongly treated as a worker"
fi

adapter_sid="worker-boundary-claude-adapter"
mkfix "$adapter_sid"
rm -f "$STUBWORKER/CLAUDE_CALLED"
AGENT_SESSION_ROLE=worker MEM_DISTILL_ENABLE=1 PATH="$STUBWORKER:$PATH" \
  bash "$CLAUDE_DISPATCH" distill "$adapter_sid" "/tmp"
if [ ! -e "$STUBWORKER/CLAUDE_CALLED" ] \
  && [ ! -e "$STORE/.distill-lock-$adapter_sid" ]; then
  ok "D-42 Claude adapter dispatcher: portable worker marker no-ops"
else
  bad "D-42 Claude adapter dispatcher escaped portable worker marker"
fi

# ============================================================
# S-1 storm guard — atomic global slots + sustained start budget + kill switch
# ============================================================
echo "== S-1 storm guard — 12 concurrent sessions stay <=2; later backlog stays blocked =="
STORMSTORE="$(mktemp -d)"; STORMPROJ="$(mktemp -d)"; STORMSTUB="$(mktemp -d)"
CLEANUP+=("$STORMSTORE" "$STORMPROJ" "$STORMSTUB")
STORM_CALLS="$STORMSTORE/calls"
cat > "$STORMSTUB/claude" <<'STUBEOF'
#!/bin/sh
printf '%s\n' "$$" >> "$STORM_CALLS"
sleep 2
STUBEOF
chmod +x "$STORMSTUB/claude"

mkstorm() {  # $1=sid
  local sid="$1" enc="$STORMPROJ/-home-fake-$1"
  mkdir -p "$enc"
  cat > "$enc/$sid.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"storm prompt $sid"},"uuid":"${sid}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"storm reply $sid"}]},"uuid":"${sid}a1","timestamp":"t2","isSidechain":false}
JSONL
}

for n in $(seq 1 16); do mkstorm "storm$n"; done
for n in $(seq 1 12); do
  MEM_STORE="$STORMSTORE" MEM_PROJECTS="$STORMPROJ" MEM_DISTILL_ENABLE=1 \
    MEM_DISTILL_WORKER="$STORMSTUB/claude" MEM_DISTILL_MAX_CONCURRENT=2 \
    MEM_DISTILL_MAX_STARTS=2 STORM_CALLS="$STORM_CALLS" \
    bash "$DISPATCH" distill "storm$n" "/tmp" &
done
wait
for _ in $(seq 1 30); do
  first_calls="$(wc -l < "$STORM_CALLS" 2>/dev/null || echo 0)"
  [ "${first_calls:-0}" -ge 1 ] 2>/dev/null && break
  sleep 0.1
done
first_calls="$(wc -l < "$STORM_CALLS" 2>/dev/null || echo 0)"
if [ "${first_calls:-0}" -ge 1 ] 2>/dev/null && [ "${first_calls:-0}" -le 2 ] 2>/dev/null; then
  ok "S-1: 12-session concurrent wave invoked at most 2 workers (atomic slots)"
else
  bad "S-1: concurrent worker count=${first_calls:-0}, expected 1..2"
fi
for _ in $(seq 1 60); do
  slots="$(find "$STORMSTORE" -maxdepth 1 -name '.distill-slot-*' -type d | wc -l)"
  [ "$slots" = 0 ] && break
  sleep 0.1
done

# Slots are free now, but the persistent 10-minute start leases must reject a
# second wave. This catches a concurrency-only guard that drains backlog forever.
for n in $(seq 13 16); do
  MEM_STORE="$STORMSTORE" MEM_PROJECTS="$STORMPROJ" MEM_DISTILL_ENABLE=1 \
    MEM_DISTILL_WORKER="$STORMSTUB/claude" MEM_DISTILL_MAX_CONCURRENT=2 \
    MEM_DISTILL_MAX_STARTS=2 STORM_CALLS="$STORM_CALLS" \
    bash "$DISPATCH" distill "storm$n" "/tmp" &
done
wait
sleep 0.3
second_calls="$(wc -l < "$STORM_CALLS" 2>/dev/null || echo 0)"
[ "$second_calls" = "$first_calls" ] \
  && ok "S-1: later sequential backlog blocked by rolling start budget" \
  || bad "S-1: start budget bypassed (first=$first_calls second=$second_calls)"

mkdir -p "$STORMSTORE/.distill-disable"
MEM_STORE="$STORMSTORE" MEM_PROJECTS="$STORMPROJ" MEM_DISTILL_ENABLE=1 \
  MEM_DISTILL_WORKER="$STORMSTUB/claude" STORM_CALLS="$STORM_CALLS" \
  bash "$DISPATCH" distill "storm16" "/tmp"
sleep 0.2
kill_calls="$(wc -l < "$STORM_CALLS" 2>/dev/null || echo 0)"
[ "$kill_calls" = "$second_calls" ] \
  && ok "S-1: .distill-disable prevents every new worker" \
  || bad "S-1: kill switch allowed a worker"

# ============================================================
# ⑥ worker argv capture — mode + model + prompt-file
# ============================================================
echo "== ⑥ worker argv capture — increment · fast-distiller · prompt-file =="
SID6="dispatchsid6"
mkfix "$SID6"
rm -f "$STUBCAP/ARGV"
MEM_DISTILL_ENABLE=1 PATH="$STUBCAP:$PATH" bash "$DISPATCH" distill "$SID6" "/tmp"
# detached child(setsid)의 argv write 가 도달할 시간 — 폴링 (최대 ~5s, CI/부하 환경 마진)
for _ in $(seq 1 50); do [ -s "$STUBCAP/ARGV" ] && break; sleep 0.1; done
argv="$(cat "$STUBCAP/ARGV" 2>/dev/null || true)"
printf '%s\n' "$argv" | grep -qx -- "increment" \
  && ok "⑥ argv: mode increment 포함" \
  || bad "⑥ argv: mode increment 부재: [$argv]"
printf '%s\n' "$argv" | grep -qx -- "fast-distiller" \
  && ok "⑥ argv: model role fast-distiller 포함" \
  || bad "⑥ argv: model role fast-distiller 부재: [$argv]"
prompt_arg="$(printf '%s\n' "$argv" | tail -n1)"
[ -n "$prompt_arg" ] && printf '%s' "$prompt_arg" | grep -q '\.distill-prompt-' \
  && ok "⑥ argv: prompt-file 경로 전달됨" \
  || bad "⑥ argv: prompt-file 경로 이상: [$prompt_arg]"
rmdir "$STORE/.distill-lock-$SID6" 2>/dev/null || true

# ============================================================
# test (a) — JSON-lines 파싱 (stub): 유효 2줄 + malformed 5줄 → row count == 2
# ============================================================
echo "== test(a): JSON-lines 파싱 — 유효 2줄 + malformed 5줄 → isolated DB row count == 2 =="
SIDa="dispatchsida"
STOREa="$(mktemp -d)"; PROJa="$(mktemp -d)"
STUBa="$(mktemp -d)"
CLEANUP+=("$STOREa" "$PROJa" "$STUBa")

# fixture (isolated)
enc_a="$PROJa/-home-fake-$SIDa"; mkdir -p "$enc_a"
cat > "$enc_a/$SIDa.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"test (a) prompt"},"uuid":"${SIDa}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"test (a) reply text"}]},"uuid":"${SIDa}a1","timestamp":"t2","isSidechain":false}
JSONL

# stub 이 PROMPT argv 무관하게 고정 JSON-lines 출력 (printf/heredoc — 셸 확장 없이 literal)
# 유효 2줄: body ≥15자, tier/type/body 모두 있음
# malformed 5줄: (1) non-JSON garbage, (2) body 누락, (3) type 누락, (4) bad tier, (5) body 2001자
LONG_BODY="$(python3 -c "print('x' * 2001)")"
cat > "$STUBa/claude" <<STUBEOF
#!/bin/sh
printf '%s\n' '{"tier":"durable","type":"decision","body":"this is a valid durable decision record","headline":"Durable decision","aliases":[],"entities":[],"topics":["tests"],"artifact_refs":[]}' \
              '{"tier":"working","type":"unresolved-obligation","body":"this is a valid working obligation item","headline":"Working obligation","aliases":[],"entities":[],"topics":["tests"],"artifact_refs":[]}' \
              'not-json-garbage-line' \
              '{"tier":"durable","type":"decision"}' \
              '{"tier":"durable","body":"missing type field here okay","headline":"Missing type","aliases":[],"entities":[],"topics":[],"artifact_refs":[]}' \
              '{"tier":"baz","type":"decision","body":"bad tier value test record","headline":"Bad tier","aliases":[],"entities":[],"topics":[],"artifact_refs":[]}' \
              "{\"tier\":\"durable\",\"type\":\"decision\",\"body\":\"$LONG_BODY\",\"headline\":\"Too long\",\"aliases\":[],\"entities\":[],\"topics\":[],\"artifact_refs\":[]}"
STUBEOF
chmod +x "$STUBa/claude"

MEM_STORE="$STOREa" MEM_PROJECTS="$PROJa" MEM_DISTILL_ENABLE=1 PATH="$STUBa:$PATH" \
  bash "$DISPATCH" distill "$SIDa" "/tmp"

# 폴링: detached child 가 mem add 완료할 때까지 row-count ≥ 1 또는 최대 5초 대기 (M5)
# 유효 2줄 → row count == 2 기대, 하지만 quality_ok(≥15자) 이미 충족
for _ in $(seq 1 50); do
  cnt_a="$(MEM_STORE="$STOREa" python3 "$MEM" stats 2>/dev/null | grep -E '^\s+total:' | awk '{print $2}')"
  [ "${cnt_a:-0}" -ge 2 ] 2>/dev/null && break
  sleep 0.1
done
cnt_a="$(MEM_STORE="$STOREa" python3 "$MEM" stats 2>/dev/null | grep -E '^\s+total:' | awk '{print $2}')"
[ "${cnt_a:-0}" = "2" ] \
  && ok "test(a): row count == 2 (유효 2줄만 저장, malformed 5줄 skip)" \
  || bad "test(a): row count = ${cnt_a:-0}, 기대 2 (유효 2줄 저장 실패 or malformed 포함)"
rmdir "$STOREa/.distill-lock-$SIDa" 2>/dev/null || true

# ============================================================
# test (M3) — code-fence 출력 → 레코드 정상 파싱
# ============================================================
echo "== test(M3): code-fence 감싼 출력 → 내부 유효 줄만 레코드 생성 =="
SIDm3="dispatchsidm3"
STOREm3="$(mktemp -d)"; PROJm3="$(mktemp -d)"
STUBm3="$(mktemp -d)"
CLEANUP+=("$STOREm3" "$PROJm3" "$STUBm3")

enc_m3="$PROJm3/-home-fake-$SIDm3"; mkdir -p "$enc_m3"
cat > "$enc_m3/$SIDm3.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"test M3 prompt"},"uuid":"${SIDm3}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"test M3 reply content"}]},"uuid":"${SIDm3}a1","timestamp":"t2","isSidechain":false}
JSONL

# stub: ```json 펜스로 감싼 유효 JSON 2줄 출력 (첫줄 ```json, 닫는 ```)
cat > "$STUBm3/claude" <<'STUBEOF'
#!/bin/sh
printf '%s\n' '```json' \
              '{"tier":"durable","type":"decision","body":"fenced valid durable decision record","headline":"Fenced decision","aliases":[],"entities":[],"topics":[],"artifact_refs":[]}' \
              '{"tier":"working","type":"unresolved-obligation","body":"fenced valid working obligation record","headline":"Fenced obligation","aliases":[],"entities":[],"topics":[],"artifact_refs":[]}' \
              '```'
STUBEOF
chmod +x "$STUBm3/claude"

MEM_STORE="$STOREm3" MEM_PROJECTS="$PROJm3" MEM_DISTILL_ENABLE=1 PATH="$STUBm3:$PATH" \
  bash "$DISPATCH" distill "$SIDm3" "/tmp"

# 폴링: row count ≥ 1 대기 (내부 유효 2줄 기대)
for _ in $(seq 1 50); do
  cnt_m3="$(MEM_STORE="$STOREm3" python3 "$MEM" stats 2>/dev/null | grep -E '^\s+total:' | awk '{print $2}')"
  [ "${cnt_m3:-0}" -ge 2 ] 2>/dev/null && break
  sleep 0.1
done
cnt_m3="$(MEM_STORE="$STOREm3" python3 "$MEM" stats 2>/dev/null | grep -E '^\s+total:' | awk '{print $2}')"
[ "${cnt_m3:-0}" = "2" ] \
  && ok "test(M3): code-fence 감싼 출력 → 내부 유효 2줄 레코드 생성 (fence skip 정상)" \
  || bad "test(M3): row count = ${cnt_m3:-0}, 기대 2 (fence skip 실패 또는 bare json.loads 회귀)"
rmdir "$STOREm3/.distill-lock-$SIDm3" 2>/dev/null || true

# ============================================================
# test (M4) — 빈 출력 → 0 레코드 + marker 값 전진
# ============================================================
echo "== test(M4): 빈 stdout → 0 레코드, marker = fixture 마지막 uuid =="
SIDm4="dispatchsidm4"
STOREm4="$(mktemp -d)"; PROJm4="$(mktemp -d)"
STUBm4="$(mktemp -d)"
CLEANUP+=("$STOREm4" "$PROJm4" "$STUBm4")

enc_m4="$PROJm4/-home-fake-$SIDm4"; mkdir -p "$enc_m4"
# mkfix 가 쓰는 마지막 uuid = ${SIDm4}a1
cat > "$enc_m4/$SIDm4.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"test M4 prompt"},"uuid":"${SIDm4}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"test M4 reply"}]},"uuid":"${SIDm4}a1","timestamp":"t2","isSidechain":false}
JSONL
LAST_UUID_M4="${SIDm4}a1"

# stub: 빈 stdout (0줄)
printf '#!/bin/sh\n# empty output\n' > "$STUBm4/claude"
chmod +x "$STUBm4/claude"

MEM_STORE="$STOREm4" MEM_PROJECTS="$PROJm4" MEM_DISTILL_ENABLE=1 PATH="$STUBm4:$PATH" \
  bash "$DISPATCH" distill "$SIDm4" "/tmp"

# 폴링: marker state file 출현 또는 lock 소멸까지 bounded wait (최대 5초)
for _ in $(seq 1 50); do
  if [ -f "$STOREm4/.distill-state-$SIDm4" ]; then break; fi
  if [ ! -d "$STOREm4/.distill-lock-$SIDm4" ]; then
    # lock 이 사라졌으면 child 완료 — 한 번 더 대기 후 break
    sleep 0.2; break
  fi
  sleep 0.1
done

cnt_m4="$(MEM_STORE="$STOREm4" python3 "$MEM" stats 2>/dev/null | grep -E '^\s+total:' | awk '{print $2}')"
[ "${cnt_m4:-0}" = "0" ] \
  && ok "test(M4): 빈 출력 → row count == 0" \
  || bad "test(M4): row count = ${cnt_m4:-0}, 기대 0"

# marker 값 단언: state file 이 fixture 의 마지막 uuid 와 일치해야 함 (v8 신규 계약)
marker_val="$(cat "$STOREm4/.distill-state-$SIDm4" 2>/dev/null || true)"
[ "$marker_val" = "$LAST_UUID_M4" ] \
  && ok "test(M4): marker 값 = $LAST_UUID_M4 (빈 출력에도 advance 완료)" \
  || bad "test(M4): marker 값 = '${marker_val}', 기대 '$LAST_UUID_M4' (advance 미실행 또는 값 불일치)"
rmdir "$STOREm4/.distill-lock-$SIDm4" 2>/dev/null || true

# ============================================================
# test (b) — injection-in-body: body 에 셸 주입 payload → 레코드 저장, sentinel 미생성
# ============================================================
echo "== test(b): injection-in-body → record stored (row-count), sentinel NOT created (D-14) =="
SIDb="dispatchsidb"
STOREb="$(mktemp -d)"; PROJb="$(mktemp -d)"
STUBb="$(mktemp -d)"
SENTINEL_B="/tmp/cc-v8-inject-$$-$RANDOM"
CLEANUP+=("$STOREb" "$PROJb" "$STUBb")  # SENTINEL_B 는 위 단일 trap 의 rm -f 가 커버

enc_b="$PROJb/-home-fake-$SIDb"; mkdir -p "$enc_b"
cat > "$enc_b/$SIDb.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"test (b) injection prompt"},"uuid":"${SIDb}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"test (b) injection reply"}]},"uuid":"${SIDb}a1","timestamp":"t2","isSidechain":false}
JSONL

# stub: body 에 셸 주입 payload 포함 — body ≥15자(quality_ok 통과), IF body 가 sh -c/eval 됐다면 sentinel 생성
# SENTINEL_B 는 아직 미생성 상태 (사전 생성 안 함).
# python -c 로 JSON 직렬화: 셸 이스케이프 중첩 없이 body 안 특수문자를 안전하게 인코딩.
cat > "$STUBb/claude" <<STUBEOF
#!/bin/sh
python3 -c "
import json
payload = {'tier': 'durable', 'type': 'decision',
           'body': 'normal text \"; touch $SENTINEL_B ; echo \"',
           'headline': 'Injection safety decision', 'aliases': [], 'entities': [],
           'topics': [], 'artifact_refs': []}
print(json.dumps(payload))
"
STUBEOF
chmod +x "$STUBb/claude"

MEM_STORE="$STOREb" MEM_PROJECTS="$PROJb" MEM_DISTILL_ENABLE=1 PATH="$STUBb:$PATH" \
  bash "$DISPATCH" distill "$SIDb" "/tmp"

# 폴링: row count ≥ 1 (레코드 저장 완료 대기) 또는 lock 소멸 대기 — M5 row-count 폴링
for _ in $(seq 1 50); do
  cnt_b="$(MEM_STORE="$STOREb" python3 "$MEM" stats 2>/dev/null | grep -E '^\s+total:' | awk '{print $2}')"
  [ "${cnt_b:-0}" -ge 1 ] 2>/dev/null && break
  # lock 소멸이면 child 완료 (0-record 경로 포함)
  if [ ! -d "$STOREb/.distill-lock-$SIDb" ]; then sleep 0.2; break; fi
  sleep 0.1
done

cnt_b="$(MEM_STORE="$STOREb" python3 "$MEM" stats 2>/dev/null | grep -E '^\s+total:' | awk '{print $2}')"
# S6 단언 (1): 레코드 저장됨 — DB row-count == 1 (INJECTION_PAT 플래그 여부 무관, row-count 가 stable signal)
[ "${cnt_b:-0}" = "1" ] \
  && ok "test(b): injection body → 레코드 저장됨 (row count == 1, body-token match 아님)" \
  || bad "test(b): row count = ${cnt_b:-0}, 기대 1 (injection body 저장 실패)"
# S6 단언 (2): sentinel 미생성 — body 가 sh -c/eval 된 적 없음 (D-14 핵심 단언)
[ ! -f "$SENTINEL_B" ] \
  && ok "test(b): sentinel 미생성 — injection body 셸 미실행 (D-14 단위 단언 PASS)" \
  || bad "test(b): sentinel 파일 존재! body 가 셸 실행됨 (D-14 위반 — CRITICAL)"
rm -f "$SENTINEL_B" 2>/dev/null || true
rmdir "$STOREb/.distill-lock-$SIDb" 2>/dev/null || true

# ============================================================
# γ (Phase E-γ, D-18) — 2-mode model branch · opus no-tools · opt-in no-op · action parser · S2b membership
# ============================================================

# ⑧ mode→model. ⑥ above is the ARGUMENT-mode(fast distiller) regression anchor (DO NOT remove): it passes
#   post-γ regardless of the SessionEnd change, so this ⑧ is the REQUIRED SessionEnd(opus) pair (CP1/BR-1).
echo "== ⑧ mode→model: SessionEnd(stdin-JSON)=deep curator model =="
SID8="dispatchsidg8"
mkfix "$SID8"
rm -f "$STUBCAP/ARGV"
printf '{"session_id":"%s","cwd":"/tmp"}' "$SID8" \
  | MEM_DISTILL_ENABLE=1 PATH="$STUBCAP:$PATH" bash "$DISPATCH"
for _ in $(seq 1 50); do [ -s "$STUBCAP/ARGV" ] && break; sleep 0.1; done
argv8="$(cat "$STUBCAP/ARGV" 2>/dev/null || true)"
printf '%s\n' "$argv8" | grep -qx -- "deep-curator" \
  && ok "⑧ SessionEnd(stdin-JSON) → model role deep-curator (argument 모드 fast role = ⑥ anchor)" \
  || bad "⑧ SessionEnd model role != deep-curator: [$argv8]"
printf '%s\n' "$argv8" | grep -qx -- "curate" \
  && ok "⑧ SessionEnd(stdin-JSON) → mode curate" \
  || bad "⑧ SessionEnd mode != curate: [$argv8]"
rmdir "$STORE/.distill-lock-$SID8" 2>/dev/null || true; rm -f "$STORE/.distill-snapids-$SID8" 2>/dev/null || true

# opt-in no-op (Sec-🟡-8): ENABLE unset → SessionEnd 경로 완전 no-op (claude 분사 X, curate-snapshot X)
# fresh stub dir (공유 STUBBIN 의 prior CLAUDE_CALLED 누적·detached race 회피 — 결정성).
echo "== opt-in: MEM_DISTILL_ENABLE unset → SessionEnd no-op =="
SID9="dispatchsidg9"
mkfix "$SID9"
STUBOPT="$(mktemp -d)"; CLEANUP+=("$STUBOPT")
printf '#!/bin/sh\ntouch "%s/CLAUDE_CALLED"\n' "$STUBOPT" > "$STUBOPT/claude"; chmod +x "$STUBOPT/claude"
# env -u: 앰비언트 세션이 MEM_DISTILL_ENABLE=1 을 export 했어도(프로덕션 상태) 이 케이스는 unset 강제.
printf '{"session_id":"%s","cwd":"/tmp"}' "$SID9" | env -u MEM_DISTILL_ENABLE PATH="$STUBOPT:$PATH" bash "$DISPATCH" || true
sleep 0.3
[ ! -f "$STUBOPT/CLAUDE_CALLED" ] \
  && ok "opt-in: ENABLE unset → claude 미분사 (SessionEnd no-op)" || bad "opt-in: ENABLE unset 인데 claude 분사됨"
[ ! -f "$STORE/.distill-snapids-$SID9" ] \
  && ok "opt-in: ENABLE unset → snapids 미생성 (curate-snapshot 미실행)" || bad "opt-in: snapids 생성됨"

# ④ action-JSON body injection (curate add) → 셸 payload 미실행 + 레코드 저장 (argv-only)
echo "== ④ action-JSON body injection (curate add) → 셸 미실행 + 레코드 저장 =="
SIDg4="dispatchsidg4"
STOREg4="$(mktemp -d)"; PROJg4="$(mktemp -d)"; STUBg4="$(mktemp -d)"
CLEANUP+=("$STOREg4" "$PROJg4" "$STUBg4")
SENTINEL_G4="$STOREg4/SENTINEL_G4"
enc_g4="$PROJg4/-home-fake-$SIDg4"; mkdir -p "$enc_g4"
cat > "$enc_g4/$SIDg4.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"g4 prompt"},"uuid":"${SIDg4}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"g4 reply"}]},"uuid":"${SIDg4}a1","timestamp":"t2","isSidechain":false}
JSONL
# stub: action JSON add with shell payload in body (python -c → 안전한 JSON 직렬화, 셸 이스케이프 중첩 회피)
cat > "$STUBg4/claude" <<STUBEOF
#!/bin/sh
python3 -c "
import json
print(json.dumps({'action':'add','tier':'durable','type':'decision',
                  'body':'normal text \"; touch $SENTINEL_G4 ; echo \"',
                  'headline':'Curator injection decision','aliases':[],'entities':[],
                  'topics':[],'artifact_refs':[]}))
"
STUBEOF
chmod +x "$STUBg4/claude"
printf '{"session_id":"%s","cwd":"/tmp"}' "$SIDg4" \
  | MEM_STORE="$STOREg4" MEM_PROJECTS="$PROJg4" MEM_DISTILL_ENABLE=1 PATH="$STUBg4:$PATH" bash "$DISPATCH"
for _ in $(seq 1 50); do
  cntg4="$(MEM_STORE="$STOREg4" python3 "$MEM" stats 2>/dev/null | grep -E '^\s+total:' | awk '{print $2}')"
  [ "${cntg4:-0}" -ge 1 ] 2>/dev/null && break
  [ ! -d "$STOREg4/.distill-lock-$SIDg4" ] && { sleep 0.2; break; }
  sleep 0.1
done
cntg4="$(MEM_STORE="$STOREg4" python3 "$MEM" stats 2>/dev/null | grep -E '^\s+total:' | awk '{print $2}')"
[ "${cntg4:-0}" = "1" ] && ok "④ curate add (action JSON) → 레코드 저장 (row==1)" || bad "④ curate add row=${cntg4:-0} (기대 1)"
[ ! -f "$SENTINEL_G4" ] && ok "④ sentinel 미생성 — action body 셸 미실행 (argv-only)" || bad "④ sentinel 생성! body 셸 실행됨 (CRITICAL)"
rmdir "$STOREg4/.distill-lock-$SIDg4" 2>/dev/null || true

# INJ-2: prune injection id → 셸 미실행 + prune 실제 시도(nonexistent 거부, false-green 아님)
echo "== INJ-2 prune injection id → 셸 미실행 + prune 시도(거부) =="
STOREi2="$(mktemp -d)"; CLEANUP+=("$STOREi2")
SENTINEL_I2="$STOREi2/SENTINEL_I2"
out_i2="$(MEM_STORE="$STOREi2" python3 "$MEM" prune "x\"; touch $SENTINEL_I2; echo \"" 2>&1)"; rc=$?
[ ! -f "$SENTINEL_I2" ] && ok "INJ-2: sentinel 미생성 — prune id argv-only (셸 미실행)" || bad "INJ-2: sentinel 생성! prune id 셸 실행됨 (CRITICAL)"
{ [ "$rc" = 1 ] && printf '%s' "$out_i2" | grep -q "refused"; } \
  && ok "INJ-2: prune 실제 시도됨 — 전체 문자열 1개 id argv 로 게이트 거부(false-green 아님)" \
  || bad "INJ-2: prune 거부 미발생 (rc=$rc out=[$out_i2])"

# action routing + S2b membership: in-snapshot prune 실행 / non-snapshot prune skip
echo "== action routing + S2b membership: in-snapshot prune 실행 · non-snapshot skip =="
SIDg5="dispatchsidg5"
STOREg5="$(mktemp -d)"; PROJg5="$(mktemp -d)"; STUBg5="$(mktemp -d)"
CLEANUP+=("$STOREg5" "$PROJg5" "$STUBg5")
enc_g5="$PROJg5/-home-fake-$SIDg5"; mkdir -p "$enc_g5"
cat > "$enc_g5/$SIDg5.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"g5 prompt"},"uuid":"${SIDg5}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"g5 reply"}]},"uuid":"${SIDg5}a1","timestamp":"t2","isSidechain":false}
JSONL
# seed a durable in /tmp's project (curate-snapshot for cwd=/tmp 이 포함 → membership 허용)
( cd /tmp && MEM_STORE="$STOREg5" python3 "$MEM" add durable lesson \
    "membership routing target durable body content here" >/dev/null 2>&1 )
RID5="$(python3 - "$STOREg5/memory.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
r = c.execute("SELECT id FROM records WHERE tier='durable'").fetchone()
print(r[0] if r else "")
PY
)"
cat > "$STUBg5/claude" <<STUBEOF
#!/bin/sh
printf '%s\n' '{"action":"prune","id":"$RID5"}' '{"action":"prune","id":"ghost_not_in_snapshot_xyz"}'
STUBEOF
chmod +x "$STUBg5/claude"
printf '{"session_id":"%s","cwd":"/tmp"}' "$SIDg5" \
  | MEM_STORE="$STOREg5" MEM_PROJECTS="$PROJg5" MEM_DISTILL_ENABLE=1 PATH="$STUBg5:$PATH" bash "$DISPATCH"
for _ in $(seq 1 50); do [ ! -d "$STOREg5/.distill-lock-$SIDg5" ] && break; sleep 0.1; done
sleep 0.2
gone5="$(python3 - "$STOREg5/memory.db" "$RID5" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute("SELECT COUNT(*) FROM records WHERE id=?", (sys.argv[2],)).fetchone()[0])
PY
)"
[ -n "$RID5" ] && [ "$gone5" = 0 ] \
  && ok "routing: in-snapshot prune action 실행됨 (RID 삭제)" || bad "routing: in-snapshot prune 미실행 (RID5=[$RID5] gone=$gone5)"
[ -f "$STOREg5/deleted-records.jsonl" ] && grep -q "$RID5" "$STOREg5/deleted-records.jsonl" \
  && ok "routing: prune graveyard 기록됨 (S2b in-snapshot 경로)" || bad "routing: graveyard 미기록"
rmdir "$STOREg5/.distill-lock-$SIDg5" 2>/dev/null || true

# D-35 pending handoff boundary: mem.py exposes protected records in a separate
# snapshot section and omits them from IDS. The applier must treat IDS as the
# destructive allowlist, and every runtime prompt must preserve that contract.
echo "== D-35 pending handoff/thread → destructive IDS 제외 + prompt parity =="
prompt_contract_ok=1
for f in "$DISPATCH" "$CLAUDE_DISPATCH" "$CODEX_DISTILL" "$OPENCODE_DISTILL"; do
  grep -q 'PROTECTED PENDING' "$f" || prompt_contract_ok=0
done
if [ "$prompt_contract_ok" = 1 ]; then
  ok "D-35: Claude canonical/delta + Codex/OpenCode prompts name PROTECTED PENDING"
else
  bad "D-35: a runtime distill prompt is missing PROTECTED PENDING"
fi
# A4 added a curate branch that legitimately names destructive actions
# (prune/merge/graduate/reattribute), so the increment/add-only assertion must
# scope to the increment prompt heredoc only, not the whole worker file.
opencode_increment_prompt=$(awk '
  /You are a memory distillation worker\./ { flag=1 }
  flag { print }
  flag && /^EOF$/ { exit }
' "$OPENCODE_DISTILL")
if ! printf '%s\n' "$opencode_increment_prompt" | grep -q '"action":"prune"' \
  && printf '%s\n' "$opencode_increment_prompt" | grep -q 'increment/add-only'; then
  ok "D-35: OpenCode increment prompt exposes add-only actions"
else
  bad "D-35: OpenCode increment prompt still exposes destructive actions"
fi
opencode_curate_prompt=$(awk '
  /You are a no-tools session memory curator\./ { flag=1 }
  flag { print }
  flag && /^EOF$/ { exit }
' "$OPENCODE_DISTILL")
if printf '%s\n' "$opencode_curate_prompt" | grep -q '"action":"add"' \
  && printf '%s\n' "$opencode_curate_prompt" | grep -q '"action":"reinforce"' \
  && printf '%s\n' "$opencode_curate_prompt" | grep -q '"action":"merge"' \
  && printf '%s\n' "$opencode_curate_prompt" | grep -q '"action":"prune"' \
  && printf '%s\n' "$opencode_curate_prompt" | grep -q '"action":"graduate"' \
  && printf '%s\n' "$opencode_curate_prompt" | grep -q '"action":"reattribute"' \
  && ! printf '%s\n' "$opencode_curate_prompt" | grep -q '"action":"delete"' \
  && ! printf '%s\n' "$opencode_curate_prompt" | grep -q '"action":"consume"'; then
  ok "A4: OpenCode curate prompt names exactly the six allowed actions"
else
  bad "A4: OpenCode curate prompt should name exactly the six allowed actions and no delete/consume"
fi

echo "== A4 OpenCode distill-worker mode/lock/advance =="
if "$OPENCODE_DISTILL" -h 2>&1 | grep -q 'increment|curate'; then
  ok "A4: OpenCode distill-worker usage documents increment|curate"
else
  bad "A4: OpenCode distill-worker usage should document increment|curate"
fi
if OPENCODE_DISTILL_ENABLE=1 "$OPENCODE_DISTILL" a4-badmode /tmp bogus-mode >/tmp/a4_badmode.out 2>/tmp/a4_badmode.err; then
  bad "A4: OpenCode distill-worker should reject an unknown mode"
else
  [ "$?" -eq 64 ] && ok "A4: OpenCode distill-worker rejects an unknown mode (exit 64)" \
    || bad "A4: OpenCode distill-worker wrong exit for unknown mode"
fi
if MEM_DISTILL=1 OPENCODE_DISTILL_ENABLE=1 "$OPENCODE_DISTILL" a4-recursion /tmp increment >/tmp/a4_recursion.out 2>/tmp/a4_recursion.err \
  && [ ! -s /tmp/a4_recursion.out ]; then
  ok "A4: OpenCode distill-worker MEM_DISTILL=1 stays a no-op"
else
  bad "A4: OpenCode distill-worker should stay a no-op under MEM_DISTILL=1"
fi
if "$OPENCODE_DISTILL" a4-disabled /tmp increment >/tmp/a4_disabled.out 2>/tmp/a4_disabled.err \
  && [ ! -s /tmp/a4_disabled.out ]; then
  ok "A4: OpenCode distill-worker exits 0 with no output when OPENCODE_DISTILL_ENABLE is unset"
else
  bad "A4: OpenCode distill-worker should exit 0 silently when OPENCODE_DISTILL_ENABLE is unset"
fi

PEND_TMP="$(mktemp -d)"; CLEANUP+=("$PEND_TMP")
cat > "$PEND_TMP/actions.jsonl" <<'JSONL'
{"action":"prune","id":"pending_handoff"}
{"action":"merge","ids":["allowed_record","pending_thread"],"canonical":"allowed_record"}
{"action":"delete","id":"pending_handoff"}
{"action":"prune","id":"allowed_record"}
JSONL
printf '%s\n' "allowed_record" > "$PEND_TMP/destructive-ids"
cat > "$PEND_TMP/mem-stub.py" <<'PY'
import os
import sys
with open(os.environ["PENDING_CALLS"], "a", encoding="utf-8") as fh:
    fh.write("|".join(sys.argv[1:]) + "\n")
PY
PENDING_CALLS="$PEND_TMP/calls" python3 "$APPLIER" \
  "$PEND_TMP/actions.jsonl" "$PEND_TMP/mem-stub.py" \
  --mode curate --snapshot-ids "$PEND_TMP/destructive-ids" \
  2> "$PEND_TMP/applier.err"
calls="$(cat "$PEND_TMP/calls" 2>/dev/null || true)"
if [ "$calls" = "prune|allowed_record" ] \
  && grep -q 'non-destructive-allowlist id (prune)' "$PEND_TMP/applier.err" \
  && grep -q 'id outside destructive allowlist' "$PEND_TMP/applier.err" \
  && grep -q 'unsupported destructive action' "$PEND_TMP/applier.err"; then
  ok "D-35: pending prune/merge/delete skipped; destructive IDS member alone reaches mem.py"
else
  bad "D-35: applier destructive allowlist leak (calls=[$calls])"
fi

# ============================================================
# F-3 — bounded worker stderr (.distill-err-<sid>) + GC glob coverage
# ============================================================
echo "== F-3: worker stderr is captured, capped at 64 KiB, GC glob includes .distill-err-* =="

for f in "$DISPATCH" "$CLAUDE_DISPATCH"; do
  grep -q "name '.distill-err-\*'" "$f" \
    && ok "F-3: 60-minute stale GC glob includes .distill-err-* ($f)" \
    || bad "F-3: 60-minute stale GC glob missing .distill-err-* ($f)"
done

STOREf3="$(mktemp -d)"; PROJf3="$(mktemp -d)"; STUBf3="$(mktemp -d)"
CLEANUP+=("$STOREf3" "$PROJf3" "$STUBf3")
# Worker stub: writes exactly 65537 bytes of stderr, then fails.
cat > "$STUBf3/claude" <<'STUBEOF'
#!/bin/sh
head -c 65537 /dev/zero | tr '\0' 'x' >&2
exit 1
STUBEOF
chmod +x "$STUBf3/claude"

f3_mkfix() {  # $1=store $2=proj $3=sid
  enc="$2/-home-fake-$3"; mkdir -p "$enc"
  cat > "$enc/$3.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"F-3 prompt $3"},"uuid":"${3}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"F-3 reply $3"}]},"uuid":"${3}a1","timestamp":"t2","isSidechain":false}
JSONL
}

f3_run() {  # $1=dispatcher path $2=store $3=proj $4=sid
  MEM_STORE="$2" MEM_PROJECTS="$3" MEM_DISTILL_ENABLE=1 PATH="$STUBf3:$PATH" \
    bash "$1" distill "$4" "/tmp"
  # detached child — poll for lock release before inspecting the error log
  for _ in $(seq 1 50); do [ ! -d "$2/.distill-lock-$4" ] && break; sleep 0.1; done
}

for pair in "$DISPATCH|hooks" "$CLAUDE_DISPATCH|claude-adapter"; do
  disp="${pair%%|*}"; label="${pair##*|}"
  sidf3="f3-$label"
  f3_mkfix "$STOREf3" "$PROJf3" "$sidf3"
  f3_run "$disp" "$STOREf3" "$PROJf3" "$sidf3"
  size1="$(wc -c < "$STOREf3/.distill-err-$sidf3" 2>/dev/null || echo -1)"
  if [ "${size1:--1}" -ge 0 ] 2>/dev/null && [ "$size1" -le 65536 ] 2>/dev/null; then
    ok "F-3 ($label): single 65537-byte stderr run capped at <= 65536 bytes (size=$size1)"
  else
    bad "F-3 ($label): .distill-err-$sidf3 size=$size1, expected 0..65536"
  fi

  # Same SID, second consecutive failure — must not accumulate past the cap.
  f3_mkfix "$STOREf3" "$PROJf3" "$sidf3"
  f3_run "$disp" "$STOREf3" "$PROJf3" "$sidf3"
  size2="$(wc -c < "$STOREf3/.distill-err-$sidf3" 2>/dev/null || echo -1)"
  if [ "${size2:--1}" -ge 0 ] 2>/dev/null && [ "$size2" -le 65536 ] 2>/dev/null; then
    ok "F-3 ($label): two consecutive 65537-byte stderr runs stay <= 65536 bytes (size=$size2)"
  else
    bad "F-3 ($label): .distill-err-$sidf3 size=$size2 after 2nd run, expected 0..65536"
  fi
done

# ============================================================
# R-2 — bounded-retry (D): marker advance gated on worker/governor exit code
# ============================================================
echo "== R-2: bounded-retry — marker non-advance on failure, forced-advance at 3 strikes =="

for f in "$DISPATCH" "$CLAUDE_DISPATCH"; do
  grep -q "name '.distill-fail-\*'" "$f" \
    && ok "R-2: 60-minute stale GC glob includes .distill-fail-* ($f)" \
    || bad "R-2: 60-minute stale GC glob missing .distill-fail-* ($f)"
done

r2_mkfix() {  # $1=store $2=proj $3=sid
  # Each test case is one independent dispatch contract; reset the rolling
  # start budget so a prior case's leases cannot starve this one (mirrors mkfix).
  rm -rf "$1"/.distill-budget-* 2>/dev/null || true
  enc="$2/-home-fake-$3"; mkdir -p "$enc"
  cat > "$enc/$3.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"R-2 prompt $3"},"uuid":"${3}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"R-2 reply $3"}]},"uuid":"${3}a1","timestamp":"t2","isSidechain":false}
JSONL
}

r2_run() {  # $1=dispatcher path $2=store $3=proj $4=sid $5=stub-bin-dir $6=cwd
  # Isolated governor root: R-2 asserts on the real worker_rc, so this block
  # must not inherit rolling-start-budget exhaustion from earlier cases in
  # this large test file (a governor rejection is not a worker failure).
  MEM_STORE="$2" MEM_PROJECTS="$3" MEM_DISTILL_ENABLE=1 PATH="$5:$PATH" \
    AGENT_MODEL_GOVERNOR_ROOT="$2/.test-model-governor" \
    bash "$1" distill "$4" "${6:-/tmp}"
  for _ in $(seq 1 50); do [ ! -d "$2/.distill-lock-$4" ] && break; sleep 0.1; done
}

STOREr2="$(mktemp -d)"; PROJr2="$(mktemp -d)"
STUBr2fail="$(mktemp -d)"; STUBr2ok="$(mktemp -d)"
CLEANUP+=("$STOREr2" "$PROJr2" "$STUBr2fail" "$STUBr2ok")
printf '#!/bin/sh\nexit 1\n' > "$STUBr2fail/claude"; chmod +x "$STUBr2fail/claude"
printf '#!/bin/sh\n# empty output, success\n' > "$STUBr2ok/claude"; chmod +x "$STUBr2ok/claude"

for pair in "$DISPATCH|hooks" "$CLAUDE_DISPATCH|claude-adapter"; do
  disp="${pair%%|*}"; label="${pair##*|}"

  # ① single failure: marker non-advance, counter == 1, one failure-log line
  sid1="r2-fail1-$label"
  r2_mkfix "$STOREr2" "$PROJr2" "$sid1"
  r2_run "$disp" "$STOREr2" "$PROJr2" "$sid1" "$STUBr2fail"
  [ ! -f "$STOREr2/.distill-state-$sid1" ] \
    && ok "R-2① ($label): single failure → marker NOT advanced" \
    || bad "R-2① ($label): marker advanced on a failed worker run"
  cnt1="$(cat "$STOREr2/.distill-fail-$sid1" 2>/dev/null || echo '?')"
  [ "$cnt1" = "1" ] \
    && ok "R-2① ($label): .distill-fail-$sid1 == 1" \
    || bad "R-2① ($label): .distill-fail-$sid1 = '$cnt1', expected 1"
  loglines1="$(wc -l < "$STOREr2/.distill-failures.log" 2>/dev/null || echo 0)"
  [ "${loglines1:-0}" -ge 1 ] 2>/dev/null \
    && ok "R-2① ($label): .distill-failures.log has >=1 line after first failure" \
    || bad "R-2① ($label): .distill-failures.log line count = ${loglines1:-0}"

  # ② two more consecutive failures on the same sid → 3rd strike forces the advance
  r2_run "$disp" "$STOREr2" "$PROJr2" "$sid1" "$STUBr2fail"
  r2_run "$disp" "$STOREr2" "$PROJr2" "$sid1" "$STUBr2fail"
  [ -f "$STOREr2/.distill-state-$sid1" ] \
    && ok "R-2② ($label): 3 consecutive failures → forced marker advance" \
    || bad "R-2② ($label): marker still not advanced after 3 strikes"
  [ ! -f "$STOREr2/.distill-fail-$sid1" ] \
    && ok "R-2② ($label): .distill-fail-$sid1 counter deleted after forced advance" \
    || bad "R-2② ($label): .distill-fail-$sid1 counter still present after forced advance"
  grep -q 'forced-advance' "$STOREr2/.distill-failures.log" 2>/dev/null \
    && ok "R-2② ($label): .distill-failures.log records forced-advance" \
    || bad "R-2② ($label): .distill-failures.log missing forced-advance entry"

  # ③ exit 0 + empty stdout (normal zero-output) → marker advances, no counter file
  sid3="r2-zero-$label"
  r2_mkfix "$STOREr2" "$PROJr2" "$sid3"
  r2_run "$disp" "$STOREr2" "$PROJr2" "$sid3" "$STUBr2ok"
  [ -f "$STOREr2/.distill-state-$sid3" ] \
    && ok "R-2③ ($label): exit0+empty stdout → marker advances (normal zero-output)" \
    || bad "R-2③ ($label): marker not advanced on normal zero-output"
  [ ! -f "$STOREr2/.distill-fail-$sid3" ] \
    && ok "R-2③ ($label): no .distill-fail-* counter created on success" \
    || bad "R-2③ ($label): .distill-fail-* counter unexpectedly created on success"

  # ④ failure then success on the same sid → counter resets (deleted)
  sid4="r2-reset-$label"
  r2_mkfix "$STOREr2" "$PROJr2" "$sid4"
  r2_run "$disp" "$STOREr2" "$PROJr2" "$sid4" "$STUBr2fail"
  [ -f "$STOREr2/.distill-fail-$sid4" ] \
    || bad "R-2④ ($label): counter missing after the setup failure"
  r2_run "$disp" "$STOREr2" "$PROJr2" "$sid4" "$STUBr2ok"
  [ ! -f "$STOREr2/.distill-fail-$sid4" ] \
    && ok "R-2④ ($label): failure then success resets (deletes) the counter" \
    || bad "R-2④ ($label): .distill-fail-$sid4 still present after a later success"
  [ -f "$STOREr2/.distill-state-$sid4" ] \
    && ok "R-2④ ($label): the later successful run advances the marker" \
    || bad "R-2④ ($label): marker not advanced by the later successful run"
done

# ⑦ .distill-failures.log rotation — pre-seed past the 256 KiB bound, run once, expect <= 262144
for pair in "$DISPATCH|hooks" "$CLAUDE_DISPATCH|claude-adapter"; do
  disp="${pair%%|*}"; label="${pair##*|}"
  STOREr2rot="$(mktemp -d)"; PROJr2rot="$(mktemp -d)"
  CLEANUP+=("$STOREr2rot" "$PROJr2rot")
  head -c 262145 /dev/zero | tr '\0' 'x' > "$STOREr2rot/.distill-failures.log"
  sidrot="r2-rotate-$label"
  r2_mkfix "$STOREr2rot" "$PROJr2rot" "$sidrot"
  r2_run "$disp" "$STOREr2rot" "$PROJr2rot" "$sidrot" "$STUBr2fail"
  rotsize="$(wc -c < "$STOREr2rot/.distill-failures.log" 2>/dev/null || echo -1)"
  if [ "${rotsize:--1}" -ge 0 ] 2>/dev/null && [ "$rotsize" -le 262144 ] 2>/dev/null; then
    ok "R-2⑦ ($label): .distill-failures.log rotated to <= 262144 bytes (size=$rotsize)"
  else
    bad "R-2⑦ ($label): .distill-failures.log size=$rotsize, expected 0..262144"
  fi
done

# ============================================================
# G-75 — inherited foreign governor reservation must not poison distillation
# ============================================================
echo "== G-75: dispatcher drops an inherited absent reservation before distill admission =="

STOREg75="$(mktemp -d)"; PROJg75="$(mktemp -d)"; STUBg75="$(mktemp -d)"
CLEANUP+=("$STOREg75" "$PROJg75" "$STUBg75")
cat > "$STUBg75/distill-worker" <<'STUBEOF'
#!/bin/sh
json='{"tier":"working","type":"decision","body":"g75","headline":"g75","aliases":["g75"],"entities":["G-75"],"topics":["memory-pipeline"],"artifact_refs":[]}'
printf '%s\n' "$json" | tee "$DISTILL_JSON_CAPTURE"
touch "$DISTILL_WORKER_CALLED"
STUBEOF
chmod +x "$STUBg75/distill-worker"

for pair in "$DISPATCH|hooks" "$CLAUDE_DISPATCH|claude-adapter"; do
  disp="${pair%%|*}"; label="${pair##*|}"; sid="g75-$label"
  rm -rf "$STOREg75"/.distill-budget-* "$STOREg75/.governor" 2>/dev/null || true
  enc="$PROJg75/-home-fake-$sid"; mkdir -p "$enc"
  cat > "$enc/$sid.jsonl" <<JSONL
{"type":"user","message":{"role":"user","content":"G-75 prompt"},"uuid":"${sid}u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"G-75 reply"}]},"uuid":"${sid}a1","timestamp":"t2","isSidechain":false}
JSONL
  capture="$STOREg75/$sid.jsonl.out"; called="$STOREg75/$sid.called"
  MEM_STORE="$STOREg75" MEM_PROJECTS="$PROJg75" MEM_DISTILL_ENABLE=1 \
    MEM_DISTILL_WORKER="$STUBg75/distill-worker" \
    DISTILL_JSON_CAPTURE="$capture" DISTILL_WORKER_CALLED="$called" \
    AGENT_MODEL_GOVERNOR_ROOT="$STOREg75/.governor" \
    AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN=00000000000000000000000000000000 \
    bash "$disp" distill "$sid" /tmp
  for _ in $(seq 1 50); do [ ! -d "$STOREg75/.distill-lock-$sid" ] && break; sleep 0.1; done
  if [ -f "$called" ] && grep -q '"headline":"g75"' "$capture" 2>/dev/null \
    && [ -f "$STOREg75/.distill-state-$sid" ] \
    && ! grep -q " 75 increment $sid " "$STOREg75/.distill-failures.log" 2>/dev/null; then
    ok "G-75 ($label): argv dispatch runs stub, captures JSON, exits cleanly despite absent inherited token"
  else
    bad "G-75 ($label): inherited absent token still poisoned dispatcher/governor handshake"
  fi
done

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
