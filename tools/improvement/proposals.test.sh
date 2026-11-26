#!/usr/bin/env bash
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../run-tests.py" --direct-run bash "$0" "$@"
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
REAL_HOME=${HOME:?HOME is required}
TMP=$(mktemp -d)

runtime_snapshot() {
  REAL_HOME_SNAPSHOT="$REAL_HOME" python3 - <<'PY'
import hashlib
import os
from pathlib import Path

home = Path(os.environ["REAL_HOME_SNAPSHOT"])
targets = [
    home / ".claude/settings.json",
    home / ".claude/plugins/installed_plugins.json",
    home / ".codex/config.toml",
    home / ".config/opencode/opencode.json",
    home / ".config/opencode/opencode.jsonc",
    home / ".config/opencode/plugins/hearting-guards.js",
]
for base in (home / ".claude/plugins/cache", home / ".codex/plugins/cache"):
    if base.is_dir():
        targets.extend(
            path
            for path in base.rglob("*")
            if "hearting" in path.as_posix() and (path.is_file() or path.is_symlink())
        )
digest = hashlib.sha256()
for path in sorted(set(targets), key=lambda item: str(item)):
    digest.update(str(path).encode("utf-8") + b"\0")
    if path.is_symlink():
        digest.update(b"L\0" + os.readlink(path).encode("utf-8") + b"\0")
    elif path.is_file():
        digest.update(b"F\0" + path.read_bytes() + b"\0")
    else:
        digest.update(b"M\0")
print(digest.hexdigest())
PY
}

BEFORE=$(runtime_snapshot)
cleanup() {
  status=$?
  AFTER=$(runtime_snapshot)
  rm -rf "$TMP"
  if [ "$BEFORE" != "$AFTER" ]; then
    echo "FAIL: real runtime config/plugin state changed" >&2
    exit 97
  fi
  exit "$status"
}
trap cleanup EXIT

mkdir -p \
  "$TMP/home" \
  "$TMP/state" \
  "$TMP/config" \
  "$TMP/data" \
  "$TMP/runtime/claude" \
  "$TMP/runtime/codex" \
  "$TMP/runtime/opencode"

cd "$ROOT"
HOME="$TMP/home" \
XDG_STATE_HOME="$TMP/state" \
XDG_CONFIG_HOME="$TMP/config" \
XDG_DATA_HOME="$TMP/data" \
CLAUDE_CONFIG_DIR="$TMP/runtime/claude" \
CODEX_HOME="$TMP/runtime/codex" \
OPENCODE_CONFIG_DIR="$TMP/runtime/opencode" \
python3 -m unittest -v tools.improvement.test_proposals

echo "PASS: proposal lifecycle is isolated; real runtime state unchanged ($BEFORE)"
