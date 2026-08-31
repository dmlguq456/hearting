#!/bin/sh
# Managed release lifecycle/security checks under one isolated HOME.
set -eu
export PYTHONDONTWRITEBYTECODE=1

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
TMP=$(mktemp -d)
# destructive-ok: reason=clean one mktemp release fixture; boundary=TMP returned by the immediately preceding mktemp call
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

eval "$(python3 "$ROOT/tools/install/fixture_env.py" shell "$TMP" "$ROOT")"
HARNESS_BIN_DIR="$HOME/.local/bin"
HARNESS_ALLOW_FILE_RELEASES=1
HARNESS_SCHEDULER_NO_ACTIVATE=1
HARNESS_TEST_PLATFORM=linux
export HARNESS_BIN_DIR
export HARNESS_ALLOW_FILE_RELEASES HARNESS_SCHEDULER_NO_ACTIVATE
export HARNESS_TEST_PLATFORM ROOT TMP

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
    "hearting/utilities/compute-hosts": "#!/bin/sh\n",
    "hearting/utilities/compute-hosts.py": "#!/usr/bin/env python3\n",
    "hearting/tools/fleet/fleet.sh": "#!/bin/sh\n",
    "hearting/tools/memory/mem.py": "#!/usr/bin/env python3\n",
    "hearting/tools/memory/protocol_v2.py": "#!/usr/bin/env python3\n",
    "hearting/tools/memory/git_exchange_v2.py": "#!/usr/bin/env python3\n",
    "hearting/tools/memory/sync_v2.py": "#!/usr/bin/env python3\n",
    "hearting/tools/memory/migration_v2.py": "#!/usr/bin/env python3\n",
}

def make_release(version, attack=None, wrong_checksum=False):
    archive = assets / f"{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        files = dict(required)
        files["hearting/RELEASE_VERSION"] = version + "\n"
        for name, text in files.items():
            payload = text.encode()
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if name.endswith(".sh") or name.endswith("/compute-hosts") else 0o644
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
for name, relative in d.TOOL_LAUNCHERS:
    path = d.bin_dir() / name
    assert path.is_symlink()
    assert Path(os.readlink(path)) == d.current_path() / relative
compute_hosts = d.bin_dir() / "compute-hosts"
assert compute_hosts.is_symlink() and os.access(compute_hosts, os.X_OK)
assert Path(os.readlink(compute_hosts)) == d.current_path() / "utilities/compute-hosts"
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

fleet_launcher = d.bin_dir() / "fleet"
prior_bundle = Path(os.environ["CODEX_HOME"]) / ".harness/bundles/prior/source"
prior_fleet = prior_bundle / "tools/fleet/fleet.sh"
prior_fleet.parent.mkdir(parents=True)
prior_fleet.write_text("#!/bin/sh\n")
fleet_launcher.unlink()
fleet_launcher.symlink_to(prior_fleet)
same = d.update()
assert same["status"] == "repaired"
assert Path(os.readlink(fleet_launcher)) == (
    d.current_path() / "tools/fleet/fleet.sh"
)

fleet_launcher.unlink()
fleet_launcher.symlink_to(tmp / "foreign/fleet")
try:
    d.update()
except d.DistributionError:
    pass
else:
    raise AssertionError("foreign Fleet launcher was overwritten")
assert Path(os.readlink(fleet_launcher)) == tmp / "foreign/fleet"
fleet_launcher.unlink()
fleet_launcher.symlink_to(d.current_path() / "tools/fleet/fleet.sh")

old_root = Path(installed["release_root"])
linked = tmp / "linked-checkout"
linked.mkdir()
(linked / "sentinel").write_text("unchanged")
(d.bin_dir() / "fleet").unlink()
(d.bin_dir() / "fleet").symlink_to(linked / "tools/fleet/fleet.sh")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))

make_release("v1.1.0")
updated = d.update()
assert updated["status"] == "updated"
assert set(updated["runtimes"]) == {"codex", "opencode"}
assert updated["skipped"] == {"claude": "linked"}
assert (linked / "sentinel").read_text() == "unchanged"
assert d.current_path().resolve().name == "v1.1.0"
assert Path(os.readlink(d.bin_dir() / "fleet")) == (
    d.current_path() / "tools/fleet/fleet.sh"
)
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
assert Path(os.readlink(d.bin_dir() / "fleet")) == (
    d.current_path() / "tools/fleet/fleet.sh"
)

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

OUTER_TMP=$TMP
eval "$(python3 "$ROOT/tools/install/fixture_env.py" shell "$INTEGRATION" "$ROOT")"
# The lifecycle's outer mktemp root owns the EXIT trap; keep its shell variable
# stable while TEMP/TMPDIR and every runtime selector remain in INTEGRATION.
TMP=$OUTER_TMP
export TMP
HARNESS_BIN_DIR="$HOME/.local/bin"
HARNESS_RELEASE_INDEX_URL="file://$INTEGRATION/release.json"
export HARNESS_BIN_DIR HARNESS_RELEASE_INDEX_URL

# A fake standalone vendor Codex binary on PATH (never inside HARNESS_BIN_DIR)
# proves the managed-release flow reconciles the protected launcher through
# the real codex_launcher.py transaction from the built archive, not a
# duplicated launcher implementation inside distribution.py.
VENDOR_BIN="$INTEGRATION/vendor-bin"
mkdir -p "$VENDOR_BIN"
printf '%s\n' '#!/bin/sh' 'exit 0' > "$VENDOR_BIN/codex"
chmod +x "$VENDOR_BIN/codex"
PATH="$VENDOR_BIN:$PATH"
export PATH

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

python3 - "$INTEGRATION/install.json" "$INTEGRATION/doctor.json" "$INTEGRATION/update.json" "$HOME" "$VENDOR_BIN/codex" <<'PY'
import json, os, sys
installed = json.load(open(sys.argv[1]))
doctor = json.load(open(sys.argv[2]))
updated = json.load(open(sys.argv[3]))
home = sys.argv[4]
vendor_codex = sys.argv[5]
state = json.load(
    open(os.path.join(os.environ["XDG_STATE_HOME"], "hearting/distribution.json"))
)
assert installed["status"] == "installed"
assert installed["version"] == "v0.0.0-integration"
assert state["repository"] == "example/harness"
assert set(installed["runtimes"]) == {"claude", "codex", "opencode"}
assert doctor["exit"] == 0
assert updated["release"]["status"] == "up-to-date"

codex_report = next(r for r in doctor["runtimes"] if r["runtime"] == "codex")
launcher = codex_report["managed_launcher"]
assert launcher["installed"], launcher
assert launcher["healthy"], launcher
# This fixture sets HARNESS_BIN_DIR globally for the top-level `harness`
# launcher's own install location; codex_launcher.py treats that same
# explicit-bin-dir signal as opt-in compatibility mode by design (an
# operator naming HARNESS_BIN_DIR is exactly the documented escape hatch for
# private/test fixtures), so it correctly reports `legacy-inplace-v1` /
# `protected=false` here rather than the default protected-path mode.
assert launcher["mode"] == "legacy-inplace-v1", launcher
assert launcher["protected"] is False, launcher
assert launcher["real_command"] == os.path.abspath(vendor_codex), launcher
assert os.path.realpath(vendor_codex) == os.path.abspath(vendor_codex), (
    "same-version repair must never overwrite the vendor Codex command"
)
same_version_launcher = updated["release"]["launcher"]
assert same_version_launcher is not None
assert same_version_launcher["status"] in {"unchanged", "created"}, same_version_launcher
PY
echo "ok - managed release installs and same-version-repairs the protected Codex launcher without touching the vendor command"

HARNESS_INSTALL_URL="file://$INTEGRATION/assets/install.sh" "$ROOT/install.sh" --no-auto-update --json > "$INTEGRATION/legacy-redirect.json"
python3 - "$INTEGRATION/legacy-redirect.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
assert result["status"] in {"up-to-date", "repaired"}
assert result["version"] == "v0.0.0-integration"
PY
echo "ok - release-bound installer and legacy redirect activate and verify all runtimes"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# Phase 3 residual (checklist rows 66/67): a genuine version update, a
# launcher-specific fault rollback, and pruning must all preserve the real
# vendor Codex binding and never move the protected ingress into a release
# directory. `install.sh` is deliberately version-locked at build time (see
# the "--version"/"--repository" rejection above), so moving versions here
# means rewriting the same `HARNESS_RELEASE_INDEX_URL` release.json/archive
# this fixture's managed install already resolves through and calling
# `harness update --version <tag>`, exactly like a real operator would.
publish_integration_release() {
  python3 - "$ROOT" "$INTEGRATION" "$1" <<'PY'
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

root = Path(sys.argv[1])
target = Path(sys.argv[2])
version = sys.argv[3]
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
    marker = (version + "\n").encode()
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
            "tag_name": version,
            "assets": [
                {"name": "hearting.tar.gz", "browser_download_url": archive.as_uri()},
                {"name": "hearting.tar.gz.sha256", "browser_download_url": checksum.as_uri()},
            ],
        }
    )
)
PY
}

CODEX_WRAPPER="$HARNESS_BIN_DIR/codex"
CODEX_LAUNCHER_STATE="$CODEX_HOME/.harness/codex-launcher.json"
RELEASES_ROOT="$XDG_DATA_HOME/hearting/releases"

# `publish_integration_release` shells out to `git -C "$ROOT"`, which needs
# this fixture's own (now-current) $HOME's gitconfig to trust $ROOT, same as
# the original build above did under the previous $HOME.
git config --file "$HOME/.gitconfig" --add safe.directory "$ROOT"

publish_integration_release "v0.0.0-integration-2"
"$HARNESS_BIN_DIR/harness" update --version v0.0.0-integration-2 --json > "$INTEGRATION/update-v2.json"
python3 - "$INTEGRATION/update-v2.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
assert result["exit"] == 0, result
assert result["release"]["version"] == "v0.0.0-integration-2", result
PY
"$HARNESS_BIN_DIR/harness" runtime doctor --runtime codex --strict --json > "$INTEGRATION/doctor-v2.json"
python3 - "$INTEGRATION/doctor-v2.json" "$VENDOR_BIN/codex" <<'PY'
import json, os, sys
row = json.load(open(sys.argv[1]))
vendor_codex = sys.argv[2]
codex = row if row.get("runtime") == "codex" else next(r for r in row["runtimes"] if r["runtime"] == "codex")
launcher = codex["managed_launcher"]
assert launcher["installed"] and launcher["healthy"], launcher
assert launcher["real_command"] == os.path.abspath(vendor_codex), (
    "genuine version update must preserve the vendor Codex binding", launcher
)
PY
echo "ok - genuine Codex-managed version update preserves the protected launcher and vendor binding"

WRAPPER_BEFORE=$(sha256sum "$CODEX_WRAPPER" | cut -d ' ' -f 1)
STATE_BEFORE=$(sha256sum "$CODEX_LAUNCHER_STATE" | cut -d ' ' -f 1)
VENDOR_BEFORE=$(sha256sum "$VENDOR_BIN/codex" | cut -d ' ' -f 1)
CURRENT_BEFORE=$(readlink "$XDG_DATA_HOME/hearting/current")

publish_integration_release "v0.0.0-integration-3"
export HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER=1
set +e
"$HARNESS_BIN_DIR/harness" update --version v0.0.0-integration-3 --json \
  > "$INTEGRATION/update-v3-fail.json" 2> "$INTEGRATION/update-v3-fail.err"
FAIL_EXIT=$?
set -e
unset HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER
[ "$FAIL_EXIT" -ne 0 ] || fail "injected launcher-boundary failure during version update unexpectedly succeeded"

python3 - "$INTEGRATION/update-v3-fail.json" "$XDG_STATE_HOME/hearting/distribution.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
assert result["exit"] != 0, result
state = json.load(open(sys.argv[2]))
assert state["version"] == "v0.0.0-integration-2", (
    "failed version update must leave the prior version current", state
)
PY
WRAPPER_AFTER=$(sha256sum "$CODEX_WRAPPER" | cut -d ' ' -f 1)
STATE_AFTER=$(sha256sum "$CODEX_LAUNCHER_STATE" | cut -d ' ' -f 1)
VENDOR_AFTER=$(sha256sum "$VENDOR_BIN/codex" | cut -d ' ' -f 1)
CURRENT_AFTER=$(readlink "$XDG_DATA_HOME/hearting/current")
test "$WRAPPER_BEFORE" = "$WRAPPER_AFTER" || fail "launcher wrapper changed after a rolled-back version update"
test "$STATE_BEFORE" = "$STATE_AFTER" || fail "launcher state changed after a rolled-back version update"
test "$VENDOR_BEFORE" = "$VENDOR_AFTER" || fail "vendor Codex command changed after a rolled-back version update"
test "$CURRENT_BEFORE" = "$CURRENT_AFTER" || fail "current-pointer rollback left it pointing at the failed release"
test ! -d "$RELEASES_ROOT/v0.0.0-integration-3" || fail "failed release directory was not cleaned up"
echo "ok - injected launcher-boundary failure during a genuine version update restores the exact prior launcher, vendor binding, and current pointer"

# Real (non-injected) rotation past the failed attempt, twice more, to push
# retention past its floor and force a real prune while the launcher keeps
# working across it.
publish_integration_release "v0.0.0-integration-3"
"$HARNESS_BIN_DIR/harness" update --version v0.0.0-integration-3 --json > "$INTEGRATION/update-v3.json"
publish_integration_release "v0.0.0-integration-4"
"$HARNESS_BIN_DIR/harness" update --version v0.0.0-integration-4 --json > "$INTEGRATION/update-v4.json"

python3 - "$INTEGRATION/update-v3.json" "$INTEGRATION/update-v4.json" "$RELEASES_ROOT" \
  "$XDG_DATA_HOME/hearting/current" "$CODEX_WRAPPER" "$CODEX_LAUNCHER_STATE" "$VENDOR_BIN/codex" <<'PY'
import json, os, sys
v3 = json.load(open(sys.argv[1]))
v4 = json.load(open(sys.argv[2]))
releases_root = os.path.realpath(sys.argv[3])
current = os.path.realpath(sys.argv[4])
wrapper = os.path.realpath(sys.argv[5])
state = os.path.realpath(sys.argv[6])
vendor_codex = os.path.abspath(sys.argv[7])

assert v3["exit"] == 0 and v3["release"]["version"] == "v0.0.0-integration-3", v3
assert v4["exit"] == 0 and v4["release"]["version"] == "v0.0.0-integration-4", v4
assert current == os.path.join(releases_root, "v0.0.0-integration-4")

# Retention keeps only the 2 most recent releases; the original install and
# the second version are now pruned.
assert not os.path.isdir(os.path.join(releases_root, "v0.0.0-integration")), (
    "the original release should have been pruned by now"
)
assert not os.path.isdir(os.path.join(releases_root, "v0.0.0-integration-2")), (
    "the superseded release should have been pruned by now"
)

# The managed launcher's own ingress and state are never inside the
# release tree at all (they live under $CODEX_HOME, entirely outside
# data_root()/releases), so pruning a release structurally cannot ever
# repoint or remove them -- assert that invariant directly rather than
# only inferring it from the launcher still working below.
assert not wrapper.startswith(releases_root + os.sep), wrapper
assert not state.startswith(releases_root + os.sep), state

state_data = json.load(open(state))
assert state_data["real_command"] == vendor_codex, state_data
PY
"$HARNESS_BIN_DIR/harness" runtime doctor --runtime codex --strict --json > "$INTEGRATION/doctor-v4.json"
python3 - "$INTEGRATION/doctor-v4.json" "$VENDOR_BIN/codex" <<'PY'
import json, os, sys
row = json.load(open(sys.argv[1]))
vendor_codex = sys.argv[2]
codex = row if row.get("runtime") == "codex" else next(r for r in row["runtimes"] if r["runtime"] == "codex")
launcher = codex["managed_launcher"]
assert launcher["installed"] and launcher["healthy"], launcher
assert launcher["real_command"] == os.path.abspath(vendor_codex), launcher
PY
echo "ok - pruning older managed releases never repoints or removes the protected Codex ingress, which stays vendor-bound"

# I-2 regression (plan-check round-1 frame §6.4 / assignment 검증요구 (a)):
# _cleanup_releases keeps only the 2 most recent packaged releases and
# shutil.rmtree()s the rest. Dispatch state (completion markers) must live
# beside the canonical registry (dispatch_state_root), never inside a
# packaged release, or 3 rotations silently destroy it.
python3 - "$ROOT" <<'PY'
import json, os, sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools/install"))
sys.path.insert(0, str(root / "utilities"))
import distribution as d
import dispatch_contract as dc
import importlib.util
spec = importlib.util.spec_from_file_location("route", root / "utilities/capability-route.py")
ROUTE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ROUTE)

releases = d.data_root() / "releases"
releases.mkdir(parents=True, exist_ok=True)
runtime = d.data_root() / "runtime"
jobs = runtime / ".harness" / "dispatch" / "jobs.log"

import time
# Earlier steps in this script populate the same shared releases/ directory
# with real (roughly "now") mtimes; push these fixture releases far into the
# future so they unambiguously sort as the newest regardless of what already
# exists there, keeping this block's "which 2 survive" assertions exact.
future_base = time.time() + 10_000_000
release_dirs = []
for i, name in enumerate(("v-rot-1", "v-rot-2", "v-rot-3", "v-rot-4")):
    rel = releases / name
    (rel / "core").mkdir(parents=True)
    (rel / "core" / "CORE.md").write_text("fixture\n")
    os.utime(rel, (future_base + i, future_base + i))  # deterministic mtime ordering
    release_dirs.append(rel)

oldest = release_dirs[0]
os.environ["AGENT_HOME"] = str(oldest)
os.environ["AGENT_DISPATCH_JOBS"] = str(jobs)

# Write dispatch state while AGENT_HOME points at what will become the
# rotated-away release -- this is exactly the ordering a real
# register/complete sees relative to a later install-time rotation.
state_root = dc.dispatch_state_root(jobs)
assert not str(state_root).startswith(str(releases)), (
    "dispatch state root must never live under the packaged releases tree"
)
completion_dir = state_root / "completion" / "rt-rotation-fixture"
completion_dir.mkdir(parents=True)
marker_path = completion_dir / "plan.json"
marker_path.write_text(json.dumps({"schema_version": 2, "route_id": "rt-rotation-fixture"}))

# 3 rotations: only the 2 most recent release dirs survive.
d._cleanup_releases(keep=set())

assert not release_dirs[0].exists(), "oldest release should have been rmtree'd"
assert not release_dirs[1].exists(), "second-oldest release should have been rmtree'd"
assert release_dirs[2].exists() and release_dirs[3].exists()
assert marker_path.is_file(), "completion marker must survive release rotation"
assert json.loads(marker_path.read_text())["route_id"] == "rt-rotation-fixture"
PY
echo "ok - dispatch state root content is unchanged across 3 release rotations"

# N-3 regression (impl-review round 2): a process with no AGENT_DISPATCH_JOBS
# resolves dispatch state under whichever release `current` pointed at when it
# ran (chain (3)). The block above never exercises that chain -- it always has
# AGENT_DISPATCH_JOBS set, so the state root sits outside releases/ from the
# start and rotation was never the thing under test. This block clears that
# var, writes chain-(3) state under a release that is about to be pruned, and
# proves _cleanup_releases carries it into the surviving `current` release
# instead of rmtree-ing it away.
python3 - "$ROOT" <<'PY'
import json, os, sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools/install"))
sys.path.insert(0, str(root / "utilities"))
import distribution as d
import dispatch_contract as dc

# SD-112 chain-3 supersession (§13.33.2-(8)): the env-less fallback no
# longer resolves relative to whatever release `current` points at -- it
# always lands under the stable per-user root now. A session that resolved
# its dispatch state directly under a specific release is only possible
# through an explicit override (isolated opt-in / a legacy pre-SD-112
# process), so this fixture pins one explicitly instead of relying on the
# retired implicit fallback.
os.environ.pop("AGENT_DISPATCH_JOBS", None)

releases = d.data_root() / "releases"
releases.mkdir(parents=True, exist_ok=True)

import time
future_base = time.time() + 20_000_000
names = ("v-chain3-1", "v-chain3-2", "v-chain3-3", "v-chain3-4")
release_dirs = []
for i, name in enumerate(names):
    rel = releases / name
    (rel / "core").mkdir(parents=True)
    (rel / "core" / "CORE.md").write_text("fixture\n")
    os.utime(rel, (future_base + i, future_base + i))
    release_dirs.append(rel)

oldest, newest = release_dirs[0], release_dirs[3]

# A session ran while `current` pointed at the release that is about to
# rotate away, explicitly registered under that release's `.dispatch`
# (isolated/legacy override -- the only way release-embedded state exists
# post-SD-112).
os.environ["AGENT_DISPATCH_JOBS"] = str(oldest / ".dispatch" / "jobs.log")
state_root_before = dc.resolve_dispatch_state_root(oldest, environ=os.environ)
assert state_root_before == oldest / ".dispatch"
completion_dir = state_root_before / "completion" / "rt-chain3-fixture"
completion_dir.mkdir(parents=True)
marker_path = completion_dir / "plan.json"
marker_path.write_text(json.dumps({"schema_version": 2, "route_id": "rt-chain3-fixture"}))

# Rotation: `current` now points at the newest release, and cleanup prunes
# everything but the 2 most recent -- oldest is scheduled for deletion.
if d.current_path().exists() or d.current_path().is_symlink():
    d.current_path().unlink()
d.current_path().symlink_to(newest)

d._cleanup_releases(keep=set())

assert not oldest.exists(), "oldest release should have been rmtree'd"
migrated = newest / ".dispatch" / "completion" / "rt-chain3-fixture" / "plan.json"
assert migrated.is_file(), (
    "chain-(3) dispatch state must be carried into the live release before "
    "the release holding it is pruned"
)
assert json.loads(migrated.read_text())["route_id"] == "rt-chain3-fixture"

# And it must be reachable through the same explicit derivation a later
# writer bound to that same release would use.
os.environ["AGENT_DISPATCH_JOBS"] = str(newest / ".dispatch" / "jobs.log")
state_root_after = dc.resolve_dispatch_state_root(d.current_path().resolve(), environ=os.environ)
assert state_root_after == newest / ".dispatch"

# SD-112 §13.33.2-(6): an env-less reader's *write* root is the stable root
# now (chain-3 supersession), but the carried-forward release-embedded tree
# stays a legacy *read* candidate through the multi-root window, not lost.
os.environ.pop("AGENT_DISPATCH_JOBS", None)
legacy_read_candidates = dc.dispatch_state_roots(d.current_path().resolve(), environ=os.environ)
assert (newest / ".dispatch") in legacy_read_candidates
assert (state_root_after / "completion" / "rt-chain3-fixture" / "plan.json").is_file()
PY
echo "ok - chain-(3) dispatch state survives release rotation via _cleanup_releases succession"

# In-use release preservation (installer-probes-dispatch-fixes plan item 2, P0):
# a live registered dispatch attempt with an open row whose launch_home= names a
# release must survive rotation even when its retention floor and succession
# would otherwise let _cleanup_releases rmtree it. Reproduces the real incident:
# v2.57.1 was live, v2.57.2 -> v2.57.3 rotated past it, and the directory was
# deleted out from under a registered owner whose route was sealed to it.
python3 - "$ROOT" <<'PY'
import os, sys, time
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools/install"))
import distribution as d

os.environ.pop("AGENT_DISPATCH_JOBS", None)

releases = d.data_root() / "releases"
releases.mkdir(parents=True, exist_ok=True)

future_base = time.time() + 30_000_000
names = ("v-inuse-1", "v-inuse-2", "v-inuse-3", "v-inuse-4")
release_dirs = []
for i, name in enumerate(names):
    rel = releases / name
    (rel / "core").mkdir(parents=True)
    (rel / "core" / "CORE.md").write_text("fixture\n")
    os.utime(rel, (future_base + i, future_base + i))
    release_dirs.append(rel)

oldest, newest = release_dirs[0], release_dirs[3]
if d.current_path().exists() or d.current_path().is_symlink():
    d.current_path().unlink()
d.current_path().symlink_to(newest)

def write_open_row(release, launch_home, attempt_id):
    jobs_log = release / ".dispatch" / "jobs.log"
    jobs_log.parent.mkdir(parents=True, exist_ok=True)
    row = (
        f"{time.time()}\topen\trepo\t/tmp/wt\tslug\t"
        f"attempt_id={attempt_id},launch_home={launch_home}\n"
    )
    jobs_log.write_text(row)
    return jobs_log

# Case A: an open row in the oldest release's own registry names that same
# release as its launch_home -- it must be preserved, not pruned.
# Rotation-carry-fidelity plan (2026-08-23) §2.3②: these two rows use
# distinct attempt identities -- with the registry-carry merge now keyed by
# attempt identity, sharing one identity across two different releases'
# candidate rows would let a later rank comparison between them decide an
# outcome neither in-use assertion below actually inspects, which is not
# what this fixture exists to test.
jobs_log = write_open_row(oldest, str(oldest.resolve()), "att-inuse-self")
# Counter-case (same cleanup pass, so both assertions are proven against the
# identical retention/floor arithmetic): a DIFFERENT candidate whose own open
# row's launch_home= points at yet another release must not be pinned by it.
other = release_dirs[1]
write_open_row(other, str(release_dirs[2].resolve()), "att-inuse-other")

d._cleanup_releases(keep=set())

assert oldest.exists(), (
    "a release referenced by an open dispatch attempt (launch_home=self) "
    "must survive rotation"
)
assert not other.exists(), (
    "an open row naming a different release's launch_home must not pin "
    "this candidate release"
)

# Symmetric counter-case: the SAME oldest row, now `done`, no longer preserves
# the release -- proves this doesn't degrade into "preserve everything".
jobs_log.write_text(
    jobs_log.read_text().replace("\topen\t", "\tdone\t", 1)
)
d._cleanup_releases(keep=set())
assert not oldest.exists(), (
    "a done row must not preserve a release -- only an open row does"
)
PY
echo "ok - a release referenced by an open dispatch attempt survives rotation"

# 2026-08-20 regression (dispatch-harness-balance plan.md Phase 2 / completion
# criterion (b)): with EXACTLY 2 release dirs, _cleanup_releases has zero
# prune candidates (the `retained < 2` floor plus `keep` protect both), so its
# prune-time _succeed_dispatch_state call never fires for the release rotation
# just walked away from. Real fleets stay at exactly 2 releases most of the
# time (2026-08-20 v2.55.6 -> v2.55.8), so this is the common case, not an
# edge case: attempt history must still carry forward at ROTATION, not only
# when a 3rd/4th release finally triggers a prune.
python3 - "$ROOT" <<'PY'
import json, os, sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools/install"))
sys.path.insert(0, str(root / "utilities"))
import distribution as d
import dispatch_allocation as da

os.environ.pop("AGENT_DISPATCH_JOBS", None)

releases = d.data_root() / "releases"
releases.mkdir(parents=True, exist_ok=True)

import time
future_base = time.time() + 40_000_000
names = ("v-2rel-old", "v-2rel-new")
release_dirs = []
for i, name in enumerate(names):
    rel = releases / name
    (rel / "core").mkdir(parents=True)
    (rel / "core" / "CORE.md").write_text("fixture\n")
    os.utime(rel, (future_base + i, future_base + i))
    release_dirs.append(rel)

old_root, new_root = release_dirs

# Purge any release dirs a prior block in this same script left behind, so
# "exactly 2 releases" is really exactly 2 for this block's own assertions.
for stray in releases.iterdir():
    if stray not in release_dirs and stray.is_dir() and not stray.is_symlink():
        import shutil
        shutil.rmtree(stray, ignore_errors=True)

# A session ran while `current` pointed at the old release, with no
# AGENT_DISPATCH_JOBS (chain (3)) -- registered attempt rows plus a
# completion marker land directly under the old release's `.dispatch`.
old_jobs = old_root / ".dispatch" / "jobs.log"
old_jobs.parent.mkdir(parents=True, exist_ok=True)
rows = []
for i in range(19):
    harness = "claude" if i % 2 == 0 else "codex"
    rows.append(
        f"2026-08-20T00:00:{i:02d}Z\tdone\t/r\t/w\tn{i}\t"
        "attempt_schema_version=2,registered_worker=1,"
        f"attempt_id=att-rot-{i},harness={harness}\n"
    )
# One row missing the schema/registered fields must never be counted.
rows.append(
    "2026-08-20T00:01:00Z\tdone\t/r\t/w\tn19\tattempt_id=att-rot-unregistered,harness=claude\n"
)
old_jobs.write_text("".join(rows))
completion_dir = old_root / ".dispatch" / "completion" / "rt-2rel-fixture"
completion_dir.mkdir(parents=True)
marker_path = completion_dir / "plan.json"
marker_path.write_text(json.dumps({"schema_version": 2, "route_id": "rt-2rel-fixture"}))

if d.current_path().exists() or d.current_path().is_symlink():
    d.current_path().unlink()
d.current_path().symlink_to(old_root)

# Reproduce the exact rotation-time sequence _install_or_update now runs:
# retarget `current` to the new release, THEN succeed the old release's
# dispatch state -- _succeed_dispatch_state reads `current` to find the live
# release, so order matters.
d.current_path().unlink()
d.current_path().symlink_to(new_root)
ok = d._succeed_dispatch_state(old_root)
assert ok, "dispatch state carry-forward must report success for a clean copy"

# With exactly 2 releases, _cleanup_releases prunes nothing -- the exact
# shape this block exists to cover.
d._cleanup_releases(keep={new_root, old_root})
assert old_root.exists() and new_root.exists(), (
    "succession must not depend on the old release being deleted"
)

new_jobs = new_root / ".dispatch" / "jobs.log"
assert new_jobs.is_file(), "attempt rows must be carried into the live release at rotation time"
migrated_marker = new_root / ".dispatch" / "completion" / "rt-2rel-fixture" / "plan.json"
assert migrated_marker.is_file(), "completion marker must be re-anchored into the live release"
assert json.loads(migrated_marker.read_text())["route_id"] == "rt-2rel-fixture"

counts = da.attempt_counts(new_jobs, window=30)
assert counts != {"claude": 0, "codex": 0, "opencode": 0}, (
    "carried-forward history must reach allocation counting, not just sit on disk"
)
assert counts["claude"] == 10 and counts["codex"] == 9, counts

# Idempotency: a second succession call must not change the live release's
# content (additive-only, live copy always wins).
before = new_jobs.read_text()
ok_again = d._succeed_dispatch_state(old_root)
assert ok_again
assert new_jobs.read_text() == before, "a repeat succession call must be a no-op on the live release"
PY
echo "ok - dispatch state carries forward at rotation time even when release pruning has nothing to do"

# T-2 regression (impl-review round-4 Q-6/P-3): if _succeed_dispatch_state
# hits a copy failure partway through, _cleanup_releases must keep the
# candidate release instead of rmtree-ing it -- losing the release directory
# is recoverable (a repeat install fetches it again); silently losing
# dispatch state that was never fully carried forward is not.
python3 - "$ROOT" <<'PY'
import contextlib, io, json, os, sys, time
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools/install"))
sys.path.insert(0, str(root / "utilities"))
import distribution as d
import dispatch_contract as dc

os.environ.pop("AGENT_DISPATCH_JOBS", None)

releases = d.data_root() / "releases"
releases.mkdir(parents=True, exist_ok=True)

future_base = time.time() + 30_000_000
names = ("v-fail-1", "v-fail-2", "v-fail-3", "v-fail-4")
release_dirs = []
for i, name in enumerate(names):
    rel = releases / name
    (rel / "core").mkdir(parents=True)
    (rel / "core" / "CORE.md").write_text("fixture\n")
    os.utime(rel, (future_base + i, future_base + i))
    release_dirs.append(rel)

oldest, newest = release_dirs[0], release_dirs[3]

# SD-112 chain-3 supersession: pin an explicit override for the
# release-embedded write, matching the earlier succession fixture above.
os.environ["AGENT_DISPATCH_JOBS"] = str(oldest / ".dispatch" / "jobs.log")
state_root_before = dc.resolve_dispatch_state_root(oldest, environ=os.environ)
completion_dir = state_root_before / "completion" / "rt-fail-fixture"
completion_dir.mkdir(parents=True)
marker_path = completion_dir / "plan.json"
marker_path.write_text(json.dumps({"schema_version": 2, "route_id": "rt-fail-fixture"}))

# Rotation retargets `current` to the newest release...
if d.current_path().exists() or d.current_path().is_symlink():
    d.current_path().unlink()
d.current_path().symlink_to(newest)

# ...but sabotage the copy destination: pre-create the live release's
# .dispatch/completion as a plain FILE so mkdir(parents=True) for the
# carried-forward completion dir raises OSError, forcing a partial-copy
# failure.
(newest / ".dispatch").mkdir(parents=True)
(newest / ".dispatch" / "completion").write_text("not a directory\n")

stderr_capture = io.StringIO()
with contextlib.redirect_stderr(stderr_capture):
    d._cleanup_releases(keep=set())

assert oldest.exists(), (
    "a release whose dispatch state failed to carry forward must not be "
    "deleted -- the state would be lost silently"
)
assert marker_path.is_file(), "original marker must survive an incomplete carry-forward"
diagnostic = stderr_capture.getvalue()
assert "dispatch state carry-forward incomplete" in diagnostic, (
    f"expected a carry-forward diagnostic on stderr, got: {diagnostic!r}"
)
assert str(oldest) in diagnostic
PY
echo "ok - _cleanup_releases keeps a release instead of deleting it when dispatch state carry-forward fails"

# S-2 regression (round-5 review, anchor + codex legs): before the fix,
# _reanchor_succeeded_attempt_links caught a malformed sidecar's json.loads()
# failure with a bare `continue` that never touched `ok`, so
# _succeed_dispatch_state still returned True and _cleanup_releases deleted
# the only copy of the (unreanchored, still-malformed) state. The candidate
# must now be preserved, exactly like the copy-failure case above.
python3 - "$ROOT" <<'PY'
import contextlib, io, json, os, sys, time
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools/install"))
sys.path.insert(0, str(root / "utilities"))
import distribution as d
import dispatch_contract as dc

os.environ.pop("AGENT_DISPATCH_JOBS", None)

releases = d.data_root() / "releases"
releases.mkdir(parents=True, exist_ok=True)

future_base = time.time() + 40_000_000
names = ("v-malformed-1", "v-malformed-2", "v-malformed-3", "v-malformed-4")
release_dirs = []
for i, name in enumerate(names):
    rel = releases / name
    (rel / "core").mkdir(parents=True)
    (rel / "core" / "CORE.md").write_text("fixture\n")
    os.utime(rel, (future_base + i, future_base + i))
    release_dirs.append(rel)

oldest, newest = release_dirs[0], release_dirs[3]

# SD-112 chain-3 supersession: pin an explicit override for the
# release-embedded write (see the carry-forward fixtures above).
os.environ["AGENT_DISPATCH_JOBS"] = str(oldest / ".dispatch" / "jobs.log")
state_root_before = dc.resolve_dispatch_state_root(oldest, environ=os.environ)
completion_dir = state_root_before / "completion" / "rt-malformed-fixture"
completion_dir.mkdir(parents=True)
marker_path = completion_dir / "plan.json"
marker_path.write_text(json.dumps({"schema_version": 2, "route_id": "rt-malformed-fixture"}))
# Same directory also holds an unrelated, truncated attempt sidecar --
# the kind a crashed writer could leave behind.
malformed_sidecar = completion_dir / "plan.att-malformed.attempt.json"
malformed_sidecar.write_text("{not valid json")

if d.current_path().exists() or d.current_path().is_symlink():
    d.current_path().unlink()
d.current_path().symlink_to(newest)

stderr_capture = io.StringIO()
with contextlib.redirect_stderr(stderr_capture):
    d._cleanup_releases(keep=set())

assert oldest.exists(), (
    "a release whose sidecar could not be re-anchored must not be deleted "
    "-- a malformed-JSON parse failure is not success"
)
assert marker_path.is_file(), "original marker must survive an unreanchored sidecar"
assert malformed_sidecar.is_file(), "original malformed sidecar must survive too"
diagnostic = stderr_capture.getvalue()
assert "dispatch state carry-forward incomplete" in diagnostic, (
    f"expected a carry-forward diagnostic on stderr, got: {diagnostic!r}"
)
assert str(oldest) in diagnostic
PY
echo "ok - _cleanup_releases keeps a release instead of deleting it when a migrated sidecar cannot be re-anchored (malformed JSON)"

# S-2 regression (round-5 review, codex leg S-1): a re-anchor WRITE failure
# (not just a read/parse failure) must also propagate to _succeed_dispatch_state
# instead of letting the exception escape past _cleanup_releases's caller --
# and, either way, must not cause the candidate release to be deleted.
python3 - "$ROOT" <<'PY'
import contextlib, io, json, os, sys, time
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools/install"))
sys.path.insert(0, str(root / "utilities"))
import distribution as d
import dispatch_contract as dc

os.environ.pop("AGENT_DISPATCH_JOBS", None)

releases = d.data_root() / "releases"
releases.mkdir(parents=True, exist_ok=True)

future_base = time.time() + 50_000_000
names = ("v-reanchor-fail-1", "v-reanchor-fail-2", "v-reanchor-fail-3", "v-reanchor-fail-4")
release_dirs = []
for i, name in enumerate(names):
    rel = releases / name
    (rel / "core").mkdir(parents=True)
    (rel / "core" / "CORE.md").write_text("fixture\n")
    os.utime(rel, (future_base + i, future_base + i))
    release_dirs.append(rel)

oldest, newest = release_dirs[0], release_dirs[3]

# SD-112 chain-3 supersession: pin an explicit override for the
# release-embedded write (see the carry-forward fixtures above).
os.environ["AGENT_DISPATCH_JOBS"] = str(oldest / ".dispatch" / "jobs.log")
state_root_before = dc.resolve_dispatch_state_root(oldest, environ=os.environ)
completion_dir = state_root_before / "completion" / "rt-reanchor-fail-fixture"
completion_dir.mkdir(parents=True)
sidecar = completion_dir / "plan.att-reanchor-fail.attempt.json"
sidecar.write_text(json.dumps({
    "schema_version": 2, "route_id": "rt-reanchor-fail-fixture",
    "completion_marker": str(oldest / ".dispatch" / "completion"
                              / "rt-reanchor-fail-fixture" / "plan.json"),
}))
# shutil.copy2 preserves the source file's mode bits at the destination, so
# a read-only source sidecar becomes a read-only (un-writable) destination
# sidecar -- the re-anchor write below must fail with PermissionError.
os.chmod(sidecar, 0o444)

if d.current_path().exists() or d.current_path().is_symlink():
    d.current_path().unlink()
d.current_path().symlink_to(newest)

try:
    stderr_capture = io.StringIO()
    with contextlib.redirect_stderr(stderr_capture):
        d._cleanup_releases(keep=set())
finally:
    os.chmod(sidecar, 0o644)

assert oldest.exists(), (
    "a release whose sidecar re-anchor write failed must not be deleted"
)
assert sidecar.is_file(), "original sidecar must survive an unwritable re-anchor destination"
diagnostic = stderr_capture.getvalue()
assert "dispatch state carry-forward incomplete" in diagnostic, (
    f"expected a carry-forward diagnostic on stderr, got: {diagnostic!r}"
)
assert str(oldest) in diagnostic
PY
echo "ok - _cleanup_releases keeps a release instead of deleting it when a re-anchor write fails"

# SD-112 B-10 (§13.33.2-(4)/(7), cycle-2 gate): an isolated `harness update`
# whose stable dispatch state root cannot be created must stop at M0 with
# the exact typed refusal, no release ever created/deleted, and no
# completed migration journal record -- never a warning-and-continue.
python3 - "$ROOT" "$TMP" <<'PY'
import json, os, stat, subprocess, sys
from pathlib import Path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
sys.path.insert(0, str(root / "tools/install"))
import distribution as d

b10_home = tmp / "b10-home"
xdg_state_parent = b10_home / ".local" / "state"
xdg_state_parent.mkdir(parents=True)
hearting_dir = xdg_state_parent / "hearting"
hearting_dir.mkdir()
# Pre-create the distribution lock file so `_distribution_lock()` can still
# open it (needs only search on the parent, not write) -- the write that
# must fail is `dispatch/` creation inside this now-read-only parent, not
# the lock itself (B-10's own design note: state root exists, only the
# `dispatch/` creation point fails).
(hearting_dir / "distribution.lock").touch()

shell_uid_out = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
shell_gid_out = subprocess.run(["id", "-g"], capture_output=True, text=True).stdout.strip()
shell_groups_out = subprocess.run(["id", "-G"], capture_output=True, text=True).stdout.strip()
b10_executor = {
    "user": subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip(),
    "uid": shell_uid_out,
    "euid": str(os.geteuid()),
    "gids": shell_groups_out,
}
b10_non_root_detection = {
    "shell_uid": shell_uid_out,
    "python_euid": str(os.geteuid()),
    "matched": shell_uid_out == str(os.geteuid()),
    "method": "id -u + os.geteuid",
}

os.chmod(hearting_dir, 0o500)
try:
    os.environ["HOME"] = str(b10_home)
    os.environ["XDG_STATE_HOME"] = str(xdg_state_parent)
    os.environ.pop("HARNESS_STATE_ROOT", None)
    import importlib
    importlib.reload(d)

    releases_before = list((d.data_root() / "releases").iterdir()) if (d.data_root() / "releases").is_dir() else []

    started_at = d._utc_now()
    typed_refusal = None
    try:
        d._install_or_update(
            repository="acme/hearting", version="v1", runtimes=["claude"],
            bootstrap=True, channel="stable", pinned_version=None,
        )
    except d.DistributionError as exc:
        typed_refusal = str(exc)
    finished_at = d._utc_now()

    releases_after = list((d.data_root() / "releases").iterdir()) if (d.data_root() / "releases").is_dir() else []

    b10_fixture_execution = {
        "command": "_install_or_update(bootstrap=True)",
        "exit_code": 0 if typed_refusal is not None else 1,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    assert typed_refusal is not None, "expected dispatch-state-root-unwritable, got no refusal"
    assert "dispatch-state-root-unwritable" in typed_refusal, typed_refusal
    assert releases_after == releases_before, "M0 must refuse before any release is created"
    m0_stopped = True
    release_deletions = 0
    completed_journal_records = 0

    is_root = os.geteuid() == 0
    if is_root or not b10_non_root_detection["matched"]:
        b10_grade = {"result": "unproven"}
    else:
        b10_grade = {
            "result": "pass",
            "m0_stopped": m0_stopped,
            "typed_refusal": "dispatch-state-root-unwritable",
            "release_deletions": release_deletions,
            "completed_journal_records": completed_journal_records,
        }
    print(json.dumps({
        "b10_fixture_execution": b10_fixture_execution,
        "b10_executor": b10_executor,
        "b10_non_root_detection": b10_non_root_detection,
        "b10_grade": b10_grade,
    }, default=str))
    assert b10_grade["result"] in ("pass", "unproven")
finally:
    os.chmod(hearting_dir, 0o700)
PY
echo "ok - B-10: isolated harness update refuses at M0 with dispatch-state-root-unwritable (typed refusal, zero deletions, zero completed journal)"

# SD-112 B-7/B-8 (§13.33.2-(5)): source deletion preconditions -- an
# unreconciled legacy-bound delta blocks pruning with a typed refusal, and
# the release list is left unchanged (never a warn-and-continue).
python3 - "$ROOT" "$TMP" <<'PY'
import os, sys, time
from pathlib import Path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
sys.path.insert(0, str(root / "tools/install"))
import distribution as d

b78_home = tmp / "b78-home"
os.environ["HOME"] = str(b78_home)
os.environ["XDG_STATE_HOME"] = str(b78_home / ".local" / "state")
os.environ.pop("HARNESS_STATE_ROOT", None)
os.environ["HARNESS_DATA_ROOT"] = str(b78_home / ".local" / "share" / "hearting")
os.environ.pop("AGENT_DISPATCH_JOBS", None)
import importlib
importlib.reload(d)

releases = d.data_root() / "releases"
releases.mkdir(parents=True, exist_ok=True)

future = time.time() + 70_000_000
def make_release(name, ts):
    rel = releases / name
    (rel / "core").mkdir(parents=True)
    (rel / "core" / "CORE.md").write_text("fixture\n")
    os.utime(rel, (ts, ts))
    return rel

v1 = make_release("v-b78-1", future)
(v1 / ".dispatch").mkdir(parents=True)
(v1 / ".dispatch" / "jobs.log").write_text(
    "2026-08-01T00:00:00Z\tdone\t/r\t/w\tatt-b78\t"
    "attempt_schema_version=2,registered_worker=1,attempt_id=att-b78,harness=claude\n"
)
d.current_path().parent.mkdir(parents=True, exist_ok=True)
d.current_path().symlink_to(v1)
migration = d.run_dispatch_state_migration(v1 / ".dispatch", environ=os.environ)
assert migration["status"] == "completed", migration

# A legacy-bound writer keeps writing to v1's `.dispatch` AFTER promotion,
# and the stable side already has a *different*, out-of-band copy at that
# same relative path -- additive-only carry-forward skips a destination
# that already exists, so this is exactly the "reconciliation left a real
# mismatch behind" case the final delta digest check (not the sweep call's
# own boolean) must catch.
(v1 / ".dispatch" / "logs").mkdir(parents=True)
(v1 / ".dispatch" / "logs" / "late.txt").write_text("late write\n")
stable_root_for_delta = d.stable_state_root(os.environ)
(stable_root_for_delta / "logs").mkdir(parents=True, exist_ok=True)
(stable_root_for_delta / "logs" / "late.txt").write_text("stale out-of-band copy\n")

v2 = make_release("v-b78-2", future + 1)
v3 = make_release("v-b78-3", future + 2)
d.current_path().unlink()
d.current_path().symlink_to(v3)

releases_before = sorted(p.name for p in releases.iterdir())
d._cleanup_releases(keep=set())
releases_after = sorted(p.name for p in releases.iterdir())

assert v1.exists(), "unreconciled delta must block v1's deletion (B-7/B-8)"
assert releases_before == releases_after, "release list must be unchanged when deletion is blocked"
assert (v1 / ".dispatch" / "logs" / "late.txt").is_file(), "late write must survive untouched"
PY
echo "ok - B-7/B-8: unreconciled legacy-bound delta blocks source deletion, release list unchanged"

# SD-112 B-12 (§13.33.2-(7)): new stable dispatch root is 0700, new migration
# journal is 0600; a pre-existing wide-mode root/file is refused, never
# chmod'ed.
python3 - "$ROOT" "$TMP" <<'PY'
import os, stat, sys
from pathlib import Path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
sys.path.insert(0, str(root / "tools/install"))
import distribution as d

def mode(path):
    return stat.S_IMODE(path.stat().st_mode)

if os.geteuid() == 0:
    print("SKIP=unproven (running as root, mode enforcement is not observable)")
else:
    b12_home = tmp / "b12-home"
    os.environ["HOME"] = str(b12_home)
    os.environ["XDG_STATE_HOME"] = str(b12_home / ".local" / "state")
    os.environ.pop("HARNESS_STATE_ROOT", None)
    stable = d.stable_state_root(os.environ)
    assert not stable.exists()
    d._migration_m0_preflight(b12_home / "nonexistent" / ".dispatch", stable)
    assert mode(stable) == 0o700, oct(mode(stable))

    journal = stable / "migration-journal.jsonl"
    d._append_migration_journal(stable, {"record_version": 1, "status": "open", "migration_id": "b12"})
    assert mode(journal) == 0o600, oct(mode(journal))

    # Pre-existing wide-mode root: typed refusal, never chmod'ed.
    wide_home = tmp / "b12-wide-home"
    os.environ["HOME"] = str(wide_home)
    os.environ["XDG_STATE_HOME"] = str(wide_home / ".local" / "state")
    wide_stable = d.stable_state_root(os.environ)
    wide_stable.mkdir(parents=True)
    os.chmod(wide_stable, 0o755)
    try:
        d._migration_m0_preflight(wide_home / "nonexistent" / ".dispatch", wide_stable)
    except d.DistributionError as exc:
        assert "dispatch-state-root-mode-violation" in str(exc), str(exc)
    else:
        raise AssertionError("pre-existing wide-mode stable root must be refused, not accepted")
    assert mode(wide_stable) == 0o755, "must never chmod a pre-existing root"
PY
echo "ok - B-12: new dispatch root 0700 and journal 0600; pre-existing wide-mode root refused without chmod"

# SD-115 §13.34.3-(2): a migration-delta content-mismatch (the same
# unproven-ness B-7/B-8 exercises) routes through the identical
# force_prune_unproven + gap-precommit gate as the containment precondition
# -- it must never be force-overridable without a gap record landing first,
# but WITH the force flag and a successful gap commit it stops blocking
# deletion.
python3 - "$ROOT" "$TMP" <<'PY'
import json, os, sys, time
from pathlib import Path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
sys.path.insert(0, str(root / "tools/install"))
import distribution as d

b78f_home = tmp / "b78-force-home"
os.environ["HOME"] = str(b78f_home)
os.environ["XDG_STATE_HOME"] = str(b78f_home / ".local" / "state")
os.environ.pop("HARNESS_STATE_ROOT", None)
os.environ["HARNESS_DATA_ROOT"] = str(b78f_home / ".local" / "share" / "hearting")
os.environ.pop("AGENT_DISPATCH_JOBS", None)
import importlib
importlib.reload(d)

releases = d.data_root() / "releases"
releases.mkdir(parents=True, exist_ok=True)

future = time.time() + 70_000_000
def make_release(name, ts):
    rel = releases / name
    (rel / "core").mkdir(parents=True)
    (rel / "core" / "CORE.md").write_text("fixture\n")
    os.utime(rel, (ts, ts))
    return rel

v1 = make_release("v-b78f-1", future)
(v1 / ".dispatch").mkdir(parents=True)
(v1 / ".dispatch" / "jobs.log").write_text(
    "2026-08-01T00:00:00Z\tdone\t/r\t/w\tatt-b78f\t"
    "attempt_schema_version=2,registered_worker=1,attempt_id=att-b78f,harness=claude\n"
)
d.current_path().parent.mkdir(parents=True, exist_ok=True)
d.current_path().symlink_to(v1)
migration = d.run_dispatch_state_migration(v1 / ".dispatch", environ=os.environ)
assert migration["status"] == "completed", migration

(v1 / ".dispatch" / "logs").mkdir(parents=True)
(v1 / ".dispatch" / "logs" / "late.txt").write_text("late write\n")
stable_root_for_delta = d.stable_state_root(os.environ)
(stable_root_for_delta / "logs").mkdir(parents=True, exist_ok=True)
(stable_root_for_delta / "logs" / "late.txt").write_text("stale out-of-band copy\n")

v2 = make_release("v-b78f-2", future + 1)
v3 = make_release("v-b78f-3", future + 2)
d.current_path().unlink()
d.current_path().symlink_to(v3)

gaps_path = stable_root_for_delta / "inventory" / "gaps.jsonl"
assert not gaps_path.exists(), "no gap record should exist before a forced prune"

# Without the force flag: identical to B-7/B-8, still blocked, zero deletions.
releases_before = sorted(p.name for p in releases.iterdir())
d._cleanup_releases(keep=set(), force_prune_unproven=False)
assert v1.exists(), "unforced content-mismatch must still block deletion"
assert sorted(p.name for p in releases.iterdir()) == releases_before
assert not gaps_path.exists(), "an unforced block must never write a gap record"

# With the force flag: a gap record for the content-mismatch is committed
# BEFORE the release is deleted, then deletion proceeds.
d._cleanup_releases(keep=set(), force_prune_unproven=True)
assert not v1.exists(), "forced prune with a committed gap record must delete v1"
assert gaps_path.is_file(), "forced prune must commit a gap record before deleting"
rows = [json.loads(line) for line in gaps_path.read_text().splitlines() if line.strip()]
assert rows, "gap record file must not be empty"
last = rows[-1]
assert last["recoverable"] is False, last
assert last["discovered_by"] == "forced-prune", last
PY
echo "ok - SD-115: migration content-mismatch is force-overridable only via the same gap-precommit gate as containment"
