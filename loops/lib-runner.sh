#!/usr/bin/env bash
# Portable loop/drill adapter runner — the core/adapter split for loop engineering.
#
# Loop CASES (prompt/fixture/assert) are runtime-neutral; only the RUNNER is
# adapter-specific. This library runs a prompt on the chosen runtime adapter and
# normalizes the result to (a) a transcript file and (b) a
# "turns|in_tok|out_tok|cost" metrics line on stdout — the same contract for all
# three adapters, so `run.sh` and the loop scripts stay adapter-agnostic.
#
#   run_case_on_adapter <adapter> <prompt_file> <repo_dir> <timeout_s> <max_turns> <out_json> <out_transcript>
#     adapters: claude | codex | opencode
#     stdout  : turns|in_tok|out_tok|cost
#
# Prereq for codex/opencode: the runtime projection must be installed (so the
# harness bootstrap + hooks/plugin load in the headless run); otherwise the case
# runs without the harness and the assertion is meaningless. Callers should gate
# on `preflight.sh doctor --runtime` (codex) / installed plugin (opencode).
#
# JSON event schemas (probed 2026-07-01):
#   codex exec --json : {"type":"item.completed","item":{"type":"agent_message","text":..}}
#                       {"type":"turn.completed","usage":{"input_tokens","output_tokens"}}
#   opencode run --format json : {"type":"text","part":{"text":..}}
#                       {"type":"step_finish","part":{"tokens":{"input","output"},"cost":..}}

DRILL_CLAUDE_TOOLS="${DRILL_CLAUDE_TOOLS:-Bash,Read,Write,Edit,Glob,Grep,Skill,Agent,TodoWrite}"

_loop_agent_home() {
  if [ -n "${AGENT_HOME:-}" ]; then
    printf '%s\n' "$AGENT_HOME"
    return 0
  fi
  local src="${BASH_SOURCE[0]:-$0}" dir root cand
  dir=$(CDPATH= cd -- "$(dirname -- "$src")" && pwd)
  root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null || true)
  if [ -n "$root" ] && [ -f "$root/core/CORE.md" ]; then
    printf '%s\n' "$root"
    return 0
  fi
  cand="$HOME/hearting"
  if [ -d "$cand" ]; then
    printf '%s\n' "$cand"
    return 0
  fi
  cand="$HOME/agent_setting"
  if [ -d "$cand" ]; then
    printf '%s\n' "$cand"
    return 0
  fi
  printf '%s\n' "${CLAUDE_HOME:-$HOME/.claude}"
}

_loop_governor_path() {
  local agent_home src dir root
  agent_home="$(_loop_agent_home)"
  if [ -f "$agent_home/utilities/model-worker-governor.py" ]; then
    printf '%s\n' "$agent_home/utilities/model-worker-governor.py"
    return 0
  fi
  src="${BASH_SOURCE[0]:-$0}"
  dir=$(CDPATH= cd -- "$(dirname -- "$src")" && pwd)
  root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null || true)
  if [ -n "$root" ] && [ -f "$root/utilities/model-worker-governor.py" ]; then
    printf '%s\n' "$root/utilities/model-worker-governor.py"
    return 0
  fi
  printf 'model-worker governor unavailable for loop runner\n' >&2
  return 69
}

_loop_jobs_path() {
  if [ -n "${AGENT_DISPATCH_JOBS:-}" ]; then
    printf '%s\n' "$AGENT_DISPATCH_JOBS"
    return 0
  fi
  printf '%s/.dispatch/jobs.log\n' "$(_loop_agent_home)"
}

_loop_registry_value() {
  local v="${1:-}"
  v=${v//$'\t'/_}
  v=${v//$'\n'/_}
  v=${v//$'\r'/_}
  v=${v//,/_}
  printf '%s' "$v"
}

_loop_case_id() {
  local pf=$1 dir
  dir=$(CDPATH= cd -- "$(dirname -- "$pf")" && pwd)
  _loop_registry_value "$(basename "$dir")"
}

_loop_registry_slug() {
  local adapter=$1 case_id=$2 raw
  raw="drill-$adapter-$case_id-$(date -u +%Y%m%d%H%M%S)-$$"
  raw=${raw//[^A-Za-z0-9_.-]/-}
  printf '%s\n' "$raw"
}

_loop_registry_append() {
  local jobs=$1 line=$2 dir lock
  dir=$(dirname -- "$jobs")
  mkdir -p "$dir" 2>/dev/null || return 0
  lock="$jobs.lock"
  if command -v flock >/dev/null 2>&1; then
    (
      flock -x 9
      printf '%s\n' "$line" >> "$jobs"
    ) 9>>"$lock" 2>/dev/null || true
  else
    printf '%s\n' "$line" >> "$jobs" 2>/dev/null || true
  fi
}

_loop_registry_finish_unlocked() {
  local jobs=$1 slug=$2 death_reason=$3 reset=$4
  python3 - "$jobs" "$slug" "$death_reason" "$reset" <<'PY'
import os
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
slug, reason, reset = sys.argv[2:]
try:
    lines = path.read_text(encoding="utf-8").splitlines()
    mode = stat.S_IMODE(path.stat().st_mode)
except OSError:
    raise SystemExit(3)

changed = False
for idx, line in enumerate(lines):
    fields = line.split("\t")
    if len(fields) != 6 or fields[4] != slug or fields[1] not in {"open", "running"}:
        continue
    fields[1] = "done"
    if reason:
        fields[5] += f",note=dead-{reason}"
        if reset:
            fields[5] += f",reset={reset}"
    lines[idx] = "\t".join(fields)
    changed = True

if not changed:
    raise SystemExit(3)

tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
try:
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
finally:
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
PY
}

_loop_registry_finish() {
  local jobs=$1 slug=$2 death_reason=$3 reset=$4 dir lock
  dir=$(dirname -- "$jobs")
  mkdir -p "$dir" 2>/dev/null || return 3
  lock="$jobs.lock"
  if command -v flock >/dev/null 2>&1; then
    (
      flock -x 9
      _loop_registry_finish_unlocked "$jobs" "$slug" "$death_reason" "$reset"
    ) 9>>"$lock"
  else
    _loop_registry_finish_unlocked "$jobs" "$slug" "$death_reason" "$reset"
  fi
}

_loop_register_dispatch_job() {
  [ "${DRILL_FLEET_REGISTRY:-1}" = "0" ] && return 0
  local status=$1 adapter=$2 pf=$3 repo=$4 slug=$5
  local death_reason="${6:-}" reset="${7:-}"
  local jobs ts case_id git_root parent_sid parent_cwd parent_slug mode pipe line
  jobs=$(_loop_jobs_path)
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  case_id=$(_loop_case_id "$pf")
  git_root=$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$repo")
  # A drill case is its own fixture-rooted Fleet tree. Do not inherit the
  # interactive launcher session/cwd: doing so renders one run twice, once under
  # agent_setting and once under /tmp/drill-*. Explicit drill-only parent fields
  # remain available for a caller that intentionally nests diagnostic workers.
  parent_sid="${DRILL_FLEET_PARENT_SESSION_ID:-}"
  parent_cwd="${DRILL_FLEET_PARENT_CWD:-}"
  parent_slug="${DRILL_FLEET_PARENT_SLUG:-}"
  mode=$(_loop_registry_value "${DRILL_FLEET_MODE:-loop/drill}")
  pipe="capability=drill,mode=$mode,qa=quick,intensity=quick,attempt_schema_version=2,attempt_id=att-$slug,dispatch_depth=1,transport=headless,execution_surface=registered-headless,registered_worker=1,fallback_hop=same-harness-headless,harness=$(_loop_registry_value "$adapter"),worker_type=owner,assigned_contract=drill,owner=drill"
  if [ -n "$parent_sid" ]; then
    pipe="$pipe,parent_sid=$(_loop_registry_value "$parent_sid")"
  fi
  if [ -n "$parent_cwd" ]; then
    pipe="$pipe,parent_cwd=$(_loop_registry_value "$parent_cwd")"
  fi
  if [ -n "$parent_slug" ]; then
    pipe="$pipe,parent=$(_loop_registry_value "$parent_slug")"
  fi
  if [ -n "$death_reason" ]; then
    pipe="$pipe,note=dead-$(_loop_registry_value "$death_reason")"
    [ -n "$reset" ] && pipe="$pipe,reset=$(_loop_registry_value "$reset")"
  fi
  if [ "$status" = "done" ] && _loop_registry_finish "$jobs" "$slug" \
    "$(_loop_registry_value "$death_reason")" "$(_loop_registry_value "$reset")"; then
    return 0
  fi
  line=$(printf '%s\t%s\t%s\t%s\t%s\t%s' \
    "$ts" "$status" "$git_root" "$repo" "$slug" "$pipe")
  _loop_registry_append "$jobs" "$line"
}

# Runtime failures are also Fleet availability signals. Extract a stable death
# reason and, when the runtime supplies one, a machine-parseable reset time from
# the raw event stream/stderr. Empty output means an ordinary non-limit failure.
_loop_failure_metadata() {
  local raw=$1
  python3 - "$raw" "${raw%.json}.stderr.txt" <<'PY'
import datetime as dt
import pathlib
import re
import sys

text = "\n".join(
    p.read_text(errors="replace")
    for name in sys.argv[1:]
    if (p := pathlib.Path(name)).is_file()
)
low = text.lower()

reason = ""
if re.search(r"(?:session|weekly|monthly)\s+(?:usage\s+)?limit", low):
    reason = "session-limit"
elif re.search(r"(?:usage|rate)\s+limit|hit\s+your[^\n]{0,80}\blimit\b|quota\s+exceeded", low):
    reason = "usage-limit"
elif re.search(r"(?:credit|balance)[^\n]{0,80}(?:exhaust|insufficient|limit|deplet)", low):
    reason = "credit-limit"
elif re.search(r"authentication|unauthori[sz]ed|invalid\s+(?:api\s+)?key|login\s+required", low):
    reason = "auth"

if not reason:
    raise SystemExit(0)

reset = ""
match = re.search(r"try\s+again\s+at\s+([^\n\"]+)", text, re.I)
if match:
    value = match.group(1).strip().rstrip(". )]}")
    value = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", value, flags=re.I)
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            reset = dt.datetime.strptime(value, fmt).isoformat(timespec="seconds")
            break
        except ValueError:
            pass

if not reset:
    match = re.search(
        r"reset(?:s|ting)?(?:\s+at|\s*:)[ \t]*"
        r"([0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?|noon|midnight)",
        text,
        re.I,
    )
    if match:
        reset = re.sub(r"\s+", "", match.group(1).lower())

print(f"{reason}\t{reset}")
PY
}

run_case_on_adapter() {
  local adapter=$1
  shift
  case "$adapter" in
    claude|codex|opencode) ;;
    *) echo "?|?|?|?"; return 64 ;;
  esac

  local pf=$1 repo=$2 case_id slug metrics rc failure_meta death_reason reset
  case_id=$(_loop_case_id "$pf")
  slug=$(_loop_registry_slug "$adapter" "$case_id")
  _loop_register_dispatch_job open "$adapter" "$pf" "$repo" "$slug" || true

  case "$adapter" in
    claude)   metrics=$(_loop_run_claude   "$@"); rc=$? ;;
    codex)    metrics=$(_loop_run_codex    "$@"); rc=$? ;;
    opencode) metrics=$(_loop_run_opencode "$@"); rc=$? ;;
  esac

  death_reason=""; reset=""
  if [ "$rc" -ne 0 ]; then
    failure_meta=$(_loop_failure_metadata "$5" 2>/dev/null || true)
    IFS=$'\t' read -r death_reason reset <<< "$failure_meta"
  fi
  _loop_register_dispatch_job done "$adapter" "$pf" "$repo" "$slug" "$death_reason" "$reset" || true
  printf '%s\n' "${metrics:-?|?|?|?}"
  return "$rc"
}

_loop_run_claude() {
  local pf=$1 repo=$2 to=$3 maxturns=$4 j=$5 t=$6
  local bin="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
  local agent_home="$(_loop_agent_home)"
  local governor runtime_rc parse_rc
  governor=$(_loop_governor_path) || return $?
  export AGENT_HOME="$agent_home"
  local settings="${DRILL_CLAUDE_SETTINGS:-$AGENT_HOME/adapters/claude/settings.json}"
  local settings_arg=()
  [ -f "$settings" ] && settings_arg=(--settings "$settings")
  ( cd "$repo" && AGENT_HOME="$agent_home" AGENT_SESSION_ROLE=worker python3 "$governor" run --class loop -- timeout "$to" "$bin" -p "$(cat "$pf")" \
      "${settings_arg[@]}" --allowedTools "$DRILL_CLAUDE_TOOLS" --output-format json ${maxturns:+--max-turns "$maxturns"} ) \
      > "$j" 2>"${j%.json}.stderr.txt"
  runtime_rc=$?
  python3 - "$j" "$t" <<'PY'
import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: d = {}
open(sys.argv[2], "w").write(d.get("result", "") or "")
u = d.get("usage", {}) or {}
tin = (u.get("input_tokens", 0) or 0) + (u.get("cache_creation_input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0)
print(f"{d.get('num_turns','?')}|{tin}|{u.get('output_tokens',0)}|{round(d.get('total_cost_usd') or 0, 3)}")
PY
  parse_rc=$?
  [ "$parse_rc" -eq 0 ] || return "$parse_rc"
  return "$runtime_rc"
}

_loop_run_codex() {
  local pf=$1 repo=$2 to=$3 maxturns=$4 j=$5 t=$6
  local bin="${CODEX_BIN:-codex}"
  local agent_home="$(_loop_agent_home)"
  local work_root dispatch_root spec_marker_root core_marker_root codex_sandbox runtime_home runtime_link
  local sandbox_args=() governor runtime_rc parse_rc
  governor=$(_loop_governor_path) || return $?
  work_root=$(CDPATH= cd -- "$repo/.." && pwd)
  dispatch_root=$(dirname -- "$(_loop_jobs_path)")
  spec_marker_root="$agent_home/.spec-grounding"
  core_marker_root="$agent_home/.core-grounding"
  mkdir -p "$dispatch_root" "$spec_marker_root" "$core_marker_root"
  # Fleet resolves Codex liveness through a deterministic worktree-local pointer.
  # Drill may use an isolated CODEX_HOME to avoid touching runtime-owned config;
  # project only that path, never its contents, into the disposable fixture.
  runtime_home="${CODEX_HOME:-$HOME/.codex}"
  runtime_link="$repo/.dispatch/codex-home"
  mkdir -p "$(dirname -- "$runtime_link")"
  if [ -L "$runtime_link" ]; then
    ln -sfn "$runtime_home" "$runtime_link"
  elif [ ! -e "$runtime_link" ]; then
    ln -s "$runtime_home" "$runtime_link"
  fi
  codex_sandbox="${LOOP_CODEX_SANDBOX:-workspace-write}"
  case "$codex_sandbox" in
    danger-full-access)
      # Disposable drill fixtures exercise git refs/worktree creation. Codex's
      # workspace-write sandbox protects .git even under --add-dir, so drill may
      # opt into this mode explicitly without changing other loop defaults.
      sandbox_args=(--sandbox danger-full-access)
      ;;
    workspace-write)
      # `--add-dir` grants the non-git writable roots needed by normal loops.
      sandbox_args=(--sandbox workspace-write --add-dir "$work_root" --add-dir "$dispatch_root" \
        --add-dir "$spec_marker_root" --add-dir "$core_marker_root")
      ;;
    *)
      printf 'unsupported LOOP_CODEX_SANDBOX=%s\n' "$codex_sandbox" >&2
      return 64
      ;;
  esac
  ( cd "$repo" && AGENT_HOME="$agent_home" AGENT_SESSION_ROLE=worker python3 "$governor" run --class loop -- timeout "$to" "$bin" exec --cd "$repo" \
      "${sandbox_args[@]}" \
      --skip-git-repo-check --json - < "$pf" ) \
      > "$j" 2>"${j%.json}.stderr.txt"
  runtime_rc=$?
  python3 - "$j" "$t" <<'PY'
import json, sys
texts = []; tin = tout = turns = 0; failed = False
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line: continue
    try: e = json.loads(line)
    except Exception: continue
    et = e.get("type")
    if et == "item.completed":
        it = e.get("item") or {}
        if it.get("type") == "agent_message" and it.get("text"):
            texts.append(it["text"])
    elif et == "turn.completed":
        turns += 1
        u = e.get("usage") or {}
        tin += (u.get("input_tokens", 0) or 0)
        tout += (u.get("output_tokens", 0) or 0)
    elif et == "turn.failed":
        failed = True
open(sys.argv[2], "w").write("\n".join(texts))
print(f"{turns or '?'}|{tin}|{tout}|0")
if failed:
    raise SystemExit(70)
PY
  parse_rc=$?
  [ "$parse_rc" -eq 0 ] || return "$parse_rc"
  return "$runtime_rc"
}

_loop_run_opencode() {
  local pf=$1 repo=$2 to=$3 maxturns=$4 j=$5 t=$6
  local bin="${OPENCODE_BIN:-opencode}"
  local agent_home="$(_loop_agent_home)"
  local governor runtime_rc parse_rc
  governor=$(_loop_governor_path) || return $?
  command -v "$bin" >/dev/null 2>&1 || bin="$HOME/.opencode/bin/opencode"
  local model_arg=(); [ -n "${OPENCODE_LOOP_MODEL:-}" ] && model_arg=(-m "$OPENCODE_LOOP_MODEL")
  ( cd "$repo" && AGENT_HOME="$agent_home" AGENT_SESSION_ROLE=worker python3 "$governor" run --class loop -- timeout "$to" "$bin" run --dir "$repo" --format json "${model_arg[@]}" "$(cat "$pf")" ) \
      > "$j" 2>"${j%.json}.stderr.txt"
  runtime_rc=$?
  python3 - "$j" "$t" <<'PY'
import json, sys
parts = {}; tin = tout = turns = 0; cost = 0.0
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line: continue
    try: e = json.loads(line)
    except Exception: continue
    et = e.get("type"); part = e.get("part") or {}
    if et == "text":
        pid = part.get("id")
        if pid and isinstance(part.get("text"), str):
            parts[pid] = part["text"]   # keep latest snapshot per part id (dedupe streaming)
    elif et == "step_finish":
        turns += 1
        tk = part.get("tokens") or {}
        tin += (tk.get("input", 0) or 0)
        tout += (tk.get("output", 0) or 0)
        cost += (part.get("cost", 0) or 0)
open(sys.argv[2], "w").write("\n".join(parts.values()))
print(f"{turns or '?'}|{tin}|{tout}|{round(cost, 3)}")
PY
  parse_rc=$?
  [ "$parse_rc" -eq 0 ] || return "$parse_rc"
  return "$runtime_rc"
}
