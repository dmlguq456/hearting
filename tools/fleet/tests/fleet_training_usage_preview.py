"""Fixture-only Fleet GPU-command and usage display capture.

This harness never invokes collectors, Fleet's live loop, the user's HOME, or the
dispatch registry.  It feeds one committed preview snapshot directly into the
renderer and can optionally capture one curses `_draw` frame through a private
PTY.
"""
from __future__ import annotations

import argparse
import curses
import fcntl
import hashlib
import importlib.util
import json
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "training_usage_preview.json"
sys.path.insert(0, str(ROOT))
from tools.fleet import projection, render  # noqa: E402
from tools.fleet.collectors import dispatch as dispatch_collector  # noqa: E402
from tools.fleet.model import DispatchJob  # noqa: E402


def _segments(process, width):
    rows = render._gpu_process_rows({"processes": [process]}, "    ", width)
    return rows[0] if rows else []


def _ansi(segs, mode):
    if mode == "none":
        return "".join(text for text, _key in segs)
    chunks = []
    for text, key in segs:
        if not text:
            continue
        hue, attr = render._HUE_OF.get(key, ("d", 0))
        codes = []
        if attr & render._A_D:
            codes.append("2")
        if attr & render._A_B:
            codes.append("1")
        if mode == "256":
            palette = {"d": None, "w": "soft", "g": "green", "y": "yellow",
                       "r": "red", "v": "vanilla", "c": "cyan", "m": "magenta",
                       "l": "blue"}
            color = palette.get(hue)
            if color:
                codes.append("38;5;%d" % render._MUTED_256[color])
        elif mode == "basic":
            palette = {"d": None, "w": "37", "g": "32", "y": "33", "r": "31",
                       "v": "33", "c": "36", "m": "35", "l": "34"}
            color = palette.get(hue)
            if color:
                codes.append(color)
        chunks.append(("\x1b[" + ";".join(codes) + "m" if codes else "")
                      + text + ("\x1b[0m" if codes else ""))
    return "".join(chunks)


def _capture(widths, head):
    fixture_bytes = FIXTURE.read_bytes()
    data = json.loads(fixture_bytes)
    captures = {}
    for width in widths:
        captures[str(width)] = []
        for process in data["processes"]:
            segs = _segments(process, width)
            captures[str(width)].append({
                "pid": process["pid"],
                "segments": segs,
                "plain": "".join(text for text, _key in segs),
                "ansi_256": _ansi(segs, "256"),
                "ansi_basic": _ansi(segs, "basic"),
                "no_color": _ansi(segs, "none"),
            })
    now = 1_791_006_000.0
    job = _usage_job(data["codex_usage"])
    projection.attach_projections([], [job], now=now, spec_markers={},
                                   capability_groundings={})
    detail = {}
    for width in widths:
        row = render._dispatch_summary_detail_row(job, depth=1, term_width=width)
        detail[str(width)] = {"segments": row[0] if row else [], "plain": "".join(
            text for text, _key in row[0]) if row else ""}
    return {
        "capture_kind": "before" if head else "after",
        "head": head,
        "fixture": str(FIXTURE),
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "environment": {"TERM": os.environ.get("TERM", ""), "clock_epoch": now,
                        "animation": "not called; single deterministic snapshot"},
        "widths": widths,
        "gpu_processes": captures,
        "codex_usage": {
            "producer_envelope": _producer_envelope(data["codex_usage"]),
            "context_used_pct": getattr(getattr(job, "context", None), "used_pct", None),
            "active_context_tokens": job.active_context_tokens,
            "session_total_tokens": job.session_total_tokens,
            "detail": detail,
        },
    }


def _producer_envelope(usage):
    path = ROOT / "utilities" / "codex-app-server-supervisor.py"
    spec = importlib.util.spec_from_file_location("fleet_preview_codex_producer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sanitized = module.normalize_token_usage(usage["wire_token_usage"])
    return {"type": "dispatch.supervisor.token_usage", "thread_id": usage["thread_id"],
            "turn_id": usage["turn_id"], "token_usage": sanitized}


def _usage_job(usage):
    envelope = _producer_envelope(usage)
    with tempfile.TemporaryDirectory(prefix="fleet-preview-") as tmp:
        root = Path(tmp)
        log_dir = root / "logs"
        log_dir.mkdir()
        attempt = "att-preview-exact"
        path = log_dir / ("preview.%s.codex.jsonl" % attempt)
        path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
        job = DispatchJob(key="autopilot-code", slug="preview", harness="codex",
                          attempt_id=attempt, liveness="working", depth=2,
                          cwd=str(root / "worktree"), artifact_root=str(root))
        job._log_file = str(path)
        dispatch_collector._CODEX_ATTEMPT_CACHE.clear()
        dispatch_collector._enrich_codex_attempt_session(job)
        # Preserve the measured projection beyond the temporary log lifetime.
        job._context_evidence = None if job._context_evidence is None else job._context_evidence
        return job


def _pty_capture(width, term):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
    code = r'''import curses, json, os
from tools.fleet import render
data=json.load(open(os.environ["FLEET_PREVIEW_FIXTURE"], encoding="utf-8"))
width=int(os.environ["COLUMNS"])
rows=[row for p in data["processes"]
      for row in render._gpu_process_rows({"processes": [p]}, "    ", width)]
render._compute_host_rows=lambda *args, **kwargs: rows
render._gpu_session_resources=lambda *args, **kwargs: {}
render._build_lines=lambda *args, **kwargs: render._build_process_lines(
    [], [], {}, 0, None, width, "wide")
def frame(stdscr):
    render._init_colors()
    render._draw(stdscr, [], [], "all", [], resources={}, usage_snapshots={})
curses.wrapper(frame)
'''
    env = dict(os.environ, TERM=term, COLUMNS=str(width), FLEET_PREVIEW_FIXTURE=str(FIXTURE))
    child = subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT),
                             stdin=slave, stdout=slave, stderr=slave, env=env,
                             close_fds=True)
    os.close(slave)
    raw = bytearray()
    while child.poll() is None:
        ready, _, _ = select.select([master], [], [], 1)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            raw.extend(chunk)
    try:
        while True:
            chunk = os.read(master, 65536)
            if not chunk:
                break
            raw.extend(chunk)
    except OSError:
        pass
    os.close(master)
    code = child.wait(timeout=5)
    return code, bytes(raw).decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--head", default="unknown")
    parser.add_argument("--widths", default="168,120,100,60,40,23")
    parser.add_argument("--pty-width", type=int)
    parser.add_argument("--pty-term", default="xterm-256color")
    args = parser.parse_args()
    widths = [int(value) for value in args.widths.split(",")]
    capture = _capture(widths, args.head)
    if args.pty_width:
        code, raw = _pty_capture(args.pty_width, args.pty_term)
        capture["pty"] = {"term": args.pty_term, "width": args.pty_width,
                          "exit_code": code, "capture_type": "raw curses PTY bytes",
                          "ansi_bytes": raw.count("\x1b"),
                          "text": raw}
    encoded = json.dumps(capture, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
