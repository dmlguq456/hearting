"""The existing prompt classifier also supplies a bounded, read-only Fleet view."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools/memory/tests"), str(ROOT)]
from tidy_isolation import isolated_env
import session_tidy as st
import session_tidy_clear as clear
from tools.fleet.collectors import prompt_inputs


RUNTIME_COST_PROBE = '''
import importlib.util,json,sys,time
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root/'utilities'),str(root/'tools/memory/tests'),str(root)]
spec=importlib.util.spec_from_file_location('runtime_cost_fixture',root/'utilities/session_tidy_prompt_display.test.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
st=fixture.st
rows=[]
f=fixture.PromptDisplayTests();f.setUp()
try:
 for n in (15000,60000):
  for kind in ('truncated','foreign-close','complete'):
   prefix='<shell id="s" state="completed" command="'
   text=(prefix+'x">\\n'*n if kind!='complete' else
         prefix+'printf done">\\nACTUAL OUTPUT\\n'+'x'*(4*n)+'\\n</shell>')
   if kind=='foreign-close':text+='</unrelated>'
   sid=f'cost-{n}-{kind}'
   if sys.argv[2]=='hook':
    human=f.prompt('Original person input','opencode',sid)['human']
   started=time.perf_counter()
   if sys.argv[2]=='hook':
    display=f.prompt(text,'opencode',sid,now=1010)
    if kind=='complete':
     assert display['human']==human
     assert display['received'][-1]['text']=='ACTUAL OUTPUT'
    else:
     assert display['human_state']=='known' and display['human']['source']=='real'
   else:
    source=st.runtime_turn_source(text)
    assert source==('notification' if kind=='complete' else '')
    line=st.prompt_first_line(text)
    assert line==('ACTUAL OUTPUT' if kind=='complete' else '<shell id="s" state="completed" command="x">')
   rows.append({'kind':kind,'bytes':len(text.encode()),'seconds':time.perf_counter()-started})
finally:f.doCleanups()
print(json.dumps(rows))
'''


class PromptDisplayTests(unittest.TestCase):
    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.cwd = self.iso.root / "project"
        self.cwd.mkdir()
        self.env = self.iso.patched_environ({"HEARTING_TIDY_TEST_ROOT": str(self.iso.root)})
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.helper = mock.patch.object(clear, "_start_helper")
        self.helper.start()
        self.addCleanup(self.helper.stop)
        self.wait_projection = mock.patch("human_wait_state._request_producer")
        self.wait_projection.start()
        self.addCleanup(self.wait_projection.stop)
        prompt_inputs._CACHE.clear()

    def seat(self, harness="claude", sid="prompt-session"):
        return st.session_seat(st.Seat("native", "", harness=harness, sid=sid), harness, sid)

    def prompt(self, text, harness="claude", sid="prompt-session", now=1000, origin=None, seat=None):
        seat = seat or self.seat(harness, sid)
        with mock.patch.object(st, "resolve_seat", return_value=seat), \
                mock.patch.object(st, "_current_request_origin", return_value=origin):
            st.run_hook(harness, "prompt", sid, cwd=str(self.cwd), prompt_text=text, now=now)
        return st.prompt_display(seat, harness, sid)

    def test_all_harnesses_keep_human_input_across_runtime_and_pasted_peer_prompts(self):
        for harness in st.HARNESSES:
            with self.subTest(harness=harness):
                original = self.prompt("내가 쓴 첫 줄\n둘째 줄", harness, now=1000)
                self.assertEqual(original["human"]["text"], "내가 쓴 첫 줄")
                seat = self.seat(harness)
                frontier = st.real_prompt_frontier(seat, harness, "prompt-session")
                for index, text in enumerate((
                    "<task-notification>finished</task-notification>",
                    '<pasted_content id="peer">\n<agent-message>peer request</agent-message>\n</pasted_content id="peer">',
                    "<system-reminder>runtime request</system-reminder>",
                )):
                    result = self.prompt(text, harness, now=1010 + index)
                    self.assertEqual(result["human"], original["human"])
                    self.assertEqual(st.real_prompt_frontier(seat, harness, "prompt-session"), frontier)
                self.assertEqual([row["source"] for row in result["received"]],
                                 ["notification", "peer", "runtime"])

    def _queued_notice(self, root, sid, key):
        """A notice stored for ``sid`` and the exact text its native queue item carries."""
        import dispatch_notice_receipt as notice_receipt
        import dispatch_pending_delivery as pending
        import dispatch_recipient_receipt as receipt
        body = {"schema_version": 1, "kind": "notice", "subject": "Remote run",
                "text": "run 7 ended exit=0", "required_action": "report-to-user", "key": key}
        record = pending.create(
            root, recipient_kind="codex-native-queue", recipient_key=sid,
            delivery_id="delivery-" + key * 32, session_generation="",
            session_generation_supported="0", attempt_ids=["att-" + "c" * 28],
            parent_attempt_id="att-" + "c" * 28, route_id="route-test",
            route_node="one-shot", receipt=body,
            receipt_digest=pending._canonical_receipt_digest(body),
            row_revisions={"att-" + "c" * 28: "rev-1"})
        return notice_receipt.native_text(body) + "\n" + receipt.receipt_marker(record, sid)

    def test_a_hearting_delivery_bound_to_this_session_is_not_the_persons_prompt(self):
        # PG-9: a Codex native-queue notice arrives as a user message carrying its marker; it
        # is the delivery, so a manual tidy booked before it still holds (no "new-input").
        import dispatch_contract
        root = self.iso.root / "dispatch"
        for harness in st.HARNESSES:
            with self.subTest(harness=harness):
                sid = "queued-" + harness
                text = self._queued_notice(root, sid, harness[0])
                original = self.prompt("내가 쓴 요청", harness, sid, now=1000)
                seat = self.seat(harness, sid)
                human = st.last_human_prompt_seq(seat)
                with mock.patch.object(dispatch_contract, "dispatch_state_roots",
                                       return_value=(root,)):
                    shown = self.prompt(text, harness, sid, now=1010)
                    other = self.seat(harness, sid + "-other")
                    self.prompt(text, harness, sid + "-other", now=1020)
                self.assertEqual(st.read_prompt_seq(seat), human + 1)
                self.assertEqual(st.last_human_prompt_seq(seat), human)
                self.assertEqual(shown["human"], original["human"])
                self.assertEqual(shown["received"][-1]["source"], "runtime")
                # The same text addressed to another session is that session's own prompt.
                self.assertEqual(st.last_human_prompt_seq(other), st.read_prompt_seq(other))

    def test_a_person_who_pastes_a_bound_notice_with_their_own_words_is_the_person(self):
        # The marker is visible and its record outlives the ACK, so a person can copy a notice
        # through its marker line and add an instruction.  The whole prompt decides, like
        # runtime_turn_source: the booked tidy ends with "new-input" and the open question
        # closes on this answer, while the notice alone still holds both.
        import dispatch_contract
        import human_wait_state
        root = self.iso.root / "dispatch"
        for index, harness in enumerate(st.HARNESSES):
            with self.subTest(harness=harness):
                sid = "pasted-" + harness
                notice = self._queued_notice(root, sid, "pqr"[index])
                # A keyed native seat, so the open question and the booking have their own slots.
                seat = st.session_seat(clear._peer_seat(harness, sid), harness, sid)
                self.prompt("내가 쓴 요청", harness, sid, now=1000, seat=seat)
                clear._write_reservation({
                    "schema": clear.SCHEMA, "nonce": "n-" + harness, "seat": {"key": seat.key},
                    "status": "reserved", "origin": "compact", "harness": harness, "sid": sid})
                self.assertTrue(human_wait_state.open_signal(
                    harness, sid, seat.key, "[결정 필요] X 또는 Y",
                    sequence=st.read_prompt_seq(seat), now=1001))
                with mock.patch.object(dispatch_contract, "dispatch_state_roots",
                                       return_value=(root,)):
                    alone = self.prompt(notice, harness, sid, now=1010, seat=seat)
                    self.assertEqual(alone["received"][-1]["source"], "runtime")
                    self.assertEqual(clear.read_reservation(seat.key)["status"], "reserved")
                    self.assertEqual(len(human_wait_state.read_signals(now=1011)), 1)
                    shown = self.prompt("이 알림 보고 X 말고 Y로 가 줘\n" + notice,
                                        harness, sid, now=1020, seat=seat)
                self.assertEqual(st.last_human_prompt_seq(seat), st.read_prompt_seq(seat))
                self.assertEqual(shown["human"]["text"], "이 알림 보고 X 말고 Y로 가 줘")
                held = clear.read_reservation(seat.key)
                self.assertEqual((held["status"], held["reason"]), ("cancelled", "new-input"))
                self.assertEqual(human_wait_state.read_signals(now=1021), [])

    RUNTIME_NOTICES = (
        ("claude", "<task-notification>\n실제 알림 본문\n</task-notification>", "실제 알림 본문"),
        ("claude", '<task-notification>\n<task-id>background-1</task-id>\n'
         '<tool-use-id>call-1</tool-use-id>\n<output-file>/private/task.output</output-file>\n'
         '<status>completed</status>\n<summary>Background shell command finished\n'
         'private second line</summary>\n</task-notification>',
         "Background shell command finished"),
        ("codex", '<subagent_notification>\n{"agent_id":"child-1",'
         '"status":{"completed":"검토를 마쳤습니다.\\nprivate second line"}}\n'
         '</subagent_notification>', "검토를 마쳤습니다."),
        ("codex", '<subagent_notification>{"agent_id":"child-2",'
         '"status":{"errored":"Worker stopped safely"}}</subagent_notification>',
         "Worker stopped safely"),
        ("opencode", '<subagent sessionID="child-1" state="completed" description="Review">\n'
         'Review complete\nprivate second line\n</subagent>', "Review complete"),
        ("opencode", '<shell id="shell-1" state="completed" command="printf done > result">\n'
         'Command output first line\nprivate second line\n</shell>', "Command output first line"),
        ("opencode", "<system-reminder>\nBackground command finished\n"
         "private second line\n</system-reminder>", "Background command finished"),
    )

    def test_runtime_envelopes_show_the_body_instead_of_metadata(self):
        for harness, text, expected in self.RUNTIME_NOTICES:
            with self.subTest(harness=harness, expected=expected):
                self.assertEqual(st.prompt_first_line(text), expected)
                pasted = '<pasted_content id="n">\n' + text + '\n</pasted_content>'
                self.assertEqual(st.prompt_first_line(pasted), expected)

    def test_runtime_arrivals_preserve_the_human_and_reach_the_expanded_view(self):
        from tools.fleet.explorer import live
        from tools.fleet.model import Session
        from tools.fleet.refresh import LiveSnapshot
        for index, (harness, text, expected) in enumerate(self.RUNTIME_NOTICES):
            with self.subTest(harness=harness, expected=expected):
                sid = "runtime-notice-" + str(index)
                original = self.prompt("Original human input", harness, sid)["human"]
                seat = self.seat(harness, sid)
                frontier = st.real_prompt_frontier(seat, harness, sid)
                for wrapped in (text, '<pasted_content id="n">\n' + text + '\n</pasted_content id="n">',
                                '<pasted_content id="n">\n' + text + '\n</pasted_content>'):
                    display = self.prompt(wrapped, harness, sid, now=1010)
                    self.assertEqual(display["human"], original)
                    self.assertEqual(st.real_prompt_frontier(seat, harness, sid), frontier)
                    self.assertIn(display["received"][-1]["source"], ("notification", "runtime"))
                    self.assertEqual(display["received"][-1]["text"], expected)
                row = Session(harness=harness, session_id=sid, session_tag="rt" + str(index),
                              pid=123, cwd=str(self.cwd), liveness="idle",
                              _prompt_display=display)
                vm = live.build_view_model(LiveSnapshot(sessions=[row]), now=1060)
                self.assertIn("Original human input", vm.sessions[0]["last_input"])
                self.assertNotIn(expected, vm.sessions[0]["last_input"])
                self.assertIn(expected, str(vm.sessions[0]["received_inputs"]))

    def test_compound_runtime_blocks_skip_metadata_and_keep_literal_body_markup(self):
        self.assertEqual(st.prompt_first_line(
            '<task-notification><task-id>only-metadata</task-id><status>completed</status>'
            '</task-notification>\n<system-reminder>Actual notice</system-reminder>'),
            "Actual notice")
        self.assertEqual(st.prompt_first_line(
            '<system-reminder><task-notification><summary>Nested notice</summary>'
            '</task-notification></system-reminder>'), "Nested notice")
        self.assertEqual(st.prompt_first_line(
            '<task-notification>\n<task-id>one</task-id>\nPlain body\n</task-notification>'),
            "Plain body")
        self.assertEqual(st.prompt_first_line(
            '<system-reminder>Use <filename> as written</system-reminder>'),
            "Use <filename> as written")
        self.assertEqual(st.prompt_first_line(
            '<subagent_notification>{bad JSON}</subagent_notification>'), "{bad JSON}")
        self.assertEqual(st.prompt_first_line(
            '<subagent_notification>{"agent_id":"a","status":{"completed":null}}'
            '</subagent_notification>'), '{"agent_id":"a","status":{"completed":null}}')

    def test_runtime_examples_with_human_framing_still_show_the_human(self):
        for harness, text, expected in self.RUNTIME_NOTICES:
            for framed in ("Please inspect this example\n" + text, text + "\nMy actual instruction"):
                with self.subTest(harness=harness, starts=framed[:30]):
                    self.assertEqual(st.runtime_turn_source(framed), "")
                    display = self.prompt(framed, harness, "framed-" + harness)
                    self.assertEqual(display["human"]["source"], "real")
                    self.assertEqual(display["human"]["text"], framed.splitlines()[0])
                    self.assertEqual(display["received"], [])
        unknown = "<unknown-tag>\nDo not guess this wrapper\n</unknown-tag>"
        self.assertEqual(st.prompt_first_line(unknown), "<unknown-tag>")
        malformed = "<task-notification>\nunfinished"
        self.assertEqual(st.prompt_first_line(malformed), "<task-notification>")

    def test_opencode_results_require_the_native_envelope_and_keep_user_shell_framing(self):
        for text in ('<subagent>Human XML example</subagent>',
                     '<shell>Human XML example</shell>',
                     '<subagent sessionID="child" state="unrecognized" description="Review">'
                     'Not a known completion</subagent>',
                     'The following shell command was executed by the user:\n'
                     '<shell id="shell-1" state="completed" command="pwd">/project</shell>'):
            with self.subTest(text=text[:50]):
                result = self.prompt(text, "opencode", "human-shell")
                self.assertEqual(st.runtime_turn_source(text), "")
                self.assertEqual(result["human"]["source"], "real")
                self.assertEqual(result["received"], [])

    def test_runtime_body_is_redacted_and_bounded_after_unwrapping(self):
        for text in ('<task-notification><summary>password=PRIVATE_SECRET\nsecond</summary></task-notification>',
                     '<subagent_notification>{"agent_id":"child",'
                     '"status":{"completed":"password=PRIVATE_SECRET"}}</subagent_notification>'):
            with self.subTest(text=text[:30]):
                self.assertNotIn("PRIVATE_SECRET", st.prompt_first_line(text))
                self.assertNotIn("second", st.prompt_first_line(text))
        text = '<system-reminder>\n' + "가" * 1000 + '\n</system-reminder>'
        line = st.prompt_first_line(text)
        self.assertEqual(len(line), st.PROMPT_LINE_CHARS)
        self.assertTrue(line.endswith("…"))

    def test_opencode_raw_native_fields_keep_the_human_frontier_and_show_the_body(self):
        # These are the installed producer templates, with no XML escaping.
        shell_commands = ("printf '\"'", 'printf "%s" "a > b"',
                          'printf "%s" "a > b"\nprintf done')
        notices = [
            f'<shell id="fixture" state="completed" command="{command}">\nACTUAL OUTPUT\n</shell>'
            for command in shell_commands
        ] + [
            f'<subagent sessionID="child" state="completed" description="{description}">\n'
            'ACTUAL OUTPUT\n</subagent>'
            for description in ('Review "', 'Review "a > b"')
        ]
        for index, notice in enumerate(notices):
            for style, delivered in enumerate((
                notice,
                '<pasted_content id="n">\n' + notice + '\n</pasted_content id="n">',
                '<pasted_content id="n">\n' + notice + '\n</pasted_content>',
            )):
                with self.subTest(index=index, style=style):
                    sid = f"native-raw-{index}-{style}"
                    original = self.prompt("Original person input", "opencode", sid)["human"]
                    seat = self.seat("opencode", sid)
                    frontier = st.real_prompt_frontier(seat, "opencode", sid)
                    value = self.prompt(delivered, "opencode", sid, now=1010)
                    self.assertEqual(value["human"], original)
                    self.assertEqual(st.real_prompt_frontier(seat, "opencode", sid), frontier)
                    self.assertEqual(value["received"][-1]["source"], "notification")
                    self.assertEqual(value["received"][-1]["text"], "ACTUAL OUTPUT")
                    for framed in ("My instruction\n" + delivered, delivered + "\nMy instruction"):
                        self.assertEqual(st.runtime_turn_source(framed), "")

    def test_ambiguous_raw_native_delimiters_preserve_the_human_and_show_unknown(self):
        def envelope(command, output):
            return (f'<shell id="fixture" state="completed" command="{command}">\n'
                    f'{output}\n</shell>')
        # Two different command/output pairs produce the same native text.
        notice = envelope("printf one", '<row a="v">\nACTUAL OUTPUT')
        self.assertEqual(notice, envelope('printf one">\n<row a="v', "ACTUAL OUTPUT"))
        sid = "native-ambiguous"
        original = self.prompt("Original person input", "opencode", sid)["human"]
        seat = self.seat("opencode", sid)
        frontier = st.real_prompt_frontier(seat, "opencode", sid)
        value = self.prompt(notice, "opencode", sid, now=1010)
        self.assertEqual(value["human"], original)
        self.assertEqual(st.real_prompt_frontier(seat, "opencode", sid), frontier)
        self.assertEqual(value["received"][-1]["source"], "notification")
        self.assertEqual(value["received"][-1]["text"], "?")

    def _runtime_cost(self, mode):
        try:
            result = subprocess.run([sys.executable, "-c", RUNTIME_COST_PROBE, str(ROOT), mode],
                                    cwd=ROOT, text=True, capture_output=True, timeout=3)
        except subprocess.TimeoutExpired:
            self.fail(f"large {mode} input exceeded the 3s guard (adapter budget: 4s)")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 6)
        # The original real hook took about 0.054s. Allow CI scheduling noise,
        # but retain substantial headroom below the actual adapter's 4s budget.
        for row in rows:
            self.assertLess(row["seconds"], 1.0, row)

    def test_large_complete_and_unclosed_runtime_inputs_have_bounded_parse_cost(self):
        self._runtime_cost("parse")

    def test_large_complete_and_unclosed_inputs_finish_the_actual_hook_in_budget(self):
        self._runtime_cost("hook")

    def test_verified_peer_and_self_request_do_not_replace_the_human(self):
        self.prompt("original", now=1000)
        origin = {"kind": "peer", "harness": "claude", "session_id": "prompt-session",
                  "request_digest": "a" * 64, "transfer_ref": "b" * 32,
                  "transfer_body_sha256": "c" * 64, "name": "Codex [73]"}
        result = self.prompt("human-looking peer first line\nprivate second line", now=1010, origin=origin)
        self.assertEqual(result["human"]["text"], "original")
        self.assertEqual((result["received"][-1]["sender"], result["received"][-1]["text"]),
                         ("Codex [73]", "human-looking peer first line"))
        with mock.patch.object(st, "matching_tidy_claim", return_value={"claim_id": "synthetic-claim"}):
            result = self.prompt("Hearting's own request", now=1020)
        self.assertEqual(result["human"]["text"], "original")
        self.assertEqual(result["received"][-1]["source"], "claim")

    def test_whole_paste_envelopes_show_the_human_body_and_preserve_framing(self):
        for harness in st.HARNESSES:
            with self.subTest(harness=harness):
                sid = "human-paste-" + harness
                for closing in ('</pasted_content id="p">', '</pasted_content>'):
                    wrapped = '<pasted_content id="p">\nHUMAN FIRST LINE\nsecond line\n' + closing
                    value = self.prompt(wrapped, harness, sid, now=1000)
                    self.assertEqual(value["human"]["source"], "real")
                    self.assertEqual(value["human"]["text"], "HUMAN FIRST LINE")
                    self.assertEqual(st.prompt_first_line("My framing\n" + wrapped), "My framing")
                    self.assertEqual(st.prompt_first_line(wrapped + "\nMy framing"),
                                     '<pasted_content id="p">')
                wrong_id = '<pasted_content id="p">\nbody\n</pasted_content id="different">'
                self.assertEqual(st.prompt_first_line(wrong_id), '<pasted_content id="p">')
                self.assertEqual(st.prompt_first_line('<pasted_content id="p">body</pasted_content>'),
                                 "body")

    def test_sealed_short_peer_paste_forms_preserve_the_human_frontier(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("_paste_display_peer_test",
                                                     ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = peer
        spec.loader.exec_module(peer)
        for harness in st.HARNESSES:
            with self.subTest(harness=harness):
                sid = "peer-paste-" + harness
                human = self.prompt("Original person input", harness, sid, now=1000)["human"]
                seat = self.seat(harness, sid)
                frontier = st.real_prompt_frontier(seat, harness, sid)
                text, _ref = peer.prepare_peer_message(
                    "PEER FIRST LINE\nprivate second line",
                    {"harness": "codex", "session_id": "paste-sender", "name": "Codex [ab]"},
                    {"harness": harness, "session_id": sid}, defer=True)
                for index, delivered in enumerate((text,
                        '<pasted_content id="p">\n' + text + '\n</pasted_content id="p">',
                        '<pasted_content id="p">\n' + text + '\n</pasted_content>')):
                    with mock.patch.object(st, "resolve_seat", return_value=seat):
                        st.run_hook(harness, "prompt", sid, cwd=str(self.cwd),
                                    prompt_text=delivered, now=1010 + index)
                    value = st.prompt_display(seat, harness, sid)
                    self.assertEqual(value["human"], human)
                    self.assertEqual(st.real_prompt_frontier(seat, harness, sid), frontier)
                    self.assertEqual(value["received"][-1]["source"], "peer")
                    self.assertEqual(value["received"][-1]["text"], "PEER FIRST LINE")

    def test_a_literal_approval_is_still_the_persons_input(self):
        result = self.prompt("진행")
        self.assertEqual(result["human"]["text"], "진행")
        self.assertEqual(result["received"], [])

    def test_a_large_received_message_uses_its_verified_original_not_the_preview(self):
        from types import SimpleNamespace
        reader = mock.Mock(return_value={"body": "원문 첫 줄\nPRIVATE SECOND LINE"})
        fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
        with mock.patch("importlib.util.spec_from_file_location", return_value=fake_spec), \
                mock.patch("importlib.util.module_from_spec", return_value=SimpleNamespace(read_peer_payload=reader)):
            line = st._received_prompt_line("Long peer message. Preview only", {"transfer_ref": "a" * 32})
        self.assertEqual(line, "원문 첫 줄")
        reader.assert_called_once_with("a" * 32)

    def test_long_multiline_and_secret_values_are_bounded_before_projection(self):
        result = self.prompt("가" * 1000 + "\nSECOND LINE NEVER SHOWN")
        self.assertEqual(len(result["human"]["text"]), st.PROMPT_LINE_CHARS)
        self.assertTrue(result["human"]["text"].endswith("…"))
        result = self.prompt("password=PRIVATE_SECRET\nsecond", now=1010)
        self.assertNotIn("PRIVATE_SECRET", result["human"]["text"])
        self.assertNotIn("second", result["human"]["text"])

    def test_exact_identity_no_alias_and_received_retention(self):
        self.prompt("original")
        for index in range(12):
            result = self.prompt("<system-reminder>notice %d</system-reminder>" % index, now=1100 + index)
        self.assertEqual(len(result["received"]), st.RECEIVED_INPUTS_LIMIT)
        self.assertEqual(result["human"]["text"], "original")
        for harness, sid in (("codex", "prompt-session"), ("claude", "other-session")):
            other = st.prompt_display(self.seat(harness, sid), harness, sid)
            self.assertIsNone(other["human"])
            self.assertEqual(other["received"], [])

    def test_cached_reader_reuses_unchanged_frontier_and_returns_owned_data(self):
        self.prompt("first")
        with mock.patch.object(prompt_inputs.os, "open", wraps=prompt_inputs.os.open) as opened:
            first = prompt_inputs.read("claude", "prompt-session")
            self.assertEqual(opened.call_count, 1)
            first["human"]["text"] = "not a cache edit"
            self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human"]["text"], "first")
            self.assertEqual(opened.call_count, 1)
        self.prompt("second", now=1010)
        self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human"]["text"], "second")

    def test_live_required_detail_and_merge_publish_the_exact_frontier(self):
        from tools.fleet import details
        from tools.fleet.explorer import live
        from tools.fleet.model import Session
        from tools.fleet.refresh import LiveSnapshot
        self.prompt("visible through the live detail path")
        row = Session(harness="claude", session_id="prompt-session", session_tag="73",
                      pid=123, proc_start="original", cwd=str(self.cwd),
                      liveness="idle", process_role="conversation")
        basic = LiveSnapshot(sessions=[row], observations={"basic": {
            "state": "ok", "generation_identity": "synthetic-basic", "all_new": True}, "required-details": {
            "state": "ok", "generation_identity": "synthetic-basic", "all_new": True}})
        observed = details.enrich(basic, required_only=True)
        merged = details.merge(basic, observed)
        self.assertIsNotNone(merged.sessions[0]._prompt_display)
        self.assertEqual(merged.sessions[0]._prompt_display["human"]["text"],
                         "visible through the live detail path")
        self.assertIsNone(basic.sessions[0]._prompt_display)
        vm = live.build_view_model(merged, now=1060)
        self.assertIn("visible through the live detail path", vm.sessions[0]["last_input"])
        other = LiveSnapshot(sessions=[Session(harness="claude", session_id="other-session",
            session_tag="74", pid=123, proc_start="original", cwd=str(self.cwd), liveness="idle")])
        self.assertIsNone(details.merge(other, observed).sessions[0]._prompt_display)
        with mock.patch.object(prompt_inputs, "read", side_effect=OSError("synthetic unreadable frontier")):
            unavailable = details.enrich(basic, required_only=True)
        self.assertIsNone(details.merge(basic, unavailable).sessions[0]._prompt_display)

    def test_missing_corrupt_replaced_and_legacy_frontiers_stay_honest(self):
        self.assertIsNone(prompt_inputs.read("claude", "prompt-session"))
        self.prompt("first")
        path = st._prompt_seq_path(self.seat())
        value = st.read_json(path)
        value.pop("last_human_prompt")
        value["request_excerpt"] = "first"  # A legacy frontier that retained this exact quote.
        st.atomic_write_json(path, value)
        self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human"]["text"], "first")
        value.pop("last_human_seq")  # The old frontier used seq before source-specific counters.
        value.pop("prior_human_seq")
        st.atomic_write_json(path, value)
        self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human"]["text"], "first")
        value.pop("request_excerpt")
        st.atomic_write_json(path, value)
        self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human_state"], "unknown")
        path.write_text("{broken")
        self.assertIsNone(prompt_inputs.read("claude", "prompt-session"))
        path.write_text("null")
        with mock.patch.object(prompt_inputs.os, "open", wraps=prompt_inputs.os.open) as opened:
            self.assertEqual(prompt_inputs.read("claude", "prompt-session")["human_state"], "unknown")
            self.assertEqual(opened.call_count, 1)
        path.unlink()
        path.symlink_to(self.cwd / "do-not-read")
        self.assertIsNone(prompt_inputs.read("claude", "prompt-session"))

    def test_a_rekeyed_frontier_does_not_borrow_another_native_sessions_payload(self):
        self.prompt("original")
        value = st.read_json(st._prompt_seq_path(self.seat()))
        value["sid"] = "other-session"
        value["request_excerpt"] = "not an exact quote for this identity"
        st.atomic_write_json(st._prompt_seq_path(self.seat(sid="other-session")), value)
        other = prompt_inputs.read("claude", "other-session")
        self.assertEqual(other["human_state"], "unknown")
        self.assertIsNone(other["human"])
        self.assertEqual(other["received"], [])


if __name__ == "__main__":
    unittest.main()
