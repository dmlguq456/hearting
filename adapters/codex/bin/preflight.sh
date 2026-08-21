#!/usr/bin/env sh
set -eu

READLINK_F_AVAILABLE=0
if command -v readlink >/dev/null 2>&1 && readlink_probe=$(readlink -f / 2>/dev/null) && [ "$readlink_probe" = / ]; then READLINK_F_AVAILABLE=1; fi
canonical_existing_path() {
  path=$1; [ -n "$path" ] || return 1; [ -e "$path" ] || [ -L "$path" ] || return 1
  if [ "$READLINK_F_AVAILABLE" -eq 1 ] && canonical=$(readlink -f "$path" 2>/dev/null); then
    [ -n "$canonical" ] && [ -e "$canonical" ] || return 1; printf '%s\n' "$canonical"; return 0
  fi
  if [ -d "$path" ]; then
    if physical_dir=$(CDPATH= cd -P "$path" 2>/dev/null && pwd -P); then printf '%s\n' "$physical_dir"; return 0; fi
    return 1
  fi
  [ -L "$path" ] && return 1
  path_dir=$(dirname "$path"); path_base=$(basename "$path")
  if physical_dir=$(CDPATH= cd -P "$path_dir" 2>/dev/null && pwd -P) && [ -e "$physical_dir/$path_base" ]; then printf '%s/%s\n' "$physical_dir" "$path_base"; return 0; fi
  return 1
}
is_harness_source_root() {
  candidate=$1
  if canonical_candidate=$(canonical_existing_path "$candidate" 2>/dev/null); then :; else return 1; fi
  [ -d "$canonical_candidate" ] && [ -f "$canonical_candidate/core/CORE.md" ] || return 1
  [ -f "$canonical_candidate/adapters/codex/bin/preflight.sh" ] && [ -x "$canonical_candidate/adapters/codex/bin/preflight.sh" ] || return 1
  [ -f "$canonical_candidate/adapters/codex/utilities/agent-home.sh" ] && [ -x "$canonical_candidate/adapters/codex/utilities/agent-home.sh" ] || return 1
  [ -f "$canonical_candidate/utilities/artifact-root.sh" ] || return 1
  [ -d "$canonical_candidate/roles" ] && [ -d "$canonical_candidate/capabilities" ] || return 1
  expected_hook=$canonical_candidate/hooks/core-first-guard.sh; [ -f "$expected_hook" ] && [ -x "$expected_hook" ] || return 1
  if canonical_hook=$(canonical_existing_path "$expected_hook" 2>/dev/null); then :; else return 1; fi
  [ "$canonical_hook" = "$expected_hook" ] || return 1
  printf '%s\n' "$canonical_candidate"
}
typed_root_refusal() { printf 'check=failed\nreason=harness-source-root-unresolved\n' >&2; exit 69; }
if script_parent=$(dirname "$0") && SCRIPT_DIR=$(CDPATH= cd -P "$script_parent" 2>/dev/null && pwd -P); then :; else typed_root_refusal; fi
if SELF_REAL=$(canonical_existing_path "$0" 2>/dev/null); then :; else typed_root_refusal; fi
resolve_source_root() {
  git_candidate=""
  if command -v git >/dev/null 2>&1 && git_root=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null) && git_root=$(canonical_existing_path "$git_root" 2>/dev/null); then git_candidate=$git_root; fi
  relative_candidate=""
  if relative_candidate=$(CDPATH= cd -P "$SCRIPT_DIR/../../.." 2>/dev/null && pwd -P) && relative_candidate=$(canonical_existing_path "$relative_candidate" 2>/dev/null); then :; else relative_candidate=""; fi
  for candidate in "$git_candidate" "$relative_candidate" "${AGENT_HOME:-}"; do
    [ -n "$candidate" ] || continue
    if accepted=$(is_harness_source_root "$candidate" 2>/dev/null); then printf '%s\n' "$accepted"; return 0; fi
  done
  record=${CODEX_HOME:-${HOME:-}/.codex}/.harness/activation.json
  if [ -f "$record" ] && command -v python3 >/dev/null 2>&1; then
    if activation_candidate=$(python3 - "$record" codex 2>/dev/null <<'PY'
import json, os, sys
with open(sys.argv[1], encoding="utf-8") as h: data=json.load(h)
if data.get("runtime") != sys.argv[2]: raise SystemExit(1)
root=data["active_root"] if "active_root" in data else data.get("source_root")
if not isinstance(root, str) or not root or not os.path.isabs(root): raise SystemExit(1)
if data.get("mode") == "packaged" and not data.get("activated_projection_digest"): raise SystemExit(1)
print(root)
PY
    ); then
      if [ -n "$activation_candidate" ] && accepted=$(is_harness_source_root "$activation_candidate" 2>/dev/null); then printf '%s\n' "$accepted"; return 0; fi
    fi
  fi
  adjacent_resolver=$SCRIPT_DIR/../utilities/agent-home.sh
  if [ -x "$adjacent_resolver" ] && resolver_value=$("$adjacent_resolver" 2>/dev/null); then
    if [ -n "$resolver_value" ] && accepted=$(is_harness_source_root "$resolver_value" 2>/dev/null); then printf '%s\n' "$accepted"; return 0; fi
  fi
  for candidate in "${CODEX_HOME:-${HOME:-}/.codex}/hearting" "${HOME:-}/hearting" "${HOME:-}/agent_setting"; do
    if accepted=$(is_harness_source_root "$candidate" 2>/dev/null); then printf '%s\n' "$accepted"; return 0; fi
  done
  return 1
}
if ROOT=$(resolve_source_root 2>/dev/null); then :; else typed_root_refusal; fi

checked_guard_target() {
  relative_guard=$1
  case "$relative_guard" in
    hooks/git-state-guard.sh|hooks/core-first-guard.sh|hooks/artifact-guard.sh|hooks/builtin-memory-guard.sh|hooks/material-route-guard.py|hooks/worktree-path-guard.sh) :;;
    *) printf 'check=failed\nreason=guard-target-unresolved\n' >&2; exit 69;;
  esac
  expected=$ROOT/$relative_guard
  if [ ! -f "$expected" ] || [ ! -x "$expected" ]; then printf 'check=failed\nreason=guard-target-unresolved\n' >&2; exit 69; fi
  if target=$(canonical_existing_path "$expected" 2>/dev/null); then :; else printf 'check=failed\nreason=guard-target-unresolved\n' >&2; exit 69; fi
  case "$relative_guard" in
    hooks/*) name=${relative_guard#hooks/}; expected=$ROOT/hooks/$name;;
  esac
  if [ "$target" != "$expected" ]; then printf 'check=failed\nreason=guard-target-self-reference\n' >&2; exit 69; fi
  printf '%s\n' "$target"
}
run_guard() {
  relative_guard=$1; shift
  target=$(checked_guard_target "$relative_guard")
  case "$relative_guard" in
    hooks/material-route-guard.py) exec python3 "$target" --agent-home "$AGENT_ROOT" "$@";;
    hooks/worktree-path-guard.sh) exec "$target" "$@";;
    *) "$target" "$@";;
  esac
}

agent_home() {
  if [ -n "${AGENT_HOME:-}" ] && [ -f "$AGENT_HOME/core/CORE.md" ]; then
    printf '%s\n' "$AGENT_HOME"
    return
  fi
  if candidate=$("$ROOT/adapters/codex/utilities/agent-home.sh" 2>/dev/null); then :; else candidate=""; fi
  if [ -n "$candidate" ] && [ -f "$candidate/core/CORE.md" ]; then
    printf '%s\n' "$candidate"
  else
    # A standalone, not-yet-installed checkout remains usable. A linked
    # feature worktree does not reach this branch when the canonical
    # $HOME/hearting, legacy $HOME/agent_setting, or the Codex runtime pointer is available.
    printf '%s\n' "$ROOT"
  fi
}

AGENT_ROOT=$(agent_home)

is_worker_session() {
  [ "${AGENT_SESSION_ROLE:-}" = "worker" ] \
    || [ "${AGENT_DISPATCH_CHILD:-}" = "1" ] \
    || [ -n "${AGENT_DISPATCH_DEPTH:-}" ] \
    || [ -n "${OPENCODE_DISPATCH_SLUG:-}" ] \
    || [ "${FLEET_TITLE_REFRESH:-}" = "1" ] \
    || [ "${MEM_DISTILL:-}" = "1" ]
}

# SD-45/round_1 finding 4: the guard identity default falls back to the
# canonical dispatch attempt id, never a shared literal. `${x:-default}`
# collapses both an unset and an empty AGENT_DISPATCH_ATTEMPT_ID onto the
# `codex` default identically (POSIX), so the one case that must never
# silently pass is a worker session that lands on that shared default —
# whether the variable was never exported or was exported empty. An
# interactive, non-worker session legitimately keeps the `codex` default.
guard_identity_hard_fail_if_worker() {
  sid_value=$1
  if [ "$sid_value" = codex ] && is_worker_session; then
    printf 'check=failed\nreason=guard-identity-unavailable\n' >&2
    exit 65
  fi
}

usage() {
  cat <<'EOF'
usage: preflight.sh write <file> [session-id] [turn-id]
       preflight.sh read <file> [session-id]
       preflight.sh route <capability> [cwd] [session-id] [mode] [intensity]
       preflight.sh capability <name> [cwd] [session-id]
       preflight.sh skill <name> [cwd] [session-id]
       preflight.sh session-end [cwd] [session-id]
       preflight.sh prompt-signal [cwd] [session-id]
       preflight.sh turn-nudge [cwd] [session-id]
       preflight.sh token-budget [cwd] [session-id] [kv|json|hook]
       preflight.sh memory [cwd]
       preflight.sh candidates <prompt> <cwd> <session-id> [turn-id]
       preflight.sh recall <query> [cwd] [session-id]
       preflight.sh recall-gate <cwd> (--decision recall|skip --reason <reason> [--query <query>] | --outcome applied|miss --gate-id <id>) [options]
       preflight.sh briefing [cwd]
       preflight.sh status [cwd] [session-id]
       preflight.sh permissions
       preflight.sh tui-config
       preflight.sh managed-entry [--check] --codex-home <private-dir> --state-dir <private-dir> --workspace <dir> [--jobs <jobs.log>] [-- client-args...]
       preflight.sh subagent-info [--check]
       preflight.sh headless [--check] [--require-hook-trust] <worktree>
       preflight.sh nested-headless --parent-harness <h> --parent-transport <t> --parent-sandbox <s> --child-harness <h> --launch-authority <authority> --worktree <path> [--prospective-standard-owner --jobs <canonical-jobs.log>] [--user-disabled] [--json]
       preflight.sh dispatch-readiness --worktree <path> --jobs <canonical-jobs.log> --owner-harness <h>... --child-harness <h>... --output <evidence.json>
       preflight.sh broker <status|stop> --jobs <jobs.log> [--root <broker-root>]  # legacy drain only
       preflight.sh dispatch [--dry-run|--register|--start] [--require-hook-trust] --worktree <path> --slug <slug> --capability <name> --capability-mode <mode> [--worker-mode <family/mode>] --qa <level> [--intensity <level>] [--dispatch-depth 1|2] [--parent <slug>] [--worker-type owner|stage|review|support] [--unit <unit>] [--assigned-contract <capability>] [--owner <capability>] (--model-profile <deep|balanced-deep|light|mini> [--model-role <role>]|--model-role <role>|--model <model> --reasoning <effort>|--inherit-model-settings) [--prompt-file <file>|--prompt-text <text>] [--jobs <jobs.log>]
       preflight.sh dispatch-owner [--adapter <harness>] [--dry-run|--register|--start] --worktree <path> --slug <slug> --capability <name> --capability-mode <mode> --qa <level> --intensity <level> --dispatch-depth 1 --worker-type owner --assigned-contract <capability> --owner <capability> --model-profile <deep|balanced-deep|light> [--prompt-file <file>|--prompt-text <text>] [--jobs <jobs.log>]
       preflight.sh dispatch-chain --route <route.json> --node <id> --slug <slug> --parent <slug> [--capability-mode <mode>] [--worker-mode <family/mode>] [--model-role <role>] [--capacity-model <id> --capacity-reasoning|--capacity-effort|--capacity-variant <level>] [--progress-window-seconds N --watchdog-max-windows M] [--dry-run|--register|--start]
       preflight.sh dispatch-session-chain <check|register|start|run> --manifest <chain.json> --parent <slug> [--jobs <jobs.log>] [--max-seconds N]
       preflight.sh dispatch-batch --route <route.json> --parallel-group <id> --slug-prefix <slug> --parent <slug> --action dry-run|start [--qa <level>] [--jobs <jobs.log>] [--prompt-text <text>] [--allow-degraded-independence]
       preflight.sh stage-heartbeat --attempt-id <id> --route-id <id> --route-node <id> --jobs <jobs.log> --phase <phase> --kind <kind> --evidence <ref>
       preflight.sh dispatch-wait --attempt-id <id> [--interval <seconds>] [--max <seconds>]
       preflight.sh dispatch-current --jobs <jobs.log> (--session <id>|--route <id>|--node <id>|--attempt <id>|--job <slug>) [--all]
       preflight.sh dispatch-reconcile --jobs <jobs.log> (--session <id>|--route <id>|--node <id>|--attempt <id>|--job <slug>) [--apply] [--cancel-receiptless-namespace  # exact --attempt only]
       preflight.sh qa-policy <quick|light|standard|thorough|adversarial> [code|research|doc|general]
       preflight.sh liveness [jobs.log] [--session <id>|--route <id>|--node <id>|--attempt <id>|--job <slug>] [--all]
       preflight.sh harvest [--jobs <jobs.log>] [--reconcile-local <legacy-jobs.log>] [--attempt-id <id>|--slug <slug>|--worktree <path>] [--status open|done|all] [--mark-done]
       preflight.sh worktree-cleanup [--check|--apply] (--worktree <path>|--all-eligible [--repo <path>]) [--integration-ref <ref>] [--jobs <jobs.log>]
       preflight.sh mcp [--check]
       preflight.sh worklog [cwd]
       preflight.sh artifact-sink <'--check'|'emit' ...>
       preflight.sh ui-info
       preflight.sh loop-info <oncall|note|study|drill|runtime-watch>
       preflight.sh claim-verify [--check] <claim> [--out <file>]
       preflight.sh browser-fetch [--check] <url> [--out <dir>]
       preflight.sh data-script [--check] <script.py> [-- args...]
       preflight.sh figure-gen [--check] <script.py> [-- args...]
       preflight.sh figure-gen --verify-report <manifest.json> <report.md>
       preflight.sh pdf-extract [--check] <file.pdf> [--out <file.txt>]
       preflight.sh web-image-search [--check] <query> [--max-results N] [--out <file>]
       preflight.sh verification-runner [--check] [--timeout seconds] -- <command> [args...]
       preflight.sh design <file>
       preflight.sh visual-harness [file.html]
       preflight.sh convert [pdf|bundle|pptx] <file.html> [out]
       preflight.sh distill-delta <session-id>
       preflight.sh distill-propose <session-id> [cwd]
       preflight.sh role <portable-role|role-profile|pipeline-stage>
       preflight.sh capability-info <capability>
       preflight.sh mode-info <family/mode>
       preflight.sh runtime-projection [--require-hook-trust]
       preflight.sh doctor [--runtime|--runtime-strict]

Runs portable checks that Codex can call without consuming Claude hook JSON or
settings.json.
EOF
}

doctor_check() {
  name=$1
  shift
  if "$@" >/dev/null 2>&1; then
    printf 'check=%s:ok\n' "$name"
    return 0
  fi
  printf 'check=%s:failed\n' "$name"
  return 1
}

doctor_boundary() {
  lock="${TMPDIR:-/tmp}/agent-setting-adaptation-boundary.lock"
  tries=0
  while ! mkdir "$lock" 2>/dev/null; do
    # a SIGKILLed holder (e.g. an outer CLI timeout) leaks the sentinel and
    # would wedge every later doctor run globally -- reclaim when the
    # recorded owner is gone or never recorded its pid.
    owner="$(cat "$lock/pid" 2>/dev/null || true)"
    if [ -z "$owner" ] || ! kill -0 "$owner" 2>/dev/null; then
      rm -rf "$lock" 2>/dev/null || true
      continue
    fi
    tries=$((tries + 1))
    if [ "$tries" -ge 100 ]; then
      return 1
    fi
    sleep 0.1
  done
  printf '%s' "$$" > "$lock/pid"
  trap 'rm -rf "$lock" 2>/dev/null || true' EXIT HUP INT TERM
  "$ROOT/tools/check-adaptation-boundary.sh"
  rc=$?
  rm -rf "$lock" 2>/dev/null || true
  trap - EXIT HUP INT TERM
  return "$rc"
}

doctor() {
  runtime_check=0
  require_hook_trust=0
  case "${1:-}" in
    "")
      ;;
    --runtime)
      runtime_check=1
      ;;
    --runtime-strict)
      runtime_check=1
      require_hook_trust=1
      ;;
    *)
      echo "codex preflight: unknown doctor option: $1" >&2
      exit 64
      ;;
  esac

  rc=0
  printf 'adapter=codex\n'
  printf 'runtime_surface=adapter-readiness-doctor\n'
  printf 'agent_home=%s\n' "$AGENT_ROOT"
  if command -v codex >/dev/null 2>&1; then
    printf 'runtime_cli=available\n'
  else
    printf 'runtime_cli=unavailable\n'
  fi

  doctor_check generated-projections python3 "$ROOT/tools/generate.py" --check || rc=1
  doctor_check native-subagents "$0" subagent-info --check || rc=1
  doctor_check hook-bridges python3 -c 'import pathlib, sys; [compile(pathlib.Path(p).read_text(encoding="utf-8"), p, "exec") for p in sys.argv[1:]]' \
    "$ROOT/adapters/codex/hooks/sessionstart-lifecycle.py" \
    "$ROOT/adapters/codex/hooks/sessionend-lifecycle.py" \
    "$ROOT/adapters/codex/hooks/stop-lifecycle.py" \
    "$ROOT/adapters/codex/hooks/userprompt-lifecycle.py" \
    "$ROOT/adapters/codex/hooks/permissionrequest-lifecycle.py" \
    "$ROOT/adapters/codex/hooks/posttooluse-interaction-clear.py" \
    "$ROOT/adapters/codex/hooks/pretooluse-write-guard.py" \
    "$ROOT/adapters/codex/hooks/posttooluse-design-check.py" \
    "$ROOT/adapters/codex/hooks/posttooluse-read-marker.py" \
    "$ROOT/adapters/codex/hooks/worker-state-compact.py" || rc=1
  doctor_check token-budget python3 "$ROOT/utilities/token-budget.py" \
    --adapter portable --active-context-tokens 1 --context-window 100 --format json || rc=1
  doctor_check token-budget-experiment python3 "$ROOT/utilities/token-budget-experiment.py" --help || rc=1
  doctor_check adaptation-boundary doctor_boundary || rc=1
  if [ "$runtime_check" -eq 1 ]; then
    if [ "$require_hook_trust" -eq 1 ]; then
      doctor_check runtime-projection env CODEX_REQUIRE_HOOK_TRUST=1 "$ROOT/adapters/codex/bin/check-runtime-projection.sh" || rc=1
    else
      doctor_check runtime-projection "$ROOT/adapters/codex/bin/check-runtime-projection.sh" || rc=1
    fi
  else
    printf 'check=runtime-projection:skipped\n'
    printf 'runtime_projection_hint=adapters/codex/bin/preflight.sh doctor --runtime\n'
  fi

  if [ "$rc" -eq 0 ]; then
    printf 'status=ok\n'
  else
    printf 'status=failed\n'
  fi
  return "$rc"
}

codex_runtime_projection_check() {
  codex_home=${CODEX_HOME:-${HOME:-}/.codex}
  if [ -z "$codex_home" ]; then
    printf 'check=failed\nreason=codex-home-unset\n'
    return 69
  fi
  AGENT_HOME="$AGENT_ROOT" CODEX_RUNTIME_PROJECTION_FAST=1 CODEX_RUNTIME_PROJECTION_SKIP_CLI_DISCOVERY=1 CODEX_REQUIRE_HOOK_TRUST="${CODEX_REQUIRE_HOOK_TRUST:-0}" \
    "$ROOT/adapters/codex/bin/check-runtime-projection.sh" || return $?
  printf 'runtime_projection=ok\ncodex_home=%s\n' "$codex_home"
  return 0
}

cmd=${1:-}
case "$cmd" in
  doctor)
    [ "$#" -le 2 ] || { echo "codex preflight: doctor accepts at most one option" >&2; exit 64; }
    doctor "${2:-}"
    ;;
  runtime-projection)
    case "${2:-}" in
      "")
        [ "$#" -eq 1 ] || { echo "codex preflight: runtime-projection accepts at most one option" >&2; exit 64; }
        AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/codex/bin/check-runtime-projection.sh"
        ;;
      --require-hook-trust)
        [ "$#" -eq 2 ] || { echo "codex preflight: runtime-projection accepts at most one option" >&2; exit 64; }
        AGENT_HOME="$AGENT_ROOT" CODEX_REQUIRE_HOOK_TRUST=1 "$ROOT/adapters/codex/bin/check-runtime-projection.sh"
        ;;
      *)
        echo "codex preflight: unknown runtime-projection option: $2" >&2
        exit 64
        ;;
    esac
    ;;
  write)
    [ "$#" -ge 2 ] || { echo "codex preflight: write requires a file path" >&2; exit 64; }
    file=$2
    sid=${3:-${AGENT_DISPATCH_ATTEMPT_ID:-codex}}
    guard_identity_hard_fail_if_worker "$sid"
    turn=${4:-}
    if [ "${AGENT_DISPATCH_STAGE_AUTHORITY:-1}" = "0" ]; then
      [ -n "${AGENT_WORKER_STATE_LEDGER:-}" ] && [ -n "${AGENT_DISPATCH_ATTEMPT_ID:-}" ] || {
        echo "worker sub-session ledger binding missing" >&2; exit 65;
      }
      python3 "$ROOT/utilities/worker-state-ledger.py" guard-edit \
        --path "$AGENT_WORKER_STATE_LEDGER" \
        --attempt-id "$AGENT_DISPATCH_ATTEMPT_ID" --file "$file"
    fi
    run_guard hooks/git-state-guard.sh --file "$file"
    run_guard hooks/core-first-guard.sh --file "$file" --session "$sid"
    run_guard hooks/artifact-guard.sh --file "$file" --session "$sid"
    run_guard hooks/builtin-memory-guard.sh --file "$file"
    # Spec read gate, fitted to Codex's interception point. Claude hard-denies the
    # ungrounded autopilot-code/spec *Skill* (PreToolUse[Skill]); Codex has no
    # skill-invocation event (skills are implicitly selected), so the equivalent
    # hard gate is applied where Codex *can* intercept — the write of a
    # spec-changing artifact (plans/* or a spec blueprint). Same portable invariant
    # (no spec-changing work without a current prd.md read marker), same shared
    # gate script, same per-cwd marker written by posttooluse-read-marker. Editing
    # an existing artifact while ungrounded is denied; creating the first prd.md is
    # not (no prd.md yet → not spec-backed → gate passes, artifact-order still runs).
    # SD-45: hand the route record to the gate as an additional evidence path
    # (round_1-corrected plan.md Step 1.4). Only appended when set — the gate
    # itself falls through to the marker when no route is resolved.
    set -- ; [ -n "${AGENT_ROUTE_FILE:-}" ] && set -- --route "$AGENT_ROUTE_FILE"
    case "$file" in
      */.agent_reports/plans/*|*/.claude_reports/plans/*)
        "$ROOT/hooks/spec-skill-gate.sh" --skill autopilot-code --cwd "$(dirname "$file")" --session "$sid" "$@" ;;
      */.agent_reports/spec/prd.md|*/.claude_reports/spec/prd.md|\
      */.agent_reports/spec/stack.md|*/.claude_reports/spec/stack.md|\
      */.agent_reports/spec/stack_decision.md|*/.claude_reports/spec/stack_decision.md|\
      */.agent_reports/spec/ship.md|*/.claude_reports/spec/ship.md|\
      */.agent_reports/spec/api_contract.md|*/.claude_reports/spec/api_contract.md|\
      */.agent_reports/spec/data_model.md|*/.claude_reports/spec/data_model.md|\
      */.agent_reports/spec/ui_flow.md|*/.claude_reports/spec/ui_flow.md)
        "$ROOT/hooks/spec-skill-gate.sh" --skill autopilot-spec --cwd "$(dirname "$file")" --session "$sid" "$@" ;;
    esac
    if [ -n "$turn" ]; then
      "$0" material-route check --tool Write --file "$file" --cwd "$(dirname "$file")" --session "$sid" --turn "$turn"
    else
      "$0" material-route check --tool Write --file "$file" --cwd "$(dirname "$file")" --session "$sid"
    fi
    ;;
  read)
    [ "$#" -ge 2 ] || { echo "codex preflight: read requires a file path" >&2; exit 64; }
    file=$2
    sid=${3:-${AGENT_DISPATCH_ATTEMPT_ID:-codex}}
    guard_identity_hard_fail_if_worker "$sid"
    "$ROOT/hooks/core-read-marker.sh" --file "$file" --session "$sid"
    "$ROOT/hooks/spec-read-marker.sh" --file "$file" --session "$sid"
    ;;
  route)
    if [ "${2:-}" = "--capability" ]; then
      shift
      exec python3 "$ROOT/utilities/capability-route.py" compile "$@"
    fi
    [ "$#" -ge 2 ] || { echo "codex preflight: route requires a capability name" >&2; exit 64; }
    name=$2
    cwd=${3:-$PWD}
    sid=${4:-codex}
    mode=${5:-}
    intensity=${6:-}
    [ "$#" -le 6 ] || { echo "codex preflight: route accepts at most capability, cwd, session-id, mode, and intensity" >&2; exit 64; }
    "$0" status "$cwd" "$sid"
    "$0" prompt-signal "$cwd" "$sid"
    "$0" capability-info "$name"
    "$0" capability "$name" "$cwd" "$sid"
    # Codex has no native Skill-invocation hook.  Realize the portable inline
    # capability-grounding contract at its explicit router instead (F-43).
    # Display evidence is best-effort and never turns a successful route into
    # a failure; worker sessions carry their identity in jobs.log/route records.
    if [ "$sid" != codex ] && ! is_worker_session; then
      set -- record --sid "$sid" --capability "$name" --agent-home "$AGENT_ROOT" --cwd "$cwd"
      [ -z "$mode" ] || set -- "$@" --mode "$mode"
      [ -z "$intensity" ] || set -- "$@" --intensity "$intensity"
      sh "$ROOT/utilities/capability-grounding.sh" "$@" >/dev/null 2>&1 || :
    fi
    ;;
  material-route)
    [ "$#" -ge 2 ] || { echo "codex preflight: material-route requires an action" >&2; exit 64; }
    shift
    run_guard hooks/material-route-guard.py "$@"
    ;;
  worktree-path)
    shift
    run_guard hooks/worktree-path-guard.sh "$@"
    ;;
  worker-route)
    shift
    exec python3 "$ROOT/utilities/worker-route-guard.py" validate "$@"
    ;;
  dispatch-node)
    shift
    exec python3 "$ROOT/utilities/dispatch-node.py" --adapter codex "$@"
    ;;
  capability|skill)
    [ "$#" -ge 2 ] || { echo "codex preflight: $cmd requires a capability name" >&2; exit 64; }
    name=$2
    cwd=${3:-$PWD}
    sid=${4:-${AGENT_DISPATCH_ATTEMPT_ID:-codex}}
    guard_identity_hard_fail_if_worker "$sid"
    if ! "$ROOT/adapters/codex/bin/capability-map.sh" "$name" >/dev/null 2>/dev/null; then
      printf 'check=failed\nreason=unknown-capability\ncapability=%s\n' "$name"
      exit 64
    fi
    set -- ; [ -n "${AGENT_ROUTE_FILE:-}" ] && set -- --route "$AGENT_ROUTE_FILE"
    "$ROOT/hooks/spec-skill-gate.sh" --skill "$name" --cwd "$cwd" --session "$sid" "$@"
    ;;
  session-end)
    cwd=${2:-$PWD}
    sid=${3:-codex}
    # D-42 defense in depth: worker exit owns no sync/curator lifecycle.
    is_worker_session && exit 0
    # SessionEnd sync contract (core/MEMORY.md §7): local sync is the default.
    # Pass the user's MEM_SYNC_REMOTE / deprecated MEM_DUMP_PUSH environment
    # unchanged; the adapter never opts the session into remote exchange and
    # the compatibility flag never means dump push. Preserve the typed sync
    # exit after the bounded curator fallback has had its chance to run.
    sync_status=0
    (cd "$cwd" && AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/tools/memory/mem.py" sync --json >/dev/null) || sync_status=$?
    if [ "$sync_status" -ne 0 ]; then
      printf 'codex preflight: session-end memory sync status=%s; continuing bounded curator fallback\n' "$sync_status" >&2
    fi
    # Automatic session-end distillation is enabled: the codex exec read-only
    # sandbox was verified tool-free (adapters/codex/ADAPTATION.md Distillation
    # Boundary), so default the worker to apply mode. Opt out with
    # CODEX_DISTILL_ENABLE=0. session-end runs the *curate* (deep) tier —
    # snapshot-grounded prune/merge/graduate via the shared curate-snapshot +
    # whitelist applier (D-30/D-32); turn-nudge runs increment. Synchronous so the
    # headless codex exec captures memory before it exits (curate timeout-bounded).
    curator_status=0
    AGENT_HOME="$AGENT_ROOT" \
      CODEX_DISTILL_ENABLE="${CODEX_DISTILL_ENABLE:-1}" \
      CODEX_DISTILL_APPLY="${CODEX_DISTILL_APPLY:-1}" \
      CODEX_DISTILL_CONTRACT_ACCEPTED="${CODEX_DISTILL_CONTRACT_ACCEPTED:-1}" \
      "$ROOT/adapters/codex/bin/distill-worker.sh" "$sid" "$cwd" curate || curator_status=$?
    [ "$sync_status" -eq 0 ] || exit "$sync_status"
    exit "$curator_status"
    ;;
  prompt-signal)
    cwd=${2:-$PWD}
    sid=${3:-codex}
    status=$(AGENT_ADAPTER=codex "$ROOT/utilities/harness-status.sh" "$cwd" "$sid")
    artifact_root_kind=$(printf '%s\n' "$status" | awk -F= '$1=="artifact_root_kind"{print $2; exit}')
    git_operation=$(printf '%s\n' "$status" | awk -F= '$1=="git_operation"{print $2; exit}')
    git_dirty_tracked=$(printf '%s\n' "$status" | awk -F= '$1=="git_dirty_tracked"{print $2; exit}')
    git_untracked=$(printf '%s\n' "$status" | awk -F= '$1=="git_untracked"{print $2; exit}')
    git_extra_worktrees=$(printf '%s\n' "$status" | awk -F= '$1=="git_extra_worktrees"{print $2; exit}')
    git_branch_done=$(printf '%s\n' "$status" | awk -F= '$1=="git_branch_done"{print $2; exit}')
    headless_open_jobs=$(printf '%s\n' "$status" | awk -F= '$1=="headless_open_jobs"{print $2; exit}')
    printf 'adapter=codex\n'
    printf 'runtime_surface=codex-userprompt-hook-signal\n'
    printf 'hook_event=UserPromptSubmit\n'
    printf 'hook_scope=runtime-hook\n'
    printf 'artifact_root_kind=%s\n' "${artifact_root_kind:-unknown}"
    printf 'git_operation=%s\n' "${git_operation:-unknown}"
    printf 'git_dirty_tracked=%s\n' "${git_dirty_tracked:-0}"
    printf 'git_untracked=%s\n' "${git_untracked:-0}"
    printf 'git_extra_worktrees=%s\n' "${git_extra_worktrees:-0}"
    printf 'git_branch_done=%s\n' "${git_branch_done:-0}"
    printf 'headless_open_jobs=%s\n' "${headless_open_jobs:-0}"
    printf 'autopilot_route=autopilot-required-for-spec-and-nontrivial-work\n'
    printf 'routing_contract=core/WORKFLOW.md\n'
    printf 'routing_action=read-workflow-and-select-codex-skill\n'
    printf 'capability_entrypoints=codex-native-skills\n'
    printf 'enforced_hooks=structured-write-guards,core-first-guard,posttool-read-markers,posttool-design-check,session-memory,turn-nudge\n'
    printf 'hook_boundary=shell-read-write-targeted-detection-explicit-preflight-fallback\n'
    printf 'shell_fallback=run-preflight-for-ambiguous-shell-io\n'
    ;;
  turn-nudge)
    cwd=${2:-$PWD}
    sid=${3:-codex}
    # Return before creating or advancing any worker turn state (D-42).
    is_worker_session && exit 0
    [ -n "$sid" ] && [ "$sid" != "default" ] || exit 0
    interval=${MEM_NUDGE_INTERVAL:-10}
    case "$interval" in (*[!0-9]*|"") interval=10 ;; esac
    [ "$interval" -gt 0 ] || interval=10
    default_store="$AGENT_ROOT/memory"
    [ -e "$default_store" ] || [ -L "$default_store" ] \
      || default_store="${XDG_DATA_HOME:-$HOME/.local/share}/hearting/memory"
    store=${MEM_STORE:-$default_store}
    mkdir -p "$store" 2>/dev/null || true
    state="$store/.codex-turn-state-$sid"
    counter=0
    if [ -f "$state" ]; then
      counter=$(sed -n '1p' "$state" 2>/dev/null || echo 0)
    fi
    case "$counter" in (*[!0-9]*|"") counter=0 ;; esac
    counter=$((counter + 1))
    if [ "$counter" -ge "$interval" ]; then
      counter=0
      AGENT_HOME="$AGENT_ROOT" \
        CODEX_DISTILL_ENABLE="${CODEX_DISTILL_ENABLE:-1}" \
        CODEX_DISTILL_APPLY="${CODEX_DISTILL_APPLY:-1}" \
        CODEX_DISTILL_CONTRACT_ACCEPTED="${CODEX_DISTILL_CONTRACT_ACCEPTED:-1}" \
        "$ROOT/adapters/codex/bin/distill-worker.sh" "$sid" "$cwd" increment >/dev/null 2>/dev/null || true
    fi
    printf '%s\n' "$counter" > "$state" 2>/dev/null || true
    find "$store" -maxdepth 1 -name '.codex-turn-state-*' -mmin +4320 -delete 2>/dev/null || true
    ;;
  token-budget)
    cwd=${2:-$PWD}
    sid=${3:-codex-hook}
    format=${4:-kv}
    case "$format" in
      kv|json|hook) ;;
      *) echo "codex preflight: token-budget format must be kv, json, or hook" >&2; exit 64 ;;
    esac
    # cwd is intentionally not a rollout selection key: exact session id only.
    # Unknown/degraded signals return success and emit no hook directive.
    : "$cwd"
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/token-budget.py" \
      --adapter codex --session-id "$sid" --format "$format"
    ;;
  memory)
    cwd=${2:-$PWD}
    (cd "$cwd" && AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/tools/memory/mem.py" inject)
    ;;
  candidates)
    [ "$#" -ge 4 ] || { echo "codex preflight: candidates requires prompt, cwd, and session-id" >&2; exit 64; }
    prompt=$2
    cwd=$3
    sid=$4
    turn=${5:-}
    set -- --prompt "$prompt" --cwd "$cwd" --session-id "$sid" --runtime codex --format text
    [ -z "$turn" ] || set -- "$@" --turn-id "$turn"
    AGENT_HOME="$AGENT_ROOT" bash "$ROOT/hooks/mem-recall-inject.sh" "$@"
    ;;
  recall)
    [ "$#" -ge 2 ] || { echo "codex preflight: recall requires a query" >&2; exit 64; }
    query=$2
    cwd=${3:-$PWD}
    (cd "$cwd" && AGENT_HOME="$AGENT_ROOT" MEM_RECALL_RUNTIME=codex \
      "$ROOT/tools/memory/recall.sh" "$query")
    ;;
  recall-gate)
    [ "$#" -ge 3 ] || { echo "codex preflight: recall-gate requires cwd and gate arguments" >&2; exit 64; }
    cwd=$2
    shift 2
    (cd "$cwd" && AGENT_HOME="$AGENT_ROOT" MEM_RECALL_RUNTIME=codex \
      python3 "$ROOT/tools/memory/mem.py" recall-gate "$@")
    ;;
  briefing)
    cwd=${2:-$PWD}
    AGENT_HOME="$AGENT_ROOT" bash "$ROOT/hooks/mem-briefing-inject.sh" --cwd "$cwd" --format text
    ;;
  status)
    cwd=${2:-$PWD}
    sid=${3:-codex}
    AGENT_ADAPTER=codex "$ROOT/utilities/harness-status.sh" "$cwd" "$sid"
    ;;
  permissions)
    cat <<'EOF'
adapter=codex
runtime_surface=codex-native-approval-sandbox
status=native-runtime-config
permission_model=approval-policy+sandbox
approval_surface=codex --ask-for-approval <untrusted|on-request|never>
sandbox_surface=codex --sandbox <read-only|workspace-write|danger-full-access>
config_surface=$CODEX_HOME/config.toml
config_fragment=codex_setting/codex-config/approval-sandbox.toml
claude_allowed_tools=unsupported
guard_contract=preflight-write-hooks-and-explicit-tool-contracts
structured_write_hooks=Write,Edit,MultiEdit,apply_patch,functions.apply_patch
targeted_shell_hooks=Bash,Shell,functions.exec_command
shell_read_write_hooks=targeted-detection
targeted_shell_write_patterns=redirect,tee,touch,cp,mv,rm,install,rsync,dd-of,sed-i
shell_fallback=run-preflight-for-ambiguous-shell-io
fallback=configure-codex-approval-sandbox-and-run-preflight-guards
note=Do not port Claude allowedTools into Codex; use Codex approval/sandbox settings plus adapter preflight guards.
EOF
    ;;
  tui-config)
    [ "$#" -eq 1 ] || { echo "codex preflight: tui-config accepts no arguments" >&2; exit 64; }
    AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/codex/bin/apply-tui-config.sh"
    ;;
  managed-entry)
    shift
    exec python3 "$ROOT/utilities/codex-managed-entry.py" "$@"
    ;;
  headless)
    shift
    check_only=0
    require_hook_trust=0
    worktree=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --check)
          check_only=1
          shift
          ;;
        --require-hook-trust)
          require_hook_trust=1
          shift
          ;;
        --*)
          echo "codex preflight: unknown headless option: $1" >&2
          exit 64
          ;;
        *)
          if [ -z "$worktree" ]; then
            worktree=$1
          else
            echo "codex preflight: headless accepts one worktree path" >&2
            exit 64
          fi
          shift
          ;;
      esac
    done
    cat <<'EOF'
adapter=codex
runtime_surface=codex-exec-headless
status=tool-contract
tool_contract=headless-dispatch
tool_contract_check=adapters/codex/bin/preflight.sh headless --check <worktree>
strict_tool_contract_check=adapters/codex/bin/preflight.sh headless --check --require-hook-trust <worktree>
command_template=codex exec --cd <worktree> --sandbox workspace-write (--model <main-selected-model> -c model_reasoning_effort=<main-selected-reasoning>|inherit) -c approval_policy=never --json -
model_selection_policy=main-orchestrator-must-select-per-job
model_selection_surface=--model-profile <deep|balanced-deep|light|mini> [--model-role <portable-role>]|--model-role <portable-role>|--model <model> --reasoning <effort>|--inherit-model-settings
runtime_projection_requires=hearting,AGENTS.md,hooks.json,native-skills,native-agents,native-modes
runtime_projection_strict_requires=complete-codex-hook-trust
job_registry=<agent-home>/.dispatch/jobs.log (immutable AGENT_DISPATCH_JOBS for descendants)
nested_eligibility_check=adapters/codex/bin/preflight.sh nested-headless --parent-harness <h> --parent-transport <t> --parent-sandbox <s> --child-harness <h> --launch-authority <a> --worktree <path>
prospective_codex_owner_check=adapters/codex/bin/preflight.sh nested-headless --parent-harness codex --parent-transport headless --parent-sandbox workspace-write --child-harness <h> --launch-authority conductor --worktree <path> --prospective-standard-owner --jobs <canonical-jobs.log>
fallback_chain=same-harness-headless,cross-harness-headless,native-subagent,inline
fallback_runner=adapters/codex/bin/preflight.sh dispatch-chain --route <route> --node <node> --dry-run|--register|--start
broker_lifecycle=retired-status-stop-only
launch_authority=conductor
liveness_surface=codex-session-jsonl-mtime
liveness_check=adapters/codex/bin/preflight.sh liveness [jobs.log]
harvest_check=adapters/codex/bin/preflight.sh harvest [--jobs jobs.log] [--attempt-id id|--slug slug] [--mark-done]
dispatch_prompt_contract=portable-typed-worker-bootstrap
worker_bootstrap_source=roles/worker-bootstrap.md+roles/worker-types/<owner|stage|review|support>.md
worker_handoff=artifact,verdict,blocker
dispatch_input_validation=capability-info,capability-mode-catalog,optional-worker-mode-info,owner-mode-axis-consistency,qa-level,intensity-dispatch_depth-parent
worker_startup_signal=wrapper-validated-metadata-or-immutable-route
worker_startup_signal_contract=dispatch-wrapper-validates-before-materializing-prompt; worker rechecks only for safety
physical_project_agents_masking=unsupported-runtime-auto-discovery-may-remain
constraints=main-or-owner-dispatched,max-dispatch-depth-2-for-standard-plus-owner,register-open-job,headless-owner-supervisor-or-managed-gateway-or-interactive-poll-fallback,explicit-capability-mode-qa-intensity-dispatch_depth-parent-parent_sid,transcript-liveness-required
claude_headless=unsupported
fallback=checked-dispatch-chain-or-structured-degradation
EOF
    if [ "$check_only" -eq 0 ]; then
      exit 0
    fi
    [ -n "$worktree" ] || { echo "codex preflight: headless --check requires a worktree path" >&2; exit 64; }
    if [ ! -d "$worktree" ]; then
      printf 'check=failed\nreason=worktree-not-found\nfailure_scope=exact-worktree\ncodex_command=unchecked\nretry_on_isolated_worktree=1\nworktree=%s\n' "$worktree"
      exit 66
    fi
    if ! command -v codex >/dev/null 2>&1; then
      printf 'check=failed\nreason=codex-command-unavailable\nfailure_scope=runtime-global\ncodex_command=unavailable\nretry_on_isolated_worktree=0\nworktree=%s\n' "$worktree"
      exit 69
    fi
    if ! git -C "$worktree" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      printf 'check=failed\nreason=not-a-git-worktree\nfailure_scope=exact-worktree\ncodex_command=ok\nretry_on_isolated_worktree=1\nworktree=%s\n' "$worktree"
      exit 65
    fi
    # The sandboxed runtime mounts <worktree>/.codex, so a file or symlink there
    # kills the spawn with exit 65. dispatch-headless.py already refuses that
    # shape, but only after this probe has already answered `supported` — the
    # readiness synthesis OPERATIONS §5.10 (SD-48) forbids. Refuse it here so the
    # eligibility tuple is honestly `unsupported` before an attempt is burned.
    # Both signals that disable the inner mount sandbox in the wrapper's
    # `effective_runtime_sandbox` are honored, so this never rejects a shape the
    # wrapper itself would have accepted.
    if [ "${CODEX_DISPATCH_SANDBOX_FORCE:-}" != "danger-full-access" ] \
      && [ "${AGENT_DISPATCH_CHILD:-}" != "1" ] \
      && { [ -L "$worktree/.codex" ] || { [ -e "$worktree/.codex" ] && [ ! -d "$worktree/.codex" ]; }; }; then
      printf 'check=failed\nreason=invalid-worktree-codex-mount-target\ndetail=.codex must be a directory while the Codex sandbox is enabled\nfailure_scope=exact-worktree\ncodex_command=ok\nretry_on_isolated_worktree=1\npath=%s\nworktree=%s\n' \
        "$worktree/.codex" "$worktree"
      exit 65
    fi
    if [ "$require_hook_trust" -eq 1 ]; then
      CODEX_REQUIRE_HOOK_TRUST=1 codex_runtime_projection_check
    else
      codex_runtime_projection_check
    fi
    printf 'check=ok\nfailure_scope=none\ncodex_command=ok\nretry_on_isolated_worktree=0\nworktree=%s\n' "$worktree"
    ;;
  nested-headless)
    shift
    python3 "$ROOT/utilities/nested-dispatch-eligibility.py" "$@"
    ;;
  dispatch-readiness)
    shift
    python3 "$ROOT/utilities/dispatch-readiness.py" "$@"
    ;;
  broker)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/dispatch-broker.py" "$@"
    ;;
  dispatch-wait)
    shift
    AGENT_HOME="$AGENT_ROOT" "$ROOT/utilities/dispatch-wait.sh" "$@"
    ;;
  liveness)
    shift
    jobs=${AGENT_DISPATCH_JOBS:-"$AGENT_ROOT/.dispatch/jobs.log"}
    if [ "$#" -gt 0 ] && [ "${1#--}" = "$1" ]; then jobs=$1; shift; fi
    current_jobs=$(mktemp)
    trap 'rm -f "$current_jobs"' EXIT
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/dispatch-registry.py" liveness --jobs "$jobs" "$@" > "$current_jobs" || exit $?
    AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/codex/bin/dispatch-liveness.py" "$current_jobs"
    ;;
  harvest)
    shift
    AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/codex/bin/dispatch-harvest.py" "$@"
    ;;
  worktree-cleanup)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/worktree-cleanup.py" "$@"
    ;;
  dispatch)
    shift
    AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/codex/bin/dispatch-headless.py" "$@"
    ;;
  dispatch-owner)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/dispatch-owner.py" "$@"
    ;;
  dispatch-chain)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/stage-dispatch-fallback.py" "$@"
    ;;
  dispatch-session-chain)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/stage-session-chain.py" "$@"
    ;;
  dispatch-batch)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/dispatch-batch.py" "$@"
    ;;
  stage-heartbeat)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/dispatch-progress.py" heartbeat "$@"
    ;;
  dispatch-current)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/dispatch-registry.py" current "$@"
    ;;
  dispatch-reconcile)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/dispatch-registry.py" reconcile "$@"
    ;;
  qa-policy)
    [ "$#" -ge 2 ] || { echo "codex preflight: qa-policy requires a QA level" >&2; exit 64; }
    level=$2
    track=${3:-general}
    case "$level" in
      quick)
        quality_reviewers="1x-fast-reviewer"
        fact_checker="skip"
        external_adversary="skip"
        max_round="1"
        role_checks="preflight.sh role fast reviewer"
        ;;
      light)
        quality_reviewers="2x-fast-reviewers"
        fact_checker="skip"
        external_adversary="skip"
        max_round="1"
        role_checks="preflight.sh role fast reviewer"
        ;;
      standard)
        quality_reviewers="1x-deep-reviewer+2x-fast-reviewers"
        fact_checker="1x-fast-fact-checker"
        external_adversary="skip"
        max_round="1"
        role_checks="preflight.sh role deep reviewer;preflight.sh role fast reviewer"
        ;;
      thorough)
        quality_reviewers="2x-deep-reviewers+2x-fast-reviewers"
        fact_checker="1x-fast-fact-checker"
        external_adversary="skip"
        max_round="2"
        role_checks="preflight.sh role deep reviewer;preflight.sh role fast reviewer"
        ;;
      adversarial)
        quality_reviewers="2x-deep-reviewers+2x-fast-reviewers"
        fact_checker="1x-fast-fact-checker"
        external_adversary="1x-external-adversary"
        max_round="2+external-1"
        role_checks="preflight.sh role deep reviewer;preflight.sh role fast reviewer;preflight.sh role external adversary"
        ;;
      *)
        echo "codex preflight: unknown QA level: $level" >&2
        exit 64
        ;;
    esac
    case "$track" in
      code)
        fact_checker="skip-code-track"
        ;;
      research|doc|general)
        ;;
      *)
        echo "codex preflight: unknown QA track: $track" >&2
        exit 64
        ;;
    esac
    printf 'adapter=codex\n'
    printf 'runtime_surface=codex-qa-policy\n'
    printf 'source=core/CONVENTIONS.md\n'
    printf 'qa_level=%s\n' "$level"
    printf 'qa_track=%s\n' "$track"
    printf 'quality_reviewers=%s\n' "$quality_reviewers"
    printf 'fact_checker=%s\n' "$fact_checker"
    printf 'external_adversary=%s\n' "$external_adversary"
    printf 'max_round=%s\n' "$max_round"
    printf 'assurance_scope=plan-check:selected-independent-pass:final-verify\n'
    printf 'stage_graph_selector=intensity-not-qa\n'
    printf 'reviewer_counts=upper-bound-for-selected-pass-not-per-stage-loop\n'
    printf 'codex_role_checks=%s\n' "$role_checks"
    printf 'independent_delegation_policy=claim-only-if-separate-codex-agent-headless-or-external-pass-ran\n'
    printf 'fallback=report-inline-review-if-independent-agent-unavailable\n'
    ;;
  mcp)
    shift
    check_only=0
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --check)
          check_only=1
          shift
          ;;
        --*)
          echo "codex preflight: unknown mcp option: $1" >&2
          exit 64
          ;;
        *)
          echo "codex preflight: mcp accepts no positional arguments" >&2
          exit 64
          ;;
      esac
    done
    cat <<'EOF'
adapter=codex
runtime_surface=codex-native-mcp
status=native-runtime-config
mcp_surface=codex mcp
config_surface=$CODEX_HOME/config.toml
design_mcp_projection=policy-not-adopted-approval-gated
design_mcp_registration=stdio-mcp_servers.design-node-server.js
design_mcp_exec_gate=noninteractive-exec-approval-blocks-tool-calls
design_mcp_convert=adapters/codex/bin/preflight.sh convert <pdf|bundle|pptx> <file.html>
design_mcp_guidance=adapters/codex/ADAPTATION.md
claude_settings_mcp=unsupported
tool_contract_check=adapters/codex/bin/preflight.sh mcp --check
fallback=use-adapter-visual-harness-or-register-design-mcp-under-approval-policy
note=Codex can register the design MCP server (stdio [mcp_servers.design], node server.js) and its tools are discoverable and consume screenshots, but the adapter defaults to the owned visual harness; noninteractive codex exec auto-denies MCP tool calls until an approval/trust policy allows them. Do not copy Claude settings.json MCP registrations wholesale; see the MCP section of adapters/codex/ADAPTATION.md for the registration + approval path.
EOF
    if [ "$check_only" -eq 0 ]; then
      exit 0
    fi
    if command -v codex >/dev/null 2>&1 && codex mcp --help >/dev/null 2>&1; then
      printf 'check=ok\n'
    else
      printf 'check=failed\nreason=codex-mcp-unavailable\n'
      exit 69
    fi
    ;;
  worklog)
    cwd=${2:-$PWD}
      AGENT_HOME="$AGENT_ROOT" \
      AGENT_NOTES_ROOT="${AGENT_NOTES_ROOT:-${WORKLOG_NOTES_ROOT:-}}" \
      CAIRN_APP="${CAIRN_APP:-${WORKLOG_BOARD_APP:-}}" \
      CAIRN_WT="${CAIRN_WT:-${WORKLOG_BOARD_WT:-}}" \
      "$ROOT/utilities/agent-worklog-state.sh" "$cwd"
    ;;
  artifact-sink)
    shift
    exec "$ROOT/utilities/artifact-sink.sh" "$@"
    ;;
  ui-info)
    cat <<'EOF'
adapter=codex
runtime_surface=codex-native-ui-boundary
status=partial-native-parity
statusline_surface=codex-native-footer-config
statusline_command=/statusline
statusline_custom_dynamic_fields=unsupported
statusline_config_surface=$CODEX_HOME/config.toml
statusline_fragment=codex_setting/codex-config/tui-statusline.toml
recommended_status_line=project-name,git-branch,context-used,current-dir,model-with-reasoning,five-hour-limit,weekly-limit
recommended_status_line_use_colors=true
title_surface=codex-native-title-config
title_command=/title
hook_status_messages=available-after-hook-trust
harness_status_surface=adapter-owned-preflight-status
harness_status_command=adapters/codex/bin/preflight.sh status [cwd] [session-id]
autopilot_entrypoints=codex-native-skills
autopilot_auto_routing=instruction-guided-not-claude-slash-router
subagent_surface=codex-native-subagents
subagent_auto_spawn=explicit-or-main-dispatched
subagent_feature_check=adapters/codex/bin/preflight.sh subagent-info --check
note=Codex can configure built-in footer/title items, but it does not expose a Claude-style arbitrary live statusline script surface; use preflight status for harness-specific signals, and keep hooks silent (no statusMessage labels) to match Claude Code.
EOF
    ;;
  subagent-info)
    check_only=0
    case "${2:-}" in
      "")
        ;;
      --check)
        check_only=1
        ;;
      *)
        echo "codex preflight: subagent-info accepts only --check" >&2
        exit 64
        ;;
    esac
    cat <<EOF
adapter=codex
runtime_surface=codex-native-subagents
status=native-runtime-config
feature=multi_agent
feature_check=codex features list
native_agents_path=\$CODEX_HOME/agents
projection=codex_setting/codex-agents
trigger=explicit-user-request-or-main-dispatch
auto_spawn=explicit-only
custom_agent_config=model,model_reasoning_effort,sandbox_mode
memory_scout=adapters/codex/agents/memory-scout.toml
dispatch_fallback=adapters/codex/bin/preflight.sh dispatch --dry-run|--register|--start
constraints=depth-one-owner-with-standard-plus-depth-two,depth-two-standard-plus-via-capability-owner-dispatch,approval-and-sandbox-inherited
claude_subagent_frontmatter=unsupported
parity_caveat=Codex custom agents are TOML config layers, not Claude Code Agent frontmatter; verify discovery, UI, approval inheritance, and noninteractive behavior before claiming parity.
note=Codex subagents are native workflows and do not use Claude Agent files; verify the multi_agent feature, projected custom agents, and runtime config behavior before claiming delegation parity.
EOF
    if [ "$check_only" -eq 0 ]; then
      exit 0
    fi
    if ! command -v codex >/dev/null 2>&1; then
      printf 'check=failed\nreason=codex-command-unavailable\n'
      exit 69
    fi
    if codex features list 2>/dev/null | awk '$1=="multi_agent" && $3=="true" {found=1} END {exit found ? 0 : 1}'; then
      printf 'check=ok\nfeature=multi_agent\n'
    else
      printf 'check=failed\nreason=multi-agent-feature-disabled-or-unavailable\n'
      exit 69
    fi
    ;;
  loop-info)
    [ "$#" -eq 2 ] || { echo "codex preflight: loop-info requires one loop name" >&2; exit 64; }
    loop=$2
    case "$loop" in
      oncall)
        cat <<'EOF'
adapter=codex
loop=oncall
source=loops/oncall.md
status=manual-contract
runtime_surface=codex-loop-guidance
trigger=external-scheduler-or-manual
action=corroborated-offline-proposal-evidence-and-report
output=notes/oncall/<date>.md plus offline proposal evidence
executable_projection=unsupported-runtime-script
fallback=read-source-and-report-in-main-session
note=Codex may follow the portable oncall guide manually and use only its guarded offline proposal CLI path; do not run the Claude-coupled loop script as a Codex-native executable.
EOF
        ;;
      study)
        cat <<'EOF'
adapter=codex
loop=study
source=loops/study.md
status=manual-contract
runtime_surface=codex-loop-guidance
trigger=external-scheduler-or-manual
action=proposal-report-only
output=notes/study/<date>.md
executable_projection=unsupported-runtime-script
fallback=read-source-and-draft-proposal-in-main-session
note=Codex may follow the portable study guide manually; any proposed edits remain proposals until the user accepts them.
EOF
        ;;
      drill)
        cat <<'EOF'
adapter=codex
loop=drill
source=loops/drill/README.md
status=manual-contract
runtime_surface=codex-loop-guidance
trigger=manual-only
action=report-usefulness-before-running
auto_run=unsupported
executable_projection=unsupported-runtime-script
fallback=report-drill-would-be-useful
note=Do not run drill automatically from Codex; it can launch headless runtime sessions and spend tokens.
EOF
        ;;
      note)
        cat <<'EOF'
adapter=codex
loop=note
source=loops/README.md
status=unsupported
runtime_surface=missing-native-loop
trigger=external-scheduler
related_extension=artifact-sink
extension_check=adapters/codex/bin/preflight.sh artifact-sink --check
native_extension_surface=application-owned-plugin
scheduler_surface=external-application
action=not-implemented-in-repo
fallback=extension-unavailable-or-application-scheduler
note=The note application owns note semantics and scheduling; this harness exposes only the optional app-neutral artifact-sink port.
EOF
        ;;
      runtime-watch)
        cat <<'EOF'
adapter=codex
loop=runtime-watch
source=loops/runtime-watch.md
status=manual-contract
runtime_surface=codex-loop-guidance
trigger=change-triggered-or-conservative-schedule
action=deterministic-probe-and-proposal-report-only
auto_edit=unsupported
output=notes/runtime-watch/<date>.md
official_sources=openai-codex-pricing,openai-codex-changelog,openai-codex-rate-card,openai-codex-plan,claude-code-plan,claude-code-changelog
local_projection_checks=codex-runtime-projection,usage-check,cli-version
executable_projection=portable-shell-probe
probe=loops/runtime-watch.sh --probe
background_output_auto_resume=unsupported-for-arbitrary-detached-shell
registered_headless_completion_delivery=app-server-supervised-after-runtime-probe
completion_delivery=native-scheduled-follow-up-if-exposed-else-state-automatic-follow-up-impossible
completion_delivery_unavailable=state-automatic-follow-up-impossible
detached_completion_promise=forbidden
fallback=read-source-and-run-probe-or-report-unavailable
note=Runtime-watch separates official runtime support, local adapter projection, parity gap, and fallback. It must not auto-edit policy.
EOF
        ;;
      *)
        echo "codex preflight: unknown loop: $loop" >&2
        exit 64
        ;;
    esac
    ;;
  claim-verify)
    shift
    "$ROOT/adapters/codex/tools/research/claim-verify.sh" "$@"
    ;;
  browser-fetch)
    shift
    "$ROOT/adapters/codex/tools/material/browser-fetch.sh" "$@"
    ;;
  data-script)
    shift
    "$ROOT/adapters/codex/tools/material/data-script.sh" "$@"
    ;;
  figure-gen)
    shift
    "$ROOT/adapters/codex/tools/material/figure-gen.sh" "$@"
    ;;
  pdf-extract)
    shift
    "$ROOT/adapters/codex/tools/material/pdf-extract.sh" "$@"
    ;;
  web-image-search)
    shift
    "$ROOT/adapters/codex/tools/material/web-image-search.sh" "$@"
    ;;
  verification-runner)
    shift
    "$ROOT/adapters/codex/tools/qa/verification-runner.sh" "$@"
    ;;
  design)
    [ "$#" -ge 2 ] || { echo "codex preflight: design requires a file path" >&2; exit 64; }
    file=$2
    AGENT_HOME="$AGENT_ROOT" bash "$ROOT/hooks/design-postwrite.sh" --file "$file"
    ;;
  visual-harness)
    if [ "$#" -ge 2 ]; then
      shift
      "$ROOT/adapters/codex/tools/design/visual-harness.sh" "$@"
      exit $?
    fi
    cat <<'EOF'
adapter=codex
status=tool-contract
tool_contract=visual-harness
runtime_surface=adapter-owned-visual-harness
tool_contract_check=adapters/codex/bin/preflight.sh visual-harness <file.html>
fallback=preflight.sh visual-harness <file.html>
portable_source=capabilities/autopilot-design.md
note=Codex design capabilities have native Skill guidance and an adapter-owned render/screenshot/console harness. Run it for every design HTML output, then inspect the screenshot before claiming visual completion.
EOF
    ;;
  convert)
    if [ "$#" -ge 2 ]; then
      shift
      "$ROOT/adapters/codex/tools/design/convert-harness.sh" "$@"
      exit $?
    fi
    cat <<'EOF'
adapter=codex
status=tool-contract
tool_contract=design-convert
runtime_surface=adapter-owned-design-convert
tool_contract_check=adapters/codex/bin/preflight.sh convert <pdf|bundle|pptx> <file.html>
fallback=preflight.sh convert <pdf|bundle|pptx> <file.html>
portable_source=capabilities/autopilot-design.md
note=Codex-owned wrapper around the shared design converter (PDF/PPTX/bundle export). bundle is pure-Node; pdf/pptx report a tool-contract when Playwright/pptxgenjs are unavailable. Use it for design-handoff export where the visual harness only renders.
EOF
    ;;
  distill-delta)
    [ "$#" -ge 2 ] || { echo "codex preflight: distill-delta requires a session id" >&2; exit 64; }
    sid=$2
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/tools/memory/mem.py" distill "$sid" --source codex
    ;;
  distill-propose)
    [ "$#" -ge 2 ] || { echo "codex preflight: distill-propose requires a session id" >&2; exit 64; }
    sid=$2
    cwd=${3:-$PWD}
    if [ "${CODEX_DISTILL_ENABLE:-0}" != "1" ]; then
      cat <<EOF
adapter=codex
status=tool-contract
tool_contract=no-tools-distill-worker
runtime_surface=codex-exec-constrained-proposal
reason=distill-proposal-disabled
delta_surface=adapters/codex/bin/preflight.sh distill-delta <session-id>
enable=CODEX_DISTILL_ENABLE=1
apply_gate=CODEX_DISTILL_APPLY=1+CODEX_DISTILL_CONTRACT_ACCEPTED=1
fallback=inspect-distill-delta-or-enable-after-contract-review
cwd=$cwd
session_id=$sid
EOF
      exit 69
    fi
    AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/codex/bin/distill-worker.sh" "$sid" "$cwd"
    ;;
  role)
    [ "$#" -ge 2 ] || { echo "codex preflight: role requires a portable role" >&2; exit 64; }
    shift
    "$ROOT/adapters/codex/bin/role-map.sh" "$@"
    ;;
  capability-info)
    [ "$#" -eq 2 ] || { echo "codex preflight: capability-info requires one capability" >&2; exit 64; }
    "$ROOT/adapters/codex/bin/capability-map.sh" "$2"
    ;;
  mode-info)
    [ "$#" -eq 2 ] || { echo "codex preflight: mode-info requires one family/mode" >&2; exit 64; }
    "$ROOT/adapters/codex/bin/mode-map.sh" "$2"
    ;;
  -h|--help|"")
    usage
    exit 0
    ;;
  *)
    echo "codex preflight: unknown command: $cmd" >&2
    usage >&2
    exit 64
    ;;
esac
