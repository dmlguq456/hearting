#!/usr/bin/env python3
"""Fresh-process Fleet peer preview, PTY latency, scale and retention probe.

Synthetic modes isolate renderer and navigation cost. The
``explorer-collecting`` mode opts into real read-only Fleet collection and records
the first complete Explorer frame plus key-to-paint latency while collection
runs; its private terminal capture belongs only in the supplied evidence root.
"""
from __future__ import annotations

import argparse
import curses
import fcntl
import hashlib
import json
import os
import pty
import resource
import selectors
import signal
import statistics
import struct
import subprocess
import sys
import termios
import threading
import time
import tracemalloc
from pathlib import Path


SCRIPT = Path(__file__).resolve()


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     indent=2) + "\n", encoding="utf-8")


def _load_fixture(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("fixture_schema") != "peer-navigation-v1" or not raw.get("rows"):
        raise ValueError("unsupported or empty peer navigation fixture")
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode()).hexdigest()
    return raw, digest


def _isolate_runtime(evidence_dir, fixture, registry_count):
    """Keep every runtime lookup in this probe inside its evidence sandbox."""
    root = Path(evidence_dir).resolve()
    for name, child in (("HOME", "sandbox-home"),
                        ("XDG_CONFIG_HOME", "sandbox-config"),
                        ("XDG_DATA_HOME", "sandbox-data"),
                        ("XDG_STATE_HOME", "sandbox-state")):
        path = root / child
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    os.environ["HERDR_SOCKET_PATH"] = str(root / "sandbox-herdr.sock")
    os.environ["TERM"] = "xterm-256color"
    os.environ["LC_ALL"] = "C.UTF-8"

    assignments = {}
    for row in fixture["rows"]:
        for direction, harness_key, sid_key in (
                ("sent", "to_harness", "to_session_id"),
                ("recv", "from_harness", "from_session_id")):
            peer = row.get(direction) or {}
            harness, sid = peer.get(harness_key), peer.get(sid_key)
            if harness in {"claude", "codex", "opencode"} and isinstance(sid, str) and sid:
                assignments.setdefault((harness, sid), None)
    used_tags = set()
    next_tag = 0
    rows = []
    for index, (harness, sid) in enumerate(sorted(assignments)):
        if harness == "claude":
            tag = "aa"  # Claude may retain colliding derived tags.
        else:
            while "%02x" % next_tag in used_tags:
                next_tag += 1
            tag = "%02x" % next_tag
            used_tags.add(tag)
            next_tag += 1
        rows.append({"harness": harness, "session_id": sid, "tag": tag,
                     "first_seen": index + 1, "started_at": index + 1})
    for index in range(max(0, registry_count - len(rows))):
        rows.append({"harness": "claude", "session_id": "benchmark-registry-%05d" % index,
                     "tag": "aa", "first_seen": len(rows) + 1,
                     "started_at": len(rows) + 1})
    target = Path(os.environ["XDG_STATE_HOME"]) / "agent-fleet" / "tag-assignments.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _json(target, {"version": 1, "assignments": rows})


def _rss_pss():
    result = {}
    try:
        for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"Rss", "Pss", "Pss_Anon", "Private_Dirty", "Anonymous"}:
                result[key + "_KiB"] = int(value.strip().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return result


def _proc_start(pid):
    try:
        tail = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
        return tail[19]
    except (OSError, IndexError):
        return None


def _sessions(fixture, count, *, protected=False):
    from fleet.model import Session
    values = []
    rows = fixture["rows"]
    for i in range(count):
        row = rows[i] if i < len(rows) else rows[i % len(rows)]
        sid = row["session_id"] if i < len(rows) else "%s-%04d" % (row["session_id"], i)
        pid = 9500000 + i
        proc_start = "fixture-start-%04d" % i
        if protected and i == 0:
            pid, proc_start = os.getpid(), _proc_start(os.getpid())
        elif protected and i == 1:
            parent = os.getppid()
            pid, proc_start = parent, _proc_start(parent)
        session = Session(
            harness=row["harness"], pid=pid, proc_start=proc_start,
            cwd=fixture["cwd"], slug="peer-fixture-%04d" % i,
            title=row.get("title", "peer fixture %04d" % i), session_id=sid,
            liveness="working", elapsed_min=i + 1, session_tag=row.get("session_tag"),
            process_role="conversation")
        session.peer_last_sent = dict(row["sent"]) if row.get("sent") else None
        session.peer_last_recv = dict(row["recv"]) if row.get("recv") else None
        values.append(session)
    return values


def _forbidden(name):
    def fail(*_args, **_kwargs):
        raise AssertionError("benchmark reached forbidden operation: " + name)
    return fail


def _setup(source_tree, fixture_path):
    tools_dir = str(Path(source_tree) / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    fixture, fixture_hash = _load_fixture(fixture_path)
    from fleet import control, gitinfo, render, session_tags
    render._API_DISABLED = True
    render._REPO_FOLD = {}
    render._ROUTE_FOLD = {}
    render._OFFSET = 0
    render._SELECT_MODE = False
    render._CURSOR_ID = None
    render._collect_memory = lambda: None
    render._collect_governor = lambda: None
    control.kill_target = _forbidden("control.kill_target")
    control.close_registry_row = _forbidden("control.close_registry_row")
    # A missing fixture assignment must fail inside this isolated probe rather
    # than seed tags by scanning a real Herdr or harness registry.
    session_tags.assign = _forbidden("session_tags.assign")
    # The detail pump still follows its normal copy/merge publication path, but
    # it cannot start a second inventory, provider, GPU, or repository observer.
    gitinfo.enrich_entities = lambda *_args, **_kwargs: None
    return fixture, fixture_hash, render


def _display_snapshot(rows, memory=None):
    """Use the candidate's producer projection when present; sealed baseline has none."""
    try:
        from fleet.display_snapshot import build, extend
    except ImportError:
        return None
    return extend(build(rows), memory=memory)


def _display_kwargs(display):
    return {"display_snapshot": display} if display is not None else {}


def _live_snapshot(rows):
    from fleet.refresh import LiveSnapshot
    value = LiveSnapshot(sessions=rows)
    display = _display_snapshot(rows)
    if display is not None and hasattr(value, "display_snapshot"):
        value.display_snapshot = display
    return value


def _preview(source_tree, fixture_path):
    fixture, fixture_hash, render = _setup(source_tree, fixture_path)
    cases = []
    for harness in ("claude", "codex", "opencode"):
        rows = _sessions(fixture, len(fixture["rows"]))
        for session in rows:
            session.harness = harness
            session.session_id = "%s-%s" % (harness, session.session_id)
        for width in (168, 139, 138, 137, 120, 100, 71, 70, 69, 60, 46):
            render.reset_selection()
            render._REPO_FOLD = {}
            render._OFFSET = 0
            layout = render._layout_mode(width)
            display = _display_snapshot(rows)
            lines = render._build_lines(rows, [], "both", width < render._NARROW_CUTOFF,
                                        0, layout=layout, term_width=width,
                                        usage_snapshots={}, **_display_kwargs(display))
            text_lines = ["".join(text for text, _key in line or []) for line in lines]
            cases.append({"harness": harness, "width": width, "layout": layout,
                          "logical_rows": len(lines), "lines": text_lines,
                          "has_forward": any("→" in line for line in text_lines),
                          "has_reverse": any("←" in line for line in text_lines),
                          "has_both": any("↔" in line for line in text_lines),
                          "overflow": any(sum(render._dw(text) for text, _ in line or []) > width
                                          for line in lines)})
    return {"fixture_sha256": fixture_hash, "source_tree": str(source_tree),
            "cases": cases}


def _counted_build(render, sessions, width, iterations):
    from fleet import gitinfo, model
    counters = {"project_of": 0, "render_project_of": 0,
                "display_project_of": 0, "resolve_gitdir": 0, "peer_tag": 0}
    orig_project, orig_model_project = render.project_of, model.project_of
    orig_gitdir = gitinfo.resolve_gitdir
    orig_tag = render._resolve_session_tag

    def project(*args, **kwargs):
        counters["project_of"] += 1
        counters["render_project_of"] += 1
        return orig_project(*args, **kwargs)

    def model_project(*args, **kwargs):
        counters["project_of"] += 1
        counters["display_project_of"] += 1
        return orig_model_project(*args, **kwargs)

    def gitdir(*args, **kwargs):
        counters["resolve_gitdir"] += 1
        return orig_gitdir(*args, **kwargs)

    def tag(*args, **kwargs):
        counters["peer_tag"] += 1
        return orig_tag(*args, **kwargs)

    render.project_of, model.project_of = project, model_project
    gitinfo.resolve_gitdir, render._resolve_session_tag = gitdir, tag
    memory = {"periodic_curate": {"total": 1, "current_cwd": "/tmp/navigation-curator"}}
    display = _display_snapshot(sessions, memory=memory)
    display_project_calls = counters["display_project_of"]
    before_builds = dict(counters)
    samples, cpu = [], []
    before_mem = _rss_pss()
    tracemalloc.start()
    try:
        for _ in range(iterations):
            wall0, cpu0 = time.perf_counter_ns(), time.process_time_ns()
            render._build_lines(sessions, [], "both", width < render._NARROW_CUTOFF,
                                0, layout=render._layout_mode(width), memory=memory,
                                term_width=width, usage_snapshots={},
                                **_display_kwargs(display))
            samples.append(time.perf_counter_ns() - wall0)
            cpu.append(time.process_time_ns() - cpu0)
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        render.project_of, model.project_of = orig_project, orig_model_project
        gitinfo.resolve_gitdir, render._resolve_session_tag = orig_gitdir, orig_tag
    return {"sessions": len(sessions), "width": width, "iterations": iterations,
            "wall_median_ms": statistics.median(samples) / 1e6,
            "wall_max_ms": max(samples) / 1e6,
            "cpu_median_ms": statistics.median(cpu) / 1e6,
            "calls_per_build": {key: (value - before_builds[key]) / iterations
                                for key, value in counters.items()},
            "display_snapshot_project_calls": display_project_calls,
            "rss_pss_before": before_mem, "rss_pss_after": _rss_pss(),
            "tracemalloc_current_bytes": current, "tracemalloc_peak_bytes": peak,
            "ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def _scale_one(source_tree, fixture_path, count, width, registry_count):
    fixture, fixture_hash, render = _setup(source_tree, fixture_path)
    rows = _sessions(fixture, count)
    result = _counted_build(render, rows, width, 20)
    result.update(fixture_sha256=fixture_hash, source_tree=str(source_tree),
                  registry_assignments=registry_count,
                  python=sys.version.split()[0], pid=os.getpid())
    return result


def _retention(source_tree, fixture_path):
    fixture, fixture_hash, render = _setup(source_tree, fixture_path)
    import copy
    import gc
    from fleet.refresh import LiveSnapshot
    rows = _sessions(fixture, 100)
    base = _live_snapshot(rows)
    tracemalloc.start()
    before = _rss_pss()
    cpu0 = time.process_time_ns()
    fixed = None
    churn = None
    fixed_retained = None
    fixed_rss = None
    for i in range(60):
        source = base if i < 30 else _live_snapshot(_sessions(fixture, 100))
        copied = copy.deepcopy(source)
        render._build_lines(copied.sessions, [], "both", False, 0, term_width=168,
                            usage_snapshots={}, **_display_kwargs(
                                getattr(copied, "display_snapshot", None)))
        if i < 30:
            fixed = copied
        else:
            churn = copied
        if i in (0, 9, 29, 30, 39, 59):
            gc.collect()
        if i == 29:
            fixed_retained = tracemalloc.get_traced_memory()[0]
            fixed_rss = _rss_pss()
    current, peak = tracemalloc.get_traced_memory()
    gc.collect()
    after_gc = tracemalloc.get_traced_memory()[0]
    after = _rss_pss()
    cpu_ms = (time.process_time_ns() - cpu0) / 1e6
    tracemalloc.stop()
    return {"fixture_sha256": fixture_hash, "source_tree": str(source_tree),
            "profile": "30 fixed + 30 roster-churn snapshot copies, 100 sessions, 1000 synthetic registry rows",
            "iterations": 60, "registry_assignments": 1000, "rss_pss_before": before,
            "fixed_profile_retained_bytes": fixed_retained, "fixed_profile_rss_pss": fixed_rss,
            "rss_pss_after_gc": after, "tracemalloc_current_before_gc_bytes": current,
            "tracemalloc_retained_after_gc_bytes": after_gc,
            "tracemalloc_peak_bytes": peak, "process_cpu_ms": cpu_ms,
            "ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "live_snapshot_refs": int(fixed is not None) + int(churn is not None)}


def _emit(fd, record):
    data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _pty_child(source_tree, fixture_path, telemetry_fd, width, mode):
    live_collecting = mode == "explorer-collecting"
    live_collect_all = None
    if live_collecting:
        tools_dir = str(Path(source_tree) / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        fixture, fixture_hash = _load_fixture(fixture_path)
        from fleet import render
        from fleet.collectors import collect_all as live_collect_all
        render._API_DISABLED = True
        render._EXPLORER_ENABLED = True
        if not hasattr(render, "_EXPLORER_RUNTIME"):
            class RuntimeProbe:
                def collection_options(self, first_complete):
                    return {"fast_first": bool(first_complete)}

                def prepare(self, _snapshot, _detail, *, vm, generation, now):
                    return vm, {"ready": True, "generation": generation}

                def loading_frame(self, _cols, _rows):
                    return None

                def apply_effects(self, _effects, _vm):
                    return None

            render._explorer_runtime = lambda: RuntimeProbe()
        rows = []
    else:
        fixture, fixture_hash, render = _setup(source_tree, fixture_path)
        rows = _sessions(fixture, 100, protected=True)
    current = {"event_id": 0, "record": None, "last_build_ns": 0,
               "merge": None, "input_cpu_ns": 0}
    counts = {"build": 0, "project_of": 0, "tag_resolver": 0}
    orig_build, orig_draw = render._build_lines, render._draw
    orig_read = render._CursesInput.read
    orig_flush = curses.doupdate
    orig_select, orig_base = render._handle_select_key, render._handle_base_key
    from fleet import details, gitinfo, model
    orig_merge = details.merge
    orig_project, orig_model_project = render.project_of, model.project_of
    orig_tag = render._resolve_session_tag
    first_frame = {"start_ns": time.perf_counter_ns(), "reported": False}

    def project(*args, **kwargs):
        counts["project_of"] += 1
        return orig_project(*args, **kwargs)

    def model_project(*args, **kwargs):
        counts["project_of"] += 1
        return orig_model_project(*args, **kwargs)

    def tag(*args, **kwargs):
        counts["tag_resolver"] += 1
        return orig_tag(*args, **kwargs)

    def build(*args, **kwargs):
        start = time.perf_counter_ns()
        result = orig_build(*args, **kwargs)
        current["last_build_ns"] = time.perf_counter_ns() - start
        counts["build"] += 1
        return result

    def merge(*args, **kwargs):
        start = time.perf_counter_ns()
        result = orig_merge(*args, **kwargs)
        current["merge"] = {"start_ns": start, "end_ns": time.perf_counter_ns()}
        return result

    def read(reader, stdscr, **kwargs):
        wall0, thread0 = time.perf_counter_ns(), time.thread_time_ns()
        ch, mouse = orig_read(reader, stdscr, **kwargs)
        if ch != curses.ERR and ch != getattr(curses, "KEY_RESIZE", -9999):
            current["event_id"] += 1
            current["record"] = {"event_id": current["event_id"], "key": ch,
                                 "read_start_ns": wall0,
                                 "decoded_ns": time.perf_counter_ns(),
                                 "decode_thread_ns": time.thread_time_ns() - thread0,
                                 "before_cursor": render._CURSOR_ID,
                                 "before_offset": render._OFFSET,
                                 "merge": current.get("merge"),
                                 "process_cpu_ns": time.process_time_ns(),
                                 "thread_cpu_ns": time.thread_time_ns()}
            _emit(telemetry_fd, {"kind": "decoded", **current["record"]})
        return ch, mouse

    def select_key(ch):
        record = current.get("record")
        start = time.perf_counter_ns()
        result = orig_select(ch)
        if record is not None:
            record["selection_ns"] = time.perf_counter_ns() - start
            record["after_cursor"] = render._CURSOR_ID
        return result

    def base_key(ch, body_h):
        record = current.get("record")
        start = time.perf_counter_ns()
        result = orig_base(ch, body_h)
        if record is not None:
            record["selection_ns"] = time.perf_counter_ns() - start
            record["after_offset"] = render._OFFSET
        return result

    def draw(stdscr, *args, **kwargs):
        result = orig_draw(stdscr, *args, **kwargs)
        if (live_collecting and not first_frame["reported"]
                and getattr(render, "_EXPLORER_GENERATION", 0) > 0
                and getattr(render, "_EXPLORER_FRAME", None) is not None):
            first_frame["reported"] = True
            _emit(telemetry_fd, {
                "kind": "first-complete-frame",
                "elapsed_ms": (time.perf_counter_ns() - first_frame["start_ns"]) / 1e6,
                "generation": render._EXPLORER_GENERATION,
                "readiness": getattr(render, "_EXPLORER_READINESS", None),
            })
        record = current.get("record")
        if record is not None and record.get("reported") is not True:
            record["draw_end_ns"] = time.perf_counter_ns()
            record["draw_thread_ns"] = time.thread_time_ns()
            record["draw_process_cpu_ns"] = time.process_time_ns()
            record["draw_ns"] = record["draw_end_ns"] - record["decoded_ns"]
            record["build_ns"] = current["last_build_ns"]
            record["flush_ns"] = current.get("last_flush_ns", 0)
            record["selection_mode"] = bool(render._SELECT_MODE)
            record["cursor"] = render._CURSOR_ID
            record["offset"] = render._OFFSET
            record["size"] = list(stdscr.getmaxyx())
            record["counts"] = dict(counts)
            record["reported"] = True
            _emit(telemetry_fd, {"kind": "painted", **record})
        return result

    def flush():
        start = time.perf_counter_ns()
        try:
            return orig_flush()
        finally:
            current["last_flush_ns"] = time.perf_counter_ns() - start

    curses.doupdate = flush
    render.project_of, model.project_of = project, model_project
    render._resolve_session_tag = tag
    render._build_lines, render._draw = build, draw
    render._CursesInput.read = read
    render._handle_select_key, render._handle_base_key = select_key, base_key
    details.merge = merge
    render._REPO_FOLD = {}
    render._ROUTE_FOLD = {}
    render._PROCESS_VIEW = False
    render._SELECT_MODE = False
    render._CURSOR_ID = None
    render._OFFSET = 0

    collector_state = {"snapshot": None, "ready": threading.Event(),
                       "stop": threading.Event(), "thread": None}

    def live_collection_loop():
        index = 0
        while not collector_state["stop"].is_set():
            started = time.perf_counter_ns()
            _emit(telemetry_fd, {"kind": "collection-start", "index": index,
                                 "time_ns": started})
            try:
                value = live_collect_all(harness_filter=None, fast_first=(index == 0))
                collector_state["snapshot"] = value
                collector_state["ready"].set()
                _emit(telemetry_fd, {"kind": "collection-done", "index": index,
                                     "duration_ms": (time.perf_counter_ns() - started) / 1e6,
                                     "sessions": len(getattr(value, "sessions", ())),
                                     "jobs": len(getattr(value, "jobs", ()))})
            except BaseException as exc:
                _emit(telemetry_fd, {"kind": "collection-error", "index": index,
                                     "error": repr(exc)})
                if index == 0:
                    collector_state["ready"].set()
                return
            index += 1
            if collector_state["stop"].wait(0.01):
                return

    if live_collecting:
        collector_state["thread"] = threading.Thread(
            target=live_collection_loop, name="benchmark-live-collection", daemon=True)
        collector_state["thread"].start()

    def collect_all(**_kwargs):
        start = time.perf_counter_ns()
        if live_collecting:
            if not collector_state["ready"].wait(240):
                raise TimeoutError("live collector did not publish its first snapshot")
            value = collector_state["snapshot"]
            if value is None:
                raise RuntimeError("live collector failed before first snapshot")
        else:
            value = _live_snapshot(rows)
        current["last_collection_ns"] = time.perf_counter_ns() - start
        _emit(telemetry_fd, {"kind": "snapshot-produced", "duration_ns": current["last_collection_ns"],
                             "project_lookups": counts["project_of"], "pid": os.getpid()})
        return value

    collect_all.__name__ = "synthetic_collect_all"
    if live_collecting:
        def close():
            collector_state["stop"].set()
            thread = collector_state["thread"]
            if thread is not None:
                thread.join(2.0)
        collect_all.close = close
    _emit(telemetry_fd, {"kind": "child-start", "pid": os.getpid(),
                         "proc_start": _proc_start(os.getpid()),
                         "source_tree": str(source_tree), "fixture_sha256": fixture_hash,
                         "width": width, "mode": mode,
                         "safety_fixture_pids": [row.pid for row in rows[:2]],
                         "counts": counts})
    curses.wrapper(render._loop, collect_all, None, "both", 20.0)


def _pty_run(source_tree, fixture_path, evidence_dir, width, mode, replicate, keys=100,
             event_timeout=15):
    run_dir = Path(evidence_dir) / ("pty-%s-%03d-r%d" % (mode, width, replicate))
    run_dir.mkdir(parents=True, exist_ok=True)
    read_fd, write_fd = os.pipe()
    pid, master = pty.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            _pty_child(source_tree, fixture_path, write_fd, width, mode)
            os._exit(0)
        except BaseException as exc:
            try:
                _emit(write_fd, {"kind": "child-error", "error": repr(exc)})
            finally:
                os._exit(2)
    os.close(write_fd)
    start = _proc_start(pid)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 36, width, 0, 0))
    os.set_blocking(master, False)
    os.set_blocking(read_fd, False)
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ, "pty")
    selector.register(read_fd, selectors.EVENT_READ, "telemetry")
    transcript = bytearray()
    pipe_buffer = bytearray()
    telemetry, painted, sent = [], {}, {}
    counter = 0
    child_error = None

    def drain(timeout=0.1):
        nonlocal child_error
        for key, _ in selector.select(timeout):
            try:
                data = os.read(key.fd, 262144)
            except BlockingIOError:
                data = b""
            except OSError:
                data = b""
            if key.data == "pty" and data:
                transcript.extend(data)
            elif key.data == "telemetry" and data:
                pipe_buffer.extend(data)
                while b"\n" in pipe_buffer:
                    line, _, rest = pipe_buffer.partition(b"\n")
                    pipe_buffer[:] = rest
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    telemetry.append(record)
                    if record.get("kind") == "painted":
                        painted[record["event_id"]] = record
                    elif record.get("kind") == "child-error":
                        child_error = record.get("error")

    def wait_event(event_id, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            drain(0.02)
            if event_id in painted:
                return painted[event_id]
            if child_error or os.waitpid(pid, os.WNOHANG)[0]:
                raise RuntimeError("PTY child exited before event %d: %r" % (event_id, child_error))
        raise TimeoutError("timed out waiting for PTY paint event %d" % event_id)

    def wait_first_complete(timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            drain(0.02)
            frame = next((item for item in telemetry
                          if item.get("kind") == "first-complete-frame"), None)
            if frame is not None:
                return frame
            if child_error or os.waitpid(pid, os.WNOHANG)[0]:
                raise RuntimeError("PTY child exited before first complete frame: %r" % child_error)
        raise TimeoutError("timed out waiting for first complete Explorer frame")

    def send(data, warmup=False):
        nonlocal counter
        counter += 1
        write_ns = time.perf_counter_ns()
        os.write(master, data)
        sent[counter] = {"write_ns": write_ns, "warmup": warmup, "bytes": data.hex()}
        return wait_event(counter, timeout=event_timeout)

    try:
        deadline = time.monotonic() + 5
        while not any(r.get("kind") == "child-start" for r in telemetry):
            drain(0.02)
            if time.monotonic() > deadline:
                raise TimeoutError("PTY child did not start")
        if mode == "selection":
            send(b"s", warmup=True)
        # Exercise the arrow decoder in selection mode too, so its latency
        # includes the same escape-sequence path as the user's navigation.
        key_bytes = b"\x1b[B"
        other = b"\x1b[A"
        for i in range(10):
            send(key_bytes if i % 2 == 0 else other, warmup=True)
        if mode == "explorer-collecting":
            wait_first_complete()
        measured = []
        for i in range(keys):
            data = key_bytes if i % 2 == 0 else other
            paint = send(data)
            event_id = counter
            row = dict(paint)
            row["parent_write_ns"] = sent[event_id]["write_ns"]
            row["parent_painted_receipt_ns"] = time.perf_counter_ns()
            row["end_to_paint_ms"] = (row["parent_painted_receipt_ns"] - row["parent_write_ns"]) / 1e6
            measured.append(row)
        os.write(master, b"q")
        end = time.monotonic() + 2
        while time.monotonic() < end:
            drain(0.02)
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
        else:
            # Cleanup is scoped to this exact fixture child and its validated start time.
            if _proc_start(pid) == start:
                os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        if child_error:
            raise RuntimeError(child_error)
        (run_dir / "pty.raw").write_bytes(transcript)
        _json(run_dir / "telemetry.json", telemetry)
        values = [row["end_to_paint_ms"] for row in measured]
        collection_started = {item["index"]: item["time_ns"] for item in telemetry
                              if item.get("kind") == "collection-start"}
        collection_done = {item["index"]: item["duration_ms"] for item in telemetry
                           if item.get("kind") == "collection-done"}
        last_paint_ns = max((row.get("draw_end_ns", 0) for row in measured), default=0)
        active_samples = sum(1 for row in measured if any(
            started <= row.get("decoded_ns", 0)
            and (index not in collection_done
                 or row.get("decoded_ns", 0) < started + collection_done[index] * 1e6)
            for index, started in collection_started.items()))
        return {"source_tree": str(source_tree), "mode": mode, "width": width,
                "replicate": replicate, "keys": len(measured),
                "median_ms": statistics.median(values),
                "max_ms": max(values),
                "mean_child_decode_thread_ms": statistics.mean(
                    row.get("decode_thread_ns", 0) for row in measured) / 1e6,
                "mean_child_selection_ms": statistics.mean(
                    row.get("selection_ns", 0) for row in measured) / 1e6,
                "mean_child_build_ms": statistics.mean(
                    row.get("build_ns", 0) for row in measured) / 1e6,
                "mean_child_flush_ms": statistics.mean(
                    row.get("flush_ns", 0) for row in measured) / 1e6,
                "mean_child_draw_after_decode_ms": statistics.mean(
                    row.get("draw_ns", 0) for row in measured) / 1e6,
                "first_complete_frame_ms": next((item["elapsed_ms"] for item in telemetry
                                                  if item.get("kind") == "first-complete-frame"), None),
                "key_samples_during_collection": active_samples,
                "collection_runs_started": len(collection_started),
                "collection_runs_completed": len(collection_done),
                "last_paint_ns": last_paint_ns,
                "collection_durations_ms": [item["duration_ms"] for item in telemetry
                                             if item.get("kind") == "collection-done"],
                "parent_raw_pty_bytes": len(transcript),
                "child_start_ticks": start,
                "sample_events": measured,
                "raw_paths": [str(run_dir / "pty.raw"), str(run_dir / "telemetry.json")]}
    except BaseException as exc:
        # A timed-out sample still owns this exact forked PTY child. Save its partial
        # transcript and receipts, then stop only the PID whose start tick we captured.
        (run_dir / "pty.partial.raw").write_bytes(transcript)
        _json(run_dir / "telemetry.partial.json", telemetry)
        if _proc_start(pid) == start:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        _json(run_dir / "failure.json", {"error": repr(exc), "pid": pid,
                                         "proc_start": start,
                                         "last_painted_event": max(painted, default=None),
                                         "fixture_sha256": _load_fixture(fixture_path)[1]})
        raise
    finally:
        selector.close()
        os.close(master)
        os.close(read_fd)


def _run(args):
    mode = args.mode
    output = Path(args.evidence_dir)
    output.mkdir(parents=True, exist_ok=True)
    result = {"phase": args.phase, "source_tree": str(args.source_tree),
              "fixture": str(args.fixture), "fixture_sha256": _load_fixture(args.fixture)[1],
              "python": sys.version.split()[0], "platform": sys.platform,
              "term": os.environ.get("TERM"), "locale": os.environ.get("LC_ALL")}
    if mode in ("preview", "all"):
        value = _preview(args.source_tree, args.fixture)
        _json(output / "preview.json", value)
        result["preview"] = str(output / "preview.json")
    if mode in ("scale", "all"):
        values = []
        for count in (3, 30, 100, 300):
            for width in (168, 100, 60):
                for replicate in range(1, 4):
                    cmd = [sys.executable, str(SCRIPT), "--source-tree", str(args.source_tree),
                           "--fixture", str(args.fixture), "--phase", args.phase,
                           "--mode", "_scale-one", "--scale-count", str(count),
                           "--scale-width", str(width), "--registry-count", "100",
                           "--evidence-dir", str(output / "workers")]
                    completed = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                    values.append(json.loads(completed.stdout))
        session_values = values
        registry_values = []
        for registry_count in (10, 100, 1000, 5000):
            for replicate in range(1, 4):
                cmd = [sys.executable, str(SCRIPT), "--source-tree", str(args.source_tree),
                       "--fixture", str(args.fixture), "--phase", args.phase,
                       "--mode", "_scale-one", "--scale-count", "100",
                       "--scale-width", "100", "--registry-count", str(registry_count),
                       "--evidence-dir", str(output / "workers")]
                completed = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                registry_values.append(json.loads(completed.stdout))
        _json(output / "scale.json", {"session_scale": session_values,
                                      "registry_scale": registry_values})
        result["scale"] = str(output / "scale.json")
    if mode in ("retention", "all"):
        cmd = [sys.executable, str(SCRIPT), "--source-tree", str(args.source_tree),
               "--fixture", str(args.fixture), "--phase", args.phase, "--mode", "_retention",
               "--registry-count", "1000", "--evidence-dir", str(output / "worker")]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        value = json.loads(completed.stdout)
        _json(output / "retention.json", value)
        result["retention"] = str(output / "retention.json")
    if mode in ("latency", "all"):
        values = []
        for test_mode, width in (("selection", 168), ("scroll", 60)):
            for replicate in range(1, 4):
                values.append(_pty_run(args.source_tree, args.fixture, output,
                                       width, test_mode, replicate, keys=100,
                                       event_timeout=args.event_timeout))
        _json(output / "latency.json", values)
        result["latency"] = str(output / "latency.json")
    if mode == "explorer-collecting":
        values = [_pty_run(args.source_tree, args.fixture, output, 168,
                           "explorer-collecting", replicate, keys=100,
                           event_timeout=args.event_timeout)
                  for replicate in range(1, 4)]
        _json(output / "explorer-collecting.json", values)
        result["explorer_collecting"] = str(output / "explorer-collecting.json")
        result["first_complete_frame_ms"] = [item["first_complete_frame_ms"] for item in values]
        result["latency_p50_ms"] = [statistics.median(
            sample["end_to_paint_ms"] for sample in item["sample_events"]) for item in values]
        result["latency_p95_ms"] = [sorted(
            sample["end_to_paint_ms"] for sample in item["sample_events"])[94]
            for item in values]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--phase", choices=("baseline", "after"), required=True)
    parser.add_argument("--evidence-dir", default=".")
    parser.add_argument("--mode", choices=("latency", "explorer-collecting", "retention", "preview", "scale", "all",
                                             "_scale-one", "_retention"), required=True)
    parser.add_argument("--scale-count", type=int, default=3)
    parser.add_argument("--scale-width", type=int, default=168)
    parser.add_argument("--registry-count", type=int, default=100)
    parser.add_argument("--event-timeout", type=float, default=15)
    args = parser.parse_args()
    fixture, _fixture_hash = _load_fixture(args.fixture)
    if args.mode != "explorer-collecting":
        _isolate_runtime(args.evidence_dir, fixture, args.registry_count)
    if args.mode == "_scale-one":
        value = _scale_one(args.source_tree, args.fixture, args.scale_count,
                           args.scale_width, args.registry_count)
        print(json.dumps(value, ensure_ascii=False))
    elif args.mode == "_retention":
        print(json.dumps(_retention(args.source_tree, args.fixture), ensure_ascii=False))
    else:
        print(json.dumps(_run(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
