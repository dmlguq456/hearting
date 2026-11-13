"""Resume notices use the real courier flow with synthetic native I/O only."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

path=Path(__file__).with_name('peer_steward_prompt.test.py')
spec=importlib.util.spec_from_file_location('resume_courier_fixture',path)
fixture=importlib.util.module_from_spec(spec);sys.modules[spec.name]=fixture;spec.loader.exec_module(fixture)
import session_resume


class ResumeCourierTest(fixture._TmpRootMixin,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sid="fixture-owner-session"
        self.pane="w1:p-notice"
        self.ident={"pane":self.pane,"harness":"claude","session_id":self.sid,
            "pid":os.getpid(),"pid_start":fixture.peer_steward._proc_start_ticks(os.getpid()),
            "pid_namespace":os.readlink("/proc/self/ns/pid")}

    parent_row=fixture.RetainedRunNoticeCarrierTest.parent_row
    _notice=fixture.RetainedRunNoticeCarrierTest._notice

    def _run_notice_duty(self,harness,duty,*,target_state='idle',queue_accept_only=False,
                         duties=None,input_observer=None,native_queue_impl=None):
        """One runner pass with a shell-attached recipient; the host's input is the receipt.

        Claude/OpenCode take the real ``session_shell.deliver_pending`` (status -> inject);
        Codex takes its exact native queue. ``sends`` counts what the host or queue got."""
        import session_shell
        steward=fixture.peer_steward
        store=steward.peer_obligations.ObligationStore(self.tmp_root)
        binding={'harness':harness,'sid':self.sid,'native_pid':os.getpid(),
                 'native_start':self.ident['pid_start'],'host_pid':os.getpid()}
        recipient={'harness':harness,'session_id':self.sid}
        sends=[]
        def exact_binding(h,sid):
            return (binding,'') if (h,sid)==(harness,self.sid) else (None,'attachment-missing')
        def receive(text):
            if input_observer:
                input_observer(harness,self.sid,text)
            sends.append(text)
            self.assertEqual(steward.peer_message.receive_peer_message(text,recipient),0)
        def control(_binding,operation,**fields):
            if operation=='status':
                return {'ok':True,'state':target_state,'empty':True,'focused':False,'epoch':1}
            if operation=='inject':
                receive(steward.peer_message._read_pending(fields['ref'])['text'])
                return {'ok':True}
            return {'ok':False,'reason':'fixture-unsupported'}
        def native_queue(ref,*,timeout=1.0,**_kw):
            row=steward.peer_message._read_pending(ref)
            self.assertIsNotNone(row)
            if queue_accept_only:
                sends.append(row['text'])
                with steward.peer_message.pending_lock(ref):
                    current=steward.peer_message._read_pending(ref)
                    steward.peer_message._save_pending(dict(
                        current,state='queued',receipt='native-queue-accepted'))
                return {'status':'queued','reason':'fixture-native-queue-accepted'}
            receive(row['text'])
            return {'status':'received','reason':'fixture-exact-native-history'}
        with mock.patch.object(session_shell,'exact_binding',side_effect=exact_binding), \
             mock.patch.object(session_shell,'control',side_effect=control), \
             mock.patch.object(steward.subprocess,'run',side_effect=OSError('no terminal multiplexer')), \
             mock.patch.object(steward,'_project_of',return_value=str(self.tmp_root)), \
             mock.patch.object(steward.peer_message,'deliver_pending_codex',
                               side_effect=native_queue_impl or native_queue):
            steward._reconcile_obligations(
                store,duties=steward._runner_duties(store,duties or [duty]))
        return store,sends

    def notice(self,harness,*,sequence=1):
        key=session_resume.PREFIX+fixture.json.dumps([harness,self.sid,sequence])
        record=self._notice(harness,key=key)
        store=fixture.peer_steward.peer_obligations.ObligationStore(self.tmp_root)
        duty=next(row for row in store.list(kinds={'run-notice'})
                  if row['intent']['delivery_id']==record['delivery_id'])
        return record,duty

    def test_resume_idle_native_receipt_settles_the_same_notice_for_all_three(self):
        for harness in ('claude','codex','opencode'):
            with self.subTest(harness=harness),mock.patch.object(session_resume,'current',return_value=True):
                record,duty=self.notice(harness)
                store,sends=self._run_notice_duty(harness,duty)
                self.assertEqual(len(sends),1)
                import dispatch_pending_delivery as pending
                self.assertEqual(pending.read(self.tmp_root,self.sid,record['delivery_id'])['state'],'acked')

    def test_accepted_codex_queue_is_not_submitted_again_without_exact_receipt(self):
        with mock.patch.object(session_resume,'current',return_value=True):
            record,duty=self.notice('codex')
            store,sends=self._run_notice_duty('codex',duty,queue_accept_only=True)
            self.assertEqual(len(sends),1)
            _store,again=self._run_notice_duty('codex',store.get(duty['id']),queue_accept_only=True)
            self.assertEqual(again,[])



    def test_ongoing_background_does_not_take_a_send_lease_and_updates_next_pass(self):
        record,duty=self.notice('claude')
        with mock.patch.object(session_resume,'current',side_effect=ValueError('resume-observation-waiting-background')):
            store,sends=self._run_notice_duty('claude',duty)
        self.assertEqual(sends,[])
        import dispatch_pending_delivery as pending
        self.assertEqual(pending.read(self.tmp_root,self.sid,record['delivery_id'])['state'],'pending')
        with mock.patch.object(session_resume,'current',return_value=True):
            _store,sends=self._run_notice_duty('claude',store.get(duty['id']))
        self.assertEqual(len(sends),1)



    def test_fresh_prompt_frontier_cancels_a_notice_even_if_ui_state_has_not_updated(self):
        import session_tidy as tidy
        import time
        for harness in ('claude','codex','opencode'):
            with self.subTest(harness=harness):
                seat=session_resume._seat(harness,self.sid)
                source={'session_seq':1,'prompt_seq':tidy.read_prompt_seq(seat),'ts':time.time()}
                with mock.patch.object(session_resume,'_resume',return_value=source), \
                     mock.patch.object(session_resume,'native_state',return_value='interrupted-turn'), \
                     mock.patch.object(session_resume,'_human_wait',return_value=False):
                    record,duty=self.notice(harness)
                    # A new user prompt lands after the notice; the host still reads idle/empty.
                    with tidy.session_state_lock(seat):
                        tidy.bump_prompt_seq(seat,harness,self.sid,time.time())
                    _store,sends=self._run_notice_duty(harness,duty)
                self.assertEqual(sends,[])
                import dispatch_pending_delivery as pending
                self.assertNotEqual(pending.read(self.tmp_root,self.sid,record['delivery_id'])['state'],'acked')

    def test_own_native_prompt_frontier_still_allows_exact_receipt_settlement(self):
        import session_tidy as tidy
        import time
        import dispatch_pending_delivery as pending
        for harness in ('claude','codex','opencode'):
            with self.subTest(harness=harness):
                seat=session_resume._seat(harness,self.sid)
                source={'session_seq':1,'prompt_seq':tidy.read_prompt_seq(seat),'ts':time.time()}
                def observed_input(_harness,_sid,_body):
                    with tidy.session_state_lock(seat):
                        tidy.bump_prompt_seq(seat,harness,self.sid,time.time())
                with mock.patch.object(session_resume,'_resume',return_value=source), \
                     mock.patch.object(session_resume,'native_state',return_value='interrupted-turn'), \
                     mock.patch.object(session_resume,'_human_wait',return_value=False):
                    record,duty=self.notice(harness)
                    _store,sends=self._run_notice_duty(harness,duty,input_observer=observed_input)
                self.assertEqual(len(sends),1)
                self.assertEqual(pending.read(self.tmp_root,self.sid,record['delivery_id'])['state'],'acked')

    def test_new_activity_at_the_real_codex_before_add_boundary_prevents_input(self):
        import codex_queue_delivery
        import codex_queue_dispatch
        import session_tidy as tidy
        import time
        actual_queue=fixture.peer_steward.peer_message.deliver_pending_codex
        for sequence,changed in enumerate((False,True),1):
            with self.subTest(changed=changed):
                seat=session_resume._seat('codex',self.sid)
                source={'session_seq':sequence,'prompt_seq':tidy.read_prompt_seq(seat),'ts':time.time()}
                added=[]; before_add=[]
                def send(*args,**kwargs):
                    if kwargs['allow_add']:
                        before_add.append(kwargs['thread_id'])
                        if changed:
                            with tidy.session_state_lock(seat):
                                tidy.bump_prompt_seq(seat,'codex',self.sid,time.time())
                        kwargs['before_add']()
                        added.append(kwargs['thread_id'])
                    return {'status':'queued'}
                with mock.patch.object(session_resume,'_resume',return_value=source), \
                     mock.patch.object(session_resume,'native_state',return_value='interrupted-turn'), \
                     mock.patch.object(session_resume,'_human_wait',return_value=False), \
                     mock.patch.object(codex_queue_dispatch,'resolve_queue_socket',return_value=self.tmp_root/'unused.sock'), \
                     mock.patch.object(codex_queue_delivery,'send_at_least_once',side_effect=send):
                    _record,duty=self.notice('codex',sequence=sequence)
                    store,_sends=self._run_notice_duty('codex',duty,native_queue_impl=actual_queue)
                    self.assertEqual(before_add,[self.sid])
                    if changed:
                        import dispatch_pending_delivery as pending
                        record=pending.read(self.tmp_root,self.sid,_record['delivery_id'])
                        self.assertEqual(record['state'],'sent-ambiguous')
                        self.assertTrue(session_resume.current(record))
                        self.assertFalse(session_resume.current(record,for_submission=True))
                        ref=store.get(duty['id'])['observation']['transfer_ref']
                        actual_queue(ref)
                self.assertEqual(added,[] if changed else [self.sid])

    def test_input_during_a_slow_source_observation_is_rechecked_before_submission(self):
        import session_tidy as tidy
        import time
        for harness in ('claude','codex','opencode'):
            with self.subTest(harness=harness):
                with mock.patch.object(session_resume,'current',return_value=True):
                    record,_duty=self.notice(harness)
                seat=session_resume._seat(harness,self.sid)
                source={'session_seq':1,'prompt_seq':tidy.read_prompt_seq(seat),'ts':time.time()}
                def native(*args,**kwargs):
                    with tidy.session_state_lock(seat):
                        tidy.bump_prompt_seq(seat,harness,self.sid,time.time())
                    return 'interrupted-turn'
                with mock.patch.object(session_resume,'_resume',return_value=source), \
                     mock.patch.object(session_resume,'native_state',side_effect=native), \
                     mock.patch.object(session_resume,'_human_wait',return_value=False):
                    self.assertFalse(session_resume.current(record,for_submission=True))



    def test_resume_does_not_block_an_ordinary_notice_steering_an_active_codex(self):
        import dispatch_pending_delivery as pending
        with mock.patch.object(session_resume,'current',return_value=True):
            resume_record,resume_duty=self.notice('codex')
            ordinary=self._notice('codex',key='ordinary-existing-policy')
            store=fixture.peer_steward.peer_obligations.ObligationStore(self.tmp_root)
            ordinary_duty=next(row for row in store.list(kinds={'run-notice'})
                if row['intent']['delivery_id']==ordinary['delivery_id'])
            _store,sends=self._run_notice_duty('codex',resume_duty,target_state='working',
                duties=[resume_duty,ordinary_duty])
        self.assertEqual(len(sends),1)
        self.assertIn(ordinary['delivery_id'],sends[0])
        self.assertNotIn(resume_record['delivery_id'],sends[0])
        self.assertEqual(pending.read(self.tmp_root,self.sid,ordinary['delivery_id'])['state'],'acked')
        self.assertEqual(pending.read(self.tmp_root,self.sid,resume_record['delivery_id'])['state'],'pending')


if __name__=='__main__':unittest.main()
