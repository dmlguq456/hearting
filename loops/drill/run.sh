#!/bin/bash
# Drill-set instruction regression runner. Usage: run.sh [case_id ...]
# Captures assertions and context-cost metrics; g0_overhead tracks fixed harness overhead.
set -u
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_AGENT_HOME=$(sh "$SCRIPT_DIR/../../utilities/agent-home.sh" 2>/dev/null || printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/hearting/current")
AGENT_HOME="${AGENT_HOME:-$DEFAULT_AGENT_HOME}"
export AGENT_HOME
GOLD="${DRILL_HOME:-$AGENT_HOME/loops/drill}"   # DRILL_HOME supports worktree tests without changing production.
# Adapter runner (core/adapter split): the CASES are portable, the RUNNER is
# adapter-specific. DRILL_ADAPTER / --adapter selects claude|codex|opencode.
# shellcheck source=../lib-runner.sh
. "$SCRIPT_DIR/../lib-runner.sh"
ADAPTER="${DRILL_ADAPTER:-claude}"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
RUNNER_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$AGENT_HOME")
STAMP=$(date +%F_%H%M)
RESULTS="$GOLD/results/$STAMP"
mkdir -p "$RESULTS"

# Arguments: --axis <git|spec|memory|routing|artifact|meta>, --sample N, and case IDs.
# No arguments runs the full set; IDs, axis filtering, and sampling may be combined.
AXIS=""; SAMPLE=""; LIST=""; ids=()
while [ $# -gt 0 ]; do
  case "$1" in
    --axis)    AXIS="${2:-}"; shift 2 ;;
    --sample)  SAMPLE="${2:-}"; shift 2 ;;
    --adapter) ADAPTER="${2:-claude}"; shift 2 ;;
    --list)    LIST=1; shift ;;
    *)         ids+=("$1"); shift ;;
  esac
done
export DRILL_ADAPTER="$ADAPTER"
# Codex workspace-write deliberately protects .git metadata, while several
# disposable drill cases validate branch/worktree operations. Scope the wider
# sandbox to drill only; every other loop keeps lib-runner's workspace-write
# default. Set DRILL_CODEX_SANDBOX=workspace-write to opt back down.
if [ "$ADAPTER" = "codex" ]; then
  export LOOP_CODEX_SANDBOX="${DRILL_CODEX_SANDBOX:-danger-full-access}"
  # Depth-1/2 Codex workers are nested processes. In workspace-write the parent
  # cannot update git metadata and its children inherit network denial, so carry
  # the drill-only sandbox choice through dispatch-headless.py as well.
  export CODEX_DISPATCH_SANDBOX="$LOOP_CODEX_SANDBOX"
  # A stage owner may spell out a narrower --sandbox after reading a generic
  # command template. Drill fixtures need the selected sandbox to remain an
  # invariant across nested depth-1/2 dispatch, so force it only for this loop.
  export CODEX_DISPATCH_SANDBOX_FORCE="$LOOP_CODEX_SANDBOX"
  # Fleet ancestry must follow the actual running Codex thread, not a model-
  # invented parent id/slug. The wrapper applies this dynamically in every
  # depth-1/2 session; depth 1 has no dispatch-job parent slug of its own.
  export CODEX_DISPATCH_PARENT_CURRENT_FORCE=1
fi

# --- conformance pre-stage (P-20) ---
# Initialize conformance before building the case pool so an empty selection cannot bypass it.
# Explicit initialization also keeps set -u safe on skipped paths.
CONF_STATUS=SKIP; CONF_FAIL=0; CASE_FAIL=0

RUN_CONFORMANCE=0
if [ "${DRILL_CONFORMANCE_ONLY:-0}" = "1" ]; then
  # A conformance-only run ignores list and skip controls.
  RUN_CONFORMANCE=1
elif [ -n "$LIST" ] || [ "${DRILL_SKIP_CONFORMANCE:-0}" = "1" ]; then
  RUN_CONFORMANCE=0
elif [ ${#ids[@]} -gt 0 ] || [ -n "$AXIS" ] || [ -n "$SAMPLE" ]; then
  # Subset runs skip unrelated parity checks unless DRILL_CONFORMANCE=1 opts in.
  [ "${DRILL_CONFORMANCE:-0}" = "1" ] && RUN_CONFORMANCE=1
else
  RUN_CONFORMANCE=1   # Full runs enable conformance by default.
fi

if [ "$RUN_CONFORMANCE" = "1" ]; then
  # Resolve conformance scripts from this runner's worktree, not a primary-repo AGENT_HOME.
  # Call the repo-root portable guard because its root resolution is path-relative.
  HARNESS_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$AGENT_HOME")
  echo "== conformance (tree=$HARNESS_ROOT) =="
  echo "conformance=worktree / cases=primary"

  if bash "$HARNESS_ROOT/tools/check-adaptation-boundary.sh"; then
    CONF_STATUS=PASS
  else
    CONF_STATUS=FAIL
    CONF_FAIL=1
  fi

  # portable-guards is informational because environment-gated runtime checks may lack binaries.
  if bash "$HARNESS_ROOT/hooks/portable-guards.test.sh"; then
    echo "conformance: portable-guards.test.sh PASS (informational)"
  else
    echo "conformance: portable-guards.test.sh FAIL (informational; runtime doctor may be environment-gated)"
  fi
fi

if [ "${DRILL_CONFORMANCE_ONLY:-0}" = "1" ]; then
  {
    echo "# Drill conformance-only run $STAMP"
    echo
    echo "| conformance | verdict |"
    echo "|---|---|"
    echo "| conformance | $CONF_STATUS |"
  } > "$RESULTS/summary.md"
  exit "$CONF_FAIL"
fi

# Build the candidate pool from explicit IDs or all cases.
if [ ${#ids[@]} -gt 0 ]; then
  cases=("${ids[@]}")
else
  cases=($(ls "$GOLD/cases"))
  for g in $(ls "$GOLD/cases_growing" 2>/dev/null); do cases+=("growing:$g"); done
fi

# Resolve case IDs to directories.
_casedir() { case "$1" in growing:*) echo "$GOLD/cases_growing/${1#growing:}" ;; *) [ -d "$GOLD/cases/$1" ] && echo "$GOLD/cases/$1" || echo "$GOLD/cases_growing/$1" ;; esac; }

# Filter by the AXIS value in each config.
if [ -n "$AXIS" ]; then
  filtered=()
  for c in "${cases[@]}"; do
    a=""; cf="$(_casedir "$c")/config"; [ -f "$cf" ] && a=$(sed -n 's/^AXIS=//p' "$cf" | tr -d ' "')
    [ "$a" = "$AXIS" ] && filtered+=("$c")
  done
  cases=("${filtered[@]}")
fi

# Randomly sample N candidates for periodic checks.
if [ -n "$SAMPLE" ] && [ "$SAMPLE" -gt 0 ] 2>/dev/null && [ ${#cases[@]} -gt "$SAMPLE" ]; then
  mapfile -t cases < <(printf '%s\n' "${cases[@]}" | shuf | head -n "$SAMPLE")
fi

[ ${#cases[@]} -eq 0 ] && { echo "selected 0 cases (no match for axis='$AXIS'?)"; exit 0; }
echo "drill targets: ${#cases[@]}${AXIS:+ [axis=$AXIS]}${SAMPLE:+ [sample=$SAMPLE]}: ${cases[*]}"
[ -n "$LIST" ] && { echo "(--list: selection only; nothing executed)"; exit 0; }

declare -A verdicts metrics
case_workdirs=()
for c in "${cases[@]}"; do
  grow=""
  case "$c" in growing:*) grow="(g)"; CASE_DIR="$GOLD/cases_growing/${c#growing:}" ;; *) CASE_DIR="$GOLD/cases/$c"; [ -d "$CASE_DIR" ] || CASE_DIR="$GOLD/cases_growing/$c" ;; esac
  [ -d "$CASE_DIR" ] || { echo "SKIP $c (missing)"; continue; }
  MAX_TURNS=""; TIMEOUT=1800; ADAPTERS=""
  [ -f "$CASE_DIR/config" ] && . "$CASE_DIR/config"
  # Pin cases that require runtime-specific evidence through config ADAPTERS.
  if [ -n "$ADAPTERS" ] && ! printf ' %s ' "$ADAPTERS" | grep -qF " $ADAPTER "; then
    verdicts[$c]="SKIP(adapter!=$ADAPTERS)"; metrics[$c]="0|0|0|0"
    echo "▶ $c → SKIP (requires adapter: $ADAPTERS, run=$ADAPTER)"; continue
  fi

  # Sanitize a growing: prefix so the colon cannot break paths or PYTHONPATH.
  WORK=$(mktemp -d "/tmp/drill-${c//:/_}-XXXX")
  case_workdirs+=("$WORK")
  echo "▶ $c (work=$WORK)"
  bash "$CASE_DIR/fixture.sh" "$WORK" || { verdicts[$c]="FIXTURE-ERR"; CASE_FAIL=1; continue; }

  T="$RESULTS/$c.transcript.txt"
  J="$RESULTS/$c.json"
  # Static-assert cases (AXIS=static) carry no user turn — they lint the live
  # repo deterministically (e.g. skill-conformance scan). Skip the adapter run,
  # emit a zero-cost metric, and go straight to assert.sh.
  if [ -f "$CASE_DIR/config" ] && grep -q '^AXIS=static' "$CASE_DIR/config"; then
    metrics[$c]="0|0|0|0"; : > "$T"; rc=0
  else
    # Adapter runner: writes $J (raw) + $T (normalized transcript), echoes
    # turns|in_tok|out_tok|cost. Same contract for claude|codex|opencode.
    metrics[$c]=$(run_case_on_adapter "$ADAPTER" "$CASE_DIR/prompt.md" "$WORK/repo" "$TIMEOUT" "${MAX_TURNS:-}" "$J" "$T")
    rc=$?
  fi

  # Spec-grounding marker home for assertions: guards write the marker to the
  # ADAPTER's resolved agent-home, so cases must read it there, not a literal
  # claude path. Default to this run's AGENT_HOME.
  export DRILL_MARKER_HOME="${DRILL_MARKER_HOME:-$AGENT_HOME}"

  assert_rc=0
  out=$(bash "$CASE_DIR/assert.sh" "$WORK" "$T" 2>&1) || assert_rc=$?
  if [ "$rc" -eq 0 ] && [ "$assert_rc" -eq 0 ]; then
    verdicts[$c]="PASS$grow"
  else
    verdicts[$c]="FAIL$grow"
    CASE_FAIL=1
    if [ "$rc" -ne 0 ]; then
      out="RUNTIME-FAIL: $ADAPTER exit $rc${out:+$'\n'$out}"
    fi
  fi
  echo "$out" | tee "$RESULTS/$c.assert.txt"
  echo "  → ${verdicts[$c]} ($ADAPTER exit $rc, ${metrics[$c]})"

  # Failure diagnosis uses the selected adapter and never falls back implicitly to Claude.
  if [[ "${verdicts[$c]}" == FAIL* ]] && [ "$rc" -eq 0 ] && [ "${DRILL_AUTO_DIAG:-1}" = "1" ]; then
    DIAG_PROMPT="$WORK/diagnosis.prompt.md"
    {
      echo "Diagnose this failed drill case."
      echo "Case definition: $CASE_DIR (prompt.md=user request, assert.sh=verdict)"
      echo "Assertion output: $RESULTS/$c.assert.txt"
      echo "transcript: $T"
      echo "Fixture output: $WORK"
      echo
      echo "Read this material and concisely report: (1) the violation, (2) missing or ambiguous guidance, and (3) one proposed fix. Do not modify files."
    } > "$DIAG_PROMPT"
    diag_metrics=$(DRILL_FLEET_MODE=loop/drill-diagnosis \
      run_case_on_adapter "$ADAPTER" "$DIAG_PROMPT" "$WORK/repo" 600 25 \
        "$RESULTS/$c.diagnosis.json" "$RESULTS/$c.diagnosis.md") || true
    [ -s "$RESULTS/$c.diagnosis.md" ] && echo "  diagnosis: $RESULTS/$c.diagnosis.md ($ADAPTER, ${diag_metrics:-?})"
  fi
done

{
  echo "# Drill run $STAMP"
  echo
  echo "| case | verdict | turns | in_tok | out_tok | cost\$ |"
  echo "|---|---|---|---|---|---|"
  # Conformance is not a case; emit a distinct prefixed row and omit it when skipped.
  [ "$CONF_STATUS" != SKIP ] && echo "| conformance | $CONF_STATUS | - | - | - | - |"
  for c in "${cases[@]}"; do
    IFS='|' read -r mt mi mo mc <<< "${metrics[$c]:-?|?|?|?}"
    echo "| $c | ${verdicts[$c]:-?} | $mt | $mi | $mo | $mc |"
  done
} | tee "$RESULTS/summary.md"

# Append trend metrics, especially g0_overhead input-token growth.
for c in "${cases[@]}"; do
  echo "$STAMP,$c,${verdicts[$c]:-?},${metrics[$c]:-?|?|?|?}" | tr '|' ',' >> "$GOLD/metrics.csv"
done

# Optional response-policy grading pass over all transcripts.
if [ "${RUN_JUDGE:-0}" = "1" ]; then
  JUDGE_PROMPT="$RESULTS/judge.prompt.md"
  {
    cat "$GOLD/judge.md"
    echo
    echo "Transcript directory: $RESULTS (each *.transcript.txt). Do not modify files; return only the grading report."
  } > "$JUDGE_PROMPT"
  judge_metrics=$(run_case_on_adapter "$ADAPTER" "$JUDGE_PROMPT" "$RUNNER_ROOT" 600 25 \
    "$RESULTS/judge.json" "$RESULTS/judge.md") || true
  echo "judge → $RESULTS/judge.md ($ADAPTER, ${judge_metrics:-?})"
fi

# Remove only session detritus created by this run; never touch concurrent drill paths.
for work in "${case_workdirs[@]}"; do
  if [ "$ADAPTER" = "claude" ]; then
    enc=$(printf '%s' "$work/repo" | sed 's#[/._]#-#g')
    rm -rf "$AGENT_HOME/projects/$enc" 2>/dev/null || true
  fi
  rm -rf "$work" 2>/dev/null || true
done
echo "cleanup: removed drill temp and session detritus (adapter=$ADAPTER)"

# Report conformance failures but still run cases; after cleanup, any gating failure exits nonzero.
if [ "$CONF_FAIL" -ne 0 ] || [ "$CASE_FAIL" -ne 0 ]; then
  exit 1
fi
exit 0
