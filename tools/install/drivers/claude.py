"""Claude Code channel driver.

Claude is the native runtime for the root source tree. Its dev channel uses
symlink projection plus copy-once settings, while the self-contained plugin
bundle is generated separately. Plugin installation never replaces the dev
projection because settings, status, memory, and bootstrap surfaces cannot all
be carried by the plugin.
"""

import os
import shutil
import subprocess
from pathlib import Path

import paths
import projector
import manifest
import verifier
import user_model_config

RUNTIME = "claude"

_MARKETPLACE_SOURCE_RELPATH = "adapters/claude/plugin-marketplace"
_MARKETPLACE_NAME = "hearting"
_PLUGIN_SPEC = f"hearting-claude@{_MARKETPLACE_NAME}"


def _dev_channel_active(scope="global"):
    """Return whether the dev-channel bootstrap symlink points into agent_home."""
    claude_md = paths.runtime_home(RUNTIME, scope) / "CLAUDE.md"
    if not claude_md.is_symlink():
        return False
    try:
        resolved = claude_md.resolve()
        home = paths.agent_home().resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(home)
        return True
    except ValueError:
        return False


def _plugin_action(dry_run):
    """Phase 2 Step 2.1 — `claude plugin marketplace add`/`plugin install` wrapping.

    Claude's CLI does not accept Codex's ``--json`` flag for these commands.
    The marketplace source is the adapter path because Claude is the native
    source runtime.
    """
    marketplace_source = str(paths.resolve_source(_MARKETPLACE_SOURCE_RELPATH))
    marketplace_cmd = ["claude", "plugin", "marketplace", "add", marketplace_source]
    plugin_cmd = ["claude", "plugin", "install", _PLUGIN_SPEC]

    if dry_run:
        return {
            "action": "plugin",
            "status": "planned",
            "detail": f"dry-run: {' '.join(marketplace_cmd)} ; {' '.join(plugin_cmd)}",
        }

    if shutil.which("claude") is None:
        return {
            "action": "plugin",
            "status": "skipped",
            "detail": "SKIP(claude): plugin channel wrapping — claude CLI absent",
        }

    try:
        mp_result = subprocess.run(marketplace_cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"action": "plugin", "status": "blocked", "detail": f"marketplace add failed: {exc}"}

    if mp_result.returncode != 0:
        return {
            "action": "plugin",
            "status": "blocked",
            "detail": f"marketplace add exit={mp_result.returncode} stderr={mp_result.stderr[:300]!r}",
        }

    try:
        plugin_result = subprocess.run(plugin_cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"action": "plugin", "status": "blocked", "detail": f"plugin install failed: {exc}"}

    if plugin_result.returncode != 0:
        return {
            "action": "plugin",
            "status": "blocked",
            "detail": f"plugin install exit={plugin_result.returncode} stderr={plugin_result.stderr[:300]!r}",
        }

    return {
        "action": "plugin",
        "status": "registered",
        "detail": f"marketplace + plugin install OK: {_PLUGIN_SPEC}",
    }


def install(scope="global", plugin=False, dry_run=False):
    """Apply symlinks, copy-once runtime surfaces, and optional plugin wrapping."""
    entries = projector.plan(["claude"], scope=scope)["claude"]

    actions = []

    if plugin:
        actions.append(_plugin_action(dry_run))

    copy_once_files = []

    for entry in entries:
        action = entry["action"]

        if action == "skip":
            actions.append(
                {
                    "action": "skip",
                    "dest": entry["dest"],
                    "status": "skipped",
                    "detail": entry["reason"],
                }
            )
            continue

        if action == "symlink":
            source = Path(entry["source"])
            dest = Path(entry["dest"])

            if dry_run:
                actions.append(
                    {
                        "action": "symlink",
                        "source": str(source),
                        "dest": str(dest),
                        "status": "planned",
                        "detail": "dry-run",
                    }
                )
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)

            already_linked = False
            if dest.is_symlink():
                try:
                    already_linked = dest.resolve() == source.resolve()
                except OSError:
                    already_linked = os.readlink(dest) == str(source)

            if already_linked:
                actions.append(
                    {
                        "action": "symlink",
                        "source": str(source),
                        "dest": str(dest),
                        "status": "unchanged",
                        "detail": "already linked",
                    }
                )
                continue

            if dest.exists() and not dest.is_symlink():
                actions.append(
                    {
                        "action": "symlink",
                        "source": str(source),
                        "dest": str(dest),
                        "status": "blocked",
                        "detail": f"dest is a real file/directory, refusing to overwrite: {dest}",
                    }
                )
                continue

            if dest.is_symlink() or dest.exists():
                dest.unlink()

            dest.symlink_to(source, target_is_directory=source.is_dir())
            actions.append(
                {
                    "action": "symlink",
                    "source": str(source),
                    "dest": str(dest),
                    "status": "created",
                    "detail": "symlink created",
                }
            )
            continue

        if action == "copy_once":
            source = Path(entry["source"])
            dest = Path(entry["dest"])

            if dry_run:
                actions.append(
                    {
                        "action": "copy_once",
                        "source": str(source),
                        "dest": str(dest),
                        "status": "planned",
                        "detail": "dry-run",
                    }
                )
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)

            if dest.exists():
                status = "unchanged"
            else:
                shutil.copyfile(source, dest)
                status = "created"

            actions.append(
                {
                    "action": "copy_once",
                    "source": str(source),
                    "dest": str(dest),
                    "status": status,
                    "detail": "never re-copy over an existing runtime-owned copy",
                }
            )
            copy_once_files.append(
                {"relpath": dest.name, "source_abs": str(source), "dest_abs": str(dest)}
            )
            continue

        if action == "seed_once":
            try:
                actions.append(
                    user_model_config.seed_model_config(
                        RUNTIME,
                        entry["source"],
                        paths.runtime_home(RUNTIME, scope),
                        dry_run=dry_run,
                    )
                )
            except user_model_config.UserModelConfigError as exc:
                actions.append(
                    {
                        "action": "seed_once",
                        "source": entry["source"],
                        "dest": entry["dest"],
                        "status": "blocked",
                        "detail": str(exc),
                    }
                )
            continue

        if action == "delegate":
            is_windows = os.name == "nt"
            if not is_windows or dry_run:
                reason = "not Windows" if not is_windows else "dry-run"
                actions.append(
                    {
                        "action": "delegate",
                        "cmd": entry["cmd"],
                        "status": "planned" if dry_run and is_windows else "skipped",
                        "detail": f"install-windows.sh delegate skipped ({reason})",
                    }
                )
                continue

            subprocess.run(entry["cmd"], cwd=str(paths.agent_home()))
            actions.append(
                {
                    "action": "delegate",
                    "cmd": entry["cmd"],
                    "status": "delegated",
                    "detail": "install-windows.sh executed",
                }
            )
            continue

    blocked = any(a.get("status") == "blocked" for a in actions)

    manifest_result = None
    if not dry_run and not blocked and copy_once_files:
        manifest_result = manifest.record("claude", copy_once_files, scope=scope)

    return {
        "runtime": "claude",
        "actions": actions,
        "blocked": blocked,
        "manifest": manifest_result,
    }


def checks(scope="global"):
    """Return native-projection and bootstrap checks for ``verify``.

    The marketplace bundle is an explicit ``install --plugin`` channel and is
    not a core verification requirement.
    """
    entries = projector.plan(["claude"], scope=scope)["claude"]

    check_list = []

    for entry in entries:
        action = entry["action"]
        if action == "symlink":
            dest = entry["dest"]
            name = Path(dest).name
            check_list.append(
                verifier.check_symlink(f"claude.symlink.{name}", dest, entry["source"])
            )
        elif action == "copy_once":
            dest = entry["dest"]
            name = Path(dest).name
            check_list.append(verifier.check_file_exists(f"claude.file.{name}", dest))
        elif action == "seed_once":
            check_list.append(
                user_model_config.verification_check(
                    "claude.file.agent-config.models.conf", entry["dest"]
                )
            )
        # Skip and delegate entries are not read-only verification targets.

    agent_home = str(paths.agent_home())

    check_list.append(
        verifier.check_cmd(
            "claude.generated-projections",
            ["python3", "tools/generate.py", "--check"],
            cwd=agent_home,
        )
    )

    check_list.append(
        verifier.check_cmd(
            "claude.compile-smoke",
            [
                "python3",
                "-c",
                "import sys; [compile(open(f,encoding='utf-8').read(), f, 'exec') for f in sys.argv[1:]]",
                "tools/build-manifest.py",
                "tools/memory/mem.py",
                "tools/memory/protocol_v2.py",
                "tools/memory/git_exchange_v2.py",
                "tools/memory/sync_v2.py",
            ],
            cwd=agent_home,
        )
    )

    def _bootstrap_smoke():
        if shutil.which("claude") is None:
            return {
                "id": "claude.bootstrap-smoke",
                "ok": True,
                "detail": "SKIP(claude): bootstrap smoke — claude CLI absent",
            }
        return {
            "id": "claude.bootstrap-smoke",
            "ok": True,
            "detail": "claude CLI present (no scripted bootstrap-smoke contract yet)",
        }

    check_list.append(_bootstrap_smoke)

    return check_list


def status(scope="global"):
    """Summarize channel, version, and drift state."""
    manifest_data = manifest._load_manifest(manifest._manifest_path("claude", scope))
    drift = manifest.check_drift(["claude"], scope=scope)
    dev_channel_active = _dev_channel_active(scope)

    if manifest_data is None:
        return {
            "channel": "dev",
            "version": "not-installed",
            "file_count": 0,
            "drift_count": len(drift),
            "dev_channel_active": dev_channel_active,
        }

    return {
        "channel": "dev",
        "version": manifest_data.get("version", "none"),
        "file_count": len(manifest_data.get("files", {})),
        "drift_count": len(drift),
        "dev_channel_active": dev_channel_active,
    }
