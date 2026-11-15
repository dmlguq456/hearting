"""The native-like block prioritizes exact questions without consuming native keys."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from tools.fleet import session_line
from tools.fleet.explorer.grid import cell_width


class SessionBlockTest(unittest.TestCase):
    def test_choices_wrap_within_each_choice_and_do_not_reserve_a_source_row(self):
        from tools.fleet.session_block import question_presentations
        choices = ['A long first choice that needs several lines', 'Second choice']
        item = {'id': 'exact', 'answerable': True, 'age': '0m',
                'questions': [{'id': 'q', 'question': 'Choose', 'choices': choices}]}
        question_presentations([item], 30)
        display = item['questions'][0]['display']
        lines = [''.join(text for text, _ in line) for line in display['options']]
        self.assertTrue(lines[0].startswith('1 '))
        second = next(index for index, line in enumerate(lines) if line.startswith('2 '))
        self.assertGreater(second, 1)
        self.assertTrue(all(line.startswith('  ') for line in lines[1:second]))
        # Two cells stay free for the editor's mark column (spec E9 rule 53).
        self.assertTrue(all(cell_width(line) <= 24 for line in lines))
        self.assertEqual(display['source'], [])
        self.assertEqual(item['questions'][0]['choices'], choices)

    def test_box_heads_count_one_to_n_after_a_question_with_choices(self):
        # 11-14 night: the choices' own numbering overwrote the box number, so the boxes after a
        # four-choice question read `QUESTIONS 3 · 5/3`, `6/3`. Each head reads `i/N`, 1 to N.
        from tools.fleet.session_block import question_presentations
        item = {'id': 'many', 'answerable': True, 'age': '0m', 'questions': [
            {'id': 'a', 'question': 'Pick one', 'choices': ['one', 'two', 'three', 'four'], 'recommended': 1},
            {'id': 'b', 'question': 'Then this'}, {'id': 'c', 'question': 'And this', 'choices': ['yes', 'no']}]}
        question_presentations([item], 80)
        heads = [''.join(text for text, _ in question['display']['head']) for question in item['questions']]
        self.assertEqual([head.split(' · ')[1] for head in heads], ['1/3', '2/3', '3/3'], heads)
        # The choices keep their own numbers and the recommended mark.
        options = [''.join(text for text, _ in line) for line in item['questions'][0]['display']['options']]
        self.assertEqual([line.split(' ')[0] for line in options], ['1', '2', '3', '4'])
        self.assertIn('2 two (', options[1])

    def test_question_body_preserves_decision_lines_at_wide_and_narrow_widths(self):
        from tools.fleet.session_block import question_presentations
        text = "[결정 필요] 로그 보관 기간\n- 30일: 기존 설정 유지\n- 90일: 저장 공간 3배\n추천: 30일 — 되돌리기 쉬움\n답이 없으면: 보류"
        for width in (100, 48):
            item = {"answerable": True, "questions": [{"question": text,
                    "choices": ["켜기", "끄기"], "recommended": 1}]}
            question_presentations([item], width)
            body = ["".join(part for part, _ in line) for line in item["questions"][0]["display"]["body"]]
            self.assertGreaterEqual(len(body), 5)
            self.assertTrue(body[0].startswith("[결정 필요]"))
            self.assertTrue(any(line.startswith("- 30일") for line in body))
            self.assertTrue(any(line.startswith("- 90일") for line in body))
            self.assertTrue(any(line.startswith("추천:") for line in body))
            options = ["".join(part for part, _ in line) for line in item["questions"][0]["display"]["options"]]
            from tools.fleet.config import ui_text
            self.assertIn("(" + ui_text("fleet.session_block.recommended") + ")", "\n".join(options))

    def test_a_wait_not_answerable_here_says_where_to_answer_by_its_reason(self):
        # Spec E9 rule 54: no "answer unavailable"; the reason picks one of five phrases. In this
        # session's own panel the window to answer in is below; from Fleet or another session it
        # is that session's window. An unknown reason falls to that window too.
        import json
        from pathlib import Path
        from tools.fleet.session_block import question_presentations, question_state, unanswerable_state
        from tools.fleet.config import ui_text
        cases = [
            ("marker-has-no-bound-native-response-surface", "answer_below", "answer_there"),
            ("no exact owner session binding", "answer_below", "answer_there"),
            ("codex-native-request-kind-unsupported", "answer_below", "answer_there"),
            ("claude-native-response-surface-unavailable", "answer_below", "answer_there"),
            ("owner-correction-lineage-unverified", "answer_parent", "answer_parent"),
            ("owner-correction-native-parent-context-required", "answer_parent", "answer_parent"),
            ("owner-correction-surface-unavailable:OSError", "answer_parent", "answer_parent"),
            ("owner-correction-already-pending", "answer_sending", "answer_sending"),
            ("action-pending", "answer_sending", "answer_sending"),
            ("codex-native-request-not-current", "answer_closed", "answer_closed"),
            ("selected-marker-no-longer-current", "answer_closed", "answer_closed"),
            ("message-question-no-longer-current", "answer_closed", "answer_closed"),
            ("message-session-incarnation-changed", "answer_closed", "answer_closed"),
            ("opencode-form-not-pending", "answer_closed", "answer_closed"),
            ("opencode-form-cancelled", "answer_closed", "answer_closed"),
            ("a-reason-nobody-named-yet", "answer_below", "answer_there"),
            (None, "answer_below", "answer_there"),
        ]
        for reason, own, other in cases:
            self.assertEqual(unanswerable_state(reason), own, reason)
            self.assertEqual(unanswerable_state(reason, own=False), other, reason)
            item = {"answerable": False, **({"reason": reason} if reason else {})}
            self.assertEqual(question_state(item), own, reason)
            self.assertEqual(question_state(item, own=False), other, reason)
        # A request state keeps its own word; an answerable open wait stays open.
        self.assertEqual(question_state({"answerable": False, "request_state": "expired",
                                         "reason": "codex-native-request-not-current"}), "expired")
        self.assertEqual(question_state({"answerable": True}), "open")
        # The catalog: both languages, the rule's words, and no "answer unavailable" anywhere.
        catalog = json.loads((Path(__file__).resolve().parents[2] / "i18n/messages.json").read_text())["messages"]
        want = {"answer_below": ("answer below", "아래 입력창에 답"),
                "answer_there": ("answer in its window", "그 창에서 답"),
                "answer_parent": ("answer in the parent", "부모 창에서 답"),
                "answer_sending": ("sending", "보내는 중"),
                "answer_closed": ("closed", "닫힘")}
        for key, (en, ko) in want.items():
            self.assertEqual(catalog["fleet.session_block." + key], {"en": en, "ko": ko}, key)
            compact = catalog["fleet.session_block.compact_" + key]
            self.assertLessEqual(cell_width(compact["ko"]), 12, compact)
            self.assertLessEqual(cell_width(compact["en"]), 13, compact)
        for key, value in catalog.items():
            if key.startswith("fleet.session_block."):
                self.assertNotIn("답변 불가", value["ko"], key)
                self.assertNotIn("unavailable", value["en"], key)
        # The block: the head names the phrase (short form under 64 columns), the body row says it
        # whole, and the machine reason never reaches the screen.
        for columns, prefix in ((100, ""), (40, "compact_")):
            item = {"answerable": False, "reason": "marker-has-no-bound-native-response-surface",
                    "questions": [{"id": "q", "question": "Which one?", "choices": []}]}
            question_presentations([item], columns)
            display = item["questions"][0]["display"]
            self.assertEqual(item["answer_state"], "answer_below")
            self.assertEqual(display["head"][-1][0], ui_text("fleet.session_block." + prefix + "answer_below"))
            self.assertEqual(display["options"], [[(ui_text("fleet.session_block.answer_below"), "dim")]])
            flat = json.dumps(display, ensure_ascii=False)
            self.assertNotIn("marker-has-no-bound", flat)
            other = {"answerable": False, "reason": "no exact owner session binding",
                     "questions": [{"id": "q", "question": "Which one?"}]}
            question_presentations([other], columns, own=False)
            self.assertEqual(other["answer_state"], "answer_there")
            self.assertEqual(other["questions"][0]["display"]["head"][-1][0],
                             ui_text("fleet.session_block." + prefix + "answer_there"))
        # An open wait carries no such state.
        item = {"answerable": True, "questions": [{"id": "q", "question": "Which?", "choices": ["a"]}]}
        question_presentations([item], 100)
        self.assertNotIn("answer_state", item)

    def values(self):
        return dict(harness='claude',tag='52',state='idle',cwd='/work/project',title='A session',
                    branch='topic*',ctx_pct=42,model='opus',effort='high',weekly_pct=60)

    def test_status_line_has_a_shared_level_gauge_and_native_like_icons(self):
        for percentage,key in ((0,'lvl_g'),(49,'lvl_g'),(50,'lvl_y'),(79,'lvl_y'),(80,'lvl_r')):
            parts=session_line.line_segments({**self.values(),'ctx_pct':percentage},300)
            text=''.join(text for text,_ in parts)
            # Spec 2026-11-11: `[number] project (git)`, no folder or branch glyph; E9 rule 29:
            # the name ends in `/`.
            self.assertIn('[52] project/ (topic*)',text);self.assertNotIn('📁',text);self.assertNotIn('⎇',text)
            self.assertTrue('━' in text or '─' in text)
            self.assertIn(key,[style for text,style in parts if '%' in text])

    def test_question_rows_get_space_before_other_sections_and_empty_sections_hide(self):
        from tools.fleet.session_block import block_lines
        board=[dict(id='exact-wait',questions=[dict(id='q1',question='Include 2025?',choices=['Yes','No'],recommended=0)])]
        for rows in (12,24,50):
            lines=block_lines(self.values(),columns=100,rows=rows,questions=board,
                              route='route 1 · draft',received=['✉ [73] ready'],human='› 2m · hello')
            self.assertTrue(1<=len(lines)<=10)
            text=[''.join(part for part,_ in line) for line in lines]
            self.assertIn('Include 2025?', '\n'.join(text))
            self.assertNotIn('ROUTES','\n'.join(text));self.assertNotIn('hearting ─','\n'.join(text))
            self.assertTrue(all(cell_width(line)<=100 for line in text))
        empty=block_lines({**self.values(),"answer_wait":0},columns=100,rows=30,questions=[],human='› ·')
        self.assertNotIn('◑',''.join(t for line in empty for t,_ in line))


    def test_help_is_only_visible_while_the_block_is_focused(self):
        from tools.fleet.session_block import block_lines
        plain=lambda rows:'\n'.join(''.join(text for text,_ in row) for row in rows)
        self.assertNotIn('Ctrl+]',plain(block_lines(self.values(),columns=180,rows=50)))
        self.assertIn('Ctrl+]',plain(block_lines(self.values(),columns=180,rows=50,focused=True)))

    def test_claude_block_omits_the_status_area_while_other_harnesses_keep_it(self):
        from tools.fleet.session_block import block_lines
        for harness in ('claude','codex','opencode'):
            text='\n'.join(''.join(t for t,_ in row) for row in block_lines(
                {**self.values(),'harness':harness},columns=180,rows=50,route='route 1 · draft',human='› 1m · hello'))
            self.assertEqual('context ' in text,harness!='claude')
            self.assertEqual('[52] project' in text,harness!='claude')
            self.assertIn('route 1',text);self.assertIn('› 1m · hello',text)

    def test_context_is_one_ten_cell_colored_group(self):
        for percent,key,bar in ((42,'lvl_g','━━━━──────'),(60,'lvl_y','━━━━━━────'),(80,'lvl_r','━━━━━━━━──')):
            parts=session_line.line_segments({**self.values(),'ctx_pct':percent},300)
            text=''.join(text for text,_ in parts)
            self.assertIn('context '+bar+' '+str(percent)+'%',text)
            self.assertIn((str(percent)+'%',key),parts)

    def test_claude_native_badges_are_not_repeated_above_question_contents(self):
        from tools.fleet.session_block import block_lines
        board=[{'questions':[{'question':'Keep this question?','choices':['Yes','No']}]}]
        text='\n'.join(''.join(t for t,_ in row) for row in block_lines(
            {**self.values(),'native_route':'route 1 · draft','native_wait':1},
            columns=180,rows=50,questions=board,route='route 1 · draft'))
        self.assertIn('Keep this question?',text)
        self.assertNotIn('⏳',text);self.assertNotIn('◑',text)


    def test_empty_sections_and_hidden_help_reserve_no_rows(self):
        from tools.fleet.session_block import block_lines
        self.assertEqual(block_lines(self.values(),columns=100,rows=30),[])
        plain=block_lines(self.values(),columns=100,rows=30,human='› 1m · hello')
        self.assertEqual(len(plain),1)
        focused=block_lines(self.values(),columns=100,rows=30,human='› 1m · hello',focused=True)
        self.assertEqual(len(focused),2)

    def test_all_original_claude_status_items_survive_in_the_expanded_block(self):
        # Compare against the native renderer itself, not a second item inventory.
        import os,re,shlex,subprocess,time
        from pathlib import Path
        from tools.fleet.session_block import block_lines
        root=Path(__file__).resolve().parents[3]
        source=(root/'adapters/claude/statusline.sh').read_text()
        render=source[source.index('# ANSI 색 + 퍼센트 색 헬퍼'):]
        fields=dict(S_CWD='/work/project',S_MODEL='Opus',S_EFFORT='high',S_CTX='42',
                    S_SESSION_DISPLAY='Explicit session',S_SID='',S_5H='31',S_7D='60',
                    S_5H_RST='1h30m',S_7D_RST='2d3h',S_MS='fable:57',dir='project',
                    git_repo='1',branch='topic*',gflag='⚠️rebase',git_extra_worktrees='2',
                    git_observation='fresh',git_observed_label='12:34:56',git_jobs='compile',
                    SL_STATE_HOME='/dev/null')
        script='\n'.join(key+'='+shlex.quote(value) for key,value in fields.items())+'\n'+render
        native=subprocess.run(['bash'],input=script,text=True,capture_output=True,timeout=5)
        self.assertEqual(native.returncode,0,native.stderr)
        original=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',native.stdout).strip()
        values={**self.values(),'title':'Explicit session','model':'Opus','jobs':'compile',
                'git':dict(repo=1,operation='rebase',extra_worktrees=2,observation='fresh',observed_label='12:34:56'),
                'quota_windows':[('5h',31,time.time()+5401),('7d',60,time.time()+183601)],
                'model_quotas':[('fable',57)],'answer_wait':0}
        block='\n'.join(''.join(t for t,_ in row) for row in [*session_line.status_lines(values,180),*block_lines(values,columns=180,rows=50)])
        for field in original.replace(' │ ','\n').splitlines():
            with self.subTest(field=field):
                self.assertIn(re.sub(r"[\s·│]", "", field),re.sub(r"[\s·│]", "", block))


if __name__=='__main__':unittest.main()
