"""The panel shares Fleet's decided prompt presentation and message catalog."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'utilities'))
import session_shell as shell
shell.registry_root()
from fleet import config,session_tags,prompt_line
from fleet.explorer import live
import human_wait_state,session_tidy,questions


class SessionShellCopyTest(unittest.TestCase):
    NOW=1700000200

    def panel(self,display,language='en',question=False,**extra):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ,{'FLEET_NOW_LANG':language}))
            stack.enter_context(mock.patch.object(human_wait_state,'cached_statusline',return_value=((0,0),'')))
            items=[{'id':'copy-fixture','owner':{'harness':'claude','session_id':'test-native-session'}}] if question else []
            stack.enter_context(mock.patch.object(human_wait_state,'cached_view',return_value={
                'items':items,'by_session':{('claude','test-native-session'):['copy-fixture']}}))
            stack.enter_context(mock.patch.object(questions,'describe',return_value={
                'id':'copy-fixture','revision':'0'*64,'answerable':True,'questions':[{'question':'Choose?','choices':['Yes','No']}]}))
            stack.enter_context(mock.patch.object(session_tidy,'prompt_display',return_value=display))
            stack.enter_context(mock.patch.object(session_tags,'assigned_tag',return_value='52'))
            stack.enter_context(mock.patch('time.time',return_value=self.NOW))
            return shell.panel_projection(extra.pop('harness','claude'),'test-native-session',expanded=True,**extra)

    def lines(self,result):
        return [''.join(part['text'] for part in line) for line in result['lines']]

    def test_a_known_native_context_is_not_erased_by_a_cold_projection(self):
        for harness,percent in (('codex',0),('opencode',10)):
            with self.subTest(harness=harness):
                result=self.panel({'human_state':'none'},harness=harness,native_context=percent)
                self.assertEqual(result['context'],percent)
                self.assertTrue(any(str(percent)+'%' in line for line in self.lines(result)))

    def test_the_existing_fleet_formatter_owns_the_panel_input_line(self):
        with mock.patch.object(prompt_line,'last_input',return_value='SHARED_FRONTIER_PRESENTATION') as present:
            result=self.panel({'human_state':'unknown'})
            self.assertEqual(live._last_input({'human_state':'unknown'},self.NOW),'SHARED_FRONTIER_PRESENTATION')
        self.assertEqual(present.call_args_list,[mock.call({'human_state':'unknown'},self.NOW)]*2)
        self.assertIn('SHARED_FRONTIER_PRESENTATION',self.lines(result))

    def test_relative_age_none_and_unknown_match_fleet_exactly(self):
        cases=[({'human_state':'known','human':{'at':self.NOW-60,'text':'hello'}},'› 1m · hello'),
               ({'human_state':'known','human':{'at':self.NOW+1,'text':'hello'}},'› ? · hello'),
               ({'human_state':'none'},'› ·'),({'human_state':'unknown'},'› ?'),({},'› ?')]
        for language in ('en','ko'):
            for display,expected in cases:
                with self.subTest(language=language,display=display):
                    result=self.panel(display,language)
                    lines=self.lines(result)
                    if expected=='› ·':
                        self.assertNotIn(expected,lines)  # Approved empty sections disappear.
                    else:
                        self.assertIn(expected,lines)
                    self.assertEqual(live._last_input(display,self.NOW),expected)
                    self.assertFalse(any('› 나' in line for line in lines))

    def test_header_and_guidance_are_catalogued_in_fleets_language(self):
        catalog=json.loads((ROOT/'tools/i18n/messages.json').read_text())['messages']
        for key in ('fleet.session_block.questions','fleet.session_block.editor_help'):
            self.assertIn(key,catalog)
        for language in ('en','ko'):
            with self.subTest(language=language),mock.patch.dict(os.environ,{'FLEET_NOW_LANG':language}):
                result=self.panel({'human_state':'none'},language,question=True)
                self.assertTrue(any(config.ui_text('fleet.session_block.questions')+' 1' in line
                                    for line in self.lines(result)))
                self.assertEqual(result['help'],config.ui_text('fleet.session_block.editor_help'))
                self.assertNotIn('hearting ─','\n'.join(self.lines(result)))

    def test_catalog_changes_reach_the_panel_without_another_wording_definition(self):
        catalog=config._ui_catalog()
        def translate(key,language,**fields):
            return {'fleet.session_block.questions':'QUESTIONS_FROM_CATALOG',
                    'fleet.session_block.editor_help':'GUIDE_FROM_CATALOG'}.get(key) or catalog.text(key,language,**fields)
        shared=SimpleNamespace(text=translate,normalize=catalog.normalize)
        with mock.patch.object(config,'_ui_catalog',return_value=shared):
            result=self.panel({'human_state':'none'},question=True)
        self.assertTrue(any('QUESTIONS_FROM_CATALOG' in line for line in self.lines(result)))
        self.assertEqual(result['help'],'GUIDE_FROM_CATALOG')


if __name__=='__main__':unittest.main()
