"""The shared panel's content order is independent of the native harness."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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

    def test_claude_native_row_owns_no_panel_field(self):
        script = r'''const assert=require('node:assert/strict'),d=require(process.argv[1]);
const seen=d.observe('claude',['  📁 project · ⎇ ? · context ? · 5h ? · 7d ?'],'exact');
assert.equal(seen,null,'an unknown native reading claims nothing');
const numeric=d.observe('claude',['  📁 project · ⎇ main · context ━━━━────── 42% · 5h ? · 7d ?'],'exact');
assert.deepEqual(numeric,{sid:'exact',percent:42},'quota must not replace the observed model');
const full=d.observe('claude',['  📁 project · ⎇ main · context ━━━━────── 42% · Opus high · 5h ? · 7d ? · route 1 · plan · ◑ 1'],'exact');
assert.deepEqual(full,{sid:'exact',percent:42,model:'Opus',effort:'high'},'route, wait and quota stay with the panel');
'''
        result = subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/native-display.cjs')],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_claude_status_rows_blank_only_the_exact_recorded_output(self):
        script = r'''const assert=require('node:assert/strict'),d=require(process.argv[1]);
const rule='─'.repeat(40),box=[rule,'❯  ',rule];
const screen=['conversation','  📁 project · ⎇ main · context 42%',...box,'  📁 project · ⎇ main · context 42%','  ⏵⏵ auto mode on'];
const record={session_id:'exact',lines:['📁 project · ⎇ main · context 42%'],previous:[]};
assert.deepEqual([...d.claudeStatusRows(screen,'exact',record)],[5],'status area only, never the same text in the conversation');
assert.equal(d.claudeStatusRows(screen,'other',record).size,0,'another session');
assert.equal(d.claudeStatusRows(screen,'',record).size,0,'before binding');
assert.equal(d.claudeStatusRows(screen,'exact',{...record,lines:['📁 project · ⎇ main · context 43%']}).size,0,'changed text stays visible');
assert.deepEqual([...d.claudeStatusRows(screen,'exact',{...record,lines:['📁 project · ⎇ main · context 43%'],previous:record.lines})],[5],'paint in flight');
const two=['x',...box,'  📁 a · ⎇ b','  >_ running: job','  footer'];
assert.deepEqual([...d.claudeStatusRows(two,'exact',{session_id:'exact',lines:['📁 a · ⎇ b','>_ running: job']})],[4,5]);
assert.equal(d.claudeStatusRows(two,'exact',{session_id:'exact',lines:['📁 a · ⎇ b','>_ running: other']}).size,0,'partial block');
// The bottom layout's short native screen ends on the record's first row (live
// 80x24 capture, 2026-11-11): that row is the recorded one and yields; a second
// row that differs, or a first row that differs, keeps everything.
const cut=['x',...box,'  📁 a · ⎇ b'];
assert.deepEqual([...d.claudeStatusRows(cut,'exact',{session_id:'exact',lines:['📁 a · ⎇ b','5h 12% 7d 66%']})],[4],'record cut by the screen end');
assert.equal(d.claudeStatusRows(cut,'exact',{session_id:'exact',lines:['📁 a · ⎇ c','5h 12% 7d 66%']}).size,0,'cut and changed');
assert.equal(d.claudeStatusRows([...cut,'  5h 12% 7d 67%'],'exact',{session_id:'exact',lines:['📁 a · ⎇ b','5h 12% 7d 66%']}).size,0,'whole block shown and changed');
const emoji=[...box,'  📁 a · ⎇ b ⚠ ? 🚧 ?   '];
assert.deepEqual([...d.claudeStatusRows(emoji,'exact',{session_id:'exact',lines:['📁 a · ⎇ b ⚠️? 🚧 ?']})],[3],'wide-glyph cell spacing');
const clipped=[...box,'  📁 project · ⎇ main · cont…'];
assert.deepEqual([...d.claudeStatusRows(clipped,'exact',record)],[3],'native ellipsis clip');
// Ctrl+O transcript: the callback's text quoted in the conversation, no input box.
const transcript=['● VERBATIM_STATUS_START','  📁 project · ⎇ main · context 42%','  VERBATIM_STATUS_END','',rule,'  Showing detailed transcript · ctrl+o to toggle'];
assert.equal(d.claudeStatusRows(transcript,'exact',record).size,0,'transcript body stays visible');
const below=['  📁 project · ⎇ main · context 42%',...box,'  ⏵⏵ auto mode on'];
assert.equal(d.claudeStatusRows(below,'exact',record).size,0,'text above the box is conversation');
const quoted=[...box,'● reply','  📁 project · ⎇ main · context 42%','  ⏵⏵ auto mode on'];
assert.equal(d.claudeStatusRows(quoted,'exact',record).size,0,'only the rows directly under the box');
'''
        result = subprocess.run(['node','-e',script,str(ROOT/'utilities/session-shell/native-display.cjs')],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_claude_native_row_and_panel_both_keep_every_field(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from tools.fleet.session_line import line_segments
        from tools.fleet.session_panel import presentation
        values = {'model': 'Native-selected', 'effort': 'high',
                  'ctx_pct': 42, 'weekly_pct': 60, 'route_label': 'route raw review', 'answer_wait': 1}
        native = ''.join(text for text, _ in line_segments(values, 240))
        for field in ('Native-selected', 'route raw', '42%', '60%', '◑'):
            self.assertIn(field, native)
        panel = presentation(values, {}, 'claude', '', 40)
        self.assertIn('Native-selected', ''.join(text for text, _ in panel['model']))
        summary = ''.join(text for row in panel['summary'] for text, _ in row)
        self.assertIn('42%', summary)
        self.assertIn('60%', summary)

    def test_claude_notice_line_shows_one_item_by_priority(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from tools.fleet.session_panel import notice_line
        text = lambda parts: ''.join(t for t, _ in parts)
        now = 10_000.0
        calm = {'ctx_pct': 40, 'quota_windows': [('5h', 30, None), ('7d', 50, None)]}
        display = {'human': {'at': now - 300, 'text': 'x'},
                   'received': [{'at': now - 60, 'sender': '[e7]', 'text': '#1445 병합 가능'}]}
        ask = [{'answerable': True, 'request_state': 'open'}, {'answerable': False}]
        blocked = [{'state': 'attention', 'state_label': 'Needs attention', 'title': 'shell'}]
        done = [{'state': 'done', 'outcome': 'PASS', 'title': 'shell', 'result_at': now - 10}]
        self.assertEqual(notice_line(calm, [], [], {}, now), [], 'nothing means an empty line')
        self.assertIn("1 │ Ctrl+]", text(notice_line({**calm, 'ctx_pct': 95}, blocked, ask, display, now)))
        self.assertIn('95%', text(notice_line({**calm, 'ctx_pct': 95}, blocked, [], display, now)))
        self.assertIn('85%', text(notice_line({**calm, 'quota_windows': [('5h', 85, None)]}, [], [], display, now)))
        self.assertIn('Needs attention · shell', text(notice_line(calm, blocked, [], display, now)))
        # The common reader's blocked route waits on a person: Fleet's ◑ "answer needed".
        waiting = [{'state': 'blocked', 'state_label': '?', 'title': 'review approval'}]
        from tools.fleet.session_panel import ui_text
        self.assertEqual(text(notice_line(calm, waiting, [], {}, now)),
                         '◑ ' + ui_text('fleet.session_block.open') + ' · review approval')
        self.assertEqual(notice_line(calm, [{**waiting[0], 'state': 'working'}], [], {}, now), [], 'a working route is calm')
        self.assertIn('PASS', text(notice_line(calm, done, [], display, now)), 'newest news first')
        self.assertIn('[e7] · #1445', text(notice_line(calm, [], [], display, now)))
        later = {**display, 'human': {'at': now - 5, 'text': 'next'}}
        self.assertEqual(notice_line(calm, done, [], later, now), [], 'the next input retires news')
        self.assertEqual(notice_line(calm, done, [], display, now + 3600), [], 'and so does time')

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
 // Rule 5 (11-14): a sent answer returns the person to the native input, so it holds no delivery.
 assert.equal(block.focused,false);assert.equal(block.waitingForReceipt,false);
 block.update([item,{...item,id:'new'}]);block.enter();
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
 const model={model:[{text:'fixture'}],model_value:'fixture',effort_value:'',routes_known:true,
   status:[[{text:'Full detail '+id}]],summary:[[{text:'shared telemetry'}]],
   routes:state==='start'?[]:[{title:'Current task',state:'working',stages:[{name:'inline',state:'active'}],visibility:{summary:'solo · test? · review? · CI?'}}]};
 const question=['question','focus','draft','restored'].includes(state);
 const args={width,height,side,model,native:{model:'fixture',effort:''},focused:['focus','draft'].includes(state),
   questions:()=>question?[[{text:'Choose the destination'}],[{text:'1 Current project'}],[{text:state==='draft'?'› My draft':'Ctrl+] answer'}]]:[]};
 const all=['claude','codex','opencode'].map(harness=>v.render({...args,harness}).rows.map(r=>r.map(p=>p.text).join('')));
 // Spec rule 29: the top border names the harness (Fleet's session-row head); every other row is the same.
 all.forEach((rows,i)=>assert(rows[0].includes(['claude','codex','opencode'][i]),state+' top border names its harness'));
 const rows=all.map(r=>r.slice(1));
 assert.deepEqual(rows[1],rows[0],state+' codex');assert.deepEqual(rows[2],rows[0],state+' opencode');
 const text=rows[0].join('\n');assert(!text.includes(id),state+' default exposes full ID');
 // Spec 2026-11-11: an unreported effort is left out, never painted as `?`.
 assert(text.includes('fixture')&&!text.includes('fixture · ?'),state+' model row');
 if(question){assert(text.includes('─ Choose the destination ')&&text.includes('│ │ 1 Current project'),state+' question box title and inner padding');}
 // Spec E9 rule 45: beside the native area the count leads its upper-case word.
 if(state!=='start')assert(text.includes(side?'1 ROUTE':'route 1')&&text.includes('Current task'),state+' route row missing');
 else assert(!/route/i.test(text),state+' a known-empty route list draws no box');
}
'''
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/panel-view.cjs')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_opencode_settings_offer_the_same_width_as_the_others(self):
        # Spec rule 49 (d): the fold judgment is one constant, so no harness offers a `threshold`.
        script = r"""
const assert=require('node:assert/strict'),v=require(process.argv[1]);
assert.equal(v.nativePanelHit,undefined);assert.equal(v.nativeSidebarColumns,undefined);
const text=harness=>v.render({width:40,height:21,side:true,harness,model:{labels:{layout:'Layout',width:'Width',threshold:'Threshold'}},
  settings:{value:{...v.defaults},index:0},questions:()=>[]}).rows.flat().map(p=>p.text).join('');
const opencode=text('opencode');assert(opencode.includes('Width')&&!opencode.includes('Threshold'));
assert(!/fixed 42/.test(opencode));assert.equal(opencode,text('claude'));assert.equal(opencode,text('codex'));
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/panel-view.cjs')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == '__main__':
    unittest.main()
