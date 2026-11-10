"""A resumed root needs native viewport selection, not retained membership alone."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'utilities'), str(ROOT / 'tools')]
import session_shell as shell
import pane_ownership
from fleet.collectors import codex


class ResumedSelectionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'selection_answer_fixture', ROOT / 'utilities/session_shell_answer_current.test.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.fx = module.CurrentAnswerTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.old = self.fx.sid
        self.resumed = '00000000-0000-4000-8000-000000000099'
        self.fx.selected = None  # Both loaded roots have this TUI's native origin.
        self.frame_sid = self.resumed
        self.revision = 9
        self.member = (self.fx.native.pid, self.fx.binding['native_start'])
        self.process_row = (self.member[0], 1, self.member[0], 100, self.member[0], self.member[1])
        self.patches = [
            mock.patch.object(shell, 'control', side_effect=self.observe),
            mock.patch.object(codex, 'process_for_thread', side_effect=self.membership),
            mock.patch.object(pane_ownership, '_runtime', return_value=('codex', False)),
            mock.patch.object(pane_ownership, '_registered_worker', return_value=False),
            mock.patch.object(pane_ownership, '_process', return_value=self.process_row),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def observe(self, binding, operation, **_fields):
        self.assertEqual(operation, 'native-selection')
        return dict(ok=True, native_frame=dict(revision=self.revision, epoch=12,
            lines=['› Ask Codex', self.frame_sid + ' · Context 10% used',
                   '← for agents · ? for shortcuts']))

    def membership(self, sid, **kwargs):
        self.assertTrue(kwargs.get('fresh'))
        return self.member if sid in {self.old, self.resumed} else None

    def select_resumed(self):
        self.fx.sid = self.resumed
        self.fx.binding = {**self.fx.binding, 'sid': self.resumed}
        self.assertTrue(shell.publish_binding(self.fx.binding))

    def test_backend_selects_the_current_native_root_without_a_start_hook(self):
        with mock.patch.object(shell.os, 'getppid', return_value=self.fx.binding['host_pid']):
            result = shell._backend_operation(dict(op='selection', binding=self.fx.binding))
        self.assertEqual(result.get('selected_sid'), self.resumed)
        # Observation never rewrites the old attachment or answers a question.
        self.assertEqual(shell.read_binding(shell.binding_path('codex', self.fx.binding['host_pid'])),
                         self.fx.binding)

    def test_resumed_answer_uses_the_existing_queue_and_rechecks_selection_at_effect(self):
        self.select_resumed()
        self.fx.ask('Question in resumed root')
        self.assertTrue(self.fx.answer()['ok'])
        _text, ref, _recipient = self.fx.fx.prepare(self.fx.signal)
        self.frame_sid = self.old
        result = self.fx.deliver(ref)
        self.assertNotIn('thread/queue/add', self.fx.rpcs)
        self.assertIn('current-session-unverified', result.get('reason', ''))
        self.frame_sid = self.resumed
        self.fx.rpcs.clear()
        self.fx.deliver(ref)
        self.assertEqual(self.fx.rpcs.count('thread/queue/add'), 1)

    def test_old_answer_cannot_borrow_the_resumed_root_or_its_display(self):
        self.fx.ask()
        self.assertFalse(self.fx.answer()['ok'])
        self.assertIsNone(self.fx.fx.record(self.fx.signal))

    def test_selection_needs_stable_frames_and_the_same_native_lifetime(self):
        self.select_resumed()
        self.assertTrue(shell.answer_attachment_current(self.fx.binding))
        for member in (None, (self.fx.native.pid + 1, self.member[1]),
                       (self.fx.native.pid, '0')):
            with self.subTest(member=member), mock.patch.object(codex, 'process_for_thread', return_value=member):
                self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        first = self.observe(self.fx.binding, 'native-selection')
        second = dict(ok=True, native_frame={**first['native_frame'], 'revision': 10})
        with mock.patch.object(shell, 'control', side_effect=[first, second]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        second = dict(ok=True, native_frame={**first['native_frame'], 'epoch': 13})
        with mock.patch.object(shell, 'control', side_effect=[first, second]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        moved = dict(ok=True, native_frame={**first['native_frame'], 'revision': 10,
                                            'lines': ['›', self.old + ' · Context 10% used']})
        with mock.patch.object(shell, 'control', side_effect=[first, moved]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))

    def test_a_repaint_between_the_reads_leaves_the_decision_to_native_proof(self):
        # A working turn repaints the screen between the two reads. The frame is
        # then no observation: only an independent native proof of this same root
        # can pass, exactly as when no frame is available at all.
        self.select_resumed()
        first = self.observe(self.fx.binding, 'native-selection')
        repaint = dict(ok=True, native_frame={**first['native_frame'], 'revision': 10})
        for native, expected in ((None, False), (self.old, False), (self.resumed, True)):
            self.fx.selected = native
            with self.subTest(native=native), \
                    mock.patch.object(shell, 'control', side_effect=[first, repaint]):
                self.assertEqual(shell.answer_attachment_current(self.fx.binding), expected)
        with mock.patch.object(pane_ownership, '_runtime', side_effect=[('codex', False), ('codex', True)]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        with mock.patch.object(codex, 'session_id_of_process', side_effect=[None, None, self.old]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        background = (*self.process_row[:4], self.member[0] + 1, self.member[1])
        with mock.patch.object(pane_ownership, '_process', return_value=background):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        with mock.patch.object(pane_ownership, '_process', side_effect=[self.process_row, background]):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))

    def test_unknown_conflicting_or_non_native_footer_never_authorizes_an_answer(self):
        self.select_resumed()
        for lines in (['›', 'custom status', '? for shortcuts'],
                      ['›', self.resumed, 'Hearting projected status'],
                      ['›', self.old + ' · ' + self.resumed, '? for shortcuts'],
                      ['›', self.resumed[:-3] + '…', '? for shortcuts']):
            with self.subTest(lines=lines), mock.patch.object(shell, 'control',
                    return_value=dict(ok=True, native_frame=dict(revision=9, epoch=12, lines=lines))):
                self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        self.fx.selected = self.resumed
        with mock.patch.object(shell, 'control', return_value=dict(ok=True,
                native_frame=dict(revision=9, epoch=12, lines=[], invalid=True))):
            self.assertFalse(shell.answer_attachment_current(self.fx.binding))
        self.fx.selected = self.old  # Independent, current native proof conflicts.
        self.assertFalse(shell.answer_attachment_current(self.fx.binding))

    def test_existing_unique_native_proof_does_not_require_the_new_optional_surface(self):
        self.fx.selected = self.old
        with mock.patch.object(shell, 'control', return_value=dict(ok=False, reason='unsupported-operation')):
            self.assertTrue(shell.answer_attachment_current(self.fx.binding))


class NativeSelectionHostTests(unittest.TestCase):
    def test_actual_control_and_native_output_callbacks_keep_selection_generation(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),a=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const begin=source.indexOf('function nativeSelectionFrame()');
const helpers=begin<0?'':source.slice(begin,source.indexOf('function readAction(',begin));
const request=source.slice(source.indexOf('async function request('),source.indexOf('terminal.parser.registerCsiHandler',source.indexOf('async function request(')));
const A='00000000-0000-4000-8000-000000000099',B='00000000-0000-4000-8000-000000000052';
function context(extra=[]){
 const lines=['transcript '+B,'› PRIVATE HUMAN DRAFT',...extra,'',A+' · Context 10% used','← for agents · ? for shortcuts',''];
 const actions=[];let resolve;
 const c={harness:'codex',sid:B,native:{pid:123,start:'456'},screenRevision:9,epoch:12,selection:1,
   sequence:4,token:'owned',ended:false,relay:false,actions,
   terminal:{rows:lines.length,buffer:{active:{viewportY:0,getLine:i=>({translateToString:()=>lines[i]})}}},
   exact:r=>r.sid===c.sid,source:()=>new Promise(r=>{resolve=r;}),lifecycle:r=>{actions.push(r);},
   release:r=>resolve(r)};
 vm.createContext(c);vm.runInContext(helpers+request,c);return c;
}
(async()=>{
 const c=context(),seen=await c.request({op:'native-selection',sid:B});
 a.equal(seen.ok,true);a.equal(seen.native_frame.revision,9);
 a.deepEqual(Array.from(seen.native_frame.lines),['›',A+' · ?','? for shortcuts']);
 a.equal(JSON.stringify(seen).includes('PRIVATE HUMAN DRAFT'),false);
 for(const body of ['PRIVATE_CONTINUATION_SENTINEL','Where should the new conversation run?']){
  const hidden=await context([body]).request({op:'native-selection',sid:B});
  a.equal(hidden.native_frame.invalid,true);a.equal(hidden.native_frame.lines.length,0);
  a.equal(JSON.stringify(hidden).includes(body),false);
 }
 a.equal((await c.request({op:'native-selection',sid:A})).ok,false);
 for(const changed of ['none','screen','input','selection','unproved']){
  const x=context();x.observeNativeSelection();
  if(changed==='screen')x.screenRevision++;
  if(changed==='input')x.epoch++;
  if(changed==='selection')x.selection++;
  x.release({ok:changed!=='unproved',selected_sid:A,native_revision:9,native_epoch:12});
  await new Promise(setImmediate);
  a.equal(x.actions.length,changed==='none'?1:0,changed);
  if(x.actions.length)a.deepEqual(JSON.parse(JSON.stringify(x.actions[0])),
    {token:'owned',harness:'codex',pid:123,start:'456',sid:A,seq:5,event:'select'});
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/host.cjs')],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_a_screen_painted_during_a_check_is_looked_at_once_it_ends(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),a=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const begin=source.indexOf('function nativeSelectionFrame()');
const helpers=source.slice(begin,source.indexOf('function readAction(',begin));
const A='00000000-0000-4000-8000-000000000099',B='00000000-0000-4000-8000-000000000052';
const lines=['› Ask Codex to do anything','',A+' · '+A];
const calls=[],actions=[];
const c={harness:'codex',sid:B,native:{pid:123,start:'456'},screenRevision:9,epoch:12,selection:1,
  sequence:4,token:'owned',ended:false,relay:false,
  terminal:{rows:lines.length,buffer:{active:{viewportY:0,getLine:i=>({translateToString:()=>lines[i]})}}},
  source:()=>new Promise(r=>calls.push(r)),lifecycle:r=>{actions.push(r);}};
vm.createContext(c);vm.runInContext(helpers,c);
(async()=>{
 c.observeNativeSelection();a.equal(calls.length,1);
 c.screenRevision=10;c.observeNativeSelection();a.equal(calls.length,1);  // skipped while checking
 calls[0]({ok:true,selected_sid:A,native_revision:9,native_epoch:12});
 await new Promise(setImmediate);
 a.equal(actions.length,0);a.equal(calls.length,2);                     // the skipped screen, once
 calls[1]({ok:true,selected_sid:A,native_revision:10,native_epoch:12});
 await new Promise(setImmediate);
 a.equal(actions.length,1);a.equal(actions[0].sid,A);a.equal(calls.length,2);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/host.cjs')],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

class FooterReaderParityTests(unittest.TestCase):
    """The host frame and the Python footer reader name the same current thread."""
    FIXTURES = ROOT / 'utilities/fixtures/codex-0.163-shared-footer'
    REAL = {'side-133x30-native91.txt': '01a1c650-6f32-7880-bb70-815b30d8bd06',
            'bottom-80x24.txt': '01a1c655-bacb-75a2-b8d1-2997f18b9fcd'}
    T = '01a1c520-2193-71f0-a79c-b1fb86bd1a21'
    OTHER = '99a1c520-2193-71f0-a79c-b1fb86bd1a21'

    def host_frames(self, screens):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8'),screens=JSON.parse(fs.readFileSync(0,'utf8'));
const begin=source.indexOf('function nativeSelectionFrame()');
const fn=source.slice(begin,source.indexOf('let checkingNativeSelection=',begin));
console.log(JSON.stringify(screens.map(lines=>{
 const c={screenRevision:3,epoch:1,terminal:{rows:lines.length,
   buffer:{active:{viewportY:0,getLine:i=>({translateToString:()=>lines[i]})}}}};
 vm.createContext(c);vm.runInContext(fn+'\nthis.out=nativeSelectionFrame();',c);return c.out;
})));
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell/host.cjs')],
                                input=__import__('json').dumps(screens), text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return __import__('json').loads(result.stdout)

    @staticmethod
    def current(state_sid):
        state, sid = state_sid
        return sid if state == 'current' else None

    def test_both_readers_agree_on_real_and_edge_screens(self):
        real = {name: self.FIXTURES.joinpath(name).read_text().splitlines() for name in self.REAL}
        prompt = ['', '› Ask Codex to do anything', '']
        rows = [
            # Status line alone (Codex 0.163, no help row).
            f'  {self.T} · {self.T} · master · Context 0% used · gpt default',
            f'  {self.T} · proj · Context 12% used',
            f'  {self.T} · {self.T[:-3]}…',
            f'  {self.T} · {self.T[:8]}…  ⚠ 1 warning · f2 to view',
            # Not a status line: no identity.
            f'  text mentioning {self.T}', f'  {self.T} · master',
            f'  {self.T} · {self.OTHER[:20]}…', f'  title · {self.T} · Context 1% used',
        ]
        layouts = {name: lines for name, lines in real.items()}
        layouts.update({f'row-{i}': prompt + [row] for i, row in enumerate(rows)})
        layouts.update({
            'help-first': prompt + [f'  {self.T} · Context 10% used', '  ← for agents · ? for shortcuts'],
            'help-last': prompt + [f'  proj · {self.T}', '  ? for shortcuts'],
            'help-compact': prompt + [f'  {self.T} · {self.T}', '  ← for agents'],
            'help-conflict': prompt + [f'  {self.T} · x · {self.OTHER}', '  ? for shortcuts'],
            'menu': prompt + ['  Where should the new conversation run?'],
            'no-prompt': ['  ' + self.T + ' · ' + self.T],
        })
        frames = dict(zip(layouts, self.host_frames(list(layouts.values()))))
        for name, lines in layouts.items():
            with self.subTest(screen=name):
                frame = frames[name]
                direct = self.current(pane_ownership._codex_current_footer(lines))
                via_host = (None if frame.get('invalid')
                            else self.current(pane_ownership._codex_current_footer(frame['lines'])))
                self.assertEqual(via_host, direct)
                if name in self.REAL:
                    self.assertEqual(direct, self.REAL[name])
                # Only identity evidence leaves the native viewport.
                for line in frame['lines']:
                    self.assertNotIn('master', line)
                    self.assertNotIn('warning', line)


if __name__ == '__main__':
    unittest.main()
