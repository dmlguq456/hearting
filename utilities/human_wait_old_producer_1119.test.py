"""11-19: a window of the installed release asks for the install-time handoff when an older release's producer holds the cache.

The panel cache every window shares is made by one producer, which starts from the file of the window that
asked. A window left open from an older release (v4.40.0: no `now` in a route row; no doers, peers or
resources) asked again after an install and took the producer's place, so every newer window drew that
release's rows. The producer now writes `reconnect_aware` into the cache (an older release builds the cache
afresh, so the key is absent there). A window of the installed release whose request finds a fresh cache
without it, a lock someone holds and a session it has not asked for in 60 seconds asks the handoff the
installer already runs (`--reconnect-cache`, detached) to replace that producer, with that handoff's proof.

The older producer is a fixture: a cache of its form (the release's own `publish_cache` out of a tag when git
has it), a lock held by this test, and the release's own reader run in a child process. The handoff itself
(signals, proof) is tested in `peer_runner_lifecycle.test.py` beside the install-time cases.
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
import human_wait_state as state  # noqa: E402
import peer_obligations  # noqa: E402

SID = "sid-old-window-neighbour"
KEY = "claude\0" + SID
QUESTION = "S1:claude:%s:1" % SID
OLD_ROW = {"id": "rt-0123456789abcdef", "title": "route of the window", "state": "working", "worker_stage": "execute",
           "stages": [{"name": "execute", "st": "active"}], "harness": "claude", "role": "owner"}
NEW_ROW = {**OLD_ROW, "now": "reading the cache producer", "field": "dev", "chain": [["plan", "done"]]}
OLD_TAGS = ("v4.40.0", "v4.51.0", "v4.52.0", "v4.62.3")
REAL_POPEN = subprocess.Popen
MARK = "reconnect_aware"


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
        self.assertTrue(str(self.root).startswith(self.tmp.name))   # every file below is in this test's own state folder
        self.held = []
        self.addCleanup(lambda: [os.close(fd) for fd in self.held])
        self.spawned = []
        self.commands = []
        spawn = mock.patch.object(state.subprocess, "Popen", side_effect=self.spawn)
        spawn.start()
        self.addCleanup(spawn.stop)
        self.installed = mock.patch.object(peer_obligations, "installed_release", return_value=ROOT)
        self.installed.start()
        self.addCleanup(self.installed.stop)

    def spawn(self, command, **kw):
        if command[2:] == ["--reconnect-cache"]:   # a handoff child: nothing runs here, the command and its env are kept
            self.spawned.append(command[2])
            self.commands.append((command, kw))
            return SimpleNamespace()
        if "pass_fds" not in kw:   # a child this test itself runs (git, an older release's code)
            return REAL_POPEN(command, **kw)
        self.spawned.append(command[2])
        self.held.append(os.dup(kw["pass_fds"][0]))   # the spawned producer holds its lock
        return SimpleNamespace()

    def hold(self, name=".projection.lock"):
        """What a producer does: holds the shared lock for its lifetime."""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.held.append(fd)

    def write_old_cache(self, now):
        """The file an older release's producer publishes: no `reconnect_aware`, and none of the keys that came later."""
        self.assertTrue(state.publish_cache(pass_result(now, OLD_ROW)))
        path = state._cache_path()
        value = json.loads(path.read_text(encoding="utf-8"))
        for name in (MARK, "session_doers", "session_peers", "resources"):
            value.pop(name, None)
        state._write(path, value)

    def view_questions(self, view):
        return ([item["id"] for item in view["items"]], view["by_session"].get(("claude", SID)), view["total"])


class RequestAsksTheHandoff(Fixture):
    def test_the_cache_names_the_producer_that_wrote_it(self):
        self.assertTrue(state.publish_cache(pass_result(time.time(), NEW_ROW)))
        value = json.loads(state._cache_path().read_text(encoding="utf-8"))
        self.assertIs(value.get(MARK), True)
        self.assertEqual(value["schema_version"], 3)   # the file's form is every release's `== 3`: not touched

    def test_all_conditions_start_the_handoff_once(self):
        self.write_old_cache(time.time())
        self.hold()
        state._request_producer("claude", SID)
        self.assertEqual(self.spawned, ["--reconnect-cache"])
        command, kw = self.commands[0]
        self.assertEqual(command, [sys.executable, str(Path(state.__file__).resolve()), "--reconnect-cache"])
        self.assertEqual(kw["env"]["AGENT_HOME"], str(ROOT))                 # as the installer starts it
        self.assertTrue(kw["start_new_session"])                              # detached: the render does not wait
        state._request_producer("claude", SID)                                # the same window within 60 seconds: not `changed`
        self.assertEqual(self.spawned, ["--reconnect-cache"])

    def test_without_one_condition_it_is_not_started(self):
        now = time.time()
        cases = {
            "the cache names its producer": dict(cache=lambda: self.assertTrue(state.publish_cache(pass_result(now, NEW_ROW))), args=("claude", SID)),
            "the cache is stale": dict(cache=lambda: self.write_old_cache(now - 120), args=("claude", SID)),
            "the request is not a changed one (no session asked)": dict(cache=lambda: self.write_old_cache(now), args=()),
            "the session is not one that can be recorded": dict(cache=lambda: self.write_old_cache(now), args=("bogus", SID)),
            "the window is not the installed release": dict(cache=lambda: self.write_old_cache(now), args=("claude", SID),
                                                            installed=ROOT / "elsewhere"),
            "the window is a checkout": dict(cache=lambda: self.write_old_cache(now), args=("claude", SID), installed=None),
        }
        for name, case in cases.items():
            with self.subTest(name):
                for path in self.root.glob("*"):      # each case starts from an empty state folder and a held lock
                    path.unlink()
                for path in self.root.glob(".*"):
                    path.unlink()
                self.spawned.clear()
                self.hold()
                case["cache"]()
                with mock.patch.object(peer_obligations, "installed_release",
                                       return_value=case.get("installed", ROOT)):
                    state._request_producer(*case["args"])
                    state._request_producer(*case["args"])
                self.assertEqual(self.spawned, [])

    def test_a_free_lock_starts_the_ordinary_producer_and_not_the_handoff(self):
        self.write_old_cache(time.time())
        state._request_producer("claude", SID)
        self.assertEqual(self.spawned, ["--refresh-cache"])

    def test_one_request_per_session_a_minute_bounds_the_unreplaceable_producer(self):
        """A producer the handoff keeps refusing (a checkout's, two of them, another env) is asked about once
        per session per `_REQUEST_TTL`: the `.demand` record `changed` already reads, no file of its own."""
        self.write_old_cache(time.time())
        self.hold()
        sessions = ["sid-window-%02d" % number for number in range(12)]
        for _ in range(5):                       # five renders of each of twelve windows inside one minute
            for sid in sessions:
                state._request_producer("claude", sid)
        self.assertEqual(self.spawned, ["--reconnect-cache"] * 12)
        demands = state._route_demands()
        self.assertEqual(sorted(demands), sorted("claude\0" + sid for sid in sessions))
        # A minute on, each asks once more (the record is the only memory).
        with mock.patch.object(state.time, "time", return_value=time.time() + state._REQUEST_TTL + 1):
            self.write_old_cache(time.time())
            self.spawned.clear()
            for sid in sessions:
                state._request_producer("claude", sid)
        self.assertEqual(len(self.spawned), 12)


class NoOlderProducer(Fixture):
    def test_the_producer_count_is_what_it_was(self):
        state._request_producer("claude", SID)                  # no cache: one shared producer
        self.assertEqual(self.spawned, ["--refresh-cache"])
        self.assertTrue(state.publish_cache(pass_result(time.time(), NEW_ROW)))
        state._request_producer("claude", SID)                  # a fresh cache of this release: nothing
        self.assertEqual(self.spawned, ["--refresh-cache"])
        state._request_producer("claude", "sid-another-window")  # another window, a held lock, a marked cache: nothing
        self.assertEqual(self.spawned, ["--refresh-cache"])
        self.assertEqual(state.cached_view(request=False)["route_panels"][KEY], [NEW_ROW])

    def test_the_screen_before_the_replacement_is_the_screen_now(self):
        self.write_old_cache(time.time())
        view = state.cached_view(request=False)
        self.assertEqual(view["route_panels"], {KEY: [OLD_ROW]})       # the rows are drawn as they are: no line is emptied
        self.assertEqual(self.view_questions(view), ([QUESTION], [QUESTION], 1))
        self.assertEqual(state.cached_statusline("claude", SID), ((1, 1), "route 1"))


class OlderReleaseCode(Fixture):
    """The release's own code, from its tag. Skipped where git or the tag is not there."""

    def release_file(self, tag):
        try:
            shown = subprocess.run(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "show", f"{tag}:utilities/human_wait_state.py"],
                                   capture_output=True, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp.name})
        except OSError:
            self.skipTest("git is not available")
        if shown.returncode != 0:
            self.skipTest(f"{tag} is not in this checkout")
        directory = Path(self.tmp.name) / ("release-" + tag)
        directory.mkdir(exist_ok=True)
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

    def read_as_old(self, tag):
        return self.run_old(tag, (
            "view = hws.cached_view(request=False)\ncounts, label = hws.cached_statusline('claude', %r)\n"
            "print(json.dumps({'ids': [i['id'] for i in view['items']], 'total': view['total'],\n"
            " 'by_session': view['by_session'].get(('claude', %r)), 'panels': view['route_panels'],\n"
            " 'counts': counts, 'label': label}))\n" % (SID, SID)))

    def test_a_cache_an_older_producer_published_asks_the_handoff_and_keeps_its_question(self):
        now = time.time()
        self.hold()
        for tag in OLD_TAGS:
            with self.subTest(tag=tag):
                self.spawned.clear()
                for path in self.root.glob(".demand*"):
                    if path.name != ".demand.lock":
                        path.unlink()
                self.assertTrue(self.run_old(tag, "print(json.dumps(hws.publish_cache(%r)))\n" % pass_result(now, OLD_ROW)))
                self.assertNotIn(MARK, json.loads(state._cache_path().read_text(encoding="utf-8")))
                view = state.cached_view(request=False)
                self.assertEqual(self.view_questions(view), ([QUESTION], [QUESTION], 1))   # before the replacement: as now
                state._request_producer("claude", SID)
                self.assertEqual(self.spawned, ["--reconnect-cache"])

    def test_the_cache_of_this_release_is_read_by_older_windows_without_error_and_with_the_same_question(self):
        now = time.time()
        self.assertTrue(state.publish_cache(pass_result(now, NEW_ROW)))
        for tag in OLD_TAGS:
            with self.subTest(tag=tag):
                self.assertEqual(self.read_as_old(tag), {"ids": [QUESTION], "total": 1, "by_session": [QUESTION],
                                                         "panels": {KEY: [NEW_ROW]}, "counts": [1, 1], "label": "route 1"})
        view = state.cached_view(request=False)                           # and the window of this release, before and after alike
        self.assertEqual(self.view_questions(view), ([QUESTION], [QUESTION], 1))


if __name__ == "__main__":
    unittest.main()
