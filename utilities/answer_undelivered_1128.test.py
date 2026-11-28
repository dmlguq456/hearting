"""11-28 ("답변을 받는것도 보니까 사실상 하나씩만 잘 동작하는 구조인것 같더만"): an answer the person sent is
"sent" only while its delivery can still arrive.

An answer whose delivery ended `rejected` or `expired` after it was sent (review 1633 follow-up 2), or that an
older release's courier ended right after an install because it could not read the answer's new shape (review
1799, advisory 1), stayed `submitted`: the panel kept `✓ answer sent`, no count held it, and the question could
not be answered again. Now the courier pass that retires the ended delivery's duty releases that exact action
(`session_notice.release_undelivered_answer`): the question stands and counts again, and the next answer goes as a
new delivery, once, while the ended record stays as its receipt. An answer that arrived, or that may have arrived,
is never released. A correction sent to a blocked worker (S3, follow-up 6) is no sent line in its parent's panel.

The questions are registered, answered, carried and taken through the real code in a private state root
(fixtures/message_questions.py); only native census, RPC and I/O are fixtures. No window, process or model is used.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import dispatch_pending_delivery as pending  # noqa: E402
import human_wait_state as waits  # noqa: E402
import peer_obligations as obligations  # noqa: E402
import questions  # noqa: E402
import session_notice  # noqa: E402
import session_shell as shell  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")
CHOICES = ["original-value", "other"]


class AnswerUndeliveredTest(unittest.TestCase):
    @contextlib.contextmanager
    def fresh(self, **subtest):
        """One case: its own private state root, so no case's duties ride another's courier pass."""
        from fixtures.message_questions import MessageQuestionFixture
        with self.subTest(**subtest), MessageQuestionFixture() as self.f:
            yield

    # -- the real path, step by step ------------------------------------------------------------

    def ask(self, harness, sid, **kwargs):
        signal, envelope = self.f.ask(harness, sid, "Synthetic question?", choices=CHOICES, **kwargs)
        self.assertTrue(waits._refresh_cache())
        return signal, envelope

    def answer(self, signal, values, action):
        envelope = waits.read_selected_request({"id": signal["id"], "harness": signal["harness"],
                                                "session_id": signal["session_id"]})
        return waits.submit_selected_response(envelope, {"answers": {"message-response": values}},
                                              action_id=action)

    def carry(self, signal):
        """One normal courier pass; returns what reached the session (nothing once no duty is left)."""
        try:
            calls, _ref, row = self.f.courier(signal)
        except StopIteration:
            return None
        if not calls:
            return ""
        return (row or {}).get("text") or calls[-1]

    def end(self, signal, ending):
        """End the current answer's open delivery the ways the queue ends one: a carrier's typed refusal, or the
        operator's recovery expiry (answers are kept out of the notice TTL, so that is how one expires today)."""
        record = self.f.record(signal)
        sid = signal["session_id"]
        if ending == "rejected":
            claim = pending.claim(self.f.root, sid, record["delivery_id"], claim_owner="fixture-carrier",
                                  lease_seconds=30, require_generation_proof=False, jobs=self.f.jobs)
            pending.reject_claimed(self.f.root, sid, claim["delivery_id"], claim_owner="fixture-carrier",
                                   reason="fixture-typed-refusal")
        else:
            pending.expire_recovery(self.f.root, sid, record["delivery_id"],
                                    expected={"receipt_digest": record["receipt_digest"]},
                                    actor="fixture-operator", reason="fixture recovery")
        return pending.read(self.f.root, sid, record["delivery_id"])

    def entry(self, signal):
        return waits._selected_entry({"harness": signal["harness"], "session_id": signal["session_id"],
                                      "request_id": signal["id"], "question_id": "message-response",
                                      "turn_id": None}) or {}

    def seen(self, signal):
        """What the person sees: the asking panel's row, Fleet's row, the shared count and `questions list`."""
        self.assertTrue(waits._refresh_cache())
        view = waits.cached_view(request=False) or {}
        board = shell.session_questions(signal["harness"], signal["session_id"], view=view)
        row = next((item for item in board if item.get("id") == signal["id"]), None)
        panel = ("none" if row is None else "question" if row.get("answerable") is True
                 else "sent" if row.get("sent") is not None else "other")
        fleet = json.loads(waits._fleet_cache_path().read_text(encoding="utf-8"))
        frow = next((item for item in fleet.get("items", ()) if item.get("id") == signal["id"]), None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            questions.main(["list"])
        counts = waits.wait_counts(view.get("items") or ())
        head = out.getvalue().splitlines()[0]
        self.assertTrue(head.startswith("questions: %d current" % counts["total"]), head)
        return {"panel": panel, "fleet": None if frow is None else frow.get("answer", "waiting"),
                "counted": signal["id"] in (view.get("by_session") or {}).get(
                    (signal["harness"], signal["session_id"]), ()),
                "total": counts["total"], "row": row, "listing": out.getvalue()}

    def source(self, signal):
        return waits._read(waits._signal_path(signal)) or {}

    def assert_standing_again(self, signal, ended):
        seen = self.seen(signal)
        self.assertEqual((seen["panel"], seen["fleet"], seen["counted"]), ("question", "waiting", True))
        entry = self.entry(signal)
        self.assertEqual((entry.get("state"), entry.get("action_id")), ("open", None))
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        kept = pending.read(self.f.root, signal["session_id"], ended["delivery_id"])
        self.assertEqual(kept["state"], ended["state"], "the ended delivery stays as its receipt")
        return seen

    def assert_arrives_once(self, signal, values, action):
        again = self.answer(signal, values, action)
        self.assertEqual((again["state"], again["action_id"]), ("submitted", action), again.get("result"))
        self.assertEqual(self.seen(signal)["panel"], "sent")
        record = self.f.record(signal)
        self.assertEqual((record["state"], record["receipt"]["answer_binding"]["action_id"]), ("pending", action))
        wire = self.carry(signal)
        self.assertEqual(len(self.f.bodies), 1, "one send")
        self.last_body = self.f.bodies[0]
        self.f.consume(signal, wire)
        completion = self.source(signal)["completion"]
        self.assertEqual(completion["action_id"], action)
        self.assertEqual(self.entry(signal)["state"], "applied")
        self.assertEqual(self.f.record(signal)["state"], "acked")
        self.assertIsNone(self.carry(signal), "nothing is left to send again")
        self.assertEqual((self.seen(signal)["panel"], self.seen(signal)["counted"]), ("none", False))
        return completion

    # -- A: a delivery that ends later ----------------------------------------------------------

    def test_a_delivery_that_ends_later_brings_the_question_back_and_the_next_answer_arrives_once(self):
        for harness in HARNESSES:
            for ending in ("rejected", "expired"):
                for second in (["original-value"], ["other"]):   # the same answer, a different one
                    with self.fresh(harness=harness, ending=ending, second=second[0]):
                        signal, _ = self.ask(harness, "a-%s-%s-%s" % (harness, ending, second[0]))
                        self.assertEqual(self.answer(signal, ["original-value"], "action-one")["state"], "submitted")
                        sent = self.seen(signal)
                        self.assertEqual((sent["panel"], sent["fleet"], sent["counted"]), ("sent", "sent", False))
                        ended = self.end(signal, ending)
                        self.assertEqual(self.carry(signal), None, "the pass retires the ended duty, sends nothing")
                        self.assert_standing_again(signal, ended)
                        completion = self.assert_arrives_once(signal, second, "action-two")
                        self.assertEqual(completion["answer"], second[0])
                        self.assertEqual(pending.read(self.f.root, signal["session_id"],
                                                      ended["delivery_id"])["state"], ending)

    # -- B: an older release's courier ends a new-shaped answer ---------------------------------

    def test_an_older_courier_ending_a_new_shaped_answer_is_seen_by_this_courier(self):
        # Review 1799 (advisory 1): right after an install, an older release's courier picks the answer it cannot
        # read (two values, a value over 600 characters, an array-shaped value) and ends it `rejected` through the
        # same keep_claim this release has (`notice-obligation-resolved`), leaving its duty pending. Isolated runs
        # against the older code (6412bdc12) leave exactly this state; this release's next pass is the first to see it.
        shapes = {"two values": ["original-value", "A separate note"], "700 characters": ["x" * 700],
                  "array-shaped": ['["a","b"]']}
        for harness in HARNESSES:
            for label, values in shapes.items():
                with self.fresh(harness=harness, shape=label):
                    signal, _ = self.ask(harness, "b-%s-%d" % (harness, len(label)))
                    self.assertEqual(self.answer(signal, values, "action-one")["state"], "submitted")
                    with mock.patch.object(session_notice, "notice_is_current", return_value=False):
                        self.assertEqual(self.carry(signal), "", "the older courier ends it and sends nothing")
                    ended = pending.read(self.f.root, signal["session_id"], self.f.record(signal)["delivery_id"])
                    self.assertEqual((ended["state"], ended["expiry_reason"]), ("rejected", "notice-obligation-resolved"))
                    duty = next(item for item in obligations.ObligationStore(self.f.root).list(kinds={"run-notice"})
                                if item["intent"]["delivery_id"] == ended["delivery_id"])
                    self.assertEqual(duty["state"], "pending")
                    self.assertEqual(self.seen(signal)["panel"], "sent")
                    self.assertIsNone(self.carry(signal))
                    self.assert_standing_again(signal, ended)
                    completion = self.assert_arrives_once(signal, values, "action-two")
                    self.assertEqual(completion["answer"], values[0])
                    self.assertEqual(completion.get("note"), values[1] if len(values) == 2 else None)

    # -- what is never released -----------------------------------------------------------------

    def test_an_answer_that_arrived_or_may_have_arrived_is_never_released(self):
        for harness in HARNESSES:
            with self.fresh(harness=harness, case="sent unconfirmed, then acked and applied"):
                signal, _ = self.ask(harness, "ambiguous-" + harness)
                self.answer(signal, ["original-value"], "action-one")
                wire = self.carry(signal)
                record = self.f.record(signal)
                self.assertEqual(record["state"], "sent-ambiguous")
                self.assertFalse(session_notice.release_undelivered_answer(record))
                self.carry(signal)
                self.assertEqual((self.entry(signal)["state"], self.seen(signal)["panel"]), ("submitted", "sent"))
                # then taken: acked and applied
                self.f.consume(signal, wire)
                record = self.f.record(signal)
                self.assertEqual((record["state"], self.entry(signal)["state"]), ("acked", "applied"))
                self.assertFalse(session_notice.release_undelivered_answer(record))
                self.assertFalse(session_notice.release_undelivered_answer(dict(record, state="rejected")))
                self.assertEqual(self.entry(signal)["state"], "applied")
                self.assertEqual(self.source(signal)["lifecycle"], "answered")
            with self.fresh(harness=harness, case="received"):
                signal, envelope = self.ask(harness, "received-" + harness)
                self.answer(signal, ["original-value"], "action-one")
                entry = self.entry(signal)
                self.assertTrue(waits._store_selected_entry(envelope, dict(
                    entry, state="received", result={"transport": "received", "status": "received"})))
                ended = self.end(signal, "rejected")
                self.assertFalse(session_notice.release_undelivered_answer(ended))
                self.assertIsNone(self.carry(signal))
                self.assertEqual((self.entry(signal)["state"], self.entry(signal)["action_id"]),
                                 ("received", "action-one"))

    def test_other_questions_sessions_and_notices_are_untouched(self):
        for harness in HARNESSES:
            with self.fresh(harness=harness):
                done, _ = self.ask(harness, "same-" + harness)          # answered and taken first
                self.answer(done, ["original-value"], "done-action")
                self.f.consume(done, self.carry(done))
                other, _ = self.ask(harness, "other-" + harness)       # another session's answer on its way
                self.answer(other, ["other"], "other-action")
                signal, _ = self.f.ask(harness, "same-" + harness, "A later synthetic question?",
                                       choices=CHOICES)                 # the same session's next question
                self.assertNotEqual(signal["id"], done["id"])
                self.answer(signal, ["original-value"], "action-one")
                note = session_notice.notify(harness, "same-" + harness, key="fixture-notice:" + harness,
                                             subject="fixture", text="an ordinary notice", jobs=self.f.jobs)
                claim = pending.claim(self.f.root, "same-" + harness, note["delivery_id"], claim_owner="fixture",
                                      lease_seconds=30, require_generation_proof=False, jobs=self.f.jobs)
                ended_note = pending.reject_claimed(self.f.root, "same-" + harness, claim["delivery_id"],
                                                    claim_owner="fixture", reason="fixture")
                self.assertFalse(session_notice.release_undelivered_answer(ended_note))
                ended = self.end(signal, "rejected")
                self.assertIsNone(self.carry(signal))
                self.assert_standing_again(signal, ended)
                self.assertEqual((self.entry(done)["state"], self.f.record(done)["state"]), ("applied", "acked"))
                self.assertEqual((self.entry(other)["state"], self.entry(other)["action_id"]),
                                 ("submitted", "other-action"))
                self.assertEqual(self.seen(other)["panel"], "sent")
                self.assertEqual(pending.read(self.f.root, "same-" + harness, note["delivery_id"])["state"], "rejected")

    def test_a_secret_or_multiline_answer_comes_back_hidden_and_goes_again_whole(self):
        multiline = "first line of the answer\n  second line, indented\n\nlast line"
        for harness in HARNESSES:
            with self.fresh(harness=harness, case="secret"):
                signal, _ = self.f.ask(harness, "secret-" + harness, "Synthetic secret?", secret=True)
                self.assertTrue(waits._refresh_cache())
                self.answer(signal, ["SECRET-ANSWER-SENTINEL"], "action-one")
                ended = self.end(signal, "expired")
                self.assertIsNone(self.carry(signal))
                seen = self.assert_standing_again(signal, ended)
                for shown in (json.dumps(seen["row"]), seen["listing"],
                              waits._fleet_cache_path().read_text(encoding="utf-8")):
                    self.assertNotIn("SECRET-ANSWER-SENTINEL", shown)
                self.assert_arrives_once(signal, ["SECRET-ANSWER-SENTINEL-2"], "action-two")
                self.assertNotIn("answer", self.source(signal)["completion"], "a secret answer is not kept in clear")
            with self.fresh(harness=harness, case="multiline"):
                signal, _ = self.ask(harness, "multiline-" + harness)
                self.answer(signal, [multiline], "action-one")
                ended = self.end(signal, "rejected")
                self.assertIsNone(self.carry(signal))
                self.assert_standing_again(signal, ended)
                completion = self.assert_arrives_once(signal, [multiline], "action-two")
                self.assertEqual(completion["answer"], multiline)
                self.assertIn(multiline, self.last_body)

    def test_reading_the_same_ended_record_again_changes_nothing(self):
        for harness in HARNESSES:
            with self.fresh(harness=harness):
                signal, _ = self.ask(harness, "twice-" + harness)
                self.answer(signal, ["original-value"], "action-one")
                ended = self.end(signal, "rejected")
                self.assertTrue(session_notice.release_undelivered_answer(ended))
                self.assertFalse(session_notice.release_undelivered_answer(ended))
                self.assertIsNone(self.carry(signal))     # the restarted courier reads it again
                self.assertIsNone(self.carry(signal))
                self.assert_standing_again(signal, ended)
                self.assertEqual(self.answer(signal, ["other"], "action-two")["state"], "submitted")
                self.assertFalse(session_notice.release_undelivered_answer(ended), "the old record never ends a later answer")
                self.assertEqual((self.entry(signal)["state"], self.entry(signal)["action_id"]),
                                 ("submitted", "action-two"))


class BlockedWorkerCorrectionLineTest(unittest.TestCase):
    """Review 1633, follow-up 6: a correction sent to a blocked worker (S3) is its parent agent's, not a person's
    answer; the parent's panel shows no sent line for it. A person's sent answer and a gate's still show theirs;
    Fleet keeps marking the S3 row as sent, and no count changes."""

    def test_only_a_persons_sent_answers_are_sent_lines_in_the_panel(self):
        def describe(item, **_kw):
            return dict(id=item["id"], source=item["source"], owner=item["owner"], state="current",
                        questions=[dict(id="q", question=item["id"], choices=[])],
                        answerable=False, request_state="received", sent=["continue"])
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                owner = {"harness": harness, "session_id": "parent-" + harness}
                items = [dict(id=wait_id, source=wait_id[:2], kind="message", since=1, stale=False,
                              owner=owner, answer="sent")
                         for wait_id in ("S3:att-blocked", "S4:" + "a" * 64, "S2:route:gate")]
                view = {"items": items, "by_session": {}, "total": 0, "complete": True}
                with mock.patch.object(questions, "describe", side_effect=describe):
                    board = shell.session_questions(harness, owner["session_id"], view=view)
                self.assertEqual([row["id"] for row in board], ["S4:" + "a" * 64, "S2:route:gate"])
                self.assertEqual(waits.wait_counts(items)["total"], 0)
                self.assertEqual([row["answer"] for row in items], ["sent"] * 3)


if __name__ == "__main__":
    unittest.main()
