#!/bin/sh
set -eu

emit_bootstrap_error() {
  code=$1
  status=$2
  detail=$3
  observed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '{"error":{"code":"%s","detail":"%s","retryable":false,"observed_at":"%s"}}\n' \
    "$code" "$detail" "$observed_at"
  exit "$status"
}

[ "$#" -eq 0 ] || emit_bootstrap_error INVALID_REQUEST 4 'command options are not supported'
[ -n "${CAIRN_ROOT-}" ] || emit_bootstrap_error INTERNAL_FAILURE 18 'Cairn W3a checkout is unavailable'
[ -d "$CAIRN_ROOT" ] || emit_bootstrap_error INTERNAL_FAILURE 18 'Cairn W3a checkout is unavailable'
tsx=${CAIRN_TSX:-$CAIRN_ROOT/node_modules/.bin/tsx}
[ -x "$tsx" ] || emit_bootstrap_error INTERNAL_FAILURE 18 'Cairn W3a TypeScript runtime is unavailable'

exec "$tsx" "$(dirname "$0")/cairn-artifact-read.ts"
