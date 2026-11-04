#!/usr/bin/env python3
"""Exercise durable correction admission and real App Server pipe boundaries."""
import importlib.util
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'utilities'))
import dispatch_owner_input as I
from dispatch_contract import hold_supervisor_lease, supervisor_lease_path
from split_module_fixture import patch_split


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


C = load('input_codex_supervisor', ROOT / 'utilities/codex-app-server-supervisor.py')
FIXTURE = load('input_supervisor_fixture', ROOT / 'utilities/codex_app_server_supervisor.test.py')


class OwnerInputTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs.log'
        self.attempt = FIXTURE.PARENT
        self.lease = supervisor_lease_path(self.jobs, self.attempt)
        self.jobs.write_text(FIXTURE.owner_row(self.lease))
        self.held = hold_supervisor_lease(self.jobs, self.attempt, self.lease)
        self.held.__enter__()
        self.addCleanup(self.held.__exit__, None, None, None)
        self.events = []
        self.control = I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)

    def state(self):
        return I.inspect(self.jobs, self.attempt)['requests']

    def test_duplicate_and_changed_request_id_use_one_durable_record(self):
        errors = []
        def send():
            try:
                I.submit(self.jobs, self.attempt, 'reuse the existing implementation', 'same')
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=send) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.state()), 1)
        with self.assertRaisesRegex(I.InputError, 'content-conflict'):
            I.submit(self.jobs, self.attempt, 'different', 'same')
        self.assertEqual(self.state()[0]['state'], 'queued')

    def test_held_lease_stores_once_without_starting_a_second_consumer(self):
        with mock.patch.object(I, 'recover_consumer') as recover:
            receipt = I.submit(self.jobs, self.attempt, 'keep for the live consumer', 'lease')
        self.assertEqual(receipt['consumer_recovery'], 'live')
        self.assertEqual(self.state()[0]['state'], 'queued')
        recover.assert_not_called()

    def test_unknown_owner_process_and_changed_resume_thread_never_start_a_consumer(self):
        import dispatch_contract as contract
        import dispatch_completion_join as join
        import route_authority
        import route_parent_close

        I.submit(self.jobs, self.attempt, 'pending correction', 'pending')
        self.jobs.write_text(self.jobs.read_text().replace(
            'attempt_id=' + self.attempt,
            'launch_claimed=1,launch_started=1,launch_fence=registry-v1,'
            'pid=99999999,pid_start=100,attempt_id=' + self.attempt))
        launch = mock.Mock()
        patches = (
            patch_split(contract, 'supervisor_lease_is_held', return_value=False),
            mock.patch.object(route_authority, 'owns', return_value=True),
            mock.patch.object(route_parent_close, 'row_requested', return_value=False),
            mock.patch.object(route_parent_close, 'owner_continuation_processes', return_value=(
                contract.ProcessQuiescence('quiescent', 'registered-resources-preserved'),
                [{'kind': 'resource', 'run_id': 'cpu'}])),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with patch_split(contract, 'attempt_governed_process_quiescence',
                               return_value=contract.ProcessQuiescence(
                                   'unverifiable', 'identity-missing')):
            unknown = I.recover_consumer(self.jobs, self.attempt, session='parent', spawn=launch)
        self.assertEqual(unknown['consumer_recovery'], 'not-started:owner-process-unverifiable')
        launch.assert_not_called()

        with patch_split(contract, 'attempt_governed_process_quiescence',
                               return_value=contract.ProcessQuiescence('quiescent', 'gone')), \
             mock.patch.object(join, 'read_supervisor_phase_state',
                               return_value=SimpleNamespace(resource={'session_id': 'different-thread'})):
            changed = I.recover_consumer(self.jobs, self.attempt, session='parent', spawn=launch)
        self.assertEqual(changed['consumer_recovery'], 'not-started:owner-input-thread-mismatch')
        launch.assert_not_called()

    def test_only_an_adapter_with_a_same_attempt_recovery_restores_or_moves_a_consumer(self):
        # T10: the decision follows the adapter's realization, not a harness name.
        import dispatch_contract as contract
        import route_authority
        import route_parent_close

        self.assertTrue(callable(I._respawn_adapter('codex')))
        for harness in ('claude', 'opencode', 'unknown'):
            self.assertIsNone(I._respawn_adapter(harness))
        resources = [{'kind': 'resource', 'run_id': 'cpu'}]
        for harness in ('claude', 'opencode'):
            with self.subTest(harness=harness):
                jobs = self.root / harness / 'jobs.log'
                jobs.parent.mkdir()
                lease = supervisor_lease_path(jobs, self.attempt)
                jobs.write_text(FIXTURE.owner_row(lease))
                I.initialize_owner_input(jobs, self.attempt, 'codex-active-turn')
                with hold_supervisor_lease(jobs, self.attempt, lease):
                    I.OwnerInput(jobs, self.attempt, 'thread-1', 'codex-active-turn', lambda _e: None)
                # The same bound channel, now on a row of another harness.
                jobs.write_text(FIXTURE.owner_row(lease).replace(
                    'harness=codex', 'harness=' + harness).replace(
                    'attempt_id=' + self.attempt,
                    'launch_claimed=1,launch_started=1,launch_fence=registry-v1,'
                    'pid=99999999,pid_start=100,attempt_id=' + self.attempt))
                channel = I._path(jobs, self.attempt)
                value = json.loads(channel.read_text())
                value['target'] = I._target(jobs, self.attempt)[1]
                channel.write_text(json.dumps(value))
                I.submit(jobs, self.attempt, 'pending correction', 'pending')
                with patch_split(contract, 'supervisor_lease_is_held', return_value=False), \
                     mock.patch.object(route_authority, 'owns', return_value=True), \
                     mock.patch.object(route_parent_close, 'row_requested', return_value=False), \
                     mock.patch.object(route_parent_close, 'owner_continuation_processes',
                                       return_value=(contract.ProcessQuiescence(
                                           'quiescent', 'registered-resources-preserved'), resources)), \
                     patch_split(contract, 'attempt_governed_process_quiescence',
                                       return_value=contract.ProcessQuiescence('quiescent', 'gone')):
                    result = I.recover_consumer(jobs, self.attempt, session='parent')
                self.assertEqual(result['consumer_recovery'],
                                 'not-started:consumer-respawn-unrealized-' + harness)
                with mock.patch.object(I, '_installed_release', return_value=ROOT.resolve()), \
                     mock.patch.object(I, '_consumer_release', return_value=ROOT.resolve().parent), \
                     mock.patch.object(route_authority, 'owns', return_value=True), \
                     mock.patch.object(route_parent_close, 'row_requested', return_value=False), \
                     mock.patch.object(route_parent_close, 'owner_resources', return_value=resources), \
                     mock.patch.object(route_parent_close, 'owner_consumer_session',
                                       return_value='thread-1'), \
                     patch_split(I, 'supervisor_lease_is_held', return_value=True), \
                     mock.patch.object(I, '_respawn_adapter', wraps=I._respawn_adapter) as adapter:
                    row, target = I._target(jobs, self.attempt)
                    self.assertEqual(I.move_parked_consumer(jobs, row, target, 'pending'), {})
                    adapter.assert_called_once_with(harness)

    def test_terminal_admission_race_and_completed_receipt_are_separate(self):
        I.submit(self.jobs, self.attempt, 'reuse')
        self.assertFalse(self.control.terminal_boundary())
        prompt = self.control.prepare('current context')
        self.assertIn('reuse', prompt)
        self.control.started('turn-1')
        self.assertEqual(self.state()[0]['state'], 'accepted')
        self.control.completed('turn-1')
        self.assertEqual(self.state()[0]['state'], 'turn-completed')
        self.assertTrue(self.control.terminal_boundary())
        with self.assertRaisesRegex(I.InputError, 'unavailable'):
            I.submit(self.jobs, self.attempt, 'late correction')
        self.assertEqual(I.inspect(self.jobs, self.attempt)['applied'], 'not-verified')

    def test_restart_recovers_unsent_input_but_never_replays_unknown_send(self):
        I.submit(self.jobs, self.attempt, 'first', 'first')
        self.control.take('active-turn')
        I.submit(self.jobs, self.attempt, 'second', 'second')
        recovered = I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
        self.assertEqual([r['state'] for r in self.state()], ['delivery-unknown', 'queued'])
        prompt = recovered.prepare('resume')
        self.assertNotIn('"text": "first"', prompt)
        self.assertIn('"text": "second"', prompt)
        with self.assertRaisesRegex(I.InputError, 'consumer-changed'):
            self.control.prepare('stale consumer')

    def test_thread_change_cannot_silently_retarget_queued_input(self):
        I.submit(self.jobs, self.attempt, 'old thread')
        I.OwnerInput(self.jobs, self.attempt, 'thread-2', 'codex-active-turn', self.events.append)
        self.assertEqual(self.state()[0]['state'], 'undelivered')
        self.assertEqual(self.state()[0]['reason'], 'owner-thread-changed')

    def test_unknown_send_and_unsent_input_survive_shutdown(self):
        I.submit(self.jobs, self.attempt, 'unknown', 'unknown')
        self.control.take('active-turn')
        I.submit(self.jobs, self.attempt, 'unsent', 'unsent')
        self.control.close()
        self.assertEqual([r['state'] for r in self.state()], ['delivery-unknown', 'undelivered'])
        self.assertTrue(I.unresolved(self.jobs, self.attempt))

    def test_old_supervisor_is_rejected_without_queueing(self):
        I._path(self.jobs, self.attempt).unlink()
        with self.assertRaisesRegex(I.InputError, 'unsupported'):
            I.submit(self.jobs, self.attempt, 'new input')
        self.assertFalse(I._path(self.jobs, self.attempt).exists())

    def test_first_native_session_receipt_binds_the_returned_identity(self):
        control=I.OwnerInput(self.jobs,self.attempt,'pending-native-session','opencode-next-turn',self.events.append)
        I.submit(self.jobs,self.attempt,'initial correction')
        control.prepare('first turn')
        control.bind_initial_thread('ses_returned')
        control.started('1')
        self.assertEqual(self.state()[0]['thread_id'],'ses_returned')

    def test_rejection_defers_to_same_owner_next_turn(self):
        I.submit(self.jobs, self.attempt, 'deferred')
        server = SimpleNamespace(next_id=1, send=lambda value: None)
        self.control.active_turn = 'one'
        self.control.tick(server)
        self.assertTrue(self.control.response({'id': 1, 'error': {'message': 'no active turn'}}))
        self.assertEqual(self.state()[0]['state'], 'queued')
        self.assertIn('deferred', self.control.prepare('next existing resume'))

    def test_same_contract_for_cli_transports(self):
        for harness in ('claude', 'opencode'):
            with self.subTest(harness=harness):
                control = I.OwnerInput(self.jobs, self.attempt, 'thread-1', harness+'-next-turn', self.events.append)
                I.submit(self.jobs, self.attempt, 'correction '+harness, harness)
                prompt = control.prepare('same-session continuation')
                self.assertIn('correction '+harness, prompt)
                control.started(harness+'-turn')
                control.completed(harness+'-turn')
                self.assertEqual(self.state()[-1]['state'], 'turn-completed')

    def test_undelivered_notice_uses_existing_carrier_and_survives_terminal_result(self):
        import dispatch_supervision as supervision
        import dispatch_completion_join as join
        I._path(self.jobs, self.attempt).unlink()
        self.jobs.write_text(self.jobs.read_text().rstrip() +
            ",parent_sid=0100000c-0000-7000-8000-000000000014,"
            "parent_completion_delivery=codex-managed-gateway,managed_sealed_batch_id=batch-test\n")
        control = I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
        I.submit(self.jobs, self.attempt, 'preserve this correction')
        control.take('active-turn')
        control.close()
        records = list((self.root/'pending-delivery').glob('*/*.json'))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertTrue(supervision.notice_is_current(record))
        self.assertIn('correct --jobs', supervision.render_text(record['receipt']))
        self.jobs.write_text(self.jobs.read_text().replace('\topen\t', '\tdone\t'))
        before = self.jobs.read_bytes()
        join.materialize_after_terminal_close(self.jobs, self.attempt)
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertTrue(supervision.notice_is_current(record))
        self.assertEqual(len(list((self.root/'pending-delivery').glob('*/*.json'))), 1)

    def test_actual_consumer_death_stores_new_input_without_replaying_unknown_send(self):
        child_root = self.root/'dead-consumer'; child_root.mkdir()
        jobs = child_root/'jobs.log'
        lease = supervisor_lease_path(jobs, self.attempt)
        jobs.write_text(FIXTURE.owner_row(lease).rstrip() +
            ",parent_sid=0100000c-0000-7000-8000-000000000014,"
            "parent_completion_delivery=codex-managed-gateway,managed_sealed_batch_id=batch-test\n")
        script = child_root/'consumer.py'
        script.write_text("""import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import dispatch_owner_input as I
from dispatch_contract import hold_supervisor_lease,supervisor_lease_path
jobs=Path(sys.argv[2]); attempt=sys.argv[3]
with hold_supervisor_lease(jobs,attempt,supervisor_lease_path(jobs,attempt)):
 c=I.OwnerInput(jobs,attempt,'thread-1','codex-active-turn',lambda v:None)
 I.submit(jobs,attempt,'one correction')
 c.take('active-turn')
 print('sending',flush=True)
 sys.stdin.read()
""")
        child = subprocess.Popen([sys.executable,str(script),str(ROOT/'utilities'),str(jobs),self.attempt],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'sending')
            before = json.loads(I._path(jobs,self.attempt).read_text())
            child.kill(); child.wait(timeout=5)
            observed = I.inspect(jobs,self.attempt)
            self.assertFalse(observed['supervisor_live'])
            self.assertEqual(observed['requests'][0]['delivery_observation'], 'delivery-unknown')
            self.assertTrue(I.unresolved(jobs,self.attempt))
            receipt = I.submit(jobs,self.attempt,'another correction')
            requests = I.inspect(jobs,self.attempt)['requests']
            self.assertEqual([item['state'] for item in requests], ['sending', 'queued'])
            self.assertEqual(requests[0]['delivery_observation'], 'delivery-unknown')
            self.assertEqual(requests[0]['id'], before['requests'][0]['id'])
            self.assertEqual(requests[0]['events'], before['requests'][0]['events'])
            self.assertIn(receipt['consumer_recovery'], {
                'awaiting-parent', 'not-started:owner-launch-not-claimed'})
            self.assertTrue(I.unresolved(jobs,self.attempt))
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=5)
            child.stdin.close(); child.stdout.close()

    def test_parked_consumer_of_an_earlier_release_moves_and_keeps_its_resource(self):
        import dispatch_contract as contract
        import dispatch_completion_join as join
        import route_authority
        import route_parent_close
        child_root = self.root/'release-move'; (child_root/'old/utilities').mkdir(parents=True)
        jobs = child_root/'jobs.log'
        lease = supervisor_lease_path(jobs, self.attempt)
        jobs.write_text(FIXTURE.owner_row(lease))
        I.initialize_owner_input(jobs, self.attempt, 'codex-active-turn')
        governor = child_root/'old/utilities/model-worker-governor.py'
        governor.write_text("""import subprocess,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import dispatch_owner_input as I
from dispatch_contract import hold_supervisor_lease,supervisor_lease_path
jobs=Path(sys.argv[2]); attempt=sys.argv[3]
with hold_supervisor_lease(jobs,attempt,supervisor_lease_path(jobs,attempt)):
 I.OwnerInput(jobs,attempt,'thread-1','codex-active-turn',lambda v:None)
 server=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
 print(server.pid,flush=True)
 time.sleep(60)
""")
        tagged = {**os.environ, 'AGENT_DISPATCH_ATTEMPT_ID': self.attempt}
        payload = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   start_new_session=True, env=tagged)
        consumer = subprocess.Popen([sys.executable, str(governor), str(ROOT/'utilities'), str(jobs),
                                     self.attempt], start_new_session=True, env=tagged,
                                    stdout=subprocess.PIPE, text=True)
        def stop(process):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(stop, payload)
        self.addCleanup(stop, consumer)
        server = int(consumer.stdout.readline())
        self.addCleanup(consumer.stdout.close)
        _, start, _ = contract.process_observation(consumer.pid)
        _, payload_start, _ = contract.process_observation(payload.pid)
        namespace = os.readlink('/proc/self/ns/pid')
        jobs.write_text(jobs.read_text().replace('attempt_id=' + self.attempt,
            f'launch_claimed=1,launch_started=1,pid={consumer.pid},pid_start={start},'
            f'pgid={consumer.pid},pid_ns={namespace},pid_observer_ns={namespace},'
            f'attempt_id={self.attempt}'))
        join.write_supervisor_state(lease.with_suffix('.json'), self.attempt, set(), phase='parked')
        state = json.loads(lease.with_suffix('.json').read_text())
        state['resource'] = {'session_id': 'thread-1', 'delivered': [], 'outbox': None}
        lease.with_suffix('.json').write_text(json.dumps(state))
        resources = [{'kind': 'resource', 'run_id': 'var', 'registry': str(child_root/'runs.json'),
                      'row': {'run_id': 'var', 'pid': payload.pid, 'starttime': payload_start,
                              'launch_state': 'started'}}]
        calls = []
        def respawn(jobs_path, attempt, *, expected_identity, check_only=False):
            calls.append((expected_identity, check_only))
            return {'state': 'ready'} if check_only else {
                'state': 'respawned', 'pid': '1', 'pid_start': '1',
                'respawn_release': str(ROOT)}
        with mock.patch.object(I, '_installed_release', return_value=ROOT.resolve()), \
             mock.patch.object(I, '_respawn_adapter', return_value=respawn), \
             mock.patch.object(route_authority, 'owns', return_value=True), \
             mock.patch.object(route_parent_close, 'row_requested', return_value=False), \
             mock.patch.object(route_parent_close, 'owner_resources', return_value=resources):
            # An input being sent, or a turn running, keeps the consumer as it is.
            channel = I._path(jobs, self.attempt)
            idle = channel.read_bytes()
            busy = json.loads(idle)
            busy['requests'] = [{'id': 'in-flight', 'digest': '0', 'state': 'sending', 'events': []}]
            channel.write_text(json.dumps(busy))
            self.assertEqual(I.move_parked_consumer(jobs, *I._target(jobs, self.attempt), 'resume'), {})
            channel.write_bytes(idle)
            join.write_supervisor_state(lease.with_suffix('.json'), self.attempt, set(),
                                        phase='running-turn')
            self.assertEqual(I.move_parked_consumer(jobs, *I._target(jobs, self.attempt), 'resume'), {})
            lease.with_suffix('.json').write_text(json.dumps(state))
            self.assertIsNone(consumer.poll())
            receipt = I.submit(jobs, self.attempt, 'resume the fixed run on the installed release', 'resume')
        self.assertEqual(receipt['consumer_release_moved_from'], str((child_root/'old').resolve()))
        self.assertEqual(receipt['consumer_recovery'], 'respawned', receipt)
        expected = (str(consumer.pid), start)
        self.assertEqual(calls[-2:], [(expected, True), (expected, False)])
        self.assertEqual(consumer.wait(timeout=5), -15)
        deadline = time.monotonic() + 5
        while contract.process_observation(server)[0] == 'present' and time.monotonic() < deadline:
            if contract.process_observation(server)[2] == 'Z':
                break
            time.sleep(.02)
        self.assertNotEqual(contract.process_observation(server)[0:3:2], ('present', 'S'))
        self.assertIsNone(payload.poll())
        self.assertEqual([item['state'] for item in I.inspect(jobs, self.attempt)['requests']], ['queued'])
        self.assertIn('\topen\t', jobs.read_text())

    def test_cli_supervisor_delivers_correction_before_terminal_for_both_transports(self):
        fixture = load('owner_input_cli_fixture', ROOT/'utilities/claude_session_supervisor.test.py')
        for harness in ('claude', 'opencode'):
            with self.subTest(harness=harness):
                f = fixture.ClaudeSessionSupervisorTest('test_no_child_finishes_without_resume')
                f.setUp()
                try:
                    f.jobs.write_text(fixture.owner_row(f.lease).replace('harness=claude', 'harness='+harness))
                    native = f.base/'input-runtime.py'
                    native.write_text("""import json,sys,time
from pathlib import Path
prompt=sys.stdin.read()
trace=Path('input-trace')
first=not trace.exists()
with trace.open('a') as out: out.write(json.dumps({'args':sys.argv,'prompt':prompt})+'\\n')
if first:
 Path('input-ready').touch()
 deadline=time.monotonic()+8
 while not Path('input-release').exists() and time.monotonic()<deadline: time.sleep(.01)
text='artifact: -\\nverdict: PASS\\nblocker: none'
if '--format' in sys.argv:
 for kind,part in [('step_start',{}),('text',{'text':text}),('step_finish',{'reason':'stop'})]:
  print(json.dumps({'type':kind,'sessionID':'ses_input','part':part}),flush=True)
else:
 print(json.dumps({'type':'result','is_error':False,'result':text}),flush=True)
""")
                    command = f.command()
                    option = '--claude-command' if harness=='claude' else '--opencode-command'
                    if option in command:
                        command[command.index(option)+1] = shlex.join([sys.executable,str(native)])
                    else:
                        command += [option,shlex.join([sys.executable,str(native)])]
                    if harness=='opencode': command += ['--runtime-harness','opencode']
                    process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE,text=True,env=f.child_env())
                    process.stdin.write('initial work'); process.stdin.close(); process.stdin=None
                    try:
                        deadline=time.monotonic()+8
                        while not (f.base/'input-ready').exists() and time.monotonic()<deadline:
                            if process.poll() is not None: break
                            time.sleep(.01)
                        self.assertTrue((f.base/'input-ready').exists())
                        I.submit(f.jobs,fixture.PARENT,'reuse existing source; do not repeat planning','correction')
                        (f.base/'input-release').touch()
                        out,err=process.communicate(timeout=10)
                        self.assertEqual(process.returncode,0,out+err)
                        turns=[json.loads(line) for line in (f.base/'input-trace').read_text().splitlines()]
                        self.assertEqual(len(turns),2)
                        self.assertIn('reuse existing source',turns[1]['prompt'])
                        self.assertIn('--resume' if harness=='claude' else '--session',turns[1]['args'])
                        receipt=I.inspect(f.jobs,fixture.PARENT)
                        self.assertEqual(receipt['requests'][0]['state'],'turn-completed')
                        self.assertIn('completed-supervisor',f.jobs.read_text())
                    finally:
                        if process.poll() is None: process.kill(); process.communicate(timeout=5)
                finally:
                    f.tearDown()
                    f.doCleanups()

    def test_pending_input_yields_serial_successor_decision_to_existing_owner(self):
        import dispatch_subsession_advance as advance
        from unittest import mock
        I.submit(self.jobs,self.attempt,'change the remaining implementation')
        row=SimpleNamespace(attempt_id='att-slice',status='done',metadata={
            'session_chain_id':'chain-1','subsession_mode':'serial'})
        receipt={'state':'ready','children':[]}
        with mock.patch.object(advance,'advance_chain_step') as launch:
            result=advance.drive_serial_chain(jobs=self.jobs,parent_attempt_id=self.attempt,
                attempts={'att-slice'},receipt=receipt,refresh=lambda attempts:[row],
                join=lambda attempts:receipt,reconcile=lambda rows,attempts:False,max_reparks=1,
                allow_advance=lambda:not self.control.pending())
        launch.assert_not_called()
        self.assertEqual(result.receipt,receipt)
        self.assertIsNone(result.refusal)
        self.assertTrue(self.control.pending())

    def test_real_pipe_cli_submission_reaches_only_exact_active_turn(self):
        script = self.root / 'server.py'
        ready = self.root / 'ready'
        script.write_text('''import json,sys
from pathlib import Path
def send(x): print(json.dumps(x),flush=True)
for line in sys.stdin:
 v=json.loads(line); m=v['method']
 if m=='turn/start':
  send({'id':v['id'],'result':{'turn':{'id':'active-one'}}})
  Path(sys.argv[1]).write_text('ready')
 elif m=='turn/steer':
  assert v['params']['threadId']=='thread-1'
  assert v['params']['expectedTurnId']=='active-one'
  assert 'reuse existing code' in v['params']['input'][0]['text']
  send({'id':v['id'],'result':{'turnId':'active-one'}})
  send({'method':'item/completed','params':{'turnId':'active-one','item':{'type':'agentMessage','text':'correction received'}}})
  send({'method':'turn/completed','params':{'turn':{'id':'active-one','status':'completed'}}})
''')
        server = C.AppServer([sys.executable, str(script), str(ready)], str(self.root), dict(os.environ))
        self.addCleanup(server.close)
        server.input_control = self.control
        args = SimpleNamespace(worktree=str(self.root), sandbox='read-only', network_access=False,
                               writable_root=[], approval='never', model=None, reasoning=None, owner_input=self.control)
        failures = []
        def submit():
            deadline = time.monotonic()+10
            while not ready.exists() and time.monotonic()<deadline:
                time.sleep(.01)
            try:
                body = self.root / 'correction.txt'; body.write_text('reuse existing code')
                result = subprocess.run([sys.executable, str(ROOT/'utilities/capability-route.py'), 'correct',
                                         '--jobs', str(self.jobs), '--attempt-id', self.attempt,
                                         '--message-file', str(body)], capture_output=True, text=True, timeout=10)
                if result.returncode:
                    failures.append(result.stdout+result.stderr)
                    server.process.terminate()
            except Exception as exc:
                failures.append(str(exc)); server.process.terminate()
        sender = threading.Thread(target=submit); sender.start()
        try:
            final, _ = C.run_turn(server, thread_id='thread-1', prompt='wait for correction', args=args)
        finally:
            sender.join(timeout=12)
        self.assertEqual(failures, [])
        self.assertEqual(final, 'correction received')
        self.assertEqual(self.state()[0]['state'], 'turn-completed')
        self.assertEqual(self.state()[0]['turn_id'], 'active-one')


class RegisteredOwnerInputTest(unittest.TestCase):
    """Input admitted between registration and the first consumer."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs.log'
        self.attempt = FIXTURE.PARENT
        self.lease = supervisor_lease_path(self.jobs, self.attempt)
        self.jobs.write_text(FIXTURE.owner_row(self.lease))
        self.events = []

    def close_row(self):
        self.jobs.write_text(self.jobs.read_text().replace('\topen\t', '\tdone\t'))

    def state_file(self):
        return I._path(self.jobs, self.attempt)

    def test_initialize_is_idempotent_and_keeps_queued_input(self):
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        first = json.loads(self.state_file().read_text())
        self.assertEqual(first['thread_id'], I.PLACEHOLDER_THREAD)
        self.assertNotIn('generation', first)
        self.assertTrue(first['accepting'])
        I.submit(self.jobs, self.attempt, 'before the first turn', 'early')
        before = self.state_file().read_bytes()
        I.initialize_owner_input(self.jobs, self.attempt, 'claude-next-turn')
        self.assertEqual(self.state_file().read_bytes(), before)

    def test_correction_before_first_consumer_is_accepted_and_observed_queued(self):
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        receipt = I.submit(self.jobs, self.attempt, 'early word', 'early')
        self.assertTrue(receipt['accepting'])
        self.assertFalse(receipt['supervisor_live'])
        observed = I.inspect(self.jobs, self.attempt)
        self.assertTrue(observed['accepting'])
        self.assertFalse(observed['supervisor_live'])
        self.assertEqual(observed['requests'][0]['state'], 'queued')
        self.assertEqual(observed['requests'][0]['delivery_observation'], 'queued')
        self.assertFalse(I.unresolved(self.jobs, self.attempt))
        again = I.submit(self.jobs, self.attempt, 'early word', 'early')
        self.assertTrue(again['duplicate'])
        with self.assertRaisesRegex(I.InputError, 'content-conflict'):
            I.submit(self.jobs, self.attempt, 'other word', 'early')

    def test_first_consumer_inherits_the_queue_for_any_real_thread(self):
        for thread, transport in (('thread-real', 'codex-active-turn'),
                                  ('11111111-2222-3333-4444-555555555555', 'claude-next-turn'),
                                  (I.PLACEHOLDER_THREAD, 'opencode-next-turn')):
            with self.subTest(thread=thread):
                self.setUp()
                I.initialize_owner_input(self.jobs, self.attempt, transport)
                I.submit(self.jobs, self.attempt, 'early word', 'early')
                with hold_supervisor_lease(self.jobs, self.attempt, self.lease):
                    control = I.OwnerInput(self.jobs, self.attempt, thread, transport, self.events.append)
                    self.assertEqual(I.inspect(self.jobs, self.attempt)['requests'][0]['state'], 'queued')
                    self.assertIn('early word', control.prepare('first turn'))
                    if thread == I.PLACEHOLDER_THREAD:
                        control.bind_initial_thread('ses_real')
                    control.started('turn-1')
                    control.completed('turn-1')
                    receipt = I.inspect(self.jobs, self.attempt)
                    self.assertEqual(receipt['requests'][0]['state'], 'turn-completed')
                    self.assertEqual(receipt['requests'][0]['thread_id'],
                                     'ses_real' if thread == I.PLACEHOLDER_THREAD else thread)

    def test_consumer_that_attached_and_died_stores_for_parent_recovery(self):
        self.jobs.write_text(self.jobs.read_text().rstrip() +
            ",parent_sid=0100000c-0000-7000-8000-000000000014,"
            "parent_completion_delivery=codex-managed-gateway,managed_sealed_batch_id=batch-test\n")
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        with hold_supervisor_lease(self.jobs, self.attempt, self.lease):
            I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
        # The consumer is gone but the row is still open.
        first = I.submit(self.jobs, self.attempt, 'queued for same attempt')
        self.assertEqual(first['consumer_recovery'], 'awaiting-parent')
        self.assertEqual(I.inspect(self.jobs, self.attempt)['requests'][0]['state'], 'queued')
        before = self.state_file().read_bytes()
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        self.assertEqual(self.state_file().read_bytes(), before)
        second = I.submit(self.jobs, self.attempt, 'another queued correction')
        self.assertEqual(second['consumer_recovery'], 'awaiting-parent')
        self.assertEqual([item['state'] for item in I.inspect(self.jobs, self.attempt)['requests']],
                         ['queued', 'queued'])

    def test_duplicate_delivery_unknown_is_not_retransmitted_or_used_to_start(self):
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        with hold_supervisor_lease(self.jobs, self.attempt, self.lease):
            control = I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
            I.submit(self.jobs, self.attempt, 'possibly delivered', 'uncertain')
            control.take('active-turn')
            control.close()
            before = self.state_file().read_bytes()
            with mock.patch.object(I, 'recover_consumer') as recover:
                receipt = I.submit(self.jobs, self.attempt, 'possibly delivered', 'uncertain')
            recover.assert_not_called()
        self.assertEqual(receipt['consumer_recovery'], 'not-started:delivery-unknown-no-replay')
        self.assertEqual(self.state_file().read_bytes(), before)

    def test_non_parent_stores_once_and_leaves_existing_parent_notice(self):
        self.jobs.write_text(self.jobs.read_text().rstrip() +
            ",parent_sid=0100000c-0000-7000-8000-000000000014,"
            "parent_completion_delivery=codex-managed-gateway,managed_sealed_batch_id=batch-test\n")
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
        import route_authority
        import dispatch_completion_join as join
        with patch_split(I, 'supervisor_lease_is_held', return_value=False), \
             mock.patch.object(join, 'settle_finished_attempt'), \
             mock.patch.object(route_authority, 'owns', return_value=False), \
             mock.patch.object(I, 'recover_consumer', wraps=I.recover_consumer) as recover:
            receipt = I.submit(self.jobs, self.attempt, 'keep for effective parent', 'parent-only')
        recover.assert_called_once()
        self.assertEqual(receipt['consumer_recovery'], 'awaiting-parent')
        self.assertEqual(I.inspect(self.jobs, self.attempt)['requests'][0]['state'], 'queued')
        self.assertEqual(len(list((self.root/'pending-delivery').glob('*/*.json'))), 1)

    def test_an_unreadable_lineage_keeps_the_correction_queued_without_naming_another_parent(self):
        # RV-OWNS-TRI: unknown is neither the parent nor "awaiting" another one.
        self.jobs.write_text(self.jobs.read_text().rstrip() +
            ",parent_sid=0100000c-0000-7000-8000-000000000014,"
            "parent_completion_delivery=codex-managed-gateway,managed_sealed_batch_id=batch-test\n")
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
        import route_authority
        import dispatch_completion_join as join
        with patch_split(I, 'supervisor_lease_is_held', return_value=False), \
             mock.patch.object(join, 'settle_finished_attempt'), \
             mock.patch.object(route_authority, 'owns', return_value=None):
            receipt = I.submit(self.jobs, self.attempt, 'keep until the lineage reads', 'lineage-unknown')
        self.assertEqual(receipt['consumer_recovery'], 'not-started:handover-unreadable')
        self.assertIn('run the same command', receipt['consumer_retry_when'])
        self.assertIn(self.attempt, receipt['consumer_detail'])
        self.assertNotIn('restore this same owner', receipt.get('next_step', ''))
        self.assertEqual(I.inspect(self.jobs, self.attempt)['requests'][0]['state'], 'queued')

    def test_consumer_that_never_attached_leaves_an_unresolved_undelivered_input(self):
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        I.submit(self.jobs, self.attempt, 'never delivered', 'early')
        self.assertFalse(I.unresolved(self.jobs, self.attempt))
        self.close_row()
        self.assertTrue(I.unresolved(self.jobs, self.attempt))
        observed = I.inspect(self.jobs, self.attempt)
        self.assertFalse(observed['accepting'])
        self.assertFalse(observed['supervisor_live'])
        self.assertEqual(observed['requests'][0]['state'], 'queued')
        self.assertEqual(observed['requests'][0]['delivery_observation'], 'undelivered')
        with self.assertRaisesRegex(I.InputError, 'unavailable'):
            I.submit(self.jobs, self.attempt, 'after the row closed')

    def test_initialize_refuses_closed_non_owner_and_changed_identity(self):
        self.close_row()
        with self.assertRaisesRegex(I.InputError, 'unavailable'):
            I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        self.assertFalse(self.state_file().exists())
        self.setUp()
        self.jobs.write_text(self.jobs.read_text().replace('worker_type=owner', 'worker_type=stage'))
        with self.assertRaisesRegex(I.InputError, 'not-owner'):
            I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        self.assertFalse(self.state_file().exists())
        self.setUp()
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        with hold_supervisor_lease(self.jobs, self.attempt, self.lease):
            I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
        self.jobs.write_text(self.jobs.read_text().replace('d' * 64, 'e' * 64))
        with self.assertRaisesRegex(I.InputError, 'target-changed'):
            I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        with self.assertRaisesRegex(I.InputError, 'target-changed'):
            I.submit(self.jobs, self.attempt, 'wrong nonce')

    def test_relaunched_row_keeps_input_queued_before_the_first_consumer(self):
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        I.submit(self.jobs, self.attempt, 'before the relaunch', 'early')
        # The next launcher of the same never-claimed attempt mints its own lease nonce.
        self.jobs.write_text(self.jobs.read_text().replace('d' * 64, 'e' * 64))
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        observed = I.inspect(self.jobs, self.attempt)
        self.assertTrue(observed['accepting'])
        self.assertEqual([item['state'] for item in observed['requests']], ['queued'])
        I.submit(self.jobs, self.attempt, 'after the relaunch', 'late')
        # A consumer binding after the row changed again inherits the same queue.
        self.jobs.write_text(self.jobs.read_text().replace('e' * 64, 'f' * 64))
        with hold_supervisor_lease(self.jobs, self.attempt, self.lease):
            consumer = I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
            self.assertTrue(consumer.pending())
        states = {item['id']: item['state'] for item in I.inspect(self.jobs, self.attempt)['requests']}
        self.assertEqual(states, {'early': 'queued', 'late': 'queued'})

    def test_unsupported_owner_creates_no_lock_or_state(self):
        for call in (lambda: I.inspect(self.jobs, self.attempt),
                     lambda: I.submit(self.jobs, self.attempt, 'text'),
                     lambda: I.unresolved(self.jobs, self.attempt)):
            try:
                self.assertFalse(call())
            except I.InputError as exc:
                self.assertIn('unsupported', str(exc))
        self.assertEqual(sorted(p.name for p in self.root.rglob('*.input.json*')), [])

    def end_row(self, note):
        self.jobs.write_text(self.jobs.read_text().replace('\topen\t', '\tdone\t')
                             .replace('\n', f',note={note}\n', 1))

    def test_an_answer_to_an_owner_that_ended_blocked_is_kept_for_its_continuation(self):
        I.initialize_owner_input(self.jobs, self.attempt, 'claude-next-turn')
        self.end_row('dead-worker-blocked')
        result = I.submit(self.jobs, self.attempt, 'approved: start the full run', 'approval')
        self.assertTrue(result['retained'])
        self.assertFalse(result['duplicate'])
        self.assertIn('replacement owner', result['next_step'])
        self.assertEqual([item['state'] for item in result['requests']], ['retained'])
        self.assertEqual(I.retained(self.jobs, self.attempt),
                         [{'id': 'approval', 'digest': I._digest('approved: start the full run'),
                           'text': 'approved: start the full run'}])
        again = I.submit(self.jobs, self.attempt, 'approved: start the full run', 'approval')
        self.assertTrue(again['duplicate'] and again['retained'])
        self.assertEqual(len(I.retained(self.jobs, self.attempt)), 1)
        # A kept answer is not undelivered input: no supervision notice is raised for it.
        self.assertFalse(I.unresolved(self.jobs, self.attempt))
        self.assertEqual(I.inspect(self.jobs, self.attempt)['requests'][0]['delivery_observation'], 'retained')

    def test_an_owner_that_ends_blocked_while_the_answer_is_sent_still_gets_it(self):
        """The row read before the input lock said open; it ended BLOCKED before the decision."""
        import dataclasses
        from unittest import mock
        I.initialize_owner_input(self.jobs, self.attempt, 'claude-next-turn')
        self.end_row('dead-worker-blocked')
        real = I._target
        reads = []
        def stale_then_real(jobs, attempt):
            row, target = real(jobs, attempt)
            reads.append(row.status)
            return (dataclasses.replace(row, status='open'), target) if len(reads) == 1 else (row, target)
        with mock.patch.object(I, '_target', side_effect=stale_then_real):
            result = I.submit(self.jobs, self.attempt, 'approved', 'raced')
        self.assertEqual(len(reads), 2)
        self.assertTrue(result['retained'])
        self.assertEqual([item['id'] for item in I.retained(self.jobs, self.attempt)], ['raced'])

    def test_input_queued_before_the_owner_ended_blocked_is_still_its_answer(self):
        I.initialize_owner_input(self.jobs, self.attempt, 'claude-next-turn')
        I.submit(self.jobs, self.attempt, 'approved, sent while it was finishing', 'early')
        self.assertEqual(I.blocked_owner_answers(self.jobs, self.attempt), [])   # still open: not an answer yet
        self.end_row('dead-worker-blocked')
        self.assertEqual([item['id'] for item in I.blocked_owner_answers(self.jobs, self.attempt)], ['early'])

    def test_an_answer_to_any_other_ended_owner_is_still_not_admitted(self):
        for note in ('dead-worker-fail', 'completed-supervisor', 'dead-exact-pid'):
            with self.subTest(note=note):
                self.setUp()
                I.initialize_owner_input(self.jobs, self.attempt, 'claude-next-turn')
                self.end_row(note)
                with self.assertRaisesRegex(I.InputError, 'unavailable-retain-correction'):
                    I.submit(self.jobs, self.attempt, 'too late')
                self.assertEqual(I.retained(self.jobs, self.attempt), [])

    def test_submissions_racing_the_first_consumer_are_all_kept(self):
        I.initialize_owner_input(self.jobs, self.attempt, 'codex-active-turn')
        errors = []
        def send(n):
            try:
                I.submit(self.jobs, self.attempt, 'word %d' % n, 'r%d' % n)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=send, args=(n,)) for n in range(6)]
        with hold_supervisor_lease(self.jobs, self.attempt, self.lease):
            for t in threads[:3]: t.start()
            control = I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
            for t in threads[3:]: t.start()
            for t in threads: t.join()
            self.assertEqual(errors, [])
            prompt = control.prepare('first turn')
            states = {r['id']: r['state'] for r in I.inspect(self.jobs, self.attempt)['requests']}
            self.assertEqual(len(states), 6)
            self.assertEqual(set(states.values()), {'sending'})
            self.assertEqual(sum('"text": "word' in line for line in prompt.splitlines()), 6)


class OwnerFinishCorrectionRegressionTest(OwnerInputTest):
    def test_parked_queued_response_explains_next_turn_delay_and_no_cancel(self):
        from unittest import mock
        phase = self.root / 'phase.json'
        phase.write_text(json.dumps({'schema_version': 2, 'parent_attempt_id': self.attempt,
                                     'phase': 'parked', 'delivered_attempt_ids': []}))
        I.submit(self.jobs, self.attempt, 'private correction text', 'notice')
        with mock.patch.dict(os.environ, {'AGENT_DISPATCH_COMPLETION_STATE_FILE': str(phase)}):
            result = I.inspect(self.jobs, self.attempt)
        self.assertEqual(result.get('owner_phase'), 'parked')
        self.assertEqual(result.get('delivery_timing'), 'next-owner-turn')
        notice = result.get('delivery_notice', '').lower()
        self.assertIn('next owner turn', notice)
        self.assertIn('delay', notice)
        self.assertIn('effective parent', notice)
        self.assertIn('never replayed', notice)
        self.assertNotIn('does not wake or cancel', notice)
        # BC rt-96bab699: read as "after the whole chain"; the chain stops at the running sub-session.
        self.assertIn('starts no further sub-session', notice)
        self.assertNotIn('private correction text', json.dumps(result))

    def test_codex_active_turn_without_exact_phase_reports_unavailable_timing(self):
        from unittest import mock
        I.submit(self.jobs, self.attempt, 'private correction text', 'phase-missing')
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AGENT_DISPATCH_COMPLETION_STATE_FILE', None)
            result = I.inspect(self.jobs, self.attempt)
        self.assertEqual(result.get('owner_phase'), 'unknown')
        self.assertEqual(result.get('delivery_timing'), 'phase-unavailable')
        self.assertIn('timing is unknown', result.get('delivery_notice', '').lower())
        self.assertIn('may wait for the next owner turn', result.get('delivery_notice', '').lower())
        self.assertNotIn('are delayed', result.get('delivery_notice', '').lower())
        self.assertNotIn('private correction text', json.dumps(result))

    def test_codex_active_turn_with_exact_running_phase_reports_active_turn(self):
        from unittest import mock
        phase = self.root / 'phase-running.json'
        phase.write_text(json.dumps({'schema_version': 2, 'parent_attempt_id': self.attempt,
                                     'phase': 'running-turn', 'delivered_attempt_ids': []}))
        I.submit(self.jobs, self.attempt, 'private correction text', 'phase-running')
        with mock.patch.dict(os.environ, {'AGENT_DISPATCH_COMPLETION_STATE_FILE': str(phase)}):
            result = I.inspect(self.jobs, self.attempt)
        self.assertEqual(result.get('owner_phase'), 'running-turn')
        self.assertEqual(result.get('delivery_timing'), 'active-turn')
        notice = result.get('delivery_notice', '').lower()
        self.assertIn("goes into the owner's running turn", notice)
        self.assertNotIn('are delayed', notice)

    def test_queued_claude_and_opencode_remain_next_owner_turn(self):
        for transport in ('claude-next-turn', 'opencode-next-turn'):
            with self.subTest(transport=transport):
                value = json.loads(I._path(self.jobs, self.attempt).read_text())
                value['transport'] = transport
                result = I._public(value, owner_phase='unknown')
                self.assertEqual(result.get('delivery_timing'), 'next-owner-turn')


if __name__ == '__main__':
    unittest.main()
