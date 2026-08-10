#!/usr/bin/env sh
set -eu
# Concrete model IDs and default efforts come from the user copy when valid,
# with the shipped adapter file as a whole-file fallback.
# Role->tier grouping is ALSO config-owned (CFG_ROLES_DEEP/LIGHT): membership is
# derived here instead of hardcoded case labels (2026-07-22 단일원천화). Env
# override chain preserved: deep=SOL, light=BALANCED>LUNA (BALANCED, formerly the
# orchestrator-only knob, now covers the whole light tier — operator override).
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$dir/../../.." && pwd)
eval "$("$root/utilities/model-config.sh" --adapter codex --source-root "$root")"
role=$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | tr '_-' '  ' | awk '{$1=$1; print}')
family=gpt
case "|$CFG_ROLES_DEEP|" in
  *"|$role|"*)
    model=${CODEX_MODEL_SOL:-$CFG_TIER_DEEP_MODEL}; reasoning=${CODEX_REASONING_SOL:-$CFG_TIER_DEEP_EFFORT};;
  *)
    case "|$CFG_ROLES_LIGHT|" in
      *"|$role|"*)
        model=${CODEX_MODEL_BALANCED:-${CODEX_MODEL_LUNA:-$CFG_TIER_LIGHT_MODEL}}; reasoning=${CODEX_REASONING_BALANCED:-${CODEX_REASONING_LUNA:-$CFG_TIER_LIGHT_EFFORT}};;
      *) echo "codex model-map: unknown role: ${1:-}" >&2; exit 64;;
    esac;;
esac
printf 'adapter=codex\nfamily=%s\nexact_model_id=%s\nreasoning=%s\nprobe=opt-in:codex exec --ephemeral --sandbox read-only --model <id> -c model_reasoning_effort=<level>\n' "$family" "$model" "$reasoning"
