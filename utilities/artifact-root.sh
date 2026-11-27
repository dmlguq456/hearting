#!/usr/bin/env sh
# Print the one writable artifact root for a project.
#
# Linked task worktrees are source-only execution surfaces. Their tracked
# .agent_reports snapshot is never selected as a write target. An explicit
# absolute AGENT_ARTIFACT_ROOT wins; otherwise Git projects use the primary
# worktree (the first `git worktree list --porcelain` entry). Non-Git projects
# retain upward discovery from the supplied cwd.
#
# An installed immutable source tree is never a writable root, whichever of those
# paths reaches it: it is replaced wholesale on update, so state written there is
# silently lost, and it must stay byte-identical to the release it was built from.
# Every emit therefore goes through `emit`, which refuses such a root with a typed
# failure instead of returning it.
set -eu

physical_dir() {
  candidate=$1
  [ -d "$candidate" ] || return 1
  (CDPATH= cd -- "$candidate" && pwd -P)
}

physical_path() {
  candidate=$1
  if [ -d "$candidate" ]; then
    physical_dir "$candidate"
    return
  fi
  parent=$(dirname "$candidate")
  leaf=$(basename "$candidate")
  parent=$(physical_dir "$parent") || return 1
  printf '%s/%s\n' "$parent" "$leaf"
}

# Immutable installed source layouts, mirroring
# utilities/dispatch_contract.py::_versioned_source_layout: a per-runtime release
# bundle is `<runtime-home>/.harness/bundles/<id>/source`, and a shared managed
# release is `<xdg-data>/hearting/releases/<version>`. The whole bundle subtree is
# refused rather than only `<id>/source`, because nothing under a bundle is
# mutable state.
emit() {
  case "$1/" in
    */.harness/bundles/*|*/hearting/releases/*)
      echo "artifact-root: refusing an immutable installed source tree as a writable root: $1" >&2
      exit 67
      ;;
  esac
  printf '%s\n' "$1"
  exit 0
}

emit_project_root() {
  # With one name (or none) there is nothing to choose, so no interpreter starts.
  # A folder the shell cannot enter is left to the interpreter, whose realpath
  # still names (and so lets `emit` judge) its physical target.
  agent=${1%/}/.agent_reports
  claude=${1%/}/.claude_reports
  if [ ! -d "$claude" ]; then
    [ -d "$agent" ] || emit "$agent"
    root=$(physical_dir "$agent" 2>/dev/null) && emit "$root"
  elif [ ! -d "$agent" ]; then
    root=$(physical_dir "$claude" 2>/dev/null) && emit "$root"
  fi

  resolver=$0
  while [ -L "$resolver" ]; do
    target=$(readlink "$resolver") || exit 66
    case "$target" in
      /*) resolver=$target ;;
      *) resolver=$(dirname -- "$resolver")/$target ;;
    esac
  done
  # An inherited PYTHONSAFEPATH=1 would hide the script's sibling modules.
  root=$(PYTHONSAFEPATH= python3 -B "$(dirname -- "$resolver")/artifact_root_selection.py" "$1") || exit $?
  emit "$root"
}

if [ -n "${AGENT_ARTIFACT_ROOT:-}" ]; then
  case "$AGENT_ARTIFACT_ROOT" in
    /*) ;;
    *)
      echo "artifact-root: AGENT_ARTIFACT_ROOT must be an absolute path" >&2
      exit 64
      ;;
  esac
  override=$(physical_path "$AGENT_ARTIFACT_ROOT") || {
    echo "artifact-root: parent directory does not exist: $AGENT_ARTIFACT_ROOT" >&2
    exit 66
  }
  emit "$override"
fi

start="${1:-$PWD}"
start=$(physical_dir "$start") || {
  echo "artifact-root: directory does not exist: $start" >&2
  exit 66
}

if command -v git >/dev/null 2>&1 \
  && git -C "$start" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # The ordinary common directory names the primary checkout directly.
  # Enumerating every linked checkout can wait on unrelated NAS paths long
  # enough to exhaust session-tidy's path-resolution timeout.
  # git < 2.31 has no --path-format and would echo the option as an extra line,
  # so ask plainly and make a relative answer absolute against "$start".
  common=$(git -C "$start" rev-parse --git-common-dir 2>/dev/null) || common=""
  case "$common" in
    /*) ;;
    "") ;;
    *) common=$start/$common ;;
  esac
  primary=""
  case "$common" in
    */.git)
      candidate=$(dirname "$common")
      # A separated git directory can also be named .git. Preserve its
      # configured worktree rather than assuming the containing directory.
      configured=$(git --git-dir="$common" config --get core.worktree 2>/dev/null) || configured=""
      bare=$(git --git-dir="$common" config --bool --get core.bare 2>/dev/null) || bare=""
      if [ -z "$configured" ] && [ "$bare" = false ] && [ -d "$candidate" ]; then
        primary=$candidate
      fi
      ;;
  esac
  [ -n "$primary" ] || primary=$(git -C "$start" worktree list --porcelain 2>/dev/null \
    | awk '$1=="worktree"{print substr($0,10); exit}')
  [ -n "$primary" ] || primary=$(git -C "$start" rev-parse --show-toplevel)
  primary=$(physical_dir "$primary")
  emit_project_root "$primary"
fi

# D-3: cwd's own root is used on discovery (self is not inheritance, no marker
# needed). A strict ancestor's root is inherited only when that ancestor also
# carries a regular `.agent-workspace` marker file (contents unread, existence
# only). F7: an earlier revision also accepted the ancestor's own `.git`
# directory as an implicit marker, git-installed or not -- PRD D-3 and
# core/CONVENTIONS.md/CORE.md name only `.agent-workspace`, and a malformed or
# inaccessible `.git` would have silently re-opened the exact $HOME/NAS root
# absorption D-3 exists to close. An ancestor with a root but no marker is
# skipped, not adopted -- the walk keeps climbing instead of stopping at the
# first root it finds.
d=$start
self=1
while :; do
  if { [ "$self" = 1 ] || [ -f "$d/.agent-workspace" ]; } \
    && { [ -d "$d/.agent_reports" ] || [ -d "$d/.claude_reports" ]; }; then
    emit_project_root "$d"
  fi
  self=0
  [ "$d" = "/" ] && break
  parent=$(dirname "$d")
  [ "$parent" = "$d" ] && break
  d=$parent
done
emit "$start/.agent_reports"
