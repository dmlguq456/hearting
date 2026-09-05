#!/bin/sh
# PreToolUse(Skill): when a spec-changing Skill runs in a spec-backed cwd,
# deny it unless this session has actually read prd.md (marker present).
# Portable CLI: spec-skill-gate.sh --skill <name> [--cwd <dir>] [--session <id>] [--agent-home <dir>]
# Also deny when prd.md changed after that read, forcing a fresh read.
# This is a verifiable hard gate rather than self-reporting. POSIX sh, no jq.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/../utilities/agent-home.sh")}"
ARTIFACT_ROOT_RESOLVER="$SCRIPT_DIR/../utilities/artifact-root.sh"

usage() {
  cat <<'EOF'
usage: spec-skill-gate.sh --skill <name> [--cwd <dir>] [--session <id>] [--route <file>] [--agent-home <dir>]
       spec-skill-gate.sh --capability <name> [--cwd <dir>] [--session <id>] [--route <file>] [--agent-home <dir>]

Without arguments, reads Claude hook JSON from stdin.
EOF
}

# Governing prd.md candidates for one artifact root -- ONE definition:
# `utilities/artifact_cutover.py prd_candidates` (latest shared/spec revision
# first, the legacy `spec/` bucket only while no shared revision exists, the
# order `artifact_reader.spec_dir` uses). Without python the fallback is the
# legacy scan, and spec-read-marker.sh carries the identical function and
# fallback, so the gate and the marker can never disagree about which file
# governs (defect K: they did, and operators wrote where the gate looked).
prd_candidates_for() {
  ar=$1
  out=""
  cutover="$AGENT_HOME/utilities/artifact_cutover.py"
  [ -f "$cutover" ] || cutover="$SCRIPT_DIR/../utilities/artifact_cutover.py"
  if [ -f "$cutover" ] && command -v python3 >/dev/null 2>&1; then
    out=$(python3 "$cutover" resolve-legacy --artifact-root "$ar" --prd-candidates 2>/dev/null) || out=""
  fi
  if [ -z "$out" ]; then
    [ -f "$ar/spec/prd.md" ] && out="$ar/spec/prd.md"
    for d in "$ar"/spec/*/; do
      [ -d "$d" ] || continue
      d="${d%/}"
      [ "$(basename "$d")" = "_internal" ] && continue
      [ -f "$d/prd.md" ] || continue
      if [ -z "$out" ]; then
        out="$d/prd.md"
      else
        out="$out
$d/prd.md"
      fi
    done
  fi
  printf '%s' "$out"
}

find_prd() {
  dir=$1
  candidates=""
  root=""
  while [ ! -d "$dir" ]; do
    parent=$(dirname "$dir")
    [ "$parent" = "$dir" ] && return 0
    dir=$parent
  done
  dir=$(CDPATH= cd -- "$dir" && pwd -P)
  artifact_root=$("$ARTIFACT_ROOT_RESOLVER" "$dir" 2>/dev/null) || return 0

  candidates=$(prd_candidates_for "$artifact_root")

  [ -n "$candidates" ] && root=$(dirname "$artifact_root")
}

# Marker key for one prd.md candidate: `spec/prd.md` and
# `shared/spec/<ref>/revisions/<rrev>/prd.md` share the root key; a component
# prd (`spec/<slug>/prd.md` or `.../<rrev>/<slug>/prd.md`) keys by its slug.
prd_slug() {
  parent=$(dirname "$1")
  parent_base=$(basename "$parent")
  case "$parent_base" in
    spec|rrev_*) printf '' ;;
    *) printf '%s' "$parent_base" ;;
  esac
}

check_gate() {
  skill=$1
  cwd=$2
  sid=$3
  route_file=${4:-}

  if [ -d "$cwd" ]; then
    cwd=$(CDPATH= cd -- "$cwd" && pwd -P)
  fi

  case "$skill" in
    autopilot-code|autopilot-spec) ;;
    *) return 0 ;;   # Capability is not spec-governed.
  esac

  find_prd "$cwd"
  [ -z "$candidates" ] && return 0   # Not a spec-backed project.

  key=$(printf '%s' "$root" | sed 's#[/ ]#_#g')

  # SD-45: the route record is a fail-open-safe ADDITIONAL pass path. It can
  # only ever add a pass; it never adds a new deny reason and never turns a
  # marker pass into a deny. Any resolution failure — no route file, no
  # python3, non-zero probe exit — falls straight through to the existing
  # marker loop below, so the worst case is exactly today's marker-only
  # behaviour (plan.md §9, round_1 finding 2 trust-boundary note).
  route_resolved="${route_file:-${AGENT_ROUTE_FILE:-}}"
  if [ -n "$route_resolved" ] && command -v python3 >/dev/null 2>&1; then
    IFS_OLD_ROUTE=$IFS
    IFS='
'
    for candidate in $candidates; do
      IFS=$IFS_OLD_ROUTE
      if python3 "$SCRIPT_DIR/../utilities/spec_gate_evidence.py" \
          --route "$route_resolved" --prd "$candidate" \
          --project-root "$root" --artifact-root "$artifact_root" \
          --route-id "${AGENT_ROUTE_ID:-}" >/dev/null 2>&1; then
        IFS=$IFS_OLD_ROUTE
        return 0
      fi
      IFS='
'
    done
    IFS=$IFS_OLD_ROUTE
  fi

  any_marker=0
  unsatisfied=""
  total=0
  IFS_OLD=$IFS
  IFS='
'
  for candidate in $candidates; do
    IFS=$IFS_OLD
    total=$((total + 1))
    parent_base=$(prd_slug "$candidate")
    if [ -z "$parent_base" ]; then
      marker_name="${sid}__${key}"
    else
      slug_key=$(printf '%s' "$parent_base" | sed 's#[/ ]#_#g')
      marker_name="${sid}__${key}__${slug_key}"
    fi
    marker="$AGENT_HOME/.spec-grounding/${marker_name}"
    cur=$(stat -c %Y "$candidate" 2>/dev/null || echo 0)
    if [ -f "$marker" ]; then
      any_marker=1
      read_mtime=$(cat "$marker" 2>/dev/null || echo 0)
      if [ "$cur" -le "$read_mtime" ]; then
        IFS=$IFS_OLD
        return 0   # This candidate is satisfied — ANY satisfied candidate passes the gate.
      fi
    fi
    if [ -z "$unsatisfied" ]; then
      unsatisfied="$candidate"
    else
      unsatisfied="$unsatisfied
$candidate"
    fi
    IFS='
'
  done
  IFS=$IFS_OLD

  if [ "$total" -eq 1 ]; then
    prd="$candidates"
    if [ "$any_marker" -eq 1 ]; then
      reason="prd.md changed after the most recent Read marker. Read $prd again, then retry."
    else
      reason="This cwd is spec-backed, but prd.md was not read in this session. Read $prd directly with the Read tool, then retry. A code comment or brief quotation does not satisfy the gate."
    fi
    return 2
  fi

  list=$(printf '%s' "$unsatisfied" | tr '\n' ',' | sed 's/,/, /g')
  if [ "$any_marker" -eq 1 ]; then
    reason="One or more governing spec candidates changed after the most recent Read marker: $list. Read the one governing the declared work scope again, then retry. A code comment or brief quotation does not satisfy the gate."
  else
    reason="This cwd is spec-backed, but no governing spec candidate was read in this session: $list. Read the one governing the declared work scope directly with the Read tool, then retry. A code comment or brief quotation does not satisfy the gate."
  fi
  return 2
}

deny_json() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$1"
  exit 0
}

if [ "$#" -gt 0 ]; then
  skill=""
  cwd=$PWD
  sid="nosession"
  route_arg=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --skill|--capability)
        [ "$#" -ge 2 ] || { echo "spec-skill-gate: $1 requires a name" >&2; exit 64; }
        skill=$2
        shift 2
        ;;
      --cwd)
        [ "$#" -ge 2 ] || { echo "spec-skill-gate: --cwd requires a dir" >&2; exit 64; }
        cwd=$2
        shift 2
        ;;
      --session)
        [ "$#" -ge 2 ] || { echo "spec-skill-gate: --session requires an id" >&2; exit 64; }
        sid=$2
        shift 2
        ;;
      --route)
        [ "$#" -ge 2 ] || { echo "spec-skill-gate: --route requires a file" >&2; exit 64; }
        route_arg=$2
        shift 2
        ;;
      --agent-home)
        [ "$#" -ge 2 ] || { echo "spec-skill-gate: --agent-home requires a dir" >&2; exit 64; }
        AGENT_HOME=$2
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "spec-skill-gate: unknown argument: $1" >&2
        usage >&2
        exit 64
        ;;
    esac
  done
  [ -n "$skill" ] || { echo "spec-skill-gate: --skill is required" >&2; exit 64; }
  reason=""
  check_gate "$skill" "$cwd" "$sid" "$route_arg"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    exit 0
  fi
  [ "$rc" -eq 2 ] && printf '%s\n' "$reason" >&2
  exit "$rc"
fi

input=$(cat 2>/dev/null)
[ -z "$input" ] && exit 0

skill=$(printf '%s' "$input" | grep -o '"skill"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"skill"[[:space:]]*:[[:space:]]*"//; s/"$//')
sid=$(printf '%s' "$input" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"session_id"[[:space:]]*:[[:space:]]*"//; s/"$//')
[ -z "$sid" ] && sid="nosession"

reason=""
check_gate "$skill" "$PWD" "$sid" ""
rc=$?
if [ "$rc" -eq 0 ]; then
  exit 0
fi
[ "$rc" -eq 2 ] && deny_json "$reason"
exit 0
