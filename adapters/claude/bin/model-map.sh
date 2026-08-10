#!/usr/bin/env sh
set -eu
# Concrete model IDs and default efforts come from the user copy when valid,
# with the shipped adapter file as a whole-file fallback.
# Role->tier grouping is ALSO config-owned (CFG_ROLES_DEEP/LIGHT): membership is
# derived here instead of hardcoded case labels (2026-07-22 단일원천화 — 종전엔
# case 라벨이 conf grouping을 중복 보유해 역할 이동 시 두 곳을 고쳐야 했다).
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$dir/../../.." && pwd)
eval "$("$root/utilities/model-config.sh" --adapter claude --source-root "$root")"
role=$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | tr '_-' '  ' | awk '{$1=$1; print}')
family=claude
case "|$CFG_ROLES_DEEP|" in
  *"|$role|"*)
    model=${CLAUDE_MODEL_DEEP:-$CFG_TIER_DEEP_MODEL}; effort=${CLAUDE_EFFORT_DEEP:-$CFG_TIER_DEEP_EFFORT};;
  *)
    case "|$CFG_ROLES_LIGHT|" in
      *"|$role|"*)
        model=${CLAUDE_MODEL_BALANCED:-$CFG_TIER_LIGHT_MODEL}; effort=${CLAUDE_EFFORT_BALANCED:-$CFG_TIER_LIGHT_EFFORT};;
      *) echo "claude model-map: unknown role: ${1:-}" >&2; exit 64;;
    esac;;
esac
printf 'adapter=claude\nfamily=%s\nexact_model_id=%s\nreasoning=%s\nprobe=opt-in:claude -p --no-session-persistence --permission-mode plan --max-turns 1 --model <id> --effort <level>\n' "$family" "$model" "$effort"
