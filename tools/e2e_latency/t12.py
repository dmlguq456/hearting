"""T1 and T2: a route or worker value changes -> shell panel backend / Fleet screen model.

One producer child (``utilities/human_wait_state.py --refresh-cache``, the real
loop) and one Fleet child (``tools/fleet/fleet.py --explorer --no-tmux`` on a PTY)
run against the same fixture with the pass shim. After one cold pass and two
unchanged warm passes, events are injected through the real writers
(``events.py``) and each is followed to the moment *its value is shown*:

T1  event -> (producer wake -> pass -> publish) -> the first ``projection.json`` that
    carries the value (``visible.t1``) -> host debounce -> ``session_shell.panel_projection``.
T2  event -> the first Fleet ``_explorer_compose`` whose screen model carries the value
    (``visible.t2``, from the digest the shim takes after each compose).

A value that never shows within the wait is ``not_visible``: it gets no time, and every
table says how many of the injected events were seen (``n_visible / n``).

Phases. Events that change the registry (route-start, stage-transition, route-end) wake
the producer, so they are injected 0.3-4.2 s after the last publish (around its 2 s
registry min gap). Events outside the registry (worker-tool, answer-wait, owner-dead) wake
nothing; they wait for the producer's own cadence, so their phase is the variable: one
producer cycle per rep carries one event of each kind at seeded, stratified phases over
the whole cadence (``slow_phase_plan``), and every sample records its phase.

Latency is measured from the *completed write*. Nothing is mocked in the timed
path; the only substitution is session discovery (fixture sleepers, see shim).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import random
import re
import signal
import subprocess
import sys
import threading
import time

from . import events, fixture as fx, shim_pass, visible

REPO = fx.REPO
PHASES_S = (0.3, 1.7, 3.1, 0.9, 2.4, 4.2, 1.2, 2.8)  # registry-change events: after the last publish, around the 2 s min gap
PHASE_SEED = 20261113  # fixed: the registry-outside phase plan reproduces
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


def summarize(samples, total: int | None = None) -> dict:
    """``n`` is the samples that arrived; ``n_total`` (when given) is how many events were injected."""
    values = [v for v in samples if v is not None]
    out = {"n": len(values), "median_s": None, "p95_s": None, "max_s": None,
           "samples_s": [round(v, 4) for v in values]}
    if total is not None:
        out.update(n_total=total, n_not_visible=max(0, total - len(values)))
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
            {"harness": s.get("harness", "claude"), "pid": s["pid"], "pid_start": s["pid_start"], "cwd": s["cwd"]}
            for s in list(fixture["sessions"]) + list(fixture.get("codex_sessions") or ())]), encoding="utf-8")
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
        "    sid, pid, start, needle = line.split()\n"
        "    t0 = time.monotonic_ns()\n"
        "    try:\n"
        "        out = session_shell.panel_projection('claude', sid, native_pid=int(pid), native_start=start,\n"
        "                                             columns=120, rows=40, state='idle')\n"
        "        err = None\n"
        "    except Exception as exc:\n"
        "        out, err = {}, repr(exc)\n"
        "    t1 = time.monotonic_ns()\n"
        "    contains = None if needle == '-' else needle in json.dumps(out, default=str)\n"
        "    print(json.dumps({'dur_s': (t1 - t0) / 1e9, 'error': err, 't1': t1, 'contains': contains}), flush=True)\n"
    ) % (str(REPO / "utilities"), str(REPO / "tools"))

    def __init__(self, fixture: dict, shim_dir: str):
        env = fx.child_env(fixture)
        self.proc = subprocess.Popen([sys.executable, "-c", self.SCRIPT], env=env, cwd=fixture["root"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, start_new_session=True)

    def ask(self, session: dict, needle: str | None = None) -> dict:
        self.proc.stdin.write(f"{session['session_id']} {session['pid']} {session['pid_start']} {needle or '-'}\n")
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


def _read_cache(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def producer_cadence_s() -> float:
    """The producer's idle sleep (``delay = min(30.0, ...)``) read from its own source."""
    try:
        text = (REPO / "utilities" / "human_wait_state.py").read_text(encoding="utf-8")
        return float(re.search(r"delay = min\(\s*([0-9.]+)\s*,\s*max\(1\.0,\s*600\.0 - idle\)\)", text).group(1))
    except (OSError, AttributeError, ValueError):
        return 30.0


# Events outside the registry: a worker stream line, an answer-wait signal and an owner process
# ending change nothing the producer's registry-generation wake watches, so they wait for its cadence.
SLOW_KINDS = ("worker-tool", "answer-wait", "owner-dead")


def slow_phase_plan(kinds, reps: int, period_s: float, seed: int = PHASE_SEED) -> dict:
    """Phase (seconds after a publish) of every ``(rep, kind)`` of the registry-outside events.

    Stratified per kind: its ``reps`` samples take ``reps`` equal slices of ``[0, period)`` in a
    seeded random order, one random point inside each slice, so the phases cover the whole
    cadence evenly and the same seed gives the same plan.
    """
    rng = random.Random(seed)
    plan = {}
    for kind in kinds:
        order = list(range(reps))
        rng.shuffle(order)
        for rep in range(reps):
            plan[(rep, kind)] = round((order[rep] + rng.random()) / reps * period_s, 3)
    return plan


def _sleep_for(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(0.05, max(0.0, end - time.monotonic())))


def _inject(fixture, kind, seq, rep, not_measured, cache: Path):
    """Write one event. The readers' current rows are taken first, so an "is gone" event can
    tell a route it never showed from one that went away."""
    projection = _read_cache(cache) or {}
    try:
        ev = events.inject(fixture, kind, seq)
    except Exception as exc:  # a writer that cannot run is reported, not hidden
        not_measured.append({"what": f"{kind}#{seq}", "reason": f"{type(exc).__name__}: {exc}"})
        return None
    expect = ev.setdefault("expect", {})
    # The baseline is the projection read just before the write; the Fleet side is filled by the
    # caller from the last digest it has (see ``_stamp_fleet_baseline``).
    expect["was_present"] = visible.route_shown(projection, expect) if "route_id" in expect else None
    ev.update(seq=seq, rep=rep)
    return ev


class PublishWatch:
    """Marks each pending event when a ``projection.json`` publish carries its value.

    Polled on every wait, so an event is stamped when its publish happens, not when the
    caller next looks. A publish counts only if ``visible.t1`` finds the event's value in it
    (route row, stage, tool, item id, owner state): a write the reader does not recognise is
    never "seen", however many publishes follow it.
    """

    def __init__(self, cache: Path):
        self.cache, self.last, self.pending = cache, _stat_ns(cache), []
        self.last_publish_ns: int | None = None

    def add(self, ev: dict) -> None:
        self.pending.append(ev)
        self._check(time.monotonic_ns())  # a publish may already hold it

    def _check(self, now: int) -> None:
        if not self.pending:
            return
        value = _read_cache(self.cache)
        if value is None:
            return
        for ev in list(self.pending):
            if visible.t1(ev, value):
                ev["detect_ns"] = now
                ev["detect_observed_at"] = value.get("observed_at")
                self.pending.remove(ev)

    def poll(self) -> None:
        stamp = _stat_ns(self.cache)
        if stamp is None or stamp == self.last:
            return
        self.last = stamp
        now = time.monotonic_ns()
        self.last_publish_ns = now
        self._check(now)

    def run(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.poll()
            time.sleep(0.002)

    def run_until_ns(self, target_ns: int) -> None:
        while time.monotonic_ns() < target_ns:
            self.poll()
            time.sleep(0.002)

    def until_done(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while self.pending and time.monotonic() < end:
            self.poll()
            time.sleep(0.002)

    def wait_publish(self, timeout: float) -> bool:
        """Block until a publish newer than this call is seen."""
        before, end = self.last_publish_ns, time.monotonic() + timeout
        while time.monotonic() < end:
            self.poll()
            if self.last_publish_ns != before:
                return True
            time.sleep(0.002)
        return False


def _ask(probe, fixture, ev) -> None:
    """The host's side: one ``panel_projection`` answer for the event's session.

    Its duration is the host's cost; its content is checked for the event's own id/tool name
    where it has one (``host_shows``), so a panel that never shows the value is visible here.
    """
    if "detect_ns" not in ev:
        return
    expect = ev.get("expect") or {}
    session = next((s for s in fixture["sessions"] if s["session_id"] == expect.get("session_id")),
                   fixture["sessions"][ev["seq"] % len(fixture["sessions"])])
    needle = {"route-start": expect.get("route_id"), "worker-tool": expect.get("tool"),
              "answer-wait": expect.get("item_id")}.get(ev["kind"])
    answer = probe.ask(session, needle)
    ev["projection_s"] = answer["dur_s"]
    ev["projection_error"] = answer.get("error")
    ev["host_shows"] = answer.get("contains")


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


def _fleet_arrival(composes: list[dict], ev: dict):
    """The first ``_explorer_compose`` that ends after the event with the event's value in its model."""
    for rec in composes:
        if rec["t1"] >= ev["t_ns"] and visible.t2(ev, rec.get("model_now")):
            return rec
    return None


def _fleet_baseline(fleet: list[dict], ev: dict) -> bool | None:
    """Was the route shown in the Fleet model just before the write (for "is gone" events)?"""
    expect = ev.get("expect") or {}
    if "route_id" not in expect:
        return None
    before = [r for r in shim_pass.with_models(fleet)
              if r["root"] == "render._explorer_compose" and r["t1"] <= ev["t_start_ns"]]
    digest = before[-1].get("model_now") if before else None
    return bool(digest and expect["route_id"] in (digest.get("routes") or {}))


def measure(fixture: dict, *, reps: int, scratch: Path, kinds=None, warm_timeout: float = 300.0,
            event_timeout: float = EVENT_TIMEOUT_S) -> dict:
    """Run T1 and T2 against ``fixture``; the result follows the orchestrator contract."""
    kinds = list(kinds or events.KINDS)
    min_gap = registry_min_gap()
    cadence = producer_cadence_s()
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
                phase = PHASES_S[seq % len(PHASES_S)]
                watch.run(phase)  # phase after the last publish
                ev = _inject(fixture, kind, seq, rep, not_measured, cache)
                if ev is None:
                    continue
                ev["phase_s"] = phase
                injected.append(ev)
                watch.add(ev)
                watch.until_done(event_timeout)
                _ask(probe, fixture, ev)
        if slow:
            # Registry-outside events wait for the producer's own cadence, so their phase is the
            # variable: one producer cycle per rep carries one event of each kind, at seeded,
            # stratified phases over the whole cadence (see ``slow_phase_plan``).
            plan = slow_phase_plan(slow, reps, cadence)
            watch = PublishWatch(cache)
            watch.wait_publish(cadence + 30.0)
            cycle = []
            for rep in range(reps):
                anchor = watch.last_publish_ns or time.monotonic_ns()
                batch = []
                for kind in sorted(slow, key=lambda k: plan[(rep, k)]):
                    watch.run_until_ns(anchor + int(plan[(rep, kind)] * 1e9))
                    seq += 1
                    ev = _inject(fixture, kind, seq, rep, not_measured, cache)
                    if ev is None:
                        continue
                    ev.update(phase_target_s=plan[(rep, kind)],
                              phase_s=round((ev["t_ns"] - anchor) / 1e9, 3), anchor_ns=anchor)
                    batch.append(ev)
                    injected.append(ev)
                    watch.add(ev)
                watch.until_done(cadence + 30.0)
                cycle.extend(batch)
                if watch.pending or watch.last_publish_ns == anchor:
                    watch.wait_publish(cadence + 5.0)
            for ev in cycle:
                _ask(probe, fixture, ev)
        # The Fleet keeps going at its own cadence: give the last events time to show.
        tail_end = time.monotonic() + FLEET_TAIL_S
        while time.monotonic() < tail_end:
            fleet = shim_pass.with_models(shim_pass.read_passes(children.fleet_log))
            composes = [r for r in fleet if r["root"] == "render._explorer_compose"]
            if injected and all(_fleet_arrival(composes, e) is not None for e in injected):
                break
            time.sleep(0.5)
        producer = shim_pass.read_passes(children.producer_log)
        fleet = shim_pass.read_passes(children.fleet_log)
    finally:
        if probe is not None:
            probe.close()
        children.close()
    for ev in injected:
        ev["expect"]["was_present_fleet"] = _fleet_baseline(fleet, ev)
    return _assemble(fixture, kinds, injected, producer, fleet, min_gap, debounce, notes,
                     not_measured, warm, children.producer_log, cadence=cadence, event_timeout=event_timeout)


def _bin_counts(phases: list[float], period: float, width: float = 5.0) -> dict:
    out: dict[str, int] = {}
    edge = 0.0
    while edge < period:
        out[f"{edge:g}-{min(edge + width, period):g}"] = 0
        edge += width
    for value in phases:
        key = f"{(min(value, period - 1e-9) // width) * width:g}-{min(((value // width) + 1) * width, period):g}"
        if key in out:
            out[key] += 1
    return out


def _assemble(fixture, kinds, injected, producer, fleet, min_gap, debounce, notes, not_measured, warm,
              producer_log=None, cadence: float = 30.0, event_timeout: float = EVENT_TIMEOUT_S) -> dict:
    t1_total: dict[str, list] = {k: [] for k in kinds}
    t2_total: dict[str, list] = {k: [] for k in kinds}
    seg_samples: dict[str, dict[str, list]] = {k: {} for k in kinds}
    t2_seg: dict[str, dict[str, list]] = {k: {} for k in kinds}
    totals = {k: sum(1 for e in injected if e["kind"] == k) for k in kinds}
    publisher_passes: dict[int, str] = {}
    fleet_collects = [r for r in fleet if r["root"] == "__init__.collect_all"]
    fleet_composes = [r for r in shim_pass.with_models(fleet) if r["root"] == "render._explorer_compose"]
    outside = []
    unresolved = []
    for ev in injected:
        kind = ev["kind"]
        record = None
        if "detect_ns" in ev:
            publish = _publisher_pass(producer, ev["detect_ns"])
            if publish is not None:
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
                if kind in SLOW_KINDS:
                    last_pass = [p for p in producer if p["t0"] <= ev["t_ns"]]
                    gap = (publish["t0"] - previous[-1]["t1"]) / 1e9 if previous else None
                    record = {"kind": kind, "rep": ev.get("rep"), "phase_s": ev.get("phase_s"),
                              "phase_target_s": ev.get("phase_target_s"),
                              "since_pass_start_s": round((ev["t_ns"] - last_pass[-1]["t0"]) / 1e9, 3) if last_pass else None,
                              "total_s": round(total, 4),
                              "cadence_wait_s": round(max(0.0, cadence - (ev.get("phase_s") or 0.0)), 3),
                              # the publish came sooner than the idle sleep: another registry change woke the producer
                              "woken_early": bool(gap is not None and gap < cadence - 1.0),
                              "host_shows": ev.get("host_shows")}
                    outside.append(record)
        else:
            unresolved.append({"what": f"{kind}#{ev['seq']} T1",
                               "reason": f"not_visible: its value was not in projection.json within the wait "
                                         f"({(event_timeout if kind not in SLOW_KINDS else cadence + 30.0):g}s)"})
        arrival = _fleet_arrival(fleet_composes, ev)
        if arrival is not None:
            ev["t2_arrival_t1"] = arrival["t1"]
            collect = [r for r in fleet_collects if r["t1"] <= arrival["t0"]]
            collect = collect[-1] if collect else None
            t2_total[kind].append((arrival["t1"] - ev["t_ns"]) / 1e9)
            t2_seg[kind].setdefault("compose_s", []).append((arrival["t1"] - arrival["t0"]) / 1e9)
            if collect is not None:
                t2_seg[kind].setdefault("wait_for_collect_s", []).append(max(0.0, (collect["t0"] - ev["t_ns"]) / 1e9))
                t2_seg[kind].setdefault("collect_s", []).append((collect["t1"] - collect["t0"]) / 1e9)
                t2_seg[kind].setdefault("compose_wait_s", []).append((arrival["t0"] - collect["t1"]) / 1e9)
        else:
            unresolved.append({"what": f"{kind}#{ev['seq']} T2",
                               "reason": "not_visible: its value was not in the Fleet screen model within the wait"})
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
    for metric, samples, segs in (("T1", t1_total, {k: {n: summarize(v) for n, v in s.items()}
                                                    for k, s in seg_samples.items()}),
                                  ("T2", t2_total, {k: {n: summarize(v) for n, v in s.items()}
                                                    for k, s in t2_seg.items()})):
        out.append({"metric": metric, "events": {k: summarize(v, total=totals.get(k)) for k, v in samples.items()},
                    "segments": segs})
    outside_group = None
    if outside:
        phases = [r["phase_s"] for r in outside if r["phase_s"] is not None]
        residual = [r["total_s"] - r["cadence_wait_s"] for r in outside]
        outside_group = {
            "mechanism": "registry 밖 사건 (worker stream 줄, 답 대기 signal, owner 종료): producer 주기 대기",
            "cadence_s": cadence, "n": len(outside),
            "n_total": sum(totals.get(k, 0) for k in SLOW_KINDS),
            "total_s": summarize([r["total_s"] for r in outside]),
            "phase_s": summarize(phases),
            "phase_bins_5s": _bin_counts(phases, cadence),
            "residual_after_cadence_wait_s": summarize(residual),
            "woken_early": sum(1 for r in outside if r["woken_early"]),
            "host_shows": {"true": sum(1 for r in outside if r["host_shows"] is True),
                           "false": sum(1 for r in outside if r["host_shows"] is False),
                           "unchecked": sum(1 for r in outside if r["host_shows"] is None)},
            "samples": outside}
    notes.append("latency starts at the completed write; T1 includes the 25 ms host debounce constant "
                 "read from host.cjs and the panel_projection call; the host's own paint is not measured.")
    notes.append("T1 arrival is the first projection.json that carries the event's value (route row with its "
                 "owner attempt, new stage, tool name, closed outcome, item id, owner no longer working); "
                 "a write the reader does not show is `not_visible`, never given a time.")
    notes.append("T2 arrival is the first _explorer_compose whose screen model (digest taken by the shim "
                 "after the compose) carries the same value.")
    notes.append(f"registry-outside events are placed at seeded, stratified phases over the producer's "
                 f"{cadence:g}s cadence (phase = seconds after the last publish).")
    return {
        "metric": "T12", "results": out, "passes": passes, "outside_registry": outside_group,
        "events": [{k: v for k, v in e.items() if k != "anchor_ns"} for e in injected],
        "warm": warm, "self_check": {"checks": checks, "failures": failures},
        "shim_mode": shim_pass.shim_mode(producer_log),
        "notes": notes, "not_measured": not_measured + unresolved,
        "min_gap_s": min_gap, "debounce_s": debounce, "cadence_s": cadence,
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
