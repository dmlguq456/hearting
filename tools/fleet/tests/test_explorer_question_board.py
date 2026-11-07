"""The QUESTIONS board as the person reads it: one count, who/what/route/when, no silent hiding."""
import unittest
import sys
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.fleet.explorer import live
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.live import _question_records, build_view_model
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import ExplorerViewModel

NOW = 1_762_000_000.0
HOUR = 3600
PRIVATE = "PRIVATE-QUESTION-BODY-SENTINEL"


def _sessions():
    rows = [{"tag": "b6", "harness": "claude", "repo": "hearting", "pane": "w1:p3",
             "model": ["opus", "fam_opus"], "effort": "xhigh"},
            {"tag": "34", "harness": "codex", "repo": "hearting", "pane": "w2:p1",
             "model": None, "effort": None}]
    index = {"b6": {"sid": "sid-b6", "session": {"harness": "claude"}, "ids": ["sid-b6"]},
             "34": {"sid": "sid-34", "session": {"harness": "codex"}, "ids": ["sid-34", "sid-old"]}}
    return rows, index


_ROUTES = [{"id": "rt-454aff269a3d2452", "title": "v4 jobslog readers", "state": "working",
            "repo": "hearting"}]


def _items():
    return [
        {"id": "S2:rt-454aff269a3d2452:frame-review:1", "source": "S2", "kind": "human gate",
         "route_id": "rt-454aff269a3d2452", "node": "frame-review", "since": NOW - 26 * 60,
         "stale": "current", "harness": "claude", "session_id": "sid-b6", "owner_state": "live",
         "question": PRIVATE},
        {"id": "S3:att-successor", "source": "S3", "kind": "blocked owner",
         "route_id": "rt-18cee3b07abdc802", "since": NOW - 5 * HOUR, "stale": "current",
         "harness": "codex", "session_id": "sid-34", "owner_state": "successor",
         "registered_harness": "codex", "registered_session_id": "sid-old"},
        {"id": "S2:rt-old:run-authorization:1", "source": "S2", "kind": "human gate",
         "route_id": "rt-928af3a564f61fdc", "node": "run-authorization",
         "since": NOW - 27 * 24 * HOUR, "stale": "older", "fold": "older",
         "harness": "claude", "session_id": "sid-gone", "owner_state": "unknown",
         "cwd": "/synthetic/hearting"},
        {"id": "S3:att-ownerless", "source": "S3", "kind": "blocked owner",
         "route_id": "rt-9a5f6d169b619df1", "since": NOW - 2 * 24 * HOUR, "stale": "older",
         "fold": "ownerless", "harness": "codex", "session_id": "01a186b9-aaaa-bbbb",
         "owner_state": "none", "cwd": "/synthetic/hearting"},
    ]


def _board():
    rows, index = _sessions()
    waits = {"items": _items(), "complete": True, "total": 2, "older": 2, "ownerless": 1,
             "health": []}
    questions, summary = _question_records({"answer_waits": waits}, rows, index, NOW,
                                           routes=_ROUTES, resolver=lambda _cwd: "hearting")
    vm = ExplorerViewModel.from_mapping({"questions": questions, "routes": _ROUTES,
                                         "sessions": [], "system": {"answer_waits": summary}})
    return questions, summary, vm


def _text(vm, state, cols=168):
    return draw(vm, state, cols, 60, beat=1).text_rows()


class OneCount(unittest.TestCase):
    def test_overview_status_line_counts_the_same_current_waits_as_the_questions_tab(self):
        snapshot = {
            "sessions": [{"harness": "claude", "session_id": "sid-a", "session_tag": "a1",
                          "cwd": "/synthetic/repo", "liveness": "idle"}],
            "project_of": lambda _cwd: "repo",
            "answer_waits": {"items": [
                {"id": f"S2:current-{n}", "source": "S2", "kind": "human gate", "since": 900,
                 "stale": "current", "harness": "claude", "session_id": "sid-a"} for n in range(3)
            ] + [
                {"id": f"S3:older-{n}", "source": "S3", "kind": "blocked owner", "since": 10,
                 "stale": "older", "harness": "claude", "session_id": "sid-a"} for n in range(6)
            ], "total": 3, "older": 6, "complete": True, "health": []},
            "resources": [], "jobs": [],
        }
        vm = build_view_model(snapshot, now=1000)
        self.assertEqual(len(vm.questions), 9)
        overview = _text(vm, NavState(), 140)
        self.assertIn("3 questions", overview[-1])
        self.assertNotIn("9 question", "\n".join(overview))
        board = _text(vm, NavState(view="QUESTIONS", title="QUESTIONS"), 140)
        self.assertIn("WAITING  3 questions · older waits 6", "\n".join(board))
        self.assertIn("3 waiting · older waits 6", board[-1])

    def test_an_unobserved_board_is_never_counted_as_zero_on_the_first_screen(self):
        vm = build_view_model({"sessions": [], "resources": [], "jobs": [],
                               "answer_waits": {"items": [], "total": 0, "older": 0,
                                                "complete": False,
                                                "health": [{"source": "cache", "state": "failed"}]}})
        self.assertIn("? questions", _text(vm, NavState(), 140)[-1])


class EachRow(unittest.TestCase):
    def test_row_names_who_answers_what_is_asked_its_route_and_since_when(self):
        questions, _summary, vm = _board()
        self.assertTrue(all("question" not in item and "choices" not in item for item in questions))
        text = "\n".join(_text(vm, NavState(view="QUESTIONS", title="QUESTIONS")))
        self.assertNotIn(PRIVATE, text)
        self.assertIn("[b6]", text)
        self.assertIn("claude (opus·xhigh)", text)
        self.assertIn("frame plan review · v4 jobslog readers", text)
        self.assertIn("route:    v4 jobslog readers · rt-454aff269a3d2452", text)
        self.assertRegex(text, r"since:    \d\d:\d\d · waiting 26m")
        self.assertIn("QUESTION", text)
        self.assertIn("blocked owner 5h 0m", text)        # KIND fits whole in a wide row

    def test_successor_and_ownerless_waits_say_who_can_answer(self):
        questions, summary, vm = _board()
        by_id = {item["id"]: item for item in questions}
        self.assertEqual(by_id["S3:att-successor"]["registered"], "session sid-old")
        ownerless = by_id["S3:att-ownerless"]
        self.assertEqual((ownerless["marker"], ownerless["project"], ownerless["owner_sid"]),
                         ("◌", "hearting", "01a186b9"))
        self.assertEqual(summary["ownerless"], 1)
        text = "\n".join(_text(vm, NavState(view="QUESTIONS", title="QUESTIONS")))
        self.assertIn("older waits 2 (no session to take it 1)", text)
        self.assertIn("taken over · blocked work needs direction", text)
        self.assertIn("owner:    [34] taken over · originally session sid-old", text)
        self.assertIn("◌ [··]", text)
        self.assertIn("no session to take it · blocked work needs direction", text)
        self.assertIn("no session to take it · original session ended (01a186b9)", text)
        self.assertIn("owner:    session sid-gone · not in the Fleet list", text)


    def test_a_successor_in_another_seat_names_the_original_seat(self):
        rows, index = _sessions()
        item = dict(_items()[1], registered_session_id="sid-b6")
        questions, _ = _question_records({"answer_waits": {"items": [item], "complete": True,
                                                           "total": 1, "older": 0, "health": []}},
                                         rows, index, NOW)
        self.assertEqual((questions[0]["session_tag"], questions[0]["registered"]), ("34", "[b6]"))


class OlderWaits(unittest.TestCase):
    def test_older_waits_are_listed_marked_and_folded_only_by_the_person(self):
        _questions, _summary, vm = _board()
        state = NavState(view="QUESTIONS", title="QUESTIONS")
        frame = draw(vm, state, 168, 60, beat=1)
        rows = frame.text_rows()
        heading = next(y for y, row in enumerate(rows) if "OLDER WAITS · 2" in row)
        self.assertIn("not closed automatically", rows[heading])
        self.assertIn("[ hide ]", rows[heading])
        older = next(y for y, row in enumerate(rows) if "run-authorization gate check" in row)
        self.assertGreater(older, heading)
        age_x = rows[older].index("27d 0h")
        self.assertEqual(frame.cells[older][age_x][1], "g_idle")      # lvl_y: the yellow age
        folded, _ = reduce(state, {"type": "click", "action": ("question.older.toggle",)})
        text = "\n".join(_text(vm, folded))
        self.assertNotIn("run-authorization", text)
        self.assertIn("OLDER WAITS · 2", text)
        self.assertIn("[ show ]", text)
        self.assertIn("older waits 2", text)


class NoResponseRoutes(unittest.TestCase):
    def _routes(self, liveness):
        view = {"nodes": [{"id": "execute", "state": "reconciling", "unit": "dev/backend",
                           "harness": "codex"}],
                "execution_topology": "staged", "capability": "autopilot-code",
                "cwd": "/synthetic/repo", "slug": "owner"}
        if liveness:
            view["liveness"] = liveness
        slot = {"view": view, "record": {"result": None, "slug": "owner"}, "jobs": [],
                "sessions": []}
        with patch.object(live, "_collect_routes", return_value={"rt-quiet": slot}):
            routes, _nodes = live._route_records({"resources": []}, [], {},
                                                 lambda _cwd: "repo", 1000)
        return routes

    def test_route_reader_verdict_unknown_reads_no_response_and_is_not_open(self):
        self.assertEqual(self._routes(None)[0]["state"], "working")
        routes = self._routes("unknown")
        self.assertEqual(routes[0]["state"], "stale")
        self.assertEqual(routes[0]["worker"][2][0][0], "no work running")
        vm = ExplorerViewModel.from_mapping({"routes": routes, "sessions": [], "repos": [],
                                             "system": {}})
        table = _text(vm, NavState(view="ROUTES", title="ROUTES"), 140)
        self.assertIn("no response", "\n".join(table))
        self.assertIn("0 open · no response 1 · 0 stopped legs", table[-1])
        overview = _text(vm, NavState(), 140)
        self.assertIn("0 routes · no response 1", overview[-1])


class MessageQuestions(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "utilities"))
        from fixtures.message_questions import MessageQuestionFixture
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def board(self, signal):
        import human_wait_state as waits
        import time
        self.assertTrue(waits.publish_cache({"observed_at": time.time(), "complete": True, "items": [{
            "id": signal["id"], "source": "S4", "kind": "message", "since": signal["since"],
            "owner": {"harness": signal["harness"], "session_id": signal["session_id"]},
            "attribution": "exact"}]}))
        vm = build_view_model({"sessions": [], "jobs": [], "resources": [],
                               "answer_waits": waits.cached_fleet_view(request=False)})
        return vm, vm.questions[0]

    def effect(self, state, effect, vm):
        from tools.fleet import render
        from tools.fleet.explorer.runtime import ExplorerRuntime
        result = ExplorerRuntime.execute_question_action(effect)
        with patch.object(render, "_EXPLORER_STATE", state), patch.object(render, "_EXPLORER_MODEL", vm), \
             patch.object(render, "_EXPLORER_QUESTION_REFRESH_AT", 0), \
             patch.object(render, "_explorer_queue_question_effect"):
            render._explorer_accept_question_result({"effect": effect, "result": result})
            return render._EXPLORER_STATE, result

    def action(self, state, action, vm):
        state, effects = reduce(state, {"type": "click", "action": action})
        for effect in effects:
            state, _ = self.effect(state, effect, vm)
        return state, effects

    def test_message_inline_editor_reaches_each_original_native_consumer_once(self):
        import human_wait_state as waits
        import tidy_decisions
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                signal, _ = self.f.ask(harness, text="PRIVATE QUESTION " + harness,
                                       choices=["A", "B"], recommended=1)
                vm, row = self.board(signal)
                state, _ = self.action(NavState(view="QUESTIONS"),
                    ("question.select", row["id"], row["ref"]), vm)
                self.assertEqual(state.question_overlay["state"], "open", state.question_overlay)
                self.assertIn("recommended", "\n".join(_text(vm, state)))
                state, effects = self.action(state, ("question.choice", "message-response", "B"), vm)
                self.assertEqual(effects[0][0], "questions.draft")
                state, effects = self.action(state, ("question.submit",), vm)
                self.assertEqual(effects[0][0], "questions.submit")
                self.assertEqual(state.question_overlay["state"], "submitted", state.question_overlay)
                self.assertIn("Queued", state.question_overlay["status"])
                self.assertEqual(waits._read(waits._signal_path(signal))["lifecycle"], "open")
                calls, ref, pending = self.f.courier(signal)
                self.assertEqual(len(calls), 1)
                self.assertEqual(pending["state"], "queued")
                wire = pending["text"]
                repeated, same_ref, _ = self.f.courier(signal)
                self.assertEqual((repeated, same_ref), ([], ref))
                self.assertTrue(wire.count("Question answer") == 1)
                self.f.consume(signal, wire, sid="wrong-native-session")
                self.assertEqual(waits._read(waits._signal_path(signal))["lifecycle"], "open")
                self.f.consume(signal, wire)
                done = waits._read(waits._signal_path(signal))
                self.assertEqual((done["lifecycle"], done["completion"]["actor"]), ("answered", "fleet"))
                completion = done["completion"]
                self.f.consume(signal, wire)
                self.assertEqual(waits._read(waits._signal_path(signal))["completion"], completion)
                self.assertEqual(self.f.peer._read_pending(ref)["state"], "received")
                self.assertEqual(self.f.record(signal)["state"], "acked")
                records = [item for item in self.f.peer._iter_records() if item.get("transfer_ref") == ref
                           and item.get("delivery", {}).get("status") == "received"]
                self.assertEqual(len(records), 1)
                self.assertNotIn("PRIVATE QUESTION", json.dumps(records))
        self.assertEqual(len(tidy_decisions.list_pending()), 3)

    def test_editor_preserves_raw_free_text_secret_and_changed_request_draft(self):
        import human_wait_state as waits
        import tidy_decisions
        from tools.fleet.explorer.state import serialize
        signal, _ = self.f.ask("opencode", secret=True)
        vm, row = self.board(signal)
        state, _ = self.action(NavState(view="QUESTIONS"), ("question.select", row["id"], row["ref"]), vm)
        state, _ = self.action(state, ("question.edit", "message-response"), vm)
        answer = "PRIVATE-SECRET-ANSWER 한국어 🙂"
        state, effects = reduce(state, {"type": "key", "key": "text:" + answer})
        for effect in effects:
            state, _result = self.effect(state, effect, vm)
        self.assertNotIn(answer, "\n".join(_text(vm, state)))
        self.assertNotIn(answer, json.dumps(serialize(state)))
        self.assertNotIn("PRIVATE-SECRET-ANSWER", repr(vm.source_records))
        state, _ = self.action(state, ("question.submit",), vm)
        self.assertEqual(state.question_overlay["state"], "submitted")
        calls, _ref, pending = self.f.courier(signal)
        self.assertEqual(len(calls), 1)
        wire, recipient = pending["text"], pending["to"]
        self.assertIn(answer, wire)
        self.f.peer.receive_peer_message(wire, recipient)
        self.assertEqual(tidy_decisions.list_pending(), [])
        self.assertNotIn(answer, json.dumps(list(self.f.peer._iter_records())))
        self.assertNotIn(answer, waits._fleet_cache_path().read_text())

        old, envelope = self.f.ask("claude", text="old")
        vm, row = self.board(old)
        state, _ = self.action(NavState(view="QUESTIONS"), ("question.select", row["id"], row["ref"]), vm)
        state, _ = self.action(state, ("question.edit", "message-response"), vm)
        state, effects = reduce(state, {"type": "key", "key": "text:keep this draft"})
        for effect in effects:
            state, _ = self.effect(state, effect, vm)
        newer, _ = self.f.ask("claude", text="replacement")
        newer_vm, _ = self.board(newer)
        # The rendered stale row cannot send. Its private draft survives replacement.
        from tools.fleet import render
        from tools.fleet.explorer.state import _question_action_ref
        ref = _question_action_ref(state.question_overlay)
        with patch.object(render, "_EXPLORER_STATE", state), patch.object(render, "_EXPLORER_MODEL", newer_vm):
            self.assertFalse(render._explorer_question_ref_current(ref))
        result = waits.submit_selected_response(ref, {"answers": {"message-response": ["late"]}})
        self.assertFalse(result["support"]["supported"])
        self.assertIn("keep this draft", waits._selected_entry(envelope)["draft"])
        self.assertIsNone(self.f.record(old))


if __name__ == "__main__":
    unittest.main()
