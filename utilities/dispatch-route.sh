#!/usr/bin/env sh
# Deterministic, read-only dispatch candidate selector.
set -eu

stage=; capability=; intensity=standard; qa=; required=; adapter=; family=; role=; maker_family=; jobs=
while [ $# -gt 0 ]; do
  case "$1" in
    --stage) stage=${2:?}; shift 2;; --capability) capability=${2:?}; shift 2;;
    --intensity) intensity=${2:?}; shift 2;; --qa) qa=${2:?}; shift 2;;
    --required-surface) required="${required}${required:+,}${2:?}"; shift 2;;
    --adapter) adapter=${2:?}; shift 2;; --family) family=${2:?}; shift 2;;
    --role) role=${2:?}; shift 2;; --maker-family) maker_family=${2:?}; shift 2;;
    --jobs) jobs=${2:?}; shift 2;; -h|--help) echo 'usage: dispatch-route.sh --stage <stage> [--adapter claude|codex|opencode] [--family claude|gpt|unknown] [--role <portable-role>] [--maker-family claude|gpt] [--jobs <jobs.log>]'; exit 0;;
    *) echo "dispatch-route: unknown arg $1" >&2; exit 64;;
  esac
done
[ -n "$stage" ] || { echo 'dispatch-route: --stage is required' >&2; exit 64; }
case "$adapter" in ''|claude|codex|opencode) ;; *) echo "dispatch-route: unknown adapter: $adapter" >&2; exit 64;; esac
case "$family" in ''|claude|gpt|unknown) ;; *) echo "dispatch-route: unknown family: $family" >&2; exit 64;; esac
case "$maker_family" in ''|claude|gpt|unknown) ;; *) echo "dispatch-route: unknown maker family: $maker_family" >&2; exit 64;; esac
case "$adapter:$family" in claude:gpt|codex:claude|opencode:claude|opencode:gpt) echo 'dispatch-route: adapter/family conflict' >&2; exit 64;; esac

# selector-paths: resolve this script's real location so internal helper
# lookups work when it is invoked through the adapters/<harness>/utilities/
# symlink projection. readlink -f canonicalizes the symlink to the true
# utilities/ dir; if readlink lacks -f (non-GNU) it fails and we keep the
# prior $0-based dirname path.
real_self=$(readlink -f "$0" 2>/dev/null) || real_self=$0
self_dir=$(CDPATH= cd -- "$(dirname -- "$real_self")" && pwd)
repo_root=$(CDPATH= cd -- "$self_dir/.." && pwd)

# SD-66: profiles/dispatch-defaults.yaml is the user-declared source for
# stage-affinity (SD-22 cascade step 3). Validate unconditionally so a
# malformed config fails loud even when an explicit --adapter/--family
# already decides this call; resolution honors DISPATCH_DEFAULTS_CONFIG for
# fixtures via utilities/dispatch-defaults.py.
defaults_script="$self_dir/dispatch-defaults.py"
python3 "$defaults_script" validate >/dev/null || { echo 'dispatch-route: invalid dispatch-defaults config' >&2; exit 64; }
config_affinity=$(python3 "$defaults_script" affinity --capability "$capability" --stage "$stage")
allocation_info=$(python3 "$defaults_script" allocation)
allocation_strategy=$(printf '%s\n' "$allocation_info" | awk -F= '$1=="strategy" {print $2}')
allocation_window=$(printf '%s\n' "$allocation_info" | awk -F= '$1=="window" {print $2}')
allocation_order=$(printf '%s\n' "$allocation_info" | awk -F= '$1=="harness_order" {print $2}')

case "$stage" in
  plan|planning|architecture|decomposition) default_role='deep maker'; affinity=codex;;
  *review*|*test*|checker) default_role='deep reviewer'; affinity=diverse;;
  *) default_role='deep orchestrator'; affinity=neutral;;
esac
[ -n "$role" ] || role=$default_role
usage_script="$self_dir/usage-check.sh"
if [ -n "$jobs" ]; then usage=$($usage_script --harness all --jobs "$jobs"); else usage=$($usage_script --harness all); fi
state() { printf '%s\n' "$usage" | awk -v h="$1" '$1==h {print $2}'; }
bias=$(printf '%s\n' "$usage" | awk '$1=="bias" {print $2; exit}')
# usage-check reports auto when neither known family has a capacity preference.
# Treat that neutral state as the stable compatibility tie-break, not an
# adapter name.
[ "$bias" != auto ] || bias=claude
family_of() { case "$1" in codex) printf gpt;; claude) printf claude;; *) printf unknown;; esac; }
eligible() { s=$(state "$1"); [ "$s" != limited ] && [ "${s#limited(}" = "$s" ]; }
allocation_jobs=$jobs
if [ -z "$allocation_jobs" ]; then
  allocation_home=${AGENT_HOME:-$("$self_dir/agent-home.sh")}
  allocation_jobs="$allocation_home/.dispatch/jobs.log"
fi
allocation_rank() {
  python3 "$self_dir/dispatch_allocation.py" rank --jobs "$allocation_jobs" \
    --window "$allocation_window" --candidates "$1" --declared-order "$allocation_order"
}

# Explicit --adapter opencode always routes here. A config affinity of
# "opencode" (SD-66) is honored the same way, but only when the caller left
# --adapter/--family unset: explicit choice still outranks config.
effective_adapter=$adapter
if [ -z "$effective_adapter" ] && [ -z "$family" ] && [ "$config_affinity" = opencode ]; then
  effective_adapter=opencode
fi
if [ "$family" = unknown ]; then
  echo status=unknown; echo adapter=opencode; echo family=unknown; echo "role=$role"; echo exact_model_id=unknown; echo reasoning=unknown
  echo "trace.1=explicit=${adapter:-none};config_affinity=${config_affinity};eligibility=opencode-runtime-inventory-unknown"
  echo unknown=opencode-runtime-model-inventory-unavailable
  exit 0
fi

choose=$effective_adapter
[ -n "$choose" ] || case "$family" in gpt) choose=codex;; claude) choose=claude;; esac
# SD-22 cascade: explicit adapter/family already resolved above; next comes
# the validated config affinity, then the existing stage-heuristic/bias
# fallback. Schema-v2 `diverse` ranks all normal checked harnesses from the
# rolling exact-attempt counters sealed by the defaults policy.
if [ -z "$choose" ]; then
  case "$config_affinity" in
    claude|codex) choose=$config_affinity;;
    diverse)
      if [ "$allocation_strategy" = least-recent-attempts ]; then
        pool=
        old_ifs=$IFS; IFS=,
        for candidate in $allocation_order; do
          if eligible "$candidate"; then pool="${pool}${pool:+,}${candidate}"; fi
        done
        IFS=$old_ifs
        if [ -n "$pool" ]; then
          ranked=$(allocation_rank "$pool")
          choose=$(printf '%s\n' "$ranked" | awk -F= '$1=="rank" {split($2,a,","); print a[1]}')
        fi
      else
        case "$maker_family" in
          claude) choose=codex;;
          gpt) choose=claude;;
          *) choose=${bias:-claude};;
        esac
      fi
      ;;
  esac
fi
if [ -z "$choose" ]; then
  case "$affinity:$maker_family" in
    codex:*) choose=codex;;
    diverse:claude) choose=codex;;
    diverse:gpt) choose=claude;;
    diverse:*) choose=${bias:-claude};;
    neutral:*) choose=${bias:-claude};;
  esac
fi
case "$choose" in claude|codex|opencode) ;; *) echo 'dispatch-route: no known candidate' >&2; exit 64;; esac

rejected=; fallback=
if ! eligible "$choose"; then
  s=$(state "$choose"); rejected="$choose:usage-$s"
  if [ "$allocation_strategy" = least-recent-attempts ]; then
    fallback_pool=
    old_ifs=$IFS; IFS=,
    for candidate in $allocation_order; do
      if [ "$candidate" != "$choose" ] && eligible "$candidate"; then
        fallback_pool="${fallback_pool}${fallback_pool:+,}${candidate}"
      fi
    done
    IFS=$old_ifs
    other=
    if [ -n "$fallback_pool" ]; then
      fallback_ranked=$(allocation_rank "$fallback_pool")
      other=$(printf '%s\n' "$fallback_ranked" | awk -F= '$1=="rank" {split($2,a,","); print a[1]}')
    fi
  else
    other=claude; [ "$choose" = claude ] && other=codex
    eligible "$other" || other=
  fi
  if [ -n "$other" ]; then
    fallback="$other:known-limit-on-$choose"; choose=$other
  else
    echo status=unavailable; echo "role=$role"; echo "rejected.1=$rejected"
    reject_n=2
    old_ifs=$IFS; IFS=,
    for candidate in $allocation_order; do
      [ "$candidate" = "${rejected%%:*}" ] && continue
      echo "rejected.$reject_n=$candidate:usage-$(state "$candidate")"
      reject_n=$((reject_n + 1))
    done
    IFS=$old_ifs
    echo 'fallback.1=none:no-eligible-known-candidate'; exit 1
  fi
fi
case "$choose" in
  codex) map="$repo_root/adapters/codex/bin/model-map.sh";;
  claude) map="$repo_root/adapters/claude/bin/model-map.sh";;
  opencode) map="$repo_root/adapters/opencode/bin/model-map.sh";;
esac
mapped=$($map "$role")
get() { printf '%s\n' "$mapped" | awk -F= -v k="$1" '$1==k {sub(/^[^=]*=/, ""); print; exit}'; }
mapped_status=$(get status); [ -n "$mapped_status" ] || mapped_status=eligible
mapped_family=$(get family); [ -n "$mapped_family" ] || mapped_family=$(family_of "$choose")
echo "status=$mapped_status"; echo "adapter=$choose"; echo "family=$mapped_family"; echo "role=$role"; echo "exact_model_id=$(get exact_model_id)"; echo "reasoning=$(get reasoning)"
[ -z "$rejected" ] || echo "rejected.1=$rejected"
[ -z "$fallback" ] || echo "fallback.1=$fallback"
echo "trace.1=explicit=${adapter:-none};family=${family:-none};eligibility=usage-$(state "$choose")"
echo "trace.2=affinity=$affinity;maker_family=${maker_family:-unknown};required=${required:-none};bias=${bias:-unknown}"
if [ "$allocation_strategy" = least-recent-attempts ]; then
  allocation_rank "$allocation_order" | sed 's/^rank=/allocation_rank=/'
fi
