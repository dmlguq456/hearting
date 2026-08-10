#!/usr/bin/env sh
# check-runtime-projection.sh — read-only validation that the Codex runtime home
# ($CODEX_HOME, default $HOME/.codex) is wired to the harness projection. Emits
# machine-readable `check=<name>:ok|failed|skipped` lines plus a final
# `status=ok|failed`. Hard checks (symlink wiring, linked skills/agents) drive
# status; bootstrap/plugin checks are soft (skipped when codex is unavailable,
# the plugin reports install commands when missing). config.toml stays
# runtime-owned. Hearting's model map is a separate user-owned regular file.
# Set CODEX_REQUIRE_HOOK_TRUST=1 to make missing `/hooks` trust records fail.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME=${AGENT_HOME:-}
if [ -z "$AGENT_HOME" ] || [ ! -f "$AGENT_HOME/core/CORE.md" ]; then
  AGENT_HOME=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
fi
CODEX_HOME=${CODEX_HOME:-$HOME/.codex}
CLI_TIMEOUT=${CODEX_RUNTIME_PROJECTION_CLI_TIMEOUT:-15}
S="$AGENT_HOME/codex_setting"

case "${1:-}" in
  -h|--help)
    echo "usage: check-runtime-projection.sh   # validates \$CODEX_HOME wiring, read-only"
    exit 0 ;;
esac

fails=0
printf 'runtime_surface=codex-runtime-projection-check\n'
printf 'codex_home=%s\n' "$CODEX_HOME"

real() { readlink -f "$1" 2>/dev/null || true; }

# The portable runtime activator projects the full capability set per-file. Its
# activation record is authoritative for per-file Skills, agents, and modes;
# the legacy installer instead projects the complete compatibility trees.
activation_state="$CODEX_HOME/.harness/activation.json"
native_managed=0
if [ -f "$activation_state" ]; then
  if python3 - "$activation_state" "$AGENT_HOME" <<'PY' 2>/dev/null; then
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
source = pathlib.Path(state.get("source_root", "")).resolve()
expected = pathlib.Path(sys.argv[2]).resolve()
if state.get("runtime") == "codex" and source == expected and state.get("activated_projection_digest"):
    print("managed")
    raise SystemExit(0)
raise SystemExit(1)
PY
    native_managed=1
  fi
fi

expect_link() {  # <linkpath> <expected-target> <checkname>
  lp=$1; exp=$2; name=$3
  if [ -L "$lp" ] && [ -n "$(real "$lp")" ] && [ "$(real "$lp")" = "$(real "$exp")" ]; then
    printf 'check=%s:ok\n' "$name"
  else
    printf 'check=%s:failed reason=expected-symlink-to:%s\n' "$name" "$exp"
    fails=$((fails + 1))
  fi
}

expect_link "$CODEX_HOME/hearting"            "$AGENT_HOME"                  hearting
expect_link "$CODEX_HOME/AGENTS.md"                "$S/AGENTS.md"                 agents-md
expect_link "$CODEX_HOME/agent-core"               "$S/core"                      agent-core
expect_link "$CODEX_HOME/agent-capabilities"       "$S/capabilities"              agent-capabilities
expect_link "$CODEX_HOME/agent-roles"              "$S/roles"                     agent-roles
expect_link "$CODEX_HOME/agent-bin"                "$S/bin"                       agent-bin
expect_link "$CODEX_HOME/agent-hooks"              "$S/codex-hooks"               agent-hooks
if [ "$native_managed" -eq 1 ]; then
  for name in hearting-readme agent-tools agent-utilities agent-scaffolds agent-skills agent-agents agent-plugin-marketplace; do
    printf 'check=%s:skipped reason=runtime-managed\n' "$name"
  done
  printf 'check=agent-modes:ok reason=runtime-managed-per-file\n'
  if "$AGENT_HOME/tools/install/harness.sh" runtime doctor --runtime codex --strict --json >/dev/null 2>&1; then
    printf 'check=runtime-activation:ok\n'
  else
    printf 'check=runtime-activation:failed\n'
    fails=$((fails + 1))
  fi
else
  expect_link "$CODEX_HOME/hearting-readme.md"  "$S/README.md"                 hearting-readme
  expect_link "$CODEX_HOME/agent-tools"              "$S/tools"                     agent-tools
  expect_link "$CODEX_HOME/agent-utilities"          "$S/utilities"                 agent-utilities
  expect_link "$CODEX_HOME/agent-scaffolds"          "$S/scaffolds"                 agent-scaffolds
  expect_link "$CODEX_HOME/agent-skills"             "$S/codex-skills"              agent-skills
  expect_link "$CODEX_HOME/agent-agents"             "$S/codex-agents"              agent-agents
  expect_link "$CODEX_HOME/agent-modes"              "$S/codex-modes"               agent-modes
  expect_link "$CODEX_HOME/agent-plugin-marketplace" "$S/codex-plugin-marketplace" agent-plugin-marketplace
fi

if [ -f "$CODEX_HOME/agent-config/models.conf" ] && [ ! -L "$CODEX_HOME/agent-config" ] && [ ! -L "$CODEX_HOME/agent-config/models.conf" ]; then
  printf 'check=agent-config:ok reason=user-owned-model-config\n'
else
  printf 'check=agent-config:failed reason=expected-regular-user-model-config\n'
  fails=$((fails + 1))
fi

hook_trust_check() {
  hook_file="$CODEX_HOME/hooks.json"
  helper="$AGENT_HOME/adapters/codex/bin/hook-trust-status.py"
  if [ "${CODEX_REQUIRE_HOOK_TRUST:-0}" != "1" ]; then
    printf 'check=hook-trust:skipped reason=authoritative-current-hash-check-not-requested\n'
    printf 'hook_trust_hint=rerun with --require-hook-trust for authoritative App Server verification\n'
    return 0
  fi
  hook_out=""
  if [ -f "$hook_file" ] && [ -f "$helper" ] && command -v codex >/dev/null 2>&1; then
    if [ -n "${CODEX_PARENT_SESSION_ID:-}" ]; then
      parent_args="--parent-session-id $CODEX_PARENT_SESSION_ID"
      [ -n "${CODEX_PARENT_DEFINITION_LEDGER_PATH:-}" ] && parent_args="$parent_args --ledger-path $CODEX_PARENT_DEFINITION_LEDGER_PATH"
      [ -n "${CODEX_PARENT_DEFINITION_LOCK_PATH:-}" ] && parent_args="$parent_args --lock-path $CODEX_PARENT_DEFINITION_LOCK_PATH"
      # These values are harness-controlled dispatch paths, not shell code.
      hook_out=$(CODEX_HOME="$CODEX_HOME" timeout "$CLI_TIMEOUT" python3 "$helper" \
        --hooks-file "$hook_file" --cwd "$AGENT_HOME" \
        $parent_args 2>/dev/null) && hook_rc=0 || hook_rc=$?
    else
      hook_out=$(CODEX_HOME="$CODEX_HOME" timeout "$CLI_TIMEOUT" python3 "$helper" \
        --hooks-file "$hook_file" --cwd "$AGENT_HOME" 2>/dev/null) && hook_rc=0 || hook_rc=$?
    fi
    if [ "$hook_rc" -eq 0 ]; then
      printf 'check=hook-trust:ok current_hash=verified\n'
      [ -n "$hook_out" ] && printf 'hook_trust_detail=%s\n' "$hook_out"
      return 0
    else
      :
    fi
  else
    hook_rc=69
  fi
  if [ "$hook_rc" -eq 3 ]; then
    printf 'check=hook-trust:review-needed reason=current-hash-not-trusted\n'
  else
    printf 'check=hook-trust:review-needed reason=authoritative-check-unavailable exit=%s\n' "$hook_rc"
  fi
  [ -n "$hook_out" ] && printf 'hook_trust_detail=%s\n' "$hook_out"
  printf 'hook_trust_hint=run /hooks in Codex CLI and trust changed agent harness hooks\n'
  return 1
}

if ! hook_trust_check; then
  fails=$((fails + 1))
fi

# hooks.json: harness projection symlink, or a real file with matching content.
if [ -L "$CODEX_HOME/hooks.json" ] && [ "$(real "$CODEX_HOME/hooks.json")" = "$(real "$S/codex-hooks/hooks.json")" ]; then
  printf 'check=hooks-json:ok\n'
elif [ -f "$CODEX_HOME/hooks.json" ] && cmp -s "$CODEX_HOME/hooks.json" "$S/codex-hooks/hooks.json"; then
  printf 'check=hooks-json:ok reason=content-match\n'
else
  printf 'check=hooks-json:failed reason=not-harness-hook-projection\n'
  fails=$((fails + 1))
fi

detect_plugin_state() {
  if [ "${CODEX_RUNTIME_PROJECTION_SKIP_CLI_DISCOVERY:-0}" = "1" ]; then
    plugin_state=skipped_cli
    return 0
  fi
  if ! command -v codex >/dev/null 2>&1; then
    plugin_state=command_not_found
    return 0
  fi
  plugin_out=""
  if plugin_out=$(CODEX_HOME="$CODEX_HOME" timeout "$CLI_TIMEOUT" codex plugin list --json 2>/dev/null); then
    if printf '%s\n' "$plugin_out" | grep -q 'hearting-codex'; then
      plugin_state=installed
    else
      plugin_state=missing
    fi
  else
    plugin_rc=$?
    if [ "$plugin_rc" -eq 124 ] || [ "$plugin_rc" -eq 137 ]; then
      plugin_state=timeout
    else
      plugin_state=list_failed
    fi
  fi
}

print_plugin_check() {
  case "$plugin_state" in
    installed)
      printf 'check=plugin:ok\n'
      ;;
    missing)
      printf 'check=plugin:missing\n'
      printf 'plugin_install_1=codex plugin marketplace add %s/codex-plugin-marketplace\n' "$S"
      printf 'plugin_install_2=codex plugin add hearting-codex@hearting\n'
      printf 'plugin_hint=run install-runtime-projection.sh --install-plugin\n'
      ;;
    skipped_cli)
      printf 'check=plugin:skipped reason=codex-cli-discovery-skipped\n'
      ;;
    timeout)
      printf 'check=plugin:skipped reason=codex-cli-timeout timeout=%s\n' "$CLI_TIMEOUT"
      ;;
    list_failed)
      printf 'check=plugin:missing reason=codex-plugin-list-failed exit=%s\n' "${plugin_rc:-unknown}"
      printf 'plugin_install_1=codex plugin marketplace add %s/codex-plugin-marketplace\n' "$S"
      printf 'plugin_install_2=codex plugin add hearting-codex@hearting\n'
      printf 'plugin_hint=run install-runtime-projection.sh --install-plugin\n'
      ;;
    command_not_found|*)
      printf 'check=plugin:skipped reason=codex-command-not-found\n'
      ;;
  esac
}

plugin_state=unknown
plugin_rc=
detect_plugin_state

# Codex skill discovery may be native symlinks or the installable plugin. A
# count-only check can miss stale or wrong targets, so every projected skill is
# classified before selecting the active discovery surface.
if [ "$native_managed" -eq 1 ]; then
  linked_skills=$(find "$CODEX_HOME/skills" -mindepth 1 -maxdepth 1 -type l 2>/dev/null | wc -l | tr -d ' ')
  printf 'skills_linked=%s plugin_state=%s\n' "$linked_skills" "$plugin_state"
  printf 'check=skill-discovery:native\n'
  printf 'check=skills-linked:ok reason=runtime-activation-verified\n'
else
projected_skills=$(find -L "$S/codex-skills" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
linked_skills=$(find "$CODEX_HOME/skills" -mindepth 1 -maxdepth 1 -type l 2>/dev/null | wc -l | tr -d ' ')
printf 'skills_projected=%s skills_linked=%s plugin_state=%s\n' "$projected_skills" "$linked_skills" "$plugin_state"
skill_link_fails=0
skill_link_ok=0
skill_link_absent=0
for d in "$S/codex-skills"/*; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  linkpath="$CODEX_HOME/skills/$name"
  if [ -L "$linkpath" ]; then
    if [ -n "$(real "$linkpath")" ] && [ "$(real "$linkpath")" = "$(real "$d")" ]; then
      printf 'check=skill-link:%s:ok\n' "$name"
      skill_link_ok=$((skill_link_ok + 1))
    else
      printf 'check=skill-link:%s:failed reason=expected-symlink-to:%s\n' "$name" "$d"
      skill_link_fails=$((skill_link_fails + 1))
    fi
  elif [ -e "$linkpath" ]; then
    printf 'check=skill-link:%s:failed reason=non-symlink-exists\n' "$name"
    skill_link_fails=$((skill_link_fails + 1))
  else
    printf 'check=skill-link:%s:absent\n' "$name"
    skill_link_absent=$((skill_link_absent + 1))
  fi
done
if [ "$projected_skills" -eq 0 ]; then
  printf 'check=skill-discovery:failed reason=no-projected-skills\n'
  printf 'check=skills-linked:failed reason=no-projected-skills\n'
  fails=$((fails + 1))
elif [ "$skill_link_fails" -gt 0 ]; then
  printf 'check=skill-discovery:failed reason=harness-skills-miswired\n'
  printf 'check=skills-linked:failed reason=harness-skills-miswired\n'
  fails=$((fails + 1))
elif [ "$skill_link_ok" -eq "$projected_skills" ]; then
  if [ "$plugin_state" = installed ]; then
    printf 'check=skill-discovery:native duplicate-warning=plugin-also-installed\n'
  else
    printf 'check=skill-discovery:native\n'
  fi
  printf 'check=skills-linked:ok\n'
elif [ "$skill_link_absent" -eq "$projected_skills" ] && [ "$plugin_state" = installed ]; then
  printf 'check=skill-discovery:plugin plugin=installed\n'
  printf 'check=skills-linked:skipped reason=plugin-skill-discovery\n'
elif [ "$skill_link_absent" -eq "$projected_skills" ]; then
  printf 'check=skill-discovery:failed reason=no-native-skill-links-and-plugin-unavailable plugin_state=%s\n' "$plugin_state"
  printf 'check=skills-linked:failed reason=harness-skills-not-linked-and-plugin-unavailable\n'
  fails=$((fails + 1))
else
  printf 'check=skill-discovery:failed reason=partial-native-skill-links ok=%s absent=%s projected=%s\n' "$skill_link_ok" "$skill_link_absent" "$projected_skills"
  printf 'check=skills-linked:failed reason=partial-native-skill-links\n'
  fails=$((fails + 1))
fi
fi

# Native agent links: every projected custom-agent TOML must be linked to the
# matching adapter-owned file.
if [ "$native_managed" -eq 1 ]; then
  linked_agents=$(find "$CODEX_HOME/agents" -mindepth 1 -maxdepth 1 -type l 2>/dev/null | wc -l | tr -d ' ')
  printf 'agents_linked=%s\n' "$linked_agents"
  printf 'check=agents-linked:ok reason=runtime-activation-verified\n'
else
projected_agents=$(find -L "$S/codex-agents" -mindepth 1 -maxdepth 1 -type f -name '*.toml' 2>/dev/null | wc -l | tr -d ' ')
linked_agents=$(find "$CODEX_HOME/agents" -mindepth 1 -maxdepth 1 -type l 2>/dev/null | wc -l | tr -d ' ')
printf 'agents_projected=%s agents_linked=%s\n' "$projected_agents" "$linked_agents"
agent_link_fails=0
for f in "$S/codex-agents"/*.toml; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  linkpath="$CODEX_HOME/agents/$name"
  if [ -L "$linkpath" ] && [ -n "$(real "$linkpath")" ] && [ "$(real "$linkpath")" = "$(real "$f")" ]; then
    printf 'check=agent-link:%s:ok\n' "$name"
  else
    printf 'check=agent-link:%s:failed reason=expected-symlink-to:%s\n' "$name" "$f"
    agent_link_fails=$((agent_link_fails + 1))
  fi
done
if [ "$agent_link_fails" -eq 0 ] && [ "$projected_agents" -gt 0 ]; then
  printf 'check=agents-linked:ok\n'
else
  printf 'check=agents-linked:failed reason=harness-agents-not-linked-or-miswired\n'
  fails=$((fails + 1))
fi
fi

# Transparent interactive launcher. A private CODEX_HOME without an explicit
# HARNESS_BIN_DIR is an isolated projection fixture, not authority to inspect or
# replace the user's global CLI ingress.
launcher_json=$(python3 "$AGENT_HOME/tools/install/codex_launcher.py" status \
  --codex-home "$CODEX_HOME" --json 2>/dev/null || true)
launcher_fields=$(python3 - "$launcher_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    value=json.loads(sys.argv[1])
except (IndexError, json.JSONDecodeError):
    raise SystemExit(0)
print("installed=" + ("1" if value.get("installed") else "0"))
print("healthy=" + ("1" if value.get("healthy") else "0"))
print("detail=" + str(value.get("detail", "unknown")))
PY
)
launcher_installed=$(printf '%s\n' "$launcher_fields" | sed -n 's/^installed=//p')
launcher_healthy=$(printf '%s\n' "$launcher_fields" | sed -n 's/^healthy=//p')
launcher_detail=$(printf '%s\n' "$launcher_fields" | sed -n 's/^detail=//p')
default_codex_home=$HOME/.codex
if [ "${CODEX_RUNTIME_PROJECTION_SKIP_CLI_DISCOVERY:-0}" = "1" ]; then
  printf 'check=managed-launcher:skipped reason=codex-cli-discovery-skipped\n'
elif [ -z "${HARNESS_BIN_DIR:-}" ] && [ "$(real "$CODEX_HOME")" != "$(real "$default_codex_home")" ]; then
  printf 'check=managed-launcher:skipped reason=non-default-codex-home-without-harness-bin-dir\n'
elif [ "$launcher_healthy" = "1" ]; then
  printf 'check=managed-launcher:ok\n'
elif [ "$launcher_installed" = "1" ]; then
  printf 'check=managed-launcher:failed installed=%s reason=%s\n' \
    "${launcher_installed:-0}" "${launcher_detail:-status-unavailable}"
  fails=$((fails + 1))
elif command -v codex >/dev/null 2>&1; then
  printf 'check=managed-launcher:missing hint=run-runtime-refresh\n'
else
  printf 'check=managed-launcher:skipped reason=codex-command-not-found\n'
fi

# Bootstrap discovery (soft): requires the codex CLI. Headless preflight may
# intentionally skip this when `codex` is stubbed for launch testing.
if [ "${CODEX_RUNTIME_PROJECTION_SKIP_CLI_DISCOVERY:-0}" = "1" ]; then
  printf 'check=bootstrap:skipped reason=codex-cli-discovery-skipped\n'
elif command -v codex >/dev/null 2>&1; then
  bootstrap_out=""
  if bootstrap_out=$(CODEX_HOME="$CODEX_HOME" timeout "$CLI_TIMEOUT" codex debug prompt-input 'agent harness runtime projection check' 2>/dev/null); then
    if printf '%s\n' "$bootstrap_out" | grep -q 'AGENTS.md — Codex Adapter Bootstrap'; then
      printf 'check=bootstrap:ok\n'
    else
      printf 'check=bootstrap:failed reason=harness-bootstrap-not-loaded\n'
      fails=$((fails + 1))
    fi
  else
    rc=$?
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
      printf 'check=bootstrap:skipped reason=codex-cli-timeout timeout=%s\n' "$CLI_TIMEOUT"
    else
      printf 'check=bootstrap:failed reason=codex-debug-failed exit=%s\n' "$rc"
      fails=$((fails + 1))
    fi
  fi
else
  printf 'check=bootstrap:skipped reason=codex-command-not-found\n'
fi
print_plugin_check

if [ "$fails" -eq 0 ]; then
  printf 'status=ok\n'
  exit 0
else
  printf 'status=failed fails=%s\n' "$fails"
  exit 1
fi
