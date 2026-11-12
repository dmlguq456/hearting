"""T1 and T2: a route or worker value changes -> shell panel backend / Fleet screen model.

One producer child (``utilities/human_wait_state.py --refresh-cache``, the real
loop) and one Fleet child (``tools/fleet/fleet.py --explorer --no-tmux`` on a PTY)
run against the same fixture with the pass shim. After one cold pass and two
unchanged warm passes, events are injected through the real writers
(``events.py``), at phases spread over the producer's cadence (the #1548 method,
widened), and each event is followed to its end:

T1  event -> (producer wake -> pass -> publish) -> ``projection.json`` carries it
    -> host debounce -> ``session_shell.panel_projection`` answers.
T2  event -> first ``collect_all`` that starts after it -> the Fleet explorer
    ``_explorer_compose`` that consumes it ends (the screen model).

Latency is measured from the *completed write*. Nothing is mocked in the timed
path; the only substitution is session discovery (fixture sleepers, see shim).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import re
import signal
import subprocess
import sys
import threading
import time

from . import events, fixture as fx, shim_pass

REPO = fx.REPO
PHASES_S = (0.3, 1.7, 3.1, 0.9, 2.4, 4.2, 1.2, 2.8)
HOST_DEBOUNCE_S = 0.025  # host.cjs queueProjection setTimeout
WARM_PASSES = 2
EVENT_TIMEOUT_S = 45.0
FLEET_TAIL_S = 75.0


def percentile(values, q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..100)."""
    data = sorted(values)
    if not data:
        return None
    rank = min(len(data), max(1, int(-(-q * len(data) // 100))))
    return data[rank - 1]


def summarize(samples) -> dict:
    values = [v for v in samples if v is not None]
    out = {"n": len(values), "median_s": None, "p95_s": None, "max_s": None,
           "samples_s": [round(v, 4) for v in values]}
    if values:
        data = sorted(values)
        mid = len(data) // 2
        out["median_s"] = round(data[mid] if len(data) % 2 else (data[mid - 1] + data[mid]) / 2, 4)
        out["p95_s"] = round(percentile(values, 95), 4)
        out["max_s"] = round(data[-1], 4)
        if len(values) < 20:
            out["p95_basis"] = "max-of-n"
    return out


def registry_min_gap() -> float:
    """``_REGISTRY_MIN_GAP`` from the producer's own source (not a copy of the number)."""
    try:
        text = (REPO / "utilities" / "human_wait_state.py").read_text(encoding="utf-8")
        return float(re.search(r"^_REGISTRY_MIN_GAP\s*=\s*([0-9.]+)", text, re.M).group(1))
    except (OSError, AttributeError, ValueError):
        return 2.0


def host_debounce_s() -> float:
    try:
        text = (REPO / "utilities" / "session-shell" / "host.cjs").read_text(encoding="utf-8")
        match = re.search(r"projectionTimer=setTimeout\(\(\)=>\{[^}]*\},(\d+)\)", text)
        return int(match.group(1)) / 1000.0
    except (OSError, AttributeError, ValueError):
        return HOST_DEBOUNCE_S


class Children:
    """Producer, Fleet and probe children of one run, all marked and all ours."""

    def __init__(self, fixture: dict, scratch: Path):
        self.fixture, self.scratch = fixture, scratch
        self.procs: list[subprocess.Popen] = []
        self.stop = threading.Event()
        self.master = None
        self.shim_dir = shim_pass.install(scratch / "shim")
        sessions = scratch / "sessions.json"
        sessions.write_text(json.dumps([
            {"harness": "claude", "pid": s["pid"], "pid_start": s["pid_start"], "cwd": s["cwd"]}
            for s in fixture["sessions"]]), encoding="utf-8")
        self.sessions_file = str(sessions)
        self.producer_log = scratch / "producer.jsonl"
        self.fleet_log = scratch / "fleet.jsonl"
        self.root = Path(fixture["env"]["XDG_STATE_HOME"]) / "hearting" / "human-waits"

    # -- demand marker keeps the producer alive and gives the panels something to show
    def write_demand(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        value = {f"claude\0{s['session_id']}": time.time() for s in self.fixture["sessions"]}
        tmp = self.root / ".demand.tmp"
        tmp.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.root / ".demand")

    def _demand_loop(self) -> None:
        while not self.stop.wait(20.0):
            try:
                self.write_demand()
            except OSError:
                pass

    def start_producer(self) -> None:
        self.write_demand()
        env = fx.child_env(self.fixture, shim_pass.shim_env(
            out=str(self.producer_log), shim_dir=self.shim_dir, role="producer",
            sessions=self.sessions_file))
        self.procs.append(subprocess.Popen(
            [sys.executable, str(REPO / "utilities" / "human_wait_state.py"), "--refresh-cache"],
            env=env, cwd=self.fixture["root"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True))
        threading.Thread(target=self._demand_loop, daemon=True).start()

    def start_fleet(self) -> None:
        master, slave = pty.openpty()
        import fcntl
        import struct
        import termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        env = fx.child_env(self.fixture, {**shim_pass.shim_env(
            out=str(self.fleet_log), shim_dir=self.shim_dir, role="fleet",
            sessions=self.sessions_file), "TERM": "xterm-256color", "COLUMNS": "120", "LINES": "40"})
        self.procs.append(subprocess.Popen(
            [sys.executable, str(REPO / "tools" / "fleet" / "fleet.py"), "--explorer", "--no-tmux"],
            env=env, cwd=self.fixture["root"], stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True))
        os.close(slave)
        self.master = master

        def drain() -> None:  # an undrained PTY would stall the Fleet child
            while not self.stop.is_set():
                try:
                    if not os.read(master, 65536):
                        return
                except OSError:
                    return
        threading.Thread(target=drain, daemon=True).start()

    def close(self) -> None:
        self.stop.set()
        for proc in self.procs:
            try:
                os.kill(proc.pid, signal.SIGTERM)  # our own child, nothing else
            except OSError:
                pass
        for proc in self.procs:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass  # the orchestrator's marker reap is the backstop
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass


class Probe:
    """The shell host's side of T1: one persistent child answering ``panel_projection``."""

    SCRIPT = (
        "import sys, json, time\n"
        "sys.path[:0] = [%r, %r]\n"
        "import session_shell\n"
        "for line in sys.stdin:\n"
        "    sid, pid, start = line.split()\n"
        "    t0 = time.monotonic_ns()\n"
        "    try:\n"
        "        out = session_shell.panel_projection('claude', sid, native_pid=int(pid), native_start=start,\n"
        "                                             columns=120, rows=40, state='idle')\n"
        "        err = None\n"
        "    except Exception as exc:\n"
        "        out, err = {}, repr(exc)\n"
        "    t1 = time.monotonic_ns()\n"
        "    print(json.dumps({'dur_s': (t1 - t0) / 1e9, 'error': err, 't1': t1,\n"
        "                      'label': str(out.get('route_label') if isinstance(out, dict) else '')[:80]}), flush=True)\n"
    ) % (str(REPO / "utilities"), str(REPO / "tools"))

    def __init__(self, fixture: dict, shim_dir: str):
        env = fx.child_env(fixture)
        self.proc = subprocess.Popen([sys.executable, "-c", self.SCRIPT], env=env, cwd=fixture["root"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, start_new_session=True)

    def ask(self, session: dict) -> dict:
        self.proc.stdin.write(f"{session['session_id']} {session['pid']} {session['pid_start']}\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                os.kill(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass


def _stat_ns(path: Path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _read_cache(path: Path) -> tuple[float | None, str]:
    try:
        text = path.read_text(encoding="utf-8")
        return float(json.loads(text).get("observed_at") or 0), text
    except (OSError, ValueError, AttributeError):
        return None, ""


SLOW_KINDS = ("worker-tool", "answer-wait", "owner-dead")
SLOW_STAGGER_S = 7.0


def _sleep_for(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(0.05, max(0.0, end - time.monotonic())))


def _inject(fixture, kind, seq, rep, phase, not_measured):
    try:
        ev = events.inject(fixture, kind, seq)
    except Exception as exc:  # a writer that cannot run is reported, not hidden
        not_measured.append({"what": f"{kind}#{seq}", "reason": f"{type(exc).__name__}: {exc}"})
        return None
    ev.update(phase_s=phase, seq=seq, rep=rep)
    return ev


class PublishWatch:
    """Marks each pending event with the first ``projection.json`` publish that carries it.

    Polled on every wait, so an event is stamped when its publish happens, not when the
    caller next looks (a staggered batch keeps polling between its injections).
    """

    def __init__(self, cache: Path):
        self.cache, self.last, self.pending = cache, _stat_ns(cache), []

    def add(self, ev: dict) -> None:
        self.pending.append(ev)

    def poll(self) -> None:
        stamp = _stat_ns(self.cache)
        if stamp is None or stamp == self.last or not self.pending:
            self.last = stamp if stamp is not None else self.last
            return
        self.last = stamp
        observed, text = _read_cache(self.cache)
        now = time.monotonic_ns()
        if observed is None:
            return
        for ev in list(self.pending):
            if observed >= ev["wall"] and (ev["kind"] != "answer-wait" or ev["marker"] in text):
                ev["detect_ns"] = now
                self.pending.remove(ev)

    def run(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.poll()
            time.sleep(0.002)

    def until_done(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while self.pending and time.monotonic() < end:
            self.poll()
            time.sleep(0.002)


def _ask(probe, fixture, ev, seq, probe_rows) -> None:
    if "detect_ns" not in ev:
        return
    answer = probe.ask(fixture["sessions"][seq % len(fixture["sessions"])])
    ev["projection_s"] = answer["dur_s"]
    ev["projection_error"] = answer.get("error")
    probe_rows[seq] = answer.get("label")


def wait_warm(children: Children, *, timeout: float) -> dict:
    """One cold pass and ``WARM_PASSES`` unchanged warm passes of each child."""
    deadline = time.monotonic() + timeout
    state = {"producer": 0, "fleet_collect": 0, "fleet_compose": 0}
    while time.monotonic() < deadline:
        producer = shim_pass.read_passes(children.producer_log)
        fleet = shim_pass.read_passes(children.fleet_log)
        state = {"producer": len(producer),
                 "fleet_collect": sum(1 for r in fleet if r["root"] == "__init__.collect_all"),
                 "fleet_compose": sum(1 for r in fleet if r["root"] == "render._explorer_compose")}
        if (state["producer"] >= 1 + WARM_PASSES and state["fleet_collect"] >= 1 + WARM_PASSES
                and state["fleet_compose"] >= 1):
            break
        time.sleep(0.5)
    state["warm_ready"] = (state["producer"] >= 1 + WARM_PASSES
                           and state["fleet_collect"] >= 1 + WARM_PASSES and state["fleet_compose"] >= 1)
    return state


def _publisher_pass(passes: list[dict], detect_ns: int):
    done = [p for p in passes if p["t1"] <= detect_ns + 5_000_000]
    return done[-1] if done else None


def _pass_row(index: int, kind: str, rec: dict, role: str) -> dict:
    return {"index": index, "role": role, "kind": kind, "root": rec["root"],
            "dur_s": round((rec["t1"] - rec["t0"]) / 1e9, 4),
            "segments": {name: {"n": s["n"], "total_s": round(s["total_ns"] / 1e9, 4),
                                "max_s": round(s["max_ns"] / 1e9, 4)}
                         for name, s in sorted(rec["seg"].items())},
            "open_calls": rec.get("open"), "listing_calls": rec.get("listing"),
            "metrics": rec.get("metrics", {})}


def measure(fixture: dict, *, reps: int, scratch: Path, kinds=None, warm_timeout: float = 300.0,
            event_timeout: float = EVENT_TIMEOUT_S) -> dict:
    """Run T1 and T2 against ``fixture``; the result follows the orchestrator contract."""
    kinds = list(kinds or events.KINDS)
    min_gap = registry_min_gap()
    debounce = host_debounce_s()
    children = Children(fixture, scratch)
    probe = None
    injected: list[dict] = []
    notes: list[str] = []
    not_measured: list[dict] = []
    cache = children.root / "projection.json"
    try:
        children.start_producer()
        children.start_fleet()
        probe = Probe(fixture, children.shim_dir)
        warm = wait_warm(children, timeout=warm_timeout)
        if not warm["warm_ready"]:
            notes.append(f"warm-up incomplete after {warm_timeout:.0f}s: {warm}")
        probe_rows = {}
        # The host process is long-lived: its first answer pays imports, so it is asked once
        # before any event and that cold answer is reported on its own.
        cold_probe = probe.ask(fixture["sessions"][0])
        probe.ask(fixture["sessions"][0])
        notes.append(f"panel_projection first (cold) answer {cold_probe['dur_s']:.3f}s, not in any sample")
        seq = 0
        slow = [k for k in kinds if k in SLOW_KINDS]
        fast = [k for k in kinds if k not in SLOW_KINDS]
        for rep in range(reps):
            for kind in fast:
                seq += 1
                watch = PublishWatch(cache)
                _sleep_for(PHASES_S[seq % len(PHASES_S)])  # phase after the last publish
                ev = _inject(fixture, kind, seq, rep, PHASES_S[seq % len(PHASES_S)], not_measured)
                if ev is None:
                    continue
                injected.append(ev)
                watch.add(ev)
                watch.until_done(event_timeout)
                _ask(probe, fixture, ev, seq, probe_rows)
            if slow:
                # These writes are not registry changes, so the producer only sees them at its own
                # cadence. One batch per rep, staggered, so the events land at different phases of
                # that cadence and share the wait instead of serialising it.
                batch = []
                watch = PublishWatch(cache)
                _sleep_for(PHASES_S[(seq + 1) % len(PHASES_S)])
                for position, kind in enumerate(slow):
                    seq += 1
                    if position:
                        watch.run(SLOW_STAGGER_S)
                    ev = _inject(fixture, kind, seq, rep, PHASES_S[seq % len(PHASES_S)], not_measured)
                    if ev is not None:
                        batch.append(ev)
                        injected.append(ev)
                        watch.add(ev)
                watch.until_done(event_timeout)
                for ev in batch:
                    _ask(probe, fixture, ev, ev["seq"], probe_rows)
        # Fleet answers keep arriving at its own cadence: give the last events time to resolve.
        tail_end = time.monotonic() + FLEET_TAIL_S
        while time.monotonic() < tail_end:
            fleet = shim_pass.read_passes(children.fleet_log)
            last_event = max((e["t_ns"] for e in injected), default=0)
            collects = [r for r in fleet if r["root"] == "__init__.collect_all" and r["t0"] > last_event]
            composes = [r for r in fleet if r["root"] == "render._explorer_compose"
                        and collects and r["t0"] >= collects[0]["t1"]]
            if collects and composes:
                break
            time.sleep(0.5)
        producer = shim_pass.read_passes(children.producer_log)
        fleet = shim_pass.read_passes(children.fleet_log)
    finally:
        if probe is not None:
            probe.close()
        children.close()
    return _assemble(fixture, kinds, injected, producer, fleet, min_gap, debounce, notes,
                     not_measured, warm, children.producer_log)


def _assemble(fixture, kinds, injected, producer, fleet, min_gap, debounce, notes, not_measured, warm,
              producer_log=None) -> dict:
    t1_total: dict[str, list] = {k: [] for k in kinds}
    t2_total: dict[str, list] = {k: [] for k in kinds}
    seg_samples: dict[str, dict[str, list]] = {k: {} for k in kinds}
    t2_seg: dict[str, dict[str, list]] = {k: {} for k in kinds}
    publisher_passes: dict[int, str] = {}
    fleet_collects = [r for r in fleet if r["root"] == "__init__.collect_all"]
    fleet_composes = [r for r in fleet if r["root"] == "render._explorer_compose"]
    for ev in injected:
        kind = ev["kind"]
        if "detect_ns" in ev:
            publish = _publisher_pass(producer, ev["detect_ns"])
            if publish is not None:
                pass_start = max(publish["t0"], ev["t_ns"])
                previous = [p for p in producer if p["t1"] <= publish["t0"]]
                wake = max(0.0, (publish["t0"] - ev["t_ns"]) / 1e9)
                ready_gap = 0.0
                if previous:
                    prev = previous[-1]
                    ready = prev["t1"] + int(max(min_gap, (prev["t1"] - prev["t0"]) / 1e9) * 1e9)
                    ready_gap = min(wake, max(0.0, (ready - ev["t_ns"]) / 1e9))
                total = (ev["detect_ns"] - ev["t_ns"]) / 1e9 + debounce + ev["projection_s"]
                parts = {
                    "wake_s": wake - ready_gap, "min_gap_s": ready_gap,
                    "pass_s": (publish["t1"] - publish["t0"]) / 1e9,
                    "publish_s": max(0.0, (ev["detect_ns"] - publish["t1"]) / 1e9),
                    "debounce_s": debounce, "projection_s": ev["projection_s"],
                }
                if publish["t0"] < ev["t_ns"]:
                    # a pass that began before the event read the registry after it
                    parts["pass_s"] = max(0.0, (publish["t1"] - ev["t_ns"]) / 1e9)
                    parts["wake_s"] = parts["min_gap_s"] = 0.0
                    parts["pass_started_before_event"] = True
                t1_total[kind].append(total)
                for key, value in parts.items():
                    if isinstance(value, bool):
                        continue
                    seg_samples[kind].setdefault(key, []).append(value)
                publisher_passes[publish["index"]] = f"after-{kind}"
        collect = next((r for r in fleet_collects if r["t0"] >= ev["t_ns"]), None)
        if collect is not None:
            compose = next((r for r in fleet_composes if r["t0"] >= collect["t1"]), None)
            if compose is not None:
                t2_total[kind].append((compose["t1"] - ev["t_ns"]) / 1e9)
                t2_seg[kind].setdefault("wait_for_collect_s", []).append((collect["t0"] - ev["t_ns"]) / 1e9)
                t2_seg[kind].setdefault("collect_s", []).append((collect["t1"] - collect["t0"]) / 1e9)
                t2_seg[kind].setdefault("compose_s", []).append((compose["t1"] - compose["t0"]) / 1e9)
                t2_seg[kind].setdefault("compose_wait_s", []).append((compose["t0"] - collect["t1"]) / 1e9)
    passes = []
    for index, rec in enumerate(producer, 1):
        label = "cold-1" if index == 1 else publisher_passes.get(rec["index"], "warm-unchanged")
        passes.append(_pass_row(index, label, rec, "producer"))
    fleet_labels: dict[int, str] = {}
    for ev in injected:
        collect = next((r for r in fleet_collects if r["t0"] >= ev["t_ns"]), None)
        if collect is not None and collect["index"] not in fleet_labels:
            fleet_labels[collect["index"]] = f"after-{ev['kind']}"
    for index, rec in enumerate(fleet_collects, 1):
        label = "cold-1" if index == 1 else fleet_labels.get(rec["index"], "warm-unchanged")
        passes.append(_pass_row(index, label, rec, "fleet"))
    checks = {"producer": shim_pass.self_check(producer, "producer"),
              "fleet": shim_pass.self_check(fleet, "fleet")}
    failures = checks["producer"]["failures"] + checks["fleet"]["failures"]
    out = []
    for metric, totals, segs in (("T1", t1_total, {k: {n: summarize(v) for n, v in s.items()}
                                                   for k, s in seg_samples.items()}),
                                 ("T2", t2_total, {k: {n: summarize(v) for n, v in s.items()}
                                                   for k, s in t2_seg.items()})):
        out.append({"metric": metric, "events": {k: summarize(v) for k, v in totals.items()},
                    "segments": segs})
    unresolved = [{"what": f"{e['kind']}#{e['seq']} T1", "reason": "no publish seen in time"}
                  for e in injected if "detect_ns" not in e]
    for ev in injected:
        if not any(ev["t_ns"] <= r["t0"] for r in fleet_collects):
            unresolved.append({"what": f"{ev['kind']}#{ev['seq']} T2", "reason": "no collect_all started after it"})
    notes.append("latency starts at the completed write; T1 includes the 25 ms host debounce constant "
                 "read from host.cjs and the panel_projection call; the host's own paint is not measured.")
    notes.append("T1 publish is the first projection.json whose observed_at is at or after the event "
                 "(answer-wait also requires its item id in the file).")
    notes.append("T2 ends at the _explorer_compose that starts after the first collect_all that started "
                 "after the event; the content of that snapshot is not re-read.")
    return {
        "metric": "T12", "results": out, "passes": passes,
        "events": [dict(e) for e in injected],
        "warm": warm, "self_check": {"checks": checks, "failures": failures},
        "shim_mode": shim_pass.shim_mode(producer_log),
        "notes": notes, "not_measured": not_measured + unresolved,
        "min_gap_s": min_gap, "debounce_s": debounce,
    }


def _median(values):
    data = sorted(values)
    if not data:
        return None
    mid = len(data) // 2
    return data[mid] if len(data) % 2 else (data[mid - 1] + data[mid]) / 2


def measure_replace(fixture: dict, *, scratch: Path, warm_passes: int = 3, timeout: float = 400.0) -> dict:
    """Warm unchanged passes versus the pass right after a whole-registry replacement.

    Open routes and sessions stay as they are, no event is injected apart from
    the replacement; both children run as in ``measure``. The result carries the
    median warm duration, the after-replace duration and the extra the replacement
    adds, for the producer and for the Fleet collect, with their registry metrics.
    """
    children = Children(fixture, scratch)
    after_t = None
    try:
        children.start_producer()
        children.start_fleet()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            producer = shim_pass.read_passes(children.producer_log)
            fleet = [r for r in shim_pass.read_passes(children.fleet_log) if r["root"] == "__init__.collect_all"]
            if len(producer) >= 1 + warm_passes and len(fleet) >= 1 + 5:
                break
            time.sleep(0.5)
        time.sleep(0.3)
        event = events.registry_replace(fixture, 0)
        after_t = event["t_ns"]
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            producer = shim_pass.read_passes(children.producer_log)
            fleet = [r for r in shim_pass.read_passes(children.fleet_log) if r["root"] == "__init__.collect_all"]
            if (any(r["t0"] >= after_t for r in producer)
                    and any(r["t0"] >= after_t for r in fleet)):
                break
            time.sleep(0.2)
        producer = shim_pass.read_passes(children.producer_log)
        fleet = [r for r in shim_pass.read_passes(children.fleet_log) if r["root"] == "__init__.collect_all"]
    finally:
        children.close()
    out = {"rows": fixture.get("rows"), "registry_bytes": fixture.get("bytes"), "roles": {}}
    for role, rows in (("producer", producer), ("fleet", fleet)):
        before = [r for r in rows if r["t1"] <= after_t][1:]  # drop the cold pass
        after = next((r for r in rows if r["t0"] >= after_t), None)
        warm = [(r["t1"] - r["t0"]) / 1e9 for r in before]
        entry = {"warm_passes": len(warm), "warm_median_s": _median(warm),
                 "warm_samples_s": [round(v, 4) for v in warm]}
        if after is not None:
            dur = (after["t1"] - after["t0"]) / 1e9
            entry.update(after_replace_s=round(dur, 4), extra_s=round(dur - (entry["warm_median_s"] or 0), 4),
                         after_row=_pass_row(0, "after-replace", after, role),
                         warm_row=_pass_row(0, "warm-unchanged", before[-1], role) if before else None)
            if entry["warm_median_s"] is not None:
                entry["warm_median_s"] = round(entry["warm_median_s"], 4)
        out["roles"][role] = entry
    out["self_check"] = {"producer": shim_pass.self_check(producer, "producer"),
                         "fleet": shim_pass.self_check(
                             [dict(r) for r in shim_pass.read_passes(children.fleet_log)], "fleet")}
    return out
