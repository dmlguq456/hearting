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

# A hosted CI runner has no vendor Codex command. Seed one outside the
# harness-owned ingress so activation exercises the real binding/protection
# path without borrowing a developer machine's installation.
VENDOR_BIN="$TMP/vendor/bin"
mkdir -p "$VENDOR_BIN"
printf '%s\n' '#!/usr/bin/env sh' 'printf "%s\n" "codex-cli 0.151.0"' > "$VENDOR_BIN/codex"
chmod 755 "$VENDOR_BIN/codex"
PATH="$VENDOR_BIN${PATH:+:$PATH}"
export PATH

harness() {
  sh "$ROOT/tools/install/harness.sh" "$@"
}

fail() {
  echo "not ok - $*" >&2
  exit 1
}

count_dirs() {
  find "$1" -mindepth 1 -maxdepth 1 -type l 2>/dev/null | wc -l | tr -d ' '
}

count_links() {
  find "$1" -type l 2>/dev/null | wc -l | tr -d ' '
}

# Expected counts are derived from harness-manifest.json, never hardcoded:
# capability count, kernel-agent count, and non-internal mode count.
EXPECT=$(python3 - "$ROOT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
manifest = json.load(open(root / "harness-manifest.json"))
capabilities = len(manifest["capabilities"])
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
  harness runtime activate --runtime all --mode "$mode" --source "$ROOT" --json > "$TMP/$mode-activate.json"
  python3 - "$TMP/$mode-activate.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for row in data["runtimes"]:
    assert "profile" not in row, row
    assert row["freshness"] == "fresh", row
PY

  # Strict doctor now verifies the protected Codex ingress is the command a
  # fresh shell would resolve. Keep this private-HOME fixture hermetic instead
  # of inheriting a host vendor command that happens to appear earlier on PATH.
  PATH="$CODEX_HOME/.harness/bin${PATH:+:$PATH}"
  export PATH
  hash -r 2>/dev/null || true
  resolved_codex=$(command -v codex || true)
  test "$resolved_codex" = "$CODEX_HOME/.harness/bin/codex" \
    || fail "$mode protected Codex ingress is not first on PATH: expected=$CODEX_HOME/.harness/bin/codex actual=${resolved_codex:-missing}"

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
  test -L "$HOME/.claude/hooks/artifact-guard.sh" || fail "$mode lost Claude kernel hooks"
  test -L "$HOME/.claude/statusline.sh" || fail "$mode lost Claude statusline"
  test -x "$HOME/.claude/statusline.sh" || fail "$mode Claude statusline is not executable"
  test -L "$HOME/.config/opencode/plugins/hearting-guards.js" || fail "$mode lost OpenCode kernel guard plugin"

  python3 - "$HOME/.claude/settings.json" <<'PY'
import json, sys
settings = json.load(open(sys.argv[1]))
assert settings["statusLine"]["command"] == "bash $HOME/.claude/statusline.sh", settings
assert settings["env"]["MEM_DISTILL_ENABLE"] == "1", settings
PY

  harness runtime status --runtime all --json > "$TMP/$mode-status.json"
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

assert_projection packaged
sha256sum -c "$TMP/user-model-config.sha256" >/dev/null \
  || fail "packaged reactivation rewrote a user model config"

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
    "memory-sync-config.policy",
    "bootstrap.launcher.compute-hosts",
}, row
PY

# Duplicate remediation next_action names the source, not a retired flag.
python3 - "$ROOT" <<'PY'
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

harness runtime doctor --runtime codex --strict --json > "$TMP/legacy-doctor.json"
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
