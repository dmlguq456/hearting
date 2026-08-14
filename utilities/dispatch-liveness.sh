#!/usr/bin/env bash
# dispatch-liveness — deterministic stealth-death check for headless jobs.
#   A hung or crashed worker may never deliver completion, so this script
#   classifies open jobs instead of relying on the orchestrator to remember to
#   watch them (OPERATIONS §5.10).
#
#   Primary signal: the wrapper-recorded `pid=` exists under /proc and its
#   command line matches the row harness. PID state avoids shared-worktree transcript
#   aliasing, where conductor activity can keep an already-finished child's
#   transcript directory fresh. A dead PID on an open row is EXITED and needs
#   harvesting.
#
#   Fallback signal for PID-less rows is harness-aware: Claude session transcript,
#   Codex wrapper JSONL, or OpenCode heartbeat/JSONL mtime. Path-based pgrep is
#   intentionally not used because common paths produce false-alive matches.
#
#   Run while waiting after dispatch. SUSPECT/DEAD/EXITED requires transcript
#   and dispatch-log diagnosis followed by harvest or redispatch. Exit 3 means
#   at least one stealth-death candidate or unharvested exit.
set -uo pipefail
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/agent-home.sh")}"
JOBS=""
if [ "$#" -gt 0 ] && [ "${1#--}" = "$1" ]; then
  JOBS=$1
  shift
fi
STATE_ROOT=$("$SCRIPT_DIR/dispatch-state-root.sh" "$JOBS") || exit $?
[ -n "$JOBS" ] || JOBS="$STATE_ROOT/jobs.log"
STALE_MIN="${DISPATCH_STALE_MIN:-15}"   # Suspect hang/death after N quiet minutes.
# Runtime root (HLS-6): session transcripts/state live under the runtime, not
# the harness source repository. Claude defaults to CLAUDE_CONFIG_DIR; other
# adapters override DISPATCH_RUNTIME_ROOT. Profile homes are already isolated.
RUNTIME_ROOT="${DISPATCH_RUNTIME_ROOT:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}"
PROJ="$RUNTIME_ROOT/projects"
LOG_DIR="$STATE_ROOT/logs"
# SD-15: if an open job log ends with a limit/auth fatal pattern, use that as
# the DEAD reason. Keep this deliberate duplicate synchronized with
# dispatch-headless.py DEATH_PATTERNS.
LIMIT_RE='operation not permitted|network is unreachable|network access denied|hit your (session|usage) limit|session limit reached|usage limit reached|weekly limit|rate limit|[^0-9]429[^0-9]|invalid api key|authentication_error|not logged in|please run /login|unauthorized|[^0-9]401[^0-9]|credit balance is too low|insufficient (credit|quota|funds)'
CAPACITY_RE='^(error[[:space:]]*[:\-][[:space:]]*)?(selected[[:space:]]+)?model([[:space:]]+[A-Za-z0-9._:/-]+)?[[:space:]]+(is[[:space:]]+)?at[[:space:]]+capacity[.!]?$'
CODEX_TERMINAL_INSPECTOR="${CODEX_TERMINAL_INSPECTOR:-$SCRIPT_DIR/codex_dispatch_terminal.py}"
OBSERVED_LIVENESS="${OBSERVED_LIVENESS:-$SCRIPT_DIR/dispatch-observed-liveness.py}"

# SD-15b: anchor log-pattern death detection to a few short trailing lines.
# Scanning a large tail caused false DEAD verdicts when a successful report only
# discussed limits. Fresh transcript evidence bypasses this scan below.
scan_log_death() {  # $1=slug; print the matching log path and return 0.
  _slug=$1
  [ -n "$_slug" ] || return 1
  for lf in "$LOG_DIR/${_slug}."*.log "$LOG_DIR/${_slug}."*.jsonl; do
    [ -f "$lf" ] || continue
    # Inspect the last three non-empty lines and accept only terse (≤200) matches.
    lines=$(tail -n 40 "$lf" 2>/dev/null | awk 'NF' | tail -n 3)
    hit=$(printf '%s\n' "$lines" | grep -Ei "$LIMIT_RE" | awk 'length($0) <= 200 { print; exit }')
    [ -n "$hit" ] || hit=$(printf '%s\n' "$lines" | grep -Ei "$CAPACITY_RE" | awk 'length($0) <= 200 { print; exit }')
    [ -n "$hit" ] && { printf '%s' "$lf"; return 0; }
  done
  return 1
}

[ -f "$JOBS" ] || { echo "(jobs.log not found: $JOBS)"; exit 0; }
SOURCE_JOBS=$JOBS
CURRENT_JOBS=$(mktemp)
trap 'rm -f "$CURRENT_JOBS"' EXIT
if ! python3 "$SCRIPT_DIR/dispatch-registry.py" liveness --jobs "$SOURCE_JOBS" "$@" > "$CURRENT_JOBS"; then
  echo "dispatch-liveness: current-view filtering failed" >&2
  exit 69
fi
JOBS=$CURRENT_JOBS

now=$(date +%s); alive=0; suspect=0; open_n=0
while IFS=$'\t' read -r ts status repo wt slug pipe || [ -n "${ts:-}" ]; do
  [ "${status:-}" = "open" ] || continue
  open_n=$((open_n + 1))
  # Primary signal: wrapper-recorded PID, independent of transcript aliasing.
  pid=$(printf '%s' "$pipe" | tr ',' '\n' | sed -n 's/^pid=//p' | head -1)
  pid_start=$(printf '%s' "$pipe" | tr ',' '\n' | sed -n 's/^pid_start=//p' | head -1)
  pid_scope=$(printf '%s' "$pipe" | tr ',' '\n' | sed -n 's/^pid_scope=//p' | head -1)
  attempt_id=$(printf '%s' "$pipe" | tr ',' '\n' | sed -n 's/^attempt_id=//p' | head -1)
  route_id=$(printf '%s' "$pipe" | tr ',' '\n' | sed -n 's/^route_id=//p' | head -1)
  route_node=$(printf '%s' "$pipe" | tr ',' '\n' | sed -n 's/^route_node=//p' | head -1)
  harness=$(printf '%s' "$pipe" | tr ',' '\n' | sed -n 's/^harness=//p' | head -1)
  log_file=$(printf '%s' "$pipe" | tr ',' '\n' | sed -n 's/^log_file=//p' | head -1)
  artifact_root=$(printf '%s' "$pipe" | tr ',' '\n' | sed -n 's/^artifact_root=//p' | head -1)
  [ -n "$harness" ] || harness="claude"
  observed_state=""; observed_reason=""; observed_process_reason=""
  if [ -n "$attempt_id" ]; then
    observed_out=$(python3 "$OBSERVED_LIVENESS" --jobs "$SOURCE_JOBS" --attempt-id "$attempt_id" 2>/dev/null || true)
    observed_state=$(printf '%s\n' "$observed_out" | sed -n 's/^state=//p' | head -1)
    observed_reason=$(printf '%s\n' "$observed_out" | sed -n 's/^reason=//p' | head -1)
    observed_process_reason=$(printf '%s\n' "$observed_out" | sed -n 's/^process_reason=//p' | head -1)
  fi
  terminal_state=""; terminal_source=""; terminal_verdict=""; terminal_artifact=""; terminal_blocker=""
  if { [ "$harness" = "codex" ] || [ "$harness" = "claude" ]; } && [ -n "$log_file" ]; then
    wire_out=$(python3 "$CODEX_TERMINAL_INSPECTOR" \
      --worktree "$wt" --artifact-root-metadata "${artifact_root:--}" "$log_file" 2>/dev/null)
    wire_rc=$?
    wire_shape=$(printf '%s\n' "$wire_out" | awk -F '\t' '
      NR == 1 && NF == 6 && $1 == "codex-terminal-v1" { good=1 }
      END { if (NR == 1 && good) print "ok"; else print "bad" }')
    if [ "$wire_shape" = "ok" ]; then
      IFS=$'\t' read -r _wire terminal_state terminal_source terminal_verdict terminal_artifact terminal_blocker <<< "$wire_out"
      wire_key="$wire_rc|$terminal_state|$terminal_source|$terminal_verdict|$terminal_artifact|$terminal_blocker"
      case "$wire_key" in
        0\|valid\|exact-turn-completed\|PASS\|none\|none|\
        0\|valid\|exact-claude-result\|PASS\|none\|none|\
        0\|valid\|exact-turn-completed\|PASS\|readable\|none|\
        0\|valid\|exact-claude-result\|PASS\|readable\|none|\
        0\|valid\|exact-turn-completed\|FAIL\|none\|none|\
        0\|valid\|exact-claude-result\|FAIL\|none\|none|\
        0\|valid\|exact-turn-completed\|FAIL\|none\|worker-reported|\
        0\|valid\|exact-claude-result\|FAIL\|none\|worker-reported|\
        0\|valid\|exact-turn-completed\|FAIL\|readable\|none|\
        0\|valid\|exact-claude-result\|FAIL\|readable\|none|\
        0\|valid\|exact-turn-completed\|FAIL\|readable\|worker-reported|\
        0\|valid\|exact-claude-result\|FAIL\|readable\|worker-reported|\
        0\|valid\|exact-turn-completed\|BLOCKED\|none\|none|\
        0\|valid\|exact-claude-result\|BLOCKED\|none\|none|\
        0\|valid\|exact-turn-completed\|BLOCKED\|none\|worker-reported|\
        0\|valid\|exact-claude-result\|BLOCKED\|none\|worker-reported|\
        0\|valid\|exact-turn-completed\|BLOCKED\|readable\|none|\
        0\|valid\|exact-claude-result\|BLOCKED\|readable\|none|\
        0\|valid\|exact-turn-completed\|BLOCKED\|readable\|worker-reported|\
        0\|valid\|exact-claude-result\|BLOCKED\|readable\|worker-reported|\
        2\|absent\|none\|-\|unchecked\|-|\
        3\|invalid\|exact-turn-completed\|-\|unchecked\|contract-violation|\
        3\|invalid\|exact-claude-result\|-\|unchecked\|contract-violation|\
        3\|invalid\|exact-turn-completed\|-\|missing\|contract-violation|\
        3\|invalid\|exact-claude-result\|-\|missing\|contract-violation|\
        3\|invalid\|exact-turn-completed\|-\|outside-root\|contract-violation|\
        3\|invalid\|exact-claude-result\|-\|outside-root\|contract-violation|\
        4\|error\|runtime-error\|-\|unchecked\|contract-violation|\
        4\|error\|runtime-error\|-\|unsafe-root\|contract-violation) ;;
        *) terminal_state="wire-invalid" ;;
      esac
    else
      terminal_state="wire-invalid"
    fi
  fi

  if [ "$observed_state" = "reconcile-needed" ] && [ -n "$attempt_id" ]; then
    orphan_info=$(python3 "$SCRIPT_DIR/dispatch-registry.py" orphan-status --attempt "$attempt_id" --jobs "$SOURCE_JOBS" --agent-home "$AGENT_HOME" 2>/dev/null || true)
    if printf '%s\n' "$orphan_info" | grep -q '^orphan=1$'; then
      orphan_route=$(printf '%s\n' "$orphan_info" | sed -n 's/^route_id=//p')
      orphan_boundary=$(printf '%s\n' "$orphan_info" | sed -n 's/^resume_boundary=//p')
      echo "⚠️ ORPHANED ${slug:-?}  — pipeline orphaned; route=$orphan_route; resume boundary=$orphan_boundary; dispatch-depth-0 decision  [open: $ts]"
      suspect=$((suspect + 1)); continue
    fi
  fi
  if [ "$observed_state" = "alive" ]; then
    echo "ALIVE      ${slug:-?}  ($observed_process_reason; shared observed-liveness)"
    alive=$((alive + 1)); continue
  fi

  # A dead depth-1 owner with unfinished dependents is still an orphan even if
  # its exact log contains PASS.  Ask the canonical registry classifier before
  # rendering the terminal observation; this is the SD-64/71 post-exit seam.
  if [ "$terminal_state" = "valid" ] && [ "$terminal_verdict" = "PASS" ] \
      && [ -n "$pid" ] && [ -n "$pid_start" ] && [ -n "$attempt_id" ]; then
    orphan_exact_args=(attempt-state --pid "$pid" --pid-start "$pid_start")
    [ -n "$pid_scope" ] && orphan_exact_args+=(--pid-scope "$pid_scope")
    [ -n "$route_id" ] && orphan_exact_args+=(--route "$route_id")
    [ -n "$route_node" ] && orphan_exact_args+=(--node "$route_node")
    orphan_exact_args+=(--attempt "$attempt_id")
    orphan_exact=$(python3 "$SCRIPT_DIR/dispatch-registry.py" "${orphan_exact_args[@]}" --agent-home "$AGENT_HOME" 2>/dev/null || true)
    if printf '%s\n' "$orphan_exact" | grep -q '^state=dead$'; then
      orphan_info=$(python3 "$SCRIPT_DIR/dispatch-registry.py" orphan-status --attempt "$attempt_id" --jobs "$SOURCE_JOBS" --agent-home "$AGENT_HOME" 2>/dev/null || true)
      if printf '%s\n' "$orphan_info" | grep -q '^orphan=1$'; then
        orphan_route=$(printf '%s\n' "$orphan_info" | sed -n 's/^route_id=//p')
        orphan_boundary=$(printf '%s\n' "$orphan_info" | sed -n 's/^resume_boundary=//p')
        echo "⚠️ ORPHANED ${slug:-?}  — pipeline orphaned; route=$orphan_route; resume boundary=$orphan_boundary; dispatch-depth-0 decision  [open: $ts]"
        suspect=$((suspect + 1)); continue
      fi
    fi
  fi

  case "$terminal_state" in
    valid)
      terminal_event_label="turn.completed"
      [ "$terminal_source" = "exact-claude-result" ] && terminal_event_label="Claude result"
      if [ "$terminal_verdict" = "PASS" ]; then
        echo "⚠️ COMPLETED ${slug:-?}  — exact $terminal_event_label PASS; harvest required (artifact_state=$terminal_artifact; blocker_reason=none)  [open: $ts]"
      else
        echo "⚠️ EXITED   ${slug:-?}  — exact $terminal_event_label $terminal_verdict (blocker_reason=$terminal_blocker; artifact_state=$terminal_artifact)  [open: $ts]"
      fi
      suspect=$((suspect + 1)); continue ;;
    invalid)
      echo "⚠️ EXITED   ${slug:-?}  — invalid-handoff (artifact_state=$terminal_artifact; blocker_reason=contract-violation)  [open: $ts]"
      suspect=$((suspect + 1)); continue ;;
    error)
      echo "⚠️ EXITED   ${slug:-?}  — terminal-inspector-error (artifact_state=$terminal_artifact; blocker_reason=contract-violation)  [open: $ts]"
      suspect=$((suspect + 1)); continue ;;
    wire-invalid)
      echo "⚠️ EXITED   ${slug:-?}  — inspector-wire-invalid (blocker_reason=contract-violation)  [open: $ts]"
      suspect=$((suspect + 1)); continue ;;
  esac
  if [ "$observed_state" = "reconcile-needed" ]; then
    echo "⚠️ RECONCILE ${slug:-?}  — terminal-observed/reconcile-needed ($observed_reason; $observed_process_reason)  [open: $ts]"
    suspect=$((suspect + 1)); continue
  fi
  [ -d /proc ] || pid=""   # Fall back to transcript mtime without /proc.
  if [ -n "$pid" ] && [ -n "$pid_start" ]; then
    exact_args=(attempt-state --pid "$pid" --pid-start "$pid_start")
    [ -n "$pid_scope" ] && exact_args+=(--pid-scope "$pid_scope")
    [ -n "$attempt_id" ] && exact_args+=(--attempt "$attempt_id")
    [ -n "$route_id" ] && exact_args+=(--route "$route_id")
    [ -n "$route_node" ] && exact_args+=(--node "$route_node")
    exact=$(python3 "$SCRIPT_DIR/dispatch-registry.py" "${exact_args[@]}" --agent-home "$AGENT_HOME" 2>/dev/null || true)
    exact_state=$(printf '%s\n' "$exact" | sed -n 's/^state=//p' | head -1)
    classifier=$(printf '%s\n' "$exact" | sed -n 's/^classifier_source=//p' | head -1)
    if [ "$exact_state" = "working" ]; then
      if [ "$pid_scope" = "namespace-local" ]; then
        echo "ALIVE      ${slug:-?}  (namespace-local exact heartbeat; harness=$harness; classifier=$classifier)"
      else
        echo "ALIVE      ${slug:-?}  (pid $pid running; harness=$harness; classifier=$classifier)"
      fi
      alive=$((alive + 1)); continue
    fi
    if [ "$exact_state" = "dead" ]; then
      # SD-64/71: a dead conductor row may be a detected orphan (route
      # incomplete + an open/live child or a ready un-started successor).
      # Reuse the registry's own classification rather than re-deriving it.
      if [ -n "$attempt_id" ]; then
        orphan_info=$(python3 "$SCRIPT_DIR/dispatch-registry.py" orphan-status --attempt "$attempt_id" --jobs "$SOURCE_JOBS" --agent-home "$AGENT_HOME" 2>/dev/null || true)
      else
        orphan_info=""
      fi
      if printf '%s\n' "$orphan_info" | grep -q '^orphan=1$'; then
        orphan_route=$(printf '%s\n' "$orphan_info" | sed -n 's/^route_id=//p')
        orphan_boundary=$(printf '%s\n' "$orphan_info" | sed -n 's/^resume_boundary=//p')
        echo "⚠️ ORPHANED ${slug:-?}  — pipeline orphaned; route=$orphan_route; resume boundary=$orphan_boundary; dispatch-depth-0 decision  [open: $ts]"
      elif log_hit=$(scan_log_death "$slug"); then
        echo "⚠️ DEAD     ${slug:-?}  — trailing limit/auth log pattern ($log_hit)  [open: $ts]"
      else
        echo "⚠️ EXITED   ${slug:-?}  — pid $pid ended or identity changed; classifier=$classifier  [open: $ts]"
      fi
      suspect=$((suspect + 1)); continue
    fi
    if [ "$exact_state" = "done" ]; then
      echo "⚠️ COMPLETED ${slug:-?}  — namespace-local terminal heartbeat awaits registry reconciliation  [open: $ts]"
      suspect=$((suspect + 1)); continue
    fi
    # A namespace-local PID cannot be checked from the root namespace. If its exact
    # heartbeat is no longer fresh, continue with the harness transcript fallback
    # instead of probing an unrelated host PID with the same numeric value.
    [ "$pid_scope" = "namespace-local" ] && pid=""
  fi
  if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
    actual_start=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || true)
    identity_ok=0
    case "$harness" in
      codex)    printf '%s' "$cmdline" | grep -q 'codex' && identity_ok=1 ;;
      opencode) printf '%s' "$cmdline" | grep -q 'opencode' && identity_ok=1 ;;
      claude|*) printf '%s' "$cmdline" | grep -q 'claude' && identity_ok=1 ;;
    esac
    if [ "$identity_ok" -eq 1 ] && { [ -z "$pid_start" ] || [ "$pid_start" = "$actual_start" ]; }; then
      echo "ALIVE      ${slug:-?}  (pid $pid running; harness=$harness)"
      alive=$((alive + 1)); continue
    fi
  fi
  if [ -n "$pid" ]; then
    # Dead PID + open row is an unharvested completion or failure.
    if log_hit=$(scan_log_death "$slug"); then
      echo "⚠️ DEAD     ${slug:-?}  — trailing limit/auth log pattern ($log_hit)  [open: $ts]"
    else
      echo "⚠️ EXITED   ${slug:-?}  — pid $pid ended while row stayed open; harvest the dispatch-log verdict  [open: $ts]"
    fi
    suspect=$((suspect + 1)); continue
  fi
  # Harness-aware fallback for PID-less legacy rows.
  evidence_label="transcript"
  case "$harness" in
    codex)
      dir="$LOG_DIR"
      newest="$LOG_DIR/${slug}.codex.jsonl"
      [ -f "$newest" ] || newest=""
      evidence_label="Codex dispatch log"
      ;;
    opencode)
      dir="$LOG_DIR"
      newest="$LOG_DIR/${slug}.heartbeat"
      [ -f "$newest" ] || newest="$LOG_DIR/${slug}.opencode.jsonl"
      [ -f "$newest" ] || newest=""
      evidence_label="OpenCode heartbeat/log"
      ;;
    *)
      enc=$(printf '%s' "${wt:-}" | sed 's#[/._]#-#g')
      name=""
      case "$pipe" in *profile=*) name=${pipe##*profile=}; name=${name%%,*};; esac
      if [ -n "$name" ]; then
        dir="$STATE_ROOT/homes/${slug}.${name}/projects/$enc"
      else
        dir="$PROJ/$enc"
      fi
      newest=$(ls -t "$dir"/*.jsonl 2>/dev/null | head -1)
      ;;
  esac
  if [ -z "$newest" ]; then
    # No transcript means the worker died before launch or never started.
    if log_hit=$(scan_log_death "$slug"); then
      echo "⚠️ DEAD     ${slug:-?}  — trailing limit/auth log pattern ($log_hit)  [open: $ts]"
    else
      echo "⚠️ DEAD     ${slug:-?}  — no $evidence_label ($dir)  [open: $ts]"
    fi
    suspect=$((suspect + 1)); continue
  fi
  mt=$(stat -c %Y "$newest" 2>/dev/null || echo 0)
  age=$(( (now - mt) / 60 ))
  if [ "$age" -le "$STALE_MIN" ]; then
    # Fresh transcript evidence excludes a DEAD verdict from log prose.
    echo "ALIVE      ${slug:-?}  ($evidence_label updated ${age}m ago)"
    alive=$((alive + 1))
  else
    # A stale transcript is a hang or post-failure stop; attach a fatal log reason when present.
    if log_hit=$(scan_log_death "$slug"); then
      echo "⚠️ DEAD     ${slug:-?}  — trailing limit/auth log pattern ($log_hit)  [open: $ts]"
    else
      echo "⚠️ SUSPECT  ${slug:-?}  — $evidence_label stalled for ${age}m (possible hang/death)  [open: $ts]"
    fi
    suspect=$((suspect + 1))
  fi
done < "$JOBS"

echo "— open $open_n · alive $alive · suspect/dead/exited $suspect"
if [ "$suspect" -gt 0 ]; then
  echo "→ terminal/SUSPECT/DEAD/EXITED: inspect typed status, then harvest or redispatch; do not wait indefinitely."
  exit 3
fi
exit 0
