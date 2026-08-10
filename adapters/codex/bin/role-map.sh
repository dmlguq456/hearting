#!/usr/bin/env sh
set -eu

# Concrete model IDs and default efforts come from the user copy when valid,
# with the shipped adapter file as a whole-file fallback.
_cmdir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
_root=$(CDPATH= cd -- "$_cmdir/../../.." && pwd)
eval "$("$_root/utilities/model-config.sh" --adapter codex --source-root "$_root")"

usage() {
  cat <<'EOF'
usage: role-map.sh <portable-role|pipeline-stage>

Prints a Codex adapter mapping for a portable model role or pipeline-stage alias.

Config knobs:
  AGENT_MODEL_FAST / AGENT_REASONING_FAST
  AGENT_MODEL_DEEP / AGENT_REASONING_DEEP
  AGENT_MODEL_TERRA / AGENT_REASONING_TERRA
  AGENT_MODEL_LUNA / AGENT_REASONING_LUNA
  AGENT_MODEL_BALANCED / AGENT_REASONING_BALANCED
  AGENT_MODEL_EXTERNAL / AGENT_REASONING_EXTERNAL
  AGENT_MODEL_ORCHESTRATOR / AGENT_REASONING_ORCHESTRATOR
  AGENT_EXTERNAL_CMD
EOF
}

[ "${1:-}" != "-h" ] && [ "${1:-}" != "--help" ] || { usage; exit 0; }
[ "$#" -ge 1 ] || { usage >&2; exit 64; }

raw=$*
role=$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr '_-' '  ' | awk '{$1=$1; print}')

# Runtime team agents are retired (재홈 2026-07-22, core/CONVENTIONS.md §2.3):
# dispatchable behavior lives in the portable unit catalog (roles/units/), and a
# pipeline-stage alias resolves to its portable ROLE, never to a native team
# agent. The only remaining Codex native agent is the kernel helper
# (memory-scout); native_agent_path is emitted only when that concrete file
# actually exists, so retired team tomls can never be re-emitted here.
pipeline_stage=""
portable_model_role=""
native_agent=""
case "$role" in
  "planning")
    pipeline_stage=planning
    portable_model_role="deep maker"
    ;;
  "implementation")
    pipeline_stage=implementation
    portable_model_role="fast implementer"
    ;;
  "verification")
    # variable reviewer/verifier budget derived from intensity (CONVENTIONS §2.3)
    pipeline_stage=verification
    portable_model_role="variable reviewer"
    ;;
  "report"|"reporting")
    pipeline_stage=report
    portable_model_role="fast writer"
    ;;
esac
if [ -n "$pipeline_stage" ]; then
  role=$portable_model_role
fi

family=fast
canonical=$role
available=1
status=default
reason=""
external_cmd_bin=""
role_set=""

case "$role" in
  "variable reviewer")
    family=role-set
    role_set="fast reviewer,deep reviewer,external adversary"
    ;;
  "variable research reviewer")
    family=role-set
    role_set="fast fact checker,deep reviewer,external adversary"
    ;;
  "fast implementer by default")
    family=role-set
    role_set="fast implementer"
    ;;
  "deep maker plus fast tool worker")
    family=role-set
    role_set="deep maker,fast tool worker"
    ;;
  "deep maker plus verifier")
    family=role-set
    role_set="deep maker,fast reviewer"
    ;;
  "deep maker / fast reviewer by mode")
    family=role-set
    role_set="deep maker,fast reviewer"
    ;;
  "external adversary plus orchestrator")
    family=role-set
    role_set="external adversary,orchestrator"
    ;;
esac

if [ -z "$role_set" ]; then
  case "$role" in
    "fast reviewer"|"fast fact checker"|"fast fact-checker"|"fast writer"|"fast tool worker")
      family=fast
      ;;
    "fast implementer")
      family=implementer
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
    "orchestrator"|"external adversary orchestrator")
      family=balanced
      ;;
    *)
      echo "codex role-map: unknown portable role: $raw" >&2
      usage >&2
      exit 64
      ;;
  esac
fi

case "$family" in
  fast)
    model=${AGENT_MODEL_LUNA:-${AGENT_MODEL_FAST:-$CFG_TIER_LIGHT_MODEL}}
    reasoning=${AGENT_REASONING_LUNA:-${AGENT_REASONING_FAST:-$CFG_TIER_LIGHT_EFFORT}}
    [ -n "${AGENT_MODEL_LUNA:-}${AGENT_REASONING_LUNA:-}${AGENT_MODEL_FAST:-}${AGENT_REASONING_FAST:-}" ] && status=configured || status=default
    ;;
  implementer)
    # 2026-07-22 사용자 원칙: 티어 고정, 특례 없음 — implementer는 light 티어를 탄다.
    model=${AGENT_MODEL_LUNA:-${AGENT_MODEL_FAST:-$CFG_TIER_LIGHT_MODEL}}
    reasoning=${AGENT_REASONING_LUNA:-${AGENT_REASONING_FAST:-$CFG_TIER_LIGHT_EFFORT}}
    [ -n "${AGENT_MODEL_LUNA:-}${AGENT_REASONING_LUNA:-}${AGENT_MODEL_FAST:-}${AGENT_REASONING_FAST:-}" ] && status=configured || status=default
    ;;
  deep)
    model=${AGENT_MODEL_DEEP:-$CFG_TIER_DEEP_MODEL}
    reasoning=${AGENT_REASONING_DEEP:-$CFG_TIER_DEEP_EFFORT}
    [ -n "${AGENT_MODEL_DEEP:-}${AGENT_REASONING_DEEP:-}" ] && status=configured || status=default
    ;;
  external)
    if [ "$available" -eq 0 ] && [ -z "${AGENT_MODEL_EXTERNAL:-}" ] && [ -z "${AGENT_EXTERNAL_CMD:-}" ]; then
      model=unconfigured
    elif [ -n "${AGENT_EXTERNAL_CMD:-}" ] && [ -z "${AGENT_MODEL_EXTERNAL:-}" ]; then
      model=external-command
    else
      model=${AGENT_MODEL_EXTERNAL:-$CFG_TIER_DEEP_MODEL}
    fi
    reasoning=${AGENT_REASONING_EXTERNAL:-$CFG_TIER_DEEP_EFFORT}
    [ "$available" -eq 1 ] && status=configured
    ;;
  balanced)
    model=${AGENT_MODEL_BALANCED:-${AGENT_MODEL_ORCHESTRATOR:-$CFG_TIER_LIGHT_MODEL}}
    reasoning=${AGENT_REASONING_BALANCED:-${AGENT_REASONING_ORCHESTRATOR:-$CFG_TIER_LIGHT_EFFORT}}
    [ -n "${AGENT_MODEL_BALANCED:-}${AGENT_REASONING_BALANCED:-}${AGENT_MODEL_ORCHESTRATOR:-}${AGENT_REASONING_ORCHESTRATOR:-}" ] && status=configured || status=default
    ;;
  role-set)
    model=role-set
    reasoning=select-by-mode
    status=role-set
    ;;
esac

printf 'role=%s\n' "$canonical"
printf 'adapter=codex\n'
printf 'source=roles/README.md\n'
printf 'family=%s\n' "$family"
if [ -n "$pipeline_stage" ]; then
  printf 'pipeline_stage=%s\n' "$pipeline_stage"
  printf 'portable_model_role=%s\n' "$portable_model_role"
  printf 'unit_catalog=roles/units/\n'
fi
if [ -n "$native_agent" ] && [ -f "$_cmdir/../agents/$native_agent.toml" ]; then
  printf 'native_agent_path=adapters/codex/agents/%s.toml\n' "$native_agent"
fi
if [ -n "$role_set" ]; then
  printf 'role_set=%s\n' "$role_set"
fi
printf 'model=%s\n' "$model"
printf 'reasoning=%s\n' "$reasoning"
printf 'available=%s\n' "$available"
printf 'status=%s\n' "$status"
[ -z "$external_cmd_bin" ] || printf 'external_command=%s\n' "$AGENT_EXTERNAL_CMD"
[ -z "$reason" ] || printf 'reason=%s\n' "$reason"
