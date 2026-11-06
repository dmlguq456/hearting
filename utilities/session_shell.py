"""Optional native-terminal attachment: exact identity and private local control.

This module does not own messages, questions, roles or delivery receipts.
Attachments are derived entries in the existing Fleet session registry; every
control call checks current process births and the connected peer again.
"""
from __future__ import annotations

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


def policy(environ: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """Absence and invalid configuration both preserve the original CLI."""
    try:
        value = _private_json(policy_path(environ), MAX_RECORD_BYTES)
    except FileNotFoundError:
        return False, "disabled"
    except (OSError, ValueError, TypeError):
        return False, "config-unavailable"
    if not isinstance(value, dict) or set(value) != {"enabled"} or type(value["enabled"]) is not bool:
        return False, "config-invalid"
    return value["enabled"], "enabled" if value["enabled"] else "disabled"


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
    return bool(host and native and host["start"] == value.get("host_start")
                and native["start"] == value.get("native_start")
                and host["pid"] != native["pid"]
                and _descends(native["pid"], host["pid"]))


def read_binding(path: Path, *, harness: str | None = None, sid: str | None = None) -> dict | None:
    try:
        value = _private_json(path, MAX_RECORD_BYTES)
        if not valid_binding(value, harness=harness, sid=sid):
            return None
        if path != binding_path(value["harness"], value["host_pid"]):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def bindings(*, harness: str | None = None, sid: str | None = None) -> list[dict]:
    if harness is not None and harness not in HARNESSES:
        return []
    rows = []
    for name in sorted((harness,) if harness else HARNESSES):
        for path in (registry_root() / name).glob("session-shell-*.json"):
            row = read_binding(path, harness=name, sid=sid)
            if row is not None:
                rows.append(row)
    return rows


def exact_binding(harness: str, sid: str) -> tuple[dict | None, str]:
    rows = bindings(harness=harness, sid=sid)
    return (rows[0], "") if len(rows) == 1 else (None, "attachment-ambiguous" if rows else "attachment-unavailable")


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


def lifecycle(harness: str, event: str, sid: str, *, env=None) -> None:
    """Silent native hook report, separate from all prompt classification."""
    env = os.environ if env is None else env
    endpoint, token = env.get("HEARTING_SHELL_SOCKET"), env.get("HEARTING_SHELL_TOKEN")
    if not endpoint or not token or harness not in HARNESSES or not _SID.fullmatch(sid or ""):
        return
    import session_tidy
    from pane_ownership import _caller_runtime
    if session_tidy.is_worker(env):
        return
    # The official callback is the SID source. A native registry can appear
    # after SessionStart, so requiring its prior SID would lose that first
    # report forever. The host separately checks this owner's exact PTY.
    owner, service = _caller_runtime(os.getpid(), harness)
    if owner is None or service:
        return
    me = process(owner[0])
    if me is None or me["start"] != str(owner[5]):
        return
    import time
    req = {"op": "event", "harness": harness, "sid": sid, "event": event,
           "token": token, "pid": me["pid"], "start": me["start"], "seq": time.monotonic_ns() // 1000}
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
    from fleet.session_tags import assign_observed
    assign_observed(value["harness"], value["sid"], pid=value["native_pid"], proc_start=value["native_start"])
    return True


def _plain(value, limit=256):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in value).split())[:limit]


def panel_projection(harness: str, sid: str, *, native_pid=None, native_start=None,
                     columns=100, state="unknown", expanded=False) -> dict:
    """Display only, using the same route/question and human-input sources."""
    import time
    registry_root()
    import human_wait_state
    import session_tidy
    from fleet.config import ui_text
    from fleet.explorer.live import _last_input
    from fleet.session_tags import assigned_tag
    from fleet.session_line import line_segments
    from fleet.palette import style_components
    from fleet.session_view import observe
    # The producer owns route/question interpretation; no Fleet collection here.
    counts, route = human_wait_state.cached_statusline(harness, sid)
    view = human_wait_state.cached_view(request=False) or {}
    ids = set(view.get("by_session", {}).get((harness, sid), ()))
    seat = session_tidy.resolve_seat(harness, env={}, sid=sid)
    display = getattr(session_tidy, "prompt_display", lambda *a, **k: {"human_state": "unknown"})(seat, harness, sid)
    human_line = _last_input(display, time.time())
    received = display.get("received") or []
    peers = ["⇄ " + _plain(row.get("sender") or row.get("sender_harness") or "peer") + ": " + _plain(row.get("text"), 160)
             for row in received[-2:]]
    tag = assigned_tag(harness, sid)
    tag = f"[{tag}]" if tag and not str(tag).startswith("[") else str(tag or "[?]")
    count = counts[1] if counts is not None else None
    values, watched = observe(harness, native_pid, native_start, sid) if native_pid else ({}, [])
    values.update(harness=harness, tag=tag, state=state, route_label=_plain(route), answer_wait=count)
    no_color = "NO_COLOR" in os.environ
    colors = 256 if "256color" in os.environ.get("TERM", "") or os.environ.get("COLORTERM") else 8
    segments = [{"text": text, **style_components(key, colors=colors, no_color=no_color)}
                for text, key in line_segments(values, columns)]
    open_fleet = ui_text("fleet.session_panel.open_fleet")
    title = ui_text("fleet.session_panel.title", tag=tag, harness=harness,
                    collapse=ui_text("fleet.memory.collapse"))
    question_lines = []
    if expanded:
        import questions
        import time
        for item in view.get("items", ()):
            if item.get("id") not in ids or item.get("stale"):
                continue
            description = questions.describe(item, now=time.time())
            rows = description.get("questions") or []
            question_lines.extend(_plain(row.get("question")) or ui_text("wait.title") for row in rows)
            if not rows:
                question_lines.append(_plain(description.get("kind")) + " · " + open_fleet)
            if len(question_lines) >= 3:
                break
    watched.extend((str(human_wait_state._cache_path()),
                    str(session_tidy._prompt_seq_path(session_tidy.session_seat(seat, harness, sid)))))
    return {"footer": "".join(row["text"] for row in segments), "segments": segments,
            "watch": watched, "lines": [title,
            "ROUTES  " + (_plain(route) or "?"),
            f"QUESTIONS {count if count is not None else '?'} · {open_fleet}",
            *question_lines[:3], *peers, human_line]}


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
    if op == "projection":
        columns = request.get("columns", 100)
        if type(columns) is not int or not 20 <= columns <= 10000:
            return {"ok": False, "reason": "invalid-display-size"}
        return {"ok": True, **panel_projection(harness, sid,
            native_pid=binding["native_pid"], native_start=binding["native_start"],
            columns=columns, state=request.get("state", "unknown"), expanded=request.get("expanded") is True)}
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
