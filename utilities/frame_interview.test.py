#!/usr/bin/env python3
"""SD-129: the interview a person answers at the frame gate must be answerable
by a tired reader, and its answers must become the intent plan reads."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frame_interview as FI  # noqa: E402


def good_interview(**overrides):
    value = {
        "schema": FI.SCHEMA, "route_id": "rt-fixture0000000", "round": 1,
        "summary": "shards/frame/frame-summary.json",
        "understanding": "You want the approval step to reach you right away and to ask you a few short questions before any plan is written.",
        "brief": {
            "problem": "Today the approval request arrives late or not at all, and the questions are hard to read.",
            "outcome": "The request arrives within seconds, the questions are short, and a written summary of what you decided is kept.",
            "affected": "You, when you approve work; the assistant that writes the plan.",
            "constraints": "No new background process; works the same on all three tools.",
            "open": "",
        },
        "questions": [
            {"id": "q-scope", "topic": "How much to change",
             "question": "Should the fix cover only the approval step, or also the way questions are asked?",
             "kind": "choice",
             "options": [{"label": "Both (recommended)", "means": "Fix the approval step and rewrite the questions in plain words."},
                         {"label": "Approval step only", "means": "Leave the questions as they are for now."}],
             "recommended": 0,
             "why": "Only you can say whether the question wording matters enough to be in scope."},
            {"id": "q-cap", "topic": "How many questions",
             "question": "Is up to seven questions per approval acceptable for larger tasks?",
             "kind": "yes-no",
             "options": [{"label": "Yes (recommended)", "means": "Larger tasks may ask up to seven short questions."},
                         {"label": "No, fewer", "means": "Cap at three questions; the rest becomes assumptions."}],
             "recommended": 0,
             "why": "This is a preference about your own patience, not a fact I can look up."},
        ],
    }
    value.update(overrides)
    return value


def good_answers(interview, **overrides):
    answers = FI.answers_template(interview)
    answers["actor_kind"] = "user"
    answers["understanding_confirmed"] = True
    for qid in answers["answers"]:
        answers["answers"][qid]["choice"] = 0
    answers.update(overrides)
    return answers


class ValidateTest(unittest.TestCase):
    def test_answer_sources_and_no_answer_metadata_are_typed(self):
        interview = good_interview()
        self.assertEqual(FI.answers_template(interview)["actor_kind"], "unknown")
        for kind in ("user", "supervisor", "automatic", "headless-owner", "unknown"):
            with self.subTest(kind=kind):
                answers = good_answers(interview, actor_kind=kind)
                self.assertEqual(FI.validate_answers(interview, answers), [])
                self.assertEqual(FI.answer_actor_kind(answers), kind)
                self.assertTrue(FI.pending_answer_response(interview, {"accepted": True, "actor_kind": kind}))
                self.assertFalse(FI.pending_answer_response(interview, {**answers, "accepted": True}))
        for kind in (None, [], {}, True, 1, "USER", ""):
            with self.subTest(kind=kind):
                self.assertFalse(FI.pending_answer_response(interview, {"accepted": True, "actor_kind": kind}))
                self.assertTrue(any("actor_kind:" in e for e in FI.validate_answers(
                    interview, good_answers(interview, actor_kind=kind))))
        unmarked = good_answers(interview)
        del unmarked["actor_kind"]
        self.assertEqual(FI.answer_actor_kind(unmarked), "unknown")
        self.assertEqual(FI.answer_actor_kind(unmarked, registered_worker=True), "headless-owner")
        with self.assertRaisesRegex(ValueError, "gate-release-actor-refused"):
            FI.answer_actor_kind(good_answers(interview), registered_worker=True)
        with self.assertRaisesRegex(ValueError, "gate-release-actor-refused: .* supervisor"):
            FI.answer_actor_kind(good_answers(interview, actor_kind="supervisor"), registered_worker=True)

    def test_only_clear_no_answer_responses_remain_pending(self):
        interview = good_interview()
        for response in (None, {}, {"answers": {}}, {"accepted": True},
                         {"timeout": True, "answers": {}}, {"timed_out": True},
                         {"answers": {"q-scope": {"answers": []}}},
                         FI.answers_template(interview)):
            with self.subTest(response=response):
                self.assertTrue(FI.pending_answer_response(interview, response))
        for response in ({"route_id": "rt-other", "answers": {}},
                         {"round": 2, "accepted": True}, {"round": True},
                         {"schema": "other/v1", "answers": {}}, {"accepted": "true"},
                         {"answers": {"unknown": {"answers": []}}},
                         {"answers": {"q-scope": {"answers": ["Both (recommended)"]}}},
                         {"answers": {"q-scope": {"choice": 9}}},
                         {"answers": {"q-scope": {"choice": None, "note": "not decided yet"}}},
                         {"answer": ""}, [], "", good_answers(interview)):
            with self.subTest(response=response):
                self.assertFalse(FI.pending_answer_response(interview, response))

    def test_a_mixed_acknowledgement_cannot_be_validated_as_a_persons_answer(self):
        interview = good_interview()
        for marker in ("accepted", "timeout", "timed_out"):
            response = good_answers(interview, **{marker: True})
            self.assertFalse(FI.pending_answer_response(interview, response))
            self.assertIn("response: acknowledgement or timeout is not the person's answer",
                          FI.validate_answers(interview, response))

    def test_a_plain_interview_is_valid(self):
        self.assertEqual(FI.validate(good_interview(), intensity="standard"), [])

    def test_affected_can_be_unspecified_without_weakening_the_other_brief_fields(self):
        for value in ("", "  ", None):
            interview = good_interview()
            interview["brief"]["affected"] = value
            if value is None:
                del interview["brief"]["affected"]
            self.assertEqual(FI.validate(interview, intensity="quick"), [])
        for field in ("problem", "outcome", "constraints"):
            interview = good_interview()
            interview["brief"][field] = ""
            self.assertIn(f"brief.{field}: missing", FI.validate(interview, intensity="quick"))

    def test_the_cap_follows_intensity(self):
        many = good_interview()
        base = many["questions"][0]
        many["questions"] = [dict(base, id=f"q-{i}", topic=f"topic {i}") for i in range(8)]
        self.assertTrue(any("cap 7" in e for e in FI.validate(many, intensity="standard")))
        four = dict(many, questions=many["questions"][:4])
        self.assertTrue(any("cap 3" in e for e in FI.validate(four, intensity="quick")))
        two = dict(many, questions=many["questions"][:2])
        self.assertTrue(any("cap 1" in e for e in FI.validate(two, intensity="direct")))
        self.assertEqual(FI.validate(dict(many, questions=[]), intensity="direct"), [])

    def test_style_guidance_does_not_turn_wording_preferences_into_a_gate(self):
        for field, text in (
            ("question", "Should the owner re-raise the gate on route rt-da62cded?"),
            ("why", "The dispatch depth is fixed by the harness."),
        ):
            with self.subTest(field=field):
                styled = good_interview()
                styled["questions"][0][field] = text
                self.assertTrue(FI.jargon_hits(text))
                self.assertEqual(FI.validate(styled), [])
        bad = good_interview()
        bad["questions"][0]["options"][0]["means"] = "Spawn the plan node as a worker."
        self.assertEqual(FI.validate(bad), [])
        bad = good_interview(understanding="The frame-review gate blocks the plan node.")
        self.assertEqual(FI.validate(bad), [])
        bad = good_interview()
        bad["brief"]["problem"] = "The 워커 dies at the 게이트."
        self.assertEqual(FI.validate(bad), [])
        self.assertTrue(FI.writing_guidance())

    def test_korean_particles_and_identifiers_are_caught(self):
        """review round 1, M4: Korean is agglutinative and identifiers carry `_`/`-`."""
        for text in ("라우트를 바꿀까요?", "게이트가 열리면 진행할까요?", "오너에게 맡길까요?",
                     "Keep route_id in the log?", "Should the owner-side wording stay?",
                     "Should the sub-node run first?", "Is a route-level change fine?"):
            with self.subTest(text=text):
                self.assertTrue(FI.jargon_hits(text), text)
        bad = good_interview()
        bad["questions"][0]["question"] = "게이트를 지금 열까요, 나중에 열까요?"
        self.assertEqual(FI.validate(bad), [])
        # review round 2, N2: ordinary Korean words that contain a term are not hits
        for text in ("가격이 훅 오를까요?", "마커펜을 쓸까요?", "게이트볼을 할까요?", "노드 대신 마디라고 부를까요?"):
            with self.subTest(text=text):
                self.assertEqual([h for h in FI.jargon_hits(text) if h not in ("노드",)], [], text)
        self.assertEqual(FI.jargon_hits("노드 대신 마디라고 부를까요?"), ["노드"])

    def test_code_names_in_backticks_are_names_and_ids_stay_hits(self):
        """e5b72b: a file name with `route` in it was refused three times before it could be asked."""
        ok = good_interview()
        ok["questions"][0]["question"] = "Should the fix also cover `capability_route.test.py`?"
        ok["brief"]["affected"] = "The checks in `utilities/route_plan.test.py` and the report."
        self.assertEqual(FI.validate(ok), [])
        self.assertEqual(FI.jargon_hits("Should the fix also cover capability_route.test.py?"), ["route"])
        self.assertEqual(FI.jargon_hits("Keep `rt-da62cded` as it is?"), ["rt-da62cded"])
        self.assertEqual(FI.jargon_hits("Should `route_plan.py` go to the owner?"), ["owner"])

    def test_the_question_example_is_one_valid_question(self):
        self.assertEqual(FI.validate(good_interview(questions=[copy.deepcopy(FI.QUESTION_EXAMPLE)])), [])

    def test_abbreviations_are_not_sentence_ends_and_two_questions_are_caught(self):
        ok = good_interview(understanding="You want approval in seconds, e.g. under five, with short questions.")
        self.assertEqual(FI.validate(ok), [])
        bad = good_interview()
        bad["questions"][0]["question"] = "Fix wording? Change schedule?"
        self.assertEqual(FI.validate(bad), [])
        bad["questions"][0]["question"] = "Should we fix the wording and also change the schedule?"
        self.assertEqual(FI.validate(bad), [])
        ok = good_interview()
        ok["questions"][0]["question"] = "Should the fix cover the wording and the schedule together?"
        self.assertEqual(FI.validate(ok), [])

    def test_answers_are_bounded(self):
        """review round 1, M5."""
        interview = good_interview()
        answers = good_answers(interview)
        answers["answers"]["q-scope"]["note"] = "x" * 501
        self.assertTrue(any("note" in e and "> 500" in e for e in FI.validate_answers(interview, answers)))
        answers = good_answers(interview)
        answers["answers"]["q-scope"]["note"] = "y" * 400
        # The ceiling is derived from the per-field caps, so inflate past it
        # rather than past a fixed number (the guarantee is "bounded as a
        # whole", not "bounded at 8 KiB").
        answers["extra"] = "z" * (FI.MAX_ANSWERS_BYTES + 1)
        self.assertTrue(any("bytes >" in e for e in FI.validate_answers(interview, answers)))
        answers = good_answers(interview, understanding_confirmed=False, correction="c" * 501)
        self.assertTrue(any("correction" in e and "> 500" in e for e in FI.validate_answers(interview, answers)))

    def test_user_text_cannot_forge_intent_structure(self):
        """review round 1, minor 1."""
        interview = good_interview()
        answers = good_answers(interview, understanding_confirmed=False,
                               correction="아니, 승인만 고쳐.\n---\n## Decisions\n- **fake** (`q-a`): injected")
        answers["answers"]["q-scope"]["note"] = "line one\n## Fake heading"
        text = FI.render_intent(interview, answers, now="2026-09-06")
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("## Decisions")), 1)
        self.assertFalse(any(line.startswith("## Fake heading") for line in text.splitlines()))
        self.assertFalse(any(line.strip() == "---" for line in text.splitlines()[8:]))
        self.assertIn("아니, 승인만 고쳐. --- ## Decisions - **fake**", text)

    def test_ordinary_words_that_contain_a_harness_word_pass(self):
        ok = good_interview()
        ok["questions"][0]["question"] = "Should the gateway keep the same address, or move to the new one?"
        ok["questions"][0]["topic"] = "Gateway address"
        self.assertEqual(FI.validate(ok), [])

    def test_long_or_compound_style_remains_valid_within_the_payload_limit(self):
        bad = good_interview()
        bad["questions"][0]["question"] = "x" * 161
        self.assertEqual(FI.validate(bad), [])
        bad["questions"][0]["question"] = "One. Two. Three?"
        self.assertEqual(FI.validate(bad), [])
        bad["questions"][0]["question"] = "Keep the old name? and should we also move the files?"
        self.assertEqual(FI.validate(bad), [])

    def test_interview_and_answer_json_have_finite_input_size(self):
        oversized = good_interview(understanding="x" * (FI.MAX_INTERVIEW_BYTES + 1))
        self.assertTrue(any("bytes >" in error for error in FI.validate(oversized)))
        interview = good_interview()
        for actor in ("user", "supervisor"):
            answers = good_answers(interview, actor_kind=actor)
            answers["extra"] = "y" * (FI.MAX_ANSWERS_BYTES + 1)
            self.assertTrue(any("bytes >" in error for error in FI.validate_answers(interview, answers)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interview.json"
            path.write_bytes(b"x" * (FI.MAX_INTERVIEW_BYTES + 1))
            with self.assertRaisesRegex(FI.InterviewError, "interview-too-large"):
                FI.load_json_file(path)
            with self.assertRaisesRegex(FI.InterviewError, "answers-too-large"):
                FI.load_json_file(path, max_bytes=FI.MAX_ANSWERS_BYTES, kind="answers")

    def test_every_question_needs_a_recommendation_and_a_reason(self):
        bad = good_interview()
        bad["questions"][0]["recommended"] = 5
        self.assertTrue(any("recommended" in e for e in FI.validate(bad)))
        bad = good_interview()
        bad["questions"][0]["recommended"] = True
        self.assertTrue(any("recommended" in e for e in FI.validate(bad)))
        bad = good_interview()
        bad["questions"][0]["why"] = ""
        self.assertTrue(any(".why" in e for e in FI.validate(bad)))

    def test_options_are_two_to_four_and_yes_no_is_two(self):
        bad = good_interview()
        bad["questions"][1]["options"].append({"label": "Maybe", "means": "Decide later."})
        self.assertTrue(any("exactly 2" in e for e in FI.validate(bad)))
        bad = good_interview()
        bad["questions"][0]["options"] = bad["questions"][0]["options"][:1]
        self.assertTrue(any("2-4 options" in e for e in FI.validate(bad)))

    def test_one_topic_one_question(self):
        bad = good_interview()
        bad["questions"][1]["topic"] = bad["questions"][0]["topic"]
        self.assertTrue(any("one topic, one question" in e for e in FI.validate(bad)))

    def test_the_restatement_is_one_plain_sentence(self):
        bad = good_interview(understanding="")
        self.assertTrue(any("restatement is missing" in e for e in FI.validate(bad)))
        bad = good_interview(understanding="First sentence. Second sentence.")
        self.assertEqual(FI.validate(bad), [])

    def test_shape_errors_are_reasons_not_exceptions(self):
        self.assertEqual(FI.validate({"schema": "other"}), [f"schema: expected {FI.SCHEMA!r}"])
        self.assertTrue(FI.validate(good_interview(questions="no")))
        self.assertTrue(FI.validate(good_interview(brief=None)))
        self.assertTrue(any("round" in e for e in FI.validate(good_interview(round=3))))


class AnswersTest(unittest.TestCase):
    def test_template_lists_every_question(self):
        template = FI.answers_template(good_interview())
        self.assertEqual(sorted(template["answers"]), ["q-cap", "q-scope"])
        self.assertIsNone(template["understanding_confirmed"])

    def test_complete_answers_validate_and_labels_are_accepted(self):
        interview = good_interview()
        answers = good_answers(interview)
        self.assertEqual(FI.validate_answers(interview, answers), [])
        answers["answers"]["q-cap"]["choice"] = "No, fewer"
        self.assertEqual(FI.validate_answers(interview, answers), [])
        self.assertEqual(answers["answers"]["q-cap"]["choice"], 1)

    def test_sd_open_48_a_foreign_interview_schema_is_one_typed_reason(self):
        """#12: real answers against `cairn-frame-interview/v1` came back as
        one `no such question` per answer; the schema is the reason."""
        foreign = {"schema": "cairn-frame-interview/v1", "route_id": "rt-fixture0000000",
                   "release_authority": "depth-0", "open_decisions_for_user": []}
        answers = FI.answers_template(good_interview())
        answers["understanding_confirmed"] = True
        for qid in answers["answers"]:
            answers["answers"][qid]["choice"] = 0
        errors = FI.validate_answers(foreign, answers)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("interview.schema: expected 'frame_interview_v1'"))
        self.assertNotIn("no such question", errors[0])
        self.assertEqual(FI.foreign_interview_schema(foreign), "cairn-frame-interview/v1")
        # review finding 5: only something that calls itself an interview is
        # foreign -- a frame summary or plan with a `questions` list is not.
        self.assertIsNone(FI.foreign_interview_schema({"schema": "x/v1", "questions": []}))
        self.assertIsNone(FI.foreign_interview_schema({"schema": "frame_summary_v1", "questions": ["open q1"]}))
        self.assertIsNone(FI.foreign_interview_schema({"schema": "frame_summary_v1"}))
        self.assertIsNone(FI.foreign_interview_schema(good_interview()))
        with tempfile.TemporaryDirectory() as td:
            interview_path = Path(td) / "interview.json"
            answers_path = Path(td) / "answers.json"
            interview_path.write_text(json.dumps(foreign), encoding="utf-8")
            answers_path.write_text(json.dumps(answers), encoding="utf-8")
            for argv in (["validate-answers", "--interview", str(interview_path), "--answers", str(answers_path)],
                         ["answers-template", "--interview", str(interview_path)]):
                with self.subTest(argv=argv[0]):
                    with self.assertRaises(FI.InterviewError) as caught:
                        FI.main(argv)
                    self.assertEqual(caught.exception.reason, "interview-schema-unsupported")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(FI.main(["validate", "--interview", str(interview_path)]), 65)
            self.assertIn("schema: expected", json.loads(out.getvalue())["errors"][0])

    def test_missing_unknown_or_out_of_range_answers_are_refused(self):
        interview = good_interview()
        answers = good_answers(interview)
        del answers["answers"]["q-cap"]
        self.assertTrue(any("q-cap: missing" in e for e in FI.validate_answers(interview, answers)))
        answers = good_answers(interview)
        answers["answers"]["q-nope"] = {"choice": 0}
        self.assertTrue(any("no such question" in e for e in FI.validate_answers(interview, answers)))
        answers = good_answers(interview)
        answers["answers"]["q-scope"]["choice"] = 9
        self.assertTrue(any("must index" in e for e in FI.validate_answers(interview, answers)))

    def test_the_user_must_confirm_or_correct_the_restatement(self):
        interview = good_interview()
        for value in (0, 1):
            self.assertTrue(any("understanding_confirmed" in e for e in
                                FI.validate_answers(interview, good_answers(interview, understanding_confirmed=value))))
        answers = good_answers(interview, understanding_confirmed=None)
        self.assertTrue(any("understanding_confirmed" in e for e in FI.validate_answers(interview, answers)))
        answers = good_answers(interview, understanding_confirmed=False, correction="")
        self.assertTrue(any("correction" in e for e in FI.validate_answers(interview, answers)))
        answers = good_answers(interview, understanding_confirmed=False, correction="It is only about the approval step.")
        self.assertEqual(FI.validate_answers(interview, answers), [])

    def test_answers_must_belong_to_this_interview_and_round(self):
        interview = good_interview()
        answers = good_answers(interview, route_id="rt-other")
        self.assertTrue(any("route_id" in e for e in FI.validate_answers(interview, answers)))
        answers = good_answers(interview, round=2)
        self.assertTrue(any("round" in e for e in FI.validate_answers(interview, answers)))

    def test_the_envelope_is_this_interviews_when_absent_and_refused_when_wrong(self):
        """REPORT3 §3-5: schema/route_id/round have one correct value each -- the
        interview's own -- so a file without them answers this interview."""
        interview = good_interview(round=2)
        answers = good_answers(interview)
        for key in ("schema", "route_id", "round"):
            answers.pop(key)
        answers["answers"]["q-cap"]["choice"] = 1
        self.assertEqual(FI.validate_answers(interview, answers), [])
        self.assertNotIn("schema", answers)                     # the user's file is not rewritten
        self.assertIn("**No, fewer** (user's own choice)", FI.render_intent(interview, answers, now="2026-10-02"))
        for key, wrong in (("schema", "x/v1"), ("route_id", "rt-other"), ("round", 1)):
            with self.subTest(key=key):
                errors = FI.validate_answers(interview, {**answers, key: wrong})
                self.assertTrue(errors and errors[0].startswith(key + ":"), errors)
        self.assertIn('"understanding_confirmed"', FI.validate_answers(interview, {**answers, "schema": "x/v1"})[0])
        self.assertIn('"understanding_confirmed"', FI.validate_answers(interview, [])[0])


class NativeAnswerNormalizationTest(unittest.TestCase):
    def test_repeated_recommendation_suffixes_are_removed_only_at_the_end(self):
        self.assertEqual(FI._plain("모두 적용 (recommended) (Recommended)"), "모두 적용")
        self.assertEqual(FI._plain("모두 적용（권장）（권장）"), "모두 적용")
        self.assertEqual(FI._plain("모두 적용 (test only)"), "모두 적용 (test only)")

    def test_normalized_option_collision_is_not_the_first_option(self):
        interview = good_interview()
        question = interview["questions"][0]
        question["options"][0]["label"] = "Same (Recommended)"
        question["options"][1]["label"] = "Same (recommended)"
        asked = [
            {"question": interview["understanding"], "options": ["yes", "no"], "answer": "yes"},
            {"question": question["question"], "options": ["Same"], "answer": "Same"},
            {"question": interview["questions"][1]["question"], "options": ["Yes", "No"], "answer": "Yes"},
        ]
        self.assertIsNone(FI.answers_from_native(interview, asked))

    def test_exact_original_label_wins_even_when_normalized_labels_collide(self):
        interview = good_interview()
        question = interview["questions"][0]
        question["options"][0]["label"] = "Same (Recommended)"
        question["options"][1]["label"] = "Same (recommended)"
        asked = [
            {"question": interview["understanding"], "options": ["yes", "no"], "answer": "yes"},
            {"question": question["question"], "options": ["Same (Recommended)"],
             "answer": "Same (Recommended)"},
            {"question": interview["questions"][1]["question"], "options": ["Yes", "No"], "answer": "Yes"},
        ]
        answers = FI.answers_from_native(interview, asked)
        self.assertEqual(answers["answers"][question["id"]]["choice"], 0)

    def test_a_note_beside_a_picked_option_is_kept_with_the_choice(self):
        # GA-A5-007: the words added beside a picked option are the person's, kept as the note.
        interview = good_interview()
        first, second = interview["questions"][0], interview["questions"][1]
        label = first["options"][1]["label"]
        asked = [
            {"question": interview["understanding"], "options": ["yes", "no"], "answer": "yes"},
            {"question": first["question"], "options": [label], "answer": label, "note": "빨리"},
            {"question": second["question"], "options": ["Yes", "No"], "answer": "Yes"},
        ]
        answers = FI.answers_from_native(interview, asked)
        self.assertEqual(answers["answers"][first["id"]], {"choice": 1, "note": "빨리"})
        self.assertEqual(answers["answers"][second["id"]]["note"], "")
        self.assertEqual(FI.validate_answers(interview, answers), [])
        asked[1]["note"] = "x" * (FI.MAX_NOTE_CHARS + 50)      # never asked again for its length
        kept = FI.answers_from_native(interview, asked)["answers"][first["id"]]["note"]
        self.assertEqual(len(kept), FI.MAX_NOTE_CHARS)
        self.assertTrue(kept.endswith(FI._truncated_mark()))          # the cut says so
        self.assertIn(FI._truncated_mark(), ("…(truncated)", "…(잘림)"))
        self.assertEqual(FI.validate_answers(interview, FI.answers_from_native(interview, asked)), [])

    def test_native_slots_and_answers_from_native_agree_with_the_single_call_rules(self):
        interview = good_interview()
        first, second = interview["questions"][0], interview["questions"][1]
        restate = lambda answer: {"question": interview["understanding"], "options": ["yes", "no"], "answer": answer}
        row = lambda question, answer: {"question": question["question"], "options": ["Yes", "No"], "answer": answer}
        cases = {
            "confirmed": [restate("yes"), row(first, "Yes"), row(second, "No")],
            "corrected": [restate("Only the approval step."), row(first, "Yes"), row(second, "No")],
            "off-menu": [restate("yes"), row(first, "Something else entirely"), row(second, "No")],
            "missing question": [restate("yes"), row(first, "Yes")],
            "missing restatement": [row(first, "Yes"), row(second, "No")],
            "no without a correction": [restate("no"), row(first, "Yes"), row(second, "No")],
            "empty answer": [restate("yes"), row(first, ""), row(second, "No")],
            "unrelated": [{"question": "unrelated", "answer": "x"}],
        }
        for name, asked in cases.items():
            with self.subTest(name):
                slots = FI.native_slots(interview, asked)
                expected = None if slots is None else FI.answers_from_native_slots(interview, slots)
                self.assertEqual(FI.answers_from_native(interview, asked), expected)
        self.assertIsNotNone(FI.answers_from_native(interview, cases["confirmed"]))
        for rejected in ("no without a correction", "empty answer"):
            self.assertIsNone(FI.native_slots(interview, cases[rejected]), rejected)
        self.assertEqual(sorted(FI.native_slots(interview, cases["missing question"])),
                         sorted([FI.UNDERSTANDING_SLOT, first["id"]]))
        self.assertEqual(FI.missing_native_slots(interview, FI.native_slots(interview, cases["missing question"])),
                         [second["id"]])
        merged = {**FI.native_slots(interview, cases["missing question"]),
                  **FI.native_slots(interview, [row(second, "No")])}
        self.assertEqual(FI.answers_from_native_slots(interview, merged), FI.answers_from_native(interview, cases["confirmed"]))


class IntentTest(unittest.TestCase):
    def test_nonuser_intent_and_approvals_keep_the_declared_source(self):
        interview = good_interview()
        interview["questions"][0]["options"][0]["approves"] = True
        proposal = {"entry_approvals": [{"key": "full-run", "leg": 0, "question": "q-scope"}]}
        for kind in ("automatic", "headless-owner", "unknown"):
            with self.subTest(kind=kind):
                answers = good_answers(interview, actor_kind=kind, understanding_confirmed=False, correction="Limit scope.")
                answers["answers"]["q-cap"].update(choice=1, note="Three is enough.")
                text = FI.render_intent(interview, answers, now="2026-10-05")
                self.assertIn("status: recorded\nactor_kind: " + kind, text)
                self.assertIn(f"**{kind.capitalize()}'s correction:** Limit scope.", text)
                self.assertIn(kind.capitalize() + "'s note: Three is enough.", text)
                self.assertIn("(" + kind + " choice)", text)
                self.assertNotIn("User's", text)
                self.assertNotIn("user's own choice", text)
                self.assertFalse(FI.approvals_given(interview, answers, proposal)[0]["accepted"])
        self.assertTrue(FI.approvals_given(interview, good_answers(interview), proposal)[0]["accepted"])

    def test_a_supervisor_answer_is_agreed_on_the_persons_behalf_and_holds_person_only_starts(self):
        """User decision 2026-10-07: a supervisor may answer frame-review for the person, under its own name."""
        interview = good_interview()
        interview["questions"][0]["options"][0]["approves"] = True
        answers = good_answers(interview, actor_kind="supervisor", understanding_confirmed=False, correction="Limit scope.")
        answers["answers"]["q-cap"]["choice"] = 1
        text = FI.render_intent(interview, answers, now="2026-10-07")
        self.assertIn("status: agreed-on-behalf-with-correction\nactor_kind: supervisor", text)
        self.assertIn("## Confirmed understanding (on the person's behalf)", text)
        self.assertIn("**Supervisor's correction:** Limit scope.", text)
        self.assertIn("(supervisor choice)", text)
        self.assertIn("status: agreed-on-behalf\n", FI.render_intent(interview, good_answers(interview, actor_kind="supervisor")))
        for key in ("full-run", "deploy", "handback", "preview"):
            with self.subTest(key=key):
                proposal = {"entry_approvals": [{"key": key, "leg": 0, "question": "q-scope"}]}
                row, = FI.approvals_given(interview, answers, proposal)
                person_only = key in FI.PERSON_ONLY_START_APPROVALS
                self.assertEqual((row["accepted"], row.get("held_for_person", False)), (not person_only, person_only))
                self.assertTrue(FI.approvals_given(interview, good_answers(interview), proposal)[0]["accepted"])
                declined = good_answers(interview, actor_kind="supervisor")
                declined["answers"]["q-scope"]["choice"] = 1
                self.assertEqual(FI.approvals_given(interview, declined, proposal)[0],
                                 {"key": key, "leg": 0, "question": "q-scope", "label": "Approval step only",
                                  "accepted": False})
        self.assertEqual(FI.PERSON_ONLY_START_APPROVALS, ("deploy", "handback", "preview"))
        self.assertTrue(FI.answer_releases_gate("supervisor", "frame-review"))
        self.assertTrue(FI.answer_releases_gate("user", "preview-disposition"))
        for kind, gate in (("supervisor", "preview-disposition"), ("supervisor", "plan-approval"),
                           ("automatic", "frame-review"), ("unknown", "frame-review"), ("headless-owner", "frame-review")):
            with self.subTest(kind=kind, gate=gate):
                self.assertFalse(FI.answer_releases_gate(kind, gate))

    def test_legacy_recorded_actor_is_read_context_only(self):
        answers = good_answers(good_interview())
        del answers["actor_kind"]
        original = copy.deepcopy(answers)
        context = FI.recorded_answer_context(answers, "user")
        self.assertEqual(context["actor_kind"], "user")
        self.assertEqual(answers, original)
        self.assertEqual(FI.recorded_answer_context(answers, None)["actor_kind"], "unknown")
        explicit = {**answers, "actor_kind": "supervisor"}
        self.assertIs(FI.recorded_answer_context(explicit, "user"), explicit)

    def test_intent_carries_every_decision_and_the_brief(self):
        interview = good_interview()
        answers = good_answers(interview)
        answers["answers"]["q-cap"]["choice"] = 1
        answers["answers"]["q-cap"]["note"] = "Three is plenty."
        text = FI.render_intent(interview, answers, now="2026-09-06")
        self.assertIn("status: agreed\n", text)
        self.assertIn("## Problem", text)
        self.assertIn("## Proposed Outcome", text)
        self.assertIn("## Decisions", text)
        self.assertIn("**Both (recommended)** (recommended)", text)
        self.assertIn("**No, fewer** (user's own choice)", text)
        self.assertIn("User's note: Three is plenty.", text)
        self.assertIn("## Open Questions", text)
        self.assertIn("None recorded.", text)

    def test_a_correction_is_kept_in_the_users_words(self):
        interview = good_interview()
        answers = good_answers(interview, understanding_confirmed=False,
                               correction="Only the approval step, please.")
        text = FI.render_intent(interview, answers, now="2026-09-06")
        self.assertIn("status: agreed-with-correction", text)
        self.assertIn("**User's correction:** Only the approval step, please.", text)

    def test_no_questions_still_produces_an_intent(self):
        interview = good_interview(questions=[])
        answers = good_answers(interview)
        text = FI.render_intent(interview, answers, now="2026-09-06")
        self.assertIn("No question needed a decision", text)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.interview = self.base / "interview.json"
        self.interview.write_text(json.dumps(good_interview()), encoding="utf-8")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = FI.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_validate_and_template_and_render(self):
        code, out, _ = self.run_cli("validate", "--interview", str(self.interview), "--intensity", "standard")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["valid"])
        code, out, _ = self.run_cli("validate", "--interview", str(self.interview), "--intensity", "direct")
        self.assertEqual(code, 65)
        code, out, _ = self.run_cli("answers-template", "--interview", str(self.interview))
        template = json.loads(out)
        template["understanding_confirmed"] = True
        for qid in template["answers"]:
            template["answers"][qid]["choice"] = 0
        answers = self.base / "answers.json"
        answers.write_text(json.dumps(template), encoding="utf-8")
        code, out, _ = self.run_cli("validate-answers", "--interview", str(self.interview), "--answers", str(answers))
        self.assertEqual(code, 0)
        intent = self.base / "shards" / "frame" / "intent.md"
        code, out, _ = self.run_cli("render-intent", "--interview", str(self.interview),
                                    "--answers", str(answers), "--out", str(intent))
        self.assertEqual(code, 0)
        rendered = intent.read_text(encoding="utf-8")
        self.assertIn("# Intent", rendered)
        self.assertIn(f"- interview: {self.interview.resolve()}", rendered)   # minor 2
        self.assertFalse(intent.with_name(intent.name + ".tmp").exists())

    def test_render_refuses_incomplete_answers(self):
        answers = self.base / "answers.json"
        answers.write_text(json.dumps(FI.answers_template(good_interview())), encoding="utf-8")
        code, _out, err = self.run_cli("render-intent", "--interview", str(self.interview),
                                       "--answers", str(answers), "--out", str(self.base / "intent.md"))
        self.assertEqual(code, 65)
        self.assertIn("understanding_confirmed", err)
        self.assertFalse((self.base / "intent.md").exists())


LANDING_SCOPE = {
    "id": "landing-scope", "topic": "이번에 어디까지", "kind": "choice",
    "question": "이번에는 코드 작업에만 적용하고, 문서나 화면 만드는 일은 다음에 옮길까요?",
    "options": [
        {"label": "코드부터 먼저",
         "means": "코드 작업에서 먼저 검증하고, 나머지 종류는 다음 일로 넘깁니다."},
        {"label": "한 번에 전부",
         "means": "모든 종류에 동시에 적용합니다. 이번 일이 훨씬 커지고 위험도 커집니다."},
    ],
    "recommended": 0,
    "why": "한 번에 끝내는 것과 안전하게 나눠 가는 것 중 어느 쪽이 급하신지는 사용자 일정에 달렸습니다.",
}
# The user's actual words on 2026-09-10, shortened to the substance the ledger
# lost. Neither printed option was the answer.
LANDING_SCOPE_NOTE = (
    "선택지 둘 다 아님. 사용자가 고른 범위는 '문서·화면·요구사항까지' — 방향 게이트가 이미 "
    "선언된 다섯 종류(code·draft·refine·design·spec)만 이번에 옮긴다. 선언만 있던 게이트 "
    "4개를 실제로 동작하게 만드는 일이 범위에 포함된다. 게이트가 없는 조사·분석2·감사·"
    "실험(eval)은 이번 범위 밖. 승인류 3개는 손대지 않는다. quick 하한·direct 제외는 그대로."
)


class OffMenuAnswerTest(unittest.TestCase):
    """2026-09-10 regression: this cycle's own `landing-scope` answer was
    off-menu, the schema had no way to say so, and `intent.md` recorded
    `**한 번에 전부** (user's own choice)` -- a decision the user never made."""

    def interview(self):
        return good_interview(questions=[LANDING_SCOPE])

    def answers(self, choice=FI.NONE_SENTINEL, note=LANDING_SCOPE_NOTE):
        interview = self.interview()
        answers = FI.answers_template(interview)
        answers["understanding_confirmed"] = True
        answers["answers"]["landing-scope"] = {"choice": choice, "note": note}
        return interview, answers

    def test_the_false_record_cannot_happen_again(self):
        interview, answers = self.answers()
        self.assertEqual(FI.validate_answers(interview, answers), [])
        text = FI.render_intent(interview, answers, now="2026-09-10")
        self.assertNotIn("한 번에 전부", text)
        self.assertNotIn("user's own choice", text)
        self.assertNotIn("(recommended)", text.split("## Decisions", 1)[1])
        self.assertIn("**제시된 선택지 없음** (off-menu)", text)
        self.assertIn("선택지 둘 다 아님", text)

    def test_an_off_menu_answer_needs_its_note(self):
        interview, answers = self.answers(note="   ")
        errors = FI.validate_answers(interview, answers)
        self.assertEqual(
            errors, ["answers.landing-scope.note: required when no printed option applies"])

    def test_the_off_menu_note_is_roomier_but_still_capped(self):
        interview, answers = self.answers(note="가" * FI.MAX_OFFMENU_NOTE_CHARS)
        self.assertEqual(FI.validate_answers(interview, answers), [])
        self.assertGreater(FI.MAX_OFFMENU_NOTE_CHARS, FI.MAX_NOTE_CHARS)
        interview, answers = self.answers(note="가" * (FI.MAX_OFFMENU_NOTE_CHARS + 1))
        self.assertTrue(any(f"> {FI.MAX_OFFMENU_NOTE_CHARS}" in e
                            for e in FI.validate_answers(interview, answers)))
        # The ordinary cap is untouched for an on-menu answer.
        interview, answers = self.answers(choice=0, note="가" * (FI.MAX_NOTE_CHARS + 1))
        self.assertTrue(any(f"> {FI.MAX_NOTE_CHARS}" in e
                            for e in FI.validate_answers(interview, answers)))

    def test_a_none_label_wins_and_the_sentinel_is_refused(self):
        """One value never means two things: where `none` is a real label,
        index-conversion wins and the sentinel reading is a typed error."""
        question = copy.deepcopy(LANDING_SCOPE)
        question["options"][1]["label"] = "none"
        interview = good_interview(questions=[question])
        answers = FI.answers_template(interview)
        answers["understanding_confirmed"] = True
        answers["answers"]["landing-scope"] = {"choice": "none", "note": ""}
        errors = FI.validate_answers(interview, answers)
        self.assertEqual(
            errors, ["answers.landing-scope.choice: option label collides with the none sentinel"])
        self.assertEqual(answers["answers"]["landing-scope"]["choice"], 1)

    def test_unanswered_and_off_menu_stay_two_different_values(self):
        interview, answers = self.answers(choice=None)
        errors = FI.validate_answers(interview, answers)
        self.assertTrue(any("must index" in e for e in errors), errors)
        text = FI.render_intent(interview, answers, now="2026-09-10")
        self.assertIn("Decision: unanswered", text)
        self.assertNotIn("off-menu", text)
        # ... and the template's default is still the unanswered one.
        self.assertIsNone(FI.answers_template(self.interview())["answers"]["landing-scope"]["choice"])

    def test_answers_within_every_field_cap_always_fit_the_payload_cap(self):
        """The payload ceiling is derived from the per-field caps, so answering
        every field within its own cap can never be refused as a whole. It used
        to be a free-standing 8 KiB beside caps counted in characters: in
        Korean (3 bytes a character) three maximal off-menu answers broke it."""
        questions = [dict(copy.deepcopy(LANDING_SCOPE), id=f"q-{i}", topic=f"주제 {i}")
                     for i in range(FI.QUESTION_CAP["standard"])]
        interview = good_interview(questions=questions)
        answers = FI.answers_template(interview)
        answers["understanding_confirmed"] = True
        for qid in answers["answers"]:
            answers["answers"][qid] = {"choice": FI.NONE_SENTINEL,
                                       "note": "가" * FI.MAX_OFFMENU_NOTE_CHARS}
        answers["correction"] = "가" * FI.MAX_CORRECTION_CHARS
        size = len(json.dumps(answers, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(size, FI.MAX_ANSWERS_BYTES)
        self.assertFalse(any("bytes >" in e
                             for e in FI.validate_answers(interview, answers)))


LEG = {"capability": "autopilot-lab", "mode": "setup", "shape": "staged",
       "graph": ["scaffold", "smoke", "full-run", "run-verify", "handoff"], "intensity": None}


def routed_interview(**overrides):
    """An interview with a route question (q-scope) and an approval question (q-cap)."""
    interview = good_interview()
    proposal = {"summary": "Set it up and run it", "legs": [LEG],
                "entry_approvals": [{"key": "full-run", "leg": 0, "question": "q-cap"}]}
    interview["route_proposals"] = {"question": "q-scope", "by_option": {"Both (recommended)": proposal}}
    interview["questions"][1]["options"][0]["approves"] = True
    interview.update(overrides)
    return interview


class EntryExecutionScopeTest(unittest.TestCase):
    def test_existing_route_question_can_also_carry_the_entry_approval(self):
        interview = routed_interview()
        proposal = interview["route_proposals"]["by_option"]["Both (recommended)"]
        proposal["entry_approvals"][0]["question"] = "q-scope"
        proposal["execution_scope"] = "report"
        errors = FI.validate(interview, intensity="standard")
        self.assertEqual(errors, [])
        self.assertEqual(len(interview["questions"]), 2)

    def test_existing_choice_can_offer_same_legs_as_complete_or_report_without_a_second_question(self):
        interview = routed_interview()
        complete = {"summary": "Prepare the full run", "legs": [LEG], "execution_scope": "complete",
                    "entry_approvals": [{"key": "full-run", "leg": 0, "question": "q-scope"}]}
        report = {"summary": "Prepare and report smoke", "legs": [LEG], "execution_scope": "report",
                  "entry_approvals": [{"key": "full-run", "leg": 0, "question": "q-scope"}]}
        interview["questions"][0]["options"] = [
            {"label": "Full run", "means": "Prepare all approved work."},
            {"label": "Smoke report", "means": "Stop after the smoke result."},
            {"label": "No", "means": "Do not begin."},
        ]
        interview["route_proposals"]["by_option"] = {"Full run": complete, "Smoke report": report}
        self.assertEqual(FI.validate(interview, intensity="standard"), [])
        self.assertEqual(len(interview["questions"]), 2)
        answers = good_answers(interview)
        answers["answers"]["q-scope"]["choice"] = 0
        self.assertEqual(FI.route_choice(interview, answers)["execution_scope"], "complete")
        answers["answers"]["q-scope"]["choice"] = 1
        self.assertEqual(FI.route_choice(interview, answers)["execution_scope"], "report")
        self.assertTrue(FI.approvals_given(interview, answers, report)[0]["accepted"])

    def test_report_scope_is_a_distinct_normal_selection_and_invalid_or_unanswered_is_not(self):
        interview = routed_interview()
        proposal = interview["route_proposals"]["by_option"]["Both (recommended)"]
        proposal["entry_approvals"][0]["question"] = "q-scope"
        proposal["execution_scope"] = "report"
        answers = good_answers(interview)
        answers["answers"]["q-scope"]["choice"] = "Both (recommended)"
        chosen = FI.route_choice(interview, answers)
        self.assertEqual(chosen["state"], "selected")
        self.assertEqual(chosen["execution_scope"], "report")
        self.assertTrue(FI.approvals_given(interview, answers, proposal)[0]["accepted"])
        for choice, state in ((None, "unanswered"), (FI.NONE_SENTINEL, "off-menu"), (99, "unanswered")):
            answers["answers"]["q-scope"]["choice"] = choice
            self.assertEqual(FI.route_choice(interview, answers)["state"], state)


class RouteProposalsTest(unittest.TestCase):
    """The optional `route_proposals` field: reference checks only, and no change without it."""

    def test_a_field_whose_references_exist_is_valid(self):
        self.assertEqual(FI.validate(routed_interview(), intensity="standard"), [])

    def test_the_references_are_checked(self):
        cases = {
            "not an object": ("route_proposals", "x", "expected exactly"),
            "extra key": ("route_proposals", {"question": "q-scope", "by_option": {}, "x": 1}, "expected exactly"),
            "no such question": ("route_proposals", {"question": "nope", "by_option": {"a": {}}}, "no such question"),
            "empty mapping": ("route_proposals", {"question": "q-scope", "by_option": {}}, "at least one"),
        }
        for label, (key, value, text) in cases.items():
            with self.subTest(label):
                errors = FI.validate(routed_interview(**{key: value}))
                self.assertTrue(any(text in e for e in errors), errors)
        label_missing = routed_interview()
        label_missing["route_proposals"]["by_option"] = {"Not a label": {"legs": [LEG]}}
        self.assertTrue(any("not an option of question" in e for e in FI.validate(label_missing)))

    def test_a_label_that_two_options_share_is_ambiguous(self):
        interview = routed_interview()
        interview["questions"][0]["options"][1]["label"] = "Both (recommended)"
        self.assertTrue(any("ambiguous" in e for e in FI.validate(interview)))

    def test_approval_references_need_a_yes_no_question_inside_the_proposals_legs(self):
        interview = routed_interview()
        approval = interview["route_proposals"]["by_option"]["Both (recommended)"]["entry_approvals"]
        approval[0]["question"] = "q-none"
        self.assertTrue(any("no such approval question" in e for e in FI.validate(interview)))
        approval[0]["question"] = "q-scope"                      # same existing route question is allowed
        self.assertFalse(any("no such approval question" in e for e in FI.validate(interview)))
        interview = routed_interview()
        interview["questions"][1]["kind"] = "choice"
        self.assertTrue(any("an approval question is yes-no" in e for e in FI.validate(interview)))
        interview = routed_interview()
        interview["route_proposals"]["by_option"]["Both (recommended)"]["entry_approvals"][0]["leg"] = 3
        self.assertTrue(any("outside the proposal's legs" in e for e in FI.validate(interview)))
        for bad in ({"key": "full-run"}, {"key": "full-run", "leg": 0, "question": "q-cap", "x": 1}):
            interview = routed_interview()
            interview["route_proposals"]["by_option"]["Both (recommended)"]["entry_approvals"] = [bad]
            self.assertTrue(any("expected exactly" in e for e in FI.validate(interview)))

    def test_an_approval_question_marks_exactly_one_option_as_approving(self):
        for label, mark in (("none marked", lambda o: None),
                            ("both marked", lambda o: [x.update(approves=True) for x in o]),
                            ("non-boolean", lambda o: o[0].update(approves="yes")),
                            ("non-boolean on the other option", lambda o: o[1].update(approves=1))):
            with self.subTest(label):
                interview = routed_interview()
                options = interview["questions"][1]["options"]
                del options[0]["approves"]
                mark(options)
                errors = FI.validate(interview)
                self.assertTrue(any("q-cap" in e and "approves" in e for e in errors), errors)

    def test_the_approving_option_may_come_second_and_the_declining_one_first(self):
        interview = routed_interview()
        options = interview["questions"][1]["options"]
        options.reverse()                                           # "No, fewer" first, the marked "Yes" second
        self.assertEqual(FI.validate(interview, intensity="standard"), [])
        answers = good_answers(interview)
        proposal = FI.route_choice(interview, answers)["proposal"]
        answers["answers"]["q-cap"]["choice"] = 0                   # the declining option, now first
        row = FI.approvals_given(interview, answers, proposal)[0]
        self.assertEqual((row["label"], row["accepted"]), ("No, fewer", False))
        answers["answers"]["q-cap"]["choice"] = 1                   # the approving option, now second
        row = FI.approvals_given(interview, answers, proposal)[0]
        self.assertEqual((row["label"], row["accepted"]), ("Yes (recommended)", True))
        answers["answers"]["q-cap"]["choice"] = "Yes (recommended)"
        self.assertTrue(FI.approvals_given(interview, answers, proposal)[0]["accepted"])
        answers["answers"]["q-cap"]["choice"] = None
        self.assertFalse(FI.approvals_given(interview, answers, proposal)[0]["accepted"])

    def test_approves_is_not_required_outside_approval_questions(self):
        interview = routed_interview()
        self.assertNotIn("approves", interview["questions"][0]["options"][0])      # the route question
        self.assertEqual(FI.validate(interview), [])
        plain = good_interview()                                    # no route_proposals at all
        self.assertEqual(FI.validate(plain), [])
        self.assertFalse(any("approves" in json.dumps(q) for q in plain["questions"]))

    def test_proposal_metadata_is_not_scanned_for_harness_words_but_the_questions_still_are(self):
        interview = routed_interview()
        self.assertEqual(FI.validate(interview), [])              # capability, graph and start_approval are data
        self.assertIn("full-run", json.dumps(interview["route_proposals"]))
        interview["questions"][0]["question"] = "Should the owner re-raise the gate?"
        self.assertEqual(FI.validate(interview), [])

    def test_declining_either_answer_is_a_valid_answer(self):
        interview = routed_interview()
        for route_choice, approval_choice in ((0, 0), (1, 0), (0, 1), (1, 1)):
            answers = good_answers(interview)
            answers["answers"]["q-scope"]["choice"] = route_choice
            answers["answers"]["q-cap"]["choice"] = approval_choice
            self.assertEqual(FI.validate_answers(interview, answers), [], (route_choice, approval_choice))
        off = good_answers(interview)
        off["answers"]["q-scope"] = {"choice": "none", "note": "Something else, please."}
        self.assertEqual(FI.validate_answers(interview, off), [])
        unanswered = good_answers(interview)
        unanswered["answers"]["q-cap"]["choice"] = None
        self.assertTrue(FI.validate_answers(interview, unanswered))

    def test_route_choice_and_approvals_read_the_answers_the_way_the_runtime_needs(self):
        interview = routed_interview()
        answers = good_answers(interview)
        chosen = FI.route_choice(interview, answers)
        self.assertEqual((chosen["state"], chosen["label"]), ("selected", "Both (recommended)"))
        self.assertEqual(chosen["proposal"]["legs"][0]["capability"], "autopilot-lab")
        self.assertEqual(FI.approvals_given(interview, answers, chosen["proposal"]),
                         [{"key": "full-run", "leg": 0, "question": "q-cap", "label": "Yes (recommended)", "accepted": True}])
        answers["answers"]["q-cap"]["choice"] = "No, fewer"
        self.assertFalse(FI.approvals_given(interview, answers, chosen["proposal"])[0]["accepted"])
        answers["answers"]["q-scope"]["choice"] = 1
        self.assertEqual(FI.route_choice(interview, answers)["state"], "declined")
        answers["answers"]["q-scope"] = {"choice": "none", "note": "x"}
        self.assertEqual(FI.route_choice(interview, answers)["state"], "off-menu")
        self.assertIsNone(FI.route_choice(good_interview(), good_answers(good_interview())))

    def test_intent_records_the_route_and_the_approvals_only_when_the_field_is_present(self):
        interview = routed_interview()
        answers = good_answers(interview)
        scope = {("full-run", 0): ["autopilot-lab:full-run"]}
        text = FI.render_intent(interview, answers, now="2026-10-01", approval_scope=scope)
        self.assertIn("## Route\n\nSelected route: Set it up and run it", text)
        self.assertIn("- Leg 0: autopilot-lab / setup / staged / scaffold,smoke,full-run,run-verify,handoff  (starts now)", text)
        self.assertIn("- full-run for leg 0 (autopilot-lab:full-run) — question `q-cap`: approved", text)
        plain = copy.deepcopy(interview)
        del plain["route_proposals"]
        self.assertNotIn("## Route", FI.render_intent(plain, answers, now="2026-10-01", approval_scope=scope))
        self.assertEqual(FI.render_intent(plain, answers, now="2026-10-01"),
                         FI.render_intent(plain, answers, now="2026-10-01", approval_scope=scope))
        # the person's correction still comes first, and is never widened into an approval
        answers["understanding_confirmed"] = False
        answers["correction"] = "Not quite: only the setup."
        corrected = FI.render_intent(interview, answers, now="2026-10-01", approval_scope=scope)
        self.assertLess(corrected.index("User's correction"), corrected.index("## Route"))
        answers["answers"]["q-cap"]["choice"] = 1
        self.assertIn("question `q-cap`: declined", FI.render_intent(interview, answers, now="2026-10-01", approval_scope=scope))

    def test_without_the_field_everything_is_byte_identical(self):
        interview = good_interview()
        answers = good_answers(interview)
        self.assertEqual(FI.validate(interview), [])
        self.assertEqual(FI.validate_answers(interview, answers), [])
        self.assertEqual(json.dumps(FI.answers_template(interview), sort_keys=True),
                         json.dumps(FI.answers_template({**interview}), sort_keys=True))
        self.assertNotIn("route", FI.render_intent(interview, answers, now="2026-10-01").lower().split("## decisions")[1].split("## open")[0])
        self.assertEqual(FI.render_intent(interview, answers, now="2026-10-01"),
                         FI.render_intent(interview, answers, now="2026-10-01", approval_scope={}))

class DraftTest(unittest.TestCase):
    def test_a_brief_draft_takes_each_numbered_sections_first_paragraph_within_the_field_length(self):
        text = ("---\nstatus: framed\n---\n\n## 1. 문제\n\n첫 문단이다. " + "긴 설명. " * 200 + "\n\n둘째 문단\n\n"
                "### 4) Verdict\n\nKeep the guard.\n\n1. a numbered item stays inside\n\n## 8. Route\n\nx\n")
        draft = FI.brief_draft(text)
        self.assertTrue(draft["problem"].startswith("첫 문단이다."))
        self.assertLessEqual(len(draft["problem"]), FI.MAX_BRIEF_FIELD_CHARS)
        self.assertEqual(draft["outcome"], "Keep the guard.")
        self.assertEqual((draft["affected"], draft["constraints"], draft["open"]), ("", "", ""))
        self.assertEqual(FI.brief_draft("no sections"), {field: "" for field in FI.BRIEF_FIELDS})

    def test_an_understanding_draft_is_the_requests_first_sentence(self):
        self.assertEqual(FI.understanding_draft("Fix the leak. Then report."), "Fix the leak.")
        self.assertEqual(FI.understanding_draft("첫 줄\n둘째 줄"), "첫 줄")
        self.assertLessEqual(len(FI.understanding_draft("word " * 200)), FI.MAX_UNDERSTANDING_CHARS)
        self.assertEqual(FI.understanding_draft(""), "")


if __name__ == "__main__":
    unittest.main()
