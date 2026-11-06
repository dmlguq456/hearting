"""Synthetic native resume records only; no operating session input or signals."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'utilities'),str(ROOT/'tools/memory/tests')]
from tidy_isolation import isolated_env
import session_tidy as st
import session_notice
import session_tidy_clear


class _ResumeFixture(unittest.TestCase):
    def setUp(self):
        self.iso=isolated_env();self.addCleanup(self.iso.cleanup)
        self.cwd=self.iso.root/'project';self.cwd.mkdir()
        self.env=self.iso.patched_environ();self.env.__enter__();self.addCleanup(self.env.__exit__,None,None,None)
        self.notify=mock.patch.object(session_notice,'notify',wraps=session_notice.notify)
        self.notices=self.notify.start();self.addCleanup(self.notify.stop)
        # Keep ordinary memory/context/provider discovery out of a source test.
        self.routes=mock.patch('session_resume.open_work',return_value=False)
        self.routes.start();self.addCleanup(self.routes.stop)
        self.recover=mock.patch('peer_obligations.request_obligation_recovery',return_value=False)
        self.recover.start();self.addCleanup(self.recover.stop)
        self.runner=mock.patch('peer_obligations.ensure_normal_runner',return_value=False)
        self.runner.start();self.addCleanup(self.runner.stop)

    def transcript(self,sid,rows):
        p=self.iso.root/(sid+'.jsonl')
        p.write_text(''.join(json.dumps({'sessionId':sid,'timestamp':'2020-01-01T00:00:00Z',**r})+'\n' for r in rows))
        return str(p)

    def hook(self,sid,rows):
        return st.run_hook('claude','start',sid,source='resume',transcript=self.transcript(sid,rows),
                           cwd=str(self.cwd),render_context=False,now=1793936400)


class ResumeHookTest(_ResumeFixture):
    def test_interrupted_tool_turn_retains_one_existing_notice(self):
        self.hook('resume-tool',[{'type':'assistant','message':{'role':'assistant','stop_reason':'tool_use',
            'content':[{'type':'tool_use','id':'call1','name':'Bash','input':{'command':'synthetic work'}}]}}])
        self.assertEqual(self.notices.call_count,1)
        self.assertEqual(self.notices.call_args.args[:2],('claude','resume-tool'))

    def test_queued_stopped_native_task_retains_notice(self):
        self.hook('resume-background',[{'type':'assistant','message':{'role':'assistant','stop_reason':'end_turn','content':[]}},
            {'type':'user','origin':{'kind':'task-notification'},'promptSource':'system','queueTranscriptOnly':True,
             'message':{'role':'user','content':'<task-notification><task-id>own-task</task-id><status>stopped</status></task-notification>'}}])
        self.assertEqual(self.notices.call_count,1)

    def test_completed_session_does_not_retain_wake(self):
        self.hook('resume-complete',[{'type':'assistant','message':{'role':'assistant','stop_reason':'end_turn',
            'content':[{'type':'text','text':'Synthetic work completed.'}]}}])
        self.notices.assert_not_called()

    def test_native_question_wait_does_not_retain_wake(self):
        self.hook('resume-human',[{'type':'assistant','message':{'role':'assistant','stop_reason':'tool_use',
            'content':[{'type':'tool_use','id':'question1','name':'AskUserQuestion','input':{}}]}}])
        self.notices.assert_not_called()

    def test_fourteen_resumes_keep_their_exact_separate_recipients(self):
        def resume(n):
            self.hook(f'resume-batch-{n}',[{'type':'assistant','message':{'role':'assistant','stop_reason':'tool_use',
                'content':[{'type':'tool_use','id':f'call{n}','name':'Bash','input':{}}]}}])
        with ThreadPoolExecutor(max_workers=14) as pool:
            list(pool.map(resume, range(14)))
        self.assertEqual(self.notices.call_count,14)
        self.assertEqual({call.args[1] for call in self.notices.call_args_list},
                         {f'resume-batch-{n}' for n in range(14)})




class ResumeFactsTest(_ResumeFixture):
    def state(self, sid, rows):
        import session_resume as resume
        return resume.native_state('claude',sid,self.transcript(sid,rows),resumed_at=1793936400)

    def test_user_pasted_task_envelope_is_not_native_resume_evidence(self):
        state=self.state('pasted',[{'type':'user','message':{'content':'<task-notification><task-id>x</task-id><status>stopped</status></task-notification>'}},
            {'type':'assistant','message':{'stop_reason':'end_turn'}}])
        self.assertEqual(state,'completed')

    def test_answered_old_question_does_not_hide_later_interrupted_work(self):
        state=self.state('answered',[{'type':'assistant','message':{'content':[{'type':'tool_use','name':'AskUserQuestion','id':'q1'}]}},
            {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'q1','content':'synthetic answer'}]}},
            {'type':'assistant','message':{'stop_reason':'tool_use','content':[{'type':'tool_use','name':'Bash','id':'work','input':{}}]}}])
        self.assertEqual(state,'interrupted-turn')

    def test_pending_background_wait_is_retained_without_premature_wake(self):
        state=self.state('background-live',[{'type':'assistant','message':{'stop_reason':'tool_use',
            'content':[{'type':'tool_use','name':'Bash','id':'bg','input':{'run_in_background':True}}]}},
            {'type':'user','toolUseResult':{'backgroundTaskId':'task1'},'message':{'content':[{'type':'tool_result','tool_use_id':'bg'}]}},
            {'type':'assistant','message':{'stop_reason':'end_turn','content':[]}}])
        self.assertEqual(state,'waiting-background')

    def test_finished_old_background_does_not_keep_completed_session_pending(self):
        state=self.state('background-done',[{'type':'assistant','message':{'stop_reason':'tool_use',
            'content':[{'type':'tool_use','name':'Bash','id':'bg','input':{'run_in_background':True}}]}},
            {'type':'user','toolUseResult':{'backgroundTaskId':'task1'},'message':{'content':[{'type':'tool_result','tool_use_id':'bg'}]}},
            {'type':'user','origin':{'kind':'task-notification'},'message':{'content':'<task-notification><task-id>task1</task-id><status>completed</status></task-notification>'}},
            {'type':'assistant','message':{'stop_reason':'end_turn','content':[]}}])
        self.assertEqual(state,'completed')

    def test_codex_native_turn_boundaries_and_user_wait(self):
        import session_resume as resume
        sid='codex-native'
        for kind,expected in [('task_started','interrupted-turn'),('task_complete','completed')]:
            path=self.transcript(sid,[{'type':'event_msg','payload':{'type':kind}}])
            self.assertEqual(resume.native_state('codex',sid,path,resumed_at=1793936400),expected)
        path=self.transcript(sid,[{'type':'response_item','payload':{'type':'function_call','name':'request_user_input'}}])
        self.assertEqual(resume.native_state('codex',sid,path,resumed_at=1793936400),'human-wait')

    def test_opencode_uses_existing_exact_native_observation(self):
        import session_resume as resume
        for message,expected in [({'type':'assistant','completed':123},'completed'),
                                 ({'type':'assistant','completed':None},'interrupted-turn'),
                                 ({'type':'assistant','tools':[{'name':'question'}]},'human-wait')]:
            with mock.patch('opencode_runtime.read_session_observation',return_value={'state':'observed','messages':[message]}) as read:
                self.assertEqual(resume.native_state('opencode','ses_native','',resumed_at=1793936400),expected)
                read.assert_called_once_with('ses_native',shell_after_seq=0)

    def test_repeated_resume_starts_coalesce_until_actual_work(self):
        import session_resume as resume
        rows=[{'type':'assistant','message':{'stop_reason':'tool_use','content':[]}}]
        self.hook('duplicate',rows)
        first=self.notices.call_args.kwargs['key']
        self.hook('duplicate',rows)
        self.assertEqual(self.notices.call_args.kwargs['key'],first)
        seat=st.resolve_seat('claude',env={},sid='duplicate')
        events=[r for r in st._read_ledger_lines(seat) if r.get('event')=='resume-notice']
        self.assertEqual(len(events),1)

    def notice_record(self,sid):
        import dispatch_pending_delivery as pending
        seat=st.resolve_seat('claude',env={},sid=sid)
        note=next(r for r in st._read_ledger_lines(seat) if r.get('event')=='resume-notice')
        return pending.read(Path(note['jobs']).parent,sid,note['delivery_id'])

    def test_new_human_input_cancels_unsubmitted_resume_notice(self):
        import session_resume as resume
        self.hook('human-moved',[{'type':'assistant','message':{'stop_reason':'tool_use','content':[]}}])
        record=self.notice_record('human-moved')
        self.assertTrue(resume.current(record))
        seat=st.resolve_seat('claude',env={},sid='human-moved')
        with st.seat_lock(seat.key):
            st.record_real_prompt(seat,'claude','human-moved',1,1793936401)
        self.assertFalse(resume.current(record))

    def test_resume_key_cannot_authorize_another_recipient(self):
        import session_resume as resume
        import dispatch_pending_delivery as pending
        self.hook('bound-recipient',[{'type':'assistant','message':{'stop_reason':'tool_use','content':[]}}])
        record=self.notice_record('bound-recipient')
        self.assertTrue(resume.current(record))
        self.assertFalse(resume.current({**record,'recipient_digest':pending.recipient_digest('other-session')}))


    def test_new_unprocessed_user_turn_after_an_older_final_reply_is_interrupted(self):
        state=self.state('unprocessed-user',[{'type':'assistant','message':{'stop_reason':'end_turn'}},
            {'type':'user','message':{'role':'user','content':'synthetic new work'}}])
        self.assertEqual(state,'interrupted-turn')

    def test_native_enum_reuses_same_generation_and_invalidates_after_change(self):
        import session_resume as resume
        path=self.transcript('generation',[{'type':'assistant','message':{'stop_reason':'tool_use'}}])
        with mock.patch.object(resume,'_tail',wraps=resume._tail) as read:
            self.assertEqual(resume.native_state('claude','generation',path,resumed_at=1793936400),'interrupted-turn')
            self.assertEqual(resume.native_state('claude','generation',path,resumed_at=1793936400),'interrupted-turn')
            self.assertEqual(read.call_count,1)
            self.transcript('generation',[{'type':'assistant','message':{'stop_reason':'end_turn'}}])
            self.assertEqual(resume.native_state('claude','generation',path,resumed_at=1793936400),'completed')
            self.assertEqual(read.call_count,2)

    def test_opencode_stopped_background_after_last_reply_requires_followthrough(self):
        import session_resume as resume
        message={'type':'assistant','completed':1000,'created':900}
        stopped={'native_record_kind':'session-message-shell','native_status':'stopped','completed_at_ms':1100}
        with mock.patch('opencode_runtime.read_session_observation',return_value={
                'state':'observed','messages':[message],'shell_tasks':[stopped]}):
            self.assertEqual(resume.native_state('opencode','ses_native','',resumed_at=1793936400),'queued-task-result')
        message['completed']=1200
        with mock.patch('opencode_runtime.read_session_observation',return_value={
                'state':'observed','messages':[message],'shell_tasks':[stopped]}):
            self.assertEqual(resume.native_state('opencode','ses_native','',resumed_at=1793936400),'completed')

    def test_codex_background_finishes_after_the_waiting_turn(self):
        import session_resume as resume
        sid='codex-background'
        start={'type':'item.started','item':{'type':'command_execution','id':'cmd1'}}
        done={'type':'item.completed','item':{'type':'command_execution','id':'cmd1'}}
        turn={'type':'event_msg','payload':{'type':'task_complete'}}
        path=self.transcript(sid,[start,turn])
        self.assertEqual(resume.native_state('codex',sid,path,resumed_at=1793936400),'waiting-background')
        path=self.transcript(sid,[start,turn,done])
        self.assertEqual(resume.native_state('codex',sid,path,resumed_at=1793936400),'queued-task-result')
        path=self.transcript(sid,[start,done,turn])
        self.assertEqual(resume.native_state('codex',sid,path,resumed_at=1793936400),'completed')

    def test_malformed_native_fields_stay_unknown_instead_of_crashing_collection(self):
        state=self.state('malformed',[{'type':'assistant','message':{'stop_reason':'end_turn'}},
            {'type':'user','origin':['invalid'],'promptSource':'system','queueTranscriptOnly':True,
             'message':{'content':'<task-notification><task-id>x</task-id><status>stopped</status></task-notification>'}}])
        self.assertEqual(state,'unknown')


class OpenCodeNativeResumeTest(_ResumeFixture):
    def report(self, *, bad_birth=False, wrong_sid=False, requested=True, repeats=1, forked=False):
        import subprocess
        code = r"""
import json,os,subprocess,sys
from pathlib import Path
sid='ses_resumefixture'
start=Path('/proc/self/stat').read_text().rsplit(') ',1)[1].split()[19]
p=Path(os.environ['XDG_STATE_HOME'])/'hearting/tui-identity'/f'{os.getpid()}-{start}.json'
p.parent.mkdir(parents=True,exist_ok=True)
p.write_text(json.dumps({'schema':'hearting-tui-selection-v1','pid':os.getpid(),'start':start,'sessionID':sid}))
p.chmod(0o600)
req={'harness':'opencode','pid':os.getpid(),'start':start+'0' if sys.argv[2]=='bad' else start,'sid':'ses_other' if sys.argv[3]=='wrong' else sid}
for _ in range(int(sys.argv[5])):
 child=subprocess.run([sys.argv[4],sys.argv[1]],input=json.dumps(req),text=True,capture_output=True,timeout=10)
 print(child.returncode)
"""
        script=self.iso.root/'synthetic-native-tui.py';script.write_text(code)
        args=['opencode',str(script),str(ROOT/'utilities/session_resume.py'),
              'bad' if bad_birth else 'good','wrong' if wrong_sid else 'same',sys.executable,str(repeats)]
        if requested:args+=['--session','ses_resumefixture']
        if forked:args+=['--fork']
        child=subprocess.run(args,executable=sys.executable,text=True,capture_output=True,
            env=self.iso.env(),cwd=self.cwd,timeout=15)
        self.assertEqual(child.returncode,0,child.stderr)
        seat=st.resolve_seat('opencode',env={},sid='ses_resumefixture')
        return [row for row in st._read_ledger_lines(seat) if row.get('event')=='start']

    def test_owned_explicit_native_resume_records_start_without_consuming_context(self):
        rows=self.report()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['source'],'resume')

    def test_same_native_process_reporting_again_does_not_create_another_start(self):
        self.assertEqual(len(self.report(repeats=2)),1)

    def test_fork_is_not_promoted_to_resuming_old_work(self):
        self.assertEqual(self.report(forked=True),[])

    def test_changed_birth_is_not_a_native_resume(self):
        self.assertEqual(self.report(bad_birth=True),[])

    def test_different_selected_session_is_not_a_native_resume(self):
        self.assertEqual(self.report(wrong_sid=True),[])

    def test_normal_native_start_is_not_promoted_to_resume(self):
        self.assertEqual(self.report(requested=False),[])




class ResumeStatusTest(_ResumeFixture):
    def test_retained_resume_is_visible_then_clears_when_native_work_continues(self):
        import session_resume as resume
        self.notify.stop()
        with mock.patch('peer_obligations.ensure_normal_runner',return_value=False), \
             mock.patch('peer_obligations.request_dispatch_reconciliation',return_value=False):
            self.hook('visible-resume',[{'type':'assistant','message':{'stop_reason':'tool_use','content':[]}}])
        self.assertEqual(resume.status_for_session('claude','visible-resume'),'stopped')
        path=self.iso.root/'visible-resume.jsonl'
        from datetime import datetime,timezone
        with path.open('a') as handle:
            handle.write(json.dumps({'type':'assistant','sessionId':'visible-resume',
                'timestamp':datetime.fromtimestamp(1793936401,timezone.utc).isoformat(),
                'message':{'stop_reason':'end_turn','content':[]}})+'\n')
        self.assertIsNone(resume.status_for_session('claude','visible-resume'))

    def test_positive_user_wait_suppresses_resume_and_display(self):
        import session_resume as resume
        with mock.patch.object(resume,'_human_wait',return_value=True):
            self.hook('waiting-human',[{'type':'assistant','message':{'stop_reason':'tool_use','content':[]}}])
            self.notices.assert_not_called()
            self.assertIsNone(resume.status_for_session('claude','waiting-human'))

    def test_open_registered_work_retains_a_notice_after_a_terminal_reply(self):
        with mock.patch('session_resume.open_work',return_value=True):
            self.hook('open-work',[{'type':'assistant','message':{'stop_reason':'end_turn','content':[]}}])
            self.assertEqual(self.notices.call_count,1)






class ResumeRecoveryTest(_ResumeFixture):
    def test_existing_runner_discovers_an_earlier_recorded_resume_without_a_new_hook(self):
        import time
        from types import SimpleNamespace
        import peer_steward_duties as duties
        sid='earlier-resume'
        path=self.transcript(sid,[{'type':'assistant','message':{'stop_reason':'tool_use','content':[]}}])
        seat=st.resolve_seat('claude',env={},sid=sid)
        with st.session_state_lock(seat):
            st.record_event(seat,'claude',sid,'start',source='resume',transcript=path,now=1793936400)
        self.notices.assert_not_called()
        root=session_notice._registry().parent
        with mock.patch.object(duties,'_live_recipients',return_value={('claude',sid)}), \
             mock.patch('dispatch_seat_handover.lineage_graph',return_value=SimpleNamespace(edges=[])), \
             mock.patch('dispatch_recipient_inbox.read_inbox',return_value=None), \
             mock.patch.object(duties,'_prefill_recipient_inbox',return_value=True):
            for _ in range(2):
                duties._prefill_live_inboxes(root,deadline_ns=time.monotonic_ns()+10_000_000_000)
        self.assertEqual(self.notices.call_count,1)
        self.assertEqual(self.notices.call_args.args[:2],('claude',sid))

    def test_another_registry_reuses_the_original_retained_notice(self):
        import session_resume as resume
        import dispatch_pending_delivery as pending
        sid='same-native-session'
        self.hook(sid,[{'type':'assistant','message':{'stop_reason':'tool_use','content':[]}}])
        seat=st.resolve_seat('claude',env={},sid=sid)
        note=next(r for r in st._read_ledger_lines(seat) if r.get('event')=='resume-notice')
        alternate=self.iso.root/'another-registry'/'jobs.log'
        alternate.parent.mkdir();alternate.touch()
        record=resume.register('claude',sid,source='resume',jobs=alternate)
        self.assertEqual(record['delivery_id'],note['delivery_id'])
        self.assertEqual(self.notices.call_count,1)
        self.assertFalse(pending.record_path(alternate.parent,sid,record['delivery_id']).exists())




class ResumeRetentionTest(_ResumeFixture):
    def test_fold_keeps_original_notice_and_cross_registry_deduplication(self):
        import session_resume as resume
        sid='folded-resume'
        self.hook(sid,[{'type':'assistant','message':{'stop_reason':'tool_use','content':[]}}])
        seat=st.resolve_seat('claude',env={},sid=sid)
        with st.session_state_lock(seat):
            for n in range(6):
                st._append_ledger_entry(seat,{'event':'runtime-status','harness':'claude','sid':sid,
                    'epoch':0,'ts':1793936398+n*.01})
            with mock.patch.object(st,'LEDGER_FOLD_LINES',3),mock.patch.object(st,'LEDGER_KEEP_RAW',1):
                st._fold_ledger(seat)
        alternate=self.iso.root/'folded-other'/'jobs.log';alternate.parent.mkdir();alternate.touch()
        record=resume.register('claude',sid,source='resume',jobs=alternate)
        self.assertIsNotNone(record)
        self.assertEqual(self.notices.call_count,1)
        self.assertEqual(resume.status_for_session('claude',sid),'stopped')

    def test_effect_reads_new_start_even_inside_a_pinned_ledger_view(self):
        import session_resume as resume
        import dispatch_pending_delivery as pending
        sid='pinned-resume'
        self.hook(sid,[{'type':'assistant','message':{'stop_reason':'tool_use','content':[]}}])
        seat=st.resolve_seat('claude',env={},sid=sid)
        note=next(r for r in st._read_ledger_lines(seat) if r.get('event')=='resume-notice')
        record=pending.read(Path(note['jobs']).parent,sid,note['delivery_id'])
        self.assertTrue(resume.current(record))
        with st.seat_ledger_read_scope():
            st._read_ledger_lines(seat)
            # Another hook process would not invalidate this loop's memory view.
            with st._native_ledger_path(seat).open('a') as out:
                out.write(json.dumps({'event':'start','source':'clear','harness':'claude','sid':sid,
                    'ts':1793936401,'epoch':1,'session_seq':1793936401000000})+'\n')
            self.assertFalse(resume.current(record))


if __name__=='__main__':unittest.main()
