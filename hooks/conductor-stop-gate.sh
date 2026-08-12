#!/bin/sh
# Stop: SD-14b conductor Stop gate — block turn-end while the conductor still
# has open depth-2 stage children (legacy polling-fallback backstop; the normal
# SD-78 path is owned by the adapter session supervisor).
#
#   ┌─────────────────────────────────────────────────────────────────────┐
#   │ UNREGISTERED / HELD as of 2026-07-10 probe.                          │
#   │ Empirical probe: a `Stop` hook does NOT fire under `claude -p`, and  │
#   │ (CC issue #38651) a registered Stop hook EMPTIES the `-p` result     │
#   │ output — which would break the very dispatch-harvest it is meant to  │
#   │ protect. So this script is kept on disk (logic + CLI ready) but is   │
#   │ NOT added to adapters/claude/settings.json. SD-14 ships via layers   │
#   │ (a) explicit wrapper poll-fallback + (c) utilities/dispatch-wait.    │
#   │ Re-register only when Claude Code fixes headless Stop firing.        │
#   │ See .agent_reports/plans/2026-07-10_stage-dispatch-phase2/            │
#   │     _internal/dev_reviews/phaseA_stop_probe.md                       │
#   └─────────────────────────────────────────────────────────────────────┘
#
#   Fire only when (else clean exit 0):
#     - AGENT_DISPATCH_DEPTH=1 (harness-planted dispatch marker → a conductor;
#       a runtime child-session marker is not used, §5.10)
#     - AGENT_DISPATCH_SELF_SLUG set (from dispatch-headless.py, needed to match
#       open children whose parent= equals my own slug)
#     - stop_hook_active is NOT already true (loop guard — never infinite-block)
#   Recursion guard: MEM_DISTILL=1 → drain stdin, exit 0.
#
#   Logic:
#     1. Resolve jobs.log via agent-home.sh (same registry as the wrapper /
#        dispatch-liveness / dispatch-wait — registry parity, SD-14b②).
#     2. Count open rows where parent=<self-slug>. Zero → exit 0 (may finish).
#     3. ≥1 open child: run dispatch-liveness on that scoped view:
#        - ALIVE → block Stop: "open stage children still running — poll with
#          dispatch-wait and harvest, do not end the turn."
#        - SUSPECT/DEAD → block with a DIAGNOSE action (not "keep waiting" — a
#          dead stage must not trap the conductor forever): "diagnose via
#          transcript tail / dispatch log → harvest or re-dispatch → clean the
#          jobs.log row, then finish." (§8.5.7, §14-(5))
#
#   Portable CLI (unit): conductor-stop-gate.sh --self-slug <slug> [--jobs <path>]
#     [--stop-active true|false]
#   Without args, reads Claude Stop hook JSON from stdin (emits Stop-hook JSON).
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/../utilities/agent-home.sh")}"
LIVENESS="$SCRIPT_DIR/../utilities/dispatch-liveness.sh"

[ "${MEM_DISTILL:-}" = "1" ] && { cat >/dev/null 2>&1; exit 0; }

# Emit a Stop-hook block decision. Claude Stop hook schema: {"decision":"block",
# "reason":"..."} keeps the turn open and feeds `reason` back to the model.
block_json() {
  printf '%s' "$1" | python3 -c 'import sys,json; print(json.dumps({"decision":"block","reason":sys.stdin.read()}))'
}

# Count + collect open child rows for a given parent slug from a jobs.log.
open_children() { # $1=jobs $2=self_slug ; prints matching open rows
  awk -F'\t' -v slug="$2" '
    ($2=="open") {
      n=split($6, kv, ",")
      for (i=1;i<=n;i++) { if (kv[i]=="parent=" slug) { print; next } }
    }' "$1" 2>/dev/null
}

# Core decision. Prints the block JSON (and returns 0) when it should block;
# returns 1 (no output) when the conductor may finish.
decide() { # $1=self_slug $2=jobs $3=stop_active
  self=$1; jobs=$2; stop_active=$3
  [ "${AGENT_DISPATCH_DEPTH:-}" = "1" ] || return 1
  [ -n "$self" ] || return 1
  [ "$stop_active" = "true" ] && return 1
  [ -f "$jobs" ] || return 1

  rows=$(open_children "$jobs" "$self")
  [ -n "$rows" ] || return 1
  n=$(printf '%s\n' "$rows" | grep -c .)

  tmp=$(mktemp)
  printf '%s\n' "$rows" > "$tmp"
  AGENT_HOME="$AGENT_HOME" "$LIVENESS" "$tmp" >/dev/null 2>&1
  live_rc=$?
  rm -f "$tmp"

  if [ "$live_rc" -eq 3 ]; then
    block_json "A stage child is SUSPECT/DEAD. Do not wait. Diagnose with the transcript tail and dispatch log, then harvest or redispatch it, clean up the jobs.log row, and only then finish."
  else
    block_json "${n} stage child job(s) are still running. Do not end the turn; poll with dispatch-wait and harvest them first."
  fi
  return 0
}

# --- CLI mode ---
if [ "$#" -gt 0 ]; then
  self="${AGENT_DISPATCH_SELF_SLUG:-}"; jobs=""; stop_active="false"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --self-slug) self=$2; shift 2 ;;
      --jobs) jobs=$2; shift 2 ;;
      --stop-active) stop_active=$2; shift 2 ;;
      -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) echo "conductor-stop-gate: unknown arg '$1'" >&2; exit 64 ;;
    esac
  done
  [ -n "$jobs" ] || jobs="${AGENT_DISPATCH_JOBS:-$AGENT_HOME/.dispatch/jobs.log}"
  decide "$self" "$jobs" "$stop_active" || true
  exit 0
fi

# --- stdin (Claude Stop hook JSON) mode ---
input=$(cat 2>/dev/null)
[ -z "$input" ] && exit 0
stop_active=$(printf '%s' "$input" | grep -o '"stop_hook_active"[[:space:]]*:[[:space:]]*true' | head -1)
[ -n "$stop_active" ] && stop_active="true" || stop_active="false"
self="${AGENT_DISPATCH_SELF_SLUG:-}"
jobs="${AGENT_DISPATCH_JOBS:-$AGENT_HOME/.dispatch/jobs.log}"
decide "$self" "$jobs" "$stop_active" || true
exit 0
