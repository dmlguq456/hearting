"""The panel shares Fleet's decided prompt presentation and message catalog."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'utilities'))
import session_shell as shell
shell.registry_root()
from fleet import config,session_tags
from fleet.explorer import live
import human_wait_state,session_tidy


class SessionShellCopyTest(unittest.TestCase):
    NOW=1700000200

    def panel(self,display,language='en'):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ,{'FLEET_NOW_LANG':language}))
            stack.enter_context(mock.patch.object(human_wait_state,'cached_statusline',return_value=((0,0),'')))
            stack.enter_context(mock.patch.object(human_wait_state,'cached_view',return_value={'items':[],'by_session':{}}))
            stack.enter_context(mock.patch.object(session_tidy,'prompt_display',return_value=display))
            stack.enter_context(mock.patch.object(session_tags,'assigned_tag',return_value='52'))
            stack.enter_context(mock.patch('time.time',return_value=self.NOW))
            return shell.panel_projection('claude','test-native-session',expanded=True)

    def test_the_existing_fleet_formatter_owns_the_panel_input_line(self):
        with mock.patch.object(live,'_last_input',return_value='SHARED_FRONTIER_PRESENTATION') as present:
            result=self.panel({'human_state':'unknown'})
        present.assert_called_once_with({'human_state':'unknown'},self.NOW)
        self.assertIn('SHARED_FRONTIER_PRESENTATION',result['lines'])

    def test_relative_age_none_and_unknown_match_fleet_exactly(self):
        cases=[({'human_state':'known','human':{'at':self.NOW-60,'text':'hello'}},'› 1m · hello'),
               ({'human_state':'known','human':{'at':self.NOW+1,'text':'hello'}},'› ? · hello'),
               ({'human_state':'none'},'› ·'),({'human_state':'unknown'},'› ?'),({},'› ?')]
        for language in ('en','ko'):
            for display,expected in cases:
                with self.subTest(language=language,display=display):
                    result=self.panel(display,language)
                    self.assertIn(expected,result['lines'])
                    self.assertFalse(any('› 나' in line for line in result['lines']))

    def test_header_and_guidance_are_catalogued_in_fleets_language(self):
        catalog=json.loads((ROOT/'tools/i18n/messages.json').read_text())['messages']
        for key in ('fleet.session_panel.title','fleet.session_panel.open_fleet'):
            self.assertIn(key,catalog)
        for language in ('en','ko'):
            with self.subTest(language=language),mock.patch.dict(os.environ,{'FLEET_NOW_LANG':language}):
                result=self.panel({'human_state':'none'},language)
                title=config.ui_text('fleet.session_panel.title',tag='[52]',harness='claude',
                                     collapse=config.ui_text('fleet.memory.collapse'))
                guide=config.ui_text('fleet.session_panel.open_fleet')
                self.assertEqual(result['lines'][0],title)
                self.assertIn(guide,result['lines'][2])

    def test_catalog_changes_reach_the_panel_without_another_wording_definition(self):
        original=config.ui_text
        def translate(key,**fields):
            return {'fleet.session_panel.title':'HEADER_FROM_CATALOG',
                    'fleet.session_panel.open_fleet':'GUIDE_FROM_CATALOG'}.get(key) or original(key,**fields)
        with mock.patch.object(config,'ui_text',side_effect=translate):
            result=self.panel({'human_state':'none'})
        self.assertEqual(result['lines'][0],'HEADER_FROM_CATALOG')
        self.assertIn('GUIDE_FROM_CATALOG',result['lines'][2])


if __name__=='__main__':unittest.main()
