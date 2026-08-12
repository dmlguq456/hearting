#!/usr/bin/env bash
# fleet.sh — current-terminal launcher for fleet.py.
#   Default: run directly in the current full-size terminal, inside or outside tmux.
#   --window: open a new full-size tmux window when inside tmux; otherwise run directly.
#   Keyboard scrolling is the default; mouse toggles are opt-in via `tmux set -g mouse on`.
#   Pass all fleet.py options through unchanged; only --window is consumed here.
set -euo pipefail

# Resolve the real script location when invoked through a symlink.
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR=$(cd -P "$(dirname "$SOURCE")" && pwd)
  SOURCE=$(readlink "$SOURCE")
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR=$(cd -P "$(dirname "$SOURCE")" && pwd)
FLEET_PY="$SCRIPT_DIR/fleet.py"
HARNESS_ROOT=$(cd -P "$SCRIPT_DIR/../.." && pwd)
if [ -f "$HARNESS_ROOT/core/CORE.md" ]; then
  # The launcher owns source identity. Mutable dispatch state continues to
  # follow an inherited AGENT_DISPATCH_JOBS or each row's registry path.
  export AGENT_HOME="$HARNESS_ROOT"
fi

# FLEET_PYTHON overrides interpreter discovery. On Windows the `python`/`python3`
# on PATH are the WindowsApps app-execution aliases (a pymanager stub that can
# crash with "Internal error 0x00000001"); the launcher points FLEET_PYTHON at a
# real python.exe to bypass it.
PY=${FLEET_PYTHON:-$(command -v python3 || command -v python || true)}
if [ -z "$PY" ]; then echo "fleet: python3 is required." >&2; exit 1; fi
if [ ! -f "$FLEET_PY" ]; then echo "fleet: fleet.py was not found ($FLEET_PY)." >&2; exit 1; fi

# Remove --window exactly once before routing because fleet.py does not accept it.
want_window=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--window" ]; then
    want_window=1
  else
    ARGS+=("$a")
  fi
done

# Snapshot and JSON modes need no terminal launcher.
direct=0
for a in ${ARGS[@]+"${ARGS[@]}"}; do case "$a" in --once|--json) direct=1 ;; esac; done

run_direct() { exec "$PY" "$FLEET_PY" "$@"; }

# Curses renders the persistent key guide; no pre-launch scroll notice is needed.

if [ "$direct" = "1" ]; then
  run_direct ${ARGS[@]+"${ARGS[@]}"}
fi

if [ "$want_window" = "1" ]; then
  if [ -n "${TMUX:-}" ]; then
    cmd="$(printf '%q' "$PY") $(printf '%q' "$FLEET_PY")"
    for a in ${ARGS[@]+"${ARGS[@]}"}; do cmd="$cmd $(printf '%q' "$a")"; done
    tmux new-window "$cmd"
    exit 0
  fi
  echo "fleet: ignoring --window outside tmux and running in the current terminal." >&2
  run_direct ${ARGS[@]+"${ARGS[@]}"}
fi

# Default: run directly in the current full-size terminal.
run_direct ${ARGS[@]+"${ARGS[@]}"}
