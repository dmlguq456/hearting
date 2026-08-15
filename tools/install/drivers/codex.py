"""Codex channel driver.

Reuse the generated Codex projections and marketplace bundle. Plugin install is
optional and never replaces symlinks for custom agents, prompts, configuration,
or the bootstrap surface.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import paths
import projector
import manifest
import verifier
import codex_launcher
import user_model_config

RUNTIME = "codex"

_MARKETPLACE_SOURCE_RELPATH = "codex_setting/codex-plugin-marketplace"
_MARKETPLACE_NAME = "hearting"
_PLUGIN_SPEC = f"hearting-codex@{_MARKETPLACE_NAME}"


def _plugin_action(dry_run):
    """Phase 7 Step 7.1 — `codex plugin marketplace add`/`plugin add` wrapping.

    The marketplace source is ``codex_setting/codex-plugin-marketplace``. Skip
    without launching a subprocess when the CLI is absent.
    """
    marketplace_source = str(paths.resolve_source(_MARKETPLACE_SOURCE_RELPATH))
    marketplace_cmd = ["codex", "plugin", "marketplace", "add", marketplace_source, "--json"]
    plugin_cmd = ["codex", "plugin", "add", _PLUGIN_SPEC, "--json"]

    if dry_run:
        return {
            "action": "plugin",
            "status": "planned",
            "detail": f"dry-run: {' '.join(marketplace_cmd)} ; {' '.join(plugin_cmd)}",
        }

    if shutil.which("codex") is None:
        return {
            "action": "plugin",
            "status": "skipped",
            "detail": "SKIP(codex): plugin channel wrapping — codex CLI absent",
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
        return {"action": "plugin", "status": "blocked", "detail": f"plugin add failed: {exc}"}

    if plugin_result.returncode != 0:
        return {
            "action": "plugin",
            "status": "blocked",
            "detail": f"plugin add exit={plugin_result.returncode} stderr={plugin_result.stderr[:300]!r}",
        }

    return {
        "action": "plugin",
        "status": "registered",
        "detail": f"marketplace + plugin add OK: {_PLUGIN_SPEC}",
    }


def install(scope="global", plugin=False, dry_run=False):
    """Apply the symlink projection and optional plugin wrapper."""
    entries = projector.plan(["codex"], scope=scope)["codex"]

    actions = []

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
                    already_linked = Path(os.readlink(dest)) == source
                except OSError:
                    already_linked = False

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
                        "detail": (
                            f"dest is a real file/dir, refusing to overwrite: {dest} "
                            "(e.g. a pre-existing vanilla codex AGENTS.md — surface, don't clobber)"
                        ),
                    }
                )
                continue

            if dest.is_symlink():
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

    if plugin:
        actions.append(_plugin_action(dry_run))
        try:
            actions.append(codex_launcher.install(dry_run=dry_run))
        except codex_launcher.CodexUnavailableError as exc:
            actions.append(
                {
                    "action": "managed-launcher",
                    "status": "skipped-unavailable",
                    "detail": str(exc),
                }
            )
        except codex_launcher.CodexLauncherError as exc:
            actions.append(
                {
                    "action": "managed-launcher",
                    "status": "blocked",
                    "detail": str(exc),
                }
            )

    manifest_result = None
    if not dry_run:
        manifest_result = manifest.record("codex", [], scope=scope)

    return {
        "runtime": "codex",
        "actions": actions,
        "blocked": any(a.get("status") == "blocked" for a in actions),
        "manifest": manifest_result,
    }


def checks(scope="global"):
    """Return core-projection, preflight, and bootstrap checks."""
    entries = projector.plan(["codex"], scope=scope)["codex"]
    agent_home = str(paths.agent_home())

    check_list = []

    for entry in entries:
        if entry["action"] == "symlink":
            dest = entry["dest"]
            dest_path = Path(dest)
            check_id = f"codex.symlink.{dest_path.parent.name}.{dest_path.name}"
            check_list.append(verifier.check_symlink(check_id, dest, entry["source"]))
        elif entry["action"] == "seed_once":
            check_list.append(
                user_model_config.verification_check(
                    "codex.file.agent-config.models.conf", entry["dest"]
                )
            )

    check_list.append(
        verifier.check_cmd(
            "codex.generated-projections",
            ["python3", "tools/generate.py", "--check"],
            cwd=agent_home,
        )
    )

    check_list.append(
        verifier.check_cmd(
            "codex.compile-smoke",
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

    check_list.append(
        verifier.check_cmd(
            "codex.preflight.capability-info",
            ["adapters/codex/bin/preflight.sh", "capability-info", "autopilot-code"],
            must_match=[r"^native_skill_path="],
            cwd=agent_home,
        )
    )
    check_list.append(
        verifier.check_cmd(
            "codex.preflight.role",
            ["adapters/codex/bin/preflight.sh", "role", "fast", "reviewer"],
            must_match=[r"^adapter=codex$"],
            cwd=agent_home,
        )
    )
    def _bootstrap_smoke():
        if shutil.which("codex") is None:
            return {
                "id": "codex.bootstrap-smoke",
                "ok": True,
                "detail": "SKIP(codex): bootstrap smoke — codex CLI absent",
            }

        tmp_home = tempfile.mkdtemp(prefix="codex-bootstrap-smoke-")
        try:
            source = paths.resolve_source("codex_setting/AGENTS.md")
            dest = Path(tmp_home) / "AGENTS.md"
            dest.symlink_to(source)

            try:
                result = subprocess.run(
                    ["codex", "debug", "prompt-input", "bootstrap check"],
                    capture_output=True,
                    text=True,
                    env={**os.environ, "CODEX_HOME": tmp_home},
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                return {
                    "id": "codex.bootstrap-smoke",
                    "ok": False,
                    "detail": "timeout: codex debug prompt-input",
                }

            if result.returncode != 0:
                return {
                    "id": "codex.bootstrap-smoke",
                    "ok": False,
                    "detail": f"exit={result.returncode} stderr={result.stderr[:300]!r}",
                }

            marker = "AGENTS.md — Codex Adapter Bootstrap"
            if marker not in result.stdout:
                return {
                    "id": "codex.bootstrap-smoke",
                    "ok": False,
                    "detail": f"marker not found: {marker!r} (stdout excerpt: {result.stdout[:300]!r})",
                }

            return {
                "id": "codex.bootstrap-smoke",
                "ok": True,
                "detail": "OK: AGENTS.md bootstrap marker found via codex debug prompt-input",
            }
        finally:
            shutil.rmtree(tmp_home, ignore_errors=True)

    check_list.append(_bootstrap_smoke)

    return check_list


def status(scope="global"):
    """Summarize channel, version, drift, pointer, and marketplace state."""
    manifest_data = manifest._load_manifest(manifest._manifest_path("codex", scope))
    drift = manifest.check_drift(["codex"], scope=scope)

    if manifest_data is None:
        version = "not-installed"
    else:
        version = manifest_data.get("version", "none")

    pointer_present = (paths.runtime_home("codex", scope) / "hearting").is_symlink()
    plugin_marketplace_source_present = paths.resolve_source(
        "adapters/codex/plugin-marketplace"
    ).exists()

    return {
        "channel": "dev",
        "version": version,
        "drift_count": len(drift),
        "pointer_present": pointer_present,
        "plugin_marketplace_source_present": plugin_marketplace_source_present,
        "plugin_registered": None,  # Phase 7 TODO — needs `codex plugin marketplace list`
    }
