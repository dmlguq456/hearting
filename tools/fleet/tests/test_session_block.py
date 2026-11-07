"""The native-like block prioritizes exact questions without consuming native keys."""
import unittest
from tools.fleet import session_line
from tools.fleet.explorer.grid import cell_width


class SessionBlockTest(unittest.TestCase):
    def values(self):
        return dict(harness='claude',tag='52',state='idle',cwd='/work/project',title='A session',
                    branch='topic*',ctx_pct=42,model='opus',effort='high',weekly_pct=60)

    def test_status_line_has_a_shared_level_gauge_and_native_like_icons(self):
        for percentage,key in ((0,'lvl_g'),(49,'lvl_g'),(50,'lvl_y'),(79,'lvl_y'),(80,'lvl_r')):
            parts=session_line.line_segments({**self.values(),'ctx_pct':percentage},300)
            text=''.join(text for text,_ in parts)
            self.assertIn('📁',text);self.assertIn('⎇',text);self.assertTrue('━' in text or '─' in text)
            self.assertIn(key,[style for text,style in parts if '%' in text])

    def test_question_rows_get_space_before_other_sections_and_empty_sections_hide(self):
        from tools.fleet.session_block import block_lines
        board=[dict(id='exact-wait',questions=[dict(id='q1',question='Include 2025?',choices=['Yes','No'],recommended=0)])]
        for rows in (12,24,50):
            lines=block_lines(self.values(),columns=100,rows=rows,questions=board,
                              route='⏳ route 1 · draft',received=['✉ [73] ready'],human='› 2m · hello')
            self.assertTrue(4<=len(lines)<=10)
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
                {**self.values(),'harness':harness},columns=180,rows=50,route='⏳ route 1 · draft',human='› 1m · hello'))
            self.assertEqual('context ' in text,harness!='claude')
            self.assertEqual('📁 ' in text,harness!='claude')
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
            {**self.values(),'native_route':'⏳ route 1 · draft','native_wait':1},
            columns=180,rows=50,questions=board,route='⏳ route 1 · draft'))
        self.assertIn('Keep this question?',text)
        self.assertNotIn('⏳',text);self.assertNotIn('◑',text)


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
