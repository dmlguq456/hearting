#!/usr/bin/env python3
"""SD-155: `verified_route_lineage` / `canonical_route_path` unit tests.

Fixtures build raw route dicts by hand -- the leaf itself only reads
`route_id`, `route_hash`, `source_route_id`, `source_route_hash`,
`continuation_contract_version`, `artifact_root`, `cwd`, `capability` -- so
these tests never need `capability-route.py`'s full compiler (that parity is
covered separately by `capability_route.test.py::SourceCensusTest` and the
`ROUTE.verified_route_lineage is route_lineage.verified_route_lineage` check).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import route_identity  # noqa: E402
import route_lineage as RL  # noqa: E402


def _route(**overrides):
    base = {
        "route_id": "rt-" + "0" * 16,
        "artifact_root": "/tmp/fixture-root",
        "cwd": "/tmp/fixture-cwd",
        "capability": "autopilot-code",
        "nodes": [],
    }
    base.update(overrides)
    base.pop("route_hash", None)
    base["route_hash"] = route_identity.route_hash(base)
    base["route_id"] = "rt-" + base["route_hash"].split(":", 1)[1][:16]
    return base


class RouteLineageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, route):
        path = RL.canonical_route_path(self.root, route["route_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(route), encoding="utf-8")
        return path

    def _continuation(self, parent, **overrides):
        fields = {
            "artifact_root": parent["artifact_root"], "cwd": parent["cwd"], "capability": parent["capability"],
            "continuation_contract_version": 1, "source_route_id": parent["route_id"],
            "source_route_hash": parent["route_hash"],
        }
        fields.update(overrides)
        return _route(**fields)

    def test_canonical_route_path_is_under_runtime_routes(self):
        path = RL.canonical_route_path(self.root, "rt-" + "a" * 16)
        self.assertEqual(path, self.root.resolve() / ".runtime" / "routes" / ("rt-" + "a" * 16 + ".json"))

    def test_a_route_with_no_continuation_version_is_its_own_lineage(self):
        a = _route()
        self._write(a)
        self.assertEqual(RL.verified_route_lineage(a, artifact_root=self.root), [a])

    def test_two_generation_chain_passes_nearest_first(self):
        a = _route()
        self._write(a)
        b = self._continuation(a)
        self._write(b)
        lineage = RL.verified_route_lineage(b, artifact_root=self.root)
        self.assertEqual([r["route_id"] for r in lineage], [b["route_id"], a["route_id"]])

    def test_three_generation_chain_reaches_the_grandparent(self):
        a = _route()
        self._write(a)
        b = self._continuation(a)
        self._write(b)
        d = self._continuation(b)
        self._write(d)
        lineage = RL.verified_route_lineage(d, artifact_root=self.root)
        self.assertEqual([r["route_id"] for r in lineage], [d["route_id"], b["route_id"], a["route_id"]])

    def test_a_cycle_is_refused_by_seen_set(self):
        a = _route()
        b = self._continuation(a)
        # Force a's source to point back at b, forming a two-node cycle.
        cyclic_a = dict(a, continuation_contract_version=1, source_route_id=b["route_id"],
                        source_route_hash=b["route_hash"])
        cyclic_a["route_hash"] = route_identity.route_hash(
            {k: v for k, v in cyclic_a.items() if k != "route_hash"})
        self._write(cyclic_a)
        self._write(b)
        with self.assertRaises(RL.RouteLineageError) as ctx:
            RL.verified_route_lineage(b, artifact_root=self.root)
        self.assertEqual(ctx.exception.code, "route-lineage-unverified")

    def test_missing_parent_file_is_refused(self):
        a = _route()
        b = self._continuation(a)
        self._write(b)  # parent `a` never written
        with self.assertRaises(RL.RouteLineageError) as ctx:
            RL.verified_route_lineage(b, artifact_root=self.root)
        self.assertIn("parent-unreadable", ctx.exception.detail)

    def test_artifact_root_defaults_to_the_route_field(self):
        a = _route(artifact_root=str(self.root))
        self._write(a)
        b = self._continuation(a, artifact_root=str(self.root))
        self._write(b)
        lineage = RL.verified_route_lineage(b)  # no explicit artifact_root kwarg
        self.assertEqual([r["route_id"] for r in lineage], [b["route_id"], a["route_id"]])


class LeafBoundaryTest(unittest.TestCase):
    """SD-155 I-1/B1: this leaf carries no git calls and no capability-route import.

    `source_lineage_verdict` (SD-156, A-5) stays in `capability-route.py` per
    plan correction B1; only `verified_route_lineage`/`canonical_route_path`
    live here. This is the module-shape half of A-SD156-6's census (the git
    half is `capability_route.test.py::SourceCensusTest`).
    """
    def test_route_lineage_leaf_makes_no_subprocess_or_git_calls(self):
        import inspect
        source = inspect.getsource(RL)
        self.assertNotIn("subprocess.", source)
        self.assertNotIn("rev-list", source)
        self.assertNotIn("import capability_route", source.replace("-", "_"))


if __name__ == "__main__":
    unittest.main()
