#!/usr/bin/env python3
"""Production read-only sampler: registry change -> producer cache publication lag.

It watches, and never touches, the operator's dispatch registry and the producer's
two caches (``projection.json``, ``fleet.json``). Per ``--period`` it ``os.stat``s
each file. A registry whose (dev, ino, size, mtime, ctime) changed is one change at
its mtime: ``append`` when the same inode only grew, else ``replace``. A cache whose
mtime/inode changed is one publication; the only value taken from it is
``observed_at`` (the rest of the JSON is parsed and dropped). The lag of a change is
the first publication whose ``observed_at`` is at or after the change, minus the
change time. Output carries counts and times only: no path, no row, no cache content.

Read-only is enforced for the sampling loop and for the output write, not for the whole
process: an audit hook armed around those two refuses (and records as ``violations``)
any write-mode open outside ``--out``/``--markdown``, ``os.kill`` and signal calls,
rename/remove/mkdir/utime/chmod and the like, sockets and process spawns; one violation
fails the run (exit 1). What runs before it (argument parsing, ``default_registries()``
importing the Fleet resolver that names the registry paths) is outside the hook; that
part only reads, which is checked by reading it and by an isolated run, not by the hook.
No bytecode is written either: ``measure_e2e_latency.py`` turns ``dont_write_bytecode`` on
before any import.

    python3 tools/measure_e2e_latency.py live --duration 600 --period 0.1 --out live.json

``Sampler`` is the clock-free core; ``live.test.py`` drives it with synthetic stats.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True      # also set by measure_e2e_latency.py before any import (this module's own .pyc)
HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[1]
CACHE_NAMES = ("projection", "fleet")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_REFUSED = {
    "os.kill": "signal", "os.killpg": "signal", "signal.pthread_kill": "signal",
    "signal.raise_signal": "signal", "os.rename": "write", "os.replace": "write",
    "os.remove": "write", "os.rmdir": "write", "os.mkdir": "write", "os.utime": "write",
    "os.chmod": "write", "os.chown": "write", "os.truncate": "write", "os.link": "write",
    "os.symlink": "write", "os.mkfifo": "write", "os.mknod": "write",
    "subprocess.Popen": "spawn", "os.system": "spawn", "os.fork": "spawn",
    "os.posix_spawn": "spawn", "os.spawn": "spawn", "os.exec": "spawn",
    "socket.connect": "socket", "socket.bind": "socket",
}


def summarize(samples):
    xs = sorted(float(x) for x in samples)
    if not xs:
        return {"n": 0, "median_s": None, "p95_s": None, "max_s": None}
    n = len(xs)
    mid = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    out = {"n": n, "median_s": round(mid, 4), "p95_s": round(xs[max(-(-95 * n // 100), 1) - 1], 4),
           "max_s": round(xs[-1], 4)}
    if n < 20:
        out["p95_basis"] = "max-of-n"
    return out


# --- read-only enforcement ------------------------------------------------------------

class ReadOnlyGuard:
    """One process-wide audit hook, active only while a ``with ReadOnlyGuard(...)`` block runs.

    It guards what runs inside that block (``run``'s sampling loop and ``main``'s output
    write), nothing before or after it.
    """
    _installed = False
    _guards = []

    def __init__(self, allowed_writes=()):
        self.allowed = {os.path.realpath(p) for p in allowed_writes}
        self.violations = []

    def __enter__(self):
        if not ReadOnlyGuard._installed:
            sys.addaudithook(ReadOnlyGuard._hook)
            ReadOnlyGuard._installed = True
        ReadOnlyGuard._guards.append(self)
        return self

    def __exit__(self, *_exc):
        if self in ReadOnlyGuard._guards:
            ReadOnlyGuard._guards.remove(self)
        return False

    @staticmethod
    def _hook(event, args):
        if not ReadOnlyGuard._guards:
            return
        guard = ReadOnlyGuard._guards[-1]
        kind = _REFUSED.get(event)
        if kind is None and event.startswith(("os.exec", "os.spawn", "os.posix_spawn")):
            kind = "spawn"
        if kind is None and event == "open":
            mode, flags = (args[1] if len(args) > 1 else None), (args[2] if len(args) > 2 else 0)
            writing = (isinstance(flags, int) and flags & _WRITE_FLAGS) or (
                isinstance(mode, str) and any(c in mode for c in "wax+"))
            if not writing:
                return
            target = args[0]
            if isinstance(target, (str, bytes, os.PathLike)):
                try:
                    if os.path.realpath(os.fsdecode(target)) in guard.allowed:
                        return
                except (OSError, ValueError):
                    pass
            kind = "write"
        if kind is None:
            return
        guard.violations.append({"event": event, "kind": kind})
        raise PermissionError("read-only sampler refused %s" % event)


# --- sampling core --------------------------------------------------------------------

def _sig(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _observed_at(path):
    """The one value taken from a cache: its ``observed_at`` (anything else is dropped)."""
    try:
        with open(path, "rb") as fh:
            value = json.loads(fh.read().decode("utf-8", "replace"))
        seen = value.get("observed_at") if isinstance(value, dict) else None
        return float(seen) if isinstance(seen, (int, float)) and not isinstance(seen, bool) else None
    except (OSError, ValueError):
        return None


class Sampler:
    """Feed it ``poll(now)``; it only calls ``os.stat`` and opens cache files to read."""

    def __init__(self, registries, cache_dir):
        self.registries = [str(p) for p in registries]
        self.cache_paths = {name: str(Path(cache_dir) / (name + ".json")) for name in CACHE_NAMES}
        self._reg = {}
        self._cache = {}
        self.changes = []                       # {"t", "kind", "registry"}
        self.publications = {n: [] for n in CACHE_NAMES}   # {"observed_at","published_at","seen_at"}
        self.cache_age = {n: [] for n in CACHE_NAMES}
        self.polls = 0
        self._latest = {}

    def poll(self, now):
        self.polls += 1
        for index, path in enumerate(self.registries):
            try:
                st = os.stat(path)
            except OSError:
                st = None
            sig = _sig(st) if st else None
            if index not in self._reg:
                self._reg[index] = (sig, st)
                continue
            old_sig, old = self._reg[index]
            if sig is not None and sig != old_sig:
                grew = (old is not None and (st.st_dev, st.st_ino) == (old.st_dev, old.st_ino)
                        and st.st_size > old.st_size)
                self.changes.append({"t": st.st_mtime_ns / 1e9,
                                     "kind": "append" if grew else "replace", "registry": index})
            self._reg[index] = (sig, st if st else old)
        for name, path in self.cache_paths.items():
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig = (st.st_dev, st.st_ino, st.st_mtime_ns)
            if self._cache.get(name) != sig:
                first_look = self.polls == 1
                self._cache[name] = sig
                seen = _observed_at(path)
                if seen is not None:
                    self._latest[name] = seen
                    if not first_look:      # the file already there at the start is no publication
                        self.publications[name].append(
                            {"observed_at": seen, "published_at": st.st_mtime_ns / 1e9,
                             "seen_at": now})
            if name in self._latest:
                self.cache_age[name].append(max(0.0, now - self._latest[name]))

    def lags(self, name):
        """Per change: lag to the first publication that observed it. ``(paired, unpaired)``."""
        pubs = sorted(self.publications[name], key=lambda p: p["published_at"])
        paired, unpaired = [], 0
        for change in self.changes:
            hit = next((p for p in pubs if p["observed_at"] >= change["t"]
                        and p["published_at"] >= change["t"]), None)
            if hit is None:
                unpaired += 1
            else:
                paired.append({"kind": change["kind"], "t": change["t"],
                               "lag_s": hit["published_at"] - change["t"],
                               "observed_lag_s": hit["observed_at"] - change["t"]})
        return paired, unpaired

    def report(self):
        latency = {}
        for name in CACHE_NAMES:
            paired, unpaired = self.lags(name)
            latency[name] = {
                "all": summarize(p["lag_s"] for p in paired),
                "append": summarize(p["lag_s"] for p in paired if p["kind"] == "append"),
                "replace": summarize(p["lag_s"] for p in paired if p["kind"] == "replace"),
                "observed_lag": summarize(p["observed_lag_s"] for p in paired),
                "unpaired_changes": unpaired,
                "publications": len(self.publications[name]),
            }
        counts = {"append": 0, "replace": 0}
        for change in self.changes:
            counts[change["kind"]] += 1
        return {
            "polls": self.polls,
            "registry": {"watched": len(self.registries), "changes": counts,
                         "change_times": [round(c["t"], 3) for c in self.changes][:5000]},
            "latency": latency,
            "cache_age": {n: summarize(self.cache_age[n]) for n in CACHE_NAMES},
        }


def producer_lock_held(lock_path):
    """True when some process holds an flock on ``lock_path`` (from /proc/locks; no cmdline)."""
    try:
        st = os.stat(lock_path)
        want = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
        with open("/proc/locks", encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                for field in fields:
                    parts = field.split(":")
                    if len(parts) == 3 and all(parts):
                        try:
                            got = (int(parts[0], 16), int(parts[1], 16), int(parts[2]))
                        except ValueError:
                            continue
                        if got == want:
                            return True
    except OSError:
        return False
    return False


def default_registries():
    """The registries Fleet itself would read (``human_gate_registry_paths``), resolved read-only."""
    sys.path.insert(0, str(WORKTREE / "tools"))
    try:
        from fleet.collectors import dispatch
        return [str(p) for p in dispatch.human_gate_registry_paths()]
    finally:
        sys.path.remove(str(WORKTREE / "tools"))


def default_cache_dir():
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return str(Path(xdg) / "hearting" / "human-waits")


def run(registries, cache_dir, *, duration, period, outputs=(), clock=time.time,
        sleep=time.sleep, mono=time.monotonic):
    """Sample for ``duration`` seconds under the guard. Returns the result dict."""
    sampler = Sampler(registries, cache_dir)
    lock = str(Path(cache_dir) / ".projection.lock")
    started = clock()
    with ReadOnlyGuard(allowed_writes=outputs) as guard:
        lock_start = producer_lock_held(lock)
        deadline = mono() + duration
        try:
            while True:
                sampler.poll(clock())
                if mono() >= deadline:
                    break
                sleep(period)
        except PermissionError:
            pass
        lock_end = producer_lock_held(lock)
    result = {"tool": "e2e_latency.live", "read_only": True, "duration_s": duration,
              "period_s": period, "started_at": round(started, 3), "ended_at": round(clock(), 3)}
    result.update(sampler.report())
    result["producer"] = {"lock_held_start": lock_start, "lock_held_end": lock_end}
    result["violations"] = list(guard.violations)
    return result


def to_markdown(result):
    def row(label, s):
        return "| %s | %s | %s | %s | %s |" % (label, s["n"], s["median_s"], s["p95_s"], s["max_s"])

    lines = ["# 운영 읽기 전용 표본 — registry 변화 → cache 게시 지연", "",
             "- 표본 %s회, %.0f초, 변화 덧붙음 %s·교체 %s" % (
                 result["polls"], result["duration_s"], result["registry"]["changes"]["append"],
                 result["registry"]["changes"]["replace"]),
             "- producer lock 보유: 시작 %s, 끝 %s" % (
                 result["producer"]["lock_held_start"], result["producer"]["lock_held_end"]),
             "- 위반: %d" % len(result["violations"]), "",
             "| 구간 | n | 중앙 s | p95 s | 최대 s |", "|---|---|---|---|---|"]
    for name in CACHE_NAMES:
        block = result["latency"][name]
        for key in ("all", "append", "replace", "observed_lag"):
            lines.append(row("%s %s" % (name, key), block[key]))
        lines.append("| %s 짝 못 찾은 변화 | %s | | | |" % (name, block["unpaired_changes"]))
    for name in CACHE_NAMES:
        lines.append(row("%s cache 나이" % name, result["cache_age"][name]))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False, prog="live",
                                     description="read-only registry -> cache publication lag")
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--period", type=float, default=0.1)
    parser.add_argument("--out")
    parser.add_argument("--markdown")
    parser.add_argument("--registry", action="append", default=None,
                        help="registry file to watch (repeatable); default: what Fleet reads")
    parser.add_argument("--cache-dir", default=None, help="human-waits dir; default: XDG state")
    args = parser.parse_args(argv)
    if args.duration <= 0 or args.period <= 0:
        parser.error("--duration and --period must be positive")
    outputs = [p for p in (args.out, args.markdown) if p]
    for path in outputs:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    try:
        registries = args.registry if args.registry is not None else default_registries()
    except Exception as exc:                                     # noqa: BLE001
        sys.stderr.write("live: cannot resolve registries: %s: %s\n" % (type(exc).__name__, exc))
        return 2
    cache_dir = args.cache_dir or default_cache_dir()
    result = run(registries, cache_dir, duration=args.duration, period=args.period, outputs=outputs)
    text = json.dumps(result, ensure_ascii=False, indent=1) + "\n"
    with ReadOnlyGuard(allowed_writes=outputs) as guard:
        try:
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
            if args.markdown:
                Path(args.markdown).write_text(to_markdown(result), encoding="utf-8")
        except PermissionError:
            pass
    if result["violations"] or guard.violations:
        sys.stderr.write("live: read-only violations: %s\n" % (result["violations"] + guard.violations))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
