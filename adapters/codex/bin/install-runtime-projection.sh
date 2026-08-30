#!/usr/bin/env sh
# install-runtime-projection.sh — wire the Codex runtime home ($CODEX_HOME,
# default $HOME/.codex) to the portable harness projection under
# codex_setting/. Idempotent: re-running only refreshes harness-owned symlinks.
#
# It NEVER touches Codex-owned credentials, sessions, history, logs, caches,
# config.toml, or local databases. It creates/refreshes the harness `agent-*`
# pointers, `hooks.json`, selected per-skill / per-agent symlinks, and the
# one-time user model default, and the manifest-backed interactive launcher in
# the user's harness bin directory. A
# pre-existing real `hooks.json` is backed up once to `hooks.json.pre-harness`
# before it is replaced by the projection symlink. The launcher records the real
# Codex binding and restores it through `harness uninstall codex`.
#
# Plugin install is opt-in: pass --install-plugin to run the `codex plugin`
# commands; otherwise the plugin is left untouched (the checker reports it). Skill
# discovery is selectable: native symlinks, plugin, or both. When --install-plugin
# is used and --skills-mode is omitted, plugin mode is selected to avoid duplicate
# skill metadata in Codex's initial context.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
agent_home_candidate=${AGENT_HOME:-}
if [ -z "$agent_home_candidate" ] || [ ! -f "$agent_home_candidate/core/CORE.md" ]; then
  agent_home_candidate=$SCRIPT_DIR/../../..
fi
# Resolve the source before refreshing $CODEX_HOME/hearting.  Managed sessions
# commonly export AGENT_HOME through that projection; keeping the lexical path
# would otherwise turn `hearting -> <source>` into `hearting -> hearting`.
if ! AGENT_HOME=$(CDPATH= cd -- "$agent_home_candidate" && pwd -P); then
  echo "install-runtime-projection: cannot resolve agent home: $agent_home_candidate" >&2
  exit 69
fi
CODEX_HOME=${CODEX_HOME:-$HOME/.codex}

install_plugin=0
skills_mode=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-plugin)
      install_plugin=1
      shift
      ;;
    --skills-mode)
      [ "$#" -ge 2 ] || { echo "install-runtime-projection: --skills-mode requires native|plugin|both" >&2; exit 64; }
      skills_mode=$2
      shift 2
      ;;
    --skills-mode=*)
      skills_mode=${1#--skills-mode=}
      shift
      ;;
    -h|--help)
      cat <<EOF
usage: install-runtime-projection.sh [--install-plugin] [--skills-mode native|plugin|both]

Wires \$CODEX_HOME (default \$HOME/.codex) to the harness projection in
codex_setting/. Idempotent; never mutates Codex-owned runtime state.
  --install-plugin             also run codex plugin marketplace add + plugin add
  --skills-mode native|plugin|both
                               choose Codex skill discovery surface. Default: native;
                               with --install-plugin: plugin.
EOF
      exit 0 ;;
    *) echo "install-runtime-projection: unknown option: $1" >&2; exit 64 ;;
  esac
done

if [ -z "$skills_mode" ]; then
  if [ "$install_plugin" = "1" ]; then
    skills_mode=plugin
  else
    skills_mode=native
  fi
fi
case "$skills_mode" in
  native|plugin|both) ;;
  *) echo "install-runtime-projection: --skills-mode must be native, plugin, or both" >&2; exit 64 ;;
esac

if [ ! -d "$AGENT_HOME/codex_setting" ]; then
  echo "install-runtime-projection: codex_setting projection missing under $AGENT_HOME" >&2
  exit 69
fi

mkdir -p "$CODEX_HOME"

# link <target> <linkpath>: refresh a harness-owned symlink. Refuses to clobber a
# real (non-symlink) file or directory, except hooks.json which is backed up.
link() {
  target=$1
  linkpath=$2
  if [ ! -e "$target" ]; then
    printf 'skip=%s reason=projection-target-missing\n' "$linkpath"
    return 0
  fi
  if ! link_parent=$(CDPATH= cd -- "$(dirname -- "$linkpath")" && pwd -P); then
    printf 'install-runtime-projection: cannot resolve link parent: %s\n' "$linkpath" >&2
    return 3
  fi
  link_identity=$link_parent/$(basename -- "$linkpath")
  if [ "$target" = "$link_identity" ]; then
    printf 'install-runtime-projection: refusing self-referential link: %s\n' "$linkpath" >&2
    return 3
  fi
  if [ -e "$linkpath" ] && [ ! -L "$linkpath" ]; then
    if [ "$(basename "$linkpath")" = "hooks.json" ]; then
      [ -e "$linkpath.pre-harness" ] || cp "$linkpath" "$linkpath.pre-harness"
      rm -f "$linkpath"
    else
      printf 'skip=%s reason=non-symlink-exists\n' "$linkpath"
      return 0
    fi
  fi
  ln -sfn "$target" "$linkpath"
  printf 'link=%s\n' "$linkpath"
}

real() { readlink -f "$1" 2>/dev/null || true; }

S="$AGENT_HOME/codex_setting"

# Stable harness pointers consumed by the Codex adapter at runtime.
link "$AGENT_HOME"                       "$CODEX_HOME/hearting"
link "$S/AGENTS.md"                      "$CODEX_HOME/AGENTS.md"
link "$S/README.md"                      "$CODEX_HOME/hearting-readme.md"
link "$S/core"                           "$CODEX_HOME/agent-core"
link "$S/capabilities"                   "$CODEX_HOME/agent-capabilities"
link "$S/roles"                          "$CODEX_HOME/agent-roles"
link "$S/bin"                            "$CODEX_HOME/agent-bin"
link "$S/tools"                          "$CODEX_HOME/agent-tools"
link "$S/utilities"                      "$CODEX_HOME/agent-utilities"
link "$S/scaffolds"                      "$CODEX_HOME/agent-scaffolds"
link "$S/codex-skills"                   "$CODEX_HOME/agent-skills"
link "$S/codex-modes"                    "$CODEX_HOME/agent-modes"
link "$S/codex-agents"                   "$CODEX_HOME/agent-agents"
link "$S/codex-plugin-marketplace"       "$CODEX_HOME/agent-plugin-marketplace"
link "$S/codex-hooks"                    "$CODEX_HOME/agent-hooks"
link "$S/codex-hooks/hooks.json"         "$CODEX_HOME/hooks.json"

model_config_result=$(python3 "$AGENT_HOME/tools/install/user_model_config.py" \
  --adapter codex \
  --source "$AGENT_HOME/adapters/codex/config/models.conf" \
  --runtime-home "$CODEX_HOME") || {
    printf '%s\n' "$model_config_result" >&2
    echo "install-runtime-projection: user model config seed failed" >&2
    exit 3
  }
printf 'model_config=%s\n' "$model_config_result"

# Native Codex skill discovery: one symlink per generated skill directory, or
# plugin-only discovery with stale harness-owned skill symlinks removed.
skills_linked=0
skills_unlinked=0
mkdir -p "$CODEX_HOME/skills"
case "$skills_mode" in
  native|both)
    for d in "$S/codex-skills"/*; do
      [ -d "$d" ] || continue
      ln -sfn "$d" "$CODEX_HOME/skills/$(basename "$d")"
      skills_linked=$((skills_linked + 1))
    done
    ;;
  plugin)
    for d in "$S/codex-skills"/*; do
      [ -d "$d" ] || continue
      linkpath="$CODEX_HOME/skills/$(basename "$d")"
      if [ -L "$linkpath" ] && [ -n "$(real "$linkpath")" ] && [ "$(real "$linkpath")" = "$(real "$d")" ]; then
        rm -f "$linkpath"
        skills_unlinked=$((skills_unlinked + 1))
      fi
    done
    ;;
esac
printf 'skills_mode=%s\n' "$skills_mode"
printf 'skills_linked=%s\n' "$skills_linked"
[ "$skills_mode" = "plugin" ] && printf 'skills_unlinked=%s\n' "$skills_unlinked"

# Native Codex custom-agent discovery: one symlink per generated TOML.
agents_linked=0
mkdir -p "$CODEX_HOME/agents"
for f in "$S/codex-agents"/*.toml; do
  [ -f "$f" ] || continue
  ln -sfn "$f" "$CODEX_HOME/agents/$(basename "$f")"
  agents_linked=$((agents_linked + 1))
done
printf 'agents_linked=%s\n' "$agents_linked"

if [ "$install_plugin" = "1" ]; then
  if command -v codex >/dev/null 2>&1; then
    CODEX_HOME="$CODEX_HOME" codex plugin marketplace add "$S/codex-plugin-marketplace" >/dev/null 2>&1 || true
    CODEX_HOME="$CODEX_HOME" codex plugin add hearting-codex@hearting >/dev/null 2>&1 || true
    printf 'plugin_install=attempted\n'
  else
    printf 'plugin_install=skipped reason=codex-command-not-found\n'
  fi
else
  printf 'plugin_install=not-requested hint=rerun-with---install-plugin\n'
fi

default_codex_home=$HOME/.codex
if [ -z "${HARNESS_BIN_DIR:-}" ] && [ "$(real "$CODEX_HOME")" != "$(real "$default_codex_home")" ]; then
  # A temporary/private CODEX_HOME must not silently replace the user's global
  # CLI ingress. Operators can opt in with an explicit HARNESS_BIN_DIR.
  printf 'managed_launcher=skipped reason=non-default-codex-home-without-harness-bin-dir\n'
elif command -v codex >/dev/null 2>&1; then
  launcher_result=$(python3 "$AGENT_HOME/tools/install/codex_launcher.py" install \
    --codex-home "$CODEX_HOME" --json) || {
      printf '%s\n' "$launcher_result" >&2
      echo "install-runtime-projection: managed Codex launcher installation failed" >&2
      exit 3
    }
  printf 'managed_launcher=%s\n' "$launcher_result"
else
  printf 'managed_launcher=skipped-unavailable reason=codex-command-not-found\n'
fi

printf 'status=ok\n'
printf 'codex_home=%s\n' "$CODEX_HOME"
