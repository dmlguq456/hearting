#!/usr/bin/env bash
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../tools/run-tests.py" --direct-run bash "$0" "$@"
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HELPER="$ROOT/utilities/verify-files.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

ok() { printf 'ok - %s\n' "$1"; }
fail() { printf 'not ok - %s\n' "$1" >&2; exit 1; }

fixture="$TMP/tree"
mkdir -p "$fixture/src" "$fixture/dir with space" "$fixture/node_modules/dep"
printf 'a\n' > "$fixture/src/a.sh"
printf 'b\n' > "$fixture/dir with space/b.sh"
printf 'n\n' > "$fixture/x
y.sh"
printf 'skip\n' > "$fixture/node_modules/dep/skip.sh"
printf 'doc\n' > "$fixture/notes.md"

expected="$TMP/expected.bin"
# LC_ALL=C sort order over full paths: "dir with space/b.sh" < "node_modules..."
# < "src/a.sh" < "x\ny.sh" (excluded and non-*.sh entries absent).
printf '%s\0' \
  "$fixture/dir with space/b.sh" \
  "$fixture/src/a.sh" \
  "$fixture/x
y.sh" > "$expected"

run_helper() { # $1=interpreter
  "$1" "$HELPER" --root "$fixture" --name '*.sh' \
    --exclude-path '*/node_modules' --print0
}

# 1. Exact NUL-delimited list: spaces and newlines survive, prune works.
run_helper sh > "$TMP/out.sh.bin"
cmp -s "$expected" "$TMP/out.sh.bin" || fail "sh print0 list matches expected bytes"
ok "sh print0 list matches expected bytes"

# 2. Identical bytes when the script itself is executed by bash and zsh.
run_helper bash > "$TMP/out.bash.bin"
cmp -s "$expected" "$TMP/out.bash.bin" || fail "bash-executed helper matches sh output"
ok "bash-executed helper matches sh output"

if command -v zsh >/dev/null 2>&1; then
  run_helper zsh > "$TMP/out.zsh.bin"
  cmp -s "$expected" "$TMP/out.zsh.bin" || fail "zsh-executed helper matches sh output"
  ok "zsh-executed helper matches sh output"
else
  ok "zsh-executed helper matches sh output # SKIP zsh unavailable"
fi

# 3. Run mode passes the same argv, null-delimited, through xargs.
sh "$HELPER" --root "$fixture" --name '*.sh' --exclude-path '*/node_modules' \
  -- sh -c 'printf "%s\0" "$@"' argv0 > "$TMP/out.run.bin"
cmp -s "$expected" "$TMP/out.run.bin" || fail "run mode forwards identical argv"
ok "run mode forwards identical argv"

# 4. --max-args batches one file per invocation.
count=$(sh "$HELPER" --root "$fixture" --name '*.sh' --exclude-path '*/node_modules' \
  --max-args 1 -- sh -c 'echo tick' argv0 | wc -l | tr -d ' ')
[ "$count" = "3" ] || fail "--max-args 1 yields one invocation per file (got $count)"
ok "--max-args 1 yields one invocation per file"

# 5. A failing command propagates a nonzero xargs status.
status=0
sh "$HELPER" --root "$fixture" --name '*.sh' -- false >/dev/null 2>&1 || status=$?
[ "$status" -ne 0 ] || fail "failing command propagates nonzero exit"
ok "failing command propagates nonzero exit (status=$status)"

# 6. Empty match runs nothing and succeeds.
sh "$HELPER" --root "$fixture" --name '*.nope' -- false >/dev/null 2>&1 \
  || fail "empty match must exit 0 without running the command"
ok "empty match exits 0 without running the command"

# 7. Usage errors are closed with exit 2.
status=0
sh "$HELPER" --root "$fixture" >/dev/null 2>&1 || status=$?
[ "$status" = "2" ] || fail "missing mode exits 2 (got $status)"
ok "missing mode exits 2"

printf 'all verify-files checks passed\n'
