#!/usr/bin/env sh
set -eu

cwd=${1:-$PWD}

notes_root=${AGENT_NOTES_ROOT:-${WORKLOG_NOTES_ROOT:-}}
board_app=${CAIRN_APP:-${WORKLOG_BOARD_APP:-}}
board_wt=${CAIRN_WT:-${WORKLOG_BOARD_WT:-}}

count_files() {
  dir=$1
  if [ -d "$dir" ]; then
    find "$dir" -type f 2>/dev/null | wc -l | tr -d ' '
  else
    printf 'missing'
  fi
}

latest_file() {
  dir=$1
  if [ -d "$dir" ]; then
    find "$dir" -type f -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p'
  fi
}

printf 'worklog-state cwd=%s\n' "$cwd"
printf 'agent-notes-root=%s\n' "${notes_root:-unset}"
printf 'worklog-board-app=%s\n' "${board_app:-unset}"
printf 'worklog-board-wt=%s\n' "${board_wt:-unset}"

for sub in cards _layer2/notes _layer2/backbones _layer2/tasks _layer2/papers _triage _feedback _change_review digests oncall study manual; do
  if [ -n "$notes_root" ]; then
    path=$notes_root/$sub
    printf 'notes.%s.files=%s\n' "$sub" "$(count_files "$path")"
  else
    printf 'notes.%s.files=unconfigured\n' "$sub"
  fi
done

if [ -n "$board_app" ]; then
  for path in "$board_app" "$board_app/.cache" "$board_app/.dispatch" "$board_app/.agent_reports" "$board_app/.claude_reports"; do
    if [ -e "$path" ]; then
      printf 'path.exists=%s\n' "$path"
    else
      printf 'path.missing=%s\n' "$path"
    fi
  done
else
  printf 'path.unconfigured=worklog-board-app\n'
fi

if [ -n "$board_wt" ]; then
  if [ -e "$board_wt" ]; then
    printf 'path.exists=%s\n' "$board_wt"
  else
    printf 'path.missing=%s\n' "$board_wt"
  fi
else
  printf 'path.unconfigured=worklog-board-wt\n'
fi

latest_oncall=
latest_digest=
if [ -n "$notes_root" ]; then
  latest_oncall=$(latest_file "$notes_root/oncall" || true)
  latest_digest=$(latest_file "$notes_root/digests" || true)
fi
[ -n "${latest_oncall:-}" ] && printf 'latest.oncall=%s\n' "$latest_oncall"
[ -n "${latest_digest:-}" ] && printf 'latest.digest=%s\n' "$latest_digest"

printf 'note=read-only inventory; do not move, delete, or commit notes/app runtime data into the harness repo\n'
