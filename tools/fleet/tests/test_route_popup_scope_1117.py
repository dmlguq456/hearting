"""BC-ROUTE-POPUP-SLOW-1117 — a one-session collection shows what a full one shows, with less read.

The route popup asks `collect_all(route_sessions=[(harness, sid)])`. That pass now defers the
sub-agent transcript tail to the rows that survive the session filter, leaves the supervision
census alone, and normalizes only the experiment runs the shown routes or the session's project
can use. The route popup and the STATUS popup's Fleet lines built from it must equal the ones
built from a full collection. Only native observation backends are
stubbed; Claude enrichment, row selection, route projection and resource normalization run.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import dataclasses
import json
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "utilities"))

import fixture_processes  # noqa: E402
import resource_run_registry  # noqa: E402
import session_shell as shell  # noqa: E402
from fleet import collectors, route  # noqa: E402
from fleet.collectors import claude, codex, dispatch, herdr, liveness, opencode, steward, tmux  # noqa: E402
from fleet.explorer import live  # noqa: E402
from fleet.model import Session  # noqa: E402
from fleet.refresh import as_snapshot  # noqa: E402

SID_A, SID_B = "sid-popup-a", "sid-popup-b"


def _tool_use(tid):
    return {"type": "assistant", "isSidechain": False, "timestamp": "2026-11-17T10:00:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": "Agent", "input": {"subagent_type": "explore"}}]}}


def _prompt(text):
    return {"type": "user", "isSidechain": False, "timestamp": "2026-11-17T09:59:00Z",
            "message": {"role": "user", "content": text}}


class ScopedCollectionFixture(unittest.TestCase):
    harness = "claude"

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        # A popup asks for the board's producer (`human_wait_state --refresh-cache`), started detached with this
        # root's state folder in its environment; it keeps writing `state/hearting/human-waits` for minutes.
        # The fixture's root is removed only after the processes that carry it are gone (CI #1911, shard 3).
        self.root = root = Path(stack.enter_context(fixture_processes.TemporaryDirectory()))
        self.work = root / "work"
        self.work.mkdir()
        # Each folder is its own repository root, so the project test does not
        # depend on whether some parent of the temp dir (e.g. /tmp) is a repository.
        (self.work / ".git").mkdir()
        self.jobs = root / "jobs.log"
        self.index = root / "resource-index.json"
        self.config = root / "claude"
        stack.enter_context(mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(root / "state"), "HOME": str(root / "home"),
            "AGENT_DISPATCH_JOBS": str(self.jobs), "AGENT_RESOURCE_RUN_INDEX": str(self.index),
            "CLAUDE_CONFIG_DIR": str(self.config), "TERM": "xterm-256color"}))
        stack.enter_context(mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(root / "artifacts")}))
        self.build_route()
        self.build_transcripts()
        self.build_resource_runs()
        self.sessions = [
            Session(harness=self.harness, pid=os.getpid(), session_id=SID_A, cwd=str(self.work), liveness="idle"),
            Session(harness="claude", pid=os.getpid() + 1, session_id=SID_B, cwd=str(self.work), liveness="idle"),
        ]
        # Only native observation backends are stubbed.
        stack.enter_context(mock.patch.object(collectors.procscan, "scan",
            side_effect=lambda **kwargs: copy.deepcopy(self.sessions)))
        for module in (tmux, codex, opencode):
            stack.enter_context(mock.patch.object(module, "enrich", return_value=None))
        stack.enter_context(mock.patch.object(herdr, "enrich", return_value={"complete": True, "agents": []}))
        stack.enter_context(mock.patch.object(dispatch, "_scan_processes", return_value=[]))
        stack.enter_context(mock.patch.object(dispatch, "_dispatch_liveness", return_value="working"))
        stack.enter_context(mock.patch.object(liveness, "classify", return_value="idle"))

    def build_route(self):
        record = {"schema_version": 1, "nodes": [{"id": "execute", "depends_on": []}],
                  "cwd": str(self.work), "artifact_root": str(self.root / "artifacts"),
                  "capability": "autopilot-code", "owner_attempt_id": "att-popup"}
        record["route_hash"] = route.route_hash(record)
        record["route_id"] = self.route_id = "rt-" + record["route_hash"].split(":")[1][:16]
        path = self.root / "route.json"
        path.write_text(json.dumps(record))
        meta = {"attempt_id": "att-popup", "parent_sid": SID_A, "parent_harness": self.harness,
                "harness": self.harness, "capability": "autopilot-code", "worker_type": "owner",
                "dispatch_depth": "1", "route_node": "execute", "route_id": record["route_id"],
                "route_hash": record["route_hash"], "route_file": str(path),
                "owner_route_id": record["route_id"], "owner_route_hash": record["route_hash"],
                "owner_route_file": str(path)}
        self.jobs.write_text(f"{time.time()}\trunning\trepo\t{self.work}\towner\t"
                             + ",".join(k + "=" + v for k, v in meta.items()) + "\n")

    def build_transcripts(self):
        proj = self.config / "projects" / claude._enc_cwd(str(self.work))
        proj.mkdir(parents=True)
        (proj / (SID_A + ".jsonl")).write_text(
            "\n".join(json.dumps(line) for line in (_prompt("a"), _tool_use("tool-a"))) + "\n")
        (proj / (SID_B + ".jsonl")).write_text(
            "\n".join(json.dumps(line) for line in (_prompt("b"), _tool_use("tool-b"))) + "\n")
        self.transcripts = {SID_A: str(proj / (SID_A + ".jsonl")), SID_B: str(proj / (SID_B + ".jsonl"))}

    def build_resource_runs(self):
        identity = resource_run_registry.proc_identity(os.getpid())
        base = {**identity, "cwd": str(self.work), "status": "running"}
        other = self.root / "other-project"
        other.mkdir()
        (other / ".git").mkdir()
        registry = self.root / "resource-runs.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {
            "by-attempt": {**base, "parent_attempt_id": "att-popup"},
            "by-route": {**base, "route_id": self.route_id},
            # Another job's run that finished in this session's project: the STATUS
            # popup's `changed:` line shows it as the project's newest result.
            "same-project-done": {**identity, "pid": 2 ** 22 + 7, "cwd": str(self.work), "status": "exited",
                                  "exit_code": 0, "ended_at": time.time() - 60,
                                  "parent_attempt_id": "att-elsewhere", "route_id": "rt-elsewhere"},
            "unrelated": {**base, "cwd": str(other), "parent_attempt_id": "att-elsewhere",
                          "route_id": "rt-elsewhere"},
        }}))
        resource_run_registry.register_registry(registry, self.index)

    def collect(self, scoped):
        return collectors.collect_all(
            usage="cache-only", include_report_verification=False,
            route_sessions=[(self.harness, SID_A)] if scoped else None)

    def popup_pair(self, request):
        binding = {"harness": self.harness, "sid": SID_A, "native_pid": 2, "native_start": "1"}
        out = []
        for scoped in (True, False):
            def view(harness, sid, kind, scoped=scoped):
                return live.build_view_model(as_snapshot(self.collect(scoped)))
            with mock.patch.object(shell, "_popup_view_model", view):
                out.append(shell._panel_popup(binding, dict(request)))
        return out


class ScopedRoutePopupTest(ScopedCollectionFixture):
    def test_popup_rows_styles_hits_match_full_collection(self):
        for request in ({"kind": "routes", "columns": 100},
                        {"kind": "routes", "columns": 100, "open": "ROUTE"},
                        {"kind": "routes", "columns": 100, "open": "ROUTE", "page": True}):
            if request.get("open"):
                request["open"] = self.route_id
            scoped, full = self.popup_pair(request)
            self.assertTrue(scoped.get("ok", True), scoped)
            self.assertTrue(scoped["rows"], request)
            self.assertEqual(self.route_id in scoped["routes"], True)
            for key in ("rows", "styles", "hits", "routes"):
                self.assertEqual(scoped.get(key), full.get(key), (key, request))

    def test_status_popup_fleet_lines_match_full_collection(self):
        request = {"kind": "status", "columns": 100, "cwd": str(self.work), "routes": [], "part": "fleet"}
        scoped, full = self.popup_pair(request)
        self.assertTrue(scoped.get("ok", True), scoped)
        changed = [row for row in full.get("rows") or () if "changed:" in str(row)]
        self.assertTrue(changed and "resource same project done" in str(changed), full.get("rows"))
        for key in ("rows", "styles", "hits"):
            self.assertEqual(scoped.get(key), full.get(key), key)

    def test_scoped_pass_reads_only_the_requested_session_subagents(self):
        if self.harness != "claude":
            self.skipTest("sub-agent tail is the Claude row's")
        with mock.patch.object(claude, "_tail_subagents", wraps=claude._tail_subagents) as tail:
            scoped = self.collect(True)
        self.assertEqual({call.args[0] for call in tail.call_args_list}, {self.transcripts[SID_A]})
        with mock.patch.object(claude, "_tail_subagents", wraps=claude._tail_subagents) as tail:
            full = self.collect(False)
        self.assertEqual({call.args[0] for call in tail.call_args_list}, set(self.transcripts.values()))

        def subs(snapshot, sid):
            row = next(s for s in snapshot.sessions if s.session_id == sid)
            return [dataclasses.asdict(s) for s in (getattr(row, "subagents", None) or ())]
        self.assertTrue(subs(full, SID_A) or self.harness != "claude")
        self.assertEqual(subs(scoped, SID_A), subs(full, SID_A))

    def test_scoped_pass_leaves_supervision_census_alone(self):
        with mock.patch.object(steward, "_record_supervision", wraps=steward._record_supervision) as record:
            self.collect(True)
        self.assertEqual(record.call_count, 0)
        with mock.patch.object(steward, "_record_supervision", wraps=steward._record_supervision) as record:
            self.collect(False)
        self.assertGreaterEqual(record.call_count, 1)

    def test_scoped_pass_normalizes_only_related_resource_runs(self):
        def normalized(scoped):
            with mock.patch.object(resource_run_registry, "normalize_run",
                                   wraps=resource_run_registry.normalize_run) as normalize:
                snapshot = self.collect(scoped)
            return {call.args[0] for call in normalize.call_args_list}, snapshot
        scoped_ids, scoped = normalized(True)
        full_ids, full = normalized(False)
        self.assertEqual(scoped_ids, {"by-attempt", "by-route", "same-project-done"})
        self.assertEqual(full_ids, {"by-attempt", "by-route", "same-project-done", "unrelated"})
        self.assertEqual(scoped.observations["resource-runs"]["state"], full.observations["resource-runs"]["state"])
        self.assertEqual([r.run_id for r in scoped.resources], ["by-attempt", "by-route", "same-project-done"])
        job = next(j for j in scoped.jobs if j.attempt_id == "att-popup")
        full_job = next(j for j in full.jobs if j.attempt_id == "att-popup")
        self.assertEqual([r.run_id for r in job.resource_children], ["by-attempt"])
        self.assertEqual([r.run_id for r in job.resource_children], [r.run_id for r in full_job.resource_children])

    def test_keep_predicate_failure_reads_every_run(self):
        original = collectors._scoped_resource_keep

        def failing(sessions, jobs):
            with mock.patch("fleet.route_chain.session_lines", side_effect=RuntimeError("boom")):
                return original(sessions, jobs)
        with mock.patch.object(collectors, "_scoped_resource_keep", failing), \
             mock.patch.object(resource_run_registry, "normalize_run",
                               wraps=resource_run_registry.normalize_run) as normalize:
            self.collect(True)
        self.assertEqual({call.args[0] for call in normalize.call_args_list},
                         {"by-attempt", "by-route", "same-project-done", "unrelated"})

    def test_full_collection_still_reads_everything(self):
        snapshot = self.collect(False)
        self.assertEqual({r.run_id for r in snapshot.resources},
                         {"by-attempt", "by-route", "same-project-done", "unrelated"})


class ScopedRoutePopupCodexTest(ScopedRoutePopupTest):
    harness = "codex"


class ScopedRoutePopupOpenCodeTest(ScopedRoutePopupTest):
    harness = "opencode"


if __name__ == "__main__":
    unittest.main()
