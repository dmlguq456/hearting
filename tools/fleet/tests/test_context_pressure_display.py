"""Replay the supplied numbers through existing renderers; no native consumer."""
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet.config import ui_text
from fleet.model import Session
from fleet.session_line import status_lines, context_detail_lines
from fleet.token_budget import compaction_imminent, context_display_detail
from fleet.explorer.frame import draw
from fleet.explorer.state import NavState
from fleet.explorer.view_model import ExplorerViewModel, load_fixture, persistable_view_model
from fleet.native_refresh import NativeDisplayLayer


def pressure():
    return dict(state='observed',source='codex-internal-log-v2',session_id='exact-session-id',
                scope_tokens=799116,limit_tokens=784800,scope='Total',observed_at=1000.501,
                usage_at=1000.5,compact_at=None,fresh_until=1900.5,token_limit_reached=True)


def values():
    return dict(harness='codex',ctx_pct=65,active_context_tokens=539613,
                context_window_tokens=828400,compaction_pressure=pressure())


class PressureDisplayTest(unittest.TestCase):
    def test_common_line_preserves_65_percent_and_adds_only_the_observed_warning(self):
        with patch('time.time',return_value=1001):
            text=''.join(s for row in status_lines(values(),274) for s,_ in row)
            self.assertIn('65%',text);self.assertIn(ui_text('fleet.context.compaction_imminent'),text)
            detail=context_detail_lines(context_display_detail(values()))
            self.assertIn('539,613 / 828,400',str(detail));self.assertIn('799,116',str(detail))
            self.assertIn('784,800',str(detail));self.assertIn('Total',str(detail))
        for changed in (None,{'state':'unknown'},dict(pressure(),token_limit_reached=False)):
            with patch('time.time',return_value=1001):
                text=''.join(s for row in status_lines(dict(values(),compaction_pressure=changed),274) for s,_ in row)
                self.assertIn('65%',text);self.assertNotIn(ui_text('fleet.context.compaction_imminent'),text)
        with patch('time.time',return_value=2000):
            self.assertFalse(compaction_imminent(pressure()))
            detail=context_detail_lines(context_display_detail(values()))
            self.assertEqual(detail[1][1],'?');self.assertIn('539,613',str(detail))

    def test_ended_rows_do_not_retain_an_active_compaction_warning(self):
        for state in ('dead','stale'):
            self.assertIsNone(context_display_detail(dict(values(),liveness=state)))

    def mapping(self):
        m=load_fixture(Path(__file__).parent/'fixtures/explorer/view_model.json').to_mapping()
        session=dict(m['sessions'][0],tag='52',harness='codex',context=65,window=.8284,
                     context_detail=context_display_detail(values()))
        m.update(sessions=[session],routes=[],questions=[])
        return m

    def test_fleet_row_and_details_use_the_same_numeric_observation(self):
        with patch('time.time',return_value=1001):
            vm=ExplorerViewModel.from_mapping(self.mapping())
            frame=draw(vm,NavState(view='OVERVIEW',title='OVERVIEW'),274,50,beat=1)
            text='\n'.join(frame.text_rows())
            self.assertIn('65%',text);self.assertIn(ui_text('fleet.context.compaction_imminent'),text)
            frame=draw(vm,NavState(view='OVERVIEW',title='OVERVIEW',scope='session:52'),274,60,beat=1)
            text='\n'.join(frame.text_rows())
            self.assertIn('539,613',text);self.assertIn('799,116',text);self.assertIn('784,800',text)
        # Pressure is transient display data, not saved stale-screen authority.
        saved=persistable_view_model(vm.to_mapping())
        self.assertNotIn('context_detail',str(saved))

    def test_fast_display_overlay_does_not_change_readiness_or_other_bindings(self):
        m=self.mapping();m['sessions'][0].pop('context_detail')
        m.update(readiness={'ready':True},source_records={'_detail_tags':{'codex:exact-session-id':'52'},
                 '_detail_bindings':{'codex:exact-session-id':('codex','exact-session-id','2','birth',(),'', '')}})
        vm=ExplorerViewModel.from_mapping(m);layer=NativeDisplayLayer()
        detail=context_display_detail(values());layer.update(((('codex','exact-session-id','2','birth'),
           dict(context_detail=detail,_base={'context_detail':None})),))
        updated=layer.apply(vm)
        self.assertIs(updated.readiness,vm.readiness)
        self.assertIs(updated.source_records,vm.source_records)
        self.assertEqual(updated.sessions[0]['context'],65)
        self.assertEqual(updated.sessions[0]['context_detail']['pressure']['scope_tokens'],799116)


if __name__=='__main__':unittest.main()
