#!/usr/bin/env bash
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

printf '%s\n' 'A changed source body that must fail the exact DB parity gate.' \
  > "$MEM_PROJECTS/-tmp-project/memory/topic.md"
if python3 "$MEM" migrate --apply --all-projects --cleanup-runtime-memory \
    --cleanup-archive "$TMP/mismatch.tar.gz" >/dev/null 2>&1; then
  echo 'FAIL: body mismatch was accepted' >&2
  exit 1
fi
test -d "$MEM_PROJECTS/-tmp-project/memory"
test ! -e "$TMP/mismatch.tar.gz"

printf '%s\n' 'A durable runtime-memory topic with enough detail to migrate.' \
  > "$MEM_PROJECTS/-tmp-project/memory/topic.md"
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

echo 'PASS: runtime-memory cleanup is parity-gated, archived, and recoverable'
