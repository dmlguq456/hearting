import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
from pathlib import Path
import tempfile
import unittest

import resource_run_evidence as evidence


class ResourceRunEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root / "artifacts"
        self.route_file = self.root / "route.json"

    def tearDown(self):
        self.temp.cleanup()

    def route(self, *, mapped=False):
        output = "parts/autopilot-lab/full-run/run.json" if mapped else "run.json"
        node = {"id": "resource", "outputs": [output], "write_scope": [output]}
        if mapped:
            node.update(part="autopilot-lab:full-run", part_io={"run.json": output})
        route = {"route_id": "rt-test", "route_hash": "sha256:" + "a" * 64,
                 "artifact_root": str(self.root), "nodes": [node]}
        self.route_file.write_text(json.dumps(route))
        return {"artifact_base": str(self.base), "declared_outputs": [output],
                "route_file": str(self.route_file), "route_id": route["route_id"],
                "route_hash": route["route_hash"], "node": "resource"}

    def test_native_run_record_stays_at_cycle_root_without_writes(self):
        receipt = evidence.paths(self.route(), prepare=False)
        self.assertEqual(receipt["expected_outputs"], [str(self.base / "run.json")])
        self.assertEqual(receipt["runtime_output"], str(self.base / "run.json"))
        self.assertFalse(self.base.exists())

    def test_borrowed_run_record_uses_its_mapped_declaration_without_root_copy(self):
        receipt = evidence.paths(self.route(mapped=True), prepare=False)
        mapped = self.base / "parts/autopilot-lab/full-run/run.json"
        self.assertEqual(receipt["expected_outputs"], [str(mapped)])
        self.assertEqual(receipt["runtime_output"], str(mapped))
        self.assertFalse((self.base / "run.json").exists())
        self.assertFalse(self.base.exists())

    def test_mapped_output_rejects_symlinked_parent_for_resolution_and_write(self):
        armed = self.route(mapped=True)
        sibling = self.base / "parts/autopilot-lab/eval-run"
        sibling.mkdir(parents=True)
        sibling_record = sibling / "run.json"
        original = b'{"owner-document":"leave unchanged"}\n'
        sibling_record.write_bytes(original)
        mapped_parent = self.base / "parts/autopilot-lab/full-run"
        mapped_parent.symlink_to(sibling, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, r"^resource-output-symlink:"):
            evidence.paths(armed)
        with self.assertRaisesRegex(ValueError, r"^resource-output-symlink:"):
            evidence.write(armed, {})

        self.assertEqual(sibling_record.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
