#!/usr/bin/env python3
"""A native question call has three outcomes, the same on all three harnesses (11-28, piece 3b).

Registered (new or the same open question, whatever else the session waits on), pass (registration
failed before any write and no same question was read: the native question opens with its private
reference on the session's native wait marker, so it is counted and the same call again is told it
is there), unknown (nothing opens; the refusal says so once and the same call again settles it).
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))
import human_wait_state as state  # noqa: E402
import native_question_redirect as redirect  # noqa: E402
import questions  # noqa: E402
from fleet import interaction  # noqa: E402

SID = "native-intercept-session"
OPTIONS = [("Proceed (Recommended)", "Start the work"), ("Edit", "Change the plan"), ("Stop", "Do not start")]
HARNESSES = ("claude", "codex", "opencode")
ID = re.compile(r"S4:[0-9a-f]{64}")


def asked(questions_):
    """The three harness payloads of one native call carrying `questions_`."""
    return {"claude": {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                       "tool_input": {"questions": questions_}},
            "codex": {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "request_user_input",
                      "tool_input": {"questions": questions_}},
            "opencode": {"tool": "question", "sessionID": SID, "args": {"questions": questions_}}}


def start_question(**extra):
    return {"question": "Start now?", "header": "Start", "multiSelect": False,
            "options": [{"label": label, "description": text} for label, text in OPTIONS], **extra}


def payloads(**extra):
    return asked([start_question(**extra)])


class Base(unittest.TestCase):
    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        import session_tidy as tidy
        from fleet import process_identity
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.tidy = tidy
        for patch in (mock.patch.object(tidy, "resolve_seat",
                                        side_effect=lambda harness, *a, **k: self.f.seat(harness, a[2] if len(a) > 2 else SID)),
                      mock.patch.object(process_identity, "current_launch_context", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def open_ids(self, harness):
        rows, health = state.message_questions(harness, SID)
        self.assertEqual(health, "ok")
        return sorted(row["id"] for row in rows.values() if row["lifecycle"] == "open")

    def done(self, harness):
        """Every open question of the session closed, its native wait cleared and a new prompt (the
        same registration at the same prompt would read back its closed record): the next case stands alone."""
        for item_id in self.open_ids(harness):
            state.close_message(harness, SID, self.f.seat(harness, SID).key, item_id)
        interaction.clear_wait(SID, harness)
        self.next_prompt(harness)

    def next_prompt(self, harness):
        self.tidy.bump_prompt_seq(self.f.seat(harness, SID), harness, SID, time.time())

    def the_id(self, outcome):
        self.assertEqual(outcome.kind, "registered", outcome)
        (found,) = ID.findall(outcome.reason)
        return found


class RegisteredBesideOtherWaitsTest(Base):
    def test_another_question_a_native_wait_and_a_body_marker_never_send_the_call_native(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                earlier, envelope = self.f.ask(harness, SID, text="Which scope first?", choices=["Small", "Large"])
                state.write_selected_draft(envelope, "kept draft")
                self.assertTrue(interaction.set_wait(SID, harness, "permission",
                                                     {"claude": "claude-permission", "codex": "codex-permissionrequest",
                                                      "opencode": "opencode-permission"}[harness]))
                outcome = redirect.decide(harness, payloads()[harness])
                added = self.the_id(outcome)
                self.assertNotEqual(added, earlier["id"])
                self.assertEqual(self.open_ids(harness), sorted([earlier["id"], added]))
                self.assertEqual(state._selected_entry(envelope)["draft"], "kept draft")
                self.assertEqual(interaction.read_wait(SID, harness)["kind"], "permission")  # left as it was
                self.assertNotIn("call_ref", interaction.read_wait(SID, harness))
                # the same call again, at this prompt and the next, is that question
                self.assertEqual(self.the_id(redirect.decide(harness, payloads()[harness])), added)
                self.next_prompt(harness)
                self.assertEqual(self.the_id(redirect.decide(harness, payloads()[harness])), added)
                self.done(harness)

    def test_the_line_names_the_id_replaces_ends_the_turn_and_says_the_answer_carries_it(self):
        reason = redirect.decide("claude", payloads()["claude"]).reason
        self.assertLess(len(reason), 220)
        self.assertNotIn("\n", reason)
        for part in ("replaces this native question", "End your turn now", "marked with this id"):
            self.assertIn(part, reason)
        for word in ("hearting run", "--", "command"):
            self.assertNotIn(word, reason)


class SameQuestionTest(Base):
    def test_a_question_asked_on_the_cli_is_the_native_one_at_this_prompt_and_the_next(self):
        for harness in HARNESSES:
            for when in ("this prompt", "next prompt"):
                with self.subTest(harness=harness, when=when):
                    cli, _ = self.f.ask(harness, SID, text="Start now?", choices=["Proceed", "Edit", "Stop"],
                                        recommended=0)
                    if when == "next prompt":
                        self.next_prompt(harness)
                    self.assertEqual(self.the_id(redirect.decide(harness, payloads()[harness])), cli["id"])
                    self.assertEqual(self.open_ids(harness), [cli["id"]])
                    self.done(harness)

    def test_a_native_question_asked_again_on_the_cli_is_that_question(self):
        """The descriptions travel in their own field (#1908), so the native record's text is the
        question itself and the CLI's own same-question check finds it."""
        for harness in HARNESSES:
            for when in ("this prompt", "next prompt"):
                with self.subTest(harness=harness, when=when):
                    native = self.the_id(redirect.decide(harness, payloads()[harness]))
                    if when == "next prompt":
                        self.next_prompt(harness)
                    cli, _ = self.f.ask(harness, SID, text="Start now?", choices=["Proceed", "Edit", "Stop"])
                    self.assertEqual(cli["id"], native)
                    self.assertEqual(self.open_ids(harness), [native])
                    self.done(harness)

    def test_an_earlier_native_record_with_its_descriptions_in_the_text_is_the_same(self):
        # what the builder wrote before #1908 moved the descriptions to `choice_descriptions`
        earlier = "Start now?\n- Proceed: Start the work\n- Edit: Change the plan\n- Stop: Do not start"
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                old, _ = self.f.ask(harness, SID, text=earlier, choices=["Proceed", "Edit", "Stop"])
                self.next_prompt(harness)
                self.assertEqual(self.the_id(redirect.decide(harness, payloads()[harness])), old["id"])
                self.done(harness)
                # only that exact text: one more line of the person's own is another question
                other, _ = self.f.ask(harness, SID, text=earlier + "\n- Note: ask me first",
                                      choices=["Proceed", "Edit", "Stop"])
                self.assertNotEqual(self.the_id(redirect.decide(harness, payloads()[harness])), other["id"])
                self.done(harness)

    def test_a_native_question_keeps_its_choice_descriptions_by_their_choices(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                found = self.the_id(redirect.decide(harness, payloads()[harness]))
                envelope = state.read_selected_request({"id": found, "harness": harness, "session_id": SID})
                (question,) = questions.questions_of(envelope)
                self.assertEqual(question["question"], "Start now?")
                self.assertEqual(question["descriptions"], ["Start the work", "Change the plan", "Do not start"])
                self.done(harness)

    def test_other_choice_values_order_or_secrecy_is_another_question(self):
        reordered = asked([{**start_question(), "options": list(reversed(start_question()["options"]))}])
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                first = self.the_id(redirect.decide(harness, payloads()[harness]))
                for other in (reordered[harness], payloads(isSecret=True)[harness]):
                    self.assertNotEqual(self.the_id(redirect.decide(harness, other)), first)
                self.assertEqual(len(self.open_ids(harness)), 3)
                self.done(harness)


class SecretTest(Base):
    def test_one_secret_question_is_registered_as_a_secret_question(self):
        secret = asked([{"question": "API token?", "header": "Token", "isSecret": True, "options": []}])
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                found = self.the_id(redirect.decide(harness, secret[harness]))
                envelope = state.read_selected_request({"id": found, "harness": harness, "session_id": SID})
                (question,) = questions.questions_of(envelope)
                self.assertTrue(question["secret"])
                self.assertIsNone(interaction.read_wait(SID, harness))
                self.done(harness)

    def test_several_questions_with_a_secret_among_them_pass_and_write_nothing(self):
        mixed = asked([start_question(), {"question": "API token?", "isSecret": True, "options": []}])
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                self.assertEqual(redirect.decide(harness, mixed[harness]).kind, "pass")
                self.assertEqual(self.open_ids(harness), [])
                self.assertTrue(interaction.read_wait(SID, harness)["call_ref"].startswith("nq-"))
                self.done(harness)


class PassTest(Base):
    """Only a failure before any write, with no same question read, opens the native question."""

    def failures(self):
        import session_tidy as tidy

        def slow(*args, **kwargs):
            time.sleep(10)
        yield "no seat", mock.patch.object(tidy, "resolve_seat", return_value=tidy.Seat("none", "", pane=""))
        yield "refused write", mock.patch.object(state, "register_message",
                                                 side_effect=ValueError("question-prompt-changed"))
        yield "failed write", mock.patch.object(state, "_write", side_effect=OSError("disk full"))
        yield "budget", mock.patch.object(state, "register_message", side_effect=slow)
        yield "shape", None

    def test_each_failure_before_a_write_opens_the_native_question_marked_and_counted(self):
        shapeless = asked([{"question": "Pick", "options": [{"label": " "}]}])
        for harness in HARNESSES:
            for name, patch in self.failures():
                with self.subTest(harness=harness, failure=name):
                    payload = (shapeless if patch is None else payloads())[harness]
                    with patch or contextlib.nullcontext():
                        outcome = redirect.decide(harness, payload, seconds=1)
                    self.assertEqual(outcome, redirect.Outcome("pass"))
                    self.assertEqual(self.open_ids(harness), [])
                    wait = interaction.read_wait(SID, harness)
                    self.assertEqual((wait["kind"], wait["source"]),
                                     ("decision", redirect._WAIT_SOURCE[harness]))
                    self.assertEqual(wait["call_ref"], redirect._call_ref(harness, SID, payload))
                    for text in ("Start now", "Proceed", "Pick"):                # no text on the marker
                        self.assertNotIn(text, json.dumps(wait))
                    # the same call again is told the native question is open there; nothing is added
                    again = redirect.decide(harness, payload)
                    self.assertEqual(again.kind, "native")
                    self.assertIn("already open as a native question", again.reason)
                    self.assertEqual(self.open_ids(harness), [])
                    # the native question's own end ends the reference: the call registers again
                    interaction.clear_wait(SID, harness, reason="answered")
                    if patch is not None:
                        self.the_id(redirect.decide(harness, payload))
                    self.done(harness)

    def test_a_question_closed_and_asked_again_at_the_same_prompt_opens_natively(self):
        """#1927 returns this very registration's ended record at the same prompt; it is not read as
        registered (no answer would come), so the native question opens, marked; the next prompt
        registers it anew."""
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                first = self.the_id(redirect.decide(harness, payloads()[harness]))
                state.close_message(harness, SID, self.f.seat(harness, SID).key, first)
                self.assertEqual(redirect.decide(harness, payloads()[harness]), redirect.Outcome("pass"))
                self.assertTrue(interaction.read_wait(SID, harness)["call_ref"])
                interaction.clear_wait(SID, harness, reason="answered")
                self.next_prompt(harness)
                self.assertNotEqual(self.the_id(redirect.decide(harness, payloads()[harness])), first)
                self.done(harness)

    def test_a_passed_codex_question_is_on_the_shared_wait_list_until_its_tool_ends(self):
        """Backlog 11-14 (RV-PR1633 follow-up 7): before this, nothing marked a Codex question the
        redirect let through, so only a readable live rollout could show it."""
        import human_gate_observation as observe
        with mock.patch.object(observe, "_codex_rollout_waits", return_value={"complete": True, "pending": {}}), \
             mock.patch.object(observe, "_later_session_turn", return_value=False):
            def listed():
                result = observe.read_answer_waits(jobs_paths=[self.f.jobs], now=time.time() + 1)
                mine = [item for item in result["items"] if item["source"] == "S1"
                        and item["owner"] == {"harness": "codex", "session_id": SID}]
                return mine, state.wait_counts(result["items"])["total"]
            self.assertEqual(listed(), ([], 0))
            with mock.patch.object(state, "_write", side_effect=OSError("disk full")):
                self.assertEqual(redirect.decide("codex", payloads()["codex"]).kind, "pass")
            mine, total = listed()
            self.assertEqual((len(mine), total), (1, 1))
            self.assertNotIn("call_ref", mine[0])                                # the reference stays private
            hook = ROOT / "adapters/codex/hooks/posttooluse-interaction-clear.py"
            done = subprocess.run([sys.executable, str(hook)], input=json.dumps({"session_id": SID}),
                                  env={**os.environ, "AGENT_SESSION_ROLE": ""}, text=True,
                                  capture_output=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(listed(), ([], 0))

    def test_the_native_question_publishing_its_request_keeps_the_reference_and_time(self):
        """OpenCode's `question.asked` (and Claude's decision marker) publish the wait the redirect
        marked before the question opened: the reference and the start time stay, no replace."""
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                with mock.patch.object(state, "_write", side_effect=OSError("disk full")):
                    self.assertEqual(redirect.decide(harness, payloads()[harness]).kind, "pass")
                marked = interaction.read_wait(SID, harness)
                with mock.patch.object(state, "record_closed_question") as closed:
                    self.assertTrue(interaction.set_wait(SID, harness, "decision", marked["source"],
                                                         request_id="que_1" if harness == "opencode" else None))
                    closed.assert_not_called()
                published = interaction.read_wait(SID, harness)
                self.assertEqual((published["call_ref"], published["waiting_since"]),
                                 (marked["call_ref"], marked["waiting_since"]))
                self.assertEqual(redirect.decide(harness, payloads()[harness]).kind, "native")
                # a permission is another wait and takes no reference
                interaction.set_wait(SID, harness, "permission", "opencode-permission" if harness == "opencode"
                                     else "claude-permission" if harness == "claude" else "codex-permissionrequest")
                self.assertNotIn("call_ref", interaction.read_wait(SID, harness))
                self.done(harness)


class UnknownTest(Base):
    """Nothing opens; the same call again reads as registered (the same id) or pass."""

    def test_unreadable_storage_refuses_once_and_the_retry_registers(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                path = state._questions_path(harness, SID)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_text("{broken")
                path.chmod(0o600)
                outcome = redirect.decide(harness, payloads()[harness])
                self.assertEqual(outcome.kind, "unknown")
                self.assertIn("question storage cannot be read", outcome.reason)
                for step in ("Make the same call again", "hearting run questions ask"):
                    self.assertIn(step, outcome.reason)
                self.assertIsNone(interaction.read_wait(SID, harness))            # no native question
                path.unlink()
                self.the_id(redirect.decide(harness, payloads()[harness]))
                self.done(harness)

    def test_a_write_whose_result_was_lost_reads_back_as_the_same_id(self):
        real = state.register_message

        def lost(*args, **kwargs):
            real(*args, **kwargs)
            raise TimeoutError("result lost after the write")
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                with mock.patch.object(state, "register_message", side_effect=lost):
                    first = self.the_id(redirect.decide(harness, payloads()[harness]))
                self.assertEqual(self.open_ids(harness), [first])
                self.assertEqual(self.the_id(redirect.decide(harness, payloads()[harness])), first)
                self.assertEqual(self.open_ids(harness), [first])
                self.assertIsNone(interaction.read_wait(SID, harness))
                self.done(harness)

    def test_a_native_wait_that_cannot_be_marked_or_a_check_that_runs_late_opens_nothing(self):
        def stuck(*args, **kwargs):
            time.sleep(5)
        for harness in HARNESSES:
            for name, patch, why in (
                    ("marker unwritable", mock.patch.object(interaction, "set_wait", return_value=False),
                     "native wait cannot be marked"),
                    ("read-back late", mock.patch.object(state, "message_questions", side_effect=stuck),
                     "check did not finish")):
                with self.subTest(harness=harness, case=name):
                    started = time.monotonic()
                    with mock.patch.object(state, "_write", side_effect=OSError("disk full")), patch:
                        outcome = redirect.decide(harness, payloads()[harness], seconds=1, settle=0.5)
                    self.assertLess(time.monotonic() - started, 4)
                    self.assertEqual(outcome.kind, "unknown")
                    self.assertIn(why, outcome.reason)
                    self.assertEqual(self.open_ids(harness), [])
                    self.done(harness)


class HookTest(Base):
    def claude(self, payload, **patches):
        spec = importlib.util.spec_from_file_location("native_question_redirect_hook_1128",
                                                      ROOT / "hooks/native-question-redirect.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for target, value in patches.items():
                stack.enter_context(mock.patch.object(state, target, **value))
            stack.enter_context(mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))))
            stack.enter_context(contextlib.redirect_stdout(out))
            self.assertEqual(hook.main(), 0)
        return json.loads(out.getvalue())["hookSpecificOutput"] if out.getvalue() else None

    def test_claude_refuses_registered_native_and_unknown_and_passes_only_the_pass(self):
        payload = payloads()["claude"]
        self.assertRegex(self.claude(payload)["permissionDecisionReason"], ID)
        self.done("claude")
        self.assertIsNone(self.claude(payload, _write={"side_effect": OSError("disk full")}))
        wait = interaction.read_wait(SID, "claude")                           # the decision marker, with the reference
        self.assertEqual((wait["source"], wait["call_ref"]), ("claude-asktool", redirect._call_ref("claude", SID, payload)))
        self.assertIn("already open as a native question", self.claude(payload)["permissionDecisionReason"])
        self.done("claude")
        refused = self.claude(payload, message_questions={"return_value": ({}, "unknown")})
        self.assertEqual(refused["permissionDecision"], "deny")
        self.assertIn("could not confirm", refused["permissionDecisionReason"])
        self.assertIsNone(interaction.read_wait(SID, "claude"))

    def test_codex_entry_prints_the_refusal_for_unknown_and_nothing_for_a_pass(self):
        out = io.StringIO()
        with mock.patch.object(state, "message_questions", return_value=({}, "unknown")), \
             contextlib.redirect_stdout(out):
            self.assertEqual(redirect.main(["--codex"], stdin=io.StringIO(json.dumps(payloads()["codex"]))), 0)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(state, "_write", side_effect=OSError("disk full")), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(redirect.main(["--codex"], stdin=io.StringIO(json.dumps(payloads()["codex"]))), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))          # only OpenCode reads the stderr line
        out, err = io.StringIO(), io.StringIO()
        interaction.clear_wait(SID, "opencode")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(redirect.main(["--opencode"], stdin=io.StringIO(json.dumps(payloads()["opencode"]))), 1)
        self.assertRegex(out.getvalue(), ID)
        self.assertEqual(err.getvalue().strip(), redirect.REGISTERING)

    def test_a_registration_stuck_before_its_write_ends_inside_the_hook_timeout_as_a_pass(self):
        """The real Codex entry and Claude hook processes, with the question store's write lock held
        elsewhere: the 5 s budget gives up, the read-back finds nothing, the native wait is marked,
        and the process ends well inside the harness's 10 s hook timeout (no second window)."""
        import fcntl
        root = state.state_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in (".projection.lock", ".cache-write.lock"):
            lock = os.open(root / name, os.O_CREAT | os.O_RDWR, 0o600)
            self.addCleanup(os.close, lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
        env = {"HOME": os.environ["XDG_STATE_HOME"], "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
               "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]}
        for harness, script in (("codex", ROOT / "adapters/codex/hooks/pretooluse-question-redirect.py"),
                                ("claude", ROOT / "hooks/native-question-redirect.py")):
            with self.subTest(harness=harness):
                started = time.monotonic()
                done = subprocess.run([sys.executable, str(script)], input=json.dumps(payloads()[harness]),
                                      env=env, text=True, capture_output=True, timeout=30)
                elapsed = time.monotonic() - started
                self.assertEqual((done.returncode, done.stdout), (0, ""), done.stderr)
                self.assertGreater(elapsed, redirect.BUDGET_SECONDS - 0.5)
                self.assertLess(elapsed, 9)
                wait = interaction.read_wait(SID, harness)
                self.assertEqual((wait["source"], wait["call_ref"]),
                                 (redirect._WAIT_SOURCE[harness], redirect._call_ref(harness, SID, payloads()[harness])))
                self.assertEqual(state.message_questions(harness, SID), ({}, "ok"))

    def test_opencode_reads_three_outcomes_and_a_check_that_died_after_it_began_is_refused(self):
        node = self.f.node                                                   # found before the fixture hid PATH
        if not node:
            self.skipTest("node is unavailable for the plugin module contract")
        with tempfile.TemporaryDirectory(prefix="ni1128-") as tmp:
            home, project, harness = Path(tmp) / "home", Path(tmp) / "project", Path(tmp) / "harness"
            for directory in (home, project, harness / "core", harness / "utilities",
                              harness / "adapters" / "opencode" / "bin"):
                directory.mkdir(parents=True)
            (harness / "core" / "CORE.md").write_text("fixture\n")
            (harness / "adapters" / "opencode" / "bin" / "preflight.sh").write_text("exit 0\n")
            (harness / "utilities" / "native_question_redirect.py").write_text(
                "import os, signal, sys\n"
                "sys.stdin.read()\n"
                "mode = os.environ['REDIRECT_MODE']\n"
                "if mode == 'refuse':\n"
                "    print('Registered as Hearting question S4:fixture'); raise SystemExit(1)\n"
                "if mode == 'pass':\n"
                "    raise SystemExit(0)\n"
                "if mode == 'dies-after':\n"
                "    print(%r, file=sys.stderr, flush=True)\n"
                "os.kill(os.getpid(), signal.SIGKILL)\n" % redirect.REGISTERING)
            script = r"""
import { pathToFileURL } from "node:url"
const plugin = (await import(pathToFileURL(process.env.PLUGIN_PATH))).default
const hooks = new Map()
const ctx = {
  location: { directory: process.env.TEMP_ROOT, project: { id: "fixture" } },
  app: { log: async () => {} },
  session: {
    get: async ({ sessionID }) => ({ data: { id: sessionID, agent: "build", title: "Main", parentID: null } }),
    prompt: async () => ({ data: null }), context: async () => ({ data: [] }),
    hook: async (name, fn) => hooks.set("session." + name, fn),
  },
  tool: { hook: async (name, fn) => hooks.set("tool." + name, fn) },
  shell: { hook: async (name, fn) => hooks.set("shell." + name, fn) },
  event: { subscribe: async function* ({ signal }) {
    await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }))
  } },
  permission: { hook: async (name, fn) => hooks.set("permission." + name, fn) },
}
const cleanup = await plugin.setup(ctx)
try {
  let refused = ""
  try { await hooks.get("tool.execute.before")({ tool: "question", sessionID: "ses_main",
    input: { questions: [{ question: "Which?", options: [{ label: "A" }, { label: "B" }] }] } }) }
  catch (error) { refused = String(error.message) }
  console.log(JSON.stringify({ refused }))
} finally { await cleanup() }
"""
            base = {key: value for key, value in os.environ.items()
                    if key not in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                                   "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH")}
            base.update({"PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                         "TEMP_ROOT": str(project), "HOME": str(home), "AGENT_HOME": str(harness),
                         "PYTHONDONTWRITEBYTECODE": "1", "XDG_STATE_HOME": str(home / ".state"),
                         "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".data"),
                         "XDG_CACHE_HOME": str(home / ".cache"),
                         "PATH": os.pathsep.join([os.path.dirname(node), os.path.dirname(sys.executable),
                                                  "/usr/bin", "/bin"])})
            seen = {}
            for mode in ("refuse", "pass", "dies-before", "dies-after"):
                result = subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT,
                                        env={**base, "REDIRECT_MODE": mode}, text=True,
                                        capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                seen[mode] = json.loads(result.stdout.strip().splitlines()[-1])["refused"]
        self.assertEqual(seen["refuse"], "Registered as Hearting question S4:fixture")
        self.assertEqual((seen["pass"], seen["dies-before"]), ("", ""))
        self.assertIn("could not confirm whether this question was registered", seen["dies-after"])


if __name__ == "__main__":
    unittest.main()
