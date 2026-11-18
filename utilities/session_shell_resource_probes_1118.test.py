"""11-18 (round 4 of PR #1715, review C1): what the panels' RESOURCES cost the compute hosts.

The shared producer reads the hosts (one SSH probe each) only for a display that draws the section: a side panel that is open asks
(`note_resources_wanted`, from `panel_projection(side=True)`); a status line alone, a bottom panel or no panel asks nothing. It reads
them at most once in `RESOURCE_SAMPLE_S` (60 s) however many windows ask, and not at all while Fleet is open: Fleet's own ten-second
sample is left for the producer (`share_resource_sample`) and a sample younger than the period, whoever took it, stands in for a probe.
The probes are counted with a fake probe function over simulated minutes; no host is read.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "utilities"), str(ROOT / "tools")]

START = 1_800_000_000.0
HOSTS = {"configured": True, "observed_at": START, "hosts": [{"host": "box", "self": True, "reachable": True, "cpu_utilization_pct": 10.0,
                                                                   "cpu_count": 8, "memory_used_mib": 10, "memory_total_mib": 100, "gpus": []}]}


class ProbeCountTest(unittest.TestCase):
    def setUp(self):
        import human_wait_state as state
        self.state = state
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp.name, "HOME": tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        reset = mock.patch.object(state, "_resources", {"started": 0.0, "at": 0.0, "snapshot": None, "thread": None, "on": False})
        reset.start()
        self.addCleanup(reset.stop)
        self.clock = [START]
        clock = mock.patch("time.time", lambda: self.clock[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.probes = {"producer": 0, "fleet": 0}

    def fake_probe(self, who):
        def probe(*_a, **_k):
            self.probes[who] += 1
            return json.loads(json.dumps(HOSTS))
        return probe

    def world(self):
        from tools.fleet.tests import readme_demo, readme_panel
        snapshot, hosts = readme_demo.snapshot(START)
        snapshot.observations = {**snapshot.observations, "basic": {"all_new": True}}
        return snapshot, readme_panel.World(snapshot, START, hosts)

    def run_scenario(self, side=0, bottom=0, fleet_until=0, seconds=300):
        """The producer's loop over ``seconds``: a pass every 30 s while a status line asks, a projection every 10 s for each open panel,
        Fleet's pump every 10 s while Fleet is open. Returns the probes the producer took and the ones Fleet took."""
        import tools.fleet.collectors as collectors
        from tools.fleet.collectors import compute_hosts
        from tools.fleet.tests import readme_panel
        state = self.state
        snapshot, world = self.world()
        sids = ["readme-claude-api", "readme-oc-ml", "readme-claude-ml"]
        root = state.state_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        state._resources["on"] = True
        with mock.patch.object(collectors, "collect_all", lambda **_k: snapshot), \
                mock.patch.object(compute_hosts, "collect", self.fake_probe("producer")), \
                mock.patch.object(state, "_request_producer", lambda *a, **k: None):
            for tick in range(0, seconds, 10):
                self.clock[0] = START + tick
                if tick % 30 == 0:
                    (root / ".demand").write_text(json.dumps({"claude\0readme-claude-api": self.clock[0]}))
                    os.chmod(root / ".demand", 0o600)
                world.now = self.clock[0]        # (the projection reads the clock as the world's moment)
                for sid in sids[:side]:
                    readme_panel.projection(world, "claude", sid, columns=44, rows=70, side=True)
                for sid in sids[:bottom]:
                    readme_panel.projection(world, "claude", sid, columns=100, rows=16, side=False)
                if tick < fleet_until:
                    state.share_resource_sample(self.fake_probe("fleet")())
                if tick % 30 == 0:
                    self.assertTrue(state._refresh_cache())
                    thread = state._resources["thread"]
                    if thread is not None:
                        thread.join(5)
        state._resources["on"] = False
        return self.probes["producer"], self.probes["fleet"], state._read_cache(now=START + seconds, max_age=float("inf")) or {}

    def test_a_status_line_alone_takes_no_host_sample(self):
        taken, _fleet, cache = self.run_scenario()
        self.assertEqual(taken, 0)
        self.assertFalse(cache.get("resources"))

    def test_a_bottom_panel_takes_none_either(self):
        taken, _fleet, cache = self.run_scenario(bottom=1)
        self.assertEqual(taken, 0)
        self.assertFalse(cache.get("resources"))

    def test_a_side_panel_takes_one_sample_a_minute_and_three_panels_take_no_more(self):
        one, _f, cache = self.run_scenario(side=1)
        self.assertTrue(cache.get("resources"))
        self.state._resources.update(started=0.0, at=0.0, snapshot=None)
        self.probes["producer"] = 0
        for name in (".resources-wanted", "resource-sample.json"):
            (self.state.state_root() / name).unlink(missing_ok=True)
        three, _f, _c = self.run_scenario(side=3)
        self.assertIn(one, (5, 6))
        self.assertEqual(three, one)
        self.assertLessEqual(one, 300 // int(self.state.RESOURCE_SAMPLE_S) + 1)

    def test_fleet_open_leaves_its_sample_and_the_panel_takes_no_extra_probe(self):
        taken, fleet, cache = self.run_scenario(side=1, fleet_until=300)
        self.assertEqual(taken, 0)
        self.assertEqual(fleet, 30)
        self.assertTrue(cache.get("resources"))

    def test_when_fleet_closes_the_producer_takes_over_once_the_sample_is_a_period_old(self):
        taken, fleet, _cache = self.run_scenario(side=1, fleet_until=150)
        self.assertEqual(fleet, 15)
        self.assertIn(taken, (2, 3))      # the last Fleet sample is at 140 s: the next probe is the pass at 210 s, then 270 s

    def test_the_ask_is_a_side_panels_alone_and_lapses(self):
        state = self.state
        self.assertFalse(state._resources_wanted(START))
        state.note_resources_wanted(START)
        self.assertTrue(state._resources_wanted(START + 1))
        self.assertTrue(state._resources_wanted(START + state.RESOURCE_WANT_S - 1))
        self.assertFalse(state._resources_wanted(START + state.RESOURCE_WANT_S + 1))
        marker = state.state_root() / ".resources-wanted"
        before = marker.stat().st_mtime
        state.note_resources_wanted(START + 5)              # touched at most every twenty seconds
        self.assertEqual(marker.stat().st_mtime, before)
        state.note_resources_wanted(START + 25)
        self.assertGreater(marker.stat().st_mtime, before)

    def test_a_projection_asks_only_from_an_open_side_panel(self):
        from tools.fleet.tests import readme_panel
        _snapshot, world = self.world()
        marker = self.state.state_root() / ".resources-wanted"
        for kwargs in ({"columns": 100, "rows": 16, "side": False}, {"columns": 44, "rows": 70}):
            readme_panel.projection(world, "claude", "readme-claude-api", **kwargs)
            self.assertFalse(marker.exists(), kwargs)
        readme_panel.projection(world, "claude", "readme-claude-api", columns=44, rows=70, side=True)
        self.assertTrue(marker.exists())

    def test_a_sample_left_by_fleet_is_used_and_an_old_or_foreign_one_is_not(self):
        state = self.state
        probe = self.fake_probe("producer")
        with mock.patch("tools.fleet.collectors.compute_hosts.collect", probe), \
                mock.patch.object(state.threading, "Thread", type("Quick", (), {
                    "__init__": lambda self, target, **_k: setattr(self, "t", target), "start": lambda self: self.t(), "is_alive": lambda self: False})):
            state.share_resource_sample(HOSTS)
            self.assertEqual(state._resource_sample(START + 10)[0], HOSTS)
            self.assertEqual(self.probes["producer"], 0)
            state._resource_sample(START + state.RESOURCE_SAMPLE_S + 1)           # a period old: the producer takes its own
            self.assertEqual(self.probes["producer"], 1)
            state.share_resource_sample({"error": "x"})                            # an error is not a sample
            state.share_resource_sample({"hosts": "no"})
            self.assertEqual(json.loads((state.state_root() / "resource-sample.json").read_text())["snapshot"], HOSTS)

    def test_the_host_name_is_cut_with_the_mark(self):
        state = self.state
        self.assertEqual(state._clipped("a" * 32, 32), "a" * 32)
        self.assertEqual(state._clipped("a" * 33, 32), "a" * 31 + "…")


if __name__ == "__main__":
    unittest.main()
