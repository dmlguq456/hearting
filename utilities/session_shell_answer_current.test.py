"""Wrapped answers retain current-root admission through the real notice/queue path."""
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'utilities'), str(ROOT/'tools')]
import session_shell as shell
import human_wait_state as waits
import questions
import session_notice
import codex_queue_delivery as queue
import codex_queue_dispatch
from fixtures.message_questions import MessageQuestionFixture
from fleet.collectors import codex


class CurrentAnswerTests(unittest.TestCase):
    def setUp(self):
        self.fx = MessageQuestionFixture().__enter__()
        self.addCleanup(self.fx.__exit__, None, None, None)
        private = {name:str(self.fx.root/leaf) for name, leaf in (('HOME','home'),('CODEX_HOME','codex'))}
        for value in private.values():
            Path(value).mkdir()
        self.fx.env.update(private); os.environ.update(private)
        self.native = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.buffer.read()'], stdin=subprocess.PIPE)
        def finish():
            self.native.stdin.close()
            self.native.wait(timeout=3)
        self.addCleanup(finish)
        self.sid = '00000000-0000-4000-8000-000000000052'
        self.selected = self.sid
        me, native = shell.process(os.getpid()), shell.process(self.native.pid)
        self.binding = dict(schema=shell.SCHEMA, harness='codex', sid=self.sid,
            incarnation='a'*48, host_pid=me['pid'], host_start=me['start'],
            native_pid=native['pid'], native_start=native['start'], socket=str(self.fx.root/'control.sock'))
        self.assertTrue(shell.publish_binding(self.binding))
        patch = mock.patch.object(codex, 'session_id_of_process', side_effect=lambda _pid, **_kw: self.selected)
        patch.start(); self.addCleanup(patch.stop)
        self.rpcs = []

    def ask(self, label='Question before resume'):
        self.signal, self.envelope = self.fx.ask('codex', self.sid, label,
            incarnation={'pid':self.native.pid, 'proc_start':self.binding['native_start']})

    def answer(self):
        item = dict(id=self.signal['id'], owner=dict(harness='codex', session_id=self.sid))
        with mock.patch.object(waits, 'cached_view', return_value={'items':[item]}), \
             mock.patch.object(questions, 'load_board', return_value={'items':[item]}):
            return shell._block_answer(self.binding, dict(question_id=self.signal['id'],
                revision=questions.request_revision(self.envelope), answers={'message-response':['Owned answer']}))

    def deliver(self, ref, *, change=False, steer=False):
        def rpc(_endpoint, method, params, **_kw):
            self.rpcs.append(method)
            if method == 'thread/queue/list':
                return {'data':[]}
            if method == 'thread/turns/list':
                if change:
                    self.selected = None
                return {'data':[{'id':'owned-turn', 'status':'inProgress' if steer else 'completed'}]}
            if method == 'thread/queue/add':
                return {'queuedSubmission':{'id':'owned-item'}}
            if method == 'turn/steer':
                return {'turnId':'owned-turn'}
            raise AssertionError(method)
        with mock.patch.object(queue, '_rpc', side_effect=rpc), \
             mock.patch.object(queue, '_known_consumed', return_value=None), \
             mock.patch.object(codex_queue_dispatch, 'resolve_queue_socket', return_value=self.fx.root/'fixture.sock'):
            return self.fx.peer.deliver_pending_codex(ref, _promote_prior=False)

    def test_question_open_before_resume_is_not_submitted_with_unknown_root(self):
        self.ask(); self.selected = None
        result = self.answer()
        self.assertFalse(result['ok'])
        self.assertIsNone(self.fx.record(self.signal))
        self.assertEqual(waits.read_selected_request(self.envelope)['state'], 'open')

    def test_a_late_question_cannot_restore_the_old_block_answer_permission(self):
        self.selected = None; self.ask('Question registered after resume')
        result = self.answer()
        self.assertFalse(result['ok'])
        self.assertIsNone(self.fx.record(self.signal))

    def test_selection_change_after_worker_admission_stops_the_native_queue_effect(self):
        self.ask(); self.assertTrue(self.answer()['ok'])
        _text, ref, _recipient = self.fx.prepare(self.signal)
        result = self.deliver(ref, change=True)
        self.assertNotIn('thread/queue/add', self.rpcs)
        self.assertIn('current-session-unverified', result.get('reason', ''))
        self.assertEqual(self.fx.peer._read_pending(ref)['state'], 'pending')
        self.assertTrue(session_notice.notice_is_current(self.fx.record(self.signal)))
        self.assertNotEqual(waits.read_selected_request(self.envelope)['state'], 'applied')
        # The retained answer can use the same transfer when exact proof returns.
        self.selected = self.sid; self.rpcs.clear(); self.deliver(ref)
        self.assertEqual(self.rpcs.count('thread/queue/add'), 1)

    def test_a_waiting_worker_cannot_steer_into_an_unproved_current_root(self):
        self.ask(); self.assertTrue(self.answer()['ok'])
        _text, ref, _recipient = self.fx.prepare(self.signal)
        row = self.fx.peer._read_pending(ref)
        with mock.patch.object(self.fx.peer, '_codex_steer_eligible', return_value=True):
            self.deliver(ref, change=True, steer=True)
        self.assertNotIn('turn/steer', self.rpcs)
        self.assertNotIn('thread/queue/add', self.rpcs)
        self.assertEqual(self.fx.peer._read_pending(ref)['state'], row['state'])

    def test_unique_root_answer_and_exact_receipt_survive_a_later_selection_change(self):
        self.ask(); self.assertTrue(self.answer()['ok'])
        entry = waits._selected_entry(self.envelope)
        self.assertEqual(entry['answer_attachment'], self.binding)
        text, ref, _recipient = self.fx.prepare(self.signal)
        self.deliver(ref)
        self.assertEqual(self.rpcs.count('thread/queue/add'), 1)
        self.selected = None
        self.fx.consume(self.signal, text)
        self.assertEqual(waits.read_selected_request(self.envelope)['state'], 'applied')

    def test_ordinary_full_sid_answer_does_not_inherit_the_block_guard(self):
        self.ask(); self.selected = None
        self.fx.submit(self.envelope)
        _text, ref, _recipient = self.fx.prepare(self.signal)
        self.deliver(ref)
        self.assertEqual(self.rpcs.count('thread/queue/add'), 1)

    def test_native_permission_reply_rechecks_after_its_native_observation(self):
        gateway={'pid':self.native.pid,'proc_start':self.binding['native_start'],'path':str(self.fx.root/'native.sock'),'epoch':'owned'}
        ref=dict(transport='codex-managed-control',request={'id':'owned-request'},pid=gateway['pid'],proc_start=gateway['proc_start'],control_path=gateway['path'],epoch='owned')
        envelope=dict(harness='codex',session_id=self.sid,request_id='owned-request',turn_id='owned-turn',native_ref=ref)
        def observed(*args,**kwargs):
            self.selected=None
            return dict(envelope,state='open',support=True,gateway=gateway)
        with mock.patch.object(waits,'_selected_base',return_value=({'session_id':self.sid},None)),mock.patch.object(waits,'_codex_current_selected',side_effect=observed),mock.patch.object(waits,'_codex_control_request') as send:
            state,result=waits._codex_submit(envelope,{'decision':'approve'},'owned-action',answer_attachment=self.binding)
        self.assertEqual((state,result['status']),('open','not-sent'))
        send.assert_not_called()

    def test_a_provably_unsent_native_reply_leaves_its_question_editable(self):
        self.ask()
        gateway={'pid':self.native.pid,'proc_start':self.binding['native_start'],'path':str(self.fx.root/'native.sock'),'epoch':'owned'}
        native=dict(transport='codex-managed-control',request={'id':'owned-request'},pid=gateway['pid'],proc_start=gateway['proc_start'],control_path=gateway['path'],epoch='owned')
        envelope=dict(self.envelope,kind='approval',request_id='owned-request',turn_id='owned-turn',question_id='approval',native_ref=native)
        flip=[True]
        def observed(*args,**kwargs):
            if flip[0]:self.selected=None
            return dict(envelope,support=True,gateway=gateway)
        def replied(_path,payload):
            return {'action_id':payload['action_id'],'request':payload['ref'],'status':'received'}
        with mock.patch.object(waits,'_selected_base',return_value=(envelope,None)),mock.patch.object(waits,'read_selected_request',return_value=envelope),mock.patch.object(waits,'_codex_current_selected',side_effect=observed),mock.patch.object(waits,'_native_response_error',return_value=None),mock.patch.object(waits,'_codex_control_request',side_effect=replied) as send:
            result=waits.submit_selected_response(envelope,{'decision':'approve'},action_id='first',answer_attachment=self.binding)
            self.assertEqual(result['state'],'open');send.assert_not_called()
            self.assertFalse(waits._selected_entry(envelope).get('action_id'))
            self.selected=self.sid;flip[0]=False
            result=waits.submit_selected_response(envelope,{'decision':'approve'},action_id='second',answer_attachment=self.binding)
            self.assertEqual(result['state'],'received');send.assert_called_once()

    def test_unknown_or_changed_attachment_never_grants_current_root(self):
        self.assertTrue(shell.answer_attachment_current(self.binding))
        with mock.patch.object(codex,'session_id_of_process',side_effect=OSError('unavailable')):
            self.assertFalse(shell.answer_attachment_current(self.binding))
        self.assertTrue(shell.publish_binding(dict(self.binding,incarnation='b'*48)))
        self.assertFalse(shell.answer_attachment_current(self.binding))

    def test_answer_attachment_is_retained_and_cannot_be_retargeted(self):
        self.ask(); self.answer()
        entry = waits._selected_entry(self.envelope)
        original = entry['message_binding']
        without = {key:value for key,value in entry.items() if key != 'answer_attachment'}
        self.assertTrue(waits._store_selected_entry(self.envelope, without))
        self.assertEqual(waits._selected_entry(self.envelope)['answer_attachment'], self.binding)
        changed = dict(entry, answer_attachment=dict(self.binding, incarnation='b'*48))
        self.assertFalse(waits._store_selected_entry(self.envelope, changed))
        self.assertEqual(waits._selected_entry(self.envelope)['message_binding'], original)


if __name__ == '__main__':
    unittest.main()
