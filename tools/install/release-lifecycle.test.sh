#!/bin/sh
# Managed release lifecycle/security checks under one isolated HOME.
set -eu
export PYTHONDONTWRITEBYTECODE=1

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

HOME="$TMP/home"
XDG_CONFIG_HOME="$HOME/.config"
XDG_DATA_HOME="$HOME/.local/share"
XDG_STATE_HOME="$HOME/.local/state"
HARNESS_BIN_DIR="$HOME/.local/bin"
CODEX_HOME="$HOME/.codex"
CLAUDE_CONFIG_DIR="$HOME/.claude"
HARNESS_ALLOW_FILE_RELEASES=1
HARNESS_SCHEDULER_NO_ACTIVATE=1
HARNESS_TEST_PLATFORM=linux
AGENT_HOME="$ROOT"
export HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME HARNESS_BIN_DIR CODEX_HOME CLAUDE_CONFIG_DIR
export HARNESS_ALLOW_FILE_RELEASES HARNESS_SCHEDULER_NO_ACTIVATE
export HARNESS_TEST_PLATFORM AGENT_HOME ROOT TMP
mkdir -p "$HOME"

python3 - "$ROOT" "$TMP" <<'PY'
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
sys.path.insert(0, str(root / "tools/install"))
import distribution as d
import installer
os.environ["CODEX_HOME"] = str(Path(os.environ["HOME"]) / ".codex")

# GitHub Actions smoke installs authenticate only the API metadata lookup. The
# token must never be attached to arbitrary release-index overrides or asset
# hosts, and malformed credentials fail before urllib sees them.
os.environ["GH_TOKEN"] = "fixture-token"
assert d._request_headers("https://api.github.com/repos/acme/hearting/releases/latest")[
    "Authorization"
] == "Bearer fixture-token"
assert "Authorization" not in d._request_headers(
    "https://github.com/acme/hearting/releases/download/v1/hearting.tar.gz"
)
assert "Authorization" not in d._request_headers("https://example.test/release.json")
os.environ["GH_TOKEN"] = "bad\nvalue"
try:
    d._request_headers("https://api.github.com/repos/acme/hearting/releases/latest")
except d.DistributionError as exc:
    assert "invalid whitespace" in str(exc)
else:
    raise AssertionError("malformed GitHub token was accepted")
os.environ.pop("GH_TOKEN")

# Status probes are read-only and must never depend on the host user bus.
real_probe_command = d._probe_command
probe_calls = []
def unavailable_probe(command):
    probe_calls.append(tuple(command))
    return False, "probe unavailable (lifecycle fixture)"
d._probe_command = unavailable_probe

assets = tmp / "assets"
assets.mkdir()
index = tmp / "release.json"
os.environ["HARNESS_RELEASE_INDEX_URL"] = index.as_uri()

required = {
    "hearting/harness-manifest.json": "{}\n",
    "hearting/core/CORE.md": "# core\n",
    "hearting/tools/install/harness.sh": "#!/bin/sh\n",
    "hearting/tools/install/installer.py": "# fixture\n",
    "hearting/tools/install/distribution.py": "# fixture\n",
}

def make_release(version, attack=None, wrong_checksum=False):
    archive = assets / f"{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        files = dict(required)
        files["hearting/RELEASE_VERSION"] = version + "\n"
        for name, text in files.items():
            payload = text.encode()
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
        if attack == "traversal":
            payload = b"escape"
            info = tarfile.TarInfo("../escape")
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
        elif attack == "symlink":
            info = tarfile.TarInfo("hearting/escape-link")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../escape"
            bundle.addfile(info)
        elif attack == "hardlink":
            info = tarfile.TarInfo("hearting/escape-hardlink")
            info.type = tarfile.LNKTYPE
            info.linkname = "../../escape"
            bundle.addfile(info)
        elif attack == "fifo":
            info = tarfile.TarInfo("hearting/fifo")
            info.type = tarfile.FIFOTYPE
            bundle.addfile(info)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = assets / f"{version}.sha256"
    checksum.write_text(
        (("0" * 64) if wrong_checksum else digest)
        + "  hearting.tar.gz\n"
    )
    index.write_text(
        json.dumps(
            {
                "tag_name": version,
                "assets": [
                    {
                        "name": "hearting.tar.gz",
                        "browser_download_url": archive.as_uri(),
                    },
                    {
                        "name": "hearting.tar.gz.sha256",
                        "browser_download_url": checksum.as_uri(),
                    },
                ],
            }
        )
    )
    return archive

activation_calls = []
def fake_activate(release_root, runtimes):
    selected = list(runtimes)
    activation_calls.append((Path(release_root).name, tuple(selected)))
    return {
        "runtimes": selected,
        "session_action": {runtime: {"skill": "new-session"} for runtime in selected},
    }

d._activate_release = fake_activate

try:
    d.enable_auto_update()
except d.DistributionError:
    pass
else:
    raise AssertionError("auto-update enabled without a managed release")

make_release("v1.0.0")
installed = d.bootstrap("example/harness", "latest", d.RUNTIMES, True)
assert installed["status"] == "installed"
assert Path(installed["release_root"]).name == "v1.0.0"
assert d.current_path().resolve().name == "v1.0.0"
assert d.launcher_path().is_symlink()
assert d.is_managed()
service, timer = d._systemd_paths()
assert service.is_file() and timer.is_file()
assert " update --auto" in service.read_text()
assert "XDG_DATA_HOME=" + os.environ["XDG_DATA_HOME"] in service.read_text()
assert "XDG_STATE_HOME=" + os.environ["XDG_STATE_HOME"] in service.read_text()
status = d.auto_update_status()
assert status["status"] == "configured"
assert status["health"] == "unknown"
assert status["scheduler"]["probe"] == "unavailable"
assert len(probe_calls) == 1 and probe_calls[0][:3] == ("systemctl", "--user", "show")

old_probe = d._probe_command
old_run = d.subprocess.run
def permission_denied(*args, **kwargs):
    raise PermissionError("scheduler probe denied")
d._probe_command = real_probe_command
d.subprocess.run = permission_denied
try:
    denied = d.auto_update_status()
    assert denied["status"] == "configured"
    assert denied["health"] == "unknown"
    assert denied["scheduler"]["probe"] == "unavailable"
    assert "denied" in denied["scheduler"]["detail"]
finally:
    d.subprocess.run = old_run
    d._probe_command = old_probe

old_run = d.subprocess.run
def unexpected_probe(*args, **kwargs):
    raise AssertionError("non-owned status command reached subprocess")
d.subprocess.run = unexpected_probe
try:
    allowed, detail = real_probe_command([
        "systemctl", "--user", "show", "foreign.timer",
        "--property=LoadState,ActiveState,UnitFileState,LastTriggerUSec",
    ])
    assert not allowed and "not allowlisted" in detail
finally:
    d.subprocess.run = old_run

def status_fixture(platform, responses, files=True):
    old_platform = os.environ.get("HARNESS_TEST_PLATFORM")
    old_probe = d._probe_command
    calls = []
    os.environ["HARNESS_TEST_PLATFORM"] = platform
    if platform == "darwin" and files:
        path = d._launch_agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    def fake_probe(command):
        calls.append(tuple(command))
        value = responses[len(calls) - 1]
        return value
    d._probe_command = fake_probe
    try:
        return d.auto_update_status(), calls
    finally:
        d._probe_command = old_probe
        if old_platform is None:
            os.environ.pop("HARNESS_TEST_PLATFORM", None)
        else:
            os.environ["HARNESS_TEST_PLATFORM"] = old_platform

healthy_timer = "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nLastTriggerUSec=now\n"
healthy_service = "Result=success\nExecMainStatus=0\n"
healthy, calls = status_fixture("linux", [(True, healthy_timer), (True, healthy_service)])
assert healthy["health"] == "ok" and healthy["scheduler"]["exit_status"] == 0
assert len(calls) == 2
never_timer = healthy_timer.replace("LastTriggerUSec=now", "LastTriggerUSec=n/a")
never, calls = status_fixture("linux", [(True, never_timer)])
assert never["health"] == "ok" and never["scheduler"]["last_trigger"] is None
assert never["scheduler"]["last_result"] is None and len(calls) == 1
failed, _ = status_fixture("linux", [(True, healthy_timer), (True, "Result=failed\nExecMainStatus=7\n")])
assert failed["status"] == "configured" and failed["health"] == "degraded"
assert failed["scheduler"]["exit_status"] == 7
inactive_timer = healthy_timer.replace("ActiveState=active", "ActiveState=inactive")
inactive, _ = status_fixture("linux", [(True, inactive_timer), (True, healthy_service)])
assert inactive["health"] == "degraded" and inactive["scheduler"]["active"] is False
no_service_result, _ = status_fixture("linux", [(True, healthy_timer), (False, "service unavailable")])
assert no_service_result["health"] == "ok"
assert no_service_result["scheduler"]["last_result"] is None
assert "last result unavailable" in no_service_result["scheduler"]["detail"]
launch_output = "state = not running\n" + ("fixture = value\n" * 20) + "last exit code = 0\n"
launch, calls = status_fixture("darwin", [(True, launch_output)])
assert launch["kind"] == "launch-agent" and launch["health"] == "ok"
assert launch["scheduler"]["active"] is True and len(calls) == 1
assert "state=not running" in launch["scheduler"]["detail"]
old_run = d.subprocess.run
def successful_launch_probe(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, stdout=launch_output, stderr="warning")
d.subprocess.run = successful_launch_probe
try:
    allowed, complete_output = real_probe_command([
        "launchctl", "print", f"gui/{os.getuid()}/com.hearting.update",
    ])
    assert allowed and complete_output == launch_output.strip()
    assert "last exit code = 0" in complete_output
finally:
    d.subprocess.run = old_run
launch_never, _ = status_fixture(
    "darwin", [(True, "state = not running\nlast exit code = (never exited)\n")]
)
assert launch_never["health"] == "ok"
assert launch_never["scheduler"]["last_result"] is None
launch_failed, _ = status_fixture("darwin", [(True, "state = exited\nlast exit code = 5\n")])
assert launch_failed["health"] == "degraded"
assert launch_failed["scheduler"]["exit_status"] == 5
malformed, _ = status_fixture("darwin", [(True, "state = running\nlast exit code = nope\n")])
assert malformed["health"] == "unknown" and malformed["scheduler"]["probe"] == "unavailable"
unsupported, calls = status_fixture("freebsd", [], files=False)
assert unsupported["status"] == "disabled" and unsupported["health"] == "unsupported"
assert calls == []
os.environ["HARNESS_TEST_PLATFORM"] = "linux"

same = d.update()
assert same["status"] == "up-to-date"
assert len(activation_calls) == 1

old_root = Path(installed["release_root"])
linked = tmp / "linked-checkout"
linked.mkdir()
(linked / "sentinel").write_text("unchanged")
runtime_homes = {
    "claude": Path(os.environ["HOME"]) / ".claude",
    "codex": Path(os.environ["HOME"]) / ".codex",
    "opencode": Path(os.environ["XDG_CONFIG_HOME"]) / "opencode",
}
for runtime, home in runtime_homes.items():
    state = {
        "mode": "linked" if runtime == "claude" else "packaged",
        "source_root": str(linked if runtime == "claude" else old_root),
    }
    path = home / ".harness/activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state))

make_release("v1.1.0")
updated = d.update()
assert updated["status"] == "updated"
assert set(updated["runtimes"]) == {"codex", "opencode"}
assert updated["skipped"] == {"claude": "linked"}
assert (linked / "sentinel").read_text() == "unchanged"
assert d.current_path().resolve().name == "v1.1.0"
for runtime in ("codex", "opencode"):
    path = runtime_homes[runtime] / ".harness/activation.json"
    path.write_text(
        json.dumps(
            {
                "mode": "packaged",
                "source_root": updated["release_root"],
            }
        )
    )
state_before_failure = d.state_path().read_bytes()

make_release("v1.2.0")
os.environ["HARNESS_TEST_FAIL_STATE_COMMIT"] = "1"
try:
    d.update()
except d.DistributionError:
    pass
else:
    raise AssertionError("state commit failure was accepted")
finally:
    os.environ.pop("HARNESS_TEST_FAIL_STATE_COMMIT")
assert d.current_path().resolve().name == "v1.1.0"
assert d.state_path().read_bytes() == state_before_failure
assert not (d.data_root() / "releases/v1.2.0").exists()
assert activation_calls[-1][0] == "v1.1.0"

for version, attack, wrong in [
    ("v1.2.1", None, True),
    ("v1.2.2", "traversal", False),
    ("v1.2.3", "symlink", False),
    ("v1.2.4", "hardlink", False),
    ("v1.2.5", "fifo", False),
]:
    make_release(version, attack=attack, wrong_checksum=wrong)
    try:
        d.update()
    except d.DistributionError:
        pass
    else:
        raise AssertionError(f"malicious release accepted: {version}")
    assert d.current_path().resolve().name == "v1.1.0"

# Explicit tags persist as a pin. A same-asset check repairs owned pointers,
# while the scheduled --auto path exits without consulting latest metadata.
archive = assets / "v1.1.0.tar.gz"
checksum = assets / "v1.1.0.sha256"
index.write_text(
    json.dumps(
        {
            "tag_name": "v1.1.0",
            "assets": [
                {"name": d.ARCHIVE_NAME, "browser_download_url": archive.as_uri()},
                {"name": d.CHECKSUM_NAME, "browser_download_url": checksum.as_uri()},
            ],
        }
    )
)
d.current_path().unlink()
d.launcher_path().unlink()
pinned = d.update(version="v1.1.0")
assert pinned["status"] == "repaired"
assert d.current_path().resolve().name == "v1.1.0"
assert d.launcher_path().is_symlink()
assert json.loads(d.state_path().read_text())["pinned_version"] == "v1.1.0"
assert "profile" not in json.loads(d.state_path().read_text())
index.unlink()
assert d.update(automatic=True)["status"] == "pinned"
index.write_text(
    json.dumps(
        {
            "tag_name": "v1.1.0",
            "assets": [
                {"name": d.ARCHIVE_NAME, "browser_download_url": archive.as_uri()},
                {"name": d.CHECKSUM_NAME, "browser_download_url": checksum.as_uri()},
            ],
        }
    )
)

# Legacy distribution state read-compat: an old state file may still carry
# the retired `profile` field. update() must succeed without it driving any
# runtime reconfiguration, and the rewritten state must drop the field.
legacy_state = json.loads(d.state_path().read_text())
legacy_state["profile"] = "builder"
d.state_path().write_text(json.dumps(legacy_state))
calls_before_legacy_profile = len(activation_calls)
legacy_result = d.update()
assert legacy_result["status"] in ("repaired", "up-to-date")
assert "profile" not in json.loads(d.state_path().read_text())
assert len(activation_calls) == calls_before_legacy_profile

disabled = d.auto_update("disable")
assert disabled["status"] == "disabled"
assert not service.exists() and not timer.exists()
enabled = d.auto_update("enable")
assert enabled["status"] == "configured-manual"
assert service.exists() and timer.exists()

state_bytes = d.state_path().read_bytes()
d.state_path().unlink()
d.state_path().symlink_to(tmp / "foreign-state")
try:
    d.is_managed()
except d.DistributionError:
    pass
else:
    raise AssertionError("symlinked distribution state was accepted")
d.state_path().unlink()
d._atomic_bytes(d.state_path(), state_bytes)
lock_path = d.state_root() / "distribution.lock"
lock_path.unlink(missing_ok=True)
lock_path.symlink_to(tmp / "foreign-lock")
try:
    d.enable_auto_update()
except d.DistributionError:
    pass
else:
    raise AssertionError("symlinked distribution lock was accepted")
lock_path.unlink()

# The installer CLI preserves legacy update behavior unless managed state is
# present, and maps managed update results without shelling out to Git.
original_is_managed = installer.distribution.is_managed
original_update = installer.distribution.update
installer.distribution.is_managed = lambda: True
installer.distribution.update = lambda **kwargs: {
    "status": "updated",
    "version": "v9",
    "runtimes": ["codex"],
    "skipped": {"claude": "linked"},
    "session_action": {"codex": {"skill": "new-session"}},
}
args = argparse.Namespace(
    runtimes=None,
    reapply=False,
    dry_run=False,
    version="latest",
    scope="global",
    plugin=False,
    auto=False,
)
cli_result = installer.cmd_update(args)
assert cli_result["exit"] == 0 and cli_result["channel"] == "managed-release"
assert "claude (linked)" in "\n".join(cli_result["lines"])
args.dry_run = True
blocked_dry_run = installer.cmd_update(args)
assert blocked_dry_run["exit"] == installer.EXIT_BLOCKED
installer.distribution.is_managed = original_is_managed
installer.distribution.update = original_update

status_args = argparse.Namespace(operation="status")
original_status = installer.distribution.auto_update_status
installer.distribution.auto_update_status = lambda: healthy
try:
    status_result = installer.cmd_auto_update(status_args)
finally:
    installer.distribution.auto_update_status = original_status
assert status_result["checks"][0]["detail"] == "configured (systemd-user) health=ok"
plain = io.StringIO()
with contextlib.redirect_stdout(plain):
    assert installer.emit(status_result, False) == 0
assert "managed release:" in plain.getvalue()
assert "scheduler state: loaded=yes active=yes enabled=yes" in plain.getvalue()
assert "last trigger: now" in plain.getvalue()
assert "last result: success (exit=0)" in plain.getvalue()
encoded = io.StringIO()
with contextlib.redirect_stdout(encoded):
    assert installer.emit(status_result, True) == 0
json_result = json.loads(encoded.getvalue())
assert set(("operation", "scheduler", "checks", "drift", "exit")) <= json_result.keys()
assert json_result["scheduler"]["health"] == "ok"
assert encoded.getvalue().count("\n") == 1

# The release builder is deterministic, adds the version marker, and excludes
# report caches from the public payload.
spec = importlib.util.spec_from_file_location(
    "build_release", root / "tools/install/build-release.py"
)
build_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_release)
for invalid_version in ("v-integration", "v1.0.0-01"):
    try:
        build_release.build_installer(
            invalid_version,
            tmp / "invalid-version",
            b"print('fixture')\n",
        )
    except SystemExit:
        pass
    else:
        raise AssertionError(f"invalid release version accepted: {invalid_version}")
fixture = tmp / "git-fixture"
fixture.mkdir()
subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=fixture, check=True)
subprocess.run(["git", "config", "user.name", "fixture"], cwd=fixture, check=True)
(fixture / "README.md").write_text("fixture\n")
(fixture / "tools/install").mkdir(parents=True)
(fixture / "tools/install/distribution.py").write_text(
    "#!/usr/bin/env python3\nprint('fixture distribution')\n"
)
(fixture / ".agent_reports").mkdir()
(fixture / ".agent_reports/cache").write_text("private report cache\n")
subprocess.run(["git", "add", "."], cwd=fixture, check=True)
subprocess.run(["git", "commit", "-qm", "fixture"], cwd=fixture, check=True)
first = build_release.build(fixture, "v3.0.0", tmp / "dist1", "HEAD")
second = build_release.build(fixture, "v3.0.0", tmp / "dist2", "HEAD")
for left, right in zip(first, second):
    assert left.name == right.name
    assert hashlib.sha256(left.read_bytes()).digest() == hashlib.sha256(right.read_bytes()).digest()
archive, archive_checksum, release_installer, installer_checksum = first
assert release_installer.stat().st_mode & 0o111
installer_text = release_installer.read_text()
assert "RELEASE_VERSION='v3.0.0'" in installer_text
assert "REPOSITORY='dmlguq456/hearting'" in installer_text
assert "fixture distribution" in installer_text
assert "--version \"$RELEASE_VERSION\"" in installer_text
assert "hearting.tar.gz" in archive_checksum.read_text()
assert "install.sh" in installer_checksum.read_text()
with tarfile.open(archive, "r:gz") as bundle:
    names = bundle.getnames()
    assert "hearting/RELEASE_VERSION" in names
    assert not any(name.startswith("hearting/.agent_reports") for name in names)

print("ok - managed release install/update/rollback/security/scheduler/build")
PY

env -u AGENT_HOME "$ROOT/tools/install/harness.sh" --help >/dev/null
python3 -m py_compile "$ROOT/tools/install/distribution.py" "$ROOT/tools/install/build-release.py" "$ROOT/tools/install/installer.py"
sh -n "$ROOT/install.sh" "$ROOT/tools/install/harness.sh"
! grep -Fq "raw.githubusercontent.com" "$ROOT/install.sh"
echo "ok - release launcher and syntax"

# Build a working-tree release (including the files under test), then exercise
# the real packaged activation for all three runtimes without a Git checkout.
INTEGRATION="$TMP/integration"
mkdir -p "$INTEGRATION/assets"
# This fixture deliberately replaces HOME. On shared filesystems the checkout
# can have a different numeric owner, so carry the exact test root into the
# temporary protected Git config instead of depending on the developer's HOME.
git config --file "$HOME/.gitconfig" --add safe.directory "$ROOT"
python3 - "$ROOT" "$INTEGRATION" <<'PY'
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

root = Path(sys.argv[1])
target = Path(sys.argv[2])
archive = target / "assets/hearting.tar.gz"
listed = subprocess.run(
    [
        "git", "-C", str(root), "ls-files", "--cached", "--others",
        "--exclude-standard", "-z",
    ],
    check=True,
    capture_output=True,
).stdout.decode().split("\0")
paths = sorted(
    {
        item
        for item in listed
        if item
        and not item.startswith((".agent_reports/", ".claude_reports/"))
        and "__pycache__" not in Path(item).parts
    }
)
with tarfile.open(archive, "w:gz") as bundle:
    for relative in paths:
        source = root / relative
        if source.exists() or source.is_symlink():
            bundle.add(source, arcname="hearting/" + relative, recursive=False)
    marker = b"v0.0.0-integration\n"
    info = tarfile.TarInfo("hearting/RELEASE_VERSION")
    info.mode = 0o644
    info.size = len(marker)
    bundle.addfile(info, io.BytesIO(marker))
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
checksum = target / "assets/hearting.tar.gz.sha256"
checksum.write_text(digest + "  hearting.tar.gz\n")
(target / "release.json").write_text(
    json.dumps(
        {
            "tag_name": "v0.0.0-integration",
            "assets": [
                {"name": "hearting.tar.gz", "browser_download_url": archive.as_uri()},
                {"name": "hearting.tar.gz.sha256", "browser_download_url": checksum.as_uri()},
            ],
        }
    )
)
spec = importlib.util.spec_from_file_location(
    "build_release", root / "tools/install/build-release.py"
)
build_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_release)
build_release.build_installer(
    "v0.0.0-integration",
    target / "assets",
    (root / "tools/install/distribution.py").read_bytes(),
    "example/harness",
)
PY

HOME="$INTEGRATION/home"
XDG_CONFIG_HOME="$HOME/.config"
XDG_DATA_HOME="$HOME/.local/share"
XDG_STATE_HOME="$HOME/.local/state"
HARNESS_BIN_DIR="$HOME/.local/bin"
HARNESS_RELEASE_INDEX_URL="file://$INTEGRATION/release.json"
CODEX_HOME="$HOME/.codex"
CLAUDE_CONFIG_DIR="$HOME/.claude"
export HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME HARNESS_BIN_DIR
export HARNESS_RELEASE_INDEX_URL CODEX_HOME CLAUDE_CONFIG_DIR
mkdir -p "$HOME"

set +e
"$INTEGRATION/assets/install.sh" --version v-other > "$INTEGRATION/version-override.out" 2>&1
OVERRIDE_EXIT=$?
set -e
[ "$OVERRIDE_EXIT" -eq 64 ]

set +e
"$INTEGRATION/assets/install.sh" --repository other/harness > "$INTEGRATION/repository-override.out" 2>&1
REPOSITORY_OVERRIDE_EXIT=$?
set -e
[ "$REPOSITORY_OVERRIDE_EXIT" -eq 64 ]

HARNESS_REPOSITORY=other/harness HARNESS_VERSION=v-other "$INTEGRATION/assets/install.sh" --no-auto-update --json > "$INTEGRATION/install.json"
"$HARNESS_BIN_DIR/harness" runtime doctor --runtime all --strict --json > "$INTEGRATION/doctor.json"
"$HARNESS_BIN_DIR/harness" update --json > "$INTEGRATION/update.json"

python3 - "$INTEGRATION/install.json" "$INTEGRATION/doctor.json" "$INTEGRATION/update.json" <<'PY'
import json, os, sys
installed = json.load(open(sys.argv[1]))
doctor = json.load(open(sys.argv[2]))
updated = json.load(open(sys.argv[3]))
state = json.load(
    open(os.path.join(os.environ["XDG_STATE_HOME"], "hearting/distribution.json"))
)
assert installed["status"] == "installed"
assert installed["version"] == "v0.0.0-integration"
assert state["repository"] == "example/harness"
assert set(installed["runtimes"]) == {"claude", "codex", "opencode"}
assert doctor["exit"] == 0
assert updated["release"]["status"] == "up-to-date"
PY

HARNESS_INSTALL_URL="file://$INTEGRATION/assets/install.sh" "$ROOT/install.sh" --no-auto-update --json > "$INTEGRATION/legacy-redirect.json"
python3 - "$INTEGRATION/legacy-redirect.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
assert result["status"] in {"up-to-date", "repaired"}
assert result["version"] == "v0.0.0-integration"
PY
echo "ok - release-bound installer and legacy redirect activate and verify all runtimes"
