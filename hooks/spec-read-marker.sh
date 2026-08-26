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

# True when the legacy bucket still carries a governing prd.md (root or a
# one-level component); `_internal/` never counts.
legacy_has_prd() {
  legacy=$1
  [ -d "$legacy" ] || return 1
  [ -f "$legacy/prd.md" ] && return 0
  for d in "$legacy"/*/; do
    [ -d "$d" ] || continue
    d="${d%/}"
    [ "$(basename "$d")" = "_internal" ] && continue
    [ -f "$d/prd.md" ] && return 0
  done
  return 1
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
  if [ -n "$shared_prd" ]; then
    # A shared/spec read counts only while the legacy `spec/` bucket holds no
    # prd.md candidate — the same precedence spec-skill-gate.sh applies. The
    # bucket directory itself may survive retirement (W7D: it keeps excluded
    # `_internal/` evidence), so its mere existence is not the test.
    legacy_has_prd "$canonical/spec" && return 0
    canonical_prd="$fp"
  elif [ -z "$slug" ]; then
    canonical_prd="$canonical/spec/prd.md"
  else
    canonical_prd="$canonical/spec/$slug/prd.md"
  fi
  canonical_parent=$(dirname "$canonical_prd")
  canonical_parent=$(CDPATH= cd -- "$canonical_parent" 2>/dev/null && pwd -P) || return 0
  canonical_prd="$canonical_parent/$(basename "$canonical_prd")"
  file_parent=$(dirname "$fp")
  file_parent=$(CDPATH= cd -- "$file_parent" 2>/dev/null && pwd -P) || return 0
  fp="$file_parent/$(basename "$fp")"
  [ "$fp" = "$canonical_prd" ] || return 0

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
