"""Fleet render: GPU process and run rows.

Split out of `render.py`, which re-exports every name defined here.
"""
import os
import re
import shlex
import threading
import time
from collections import Counter
from .display import label as _user_label
from .config import ui_text
from .session_handle import sanitize_title as _sanitize_session_title
from .session_handle import resolve_tag as _resolve_session_tag

from .render_cells import (
    _BAR_EMPTY, _BAR_FULL, _clip_w, _dw, _flat_level, _gauge_segs, _glyph, _half_up, _pct_key,
)

from .render_rows import (
    _clip_segs,
)

from .render_select import (
    _gpu_safe_text, _gpu_session_keys, _session_repo_label,
)

from .render_status import (
    _gpu_clip_command, _gpu_command_room, _gpu_command_text, _gpu_display_model, _gpu_gib,
    _gpu_model_key, _gpu_path_collisions, _gpu_process_rows, _gpu_processes_by_memory,
)


def _gpu_process_label(command):
    """Shorten only unambiguous command names; leave other commands literal."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    def value_of(option):
        for index, word in enumerate(words):
            if word.startswith(option + "="):
                return word.split("=", 1)[1]
            if word == option and index + 1 < len(words):
                return words[index + 1]
        return None

    mode = value_of("--engine_mode")
    for option in ("--run-id", "--name", "--config"):
        value = value_of(option)
        if value:
            title = os.path.basename(value)
            if option == "--config":
                stem = os.path.splitext(title)[0]
                fine_tune = re.match(r"^_?ft0*([1-9][0-9]*)__m([0-9]+)(?:_|$)", stem,
                                     re.IGNORECASE)
                model = re.match(r"^_?m([0-9]+(?:_[0-9]+)?)(?:_|$)", stem,
                                 re.IGNORECASE)
                if fine_tune:
                    title = "M%s_%s" % (fine_tune.group(2), fine_tune.group(1))
                elif model:
                    title = "M" + model.group(1)
                else:
                    title = stem
            activity = "train" if mode in {"train", "train_ft"} else None
            return title + (" " + activity if activity else "")
    return command


def _gpu_state(gpu):
    """GPU liveness from exact current process evidence, never utilization heuristics."""
    if gpu.get("observation_source"):
        return ("working" if any(p.get("gpu_placement") in {"visible-device-access", "mapped-device-access"}
                                 for p in gpu.get("processes") or ()) else "unknown")
    return ("working" if any(isinstance(process, dict)
                             for process in (gpu.get("processes") or ()))
            else "idle")


def _resource_level_key(value, active):
    """Usage hue at active or idle intensity; unknown telemetry stays dim."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "dim"
    level = _flat_level(_pct_key(max(0, min(100, value))))
    if active:
        return level
    return {"lvl_g": "lvl_g_dim", "lvl_y": "lvl_y_dim",
            "lvl_r_flat": "lvl_r_dim"}.get(level, "dim")


_RESOURCE_GAUGE_W = 8
_CPU_GAUGE_MIN = 4
_CPU_GAUGE_MAX = 20  # shared legacy bounds for the capacity-scaled VRAM track
_GPU_MODEL_COLUMN_W = 18
_GPU_CAPACITY_COLUMN_W = 9


def _resource_gauge_segs(value, track=_RESOURCE_GAUGE_W):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return [("·" * track, "dim"), ("   —", "dim")]
    value = max(0, min(100, _half_up(value)))
    return (_gauge_segs(value, track, track=track)
            + [(" %3d%%" % value, _flat_level(_pct_key(value)))])


def _resource_pct_segs(value, width=4, active=True):
    """Fixed-width percent column; unknown remains explicit."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return [("—".rjust(width), "dim")]
    value = max(0, min(100, _half_up(value)))
    return [(('%d%%' % value).rjust(width), _resource_level_key(value, active))]


def _cpu_thread_values(values, cpu_count):
    """Normalize a complete logical-index thread sample or reject it as legacy."""
    if not isinstance(cpu_count, int) or isinstance(cpu_count, bool) or cpu_count <= 0:
        return None
    if not isinstance(values, (list, tuple)) or len(values) != cpu_count:
        return None
    normalized = []
    for value in values:
        if value is None:
            normalized.append(None)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            normalized.append(max(0.0, min(100.0, float(value))))
        else:
            return None
    return normalized


def _cpu_active_threads(utilization, cpu_count, thread_utilizations=None):
    """Count observed active logical threads, with the aggregate legacy fallback."""
    values = _cpu_thread_values(thread_utilizations, cpu_count)
    if values is not None:
        return sum(value is not None and value > 0 for value in values)
    if not isinstance(cpu_count, int) or isinstance(cpu_count, bool) or cpu_count <= 0:
        return None
    if not isinstance(utilization, (int, float)) or isinstance(utilization, bool):
        return None
    pct = max(0.0, min(100.0, float(utilization)))
    return max(0, min(cpu_count, _half_up(pct * cpu_count / 100.0)))


def _cpu_thread_text(active, cpu_count):
    if not isinstance(cpu_count, int) or isinstance(cpu_count, bool) or cpu_count <= 0:
        return "— thr"
    if not isinstance(active, int) or isinstance(active, bool):
        return "—/%d thr" % cpu_count
    return "%d/%d thr" % (active, cpu_count)


def _cpu_gauge_track(cpu_count, available=None):
    """One cell per four logical threads; compress or omit before numbers."""
    known = isinstance(cpu_count, int) and not isinstance(cpu_count, bool) and cpu_count > 0
    desired = (max(_CPU_GAUGE_MIN, min(_CPU_GAUGE_MAX,
                                       _half_up(cpu_count / 4.0)))
               if known else _RESOURCE_GAUGE_W)
    if available is None:
        return desired
    available = max(0, int(available))
    if available >= desired:
        return desired
    minimum = min(_CPU_GAUGE_MIN, desired)
    return available if available >= minimum else 0


def _vram_gauge_track(total_mib):
    """Four GiB per cell, bounded like the CPU capacity track."""
    if (not isinstance(total_mib, (int, float)) or isinstance(total_mib, bool)
            or total_mib <= 0):
        return _RESOURCE_GAUGE_W
    return max(_CPU_GAUGE_MIN, min(_CPU_GAUGE_MAX,
                                   _half_up(total_mib / 4096.0)))


def _cpu_gauge_segs(active, cpu_count, track, key):
    """Render the active-thread ratio as one contiguous aggregate gauge."""
    if track <= 0:
        return []
    if (not isinstance(active, int) or isinstance(active, bool)
            or not isinstance(cpu_count, int) or isinstance(cpu_count, bool)
            or cpu_count <= 0 or active <= 0):
        filled = 0
    elif active >= cpu_count:
        filled = track
    elif track <= 1:
        filled = 0
    else:
        filled = max(1, min(track - 1, _half_up(active * track / float(cpu_count))))
    return [(_BAR_FULL * filled, key), (_BAR_EMPTY * (track - filled), "dim")]


def _host_memory_pair(used, total, unit):
    return "%s/%s%s" % (_gpu_gib(used), _gpu_gib(total), unit)


def _subdued_ratio_key(used, total):
    if (not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0
            or not isinstance(used, (int, float)) or isinstance(used, bool)):
        return "dim"
    return _resource_level_key(100.0 * used / total, False)


def _gpu_token(gpu, available, show_name=False, sessions=None, index_width=1,
               vram_slot=None):
    if gpu.get("observation_source"):
        state = _gpu_state(gpu)
        glyph, state_key = _glyph(state)
        identity = "%s: " % gpu.get("index", "?")
        name = _gpu_display_model(gpu.get("name")) if show_name and gpu.get("name") else ""
        segs = [(glyph + " ", state_key), (identity, "name_dim")]
        if name:
            segs += [(name + "  ", "name_dim")]
        segs += [("UTIL/VRAM unknown", "dim")]
        if any(p.get("gpu_placement") == "device-access-only" for p in gpu.get("processes") or ()):
            segs += [(" · check device access", "lvl_y")]
        return _clip_segs(segs, available)[0]
    index = gpu.get("index")
    util = gpu.get("utilization_gpu_pct")
    total, used = gpu.get("memory_total_mib"), gpu.get("memory_used_mib")
    vram_pct = (100.0 * used / total
                if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0
                and isinstance(used, (int, float)) and not isinstance(used, bool) else None)
    memory = "%s/%sGB" % (_gpu_gib(gpu.get("memory_used_mib")),
                           _gpu_gib(gpu.get("memory_total_mib")))
    state = _gpu_state(gpu)
    active = state == "working"
    summary_key = "resource_active" if active else "dim"
    vram_key = _resource_level_key(vram_pct, active)
    glyph, state_key = _glyph(state)
    show_state_word = available >= 58
    narrow_track = available < 70
    vram_track = 4 if narrow_track else _vram_gauge_track(total)
    if narrow_track:
        vram_slot = 4
    elif not isinstance(vram_slot, int) or isinstance(vram_slot, bool):
        vram_slot = vram_track
    else:
        vram_slot = max(vram_track, vram_slot)
    index_text = str(index) if isinstance(index, int) and not isinstance(index, bool) else "?"
    segs = [(glyph, state_key), (" ", None),
            ("%s: " % index_text.rjust(max(1, index_width)), summary_key)]
    if show_state_word:
        word = _user_label(state)
        segs.append((word + " " * max(0, 7 - _dw(word)) + "  ", state_key))
    fixed_tail_width = 5 + 4 + 7 + vram_slot + 2 + _GPU_CAPACITY_COLUMN_W
    show_model_column = (show_name and
                         sum(_dw(text) for text, _key in segs)
                         + _GPU_MODEL_COLUMN_W + 2 + fixed_tail_width <= available)
    if show_model_column:
        raw_name = gpu.get("name")
        name = _gpu_display_model(raw_name) if raw_name else "—"
        model = _clip_w(name, _GPU_MODEL_COLUMN_W)
        segs += [((model + " " * (_GPU_MODEL_COLUMN_W - _dw(model))),
                  _gpu_model_key(raw_name, active=active) if raw_name else summary_key),
                 ("  ", None)]
    segs.append(("UTIL ", summary_key))
    segs += _resource_pct_segs(util, active=active)
    segs += [("  VRAM ", summary_key)]
    gauge = _gauge_segs(vram_pct, vram_track, track=vram_track)
    gauge[0] = (gauge[0][0], vram_key)
    segs += gauge
    if vram_track < vram_slot:
        segs += [(" " * (vram_slot - vram_track), None)]
    capacity = _clip_w(memory, _GPU_CAPACITY_COLUMN_W, ellipsis="")
    segs += [("  " + capacity.rjust(_GPU_CAPACITY_COLUMN_W), vram_key)]
    return segs


def _gpu_status_text(status):
    """Fleet vocabulary from the common measurement, without changing its payload."""
    text = "GPU unavailable"
    if status.get("code") == "nvml-driver-mismatch":
        text += " · driver mismatch (lib %s / module %s)" % (
            status.get("library_version") or "unknown", status.get("module_version") or "unknown")
    if status.get("reboot_required"):
        text += " · reboot needed"
    count = status.get("running_count")
    if type(count) is int:
        text += " · runs:%d" % count
    return text


def _gpu_session_key(owner):
    if (isinstance(owner, dict) and owner.get("kind") == "session"
            and owner.get("harness") in {"claude", "codex", "opencode"}
            and isinstance(owner.get("id"), str) and owner["id"]):
        return owner["harness"], owner["id"]
    return None


def _gpu_process_session_key(process):
    return (_gpu_session_key(process.get("session_owner"))
            or _gpu_session_key(process.get("owner")))


def _gpu_session_label(owner, sessions=None, display_snapshot=None):
    """Name only an exact session; workload labels carry no session identity."""
    key = _gpu_session_key(owner)
    if key is None:
        unattributed = ui_text("fleet.gpu.unattributed")
        return unattributed, unattributed
    session = next((s for s in sessions or () if key in _gpu_session_keys(s)), None)
    name = ""
    if session is not None:
        for candidate in (getattr(session, "runtime_name", None),
                          getattr(session, "_herdr_name", None)):
            name = _sanitize_session_title(candidate)
            if name:
                break
    tag = getattr(session, "session_tag", None) if session is not None else None
    tag = tag or _resolve_session_tag(*key)
    short = "[%s]" % tag if tag else key[1][:8]
    if name:
        return name + " " + short, short
    repo = _session_repo_label(session, display_snapshot)
    return (repo + " " + short if repo else short), short


def _gpu_lease_segs(lease, sessions=None, processes=None, compact=False,
                    display_snapshot=None):
    """Session and start of one reservation, without task/run descriptions."""
    if processes is None:
        label, short = _gpu_session_label(lease.get("owner"), sessions, display_snapshot)
        if compact:
            label = short
    else:
        keys = dict.fromkeys(_gpu_process_session_key(process) for process in processes)
        owners = ({"kind": "session", "harness": key[0], "id": key[1]} if key else None
                  for key in keys)
        label = " / ".join(_gpu_session_label(owner, sessions, display_snapshot)[1 if compact else 0]
                           for owner in owners)
    started = lease.get("started_at")
    when = time.strftime("%m-%d %H:%M", time.localtime(started)) if isinstance(started, (int, float)) else "?"
    return [(_gpu_safe_text(label), "tag"), (" · " + when, "dim")]


def _gpu_reservation_rows(leases, indent, width, unplaced=False, sessions=None,
                          display_snapshot=None):
    prefix = "GPU reserved · device unknown · " if unplaced else "  reserved "
    return [_clip_segs([(indent + prefix, "lvl_y")] + _gpu_lease_segs(
        lease, sessions, display_snapshot=display_snapshot), width)[0]
            for lease in leases]


def _gpu_run_pairs(leases, processes):
    """Join each reservation to the processes of its own run.

    The resource runner leads the process group its reservation records, so
    lease pid == process pgid is exact. A run that left that group joins only
    as its session's sole unjoined lease and process on this GPU.
    """
    pairs, joined = [], set()
    for lease in leases:
        pid = lease.get("pid")
        exact = isinstance(pid, int) and not isinstance(pid, bool)
        matched = [process for process in processes
                   if exact and process.get("pgid") == pid and id(process) not in joined]
        joined.update(map(id, matched))
        pairs.append((lease, matched))
    rest = [process for process in processes if id(process) not in joined]
    for lease, matched in pairs:
        key = _gpu_session_key(lease.get("owner"))
        if matched or key is None:
            continue
        leases_left = [other for other, found in pairs
                       if not found and _gpu_session_key(other.get("owner")) == key]
        processes_left = [process for process in rest if _gpu_process_session_key(process) == key]
        if len(leases_left) == 1 and len(processes_left) == 1:
            matched.append(processes_left[0])
            rest.remove(processes_left[0])
    return pairs, rest


_GPU_RUN_COMMAND_MIN_W = 12   # minimum room for a useful observed command


def _gpu_run_rows(gpu, indent, width, sessions=None, display_snapshot=None):
    """Expanded GPU: one row per run, its reservation and command together.

    Unjoined reservations and processes keep their own rows; whether they
    disagree is judged once, by the folded summary.
    """
    leases = [lease for lease in (gpu.get("reservations") or ()) if isinstance(lease, dict)]
    processes = _gpu_processes_by_memory(gpu)
    ambiguous = _gpu_path_collisions(
        _gpu_safe_text(process.get("command")) for process in processes)
    pairs, rest = _gpu_run_pairs(leases, processes)
    rows = []
    for lease, matched in pairs:
        if not matched:
            rows.extend(_gpu_reservation_rows([lease], indent, width, sessions=sessions,
                                              display_snapshot=display_snapshot))
            continue
        row = [(indent + "  ", None), ("↳ ", "dim")] + _gpu_lease_segs(
            lease, sessions, matched, display_snapshot=display_snapshot)
        more = [(" +%d" % (len(matched) - 1), "dim")] if len(matched) > 1 else []
        room = _gpu_command_room(row + more, width) - _dw(" · ")
        if room < _GPU_RUN_COMMAND_MIN_W:
            # An optional long alias cannot displace the observed command.
            row = ([(indent + "  ", None), ("↳ ", "dim")]
                   + _gpu_lease_segs(lease, sessions, matched, compact=True,
                                     display_snapshot=display_snapshot))
            room = _gpu_command_room(row + more, width) - _dw(" · ")
        if room >= _GPU_RUN_COMMAND_MIN_W:
            row += [(" · ", "dim"),
                    (_gpu_clip_command(_gpu_command_text(matched[0], ambiguous), room), "dim")]
        rows.append(_clip_segs(row + more, width)[0])
    rows.extend(_gpu_process_rows({"processes": rest}, indent, width, ambiguous))
    return rows


def _gpu_activity_summary(gpu, host, sessions=None, display_snapshot=None):
    """Count observed processes by full session identity, not saved badge text."""
    observed = Counter(_gpu_process_session_key(process)
                       for process in gpu.get("processes") or () if isinstance(process, dict))
    reserved = Counter(_gpu_session_key(lease.get("owner"))
                       for lease in gpu.get("reservations") or () if isinstance(lease, dict))
    groups = []
    for key, count in observed.items():
        owner = {"kind": "session", "harness": key[0], "id": key[1]} if key else None
        label, short = _gpu_session_label(owner, sessions, display_snapshot)
        groups.append((label, count, short))
    groups.sort(key=lambda group: (-group[1], group[0]))
    warnings = []
    # A failed process query or unmapped fallback cannot prove an empty GPU.
    if (isinstance(gpu.get("processes"), (list, tuple))
            and isinstance(gpu.get("reservations"), (list, tuple))
            and not host.get("process_detail") and not gpu.get("observation_source")):
        extra_reserved = sum((reserved - observed).values())
        extra_observed = sum((observed - reserved).values())
        if extra_reserved:
            warnings.append("reserved+%d" % extra_reserved)
        if extra_observed and not host.get("reservation_detail"):
            warnings.append("unreserved+%d" % extra_observed)
    return groups, warnings


def _gpu_activity_tail(groups, warnings, available):
    """Fit whole session entries; +N counts omitted sessions, not processes."""
    entries = [group[0] for group in groups]
    compact = [group[2] for group in groups]
    owner_width = _dw(" · " + " ".join(compact)) if compact else 0
    warning_room = max(0, available - owner_width)
    warning = " ".join(warnings)
    if _dw(" · " + warning) > warning_room:
        warning = warning.replace("unreserved+", "unres+").replace("reserved+", "res+")
    if _dw(" · " + warning) > warning_room:
        warning = warning.replace("unres+", "U+").replace("res+", "R+")
    if _dw(" · " + warning) > available:
        warning = "/".join("U" if item.startswith("unreserved+") else "R"
                           for item in warnings)
    warning_width = _dw(" · " + warning) if warning else 0
    room = available - warning_width - 3
    summary = ""
    for kept in range(len(entries), -1, -1):
        for labels in (entries, compact):
            parts = labels[:kept]
            if kept < len(entries):
                parts = parts + ["+%d" % (len(entries) - kept)]
            candidate = " ".join(parts)
            if _dw(candidate) <= room:
                summary = candidate
                break
        if summary:
            break
    tail = [(" · " + summary, "dim")] if summary else []
    if warning and warning_width <= available:
        tail.append((" · " + warning, "lvl_y"))
    return tail


def _gpu_on_session_lines(host, gpu, session_runs):
    """Every run on this GPU is already named on its session's GPU line (no reservation)."""
    processes = [process for process in gpu.get("processes") or () if isinstance(process, dict)]
    name = _gpu_safe_text(host.get("host") or "?") or "?"
    return (bool(processes) and not gpu.get("reservations")
            and all((name, process.get("pid"), str(process.get("proc_start"))) in session_runs
                    for process in processes))
_EXPLORER_QUESTION_QUEUE_LOCK = threading.Lock()


class _ExplorerView:
    """The published snapshot plus the few values render itself owns (GPU hosts, stable session order,
    the cached project resolver). Read-only: attribute lookups fall through to the snapshot."""

    def __init__(self, base, **extras):
        self.__dict__["_base"] = base
        self.__dict__["_extras"] = extras

    def __copy__(self):
        value = object.__new__(type(self))
        value.__dict__ = dict(self.__dict__, _extras=dict(self._extras))
        return value

    def __getattr__(self, name):
        extras = self.__dict__["_extras"]
        if name in extras:
            return extras[name]
        return getattr(self.__dict__["_base"], name)
