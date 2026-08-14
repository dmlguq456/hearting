#!/usr/bin/env sh
# dispatch-wait — SD-14 one-shot wait-contract helper (OPERATIONS §5.10).
#   A headless main/conductor is a one-shot process, so ending the turn also
#   ends the process. After dispatching a stage, the conductor must poll for
#   completion in the same turn instead of ending on a notification wait.
#   This helper polls the shared exact-process readiness classifier.
#
#   Usage: dispatch-wait.sh [--parent <self-slug>] [--slug <row-slug>] [--attempt-id <id>]
#                           [--jobs <path>] [--interval <s>] [--max <s>]
#     --parent     Limit open rows to children with parent=<slug>; otherwise use all.
#     --slug       Limit to the row whose own slug (field 5) exactly matches.
#     --attempt-id Limit to the row whose attempt_id=<id> kv field exactly matches.
#     --jobs       jobs.log path (default: canonical dispatch state root).
#     --interval   Poll interval in seconds (default 20).
#     --max        Maximum wait for this call (default and cap: 600).
#   --parent/--slug/--attempt-id combine with AND when more than one is given.
#
#   exit 0: all target children are semantically successful and quiescent.
#   exit 2: children remain alive at --max; call again.
#   exit 3: a quiescent child has failure/unclosed terminal evidence; diagnose.
#   Each iteration emits one status line. No background/nohup waits are used.
#   Legacy/non-registered rows remain compatible: closed rows do not require a
#   governed PID, while open rows without exact identity stay unverifiable.
set -u   # POSIX sh/dash has no pipefail; this wrapper does not depend on pipelines.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/agent-home.sh")}"
READINESS="$SCRIPT_DIR/dispatch-attempt-ready.py"

PARENT=""
SLUG=""
ATTEMPT_ID=""
JOBS=""
INTERVAL=20
MAX=600
while [ $# -gt 0 ]; do
  case "$1" in
    --parent=*) PARENT=${1#*=}; [ -n "$PARENT" ] || { echo "dispatch-wait: empty --parent" >&2; exit 64; }; shift ;;
    --parent) PARENT="${2:-}"; shift 2 ;;
    --slug=*) SLUG=${1#*=}; [ -n "$SLUG" ] || { echo "dispatch-wait: empty --slug" >&2; exit 64; }; shift ;;
    --slug) SLUG="${2:-}"; shift 2 ;;
    --attempt-id=*) ATTEMPT_ID=${1#*=}; [ -n "$ATTEMPT_ID" ] || { echo "dispatch-wait: empty --attempt-id" >&2; exit 64; }; shift ;;
    --attempt-id) ATTEMPT_ID="${2:-}"; shift 2 ;;
    --jobs=*) JOBS=${1#*=}; [ -n "$JOBS" ] || { echo "dispatch-wait: empty --jobs" >&2; exit 64; }; shift ;;
    --jobs) JOBS="${2:-}"; shift 2 ;;
    --interval=*) INTERVAL=${1#*=}; [ -n "$INTERVAL" ] || { echo "dispatch-wait: empty --interval" >&2; exit 64; }; shift ;;
    --interval) INTERVAL="${2:-20}"; shift 2 ;;
    --max=*) MAX=${1#*=}; [ -n "$MAX" ] || { echo "dispatch-wait: empty --max" >&2; exit 64; }; shift ;;
    --max) MAX="${2:-120}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "dispatch-wait: unknown arg '$1'" >&2; exit 64 ;;
  esac
done
if [ -z "$JOBS" ]; then
  # Explicit AGENT_DISPATCH_JOBS forwarding: the canonical-registry override must
  # stay visible at this call site (deterministic boundary guard greps for it),
  # not only inside dispatch-state-root.sh's own fallback chain.
  STATE_ROOT=$("$SCRIPT_DIR/dispatch-state-root.sh" "${AGENT_DISPATCH_JOBS:-}") || exit $?
  JOBS="$STATE_ROOT/jobs.log"
fi
# Clamp to a single shell-call timeout budget.
[ "$MAX" -gt 600 ] 2>/dev/null && MAX=600
[ "$INTERVAL" -ge 1 ] 2>/dev/null || INTERVAL=20

# Without jobs.log there are no selected children to wait for.
if [ ! -f "$JOBS" ]; then
  echo "(jobs.log not found: $JOBS) — no open children"
  exit 0
fi

elapsed=0
while :; do
  set -- --jobs "$JOBS"
  [ -n "$PARENT" ] && set -- "$@" --parent "$PARENT"
  [ -n "$SLUG" ] && set -- "$@" --slug "$SLUG"
  [ -n "$ATTEMPT_ID" ] && set -- "$@" --attempt-id "$ATTEMPT_ID"
  ready_out=$(python3 "$READINESS" "$@" 2>&1)
  ready_rc=$?
  if [ "$ready_rc" -eq 0 ]; then
    echo "✓ selected children are semantic-terminal and execution-quiescent — ready to harvest (exit 0)"
    exit 0
  fi
  if [ "$ready_rc" -eq 3 ]; then
    echo "⚠️ quiescent terminal failure/unclosed child detected — harvest or diagnose (exit 3)"
    printf '%s\n' "$ready_out"
    exit 3
  fi
  if [ "$ready_rc" -ne 2 ]; then
    printf '%s\n' "$ready_out" >&2
    exit "$ready_rc"
  fi

  if [ "$elapsed" -ge "$MAX" ]; then
    echo "… selected children still running or unverifiable after ${elapsed}s (max ${MAX}s) — call again (exit 2)"
    exit 2
  fi

  echo "… selected children not execution-quiescent — poll again in ${INTERVAL}s (elapsed ${elapsed}s/${MAX}s)"
  sleep "$INTERVAL"
  elapsed=$((elapsed + INTERVAL))
done
