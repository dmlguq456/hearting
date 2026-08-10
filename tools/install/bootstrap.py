"""Runtime-neutral installer bootstrap helpers.

``restore_memory`` imports ``dump.jsonl`` when ``memory.db`` is absent.
``install_launchers`` creates guarded ``~/.local/bin/{harness,fleet,mem}`` symlinks.
The helpers remain usable independently of installer command wiring.
"""

import os
import subprocess
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
)


def _is_our_symlink(target, source):
    """Return whether target is a symlink resolving exactly to source."""
    if not target.is_symlink():
        return False
    try:
        return target.resolve() == source.resolve()
    except OSError:
        return False


def install_launchers(home=None, dry_run=False):
    """Create guarded ``~/.local/bin/{harness,fleet,mem}`` symlinks.

    Args:
        home: Destination home directory, or ``Path.home()`` when omitted.
        dry_run: Return the plan without modifying disk when true.

    Returns:
        list of dict — [{"name", "target", "source", "status"}, ...]
        status ∈ {"planned", "created", "unchanged", "skipped-collision"}
    """
    if home is None:
        home = Path.home()
    bin_dir = home / ".local" / "bin"

    results = []

    if dry_run:
        for name, rel_source in LAUNCHERS:
            source = paths.resolve_source(rel_source)
            target = bin_dir / name
            results.append({
                "name": name,
                "target": str(target),
                "source": str(source),
                "status": "planned",
            })
        return results

    bin_dir.mkdir(parents=True, exist_ok=True)

    for name, rel_source in LAUNCHERS:
        source = paths.resolve_source(rel_source)
        target = bin_dir / name

        if target.exists() or target.is_symlink():
            if _is_our_symlink(target, source):
                results.append({
                    "name": name,
                    "target": str(target),
                    "source": str(source),
                    "status": "unchanged",
                })
                continue

            # Never overwrite a foreign file or symlink.
            results.append({
                "name": name,
                "target": str(target),
                "source": str(source),
                "status": "skipped-collision",
                "detail": f"foreign '{name}' already at {target} — not overwriting",
            })
            continue

        target.symlink_to(source)
        results.append({
            "name": name,
            "target": str(target),
            "source": str(source),
            "status": "created",
        })

    return results
