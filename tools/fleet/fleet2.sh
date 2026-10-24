#!/usr/bin/env bash
# fleet2.sh — the Fleet explorer screen.
#   `fleet` keeps the group/process screen until this one is ready to replace it.
#   Same options as fleet.sh (including --window); this only adds --explorer.
set -euo pipefail

# Resolve the real script location when invoked through a symlink.
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR=$(cd -P "$(dirname "$SOURCE")" && pwd)
  SOURCE=$(readlink "$SOURCE")
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR=$(cd -P "$(dirname "$SOURCE")" && pwd)
exec "$SCRIPT_DIR/fleet.sh" --explorer "$@"
