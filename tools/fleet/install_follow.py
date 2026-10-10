"""Follow a committed managed install without creating a second Fleet viewer."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from . import installinfo

_HANDOFF = "_FLEET_REEXEC_FD"
_STDERR = "_FLEET_REEXEC_STDERR_FD"


def read_handoff(env=None):
    """Consume this viewer's anonymous descriptor once; no shared state file."""
    env = os.environ if env is None else env
    raw = env.pop(_HANDOFF, None)
    stderr = env.pop(_STDERR, None)
    if stderr:
        try:
            fd = int(stderr)
            if fd > 2:
                os.dup2(fd, 2)
                os.close(fd)
        except (ValueError, OSError):
            pass
    if raw is None:
        return None
    try:
        fd = int(raw)
        if fd <= 2 or not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            value = json.loads(handle.read(8 * 1024 * 1024))
        return value if isinstance(value, dict) else None
    except (ValueError, OSError, UnicodeError):
        return None


class InstallFollower:
    def __init__(self, source=None, *, env=None):
        self.env = dict(os.environ if env is None else env)
        self.source = Path(source or installinfo._root_from_module()).resolve()
        home = Path.home()
        xdg_data = installinfo._absolute_env_path(self.env, "XDG_DATA_HOME", home / ".local/share")
        xdg_state = installinfo._absolute_env_path(self.env, "XDG_STATE_HOME", home / ".local/state")
        self.data = installinfo._absolute_env_path(
            self.env, "HARNESS_DATA_ROOT", (xdg_data or home / ".local/share") / "hearting")
        self.state = installinfo._absolute_env_path(
            self.env, "HARNESS_STATE_ROOT", (xdg_state or home / ".local/state") / "hearting")
        version = installinfo._release_version(self.source)
        match = installinfo._SEMVER_TAG_RE.fullmatch(version or "")
        self.version = tuple(map(int, match.groups())) if match else None
        self.managed = bool(self.version is not None and self.data and self.state
                            and self.source.parent == (self.data / "releases").resolve()
                            and self.source.name == version
                            and installinfo._load_json(self.source / ".hearting-release.json"))

    @contextmanager
    def _installed(self):
        """Do not wait for an install or change its existing lock/state."""
        if not self.managed:
            yield False
            return
        handle = None
        try:
            import fcntl
            fd = os.open(self.state / "distribution.lock",
                         os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            handle = os.fdopen(fd, "rb")
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                handle.close()
                handle = None
            else:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except (ImportError, OSError):
            if handle is not None:
                handle.close()
                handle = None
        try:
            yield handle is not None
        finally:
            if handle is not None:
                handle.close()

    def _candidate(self):
        try:
            current = self.data / "current"
            if not current.is_symlink():
                return None
            target = current.resolve(strict=True)
            if target == self.source or target.parent != (self.data / "releases").resolve():
                return None
            version = installinfo._release_version(target)
            match = installinfo._SEMVER_TAG_RE.fullmatch(version or "")
            if not match or tuple(map(int, match.groups())) <= self.version or target.name != version:
                return None
            state = installinfo._load_json(self.state / "distribution.json") or {}
            release = installinfo._load_json(target / ".hearting-release.json") or {}
            checksum = state.get("archive_sha256")
            if (state.get("schema") != 1 or release.get("schema") != 1
                    or state.get("channel", "stable") not in {"stable", "pinned"}
                    or state.get("version") != version or release.get("version") != version
                    or not installinfo._same_root(state.get("release_root"), target)
                    or not isinstance(checksum, str) or len(checksum) != 64
                    or any(char not in "0123456789abcdef" for char in checksum)
                    or release.get("archive_sha256") != checksum
                    or not (target / "tools/fleet/fleet.py").is_file()):
                return None
            return target
        except (OSError, RuntimeError, ValueError):
            return None

    def target(self):
        with self._installed() as ready:
            return self._candidate() if ready else None

    def restart(self, target, argv, viewer_state, *, stderr_fd=None):
        """Recheck under the install lock, then exec in-place with anonymous state."""
        with self._installed() as ready:
            if not ready or self._candidate() != target:
                return False
            try:
                with tempfile.TemporaryFile(mode="w+b") as handoff:
                    handoff.write(json.dumps(viewer_state, ensure_ascii=False).encode("utf-8"))
                    handoff.seek(0)
                    fd = handoff.fileno()
                    os.set_inheritable(fd, True)
                    env = dict(os.environ)
                    env["AGENT_HOME"] = str(target)
                    env[_HANDOFF] = str(fd)
                    if stderr_fd is not None:
                        os.set_inheritable(stderr_fd, True)
                        env[_STDERR] = str(stderr_fd)
                    os.execve(sys.executable,
                              [sys.executable, str(target / "tools/fleet/fleet.py"), *argv], env)
            except (OSError, ValueError, TypeError):
                return False
            finally:
                if stderr_fd is not None:
                    os.set_inheritable(stderr_fd, False)
        return False
