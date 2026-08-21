#!/usr/bin/env sh
set -eu

READLINK_F_AVAILABLE=0
if command -v readlink >/dev/null 2>&1 && readlink_probe=$(readlink -f / 2>/dev/null) && [ "$readlink_probe" = / ]; then READLINK_F_AVAILABLE=1; fi
canonical_existing_path() {
  path=$1; [ -n "$path" ] || return 1; [ -e "$path" ] || [ -L "$path" ] || return 1
  if [ "$READLINK_F_AVAILABLE" -eq 1 ] && canonical=$(readlink -f "$path" 2>/dev/null); then [ -n "$canonical" ] && [ -e "$canonical" ] || return 1; printf '%s\n' "$canonical"; return 0; fi
  if [ -d "$path" ]; then if physical_dir=$(CDPATH= cd -P "$path" 2>/dev/null && pwd -P); then printf '%s\n' "$physical_dir"; return 0; fi; return 1; fi
  [ -L "$path" ] && return 1; path_dir=$(dirname "$path"); path_base=$(basename "$path")
  if physical_dir=$(CDPATH= cd -P "$path_dir" 2>/dev/null && pwd -P) && [ -e "$physical_dir/$path_base" ]; then printf '%s/%s\n' "$physical_dir" "$path_base"; return 0; fi
  return 1
}
is_harness_source_root() {
  candidate=$1; if canonical_candidate=$(canonical_existing_path "$candidate" 2>/dev/null); then :; else return 1; fi
  [ -d "$canonical_candidate" ] && [ -f "$canonical_candidate/core/CORE.md" ] || return 1
  [ -f "$canonical_candidate/adapters/opencode/bin/preflight.sh" ] && [ -x "$canonical_candidate/adapters/opencode/bin/preflight.sh" ] || return 1
  [ -f "$canonical_candidate/adapters/opencode/utilities/agent-home.sh" ] && [ -x "$canonical_candidate/adapters/opencode/utilities/agent-home.sh" ] || return 1
  [ -f "$canonical_candidate/utilities/artifact-root.sh" ] || return 1; [ -d "$canonical_candidate/roles" ] && [ -d "$canonical_candidate/capabilities" ] || return 1
  expected_hook=$canonical_candidate/hooks/core-first-guard.sh; [ -f "$expected_hook" ] && [ -x "$expected_hook" ] || return 1
  if canonical_hook=$(canonical_existing_path "$expected_hook" 2>/dev/null); then :; else return 1; fi
  [ "$canonical_hook" = "$expected_hook" ] || return 1; printf '%s\n' "$canonical_candidate"
}
typed_root_refusal() { printf 'check=failed\nreason=harness-source-root-unresolved\n' >&2; exit 69; }
if script_parent=$(dirname "$0") && SCRIPT_DIR=$(CDPATH= cd -P "$script_parent" 2>/dev/null && pwd -P); then :; else typed_root_refusal; fi
if SELF_REAL=$(canonical_existing_path "$0" 2>/dev/null); then :; else typed_root_refusal; fi
resolve_source_root() {
  git_candidate=""; if command -v git >/dev/null 2>&1 && git_root=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null) && git_root=$(canonical_existing_path "$git_root" 2>/dev/null); then git_candidate=$git_root; fi
  relative_candidate=""; if relative_candidate=$(CDPATH= cd -P "$SCRIPT_DIR/../../.." 2>/dev/null && pwd -P) && relative_candidate=$(canonical_existing_path "$relative_candidate" 2>/dev/null); then :; else relative_candidate=""; fi
  for candidate in "$git_candidate" "$relative_candidate" "${AGENT_HOME:-}"; do [ -n "$candidate" ] || continue; if accepted=$(is_harness_source_root "$candidate" 2>/dev/null); then printf '%s\n' "$accepted"; return 0; fi; done
  record=${XDG_CONFIG_HOME:-${HOME:-}/.config}/opencode/.harness/activation.json
  if [ -f "$record" ] && command -v python3 >/dev/null 2>&1; then
    if activation_candidate=$(python3 - "$record" opencode 2>/dev/null <<'PY'
import json, os, sys
with open(sys.argv[1], encoding="utf-8") as h: data=json.load(h)
if data.get("runtime") != sys.argv[2]: raise SystemExit(1)
root=data["active_root"] if "active_root" in data else data.get("source_root")
if not isinstance(root, str) or not root or not os.path.isabs(root): raise SystemExit(1)
if data.get("mode") == "packaged" and not data.get("activated_projection_digest"): raise SystemExit(1)
print(root)
PY
    ); then if [ -n "$activation_candidate" ] && accepted=$(is_harness_source_root "$activation_candidate" 2>/dev/null); then printf '%s\n' "$accepted"; return 0; fi; fi
  fi
  adjacent_resolver=$SCRIPT_DIR/../utilities/agent-home.sh
  if [ -x "$adjacent_resolver" ] && resolver_value=$("$adjacent_resolver" 2>/dev/null); then if [ -n "$resolver_value" ] && accepted=$(is_harness_source_root "$resolver_value" 2>/dev/null); then printf '%s\n' "$accepted"; return 0; fi; fi
  for candidate in "${XDG_CONFIG_HOME:-${HOME:-}/.config}/opencode/hearting" "${HOME:-}/hearting" "${HOME:-}/agent_setting"; do if accepted=$(is_harness_source_root "$candidate" 2>/dev/null); then printf '%s\n' "$accepted"; return 0; fi; done
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
  name=${relative_guard#hooks/}; expected=$ROOT/hooks/$name
  if [ "$target" != "$expected" ]; then printf 'check=failed\nreason=guard-target-self-reference\n' >&2; exit 69; fi
  printf '%s\n' "$target"
}
run_guard() {
  relative_guard=$1; shift; target=$(checked_guard_target "$relative_guard")
  case "$relative_guard" in
    hooks/material-route-guard.py) exec python3 "$target" --agent-home "$AGENT_ROOT" "$@";;
    hooks/worktree-path-guard.sh) exec "$target" "$@";;
    *) "$target" "$@";;
  esac
}

agent_home() {
  if [ -n "${AGENT_HOME:-}" ] && [ -f "$AGENT_HOME/core/CORE.md" ]; then
    printf '%s\n' "$AGENT_HOME"
  else
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

opencode_config_content_has_opencode_skills() {
  content=$1
  if [ -z "$content" ]; then
    return 1
  fi
  if printf '%s' "$content" | rg -q 'opencode-skills'; then
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi
  if printf '%s' "$content" | python3 -c 'import json, sys
def _has_opencode_skills(value):
    if isinstance(value, str):
        return "opencode-skills" in value
    if isinstance(value, list):
        return any(_has_opencode_skills(item) for item in value)
    if isinstance(value, dict):
        return any(_has_opencode_skills(value[k]) for k in value)
    return False

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)

sys.exit(0 if _has_opencode_skills(data) else 1)' ; then
    return 0
  fi
  return 1
}

usage() {
  cat <<'EOF'
usage: preflight.sh write <file> [session-id] [turn-id]
       preflight.sh read <file> [session-id]
       preflight.sh material-route <bind|check|clear> [options]
       preflight.sh worktree-path --tool <tool> [--command <cmd>] [--cwd <dir>] [--session <id>]
       preflight.sh capability <name> [cwd] [session-id]
       preflight.sh skill <name> [cwd] [session-id]
       preflight.sh memory [cwd]
       preflight.sh candidates <prompt> <cwd> <session-id> [turn-id]
       preflight.sh recall <query> [cwd] [session-id]
       preflight.sh recall-gate <cwd> (--decision recall|skip --reason <reason> [--query <query>] | --outcome applied|miss --gate-id <id>) [options]
       preflight.sh briefing [cwd]
       preflight.sh status [cwd] [session-id]
       preflight.sh permissions
       preflight.sh headless [--check] <worktree>
       preflight.sh nested-headless --parent-harness <h> --parent-transport <t> --parent-sandbox <s> --child-harness <h> --launch-authority <authority> --worktree <path> [--json]
       preflight.sh dispatch-readiness --worktree <path> --jobs <canonical-jobs.log> --owner-harness <h>... --child-harness <h>... --output <evidence.json>
       preflight.sh broker <status|stop> --jobs <jobs.log> [--root <broker-root>]  # legacy drain only
       preflight.sh dispatch [--dry-run|--register|--start] --worktree <path> --slug <slug> --capability <name> --capability-mode <mode> [--worker-mode <family/mode>] --qa <level> [--intensity <level>] [--dispatch-depth 1|2] [--parent <slug>] [--worker-type owner|stage|review|support] [--unit <unit>] [--assigned-contract <capability>] [--owner <capability>] [--agent <agent>] (--model-profile <deep|balanced-deep|light|mini> [--model-role <role>]|--model-role <role>|--model <model> --variant <variant>|--inherit-model-settings) [--prompt-file <file>|--prompt-text <text>] [--jobs <jobs.log>] [--log-dir <dir>]
       preflight.sh dispatch-chain --route <route.json> --node <id> --slug <slug> --parent <slug> [--capability-mode <mode>] [--worker-mode <family/mode>] [--model-role <role>] [--dry-run|--register|--start]
       preflight.sh dispatch-session-chain <check|register|start|run> --manifest <chain.json> --parent <slug> [--jobs <jobs.log>] [--max-seconds N]
       preflight.sh stage-heartbeat --attempt-id <id> --route-id <id> --route-node <id> --jobs <jobs.log> --phase <phase> --kind <kind> --evidence <ref>
       preflight.sh dispatch-current --jobs <jobs.log> (--session <id>|--route <id>|--node <id>|--attempt <id>|--job <slug>) [--all]
       preflight.sh dispatch-reconcile --jobs <jobs.log> (--session <id>|--route <id>|--node <id>|--attempt <id>|--job <slug>) [--apply]
       preflight.sh liveness [jobs.log] [--session <id>|--route <id>|--node <id>|--attempt <id>|--job <slug>] [--all]
       preflight.sh harvest [--jobs <jobs.log>] [--reconcile-local <legacy-jobs.log>] [--slug <slug>|--worktree <path>] [--status open|done|all] [--mark-done]
       preflight.sh worktree-cleanup [--check|--apply] (--worktree <path>|--all-eligible [--repo <path>]) [--integration-ref <ref>] [--jobs <jobs.log>]
       preflight.sh mcp [--check]
       preflight.sh worklog [cwd]
       preflight.sh artifact-sink <'--check'|'emit' ...>
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
       preflight.sh distill-delta <session-id>
       preflight.sh distill-propose <session-id> [cwd]
       preflight.sh role <portable-role>
       preflight.sh capability-info <capability>
       preflight.sh mode-info <family/mode>
       preflight.sh qa-policy <quick|light|standard|thorough|adversarial> [code|research|doc|general]
       preflight.sh doctor

Runs portable checks that OpenCode can call without consuming Claude hook JSON,
settings.json, or statusline.sh. The adapter also provides an OpenCode JS
plugin guard for write/edit/patch tools; use these wrappers as explicit
preflight checks when that plugin is not installed or trusted.
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
  rc=0
  printf 'adapter=opencode\n'
  printf 'runtime_surface=adapter-readiness-doctor\n'
  printf 'agent_home=%s\n' "$AGENT_ROOT"
  if command -v opencode >/dev/null 2>&1; then
    printf 'runtime_cli=available\n'
  else
    printf 'runtime_cli=unavailable\n'
  fi

  doctor_check generated-projections python3 "$ROOT/tools/generate.py" --check || rc=1
  doctor_check adaptation-boundary doctor_boundary || rc=1

  if [ "$rc" -eq 0 ]; then
    printf 'status=ok\n'
  else
    printf 'status=failed\n'
  fi
  return "$rc"
}

opencode_runtime_projection_check() {
  config_home=${XDG_CONFIG_HOME:-${HOME:-}/.config}
  opencode_home="$config_home/opencode"
  if [ -z "$config_home" ]; then
    printf 'check=failed\nreason=opencode-config-home-unset\n'
    return 69
  fi
  harness="$opencode_home/hearting"
  if [ ! -f "$harness/core/CORE.md" ]; then
    printf 'check=failed\nreason=opencode-hearting-missing\nopencode_home=%s\nexpected=%s\n' "$opencode_home" "$harness"
    return 69
  fi
  # Kernel helper is the projection liveness probe: runtime team agents retired
  # 2026-07-22 (재홈, CONVENTIONS §2.3) — memory-scout is the only projected native agent.
  native_agent="$opencode_home/agents/memory-scout/memory-scout.md"
  [ -f "$native_agent" ] || native_agent="$opencode_home/agents/memory-scout.md"
  [ -f "$native_agent" ] || native_agent="$opencode_home/agent/memory-scout.md"
  if [ ! -f "$native_agent" ]; then
    printf 'check=failed\nreason=opencode-native-agents-missing\nopencode_home=%s\nexpected=%s|%s\n' "$opencode_home" "$opencode_home/agents/memory-scout/memory-scout.md" "$opencode_home/agents/memory-scout.md"
    return 69
  fi
  native_command="$opencode_home/commands/autopilot-code.md"
  [ -f "$native_command" ] || native_command="$opencode_home/command/autopilot-code.md"
  if [ ! -f "$native_command" ]; then
    printf 'check=failed\nreason=opencode-native-commands-missing\nopencode_home=%s\nexpected=%s|%s\n' "$opencode_home" "$opencode_home/commands/autopilot-code.md" "$opencode_home/command/autopilot-code.md"
    return 69
  fi
  if [ ! -f "$opencode_home/plugins/hearting-guards.js" ]; then
    printf 'check=failed\nreason=opencode-native-plugin-missing\nopencode_home=%s\nexpected=%s\n' "$opencode_home" "$opencode_home/plugins/hearting-guards.js"
    return 69
  fi
  if [ ! -d "$opencode_home/skills" ] && [ ! -d "$opencode_home/agent-skills" ] && ! opencode_config_content_has_opencode_skills "${OPENCODE_CONFIG_CONTENT:-}"; then
    printf 'check=failed\nreason=opencode-native-skills-missing\nopencode_home=%s\nexpected=%s|%s\n' "$opencode_home" "$opencode_home/skills" "$opencode_home/agent-skills"
    return 69
  fi
  if rg -q 'adapters/claude|claude_setting|settings\.json|statusline\.sh|CLAUDE\.md|agent-modes|allowedTools|/\.claude/' \
    "$native_agent" "$native_command" "$opencode_home/plugins/hearting-guards.js" 2>/dev/null; then
    printf 'check=failed\nreason=opencode-runtime-projection-exposes-claude-surface\nopencode_home=%s\n' "$opencode_home"
    return 69
  fi
  printf 'runtime_projection=ok\nopencode_home=%s\nnative_agent=%s\nnative_command=%s\n' "$opencode_home" "$native_agent" "$native_command"
  return 0
}

cmd=${1:-}
case "$cmd" in
  doctor)
    doctor
    ;;
  write)
    [ "$#" -ge 2 ] || { echo "opencode preflight: write requires a file path" >&2; exit 64; }
    file=$2
    sid=${3:-opencode}
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
    if [ -n "$turn" ]; then
      "$0" material-route check --tool Write --file "$file" --cwd "$(dirname "$file")" --session "$sid" --turn "$turn"
    else
      "$0" material-route check --tool Write --file "$file" --cwd "$(dirname "$file")" --session "$sid"
    fi
    ;;
  material-route)
    [ "$#" -ge 2 ] || { echo "opencode preflight: material-route requires an action" >&2; exit 64; }
    shift
    run_guard hooks/material-route-guard.py "$@"
    ;;
  worktree-path)
    shift
    run_guard hooks/worktree-path-guard.sh "$@"
    ;;
  read)
    [ "$#" -ge 2 ] || { echo "opencode preflight: read requires a file path" >&2; exit 64; }
    file=$2
    sid=${3:-opencode}
    "$ROOT/hooks/core-read-marker.sh" --file "$file" --session "$sid"
    "$ROOT/hooks/spec-read-marker.sh" --file "$file" --session "$sid"
    ;;
  capability|skill)
    [ "$#" -ge 2 ] || { echo "opencode preflight: $cmd requires a capability name" >&2; exit 64; }
    name=$2
    cwd=${3:-$PWD}
    sid=${4:-opencode}
    "$ROOT/hooks/spec-skill-gate.sh" --skill "$name" --cwd "$cwd" --session "$sid"
    ;;
  prompt-signal)
    cwd=${2:-$PWD}
    sid=${3:-opencode}
    status=$(AGENT_ADAPTER=opencode "$ROOT/utilities/harness-status.sh" "$cwd" "$sid")
    artifact_root_kind=$(printf '%s\n' "$status" | awk -F= '$1=="artifact_root_kind"{print $2; exit}')
    git_operation=$(printf '%s\n' "$status" | awk -F= '$1=="git_operation"{print $2; exit}')
    headless_open_jobs=$(printf '%s\n' "$status" | awk -F= '$1=="headless_open_jobs"{print $2; exit}')
    printf 'adapter=opencode\n'
    printf 'runtime_surface=opencode-system-transform-hook-signal\n'
    printf 'hook_event=experimental.chat.system.transform\n'
    printf 'hook_scope=runtime-plugin\n'
    printf 'artifact_root_kind=%s\n' "${artifact_root_kind:-unknown}"
    printf 'git_operation=%s\n' "${git_operation:-unknown}"
    printf 'headless_open_jobs=%s\n' "${headless_open_jobs:-0}"
    printf 'autopilot_route=autopilot-required-for-spec-and-nontrivial-work\n'
    printf 'routing_contract=core/WORKFLOW.md\n'
    printf 'routing_action=read-workflow-and-select-opencode-skill-or-command\n'
    printf 'capability_entrypoints=opencode-native-skills-commands\n'
    printf 'enforced_hooks=plugin-write-guards,core-first-guard,plugin-command-spec-gate,plugin-read-markers,plugin-design-check,session-memory,prompt-recall,session-idle-distill\n'
    printf 'hook_boundary=plugin-tool-command-event-bridges\n'
    ;;
  ui-info)
    cat <<'EOF'
adapter=opencode
runtime_surface=opencode-native-ui-boundary
status=partial-native-parity
statusline_surface=opencode-native-tui-footer
statusline_custom_script=unsupported
statusline_config_surface=none-in-opencode-config-schema
harness_status_surface=adapter-owned-preflight-status
harness_status_command=adapters/opencode/bin/preflight.sh status [cwd] [session-id]
autopilot_entrypoints=opencode-native-skills-commands
autopilot_auto_routing=instruction-guided-not-claude-slash-router
subagent_surface=opencode-native-subagents
subagent_auto_spawn=explicit-or-main-dispatched
note=OpenCode exposes a native TUI footer (model/context/tokens/session) but no user-customizable shell statusline script; use preflight status for harness-specific signals.
EOF
    ;;
  memory)
    cwd=${2:-$PWD}
    (cd "$cwd" && AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/tools/memory/mem.py" inject)
    ;;
  candidates)
    [ "$#" -ge 4 ] || { echo "opencode preflight: candidates requires prompt, cwd, and session-id" >&2; exit 64; }
    prompt=$2
    cwd=$3
    sid=$4
    turn=${5:-}
    set -- --prompt "$prompt" --cwd "$cwd" --session-id "$sid" --runtime opencode --format text
    [ -z "$turn" ] || set -- "$@" --turn-id "$turn"
    AGENT_HOME="$AGENT_ROOT" bash "$ROOT/hooks/mem-recall-inject.sh" "$@"
    ;;
  recall)
    [ "$#" -ge 2 ] || { echo "opencode preflight: recall requires a query" >&2; exit 64; }
    query=$2
    cwd=${3:-$PWD}
    (cd "$cwd" && AGENT_HOME="$AGENT_ROOT" MEM_RECALL_RUNTIME=opencode \
      "$ROOT/tools/memory/recall.sh" "$query")
    ;;
  recall-gate)
    [ "$#" -ge 3 ] || { echo "opencode preflight: recall-gate requires cwd and gate arguments" >&2; exit 64; }
    cwd=$2
    shift 2
    (cd "$cwd" && AGENT_HOME="$AGENT_ROOT" MEM_RECALL_RUNTIME=opencode \
      python3 "$ROOT/tools/memory/mem.py" recall-gate "$@")
    ;;
  briefing)
    cwd=${2:-$PWD}
    AGENT_HOME="$AGENT_ROOT" bash "$ROOT/hooks/mem-briefing-inject.sh" --cwd "$cwd" --format text
    ;;
  status)
    cwd=${2:-$PWD}
    sid=${3:-opencode}
    AGENT_ADAPTER=opencode "$ROOT/utilities/harness-status.sh" "$cwd" "$sid"
    ;;
  permissions)
    cat <<'EOF'
adapter=opencode
runtime_surface=opencode-native-permission-config
status=native-runtime-config
permission_model=permission-allow-ask-deny
permission_surface=opencode permission config with allow/ask/deny per tool and per-agent override
plugin_surface=permission.ask and tool.execute hooks
config_surface=$HOME/.config/opencode/opencode.json
claude_allowed_tools=unsupported
guard_contract=preflight-write-plugin-and-explicit-tool-contracts
fallback=configure-opencode-permissions-and-run-preflight-guards
note=Do not port Claude allowedTools into OpenCode; use OpenCode permission config plus adapter preflight/plugin guards.
EOF
    ;;
  headless)
    shift
    check_only=0
    worktree=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --check)
          check_only=1
          shift
          ;;
        --*)
          echo "opencode preflight: unknown headless option: $1" >&2
          exit 64
          ;;
        *)
          if [ -z "$worktree" ]; then
            worktree=$1
          else
            echo "opencode preflight: headless accepts one worktree path" >&2
            exit 64
          fi
          shift
          ;;
      esac
    done
    cat <<'EOF'
adapter=opencode
runtime_surface=opencode-run-headless
status=tool-contract
tool_contract=headless-dispatch
tool_contract_check=adapters/opencode/bin/preflight.sh headless --check <worktree>
command_template=opencode run --dir <worktree> --format json --agent <agent> (--model <main-selected-model> --variant <main-selected-variant>|inherit) "$(cat -- <prompt-file>)"
model_selection_policy=main-orchestrator-must-select-per-job
model_selection_surface=--model-profile <deep|balanced-deep|light|mini> [--model-role <portable-role>]|--model-role <portable-role>|--model <model> --variant <variant>|--inherit-model-settings
job_registry=<agent-home>/.dispatch/jobs.log (immutable AGENT_DISPATCH_JOBS for descendants)
nested_eligibility_check=adapters/opencode/bin/preflight.sh nested-headless --parent-harness <h> --parent-transport <t> --parent-sandbox <s> --child-harness <h> --launch-authority <a> --worktree <path>
fallback_chain=same-harness-headless,cross-harness-headless,native-subagent,inline
fallback_runner=adapters/opencode/bin/preflight.sh dispatch-chain --route <route> --node <node> --dry-run|--register|--start
broker_lifecycle=retired-status-stop-only
launch_authority=conductor
liveness_surface=opencode-sqlite-session-mtime+plugin-heartbeat
liveness_heartbeat=<agent-home>/.dispatch/logs/<slug>.heartbeat
liveness_plugin_load_marker=<agent-home>/.dispatch/plugin-load.<slug>.mark
liveness_check=adapters/opencode/bin/preflight.sh liveness [jobs.log]
harvest_check=adapters/opencode/bin/preflight.sh harvest [--jobs jobs.log] [--slug slug] [--mark-done]
dispatch_prompt_contract=portable-typed-worker-bootstrap
worker_bootstrap_source=roles/worker-bootstrap.md+roles/worker-types/<owner|stage|review|support>.md
worker_handoff=artifact,verdict,blocker
dispatch_input_validation=capability-info,capability-mode-catalog,optional-worker-mode-info,owner-mode-axis-consistency,qa-level,intensity-dispatch_depth-parent
physical_project_instruction_masking=unverified-checked-prompt-isolation-fallback
constraints=main-or-owner-dispatched,max-dispatch-depth-2-for-standard-plus-owner,register-open-job,explicit-capability-mode-qa-intensity-dispatch_depth-parent-parent_sid,transcript-liveness-required
claude_headless=unsupported
fallback=checked-dispatch-chain-or-structured-degradation
EOF
    if [ "$check_only" -eq 0 ]; then
      exit 0
    fi
    [ -n "$worktree" ] || { echo "opencode preflight: headless --check requires a worktree path" >&2; exit 64; }
    if [ ! -d "$worktree" ]; then
      printf 'check=failed\nreason=worktree-not-found\nworktree=%s\n' "$worktree"
      exit 66
    fi
    if ! command -v opencode >/dev/null 2>&1; then
      printf 'check=failed\nreason=opencode-command-unavailable\nworktree=%s\n' "$worktree"
      exit 69
    fi
    if ! git -C "$worktree" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      printf 'check=failed\nreason=not-a-git-worktree\nworktree=%s\n' "$worktree"
      exit 65
    fi
    opencode_runtime_projection_check
    printf 'check=ok\nworktree=%s\n' "$worktree"
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
  liveness)
    shift
    jobs=${AGENT_DISPATCH_JOBS:-"$AGENT_ROOT/.dispatch/jobs.log"}
    if [ "$#" -gt 0 ] && [ "${1#--}" = "$1" ]; then jobs=$1; shift; fi
    current_jobs=$(mktemp)
    trap 'rm -f "$current_jobs"' EXIT
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/dispatch-registry.py" liveness --jobs "$jobs" "$@" > "$current_jobs" || exit $?
    AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/opencode/bin/dispatch-liveness.py" "$current_jobs"
    ;;
  harvest)
    shift
    AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/opencode/bin/dispatch-harvest.py" "$@"
    ;;
  worktree-cleanup)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/worktree-cleanup.py" "$@"
    ;;
  dispatch)
    shift
    AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/opencode/bin/dispatch-headless.py" "$@"
    ;;
  dispatch-chain)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/stage-dispatch-fallback.py" "$@"
    ;;
  dispatch-session-chain)
    shift
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/utilities/stage-session-chain.py" "$@"
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
    [ "$#" -ge 2 ] || { echo "opencode preflight: qa-policy requires a QA level" >&2; exit 64; }
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
        echo "opencode preflight: unknown QA level: $level" >&2
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
        echo "opencode preflight: unknown QA track: $track" >&2
        exit 64
        ;;
    esac
    printf 'adapter=opencode\n'
    printf 'runtime_surface=opencode-qa-policy\n'
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
    printf 'opencode_role_checks=%s\n' "$role_checks"
    printf 'independent_delegation_policy=claim-only-if-separate-opencode-agent-headless-or-external-pass-ran\n'
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
          echo "opencode preflight: unknown mcp option: $1" >&2
          exit 64
          ;;
        *)
          echo "opencode preflight: mcp accepts no positional arguments" >&2
          exit 64
          ;;
      esac
    done
    cat <<'EOF'
adapter=opencode
runtime_surface=opencode-native-mcp
status=native-runtime-config
mcp_surface=opencode mcp
config_surface=$HOME/.config/opencode/opencode.json
design_mcp_projection=unsupported
claude_settings_mcp=unsupported
tool_contract_check=adapters/opencode/bin/preflight.sh mcp --check
fallback=use-adapter-visual-harness-or-report-mcp-unavailable
note=Do not copy Claude settings.json MCP registrations or project tools/design-mcp wholesale into OpenCode.
EOF
    if [ "$check_only" -eq 0 ]; then
      exit 0
    fi
    if command -v opencode >/dev/null 2>&1 && opencode mcp --help >/dev/null 2>&1; then
      printf 'check=ok\n'
    else
      printf 'check=failed\nreason=opencode-mcp-unavailable\n'
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
  loop-info)
    [ "$#" -eq 2 ] || { echo "opencode preflight: loop-info requires one loop name" >&2; exit 64; }
    loop=$2
    case "$loop" in
      oncall)
        cat <<'EOF'
adapter=opencode
loop=oncall
source=loops/oncall.md
status=manual-contract
runtime_surface=opencode-loop-guidance
trigger=external-scheduler-or-manual
action=corroborated-offline-proposal-evidence-and-report
output=notes/oncall/<date>.md plus offline proposal evidence
executable_projection=unsupported-runtime-script
fallback=read-source-and-report-in-main-session
note=OpenCode may follow the portable oncall guide manually and use only its guarded offline proposal CLI path; do not run the Claude-coupled loop script as an OpenCode-native executable.
EOF
        ;;
      study)
        cat <<'EOF'
adapter=opencode
loop=study
source=loops/study.md
status=manual-contract
runtime_surface=opencode-loop-guidance
trigger=external-scheduler-or-manual
action=proposal-report-only
output=notes/study/<date>.md
executable_projection=unsupported-runtime-script
fallback=read-source-and-draft-proposal-in-main-session
note=OpenCode may follow the portable study guide manually; any proposed edits remain proposals until the user accepts them.
EOF
        ;;
      drill)
        cat <<'EOF'
adapter=opencode
loop=drill
source=loops/drill/README.md
status=manual-contract
runtime_surface=opencode-loop-guidance
trigger=manual-only
action=report-usefulness-before-running
auto_run=unsupported
executable_projection=unsupported-runtime-script
fallback=report-drill-would-be-useful
note=Do not run drill automatically from OpenCode; it can launch headless runtime sessions and spend tokens.
EOF
        ;;
      note)
        cat <<'EOF'
adapter=opencode
loop=note
source=loops/README.md
status=unsupported
runtime_surface=missing-native-loop
trigger=external-scheduler
related_extension=artifact-sink
extension_check=adapters/opencode/bin/preflight.sh artifact-sink --check
native_extension_surface=application-owned-plugin
scheduler_surface=external-application
action=not-implemented-in-repo
fallback=extension-unavailable-or-application-scheduler
note=The note application owns note semantics and scheduling; this harness exposes only the optional app-neutral artifact-sink port.
EOF
        ;;
      runtime-watch)
        cat <<'EOF'
adapter=opencode
loop=runtime-watch
source=loops/runtime-watch.md
status=manual-contract
runtime_surface=opencode-loop-guidance
trigger=change-triggered-or-conservative-schedule
action=deterministic-probe-and-proposal-report-only
auto_edit=unsupported
output=notes/runtime-watch/<date>.md
official_sources=openai-codex-pricing,openai-codex-changelog,openai-codex-rate-card,openai-codex-plan,claude-code-plan,claude-code-changelog
local_projection_checks=codex-runtime-projection,usage-check,cli-version
executable_projection=portable-shell-probe
probe=loops/runtime-watch.sh --probe
fallback=read-source-and-run-probe-or-report-unavailable
note=Runtime-watch separates official runtime support, local adapter projection, parity gap, and fallback. It must not auto-edit policy.
EOF
        ;;
      *)
        echo "opencode preflight: unknown loop: $loop" >&2
        exit 64
        ;;
    esac
    ;;
  claim-verify)
    shift
    "$ROOT/adapters/opencode/tools/research/claim-verify.sh" "$@"
    ;;
  browser-fetch)
    shift
    "$ROOT/adapters/opencode/tools/material/browser-fetch.sh" "$@"
    ;;
  data-script)
    shift
    "$ROOT/adapters/opencode/tools/material/data-script.sh" "$@"
    ;;
  figure-gen)
    shift
    "$ROOT/adapters/opencode/tools/material/figure-gen.sh" "$@"
    ;;
  pdf-extract)
    shift
    "$ROOT/adapters/opencode/tools/material/pdf-extract.sh" "$@"
    ;;
  web-image-search)
    shift
    "$ROOT/adapters/opencode/tools/material/web-image-search.sh" "$@"
    ;;
  verification-runner)
    shift
    "$ROOT/adapters/opencode/tools/qa/verification-runner.sh" "$@"
    ;;
  design)
    [ "$#" -ge 2 ] || { echo "opencode preflight: design requires a file path" >&2; exit 64; }
    file=$2
    AGENT_HOME="$AGENT_ROOT" bash "$ROOT/hooks/design-postwrite.sh" --file "$file"
    ;;
  visual-harness)
    if [ "$#" -ge 2 ]; then
      shift
      "$ROOT/adapters/opencode/tools/design/visual-harness.sh" "$@"
      exit $?
    fi
    cat <<'EOF'
adapter=opencode
status=tool-contract
tool_contract=visual-harness
runtime_surface=adapter-owned-visual-harness
tool_contract_check=adapters/opencode/bin/preflight.sh visual-harness <file.html>
fallback=preflight.sh visual-harness <file.html>
portable_source=capabilities/autopilot-design.md
note=OpenCode design capabilities have native Skill/Command guidance and an adapter-owned render/screenshot/console harness. Run it for every design HTML output, then inspect the screenshot before claiming visual completion.
EOF
    ;;
  distill-delta)
    [ "$#" -ge 2 ] || { echo "opencode preflight: distill-delta requires a session id" >&2; exit 64; }
    sid=$2
    AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/tools/memory/mem.py" distill "$sid" --source opencode
    ;;
  distill-propose)
    [ "$#" -ge 2 ] || { echo "opencode preflight: distill-propose requires a session id" >&2; exit 64; }
    sid=$2
    cwd=${3:-$PWD}
    # The explicit, user-facing proposal stays an opt-in preview (mirrors codex):
    # the no-tools worker is verified, but you enable the explicit run with
    # OPENCODE_DISTILL_ENABLE=1. The automatic session-end path defaults it on.
    if [ "${OPENCODE_DISTILL_ENABLE:-0}" != "1" ]; then
      cat <<EOF
adapter=opencode
status=tool-contract
tool_contract=no-tools-distill-worker
runtime_surface=opencode-run-pure-notools-agent
reason=distill-proposal-disabled
delta_surface=adapters/opencode/bin/preflight.sh distill-delta <session-id>
enable=OPENCODE_DISTILL_ENABLE=1
apply_gate=OPENCODE_DISTILL_APPLY=1
auto=plugin-session-idle-to-session-end-default-on
fallback=inspect-distill-delta-or-enable-explicit-proposal
cwd=$cwd
session_id=$sid
EOF
      exit 69
    fi
    AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/opencode/bin/distill-worker.sh" "$sid" "$cwd"
    ;;
  session-end)
    cwd=${2:-$PWD}
    sid=${3:-opencode}
    # D-42 defense in depth: workers never create a debounce stamp, sync, or
    # start another distiller from session.idle/session-end.
    is_worker_session && exit 0
    # Debounce: the OpenCode plugin fires this on session.idle, which occurs after
    # every turn. Rate-limit per session so a long TUI session triggers at most
    # one worker per OPENCODE_DISTILL_MIN_INTERVAL seconds (default 600).
    default_store="$AGENT_ROOT/memory"
    [ -e "$default_store" ] || [ -L "$default_store" ] \
      || default_store="${XDG_DATA_HOME:-$HOME/.local/share}/hearting/memory"
    store=${MEM_STORE:-$default_store}
    mkdir -p "$store" 2>/dev/null || true
    stamp="$store/.opencode-distill-stamp-$sid"
    interval=${OPENCODE_DISTILL_MIN_INTERVAL:-600}
    now=$(date +%s 2>/dev/null || echo 0)
    if [ -f "$stamp" ] && [ "$now" -gt 0 ]; then
      last=$(cat "$stamp" 2>/dev/null || echo 0)
      [ "$last" -gt 0 ] && [ "$((now - last))" -lt "$interval" ] && exit 0
    fi
    printf '%s\n' "$now" > "$stamp" 2>/dev/null || true
    # Absorb any stray native writes, then run the auto-distiller. Enabled by
    # default (parity with the codex/claude session-end distillers); opt out with
    # OPENCODE_DISTILL_ENABLE=0. The worker is no-tools verified and timeout-
    # guarded, so a slow/unreachable model can never stall this path.
    (cd "$cwd" && AGENT_HOME="$AGENT_ROOT" python3 "$ROOT/tools/memory/mem.py" sync --json >/dev/null) || true
    AGENT_HOME="$AGENT_ROOT" \
      OPENCODE_DISTILL_ENABLE="${OPENCODE_DISTILL_ENABLE:-1}" \
      OPENCODE_DISTILL_APPLY="${OPENCODE_DISTILL_APPLY:-1}" \
      "$ROOT/adapters/opencode/bin/distill-worker.sh" "$sid" "$cwd" curate
    ;;
  role)
    [ "$#" -ge 2 ] || { echo "opencode preflight: role requires a portable role" >&2; exit 64; }
    shift
    "$ROOT/adapters/opencode/bin/role-map.sh" "$@"
    ;;
  route)
    if [ "${2:-}" != "--capability" ]; then
      echo "opencode preflight: route requires --capability option form" >&2
      exit 64
    fi
    shift
    # D1: bind only after a successful compile with exactly one --output, a
    # --cwd, and a nonempty OPENCODE_SESSION_ID (from the plugin's shell.env
    # hook). Scan the already-tokenized argv only, never the command text.
    # No --output (optional on the compiler), no sid, more than one --output,
    # or a nonzero compile all pass through unbound and silently, preserving
    # the compiler's own stdout/stderr/exit status.
    output_count=0
    output_val=""
    cwd_val=""
    prev=""
    for a in "$@"; do
      case "$a" in
        --output=*) output_count=$((output_count + 1)); output_val=${a#--output=} ;;
        --cwd=*) cwd_val=${a#--cwd=} ;;
      esac
      case "$prev" in
        --output) output_count=$((output_count + 1)); output_val=$a ;;
        --cwd) cwd_val=$a ;;
      esac
      prev=$a
    done
    set +e
    python3 "$ROOT/utilities/capability-route.py" compile "$@"
    compile_rc=$?
    set -e
    bind_rc=0
    if [ "$compile_rc" -eq 0 ] && [ "$output_count" -eq 1 ] && [ -n "$cwd_val" ] && [ -n "${OPENCODE_SESSION_ID:-}" ]; then
      set +e
      "$0" material-route bind --route "$output_val" --cwd "$cwd_val" --session "$OPENCODE_SESSION_ID"
      bind_rc=$?
      set -e
    fi
    if [ "$bind_rc" -ne 0 ]; then
      exit "$bind_rc"
    fi
    exit "$compile_rc"
    ;;
  worker-route)
    shift
    exec python3 "$ROOT/utilities/worker-route-guard.py" validate "$@"
    ;;
  dispatch-node)
    shift
    exec python3 "$ROOT/utilities/dispatch-node.py" --adapter opencode "$@"
    ;;
  capability-info)
    [ "$#" -eq 2 ] || { echo "opencode preflight: capability-info requires one capability" >&2; exit 64; }
    "$ROOT/adapters/opencode/bin/capability-map.sh" "$2"
    ;;
  mode-info)
    [ "$#" -eq 2 ] || { echo "opencode preflight: mode-info requires one family/mode" >&2; exit 64; }
    "$ROOT/adapters/opencode/bin/mode-map.sh" "$2"
    ;;
  -h|--help|"")
    usage
    exit 0
    ;;
  *)
    echo "opencode preflight: unknown command: $cmd" >&2
    usage >&2
    exit 64
    ;;
esac
