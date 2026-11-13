"""F-48 blocked precedence, process safety, render badges and public JSON."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import fleet as fleetmod  # noqa: E402
from fleet import model, render  # noqa: E402
from fleet.model import Session  # noqa: E402


WAIT = {"kind": "decision", "source": "codex-rollout", "waiting_since": 100.0}


class InvalidationRenderTest(unittest.TestCase):
    def setUp(self):
        model.reset_state_tracker()

    def evidence(self, **overrides):
        value = {
            "harness": "codex", "pid": 10, "pid_alive": True,
            "proc_start": "100", "proc_start_match": True, "orphan": False,
            "status": "busy", "mtime": 199.0, "transcript": True,
            "interaction_wait": dict(WAIT),
        }
        value.update(overrides)
        return value

    def test_blocked_precedes_activity_without_dwell(self):
        key = ("s", "codex", 10, "100")
        state, evidence = model.classify_session(self.evidence(), 200.0, key=key)
        self.assertEqual(state, "blocked")
        self.assertEqual(evidence["tier"], 2)
        self.assertEqual(evidence["source"], "interaction:codex-rollout")
        self.assertIsNone(evidence["hysteresis"])
        state, evidence = model.classify_session(self.evidence(status=None), 201.0, key=key)
        self.assertEqual(state, "blocked")
        self.assertIsNone(evidence["hysteresis"])

    def test_existence_and_identity_are_stronger(self):
        cases = [
            (dict(pid_alive=False), "dead"),
            (dict(proc_start_match=False), "dead"),
            (dict(orphan=True), "stale"),
        ]
        for overrides, expected in cases:
            state, _evidence = model.classify_session(self.evidence(**overrides), 200.0)
            self.assertEqual(state, expected)

    def session(self, kind="decision"):
        return Session(
            harness="codex", pid=10, cwd="/work/project", session_id="sid",
            slug="project", liveness="blocked", elapsed_min=3,
            interaction_state={"kind": kind, "source": "codex-rollout", "waiting_since": 100.0},
            state_evidence={"source": "interaction:codex-rollout",
                            "inputs": {"interaction_wait": dict(WAIT)}},
        )

    def test_wide_narrow_stack_and_plain_show_kind(self):
        session = self.session()
        wide = render._session_row(session, narrow=False, name_width=40)
        narrow = render._session_row_2line(session, term_width=100)[0]
        stack = render._session_row_stack(session, term_width=60)[0]
        for segments in (wide, narrow, stack):
            text = render._plain(segments)
            self.assertIn('answer needed', text)
            self.assertIn("◑", text)
            self.assertNotIn("실제 질문", text)

    def test_compact_kind_labels(self):
        expected = {"decision": 'answer needed', "approval": 'approval needed',
                    "permission": 'approval needed', "elicitation": 'input needed'}
        for kind, label in expected.items():
            self.assertEqual(render._interaction_badge(self.session(kind)).strip(), label)

    def test_permission_and_approval_share_one_fleet_label(self):
        labels = {
            render._interaction_badge(self.session(kind)).strip()
            for kind in ("permission", "approval")
        }
        self.assertEqual(labels, {'approval needed'})

    def test_working_spinners_use_dedicated_vanilla_keys(self):
        with mock.patch.object(render.time, "time", return_value=0.0):
            _main_glyph, main_key = render._glyph("working")
            _job_glyph, job_key = render._glyph("working", dim=True)
            pulse = render._pulse_segs(
                [Session(harness="codex", pid=1, liveness="working")], []
            )
        self.assertEqual(main_key, "g_spin")
        self.assertEqual(job_key, "g_spin_dim")
        self.assertEqual(render._HUE_OF[main_key], ("v", 0))
        self.assertEqual(render._HUE_OF[job_key], ("v", render._A_DIM))
        self.assertEqual(pulse[1][1], "g_spin")
        # F-60 moved blocked off the shared yellow `g_idle` onto its own red key. The point
        # this line has always made — blocked does not borrow the working axis' keys — is
        # unchanged, and it is now also distinct from `g_dead`.
        self.assertEqual(render._GLYPH_KEY["blocked"], "g_blocked")
        self.assertNotEqual(render._GLYPH_KEY["blocked"], render._GLYPH_KEY["dead"])

    def test_public_json_contains_only_interaction_metadata(self):
        session = self.session()
        session._prompt_display = {"human_state": "known", "human": {
            "text": "private human first line"}, "received": [{"text": "private peer line"}]}
        self.assertNotIn("private human first line", repr(session))
        with mock.patch.object(fleetmod, "_collect_memory", return_value=None), \
             mock.patch.object(fleetmod, "_collect_route", return_value=[]), \
             mock.patch.object(fleetmod, "_collect_governor", return_value=None):
            output = json.loads(fleetmod._snapshot_json([session], []))
        public = output["sessions"][0]
        self.assertEqual(public["interaction_state"]["kind"], "decision")
        self.assertEqual(public["state_evidence"]["inputs"]["interaction_wait"]["kind"],
                         "decision")
        self.assertNotIn("_interaction_activity", public)
        self.assertNotIn("prompt", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
