#!/usr/bin/env python3
"""A running route stays in its session's panel, on the three harnesses (`harness_behavior_scenarios.py`; 11-27).

``panel-route-running``
    The `solo·standard` cell (`harness_behavior_cases_solo_review.py`): one real registered owner makes the work,
    starts its review node with the command its task gives, waits (the supervisor parks it) and is woken by the
    verdict. The fixture model holds the owner's first turn, the review's first turn and the woken owner's last
    turn for a while, so the route stays at each point: the owner working its own turn, the owner parked while
    its depth-2 worker runs, the owner woken, and the route closed. At each point the scenario reads, in the
    fixture's own environment, what the session panel's producer reads (``collect_all`` scoped to the session,
    then ``session_route_status``: the session's ``route_panels`` rows) and what Fleet reads (the full
    ``collect_all`` and its view model: OVERVIEW's current routes and the ROUTES rows). Pass: while the route
    runs, the session's panel has the route's row with its current stage at every point, and Fleet has it as a
    current route; after the close, the panel still has the finished row (its result) and Fleet no longer counts
    it as current.

The oracle is the two readers' own output plus the registry rows. Synthetic data stays under the fixture root.
"""
from __future__ import annotations

import json
from shlex import quote as shlex_quote
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import native_binary as base_native, wait_for  # noqa: E402
from harness_behavior_cases_b import fixture_python, session_id  # noqa: E402
from harness_behavior_cases_e import TERMINAL_STATES  # noqa: E402
import harness_behavior_cases_solo_review as solo  # noqa: E402
import harness_behavior_cases_unit_route as unit  # noqa: E402

# Read in the fixture environment: the panel producer's exact read, then Fleet's.
PROBE = r"""
import time
sys.path.insert(0, home)
from tools.fleet.collectors import collect_all, procscan
from tools.fleet.explorer import live
from tools.fleet.session_line import session_route_status
harness, sid, rid = args["harness"], args["sid"], args.get("route_id")
key = harness + "\0" + sid
# Only this fixture's processes are listed: another session's process (and the registry its environment names)
# on the same host is never read.
marker = ("HEARTING_FIXTURE_ROOT=" + os.environ["HEARTING_FIXTURE_ROOT"]).encode()
listed = procscan._ps_lines

def own(pid):
    try:
        with open("/proc/%s/environ" % pid, "rb") as handle:
            return marker in handle.read().split(b"\0")
    except OSError:
        return False

def fixture_lines():
    rows = listed()
    kept = [line for line in rows if line.split(None, 1)[:1] and own(line.split(None, 1)[0])]
    return procscan._CensusRows(kept, complete=getattr(rows, "census_complete", False),
                                reasons=getattr(rows, "census_reasons", ()))
procscan._ps_lines = fixture_lines

def job(j):
    ev = getattr(j, "state_evidence", None) or {}
    attempt = ev.get("attempt") if isinstance(ev.get("attempt"), dict) else ev
    return {"attempt": (getattr(j, "attempt_id", None) or "")[-8:], "depth": getattr(j, "dispatch_depth", None),
            "worker_type": getattr(j, "worker_type", None), "node": getattr(j, "route_node", None),
            "harness": getattr(j, "harness", None), "status": getattr(j, "status", None),
            "liveness": getattr(j, "liveness", None), "note": getattr(j, "note", None),
            "rule": (attempt or {}).get("rule"), "source": (attempt or {}).get("source"),
            "route": getattr(j, "route_id", None)}

def routes_of(snapshot, now):
    resolver = live._resolver(snapshot)
    rows, index = live._session_records(snapshot, now, resolver)
    routes, _nodes = live._route_records(snapshot, rows, index, resolver, now)
    return [{k: r.get(k) for k in ("id", "state", "current", "worker_stage", "session_tag", "activity", "outcome")}
            | {"stages": [(s.get("name"), s.get("st")) for s in r.get("stages") or ()]}
            for r in routes if not rid or r.get("id") == rid]

now = time.time()
out = {"at": now}
snap = collect_all(harness_filter={harness}, route_sessions=[(harness, sid)], include_report_verification=False)
out["panel_all_new"] = snap.observations.get("basic", {}).get("all_new")
labels, panels = session_route_status(snap, include_details=True)
out["panel_label"] = labels.get(key)
out["panel_rows"] = [{k: r.get(k) for k in ("id", "state", "worker_stage", "outcome")}
                     | {"stages": [(s.get("name"), s.get("st")) for s in r.get("stages") or ()]}
                     for r in panels.get(key) or () if not rid or r.get("id") == rid]
out["panel_routes"] = routes_of(snap, now)
out["panel_jobs"] = [job(j) for j in snap.jobs if not rid or getattr(j, "route_id", None) == rid]
full = collect_all(include_report_verification=False)
view = live.build_view_model(full, now=now)
counts, opened = live.current_route_counts(view.routes)
out["fleet_routes"] = [{k: r.get(k) for k in ("id", "state", "current", "worker_stage", "session_tag")}
                       for r in view.routes if not rid or r.get("id") == rid]
out["fleet_overview"] = {"counts": counts, "opened": sorted(opened)}
out["fleet_jobs"] = [job(j) for j in full.jobs if not rid or getattr(j, "route_id", None) == rid]
print(json.dumps(out, default=str))
"""


class PanelRouteRunning(solo.SoloReviewScenario):
    name = "panel-route-running"
    summary = ("A running route keeps its row and current stage in its session's panel and in Fleet while the owner "
               "works, while it is parked waiting for its depth-2 review, and when it is woken; after the close only "
               "the finished row remains.")
    HOLD = 75          # seconds each held turn stays held (the fixture's gate limit is 170)
    SAMPLES = 2        # reads per held point (at the start and near the end of the hold)

    def script(self, run):
        answer = super().script(run)
        cell = self
        cell.gates = {name: threading.Event() for name in ("owner-turn", "review-turn", "owner-woken")}
        cell.held = {name: threading.Event() for name in cell.gates}

        def held(record, earlier):
            reply = answer(record, earlier)
            text = record["text"]
            if "Dispatch metadata:" not in text:
                return reply
            if "- worker_type: review" in text:
                if record["last_kind"] != "tool" and not cell.held["review-turn"].is_set():
                    cell.held["review-turn"].set()
                    reply.gate = cell.gates["review-turn"]
                return reply
            if solo.WORK not in text:
                return reply
            steps = next((s for sid, s in cell.owner_steps.items() if not sid.endswith("-wrote")), [])
            if steps == ["work"] and not cell.held["owner-turn"].is_set():
                cell.held["owner-turn"].set()
                reply.gate = cell.gates["owner-turn"]
            elif steps and steps[-1] == "final" and not cell.held["owner-woken"].is_set():
                cell.held["owner-woken"].set()
                reply.gate = cell.gates["owner-woken"]
            return reply
        return held

    def prepare(self, run):
        super().prepare(run)
        # The installed CLIs word their local auth status differently from what the readiness probe accepts
        # (Codex 0.13x prints "API key configured" for the fixture's API key; Claude reports an API-key-only
        # profile as not logged in). A fixture-only shim answers those two status calls and runs the real
        # binary for everything else; the model stays the loopback fixture.
        shims = run.home.root / "auth-shim"
        shims.mkdir(exist_ok=True)
        answers = {"codex": ("login", "status", "Logged in using an API key - fixture"),
                   "claude": ("auth", "status", '{"loggedIn": true, "authMethod": "api_key"}')}
        for name, (first, second, text) in answers.items():
            native = base_native(name)
            if not native:
                continue
            shim = shims / name
            shim.write_text("#!/bin/sh\n"
                            f'if [ "$1" = {first} ] && [ "$2" = {second} ]; then echo {shlex_quote(text)}; exit 0; fi\n'
                            f'exec {shlex_quote(native)} "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
        run.home.env["PATH"] = str(shims) + ":" + run.home.env.get("PATH", "")

    def read(self, run, label, sid, route_id):
        value = fixture_python(run, PROBE, {"harness": run.harness, "sid": sid, "route_id": route_id}, timeout=120)
        rid = route_id
        panel = [r for r in value.get("panel_rows") or () if r.get("id") == rid] if rid else []
        fleet = [r for r in value.get("fleet_routes") or () if r.get("id") == rid] if rid else []
        sample = {"label": label, "at": round(time.time() - self.t0, 1),
                  "panel_row": bool(panel), "panel_stage": panel[0].get("worker_stage") if panel else None,
                  "panel_state": panel[0].get("state") if panel else None,
                  "fleet_current": any(r.get("current") is True for r in fleet),
                  "fleet_state": fleet[0].get("state") if fleet else None,
                  "probe": value}
        self.samples.append(sample)
        return sample

    def hold_point(self, run, label, gate, sid):
        """Read the readers while ``gate``'s turn is held, then release it."""
        route_id = self.route_id(run)
        start = time.time()
        for index in range(self.SAMPLES):
            if index:
                wait_for(lambda: False, max(0.0, self.HOLD - (time.time() - start)), 1)
            self.read(run, f"{label}#{index}", sid, route_id)
        self.gates[gate].set()

    def route_id(self, run):
        files = self.route_files(run)
        if not files:
            return None
        try:
            return json.loads(files[0].read_text(encoding="utf-8")).get("route_id")
        except (OSError, ValueError):
            return None

    def drive(self, run):
        try:
            return self.run_panel(run, run.evidence)
        finally:
            for gate in getattr(self, "gates", {}).values():
                gate.set()

    def run_panel(self, run, ev):
        self.samples, self.t0 = [], time.time()
        run.prompt(solo.START)
        sid = session_id(run, timeout=90)
        if not sid:
            run.note("the session's own id was never recorded")
            return "inconclusive"
        owner_rows = lambda: [r for r in self.unit_rows() if r.get("worker_type") == "owner"]  # noqa: E731
        review_rows = lambda: [r for r in self.unit_rows() if r.get("worker_type") == "review"]  # noqa: E731
        # Each point is read once the registry says it was reached, never after a fixed pause.
        reached = {"owner-turn": lambda: bool(owner_rows()),
                   "review-turn": lambda: bool(review_rows()),
                   "owner-woken": lambda: bool(review_rows()) and review_rows()[-1]["status"] in TERMINAL_STATES}
        for label, gate in (("owner-turn", "owner-turn"), ("parked-review-running", "review-turn"),
                            ("owner-woken", "owner-woken")):
            if not wait_for(lambda: self.held[gate].is_set() and reached[gate](), 240, 0.5):
                run.note(f"{label}: the held turn never arrived")
                break
            self.hold_point(run, label, gate, sid)
        done = wait_for(lambda: owner_rows() and owner_rows()[-1]["status"] in TERMINAL_STATES, self.WAIT, 1)
        route_files = self.route_files(run)
        outcome = route_files[0].with_name(route_files[0].stem + ".outcome.json") if route_files else None
        wait_for(lambda: outcome is not None and outcome.exists(), 60 if done else 5, 0.5)
        self.read(run, "closed", sid, self.route_id(run))
        ev["samples"] = [{k: v for k, v in s.items() if k != "probe"} for s in self.samples]
        ev["probes"] = {s["label"]: s["probe"] for s in self.samples}
        ev["owner_rows"] = [{k: r.get(k) for k in ("status", "route_node", "dispatch_depth", "harness", "note")}
                            for r in owner_rows()]
        ev["route_closed"] = bool(outcome and outcome.exists())
        running = [s for s in self.samples if s["label"] != "closed"]
        closed = [s for s in self.samples if s["label"] == "closed"]
        missing = [s["label"] for s in running if not (s["panel_row"] and s["panel_stage"] not in (None, "", "·"))]
        fleet_missing = [s["label"] for s in running if not s["fleet_current"]]
        run.note(f"panel rows missing at {missing or 'none'}; fleet current missing at {fleet_missing or 'none'}; "
                 f"closed={ev['route_closed']}")
        if not done or len(running) < 3 * self.SAMPLES or not closed:
            return "inconclusive"
        ended = closed[0]
        ok = (not missing and not fleet_missing and ended["panel_row"] and ended["panel_state"] == "done"
              and not ended["fleet_current"])
        return "pass" if ok else "fail"


SCENARIOS = (PanelRouteRunning,)
