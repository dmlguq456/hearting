#!/usr/bin/env sh
# Read-only harness status snapshot for runtime adapters.
set -eu

usage() {
  cat <<'EOF'
usage: harness-status.sh [--signal] [cwd] [session-id]

Prints machine-readable harness-specific status signals. This is not a
replacement for runtime-native status UI; adapters use it to expose the
workflow, artifact, notes, and git-risk signals that native footers usually do
not cover.

--signal prints only what the per-prompt signal reads (git, artifact root and
the open-row count). Process evidence, the orphaned-conductor scan and
resource-run counts are computed when the full status is asked for.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

facts=all
if [ "${1:-}" = "--signal" ]; then
  facts=signal
  shift
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
# One read of the registry for every fact below: the open rows (exactly six
# fields, status open), the observed process and result evidence, and the
# SD-64/71 orphaned-conductor scan (dead owner, incomplete route, open child or
# ready un-started successor), and the F-59 resource-run counts below.
# Read-only; it changes no row and runs no work.
resource_counts=""
if [ "$facts" = signal ]; then
  # The per-prompt signal reads only the open-row count: one pass, no Python.
  if [ -n "$jobs_log" ] && [ -f "$jobs_log" ]; then
    awk -F '\t' 'NF==6 && $2=="open"{c++; slugs=slugs sep $5; sep=","}
      END{print "headless_open_jobs=" c+0; if (slugs != "") print "headless_open_slugs=" slugs}' "$jobs_log"
  else
    printf 'headless_open_jobs=0\n'
  fi
  exit 0
fi
if [ -n "$jobs_log" ] && [ -f "$jobs_log" ]; then
  if registry_facts=$(python3 "$self_dir/dispatch-registry.py" status-snapshot --jobs "$jobs_log" --agent-home "$status_home" 2>/dev/null); then
    printf '%s\n' "$registry_facts" | grep -v '^resource_count_' || true
    resource_counts=$(printf '%s\n' "$registry_facts" | sed -n 's/^resource_count_//p')
  else
    # The status stays usable without the shared reader: the count from the
    # file itself, unknown evidence and no orphan count.
    awk -F '\t' 'NF==6 && $2=="open"{c++; slugs=slugs sep $5; sep=","}
      END{print "headless_open_jobs=" c+0; if (slugs != "") print "headless_open_slugs=" slugs}' "$jobs_log"
    printf 'headless_evidence=unknown\n'
    printf 'orphaned_conductor_jobs=0\n'
  fi
else
  printf 'headless_open_jobs=0\n'
  printf 'headless_evidence=none\n'
  printf 'orphaned_conductor_jobs=0\n'
fi

# F-59: the same exact-identity classifier used by Fleet. A malformed index or
# registry is diagnostic data, never a reason for the status snapshot to fail.
if [ -z "$resource_counts" ]; then
  resource_counts=$(python3 "$self_dir/resource_run_registry.py" counts --format shell 2>/dev/null || true)
fi
resource_live=$(printf '%s\n' "$resource_counts" | awk -F= '$1=="working"{print $2; exit}')
resource_stale=$(printf '%s\n' "$resource_counts" | awk -F= '$1=="stale"{print $2; exit}')
resource_exited=$(printf '%s\n' "$resource_counts" | awk -F= '$1=="exited"{print $2; exit}')
resource_malformed=$(printf '%s\n' "$resource_counts" | awk -F= '$1=="malformed"{print $2; exit}')
printf 'resource_run_live=%s\n' "${resource_live:-0}"
printf 'resource_run_stale=%s\n' "${resource_stale:-0}"
printf 'resource_run_exited=%s\n' "${resource_exited:-0}"
printf 'resource_run_malformed=%s\n' "${resource_malformed:-0}"

printf 'note=read-only snapshot; runtime-native status UI remains authoritative for model/context/token/session fields\n'
