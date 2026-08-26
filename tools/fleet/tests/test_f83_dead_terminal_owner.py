"""F-83 (2026-08-26): a supervisor-closed dead depth-1 owner stays on the board — as a ✕
card under its session — while its route still has open work, instead of aging off with
the afterglow window and leaving the session to the legacy multi-line stage surface."""
import json
import os
import tempfile
import unittest
from unittest import mock

from tools.fleet.collectors import dispatch

_ROW = ("2026-07-19T00:00:00Z\tdone\t/r\t/w\t{slug}\t"
        "route_id={rid},route_file={rf},owner_route_id={rid},owner_route_file={rf},"
        "worker_type=owner,attempt_id={att},pid=999999990,pid_start=1,"
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "fallback_hop=same-harness-headless,note={note},failure_class=runtime\n")


class DeadTerminalOwnerTest(unittest.TestCase):
    def _route(self, td, rid):
        record = {"route_id": rid, "nodes": [
            {"id": "plan", "depends_on": []}, {"id": "execute", "depends_on": ["plan"]},
            {"id": "test", "depends_on": ["execute"]}, {"id": "report", "depends_on": ["test"]}]}
        path = os.path.join(td, "route.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return path

    def _markers(self, home, rid, nodes):
        d = os.path.join(home, ".dispatch", "completion", rid)
        os.makedirs(d, exist_ok=True)
        for n in nodes:
            with open(os.path.join(d, "%s.json" % n), "w", encoding="utf-8") as fh:
                json.dump({"node_id": n}, fh)

    def _collect(self, td, home, rows):
        jobs_path = os.path.join(td, "jobs.log")
        with open(jobs_path, "w", encoding="utf-8") as fh:
            fh.write("".join(rows))
        with mock.patch.object(dispatch.procscan, "_ps_lines", return_value=[]), \
             mock.patch.dict(os.environ, {"AGENT_HOME": home}):
            return dispatch.collect(jobs_path=jobs_path)

    def test_route_open_dead_owner_is_retained_as_dead_card(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            rid = "rt-f83-open"
            self._markers(home, rid, ["plan"])
            rf = self._route(td, rid)
            jobs = self._collect(td, home, [_ROW.format(
                slug="owner-f83", rid=rid, rf=rf, att="att-f83-owner", note="dead-runtime-exit")])
            owner = next(j for j in jobs if j.slug == "owner-f83")
            self.assertEqual(owner.liveness, "dead")
            self.assertEqual(owner.note, "dead-runtime-exit")
            self.assertEqual(owner.resume_boundary, "execute")
            self.assertTrue(owner._dead_terminal_owner)
            self.assertFalse(owner.afterglow)

    def test_route_complete_dead_owner_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            rid = "rt-f83-done"
            self._markers(home, rid, ["plan", "execute", "test", "report"])
            rf = self._route(td, rid)
            jobs = self._collect(td, home, [_ROW.format(
                slug="owner-f83-done", rid=rid, rf=rf, att="att-f83-done", note="dead-runtime-exit")])
            self.assertEqual([j.slug for j in jobs if j.slug == "owner-f83-done"], [])

    def test_superseded_dead_owner_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            rid = "rt-f83-sup"
            self._markers(home, rid, ["plan"])
            rf = self._route(td, rid)
            rows = [_ROW.format(slug="owner-f83-r1", rid=rid, rf=rf, att="att-f83-r1",
                                note="dead-runtime-exit"),
                    _ROW.format(slug="owner-f83-r2", rid=rid, rf=rf, att="att-f83-r2",
                                note="dead-runtime-exit").replace("\tdone\t", "\topen\t")]
            jobs = self._collect(td, home, rows)
            self.assertEqual([j.slug for j in jobs if j.slug == "owner-f83-r1"], [])

    def test_plain_done_owner_still_ages_off(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            rid = "rt-f83-plain"
            self._markers(home, rid, ["plan"])
            rf = self._route(td, rid)
            row = _ROW.format(slug="owner-f83-plain", rid=rid, rf=rf, att="att-f83-plain",
                              note="completed-supervisor").replace(",failure_class=runtime", "")
            jobs = self._collect(td, home, [row])
            self.assertEqual([j.slug for j in jobs if j.slug == "owner-f83-plain"], [])


if __name__ == "__main__":
    unittest.main()
