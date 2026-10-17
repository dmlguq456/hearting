#!/usr/bin/env bash
# Every checker must tolerate the `__pycache__` a local Python run leaves in
# any source directory: git ignores it and a release is `git archive`, so it
# is never part of a projection or a release (2026-09-10, after five false
# failures in one night). This test seeds a cache into every tracked Python
# directory of a private checkout copy (tools/checkout-copy.sh) and runs the
# checkers that walk those trees there.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
COPY_PARENT=$(mktemp -d)
trap 'rm -rf "$COPY_PARENT"' EXIT
. "$ROOT/tools/checkout-copy.sh"
checkout_copy "$ROOT" "$COPY_PARENT/repo" || { echo "FAIL - checkout copy"; exit 1; }
cd "$COPY_PARENT/repo"
seeded=()
while IFS= read -r dir; do
  cache="$dir/__pycache__"
  if [ ! -e "$cache" ]; then
    mkdir -p "$cache" && printf 'seed' > "$cache/seed.cpython-312.pyc" && seeded+=("$cache")
  fi
done < <(git ls-files '*.py' | xargs -n1 dirname | sort -u)
echo "seeded ${#seeded[@]} cache directories"
fail=0
run() {
  if "$@" >/tmp/bytecode-cache-tolerance.$$.log 2>&1; then
    echo "ok   - $*"
  else
    echo "FAIL - $*"; grep -v 'SyntaxWarning\|PAGE_TEMPLATE' /tmp/bytecode-cache-tolerance.$$.log | tail -8; fail=1
  fi
}
run python3 tools/generate.py --check
run ./tools/check-adaptation-boundary.sh
run python3 tools/check-model-config.py
run python3 tools/check-unit-config.py
run python3 tools/check-scope-placeholders.py
mkdir -p "$COPY_PARENT/archive"
git archive --format=tar HEAD | tar -xf - -C "$COPY_PARENT/archive"
cd "$COPY_PARENT/archive"
mkdir -p tools/memory/__pycache__ tools/install/__pycache__
printf 'bytecode' > tools/memory/__pycache__/probe.cpython-312.pyc
printf 'bytecode' > tools/install/probe.pyc
run ./tools/check-adaptation-boundary.sh
run python3 tools/sync-missing-projections.py
if [ -e adapters/claude/tools/memory/__pycache__ ] || [ -e adapters/claude/tools/install/probe.pyc ]; then
  echo "FAIL - projection sync copied Python bytecode into the release tree"
  fail=1
else
  echo "ok   - projection sync skips bytecode in a Git archive"
fi
rm -rf tools/memory/__pycache__ tools/install/__pycache__ adapters/claude/tools/memory/__pycache__
unset PYTHONDONTWRITEBYTECODE PYTHONPYCACHEPREFIX
unset AGENT_HOME
HARNESS_STATE_ROOT="$COPY_PARENT/state"
export HARNESS_STATE_ROOT
printf '#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, "utilities")\nimport artifact_producer\n' > hooks/_bytecode_probe.py
chmod +x hooks/_bytecode_probe.py
run hooks/run-hook.sh exec _bytecode_probe.py
rm -f hooks/_bytecode_probe.py
if [ -d utilities/__pycache__ ] || [ -d hooks/__pycache__ ]; then
  echo "FAIL - interactive hook runner wrote bytecode into the release tree"
  fail=1
else
  echo "ok   - interactive hook runner keeps bytecode out of the release tree"
fi
rm -f /tmp/bytecode-cache-tolerance.$$.log
[ "$fail" = 0 ] && echo "bytecode-cache-tolerance: PASS" || { echo "bytecode-cache-tolerance: FAIL"; exit 1; }
