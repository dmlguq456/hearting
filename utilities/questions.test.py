#!/usr/bin/env python3
"""``hearting run questions``: one shared board, answers only through the existing path."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import human_wait_state as state  # noqa: E402
import questions  # noqa: E402


def _run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = questions.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class BuildResponseTest(unittest.TestCase):
    INTERVIEW = {"kind": "question", "detail": {"questions": [
        {"id": "_understanding", "question": "Restated", "recommended": 0,
         "choices": [{"value": "I understand", "label": "I understand"}]},
        {"id": "scope", "question": "Which scope?", "recommended": 1,
         "choices": [{"value": "Small", "label": "Small"}, {"value": "Both", "label": "Both"}]},
    ]}}

    def test_one_choice_per_question_maps_to_the_native_values(self):
        response, error = questions.build_response(self.INTERVIEW, choices=[1, 2])
        self.assertIsNone(error)
        self.assertEqual(response, {"answers": {"_understanding": ["I understand"],
                                                "scope": ["Both"]}})
        self.assertIsNone(questions.build_response(self.INTERVIEW, choices=[1])[0])
        self.assertIsNone(questions.build_response(self.INTERVIEW, choices=[1, 3])[0])
        self.assertIsNone(questions.build_response(self.INTERVIEW, text="note")[0])
        self.assertEqual(questions.questions_of(self.INTERVIEW)[1]["recommended"], 1)

    def test_single_question_text_answers_file_and_approval_shapes(self):
        single = {"kind": "question", "question_id": "owner-correction",
                  "detail": {"question": "blocked", "choices": []}}
        self.assertEqual(questions.build_response(single, text="retry")[0],
                         {"answers": {"owner-correction": ["retry"]}})
        self.assertEqual(questions.build_response(self.INTERVIEW, answers={
            "_understanding": "I understand", "scope": ["Small"]})[0],
            {"answers": {"_understanding": ["I understand"], "scope": ["Small"]}})
        opencode = {"kind": "question", "detail": {"questions": [
            {"id": "enabled", "question": "Enable?", "field_type": "boolean", "choices": []}]}}
        self.assertEqual(questions.build_response(opencode, answers={"enabled": True})[0],
                         {"answers": {"enabled": [True]}})
        approval = {"kind": "approval"}
        self.assertEqual(questions.build_response(approval, decision="reject")[0],
                         {"decision": "reject"})
        self.assertIsNone(questions.build_response(approval, choices=[1])[0])
        self.assertIsNone(questions.build_response(single, decision="approve")[0])


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name,
                                             "PATH": str(Path(self.tmp.name) / "empty-bin")})
        patch.start()
        self.addCleanup(patch.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)

    def test_missing_observation_is_unknown_not_an_empty_board(self):
        with mock.patch.object(questions, "_CACHE_WAIT_SECONDS", 0.0):
            code, out, err = _run()
        self.assertEqual((code, out), (3, ""))
        self.assertIn("unknown", err)

    def test_list_counts_match_the_shared_projection_and_ids_resolve_exactly(self):
        import time
        now = time.time()
        self.assertTrue(state.publish_cache({"observed_at": now, "complete": True, "items": [
            {"id": "S1:codex:sid-a:1", "source": "S1", "kind": "decision", "since": now - 30,
             "stale": False, "owner": {"harness": "codex", "session_id": "sid-a"},
             "owner_state": "live", "attribution": "exact"},
            {"id": "S1:codex:sid-b:2", "source": "S1", "kind": "decision", "since": now - 99,
             "stale": True, "fold": "ownerless", "owner_state": "none", "attribution": "exact",
             "owner": {"harness": "codex", "session_id": "sid-b"}},
        ]}))
        envelope = {"state": "open", "kind": "question", "support": {"supported": True},
                    "detail": {"questions": [{"id": "q", "question": "Pick one", "choices": [
                        {"value": "A", "label": "A"}, {"value": "B", "label": "B"}]}]}}
        with mock.patch.object(state, "read_selected_request", return_value=envelope) as read:
            code, out, _err = _run()
        self.assertEqual(code, 0)
        self.assertIn("questions: 1 current · 1 older (1 ownerless)", out)
        self.assertIn("Pick one", out)
        self.assertIn("S1:codex:sid-b:2  native question", out)
        self.assertIn("owner ended, no successor", out)
        read.assert_called_once_with({"id": "S1:codex:sid-a:1", "harness": "codex",
                                      "session_id": "sid-a"})
        with mock.patch.object(state, "read_selected_request", return_value=envelope), \
             mock.patch.object(state, "submit_selected_response") as submit:
            for prefix in ("S1:codex:sid-", "S1:codex:sid-a"):
                code, _out, err = _run("answer", prefix, "--choice", "1")
                self.assertEqual(code, 2)
                self.assertIn("exactly this id", err)
        submit.assert_not_called()

        def own(ref, response, *, action_id):
            return {"state": "applied", "action_id": action_id}
        with mock.patch.object(state, "read_selected_request", return_value=envelope), \
             mock.patch.object(state, "submit_selected_response", side_effect=own) as submit:
            code, out, _err = _run("answer", "S1:codex:sid-a:1", "--choice", "2")
        self.assertEqual(code, 0)
        self.assertIn("applied", out)
        ref, response = submit.call_args.args
        self.assertEqual((ref["id"], ref["question_id"], response),
                         ("S1:codex:sid-a:1", "q", {"answers": {"q": ["B"]}}))
        self.assertRegex(submit.call_args.kwargs["action_id"], r"^[0-9a-f]{32}$")
        # Another caller (Fleet) claimed first: its state is not this command's success.
        for other in ({"state": "submitted", "action_id": "fleet-action"},
                      {"state": "applied"}):
            with mock.patch.object(state, "read_selected_request", return_value=envelope), \
                 mock.patch.object(state, "submit_selected_response", return_value=other):
                code, out, _err = _run("answer", "S1:codex:sid-a:1", "--choice", "2")
            self.assertEqual(code, 1)
            self.assertIn("answered-elsewhere", out)

    def test_defer_is_reported_as_deferred_and_sends_nothing(self):
        import time
        now = time.time()
        self.assertTrue(state.publish_cache({"observed_at": now, "complete": True, "items": [
            {"id": "S1:codex:sid-a:1", "source": "S1", "kind": "approval", "since": now - 30,
             "stale": False, "owner": {"harness": "codex", "session_id": "sid-a"},
             "attribution": "exact"}]}))
        envelope = {"state": "open", "kind": "approval", "support": {"supported": True}}
        deferred = {"state": "open", "result": {"decision": "defer",
                                                "status": "no-response-sent"}}
        with mock.patch.object(state, "read_selected_request", return_value=envelope), \
             mock.patch.object(state, "submit_selected_response", return_value=deferred):
            code, out, _err = _run("answer", "S1:codex:sid-a:1", "--decision", "defer")
        self.assertEqual(code, 0)
        self.assertIn("deferred", out)


if __name__ == "__main__":
    unittest.main()
