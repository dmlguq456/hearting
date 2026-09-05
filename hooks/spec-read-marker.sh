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
# order `artifact_reader.spec_dir` uses). The checkout's own copy wins (the
# adapter hook is a symlink into it); the plugin projection, which carries no
# utilities, resolves the installed harness through `agent-home.sh` instead of
# `$AGENT_HOME` (there it is the plugin *state* dir, never a harness root).
# Without python, or on a root with no `shared/spec` at all, the fallback is
# the legacy scan -- and spec-read-marker.sh carries the identical function and
# fallback, so the gate and the marker can never disagree about which file
# governs (defect K: they did, and operators wrote where the gate looked).
prd_candidates_for() {
  ar=$1
  out=""
  if [ -d "$ar/shared/spec" ] && command -v python3 >/dev/null 2>&1; then
    cutover="$SCRIPT_DIR/../utilities/artifact_cutover.py"
    if [ ! -f "$cutover" ]; then
      harness=$("$SCRIPT_DIR/../utilities/agent-home.sh" 2>/dev/null) || harness=""
      cutover="$harness/utilities/artifact_cutover.py"
    fi
    if [ -f "$cutover" ]; then
      out=$(python3 "$cutover" resolve-legacy --artifact-root "$ar" --prd-candidates 2>/dev/null) || out=""
    fi
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

mark_read() {
  fp=$1
  sid=$2

  case "$fp" in
    /*) ;;
    *) fp="$PWD/$fp" ;;
  esac

  [ "$(basename "$fp")" = prd.md ] || return 0
  # Locate the artifact-reports dir the file lives under; the file's exact
  # shape is decided by candidate membership below, not by a second parser.
  case "$fp" in
    */.agent_reports/*) file_root=${fp%%/.agent_reports/*} ;;
    */.claude_reports/*) file_root=${fp%%/.claude_reports/*} ;;
    *) return 0 ;;
  esac
  [ -f "$fp" ] || return 0

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
  slug=$(prd_slug "$fp")

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
