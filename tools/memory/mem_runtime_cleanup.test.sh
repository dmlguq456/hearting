#!/usr/bin/env bash
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../run-tests.py" --direct-run bash "$0" "$@"
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
MEM="$ROOT/tools/memory/mem.py"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

export MEM_STORE="$TMP/store"
export MEM_PROJECTS="$TMP/projects"
export MEM_PROFILE="$TMP/profile"
mkdir -p "$MEM_PROJECTS/-tmp-project/memory/_projection"
printf '%s\n' 'A durable runtime-memory topic with enough detail to migrate.' \
  > "$MEM_PROJECTS/-tmp-project/memory/topic.md"
printf '%s\n' '# Index' > "$MEM_PROJECTS/-tmp-project/memory/MEMORY.md"
printf '%s\n' '# Projection' \
  > "$MEM_PROJECTS/-tmp-project/memory/_projection/project.md"

python3 "$MEM" migrate --apply --all-projects >/dev/null
python3 "$MEM" migrate --all-projects --cleanup-runtime-memory >/dev/null

# A file edited after its first absorption is no longer a permanent parity
# blocker: `migrate --apply` re-syncs the DB row to the live file body before
# the cleanup plan is checked (D-79 gap, 2026-09-26), so a combined
# migrate+cleanup call now self-heals the drift and proceeds. `--all-projects`
# cleanup sweeps every project unconditionally, so this runs in its own
# MEM_STORE/MEM_PROJECTS subshell rather than -tmp-project's shared fixture.
(
  DRIFT_STORE="$TMP/drift-store"
  DRIFT_PROJECTS="$TMP/drift-projects"
  mkdir -p "$DRIFT_STORE" "$DRIFT_PROJECTS/-drift-project/memory"
  export MEM_STORE="$DRIFT_STORE" MEM_PROJECTS="$DRIFT_PROJECTS"
  printf '%s\n' 'A drift topic absorbed once, then edited before cleanup runs.' \
    > "$DRIFT_PROJECTS/-drift-project/memory/drift.md"
  python3 "$MEM" migrate --apply --all-projects >/dev/null
  printf '%s\n' 'A drift topic absorbed once, then edited before cleanup runs (revised).' \
    > "$DRIFT_PROJECTS/-drift-project/memory/drift.md"
  python3 "$MEM" migrate --apply --all-projects --cleanup-runtime-memory \
    --cleanup-archive "$TMP/drift.tar.gz" >/dev/null
  test -f "$TMP/drift.tar.gz"
  test ! -e "$DRIFT_PROJECTS/-drift-project/memory"
  tar -tzf "$TMP/drift.tar.gz" | grep -q \
    'runtime-project-memory/-drift-project/memory/drift.md'
  python3 - "$DRIFT_STORE/memory.db" <<'PY'
import sqlite3
import sys
con = sqlite3.connect(sys.argv[1])
row = con.execute(
    "SELECT body FROM records WHERE source=?",
    ("auto-memory:-drift-project/drift.md",),
).fetchone()
assert row == ("A drift topic absorbed once, then edited before cleanup runs (revised).\n",), row
PY
)

mkdir -p "$TMP/external-project/memory"
printf '%s\n' 'An external topic reached only through an unsafe project symlink.' \
  > "$TMP/external-project/memory/external.md"
ln -s "$TMP/external-project" "$MEM_PROJECTS/-linked-project"
if python3 "$MEM" migrate --apply --all-projects --cleanup-runtime-memory \
    --cleanup-archive "$TMP/symlink.tar.gz" >/dev/null 2>&1; then
  echo 'FAIL: symlinked project parent was accepted' >&2
  exit 1
fi
test -f "$TMP/external-project/memory/external.md"
test ! -e "$TMP/symlink.tar.gz"
rm "$MEM_PROJECTS/-linked-project"

python3 "$MEM" migrate --apply --all-projects --cleanup-runtime-memory \
  --cleanup-archive "$TMP/recovery.tar.gz" >/dev/null
test -f "$TMP/recovery.tar.gz"
test ! -e "$MEM_PROJECTS/-tmp-project/memory"
tar -tzf "$TMP/recovery.tar.gz" | grep -q \
  'runtime-project-memory/-tmp-project/memory/topic.md'

python3 - "$MEM_STORE/memory.db" <<'PY'
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
row = con.execute(
    "SELECT body FROM records WHERE source=?",
    ("auto-memory:-tmp-project/topic.md",),
).fetchone()
assert row == ("A durable runtime-memory topic with enough detail to migrate.\n",)
PY

# `_runtime_memory_cleanup_plan`'s own duplicate-row and body-mismatch defenses
# (mem.py ~3298-3306) are no longer reachable through an ordinary migrate+cleanup
# call (migrate now self-heals drift first), but the gate itself must still be
# live for a state migrate can't produce on its own, e.g. two active rows left
# behind for one source (review finding, 2026-09-27, alongside the superseded-
# record fix: a duplicate-row corruption must still hard-fail cleanup, not
# silently archive one arbitrary row and lose the other).
(
  GATE_STORE="$TMP/gate-store"
  GATE_PROJECTS="$TMP/gate-projects"
  mkdir -p "$GATE_STORE" "$GATE_PROJECTS/-gate-project/memory"
  export MEM_STORE="$GATE_STORE" MEM_PROJECTS="$GATE_PROJECTS"
  printf '%s\n' 'A gate topic with exactly one migrated row expected.' \
    > "$GATE_PROJECTS/-gate-project/memory/gate.md"
  python3 "$MEM" migrate --apply --all-projects >/dev/null
  python3 - "$GATE_STORE/memory.db" <<'PY'
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
row = con.execute(
    "SELECT * FROM records WHERE source=?",
    ("auto-memory:-gate-project/gate.md",),
).fetchone()
cols = [d[0] for d in con.execute("SELECT * FROM records LIMIT 0").description]
values = dict(zip(cols, row))
values["id"] = "gate_duplicate_row"
placeholders = ",".join("?" for _ in cols)
con.execute(f"INSERT INTO records({','.join(cols)}) VALUES({placeholders})",
            [values[c] for c in cols])
con.commit()
PY
  if PYTHONPATH="$ROOT/tools/memory" python3 -c '
import sys
sys.path.insert(0, "'"$ROOT"'/tools/memory")
import mem
mem._runtime_memory_cleanup_plan()
' 2>"$TMP/gate-error.log"; then
    echo 'FAIL: duplicate-row parity gate accepted two active rows for one source' >&2
    exit 1
  fi
  grep -q 'expected exactly one migrated row' "$TMP/gate-error.log" \
    || { cat "$TMP/gate-error.log" >&2; echo 'FAIL: duplicate-row rejection lacked the expected diagnostic' >&2; exit 1; }
)
echo 'ok - duplicate-row parity gate still hard-fails cleanup planning'

echo 'PASS: runtime-memory cleanup is parity-gated, archived, and recoverable'
