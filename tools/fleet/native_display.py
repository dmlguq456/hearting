"""Exact native display sources shared by Fleet and session attachments."""
import json
import math
import os
import stat


def claude_callback(path, sid, pid, start):
    """Read the existing bounded, exact-session display tap, never latest-global."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > 65536:
                return None
            raw = os.read(fd, 65537)
            stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if len(raw) != before.st_size or stamp(os.fstat(fd)) != stamp(before) or stamp(path.lstat()) != stamp(before):
                return None
            value = json.loads(raw)
        finally:
            os.close(fd)
    except (OSError, ValueError):
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

