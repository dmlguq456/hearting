#!/usr/bin/env sh
# Post-publish smoke: install the just-published release into a disposable
# HOME exactly the way a user would, verify the projected surface, then
# uninstall and prove nothing harness-owned remains.
set -eu

REPOSITORY=${1:?usage: post-publish-smoke.sh <owner/repo> <tag>}
VERSION=${2:?usage: post-publish-smoke.sh <owner/repo> <tag>}

SMOKE=$(mktemp -d)
trap 'rm -rf "$SMOKE"' EXIT HUP INT TERM
mkdir -p "$SMOKE/home" "$SMOKE/bin"

# A stub Codex CLI lets the launcher-binding chain run on hosts without the
# real CLI while proving the wrapper delegates to whatever PATH provides.
printf '%s\n' '#!/bin/sh' 'echo codex-stub-ok' > "$SMOKE/bin/codex"
chmod +x "$SMOKE/bin/codex"

url="https://github.com/$REPOSITORY/releases/download/$VERSION"

# Release assets propagate lazily right after publish (v2.0.1: 404s for the
# first minutes), so every asset fetch shares one ~5-minute retry envelope.
fetch_asset() {
  _attempt=1
  while ! curl -fsSL "$1" -o "$2"; do
    if [ "$_attempt" -ge 30 ]; then
      echo "post-publish smoke: $1 not downloadable after $_attempt tries" >&2
      exit 1
    fi
    _attempt=$((_attempt + 1))
    sleep 10
  done
}

fetch_asset "$url/install.sh" "$SMOKE/install.sh"
fetch_asset "$url/install.sh.sha256" "$SMOKE/install.sh.sha256"
(cd "$SMOKE" && sha256sum -c install.sh.sha256 >/dev/null)

run_isolated() {
  env -u XDG_DATA_HOME -u XDG_CONFIG_HOME -u XDG_STATE_HOME \
    -u CODEX_HOME -u AGENT_HOME -u AGENT_CODEX_LAUNCHER_GUARD_PID \
    HOME="$SMOKE/home" PATH="$SMOKE/home/.local/bin:$SMOKE/bin:$PATH" "$@"
}

run_isolated timeout 300 sh "$SMOKE/install.sh" \
  --runtime claude --runtime codex --no-auto-update >/dev/null

# Every hook command file that settings.json registers must exist, and the
# launcher must bind the stub CLI, never a harness wrapper.
HOME_DIR="$SMOKE/home" STUB="$SMOKE/bin/codex" python3 - <<'PY'
import json, os, re, sys
from pathlib import Path

home = Path(os.environ["HOME_DIR"])
settings = json.loads((home / ".claude" / "settings.json").read_text())
missing = []
for entries in settings.get("hooks", {}).values():
    for entry in entries:
        for hook in entry.get("hooks", []):
            command = hook.get("command", "")
            for raw in re.findall(r"\$HOME/[^\s\"']+", command):
                candidate = Path(str(home) + raw[len("$HOME"):])
                if candidate.suffix and not candidate.exists():
                    missing.append(raw)
if missing:
    sys.exit(f"settings.json references missing files: {missing}")
state = json.loads((home / ".codex" / ".harness" / "codex-launcher.json").read_text())
if state["real_command"] != os.environ["STUB"]:
    sys.exit(f"launcher bound {state['real_command']} instead of {os.environ['STUB']}")
PY

# The wrapper must delegate instantly; a hang here is the recursion regression.
out=$(run_isolated timeout 30 codex --version)
if [ "$out" != "codex-stub-ok" ]; then
  echo "post-publish smoke: wrapper pass-through failed: $out" >&2
  exit 1
fi

for runtime in claude codex; do
  run_isolated "$SMOKE/home/.local/bin/harness" runtime status \
    --runtime "$runtime" --json > "$SMOKE/status-$runtime.json"
  python3 - "$SMOKE/status-$runtime.json" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))
assert row["freshness"] == "fresh", row["freshness"]
PY
done

# Model routing becomes user-owned on first install. Uninstall must preserve the
# exact regular-file bytes for both runtimes while removing harness projections.
sha256sum \
  "$SMOKE/home/.claude/agent-config/models.conf" \
  "$SMOKE/home/.codex/agent-config/models.conf" \
  > "$SMOKE/user-model-config.sha256"

run_isolated "$SMOKE/home/.local/bin/harness" uninstall codex >/dev/null
run_isolated "$SMOKE/home/.local/bin/harness" uninstall claude >/dev/null
if [ -e "$SMOKE/home/.local/bin/codex" ]; then
  echo "post-publish smoke: uninstall left the codex wrapper" >&2
  exit 1
fi
sha256sum -c "$SMOKE/user-model-config.sha256" >/dev/null || {
  echo "post-publish smoke: uninstall changed a user-owned model config" >&2
  exit 1
}
codex_model_config="$SMOKE/home/.codex/agent-config/models.conf"
leftovers=$(find "$SMOKE/home/.codex" ! -type d ! -path "$codex_model_config" 2>/dev/null | wc -l)
if [ "$leftovers" -ne 0 ]; then
  find "$SMOKE/home/.codex" ! -type d ! -path "$codex_model_config" >&2
  echo "post-publish smoke: uninstall left $leftovers harness-owned file(s) in .codex" >&2
  exit 1
fi
if [ -e "$SMOKE/home/.claude/CLAUDE.md" ] || [ -L "$SMOKE/home/.claude/CLAUDE.md" ]; then
  echo "post-publish smoke: uninstall left the claude bootstrap projection" >&2
  exit 1
fi

echo "post-publish smoke: PASS ($VERSION)"
