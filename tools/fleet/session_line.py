"""Compact session line sharing Fleet's values, glyphs and semantic style keys."""
from __future__ import annotations
import math
import re
import time
from .explorer.grid import cell_width, clip_cells
from .model import project_of
from .render_cells import _clean_model, _effort_text, _EFF_SHORT, _glyph, _model_key, _half_up, _LIVE_GLYPH, _eff_key
from .session_handle import sanitize_title

SEPARATOR = " │ "
# The answer-wait words follow the screen language (spec rule 3, supervisor
# 2026-11-11): the same strings as Fleet #1514's ``ANSWER_WAIT_WORDS``.
ANSWER_WAIT_WORDS = {"ko": "◑답 대기", "en": "◑ to answer"}


def answer_wait_word():
    """``◑답 대기`` on a Korean screen, ``◑ to answer`` otherwise."""
    try:
        from .config import ui_language
        return ANSWER_WAIT_WORDS.get(ui_language(), ANSWER_WAIT_WORDS["en"])
    except Exception:
        return ANSWER_WAIT_WORDS["ko"]
QUOTA_GROUPS = frozenset({"quota", "model_quota"})


def _text(value, fallback="?"):
    return sanitize_title(value) or fallback


def _percent(value):
    return (str(_half_up(value)) + "%" if isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else "?")


def _level(value):
    return "lvl_r" if type(value) in (int, float) and value >= 80 else (
        "lvl_y" if type(value) in (int, float) and value >= 50 else "lvl_g")


def _remaining(reset):
    """Time left until a quota reset in Fleet's spaced form: ``45m``, ``3h 20m``, ``3d 0h``."""
    if type(reset) not in (int, float) or not math.isfinite(reset):
        return ""
    seconds = int(reset - time.time())
    if seconds <= 0:
        return ""
    return elapsed_text(seconds // 60)


def _git_text(values):
    """``main 🚧 3`` and its existing Git warnings, for the dim parentheses."""
    git = values.get("git") or {}
    branch = _text(values.get("branch"), "")
    parts = [clip_cells(branch, 32)] if branch else []
    if git and git.get("repo") != 0:
        operation = git.get("operation")
        flag = "⚠️" + operation if operation in {"merge", "rebase", "cherry-pick"} else (
            "💀merged" if operation == "none" and (git.get("merged") == 1 or git.get("branch_done") == 1) else "")
        if operation is None or operation == "unknown":
            flag = "⚠️?"
        if flag:
            parts.append(flag)
        for key, glyph in (("ahead", "↑"), ("behind", "↓")):
            if type(git.get(key)) is int and git[key] > 0:
                parts.append(glyph + str(git[key]))
        wt = git.get("extra_worktrees")
        if wt is None:
            parts.append("🚧 ?")
        elif type(wt) is int and wt > 0:
            parts.append("🚧 " + str(wt))
    return " ".join(parts)


def gauge_parts(percent, *, cells=10):
    """``━━━━──── 42%`` in the level color, or [] for an unobserved value."""
    if _percent(percent) == "?":
        return []
    level = _level(percent)
    filled = max(0, min(cells, _half_up(percent * cells / 100)))
    return [("━" * filled, level), ("─" * (cells - filled), "dim"), (" ", "dim"), (_percent(percent), level)]


def effort_label(effort):
    text = _effort_text(effort)
    return "med" if text == "medium" else text


def line_segments(values, columns, *, complete=False, reported_only=False, fields=None):
    """``[tag] project (git) │ model effort │ context ━━── N% │ ◑답 대기 N │ route N │ 5h … 7d …``.

    Shorten words before losing a known count; never invent an unknown value.
    ``fields`` keeps only the named groups that carry a value, for a surface
    which already paints the others; an absent value is left out rather than
    painted as ``?``.

    ``reported_only`` leaves out the context and quota readings the native
    callback has not reported yet (Claude sends them only after its first
    response), as the native statusline did, instead of painting ``?``.

    Input is the same observed Session fields plus the shared question/route
    projection and the account's seven-day window. Exact native display telemetry
    may supply the session values; it never establishes input readiness.
    """
    columns = max(1, int(columns))
    groups = []

    def words(name, text, key, maximum, priority, before=(), after=(), minimum=1, short=None):
        if fields is not None and (name not in fields or text is None or text == ""):
            return
        text = _text(text)
        groups.append({"name": name, "text": text, "key": key, "budget": min(maximum, cell_width(text)),
                       "minimum": min(minimum, cell_width(text)), "priority": priority,
                       "before": list(before), "after": list(after), "short": short})

    def fixed(name, text, key, short=None):
        words(name, text, key, cell_width(text), 99, minimum=cell_width(text), short=short)

    tag = _text(values.get("tag"), "").strip("[]")
    project = project_of(values.get("cwd")) if values.get("cwd") else None
    git = _git_text(values)
    if values.get("git") and (values["git"] or {}).get("repo") == 0:
        git = "no-git"
    after = [(" (" + git + ")", "dim")] if git else []
    if project:
        words("folder", project, "loc_repo", 64 if complete else 24, 2,
              before=[("[" + tag + "] ", "tag")] if tag else [], after=after)
    elif tag and (fields is None or "folder" in fields):
        groups.append({"name": "folder", "text": "[" + tag + "]", "key": "tag", "budget": cell_width(tag) + 2,
                       "minimum": cell_width(tag) + 2, "priority": 99, "before": [], "after": after, "short": None})
    if values.get("native_sid"):
        words("thread", values["native_sid"], "dim", 256, 0)
    effort = effort_label(values.get("effort"))
    suffix = [(" ", "dim"), (effort, _eff_key(values.get("effort"), False))] if effort else []
    words("model", _clean_model(values.get("model")), _model_key(values.get("model")), 48 if complete else 24, 4, after=suffix)
    percent = values.get("ctx_pct")
    unreported = reported_only and _percent(percent) == "?"
    if not unreported:
        gauge = gauge_parts(percent)
        words("context", _percent(percent), _level(percent) if gauge else "dim",
              cell_width(_percent(percent)), 99, before=[("context ", "dim"), *gauge[:3]],
              minimum=cell_width(_percent(percent)))
    from .token_budget import compaction_imminent
    if compaction_imminent(values.get("compaction_pressure")):
        from .config import ui_text
        warning = ui_text("fleet.context.compaction_imminent")
        words("compaction", warning, "lvl_r", cell_width(warning), 99,
              minimum=cell_width(warning))
    count = values.get("answer_wait")
    if count is None or isinstance(count, int) and not isinstance(count, bool) and count > 0:
        number = str(count) if count is not None else "?"
        fixed("wait", answer_wait_word() + " " + number, "g_blocked", short=_LIVE_GLYPH["blocked"] + " " + number)
    route = route_text(_text(values.get("route_label"), ""))
    if route:
        words("route", route, "g_idle", 256, 3, minimum=cell_width(route))
    windows = values.get("quota_windows") or [("7d", values.get("weekly_pct"), None)]
    if reported_only:
        windows = [window for window in windows if _percent(window[1]) != "?"]
    for label, used, reset in windows:
        remaining = _remaining(reset)
        words("quota", _percent(used), _level(used) if used is not None else "dim", 8, 99,
              before=[(_text(label) + " ", "dim")],
              after=[("(↻ " + remaining + ")", "dim")] if remaining else [], minimum=8)
    for label, used in values.get("model_quotas") or ():
        words("model_quota", _percent(used), _level(used), 8, 99,
              before=[(_text(label) + " ", _model_key(label))], minimum=8)

    separator = SEPARATOR

    def render():
        output = []
        for index, group in enumerate(groups):
            if index:
                joined = group["name"] in QUOTA_GROUPS and groups[index - 1]["name"] in QUOTA_GROUPS
                output.append((" " if joined else separator, "dim"))
            output.extend(group["before"])
            output.append((clip_cells(group["text"], group["budget"]), group["key"]))
            output.extend(group["after"])
        return output

    def size():
        return sum(cell_width(text) for text, _key in render())

    for group in sorted(groups, key=lambda group: group["priority"]):
        extra = max(0, size() - columns)
        group["budget"] -= min(extra, group["budget"] - group["minimum"])
    if size() > columns:
        separator = " "
    for group in groups:
        if size() > columns and group.get("short"):
            group["text"] = group["short"]; group["budget"] = cell_width(group["short"])
    for name in ("thread", "folder", "model"):
        if size() <= columns:
            break
        groups[:] = [group for group in groups if group["name"] != name]
    output = render()
    if size() > columns:
        # At exceptionally small sizes the native terminal still gets its full
        # viewport. Preserve the two action counts before optional telemetry.
        groups[:] = [group for group in groups if group["name"] in {"route", "wait"}]
        output = render()
    return output


def wrap_segments(parts, columns):
    """Wrap semantic spans at cell boundaries, retaining their Fleet colors."""
    columns = max(1, columns)
    result, row, width = [], [], 0
    for text, key in parts:
        for char in str(text):
            if ord(char) < 32 or ord(char) == 127:
                char = " "
            size = cell_width(char)
            if width + size > columns and row:
                result.append(row); row=[]; width=0
            if row and row[-1][1] == key:
                row[-1] = (row[-1][0] + char, key)
            else:
                row.append((char, key))
            width += size
    if row:
        result.append(row)
    return result



def status_lines(values, columns, *, wrap_long=False, reported_only=False, fields=None):
    """Wrap complete native fields in expanded mode; compact mode may shorten."""
    parts = line_segments(values, 10000, complete=True, reported_only=reported_only, fields=fields)
    groups = [[]]
    for part in parts:
        if part == (SEPARATOR, "dim"):
            groups.append([])
        else:
            groups[-1].append(part)
    result, line, width = [], [], 0
    for group in groups:
        size = sum(cell_width(text) for text, _ in group)
        if line and width + cell_width(SEPARATOR) + size > columns:
            result.append(line);line=[];width=0
        if line:
            line.append((SEPARATOR, "dim"));width+=cell_width(SEPARATOR)
        if size > columns and wrap_long:
            result.extend(wrap_segments(group, columns))
            continue
        if size > columns:
            # A single long name cannot consume rows intended for questions.
            clipped, left = [], columns
            for text, key in group:
                text = clip_cells(text, max(0, left))
                clipped.append((text, key));left -= cell_width(text)
            group = clipped
            size = sum(cell_width(text) for text, _ in group)
        line.extend(group);width+=size
    if line:
        result.append(line)
    if values.get("jobs"):
        job = [(">_", "lvl_g"), (" running: ", "dim"), (_text(values["jobs"]), "loc_repo")]
        result.extend(wrap_segments(job, columns) if wrap_long else [job])
    return result


def clip_segments(parts, columns):
    result = []
    for text, key in parts:
        clean = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in str(text))
        value = clip_cells(clean, max(0, columns))
        result.append((value, key));columns -= cell_width(value)
        if columns <= 0:
            break
    return result


def claude_native_statusline(callback, environ):
    """Paint the shared status area in Claude's existing native callback row.

    The shell has already resolved the session subject and Git observation. Reuse
    those facts; only native callback telemetry and cached badges are read here.
    None of these display fields establish input or process authority.
    """
    import sys
    from pathlib import Path
    from .native_display import claude_fields
    from .palette import ansi_segments
    from .session_tags import assigned_tag
    from . import session_registry
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utilities"))
    import human_wait_state
    values = claude_fields(callback)
    sid = callback.get("session_id") or ""
    git = {}
    for key in ("repo", "operation", "merged", "branch_done", "extra_worktrees", "ahead", "behind",
                "dirty", "observation", "observed_label"):
        raw = environ.get("git_" + key)
        git[key] = None if raw in (None, "unknown") else int(raw) if raw.isdigit() else raw
    branch = environ.get("git_branch")
    if branch == "unknown":
        branch = None
    if git["repo"] == 0:
        branch = "no-git"
    elif branch and git["dirty"] == 1:
        branch += "*"
    record = None
    pid = environ.get("HEARTING_STATUS_DISPLAY_PID", "")
    if pid.isdigit() and int(pid) > 1:
        from .collectors.procscan import read_proc_start
        before = read_proc_start(int(pid))
        record = session_registry.read("claude", int(pid))
        if (not record or str(record.get("procStart")) != before or read_proc_start(int(pid)) != before):
            record = None
    state = "unknown"
    if record and record.get("sessionId") == sid:
        state = {"busy": "working", "idle": "idle"}.get(record.get("status"), "unknown")
    counts, route = human_wait_state.cached_statusline("claude", sid)
    waiting = (counts[0] if counts[0] >= 0 else None) if counts and counts[1] > 0 else (0 if counts else None)
    values.update(title=environ.get("S_SESSION_DISPLAY"), git=git, branch=branch,
                  jobs=environ.get("git_jobs"), state=state, tag=assigned_tag("claude", sid),
                  route_label=route, answer_wait=waiting)
    try:
        columns = max(20, min(10000, int(environ.get("COLUMNS", "100"))))
    except ValueError:
        columns = 100
    colors = 256 if "256color" in environ.get("TERM", "") or environ.get("COLORTERM") else 8
    # Every field stays in this row on every callback. While the visible panel
    # paints these values, the shell blanks exactly this recorded output; if the
    # panel goes away, the native row is already complete.
    rows = [clip_segments(line, columns) for line in status_lines(values, columns, reported_only=True)]
    _record_claude_rows(environ, sid, ["".join(text for text, _key in row) for row in rows])
    return "\n".join(ansi_segments(row, colors=colors, no_color="NO_COLOR" in environ) for row in rows)


def _shell_directory(environ):
    """The wrapper's private directory; any doubt means no shell display state."""
    import os, stat
    from pathlib import Path
    endpoint = environ.get("HEARTING_SHELL_SOCKET")
    if not endpoint:
        return None
    directory = Path(environ.get("HEARTING_SHELL_DIRECTORY") or Path(endpoint).parent)
    try:
        info = directory.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return None
    return directory


def _record_claude_rows(environ, sid, rows):
    """Record the plain text Claude is about to paint, for exact-match blanking."""
    import json, os
    if environ.get("HEARTING_SHELL_INITIAL_REFRESH") or not sid:
        return  # That refresh's output is discarded and never painted.
    directory = _shell_directory(environ)
    if not directory:
        return
    path = directory / "claude-status.json"
    previous = []
    try:
        if path.lstat().st_size <= 65536:
            old = json.loads(path.read_text())
            if isinstance(old, dict) and old.get("session_id") == sid and isinstance(old.get("lines"), list):
                previous = old["lines"]
    except (OSError, ValueError):
        pass
    if previous == rows:
        return
    temp = directory / (".claude-status." + str(os.getpid()))
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"session_id": sid, "lines": rows, "previous": previous}, stream, ensure_ascii=False)
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


_CLAUDE_DISPLAY_FIELDS=('S_SESSION_DISPLAY','git_repo','git_branch','git_operation','git_merged',
    'git_branch_done','git_extra_worktrees','git_ahead','git_behind','git_dirty',
    'git_observation','git_observed_label','git_jobs')


def remember_claude_display(callback, environ):
    """Retain display metadata in the existing exact-session tap, never authority."""
    import json, os
    from pathlib import Path
    sid=callback.get('session_id') or ''
    if not sid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._' for c in sid):
        return
    home=Path(environ.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude')
    path=home/'.statusline'/(sid+'.json')
    from contextlib import ExitStack
    import fcntl
    try:
        with ExitStack() as stack:
            endpoint=environ.get('HEARTING_SHELL_SOCKET')
            marker=Path(endpoint).parent/'claude-first-display.json' if endpoint else None
            if marker and marker.exists():
                guard=stack.enter_context(marker.open('r+'))
                fcntl.flock(guard,fcntl.LOCK_EX)
            if path.stat().st_size>65536:return
            before=path.stat()
            value=json.loads(path.read_text())
            if value.get('session_id')!=sid:return
            value['hearting_display']={key:environ[key] for key in _CLAUDE_DISPLAY_FIELDS if key in environ}
            raw=json.dumps(value,ensure_ascii=False)
            if len(raw.encode())>65536:return
            temp=path.with_name('.'+path.name+'.display-'+str(os.getpid()))
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream:stream.write(raw)
            now=path.stat()
            if (before.st_ino,before.st_mtime_ns)!=(now.st_ino,now.st_mtime_ns):
                temp.unlink();return
            os.replace(temp,path)
    except (OSError,ValueError,TypeError):return


def claude_first_statusline(callback, environ, script):
    """Paint cached metadata/current native telemetry before finite refresh work."""
    import json,os,stat,subprocess,sys,tempfile
    from pathlib import Path
    explicit=environ.get('HEARTING_SHELL_DIRECTORY')
    if not explicit:
        try:
            endpoint=Path(environ.get('HEARTING_SHELL_SOCKET',''))
            info=endpoint.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():return None
            explicit=str(endpoint.parent)
        except OSError:return None
    directory=Path(explicit)
    from .native_display import claude_fields
    known=claude_fields(callback).get('ctx_pct') is not None
    marker=directory/'claude-first-display.json'
    try:
        info=directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:return None
        try:
            fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:
            info=marker.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_size>1024:return None
            previous=json.loads(marker.read_text())
            if previous.get('session_id')==callback.get('session_id') and (previous.get('numeric') or not known):return None
            fd=os.open(marker,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW)
    except (OSError,ValueError,TypeError):return None
    with os.fdopen(fd,'w') as stream:json.dump({'session_id':callback.get('session_id'),'numeric':known},stream)
    env=dict(environ)
    sid=callback.get('session_id') or ''
    home=Path(env.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude')
    if sid and all(c.isalnum() or c in '-._' for c in sid):
        path=home/'.statusline'/(sid+'.json')
        try:
            if path.stat().st_size<=65536:
                old=json.loads(path.read_text())
                if old.get('session_id')==sid and old.get('cwd')==callback.get('cwd'):
                    for key,value in (old.get('hearting_display') or {}).items():
                        if isinstance(value,str) and key in _CLAUDE_DISPLAY_FIELDS:env[key]=value
        except (OSError,ValueError,TypeError):pass
    # The same shared Git observation the full callback reads: a file read, no
    # Git command. A new session has no retained display yet, and Claude only
    # calls again on its first response or refreshInterval.
    try:
        from . import gitinfo
        workspace=callback.get('workspace') if isinstance(callback.get('workspace'),dict) else {}
        cwd=callback.get('cwd') or workspace.get('current_dir')
        if isinstance(cwd,str) and cwd:
            project=workspace.get('project_dir') if isinstance(workspace.get('project_dir'),str) else None
            observed=gitinfo.cached_display(cwd,request=False,project_dir=project or cwd)
            if observed is not None:
                for key,item in gitinfo.display_fields(observed).items():
                    if 'git_'+key in _CLAUDE_DISPLAY_FIELDS:
                        text='unknown' if item is None else str(item)
                        env['git_'+key]=''.join(c for c in text if c>=' ' and c!='\x7f')
    except (OSError,ValueError,TypeError,ImportError):pass
    # Preserve the same native PID/SID and birth check when the finite refresh
    # outlives this short native callback shell.
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'utilities'))
    import claude_process_identity
    try:
        current,_tap,_policy=claude_process_identity.statusline_bindings(env.get('HEARTING_STATUS_CALLER_PID',''),sid,str(home))
        if current:
            pid=str(current['pid'])
            from .collectors.procscan import read_proc_start
            env.update(HEARTING_STATUS_CALLER_PID=pid,HEARTING_STATUS_CALLER_START=read_proc_start(int(pid)),HEARTING_STATUS_DISPLAY_PID=pid)
    except (OSError,ValueError,TypeError):pass
    line=claude_native_statusline(callback,env)
    env['HEARTING_SHELL_INITIAL_REFRESH']='1'
    with tempfile.TemporaryFile() as source:
        source.write(json.dumps(callback).encode());source.seek(0)
        subprocess.Popen(['bash',script],env=env,stdin=source,
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    return line


def context_detail_lines(detail):
    """Common detail labels retain server usage beside a separate native estimate."""
    from .token_budget import current_compaction_pressure
    from .config import ui_text
    if not isinstance(detail, dict) and not hasattr(detail, "get"):
        return []
    def number(value):
        return f"{value:,}" if type(value) is int and value >= 0 else "?"
    pressure = detail.get("pressure")
    observed = current_compaction_pressure(pressure)
    return [
        (ui_text("fleet.context.server"), number(detail.get("server_total_tokens")) + " / " + number(detail.get("server_window_tokens"))),
        (ui_text("fleet.context.native_estimate"), number(pressure.get("scope_tokens")) if observed else "?"),
        (ui_text("fleet.context.limit"), (number(pressure.get("limit_tokens")) + " · " + str(pressure.get("scope"))) if observed else "?"),
        (ui_text("fleet.context.observed"), time.strftime("%H:%M:%S", time.localtime(pressure["observed_at"])) if observed else "?"),
    ]


# -- routes: one row per route, from the shared answer-wait cache ------------------------
# The cache producer already interprets each displayed session's routes once
# (``explorer.live.session_route_status``). These helpers add the few values a
# route row shows from the jobs of that very snapshot: no second collection,
# timer or process. Display only; none of this is liveness or input authority.

# The one place the shell judges a route row and counts the states. It follows
# Fleet #1514 (``explorer.live.route_marker_state``/``route_state_counts``/
# ``_route_doer``): once those are on main, ``route_marker`` and ``route_doer``
# below call them (the ``getattr`` lines) and this fallback goes unused.
ROUTE_MARKS = {"working": "⠏", "idle": "●", "blocked": "●", "dead": "✕", "done": "✓", "stale": "◌"}
_TALLY = (("running", "⠏"), ("waiting", "●"), ("stopped", "✕"))


def route_closed(route):
    """A route its supervisor closed (the outcome sidecar's ``closed_at``) is over."""
    if route.get("closed"):
        return True
    codes = (route.get("visibility") or {}).get("codes") if isinstance(route.get("visibility"), dict) else None
    verification = codes.get("verification") if isinstance(codes, dict) else None
    return bool(isinstance(verification, dict) and verification.get("closed_at"))


def route_marker(route):
    """Fleet's route marker: working, idle (waits), blocked, dead, done or stale."""
    from .explorer import live
    shared = getattr(live, "route_marker_state", None)
    if shared is not None:
        return shared(route)
    if route.get("owner_waits"):
        return "idle"
    if route.get("activity") == "running":
        return "working"
    state = str(route.get("state") or "")
    return state if state in ("working", "blocked", "dead", "done", "stale") else "idle"


def route_tally(markers):
    """``{"running", "waiting", "stopped"}`` as Fleet's ``route_state_counts`` counts them,
    over (marker, closed) pairs of current routes; a closed route is in neither count."""
    counts = {"running": 0, "waiting": 0, "stopped": 0}
    for marker, closed in markers:
        if closed:
            continue
        if marker == "working":
            counts["running"] += 1
        elif marker in ("idle", "blocked"):
            counts["waiting"] += 1
        elif marker == "dead":
            counts["stopped"] += 1
    return counts


def route_glyph(marker):
    """The glyph and color key Fleet's route row draws for a marker (the spinner frame while working)."""
    from .render_cells import _state_key
    if marker == "working":
        return _glyph("working")
    return ROUTE_MARKS.get(marker, "·"), _state_key(marker if marker in _GLYPH_STATES else "unknown")


_GLYPH_STATES = frozenset(("working", "idle", "blocked", "dead", "done", "stale"))


# The producer's route label, ``route N …``. An earlier release wrote it with a
# leading ``⏳ ``, and a shared cache may still hold that until it is rewritten.
ROUTE_LABEL = re.compile(r"(?:⏳ )?route (\d+)")


def route_text(label):
    """The route label as drawn now: an earlier ``⏳ route …`` without its hourglass."""
    return label[2:] if isinstance(label, str) and label.startswith("⏳ route ") else label


def route_badge(label, panels, markers=None):
    """``route 3 ⠏2 ✕1``: the route count with Fleet's running/waiting/stopped counts.

    ``label`` is the producer's own ``route N`` reading (an earlier release's
    ``⏳ route N`` still reads, and is written back without it); its first N panel
    rows are those current routes. ``markers`` are the (marker, closed) pairs of
    all N current routes from the same pass, so a count past the panel's kept
    rows is still tallied. Without every marker the count stays alone.
    """
    match = ROUTE_LABEL.match(label or "")
    if not match:
        return label or ""
    count = int(match.group(1))
    if markers is None or len(markers) != count:
        rows = [row for row in (panels or ())[:count] if isinstance(row, dict) and row.get("marker")]
        markers = [(row["marker"], bool(row.get("closed"))) for row in rows]
    if len(markers) != count:
        return "route " + str(count)
    tally = route_tally(markers)
    return " ".join(["route " + str(count)] + [glyph + str(tally[name]) for name, glyph in _TALLY if tally[name]])


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def job_context(job, get):
    """``(percent, tokens)``: Fleet's own gauge percent, else tokens over a known window,
    else the exact token count with no percent; ``(None, None)`` for a stopped job."""
    if get(job, "liveness") in ("stale", "dead"):
        return None, None
    percent = None
    try:
        from .explorer import live
        percent = _number(live._context_gauge(job)[0])
    except Exception:
        percent = None
    if percent is None:
        percent = _number(get(job, "ctx_pct"))
    tokens, window = _number(get(job, "active_context_tokens")), _number(get(job, "context_window_tokens"))
    if percent is None and tokens is not None and window:
        percent = tokens * 100.0 / window
    if percent is not None:
        return min(100.0, percent), None
    return None, int(tokens) if tokens is not None else None


def route_doer(route, jobs, get, live):
    """Who does the route's work now: the live worker of the shown stage, else the owner.

    Fleet #1514 records this as ``route["doer"]``; when present it is used as is.
    """
    shared = route.get("doer") if isinstance(route, dict) else None
    if isinstance(shared, dict):
        pair = shared.get("model")
        return {"harness": shared.get("harness"), "model": pair[0] if isinstance(pair, (list, tuple)) and pair else None,
                "effort": shared.get("effort"), "percent": _number(shared.get("context")),
                "tokens": shared.get("tokens") if type(shared.get("tokens")) is int else None,
                "tool": shared.get("tool"), "agents": shared.get("agents") or 0,
                **{key: shared[key] for key in ("target", "tool_age", "idle_tool", "after_tool")
                   if shared.get(key)}}
    stage = (route or {}).get("worker_stage")
    owners = live._owner_jobs(jobs)
    workers = [job for job in jobs if job not in owners and stage and get(job, "route_node") == stage
               and get(job, "liveness") in ("working", "idle", "blocked")]
    owner = owners[0] if len(owners) == 1 else None
    job = workers[0] if len(workers) == 1 else owner
    if job is None:
        return None
    harness = get(job, "harness")
    if (route or {}).get("state") in ("dead", "stale"):
        return {"harness": harness}
    pair = live._model_pair(get(job, "resolved_model", "model"))
    percent, tokens = job_context(job, get)
    tool = get(job, "exec_tool")
    name = tool.get("name") if isinstance(tool, dict) else get(tool, "name") if tool is not None else None
    working = get(job, "liveness") == "working"
    agents = get(job, "subagents") or ()
    activity = live._doer_activity(job, tool if working and name else None, working)
    return {"harness": harness, "model": pair[0] if pair else None, "effort": get(job, "effort"),
            "percent": percent, "tokens": tokens, "tool": name if working else None,
            "agents": sum(1 for agent in agents if get(agent, "active") is True)
                      if working and isinstance(agents, (list, tuple)) else 0,
            **{key: activity[key] for key in ("target", "tool_age", "idle_tool", "after_tool")
               if key in activity}}


def route_details(snapshot, panels, routes=None, now=None):
    """Add each panel row's marker, closed flag and doer values, and its start time.

    Rows are changed in place; ``routes`` are Fleet's own route records by id.
    A value that was not reported is left out. The start time is absolute so a
    re-published pass still ages correctly.
    """
    from .explorer import live
    now = time.time() if now is None else now
    get = live._get
    collected = live._collect_routes(snapshot)
    routes = routes or {}
    seen = set()
    for rows in (panels or {}).values():
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, dict) or id(row) in seen:
                continue
            seen.add(id(row))
            route = routes.get(row.get("id"))
            if isinstance(route, dict):
                row["marker"] = route_marker(route)
                if route_closed(route):
                    row["closed"] = True
            slot = collected.get(row.get("id"))
            jobs = list(slot.get("jobs") or ()) if slot else []
            # The route's current owner, chosen as Fleet's route row chooses it (the
            # record's owner_attempt_id, else the single depth-1 owner): the shell's
            # route page reads this owner's output, not an earlier attempt's.
            expected = live._text(((slot or {}).get("record") or {}).get("owner_attempt_id"))
            owner = live._owner_job([job for job in jobs if expected in (None, "-")
                                     or get(job, "attempt_id") == expected])
            attempt = get(owner, "attempt_id") if owner is not None else None
            if isinstance(attempt, str) and 0 < len(attempt) <= 128:
                row["owner_attempt"] = attempt
            elapsed = [value for value in (_number(get(item, "elapsed_min")) for item in jobs) if value is not None]
            if elapsed:
                if any(get(item, "liveness") in ("working", "idle", "blocked") for item in jobs):
                    row["since"] = now - max(elapsed) * 60
                else:
                    row["elapsed_min"] = round(max(elapsed), 1)
            doer = route_doer(route if isinstance(route, dict) else row, jobs, get, live)
            if not doer:
                continue
            for key, limit in (("harness", 64), ("model", 128), ("effort", 64), ("tool", 64),
                               ("target", 120), ("tool_age", 16), ("after_tool", 160)):
                if isinstance(doer.get(key), str) and 0 < len(doer[key]) <= limit:
                    row[key] = doer[key]
            if doer.get("idle_tool") is True:
                row["idle_tool"] = True
            if doer.get("percent") is not None:
                row["ctx_pct"] = round(doer["percent"], 1)
            elif type(doer.get("tokens")) is int:
                row["ctx_tokens"] = doer["tokens"]
            if type(doer.get("agents")) is int and doer["agents"] > 0:
                row["agents"] = doer["agents"]
    return panels


def session_route_status(snapshot, now=None, *, include_details=False):
    """Fleet's question-free route labels, with the row values the shell panel paints.

    Fleet's own route records are read from the same interpretation pass (no
    second pass): its record builder is observed for the duration of the call.
    """
    from .explorer import live
    if not include_details:
        return live.session_route_status(snapshot, now)
    captured = []
    original = live._route_records

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result[0])
        return result

    live._route_records = observed
    try:
        labels, panels = live.session_route_status(snapshot, now, include_details=True)
    finally:
        live._route_records = original
    try:
        routes = {route.get("id"): route for route in (captured[-1] if captured else ()) if isinstance(route, dict)}
        route_details(snapshot, panels, routes, now)
        # The statusline reads only this label: count the states here, once, over
        # every current route of the session (the panel keeps at most eight rows).
        by_tag = {}
        for route in routes.values():
            if route.get("current") is True and route.get("session_tag"):
                by_tag.setdefault(route["session_tag"], []).append((route_marker(route), route_closed(route)))

        def markers(key):
            for row in panels.get(key) or ():
                route = routes.get(row.get("id")) if isinstance(row, dict) else None
                if isinstance(route, dict) and route.get("session_tag"):
                    return by_tag.get(route["session_tag"])
            return None
        labels = {key: route_badge(label, panels.get(key), markers(key)) for key, label in labels.items()}
    except Exception:
        pass  # Optional display values never cost the labels.
    return labels, panels


def token_text(tokens):
    """``250K`` as Fleet writes an exact token count with no known window."""
    if type(tokens) is not int or tokens < 0:
        return ""
    if tokens < 1000:
        return str(tokens)
    if tokens < 999_500:
        return f"{round(tokens / 1000)}K"
    return f"{tokens / 1_000_000:.1f}M"


def elapsed_text(minutes):
    """``42m``, ``1h 27m`` or ``2d 3h`` (Fleet's ``_dur``); empty for an unknown value."""
    if _number(minutes) is None:
        return ""
    minutes = int(minutes)   # whole minutes, as Fleet's ``_dur``
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def stage_glyph(state):
    return {"active": "●", "running": "●", "working": "●", "reconciling": "●", "recovering": "●",
            "done": "✓", "completed": "✓", "failed": "✕", "dead": "✕"}.get(str(state or ""), "")


# -- messages: what this session and other sessions sent each other -----------------------

def peer_message_rows(received, sent, tag_of, limit=3):
    """The latest ``limit`` peer messages, oldest first: ``{"way", "who", "text", "at"}``.

    ``received`` are the exact session's retained received inputs (source
    ``peer``); ``sent`` are its own ledger records. Bodies are what those
    records already keep; nothing is fetched or reconstructed here.
    """
    rows = []
    for row in received or ():
        if not isinstance(row, dict) or row.get("source") != "peer":
            continue
        tag = tag_of(row.get("sender_harness"), row.get("sender_session_id"))
        who = "[" + tag.strip("[]") + "]" if tag else _text(row.get("sender"), "peer")
        rows.append({"way": "←", "who": who, "text": _text(row.get("text"), ""), "at": row.get("at") or 0})
    for record in sent or ():
        target = record.get("to") if isinstance(record.get("to"), dict) else {}
        tag = tag_of(target.get("harness"), target.get("session_id"))
        who = "[" + tag.strip("[]") + "]" if tag else _text(target.get("name"), "peer")
        rows.append({"way": "→", "who": who, "text": _text(record.get("summary"), ""), "at": record.get("at") or 0})
    rows = [row for row in rows if row["text"]]
    rows.sort(key=lambda row: row["at"])
    return rows[-limit:]
