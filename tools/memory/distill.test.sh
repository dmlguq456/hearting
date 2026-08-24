#!/usr/bin/env bash
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
# Isolated test for `mem distill` + session distill adapter/ingest/marker (Cluster C, D-11~13).
# Fully isolated via MEM_STORE + MEM_PROJECTS temp dirs — never touches real runtime memory.
# All mem calls go through `python3 tools/memory/mem.py` subprocess (env evaluated at module load).
# Verification ①~⑤ from plan: 2026-06-16_memory-cluster-c-distill/plan/plan.md
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MEM="$ROOT/tools/memory/mem.py"
[ -f "$MEM" ] || { echo "FAIL: mem.py not found at $MEM"; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  ❌ %s\n' "$1"; }

# ---- isolated store/projects ----
STORE="$(mktemp -d)"; PROJ="$(mktemp -d)"
TMPSTUB=""  # 추가 케이스 A~C 에서 할당; trap 이 한 번만 등록되도록 여기서 선언
trap 'rm -rf "$STORE" "$PROJ" ${TMPSTUB:+"$TMPSTUB"}' EXIT
export MEM_STORE="$STORE" MEM_PROJECTS="$PROJ"

SID="distillsid"
ENC="$PROJ/-home-fake-cwd"   # enc_cwd folder name arbitrary — adapter globs */sid.jsonl
mkdir -p "$ENC"
FIX="$ENC/$SID.jsonl"

# ---- synthetic fixture jsonl (one JSON object per line, unique uuids) ----
cat > "$FIX" <<'JSONL'
{"type":"user","isMeta":true,"message":{"role":"user","content":"<system-reminder>injected meta</system-reminder>"},"uuid":"m1","timestamp":"t0","isSidechain":false}
{"type":"user","message":{"role":"user","content":"실제 사용자 프롬프트 ALPHA"},"uuid":"u1","timestamp":"t1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"thinking","thinking":"THINKINGSECRET"},{"type":"text","text":"assistant 답변 BETA"},{"type":"tool_use","name":"Bash","input":{}}]},"uuid":"a1","timestamp":"t2","isSidechain":false}
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"TOOLRESULTBODY"}]},"uuid":"u2","timestamp":"t3","isSidechain":false}
{"type":"user","message":{"role":"user","content":"사이드체인 무관 GAMMA"},"uuid":"s1","timestamp":"t4","isSidechain":true}
{"type":"summary","summary":"SUMMARYBODY","uuid":"x1"}
{"type":"user","message":{"role":"user","content":"마지막 실제 프롬프트 DELTA"},"uuid":"u3","timestamp":"t5","isSidechain":false}
JSONL

MARKER="$STORE/.distill-state-$SID"

echo "== Verification ① marker (empty state) =="
[ ! -e "$MARKER" ] && ok "read_marker empty: .distill-state file absent before any --advance" \
  || bad "marker file should not exist yet, found: $MARKER"

echo "== Verification ② adapter / ingest (normalization) =="
out="$(python3 "$MEM" distill "$SID")"
# salient bodies present
echo "$out" | grep -q "ALPHA" && ok "user str content ALPHA present" || bad "ALPHA missing: $out"
echo "$out" | grep -q "BETA"  && ok "assistant text BETA present"   || bad "BETA missing: $out"
echo "$out" | grep -q "DELTA" && ok "final user DELTA present"      || bad "DELTA missing: $out"
echo "$out" | grep -q '\[tool:Bash\]' && ok "tool_use → [tool:Bash] label present" || bad "[tool:Bash] missing: $out"
echo "$out" | grep -q '\[user\]' && ok "[user] prefix present" || bad "[user] prefix missing: $out"
echo "$out" | grep -q '\[assistant\]' && ok "[assistant] prefix present" || bad "[assistant] prefix missing: $out"
# excluded bodies absent
echo "$out" | grep -q "THINKINGSECRET"  && bad "thinking body leaked: $out"     || ok "thinking body excluded"
echo "$out" | grep -q "TOOLRESULTBODY"  && bad "tool_result body leaked: $out"  || ok "tool_result body excluded"
echo "$out" | grep -q "SUMMARYBODY"     && bad "non-role summary leaked: $out"  || ok "non-role (summary) body excluded"
echo "$out" | grep -q "GAMMA"           && bad "sidechain body leaked: $out"    || ok "sidechain body excluded"
echo "$out" | grep -q "injected meta"   && bad "isMeta body leaked: $out"       || ok "isMeta body excluded"

echo "== Verification ① marker advance → uuid of last real role msg (u3) =="
python3 "$MEM" distill "$SID" --advance >/dev/null
if [ -f "$MARKER" ]; then
  m="$(cat "$MARKER" | tr -d '[:space:]')"
  [ "$m" = "u3" ] && ok "marker == last real-role uuid (u3)" || bad "marker should be u3, got: '$m'"
else
  bad "marker file not created after --advance"
fi

echo "== Verification ① marker idempotent re-advance =="
python3 "$MEM" distill "$SID" --advance >/dev/null
m2="$(cat "$MARKER" | tr -d '[:space:]')"
[ "$m2" = "u3" ] && ok "marker unchanged after re-advance (idempotent)" || bad "marker changed to: '$m2'"

echo "== Verification ⑤ distill idempotent (empty after advance) =="
out2="$(python3 "$MEM" distill "$SID")"
[ -z "$out2" ] && ok "distill output empty after advance (no re-emission)" || bad "expected empty, got: $out2"

echo "== Verification ③ recursion guard (MEM_DISTILL=1 → immediate exit, no claude spawn) =="
# stub claude that creates a sentinel iff invoked
STUBBIN="$(mktemp -d)"
cat > "$STUBBIN/claude" <<EOF
#!/bin/sh
touch "$STUBBIN/CLAUDE_CALLED"
EOF
chmod +x "$STUBBIN/claude"
rc=0
echo '{"session_id":"x","cwd":"/tmp"}' \
  | MEM_DISTILL=1 PATH="$STUBBIN:$PATH" bash "$ROOT/hooks/mem-distill-dispatch.sh" || rc=$?
[ "$rc" = "0" ] && ok "recursion guard → exit 0" || bad "recursion guard exit code: $rc"
[ ! -e "$STUBBIN/CLAUDE_CALLED" ] && ok "claude NOT spawned under MEM_DISTILL=1 (sentinel absent)" \
  || bad "claude was spawned (sentinel present) — recursion guard failed"
rm -rf "$STUBBIN"

echo "== Verification ④ turn-nudge 11종 regression =="
nudge_out="$(bash "$ROOT/hooks/mem-turn-nudge.test.sh" 2>&1)" || true
echo "$nudge_out" | tail -3
echo "$nudge_out" | grep -q "RESULT: PASS=21 FAIL=0" \
  && ok "mem-turn-nudge.test.sh → PASS=21 FAIL=0" \
  || bad "turn-nudge regression not 21/0: $(echo "$nudge_out" | grep RESULT)"


# ============================================================
# 추가 케이스 A~G (QA 리뷰 accuracy 🔴 / codex / coverage 갭)
# ============================================================

# ---- 공통 sentinel stub (A~C 케이스용) ----
TMPSTUB="$(mktemp -d)"  # trap 은 파일 상단에 등록돼 있음; 여기서 재등록 X
mkdir -p "$TMPSTUB/bin"
printf '#!/bin/sh\ntouch "%s/CLAUDE_CALLED"\n' "$TMPSTUB" > "$TMPSTUB/bin/claude"
chmod +x "$TMPSTUB/bin/claude"
export MEM_DISTILL_WORKER=claude

# ---- A. 재귀가드 positive control + negative (③ 보완) ----
echo "== A. 재귀가드 — positive control + negative =="

# positive control 용 sid (delta 있는 상태 = advance 전)
SID_A="distillsid_a"
ENC_A="$PROJ/-home-fake-a"
mkdir -p "$ENC_A"
cat > "$ENC_A/$SID_A.jsonl" <<'JSONL'
{"type":"user","message":{"role":"user","content":"positive control prompt A"},"uuid":"pa1","timestamp":"ta1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"positive control reply A"}]},"uuid":"pa2","timestamp":"ta2","isSidechain":false}
JSONL

rm -f "$TMPSTUB/CLAUDE_CALLED"
# positive control: ENABLE=1, MEM_DISTILL 미설정, non-empty delta → dispatch 는 분사 경로 진입.
# v7 lock(Step 1.3): 분사 직전 `mkdir "$STORE/.distill-lock-$SID"` 를 spawn(&) *동기* 경로에서
# 하므로(claude 분사 전), dispatch 반환 시점에 lock dir 이 이미 존재한다. detached child sentinel
# (race) 대신 이 동기 lock 으로 분사 경로 진입을 단언 — RP-M1/RP-M2 동기 신호 패턴. dispatch.sh 가
# $HOME/.claude/.../mem.py 를 하드코딩하더라도 lock 은 MEM_STORE 격리 자리에 materialize 된다.
delta_a="$(python3 "$MEM" distill "$SID_A")"
LOCK_A="$STORE/.distill-lock-$SID_A"
rc_apos=0
echo "{\"session_id\":\"$SID_A\",\"cwd\":\"/tmp\"}" \
  | MEM_DISTILL_ENABLE=1 PATH="$TMPSTUB/bin:$PATH" bash "$ROOT/hooks/mem-distill-dispatch.sh" || rc_apos=$?
[ -n "${delta_a//[[:space:]]/}" ] \
  && ok "A-pos: non-empty delta 확인 (분사 진입 전제 조건 성립)" \
  || bad "A-pos: delta 가 비어 있음 — fixture 이상"
[ "$rc_apos" = "0" ] \
  && ok "A-pos: ENABLE=1 + non-empty delta + MEM_DISTILL 미설정 → dispatch exit 0 (분사 경로 통과)" \
  || bad "A-pos: dispatch 가 non-zero exit: $rc_apos"
# lock cleanup + acquire 단언 (RP-M2): rmdir 성공(rc 0) = "lock 이 실제 acquire 됐다" positive +
# detached child trap rmdir 가 아직 안 돌았어도 order-dependency 오염을 여기서 제거.
rmdir "$LOCK_A" 2>/dev/null
[ "$?" = "0" ] \
  && ok "A-pos: lock dir 실제 acquire 됨 (rmdir rc 0 — v7 lock 경로 진입 positive)" \
  || bad "A-pos: lock dir 미생성 — v7 lock acquire 경로 미진입"

# negative: MEM_DISTILL=1 (재귀가드) → 즉시 exit 0, sentinel ABSENT.
# A-neg 격리 (🔴-1): A-pos 의 detached `setsid claude &` 자식이 A-pos sentinel 을 async touch 할 수
# 있어, 공유 sentinel 이면 A-neg 의 rm 뒤 부활→FAIL(race). A-neg 전용 stub/sentinel 로 격리 —
# A-pos 의 lingering child 가 A-neg sentinel 을 절대 건드릴 수 없게.
STUB_NEG="$(mktemp -d)"
printf '#!/bin/sh\ntouch "%s/CLAUDE_CALLED_neg"\n' "$STUB_NEG" > "$STUB_NEG/claude"
chmod +x "$STUB_NEG/claude"
rc_a=0
echo "{\"session_id\":\"$SID_A\",\"cwd\":\"/tmp\"}" \
  | MEM_DISTILL=1 MEM_DISTILL_ENABLE=1 PATH="$STUB_NEG:$PATH" bash "$ROOT/hooks/mem-distill-dispatch.sh" || rc_a=$?
[ "$rc_a" = "0" ] && ok "A-neg: 재귀가드 → exit 0" || bad "A-neg: 재귀가드 exit code: $rc_a"
[ ! -e "$STUB_NEG/CLAUDE_CALLED_neg" ] \
  && ok "A-neg: MEM_DISTILL=1 → sentinel ABSENT (재귀가드 분사 차단, 격리 sentinel)" \
  || bad "A-neg: sentinel PRESENT — 재귀가드 실패"
rm -rf "$STUB_NEG"

# ---- B. opt-in 게이트 (MEM_DISTILL_ENABLE 미설정 → no-op) ----
echo "== B. opt-in 게이트 — MEM_DISTILL_ENABLE 미설정 → no-op =="

rm -f "$TMPSTUB/CLAUDE_CALLED"
rc_b=0
echo "{\"session_id\":\"$SID_A\",\"cwd\":\"/tmp\"}" \
  | PATH="$TMPSTUB/bin:$PATH" bash "$ROOT/hooks/mem-distill-dispatch.sh" || rc_b=$?
[ "$rc_b" = "0" ] && ok "B: ENABLE 미설정 → exit 0" || bad "B: exit code $rc_b"
[ ! -e "$TMPSTUB/CLAUDE_CALLED" ] \
  && ok "B: ENABLE 미설정 → sentinel ABSENT (게이트 no-op)" \
  || bad "B: sentinel PRESENT — opt-in 게이트 실패"

# ---- C. 빈-delta 분사 skip ----
echo "== C. 빈-delta — advance 완료 sid 분사 skip =="

SID_C="distillsid_c"
ENC_C="$PROJ/-home-fake-c"
mkdir -p "$ENC_C"
cat > "$ENC_C/$SID_C.jsonl" <<'JSONL'
{"type":"user","message":{"role":"user","content":"delta skip test prompt"},"uuid":"pc1","timestamp":"tc1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"delta skip reply"}]},"uuid":"pc2","timestamp":"tc2","isSidechain":false}
JSONL

# advance 해서 marker == pc2 (delta 비워버림)
python3 "$MEM" distill "$SID_C" --advance >/dev/null

# distill 출력이 완전 빈 문자열인지 직접 단언
out_c="$(python3 "$MEM" distill "$SID_C")"
[ -z "$out_c" ] && ok "C: advance 후 distill 출력 완전 빈 문자열 (no trailing \\n)" \
  || bad "C: distill 출력이 빈 문자열이 아님: '$out_c'"

rm -f "$TMPSTUB/CLAUDE_CALLED"
rc_c=0
echo "{\"session_id\":\"$SID_C\",\"cwd\":\"/tmp\"}" \
  | MEM_DISTILL_ENABLE=1 PATH="$TMPSTUB/bin:$PATH" bash "$ROOT/hooks/mem-distill-dispatch.sh" || rc_c=$?
[ "$rc_c" = "0" ] && ok "C: 빈-delta dispatch → exit 0" || bad "C: exit code $rc_c"
[ ! -e "$TMPSTUB/CLAUDE_CALLED" ] \
  && ok "C: 빈-delta → sentinel ABSENT (분사 skip)" \
  || bad "C: sentinel PRESENT — 빈-delta 임에도 claude 분사됨"

# ---- D. marker 라벨 정정 + sidechain-last ----
echo "== D. marker sidechain-last — marker 는 sidechain 포함 전 구간 끝까지 =="

SID_D="distillsid_d"
ENC_D="$PROJ/-home-fake-d"
mkdir -p "$ENC_D"
# 마지막 줄이 sidechain: marker 는 sidechain uuid sc1 이어야 함
cat > "$ENC_D/$SID_D.jsonl" <<'JSONL'
{"type":"user","message":{"role":"user","content":"sidechain last prompt"},"uuid":"pd1","timestamp":"td1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"sidechain last reply"}]},"uuid":"pd2","timestamp":"td2","isSidechain":false}
{"type":"user","message":{"role":"user","content":"이건 sidechain 마지막 줄"},"uuid":"sc1","timestamp":"td3","isSidechain":true}
JSONL

MARKER_D="$STORE/.distill-state-$SID_D"
python3 "$MEM" distill "$SID_D" --advance >/dev/null
if [ -f "$MARKER_D" ]; then
  md="$(cat "$MARKER_D" | tr -d '[:space:]')"
  [ "$md" = "sc1" ] \
    && ok "D: marker == sc1 (sidechain 포함 전 구간 끝 uuid — last ingested uuid)" \
    || bad "D: marker should be sc1, got: '$md'"
else
  bad "D: marker 파일 미생성"
fi

# ---- E. None-uuid advance ----
echo "== E. None-uuid advance — 마지막 uuid=null 시 앞 유효 uuid(u9)로 advance =="

SID_E="distillsid_e"
ENC_E="$PROJ/-home-fake-e"
mkdir -p "$ENC_E"
cat > "$ENC_E/$SID_E.jsonl" <<'JSONL'
{"type":"user","message":{"role":"user","content":"none uuid before prompt"},"uuid":"u9","timestamp":"te1","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"none uuid reply"}]},"uuid":null,"timestamp":"te2","isSidechain":false}
JSONL

MARKER_E="$STORE/.distill-state-$SID_E"
python3 "$MEM" distill "$SID_E" --advance >/dev/null
if [ -f "$MARKER_E" ]; then
  me="$(cat "$MARKER_E" | tr -d '[:space:]')"
  [ "$me" = "u9" ] \
    && ok "E: marker == u9 (None-uuid guard 동작 — null uuid 로 advance skip, 마지막 유효 uuid)" \
    || bad "E: marker should be u9, got: '$me'"
else
  bad "E: marker 파일 미생성 (None-uuid 인 마지막 줄이 있을 때도 u9 로 advance 돼야 함)"
fi

# ---- F. negative 3종 ----
echo "== F-1. locate None — 없는 sid → 빈 출력 rc0 + state 파일 미생성 =="

SID_GHOST="nonexistent-sid-xyz"
out_f1="$(python3 "$MEM" distill "$SID_GHOST")"
[ -z "$out_f1" ] && ok "F-1: 없는 sid distill → 빈 출력" || bad "F-1: 출력 non-empty: '$out_f1'"
python3 "$MEM" distill "$SID_GHOST" --advance >/dev/null 2>&1 || true
[ ! -e "$STORE/.distill-state-$SID_GHOST" ] \
  && ok "F-1: --advance 후에도 state 파일 미생성" \
  || bad "F-1: state 파일이 생성됨 (없는 sid)"

echo "== F-2. malformed jsonl — 깨진 줄 skip, 앞뒤 정상 줄 둘 다 출력 =="

SID_F2="distillsid_f2"
ENC_F2="$PROJ/-home-fake-f2"
mkdir -p "$ENC_F2"
cat > "$ENC_F2/$SID_F2.jsonl" <<'JSONL'
{"type":"user","message":{"role":"user","content":"정상 줄 앞 GOOD1"},"uuid":"f2u1","timestamp":"tf1","isSidechain":false}
{not json — broken line
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"정상 줄 뒤 GOOD2"}]},"uuid":"f2a1","timestamp":"tf2","isSidechain":false}
JSONL

out_f2="$(python3 "$MEM" distill "$SID_F2")"
echo "$out_f2" | grep -q "GOOD1" \
  && ok "F-2: malformed jsonl — 앞 정상 줄 GOOD1 출력 포함" \
  || bad "F-2: GOOD1 missing: $out_f2"
echo "$out_f2" | grep -q "GOOD2" \
  && ok "F-2: malformed jsonl — 뒤 정상 줄 GOOD2 출력 포함" \
  || bad "F-2: GOOD2 missing: $out_f2"

echo "== F-3. ghost marker — 파일에 없는 uuid → 빈 출력 (보수적 무-yield) =="

SID_F3="distillsid_f3"
ENC_F3="$PROJ/-home-fake-f3"
mkdir -p "$ENC_F3"
cat > "$ENC_F3/$SID_F3.jsonl" <<'JSONL'
{"type":"user","message":{"role":"user","content":"ghost marker test prompt"},"uuid":"f3u1","timestamp":"tf31","isSidechain":false}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"ghost marker reply"}]},"uuid":"f3a1","timestamp":"tf32","isSidechain":false}
JSONL

# ghost marker 주입
MARKER_F3="$STORE/.distill-state-$SID_F3"
printf 'GHOST-NOT-IN-FILE' > "$MARKER_F3"
out_f3="$(python3 "$MEM" distill "$SID_F3")"
[ -z "$out_f3" ] \
  && ok "F-3: ghost marker → 빈 출력 (보수적 무-yield, 재-dup 방지 불변식)" \
  || bad "F-3: ghost marker 임에도 출력 non-empty: '$out_f3'"

# ---- G. _assistant_text non-list + _user_text no-text-block ----
echo "== G-1. assistant content 가 str(list 아님) → 빈 text, 출력 미포함 =="

SID_G1="distillsid_g1"
ENC_G1="$PROJ/-home-fake-g1"
mkdir -p "$ENC_G1"
cat > "$ENC_G1/$SID_G1.jsonl" <<'JSONL'
{"type":"assistant","message":{"role":"assistant","content":"단순 str content — should be silent"},"uuid":"g1a1","timestamp":"tg1","isSidechain":false}
JSONL

out_g1="$(python3 "$MEM" distill "$SID_G1")"
[ -z "$out_g1" ] \
  && ok "G-1: assistant str content → 빈 text, distill 출력 미포함 (silent)" \
  || bad "G-1: 출력 non-empty: '$out_g1'"

echo "== G-2. user content 가 list + text 블록 없음(image only) → 빈 text, 출력 미포함 =="

SID_G2="distillsid_g2"
ENC_G2="$PROJ/-home-fake-g2"
mkdir -p "$ENC_G2"
cat > "$ENC_G2/$SID_G2.jsonl" <<'JSONL'
{"type":"user","message":{"role":"user","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"FAKEDATA"}}]},"uuid":"g2u1","timestamp":"tg2","isSidechain":false}
JSONL

out_g2="$(python3 "$MEM" distill "$SID_G2")"
[ -z "$out_g2" ] \
  && ok "G-2: user image-only content → 빈 text, distill 출력 미포함 (silent)" \
  || bad "G-2: 출력 non-empty: '$out_g2'"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
