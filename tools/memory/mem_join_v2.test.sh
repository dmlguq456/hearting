#!/usr/bin/env bash
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
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
json_path(){ python3 -c 'import json,sys; value=json.load(sys.stdin); [value:=value[key] for key in sys.argv[1].split(".")]; print(value)' "$1"; }

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
[ "$(printf '%s' "$SYNC" | json status)" = "folded" ] \
  && [ "$(printf '%s' "$SYNC" | json reason)" = "None" ] \
  && [ "$(printf '%s' "$SYNC" | json_path peer.status)" = "folded" ] \
  && [ "$(printf '%s' "$SYNC" | json_path phases.remote-fetch-validate)" = "ok" ] \
  && [ "$(printf '%s' "$SYNC" | json_path phases.remote-fold)" = "ok" ] \
  && [ "$(printf '%s' "$SYNC" | json_path phases.remote-render)" = "not-applicable" ] \
  && [ "$(printf '%s' "$SYNC" | json_path phases.remote-commit)" = "not-applicable" ] \
  && [ "$(printf '%s' "$SYNC" | json_path phases.remote-push)" = "not-applicable" ] \
  && [ "$(printf '%s' "$SYNC" | json_path phases.remote-confirm)" = "ok" ] \
  && ok "tipless remote is folded with exact fetch/fold evidence" \
  || bad "tipless sync evidence was not folded: $SYNC"

echo "== a published remote tip remains remote-confirmed =="
MEM_STORE="$CONFIG_STORE" MEM_INIT=1 python3 "$MEM" add durable note \
  "published tip fixture record" --scope global >/dev/null 2>&1
CONFIRMED="$(MEM_STORE="$CONFIG_STORE" python3 "$MEM" sync --json 2>/dev/null)"
[ "$(printf '%s' "$CONFIRMED" | json status)" = "remote-confirmed" ] \
  && [ "$(printf '%s' "$CONFIRMED" | json_path peer.last_confirmed_tip)" != "None" ] \
  && [ "$(printf '%s' "$CONFIRMED" | json_path phases.remote-push)" = "ok" ] \
  && [ "$(printf '%s' "$CONFIRMED" | json_path phases.remote-confirm)" = "ok" ] \
  && ok "published remote tip remains remote-confirmed" \
  || bad "published tip lost remote confirmation: $CONFIRMED"

echo "== a session opened in the home directory does not veto the default =="
# The home directory is an ancestor of the default exchange location, so
# counting it as a synchronized project root excluded the shipped default
# from itself and left no usable value at all. Checked at the source, since
# only a project entry that resolves to a real directory is decoded.
# The encoded-cwd form uses '-' as its separator, so the fixture path itself
# must not contain one or it cannot be decoded back to a real directory.
FAKE_ROOT="$(mktemp -d "${MEM_TEST_ROOT:-/var/tmp}/hearting_home_XXXXXX")"
trap 'rm -rf "$TMP" "$FAKE_ROOT"' EXIT
FAKE_HOME="$FAKE_ROOT/home"
mkdir -p "$FAKE_HOME/projects"
# The encoder maps '/', '.', and '_' alike onto the separator.
ENC="-$(printf %s "$FAKE_HOME" | sed 's|^/||; s|[/._]|-|g')"
mkdir -p "$FAKE_HOME/projects/$ENC"
python3 - "$HERE" "$FAKE_HOME" <<'PYEOF'
import importlib.util, os, sys
here, fake_home = sys.argv[1], sys.argv[2]
os.environ["HOME"] = fake_home
os.environ["MEM_PROJECTS"] = fake_home + "/projects"
os.environ["MEM_STORE"] = fake_home + "/store"
os.environ["MEM_INIT"] = "1"
spec = importlib.util.spec_from_file_location("memmod", here + "/mem.py")
mem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mem)
from pathlib import Path
roots = [Path(r) for r in mem._synchronized_project_roots()]
home = Path(fake_home)
assert home not in roots, f"home is still a forbidden root: {roots}"
# A real project under the home directory must still be refused.
project = home / "someproject"
project.mkdir(exist_ok=True)
import re
enc = "-" + re.sub(r"[/._]", "-", str(project).lstrip("/"))
(Path(os.environ["MEM_PROJECTS"]) / enc).mkdir(exist_ok=True)
roots = [Path(r) for r in mem._synchronized_project_roots()]
assert project in roots, f"a real project under home was dropped: {roots}"
print("ok")
PYEOF
[ $? -eq 0 ] && ok "home is exempt while its real project subtrees are not" \
  || bad "home exemption is wrong"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
