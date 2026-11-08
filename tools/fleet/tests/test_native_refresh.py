"""Fast cells retain native identity and never advance a complete publication."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.fleet import native_refresh as fast
from tools.fleet.model import Session
from tools.fleet.refresh import LiveSnapshot
from tools.fleet.explorer.view_model import ExplorerViewModel


class NativeDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.native = Session('opencode', 1234, session_id='exact', proc_start='native-birth',
                              model='Old model', liveness='idle', context_policy={'state':'unknown'})
        self.snapshot = LiveSnapshot(sessions=[self.native])
        self.identity = ('opencode','exact','1234','native-birth')
        self.binding = dict(harness='opencode',sid='exact',native_pid=1234,native_start='native-birth',
                            host_pid=4321,host_start='host-birth',incarnation='a'*48,
                            socket=str(self.root/'control.sock'))
        self.starts = {1234:'native-birth',4321:'host-birth'}
        self.registry = self.root/'registry.json'
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(fast,'_attachment_index',return_value={self.identity:self.binding}).start()
        mock.patch.object(fast,'read_proc_start',side_effect=lambda pid:self.starts.get(pid)).start()
        mock.patch.object(fast.session_registry,'registry_path',return_value=str(self.registry)).start()
        mock.patch.object(fast.session_registry,'read',side_effect=lambda *a:
                          json.loads(self.registry.read_text()) if self.registry.exists() else None).start()
        mock.patch.object(fast.opencode,'_db',return_value=str(self.root/'absent.db')).start()
        from tools.fleet import session_view
        mock.patch.object(session_view,'observe',return_value=({},[])).start()
        self.reader = fast.NativeDisplayReader()

    def write(self, percentage=37, **changes):
        value = dict(schema='hearting-native-display-v1',harness='opencode',sid='exact',
                     pid=1234,start='native-birth',host_pid=4321,host_start='host-birth',
                     incarnation='a'*48,values=dict(context_percent=percentage,model='Native model',effort='high'))
        value.update(changes)
        temporary=self.root/'next.json'
        temporary.write_text(json.dumps(value))
        temporary.chmod(0o600)
        temporary.replace(self.root/'native-display.json')

    def test_native_changes_update_without_mutating_or_recollecting_the_base(self):
        self.write()
        original=copy.deepcopy(self.native)
        first=self.reader.read(self.snapshot)
        self.assertEqual(first[0][1]['context'],37)
        self.assertEqual(first[0][1]['model'][0],'Native model')
        self.assertIs(self.reader.read(self.snapshot),first)
        self.write(71)
        self.assertEqual(self.reader.read(self.snapshot)[0][1]['context'],71)
        self.assertEqual(self.native,original)

    def test_wrong_session_or_incarnation_cannot_supply_another_value(self):
        self.write();self.reader.read(self.snapshot)
        self.write(91,sid='another')
        self.assertEqual(self.reader.read(self.snapshot),())
        self.write(91,incarnation='b'*48)
        self.assertEqual(self.reader.read(self.snapshot),())

    def test_native_and_host_births_stay_current_even_with_unchanged_files(self):
        self.write();self.reader.read(self.snapshot)
        self.starts[1234]='reused-pid'
        self.assertIsNone(self.reader.read(self.snapshot)[0][1]['context'])
        self.starts[1234]='native-birth'
        self.starts[4321]='reused-host'
        self.assertEqual(self.reader.read(self.snapshot),())

    def test_native_selection_change_invalidates_the_old_session(self):
        self.write();self.reader.read(self.snapshot)
        self.registry.write_text(json.dumps({'procStart':'native-birth','sessionId':'replacement'}))
        self.assertIsNone(self.reader.read(self.snapshot)[0][1]['context'])

    def test_attachment_change_cannot_reuse_values_from_its_previous_incarnation(self):
        self.write(); self.reader.read(self.snapshot)
        changed = dict(self.binding, incarnation='b'*48)
        with mock.patch.object(fast, '_attachment_index', return_value={self.identity:changed}):
            self.reader.invalidate()
            self.assertEqual(self.reader.read(self.snapshot), ())

    def test_symlink_or_malformed_feed_drops_only_the_fast_value(self):
        self.write();self.reader.read(self.snapshot)
        path=self.root/'native-display.json';path.unlink()
        target=self.root/'other';target.write_text('{}');path.symlink_to(target)
        self.assertEqual(self.reader.read(self.snapshot),())
        path.unlink();path.write_text('{')
        self.assertEqual(self.reader.read(self.snapshot),())

    def test_native_percentage_never_admits_or_replaces_an_applied_trigger(self):
        self.native.ctx_pct=23
        self.native.context_policy={'state':'applied','applied_trigger_tokens':100000}
        self.native.context_display_window_tokens=100000
        self.write(91)
        fields=self.reader.read(self.snapshot)[0][1]
        self.assertEqual(fields['context'],23)
        self.assertEqual(fields['window'],.1)
        self.assertEqual(self.native.context_policy['state'],'applied')

    def test_departed_rows_do_not_accumulate_in_the_display_reader(self):
        self.write();self.reader.read(self.snapshot)
        self.assertEqual(self.reader.read(LiveSnapshot()),())
        self.assertEqual(self.reader._entries,{})


class NativeLayerTests(unittest.TestCase):
    def model(self, pid='1234'):
        return ExplorerViewModel.from_mapping(dict(
            identity=1,sessions=[dict(tag='73',harness='claude',state='idle',context=10,model=['Old','fam_other'])],
            readiness={'ready':True},source_records={'_detail_tags':{'claude:exact':'73'},
            '_detail_bindings':{'claude:exact':('claude','exact',pid,'birth',(),'', '')}}))

    def test_only_cells_change_and_the_published_model_and_bindings_stay_immutable(self):
        base=self.model();layer=fast.NativeDisplayLayer()
        layer.update(((('claude','exact','1234','birth'),{'context':37}),))
        shown=layer.apply(base)
        self.assertEqual(shown.sessions[0]['context'],37)
        self.assertEqual(base.sessions[0]['context'],10)
        self.assertIs(shown.readiness,base.readiness)
        self.assertIs(shown.source_records,base.source_records)
        self.assertEqual(shown.identity,base.identity)
        self.assertIs(layer.apply(base),shown)

    def test_pending_replay_or_changed_binding_cannot_borrow_the_live_value(self):
        layer=fast.NativeDisplayLayer()
        layer.update(((('claude','exact','1234','birth'),{'context':37}),))
        changed=self.model(pid='9999')
        self.assertIs(layer.apply(changed),changed)
        replay=self.model().with_updates(source_records={})
        self.assertIs(layer.apply(replay),replay)

    def test_a_late_fast_read_cannot_replace_cells_changed_by_a_newer_base(self):
        layer=fast.NativeDisplayLayer()
        layer.update(((('claude','exact','1234','birth'),
                       {'context':37,'_base':{'context':10}}),))
        base=self.model()
        newer=base.with_updates(sessions=[dict(base.sessions[0],context=81)])
        self.assertEqual(layer.apply(newer).sessions[0]['context'],81)

    def test_clock_and_usage_updates_reuse_rows_and_keep_the_new_metadata(self):
        base=self.model();layer=fast.NativeDisplayLayer()
        layer.update(((('claude','exact','1234','birth'),{'context':37}),))
        first=layer.apply(base)
        current=base.with_updates(clock=2,usage=[{'text':'fresh quota'}],readiness={'ready':False})
        shown=layer.apply(current)
        self.assertIs(shown.sessions,first.sessions)
        self.assertEqual(shown.usage,current.usage)
        self.assertEqual(shown.readiness,current.readiness)
        self.assertEqual(shown.clock,2)
        layer.update(((('claude','exact','1234','birth'),{'context':71}),))
        self.assertEqual(layer.apply(current).sessions[0]['context'],71)

    def test_cached_rows_cannot_follow_changed_bindings_with_the_same_sessions(self):
        base=self.model();layer=fast.NativeDisplayLayer()
        layer.update(((('claude','exact','1234','birth'),{'context':37}),))
        layer.apply(base)
        replaced=base.with_updates(source_records=self.model(pid='9999').source_records)
        self.assertIs(replaced.sessions,base.sessions)
        self.assertEqual(layer.apply(replaced).sessions[0]['context'],10)


class NativePublisherTests(unittest.TestCase):
    def test_latest_observation_wins_and_only_display_fields_are_shared(self):
        root=Path(__file__).resolve().parents[3]
        code=r'''
const assert=require('node:assert/strict'),fs=require('node:fs/promises');
const display=require(process.argv[1]),directory=process.argv[2];
const identity={harness:'codex',sid:'exact',native_pid:123,native_start:'n',host_pid:456,host_start:'h',incarnation:'a'.repeat(48),token:'PRIVATE'};
const publish=display.publisher(directory,()=>identity);
(async()=>{
 await publish({context_percent:10,model:'A',body:'not a display field'});
 const b=publish({context_percent:20,model:'B'});
 const a=publish({context_percent:10,model:'A'});
 await Promise.all([b,a]);
 let row=JSON.parse(await fs.readFile(directory+'/native-display.json','utf8'));
 assert.equal(row.values.context_percent,10);assert.equal(row.values.model,'A');
 assert.equal(row.token,undefined);assert.equal(row.values.body,undefined);
 identity.sid='replacement';await publish({context_percent:0,model:'New'});
 row=JSON.parse(await fs.readFile(directory+'/native-display.json','utf8'));
 assert.equal(row.sid,'replacement');assert.equal(row.values.context_percent,0);
 console.log('PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run(['node','-e',code,str(root/'utilities/session-shell/native-display.cjs'),directory],
                                  text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('PASS',result.stdout)


if __name__=='__main__':unittest.main()
