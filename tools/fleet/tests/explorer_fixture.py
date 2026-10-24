"""Small synthetic source helpers shared by Fleet explorer reader tests."""

import contextlib
import json
import sqlite3
from pathlib import Path
from datetime import timedelta, timezone

FIXTURE_DIR = Path(__file__).with_name("fixtures") / "explorer"
KST = timezone(timedelta(hours=9))


def load_source():
    source = json.loads((FIXTURE_DIR / "source.json").read_text(encoding="utf-8"))
    if "view_model" in source:
        view_model = json.loads((FIXTURE_DIR / source["view_model"]).read_text(encoding="utf-8"))
        from tools.fleet.explorer.view_model import decode_fixture_values
        return {**source, "sources": [decode_fixture_values(view_model["values"])]}
    return source


def output_reader(**options):
    from tools.fleet.explorer.output import OutputReader
    options.setdefault("tz", KST)
    return OutputReader(**options)


def supervisor_rows(attempt_id, session_id, cwd):
    return [{"type": "dispatch.supervisor.session", "parent_attempt_id": attempt_id,
             "session_id": session_id, "cwd": cwd},
            {"type": "dispatch.supervisor.turn-started", "parent_attempt_id": attempt_id,
             "turn_ordinal": 1},
            {"type": "dispatch.supervisor.turn-completed", "parent_attempt_id": attempt_id,
             "turn_ordinal": 1}]


def write_opencode_db(path, sid, generation, rows):
    """Write synthetic v1/v2 OpenCode transcript rows for bounded reader tests."""
    with contextlib.closing(sqlite3.connect(path)) as connection:
        if generation == "v1":
            connection.execute("CREATE TABLE IF NOT EXISTS message (id TEXT, session_id TEXT, role TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS part (session_id TEXT, message_id TEXT, data TEXT)")
            for row in rows:
                connection.execute("INSERT INTO message VALUES (?, ?, ?)",
                                   (row["message"], sid, row["role"]))
                connection.execute("INSERT INTO part (rowid, session_id, message_id, data) VALUES (?, ?, ?, ?)",
                                   (row["rowid"], sid, row["message"],
                                    json.dumps(row["data"], ensure_ascii=False)))
        else:
            connection.execute("CREATE TABLE IF NOT EXISTS session_message "
                               "(session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
            for row in rows:
                connection.execute("INSERT INTO session_message VALUES (?, ?, ?, ?)",
                                   (sid, row["type"], row["seq"],
                                    json.dumps(row["data"], ensure_ascii=False)))
        connection.commit()


# -- demo snapshot, state exploration and frame invariants (end-to-end sweep) ------------------

DECOR = frozenset("─━│┃┈┌┐└┘├┤┬┴┼┳┻┫┣╋╌╍═║┗┏┓┛╭╮╯╰ ")
KEYS_LINE = " ↑↓ move · ←→ focus · ⏎ go to pane · Tab next view · / filter · u usage · F feed · q quit"


def demo_snapshot(now=None):
    """The ``--demo`` rows, projected the way a live tick projects them, plus fixed host rows.

    Hermetic: no home, registry, herdr or network read. The compute-host block and peer records are
    attached so every section the screens read has a source.
    """
    import time
    from tools.fleet import demo
    from tools.fleet.projection import attach_projections
    from tools.fleet.refresh import LiveSnapshot
    sessions, jobs = demo.collect()
    sessions, jobs = attach_projections(sessions, jobs, spec_markers={}, capability_groundings={})
    now = time.time() if now is None else now
    by_sid = {s.session_id: s for s in sessions}
    claude, codex = by_sid["demo-claude-1"], by_sid["demo-codex-1"]
    claude.peer_last_sent = {"to_name": "demo-app", "to_session_id": "demo-codex-1",
                             "to_harness": "codex", "kind": "steer", "age_min": 12}
    codex.peer_last_recv = {"from_name": "demo-app-a7", "from_session_id": "demo-claude-1",
                            "from_harness": "claude", "kind": "steer", "age_min": 12}
    import dataclasses
    claude.work_projection = dataclasses.replace(
        claude.work_projection, result={"name": "demo release notes", "result": "success",
                                        "at": now - 41 * 60, "source": "route"})
    hosts = {"configured": True, "observed_at": now, "hosts": [
        {"host": "base", "self": True, "reachable": True, "cpu_utilization_pct": 23.0, "cpu_count": 64,
         "memory_used_mib": 40960, "memory_total_mib": 128000,
         "gpus": [{"index": 0, "name": "RTX 6000 Ada", "utilization_gpu_pct": 91, "memory_used_mib": 27648,
                   "memory_total_mib": 49152,
                   "processes": [{"pid": 4242, "session_owner": {"harness": "claude", "session_id": "demo-claude-1"}}]},
                  {"index": 1, "name": "RTX 6000 Ada", "utilization_gpu_pct": 0, "memory_used_mib": 0,
                   "memory_total_mib": 49152, "processes": []}]},
        {"host": "remote", "self": False, "reachable": True, "cpu_utilization_pct": 4.0, "cpu_count": 32,
         "memory_used_mib": 8192, "memory_total_mib": 64000, "gpus": []},
        {"host": "gone", "self": False, "reachable": False, "gpus": []}]}
    memory = demo.memory_snapshot()
    memory["today"] = {"added": 2, "expired": 1}
    memory["by_repo"] = {"demo-app": [
        {"ts": "2026-10-23T12:00:00+00:00", "action": "add", "type": "decision", "snippet": "keep the row"},
        {"ts": "2026-10-23T11:00:00+00:00", "action": "update", "type": "artifact-pointer", "snippet": None}]}
    memory["recent"] = [memory["by_repo"]["demo-app"][0]]
    return LiveSnapshot(sessions=sessions, jobs=jobs, memory=memory,
                        usage_snapshots={}, hearting={"version": "v3.42.0-2-gdc406c55a-dirty",
                                                      "install_method": "unmanaged"}), hosts


def demo_seat_entries(now, sid, harness="claude", full=True):
    """A seat history as the reader returns it: an earlier session with two cards, the running one last."""
    def card(generation, title, hours):
        entry = {"generation": generation, "status": "available", "at": now - hours * 3600, "title": title,
                 "chars": 120}
        if full:
            entry["lines"] = [title, "", "- next step", "A **bold** line with `code` in it."]
        return entry
    return (
        {"harness": harness, "sid": "older-" + sid[-4:], "started_at": now - 9 * 3600, "source": "startup",
         "relation": "chronological", "transcript": {}, "current": False,
         "cards": [card(1, "first card", 8), card(2, "second card", 7)]},
        {"harness": harness, "sid": sid, "started_at": now - 3600, "source": "clear",
         "relation": "confirmed", "transcript": {}, "current": True, "cards": []})


def demo_models():
    """A MODELS record of the shape the config reader returns (one profile table, one pool, one line)."""
    profiles = ["top", "deep", "balanced-deep", "balanced", "light", "mini"]
    cell = lambda model, effort: [model, effort]
    return {
        "profiles": profiles,
        "profile_map": {"claude": {p: cell("opus", "high") for p in profiles},
                        "codex": {p: cell("gpt-5.5", "medium") for p in profiles},
                        "opencode": {**{p: cell("glm-5.2", "high") for p in profiles}, "top": None}},
        "model_choices": {"claude": ["opus", "sonnet"], "codex": ["gpt-5.5"], "opencode": ["glm-5.2"]},
        "main_only": {"claude": "fable"}, "efforts": ["low", "medium", "high", "xhigh", "max"],
        "pools": {"deep": [[["codex", "claude"]], "relief opencode below 25%"], "top": None},
        "effort_keys": {"low": "dim", "medium": "eff_medium", "high": "soft", "xhigh": "eff_xhigh", "max": "dead"},
        "shown": {"opus": "Opus", "sonnet": "Sonnet", "fable": "Fable"},
        "context": {"claude": 800000, "codex": 800000, "opencode": 800000},
        "context_source": {"claude": "→ CLAUDE_CODE_AUTO_COMPACT_WINDOW", "codex": "→ model_context_window",
                           "opencode": "→ limit.context capped at start"},
        "tidy": None, "token_units": {"400k": 400000, "800k": 800000, "1M": 1000000},
        "allocation": {"fields": [["strategy:", [["balanced", "soft"], ["   window ", "label"], ["30 attempts", "soft"]]]],
                       "note": "these keys have no setter command yet"}}


def demo_detail(snapshot, now=None):
    """What the selection pumps would hand over for the demo rows: seats of every main session, the
    first Claude session's cards, conversation, pane text and a route's owner output, the models."""
    import time
    from tools.fleet.explorer.types import ExplorerDetail, FocusKey, FocusResult
    now = time.time() if now is None else now
    sid = "demo-claude-1"
    seats = {f"{s.harness}:{s.session_id}": demo_seat_entries(now, s.session_id, s.harness, full=False)
             for s in snapshot.sessions if not s.is_child and not s.mem_worker}
    focus = FocusResult(
        FocusKey("claude", sid), 1, 1,
        events=({"time_kst": "2026-10-23T12:00:00+09:00", "kind": "registry", "tag": "a7", "status": "running",
                 "worker_type": "stage", "route_node": "execute", "note": "a note"},),
        source_records={
            "seat": demo_seat_entries(now, sid),
            "dialogue": {"index": 2, "harness": "claude", "sid": sid,
                         "lines": ["user: hello", "assistant: hi there", "tool Bash: ls", "tool output: a b",
                                   "assistant: " + "a long reply that has to wrap across the box " * 4]},
            "pane_read": {"pane": "w1:p1", "at": now, "lines": ["$ ls", "a b", "x" * 300]},
            "raw": {"att": "att-0123456789ab", "route": "rt-271492e352d13180",
                    "lines": ["assistant: working", "tool Bash: ls", "tool output: a b"] * 8}})
    return ExplorerDetail(focus=focus, seats=seats, config=demo_models(), panes={sid: "w1:p1"})


def demo_model(now=None, detail=False):
    import time
    from tools.fleet.explorer.live import build_view_model
    snapshot, hosts = demo_snapshot(now)
    view = type("View", (), {})()
    view.__dict__.update({name: getattr(snapshot, name) for name in snapshot.__dataclass_fields__})
    view.compute_hosts = hosts
    clock = time.time() if now is None else now
    return build_view_model(view, accepted_detail=demo_detail(snapshot, clock) if detail else None, now=clock)


def trace_draw(draw, vm, state, cols, rows):
    """``draw`` a frame while recording every grid write; returns ``(frame, trace)``."""
    from tools.fleet.explorer.grid import CellGrid
    trace = []
    CellGrid.default_trace = trace
    try:
        frame = draw(vm, state, cols, rows, beat=1)
    finally:
        CellGrid.default_trace = None
    return frame, trace


def frame_violations(frame, trace, cols, rows):
    """Everything wrong with one painted frame: overlaps, overflow, covered bottom lines, bad hits."""
    from tools.fleet.explorer.grid import cell_width
    problems = []
    if len(frame.cells) != rows or any(len(row) != cols for row in frame.cells):
        return ["frame is not %dx%d" % (cols, rows)]
    for y, row in enumerate(frame.cells):
        if sum(cell_width(cell[0]) for cell in row) != cols:
            problems.append("row %d is not %d cells wide" % (y, cols))
        for x, cell in enumerate(row):
            width = cell_width(cell[0])
            if width == 2 and (x + 1 >= cols or row[x + 1][0] != ""):
                problems.append("wide glyph %r at (%d,%d) lost its second cell" % (cell[0], x, y))
            if cell[0] == "" and (x == 0 or cell_width(row[x - 1][0]) != 2):
                problems.append("orphan continuation at (%d,%d)" % (x, y))
    owners = {}
    for entry in trace:
        if entry[0] == "reset":
            owners.clear()
        elif entry[0] == "fill":
            _kind, y, lo, hi, glyph = entry
            for x in range(lo, hi):
                if glyph == " ":
                    owners.pop((x, y), None)
                else:
                    owners[(x, y)] = (-1, glyph)
        elif entry[0] == "clip":
            _kind, _serial, y, dropped, past_screen = entry
            if past_screen and dropped.strip():
                problems.append("text runs past the screen edge on row %d: %r" % (y, dropped[:20]))
        else:
            _kind, serial, y, glyph, x, width = entry
            for offset in range(width):
                key = (x + offset, y)
                prior = owners.get(key)
                if (prior is not None and prior[0] != serial and glyph not in DECOR
                        and prior[1] not in DECOR and prior[1] != glyph):
                    problems.append("%r overwrites %r at (%d,%d)" % (glyph, prior[1], x + offset, y))
                owners[key] = (serial, glyph)
    text = frame.text_rows()
    body_end = rows - 4
    if not text[rows - 2].startswith(" ↑↓ move · ←→ focus"):
        problems.append("key line is covered: %r" % text[rows - 2][:60])
    if not text[rows - 1].startswith(" hearting ›"):
        problems.append("status line is covered: %r" % text[rows - 1][:60])
    feed = text[rows - 3]
    if feed.strip() and not feed.startswith(" ≋ "):
        problems.append("feed line is covered: %r" % feed[:60])
    for hit in frame.hits:
        if hit.y == -1:
            continue
        if not (0 <= hit.y < body_end) and not (hit.y == 1 and hit.action == ("usage",)):
            problems.append("hit below the pane rule: %r" % (hit,))
            continue
        if not (0 <= hit.x0 < hit.x1 <= cols):
            problems.append("hit outside the screen: %r" % (hit,))
            continue
        if hit.action[:2] == ("root", "COMPUTE"):
            continue                    # the COMPUTE panel is one click target, blank rows included
        cells = frame.cells[hit.y][hit.x0:hit.x1]
        if not any(cell[0].strip() or cell[2] is not None for cell in cells):
            problems.append("hit over empty cells: %r" % (hit,))
    return problems


def explore_states(vm, draw, reduce, start, cols, rows, depth, seen=None):
    """Yield ``(state, frame, trace)`` for every state reachable from ``start`` by clicking hits
    (and wheeling scroll strips) up to ``depth`` steps; each distinct state is drawn once."""
    seen = set() if seen is None else seen

    def key(state):
        return (state.view, state.scope, state.folds, state.usage_open, state.scroll)

    def walk(state, remaining):
        if key(state) in seen:
            return
        seen.add(key(state))
        frame, trace = trace_draw(draw, vm, state, cols, rows)
        yield state, frame, trace
        if not remaining:
            return
        actions = []
        for hit in frame.hits:
            if hit.y < 0 or hit.action[0] in ("pane", "parent"):
                continue
            event = ({"type": "wheel", "x": hit.x0, "y": hit.y, "direction": direction}
                     if hit.action[0] == "scroll" else {"type": "click", "action": hit.action})
            for direction in ((-1, 1) if hit.action[0] == "scroll" else (0,)):
                event = (dict(event, direction=direction) if hit.action[0] == "scroll" else event)
                if event not in actions:
                    actions.append(event)
        for event in actions:
            after, _effects = reduce(state, event, frame)
            yield from walk(after, remaining - 1)

    yield from walk(start, depth)
