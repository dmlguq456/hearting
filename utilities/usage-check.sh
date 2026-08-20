#!/usr/bin/env sh
# usage-check — SD-16(a) usage-aware cross-harness dispatch helper
# (OPERATIONS §5.10). Before dispatch, report each harness as
# `{ok | limited(<reset>) | unknown}` for the orchestrator/main/conductor.
#
#   Surface limit (runtime-currentness, 2026-07): Claude `/usage` and Codex
#   `/status` are interactive commands; neither exposes a scriptable headless
#   usage API. This helper therefore uses the conservative `note=dead-*limit*`
#   markers written to jobs.log by the SD-15 wrapper.
#
#   SD-16e reset semantics:
#     limited(<reset>)       known future reset on a dead-limit marker; the
#                            reset clock takes precedence over marker age.
#     limited(unknown-reset) no parseable reset; remain conservative only for
#                            UNKNOWN_WINDOW_MIN (default 60 minutes).
#     ok                     no active marker, reset passed, or unknown window
#                            expired. This means "no known block," not guaranteed capacity.
#     unknown                jobs.log is unavailable; the orchestrator decides.
#
#   Usage: usage-check.sh [--harness claude|codex|opencode|all] [--jobs <path>]
#                         [--unknown-window-min <N>]
#   Output: one parseable `<harness> <state>` line per harness, then
#   `bias <harness>`. This informational command always exits 0.
#
#   Capacity policy: HARNESS_CAPACITY_BIAS overrides the neutral `auto` default;
#   no runtime is assumed to have more capacity without current evidence.
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/agent-home.sh")}"

HARNESS="all"
JOBS=""
# Unknown resets stay limited only within this bounded conservative window.
UNKNOWN_WINDOW_MIN="${UNKNOWN_WINDOW_MIN:-60}"
while [ $# -gt 0 ]; do
  case "$1" in
    --harness) HARNESS="${2:-all}"; shift 2 ;;
    --jobs) JOBS="${2:-}"; shift 2 ;;
    --window-min) UNKNOWN_WINDOW_MIN="${2:-60}"; shift 2 ;; # backward-compatible alias
    --unknown-window-min) UNKNOWN_WINDOW_MIN="${2:-60}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage-check: unknown arg '$1'" >&2; exit 64 ;;
  esac
done
if [ -z "$JOBS" ]; then
  STATE_ROOT=$("$SCRIPT_DIR/dispatch-state-root.sh") || exit $?
  JOBS="$STATE_ROOT/jobs.log"
fi
case "$HARNESS" in all) HARNESSES="claude codex opencode" ;; *) HARNESSES="$HARNESS" ;; esac

now=$(date +%s)

# Convert ISO8601 (...Z) to epoch; return 0 on failure.
to_epoch() { date -d "$(printf '%s' "$1" | sed 's/Z$/ UTC/')" +%s 2>/dev/null || echo 0; }

# Convert a reset clock (3pm, noon, 15:45, ...) to its first occurrence after
# the marker timestamp. Return 0 when absent or unparseable.
reset_to_epoch() { # $1=reset string, $2=marker epoch; print epoch or 0
  r=$1; mep=$2
  case "$r" in ''|-|unknown|unknown-reset) echo 0; return ;; esac
  # Normalize noon/midnight into clocks understood by date.
  r=$(printf '%s' "$r" | sed 's/[Nn][Oo][Oo][Nn]/12pm/; s/[Mm][Ii][Dd][Nn][Ii][Gg][Hh][Tt]/12am/')
  e=$(date -d "$r" +%s 2>/dev/null || echo 0)
  [ "${e:-0}" -gt 0 ] 2>/dev/null || { echo 0; return; }
  # date interprets a bare clock today; roll forward when the marker is later.
  if [ "$mep" -gt 0 ] && [ "$e" -lt "$mep" ]; then e=$((e + 86400)); fi
  echo "$e"
}

# Print the latest dead-limit marker for one harness as `<timestamp> <reset>`.
latest_limit_marker() { # $1=harness
  h=$1
  [ -f "$JOBS" ] || return 0
  awk -F'\t' -v h="$h" '
    $6 ~ /note=dead-[a-z-]*limit/ {
      # Attribute the marker to the harness whose session actually hit the
      # limit: the row-own harness= field. owner_harness= is fleet-continuity
      # metadata (SD-16c) naming the launching parent, so a codex child under
      # a claude owner must not flip claude to limited; it is consulted only
      # when harness= is absent (legacy rows). Field-anchored parsing also
      # keeps owner_harness=<h> from substring-matching "harness=<h>".
      n=split($6, kv, ",")
      row_h=""; owner_h=""; reset="-"
      for (i=1;i<=n;i++) {
        if (kv[i] ~ /^harness=/) { row_h=substr(kv[i],9) }
        else if (kv[i] ~ /^owner_harness=/) { owner_h=substr(kv[i],15) }
        else if (kv[i] ~ /^reset=/) { reset=substr(kv[i],7) }
      }
      exec_h = (row_h != "") ? row_h : owner_h
      if (exec_h == h) { print $1 "\t" reset }
    }' "$JOBS" | sort | tail -1
}

for h in $HARNESSES; do
  state="ok"
  marker=$(latest_limit_marker "$h")
  reset="-"
  if [ -n "$marker" ]; then
    mts=$(printf '%s' "$marker" | cut -f1)
    reset=$(printf '%s' "$marker" | cut -f2)
    mepoch=$(to_epoch "$mts")
    age_min=$(( (now - mepoch) / 60 ))
    if [ "$mepoch" -gt 0 ]; then
      # A known reset clock wins over any stale fixed-duration assumption.
      reset_epoch=$(reset_to_epoch "$reset" "$mepoch")
      if [ "$reset_epoch" -gt 0 ]; then
        if [ "$now" -lt "$reset_epoch" ]; then
          state="limited(${reset})"
        fi
      elif [ "$age_min" -lt "$UNKNOWN_WINDOW_MIN" ]; then
        # Unknown reset: stay conservative only inside the bounded window.
        state="limited(unknown-reset)"
      fi
    fi
  fi
  # Without jobs.log, capacity evidence is unavailable.
  [ -f "$JOBS" ] || state="unknown"
  printf '%s %s\n' "$h" "$state"
done

# Neutral default: do not favor a harness without an explicit override.
printf 'bias %s\n' "${HARNESS_CAPACITY_BIAS:-auto}"
