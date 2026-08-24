"""Runtime-neutral installer bootstrap helpers.

``restore_memory`` imports ``dump.jsonl`` when ``memory.db`` is absent.
``install_launchers`` creates guarded
``~/.local/bin/{hearting,harness,fleet,mem,compute-hosts}`` symlinks.
The helpers remain usable independently of installer command wiring.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path

import paths


def restore_memory(mem_store=None):
    """Restore ``memory.db`` from ``dump.jsonl`` when the database is absent.

    Args:
        mem_store: Store directory. If omitted, use ``MEM_STORE``, an existing
            legacy `paths.agent_home() / "memory"`, and then the local XDG data
            store.

    Returns:
        dict — {"action": "skipped"|"imported"|"failed", "detail": str}
    """
    if mem_store is None:
        mem_store = os.environ.get("MEM_STORE")
    if mem_store is None:
        legacy = paths.agent_home() / "memory"
        if legacy.exists() or legacy.is_symlink():
            mem_store = legacy
        else:
            data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            mem_store = data_home / "hearting" / "memory"
    mem_store = Path(mem_store)

    db_path = mem_store / "memory.db"
    dump_path = mem_store / "dump.jsonl"

    if db_path.exists():
        return {"action": "skipped", "detail": "memory.db already present"}

    if not dump_path.exists():
        return {
            "action": "skipped",
            "detail": "no dump.jsonl to restore from, and no existing memory.db",
        }

    mem_script = paths.resolve_source("tools/memory/mem.py")
    env = {**os.environ, "MEM_STORE": str(mem_store)}
    result = subprocess.run(
        ["python3", str(mem_script), "import", str(dump_path)],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return {"action": "imported", "detail": f"mem import from {dump_path}"}

    # Best effort: optional memory restoration must not fail the installation.
    return {
        "action": "failed",
        "detail": (
            f"mem import failed: exit={result.returncode} "
            f"stderr={result.stderr[:300]}"
        ),
    }


LAUNCHERS = (
    # `hearting` is the product name; `harness` predates the rename and stays
    # installed beside it so existing shells and scripts keep working.
    ("hearting", "tools/install/harness.sh"),
    ("harness", "tools/install/harness.sh"),
    ("fleet", "tools/fleet/fleet.sh"),
    ("mem", "tools/memory/mem.py"),
    ("compute-hosts", "utilities/compute-hosts"),
)


def _is_our_symlink(target, source):
    """Return whether target is a symlink resolving exactly to source."""
    if not target.is_symlink():
        return False
    try:
        return target.resolve() == source.resolve()
    except (OSError, RuntimeError):
        return False


def _symlink_destination(target):
    """Return a normalized symlink destination without requiring it to exist."""
    try:
        raw = Path(os.readlink(target))
        value = raw if raw.is_absolute() else target.parent / raw
        return value.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _managed_release_roots(home):
    """Release trees this installer itself extracted under the Hearting data home.

    F-80b: `~/.local/share/hearting/{current,releases/*}`. These are installer-owned, so a
    launcher pointing into one is ours to re-point — unlike a genuinely foreign path, which
    the collision guard must keep refusing to touch.
    """
    raw = os.environ.get("XDG_DATA_HOME")
    data_home = Path(raw) if raw and Path(raw).is_absolute() else home / ".local" / "share"
    hearting = data_home / "hearting"
    roots = [hearting / "current"]
    try:
        roots.extend(entry for entry in (hearting / "releases").iterdir() if entry.is_dir())
    except OSError:
        pass
    return roots


def _is_prior_linked_launcher(target, home, rel_source):
    """Recognize exact launchers from legacy or activated Hearting sources."""
    destination = _symlink_destination(target)
    if destination is None:
        return False
    try:
        prior = {
            (home / checkout / rel_source).resolve(strict=False)
            for checkout in ("hearting", "agent_setting")
        }
        # F-80b (user 2026-08-16 "그럼 전부 고쳐"): a launcher already pointing into a
        # managed release snapshot never self-corrected — the snapshot path was in no
        # `prior` set, so the collision guard treated the installer's own artifact as a
        # foreign file and preserved it. That is how `hearting`/`harness`/`mem` stayed
        # pinned to v2.49.0 while `fleet` had to be repointed by hand.
        prior.update((root / rel_source).resolve(strict=False)
                     for root in _managed_release_roots(home))
    except (OSError, RuntimeError):
        return False
    activation_paths = (
        ("claude", home / ".claude" / ".harness" / "activation.json"),
        ("codex", home / ".codex" / ".harness" / "activation.json"),
        (
            "opencode",
            home / ".config" / "opencode" / ".harness" / "activation.json",
        ),
    )
    for runtime, activation in activation_paths:
        try:
            if activation.is_symlink() or activation.stat().st_size > 1 << 20:
                continue
            record = json.loads(activation.read_text(encoding="utf-8"))
            if (
                not isinstance(record, dict)
                or record.get("schema") != 2
                or record.get("runtime") != runtime
                or record.get("scope") != "global"
            ):
                continue
            source_root = Path(record.get("source_root", ""))
            if not source_root.is_absolute():
                continue
            prior.add((source_root / rel_source).resolve(strict=False))
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            continue
    return destination in prior


def _replace_symlink(target, source):
    """Atomically replace one already-validated installer-owned symlink."""
    temporary = target.with_name(f".{target.name}.hearting-{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(source)
        os.replace(temporary, target)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def install_launchers(home=None, dry_run=False):
    """Create guarded common PATH launcher symlinks.

    Args:
        home: Destination home directory, or ``Path.home()`` when omitted.
        dry_run: Return the plan without modifying disk when true.

    Returns:
        list of dict — [{"name", "target", "source", "status"}, ...]
        status ∈ {"planned", "planned-migration", "created", "migrated-legacy",
                  "unchanged", "skipped-collision"}
    """
    if home is None:
        home = Path.home()
    home = Path(home)
    bin_dir = home / ".local" / "bin"

    results = []

    if not dry_run:
        bin_dir.mkdir(parents=True, exist_ok=True)

    managed_current = None
    try:
        import distribution
        if distribution.is_managed():
            managed_current = distribution.current_path()
    except (ImportError, OSError, ValueError):
        managed_current = None

    for name, rel_source in LAUNCHERS:
        # F-80: pinned to the primary checkout, never to whichever tree happens to be
        # running this install. A launcher outlives the worktree an install was run from.
        source = paths.resolve_launcher_source(rel_source)
        desired_source = (
            managed_current / rel_source
            if name == "compute-hosts" and managed_current is not None
            else source
        )
        target = bin_dir / name
        common = {"name": name, "target": str(target), "source": str(source)}

        if target.exists() or target.is_symlink():
            if _is_our_symlink(target, desired_source):
                results.append({**common, "status": "unchanged"})
                continue

            if _is_prior_linked_launcher(target, home, rel_source):
                old_source = _symlink_destination(target)
                if not dry_run:
                    _replace_symlink(target, desired_source)
                results.append({
                    **common,
                    "source": str(desired_source),
                    "status": "planned-migration" if dry_run else "migrated-legacy",
                    "detail": f"{old_source} -> {source}",
                })
                continue

            # Never overwrite a foreign file or symlink.
            results.append({
                **common,
                "status": "skipped-collision",
                "detail": f"foreign '{name}' already at {target} — not overwriting",
            })
            continue

        if not dry_run:
            target.symlink_to(desired_source)
        results.append({**common, "status": "planned" if dry_run else "created"})

    return results


def compute_hosts_status(home=None):
    """Return a read-only ownership/health report for the shared launcher."""
    home = Path(home) if home is not None else Path.home()
    target = home / ".local" / "bin" / "compute-hosts"
    rel_source = "utilities/compute-hosts"
    source = paths.resolve_launcher_source(rel_source)
    if not target.exists() and not target.is_symlink():
        return {"status": "missing", "target": str(target), "source": str(source)}
    if not target.is_symlink():
        return {"status": "foreign-collision", "target": str(target), "source": str(source)}
    managed_current = None
    try:
        import distribution
        if distribution.is_managed():
            managed_current = distribution.current_path()
    except (ImportError, OSError, ValueError):
        managed_current = None
    if managed_current is not None:
        # Ownership and health are separate: concrete release links are ours,
        # but only the lexical current pointer is the healthy managed target.
        desired_raw = str(managed_current / rel_source)
        raw_target = os.readlink(target)
        owned = raw_target == desired_raw or _is_prior_linked_launcher(target, home, rel_source)
        if not owned:
            return {"status": "foreign-collision", "target": str(target), "source": str(source)}
        healthy = raw_target == desired_raw and target.resolve().is_file() and os.access(target, os.X_OK)
        return {
            "status": "healthy" if healthy else "owned-drift",
            "target": str(target), "source": str(managed_current / rel_source),
        }
    if _is_our_symlink(target, source) or _is_prior_linked_launcher(target, home, rel_source):
        return {
            "status": "healthy" if target.resolve().is_file() and os.access(target, os.X_OK) else "owned-drift",
            "target": str(target), "source": str(source),
        }
    return {"status": "foreign-collision", "target": str(target), "source": str(source)}


def uninstall_compute_hosts(home=None, dry_run=False):
    """Remove only an exact installer-owned compute-hosts link."""
    home = Path(home) if home is not None else Path.home()
    target = home / ".local" / "bin" / "compute-hosts"
    report = compute_hosts_status(home)
    if report["status"] == "missing":
        report["status"] = "not-installed"
    elif report["status"] in {"healthy", "owned-drift"}:
        report["status"] = "planned-remove" if dry_run else "removed"
        if not dry_run:
            target.unlink()
    else:
        report["status"] = "preserved-foreign"
    return report
