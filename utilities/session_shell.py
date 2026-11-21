"""Optional native-terminal attachment: exact identity and private local control.

This module does not own messages, questions, roles or delivery receipts.
Attachments are derived entries in the existing Fleet session registry; every
control call checks current process births and the connected peer again.
"""
from __future__ import annotations

import fnmatch
import hashlib
import contextlib
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
                    "position": "right", "theme": "terminal", "shade": "none", "route_line": "time",
                    "worker_box": "closed", "rule_motion": "rule"}
# Appearance choices. An unknown value falls back to its default on its own and
# never disables the shell, so a hand-edited or newer value cannot cost the panel.
APPEARANCE = {"position": ("right", "left"), "theme": ("terminal", "light", "mono"),
              "shade": ("none", "light", "medium")}
# What a folded route row carries besides its title, harness and context:
# its elapsed time or its model (spec rule 7). How a running session moves (spec E9 rule 47,
# 11-15): a light over the head rule under a lit frame (`rule`, the default), one arc round the
# frame (`border`), a wave round it (`scanners`), or nothing (`off`). Same per-key fallback: the
# `on` saved before 11-15 reads as `rule`, and the file is not rewritten for it.
# 11-18 (the person: "박스 여부는 내가 골라가면서 볼 수 있게 그냥 옵션에 좀 넣어줘"): `worker_box`, how the ROUTE box draws a stage's workers:
# in a `closed` box (the default), an `open` one (its top line and left side), or `none` (the stage row and the `↳` indent).
CHOICES = {**APPEARANCE, "route_line": ("time", "model"), "worker_box": ("closed", "open", "none"),
           "rule_motion": ("rule", "border", "scanners", "off")}


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


def release_behind(binding: dict) -> bool | None:
    """Whether this window's display runs an older release than its launch path names now.

    The host's own rule for its ``New release waiting`` line (host.cjs ``detectRelease``): the
    release it runs is not the one its launch path (``HEARTING_SHELL_HOST_PATH``) resolves to
    today. The release it runs is read from the backend it started from its own directory,
    the launch path from its environment; None when either cannot be read.
    """
    if not valid_binding(binding):
        return None
    host = Path("/proc") / str(binding["host_pid"])
    try:
        launch = next((entry.split(b"=", 1)[1] for entry in (host / "environ").read_bytes().split(b"\0")
                       if entry.startswith(b"HEARTING_SHELL_HOST_PATH=")), b"")
        children = set()
        for task in os.listdir(host / "task"):
            with contextlib.suppress(OSError):
                children.update((host / "task" / task / "children").read_text().split())
    except OSError:
        return None
    if not launch:
        return None
    for child in sorted(children):
        try:
            argv = (Path("/proc") / child / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(argv) >= 3 and argv[1].endswith(b"/session_shell.py") and argv[2] == b"backend":
            running = Path(os.fsdecode(argv[1])).resolve().parent
            return running != Path(os.fsdecode(launch)).resolve().parent.parent
    return None


# Why an answer the person sent still waits on them (rule 5): a resting session takes it only at
# a prompt typed in that window. An older shell's host may not take a newer answer (11-16: a
# v4.29.3 host refused every v4.40.0 answer), and a Claude session outside the shell has no
# idle wake. Codex takes it from its own queue; OpenCode's plugin takes it when idle. A session
# still working takes it when its turn ends, so it waits on no one; nor is a reason given for a
# session whose state is not known.
ANSWER_WAIT_OLD_SHELL = "old-shell"
ANSWER_WAIT_NO_SHELL = "no-shell"
# Fleet's session states (``fleet.model.classify_session``) of a session that rests: idle, never
# used, or silent past the session window with its process alive.
RESTING_STATES = frozenset({"idle", "unused", "stale"})


def answer_delivery_wait(harness: str, sid: str, state: str | None = None) -> str:
    """``old-shell`` | ``no-shell`` | ``""``: what a sent answer to this session waits for.

    ``state`` is the session's Fleet state; only a resting one waits on the person."""
    if (state not in RESTING_STATES or harness not in ("claude", "opencode")
            or not isinstance(sid, str) or not _SID.fullmatch(sid)):
        return ""
    binding, reason = exact_binding(harness, sid)
    if binding is None:
        return ANSWER_WAIT_NO_SHELL if harness == "claude" and reason == "attachment-unavailable" else ""
    return ANSWER_WAIT_OLD_SHELL if release_behind(binding) else ""


def mark_answer_waits(items, states=None):
    """Mark each sent message answer of the shared board with what it waits for (``answer_wait``).

    Read live by a surface (Fleet's collector), never stored: a window's shell and release are
    not part of the answer record. ``states`` maps ``(harness, session_id)`` to the session's
    Fleet state; a session missing from it is not known and gets no reason. A wait with no
    session to take it says that instead.
    """
    states = states or {}
    for item in items or ():
        if (isinstance(item, dict) and item.get("answer") == "sent" and item.get("kind") == "message"
                and item.get("owner_state") not in ("none", "successor")):
            owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
            harness = item.get("harness") or owner.get("harness")
            sid = item.get("session_id") or owner.get("session_id")
            wait = answer_delivery_wait(harness, sid, states.get((harness, sid)))
            if wait:
                item["answer_wait"] = wait
            else:
                item.pop("answer_wait", None)
    return items


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


# Why a shell seat's screen could not be read; tidy names the cause in its one notice line.
SCREEN_HOST_OUTDATED = "shell-host-outdated"   # a host started before it learned `screen`
SCREEN_EDITOR_OPEN = "shell-editor-open"       # settings, the Fleet explorer or a question holds the keys
SCREEN_NO_ANSWER = "shell-no-answer"           # no or no usable answer


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
    return native_screen_reason(binding)[0]


def native_screen_reason(binding: dict):
    """``(lines | None | PASSTHROUGH, reason)``: :func:`native_screen` and, for None, why."""
    result = control(binding, "screen")
    if isinstance(result, dict) and result.get("ok") is False:
        if result.get("reason") == "display-unavailable":
            return PASSTHROUGH, ""
        if result.get("reason") == "unsupported-operation":
            return None, SCREEN_HOST_OUTDATED
    if isinstance(result, dict) and result.get("ok") and result.get("focused") is True:
        return None, SCREEN_EDITOR_OPEN
    screen = (result.get("screen") if isinstance(result, dict) and result.get("ok")
              and result.get("focused") is False else None)
    if not isinstance(screen, list) or len(screen) > 512:
        return None, SCREEN_NO_ANSWER
    lines = []
    for runs in screen:
        if not isinstance(runs, list):
            return None, SCREEN_NO_ANSWER
        line = []
        for run in runs:
            if (not isinstance(run, list) or len(run) != 2 or not isinstance(run[0], str)
                    or type(run[1]) is not bool):
                return None, SCREEN_NO_ANSWER
            line.extend((char, run[1]) for char in run[0])
        lines.append(line)
    return lines, ""


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


def _hold(binding, status):
    """Why the window keeps a message, in the receiving host's own word (``status().hold``).

    A host from before that word names only its state; an older release than the one installed
    takes the message once its window is reopened or typed in (``old-shell``)."""
    hold = status.get("hold") if status.get("ok") else ""
    if isinstance(hold, str) and hold:
        return hold
    if status.get("state") == "blocked":
        return "form"
    if status.get("state") in ("working", "submitting"):
        return "busy"
    return ANSWER_WAIT_OLD_SHELL if status.get("ok") and "hold" not in status and release_behind(binding) else "unobserved"


def deliver_pending(ref: str, harness: str, sid: str) -> dict:
    """Use the existing peer claim once; socket acceptance is not a receipt."""
    if harness not in ("claude", "opencode"):
        return {"ok": False, "reason": "native-queue-required", "not_started": True}
    # A request revision superseded or cancelled before delivery is not sent (peer_request).
    peer = _peer_message()
    try:
        row = peer._read_pending(ref)
    except Exception:
        row = None
    if row and peer._request_withdrawn(row):
        return {"ok": False, "reason": "request-withdrawn", "not_started": True}
    binding, reason = exact_binding(harness, sid)
    if binding is None:
        return {"ok": False, "reason": reason, "not_started": True}
    before = control(binding, "status")
    if not before.get("ok") or before.get("state") != "idle" or not before.get("empty"):
        return {"ok": False, "reason": "not-idle-empty", "hold": _hold(binding, before), "not_started": True}
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


def lifecycle(harness: str, event: str, sid: str, *, env=None, source: str = "") -> None:
    """Silent native hook report, separate from all prompt classification.

    A start carries the native's own start reason (``clear`` keeps the process and its
    model, spec E9 rule 34 (f))."""
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
    if event == "start" and source in {"startup", "resume", "clear", "compact"}:
        req["source"] = source
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


_NOTICE_TAG = re.compile(r"^\[[^\]]{1,16}\]\s*")
_NOTICE_BATCH = re.compile(r"^(?:묶음|batch)\s+[A-Za-z0-9][A-Za-z0-9._-]{0,63}:\s*")
_NOTICE_RANGE = re.compile(r"(?:,\s*)?\b(?:byte|rowid)\s+[\d,]+–[\d,]+\s*/\s*\S+\s+[\d,]+")


def quiet_notice_line(text):
    """The panel's line for a tidy result that ended well (11-16): the words a person reads, without
    the machine's own (the `[tidy]` tag, the batch id, the undo command and a byte or rowid range), under
    the one lead word `session_shell.tidy_done`. The notice itself (what the session receives) is unchanged."""
    line = " ".join(str(text or "").split())
    at = line.rfind("mem tidy-undo")
    cut = line.rfind(" — ", 0, at) if at >= 0 else -1
    if cut >= 0:
        line = line[:cut]
    line = _NOTICE_BATCH.sub("", _NOTICE_TAG.sub("", line))
    line = re.sub(r"\(\s*\)", "", _NOTICE_RANGE.sub("", line))
    line = " ".join(line.split()).strip(" ·")
    from fleet.config import ui_text
    return ui_text("session_shell.tidy_done", text=line) if line else ""


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


_RECEIPT_STATES = frozenset({"submitted", "received", "applied"})


def session_questions(harness, sid, *, view=None, retained=(), elsewhere=None):
    """The exact session's canonical board; retained IDs read receipts only.

    Only what the person can answer here now is a question in this panel (11-14): the
    answerable ones first, then the person's own answers still showing their receipt. A wait
    not answerable here (its existing ``answerable``/``reason``) is no question in the block;
    ``elsewhere`` (a list, when given) receives it, for the one guidance line. An answer the
    person sent and that still travels (``answer: sent``) is out of the session's wait index
    and in no count, yet comes here as its receipt: the panel's one dim line (rule 5).
    """
    import time
    import human_wait_state
    import questions
    view = view if isinstance(view, dict) else human_wait_state.cached_view(request=False) or {}
    ids = set(view.get("by_session", {}).get((harness, sid), ()))
    answerable, receipts, current = [], [], set()
    for item in view.get("items", ()):
        owner = item.get("owner") or {}
        if ((item.get("id") in ids or item.get("answer") == "sent") and not item.get("stale")
                and owner.get("harness") == harness and owner.get("session_id") == sid):
            current.add(item.get("id"))
            described = questions.describe(item, now=time.time())
            if described.get("answerable") is True:
                answerable.append(described)
            elif described.get("request_state") in _RECEIPT_STATES:
                receipts.append(described)
            elif elsewhere is not None:
                elsewhere.append(described)
    result = (answerable + receipts)[:8]
    # A current wait left out above stays out: a retained display reference keeps only a
    # question that has left this session's waits (its receipt), never one judged here.
    known = {item["id"] for item in result} | current
    for old in list(retained or ())[:8]:
        if not isinstance(old, dict) or old.get("id") in known or len(result) >= 8:
            continue
        if not isinstance(old.get("id"), str) or not re.fullmatch(r"S[1-4]:[A-Za-z0-9:_-]{1,256}", old["id"]):
            continue
        item = {"id": old["id"], "source": old["id"][:2],
                "owner": {"harness": harness, "session_id": sid}}
        described = questions.describe(item, now=time.time())
        if described.get("revision") == old.get("revision"):
            # No active input comes from a retained display reference; it has left this
            # session's waits, so an open one reads as closed (spec E9 rule 54 (e)).
            if described.get("answerable"):
                described["reason"] = "retained-display-no-longer-current"
            described["answerable"] = False
            result.append(described)
            known.add(described["id"])
    return result


def _footer_path(cwd):
    """The working folder as the last line writes it: the home folder as ``~`` (spec E9 rule 60)."""
    text = _plain(cwd, 1024) if isinstance(cwd, str) else ""
    home = os.path.expanduser("~").rstrip("/")
    if text and home and (text == home or text.startswith(home + "/")):
        text = "~" + text[len(home):]
    return text


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
        # Beside the native area the Git row is painted without the worktree count (spec E9
        # rule 37 (a)): the row compared is the row painted.
        side_git = text("git_side") if panel.get("git_side") is not None else git
        rows = [head, side_git, selected, context]
    return [row for row in rows + [quota] if row]


def messages_since_input(messages, display):
    """Spec E9 rule 49 (c): the folded strip's `MESSAGES ←N`, the kept messages received since
    the person's last input in this session (every kept one before any input). Display only."""
    human_at = ((display or {}).get("human") or {}).get("at")
    human_at = human_at if type(human_at) in (int, float) else 0
    return sum(1 for row in messages or () if isinstance(row, dict) and row.get("way") == "←"
               and type(row.get("at")) in (int, float) and row["at"] > human_at)


_SENT_CACHE: dict = {}
_REQUEST_BODIES: dict = {}  # a sealed body never changes, so its text is kept by transfer ref
_REQUEST_BODY_BYTES = 16 * 1024
_REQUEST_VIEWS = 24


def _request_body(peer, ref):
    """The sealed body of one request revision, at most 16 KiB (a read page, not a transfer)."""
    held = _REQUEST_BODIES.get(ref)
    if held is not None:
        return held
    try:
        payload = peer.read_peer_payload(ref) or {}
        raw = str(payload.get("body") or "").encode("utf-8")[:_REQUEST_BODY_BYTES]
        text = raw.decode("utf-8", "ignore")
    except Exception:
        return ""
    if len(_REQUEST_BODIES) > 64:
        _REQUEST_BODIES.clear()
    _REQUEST_BODIES[ref] = text
    return text


def _request_views(harness, sid, now, errors=None):
    """``(views, watched)``: what this session sent and received as requests, read from the
    request records (the index only narrows which; never the recent-message tail), with the
    delivery rows of their messages. The files read are watched so a change repaints the panel.

    A read that fails shows no request lines, and says why: its reason is appended to
    ``errors`` (the panel keeps it beside its content, never on screen)."""
    try:
        import peer_request
        listing = peer_request.session_requests(harness, sid)
    except Exception as exc:  # noqa: BLE001 - a display read never stops the panel
        if errors is not None:
            errors.append("%s: %s" % (type(exc).__name__, str(exc)[:96]))
        return [], []
    views, watched = [], []
    with contextlib.suppress(Exception):
        watched.append(str(peer_request.index_path()))  # a new request repaints the panel
    if not listing["out"] and not listing["in"]:
        return [], watched
    peer = _peer_message()
    for side in ("in", "out"):
        # Open work is never cut for its count; only closed requests are, and only
        # the first views read a body (the panel opens one at a time).
        closed_views = 0
        for index, record in enumerate(listing[side]):
            closed = record["state"] in ("final", "rejected", "cancelled")
            if closed and now - float(record.get("updated") or 0) > 7 * 86400:
                continue
            if closed:
                closed_views += 1
                if closed_views > _REQUEST_VIEWS:
                    continue
            refs = []
            for revision in record["revisions"]:
                refs.append(revision["transfer_ref"])
            refs += [r.get("transfer_ref") for r in record["reports"]]
            refs.append((record.get("cancel") or {}).get("notice_ref"))
            rows = {}
            for ref in refs:
                if not isinstance(ref, str) or not ref:
                    continue
                try:
                    rows[ref] = peer._read_pending(ref)
                    watched.append(str(peer._pending_path(ref)))
                except Exception:
                    rows[ref] = None
            with contextlib.suppress(Exception):
                watched.append(str(peer_request._record_path(record["id"])))
            current = next((r for r in reversed(record["revisions"]) if r.get("state") == "current"),
                           record["revisions"][-1] if record["revisions"] else {})
            gone = False
            if side == "out" and not closed and current.get("transfer_ref"):
                try:
                    from peer_obligations import ObligationStore
                    duty = ObligationStore().get("message-" + current["transfer_ref"]) or {}
                    gone = (duty.get("state") == "cancelled"
                            and (duty.get("observation") or {}).get("reason") == "recipient-session-ended")
                except Exception:
                    gone = False
            body = ""
            if not closed and current.get("transfer_ref") and index < _REQUEST_VIEWS:
                body = _request_body(peer, current["transfer_ref"])
            views.append({"record": record, "side": side, "rows": rows, "gone": gone, "body": body})
    return views, watched


_SEALED_TRANSFERS: dict = {}


def _without_request_transfers(received, sent):
    """The recent messages without the transfers of a request (the request itself, a follow-up,
    a report, a cancel notice, a late answer): the route box's request line and its opened
    detail show that work once, so the message lists do not repeat it. A transfer is a request's
    when it carries the sealed ``request`` field (a received one through its transfer record, a
    sent one through the request ref its ledger record names). Display only."""
    import peer_request
    peer = _peer_message()

    def sealed(ref):
        # A transfer record never changes once written, so a read one is remembered.
        if ref in _SEALED_TRANSFERS:
            return _SEALED_TRANSFERS[ref]
        try:
            record = peer._read_transfer_record(ref)
            found = bool(record) and peer_request.valid_seal(record.get("request"))
        except Exception:  # noqa: BLE001 - an unreadable record stays an ordinary message
            return False
        if record:
            _SEALED_TRANSFERS[ref] = found
            if len(_SEALED_TRANSFERS) > 256:
                _SEALED_TRANSFERS.pop(next(iter(_SEALED_TRANSFERS)))
        return found

    # Only a peer message is looked up: a runtime notice never reaches the message list.
    kept_received = [row for row in received or ()
                     if not (isinstance(row, dict) and row.get("source") == "peer"
                             and isinstance(row.get("ref"), str) and sealed(row["ref"]))]
    kept_sent = [row for row in sent or ()
                 if not peer_request.delivery_ref_refs({"refs": row.get("refs")})]
    return kept_received, kept_sent


def _sent_peer_messages(sid, limit=8):
    """This session's own sent peer records (``to``, ``summary``, ``kind`` and the
    delivery ``status``; a later record of the same message updates its status), newest last.

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
            delivery = record.get("delivery") if isinstance(record.get("delivery"), dict) else {}
            status = delivery.get("status") if isinstance(delivery.get("status"), str) else None
            message_id = record.get("message_id") if isinstance(record.get("message_id"), str) else None
            earlier = next((row for row in rows if message_id and row.get("message_id") == message_id), None)
            if earlier is not None:
                earlier["status"] = status or earlier.get("status")
                continue
            rows.append({"to": record.get("to") if isinstance(record.get("to"), dict) else {},
                         "summary": record["summary"][:400], "at": at, "message_id": message_id,
                         "kind": record.get("kind") if isinstance(record.get("kind"), str) else None,
                         "status": status,
                         "refs": [ref for ref in (record.get("refs") or ())[:8] if isinstance(ref, str)]
                         if isinstance(record.get("refs"), list) else []})
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
    values, watched = observe(harness, pid, start, sid, previous=_LAST_OBSERVED.get(key, {}), wake_quota=True)
    _LAST_OBSERVED.clear()
    if values:
        _LAST_OBSERVED[key] = dict(values)
    return values, watched


# The shared style keys the panel paints by name (state glyphs and frame, harness names,
# the steward mark's two beats); the rest arrive already styled.
PANEL_STYLE_KEYS = ("dim", "loc_repo", "g_idle", "g_work", "g_dead", "g_blocked", "lvl_g", "lvl_y", "lvl_r",
                    "g_spin", "g_work_off", "tag_steward", "tag_steward_dim",
                    "zone_direct", "zone_dispatch", "zone_request",
                    # 11-18: the five fields' hues for a route's kind word (`kind_`) and its bracket (dim, plain, bold).
                    *("kind%s_%s" % (weight, field) for field in ("code", "lab", "design", "docs", "research")
                      for weight in ("", "q", "s", "b")),
                    "hb_claude", "hb_codex", "hb_opencode", "hb_other",
                    # Fleet's stage colors (`render_stages.route_stage_key`): a running stage lit, the others dimmed.
                    *("stg%d_%s" % (i, on) for i in range(5) for on in ("on", "off")))


def _panel_read_target(harness, sid, native_pid, native_start):
    """The session this panel READS: ``(sid, pid, start, parked)``.

    A Claude window whose conversation was sent to the background (``/bg``, or the left arrow
    on an empty prompt) stays the shell's viewer of the old session id, while the conversation
    goes on as another session in another process (``session_registry.parked_successor``, read
    live each time; it never stores the link). The panel then draws that session's route
    chain, context, NOW, peers and requests. Input, delivery and questions keep the host's own
    ids: only what the panel reads changes. No link, or any doubt, reads the host's own."""
    if harness == "claude" and native_pid:
        try:
            from fleet import session_registry
            found = session_registry.parked_successor(
                harness, sid, viewer_pid=int(native_pid), viewer_start=native_start)
        except Exception:
            found = None
        if found:
            return found["session_id"], found["pid"], found["proc_start"], found
    return sid, native_pid, native_start, None


def panel_projection(harness: str, sid: str, *, native_pid=None, native_start=None,
                     columns=100, rows=30, state="unknown", expanded=True, native_context=None,
                     native_badges=None, retained_questions=(), pane_binding=None, answer_busy=False,
                     upgrade_note="", native_model=None, popup_columns=None, table_columns=None, side=False) -> dict:
    """No collection pass: read the same exact native display and question APIs. ``side``: the panel stands beside the native area
    and open, so it draws RESOURCES (the shared producer takes the host sample only for such a panel)."""
    import time
    registry_root()
    import human_wait_state
    import session_tidy
    import questions
    from fleet.prompt_line import last_input
    from fleet.session_tags import assigned_tag
    from fleet.session_line import line_segments, fresh_part
    from fleet.session_line import status_lines
    from fleet.session_block import block_lines, block_sections, question_presentations
    from fleet.palette import style_components
    from fleet.config import ui_text
    from fleet.session_view import observe
    from fleet.session_work import observe as observe_work, open_count, recent_memory
    from fleet.session_panel import MEMORY_ITEMS, presentation, request_rows, resource_rows, usage_rows
    from fleet.session_line import peer_message_rows
    read_sid, read_pid, read_start, parked = _panel_read_target(harness, sid, native_pid, native_start)
    counts, route = human_wait_state.cached_statusline(harness, read_sid)
    view = human_wait_state.cached_view(request=False) or {}
    if side and expanded:
        human_wait_state.note_resources_wanted()
    seat = session_tidy.resolve_seat(harness, env={}, sid=sid)
    display = session_tidy.prompt_display(seat, harness, sid)
    received = display.get("received") or []
    peers = ["⇄ " + _plain(row.get("sender") or row.get("sender_harness") or "peer")
             + " · " + _plain(row.get("text"), 160) for row in received[-2:]]
    tag = assigned_tag(harness, read_sid)
    tag = f"[{tag}]" if tag and not str(tag).startswith("[") else str(tag or "[?]")
    values, watched = _observe_session(observe, harness, read_sid, read_pid, read_start) if native_pid else ({}, [])
    if parked:
        # The window's own record ends the link when it changes: watch it for the next projection.
        from fleet import session_registry
        watched.append(os.fspath(Path(session_registry.registry_path("claude", parked["viewer_pid"]))))
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
    # Fleet's steward mark, from the same role evidence Fleet's collector reads (markers and
    # relations); no collection pass. A failed read shows no mark. It is read for the session
    # the panel shows (``read_sid``): beside another session's number it would be that one's mark.
    try:
        from fleet.collectors import steward as steward_role
        values["steward"] = bool(steward_role.role_flag(harness, read_sid, cwd=values.get("cwd")))
    except Exception:
        values["steward"] = False
    elsewhere = []
    board = session_questions(harness, sid, view=view, retained=retained_questions, elsewhere=elsewhere)
    # The answer wait this panel counts is every wait of this session a person must answer
    # (11-14), wherever it is answered: the questions its block answers, and the ones answered
    # in another place (a Claude native request, a marker question), which stand as one line
    # saying where (``wait_guide``). A finished one (closed, expired, an answer on its way)
    # waits for no one; a blocked owner is its parent agent's.
    answerable = sum(1 for item in board if item.get("answerable") is True
                     and (item.get("request_state") or "open") == "open")
    from fleet.session_block import unanswerable_state
    def place(item):
        state = item.get("request_state")
        return (state if state in {"expired", "cancelled"} or state == "unknown" and not item.get("reason")
                else unanswerable_state(item.get("reason"), own=True))
    elsewhere = [item for item in elsewhere if human_wait_state.person_wait(item)
                 and place(item) not in {"expired", "cancelled", "answer_closed", "answer_sending"}]
    waiting = answerable + len(elsewhere)
    values.update(harness=harness, tag=tag, native_sid=sid if harness == "codex" else None,
                  state=state, route_label=_plain(route),
                  answer_wait=(None if not counts else waiting if waiting
                               else None if counts[0] < 0 and counts[1] > 0 else 0))
    if elsewhere:
        word = ui_text("fleet.session_block." + ("compact_" if columns < 64 else "") + place(elsewhere[0]))
        values["wait_place"] = word
        values["wait_guide"] = str(len(elsewhere)) + " " + ui_text("fleet.session_block.questions") + " · " + word
    no_color = "NO_COLOR" in os.environ
    colors = 256 if "256color" in os.environ.get("TERM", "") or os.environ.get("COLORTERM") else 8
    # A terminal that says it takes 24-bit color (the COLORTERM it already reads): the moving
    # rule's breath may use it (spec E9 rule 47); every other color stays a 256-color index.
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        colors = 1 << 24
    def styled(parts):
        # Spec E9 rule 50 (d): Git's `↑N`/`↓N` are marked so the panel can read them as the
        # rest of the Git words where it shows no hue (mono, eight colors).
        return [{"text": text, **style_components(key, colors=colors, no_color=no_color),
                 **({"role": "git-fresh"} if fresh_part(text, key) else {})} for text, key in parts]
    segments = styled(line_segments(values, columns))
    # Spec E9 rules 37 (b), 46 (c): the repo's newest saved memory records, five at most (11-16; the
    # journal Fleet reads, read again only when it changed); their headlines ride the open-work
    # query below.
    memory_rows, memory_paths = recent_memory(values.get("cwd"), MEMORY_ITEMS)
    work, work_paths = observe_work(values.get("cwd"),
                                    tuple(row["headline_id"] for row in memory_rows if row.get("headline_id")))
    if memory_rows:
        values["memory_recent"] = [{**row, "headline": work.headlines.get(str(row.get("headline_id")), "")}
                                   for row in memory_rows]
        values["memory_latest"] = values["memory_recent"][0]
    watched.extend(work_paths)
    watched.extend(memory_paths)
    human_line = last_input(display, time.time())
    lines = block_lines(values, columns=columns, rows=rows, questions=board, route=_plain(route),
                        received=peers, human=human_line,
                        work=(str(open_count(work)) + " open · " + _plain(work[0].get("headline"))) if work else "")
    prefix, tail = block_sections(values, columns=columns, route=_plain(route), received=peers,
                                 human=human_line,
                                 work=(str(open_count(work)) + " open · " + _plain(work[0].get("headline"))) if work else "")
    # 11-15: a question reads whole in the popup at its own width; the panel draws each
    # question's summary at the panel's width inside its frame.
    editor = popup_columns or max(20, columns - 4)
    question_presentations(board, editor, native_wait=values.get("native_wait"), brief_columns=columns - 4)
    for item in board:
        for question in item.get("questions") or ():
            question["display"] = {key: ([styled(line) for line in value] if key not in ("head", "brief_line") else styled(value))
                                   for key, value in question["display"].items()}
    # Spec E9 rule 54: the five `answer_*` words say where a wait not answerable here is answered.
    states = ("open", "submitted", "received", "applied", "expired", "cancelled", "unknown",
              "answer_below", "answer_there", "answer_parent", "answer_sending", "answer_closed")
    labels = {key: ui_text("fleet.session_block." + key) for key in (
        *states, "choose", "send", "navigate", "text", "changed", "complete", "busy", "empty", "more", "more_choose",
        "more_page", "sent", "questions")}
    if editor < 64:
        for key in states:
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
                available += elsewhere  # counted with the rest, answered in their own place
                projector(pane_binding, values, available)
        except Exception:
            pass  # Optional metadata cannot interrupt this terminal's display.
    native_footer = None
    native_owned = (native_badges or {}).get("owned", ()) if isinstance(native_badges, dict) else ()
    panel = presentation(values, view, harness, read_sid, columns, native_owned=native_owned,
                         display=display, now=time.time())
    # 11-18: when the producer last read this session's routes (epoch seconds, down to the minute so the
    # projection changes at most once a minute for it): the route popup's first screen says as of when it stands.
    observed = view.get("observed_at")
    panel["route_read_at"] = (int(observed) // 60 * 60
                              if isinstance(observed, (int, float)) and not isinstance(observed, bool) and 0 < observed < 1e11 else 0)
    # A result line (a continue that was not sent) otherwise waits for the next hook,
    # which in a cleared window is the person's next input. Display only. Its own
    # field: Claude's `notice` below is the native status row's segments.
    notice, notice_paths, notice_level = session_tidy.waiting_notice_display(seat, harness, sid, with_level=True)
    # 11-16 (the person: "알림이 알림이 아니라 경고 같아서"): a line the writer marked as work that ended well
    # (`done`) shows quietly (`tidy_quiet`: dim, no `⚠`), its inner words left out of the panel's line; the line
    # the session receives stays whole. What leaves the person something to do keeps the caution look.
    quiet = bool(notice) and notice_level == "done"
    panel["tidy_notice"] = _plain(quiet_notice_line(notice) if quiet else notice, 400)
    panel["tidy_quiet"] = quiet and bool(panel["tidy_notice"])
    # A newer installed release this window has not switched to yet, or a switch that could
    # not carry the scrollback: the lowest-priority line, never over another notice (quiet too).
    panel["tidy_upgrade"] = False
    # 11-16 (spec E9 rule 60): the same words also stand at the right end of the window's last line, in the
    # place of `hearting v<version>` (the line the shell writes over the native one, in every harness).
    panel["footer_notice"] = ui_text("session_shell.upgrade_" + upgrade_note) if upgrade_note in ("pending", "scrollback") else ""
    panel["footer_path"] = _footer_path(values.get("cwd"))
    if upgrade_note in ("pending", "scrollback") and not panel["tidy_notice"]:
        panel["tidy_notice"] = ui_text("session_shell.upgrade_" + upgrade_note)
        panel["tidy_upgrade"] = upgrade_note == "pending"
        panel["tidy_quiet"] = True
    watched.extend(notice_paths)
    panel["model"] = styled(panel["model"])
    panel["status"] = [styled(line) for line in panel["status"]]
    panel["summary"] = [styled(line) for line in panel["summary"]]
    panel["session"] = [styled(line) for line in panel["session"]]
    panel["brief"] = [styled(line) for line in panel["brief"]]
    quota_raw = panel["quota"]
    for name in ("head", "repo", "repo_side", "git", "git_side", "worktrees", "memory", "selected", "context", "context_side", "quota", "jobs", "mcp", "cost"):
        panel[name] = styled(panel[name])
    # Spec E9 rule 59 (11-16): the USAGE section, the three harnesses' usage rows (the records Fleet keeps).
    panel["usage"] = [{"harness": row["harness"], "parts": styled(row["parts"])} for row in usage_rows(harness, values)]
    # 11-16 (review of #1662): a reading at Fleet's red level carries `level: red`, so the folded strip can keep it
    # (the strip lets the other windows go first) in the place the old notice row gave it.
    panel["quota"] = [{**part, "level": "red"} if key == "lvl_r" else part
                      for part, (_text_, key) in zip(panel["quota"], quota_raw)]
    def route_paint(row):
        paint = {}
        for name, key in (("glyph", "glyph_key"), ("harness", "harness_key"), ("model_text", "model_key"),
                          ("effort", "effort_key"), ("ctx", "ctx_key")):
            if row.get(name):
                paint[name] = styled([(row[name], row.get(key) or "dim")])[0]
        for name in ("gauge", "gauge5", "gauge_row"):
            if row.get(name):
                paint[name] = styled([tuple(part) for part in row[name]])
        if isinstance(row.get("now"), str) and row["now"]:
            paint["now"] = row["now"]
        return paint
    def called_paint(rows):
        for item in rows or ():
            item["paint"] = route_paint(item)
            called_paint(item.get("called"))
    for row in panel["routes"]:
        row["paint"] = route_paint(row)
        # Spec E9 rule 43: the owner apart from a worker that leads the row, and each live worker.
        if isinstance(row.get("owner_paint"), dict):
            row["owner_paint"] = route_paint(row["owner_paint"])
        for worker in row.get("workers_live") or ():
            worker["paint"] = route_paint(worker)
            called_paint(worker.get("called"))
        called_paint(row.get("called"))
    # The repo's open work, as the earlier panel's tail row read it.
    # (`◌` is Fleet's stale route alone: the open work's line has no glyph, popup design F 10.)
    panel["work"] = styled([(str(open_count(work)) + " open · " + _plain(work[0].get("headline")), "loc_repo")]) if work else []
    sent, sent_paths = _sent_peer_messages(sid)
    watched.extend(sent_paths)
    received_plain, sent_plain = _without_request_transfers(received, sent)
    # A message to this session itself (a tidy notice to its own seat) is no message between two sessions.
    # 11-17: a sent message whose pending row has not gone in reads `awaiting delivery` and why, as
    # `peer-steward show` words it; the visible rows' pending rows are read once, and watched.
    def pending_of(ref):
        peer = _peer_message()
        watched.append(str(peer._pending_path(ref)))
        return peer._read_pending(ref)
    panel["messages"] = peer_message_rows(received_plain, sent_plain, assigned_tag, limit=8, own=(harness, sid),
                                          pending_of=pending_of)
    panel["messages_new"] = messages_since_input(panel["messages"], display)
    # Requests sent and received: the open work, read from the request records (not from the
    # recent-message tail, so a request does not leave when newer messages push it out).
    request_errors = []
    request_views, request_paths = _request_views(harness, read_sid, time.time(), request_errors)
    watched.extend(request_paths)
    # 11-16: the other session's model and effort as far as the values Fleet's shared producer already read
    # for the session displays (``session_doers`` in the cache this projection reads above) know them.
    doers = view.get("session_doers") if isinstance(view.get("session_doers"), dict) else {}

    def doer_of(other_harness, other_sid):
        found = doers.get(str(other_harness) + "\0" + str(other_sid))
        return found if isinstance(found, dict) else None
    own_known = doer_of(harness, read_sid) or {}
    panel["requests"] = request_rows(request_views, time.time(), assigned_tag, doer_of, own_repo=own_known.get("repo")) if request_views else []
    # The PEERS line: who supervises whom (the steward records the shared producer already read, `session_peers` in the
    # cache read above; no read of its own), this window's own group first when it is supervised, then the one it leads.
    panel["peers"] = peer_groups((view.get("session_peers") or {}).get(harness + "\0" + read_sid), tag, harness)
    # 11-18: the RESOURCES section (the compute hosts' CPU, memory and GPU use and the numbers of the sessions that hold a GPU): the
    # sample the shared producer already took, in the cache read above (no read of its own).
    panel["resources"] = [{"host": styled(row["host"]), "stats": [styled(item) for item in row["stats"]],
                           "gpus": [{name: styled(parts) for name, parts in gpu.items()} for gpu in row["gpus"]]}
                          for row in resource_rows(view.get("resources"), time.time())]
    for request in panel["requests"]:
        # Painted as a route's doer is: the same harness, model and effort keys.
        request["paint"] = route_paint(request)
    # 11-15: a request body's markdown tables read aligned in the MESSAGES popup (its rows two cells in,
    # at the popup's width); its other lines stay the strings the popup wraps as before.
    from fleet.explorer.transcript import table_parts
    for request in panel["requests"]:
        parts = table_parts(request.get("body") or (), max(8, (table_columns or popup_columns or columns) - 2))
        if any(isinstance(item, list) for item in parts):
            # One item a row: a line the popup wraps (``str``), or a drawn table row (painted parts).
            request["body_parts"] = [row for item in parts for row in
                                     ([item] if isinstance(item, str) else [_runs(row, colors, no_color) for row in item])]
    if request_errors:
        panel["request_error"] = request_errors[0]
    if harness == "claude":
        from fleet.session_panel import notice_line
        # Only what the person must do now (2026-11-14); a waiting release stays in the tidy slot above.
        panel["notice"] = styled(notice_line(values, panel["routes"], board))
    panel["styles"] = {key: style_components(key, colors=colors, no_color=no_color)
                       for key in PANEL_STYLE_KEYS}
    # A popup that could not be read says so in Fleet's own word (`fleet.output.unreadable`).
    panel.setdefault("labels", {})["popup_unread"] = ui_text("fleet.output.unreadable")
    # The route popup's first screen (read before this one, a reading under way): its as-of words.
    panel["labels"]["popup_shown_at"] = ui_text("fleet.session_panel.popup_shown_at")
    panel["labels"]["popup_shown_failed"] = ui_text("fleet.session_panel.popup_shown_failed")
    panel.update(colors=colors, no_color=no_color)  # The panel's theme and shade map these.
    # The model name the host reads off the native's own screen, painted by the rule route rows
    # use (its family's color, else this harness's: ``_model_key(model, harness=...)``), so the
    # panel keeps no family table of its own. Display only.
    if isinstance(native_model, str) and 0 < len(native_model) <= 128 and native_model.isprintable():
        from fleet.session_line import _model_key
        panel["native_model_part"] = styled([(native_model, _model_key(native_model, harness=harness))])[0]
    if harness == "codex" and native_pid:
        native_footer, config_path = _codex_footer_plan(values, sid, native_pid,
                                                       status_lines(values, columns - 4, wrap_long=columns < 64), lines, padding="  ",
                                                       panel_lines=_panel_common_rows(panel, columns),
                                                       detail_lines=["".join(p["text"] for p in row) for row in panel["status"]])
        if config_path:
            watched.append(config_path)
    watched.extend((str(human_wait_state._cache_path()),
                    str(session_tidy._prompt_seq_path(session_tidy.session_seat(seat, harness, sid)))))
    # Spec rule 26 and the 11-12 rule 6: the runtime's own turn record, read with the
    # session above (its files are watched), for the host's own display of this session's
    # state. Display only: it never stands for input readiness.
    turn = values.get("native_turn")
    turn_record = None
    if harness == "claude" and native_pid:
        # The record the host reads itself the moment its watched file changes.
        from fleet.collectors import claude as claude_collector
        turn_record = os.fspath(Path(claude_collector._home()) / "sessions" / f"{int(native_pid)}.json")
    return {"footer": "".join(row["text"] for row in segments), "segments": segments,
            **({"native_turn": turn} if turn in ("idle", "working", "blocked") else {}),
            **({"turn_record": turn_record} if turn_record else {}),
            "watch": watched, "lines": [styled(line) for line in lines],
            "questions": board, "context": values.get("ctx_pct"), "native_footer": native_footer,
            "block": {"prefix": [styled(line) for line in prefix], "tail": [styled(line) for line in tail],
                      "labels": labels, "styles": {**{key: style_components(key, colors=colors, no_color=no_color)
                      for key in ("dim", "loc_repo", "g_blocked", "lvl_g", "lvl_r")},
                      # The chosen choice's `▸` takes this session's harness color (spec E9 rule 53).
                      "harness": style_components("hb_" + str(harness), colors=colors, no_color=no_color)}},
            "native_display": {"model": values.get("model"), "effort": values.get("effort"),
                               "context_percent": values.get("ctx_pct")},
            "panel": panel, "help": ui_text("fleet.session_block.editor_help")}


def peer_groups(entry, own, harness):
    """``{"me", "groups": [{"steward", "members"}]}`` for the PEERS line: each supervisor with this window (it as one
    of the supervised), then this window with the sessions it supervises. Each session is its Fleet number and the
    style key of its harness. None where this window has no supervision relation."""
    if not isinstance(entry, dict):
        return None

    def end(pair):
        harness = pair[1] if pair[1] in ("claude", "codex", "opencode") else "other"
        return {"tag": str(pair[0]), "key": "hb_" + harness}
    me = {"tag": str(own).strip("[]"), "key": end([own, harness])["key"]}
    groups = [{"steward": end(pair), "members": [{**me, "me": True}]} for pair in entry.get("supervisors") or ()]
    if entry.get("targets"):
        groups.append({"steward": {**me, "me": True}, "members": [end(pair) for pair in entry["targets"]]})
    return {"me": me["tag"], "groups": groups} if groups else None


def _runs(row, colors, no_color):
    """A Fleet row of ``(text, key)`` segments as painted parts (a segment with no key: the terminal's own look)."""
    from fleet.palette import style_components
    out = []
    for text, key in row:
        try:
            look = style_components(key, colors=colors, no_color=no_color) if key else {}
        except Exception:  # noqa: BLE001 -- an unknown key draws in the terminal's own look
            look = {}
        out.append({"text": str(text), **look})
    return out


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
        return {"ok": True, "lines": [_plain(row.get("headline")) for row in rows[:32]] or ["·"]}
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
    expected = {"harness": binding["harness"], "session_id": binding["sid"]}
    wanted = request.get("question_id")
    revision = request.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        return {"ok": False, "reason": "question-changed"}
    if not isinstance(wanted, str) or len(wanted) > 512:
        return {"ok": False, "reason": "question-changed"}
    board = human_wait_state.cached_view(request=False)
    item, error = questions._find(board or {}, wanted)
    if error or (item.get("owner") or {}).get("harness") != expected["harness"] or (
            item.get("owner") or {}).get("session_id") != expected["session_id"] or item.get("stale"):
        return {"ok": False, "reason": "question-changed"}
    answers = request.get("answers")
    decision = request.get("decision")
    # Keep one command implementation for CLI, Fleet and block. No shell text,
    # second store, alternate send or optimistic receipt.
    out = io.StringIO()
    args = SimpleNamespace(id=wanted, choice=[], text=None, text_file=None,
                           decision=decision, answers_file=None, json=True,
                           expected_revision=revision, expected_owner=dict(expected))
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


# 11-15 (popup design B): the routes popup is Fleet's own table, drawn by Fleet's own functions into a
# grid of the popup's width; nothing here draws a table of its own. (The LAB and sessions popups went
# with their panel sections, 11-15.)
POPUP_KINDS = frozenset({"routes", "status", "now", "backlog"})
POPUP_ROWS = 160     # rows a table popup carries at most (the popup pages them with PgUp/PgDn)


def _paint_mode():
    """The colors the panel paints with: ``(colors, no_color)``, as the projection reads them."""
    no_color = "NO_COLOR" in os.environ
    colors = 256 if "256color" in os.environ.get("TERM", "") or os.environ.get("COLORTERM") else 8
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        colors = 1 << 24
    return colors, no_color


def popup_rows(grid, *, colors=256, no_color=False):
    """``(rows, styles, hits)``: a Fleet grid's cells as runs ``[text, style index]`` (Fleet's row
    tints are not drawn: the panel has no shade), trailing blanks and empty rows dropped, and its
    press targets."""
    from fleet.palette import style_components
    styles, index = [], {}

    def style(key):
        if key not in index:
            try:
                value = style_components(key, colors=colors, no_color=no_color) if key else {}
            except Exception:  # noqa: BLE001 -- an unknown key draws in the terminal's own look
                value = {}
            index[key] = len(styles)
            styles.append(value)
        return index[key]
    rows = []
    for cells in grid.cells:
        runs, current, text = [], None, ""
        for char, key, _tint in cells:
            if char == "":
                continue
            if key != current and text:
                runs.append([text, current])
                text = ""
            current = key
            text += char
        if text:
            runs.append([text, current])
        while runs and not runs[-1][0].strip():
            runs.pop()
        if runs and not runs[-1][1]:
            runs[-1][0] = runs[-1][0].rstrip()
        rows.append([[text, style(key)] for text, key in runs])
    while rows and not rows[-1]:
        rows.pop()
    hits = [{"row": hit.y, "left": hit.x0, "right": hit.x1, "action": [str(part) for part in hit.action]}
            for hit in getattr(grid, "hits", ()) if hit.y < min(len(rows), POPUP_ROWS)]
    return rows[:POPUP_ROWS], styles, hits


POPUP_ANSWER_BYTES = 60 * 1024    # what a table's answer may weigh: the host reads 64 KB of one, and an answer past it is no table


def _popup_table(kind, rows, styles, hits, total, *, colors, no_color, **extra):
    """A routes table as the host reads it, whole when it fits and else as many of its rows as fit
    (``POPUP_ROWS`` rows and ``POPUP_ANSWER_BYTES``), with one dim last line for the rest (`외 N개`, the
    entrance's own words): ``total`` is how many routes the table has, and the ones its kept rows show
    are those that have a press target there. ``routes`` lists the shown ones."""
    from fleet.config import ui_text
    from fleet.palette import style_components

    def ident(hit):
        action = hit["action"]
        return action[1] if action[0] == "route" and len(action) > 1 else None

    def build(count):
        kept = [hit for hit in hits if hit["row"] < count]
        shown = list(dict.fromkeys(item for item in map(ident, kept) if item is not None))
        out, painted = rows[:count], styles
        left = max(0, total - len(shown))
        if left:
            try:
                dim = style_components("dim", colors=colors, no_color=no_color)
            except Exception:  # noqa: BLE001 -- an unknown key draws in the terminal's own look
                dim = {}
            painted = [*styles, dim]
            out = [*out, [[ui_text("entrance.more", count=left).strip(), len(styles)]]]
        return {"ok": True, "kind": kind, "rows": out, "styles": painted, "hits": kept, "routes": shown, **extra}

    def weight(result):
        return len(json.dumps({"id": 0, **result}, ensure_ascii=False).encode())
    full = build(len(rows))
    if weight(full) <= POPUP_ANSWER_BYTES:
        return full
    low, high = 0, len(rows)      # the most rows that fit: `low` fits, `high` does not
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (middle, high) if weight(build(middle)) <= POPUP_ANSWER_BYTES else (low, middle)
    return build(low)


def _popup_view_model(harness, sid, kind):
    """Fleet's own view model, from Fleet's own collection (what its explorer shows), read once for
    a person who opened the routes popup; it interprets only this session's routes."""
    from fleet.collectors import collect_all
    from fleet.refresh import as_snapshot
    from fleet.explorer import live
    # The routes popup draws no steward badge; the STATUS popup keeps the collection it had.
    snapshot = as_snapshot(collect_all(usage="cache-only", include_report_verification=False,
                                       route_sessions=[(harness, sid)],
                                       **({"route_view": "routes"} if kind == "routes" else {})))
    return live.build_view_model(snapshot)


# A popup's held backend draws a route unfolded or its page from the collection it made, while the host says
# nothing changed (`reuse`). One slot: (harness, sid) -> (view model, the time it was collected).
_HELD_VIEW = {}


_ROUTE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _status_request(request):
    """``(cwd, route ids)`` a status read names: the session's folder (absolute) and its route ids, else None."""
    cwd, routes = request.get("cwd"), request.get("routes")
    if (not isinstance(cwd, str) or not os.path.isabs(cwd) or len(cwd) > 4096 or "\0" in cwd
            or not isinstance(routes, list) or len(routes) > 64):
        return None
    return cwd, [route for route in routes if isinstance(route, str) and _ROUTE_ID.match(route)][:8]


def _panel_status(binding, request):
    """The panel's status button value (``fleet.session_status.read``), read in its own worker: the host asks
    at its first projection, when this session's routes change and at a watch event on the paths the value
    names, never per paint, so a slow first read holds neither the panel nor a key. No artifact root: None."""
    named = _status_request(request)
    if named is None:
        return {"ok": False, "reason": "invalid-status"}
    registry_root()
    from fleet import session_status
    value = session_status.read(*named)
    have = request.get("have") if isinstance(request.get("have"), dict) else {}
    # (11-21: the two short lists the panel shows under REPO come with it, read only where the document changed since the host's `have`)
    lists = session_status.panel_lists(named[0], value, {key: have.get(key) for key in ("status", "backlog") if isinstance(have.get(key), str)}) if value else None
    return {"ok": True, "status": value, "lists": lists}


def _status_popup(binding, request, columns, colors, no_color, kind="status"):
    """The status popup, in the parts the host asks for (``part``); as ``kind="backlog"`` the backlog popup (the same answer from
    ``<artifact root>/backlog.md``, with no Fleet part). ``doc`` is the whole document at the popup's width, with
    no wait on Fleet's collection (the value the button already read comes with the request: ``session_status.held``, and
    only a request without one reads it); ``fleet`` is Fleet's status lines for this project (``session_status.popup_head_rows``),
    which the host puts above the document when they arrive. A document that cannot be read answers ``ok: False`` (the host
    draws its own dim word and keeps nothing, so the next opening reads it again) and Fleet's part is not asked. Without
    ``part``: both, as one answer. ``list`` (11-21) is the popup's first screen: for the campaign page Hearting generates it
    answers ``list``, the list screen as entries the host lays out by its own state (``session_status.listing_entries``),
    and no rows (the document read whole is asked for with ``doc`` when ``full document ›`` is opened, and is the
    answer it always was, rows and budget, whatever the list weighs); a list that alone passes the answer's size, and any
    other document (a hand-written page, none, a cut one), answers as ``doc`` does, with no ``list``."""
    named = _status_request(request)
    if named is None:
        return {"ok": False, "reason": "invalid-status"}
    part = request.get("part")
    backlog = kind == "backlog"
    if part not in (None, "doc", "fleet", "list") or (backlog and part == "fleet"):
        return {"ok": False, "reason": "invalid-status"}
    from fleet import session_status
    view = {}
    if part != "fleet" and not backlog:
        view = session_status.held(named[0], request.get("status")) or session_status.read(*named)
        if view is None:
            return {"ok": False, "reason": "no-status"}
    rows = []
    if part not in ("doc", "list") and not backlog:
        status = None
        try:
            from fleet.session_tags import assigned_tag
            vm = _popup_view_model(binding["harness"], binding["sid"], "status")
            tag = str(assigned_tag(binding["harness"], binding["sid"]))
            own = next((row for row in tuple(getattr(vm, "sessions", ()) or ()) if str(row.get("tag")) == tag), None)
            repo = next((row for row in tuple(getattr(vm, "repos", ()) or ())
                         if own is not None and str(row.get("id")) == str(own.get("repo_id"))), None)
            status = repo.get("status") if repo is not None else None
        except Exception:  # noqa: BLE001 -- without Fleet's lines the document still reads
            if part == "fleet":
                return {"ok": False, "reason": "no-fleet"}
        rows = session_status.popup_head_rows(status, columns)
    listing, later = None, None
    if part != "fleet":
        if backlog:
            import time
            # (the backlog needs no status value: the folder of this window's status document, which only says which group stands first,
            # is the host's word, kept to a folder name)
            own = request.get("own") if isinstance(request.get("own"), str) and len(request["own"]) <= 200 and "/" not in request["own"] else ""
            doc, listing, shown = session_status.backlog_view(session_status._root(named[0], time.time()), own, columns, want_list=part == "list")
            view = {"title": shown["title"], "has_doc": shown["has_doc"]}
        else:
            doc, listing = session_status.popup_doc(view, columns, want_list=part == "list")
        if doc is None:
            return {"ok": False, "reason": "doc-unreadable"}
        # (with a list the document's rows are made only if the list is dropped, below)
        if callable(doc):
            later, doc = doc, []
        rows = rows + doc
    styles, index = [], {}

    def style(key):
        if key not in index:
            index[key] = len(styles)
            styles.append(_runs([("", key)], colors, no_color)[0] if key else {})
            styles[-1].pop("text", None)
        return index[key]
    # Each run carries its cells as Fleet measured them (``cell_width``), so the host lays the rows out by
    # the same measure Fleet drew them with (its own differs for some characters: Thai marks, jamo, new emoji).
    from fleet.explorer.grid import cell_width

    def lay(rows_of):
        return [[[text, style(key), cell_width(text)] for text, key in row if text] for row in rows_of]
    result = {"ok": True, "kind": kind, "rows": [], "styles": styles, "hits": [], "page": False}
    if part != "fleet":
        result.update(title=view.get("title") or view.get("word") or "", has_doc=view.get("has_doc") is True)
    if part:
        result["part"] = part

    def weight(value):
        return len(json.dumps({"id": 0, **value}, ensure_ascii=False).encode())
    if listing:
        known = len(styles)
        entries = [{**{k: v for k, v in entry.items() if k not in ("rows", "alt", "more")}, "rows": lay(entry["rows"]),
                    **({"alt": lay(entry["alt"])} if entry.get("alt") else {}),
                    **({"more": lay(entry["more"])} if entry.get("more") else {})} for entry in listing]
        # The list screen's answer carries the list alone: the document's rows are the other answer (``doc``), asked for when
        # the document is read whole, so the two never share the answer's size. A list that alone does not fit is left out and
        # the answer is the document's, as it always was.
        listed = {**result, "list": entries}
        if weight(listed) <= POPUP_ANSWER_BYTES:
            return listed
        del styles[known:]            # (the styles only the list used go with it, and the names that pointed at them: the answer is the document's alone)
        for name in [k for k, at in index.items() if at >= known]:
            del index[name]
    runs = lay(rows + (later() if later else []))
    result["rows"] = runs
    if weight(result) <= POPUP_ANSWER_BYTES:
        return result
    low, high = 0, len(runs)      # the most rows that fit with a dim `…` under them
    dim = style("dim")
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (middle, high) if weight({**result, "rows": runs[:middle] + [[["…", dim, 1]]]}) <= POPUP_ANSWER_BYTES else (low, middle)
    return {**result, "rows": runs[:low] + [[["…", dim, 1]]]}


def _now_popup(binding, request, columns, colors, no_color):
    """The NOW popup: this session's story as a list (``timeline.rows``, folded by ``timeline.fold`` where it is
    taller than the popup) or, with ``page``, the conversation one row stands for (``timeline.passage``). Nothing
    is drawn here that the timeline layer does not give; the rows are painted in the panel's own style keys.
    ``open`` is the key of the row the person stands on."""
    from fleet.explorer import timeline, timeline_popup
    from fleet.explorer.grid import cell_width
    wanted = request.get("open") if isinstance(request.get("open"), str) and len(request["open"]) <= 320 else ""
    room = request.get("rows") if type(request.get("rows")) is int and 3 <= request["rows"] <= 200 else 12
    story = timeline.rows(binding["harness"], binding["sid"], events=True)
    page = request.get("page") is True
    target = next((row for row in story if row.get("key") == wanted and row.get("kind") in ("prompt", "now")), None)
    if page and target is None:
        return {"ok": False, "reason": "no-row"}
    if page:
        view = timeline_popup.page_view(target, columns)
        drawn, meta, extra = view["rows"], [{"key": "", "kind": "page", "select": False}] * len(view["rows"]), {
            "title": view["title"], "start": wanted, "folded": False, "read": view["ok"]}
    else:
        view = timeline_popup.list_view(story, columns, room, wanted)
        drawn, meta, extra = view["rows"], view["meta"], {"title": view["title"], "start": view["start"],
                                                          "folded": view["folded"]}
    styles, index = [], {}

    def style(key):
        if key not in index:
            index[key] = len(styles)
            styles.append(_runs([("", key)], colors, no_color)[0] if key else {})
            styles[-1].pop("text", None)
        return index[key]
    runs = [[[text, style(key), cell_width(text)] for text, key in row if text] for row in drawn]
    result = {"ok": True, "kind": "now", "rows": runs, "styles": styles, "hits": [], "page": page, "meta": [],
              **extra}

    def weight(value):
        return len(json.dumps({"id": 0, **value}, ensure_ascii=False).encode())

    def build(keep):
        """The rows ``keep`` names (indices, in order), each with its note; the gap between is one dim ``…``."""
        out, notes, last = [], [], -1
        for number in keep:
            if number != last + 1 and out:
                out.append([["…", style("dim"), 1]])
                notes.append({"key": "", "kind": "gap", "select": False})
            out.append(runs[number])
            notes.append(meta[number])
            last = number
        return {**result, "rows": out, "meta": notes}
    everything = list(range(len(runs)))
    full = build(everything)
    if weight(full) <= POPUP_ANSWER_BYTES:
        return full
    if page:
        # Too much for one answer: a page keeps its top rows and as many of its last ones as fit, with a `…` between.
        head = timeline_popup.PAGE_HEAD_ROWS
        low, high = 0, len(runs) - head      # `low` rows from the end fit, `high` do not
        while high - low > 1:
            middle = (low + high) // 2
            low, high = (middle, high) if weight(build(everything[:head] + everything[len(runs) - middle:])) <= POPUP_ANSWER_BYTES else (low, middle)
        return build(everything[:head] + everything[len(runs) - low:])

    # Too much for one answer: a list keeps its newest rows (the popup opens at the bottom), from a group's start (a
    # date, a divider or a prompt, never a NOW without its prompt), and says how many rows it left off.
    def newest(first):
        kept = build(everything[first:])
        word = timeline_popup._word("now_earlier", count=first)
        return {**kept, "rows": [[[word, style("dim"), cell_width(word)]]] + kept["rows"],
                "meta": [{"key": "", "kind": "gap", "select": False}] + kept["meta"]}
    starts = [number for number, note in enumerate(meta) if note.get("kind") in ("date", "divider", "prompt") and number]
    if not starts or weight(newest(starts[-1])) > POPUP_ANSWER_BYTES:
        starts = everything[1:]              # one group past the answer: any row will do
    low, high = -1, len(starts) - 1          # `starts[high]` fits (or is the last that can); `low` does not
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (low, middle) if weight(newest(starts[middle])) <= POPUP_ANSWER_BYTES else (middle, high)
    return newest(starts[high])


def _panel_popup(binding, request):
    """One popup's content, read when the person opens it and again at the watch events it names."""
    registry_root()
    from fleet.explorer.grid import CellGrid
    kind = request.get("kind")
    columns = request.get("columns")
    if kind not in POPUP_KINDS or type(columns) is not int or not 20 <= columns <= 200:
        return {"ok": False, "reason": "invalid-popup"}
    colors, no_color = _paint_mode()
    harness, sid = binding["harness"], binding["sid"]
    if kind in ("status", "backlog"):
        return _status_popup(binding, request, columns, colors, no_color, kind)
    if kind == "now":
        return _now_popup(binding, request, columns, colors, no_color)
    opened = request.get("open") if isinstance(request.get("open"), str) and len(request["open"]) <= 64 else ""
    held = _HELD_VIEW.get((harness, sid)) if request.get("reuse") is True else None
    if held is None:
        import time
        started = time.time()
        held = (_popup_view_model(harness, sid, kind), started)
        _HELD_VIEW.clear()
        _HELD_VIEW[(harness, sid)] = held
    vm, read_at = held
    grid = CellGrid(columns, POPUP_ROWS + 8)
    from dataclasses import replace
    from fleet.explorer import pages
    from fleet.session_tags import assigned_tag
    tag = str(assigned_tag(harness, sid))
    routes = tuple(route for route in tuple(getattr(vm, "routes", ()) or ()) if str(route.get("session_tag")) == tag)
    ids = [str(route.get("id") or "") for route in routes]
    page = request.get("page") is True and opened in ids
    if page:
        # Fleet's route page with every line of it (the popup reads a longer page on with PgUp/PgDn,
        # so no line is folded into `+N more`): the grid is as tall as the window's rows and the
        # visibility lines together; its stage frame takes what the window's rows leave, never less
        # than a 36-row window leaves (a lower window reads the page on instead).
        room = request.get("rows") if type(request.get("rows")) is int and 6 <= request["rows"] <= 200 else 30
        room = max(room, 30)
        route = next(item for item in routes if str(item.get("id")) == opened)
        grid = CellGrid(columns, room + 3 + sum(pages._route_visibility_rows(route.get("visibility"), 0, columns - 1)))
        subset = replace(vm, routes=routes)
        pages._route_page(grid, subset, opened, 0, columns - 1, grid.rows,
                          pages._SubsetState(None, "route:" + opened, opened, True), whole=True)
    elif routes:
        pages._draw_route_subset(grid, vm, routes, 0, 0, grid.rows - 4, opened=opened, no_repo_group=True)
    rows, styles, hits = popup_rows(grid, colors=colors, no_color=no_color)
    # The route page starts under Fleet's page head: its empty rows above the content go.
    while page and rows and not rows[0]:
        rows.pop(0)
        hits = [{**hit, "row": hit["row"] - 1} for hit in hits if hit["row"] > 0]
    if page:
        return {"ok": True, "kind": kind, "rows": rows, "styles": styles, "hits": hits, "routes": ids[:64], "page": page,
                "read_at": int(read_at)}
    return _popup_table(kind, rows, styles, hits, len(ids), colors=colors, no_color=no_color, page=False,
                        read_at=int(read_at))


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
        # Claude writes `shell` once a turn has ended while a background shell it started still
        # runs (a foreground call holds `busy`; see fleet.model._session_status_state): its composer
        # takes input as at `idle`, so a waiting message goes in (11-16: held for that shell's life).
        # Only this delivery reading counts it so; tidy and Fleet read the record themselves.
        idle = bool(record and record.get("pid") == binding["native_pid"]
                    and str(record.get("procStart")) == binding["native_start"]
                    and record.get("sessionId") == binding["sid"] and record.get("status") in ("idle", "shell"))
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
            for name in ("head", "repo", "git", "selected", "context", "quota", "jobs", "mcp"):
                panel[name] = [{"text": text, **style_components(key, colors=colors, no_color=no_color)}
                               for text, key in panel[name]]
            panel["styles"] = {key: style_components(key, colors=colors, no_color=no_color)
                               for key in PANEL_STYLE_KEYS}
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
        # The question popup's width (11-15); an older host sends none and keeps the panel's.
        popup = request.get("popup_columns")
        popup = popup if type(popup) is int and 20 <= popup <= 10000 else None
        # The MESSAGES popup's inside (a table popup's width); an older host sends none.
        table = request.get("table_columns")
        table = table if type(table) is int and 16 <= table <= 10000 else None
        return {"ok": True, **panel_projection(harness, sid,
            native_pid=binding["native_pid"], native_start=binding["native_start"],
            columns=columns, rows=rows, state=request.get("state", "unknown"), expanded=request.get("expanded") is True,
            popup_columns=popup, table_columns=table,
            native_context=request.get("native_context"),native_badges=request.get("native_badges"),
            retained_questions=request.get("retained_questions", ()), pane_binding=binding,
            answer_busy=request.get("answer_busy") is True,
            upgrade_note=request.get("upgrade_note") if request.get("upgrade_note") in ("pending", "scrollback") else "",
            native_model=request.get("native_model"), side=request.get("side") is True)}
    if op == "answer":
        return _block_answer(binding, request)
    if op == "popup":
        return _panel_popup(binding, request)
    if op == "status":
        return _panel_status(binding, request)
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
        text = peer.pasted_text(row)
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
    try:
        # The panel backend re-reads the same ledgers at every projection; reuse
        # unchanged ones, as the obligation runner does.
        import dispatch_seat_handover
        dispatch_seat_handover.trust_unchanged_ledgers()
    except Exception:
        pass
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
