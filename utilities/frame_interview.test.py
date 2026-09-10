#!/usr/bin/env python3
"""SD-129: the interview a person answers at the frame gate must be answerable
by a tired reader, and its answers must become the intent plan reads."""
from __future__ import annotations

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
    answers["understanding_confirmed"] = True
    for qid in answers["answers"]:
        answers["answers"][qid]["choice"] = 0
    answers.update(overrides)
    return answers


class ValidateTest(unittest.TestCase):
    def test_a_plain_interview_is_valid(self):
        self.assertEqual(FI.validate(good_interview(), intensity="standard"), [])

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

    def test_harness_words_are_refused_wherever_a_person_reads(self):
        for field, text in (
            ("question", "Should the owner re-raise the gate on route rt-da62cded?"),
            ("why", "The dispatch depth is fixed by the harness."),
        ):
            with self.subTest(field=field):
                bad = good_interview()
                bad["questions"][0][field] = text
                errors = FI.validate(bad)
                self.assertTrue(errors, field)
                self.assertTrue(all("harness word" in e for e in errors), errors)
        bad = good_interview()
        bad["questions"][0]["options"][0]["means"] = "Spawn the plan node as a worker."
        self.assertTrue(any("harness word" in e for e in FI.validate(bad)))
        bad = good_interview(understanding="The frame-review gate blocks the plan node.")
        self.assertTrue(any("harness word" in e for e in FI.validate(bad)))
        bad = good_interview()
        bad["brief"]["problem"] = "The 워커 dies at the 게이트."
        self.assertEqual(len([e for e in FI.validate(bad) if "harness word" in e]), 2)

    def test_korean_particles_and_identifiers_are_caught(self):
        """review round 1, M4: Korean is agglutinative and identifiers carry `_`/`-`."""
        for text in ("라우트를 바꿀까요?", "게이트가 열리면 진행할까요?", "오너에게 맡길까요?",
                     "Keep route_id in the log?", "Should the owner-side wording stay?",
                     "Should the sub-node run first?", "Is a route-level change fine?"):
            with self.subTest(text=text):
                self.assertTrue(FI.jargon_hits(text), text)
        bad = good_interview()
        bad["questions"][0]["question"] = "게이트를 지금 열까요, 나중에 열까요?"
        self.assertTrue(any("harness word" in e for e in FI.validate(bad)))
        # review round 2, N2: ordinary Korean words that contain a term are not hits
        for text in ("가격이 훅 오를까요?", "마커펜을 쓸까요?", "게이트볼을 할까요?", "노드 대신 마디라고 부를까요?"):
            with self.subTest(text=text):
                self.assertEqual([h for h in FI.jargon_hits(text) if h not in ("노드",)], [], text)
        self.assertEqual(FI.jargon_hits("노드 대신 마디라고 부를까요?"), ["노드"])

    def test_abbreviations_are_not_sentence_ends_and_two_questions_are_caught(self):
        ok = good_interview(understanding="You want approval in seconds, e.g. under five, with short questions.")
        self.assertEqual(FI.validate(ok), [])
        bad = good_interview()
        bad["questions"][0]["question"] = "Fix wording? Change schedule?"
        self.assertTrue(any("two things" in e for e in FI.validate(bad)))
        bad["questions"][0]["question"] = "Should we fix the wording and also change the schedule?"
        self.assertTrue(any("two things" in e for e in FI.validate(bad)))
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
        self.assertFalse(any(line.strip() == "---" for line in text.splitlines()[7:]))
        self.assertIn("아니, 승인만 고쳐. --- ## Decisions - **fake**", text)

    def test_ordinary_words_that_contain_a_harness_word_pass(self):
        ok = good_interview()
        ok["questions"][0]["question"] = "Should the gateway keep the same address, or move to the new one?"
        ok["questions"][0]["topic"] = "Gateway address"
        self.assertEqual(FI.validate(ok), [])

    def test_long_or_double_questions_are_refused(self):
        bad = good_interview()
        bad["questions"][0]["question"] = "x" * 161
        self.assertTrue(any("> 160" in e for e in FI.validate(bad)))
        bad["questions"][0]["question"] = "One. Two. Three?"
        self.assertTrue(any("sentences" in e for e in FI.validate(bad)))
        bad["questions"][0]["question"] = "Keep the old name? and should we also move the files?"
        self.assertTrue(any("two things" in e for e in FI.validate(bad)))

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
        self.assertTrue(any("one sentence" in e for e in FI.validate(bad)))

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


class IntentTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
