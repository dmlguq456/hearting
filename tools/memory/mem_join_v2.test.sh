#!/usr/bin/env bash
# Regressions for the fresh-store join surface (2026-08-21):
#   1) `mem migration join` refuses a store that already holds memory
#   2) its dry-run writes nothing, and apply opens the remote gate
#   3) exchange policy is read from the shared config file, not just the
#      environment, so every adapter resolves the same exchange
# The remote is a local bare repository; no network and no live store.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MEM="$HERE/mem.py"
PASS=0 FAIL=0
ok(){ PASS=$((PASS+1)); printf '  ok  %s\n' "$*"; }
bad(){ FAIL=$((FAIL+1)); printf '  BAD %s\n' "$*"; }
# The transport refuses an exchange inside a Git tree, and /tmp may itself be
# one, so the fixture root has to be somewhere that is provably not.
TMP="$(mktemp -d "${MEM_TEST_ROOT:-/var/tmp}/hearting-join.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
export AGENT_HOME="$(cd "$HERE/../.." && pwd)"
export MEM_PROFILE="$TMP/no-profile"
export XDG_CONFIG_HOME="$TMP/config"
export XDG_STATE_HOME="$TMP/state"
unset MEM_SYNC_REMOTE MEM_SYNC_DIR MEM_SYNC_REF MEM_SYNC_REMOTE_URL
unset MEM_DUMP_PUSH MEM_DUMP_COMMIT

REF="refs/heads/hearting-memory-v2"
REMOTE="$TMP/remote.git"
git init -q --bare "$REMOTE"

json(){ python3 -c "import json,sys; print(json.load(sys.stdin).get(sys.argv[1]))" "$1"; }

echo "== a populated store cannot join =="
POPULATED="$TMP/populated"
MEM_STORE="$POPULATED" MEM_INIT=1 python3 "$MEM" add durable note \
  "populated fixture record that blocks a fresh join" --scope global >/dev/null 2>&1
OUT="$(MEM_STORE="$POPULATED" MEM_SYNC_DIR="$TMP/ex-populated" MEM_SYNC_REF="$REF" \
  MEM_SYNC_REMOTE_URL="$REMOTE" python3 "$MEM" migration join --json 2>/dev/null)"
[ "$(printf '%s' "$OUT" | json status)" = "hard-failure" ] \
  && printf '%s' "$OUT" | grep -q "store-is-not-fresh" \
  && ok "join refuses a store that already holds records" \
  || bad "join did not refuse a populated store: $OUT"

echo "== dry-run writes nothing =="
FRESH="$TMP/fresh"
EXCHANGE="$TMP/ex-fresh"
run_fresh(){ MEM_STORE="$FRESH" MEM_INIT=1 MEM_SYNC_DIR="$EXCHANGE" \
  MEM_SYNC_REF="$REF" MEM_SYNC_REMOTE_URL="$REMOTE" MEM_SYNC_REMOTE=1 \
  python3 "$MEM" "$@"; }
OUT="$(run_fresh migration join --json 2>/dev/null)"
[ "$(printf '%s' "$OUT" | json reason)" = "dry-run" ] && [ ! -e "$EXCHANGE" ] \
  && ok "dry-run reports the plan and creates no exchange" \
  || bad "dry-run was not inert: $OUT"

echo "== apply opens the remote gate and is idempotent =="
OUT="$(run_fresh migration join --apply --json 2>/dev/null)"
[ "$(printf '%s' "$OUT" | json migration_state)" = "joined" ] \
  && [ "$(printf '%s' "$OUT" | json remote_allowed)" = "True" ] \
  && ok "apply joins the store and allows remote exchange" \
  || bad "apply did not open the gate: $OUT"
OUT="$(run_fresh migration join --apply --json 2>/dev/null)"
[ "$(printf '%s' "$OUT" | json reason)" = "already-joined" ] \
  && ok "a second join is a no-op, not a second epoch" \
  || bad "repeated join was not idempotent: $OUT"

echo "== the shared config file supplies the policy without any env =="
CONFIG_STORE="$TMP/config-store"
CONFIG_EXCHANGE="$TMP/ex-config"
mkdir -p "$XDG_CONFIG_HOME/hearting"
python3 - "$XDG_CONFIG_HOME/hearting/memory-sync.json" "$REMOTE" "$REF" "$CONFIG_EXCHANGE" <<'PY'
import json, sys
path, remote, ref, exchange = sys.argv[1:5]
open(path, "w", encoding="utf-8").write(json.dumps({
    "schema_version": 1, "enabled": True, "remote_url": remote,
    "ref": ref, "exchange_dir": exchange}, sort_keys=True) + "\n")
PY
OUT="$(MEM_STORE="$CONFIG_STORE" MEM_INIT=1 python3 "$MEM" migration join --apply --json 2>/dev/null)"
[ "$(printf '%s' "$OUT" | json exchange_dir)" = "$CONFIG_EXCHANGE" ] \
  && [ "$(printf '%s' "$OUT" | json migration_state)" = "joined" ] \
  && ok "join resolves remote, ref, and exchange from the config file" \
  || bad "config-file policy was not used: $OUT"

SYNC="$(MEM_STORE="$CONFIG_STORE" python3 "$MEM" sync --json 2>/dev/null)"
[ "$(printf '%s' "$SYNC" | json status)" = "remote-confirmed" ] \
  && ok "sync reaches the remote using only the config file" \
  || bad "sync did not use the config file: $(printf '%s' "$SYNC" | json reason)"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
