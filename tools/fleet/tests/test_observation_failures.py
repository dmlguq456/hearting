"""Fleet audit P3: failed observations never establish negative facts."""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import sys
import threading
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import collectors, details, herdr_projection, projection, render, session_tags
from fleet.collectors import dispatch, herdr, procscan, resource_runs, usage_cache
from fleet.model import DispatchJob, ResourceJob, Session
from fleet.refresh import LiveSnapshot


class PaneFailures(unittest.TestCase):
    def test_partial_probe_preserves_positive_match_and_unknown_nonmatch(self):
        panes = [{"pane_id": "good", "agent": "codex"},
                 {"pane_id": "failed", "agent": "claude"}]

        def runner(argv, **kwargs):
            if argv[-1] == "failed":
                raise TimeoutError("pane unavailable")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {
                "process_info": {"foreground_processes": [{"pid": 4321, "name": "codex"}]}}}))

        pids = herdr.pane_pids(panes, runner=runner)
        sessions = [Session(harness="codex", pid=4321),
                    Session(harness="claude", pid=9876),
                    Session(harness="claude", pid=9877, session_id="unmatched")]
        with mock.patch.object(herdr, "_ppid_of", return_value=1):
            herdr.enrich(sessions, agents=[], pids=pids)
        self.assertEqual([s.herdr_attached for s in sessions], [True, None, None])

    def test_fresh_pane_positive_survives_cached_peer_and_other_pane_failure(self):
        panes = [{"pane_id": "fresh", "agent": "codex"},
                 {"pane_id": "cached", "agent": "codex"},
                 {"pane_id": "failed", "agent": "claude"}]
        observation = SimpleNamespace(
            endpoint="freshness-test", socket_path="/tmp/herdr-freshness-test.sock",
            generation=9391, pane_revisions={"fresh": 1, "cached": 2, "failed": 3},
            complete=False)
        cache_key = (observation.endpoint, observation.socket_path, observation.generation,
                     observation.pane_revisions["cached"], "cached")
        herdr._PANE_INFO_CACHE[cache_key] = {
            "foreground_processes": [{"pid": 4555, "name": "codex"}]}
        self.addCleanup(herdr._PANE_INFO_CACHE.pop, cache_key, None)

        def runner(argv, **_kwargs):
            pane_id = argv[-1]
            if pane_id == "failed":
                raise TimeoutError("pane unavailable")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {
                "process_info": {"foreground_processes": [
                    {"pid": 4321, "name": "codex"}]}}}))

        with mock.patch.object(herdr.subprocess, "run", side_effect=runner):
            pids = herdr.pane_pids(
                panes, runner=herdr.subprocess.run, observation=observation)
        self.assertFalse(pids.complete)
        self.assertEqual(pids[1], {4321, 4555})
        self.assertEqual(pids.fresh_foreground, {4321})

        sessions = [Session(harness="codex", pid=4321),
                    Session(harness="codex", pid=4555),
                    Session(harness="claude", pid=9877, session_id="unmatched")]
        with mock.patch.object(herdr, "_ppid_of", return_value=1):
            herdr.enrich(sessions, agents=[], panes=panes, pids=pids)
        self.assertEqual([s.herdr_attached for s in sessions], [True, None, None])

        complete_observation = SimpleNamespace(
            endpoint=observation.endpoint, socket_path=observation.socket_path,
            generation=observation.generation, pane_revisions=observation.pane_revisions,
            complete=True)
        with mock.patch.object(herdr.subprocess, "run", side_effect=runner):
            complete_pids = herdr.pane_pids(
                [panes[1]], runner=herdr.subprocess.run, observation=complete_observation)
        self.assertTrue(complete_pids.complete)
        self.assertEqual(complete_pids.fresh_foreground, set())
        cached_session = Session(harness="codex", pid=4555)
        herdr.enrich([cached_session], agents=[], panes=[panes[1]], pids=complete_pids)
        self.assertIs(cached_session.herdr_attached, True)

    def test_pane_waits_overlap_in_one_bounded_observation(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def runner(argv, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.025)
            with lock:
                active -= 1
            raise TimeoutError("pane unavailable")

        herdr.pane_pids([{"pane_id": str(i), "agent": "codex"} for i in range(12)], runner=runner)
        self.assertGreater(maximum, 1)


class JobFailures(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for patch in (
            mock.patch.dict(os.environ, {"XDG_STATE_HOME": state.name}),
            mock.patch.object(session_tags, "refresh"),
            mock.patch.object(session_tags, "assign", return_value=[], create=True),
            mock.patch.object(herdr_projection, "_report"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_registry_candidates_propagate_inaccessible_paths(self):
        with mock.patch.object(dispatch.os, "stat", side_effect=PermissionError("NAS denied")):
            with self.assertRaises(PermissionError):
                dispatch._installed_registry_paths()
            with mock.patch.dict(dispatch.os.environ, {}, clear=True), \
                 mock.patch.object(dispatch, "_jobs_path", return_value="/audit/jobs.log"):
                with self.assertRaises(PermissionError):
                    dispatch._candidate_jobs_paths()
            with self.assertRaises(PermissionError):
                dispatch._validated_split_registry_paths(
                    [], session_rows=[Session(harness="codex", pid=1, session_id="parent")],
                    observed={"/audit/split.log": {"attempt"}})

    def test_first_failed_census_never_claims_no_work(self):
        with mock.patch.object(procscan, "scan", return_value=[]), \
             mock.patch.object(herdr, "enrich"), \
             mock.patch.object(usage_cache, "account_usage", return_value={}), \
             mock.patch.object(resource_runs, "collect", return_value=[]), \
             mock.patch.object(projection, "attach_projections"), \
             mock.patch.object(dispatch, "collect", side_effect=PermissionError("NAS denied")):
            failed = collectors.collect_all(jobs_path="/audit/p3/first-failure", fast_first=True)
        for process_view in (False, True):
            with self.subTest(process_view=process_view), \
                 mock.patch.object(render, "_PROCESS_VIEW", process_view), \
                 mock.patch.object(render, "_compute_host_rows", return_value=[]), \
                 mock.patch.object(render, "_fresh_compute_hosts", return_value=(None, None)):
                text = "\n".join(render._plain(line) for line in render._build_lines(
                    failed.sessions, failed.jobs, "both", False, 0,
                    observations=failed.observations, governor=None))
                self.assertIn('jobs.log unknown', text)
                self.assertNotIn("no active", text)
                self.assertNotIn("0 jobs", text)

    def test_registry_permission_error_is_not_an_empty_census(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fresh-jobs.log"
            path.write_text("1\trunning\t/repo\t/tmp/wt\tworker\t", encoding="utf-8")
            read_text = Path.read_text

            def denied(source, *args, **kwargs):
                if source == path:
                    raise PermissionError("NAS denied")
                return read_text(source, *args, **kwargs)

            with mock.patch.object(Path, "read_text", denied):
                with self.assertRaises(PermissionError):
                    dispatch._scan_jobs_log(path, set())
                with self.assertRaises(PermissionError):
                    dispatch._jobs_log_fields([path])

    def test_last_jobs_and_failure_are_one_publication_then_recover(self):
        owner = DispatchJob(key="owner", slug="audit-owner", attempt_id="audit-attempt",
                            harness="codex", liveness="working")
        with mock.patch.object(procscan, "scan", return_value=[]), \
             mock.patch.object(herdr, "enrich"), \
             mock.patch.object(usage_cache, "account_usage", return_value={}), \
             mock.patch.object(resource_runs, "collect", return_value=[]), \
             mock.patch.object(projection, "attach_projections"), \
             mock.patch.object(dispatch, "collect", side_effect=[[owner], RuntimeError("NAS denied"), []]):
            first = collectors.collect_all(jobs_path="/audit/p3/isolated", fast_first=True)
            failed = collectors.collect_all(jobs_path="/audit/p3/isolated", fast_first=True)
            recovered = collectors.collect_all(jobs_path="/audit/p3/isolated", fast_first=True)
        self.assertEqual([j.attempt_id for j in failed.jobs], ["audit-attempt"])
        self.assertIsNot(failed.jobs[0], first.jobs[0])
        self.assertIn("NAS denied", failed.observations["jobs"]["last_error"])
        self.assertEqual(failed.observations["jobs"]["state"], "failed")
        self.assertEqual(failed.jobs[0].liveness, "unknown")
        with mock.patch.object(render, "_PROCESS_VIEW", False):
            lines = render._build_lines(failed.sessions, failed.jobs, "both", False, 0,
                                        observations=failed.observations, governor=None)
        text = "\n".join(render._plain(line) for line in lines)
        self.assertIn('jobs.log unknown', text)
        self.assertIn("audit-owner", text)
        self.assertEqual(recovered.jobs, [])
        self.assertEqual(recovered.observations["jobs"]["state"], "idle")

    def test_resource_census_failure_blocks_cache_and_recovers_without_dropping_rows(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.view_model import ExplorerViewModel

        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            jobs_path = Path(td) / "jobs.log"
            for target, result in (
                ("fleet.collectors.procscan.scan", []),
                ("fleet.collectors.herdr.list_agents", []),
                ("fleet.collectors.herdr.enrich", None),
                ("fleet.collectors.usage_cache.account_usage", {}),
                ("fleet.projection.attach_projections", None),
                ("fleet.session_tags.refresh", None),
                ("fleet.session_tags.assign", []),
                ("fleet.herdr_projection._report", None),
                ("fleet.route_chain.enrich", None),
                ("fleet.collectors.peer_messages.collect", None),
            ):
                stack.enter_context(mock.patch(target, return_value=result))
            dispatch_reader = stack.enter_context(mock.patch.object(dispatch, "collect", return_value=[]))
            dispatch_reader.last_malformed = 0
            dispatch_reader.last_route_nodes = {}
            dispatch_reader.last_degradations = {}
            dispatch_reader.last_pending_delivery = None
            resource_reader = stack.enter_context(mock.patch.object(
                resource_runs, "collect",
                side_effect=[OSError("first resource read failed"), [],
                             OSError("resource read temporarily failed"),
                             [ResourceJob(run_id="resource-recovered", liveness="working")]],
            ))
            resource_reader.last_diagnostics = []

            runtime = ExplorerRuntime(Path(td) / "state", strict_readiness=True)
            runtime.configure_persistence({"codex"}, strict_readiness=True)
            vm = ExplorerViewModel.from_mapping({"identity": "resource-test", "clock": 100.0})

            def observe(generation):
                snapshot = collectors.collect_all(
                    harness_filter={"codex"}, jobs_path=str(jobs_path), fast_first=True)
                identity = snapshot.observations["basic"]["generation_identity"]
                snapshot.observations.update({
                    "required-details": {"state": "ok", "generation_identity": identity,
                                          "all_new": True},
                    "usage:codex": {"state": "absent", "attempted": True, "all_new": True},
                    "remote-host": {"state": "absent", "attempted": True},
                })
                _model, readiness = runtime.prepare(
                    snapshot, None, vm=vm, generation=generation, now=100.0 + generation)
                return snapshot, readiness

            failed, failed_readiness = observe(1)
            self.assertEqual(failed.observations["resource-runs"]["state"], "failed")
            self.assertTrue(failed.observations["resource-runs"]["attempted"])
            self.assertFalse(failed_readiness["ready"])
            self.assertFalse(failed_readiness["all_new"])
            self.assertIn("resource-runs", failed_readiness["pending"])
            self.assertIsNone(runtime.persist_complete_async(vm, failed_readiness))
            self.assertFalse(runtime.cache_path.exists())

            empty, empty_readiness = observe(2)
            self.assertEqual(empty.observations["resource-runs"]["state"], "absent")
            self.assertTrue(empty_readiness["ready"])
            empty_write = runtime.persist_complete_async(vm, empty_readiness)
            self.assertIsNotNone(empty_write)
            empty_write.join(2)
            previous_cache = runtime.cache_path.read_bytes()

            failed_again, failed_again_readiness = observe(3)
            self.assertEqual(failed_again.observations["resource-runs"]["state"], "failed")
            self.assertFalse(failed_again_readiness["ready"])
            self.assertIsNone(runtime.persist_complete_async(vm, failed_again_readiness))
            self.assertEqual(runtime.cache_path.read_bytes(), previous_cache)
            self.assertIsNotNone(runtime.load_last_complete({"codex"}))

            recovered, recovered_readiness = observe(4)
            self.assertEqual(recovered.observations["resource-runs"]["state"], "ok")
            self.assertTrue(recovered_readiness["ready"])
            self.assertTrue(recovered_readiness["all_new"])
            self.assertEqual([row.run_id for row in recovered.resources], ["resource-recovered"])
            recovered_write = runtime.persist_complete_async(vm, recovered_readiness)
            self.assertIsNotNone(recovered_write)
            recovered_write.join(2)
            self.assertNotEqual(runtime.cache_path.read_bytes(), previous_cache)


class PaneMetadata(unittest.TestCase):
    def test_derived_claude_idle_metadata_is_repaired_by_existing_detail_path(self):
        sid = "1d334a90-4214-4d30-a3ad-4c0b20f1ca28"
        observed = {"agent": "claude", "pane_id": "idle-pane", "agent_status": "idle",
                    "revision": 0, "agent_session": {"kind": "id", "agent": "claude", "value": sid}}
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            home = Path(temporary) / "claude"
            (home / "sessions").mkdir(parents=True)
            (home / "sessions/77.json").write_text(json.dumps({
                "pid": 77, "sessionId": sid, "name": "project-2e", "nameSource": "derived"}))
            stack.enter_context(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home),
                "XDG_STATE_HOME": temporary + "/state", "FLEET_TITLE_STATE_DIR": temporary + "/titles",
                "HERDR_PANE_ID": ""}))
            stack.enter_context(mock.patch.object(herdr, "list_agents", return_value=[observed]))
            stack.enter_context(mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"))
            stack.enter_context(mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)))
            stack.enter_context(mock.patch.object(herdr_projection, "session_title", return_value="Existing subject"))
            writer = stack.enter_context(mock.patch.object(herdr_projection, "_send_projection", autospec=True))
            forbidden = [stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError(name)))
                         for owner, name in ((herdr_projection, "_report"),
                             (herdr_projection, "_session_lifecycle"), (herdr_projection, "_assign_observed_tag"),
                             (session_tags, "assign"), (session_tags, "assign_observed"))]
            for name, value in (("_fill_locations", None), ("_campaign_labels", None),
                                ("_scan_degradations", {}), ("_pending_delivery_counts", {})):
                stack.enter_context(mock.patch.object(dispatch, name, return_value=value))
            stack.enter_context(mock.patch.object(projection, "attach_projections"))
            stack.enter_context(mock.patch("fleet.route_chain.enrich"))
            stack.enter_context(mock.patch("fleet.collectors.peer_messages.collect"))
            self.assertEqual(herdr_projection.resolve_tag("claude", sid), "2e")
            # The already published basic snapshot is the input to the existing
            # detail worker; there is no hook/lifecycle/number assignment here.
            details._enrich(LiveSnapshot(tag_metadata=[observed]))
            writer.assert_called_once()
            command, observation = writer.call_args.args
            self.assertEqual(command[:4], ["herdr", "pane", "report-metadata", "idle-pane"])
            self.assertEqual(command[command.index("--source") + 1], "herdr:claude")
            self.assertEqual(command[command.index("--display-agent") + 1], "[2e] claude")
            self.assertIn("[2e]", command[command.index("--title") + 1])
            self.assertIn("Existing subject", command[command.index("--title") + 1])
            self.assertIsNone(observation)
            for spy in forbidden:
                spy.assert_not_called()
            writer.reset_mock()
            (home / "sessions/77.json").write_text(json.dumps({
                "pid": 77, "sessionId": sid, "name": "Automatic subject", "nameSource": "auto"}))
            self.assertIsNone(herdr_projection.resolve_tag("claude", sid))
            details._enrich(LiveSnapshot(tag_metadata=[observed]))
            writer.assert_not_called()
            for spy in forbidden:
                spy.assert_not_called()

    def test_missing_unknown_auto_or_already_correct_metadata_never_sends(self):
        for harness in herdr_projection.HARNESSES:
            observed = {"agent": harness, "pane_id": "pane-a",
                        "agent_session": {"kind": "id", "agent": harness, "value": "observed-sid"}}
            for row, tag in ((observed, None), ({**observed, "display_agent": "[ab] " + harness}, "ab"),
                    ({**observed, "pane_id": None}, "ab"), ({**observed, "agent_session": None}, "ab"),
                    ({**observed, "agent_session": {"kind": "id", "value": ""}}, "ab"),
                    ({**observed, "agent_session": {"kind": "name", "value": "observed-sid"}}, "ab"),
                    ({**observed, "agent_session": {"kind": "id", "agent": "foreign", "value": "observed-sid"}}, "ab")):
                with self.subTest(harness=harness, row=row, tag=tag), \
                        mock.patch.object(herdr_projection, "resolve_tag", return_value=tag), \
                        mock.patch.object(herdr_projection, "_send_projection") as writer, \
                        mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"):
                    herdr_projection.refresh_observed_tag_metadata([row])
                    writer.assert_not_called()

    def test_first_collection_assigns_and_failure_still_reads_confirmed_snapshot(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"XDG_STATE_HOME": td}))
            sessions = [Session(harness=h, pid=os.getpid(), session_id="native-" + h, session_tag="ff")
                        for h in ("codex", "opencode")]
            live = {(s.harness, s.session_id): {"harness": s.harness,
                    "session_id": s.session_id, "started_at": 10} for s in sessions}
            for target, result in (
                ("fleet.collectors.procscan.scan", sessions),
                ("fleet.collectors.codex.prepare_tick", None),
                ("fleet.collectors.codex.enrich", None),
                ("fleet.collectors.opencode.enrich", None),
                ("fleet.collectors.herdr.enrich", None),
                ("fleet.collectors.usage_cache.account_usage", {}),
                ("fleet.collectors.resource_runs.collect", []),
                ("fleet.projection.attach_projections", None),
                ("fleet.route_chain.enrich", None),
                ("fleet.collectors.peer_messages.collect", None),
                ("fleet.session_tags._inventory", (live, [])),
                ("fleet.collectors.dispatch.collect", []),
            ):
                stack.enter_context(mock.patch(target, return_value=result))
            path = session_tags._path()
            path.parent.mkdir(parents=True)
            with path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                pending = collectors.collect_all(jobs_path=td + "/jobs", fast_first=True)
            self.assertEqual([s.session_tag for s in pending.sessions], [None, None])
            self.assertFalse(path.exists())
            confirmed = collectors.collect_all(jobs_path=td + "/jobs", fast_first=True)
            tags = [s.session_tag for s in confirmed.sessions]
            self.assertTrue(all(tags))
            for s in sessions:
                s.session_tag = "ff"  # stale enrichment may not survive a missing row
            sessions.append(Session(harness="codex", pid=os.getpid(), session_id="new-native", session_tag="ff"))
            with mock.patch.object(session_tags, "assign", side_effect=OSError("optional writer")):
                failed = collectors.collect_all(jobs_path=td + "/jobs", fast_first=True)
            self.assertEqual([s.session_tag for s in failed.sessions], tags + [None])


    def test_pane_metadata_wait_cannot_delay_basic_publication(self):
        with tempfile.TemporaryDirectory() as state:
            agents = [{"agent": herdr_projection.HARNESSES[i % 3], "pane_id": str(i),
                       "agent_session": {"kind": "id", "value": "audit-pane-%d" % i}}
                      for i in range(6)]
            live = {(herdr_projection.HARNESSES[i % 3], "audit-pane-%d" % i):
                    {"harness": herdr_projection.HARNESSES[i % 3], "session_id": "audit-pane-%d" % i,
                     "tag": "ab" if i % 3 == 0 else None,
                     "started_at": 10, "pid": 1, "proc_start": "audit"}
                    for i in range(6)}
            published, reporting, release = threading.Event(), threading.Event(), threading.Event()
            values = []

            def report(*args, **kwargs):
                reporting.set()
                release.wait(3)

            def collect():
                values.append(collectors.collect_all(jobs_path=state + "/jobs", fast_first=True))
                published.set()

            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}), \
                 mock.patch.object(session_tags, "_inventory", side_effect=lambda rows: (live.copy(), agents)), \
                 mock.patch.object(herdr_projection, "_send_projection", autospec=True, side_effect=report) as writer, \
                 mock.patch.multiple(herdr_projection, session_title=lambda *a: "",
                                     _formatter_overrides=lambda *a: (None, None),
                                     compose=lambda *a, **k: ("[ab] codex", ""),
                                     resolve_tag=lambda *a: "ab"), \
                 mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"), \
                 mock.patch.object(herdr, "list_agents", return_value=agents), \
                 mock.patch.object(procscan, "scan", return_value=[]), \
                 mock.patch.object(herdr, "enrich"), \
                 mock.patch.object(usage_cache, "account_usage", return_value={}), \
                 mock.patch.object(resource_runs, "collect", return_value=[]), \
                 mock.patch.object(projection, "attach_projections"), \
                 mock.patch.object(dispatch, "collect", return_value=[]), \
                 mock.patch.object(dispatch, "_fill_locations"), \
                 mock.patch.object(dispatch, "_campaign_labels"), \
                 mock.patch.object(dispatch, "_scan_degradations", return_value={}), \
                 mock.patch.object(dispatch, "_pending_delivery_counts", return_value={}), \
                 mock.patch("fleet.route_chain.enrich"), \
                 mock.patch("fleet.collectors.peer_messages.collect", return_value=None):
                basic = threading.Thread(target=collect, daemon=True)
                detail = None
                basic.start()
                try:
                    self.assertTrue(published.wait(1), "pane metadata blocked basic publication")
                    writer.assert_not_called()
                    self.assertEqual(values[0].tag_metadata, agents)
                    detail = threading.Thread(target=details._enrich, args=(values[0],), daemon=True)
                    detail.start()
                    self.assertTrue(reporting.wait(1), "detail did not update pane metadata")
                    self.assertFalse(release.is_set())
                    collectors.collect_all(jobs_path=state + "/jobs", fast_first=True)
                finally:
                    release.set()
                    basic.join(3)
                    if detail is not None:
                        detail.join(3)
                self.assertFalse(basic.is_alive())
                self.assertFalse(detail.is_alive())
                self.assertEqual(writer.call_count, len(agents))

    def test_delayed_metadata_never_writes_to_a_changed_or_unconfirmed_pane(self):
        for harness in herdr_projection.HARNESSES:
            observed = {"agent": harness, "pane_id": "pane-a",
                        "agent_session": {"kind": "id", "value": "observed-sid"}}
            changed = {**observed, "agent_session": {"kind": "id", "value": "new-sid"}}
            foreign = {**observed, "agent": "foreign"}
            moved = {**observed, "pane_id": "different-pane"}
            for current, count in (([observed], 1), ([changed], 0), (None, 0),
                                   ([observed, changed], 0), ([observed, observed], 0),
                                   ([foreign], 0), ([moved], 0)):
                with self.subTest(harness=harness, current=current), \
                     mock.patch.object(herdr, "list_agents", return_value=current), \
                     mock.patch.object(herdr_projection, "resolve_tag", return_value="ab"), \
                     mock.patch.object(herdr_projection, "_send_projection", autospec=True) as writer, \
                     mock.patch.object(herdr_projection, "session_title", return_value=""), \
                     mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
                     mock.patch.object(herdr_projection, "compose", return_value=("[ab] " + harness, "")), \
                     mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"), \
                     mock.patch.object(dispatch, "_fill_locations"), \
                     mock.patch.object(dispatch, "_campaign_labels"), \
                     mock.patch.object(dispatch, "_scan_degradations", return_value={}), \
                     mock.patch.object(dispatch, "_pending_delivery_counts", return_value={}), \
                     mock.patch.object(projection, "attach_projections"), \
                     mock.patch("fleet.route_chain.enrich"), \
                     mock.patch("fleet.collectors.peer_messages.collect", return_value=None):
                    details._enrich(LiveSnapshot(tag_metadata=[observed]))
                    self.assertEqual(writer.call_count, count)

    def test_formatter_delay_cannot_write_old_metadata_after_pane_switch(self):
        for harness in herdr_projection.HARNESSES:
            for change in ("sid", "harness", "pane", "tag"):
                observed = {"agent": harness, "pane_id": "pane-a",
                            "agent_session": {"kind": "id", "value": "observed-sid"}}
                current, tag = [observed], ["ab"]

                def formatter(*args):
                    if change == "tag":
                        tag[:] = ["cd"]
                    elif change == "harness":
                        current[:] = [{**observed, "agent": "foreign"}]
                    elif change == "pane":
                        current[:] = [{**observed, "pane_id": "pane-b"}]
                    else:
                        current[:] = [{**observed, "agent_session": {"kind": "id", "value": "new-sid"}}]
                    return None, None

                with self.subTest(harness=harness, change=change), \
                     mock.patch.object(herdr, "list_agents", side_effect=lambda: current.copy()), \
                     mock.patch.object(herdr_projection, "resolve_tag", side_effect=lambda *a: tag[0]), \
                     mock.patch.object(herdr_projection, "_send_projection", autospec=True) as writer, \
                     mock.patch.object(herdr_projection, "session_title", return_value=""), \
                     mock.patch.object(herdr_projection, "_formatter_overrides", side_effect=formatter), \
                     mock.patch.object(herdr_projection, "compose", return_value=("[ab] " + harness, "")), \
                     mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"):
                    herdr_projection.refresh_observed_tag_metadata([observed])
                writer.assert_not_called()

    def test_identity_lookup_delay_cannot_write_a_reassigned_number(self):
        for harness in herdr_projection.HARNESSES:
            observed = {"agent": harness, "pane_id": "pane-a",
                        "agent_session": {"kind": "id", "value": "observed-sid"}}
            tag = ["ab"]

            def current_agents():
                tag[:] = ["cd"]
                return [observed]

            with mock.patch.object(herdr, "list_agents", side_effect=current_agents), \
                 mock.patch.object(herdr_projection, "resolve_tag", side_effect=lambda *a: tag[0]), \
                 mock.patch.object(herdr_projection, "_send_projection", autospec=True) as writer, \
                 mock.patch.object(herdr_projection, "_metadata_command",
                                   return_value=["herdr", "pane", "report-metadata", "pane-a",
                                                 "--display-agent", "[ab] " + harness]), \
                 mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"):
                herdr_projection.refresh_observed_tag_metadata([observed])
            writer.assert_not_called()


class PublishedSnapshots(unittest.TestCase):
    def tearDown(self):
        render.set_refresh_health()

    def test_once_thread_start_failure_keeps_basic_screen(self):
        from fleet import refresh
        import io
        with mock.patch.object(refresh.threading.Thread, "start", side_effect=RuntimeError("no threads")), \
             mock.patch.object(render, "_collect_memory", return_value=None), \
             mock.patch.object(render.gitinfo, "enrich_entities"), \
             mock.patch.object(render, "_build_lines", return_value=[]) as build, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            render.render_once(lambda **kwargs: LiveSnapshot(), None, "both",
                               compute_hosts_refresh=lambda: None)
        self.assertEqual(build.call_args.kwargs["observations"]["governor"]["state"], "failed")
        self.assertEqual(build.call_args.kwargs["observations"]["compute hosts"]["state"], "failed")

    def test_detail_failure_reaches_existing_health_display(self):
        render.set_refresh_health(snapshot={"state": "idle", "age": 0},
                                  details={"state": "failed", "age": 55,
                                           "last_error": "OSError: NAS denied"})
        text = "".join(t for t, _ in render._refresh_health_segments())
        self.assertIn('detail unknown', text)
        self.assertIn("NAS denied", text)
        self.assertIn("0m ago", text)

    def test_actual_loop_publishes_detail_failure_while_basic_refresh_succeeds(self):
        self.addCleanup(setattr, render, "_BLINK_ON", render._BLINK_ON)
        observed = threading.Event()
        values = []

        def collector(**kwargs):
            return LiveSnapshot()

        def failed(source):
            raise OSError("detail NAS denied")

        collector.detail_refresh = failed

        def draw(*args, **kwargs):
            detail = render._REFRESH_HEALTH.get("details") or {}
            if detail.get("state") == "failed":
                values.append(render._REFRESH_HEALTH)
                observed.set()

        class Screen:
            def timeout(self, value):
                pass
            def getmaxyx(self):
                return 50, 168
            def getch(self):
                return ord("q") if observed.wait(0.01) else -1

        with mock.patch.object(render, "_init_colors"), \
             mock.patch.object(render, "_draw", side_effect=draw), \
             mock.patch.object(render.curses, "curs_set"), \
             mock.patch.object(render, "_collect_memory", return_value=None), \
             mock.patch.object(render, "_collect_governor", return_value=None), \
             mock.patch.object(render.gitinfo, "enrich_entities"), \
             mock.patch.object(render, "_poll_pending_kill"), \
             mock.patch.object(render, "_handle_base_key", return_value=False), \
             mock.patch.object(render, "_PROMPT", None), \
             mock.patch.object(render, "_SELECT_MODE", False):
            worker = threading.Thread(target=render._loop,
                                      args=(Screen(), collector, None, "both", 60.0), daemon=True)
            worker.start()
            self.assertTrue(observed.wait(3.0))
            worker.join(3.0)
            self.assertFalse(worker.is_alive())
        self.assertIn("detail NAS denied", values[-1]["details"]["last_error"])
        self.assertIsNone(values[-1]["details"]["last_success_at"])
        self.assertNotEqual(values[-1]["snapshot"]["state"], "failed")

    def test_render_never_reprojects_bare_rows_or_reads_collector_globals(self):
        evidence = {"rt-audit": {"report": {"route_file": "/audit/route.json"}}}
        with mock.patch.object(dispatch.collect, "last_route_nodes", evidence), \
             mock.patch.object(projection, "attach_projections") as attach:
            render._build_lines([], [], "both", False, 0)
            render._build_lines([], [DispatchJob(key="audit", slug="audit")], "both", False, 0)
        attach.assert_not_called()

    def test_detail_publication_does_not_write_collector_side_channels(self):
        previous = {"audit": "old"}
        with mock.patch.object(dispatch.collect, "last_degradations", previous), \
             mock.patch.object(dispatch.collect, "last_pending_delivery", previous), \
             mock.patch.object(dispatch, "_fill_locations"), \
             mock.patch.object(dispatch, "_campaign_labels"), \
             mock.patch.object(dispatch, "_scan_degradations", return_value={"audit": "new"}), \
             mock.patch.object(dispatch, "_pending_delivery_counts", return_value={}), \
             mock.patch.object(projection, "attach_projections"), \
             mock.patch("fleet.route_chain.enrich"), \
             mock.patch("fleet.collectors.peer_messages.collect", return_value=None):
            basic = LiveSnapshot()
            detail = details._enrich(basic)
            self.assertEqual(details.merge(basic, detail).degradations, {"audit": "new"})
            changed = LiveSnapshot(jobs=[DispatchJob(key="new", slug="new")])
            self.assertEqual(details.merge(changed, detail).degradations, {})
            self.assertEqual(dispatch.collect.last_degradations, previous)
            self.assertEqual(dispatch.collect.last_pending_delivery, previous)


if __name__ == "__main__":
    unittest.main()
