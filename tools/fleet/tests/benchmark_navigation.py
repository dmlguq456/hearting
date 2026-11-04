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
import datetime
import errno
import fcntl
import hashlib
import json
import os
import platform
import pty
import re
import resource
import selectors
import signal
import shutil
import socket
import stat
import statistics
import struct
import subprocess
import sys
import termios
import threading
import time
import tracemalloc
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[3] / "utilities"))  # split_module_fixture
from split_module_fixture import set_split  # noqa: E402


SCRIPT = Path(__file__).resolve()


def _frame_metrics(readiness, generation, replay_active, frame_mode,
                   frame_present=True):
    readiness = readiness if isinstance(readiness, dict) else {}
    sources = readiness.get("sources") if isinstance(readiness.get("sources"), dict) else {}
    basic = sources.get("basic") if isinstance(sources.get("basic"), dict) else {}
    required = (sources.get("required-details")
                if isinstance(sources.get("required-details"), dict) else {})
    complete_states = {"complete", "completed", "success", "ok", "observed"}
    basic_complete = str(basic.get("state") or "").lower() in complete_states
    detail_state = str(required.get("state") or "").lower()
    details_complete = (detail_state in complete_states or
                        (detail_state == "continued" and
                         required.get("continuation_validated") is True))
    loading = str(frame_mode or "").lower() == "loading"
    ready = (readiness.get("ready") is True and readiness.get("fully_fresh") is True
             and basic_complete and details_complete)
    fresh = bool(frame_present and not replay_active and not loading
                 and generation > 0 and ready)
    all_new = fresh and readiness.get("all_new") is True
    return {"frame_present": bool(frame_present), "frame_mode": frame_mode,
            "loading": loading, "replay": bool(replay_active),
            "basic_complete": basic_complete,
            "required_details_complete": details_complete,
            "ready": ready, "fresh_complete": fresh,
            "fresh_all_new": all_new,
            "generation": generation,
            "pending": list(readiness.get("pending") or ())}


def _production_schedule(keys, duration):
    if keys < 0 or duration < 0:
        raise ValueError("keys and duration must be non-negative")
    if keys == 0:
        return []
    return [duration * index / keys for index in range(keys)]


def _production_order(replicates=3):
    arms = ("base", "candidate", "legacy")
    orders = (arms, ("candidate", "base", "legacy"), arms)
    return [list(orders[index % len(orders)]) for index in range(replicates)]


def _production_launcher(role, tree):
    fleet_dir = Path(tree) / "tools" / "fleet"
    if role == "legacy":
        monitor = fleet_dir / "fleet-monitor.sh"
        if monitor.is_file():
            return monitor
        return fleet_dir / "fleet.sh"
    return fleet_dir / "fleet2.sh"


def _production_argv(role, trees):
    tree = Path(trees[role]).resolve()
    return [str(_production_launcher(role, tree))]


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * percentile))]


def _write_pty_input(master, payload, byte_offset, stream_hash, *,
                     write=os.write, monotonic_ns=time.monotonic_ns):
    """Write once and retain the accepted PTY byte range and timing."""
    started_ns = monotonic_ns()
    error = None
    try:
        written = write(master, payload)
    except OSError as exc:
        written = 0
        error = type(exc).__name__
    finished_ns = monotonic_ns()
    if not isinstance(written, int) or written < 0 or written > len(payload):
        written = 0
        error = "InvalidWriteCount"
    accepted = payload[:written]
    stream_hash.update(accepted)
    return ({"write_start_ns": started_ns, "parent_write_ns": started_ns,
             "write_finish_ns": finished_ns, "bytes_requested": len(payload),
             "bytes_written": written, "byte_start": byte_offset,
             "byte_end": byte_offset + written,
             "input_bytes_sha256": hashlib.sha256(payload).hexdigest(),
             "stream_byte_count": byte_offset + written,
             "stream_prefix_sha256": stream_hash.hexdigest(),
             "write_error": error}, byte_offset + written)


def _match_production_keys(sent, events):
    """Use exact PTY ranges, child stream prefixes, decoded keys, and flush IDs."""
    decoded = [item for item in events if item.get("kind") == "input-decoded"]
    raw_bytes = [item for item in events if item.get("kind") == "input-raw-byte"]
    unproven_reads = [item for item in events
                      if item.get("kind") == "input-raw-unproven"]
    flushes = [item for item in events
               if item.get("kind") == "flush" and item.get("input_id") is not None]
    matched = []
    for row in sent:
        if row.get("write_error"):
            status = "write-error"
        elif row.get("bytes_written") != row.get("bytes_requested"):
            status = "partial-write"
        else:
            decode = paint = None
            start, end = row.get("byte_start"), row.get("byte_end")
            same_span = [item for item in decoded
                         if item.get("source_byte_start") == start
                         and item.get("source_byte_end") == end]
            overlapping = [item for item in decoded
                           if isinstance(item.get("source_byte_start"), int)
                           and isinstance(item.get("source_byte_end"), int)
                           and item["source_byte_start"] < end
                           and item["source_byte_end"] > start]
            covering_samples = [candidate for candidate in sent
                                if isinstance(candidate.get("byte_start"), int)
                                and isinstance(candidate.get("byte_end"), int)
                                and candidate["byte_start"] < end
                                and candidate["byte_end"] > start]
            decode = same_span[0] if len(same_span) == 1 else None
            raw_counts = {}
            for item in raw_bytes:
                offset = item.get("byte_offset")
                if isinstance(offset, int):
                    raw_counts[offset] = raw_counts.get(offset, 0) + 1
            raw_coverage = (isinstance(start, int) and isinstance(end, int)
                            and all(raw_counts.get(offset) == 1
                                    for offset in range(start, end)))
            prefix_proven = bool(
                decode and decode.get("source_byte_proven") is True
                and decode.get("child_byte_count") == end
                and decode.get("child_stream_sha256") == row.get("stream_prefix_sha256")
                and raw_coverage
            )
            expected_kind = row.get("key")
            if len(same_span) > 1:
                status, decode = "ambiguous-decode", None
            elif len(covering_samples) > 1 or any(
                    item.get("source_byte_start", start) <= start
                    and item.get("source_byte_end", end) >= end
                    and (item.get("source_byte_start") != start
                         or item.get("source_byte_end") != end)
                    for item in overlapping):
                status, decode = "coalesced-decode", None
            elif decode and decode.get("input_kind") not in ("up", "down"):
                status = "unknown-decode"
            elif decode and not prefix_proven:
                status = "ambiguous-read"
            elif decode and (not isinstance(decode.get("time_ns"), int)
                             or decode["time_ns"] < row.get("parent_write_ns", 0)):
                status = "ambiguous-read"
            elif decode and decode.get("input_kind") != expected_kind:
                status = "ambiguous-decode"
            elif not decode:
                if any(item.get("input_kind") is None for item in overlapping):
                    status = "unknown-decode"
                elif overlapping:
                    status = "ambiguous-read"
                elif any(item.get("child_byte_count", 0) <= end
                         for item in unproven_reads):
                    status = "ambiguous-read"
                else:
                    status = "missing-decode"
            else:
                linked = [item for item in flushes
                          if item.get("input_id") == decode.get("input_id")]
                if len(linked) > 1:
                    status, decode, paint = "ambiguous-flush", None, None
                elif not linked:
                    status, paint = "decoded-no-flush", None
                else:
                    paint = linked[0]
                    if (not isinstance(paint.get("time_ns"), int)
                            or paint["time_ns"] < decode.get("time_ns", 0)
                            or paint["time_ns"] < row.get("parent_write_ns", 0)):
                        status, decode, paint = "ambiguous-flush", None, None
                    else:
                        status = "painted"
            latency = ((paint["time_ns"] - row["parent_write_ns"]) / 1e6
                       if status == "painted" else None)
        if row.get("write_error") or row.get("bytes_written") != row.get("bytes_requested"):
            decode = paint = None
            latency = None
        matched.append({**row,
                        "input_id": decode.get("input_id") if decode else None,
                        "decoded_ns": decode.get("time_ns") if decode else None,
                        "painted_ns": paint.get("time_ns") if paint else None,
                        "end_to_flush_ms": latency, "status": status})
    return matched


def _pump_phase_samples(events):
    previous, completed, samples = {}, {}, []
    for item in events:
        name = item.get("name")
        if item.get("kind") == "pump-start":
            prior = previous.get(name)
            samples.append({"name": name, "phase": "start",
                            "time_ns": item.get("time_ns"),
                            "interval_seconds": item.get("interval_seconds"),
                            "observed_cadence_seconds": (
                                (item["time_ns"] - prior) / 1e9
                                if prior is not None and item.get("time_ns") is not None else None)})
            previous[name] = item.get("time_ns")
        elif item.get("kind") in ("pump-done", "pump-error"):
            index = completed.get(name, 0)
            completed[name] = index + 1
            if "install" in str(name):
                phase = "install"
            elif "selection" in str(name):
                phase = "selection"
            elif "gpu" in str(name):
                phase = "compute-host"
            elif "detail" in str(name):
                phase = "required-detail" if index == 0 else "optional-detail"
            else:
                phase = "first-basic" if index == 0 else "steady-basic"
            samples.append({"name": name, "phase": phase,
                            "duration_ns": item.get("duration_ns"),
                            "cpu_ns": item.get("cpu_ns"),
                            "status": "error" if item.get("kind") == "pump-error" else "complete",
                            "sessions": item.get("sessions"), "jobs": item.get("jobs"),
                            "resources": item.get("resources")})
    return samples


def _startup_profile_summary(events):
    draws = [item for item in events if item.get("kind") == "synchronous-draw"]
    readiness = [item for item in events if item.get("kind") == "readiness-check"]
    installinfo = [item for item in events if item.get("kind") == "installinfo-call"]
    return {
        "enabled": bool(draws or readiness or installinfo),
        "installinfo_calls": installinfo,
        "synchronous_draw_samples": draws,
        "readiness_check_samples": readiness,
    }


def _importtime_profile(transcript):
    pattern = re.compile(rb"^import time:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(.*)$")
    entries = []
    for line in transcript.splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        entries.append({"self_us": int(match.group(1)),
                        "cumulative_us": int(match.group(2)),
                        "module": match.group(3).decode("utf-8", "replace")})
    entries.sort(key=lambda item: item["cumulative_us"], reverse=True)
    return {"entry_count": len(entries), "largest_cumulative": entries[:30]}


def _observer_source():
    return r'''import atexit
import curses
import hashlib
import json
import os
import signal
from importlib.machinery import PathFinder
from pathlib import Path
import sys
import time

FD = int(os.environ.get("FLEET_BENCHMARK_TELEMETRY_FD", "-1"))
PROFILE_STARTUP = bool(os.environ.get("FLEET_BENCHMARK_PROFILE_STARTUP"))
_emit_ns = 0
_input_id = 0
_pending_input = None
_flush_id = 0

def emit(record):
    global _emit_ns
    if FD < 0:
        return
    started = time.monotonic_ns()
    try:
        data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        view = memoryview(data)
        while view:
            written = os.write(FD, view)
            view = view[written:]
    except (OSError, TypeError, ValueError):
        return
    finally:
        _emit_ns += time.monotonic_ns() - started

def chain_sitecustomize():
    observer_file = Path(__file__).resolve()
    observer_dir = Path(os.environ.get("FLEET_BENCHMARK_OBSERVER_DIR", "")).resolve()
    for entry in sys.path:
        try:
            if Path(entry or os.getcwd()).resolve() == observer_dir:
                continue
        except OSError:
            continue
        spec = PathFinder.find_spec("sitecustomize", [entry])
        if spec is None or not spec.origin or Path(spec.origin).resolve() == observer_file:
            continue
        try:
            source = Path(spec.origin).read_bytes()
            globals()["__file__"] = spec.origin
            exec(compile(source, spec.origin, "exec"), globals(), globals())
        except Exception as error:
            emit({"kind": "observer-unsupported", "component": "sitecustomize-chain",
                  "error_type": type(error).__name__})
        finally:
            globals()["__file__"] = str(observer_file)
        break

chain_sitecustomize()

def _times():
    value = os.times()
    return {"user_seconds": value.user, "system_seconds": value.system,
            "children_user_seconds": value.children_user,
            "children_system_seconds": value.children_system}

def _signal_handler_name(number):
    handler = signal.getsignal(number)
    if handler == signal.SIG_DFL:
        return "default"
    if handler == signal.SIG_IGN:
        return "ignored"
    return "%s.%s" % (getattr(handler, "__module__", "unknown"),
                       getattr(handler, "__qualname__", type(handler).__name__))

def _states(readiness):
    sources = readiness.get("sources", readiness) if isinstance(readiness, dict) else {}
    if not isinstance(sources, dict):
        return {}
    return {str(name): {key: item[key] for key in ("state", "all_new", "attempted",
                                                    "continuation_validated")
                        if isinstance(item, dict) and key in item}
            for name, item in sources.items()}

def _frame_event(render):
    frame = getattr(render, "_EXPLORER_FRAME", None)
    readiness = getattr(render, "_EXPLORER_READINESS", None)
    readiness = readiness if isinstance(readiness, dict) else {}
    sources = _states(readiness)
    basic_state = str((sources.get("basic") or {}).get("state") or "").lower()
    detail_state = str((sources.get("required-details") or {}).get("state") or "").lower()
    complete = {"complete", "completed", "success", "ok", "observed"}
    detail_complete = (detail_state in complete or
                       (detail_state == "continued" and
                        (sources.get("required-details") or {}).get(
                            "continuation_validated") is True))
    frame_mode = (getattr(frame, "mode", None) or getattr(frame, "kind", None)
                  or getattr(frame, "view", None) or getattr(frame, "status", None)
                  or getattr(frame, "title", None)) if frame is not None else None
    loading = str(frame_mode or "").lower() == "loading"
    replay = bool(getattr(render, "_EXPLORER_REPLAY_ACTIVE", False))
    generation = getattr(render, "_EXPLORER_GENERATION", 0)
    ready = (readiness.get("ready") is True and readiness.get("fully_fresh") is True
             and basic_state in complete and detail_complete)
    fresh = bool(frame is not None and not loading and not replay
                 and isinstance(generation, int) and generation > 0 and ready)
    return {"screen_flush": True, "frame_present": frame is not None,
            "frame_mode": frame_mode, "loading": loading, "replay": replay,
            "generation": generation, "ready": ready,
            "basic_complete": basic_state in complete,
            "required_details_complete": detail_complete,
            "fresh_complete": fresh,
            "fresh_all_new": fresh and readiness.get("all_new") is True,
            "pending": list(readiness.get("pending") or ()),
            "source_states": sources}

def _required_detail_diagnostics(snapshot):
    groups = (("session", list(getattr(snapshot, "sessions", ()) or ())),
              ("job", list(getattr(snapshot, "jobs", ()) or ())),
              ("route-entity", list(getattr(snapshot, "route_entities", ()) or ())))
    entities = [entity for _kind, rows in groups for entity in rows]
    result = {"entity_count": len(entities), "route_bound_count": 0,
              "details_pending": 0, "required_detail_error": 0,
              "projection_missing": 0, "projection_ambiguous": 0,
              "projection_route_missing": 0, "route_record_missing": 0,
              "route_record_identity_mismatch": 0, "route_record_hash_mismatch": 0,
              "route_bound_by_kind": {}, "route_bound_issues": []}
    for entity in entities:
        result["details_pending"] += bool(getattr(entity, "_details_pending", False))
        result["required_detail_error"] += bool(getattr(entity, "_required_detail_error", False))
    for kind, rows in groups:
        bound = [entity for entity in rows
                 if getattr(entity, "route_id", None) or getattr(entity, "owner_route_id", None)]
        result["route_bound_count"] += len(bound)
        result["route_bound_by_kind"][kind] = len(bound)
        for entity in bound:
            work = getattr(entity, "work_projection", None)
            problems = {}
            if work is None:
                result["projection_missing"] += 1
                problems["projection_missing"] = True
            else:
                ambiguous = bool(getattr(work, "ambiguity", None))
                result["projection_ambiguous"] += ambiguous
                if ambiguous:
                    problems["projection_ambiguous"] = True
                    code = str(getattr(work, "ambiguity", ""))[:80].lower()
                    problems["ambiguity_code"] = (code if code and all(
                        char.isascii() and (char.isalnum() or char in "-_")
                        for char in code) else "unclassified")
                route_id = getattr(work, "route_id", None)
                if not route_id:
                    result["projection_route_missing"] += 1
                    problems["projection_route_missing"] = True
                route_view = getattr(work, "_route_view", None)
                record = route_view.get("record") if isinstance(route_view, dict) else None
                if not isinstance(record, dict):
                    result["route_record_missing"] += 1
                    problems["route_record_missing"] = True
                else:
                    identity_mismatch = record.get("route_id") != route_id
                    hash_mismatch = record.get("route_hash") != getattr(work, "route_hash", None)
                    result["route_record_identity_mismatch"] += identity_mismatch
                    result["route_record_hash_mismatch"] += hash_mismatch
                    if identity_mismatch:
                        problems["route_record_identity_mismatch"] = True
                    if hash_mismatch:
                        problems["route_record_hash_mismatch"] = True
            if problems and len(result["route_bound_issues"]) < 32:
                exact = (kind, getattr(entity, "harness", None), getattr(entity, "session_id", None),
                         getattr(entity, "attempt_id", None), getattr(entity, "pid", None),
                         getattr(entity, "proc_start", None), getattr(entity, "cwd", None),
                         getattr(entity, "source", None))
                fingerprint = hashlib.sha256(repr(exact).encode("utf-8", "replace")).hexdigest()
                result["route_bound_issues"].append({"kind": kind,
                                                     "identity_sha256": fingerprint,
                                                     **problems})
    return result

class WindowRefreshObserver:
    """Observe the completed refresh used by production renderers."""
    def __init__(self, window, before_refresh, after_refresh, getch_observer=None):
        self._window = window
        self._before_refresh = before_refresh
        self._after_refresh = after_refresh
        self._getch_observer = getch_observer

    def refresh(self, *args, **kwargs):
        self._before_refresh()
        try:
            result = self._window.refresh(*args, **kwargs)
        except BaseException:
            self._after_refresh(False)
            raise
        self._after_refresh(True)
        return result

    def getch(self, *args, **kwargs):
        value = self._window.getch(*args, **kwargs)
        if self._getch_observer is not None:
            self._getch_observer(value)
        return value

    def __getattr__(self, name):
        return getattr(self._window, name)

def install():
    tools = os.environ.get("FLEET_BENCHMARK_SOURCE_TOOLS")
    if tools and tools not in sys.path:
        sys.path.insert(0, tools)
    cache_home = os.environ.get("FLEET_BENCHMARK_CACHE_HOME")
    try:
        from fleet.explorer.runtime import ExplorerRuntime
        original_configure = ExplorerRuntime.configure_persistence
        def configure(runtime, *args, **kwargs):
            if cache_home:
                runtime._state_home = Path(cache_home)
            result = original_configure(runtime, *args, **kwargs)
            emit({"kind": "cache-configured", "cache_path": str(result)})
            return result
        ExplorerRuntime.configure_persistence = configure
        if PROFILE_STARTUP:
            original_ready = ExplorerRuntime.publication_ready
            readiness_samples = {"count": 0}
            def publication_ready(runtime, *args, **kwargs):
                started = time.monotonic_ns()
                result = original_ready(runtime, *args, **kwargs)
                readiness_samples["count"] += 1
                if readiness_samples["count"] <= 20:
                    emit({"kind": "readiness-check", "ready": bool(result),
                          "duration_ns": time.monotonic_ns() - started})
                return result
            ExplorerRuntime.publication_ready = publication_ready
    except Exception as error:
        emit({"kind": "observer-unsupported", "component": "explorer-runtime",
              "error_type": type(error).__name__})
    try:
        from fleet import render
        if PROFILE_STARTUP:
            from fleet import installinfo
            original_installinfo_collect = installinfo.collect
            installinfo_samples = {"count": 0}
            def collect_installinfo(*args, **kwargs):
                started = time.monotonic_ns()
                result = original_installinfo_collect(*args, **kwargs)
                installinfo_samples["count"] += 1
                if installinfo_samples["count"] <= 8:
                    emit({"kind": "installinfo-call",
                          "fast_local": bool(kwargs.get("fast_local")),
                          "refresh_remote": bool(kwargs.get("refresh_remote")),
                          "duration_ns": time.monotonic_ns() - started})
                return result
            installinfo.collect = collect_installinfo
            original_draw = render._draw
            draw_samples = {"count": 0}
            def draw(*args, **kwargs):
                started = time.monotonic_ns()
                result = original_draw(*args, **kwargs)
                draw_samples["count"] += 1
                if draw_samples["count"] <= 12:
                    emit({"kind": "synchronous-draw",
                          "loading": bool(kwargs.get("loading")),
                          "duration_ns": time.monotonic_ns() - started})
                return result
            render._draw = draw
        from fleet.refresh import RefreshPump
        from fleet.collectors import dispatch
        original_init = RefreshPump.__init__
        def init(pump, producer, interval, *args, **kwargs):
            name = kwargs.get("name", args[2] if len(args) > 2 else "fleet-refresh")
            adaptive = kwargs.get("adaptive_cadence", False)
            def observed(*call_args, **call_kwargs):
                started = time.monotonic_ns()
                cpu_started = time.process_time_ns()
                emit({"kind": "pump-start", "name": name, "interval_seconds": interval,
                      "adaptive_cadence": bool(adaptive), "time_ns": started})
                try:
                    result = producer(*call_args, **call_kwargs)
                except BaseException as error:
                    emit({"kind": "pump-error", "name": name,
                          "duration_ns": time.monotonic_ns() - started,
                          "cpu_ns": time.process_time_ns() - cpu_started,
                          "error_type": type(error).__name__})
                    raise
                value = result[0] if isinstance(result, tuple) and result else result
                snapshot = getattr(value, "snapshot", value)
                emit({"kind": "pump-done", "name": name,
                      "duration_ns": time.monotonic_ns() - started,
                      "cpu_ns": time.process_time_ns() - cpu_started,
                      "sessions": len(getattr(snapshot, "sessions", ()) or ()),
                      "jobs": len(getattr(snapshot, "jobs", ()) or ()),
                      "resources": len(getattr(snapshot, "resources", ()) or ()),
                      "observations": _states(getattr(snapshot, "observations", {})),
                      "required_detail_diagnostics": _required_detail_diagnostics(snapshot)})
                return result
            observed.__name__ = getattr(producer, "__name__", "observed_producer")
            return original_init(pump, observed, interval, *args, **kwargs)
        RefreshPump.__init__ = init

        original_registry = dispatch.parsed_registry_rows
        def parsed_registry(path):
            started = time.monotonic_ns()
            rows = original_registry(path)
            try:
                info = Path(path).stat()
                generation = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
            except OSError:
                generation = None
            emit({"kind": "registry", "path": str(path), "generation": generation,
                  "rows": len(rows), "duration_ns": time.monotonic_ns() - started})
            return rows
        dispatch.parsed_registry_rows = parsed_registry

        state = {"input_id": 0, "pending_input": None, "flush_id": 0,
                 "refresh_depth": 0, "child_byte_count": 0,
                 "child_stream": hashlib.sha256(), "active_reader": None,
                 "reader_bytes": {}, "raw_event_id": 0}

        def observe_getch(value):
            state["raw_event_id"] += 1
            if isinstance(value, int) and 0 <= value <= 255:
                offset = state["child_byte_count"]
                state["child_byte_count"] += 1
                state["child_stream"].update(bytes((value,)))
                reader = state["active_reader"]
                if reader is not None:
                    state["reader_bytes"].setdefault(id(reader), []).append(
                        {"offset": offset, "value": value,
                         "event_id": state["raw_event_id"]})
                emit({"kind": "input-raw-byte", "byte_offset": offset,
                      "time_ns": time.monotonic_ns(),
                      "child_byte_count": state["child_byte_count"],
                      "child_stream_sha256": state["child_stream"].hexdigest()})
            elif value not in (curses.ERR, getattr(curses, "KEY_RESIZE", -999999)):
                emit({"kind": "input-raw-unproven", "value_kind": type(value).__name__,
                      "time_ns": time.monotonic_ns(),
                      "child_byte_count": state["child_byte_count"]})

        def source_bytes(reader, code, input_kind, read_started_at):
            queue = state["reader_bytes"].setdefault(id(reader), [])
            if input_kind in ("up", "down"):
                expected = (27, 91, 65 if input_kind == "up" else 66)
                if len(queue) < 3:
                    return None
                tail = queue[-3:]
                if (tuple(item["value"] for item in tail) != expected
                        or any(right["offset"] != left["offset"] + 1
                               for left, right in zip(tail, tail[1:]))
                        or not any(item["event_id"] >= read_started_at for item in tail)):
                    return None
                source = {"source_byte_start": tail[0]["offset"],
                          "source_byte_end": tail[-1]["offset"] + 1,
                          "source_byte_proven": True}
                del queue[:]
                return source
            for index, item in enumerate(queue):
                if item["value"] == code:
                    if index != 0:
                        return None
                    del queue[0]
                    return {"source_byte_start": item["offset"],
                            "source_byte_end": item["offset"] + 1,
                            "source_byte_proven": True}
            return None

        original_read = render._CursesInput.read
        def read(reader, *args, **kwargs):
            read_started_at = state["raw_event_id"] + 1
            state["active_reader"] = reader
            try:
                value = original_read(reader, *args, **kwargs)
            finally:
                state["active_reader"] = None
            ch = value[0] if isinstance(value, tuple) else value
            if ch not in (curses.ERR, getattr(curses, "KEY_RESIZE", -999999)):
                state["input_id"] += 1
                state["pending_input"] = state["input_id"]
                input_kind = {ord("q"): "quit", ord("Q"): "quit",
                              curses.KEY_UP: "up", curses.KEY_DOWN: "down"}.get(ch)
                source = source_bytes(reader, ch, input_kind, read_started_at)
                emit({"kind": "input-decoded", "input_id": state["input_id"],
                      "input_kind": input_kind, "time_ns": time.monotonic_ns(),
                      **(source or {"source_byte_proven": False}),
                      "child_byte_count": state["child_byte_count"],
                      "child_stream_sha256": state["child_stream"].hexdigest()})
            elif (not reader.pending and not reader.ready
                  and not getattr(reader, "discarding_sgr", False)):
                # The decoder may intentionally discard an inert escape sequence.
                # Its bytes remain in the stream hash, but cannot be mistaken for
                # a later decoded key's source token.
                state["reader_bytes"].setdefault(id(reader), []).clear()
            return value
        render._CursesInput.read = read

        def record_flush(path):
            state["flush_id"] += 1
            event = _frame_event(render)
            event.update({"kind": "flush", "flush_id": state["flush_id"],
                          "flush_path": path, "input_id": state["pending_input"],
                          "time_ns": time.monotonic_ns(), "process_times": _times(),
                          "sigterm_handler": _signal_handler_name(signal.SIGTERM),
                          "observer_emit_ns": _emit_ns})
            state["pending_input"] = None
            emit(event)

        original_flush = curses.doupdate
        def flush():
            value = original_flush()
            if state["refresh_depth"] == 0:
                record_flush("curses.doupdate")
            return value
        curses.doupdate = flush

        original_wrapper = curses.wrapper
        def observed_wrapper(func, *args, **kwargs):
            def observed_main(stdscr, *call_args, **call_kwargs):
                def before_refresh():
                    state["refresh_depth"] += 1
                def after_refresh(succeeded):
                    state["refresh_depth"] -= 1
                    if succeeded:
                        record_flush("stdscr.refresh")
                try:
                    result = func(WindowRefreshObserver(
                                      stdscr, before_refresh, after_refresh, observe_getch),
                                  *call_args, **call_kwargs)
                except BaseException as error:
                    emit({"kind": "tui-loop-exception",
                          "error_type": type(error).__name__})
                    raise
                emit({"kind": "tui-loop-return",
                      "return_code": result if isinstance(result, int) else None})
                return result
            try:
                result = original_wrapper(observed_main, *args, **kwargs)
            except BaseException as error:
                emit({"kind": "curses-wrapper-exception",
                      "error_type": type(error).__name__})
                raise
            emit({"kind": "curses-wrapper-return",
                  "return_code": result if isinstance(result, int) else None})
            return result
        curses.wrapper = observed_wrapper
        original_run_live = render.run_live
        def run_live(*args, **kwargs):
            result = original_run_live(*args, **kwargs)
            emit({"kind": "run-live-return",
                  "return_code": result if isinstance(result, int) else None,
                  "time_ns": time.monotonic_ns()})
            return result
        render.run_live = run_live
        emit({"kind": "observer-ready", "pid": os.getpid(), "argv": sys.argv,
              "python": sys.executable, "term": os.environ.get("TERM"),
              "agent_home": os.environ.get("AGENT_HOME"),
              "refresh_interval": os.environ.get("FLEET_REFRESH_INTERVAL"),
              "compute_interval": os.environ.get("FLEET_COMPUTE_HOST_INTERVAL"),
              "time_ns": time.monotonic_ns(), "process_times": _times(),
              "sigterm_handler": _signal_handler_name(signal.SIGTERM)})
        def record_process_exit():
            try:
                os.fstat(FD)
                telemetry_fd_open = True
            except OSError:
                telemetry_fd_open = False
            emit({"kind": "process-exit", "time_ns": time.monotonic_ns(),
                  "telemetry_fd_open": telemetry_fd_open,
                  "sigterm_handler": _signal_handler_name(signal.SIGTERM),
                  "process_times": _times(), "observer_emit_ns": _emit_ns,
                  "input_stream": {"child_byte_count": state["child_byte_count"],
                                   "child_stream_sha256": state["child_stream"].hexdigest()}})
        atexit.register(record_process_exit)
    except Exception as error:
        emit({"kind": "observer-error", "component": "fleet-hooks",
              "error_type": type(error).__name__})

try:
    install()
except Exception as error:
    emit({"kind": "observer-error", "component": "install",
          "error_type": type(error).__name__})
'''


def _hash_file(path):
    target = Path(path)
    try:
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": str(target.resolve()), "sha256": digest.hexdigest(),
                "bytes": target.stat().st_size}
    except OSError as error:
        return {"path": str(target), "error": type(error).__name__}


def _git_provenance(tree):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

    def git(*args):
        try:
            value = subprocess.run(["git", *args], cwd=str(tree), env=env,
                                   text=True, capture_output=True, timeout=5, check=False)
            return value.stdout.strip() if value.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    status = git("status", "--porcelain", "--untracked-files=no") or ""
    main = git("rev-parse", "origin/main")
    head = git("rev-parse", "HEAD")
    ancestor = None
    if main and head:
        try:
            check = subprocess.run(["git", "merge-base", "--is-ancestor", main, head],
                                   cwd=str(tree), env=env, timeout=5, check=False)
            ancestor = check.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    return {"tree": str(Path(tree).resolve()), "head": head,
            "branch": git("branch", "--show-current"), "dirty": bool(status),
            "dirty_status_sha256": hashlib.sha256(status.encode()).hexdigest(),
            "origin_main": main, "origin_main_is_ancestor": ancestor}


def _proc_stats(pid):
    result = {"time_ns": time.monotonic_ns()}
    try:
        fields = Path("/proc/%d/stat" % pid).read_text(encoding="ascii").rsplit(")", 1)[1].split()
        result["user_seconds"] = int(fields[11]) / os.sysconf("SC_CLK_TCK")
        result["system_seconds"] = int(fields[12]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        result["user_seconds"] = result["system_seconds"] = None
    try:
        for line in Path("/proc/%d/status" % pid).read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                result["rss_kib"] = int(line.split()[1])
            elif line.startswith("VmHWM:"):
                result["rss_peak_kib"] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        result.setdefault("rss_kib", None)
        result.setdefault("rss_peak_kib", None)
    return result


def _valid_cache(path):
    if not path:
        return False
    target = Path(path)
    try:
        info = target.lstat()
        if (target.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_size > 1024 * 1024
                or stat.S_IMODE(info.st_mode) != 0o600):
            return False
        value = json.loads(target.read_text(encoding="utf-8"))
        return value.get("fresh_complete") is True and value.get("all_new") is True
    except (OSError, ValueError, TypeError):
        return False


def _cache_envelope(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return {key: value.get(key) for key in (
            "schema_version", "fresh_complete", "all_new", "generation",
            "observed_at", "source_freshness")}
    except (OSError, ValueError, TypeError):
        return None


def _python_cache_usage(path):
    root = Path(path)
    count = size = 0
    if root.is_dir():
        for item in root.rglob("*.pyc"):
            try:
                info = item.stat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                count += 1
                size += info.st_size
    return {"files": count, "bytes": size}


def _production_status(exit_code, *, failure=None, agent_home_matches_tree=None,
                       seed=False, cache_seed_valid=False):
    if failure:
        return "failed", failure
    if agent_home_matches_tree is False:
        return "failed", "AGENT_HOME does not resolve to the launched source tree"
    if seed:
        return ("seeded", None) if cache_seed_valid else ("seed-incomplete", None)
    return ("completed", None) if exit_code == 0 else ("nonzero-exit", None)


def _production_environment(tree, cache_home, observer_dir, telemetry_fd,
                            launch_start_ns, runtime_dir):
    env = os.environ.copy()
    for name in (
        "AGENT_ROUTE_FILE", "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE",
        "AGENT_OWNER_ROUTE_FILE", "AGENT_OWNER_ROUTE_ID", "AGENT_OWNER_ROUTE_HASH",
        "AGENT_DISPATCH_MODEL", "AGENT_DISPATCH_MODEL_ROLE",
        "AGENT_DISPATCH_MODEL_PROFILE", "AGENT_DISPATCH_REASONING",
        "AGENT_DISPATCH_EFFORT",
    ):
        env.pop(name, None)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(observer_dir) + (os.pathsep + existing if existing else "")
    env["PYTHONPYCACHEPREFIX"] = str(Path(runtime_dir) / "pycache")
    env.setdefault("TERM", "xterm-256color")
    env.update({
        "FLEET_BENCHMARK_SOURCE_TOOLS": str(Path(tree).resolve() / "tools"),
        "FLEET_BENCHMARK_CACHE_HOME": str(cache_home),
        "FLEET_BENCHMARK_TELEMETRY_FD": str(telemetry_fd),
        "FLEET_BENCHMARK_START_MONOTONIC_NS": str(launch_start_ns),
        "FLEET_BENCHMARK_OBSERVER_DIR": str(observer_dir),
    })
    return env


# render._loop may join three pumps for 1s each and the install pump for 0.1s;
# fleet's observation close waits up to 0.8s for active futures. Leave 1.1s for
# synchronous transport close and PTY scheduling while keeping normal quit bounded.
_PRODUCTION_NORMAL_QUIT_TIMEOUT_SECONDS = 5.0
_PRODUCTION_SIGTERM_TIMEOUT_SECONDS = 2.0


def _production_closeout(proc, q_write_ns, drain, sample_process, *,
                         monotonic=time.monotonic,
                         monotonic_ns=time.monotonic_ns,
                         killpg=os.killpg):
    """Wait for normal Fleet cleanup, then escalate and reap within fixed bounds."""
    parent_actions = []
    sigterm_ns = None
    sigkill_ns = None
    exit_deadline = monotonic() + _PRODUCTION_NORMAL_QUIT_TIMEOUT_SECONDS
    while monotonic() < exit_deadline and proc.poll() is None:
        drain(0.05)
        sample_process()
    exit_after_q = proc.poll()
    if exit_after_q is None:
        sigterm_ns = monotonic_ns()
        try:
            killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            parent_actions.append({"action": "sigterm", "time_ns": sigterm_ns,
                                   "process_group_present": False})
        else:
            parent_actions.append({"action": "sigterm", "time_ns": sigterm_ns,
                                   "process_group_present": True})
        try:
            proc.wait(timeout=_PRODUCTION_SIGTERM_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            sigkill_ns = monotonic_ns()
            try:
                killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                parent_actions.append({"action": "sigkill", "time_ns": sigkill_ns,
                                       "process_group_present": False})
            else:
                parent_actions.append({"action": "sigkill", "time_ns": sigkill_ns,
                                       "process_group_present": True})
    exit_code = proc.wait()
    parent_actions.append({"action": "process-reaped", "time_ns": monotonic_ns(),
                           "exit_code": exit_code,
                           "elapsed_after_q_ms": ((monotonic_ns() - q_write_ns) / 1e6
                                                   if q_write_ns is not None else None)})
    return {"exit_code": exit_code, "exit_after_q": exit_after_q,
            "sigterm_ns": sigterm_ns, "sigkill_ns": sigkill_ns,
            "parent_actions": parent_actions}


def _production_run(role, tree, cache_home, observer_dir, runtime_dir,
                    run_dir, *, keys=100, duration=60, seed=False,
                    event_timeout=2.0):
    tree = Path(tree).resolve()
    cache_home = Path(cache_home).resolve()
    runtime_dir = Path(runtime_dir).resolve()
    run_dir = Path(run_dir).resolve()
    raw_dir = runtime_dir / "production-raw" / run_dir.name
    raw_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(raw_dir, 0o700)
    run_dir.mkdir(parents=True, exist_ok=True)
    launcher = _production_argv(role, {role: tree})[0]
    if not Path(launcher).is_file():
        return {"role": role, "tree": str(tree), "launcher": launcher,
                "error": "launcher-missing", "keys": [], "exit_code": None}

    observer_dir = Path(observer_dir).resolve()
    observer_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(observer_dir, 0o700)
    _write_private_bytes(observer_dir / "sitecustomize.py", _observer_source().encode("utf-8"))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 168, 0, 0))
    telemetry_read, telemetry_write = os.pipe()
    os.set_blocking(master, False)
    os.set_blocking(telemetry_read, False)
    env = _production_environment(tree, cache_home, observer_dir, telemetry_write,
                                  0, runtime_dir)
    keep_flags = ("TERM", "FLEET_REFRESH_INTERVAL", "FLEET_COMPUTE_HOST_INTERVAL",
                  "FLEET_MOUSE", "FLEET_PYTHON", "PYTHONDONTWRITEBYTECODE",
                  "PYTHONPYCACHEPREFIX", "PYTHONPROFILEIMPORTTIME",
                  "FLEET_BENCHMARK_PROFILE_STARTUP")
    flags = {key: env.get(key) for key in keep_flags}
    python_cache_path = Path(env["PYTHONPYCACHEPREFIX"])
    python_cache_before = _python_cache_usage(python_cache_path)
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    observer_cpu_start = time.process_time()
    parent_wall_start = time.monotonic()
    proc = None
    transcript = bytearray()
    telemetry_buffer = bytearray()
    events = []
    rss_samples = []
    child_start = None
    failure = None
    parent_actions = []
    eof_descriptors = set()
    telemetry_parse_errors = 0
    parent_input_stream = hashlib.sha256()
    parent_input_byte_count = 0
    nav_input_byte_count = 0
    nav_input_stream_sha256 = hashlib.sha256().hexdigest()
    quit_write = None
    selector = selectors.DefaultSelector()
    state_home = env.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    stderr_log_path = Path(state_home) / "agent-fleet" / "fleet-stderr.log"
    try:
        stderr_before = stderr_log_path.stat()
        stderr_before_signature = (stderr_before.st_dev, stderr_before.st_ino,
                                   stderr_before.st_size)
    except OSError:
        stderr_before_signature = None
    try:
        launch_start_ns = time.monotonic_ns()
        launch_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        env["FLEET_BENCHMARK_START_MONOTONIC_NS"] = str(launch_start_ns)
        proc = subprocess.Popen(
            _production_argv(role, {role: tree}), cwd=str(tree), env=env,
            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
            pass_fds=(telemetry_write,), start_new_session=True,
        )
        os.close(slave)
        slave = -1
        os.close(telemetry_write)
        telemetry_write = -1
        child_start = _proc_start(proc.pid)
        selector.register(master, selectors.EVENT_READ, "pty")
        selector.register(telemetry_read, selectors.EVENT_READ, "telemetry")

        def drain(timeout=0.05):
            nonlocal telemetry_parse_errors
            for selected, _mask in selector.select(timeout):
                while True:
                    try:
                        data = os.read(selected.fd, 262144)
                    except BlockingIOError:
                        break
                    except OSError as error:
                        if error.errno not in (errno.EIO, errno.EBADF):
                            telemetry_parse_errors += 1
                        data = b""
                    if not data:
                        eof_descriptors.add(selected.fd)
                        try:
                            selector.unregister(selected.fd)
                        except (KeyError, ValueError):
                            pass
                        break
                    if selected.data == "pty":
                        transcript.extend(data)
                        continue
                    telemetry_buffer.extend(data)
                    while b"\n" in telemetry_buffer:
                        line, _, rest = telemetry_buffer.partition(b"\n")
                        telemetry_buffer[:] = rest
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeError):
                            telemetry_parse_errors += 1
                            continue
                        if isinstance(record, dict):
                            events.append(record)

        def sample_process():
            if proc is not None and proc.poll() is None:
                row = _proc_stats(proc.pid)
                if (not rss_samples or row["time_ns"] - rss_samples[-1]["time_ns"]
                        >= 1_000_000_000):
                    rss_samples.append(row)

        if seed:
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                drain(0.05)
                sample_process()
                cache_path = next((item.get("cache_path") for item in events
                                   if item.get("kind") == "cache-configured"), None)
                if (any(item.get("kind") == "flush" and item.get("fresh_all_new")
                        for item in events) and _valid_cache(cache_path)):
                    break
                if proc.poll() is not None:
                    failure = "candidate-exited-before-all-new-cache"
                    break
            else:
                failure = "candidate-all-new-cache-timeout"
        else:
            schedule = _production_schedule(keys, duration)
            key_raw = []
            for index, offset in enumerate(schedule):
                deadline_ns = launch_start_ns + int(offset * 1e9)
                while time.monotonic_ns() < deadline_ns:
                    remaining = max(0.0, (deadline_ns - time.monotonic_ns()) / 1e9)
                    drain(min(0.1, remaining))
                    sample_process()
                value = b"\x1b[B" if index % 2 == 0 else b"\x1b[A"
                write, parent_input_byte_count = _write_pty_input(
                    master, value, parent_input_byte_count, parent_input_stream)
                key_raw.append({"sample": index, "scheduled_offset_seconds": offset,
                                "key": "down" if index % 2 == 0 else "up",
                                **write})
            nav_input_byte_count = parent_input_byte_count
            nav_input_stream_sha256 = parent_input_stream.hexdigest()
            end_ns = launch_start_ns + int(duration * 1e9)
            while time.monotonic_ns() < end_ns:
                remaining = max(0.0, (end_ns - time.monotonic_ns()) / 1e9)
                drain(min(0.1, remaining))
                sample_process()
            settle_until = time.monotonic() + event_timeout
            while time.monotonic() < settle_until and proc.poll() is None:
                drain(0.05)
                sample_process()

        q_write_ns = None
        q_write_error = None
        sigterm_ns = None
        sigkill_ns = None
        exit_after_q = None
        if proc is not None and proc.poll() is None:
            q_write_ns = time.monotonic_ns()
            quit_write, parent_input_byte_count = _write_pty_input(
                master, b"q", parent_input_byte_count, parent_input_stream)
            q_bytes = quit_write["bytes_written"]
            q_write_error = quit_write["write_error"]
            parent_actions.append({"action": "write-q", **quit_write,
                                   "bytes": q_bytes, "error": q_write_error,
                                   "process_alive": proc.poll() is None})
            closeout = _production_closeout(proc, q_write_ns, drain, sample_process)
            exit_code = closeout["exit_code"]
            exit_after_q = closeout["exit_after_q"]
            sigterm_ns = closeout["sigterm_ns"]
            sigkill_ns = closeout["sigkill_ns"]
            parent_actions.extend(closeout["parent_actions"])
        else:
            exit_code = proc.wait() if proc is not None else None
            exit_after_q = None

        # Drain both descriptors to EOF or a bounded quiet point after wait().
        # A final atexit record or PTY teardown bytes must not be lost to one short read.
        final_drain_start = time.monotonic()
        quiet_since = final_drain_start
        while time.monotonic() - final_drain_start < 1.0:
            old_lengths = (len(transcript), len(telemetry_buffer), len(events))
            drain(0.05)
            new_lengths = (len(transcript), len(telemetry_buffer), len(events))
            if new_lengths != old_lengths:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= 0.15:
                break
            if len(eof_descriptors) == 2:
                break
        final_drain_ms = (time.monotonic() - final_drain_start) * 1000
        telemetry_unterminated_bytes = len(telemetry_buffer)
        if telemetry_buffer:
            try:
                record = json.loads(telemetry_buffer)
            except (ValueError, UnicodeError):
                telemetry_parse_errors += 1
            else:
                if isinstance(record, dict):
                    events.append(record)
            telemetry_buffer.clear()
        if not seed:
            key_samples = _match_production_keys(key_raw, events)
        else:
            key_samples = []
        cache_path = next((item.get("cache_path") for item in events
                           if item.get("kind") == "cache-configured"), None)
        raw_pty = raw_dir / "pty.raw"
        raw_telemetry = raw_dir / "telemetry.json"
        _write_private_bytes(raw_pty, transcript)
        _write_private_json(raw_telemetry, events)
        times = [item for item in events if item.get("kind") == "process-exit"]
        process_exit_times = times[-1].get("process_times") if times else None
        flushes = [item for item in events if item.get("kind") == "flush"]
        decoded_inputs = [item for item in events if item.get("kind") == "input-decoded"]
        raw_input_bytes = [item for item in events
                           if item.get("kind") == "input-raw-byte"]
        child_stream = next((item.get("input_stream") for item in reversed(events)
                             if item.get("kind") == "process-exit"), None)
        first_flush = next((item for item in flushes if item.get("frame_present")), None)
        replay = next((item for item in flushes if item.get("replay")
                       and item.get("frame_present")), None)
        fresh = next((item for item in flushes if item.get("fresh_complete")), None)
        all_new = next((item for item in flushes if item.get("fresh_all_new")), None)
        latencies = [item["end_to_flush_ms"] for item in key_samples
                     if item["end_to_flush_ms"] is not None]
        run = {
            "role": role, "tree": str(tree), "launcher": launcher,
            "launch_utc": launch_utc, "launch_start_ns": launch_start_ns,
            "wall_seconds": time.monotonic() - parent_wall_start,
            "launch_elapsed_to_observer_ready_ms": next((
                (item.get("time_ns") - launch_start_ns) / 1e6 for item in events
                if item.get("kind") == "observer-ready" and isinstance(item.get("time_ns"), int)), None),
            "launch_elapsed_to_first_frame_ms": ((first_flush["time_ns"] - launch_start_ns) / 1e6
                                                   if first_flush else None),
            "launch_elapsed_to_replay_ms": ((replay["time_ns"] - launch_start_ns) / 1e6
                                             if replay else None),
            "launch_elapsed_to_fresh_ms": ((fresh["time_ns"] - launch_start_ns) / 1e6
                                            if fresh else None),
            "launch_elapsed_to_all_new_ms": ((all_new["time_ns"] - launch_start_ns) / 1e6
                                              if all_new else None),
            "fresh_status": ("observed" if fresh else "unsupported-or-unobserved"),
            "replay_status": ("observed" if replay else "unsupported-or-unobserved"),
            "cache_state_path": str(cache_path) if cache_path else None,
            "cache_envelope": _cache_envelope(cache_path) if cache_path else None,
            "cache_is_all_new": _valid_cache(cache_path),
            "screen_flushes": len(flushes), "key_count": len(key_samples),
            "key_painted_count": sum(item["status"] == "painted" for item in key_samples),
            "key_missing_count": sum(item["status"] != "painted" for item in key_samples),
            "navigation_decode_count": sum(item.get("input_kind") in ("up", "down")
                                            for item in decoded_inputs),
            "decoded_input_event_count": len(decoded_inputs),
            "raw_input_byte_count": len(raw_input_bytes),
            "raw_input_byte_offset_duplicates": (
                len(raw_input_bytes) - len({item.get("byte_offset")
                                            for item in raw_input_bytes})),
            "parent_input_stream": {
                "scheduled_navigation_byte_count": nav_input_byte_count,
                "scheduled_navigation_sha256": nav_input_stream_sha256,
                "total_accepted_byte_count": parent_input_byte_count,
                "total_accepted_sha256": parent_input_stream.hexdigest(),
                "quit_write": quit_write,
                "child_stream": child_stream,
                "complete_stream_match": bool(
                    child_stream
                    and child_stream.get("child_byte_count") == parent_input_byte_count
                    and child_stream.get("child_stream_sha256")
                    == parent_input_stream.hexdigest()),
            },
            "key_latency_ms": {"count": len(latencies), "raw": latencies,
                               "p50": statistics.median(latencies) if latencies else None,
                               "p95": _percentile(latencies, 0.95),
                               "p95_estimator": "lower-order statistic: sorted[floor((n-1)*p)]",
                               "max": max(latencies) if latencies else None},
            "key_samples": key_samples, "rss_unit": "KiB", "rss_samples": rss_samples,
            "rss_sampling_interval_seconds": 1,
            "rss_current_kib": rss_samples[-1].get("rss_kib") if rss_samples else None,
            "rss_steady_median_kib": statistics.median(
                [row["rss_kib"] for row in rss_samples[-10:] if row.get("rss_kib") is not None]
            ) if any(row.get("rss_kib") is not None for row in rss_samples[-10:]) else None,
            "rss_peak_kib": max((row.get("rss_peak_kib") or row.get("rss_kib") or 0
                                 for row in rss_samples), default=None),
            "process_times": process_exit_times,
            "process_user_system_seconds": {
                "user": rss_samples[-1].get("user_seconds") if rss_samples else None,
                "system": rss_samples[-1].get("system_seconds") if rss_samples else None},
            "environment_flags": flags,
            "observer_ready": any(item.get("kind") == "observer-ready" for item in events),
            "agent_home": next((item.get("agent_home") for item in events
                                if item.get("kind") == "observer-ready"), None),
            "agent_home_matches_tree": next((item.get("agent_home") == str(tree)
                                              for item in events
                                              if item.get("kind") == "observer-ready"), None),
            "observer_emit_ns": next((item.get("observer_emit_ns") for item in reversed(events)
                                      if item.get("kind") in ("flush", "process-exit")), None),
            "observer_unsupported": [item for item in events
                                     if item.get("kind") == "observer-unsupported"],
            "shutdown": {
                "parent_actions": parent_actions,
                "q_decoded": any(item.get("kind") == "input-decoded"
                                 and item.get("input_kind") == "quit" for item in events),
                "exit_observed_before_sigterm": (exit_after_q is not None),
                "sigterm_sent": sigterm_ns is not None,
                "sigkill_sent": sigkill_ns is not None,
                "exit_code_kind": ("signal" if isinstance(exit_code, int) and exit_code < 0
                                   else "normal" if isinstance(exit_code, int) else "unknown"),
                "process_exit_event_seen": bool(times),
                "sigterm_handler": next((item.get("sigterm_handler") for item in events
                                           if item.get("kind") == "observer-ready"), None),
                "sigterm_handlers_during_run": sorted({
                    item.get("sigterm_handler") for item in events
                    if item.get("sigterm_handler")}),
                "final_drain_ms": final_drain_ms,
                "pty_eof_seen": master in eof_descriptors,
                "telemetry_eof_seen": telemetry_read in eof_descriptors,
                "telemetry_parse_errors": telemetry_parse_errors,
                "telemetry_unterminated_bytes": telemetry_unterminated_bytes,
                "stderr_log_path": str(stderr_log_path),
                "stderr_log_delta_bytes": None,
                "stderr_log_sha256": None,
            },
            "pump_events": [item for item in events
                            if item.get("kind") in ("pump-start", "pump-done", "pump-error")],
            "pump_phase_samples": _pump_phase_samples(events),
            "startup_profile": _startup_profile_summary(events),
            "importtime_profile": _importtime_profile(transcript),
            "session_job_resource_counts": [
                {key: item.get(key) for key in ("kind", "name", "sessions", "jobs", "resources")}
                for item in events if item.get("kind") == "pump-done"],
            "registry_observations": [
                {"path_sha256": hashlib.sha256(str(item.get("path", "")).encode()).hexdigest(),
                 "generation": item.get("generation"), "rows": item.get("rows"),
                 "duration_ns": item.get("duration_ns")}
                for item in events if item.get("kind") == "registry"],
            "exit_code": exit_code, "error": failure,
            "process_start_ticks": child_start,
            "pty_raw": {"path": str(raw_pty), "sha256": hashlib.sha256(transcript).hexdigest(),
                        "bytes": len(transcript)},
            "telemetry_raw": {"path": str(raw_telemetry),
                              "sha256": hashlib.sha256(raw_telemetry.read_bytes()).hexdigest(),
                              "bytes": raw_telemetry.stat().st_size,
                              "event_count": len(events)},
            "observer_cpu_seconds_parent": max(0.0, time.process_time() - observer_cpu_start),
        }
        try:
            stderr_after = stderr_log_path.stat()
            after_signature = (stderr_after.st_dev, stderr_after.st_ino, stderr_after.st_size)
            if stderr_before_signature is None:
                delta_start = 0
            elif after_signature[:2] == stderr_before_signature[:2]:
                delta_start = min(stderr_before_signature[2], after_signature[2])
            else:
                delta_start = 0
            with stderr_log_path.open("rb") as handle:
                handle.seek(delta_start)
                stderr_delta = handle.read()
            run["shutdown"]["stderr_log_delta_bytes"] = len(stderr_delta)
            run["shutdown"]["stderr_log_sha256"] = hashlib.sha256(stderr_delta).hexdigest()
            run["shutdown"]["stderr_log_inode_changed"] = (
                stderr_before_signature is not None
                and after_signature[:2] != stderr_before_signature[:2])
        except OSError as error:
            run["shutdown"]["stderr_log_error"] = type(error).__name__
        run["python_bytecode_cache"] = {
            "prefix": str(python_cache_path),
            "dont_write_bytecode": env.get("PYTHONDONTWRITEBYTECODE"),
            "before": python_cache_before,
            "after": _python_cache_usage(python_cache_path),
        }
        cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        run["child_rusage_cpu_seconds"] = {
            "user": max(0.0, cpu_after.ru_utime - cpu_before.ru_utime),
            "system": max(0.0, cpu_after.ru_stime - cpu_before.ru_stime)}
        run["cache_seed_valid"] = _valid_cache(cache_path) if seed else None
        run["status"], run["error"] = _production_status(
            exit_code, failure=failure,
            agent_home_matches_tree=run.get("agent_home_matches_tree"),
            seed=seed, cache_seed_valid=run["cache_seed_valid"])
        return run
    except BaseException as error:
        failure = type(error).__name__
        return {"role": role, "tree": str(tree), "launcher": launcher,
                "status": "failed", "error": failure, "keys": [],
                "exit_code": proc.poll() if proc else None}
    finally:
        if proc is not None and proc.poll() is None:
            if _proc_start(proc.pid) == child_start:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                if _proc_start(proc.pid) == child_start:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.wait()
        selector.close()
        for fd in (master, slave, telemetry_read, telemetry_write):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _cache_files(cache_home):
    root = Path(cache_home)
    if not root.is_dir():
        return []
    return sorted(path.relative_to(root).as_posix() for path in root.rglob(
        "fleet2-observation-*.json") if path.is_file() and not path.is_symlink())


def _cache_fingerprints(cache_home):
    root = Path(cache_home)
    result = []
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("fleet2-observation-*.json")):
        if path.is_file() and not path.is_symlink():
            fingerprint = _hash_file(path)
            fingerprint["path"] = path.relative_to(root).as_posix()
            result.append(fingerprint)
    return result


def _production_campaign(args):
    candidate = Path(args.source_tree).resolve()
    trees = {"base": Path(args.base_tree).resolve(),
             "candidate": candidate, "legacy": Path(args.legacy_tree).resolve()}
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    evidence_dir = Path(args.evidence_dir).expanduser().resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    campaign_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    campaign_id += "-%d-%d" % (os.getpid(), time.monotonic_ns() % 1_000_000_000)
    campaign_runtime = runtime_dir / ("fleet2-production-" + campaign_id)
    campaign_runtime.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(campaign_runtime, 0o700)
    observer_dir = campaign_runtime / "observer"
    observer_dir.mkdir(mode=0o700)
    os.chmod(observer_dir, 0o700)
    _write_private_bytes(observer_dir / "sitecustomize.py", _observer_source().encode("utf-8"))
    trees_provenance = {}
    for role, tree in trees.items():
        launcher = Path(_production_argv(role, trees)[0])
        trees_provenance[role] = {
            **_git_provenance(tree),
            "launcher": _hash_file(launcher),
            "fleet_python": _hash_file(tree / "tools" / "fleet" / "fleet.py"),
        }
    interpreter = os.environ.get("FLEET_PYTHON") or shutil.which("python3") or sys.executable
    interpreter_path = shutil.which(interpreter) or interpreter
    cache_states = ("absent", "present") if args.cache_state == "both" else (args.cache_state,)
    result = {
        "schema": "fleet-production-navigation-v1",
        "campaign_id": campaign_id,
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "candidate_tree": str(candidate), "trees": trees_provenance,
        "interpreter": _hash_file(interpreter_path),
        "host": socket.gethostname(),
        "euid_sha256": hashlib.sha256(str(os.geteuid()).encode()).hexdigest(),
        "platform": platform.platform(), "cpu_count": os.cpu_count(),
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "runtime_dir": str(campaign_runtime), "evidence_dir": str(evidence_dir),
        "terminal": {"columns": 168, "rows": 45,
                     "term": os.environ.get("TERM") or "xterm-256color"},
        "sample_contract": {"keys": args.keys, "duration_seconds": args.duration,
                            "replicates": args.replicates,
                            "key_schedule_seconds": _production_schedule(args.keys, args.duration),
                            "key_pattern": "down,up repeated",
                            "cache_states": list(cache_states),
                            "page_cache": "not flushed; process-cold and filesystem state recorded separately"},
        "runs": [], "cache_seeds": [], "state_errors": [],
    }

    for cache_state in cache_states:
        cache_root = campaign_runtime / "cache" / cache_state
        if cache_state == "present":
            seed_home = cache_root / "candidate"
            seed_dir = evidence_dir / "seeds" / campaign_id
            seed = _production_run("candidate", candidate, seed_home, observer_dir,
                                   campaign_runtime, seed_dir, keys=0, duration=0,
                                   seed=True, event_timeout=args.event_timeout)
            seed.update({"cache_state": "present-seed", "role": "candidate"})
            _json(seed_dir / "summary.json", seed)
            result["cache_seeds"].append(seed)
            if seed.get("status") != "seeded" or not seed.get("cache_seed_valid"):
                result["state_errors"].append({"cache_state": "present",
                                               "reason": "candidate all-new cache seed did not validate"})
                continue

        for replicate, order in enumerate(_production_order(args.replicates), start=1):
            for role in order:
                if cache_state == "present" and role == "candidate":
                    role_cache = cache_root / role
                else:
                    role_cache = cache_root / ("r%d-%s" % (replicate, role))
                before_files = _cache_files(role_cache)
                before_fingerprints = _cache_fingerprints(role_cache)
                if cache_state == "absent" and before_files:
                    result["state_errors"].append({"cache_state": cache_state,
                                                   "role": role,
                                                   "reason": "isolated absent cache was not empty"})
                    continue
                if cache_state == "present" and role == "candidate" and not before_files:
                    result["state_errors"].append({"cache_state": cache_state,
                                                   "role": role,
                                                   "reason": "candidate cache disappeared before fresh process"})
                    continue
                run_dir = evidence_dir / "runs" / cache_state / ("r%d-%s" % (replicate, role))
                run = _production_run(role, trees[role], role_cache, observer_dir,
                                      campaign_runtime, run_dir, keys=args.keys,
                                      duration=args.duration,
                                      event_timeout=args.event_timeout)
                run.update({"cache_state": cache_state, "replicate": replicate,
                            "cache_files_at_launch": before_fingerprints,
                            "cache_files_after_run": _cache_fingerprints(role_cache),
                            "cache_at_launch": "present" if before_files else "absent",
                            "order": order})
                _json(run_dir / "summary.json", run)
                result["runs"].append(run)

    groups = {}
    for run in result["runs"]:
        key = run["cache_state"] + "/" + run["role"]
        groups.setdefault(key, []).append(run)
    result["groups"] = {}
    for key, runs in groups.items():
        for metric in ("launch_elapsed_to_replay_ms", "launch_elapsed_to_fresh_ms",
                       "launch_elapsed_to_all_new_ms"):
            values = [run[metric] for run in runs if run.get(metric) is not None]
            result["groups"].setdefault(key, {})[metric] = {
                "count": len(values), "raw": values,
                "median": statistics.median(values) if values else None,
                "min": min(values) if values else None, "max": max(values) if values else None}
        latencies = [sample["end_to_flush_ms"] for run in runs
                     for sample in run.get("key_samples", ())
                     if sample.get("end_to_flush_ms") is not None]
        result["groups"][key]["key_latency_ms"] = {
            "count": len(latencies), "raw": latencies,
            "p50": statistics.median(latencies) if latencies else None,
            "p95": _percentile(latencies, 0.95),
            "p95_estimator": "lower-order statistic: sorted[floor((n-1)*p)]",
            "max": max(latencies) if latencies else None}
    failures = [run for run in result["runs"]
                if run.get("status") in ("failed", "nonzero-exit")]
    result["status"] = "failed" if failures or result["state_errors"] else "complete"
    result["failure_count"] = len(failures) + len(result["state_errors"])
    result_path = evidence_dir / "production.json"
    result["result_path"] = str(result_path)
    _json(result_path, result)
    return result


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     indent=2) + "\n", encoding="utf-8")


def _write_private_bytes(path, data):
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _write_private_json(path, value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n"
    _write_private_bytes(path, data.encode("utf-8"))


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
    gitinfo.resolve_gitdir = gitdir
    set_split(render, "_resolve_session_tag", tag)
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
        gitinfo.resolve_gitdir = orig_gitdir
        set_split(render, "_resolve_session_tag", orig_tag)
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
    set_split(render, "_resolve_session_tag", tag)
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
    parser.add_argument("--source-tree", default=".")
    parser.add_argument("--base-tree")
    parser.add_argument("--legacy-tree")
    parser.add_argument("--fixture")
    parser.add_argument("--phase", choices=("baseline", "after"))
    parser.add_argument("--evidence-dir", default=".")
    parser.add_argument("--runtime-dir")
    parser.add_argument("--mode", choices=("production", "latency", "explorer-collecting", "retention", "preview", "scale", "all",
                                             "_scale-one", "_retention"), required=True)
    parser.add_argument("--replicates", type=int, default=3)
    parser.add_argument("--keys", type=int, default=100)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--cache-state", choices=("absent", "present", "both"), default="both")
    parser.add_argument("--scale-count", type=int, default=3)
    parser.add_argument("--scale-width", type=int, default=168)
    parser.add_argument("--registry-count", type=int, default=100)
    parser.add_argument("--event-timeout", type=float, default=15)
    args = parser.parse_args()
    if args.mode == "production":
        if not args.base_tree or not args.legacy_tree or not args.runtime_dir:
            parser.error("production mode requires --base-tree, --legacy-tree, and --runtime-dir")
        if not args.evidence_dir:
            parser.error("production mode requires --evidence-dir")
        if args.replicates != 3:
            parser.error("production mode requires exactly three alternating replicates")
        if args.keys != 100 or args.duration != 60:
            parser.error("production mode requires exactly 100 keys over 60 seconds")
        if args.event_timeout <= 0:
            parser.error("--event-timeout must be positive")
        for label, path in (("source", args.source_tree), ("base", args.base_tree),
                            ("legacy", args.legacy_tree)):
            role = "candidate" if label == "source" else label
            if not _production_launcher(role, Path(path).expanduser()).is_file():
                parser.error("%s tree is missing its production launcher" % label)
        value = _production_campaign(args)
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if value["status"] == "complete" else 1
    if not args.fixture:
        parser.error("--fixture is required outside production mode")
    if not args.phase:
        parser.error("--phase is required outside production mode")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
