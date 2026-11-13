#!/usr/bin/env bash
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/run-tests.py" --direct-run bash "$0" "$@"
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
rm -f tools/install/probe.pyc
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
if python3 - "$PWD" "$COPY_PARENT" <<'PY'
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

release = Path(sys.argv[1]).resolve()
scratch = Path(sys.argv[2]).resolve()
home = scratch / "probe home with spaces"
claude_config = home / ".claude"
claude_config.mkdir(parents=True, exist_ok=True)
(claude_config / "hooks").symlink_to(release / "hooks", target_is_directory=True)

def commands(node):
    if isinstance(node, dict):
        command = node.get("command")
        if isinstance(command, str):
            yield command
        for value in node.values():
            yield from commands(value)
    elif isinstance(node, list):
        for value in node:
            yield from commands(value)

bad = []
for config in (release / "adapters/claude/settings.json",
               release / "adapters/codex/hooks/hooks.json"):
    for command in commands(json.loads(config.read_text(encoding="utf-8"))):
        if ("python" in command and "run-hook.sh" not in command
                and "tools/memory/mem.py" not in command):
            bad.append((str(config.relative_to(release)), command))
if bad:
    for path, command in bad:
        print(f"FAIL - Python hook bypasses run-hook.sh: {path}: {command}")
    raise SystemExit(1)

settings = json.loads((release / "adapters/claude/settings.json").read_text(encoding="utf-8"))
hook_commands = [command for command in commands(settings)
                 if "start-receipt-context.py" in command
                 or "git-index-lock-context.py" in command]
if len(hook_commands) != 3:
    print(f"FAIL - expected three configured direct Python hooks, found {len(hook_commands)}")
    raise SystemExit(1)
if any("run-hook.sh" not in command for command in hook_commands):
    print("FAIL - configured direct Python hook bypasses run-hook.sh")
    raise SystemExit(1)

entries = []
git_hook_number = 0
for command in hook_commands:
    if "start-receipt-context.py" in command:
        event = "PostToolUse"
        label = "hook-start-receipt"
        cache_module = "start_receipt"
    else:
        event = "PostToolUse" if git_hook_number == 0 else "PostToolUseFailure"
        label = f"hook-git-index-{git_hook_number}"
        cache_module = "git_index_lock"
        git_hook_number += 1
    payload = {"session_id": "bytecode-probe", "hook_event_name": event,
               "tool_name": "Bash", "tool_input": {"command": "true"}}
    entries.append((label, command, json.dumps(payload), cache_module))
entries.extend([
    ("statusline",
     "bash " + shlex.quote(str(release / "adapters/claude/statusline.sh")),
     json.dumps({"session_id": "bytecode-probe", "cwd": str(release),
                 "model": {"id": "probe"}}), "session_handle"),
    ("mem", shlex.join([str(release / "tools/memory/mem.py"), "--help"]), "",
     "git_exchange_v2"),
    ("compute-hosts", shlex.join([str(release / "utilities/compute-hosts"), "--help"]), "",
     "session_identity"),
])

def base_env():
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("PYTHON") or key == "PYTHONDONTWRITEBYTECODE":
            env.pop(key, None)
    env.update({
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude_config),
        "AGENT_HOME": str(release),
        "XDG_CONFIG_HOME": str(scratch / "xdg config"),
        "MEM_STORE": str(scratch / "memory store"),
        "MEM_PROJECTS": str(home / "projects"),
        "CODEX_SESSIONS": str(home / "codex sessions"),
        "MEM_PROFILE": str(scratch / "memory profile"),
        "COMPUTE_HOSTS_CONFIG": str(scratch / "compute hosts.yaml"),
        # The synchronous session helper exercises statusline imports. Keep
        # unrelated detached account/title refreshers out of this fixture.
        "HARNESS_CAPACITY_REFRESH_DISABLE": "1",
        "FLEET_TITLE_DISABLE": "1",
    })
    return env

def release_bytecode():
    return sorted(path for path in release.rglob("*")
                  if path.is_file() and path.suffix in (".pyc", ".pyo"))

def clear_release_bytecode(paths):
    for path in paths:
        path.unlink(missing_ok=True)
    for directory in sorted(release.rglob("__pycache__"), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass

failures = []
def exercise(label, command, payload, policy, cache_module):
    env = base_env()
    state_root = scratch / f"state {policy} {label}"
    xdg_root = scratch / f"xdg state {policy} {label}"
    prefix_root = scratch / f"caller cache {policy} {label}"
    env["HARNESS_STATE_ROOT"] = str(state_root)
    env["XDG_STATE_HOME"] = str(xdg_root)
    expected = state_root / "pycache"
    if policy == "xdg":
        env["HARNESS_STATE_ROOT"] = ""
        expected = xdg_root / "hearting" / "pycache"
    elif policy == "home":
        env["HARNESS_STATE_ROOT"] = ""
        env["XDG_STATE_HOME"] = ""
        expected = home / ".local/state/hearting/pycache"
    elif policy == "caller-prefix":
        env["PYTHONPYCACHEPREFIX"] = str(prefix_root)
        expected = prefix_root
    elif policy == "dontwrite":
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        expected = None

    try:
        result = subprocess.run(command, shell=True, input=payload, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, timeout=20)
    except subprocess.TimeoutExpired:
        failures.append(f"{label}/{policy}: command timed out; descendants did not finish")
        print(f"FAIL - {label}/{policy}: command timed out; descendants did not finish")
        return
    local = release_bytecode()
    cache = list(expected.rglob("*.pyc")) if expected and expected.exists() else []
    module_cache = [path for path in cache if path.name.startswith(cache_module + ".")]
    okay = result.returncode == 0 and not local
    if expected is not None:
        okay = okay and bool(cache) and bool(module_cache)
    else:
        forbidden = [state_root, xdg_root, prefix_root]
        okay = okay and not any(root.exists() and list(root.rglob("*.pyc"))
                                for root in forbidden)
    if okay:
        detail = (f"cache_files={len(cache)}, consumer_module={cache_module}"
                  if expected is not None else "bytecode=disabled")
        print(f"ok   - {label}/{policy}: exit={result.returncode}, {detail}, release bytecode=0")
    else:
        failures.append(f"{label}/{policy}: exit={result.returncode}, cache_files={len(cache)}, "
                        f"consumer_module={len(module_cache)}, release_bytecode={len(local)}")
        print(f"FAIL - {label}/{policy}: exit={result.returncode}, cache_files={len(cache)}, "
              f"consumer_module={len(module_cache)}, release_bytecode={len(local)}")
        if result.stdout:
            print("stdout:", result.stdout[-600:].rstrip())
        if result.stderr:
            print("stderr:", result.stderr[-600:].rstrip())
        if local:
            print("release paths:", ", ".join(str(path.relative_to(release)) for path in local[:8]))
    clear_release_bytecode(local)

for label, command, payload, cache_module in entries:
    for policy in ("state", "xdg", "home", "caller-prefix", "dontwrite"):
        exercise(label, command, payload, policy, cache_module)

if failures:
    print(f"bytecode entrypoint matrix: FAIL ({len(failures)} failed cases)")
    raise SystemExit(1)
print(f"bytecode entrypoint matrix: PASS ({len(entries)} paths x 5 cache policies)")
PY
then
  :
else
  fail=1
fi
rm -f /tmp/bytecode-cache-tolerance.$$.log
[ "$fail" = 0 ] && echo "bytecode-cache-tolerance: PASS" || { echo "bytecode-cache-tolerance: FAIL"; exit 1; }
