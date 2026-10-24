"""Pure status band and system page built only from what the snapshot already carries.

Every value is displayed, never re-judged: liveness is the job's own, usage percentages come
from the usage snapshots, and a GPU counts as busy when a process is actually on it. A value
the snapshot does not carry is shown as ``?`` — never as 0 and never as a success.
"""

from collections.abc import Mapping

from .grid import clip_cells as clip

HARNESS_ABBR = {"claude": "CL", "codex": "CX", "opencode": "OC"}
LIVENESS_GLYPH = {"working": "⠇", "idle": "·", "unknown": "?", "blocked": "◑"}
LIVENESS_WORD = {"working": "도는 중", "idle": "쉼", "unknown": "확인 불가", "blocked": "답 대기"}

HARNESS_ORDER = ("claude", "codex", "opencode")
_WINDOW_LABEL = {"5h": "5h", "week": "주", "month": "월"}
_WINDOW_ALIAS = {"7d": "week", "wk": "week", "mo": "month"}
_FAILED_SOURCE = ("error", "failed", "unavailable")
PRODUCT = "hearting"


def _get(value, key, default=None):
    if value is None:
        return default
    found = value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
    return default if found is None else found


def _jobs(snapshot):
    return list(_get(snapshot, "jobs", ()) or ())


# -- status band -----------------------------------------------------------------------------

_JOB_WORDS = (("working", "⠇도는 중"), ("idle", "쉼"), ("done", "끝남"), ("unknown", "?확인 불가"),
              ("dead", "종료"))


def liveness_counts(snapshot):
    """Count jobs by their own liveness; a value outside the five known words is 'unknown'."""
    known = {key for key, _word in _JOB_WORDS} | {"blocked"}
    counts = {key: 0 for key in known}
    for job in _jobs(snapshot):
        value = str(_get(job, "liveness", "") or "unknown")
        counts[value if value in known else "unknown"] += 1
    return counts


def job_summary(snapshot):
    counts = liveness_counts(snapshot)
    parts = [f"{word} {counts[key]}" for key, word in _JOB_WORDS]
    if counts["blocked"]:
        parts.insert(2, f"◑답 대기 {counts['blocked']}")
    return " · ".join(parts)


def _hosts(snapshot):
    block = _get(snapshot, "compute_hosts")
    hosts = _get(block, "hosts") if block is not None else None
    return None if hosts is None else list(hosts)


def host_views(snapshot):
    """[(host, reachable, cpu, mem_used, mem_total, [gpu dicts])] or None when unread."""
    hosts = _hosts(snapshot)
    if hosts is None:
        return None
    views = []
    for host in hosts:
        gpus = []
        for gpu in _get(host, "gpus", ()) or ():
            processes = list(_get(gpu, "processes", ()) or ())
            owners = sorted({str(_get(_get(p, "session_owner"), "label")) for p in processes
                             if _get(_get(p, "session_owner"), "label")})
            gpus.append({"index": _get(gpu, "index", 0), "used": _get(gpu, "memory_used_mib"),
                         "total": _get(gpu, "memory_total_mib"), "busy": bool(processes),
                         "owners": owners})
        views.append({"host": str(_get(host, "host", "?")), "reachable": _get(host, "reachable", True) is not False,
                      "cpu": _get(host, "cpu_utilization_pct"), "mem_used": _get(host, "memory_used_mib"),
                      "mem_total": _get(host, "memory_total_mib"), "gpus": gpus})
    return views


def gpu_counts(snapshot):
    """(busy, total) over reachable hosts, or None when compute was not read."""
    views = host_views(snapshot)
    if views is None:
        return None
    gpus = [gpu for view in views if view["reachable"] for gpu in view["gpus"]]
    return sum(1 for gpu in gpus if gpu["busy"]), len(gpus)


def gpu_text(snapshot):
    counts = gpu_counts(snapshot)
    return "GPU ?" if counts is None else f"GPU {counts[0]}/{counts[1]}"


def _age_text(snapshot, name):
    entry = _get(_get(snapshot, "observations", {}) or {}, name)
    age = _get(entry, "age")
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        return f"{name} ?"
    return f"{name} {max(0, int(age))}s"


def age_seconds(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}분" if seconds >= 60 else f"{seconds}초"


def source_ages(snapshot):
    return "·".join(_age_text(snapshot, name) for name in ("jobs", "herdr", "compute"))


def failed_sources(snapshot, focus_result=None, now=None):
    """['관계 읽지 못함 2분', ...] for each source reported failed; the age is its last success."""
    items = []
    for name, health in tuple(getattr(focus_result, "sources", ()) or ()):
        if health is not None and health.state == "failed":
            when = f" {age_seconds(now - health.last_success_at)}" if (
                now is not None and health.last_success_at is not None) else ""
            items.append(f"{_SOURCE_WORD.get(name, name)} 읽지 못함{when}")
    for name, entry in (_get(snapshot, "observations", {}) or {}).items():
        if str(_get(entry, "state", "")) in _FAILED_SOURCE:
            items.append(f"{name} 읽지 못함")
    return items


_SOURCE_WORD = {"relations": "관계", "obligations": "남은 일", "output": "출력", "steward": "관계"}


def _windows(entry):
    payload = _get(entry, "payload")
    payload = payload if isinstance(payload, Mapping) else {}
    raw = payload.get("rl_windows") or payload.get("windows")
    if raw:
        pairs = [(str(row[0]).strip(), row[1]) for row in raw if isinstance(row, (list, tuple)) and len(row) >= 2]
    else:
        pairs = [("5h", payload.get("rl_5h")), ("week", payload.get("rl_7d"))]
    return [(_WINDOW_ALIAS.get(label, label), value) for label, value in pairs]


def usage_gauges(snapshot):
    """[(abbr, label|None, percent|None)] for each harness with a usage snapshot or a live session."""
    snapshots = _get(snapshot, "usage_snapshots", {}) or {}
    live = {str(_get(s, "harness", "")).lower() for s in (_get(snapshot, "sessions", ()) or ())}
    gauges = []
    for harness in HARNESS_ORDER:
        if harness not in snapshots and harness not in live:
            continue
        entry = snapshots.get(harness)
        window = next(((label, value) for label, value in _windows(entry)
                       if label in _WINDOW_LABEL and isinstance(value, (int, float)) and not isinstance(value, bool)),
                      None) if entry is not None else None
        gauges.append((HARNESS_ABBR[harness], window[0] if window else None, window[1] if window else None))
    return gauges


def usage_text(snapshot, spaced=False):
    """'CL 5h29% · CX 주8% · OC 5h0%' (spaced: 'CL 5h 29%'); '·' when nothing is known."""
    gauges = usage_gauges(snapshot)
    if not gauges:
        return "·"
    parts = []
    for abbr, label, value in gauges:
        if label is None:
            parts.append(f"{abbr} ?")
        else:
            gap = " " if spaced else ""
            parts.append(f"{abbr} {_WINDOW_LABEL[label]}{gap}{int(round(value))}%")
    return " · ".join(parts)


def running_counts(snapshot):
    counts = {harness: 0 for harness in HARNESS_ORDER}
    for job in _jobs(snapshot):
        harness = str(_get(job, "harness", "")).lower()
        if _get(job, "liveness") == "working" and harness in counts:
            counts[harness] += 1
    return counts


def governor_text(snapshot):
    governor = _get(snapshot, "governor")
    active, cap = _get(governor, "active"), _get(governor, "cap")
    return f"{active}/{cap}" if active is not None and cap is not None else "?/?"


def allocation_text(snapshot):
    counts = running_counts(snapshot)
    cells = " ".join(f"{HARNESS_ABBR[h]}{counts[h]}" for h in HARNESS_ORDER)
    return f"배분 {cells} · {governor_text(snapshot)}"


def warnings(snapshot):
    """Only abnormal states: an unreachable host, a GPU process with no owner at all."""
    items = []
    views = host_views(snapshot) or []
    down = sum(1 for view in views if not view["reachable"])
    if down:
        items.append(f"⚠ host 응답 없음 {down}")
    unowned = sum(1 for entry in (_get(snapshot, "unregistered_gpu", ()) or ())
                  if not _get(entry, "owner_kind") and not _get(entry, "owner_label")
                  and not _get(entry, "session_owner"))
    if unowned:
        items.append(f"⚠ 주인 없는 GPU {unowned}")
    return items


def status_lines(snapshot, focus_result=None, now=None, width=None):
    """The two status rows above the body."""
    first = f"{PRODUCT} │ {job_summary(snapshot)}"
    extra = warnings(snapshot)
    if extra:
        first += " · " + " · ".join(extra)
    ages = source_ages(snapshot)
    failures = failed_sources(snapshot, focus_result, now)
    if failures:
        ages += " · " + " · ".join(failures)
    second = f"{ages} │ {usage_text(snapshot)} │ {gpu_text(snapshot)}"
    if width is not None:
        return clip(first, width), clip(second, width)
    return first, second


# -- system page -----------------------------------------------------------------------------

def _gpu_line(gpu):
    glyph = LIVENESS_GLYPH["working"] if gpu["busy"] else LIVENESS_GLYPH["idle"]
    word = LIVENESS_WORD["working"] if gpu["busy"] else LIVENESS_WORD["idle"]
    return f"  GPU{gpu['index']} {glyph} {word} · VRAM {gpu['used']}/{gpu['total']}MiB"


def _session_tag(snapshot, session_id):
    for session in _get(snapshot, "sessions", ()) or ():
        ids = [_get(session, "session_id")] + list(_get(session, "session_aliases", ()) or ())
        if session_id and session_id in ids:
            return _get(session, "session_tag")
    return None


def _tail(snapshot):
    lines = []
    for entry in _get(snapshot, "unregistered_gpu", ()) or ():
        if _get(entry, "owner_kind") == "run":
            continue                      # a registered run: the GPU line already names its session
        gpus = ",".join(str(i) for i in _get(entry, "gpu_indexes", ()) or ())
        owner = _get(entry, "owner_label") or "주인 없음"
        lines.append(f"{_get(entry, 'host', '?')} GPU{gpus} PID {_get(entry, 'pid', '?')} → {owner}")
        session = _get(entry, "session_owner")
        if session:
            tag = _session_tag(snapshot, _get(session, "id"))
            lines.append(f"  세션 [{tag}] · 주인 확인됨" if tag
                         else f"  세션 {_get(session, 'label', '?')} · 주인 확인 불가")
        elif _get(entry, "owner_kind") or _get(entry, "owner_label"):
            lines.append("  세션 귀속 없음")
        else:
            lines.append("  주인 확인 불가")
    return lines


def system_lines(snapshot):
    """시스템 · 사용량 / 배분 / host > GPU — usage, running counts, then each host's GPUs."""
    counts = running_counts(snapshot)
    running = " · ".join(f"{HARNESS_ABBR[h]} {counts[h]}" for h in HARNESS_ORDER)
    lines = ["시스템 · 사용량 / 배분 / host > GPU", usage_text(snapshot, spaced=True),
             f"실행 {running} / 동시 {governor_text(snapshot)}"]
    views = host_views(snapshot)
    if views is None:
        lines.append("host > GPU ? · 읽지 못함")
        return tuple(lines)
    for view in views:
        if not view["reachable"]:
            lines.append(f"{view['host']} 응답 없음")
            continue
        cpu = "?" if view["cpu"] is None else f"{view['cpu']}%"
        lines.append(f"{view['host']} CPU {cpu} · MEM {view['mem_used']}/{view['mem_total']}MiB")
        for gpu in view["gpus"]:
            lines.append(_gpu_line(gpu))
            lines.append("    " + (", ".join(gpu["owners"]) if gpu["owners"] else "세션 귀속 없음"))
    lines.extend(_tail(snapshot))
    return tuple(lines)


def host_nav_labels(snapshot):
    """[(host, '> GPU 2/2' | '> 응답 없음')] for the navigation's system section."""
    views = host_views(snapshot) or []
    rows = []
    for view in views:
        if not view["reachable"]:
            rows.append((view["host"], "> 응답 없음"))
        else:
            rows.append((view["host"], f"> GPU {sum(1 for g in view['gpus'] if g['busy'])}/{len(view['gpus'])}"))
    return rows
