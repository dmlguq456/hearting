"""Exact native display sources shared by Fleet and session attachments."""
import json
import math
import os
import stat


def codex_selected_model(pid, start, sid):
    """One loaded thread's official display values; no selection/input effect.

    A visible ID is only a read address. This does not subscribe, resume, bind,
    write a registry, or claim that the thread belongs to an input recipient.
    """
    import re
    import time
    from .collectors import codex, procscan
    if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", sid):
        return {}
    if procscan.read_proc_start(pid) != start or codex._live_tui_start(pid) != start:
        return {}
    client = None
    try:
        endpoint = codex._control_socket(procscan.read_environ(pid))
        stamp = codex._control_socket_identity(endpoint)
        if stamp is None:
            return {}
        from codex_queue_delivery import _connect
        deadline = time.monotonic() + .75
        client = _connect(endpoint, .75, deadline=deadline)
        response = codex._daemon_request(client, "thread/read", {"threadId": sid, "includeTurns": False}, deadline)
        thread = response.get("thread")
        if (not isinstance(thread, dict) or thread.get("id") != sid
                or codex._thread_source(thread) != "user"
                or not isinstance(thread.get("status"), dict)
                or thread["status"].get("type") not in {"idle", "active"}
                or procscan.read_proc_start(pid) != start
                or codex._control_socket_identity(endpoint) != stamp):
            return {}
        model, effort = thread.get("model"), thread.get("reasoningEffort")
        if not isinstance(model, str) or not model or len(model) > 128 or re.search(r"[\x00-\x1f\x7f]", model):
            return {}
        return {"model": model, "effort": effort if effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"} else ""}
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return {}
    finally:
        if client is not None:
            client.close()


def claude_callback(path, sid, pid, start, *, raise_unread=False):
    """Read the existing bounded, exact-session display tap, never latest-global.

    With ``raise_unread`` a tap that exists but was not read (an I/O error, or
    a replacement during the read) raises OSError instead of returning None.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > 65536:
                return None
            raw = os.read(fd, 65537)
            stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if len(raw) != before.st_size or stamp(os.fstat(fd)) != stamp(before) or stamp(path.lstat()) != stamp(before):
                if raise_unread:
                    raise InterruptedError("display tap replaced during the read")
                return None
        finally:
            os.close(fd)
        value = json.loads(raw)
    except OSError as error:
        from .session_registry import unread_error
        if raise_unread and unread_error(error):
            raise
        return None
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("session_id") != sid:
        return None
    if ("pid" in value or "proc_start" in value) and (
            value.get("pid") != pid or str(value.get("proc_start")) != start):
        return None
    return value


def percentage(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100 else None


def claude_percentage(value):
    window = value.get("context_window")
    if not isinstance(window, dict):
        return None
    used = percentage(window.get("used_percentage"))
    if used is not None:
        return used
    current = window.get("current_usage")
    capacity = window.get("context_window_size")
    if not isinstance(current, dict) or type(capacity) not in (int, float) or not math.isfinite(capacity) or capacity <= 0:
        return None
    parts = [current.get(key) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    if not all(type(n) in (int, float) and math.isfinite(n) and n >= 0 for n in parts):
        return None
    return min(100, 100 * sum(parts) / capacity)


def claude_quota(callback):
    """Native /usage windows, without borrowing another session's latest tap."""
    limits = callback.get("rate_limits") or {}
    if not isinstance(limits, dict):
        return {}
    result = {}
    for name, suffix in (("five_hour", "5h"), ("seven_day", "7d")):
        row = limits.get(name) or {}
        if not isinstance(row, dict):
            continue
        used = percentage(row.get("used_percentage"))
        if used is not None:
            result["rl_" + suffix] = int(used)
            result["rs_" + suffix] = row.get("resets_at")
    models = {}
    for name, label in (("seven_day_opus", "opus"), ("seven_day_sonnet", "sonnet"),
                        ("seven_day_overage_included", "fable"), ("seven_day_oauth_apps", "apps")):
        row = limits.get(name) or {}
        used = percentage(row.get("used_percentage")) if isinstance(row, dict) else None
        if used is not None:
            models[label] = round(used)
    for row in limits.get("model_scoped") or ():
        if not isinstance(row, dict):
            continue
        used = row.get("utilization")
        if type(used) in (int, float) and percentage(used * 100) is not None:
            label = str(row.get("display_name") or "model").split()[0].lower()
            models.setdefault(label, round(used * 100))
    if models:
        result["rl_ms"] = list(models.items())
    return result


def quota_windows(payload):
    if not isinstance(payload, dict):
        return []
    windows = payload.get("rl_windows") or payload.get("windows")
    if isinstance(windows, list):
        return [(str(row[0]), row[1], row[2] if len(row) > 2 else None)
                for row in windows if isinstance(row, (list, tuple)) and len(row) >= 2]
    return [(label, payload.get("rl_" + label), payload.get("rs_" + label))
            for label in ("5h", "7d")]


def claude_fields(callback):
    """Display values supplied by this native callback, without a collection pass."""
    model = callback.get("model") or {}
    model = model if isinstance(model, dict) else {}
    effort = callback.get("effort") or {}
    effort = effort if isinstance(effort, dict) else {}
    workspace = callback.get("workspace") or {}
    workspace = workspace if isinstance(workspace, dict) else {}
    quota = claude_quota(callback)
    return {"harness": "claude", "cwd": callback.get("cwd") or workspace.get("current_dir"),
            "model": model.get("display_name") or model.get("id"), "effort": effort.get("level"),
            "ctx_pct": claude_percentage(callback), "quota_windows": quota_windows(quota),
            "weekly_pct": quota.get("rl_7d"), "model_quotas": quota.get("rl_ms") or []}



def write_claude_tap(path, raw, environ):
    """Keep a finite initial refresh from overwriting a newer native callback."""
    import fcntl,json,os,tempfile
    from contextlib import ExitStack
    from pathlib import Path
    lock=Path(environ['HEARTING_SHELL_SOCKET']).parent/'claude-first-display.json'
    try:
        with ExitStack() as stack:
            try:guard=stack.enter_context(lock.open('r+'))
            except OSError:guard=None
            if guard:fcntl.flock(guard,fcntl.LOCK_EX)
            stamp=float(environ['HEARTING_STATUS_OBSERVED_AT'])
            previous={}
            try:
                if path.stat().st_size<=65536:previous=json.loads(path.read_text())
            except (OSError,ValueError):pass
            if previous.get('hearting_display_observed_at',0)>stamp:return
            value=json.loads(raw);value['hearting_display_observed_at']=stamp
            payload=json.dumps(value,ensure_ascii=False)
            if len(payload.encode())>65536:return
            with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix='.'+path.name,delete=False) as stream:
                stream.write(payload);temp=stream.name
            os.replace(temp,path)
    except (OSError,ValueError,TypeError):return

