"""Compact session line sharing Fleet's values, glyphs and semantic style keys."""
from __future__ import annotations
import math
import time
from .explorer.grid import cell_width, clip_cells
from .model import project_of
from .render_cells import _clean_model, _effort_text, _EFF_SHORT, _glyph, _model_key, _half_up, _LIVE_GLYPH, _eff_key
from .session_handle import sanitize_title

SEPARATOR = " · "


def _text(value, fallback="?"):
    return sanitize_title(value) or fallback


def _percent(value):
    return (str(_half_up(value)) + "%" if isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else "?")


def _level(value):
    return "lvl_r" if type(value) in (int, float) and value >= 80 else (
        "lvl_y" if type(value) in (int, float) and value >= 50 else "lvl_g")


def _remaining(reset):
    if type(reset) not in (int, float) or not math.isfinite(reset):
        return ""
    seconds = int(reset - time.time())
    if seconds <= 0:
        return ""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f"{days}d{hours}h" if days else (f"{hours}h{minutes:02d}m" if hours else f"{minutes}m")


def line_segments(values, columns, *, complete=False):
    """Shorten words before losing a known count; never invent an unknown value.

    Input is the same observed Session fields plus the shared question/route
    projection and the account's seven-day window. Exact native display telemetry
    may supply the session values; it never establishes input readiness.
    """
    columns = max(1, int(columns))
    harness = values.get("harness")
    glyph, state_key = _glyph(values.get("state", "unknown"))
    tag = _text(values.get("tag"), "?").strip("[]")
    prefix = [("[" + tag + "] ", "tag"), (glyph + " ", state_key)]
    effort = _effort_text(values.get("effort"))
    suffix = [(" ", "dim"), (effort, _eff_key(values.get("effort"), False))] if effort else []
    groups = []

    def words(name, text, key, maximum, priority, before=(), after=(), minimum=1):
        text = _text(text)
        groups.append({"name": name, "text": text, "key": key, "budget": min(maximum, cell_width(text)),
                       "minimum": min(minimum, cell_width(text)), "priority": priority,
                       "before": list(before), "after": list(after)})

    def fixed(name, text, key):
        words(name, text, key, cell_width(text), 99, minimum=cell_width(text))

    words("folder", project_of(values.get("cwd")) if values.get("cwd") else None, "loc_repo", 64 if complete else 24, 2,
          before=[("📁 ", "dim")])
    words("title", values.get("title"), "hb_" + str(harness), 48 if complete else 40, 0, prefix)
    if values.get("native_sid"):
        words("thread", values["native_sid"], "dim", 256, 0)
    git = values.get("git") or {}
    branch_after = []
    if git and git.get("repo") != 0:
        operation = git.get("operation")
        flag = "⚠️" + operation if operation in {"merge", "rebase", "cherry-pick"} else (
            "💀merged" if operation == "none" and (git.get("merged") == 1 or git.get("branch_done") == 1) else "")
        if operation is None or operation == "unknown":
            flag = "⚠️?"
        if flag:
            branch_after.append((" " + flag, "lvl_r"))
        for key, glyph in (("ahead", "↑"), ("behind", "↓")):
            if type(git.get(key)) is int and git[key] > 0:
                branch_after.append((" " + glyph + str(git[key]), "branch_s"))
        wt = git.get("extra_worktrees")
        if wt is None:
            branch_after.append((" 🚧 ?", "dim"))
        elif wt > 0:
            branch_after.append((" 🚧 " + str(wt), "branch_s"))
        branch_after.append((" " + str(git.get("observation") or "unknown"), "dim"))
    if git:
        branch_after.append((SEPARATOR + str(git.get("observed_label") or "?"), "dim"))
    words("branch", values.get("branch"), "branch_s", 64 if complete else 24, 1,
          before=[("⎇ ", "dim")], after=branch_after)
    percent = values.get("ctx_pct")
    level = _level(percent)
    gauge = []
    if _percent(percent) != "?":
        filled = max(0, min(10, _half_up(percent / 10)))
        gauge = [("━" * filled, level), ("─" * (10 - filled), "dim"), (" ", "dim")]
    words("context", _percent(percent), level if _percent(percent) != "?" else "dim",
          cell_width(_percent(percent)), 99, before=[("context ", "dim"), *gauge], minimum=cell_width(_percent(percent)))
    from .token_budget import compaction_imminent
    if compaction_imminent(values.get("compaction_pressure")):
        from .config import ui_text
        warning = ui_text("fleet.context.compaction_imminent")
        words("compaction", warning, "lvl_r", cell_width(warning), 99,
              minimum=cell_width(warning))
    words("model", _clean_model(values.get("model")), _model_key(values.get("model")), 48 if complete else 24, 4, after=suffix)
    for label, used, reset in values.get("quota_windows") or [("7d", values.get("weekly_pct"), None)]:
        remaining = _remaining(reset)
        words("quota", _percent(used), _level(used) if used is not None else "dim", 8, 99,
              before=[(_text(label) + " ", "dim")],
              after=[("(↻ " + remaining + ")", "dim")] if remaining else [], minimum=8)
    for label, used in values.get("model_quotas") or ():
        words("quota", _percent(used), _level(used), 8, 99,
              before=[(_text(label) + " ", _model_key(label))], minimum=8)
    route = _text(values.get("route_label"), "")
    if route:
        count = route.split(SEPARATOR, 1)[0]
        words("route", route, "g_idle", 256, 3,
              minimum=cell_width(count) + (1 if count != route else 0))
    count = values.get("answer_wait")
    if count is None or isinstance(count, int) and not isinstance(count, bool) and count > 0:
        fixed("wait", _LIVE_GLYPH["blocked"] + " " + (str(count) if count is not None else "?"), "g_blocked")

    separator = SEPARATOR

    def render():
        output = []
        for index, group in enumerate(groups):
            if index:
                output.append((separator, "dim"))
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
    for name in ("title", "branch", "folder", "model"):
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



def status_lines(values, columns, *, wrap_long=False):
    """Wrap complete native fields in expanded mode; compact mode may shorten."""
    parts = line_segments(values, 10000, complete=True)
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
    # Every field stays in this row whenever the panel is not painting it. While
    # the shell blanks this row behind a visible panel, one compact line keeps
    # that blank to a single row and is still complete enough if it reappears
    # before Claude's next callback.
    rows = ([clip_segments(line_segments(values, columns - 2), columns)]
            if claude_panel_painted(environ, sid) else
            [clip_segments(line, columns) for line in status_lines(values, columns)])
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


def claude_panel_painted(environ, sid):
    """The host's own report that its visible panel paints this session's status."""
    import json
    directory = _shell_directory(environ)
    if not directory or not sid:
        return False
    try:
        path = directory / "claude-panel.json"
        if path.lstat().st_size > 1024:
            return False
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("painted") is True and value.get("session_id") == sid


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
