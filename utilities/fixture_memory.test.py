"""Real answer-release tests must never write to the caller's memory paths."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools/memory/tests")]
from tidy_isolation import isolated_env


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MemoryBoundaryTest(unittest.TestCase):
    def test_real_release_producers_write_only_the_common_fixture_store(self):
        with isolated_env() as ambient, ambient.patched_environ(extra={
                "AGENT_ARTIFACT_CHECKPOINT": "off",
                "HEARTING_WORKFLOW_GROUP_REVIEW": "off"}):
            module = load("memory_boundary_work_start", ROOT / "utilities/work_start.test.py")
            cases = [
                (module.FrameInterviewStepTest, "test_answers_without_the_envelope_release_the_registered_interview"),
                (module.FrameInterviewStepTest, "test_the_persons_native_reply_is_recorded_and_the_next_start_takes_it"),
                (module.WF.TestGateSubjectNotCaller, "test_proceed_requires_the_answers_and_records_them"),
            ]
            sentinel = ambient.mem_store / "write-events.jsonl"
            sentinel.write_text('{"sentinel":true}\n')
            os.environ["MEM_WRITE_EVENTS"] = str(sentinel)
            for parent, method in cases:
                with self.subTest(producer=method):
                    observed = {}
                    class Probe(parent):
                        def tearDown(self):
                            journal = Path(os.environ["MEM_WRITE_EVENTS"])
                            observed["store"] = os.environ["MEM_STORE"]
                            observed["journal"] = str(journal)
                            observed["events"] = [json.loads(line) for line in journal.read_text().splitlines()]
                            super().tearDown()
                    result = unittest.TestResult()
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        Probe(method).run(result)
                    self.assertEqual(result.errors + result.failures, [])
                    self.assertEqual(sentinel.read_text(), '{"sentinel":true}\n')
                    self.assertNotEqual(observed["store"], str(ambient.mem_store))
                    self.assertTrue(any(e.get("action") == "decision-record" for e in observed["events"]))


if __name__ == "__main__":
    unittest.main()
