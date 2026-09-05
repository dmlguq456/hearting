#!/bin/sh
# PostToolUse(Read): write a session marker after prd.md is actually read.
# Portable CLI: spec-read-marker.sh --file <prd.md> [--session <id>] [--agent-home <dir>]
# spec-skill-gate.sh uses the marker as evidence of a real read, not a quotation.
# The marker stores prd.md mtime at read time for later drift comparison. POSIX sh, no jq.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/../utilities/agent-home.sh")}"
ARTIFACT_ROOT_RESOLVER="$SCRIPT_DIR/../utilities/artifact-root.sh"

usage() {
  cat <<'EOF'
usage: spec-read-marker.sh --file <prd.md> [--session <id>] [--agent-home <dir>]

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

mark_read() {
  fp=$1
  sid=$2

  case "$fp" in
    /*) ;;
    *) fp="$PWD/$fp" ;;
  esac

  slug=""
  shared_prd=""
  case "$fp" in
    */.agent_reports/spec/prd.md) ;;
    */.claude_reports/spec/prd.md) ;;
    */.agent_reports/shared/spec/*/revisions/*/prd.md|*/.claude_reports/shared/spec/*/revisions/*/prd.md)
      # W7C: an immutable shared/spec revision read counts as the canonical
      # prd read once the legacy bucket is retired. `<rrev>/prd.md` is the
      # root spec; `<rrev>/<slug>/prd.md` is a one-level component spec.
      [ "$(basename "$fp")" = prd.md ] || return 0
      d1=$(dirname "$fp")
      case "$(basename "$d1")" in
        rrev_*) shared_prd="root" ;;
        _internal) return 0 ;;
        *)
          case "$(basename "$(dirname "$d1")")" in
            rrev_*) slug=$(basename "$d1"); shared_prd="component" ;;
            *) return 0 ;;
          esac ;;
      esac
      ;;
    *)
      # Structural depth check for a one-level sub-spec: .../<agent-reports-dir>/spec/<slug>/prd.md.
      # A `case` glob's `*` matches `/`, so a literal pattern here would also swallow
      # spec/<slug>/_internal/versions/v1/prd.md; walk dirname() instead.
      [ "$(basename "$fp")" = prd.md ] || return 0
      d1=$(dirname "$fp")
      d2=$(dirname "$d1")
      d3=$(dirname "$d2")
      [ "$(basename "$d2")" = spec ] || return 0
      case "$(basename "$d3")" in
        .agent_reports|.claude_reports) ;;
        *) return 0 ;;
      esac
      slug=$(basename "$d1")
      [ "$slug" = "_internal" ] && return 0
      ;;
  esac
  [ -f "$fp" ] || return 0

  if [ -n "$shared_prd" ]; then
    reports_dir=${fp%/shared/spec/*}
    file_root=$(dirname "$reports_dir")
  elif [ -z "$slug" ]; then
    file_root=$(dirname "$(dirname "$(dirname "$fp")")")
  else
    file_root=$(dirname "$d3")
  fi
  canonical=$("$ARTIFACT_ROOT_RESOLVER" "$file_root" 2>/dev/null) || return 0
  file_parent=$(dirname "$fp")
  file_parent=$(CDPATH= cd -- "$file_parent" 2>/dev/null && pwd -P) || return 0
  fp="$file_parent/$(basename "$fp")"
  # The read counts only when the file IS one of the governing candidates the
  # gate will name -- the same list, from the same function.
  match=""
  candidates=$(prd_candidates_for "$canonical")
  oldifs=$IFS
  IFS='
'
  for c in $candidates; do
    c_parent=$(dirname "$c")
    c_parent=$(CDPATH= cd -- "$c_parent" 2>/dev/null && pwd -P) || continue
    [ "$c_parent/$(basename "$c")" = "$fp" ] && match=1
  done
  IFS=$oldifs
  [ -n "$match" ] || return 0

  root=$(dirname "$canonical")
  key=$(printf '%s' "$root" | sed 's#[/ ]#_#g')
  mtime=$(stat -c %Y "$fp" 2>/dev/null || echo 0)

  if [ -z "$slug" ]; then
    marker_name="${sid}__${key}"
  else
    slug_key=$(printf '%s' "$slug" | sed 's#[/ ]#_#g')
    marker_name="${sid}__${key}__${slug_key}"
  fi

  mkdir -p "$AGENT_HOME/.spec-grounding"
  printf '%s\n' "$mtime" > "$AGENT_HOME/.spec-grounding/${marker_name}"
}

if [ "$#" -gt 0 ]; then
  fp=""
  sid="nosession"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --file)
        [ "$#" -ge 2 ] || { echo "spec-read-marker: --file requires a path" >&2; exit 64; }
        fp=$2
        shift 2
        ;;
      --session)
        [ "$#" -ge 2 ] || { echo "spec-read-marker: --session requires an id" >&2; exit 64; }
        sid=$2
        shift 2
        ;;
      --agent-home)
        [ "$#" -ge 2 ] || { echo "spec-read-marker: --agent-home requires a dir" >&2; exit 64; }
        AGENT_HOME=$2
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "spec-read-marker: unknown argument: $1" >&2
        usage >&2
        exit 64
        ;;
    esac
  done
  [ -n "$fp" ] || { echo "spec-read-marker: --file is required" >&2; exit 64; }
  mark_read "$fp" "$sid"
  exit 0
fi

input=$(cat 2>/dev/null)
[ -z "$input" ] && exit 0

fp=$(printf '%s' "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"file_path"[[:space:]]*:[[:space:]]*"//; s/"$//')
sid=$(printf '%s' "$input" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"session_id"[[:space:]]*:[[:space:]]*"//; s/"$//')
[ -z "$sid" ] && sid="nosession"

mark_read "$fp" "$sid"
exit 0
