#!/usr/bin/env bash
# A record the distiller produced that `mem add` could not store must not close
# its delta window. The applier reports the failure (exit 1) and each caller
# keeps the window on the bounded-retry ladder (MEM_DISTILL_MAX_STRIKES) instead
# of acknowledging it; invalid lines and id-mutations stay best-effort.
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
set -uo pipefail

ROOT=$(git -C "$(dirname -- "$0")" rev-parse --show-toplevel)
APPLIER="$ROOT/tools/memory/apply-distill-actions.py"
MEM="$ROOT/tools/memory/mem.py"
CODEX_WORKER="$ROOT/adapters/codex/bin/distill-worker.sh"
OPENCODE_WORKER="$ROOT/adapters/opencode/bin/distill-worker.sh"
T="$HEARTING_TEST_ROOT"
export MEM_WRITE_EVENTS="$T/write-events.jsonl" MEM_RECALL_EVENTS="$T/recall-events.jsonl"
export MEM_RECALL_RECEIPTS="$T/recall-opportunities"
export AGENT_MODEL_GOVERNOR_ROOT="$T/governor"
unset AGENT_DISPATCH_JOBS MEM_DISTILL 2>/dev/null || true

PASS=0; FAIL=0
ok() { PASS=$((PASS + 1)); echo "ok: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL: $1"; }

RECORD='{"action":"add","tier":"working","type":"decision","body":"store-failure fixture decision body","headline":"Store failure fixture","aliases":[],"entities":[],"topics":[],"artifact_refs":[]}'

# --- applier ------------------------------------------------------------------
fake_mem="$T/fake-mem.py"
cat > "$fake_mem" <<'PY'
import os, sys
with open(os.environ["FAKE_MEM_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:2]) + "\n")
sys.exit(int(os.environ.get("FAKE_MEM_RC_" + sys.argv[1].upper(), "0")))
PY
export FAKE_MEM_LOG="$T/fake-mem.log"
printf '%s\n' "$RECORD" > "$T/one-record.jsonl"

FAKE_MEM_RC_ADD=2 python3 "$APPLIER" "$T/one-record.jsonl" "$fake_mem" 2>"$T/applier.err"
rc=$?
[ "$rc" -eq 1 ] && grep -q 'mem add failed' "$T/applier.err" \
  && ok "applier exits 1 when mem add fails for a valid record" \
  || bad "applier should exit 1 on a failed mem add (rc=$rc)"

FAKE_MEM_RC_ADD=0 python3 "$APPLIER" "$T/one-record.jsonl" "$fake_mem" 2>/dev/null \
  && ok "applier exits 0 when the record is stored" \
  || bad "applier should exit 0 when mem add succeeds"

: > "$FAKE_MEM_LOG"
printf 'not json\n```json\n{"action":"add","tier":"bogus"}\n' > "$T/invalid.jsonl"
FAKE_MEM_RC_ADD=2 python3 "$APPLIER" "$T/invalid.jsonl" "$fake_mem" 2>/dev/null \
  && [ ! -s "$FAKE_MEM_LOG" ] \
  && ok "invalid lines stay best-effort (exit 0, mem never called)" \
  || bad "invalid lines must not turn into a store failure"

printf 'rid-1\n' > "$T/snapids"
printf '{"action":"prune","id":"rid-1"}\n' > "$T/prune.jsonl"
FAKE_MEM_RC_PRUNE=1 python3 "$APPLIER" "$T/prune.jsonl" "$fake_mem" --mode curate \
  --snapshot-ids "$T/snapids" 2>/dev/null \
  && ok "id-mutation refusals stay best-effort (exit 0)" \
  || bad "a refused prune must not be reported as a store failure"

# --- adapter workers against a store that refuses writes ------------------------
# memory.db is read-only while its directory stays writable: `mem add` fails with
# a hard SQLite error, while the marker file beside it can still be written.
seed_store() {  # $1=store
  mkdir -p "$1"
  MEM_STORE="$1" MEM_INIT=1 python3 "$MEM" add working decision \
    'seed record that creates the store' --headline 'Seed' >/dev/null 2>&1
  chmod 444 "$1/memory.db"
}

mkdir -p "$T/bin" "$T/sessions" "$T/project"
cat > "$T/bin/codex" <<STUB
#!/bin/sh
while [ "\$#" -gt 0 ]; do
  if [ "\$1" = "--output-last-message" ]; then shift; printf '%s\n' '$RECORD' > "\$1"; fi
  shift || break
done
exit 0
STUB
cat > "$T/bin/opencode" <<STUB
#!/bin/sh
cat >/dev/null
printf '%s\n' '$RECORD'
STUB
chmod +x "$T/bin/codex" "$T/bin/opencode"
cat > "$T/opencode-export.json" <<'JSON'
{"messages":[
  {"id":"ou1","role":"user","time":"2026-09-23T00:00:00.000Z","content":[{"type":"text","text":"STOREFAIL question"}]},
  {"id":"oa1","role":"assistant","time":"2026-09-23T00:00:01.000Z","content":[{"type":"text","text":"STOREFAIL answer"}]}
]}
JSON

run_worker() {  # $1=codex|opencode $2=store $3=sid
  case "$1" in
    codex)
      MEM_STORE="$2" CODEX_SESSIONS="$T/sessions" PATH="$T/bin:$PATH" \
        CODEX_DISTILL_ENABLE=1 CODEX_DISTILL_APPLY=1 CODEX_DISTILL_CONTRACT_ACCEPTED=1 \
        sh "$CODEX_WORKER" "$3" "$T/project" increment >/dev/null 2>>"$T/$1.err" ;;
    opencode)
      MEM_STORE="$2" OPENCODE_EXPORT_FILE="$T/opencode-export.json" OPENCODE_BIN="$T/bin/opencode" \
        OPENCODE_DISTILL_ENABLE=1 OPENCODE_DISTILL_APPLY=1 \
        sh "$OPENCODE_WORKER" "$3" "$T/project" increment >/dev/null 2>>"$T/$1.err" ;;
  esac
}

delta_pending() {  # $1=codex|opencode $2=store $3=sid
  out=$(MEM_STORE="$2" CODEX_SESSIONS="$T/sessions" OPENCODE_EXPORT_FILE="$T/opencode-export.json" \
    python3 "$MEM" distill "$3" --source "$1" 2>/dev/null)
  [ -n "$(printf '%s' "$out" | tr -d '[:space:]')" ]
}

for rt in codex opencode; do
  sid="storefail-$rt"
  if [ "$rt" = codex ]; then
    cat > "$T/sessions/rollout-2026-09-23T00-00-00-$sid.jsonl" <<'ROLLOUT'
{"timestamp":"2026-09-23T00:00:00.000Z","type":"event_msg","payload":{"type":"user_message","message":"STOREFAIL question"}}
{"timestamp":"2026-09-23T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"STOREFAIL answer"}]}}
ROLLOUT
  fi
  store="$T/store-$rt"
  seed_store "$store"
  delta_pending "$rt" "$store" "$sid" || bad "$rt fixture: delta should be pending before the first run"

  run_worker "$rt" "$store" "$sid"
  delta_pending "$rt" "$store" "$sid" && [ "$(cat "$store/.$rt-distill-fail-$sid" 2>/dev/null)" = 1 ] \
    && ok "$rt: a failed store keeps the delta (strike 1)" \
    || bad "$rt: marker advanced past a record that was never stored"

  run_worker "$rt" "$store" "$sid"
  run_worker "$rt" "$store" "$sid"
  ! delta_pending "$rt" "$store" "$sid" && [ ! -e "$store/.$rt-distill-fail-$sid" ] \
    && ok "$rt: the third strike still closes the window (no poison loop)" \
    || bad "$rt: bounded retry did not close the window after MEM_DISTILL_MAX_STRIKES"

  # SQLite created -wal/-shm with the database's read-only mode; restore all.
  chmod u+w "$store"/memory.db*
  sid_ok="storeok-$rt"
  if [ "$rt" = codex ]; then
    sed "s/STOREFAIL/STOREOK/" "$T/sessions/rollout-2026-09-23T00-00-00-$sid.jsonl" \
      > "$T/sessions/rollout-2026-09-23T00-00-00-$sid_ok.jsonl"
  fi
  run_worker "$rt" "$store" "$sid_ok"
  ! delta_pending "$rt" "$store" "$sid_ok" && [ ! -e "$store/.$rt-distill-fail-$sid_ok" ] \
    && ok "$rt: a stored record advances the marker without a strike" \
    || bad "$rt: a healthy store should advance the marker"
done

echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
