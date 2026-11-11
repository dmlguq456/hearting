"""Peer address resolution from Hearting's own records — no terminal multiplexer query.

A peer address names one native session. It resolves to ``(harness, session_id)``
from what Hearting already holds, never from a Herdr or tmux answer:

- ``harness:session_id`` is itself;
- ``[xx]`` / ``xx`` is the confirmed Fleet number (``fleet.session_tags``);
- a name is the session's registry name (Claude's session registry, the
  Hearting ``session_registry`` for Codex/OpenCode), unique among live sessions;
- a window address (``wB:pB3``, tmux ``%N@socket``) is the pane value a live
  native session process received when its window started it
  (``HERDR_PANE_ID``/``TMUX_PANE`` in its own environment), on the same terminal
  server as the caller: the same server name and the same Herdr socket, where a
  missing socket value means the server's socket under that environment's own
  Herdr configuration. Another server's window with the same number never matches.

What cannot be resolved is refused with a named reason; nothing is guessed.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HARNESSES = ("claude", "codex", "opencode")
_SID = re.compile(r"^[A-Za-z0-9._-]{1,256}$")
_TAG = re.compile(r"^\[([0-9a-fA-F]{2})\]$")
_BARE_TAG = re.compile(r"^[0-9a-fA-F]{2}$")
_TMUX_PANE = re.compile(r"^(%[0-9]+)(?:@(/[^\s@]+))?$")
_TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _tools_path():
    if str(_TOOLS) not in sys.path:
        sys.path.insert(0, str(_TOOLS))


def _environ(pid):
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except (OSError, ValueError, TypeError):
        return None
    env = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def _real(path):
    try:
        return os.path.realpath(path) if path else ""
    except (OSError, TypeError, ValueError):
        return str(path or "")


def _server_name(env):
    return (env.get("AGENT_HERDR_SESSION") or env.get("HERDR_SESSION") or "default").strip() or "default"


def _herdr_config_dir(env):
    config = str(env.get("HERDR_CONFIG_PATH") or "").strip()
    if config:
        return os.path.dirname(config)
    base = str(env.get("XDG_CONFIG_HOME") or "").strip()
    if not base:
        home = str(env.get("HOME") or "").strip() or os.path.expanduser("~")
        base = os.path.join(home, ".config")
    return os.path.join(base, "herdr")


def herdr_namespace(env, server=None):
    """The one Herdr API socket a server name means in ``env``.

    An inherited ``HERDR_SOCKET_PATH`` counts only for the server that owns it.
    Otherwise it is where that server listens under this environment's Herdr
    configuration folder (``herdr.sock`` for default, ``sessions/<name>/herdr.sock``
    for a named server). A missing socket value therefore never stands for any
    server: it is this environment's own one."""
    name = (server or _server_name(env)).strip() or "default"
    socket = str(env.get("HERDR_SOCKET_PATH") or "").strip()
    if socket and _server_name(env) == name:
        return _real(socket)
    base = _herdr_config_dir(env)
    return _real(os.path.join(base, "herdr.sock") if name == "default"
                 else os.path.join(base, "sessions", name, "herdr.sock"))


def caller_endpoint(environ=None, *, server=None, namespace=None):
    """The terminal server an address is read on: Herdr server name + namespace, tmux socket.

    ``server`` is the recorded server of a retained message (its duty's identity) and
    ``namespace`` the Herdr socket recorded with it; without a record the caller's own
    environment names the namespace (``herdr_namespace``)."""
    env = os.environ if environ is None else environ
    name = (server or _server_name(env)).strip() or "default"
    socket = _real(namespace) if namespace else herdr_namespace(env, name)
    return {"server": name, "socket": socket,
            "tmux": _real(str(env.get("TMUX") or "").split(",", 1)[0].strip())}


def _same_herdr_server(env, endpoint):
    """Same server name and the same namespace; an unknown socket is never another's."""
    if _server_name(env) != endpoint["server"]:
        return False
    return bool(endpoint["socket"]) and herdr_namespace(env, endpoint["server"]) == endpoint["socket"]


def _tmux_default_socket():
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return _real(os.path.join(base, f"tmux-{os.getuid()}", "default"))


def _shell_rows():
    try:
        import session_shell
        return session_shell.bindings()
    except Exception:
        return []


def native_sessions():
    """Live user-facing native TUIs of this uid: ``{harness, sid, pid, env}``.

    The session id comes from the session's shell attachment when one owns the
    process, else from the exact native record (``pane_ownership._native_session``).
    A registered worker or a service process is never a peer seat.
    """
    from pane_ownership import _native_session, _native_user_tui, _registered_worker, _runtime
    found, seen = [], set()
    # A shell attachment is itself birth-bound proof of its native session process.
    for binding in _shell_rows():
        pid = binding.get("native_pid")
        env = _environ(pid)
        if env is None or _registered_worker(pid):
            continue
        sid = binding["sid"] if binding.get("sid") not in (None, "", "-") else ""
        found.append({"harness": binding["harness"], "sid": sid, "pid": pid, "env": env,
                      "binding": binding})
        seen.add(pid)
    uid = os.getuid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit() or int(entry) in seen:
            continue
        pid = int(entry)
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        harness, service = _runtime(pid)
        if harness not in HARNESSES or service or not _native_user_tui(pid, harness):
            continue
        try:
            sid = _native_session(pid, harness) or ""
        except Exception:
            sid = ""
        env = _environ(pid)
        if env is None:
            continue
        found.append({"harness": harness, "sid": sid, "pid": pid, "env": env, "binding": None})
    return found


def _registry_name(harness, sid):
    try:
        from peer_steward_base import _from_name
        name = _from_name(harness, sid)
    except Exception:
        return None
    return name if isinstance(name, str) and name and name != "-" else None


def presence(harness, sid, *, sessions=None):
    """``present`` / ``absent`` / ``unknown`` for one native session, from Hearting records.

    ``sessions`` is one already-read ``native_sessions()`` table (a courier pass reads it
    once for every row)."""
    if harness not in HARNESSES or not isinstance(sid, str) or not _SID.fullmatch(sid):
        return "unknown"
    try:
        import session_shell
        binding, reason = session_shell.exact_binding(harness, sid)
        if binding is not None:
            return "present"
        if reason == "attachment-ambiguous":
            return "unknown"
        if sessions is None:
            sessions = native_sessions()
        if any(row["harness"] == harness and row["sid"] == sid for row in sessions):
            return "present"
        if harness == "codex":
            _tools_path()
            from fleet.collectors import codex
            if codex.process_for_thread(sid):
                return "present"
    except Exception:
        return "unknown"
    # Not observed live. Callers that act on absence (expiry) still prove the
    # recorded process's death themselves; absence here only withholds delivery.
    return "absent"


def _identity(harness, sid, **extra):
    return {"harness": harness, "session_id": sid, **extra}


def _resolve_tag(tag):
    _tools_path()
    try:
        from fleet import session_tags
        rows = session_tags._read(session_tags._path())
    except FileNotFoundError:
        return None, "fleet-tag-not-found"
    except (OSError, ValueError, TypeError):
        return None, "fleet-tag-unavailable"
    keys = [key for key, row in rows.items() if row["tag"] == tag.lower()
            and session_tags.assigned_tag(*key) == tag.lower()]
    if len(keys) != 1:
        return None, "fleet-tag-ambiguous" if keys else "fleet-tag-not-found"
    return _identity(*keys[0]), None


def _resolve_pane(target, endpoint):
    tmux = _TMUX_PANE.fullmatch(target)
    try:
        sessions = native_sessions()
    except Exception:
        return None, "target-observation-unavailable"
    matches = []
    for row in sessions:
        env = row["env"]
        if tmux:
            socket = _real(str(env.get("TMUX") or "").split(",", 1)[0].strip())
            wanted = _real(tmux.group(2)) if tmux.group(2) else (endpoint["tmux"] or _tmux_default_socket())
            if env.get("TMUX_PANE") == tmux.group(1) and socket and socket == wanted:
                matches.append(row)
        elif env.get("HERDR_PANE_ID") == target and _same_herdr_server(env, endpoint):
            matches.append(row)
    seats = {(row["harness"], row["sid"]) for row in matches}
    if len(seats) > 1:
        return None, "target-pane-ambiguous"
    if not seats:
        return None, "target-pane-unresolved"
    harness, sid = next(iter(seats))
    if not sid:
        return None, "target-session-unresolved"
    return _identity(harness, sid, pane=target), None


def _resolve_name(target):
    try:
        sessions = native_sessions()
    except Exception:
        return None, "target-observation-unavailable"
    seats = set()
    for row in sessions:
        if row["sid"] and _registry_name(row["harness"], row["sid"]) == target:
            seats.add((row["harness"], row["sid"]))
    if len(seats) > 1:
        return None, "target-name-ambiguous"
    if not seats:
        return None, "target-name-not-found"
    return _identity(*next(iter(seats))), None


def is_pane_address(target):
    return isinstance(target, str) and (
        _TMUX_PANE.fullmatch(target) is not None
        or (":" in target and not any(c.isspace() for c in target)
            and target.split(":", 1)[0] not in HARNESSES))


def resolve(target, *, server=None, environ=None, namespace=None):
    """``(identity, reason)``: identity is ``{harness, session_id[, pane]}`` or None.

    ``namespace`` is a retained message's recorded Herdr socket (``caller_endpoint``)."""
    if not isinstance(target, str) or not target:
        return None, "target-not-found"
    if ":" in target and target.split(":", 1)[0] in HARNESSES:
        harness, sid = target.split(":", 1)
        if not _SID.fullmatch(sid) or sid == "-":
            return None, "target-not-found"
        return _identity(harness, sid), None
    tag = _TAG.fullmatch(target)
    if tag:
        return _resolve_tag(tag.group(1))
    if is_pane_address(target):
        return _resolve_pane(target, caller_endpoint(environ, server=server, namespace=namespace))
    found, reason = _resolve_name(target)
    if found is None and reason == "target-name-not-found" and _BARE_TAG.fullmatch(target):
        return _resolve_tag(target)
    return found, reason
