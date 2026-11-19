"""11-19: a window of an older release keeps the shared panel cache; a newer window must not draw its route rows.

The route values (`route_panels`, `route_status`, doers, peers, resources) sit in the one cache that every
window shares, made by one producer, which starts from the file of the window that asked. A window left
open from an older release (v4.40.0: no `now` in a route row, no `field`, `chain` or `called`; no doers,
peers or resources) asked again after an install and took the producer's place back, so every newer window
drew that release's rows (the route line lost its NOW). The cache now names the form of the rows it carries;
a window that finds an older form (no name: the oldest) keeps the questions and the counts and takes the
route values from a panels producer of its own release (`panels.json`, its own lock), which ends when the
shared producer is of this release. Questions are not read or written by it.

The old producer is a fixture: a cache of its form (the same file the release wrote, here also made by the
release's own `publish_cache` out of a tag when git has it), a lock held by this test, and the release's own
reader run in a child process on the files the new code leaves.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import fcntl
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import human_gate_observation as observation  # noqa: E402
import human_wait_state as state  # noqa: E402

SID = "sid-old-window-neighbour"
KEY = "claude\0" + SID
QUESTION = "S1:claude:%s:1" % SID
OLD_ROW = {"id": "rt-0123456789abcdef", "title": "route of the window", "state": "working", "worker_stage": "execute",
           "stages": [{"name": "execute", "st": "active"}], "harness": "claude", "role": "owner"}
NEW_ROW = {**OLD_ROW, "now": "reading the cache producer", "field": "dev", "chain": [["plan", "done"]]}
OLD_TAGS = ("v4.40.0", "v4.51.0", "v4.62.3")
REAL_POPEN = subprocess.Popen


def question(since):
    return {"id": QUESTION, "source": "S1", "kind": "decision", "question": None, "choices": [], "route_id": None,
            "attempt_id": None, "gate_id": None, "since": since, "stale": False,
            "owner": {"harness": "claude", "session_id": SID}, "attribution": "exact"}


def pass_result(now, row):
    return {"items": [question(now)], "complete": True, "observed_at": now, "health": [],
            "route_sessions": {KEY: now}, "route_status": {KEY: "route 1"}, "route_panels": {KEY: [row]}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hop-")
        self.addCleanup(self.tmp.cleanup)
        self.state_home = Path(self.tmp.name) / "state"
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_home), "HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("_WAKE_FAILED", True), ("_WAKE_EVENTS", None), ("_NEXT_WAKE", None), ("_WATCH_STAT", ())):
            patch = mock.patch.object(state, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.root = state.state_root()
        self.held = []
        self.addCleanup(lambda: [os.close(fd) for fd in self.held])
        self.spawned = []
        spawn = mock.patch.object(state.subprocess, "Popen", side_effect=self.spawn)
        spawn.start()
        self.addCleanup(spawn.stop)

    def spawn(self, command, **kw):
        if "pass_fds" not in kw:   # a child this test itself runs (git, an older release's code)
            return REAL_POPEN(command, **kw)
        self.spawned.append(command[2])
        self.held.append(os.dup(kw["pass_fds"][0]))   # the spawned producer holds its lock
        return SimpleNamespace()

    def hold(self, name):
        """What an older producer does: holds the shared lock for its lifetime."""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.held.append(fd)

    def write_old_form_cache(self, now):
        """The file an older release's producer publishes: the same rows, but not the form name and not the
        keys that came later (doers, peers, resources)."""
        self.assertTrue(state.publish_cache(pass_result(now, OLD_ROW)))
        path = state._cache_path()
        value = json.loads(path.read_text(encoding="utf-8"))
        for name in ("route_row_form", "session_doers", "session_peers", "resources"):
            value.pop(name)
        state._write(path, value)

    def view_questions(self, view):
        return ([item["id"] for item in view["items"]], view["by_session"].get(("claude", SID)), view["total"])


class NewWindowBesideOlderProducer(Fixture):
    def test_the_older_rows_are_not_drawn_and_the_question_stays(self):
        self.write_old_form_cache(time.time())
        view = state.cached_view(request=False)
        self.assertEqual(view["route_panels"], {})          # not the older row (it has no NOW)
        self.assertEqual(view["session_doers"], {})
        self.assertEqual(self.view_questions(view), ([QUESTION], [QUESTION], 1))
        (counts, label) = state.cached_statusline("claude", SID)
        self.assertEqual((counts, label), ((1, 1), ""))      # the count is the producer's; its label is not drawn

    def test_the_panels_producers_rows_are_drawn_with_the_older_producers_questions(self):
        now = time.time()
        self.write_old_form_cache(now)
        shared = (state._cache_path().read_bytes(), state._fleet_cache_path().read_bytes())

        def observed(result, demands):
            result.update(pass_result(now, NEW_ROW))
            result["session_doers"] = {KEY: {"model": "claude-opus-5-5"}}
            return None
        with mock.patch.object(state, "_observe_routes", side_effect=observed), \
             mock.patch.object(observation, "read_answer_waits", side_effect=AssertionError("reads no question")), \
             mock.patch.object(state, "_route_demands", return_value={KEY: now}), \
             mock.patch.object(state, "_notify_result", side_effect=AssertionError("notifies nothing")):
            self.assertTrue(state._refresh_panels())
        view = state.cached_view(request=False)
        self.assertEqual(view["route_panels"][KEY][0]["now"], "reading the cache producer")
        self.assertEqual(view["session_doers"], {KEY: {"model": "claude-opus-5-5"}})
        self.assertEqual(self.view_questions(view), ([QUESTION], [QUESTION], 1))
        self.assertEqual(state.cached_statusline("claude", SID), ((1, 1), "route 1"))
        # Neither the cache (nor its answer records) nor the Fleet cache was touched.
        self.assertEqual(shared, (state._cache_path().read_bytes(), state._fleet_cache_path().read_bytes()))

    def test_a_panels_file_of_an_older_form_or_age_is_not_drawn(self):
        now = time.time()
        self.write_old_form_cache(now)
        for panels in ({"observed_at": now, **state._display_projection(pass_result(now, NEW_ROW))},       # no form name
                       {"observed_at": now - 400, "route_row_form": state._ROUTE_ROW_FORM,
                        **state._display_projection(pass_result(now, NEW_ROW))}):                          # stale
            state._write(state._panels_path(), panels)
            self.assertEqual(state.cached_view(request=False)["route_panels"], {})

    def test_a_newer_form_than_this_release_is_drawn(self):
        # An older window reads what a newer producer wrote (as this window did before this change).
        with mock.patch.object(state, "_ROUTE_ROW_FORM", 1):
            self.assertTrue(state.publish_cache(pass_result(time.time(), NEW_ROW)))
        with mock.patch.object(state, "_ROUTE_ROW_FORM", 0):
            self.assertEqual(state.cached_view(request=False)["route_panels"][KEY], [NEW_ROW])

    def test_one_panels_producer_starts_beside_the_older_producer_and_no_second_cache_producer(self):
        now = time.time()
        self.write_old_form_cache(now)
        self.hold(".projection.lock")
        state._request_producer("claude", SID)
        self.assertEqual(self.spawned, ["--refresh-panels"])
        state._request_producer("claude", SID)   # its lock is held: a window asking again starts nothing
        self.assertEqual(self.spawned, ["--refresh-panels"])
        # Once its rows are written and fresh, a window asking does not even try.
        state._write(state._panels_path(), {"observed_at": time.time(), "route_row_form": state._ROUTE_ROW_FORM,
                                            **state._display_projection(pass_result(now, NEW_ROW))})
        with mock.patch.object(state, "_ensure_producer") as ensure:
            state._request_producer("claude", SID)
        ensure.assert_not_called()

    def test_the_older_cache_going_stale_hands_the_shared_producer_to_this_release(self):
        self.write_old_form_cache(time.time() - 120)            # past the request age, inside the display age
        state._request_producer("claude", SID)
        self.assertEqual(self.spawned, ["--refresh-cache"])     # the lock was free: the producer is this release's

    def test_the_panels_producer_ends_when_the_shared_cache_is_of_this_release(self):
        self.write_old_form_cache(time.time())
        self.root.joinpath(".demand").touch()
        fd = os.open(self.root / ".panels.lock", os.O_CREAT | os.O_RDWR, 0o600)
        passes = []

        def sleep(*_):   # the older producer is replaced while this one sleeps
            self.assertTrue(state.publish_cache(pass_result(time.time(), NEW_ROW)))
        with mock.patch.object(state, "_panels_lane", True), \
             mock.patch.object(state, "_follow_installed_release"), \
             mock.patch.object(state, "_timed_refresh", side_effect=lambda: passes.append(1) or (None, 0.0)), \
             mock.patch.object(state, "_sleep_until_registry_change", side_effect=sleep):
            state._producer_loop(fd)
        self.assertEqual(len(passes), 2)


class NoOlderProducer(Fixture):
    def test_the_producer_count_is_what_it_was(self):
        state._request_producer("claude", SID)                  # no cache: one shared producer, no panels producer
        self.assertEqual(self.spawned, ["--refresh-cache"])
        self.assertTrue(state.publish_cache(pass_result(time.time(), NEW_ROW)))
        state._request_producer("claude", SID)                  # a fresh cache of this release: nothing
        self.assertEqual(self.spawned, ["--refresh-cache"])
        self.assertEqual(state.cached_view(request=False)["route_panels"][KEY], [NEW_ROW])
        self.assertFalse(state._panels_path().exists())


class OlderReleaseCode(Fixture):
    """The release's own code, from its tag."""

    def release_file(self, tag):
        shown = subprocess.run(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "show", f"{tag}:utilities/human_wait_state.py"],
                               capture_output=True, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp.name})
        if shown.returncode != 0:
            self.skipTest(f"{tag} is not in this checkout")
        directory = Path(self.tmp.name) / ("release-" + tag)
        directory.mkdir()
        (directory / "human_wait_state.py").write_bytes(shown.stdout)
        return directory

    def run_old(self, tag, body):
        directory = self.release_file(tag)
        script = ("import json, sys\nsys.path.insert(0, %r)\nimport human_wait_state as hws\n"
                  "hws._request_producer = lambda *a, **k: None\n" % str(directory)) + body
        done = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True,
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp.name,
                                   "XDG_STATE_HOME": str(self.state_home)})
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return json.loads(done.stdout)

    def test_older_windows_draw_their_screen_from_the_cache_and_files_this_release_leaves(self):
        now = time.time()
        self.assertTrue(state.publish_cache(pass_result(now, NEW_ROW)))
        state._write(state._panels_path(), {"observed_at": now, "route_row_form": state._ROUTE_ROW_FORM,
                                            **state._display_projection(pass_result(now, NEW_ROW))})
        self.hold(".panels.lock")
        for tag in OLD_TAGS:
            with self.subTest(tag=tag):
                seen = self.run_old(tag, (
                    "view = hws.cached_view(request=False)\ncounts, label = hws.cached_statusline('claude', %r)\n"
                    "print(json.dumps({'ids': [i['id'] for i in view['items']], 'total': view['total'],\n"
                    " 'panels': view['route_panels'], 'counts': counts, 'label': label}))\n" % SID))
                self.assertEqual(seen, {"ids": [QUESTION], "total": 1, "panels": {KEY: [NEW_ROW]},
                                        "counts": [1, 1], "label": "route 1"})

    def test_a_cache_an_older_producer_published_is_read_here_with_its_question(self):
        now = time.time()
        for tag in OLD_TAGS:
            with self.subTest(tag=tag):
                published = self.run_old(tag, "print(json.dumps(hws.publish_cache(%r)))\n" % pass_result(now, OLD_ROW))
                self.assertTrue(published)
                self.assertNotIn("route_row_form", json.loads(state._cache_path().read_text(encoding="utf-8")))
                view = state.cached_view(request=False)
                self.assertEqual(self.view_questions(view), ([QUESTION], [QUESTION], 1))
                self.assertEqual(view["route_panels"], {})


if __name__ == "__main__":
    unittest.main()
