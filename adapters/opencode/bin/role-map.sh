#!/usr/bin/env sh
set -eu

# Concrete provider/model-id strings come from the user copy when valid, with
# the shipped adapter file as a whole-file fallback.
# This script owns role->bucket->tier routing (semantic), never model literals.
_ocdir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
_root=$(CDPATH= cd -- "$_ocdir/../../.." && pwd)
eval "$("$_root/utilities/model-config.sh" --adapter opencode --source-root "$_root")"

usage() {
  cat <<'EOF'
usage: role-map.sh <portable-role>

Prints an OpenCode adapter mapping for a portable model role.

OpenCode uses provider/model-id strings and a variant field for reasoning
profile selection. There is no numeric reasoning-effort config field.

Config knobs:
  AGENT_MODEL_FAST / AGENT_VARIANT_FAST
  AGENT_MODEL_BALANCED / AGENT_VARIANT_BALANCED
  AGENT_MODEL_DEEP / AGENT_VARIANT_DEEP
  AGENT_MODEL_EXTERNAL / AGENT_VARIANT_EXTERNAL
  AGENT_MODEL_ORCHESTRATOR / AGENT_VARIANT_ORCHESTRATOR
  AGENT_EXTERNAL_CMD
EOF
}

[ "${1:-}" != "-h" ] && [ "${1:-}" != "--help" ] || { usage; exit 0; }
[ "$#" -ge 1 ] || { usage >&2; exit 64; }

raw=$*
role=$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr '_-' '  ' | awk '{$1=$1; print}')

family=fast
canonical=$role
available=1
status=default
reason=""
external_cmd_bin=""

case "$role" in
  "fast reviewer"|"fast fact checker"|"fast fact-checker"|"fast writer"|"fast tool worker")
    family=fast
    ;;
  "fast implementer"|"orchestrator"|"external adversary orchestrator")
    family=balanced
    ;;
  "deep reviewer"|"deep maker"|"deep editor"|"deep orchestrator")
    family=deep
    ;;
  "external adversary")
    family=external
    if [ -z "${AGENT_MODEL_EXTERNAL:-}" ] && [ -z "${AGENT_EXTERNAL_CMD:-}" ]; then
      available=0
      status=unavailable
      reason="set AGENT_MODEL_EXTERNAL or AGENT_EXTERNAL_CMD for an independent external adversary"
    elif [ -n "${AGENT_EXTERNAL_CMD:-}" ]; then
      external_cmd_bin=${AGENT_EXTERNAL_CMD%% *}
      if ! command -v "$external_cmd_bin" >/dev/null 2>&1; then
        available=0
        status=unavailable
        reason="AGENT_EXTERNAL_CMD not found: $external_cmd_bin"
      fi
    fi
    ;;
  *)
    echo "opencode role-map: unknown portable role: $raw" >&2
    usage >&2
    exit 64
    ;;
esac

case "$family" in
  fast)
    model=${AGENT_MODEL_FAST:-$CFG_TIER_MINI_MODEL}
    variant=${AGENT_VARIANT_FAST:-$CFG_TIER_MINI_VARIANT}
    [ -n "${AGENT_MODEL_FAST:-}" ] && status=configured || status=default
    ;;
  balanced)
    model=${AGENT_MODEL_BALANCED:-${AGENT_MODEL_ORCHESTRATOR:-$CFG_TIER_LIGHT_MODEL}}
    variant=${AGENT_VARIANT_BALANCED:-${AGENT_VARIANT_ORCHESTRATOR:-$CFG_TIER_LIGHT_VARIANT}}
    { [ -n "${AGENT_MODEL_BALANCED:-}" ] || [ -n "${AGENT_MODEL_ORCHESTRATOR:-}" ]; } && status=configured || status=default
    ;;
  deep)
    model=${AGENT_MODEL_DEEP:-$CFG_TIER_BALANCED_DEEP_MODEL}
    variant=${AGENT_VARIANT_DEEP:-$CFG_TIER_BALANCED_DEEP_VARIANT}
    [ -n "${AGENT_MODEL_DEEP:-}" ] && status=configured || status=default
    ;;
  external)
    model=${AGENT_MODEL_EXTERNAL:-external-command}
    variant=${AGENT_VARIANT_EXTERNAL:-runtime-default}
    [ "$available" -eq 1 ] && status=configured
    ;;
esac

printf 'role=%s\n' "$canonical"
printf 'adapter=opencode\n'
printf 'source=roles/README.md\n'
printf 'family=%s\n' "$family"
printf 'model=%s\n' "$model"
printf 'variant=%s\n' "$variant"
printf 'available=%s\n' "$available"
printf 'status=%s\n' "$status"
[ -z "$external_cmd_bin" ] || printf 'external_command=%s\n' "$AGENT_EXTERNAL_CMD"
[ -z "$reason" ] || printf 'reason=%s\n' "$reason"
