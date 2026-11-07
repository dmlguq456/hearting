"""Read-only COMPUTE, MEMORY, and SYSTEM projections for Fleet explorer."""
import re
from collections.abc import Mapping

from ..config import ui_text
from .grid import CellGrid, cell_width
from .grid import clip_cells as clip


_DIGEST = re.compile(r"[0-9a-f]{12,}")


def short_cmd(command):
    """The script first, paths cut to their names, digest arguments dropped, short options kept;
    a command with no script keeps its program."""
    parts = [part.rstrip("/").rsplit("/", 1)[-1] if "/" in part else part for part in command.split()]
    first = next((index for index, part in enumerate(parts) if part.endswith(".py")), 0)
    shown = []
    for part in parts[first:]:
        if _DIGEST.fullmatch(part.split("=")[-1]):
            if shown and shown[-1].startswith("-") and "=" not in part:
                shown.pop()
            continue
        shown.append(part)
    return " ".join(shown)


def _table_head(grid, x, width, y, columns):
    grid.fill(x, min(grid.cols - 1, x + width + 1), y, " ", tint=234)
    for offset, label in columns:
        grid.put(x + offset, y, label, "head", tint=234)
    return y + 1


def _group_sep(grid, x, width, y):
    for column in range(x + 1, min(grid.cols, x + width + 1)):
        grid.put(column, y, "┈", "line")
    return y + 1


def _pct_key(value):
    if value is None:
        return "dim"
    return "lvl_r" if value >= 80 else "lvl_y" if value >= 50 else "lvl_g"


def _gauge(grid, x, y, width, value, tint=None, limit=None):
    if value is None:
        grid.put(x, y, "·" * width, "dim", tint=tint, limit=limit)
        return
    pct = max(0, min(100, value))
    filled = width if pct >= 100 else 0 if pct <= 0 else max(1, min(width - 1, int(width * pct / 100 + .5)))
    grid.put(x, y, "━" * filled, _pct_key(value), tint=tint, limit=limit)
    grid.put(x + filled, y, "─" * (width - filled), "dim", tint=tint, limit=limit)


def draw_compute(grid: CellGrid, vm, state=None, x=34, y=3):
    """Display collected host/GPU facts; absent measurements stay visibly unknown."""
    use = max(0, grid.cols - x - 2)
    hosts = tuple(vm.compute or ())
    data = vm.system if isinstance(vm.system, Mapping) else {}
    processes = tuple(data.get("gpu_processes") or ())
    # The r14 reference carries session identity on each GPU process row and has
    # no USED BY table column.
    users_col = False
    y = _table_head(grid, x, use, y, ((1, "HOST"), (11, "GPU"), (16, "MODEL"),
                                      (31, "UTIL"), (38, "VRAM")) + (((64, "USED BY"),) if users_col else ()))
    labels = tuple(vm.hosts or ())
    for host_index, record in enumerate(hosts):
        if host_index:
            y = _group_sep(grid, x, use, y)
        try:
            host, base, cpu, mem, swap, gpus = record
        except (TypeError, ValueError):
            host, base, cpu, mem, swap, gpus = "·", False, None, None, None, ()
        if host_index < len(labels) and isinstance(labels[host_index], Mapping):
            host = labels[host_index].get("name", host)
            base = labels[host_index].get("key") == "base_host"
        for gpu_index, gpu in enumerate(gpus or ()):
            idx, status, model, model_key, util, used, total, users = (tuple(gpu) + (None,) * 8)[:8]
            if gpu_index == 0:
                grid.put(x + 1, y, host, "work" if base else "soft")
            mark, status_key = (("●", "spin") if status == "working" else
                                {"idle": ("●", "idle"), "done": ("✓", "done"),
                                 "dead": ("✕", "dead")}.get(status, ("·", "dim")))
            grid.put(x + 11, y, mark, status_key)
            grid.put(x + 13, y, "·" if idx is None else str(idx), "dim")
            gpu_styles = {"gpu_ada": "h_claude", "gpu_5090": "g_work_off",
                          "gpu_4090": "h_claude", "gpu_legacy": "unknown"}
            grid.put(x + 16, y, "·" if model is None else str(model), gpu_styles.get(model_key, model_key or "dim"))
            grid.put(x + 31, y, " · " if util is None else f"{int(util):3d}%",
                     "soft" if status == "working" else "dim")
            if used is None or total in (None, 0):
                _gauge(grid, x + 38, y, 10, None)
                grid.put(x + 51, y, "·", "dim")
            else:
                percent = 100 * float(used) / float(total)
                _gauge(grid, x + 38, y, max(1, round(float(total) / 4)), percent)
                grid.put(x + 51, y, (f"{used:g}/{total:g} GB").rjust(10), "dim")
            cursor = x + 64
            for user in (users or ()) if users_col else ():
                cursor = grid.put(cursor, y, "[", "dim")
                cursor = grid.put(cursor, y, user, "soft")
                cursor = grid.put(cursor, y, "]", "dim") + 1
            y += 1
            for process in processes:
                if not isinstance(process, Mapping):
                    continue
                if str(process.get("host") or "") != str(host) or \
                        str(process.get("gpu") if process.get("gpu") is not None else "") != str(idx):
                    continue
                # New records keep the exact source ID separately from the display tag.
                # Older fixtures already put the short tag in session_id.
                sid_value = (process.get("session_tag") if "session_tag" in process else
                             process.get("session_id") or process.get("session"))
                sid = str(sid_value or "")
                cwd = str(process.get("cwd") or "")
                repo = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd else "·"
                command = short_cmd(str(process.get("command") or "")) or "·"
                age = str(process.get("age") or process.get("time") or "")
                vram = str(process.get("vram") or "·")
                tail = vram.rjust(8) + (("  " + age.rjust(6)) if grid.cols - x - 2 >= 120 else "")
                grid.put(x + 13, y, "└", "dim")
                if sid:
                    session = next((item for item in vm.sessions if str(item.get("tag")) == sid), {})
                    grid.put(x + 16, y, "[", "dim")
                    tag_key = "steward" if session.get("steward") else "soft"
                    grid.put(x + 17, y, sid, tag_key)
                    grid.put(x + 17 + len(sid), y, "]", "dim")
                else:
                    grid.put(x + 17, y, "·", "dim")
                repo_x = max(x + 21, x + 17 + len(sid) + 2)
                grid.put(repo_x, y, clip(repo + "/", 14), "title_dim")
                command_x = repo_x + len(clip(repo + "/", 14)) + 2
                right = x + use
                grid.put(command_x, y, clip(command, max(0, right - command_x - len(tail) - 2)),
                         "soft", limit=right - len(tail))
                grid.put(right - len(tail), y, tail, "dim", limit=right)
                y += 1
        if cpu or mem or swap:
            for label, values, width, unit in (("CPU", cpu, 10, "thr"), ("MEM", mem, 8, "GB"),
                                               ("SWP", swap, 8, "GB")):
                start = x + 11 if label == "CPU" else cursor + 3
                if values and len(values) >= 2 and values[1]:
                    value = f"{values[0]:g}/{values[1]:g} {unit}"
                else:
                    value = "·"
                cursor = grid.put(start, y, label + " ", "label", limit=x + use + 1)
                if value != "·":
                    _gauge(grid, cursor, y, width, 100 * float(values[0]) / float(values[1]), limit=x + use + 1)
                else:
                    _gauge(grid, cursor, y, width, None, limit=x + use + 1)
                cursor += width + 1
                cursor = grid.put(cursor, y, value, "dim", limit=x + use + 1)
            y += 1
    if not hosts:
        grid.put(x + 1, y, "·", "dim")
    busy = sum(1 for host in hosts for gpu in (host[5] if len(host) > 5 else ())
               if len(gpu) > 1 and gpu[1] == "working")
    total = sum(len(host[5]) for host in hosts if len(host) > 5)
    idle_hosts = sum(1 for host in hosts if len(host) > 5 and host[5]
                     and all(len(gpu) > 1 and gpu[1] == "idle" for gpu in host[5]))
    process_summary = data.get("gpu_process_summary") or {}
    process_count = process_summary.get("count", len(processes))
    unregistered = process_summary.get("unregistered", 0)
    grid.put(x + 1, y,
             f"{len(hosts)}/{len(hosts)} hosts · GPU {busy}/{total} working · {idle_hosts} host idle · "
             f"{process_count} processes, {unregistered} not registered with hearting",
             "dim", limit=grid.cols - 1)


OPEN_STAGE_WORDS = {"doing": ("fleet.memory.stage_doing", "g_spin_dim"),     # catalog key, colour
                    "waiting": ("fleet.memory.stage_waiting", "lvl_y"),
                    "open": ("fleet.memory.stage_open", "dim")}


def memory_projects(vm):
    """``(data, order, journal)``: the per-project memory record, its project order, and the
    journal rows by project. Without a DB summary the journal's own projects are the order."""
    system = vm.system if isinstance(vm.system, Mapping) else {}
    data = system.get("project_memory") if isinstance(system.get("project_memory"), Mapping) else {}
    journal = {}
    for item in vm.memory or ():
        try:
            name, rows = item
        except (TypeError, ValueError):
            continue
        journal[str(name)] = tuple(rows or ())
    order = [str(name) for name in data.get("order") or ()]
    order += [name for name in journal if name not in order]
    return data, order, journal


def memory_status(vm):
    """The MEMORY status line: project count, today's decisions, work in progress."""
    data, order, _journal = memory_projects(vm)
    state = str(data.get("state") or "unknown")
    if data.get("decisions_today") is None:
        return ui_text("fleet.memory.status_db", projects=len(order), state=state)
    return (ui_text("fleet.memory.status", projects=len(order), decisions=data.get("decisions_today"),
                    doing=data.get("doing", "·"))
            + ("" if state == "success" else ui_text("fleet.memory.db_suffix", state=state)))


def _memory_column(labels):
    """Where a memory row's time starts: two cells past the widest label, never before x+12."""
    return max(12, 3 + max((cell_width(label) for label in labels), default=0) + 2)


def _memory_line(grid, x, y, right, label, when, text, key="soft", mark=None, column=12):
    if label:
        grid.put(x + 3, y, label, "label", limit=right)
    grid.put(x + column, y, "·" if when is None else str(when), "dim", limit=right)
    cursor = x + column + 7
    if mark is not None:
        word, mark_key = mark
        cursor = grid.put(cursor, y, word, mark_key, limit=right) + 2
    grid.put(cursor, y, clip("·" if text is None else str(text), max(1, right - cursor)), key, limit=right)
    return y + 1


def draw_open_work(grid, x, y, right, project, name, label, *, column=12,
                   opened="", fold_key="memory:open", preview_rows=1):
    """Reuse MEMORY's existing work counts, classifications, and fold rows."""
    items = tuple(project.get("open") or ())
    if items:
        counts = project.get("counts") if isinstance(project.get("counts"), Mapping) else {}
        grid.add_hit(y, x, right, ("fold", fold_key, name))
        grid.put(x + 3, y, label, "label", limit=right)
        cursor = x + column
        more = "+" if project.get("limited") else ""
        parts = [(ui_text("fleet.memory.count_doing", count=counts.get("doing", 0)), "g_spin_dim"),
                 (ui_text("fleet.memory.count_waiting", count=counts.get("waiting", 0)), "lvl_y"),
                 (ui_text("fleet.memory.count_open", count=counts.get("open", 0)) + more, "dim")]
        if cell_width(" · ".join(text for text, _key in parts)) >= right - cursor:
            parts = [(ui_text("fleet.memory.short_doing", count=counts.get("doing", 0)), "g_spin_dim"),
                     (ui_text("fleet.memory.short_waiting", count=counts.get("waiting", 0)), "lvl_y"),
                     (ui_text("fleet.memory.short_open", count=counts.get("open", 0)) + more, "dim")]
        for part_index, (text, key) in enumerate(parts):
            if part_index:
                cursor = grid.put(cursor, y, " · ", "dim", limit=right)
            zero = text.rsplit(" ", 1)[-1] == "0"           # a zero count is not highlighted
            cursor = grid.put(cursor, y, text, "dim" if zero else key, limit=right)
        toggle = ui_text("fleet.memory.collapse" if opened == name else "fleet.memory.expand")
        if cursor + 2 + cell_width(toggle) < right - 1:
            grid.put(right - 1 - cell_width(toggle), y, toggle, "soft", limit=right)
        y += 1
        for row in (items if opened == name else items[:preview_rows]):
            stage, when, text = (tuple(row) + (None,) * 3)[:3]
            word = OPEN_STAGE_WORDS.get(stage)
            word = (ui_text(word[0]), word[1]) if word else (str(stage), "dim")
            y = _memory_line(grid, x, y, right, None, when, text, mark=word, column=column)
    return y


def draw_memory(grid: CellGrid, vm, state=None, x=34, y=3):
    """Per project: newest decisions, open work and recent changes, from stored headlines."""
    use = max(0, grid.cols - x - 2)
    right = x + use + 1
    data, order, journal = memory_projects(vm)
    projects = data.get("projects") if isinstance(data.get("projects"), Mapping) else {}
    db_state = str(data.get("state") or "unknown")
    if data and db_state != "success":
        detail = ui_text("fleet.memory.db_last_good" if data.get("stale") else "fleet.memory.db_unknown")
        grid.put(x + 1, y, ui_text("fleet.memory.db_state", state=db_state) + detail, "dim", limit=right)
        y += 1
    if not order:
        grid.put(x + 1, y, ui_text("fleet.memory.no_projects"), "dim", limit=right)
        return
    opened = state.opened_for("memory:open", "") if state is not None else ""
    labels = {key: ui_text(key)
              for key in ("fleet.memory.decisions", "fleet.memory.open_work", "fleet.memory.recent")}
    column = _memory_column(labels.values())
    for index, name in enumerate(order):
        if index:
            y = _group_sep(grid, x, use, y)
        project = projects.get(name) if isinstance(projects.get(name), Mapping) else {}
        recent = journal.get(name, ())
        last = project.get("last") or (recent[0][0] if recent and recent[0] else None)
        tail = ui_text("fleet.memory.last_change", when=last) if last else ""
        grid.put(x + 1, y, clip(name + "/" if name else "·", max(1, use - cell_width(tail) - 3)),
                 "title_dim", limit=right)
        if tail:
            grid.put(right - 1 - cell_width(tail), y, tail, "dim", limit=right)
        y += 1
        for row_index, row in enumerate(tuple(project.get("decisions") or ())):
            when, text = (tuple(row) + (None,) * 2)[:2]
            y = _memory_line(grid, x, y, right, labels["fleet.memory.decisions"] if row_index == 0 else None,
                             when, text, column=column)
        y = draw_open_work(grid, x, y, right, project, name, labels["fleet.memory.open_work"],
                           column=column, opened=opened)
        for row_index, row in enumerate(recent[:2]):
            when, action, _kind, text = (tuple(row) + (None,) * 4)[:4]
            saved = action == "saved"
            y = _memory_line(grid, x, y, right, labels["fleet.memory.recent"] if row_index == 0 else None,
                             when, text, column=column,
                             key="soft" if saved else "now_sub",
                             mark=("+", "done") if saved else ("·", "dim"))
        work = [row for row in (vm.system or {}).get("journal_records", ()) if row.get("project") == name]
        if work and y < grid.rows - 6:
            record = max(work, key=lambda row: str(row.get("at") or ""))
            targets = [ui_text("entrance.open_record") + ": " + str(record.get("reference") or record.get("record_target"))]
            targets += [ui_text("entrance.open_result") + ": " + str(row["path"])
                        for row in (record.get("result_targets") or [])[:1]]
            y = _memory_line(grid, x, y, right, None, None, " · ".join(targets), column=column)


def draw_system(grid: CellGrid, vm, state=None, x=34, y=3):
    """Display system observations without probing collectors or reclassifying state."""
    data = vm.system if isinstance(vm.system, Mapping) else {}
    install = data.get("install") or {}
    usage = data.get("usage_api") or {}
    dispatch = data.get("dispatch") or {}
    delivery = data.get("peer_delivery") or {}
    collectors = tuple(data.get("collectors") or ())
    runtimes = tuple(data.get("runtimes") or ())
    def section(title, meta=""):
        cursor = grid.put(x, y, "── ", "line")
        cursor = grid.put(cursor, y, title, "head")
        if meta:
            cursor = grid.put(cursor, y, "  " + meta, "dim")
        grid.put(cursor, y, " ", "branch_s")
        for col in range(cursor + 1, grid.cols - 1):
            grid.put(col, y, "─", "line")
    def fields(values, lab_width=17):
        nonlocal y
        for label, parts in values:
            cursor = grid.put(x + 3, y, label, "label")
            cursor = x + 3 + lab_width
            for text, key in parts:               # a field line runs to the pane edge and is cut there
                cursor = grid.put(cursor, y, text, key, limit=grid.cols - 1)
            y += 1
    section("hearting", "install")
    y += 1
    runtimes_text = []
    for index, runtime in enumerate(runtimes):
        if index:
            runtimes_text.append((" · ", "dim"))
        runtimes_text.append((str(runtime), "hb_" + str(runtime)))
    api_parts = [(str(usage.get("state", "·")), "done" if usage.get("state") == "on" else "dim")]
    if usage.get("freshness"):
        api_parts.append((" · claude/codex/opencode fresh " + str(usage["freshness"]), "dim"))
    collector_parts = []
    for index, collector in enumerate(collectors):
        if index:
            collector_parts.append(("  ", "dim"))
        collector_parts.extend(((str(collector) + " ", "dim"), ("✓", "done")))
    if collectors:
        collector_parts.extend((("  disabled ", "dim"), ("none", "dim")))
    marks = "/".join(name for name in ("managed", "pinned") if install.get(name))
    flags = " · ".join(part for part in (marks, "distribution" if install.get("distribution") else "") if part)
    version_parts = [(str(install.get("version", "·")), "ver"),
                     (" · " + flags if flags else "", "dim")]
    fields((("version:", version_parts), ("runtimes:", runtimes_text or [("·", "dim")]),
            ("usage api:", api_parts), ("collectors:", collector_parts)), 13)
    y += 1
    section("dispatch")
    y += 1
    gov = [(str(dispatch.get("governor_active", "·")), "soft"),
           (f" active / cap {dispatch.get('governor_cap', '·')} · dispatch {data.get('dispatch_count', '·')}", "dim")]
    jobs = [("↳ " + str(dispatch.get("jobs", "·")) + " jobs", "soft"),
            (" (", "dim"), ("⠏ " + str(dispatch.get("resource_running", "·")), "spin"),
            (") · resource " + str(dispatch.get("resource_running", "·")) + " running · " +
             str(dispatch.get("resource_exited", "·")) + " exited", "dim")]
    fields((("governor:", gov), ("jobs:", jobs)), 13)
    y += 1
    section("peer delivery")
    y += 1
    pending = [(str(delivery.get("pending", "·")), "lvl_y"), (" awaiting recipient", "dim")]
    no_recipient = delivery.get("no_recipient")
    if isinstance(no_recipient, int) and not isinstance(no_recipient, bool):
        pending.append((ui_text("fleet.delivery.no_recipient", count=no_recipient), "dim"))
    obligations = []
    for entry in data.get("obligations") or ():
        if obligations:
            obligations.append(("   ", "soft"))
        obligations += [("[", "dim"), (str(entry.get("tag") or "·"), "soft"), ("]", "dim"),
                        ("  ", "soft"), (str(entry.get("text") or "·"), "soft")]
        if entry.get("age"):
            obligations.append(("  · " + str(entry["age"]), "dim"))
    records = [(str(delivery.get("records", "·")), "soft"), (" in the last window", "dim")]
    fields((("pending:", pending),) + ((("obligations:", obligations),) if obligations else ()) +
           (("records:", records),), 13)
    y += 1
    extra = data.get("unregistered_gpu_processes")
    if extra:
        info = data.get("unregistered_gpu_summary") or {}
        owner = info.get("owner")
        section("unregistered GPU processes",
                f"{info.get('count', len(extra))}" + (f" · owner [{owner}]" if owner else "") + " · see RESOURCES")
        y += 1
        for item in extra[:2]:
            host, index, command, memory, age = (tuple(item) + (None,) * 5)[:5]
            cursor = grid.put(x + 3, y, f"{host}:{index}", "soft")
            cursor = grid.put(cursor, y, "  " + ("·" if command is None else str(command)), "soft")
            cursor = grid.put(cursor, y, "  " + ("·" if memory is None else f"{memory:g} GB"), "dim")
            grid.put(cursor, y, "  " + ("·" if age is None else str(age)), "dim")
            y += 1
