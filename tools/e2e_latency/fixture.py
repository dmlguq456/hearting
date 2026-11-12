"""Operating-size, isolated fixture for the end-to-end latency measurement.

One fixture lives under ``/tmp/<marker>/``. Its registry copy is at most 50 MiB.
Every child process starts from ``child_env`` (cleared environment, fixture HOME
and XDG roots, a PATH that holds only python/node/sh/git/sleep), and every
process this module starts carries ``marker`` so ``fixture_processes.reap`` can
stop it and prove nothing is left. Production state is only ever counted, never
copied: ``production_baseline`` stats and counts lines.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
for _entry in (str(REPO / "utilities"), str(REPO / "tools"), str(REPO)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

MAX_REGISTRY_BYTES = 50 * 1024 * 1024
# Live 2026-11-12: 7,738 lines, 43.0 MB, a handful to ~20 open attempts, ~27 sessions.
DEFAULT_BASELINE = {"rows": 7722, "bytes": 42_900_000, "open": 19, "sessions": 27}
ROW_BYTES = 5500
MARKER_ENV = "HEARTING_E2E_MARKER"
_ATTEMPT = re.compile(rb"attempt_id=([^,\t\n]+)")
_BIN_TOOLS = ("python3", "node", "sh", "git", "sleep")
_DROPPED_PREFIXES = ("HERDR_", "TMUX", "AGENT_SESSION_", "CLAUDE_", "CODEX_", "OPENCODE_")


def production_baseline(path: str | os.PathLike | None = None) -> dict:
    """Counts of the live registry: lines, bytes, open attempts. Content is never kept."""
    if path is None:
        state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        path = os.environ.get("AGENT_DISPATCH_JOBS") or Path(state) / "hearting" / "dispatch" / "jobs.log"
    try:
        size = os.stat(path).st_size
        rows = 0
        last: dict[bytes, bytes] = {}
        with open(path, "rb") as handle:
            for line in handle:
                rows += 1
                fields = line.split(b"\t", 6)
                if len(fields) < 6:
                    continue
                found = _ATTEMPT.search(fields[5])
                if found:
                    last[found.group(1)] = fields[1]
        opened = sum(1 for status in last.values() if status in (b"open", b"running"))
        return {"rows": rows, "bytes": size, "open": opened, "source": "live-count"}
    except OSError:
        return {**DEFAULT_BASELINE, "source": "default"}


def refuse_oversize(jobs: str | os.PathLike, limit: int = MAX_REGISTRY_BYTES) -> int:
    size = os.stat(jobs).st_size
    if size > limit:
        raise ValueError(f"registry copy is {size} bytes, over the {limit} byte fixture limit")
    return size


def new_marker() -> str:
    return f"hearting-e2e-{os.getpid()}-{secrets.token_hex(3)}"


def _process_start(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().rsplit(b")", 1)[1].split()[19].decode()
    except (OSError, IndexError):
        return None


_SLEEPERS: list = []  # keeps the Popen objects alive (no ResourceWarning); reap() ends them


def spawn_sleepers(marker: str, count: int, env_base: dict) -> list[tuple[int, str]]:
    """``count`` marked ``sleep`` processes; returns ``(pid, start ticks)`` of each."""
    out = []
    env = {**env_base, MARKER_ENV: marker}
    for _ in range(count):
        proc = subprocess.Popen([shutil.which("sleep") or "/bin/sleep", "86400"], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        _SLEEPERS.append(proc)
        out.append((proc.pid, _process_start(proc.pid) or ""))
    return out


def make_bin(base: Path) -> Path:
    """The PATH of every child: no herdr, tmux, codex, claude or opencode on it."""
    directory = base / "bin"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in _BIN_TOOLS:
        target = sys.executable if name == "python3" else shutil.which(name)
        if target and not (directory / name).exists():
            (directory / name).symlink_to(target)
    return directory


def child_env(fixture: dict, extra: dict | None = None) -> dict:
    """Environment for a child: cleared, fixture HOME/XDG/TMPDIR, no runtime variables."""
    base = Path(fixture["root"])
    env = {
        "PATH": str(base / "bin"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": str(base / "home"),
        "XDG_STATE_HOME": str(base / "xdg"),
        "XDG_CACHE_HOME": str(base / "cache"),
        "XDG_DATA_HOME": str(base / "data"),
        "XDG_CONFIG_HOME": str(base / "config"),
        "TMPDIR": str(base / "tmp"),
        "AGENT_DISPATCH_JOBS": fixture["jobs"],
        "AGENT_HOME": str(REPO),
        "AGENT_RESOURCE_RUN_INDEX": str(base / "resource-runs.index.json"),
        "CLAUDE_CONFIG_DIR": str(base / "home" / ".claude"),
        "PYTHONDONTWRITEBYTECODE": "1",
        MARKER_ENV: fixture["marker"],
    }
    env.update(extra or {})
    return {key: value for key, value in env.items()
            if not key.startswith(_DROPPED_PREFIXES) or key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")}


def build(*, history_rows: int | None = None, row_bytes: int = ROW_BYTES, open_routes: int | None = None,
          live_sessions: int | None = None, parent: str | None = None, prime: bool = False,
          probe: bool = True, scale_history: int = 1) -> dict:
    """Create the fixture and its sleeper processes; returns the orchestrator's dict.

    Keys: ``root``, ``marker``, ``jobs``, ``env`` (child env), ``sessions``,
    ``routes``, ``streams``, ``baseline``, ``bytes``, ``base_fixture``.
    """
    import hook_perf_fixture
    baseline = production_baseline() if probe else {**DEFAULT_BASELINE, "source": "default"}
    rows = history_rows if history_rows is not None else int(baseline["rows"]) * scale_history
    routes = open_routes if open_routes is not None else max(1, int(baseline.get("open", 19) or 19))
    sessions = live_sessions if live_sessions is not None else DEFAULT_BASELINE["sessions"]
    marker = new_marker()
    root = Path(tempfile.mkdtemp(prefix=marker + "-", dir=parent or "/tmp"))
    os.chmod(root, 0o700)
    for name in ("tmp", "cache", "data", "config"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    make_bin(root)
    stub = {"root": str(root), "marker": marker, "jobs": str(root / "state" / "dispatch" / "jobs.log")}
    env = child_env(stub)
    try:
        pids = spawn_sleepers(marker, sessions + routes, env)
        base = hook_perf_fixture.build(
            root, jobs_rows=rows, history_rows=rows, record_size=row_bytes,
            open_routes=routes, live_sessions=sessions, worker_streams=True,
            session_pids=pids, depth1_rows=400, depth1_open_every=0,
            min_jobs_bytes=0, prime=prime)
        size = refuse_oversize(base["jobs"])
    except BaseException:
        cleanup({**stub, "root": str(root)})
        raise
    fixture = {
        "root": str(root), "marker": marker, "jobs": base["jobs"], "bytes": size,
        "env": child_env({**stub, "jobs": base["jobs"]}),
        "sessions": base["e2e_sessions"], "routes": base["e2e_routes"],
        "streams": base["e2e_streams"], "baseline": baseline, "base_fixture": base,
        "rows": rows, "open_routes": routes,
    }
    # The hook fixture's own env (state under ``root/state``) is what T4 replays.
    return fixture


def cleanup(fixture: dict, *, keep_files: bool = False) -> dict:
    """Stop every marked process, delete the fixture, and prove both."""
    import fixture_processes
    root = Path(fixture["root"])
    marker = fixture["marker"]
    reaped, error = [], None
    try:
        reaped = fixture_processes.reap(marker)
    except TimeoutError as exc:  # keep going: deleting the files is still owed
        error = str(exc)
    if not keep_files:
        shutil.rmtree(root, ignore_errors=True)
    leftover = fixture_processes.marked_processes(marker)
    return {"scratch_removed": (not root.exists()) if not keep_files else False, "leftover_processes": len(leftover),
            "reaped": len(reaped), "error": error}
