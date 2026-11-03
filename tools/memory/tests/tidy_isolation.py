"""Hermetic environment for the session-tidy tests.

Every subprocess a session-tidy test starts gets an environment built here, so a
run started straight from a developer's shell (not only through
``tools/run-tests.py --isolation isolated``) can never reach the real memory
store, the real runtime state, the real remote setting, or the real herdr pane.

Direct test runs default to ``/var/tmp`` (``/tmp`` is refused because it may
contain a parent Git worktree). The repository runner can provide
``HEARTING_TIDY_TEST_ISOLATION_BASE`` beneath its own already-validated suite
temporary root; that root is removed with the suite and also works when
``/var/tmp`` is mounted read-only.

Two ways to use it:

    with isolated_env() as iso:
        iso.run([sys.executable, script, ...], input="...")   # subprocess
        with iso.patched_environ():                            # in-process
            module.function()

``iso.env(extra)`` is the whole child environment: nothing is inherited except
PATH (minus entries under the real home), LANG/LC_ALL/TZ.  Worker markers,
``HERDR_*``, ``MEM_SYNC_*``, ``MEM_EXCHANGE_*`` and ``MEM_DUMP_PUSH`` therefore never
leak in (``MEM_EXCHANGE_AUTO=0`` is pinned, so no background exchange starts); a test
that needs one passes it through ``extra`` on purpose.

While an ``IsolatedEnv`` is alive it also confines *this* process's own
session-tidy state access: ``HEARTING_STATE_CONFINEMENT`` names the root, so a
forgotten bare in-process write that would resolve the runner's or developer's
state root raises ``StateError`` instead of touching it.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
from typing import Iterator, Mapping, Optional

ISOLATION_BASE = Path(os.environ.get("HEARTING_TIDY_TEST_ISOLATION_BASE") or "/var/tmp")
RUNNER_ISOLATION_BASE = bool(os.environ.get("HEARTING_TIDY_TEST_ISOLATION_BASE"))

# Captured before anything patches the environment, from the passwd database
# (not from $HOME, which a runner may already have replaced).
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)

PASSTHROUGH_KEYS = ("LANG", "LC_ALL", "TZ")

# Removed from any environment handed to a child, whatever a caller passes in.
STRIPPED_PREFIXES = (
    "MEM_SYNC_",
    "MEM_EXCHANGE_",
    "HERDR_",
    "AGENT_DISPATCH_",
    "OPENCODE_DISPATCH",
    "AGENT_ROUTE_",
    "AGENT_ARTIFACT_",
)
STRIPPED_KEYS = (
    "MEM_DUMP_PUSH",
    "MEM_DISTILL",
    "AGENT_SESSION_ROLE",
    "AGENT_HOME",
    "FLEET_TITLE_REFRESH",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "OPENCODE_DB",
)

# Names the child environment must always define, each pointing under the root.
ISOLATED_PATH_KEYS = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "MEM_STORE",
    "TMPDIR",
    "CODEX_HOME",
    "OPENCODE_DB",
    "CLAUDE_CONFIG_DIR",
    "HEARTING_STATE_CONFINEMENT",
)

# One stack entry per live IsolatedEnv: [token, root, previous process value].
# The top entry always mirrors `os.environ["HEARTING_STATE_CONFINEMENT"]`;
# popping out of order is safe because the remaining top wins.
_CONFINEMENT_STACK: list[list] = []


def _push_confinement(token: object, root: str) -> None:
    _CONFINEMENT_STACK.append([token, root, os.environ.get("HEARTING_STATE_CONFINEMENT")])
    os.environ["HEARTING_STATE_CONFINEMENT"] = root


def _pop_confinement(token: object) -> None:
    for index in range(len(_CONFINEMENT_STACK) - 1, -1, -1):
        if _CONFINEMENT_STACK[index][0] is token:
            _, _, previous = _CONFINEMENT_STACK.pop(index)
            break
    else:
        return
    if _CONFINEMENT_STACK:
        os.environ["HEARTING_STATE_CONFINEMENT"] = _CONFINEMENT_STACK[-1][1]
    elif previous is None:
        os.environ.pop("HEARTING_STATE_CONFINEMENT", None)
    else:
        os.environ["HEARTING_STATE_CONFINEMENT"] = previous


class IsolationError(AssertionError):
    """A real path (or a forbidden variable) reached the isolated environment."""


def scrub(environ: Mapping[str, str]) -> dict[str, str]:
    """Return ``environ`` without remote, pane, worker and session markers."""
    out: dict[str, str] = {}
    for key, value in environ.items():
        if key in STRIPPED_KEYS or key.startswith(STRIPPED_PREFIXES):
            continue
        out[key] = value
    return out


def _safe_path(value: str) -> str:
    """PATH without entries below the real home (a pyenv or ~/.local/bin shim)."""
    kept = []
    for entry in value.split(os.pathsep):
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve()
        except OSError:
            continue
        if resolved == REAL_HOME or REAL_HOME in resolved.parents:
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


class IsolatedEnv:
    """One throwaway root plus the environment that points every state path at it."""

    def __init__(self, base: Path = ISOLATION_BASE, prefix: str = "tidy-iso-"):
        base = Path(base)
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=prefix, dir=str(base))).resolve()
        self.home = self.root / "home"
        self.xdg_config = self.root / "xdg-config"
        self.xdg_data = self.root / "xdg-data"
        self.xdg_state = self.root / "xdg-state"
        self.xdg_cache = self.root / "xdg-cache"
        self.mem_store = self.root / "mem-store"
        self.tmpdir = self.root / "tmp"
        self.codex_home = self.root / "codex-home"
        self.claude_dir = self.root / "claude-config"
        self.opencode_db = self.root / "opencode" / "opencode.db"
        for path in (self.home, self.xdg_config, self.xdg_data, self.xdg_state,
                     self.xdg_cache, self.mem_store, self.tmpdir,
                     self.codex_home / "sessions", self.claude_dir / "projects",
                     self.opencode_db.parent):
            path.mkdir(parents=True, exist_ok=True)
        # Remote exchange is off no matter what the developer's real setting says.
        sync_dir = self.xdg_config / "hearting"
        sync_dir.mkdir(parents=True, exist_ok=True)
        (sync_dir / "memory-sync.json").write_text(
            json.dumps({"enabled": False}) + "\n", encoding="utf-8")
        self.assert_isolated()
        self._confinement_token = object()
        _push_confinement(self._confinement_token, str(self.root))

    # -- environment -------------------------------------------------------

    def base_env(self) -> dict[str, str]:
        env: dict[str, str] = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg_config),
            "XDG_DATA_HOME": str(self.xdg_data),
            "XDG_STATE_HOME": str(self.xdg_state),
            "XDG_CACHE_HOME": str(self.xdg_cache),
            "MEM_STORE": str(self.mem_store),
            "TMPDIR": str(self.tmpdir),
            "CODEX_HOME": str(self.codex_home),
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "OPENCODE_DB": str(self.opencode_db),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HEARTING_STATE_CONFINEMENT": str(self.root),
            # Same pin as tools/memory/test-isolation.sh: no detached exchange worker is
            # spawned by a write or read made inside a test.
            "MEM_EXCHANGE_AUTO": "0",
            # UI text defaults to English; the tidy suites' stored lines are Korean.
            "FLEET_NOW_LANG": "Korean",
        }
        path = _safe_path(os.environ.get("PATH", ""))
        env["PATH"] = path or "/usr/local/bin:/usr/bin:/bin"
        for key in PASSTHROUGH_KEYS:
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def env(self, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        """The complete child environment; ``extra`` is applied last, on purpose."""
        env = self.base_env()
        if extra:
            env.update({str(k): str(v) for k, v in extra.items()})
        self.assert_env(env, allow=tuple(str(k) for k in (extra or {})))
        return env

    def assert_env(self, env: Mapping[str, str], allow: tuple[str, ...] = ()) -> None:
        """Fail when a real path is still reachable from ``env``.

        ``allow`` names keys a test sets deliberately (a worker marker, a pane id)
        so their presence is not an isolation failure.
        """
        for key in ISOLATED_PATH_KEYS:
            value = env.get(key)
            if not value:
                raise IsolationError(f"{key} is not set in the isolated environment")
            if not _under(Path(value), self.root):
                raise IsolationError(f"{key}={value} is outside {self.root}")
        for key, value in env.items():
            if key in allow or key in ISOLATED_PATH_KEYS or key in ("PATH", "PYTHONDONTWRITEBYTECODE"):
                continue
            if key == "MEM_EXCHANGE_AUTO":
                if value.strip().lower() not in ("0", "off", "false", "no"):
                    raise IsolationError("MEM_EXCHANGE_AUTO would start a background exchange")
                continue
            if key.startswith("MEM_EXCHANGE_"):
                raise IsolationError(f"{key} would steer a background exchange")
            if key.startswith("MEM_SYNC_") or key == "MEM_DUMP_PUSH":
                raise IsolationError(f"{key} would reach a real remote")
            if key.startswith("HERDR_"):
                raise IsolationError(f"{key} would reach a real herdr pane")
            if key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD") or key.startswith(
                    ("AGENT_DISPATCH_", "OPENCODE_DISPATCH")):
                raise IsolationError(f"{key} is a worker marker")
        for key, value in env.items():
            if key == "PATH":
                for entry in value.split(os.pathsep):
                    if entry and _under(Path(entry), REAL_HOME):
                        raise IsolationError(f"PATH entry {entry} is under the real home")
                continue
            if str(REAL_HOME) != "/" and str(REAL_HOME) in value and not _under(Path(value), self.root):
                raise IsolationError(f"{key} mentions the real home {REAL_HOME}")

    def assert_isolated(self) -> None:
        """Static checks on the root itself: location and the remote-off marker."""
        if not _under(self.root, ISOLATION_BASE.resolve()):
            raise IsolationError(f"{self.root} is not under {ISOLATION_BASE}")
        if not RUNNER_ISOLATION_BASE and _under(self.root, Path("/tmp")):
            raise IsolationError("/tmp is refused for isolation roots")
        setting = self.xdg_config / "hearting" / "memory-sync.json"
        if json.loads(setting.read_text(encoding="utf-8")) != {"enabled": False}:
            raise IsolationError("remote exchange is not disabled")
        self.assert_env(self.base_env())

    # -- use ---------------------------------------------------------------

    @contextlib.contextmanager
    def patched_environ(self, extra: Optional[Mapping[str, str]] = None) -> Iterator[None]:
        """Replace ``os.environ`` in-process for the block, then restore it."""
        saved = dict(os.environ)
        env = self.env(extra)
        os.environ.clear()
        os.environ.update(env)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def run(self, args, input: Optional[str] = None, extra: Optional[Mapping[str, str]] = None,
            cwd: Optional[Path] = None, timeout: float = 60.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(a) for a in args], input=input, env=self.env(extra), cwd=str(cwd or self.root),
            text=True, capture_output=True, timeout=timeout)

    def cleanup(self) -> None:
        _pop_confinement(getattr(self, "_confinement_token", None))
        # A tidy run started real detached helpers here (human_wait_state.py's
        # cache refresh, 14 left per suite run): stop them before the root goes.
        _fixture_processes().reap(str(self.root))
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "IsolatedEnv":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()


def _under(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    root = root.resolve() if root.exists() else root
    return resolved == root or root in resolved.parents


def _fixture_processes():
    """tools/fixture_processes.py, loaded by path so sys.path stays as the suite set it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tidy_fixture_processes", Path(__file__).resolve().parents[2] / "fixture_processes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def isolated_env(**kwargs) -> IsolatedEnv:
    return IsolatedEnv(**kwargs)


def memory_fixture_env(root: Path) -> dict[str, str]:
    """Memory-only isolation for shared workflow fixtures, also inherited by children.

    Workflow tests own their runtime/registry fixtures already. Keep those intact
    while pinning all memory writes and pending decisions to their throwaway root.
    """
    root = Path(root) / "memory-fixture"
    store, state, config = root / "store", root / "state", root / "config"
    for path in (store, state, config / "hearting"):
        path.mkdir(parents=True, exist_ok=True)
    (config / "hearting/memory-sync.json").write_text('{"enabled":false}\n')
    return {
        "MEM_STORE": str(store), "MEM_WRITE_EVENTS": str(store / "write-events.jsonl"),
        "MEM_RECALL_EVENTS": str(state / "recall-events.jsonl"),
        "MEM_RECALL_RECEIPTS": str(state / "recall-opportunities"),
        "MEM_CANDIDATE_SEEN": str(state / "candidate-seen"),
        "XDG_STATE_HOME": str(state), "XDG_CONFIG_HOME": str(config),
        "MEM_EXCHANGE_AUTO": "0",
    }


def python() -> str:
    return sys.executable
