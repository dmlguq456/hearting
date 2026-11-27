"""The window's panel shows the routes its old session started before `/bg` (display only, 11-27).

Claude's `/bg` leaves the window's process as a viewer of the old session id (OLD) while the
conversation goes on under a new id (NEW) in another process; the window's panel then reads NEW
(#1721). Fleet's pass already draws the two as one row that also answers to OLD (#1849), so the
panel producer files OLD's routes, which OLD still owns, under that one row: the window's panel
lists them as their owner's panel would, running or just finished, with no change of owner,
notice or authority. With one fact only (no link) the two rows stay apart and NEW's panel lists
only NEW's own routes.

The registry rows, the sealed route record, its outcome sidecar and the two processes' records
are real files, and the two processes are real child processes read after they signal they are
ready; the dispatch collector, the route reading, Fleet's route records, the parked merge, the
panel producer (``human_wait_state``) and the panel presentation run as in operation. Only the
native observation backends are held. Made-up ids only.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import json
import select
import subprocess
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from tools.fleet import collectors, route, session_registry as sr
from tools.fleet.collectors import claude, dispatch, herdr, liveness, steward, tmux
from tools.fleet.model import Session

OLD = "2d4f6a8c-1b3e-4c5d-8e7f-9a0b1c2d3e4f"     # the window's old session id
NEW = "6e8a0c2e-4f1a-4b3c-9d5e-7f8a9b0c1d2e"     # the background session that carries the conversation on
JOB = "4c6e8a0b"
NODES = ("plan", "execute", "impl-review")
HOLD = "import os; os.write(1, b'R'); os.read(0, 1)"


class ParkedPanelRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ppr-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.config = self.root / "home" / ".claude"
        (self.config / "sessions").mkdir(parents=True)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"), "HOME": str(self.root / "home"),
            "CLAUDE_CONFIG_DIR": str(self.config), "AGENT_DISPATCH_JOBS": str(self.jobs),
            "FLEET_SESSION_REGISTRY_DIR": str(self.root / "registry")}))
        for memory in (sr._STEADY, sr._STEADY_UNCONFIRMED):
            memory.clear()
            self.addCleanup(memory.clear)
        self.children = {}
        self.addCleanup(self._reap)
        self.records = {}
        self.lines = []
        self.live = {}           # attempt -> liveness the classifier answers for its row
        self.sessions = []
        self.stack.enter_context(mock.patch.object(
            collectors.procscan, "scan", side_effect=lambda **kwargs: copy.deepcopy(self.sessions)))
        for module in (claude, tmux, steward):
            self.stack.enter_context(mock.patch.object(module, "enrich", return_value=None))
        self.stack.enter_context(mock.patch.object(herdr, "enrich", return_value={"complete": True, "agents": []}))
        self.stack.enter_context(mock.patch.object(dispatch, "_scan_processes", return_value=[]))
        self.stack.enter_context(mock.patch.object(dispatch, "_terminal_row_process_alive", return_value=False))
        self.stack.enter_context(mock.patch.object(
            dispatch, "_dispatch_liveness", side_effect=lambda job, *a, **k: self.live.get(job.attempt_id, "done")))
        self.stack.enter_context(mock.patch.object(liveness, "classify", return_value="idle"))
        import human_wait_state
        self.stack.enter_context(mock.patch.object(human_wait_state, "_request_producer"))

    def _reap(self):
        for child in self.children.values():
            child.stdin.close()
            child.kill()
            child.wait(timeout=10)
        self.children = {}

    # -- the two processes and their records ----------------------------------------------------------
    def spawn(self, key, *argv):
        child = subprocess.Popen([sys.executable, "-c", HOLD, *argv],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.children[key] = child
        with child.stdout:      # /proc is read only once the child says it runs
            self.assertTrue(select.select([child.stdout], [], [], 10)[0], "fixture child did not start")
            self.assertEqual(child.stdout.read(1), b"R", "fixture child exited before it was ready")
        return child

    def record(self, key, sid, **fields):
        child = self.children[key]
        self.records[key] = (sid, fields)
        (self.config / "sessions" / ("%d.json" % child.pid)).write_text(json.dumps(
            {"pid": child.pid, "procStart": sr._proc_start(child.pid), "sessionId": sid, **fields}))

    def split(self, *, parked=True):
        """The window (viewer of OLD) and the background session (NEW), both live."""
        viewer = self.spawn("viewer", "--resume", OLD)
        self.record("viewer", OLD, kind="interactive", startedAt=1_700_000_000_000,
                    **({"parkedJobId": JOB} if parked else {}))
        held = self.spawn("held", "--session-id", NEW, "--fork-session", "--resume", "/h/p/%s.jsonl" % OLD)
        self.record("held", NEW, kind="bg", jobId=JOB, startedAt=1_700_000_500_000)
        self.sessions = [Session(harness="claude", pid=child.pid, session_id=sid, cwd=str(self.root),
                                 liveness="idle", proc_start=sr._proc_start(child.pid))
                         for child, sid in ((viewer, OLD), (held, NEW))]

    def end_link(self):
        """The window takes its own conversation back: its record holds no job."""
        sid, fields = self.records["viewer"]
        self.record("viewer", sid, **{k: v for k, v in fields.items() if k != "parkedJobId"})

    # -- routes ---------------------------------------------------------------------------------------
    def seal(self, slug):
        record = {"schema_version": 1, "cwd": str(self.root), "artifact_root": str(self.root / "artifacts"),
                  "capability": "autopilot-code", "capability_mode": "dev", "execution_topology": "staged",
                  "effective_intensity": "standard", "owner_attempt_id": "-", "slug": slug,
                  "nodes": [{"id": node, "depends_on": [NODES[i - 1]] if i else []} for i, node in enumerate(NODES)]}
        record["route_hash"] = route.route_hash(record)
        record["route_id"] = "rt-" + record["route_hash"].split(":")[1][:16]
        path = self.root / "artifacts" / ".runtime" / "routes" / (record["route_id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        return record, path

    def row(self, record, path, attempt, parent, status="running", *, owner=False, node=None, live="working"):
        rid, digest = record["route_id"], record["route_hash"]
        meta = {"attempt_schema_version": "2", "transport": "headless", "execution_surface": "registered-headless",
                "registered_worker": "1", "fallback_hop": "same-harness-headless",
                "attempt_id": attempt, "parent_sid": parent, "parent_harness": "claude", "harness": "codex",
                "capability": "autopilot-code", "route_id": rid, "route_hash": digest, "route_file": str(path)}
        if owner:
            meta.update(worker_type="owner", dispatch_depth="1", owner_route_id=rid, owner_route_hash=digest,
                        owner_route_file=str(path))
        else:
            meta.update(worker_type="stage", dispatch_depth="2", route_node=node,
                        parent_attempt_id="att-owner-" + record["slug"])
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.lines.append(f"{stamp}\t{status}\trepo\t{self.root}\t{attempt}\t"
                          + ",".join(k + "=" + v for k, v in meta.items()))
        self.live[attempt] = "done" if status == "done" else live

    def route(self, slug, parent, *, finished=False):
        """A staged route ``parent`` started: running at its execute stage, or finished (passed)."""
        record, path = self.seal(slug)
        if finished:
            self.row(record, path, "att-owner-" + slug, parent, "done", owner=True)
            for node in NODES:
                self.row(record, path, "att-%s-%s" % (node, slug), parent, "done", node=node)
            path.with_name(path.stem + ".outcome.json").write_text(json.dumps({
                "route_id": record["route_id"], "route_hash": record["route_hash"], "terminal_gate_proven": True,
                "closed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
        else:
            self.row(record, path, "att-owner-" + slug, parent, owner=True)
            self.row(record, path, "att-plan-" + slug, parent, "done", node="plan")
            self.row(record, path, "att-execute-" + slug, parent, node="execute")
        return record["route_id"]

    def panel(self, sid=NEW, columns=44):
        """The producer's pass for a panel that reads ``sid``, then that panel's ROUTE box."""
        self.jobs.write_text("\n".join(self.lines) + "\n")
        import human_wait_state as cache
        from tools.fleet.session_panel import presentation
        key = "claude\0" + sid
        with mock.patch.object(cache, "_route_demands", return_value={key: time.time()}), \
                mock.patch.object(cache, "_notify_result"):
            self.assertTrue(cache._refresh_cache())
        view = cache.cached_view(request=False) or {}
        label = cache.cached_statusline("claude", sid)[1]
        shown = presentation({"route_label": label}, view, "claude", sid, columns, now=time.time())
        return view, shown

    # -- what the window's panel shows ------------------------------------------------------------------
    def test_the_window_panel_lists_a_running_route_its_old_session_started(self):
        self.split()
        before = self.route("route-a", OLD)
        own = self.route("route-b", NEW)
        view, shown = self.panel()
        ids = [row["id"] for row in shown["routes"]]
        self.assertEqual(sorted(ids), sorted([before, own]))
        self.assertEqual((shown["route_count"], shown["route_total"]), (2, 2))
        # Display only: the route's owner and every recipient judgment stay with OLD.
        import dispatch_seat_handover as handover
        meta = next(dict(item.split("=", 1) for item in line.split("\t")[5].split(","))
                    for line in self.lines if "att-owner-route-a" in line)
        self.assertEqual(handover.effective_parent(meta, self.jobs), OLD)

    def test_a_finished_route_of_the_old_session_shows_its_result(self):
        self.split()
        done = self.route("route-a", OLD, finished=True)
        view, shown = self.panel()
        rows = [row for row in shown["routes"] if row["id"] == done]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "done")

    def test_without_the_link_the_background_panel_lists_only_its_own(self):
        self.split(parked=False)
        self.route("route-a", OLD)
        own = self.route("route-b", NEW)
        view, shown = self.panel()
        self.assertEqual([row["id"] for row in shown["routes"]], [own])

    def test_after_the_window_takes_its_conversation_back_each_panel_has_its_own(self):
        self.split()
        before = self.route("route-a", OLD)
        own = self.route("route-b", NEW)
        self.end_link()
        _view, new_panel = self.panel(NEW)
        _view, old_panel = self.panel(OLD)
        self.assertEqual([row["id"] for row in new_panel["routes"]], [own])
        self.assertEqual([row["id"] for row in old_panel["routes"]], [before])


if __name__ == "__main__":
    unittest.main()
