#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v git >/dev/null 2>&1 && ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null); then
  :
else
  ROOT=$SCRIPT_DIR
  while [ "$ROOT" != "/" ] && [ ! -f "$ROOT/core/CORE.md" ]; do
    ROOT=$(CDPATH= cd -- "$ROOT/.." && pwd)
  done
  if [ ! -f "$ROOT/core/CORE.md" ]; then
    ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
  fi
fi
cd "$ROOT"

fail=0
# Shared pre-owner evidence generator is invoked through adapter preflight
# wrappers and remains at the portable root; no adapter-local utility symlink.
# memory-store.sh joins this list rather than the projected one: it is the
# resolver the canonical memory hooks read their STORE from, and Codex/OpenCode
# reach those hooks at $ROOT/hooks/* (adapters/codex/bin/preflight.sh), where
# $ROOT/utilities/memory-store.sh already resolves. A per-adapter copy would
# assert a projection surface nothing loads.
SHARED_UTILITY_DEFERRED="artifact-pointer-bridge.py artifact-quiescence.py artifact-relocation.py artifact-relocation-live.py artifact-knowledge-feed.py cairn-artifact-read.sh cairn-artifact-read.ts dispatch-readiness.py verification-background-lease.py memory-store.sh compute-hosts"

say() {
  printf '%s\n' "$*"
}

fail_msg() {
  say "FAIL: $*"
  fail=1
}

CLAUDE_NATIVE_SURFACE_PATTERN='adapters/claude|claude_setting|settings\.json|statusline\.sh|CLAUDE\.md|CLAUDE_HOME|agent-modes|allowedTools|(^|[^[:alnum:]_/.-])skills/|/\.claude/'
NON_CODEX_DESIGN_SURFACE_PATTERN='Design MCP|mcp__design__|tools/design-mcp|<agent-home>/tools/design-mcp|getConsoleLogs|eval_js|preview\(\{ path \}\)'

# codex-adapter-parity audit P-19 (2026-07-04): module-level derivation of the portable hook-event
# domain from adapters/claude/settings.json ".hooks" top-level keys. MUST stay module-scope (not
# function-local) because the runner invokes the consumer check_install_layout_codex_projection
# BEFORE the producer check_codex_bin_wrappers — a function-local var would be unbound under set -u.
EVENTS=$(python3 -c '
import json
d = json.load(open("adapters/claude/settings.json"))
for k in d.get("hooks", {}).keys():
    print(k)
' 2>/dev/null || true)
if [ -z "$EVENTS" ]; then
  fail_msg "could not derive hook EVENTS domain from adapters/claude/settings.json .hooks (empty extraction)"
fi
# HOOK_EVENT_EXEMPT: events in $EVENTS explicitly not required to have a Codex hooks.json bridge /
# INSTALL_LAYOUT.md doc entry. PostToolUseFailure is Claude-native; the current Codex native hook
# surface has no verified matching event, so projecting a bridge would overclaim parity. Module-level so both consumer sites (check_install_layout_codex_projection,
# check_codex_bin_wrappers) can read it regardless of registration order.
HOOK_EVENT_EXEMPT="PostToolUseFailure"

is_mechanized_install_layout() {
  grep -Fq 'tools/install/harness.sh' INSTALL_LAYOUT.md 2>/dev/null
}

check_mechanized_install_projection() {
  runtime=$1
  expected_ids=$2
  out="${TMPDIR:-/tmp}/hearting-install-plan-$runtime-$$.json"
  test_home="${TMPDIR:-/tmp}/hearting-layout-home"

  if [ ! -x tools/install/harness.sh ]; then
    fail_msg "tools/install/harness.sh must be executable because the installed harness launcher points to it"
    return
  fi
  if ! grep -Fq "harness install $runtime" INSTALL_LAYOUT.md \
    || ! grep -Fq 'harness verify  [claude|codex|opencode|all] --json' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must route $runtime installation and verification through the mechanized harness CLI"
    return
  fi
  if ! grep -Fq 'def checks(scope="global")' "tools/install/drivers/$runtime.py"; then
    fail_msg "tools/install/drivers/$runtime.py must provide the read-only verify checks replacing the old manual INSTALL_LAYOUT recipe"
    return
  fi

  if ! AGENT_HOME="$ROOT" HOME="$test_home" \
    CODEX_HOME="$test_home/.codex" CLAUDE_CONFIG_DIR="$test_home/.claude" \
    XDG_CONFIG_HOME="$test_home/.config" \
    tools/install/harness.sh install "$runtime" --dry-run --json > "$out"; then
    fail_msg "mechanized $runtime install dry-run failed"
    rm -f "$out"
    return
  fi
  if ! python3 - "$out" "$expected_ids" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
checks = {item.get("id"): item for item in data.get("checks", [])}
expected = [item for item in sys.argv[2].split() if item]
missing = [item for item in expected if item not in checks]
failed = [item for item in data.get("checks", []) if not item.get("ok")]
if data.get("exit") != 0 or missing or failed:
    print(f"exit={data.get('exit')} missing={missing} failed={failed}", file=sys.stderr)
    raise SystemExit(1)
PY
  then
    fail_msg "mechanized $runtime install plan is missing required projection actions"
  fi
  rm -f "$out"
}

check_no_claude_native_refs() {
  path=$1
  label=$2
  bad=$(rg -n "$CLAUDE_NATIVE_SURFACE_PATTERN" "$path" 2>/dev/null || true)
  if [ -n "$bad" ]; then
    fail_msg "$label must not reference Claude-native surfaces:"
    printf '%s\n' "$bad"
    return 1
  fi
  return 0
}

check_projection_symlinks() {
  dir=$1
  [ -d "$dir" ] || { fail_msg "$dir is missing"; return; }

  non_links=$(find "$dir" -mindepth 1 -maxdepth 1 ! -type l -print)
  if [ -n "$non_links" ]; then
    fail_msg "$dir contains non-symlink projection entries:"
    printf '%s\n' "$non_links"
  fi
}

check_projection_entry_allowlist() {
  dir=$1
  shift
  allowed=" $* "
  [ -d "$dir" ] || return

  entries=$(find "$dir" -mindepth 1 -maxdepth 1 -exec basename {} \; 2>/dev/null || true)
  for entry in $entries; do
    case "$allowed" in
      *" $entry "*) ;;
      *) fail_msg "$dir/$entry is not an approved projection entry" ;;
    esac
  done
}

check_codex_forbidden_entries() {
  for p in CLAUDE.md settings.json keybindings.json commands statusline.sh skills agents agent-modes hooks; do
    if [ -e "codex_setting/$p" ] || [ -L "codex_setting/$p" ]; then
      fail_msg "codex_setting/$p exists; Codex projection must not expose Claude-native surfaces"
    fi
  done
}

check_codex_native_surface_debt() {
  for p in adapters/codex/.codex-plugin codex_setting/plugins codex_setting/.codex-plugin adapters/codex/prompts codex_setting/prompts; do
    if [ -e "$p" ] || [ -L "$p" ]; then
      fail_msg "$p exists; Codex must not expose unsupported native surfaces outside documented adapter-owned projections"
    fi
  done

  if grep -Fq 'Codex has no adapter-owned native agent projection yet' adapters/codex/README.md; then
    fail_msg "adapters/codex/README.md must not describe Codex-native agents as future-only"
  fi
}

check_opencode_forbidden_entries() {
  for p in CLAUDE.md settings.json keybindings.json commands statusline.sh skills agents agent-modes hooks; do
    if [ -e "opencode_setting/$p" ] || [ -L "opencode_setting/$p" ]; then
      fail_msg "opencode_setting/$p exists; OpenCode projection must not expose Claude-native surfaces"
    fi
  done
}

check_required_projection_entries() {
  for p in AGENTS.md README.md core capabilities roles bin tools utilities scaffolds codex-skills codex-modes codex-hooks codex-config codex-agents; do
    if [ ! -L "codex_setting/$p" ]; then
      fail_msg "codex_setting/$p must be a symlink projection entry"
    fi
  done
}

check_codex_projection_targets() {
  check_link_target codex_setting/AGENTS.md ../adapters/codex/AGENTS.md
  check_link_target codex_setting/README.md ../adapters/codex/README.md
  check_link_target codex_setting/core ../core
  check_link_target codex_setting/capabilities ../capabilities
  check_link_target codex_setting/roles ../roles
  check_link_target codex_setting/bin ../adapters/codex/bin
  check_link_target codex_setting/tools ../adapters/codex/tools
  check_link_target codex_setting/utilities ../adapters/codex/utilities
  check_link_target codex_setting/scaffolds ../adapters/codex/scaffolds
  check_link_target codex_setting/codex-skills ../adapters/codex/skills
  check_link_target codex_setting/codex-modes ../adapters/codex/modes
  check_link_target codex_setting/codex-hooks ../adapters/codex/hooks
  check_link_target codex_setting/codex-config ../adapters/codex/config
  check_link_target codex_setting/codex-agents ../adapters/codex/agents
}

check_opencode_required_projection_entries() {
  for p in AGENTS.md README.md core capabilities roles bin tools utilities opencode-skills opencode-agents opencode-commands opencode-plugins; do
    if [ ! -L "opencode_setting/$p" ]; then
      fail_msg "opencode_setting/$p must be a symlink projection entry"
    fi
  done
}

check_opencode_projection_targets() {
  check_link_target opencode_setting/AGENTS.md ../adapters/opencode/AGENTS.md
  check_link_target opencode_setting/README.md ../adapters/opencode/README.md
  check_link_target opencode_setting/core ../core
  check_link_target opencode_setting/capabilities ../capabilities
  check_link_target opencode_setting/roles ../roles
  check_link_target opencode_setting/bin ../adapters/opencode/bin
  check_link_target opencode_setting/tools ../adapters/opencode/tools
  check_link_target opencode_setting/utilities ../adapters/opencode/utilities
  check_link_target opencode_setting/opencode-skills ../adapters/opencode/skills
  check_link_target opencode_setting/opencode-agents ../adapters/opencode/agents
  check_link_target opencode_setting/opencode-commands ../adapters/opencode/commands
  check_link_target opencode_setting/opencode-plugins ../adapters/opencode/plugins
}

check_non_claude_projection_runtime_caches() {
  cache_paths=$(find adapters/codex adapters/opencode codex_setting opencode_setting \
    \( -type d -name __pycache__ -o -type f -name '*.py[co]' \) -print 2>/dev/null || true)
  if [ -n "$cache_paths" ]; then
    fail_msg "Codex/OpenCode adapter projections must not expose Python bytecode caches:"
    printf '%s\n' "$cache_paths"
  fi
}

check_codex_plugin_marketplace_projection_boundary() {
  root="adapters/codex/plugin-marketplace"
  marketplace="$root/.agents/plugins/marketplace.json"
  plugin_link="$root/plugins/hearting-codex"

  if [ ! -d "$root" ]; then
    fail_msg "$root is missing"
    return
  fi
  entries=$(find "$root" -mindepth 1 -maxdepth 1 -exec basename {} \; 2>/dev/null || true)
  for entry in $entries; do
    case "$entry" in
      .agents|plugins) ;;
      *) fail_msg "$root/$entry is not an approved Codex plugin marketplace entry" ;;
    esac
  done
  if [ ! -f "$marketplace" ]; then
    fail_msg "$marketplace is missing"
  fi
  if [ ! -L "$plugin_link" ]; then
    fail_msg "$plugin_link must project the concrete Codex plugin"
  elif [ "$(readlink "$plugin_link")" != "../../plugins/hearting-codex" ]; then
    fail_msg "$plugin_link points to $(readlink "$plugin_link"); expected ../../plugins/hearting-codex"
  fi
  if [ -e "$root/ADAPTATION.md" ] || [ -e "$root/bin" ] || [ -e "$root/hooks" ] || [ -e "$root/skills" ]; then
    fail_msg "$root must expose only the Codex marketplace layout, not the whole adapter"
  fi
  if ! grep -Fq '"path": "./plugins/hearting-codex"' "$marketplace"; then
    fail_msg "$marketplace must point at the marketplace-local plugin path"
  fi
}

check_claude_projection_targets() {
  check_link_target claude_setting/CLAUDE.md ../adapters/claude/CLAUDE.md
  check_link_target claude_setting/README.md ../README.md
  check_link_target claude_setting/core ../core
  check_link_target claude_setting/settings.json ../adapters/claude/settings.json
  check_link_target claude_setting/keybindings.json ../adapters/claude/keybindings.json
  check_link_target claude_setting/commands ../adapters/claude/commands
  check_link_target claude_setting/skills ../adapters/claude/skills
  check_link_target claude_setting/agents ../adapters/claude/agents
  # claude_setting/agent-modes retired 2026-07-22 (재홈): units project via roles/.
  check_link_target claude_setting/hooks ../adapters/claude/hooks
  check_link_target claude_setting/utilities ../adapters/claude/utilities
  check_link_target claude_setting/tools ../adapters/claude/tools
  check_link_target claude_setting/scaffolds ../adapters/claude/scaffolds
  check_link_target claude_setting/loops ../adapters/claude/loops
  check_link_target claude_setting/manifest.json ../manifest.json
  check_link_target claude_setting/statusline.sh ../adapters/claude/statusline.sh
  check_link_target claude_setting/bin ../adapters/claude/bin
}

check_claude_adapter_concrete_surfaces() {
  links=$(find adapters/claude -type l -print 2>/dev/null || true)
  # Ignore gitignored local installs. Canonical collapsed symlinks under shared
  # layers are valid at any depth when their relative targets resolve correctly.
  links=$(printf '%s\n' "$links" | while IFS= read -r l; do
    [ -n "$l" ] || continue
    git check-ignore -q "$l" 2>/dev/null && continue
    # Whole-layer collapse: the utilities domain is one dir-level symlink.
    if [ "$l" = "adapters/claude/utilities" ] \
      && [ "$(readlink "$l")" = "../../utilities" ]; then
      continue
    fi
    rel=${l#adapters/claude/}
    layer=${rel%%/*}
    sub=${rel#*/}
    case "$layer" in
      hooks|tools|utilities|loops|scaffolds)
        # Derive the relative depth from adapters/claude/<layer>/<sub>.
        _slashes=$(printf '%s' "$sub" | tr -cd '/' | wc -c)
        _up=$((3 + _slashes))
        _prefix=""; _i=0
        while [ "$_i" -lt "$_up" ]; do _prefix="../$_prefix"; _i=$((_i + 1)); done
        _want="${_prefix}${layer}/${sub}"
        if [ "$(readlink "$l")" = "$_want" ] \
          && [ -n "$(readlink -f "$l" 2>/dev/null)" ] \
          && [ "$(readlink -f "$l" 2>/dev/null)" = "$(readlink -f "$layer/$sub" 2>/dev/null)" ]; then
          continue
        fi
        ;;
    esac
    printf '%s\n' "$l"
  done)
  if [ -n "$links" ]; then
    fail_msg "adapters/claude symlinks must be canonical collapses (../../../<layer>/<name>) or adapter-owned concrete files:"
    printf '%s\n' "$links"
  fi
}

check_non_claude_adapter_symlink_boundaries() {
  for adapter in codex opencode; do
    links=$(find "adapters/$adapter" -type l -print 2>/dev/null || true)
    [ -n "$links" ] || continue
    for link in $links; do
      target=$(readlink "$link")
      case "$link:$target" in
        adapters/codex/plugin-marketplace/plugins/hearting-codex:../../plugins/hearting-codex|\
        adapters/codex/tools/memory/apply-distill-actions.py:../../../../tools/memory/apply-distill-actions.py|\
        adapters/codex/utilities/agent-worklog-state.sh:../../../utilities/agent-worklog-state.sh|\
        adapters/codex/utilities/artifact-root.sh:../../../utilities/artifact-root.sh|\
        adapters/codex/utilities/harness-status.sh:../../../utilities/harness-status.sh|\
        adapters/codex/utilities/worktree-cleanup.py:../../../utilities/worktree-cleanup.py|\
        adapters/codex/utilities/dispatch-route.sh:../../../utilities/dispatch-route.sh|\
        adapters/codex/utilities/dispatch-defaults.py:../../../utilities/dispatch-defaults.py|\
        adapters/codex/utilities/token-budget.py:../../../utilities/token-budget.py|\
        adapters/codex/utilities/token-budget-experiment.py:../../../utilities/token-budget-experiment.py|\
        adapters/codex/utilities/worker_bootstrap.py:../../../utilities/worker_bootstrap.py|\
        adapters/opencode/tools/memory/apply-distill-actions.py:../../../../tools/memory/apply-distill-actions.py|\
        adapters/opencode/utilities/agent-worklog-state.sh:../../../utilities/agent-worklog-state.sh|\
        adapters/opencode/utilities/artifact-root.sh:../../../utilities/artifact-root.sh|\
        adapters/opencode/utilities/harness-status.sh:../../../utilities/harness-status.sh|\
        adapters/opencode/utilities/worktree-cleanup.py:../../../utilities/worktree-cleanup.py|\
        adapters/opencode/utilities/dispatch-route.sh:../../../utilities/dispatch-route.sh|\
        adapters/opencode/utilities/dispatch-defaults.py:../../../utilities/dispatch-defaults.py|\
        adapters/opencode/utilities/worker_bootstrap.py:../../../utilities/worker_bootstrap.py)
          ;;
        *)
          fail_msg "$link points to $target; $adapter adapter symlinks must be explicitly allowlisted portable projections"
          ;;
      esac
      if [ ! -e "$link" ]; then
        fail_msg "$link points to $target; symlink target is missing"
      fi
      case "$target" in
        *adapters/claude*|*claude_setting*|*"/skills"*|*"/commands"*|*"/hooks"*)
          fail_msg "$link points to $target; $adapter adapter symlinks must not target Claude-native or compat surfaces"
          ;;
      esac
    done
  done
}

check_link_target() {
  path=$1
  expected=$2
  if [ ! -L "$path" ]; then
    fail_msg "$path must be a symlink to $expected"
    return
  fi
  target=$(readlink "$path")
  if [ "$target" != "$expected" ]; then
    fail_msg "$path points to $target; expected $expected"
  fi
}

# HLS-5 three-class contract for Claude shared layers:
# collapsed symlink, thin wrapper, or declared adapter delta. Concrete exceptions
# must appear in adaptation-exemptions.tsv.
EXEMPTIONS_FILE=tools/adaptation-exemptions.tsv

# CENSUS_DEFERRED lists temporary subtrees owned by another in-flight collapse.
# Remove entries when that work lands; this is not a permanent exemption.
CENSUS_DEFERRED=""

# Return true when an adapter path is under CENSUS_DEFERRED.
is_census_deferred() {
  for _d in $CENSUS_DEFERRED; do
    [ "$1" = "$_d" ] && return 0
    case "$1" in "$_d"/*) return 0;; esac
  done
  return 1
}

# Return wrapper, delta, or empty for an adapter path.
exemption_class() {
  [ -f "$EXEMPTIONS_FILE" ] || { printf ''; return; }
  awk -F'\t' -v p="$1" '$0 !~ /^[[:space:]]*#/ && $1 == p { print $2; exit }' "$EXEMPTIONS_FILE"
}

# Return a delta's canonical baseline hash, or '-'.
exemption_baseline() {
  [ -f "$EXEMPTIONS_FILE" ] || { printf '-'; return; }
  awk -F'\t' -v p="$1" '$0 !~ /^[[:space:]]*#/ && $1 == p { print ($4 == "" ? "-" : $4); exit }' "$EXEMPTIONS_FILE"
}

# Raw-byte SHA-256 with sha256sum-to-shasum fallback.
file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" 2>/dev/null | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1
  else
    printf ''
  fi
}

# Assert one shared file's three-class contract from layer and relative path.
assert_shared_adapter_class() {
  _layer=$1
  _name=$2
  _canonical="$_layer/$_name"
  _adapter="adapters/claude/$_layer/$_name"
  # Derive the relative target depth from the adapter path.
  _slashes=$(printf '%s' "$_name" | tr -cd '/' | wc -c)
  _up=$((3 + _slashes))
  _prefix=""
  _i=0
  while [ "$_i" -lt "$_up" ]; do _prefix="../$_prefix"; _i=$((_i + 1)); done
  _expected_target="${_prefix}${_layer}/${_name}"
  _class=$(exemption_class "$_adapter")

  if [ -L "$_adapter" ]; then
    if [ -n "$_class" ]; then
      fail_msg "$_adapter is a symlink but declared '$_class' in $EXEMPTIONS_FILE; exemptions (wrapper/delta) must stay concrete"
      return
    fi
    _target=$(readlink "$_adapter")
    if [ "$_target" != "$_expected_target" ]; then
      fail_msg "$_adapter collapses to $_target; expected $_expected_target (canonical)"
      return
    fi
    # Confirm that the runtime projection physically resolves to canonical.
    _resolved=$(readlink -f "$_adapter" 2>/dev/null || true)
    _want=$(readlink -f "$_canonical" 2>/dev/null || true)
    if [ -z "$_resolved" ] || [ "$_resolved" != "$_want" ]; then
      fail_msg "$_adapter does not resolve to canonical $_canonical (resolved: ${_resolved:-missing})"
    fi
    return
  fi

  # Concrete files require an explicit exemption; collapse is the default.
  case "$_class" in
    wrapper)
      if ! grep -Fq "exec \"\$AGENT_HOME/$_layer/$_name\"" "$_adapter"; then
        fail_msg "$_adapter is declared 'wrapper' but does not 'exec \"\$AGENT_HOME/$_layer/$_name\"' (canonical delegation form)"
      fi
      ;;
    delta)
      if cmp -s "$_canonical" "$_adapter"; then
        fail_msg "$_adapter is declared 'delta' but is byte-identical to canonical $_canonical; collapse it instead of exempting"
      fi
      # Bind each delta to its declared canonical raw-byte hash so canonical drift
      # forces deliberate re-derivation.
      _baseline=$(exemption_baseline "$_adapter")
      _live=$(file_sha256 "$_canonical")
      if [ -z "$_live" ]; then
        fail_msg "$_adapter delta baseline cannot be verified: no sha256 tool (sha256sum/shasum) available"
      elif ! printf '%s' "$_baseline" | grep -Eq '^[0-9a-f]{64}$'; then
        fail_msg "$_adapter is 'delta' but has no valid canonical baseline hash in $EXEMPTIONS_FILE (field 4='$_baseline'); expected sha256($_canonical)=$_live — run: python3 tools/build-manifest.py --sync-baselines"
      elif [ "$_baseline" != "$_live" ]; then
        fail_msg "$_adapter delta baseline DRIFT: canonical $_canonical is now $_live but $EXEMPTIONS_FILE binds $_baseline; canonical changed — re-derive the declared patch into $_adapter, then --sync-baselines"
      fi
      ;;
    *)
      fail_msg "$_adapter is a concrete copy of canonical $_canonical but is not declared in $EXEMPTIONS_FILE; collapse it to ../../../$_canonical or declare it wrapper/delta"
      ;;
  esac
}

check_install_layout_codex_projection() {
  [ -f INSTALL_LAYOUT.md ] || { fail_msg "INSTALL_LAYOUT.md is missing"; return; }

  if is_mechanized_install_layout; then
    check_mechanized_install_projection codex \
      "codex.symlink.hearting codex.symlink.AGENTS.md codex.symlink.hearting-readme.md codex.symlink.agent-core codex.symlink.agent-capabilities codex.symlink.agent-roles codex.symlink.agent-bin codex.symlink.agent-tools codex.symlink.agent-utilities codex.symlink.agent-scaffolds codex.symlink.agent-skills codex.symlink.agent-modes codex.symlink.agent-agents codex.symlink.agent-hooks codex.seed_once.models.conf codex.symlink.hooks.json"
    return
  fi

  for p in AGENTS.md README.md core capabilities roles bin tools utilities scaffolds codex-skills codex-modes codex-hooks codex-config codex-agents; do
    if ! grep -Fq "\$AGENT_HOME/codex_setting/$p" INSTALL_LAYOUT.md; then
      fail_msg "INSTALL_LAYOUT.md must include Codex projection install step for codex_setting/$p"
    fi
  done
  if ! grep -Fq 'ln -sfn "$AGENT_HOME" "$HOME/.codex/hearting"' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must install the Codex hook command hearting pointer"
  fi
  if ! grep -Fq "non_claude_runtime_re='adapters/claude|claude_setting|settings\\.json|statusline\\.sh|CLAUDE\\.md|agent-modes|allowedTools|/\\.claude/'" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must define a shared non-Claude runtime output deny regex"
  fi

  for p in settings.json commands skills agents statusline.sh hooks; do
    if ! grep -Fq "$p" INSTALL_LAYOUT.md; then
      fail_msg "INSTALL_LAYOUT.md must explicitly keep Claude-native $p out of Codex runtime projection"
    fi
  done

  if ! grep -Fq '.codex/agents/' INSTALL_LAYOUT.md \
    || ! grep -Fq 'developer_instructions = """' INSTALL_LAYOUT.md \
    || ! grep -Fq "Path(sys.argv[1]).glob(\"*.toml\")" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must document and validate Codex native custom agent projection"
  fi
  if ! grep -Fq 'tmp_codex_bootstrap_home=' INSTALL_LAYOUT.md \
    || ! grep -Fq 'codex_setting/AGENTS.md' INSTALL_LAYOUT.md \
    || ! grep -Fq "codex debug prompt-input 'bootstrap check' >/tmp/codex-bootstrap.json" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg 'AGENTS.md — Codex Adapter Bootstrap' /tmp/codex-bootstrap.json" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg 'codex_setting/codex-hooks' /tmp/codex-bootstrap.json" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex bootstrap projection through codex debug prompt-input"
  fi
  if ! grep -Fq 'tmp_codex_hook_home=' INSTALL_LAYOUT.md \
    || ! grep -Fq 'codex_setting/codex-hooks/hooks.json' INSTALL_LAYOUT.md \
    || ! grep -Fq 'sessionend-lifecycle.py' INSTALL_LAYOUT.md \
    || ! grep -Fq 'permissionrequest-lifecycle.py' INSTALL_LAYOUT.md \
    || ! grep -Fq 'posttooluse-read-marker.py' INSTALL_LAYOUT.md \
    || ! grep -Fq '! rg "$non_claude_runtime_re" "$tmp_codex_hook_home/hooks.json"' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex native hook projection installation"
  fi
  # codex-adapter-parity audit P-19 (2026-07-04): derive the doc-literal hook-event completeness
  # check from the SAME module-level $EVENTS var (not a re-extraction) instead of a second hardcoded
  # 7-event list, so a newly added Claude hook event that INSTALL_LAYOUT.md fails to document also
  # fails loud (closes the second P-19 leak window besides check_codex_bin_wrappers at line ~789).
  for event in $EVENTS; do
    case " $HOOK_EVENT_EXEMPT " in
      *" $event "*) continue ;;
    esac
    if ! grep -Fq "\"$event\"" INSTALL_LAYOUT.md; then
      fail_msg "INSTALL_LAYOUT.md must document Codex native hook projection for event $event"
    fi
  done
  if ! grep -Fq '! rg "$non_claude_runtime_re" /tmp/codex-skills.json' INSTALL_LAYOUT.md \
    || ! grep -Fq '! rg "$non_claude_runtime_re" /tmp/codex-plugin-skills.json' INSTALL_LAYOUT.md \
    || ! grep -Fq '! rg "$non_claude_runtime_re" "$tmp_codex_agent_home/agents"' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex runtime projection outputs with the shared non-Claude deny regex"
  fi
  if ! grep -Fq 'codex_setting/bin/preflight.sh capability-info autopilot-code >/tmp/codex-capability.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^native_skill_path=adapters/codex/skills/autopilot-code/SKILL.md$' /tmp/codex-capability.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^native_plugin_skill_path=adapters/codex/plugins/hearting-codex/skills/autopilot-code/SKILL.md$' /tmp/codex-capability.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^compat_reference=not-projected$' /tmp/codex-capability.txt" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex capability-info native projections without root Skill compat references"
  fi
  if ! grep -Fq 'codex_setting/bin/preflight.sh role fast reviewer >/tmp/codex-role.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^adapter=codex$' /tmp/codex-role.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^source=roles/README.md$' /tmp/codex-role.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^family=fast$' /tmp/codex-role.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'codex_setting/bin/preflight.sh mode-info dev/backend >/tmp/codex-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^adapter=codex$' /tmp/codex-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^status=portable$' /tmp/codex-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^native_mode_path=adapters/codex/modes/dev/backend.md$' /tmp/codex-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -f codex_setting/codex-modes/dev/backend.md' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex role and mode mapping surfaces"
  fi
  if ! grep -Fq 'codex_setting/bin/preflight.sh visual-harness >/tmp/codex-visual-contract.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^adapter=codex$' /tmp/codex-visual-contract.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-visual-harness$' /tmp/codex-visual-contract.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x codex_setting/tools/design/visual-harness.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex visual harness projection"
  fi
  if ! grep -Fq 'codex_setting/bin/preflight.sh loop-info drill >/tmp/codex-loop-drill.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^status=manual-contract$' /tmp/codex-loop-drill.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^auto_run=unsupported$' /tmp/codex-loop-drill.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'codex_setting/bin/preflight.sh loop-info study >/tmp/codex-loop-study.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^action=proposal-report-only$' /tmp/codex-loop-study.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^fallback=read-source-and-draft-proposal-in-main-session$' /tmp/codex-loop-study.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'codex_setting/bin/preflight.sh loop-info note >/tmp/codex-loop-note.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^status=unsupported$' /tmp/codex-loop-note.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^fallback=worklog-board-or-manual-post-it-flow$' /tmp/codex-loop-note.txt" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex loop-info manual/unsupported contracts"
  fi
  if ! grep -Fq 'codex_setting/bin/preflight.sh distill-propose install-check "$PWD" >/tmp/codex-distill-propose.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^status=tool-contract$' /tmp/codex-distill-propose.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^reason=distill-proposal-disabled$' /tmp/codex-distill-propose.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^enable=CODEX_DISTILL_ENABLE=1$' /tmp/codex-distill-propose.txt" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex distill-propose disabled tool-contract"
  fi
  if ! grep -Fq 'codex_setting/bin/preflight.sh mode-info material/browser-fetch >/tmp/codex-browser-fetch-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=browser-fetch$' /tmp/codex-browser-fetch-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-browser-fetch$' /tmp/codex-browser-fetch-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^native_mode_path=adapters/codex/modes/material/browser-fetch.md$' /tmp/codex-browser-fetch-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x codex_setting/tools/material/browser-fetch.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex material browser-fetch projection"
  fi
  # codex-adapter-parity audit P-42 (2026-07-04): fold the four IDENTICAL-shape material tool doc
  # checks (data-script, figure-gen, pdf-extract, web-image-search) into a derived loop templated by
  # $tool. browser-fetch is kept as its own separate block above — it carries a 5th `native_mode_path`
  # assertion the other four do not, so it must not be swept into this fold (F3, behavior-preserving).
  for tool in data-script figure-gen pdf-extract web-image-search; do
    if ! grep -Fq "codex_setting/bin/preflight.sh mode-info material/$tool >/tmp/codex-$tool-mode.txt" INSTALL_LAYOUT.md \
      || ! grep -Fq "rg '^tool_contract=$tool\$' /tmp/codex-$tool-mode.txt" INSTALL_LAYOUT.md \
      || ! grep -Fq "rg '^runtime_surface=adapter-owned-$tool\$' /tmp/codex-$tool-mode.txt" INSTALL_LAYOUT.md \
      || ! grep -Fq "test -x codex_setting/tools/material/$tool.sh" INSTALL_LAYOUT.md; then
      fail_msg "INSTALL_LAYOUT.md must validate Codex material $tool projection"
    fi
  done
  if ! grep -Fq 'codex_setting/bin/preflight.sh mode-info qa/test >/tmp/codex-test-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=verification-runner$' /tmp/codex-test-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-verification-runner$' /tmp/codex-test-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^native_mode_path=adapters/codex/modes/qa/test.md$' /tmp/codex-test-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x codex_setting/tools/qa/verification-runner.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex QA verification runner projection"
  fi
  if ! grep -Fq 'codex_setting/bin/preflight.sh mode-info research/claim-verify >/tmp/codex-claim-verify-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=external-claim-verification$' /tmp/codex-claim-verify-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-claim-verify$' /tmp/codex-claim-verify-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x codex_setting/tools/research/claim-verify.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate Codex research claim-verify projection"
  fi
}

check_install_layout_opencode_projection() {
  [ -f INSTALL_LAYOUT.md ] || { fail_msg "INSTALL_LAYOUT.md is missing"; return; }

  if is_mechanized_install_layout; then
    check_mechanized_install_projection opencode \
      "opencode.symlink.hearting opencode.symlink.agent-agents.md opencode.symlink.hearting-readme.md opencode.symlink.agent-core opencode.symlink.agent-capabilities opencode.symlink.agent-roles opencode.symlink.agent-bin opencode.symlink.agent-tools opencode.symlink.agent-utilities opencode.symlink.agent-skills opencode.symlink.agent-agents opencode.symlink.agent-commands opencode.symlink.hearting-guards.js"
    return
  fi

  for p in AGENTS.md README.md core capabilities roles bin tools utilities opencode-skills opencode-agents opencode-commands opencode-plugins; do
    if ! grep -Fq "\$AGENT_HOME/opencode_setting/$p" INSTALL_LAYOUT.md; then
      fail_msg "INSTALL_LAYOUT.md must include OpenCode projection install step for opencode_setting/$p"
    fi
  done
  if ! grep -Fq 'ln -sfn "$AGENT_HOME" "$HOME/.config/opencode/hearting"' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must install the OpenCode hearting pointer"
  fi

  if ! grep -Fq 'OPENCODE_CONFIG_CONTENT=' INSTALL_LAYOUT.md \
    || ! grep -Fq 'opencode_setting/opencode-skills' INSTALL_LAYOUT.md \
    || ! grep -Fq 'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode skills through adapter-owned paths with Claude compat autoload disabled"
  fi
  if ! grep -Fq 'tmp_opencode_bootstrap_home=' INSTALL_LAYOUT.md \
    || ! grep -Fq 'OPENCODE_CONFIG_CONTENT="{\"instructions\"' INSTALL_LAYOUT.md \
    || ! grep -Fq '$PWD/opencode_setting/AGENTS.md' INSTALL_LAYOUT.md \
    || ! grep -Fq 'opencode debug config --pure >/tmp/opencode-bootstrap.json' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg 'opencode_setting/AGENTS.md' /tmp/opencode-bootstrap.json" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg 'opencode_setting/opencode-skills' /tmp/opencode-bootstrap.json" INSTALL_LAYOUT.md \
    || ! grep -Fq "! rg '/.claude/' /tmp/opencode-bootstrap.json" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode bootstrap instructions and skill path config"
  fi
  if ! grep -Fq 'opencode_setting/opencode-plugins/hearting-guards.js' INSTALL_LAYOUT.md \
    || ! grep -Fq 'opencode debug config >/tmp/opencode-plugin.json' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg 'hearting-guards.js' /tmp/opencode-plugin.json" INSTALL_LAYOUT.md \
    || ! grep -Fq '! rg "$non_claude_runtime_re" /tmp/opencode-plugin.json' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate the OpenCode native plugin projection"
  fi
  if ! grep -Fq '! rg "$non_claude_runtime_re" /tmp/opencode-agent.json' INSTALL_LAYOUT.md \
    || ! grep -Fq '! rg "$non_claude_runtime_re" /tmp/opencode-command.json' INSTALL_LAYOUT.md \
    || ! grep -Fq '! rg "$non_claude_runtime_re" /tmp/opencode-skills.json' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode runtime projection outputs with the shared non-Claude deny regex"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh capability-info autopilot-code >/tmp/opencode-capability.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^native_skill_path=adapters/opencode/skills/autopilot-code/SKILL.md$' /tmp/opencode-capability.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^native_command_path=adapters/opencode/commands/autopilot-code.md$' /tmp/opencode-capability.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^compat_reference=not-projected$' /tmp/opencode-capability.txt" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode capability-info native projections without root Skill compat references"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh role fast reviewer >/tmp/opencode-role.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^adapter=opencode$' /tmp/opencode-role.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^source=roles/README.md$' /tmp/opencode-role.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^family=fast$' /tmp/opencode-role.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'opencode_setting/bin/preflight.sh mode-info dev/backend >/tmp/opencode-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^adapter=opencode$' /tmp/opencode-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^status=portable$' /tmp/opencode-mode.txt" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode role and mode mapping surfaces"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh visual-harness >/tmp/opencode-visual-contract.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^adapter=opencode$' /tmp/opencode-visual-contract.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-visual-harness$' /tmp/opencode-visual-contract.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x opencode_setting/tools/design/visual-harness.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode visual harness projection"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh loop-info drill >/tmp/opencode-loop-drill.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^status=manual-contract$' /tmp/opencode-loop-drill.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^auto_run=unsupported$' /tmp/opencode-loop-drill.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'opencode_setting/bin/preflight.sh loop-info study >/tmp/opencode-loop-study.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^action=proposal-report-only$' /tmp/opencode-loop-study.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^fallback=read-source-and-draft-proposal-in-main-session$' /tmp/opencode-loop-study.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'opencode_setting/bin/preflight.sh loop-info note >/tmp/opencode-loop-note.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^status=unsupported$' /tmp/opencode-loop-note.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^fallback=worklog-board-or-manual-post-it-flow$' /tmp/opencode-loop-note.txt" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode loop-info manual/unsupported contracts"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh distill-propose install-check "$PWD" >/tmp/opencode-distill-propose.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^status=tool-contract$' /tmp/opencode-distill-propose.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^reason=distill-proposal-disabled$' /tmp/opencode-distill-propose.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=no-tools-distill-worker$' /tmp/opencode-distill-propose.txt" INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode distill-propose opt-in preview tool-contract"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh mode-info material/browser-fetch >/tmp/opencode-browser-fetch-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=browser-fetch$' /tmp/opencode-browser-fetch-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-browser-fetch$' /tmp/opencode-browser-fetch-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x opencode_setting/tools/material/browser-fetch.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode material browser-fetch projection"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh mode-info material/data-script >/tmp/opencode-data-script-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=data-script$' /tmp/opencode-data-script-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-data-script$' /tmp/opencode-data-script-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x opencode_setting/tools/material/data-script.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode material data-script projection"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh mode-info material/figure-gen >/tmp/opencode-figure-gen-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=figure-gen$' /tmp/opencode-figure-gen-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-figure-gen$' /tmp/opencode-figure-gen-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x opencode_setting/tools/material/figure-gen.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode material figure-gen projection"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh mode-info material/pdf-extract >/tmp/opencode-pdf-extract-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=pdf-extract$' /tmp/opencode-pdf-extract-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-pdf-extract$' /tmp/opencode-pdf-extract-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x opencode_setting/tools/material/pdf-extract.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode material PDF extract projection"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh mode-info material/web-image-search >/tmp/opencode-web-image-search-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=web-image-search$' /tmp/opencode-web-image-search-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-web-image-search$' /tmp/opencode-web-image-search-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x opencode_setting/tools/material/web-image-search.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode material web image search projection"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh mode-info qa/test >/tmp/opencode-test-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=verification-runner$' /tmp/opencode-test-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-verification-runner$' /tmp/opencode-test-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x opencode_setting/tools/qa/verification-runner.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode QA verification runner projection"
  fi
  if ! grep -Fq 'opencode_setting/bin/preflight.sh mode-info research/claim-verify >/tmp/opencode-claim-verify-mode.txt' INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^tool_contract=external-claim-verification$' /tmp/opencode-claim-verify-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq "rg '^runtime_surface=adapter-owned-claim-verify$' /tmp/opencode-claim-verify-mode.txt" INSTALL_LAYOUT.md \
    || ! grep -Fq 'test -x opencode_setting/tools/research/claim-verify.sh' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must validate OpenCode research claim-verify projection"
  fi
}

check_codex_bin_wrappers() {
  if [ ! -L codex_setting/bin ]; then
    fail_msg "codex_setting/bin must project adapters/codex/bin"
    return
  fi

  target=$(readlink codex_setting/bin)
  if [ "$target" != "../adapters/codex/bin" ]; then
    fail_msg "codex_setting/bin points to $target; expected ../adapters/codex/bin"
  fi

  for p in preflight.sh role-map.sh capability-map.sh mode-map.sh dispatch-headless.py dispatch-liveness.py dispatch-harvest.py distill-worker.sh sync-native-skills.py sync-native-agents.py sync-native-modes.py; do
    if [ ! -x "adapters/codex/bin/$p" ]; then
      fail_msg "adapters/codex/bin/$p is missing or not executable"
    fi
  done

  for p in dispatch-headless.py dispatch-liveness.py dispatch-harvest.py; do
    if ! grep -Fq 'def resolve_agent_home()' "adapters/codex/bin/$p" \
      || { ! grep -Fq 'core" / "CORE.md"' "adapters/codex/bin/$p" \
        && ! grep -Fq 'resolve_agent_home as _resolve_agent_home' "adapters/codex/bin/$p"; } \
      || grep -Fq 'Path(os.environ.get("AGENT_HOME", os.getcwd()))' "adapters/codex/bin/$p"; then
      fail_msg "adapters/codex/bin/$p must validate AGENT_HOME before using it as the harness root"
    fi
  done
  for p in dispatch-headless.py dispatch-liveness.py dispatch-harvest.py; do
    if ! grep -Fq 'AGENT_DISPATCH_JOBS' "adapters/codex/bin/$p"; then
      fail_msg "adapters/codex/bin/$p must honor the shared dispatch registry override"
    fi
  done

  if ! grep -Fq 'AGENT_ROOT=$(agent_home)' adapters/codex/bin/preflight.sh \
    || ! grep -Fq '[ -f "$AGENT_HOME/core/CORE.md" ]' adapters/codex/bin/preflight.sh \
    || ! grep -Fq '"$ROOT/adapters/codex/utilities/agent-home.sh"' adapters/codex/bin/preflight.sh \
    || grep -Fq 'AGENT_HOME="${AGENT_HOME:-$ROOT}"' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must validate explicit AGENT_HOME and otherwise use the Codex-owned canonical resolver"
  fi
  if ! grep -Fq 'AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/codex/bin/distill-worker.sh"' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must pass a validated harness root to the distill worker"
  fi
  if ! grep -Fq 'AGENT_ROOT=$(agent_home)' adapters/codex/bin/distill-worker.sh \
    || ! grep -Fq '[ -f "$AGENT_HOME/core/CORE.md" ]' adapters/codex/bin/distill-worker.sh \
    || grep -Fq 'AGENT_HOME="${AGENT_HOME:-$ROOT}"' adapters/codex/bin/distill-worker.sh; then
    fail_msg "adapters/codex/bin/distill-worker.sh must validate AGENT_HOME before using it as the harness root"
  fi
  if ! grep -Fq 'reason=distill-proposal-disabled' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'tool_contract=no-tools-distill-worker' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'enable=CODEX_DISTILL_ENABLE=1' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'CODEX_DISTILL_APPLY=1+CODEX_DISTILL_CONTRACT_ACCEPTED=1' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must report disabled distill-propose as an explicit no-tools worker tool-contract"
  fi
  if ! grep -Fq 'runtime_surface=codex-native-approval-sandbox' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'claude_allowed_tools=unsupported' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must report the Codex permission/sandbox contract without Claude allowedTools"
  fi
  if ! grep -Fq 'runtime_surface=codex-native-mcp' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'claude_settings_mcp=unsupported' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'design_mcp_projection=policy-not-adopted-approval-gated' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must report the Codex MCP contract without Claude settings MCP projection"
  fi
  if ! grep -Fq 'runtime_surface=codex-exec-headless' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'preflight.sh dispatch [--dry-run|--register|--start]' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'headless [--check] [--require-hook-trust]' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'strict_tool_contract_check=adapters/codex/bin/preflight.sh headless --check --require-hook-trust <worktree>' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'runtime_projection_requires=hearting,AGENTS.md,hooks.json,native-skills,native-agents,native-modes' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'runtime_projection_strict_requires=complete-codex-hook-trust' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'liveness_surface=codex-session-jsonl-mtime' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'liveness_check=adapters/codex/bin/preflight.sh liveness [jobs.log]' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'harvest_check=adapters/codex/bin/preflight.sh harvest' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'dispatch_prompt_contract=portable-typed-worker-bootstrap' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'worker_handoff=artifact,verdict,blocker' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'dispatch_input_validation=capability-info,capability-mode-catalog,optional-worker-mode-info,owner-mode-axis-consistency,qa-level' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'worker_startup_signal=wrapper-validated-metadata-or-immutable-route' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'physical_project_agents_masking=unsupported-runtime-auto-discovery-may-remain' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'check-runtime-projection.sh' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'CODEX_RUNTIME_PROJECTION_SKIP_CLI_DISCOVERY=1' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'claude_headless=unsupported' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must report the Codex headless dispatch contract without Claude headless assumptions"
  fi
  if ! grep -Fq 'validate_dispatch_inputs' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq -- '--require-hook-trust' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'check_runtime_projection(args.worktree, args.require_hook_trust)' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'invalid-dispatch-capability' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'invalid-dispatch-mode' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'invalid-dispatch-qa' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'quick,light,standard,thorough,adversarial' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'render_worker_bootstrap' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'resolve_worker_type' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq -- '--worker-type' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'End with the kernel' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'prompt_path.write_text(prompt_text, encoding="utf-8")' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'fcntl.flock' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'registry_lock={jobs}.lock' adapters/codex/bin/dispatch-headless.py \
    || ! grep -Fq 'close_attempt_row' adapters/codex/bin/dispatch-harvest.py \
    || ! grep -Fq 'ROUTE.complete_node' adapters/codex/bin/dispatch-harvest.py \
    || ! grep -Fq 'registry_lock={jobs}.lock' adapters/codex/bin/dispatch-harvest.py; then
    fail_msg "adapters/codex/bin/dispatch-headless.py must validate dispatch inputs and wrap assignments with the portable typed worker bootstrap"
  fi
  if ! grep -Fq 'native Skills, native Agents, and native Modes' adapters/codex/README.md \
    || ! grep -Fq 'native Skills, native Agents, and native Modes' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'headless --check <worktree>' adapters/codex/README.md \
    || ! grep -Fq 'headless [--check] [--require-hook-trust]' adapters/codex/AGENTS.md \
    || ! grep -Fq 'Use `dispatch --dry-run|--register|--start`' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'checked managed Codex records `codex-managed-gateway`' adapters/codex/README.md \
    || ! grep -Fq 'actual parent runtime, not the child' adapters/codex/ADAPTATION.md \
    || ! grep -Fq '`codex-managed-gateway`' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'fails with `managed-entry-required` before registry mutation or spawn' adapters/codex/README.md \
    || ! grep -Fq '`dispatch-owner` and model routes cannot select it' adapters/codex/README.md \
    || ! grep -Fq 'Registry writes and harvest rewrites are serialized with a `.lock` file' adapters/codex/README.md \
    || ! grep -Fq 'Registry writes and harvest rewrites are serialized with a `.lock` file' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'minimal typed worker prompt' adapters/codex/README.md \
    || ! grep -Fq 'portable kernel, one worker type' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'validates the capability catalog' adapters/codex/README.md \
    || ! grep -Fq 'optional non-owner `worker_mode` through `mode-info`' adapters/codex/README.md \
    || ! grep -Fq '`_kernel/owner` rejects a worker mode before prompt or registry writes' adapters/codex/README.md \
    || ! grep -Fq 'validates the capability catalog' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'optional non-owner `worker_mode` through `mode-info`' adapters/codex/ADAPTATION.md \
    || ! grep -Fq '`_kernel/owner` rejects a worker mode before prompt or registry writes' adapters/codex/ADAPTATION.md; then
    fail_msg "Codex headless docs must include native mode projection in runtime projection checks"
  fi

  if ! grep -Fq 'adapter=codex' adapters/codex/bin/role-map.sh; then
    fail_msg "adapters/codex/bin/role-map.sh must report its adapter for machine-readable role mappings"
  fi
  if ! grep -Fq 'source=roles/README.md' adapters/codex/bin/role-map.sh; then
    fail_msg "adapters/codex/bin/role-map.sh must report roles/README.md as the portable source"
  fi
  # 재홈 2026-07-22 (CONVENTIONS §2.3): pipeline-stage aliases resolve to portable ROLES
  # with unit-catalog guidance; retired team profiles/paths must never be re-emitted.
  if ! adapters/codex/bin/role-map.sh planning >/tmp/codex-role-planning.out 2>/tmp/codex-role-planning.err \
    || ! grep -Fq 'pipeline_stage=planning' /tmp/codex-role-planning.out \
    || ! grep -Fq 'portable_model_role=deep maker' /tmp/codex-role-planning.out \
    || ! grep -Fq 'unit_catalog=roles/units/' /tmp/codex-role-planning.out \
    || grep -Fq 'native_agent_path=' /tmp/codex-role-planning.out \
    || grep -Fq 'role_profile=' /tmp/codex-role-planning.out \
    || ! adapters/codex/bin/role-map.sh implementation >/tmp/codex-role-implementation.out 2>/tmp/codex-role-implementation.err \
    || ! grep -Fq 'pipeline_stage=implementation' /tmp/codex-role-implementation.out \
    || ! grep -Fq 'portable_model_role=fast implementer' /tmp/codex-role-implementation.out \
    || ! adapters/codex/bin/role-map.sh verification >/tmp/codex-role-verification.out 2>/tmp/codex-role-verification.err \
    || ! grep -Fq 'pipeline_stage=verification' /tmp/codex-role-verification.out \
    || ! grep -Fq 'portable_model_role=variable reviewer' /tmp/codex-role-verification.out \
    || ! grep -Fq 'role_set=fast reviewer,deep reviewer,external adversary' /tmp/codex-role-verification.out \
    || ! adapters/codex/bin/role-map.sh report >/tmp/codex-role-report.out 2>/tmp/codex-role-report.err \
    || ! grep -Fq 'pipeline_stage=report' /tmp/codex-role-report.out \
    || ! grep -Fq 'portable_model_role=fast writer' /tmp/codex-role-report.out \
    || ! grep -Fq 'role <portable-role|role-profile|pipeline-stage>' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'role <portable-role|role-profile|pipeline-stage>' adapters/codex/README.md \
    || ! grep -Fq 'role <portable-role|role-profile|pipeline-stage>' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/bin/role-map.sh must map pipeline-stage aliases to portable roles with unit-catalog guidance (no retired team profiles)"
  fi
  if adapters/codex/bin/role-map.sh 'plan team' >/tmp/codex-role-retired.out 2>/tmp/codex-role-retired.err; then
    fail_msg "adapters/codex/bin/role-map.sh must not resolve the retired 'plan team' profile alias"
  fi
  if ! adapters/codex/bin/role-map.sh variable reviewer >/tmp/codex-role-set.out 2>/tmp/codex-role-set.err \
    || ! grep -Fq 'family=role-set' /tmp/codex-role-set.out \
    || ! grep -Fq 'role_set=fast reviewer,deep reviewer,external adversary' /tmp/codex-role-set.out; then
    fail_msg "adapters/codex/bin/role-map.sh must report mixed reviewer role sets"
  fi
  if ! adapters/codex/bin/role-map.sh 'deep maker plus fast tool worker' >/tmp/codex-role-set-material.out 2>/tmp/codex-role-set-material.err \
    || ! grep -Fq 'role_set=deep maker,fast tool worker' /tmp/codex-role-set-material.out; then
    fail_msg "adapters/codex/bin/role-map.sh must report mixed maker/tool-worker role sets"
  fi
  for var in AGENT_MODEL_FAST AGENT_MODEL_DEEP AGENT_MODEL_EXTERNAL AGENT_MODEL_ORCHESTRATOR AGENT_REASONING_FAST AGENT_REASONING_DEEP AGENT_REASONING_EXTERNAL AGENT_REASONING_ORCHESTRATOR AGENT_EXTERNAL_CMD; do
    if ! grep -Fq "$var" adapters/codex/README.md || ! grep -Fq "$var" adapters/codex/ADAPTATION.md; then
      fail_msg "Codex role mapping docs must expose $var"
    fi
  done

  for p in 'preflight.sh session-end' 'preflight.sh prompt-signal' 'preflight.sh turn-nudge' 'preflight.sh token-budget' 'preflight.sh memory' 'preflight.sh recall' 'preflight.sh briefing' 'preflight.sh worklog' 'preflight.sh ui-info' 'preflight.sh tui-config' 'preflight.sh subagent-info' 'preflight.sh loop-info' 'preflight.sh qa-policy' 'preflight.sh distill-delta' 'preflight.sh distill-propose'; do
    if ! grep -Fq "$p" adapters/codex/AGENTS.md; then
      fail_msg "adapters/codex/AGENTS.md must document manual Codex lifecycle wrapper $p"
    fi
  done

  if ! grep -Fq 'Keep Codex `/statusline` responsible for model, context, token, limit, and session footer fields.' adapters/codex/AGENTS.md \
    || ! grep -Fq 'This does not replace Codex `/statusline` for model/context/token/session fields' adapters/codex/README.md \
    || ! grep -Fq 'Codex has its own `/statusline` configuration for the TUI footer.' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'preflight.sh ui-info' adapters/codex/AGENTS.md \
    || ! grep -Fq 'preflight.sh ui-info' adapters/codex/README.md \
    || ! grep -Fq 'preflight.sh ui-info' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'statusline_custom_dynamic_fields=unsupported' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'autopilot_auto_routing=instruction-guided-not-claude-slash-router' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'subagent_auto_spawn=explicit-or-main-dispatched' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'subagent_feature_check=adapters/codex/bin/preflight.sh subagent-info --check' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'runtime_surface=codex-native-subagents' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'feature_check=codex features list' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'git_dirty_tracked=' utilities/harness-status.sh \
    || ! grep -Fq 'git_untracked=' utilities/harness-status.sh \
    || ! grep -Fq 'git_extra_worktrees=' utilities/harness-status.sh \
    || ! grep -Fq 'git_dirty_tracked=$(printf' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'git_branch_done=$(printf' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'codex prompt signal carries git dirty, worktree, and dead-branch risks' hooks/portable-guards.test.sh \
    || ! grep -Fq 'codex status distinguishes tracked dirty, untracked files, and sibling worktrees' hooks/portable-guards.test.sh \
    || ! grep -Fq 'tracked-dirty vs untracked counts' adapters/codex/README.md \
    || ! grep -Fq 'tracked-dirty vs untracked counts' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'git dirty/worktree/dead-branch risks' adapters/codex/AGENTS.md \
    || ! grep -Fq 'git dirty/worktree/dead-branch risk fields' adapters/codex/README.md \
    || ! grep -Fq 'git dirty/worktree/dead-branch risk fields' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'preflight.sh subagent-info --check' adapters/codex/AGENTS.md \
    || ! grep -Fq 'preflight.sh subagent-info --check' adapters/codex/README.md \
    || ! grep -Fq 'preflight.sh subagent-info --check' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'do not duplicate Codex-native footer' adapters/codex/ADAPTATION.md; then
    fail_msg "Codex docs must keep /statusline native and reserve preflight.sh status for harness-specific signals"
  fi

  if ! grep -Fq 'codex_setting/codex-modes' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document the Codex native mode projection"
  fi

  if ! grep -Fq 'codex_setting/codex-hooks' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document the Codex native hook projection"
  fi

  if ! grep -Fq 'hook_boundary=shell-read-write-targeted-detection-explicit-preflight-fallback' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'shell_read_write_hooks=targeted-detection' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'targeted_shell_hooks=Bash,Shell,functions.exec_command' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'targeted_shell_write_patterns=redirect,tee,touch,cp,mv,rm,install,rsync,dd-of,sed-i' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'structured_write_hooks=Write,Edit,MultiEdit,apply_patch,functions.apply_patch' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'Shell/Bash/`functions.exec_command` reads and writes have targeted hook coverage' adapters/codex/AGENTS.md \
    || ! grep -Fq 'Shell/Bash/`functions.exec_command` gets targeted detection' adapters/codex/README.md \
    || ! grep -Fq 'common mutation commands (`tee`, `touch`, `cp`, `mv`, `rm`, `install`, `rsync`)' adapters/codex/README.md \
    || ! grep -Fq 'design HTML save paths' adapters/codex/README.md \
    || ! grep -Fq 'shell-read-write-targeted-detection-explicit-preflight-fallback' adapters/codex/ADAPTATION.md; then
    fail_msg "Codex adapter must document and report targeted shell/exec read-write hook coverage with explicit preflight fallback"
  fi
  if ! grep -Fq '*) fp="$PWD/$fp" ;;' hooks/spec-read-marker.sh \
    || ! grep -Fq 'codex read wrapper resolves relative prd paths for spec gate' hooks/portable-guards.test.sh; then
    fail_msg "spec-read-marker.sh and portable guards must prove explicit preflight read accepts relative prd paths"
  fi
  # Derive Codex native hook bridges from build-manifest rather than hardcoding them.
  CODEX_NATIVE_HOOKS=$(python3 tools/build-manifest.py --adaptation-surface codex-hooks 2>/dev/null || true)
  if [ -z "$CODEX_NATIVE_HOOKS" ]; then
    fail_msg "could not derive Codex native hook surface from build-manifest --adaptation-surface codex-hooks"
  fi
  for p in $CODEX_NATIVE_HOOKS; do
    if [ ! -x "adapters/codex/hooks/$p" ]; then
      fail_msg "adapters/codex/hooks/$p is missing or not executable"
    fi
  done
  # codex-adapter-parity audit P-19 (2026-07-04): derive the hook-event domain from the module-level
  # $EVENTS var (adapters/claude/settings.json .hooks keys) instead of a hardcoded 7-event list, so a
  # newly added Claude hook event that lacks a Codex hooks.json bridge fails loud instead of leaking.
  # HOOK_EVENT_EXEMPT (module-level, defined near $EVENTS) records only verified runtime asymmetry.
  for event in $EVENTS; do
    case " $HOOK_EVENT_EXEMPT " in
      *" $event "*) continue ;;
    esac
    if ! grep -Fq "\"$event\"" adapters/codex/hooks/hooks.json; then
      fail_msg "adapters/codex/hooks/hooks.json must register Codex $event"
    fi
  done
  if ! grep -Fq 'run_preflight("memory"' adapters/codex/hooks/sessionstart-lifecycle.py \
    || ! grep -Fq 'emit_context(' adapters/codex/hooks/sessionstart-lifecycle.py \
    || ! grep -Fq '"SessionStart"' adapters/codex/hooks/sessionstart-lifecycle.py \
    || ! grep -Fq 'hookSpecificOutput' adapters/codex/hooks/sessionstart-lifecycle.py \
    || ! grep -Fq 'run_preflight("session-end"' adapters/codex/hooks/sessionend-lifecycle.py \
    || grep -Fq 'subprocess' adapters/codex/hooks/stop-lifecycle.py \
    || grep -Fq 'session-end' adapters/codex/hooks/stop-lifecycle.py \
    || grep -Fq 'join_session_batch(' adapters/codex/hooks/stop-lifecycle.py \
    || grep -Fq 'decision' adapters/codex/hooks/stop-lifecycle.py \
    || ! grep -Fq 'parent_completion_harvested' adapters/codex/bin/dispatch-harvest.py \
    || grep -Fq 'register_parent_stop_attempt(args, jobs)' adapters/codex/bin/dispatch-headless.py \
    || grep -Fq 'sys.stdout.write(result.stdout)' adapters/codex/hooks/sessionend-lifecycle.py \
    || ! grep -Fq 'run_preflight("briefing"' adapters/codex/hooks/userprompt-lifecycle.py \
    || ! grep -Fq 'token_budget_context(current_cwd, sid)' adapters/codex/hooks/userprompt-lifecycle.py \
    || ! grep -Fq 'record_accounting(sid, event, adapter="codex")' adapters/codex/hooks/userprompt-lifecycle.py \
    || ! grep -Fq 'timeout_seconds=token_budget_timeout()' adapters/codex/hooks/userprompt-lifecycle.py \
    || ! grep -Fq 'CODEX_TOKEN_BUDGET_HOOK_TIMEOUT_SECONDS' adapters/codex/hooks/userprompt-lifecycle.py \
    || ! grep -Fq 'run_preflight("turn-nudge"' adapters/codex/hooks/userprompt-lifecycle.py \
    || ! grep -Fq 'emit_context("UserPromptSubmit"' adapters/codex/hooks/userprompt-lifecycle.py \
    || ! grep -Fq 'hookSpecificOutput' adapters/codex/hooks/userprompt-lifecycle.py; then
    fail_msg "Codex lifecycle hook bridges must route through preflight.sh lifecycle commands"
  fi
  if ! grep -Fq 'keeps session start context silent by default' hooks/portable-guards.test.sh \
    || ! grep -Fq 'can opt into session start memory context' hooks/portable-guards.test.sh \
    || ! grep -Fq 'CODEX_SESSION_MEMORY_INJECT=1' hooks/portable-guards.test.sh \
    || ! grep -Fq 'out["hookEventName"]=="SessionStart"' hooks/portable-guards.test.sh \
    || ! grep -Fq 'injects no workflow-mode banner (retired)' hooks/portable-guards.test.sh \
    || ! grep -Fq 'resets the turn-nudge counter on every prompt' hooks/portable-guards.test.sh \
    || ! grep -Fq 'injects token budget only on pressure-band transition' hooks/portable-guards.test.sh \
    || ! grep -Fq 'out["hookEventName"]=="UserPromptSubmit"' hooks/portable-guards.test.sh \
    || ! grep -Fq 'silent success output' hooks/portable-guards.test.sh \
    || ! grep -Fq 'adapter loop runtime logs are ignored' hooks/portable-guards.test.sh \
    || ! grep -Fq 'adapters/*/loops/*.log' .gitignore \
    || ! grep -Fq 'git_dirty_tracked=' hooks/portable-guards.test.sh \
    || ! grep -Fq 'headless_open_jobs=' hooks/portable-guards.test.sh \
    || ! grep -Fq 'hook_event=UserPromptSubmit' hooks/portable-guards.test.sh \
    || ! grep -Fq 'runtime_surface=adapter-owned-harness-status' hooks/portable-guards.test.sh \
    || ! grep -Fq 'CODEX_SESSION_MEMORY_INJECT' adapters/codex/hooks/sessionstart-lifecycle.py \
    || ! grep -Fq 'hookSpecificOutput.additionalContext' adapters/codex/README.md \
    || ! grep -Fq 'hookSpecificOutput.additionalContext' adapters/codex/ADAPTATION.md; then
    fail_msg "Codex lifecycle hooks must prove silent defaults plus opt-in/non-default additionalContext paths in portable guards"
  fi
  if ! grep -Fq 'def candidate_context' adapters/codex/hooks/userprompt-lifecycle.py \
    || ! grep -Fq 'RECALL_HOOK' adapters/codex/hooks/userprompt-lifecycle.py \
    || grep -Fq 'def auto_recall' adapters/codex/hooks/userprompt-lifecycle.py; then
    fail_msg "Codex UserPromptSubmit must expose bounded candidates without a semantic recall classifier"
  fi
  if [ ! -f tools/fleet/token_budget.py ] \
    || [ ! -f tools/fleet/token_accounting.py ] \
    || [ ! -f tools/fleet/token_experiment.py ] \
    || [ ! -f utilities/token-budget.py ] \
    || [ ! -f utilities/token-budget-experiment.py ] \
    || ! grep -Fq 'token-budget)' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'Token pressure is orthogonal to intensity' core/CONVENTIONS.md \
    || ! grep -Fq '**Token-pressure non-interference:**' core/OPERATIONS.md \
    || ! grep -Fq 'normal, unknown, repeated-band, and validated-native states' adapters/codex/AGENTS.md \
    || ! grep -Fq 'runtime-owned `$CODEX_HOME/config.toml`' adapters/codex/README.md \
    || ! grep -Fq 'AGENT_TOKEN_BUDGET_NATIVE_VALIDATED=1' adapters/codex/ADAPTATION.md \
    || grep -Fq 'import fcntl' utilities/token-budget.py \
    || ! grep -Fq 'class TransitionLock' utilities/token-budget.py \
    || ! grep -Fq 'class DirectoryLock' tools/fleet/token_accounting.py \
    || ! grep -Fq 'eligible_for_user_review' tools/fleet/token_experiment.py \
    || ! grep -Fq 'Phase 2 automatic accounting' adapters/opencode/AGENTS.md; then
    fail_msg "token-budget projection must preserve portable orthogonality, bounded accounting, isolated evaluation, and native-config boundaries"
  fi
  for p in utilities/token-budget.py adapters/codex/hooks/userprompt-lifecycle.py adapters/codex/bin/preflight.sh adapters/codex/hooks/hooks.json; do
    if grep -Eq 'offline-forecast-v1|token_experiment|production_dynamic_enabled' "$p"; then
      fail_msg "$p must not import or activate the production-disabled token experiment"
    fi
  done
  if ! grep -Fq 'runtime_surface=codex-userprompt-hook-signal' adapters/codex/bin/preflight.sh \
    || ! grep -Fq '"$0" status "$cwd" "$sid"' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'hook_scope=runtime-hook' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'autopilot_route=autopilot-required-for-spec-and-nontrivial-work' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'routing_contract=core/WORKFLOW.md' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'routing_action=read-workflow-and-select-codex-skill' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'capability_entrypoints=codex-native-skills' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'enforced_hooks=structured-write-guards,core-first-guard,posttool-read-markers,posttool-design-check,session-memory,turn-nudge' adapters/codex/bin/preflight.sh; then
    fail_msg "Codex UserPromptSubmit hook must expose a structured workflow/autopilot signal"
  fi
  if ! grep -Fq 'codex route wrapper combines status, prompt signal, capability-info, and spec gate' hooks/portable-guards.test.sh; then
    fail_msg "Codex route wrapper tests must prove route includes harness status before capability gates"
  fi

  if ! grep -Fq 'named `tool_contract`, `tool_contract_check`, `runtime_surface`, and `fallback`' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document mode tool contract metadata fields"
  fi

  if ! grep -Fq 'visual-harness)' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must expose the Codex visual harness tool-contract"
  fi
  if ! grep -Fq 'claim-verify)' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must expose the Codex research claim-verify tool-contract"
  fi
  if ! grep -Fq 'browser-fetch)' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must expose the Codex material browser-fetch tool-contract"
  fi
  if ! grep -Fq 'data-script)' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must expose the Codex material data-script tool-contract"
  fi
  if ! grep -Fq 'figure-gen)' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must expose the Codex material figure-gen tool-contract"
  fi
  if ! grep -Fq 'pdf-extract)' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must expose the Codex material PDF extract tool-contract"
  fi
  if ! grep -Fq 'web-image-search)' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must expose the Codex material web image search tool-contract"
  fi
  if ! grep -Fq 'verification-runner)' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must expose the Codex QA verification-runner tool-contract"
  fi
  if ! grep -Fq 'runtime_surface=adapter-owned-visual-harness' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'fallback=preflight.sh visual-harness <file.html>' adapters/codex/bin/capability-map.sh; then
    fail_msg "adapters/codex/bin/capability-map.sh must report visual harness runtime surface and fallback"
  fi
  if ! grep -Fq 'code-test)' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'tool_contract="verification-runner"' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'runtime_surface=adapter-owned-verification-runner' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'tool_contract_check=adapters/codex/bin/preflight.sh verification-runner --check -- <command>' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'artifact_contract="plans/<date>_<slug>:test_logs/,_internal/test_reviews/;handoff=code-report"' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'role_contract="verification=qa/test,review=qa/code-review"' adapters/codex/bin/capability-map.sh; then
    fail_msg "Codex code-test capability-info must expose the verification-runner tool contract"
  fi
  if ! grep -Fq 'graduated verification' capabilities/code-test.md \
    || ! grep -Fq '`code-report` alone updates `pipeline_summary.md`' capabilities/code-test.md \
    || ! grep -Fq 'verification-runner' capabilities/code-test.md \
    || ! grep -Fq 'test_logs/' adapters/codex/skills/code-test/SKILL.md \
    || ! grep -Fq '`code-report` alone updates `pipeline_summary.md`' adapters/codex/skills/code-test/SKILL.md \
    || ! grep -Fq 'verification-runner' adapters/codex/plugins/hearting-codex/skills/code-test/SKILL.md; then
    fail_msg "code-test portable spec and Codex projections must describe the verification-runner contract"
  fi
  if ! grep -Fq 'compat_reference=not-projected' adapters/codex/bin/capability-map.sh \
    || grep -Fq 'compat_reference="skills/' adapters/codex/bin/capability-map.sh \
    || grep -Fq "printf 'compat_reference=skills/" adapters/codex/bin/capability-map.sh; then
    fail_msg "adapters/codex/bin/capability-map.sh must not expose root Skill compatibility paths as Codex capability-info output"
  fi
  if ! grep -Fq 'stage_graph_contract="core/CONVENTIONS.md#pipeline-intensity-stage-graph-and-assurance"' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'plan_policy="direct=no-plan;quick=registered-headless-dispatch-depth-1-one-shot-micro-plan+plan-check-lite;standard+=durable-plan"' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'pipeline_contract="code-plan>code-execute>code-test>code-report"' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'optional_pipeline_step="code-refine"' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'artifact_contract="plans/<date>_<slug>:plan.md,checklist.md,pipeline_summary.md,dev_logs/,test_logs/"' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'role_contract="planning=plan/plan-author,plan-check=qa/plan-review,implementation=dev/*,impl-review=qa/code-review,verification=qa/test,report=editorial/report"' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'dispatch_contract="preflight.sh dispatch --capability autopilot-code --capability-mode <mode> [--worker-mode <family/mode>] --qa <level> --intensity <level> --dispatch-depth 1|2 [--parent <slug>]"' adapters/codex/bin/capability-map.sh; then
    fail_msg "Codex autopilot-code capability-info must expose the portable pipeline/artifact/role/dispatch contracts"
  fi
  if ! grep -Fq 'capability-info` and `route` print the portable pipeline contract (`code-plan>code-execute>code-test>code-report` for `standard+`)' adapters/codex/AGENTS.md \
    || ! grep -Fq 'autopilot-code pipeline' adapters/codex/README.md \
    || ! grep -Fq 'stage_graph_contract=core/CONVENTIONS.md#pipeline-intensity-stage-graph-and-assurance' adapters/codex/README.md \
    || ! grep -Fq 'plan_policy=direct=no-plan;quick=registered-headless-dispatch-depth-1-one-shot-micro-plan+plan-check-lite;standard+=durable-plan' adapters/codex/README.md \
    || ! grep -Fq 'pipeline_contract=code-plan>code-execute>code-test>code-report' adapters/codex/README.md; then
    fail_msg "Codex docs must describe the autopilot-code pipeline metadata exposed by capability-info/route"
  fi
  if ! grep -Fq 'qa-policy <quick|light|standard|thorough|adversarial> [code|research|doc|general]' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'runtime_surface=codex-qa-policy' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'independent_delegation_policy=claim-only-if-separate-codex-agent-headless-or-external-pass-ran' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'preflight.sh qa-policy <level> [code|research|doc|general]' adapters/codex/AGENTS.md \
    || ! grep -Fq 'QA policy mapping' adapters/codex/README.md; then
    fail_msg "Codex adapter must expose QA level policy mapping as a runtime preflight contract"
  fi
  if ! grep -Fq 'compat_reference=not-projected' adapters/codex/README.md \
    || grep -Fq 'legacy compatibility reference, if one exists' adapters/codex/README.md; then
    fail_msg "adapters/codex/README.md must document that capability-info does not project root Skill compatibility references"
  fi
  if grep -Eq 'Claude Design MCP|Claude visual harness' adapters/codex/bin/preflight.sh adapters/codex/bin/capability-map.sh; then
    fail_msg "Codex runtime-facing visual harness output must use legacy/adapter-specific wording, not Claude implementation names"
  fi

  if ! grep -Fq 'preflight.sh visual-harness' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document the Codex visual harness tool-contract"
  fi
  if ! grep -Fq 'preflight.sh browser-fetch --check <url>' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document the Codex material browser-fetch tool-contract"
  fi
  if ! grep -Fq 'preflight.sh data-script --check <script.py>' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document the Codex material data-script tool-contract"
  fi
  if ! grep -Fq 'preflight.sh figure-gen --check <script.py>' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document the Codex material figure-gen tool-contract"
  fi
  if ! grep -Fq 'figure-gen --verify-report <manifest.json> <report.md>' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document fail-closed report figure QA"
  fi
  if ! grep -Fq 'preflight.sh pdf-extract --check <file.pdf>' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document the Codex material PDF extract tool-contract"
  fi
  if ! grep -Fq 'preflight.sh web-image-search --check <query>' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document the Codex material web image search tool-contract"
  fi
  if ! grep -Fq 'preflight.sh verification-runner --timeout <seconds> -- <command>' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document the Codex QA verification-runner tool-contract"
  fi
  if ! grep -Fq 'preflight.sh claim-verify --check <claim>' adapters/codex/AGENTS.md; then
    fail_msg "adapters/codex/AGENTS.md must document the Codex research claim-verify tool-contract"
  fi
  if ! grep -Fq 'tool_contract_check' adapters/codex/README.md \
    || ! grep -Fq 'fallback=reference-only' adapters/codex/README.md \
    || ! grep -Fq 'runtime_surface' adapters/codex/README.md \
    || ! grep -Fq 'tool_contract_check' adapters/codex/ADAPTATION.md; then
    fail_msg "Codex docs must document mode-info contract metadata fields"
  fi

  if ! grep -Fq 'loop-info)' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'loop-info <oncall|note|study|drill|runtime-watch>' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'source=loops/oncall.md' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'action=corroborated-offline-proposal-evidence-and-report' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'source=loops/study.md' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'source=loops/runtime-watch.md' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'action=deterministic-probe-and-proposal-report-only' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'auto_edit=unsupported' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'background_output_auto_resume=unsupported-for-arbitrary-detached-shell' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'registered_headless_completion_delivery=app-server-supervised-after-runtime-probe' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'completion_delivery=native-scheduled-follow-up-if-exposed-else-state-automatic-follow-up-impossible' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'completion_delivery_unavailable=state-automatic-follow-up-impossible' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'detached_completion_promise=forbidden' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'source=loops/drill/README.md' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'auto_run=unsupported' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'related_extension=artifact-sink' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'native_extension_surface=application-owned-plugin' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'scheduler_surface=external-application' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'fallback=extension-unavailable-or-application-scheduler' adapters/codex/bin/preflight.sh; then
    fail_msg "adapters/codex/bin/preflight.sh must expose Codex loop-info contracts without running loop scripts"
  fi
  if ! grep -Fq 'loop-info <oncall|note|study|drill|runtime-watch>' adapters/codex/README.md \
    || ! grep -Fq 'preflight.sh loop-info <loop>' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'Arbitrary detached shell output' adapters/codex/AGENTS.md \
    || ! grep -Fq '`utilities/codex-managed-entry.py`' adapters/codex/AGENTS.md \
    || ! grep -Fq 'Managed completion never uses Stop continuation' adapters/codex/AGENTS.md \
    || ! grep -Fq 'rejected with `managed-entry-required` before registry mutation or' adapters/codex/AGENTS.md \
    || ! grep -Fq 'portable owner selector and model routes cannot select it' adapters/codex/AGENTS.md; then
    fail_msg "Codex docs must document loop-info support/fallback contracts"
  fi

  if grep -Fq 'Codex commands must be expressed as AGENTS instructions or wrapper commands' adapters/codex/ADAPTATION.md; then
    fail_msg "adapters/codex/ADAPTATION.md must describe command-like entries through native Skills/plugins, not stale wrapper-command wording"
  fi
  if ! grep -Fq 'command-like harness entries use Codex-native Skills' adapters/codex/ADAPTATION.md; then
    fail_msg "adapters/codex/ADAPTATION.md must document native Skills realization for Claude command non-support"
  fi
  if ! grep -Fq '`codex-agents`, `codex-hooks`, selected tools' adapters/codex/ADAPTATION.md; then
    fail_msg "adapters/codex/ADAPTATION.md current projection boundary must include codex-hooks"
  fi
  if ! grep -Fq 'not a hook listing or' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'runtime hook discovery test' adapters/codex/ADAPTATION.md; then
    fail_msg "adapters/codex/ADAPTATION.md must document the current Codex hook runtime discovery boundary"
  fi
}

check_codex_utility_projection() {
  if [ ! -L codex_setting/utilities ]; then
    fail_msg "codex_setting/utilities must project adapters/codex/utilities"
    return
  fi

  target=$(readlink codex_setting/utilities)
  if [ "$target" != "../adapters/codex/utilities" ]; then
    fail_msg "codex_setting/utilities points to $target; expected ../adapters/codex/utilities"
  fi

  if [ ! -x "adapters/codex/utilities/agent-home.sh" ]; then
    fail_msg "adapters/codex/utilities/agent-home.sh must be an executable Codex-owned utility"
  elif [ -L "adapters/codex/utilities/agent-home.sh" ]; then
    fail_msg "adapters/codex/utilities/agent-home.sh must be concrete, not a symlink to the shared Claude-compatible fallback"
  elif grep -q '\.claude' "adapters/codex/utilities/agent-home.sh"; then
    fail_msg "adapters/codex/utilities/agent-home.sh must not fall back to Claude runtime home"
  fi
  if ! grep -Fq '[ -f "$AGENT_HOME/core/CORE.md" ]' adapters/codex/utilities/agent-home.sh \
    || grep -Fq 'if [ "${AGENT_HOME:-}" ]; then' adapters/codex/utilities/agent-home.sh; then
    fail_msg "adapters/codex/utilities/agent-home.sh must validate AGENT_HOME before returning it"
  fi
  if ! grep -Fq '$HOME/.codex/hearting' adapters/codex/utilities/agent-home.sh; then
    fail_msg "adapters/codex/utilities/agent-home.sh must support the Codex runtime hearting pointer"
  fi

  for p in artifact-root.sh agent-worklog-state.sh harness-status.sh worktree-cleanup.py token-budget.py token-budget-experiment.py worker_bootstrap.py; do
    if [ ! -L "adapters/codex/utilities/$p" ]; then
      fail_msg "adapters/codex/utilities/$p must be a selective portable utility projection"
      continue
    fi
    link=$(readlink "adapters/codex/utilities/$p")
    if [ "$link" != "../../../utilities/$p" ]; then
      fail_msg "adapters/codex/utilities/$p points to $link; expected ../../../utilities/$p"
    fi
  done

  extra=$(find adapters/codex/utilities -mindepth 1 -maxdepth 1 ! \( -name agent-home.sh -o -name artifact-root.sh -o -name agent-worklog-state.sh -o -name harness-status.sh -o -name worktree-cleanup.py -o -name dispatch-route.sh -o -name dispatch-defaults.py -o -name token-budget.py -o -name token-budget-experiment.py -o -name worker_bootstrap.py \) -print 2>/dev/null || true)
  if [ -n "$extra" ]; then
    fail_msg "adapters/codex/utilities contains unapproved entries:"
    printf '%s\n' "$extra"
  fi

  for p in dispatch-liveness.sh dispatch-wait.sh extract_web_figures.py; do
    if [ -e "adapters/codex/utilities/$p" ] || [ -L "adapters/codex/utilities/$p" ]; then
      fail_msg "adapters/codex/utilities/$p must not be projected until Codex support is documented"
    fi
  done

  # codex-adapter-parity audit P-40 (2026-07-04): derived under-projection completeness pair — every
  # top-level utilities/* entry must be classified projected or deferred, else fail loud (closes the
  # leak window where a newly added utility silently has no projection decision).
  UTILITY_PROJECTED="agent-home.sh artifact-root.sh agent-worklog-state.sh harness-status.sh worktree-cleanup.py dispatch-route.sh dispatch-defaults.py token-budget.py token-budget-experiment.py worker_bootstrap.py"
  # mem-periodic-curate.sh is deferred, not projected: the opt-in nightly curator
  # ships Claude-first and default-off (2026-08-13, E axis). Codex/OpenCode keep
  # their own distill workers, so a projected counterpart would assert a parity
  # that does not exist yet. Revisit when the gate is enabled for real.
  UTILITY_DEFERRED="mem-periodic-curate.sh compute-hosts.py codex_hook_definition_age.py model_profile.py model-config.sh model_config.py harness-capacity.py dispatch_degradation.py dispatch_allocation_receipt.py dispatch_quality_peer.py ripple-map.py stage-session-chain.py stage_session_contract.py stage_session_runtime.py worker-state-hook.py worker-state-ledger.py artifact-root.test.sh artifact-snapshot.py artifact-sink.sh artifact-sink.test.sh artifact_identity.py artifact_manifest.py artifact_index.py artifact_admission.py artifact_lifecycle.py artifact_receipt.py artifact_producer.py artifact_cutover.py artifact_reader.py artifact_reader.test.py stage_session_runtime.test.py dispatch-artifact-root.test.py worktree-cleanup.test.py dispatch-liveness.sh dispatch-state-root.sh dispatch-liveness.test.sh dispatch_liveness_matrix.test.py dispatch-wait.sh dispatch-wait.test.sh dispatch-concurrency.test.sh usage-check.sh usage-check.test.sh dispatch-route.test.sh extract_web_figures.py capability-route.py capability_route.test.py compose-route.py compose_route.test.py dispatch-broker.py dispatch_broker.test.py dispatch-node.py dispatch_node.test.py dispatch-owner.py owner_route_binding.py dispatch-progress.py dispatch_progress.test.py dispatch-registry.py dispatch-recovery.py dispatch_registry.test.py dispatch_summary.py session_summary_trigger.py dispatch-orphan-watch.py dispatch_orphan_watch.test.py dispatch_adapters_v11.test.py dispatch_contract.py dispatch_stage_advance.py dispatch-reap-watch.py dispatch_allocation.py dispatch_mode_contract.py dispatch-observed-liveness.py dispatch_supervisor_terminal.py dispatch_contract.test.py dispatch_completion_marker.test.py dispatch_harvest.test.py dispatch_v20.test.py dispatch_lifecycle.py dispatch_continuation_budget.py dispatch_lifecycle.test.py dispatch-attempt-ready.py dispatch-batch.py launch-fence.py replica_batch_contract.py nested-dispatch-eligibility.py nested_dispatch_eligibility.test.py stage-dispatch-fallback.py stage_dispatch_fallback.test.py stage_dispatch_capacity.test.py spec-transaction.py spec_transaction.test.py worker-route-guard.py worker_route_guard.test.py model-worker-governor.py model_worker_governor.test.py resource-runner.py resource_run_registry.py resource_runner.test.py workflow_state.py workflow-supervisor.py workflow_supervisor.test.py worker_bootstrap.test.py worker_dispatch_prompt.test.py verify-files.sh verify-files.test.sh worktree-residue.py worktree_residue.test.py dispatch_codex_nocommit_fixture.test.py codex_dispatch_terminal.py codex_dispatch_terminal.test.py claude-session-supervisor.py claude_session_supervisor.test.py codex-app-server-supervisor.py codex_app_server_supervisor.test.py dispatch_completion_join.py dispatch_completion_join.test.py registered_parent_park.test.py capability-grounding.sh codex-managed-completion.py codex-managed-entry.py codex-managed-gateway.py codex_managed_dispatch.py codex-launcher.py codex-jsonl-writer.py spec_gate_evidence.py dispatch_pending_delivery.py dispatch_session_sweep.py artifact-postscan.py dispatch_launch_tuple.py"
  UTILITY_DEFERRED_EXTRA="codex_hook_definition_age.py model_profile.py model-config.sh model_config.py harness-capacity.py dispatch_degradation.py dispatch_allocation_receipt.py dispatch_quality_peer.py ripple-map.py stage-session-chain.py stage_session_contract.py stage_session_runtime.py worker-state-hook.py worker-state-ledger.py artifact-root.test.sh dispatch-artifact-root.test.py worktree-cleanup.test.py dispatch-liveness.sh dispatch-state-root.sh dispatch-liveness.test.sh dispatch_liveness_matrix.test.py dispatch-wait.sh dispatch-wait.test.sh dispatch-concurrency.test.sh usage-check.sh usage-check.test.sh dispatch-route.test.sh extract_web_figures.py token-budget.py token-budget-experiment.py capability-route.py capability_route.test.py compose-route.py compose_route.test.py dispatch-broker.py dispatch_broker.test.py dispatch-node.py dispatch_node.test.py dispatch-owner.py owner_route_binding.py dispatch-progress.py dispatch_progress.test.py dispatch-registry.py dispatch_registry.test.py dispatch-orphan-watch.py dispatch_orphan_watch.test.py dispatch_adapters_v11.test.py dispatch_contract.py dispatch_stage_advance.py dispatch-reap-watch.py dispatch_allocation.py dispatch_mode_contract.py dispatch-observed-liveness.py dispatch_supervisor_terminal.py dispatch_contract.test.py dispatch_completion_marker.test.py dispatch_harvest.test.py dispatch_v20.test.py dispatch_lifecycle.py dispatch_continuation_budget.py dispatch_lifecycle.test.py dispatch-attempt-ready.py dispatch-batch.py launch-fence.py replica_batch_contract.py nested-dispatch-eligibility.py nested_dispatch_eligibility.test.py stage-dispatch-fallback.py stage_dispatch_fallback.test.py stage_dispatch_capacity.test.py spec-transaction.py spec_transaction.test.py worker-route-guard.py worker_route_guard.test.py model-worker-governor.py model_worker_governor.test.py resource-runner.py resource_run_registry.py resource_runner.test.py workflow_state.py workflow-supervisor.py workflow_supervisor.test.py worker_bootstrap.test.py worker_dispatch_prompt.test.py verify-files.sh verify-files.test.sh worktree-residue.py worktree_residue.test.py dispatch_codex_nocommit_fixture.test.py codex_dispatch_terminal.py codex_dispatch_terminal.test.py claude-session-supervisor.py claude_session_supervisor.test.py codex-app-server-supervisor.py codex_app_server_supervisor.test.py dispatch_completion_join.py dispatch_completion_join.test.py registered_parent_park.test.py capability-grounding.sh codex-managed-completion.py codex-managed-entry.py codex-managed-gateway.py codex_managed_dispatch.py"
  utility_count=0
  for f in utilities/*; do
    [ -f "$f" ] || continue
    utility_count=$((utility_count + 1))
  done
  if [ "$utility_count" -eq 0 ]; then
    fail_msg "utilities/* domain is empty; cannot verify Codex utility projection completeness"
    return
  fi
  for f in utilities/*; do
    [ -f "$f" ] || continue
    bn=$(basename "$f")
    # Test files are DERIVED-deferred: no *.test.* file has ever been (or may be)
    # projected into an adapter utilities dir, so the projected/deferred census
    # holds no real decision for them. Hand-listing each new test recreated the
    # "new file → forgotten census row → CI red after push" class
    # (2026-07-22 dispatch_parent_context_conformance.test.py incident).
    # Non-test utilities still require an explicit census decision below.
    case "$bn" in
      *.test.py|*.test.sh) continue ;;
    esac
    [ "$bn" = "$SHARED_UTILITY_DEFERRED" ] && continue
    case " $UTILITY_PROJECTED $UTILITY_DEFERRED $SHARED_UTILITY_DEFERRED " in
      *" $bn "*) ;;
      *) fail_msg "no projection decision for utilities/$bn (must be classified projected or deferred)" ;;
    esac
  done
}

check_codex_tool_projection() {
  if [ ! -L codex_setting/tools ]; then
    fail_msg "codex_setting/tools must project adapters/codex/tools"
    return
  fi

  target=$(readlink codex_setting/tools)
  if [ "$target" != "../adapters/codex/tools" ]; then
    fail_msg "codex_setting/tools points to $target; expected ../adapters/codex/tools"
  fi

  for p in mem.py recall.sh; do
    if [ ! -x "adapters/codex/tools/memory/$p" ]; then
      fail_msg "adapters/codex/tools/memory/$p must be an executable Codex-owned memory launcher"
    elif [ -L "adapters/codex/tools/memory/$p" ]; then
      fail_msg "adapters/codex/tools/memory/$p must be concrete, not a symlink to the shared Claude-compatible fallback"
    elif ! check_no_claude_native_refs "adapters/codex/tools/memory/$p" "adapters/codex/tools/memory/$p"; then
      :
    elif ! grep -Fq '[ -f "$AGENT_HOME/tools/memory/mem.py" ]' "adapters/codex/tools/memory/$p" \
      || grep -Fq 'if [ "${AGENT_HOME:-}" ]; then' "adapters/codex/tools/memory/$p"; then
      fail_msg "adapters/codex/tools/memory/$p must validate AGENT_HOME before using it as the harness root"
    fi
  done

  for p in apply-distill-actions.py; do
    if [ ! -L "adapters/codex/tools/memory/$p" ]; then
      fail_msg "adapters/codex/tools/memory/$p must be a selective portable memory tool projection"
      continue
    fi
    link=$(readlink "adapters/codex/tools/memory/$p")
    if [ "$link" != "../../../../tools/memory/$p" ]; then
      fail_msg "adapters/codex/tools/memory/$p points to $link; expected ../../../../tools/memory/$p"
    fi
  done

  if [ ! -x adapters/codex/tools/design/visual-harness.sh ]; then
    fail_msg "adapters/codex/tools/design/visual-harness.sh must be an executable Codex-owned design launcher"
  elif [ -L adapters/codex/tools/design/visual-harness.sh ]; then
    fail_msg "adapters/codex/tools/design/visual-harness.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/codex/tools/design/visual-harness.sh adapters/codex/tools/design/visual-harness.sh; then
    :
  fi

  if [ ! -x adapters/codex/tools/design/convert-harness.sh ]; then
    fail_msg "adapters/codex/tools/design/convert-harness.sh must be an executable Codex-owned design converter launcher"
  elif [ -L adapters/codex/tools/design/convert-harness.sh ]; then
    fail_msg "adapters/codex/tools/design/convert-harness.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/codex/tools/design/convert-harness.sh adapters/codex/tools/design/convert-harness.sh; then
    :
  fi

  if [ ! -x adapters/codex/tools/material/data-script.sh ]; then
    fail_msg "adapters/codex/tools/material/data-script.sh must be an executable Codex-owned material launcher"
  elif [ -L adapters/codex/tools/material/data-script.sh ]; then
    fail_msg "adapters/codex/tools/material/data-script.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/codex/tools/material/data-script.sh adapters/codex/tools/material/data-script.sh; then
    :
  fi

  if [ ! -x adapters/codex/tools/material/browser-fetch.sh ]; then
    fail_msg "adapters/codex/tools/material/browser-fetch.sh must be an executable Codex-owned material launcher"
  elif [ -L adapters/codex/tools/material/browser-fetch.sh ]; then
    fail_msg "adapters/codex/tools/material/browser-fetch.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/codex/tools/material/browser-fetch.sh adapters/codex/tools/material/browser-fetch.sh; then
    :
  fi

  if [ ! -x adapters/codex/tools/material/figure-gen.sh ]; then
    fail_msg "adapters/codex/tools/material/figure-gen.sh must be an executable Codex-owned material launcher"
  elif [ -L adapters/codex/tools/material/figure-gen.sh ]; then
    fail_msg "adapters/codex/tools/material/figure-gen.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/codex/tools/material/figure-gen.sh adapters/codex/tools/material/figure-gen.sh; then
    :
  elif ! grep -Fq -- '--verify-report' adapters/codex/tools/material/figure-gen.sh; then
    fail_msg "Codex figure-gen launcher must expose report semantic verification"
  fi

  if [ ! -x adapters/codex/tools/material/pdf-extract.sh ]; then
    fail_msg "adapters/codex/tools/material/pdf-extract.sh must be an executable Codex-owned material launcher"
  elif [ -L adapters/codex/tools/material/pdf-extract.sh ]; then
    fail_msg "adapters/codex/tools/material/pdf-extract.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/codex/tools/material/pdf-extract.sh adapters/codex/tools/material/pdf-extract.sh; then
    :
  fi

  if [ ! -x adapters/codex/tools/material/web-image-search.sh ]; then
    fail_msg "adapters/codex/tools/material/web-image-search.sh must be an executable Codex-owned material launcher"
  elif [ -L adapters/codex/tools/material/web-image-search.sh ]; then
    fail_msg "adapters/codex/tools/material/web-image-search.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/codex/tools/material/web-image-search.sh adapters/codex/tools/material/web-image-search.sh; then
    :
  fi

  if [ ! -x adapters/codex/tools/qa/verification-runner.sh ]; then
    fail_msg "adapters/codex/tools/qa/verification-runner.sh must be an executable Codex-owned QA launcher"
  elif [ -L adapters/codex/tools/qa/verification-runner.sh ]; then
    fail_msg "adapters/codex/tools/qa/verification-runner.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/codex/tools/qa/verification-runner.sh adapters/codex/tools/qa/verification-runner.sh; then
    :
  fi

  if [ ! -x adapters/codex/tools/research/claim-verify.sh ]; then
    fail_msg "adapters/codex/tools/research/claim-verify.sh must be an executable Codex-owned research launcher"
  elif [ -L adapters/codex/tools/research/claim-verify.sh ]; then
    fail_msg "adapters/codex/tools/research/claim-verify.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/codex/tools/research/claim-verify.sh adapters/codex/tools/research/claim-verify.sh; then
    :
  fi

  extra=$(find adapters/codex/tools -mindepth 1 ! \( -path adapters/codex/tools/memory -o -path adapters/codex/tools/memory/mem.py -o -path adapters/codex/tools/memory/apply-distill-actions.py -o -path adapters/codex/tools/memory/recall.sh -o -path adapters/codex/tools/design -o -path adapters/codex/tools/design/visual-harness.sh -o -path adapters/codex/tools/design/convert-harness.sh -o -path adapters/codex/tools/material -o -path adapters/codex/tools/material/browser-fetch.sh -o -path adapters/codex/tools/material/data-script.sh -o -path adapters/codex/tools/material/figure-gen.sh -o -path adapters/codex/tools/material/pdf-extract.sh -o -path adapters/codex/tools/material/web-image-search.sh -o -path adapters/codex/tools/qa -o -path adapters/codex/tools/qa/verification-runner.sh -o -path adapters/codex/tools/research -o -path adapters/codex/tools/research/claim-verify.sh \) -print 2>/dev/null || true)
  if [ -n "$extra" ]; then
    fail_msg "adapters/codex/tools contains unapproved entries:"
    printf '%s\n' "$extra"
  fi

  for p in build-manifest.py check-adaptation-boundary.sh design-mcp web-bundle fleet profile; do
    if [ -e "adapters/codex/tools/$p" ] || [ -L "adapters/codex/tools/$p" ]; then
      fail_msg "adapters/codex/tools/$p must not be projected until Codex support is documented"
    fi
  done

  # codex-adapter-parity audit P-21 (2026-07-04): derived under-projection completeness — every
  # top-level tools/* entry must be classified projected or deferred, else fail loud. design-mcp is
  # deferred-but-realized-as-visual-harness (a concrete launcher under a different name) — this
  # completeness check and the denylist above are separate assertions and must not be conflated.
  TOOL_PROJECTED="memory material figure-semantic-manifest.schema.json figure-semantic-verify.py"
  TOOL_DEFERRED="__pycache__ integrations artifact-w8-handoff.py artifact_w8_handoff.test.py build-manifest.py render-hub.py render-landing.py render-fleet-svg.py generate.py harness_manifest.py sync-skill-invocation-policy.py sync-entry-skill-layer.py entry-skill-layer.test.py generated-projections.test.sh sync-missing-projections.sh sync-missing-projections.py figure-semantic-verify.test.py check-adaptation-boundary.sh check-model-config.py check-unit-config.py check-utility-census.py context-footprint.py context-footprint-baseline.json adaptation-exemptions.tsv adaptation-guard.test.sh routing-contract.test.sh design-mcp skill-conformance web-bundle fleet profile install improvement release capability_topology.py capability_topology.test.py report-manifest-verify.py report_manifest_verify.test.py report-bundle.py report_bundle_verify.test.py smoke-attestation.py smoke_attestation.test.py lab-config-provenance.py artifact-producer-canary.py artifact-delta-census.py artifact_delta_census.test.py lab_config_provenance.test.py browser-acceptance check-runtime-memory-boundary.py runtime_memory_boundary.test.py migration-manifest.py migration_manifest.test.py git-hooks check-scope-placeholders.py scope-placeholders.tsv scope_placeholders.test.py run-tests.py run_tests.test.py test-baseline.tsv test-isolation.tsv installed-layout-triggers.tsv check-installed-layout-trigger.py dispatch-discriminators.tsv"
  tool_count=0
  for f in tools/*; do
    [ -e "$f" ] || continue
    tool_count=$((tool_count + 1))
  done
  if [ "$tool_count" -eq 0 ]; then
    fail_msg "tools/* domain is empty; cannot verify Codex tool projection completeness"
    return
  fi
  for f in tools/*; do
    [ -e "$f" ] || continue
    bn=$(basename "$f")
    case " $TOOL_PROJECTED $TOOL_DEFERRED " in
      *" $bn "*) ;;
      *) fail_msg "no projection decision for tools/$bn (must be classified projected or deferred)" ;;
    esac
  done
}

check_codex_scaffold_projection() {
  if [ ! -L codex_setting/scaffolds ]; then
    fail_msg "codex_setting/scaffolds must project adapters/codex/scaffolds"
    return
  fi

  target=$(readlink codex_setting/scaffolds)
  if [ "$target" != "../adapters/codex/scaffolds" ]; then
    fail_msg "codex_setting/scaffolds points to $target; expected ../adapters/codex/scaffolds"
  fi

  # codex-adapter-parity audit P-41 (2026-07-04): derive the iterated scaffold set from scaffolds/*/
  # (skip README.md, which is not a directory) instead of a hardcoded 5-list, so a newly added
  # scaffold directory is required to have a Codex projection instead of silently leaking. The
  # cmp -s shared-asset mirror (all non-deck_stage scaffolds), the deck_stage sanitization check, and
  # the Claude-path denylist below are all preserved unchanged — only the iterated set changes.
  scaffold_count=0
  for scaffold_dir in scaffolds/*/; do
    [ -d "$scaffold_dir" ] || continue
    scaffold_count=$((scaffold_count + 1))
  done
  if [ "$scaffold_count" -eq 0 ]; then
    fail_msg "scaffolds/*/ domain is empty; cannot verify Codex scaffold projection completeness"
    return
  fi
  for scaffold_dir in scaffolds/*/; do
    [ -d "$scaffold_dir" ] || continue
    d=$(basename "$scaffold_dir")
    p="$d/$d.html"
    if [ ! -f "adapters/codex/scaffolds/$p" ]; then
      fail_msg "adapters/codex/scaffolds/$p must exist as a Codex scaffold projection"
    elif [ "$d" != "deck_stage" ] && ! cmp -s "scaffolds/$p" "adapters/codex/scaffolds/$p"; then
      fail_msg "adapters/codex/scaffolds/$p must mirror the shared scaffold asset"
    fi
  done
  if ! grep -Fq 'adapter visual harness' adapters/codex/scaffolds/deck_stage/deck_stage.html; then
    fail_msg "adapters/codex/scaffolds/deck_stage/deck_stage.html must sanitize shared Design MCP wording for Codex"
  fi

  if rg -n 'adapters/claude|claude_setting|~/.claude|Design MCP|design-mcp' adapters/codex/scaffolds >/tmp/codex-scaffolds-claude.out 2>/dev/null; then
    fail_msg "Codex scaffold projection must not expose Claude-native runtime paths:"
    cat /tmp/codex-scaffolds-claude.out
  fi
}

check_codex_native_skill_projection() {
  if [ ! -x adapters/codex/bin/sync-native-skills.py ]; then
    fail_msg "adapters/codex/bin/sync-native-skills.py must be executable"
    return
  fi

  if ! adapters/codex/bin/sync-native-skills.py --check >/tmp/codex-native-skills.out 2>/tmp/codex-native-skills.err; then
    fail_msg "Codex native skill projections are stale; run adapters/codex/bin/sync-native-skills.py"
    cat /tmp/codex-native-skills.err
  fi

  for f in capabilities/*.md; do
    [ -f "$f" ] || continue
    [ "$(basename "$f")" = "README.md" ] && continue
    slug=$(basename "$f" .md)
    skill="adapters/codex/skills/$slug/SKILL.md"
    if [ ! -f "$skill" ]; then
      fail_msg "$skill is missing"
      continue
    fi
    if ! grep -Fq "capabilities/$slug.md" "$skill"; then
      fail_msg "$skill must reference capabilities/$slug.md as portable source"
    fi
    if ! grep -Fq "adapters/codex/bin/preflight.sh capability-info $slug" "$skill"; then
      fail_msg "$skill must reference the Codex capability-info wrapper"
    fi
    if ! grep -Fq "not a legacy compatibility Skill copy" "$skill"; then
      fail_msg "$skill must state that it is not a legacy compatibility Skill copy"
    fi
    invocation_class=$(awk -F '\t' -v name="$slug" '$1 == name {print $2; exit}' tools/skill-conformance/invocation-policy.tsv)
    if [ -z "$invocation_class" ]; then
      fail_msg "$skill has no manifest-generated invocation classification"
    fi
    if [ "$invocation_class" != "entry-router" ] && ! grep -Fq "Invocation semantics:" "$skill"; then
      fail_msg "$skill must include the portable invocation semantics excerpt"
    fi
    if ! grep -Fq 'named `tool_contract`' "$skill" \
      || ! grep -Fq '`tool_contract_check`' "$skill" \
      || ! grep -Fq '`runtime_surface` / `fallback`' "$skill" \
      || ! grep -Fq 'reported `fallback`' "$skill"; then
      fail_msg "$skill must instruct Codex to obey capability-info tool contract metadata"
    fi
    if ! grep -Fq 'preflight.sh status [cwd] [session-id]' "$skill" \
      || ! grep -Fq 'preflight.sh prompt-signal [cwd] [session-id]' "$skill"; then
      fail_msg "$skill must include Codex status and prompt-signal workflow guards"
    fi
    if ! grep -Fq "adapters/codex/bin/preflight.sh route $slug [cwd] [session-id]" "$skill"; then
      fail_msg "$skill must include the Codex route wrapper"
    fi
    if [ "$invocation_class" = "entry-router" ]; then
      if grep -Fq '## Projected Portable Details' "$skill" \
        || grep -Fq '## Portable Contract' "$skill"; then
        fail_msg "$skill must keep entry-router procedure detail out of the dispatch-depth-0 projection"
      fi
      if ! grep -Fq 'five-field confirmation card' "$skill" \
        || ! grep -Fq 'dispatch-depth-1 owner reads it' "$skill" \
        || ! grep -Fq 'stage workers read only their assigned contracts' "$skill"; then
        fail_msg "$skill must project the confirmation and owner/worker context boundary"
      fi
    elif ! grep -Fq '## Projected Portable Details' "$skill" \
      || ! grep -Fq '## Artifact Ownership' "$skill" \
      || ! grep -Fq '## Role Requirements' "$skill" \
      || ! grep -Fq '## Guard Requirements' "$skill"; then
      fail_msg "$skill must project portable artifact, role, and guard details"
    fi
    if grep -Fq "metadata:" "$skill"; then
      fail_msg "$skill must use Codex Skill frontmatter only, without adapter metadata"
    fi
  done
  if ! grep -Fq 'five-field confirmation card' adapters/codex/skills/autopilot-code/SKILL.md \
    || ! grep -Fq 'dispatch-depth-1 owner reads it' adapters/codex/skills/autopilot-code/SKILL.md \
    || grep -Fq '## Projected Portable Details' adapters/codex/skills/autopilot-code/SKILL.md; then
    fail_msg "Codex autopilot-code entry projection must stay compact and delegate contract reading"
  fi
  for skill in adapters/codex/skills/*/SKILL.md; do
    [ -f "$skill" ] || continue
    slug=$(basename "$(dirname "$skill")")
    if [ ! -f "capabilities/$slug.md" ]; then
      fail_msg "$skill has no matching portable capability source"
    fi
  done

  bad=$(rg -n 'adapters/claude|claude_setting|claude_realization|statusline\.sh|settings\.json|CLAUDE\.md|(^|[^[:alnum:]_/.-])skills/' adapters/codex/skills adapters/codex/bin/capability-map.sh 2>/dev/null || true)
  if [ -n "$bad" ]; then
    fail_msg "Codex native capability surfaces must not expose Claude-native surfaces:"
    printf '%s\n' "$bad"
  fi
}

check_codex_native_plugin_projection() {
  plugin_root="adapters/codex/plugins/hearting-codex"
  plugin_manifest="$plugin_root/.codex-plugin/plugin.json"
  marketplace="adapters/codex/plugin-marketplace/.agents/plugins/marketplace.json"

  if [ ! -x adapters/codex/bin/sync-native-plugin.py ]; then
    fail_msg "adapters/codex/bin/sync-native-plugin.py must be executable"
    return
  fi

  if ! adapters/codex/bin/sync-native-plugin.py --check >/tmp/codex-sync-plugin.out 2>/tmp/codex-sync-plugin.err; then
    fail_msg "Codex native plugin projection is stale; run adapters/codex/bin/sync-native-plugin.py"
    cat /tmp/codex-sync-plugin.err
  fi

  plugin_entries=$(find adapters/codex/plugins -mindepth 1 -maxdepth 1 -exec basename {} \; 2>/dev/null || true)
  for entry in $plugin_entries; do
    if [ "$entry" != "hearting-codex" ]; then
      fail_msg "adapters/codex/plugins/$entry is not an approved Codex plugin projection"
    fi
  done
  if [ -e adapters/codex/.agents ] || [ -L adapters/codex/.agents ]; then
    fail_msg "adapters/codex/.agents is obsolete; Codex marketplace projection must live under adapters/codex/plugin-marketplace"
  fi

  if [ ! -d "$plugin_root" ] || [ -L "$plugin_root" ]; then
    fail_msg "$plugin_root must be a concrete adapter-owned Codex plugin directory"
  fi
  plugin_links=$(find "$plugin_root" -type l -print 2>/dev/null || true)
  if [ -n "$plugin_links" ]; then
    fail_msg "$plugin_root must not contain symlinked plugin files:"
    printf '%s\n' "$plugin_links"
  fi

  if [ ! -f "$plugin_manifest" ]; then
    fail_msg "Codex native plugin manifest is missing"
  fi
  if [ ! -f "$marketplace" ]; then
    fail_msg "Codex native plugin marketplace is missing"
  fi
  if [ ! -f "$plugin_root/skills/autopilot-code/SKILL.md" ]; then
    fail_msg "Codex native plugin must include generated capability skills"
  fi
  if ! grep -Fq 'five-field confirmation card' "$plugin_root/skills/autopilot-lab/SKILL.md" \
    || grep -Fq '## Projected Portable Details' "$plugin_root/skills/autopilot-lab/SKILL.md"; then
    fail_msg "Codex native plugin entry Skill must preserve the compact confirmation boundary"
  fi
  if ! grep -Fq 'five-field confirmation card' "$plugin_root/skills/autopilot-code/SKILL.md" \
    || ! grep -Fq 'dispatch-depth-1 owner reads it' "$plugin_root/skills/autopilot-code/SKILL.md" \
    || grep -Fq '## Projected Portable Details' "$plugin_root/skills/autopilot-code/SKILL.md"; then
    fail_msg "Codex native plugin autopilot-code entry projection must stay compact"
  fi
  for skill in "$plugin_root"/skills/*/SKILL.md; do
    [ -f "$skill" ] || continue
    slug=$(basename "$(dirname "$skill")")
    if [ ! -f "capabilities/$slug.md" ]; then
      fail_msg "$skill has no matching portable capability source"
    fi
  done
  bad=$(rg -n "$CLAUDE_NATIVE_SURFACE_PATTERN" "$plugin_root" 2>/dev/null || true)
  if [ -n "$bad" ]; then
    fail_msg "Codex native plugin projection must not expose Claude-native surfaces:"
    printf '%s\n' "$bad"
  fi
  if ! grep -Fq '"name": "hearting-codex"' "$plugin_manifest" \
    || ! grep -Fq '"skills": "./skills/"' "$plugin_manifest"; then
    fail_msg "$plugin_manifest must define the hearting-codex plugin and plugin-local skills path"
  fi
  if grep -Eq 'Claude-native|Claude Code|adapters/claude|claude_setting' "$plugin_manifest"; then
    fail_msg "$plugin_manifest must not expose Claude implementation names in Codex runtime-facing metadata"
  fi
  if ! grep -Fq '"name": "hearting"' "$marketplace" \
    || ! grep -Fq '"path": "./plugins/hearting-codex"' "$marketplace"; then
    fail_msg "$marketplace must expose hearting-codex through the repo-local plugin path"
  fi

  if ! grep -Fq "Custom prompts are deprecated" adapters/codex/README.md; then
    fail_msg "adapters/codex/README.md must document why command-like entries use skills/plugins instead of custom prompts"
  fi
  if ! grep -Fq 'native_plugin_skill_path=' adapters/codex/bin/capability-map.sh \
    || ! grep -Fq 'codex-native-skill-plugin' adapters/codex/bin/capability-map.sh; then
    fail_msg "adapters/codex/bin/capability-map.sh must report Codex native plugin skill realization"
  fi
}

check_codex_native_agent_projection() {
  if [ ! -x adapters/codex/bin/sync-native-agents.py ]; then
    fail_msg "adapters/codex/bin/sync-native-agents.py must be executable"
    return
  fi

  if ! adapters/codex/bin/sync-native-agents.py --check >/tmp/codex-sync-agents.out 2>/tmp/codex-sync-agents.err; then
    fail_msg "Codex native agent projections are stale; run adapters/codex/bin/sync-native-agents.py"
    cat /tmp/codex-sync-agents.err
  fi

  # Runtime team agents retired 2026-07-22 (재홈, CONVENTIONS §2.3): kernel helper only,
  # and the retired projections must NOT reappear.
  for retired in plan-team dev-team qa-team research-team material-team design-team editorial-team external-adversary; do
    if [ -e "adapters/codex/agents/$retired.toml" ]; then
      fail_msg "adapters/codex/agents/$retired.toml is a retired team projection and must not exist"
    fi
  done
  for profile in memory-scout; do
    agent="adapters/codex/agents/$profile.toml"
    if [ ! -f "$agent" ]; then
      fail_msg "$agent is missing"
      continue
    fi
    if [ -L "$agent" ]; then
      fail_msg "$agent must be a concrete adapter-owned Codex custom agent"
    fi
    if ! python3 - "$agent" >/tmp/codex-agent-toml.out 2>/tmp/codex-agent-toml.err <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
for key in ("name", "description"):
    if not re.search(rf'^{key} = "[^"]+"$', text, re.MULTILINE):
        raise SystemExit(f"missing required Codex custom agent field: {key}")
for key in ("model", "model_reasoning_effort", "sandbox_mode"):
    if not re.search(rf'^{key} = "[^"]+"$', text, re.MULTILINE):
        raise SystemExit(f"missing Codex custom agent runtime config field: {key}")
if not re.search(r'^developer_instructions = """\n.+\n"""$', text, re.MULTILINE | re.DOTALL):
    raise SystemExit("missing required Codex custom agent field: developer_instructions")
PY
    then
      fail_msg "$agent must be valid Codex custom agent TOML"
      cat /tmp/codex-agent-toml.err
    fi
    if [ "$profile" = "memory-scout" ]; then
      if ! grep -Fq "core/MEMORY.md §7.4" "$agent"; then
        fail_msg "$agent must reference core/MEMORY.md §7.4 as portable source"
      fi
    elif ! grep -Fq "roles/README.md" "$agent"; then
      fail_msg "$agent must reference roles/README.md as portable source"
    fi
    if [ "$profile" != "memory-scout" ] && ! grep -Fq "adapters/codex/bin/preflight.sh role" "$agent"; then
      fail_msg "$agent must reference the Codex role mapper"
    fi
    mapped_role=$(sed -n 's/^Codex role-map input: `\(.*\)`$/\1/p' "$agent" | head -n 1)
    if [ "$profile" != "memory-scout" ] && { [ -z "$mapped_role" ] || ! adapters/codex/bin/role-map.sh "$mapped_role" >/tmp/codex-agent-role.out 2>/tmp/codex-agent-role.err; }; then
      fail_msg "$agent must include a Codex role-map input that resolves through adapters/codex/bin/role-map.sh"
      cat /tmp/codex-agent-role.err
    fi
    if ! grep -Fq "not a legacy compatibility Agent copy" "$agent" \
      && ! grep -Fq "not a Claude Agent copy" "$agent"; then
      fail_msg "$agent must state that it is not a legacy compatibility Agent copy"
    fi
  done
  for agent in adapters/codex/agents/*.toml; do
    [ -f "$agent" ] || continue
    profile=$(basename "$agent" .toml)
    # memory-scout kernel helper + the generated native subagent type catalog
    # (general-purpose/light/deep, CFG_NATIVE_AGENT_CATALOG).
    case " memory-scout general-purpose light deep " in
      *" $profile "*) ;;
      *) fail_msg "$agent is not an approved Codex native agent projection" ;;
    esac
  done

  if ! grep -Fq 'model = "gpt-5.6-luna"' adapters/codex/agents/memory-scout.toml \
    || ! grep -Fq 'model_reasoning_effort = "low"' adapters/codex/agents/memory-scout.toml \
    || ! grep -Fq 'sandbox_mode = "read-only"' adapters/codex/agents/memory-scout.toml \
    || ! grep -Fq 'Never run memory mutation commands' adapters/codex/agents/memory-scout.toml; then
    fail_msg "Codex memory-scout must be low-cost read-only custom agent projection"
  fi

  bad=$(rg -n "adapters/opencode|opencode_setting|$CLAUDE_NATIVE_SURFACE_PATTERN" adapters/codex/agents 2>/dev/null || true)
  if [ -n "$bad" ]; then
    fail_msg "Codex native agent surfaces must not expose non-Codex adapter paths:"
    printf '%s\n' "$bad"
  fi
  # Runtime team agents retired 2026-07-22 (재홈, CONVENTIONS §2.3): the per-team boundary
  # prose (QA read-only/depth-one, external-adversary independence, dev write preflight,
  # mixed role-map input sets) re-homed into roles/units/** unit contracts, which
  # tools/check-unit-config.py guards. The only per-toml assertion left here is the
  # memory-scout kernel helper above plus the must-not-exist negatives.
  if ! grep -Fq 'model_reasoning_effort' adapters/codex/README.md \
    || ! grep -Fq 'parity caveat' adapters/codex/README.md \
    || ! grep -Fq 'structural plus install-path validation' adapters/codex/README.md \
    || ! grep -Fq '`codex debug agent` listing surface' adapters/codex/README.md \
    || ! grep -Fq 'role-specific runtime boundaries' adapters/codex/README.md \
    || ! grep -Fq 'model_reasoning_effort' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'parity caveat' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'structural plus install-path validation' adapters/codex/ADAPTATION.md \
    || ! grep -Fq '`codex debug agent` listing surface' adapters/codex/ADAPTATION.md; then
    fail_msg "Codex custom agent docs must state current validation boundary until runtime agent discovery exists"
  fi
}

# codex-adapter-parity audit P-26 (2026-07-04), retired-team rework 2026-07-22 (재홈,
# CONVENTIONS §2.3): the original check cross-verified each Codex role-profile agent's
# static model pin against role-map.sh resolution. Runtime team agents are retired, so
# the check is now memory-scout-only (its static read-only pin is asserted in
# check_codex_native_agent_projection) plus must-not-exist negatives: no retired team
# toml may reappear, and role-map.sh must not resolve team names or emit team paths.
check_codex_model_pin_role_map_consistency() {
  for retired in plan-team dev-team qa-team research-team material-team design-team editorial-team external-adversary; do
    if [ -e "adapters/codex/agents/$retired.toml" ]; then
      fail_msg "adapters/codex/agents/$retired.toml is a retired team projection and must not exist"
    fi
  done
  if grep -Eq 'agents/(plan|dev|qa|research|material|design|editorial)-team|agents/external-adversary' adapters/codex/bin/role-map.sh; then
    fail_msg "adapters/codex/bin/role-map.sh must not reference retired team agent paths"
  fi
}

check_codex_native_mode_projection() {
  if [ ! -x adapters/codex/bin/sync-native-modes.py ]; then
    fail_msg "adapters/codex/bin/sync-native-modes.py must be executable"
    return
  fi

  if ! adapters/codex/bin/sync-native-modes.py --check >/tmp/codex-sync-modes.out 2>/tmp/codex-sync-modes.err; then
    fail_msg "Codex native mode projections are stale; run adapters/codex/bin/sync-native-modes.py"
    cat /tmp/codex-sync-modes.err
  fi

  # Portable persona source = unit catalog (재홈 2026-07-22): iterate unit-bearing
  # catalog files; underscore-prefixed shared/law/notes fragments have no projection.
  for f in roles/units/*/*.md; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in _*) continue ;; esac
    case "$f" in roles/units/_shared/*) continue ;; esac
    rel=${f#roles/units/}
    mode=${rel%.md}
    native="adapters/codex/modes/$mode.md"
    if [ ! -f "$native" ]; then
      fail_msg "$native is missing"
      continue
    fi
    if [ -L "$native" ]; then
      fail_msg "$native must be a concrete adapter-owned Codex mode projection"
    fi
    if ! grep -Fq "roles/units/$mode.md" "$native"; then
      fail_msg "$native must reference roles/units/$mode.md as portable source"
    fi
    if ! grep -Fq "adapters/codex/bin/preflight.sh mode-info $mode" "$native"; then
      fail_msg "$native must reference the Codex mode-info wrapper"
    fi
    if ! grep -Fq "not a legacy runtime mode copy" "$native"; then
      fail_msg "$native must state that it is not a legacy runtime mode copy"
    fi
    if ! grep -Fq "Treat \`adapters/codex/modes/$mode.md\` as the adapter-owned mode guide" "$native"; then
      fail_msg "$native must report its adapter-owned native mode path"
    fi
    if ! grep -Fq "Projected Portable Mode Contract" "$native"; then
      fail_msg "$native must embed the sanitized portable mode contract"
    fi
    if grep -Eq "$CLAUDE_NATIVE_SURFACE_PATTERN" "$native"; then
      fail_msg "$native must not expose Claude-native surfaces"
    fi
    if grep -Eq "$NON_CODEX_DESIGN_SURFACE_PATTERN" "$native"; then
      fail_msg "$native must not expose non-Codex design runtime surfaces"
    fi
  done

  if ! grep -Fq "## Levels" adapters/codex/modes/qa/test.md \
    || ! grep -Fq "Behavioral runtime observation" adapters/codex/modes/qa/test.md \
    || ! grep -Fq "verification-runner" adapters/codex/modes/qa/test.md; then
    fail_msg "adapters/codex/modes/qa/test.md must project the QA graduated test contract"
  fi
  if ! grep -Fq 'Tool Contract: `visual-harness`' adapters/codex/modes/design/maker.md \
    || ! grep -Fq "preflight.sh visual-harness <file.html>" adapters/codex/modes/design/maker.md \
    || ! grep -Fq "preflight.sh visual-harness <file.html>" adapters/codex/modes/design/verifier.md; then
    fail_msg "adapters/codex/modes/design/maker.md must project the design visual harness contract"
  fi
  if ! grep -Fq "roles, units, and adapter projections" adapters/codex/modes/research/plan-review.md; then
    fail_msg "adapters/codex/modes/research/plan-review.md must sanitize runtime adapter projection references"
  fi

  for native in adapters/codex/modes/*/*.md; do
    [ -f "$native" ] || continue
    rel=${native#adapters/codex/modes/}
    mode=${rel%.md}
    if [ ! -f "roles/units/$mode.md" ]; then
      fail_msg "$native has no matching portable unit source"
    fi
  done
}

check_codex_native_hook_projection() {
  hook_dir="adapters/codex/hooks"
  hook_json="$hook_dir/hooks.json"
  session_bridge="$hook_dir/sessionstart-lifecycle.py"
  sessionend_bridge="$hook_dir/sessionend-lifecycle.py"
  stop_bridge="$hook_dir/stop-lifecycle.py"
  prompt_bridge="$hook_dir/userprompt-lifecycle.py"
  permission_bridge="$hook_dir/permissionrequest-lifecycle.py"
  pre_bridge="$hook_dir/pretooluse-write-guard.py"
  post_bridge="$hook_dir/posttooluse-design-check.py"
  read_bridge="$hook_dir/posttooluse-read-marker.py"
  launcher="$hook_dir/run-hook.sh"

  if [ ! -f "$hook_json" ]; then
    fail_msg "$hook_json is missing"
    return
  fi
  for bridge in "$session_bridge" "$sessionend_bridge" "$stop_bridge" "$prompt_bridge" "$permission_bridge" "$pre_bridge" "$post_bridge" "$read_bridge" "$launcher"; do
    if [ ! -x "$bridge" ]; then
      fail_msg "$bridge must be executable"
    fi
    if [ -L "$bridge" ]; then
      fail_msg "$bridge must be a concrete adapter-owned Codex hook bridge"
    fi
  done
  if ! python3 -m json.tool "$hook_json" >/tmp/codex-hooks-json.out 2>/tmp/codex-hooks-json.err; then
    fail_msg "$hook_json must be valid JSON"
    cat /tmp/codex-hooks-json.err
  fi
  for script in sessionstart-lifecycle.py sessionend-lifecycle.py stop-lifecycle.py userprompt-lifecycle.py permissionrequest-lifecycle.py pretooluse-write-guard.py posttooluse-design-check.py posttooluse-read-marker.py; do
    if ! grep -Fq "run-hook.sh\\\" $script" "$hook_json"; then
      fail_msg "$hook_json must register $script through the Codex hook launcher"
    fi
  done
  if ! grep -Fq '[ -f \"$root/core/CORE.md\" ]' "$hook_json" \
    || grep -Fq "\${AGENT_HOME:-\$HOME/.codex/hearting}/adapters/codex/hooks/" "$hook_json"; then
    fail_msg "$hook_json must validate harness roots before launching Codex hook bridges"
  fi
  if ! grep -Fq '"SessionStart"' "$hook_json" || ! grep -Fq 'sessionstart-lifecycle.py' "$hook_json"; then
    fail_msg "$hook_json must register the Codex SessionStart lifecycle bridge"
  fi
  if ! grep -Fq '"SessionEnd"' "$hook_json" || ! grep -Fq 'sessionend-lifecycle.py' "$hook_json"; then
    fail_msg "$hook_json must register the Codex SessionEnd lifecycle bridge"
  fi
  if ! grep -Fq '"Stop"' "$hook_json" || ! grep -Fq 'stop-lifecycle.py' "$hook_json"; then
    fail_msg "$hook_json must register the dedicated silent no-op Codex Stop bridge"
  fi
  if ! grep -Fq '"UserPromptSubmit"' "$hook_json" || ! grep -Fq 'userprompt-lifecycle.py' "$hook_json"; then
    fail_msg "$hook_json must register the Codex UserPromptSubmit lifecycle bridge"
  fi
  if ! grep -Fq '"PermissionRequest"' "$hook_json" || ! grep -Fq 'permissionrequest-lifecycle.py' "$hook_json"; then
    fail_msg "$hook_json must register the Codex PermissionRequest lifecycle bridge"
  fi
  if ! grep -Fq '"PreToolUse"' "$hook_json" || ! grep -Fq 'pretooluse-write-guard.py' "$hook_json"; then
    fail_msg "$hook_json must register the Codex PreToolUse write guard"
  fi
  if ! grep -Fq 'Write|Edit|MultiEdit|apply_patch|functions\\.apply_patch|Bash|Shell|functions\\.exec_command' "$hook_json" \
    || ! grep -Fq 'Read|Bash|Shell|functions\\.exec_command' "$hook_json"; then
    fail_msg "$hook_json must attach Codex hooks to structured tools and targeted shell command tools"
  fi
  if ! grep -Fq '"PostToolUse"' "$hook_json" || ! grep -Fq 'posttooluse-design-check.py' "$hook_json"; then
    fail_msg "$hook_json must register the Codex PostToolUse design check"
  fi
  if ! grep -Fq '"PostToolUse"' "$hook_json" || ! grep -Fq 'posttooluse-read-marker.py' "$hook_json"; then
    fail_msg "$hook_json must register the Codex PostToolUse read marker"
  fi
  for bridge in "$session_bridge" "$sessionend_bridge" "$prompt_bridge" "$pre_bridge" "$post_bridge" "$read_bridge"; do
    if ! grep -Fq 'adapters" / "codex" / "bin" / "preflight.sh' "$bridge"; then
      fail_msg "$bridge must call the Codex preflight wrapper"
    fi
    if ! grep -Fq 'def nested_string' "$bridge" \
      || ! grep -Fq '"context", "workspace", "session", "payload", "event", "input", "data"' "$bridge"; then
      fail_msg "$bridge must resolve cwd/session from nested Codex runtime payloads"
    fi
  done
  for bridge in "$pre_bridge" "$post_bridge" "$read_bridge"; do
    if ! grep -Fq 'raw_tool = payload.get("tool")' "$bridge" \
      || ! grep -Fq 'nested_mapping(payload, "tool", "toolUse", "tool_use")' "$bridge"; then
      fail_msg "$bridge must tolerate Codex hook tool payload variants"
    fi
  done
  if ! grep -Fq '"MultiEdit", "multi_edit", "multiedit"' "$pre_bridge" \
    || ! grep -Fq '"MultiEdit", "multi_edit", "multiedit"' "$post_bridge"; then
    fail_msg "Codex write/design hook bridges must treat MultiEdit as a guarded write surface"
  fi
  if ! grep -Fq 'def is_patch_tool' "$pre_bridge" \
    || ! grep -Fq 'functions.apply_patch' "$pre_bridge" \
    || ! grep -Fq 'payload, "patch", "patchText", "patch_text", "input", "text"' "$pre_bridge" \
    || ! grep -Fq 'def is_patch_tool' "$post_bridge" \
    || ! grep -Fq 'functions.apply_patch' "$post_bridge" \
    || ! grep -Fq 'payload, "patch", "patchText", "patch_text", "input", "text"' "$post_bridge"; then
    fail_msg "Codex patch hook bridges must parse qualified apply_patch names and top-level patch text"
  fi
  if ! grep -Fq 'functions\\.apply_patch' "$hook_json" \
    || ! grep -Fq 'patch_files(base, patch_text(payload, args))' "$pre_bridge" \
    || ! grep -Fq 'material-route' "$pre_bridge" \
    || ! grep -Fq 'bind_material_route' "$read_bridge"; then
    fail_msg "Codex apply_patch matcher/payload projection and material-route bridge must remain explicit"
  fi
  if ! grep -Fq 'material-route' adapters/codex/bin/preflight.sh \
    || ! grep -Fq '"$0" material-route check --tool Write --file "$file" --cwd "$(dirname "$file")" --session "$sid"' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'material-route", "check", "--tool", "Bash"' "$pre_bridge" \
    || ! grep -Fq 'material-route", "bind", "--route"' "$read_bridge" \
    || grep -Fq 'AGENT_PARENT_PARK_ONLY' "$pre_bridge" \
    || ! grep -Fq 'SessionEnd' adapters/codex/hooks/sessionend-lifecycle.py \
    || ! grep -Fq 'Stop is a' adapters/codex/hooks/sessionend-lifecycle.py; then
    fail_msg "Codex material-route write/check/bind call sites and SessionEnd-only clear must remain wired"
  fi
  if ! python3 - "$hook_json" <<'PY'
import json
import sys

hooks = json.load(open(sys.argv[1], encoding="utf-8")).get("hooks", {})
if any(item.get("matcher") in {"*", ""} for item in hooks.get("PreToolUse", [])):
    raise SystemExit(1)
post = hooks.get("PostToolUse", [])
if not any(
    item.get("matcher") == "*"
    and any("posttooluse-interaction-clear.py" in hook.get("command", "")
            for hook in item.get("hooks", []))
    for item in post
):
    raise SystemExit(1)
PY
  then
    fail_msg "Codex PreToolUse must stay targeted and PostToolUse must expose only the named wildcard interaction-release bridge"
  fi
  if ! grep -Fq '"design"' "$post_bridge"; then
    fail_msg "$post_bridge must call the Codex design preflight"
  fi
  if ! grep -Fq 'def is_shell_tool' "$post_bridge" \
    || ! grep -Fq 'def shell_write_files' "$post_bridge" \
    || ! grep -Fq 'shell_write_files(base, shell_command(payload, args))' "$post_bridge" \
    || ! grep -Fq 'mutation_commands = {"tee", "touch", "cp", "mv", "rm", "install", "rsync"}' "$post_bridge" \
    || ! grep -Fq 'if command_name in {"cp", "install", "rsync"}' "$post_bridge" \
    || ! grep -Fq 'if token.startswith("of=") and len(token) > 3' "$post_bridge" \
    || ! grep -Fq 'if command_name == "sed"' "$post_bridge" \
    || ! grep -Fq 'codex native design hook marks targeted shell design writes' hooks/portable-guards.test.sh; then
    fail_msg "$post_bridge must route targeted shell HTML writes through the design preflight"
  fi
  if ! grep -Fq 'mutation_commands = {"tee", "touch", "cp", "mv", "rm", "install", "rsync"}' "$pre_bridge" \
    || ! grep -Fq 'def shell_write_files' "$pre_bridge" \
    || ! grep -Fq 'if command_name in {"cp", "install", "rsync"}' "$pre_bridge" \
    || ! grep -Fq 'if token.startswith("of=") and len(token) > 3' "$pre_bridge" \
    || ! grep -Fq 'if command_name == "sed"' "$pre_bridge" \
    || ! grep -Fq 'add_file(operands[-1])' "$pre_bridge" \
    || ! grep -Fq 'codex native hook projection blocks common shell mutation targets' hooks/portable-guards.test.sh \
    || ! grep -Fq 'codex native hook projection treats cp destination as the shell write target' hooks/portable-guards.test.sh \
    || ! grep -Fq 'codex native hook projection blocks install and rsync destinations' hooks/portable-guards.test.sh \
    || ! grep -Fq 'codex native hook projection blocks dd output and sed inline edits' hooks/portable-guards.test.sh; then
    fail_msg "$pre_bridge must route common shell mutation command targets through the write preflight"
  fi
  if ! grep -Fq '"read"' "$read_bridge"; then
    fail_msg "$read_bridge must call the Codex read preflight"
  fi
  if ! grep -Fq '"session-end"' "$sessionend_bridge"; then
    fail_msg "$sessionend_bridge must call the Codex session-end preflight"
  fi
  if ! grep -Fq 'PreToolUse' adapters/codex/README.md \
    || ! grep -Fq 'PostToolUse' adapters/codex/README.md \
    || ! grep -Fq 'preflight.sh design' adapters/codex/README.md; then
    fail_msg "adapters/codex/README.md must document the Codex native hook bridges"
  fi
  if grep -Eq "$CLAUDE_NATIVE_SURFACE_PATTERN" "$hook_json" "$session_bridge" "$sessionend_bridge" "$prompt_bridge" "$pre_bridge" "$post_bridge" "$launcher"; then
    fail_msg "Codex hook projection must not reference Claude-native surfaces"
  fi
}

check_portable_agent_home_resolution() {
  command -v rg >/dev/null 2>&1 || return 0

  bad=$(rg -n 'AGENT_HOME=.*CLAUDE_HOME.*HOME/\.claude|os\.environ\.get\("AGENT_HOME"\).*os\.environ\.get\("CLAUDE_HOME"\).*HOME / "\.claude"' \
    tools/memory utilities/dispatch-liveness.sh \
    --glob '!*.test.sh' 2>/dev/null || true)
  if [ -n "$bad" ]; then
    fail_msg "portable tools must use neutral agent-home resolution before legacy Claude fallback:"
    printf '%s\n' "$bad"
  fi

  for p in tools/memory/mem.py adapters/claude/tools/memory/mem.py; do
    if ! grep -Fq 'HOME / "hearting"' "$p" || ! grep -Fq 'HOME / "agent_setting"' "$p"; then
      fail_msg "$p must prefer canonical ~/hearting and retain ~/agent_setting before legacy runtime home"
    fi
  done
}

check_claude_bin_wrappers() {
  if [ ! -L claude_setting/bin ]; then
    fail_msg "claude_setting/bin must project adapters/claude/bin"
    return
  fi

  target=$(readlink claude_setting/bin)
  if [ "$target" != "../adapters/claude/bin" ]; then
    fail_msg "claude_setting/bin points to $target; expected ../adapters/claude/bin"
  fi

  if [ ! -x adapters/claude/bin/mem-distill-worker.sh ]; then
    fail_msg "adapters/claude/bin/mem-distill-worker.sh is missing or not executable"
  fi
  if ! grep -Fq 'AGENT_DISPATCH_JOBS' adapters/claude/bin/dispatch-headless.py; then
    fail_msg "adapters/claude/bin/dispatch-headless.py must honor the shared dispatch registry override"
  fi
}

check_opencode_bin_wrappers() {
  if [ ! -L opencode_setting/bin ]; then
    fail_msg "opencode_setting/bin must project adapters/opencode/bin"
    return
  fi

  target=$(readlink opencode_setting/bin)
  if [ "$target" != "../adapters/opencode/bin" ]; then
    fail_msg "opencode_setting/bin points to $target; expected ../adapters/opencode/bin"
  fi

  for p in preflight.sh role-map.sh capability-map.sh mode-map.sh dispatch-headless.py dispatch-liveness.py dispatch-harvest.py distill-worker.sh sync-native-skills.py sync-native-agents.py sync-native-commands.py; do
    if [ ! -x "adapters/opencode/bin/$p" ]; then
      fail_msg "adapters/opencode/bin/$p is missing or not executable"
    fi
  done

  for p in dispatch-headless.py dispatch-liveness.py dispatch-harvest.py; do
    if ! grep -Fq 'def resolve_agent_home()' "adapters/opencode/bin/$p" \
      || { ! grep -Fq 'core" / "CORE.md"' "adapters/opencode/bin/$p" \
        && ! grep -Fq 'resolve_agent_home as _resolve_agent_home' "adapters/opencode/bin/$p"; } \
      || grep -Fq 'Path(os.environ.get("AGENT_HOME", os.getcwd()))' "adapters/opencode/bin/$p"; then
      fail_msg "adapters/opencode/bin/$p must validate AGENT_HOME before using it as the harness root"
    fi
  done
  for p in dispatch-headless.py dispatch-liveness.py dispatch-harvest.py; do
    if ! grep -Fq 'AGENT_DISPATCH_JOBS' "adapters/opencode/bin/$p"; then
      fail_msg "adapters/opencode/bin/$p must honor the shared dispatch registry override"
    fi
  done

  if ! grep -Fq 'AGENT_ROOT=$(agent_home)' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq '[ -f "$AGENT_HOME/core/CORE.md" ]' adapters/opencode/bin/preflight.sh \
    || grep -Fq 'AGENT_HOME="${AGENT_HOME:-$ROOT}"' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must validate AGENT_HOME before using it as the harness root"
  fi
  if ! grep -Fq 'AGENT_HOME="$AGENT_ROOT" "$ROOT/adapters/opencode/bin/distill-worker.sh"' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must pass a validated harness root to the distill worker"
  fi
  if ! grep -Fq 'runtime_surface=opencode-native-permission-config' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'claude_allowed_tools=unsupported' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must report the OpenCode permission contract without Claude allowedTools"
  fi
  if ! grep -Fq 'runtime_surface=opencode-native-mcp' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'claude_settings_mcp=unsupported' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'design_mcp_projection=unsupported' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must report the OpenCode MCP contract without Claude settings MCP projection"
  fi
  if ! grep -Fq 'runtime_surface=opencode-run-headless' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'preflight.sh dispatch [--dry-run|--register|--start]' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'liveness_surface=opencode-sqlite-session-mtime' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'liveness_check=adapters/opencode/bin/preflight.sh liveness [jobs.log]' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'harvest_check=adapters/opencode/bin/preflight.sh harvest' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'claude_headless=unsupported' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must report the OpenCode headless dispatch contract without Claude headless assumptions"
  fi

  if ! grep -Fq 'preflight.sh qa-policy <quick|light|standard|thorough|adversarial> [code|research|doc|general]' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'runtime_surface=opencode-qa-policy' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'stage_graph_selector=intensity-not-qa' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'preflight.sh qa-policy <level> [code|research|doc|general]' adapters/opencode/AGENTS.md \
    || ! grep -Fq 'QA policy mapping' adapters/opencode/README.md \
    || ! grep -Fq 'QA policy mapping' adapters/opencode/ADAPTATION.md; then
    fail_msg "OpenCode adapter must expose QA level policy mapping as a runtime preflight contract"
  fi
  if ! grep -Fq 'validate_preflight' adapters/opencode/bin/dispatch-headless.py \
    || ! grep -Fq 'invalid-dispatch-capability' adapters/opencode/bin/dispatch-headless.py \
    || ! grep -Fq 'invalid-dispatch-mode' adapters/opencode/bin/dispatch-headless.py \
    || ! grep -Fq 'invalid-dispatch-qa' adapters/opencode/bin/dispatch-headless.py \
    || ! grep -Fq 'render_worker_bootstrap' adapters/opencode/bin/dispatch-headless.py \
    || ! grep -Fq 'resolve_worker_type' adapters/opencode/bin/dispatch-headless.py \
    || ! grep -Fq -- '--worker-type' adapters/opencode/bin/dispatch-headless.py \
    || ! grep -Fq 'prompt_path.write_text(prompt_text, encoding="utf-8")' adapters/opencode/bin/dispatch-headless.py; then
    fail_msg "adapters/opencode/bin/dispatch-headless.py must validate inputs and materialize typed worker prompts"
  fi

  if ! grep -Fq 'adapter=opencode' adapters/opencode/bin/role-map.sh; then
    fail_msg "adapters/opencode/bin/role-map.sh must report its adapter for machine-readable role mappings"
  fi
  if ! grep -Fq 'source=roles/README.md' adapters/opencode/bin/role-map.sh; then
    fail_msg "adapters/opencode/bin/role-map.sh must report roles/README.md as the portable source"
  fi
  for var in AGENT_MODEL_FAST AGENT_MODEL_DEEP AGENT_MODEL_EXTERNAL AGENT_MODEL_ORCHESTRATOR AGENT_VARIANT_FAST AGENT_VARIANT_DEEP AGENT_VARIANT_EXTERNAL AGENT_VARIANT_ORCHESTRATOR AGENT_EXTERNAL_CMD; do
    if ! grep -Fq "$var" adapters/opencode/README.md || ! grep -Fq "$var" adapters/opencode/ADAPTATION.md; then
      fail_msg "OpenCode role mapping docs must expose $var"
    fi
  done

  for p in 'preflight.sh memory' 'preflight.sh recall' 'preflight.sh briefing' 'preflight.sh worklog' 'preflight.sh distill-delta' 'preflight.sh distill-propose'; do
    if ! grep -Fq "$p" adapters/opencode/AGENTS.md; then
      fail_msg "adapters/opencode/AGENTS.md must document manual OpenCode lifecycle wrapper $p"
    fi
  done

  if ! grep -Fq 'opencode_setting/opencode-plugins' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document the OpenCode native plugin projection"
  fi

  if ! grep -Fq 'opencode_setting/opencode-agents' adapters/opencode/AGENTS.md \
    || ! grep -Fq 'opencode_setting/opencode-commands' adapters/opencode/AGENTS.md \
    || ! grep -Fq 'opencode_setting/opencode-skills' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document OpenCode native surface projections"
  fi

  if ! grep -Fq 'named `tool_contract`, `tool_contract_check`, `runtime_surface`, and `fallback`' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document mode tool contract metadata fields"
  fi

  if ! grep -Fq 'visual-harness)' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must expose the OpenCode visual harness tool-contract"
  fi
  if ! grep -Fq 'claim-verify)' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must expose the OpenCode research claim-verify tool-contract"
  fi
  if ! grep -Fq 'browser-fetch)' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must expose the OpenCode material browser-fetch tool-contract"
  fi
  if ! grep -Fq 'data-script)' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must expose the OpenCode material data-script tool-contract"
  fi
  if ! grep -Fq 'figure-gen)' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must expose the OpenCode material figure-gen tool-contract"
  fi
  if ! grep -Fq 'pdf-extract)' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must expose the OpenCode material PDF extract tool-contract"
  fi
  if ! grep -Fq 'web-image-search)' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must expose the OpenCode material web image search tool-contract"
  fi
  if ! grep -Fq 'verification-runner)' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must expose the OpenCode QA verification-runner tool-contract"
  fi
  if ! grep -Fq 'runtime_surface=adapter-owned-visual-harness' adapters/opencode/bin/capability-map.sh \
    || ! grep -Fq 'fallback=preflight.sh visual-harness <file.html>' adapters/opencode/bin/capability-map.sh; then
    fail_msg "adapters/opencode/bin/capability-map.sh must report visual harness runtime surface and fallback"
  fi
  if ! grep -Fq 'compat_reference=not-projected' adapters/opencode/bin/capability-map.sh \
    || grep -Fq 'compat_reference="skills/' adapters/opencode/bin/capability-map.sh \
    || grep -Fq "printf 'compat_reference=skills/" adapters/opencode/bin/capability-map.sh; then
    fail_msg "adapters/opencode/bin/capability-map.sh must not expose root Skill compatibility paths as OpenCode capability-info output"
  fi
  if ! grep -Fq 'compat_reference=not-projected' adapters/opencode/README.md \
    || grep -Fq 'legacy compatibility reference, if one exists' adapters/opencode/README.md; then
    fail_msg "adapters/opencode/README.md must document that capability-info does not project root Skill compatibility references"
  fi
  if grep -Eq 'Claude Design MCP|Claude visual harness' adapters/opencode/bin/preflight.sh adapters/opencode/bin/capability-map.sh; then
    fail_msg "OpenCode runtime-facing visual harness output must use legacy/adapter-specific wording, not Claude implementation names"
  fi

  if ! grep -Fq 'preflight.sh visual-harness' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document the OpenCode visual harness tool-contract"
  fi
  if ! grep -Fq 'preflight.sh browser-fetch --check <url>' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document the OpenCode material browser-fetch tool-contract"
  fi
  if ! grep -Fq 'preflight.sh data-script --check <script.py>' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document the OpenCode material data-script tool-contract"
  fi
  if ! grep -Fq 'preflight.sh figure-gen --check <script.py>' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document the OpenCode material figure-gen tool-contract"
  fi
  if ! grep -Fq 'figure-gen --verify-report <manifest.json> <report.md>' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document fail-closed report figure QA"
  fi
  if ! grep -Fq 'preflight.sh pdf-extract --check <file.pdf>' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document the OpenCode material PDF extract tool-contract"
  fi
  if ! grep -Fq 'preflight.sh web-image-search --check <query>' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document the OpenCode material web image search tool-contract"
  fi
  if ! grep -Fq 'preflight.sh verification-runner --timeout <seconds> -- <command>' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document the OpenCode QA verification-runner tool-contract"
  fi
  if ! grep -Fq 'preflight.sh claim-verify --check <claim>' adapters/opencode/AGENTS.md; then
    fail_msg "adapters/opencode/AGENTS.md must document the OpenCode research claim-verify tool-contract"
  fi
  if ! grep -Fq 'tool_contract_check' adapters/opencode/README.md \
    || ! grep -Fq 'fallback=reference-only' adapters/opencode/README.md \
    || ! grep -Fq 'runtime_surface' adapters/opencode/README.md \
    || ! grep -Fq 'tool_contract_check' adapters/opencode/ADAPTATION.md; then
    fail_msg "OpenCode docs must document mode-info contract metadata fields"
  fi

  if ! grep -Fq 'loop-info)' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'loop-info <oncall|note|study|drill|runtime-watch>' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'source=loops/oncall.md' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'action=corroborated-offline-proposal-evidence-and-report' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'source=loops/study.md' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'source=loops/runtime-watch.md' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'action=deterministic-probe-and-proposal-report-only' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'auto_edit=unsupported' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'source=loops/drill/README.md' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'auto_run=unsupported' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'related_extension=artifact-sink' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'native_extension_surface=application-owned-plugin' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'scheduler_surface=external-application' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'fallback=extension-unavailable-or-application-scheduler' adapters/opencode/bin/preflight.sh; then
    fail_msg "adapters/opencode/bin/preflight.sh must expose OpenCode loop-info contracts without running loop scripts"
  fi
  if ! grep -Fq 'loop-info <oncall|note|study|drill|runtime-watch>' adapters/opencode/README.md \
    || ! grep -Fq 'preflight.sh loop-info <loop>' adapters/opencode/ADAPTATION.md; then
    fail_msg "OpenCode docs must document loop-info support/fallback contracts"
  fi
}

check_opencode_utility_projection() {
  if [ ! -L opencode_setting/utilities ]; then
    fail_msg "opencode_setting/utilities must project adapters/opencode/utilities"
    return
  fi

  target=$(readlink opencode_setting/utilities)
  if [ "$target" != "../adapters/opencode/utilities" ]; then
    fail_msg "opencode_setting/utilities points to $target; expected ../adapters/opencode/utilities"
  fi

  if [ ! -x "adapters/opencode/utilities/agent-home.sh" ]; then
    fail_msg "adapters/opencode/utilities/agent-home.sh must be an executable OpenCode-owned utility"
  elif [ -L "adapters/opencode/utilities/agent-home.sh" ]; then
    fail_msg "adapters/opencode/utilities/agent-home.sh must be concrete, not a symlink to the shared Claude-compatible fallback"
  elif grep -q '\.claude' "adapters/opencode/utilities/agent-home.sh"; then
    fail_msg "adapters/opencode/utilities/agent-home.sh must not fall back to Claude runtime home"
  fi
  if ! grep -Fq '[ -f "$AGENT_HOME/core/CORE.md" ]' adapters/opencode/utilities/agent-home.sh \
    || grep -Fq 'if [ "${AGENT_HOME:-}" ]; then' adapters/opencode/utilities/agent-home.sh; then
    fail_msg "adapters/opencode/utilities/agent-home.sh must validate AGENT_HOME before returning it"
  fi
  if ! grep -Fq '$HOME/.config/opencode/hearting' adapters/opencode/utilities/agent-home.sh; then
    fail_msg "adapters/opencode/utilities/agent-home.sh must support the OpenCode runtime hearting pointer"
  fi

  for p in artifact-root.sh agent-worklog-state.sh harness-status.sh worktree-cleanup.py worker_bootstrap.py; do
    if [ ! -L "adapters/opencode/utilities/$p" ]; then
      fail_msg "adapters/opencode/utilities/$p must be a selective portable utility projection"
      continue
    fi
    link=$(readlink "adapters/opencode/utilities/$p")
    if [ "$link" != "../../../utilities/$p" ]; then
      fail_msg "adapters/opencode/utilities/$p points to $link; expected ../../../utilities/$p"
    fi
  done

  extra=$(find adapters/opencode/utilities -mindepth 1 -maxdepth 1 ! \( -name agent-home.sh -o -name artifact-root.sh -o -name agent-worklog-state.sh -o -name harness-status.sh -o -name worktree-cleanup.py -o -name dispatch-route.sh -o -name dispatch-defaults.py -o -name worker_bootstrap.py \) -print 2>/dev/null || true)
  if [ -n "$extra" ]; then
    fail_msg "adapters/opencode/utilities contains unapproved entries:"
    printf '%s\n' "$extra"
  fi

  for p in dispatch-liveness.sh dispatch-wait.sh extract_web_figures.py; do
    if [ -e "adapters/opencode/utilities/$p" ] || [ -L "adapters/opencode/utilities/$p" ]; then
      fail_msg "adapters/opencode/utilities/$p must not be projected until OpenCode support is documented"
    fi
  done

  # codex-adapter-parity audit P-40 (2026-07-04): derived under-projection completeness pair — every
  # top-level utilities/* entry must be classified projected or deferred, else fail loud (closes the
  # leak window where a newly added utility silently has no projection decision).
  UTILITY_PROJECTED="agent-home.sh artifact-root.sh agent-worklog-state.sh harness-status.sh worktree-cleanup.py dispatch-route.sh dispatch-defaults.py worker_bootstrap.py"
  # mem-periodic-curate.sh is deferred, not projected: the opt-in nightly curator
  # ships Claude-first and default-off (2026-08-13, E axis). Codex/OpenCode keep
  # their own distill workers, so a projected counterpart would assert a parity
  # that does not exist yet. Revisit when the gate is enabled for real.
  UTILITY_DEFERRED="mem-periodic-curate.sh compute-hosts.py codex_hook_definition_age.py model_profile.py model-config.sh model_config.py harness-capacity.py dispatch_degradation.py dispatch_allocation_receipt.py dispatch_quality_peer.py ripple-map.py stage-session-chain.py stage_session_contract.py stage_session_runtime.py worker-state-hook.py worker-state-ledger.py artifact-root.test.sh artifact-snapshot.py artifact-sink.sh artifact-sink.test.sh artifact_identity.py artifact_manifest.py artifact_index.py artifact_admission.py artifact_lifecycle.py artifact_receipt.py artifact_producer.py artifact_cutover.py artifact_reader.py artifact_reader.test.py stage_session_runtime.test.py dispatch-artifact-root.test.py worktree-cleanup.test.py dispatch-liveness.sh dispatch-state-root.sh dispatch-liveness.test.sh dispatch_liveness_matrix.test.py dispatch-wait.sh dispatch-wait.test.sh dispatch-concurrency.test.sh usage-check.sh usage-check.test.sh dispatch-route.test.sh extract_web_figures.py token-budget.py token-budget-experiment.py capability-route.py capability_route.test.py compose-route.py compose_route.test.py dispatch-broker.py dispatch_broker.test.py dispatch-node.py dispatch_node.test.py dispatch-owner.py owner_route_binding.py dispatch-progress.py dispatch_progress.test.py dispatch-registry.py dispatch-recovery.py dispatch_registry.test.py dispatch_summary.py session_summary_trigger.py dispatch-orphan-watch.py dispatch_orphan_watch.test.py dispatch_adapters_v11.test.py dispatch_contract.py dispatch_stage_advance.py dispatch-reap-watch.py dispatch_allocation.py dispatch_mode_contract.py dispatch-observed-liveness.py dispatch_supervisor_terminal.py dispatch_contract.test.py dispatch_completion_marker.test.py dispatch_harvest.test.py dispatch_v20.test.py dispatch_lifecycle.py dispatch_continuation_budget.py dispatch_lifecycle.test.py dispatch-attempt-ready.py dispatch-batch.py launch-fence.py replica_batch_contract.py nested-dispatch-eligibility.py nested_dispatch_eligibility.test.py stage-dispatch-fallback.py stage_dispatch_fallback.test.py stage_dispatch_capacity.test.py spec-transaction.py spec_transaction.test.py worker-route-guard.py worker_route_guard.test.py model-worker-governor.py model_worker_governor.test.py resource-runner.py resource_run_registry.py resource_runner.test.py workflow_state.py workflow-supervisor.py workflow_supervisor.test.py worker_bootstrap.test.py worker_dispatch_prompt.test.py verify-files.sh verify-files.test.sh worktree-residue.py worktree_residue.test.py dispatch_codex_nocommit_fixture.test.py codex_dispatch_terminal.py codex_dispatch_terminal.test.py claude-session-supervisor.py claude_session_supervisor.test.py codex-app-server-supervisor.py codex_app_server_supervisor.test.py dispatch_completion_join.py dispatch_completion_join.test.py registered_parent_park.test.py capability-grounding.sh codex-managed-completion.py codex-managed-entry.py codex-managed-gateway.py codex_managed_dispatch.py codex-launcher.py codex-jsonl-writer.py spec_gate_evidence.py dispatch_pending_delivery.py dispatch_session_sweep.py artifact-postscan.py dispatch_launch_tuple.py"
  utility_count=0
  for f in utilities/*; do
    [ -f "$f" ] || continue
    utility_count=$((utility_count + 1))
  done
  if [ "$utility_count" -eq 0 ]; then
    fail_msg "utilities/* domain is empty; cannot verify OpenCode utility projection completeness"
    return
  fi
  for f in utilities/*; do
    [ -f "$f" ] || continue
    bn=$(basename "$f")
    # Test files are DERIVED-deferred: no *.test.* file has ever been (or may be)
    # projected into an adapter utilities dir, so the projected/deferred census
    # holds no real decision for them. Hand-listing each new test recreated the
    # "new file → forgotten census row → CI red after push" class
    # (2026-07-22 dispatch_parent_context_conformance.test.py incident).
    # Non-test utilities still require an explicit census decision below.
    case "$bn" in
      *.test.py|*.test.sh) continue ;;
    esac
    [ "$bn" = "$SHARED_UTILITY_DEFERRED" ] && continue
    case " $UTILITY_PROJECTED $UTILITY_DEFERRED $SHARED_UTILITY_DEFERRED " in
      *" $bn "*) ;;
      *) fail_msg "no projection decision for utilities/$bn (must be classified projected or deferred)" ;;
    esac
  done
}

check_opencode_tool_projection() {
  if [ ! -L opencode_setting/tools ]; then
    fail_msg "opencode_setting/tools must project adapters/opencode/tools"
    return
  fi

  target=$(readlink opencode_setting/tools)
  if [ "$target" != "../adapters/opencode/tools" ]; then
    fail_msg "opencode_setting/tools points to $target; expected ../adapters/opencode/tools"
  fi

  for p in mem.py recall.sh; do
    if [ ! -x "adapters/opencode/tools/memory/$p" ]; then
      fail_msg "adapters/opencode/tools/memory/$p must be an executable OpenCode-owned memory launcher"
    elif [ -L "adapters/opencode/tools/memory/$p" ]; then
      fail_msg "adapters/opencode/tools/memory/$p must be concrete, not a symlink to the shared Claude-compatible fallback"
    elif ! check_no_claude_native_refs "adapters/opencode/tools/memory/$p" "adapters/opencode/tools/memory/$p"; then
      :
    elif ! grep -Fq '[ -f "$AGENT_HOME/tools/memory/mem.py" ]' "adapters/opencode/tools/memory/$p" \
      || grep -Fq 'if [ "${AGENT_HOME:-}" ]; then' "adapters/opencode/tools/memory/$p"; then
      fail_msg "adapters/opencode/tools/memory/$p must validate AGENT_HOME before using it as the harness root"
    fi
  done

  for p in apply-distill-actions.py; do
    if [ ! -L "adapters/opencode/tools/memory/$p" ]; then
      fail_msg "adapters/opencode/tools/memory/$p must be a selective portable memory tool projection"
      continue
    fi
    link=$(readlink "adapters/opencode/tools/memory/$p")
    if [ "$link" != "../../../../tools/memory/$p" ]; then
      fail_msg "adapters/opencode/tools/memory/$p points to $link; expected ../../../../tools/memory/$p"
    fi
  done

  if [ ! -x adapters/opencode/tools/design/visual-harness.sh ]; then
    fail_msg "adapters/opencode/tools/design/visual-harness.sh must be an executable OpenCode-owned design launcher"
  elif [ -L adapters/opencode/tools/design/visual-harness.sh ]; then
    fail_msg "adapters/opencode/tools/design/visual-harness.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/opencode/tools/design/visual-harness.sh adapters/opencode/tools/design/visual-harness.sh; then
    :
  fi

  if [ ! -x adapters/opencode/tools/material/data-script.sh ]; then
    fail_msg "adapters/opencode/tools/material/data-script.sh must be an executable OpenCode-owned material launcher"
  elif [ -L adapters/opencode/tools/material/data-script.sh ]; then
    fail_msg "adapters/opencode/tools/material/data-script.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/opencode/tools/material/data-script.sh adapters/opencode/tools/material/data-script.sh; then
    :
  fi

  if [ ! -x adapters/opencode/tools/material/browser-fetch.sh ]; then
    fail_msg "adapters/opencode/tools/material/browser-fetch.sh must be an executable OpenCode-owned material launcher"
  elif [ -L adapters/opencode/tools/material/browser-fetch.sh ]; then
    fail_msg "adapters/opencode/tools/material/browser-fetch.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/opencode/tools/material/browser-fetch.sh adapters/opencode/tools/material/browser-fetch.sh; then
    :
  fi

  if [ ! -x adapters/opencode/tools/material/figure-gen.sh ]; then
    fail_msg "adapters/opencode/tools/material/figure-gen.sh must be an executable OpenCode-owned material launcher"
  elif [ -L adapters/opencode/tools/material/figure-gen.sh ]; then
    fail_msg "adapters/opencode/tools/material/figure-gen.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/opencode/tools/material/figure-gen.sh adapters/opencode/tools/material/figure-gen.sh; then
    :
  elif ! grep -Fq -- '--verify-report' adapters/opencode/tools/material/figure-gen.sh; then
    fail_msg "OpenCode figure-gen launcher must expose report semantic verification"
  fi

  if [ ! -x adapters/opencode/tools/material/pdf-extract.sh ]; then
    fail_msg "adapters/opencode/tools/material/pdf-extract.sh must be an executable OpenCode-owned material launcher"
  elif [ -L adapters/opencode/tools/material/pdf-extract.sh ]; then
    fail_msg "adapters/opencode/tools/material/pdf-extract.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/opencode/tools/material/pdf-extract.sh adapters/opencode/tools/material/pdf-extract.sh; then
    :
  fi

  if [ ! -x adapters/opencode/tools/material/web-image-search.sh ]; then
    fail_msg "adapters/opencode/tools/material/web-image-search.sh must be an executable OpenCode-owned material launcher"
  elif [ -L adapters/opencode/tools/material/web-image-search.sh ]; then
    fail_msg "adapters/opencode/tools/material/web-image-search.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/opencode/tools/material/web-image-search.sh adapters/opencode/tools/material/web-image-search.sh; then
    :
  fi

  if [ ! -x adapters/opencode/tools/qa/verification-runner.sh ]; then
    fail_msg "adapters/opencode/tools/qa/verification-runner.sh must be an executable OpenCode-owned QA launcher"
  elif [ -L adapters/opencode/tools/qa/verification-runner.sh ]; then
    fail_msg "adapters/opencode/tools/qa/verification-runner.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/opencode/tools/qa/verification-runner.sh adapters/opencode/tools/qa/verification-runner.sh; then
    :
  fi

  if [ ! -x adapters/opencode/tools/research/claim-verify.sh ]; then
    fail_msg "adapters/opencode/tools/research/claim-verify.sh must be an executable OpenCode-owned research launcher"
  elif [ -L adapters/opencode/tools/research/claim-verify.sh ]; then
    fail_msg "adapters/opencode/tools/research/claim-verify.sh must be concrete, not a symlink"
  elif ! check_no_claude_native_refs adapters/opencode/tools/research/claim-verify.sh adapters/opencode/tools/research/claim-verify.sh; then
    :
  fi

  extra=$(find adapters/opencode/tools -mindepth 1 ! \( -path adapters/opencode/tools/memory -o -path adapters/opencode/tools/memory/mem.py -o -path adapters/opencode/tools/memory/apply-distill-actions.py -o -path adapters/opencode/tools/memory/recall.sh -o -path adapters/opencode/tools/design -o -path adapters/opencode/tools/design/visual-harness.sh -o -path adapters/opencode/tools/material -o -path adapters/opencode/tools/material/browser-fetch.sh -o -path adapters/opencode/tools/material/data-script.sh -o -path adapters/opencode/tools/material/figure-gen.sh -o -path adapters/opencode/tools/material/pdf-extract.sh -o -path adapters/opencode/tools/material/web-image-search.sh -o -path adapters/opencode/tools/qa -o -path adapters/opencode/tools/qa/verification-runner.sh -o -path adapters/opencode/tools/research -o -path adapters/opencode/tools/research/claim-verify.sh \) -print 2>/dev/null || true)
  if [ -n "$extra" ]; then
    fail_msg "adapters/opencode/tools contains unapproved entries:"
    printf '%s\n' "$extra"
  fi

  for p in build-manifest.py check-adaptation-boundary.sh design-mcp web-bundle fleet profile; do
    if [ -e "adapters/opencode/tools/$p" ] || [ -L "adapters/opencode/tools/$p" ]; then
      fail_msg "adapters/opencode/tools/$p must not be projected until OpenCode support is documented"
    fi
  done

  # codex-adapter-parity audit P-21 (2026-07-04): derived under-projection completeness — every
  # top-level tools/* entry must be classified projected or deferred, else fail loud. design-mcp is
  # deferred-but-realized-as-visual-harness (a concrete launcher under a different name) — this
  # completeness check and the denylist above are separate assertions and must not be conflated.
  TOOL_PROJECTED="memory material figure-semantic-manifest.schema.json figure-semantic-verify.py"
  TOOL_DEFERRED="__pycache__ integrations artifact-w8-handoff.py artifact_w8_handoff.test.py build-manifest.py render-hub.py render-landing.py render-fleet-svg.py generate.py harness_manifest.py sync-skill-invocation-policy.py sync-entry-skill-layer.py entry-skill-layer.test.py generated-projections.test.sh sync-missing-projections.sh sync-missing-projections.py figure-semantic-verify.test.py check-adaptation-boundary.sh check-model-config.py check-unit-config.py check-utility-census.py context-footprint.py context-footprint-baseline.json adaptation-exemptions.tsv adaptation-guard.test.sh routing-contract.test.sh design-mcp skill-conformance web-bundle fleet profile install improvement release capability_topology.py capability_topology.test.py report-manifest-verify.py report_manifest_verify.test.py report-bundle.py report_bundle_verify.test.py smoke-attestation.py smoke_attestation.test.py lab-config-provenance.py artifact-producer-canary.py artifact-delta-census.py artifact_delta_census.test.py lab_config_provenance.test.py browser-acceptance check-runtime-memory-boundary.py runtime_memory_boundary.test.py migration-manifest.py migration_manifest.test.py git-hooks check-scope-placeholders.py scope-placeholders.tsv scope_placeholders.test.py run-tests.py run_tests.test.py test-baseline.tsv test-isolation.tsv installed-layout-triggers.tsv check-installed-layout-trigger.py dispatch-discriminators.tsv"
  tool_count=0
  for f in tools/*; do
    [ -e "$f" ] || continue
    tool_count=$((tool_count + 1))
  done
  if [ "$tool_count" -eq 0 ]; then
    fail_msg "tools/* domain is empty; cannot verify OpenCode tool projection completeness"
    return
  fi
  for f in tools/*; do
    [ -e "$f" ] || continue
    bn=$(basename "$f")
    case " $TOOL_PROJECTED $TOOL_DEFERRED " in
      *" $bn "*) ;;
      *) fail_msg "no projection decision for tools/$bn (must be classified projected or deferred)" ;;
    esac
  done
}

check_opencode_native_skill_projection() {
  if [ ! -x adapters/opencode/bin/sync-native-skills.py ]; then
    fail_msg "adapters/opencode/bin/sync-native-skills.py must be executable"
    return
  fi

  if ! adapters/opencode/bin/sync-native-skills.py --check >/tmp/opencode-native-skills.out 2>/tmp/opencode-native-skills.err; then
    fail_msg "OpenCode native skill projections are stale; run adapters/opencode/bin/sync-native-skills.py"
    cat /tmp/opencode-native-skills.err
  fi

  for f in capabilities/*.md; do
    [ -f "$f" ] || continue
    [ "$(basename "$f")" = "README.md" ] && continue
    slug=$(basename "$f" .md)
    skill="adapters/opencode/skills/$slug/SKILL.md"
    if [ ! -f "$skill" ]; then
      fail_msg "$skill is missing"
      continue
    fi
    if ! grep -Fq "capabilities/$slug.md" "$skill"; then
      fail_msg "$skill must reference capabilities/$slug.md as portable source"
    fi
    if ! grep -Fq "adapters/opencode/bin/preflight.sh capability-info $slug" "$skill"; then
      fail_msg "$skill must reference the OpenCode capability-info wrapper"
    fi
    if ! grep -Fq "not a legacy compatibility Skill copy" "$skill"; then
      fail_msg "$skill must state that it is not a legacy compatibility Skill copy"
    fi
    invocation_class=$(awk -F '\t' -v name="$slug" '$1 == name {print $2; exit}' tools/skill-conformance/invocation-policy.tsv)
    if [ -z "$invocation_class" ]; then
      fail_msg "$skill has no manifest-generated invocation classification"
    fi
    if [ "$invocation_class" = "entry-router" ]; then
      if grep -Fq '## Portable Contract' "$skill"; then
        fail_msg "$skill must keep entry-router procedure detail out of the dispatch-depth-0 projection"
      fi
      if ! grep -Fq 'five-field confirmation card' "$skill" \
        || ! grep -Fq 'dispatch-depth-1 owner reads it' "$skill" \
        || ! grep -Fq 'stage workers read only their assigned contracts' "$skill"; then
        fail_msg "$skill must project the confirmation and owner/worker context boundary"
      fi
    elif ! grep -Fq "Invocation semantics:" "$skill"; then
      fail_msg "$skill must include the portable invocation semantics excerpt"
    fi
    if ! grep -Fq 'named `tool_contract`' "$skill" \
      || ! grep -Fq '`tool_contract_check`' "$skill" \
      || ! grep -Fq '`runtime_surface` / `fallback`' "$skill" \
      || ! grep -Fq 'reported `fallback`' "$skill"; then
      fail_msg "$skill must instruct OpenCode to obey capability-info tool contract metadata"
    fi
  done
  for skill in adapters/opencode/skills/*/SKILL.md; do
    [ -f "$skill" ] || continue
    slug=$(basename "$(dirname "$skill")")
    if [ ! -f "capabilities/$slug.md" ]; then
      fail_msg "$skill has no matching portable capability source"
    fi
  done

  bad=$(rg -n 'adapters/claude|claude_setting|claude_realization|statusline\.sh|settings\.json|CLAUDE\.md|(^|[^[:alnum:]_/.-])skills/' adapters/opencode/skills adapters/opencode/bin/capability-map.sh 2>/dev/null || true)
  if [ -n "$bad" ]; then
    fail_msg "OpenCode native skill surfaces must not expose Claude-native surfaces:"
    printf '%s\n' "$bad"
  fi
}

check_opencode_native_agent_projection() {
  if [ ! -x adapters/opencode/bin/sync-native-agents.py ]; then
    fail_msg "adapters/opencode/bin/sync-native-agents.py must be executable"
    return
  fi

  if ! adapters/opencode/bin/sync-native-agents.py --check >/tmp/opencode-sync-agents.out 2>/tmp/opencode-sync-agents.err; then
    fail_msg "OpenCode native agent projections are stale; run adapters/opencode/bin/sync-native-agents.py"
    cat /tmp/opencode-sync-agents.err
  fi

  # codex-adapter-parity audit P-18 (2026-07-04), retired-team rework 2026-07-22 (재홈,
  # CONVENTIONS §2.3): runtime team agents are retired on all harnesses — the OpenCode
  # native agent domain is the kernel helper memory-scout plus the generated native
  # subagent type catalog (general-purpose/light/deep, CFG_NATIVE_AGENT_CATALOG),
  # plus must-not-exist negatives so retired team projections cannot reappear.
  for retired in plan-team dev-team qa-team research-team material-team design-team editorial-team external-adversary; do
    if [ -e "adapters/opencode/agents/$retired" ]; then
      fail_msg "adapters/opencode/agents/$retired is a retired team projection and must not exist"
    fi
  done
  for profile in memory-scout; do
    agent="adapters/opencode/agents/$profile/$profile.md"
    if [ ! -f "$agent" ]; then
      fail_msg "$agent is missing"
      continue
    fi
    if ! grep -Fq "core/MEMORY.md" "$agent" || ! grep -Fq "§7.4" "$agent"; then
      fail_msg "$agent must reference core/MEMORY.md §7.4 as portable source"
    fi
    if grep -Fq '<portable-role>' "$agent"; then
      fail_msg "$agent must not leave placeholder OpenCode role-map input"
    fi
    if ! grep -Fq "mode: subagent" "$agent"; then
      fail_msg "$agent must declare OpenCode-native subagent mode"
    fi
    if ! grep -Fq "not a non-OpenCode Agent copy" "$agent"; then
      fail_msg "$agent must state that it is not a non-OpenCode Agent copy"
    fi
  done
  for dir in adapters/opencode/agents/*; do
    [ -d "$dir" ] || continue
    profile=$(basename "$dir")
    case " memory-scout general-purpose light deep " in
      *" $profile "*) ;;
      *) fail_msg "$dir is not an approved OpenCode native agent projection" ;;
    esac
  done

  # codex-adapter-parity audit P-18 (2026-07-04): read-only frontmatter assertion for the OpenCode
  # memory-scout projection (mirrors the Codex model/reasoning/sandbox_mode/read-only-command check).
  scout_agent="adapters/opencode/agents/memory-scout/memory-scout.md"
  if [ -f "$scout_agent" ]; then
    if ! grep -Fq 'task: false' "$scout_agent" \
      || ! grep -Fq 'edit: false' "$scout_agent" \
      || ! grep -Fq 'write: false' "$scout_agent" \
      || ! grep -Fq 'task: deny' "$scout_agent" \
      || ! grep -Fq 'edit: deny' "$scout_agent" \
      || ! grep -Fq 'Never run memory mutation commands' "$scout_agent"; then
      fail_msg "$scout_agent must be a read-only OpenCode subagent projection (tools/permission deny)"
    fi
  fi

  bad=$(rg -n "$CLAUDE_NATIVE_SURFACE_PATTERN" adapters/opencode/agents 2>/dev/null || true)
  if [ -n "$bad" ]; then
    fail_msg "OpenCode native agent surfaces must not expose Claude-native surfaces:"
    printf '%s\n' "$bad"
  fi

  if ! grep -Fq 'adapters/opencode/agents/<role>/<role>.md' adapters/opencode/README.md; then
    fail_msg "adapters/opencode/README.md must map role profiles to OpenCode-native agent projections"
  fi
  if grep -Fq 'until OpenCode-native role prompts exist' adapters/opencode/README.md; then
    fail_msg "adapters/opencode/README.md must not describe OpenCode-native role prompts as future-only"
  fi
}

check_opencode_native_command_projection() {
  if [ ! -x adapters/opencode/bin/sync-native-commands.py ]; then
    fail_msg "adapters/opencode/bin/sync-native-commands.py must be executable"
    return
  fi

  if ! adapters/opencode/bin/sync-native-commands.py --check >/tmp/opencode-sync-commands.out 2>/tmp/opencode-sync-commands.err; then
    fail_msg "OpenCode native command projections are stale; run adapters/opencode/bin/sync-native-commands.py"
    cat /tmp/opencode-sync-commands.err
  fi

  for f in capabilities/*.md; do
    [ -f "$f" ] || continue
    [ "$(basename "$f")" = "README.md" ] && continue
    slug=$(basename "$f" .md)
    command="adapters/opencode/commands/$slug.md"
    if [ ! -f "$command" ]; then
      fail_msg "$command is missing"
      continue
    fi
    if ! grep -Fq "capabilities/$slug.md" "$command"; then
      fail_msg "$command must reference capabilities/$slug.md as portable source"
    fi
    if ! grep -Fq "adapters/opencode/bin/preflight.sh capability-info $slug" "$command"; then
      fail_msg "$command must reference the OpenCode capability-info wrapper"
    fi
    if ! grep -Fq "not a runtime-specific command copy" "$command"; then
      fail_msg "$command must state that it is not a runtime-specific command copy"
    fi
    if ! grep -Fq "Invocation semantics:" "$command"; then
      fail_msg "$command must include the portable invocation semantics excerpt"
    fi
    if ! grep -Fq '$ARGUMENTS' "$command"; then
      fail_msg "$command must pass OpenCode command arguments through $ARGUMENTS"
    fi
    if ! grep -Fq 'named `tool_contract`' "$command" \
      || ! grep -Fq '`tool_contract_check`' "$command" \
      || ! grep -Fq '`runtime_surface` / `fallback`' "$command" \
      || ! grep -Fq 'reported' "$command"; then
      fail_msg "$command must instruct OpenCode to obey capability-info tool contract metadata"
    fi
  done
  for command in adapters/opencode/commands/*.md; do
    [ -f "$command" ] || continue
    slug=$(basename "$command" .md)
    if [ ! -f "capabilities/$slug.md" ]; then
      fail_msg "$command has no matching portable capability source"
    fi
  done

  bad=$(rg -n 'adapters/claude|claude_setting|statusline\.sh|settings\.json|CLAUDE\.md|(^|[^[:alnum:]_/.-])skills/' adapters/opencode/commands 2>/dev/null || true)
  if [ -n "$bad" ]; then
    fail_msg "OpenCode native command surfaces must not expose Claude-native surfaces:"
    printf '%s\n' "$bad"
  fi
  if ! grep -Fq 'native_command_path=' adapters/opencode/bin/capability-map.sh \
    || ! grep -Fq 'opencode-native-skill-command' adapters/opencode/bin/capability-map.sh; then
    fail_msg "adapters/opencode/bin/capability-map.sh must report OpenCode native command realization"
  fi
  if ! grep -Fq "_RUNLOG" adapters/opencode/commands/autopilot-lab.md \
    || ! grep -Fq "_RUNLOG" capabilities/autopilot-lab.md \
    || ! grep -Fq 'five-field confirmation card' adapters/opencode/skills/autopilot-lab/SKILL.md \
    || ! grep -Fq 'five-field confirmation card' adapters/codex/skills/autopilot-lab/SKILL.md; then
    fail_msg "native autopilot-lab projections must preserve compact entry routing and owner-readable _RUNLOG detail"
  fi
}

check_opencode_native_plugin_projection() {
  plugin="adapters/opencode/plugins/hearting-guards.js"
  plugin_entries=$(find adapters/opencode/plugins -mindepth 1 -maxdepth 1 -exec basename {} \; 2>/dev/null || true)
  for entry in $plugin_entries; do
    if [ "$entry" != "hearting-guards.js" ]; then
      fail_msg "adapters/opencode/plugins/$entry is not an approved OpenCode plugin projection"
    fi
  done

  if [ ! -f "$plugin" ]; then
    fail_msg "$plugin is missing"
    return
  fi
  if [ -L "$plugin" ]; then
    fail_msg "$plugin must be a concrete adapter-owned OpenCode plugin"
  fi
  if ! node --check "$plugin" >/tmp/opencode-plugin-check.out 2>/tmp/opencode-plugin-check.err; then
    fail_msg "$plugin must parse as JavaScript"
    cat /tmp/opencode-plugin-check.err
  fi
  if ! grep -Fq '"tool.execute.before"' "$plugin"; then
    fail_msg "$plugin must use OpenCode tool.execute.before hook"
  fi
  if ! grep -Fq '"tool.execute.after"' "$plugin"; then
    fail_msg "$plugin must use OpenCode tool.execute.after hook for design checks"
  fi
  if ! grep -Fq '"experimental.chat.system.transform"' "$plugin" \
    || ! grep -Fq '"chat.message"' "$plugin" \
    || ! grep -Fq 'collectCandidates([' "$plugin" \
    || ! grep -Fq 'promptBySession.delete' "$plugin"; then
    fail_msg "$plugin must capture one prompt transiently, expose bounded candidates, then discard it"
  fi
  if ! grep -Fq 'adapters", "opencode", "bin", "preflight.sh' "$plugin"; then
    fail_msg "$plugin must bridge to the OpenCode preflight wrapper"
  fi
  if ! grep -Fq 'process.env.AGENT_HOME' "$plugin"; then
    fail_msg "$plugin must prefer AGENT_HOME for harness root resolution"
  fi
  if ! grep -Fq 'isHarnessRoot' "$plugin" \
    || ! grep -Fq '"core", "CORE.md"' "$plugin" \
    || ! grep -Fq 'AGENT_HOME: root' "$plugin" \
    || grep -Fq 'AGENT_HOME: process.env.AGENT_HOME || root' "$plugin"; then
    fail_msg "$plugin must validate AGENT_HOME and pass the selected harness root to preflight"
  fi
  for p in 'collectPreflight("memory"' 'collectPreflight("prompt-signal"' 'collectPreflight("briefing"'; do
    if ! grep -Fq "$p" "$plugin"; then
      fail_msg "$plugin must bridge OpenCode lifecycle context through $p"
    fi
  done
  if ! grep -Fq 'runPreflight("design"' "$plugin"; then
    fail_msg "$plugin must bridge design HTML writes to the OpenCode design preflight"
  fi
  if ! grep -Fq 'experimental.chat.system.transform' adapters/opencode/README.md \
    || ! grep -Fq 'tool.execute.after' adapters/opencode/README.md \
    || ! grep -Fq 'preflight.sh design' adapters/opencode/README.md; then
    fail_msg "adapters/opencode/README.md must document the OpenCode lifecycle and design plugin bridges"
  fi
  if grep -Eq "$CLAUDE_NATIVE_SURFACE_PATTERN" "$plugin"; then
    fail_msg "$plugin must not reference Claude-native surfaces"
  fi
}

check_claude_skill_projection() {
  if [ ! -L claude_setting/skills ]; then
    fail_msg "claude_setting/skills must project adapters/claude/skills"
    return
  fi

  target=$(readlink claude_setting/skills)
  if [ "$target" != "../adapters/claude/skills" ]; then
    fail_msg "claude_setting/skills points to $target; expected ../adapters/claude/skills"
  fi

  for d in skills/*; do
    [ -d "$d" ] || continue
    [ -f "$d/SKILL.md" ] || continue
    slug=${d#skills/}
    if [ ! -d "adapters/claude/skills/$slug" ]; then
      fail_msg "adapters/claude/skills/$slug must be an adapter-owned skill directory"
      continue
    fi
    if [ ! -f "adapters/claude/skills/$slug/SKILL.md" ]; then
      fail_msg "adapters/claude/skills/$slug/SKILL.md is missing"
    fi
  done

  diff_out=$(diff -qr skills adapters/claude/skills 2>/dev/null || true)
  if [ -n "$diff_out" ]; then
    fail_msg "skills/ compatibility refs must stay byte-equivalent to adapters/claude/skills/:"
    printf '%s\n' "$diff_out"
  fi
}

check_claude_mode_projection() {
  # agent-modes retired 2026-07-22 (재홈, CONVENTIONS §2.3): the persona SoT is
  # roles/units/, projected wholesale through the roles surface. Neither the adapter
  # copy nor its claude_setting symlink may reappear.
  if [ -e adapters/claude/agent-modes ] || [ -L adapters/claude/agent-modes ]; then
    fail_msg "adapters/claude/agent-modes is retired (unit catalog roles/units/ is the persona SoT) and must not exist"
  fi
  if [ -e claude_setting/agent-modes ] || [ -L claude_setting/agent-modes ]; then
    fail_msg "claude_setting/agent-modes is a retired projection and must not exist"
  fi
  if [ ! -d roles/units ]; then
    fail_msg "roles/units unit catalog is missing"
  fi
}

check_claude_hook_projection() {
  if [ ! -L claude_setting/hooks ]; then
    fail_msg "claude_setting/hooks must project adapters/claude/hooks"
    return
  fi

  target=$(readlink claude_setting/hooks)
  if [ "$target" != "../adapters/claude/hooks" ]; then
    fail_msg "claude_setting/hooks points to $target; expected ../adapters/claude/hooks"
  fi

  # Verify each canonical hook against the collapsed/wrapper/delta contract.
  for f in hooks/*; do
    [ -f "$f" ] || continue
    name=${f#hooks/}
    if [ ! -e "adapters/claude/hooks/$name" ]; then
      fail_msg "adapters/claude/hooks/$name is missing"
      continue
    fi
    assert_shared_adapter_class hooks "$name"
  done
}

check_claude_utility_projection() {
  if [ ! -L claude_setting/utilities ]; then
    fail_msg "claude_setting/utilities must project adapters/claude/utilities"
    return
  fi

  target=$(readlink claude_setting/utilities)
  if [ "$target" != "../adapters/claude/utilities" ]; then
    fail_msg "claude_setting/utilities points to $target; expected ../adapters/claude/utilities"
  fi

  # Whole-layer collapse (2026-07-22): adapters/claude/utilities is ONE symlink
  # to the shared portable layer, so every utilities/* file — including a newly
  # added one — resolves for Claude with zero per-file mirror work. Per-file
  # mirrors and per-file deltas are retired; the last delta
  # (agent-worklog-state.sh local paths) moved to runtime settings.json env
  # (AGENT_NOTES_ROOT / WORKLOG_BOARD_APP / WORKLOG_BOARD_WT). A real directory
  # here means someone reintroduced the mirror layer — fail loud; a future
  # Claude-only utility delta requires deliberately reintroducing the per-file
  # layer plus an exemptions row, never a silent real file.
  if [ ! -L adapters/claude/utilities ]; then
    fail_msg "adapters/claude/utilities must be a whole-layer symlink to ../../utilities (per-file mirror retired)"
  else
    target=$(readlink adapters/claude/utilities)
    if [ "$target" != "../../utilities" ]; then
      fail_msg "adapters/claude/utilities points to $target; expected ../../utilities"
    elif [ ! -d adapters/claude/utilities/ ]; then
      fail_msg "adapters/claude/utilities whole-layer symlink target is missing"
    fi
  fi

  for p in dispatch-liveness.sh dispatch-wait.sh; do
    if ! grep -Fq 'AGENT_DISPATCH_JOBS' "utilities/$p"; then
      fail_msg "utilities/$p must honor the shared dispatch registry override"
    fi
  done
}

# Guard runtimes and portable-guard tests are collapsed symlinks to canonical.
check_claude_boundary_guard_projection() {
  assert_shared_adapter_class tools check-adaptation-boundary.sh
  if [ ! -x adapters/claude/tools/check-adaptation-boundary.sh ]; then
    fail_msg "adapters/claude/tools/check-adaptation-boundary.sh must resolve to an executable canonical guard"
  fi
}

check_claude_portable_guards_projection() {
  assert_shared_adapter_class hooks portable-guards.test.sh
  if [ ! -x adapters/claude/hooks/portable-guards.test.sh ]; then
    fail_msg "adapters/claude/hooks/portable-guards.test.sh must resolve to an executable canonical portable-guards test"
  fi
}

check_claude_drill_runner_projection() {
  adapter_runner=adapters/claude/loops/drill/run.sh
  root_runner=loops/drill/run.sh

  # 2026-08-25: the runner is a collapsed file symlink like every other loop file;
  # a concrete copy here drifted twice (cb4aafd, and the 2026-08 loops/ split).
  if [ ! -L "$adapter_runner" ]; then
    fail_msg "$adapter_runner must be a file symlink to ../../../../$root_runner"
    return
  fi
  if [ "$(readlink "$adapter_runner")" != "../../../../$root_runner" ]; then
    fail_msg "$adapter_runner points to $(readlink "$adapter_runner"); expected ../../../../$root_runner"
  fi
  if [ ! -x "$adapter_runner" ]; then
    fail_msg "$adapter_runner must resolve to an executable drill runner"
  fi
}

# Collapsed file-symlink projection contract shared by loops/ and scaffolds/
# (2026-08-25): directories stay concrete containers, every tracked file is a
# depth-aware relative symlink to the canonical root file. A concrete copy is a
# second edit site that drifts silently (fleet/loops/scaffolds all did).
check_claude_collapsed_domain() {
  domain=$1
  for p in $(find "$domain" -mindepth 1 -print); do
    git check-ignore -q "$p" 2>/dev/null && continue
    rel=${p#$domain/}
    adapter_p=adapters/claude/$domain/$rel
    case "$rel" in
      .gitignore|*/.gitignore)
        # git never reads a symlinked .gitignore; keep a byte-equal concrete copy.
        if [ -L "$adapter_p" ]; then
          fail_msg "$adapter_p must be a concrete .gitignore copy (git does not follow symlinked ignore files)"
        elif ! cmp -s "$p" "$adapter_p"; then
          fail_msg "$adapter_p must stay byte-equivalent to $p"
        fi
        continue
        ;;
    esac
    if [ -d "$p" ]; then
      if [ -L "$adapter_p" ]; then
        fail_msg "$adapter_p must be a concrete directory container (holds collapsed file symlinks)"
      else
        [ -d "$adapter_p" ] || fail_msg "$adapter_p is missing"
      fi
    elif [ -f "$p" ]; then
      if [ ! -L "$adapter_p" ]; then
        fail_msg "$adapter_p must be a file symlink to canonical $p, not a concrete copy"
        continue
      fi
      expected=$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],os.path.dirname(sys.argv[2])))' "$p" "$adapter_p")
      if [ "$(readlink "$adapter_p")" != "$expected" ]; then
        fail_msg "$adapter_p points to $(readlink "$adapter_p"); expected $expected"
      fi
    fi
  done
}

check_claude_scaffold_projection() {
  if [ ! -L claude_setting/scaffolds ]; then
    fail_msg "claude_setting/scaffolds must project adapters/claude/scaffolds"
    return
  fi

  target=$(readlink claude_setting/scaffolds)
  if [ "$target" != "../adapters/claude/scaffolds" ]; then
    fail_msg "claude_setting/scaffolds points to $target; expected ../adapters/claude/scaffolds"
  fi

  check_claude_collapsed_domain scaffolds
}

check_claude_loop_projection() {
  if [ ! -L claude_setting/loops ]; then
    fail_msg "claude_setting/loops must project adapters/claude/loops"
    return
  fi

  target=$(readlink claude_setting/loops)
  if [ "$target" != "../adapters/claude/loops" ]; then
    fail_msg "claude_setting/loops points to $target; expected ../adapters/claude/loops"
  fi

  check_claude_collapsed_domain loops
}

check_claude_tool_projection() {
  if [ ! -L claude_setting/tools ]; then
    fail_msg "claude_setting/tools must project adapters/claude/tools"
    return
  fi

  target=$(readlink claude_setting/tools)
  if [ "$target" != "../adapters/claude/tools" ]; then
    fail_msg "claude_setting/tools points to $target; expected ../adapters/claude/tools"
  fi

  # Apply the three-class contract to tools files at every depth; directories
  # remain concrete containers for collapsed file symlinks.
  for p in $(find tools -mindepth 1 ! -path '*/__pycache__' ! -path '*/__pycache__/*' \
      ! -path '*/node_modules' ! -path '*/node_modules/*' -print); do
    rel=${p#tools/}
    adapter_p=adapters/claude/tools/$rel
    case "$rel" in
      generate.py|harness_manifest.py|sync-skill-invocation-policy.py|sync-entry-skill-layer.py|entry-skill-layer.test.py|generated-projections.test.sh|sync-missing-projections.sh|sync-missing-projections.py|context-footprint-baseline.json|install/projection-completeness.test.sh|install/test_compute_hosts_launcher.py|release|release/*|render-landing.py|render-fleet-svg.py|lab-config-provenance.py|lab_config_provenance.test.py|artifact-producer-canary.py|artifact-w8-handoff.py|artifact_w8_handoff.test.py|artifact-delta-census.py|artifact_delta_census.test.py|git-hooks|git-hooks/*)
        # Harness-development, profile acceptance, repository release, Git
        # maintainer hooks, and GitHub Pages docs automation tools are
        # intentionally not runtime projections.
        continue
        ;;
    esac
    if is_census_deferred "$adapter_p"; then
      # In-flight deferred subtrees retain their existing concrete contract.
      if [ -d "$p" ]; then
        [ -d "$adapter_p" ] || fail_msg "$adapter_p is missing"
      elif [ -f "$p" ]; then
        [ -e "$adapter_p" ] || fail_msg "$adapter_p is missing"
      fi
      continue
    fi
    case "$rel" in
      */*)
        # Nested directories stay concrete; files follow the three-class contract.
        if [ -d "$p" ]; then
          if [ -L "$adapter_p" ]; then
            fail_msg "$adapter_p must be a concrete adapter-owned tool directory (holds collapsed file symlinks)"
          else
            [ -d "$adapter_p" ] || fail_msg "$adapter_p is missing"
          fi
        elif [ -f "$p" ]; then
          if [ ! -e "$adapter_p" ]; then
            fail_msg "$adapter_p is missing"
          else
            assert_shared_adapter_class tools "$rel"
          fi
        fi
        ;;
      *)
        if [ -d "$p" ]; then
          # Top-level tool directories remain concrete.
          if [ -L "$adapter_p" ]; then
            fail_msg "$adapter_p must be a concrete adapter-owned tool projection"
          else
            [ -d "$adapter_p" ] || fail_msg "$adapter_p is missing"
          fi
        elif [ -f "$p" ]; then
          if [ ! -e "$adapter_p" ]; then
            fail_msg "$adapter_p is missing"
          else
            assert_shared_adapter_class tools "$rel"
          fi
        fi
        ;;
    esac
  done
}

# Recursively census every shared layer so nested concrete copies cannot evade
# the collapsed/wrapper/delta contract. Only declared exceptions and temporary
# CENSUS_DEFERRED ownership are allowed.
check_claude_shared_layer_census() {
  for _layer in hooks tools utilities; do
    _root="adapters/claude/$_layer"
    [ -d "$_root" ] || continue
    # Census scope = tracked + untracked-non-ignored files. Gitignored runtime
    # artifacts (node_modules, installed deps) are not projections of canonical
    # and must not enter the census; a bare find also walks node_modules and
    # blows up guard runtime. Worktree checkouts lack these dirs, so the
    # Phase 1b in-worktree verification could not observe either failure mode.
    for _f in $(git ls-files --cached --others --exclude-standard -- "$_root" 2>/dev/null); do
      [ -f "$_f" ] || continue
      [ -L "$_f" ] && continue
      case "$_f" in */__pycache__/*) continue ;; esac
      is_census_deferred "$_f" && continue
      _cls=$(exemption_class "$_f")
      if [ -z "$_cls" ]; then
        _rel=${_f#adapters/claude/}
        fail_msg "$_f is a non-symlink real file under the shared $_layer/ layer but is not declared wrapper/delta in $EXEMPTIONS_FILE; collapse it to canonical $_rel (symlink) or declare an exemption (harness-layer-sync recursive census — HLS-5/7)"
      fi
    done
  done
}

check_removed_root_surfaces() {
  if [ -e agents ] || [ -L agents ]; then
    fail_msg "root agents/ exists; Claude-native agents must live under adapters/claude/agents and portable meaning under roles/"
  fi
  if [ -e agent-modes ] || [ -L agent-modes ]; then
    fail_msg "root agent-modes/ exists; portable mode fragments must live under roles/modes and runtime projection under adapters/*/agent-modes"
  fi
}

check_role_catalog() {
  if [ ! -f roles/README.md ]; then
    fail_msg "roles/README.md is missing"
    return
  fi

  # 재홈 2026-07-22 (CONVENTIONS §2.3): roles/README.md renders the generated UNIT
  # catalog table; every catalog unit has a row and retired team rows must not reappear.
  if ! grep -Fq '| Family | Unit | Portable model role | Worker type | Floor |' roles/README.md; then
    fail_msg "roles/README.md must render the unit catalog table header (Family/Unit/Portable model role/Worker type/Floor)"
  fi
  for unit_file in roles/units/*/*.md; do
    [ -f "$unit_file" ] || continue
    unit_family=$(basename "$(dirname "$unit_file")")
    unit_name=$(basename "$unit_file" .md)
    case "$unit_family" in _*) continue ;; esac
    case "$unit_name" in _*) continue ;; esac
    if ! grep -Fq "| \`$unit_family/$unit_name\` |" roles/README.md; then
      fail_msg "roles/README.md unit catalog table is missing unit row: $unit_family/$unit_name"
    fi
  done
  for retired in plan-team dev-team qa-team research-team material-team design-team editorial-team external-adversary; do
    if grep -Fq "| \`$retired\` |" roles/README.md; then
      fail_msg "roles/README.md must not carry retired team profile row: $retired"
    fi
    if grep -Fq "adapters/codex/agents/$retired.toml" roles/README.md \
      || grep -Fq "adapters/opencode/agents/$retired/$retired.md" roles/README.md; then
      fail_msg "roles/README.md must not reference retired native team agent projections: $retired"
    fi
  done
}

check_claude_native_agent_projection() {
  # codex-adapter-parity audit P-01 (2026-07-04): derived completeness guard — every
  # adapters/claude/agents/*.md must resolve (via an explicit name-map) to an existing Codex TOML
  # AND OpenCode dir. Complements (does not replace) the existing per-profile field-validation loops
  # in check_codex_native_agent_projection / check_opencode_native_agent_projection, which own the
  # rich field checks; this guard owns only the completeness invariant (the structural root of the
  # memory-scout OpenCode-projection leak, P-18).
  claude_agent_count=0
  for a in adapters/claude/agents/*.md; do
    [ -f "$a" ] || continue
    claude_agent_count=$((claude_agent_count + 1))
  done
  if [ "$claude_agent_count" -eq 0 ]; then
    fail_msg "adapters/claude/agents/*.md domain is empty; cannot verify Claude native agent projection completeness"
    return
  fi

  # CLAUDE_AGENT_PROJECTION_EXEMPT: agents intentionally excluded from the projection-completeness
  # requirement. Empty today — the mechanism, not a current exemption.
  CLAUDE_AGENT_PROJECTION_EXEMPT=""

  for a in adapters/claude/agents/*.md; do
    [ -f "$a" ] || continue
    name=$(basename "$a" .md)

    case " $CLAUDE_AGENT_PROJECTION_EXEMPT " in
      *" $name "*) continue ;;
    esac

    # explicit name-map: identity by default; declared renames only (never silent).
    # memory-scout is an EXTRA agent (present in both adapters, exempt from the roles/README.md
    # unit-catalog table check_role_catalog owns) but still requires this completeness projection.
    # (재홈 2026-07-22: the codex-review-team → external-adversary rename died with the teams.)
    target=$name

    if [ ! -f "adapters/codex/agents/$target.toml" ]; then
      fail_msg "adapters/claude/agents/$name.md has no Codex native agent projection (expected adapters/codex/agents/$target.toml)"
    fi
    if [ ! -f "adapters/opencode/agents/$target/$target.md" ]; then
      fail_msg "adapters/claude/agents/$name.md has no OpenCode native agent projection (expected adapters/opencode/agents/$target/$target.md)"
    fi
  done
}

check_adaptation_inventory_native_surfaces() {
  if [ ! -f core/ADAPTATION_INVENTORY.md ]; then
    fail_msg "core/ADAPTATION_INVENTORY.md is missing"
    return
  fi

  # Compare ledger file lists against build-manifest-derived native hook surfaces.
  _derived_codex_hooks=$(python3 tools/build-manifest.py --adaptation-surface codex-hooks 2>/dev/null || true)
  if [ -z "$_derived_codex_hooks" ]; then
    fail_msg "could not derive Codex native hook surface for ADAPTATION_INVENTORY ledger cross-check"
  fi
  for _h in $_derived_codex_hooks; do
    if ! grep -Fq "$_h" core/ADAPTATION_INVENTORY.md; then
      fail_msg "core/ADAPTATION_INVENTORY.md 'Codex native hook surface' row must list derived hook bridge $_h (ledger drifted from filesystem-derived set)"
    fi
  done

  if grep -Fq 'Future runtimes need native command wrappers or instruction entries' core/ADAPTATION_INVENTORY.md; then
    fail_msg "core/ADAPTATION_INVENTORY.md must not describe non-Claude command surfaces as future-only"
  fi
  if ! grep -Fq 'Codex command-like surface' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq 'OpenCode native command surface' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq 'Claude slash commands' core/ADAPTATION_INVENTORY.md; then
    fail_msg "core/ADAPTATION_INVENTORY.md must distinguish Claude slash commands, Codex command-like Skills, and OpenCode commands"
  fi
  if ! grep -Fq '"claude.compile-smoke"' tools/install/drivers/claude.py \
    || ! grep -Fq "compile(open(f,encoding='utf-8').read(), f, 'exec')" tools/install/drivers/claude.py; then
    fail_msg "Claude installer verify must syntax-check build-manifest/mem via in-memory compile without writing __pycache__"
  fi
  for s in install-runtime-projection.sh check-runtime-projection.sh; do
    if [ ! -x "adapters/codex/bin/$s" ]; then
      fail_msg "adapters/codex/bin/$s must exist and be executable (Codex runtime projection installer/checker)"
    fi
  done
  if [ ! -x adapters/codex/bin/hook-trust-status.py ] \
    || [ ! -f adapters/codex/bin/hook-trust-status.test.py ] \
    || ! grep -Fq 'hooks/list' adapters/codex/bin/hook-trust-status.py \
    || ! grep -Fq 'current-hash-not-trusted' adapters/codex/bin/hook-trust-status.test.py; then
    fail_msg "Codex hook trust check must use the authoritative current-hash App Server surface"
  fi
  if ! grep -Fq 'install-runtime-projection.sh' adapters/codex/README.md \
    || ! grep -Fq 'check-runtime-projection.sh' adapters/codex/README.md \
    || ! grep -Fq 'install-runtime-projection.sh' adapters/codex/AGENTS.md \
    || ! grep -Fq 'preflight.sh doctor --runtime' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'preflight.sh doctor [--runtime|--runtime-strict]' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'runtime-projection [--require-hook-trust]' adapters/codex/bin/preflight.sh \
    || ! grep -Fq -- '--runtime-strict' adapters/codex/bin/preflight.sh \
    || ! grep -Fq -- '--require-hook-trust' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'check=runtime-projection:skipped' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'check=hook-trust:review-needed' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'hook-trust-status.py' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'current_hash=verified' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'CODEX_REQUIRE_HOOK_TRUST=1' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'hearting-readme' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'agent-capabilities' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'agent-roles' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'agent-bin' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'agent-tools' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'agent-utilities' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'check=skill-link:%s:ok' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'check=skill-link:%s:absent' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'check=skill-discovery:plugin' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'plugin-skill-discovery' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'check=agent-link:%s:ok' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'harness-skills-miswired' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'harness-agents-not-linked-or-miswired' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'CODEX_RUNTIME_PROJECTION_CLI_TIMEOUT' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'codex-cli-timeout' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'CODEX_RUNTIME_PROJECTION_FAST=1' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'reason=fast-pinned-identity' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'reason=strict-doctor' adapters/codex/bin/check-runtime-projection.sh \
    || ! grep -Fq 'CODEX_RUNTIME_PROJECTION_SKIP_CLI_DISCOVERY=1' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'check=hook-trust:review-needed' adapters/codex/README.md \
    || ! grep -Fq 'authoritative App Server `hooks/list`' adapters/codex/README.md \
    || ! grep -Fq 'authoritative App Server `hooks/list`' adapters/codex/ADAPTATION.md \
    || ! grep -Fq 'does not force Stop/PreToolUse trust' adapters/codex/AGENTS.md \
    || ! grep -Fq 'doctor --runtime-strict' adapters/codex/README.md \
    || ! grep -Fq 'runtime-projection --require-hook-trust' adapters/codex/AGENTS.md \
    || ! grep -Fq 'check=hook-trust:review-needed' adapters/codex/ADAPTATION.md; then
    fail_msg "adapters/codex/README.md and adapters/codex/AGENTS.md must document the Codex runtime projection installer/checker"
  fi
  if ! grep -Fq 'permissionrequest-lifecycle.py' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'check-runtime-projection.sh' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'doctor_check native-subagents "$0" subagent-info --check' adapters/codex/bin/preflight.sh; then
    fail_msg "Codex preflight doctor/headless checks must syntax-check all hook bridges and reuse runtime projection validation"
  fi
  if [ ! -x adapters/codex/bin/apply-tui-config.sh ] \
    || [ ! -f adapters/codex/config/tui-statusline.toml ] \
    || ! grep -Fq 'status_line = ["project-name", "git-branch", "context-used", "current-dir", "model-with-reasoning", "five-hour-limit", "weekly-limit"]' adapters/codex/config/tui-statusline.toml \
    || ! grep -Fq 'status_line_use_colors = true' adapters/codex/config/tui-statusline.toml \
    || ! grep -Fq 'codex_setting/codex-config/tui-statusline.toml' adapters/codex/README.md \
    || ! grep -Fq 'codex_setting/codex-config/tui-statusline.toml' adapters/codex/AGENTS.md \
    || ! grep -Fq 'preflight.sh tui-config' adapters/codex/README.md \
    || ! grep -Fq 'preflight.sh tui-config' adapters/codex/AGENTS.md \
    || ! grep -Fq 'preflight.sh tui-config' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq 'statusline_fragment=codex_setting/codex-config/tui-statusline.toml' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'managed_keys=status_line,status_line_use_colors' adapters/codex/bin/apply-tui-config.sh \
    || ! grep -Fq 'Do not project or commit the full `$CODEX_HOME/config.toml`' core/ADAPTATION_INVENTORY.md; then
    fail_msg "Codex statusline config must be captured as an adapter-owned fragment, not full runtime config.toml"
  fi
  if [ ! -f adapters/codex/config/approval-sandbox.toml ] \
    || ! grep -Fq 'approvals_reviewer = "user"' adapters/codex/config/approval-sandbox.toml \
    || ! grep -Fq 'trust_level = "trusted"' adapters/codex/config/approval-sandbox.toml \
    || ! grep -Fq 'config_fragment=codex_setting/codex-config/approval-sandbox.toml' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'approval-sandbox.toml' adapters/codex/ADAPTATION.md; then
    fail_msg "Codex approval/sandbox posture must be captured as an adapter-owned config fragment (approval-sandbox.toml), referenced by preflight permissions and ADAPTATION.md"
  fi
  if grep -Fq 'core settings.json keybindings.json commands' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must not symlink runtime-owned settings.json/keybindings.json into the Claude home; Claude Code rewrites them in place and clobbers the symlink"
  fi
  if ! grep -Fq '_CLAUDE_COPY_ONCE_NAMES = ["settings.json", "keybindings.json"]' tools/install/projector.py \
    || ! grep -Fq 'if action == "copy_once":' tools/install/drivers/claude.py; then
    fail_msg "Claude installer must copy-once (not symlink) runtime-owned settings.json/keybindings.json so runtime writes do not pollute the repo"
  fi
  if ! grep -Fq 'Codex/OpenCode `preflight.sh loop-info`' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq 'without executing Claude-coupled loop scripts' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq 'unsupported/manual-contract' core/ADAPTATION_INVENTORY.md; then
    fail_msg "core/ADAPTATION_INVENTORY.md must describe Codex/OpenCode loop-info native support/fallback contracts"
  fi
  if ! grep -Fq 'runtime hook output protocol' core/HOOKS.md \
    || ! grep -Fq 'Hook stdout must match the owning runtime' core/HOOKS.md \
    || ! grep -Fq 'Portable helper text is never forwarded as raw hook stdout' core/HOOKS.md \
    || ! grep -Fq 'Runtime hook output contract' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq 'Helper CLI text must not leak into native hook stdout' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq 'final stdout protocol' core/ADAPTATION_INVENTORY.md; then
    fail_msg "core hook docs must record the adapter-owned native hook stdout protocol invariant"
  fi
  if ! grep -Fq 'adapters/codex/bin/preflight.sh liveness [jobs.log]' core/OPERATIONS.md \
    || ! grep -Fq 'adapters/opencode/bin/preflight.sh liveness [jobs.log]' core/OPERATIONS.md \
    || ! grep -Fq 'adapter liveness wrapper' core/OPERATIONS.md; then
    fail_msg "core/OPERATIONS.md must describe adapter-native liveness wrappers, not only the shared Claude-compatible dispatch-liveness helper"
  fi
  if ! grep -Fq 'Codex and OpenCode expose `preflight.sh distill-propose` as a no-tools worker tool-contract by default' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq 'Codex exits 69 until `CODEX_DISTILL_ENABLE=1`' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq 'OpenCode exits 69 until `OPENCODE_DISTILL_ENABLE=1`' core/ADAPTATION_INVENTORY.md; then
    fail_msg "core/ADAPTATION_INVENTORY.md must describe adapter distill-propose tool-contract boundaries"
  fi
  if ! grep -Fq 'SessionEnd and turn-counter triggers may launch a no-tools distiller agent' core/MEMORY.md \
    || ! grep -Fq 'Codex adapter-owned `session-end` and' core/HOOKS.md \
    || ! grep -Fq 'read-only `codex exec` tool-free proof' core/HOOKS.md \
    || ! grep -Fq 'verified automatic distill worker' adapters/codex/ADAPTATION.md \
    || grep -Fq 'opt-in distill proposal worker' adapters/codex/ADAPTATION.md; then
    fail_msg "core memory/hooks docs and Codex adaptation docs must distinguish user-facing distill-propose preview from verified automatic lifecycle distillation"
  fi
  if ! grep -Fq 'Codex, leave `/statusline`' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq '`/title` as native built-in item configuration surfaces' core/ADAPTATION_INVENTORY.md \
    || ! grep -Fq 'preflight.sh ui-info' core/ADAPTATION_INVENTORY.md; then
    fail_msg "core/ADAPTATION_INVENTORY.md must preserve the Codex /statusline vs harness status split"
  fi
  if ! grep -Fq 'capabilities/analyze-user.md' core/MEMORY.md \
    || ! grep -Fq 'adapter-native `analyze-user` projection' core/MEMORY.md \
    || ! grep -Fq 'root `skills/analyze-user/SKILL.md` is only a compatibility reference' core/MEMORY.md \
    || grep -Fq 'The agent-centric table in `skills/analyze-user/SKILL.md` is an isomorphic view' core/MEMORY.md; then
    fail_msg "core/MEMORY.md must route analyze-user profile mapping through portable capability and adapter projections, not root Skill compatibility refs"
  fi
}

check_projection_summary_docs() {
  if grep -Fq 'minimal adapted bootstrap + shared core/capabilities/roles/tools' README.md INSTALL_LAYOUT.md 2>/dev/null; then
    fail_msg "projection summary docs must describe current native adapter projections, not minimal bootstrap only"
  fi
  if grep -Fq 'Codex does not currently consume the full harness natively' README.md INSTALL_LAYOUT.md 2>/dev/null; then
    fail_msg "Codex install docs must describe selected native projections instead of implying instruction-only support"
  fi
  if ! grep -Fq '| Codex | Skills, custom agents, modes, and hooks |' README.md \
    || ! grep -Fq '| OpenCode | Skills, agents, commands, and local guard plugin |' README.md; then
    fail_msg "README.md must summarize Codex and OpenCode native projection surfaces"
  fi
  if ! grep -Fq 'Codex-native Skills, custom Agents, mode' INSTALL_LAYOUT.md \
    || ! grep -Fq 'hook bridges' INSTALL_LAYOUT.md; then
    fail_msg "INSTALL_LAYOUT.md must summarize Codex native projection install surfaces"
  fi
}

check_capability_catalog() {
  if [ ! -f capabilities/README.md ]; then
    fail_msg "capabilities/README.md is missing"
    return
  fi

  for d in skills/*; do
    [ -d "$d" ] || continue
    [ -f "$d/SKILL.md" ] || continue
    slug=${d#skills/}
    if ! grep -Fq "| \`$slug\` |" capabilities/README.md; then
      fail_msg "capabilities/README.md is missing skill capability: $slug"
    fi
    if ! grep -Fq "adapters/opencode/skills/$slug/SKILL.md" capabilities/README.md \
      || ! grep -Fq "adapters/opencode/commands/$slug.md" capabilities/README.md; then
      fail_msg "capabilities/README.md must document OpenCode native projections for $slug"
    fi
    if [ ! -f "capabilities/$slug.md" ]; then
      fail_msg "capabilities/$slug.md is missing portable capability spec"
      continue
    fi
    if ! grep -Fq "| Codex | Read this spec and run \`adapters/codex/bin/preflight.sh capability-info $slug\`" "capabilities/$slug.md"; then
      fail_msg "capabilities/$slug.md must document Codex capability-info realization"
    fi
    if ! grep -Fq "adapters/codex/skills/$slug/SKILL.md" "capabilities/$slug.md"; then
      fail_msg "capabilities/$slug.md must document the Codex native Skill projection"
    fi
    if ! grep -Fq "| OpenCode | Read this spec and run \`adapters/opencode/bin/preflight.sh capability-info $slug\`" "capabilities/$slug.md"; then
      fail_msg "capabilities/$slug.md must document OpenCode capability-info realization"
    fi
    if ! grep -Fq "adapters/opencode/skills/$slug/SKILL.md" "capabilities/$slug.md" \
      || ! grep -Fq "adapters/opencode/commands/$slug.md" "capabilities/$slug.md"; then
      fail_msg "capabilities/$slug.md must document OpenCode native Skill and command projections"
    fi
  done
}

check_codex_capability_map() {
  mapper=adapters/codex/bin/capability-map.sh
  if [ ! -x "$mapper" ]; then
    fail_msg "$mapper is missing or not executable"
    return
  fi

  for d in skills/*; do
    [ -d "$d" ] || continue
    [ -f "$d/SKILL.md" ] || continue
    slug=${d#skills/}
    if ! "$mapper" "$slug" >/dev/null 2>&1; then
      fail_msg "Codex capability map cannot resolve skill capability: $slug"
    fi
  done
}

check_codex_mode_map() {
  mapper=adapters/codex/bin/mode-map.sh
  if [ ! -x "$mapper" ]; then
    fail_msg "$mapper is missing or not executable"
    return
  fi

  for f in roles/units/*/*.md; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in _*) continue ;; esac
    case "$f" in roles/units/_shared/*) continue ;; esac
    rel=${f#roles/units/}
    rel=${rel%.md}
    out=${TMPDIR:-/tmp}/codex-mode-map.$$.out
    err=${TMPDIR:-/tmp}/codex-mode-map.$$.err
    if ! "$mapper" "$rel" >"$out" 2>"$err"; then
      fail_msg "Codex mode map cannot resolve agent mode: $rel"
      cat "$err"
      continue
    fi
    case "$rel" in
      design/*)
        native_path="adapters/codex/modes/$rel.md"
        if [ ! -f "$native_path" ]; then
          fail_msg "Codex design mode realization missing: $native_path"
        fi
        if [ -L "$native_path" ]; then
          fail_msg "Codex design mode realization must be concrete: $native_path"
        fi
        if grep -Eq "$CLAUDE_NATIVE_SURFACE_PATTERN" "$native_path"; then
          fail_msg "Codex design mode realization must not reference Claude-native surfaces: $native_path"
        fi
        if ! grep -Fq 'status=tool-contract' "$out" || ! grep -Fq 'realization=codex-native-mode-with-tool-contract' "$out"; then
          fail_msg "Codex mode map must mark $rel as Codex-native tool-contract"
        fi
        if ! grep -Fq 'tool_contract=visual-harness' "$out" \
          || ! grep -Fq 'tool_contract_check=adapters/codex/bin/preflight.sh visual-harness <file.html>' "$out" \
          || ! grep -Fq 'runtime_surface=adapter-owned-visual-harness' "$out" \
          || ! grep -Fq 'fallback=satisfy-tool-contract-or-report-unavailable' "$out" \
          || ! grep -Fq "native_mode_path=$native_path" "$out"; then
          fail_msg "Codex mode map must report visual-harness contract metadata for native design mode $rel"
        fi
        ;;
      material/*|qa/test|research/claim-verify)
        if ! grep -Fq 'status=tool-contract' "$out" || ! grep -Fq 'realization=portable-with-tool-contract' "$out"; then
          fail_msg "Codex mode map must mark $rel as portable-with-tool-contract"
        fi
        if ! grep -Eq '^tool_contract=[^[:space:]]+' "$out"; then
          fail_msg "Codex mode map must report a named tool_contract for $rel"
        fi
        if ! grep -Fq 'fallback=satisfy-tool-contract-or-report-unavailable' "$out"; then
          fail_msg "Codex mode map must report a fallback for tool-contract mode $rel"
        fi
        if [ "$rel" = "material/data-script" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/codex/bin/preflight.sh data-script --check <script.py>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-data-script' "$out"; then
            fail_msg "Codex mode map must report data-script contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "material/browser-fetch" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/codex/bin/preflight.sh browser-fetch --check <url>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-browser-fetch' "$out"; then
            fail_msg "Codex mode map must report browser-fetch contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "material/figure-gen" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/codex/bin/preflight.sh figure-gen --check <script.py>' "$out" \
            || ! grep -Fq 'report_tool_contract_check=adapters/codex/bin/preflight.sh figure-gen --verify-report <manifest.json> <report.md>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-figure-gen' "$out"; then
            fail_msg "Codex mode map must report figure-gen contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "material/pdf-extract" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/codex/bin/preflight.sh pdf-extract --check <file.pdf>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-pdf-extract' "$out"; then
            fail_msg "Codex mode map must report pdf-extract contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "material/web-image-search" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/codex/bin/preflight.sh web-image-search --check <query>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-web-image-search' "$out"; then
            fail_msg "Codex mode map must report web-image-search contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "qa/test" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/codex/bin/preflight.sh verification-runner --check -- <command>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-verification-runner' "$out"; then
            fail_msg "Codex mode map must report verification-runner contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "research/claim-verify" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/codex/bin/preflight.sh claim-verify --check <claim>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-claim-verify' "$out"; then
            fail_msg "Codex mode map must report claim-verify contract metadata for $rel"
          fi
        fi
        ;;
      *)
        if ! grep -Fq 'status=portable' "$out" || ! grep -Fq 'realization=portable-persona' "$out"; then
          fail_msg "Codex mode map must mark $rel as portable-persona"
        fi
        if [ "$rel" = "qa/security-review" ]; then
          if grep -Fq 'tool_contract=' "$out" \
            || ! grep -Fq 'read-only security review with Codex file and git diff tools' "$out"; then
            fail_msg "Codex mode map must treat qa/security-review as portable read-only guidance"
          fi
        fi
        ;;
    esac
    native_path="adapters/codex/modes/$rel.md"
    if [ ! -f "$native_path" ]; then
      fail_msg "Codex mode projection missing: $native_path"
    elif [ -L "$native_path" ]; then
      fail_msg "Codex mode projection must be concrete: $native_path"
    elif grep -Eq "$CLAUDE_NATIVE_SURFACE_PATTERN" "$native_path"; then
      fail_msg "Codex mode projection must not reference Claude-native surfaces: $native_path"
    fi
    if ! grep -Fq "native_mode_path=$native_path" "$out"; then
      fail_msg "Codex mode map must report native_mode_path=$native_path"
    fi
    if ! grep -Fq "source=roles/units/$rel.md" "$out"; then
      fail_msg "Codex mode map must report the portable source for $rel"
    fi
  done
}

check_opencode_capability_map() {
  mapper=adapters/opencode/bin/capability-map.sh
  if [ ! -x "$mapper" ]; then
    fail_msg "$mapper is missing or not executable"
    return
  fi

  for d in skills/*; do
    [ -d "$d" ] || continue
    [ -f "$d/SKILL.md" ] || continue
    slug=${d#skills/}
    if ! "$mapper" "$slug" >/dev/null 2>&1; then
      fail_msg "OpenCode capability map cannot resolve skill capability: $slug"
    fi
  done
}

check_opencode_mode_map() {
  mapper=adapters/opencode/bin/mode-map.sh
  if [ ! -x "$mapper" ]; then
    fail_msg "$mapper is missing or not executable"
    return
  fi

  for f in roles/modes/*/*.md; do
    [ -f "$f" ] || continue
    rel=${f#roles/modes/}
    rel=${rel%.md}
    out=${TMPDIR:-/tmp}/opencode-mode-map.$$.out
    err=${TMPDIR:-/tmp}/opencode-mode-map.$$.err
    if ! "$mapper" "$rel" >"$out" 2>"$err"; then
      fail_msg "OpenCode mode map cannot resolve agent mode: $rel"
      cat "$err"
      continue
    fi
    case "$rel" in
      design/*)
        if ! grep -Fq 'status=unsupported' "$out" || ! grep -Fq 'realization=adapter-coupled' "$out"; then
          fail_msg "OpenCode mode map must mark $rel as unsupported adapter-coupled"
        fi
        if ! grep -Fq 'tool_contract=visual-harness' "$out" \
          || ! grep -Fq 'tool_contract_check=adapters/opencode/bin/preflight.sh visual-harness <file.html>' "$out" \
          || ! grep -Fq 'runtime_surface=adapter-owned-visual-harness' "$out" \
          || ! grep -Fq 'fallback=reference-only' "$out"; then
          fail_msg "OpenCode mode map must report visual-harness contract metadata for unsupported design mode $rel"
        fi
        ;;
      material/*|qa/test|research/claim-verify)
        if ! grep -Fq 'status=tool-contract' "$out" || ! grep -Fq 'realization=portable-with-tool-contract' "$out"; then
          fail_msg "OpenCode mode map must mark $rel as portable-with-tool-contract"
        fi
        if ! grep -Eq '^tool_contract=[^[:space:]]+' "$out"; then
          fail_msg "OpenCode mode map must report a named tool_contract for $rel"
        fi
        if ! grep -Fq 'fallback=satisfy-tool-contract-or-report-unavailable' "$out"; then
          fail_msg "OpenCode mode map must report a fallback for tool-contract mode $rel"
        fi
        if [ "$rel" = "material/data-script" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/opencode/bin/preflight.sh data-script --check <script.py>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-data-script' "$out"; then
            fail_msg "OpenCode mode map must report data-script contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "material/browser-fetch" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/opencode/bin/preflight.sh browser-fetch --check <url>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-browser-fetch' "$out"; then
            fail_msg "OpenCode mode map must report browser-fetch contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "material/figure-gen" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/opencode/bin/preflight.sh figure-gen --check <script.py>' "$out" \
            || ! grep -Fq 'report_tool_contract_check=adapters/opencode/bin/preflight.sh figure-gen --verify-report <manifest.json> <report.md>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-figure-gen' "$out"; then
            fail_msg "OpenCode mode map must report figure-gen contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "material/pdf-extract" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/opencode/bin/preflight.sh pdf-extract --check <file.pdf>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-pdf-extract' "$out"; then
            fail_msg "OpenCode mode map must report pdf-extract contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "material/web-image-search" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/opencode/bin/preflight.sh web-image-search --check <query>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-web-image-search' "$out"; then
            fail_msg "OpenCode mode map must report web-image-search contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "qa/test" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/opencode/bin/preflight.sh verification-runner --check -- <command>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-verification-runner' "$out"; then
            fail_msg "OpenCode mode map must report verification-runner contract metadata for $rel"
          fi
        fi
        if [ "$rel" = "research/claim-verify" ]; then
          if ! grep -Fq 'tool_contract_check=adapters/opencode/bin/preflight.sh claim-verify --check <claim>' "$out" \
            || ! grep -Fq 'runtime_surface=adapter-owned-claim-verify' "$out"; then
            fail_msg "OpenCode mode map must report claim-verify contract metadata for $rel"
          fi
        fi
        ;;
      *)
        if ! grep -Fq 'status=portable' "$out" || ! grep -Fq 'realization=portable-persona' "$out"; then
          fail_msg "OpenCode mode map must mark $rel as portable-persona"
        fi
        if [ "$rel" = "qa/security-review" ]; then
          if grep -Fq 'tool_contract=' "$out" \
            || ! grep -Fq 'read-only security review with OpenCode file and git diff tools' "$out"; then
            fail_msg "OpenCode mode map must treat qa/security-review as portable read-only guidance"
          fi
        fi
        ;;
    esac
    if ! grep -Fq "source=roles/modes/$rel.md" "$out"; then
      fail_msg "OpenCode mode map must report the portable source for $rel"
    fi
  done
}

check_hook_catalog() {
  if [ ! -f core/HOOKS.md ]; then
    fail_msg "core/HOOKS.md is missing"
    return
  fi

  for f in hooks/*.sh; do
    [ -f "$f" ] || continue
    case "$f" in
      *.test.sh) continue ;;
    esac
    if ! grep -Fq "\`$f\`" core/HOOKS.md; then
      fail_msg "core/HOOKS.md is missing hook script: $f"
    fi
  done
}

check_legacy_root_links() {
  command -v rg >/dev/null 2>&1 || return 0

  legacy_skill_links=$(rg -n '\]\(\.\./\.\./(CLAUDE|MEMORY|CORE|WORKFLOW|CONVENTIONS|OPERATIONS|DESIGN_PRINCIPLES|VISION)\.md\)' \
    skills adapters README.md MANUAL.md INSTALL_LAYOUT.md 2>/dev/null || true)
  if [ -n "$legacy_skill_links" ]; then
    fail_msg "legacy ../../ tier1 markdown links remain:"
    printf '%s\n' "$legacy_skill_links"
  fi

  legacy_root_links=$(rg -n '\]\((CLAUDE|MEMORY|CORE|WORKFLOW|CONVENTIONS|OPERATIONS|DESIGN_PRINCIPLES|VISION)\.md\)' \
    README.md MANUAL.md INSTALL_LAYOUT.md adapters skills 2>/dev/null || true)
  if [ -n "$legacy_root_links" ]; then
    fail_msg "legacy root tier1 markdown links remain outside core/:"
    printf '%s\n' "$legacy_root_links"
  fi
}

warn_concrete_runtime_terms() {
  command -v rg >/dev/null 2>&1 || return 0

  count=$(rg -n 'sonnet|opus|haiku|claude -p|~/.claude|Claude adapter:' \
    core tools utilities hooks \
    --glob '!tools/check-adaptation-boundary.sh' \
    --glob '!hooks/*.test.sh' 2>/dev/null | wc -l | tr -d ' ')

  if [ "$count" != "0" ]; then
    say "WARN: $count concrete Claude/model references remain in portable areas."
    say "      This is allowed only where documented as adapter mapping, compat-reference, or compat-passthrough."
  fi
}

# HLS-8 requires explicit unsupported/fallback warnings for parity loss.
check_parity_loss_explicit_warnings() {
  # Each row is description|file|required-token; a missing token is a silent drop.
  while IFS='|' read -r _desc _file _token; do
    [ -n "$_desc" ] || continue
    if [ ! -f "$_file" ]; then
      fail_msg "parity-loss warning source $_file for '$_desc' is missing"
      continue
    fi
    if ! grep -Fq "$_token" "$_file"; then
      fail_msg "parity-loss for '$_desc' must be an explicit unsupported/fallback signal in $_file (expected token: $_token) — silent skip forbidden (HLS-8/§6.1)"
    fi
  done <<'PARITY_LOSS_EOF'
hook-execution-isolation(codex)|adapters/codex/bin/preflight.sh|claude_headless=unsupported
loop-auto-run(codex)|adapters/codex/bin/preflight.sh|auto_run=unsupported
allowedTools(codex)|adapters/codex/bin/preflight.sh|claude_allowed_tools=unsupported
settings-mcp(codex)|adapters/codex/bin/preflight.sh|claude_settings_mcp=unsupported
loop-note(codex)|core/ADAPTATION_INVENTORY.md|unsupported/manual-contract
mode-fragment(opencode)|adapters/opencode/ADAPTATION.md|status=unsupported
allowedTools(opencode)|adapters/opencode/ADAPTATION.md|allowedTools` is unsupported
PARITY_LOSS_EOF
}

# HLS-9 guards always-loaded adapter bootstrap budgets and context drift.
check_bootstrap_byte_budget() {
  # Each row is file|byte-limit|rationale.
  while IFS='|' read -r _bf _cap _why; do
    [ -n "$_bf" ] || continue
    if [ ! -f "$_bf" ]; then
      fail_msg "bootstrap byte-budget: $_bf is missing"
      continue
    fi
    _sz=$(wc -c < "$_bf" | tr -d ' ')
    if [ "$_sz" -gt "$_cap" ]; then
      fail_msg "bootstrap byte-budget: $_bf is $_sz bytes, over the $_cap-byte ceiling ($_why). Trim it or raise the ceiling with a documented reason (HLS-9/§6.2)"
    fi
  done <<'BYTE_BUDGET_EOF'
adapters/claude/CLAUDE.md|16384|portable active-context budget (ADAPTATION §6.1)
adapters/codex/AGENTS.md|16384|portable active-context budget (ADAPTATION §6.1)
adapters/opencode/AGENTS.md|16384|portable active-context budget (ADAPTATION §6.1)
BYTE_BUDGET_EOF
}

check_sibling_adapter_contract() {
  if ! grep -Fq 'smallest appropriate layer' core/DESIGN_PRINCIPLES.md \
    || ! grep -Fq 'repeated evidence and an explicit check' core/DESIGN_PRINCIPLES.md \
    || ! grep -Fq 'one owning document' core/DESIGN_PRINCIPLES.md \
    || ! grep -Fq 'Sibling-Adapter Completion' core/ADAPTATION.md \
    || ! grep -Fq 'reference implementation or parent of another adapter' core/ADAPTATION.md \
    || ! grep -Fq 'report `GREEN` only after all' core/ADAPTATION.md \
    || ! grep -Fq 'Active Context Budget' core/ADAPTATION.md \
    || ! grep -Fq 'at most `16,384` UTF-8 bytes' core/ADAPTATION.md \
    || ! grep -Fq 'at least 30' core/ADAPTATION.md; then
    fail_msg "core/ADAPTATION.md must own sibling-adapter completion and active-context budgets"
  fi

  for bootstrap in adapters/claude/CLAUDE.md adapters/codex/AGENTS.md adapters/opencode/AGENTS.md; do
    if ! grep -Fq 'siblings' "$bootstrap" \
      || ! grep -Fq 'core/capabilities/roles' "$bootstrap"; then
      fail_msg "$bootstrap must realize the portable sibling-adapter hierarchy"
    fi
  done

  if [ ! -f tools/context-footprint-baseline.json ] \
    || ! grep -Fq '"schema": 1' tools/context-footprint-baseline.json \
    || ! grep -Fq 'MAX_BASELINE_GROWTH = 1.05' tools/context-footprint.py \
    || ! grep -Fq 'SKILL_METADATA_BUDGET = 7_000' tools/context-footprint.py; then
    fail_msg "context footprint baseline and absolute/regression budgets must be versioned"
  fi
}

check_worker_bootstrap_contract() {
  if [ ! -f roles/worker-bootstrap.md ] \
    || ! grep -Fq 'artifact: <canonical path | ->' roles/worker-bootstrap.md \
    || ! grep -Fq 'verdict: PASS | FAIL | BLOCKED' roles/worker-bootstrap.md \
    || ! grep -Fq 'blocker: none | <one line>' roles/worker-bootstrap.md; then
    fail_msg "roles/worker-bootstrap.md must own the exact portable three-line handoff"
  fi

  for worker_type in owner stage review support; do
    fragment="roles/worker-types/$worker_type.md"
    if [ ! -f "$fragment" ] || ! grep -Fq '# Worker Type:' "$fragment"; then
      fail_msg "missing portable worker-type fragment: $fragment"
    fi
    if ! grep -Fq "worker_type: $worker_type" profiles/*.yaml 2>/dev/null; then
      # Not every type needs a current profile, but each declared profile must be typed;
      # owner/review currently route through generated prompts rather than a profile.
      [ "$worker_type" = owner ] || [ "$worker_type" = review ] || \
        fail_msg "profile declarations must expose their worker type"
    fi
  done

  for profile in profiles/*.yaml; do
    # profiles/dispatch-defaults.yaml is the SD-66 harness-default policy file,
    # not a worker profile; it declares no worker_type by contract.
    [ "$profile" = "profiles/dispatch-defaults.yaml" ] && continue
    if ! grep -Eq '^worker_type: (owner|stage|review|support)$' "$profile"; then
      fail_msg "$profile must declare exactly one valid worker_type"
    fi
  done

  for adapter in codex claude opencode; do
    dispatcher="adapters/$adapter/bin/dispatch-headless.py"
    if ! grep -Fq 'render_worker_bootstrap' "$dispatcher" \
      || ! grep -Fq 'resolve_worker_type' "$dispatcher" \
      || ! grep -Fq -- '--worker-type' "$dispatcher" \
      || ! grep -Fq 'AGENT_DISPATCH_WORKER_TYPE' "$dispatcher"; then
      fail_msg "$dispatcher must render and register one portable worker type"
    fi
    if grep -Fq 'Return a concise report with changed files' "$dispatcher"; then
      fail_msg "$dispatcher must keep verbose worker evidence in artifacts"
    fi
  done

  if grep -Fq 'Read $AGENT_HOME/adapters/codex/AGENTS.md first' adapters/codex/bin/dispatch-headless.py \
    || grep -Fq 'Read the four core documents' profiles/templates/bootstrap-claude.md \
    || grep -Fq 'Read the four core documents' profiles/templates/bootstrap-codex.md; then
    fail_msg "worker bootstraps must not explicitly preload the full main bootstrap"
  fi

  if [ ! -f utilities/worker_bootstrap.test.py ] \
    || [ ! -f utilities/worker_dispatch_prompt.test.py ] \
    || ! grep -Fq 'worker-bootstrap:kernel' tools/context-footprint-baseline.json; then
    fail_msg "worker bootstrap renderer, dispatch prompt, and footprint evidence must be versioned"
  fi
}

check_language_neutrality_contract() {
  # README.md is the canonical English landing page. Its one exact Korean label
  # is the language switch to the companion translation; all other root-doc
  # prose remains English. Hangul in explicit multilingual retrieval fixtures,
  # tokenizer data, and drill/test corpora is functional data and intentionally
  # outside this check.
  english_switch='<p align="center"><strong>English</strong> · <a href="README.ko.md">한국어</a></p>'
  korean_switch='<p align="center"><a href="README.md">English</a> · <strong>한국어</strong></p>'
  if [ ! -f README.ko.md ] \
    || ! grep -Fxq "$english_switch" README.md \
    || ! grep -Fxq "$korean_switch" README.ko.md \
    || ! grep -Fq '유지보수 기준인 [README.md](README.md)의 한국어 번역' README.ko.md; then
    fail_msg "the bilingual README entrypoints must keep reciprocal language switches and mark README.md as canonical"
  fi

  readme_hangul=$(grep -Fvx "$english_switch" README.md 2>/dev/null | rg -n '[가-힣]' 2>/dev/null || true)
  other_root_hangul=$(rg -n '[가-힣]' MANUAL.md INSTALL_LAYOUT.md 2>/dev/null || true)
  root_doc_hangul=$(printf '%s\n%s\n' "$readme_hangul" "$other_root_hangul" | sed '/^$/d')
  if [ -n "$root_doc_hangul" ]; then
    fail_msg "the canonical English README body, MANUAL.md, and INSTALL_LAYOUT.md must not contain Hangul prose:"
    printf '%s\n' "$root_doc_hangul"
  fi

  if ! grep -Fq '**Audience-language first**' roles/response-policy.md \
    || ! grep -Fq "default to the language the user is currently using to communicate" roles/response-policy.md; then
    fail_msg "roles/response-policy.md must define the audience-language-first artifact contract"
  fi
  if ! grep -Fq '**Terminal-safe enumeration**' roles/response-policy.md \
    || ! grep -Fq 'never use circled or otherwise enclosed' roles/response-policy.md; then
    fail_msg "roles/response-policy.md must define the terminal-safe enumeration contract"
  fi
  if rg -n '[\x{2460}-\x{2473}\x{24EA}\x{24F5}-\x{24FE}\x{2776}-\x{2793}\x{3251}-\x{325F}\x{32B1}-\x{32BF}]' \
    roles/response-policy.md skills/autopilot-spec/references/prd-authoring.md >/dev/null 2>&1; then
    fail_msg "terminal-facing response policy and choice templates must not contain enclosed numeral glyphs"
  fi
  # 재홈 2026-07-22: the retired editorial-team router's audience-language contract now
  # lives in the unit catalog's shared editorial voice fragment.
  if ! grep -Fq '## Audience language (highest-priority principle)' roles/units/editorial/_voice.md \
    || ! grep -Fq 'Do not enforce a fixed locale' roles/units/editorial/_voice.md; then
    fail_msg "the editorial voice fragment (roles/units/editorial/_voice.md) must apply the audience-language contract before language-specific style rules"
  fi

  for bootstrap in adapters/claude/CLAUDE.md adapters/codex/AGENTS.md adapters/opencode/AGENTS.md; do
    if grep -Fq 'Answer the user in Korean' "$bootstrap"; then
      fail_msg "$bootstrap must not impose a fixed response locale"
    fi
    if ! grep -Eiq 'Audience-language first' "$bootstrap"; then
      fail_msg "$bootstrap must realize the portable audience-language-first artifact contract"
    fi
  done

  if rg -n 'Write in Korean|Korean (plan|version|본문)|한국어 (설명|보고서|변경·산출 요약|변경 요약|요약)|한국어로' \
    roles/modes adapters/claude/agent-modes >/dev/null 2>&1; then
    fail_msg "portable and Claude mode contracts must not impose Korean as a fixed output language"
  fi

  if rg -n -i 'All user-facing output.*Korean|When explaining something to the user.*Korean|Print the error message in Korean|One-line chat alert.*Korean|Return ONLY.*Korean summary|한국어 요약|사용자-facing 출력은 자연스러운 한국어' \
    skills adapters/claude/skills >/dev/null 2>&1; then
    fail_msg "skill contracts must not impose Korean as a fixed user-facing or summary locale"
  fi

  if rg -n -i 'All user-facing output.*Korean|structured Korean (feedback|output)|결과 한국어 재정리' \
    adapters/claude/agents >/dev/null 2>&1; then
    fail_msg "Claude agent routers must not impose Korean as a fixed user-facing locale"
  fi

  if grep -Fq 'concise Korean report' adapters/claude/bin/dispatch-headless.py \
    || grep -Fq 'concise Korean report' adapters/codex/bin/dispatch-headless.py; then
    fail_msg "headless worker prompts must not impose a fixed report locale"
  fi

  if grep -Fq 'def auto_recall' tools/memory/mem.py \
    || grep -Fq '"--auto"' tools/memory/mem.py \
    || grep -Fq 'run_preflight("recall"' adapters/codex/hooks/userprompt-lifecycle.py \
    || grep -Fq 'collectPreflight("recall"' adapters/opencode/plugins/hearting-guards.js \
    || ! grep -Fq 'mem-recall-inject.sh' adapters/claude/settings.json \
    || ! grep -Fq 'def candidate_context' adapters/codex/hooks/userprompt-lifecycle.py \
    || ! grep -Fq 'collectCandidates([' adapters/opencode/plugins/hearting-guards.js; then
    fail_msg "active runtimes must expose mechanical capsule candidates while leaving semantic adoption to the agent"
  fi

  if ! grep -Fq 'The agent decides contextually what is worth storing, retrieving, promoting, merging, or pruning.' core/MEMORY.md \
    || ! grep -Fq 'Memory follows D-40: the acting agent decides storing, retrieval, promotion, merge, and pruning.' core/DESIGN_PRINCIPLES.md \
    || ! grep -Fq 'tools/memory/recall.sh' adapters/codex/bin/preflight.sh \
    || ! grep -Fq 'tools/memory/recall.sh' adapters/opencode/bin/preflight.sh \
    || ! grep -Fq 'CANDIDATE_MAX_RESULTS = 6' tools/memory/mem.py \
    || ! grep -Fq 'CANDIDATE_MAX_UTF8_BYTES = 2400' tools/memory/mem.py \
    || ! grep -Fq 'records_capsule_fts' tools/memory/mem.py \
    || ! grep -Fq 'recall-opportunity-missing' hooks/material-route-guard.py; then
    fail_msg "memory semantics must remain agent-owned while capsule candidates and same-turn opportunity are enforced mechanically"
  fi

  promotion_body="$(sed -n '/^def promote_candidates()/,/^# ---------- projection ----------/p' tools/memory/mem.py)"
  if printf '%s\n' "$promotion_body" | grep -Fq "type IN" \
    || ! printf '%s\n' "$promotion_body" | grep -Fq 'and strength are metadata, not semantic gates'; then
    fail_msg "institutionalization review must expose durable evidence without a fixed semantic type gate"
  fi

  for distiller in hooks/mem-distill-dispatch.sh \
    adapters/claude/hooks/mem-distill-dispatch.sh \
    adapters/codex/bin/distill-worker.sh \
    adapters/opencode/bin/distill-worker.sh; do
    if ! grep -Fq 'decision|user-correction|unresolved-obligation|artifact-pointer' "$distiller" \
      || ! grep -Fq 'artifact_refs' "$distiller"; then
      fail_msg "$distiller must enforce purpose-limited, pointer-first automatic memory ingress"
    fi
  done
}

check_fleet_depth2_liveness_regression() {
  # Full drill runs this script as its mandatory deterministic conformance
  # pre-stage. Keep the 2026-07-20 live incident fixed at all three boundaries:
  # the shared classifier, default Fleet projection, and Codex liveness CLI.
  out="${TMPDIR:-/tmp}/fleet-depth2-liveness-regression-$$.log"
  if ! PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/tools" python3 -m unittest \
    fleet.tests.test_dispatch.CodexAttemptIdentityTest.test_namespace_local_numeric_pid_collision_is_not_process_authority \
    fleet.tests.test_f15_rows.FoldingTest.test_portable_persona_child_is_visible_with_exec_hue_without_owner_track \
    >"$out" 2>&1; then
    fail_msg "Fleet dispatch-depth-2 classifier/default-view conformance regression"
    sed -n '1,160p' "$out"
  fi
  if ! PYTHONDONTWRITEBYTECODE=1 python3 utilities/dispatch_registry.test.py \
    RegistryTest.test_codex_liveness_rejects_visible_namespace_pid_without_proof \
    >"$out" 2>&1; then
    fail_msg "Codex liveness route-less namespace-local conformance regression"
    sed -n '1,160p' "$out"
  fi
  rm -f "$out"
}

check_projection_symlinks claude_setting
check_projection_symlinks codex_setting
check_projection_symlinks opencode_setting
check_projection_entry_allowlist claude_setting CLAUDE.md README.md agent-modes agents bin commands core hooks keybindings.json loops manifest.json scaffolds settings.json skills statusline.sh tools utilities
check_projection_entry_allowlist codex_setting AGENTS.md README.md core capabilities roles bin tools utilities scaffolds codex-skills codex-modes codex-plugin-marketplace codex-hooks codex-config codex-agents
check_projection_entry_allowlist opencode_setting AGENTS.md README.md core capabilities roles bin tools utilities opencode-skills opencode-agents opencode-commands opencode-plugins
check_codex_forbidden_entries
check_codex_native_surface_debt
check_required_projection_entries
check_codex_projection_targets
check_opencode_forbidden_entries
check_opencode_required_projection_entries
check_opencode_projection_targets
check_non_claude_projection_runtime_caches
check_claude_projection_targets
check_claude_adapter_concrete_surfaces
check_non_claude_adapter_symlink_boundaries
check_install_layout_codex_projection
check_install_layout_opencode_projection
check_codex_bin_wrappers
check_opencode_bin_wrappers
check_codex_tool_projection
check_codex_scaffold_projection
check_codex_native_skill_projection
check_codex_native_agent_projection
check_codex_model_pin_role_map_consistency
check_codex_native_mode_projection
check_codex_native_hook_projection
check_portable_agent_home_resolution
check_opencode_tool_projection
check_opencode_native_skill_projection
check_opencode_native_agent_projection
check_opencode_native_command_projection
check_opencode_native_plugin_projection
check_codex_utility_projection
check_opencode_utility_projection
check_claude_bin_wrappers
check_claude_skill_projection
check_claude_mode_projection
check_claude_hook_projection
check_claude_utility_projection
check_claude_boundary_guard_projection
check_claude_portable_guards_projection
check_claude_drill_runner_projection
check_claude_scaffold_projection
check_claude_loop_projection
check_claude_tool_projection
check_claude_shared_layer_census
check_removed_root_surfaces
check_role_catalog
check_claude_native_agent_projection
check_adaptation_inventory_native_surfaces
check_projection_summary_docs
check_capability_catalog
check_codex_capability_map
check_opencode_capability_map
check_codex_mode_map
check_opencode_mode_map
check_hook_catalog
check_parity_loss_explicit_warnings
check_bootstrap_byte_budget
check_sibling_adapter_contract
check_worker_bootstrap_contract
check_language_neutrality_contract
check_fleet_depth2_liveness_regression
check_legacy_root_links
warn_concrete_runtime_terms

if [ "$fail" -eq 0 ]; then
  say "OK: adaptation boundary checks passed"
fi

exit "$fail"
