#!/usr/bin/env sh
# Read-only harness status snapshot for runtime adapters.
set -eu

usage() {
  cat <<'EOF'
usage: harness-status.sh [cwd] [session-id]

Prints machine-readable harness-specific status signals. This is not a
replacement for runtime-native status UI; adapters use it to expose the
workflow, artifact, notes, and git-risk signals that native footers usually do
not cover.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

cwd=${1:-$PWD}
sid=${2:-agent-status}
adapter=${AGENT_ADAPTER:-portable}
self_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Lexical native cwd only. All Git/path discovery belongs to the finite
# shared refresh, never to the status/prompt renderer.
printf 'adapter=%s\n' "$adapter"
printf 'runtime_surface=adapter-owned-harness-status\n'
printf 'status=ok\n'
printf 'cwd=%s\n' "$cwd"

git_repo=unknown; git_root=unknown
git_dirty_tracked=unknown; git_untracked=unknown; git_extra_worktrees=unknown
git_artifact_root=unknown; git_artifact_root_kind=unknown
git_artifact_root_exists=unknown; git_artifact_root_scope=unknown
observation=$(python3 "$self_dir/../tools/fleet/gitinfo.py" --display shell "$cwd" 2>/dev/null) || observation=""
# Values are shell-quoted by the shared reader, including untrusted path names.
eval "$observation"
for key in repo root gitdir common branch detached operation dirty dirty_tracked untracked dirty_total worktree_count extra_worktrees upstream ahead behind default_branch branch_done merged observed_at completed_at observation; do
  eval 'value=${git_'"$key"':-unknown}'
  printf 'git_%s=%s\n' "$key" "$value"
done
printf 'artifact_root=%s\n' "$git_artifact_root"
printf 'artifact_root_kind=%s\n' "$git_artifact_root_kind"
printf 'artifact_root_exists=%s\n' "$git_artifact_root_exists"
printf 'artifact_root_scope=%s\n' "$git_artifact_root_scope"
printf 'artifact_write_target=canonical-only\n'

sibling_worktree=0
sibling_slug=""
case "$cwd" in
  *-wt/*)
    sibling_worktree=1
    sibling_slug=${cwd#*-wt/}
    sibling_slug=${sibling_slug%%/*}
    ;;
esac
printf 'sibling_worktree=%s\n' "$sibling_worktree"
if [ -n "$sibling_slug" ]; then
  printf 'sibling_worktree_slug=%s\n' "$sibling_slug"
fi

notes_root=${AGENT_NOTES_ROOT:-${WORKLOG_NOTES_ROOT:-}}
board_app=${CAIRN_APP:-${WORKLOG_BOARD_APP:-}}
board_wt=${CAIRN_WT:-${WORKLOG_BOARD_WT:-}}
if [ -n "$notes_root" ]; then
  printf 'agent_notes_root=%s\n' "$notes_root"
  [ -d "$notes_root" ] && printf 'agent_notes_root_exists=1\n' || printf 'agent_notes_root_exists=0\n'
else
  printf 'agent_notes_root=unset\n'
  printf 'agent_notes_root_exists=0\n'
fi
if [ -n "$board_app" ]; then
  printf 'worklog_board_app=%s\n' "$board_app"
  [ -e "$board_app" ] && printf 'worklog_board_app_exists=1\n' || printf 'worklog_board_app_exists=0\n'
else
  printf 'worklog_board_app=unset\n'
  printf 'worklog_board_app_exists=0\n'
fi
if [ -n "$board_wt" ]; then
  printf 'worklog_board_wt=%s\n' "$board_wt"
  [ -e "$board_wt" ] && printf 'worklog_board_wt_exists=1\n' || printf 'worklog_board_wt_exists=0\n'
else
  printf 'worklog_board_wt=unset\n'
  printf 'worklog_board_wt_exists=0\n'
fi

# Open headless dispatch jobs (canonical dispatch registry; tab fields:
# ts, state, repo, worktree, slug, pipe). Surfaces in-flight background work that
# native status footers do not cover. Override the registry path with
# AGENT_DISPATCH_JOBS.
status_home=${AGENT_HOME:-}
if [ -z "$status_home" ] || [ ! -f "$status_home/core/CORE.md" ]; then
  status_home=$(CDPATH= cd -- "$self_dir/.." && pwd)
fi
state_root=$(AGENT_HOME="$status_home" "$self_dir/dispatch-state-root.sh" 2>/dev/null) || state_root=""
jobs_log=${AGENT_DISPATCH_JOBS:-${state_root:+$state_root/jobs.log}}
headless_open=0
headless_slugs=""
if [ -n "$jobs_log" ] && [ -f "$jobs_log" ]; then
  headless_open=$(awk -F '\t' 'NF==6 && $2=="open"{c++} END{print c+0}' "$jobs_log")
  headless_slugs=$(awk -F '\t' 'NF==6 && $2=="open"{printf "%s%s", sep, $5; sep=","}' "$jobs_log")
fi
printf 'headless_open_jobs=%s\n' "$headless_open"
if [ -n "$headless_slugs" ]; then
  printf 'headless_open_slugs=%s\n' "$headless_slugs"
fi
# Counts above name registry rows. This view observes process and result
# evidence without changing any row or running work.
if [ -n "$jobs_log" ] && [ -f "$jobs_log" ]; then
  python3 "$self_dir/dispatch-registry.py" observed-status --jobs "$jobs_log" --agent-home "$status_home" 2>/dev/null \
    || printf 'headless_evidence=unknown\n'
else
  printf 'headless_evidence=none\n'
fi

# SD-64/71: orphaned-conductor visibility (dead owner, incomplete route, open
# child or ready un-started successor). Fail-open to 0 with no registry.
orphaned_conductor_jobs=0
orphaned_resume_boundary=""
if [ -n "$jobs_log" ] && [ -f "$jobs_log" ]; then
  scan=$(python3 "$self_dir/dispatch-registry.py" orphan-scan --jobs "$jobs_log" --agent-home "$status_home" 2>/dev/null || true)
  orphaned_conductor_jobs=$(printf '%s\n' "$scan" | awk -F= '$1=="orphaned_conductor_jobs"{print $2; exit}')
  orphaned_resume_boundary=$(printf '%s\n' "$scan" | awk -F= '$1=="orphaned_resume_boundary"{print $2; exit}')
fi
printf 'orphaned_conductor_jobs=%s\n' "${orphaned_conductor_jobs:-0}"
if [ -n "${orphaned_resume_boundary:-}" ]; then
  printf 'orphaned_resume_boundary=%s\n' "$orphaned_resume_boundary"
fi

# F-59: the same exact-identity classifier used by Fleet. A malformed index or
# registry is diagnostic data, never a reason for the status snapshot to fail.
resource_counts=$(python3 "$self_dir/resource_run_registry.py" counts --format shell 2>/dev/null || true)
resource_live=$(printf '%s\n' "$resource_counts" | awk -F= '$1=="working"{print $2; exit}')
resource_stale=$(printf '%s\n' "$resource_counts" | awk -F= '$1=="stale"{print $2; exit}')
resource_exited=$(printf '%s\n' "$resource_counts" | awk -F= '$1=="exited"{print $2; exit}')
resource_malformed=$(printf '%s\n' "$resource_counts" | awk -F= '$1=="malformed"{print $2; exit}')
printf 'resource_run_live=%s\n' "${resource_live:-0}"
printf 'resource_run_stale=%s\n' "${resource_stale:-0}"
printf 'resource_run_exited=%s\n' "${resource_exited:-0}"
printf 'resource_run_malformed=%s\n' "${resource_malformed:-0}"

printf 'note=read-only snapshot; runtime-native status UI remains authoritative for model/context/token/session fields\n'
