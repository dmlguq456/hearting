#!/usr/bin/env bash
# Standalone test for utilities/mem-periodic-curate.sh (R-3).
# Fully isolated via MEM_STORE + MEM_PROJECTS temp dirs — never touches the real store.
# Real worker spawn is ALWAYS avoided via MEM_DISTILL_WORKER=claude plus a PATH-injected stub.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UTIL="$ROOT/utilities/mem-periodic-curate.sh"
MEM="$ROOT/tools/memory/mem.py"
[ -f "$UTIL" ] || { echo "FAIL: periodic curator not found at $UTIL"; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  ❌ %s\n' "$1"; }

CLEANUP=()
trap 'rm -rf "${CLEANUP[@]}"' EXIT

# D-42 hermeticity: unset any inherited worker/dispatch markers so every case
# starts from a clean main-session-shaped environment (mirrors dispatch.test.sh).
unset AGENT_SESSION_ROLE AGENT_DISPATCH_CHILD AGENT_DISPATCH_DEPTH \
  OPENCODE_DISPATCH_SLUG FLEET_TITLE_REFRESH MEM_DISTILL

# ---- fixture helper: one eligible project = <projects>/-<enc>/memory + a real dir ----
mkproject() {  # $1=projects_root $2=real_dir
  mkdir -p "$2"
  enc="$(printf '%s' "$2" | sed -E 's#[/._]#-#g')"
  mkdir -p "$1/$enc/memory"
}

# Selection is DB-ranked: a candidate cwd is dispatched only when its origin
# holds active records in the store. Seed $4 records so the project is eligible
# (a non-git temp dir's project key is its enc_cwd — the same substitution
# mkproject applies).
mkproject_seeded() {  # $1=projects_root $2=real_dir $3=store $4=record_count
  mkproject "$1" "$2"
  _seed_i=1
  while [ "$_seed_i" -le "$4" ]; do
    # `--cwd-origin=` form: an encoded cwd starts with "-" and would otherwise
    # be parsed as an option flag.
    MEM_STORE="$3" python3 "$MEM" add working thread \
      "seed record $_seed_i for $2" --cwd-origin="$enc" >/dev/null 2>&1
    _seed_i=$((_seed_i + 1))
  done
}

CONC_STUB_BODY='#!/bin/sh
if [ -d "$CONC_DIR/.active" ]; then
  touch "$CONC_DIR/VIOLATION"
fi
mkdir "$CONC_DIR/.active" 2>/dev/null || true
printf "%s\n" "$$" >> "$CONC_DIR/calls"
sleep 0.3
rmdir "$CONC_DIR/.active" 2>/dev/null || true
'

# ============================================================
# ① gate unset → complete no-op
# ============================================================
echo "== ① MEM_PERIODIC_CURATE_ENABLE unset → complete no-op =="
STORE1="$(mktemp -d)"; PROJ1="$(mktemp -d)"; STUB1="$(mktemp -d)"; REALDIR1="$(mktemp -d)"
CLEANUP+=("$STORE1" "$PROJ1" "$STUB1" "$REALDIR1")
mkproject_seeded "$PROJ1" "$REALDIR1" "$STORE1" 1
printf '%s' "$CONC_STUB_BODY" > "$STUB1/claude"; chmod +x "$STUB1/claude"

env -u MEM_PERIODIC_CURATE_ENABLE \
  MEM_STORE="$STORE1" MEM_PROJECTS="$PROJ1" MEM_PY="$MEM" \
  MEM_DISTILL_ENABLE=1 MEM_DISTILL_WORKER=claude PATH="$STUB1:$PATH" \
  CONC_DIR="$STORE1" \
  bash "$UTIL"
rc1=$?
[ "$rc1" = "0" ] \
  && ok "①: gate unset → exit 0" \
  || bad "①: gate unset → exit $rc1"
[ ! -f "$STORE1/calls" ] \
  && ok "①: gate unset → worker stub never called" \
  || bad "①: gate unset → worker stub was called"
lockcount1="$(find "$STORE1" -maxdepth 1 -name '.distill-lock-periodic-*' -type d 2>/dev/null | wc -l)"
[ "${lockcount1:-0}" = "0" ] \
  && ok "①: gate unset → no periodic lock/state materialized" \
  || bad "①: gate unset → unexpected lock dir(s) present"

# ============================================================
# ② enable=1 → exactly one worker call per project, strictly sequential
# ============================================================
echo "== ② enable=1 → per-project single call, sequential (concurrency <= 1) =="
STORE2="$(mktemp -d)"; PROJ2="$(mktemp -d)"; STUB2="$(mktemp -d)"
CLEANUP+=("$STORE2" "$PROJ2" "$STUB2")
REALDIR2A="$(mktemp -d)"; REALDIR2B="$(mktemp -d)"; REALDIR2C="$(mktemp -d)"
CLEANUP+=("$REALDIR2A" "$REALDIR2B" "$REALDIR2C")
mkproject_seeded "$PROJ2" "$REALDIR2A" "$STORE2" 1
mkproject_seeded "$PROJ2" "$REALDIR2B" "$STORE2" 1
mkproject_seeded "$PROJ2" "$REALDIR2C" "$STORE2" 1
printf '%s' "$CONC_STUB_BODY" > "$STUB2/claude"; chmod +x "$STUB2/claude"

MEM_PERIODIC_CURATE_ENABLE=1 MEM_DISTILL_ENABLE=1 \
  MEM_STORE="$STORE2" MEM_PROJECTS="$PROJ2" MEM_PY="$MEM" \
  MEM_DISTILL_WORKER=claude PATH="$STUB2:$PATH" \
  AGENT_MODEL_GOVERNOR_ROOT="$STORE2/.test-model-governor" \
  CONC_DIR="$STORE2" \
  bash "$UTIL"

calls2="$(wc -l < "$STORE2/calls" 2>/dev/null || echo 0)"
[ "${calls2:-0}" = "3" ] \
  && ok "②: exactly one worker call per project (3 projects → 3 calls)" \
  || bad "②: worker call count = ${calls2:-0}, expected 3"
[ ! -e "$STORE2/VIOLATION" ] \
  && ok "②: no overlapping worker invocation observed (concurrency <= 1)" \
  || bad "②: concurrent worker invocation detected (VIOLATION sentinel present)"

# ============================================================
# ③ D-42 — the periodic curator itself refuses a worker-marked context
# ============================================================
echo "== ③ AGENT_SESSION_ROLE=worker → mem-periodic-curate.sh itself no-ops (D-42) =="
STORE3="$(mktemp -d)"; PROJ3="$(mktemp -d)"; STUB3="$(mktemp -d)"; REALDIR3="$(mktemp -d)"
CLEANUP+=("$STORE3" "$PROJ3" "$STUB3" "$REALDIR3")
mkproject_seeded "$PROJ3" "$REALDIR3" "$STORE3" 1
printf '%s' "$CONC_STUB_BODY" > "$STUB3/claude"; chmod +x "$STUB3/claude"

AGENT_SESSION_ROLE=worker MEM_PERIODIC_CURATE_ENABLE=1 MEM_DISTILL_ENABLE=1 \
  MEM_STORE="$STORE3" MEM_PROJECTS="$PROJ3" MEM_PY="$MEM" \
  MEM_DISTILL_WORKER=claude PATH="$STUB3:$PATH" \
  CONC_DIR="$STORE3" \
  bash "$UTIL"
[ ! -f "$STORE3/calls" ] \
  && ok "③: AGENT_SESSION_ROLE=worker → D-42 fires, worker stub never called" \
  || bad "③: AGENT_SESSION_ROLE=worker → D-42 did not stop the run"

# ============================================================
# ④ MEM_DISTILL_ENABLE unset → periodic-curate mode also no-ops (double gate)
# ============================================================
echo "== ④ MEM_DISTILL_ENABLE unset → periodic-curate dispatch no-ops (double gate) =="
STORE4="$(mktemp -d)"; PROJ4="$(mktemp -d)"; STUB4="$(mktemp -d)"; REALDIR4="$(mktemp -d)"
CLEANUP+=("$STORE4" "$PROJ4" "$STUB4" "$REALDIR4")
mkproject_seeded "$PROJ4" "$REALDIR4" "$STORE4" 1
printf '%s' "$CONC_STUB_BODY" > "$STUB4/claude"; chmod +x "$STUB4/claude"

env -u MEM_DISTILL_ENABLE \
  MEM_PERIODIC_CURATE_ENABLE=1 \
  MEM_STORE="$STORE4" MEM_PROJECTS="$PROJ4" MEM_PY="$MEM" \
  MEM_DISTILL_WORKER=claude PATH="$STUB4:$PATH" \
  CONC_DIR="$STORE4" \
  bash "$UTIL"
[ ! -f "$STORE4/calls" ] \
  && ok "④: MEM_DISTILL_ENABLE unset → dispatcher's own gate stops the run" \
  || bad "④: MEM_DISTILL_ENABLE unset → periodic-curate ran anyway"

# ============================================================
# ⑤ kill switch (.distill-disable) → no-op
# ============================================================
echo "== ⑤ .distill-disable kill switch → no-op =="
STORE5="$(mktemp -d)"; PROJ5="$(mktemp -d)"; STUB5="$(mktemp -d)"; REALDIR5="$(mktemp -d)"
CLEANUP+=("$STORE5" "$PROJ5" "$STUB5" "$REALDIR5")
mkproject_seeded "$PROJ5" "$REALDIR5" "$STORE5" 1
printf '%s' "$CONC_STUB_BODY" > "$STUB5/claude"; chmod +x "$STUB5/claude"
mkdir -p "$STORE5"
touch "$STORE5/.distill-disable"

MEM_PERIODIC_CURATE_ENABLE=1 MEM_DISTILL_ENABLE=1 \
  MEM_STORE="$STORE5" MEM_PROJECTS="$PROJ5" MEM_PY="$MEM" \
  MEM_DISTILL_WORKER=claude PATH="$STUB5:$PATH" \
  CONC_DIR="$STORE5" \
  bash "$UTIL"
[ ! -f "$STORE5/calls" ] \
  && ok "⑤: .distill-disable → worker stub never called" \
  || bad "⑤: .distill-disable → worker stub was called"

# ============================================================
# ⑥ sequential holds for a worker that outlives the old 10s poll cap
# ============================================================
# The first implementation polled the per-project lock for a fixed
# 100 x 0.1s = 10s. A deep curator legitimately runs up to
# MEM_DISTILL_TIMEOUT_CURATE (default 600s), so any real curate run would blow
# past that cap, the loop would give up, and the next project's dispatch would
# overlap the previous one -- silently breaking the "sequential" contract stated
# in this script, core/HOOKS.md, core/MEMORY.md, and plan.md.
# Case ② cannot catch this: its stub sleeps 0.3s, comfortably inside the old
# window, so it passed vacuously. This case uses a stub that deliberately
# outlives the old cap, so it fails against the 10s implementation and passes
# only when the wait actually tracks the curate completion marker. The sleep
# command is PATH-stubbed: 12 logical seconds become 0.25 real seconds, while
# the old implementation's 0.1-second polling sleeps become no-ops. This keeps
# the regression fast while preserving the ordering that exposed the bug.
echo "== ⑥ worker slower than the old 10s cap → still strictly sequential =="
SLOW_SECONDS="${MEM_PERIODIC_CURATE_TEST_SLOW:-12}"
STORE6="$(mktemp -d)"; PROJ6="$(mktemp -d)"; STUB6="$(mktemp -d)"
CLEANUP+=("$STORE6" "$PROJ6" "$STUB6")
REALDIR6A="$(mktemp -d)"; REALDIR6B="$(mktemp -d)"
CLEANUP+=("$REALDIR6A" "$REALDIR6B")
mkproject_seeded "$PROJ6" "$REALDIR6A" "$STORE6" 1
mkproject_seeded "$PROJ6" "$REALDIR6B" "$STORE6" 1
cat > "$STUB6/claude" <<EOS
#!/bin/sh
if [ -d "\$CONC_DIR/.active" ]; then
  touch "\$CONC_DIR/VIOLATION"
fi
mkdir "\$CONC_DIR/.active" 2>/dev/null || true
printf "%s\n" "\$\$" >> "\$CONC_DIR/calls"
sleep $SLOW_SECONDS
rmdir "\$CONC_DIR/.active" 2>/dev/null || true
EOS
chmod +x "$STUB6/claude"
cat > "$STUB6/sleep" <<EOS
#!/bin/sh
case "\${1:-}" in
  "$SLOW_SECONDS") exec /bin/sleep 0.25 ;;
  0.1) exit 0 ;;
  1) exec /bin/sleep 0.01 ;;
  *) exec /bin/sleep "\$@" ;;
esac
EOS
chmod +x "$STUB6/sleep"

LOG6="$STORE6/periodic.log"

MEM_PERIODIC_CURATE_ENABLE=1 MEM_DISTILL_ENABLE=1 \
  MEM_STORE="$STORE6" MEM_PROJECTS="$PROJ6" MEM_PY="$MEM" \
  MEM_DISTILL_WORKER=claude PATH="$STUB6:$PATH" \
  MEM_DISTILL_TIMEOUT_CURATE=600 \
  AGENT_MODEL_GOVERNOR_ROOT="$STORE6/.test-model-governor" \
  CONC_DIR="$STORE6" \
  bash "$UTIL" 2>"$LOG6"

calls6="$(wc -l < "$STORE6/calls" 2>/dev/null || echo 0)"
[ "${calls6:-0}" = "2" ] \
  && ok "⑥: one worker call per project (2 projects → 2 calls)" \
  || bad "⑥: worker call count = ${calls6:-0}, expected 2"
[ ! -e "$STORE6/VIOLATION" ] \
  && ok "⑥: ${SLOW_SECONDS}s worker (> old 10s cap) still did not overlap" \
  || bad "⑥: overlap detected — sequential wait gave up before the worker finished"
logcount6="$(grep -c '^mem-periodic-curate project=.* elapsed=[0-9][0-9]*s status=complete$' "$LOG6" 2>/dev/null || true)"
[ "${logcount6:-0}" = "2" ] \
  && ok "⑥: cron log has one elapsed-time line per completed project" \
  || bad "⑥: cron project log count = ${logcount6:-0}, expected 2"

# ============================================================
# ⑦ whole-run timeout stops the batch instead of allowing overlap
# ============================================================
echo "== ⑦ cron timeout → stop batch with timed project still isolated =="
STORE7="$(mktemp -d)"; PROJ7="$(mktemp -d)"; STUB7="$(mktemp -d)"
CLEANUP+=("$STORE7" "$PROJ7" "$STUB7")
REALDIR7A="$(mktemp -d)"; REALDIR7B="$(mktemp -d)"
CLEANUP+=("$REALDIR7A" "$REALDIR7B")
mkproject_seeded "$PROJ7" "$REALDIR7A" "$STORE7" 1
mkproject_seeded "$PROJ7" "$REALDIR7B" "$STORE7" 1
printf '%s' "$CONC_STUB_BODY" > "$STUB7/claude"; chmod +x "$STUB7/claude"
cat > "$STUB7/sleep" <<'EOS'
#!/bin/sh
case "${1:-}" in
  0.3) exec /bin/sleep 8 ;;
  1) exec /bin/sleep 0.01 ;;
  *) exec /bin/sleep "$@" ;;
esac
EOS
chmod +x "$STUB7/sleep"
LOG7="$STORE7/periodic.log"

MEM_PERIODIC_CURATE_ENABLE=1 MEM_DISTILL_ENABLE=1 \
  MEM_STORE="$STORE7" MEM_PROJECTS="$PROJ7" MEM_PY="$MEM" \
  MEM_DISTILL_WORKER=claude PATH="$STUB7:$PATH" \
  MEM_PERIODIC_CURATE_TIMEOUT=3 MEM_PERIODIC_CURATE_PROJECT_TIMEOUT=30 \
  AGENT_MODEL_GOVERNOR_ROOT="$STORE7/.test-model-governor" \
  CONC_DIR="$STORE7" \
  bash "$UTIL" 2>"$LOG7"

calls7="$(wc -l < "$STORE7/calls" 2>/dev/null || echo 0)"
[ "${calls7:-0}" = "1" ] \
  && ok "⑦: whole-run timeout prevents the second project from starting" \
  || bad "⑦: worker call count = ${calls7:-0}, expected 1"
grep -Eq '^mem-periodic-curate project=.* elapsed=[0-9][0-9]*s status=timeout$' "$LOG7" \
  && ok "⑦: timed project emits a bounded one-line timeout log" \
  || bad "⑦: timeout log missing or malformed"
# Let the detached test worker run its EXIT cleanup before fixture teardown.
/bin/sleep 0.4

# ============================================================
# ⑧ DB-ranked selection — residue excluded, backlog order, cap by rank
# ============================================================
# Regression for the first-field-run defect: alphabetical directory order let
# record-less worker-session residue pre-empt MAX_PROJECTS while real backlog
# projects never got dispatched. Three candidate session dirs exist; only two
# hold active records. With MAX_PROJECTS=2 the record-less residue must never
# be dispatched, and the two record-backed projects must dispatch in
# active-count order (heavier backlog first), regardless of path order.
echo "== ⑧ DB-ranked selection → residue skipped, backlog-first order =="
STORE8="$(mktemp -d)"; PROJ8="$(mktemp -d)"; STUB8="$(mktemp -d)"
CLEANUP+=("$STORE8" "$PROJ8" "$STUB8")
BASE8="$(mktemp -d)"
CLEANUP+=("$BASE8")
# aa- prefix sorts the residue FIRST alphabetically — the old implementation
# would dispatch it; zz- sorts the heaviest backlog LAST alphabetically.
RESIDUE8="$BASE8/aa-residue"
LIGHT8="$BASE8/mm-light"
HEAVY8="$BASE8/zz-heavy"
mkproject "$PROJ8" "$RESIDUE8"                       # session dir, zero records
mkproject_seeded "$PROJ8" "$LIGHT8" "$STORE8" 1
mkproject_seeded "$PROJ8" "$HEAVY8" "$STORE8" 3
printf '%s' "$CONC_STUB_BODY" > "$STUB8/claude"; chmod +x "$STUB8/claude"
LOG8="$STORE8/periodic.log"

MEM_PERIODIC_CURATE_ENABLE=1 MEM_DISTILL_ENABLE=1 \
  MEM_STORE="$STORE8" MEM_PROJECTS="$PROJ8" MEM_PY="$MEM" \
  MEM_PERIODIC_CURATE_MAX_PROJECTS=2 \
  MEM_DISTILL_WORKER=claude PATH="$STUB8:$PATH" \
  AGENT_MODEL_GOVERNOR_ROOT="$STORE8/.test-model-governor" \
  CONC_DIR="$STORE8" \
  bash "$UTIL" 2>"$LOG8"

calls8="$(wc -l < "$STORE8/calls" 2>/dev/null || echo 0)"
[ "${calls8:-0}" = "2" ] \
  && ok "⑧: cap=2 → exactly the two record-backed projects dispatched" \
  || bad "⑧: worker call count = ${calls8:-0}, expected 2"
grep -q "project=$RESIDUE8 " "$LOG8" \
  && bad "⑧: record-less residue was dispatched despite DB ranking" \
  || ok "⑧: record-less residue (alphabetically first) never dispatched"
order8="$(grep -o "project=[^ ]*" "$LOG8" | head -2 | tr '\n' ' ')"
[ "$order8" = "project=$HEAVY8 project=$LIGHT8 " ] \
  && ok "⑧: dispatch order follows active record count (3 > 1)" \
  || bad "⑧: dispatch order '$order8', expected heavy then light"
grep -q "select origin=.* active=3 durable=0 cwd=$HEAVY8\$" "$LOG8" \
  && ok "⑧: selection log carries origin/active/durable evidence" \
  || bad "⑧: selection evidence line missing for heavy project"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
