"""OpenCode channel driver.

Reuse generated native projections and preflight checks. OpenCode has no
marketplace bundle channel, so the installer is the only installation path.
Merge harness entries into ``opencode.json`` without overwriting user config;
report incompatible types instead of resolving them automatically.
"""

import hashlib
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
import safe_fs
import herdr_integration

RUNTIME = "opencode"


def _guard_check(scope):
    import runtime_activation
    home = paths.runtime_home(RUNTIME, scope)
    state = projector.opencode_activation(home)
    layout = projector.opencode_guard_layout(Path(state.get("active_root") or paths.agent_home()), home, scope)
    issues = projector.opencode_guard_health(layout) + runtime_activation.duplicate_sources(RUNTIME, scope)
    return {"id": "opencode.guard-discovery", "ok": not issues,
            "detail": "; ".join([layout["detail"], *issues])}


def _install_guard(entry, home, dry_run, scope="global"):
    dest, source = Path(entry["dest"]), Path(entry["source"])
    result = {"action": entry["action"], "dest": str(dest), "source": str(source),
              "detail": entry.get("detail", "guard projection")}
    if entry.get("preserve"):
        return dict(result, status="preserved")
    current = safe_fs.capture_state(dest)
    owned = projector.opencode_guard_owned(entry, home, scope)
    same_link = entry["action"] == "symlink" and current.kind == "symlink" and os.readlink(dest) == str(source)
    if current.kind != "missing" and not owned and not same_link:
        return dict(result, status="blocked", detail=f"foreign or user-changed guard preserved: {dest}")
    if (not entry.get("source_present") or not source.is_file()
            or any(not Path(value).is_file() for value in entry.get("dependencies", []))):
        return dict(result, status="blocked", detail=f"required guard source missing: {source}")
    if dry_run:
        return dict(result, status="planned")
    try:
        auth = safe_fs.authority(dest, owner="opencode-driver:guard-projection",
                                 allowed_paths=(dest,), allow_leaf_symlink=True, expected=current)
        if entry["action"] == "render":
            content = entry["content"].encode("utf-8")
            unchanged = current.kind == "file" and dest.read_bytes() == content
            relative = dest.relative_to(home).as_posix()
            receipt_paths = {"entry": auth}
            for name, path in (("manifest", manifest._manifest_path(RUNTIME, scope)),
                               ("pristine", manifest._pristine_path(RUNTIME, scope, relative))):
                receipt_paths[name] = safe_fs.authority(path, owner="opencode-driver:guard-receipt",
                                                       allowed_paths=(path,), allow_leaf_symlink=False,
                                                       expected=safe_fs.capture_state(path))
            with safe_fs.transaction(receipt_paths) as transaction:
                transaction.seal(postimages=transaction.preimages)
                try:
                    # The ownership decision predates lock acquisition.
                    for receipt_auth in receipt_paths.values():
                        safe_fs.assert_expected(receipt_auth)
                    if not unchanged:
                        transaction.write_bytes("entry", content, 0o644)
                    # Persist authority even when an unrelated install action
                    # fails later. Never register user-modified bytes.
                    manifest.record(RUNTIME, [{"relpath": relative, "source_abs": str(dest),
                                               "dest_abs": str(dest),
                                               "expected_state": transaction.postimages["entry"],
                                               "content_sha256": entry["content_sha256"]}],
                                    scope=scope, transaction=transaction)
                except Exception:
                    transaction.restore()
                    raise
        else:
            unchanged = same_link
            if not unchanged:
                safe_fs.atomic_write_symlink(auth, str(source), create_parents=True)
        return dict(result, status="unchanged" if unchanged else "created")
    except (OSError, ValueError, safe_fs.SafetyError) as exc:
        return dict(result, status="blocked", detail=str(exc))


def _config_path(scope):
    """Use ``runtime_home / opencode.json`` consistently for both scopes."""
    return paths.runtime_home("opencode", scope) / "opencode.json"


def _instructions_path():
    return str(paths.resolve_source("opencode_setting/AGENTS.md"))


def _skills_path():
    return str(paths.resolve_source("opencode_setting/opencode-skills"))


def _digest(path):
    """Return a file's SHA-256, or ``None`` when it cannot be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _auto_loaded_bootstrap():
    """Return the config-home ``AGENTS.md`` when it already carries our bootstrap.

    OpenCode auto-loads exactly one instruction file by name: ``AGENTS.md`` in
    the *global* config home, regardless of install scope. ``runtime activate``
    owns that link, so when it is present the bootstrap is already delivered and
    an ``instructions[]`` entry would deliver it a second time — OpenCode dedupes
    instruction sources by resolved path, not by inode or content, so the same
    file reached through two absolute paths is injected twice.

    Return ``None`` when that link is absent; the config entry is then the only
    load path and must stay. The symlink may point at an immutable snapshot
    whose bytes trail this checkout, so the target path counts as well.
    """
    path = paths.xdg_config_home() / "opencode" / "AGENTS.md"
    if not path.exists():
        return None
    if path.is_symlink() and os.path.realpath(path).endswith(
        "/adapters/opencode/AGENTS.md"
    ):
        return path
    if _digest(path) == _digest(_instructions_path()):
        return path
    return None


def _merge_config(existing, our_instructions, our_skills_path, *, v2=False):
    """Merge harness skill paths, and the bootstrap only when nothing else loads it.

    Return ``(merged, changed, blocked, detail)``. Create missing keys, append
    missing values to valid lists, and block on incompatible existing types.
    ``our_instructions=None`` means the bootstrap already arrives by auto-load; a
    previously installed entry is then removed so it stops double-loading.
    """
    merged = dict(existing)
    changed = False

    # instructions[]
    if our_instructions is None:
        instructions = merged.get("instructions")
        if isinstance(instructions, list) and _instructions_path() in instructions:
            remaining = [
                entry for entry in instructions if entry != _instructions_path()
            ]
            if remaining:
                merged["instructions"] = remaining
            else:
                merged.pop("instructions")
            changed = True
    elif "instructions" not in merged:
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

    # V1 uses skills.paths. V2 accepts a list of skill locations and
    # normalizes the legacy object during migration; emit the native V2 list
    # while preserving every existing path.
    if v2:
        skills = merged.get("skills", [])
        if isinstance(skills, dict) and set(skills) <= {"paths"}:
            skills = skills.get("paths", [])
        if not isinstance(skills, list):
            return (existing, False, True,
                    "opencode.json 'skills' value has an incompatible V2 type — refusing to merge")
        skills = list(skills)
        if our_skills_path not in skills:
            skills.append(our_skills_path)
            changed = True
        merged["skills"] = skills
        return (merged, changed, False, "merged" if changed else "unchanged")

    # V1 skills.paths
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
    home = paths.runtime_home(RUNTIME, scope)
    state = projector.opencode_activation(home)
    layout = projector.opencode_guard_layout(Path(state.get("active_root") or paths.agent_home()), home, scope)
    v2 = layout["generation"] == "v2"
    if layout["issues"]:
        actions.append({"action": "guard-check", "status": "blocked", "detail": "; ".join(layout["issues"])})
        blocked = True
    if state:
        # Keep the active immutable binding and let its existing transaction
        # retire old carriers. The legacy writer never unlinks an obsolete JS.
        guard_entries = [entry for entry in entries if entry.get("guard")]
        prior = {item.get("dest") for item in state.get("owned_paths", [])
                 if projector.opencode_guard_leaf(Path(item.get("dest", "")), home)}
        wanted = {entry["dest"] for entry in guard_entries}
        needs_refresh = prior != wanted or not _guard_check(scope)["ok"]
        if needs_refresh:
            if dry_run:
                actions.append({"action": "guard-refresh", "status": "planned",
                                "detail": "refresh through the existing activation transaction"})
            else:
                import runtime_activation
                try:
                    runtime_activation.activate(RUNTIME, state["mode"], state["source_root"], scope)
                    actions.append({"action": "guard-refresh", "status": "refreshed"})
                except (OSError, ValueError, runtime_activation.ActivationError) as exc:
                    actions.append({"action": "guard-refresh", "status": "blocked", "detail": str(exc)})
                    blocked = True

    for entry in entries:
        action = entry["action"]

        if entry.get("guard"):
            if not state:
                actions.append(_install_guard(entry, home, dry_run, scope))
            continue

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

        if action == "copy_managed":
            projected = herdr_integration.project_asset(
                Path(entry["source"]), Path(entry["dest"]), entry["integration_id"],
                dry_run=dry_run, allowed_root=paths.runtime_home(RUNTIME, scope))
            actions.append(projected)
            blocked = blocked or projected.get("status") == "blocked"
            continue

        if action == "symlink":
            source = Path(entry["source"])
            dest = Path(entry["dest"])

            preserved = (
                projector.native_leaf_preservation(RUNTIME, source, dest)
                if entry.get("native_leaf") else None
            )
            if preserved:
                actions.append(
                    {
                        "action": "symlink",
                        "source": str(source),
                        "dest": str(dest),
                        "status": "preserved",
                        "detail": preserved,
                    }
                )
                continue

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

            try:
                current = safe_fs.capture_state(dest)
                auth = safe_fs.authority(
                    dest,
                    owner="opencode-driver:projector-plan",
                    allowed_paths=(dest,),
                    expected=current,
                )
                safe_fs.atomic_write_symlink(
                    auth,
                    str(source),
                    create_parents=True,
                    target_is_directory=source.is_dir(),
                )
            except safe_fs.SafetyError as exc:
                actions.append(
                    {
                        "action": "symlink",
                        "source": str(source),
                        "dest": str(dest),
                        "status": "blocked",
                        "detail": str(exc),
                    }
                )
                continue
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
            auto_loaded = _auto_loaded_bootstrap()
            our_instructions = None if auto_loaded else _instructions_path()
            our_skills_path = _skills_path()

            if dry_run:
                planned = (
                    "would add skills.paths if missing and drop a duplicate "
                    f"instructions[] bootstrap entry ({auto_loaded} auto-loads it)"
                    if auto_loaded
                    else "would add skills.paths and instructions[] entries if missing"
                )
                actions.append(
                    {"action": "merge", "status": "planned", "detail": planned}
                )
                continue

            existing = {}
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)

            merged, changed, conflict, detail = _merge_config(
                existing, our_instructions, our_skills_path,
                # Unknown keeps an already observed config shape; a list is
                # not evidence that the current runtime generation is V2.
                v2=v2 or (layout["generation"] == "unknown" and isinstance(existing.get("skills"), list)),
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

    registration = herdr_integration.migrate_opencode_configs(
        paths.runtime_home(RUNTIME, scope), paths.agent_home(), dry_run=dry_run)
    actions.extend(registration["actions"])
    blocked = blocked or any(a.get("status") == "blocked" for a in actions)
    if not dry_run:
        guard_check = _guard_check(scope)
        if not guard_check["ok"]:
            actions.append({"action": "guard-check", "status": "blocked", "detail": guard_check["detail"]})
            blocked = True

    manifest_result = None
    integration_assets = herdr_integration.manifest_assets(
        RUNTIME, paths.agent_home(), paths.runtime_home(RUNTIME, scope))
    if not dry_run and not blocked:
        manifest_result = manifest.record("opencode", [], scope=scope,
                                          integration_assets=integration_assets)

    return {
        "runtime": "opencode",
        "actions": actions,
        "blocked": blocked,
        "manifest": manifest_result,
    }


def checks(scope="global"):
    """Return core-projection and preflight checks for ``verify``."""
    entries = projector.plan(["opencode"], scope=scope)["opencode"]
    agent_home = str(paths.agent_home())

    check_list = []

    check_list.append(verifier.check_herdr_integration(RUNTIME, agent_home))
    check_list.append(_guard_check(scope))

    for entry in entries:
        if entry.get("guard"):
            continue
        action = entry["action"]
        if action == "symlink":
            dest = entry["dest"]
            dest_path = Path(dest)
            check_id = f"opencode.symlink.{dest_path.parent.name}.{dest_path.name}"
            check_list.append(
                verifier.check_symlink(check_id, dest, entry["source"])
            )
        elif action == "copy_managed":
            dest = Path(entry["dest"])
            integration_id = entry["integration_id"]
            check_list.append({
                "id": f"opencode.herdr.installed.{integration_id}",
                "ok": herdr_integration.version_marker(
                    dest, integration_id, herdr_integration.VERSIONS[RUNTIME]),
                "detail": f"{dest} carries the current {integration_id} integration",
            })
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
            "opencode.compile-smoke",
            [
                "python3",
                "-c",
                "import sys; [compile(open(f,encoding='utf-8').read(), f, 'exec') for f in sys.argv[1:]]",
                "tools/build-manifest.py",
                "tools/memory/mem.py",
                "tools/memory/protocol_v2.py",
                "tools/memory/git_exchange_v2.py",
                "tools/memory/sync_v2.py",
                "tools/memory/migration_v2.py",
            ],
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

    def _bootstrap_load_path():
        """Exactly one copy of the bootstrap may reach an OpenCode session.

        `opencode debug config` prints only the config file, never the resolved
        instruction set, so the load path is checked from the files themselves.
        OpenCode dedupes instruction sources by resolved path, so two different
        absolute paths holding the same bootstrap bytes are injected twice.
        """
        check_id = "opencode.bootstrap-load-path"
        expected = _digest(_instructions_path())
        if expected is None:
            return {
                "id": check_id,
                "ok": False,
                "detail": f"harness bootstrap unreadable: {_instructions_path()}",
            }

        carriers = []

        auto_load = _auto_loaded_bootstrap()
        if auto_load is not None:
            carriers.append(f"config-home auto-load ({auto_load})")

        cfg_path = _config_path(scope)
        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                return {"id": check_id, "ok": False, "detail": f"{cfg_path}: {exc}"}
            entries = cfg.get("instructions", [])
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, str) or entry.startswith(
                        ("http://", "https://")
                    ):
                        continue
                    if _digest(Path(entry).expanduser()) == expected:
                        carriers.append(f"instructions[] entry ({entry})")

        if len(carriers) == 1:
            return {
                "id": check_id,
                "ok": True,
                "detail": f"bootstrap loads exactly once via {carriers[0]}",
            }
        if not carriers:
            return {
                "id": check_id,
                "ok": False,
                "detail": (
                    "no bootstrap load path — expected either the config-home "
                    "AGENTS.md auto-load link from `runtime activate`, or an "
                    "instructions[] entry from `harness install opencode`"
                ),
            }
        return {
            "id": check_id,
            "ok": False,
            "detail": (
                f"bootstrap loads {len(carriers)} times, inflating every session's "
                f"system prompt: {'; '.join(carriers)}"
            ),
        }

    check_list.append(_bootstrap_load_path)

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
            entry_present = (
                isinstance(instructions, list) and _instructions_path() in instructions
            )
            # With the auto-load link present the entry is a duplicate, not a
            # merge; without it the entry is the only bootstrap load path.
            instructions_ok = (
                not entry_present if _auto_loaded_bootstrap() else entry_present
            )
            config_merged = (
                instructions_ok
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
