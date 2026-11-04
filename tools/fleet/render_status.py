"""Fleet render: execution detail, context chips, resource locations, and version segments.

Split out of `render.py`, which re-exports every name defined here.
"""
import os
import re
import shlex
import time
from .model import fmt_min, exec_child_is_wait, current_background_shell_jobs

from .render_cells import (
    _clip_w, _cw, _dw, _GAUGE_W,
)

from .render_stages import (
    _text_age,
)

from .render_rows import (
    _clip_segs, _fmt_exec_age, _project_for_display, _SESSION_DETAIL_COL,
)

from .render_select import (
    _gpu_safe_text, _resource_gpu_resources, _resource_verified_process_location,
)


def _orphan_resource_groups(resources, drawn_jobs, display_snapshot=None):
    """Working declared children not on screen; cwd only places their project card."""
    shown = {id(child) for job in drawn_jobs
             for child in getattr(job, "resource_children", ())}
    groups = {}
    for child in resources or ():
        if child.liveness != "working" or not child.parent_attempt_id or id(child) in shown:
            continue
        name = (_project_for_display(child.cwd, display_snapshot)
                if child.cwd else child.project)
        groups.setdefault(name, []).append(child)
    return groups


# F-100b (user 2026-09-03) — the context row's lead cell is the WHERE word, not the state
# word. F-55's `working`/`idle` word duplicated the harness row's glyph (same state, same
# color, one line apart), so it is retired; the L1 glyph is the one status indicator. The
# slot keeps F-55's exact 7+1 cells (the longest state word was 7) so the gauge column and
# every NOW anchor are unchanged.
#   · True  → `herdr` in soft white (attached to a herdr pane right now)
#   · False → dim `tty` (herdr answered; this session is a plain terminal)
#   · None  → blank cells (no evidence either way — never a guess)
# `stale`/`dead` still never reach here — F-13 omits the whole row (both callers return []).
_CTX_ON_TEXT = "herdr"
_CTX_OFF_TEXT = "tty"
_CTX_TMUX_TEXT = "tmux"
_CTX_CORNER_MARK = "↖"             # aedd50: a `⚑` pane that is not its tab's top-left pane
_CTX_CHIP_W = 7                     # F-55's slot: plain literal, evaluated before `_dw` exists.
_CTX_LABEL_W = _CTX_CHIP_W + 1      # + one trailing space = the F-55 slot width, unchanged.
_CONTEXT_VALUE_W = 4
_CONTEXT_NOW_GAP = 3          # minimum gap; F-42c widens it until NOW reaches the session column.
_CONTEXT_INDENT_W = _SESSION_DETAIL_COL


def _compact_context_gauge_width(available, depth=0):
    """Compatibility shim: the legacy available-width knob no longer sizes any gauge.
    F-52b sizes the context track from the session's measured window (`_context_gauge_track`)."""
    return _GAUGE_W


_EXEC_GLYPH = "⚙"
# F-47 v47, corrected 2026-09-16 — a tool call with nothing running under it renders as ⏳. v47 forced it
# ALWAYS dim because `shell` + a `sleep` child used to promote the ROW to working, so a
# bright badge sat on top of a green glyph the wait itself had caused (user 2026-08-05
# "fleet에서 sleep 명령어가 뜨는 건 좀 모순"). That promotion is gone — `shell` is idle by
# measurement (model._session_status_state) — so brightness can follow the row's own
# classification like every other badge: dim under an idle/background row, live under a busy
# turn, which is the only way "이 세션은 9분째 스크립트를 기다리는 중" is visible at a glance.
_WAIT_GLYPH = "⏳"
# The leaf of a waiting call is a primitive (`sleep`, `flock`) whose name tells the user
# nothing and whose clock resets every iteration, so a 9-minute wait rendered as `sleep 18s`.
# The badge names the WAIT and lets the elapsed — the CALL's, not the leaf's — carry the
# information; the leaf comm stays in `--json` for forensics.
_WAIT_LABEL = "waiting"


def _exec_detail_segs(entity):
    """F-47 `⚙ <command> <elapsed>` detail segments for a session row, or [].

    Two evidence shapes feed one badge: an owned process descendant (`exec_child`, carries
    an elapsed time) and a Codex rollout tool_call still awaiting its output (`exec_tool`,
    carries no clock). Process evidence wins when both exist — it is the stronger signal
    and the only one with a real elapsed. Brightness follows the CLASSIFICATION, not the
    badge: a promoted `working` row gets the live hue, a background child under an idle row
    stays dim (prd.md:263 — the badge never makes a row or its group look hot).

    The elapsed is the CALL's, so it counts a whole `sleep 20` poll loop instead of resetting
    with each iteration, and a waiting call is labelled `대기` rather than by its primitive.
    """
    key = "g_work" if getattr(entity, "liveness", None) == "working" else "dim"
    child = getattr(entity, "exec_child", None)
    if isinstance(child, dict) and child.get("comm"):
        etime = child.get("etime_s")
        if exec_child_is_wait(child):
            # 2026-09-16: name the wait, not the primitive, and leave `key` at the row's own
            # classification — dim on an idle/background row, live under a busy turn.
            glyph, label = _WAIT_GLYPH, _WAIT_LABEL
        else:
            # A real non-helper descendant is experiment activity even if its interactive
            # parent is sleeping. Promote only this status detail, never the title.
            glyph, label, key = _EXEC_GLYPH, child["comm"], "g_work"
        text = "%s %s" % (glyph, label)
        if isinstance(etime, (int, float)) and not isinstance(etime, bool):
            text += " %s" % _fmt_exec_age(etime)
        return [(text, key)]
    tool = getattr(entity, "exec_tool", None)
    if isinstance(tool, dict):
        # Tool name first: a rollout `cmd` string usually starts with shell prologue
        # (`set -eu`, an assignment), so its first token would name the wrong thing.
        label = tool.get("name") or tool.get("command")
        if label:
            return [("%s %s" % (_EXEC_GLYPH, label), key)]
    return []


def _context_lead_chip(entity, dim=False, degrade=False, room=None):
    """F-100b lead cell → segments of exactly `_CTX_LABEL_W` cells (or none).

    `degrade=True` (the F-55b threshold: chip + track + value cannot share the row) drops
    the slot entirely — the L1 glyph already carries the state and the track is a
    measurement that must not shrink (F-52b), so the chip is what yields, whole.
    A dispatch/muted row passes `dim=True`: workers are never herdr panes, and the
    caller flattens every key to dim anyway.

    aedd50: a MAIN row passes its wider lead `room`; there the word becomes the pane's
    location (`wB:tZ·pA6`, tmux `main:1.2`, head clipped to fit) and a `⚑` pane that is
    not its tab's top-left pane leads with `↖`. Other rows keep the word.
    """
    if degrade:
        return []
    attached = getattr(entity, "herdr_attached", None)
    plex = getattr(entity, "multiplexer", None)
    if attached is True or plex == "tmux":
        word = _CTX_TMUX_TEXT if plex == "tmux" else _CTX_ON_TEXT
        key = "dim" if dim else "herdr_on"
        where = getattr(entity, "pane_location", None)
        corner = getattr(entity, "pane_corner", None) is False
        width = (room or 0) - 1 - (_dw(_CTX_CORNER_MARK) if corner else 0)
        if isinstance(where, str) and where and width >= _CTX_CHIP_W:
            text = where if _dw(where) <= width else "…" + _clip_tail_w(where, width - _dw("…"))
            lead = [(_CTX_CORNER_MARK, "dim" if dim else "lvl_y")] if corner else []
            return lead + [(text + " ", key)]
        return [(word.ljust(_CTX_CHIP_W) + " ", key)]
    if attached is False:
        return [(_CTX_OFF_TEXT.ljust(_CTX_CHIP_W) + " ", "dim")]
    return [(" " * _CTX_LABEL_W, None)]


def _clip_tail_w(text, width):
    """The last cells of ``text`` that fit ``width`` display cells."""
    out = ""
    for char in reversed(str(text)):
        if _dw(char + out) > width:
            break
        out = char + out
    return out


def _resource_command_label(command):
    """A bounded argv label, without interpreting workload logs or lifecycle."""
    words = command if isinstance(command, list) else _gpu_command_words(str(command or ""))
    for index, word in enumerate(words):
        if word == "-m" and index + 1 < len(words):
            return _gpu_safe_text(words[index + 1].rsplit(".", 1)[-1])
        if word.endswith(".py"):
            return _gpu_safe_text(os.path.basename(word))
    return _gpu_safe_text(os.path.basename(words[0])) if words else ""


def _resource_location(child, snapshot):
    """One location observation for owner NOW and resource rows on all harnesses."""
    resources = _resource_gpu_resources(child, snapshot, commands=True)
    if resources:
        where = ",".join("%s:%s" % (r["host"], r["index"]) for r in resources)
    else:
        local = next((host for host in (snapshot or {}).get("hosts", ())
                      if isinstance(host, dict) and host.get("self") is True
                      and host.get("reachable") is True), None)
        argv = getattr(child, "command", None) or []
        assignments = []
        if argv and argv[0] == "env":
            for arg in argv[1:]:
                if "=" not in arg:
                    break
                assignments.append(arg)
        identity = getattr(child, "state_evidence", None) or {}
        cpu_only = ("CUDA_VISIBLE_DEVICES=" in assignments
                    and identity.get("reason") == "exact-identity-match"
                    and not getattr(child, "remote_training", None))
        where = ("%s:CPU" % _gpu_safe_text(local["host"])
                 if local and cpu_only else "host/GPU unknown")
        placement = getattr(child, "local_placement", None) or {}
        requested = placement.get("requested_devices") or []
        if requested:
            host = _gpu_safe_text(local["host"] if local else placement.get("hostname") or "?")
            devices = []
            for device in requested:
                matches = [g for g in (local or {}).get("gpus", ())
                           if isinstance(g.get("uuid"), str) and g["uuid"].startswith(device)]
                gpu = matches[0] if len(matches) == 1 else None
                devices.append(str(gpu["index"]) if gpu else _gpu_safe_text(device))
            checked = (local and local.get("gpus") and not local.get("detail")
                       and not local.get("process_detail") and not local.get("gpu_error")
                       and not local.get("process_error") and placement.get("complete", True))
            exact = {(p["pid"], str(p["starttime"]))
                     for p in placement.get("processes", ())}
            pids = {pid for pid, _ in exact}
            uncertain = any(p.get("pid") in pids
                            and (p["pid"], str(p.get("proc_start"))) not in exact
                            for g in (local or {}).get("gpus", ())
                            for p in g.get("processes", ()))
            checked = checked and not uncertain
            state = "GPU not in use" if checked else "GPU use unknown"
            where = "%s:%s selected · %s" % (host, ",".join(devices), state)
            if placement.get("io_wait"):
                where += " · I/O wait"
    if where == "host/GPU unknown":
        where = _resource_verified_process_location(child) or where
    return where, resources


def _resource_log_age(child):
    if getattr(child, "log_size", None) == 0:
        return "no output"
    log_ts = getattr(child, "log_updated_at", None)
    return ("log · " + _text_age(log_ts)
            if isinstance(log_ts, (int, float)) and not isinstance(log_ts, bool)
            else "output unknown" if getattr(child, "log_path", None) else "no log")


def _background_shell_tail(entity, *, now, width):
    jobs = current_background_shell_jobs(
        getattr(entity, "shell_jobs", None) or (), now=now)
    if not jobs or width <= 0:
        return None
    count = len(jobs)
    count_only = "⚙ %d" % count
    if _dw(count_only) > width:
        return None
    latest = jobs[0]
    elapsed_s = latest.get("elapsed_s")
    elapsed = ("%ds" % elapsed_s if isinstance(elapsed_s, int)
               and not isinstance(elapsed_s, bool) and 0 <= elapsed_s < 60 else
               fmt_min(elapsed_s // 60) if isinstance(elapsed_s, int)
               and not isinstance(elapsed_s, bool) and elapsed_s >= 60 else "?")
    count_suffix = " (+%d)" % (count - 1) if count > 1 else ""
    command = latest.get("command_display") or latest.get("command")
    if isinstance(command, str) and command:
        fixed = "⚙  " + elapsed + count_suffix
        command_width = width - _dw(fixed)
        if command_width > 0:
            clipped = _clip_w(command, command_width)
            candidate = "⚙ " + clipped + " " + elapsed + count_suffix
            if clipped and _dw(candidate) <= width:
                return candidate
    return count_only


def _split_w_exact(text, max_width):
    """Split without ellipsis or character loss at display-cell boundaries."""
    if not text:
        return [""]
    max_width = max(1, int(max_width))
    chunks, current, used = [], [], 0
    for char in text:
        width = _cw(char)
        if current and used + width > max_width:
            chunks.append("".join(current))
            current, used = [], 0
        current.append(char)
        used += width
    if current:
        chunks.append("".join(current))
    return chunks
_COMPUTE_HOST_INTERVAL = 10.0


def _refresh_age_label(age):
    if age is None:
        return "age unknown"
    return _text_age(time.time() - max(0.0, age))


_VERSION_RELEASE_RE = re.compile(r"^v?\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.]+)?$")
_VERSION_BUILD_RE = re.compile(r"-\d+-g[0-9a-fA-F]+$")


def _hearting_version_segments(version):
    """Split a git-described version into quiet visual roles without changing its text."""
    remaining = version
    dirty = ""
    if remaining.endswith("-dirty"):
        remaining = remaining[:-6]
        dirty = "-dirty"

    build = ""
    match = _VERSION_BUILD_RE.search(remaining)
    if match:
        build = match.group(0)
        remaining = remaining[:match.start()]

    if remaining == "unknown":
        base_key = "unknown"
    elif _VERSION_RELEASE_RE.fullmatch(remaining):
        base_key = "version_release"
    else:
        base_key = "version_build"

    segments = [(remaining, base_key)] if remaining else []
    if build:
        segments.append((build, "version_build"))
    if dirty:
        segments.append((dirty, "version_dirty"))
    return segments or [(version, "unknown")]


def _gpu_gib(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    amount = max(0.0, float(value)) / 1024.0
    if value > 0 and amount < 0.1:
        return "0.0"
    if amount >= 10 or amount.is_integer():
        return str(int(round(amount)))
    return ("%.1f" % amount).rstrip("0").rstrip(".")


_GPU_MODEL_PATTERNS = (
    ("rtx6000", re.compile(r"\brtx\s+(?:a\s*)?6000\b")),
    ("rtx4090", re.compile(r"\brtx\s*4090\b")),
    ("rtx5090", re.compile(r"\brtx\s*5090\b")),
    ("blackwell", re.compile(r"\b(?:b100|b200|gb200|gb300)\b|blackwell|\brtx\s*50\d{2}\b")),
    ("hopper", re.compile(r"\b(?:h100|h200|gh200)\b|hopper")),
    ("ada", re.compile(r"\b(?:l4|l40|l40s)\b|ada|\brtx\s*40\d{2}\b")),
    ("ampere", re.compile(r"\b(?:a10|a30|a40|a100|a6000)\b|ampere|\brtx\s*30\d{2}\b")),
    ("turing", re.compile(r"\b(?:t4|rtx\s*20\d{2}|titan\s+rtx)\b|turing")),
    ("legacy", re.compile(r"\bgtx\s+(?:9|10)\d{2}\b|\btitan\s+xp?\b|\b(?:p100|p40|m40|k80|maxwell|pascal)\b")),
)


def _gpu_display_model(model):
    """Terminal model label: vendor-neutral and compact without changing identity."""
    name = _gpu_safe_text(model).replace("NVIDIA ", "", 1)
    return re.sub(r"\bgeneration\b", "Gen.", name, flags=re.IGNORECASE)


def _gpu_model_family(model):
    """Stable display-only GPU family; unknown names fail soft to dim grey."""
    normalized = _gpu_safe_text(model).lower().replace("-", " ")
    for family, pattern in _GPU_MODEL_PATTERNS:
        if pattern.search(normalized):
            return family
    return "other"


def _gpu_model_key(model, active=False):
    """Keep the model-family hue, promoting it only for a working GPU summary."""
    key = "gpu_" + _gpu_model_family(model)
    return key + "_active" if active else key


def _gpu_command_words(command):
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _gpu_path_argument(word):
    """Display-only path value; exclude URLs, ratios and expression-shaped text."""
    prefix, separator, value = word.partition("=")
    if not separator:
        prefix, value = "", word
    if ("/" not in value or "://" in value
            or re.fullmatch(r"\d+/\d+", value)
            or any(char in value for char in "(){};|")):
        return None
    return prefix + separator, value


def _gpu_path_collisions(commands):
    paths = {}
    for command in commands:
        for word in _gpu_command_words(command)[1:]:
            argument = _gpu_path_argument(word)
            if argument:
                value = argument[1].rstrip("/")
                paths.setdefault(os.path.basename(value), set()).add(value)
    return {name for name, values in paths.items() if len(values) > 1}


def _gpu_display_command(command, ambiguous=()):
    """Compact argv paths for the terminal only; structured command stays raw."""
    words = _gpu_command_words(command)
    if not words:
        return ""
    collisions = set(ambiguous) | _gpu_path_collisions([command])
    words[0] = os.path.basename(words[0]) or words[0]
    for index, word in enumerate(words[1:], 1):
        argument = _gpu_path_argument(word)
        if argument:
            prefix, value = argument
            path = value.rstrip("/")
            name = os.path.basename(path)
            if name in collisions:
                parent = os.path.basename(os.path.dirname(path))
                name = parent + "/" + name if parent else name
            words[index] = prefix + (name or value)
    return shlex.join(words)


def _gpu_clip_command(command, width):
    """Cell-bounded middle ellipsis keeps the script prefix and final argv."""
    if _dw(command) <= width:
        return command
    if width <= 1:
        return "…" if width > 0 else ""
    room = width - 1
    head_width = (room + 1) // 2
    words = _gpu_command_words(command)
    for index, word in enumerate(words[1:], 1):
        if word.endswith((".py", ".sh")) and not word.startswith("-"):
            script_width = _dw(shlex.join(words[:index + 1]))
            if index == len(words) - 1:
                # When only interpreter + script remain, the complete filename
                # matters more than the interpreter's last few characters.
                head_width = min(head_width, max(0, room - _dw(shlex.quote(word))))
            else:
                head_width = max(head_width, min(script_width, room - room // 3))
            break
    head = _clip_w(command, head_width, ellipsis="")
    tail = _clip_w(command[::-1], room - _dw(head), ellipsis="")[::-1]
    return head + "…" + tail


_GPU_FOLD_ALL = ("gpu-commands",)


def _gpu_fold_key(host, index):
    return ("gpu-commands", str(host), index)


def _gpu_processes_by_memory(gpu):
    processes = [row for row in (gpu.get("processes") or ()) if isinstance(row, dict)]
    processes.sort(key=lambda process: (
        process.get("used_memory_mib") is None,
        -(process.get("used_memory_mib") or 0)
        if isinstance(process.get("used_memory_mib"), (int, float)) else 0,
        process.get("pid") if isinstance(process.get("pid"), int) else 2**63,
    ))
    return processes


def _gpu_command_room(prefix, width):
    # Curses' _addline reserves the rightmost cell; do not let its final
    # clipping remove the suffix we deliberately kept here.
    return max(0, width - 1 - sum(_dw(text) for text, _key in prefix))


def _gpu_command_text(process, ambiguous):
    command = _gpu_display_command(_gpu_safe_text(process.get("command")), ambiguous)
    return command or os.path.basename(_gpu_safe_text(process.get("process_name"))) or "process"


def _gpu_process_rows(gpu, indent, width, ambiguous=None):
    """Bounded command-only process rows; evidence stays in structured output."""
    rows = []
    processes = _gpu_processes_by_memory(gpu)
    if ambiguous is None:
        ambiguous = _gpu_path_collisions(
            _gpu_safe_text(process.get("command")) for process in processes)
    for process in processes:
        prefix = [(indent + "  ", None), ("↳ ", "dim")]
        command = _gpu_clip_command(_gpu_command_text(process, ambiguous),
                                    _gpu_command_room(prefix, width))
        rows.append(_clip_segs(prefix + [(command, "dim")], width)[0])
    return rows
