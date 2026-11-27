#!/usr/bin/env python3
"""A native question tool call becomes this session's Hearting question, the same on all three harnesses."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))
import human_wait_state as state  # noqa: E402
import native_question_redirect as redirect  # noqa: E402
import questions  # noqa: E402

SID = "native-question-session"
OPTIONS = [("Proceed (Recommended)", "Start the work"), ("Edit", "Change the plan"), ("Stop", "Do not start")]


def claude_payload(questions_=None, **extra):
    listed = questions_ or [{"question": "Start now?", "header": "Start", "multiSelect": False,
                             "options": [{"label": label, "description": text} for label, text in OPTIONS]}]
    return {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
            "tool_input": {"questions": listed}, **extra}


def codex_payload(**extra):
    return {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "request_user_input",
            "tool_input": {"questions": [{"id": "start", "header": "Start", "question": "Start now?",
                "options": [{"label": label, "description": text} for label, text in OPTIONS]}]}, **extra}


def opencode_payload(**extra):
    return {"tool": "question", "sessionID": SID, "args": {"questions": [{"question": "Start now?",
            "header": "Start", "options": [{"label": label, "description": text} for label, text in OPTIONS]}]},
            **extra}


class Base(unittest.TestCase):
    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        import session_tidy as tidy
        from fleet import process_identity
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        for patch in (mock.patch.object(tidy, "resolve_seat",
                                        side_effect=lambda harness, *a, **k: self.f.seat(harness, a[2] if len(a) > 2 else SID)),
                      mock.patch.object(process_identity, "current_launch_context", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def answered(self, harness):
        """The person answered this session's waiting question: its source is gone, so a new one may register."""
        for signal in state.read_signals():
            if signal["harness"] == harness and signal["session_id"] == SID:
                state._remove_signal_source(signal, state._signal_path(signal))

    def registered(self, harness, reason):
        """The one registered question a refusal names."""
        self.assertIsNotNone(reason)
        item_id = re.search(r"(S4:[0-9a-f]{64})", reason).group(1)
        envelope = state.read_selected_request({"id": item_id, "harness": harness, "session_id": SID})
        return questions.questions_of(envelope)


class RedirectTest(Base):
    def test_redirected_choice_and_note_reach_each_original_session(self):
        for harness, payload in (("claude", claude_payload()), ("codex", codex_payload()),
                                 ("opencode", opencode_payload())):
            with self.subTest(harness=harness):
                reason = redirect.redirect(harness, payload)
                self.registered(harness, reason)
                signal = next(x for x in state.read_signals() if x["harness"] == harness)
                envelope = state.read_selected_request({"id": signal["id"], "harness": harness, "session_id": SID})
                question = questions.questions_of(envelope)[0]
                choice = questions._choice_value(question["choices"][0])
                values = [choice, "간격을 넓혀 주세요."]
                result = state.submit_selected_response(envelope, {"answers": {"message-response": values}}, action_id="combined-"+harness)
                self.assertEqual(result["state"], "submitted", result)
                calls, _, pending = self.f.courier(signal)
                self.assertEqual(len(calls), 1)
                wire = pending["text"]
                self.assertIn(json.dumps(values, ensure_ascii=False), wire)
                self.assertEqual(pending["to"]["session_id"], SID)
                self.f.consume(signal, wire)
                self.assertEqual(state._read(state._signal_path(signal))["lifecycle"], "answered")

    def test_one_question_keeps_text_choices_and_the_recommended_choice(self):
        reason = redirect.redirect("claude", claude_payload())
        (question,) = self.registered("claude", reason)
        self.assertTrue(question["question"].startswith("Start now?"))
        self.assertIn("Change the plan", question["question"])             # each option's meaning travels with it
        self.assertEqual([questions._choice_label(c) for c in question["choices"]], ["Proceed", "Edit", "Stop"])
        self.assertEqual(question["recommended"], 0)

    def test_the_korean_marker_is_the_recommendation_too_and_any_position_works(self):
        payload = claude_payload([{"question": "진행할까요?", "multiSelect": False, "options": [
            {"label": "수정", "description": "계획을 고친다"}, {"label": "진행(권장)", "description": "시작한다"},
            {"label": "중단", "description": "시작하지 않는다"}]}])
        (question,) = self.registered("claude", redirect.redirect("claude", payload))
        self.assertEqual([questions._choice_label(c) for c in question["choices"]], ["수정", "진행", "중단"])
        self.assertEqual(question["recommended"], 1)
        self.answered("claude")
        none = claude_payload([{"question": "Which?", "multiSelect": False,
                                "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}])
        (plain,) = self.registered("claude", redirect.redirect("claude", none))
        self.assertIsNone(plain["recommended"])

    def test_the_refusal_is_one_short_line_with_the_id_and_no_new_duty(self):
        reason = redirect.redirect("claude", claude_payload())
        self.assertLess(len(reason), 220)
        self.assertNotIn("\n", reason)
        self.assertIn("End your turn", reason)
        self.assertRegex(reason, r"S4:[0-9a-f]{64}")
        for word in ("run ", "hearting run", "--", "command"):
            self.assertNotIn(word, reason)                                   # nothing for the agent to run or give

    def test_several_questions_are_one_text_question_listing_every_question_and_option(self):
        payload = claude_payload([
            {"question": "Which scope?", "multiSelect": False,
             "options": [{"label": "Small (Recommended)", "description": "one file"}, {"label": "Large", "description": "all"}]},
            {"question": "Which checks?", "multiSelect": True,
             "options": [{"label": "Unit", "description": "fast"}, {"label": "E2E", "description": "slow",
                                                                   "preview": "a long preview\nof the run"}]}])
        (question,) = self.registered("claude", redirect.redirect("claude", payload))
        self.assertEqual(question["choices"], [])
        self.assertTrue(question["free_text"])
        for part in ("Q1. Which scope?", "Q2. Which checks?", "Small (Recommended)", "one file",
                     "several may be chosen", "E2E", "a long preview of the run"):
            self.assertIn(part, question["question"])

    def test_a_single_multiselect_or_preview_question_is_written_as_text(self):
        for extra in ({"multiSelect": True}, {"multiSelect": False, "preview": "layout"}):
            with self.subTest(extra=extra):
                option = {"label": "A", "description": "a"}
                if "preview" in extra:
                    option["preview"] = extra["preview"]
                payload = claude_payload([{"question": "Pick?", "multiSelect": extra["multiSelect"],
                                           "options": [option, {"label": "B", "description": "b"}]}])
                (question,) = self.registered("claude", redirect.redirect("claude", payload))
                self.assertEqual(question["choices"], [])
                self.answered("claude")

    def test_the_three_harnesses_register_the_same_question(self):
        seen = {}
        for harness, payload in (("claude", claude_payload()), ("codex", codex_payload()),
                                 ("opencode", opencode_payload())):
            reason = redirect.redirect(harness, payload)
            (question,) = self.registered(harness, reason)
            seen[harness] = (question["question"], [questions._choice_label(c) for c in question["choices"]],
                             question["recommended"])
            self.answered(harness)
        self.assertEqual(seen["claude"], seen["codex"])
        self.assertEqual(seen["claude"], seen["opencode"])
        self.assertEqual(seen["claude"][2], 0)

    def test_each_harness_entry_prints_its_own_refusal_shape(self):
        for flag, payload in (("--claude", claude_payload()), ("--codex", codex_payload())):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = redirect.main([flag], stdin=io.StringIO(json.dumps(payload)))
            decision = json.loads(out.getvalue())["hookSpecificOutput"]
            self.assertEqual((code, decision["hookEventName"], decision["permissionDecision"]), (0, "PreToolUse", "deny"))
            self.assertRegex(decision["permissionDecisionReason"], r"S4:[0-9a-f]{64}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = redirect.main(["--opencode"], stdin=io.StringIO(json.dumps(opencode_payload())))
        self.assertEqual(code, 1)                                            # the plugin throws this line
        self.assertRegex(out.getvalue(), r"S4:[0-9a-f]{64}")


class PassThroughTest(Base):
    """A question the person cannot see is the only failure that matters: every doubt lets it through."""

    def board_empty(self):
        self.assertEqual(list(Path(os.environ["XDG_STATE_HOME"]).rglob("*message*")), [])

    def test_a_failed_registration_lets_every_harness_question_through_silently(self):
        for harness, payload, flag in (("claude", claude_payload(), "--claude"), ("codex", codex_payload(), "--codex"),
                                       ("opencode", opencode_payload(), "--opencode")):
            for failure in (OSError("state is not writable"), ValueError("question-seat-unproven"),
                            RuntimeError("no hearting")):
                with self.subTest(harness=harness, failure=type(failure).__name__), \
                     mock.patch.object(state, "register_message", side_effect=failure):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        code = redirect.main([flag], stdin=io.StringIO(json.dumps(payload)))
                    self.assertEqual((code, out.getvalue()), (0, ""))

    def test_a_different_question_beside_an_open_one_goes_through_as_before(self):
        """Only the same question is held back; a different one shows natively and the open one stays as it was."""
        import session_tidy as tidy
        for harness, payload in (("claude", claude_payload()), ("codex", codex_payload()),
                                 ("opencode", opencode_payload())):
            earlier, envelope = self.f.ask(harness, SID, text="Which scope first?",
                                           choices=["Small", "Large"], recommended=0)
            for turn in ("same turn", "next turn after an unrelated human prompt"):
                with self.subTest(harness=harness, turn=turn):
                    if turn != "same turn":
                        tidy.bump_prompt_seq(self.f.seat(harness, SID), harness, SID, time.time())
                    for again in range(3):                                   # asked again, still shown each time
                        self.assertIsNone(redirect.redirect(harness, payload))
                    self.assertEqual(state._read(state._signal_path(earlier))["id"], earlier["id"])
                    self.assertEqual(state._selected_entry(envelope)["state"], "open")
                    self.assertEqual([item["id"] for item in state.read_signals() if item["harness"] == harness],
                                     [earlier["id"]])
            self.answered(harness)

    def test_the_same_question_asked_again_natively_is_refused_with_the_open_id(self):
        """The 11-27 scene: `questions ask`, then the same question through the native tool in the same turn."""
        for harness, payload in (("claude", claude_payload()), ("codex", codex_payload()),
                                 ("opencode", opencode_payload())):
            for text in ("Start now?", "  start NOW? ",
                         redirect.build(redirect._normalized(harness, payload)[1])["text"]):
                with self.subTest(harness=harness, text=text[:20]):
                    earlier, envelope = self.f.ask(harness, SID, text=text,
                                                   choices=["Proceed", "Edit", "Stop"], recommended=0)
                    reason = redirect.redirect(harness, payload)
                    self.assertEqual(reason, redirect.reason_already(earlier["id"]))
                    self.assertLess(len(reason), 220)
                    self.assertNotIn("\n", reason)
                    self.assertEqual([item["id"] for item in state.read_signals() if item["harness"] == harness],
                                     [earlier["id"]])
                    self.assertEqual(state._selected_entry(envelope)["state"], "open")
                    self.answered(harness)
        # Other choices, or other words, are another question: shown natively, as before.
        for text, choices in (("Start now?", ["Proceed", "Stop"]), ("Start later?", ["Proceed", "Edit", "Stop"])):
            with self.subTest(text=text, choices=choices):
                self.f.ask("claude", SID, text=text, choices=choices)
                self.assertIsNone(redirect.redirect("claude", claude_payload()))
                self.answered("claude")

    def test_an_old_question_or_one_in_another_pane_is_held_only_when_it_is_the_same(self):
        import session_tidy as tidy
        old, _ = self.f.ask("claude", SID, text="Start now?", choices=["Proceed", "Edit", "Stop"])
        source = state._read(state._signal_path(old))
        source["since"] -= 73 * 3600                                         # folded on the panel, still open
        state._write_signal_source(source)
        self.assertTrue(next(item for item in state.read_signals() if item["id"] == old["id"])["stale"])
        self.assertEqual(redirect.redirect("claude", claude_payload()), redirect.reason_already(old["id"]))
        self.answered("claude")
        self.f.ask("claude", SID, text="Which scope first?", choices=["Small", "Large"])
        source = next(item for item in state.read_signals() if item["harness"] == "claude")
        source["since"] -= 73 * 3600
        state._write_signal_source({key: value for key, value in source.items() if key != "stale"})
        self.assertIsNone(redirect.redirect("claude", claude_payload()))
        self.answered("claude")
        # The same session in another pane: the same question is held, a different one registers here (as before).
        pane = tidy.session_seat(tidy.Seat("pane", "question-seat-claude-2", pane="fixture:p2", harness="claude"),
                                 "claude", SID)
        there = state.register_message("claude", SID, pane, "Start now?", sequence=tidy.read_prompt_seq(pane),
                                       choices=["Proceed", "Edit", "Stop"], cwd=str(self.f.root))
        self.assertEqual(redirect.redirect("claude", claude_payload()), redirect.reason_already(there["id"]))
        state._remove_signal_source(there, state._signal_path(there))
        there = state.register_message("claude", SID, pane, "Which scope first?", sequence=tidy.read_prompt_seq(pane),
                                       choices=["Small", "Large"], cwd=str(self.f.root))
        reason = redirect.redirect("claude", claude_payload())
        self.assertTrue(reason.startswith("Registered as Hearting question"), reason)
        self.assertNotIn(there["id"], reason)

    def test_a_question_replaced_just_after_it_was_read_is_never_the_one_named(self):
        earlier, _ = self.f.ask("claude", SID, text="Start now?", choices=["Proceed", "Edit", "Stop"])
        read = state._read(state._signal_path(earlier))
        later, _ = self.f.ask("claude", SID, text="Which scope first?", choices=["Small", "Large"])  # replaces it
        with mock.patch.object(redirect, "_open_questions", return_value=[read]):
            self.assertIsNone(redirect.redirect("claude", claude_payload()))   # a different question: shown
        self.assertEqual(state._read(state._signal_path(later))["id"], later["id"])

    def test_the_same_open_question_refuses_the_native_call_even_when_registration_cannot_run(self):
        """A seat or a state that cannot be written never puts a native window beside the same open question."""
        import session_tidy as tidy
        earlier, _ = self.f.ask("claude", SID, text="Start now?", choices=["Proceed", "Edit", "Stop"])
        with mock.patch.object(tidy, "resolve_seat", return_value=tidy.Seat("none", "", pane="")):
            self.assertIn(earlier["id"], redirect.redirect("claude", claude_payload()))
        with mock.patch.object(tidy, "resolve_seat", side_effect=RuntimeError("seat ledger unreadable")):
            self.assertIn(earlier["id"], redirect.redirect("claude", claude_payload()))
        for failure in (OSError("state is not writable"), RuntimeError("no hearting")):
            with self.subTest(failure=type(failure).__name__), \
                 mock.patch.object(state, "register_message", side_effect=failure) as register:
                self.assertIn(earlier["id"], redirect.redirect("claude", claude_payload()))
                register.assert_not_called()
        # Another session's or another harness's question, or an answer marker, is not this one's.
        other, _ = self.f.ask("claude", "another-session", text="Start now?", choices=["Proceed", "Edit", "Stop"])
        codex, _ = self.f.ask("codex", SID, text="Start now?", choices=["Proceed", "Edit", "Stop"])
        self.answered("claude")
        seat = self.f.seat("claude", SID)
        marker = state.signal_value("claude", SID, seat.key, "[답변 필요] Start now?", sequence=tidy.read_prompt_seq(seat))
        self.assertTrue(state.write_signal(marker))
        self.assertTrue(any(item.get("mode") != "message" for item in state.read_signals()))
        reason = redirect.redirect("claude", claude_payload())
        self.assertNotIn(other["id"], reason)
        self.assertNotIn(codex["id"], reason)
        self.assertTrue(reason.startswith("Registered as Hearting question"), reason)

    def test_an_answer_on_its_way_holds_back_only_the_turn_that_asked(self):
        """The person answered on the board: within that turn the answer comes as the next prompt (refused);
        after a later prompt a record that has not closed never holds the same question back."""
        import session_tidy as tidy
        earlier, envelope = self.f.ask("claude", SID, text="Start now?", choices=["Proceed", "Edit", "Stop"],
                                       recommended=0)
        result = state.submit_selected_response(envelope, {"answers": {"message-response": ["Proceed"]}},
                                                action_id="board-claude")
        self.assertEqual(result["state"], "submitted", result)
        self.assertEqual(redirect.redirect("claude", claude_payload()), redirect.reason_already(earlier["id"]))
        tidy.bump_prompt_seq(self.f.seat("claude", SID), "claude", SID, time.time())
        self.assertIsNone(redirect.redirect("claude", claude_payload()))     # the native question shows
        self.assertEqual(state._read(state._signal_path(earlier))["id"], earlier["id"])  # nothing replaced

    def test_the_same_question_registered_meanwhile_is_the_one_the_refusal_names(self):
        earlier, _ = self.f.ask("claude", SID, text="Start now?", choices=["Proceed", "Edit", "Stop"])
        with mock.patch.object(redirect, "same_waiting", side_effect=[None, earlier]):
            reason = redirect.redirect("claude", claude_payload())    # register_message refuses: open elsewhere
        self.assertEqual(reason, redirect.reason_already(earlier["id"]))
        with mock.patch.object(redirect, "same_waiting", side_effect=[None, None]):
            self.assertIsNone(redirect.redirect("claude", claude_payload()))   # it is another question: shown
        self.assertEqual([item["id"] for item in state.read_signals() if item["harness"] == "claude"], [earlier["id"]])

    def test_the_board_answer_reaches_the_session_after_the_refusal_once(self):
        """The person answers the waiting question on the board: it reaches that session once and closes."""
        for harness, payload in (("claude", claude_payload()), ("codex", codex_payload()),
                                 ("opencode", opencode_payload())):
            with self.subTest(harness=harness):
                earlier, envelope = self.f.ask(harness, SID, text="Start now?",
                                               choices=["Proceed", "Edit", "Stop"], recommended=0)
                self.assertIn(earlier["id"], redirect.redirect(harness, payload))
                result = state.submit_selected_response(envelope, {"answers": {"message-response": ["Proceed"]}},
                                                        action_id="board-" + harness)
                self.assertEqual(result["state"], "submitted", result)
                calls, _, pending = self.f.courier(earlier)
                self.assertEqual(len(calls), 1)
                self.assertEqual(pending["to"]["session_id"], SID)
                self.f.consume(earlier, pending["text"])
                self.assertEqual(state._read(state._signal_path(earlier))["lifecycle"], "answered")
                self.assertEqual(state.read_selected_request(
                    {"id": earlier["id"], "harness": harness, "session_id": SID})["state"], "applied")
                # Nothing waits now: the next native question registers as usual.
                self.assertTrue(redirect.redirect(harness, payload).startswith("Registered as Hearting question"))
                self.answered(harness)

    def test_a_question_whose_earlier_one_was_answered_still_registers(self):
        earlier, _ = self.f.ask("claude", SID, text="Earlier?", choices=["A", "B"])
        self.answered("claude")
        self.assertIsNotNone(redirect.redirect("claude", claude_payload()))

    def test_no_seat_is_a_pass_through(self):
        import session_tidy as tidy
        with mock.patch.object(tidy, "resolve_seat", return_value=tidy.Seat("none", "", pane="")):
            self.assertIsNone(redirect.redirect("claude", claude_payload()))

    def test_registered_workers_and_native_subagents_do_nothing(self):
        for env in ({"AGENT_SESSION_ROLE": "worker"}, {"AGENT_DISPATCH_CHILD": "1"}, {"AGENT_DISPATCH_DEPTH": "2"},
                    {"AGENT_SESSION_ROLE": "owner"}, {"OPENCODE_DISPATCH_SLUG": "w1"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env), \
                 mock.patch.object(state, "register_message") as register:
                self.assertIsNone(redirect.redirect("claude", claude_payload()))
                register.assert_not_called()
        with mock.patch.object(state, "register_message") as register:
            self.assertIsNone(redirect.redirect("claude", claude_payload(agent_id="sub-1")))
            register.assert_not_called()

    def test_other_tools_secrets_and_malformed_calls_are_left_alone(self):
        cases = [("claude", {**claude_payload(), "tool_name": "Bash"}),
                 ("claude", {"session_id": SID, "tool_name": "AskUserQuestion", "tool_input": {}}),
                 ("claude", claude_payload([{"question": " ", "options": []}])),
                 ("claude", {**claude_payload(), "session_id": ""}),
                 ("codex", {**codex_payload(), "tool_name": "update_plan"}),
                 ("opencode", {**opencode_payload(), "tool": "read"}),
                 ("claude", "not a payload"), ("claude", None)]
        secret = codex_payload()
        secret["tool_input"]["questions"][0]["isSecret"] = True
        cases.append(("codex", secret))
        for harness, payload in cases:
            with self.subTest(harness=harness, payload=str(payload)[:60]), \
                 mock.patch.object(state, "register_message") as register:
                self.assertIsNone(redirect.redirect(harness, payload))
                register.assert_not_called()

    def test_a_slow_registration_gives_up_inside_the_budget_and_the_native_question_shows(self):
        def slow(*args, **kwargs):
            time.sleep(10)
        with mock.patch.object(state, "register_message", side_effect=slow):
            started = time.monotonic()
            self.assertIsNone(redirect.redirect("claude", claude_payload(), seconds=1))
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4)

    def test_the_budget_is_inside_every_hook_timeout(self):
        claude = json.loads((ROOT / "adapters/claude/settings.json").read_text())
        codex = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text())
        timeouts = [hook["timeout"] for group in claude["hooks"]["PreToolUse"] if group["matcher"] == "AskUserQuestion"
                    for hook in group["hooks"]]
        timeouts += [hook["timeout"] for group in codex["hooks"]["PreToolUse"] if "request_user_input" in group["matcher"]
                     for hook in group["hooks"]]
        self.assertEqual(len(timeouts), 2)
        for timeout in timeouts:
            self.assertLess(redirect.BUDGET_SECONDS, timeout)
        plugin = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text()
        self.assertLess(redirect.BUDGET_SECONDS * 1000, int(re.search(r"nativeQuestionRedirect, \"--opencode\"\], \{.*?timeout: (\d+)", plugin, re.S).group(1)))

    def test_codex_entry_script_registers_in_a_private_state_and_a_worker_gets_nothing(self):
        import fcntl
        script = ROOT / "adapters/codex/hooks/pretooluse-question-redirect.py"
        root = state.state_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Holding the board producer's lock keeps this registration from starting a producer process.
        lock = os.open(root / ".projection.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        env = {"HOME": os.environ["XDG_STATE_HOME"], "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
               "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]}
        outcome = {}
        for name, extra in (("main", {}), ("worker", {"AGENT_SESSION_ROLE": "worker"})):
            done = subprocess.run([sys.executable, str(script)], input=json.dumps(codex_payload()),
                                  env={**env, **extra}, text=True, capture_output=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            outcome[name] = done.stdout
        self.assertEqual(json.loads(outcome["main"])["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(outcome["worker"], "")


class ClaudeHookTest(Base):
    def run_hook(self, payload, reason):
        import importlib.util
        spec = importlib.util.spec_from_file_location("native_question_redirect_hook", ROOT / "hooks/native-question-redirect.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        out = io.StringIO()
        with mock.patch.object(redirect, "redirect", return_value=reason), \
             mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            code = hook.main()
        return code, out.getvalue()

    def test_a_registered_question_is_refused_without_a_decision_wait(self):
        from fleet import interaction
        code, out = self.run_hook(claude_payload(), "Registered as Hearting question S4:x. End your turn now.")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(interaction.read_wait(SID, "claude"))

    def test_a_question_that_goes_through_keeps_its_decision_wait_and_is_not_refused(self):
        from fleet import interaction
        code, out = self.run_hook(claude_payload(), None)
        self.assertEqual((code, out), (0, ""))
        self.assertIsNotNone(interaction.read_wait(SID, "claude"))

    def test_a_native_subagent_question_sets_nothing(self):
        from fleet import interaction
        self.run_hook(claude_payload(agent_id="sub-1"), None)
        self.assertIsNone(interaction.read_wait(SID, "claude"))


class WiringAndRulesTest(unittest.TestCase):
    def test_claude_runs_one_pretooluse_hook_for_the_question_tool_and_no_frame_bridge(self):
        settings = json.loads((ROOT / "adapters/claude/settings.json").read_text())
        pre = [hook["command"] for group in settings["hooks"]["PreToolUse"] if group["matcher"] == "AskUserQuestion"
               for hook in group["hooks"]]
        self.assertEqual(len(pre), 1)
        self.assertIn("native-question-redirect.py", pre[0])
        self.assertNotIn("set --kind decision", pre[0])
        post = json.dumps(settings["hooks"]["PostToolUse"])
        self.assertNotIn("frame-native-answer", post)
        self.assertNotIn("AskUserQuestion", post)
        self.assertTrue((ROOT / "hooks/native-question-redirect.py").is_file())
        self.assertTrue((ROOT / "adapters/claude/hooks/native-question-redirect.py").exists())

    def test_codex_registers_before_both_question_tools_and_has_no_frame_bridge(self):
        hooks = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text())["hooks"]
        groups = [g for g in hooks["PreToolUse"] if "request_user_input" in g["matcher"]]
        self.assertEqual(len(groups), 1)
        for tool in ("request_user_input", "request_user_input_async", "functions.request_user_input",
                     "functions.request_user_input_async"):             # both name shapes, as the other matchers
            self.assertTrue(re.search(groups[0]["matcher"], tool), tool)
        for shape in (r"functions\.request_user_input", r"functions\.request_user_input_async"):
            self.assertIn(shape, groups[0]["matcher"])
        self.assertIn("pretooluse-question-redirect.py", groups[0]["hooks"][0]["command"])
        self.assertIn("pretooluse-question-redirect.py", (ROOT / "adapters/codex/hooks/run-hook.sh").read_text())
        # The retired bridge keeps its hooks.json place (entries keep their order; Codex trusts each by its current hash) as a no-op.
        retired = (ROOT / "adapters/codex/hooks/posttooluse-frame-answer.py").read_text()
        self.assertNotIn("frame_native_answer", retired)
        self.assertEqual(subprocess.run([sys.executable, str(ROOT / "adapters/codex/hooks/posttooluse-frame-answer.py")],
                                        input="{}", text=True, capture_output=True).stdout, "")

    def test_opencode_plugin_redirects_before_the_question_tool_and_keeps_no_frame_recorder(self):
        plugin = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text()
        before = plugin[plugin.index('"tool.execute.before": async'):plugin.index('"tool.execute.after": async')]
        self.assertIn('toolName === "question"', before)
        self.assertIn("nativeQuestionRedirect", before)
        self.assertIn("throw new Error", before[before.index("nativeQuestionRedirect"):])
        self.assertNotIn("frame_native_answer", plugin)
        for gone in ("utilities/frame_native_answer.py", "hooks/frame-native-answer.py"):
            self.assertFalse((ROOT / gone).exists(), gone)

    def test_no_rule_sentence_tells_an_agent_to_use_a_native_question_tool(self):
        rule_files = ["core/WORKFLOW.md", "roles/response-policy.md", "adapters/claude/CLAUDE.md",
                      "adapters/codex/AGENTS.md", "adapters/opencode/AGENTS.md",
                      "adapters/claude/plugin-marketplace/plugins/hearting-claude/core/WORKFLOW.md",
                      "adapters/claude/plugin-marketplace/plugins/hearting-claude/roles/response-policy.md",
                      "adapters/codex/plugins/hearting-codex/core/WORKFLOW.md",
                      "adapters/codex/plugins/hearting-codex/roles/response-policy.md"]
        names = re.compile(r"AskUserQuestion|request_user_input|OpenCode `question`|native question|native structured-question")
        for name in rule_files:
            with self.subTest(file=name):
                self.assertIsNone(names.search((ROOT / name).read_text(encoding="utf-8")), name)
        workflow = (ROOT / "core/WORKFLOW.md").read_text(encoding="utf-8")
        self.assertIn("hearting run questions ask", workflow)
        self.assertIn("진행 / 수정 /\n중단", workflow)                         # the card keeps its three choices
        for name in ("adapters/claude/CLAUDE.md", "adapters/codex/AGENTS.md", "adapters/opencode/AGENTS.md"):
            self.assertIn("진행(권장) /\n  수정 / 중단", (ROOT / name).read_text(encoding="utf-8"), name)


if __name__ == "__main__":
    unittest.main()
