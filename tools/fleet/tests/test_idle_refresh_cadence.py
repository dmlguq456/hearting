"""Change-driven basic cadence and the unchanged tag snapshot."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
from fleet import native_events, session_tags
from fleet.collectors import procscan
from fleet.model import Session


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class IdleCadenceTest(unittest.TestCase):
    def test_silence_stretches_and_any_mark_restores_the_normal_cadence(self):
        cadence = native_events.IdleCadence(30.0)
        self.assertTrue(cadence.due(0.0))
        cadence.started(0.0)
        self.assertFalse(cadence.due(10.0))
        self.assertTrue(cadence.due(30.0))
        cadence.started(30.0)
        cadence.mark()
        self.assertTrue(cadence.due(31.0))
        cadence.started(31.0)
        self.assertFalse(cadence.dirty)

    def test_content_writes_run_at_most_once_per_content_interval(self):
        cadence = native_events.IdleCadence(30.0, 10.0)
        cadence.started(0.0)
        cadence.mark_content()
        self.assertFalse(cadence.due(5.0))
        self.assertTrue(cadence.due(10.0))
        cadence.started(10.0)
        self.assertFalse(cadence.due(15.0))   # nothing new since that run
        self.assertTrue(cadence.due(40.0))    # fallback
        cadence.mark_content()
        cadence.mark()                        # structural change wins at once
        self.assertTrue(cadence.due(11.0))
        unthrottled = native_events.IdleCadence(30.0, None)
        unthrottled.started(0.0)
        unthrottled.mark_content()
        self.assertTrue(unthrottled.due(0.5))

    def test_no_installed_watch_keeps_the_periodic_cadence(self):
        cadence = native_events.IdleCadence(30.0)
        cadence.started(0.0)
        self.assertTrue(cadence.due(1.0, watching=False))
        self.assertTrue(native_events.IdleCadence(None).due(1.0))

    def test_interval_override(self):
        resolve = native_events.idle_refresh_interval
        self.assertEqual(resolve(2.0, env={}), native_events.DEFAULT_IDLE_REFRESH)
        self.assertEqual(resolve(2.0, env={"FLEET_IDLE_REFRESH_INTERVAL": "45"}), 45.0)
        self.assertEqual(resolve(2.0, env={"FLEET_IDLE_REFRESH_INTERVAL": "0.5"}), 2.0)
        self.assertIsNone(resolve(2.0, env={"FLEET_IDLE_REFRESH_INTERVAL": "0"}))
        content = native_events.content_refresh_interval
        self.assertEqual(content(2.0, env={}), native_events.DEFAULT_CONTENT_REFRESH)
        self.assertEqual(content(2.0, env={"FLEET_CONTENT_REFRESH_INTERVAL": "5"}), 5.0)
        self.assertIsNone(content(2.0, env={"FLEET_CONTENT_REFRESH_INTERVAL": "0"}))
        self.assertEqual(resolve(2.0, env={"FLEET_IDLE_REFRESH_INTERVAL": "x"}),
                         native_events.DEFAULT_IDLE_REFRESH)


class BasicEventsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.events = native_events.NativeDisplayEvents()
        self.addCleanup(self.events.stop)

    def test_writes_mark_dirty_but_an_untouched_read_write_close_does_not(self):
        watched = self.root / "jobs.log"
        watched.write_text("a\n")
        self.events.configure([str(watched)], [(str(self.root / "registry"), "")])
        self.assertTrue(wait_for(lambda: self.events.ready.is_set() and self.events.watching))
        self.assertTrue(self.events.take_changed())  # the post-registration read
        with open(watched, "r+"):
            pass  # a SQLite-style reader: opened read-write, closed untouched
        time.sleep(0.2)
        self.assertFalse(self.events.take_changed())
        with open(watched, "a") as handle:
            handle.write("b\n")
        self.assertTrue(wait_for(self.events.take_changed))
        (self.root / "registry").mkdir()
        self.assertTrue(wait_for(self.events.take_changed))
        (self.root / "registry" / "123.json").write_text("{}")
        self.assertTrue(wait_for(self.events.take_changed))

    def test_unchanged_sources_keep_one_watch_set(self):
        watched = self.root / "jobs.log"
        watched.write_text("")
        self.events.configure([str(watched)])
        self.assertTrue(wait_for(lambda: self.events.ready.is_set()))
        self.assertTrue(self.events.take_changed())
        self.events.configure([str(watched)])
        time.sleep(0.2)
        self.assertFalse(self.events.take_changed())

    def test_stop_clears_watching(self):
        self.events.configure([str(self.root / "x")])
        self.assertTrue(wait_for(lambda: self.events.watching))
        self.events.stop()
        self.assertFalse(self.events.watching)


class ProcessBirthsTest(unittest.TestCase):
    def test_a_new_harness_like_process_marks_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proc = Path(tmp.name)
        clock = [0.0]

        def spawn(pid, comm):
            (proc / str(pid)).mkdir()
            (proc / str(pid) / "comm").write_text(comm + "\n")

        spawn(10, "bash")
        births = native_events.ProcessBirths(clock=lambda: clock[0], proc_root=str(proc))
        births._uid = os.stat(proc).st_uid  # the fixture owner stands in for this user
        self.assertFalse(births.changed())          # the first listing is the baseline
        spawn(11, "python3")                        # forked, not yet exec'd
        self.assertFalse(births.changed())
        (proc / "11" / "comm").write_text("codex\n")  # exec within the young window
        clock[0] = 2.0
        self.assertTrue(births.changed())
        self.assertFalse(births.changed())          # one mark per process
        spawn(12, "2.1.322")                        # native Claude names itself by version
        self.assertTrue(births.changed())
        spawn(13, "git")
        self.assertFalse(births.changed())
        clock[0] = 20.0
        (proc / "13" / "comm").write_text("claude\n")
        self.assertFalse(births.changed())          # too old to be a new session start


class BasicEventSourcesTest(unittest.TestCase):
    def test_live_rows_contribute_files_and_processes_finished_rows_do_not(self):
        pid = os.getpid()
        start = procscan.read_proc_start(pid)
        live = SimpleNamespace(pid=pid, proc_start=start, _transcript_path="/t/live.jsonl",
                               harness="claude")
        ended = SimpleNamespace(pid=pid, proc_start="1", log_path="/t/ended.log")
        resource = SimpleNamespace(pid=pid, starttime=start, log_path="/t/run.log",
                                   registry_path="/t/run.json")
        snapshot = SimpleNamespace(sessions=[live], jobs=[ended], resources=[resource])
        display = (("/home/u/.claude/.statusline/s.json", "/t/rollout.jsonl"),
                   (("/state/codex", "session-shell-"),), ())
        from fleet import session_registry
        registry = os.path.join(session_registry._dir_for("codex"), "77.json")
        display = (display[0] + (registry,), display[1], display[2])
        structural, content = native_events.basic_event_sources(snapshot, display)
        paths, patterns, processes = structural
        changed = content[0]
        self.assertIn("/t/live.jsonl", changed)
        self.assertIn("/t/run.log", changed)
        self.assertIn("/t/rollout.jsonl", changed)
        self.assertIn("/t/run.json", paths)
        self.assertIn(registry, paths)
        for path in ("/t/live.jsonl", "/t/run.log", "/t/rollout.jsonl"):
            self.assertNotIn(path, paths)
        self.assertNotIn("/t/ended.log", paths + changed)
        self.assertNotIn("/home/u/.claude/.statusline/s.json", paths + changed)
        self.assertIn((pid, start), processes)
        self.assertIn(("/state/codex", "session-shell-"), patterns)
        self.assertIn((session_registry._dir_for("claude"), ""), patterns)
        from fleet import route_chain
        self.assertIn((os.path.join(route_chain.state_root(), "codex"), ""), patterns)
        from fleet.collectors import dispatch
        self.assertIn(str(dispatch._jobs_path()), paths)
        self.assertEqual(native_events.basic_event_sources(snapshot, display),
                         (structural, content))


class UnchangedTagSnapshotTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root),
                                             "FLEET_SESSION_REGISTRY_DIR": str(self.root / "registry"),
                                             "CLAUDE_CONFIG_DIR": str(self.root / "claude")})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch("fleet.collectors.herdr.list_agents", return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    def test_identical_census_keeps_the_file_and_marks_only_its_generation(self):
        session = Session(harness="codex", session_id="sid-unchanged", started_at=10,
                          pid=os.getpid(), proc_start=procscan.read_proc_start(os.getpid()))
        session_tags.assign([session])
        path = session_tags._path()
        before_bytes, before = path.read_bytes(), os.stat(path)
        time.sleep(0.01)
        session_tags.assign([session])
        after = os.stat(path)
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertGreater(after.st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(list(path.parent.glob(".tag-assignments-*")))
        tag = session_tags.assigned_tag("codex", "sid-unchanged")
        self.assertIsNotNone(tag)
        # A real change still rewrites the snapshot.
        other = Session(harness="opencode", session_id="sid-new", started_at=11,
                        pid=os.getpid(), proc_start=procscan.read_proc_start(os.getpid()))
        session_tags.assign([session, other])
        rows = json.loads(path.read_text())["assignments"]
        self.assertEqual({row["session_id"] for row in rows}, {"sid-unchanged", "sid-new"})
        self.assertEqual(session_tags.assigned_tag("codex", "sid-unchanged"), tag)


if __name__ == "__main__":
    unittest.main()
