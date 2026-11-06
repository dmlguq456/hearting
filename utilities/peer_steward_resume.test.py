"""Resume notices use the real courier flow with synthetic native I/O only."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

path=Path(__file__).with_name('peer_steward.test.py')
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
    _run_notice_duty=fixture.RetainedRunNoticeCarrierTest._run_notice_duty

    def notice(self,harness):
        key=session_resume.PREFIX+fixture.json.dumps([harness,self.sid,1])
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

    def test_existing_composer_draft_blocks_resume_before_any_native_submission(self):
        for harness in ('claude','codex','opencode'):
            with self.subTest(harness=harness),mock.patch.object(session_resume,'current',return_value=True):
                record,duty=self.notice(harness)
                store,sends=self._run_notice_duty(harness,duty,idle_guard=lambda *a,**k:('draft',None))
                self.assertEqual(sends,[])
                self.assertEqual(store.get(duty['id'])['observation']['reason'],'resume-draft')

    def test_draft_appearing_during_claim_work_blocks_the_final_input_boundary(self):
        for harness in ('claude','codex','opencode'):
            calls=[]
            def guard(*args,**kwargs):
                calls.append(1)
                return (None,'fixture-proof') if len(calls)==1 else ('draft',None)
            with self.subTest(harness=harness),mock.patch.object(session_resume,'current',return_value=True):
                record,duty=self.notice(harness)
                _store,sends=self._run_notice_duty(harness,duty,idle_guard=guard)
                self.assertEqual(sends,[])
                self.assertEqual(len(calls),2)

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
                def guard(*args,**kwargs):
                    with tidy.session_state_lock(seat):
                        tidy.bump_prompt_seq(seat,harness,self.sid,time.time())
                    return None,'fixture-ready-but-old-ui'
                with mock.patch.object(session_resume,'_resume',return_value=source), \
                     mock.patch.object(session_resume,'native_state',return_value='interrupted-turn'), \
                     mock.patch.object(session_resume,'_human_wait',return_value=False):
                    record,duty=self.notice(harness)
                    _store,sends=self._run_notice_duty(harness,duty,idle_guard=guard)
                self.assertEqual(sends,[])

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



    def test_resume_does_not_block_an_ordinary_notice_steering_an_active_codex(self):
        import dispatch_pending_delivery as pending
        with mock.patch.object(session_resume,'current',return_value=True):
            resume_record,resume_duty=self.notice('codex')
            ordinary=self._notice('codex',key='ordinary-existing-policy')
            store=fixture.peer_steward.peer_obligations.ObligationStore(self.tmp_root)
            ordinary_duty=next(row for row in store.list(kinds={'run-notice'})
                if row['intent']['delivery_id']==ordinary['delivery_id'])
            _store,sends=self._run_notice_duty('codex',resume_duty,target_state='working',
                duties=[resume_duty,ordinary_duty],idle_guard=lambda *a,**k:('native-state-working',None))
        self.assertEqual(len(sends),1)
        self.assertIn(ordinary['delivery_id'],sends[0])
        self.assertNotIn(resume_record['delivery_id'],sends[0])
        self.assertEqual(pending.read(self.tmp_root,self.sid,ordinary['delivery_id'])['state'],'acked')
        self.assertEqual(pending.read(self.tmp_root,self.sid,resume_record['delivery_id'])['state'],'pending')


if __name__=='__main__':unittest.main()
