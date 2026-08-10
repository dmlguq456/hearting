import os
import sys
from pathlib import Path
import unittest


TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
from fleet import render  # noqa: E402


class DegradationAlertRemovalTest(unittest.TestCase):
    def event(self, **extra):
        value = {"event_id": "e1", "route_id": "rt-r", "route_node": "execute",
                 "kind": "leg-failure", "dispatch_depth": 2, "ts": 1,
                 "parallel_leg_index": 0, "parallel_leg_count": 2, "harness": "codex",
                 "exit_code": 78, "reason": "network"}
        value.update(extra)
        return value

    def test_formatter_is_retired(self):
        self.assertFalse(hasattr(render, "_degradation_alert_rows"))

    def test_failed_leg_evidence_does_not_create_alert_row(self):
        from fleet.collectors import dispatch
        previous = getattr(dispatch.collect, "last_degradations", None)
        try:
            dispatch.collect.last_degradations = {"rt-r": [self.event()]}
            lines = render._build_lines([], [], section="both", narrow=False,
                                        malformed=0, layout="wide")
        finally:
            dispatch.collect.last_degradations = previous
        text = "\n".join("".join(part for part, _key in line) for line in lines if line)
        self.assertNotIn("  alert ", text)
        self.assertNotIn("failed legs", text)


if __name__ == "__main__":
    unittest.main()
