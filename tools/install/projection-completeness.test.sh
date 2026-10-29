#!/usr/bin/env sh
# Runtime projection completeness E2E against the real generated source.
#
# Product profiles were removed: activation always projects the full
# manifest-derived capability set. This test asserts that invariant across
# linked and packaged modes and all three runtimes, exercises the
# retirement-link cleanup and user-owned-file preservation regression guard,
# and proves read compatibility for pre-removal activation/distribution
# state that still carries the retired `profile*` fields.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
TMP=$(mktemp -d)
# destructive-ok: reason=clean one mktemp projection fixture; boundary=TMP returned by the immediately preceding mktemp call
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

eval "$(python3 "$ROOT/tools/install/fixture_env.py" shell "$TMP" "$ROOT")"
export OPENCODE_RUNTIME_VERSION=2.0.32
export OPENCODE_BIN="$TMP/no-opencode-probe"

# A hosted CI runner has no vendor Codex command. Seed one outside the
# harness-owned ingress so activation exercises the real binding/protection
# path without borrowing a developer machine's installation.
VENDOR_BIN="$TMP/vendor/bin"
mkdir -p "$VENDOR_BIN"
printf '%s\n' '#!/usr/bin/env sh' 'printf "%s\n" "codex-cli 0.151.0"' > "$VENDOR_BIN/codex"
chmod 755 "$VENDOR_BIN/codex"
printf '%s\n' '#!/usr/bin/env sh' '[ "$1" = --version ] || exit 70' 'printf "%s\n" "2.0.32"' > "$VENDOR_BIN/opencode"
chmod 755 "$VENDOR_BIN/opencode"
PATH="$VENDOR_BIN${PATH:+:$PATH}"
export PATH

harness() {
  sh "$ROOT/tools/install/harness.sh" "$@"
}

fail() {
  echo "not ok - $*" >&2
  exit 1
}

# Packaged activation requires an immutable clean source. Build a private tree
# from HEAD and overlay the assigned changes so each runtime sees one source
# snapshot without relaxing that source guard.
PACKAGED_SOURCE="$TMP/packaged-source"
mkdir -p "$PACKAGED_SOURCE"
git -C "$ROOT" archive --format=tar HEAD > "$TMP/packaged-source.tar"
tar -xf "$TMP/packaged-source.tar" -C "$PACKAGED_SOURCE"
for changed_file in \
  tools/install/runtime_activation.py \
  tools/install/projector.py \
  tools/install/drivers/opencode.py \
  tools/install/opencode_driver.test.py \
  tools/install/runtime-activation.test.sh \
  adapters/opencode/bin/preflight.sh \
  core/ADAPTATION.md \
  adapters/claude/plugin-marketplace/plugins/hearting-claude/core/ADAPTATION.md \
  adapters/codex/plugins/hearting-codex/core/ADAPTATION.md \
  tools/install/runtime_activation.test.py \
  tools/install/projection-completeness.test.sh \
  tools/install/release-lifecycle.test.sh \
  tools/fleet/tests/test_f87_runtime_projection.py; do
  mkdir -p "$PACKAGED_SOURCE/$(dirname -- "$changed_file")"
  cp "$ROOT/$changed_file" "$PACKAGED_SOURCE/$changed_file"
done

count_dirs() {
  find "$1" -mindepth 1 -maxdepth 1 -type l 2>/dev/null | wc -l | tr -d ' '
}

count_links() {
  find "$1" -type l 2>/dev/null | wc -l | tr -d ' '
}

# Expected counts are derived from harness-manifest.json, never hardcoded:
# projected capability count, kernel-agent count, and non-internal mode count.
EXPECT=$(python3 - "$ROOT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
manifest = json.load(open(root / "harness-manifest.json"))
# A compiler-internal capability is documented but never projected as a Skill or command.
capabilities = len([s for s in manifest["capabilities"].values()
                    if s["invocation"]["class"] != "compiler-internal"])
modes = len([m for m in manifest["modes"] if not m.split("/")[-1].startswith("_")])


def catalog(runtime):
    """Native subagent type names from one adapter's model config."""
    names = set()
    config = root / "adapters" / runtime / "config" / "models.conf"
    for line in config.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("CFG_NATIVE_AGENT_CATALOG="):
            continue
        raw = line.split("=", 1)[1].strip().strip('"').strip("'")
        names.update(token.split(":", 1)[0] for token in raw.split() if ":" in token)
    return names


# Every activation projects the kernel helpers plus the adapter's native
# subagent type catalog. The single expected count below is only meaningful
# while the three adapters declare the same catalog, so assert that first.
catalogs = {runtime: catalog(runtime) for runtime in ("claude", "codex", "opencode")}
if len(set(map(frozenset, catalogs.values()))) != 1:
    raise SystemExit(f"native agent catalogs diverge across adapters: {catalogs}")
agents = len(set(manifest["kernel"]["agents"]) | catalogs["claude"])
print(f"{capabilities} {agents} {modes}")
PY
)
EXPECTED_CAPABILITIES=$(echo "$EXPECT" | cut -d' ' -f1)
EXPECTED_AGENTS=$(echo "$EXPECT" | cut -d' ' -f2)
EXPECTED_MODES=$(echo "$EXPECT" | cut -d' ' -f3)

# assert_projection <linked|packaged> — both modes share _desired_entries, so
# every runtime must expose the identical manifest-derived counts in either.
assert_projection() {
  mode=$1
  mode_source=$ROOT
  if [ "$mode" = packaged ]; then
    mode_source=$PACKAGED_SOURCE
  fi
  harness runtime activate --runtime all --mode "$mode" --source "$mode_source" --json > "$TMP/$mode-activate.json" || { cat "$TMP/$mode-activate.json" >&2; fail "$mode activation failed"; }
  python3 - "$TMP/$mode-activate.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for row in data["runtimes"]:
    assert "profile" not in row, row
    assert row["freshness"] == "fresh", row
PY

  # The canonical Herdr scripts/plugins are managed regular-file copies.
  # Check the actual source selected by linked and packaged activation for all
  # runtimes, including bytes, mode, and destination kind.
  python3 - "$TMP/$mode-activate.json" "$HOME" "$CODEX_HOME" <<'PY'
import json, stat, sys
from pathlib import Path

data = json.load(open(sys.argv[1]))
home, codex_home = Path(sys.argv[2]), Path(sys.argv[3])
for row in data["runtimes"]:
    active = Path(row["active_root"])
    assets = {
        "claude": [
            (active / "hooks/herdr-agent-state.sh",
             home / ".claude/hooks/herdr-agent-state.sh"),
        ],
        "codex": [
            (active / "adapters/codex/hooks/herdr-agent-state.sh",
             codex_home / "herdr-agent-state.sh"),
        ],
        "opencode": [
            (active / "adapters/opencode/plugins/herdr-agent-state.js",
             home / ".config/opencode/plugins/herdr-agent-state.js"),
            (active / "adapters/opencode/tui/herdr-tui-session.js",
             home / ".config/opencode/herdr-tui-session.js"),
            (active / "adapters/opencode/tui/herdr-opencode/tui.js",
             home / ".config/opencode/herdr-opencode/tui.js"),
        ],
    }[row["runtime"]]
    for source, dest in assets:
        assert source.is_file(), (row["runtime"], source)
        assert dest.is_file() and not dest.is_symlink(), (row["runtime"], dest)
        assert dest.read_bytes() == source.read_bytes(), (row["runtime"], dest)
        assert stat.S_IMODE(dest.stat().st_mode) == (source.stat().st_mode & 0o777), (
            row["runtime"], dest,
        )
PY

  # Activation leaves the native vendor CLI on PATH without a launcher.
  hash -r 2>/dev/null || true
  resolved_codex=$(command -v codex || true)
  test "$resolved_codex" = "$VENDOR_BIN/codex" \
    || fail "$mode native Codex command changed: ${resolved_codex:-missing}"
  test ! -e "$CODEX_HOME/.harness/bin/codex" || fail "$mode recreated retired launcher"
  CODEX_RUNTIME_PROJECTION_SKIP_CLI_DISCOVERY=1 AGENT_HOME="$ROOT" \
    "$ROOT/adapters/codex/bin/check-runtime-projection.sh" > "$TMP/$mode-native-projection.log" \
    || { cat "$TMP/$mode-native-projection.log" >&2; fail "$mode native projection failed"; }
  grep -q 'check=managed-launcher:retired required=0' "$TMP/$mode-native-projection.log" \
    || fail "$mode still requires a managed launcher"

  test "$(count_dirs "$HOME/.codex/skills")" = "$EXPECTED_CAPABILITIES" || fail "$mode Codex skill count"
  test "$(count_dirs "$HOME/.claude/skills")" = "$EXPECTED_CAPABILITIES" || fail "$mode Claude skill count"
  test "$(count_dirs "$HOME/.config/opencode/skills")" = "$EXPECTED_CAPABILITIES" || fail "$mode OpenCode skill count"
  test "$(count_dirs "$HOME/.config/opencode/commands")" = "$EXPECTED_CAPABILITIES" || fail "$mode OpenCode command count"

  # Kernel helpers (memory-scout) plus the native subagent type catalog;
  # runtime team agents retired 2026-07-22 (재홈) and stay unprojected.
  test "$(count_dirs "$HOME/.codex/agents")" = "$EXPECTED_AGENTS" || fail "$mode Codex agent count"
  test "$(count_dirs "$HOME/.claude/agents")" = "$EXPECTED_AGENTS" || fail "$mode Claude agent count"
  test "$(count_dirs "$HOME/.config/opencode/agents")" = "$EXPECTED_AGENTS" || fail "$mode OpenCode agent count"

  test "$(count_links "$HOME/.codex/agent-modes")" = "$EXPECTED_MODES" || fail "$mode Codex mode count"
  test -L "$HOME/.codex/agent-modes/plan/frame.md" || fail "$mode omitted plan/frame Codex mode"
  # Claude agent-modes runtime surface retired: units project through home/roles/units.
  test ! -e "$HOME/.claude/agent-modes" || fail "$mode Claude agent-modes surface should be retired"

  test -L "$HOME/.codex/hooks.json" || fail "$mode lost Codex kernel hooks"
  test -L "$HOME/.claude/hooks/spec-read-marker.sh" || fail "$mode lost Claude kernel hooks"
  test -L "$HOME/.claude/statusline.sh" || fail "$mode lost Claude statusline"
  test -x "$HOME/.claude/statusline.sh" || fail "$mode Claude statusline is not executable"
  test ! -e "$HOME/.config/opencode/plugins/hearting-guards.js" || fail "$mode duplicated OpenCode guard discovery"
  { test -f "$HOME/.config/opencode/plugins/hearting-guards/index.ts" \
    && test ! -L "$HOME/.config/opencode/plugins/hearting-guards/index.ts"; } \
    || fail "$mode lost OpenCode V2 guard package entrypoint (must be a regular file inside the package)"
  { test -f "$HOME/.config/opencode/plugins/hearting-tui-identity/tui.ts" \
    && test ! -L "$HOME/.config/opencode/plugins/hearting-tui-identity/tui.ts"; } \
    || fail "$mode lost OpenCode V2 TUI identity entrypoint (must be a regular file inside the package)"

  python3 - "$HOME/.claude/settings.json" <<'PY'
import json, sys
settings = json.load(open(sys.argv[1]))
assert settings["statusLine"]["command"] == "bash $HOME/.claude/statusline.sh", settings
assert settings["autoMemoryEnabled"] is False, settings
PY

  harness runtime status --runtime all --json > "$TMP/$mode-status.json"
  python3 - "$TMP/$mode-status.json" "$HOME/.config/opencode/plugins/hearting-guards/index.ts" <<'PY'
import json, pathlib, sys
row = next(r for r in json.load(open(sys.argv[1]))["runtimes"] if r["runtime"] == "opencode")
module = pathlib.Path(row["active_root"]) / "adapters/opencode/plugins/hearting-guards.js"
assert module.is_file(), module
assert f'from {json.dumps(str(module))}' in pathlib.Path(sys.argv[2]).read_text()
PY
  AGENT_HOME="$ROOT" "$ROOT/adapters/opencode/bin/preflight.sh" headless --check "$ROOT" \
    > "$TMP/$mode-opencode-preflight.log" \
    || { cat "$TMP/$mode-opencode-preflight.log" >&2; fail "$mode OpenCode headless preflight"; }
  guard_package="$HOME/.config/opencode/plugins/hearting-guards/index.ts"
  # destructive-ok: reason=simulate a missing selected package; boundary=exact rendered package in this fixture's private runtime home
  mv "$guard_package" "$guard_package.fixture-missing"
  if AGENT_HOME="$ROOT" "$ROOT/adapters/opencode/bin/preflight.sh" headless --check "$ROOT" \
    > "$TMP/$mode-opencode-missing-package.log" 2>&1; then
    fail "$mode OpenCode preflight accepted a missing selected carrier"
  fi
  # destructive-ok: reason=restore the simulated missing package; boundary=exact rendered package in this fixture's private runtime home
  mv "$guard_package.fixture-missing" "$guard_package"
  if [ "$mode" = packaged ]; then
    guard_module=$(python3 - "$TMP/$mode-status.json" <<'PY'
import json, pathlib, sys
row = next(r for r in json.load(open(sys.argv[1]))["runtimes"] if r["runtime"] == "opencode")
print(pathlib.Path(row["active_root"]) / "adapters/opencode/plugins/hearting-guards.js")
PY
)
    # destructive-ok: reason=simulate a missing bundle dependency; boundary=exact guard module in the fixture's private copied packaged source
    mv "$guard_module" "$guard_module.fixture-missing"
    if AGENT_HOME="$ROOT" "$ROOT/adapters/opencode/bin/preflight.sh" headless --check "$ROOT" \
      > "$TMP/$mode-opencode-missing-module.log" 2>&1; then
      fail "$mode OpenCode preflight accepted a missing bundle module"
    fi
    # destructive-ok: reason=restore the simulated missing dependency; boundary=exact guard module in the fixture's private copied packaged source
    mv "$guard_module.fixture-missing" "$guard_module"
  fi
  python3 - "$TMP/$mode-status.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for row in data["runtimes"]:
    assert row["freshness"] == "fresh", row
    assert row["model_config_present"] is True, row
    assert row["model_config_source"] == "user", row
    assert row["model_config_reason"] == "user-valid", row
    assert "profile" not in row, row
PY

  harness runtime doctor --runtime all --strict --json > "$TMP/$mode-doctor.json"
  python3 - "$TMP/$mode-doctor.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data["exit"] == 0, data
PY
}

assert_projection linked

# Every adapter gets the same user-owned, copy-once model surface. Reapplying a
# different activation mode must preserve those bytes instead of refreshing
# them from the release defaults.
for config in \
  "$HOME/.claude/agent-config/models.conf" \
  "$HOME/.codex/agent-config/models.conf" \
  "$HOME/.config/opencode/agent-config/models.conf"; do
  printf '%s\n' '# user customization survives activation updates' >> "$config"
done
sha256sum \
  "$HOME/.claude/agent-config/models.conf" \
  "$HOME/.codex/agent-config/models.conf" \
  "$HOME/.config/opencode/agent-config/models.conf" \
  > "$TMP/user-model-config.sha256"

stat -c '%n %y' "$HOME/.claude/agent-config/models.conf" "$HOME/.codex/agent-config/models.conf" "$HOME/.config/opencode/agent-config/models.conf" > "$TMP/user-model-config.mtime"
assert_projection packaged
stat -c '%n %y' "$HOME/.claude/agent-config/models.conf" "$HOME/.codex/agent-config/models.conf" "$HOME/.config/opencode/agent-config/models.conf" > "$TMP/user-model-config.after-mtime"
cmp "$TMP/user-model-config.mtime" "$TMP/user-model-config.after-mtime" || fail "packaged reapply changed model config mtime"
sha256sum -c "$TMP/user-model-config.sha256" >/dev/null \
  || fail "packaged reactivation rewrote a user model config"
# Checkout update/reapply may refresh tracked copy-once surfaces, but the
# runtime's user model policy stays user-owned, including its modification time.
harness update --reapply --json > "$TMP/reapply.json" || { cat "$TMP/reapply.json" >&2; fail "checkout reapply failed"; }
sha256sum -c "$TMP/user-model-config.sha256" >/dev/null || fail "reapply rewrote user model config"
stat -c '%n %y' "$HOME/.claude/agent-config/models.conf" "$HOME/.codex/agent-config/models.conf" "$HOME/.config/opencode/agent-config/models.conf" > "$TMP/user-model-config.reapply-mtime"
cmp "$TMP/user-model-config.mtime" "$TMP/user-model-config.reapply-mtime" || fail "reapply changed model config mtime"


# User-facing verify follows activation state instead of legacy projection checks.
harness verify --json > "$TMP/verify.json"
python3 - "$TMP/verify.json" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))
assert row["exit"] == 0, row
assert all(item["ok"] for item in row["checks"]), row
assert {item["id"] for item in row["checks"]} == {
    "claude.runtime-activation",
    "codex.runtime-activation",
    "opencode.runtime-activation",
    "routing-config.user-policy",
    "report-bundle-config.root",
    "compute-hosts-config.inventory",
    "fleet-config.language",
    "memory-sync-config.policy",
    "host.pyyaml",
    "bootstrap.launcher.compute-hosts",
}, row
PY

# Duplicate remediation next_action names the source, not a retired flag.
python3 - "$mode_source" <<'PY'
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "tools", "install"))
import runtime_activation as activation
activation.duplicate_sources = lambda runtime, scope="global": ["native+plugin"]
row = activation.status("codex")
assert "--profile" not in row["next_action"], row
assert f"--source {sys.argv[1]}" in row["next_action"], row
PY

# A reactivation reports and removes an untracked native link left by an older
# all-capabilities installer, while preserving unrelated user entries. The
# duplicate-source prefix is `unmanaged-extra:` (renamed from `profile-extra:`).
ln -s "$ROOT/adapters/codex/agents/external-adversary.toml" \
  "$HOME/.codex/agents/legacy-external.toml"
printf '%s\n' 'user-owned' > "$HOME/.codex/agents/user-owned.toml"
harness runtime status --runtime codex --json > "$TMP/dup-extra.json" || true
python3 - "$TMP/dup-extra.json" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))
assert row["freshness"] == "duplicate", row
assert "unmanaged-extra:agents/legacy-external.toml" in row["duplicate_sources"], row
PY

harness runtime activate --runtime codex --mode linked --source "$ROOT" --json > "$TMP/reactivate.json"
python3 - "$TMP/reactivate.json" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))
assert "profile" not in row, row
assert row["freshness"] == "fresh", row
PY
test ! -e "$HOME/.codex/agents/legacy-external.toml" \
  || fail "reactivation retained a legacy harness agent"
test -f "$HOME/.codex/agents/user-owned.toml" \
  || fail "reactivation removed a user-owned agent"

# [C6.6] Legacy activation state read-compat: an activation.json still
# carrying the six retired `profile*` fields must not break status, strict
# doctor, or refresh, and refresh must still project the full capability set.
LEGACY_ACTIVATION="$HOME/.codex/.harness/activation.json"
seed_legacy_activation_fields() {
  python3 - "$LEGACY_ACTIVATION" <<'PY'
import json, sys
path = sys.argv[1]
state = json.load(open(path))
state.update({
    "profile": "builder",
    "profile_digest": "legacy-digest",
    "profile_counts": {"capabilities": 13, "roles": 7, "modes": 19},
    "profile_capabilities": ["autopilot-code"],
    "profile_roles": ["dev/backend"],
    "profile_modes": ["dev/backend"],
})
json.dump(state, open(path, "w"))
PY
}
seed_legacy_activation_fields

harness runtime status --runtime codex --json > "$TMP/legacy-status.json"
python3 - "$TMP/legacy-status.json" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))
assert row["freshness"] != "missing", row
assert "profile" not in row, row
PY

harness runtime doctor --runtime codex --strict --json > "$TMP/legacy-doctor.json" \
  || { cat "$TMP/legacy-doctor.json" >&2; exit 1; }
python3 - "$TMP/legacy-doctor.json" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))
assert row["exit"] == 0, row
PY

harness runtime refresh --runtime codex --json > "$TMP/legacy-refresh.json"
python3 - "$TMP/legacy-refresh.json" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))
assert "profile" not in row, row
assert row["freshness"] == "fresh", row
PY
test "$(count_dirs "$HOME/.codex/skills")" = "$EXPECTED_CAPABILITIES" \
  || fail "legacy activation refresh did not restore the full capability projection"

# [C6.11] The same six-key fixture must also survive the activation *update*
# path (a full reactivation), not just the read-only status/doctor/refresh
# surfaces above — and the rewritten state must not resurrect the retired
# keys. C6.6 covers status/doctor/refresh; C6.9 (release-lifecycle.test.sh)
# covers distribution state; this closes the activation-state x update gap
# plan-check round_1 flagged.
seed_legacy_activation_fields
harness runtime activate --runtime codex --mode linked --source "$ROOT" --json > "$TMP/legacy-update.json"
python3 - "$TMP/legacy-update.json" "$LEGACY_ACTIVATION" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))
assert "profile" not in row, row
assert row["freshness"] == "fresh", row
state = json.load(open(sys.argv[2]))
legacy_keys = {
    "profile", "profile_digest", "profile_counts",
    "profile_capabilities", "profile_roles", "profile_modes",
}
assert not (legacy_keys & set(state)), state
assert isinstance(state.get("activated_projection_digest"), str) and state["activated_projection_digest"], state
PY
test "$(count_dirs "$HOME/.codex/skills")" = "$EXPECTED_CAPABILITIES" \
  || fail "activation update did not restore the full capability projection"

# Activation never accepts the legacy project scope. `install` no longer
# routes through runtime activation once `--profile` is gone (linked
# activation is now reached only by the explicit `runtime activate` channel),
# so the rejection is exercised directly against that channel instead of the
# plan's originally cited `harness install ... --scope project --dry-run`
# (verified empirically: that command now succeeds via the legacy per-runtime
# driver path, which never validated scope). `runtime activate` has no
# --dry-run flag, so this asserts the real (non-dry-run) rejection.
if harness runtime activate --runtime codex --mode linked --source "$ROOT" --scope project --json \
  > "$TMP/project-scope.json"; then
  fail "runtime activate accepted the unsupported project scope"
else
  test "$?" = "3" || fail "runtime activate returned the wrong blocked exit"
fi
python3 - "$TMP/project-scope.json" <<'PY'
import json, sys
row = json.load(open(sys.argv[1]))
assert row["exit"] == 3, row
assert "outside Phase 1" in row["error"], row
PY

# The OpenCode leg closes the generic deactivation path not covered by the
# Codex/Claude uninstall assertions in runtime-activation.test.sh.
harness uninstall opencode >/dev/null
sha256sum -c "$TMP/user-model-config.sha256" 2>/dev/null \
  | grep -F "$HOME/.config/opencode/agent-config/models.conf: OK" >/dev/null \
  || fail "OpenCode uninstall removed or rewrote the user model config"

echo "projection-completeness: PASS"
