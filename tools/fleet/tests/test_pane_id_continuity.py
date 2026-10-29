"""Native same-pane resumes keep supervision, peer badges and current ID reporting."""
import os
import json
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))
import session_tidy
from fleet import herdr_projection, render, session_handle, session_registry
from fleet.collectors import herdr, steward, procscan
from fleet.model import Session


class PaneContinuityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.foreign = self.root / "foreign"
        (self.repo / ".git").mkdir(parents=True)
        (self.foreign / ".git").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        # These fixtures key panes from their env alone, whatever herdr runs on the host;
        # endpoint discovery is covered by EndpointDiscoveryTest.
        discovery = mock.patch.object(session_tidy, "_herdr_endpoint_socket", return_value="")
        discovery.start()
        self.addCleanup(discovery.stop)

    def record(self, harness, sid, pane="w:p", cwd=None, source="resume", now=100):
        seat = session_tidy._pane_seat_of(pane)
        with session_tidy.seat_lock(seat.key):
            session_tidy.record_event(seat, harness, sid, "start", source=source,
                                      cwd=str(cwd or self.repo), now=now)

    def handover(self, harness, older, newer, pane="w:p", source="clear", now=500):
        seat = session_tidy._pane_seat_of(pane)
        row = {"ts": now, "harness": harness, "sid": newer, "event": "handover",
               "from": older, "source": source}
        with session_tidy.seat_lock(seat.key):
            session_tidy._append_ledger_entry(seat, row)

    def test_aliases_use_exact_pane_harness_repository_and_both_id_directions(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.record(harness, "old", now=100)
                self.record(harness, "current", now=200)
                self.record(harness, "other-pane", pane="w:other", now=300)
                self.record(harness, "foreign", cwd=self.foreign, now=400)
                self.assertEqual(session_tidy.pane_session_aliases(harness, "old", "w:p", str(self.repo)), ["current"])
                self.assertEqual(session_tidy.pane_session_aliases(harness, "current", "w:p", str(self.repo)), ["old"])
                self.assertEqual(session_tidy.pane_session_aliases(harness, "unknown", "w:p", str(self.repo)), [])
                self.assertEqual(session_tidy.session_start_source(harness, "current", "w:p"), "resume")

    def test_confirmed_handover_chain_survives_compact_resume_and_recent_alias_limit(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                ids = [f"{harness}-{index}" for index in range(10)]
                self.record(harness, ids[0], source="startup", now=100)
                for index, sid in enumerate(ids[1:], start=1):
                    if harness == "opencode":
                        seat = session_tidy._pane_seat_of("w:p")
                        with session_tidy.seat_lock(seat.key):
                            session_tidy.record_event(seat, harness, sid, "prompt",
                                                      cwd=str(self.repo), now=100 + index)
                        source = "first-message"
                    else:
                        self.record(harness, sid, source="clear", now=100 + index)
                        source = "clear"
                    self.handover(harness, ids[index - 1], sid, source=source, now=200 + index)
                    seat = session_tidy._pane_seat_of("w:p")
                    with session_tidy.seat_lock(seat.key):
                        session_tidy.record_event(seat, harness, sid, "compact", now=300 + index)
                        session_tidy.record_event(seat, harness, sid, "start", source="resume",
                                                  cwd=str(self.repo), now=400 + index)

                expected = list(reversed(ids[:-1]))
                self.assertEqual(session_tidy.confirmed_pane_predecessors(
                    harness, ids[-1], "w:p", str(self.repo)), expected)
                self.assertEqual(herdr._clear_gpu_session_aliases(
                    harness, ids[-1], "w:p", str(self.repo)), expected)
                aliases = session_tidy.pane_session_aliases(harness, ids[-1], "w:p", str(self.repo))
                self.assertEqual(set(aliases), set(ids[:-1]))
                self.assertEqual(len(aliases), 9)

    def test_legacy_endpoint_ledger_requires_exact_sid_and_repository_bridge(self):
        with mock.patch.dict(os.environ, {"AGENT_HERDR_SESSION": "fixture-current",
                                         "HERDR_SOCKET_PATH": ""}):
            current = session_tidy._pane_seat_of("w:p")
            legacy = session_tidy.Seat("pane", session_tidy._digest("pane", "w:p"),
                                       "w:p", "codex")
            with session_tidy.seat_lock(current.key):
                session_tidy.record_event(current, "codex", "shared", "start",
                                          source="startup", cwd=str(self.repo), now=300)
            with session_tidy.seat_lock(legacy.key):
                session_tidy.record_event(legacy, "codex", "older", "start",
                                          source="startup", cwd=str(self.repo), now=100)
                session_tidy.record_event(legacy, "codex", "shared", "start",
                                          source="clear", cwd=str(self.repo), now=200)
                session_tidy._append_ledger_entry(legacy, {
                    "ts": 250, "harness": "codex", "sid": "shared", "event": "handover",
                    "from": "older", "source": "clear"})
            paths = (session_tidy._ledger_path(current), session_tidy._ledger_path(legacy))
            before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
            self.assertEqual(session_tidy.confirmed_pane_predecessors(
                "codex", "shared", "w:p", str(self.repo)), ["older"])
            self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_other_endpoint_and_unbridged_legacy_rows_are_not_read_as_lineage(self):
        with mock.patch.dict(os.environ, {"AGENT_HERDR_SESSION": "fixture-current",
                                         "HERDR_SOCKET_PATH": ""}):
            current = session_tidy._pane_seat_of("w:p")
            other_endpoint_id = session_tidy._digest("herdr-endpoint", "session", "fixture-other")
            other_endpoint = session_tidy.Seat(
                "pane", session_tidy._digest("pane", other_endpoint_id, "w:p"),
                "w:p", "codex", endpoint=other_endpoint_id)
            legacy = session_tidy.Seat("pane", session_tidy._digest("pane", "w:p"),
                                       "w:p", "codex")
            with session_tidy.seat_lock(current.key):
                session_tidy.record_event(current, "codex", "current", "start",
                                          source="startup", cwd=str(self.repo), now=300)
            for seat, successor in ((other_endpoint, "current"), (legacy, "legacy-current")):
                with session_tidy.seat_lock(seat.key):
                    session_tidy.record_event(seat, "codex", "foreign-old", "start",
                                              source="startup", cwd=str(self.repo), now=100)
                    session_tidy.record_event(seat, "codex", successor, "start",
                                              source="clear", cwd=str(self.repo), now=200)
                    session_tidy._append_ledger_entry(seat, {
                        "ts": 250, "harness": "codex", "sid": successor, "event": "handover",
                        "from": "foreign-old", "source": "clear"})
            self.assertEqual(session_tidy.confirmed_pane_predecessors(
                "codex", "current", "w:p", str(self.repo)), [])

    def test_conflicting_foreign_pane_metadata_invalidates_same_sid_evidence(self):
        seat = session_tidy._pane_seat_of("w:p")
        self.record("codex", "old", source="startup", now=100)
        self.record("codex", "current", source="clear", now=200)
        with session_tidy.seat_lock(seat.key):
            session_tidy._append_ledger_entry(seat, {
                "ts": 250, "harness": "codex", "sid": "current", "event": "handover",
                "from": "old", "source": "clear", "pane": "w:foreign"})
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), [])

    def test_clear_start_uses_the_exact_booking_and_immediate_same_harness_predecessor(self):
        import session_tidy_clear
        seat = session_tidy._pane_seat_of("w:p")
        self.record("codex", "first", source="startup", now=100)
        self.record("codex", "middle", source="resume", now=200)
        self.record("codex", "current", source="clear", now=300)
        booking = {"schema": 1, "nonce": "fixture", "status": "cleared", "seat": seat.as_dict(),
                   "harness": "codex", "sid": "middle", "new_session": "current",
                   "observed": {"harness": "codex", "sid": "current"}, "cwd": str(self.repo)}
        session_tidy_clear._write_reservation(booking)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), ["middle"])

        booking["observed"] = {"harness": "codex", "sid": "other"}
        session_tidy_clear._write_reservation(booking)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), [])

        booking.update(observed={"harness": "codex", "sid": "current"}, sid="first")
        session_tidy_clear._write_reservation(booking)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), [])

    def test_folded_start_provenance_confirms_clear_when_raw_predecessor_start_is_gone(self):
        import session_tidy_clear
        seat = session_tidy._pane_seat_of("w:p")
        with session_tidy.seat_lock(seat.key):
            session_tidy._append_ledger_entry(seat, {
                "ts": 150, "harness": "codex", "sid": "folded-old", "event": "summary",
                "cwd": str(self.repo), "latest_start_source": "startup", "latest_start_at": 100})
            session_tidy.record_event(seat, "codex", "current", "start", source="clear",
                                      cwd=str(self.repo), now=300)
        session_tidy_clear._write_reservation({
            "schema": 1, "nonce": "folded-fixture", "status": "cleared", "seat": seat.as_dict(),
            "harness": "codex", "sid": "folded-old", "new_session": "current",
            "observed": {"harness": "codex", "sid": "current"}, "cwd": str(self.repo)})
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), ["folded-old"])

    def test_foreign_repository_and_harness_handover_evidence_is_ignored(self):
        self.record("codex", "foreign-repo-old", cwd=self.foreign, source="startup", now=100)
        self.record("codex", "repo-current", source="resume", now=200)
        self.handover("codex", "foreign-repo-old", "repo-current", now=300)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "repo-current", "w:p", str(self.repo)), [])

        self.record("codex", "harness-old", source="startup", now=400)
        self.record("codex", "harness-current", source="resume", now=500)
        self.handover("claude", "harness-old", "harness-current", now=600)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "harness-current", "w:p", str(self.repo)), [])

    def test_ambiguous_predecessors_and_cycles_do_not_repeat_ids(self):
        for sid, now in (("a", 100), ("b", 200), ("c", 300), ("current", 400)):
            self.record("codex", sid, source="startup", now=now)
        self.handover("codex", "a", "current", now=500)
        self.handover("codex", "b", "current", now=501)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), [])

        self.handover("codex", "a", "b", now=600)
        self.handover("codex", "b", "a", now=601)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "b", "w:p", str(self.repo)), ["a"])

    def test_stale_native_id_joins_new_marker_peer_badge_and_herdr_role_on_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            self.record(harness, "old", now=100)
            self.record(harness, "current", now=200)
            parent = Session(harness=harness, pid=10, proc_start="start", session_id="old",
                             session_tag="4d", cwd=str(self.repo), liveness="idle")
            child = Session(harness="codex", pid=20, session_id="child", session_tag="cb",
                            cwd=str(self.repo), liveness="idle")
            child.peer_last_recv = {"from_harness": harness, "from_session_id": "current", "kind": "steer", "age_min": 0}
            markers = {(harness, "current"): {"targets": {"child": {
                "harness": "codex", "session_id": "child", "source": "start"}}}}
            agents = [{"agent": harness, "pane_id": "w:p", "name": "supervisor",
                       "cwd": str(self.repo), "agent_session": {"agent": harness, "value": "current"}}]
            with mock.patch.object(procscan, "read_proc_start", return_value="start"), \
                 mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=[]):
                herdr.enrich([parent, child], agents=agents, pids=({1}, {10}), pane_bindings={10: {"w:p"}})
            self.assertIn((harness, "current"), session_registry.session_join_keys(parent))
            with mock.patch.object(steward, "_registry_sessions", return_value=[]), \
                 mock.patch.object(steward, "_native_role_sessions", return_value=[]), \
                 mock.patch.object(steward, "read_markers", return_value=markers), \
                 mock.patch.object(steward, "_projection_sessions", return_value=[parent, child]), \
                 mock.patch.object(herdr_projection.os, "getcwd", return_value=str(self.repo)):
                steward.enrich([parent, child])
                self.assertTrue(parent.steward)
                self.assertTrue(herdr_projection.is_steward(harness, "current"))
                self.assertEqual(child.steward_parents[0]["session_id"], "old")
            rows = render._build_lines([child, parent], [], "fleet", False, 0, term_width=100, layout="narrow")
            shown = "\n".join(render._plain(row) for row in rows if row)
            self.assertIn("← [4d]", shown)
            self.assertLess(shown.index("[4d]"), shown.index("[cb]"))

    def test_another_live_session_keeps_its_identity_and_pid_reuse_adds_no_alias(self):
        self.record("codex", "old", now=100)
        self.record("codex", "current", now=200)
        old = Session(harness="codex", pid=10, proc_start="start", session_id="old", cwd=str(self.repo))
        current = Session(harness="codex", pid=20, proc_start="start", session_id="current", cwd=str(self.repo))
        from fleet.collectors import procscan
        for start in ("start", "reused"):
            with mock.patch.object(procscan, "read_proc_start", return_value=start):
                herdr.enrich([old, current], agents=[], pids=({1}, {10}), pane_bindings={10: {"w:p"}})
            self.assertNotIn(("codex", "current"), session_registry.session_join_keys(old))

    def test_same_pane_native_resume_companion_keeps_one_foreground_row_and_current_work(self):
        for harness in ("claude", "codex", "opencode"):
            self.record(harness, "old", now=100)
            self.record(harness, "current", now=200)
            old = Session(harness=harness, pid=10, proc_start="start", session_id="old",
                          session_tag="4d", cwd=str(self.repo), liveness="idle")
            resumed = Session(harness=harness, pid=20, proc_start="start", session_id="current",
                              cwd=str(self.root), detached=True, liveness="working", summary="current work")
            agents = [{"agent": harness, "pane_id": "w:p", "cwd": str(self.repo),
                       "agent_session": {"agent": harness, "value": "current"}}]
            rows = [old, resumed]
            with mock.patch.object(procscan, "read_proc_start", return_value="start"), \
                 mock.patch.object(herdr, "_ppid_of", side_effect=lambda pid: 10 if pid == 20 else 1):
                herdr.enrich(rows, agents=agents, pids=({1}, {10}), pane_bindings={10: {"w:p"}})
            self.assertEqual(rows, [old])
            self.assertEqual(old.liveness, "working")
            self.assertEqual(old.summary, "current work")
            self.assertIn((harness, "current"), session_registry.session_join_keys(old))

    def test_recorded_start_source_reaches_shared_publisher_without_weakening_guard(self):
        for harness in ("claude", "codex", "opencode"):
            self.record(harness, "current")
            with mock.patch.object(herdr_projection.shutil, "which", return_value="/fixture/herdr"), \
                 mock.patch.object(herdr_projection, "may_report", return_value=True), \
                 mock.patch.object(herdr_projection, "_report") as report:
                herdr_projection.project(harness, "current", pane_id="w:p")
                self.assertEqual(report.call_args.kwargs["session_start_source"], "resume")
                self.assertGreater(report.call_args.kwargs["session_seq"], 0)
            with mock.patch.object(herdr_projection.shutil, "which", return_value="/fixture/herdr"), \
                 mock.patch.object(herdr_projection, "may_report", return_value=False), \
                 mock.patch.object(herdr_projection, "_report") as report:
                herdr_projection.project(harness, "current", pane_id="w:p")
                report.assert_not_called()

    def test_codex_projection_reports_start_lifecycle_for_start_resume_and_fork(self):
        lifecycle_path = ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py"
        lifecycle_spec = importlib.util.spec_from_file_location("codex_sessionstart_lifecycle_test",
                                                                 lifecycle_path)
        lifecycle = importlib.util.module_from_spec(lifecycle_spec)
        lifecycle_spec.loader.exec_module(lifecycle)
        projection_path = ROOT / "adapters/codex/hooks/herdr_session_projection.py"
        projection_spec = importlib.util.spec_from_file_location("codex_herdr_projection_test",
                                                                  projection_path)
        projection = importlib.util.module_from_spec(projection_spec)
        projection_spec.loader.exec_module(projection)
        for source, current_thread in (("startup", "new-thread"),
                                       ("resume", "resumed-thread"),
                                       ("fork", "fork-current-thread")):
            with self.subTest(source=source):
                payload = {"hook_event_name": "SessionStart", "source": source,
                           "session_id": current_thread}
                self.assertEqual(lifecycle.session_id(payload), current_thread)
                with mock.patch.object(projection, "_project", return_value=True) as publish, \
                        mock.patch("peer_obligations.request_obligation_recovery"):
                    self.assertTrue(projection.project(payload, current_thread))
                args, kwargs = publish.call_args
                self.assertEqual(args, ("codex", current_thread))
                self.assertIs(kwargs.get("report_session", True), True)
                self.assertGreater(kwargs["session_seq"], 0)
                self.assertEqual(kwargs["session_start_source"], source)
                self.assertNotIn("resume_argv", kwargs)

    def test_codex_projection_rekeys_only_when_current_native_launch_is_proven(self):
        from fleet import process_identity
        argv = ["/opt/codex", "resume", "old-session", "--model", "fixture"]
        with mock.patch.object(herdr_projection.shutil, "which", return_value="/fixture/herdr"), \
                mock.patch.object(herdr_projection, "may_report", return_value=True), \
                mock.patch.object(process_identity, "current_launch_context", return_value={"argv": argv}), \
                mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
                mock.patch.object(herdr_projection, "compose", return_value=("codex", "")), \
                mock.patch.object(herdr_projection.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            herdr_projection.project("codex", "current-session", pane_id="w:p", session_seq=100,
                                     session_start_source="clear")
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[-8:], ["--session-start-source", "clear", "--", "codex",
                                       "resume", "current-session", "--model", "fixture"])

    def test_proven_embedded_sessionstart_keeps_reporting_with_the_v10_state_hook(self):
        path = ROOT / "adapters/codex/hooks/herdr_session_projection.py"
        spec = importlib.util.spec_from_file_location("codex_embedded_projection_test", path)
        projection = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(projection)
        payload = {"hook_event_name": "SessionStart", "session_id": "thread-A",
                   "transcript_path": "/fixture/rollout.jsonl", "source": "resume"}
        with tempfile.TemporaryDirectory(prefix="codex-home-") as temporary:
            (Path(temporary) / "herdr-agent-state.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            env = {"HERDR_SOCKET_PATH": "/fixture/herdr.sock", "HERDR_PANE_ID": "w:p",
               "CODEX_HOME": temporary,
               "CODEX_THREAD_ID": "thread-A"}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(projection, "_may_report", return_value=True), \
                 mock.patch.object(projection, "_project", return_value=True) as publish, \
                 mock.patch("peer_obligations.request_obligation_recovery"):
                projection.project(payload, "thread-A")
        kwargs = publish.call_args.kwargs
        self.assertIs(kwargs["report_session"], False)
        self.assertEqual(kwargs["session_start_source"], "resume")
        self.assertGreater(kwargs["session_seq"], 0)

    def test_claude_pane_badge_survives_native_id_change(self):
        self.record("claude", "old", now=100)
        self.record("claude", "current", now=200)
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True)
        (home / "sessions" / "10.json").write_text(json.dumps(
            {"sessionId": "old", "name": "hearting-4d", "nameSource": "derived"}))
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w:p", "CLAUDE_CONFIG_DIR": str(home),
                                          "FLEET_TITLE_STATE_DIR": str(self.root / "titles")}), \
             mock.patch.object(session_handle.os, "getcwd", return_value=str(self.repo)):
            self.assertEqual(session_handle.resolve_tag("claude", "current"), "4d")

    def test_recipient_tag_uses_recipient_pane_and_cwd_across_resume(self):
        self.record("claude", "sender-old", pane="w:sender", now=100)
        self.record("claude", "current", pane="w:sender", now=200)
        self.record("claude", "recipient-old", pane="w:recipient", now=100)
        self.record("claude", "current", pane="w:recipient", now=200)
        home = self.root / "claude-recipient"
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        for pid, sid, name in (("11", "sender-old", "sender-4c"),
                               ("12", "recipient-old", "recipient-3a")):
            (sessions / f"{pid}.json").write_text(json.dumps(
                {"sessionId": sid, "name": name, "nameSource": "derived"}),
                encoding="utf-8")
        with mock.patch.dict(os.environ, {
                "HERDR_PANE_ID": "w:sender", "CLAUDE_CONFIG_DIR": str(home),
                "FLEET_TITLE_STATE_DIR": str(self.root / "titles")}), \
                mock.patch.object(session_handle.os, "getcwd", return_value=str(self.repo)):
            # The compatibility API retains its caller-seat behavior.
            self.assertEqual(session_handle.resolve_tag("claude", "current"), "4c")
            # A recipient lookup follows only that recipient's resumed seat.
            self.assertEqual(session_handle.resolve_recipient_tag(
                "claude", "current", pane="w:recipient", cwd=str(self.repo)), "3a")

    def test_resume_source_is_in_the_actual_herdr_session_command_for_all_harnesses(self):
        for harness in ("claude", "codex", "opencode"):
            with mock.patch.object(herdr_projection.shutil, "which", return_value="/fixture/herdr"), \
                 mock.patch.object(herdr_projection, "session_title", return_value=""), \
                 mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
                 mock.patch.object(herdr_projection, "compose", return_value=(harness, "")), \
                 mock.patch.object(herdr_projection.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                herdr_projection._report(harness, "current", "w:p", True, session_seq=100, session_start_source="resume")
                self.assertEqual(run.call_args_list[0].args[0][-4:], ["--seq", "100", "--session-start-source", "resume"])


# Fixture data only: the socket herdr reported on the host where e8 (w18:p7) lost its history.
FIXTURE_SOCKET = "/home/Uihyeop/.config/herdr/herdr.sock"
E8_KEY = "cd408b2b164213cb18a8"         # (FIXTURE_SOCKET, w18:p7): the seat e8's own hooks wrote
E8_LEGACY_KEY = "d0be643a697d29c02ff6"  # endpoint-free w18:p7: what a viewer outside herdr used to read
E8_SIDS = ("7dcee068-0000-4000-8000-000000000001", "bbf14dfc-0000-4000-8000-000000000002",
           "5ed20fe8-499b-4490-a87f-f492612948cf")


class EndpointDiscoveryTest(unittest.TestCase):
    """A pane seat key is a function of (endpoint socket, pane id), not of the reader being in herdr."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.socket = str(self.root / "herdr.sock")
        self.seat_key = session_tidy._digest("pane", session_tidy._digest(
            "herdr-endpoint", "socket", self.socket), "w18:p7")
        self.repo = self.root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.status_file = self.root / "status.json"
        self.agents_file = self.root / "agents.json"
        self.calls_file = self.root / "herdr-calls.txt"
        fake = self.root / "fake-herdr"
        fake.write_text("#!/bin/sh\n"
                        f'echo "$@" >> "{self.calls_file}"\n'
                        'case "$*" in\n'
                        f'  *"status server"*) cat "{self.status_file}" ;;\n'
                        f'  *"agent list"*) cat "{self.agents_file}" ;;\n'
                        "esac\n", encoding="utf-8")
        fake.chmod(0o755)
        executable = mock.patch.object(session_tidy, "_herdr_executable", return_value=str(fake))
        executable.start()
        self.addCleanup(executable.stop)
        session_tidy._HERDR_ENDPOINT_SOCKETS.clear()
        self.addCleanup(session_tidy._HERDR_ENDPOINT_SOCKETS.clear)

    def process_env(self, **extra):
        """A process environment with no inherited Herdr values unless named here."""
        env = {"XDG_STATE_HOME": str(self.root / "state"), "HOME": str(self.root / "home"),
               "PATH": os.environ.get("PATH") or "/usr/bin:/bin", **extra}
        return mock.patch.dict(os.environ, env, clear=True)

    def status(self, **answer):
        self.status_file.write_text(json.dumps(answer), encoding="utf-8")

    def calls(self):
        return self.calls_file.read_text(encoding="utf-8").splitlines() if self.calls_file.exists() else []

    def record(self, seat, harness, sid, event="start", **kwargs):
        with session_tidy.seat_lock(seat.key):
            return session_tidy.record_event(seat, harness, sid, event, cwd=str(self.repo), **kwargs)

    def book(self, pane, key, sid, status="reserved"):
        import session_tidy_clear
        now = session_tidy.now_epoch()
        session_tidy_clear._write_reservation({
            "schema": 1, "nonce": "n" + key[:6], "status": status, "created": now - 30,
            "deadline": now + 500, "harness": "codex", "sid": sid, "cwd": str(self.repo),
            "seat": {"kind": "pane", "key": key, "pane": pane, "harness": "codex", "project_key": ""},
            "card_generation": 1, "prompt_seq": 1})

    def test_t1_viewer_outside_herdr_keys_the_pane_by_the_socket_herdr_reports(self):
        self.status(running=True, socket=self.socket, session=None)
        with self.process_env():
            seats = [session_tidy._pane_seat_of("w18:p7") for _ in range(3)]
            endpoint = session_tidy._endpoint_scope()
        self.assertEqual([seat.key for seat in seats], [self.seat_key] * 3)
        self.assertEqual(endpoint, session_tidy._endpoint_scope({"HERDR_SOCKET_PATH": self.socket}))
        self.assertEqual((seats[0].endpoint, seats[0].socket_path), (endpoint, self.socket))
        self.assertNotIn("socket_path", seats[0].as_dict())
        self.assertEqual(self.calls(), ["status server --json"])

    def test_t2_named_session_uses_its_socket_only_while_herdr_runs_it_under_that_name(self):
        named = str(self.root / "left.sock")
        by_socket = session_tidy._digest("herdr-endpoint", "socket", os.path.realpath(named))
        by_name = session_tidy._digest("herdr-endpoint", "session", "left")
        for answer, expected in (({"running": True, "socket": named, "session": "left"}, by_socket),
                                 ({"running": False, "socket": named, "session": "left"}, by_name),
                                 ({"running": True, "socket": named, "session": "other"}, by_name),
                                 ({"running": True, "socket": named, "session": None}, by_name),
                                 ({"running": True, "socket": "", "session": "left"}, by_name)):
            with self.subTest(answer=answer):
                session_tidy._HERDR_ENDPOINT_SOCKETS.clear()
                self.calls_file.unlink(missing_ok=True)
                self.status(**answer)
                with self.process_env(AGENT_HERDR_SESSION="left"):
                    self.assertEqual(session_tidy._endpoint_scope(), expected)
                    seat = session_tidy._pane_seat_of("w:p")
                self.assertEqual(seat.key, session_tidy._digest("pane", expected, "w:p"))
                self.assertEqual(seat.socket_path, named if expected == by_socket else "")
                self.assertEqual(self.calls(), ["--session left status server --json"])
        # The default server is null, "" or "default" in herdr's answer, never another name.
        for name, expected in ((None, self.seat_key), ("", self.seat_key), ("default", self.seat_key), ("left", E8_LEGACY_KEY)):
            with self.subTest(default_session=name):
                session_tidy._HERDR_ENDPOINT_SOCKETS.clear()
                self.status(running=True, socket=self.socket, session=name)
                with self.process_env():
                    self.assertEqual(session_tidy._pane_seat_of("w18:p7").key, expected)

    def test_t3_missing_or_unreadable_herdr_keeps_the_old_key_and_is_asked_again_after_60s(self):
        with mock.patch.object(session_tidy, "_herdr_executable", return_value=None) as missing, \
                self.process_env():
            self.assertEqual(session_tidy._endpoint_scope(), "")
            seat = session_tidy._pane_seat_of("w18:p7")
            self.assertEqual((seat.key, seat.socket_path), (E8_LEGACY_KEY, ""))
        self.assertEqual(missing.call_count, 1)

        session_tidy._HERDR_ENDPOINT_SOCKETS.clear()
        self.status_file.write_text("not json", encoding="utf-8")
        with self.process_env():
            for _ in range(3):
                self.assertEqual(session_tidy._pane_seat_of("w18:p7").key, E8_LEGACY_KEY)
            self.assertEqual(self.calls(), ["status server --json"])
            # herdr answers later: a miss older than the retry window is asked once more.
            self.status(running=True, socket=self.socket, session=None)
            found, asked = session_tidy._HERDR_ENDPOINT_SOCKETS[""]
            session_tidy._HERDR_ENDPOINT_SOCKETS[""] = (found, asked - session_tidy.HERDR_ENDPOINT_RETRY_SEC - 1)
            self.assertEqual(session_tidy._pane_seat_of("w18:p7").key, self.seat_key)
            self.assertEqual(session_tidy._pane_seat_of("w18:p7").key, self.seat_key)
        self.assertEqual(len(self.calls()), 2)

    def test_t4_an_inherited_socket_or_pane_is_used_without_asking_herdr(self):
        self.status(running=True, socket=self.socket, session=None)
        inherited = str(self.root / "inherited.sock")
        with self.process_env(HERDR_SOCKET_PATH=inherited, AGENT_HERDR_SESSION="left"):
            seat = session_tidy._pane_seat_of("w18:p7")
        endpoint = session_tidy._digest("herdr-endpoint", "socket", os.path.realpath(inherited))
        self.assertEqual((seat.endpoint, seat.socket_path), (endpoint, inherited))
        # A pane process keys from its env as that pane's own hooks do.
        with self.process_env(HERDR_PANE_ID="w18:p7"):
            seat = session_tidy._pane_seat_of("w18:p7")
        self.assertEqual((seat.key, seat.socket_path), (E8_LEGACY_KEY, ""))
        self.assertEqual(self.calls(), [])

    def test_t5_explicit_env_keeps_writer_and_lock_keys_even_when_herdr_answers(self):
        self.status(running=True, socket=self.socket, session=None)
        with self.process_env(HERDR_PANE_ID="w18:p7"):
            self.assertEqual(session_tidy._endpoint_scope({}), "")
            self.assertEqual(session_tidy._endpoint_scope(os.environ), "")
            self.assertEqual(session_tidy._endpoint_scope({"AGENT_HERDR_SESSION": "left"}),
                             session_tidy._digest("herdr-endpoint", "session", "left"))
            with mock.patch("pane_ownership.verified_pane", side_effect=lambda pane, *_a, **_k: pane):
                live = session_tidy.resolve_seat("claude", str(self.repo), sid="writer-sid")
            self.assertEqual((live.kind, live.key, live.endpoint), ("pane", E8_LEGACY_KEY, ""))
        self.assertEqual(self.calls(), [])

    def test_t6_e8_shaped_history_reaches_a_viewer_outside_herdr(self):
        from fleet.explorer import focus, nav
        from fleet.explorer.types import FocusKey, FocusRequest
        old, middle, current = E8_SIDS
        transcripts = {}
        for sid in E8_SIDS:
            transcripts[sid] = self.root / f"{sid}.jsonl"
            transcripts[sid].write_text('{"type":"message"}\n', encoding="utf-8")
        writer = {"HERDR_SOCKET_PATH": self.socket, "HERDR_PANE_ID": "w18:p7"}
        with self.process_env(**writer):
            seat = session_tidy._pane_seat_of("w18:p7", endpoint=session_tidy._endpoint_scope(writer),
                                              socket_path=self.socket)
            self.assertEqual(seat.key, self.seat_key)

            def card(sid, body, now):
                with session_tidy.seat_lock(seat.key):
                    session_tidy.write_card(seat, "claude", sid, body, cwd=str(self.repo), now=now)

            self.record(seat, "claude", old, source="startup", transcript=str(transcripts[old]), now=100)
            card(old, "# 7dcee068 첫 카드", 110)
            self.record(seat, "claude", old, "compact", now=150)
            self.record(seat, "claude", old, source="compact", transcript=str(transcripts[old]), now=151)
            card(old, "# 7dcee068 compact 뒤 카드", 160)
            self.record(seat, "claude", middle, source="clear", transcript=str(transcripts[middle]), now=200)
            card(middle, "# bbf14dfc 정리 전 카드", 210)
            self.record(seat, "claude", current, source="clear", transcript=str(transcripts[current]), now=300)
            with session_tidy.seat_lock(seat.key):
                session_tidy._append_ledger_entry(seat, {"ts": 301, "harness": "claude", "sid": current,
                                                         "event": "handover", "from": middle, "source": "clear"})
        self.assertEqual(self.calls(), [])

        session = {"session_id": current, "harness": "claude", "cwd": str(self.repo)}
        with self.process_env():
            self.status(running=True, socket=self.socket, session=None)
            entries, pane, health = focus.read_seat_history(session, "claude", current, pane="w18:p7")
            chain = nav.generation_chain(entries, "claude", current)
            with mock.patch.object(focus, "read_pane_map", return_value={current: "w18:p7"}):
                aux = focus.collect_aux(FocusRequest(FocusKey("*", "seats"), 1, 0, (f"claude:{current}",)),
                                        snapshot={"sessions": [session]})
        self.assertEqual((health.state, pane), ("success", "w18:p7"))
        self.assertEqual([(entry["sid"], entry["source"], entry["current"]) for entry in entries],
                         [(old, "compact", False), (middle, "clear", False), (current, "clear", True)])
        self.assertEqual(entries[-1]["relation"], "confirmed")
        self.assertEqual([(item["sid"], item["kind"]) for item in chain],
                         [(old, "new"), (old, "cont"), (middle, "new"), (current, "new")])
        self.assertEqual(aux.health.state, "success")
        self.assertEqual([entry["sid"] for entry in aux.source_records["seats"][f"claude:{current}"]],
                         [old, middle, current])
        self.assertEqual(self.calls(), ["status server --json"])

        # A missing physical endpoint does not erase exact native session
        # history. Only the confirmed middle -> current lineage is reachable;
        # old is merely a chronological neighbour in the unavailable pane.
        session_tidy._HERDR_ENDPOINT_SOCKETS.clear()
        with self.process_env(), mock.patch.object(session_tidy, "_herdr_executable", return_value=None):
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                      for path in session_tidy.state_root().rglob("*.json*")}
            entries, _pane, health = focus.read_seat_history(session, "claude", current, pane="w18:p7")
        self.assertEqual(health.state, "success")
        self.assertEqual([(entry["harness"], entry["sid"], entry["current"], entry["relation"])
                          for entry in entries],
                         [("claude", middle, False, "unknown"), ("claude", current, True, "confirmed")])
        self.assertNotIn(old, [entry["sid"] for entry in entries])
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}, before)

    def test_t7_legacy_ledger_is_read_only_for_its_exact_session_and_never_merged(self):
        self.status(running=True, socket=self.socket, session=None)
        with self.process_env():
            current = session_tidy._pane_seat_of("w:p")
            legacy = session_tidy.Seat("pane", session_tidy._digest("pane", "w:p"), "w:p", "claude")
            self.assertTrue(current.endpoint)
            self.record(current, "claude", "endpoint-now", source="startup", now=500)
            self.record(legacy, "claude", "legacy-old", source="startup", now=100)
            self.record(legacy, "claude", "legacy-now", source="clear", now=200)
            with session_tidy.seat_lock(legacy.key):
                session_tidy._append_ledger_entry(legacy, {"ts": 201, "harness": "claude", "sid": "legacy-now",
                                                           "event": "handover", "from": "legacy-old",
                                                           "source": "clear"})
            state = self.root / "state"
            before = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}

            def sids(seat, **kwargs):
                return [row["sid"] for row in session_tidy.seat_lineage(seat, **kwargs)]

            # The asked session lives only in the legacy ledger: that ledger alone is read.
            self.assertEqual(sids(current, harness="claude", sid="legacy-now"), ["legacy-now", "legacy-old"])
            self.assertEqual(sids(session_tidy.session_seat(current, "claude", "legacy-now")),
                             ["legacy-now", "legacy-old"])
            self.assertEqual(session_tidy.seat_lineage(current, harness="claude", sid="legacy-now")[0]["relation"],
                             "confirmed")
            # The endpoint ledger knows the session, or nobody does (pane id reuse): no legacy rows.
            self.assertEqual(sids(current, harness="claude", sid="endpoint-now"), ["endpoint-now"])
            self.assertEqual(sids(current, harness="claude", sid="reused-elsewhere"), ["endpoint-now"])
            # Another harness, or no session at all, never falls back.
            self.assertEqual(sids(current, harness="codex", sid="legacy-now"), [])
            self.assertEqual(sids(current), ["endpoint-now"])
            after = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_t8_codex_pane_lookup_reads_the_legacy_ledger_and_booking_after_the_normalized_key(self):
        self.status(running=True, socket=self.socket, session=None)

        def agent(pane, sid, cwd):
            return {"agent": "codex", "pane_id": pane, "cwd": str(cwd), "foreground_cwd": str(cwd),
                    "agent_session": {"kind": "id", "source": "herdr:codex", "value": sid}}

        elsewhere = self.root / "elsewhere"
        self.agents_file.write_text(json.dumps({"result": {"agents": [
            agent("w:p1", "t-old", self.repo), agent("w:p2", "t-x", elsewhere)]}}), encoding="utf-8")
        with self.process_env():
            legacy = session_tidy.Seat("pane", session_tidy._digest("pane", "w:p1"), "w:p1", "codex")
            normalized = session_tidy._pane_seat_of("w:p2")
            self.assertNotEqual(normalized.key, session_tidy._digest("pane", "w:p2"))
            self.assertEqual(session_tidy.codex_pane_for_session("t-B"), "")
            # A socket-less Codex hook kept writing w:p1 under the endpoint-free key.
            self.record(legacy, "codex", "t-B", "prompt")
            self.assertEqual(session_tidy.codex_pane_for_session("t-B"), "w:p1")
            self.record(normalized, "codex", "t-C", "prompt")
            self.assertEqual(session_tidy.codex_pane_for_session("t-C"), "w:p2")
            self.assertEqual(session_tidy.codex_pane_for_session("t-unknown"), "")

            # The first hook after a clear: the waiting booking under the legacy key is found ...
            self.book("w:p1", legacy.key, "t-old")
            self.assertEqual(session_tidy.codex_pane_for_session("t-new", str(self.repo), "clear"), "w:p1")
            # ... only while the normalized key holds no booking of its own.
            self.book("w:p1", session_tidy._pane_seat_of("w:p1").key, "t-old", status="cleared")
            self.assertEqual(session_tidy.codex_pane_for_session("t-new", str(self.repo), "clear"), "")


if __name__ == "__main__":
    unittest.main()
