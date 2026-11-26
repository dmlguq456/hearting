#!/usr/bin/env python3
"""T3 — Fleet new launch -> first observed screen, measured on a PTY.

Each repetition starts a brand-new ``fleet.py --explorer --no-tmux`` child on a
120x40 pseudo-terminal, inside an isolated HOME/XDG, and reads the bytes it
paints. A tiny ``sitecustomize`` shim (PYTHONPATH of the child only) wraps the
Explorer paint call and records, at paint time, whether that frame is the
loading frame, a replay of the last saved complete model, or a live frame, and
the readiness it was painted under. The parent pairs each paint record with the
first PTY chunk read at or after it, so the numbers are what a terminal shows.

Times (seconds after the spawn, one CLOCK_MONOTONIC for parent and child):

* ``first_byte_s``       first byte of any output
* ``replay_s``           first replay frame (only when a saved model exists)
* ``first_observed_s``   T3: first live, non-replay, non-loading frame that is either ready
                         (every required source in: the old definition) or observed (basic in)
                         with the late-cell word on screen ("details wait" / "세부 아직");
                         a tree that never records ``observed`` gives the old definition
* ``first_ready_s``      the old ``first_observed_s``: first live, ready, non-replay frame, so a
                         before/after pair can be compared on one definition
* ``details_complete_s`` first such frame whose every source is new (``all_new``)
* ``source_ready_s``     per source, when it first stopped being pending

``late_cells`` names what was still late at the first observed frame: sources
that stopped being pending only afterwards, sources carried over from a previous pass and the words "details wait"/"아직" actually
on screen (recorded only; the display is another change's territory).

Two launch conditions: ``cold-empty`` (no saved model, private empty cache) and
``second-launch`` (the previous launch saved its last-complete model). The
``once-full-collect`` column times ``fleet.py --once --json`` (one full
collection, no TUI) for the Open Question 2 comparison.

``measure()`` follows the shared result contract; with no fixture it cannot run.
Units never run a measurement: ``t3.test.py`` feeds ``analyze`` fake bytes.
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[1]
COLS, ROWS = 120, 40
CONDITIONS = ("cold-empty", "second-launch")
SCREEN_WORDS = ("details wait", "세부 아직", "아직", "loading", "불러오는 중")
DETAILS_WORDS = ("details wait", "세부 아직")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_TOOLS = ("python3", "node", "sh", "bash", "git", "sleep", "ps", "tput", "stty", "env",
          "cat", "uname", "id", "hostname")

_SHIM = r'''
import importlib.abc
import importlib.machinery
import json
import os
import sys
import time

_REC = os.environ.get("E2E_T3_RECORDS")
_MARK = os.environ.get("E2E_T3_MARKER") or ""
_FH = []


def _emit(row):
    try:
        row["t_ns"] = time.monotonic_ns()
        if not _FH:
            _FH.append(open(_REC, "a", buffering=1))
        _FH[0].write(json.dumps(row) + "\n")
    except Exception:
        pass


def _readiness(mod):
    r = getattr(mod, "_EXPLORER_READINESS", None)
    r = r if isinstance(r, dict) else {}
    sources = r.get("sources") if isinstance(r.get("sources"), dict) else {}
    carried = sorted(n for n, s in sources.items() if isinstance(s, dict)
                     and (str(s.get("state", "")).lower() == "continued" or s.get("all_new") is False))
    return {"ready": bool(r.get("ready")), "all_new": bool(r.get("all_new")),
            "observed": r.get("observed"),
            "details_pending": [str(x) for x in (r.get("details_pending") or ())],
            "pending": [str(x) for x in (r.get("pending") or ())], "carried": carried,
            "states": {str(n): str(s.get("state")) for n, s in sources.items() if isinstance(s, dict)}}


def _patch_render(mod):
    compose = mod._explorer_compose
    draw = mod._draw_explorer_frame

    def _compose(*a, **k):
        out = compose(*a, **k)
        _emit(dict(_readiness(mod), k="compose"))
        return out

    def _draw(stdscr, frame, snapshot, *, stale=False):
        _emit(dict(_readiness(mod), k="paint", replay=bool(stale),
                   loading=getattr(frame, "status", "") == "loading"))
        return draw(stdscr, frame, snapshot, stale=stale)

    mod._explorer_compose = _compose
    mod._draw_explorer_frame = _draw


def _carries(pid):
    for name in ("cmdline", "environ"):
        try:
            with open("/proc/%s/%s" % (pid, name), "rb") as fh:
                if _MARK.encode() in fh.read():
                    return True
        except OSError:
            pass
    return False


def _patch_procscan(mod):
    orig = mod._ps_lines

    def _ps_lines():
        rows = orig()
        if not _MARK:
            return rows
        keep = []
        for line in rows:
            head = line.strip().split(None, 1)
            if head and head[0].isdigit() and _carries(head[0]):
                keep.append(line)
        try:
            return type(rows)(keep, complete=getattr(rows, "census_complete", False),
                              reasons=getattr(rows, "census_reasons", ()))
        except Exception:
            return keep

    mod._ps_lines = _ps_lines
    # The fixture's sleepers are not Claude processes, so process discovery cannot find
    # them: serve the fixture's own sessions instead (as the T1/T2 shim does), so the
    # screen carries the operating-size session set and not an empty one.
    listing = os.environ.get("E2E_T3_SESSIONS")
    if listing:
        items = json.load(open(listing))

        def scan(**_kw):
            rows = []
            for item in items:
                session = mod.Session(harness=item["harness"], pid=item["pid"],
                                      cwd=item["cwd"], elapsed_min=30)
                session.proc_start = item.get("pid_start")
                rows.append(session)
            return rows

        mod.scan = scan
        starts = {int(i["pid"]): i.get("pid_start") for i in items}
        orig_start = mod.read_proc_start
        mod.read_proc_start = lambda pid: starts.get(int(pid)) or orig_start(pid)


_PATCHES = {"fleet.render": _patch_render, "tools.fleet.render": _patch_render,
            "fleet.collectors.procscan": _patch_procscan,
            "tools.fleet.collectors.procscan": _patch_procscan}


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        patch = _PATCHES.get(name)
        if patch is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.loader is None:
            return None
        loader = spec.loader
        exec_module = loader.exec_module

        def _exec(module):
            exec_module(module)
            try:
                patch(module)
            except Exception:
                pass

        loader.exec_module = _exec
        return spec


if _REC:
    sys.meta_path.insert(0, _Finder())
'''


# --- pure helpers (unit-tested) -----------------------------------------------------

def summarize(samples):
    """n / median / nearest-rank p95 / max; ``p95_basis`` says when p95 is just the max."""
    xs = sorted(float(x) for x in samples if x is not None)
    if not xs:
        return {"n": 0, "median_s": None, "p95_s": None, "max_s": None, "samples_s": []}
    n = len(xs)
    mid = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    rank = -(-95 * n // 100)
    out = {"n": n, "median_s": round(mid, 4), "p95_s": round(xs[max(rank, 1) - 1], 4),
           "max_s": round(xs[-1], 4), "samples_s": [round(x, 4) for x in xs]}
    if n < 20:
        out["p95_basis"] = "max-of-n"
    return out


def strip_ansi(data):
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
    return _ANSI.sub("", text)


def screen_words(text):
    low = text.lower()
    return {w: low.count(w.lower()) for w in SCREEN_WORDS if w.lower() in low}


def _seconds(t_ns, t0_ns):
    return None if t_ns is None else round((t_ns - t0_ns) / 1e9, 4)


def analyze(chunks, records, t0_ns):
    """Pick the screen moments from PTY ``chunks`` [(t_ns, bytes)] and shim ``records``.

    A paint record becomes visible at the first chunk read at or after it. The loading
    frame never counts as observed; a replay frame never counts as observed either.
    """
    chunks = sorted(chunks, key=lambda c: c[0])
    paints = sorted((r for r in records if r.get("k") == "paint"), key=lambda r: r["t_ns"])
    composes = sorted((r for r in records if r.get("k") == "compose"), key=lambda r: r["t_ns"])

    def visible(paint):
        if paint is None:
            return None
        for index, (t_ns, _data) in enumerate(chunks):
            if t_ns >= paint["t_ns"]:
                return index
        return None

    def first(pred):
        for paint in paints:
            if pred(paint):
                idx = visible(paint)
                if idx is not None:
                    return paint, idx
        return None, None

    live = lambda p: not p.get("loading") and not p.get("replay")        # noqa: E731

    def shown_text(paint):
        """The PTY text from this paint's first chunk to the next paint's: what that frame put on screen."""
        start = visible(paint)
        if start is None:
            return ""
        later = [p for p in paints if p["t_ns"] > paint["t_ns"]]
        stop = visible(later[0]) if later else None
        stop = len(chunks) if stop is None else stop
        return strip_ansi(b"".join(data for _t, data in chunks[start:max(start + 1, stop)]))

    def observed_with_mark(paint):
        # Basic is in, a late source is not: it counts only while the screen says so.
        return (live(paint) and not paint.get("ready") and paint.get("observed") is True
                and any(word in shown_text(paint) for word in DETAILS_WORDS))

    nonloading, nl_idx = first(lambda p: not p.get("loading"))
    replay, rp_idx = first(lambda p: p.get("replay") and not p.get("loading"))
    ready, rd_idx = first(lambda p: live(p) and p.get("ready"))
    observed, ob_idx = first(lambda p: (live(p) and p.get("ready")) or observed_with_mark(p))
    complete, cp_idx = first(lambda p: live(p) and p.get("ready") and p.get("all_new"))

    def at(idx):
        return _seconds(chunks[idx][0], t0_ns) if idx is not None else None

    source_ready = {}
    for rec in composes:
        for name, state in (rec.get("states") or {}).items():
            if name not in source_ready and state not in ("pending", "unknown", "None"):
                source_ready[name] = _seconds(rec["t_ns"], t0_ns)
    words_at_observed = screen_words(strip_ansi(chunks[ob_idx][1])) if ob_idx is not None else {}
    words_after = {}
    if ob_idx is not None:
        for _t, data in chunks[ob_idx:]:
            for word, count in screen_words(strip_ansi(data)).items():
                words_after[word] = words_after.get(word, 0) + count
    return {
        "first_byte_s": _seconds(chunks[0][0], t0_ns) if chunks else None,
        "first_nonloading_s": at(nl_idx),
        "replay_s": at(rp_idx),
        "first_observed_s": at(ob_idx),
        "first_ready_s": at(rd_idx),
        "observed_by": ("ready" if (observed or {}).get("ready") else
                        "details-wait" if observed else None),
        "details_complete_s": at(cp_idx),
        "source_ready_s": source_ready,
        "paints": len(paints),
        "late_cells": {
            "pending_at_first_nonloading": list((nonloading or {}).get("pending") or ()),
            "carried_at_observed": list((observed or {}).get("carried") or ()),
            "pending_at_replay": list((replay or {}).get("pending") or ()),
            # sources that stopped being pending only after the first observed frame, and by
            # how many seconds: the cells that fill in late
            "late_sources_s": {} if at(ob_idx) is None else {
                name: round(t - at(ob_idx), 4) for name, t in source_ready.items()
                if t is not None and t > at(ob_idx)},
            "screen_words_at_observed": words_at_observed,
            "screen_words_after_observed": words_after,
        },
    }


def read_records(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass          # a half-written last line
    except OSError:
        pass
    return rows


# --- launch machinery ---------------------------------------------------------------

def _prepare_bin(scratch):
    bindir = Path(scratch) / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name in _TOOLS:
        found = shutil.which(name)
        target = bindir / name
        if found and not target.exists():
            target.symlink_to(found)
    shim = Path(scratch) / "shim"
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "sitecustomize.py").write_text(_SHIM, encoding="utf-8")
    return bindir, shim


def child_env(fixture, *, scratch, cache_dir, marker, tag, records, bindir, shim, sessions=None):
    """A cleared environment: nothing from the caller's shell reaches the child."""
    given = dict(fixture.get("env") or {})
    root = Path(fixture.get("root") or fixture.get("base") or scratch)
    home = given.get("HOME") or str(root / "home")
    env = {
        "HOME": home,
        "XDG_STATE_HOME": given.get("XDG_STATE_HOME") or str(root / "xdg"),
        "XDG_DATA_HOME": str(Path(home) / ".local/share"),
        "XDG_CONFIG_HOME": str(Path(home) / ".config"),
        "LANG": "C.UTF-8",
    }
    env.update({k: v for k, v in given.items() if isinstance(k, str) and isinstance(v, str)})
    tmp = Path(scratch) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env.update({
        "XDG_CACHE_HOME": str(cache_dir), "TMPDIR": str(tmp), "PATH": str(bindir),
        "AGENT_HOME": str(WORKTREE), "TERM": "xterm-256color", "PYTHONPATH": str(shim),
        "PYTHONDONTWRITEBYTECODE": "1", "COLUMNS": str(COLS), "LINES": str(ROWS),
        "E2E_T3_MARKER": marker, "E2E_T3_TAG": tag, "E2E_T3_RECORDS": str(records),
    })
    if sessions:
        env["E2E_T3_SESSIONS"] = str(sessions)
    return env


def write_sessions(fixture, scratch):
    """The fixture's sessions as the shim's process-discovery answer; None when it has none."""
    items = [{"harness": s.get("harness") or "claude", "pid": s["pid"], "cwd": s["cwd"],
              "pid_start": s.get("pid_start")}
             for s in list(fixture.get("sessions") or ()) + list(fixture.get("codex_sessions") or ())
             if s.get("pid")]
    if not items:
        return None
    path = Path(scratch) / "sessions.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def _fixture_processes():
    sys.path.insert(0, str(WORKTREE / "tools"))
    try:
        import fixture_processes
    finally:
        sys.path.remove(str(WORKTREE / "tools"))
    return fixture_processes


def _observation_files(env):
    base = Path(env["XDG_STATE_HOME"]) / "hearting"
    return sorted(base.glob("fleet2-observation-*.json")) if base.is_dir() else []


def _die_with_parent():
    """Child side of fork: SIGKILL this child if the measuring process dies first."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)       # PR_SET_PDEATHSIG
    except Exception:                                                    # noqa: BLE001
        pass


def _stop(proc):
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def launch(env, *, records, timeout, linger, want_persist):
    """One PTY launch. Returns ``analyze(...)`` plus ``elapsed_s`` / ``persisted``."""
    argv = [sys.executable, str(WORKTREE / "tools/fleet/fleet.py"), "--explorer", "--no-tmux"]
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    t0 = time.monotonic_ns()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                            cwd=str(WORKTREE), start_new_session=True, close_fds=True,
                            preexec_fn=_die_with_parent)
    os.close(slave)
    chunks, result, last_look = [], {}, 0.0
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append((time.monotonic_ns(), data))
            now = (time.monotonic_ns() - t0) / 1e9
            if now - last_look >= 0.5:
                last_look = now
                result = analyze(chunks, read_records(records), t0)
                seen = result["first_observed_s"]
                if seen is not None and now - seen >= linger and (
                        not want_persist or _observation_files(env) or now - seen >= linger + 8):
                    break
            if now >= timeout or proc.poll() is not None:
                break
    finally:
        _stop(proc)
        os.close(master)
    result = analyze(chunks, read_records(records), t0)
    result["elapsed_s"] = round((time.monotonic_ns() - t0) / 1e9, 3)
    result["persisted"] = bool(_observation_files(env))
    result["exit_code"] = proc.returncode
    return result


def once_full_collect(env, timeout=90):
    argv = [sys.executable, str(WORKTREE / "tools/fleet/fleet.py"), "--once", "--json"]
    t0 = time.monotonic()
    try:
        done = subprocess.run(argv, env=env, cwd=str(WORKTREE), capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=timeout, start_new_session=True)
    except subprocess.TimeoutExpired:
        return None, "timeout after %ss" % timeout
    return (round(time.monotonic() - t0, 3), None) if done.returncode == 0 else (
        None, "exit %s" % done.returncode)


LAUNCH_TIMEOUT_S = 60.0


def measure(fixture, *, reps=5, scratch, launch_timeout=LAUNCH_TIMEOUT_S, linger=4.0,
            conditions=CONDITIONS, launch_fn=None):
    """Result contract: ``{"metric":"T3","events","segments","passes","notes","not_measured"}``.

    A launch that shows no observed frame within ``launch_timeout`` is a result ("did not
    leave loading in N s"), and it ends its condition: the remaining launches would only
    repeat the wait, so they are skipped and the reason is written down.
    """
    launch_fn = launch_fn or launch
    scratch = Path(scratch) / "t3"
    scratch.mkdir(parents=True, exist_ok=True)
    marker = str(fixture.get("marker") or "hearting-e2e-t3-%d-%s" % (os.getpid(), os.urandom(3).hex()))
    fproc = _fixture_processes()
    bindir, shim = _prepare_bin(scratch)
    sessions_file = write_sessions(fixture, scratch)
    events, segments, passes, notes, not_measured = {}, {}, [], [], []
    tags, leftovers, timed_out = [], [], {}

    def run_condition(cond):
        samples = {k: [] for k in ("first_byte_s", "first_nonloading_s", "replay_s",
                                   "first_observed_s", "first_ready_s", "details_complete_s")}
        for i in range(reps):
            tag = "%s-t3-%s-%d" % (marker, cond, i)
            tags.append(tag)
            cache = scratch / ("cache-%s" % ("cold-%d" % i if cond == "cold-empty" else "second"))
            cache.mkdir(parents=True, exist_ok=True)
            records = scratch / ("rec-%s-%d.jsonl" % (cond, i))
            records.unlink(missing_ok=True)
            env = child_env(fixture, scratch=scratch, cache_dir=cache, marker=marker, tag=tag,
                            records=records, bindir=bindir, shim=shim, sessions=sessions_file)
            if cond == "cold-empty":
                for stale in _observation_files(env):
                    stale.unlink()
            elif not _observation_files(env):
                not_measured.append({"what": "T3/second-launch/%d" % i,
                                     "reason": "no saved last-complete model before this launch"})
            row = launch_fn(env, records=records, timeout=launch_timeout, linger=linger,
                            want_persist=True)
            row.update(condition=cond, rep=i)
            passes.append(row)
            for key in samples:
                samples[key].append(row.get(key))
            try:
                fproc.reap(tag)
            except TimeoutError as exc:
                leftovers.append(str(exc))
            if row.get("first_observed_s") is None:
                skipped = reps - i - 1
                timed_out[cond] = {"after_s": launch_timeout, "launch": i, "skipped": skipped}
                not_measured.append({"what": "T3/%s" % cond, "reason":
                                     "launch %d never showed an observed frame within %ss (still loading); "
                                     "the remaining %d launch(es) of this condition were not run"
                                     % (i, launch_timeout, skipped)})
                break
        events[cond] = summarize(samples["first_observed_s"])
        if cond in timed_out:
            events[cond]["no_observed_within_s"] = launch_timeout
        segments[cond] = {k: summarize(v) for k, v in samples.items() if k != "first_observed_s"}
        if events[cond]["n"] < reps and cond not in timed_out:
            not_measured.append({"what": "T3/%s" % cond, "reason":
                                 "%d of %d launches never showed an observed frame within %ss"
                                 % (reps - events[cond]["n"], reps, launch_timeout)})

    # A killed run must still reap what it started: turn SIGTERM into an exception so the
    # ``finally`` below runs (only possible on the main thread).
    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    except ValueError:
        pass
    try:
        for cond in conditions:
            run_condition(cond)
        once_env = child_env(fixture, scratch=scratch, cache_dir=scratch / "cache-once",
                             marker=marker, tag=marker + "-t3-once", records=scratch / "rec-once.jsonl",
                             bindir=bindir, shim=shim, sessions=sessions_file)
        (scratch / "cache-once").mkdir(exist_ok=True)
        tags.append(marker + "-t3-once")
        once = []
        for _ in range(min(reps, 3)):
            took, why = once_full_collect(once_env)
            if took is None:
                not_measured.append({"what": "T3/once-full-collect", "reason": why})
            else:
                once.append(took)
        events["once-full-collect"] = summarize(once)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        for tag in tags:
            try:
                fproc.reap(tag)
            except TimeoutError as exc:
                leftovers.append(str(exc))
    leftover_now = sorted({pid for tag in tags for pid in fproc.marked_processes(tag)})
    notes.append("first_observed = first live, non-loading, non-replay paint that is ready (old definition) "
                 "or observed with 'details wait'/'세부 아직' on screen (new definition; a tree that does "
                 "not record 'observed' gives the old one); first_ready_s = the old definition on every "
                 "tree; replay_s exists only in second-launch")
    return {"metric": "T3", "events": events, "segments": segments, "passes": passes,
            "notes": notes, "not_measured": not_measured,
            "cleanup": {"leftover_processes": leftover_now, "reap_errors": leftovers}}


def main(argv=None):
    """Smoke entry for a human: build a small fixture and run ``measure`` once."""
    import argparse
    import tempfile
    parser = argparse.ArgumentParser(allow_abbrev=False, description=__doc__.splitlines()[0])
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--jobs-rows", type=int, default=300)
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(WORKTREE / "utilities"))
    import hook_perf_fixture
    with tempfile.TemporaryDirectory(prefix="hearting-e2e-t3-") as tmp:
        fixture = hook_perf_fixture.build(Path(tmp) / "fx", jobs_rows=args.jobs_rows)
        result = measure(fixture, reps=args.reps, scratch=Path(tmp) / "scratch")
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
