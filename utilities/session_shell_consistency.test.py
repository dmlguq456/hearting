"""The shared panel's content order is independent of the native harness."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ConsistencyTest(unittest.TestCase):
    def test_async_startup_display_is_not_lost_behind_a_busy_backend(self):
        script = r'''const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const src=fs.readFileSync(process.argv[1],'utf8'),a=src.indexOf('function source('),b=src.indexOf('function readAction(',a),timers=[],calls=new Map();
const c={Buffer,calls,callID:0,sid:'',binding:()=>({}),backend:{exitCode:null,stdin:{write(){}}},setTimeout:fn=>{timers.push(fn);return timers.length},clearTimeout(){}};
vm.createContext(c);vm.runInContext(src.slice(a,b)+'this.source=source;',c);
(async()=>{let value;const pending=c.source('display-bootstrap').then(x=>value=x);for(const t of timers)t();assert.equal(value,undefined);assert.equal(calls.size,1);calls.get(1).resolve({ok:true,panel:{labels:{work:'지금 맡긴 일'}}});await pending;assert.equal(value.ok,true);})();
'''
        result=subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/host.cjs')],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_unknown_native_telemetry_is_owned_without_claiming_zero(self):
        script = r'''const assert=require('node:assert/strict'),d=require(process.argv[1]);
const seen=d.observe('claude',['  📁 project · ⎇ ? · context ? · 5h ? · 7d ?'],'exact');
assert.deepEqual(seen.owned,['context','quota']);assert.equal(seen.percent,undefined);assert.equal(seen.model,undefined);
'''
        result = subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/native-display.cjs')],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_reserved_claude_and_panel_together_keep_fields_once(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from tools.fleet.session_line import line_segments
        from tools.fleet.session_panel import presentation
        values = {'_panel_owned': True, 'model': 'Native-selected', 'effort': 'high',
                  'ctx_pct': 42, 'weekly_pct': 60, 'route_label': 'route raw review', 'answer_wait': 1}
        native = ''.join(text for text, _ in line_segments(values, 240))
        self.assertNotIn('Native-selected', native)
        self.assertNotIn('route raw', native)
        self.assertIn('42%', native)
        panel = presentation(values, {}, 'claude', '', 40, native_owned=('context','quota'))
        self.assertIn('Native-selected', ''.join(text for text, _ in panel['model']))
        self.assertEqual(panel['summary'], [])

    def test_additive_click_matches_cells_before_consuming_native_mouse(self):
        script = r'''const assert=require('node:assert/strict'),v=require(process.argv[1]);
assert.equal(typeof v.nativePanelHit,'function');
const view=v.render({width:35,height:12,side:true,fill:false,model:{model_value:'native'},questions:()=>[]});
const rows=Array.from({length:30},()=>Array(133).fill(' '));
view.rows.forEach((row,i)=>[...row.map(p=>p.text).join('')].forEach((c,j)=>rows[i+3][j+94]=c));
const term={cols:133,rows:30,buffer:{active:{viewportY:0,getLine:y=>({getCell:x=>({getChars:()=>rows[y][x]}),translateToString:(_trim,start,end)=>rows[y].slice(start,end).join('').trimEnd()})}}};
assert.equal(v.nativePanelHit(term,view,123,3).action,'settings');
assert.equal(v.nativePanelHit(term,view,5,3),null,'native conversation');
rows[5][96]='X';assert.equal(v.nativePanelHit(term,view,123,3),null,'changed frame must stay native');
'''
        result = subprocess.run(['node', '-e', script, str(ROOT/'utilities/session-shell/panel-view.cjs')], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_structured_route_codes_never_promote_report_prose_or_no_response(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from tools.fleet.session_panel import route_evidence
        base = {'schema': 'route_visibility_codes_v1', 'shape': 'solo',
                'next': {'kind': 'no-response'}, 'stages': [],
                'verification': {'review': 'none', 'ci': 'recorded',
                  'ci_text_short': 'Synthetic SUCCESS 1234567', 'gate': 'none'}}
        record = {'codes': base, 'summary': 'All tests PASS; running'}
        actual = route_evidence(record)
        self.assertNotIn('SUCCESS', actual['summary'])
        self.assertIn('CI ?', actual['summary'])
        self.assertNotIn('running', actual['next'].lower())
        self.assertNotIn('진행 중', actual['next'])
        self.assertTrue(any('Synthetic SUCCESS' in row[1] for row in actual['lines']))
        base['verification']['ci'] = 'same-head'
        self.assertNotIn('CI ?', route_evidence(record)['summary'])
        self.assertNotIn('CI 통과', route_evidence(record)['summary'])
        from tools.fleet.explorer.grid import cell_width
        self.assertLessEqual(cell_width(route_evidence(record)['summary']), 31)
        self.assertNotIn('running', str(route_evidence({'summary': 'running'})))

    def test_native_model_read_is_loaded_same_thread_display_only(self):
        import os, sys
        from unittest.mock import patch, Mock
        sys.path.insert(0, str(ROOT / 'utilities'))
        sys.path.insert(0, str(ROOT))
        from tools.fleet import native_display as display
        from tools.fleet.collectors import codex, procscan
        import codex_queue_delivery
        sid = '00000000-0000-4000-8000-000000000052'
        pid, birth = os.getpid(), procscan.read_proc_start(os.getpid())
        thread = {'id': sid, 'threadSource': 'user', 'status': {'type': 'idle'},
                  'model': 'observed-native', 'reasoningEffort': 'high'}
        client = Mock(); client.request.return_value = {'thread': thread}
        with patch.object(codex, '_live_tui_start', return_value=birth), \
             patch.object(codex, '_control_socket', return_value=Path('/private/model.sock')), \
             patch.object(codex, '_control_socket_identity', return_value=('one',)), \
             patch.object(codex_queue_delivery, '_connect', return_value=client):
            self.assertEqual(display.codex_selected_model(pid, birth, sid),
                             {'model': 'observed-native', 'effort': 'high'})
            client.request.assert_called_once_with('thread/read', {'threadId': sid, 'includeTurns': False})
            client.close.assert_called_once()
            for update in ({'id': 'other'}, {'threadSource': 'subagent'}, {'status': {'type': 'notLoaded'}}, {'model': None}):
                client.request.return_value = {'thread': dict(thread, **update)}
                self.assertEqual(display.codex_selected_model(pid, birth, sid), {})
            client.request.side_effect = TimeoutError()
            self.assertEqual(display.codex_selected_model(pid, birth, sid), {})
            client.request.side_effect = None; client.request.return_value = {'thread': thread}
            with patch.object(codex, '_control_socket_identity', side_effect=[('one',), ('two',)]):
                self.assertEqual(display.codex_selected_model(pid, birth, sid), {})
            self.assertEqual(display.codex_selected_model(pid, birth+'0', sid), {})

    def test_receipt_view_does_not_hold_its_own_native_delivery_but_editors_do(self):
        script = r'''const assert=require('node:assert/strict'),make=require(process.argv[1]);
(async()=>{
 let finish;
 const block=make({changed(){},answer:()=>new Promise(resolve=>finish=resolve),action(){}});
 const item={id:'q',revision:'one',answerable:true,questions:[{id:'q1',question:'Answer?',choices:[]}]};
 block.update([item]);block.enter();block.input(Buffer.from('tdraft'));
 assert.equal(block.waitingForReceipt,false);
 block.input(Buffer.from('\r'));assert.equal(block.waitingForReceipt,false,'in-flight API is not a receipt');
 finish({ok:true,state:'submitted'});await new Promise(resolve=>setImmediate(resolve));
 assert.equal(block.waitingForReceipt,true);
 block.update([item,{...item,id:'new'}]);block.input(Buffer.from('\t'));
 assert.equal(block.waitingForReceipt,false,'another editable question');
 block.input(Buffer.from('tkept'));block.update([{...item,id:'new',revision:'two'}]);
 assert.equal(block.waitingForReceipt,false,'stale draft remains protected');
})().catch(e=>{console.error(e);process.exitCode=1;});'''
        result = subprocess.run(['node', '-e', script, str(ROOT/'utilities/session-shell/block-input.cjs')], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_route_evidence_wraps_between_tokens_not_inside_a_head_or_word(self):
        script = r'''
const assert=require('node:assert/strict'),v=require(process.argv[1]);
const value='CI 1234567 SUCCESS [same head]';
const model={routes:[{title:'Review',state:'working',visibility:{lines:[['CI',value,'info']]}}]};
const rows=v.render({width:32,height:20,side:true,routeDetail:true,model,questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
for(const token of ['1234567','SUCCESS','[same','head]'])assert(rows.some(row=>row.includes(token)),token+' split across rows');
'''
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/panel-view.cjs')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compact_native_owned_fields_and_long_head_presentation_are_explicit(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from tools.fleet.session_panel import compact_status, evidence_text
        values = {'ctx_pct': 42, 'weekly_pct': 60}
        text = lambda rows: ''.join(part[0] for row in rows for part in row)
        self.assertIn('42%', text(compact_status(values, 40)))
        self.assertNotIn('42%', text(compact_status(values, 40, ('context',))))
        self.assertEqual(compact_status(values, 40, ('context', 'quota')), [])
        rendered = evidence_text('deliver-check · · head ' + 'f'*40 + ' · ')
        self.assertEqual(rendered, 'deliver-check · head fffffff')

    def test_same_facts_and_geometry_have_same_rows_for_all_three_harnesses(self):
        script = r'''
const assert=require('node:assert/strict'),v=require(process.argv[1]);
const id='00000000-0000-4000-8000-000000000052';
for(const [width,height,side] of [[37,21,true],[80,14,false]])for(const state of ['start','idle','question','focus','draft','restored']){
 const model={model:[{text:'fixture'}],model_value:'fixture',effort_value:'',routes_known:state!=='start',
   status:[[{text:'Full detail '+id}]],summary:[[{text:'shared telemetry'}]],
   routes:state==='start'?[]:[{title:'Current task',state:'working',stages:[{name:'inline',state:'active'}],visibility:{summary:'solo · test? · review? · CI?'}}]};
 const question=['question','focus','draft','restored'].includes(state);
 const args={width,height,side,model,native:{model:'fixture',effort:''},focused:['focus','draft'].includes(state),
   questions:()=>question?[[{text:'Choose the destination'}],[{text:'1 Current project'}],[{text:state==='draft'?'› My draft':'Ctrl+] answer'}]]:[]};
 const rows=['claude','codex','opencode'].map(harness=>v.render({...args,harness}).rows.map(r=>r.map(p=>p.text).join('')));
 assert.deepEqual(rows[1],rows[0],state+' codex');assert.deepEqual(rows[2],rows[0],state+' opencode');
 const text=rows[0].join('\n');assert(!text.includes(id),state+' default exposes full ID');
 assert(text.includes('fixture · ?'),state+' missing effort placeholder');
 assert(text.includes('Current work'),state+' missing work section');
 if(question){assert(text.includes('│ Choose the destination'),state+' inner padding');}
 if(state!=='start')assert(text.includes('solo · test?'),state+' verification summary missing');
}
'''
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/panel-view.cjs')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_opencode_fixed_width_notice_is_one_physical_exception(self):
        script = r'''
const assert=require('node:assert/strict'),v=require(process.argv[1]);
const r=v.render({width:37,height:21,side:true,harness:'opencode',model:{labels:{layout:'Layout'}},settings:{value:{...v.defaults},index:0},questions:()=>[]});
const s=r.rows.flat().map(p=>p.text).join('');
assert.equal((s.match(/fixed 42/g)||[]).length,1);assert(s.includes('› Layout'));
'''
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/panel-view.cjs')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
