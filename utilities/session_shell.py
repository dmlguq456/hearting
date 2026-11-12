"""Optional native-terminal attachment: exact identity and private local control.

This module does not own messages, questions, roles or delivery receipts.
Attachments are derived entries in the existing Fleet session registry; every
control call checks current process births and the connected peer again.
"""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
from typing import Mapping

HARNESSES = frozenset(("claude", "codex", "opencode"))
SCHEMA = "hearting-session-shell-v1"
MAX_RECORD_BYTES = 8192
MAX_CONTROL_BYTES = 64 * 1024
_SID = re.compile(r"^[A-Za-z0-9._-]{1,256}$")


def policy_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME") or Path.home()) / ".config")
    return base / "hearting" / "session-shell.json"


def _private_json(path: Path, limit: int):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or info.st_size > limit):
        raise ValueError("unsafe private record")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("private record too large")
    return json.loads(raw)


DISPLAY_DEFAULTS = {"layout": "side", "side_width": 40, "side_min_columns": 100,
                    "narrow": "bottom", "auxiliary": "summary", "bottom_rows": 14,
                    "position": "right", "theme": "terminal", "shade": "none", "route_line": "time"}
# Appearance choices. An unknown value falls back to its default on its own and
# never disables the shell, so a hand-edited or newer value cannot cost the panel.
APPEARANCE = {"position": ("right", "left"), "theme": ("terminal", "light", "mono"),
              "shade": ("none", "light", "medium")}
# What a folded route row carries besides its title, harness and context:
# its elapsed time or its model (spec rule 7). Same per-key fallback.
CHOICES = {**APPEARANCE, "route_line": ("time", "model")}


def _appearance(value):
    return {key: value.get(key) if value.get(key) in choices else choices[0]
            for key, choices in CHOICES.items()}


def _validate_configuration(value):
    keys = {"enabled", *DISPLAY_DEFAULTS}
    if not isinstance(value, dict) or not set(value) <= keys or type(value.get("enabled")) is not bool:
        raise ValueError("invalid shell configuration")
    if value.get("layout", "side") not in {"bottom", "side"}:
        raise ValueError("invalid shell layout")
    if value.get("narrow", "bottom") not in {"bottom", "compact"} or value.get("auxiliary", "summary") not in {"summary", "detail"}:
        raise ValueError("invalid shell presentation")
    for key, default, lower, upper in (("side_width", 40, 32, 80), ("side_min_columns", 100, 80, 1000), ("bottom_rows", 14, 8, 20)):
        number = value.get(key, default)
        if type(number) is not int or not lower <= number <= upper:
            raise ValueError("invalid shell geometry")
    return value


def _configuration(environ=None):
    return _validate_configuration(_private_json(policy_path(environ), MAX_RECORD_BYTES))


def policy(environ: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """Absence and invalid configuration both preserve the original CLI."""
    try:
        value = _configuration(environ)
    except FileNotFoundError:
        return False, "disabled"
    except (OSError, ValueError, TypeError):
        return False, "config-unavailable"
    return value["enabled"], "enabled" if value["enabled"] else "disabled"


def display_policy(environ=None):
    try:
        value = _configuration(environ)
    except (OSError, ValueError, TypeError):
        value = {}
    return {**{key: value.get(key, default) for key, default in DISPLAY_DEFAULTS.items()}, **_appearance(value)}


def save_display_policy(settings, environ=None):
    """Explicit shell-settings Apply. Preserve enablement and backup exact bytes."""
    if not isinstance(settings, dict) or set(settings) != set(DISPLAY_DEFAULTS):
        raise ValueError("invalid shell settings")
    path = policy_path(environ)
    before = _private_json(path, MAX_RECORD_BYTES)
    if _appearance(settings) != {key: settings[key] for key in CHOICES}:
        raise ValueError("invalid shell appearance")
    value = _validate_configuration({**before, **settings})
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/install"))
    import safe_fs
    import hashlib
    state = safe_fs.capture_state(path, include_payload=True)
    if json.loads(state.payload) != before:
        raise ValueError("shell settings changed")
    backup = path.with_name(path.name + ".backup-" + hashlib.sha256(state.payload).hexdigest()[:16])
    if not backup.exists():
        authority = safe_fs.authority(backup, owner="session-shell:settings-backup", allowed_paths=(backup,), expected=safe_fs.capture_state(backup))
        safe_fs.atomic_write_bytes(authority, state.payload, state.mode or 0o600)
    elif backup.is_symlink() or backup.read_bytes() != state.payload:
        raise ValueError("shell settings backup changed")
    authority = safe_fs.authority(path, owner="session-shell:settings", allowed_paths=(path,), expected=state)
    safe_fs.atomic_write_bytes(authority, (json.dumps(value, ensure_ascii=False) + "\n").encode(), state.mode or 0o600)
    return {key: value[key] for key in DISPLAY_DEFAULTS}


def process(pid: int) -> dict | None:
    """Fresh Linux identity; unknown is never a live attachment."""
    if type(pid) is not int or pid <= 1:
        return None
    try:
        directory = Path("/proc") / str(pid)
        info = directory.stat()
        body = (directory / "stat").read_text().rsplit(") ", 1)[1].split()
        if body[0] in ("Z", "X") or info.st_uid != os.getuid():
            return None
        return {"pid": pid, "start": body[19], "parent": int(body[1]), "uid": info.st_uid}
    except (OSError, ValueError, IndexError):
        return None


def _descends(pid: int, ancestor: int) -> bool:
    seen = set()
    for _ in range(32):
        if pid == ancestor:
            return True
        if pid in seen:
            return False
        seen.add(pid)
        row = process(pid)
        if row is None:
            return False
        pid = row["parent"]
    return False


def registry_root() -> Path:
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from fleet.session_registry import state_root
    return Path(state_root())


def binding_path(harness: str, host_pid: int) -> Path:
    if harness not in HARNESSES or type(host_pid) is not int or host_pid <= 1:
        raise ValueError("invalid shell attachment identity")
    # Claude's vendor-native sessions directory remains read-only.
    return registry_root() / harness / f"session-shell-{host_pid}.json"


def valid_binding(value: dict, *, harness: str | None = None, sid: str | None = None) -> bool:
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or value.get("harness") not in HARNESSES
            or not isinstance(value.get("sid"), str) or not _SID.fullmatch(value["sid"])
            or harness is not None and value["harness"] != harness
            or sid is not None and value["sid"] != sid
            or not isinstance(value.get("incarnation"), str)
            or not re.fullmatch(r"[0-9a-f]{32,64}", value["incarnation"])
            or not isinstance(value.get("socket"), str) or not os.path.isabs(value["socket"])
            or "\0" in value["socket"] or len(os.fsencode(value["socket"])) >= 104):
        return False
    host, native = process(value.get("host_pid")), process(value.get("native_pid"))
    if not (host and native and host["start"] == value.get("host_start")
            and native["start"] == value.get("native_start") and host["pid"] != native["pid"]):
        return False
    if "broker_pid" in value or "broker_start" in value:
        owner = process(value.get("broker_pid"))
        return bool(owner and owner["start"] == value.get("broker_start")
                    and host["parent"] == owner["pid"] and native["pid"] != owner["pid"]
                    and _descends(native["pid"], owner["pid"]))
    return _descends(native["pid"], host["pid"])



def _unread(error: OSError) -> bool:
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from fleet.session_registry import unread_error
    return unread_error(error)


def read_binding(path: Path, *, harness: str | None = None, sid: str | None = None,
                 raise_unread: bool = False) -> dict | None:
    try:
        value = _private_json(path, MAX_RECORD_BYTES)
        if not valid_binding(value, harness=harness, sid=sid):
            return None
        if path != binding_path(value["harness"], value["host_pid"]):
            return None
        return value
    except OSError as error:
        if raise_unread and _unread(error):
            raise
        return None
    except (ValueError, TypeError):
        return None


def bindings(*, harness: str | None = None, sid: str | None = None,
             raise_unread: bool = False) -> list[dict]:
    """Valid attachments. With ``raise_unread`` an attachment directory or
    record that exists but was not read raises OSError instead of vanishing."""
    if harness is not None and harness not in HARNESSES:
        return []
    rows = []
    for name in sorted((harness,) if harness else HARNESSES):
        directory = registry_root() / name
        if raise_unread:
            # Path.glob() hides a directory it could not list.
            try:
                paths = [directory / entry for entry in os.listdir(directory)
                         if fnmatch.fnmatchcase(entry, "session-shell-*.json")]
            except OSError as error:
                if _unread(error):
                    raise
                paths = []
        else:
            paths = directory.glob("session-shell-*.json")
        for path in paths:
            row = read_binding(path, harness=name, sid=sid, raise_unread=raise_unread)
            if row is not None:
                rows.append(row)
    return rows


def exact_binding(harness: str, sid: str) -> tuple[dict | None, str]:
    rows = bindings(harness=harness, sid=sid)
    return (rows[0], "") if len(rows) == 1 else (None, "attachment-ambiguous" if rows else "attachment-unavailable")


def pane_seat(info, harness: str | None = None, *, rows=None, birth=None) -> dict | None:
    """The one attachment whose PTY owner is this pane's foreground: the pane's seat.

    A shell-wrapped pane's foreground is the retained PTY owner (broker), not the
    native TUI on its inner PTY. When that owner's exact birth leads the pane's
    foreground group and its attachment is valid (births, parentage and descent
    re-read by ``valid_binding``), the attachment's native session is the pane's
    seat. Callers use this one fact instead of re-proving it per step.
    """
    if not isinstance(info, dict):
        return None
    group = info.get("foreground_process_group_id")
    pids = {row.get("pid") for row in info.get("foreground_processes") or () if isinstance(row, dict)}
    if type(group) is not int or group <= 1 or group not in pids:
        return None
    if birth is None:
        def birth(pid):
            row = process(pid)
            return row["start"] if row else None
    joined = []
    for binding in bindings(harness=harness) if rows is None else rows:
        owner = "broker" if "broker_pid" in binding else "host"
        if binding.get(owner + "_pid") == group and birth(group) == binding.get(owner + "_start"):
            joined.append(binding)
    return joined[0] if len(joined) == 1 else None


PASSTHROUGH = "passthrough"


def native_screen(binding: dict):
    """The screen a key typed into this seat's pane reaches, as ``(char, faint)`` cell lines.

    A wrapped pane shows the shell's panel and frame around the native screen;
    draft, form and footer judgments read the native cells the host parses.
    While a shell editor (settings, the Fleet explorer, a question) holds the
    outer keys the host says so (``focused``); typed input would land there, not
    in the native box these cells show, so that is None like no answer.
    ``PASSTHROUGH`` only when the host relays the pane raw: the pane is then the
    native screen and its keys go straight to native.
    """
    result = control(binding, "screen")
    if isinstance(result, dict) and result.get("ok") is False and result.get("reason") == "display-unavailable":
        return PASSTHROUGH
    screen = (result.get("screen") if isinstance(result, dict) and result.get("ok")
              and result.get("focused") is False else None)
    if not isinstance(screen, list) or len(screen) > 512:
        return None
    lines = []
    for runs in screen:
        if not isinstance(runs, list):
            return None
        line = []
        for run in runs:
            if (not isinstance(run, list) or len(run) != 2 or not isinstance(run[0], str)
                    or type(run[1]) is not bool):
                return None
            line.extend((char, run[1]) for char in run[0])
        lines.append(line)
    return lines


def open_panel(harness: str, sid: str) -> dict:
    """A display request keeps the caller's exact native address throughout."""
    binding, reason = exact_binding(harness, sid)
    return control(binding, "panel", expanded=True) if binding else {"ok": False, "reason": reason}


def control(binding: dict, operation: str, **fields) -> dict:
    """One bounded request to the same live attachment, never a different fallback."""
    if not valid_binding(binding):
        return {"ok": False, "reason": "attachment-changed", "not_started": True}
    identity = {key: binding[key] for key in
                ("harness", "sid", "incarnation", "host_pid", "host_start", "native_pid", "native_start")}
    if "broker_pid" in binding:
        identity.update(broker_pid=binding["broker_pid"], broker_start=binding["broker_start"])
    request = {**fields, "op": operation, **identity}
    raw = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_CONTROL_BYTES:
        return {"ok": False, "reason": "control-too-large", "not_started": True}
    started = False
    try:
        info = Path(binding["socket"]).lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("unsafe socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(2)
            stream.connect(binding["socket"])
            peer_pid, uid, _gid = struct.unpack("3i", stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer_pid != binding["host_pid"] or uid != os.getuid() or not valid_binding(binding):
                raise ValueError("attachment peer changed")
            started = True
            stream.sendall(raw)
            response = bytearray()
            while b"\n" not in response:
                chunk = stream.recv(min(4096, MAX_CONTROL_BYTES + 1 - len(response)))
                if not chunk:
                    raise ValueError("incomplete control response")
                response.extend(chunk)
                if len(response) > MAX_CONTROL_BYTES:
                    raise ValueError("control response too large")
            result = json.loads(response.partition(b"\n")[0])
            if (not isinstance(result, dict) or any(result.get(key) != val for key, val in identity.items())):
                raise ValueError("control response identity changed")
            return result
    except (OSError, ValueError, TypeError, struct.error):
        return {"ok": False, "reason": "control-unavailable", "not_started": not started}



def resolve_target(target: str) -> tuple[dict | None, str]:
    """Pin a full native address or a unique confirmed Fleet display tag."""
    if not isinstance(target, str):
        return None, "target-not-found"
    if ":" in target:
        harness, sid = target.split(":", 1)
        if harness not in HARNESSES or not _SID.fullmatch(sid):
            return None, "target-not-found"
        return exact_binding(harness, sid)
    tag = target[1:-1] if re.fullmatch(r"\[[0-9a-fA-F]{2}\]", target) else target
    if not re.fullmatch(r"[0-9a-fA-F]{2}", tag):
        return None, "target-not-found"
    registry_root()
    from fleet import session_tags
    try:
        identities = [key for key, row in session_tags._read(session_tags._path()).items()
                      if row["tag"] == tag.lower()]
    except (OSError, ValueError, TypeError):
        return None, "fleet-tag-unavailable"
    if len(identities) != 1:
        return None, "fleet-tag-ambiguous" if identities else "fleet-tag-not-found"
    return exact_binding(*identities[0])

def display_panel(harness: str, sid: str, *, expanded: bool = True) -> dict:
    binding, reason = exact_binding(harness, sid)
    if binding is None:
        return {"ok": False, "reason": reason}
    return control(binding, "panel", expanded=bool(expanded))


def _peer_message():
    import importlib.util
    name = "hearting_shell_peer_message"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("peer-message.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def deliver_pending(ref: str, harness: str, sid: str) -> dict:
    """Use the existing peer claim once; socket acceptance is not a receipt."""
    if harness not in ("claude", "opencode"):
        return {"ok": False, "reason": "native-queue-required", "not_started": True}
    binding, reason = exact_binding(harness, sid)
    if binding is None:
        return {"ok": False, "reason": reason, "not_started": True}
    before = control(binding, "status")
    if not before.get("ok") or before.get("state") != "idle" or not before.get("empty"):
        return {"ok": False, "reason": "not-idle-empty", "not_started": True}
    peer = _peer_message()
    recipient = {"harness": harness, "session_id": sid}
    reserved = peer.reserve_pending_herdr(ref, recipient, receipt="session-shell-inflight")
    if reserved is None:
        return {"ok": False, "reason": "peer-delivery-already-claimed", "not_started": True}
    result = control(binding, "inject", ref=ref, claim=reserved["rpc_claim"], epoch=before.get("epoch"))
    if result.get("not_started") is True:
        if not peer.release_unstarted_pending_write(reserved, receipt="session-shell-not-started"):
            peer.release_pending_herdr_claim(reserved, receipt="session-shell-not-started")
    elif result.get("ok"):
        peer.mark_pending_prompt_queued(ref, reserved["rpc_claim"])
    return result


def _codex_selected_root(binding):
    """Join stable raw native footer selection to this TUI's fresh root ownership."""
    from pane_ownership import _codex_current_footer, _process, _registered_worker, _runtime
    from fleet.collectors import codex, procscan

    def same_owner():
        return (valid_binding(binding)
            and read_binding(binding_path("codex", binding["host_pid"])) == binding
            and _runtime(binding["native_pid"]) == ("codex", False)
            and not _registered_worker(binding["native_pid"]))

    if (binding.get("harness") != "codex" or not valid_binding(binding)
            or read_binding(binding_path("codex", binding["host_pid"])) != binding):
        return {"selected_sid": None}
    first = control(binding, "native-selection")
    frame = first.get("native_frame") if first.get("ok") else None
    if (not isinstance(frame, dict) or type(frame.get("revision")) is not int
            or frame["revision"] < 0 or type(frame.get("epoch")) is not int or frame["epoch"] < 0
            or not isinstance(frame.get("lines"), list)
            or len(frame["lines"]) > 8 or any(not isinstance(s, str) for s in frame["lines"])
            or sum(len(s) for s in frame["lines"]) > 16384):
        return None
    if not same_owner():
        return {"selected_sid": None}
    if frame.get("invalid") is True:
        return {"selected_sid": None}
    record = _process(binding["native_pid"])
    if (not record or str(record[5]) != binding["native_start"]
            or not record[3] or record[2] != record[4]):
        return {"selected_sid": None}
    state, selected = _codex_current_footer(frame["lines"])
    if state != "current":
        return {"selected_sid": None} if state == "invalid" else None
    pid, birth = binding["native_pid"], binding["native_start"]
    native_sid = codex.session_id_of_process(pid, fresh=True)
    if native_sid and native_sid != selected:
        return {"selected_sid": None}
    # Membership corroborates the native viewport; alone it never selects one
    # of several roots retained by a shared daemon after /resume. A TUI with no
    # daemon (--no-daemon) has no membership; there the exact process-native
    # proof of the same thread corroborates it instead.
    member = codex.process_for_thread(selected, environ=procscan.read_environ(pid), fresh=True)
    if member != (pid, birth) and not (member is None and native_sid == selected):
        return {"selected_sid": None}
    latest_native = codex.session_id_of_process(pid, fresh=True)
    if latest_native and latest_native != selected:
        return {"selected_sid": None}
    second = control(binding, "native-selection")
    later = second.get("native_frame") if second.get("ok") else None
    if not isinstance(later, dict) or _process(pid) != record or not same_owner():
        return {"selected_sid": None}
    if later != frame:
        # Only a repaint (a working turn's spinner) between the reads: the same
        # selection and no input. That is no stable observation, so the
        # independent native proof decides, as when no frame is available.
        repaint = (type(later.get("revision")) is int and later["revision"] > frame["revision"]
                   and {**later, "revision": frame["revision"]} == frame)
        return None if repaint else {"selected_sid": None}
    return {"selected_sid": selected, "native_revision": frame["revision"], "native_epoch": frame["epoch"]}


def _codex_current_root(pid, birth, sid):
    """Current native proof, or the same attachment's stable native selection."""
    from fleet.collectors import codex
    before = process(pid)
    if before is None or before["start"] != str(birth):
        return False
    current = codex.session_id_of_process(pid, fresh=True)
    if current and current != sid:
        return False
    attached = [row for row in bindings(harness="codex")
                if row["native_pid"] == pid and row["native_start"] == str(birth)]
    if len(attached) > 1:
        return False
    if attached:
        selected = _codex_selected_root(attached[0])
        if selected is not None:
            current = selected.get("selected_sid")
    after = process(pid)
    return bool(current == sid and after and after["start"] == before["start"])


def answer_attachment_shape(value):
    """Static private-claim validation; historical receipts need no live process."""
    keys = {"schema", "harness", "sid", "incarnation", "host_pid", "host_start",
            "native_pid", "native_start", "socket"}
    if not isinstance(value, dict) or set(value) not in (keys, keys | {"broker_pid", "broker_start"}):
        return False
    return bool(value.get("schema") == SCHEMA and value.get("harness") == "codex"
        and isinstance(value.get("sid"), str) and _SID.fullmatch(value["sid"])
        and isinstance(value.get("incarnation"), str) and re.fullmatch(r"[0-9a-f]{32,64}", value["incarnation"])
        and isinstance(value.get("socket"), str) and os.path.isabs(value["socket"])
        and "\0" not in value["socket"] and len(os.fsencode(value["socket"])) < 104
        and all(type(value.get(key + "_pid")) is int and value[key + "_pid"] > 1
                and isinstance(value.get(key + "_start"), str) and value[key + "_start"].isdigit()
                for key in (("host", "native", "broker") if "broker_pid" in value else ("host", "native"))))


def answer_attachment_current(value):
    """The same attachment and current root immediately before an answer effect."""
    try:
        return bool(answer_attachment_shape(value) and valid_binding(value)
            and read_binding(binding_path("codex", value["host_pid"])) == value
            and _codex_current_root(value["native_pid"], value["native_start"], value["sid"]))
    except Exception:
        return False


def _lifecycle_owner(harness: str, sid: str, env: dict):
    """The actual native TUI, including a callback run by its shared daemon."""
    import session_tidy
    from pane_ownership import _caller_runtime, _registered_worker
    owner, service = _caller_runtime(os.getpid(), harness)
    if owner is None:
        return None
    if service:
        if harness != "codex":
            return None
        from fleet.collectors import codex, procscan
        selected = codex.process_for_thread(sid, environ=env,
                                           expected_service=(owner[0], str(owner[5])))
        if selected is None:
            return None
        pid, birth = selected
        me = process(pid)
        if me is None or me["start"] != str(birth) or _registered_worker(pid):
            return None
        # The daemon retains its first client's environment. Only this proved
        # TUI's initial environment can name its own private PTY attachment.
        env = procscan.read_environ(pid)
        if session_tidy.is_worker(env) or not _codex_current_root(pid, birth, sid):
            return None
    else:
        if session_tidy.is_worker(env):
            return None
        me = process(owner[0])
        birth = str(owner[5])
    current = process(me["pid"]) if me is not None else None
    if current is None or current["start"] != str(birth):
        return None
    return current, env


def lifecycle(harness: str, event: str, sid: str, *, env=None) -> None:
    """Silent native hook report, separate from all prompt classification."""
    env = os.environ if env is None else env
    native_model = env.get("HEARTING_SHELL_NATIVE_MODEL")
    if harness not in HARNESSES or not _SID.fullmatch(sid or ""):
        return
    if harness != "codex" and not env.get("HEARTING_SHELL_SOCKET"):
        return
    # The official callback is the SID source. A native registry can appear
    # after SessionStart, so requiring its prior SID would lose that first
    # report forever. The host separately checks this owner's exact PTY.
    try:
        selected = _lifecycle_owner(harness, sid, env)
    except Exception:
        return  # An unavailable native observation never blocks the hook.
    if selected is None:
        return
    me, env = selected
    endpoint, token = env.get("HEARTING_SHELL_SOCKET"), env.get("HEARTING_SHELL_TOKEN")
    if not endpoint or not token:
        return
    import time
    req = {"op": "event", "harness": harness, "sid": sid, "event": event,
           "token": token, "pid": me["pid"], "start": me["start"], "seq": time.monotonic_ns() // 1000}
    if isinstance(native_model, str) and 0 < len(native_model) <= 128 and all(ord(c) >= 32 and ord(c) != 127 for c in native_model):
        req["native_model"] = native_model
    try:
        with socket.socket(socket.AF_UNIX) as stream:
            stream.settimeout(0.5)
            stream.connect(endpoint)
            stream.sendall((json.dumps(req) + "\n").encode())
            stream.recv(MAX_RECORD_BYTES)
    except (OSError, ValueError):
        pass


def publish_binding(value: dict) -> bool:
    """Derived, birth-bound attachment in Fleet's existing registry."""
    if not valid_binding(value):
        return False
    import tempfile
    path = binding_path(value["harness"], value["host_pid"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".session-shell-", delete=False) as output:
        temp = Path(output.name)
        try:
            os.chmod(temp, 0o600)
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    if value["sid"] != "-":  # an unselected seat has no session to tag
        from fleet.session_tags import assign_observed
        assign_observed(value["harness"], value["sid"], pid=value["native_pid"], proc_start=value["native_start"])
    return True


def _plain(value, limit=256):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in value).split())[:limit]


def _codex_footer_plan(values, sid, pid, status_rows, painted_rows, *, padding="", panel_lines=None, detail_lines=None):
    """Conceal Codex's thread-id field, and known native fields the common area painted.

    The panel names a session by its number and title, so the shell owns the
    managed footer's thread-id field outright: the value stays on the inner
    screen, where shell-wrapped identification reads it. Every other field still
    yields only to the same value the common area actually painted (``common``).
    """
    from fleet.collectors import procscan
    from fleet.render_cells import _half_up
    import codex_tui_config
    env = procscan.read_environ(pid)
    home = env.get("CODEX_HOME")
    if not home and env.get("HOME"):
        home = str(Path(env["HOME"]) / ".codex")
    if not home:
        return None, None
    observed = codex_tui_config.managed_status_line_observation(home, argv=procscan._read_argv(pid))
    path = str(Path(home) / "config.toml")
    if not observed or not sid:
        return None, path
    model, effort, percent = values.get("model"), values.get("effort"), values.get("ctx_pct")
    common = bool(status_rows and (panel_lines is not None or painted_rows[:len(status_rows)] == status_rows)
                  and model and type(percent) in (int, float) and 0 <= percent <= 100)
    painted = ("\n".join(detail_lines) if detail_lines is not None
               else "\n".join("".join(text for text, _key in row) for row in status_rows or ()))
    title = _plain(values.get("title"), 512)
    # Long names may be clipped even in expanded mode. Preserve the native row
    # whenever it carries a full name which the common area did not paint.
    titles = [sid, *([title] if title and title in painted else [])]
    branch = values.get("branch")
    if branch and branch != "no-git" and branch not in painted:
        common = False
    fields = {
        "thread-id": [sid], "thread-title": list(dict.fromkeys(titles)),
        "git-branch": [branch.removesuffix("*")] if branch and branch != "no-git" else None,
        "context-used": ["Context " + str(_half_up(percent)) + "% used"] if common else None,
        "model-with-reasoning": [_plain(model) + (" " + _plain(effort) if effort else "")] if common else None,
    }
    quotas = {label: used for label, used, _reset in values.get("quota_windows") or ()}
    for item, label, labels in (("five-hour-limit", "5h", ("5h",)),
                               ("weekly-limit", "7d", ("7d", "Weekly"))):
        used = quotas.get(label)
        options = []
        if type(used) in (int, float) and 0 <= used <= 100:
            used = _half_up(used)
            for name in labels:
                options.extend((f"{name}: {100-used}% left", f"{name} {100-used}% left",
                                f"{name} {100-used}% remaining", f"{name} {used}% used"))
        fields[item] = options or None
    plan = {"sid": sid, "config": observed, "common": common,
            "fields": [fields.get(item) if item == "thread-id" or common else None for item in observed["items"]],
            "lines": [padding + "".join(text for text, _key in row) for row in status_rows or ()]}
    if panel_lines is not None and detail_lines:
        plan.update(presentation="session-panel", lines=panel_lines, detail_lines=detail_lines)
    return plan, path


def session_questions(harness, sid, *, view=None, retained=()):
    """The exact session's canonical board; retained IDs read receipts only."""
    import time
    import human_wait_state
    import questions
    view = view if isinstance(view, dict) else human_wait_state.cached_view(request=False) or {}
    ids = set(view.get("by_session", {}).get((harness, sid), ()))
    result = []
    for item in view.get("items", ()):
        owner = item.get("owner") or {}
        if (item.get("id") in ids and not item.get("stale")
                and owner.get("harness") == harness and owner.get("session_id") == sid):
            result.append(questions.describe(item, now=time.time()))
        if len(result) >= 8:
            break
    known = {item["id"] for item in result}
    for old in list(retained or ())[:8]:
        if not isinstance(old, dict) or old.get("id") in known or len(result) >= 8:
            continue
        if not isinstance(old.get("id"), str) or not re.fullmatch(r"S[1-4]:[A-Za-z0-9:_-]{1,256}", old["id"]):
            continue
        item = {"id": old["id"], "source": old["id"][:2],
                "owner": {"harness": harness, "session_id": sid}}
        described = questions.describe(item, now=time.time())
        if described.get("revision") == old.get("revision"):
            # No active input comes from a retained display reference.
            described["answerable"] = False
            result.append(described)
            known.add(described["id"])
    return result


def _panel_common_rows(panel, columns):
    """The rows the panel paints for the values Codex's footer repeats, as painted.

    The host yields duplicate footer paint only while every one of these rows is
    on screen exactly; a wrapped or joined row simply keeps the footer visible.
    """
    from fleet.explorer.grid import cell_width
    text = lambda name: "".join(part["text"] for part in panel.get(name) or ()).strip()
    head, selected, context, quota = text("head"), text("selected"), text("context"), text("quota")
    git = text("git")  # Its own row below the repo row (spec rule 15).
    if columns >= 64:
        first = " │ ".join(part for part in (head, selected, context) if part)
        rows = [first, git] if cell_width(first) <= columns - 4 else [" │ ".join(p for p in (head, selected) if p), context, git]
    else:
        rows = [head, git, selected, context]
    return [row for row in rows + [quota] if row]


_SENT_CACHE: dict = {}


def _sent_peer_messages(sid, limit=3):
    """This session's own sent peer records (``to`` and ``summary``), newest last.

    One bounded tail read of the existing ledger shard this session writes,
    reparsed only when the file changes; nothing else is opened. The record
    keeps a 200-character summary, not the whole body.
    """
    try:
        peer = _peer_message()
        shard = peer._ledger_path(sid)
        info = shard.stat()
    except Exception:
        return [], []
    key = (str(shard), info.st_ino, info.st_size, info.st_mtime_ns)
    held = _SENT_CACHE.get(sid)
    if held and held[0] == key:
        return held[1], [str(shard)]
    rows = []
    try:
        with shard.open("rb") as stream:
            stream.seek(max(0, info.st_size - 32768))
            raw = stream.read(32768).decode("utf-8", "replace").splitlines()
        for line in raw[1:] if info.st_size > 32768 else raw:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            source = record.get("from") if isinstance(record, dict) else None
            if (not isinstance(source, dict) or source.get("session_id") != sid
                    or not isinstance(record.get("summary"), str) or not isinstance(record.get("ts"), str)):
                continue
            try:
                from datetime import datetime
                at = datetime.fromisoformat(record["ts"].replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            rows.append({"to": record.get("to") if isinstance(record.get("to"), dict) else {},
                         "summary": record["summary"][:400], "at": at})
    except OSError:
        return [], [str(shard)]
    rows = rows[-limit:]
    _SENT_CACHE.clear()
    _SENT_CACHE[sid] = (key, rows)
    return rows, [str(shard)]


# This backend's last observation of its one exact live native session. A source
# that was not read keeps these cells (Fleet's native-reader rule); the next
# projection reads it again. Another SID, PID or start never borrows them.
_LAST_OBSERVED: dict = {}


def _observe_session(observe, harness, sid, pid, start):
    key = (harness, sid, pid, start)
    values, watched = observe(harness, pid, start, sid, previous=_LAST_OBSERVED.get(key, {}))
    _LAST_OBSERVED.clear()
    if values:
        _LAST_OBSERVED[key] = dict(values)
    return values, watched


def panel_projection(harness: str, sid: str, *, native_pid=None, native_start=None,
                     columns=100, rows=30, state="unknown", expanded=True, native_context=None,
                     native_badges=None, retained_questions=(), pane_binding=None, answer_busy=False) -> dict:
    """No collection pass: read the same exact native display and question APIs."""
    import time
    registry_root()
    import human_wait_state
    import session_tidy
    import questions
    from fleet.prompt_line import last_input
    from fleet.session_tags import assigned_tag
    from fleet.session_line import line_segments
    from fleet.session_line import status_lines
    from fleet.session_block import block_lines, block_sections, question_presentations
    from fleet.palette import style_components
    from fleet.config import ui_text
    from fleet.session_view import observe
    from fleet.session_work import observe as observe_work, open_count
    from fleet.session_panel import presentation
    from fleet.session_line import peer_message_rows
    counts, route = human_wait_state.cached_statusline(harness, sid)
    view = human_wait_state.cached_view(request=False) or {}
    seat = session_tidy.resolve_seat(harness, env={}, sid=sid)
    display = session_tidy.prompt_display(seat, harness, sid)
    received = display.get("received") or []
    peers = ["⇄ " + _plain(row.get("sender") or row.get("sender_harness") or "peer")
             + " · " + _plain(row.get("text"), 160) for row in received[-2:]]
    tag = assigned_tag(harness, sid)
    tag = f"[{tag}]" if tag and not str(tag).startswith("[") else str(tag or "[?]")
    values, watched = _observe_session(observe, harness, sid, native_pid, native_start) if native_pid else ({}, [])
    # The native OpenCode sidebar already owns this display value. It does not
    # establish an effective policy, process liveness or input readiness.
    if (harness == "opencode" or harness == "codex" and values.get("ctx_pct") is None) and type(native_context) in (int, float) and 0 <= native_context <= 100:
        values["ctx_pct"] = native_context
    mcp = native_badges.get("mcp") if harness == "opencode" and isinstance(native_badges, dict) else None
    if (isinstance(mcp, dict) and set(mcp) == {"total", "connected", "failed"}
            and all(type(mcp[key]) is int and 0 <= mcp[key] <= 999 for key in mcp)):
        values["mcp"] = mcp
    if harness == "claude" and isinstance(native_badges,dict):
        values.update(native_route=native_badges.get('native_route'),native_wait=native_badges.get('native_wait'))
    values.update(harness=harness, tag=tag, native_sid=sid if harness == "codex" else None,
                  state=state, route_label=_plain(route),
                  answer_wait=(counts[0] if counts[0] >= 0 else None) if counts and counts[1] > 0 else (0 if counts else None))
    board = session_questions(harness, sid, view=view, retained=retained_questions)
    no_color = "NO_COLOR" in os.environ
    colors = 256 if "256color" in os.environ.get("TERM", "") or os.environ.get("COLORTERM") else 8
    def styled(parts):
        return [{"text": text, **style_components(key, colors=colors, no_color=no_color)} for text, key in parts]
    segments = styled(line_segments(values, columns))
    work, work_paths = observe_work(values.get("cwd"))
    watched.extend(work_paths)
    human_line = last_input(display, time.time())
    lines = block_lines(values, columns=columns, rows=rows, questions=board, route=_plain(route),
                        received=peers, human=human_line,
                        work=(str(open_count(work)) + " open · " + _plain(work[0].get("headline"))) if work else "")
    prefix, tail = block_sections(values, columns=columns, route=_plain(route), received=peers,
                                 human=human_line,
                                 work=(str(open_count(work)) + " open · " + _plain(work[0].get("headline"))) if work else "")
    question_presentations(board, max(20, columns - 4), native_wait=values.get("native_wait"))
    for item in board:
        for question in item.get("questions") or ():
            question["display"] = {key: ([styled(line) for line in value] if key != "head" else styled(value))
                                   for key, value in question["display"].items()}
    labels = {key: ui_text("fleet.session_block." + key) for key in (
        "open", "submitted", "received", "applied", "expired", "cancelled", "unknown", "unavailable",
        "choose", "send", "navigate", "text", "changed", "complete", "busy", "empty", "more", "more_choose")}
    if columns < 64:
        for key in ("open", "submitted", "received", "applied", "expired", "cancelled", "unknown", "unavailable"):
            labels[key] = ui_text("fleet.session_block.compact_" + key)
    if pane_binding:
        try:
            from fleet import herdr_projection
            projector = getattr(herdr_projection, "project_panel", None)
            if projector is not None:
                # Mirror the local single action slot for optional metadata;
                # this restrictive display hint never grants input authority.
                available = [dict(item, answerable=False, reason="action-pending")
                             if answer_busy and item.get("answerable") else item for item in board]
                projector(pane_binding, values, available)
        except Exception:
            pass  # Optional metadata cannot interrupt this terminal's display.
    native_footer = None
    native_owned = (native_badges or {}).get("owned", ()) if isinstance(native_badges, dict) else ()
    panel = presentation(values, view, harness, sid, columns, native_owned=native_owned)
    # A result line (a continue that was not sent) otherwise waits for the next hook,
    # which in a cleared window is the person's next input. Display only. Its own
    # field: Claude's `notice` below is the native status row's segments.
    notice, notice_paths = session_tidy.waiting_notice_display(seat, harness, sid)
    panel["tidy_notice"] = _plain(notice, 400)
    watched.extend(notice_paths)
    panel["model"] = styled(panel["model"])
    panel["status"] = [styled(line) for line in panel["status"]]
    panel["summary"] = [styled(line) for line in panel["summary"]]
    panel["session"] = [styled(line) for line in panel["session"]]
    panel["brief"] = [styled(line) for line in panel["brief"]]
    for name in ("head", "git", "selected", "context", "quota", "jobs", "mcp"):
        panel[name] = styled(panel[name])
    for row in panel["routes"]:
        paint = {}
        for name, key in (("glyph", "glyph_key"), ("harness", "harness_key"), ("model_text", "model_key"),
                          ("effort", "effort_key"), ("ctx", "ctx_key")):
            if row.get(name):
                paint[name] = styled([(row[name], row.get(key) or "dim")])[0]
        for name in ("gauge", "gauge5"):
            if row.get(name):
                paint[name] = styled([tuple(part) for part in row[name]])
        row["paint"] = paint
    # The repo's open work, as the earlier panel's tail row read it.
    panel["work"] = styled([("◌ ", "dim"), (str(open_count(work)) + " open · " + _plain(work[0].get("headline")), "loc_repo")]) if work else []
    sent, sent_paths = _sent_peer_messages(sid)
    watched.extend(sent_paths)
    panel["messages"] = peer_message_rows(received, sent, assigned_tag)
    if harness == "claude":
        from fleet.session_panel import notice_line
        panel["notice"] = styled(notice_line(values, panel["routes"], board, display, time.time()))
    panel["styles"] = {key: style_components(key, colors=colors, no_color=no_color)
                       for key in ("dim", "loc_repo", "g_idle", "g_work", "g_dead", "g_blocked", "lvl_g", "lvl_y", "lvl_r",
                                   "g_spin", "g_work_off")}
    panel.update(colors=colors, no_color=no_color)  # The panel's theme and shade map these.
    if harness == "codex" and native_pid:
        native_footer, config_path = _codex_footer_plan(values, sid, native_pid,
                                                       status_lines(values, columns - 4, wrap_long=columns < 64), lines, padding="  ",
                                                       panel_lines=_panel_common_rows(panel, columns),
                                                       detail_lines=["".join(p["text"] for p in row) for row in panel["status"]])
        if config_path:
            watched.append(config_path)
    watched.extend((str(human_wait_state._cache_path()),
                    str(session_tidy._prompt_seq_path(session_tidy.session_seat(seat, harness, sid)))))
    return {"footer": "".join(row["text"] for row in segments), "segments": segments,
            "watch": watched, "lines": [styled(line) for line in lines],
            "questions": board, "context": values.get("ctx_pct"), "native_footer": native_footer,
            "block": {"prefix": [styled(line) for line in prefix], "tail": [styled(line) for line in tail],
                      "labels": labels, "styles": {key: style_components(key, colors=colors, no_color=no_color)
                      for key in ("dim", "loc_repo", "g_blocked", "lvl_g", "lvl_r")}},
            "native_display": {"model": values.get("model"), "effort": values.get("effort"),
                               "context_percent": values.get("ctx_pct")},
            "panel": panel, "help": ui_text("fleet.session_block.editor_help")}


def _block_action(binding, request):
    """Only existing, read-only person entry points; never model/terminal input."""
    registry_root()
    key = request.get("action")
    if key == "r":
        from peer_steward_status import _peer_status_rows, _peer_status_line
        rows, omitted = _peer_status_rows(binding["sid"], binding["harness"])
        return {"ok": True, "lines": [_peer_status_line(row) for row in rows[:32]] or ["✉ ·"]}
    if key == "w":
        from fleet.session_view import observe
        from fleet.session_work import observe as work
        value, _ = observe(binding["harness"], binding["native_pid"], binding["native_start"], binding["sid"])
        rows, _ = work(value.get("cwd"))
        return {"ok": True, "lines": [_plain(row.get("headline")) for row in rows[:32]] or ["◌ ·"]}
    if key == "raw":
        # The route page's owner output, read once when the person opens the page,
        # through the same reader Fleet uses when its route page is opened.
        # Only the routes the page shows (the panel's own rows), each through the
        # current owner Fleet's route row names; a row without one is not guessed.
        import human_wait_state
        from fleet.explorer.focus import read_attempts, read_owner_raw
        from fleet.explorer.output import OutputReader
        view = human_wait_state.cached_view(request=False) or {}
        shown = (view.get("route_panels") or {}).get(binding["harness"] + "\0" + binding["sid"])
        routes = []
        for row in shown[:8] if isinstance(shown, list) else ():
            route = row.get("id") if isinstance(row, dict) else None
            owner = row.get("owner_attempt") if isinstance(row, dict) else None
            if (isinstance(route, str) and re.fullmatch(r"rt-[0-9a-f]{16}", route)
                    and isinstance(owner, str) and owner):
                routes.append((route, owner))
        rows = read_attempts(None, binding["sid"]) if routes else []
        reader, attempts, budget = OutputReader(), [], 40000
        for route, owner in routes[:4]:
            raw = read_owner_raw(route, owner, rows, {}, reader)
            lines = []
            for text in reversed((raw or {}).get("lines") or ()):
                text = _plain(text, 240)
                if not text or len(lines) >= 30 or budget - len(text.encode()) < 0:
                    continue
                budget -= len(text.encode())
                lines.append(text)
            if lines:
                attempts.append({"route": route, "att": _plain(raw.get("att"), 64), "lines": lines[::-1]})
        return {"ok": True, "attempts": attempts}
    if key == "\r":
        import subprocess
        from fleet.explorer.focus import session_entries
        routes = []
        for _line, _fields, meta in reversed(session_entries(None, binding["sid"])):
            route = meta.get("owner_route_id") or meta.get("route_id")
            if route and route not in routes:
                routes.append(route)
        lines = []
        for route in routes[:2]:
            if not re.fullmatch(r"rt-[0-9a-f]{16}", route):
                continue
            run = subprocess.run([sys.executable, str(Path(__file__).with_name("workflow-supervisor.py")),
                "status", "--route", route, "--json"], capture_output=True, text=True, timeout=10)
            try:
                value = json.loads(run.stdout)
                lines.append(_plain(value.get("capability") or "route") + " · "
                             + _plain(value.get("derived_workflow_state") or value.get("workflow_state") or "?"))
                for name, node in (value.get("nodes") or {}).items():
                    lines.append("  " + _plain(name) + " · " + _plain(node.get("state") or "?"))
            except ValueError:
                lines.append("?")
        return {"ok": True, "lines": lines or ["·"]}
    return {"ok": False, "reason": "unknown-action"}


def _block_answer(binding, request):
    """An explicit person action, using questions answer's existing exact request path."""
    import contextlib
    import io
    import questions
    import human_wait_state
    from types import SimpleNamespace
    if binding.get("harness") == "codex" and not answer_attachment_current(binding):
        return {"ok": False, "reason": "current-session-unverified"}
    wanted = request.get("question_id")
    revision = request.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        return {"ok": False, "reason": "question-changed"}
    if not isinstance(wanted, str) or len(wanted) > 512:
        return {"ok": False, "reason": "question-changed"}
    board = human_wait_state.cached_view(request=False)
    item, error = questions._find(board or {}, wanted)
    if error or (item.get("owner") or {}).get("harness") != binding["harness"] or (
            item.get("owner") or {}).get("session_id") != binding["sid"] or item.get("stale"):
        return {"ok": False, "reason": "question-changed"}
    answers = request.get("answers")
    decision = request.get("decision")
    # Keep one command implementation for CLI, Fleet and block. No shell text,
    # second store, alternate send or optimistic receipt.
    out = io.StringIO()
    args = SimpleNamespace(id=wanted, choice=[], text=None, text_file=None,
                           decision=decision, answers_file=None, json=True,
                           expected_revision=revision,
                           expected_owner={"harness": binding["harness"], "session_id": binding["sid"]})
    if binding["harness"] == "codex":
        args.answer_attachment = dict(binding)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        if answers is not None:
            # The command accepts a dict through its shared implementation below.
            args.answers = answers
        code = questions._answer(args)
    try:
        result = json.loads(out.getvalue())
    except ValueError:
        return {"ok": False, "reason": _plain(out.getvalue()) or "answer-unavailable"}
    return {"ok": code == 0, **result}


def _input_readiness(binding, request):
    """Use the ordinary form/draft judgment on this PTY's parsed native cells."""
    from peer_steward_duties import _screen_ready
    screen = request.get("screen")
    if not isinstance(screen, list) or len(screen) > 256:
        return {"ok": False, "reason": "screen-unavailable"}
    lines, count = [], 0
    for runs in screen:
        if not isinstance(runs, list):
            return {"ok": False, "reason": "screen-unavailable"}
        line = []
        for run in runs:
            if (not isinstance(run, list) or len(run) != 2 or not isinstance(run[0], str)
                    or type(run[1]) is not bool):
                return {"ok": False, "reason": "screen-unavailable"}
            count += len(run[0])
            if count > 64 * 1024:
                return {"ok": False, "reason": "screen-unavailable"}
            line.extend((char, run[1]) for char in run[0])
        lines.append(line)
    reason = _screen_ready(binding["harness"], lines)
    idle = request.get("state") == "idle"
    if binding["harness"] == "claude":
        from fleet import session_registry
        record = session_registry.read("claude", binding["native_pid"])
        idle = bool(record and record.get("pid") == binding["native_pid"]
                    and str(record.get("procStart")) == binding["native_start"]
                    and record.get("sessionId") == binding["sid"] and record.get("status") == "idle")
    return {"ok": True, "idle": idle, "ready": idle and reason is None and valid_binding(binding),
            "reason": reason or (None if idle else "native-idle-unobserved")}


def _backend_operation(request: dict) -> dict:
    op = request.get("op")
    binding = request.get("binding") or {}
    if op in {"display-settings", "display-bootstrap", "display-model"}:
        # Local display preferences need no SID, but still only our live parent.
        host = process(binding.get("host_pid"))
        if not host or host["pid"] != os.getppid() or host["start"] != binding.get("host_start"):
            return {"ok": False, "reason": "attachment-changed"}
        if op == "display-model":
            from pane_ownership import codex_footer_threads
            lines = request.get("lines")
            if (binding.get("harness") != "codex" or not isinstance(lines, list)
                    or len(lines) > 1000 or any(not isinstance(line, str) for line in lines)
                    or sum(len(line) for line in lines) > 60000):
                return {"ok": False}
            ids = codex_footer_threads(lines, current_id_only=True)
            if len(ids) != 1:
                return {"ok": False}
            display_sid = next(iter(ids))
            candidate = {**binding, "sid": display_sid}
            if not valid_binding(candidate):
                return {"ok": False}
            registry_root()
            from fleet.native_display import codex_selected_model
            values = codex_selected_model(candidate["native_pid"], candidate["native_start"], display_sid)
            return {"ok": bool(values) and valid_binding(candidate), "display": values}
        if op == "display-bootstrap":
            registry_root()
            from fleet.session_panel import presentation
            from fleet.palette import style_components
            panel = presentation({}, {}, binding.get("harness"), "", 40)
            no_color = "NO_COLOR" in os.environ
            colors = 256 if "256color" in os.environ.get("TERM", "") or os.environ.get("COLORTERM") else 8
            panel["model"] = [{"text": text, **style_components(key, colors=colors, no_color=no_color)} for text, key in panel["model"]]
            panel["status"] = []
            panel["summary"] = []
            panel["session"] = panel["brief"] = []
            for name in ("head", "git", "selected", "context", "quota", "jobs", "mcp"):
                panel[name] = [{"text": text, **style_components(key, colors=colors, no_color=no_color)}
                               for text, key in panel[name]]
            panel["styles"] = {key: style_components(key, colors=colors, no_color=no_color)
                               for key in ("dim", "loc_repo", "g_idle", "g_work", "g_dead", "lvl_g", "lvl_y", "lvl_r",
                                           "g_spin", "g_work_off")}
            panel.update(colors=colors, no_color=no_color)
            return {"ok": True, "panel": panel}
        try:
            return {"ok": True, "settings": save_display_policy(request.get("settings"))}
        except (OSError, ValueError, TypeError):
            return {"ok": False, "reason": "settings-unavailable"}
    if op == "unregister":
        host = process(binding.get("host_pid"))
        if not host or host["pid"] != os.getppid() or host["start"] != binding.get("host_start"):
            return {"ok": False, "reason": "attachment-changed"}
        path = binding_path(binding.get("harness"), host["pid"])
        try:
            if _private_json(path, MAX_RECORD_BYTES) == binding:
                path.unlink()
        except FileNotFoundError:
            pass
        return {"ok": True}
    # Only the live Node parent can own this subprocess's attachment.
    if binding.get("host_pid") != os.getppid() or not valid_binding(binding):
        return {"ok": False, "reason": "attachment-changed"}
    harness, sid = binding["harness"], binding["sid"]
    if op == "register":
        return {"ok": publish_binding(binding)}
    if op == "selection":
        selected = _codex_selected_root(binding)
        return {"ok": bool(selected and selected.get("selected_sid")), **(selected or {})}
    if op == "projection":
        columns = request.get("columns", 100)
        if type(columns) is not int or not 20 <= columns <= 10000:
            return {"ok": False, "reason": "invalid-display-size"}
        rows = request.get("rows", 30)
        if type(rows) is not int or not 8 <= rows <= 10000:
            return {"ok": False, "reason": "invalid-display-size"}
        return {"ok": True, **panel_projection(harness, sid,
            native_pid=binding["native_pid"], native_start=binding["native_start"],
            columns=columns, rows=rows, state=request.get("state", "unknown"), expanded=request.get("expanded") is True,
            native_context=request.get("native_context"),native_badges=request.get("native_badges"),
            retained_questions=request.get("retained_questions", ()), pane_binding=binding,
            answer_busy=request.get("answer_busy") is True)}
    if op == "answer":
        return _block_answer(binding, request)
    if op == "action":
        return _block_action(binding, request)
    if op == "readiness":
        return _input_readiness(binding, request)
    if op == "payload":
        readiness = _input_readiness(binding, request)
        if not readiness.get("ready"):
            return {"ok": False, "reason": readiness.get("reason") or "not-idle-empty"}
        peer = _peer_message()
        row = peer._read_pending(request.get("ref"))
        claim = request.get("claim")
        if (not row or row.get("state") != "pending" or row.get("no_trailer")
                or not isinstance(claim, dict) or row.get("rpc_claim") != claim
                or not peer._pending_target_matches(row, {"harness": harness, "session_id": sid})):
            return {"ok": False, "reason": "peer-claim-changed"}
        owner = process(claim.get("pid"))
        if not owner or owner["start"] != claim.get("start"):
            return {"ok": False, "reason": "claimant-unavailable"}
        text = row.get("text")
        if (not isinstance(text, str) or not text.strip() or len(text.encode()) > 32768
                or any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in text)):
            return {"ok": False, "reason": "unsafe-payload"}
        # The source's existing first-write predicate belongs here, after the
        # final native observation, while the reservation is still pending.
        # The live sender owns this claim; the private parent owns this PTY.
        begun = peer.begin_pending_write(row, recipient={"harness": harness, "session_id": sid},
            receipt="session-shell-inflight", claimant={"pid": owner["pid"], "start": owner["start"]})
        if begun is None:
            return {"ok": False, "reason": "peer-claim-changed"}
        return {"ok": True, "text": text}
    return {"ok": False, "reason": "unknown-operation"}


def _backend() -> int:
    for line in sys.stdin:
        request = {}
        try:
            if len(line.encode()) > MAX_CONTROL_BYTES:
                raise ValueError("request too large")
            request = json.loads(line)
            result = _backend_operation(request)
        except Exception:
            result = {"ok": False, "reason": "source-unavailable"}
        print(json.dumps({"id": request.get("id"), **result}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["backend"]:
        raise SystemExit(_backend())
    raise SystemExit("session_shell.py is an internal attachment client")
