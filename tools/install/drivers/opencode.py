"""OpenCode channel driver.

Reuse generated native projections and preflight checks. OpenCode has no
marketplace bundle channel, so the installer is the only installation path.
Merge harness entries into ``opencode.json`` without overwriting user config;
report incompatible types instead of resolving them automatically.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import paths
import projector
import manifest
import verifier
import user_model_config

RUNTIME = "opencode"


def _config_path(scope):
    """Use ``runtime_home / opencode.json`` consistently for both scopes."""
    return paths.runtime_home("opencode", scope) / "opencode.json"


def _instructions_path():
    return str(paths.resolve_source("opencode_setting/AGENTS.md"))


def _skills_path():
    return str(paths.resolve_source("opencode_setting/opencode-skills"))


def _merge_config(existing, our_instructions, our_skills_path):
    """Merge harness instruction and skill paths without modifying conflicts.

    Return ``(merged, changed, blocked, detail)``. Create missing keys, append
    missing values to valid lists, and block on incompatible existing types.
    """
    merged = dict(existing)
    changed = False

    # instructions[]
    if "instructions" not in merged:
        merged["instructions"] = [our_instructions]
        changed = True
    else:
        instructions = merged["instructions"]
        if not isinstance(instructions, list):
            return (
                existing,
                False,
                True,
                "opencode.json 'instructions' key exists with a non-list value — "
                "refusing to merge, manual resolution required",
            )
        if our_instructions not in instructions:
            merged["instructions"] = instructions + [our_instructions]
            changed = True

    # skills.paths
    if "skills" not in merged:
        merged["skills"] = {"paths": [our_skills_path]}
        changed = True
    else:
        skills = merged["skills"]
        if not isinstance(skills, dict):
            return (
                existing,
                False,
                True,
                "opencode.json 'skills' key exists with a non-dict value — "
                "refusing to merge, manual resolution required",
            )
        skills = dict(skills)
        if "paths" not in skills:
            skills["paths"] = [our_skills_path]
            merged["skills"] = skills
            changed = True
        else:
            skills_paths = skills["paths"]
            if not isinstance(skills_paths, list):
                return (
                    existing,
                    False,
                    True,
                    "opencode.json 'skills.paths' key exists with a non-list value — "
                    "refusing to merge, manual resolution required",
                )
            if our_skills_path not in skills_paths:
                skills = dict(skills)
                skills["paths"] = skills_paths + [our_skills_path]
                merged["skills"] = skills
                changed = True

    return (merged, changed, False, "merged" if changed else "unchanged")


def install(scope="global", plugin=False, dry_run=False):
    """Apply symlink projection and a non-destructive ``opencode.json`` merge.

    Report semantic conflicts as blocked without rolling back prior symlinks.
    """
    entries = projector.plan(["opencode"], scope=scope)["opencode"]

    actions = []
    blocked = False

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
                blocked = True
            continue

        if action == "merge":
            cfg_path = _config_path(scope)
            our_instructions = _instructions_path()
            our_skills_path = _skills_path()

            if dry_run:
                actions.append(
                    {
                        "action": "merge",
                        "status": "planned",
                        "detail": "would add instructions[]/skills.paths entries if missing",
                    }
                )
                continue

            existing = {}
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)

            merged, changed, conflict, detail = _merge_config(
                existing, our_instructions, our_skills_path
            )

            if conflict:
                actions.append(
                    {"action": "merge", "status": "blocked", "detail": detail}
                )
                blocked = True
                continue

            if changed:
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(merged, f, indent=2, ensure_ascii=False)
                actions.append(
                    {"action": "merge", "status": "merged", "detail": detail}
                )
            else:
                actions.append(
                    {"action": "merge", "status": "unchanged", "detail": detail}
                )
            continue

    blocked = blocked or any(a.get("status") == "blocked" for a in actions)

    manifest_result = None
    if not dry_run and not blocked:
        manifest_result = manifest.record("opencode", [], scope=scope)

    return {
        "runtime": "opencode",
        "actions": actions,
        "blocked": blocked,
        "manifest": manifest_result,
    }


def checks(scope="global"):
    """Return core-projection and preflight checks for ``verify``."""
    entries = projector.plan(["opencode"], scope=scope)["opencode"]

    check_list = []

    for entry in entries:
        action = entry["action"]
        if action == "symlink":
            dest = entry["dest"]
            dest_path = Path(dest)
            check_id = f"opencode.symlink.{dest_path.parent.name}.{dest_path.name}"
            check_list.append(
                verifier.check_symlink(check_id, dest, entry["source"])
            )
        elif action == "seed_once":
            check_list.append(
                user_model_config.verification_check(
                    "opencode.file.agent-config.models.conf", entry["dest"]
                )
            )
        # Skip and merge entries are not read-only verification targets.

    agent_home = str(paths.agent_home())

    check_list.append(
        verifier.check_cmd(
            "opencode.generated-projections",
            ["python3", "tools/generate.py", "--check"],
            cwd=agent_home,
        )
    )

    check_list.append(
        verifier.check_cmd(
            "opencode.preflight.capability-info",
            ["adapters/opencode/bin/preflight.sh", "capability-info", "autopilot-code"],
            must_match=[r"^native_skill_path="],
            cwd=agent_home,
        )
    )
    check_list.append(
        verifier.check_cmd(
            "opencode.preflight.role",
            ["adapters/opencode/bin/preflight.sh", "role", "fast", "reviewer"],
            must_match=[r"^adapter=opencode$"],
            cwd=agent_home,
        )
    )

    def _drift_watch_sentinel():
        """Informational INST-OPEN-4 documentation-versus-wiring drift watch."""
        version_detail = "opencode CLI absent, version unknown"
        oc_bin = shutil.which("opencode")
        if oc_bin:
            try:
                result = subprocess.run(
                    [oc_bin, "--version"], capture_output=True, text=True, timeout=10
                )
                version_detail = f"opencode --version: {result.stdout.strip() or result.stderr.strip()}"
            except Exception as exc:
                version_detail = f"opencode --version failed: {exc}"

        # Exclude plugins because the harness itself legitimately creates it.
        plural_candidates = [
            Path.home() / ".config" / "opencode" / "skills",
            Path.home() / ".config" / "opencode" / "commands",
            Path.home() / ".config" / "opencode" / "agents",
        ]
        found_plural = [str(p) for p in plural_candidates if p.is_dir()]

        if found_plural:
            detail = (
                f"{version_detail}; plural dirs (.config/opencode/skills etc) DETECTED "
                f"alongside singular wiring — review INST-OPEN-4, local opencode may have "
                f"migrated: {found_plural}"
            )
        else:
            detail = (
                f"{version_detail}; singular wiring in use (agent/, command/); plural dirs "
                f"not detected — INST-OPEN-4 still open, no action needed"
            )

        return {"id": "opencode.drift-watch", "ok": True, "detail": detail}

    check_list.append(_drift_watch_sentinel)

    def _bootstrap_smoke():
        oc_bin = shutil.which("opencode")
        if oc_bin is None:
            return {
                "id": "opencode.bootstrap-smoke",
                "ok": True,
                "detail": "SKIP(opencode): bootstrap smoke — opencode CLI absent",
            }

        import tempfile

        with tempfile.TemporaryDirectory() as tmp_home:
            config_content = json.dumps(
                {
                    "instructions": [_instructions_path()],
                    "skills": {"paths": [_skills_path()]},
                }
            )
            env = dict(os.environ)
            env["HOME"] = tmp_home
            env["XDG_CONFIG_HOME"] = str(Path(tmp_home) / ".config")
            env["XDG_DATA_HOME"] = str(Path(tmp_home) / ".local" / "share")
            env["OPENCODE_CONFIG_CONTENT"] = config_content

            try:
                result = subprocess.run(
                    [oc_bin, "debug", "config", "--pure"],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=30,
                )
            except Exception as exc:
                return {
                    "id": "opencode.bootstrap-smoke",
                    "ok": False,
                    "detail": f"opencode debug config failed: {exc}",
                }

            if "opencode_setting/AGENTS.md" in result.stdout:
                return {
                    "id": "opencode.bootstrap-smoke",
                    "ok": True,
                    "detail": "found opencode_setting/AGENTS.md in opencode debug config --pure output",
                }
            return {
                "id": "opencode.bootstrap-smoke",
                "ok": False,
                "detail": f"opencode_setting/AGENTS.md not found (stdout excerpt: {result.stdout[:300]!r})",
            }

    check_list.append(_bootstrap_smoke)

    return check_list


def status(scope="global"):
    """Summarize channel, version, drift, pointer, and config-merge state."""
    manifest_data = manifest._load_manifest(manifest._manifest_path("opencode", scope))
    drift = manifest.check_drift(["opencode"], scope=scope)

    pointer_path = paths.runtime_home("opencode", scope) / "hearting"
    pointer_present = pointer_path.is_symlink() or pointer_path.exists()

    config_merged = False
    cfg_path = _config_path(scope)
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            instructions = cfg.get("instructions", [])
            skills_paths = cfg.get("skills", {}).get("paths", [])
            config_merged = (
                isinstance(instructions, list)
                and _instructions_path() in instructions
                and isinstance(skills_paths, list)
                and _skills_path() in skills_paths
            )
        except (json.JSONDecodeError, OSError):
            config_merged = False

    if manifest_data is None:
        return {
            "channel": "dev",
            "version": "not-installed",
            "drift_count": len(drift),
            "pointer_present": pointer_present,
            "config_merged": config_merged,
        }

    return {
        "channel": "dev",
        "version": manifest_data.get("version", "none"),
        "drift_count": len(drift),
        "pointer_present": pointer_present,
        "config_merged": config_merged,
    }
