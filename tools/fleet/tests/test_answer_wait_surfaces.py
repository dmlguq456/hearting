from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import stat
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))

import human_wait_state
from fleet.explorer.system import job_summary, system_lines
from fleet.refresh import fleet_answer_wait_view
from fleet import herdr_projection
from fleet.render import _interaction_badge


class AnswerWaitSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def result(self):
        return {"items": [{"id": "S4:fixture", "source": "S4", "kind": "decision",
                           "question": "진행할까요?", "since": 100,
                           "owner": {"harness": "claude", "session_id": "sid-a"},
                           "attribution": "exact"}],
                "by_session": {("claude", "sid-a"): ["S4:fixture"]},
                "unattributed": [], "total": 1, "health": [], "observed_at": 101,
                "complete": True}

    def test_codec_preserves_typed_request_ids_and_stale_clear_cannot_remove_replacement(self):
        from fleet import interaction
        for request in (501, "501"):
            self.assertTrue(interaction.set_wait("sid-native", "codex", "decision", "codex-appserver",
                                                 request_id=request))
            observed = interaction.read_wait("sid-native", "codex")
            self.assertIs(type(observed["request_id"]), type(request))
            self.assertEqual(observed["request_id"], request)
            other = str(request) if type(request) is int else int(request)
            self.assertFalse(interaction.clear_wait("sid-native", "codex", request_id=other))
            self.assertFalse(interaction.clear_wait("sid-native", "codex", request_id=True))
            self.assertIsNotNone(interaction.read_wait("sid-native", "codex"))
            self.assertTrue(interaction.clear_wait("sid-native", "codex", request_id=request))
            self.assertIsNone(interaction.read_wait("sid-native", "codex"))

    def test_opencode_hook_keeps_only_native_refs_and_distinguishes_permission_source(self):
        import subprocess
        env = {**os.environ, "FLEET_INTERACTION_STATE_DIR": str(Path(self.tmp.name) / "interactions")}
        for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                    "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL"):
            env[key] = ""
        hook = ROOT / "hooks/fleet-interaction-state.py"
        with mock.patch.dict(os.environ, env):
            from fleet import interaction
            for kind, source in (("decision", "opencode-form"), ("permission", "opencode-permission")):
                payload = {"session_id": "ses-hook", "request_id": "request-current",
                           "source": source, "question": "PRIVATE-PROMPT", "choices": ["PRIVATE-CHOICE"],
                           "answers": ["PRIVATE-ANSWER"], "permission": "PRIVATE-TARGET"}
                run = subprocess.run([sys.executable, str(hook), "set", "--harness", "opencode",
                                      "--kind", kind], input=json.dumps(payload), env=env,
                                     capture_output=True, text=True, timeout=10)
                self.assertEqual((run.returncode, run.stdout), (0, ""), run.stderr)
                observed = interaction.read_wait("ses-hook", "opencode")
                self.assertEqual((observed["kind"], observed["source"], observed["request_id"]),
                                 (kind, source, "request-current"))
                self.assertNotIn("PRIVATE-", json.dumps(observed))
                stale = subprocess.run([sys.executable, str(hook), "clear", "--harness", "opencode"],
                                       input=json.dumps({"session_id": "ses-hook", "request_id": "old"}),
                                       env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(stale.returncode, 0, stale.stderr)
                self.assertIsNotNone(interaction.read_wait("ses-hook", "opencode"))

    def test_missing_wait_counts_stay_unknown_and_private_keys_never_enter_projection(self):
        result = self.result()
        result.pop("total")
        result.pop("complete")
        result["items"][0].update(draft="PRIVATE-DRAFT", native_ref="PRIVATE-CONTROL")
        view = fleet_answer_wait_view(result)
        self.assertIsNone(view["total"])
        self.assertNotIn("PRIVATE-", repr(view))

    def test_global_count_and_unattributed_description_hide_private_question(self):
        raw = self.result()
        raw["unattributed"] = ["S3:old-owner"]
        raw["total"] = 2
        raw["items"].append({"id": "S3:old-owner", "source": "S3", "kind": "blocked owner",
                             "question": "PRIVATE-BLOCKER-SENTINEL", "choices": ["SECRET"],
                             "route_id": "route-123456789", "attempt_id": "attempt-abcdefgh",
                             "since": 91, "owner": {"harness": "claude", "session_id": None},
                             "attribution": "unattributed", "stale": "current"})
        raw["items"][0]["stale"] = "current"
        view = fleet_answer_wait_view(raw)
        snapshot = SimpleNamespace(jobs=[], sessions=[], answer_waits=view, compute_hosts=None)
        self.assertIn("답변 필요 2", job_summary(snapshot))
        self.assertNotIn("◑답 대기", job_summary(snapshot))
        lines = system_lines(snapshot)
        self.assertIn("답 대기 2", lines)
        self.assertIn("귀속 못 한 항목 1", lines)
        line = next(line for line in lines if "owner 답 대기" in line)
        self.assertIn("Claude", line)
        self.assertIn("경로 23456789", line)
        self.assertIn("시도 abcdefgh", line)
        self.assertIn("1분 미만 경과", line)
        rendered = "\n".join(lines)
        self.assertNotIn("PRIVATE-BLOCKER-SENTINEL", rendered)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("PRIVATE-BLOCKER-SENTINEL", repr(snapshot.answer_waits))
        self.assertNotIn("question", repr(view["items"][1]))

    def test_older_workflow_waits_remain_folded_and_body_free(self):
        from fleet.explorer.live import _question_records

        items = [
            {"id": "S2:older-route", "source": "S2", "kind": "human gate",
             "stale": "older", "route_id": "route-older", "gate_id": "gate-older",
             "owner": {"harness": "codex", "session_id": "sid-s2"},
             "question": "PRIVATE-S2", "choices": ["PRIVATE-CHOICE"]},
            {"id": "S3:older-attempt", "source": "S3", "kind": "blocked owner",
             "stale": "older", "attempt_id": "attempt-older",
             "owner": {"harness": "claude", "session_id": "sid-s3"},
             "question": "PRIVATE-S3"},
            {"id": "S4:older-marker", "source": "S4", "kind": "marker",
             "stale": "older", "owner": {"harness": "opencode", "session_id": "sid-s4"},
             "question": "PRIVATE-S4"},
        ]
        rows, summary = _question_records(
            {"answer_waits": {"items": items, "complete": True, "total": 0, "older": 3}},
            [], {}, 101)

        self.assertEqual([row["id"] for row in rows],
                         ["S2:older-route", "S3:older-attempt", "S4:older-marker"])
        self.assertTrue(all(row["older"] for row in rows))
        self.assertEqual((summary["total"], summary["older"]), (0, 3))
        serialized = json.dumps(rows, ensure_ascii=False)
        for private in ("PRIVATE-S2", "PRIVATE-S3", "PRIVATE-S4", "PRIVATE-CHOICE",
                        "gate-older", "attempt-older"):
            self.assertNotIn(private, serialized)
        self.assertEqual(set(rows[0]["ref"]), {"id", "harness", "session_id", "route_id"})
        self.assertEqual(set(rows[1]["ref"]), {"id", "harness", "session_id"})

    def test_fleet_projection_has_no_private_fields_and_summary_reserves_half_for_s2(self):
        result = {"items": [
            {"id": "s1", "source": "S1", "kind": "approval", "since": 100,
             "owner": {"harness": "codex", "session_id": "a", "seat_key": "PRIVATE-SEAT"},
             "question": "PRIVATE-QUESTION", "choices": ["PRIVATE-CHOICE"],
             "recommendation": "PRIVATE-REC", "stale": False},
            {"id": "s1q", "source": "S1", "kind": "elicitation", "since": 100,
             "owner": {"harness": "opencode", "session_id": "q"},
             "question": "PRIVATE-QUESTION", "stale": False},
            {"id": "s2", "source": "S2", "kind": "decision", "since": 100,
             "route_id": "route", "gate_id": "gate", "owner": {"harness": None,
             "session_id": None}, "question": "PRIVATE-QUESTION", "stale": False},
            {"id": "s3", "source": "S3", "kind": "decision", "since": 100,
             "attempt_id": "att", "owner": {"harness": "claude", "session_id": "b"},
             "question": "PRIVATE-BLOCKER", "stale": True},
            {"id": "s3-old", "source": "S3", "kind": "decision", "since": 90,
             "attempt_id": "older", "owner": {"harness": None, "session_id": None},
             "question": "PRIVATE-OLD-S3", "stale": True},
            {"id": "s4", "source": "S4", "kind": "decision", "since": 100,
             "owner": {"harness": "claude", "session_id": "c"},
             "question": "PRIVATE-QUESTION", "stale": False},
        ], "complete": True, "observed_at": 101}
        self.assertTrue(human_wait_state.publish_cache(result))
        payload = human_wait_state._fleet_cache_path().read_bytes()
        self.assertNotIn(b'"question"', payload)
        self.assertNotIn(b'"choices"', payload)
        self.assertNotIn(b'"recommendation"', payload)
        self.assertNotIn(b'"seat_key"', payload)
        for sentinel in (b"PRIVATE-QUESTION", b"PRIVATE-CHOICE", b"PRIVATE-REC",
                         b"PRIVATE-SEAT", b"PRIVATE-BLOCKER", b"PRIVATE-OLD-S3"):
            self.assertNotIn(sentinel, payload)
        view = human_wait_state.cached_fleet_view(now=101, request=False)
        self.assertEqual([item["source"] for item in view["items"]],
                         ["S1-approval", "S1-question", "S2", "S3", "S3", "S4"])
        self.assertEqual([item["kind"] for item in view["items"]],
                         ["approval", "decision", "human gate", "blocked owner",
                          "blocked owner", "marker"])
        self.assertEqual([item["stale"] for item in view["items"]],
                         ["current", "current", "current", "older", "older", "current"])
        self.assertEqual([item.get("pane_id") for item in view["items"]],
                         [None, None, None, None, None, None])
        self.assertEqual(view["total"], 4)
        self.assertEqual(view["older"], 2)
        self.assertEqual(view["unattributed"], ["s2", "s3-old"])
        ids_before = [item["id"] for item in view["items"]]
        self.assertTrue(human_wait_state.publish_cache(result))
        second = human_wait_state.cached_fleet_view(now=101, request=False)
        self.assertEqual([item["id"] for item in second["items"]], ids_before)
        snapshot = SimpleNamespace(jobs=[], sessions=[], answer_waits=fleet_answer_wait_view(view),
                                   compute_hosts=None)
        summary = job_summary(snapshot)
        self.assertIn("◑답 대기 1", summary)
        self.assertIn("답변 필요 3", summary)

    def test_fleet_reader_does_not_fall_back_to_private_projection_cache(self):
        private_path = human_wait_state._cache_path()
        private_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_path.parent.chmod(0o700)
        private_path.write_text(
            '{"schema_version":1,"observed_at":101,"complete":true,'
            '"items":[{"id":"private","question":"PRIVATE-QUESTION-SENTINEL"}]}',
            encoding="utf-8")
        private_path.chmod(0o600)

        self.assertEqual(human_wait_state._fleet_cache_path().name, "fleet.json")
        with mock.patch.object(
                human_wait_state, "_read_cache",
                side_effect=AssertionError("Fleet must not read projection.json")) as reader:
            self.assertIsNone(human_wait_state.cached_fleet_view(now=101, request=False))
        reader.assert_not_called()

    def test_expired_and_malformed_fleet_cache_are_unknown(self):
        path = human_wait_state._fleet_cache_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(
            '{"schema_version":1,"observed_at":101,"complete":true,'
            '"total":1,"items":[],"unattributed":[]}', encoding="utf-8")
        path.chmod(0o600)

        self.assertIsNone(human_wait_state.cached_fleet_view(
            now=101 + human_wait_state._DISPLAY_TTL + 1, request=False))
        path.write_text("{malformed", encoding="utf-8")
        self.assertIsNone(human_wait_state.cached_fleet_view(now=101, request=False))

    def test_incomplete_fleet_cache_keeps_global_count_unknown_on_every_surface(self):
        partial = self.result()
        partial["complete"] = False
        self.assertTrue(human_wait_state.publish_cache(partial))

        cached = human_wait_state.cached_fleet_view(now=101, request=False)
        view = fleet_answer_wait_view(cached)
        snapshot = SimpleNamespace(jobs=[], sessions=[], answer_waits=view,
                                   compute_hosts=None)

        self.assertEqual(human_wait_state.cached_count("claude", "sid-a", now=101),
                         (-1, 1))
        self.assertIn("◑답 대기 ?", job_summary(snapshot))
        self.assertNotIn("◑답 대기 0", job_summary(snapshot))
        lines = system_lines(snapshot)
        self.assertIn("답 대기 ? · 관측 미완료", lines)
        self.assertNotIn("답 대기 0", lines)

    def test_dispatch_owner_row_uses_the_shared_wait_item_chip(self):
        job = SimpleNamespace(interaction_observations=[{"kind": "decision", "item_id": "S3:a"}])
        self.assertEqual(_interaction_badge(job).strip(), "answer needed")

    def test_cached_status_count_is_global_and_exact_session_from_same_items(self):
        self.assertTrue(human_wait_state.publish_cache(self.result()))
        self.assertEqual(human_wait_state.cached_count("claude", "sid-a", now=101), (1, 1))
        with mock.patch.object(human_wait_state, "_request_producer") as request:
            self.assertEqual(human_wait_state.cached_count("claude", "sid-a", now=200), (1, 1))
            request.assert_called_once_with()
        with mock.patch.object(human_wait_state, "_request_producer"):
            self.assertIsNone(human_wait_state.cached_count("claude", "sid-a", now=300))
        partial = self.result()
        partial["complete"] = False
        self.assertTrue(human_wait_state.publish_cache(partial))
        self.assertEqual(human_wait_state.cached_count("claude", "sid-a", now=101), (-1, 1))

    def test_herdr_title_prefix_uses_the_shared_session_projection(self):
        with mock.patch.object(herdr_projection, "session_title", return_value="기존 제목"), \
             mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
             mock.patch.object(herdr_projection, "compose", return_value=("claude", "기존 제목")), \
             mock.patch("human_wait_state.cached_view", return_value={
                 "items": self.result()["items"],
                 "by_session": {("claude", "sid-a"): ["S4:fixture"]}}):
            command = herdr_projection._metadata_command("herdr", "claude", "sid-a", "pane-a")
        self.assertIn("--title", command)
        title = command[command.index("--title") + 1]
        self.assertIn("◑답변 필요", title)
        self.assertIn("기존 제목", title)

    def test_explorer_projects_safe_current_and_older_waits_and_counts_unattributed(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.live import build_view_model
        from fleet.explorer.state import NavState

        private = "PRIVATE-WAIT-BODY-SENTINEL"
        snapshot = {
            "sessions": [{"harness": "claude", "session_id": "sid-a", "session_tag": "a1",
                          "cwd": "/synthetic/repo", "liveness": "idle", "herdr_pane": "pane-a",
                          "summary": private,
                          "interaction_state": {"kind": "decision"}},
                         {"harness": "codex", "session_id": "sid-b", "session_tag": "b1",
                          "cwd": "/synthetic/repo", "liveness": "blocked",
                          "interaction_state": {"kind": "decision"}}],
            "project_of": lambda _cwd: "repo",
            "answer_waits": {
                "items": [
                    {"id": "S1:approval", "source": "S1-approval", "kind": "approval",
                     "since": 100, "stale": "current", "harness": "claude", "session_id": "sid-a",
                     "question": private, "choices": ["PRIVATE-CHOICE-SENTINEL"]},
                    {"id": "S2:unattributed", "source": "S2", "kind": "human gate",
                     "since": 101, "stale": "current", "harness": None, "session_id": None,
                     "question": private},
                    {"id": "S4:older", "source": "S4", "kind": "decision",
                     "since": 90, "stale": "older", "harness": "claude", "session_id": "sid-a"},
                ],
                "by_session": {}, "unattributed": ["S2:unattributed"],
                "total": 2, "older": 1, "complete": True, "health": [],
            },
            "resources": [], "jobs": [],
        }
        vm = build_view_model(snapshot, now=1000)
        self.assertEqual(len(vm.questions), 3)
        self.assertEqual(vm.system["answer_waits"], {"state": "complete", "total": 2, "older": 1})
        by_tag = {row["tag"]: row for row in vm.sessions}
        self.assertEqual(by_tag["a1"]["ask"], "answer needed")
        self.assertEqual(by_tag["a1"]["answer_wait_marker"], "●")
        self.assertIsNone(by_tag["b1"]["ask"])
        records = [dict(item) for item in vm.questions]
        self.assertEqual({item["tag"] for item in records},
                         {"S1:approval", "S2:unattributed", "S4:older"})
        self.assertTrue(next(item for item in records if item["tag"] == "S4:older")["older"])
        self.assertEqual(next(item for item in records if item["tag"] == "S2:unattributed")["marker"], "◑")
        self.assertTrue(all(not ({"question", "choices", "title", "now"} & set(item))
                            for item in records))
        frame = draw(vm, NavState(view="QUESTIONS"), 140, 45)
        text = "\n".join(frame.text_rows())
        self.assertIn("2 questions", text)
        self.assertIn("older waits 1", text)
        self.assertNotIn(private, text)
        self.assertNotIn("PRIVATE-CHOICE-SENTINEL", text)

    def test_incomplete_explorer_wait_cache_never_presents_zero(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.live import build_view_model
        from fleet.explorer.state import NavState

        vm = build_view_model({"sessions": [], "resources": [],
                               "answer_waits": {"items": [], "total": 0, "older": 0,
                                                "complete": False,
                                                "health": [{"source": "cache", "state": "failed"}]}})
        self.assertEqual(vm.system["answer_waits"]["state"], "failed")
        self.assertIsNone(vm.system["answer_waits"]["total"])
        text = "\n".join(draw(vm, NavState(view="QUESTIONS"), 140, 45).text_rows())
        self.assertIn("? question", text)
        self.assertNotIn("0 questions", text)
        self.assertIn("? waiting", text)


class FleetQuestionActionTests(unittest.TestCase):
    def setUp(self):
        self.ref = {"id": "cache-q1", "harness": "codex", "session_id": "sid-q1",
                    "request_id": "req-q1", "turn_id": "turn-q1", "question_id": "question-q1"}

    def envelope(self, *, kind="question", state="open", detail="Which route should run?",
                 choices=None, draft=None):
        return {"v": 1, "harness": self.ref["harness"],
                "session_id": self.ref["session_id"], "request_id": self.ref["request_id"],
                "turn_id": self.ref["turn_id"], "question_id": self.ref["question_id"],
                "kind": kind, "detail": detail, "choices": choices,
                "draft": draft, "state": state, "support": {"supported": True},
                "native_ref": {"transport": "fixture", "epoch": 1}}

    def state(self, detail=None, *, draft=None, editing=False, pending=False):
        from fleet.explorer.state import NavState

        detail = detail or self.envelope(choices=["Continue", "Stop"])
        return NavState(view="QUESTIONS", folds=(("qsel:QUESTIONS", self.ref["id"]),),
                        question_overlay={"ref": dict(self.ref), "detail": detail,
                                          "draft": draft, "state": detail.get("state", "open"),
                                          "editing": editing, "pending": pending})

    def test_selected_detail_is_visible_only_in_private_overlay(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import serialize
        from fleet.explorer.view_model import ExplorerViewModel, persistable_view_model

        secret = "PRIVATE-QUESTION-DETAIL-SENTINEL"
        detail = self.envelope(detail=secret, choices=["Continue"])
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        text = "\n".join(draw(vm, self.state(detail), 140, 45).text_rows())

        self.assertIn(secret, text)
        self.assertNotIn(secret, repr(persistable_view_model(vm)))
        self.assertNotIn(secret, repr(serialize(self.state(detail))))
        self.assertNotIn("question_overlay", serialize(self.state(detail)))
        self.assertNotIn(secret, repr(vm))

    def test_unsupported_selected_request_shows_native_fallback_without_controls(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import NavState
        from fleet.explorer.view_model import ExplorerViewModel

        row = {"id": "S3:owner-1", "tag": "S3:owner-1", "h": "codex",
               "ref": {"id": "S3:owner-1", "harness": "codex", "session_id": "sid-1"},
               "kind": "blocked owner", "source": "S3", "wait": "now", "marker": "●"}
        envelope = {"state": "unknown", "support": {"supported": False}, "actions": []}
        overlay = {"ref": dict(row["ref"]), "detail": envelope, "state": "unknown",
                   "status": "selected-item-not-current", "draft": None, "pending": False}
        state = NavState(view="QUESTIONS", folds=(("qsel:QUESTIONS", row["id"]),),
                         question_overlay=overlay)
        vm = ExplorerViewModel(questions=(row,), system={
            "answer_waits": {"state": "complete", "total": 1, "older": 0}})

        text = "\n".join(draw(vm, state, 140, 45).text_rows())

        self.assertIn("No verified direct response; check the native prompt.", text)
        self.assertIn("select to view and answer · native fallback if unsupported", text)
        self.assertNotIn("submit answer", text)
        self.assertNotIn("[ approve ]", text)

    def test_choice_and_free_text_build_the_exact_structured_response(self):
        from fleet.explorer.state import reduce

        state, effects = reduce(self.state(), {"type": "click",
                                               "action": ("question.choice", "question-q1", "Continue")})
        expected = {"answers": {"question-q1": ["Continue"]}}
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][1]["native_ref"], self.envelope()["native_ref"])
        self.assertEqual(effects[0][1]["state"], "open")
        self.assertEqual(effects[0][2], "Continue")
        submitted, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][1]["native_ref"], self.envelope()["native_ref"])
        self.assertEqual(effects[0][2], expected)
        self.assertTrue(submitted.question_overlay["pending"])

        free_text_detail = self.envelope(detail={"questions": [{
            "id": "question-q1", "question": "Choose or enter another route",
            "choices": [{"value": "Continue", "label": "Continue"}], "is_other": True}]})
        typed, effects = reduce(self.state(free_text_detail, editing=True),
                                {"type": "key", "key": "text:take route B"})
        free_text = {"answers": {"question-q1": ["take route B"]}}
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][2], "take route B")
        typed, effects = reduce(typed, {"type": "key", "key": "enter"})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][2], free_text)

    def test_option_only_native_question_disables_free_text_but_other_allows_it(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import reduce
        from fleet.explorer.view_model import ExplorerViewModel

        option_only = self.envelope(detail={"questions": [{
            "id": "question-q1", "question": "Choose a listed route",
            "choices": [{"value": "route-a", "label": "Route A"}],
            "is_other": False}]})
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        text = "\n".join(draw(vm, self.state(option_only), 140, 45).text_rows())
        self.assertIn("Route A", text)
        self.assertNotIn("press e or click to type", text)
        blocked, effects = reduce(self.state(option_only), {"type": "click",
                                                            "action": ("question.edit", "question-q1")})
        self.assertEqual(effects, ())
        self.assertFalse(blocked.question_overlay.get("editing"))

        other_allowed = self.envelope(detail={"questions": [{
            "id": "question-q1", "question": "Choose or enter another route",
            "choices": [{"value": "route-a", "label": "Route A"}],
            "is_other": True}]})
        text = "\n".join(draw(vm, self.state(other_allowed), 140, 45).text_rows())
        self.assertIn("press e or click to type", text)
        editing, effects = reduce(self.state(other_allowed), {"type": "click",
                                                               "action": ("question.edit", "question-q1")})
        self.assertEqual(effects, ())
        self.assertTrue(editing.question_overlay["editing"])
        typed, effects = reduce(editing, {"type": "key", "key": "text:custom route"})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][2], "custom route")

    def test_constrained_opencode_multiselect_toggles_and_requires_native_bounds(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import (question_values_valid, reduce)
        from fleet.explorer.view_model import ExplorerViewModel

        multiselect = self.envelope(choices=[{"value": "a", "label": "Alpha"},
                                             {"value": "b", "label": "Beta"}])
        multiselect.update(harness="opencode", question_id="choice",
                           native_ref={"transport": "opencode-http-v2",
                                       "field_type": "multiselect",
                                       "option_values": ["a", "b"], "custom": False,
                                       "min_items": 2, "max_items": 2},
                           detail={"field_type": "multiselect", "question": "Pick two"})
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        start = self.state(multiselect)
        text = "\n".join(draw(vm, start, 140, 45).text_rows())
        self.assertIn("[ ] Alpha", text)
        self.assertIn("[ ] Beta", text)
        first, effects = reduce(start, {"type": "click", "action":
                                        ("question.choice", "choice", "a")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(first.question_overlay["draft"]["answers"]["choice"], ["a"])
        self.assertFalse(question_values_valid(multiselect, "choice", ["a"]))
        text = "\n".join(draw(vm, first, 140, 45).text_rows())
        self.assertIn("[x] Alpha", text)
        self.assertNotIn("[ submit answer ]", text)
        incomplete, effects = reduce(first, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects, ())

        complete, effects = reduce(incomplete, {"type": "click", "action":
                                                ("question.choice", "choice", "b")})
        self.assertEqual(effects[0][0], "questions.draft")
        values = complete.question_overlay["draft"]["answers"]["choice"]
        self.assertEqual(values, ["a", "b"])
        self.assertTrue(question_values_valid(multiselect, "choice", values))
        adjusted, effects = reduce(complete, {"type": "click", "action":
                                              ("question.choice", "choice", "a")})
        self.assertEqual(effects[0][0], "questions.draft")
        values = adjusted.question_overlay["draft"]["answers"]["choice"]
        self.assertEqual(values, ["b"])
        self.assertFalse(question_values_valid(multiselect, "choice", values))
        _incomplete, effects = reduce(adjusted, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects, ())
        complete, effects = reduce(adjusted, {"type": "click", "action":
                                              ("question.choice", "choice", "a")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(complete.question_overlay["draft"]["answers"]["choice"], ["b", "a"])
        text = "\n".join(draw(vm, complete, 140, 45).text_rows())
        self.assertIn("[ submit answer ]", text)
        submitted, effects = reduce(complete, {"type": "click", "action":
                                               ("question.submit",)})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][2], {"answers": {"choice": ["b", "a"]}})
        self.assertTrue(submitted.question_overlay["pending"])

        single = dict(multiselect, native_ref=dict(multiselect["native_ref"],
                                                   min_items=1, max_items=1))
        radio, _effects = reduce(self.state(single), {"type": "click", "action":
                                                      ("question.choice", "choice", "a")})
        radio, effects = reduce(radio, {"type": "click", "action":
                                        ("question.choice", "choice", "b")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(radio.question_overlay["draft"]["answers"]["choice"], ["b"])

    def test_choice_displays_label_but_submits_native_value(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import reduce
        from fleet.explorer.view_model import ExplorerViewModel

        detail = self.envelope(choices=[{"label": "Visible safe choice",
                                         "value": "native_choice_id"}])
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        text = "\n".join(draw(vm, self.state(detail), 140, 45).text_rows())
        self.assertIn("Visible safe choice", text)
        state, effects = reduce(self.state(detail), {"type": "click", "action":
                                                     ("question.choice", self.ref["question_id"],
                                                      "native_choice_id")})
        self.assertEqual(effects[0][2], "native_choice_id")

    def test_approval_requires_a_choice_and_has_no_persistent_escalation(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import reduce
        from fleet.explorer.view_model import ExplorerViewModel

        approval = self.envelope(kind="commandExecution/requestApproval",
                                 detail={"operation": "run command", "target": "repo A",
                                         "permissions": ["execute once"]})
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "approval", "source": "S1-approval", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        text = "\n".join(draw(vm, self.state(approval), 140, 45).text_rows())
        self.assertIn("run command", text)
        self.assertIn("approve", text)
        self.assertIn("reject", text)
        self.assertIn("defer", text)
        self.assertNotIn("acceptForSession", text)

        _state, effects = reduce(self.state(approval), {"type": "click",
                                                         "action": ("question.decision", "approve")})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][1]["native_ref"], approval["native_ref"])
        self.assertEqual(effects[0][2], {"decision": "approve"})
        _state, effects = reduce(self.state(approval), {"type": "click",
                                                         "action": ("question.decision", "reject")})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][1]["native_ref"], approval["native_ref"])
        self.assertEqual(effects[0][2], {"decision": "reject"})
        deferred, effects = reduce(self.state(approval), {"type": "click",
                                                           "action": ("question.decision", "defer")})
        self.assertEqual(effects, ())
        self.assertEqual(deferred.question_overlay["detail"]["state"], "open")
        self.assertIn("remains unresolved", deferred.question_overlay["status"])

        limited = self.envelope(
            kind="commandExecution/requestApproval",
            detail={"operation": "run command", "availableDecisions":
                    ["acceptForSession", "decline", "cancel"]})
        limited["native_ref"] = {"transport": "codex-managed-control",
                                  "request": {"method":
                                              "item/commandExecution/requestApproval"}}
        limited["actions"] = ["reject", "defer"]
        text = "\n".join(draw(vm, self.state(limited), 140, 45).text_rows())
        self.assertNotIn("[ approve ]", text)
        self.assertIn("[ reject ]", text)
        self.assertIn("[ defer ]", text)
        self.assertNotIn("acceptForSession", text)
        self.assertNotIn("applyNetworkPolicyAmendment", text)
        _state, effects = reduce(self.state(limited), {"type": "click",
                                                        "action": ("question.decision", "approve")})
        self.assertEqual(effects, ())
        _state, effects = reduce(self.state(limited), {"type": "click",
                                                        "action": ("question.decision", "reject")})
        self.assertEqual(effects[0][2], {"decision": "reject"})

    def test_duplicate_replaced_and_expired_requests_do_not_replay(self):
        from fleet.explorer.state import reduce
        from fleet.explorer.view_model import ExplorerViewModel
        from fleet import render

        draft = {"answers": {"question-q1": ["Continue"]}}
        state, effects = reduce(self.state(draft=draft), {"type": "click",
                                                          "action": ("question.submit",)})
        self.assertEqual(len(effects), 1)
        _state, duplicate = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(duplicate, ())

        replacement = dict(self.ref, request_id="req-new", turn_id="turn-new")
        replaced, effects = reduce(self.state(), {"type": "click",
                                                   "action": ("question.select", self.ref["id"], replacement)})
        self.assertEqual(effects, (("questions.read", replacement),))
        current_vm = ExplorerViewModel(questions=({"id": self.ref["id"], "ref": replacement},))
        with mock.patch.object(render, "_EXPLORER_STATE", replaced), \
             mock.patch.object(render, "_EXPLORER_MODEL", current_vm):
            render._explorer_accept_question_result(
                {"effect": ("questions.read", self.ref), "result": self.envelope()})
            self.assertEqual(render._EXPLORER_STATE.question_overlay["ref"], replacement)
            self.assertEqual(render._EXPLORER_STATE.question_overlay["state"], "loading")

        expired = self.state(self.envelope(state="expired"), draft=draft)
        _state, effects = reduce(expired, {"type": "click",
                                          "action": ("question.submit",)})
        self.assertEqual(effects, ())
        approval = self.state(self.envelope(kind="approval", state="expired"))
        _state, effects = reduce(approval, {"type": "click",
                                           "action": ("question.decision", "approve")})
        self.assertEqual(effects, ())

    def test_invalid_local_response_keeps_private_overlay_open_and_actionable(self):
        from fleet import render
        from fleet.explorer.view_model import ExplorerViewModel

        draft_text = "red, then blue"
        current = self.envelope(draft=draft_text)
        current.update(reason="opencode-answer-count-invalid",
                       result={"status": "invalid-local-response",
                               "reason": "opencode-answer-count-invalid"},
                       actions=["reply"])
        overlay_state = self.state(current, draft={"answers": {"question-q1": [draft_text]}},
                                   pending=True)
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        with mock.patch.object(render, "_EXPLORER_STATE", overlay_state), \
             mock.patch.object(render, "_EXPLORER_MODEL", vm):
            render._explorer_accept_question_result({
                "effect": ("questions.submit", self.ref, {"answers": {"question-q1": [draft_text]}}),
                "result": current})
            adopted = render._EXPLORER_STATE.question_overlay
        self.assertEqual(adopted["state"], "open")
        self.assertFalse(adopted["pending"])
        self.assertEqual(adopted["status"], "opencode-answer-count-invalid")
        self.assertEqual(adopted["draft"], {"answers": {"question-q1": [draft_text]}})
        self.assertEqual(adopted["detail"]["actions"], ["reply"])
        self.assertEqual(adopted["detail"]["native_ref"], current["native_ref"])

    def test_shared_draft_api_and_demo_mode_do_not_send_answers(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet import render

        draft = {"answers": {"question-q1": ["retained text"]}}
        envelope = self.envelope(draft=draft)
        with mock.patch.object(human_wait_state, "write_selected_draft", create=True,
                               return_value=envelope) as write:
            result = ExplorerRuntime.execute_question_action(
                ("questions.draft", dict(self.ref, **{"native_ref": self.envelope()["native_ref"],
                                                        "state": "open"}),
                 "retained text"))
        write.assert_called_once_with(
            dict(self.ref, **{"native_ref": self.envelope()["native_ref"], "state": "open"}),
            "retained text")
        self.assertEqual(result["draft"], draft)

        with mock.patch.object(human_wait_state, "read_selected_request", None, create=True):
            unavailable = ExplorerRuntime.execute_question_action(
                ("questions.read", self.ref))
        self.assertEqual(unavailable["state"], "unknown")
        self.assertIn("native prompt", unavailable["reason"])

        approval_response = {"decision": "approve"}
        with mock.patch.object(human_wait_state, "submit_selected_response", create=True,
                               return_value=self.envelope(kind="approval", state="submitted")) as submit:
            ExplorerRuntime.execute_question_action(
                ("questions.submit", self.ref, approval_response))
        submit.assert_called_once_with(self.ref, approval_response, action_id=None)

        state = self.state()
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", None), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": "1"}), \
             mock.patch.object(human_wait_state, "submit_selected_response", create=True) as submit:
            render._explorer_queue_question_effect(
                ("questions.submit", self.ref, approval_response))
            submit.assert_not_called()
            self.assertIn("disabled", render._EXPLORER_STATE.question_overlay["status"])


    def test_legacy_command_choices_and_explicit_backend_limits_stay_distinct(self):
        from fleet.explorer.state import question_decisions, reduce

        for native_detail in ({"operation": "Run requested command"},
                              {"operation": "Run requested command", "availableDecisions": None}):
            legacy = self.envelope(kind="approval", detail=native_detail)
            legacy["native_ref"] = {"transport": "codex-managed-control",
                                     "request": {"method": "item/commandExecution/requestApproval"}}
            legacy["actions"] = ["approve", "reject", "defer"]
            self.assertEqual(question_decisions(legacy), ("approve", "reject", "defer"))
            _state, effects = reduce(self.state(legacy), {"type": "click", "action":
                                                         ("question.decision", "approve")})
            self.assertEqual(effects[0][2], {"decision": "approve"})
        limited = dict(legacy, detail={"availableDecisions": []}, actions=["defer"])
        self.assertEqual(question_decisions(limited), ("defer",))
        file_missing = self.envelope(kind="approval", detail={"operation": "Apply file changes"})
        file_missing["actions"] = ["reject", "defer"]
        self.assertEqual(question_decisions(file_missing), ("reject", "defer"))
        _state, effects = reduce(self.state(file_missing), {"type": "click", "action":
                                                           ("question.decision", "approve")})
        self.assertEqual(effects, ())

    def test_nested_permission_targets_are_visible_only_in_selected_private_detail(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import serialize
        from fleet.explorer.view_model import ExplorerViewModel

        detail = self.envelope(kind="approval", detail={
            "threadId": "internal-thread-only", "turnId": "internal-turn-only",
            "itemId": "internal-item-only", "operation": "Request turn permissions",
            "permissions": {"fileSystem": {"write": ["/workspace/output"]},
                            "network": {"enabled": True}}})
        state = self.state(detail)
        vm = ExplorerViewModel(questions=({"id": self.ref["id"], "tag": self.ref["id"],
                                          "ref": self.ref, "kind": "approval",
                                          "source": "S1-approval", "wait": "now"},),
                               system={"answer_waits": {"state": "complete", "total": 1,
                                                        "older": 0}})
        text = "\n".join(draw(vm, state, 140, 55).text_rows())
        self.assertIn("/workspace/output", text)
        details = " ".join(row.split("detail:", 1)[1].strip()
                           for row in text.splitlines() if "detail:" in row)
        self.assertIn('"enabled": true', details)
        self.assertIn("Request turn permissions", text)
        self.assertNotIn("internal-thread-only", text)
        self.assertNotIn("/workspace/output", json.dumps(serialize(state)))
        self.assertNotIn("/workspace/output", json.dumps(vm.to_mapping()))


    def test_secret_native_answer_is_masked_without_changing_explicit_response(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import reduce
        from fleet.explorer.view_model import ExplorerViewModel

        secret = "fixture-private-answer"
        detail = self.envelope(detail={"questions": [{"id": "question-q1",
            "question": "Enter the requested secret", "choices": None,
            "is_other": True, "is_secret": True}]})
        state = self.state(detail, draft={"answers": {"question-q1": [secret]}})
        vm = ExplorerViewModel(questions=({"id": self.ref["id"], "tag": self.ref["id"],
                                          "ref": self.ref, "kind": "question",
                                          "source": "S1-question", "wait": "now"},),
                               system={"answer_waits": {"state": "complete", "total": 1,
                                                        "older": 0}})
        text = "\n".join(draw(vm, state, 140, 45).text_rows())
        self.assertNotIn(secret, text)
        self.assertIn("•", text)
        _state, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects[0][2], {"answers": {"question-q1": [secret]}})


class FleetQuestionBackendIntegrationTests(unittest.TestCase):
    """Exercise the real selected-wait API through its strict control transport seam."""

    def setUp(self):
        from time import time
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.now = time()
        self._install_wait()
        self.discovery = mock.patch.object(human_wait_state, "_codex_control",
                                           side_effect=self._discover_control)
        self.transport = mock.patch.object(human_wait_state, "_codex_control_request",
                                           side_effect=self._control_request)
        self.discovery.start()
        self.transport.start()
        self.addCleanup(self.discovery.stop)
        self.addCleanup(self.transport.stop)

    def _install_wait(self, kind="question", *, stale=False, questions=None,
                      request_id="native-request-q1"):
        self.kind = kind
        self.item_id = "S1:integration-q1"
        self.session_id = "thread-integration-q1"
        self.request_id = request_id
        self.turn_id = "native-turn-q1"
        self.question_id = "native-question-q1" if kind == "question" else None
        self.epoch = 17
        self.ref = {"id": self.item_id, "harness": "codex",
                    "session_id": self.session_id, "request_id": self.request_id,
                    "turn_id": self.turn_id}
        if self.question_id:
            self.ref["question_id"] = self.question_id
        self.native_ref = {"harness": "codex", "session_id": self.session_id,
                           "request_id": self.request_id, "turn_id": self.turn_id,
                           "item_id": "native-item-q1", "connection_epoch": self.epoch}
        self.gateway = {"path": str(Path(self.tmp.name) / "control.sock"), "pid": 73,
                        "proc_start": "730", "epoch": self.epoch,
                        "status": {"status": "ready", "thread_id": self.session_id}}
        if kind == "approval":
            native_detail = {"operation": "run command", "target": "workspace",
                             "permissions": ["execute once"]}
            native_questions = None
        else:
            native_detail = {"questions": list(questions or ({
                "id": self.question_id, "question": "Which route should run?",
                "options": [{"label": "Green route", "description": "Preferred route"},
                            {"label": "Blue route", "description": "Alternative route"}],
            },))}
            native_questions = native_detail["questions"]
        self.detail_request = {"harness": "codex", "session_id": self.session_id,
                               "request_id": self.request_id, "turn_id": self.turn_id,
                               "item_id": "native-item-q1", "kind": kind,
                               "detail": native_detail, "state": "open",
                               "connection_epoch": self.epoch}
        if self.question_id:
            self.detail_request["question_id"] = self.question_id
        if native_questions and len(native_questions) == 1:
            self.detail_request["choices"] = native_questions[0].get("options", [])
        self.transport_calls = []
        self.replies = []
        item = {"id": self.item_id, "source": "S1",
                "kind": "approval" if kind == "approval" else "decision",
                "since": self.now, "stale": stale, "attribution": "exact",
                "question": "PRIVATE-QUESTION-SENTINEL",
                "owner": {"harness": "codex", "session_id": self.session_id,
                          "seat_key": "integration-seat"},
                "request_id": self.request_id, "turn_id": self.turn_id,
                "item_id": "native-item-q1"}
        if self.question_id:
            item["question_id"] = self.question_id
        self.assertTrue(human_wait_state.publish_cache({
            "items": [item], "complete": True, "observed_at": self.now,
            "total": 0 if stale else 1, "health": [],
        }))

    def _discover_control(self, session_id):
        return self.gateway if session_id == self.session_id else None

    def _control_request(self, path, payload):
        self.assertEqual(path, self.gateway["path"])
        self.transport_calls.append(dict(payload))
        operation = payload.get("op")
        if operation == "interaction-list":
            self.assertEqual(set(payload), {"schema_version", "op", "session_id"})
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["session_id"], self.session_id)
            return {"schema_version": 1, "status": "ready", "requests": [self.native_ref]}
        if operation == "interaction-detail":
            self.assertEqual(set(payload), {"schema_version", "op", "ref"})
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["ref"], self.native_ref)
            return {"schema_version": 1, "status": "ready",
                    "request": dict(self.detail_request)}
        if operation == "interaction-reply":
            self.assertEqual(set(payload), {"schema_version", "op", "ref", "response", "action_id"})
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["ref"], self.native_ref)
            self.replies.append(dict(payload))
            return {"schema_version": 1, "status": "applied",
                    "request": self.native_ref, "action_id": payload["action_id"]}
        self.fail(f"unexpected Codex control operation: {operation}")

    def _read_into_ui(self, state=None):
        from fleet.explorer.state import NavState, question_draft_from_backend, reduce
        from fleet.explorer.runtime import ExplorerRuntime

        state = state or NavState(view="QUESTIONS")
        selected, effects = reduce(state, {"type": "click", "action":
                                          ("question.select", self.ref["id"], self.ref)})
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.read")
        envelope = ExplorerRuntime.execute_question_action(effects[0])
        overlay = dict(selected.question_overlay)
        overlay.update(detail=envelope,
                       draft=question_draft_from_backend(envelope, envelope.get("draft")),
                       state=envelope.get("state"), pending=False)
        return replace(selected, question_overlay=overlay), envelope

    def _adopt_draft(self, state, result):
        from fleet.explorer.state import question_draft_from_backend

        overlay = dict(state.question_overlay)
        overlay.update(detail=result,
                       draft=question_draft_from_backend(result, result.get("draft")),
                       state=result.get("state"), pending=False)
        return replace(state, question_overlay=overlay)

    def test_native_detail_choice_draft_restart_submit_and_deduplicate(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import NavState, reduce, restore, serialize

        state, envelope = self._read_into_ui()
        self.assertEqual(envelope["state"], "open", envelope)
        self.assertEqual(envelope["detail"]["questions"][0]["id"], self.question_id)
        state, effects = reduce(state, {"type": "click", "action":
                                        ("question.choice", self.question_id, "Green route")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][1]["native_ref"], envelope["native_ref"])
        self.assertEqual(effects[0][1]["state"], "open")
        self.assertEqual(effects[0][2], "Green route")
        saved = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(saved["draft"], "Green route")
        state = self._adopt_draft(state, saved)

        state_snapshot = json.dumps(serialize(state), ensure_ascii=False)
        self.assertNotIn("PRIVATE-QUESTION-SENTINEL", state_snapshot)
        fleet_snapshot = human_wait_state._fleet_cache_path().read_bytes()
        self.assertNotIn(b"PRIVATE-QUESTION-SENTINEL", fleet_snapshot)
        self.assertNotIn(b'"question"', fleet_snapshot)

        restarted = restore(serialize(state))
        restarted, reopened = self._read_into_ui(restarted)
        self.assertEqual(reopened["draft"], "Green route")
        self.assertEqual(restarted.question_overlay["draft"],
                         {"answers": {self.question_id: ["Green route"]}})
        submitted, effects = reduce(restarted, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][1]["native_ref"], reopened["native_ref"])
        result = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(self.replies[0]["response"],
                         {"answers": {self.question_id: ["Green route"]}})
        duplicate = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(duplicate["state"], "applied")
        self.assertEqual(len(self.replies), 1)

    def test_free_text_draft_uses_exact_native_question_id(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce

        self._install_wait(questions=[{
            "id": self.question_id, "question": "Which route should run?",
            "isOther": True,
            "options": [{"label": "Green route"}, {"label": "Blue route"}],
        }])
        state, envelope = self._read_into_ui()
        state, _ = reduce(state, {"type": "click", "action": ("question.edit", self.question_id)})
        state, effects = reduce(state, {"type": "key", "key": "text:free text answer"})
        self.assertEqual(effects[0][2], "free text answer")
        self.assertEqual(effects[0][1]["native_ref"], envelope["native_ref"])
        saved = ExplorerRuntime.execute_question_action(effects[0])
        state = self._adopt_draft(state, saved)
        state, effects = reduce(state, {"type": "key", "key": "enter"})
        self.assertEqual(effects[0][0], "questions.submit")
        result = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(self.replies[0]["response"],
                         {"answers": {self.question_id: ["free text answer"]}})

    def test_approval_defer_sends_nothing_and_approve_is_one_shot(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce

        self._install_wait(kind="approval")
        state, envelope = self._read_into_ui()
        deferred, effects = reduce(state, {"type": "click", "action":
                                          ("question.decision", "defer")})
        self.assertEqual(effects, ())
        self.assertIn("remains unresolved", deferred.question_overlay["status"])
        result = ExplorerRuntime.execute_question_action(
            ("questions.submit", dict(envelope, id=self.item_id), {"decision": "defer"}))
        self.assertEqual(result["state"], "open")
        self.assertFalse(self.replies)

        approved, effects = reduce(deferred, {"type": "click", "action":
                                             ("question.decision", "approve")})
        self.assertEqual(effects[0][2], {"decision": "approve"})
        result = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(self.replies[0]["response"], {"decision": "approve"})
        ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(len(self.replies), 1)

    def test_older_wait_is_folded_and_selected_only_when_expanded(self):
        from fleet.explorer.live import _question_records
        from fleet.explorer.state import NavState, reduce, question_draft_from_backend
        from fleet import render
        from fleet.explorer.runtime import ExplorerRuntime

        self._install_wait(stale=True)
        current = {"id": "S1:current-integration", "source": "S1", "kind": "decision",
                   "since": self.now, "stale": False, "attribution": "exact",
                   "owner": {"harness": "codex", "session_id": self.session_id},
                   "request_id": "native-request-current", "turn_id": "native-turn-current"}
        older = {"id": self.item_id, "source": "S1", "kind": "decision",
                 "since": self.now, "stale": True, "attribution": "exact",
                 "owner": {"harness": "codex", "session_id": self.session_id},
                 "request_id": self.request_id, "turn_id": self.turn_id,
                 "item_id": "native-item-q1"}
        self.assertTrue(human_wait_state.publish_cache({
            "items": [current, older], "complete": True, "observed_at": self.now,
            "total": 1, "health": [],
        }))
        waits = human_wait_state.cached_fleet_view(now=self.now, request=False)
        questions, summary = _question_records({"answer_waits": waits}, [], {}, self.now)
        self.assertEqual((summary["total"], summary["older"]), (1, 1))
        older_row = next(item for item in questions if item["id"] == self.item_id)
        self.assertTrue(older_row["older"])
        with mock.patch.object(render, "_EXPLORER_MODEL", SimpleNamespace(questions=questions)), \
             mock.patch.object(render, "_EXPLORER_STATE", NavState(view="QUESTIONS")):
            self.assertEqual([item["id"] for item in render._explorer_question_refs()],
                             ["S1:current-integration"])
            state, _ = reduce(NavState(view="QUESTIONS"), {"type": "click", "action":
                                                ("question.older.toggle",)})
            with mock.patch.object(render, "_EXPLORER_STATE", state):
                self.assertEqual([item["id"] for item in render._explorer_question_refs()],
                                 ["S1:current-integration", self.item_id])
            selected, effects = reduce(state, {"type": "click", "action":
                                               ("question.select", self.item_id,
                                                older_row["ref"])})
        self.assertEqual(effects[0][0], "questions.read")
        self.assertEqual(effects[0][1]["request_id"], self.request_id)
        envelope = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(envelope["state"], "open")
        overlay = dict(selected.question_overlay)
        overlay.update(detail=envelope, draft=question_draft_from_backend(envelope,
                                                                          envelope.get("draft")),
                       state="open")
        selected = replace(selected, question_overlay=overlay)
        selected, effects = reduce(selected, {"type": "click", "action":
                                             ("question.choice", self.question_id,
                                              "Green route")})
        saved = ExplorerRuntime.execute_question_action(effects[0])
        selected = self._adopt_draft(selected, saved)
        selected, effects = reduce(selected, {"type": "click", "action": ("question.submit",)})
        result = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(len(self.replies), 1)

    def test_multiple_native_questions_show_no_false_single_answer_support(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import (question_action_supported,
                                          question_answer_supported,
                                          question_draft_from_backend, reduce, restore,
                                          serialize)
        from fleet.explorer.frame import draw
        from fleet.explorer.view_model import ExplorerViewModel

        self._install_wait(questions=[
            {"id": "native-question-one", "question": "First?",
             "options": [{"label": "A", "value": "first-a"}]},
            {"id": "native-question-two", "question": "Second?",
             "options": [{"label": "B", "value": "second-b"}]},
        ])
        state, envelope = self._read_into_ui()
        self.assertEqual(len(envelope["detail"]["questions"]), 2)
        self.assertTrue(envelope["support"]["supported"])
        self.assertIsInstance(envelope["native_ref"], dict)
        self.assertTrue(question_answer_supported(envelope))
        self.assertEqual([choice["value"] for choice in
                          envelope["detail"]["questions"][0]["choices"]], ["A"])
        self.assertEqual([choice["value"] for choice in
                          envelope["detail"]["questions"][1]["choices"]], ["B"])

        state, effects = reduce(state, {"type": "click", "action":
                                        ("question.choice", "native-question-one", "A")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][1]["native_ref"], envelope["native_ref"])
        saved = ExplorerRuntime.execute_question_action(effects[0])
        self.assertIsInstance(saved["draft"], str)
        state = self._adopt_draft(state, saved)

        partial, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects, ())
        self.assertFalse(self.replies)

        state, effects = reduce(partial, {"type": "click", "action":
                                          ("question.choice", "native-question-two", "B")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][1]["native_ref"], envelope["native_ref"])
        saved = ExplorerRuntime.execute_question_action(effects[0])
        expected = {"answers": {"native-question-one": ["A"],
                                "native-question-two": ["B"]}}
        self.assertIsInstance(saved["draft"], str)
        self.assertEqual(question_draft_from_backend(saved, saved["draft"]), expected)
        state = self._adopt_draft(state, saved)

        restarted = restore(serialize(state))
        restarted, reopened = self._read_into_ui(restarted)
        self.assertIsInstance(reopened["draft"], str)
        self.assertEqual(question_draft_from_backend(reopened, reopened["draft"]), expected)
        self.assertEqual(restarted.question_overlay["draft"], expected)
        submitted, effects = reduce(restarted, {"type": "click", "action":
                                                ("question.submit",)})
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][1]["native_ref"], reopened["native_ref"])
        self.assertEqual(effects[0][2], expected)
        self.assertTrue(submitted.question_overlay["pending"])
        result = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(self.replies[0]["response"], expected)

        vm = ExplorerViewModel(
            questions=({"id": self.item_id, "tag": self.item_id, "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        text = "\n".join(draw(vm, restarted, 140, 45).text_rows())
        self.assertNotIn("Multiple question response is unavailable", text)

        # This is a new native request. Reusing the answered request identity
        # correctly keeps it out of the current body-free cache.
        self._install_wait(kind="elicitation", request_id="native-elicitation-q2")
        self.ref["turn_id"] = None
        self.native_ref.update(kind="elicitation", method="mcpServer/elicitation/request",
                               turn_id=None)
        self.native_ref.pop("item_id", None)
        self.detail_request.update(kind="elicitation", turn_id=None,
                                   detail={"message": "Continue in the native prompt."})
        self.detail_request.pop("item_id", None)
        cached = human_wait_state._read_cache()
        unsupported_item = dict(cached["items"][0], kind="elicitation", turn_id=None)
        unsupported_item.pop("item_id", None)
        self.assertTrue(human_wait_state.publish_cache({
            "items": [unsupported_item], "complete": True,
            "observed_at": self.now, "total": 1, "health": [],
        }))
        no_turn_state, no_turn = self._read_into_ui()
        self.assertIsNone(no_turn["turn_id"])
        self.assertFalse(no_turn["support"]["supported"])
        self.assertEqual(no_turn["actions"], [])
        self.assertFalse(question_answer_supported(no_turn))
        self.assertFalse(question_action_supported(no_turn))
        _, effects = reduce(no_turn_state, {"type": "click", "action":
                                            ("question.choice", "native-question-one", "A")})
        self.assertEqual(effects, ())
        _, effects = reduce(no_turn_state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects, ())
        self.assertFalse(self.replies)

    def test_terminal_submit_states_refresh_exact_ref_and_changed_selection_cannot_send(self):
        from collections import deque
        from fleet import render
        from fleet.explorer.state import NavState

        state, envelope = self._read_into_ui()
        selected_ref = dict(envelope, id=self.item_id, _fleet_row_ref=dict(self.ref))
        effect = ("questions.submit", selected_ref,
                  {"answers": {self.question_id: ["Green route"]}})
        model = SimpleNamespace(questions=({"id": self.item_id, "ref": self.ref},))
        for result_state in ("submitted", "received", "applied"):
            queue = deque()
            pump = mock.Mock()
            with mock.patch.object(render, "_EXPLORER_STATE", state), \
                 mock.patch.object(render, "_EXPLORER_MODEL", model), \
                 mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue), \
                 mock.patch.object(render, "_EXPLORER_QUESTION_PUMP", pump):
                render._explorer_accept_question_result({
                    "effect": effect, "result": dict(envelope, state=result_state)})
                self.assertEqual(queue[0][0], "questions.read")
                self.assertEqual(queue[0][1]["native_ref"], envelope["native_ref"])
                pump.request.assert_called_once_with(force=True)

        changed = NavState(view="QUESTIONS", folds=(("qsel:QUESTIONS", "S1:other"),),
                           question_overlay={"ref": {"id": "S1:other", "harness": "codex",
                                                      "session_id": self.session_id},
                                             "state": "open"})
        queue = deque([effect])
        before = len(self.transport_calls)
        with mock.patch.object(render, "_EXPLORER_STATE", changed), \
             mock.patch.object(render, "_EXPLORER_MODEL", model), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue):
            result = render._explorer_question_worker()
            self.assertEqual(result["result"]["state"], "unknown")
            self.assertIn("changed", result["result"]["reason"])
            render._explorer_accept_question_result({"effect": effect,
                                                     "result": dict(envelope, state="applied")})
            self.assertEqual(render._EXPLORER_STATE.question_overlay,
                             changed.question_overlay)
        self.assertEqual(len(self.transport_calls), before)


    def test_item_only_marker_resolves_rpc_id_from_the_actual_native_request(self):
        cache = human_wait_state._read_cache()
        cache["items"][0].pop("request_id")
        self.assertTrue(human_wait_state.publish_cache(cache))
        selected = human_wait_state.read_selected_request({
            "id": self.item_id, "harness": "codex", "session_id": self.session_id})
        self.assertEqual(selected["state"], "open")
        self.assertEqual(selected["request_id"], self.request_id)
        self.assertEqual(selected["native_ref"]["request"]["item_id"], "native-item-q1")
        self.assertFalse(self.replies)

    def test_reused_rpc_id_on_a_new_native_item_does_not_inherit_applied(self):
        from fleet.explorer.runtime import ExplorerRuntime

        selected_state, first = self._read_into_ui()
        old_item = human_wait_state._selected_identity(first)["item_id"]
        answered = ExplorerRuntime.execute_question_action(("questions.submit",
            dict(first, id=self.item_id), {"answers": {self.question_id: ["Green route"]}}))
        self.assertEqual(answered["state"], "applied")
        self.epoch += 1
        self.gateway["epoch"] = self.epoch
        self.native_ref = dict(self.native_ref, item_id="native-item-q2",
                               connection_epoch=self.epoch)
        self.detail_request = dict(self.detail_request, item_id="native-item-q2",
                                   connection_epoch=self.epoch)
        cache = human_wait_state._read_cache()
        cache["items"][0]["item_id"] = "native-item-q2"
        self.assertTrue(human_wait_state.publish_cache(cache))
        _state, second = self._read_into_ui()
        self.assertEqual(second["state"], "open")
        self.assertNotEqual(human_wait_state._selected_identity(second)["item_id"], old_item)
        second_answer = ExplorerRuntime.execute_question_action(("questions.submit",
            dict(second, id=self.item_id), {"answers": {self.question_id: ["Blue route"]}}))
        self.assertEqual(second_answer["state"], "applied")
        ExplorerRuntime.execute_question_action(("questions.submit", dict(second, id=self.item_id),
            {"answers": {self.question_id: ["Blue route"]}}))
        self.assertEqual(len(self.replies), 2)  # two actual native items, once each
        cache = human_wait_state._read_cache()
        cache["items"][0]["item_id"] = "different-item-from-stale-observation"
        self.assertTrue(human_wait_state.publish_cache(cache))
        mismatched = human_wait_state.read_selected_request(self.ref)
        self.assertEqual(mismatched["state"], "unknown")
        self.assertIsNone(mismatched["detail"])

    def test_selected_open_request_can_refresh_late_native_target_details(self):
        from fleet import render
        from fleet.explorer.state import reduce
        from fleet.explorer.runtime import ExplorerRuntime

        self._install_wait(kind="approval")
        self.native_ref["method"] = "item/fileChange/requestApproval"
        self.detail_request["detail"] = {"operation": "Apply file changes", "grantRoot": None,
                                         "changes": []}
        state, missing = self._read_into_ui()
        self.assertNotIn("approve", missing["actions"])
        self.detail_request["detail"]["changes"] = [{"path": "/repo/report.py",
                                                      "kind": {"type": "update"}}]
        loading, effects = reduce(state, {"type": "click", "action":
                                         ("question.select", self.item_id, self.ref)})
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.read")
        latest = ExplorerRuntime.execute_question_action(effects[0])
        with mock.patch.object(render, "_EXPLORER_STATE", loading), \
             mock.patch.object(render, "_EXPLORER_MODEL", SimpleNamespace(
                 questions=({"id": self.item_id, "ref": self.ref},))):
            render._explorer_accept_question_result({"effect": effects[0], "result": latest})
            self.assertIn("approve", render._EXPLORER_STATE.question_overlay["detail"]["actions"])
        self.assertFalse(self.replies)

    def test_numeric_request_ids_reach_actual_render_without_string_coercion(self):
        from fleet import render
        from fleet.explorer.live import _question_records

        for request_id in (17, 0, "17"):
            with self.subTest(request_id=request_id):
                self._install_wait(request_id=request_id)
                public = json.loads(human_wait_state._fleet_cache_path().read_text())
                rows, _ = _question_records(SimpleNamespace(answer_waits=public), [], {}, self.now)
                self.assertEqual(type(rows[0]["ref"]["request_id"]), type(request_id))
                self.ref = rows[0]["ref"]
                state, envelope = self._read_into_ui()
                with mock.patch.object(render, "_EXPLORER_STATE", state), \
                     mock.patch.object(render, "_EXPLORER_MODEL", SimpleNamespace(questions=rows)):
                    render._explorer_accept_question_result({
                        "effect": ("questions.read", self.ref), "result": envelope})
                    adopted = render._EXPLORER_STATE.question_overlay
                    self.assertEqual(adopted["state"], "open")
                    self.assertEqual(type(adopted["detail"]["request_id"]), type(request_id))
                    self.assertIsNone(render._explorer_question_envelope(
                        self.ref, dict(envelope, request_id=True)))
                    if type(request_id) is int:
                        self.assertIsNone(render._explorer_question_envelope(
                            self.ref, dict(envelope, request_id=str(request_id))))
        for invalid in (True, 1.5, {"question": "NOT-A-NATIVE-ID-SENTINEL"}):
            public["items"][0]["request_id"] = invalid
            rows, _ = _question_records(SimpleNamespace(answer_waits=public), [], {}, self.now)
            self.assertNotIn("request_id", rows[0]["ref"])
            self.assertNotIn("NOT-A-NATIVE-ID-SENTINEL", json.dumps(rows))
        self.assertFalse(self.replies)

    def test_delayed_native_status_refreshes_until_terminal_without_resubmitting(self):
        from collections import deque
        from fleet import render

        state, envelope = self._read_into_ui()
        ref = dict(envelope, id=self.item_id, _fleet_row_ref=dict(self.ref))
        queue = deque()
        pump = mock.Mock(running=False)
        pump.poll.return_value = None
        clock = [10.0]
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_MODEL", SimpleNamespace(
                 questions=({"id": self.item_id, "ref": self.ref},))), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue), \
             mock.patch.object(render, "_EXPLORER_QUESTION_PUMP", pump), \
             mock.patch.object(render, "_EXPLORER_QUESTION_REFRESH_AT", 0.0), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": ""}), \
             mock.patch.object(render.time, "monotonic", side_effect=lambda: clock[0]):
            for status in ("submitted", "received"):
                render._explorer_accept_question_result({
                    "effect": ("questions.read", ref), "result": dict(envelope, state=status)})
                render._explorer_question_pump_tick()
                self.assertFalse(queue)
                clock[0] += 1.1
                render._explorer_question_pump_tick()
                self.assertEqual(len(queue), 1)
                refresh = queue.popleft()
                self.assertEqual(refresh[0], "questions.read")
                self.assertEqual(refresh[1]["native_ref"], envelope["native_ref"])
            for status in ("applied", "unknown", "cancelled", "expired"):
                render._explorer_accept_question_result({
                    "effect": ("questions.read", ref), "result": dict(envelope, state=status)})
                clock[0] += 2.0
                render._explorer_question_pump_tick()
                self.assertFalse(queue, status)
        self.assertFalse(self.replies)

    def test_navigation_and_shutdown_save_local_drafts_without_running_queued_submit(self):
        from collections import deque
        from fleet import render
        from fleet.explorer.state import NavState

        state, envelope = self._read_into_ui()
        ref = dict(envelope, id=self.item_id, _fleet_row_ref=dict(self.ref))
        draft = ("questions.draft", ref, "Green route")
        submit = ("questions.submit", ref, {"answers": {self.question_id: ["Green route"]}})
        queue = deque([draft])
        with mock.patch.object(render, "_EXPLORER_STATE", NavState(view="PEERS")), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": ""}):
            saved = render._explorer_question_worker()["result"]
        self.assertEqual(saved["draft"], "Green route")

        overlay = dict(state.question_overlay,
                       draft={"answers": {self.question_id: ["Blue route"]}})
        state = replace(state, question_overlay=overlay)
        queue = deque([submit, ("questions.read", ref), draft,
                       ("questions.draft", dict(ref, draft="older"), "Green route")])
        pump = mock.Mock()
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue), \
             mock.patch.object(render, "_EXPLORER_QUESTION_PUMP", pump), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": ""}):
            results = render._explorer_stop_question_pump()
            self.assertIsNone(render._EXPLORER_QUESTION_QUEUE)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["draft"], "Blue route")
        pump.stop.assert_called_once_with(join_timeout=1.0)
        self.assertEqual(human_wait_state.read_selected_request(ref)["draft"], "Blue route")
        self.assertFalse(self.replies)

    def test_private_draft_coalesces_by_binding_and_replay_shutdown_does_not_write(self):
        from collections import deque
        from fleet import render

        state, envelope = self._read_into_ui()
        ref = dict(envelope, id=self.item_id, _fleet_row_ref=dict(self.ref))
        pump = mock.Mock()
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", deque()), \
             mock.patch.object(render, "_EXPLORER_QUESTION_PUMP", pump), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": ""}):
            render._explorer_queue_question_effect(("questions.draft", ref, "G"))
            render._explorer_queue_question_effect(("questions.draft", dict(ref, draft="G"),
                                                    "Green route"))
            self.assertEqual(len(render._EXPLORER_QUESTION_QUEUE), 1)
            self.assertEqual(render._EXPLORER_QUESTION_QUEUE[0][2], "Green route")
            before = human_wait_state._cache_path().read_bytes()
            with mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", True):
                self.assertEqual(render._explorer_stop_question_pump(), [])
            self.assertEqual(human_wait_state._cache_path().read_bytes(), before)
        self.assertFalse(self.replies)


class FleetOpenCodeFormsIntegrationTests(unittest.TestCase):
    """Exercise V2 form HTTP data through the real selected-question UI path."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from time import time

        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.sid = "session_form_ui8223"
        self.form_id = "frm_ui8223"
        self.item_id = "S1:opencode-form-ui8223"
        self.now = time()
        self.posts = []
        self.post_status = 204
        self.post_terminal_state = "answered"
        self.http_paths = []
        self._set_form(self._single_multiselect())
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def _json(self, status, value=None):
                body = b"" if value is None else json.dumps(value).encode("utf-8")
                self.send_response(status)
                if value is not None:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                from urllib.parse import unquote, urlsplit

                path = urlsplit(self.path).path
                owner.http_paths.append(("GET", path))
                if path == "/global/health":
                    # Compatibility surface required by the current local adapter.
                    self._json(200, {"healthy": True, "version": "2.0.32"})
                    return
                if path == "/api/info":
                    self._json(200, {"data": {
                        "version": "2.0.32", "pid": 7001, "urls": [],
                        "paths": {"tmp": "/tmp"}, "capabilities": None,
                    }})
                    return
                prefix = "/api/session/" + owner.sid + "/form"
                if path == prefix:
                    form = {key: value for key, value in owner.form.items() if key != "state"}
                    self._json(200, {"data": [form] if owner.form["state"]["status"] == "pending"
                                     else []})
                    return
                if path == prefix + "/" + owner.form_id:
                    self._json(200, {"data": owner.form})
                    return
                self._json(404, {"_tag": "NotFoundError", "message": "fixture path"})

            def do_POST(self):
                from urllib.parse import urlsplit

                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    payload = None
                owner.http_paths.append(("POST", path))
                owner.posts.append((path, payload))
                if path != (f"/api/session/{owner.sid}/form/{owner.form_id}/reply"):
                    self._json(404, {"_tag": "NotFoundError", "message": "fixture path"})
                    return
                if owner.post_status == 204:
                    owner.form["state"] = {"status": owner.post_terminal_state,
                                            "answer": payload.get("answer")
                                            if isinstance(payload, dict) else None}
                self._json(owner.post_status, None if owner.post_status == 204 else {
                    "_tag": "FixtureTransportError", "message": "reply was not confirmed"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._stop_http)
        self.endpoint = {
            "url": f"http://127.0.0.1:{self.server.server_port}",
            "generation": "a" * 64, "pid": 7001, "proc_start": "700100",
            "server_pid": 7001, "server_proc_start": "700100",
            "frontend_pid": 7002, "frontend_proc_start": "700200",
            "server_urls": [f"http://127.0.0.1:{self.server.server_port}"],
            "socket_inodes": ["7001001"],
        }
        self.endpoint_patch = mock.patch.object(
            human_wait_state, "_opencode_endpoint",
            side_effect=lambda session: dict(self.endpoint) if session == self.sid else None)
        self.endpoint_patch.start()
        self.addCleanup(self.endpoint_patch.stop)
        self._publish_wait()

    def _stop_http(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def _set_form(self, fields, *, state="pending"):
        self.form = {"id": self.form_id, "sessionID": self.sid,
                     "title": "Private form title", "metadata": {},
                     "fields": fields, "state": {"status": state}}

    @staticmethod
    def _single_multiselect():
        return [{"key": "colors", "type": "multiselect", "title": "Choose colors",
                 "description": "Private form description", "required": True,
                 "hidden": False, "when": [],
                 "options": [{"value": "red-native", "label": "Red label"},
                             {"value": "blue-native", "label": "Blue label"}],
                 "minItems": 1, "maxItems": 3, "custom": True,
                 "default": ["blue-native"]}]

    def _publish_wait(self):
        self.assertTrue(human_wait_state.publish_cache({
            "items": [{"id": self.item_id, "source": "S1", "kind": "elicitation",
                       "since": self.now, "stale": False, "attribution": "exact",
                       "owner": {"harness": "opencode", "session_id": self.sid},
                       "request_id": self.form_id}],
            "complete": True, "observed_at": self.now, "total": 1, "health": [],
        }))

    def _row(self):
        from fleet.explorer.live import build_view_model

        waits = human_wait_state.cached_fleet_view(now=self.now, request=False)
        vm = build_view_model({
            "sessions": [{"harness": "opencode", "session_id": self.sid,
                          "session_tag": "OC/ui8223", "cwd": str(self.root),
                          "liveness": "idle"}],
            "answer_waits": waits, "project_of": lambda _cwd: "repo",
            "resources": [], "jobs": [],
        }, now=self.now)
        return next(row for row in vm.questions if row["id"] == self.item_id), vm

    def _read_and_adopt(self, *, expected_state="open"):
        from fleet import render
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import NavState, reduce

        row, vm = self._row()
        state, effects = reduce(NavState(view="QUESTIONS"), {
            "type": "click", "action": ("question.select", self.item_id, row["ref"]),
        })
        self.assertEqual(effects[0][0], "questions.read")
        envelope = ExplorerRuntime.execute_question_action(effects[0])
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_MODEL", vm), \
             mock.patch.object(render, "_EXPLORER_QUESTION_REFRESH_AT", 0):
            render._explorer_accept_question_result({"effect": effects[0],
                                                     "result": envelope})
            state = render._EXPLORER_STATE
        self.assertEqual(state.question_overlay["state"], expected_state, envelope)
        return state, envelope, row, vm

    def _adopt(self, state, effect, result, row, vm):
        from fleet import render

        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_MODEL", vm), \
             mock.patch.object(render, "_EXPLORER_QUESTION_REFRESH_AT", 0), \
             mock.patch.object(render, "_explorer_queue_question_effect"):
            render._explorer_accept_question_result({"effect": effect, "result": result})
            return render._EXPLORER_STATE

    def _run_effect(self, state, effect, row, vm):
        from fleet.explorer.runtime import ExplorerRuntime

        result = ExplorerRuntime.execute_question_action(effect)
        return self._adopt(state, effect, result, row, vm)

    def _ui_action(self, state, action, row, vm):
        from fleet.explorer.state import reduce

        state, effects = reduce(state, {"type": "click", "action": action})
        for effect in effects:
            state = self._run_effect(state, effect, row, vm)
        return state, effects

    def _ui_text(self, state, question_id, text, row, vm):
        from fleet.explorer.state import _question_text_draft, reduce

        state, _ = reduce(state, {"type": "click", "action": ("question.edit", question_id)})
        current = _question_text_draft(state.question_overlay, question_id)
        latest = None
        for _ in current:
            state, effects = reduce(state, {"type": "key", "key": "backspace"})
            if effects:
                latest = effects[-1]
        state, effects = reduce(state, {"type": "key", "key": "text:" + text})
        if effects:
            latest = effects[-1]
        if latest is not None:
            state = self._run_effect(state, latest, row, vm)
        return state

    def _rendered_submit_hit(self, state, vm):
        from fleet.explorer.grid import CellGrid
        from fleet.explorer.tables import draw_questions

        grid = CellGrid(140, 45)
        draw_questions(grid, vm, state)
        hit = next((item for item in grid.hits
                    if item.action == ("question.submit",)), None)
        return hit, grid

    def test_turnless_multifield_schema_is_exact_and_private_in_live_render(self):
        from fleet import render
        from fleet.explorer.state import serialize

        fields = [
            {"key": "choice", "type": "string", "title": "Color",
             "required": True, "hidden": False, "when": [],
             "options": [{"value": "red-native", "label": "Red label"}]},
            {"key": "notes", "type": "string", "title": "Notes",
             "description": "PRIVATE-FORM-QUESTION", "required": True,
             "hidden": False, "when": []},
        ]
        self._set_form(fields)
        state, envelope, row, _vm = self._read_and_adopt()
        self.assertIsNone(envelope["turn_id"])
        self.assertEqual(envelope["native_ref"]["field_keys"], ["choice", "notes"])
        self.assertRegex(envelope["native_ref"]["schema_binding"], r"^[0-9a-f]{64}$")
        self.assertEqual(envelope["detail"]["questions"][0]["choices"],
                         [{"value": "red-native", "label": "Red label", "description": ""}])
        self.assertEqual(state.question_overlay["detail"]["support"]["supported"], True)
        self.assertTrue(render._explorer_opencode_form_ui_supported(envelope))
        self.assertEqual(state.question_overlay["detail"]["detail"]["questions"],
                         envelope["detail"]["questions"])
        self.assertIsNone(render._explorer_question_envelope(row["ref"],
                                                              {**envelope, "turn_id": ""}))
        for changed in (
            {"session_id": "another-session"}, {"request_id": "frm_other"},
            {"selected_id": "S1:another"},
            {"native_ref": {**envelope["native_ref"], "field_keys": ["choice", "other"]}},
            {"native_ref": {**envelope["native_ref"], "socket_inodes": []}},
        ):
            self.assertIsNone(render._explorer_question_envelope(row["ref"],
                                                                  {**envelope, **changed}))
        public = json.dumps(human_wait_state.cached_fleet_view(now=self.now, request=False))
        serialized = json.dumps(serialize(state), ensure_ascii=False)
        self.assertNotIn("PRIVATE-FORM-QUESTION", public)
        self.assertNotIn("PRIVATE-FORM-QUESTION", serialized)
        self.assertNotIn("PRIVATE-FORM-QUESTION", human_wait_state._fleet_cache_path().read_text())
        self.assertFalse(self.posts)

    def test_multiselect_choice_and_free_text_submit_once_with_native_values(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import _QUESTION_DRAFT_PREFIX, reduce, serialize

        state, envelope, row, vm = self._read_and_adopt()
        self.assertEqual(envelope["detail"]["questions"][0]["id"], "colors")
        self.assertTrue(envelope["support"]["supported"])
        state, effects = reduce(state, {"type": "click", "action":
                                        ("question.choice", "colors", "red-native")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertTrue(effects[0][2].startswith(_QUESTION_DRAFT_PREFIX))
        self.assertEqual(json.loads(effects[0][2][len(_QUESTION_DRAFT_PREFIX):]),
                         {"answers": {"colors": ["red-native"]}})
        drafted = ExplorerRuntime.execute_question_action(effects[0])
        state = self._adopt(state, effects[0], drafted, row, vm)
        state, _ = reduce(state, {"type": "click", "action": ("question.edit", "colors")})
        state, effects = reduce(state, {"type": "key", "key": "text:custom private value"})
        self.assertIn("custom private value", effects[0][2])
        drafted = ExplorerRuntime.execute_question_action(effects[0])
        state = self._adopt(state, effects[0], drafted, row, vm)
        private_state = json.dumps(serialize(state), ensure_ascii=False)
        public_cache = human_wait_state._fleet_cache_path().read_text(encoding="utf-8")
        self.assertNotIn("custom private value", private_state)
        self.assertNotIn("custom private value", public_cache)
        self.assertNotIn("custom private value", json.dumps(
            human_wait_state.cached_fleet_view(now=self.now, request=False)))
        state, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        receipt = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(receipt["state"], "applied")
        self.assertEqual(self.posts, [(
            f"/api/session/{self.sid}/form/{self.form_id}/reply",
            {"answer": {"colors": ["red-native", "custom private value"]}},
        )])
        duplicate = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(duplicate["state"], "applied")
        self.assertEqual(len(self.posts), 1)

    def test_number_and_integer_answers_are_typed_and_invalid_bounds_do_not_post(self):
        self._set_form([
            {"key": "amount", "type": "number", "minimum": 0, "maximum": 10,
             "required": True, "hidden": False, "when": []},
            {"key": "count", "type": "integer", "minimum": 0, "maximum": 8,
             "required": True, "hidden": False, "when": []},
        ])
        state, _envelope, row, vm = self._read_and_adopt()
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertFalse(effects)
        self.assertFalse(self.posts)
        state = self._ui_text(state, "amount", "11", row, vm)
        state = self._ui_text(state, "count", "3", row, vm)
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertFalse(effects)
        self.assertFalse(self.posts)
        self.assertEqual(state.question_overlay["draft"]["answers"]["amount"], ["11"])
        state = self._ui_text(state, "amount", "2.5", row, vm)
        state = self._ui_text(state, "count", "0", row, vm)
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(self.posts, [(
            f"/api/session/{self.sid}/form/{self.form_id}/reply",
            {"answer": {"amount": 2.5, "count": 0}},
        )])

    def test_invalid_required_answer_keeps_private_draft_without_claim_or_post(self):
        from fleet.explorer.runtime import ExplorerRuntime

        self._set_form([{"key": "name", "type": "string", "required": True,
                         "hidden": False, "when": []}])
        _state, envelope, row, _vm = self._read_and_adopt()
        action_ref = {**envelope, "id": self.item_id, "_fleet_row_ref": row["ref"]}
        drafted = ExplorerRuntime.execute_question_action((
            "questions.draft", action_ref, "private invalid draft"))
        self.assertEqual(drafted["state"], "open")
        invalid = ExplorerRuntime.execute_question_action((
            "questions.submit", action_ref, {"answers": {"name": [""]}}))
        self.assertEqual(invalid["state"], "open")
        self.assertEqual(invalid["draft"], "private invalid draft")
        self.assertNotIn("action_id", invalid)
        self.assertFalse(self.posts)

    def test_cancelled_receipt_is_adopted_and_never_resubmitted(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce

        state, _envelope, row, vm = self._read_and_adopt()
        state, effects = reduce(state, {"type": "click", "action":
                                        ("question.choice", "colors", "red-native")})
        draft = ExplorerRuntime.execute_question_action(effects[0])
        state = self._adopt(state, effects[0], draft, row, vm)
        self.post_terminal_state = "cancelled"
        state, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        receipt = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(receipt["state"], "cancelled")
        state = self._adopt(state, effects[0], receipt, row, vm)
        self.assertEqual(state.question_overlay["state"], "cancelled")
        duplicate = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(duplicate["state"], "cancelled")
        self.assertEqual(len(self.posts), 1)

    def test_schema_change_and_lost_reply_do_not_retarget_or_replay(self):
        from fleet.explorer.runtime import ExplorerRuntime

        _state, envelope, row, _vm = self._read_and_adopt()
        ref = {**envelope, "id": self.item_id, "_fleet_row_ref": row["ref"]}
        self._set_form([{**self._single_multiselect()[0], "maxItems": 2}])
        changed = ExplorerRuntime.execute_question_action((
            "questions.submit", ref, {"answers": {"colors": ["red-native"]}},
        ))
        self.assertIn(changed["state"], {"open", "unknown"})
        self.assertFalse(self.posts)

        self._set_form(self._single_multiselect())
        self.post_status = 500
        _state, envelope, row, _vm = self._read_and_adopt()
        ref = {**envelope, "id": self.item_id, "_fleet_row_ref": row["ref"]}
        effect = ("questions.submit", ref, {"answers": {"colors": ["red-native"]}})
        ambiguous = ExplorerRuntime.execute_question_action(effect)
        self.assertEqual(ambiguous["state"], "unknown")
        duplicate = ExplorerRuntime.execute_question_action(effect)
        self.assertEqual(duplicate["state"], "unknown")
        self.assertEqual(len(self.posts), 1)

    def test_boolean_field_and_optional_omission_never_get_defaulted(self):
        self._set_form([{"key": "confirm", "type": "boolean", "required": True,
                         "hidden": False, "when": [], "default": True}])
        state, envelope, row, vm = self._read_and_adopt()
        self.assertFalse(self.posts)
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertFalse(effects)
        state, effects = self._ui_action(state, ("question.choice", "confirm", "false"), row, vm)
        self.assertEqual(effects[0][0], "questions.draft")
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(self.posts, [(
            f"/api/session/{self.sid}/form/{self.form_id}/reply",
            {"answer": {"confirm": False}},
        )])
        self.assertNotIn("default", envelope["detail"]["questions"][0])

        self.form_id = "frm_optional_ui8223"
        self.item_id = "S1:opencode-form-ui8223-optional"
        self._set_form([
            {"key": "required", "type": "string", "required": True,
             "hidden": False, "when": []},
            {"key": "optional", "type": "string", "required": False,
             "hidden": False, "when": [], "default": "never-copy"},
        ])
        self._publish_wait()
        state, _envelope, row, vm = self._read_and_adopt()
        state = self._ui_text(state, "required", "explicit", row, vm)
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(self.posts[-1], (
            f"/api/session/{self.sid}/form/{self.form_id}/reply",
            {"answer": {"required": "explicit"}},
        ))
        self.assertNotIn("never-copy", human_wait_state._fleet_cache_path().read_text())

    def test_optional_only_input_and_hidden_defaults_remain_private(self):
        from fleet.explorer.state import question_items

        self._set_form([
            {"key": "notes", "type": "string", "required": False,
             "hidden": False, "when": [], "default": "never-copy"},
            {"key": "private", "type": "string", "required": False,
             "hidden": True, "when": [], "default": "also-never-copy"},
        ])
        state, _envelope, row, vm = self._read_and_adopt()
        self.assertEqual([item["id"] for item in question_items(
            state.question_overlay["detail"])], ["notes"])
        self.assertFalse(self.posts)
        state = self._ui_text(state, "notes", "explicit optional answer", row, vm)
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(self.posts, [(
            f"/api/session/{self.sid}/form/{self.form_id}/reply",
            {"answer": {"notes": "explicit optional answer"}},
        )])
        self.assertNotIn("never-copy", human_wait_state._fleet_cache_path().read_text())
        self.assertNotIn("also-never-copy", human_wait_state._fleet_cache_path().read_text())

    def test_optional_only_empty_submit_is_explicit_and_once(self):
        from fleet.explorer.runtime import ExplorerRuntime

        self._set_form([
            {"key": "notes", "type": "string", "required": False, "default": "never-copy"},
            {"key": "private", "type": "string", "required": False, "hidden": True},
        ])
        state, _envelope, row, vm = self._read_and_adopt()
        self.assertFalse(self.posts)
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertEqual(len(effects), 1)
        self.assertEqual(self.posts, [(
            f"/api/session/{self.sid}/form/{self.form_id}/reply", {"answer": {}})])
        self.assertEqual(state.question_overlay["state"], "applied")
        receipt = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(receipt["state"], "applied")
        self.assertEqual(len(self.posts), 1)

    def test_all_hidden_optional_form_needs_rendered_explicit_submit_and_sends_empty_once(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import question_answer_supported, question_items, reduce

        self._set_form([
            {"key": "private-note", "type": "string", "title": "HIDDEN-PRIVATE-TITLE",
             "required": False, "hidden": True, "default": "HIDDEN-PRIVATE-DEFAULT"},
            {"key": "private-choice", "type": "multiselect", "required": False,
             "hidden": True, "options": [{"value": "secret", "label": "HIDDEN-PRIVATE-CHOICE"}],
             "default": ["secret"]},
        ])
        state, envelope, row, vm = self._read_and_adopt()
        self.assertEqual(question_items(state.question_overlay["detail"]), ())
        self.assertEqual(envelope["native_ref"]["field_keys"],
                         ["private-note", "private-choice"])
        detail = state.question_overlay["detail"]
        self.assertTrue(question_answer_supported(detail))
        unknown = dict(detail, state="unknown")
        self.assertFalse(question_answer_supported(unknown))
        malformed = dict(detail)
        malformed_native = dict(detail["native_ref"], field_keys=["other"])
        malformed["native_ref"] = malformed_native
        self.assertFalse(question_answer_supported(malformed))
        unsupported_schema = dict(detail)
        unsupported_body = dict(detail["detail"])
        unsupported_body["questions"] = [dict(item, field_type="object")
                                          for item in detail["detail"]["questions"]]
        unsupported_schema["detail"] = unsupported_body
        self.assertFalse(question_answer_supported(unsupported_schema))
        self.assertFalse(self.posts)

        refresh = ExplorerRuntime.execute_question_action(
            ("questions.read", state.question_overlay["ref"]))
        self.assertEqual(refresh["state"], "open")
        human_wait_state.cached_fleet_view(now=self.now + 300, request=False)
        row, vm = self._row()
        self.assertFalse(self.posts)
        self.assertFalse(any(method == "POST" for method, _path in self.http_paths))

        public = json.dumps(human_wait_state.cached_fleet_view(now=self.now, request=False))
        cache = human_wait_state._fleet_cache_path().read_text(encoding="utf-8")
        self.assertNotIn("HIDDEN-PRIVATE-TITLE", public + cache)
        self.assertNotIn("HIDDEN-PRIVATE-DEFAULT", public + cache)
        self.assertNotIn("HIDDEN-PRIVATE-CHOICE", public + cache)

        hit, grid = self._rendered_submit_hit(state, vm)
        self.assertIsNotNone(hit, "the rendered QUESTIONS grid must expose explicit Submit")
        rendered = "\n".join(grid.rows_text())
        self.assertIn("submit answer", rendered)
        self.assertNotIn("HIDDEN-PRIVATE-TITLE", rendered)
        self.assertNotIn("HIDDEN-PRIVATE-CHOICE", rendered)
        submitted, effects = reduce(state, {"type": "click", "action": hit.action})
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][2], {"answers": {}})
        receipt = ExplorerRuntime.execute_question_action(effects[0])
        state = self._adopt(submitted, effects[0], receipt, row, vm)
        self.assertEqual(receipt["state"], "applied")
        self.assertEqual(self.posts, [(
            f"/api/session/{self.sid}/form/{self.form_id}/reply", {"answer": {}})])

        duplicate = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(duplicate["state"], "applied")
        self.assertEqual(len(self.posts), 1)

    def test_hidden_required_stale_and_unsupported_forms_never_post(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import question_answer_supported, question_items, reduce

        self._set_form([
            {"key": "private-note", "type": "string", "required": False,
             "hidden": True},
        ])
        state, _envelope, row, vm = self._read_and_adopt()
        hit, _grid = self._rendered_submit_hit(state, vm)
        self.assertIsNotNone(hit)
        submitted, effects = reduce(state, {"type": "click", "action": hit.action})
        self.assertEqual(len(effects), 1)
        self.form["fields"].append({"key": "new-field", "type": "string",
                                    "required": False, "hidden": True})
        stale = ExplorerRuntime.execute_question_action(effects[0])
        self.assertNotEqual(stale["state"], "applied")
        self.assertFalse(self.posts)

        self.form_id = "frm_hidden_required_ui8223"
        self.item_id = "S1:opencode-form-ui8223-hidden-required"
        self._set_form([
            {"key": "private-required", "type": "string", "required": True,
             "hidden": True},
        ])
        self._publish_wait()
        state, _envelope, row, vm = self._read_and_adopt(expected_state="unknown")
        self.assertEqual(question_items(state.question_overlay["detail"]), ())
        self.assertFalse(question_answer_supported(state.question_overlay["detail"]))
        would_be_supported = dict(state.question_overlay["detail"])
        would_be_supported["state"] = "open"
        would_be_supported["support"] = {"supported": True}
        self.assertFalse(question_answer_supported(would_be_supported))
        hit, _grid = self._rendered_submit_hit(state, vm)
        self.assertIsNone(hit)
        _state, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertFalse(effects)
        self.assertFalse(self.posts)

        self.form_id = "frm_mixed_hidden_required_ui8223"
        self.item_id = "S1:opencode-form-ui8223-mixed-hidden-required"
        self._set_form([
            {"key": "visible-note", "type": "string", "required": False,
             "hidden": False},
            {"key": "private-required", "type": "string", "required": True,
             "hidden": True},
        ])
        self._publish_wait()
        state, _envelope, row, vm = self._read_and_adopt(expected_state="unknown")
        self.assertTrue(question_items(state.question_overlay["detail"]))
        self.assertFalse(question_answer_supported(state.question_overlay["detail"]))
        would_be_supported = dict(state.question_overlay["detail"])
        would_be_supported["state"] = "open"
        would_be_supported["support"] = {"supported": True}
        forced_overlay = dict(state.question_overlay, state="open", detail=would_be_supported)
        forced_state = replace(state, question_overlay=forced_overlay)
        self.assertFalse(question_answer_supported(would_be_supported))
        hit, _grid = self._rendered_submit_hit(forced_state, vm)
        self.assertIsNone(hit)
        _state, effects = reduce(forced_state, {
            "type": "click", "action": ("question.submit",)})
        self.assertFalse(effects)
        self.assertFalse(self.posts)

        unsupported_detail = dict(state.question_overlay["detail"])
        unsupported_detail["support"] = {"supported": False}
        unsupported_overlay = dict(state.question_overlay, detail=unsupported_detail)
        unsupported_state = replace(state, question_overlay=unsupported_overlay)
        hit, _grid = self._rendered_submit_hit(unsupported_state, vm)
        self.assertIsNone(hit)
        _state, effects = reduce(unsupported_state, {
            "type": "click", "action": ("question.submit",)})
        self.assertFalse(effects)
        self.assertFalse(self.posts)

    def test_multifield_custom_edit_preserves_native_choices_and_field_limit(self):
        from fleet.explorer.state import question_items, question_answer_supported

        self.assertEqual(question_items(None), ())
        self.assertFalse(question_answer_supported(None))
        self._set_form([
            {**self._single_multiselect()[0], "maxItems": 2},
            {"key": "notes", "type": "string", "required": True},
        ])
        state, _envelope, row, vm = self._read_and_adopt()
        state, _ = self._ui_action(state, ("question.choice", "colors", "red-native"), row, vm)
        state = self._ui_text(state, "colors", "custom original", row, vm)
        state, effects = self._ui_action(state, ("question.choice", "colors", "blue-native"), row, vm)
        self.assertFalse(effects)
        state = self._ui_text(state, "colors", "custom revised", row, vm)
        self.assertEqual(state.question_overlay["draft"]["answers"]["colors"],
                         ["red-native", "custom revised"])
        state = self._ui_text(state, "notes", "explicit note", row, vm)
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(self.posts, [(
            f"/api/session/{self.sid}/form/{self.form_id}/reply",
            {"answer": {"colors": ["red-native", "custom revised"], "notes": "explicit note"}},
        )])

    def test_extreme_integer_exponent_stays_editable_without_post(self):
        from fleet.explorer.runtime import ExplorerRuntime

        self._set_form([{"key": "count", "type": "integer", "required": True}])
        state, envelope, row, vm = self._read_and_adopt()
        state = self._ui_text(state, "count", "1e999999999", row, vm)
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertFalse(effects)
        self.assertEqual(state.question_overlay["draft"]["answers"]["count"], ["1e999999999"])
        ref = {**envelope, "id": self.item_id, "_fleet_row_ref": row["ref"]}
        result = ExplorerRuntime.execute_question_action((
            "questions.submit", ref, {"answers": {"count": ["1e999999999"]}}))
        self.assertEqual(result["state"], "open")
        self.assertEqual(result["reason"], "opencode-form-integer-answer-unsafe")
        self.assertNotIn("action_id", result)
        self.assertFalse(self.posts)

    def test_condition_visibility_tracks_choices_and_inactive_values_are_omitted(self):
        from fleet.explorer.state import question_items

        self._set_form([
            {"key": "mode", "type": "string", "required": True,
             "hidden": False, "when": [],
             "options": [{"value": "basic-native", "label": "Basic"},
                         {"value": "advanced-native", "label": "Advanced"}]},
            {"key": "details", "type": "string", "required": True,
             "hidden": False,
             "when": [{"key": "mode", "op": "eq", "value": "advanced-native"},
                      {"key": "mode", "op": "neq", "value": "basic-native"}]},
            {"key": "private", "type": "string", "required": False,
             "hidden": True, "when": []},
        ])
        state, _envelope, row, vm = self._read_and_adopt()
        visible = lambda current: [item["id"] for item in question_items(
            current.question_overlay["detail"])]
        self.assertEqual(visible(state), ["mode"])
        state, effects = self._ui_action(
            state, ("question.choice", "mode", "advanced-native"), row, vm)
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(visible(state), ["mode", "details"])
        state = self._ui_text(state, "details", "keep this private draft", row, vm)
        state, _ = self._ui_action(state, ("question.choice", "mode", "basic-native"), row, vm)
        self.assertEqual(visible(state), ["mode"])
        self.assertEqual(state.question_overlay["draft"]["answers"]["details"],
                         ["keep this private draft"])
        state, _ = self._ui_action(
            state, ("question.choice", "mode", "advanced-native"), row, vm)
        self.assertEqual(visible(state), ["mode", "details"])
        self.assertEqual(state.question_overlay["draft"]["answers"]["details"],
                         ["keep this private draft"])
        state, _ = self._ui_action(state, ("question.choice", "mode", "basic-native"), row, vm)
        state, effects = self._ui_action(state, ("question.submit",), row, vm)
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(self.posts, [(
            f"/api/session/{self.sid}/form/{self.form_id}/reply",
            {"answer": {"mode": "basic-native"}},
        )])


class FleetWorkflowQuestionIntegrationTests(unittest.TestCase):
    """Exercise private workflow questions through real registry-backed UI actions."""

    def setUp(self):
        from time import time

        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.tmp.name)
        self.agent_home = self.root / "agent-home"
        (self.agent_home / "core").mkdir(parents=True)
        (self.agent_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "xdg"),
            "HOME": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AGENT_HOME": str(self.agent_home),
            "AGENT_WORKFLOW_ROOT": str(self.root / "workflow"),
            "AGENT_ARTIFACT_CHECKPOINT": "off",
            "AGENT_ARTIFACT_CAMPAIGN_ID": "",
            "AGENT_ARTIFACT_CYCLE_ID": "",
            "AGENT_ARTIFACT_PRODUCER_ID": "",
            "AGENT_ARTIFACT_CYCLE_DIR": "",
            "AGENT_ARTIFACT_OUTPUT_DIR": "",
            "AGENT_DISPATCH_CALLER_HARNESS": "codex",
            "AGENT_DISPATCH_REGISTERED_WORKER": "",
            "AGENT_DISPATCH_CHILD": "",
            "AGENT_DISPATCH_DEPTH": "0",
            "AGENT_DISPATCH_ATTEMPT_ID": "",
            "AGENT_OWNER_ROUTE_FILE": "",
            "AGENT_OWNER_ROUTE_ID": "",
            "AGENT_OWNER_ROUTE_HASH": "",
            "CODEX_THREAD_ID": "parent-session",
            "CODEX_SESSION_ID": "",
            "CLAUDE_CODE_SESSION_ID": "",
            "CLAUDE_SESSION_ID": "",
            "OPENCODE_SESSION_ID": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.now = time()

        demand = mock.patch.object(human_wait_state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)
        project_root = str(Path(__file__).resolve().parents[3])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import dispatch
        self.dispatch = dispatch
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        registries = mock.patch.object(dispatch, "human_gate_registry_paths",
                                       return_value=(self.jobs,))
        registries.start()
        self.addCleanup(registries.stop)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.route_file = self.root / "owner.route.json"
        self.route = {"artifact_root": str(self.artifacts), "nodes": []}
        self._seal_route()

    def _seal_route(self):
        from route_identity import route_hash, route_id_from_hash

        self.route.pop("route_hash", None)
        self.route.pop("route_id", None)
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")

    def _metadata(self, attempt="att-workflow-owner", parent_sid="parent-session"):
        from dispatch_contract import supervisor_lease_path

        lease = supervisor_lease_path(self.jobs, attempt)
        return ",".join((
            "attempt_schema_version=2", "dispatch_depth=1", "worker_type=owner",
            "transport=headless", "execution_surface=registered-headless",
            "registered_worker=1", "completion_delivery=app-server-supervised",
            "harness=codex", "attempt_id=" + attempt,
            "supervisor_lease=flock-v1", "supervisor_lease_nonce=" + "d" * 64,
            "supervisor_lease_file=" + str(lease), "parent_sid=" + parent_sid,
            "parent_harness=codex", "route_id=" + self.route["route_id"],
            "route_hash=" + self.route["route_hash"], "route_file=" + str(self.route_file),
            "owner_route_id=" + self.route["route_id"],
            "owner_route_hash=" + self.route["route_hash"],
            "owner_route_file=" + str(self.route_file),
        ))

    def _write_owner(self, status, *, attempt="att-workflow-owner", suffix="",
                     parent_sid="parent-session"):
        from time import time

        row = (f"{time():.6f}\t{status}\t/repo\t/wt\towner\t"
               + self._metadata(attempt, parent_sid) + suffix + "\n")
        self.jobs.write_text(row, encoding="utf-8")

    def _observe(self):
        import human_gate_observation

        result = human_gate_observation.read_answer_waits(jobs_paths=(self.jobs,))
        self.assertTrue(result["complete"], result["health"])
        self.assertTrue(human_wait_state.publish_cache(result))
        self.latest_result = result
        from time import time
        self.now = time()
        return result

    def _blocked_owner_wait(self):
        import dispatch_owner_input

        attempt = "att-workflow-owner"
        self._write_owner("open", attempt=attempt)
        dispatch_owner_input.initialize_owner_input(self.jobs, attempt, "codex-next-turn")
        self._write_owner("done", attempt=attempt,
                          suffix=",note=dead-worker-blocked,failure_class=blocked")
        result = self._observe()
        return next(row for row in result["items"] if row["source"] == "S3")

    def _human_gate_wait(self):
        from datetime import datetime, timezone
        import workflow_state

        self.route["human_gate_bindings"] = [{"gate": "frame-review", "node": "owner"}]
        self._seal_route()
        self._write_owner("open")
        ledger = workflow_state.WorkflowLedger(self.route["route_id"],
                                                self.route["route_hash"], jobs=self.jobs)
        ledger.root.mkdir(parents=True, exist_ok=True)
        ledger.journal_path.write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"],
            "workflow_state": "BLOCKED_HUMAN_GATE",
            "evidence": {"gate": "frame-review"},
        }) + "\n", encoding="utf-8")
        result = self._observe()
        return next(row for row in result["items"] if row["source"] == "S2"), ledger.journal_path

    def _open_marker(self, question="S4-PRIVATE-QUESTION"):
        self.assertTrue(human_wait_state.open_signal(
            "opencode", "marker-session", "marker-seat",
            "[답변 필요] " + question, sequence=7))

    def _marker_wait(self, question="S4-PRIVATE-QUESTION"):
        self._open_marker(question)
        result = self._observe()
        return next(row for row in result["items"] if row["source"] == "S4")

    def _view_model(self, result):
        from fleet.explorer.live import build_view_model

        result = result or self.latest_result
        waits = human_wait_state.cached_fleet_view(request=False)
        sessions = []
        for index, item in enumerate(result["items"]):
            owner = item.get("owner") or {}
            if not owner.get("session_id"):
                continue
            sessions.append({"harness": owner.get("harness"),
                             "session_id": owner["session_id"],
                             "session_tag": "workflow-" + str(index),
                             "cwd": "/synthetic/repo", "liveness": "idle"})
        return build_view_model({"sessions": sessions, "answer_waits": waits,
                                 "project_of": lambda _cwd: "repo",
                                 "resources": [], "jobs": []}, now=self.now)

    @staticmethod
    def _row(vm, source):
        return next(row for row in vm.questions if row["source"] == source)

    @staticmethod
    def _read_in_ui(row, state=None):
        from fleet import render
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import NavState, reduce

        state = state or NavState(view="QUESTIONS")
        state, effects = reduce(state, {"type": "click", "action":
                                       ("question.select", row["id"], row["ref"])})
        if not effects or effects[0][0] != "questions.read":
            raise AssertionError("question selection did not request the real selected detail")
        envelope = ExplorerRuntime.execute_question_action(effects[0])
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_MODEL", SimpleNamespace(questions=(row,))), \
             mock.patch.object(render, "_EXPLORER_QUESTION_REFRESH_AT", 0):
            render._explorer_accept_question_result({"effect": effects[0], "result": envelope})
            adopted = render._EXPLORER_STATE
        if adopted.question_overlay.get("detail") != envelope:
            raise AssertionError("live render handler rejected the real workflow envelope")
        return adopted, envelope, effects[0]

    @staticmethod
    def _adopt_result(state, envelope, status="Selected request is ready."):
        from fleet import render
        from fleet.explorer.state import _question_action_ref

        ref = _question_action_ref(state.question_overlay)
        row = {"id": ref["id"], "ref": ref["_fleet_row_ref"]}
        operation = "questions.draft" if envelope.get("state") == "open" else "questions.submit"
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_MODEL", SimpleNamespace(questions=(row,))), \
             mock.patch.object(render, "_EXPLORER_QUESTION_REFRESH_AT", 0), \
             mock.patch.object(render, "_explorer_queue_question_effect"):
            render._explorer_accept_question_result({"effect": (operation, ref), "result": envelope})
            adopted = render._EXPLORER_STATE
        if adopted.question_overlay.get("detail") != envelope:
            raise AssertionError("live render handler rejected the real workflow action receipt")
        return adopted

    def test_live_renderer_refuses_changed_or_incomplete_workflow_identity(self):
        from fleet import render

        self._blocked_owner_wait()
        row = self._row(self._view_model(self.latest_result), "S3")
        _state, envelope, _ = self._read_in_ui(row)
        self.assertIsNotNone(render._explorer_question_envelope(row["ref"], envelope))
        for change in ({"session_id": "another-session"}, {"request_id": "another-request"},
                       {"route_id": "another-route"}, {"turn_id": "invented-native-turn"},
                       {"native_ref": {"transport": "codex-managed-control"}}):
            with self.subTest(change=change):
                self.assertIsNone(render._explorer_question_envelope(
                    row["ref"], {**envelope, **change}))
        missing_turn = dict(envelope)
        missing_turn.pop("turn_id")
        self.assertIsNone(render._explorer_question_envelope(row["ref"], missing_turn))
        native = {**envelope, "selected_id": "S1:native", "request_id": "native-request"}
        self.assertIsNone(render._explorer_question_envelope(
            {**row["ref"], "id": "S1:native"}, native))

    def _save_typed_draft(self, state, text):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce

        question_id = state.question_overlay["detail"]["question_id"]
        state, _ = reduce(state, {"type": "click", "action": ("question.edit", question_id)})
        if not state.question_overlay.get("editing"):
            raise AssertionError("private draft editor did not open: " +
                                 repr(state.question_overlay.get("detail")))
        saved = None
        for chunk in (text[:8], text[8:]):
            if not chunk:
                continue
            state, effects = reduce(state, {"type": "key", "key": "text:" + chunk})
            if not effects or effects[0][0] != "questions.draft":
                raise AssertionError("typed question input did not save a private draft: " +
                                     repr(state.question_overlay.get("detail")))
            saved = ExplorerRuntime.execute_question_action(effects[0])
            self.assertEqual(saved.get("draft"), state.question_overlay["draft"].get(
                "answers", {}).get(question_id, [None])[0])
        return state, saved

    def test_s3_body_draft_explicit_submit_and_retained_receipt_are_private_and_one_shot(self):
        import dispatch_owner_input
        from fleet.explorer.frame import draw
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce, serialize

        self._open_marker("S4-switch-away")
        source = self._blocked_owner_wait()
        vm = self._view_model(self.latest_result)
        row, marker_row = self._row(vm, "S3"), self._row(vm, "S4")
        state, envelope, _read_effect = self._read_in_ui(row)

        self.assertEqual(envelope["state"], "open")
        self.assertTrue(envelope["support"]["supported"])
        self.assertEqual(envelope["native_ref"]["transport"], "hearting-owner-input")
        self.assertEqual(envelope["question_id"], "owner-correction")
        self.assertIsNone(envelope["turn_id"])
        self.assertEqual(envelope["item_id"], source["attempt_id"])
        body = envelope["detail"]["question"]
        self.assertTrue(body)
        panel = "\n".join(draw(vm, state, 140, 45).text_rows())
        self.assertIn(body, panel)
        self.assertIn("select to view and answer", panel)

        draft_text = "Keep the exact route and retry safely"
        state, saved = self._save_typed_draft(state, draft_text)
        self.assertEqual(saved["draft"], draft_text)
        state = self._adopt_result(state, saved,
                                   "Private draft saved; submit explicitly to send.")
        panel = "\n".join(draw(vm, state, 140, 45).text_rows())
        self.assertIn(draft_text, panel)
        self.assertIn("[ submit answer ]", panel)
        exported = json.dumps(vm.to_mapping(), ensure_ascii=False, default=str)
        fleet_cache = human_wait_state._fleet_cache_path().read_text(encoding="utf-8")
        self.assertNotIn(body, exported)
        self.assertNotIn(draft_text, exported)
        self.assertNotIn(body, fleet_cache)
        self.assertNotIn(draft_text, fleet_cache)
        self.assertNotIn("question_overlay", serialize(state))

        # The private draft survives changing selection and reopening the live request.
        state, marker_envelope, _ = self._read_in_ui(marker_row, state)
        self.assertEqual(marker_envelope["native_ref"]["transport"], "hearting-answer-marker")
        state, reopened, _ = self._read_in_ui(row, state)
        self.assertEqual(reopened["state"], "open")
        self.assertEqual(reopened["draft"], draft_text)
        state = self._adopt_result(state, reopened)

        state, submit_effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(len(submit_effects), 1)
        submit_effect = submit_effects[0]
        self.assertEqual(submit_effect[0], "questions.submit")
        self.assertEqual(submit_effect[1]["native_ref"], envelope["native_ref"])
        self.assertEqual(submit_effect[2], {"answers": {"owner-correction": [draft_text]}})
        submitted = ExplorerRuntime.execute_question_action(submit_effect)
        self.assertEqual(submitted["state"], "received")
        self.assertEqual(submitted["result"]["status"], "retained")

        receipt = dispatch_owner_input.inspect(self.jobs, source["attempt_id"])
        self.assertEqual(len(receipt["requests"]), 1)
        self.assertEqual(receipt["requests"][0]["state"], "retained")
        self.assertEqual(dispatch_owner_input.retained(self.jobs, source["attempt_id"])[0]["text"],
                         draft_text)
        duplicate = ExplorerRuntime.execute_question_action(submit_effect)
        self.assertEqual(duplicate["state"], "received")
        self.assertEqual(len(dispatch_owner_input.inspect(
            self.jobs, source["attempt_id"])["requests"]), 1)

        state = self._adopt_result(state, submitted, "Correction received; awaiting owner.")
        from fleet.explorer.state import _question_action_ref
        receipt_ref = _question_action_ref(state.question_overlay)
        reopened_receipt = ExplorerRuntime.execute_question_action(("questions.read", receipt_ref))
        self.assertEqual(reopened_receipt["state"], "received")
        self.assertEqual(reopened_receipt["draft"], draft_text)
        self.assertEqual(reopened_receipt["result"]["status"], "retained")
        self.assertNotIn(body, repr(vm))
        self.assertNotIn(draft_text, repr(vm))

    def test_opencode_parent_uses_the_actual_owner_input_transport(self):
        self._assert_parent_harness_reply("opencode")

    def test_claude_parent_uses_the_actual_owner_input_transport(self):
        self._assert_parent_harness_reply("claude")

    def _assert_parent_harness_reply(self, harness):
        import dispatch_owner_input
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce

        metadata = self._metadata
        identity = {"AGENT_DISPATCH_CALLER_HARNESS": harness,
                    "CODEX_THREAD_ID": "" if harness != "codex" else "parent-session",
                    "CLAUDE_CODE_SESSION_ID": "parent-session" if harness == "claude" else "",
                    "CLAUDE_SESSION_ID": "",
                    "OPENCODE_SESSION_ID": "parent-session" if harness == "opencode" else ""}
        with mock.patch.dict(os.environ, identity):
            with mock.patch.object(self, "_metadata", side_effect=lambda *args, **kwargs:
                    metadata(*args, **kwargs).replace(
                        "parent_harness=codex", "parent_harness=" + harness)):
                source = self._blocked_owner_wait()
            row = self._row(self._view_model(self.latest_result), "S3")
            state, envelope, _ = self._read_in_ui(row)
            self.assertEqual(envelope["harness"], harness)
            self.assertEqual(envelope["native_ref"]["transport"], "hearting-owner-input")
            state, saved = self._save_typed_draft(state, "Continue this exact owner")
            state = self._adopt_result(state, saved)
            state, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
            self.assertEqual(len(effects), 1)
            receipt = ExplorerRuntime.execute_question_action(effects[0])
            self.assertEqual(receipt["state"], "received")
            self.assertEqual(len(dispatch_owner_input.inspect(
                self.jobs, source["attempt_id"])["requests"]), 1)

    def test_workflow_free_text_does_not_widen_native_explicit_choices(self):
        from fleet.explorer.state import question_free_text_supported, question_values_valid

        native = {"harness": "codex", "kind": "question", "question_id": "native-q",
                  "native_ref": {"transport": "codex-managed-control"},
                  "detail": {"questions": [{"id": "native-q", "choices": [],
                                            "is_other": False}]}}
        self.assertFalse(question_free_text_supported(native, "native-q"))
        self.assertFalse(question_values_valid(native, "native-q", ["unoffered answer"]))

    def test_s2_and_s4_real_source_details_keep_private_drafts_and_refuse_submit(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import NavState, reduce, serialize

        self._open_marker()
        s2, journal = self._human_gate_wait()
        result = self.latest_result
        s4 = next(row for row in result["items"] if row["source"] == "S4")
        vm = self._view_model(result)
        rows = {source: self._row(vm, source) for source in ("S2", "S4")}
        self.assertEqual({row["id"] for row in rows.values()}, {s2["id"], s4["id"]})
        journal_before = journal.read_bytes()
        state = NavState(view="QUESTIONS")
        private_values = []

        for source in ("S2", "S4"):
            row = rows[source]
            state, envelope, _ = self._read_in_ui(row, state)
            self.assertEqual(envelope["state"], "open", envelope)
            self.assertFalse(envelope["support"]["supported"])
            self.assertIsNone(envelope["turn_id"])
            self.assertEqual(envelope["native_ref"]["transport"],
                             "hearting-human-gate" if source == "S2" else
                             "hearting-answer-marker")
            self.assertEqual(envelope["support"]["reason"],
                             "workflow-gate-artifact-unavailable" if source == "S2" else
                             "marker-has-no-bound-native-response-surface")
            question_body = envelope["detail"].get("question")
            display_detail = question_body or envelope["detail"].get("gate")
            self.assertTrue(display_detail)
            panel = "\n".join(draw(vm, state, 140, 45).text_rows())
            self.assertIn(display_detail, panel)
            self.assertIn("No verified direct response; check the native prompt.", panel)
            self.assertNotIn("[ submit answer ]", panel)

            draft_text = "Private " + source + " response draft"
            if question_body:
                private_values.append(question_body)
            private_values.append(draft_text)
            state, saved = self._save_typed_draft(state, draft_text)
            self.assertEqual(saved["draft"], draft_text)
            state = self._adopt_result(state, saved,
                                       "Private draft saved; use the native prompt to respond.")
            state, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
            self.assertEqual(effects, ())
            state, effects = reduce(state, {"type": "key", "key": "enter"})
            self.assertEqual(effects, ())
            self.assertFalse(state.question_overlay["editing"])
            self.assertEqual(state.question_overlay["draft"]["answers"][
                envelope["question_id"]], [draft_text])

            other = rows["S4" if source == "S2" else "S2"]
            state, _other_envelope, _ = self._read_in_ui(other, state)
            state, reopened, _ = self._read_in_ui(row, state)
            self.assertEqual(reopened["draft"], draft_text)
            self.assertFalse(reopened["support"]["supported"])
            from fleet.explorer.state import _question_action_ref
            private_ref = _question_action_ref(state.question_overlay)
            refused = ExplorerRuntime.execute_question_action((
                "questions.submit", private_ref,
                {"answers": {envelope["question_id"]: [draft_text]}}))
            self.assertFalse(refused["support"]["supported"])

        self.assertEqual(journal.read_bytes(), journal_before)
        self.assertTrue(human_wait_state.signal_is_current({"id": s4["id"],
                                                             "seat_key": "marker-seat"}))
        exported = json.dumps(vm.to_mapping(), ensure_ascii=False, default=str)
        fleet_cache = human_wait_state._fleet_cache_path().read_text(encoding="utf-8")
        for private in (*private_values, "S4-PRIVATE-QUESTION"):
            self.assertNotIn(private, exported)
            self.assertNotIn(private, fleet_cache)
        self.assertNotIn("question_overlay", serialize(state))

    def test_changed_selection_rejects_late_result_and_queued_submit(self):
        from collections import deque
        from types import SimpleNamespace
        from fleet import render
        from fleet.explorer.state import reduce, _question_action_ref

        self._open_marker("late-result-switch")
        source = self._blocked_owner_wait()
        vm = self._view_model(self.latest_result)
        s3_row, s4_row = self._row(vm, "S3"), self._row(vm, "S4")
        state, envelope, read_effect = self._read_in_ui(s3_row)
        state, _ = reduce(state, {"type": "click", "action":
                                  ("question.edit", "owner-correction")})
        state, _ = reduce(state, {"type": "key", "key": "text:do not retarget"})
        stale_submit = ("questions.submit", _question_action_ref(state.question_overlay),
                        {"answers": {"owner-correction": ["do not retarget"]}})

        state, s4_envelope, _ = self._read_in_ui(s4_row, state)
        queue = deque([stale_submit])
        model = SimpleNamespace(questions=vm.questions)
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_MODEL", model), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": ""}):
            blocked = render._explorer_question_worker()
            self.assertEqual(blocked["result"]["state"], "unknown")
            render._explorer_accept_question_result({"effect": read_effect,
                                                     "result": envelope})
            self.assertEqual(render._EXPLORER_STATE.question_overlay["ref"]["id"],
                             s4_row["id"])
            self.assertEqual(render._EXPLORER_STATE.question_overlay["detail"], s4_envelope)
        self.assertEqual(self._owner_requests(source["attempt_id"]), [])

    def test_parent_replacement_refuses_selected_owner_correction(self):
        self._assert_owner_replacement_refuses("parent")

    def test_route_file_replacement_refuses_selected_owner_correction(self):
        self._assert_owner_replacement_refuses("route")

    def _assert_owner_replacement_refuses(self, replacement):
        import dispatch_owner_input
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce

        source = self._blocked_owner_wait()
        vm = self._view_model(self.latest_result)
        row = self._row(vm, "S3")
        state, _envelope, _ = self._read_in_ui(row)
        state, _ = reduce(state, {"type": "click", "action":
                                  ("question.edit", "owner-correction")})
        state, _ = reduce(state, {"type": "key", "key": "text:reject stale binding"})
        if replacement == "parent":
            self.jobs.write_text(self.jobs.read_text(encoding="utf-8").replace(
                "parent_sid=parent-session", "parent_sid=replacement-session"),
                encoding="utf-8")
        else:
            self.route["replacement"] = "new route identity"
            self._seal_route()
        _state, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(len(effects), 1)
        refused = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(refused["state"], "unknown")
        self.assertFalse(refused["support"]["supported"])
        ledger = json.loads(dispatch_owner_input._path(
            self.jobs, source["attempt_id"]).read_text(encoding="utf-8"))
        self.assertEqual(ledger["requests"], [])

    def _owner_requests(self, attempt):
        import dispatch_owner_input

        return dispatch_owner_input.inspect(self.jobs, attempt)["requests"]


class FleetS3OwnerContinuationTests(unittest.TestCase):
    """Exercise selected S3 answers through the public correction continuation."""

    _seal_route = FleetWorkflowQuestionIntegrationTests._seal_route
    _metadata = FleetWorkflowQuestionIntegrationTests._metadata
    _write_owner = FleetWorkflowQuestionIntegrationTests._write_owner
    _observe = FleetWorkflowQuestionIntegrationTests._observe
    _blocked_owner_wait = FleetWorkflowQuestionIntegrationTests._blocked_owner_wait

    def setUp(self):
        FleetWorkflowQuestionIntegrationTests.setUp(self)
        import work_start

        project_root = Path(__file__).resolve().parents[3]
        router = work_start._route_module()
        gate = {"spec_read": {"satisfied": True, "source": "canonical-prd-sha256"},
                "drift_verdict": "within-spec", "workflow_mode": "tracked",
                "artifact_guard": {"satisfied": True, "source": "conductor-prechecked"}}
        route = router.compile_route(
            "autopilot-code", "dev", "direct", str(project_root), str(self.artifacts),
            predicates=["atomic-outcome", "known-scope", "no-shared-contract",
                        "no-resource-run", "no-artifact-handoff", "no-independent-verifier",
                        "focused-verification"],
            signals=[], transport=None, inline_reason="atomic-direct", tracking="tracked",
            tracked_gate_evidence=gate)
        route["work_request"] = {"text": "Continue the selected owner's approved route.",
                                 "owner_harness": "codex"}
        self.route = route
        self._seal_route()
        router.verify_route(self.route, str(project_root))
        self.public_correct_commands = []

    def _public_correct_runner(self, *, lose_reply=False):
        import subprocess

        real_run = subprocess.run
        calls = []
        files = []

        def run(command, *args, **kwargs):
            if (not isinstance(command, (list, tuple)) or len(command) < 3
                    or Path(str(command[1])).name != "capability-route.py"
                    or command[2] != "correct"):
                return real_run(command, *args, **kwargs)
            command = list(command)
            calls.append(command)
            message_path = Path(command[command.index("--message-file") + 1])
            self.assertTrue(message_path.is_file())
            self.assertEqual(stat.S_IMODE(message_path.stat().st_mode), 0o600)
            files.append(message_path)
            completed = real_run(command, *args, **kwargs)
            if lose_reply:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 30))
            return completed

        return run, calls, files

    def _selected_owner(self):
        item = self._blocked_owner_wait()
        selected = human_wait_state.read_selected_request({"id": item["id"]})
        self.assertEqual(selected["state"], "open")
        self.assertTrue(selected["support"]["supported"], selected)
        self.assertEqual(selected["native_ref"]["attempt_id"], item["attempt_id"])
        return item, selected

    def test_public_correct_continues_once_and_refreshes_the_private_receipt(self):
        import dispatch_owner_input

        item, selected = self._selected_owner()
        answer = "private continuation answer"
        drafted = human_wait_state.write_selected_draft(selected, answer)
        runner, calls, files = self._public_correct_runner()
        with mock.patch.object(human_wait_state.subprocess, "run", side_effect=runner):
            result = human_wait_state.submit_selected_response(
                drafted, {"answers": {"owner-correction": [answer]}},
                action_id="s3-public-correct-once")

        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[argv.index("--attempt-id") + 1], item["attempt_id"])
        self.assertEqual(argv[argv.index("--request-id") + 1], "s3-public-correct-once")
        self.assertNotIn(answer, " ".join(argv))
        self.assertEqual(len(files), 1)
        self.assertFalse(files[0].exists())
        self.assertEqual(result["state"], "received", result)
        self.assertEqual(result["result"]["status"], "retained", result)
        self.assertEqual(result["result"]["target_attempt_id"], item["attempt_id"])
        self.assertEqual(result["result"]["continuation"]["state"], "inline")
        receipt = dispatch_owner_input.inspect(self.jobs, item["attempt_id"])
        self.assertEqual([row["id"] for row in receipt["requests"]], ["s3-public-correct-once"])
        self.assertEqual(receipt["requests"][0]["state"], "retained")
        self.assertNotIn(answer, self.jobs.read_text(encoding="utf-8"))
        self.assertNotIn(answer, human_wait_state._fleet_cache_path().read_text(encoding="utf-8"))

        empty = dict(self.latest_result, items=[], total=0, older=0, unattributed=[])
        self.assertTrue(human_wait_state.publish_cache(empty))
        refreshed = human_wait_state.read_selected_request(selected)
        self.assertEqual(refreshed["state"], "received", refreshed)
        self.assertEqual(refreshed["result"]["status"], "retained", refreshed)
        self.assertEqual(refreshed["result"]["target_attempt_id"], item["attempt_id"])
        self.assertEqual(refreshed["result"]["continuation"]["state"], "inline")
        self.assertNotIn(answer, human_wait_state._fleet_cache_path().read_text(encoding="utf-8"))

    def test_lost_correct_reply_is_not_replayed_and_exact_receipt_recovers(self):
        import dispatch_owner_input

        item, selected = self._selected_owner()
        answer = "private answer with a lost command reply"
        drafted = human_wait_state.write_selected_draft(selected, answer)
        runner, calls, files = self._public_correct_runner(lose_reply=True)
        with mock.patch.object(human_wait_state.subprocess, "run", side_effect=runner):
            ambiguous = human_wait_state.submit_selected_response(
                drafted, {"answers": {"owner-correction": [answer]}},
                action_id="s3-lost-correct-reply")
            recovered = human_wait_state.submit_selected_response(
                drafted, {"answers": {"owner-correction": [answer]}},
                action_id="s3-lost-correct-reply")

        self.assertEqual(ambiguous["state"], "unknown", ambiguous)
        self.assertEqual(recovered["state"], "received", recovered)
        self.assertEqual(recovered["result"]["status"], "retained")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(files), 1)
        self.assertFalse(files[0].exists())
        receipt = dispatch_owner_input.inspect(self.jobs, item["attempt_id"])
        self.assertEqual([row["id"] for row in receipt["requests"]], ["s3-lost-correct-reply"])

    def test_registered_worker_and_foreign_parent_cannot_submit(self):
        _item, selected = self._selected_owner()
        for context in ({"AGENT_DISPATCH_REGISTERED_WORKER": "1"},
                        {"CODEX_THREAD_ID": "foreign-parent"}):
            with self.subTest(context=context), \
                 mock.patch.dict(os.environ, context), \
                 mock.patch.object(human_wait_state.subprocess, "run",
                                   side_effect=AssertionError("public correct must not run")) as run:
                refused = human_wait_state.submit_selected_response(
                    selected, {"answers": {"owner-correction": ["must stay local"]}},
                    action_id="s3-refused-context")
                self.assertFalse(refused["support"]["supported"])
                self.assertEqual(refused["reason"],
                                 "owner-correction-native-parent-context-required")
                run.assert_not_called()

    def test_changed_parent_route_target_or_lineage_refuses_before_public_correct(self):
        cases = ("parent", "route", "target", "lineage")
        for change in cases:
            with self.subTest(change=change):
                item, selected = self._selected_owner()
                drafted = human_wait_state.write_selected_draft(selected, "keep this selected answer")
                if change == "parent":
                    self.jobs.write_text(self.jobs.read_text(encoding="utf-8").replace(
                        "parent_sid=parent-session", "parent_sid=replacement-session"),
                        encoding="utf-8")
                elif change == "route":
                    self.route["changed_after_selection"] = True
                    self._seal_route()
                elif change == "target":
                    drafted = dict(drafted)
                    drafted["native_ref"] = {**drafted["native_ref"],
                                             "attempt_id": "att-different-selected-target"}
                else:
                    fields = self.jobs.read_text(encoding="utf-8").rstrip("\n").split("\t")
                    fields[5] += (",replacement_family_id=unrecorded-family"
                                  ",replacement_attempt_id=att-unrecorded"
                                  ",replacement_claim_digest=" + "a" * 64)
                    self.jobs.write_text("\t".join(fields) + "\n", encoding="utf-8")
                with mock.patch.object(human_wait_state.subprocess, "run",
                                       side_effect=AssertionError("changed selection must not submit")) as run:
                    refused = human_wait_state.submit_selected_response(
                        drafted, {"answers": {"owner-correction": ["keep this selected answer"]}},
                        action_id="s3-changed-selection")
                self.assertIn(refused["state"], {"unknown", "open"}, refused)
                run.assert_not_called()


class FleetS2WorkflowResponseTests(unittest.TestCase):
    """Exercise S2 selection through the current ledger and public release command."""

    _seal_route = FleetWorkflowQuestionIntegrationTests._seal_route
    _write_owner = FleetWorkflowQuestionIntegrationTests._write_owner
    _observe = FleetWorkflowQuestionIntegrationTests._observe
    _view_model = FleetWorkflowQuestionIntegrationTests._view_model
    _row = staticmethod(FleetWorkflowQuestionIntegrationTests._row)
    _read_in_ui = staticmethod(FleetWorkflowQuestionIntegrationTests._read_in_ui)
    _adopt_result = staticmethod(FleetWorkflowQuestionIntegrationTests._adopt_result)

    def _metadata(self, attempt="att-workflow-owner", parent_sid="parent-session"):
        value = FleetWorkflowQuestionIntegrationTests._metadata(self, attempt, parent_sid)
        return value.replace("completion_delivery=app-server-supervised",
                             "completion_delivery=app-server-supervised,"
                             "parent_completion_delivery=claude-parent-runtime")

    def setUp(self):
        FleetWorkflowQuestionIntegrationTests.setUp(self)
        agent_home = self.agent_home
        local_env = mock.patch.dict(os.environ, {
            "AGENT_HOME": str(agent_home),
            "AGENT_WORKFLOW_ROOT": str(self.root / "workflow"),
            "AGENT_DISPATCH_REGISTERED_WORKER": "",
        })
        local_env.start()
        self.addCleanup(local_env.stop)
        self.route = {
            "schema_version": 1, "capability": "autopilot-lab", "capability_mode": "setup",
            "effective_intensity": "standard", "cwd": str(self.root),
            "artifact_root": str(self.artifacts), "human_gates": ["frame-review"],
            "human_gate_bindings": [{"gate": "frame-review", "node": "verify",
                                      "position": "entry"}],
            "nodes": [
                {"id": "frame", "kind": "resource-runner", "depends_on": [],
                 "outputs": ["frame.json"], "write_scope": ["frame.json"],
                 "completion_gate": "fixture-frame",
                 "continuation": {"kind": "human-gate", "gate": "frame-review"}},
                {"id": "verify", "kind": "review-worker", "depends_on": ["frame"],
                 "outputs": ["reviews/verdict.json"], "write_scope": ["reviews/**"],
                 "completion_gate": "fixture-verify", "dispatch_depth": 2,
                 "terminal": True, "terminal_gate": "fixture-verify"},
            ],
        }
        self._seal_route()
        import importlib.util
        project_root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location(
            "workflow_supervisor_s2_fixture", project_root / "utilities" / "workflow-supervisor.py")
        self.supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.supervisor)

    def _interview_value(self):
        import frame_interview as FI

        return {
            "schema": FI.SCHEMA, "route_id": self.route["route_id"], "round": 1,
            "summary": "shards/frame/frame-summary.json",
            "understanding": "You want the approval step to reach you promptly and ask clear questions.",
            "brief": {"problem": "Approval arrives late.", "outcome": "It arrives promptly.",
                      "affected": "You and the assistant.",
                      "constraints": "No new background service.", "open": ""},
            "questions": [{
                "id": "q-scope", "topic": "Scope", "question": "Which parts should change?",
                "kind": "choice",
                "options": [{"label": "Both", "means": "Change both parts."},
                            {"label": "Approval only", "means": "Change the approval step."}],
                "recommended": 0, "why": "Only you can choose the scope.",
            }, {
                "id": "q-limit", "topic": "Limit", "question": "What limit should apply?",
                "kind": "choice",
                "options": [{"label": "Small", "means": "Keep the change small."},
                            {"label": "Other", "means": "Use another limit."}],
                "recommended": 0, "why": "Only you can choose this limit.",
            }],
        }

    def _open_gate(self, *, interview=True, age_days=0):
        import contextlib
        import io
        from datetime import datetime, timedelta, timezone
        import workflow_state as WS

        artifact = None
        if interview:
            artifact = self.artifacts / "shards" / "frame" / "interview.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(json.dumps(self._interview_value(), ensure_ascii=False),
                                encoding="utf-8")
        else:
            artifact = self.artifacts / "shards" / "frame" / "review.md"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("Review the current frame decision.\n", encoding="utf-8")
        self._write_owner("open")
        args = ["gate", "--route", str(self.route_file), "--gate", "frame-review",
                "--block", "--jobs", str(self.jobs)]
        args.extend(("--artifact", str(artifact)))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.supervisor.main(args), 0)
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"],
                                   jobs=self.jobs)
        if age_days:
            journal = ledger.journal()
            raise_row = next(row for row in journal
                             if row.get("workflow_state") == "BLOCKED_HUMAN_GATE")
            raise_row["at"] = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
            ledger.journal_path.write_text("".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in journal), encoding="utf-8")
        self._observe()
        return ledger

    def _question_response(self, envelope):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce

        state = self._read_in_ui(self._row(self._view_model(self.latest_result), "S2"))[0]
        for question_id, value in (("_understanding", "I understand the restatement"),
                                   ("q-scope", "Both")):
            state, effects = reduce(state, {"type": "click", "action":
                                            ("question.choice", question_id, value)})
            self.assertEqual(len(effects), 1)
            self.assertEqual(effects[0][0], "questions.draft")
            saved = ExplorerRuntime.execute_question_action(effects[0])
            self.assertEqual(saved["state"], "open", saved)
            state = self._adopt_result(state, saved)
        state, _ = reduce(state, {"type": "click", "action": ("question.edit", "q-limit")})
        state, effects = reduce(state, {"type": "key", "key": "text:Keep the default small"})
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.draft")
        saved = ExplorerRuntime.execute_question_action(effects[0])
        state = self._adopt_result(state, saved)
        state, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.submit")
        return state, effects[0]

    def test_interview_answers_use_real_current_release_and_one_receipt(self):
        import contextlib
        import io
        from fleet.explorer.frame import draw
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import serialize

        ledger = self._open_gate()
        vm = self._view_model(self.latest_result)
        row = self._row(vm, "S2")
        state, envelope, _ = self._read_in_ui(row)
        self.assertEqual(envelope["state"], "open")
        self.assertTrue(envelope["support"]["supported"], envelope)
        self.assertEqual(envelope["source"], "S2")
        self.assertEqual(envelope["native_ref"]["transport"], "hearting-human-gate")
        self.assertIsNone(envelope["turn_id"])
        self.assertEqual(envelope["gate_epoch"], 1)
        self.assertEqual({question["id"] for question in envelope["detail"]["questions"]},
                         {"_understanding", "q-scope", "q-limit"})
        panel = "\n".join(draw(vm, state, 140, 55).text_rows())
        self.assertIn("Which parts should change?", panel)
        self.assertIn("Both", panel)
        self.assertIn("target: verify", panel)
        self.assertIn("submit answers to proceed", panel)

        state, effect = self._question_response(envelope)
        draft = state.question_overlay["draft"]
        self.assertEqual(draft["answers"]["q-limit"], ["Keep the default small"])
        cache = json.dumps(vm.to_mapping(), ensure_ascii=False, default=str)
        public_cache = human_wait_state._fleet_cache_path().read_text(encoding="utf-8")
        self.assertNotIn("Which parts should change?", cache)
        self.assertNotIn("Keep the default small", cache)
        self.assertNotIn("Which parts should change?", public_cache)
        self.assertNotIn("Keep the default small", public_cache)
        self.assertNotIn("question_overlay", json.dumps(serialize(state)))

        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_REGISTERED_WORKER": ""}), \
             mock.patch.object(human_wait_state.subprocess, "run",
                               wraps=human_wait_state.subprocess.run) as release:
            result = ExplorerRuntime.execute_question_action(effect)
            self.assertEqual(release.call_count, 1)
            self.assertEqual(result["state"], "applied", result)
            state = self._adopt_result(state, result)
            self.assertEqual(state.question_overlay["state"], "applied")
            duplicate = ExplorerRuntime.execute_question_action(effect)
            self.assertEqual(duplicate["state"], "applied", duplicate)
            self.assertEqual(release.call_count, 1)
        resolution = __import__("workflow_state").human_gate_resolution(
            ledger.journal(), "frame-review")
        self.assertEqual((resolution["status"], resolution["epoch"]), ("proceed", 1))
        self.assertEqual(result["result"]["request_id"], result["action_id"])

    def test_empty_and_defer_leave_the_current_gate_blocked(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import reduce

        ledger = self._open_gate(interview=False)
        vm = self._view_model(self.latest_result)
        row = self._row(vm, "S2")
        state, envelope, _ = self._read_in_ui(row)
        self.assertTrue(envelope["support"]["supported"], envelope)
        panel = "\n".join(draw(vm, state, 120, 35).text_rows())
        self.assertIn("choose proceed, revise, or stop", panel)
        state, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects, ())
        state, effects = reduce(state, {"type": "click", "action": ("question.decision", "defer")})
        self.assertEqual(effects, ())
        self.assertEqual(ledger.state()["workflow_state"], "BLOCKED_HUMAN_GATE")

    def test_headless_release_refusal_keeps_the_gate_blocked(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce

        ledger = self._open_gate()
        vm = self._view_model(self.latest_result)
        row = self._row(vm, "S2")
        state, envelope, _ = self._read_in_ui(row)
        state, effect = self._question_response(envelope)
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_REGISTERED_WORKER": "1"}):
            refused = ExplorerRuntime.execute_question_action(effect)
        self.assertEqual(refused["state"], "unknown")
        self.assertEqual(ledger.state()["workflow_state"], "BLOCKED_HUMAN_GATE")
        self.assertEqual(human_wait_state._selected_entry(envelope)["state"], "unknown")

    def test_reraise_has_a_new_epoch_and_does_not_inherit_old_draft(self):
        from datetime import datetime, timedelta, timezone
        from fleet.explorer.runtime import ExplorerRuntime

        ledger = self._open_gate()
        first = self._row(self._view_model(self.latest_result), "S2")
        _state, first_envelope, _ = self._read_in_ui(first)
        first_draft = human_wait_state.write_selected_draft(first_envelope,
                                                            "keep this old answer private")
        old_identity = human_wait_state._selected_identity(first_draft)
        journal = ledger.journal()
        second_raise = next(row for row in journal
                            if row.get("workflow_state") == "BLOCKED_HUMAN_GATE")
        second_raise = dict(second_raise)
        now = datetime.now(timezone.utc)
        first_raise = next(row for row in journal
                           if row.get("workflow_state") == "BLOCKED_HUMAN_GATE")
        first_raise["at"] = (now - timedelta(seconds=3)).isoformat()
        second_raise["at"] = (now - timedelta(seconds=1)).isoformat()
        ledger.journal_path.write_text("".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in journal), encoding="utf-8")
        with ledger.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(second_raise, ensure_ascii=False) + "\n")
        self._observe()
        second = self._row(self._view_model(self.latest_result), "S2")
        _state, second_envelope, _ = self._read_in_ui(second)
        self.assertEqual(second_envelope["gate_epoch"], 2)
        self.assertNotEqual(human_wait_state._selected_identity(second_envelope), old_identity)
        self.assertEqual(second_envelope.get("draft", ""), "")
        late = ExplorerRuntime.execute_question_action(("questions.submit", first_draft,
            {"answers": {"_understanding": ["I understand the restatement"],
                         "q-scope": ["Both"], "q-limit": ["Keep this old answer private"]}}))
        self.assertEqual(late["state"], "unknown")
        self.assertEqual(ledger.state()["workflow_state"], "BLOCKED_HUMAN_GATE")

    def test_an_old_but_still_blocked_raise_remains_selectable(self):
        ledger = self._open_gate(age_days=4)
        row = self._row(self._view_model(self.latest_result), "S2")
        self.assertTrue(row.get("older"))
        _state, envelope, _ = self._read_in_ui(row)
        self.assertEqual(envelope["state"], "open")
        self.assertTrue(envelope["support"]["supported"], envelope)
        self.assertEqual(ledger.state()["workflow_state"], "BLOCKED_HUMAN_GATE")


if __name__ == "__main__":
    unittest.main()
