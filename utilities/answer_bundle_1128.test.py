"""11-27 ("답변을 받는것도 보니까 사실상 하나씩만 잘 동작하는 구조인것 같더만"): the answers one session's questions got
reach it together, in the order the person gave them, and one refused answer does not hold the others (design 3c).

The courier already sends a recipient's ready notices as one body. A person's answers in it now stand in the order
they were given (each answer's delivery record is created as it is submitted: its ``created_at_ns``), both where the
courier picks its batch and where the text is written, on the window path and the hook path. Every other notice keeps
its place. An answer the transport would refuse before writing (a Codex panel answer whose window changed) leaves the
bundle alone: it is refused, its question stands again, and the others go in that pass. A bundle larger than one
transfer sends its front and keeps the rest, in order, for the next pass. An answer that was handed over (queued, its
receipt pending) is never sent again; the next answers wait for it as before.

The questions are registered, answered, carried and received through the real code in a private state root
(fixtures/message_questions.py); only native census, RPC and I/O are fixtures. No window, process or model is used.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import hashlib
import re
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import dispatch_pending_delivery as pending  # noqa: E402
import dispatch_session_sweep as sweep  # noqa: E402
import human_gate_observation as observation  # noqa: E402
import human_wait_state as waits  # noqa: E402
import peer_obligations as obligations  # noqa: E402
import session_notice  # noqa: E402
import session_shell  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")
SID = "bundle-session"
REQUEST = re.compile(r"Question answer \(request=([^)]+)\)")


def key_id(signal):
    """The question's own delivery id (`session_notice.notify`), known before it is answered."""
    return "delivery-" + hashlib.sha256(("notice\0question-answer:" + signal["id"]).encode()).hexdigest()


class AnswerBundleTest(unittest.TestCase):
    @contextlib.contextmanager
    def fresh(self, shell=True, **subtest):
        """One case: its own private state root; the host's Codex rollouts are not this case's."""
        from fixtures.message_questions import MessageQuestionFixture
        with self.subTest(**subtest), MessageQuestionFixture() as self.f, mock.patch.object(
                observation, "_codex_rollout_waits", return_value={"complete": True, "pending": {}}):
            self.f.hold_projection_lock()
            self.f.shell = shell
            yield

    def ask(self, harness, labels):
        self.signals = {label: self.f.ask(harness, SID, text="Question %s?" % label,
                                          choices=["Dogwood", "Field Guide"])[0] for label in labels}
        return self.signals

    def answer(self, label, values=None, action=None, **kwargs):
        signal = self.signals[label]
        envelope = waits.read_selected_request({"id": signal["id"], "harness": signal["harness"],
                                                "session_id": signal["session_id"]})
        result = waits.submit_selected_response(
            envelope, {"answers": {"message-response": values or ["answer-" + label]}},
            action_id=action or "action-" + label, **kwargs)
        self.assertEqual(result["state"], "submitted", result.get("result"))
        return result

    def against_key_order(self, labels):
        """An answer order opposite to the questions' delivery ids (the order the courier used before)."""
        return sorted(labels, key=lambda label: key_id(self.signals[label]), reverse=True)

    def carry(self):
        """One courier pass: (bodies rendered, what reached the window, the transfer row)."""
        real = type(self.f).record.__get__(self.f)
        store = obligations.ObligationStore(self.f.root)

        def record(signal):   # the fixture reports on one duty after the pass: any one still listed
            live = {item["intent"]["delivery_id"] for item in store.list(kinds={"run-notice"})}
            return next((found for found in map(real, self.signals.values())
                         if found and found["delivery_id"] in live), None) or real(signal)

        self.f.record = record
        try:
            calls, _ref, row = self.f.courier(next(iter(self.signals.values())))
        except StopIteration:   # no duty was left to report on
            self.assertEqual(self.f.bodies, [], "a pass with no duty sends nothing")
            return [], [], None
        finally:
            del self.f.record
        return list(self.f.bodies), calls, row

    def order(self, text):
        labels = {signal["id"]: label for label, signal in self.signals.items()}
        return "".join(labels.get(found, "?") for found in REQUEST.findall(text or ""))

    def consume(self, calls, row):
        self.f.consume(next(iter(self.signals.values())), (row or {}).get("text") or calls[-1])

    def states(self):
        """label -> delivery record state / answer record state / question lifecycle."""
        out = {}
        for label, signal in self.signals.items():
            record = self.f.record(signal) or {}
            entry = waits._selected_entry({"harness": signal["harness"], "session_id": SID,
                                           "request_id": signal["id"], "question_id": "message-response",
                                           "turn_id": None}) or {}
            source = waits._read(waits._signal_path(signal)) or {}
            out[label] = "%s/%s/%s" % (record.get("state"), entry.get("state"), source.get("lifecycle"))
        return out

    def completion(self, label):
        return (waits._read(waits._signal_path(self.signals[label])) or {}).get("completion") or {}

    @contextlib.contextmanager
    def later(self, seconds):
        """Past every 30-second claim lease: the next courier pass of a real runner."""
        real = time.monotonic_ns
        with mock.patch("time.monotonic_ns", lambda: real() + int(seconds * 1e9)):
            yield

    # -- rule 1: the order the person answered in -----------------------------------------------------

    def test_answers_reach_the_session_in_the_order_they_were_given(self):
        for harness in HARNESSES:
            for case in ("against-key-order", "c-a-b"):
                with self.fresh(harness=harness, case=case):
                    self.ask(harness, "ABC")
                    given = self.against_key_order("ABC") if case == "against-key-order" else list("CAB")
                    for label in given:
                        self.answer(label)
                    bodies, calls, row = self.carry()
                    self.assertEqual((len(bodies), len(calls)), (1, 1), "one prompt holds all three")
                    self.assertEqual(self.order(bodies[0]), "".join(given))
                    self.consume(calls, row)
                    self.assertEqual(set(self.states().values()), {"acked/applied/answered"})
                    self.assertEqual(self.carry()[1], [], "nothing is sent again")

    def test_a_session_without_a_shell_takes_them_in_the_same_order_at_its_next_hook(self):
        for harness in ("claude", "opencode"):
            with self.fresh(shell=False, harness=harness):
                self.ask(harness, "ABC")
                given = self.against_key_order("ABC")
                for label in given:
                    self.answer(label)
                bodies, calls, row = self.carry()
                self.assertEqual((len(bodies), calls, row["state"]), (1, [], "pending"))
                if harness == "claude":
                    context = self.f.claude_boundary(SID, mode="post-tool")
                else:
                    context = "\n".join(self.f.opencode_tool_boundary(SID))
                self.assertEqual(self.order(context), "".join(given))
                self.assertEqual(set(self.states().values()), {"acked/applied/answered"})

    def test_answers_keep_their_order_and_other_notices_their_places(self):
        # OpenCode's run notices go by its own carrier, so the courier's mixed bundle is Claude's and Codex's.
        for harness in ("claude", "codex"):
            with self.fresh(harness=harness):
                self.ask(harness, "AB")
                for key, subject, text, action in (
                        ("work-done:fixture-1", "Work finished", "fixture work finished", "report-to-user"),
                        ("experiment:run7:attention:1", "Run attention", "run7 loss rose", "read-notice"),
                        ("progress:fixture-2", "Progress", "fixture progress", "read-notice")):
                    session_notice.notify(harness, SID, key=key, subject=subject, text=text,
                                          required_action=action)
                given = self.against_key_order("AB")
                for label in given:
                    self.answer(label)
                bodies, calls, _row = self.carry()
                self.assertEqual(len(calls), 1)
                lines = [line.split(" (")[0] for line in bodies[0].splitlines()
                         if line.startswith(("- ", "Needs attention", "Other notices"))]
                labels = iter(given)
                self.assertEqual(lines, ["Needs attention", "- Run attention: run7 loss rose", "Other notices:",
                                         "- Progress: fixture progress", "- Work finished: fixture work finished",
                                         "- Question answer", "- Question answer"])
                self.assertEqual(self.order(bodies[0]), "".join(labels))

    def test_the_count_and_size_limits_send_the_front_and_keep_the_rest_in_order(self):
        long = lambda label: "\n".join(["%s안: `utilities/peer-steward.py`의 묶음은 답한 차례로 갑니다." % label] * 50)
        for harness in HARNESSES:
            with self.fresh(harness=harness, limit="count"):
                self.ask(harness, "ABCDEFGHIJ")
                given = self.against_key_order("ABCDEFGHIJ")
                for label in given:
                    self.answer(label)
                carried = []
                for _ in range(2):
                    bodies, calls, row = self.carry()
                    carried.append(self.order(bodies[0]))
                    self.consume(calls, row)
                self.assertEqual(carried, ["".join(given[:8]), "".join(given[8:])])
                self.assertEqual(set(self.states().values()), {"acked/applied/answered"})
            with self.fresh(harness=harness, limit="size"):
                self.ask(harness, "ABCDE")
                for label in "ABCDE":
                    self.answer(label, [long(label)])
                self.assertGreater(len(long("A").encode()), 3500)
                carried = []
                for _ in range(3):
                    bodies, calls, row = self.carry()
                    if not calls:
                        break
                    carried.append(self.order(bodies[0]))
                    self.assertLessEqual(len(((row or {}).get("text") or calls[-1]).encode()), 16 * 1024)
                    self.consume(calls, row)
                self.assertEqual("".join(carried), "ABCDE", carried)
                self.assertGreater(len(carried), 1, "one transfer cannot hold all five")
                for label in "ABCDE":
                    self.assertEqual(self.completion(label)["answer"], long(label))

    # -- rule 2: one refused answer leaves the bundle alone -------------------------------------------

    def attachment(self):
        return {"schema": session_shell.SCHEMA, "harness": "codex", "sid": SID, "incarnation": "0" * 32,
                "host_pid": 4242, "host_start": "1", "native_pid": 4243, "native_start": "1",
                "socket": "/tmp/answer-bundle-fixture.sock"}

    def test_an_answer_whose_window_changed_leaves_the_bundle_and_the_others_go_now(self):
        # The review's scene (Codex): A was answered at a shell panel that is gone, B at Fleet.
        with self.fresh(harness="codex"):
            self.ask("codex", "AB")
            live = {"on": True}
            attachment = self.attachment()
            with mock.patch.object(session_shell, "answer_attachment_current",
                                   lambda value: live["on"] and value == attachment):
                self.answer("A", answer_attachment=attachment)
                self.answer("B")
                live["on"] = False
                bodies, calls, row = self.carry()
                self.assertEqual((len(calls), self.order(bodies[0])), (1, "B"), "one send, B alone")
                self.assertEqual(self.states(), {"A": "rejected/submitted/open", "B": "sent-ambiguous/submitted/open"})
                self.assertEqual(self.f.record(self.signals["A"])["expiry_reason"], "answer-submission-not-current")
                self.consume(calls, row)
                self.assertEqual(self.carry()[1], [])
            # Nothing is left unknown: A's question stands again and its next answer arrives once.
            self.assertEqual(self.states(), {"A": "rejected/open/open", "B": "acked/applied/answered"})
            self.answer("A", ["Field Guide"], "action-A2")
            bodies, calls, row = self.carry()
            self.assertEqual((len(calls), self.order(bodies[0])), (1, "A"))
            self.consume(calls, row)
            self.assertEqual(self.completion("A")["answer"], "Field Guide")
            self.assertEqual(self.carry()[1], [])

    def test_a_window_change_at_the_queue_itself_holds_the_others_one_lease_as_before(self):
        # The same change a moment later, at the queue's own check: nothing is written, both wait out the claim
        # lease (as on main), and the next pass refuses A alone and sends B.
        with self.fresh(harness="codex"):
            self.ask("codex", "AB")
            live = {"on": True}
            attachment = self.attachment()
            real = obligations.session_notice_delivery_current

            def at_the_queue(message):
                live["on"] = False
                return real(message)

            with mock.patch.object(session_shell, "answer_attachment_current",
                                   lambda value: live["on"] and value == attachment), \
                    mock.patch.object(obligations, "session_notice_delivery_current", at_the_queue):
                self.answer("A", answer_attachment=attachment)
                self.answer("B")
                bodies, calls, _row = self.carry()
                self.assertEqual((len(bodies), calls), (1, []), "RPC 0")
                with self.later(40):
                    bodies, calls, row = self.carry()
                self.assertEqual((len(calls), self.order(bodies[-1])), (1, "B"))
                self.consume(calls, row)
            self.assertEqual(self.states()["B"], "acked/applied/answered")
            self.assertEqual(self.states()["A"].split("/")[0], "rejected")

    def test_a_handed_over_answer_is_not_sent_again_and_the_next_waits_for_it(self):
        for harness in HARNESSES:
            with self.fresh(harness=harness):
                self.ask(harness, "AB")
                self.answer("A")
                _bodies, calls_a, row_a = self.carry()
                self.assertEqual(len(calls_a), 1)
                self.answer("B")      # A is queued; its receipt has not come
                for _ in range(2):    # the same pass again, a restarted courier
                    self.assertEqual(self.carry()[1], [], "A is not sent again, B waits")
                self.consume(calls_a, row_a)
                bodies, calls, row = self.carry()
                self.assertEqual((len(calls), self.order(bodies[0])), (1, "B"))
                self.consume(calls, row)
                self.assertEqual(set(self.states().values()), {"acked/applied/answered"})

    def test_each_receipt_closes_only_its_own_answer(self):
        for harness in HARNESSES:
            with self.fresh(harness=harness):
                self.ask(harness, "AB")
                self.answer("A")
                self.answer("B")
                # Another place answers A again: the first claim stays (one answer per id).
                envelope = waits.read_selected_request({"id": self.signals["A"]["id"], "harness": harness,
                                                        "session_id": SID})
                self.assertEqual(waits.submit_selected_response(
                    envelope, {"answers": {"message-response": ["Field Guide"]}},
                    action_id="action-A-elsewhere")["action_id"], "action-A", "the same id answered once")
                bodies, calls, _row = self.carry()
                self.assertEqual(self.order(bodies[0]), "AB")
                for label, other in (("B", "A"), ("A", None)):
                    record = self.f.record(self.signals[label])
                    self.assertTrue(waits.complete_message_answer(
                        record["receipt"], {"harness": harness, "session_id": SID}))
                    self.assertEqual(self.states()[label].split("/")[2], "answered")
                    if other:
                        self.assertEqual(self.states()[other].split("/")[1:], ["submitted", "open"])

    def test_bytes_notes_and_secrets_arrive_whole_in_one_bundle(self):
        multiline = "첫 줄\n  `utilities/session_notice.py:143` 그대로\n\n\t탭 줄 · 끝"
        for harness in HARNESSES:
            with self.fresh(harness=harness):
                self.signals = {"A": self.f.ask(harness, SID, text="Cover?", choices=["Dogwood", "Field Guide"])[0],
                                "B": self.f.ask(harness, SID, text="Notes?")[0],
                                "C": self.f.ask(harness, SID, text="Token?", secret=True)[0]}
                self.answer("A", ["Field Guide", "표지는 초록 · 제목은 두 줄"])
                self.answer("B", [multiline])
                self.answer("C", ["SECRET-BUNDLE-SENTINEL"])
                bodies, calls, row = self.carry()
                self.assertEqual((len(calls), self.order(bodies[0])), (1, "ABC"))
                self.assertNotIn("SECRET-BUNDLE-SENTINEL", calls[0], "the window never shows the secret")
                self.consume(calls, row)
                self.assertEqual((self.completion("A")["answer"], self.completion("A")["note"]),
                                 ("Field Guide", "표지는 초록 · 제목은 두 줄"))
                self.assertEqual(self.completion("B")["answer"], multiline)
                self.assertNotIn("answer", self.completion("C"))
                self.assertEqual(set(self.states().values()), {"acked/applied/answered"})

    def test_one_answer_alone_is_delivered_as_before(self):
        for harness in HARNESSES:
            with self.fresh(harness=harness):
                self.ask(harness, "A")
                self.answer("A", ["Dogwood"])
                record = self.f.record(self.signals["A"])
                self.assertEqual(record["delivery_id"], key_id(self.signals["A"]))
                bodies, calls, row = self.carry()
                from dispatch_recipient_receipt import receipt_marker
                self.assertEqual(bodies, [sweep.NOTICE_DELIVERY_HEADER + "\n- Question answer (request=%s):\nDogwood\n%s"
                                          % (self.signals["A"]["id"], receipt_marker(record, SID))])
                self.consume(calls, row)
                self.assertEqual(self.states()["A"], "acked/applied/answered")


if __name__ == "__main__":
    unittest.main()
