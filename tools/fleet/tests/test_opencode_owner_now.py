"""Exact OpenCode headless activity, with read-only and first-publication bounds."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import refresh_title as rt
from fleet import titles
from fleet import render
from fleet.collectors import dispatch
from fleet.model import DispatchJob


class OwnerNowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"AGENT_HOME": str(self.root)})
        self.env.start()
        self.db = self.root / "runtime/data/opencode/opencode.db"
        self.db.parent.mkdir(parents=True)
        self.con = sqlite3.connect(self.db)
        self.con.executescript(
            "CREATE TABLE message(id TEXT,session_id TEXT,data TEXT,time_created INT,time_updated INT);"
            "CREATE TABLE part(message_id TEXT,session_id TEXT,data TEXT,time_updated INT);")
        self.log = self.root / ".dispatch/logs/owner.att-test.opencode.jsonl"
        self.log.parent.mkdir(parents=True)
        self.log.write_text(json.dumps({"type": "dispatch.supervisor.session", "runtime": "opencode",
            "session_id": "ses_owner", "parent_attempt_id": "att-test", "cwd": "/wt"}) + "\n")
        self.job = DispatchJob(key="opencode", slug="owner")
        self.job.harness = "opencode"
        self.job.attempt_id = "att-test"
        self.job._log_file = str(self.log)
        self.job._registry_metadata = {"opencode_runtime_dir": str(self.root / "runtime")}
        dispatch._OPENCODE_ACTIVITY_CACHE.clear()
        dispatch._OPENCODE_ATTEMPT_CACHE.clear()

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()
        dispatch._OPENCODE_ACTIVITY_CACHE.clear()
        dispatch._OPENCODE_ATTEMPT_CACHE.clear()

    def message(self, mid="m1", sid="ses_owner", role="assistant", completed=False, stamp=1000):
        self.con.execute("INSERT INTO message VALUES(?,?,?,?,?)", (mid,sid,json.dumps({
            "role":role,"time":{"completed":stamp} if completed else {}}),stamp,stamp))
        self.con.commit()

    def part(self, value, mid="m1", sid="ses_owner", stamp=1000):
        self.con.execute("INSERT INTO part VALUES(?,?,?,?)", (mid,sid,json.dumps(value),stamp))
        self.con.commit()

    def enrich(self):
        dispatch._enrich_opencode_attempt_session(self.job)
        with mock.patch.object(titles,"fresh_title",return_value=None), \
                mock.patch.object(titles,"fresh_summary_with_ts",return_value=(None,None)):
            dispatch._enrich_attempt_summary(self.job)

    def test_running_command_and_public_summary_use_common_fields(self):
        self.message()
        self.part({"type":"text","text":"데이터를 확인하고 학습을 준비합니다"})
        self.part({"type":"tool","tool":"bash","state":{"status":"running",
            "input":{"command":"env DEVICE=1 python train.py"},"output":"PRIVATE OUTPUT"}}, stamp=2000)
        self.enrich()
        self.assertEqual(self.job.exec_tool,{"name":"python"})
        self.assertEqual(self.job.summary,"데이터를 확인하고 학습을 준비합니다")
        self.assertEqual(self.job.summary_ts,1.0)
        row = render._plain(render._context_detail_row(self.job,term_width=168)[0])
        self.assertIn("⚙ python",row)
        self.assertIn("데이터를 확인하고 학습을 준비합니다",row)

    def test_completed_tools_are_not_running(self):
        self.message(completed=True)
        self.part({"type":"tool","tool":"bash","state":{"status":"completed","input":{"command":"python"}}})
        self.enrich()
        self.assertIsNone(self.job.exec_tool)
        self.assertIsNone(self.job.summary)

    def test_finished_message_cannot_keep_an_unfinished_tool_badge(self):
        self.message(completed=True)
        self.part({"type":"tool","tool":"bash","state":{"status":"running"}})
        self.enrich()
        self.assertIsNone(self.job.exec_tool)

    def test_neighbours_user_reasoning_and_synthetic_text_are_excluded(self):
        self.message()
        self.message("foreign","ses_other",stamp=4000)
        self.message("user",role="user",stamp=3000)
        self.part({"type":"text","text":"FOREIGN"},"foreign","ses_other",4000)
        self.part({"type":"text","text":"USER SECRET"},"user",stamp=3000)
        self.part({"type":"reasoning","text":"HIDDEN"},stamp=2000)
        self.part({"type":"text","text":"SYNTHETIC","synthetic":True},stamp=1500)
        self.enrich()
        self.assertIsNone(self.job.summary)
        self.assertEqual(rt._opencode_text({"type":"reasoning","text":"HIDDEN"}),"")

    def test_summary_sidecar_takes_precedence_over_native_fallback(self):
        self.message()
        self.part({"type":"text","text":"Native public text"})
        dispatch._enrich_opencode_attempt_session(self.job)
        with mock.patch.object(titles,"fresh_title",return_value=None), \
                mock.patch.object(titles,"fresh_summary_with_ts",return_value=("요약 생산자의 NOW",9)):
            dispatch._enrich_attempt_summary(self.job)
        self.assertEqual(self.job.summary,"요약 생산자의 NOW")

    def test_first_publication_does_not_open_snapshot(self):
        with mock.patch.object(rt,"_opencode_snapshot",side_effect=AssertionError("DB opened")):
            dispatch._enrich_opencode_attempt_session(self.job,fast_first=True)
        self.assertEqual(self.job._runtime_session_id,"ses_owner")

    def test_unchanged_native_source_is_read_once(self):
        self.message()
        self.part({"type":"text","text":"Visible"})
        with mock.patch.object(rt,"_opencode_snapshot",wraps=rt._opencode_snapshot) as snapshot:
            self.enrich()
            self.enrich()
        self.assertEqual(snapshot.call_count,1)

    def test_missing_private_runtime_and_foreign_attempt_do_not_fall_back(self):
        self.message()
        self.part({"type":"text","text":"Visible"})
        self.job._registry_metadata = {}
        self.enrich()
        self.assertIsNone(self.job.summary)
        self.job.attempt_id = "att-foreign"
        with mock.patch.object(rt,"_opencode_snapshot",side_effect=AssertionError("DB opened")):
            dispatch._enrich_opencode_attempt_session(self.job)

    def test_source_cap_and_changing_source_fail_closed(self):
        self.message()
        self.part({"type":"text","text":"Visible"})
        with mock.patch.object(dispatch,"_OPENCODE_ACTIVITY_BYTES",1), \
                mock.patch.object(rt,"_opencode_snapshot",side_effect=AssertionError("DB opened")):
            self.enrich()
        self.assertIsNone(self.job.summary)
        with mock.patch.object(rt,"_opencode_snapshot",side_effect=OSError("changed")):
            self.enrich()
        self.assertIsNone(self.job.summary)

    def test_model_turn_before_public_text_has_now(self):
        self.message()
        self.enrich()
        self.assertEqual(self.job.summary,"model responding")

    def test_malformed_native_time_does_not_assert_a_running_call(self):
        self.message()
        self.con.execute("UPDATE message SET data=?",(json.dumps({"role":"assistant","time":"bad"}),))
        self.con.commit()
        self.part({"type":"tool","tool":"bash","state":{"status":"running"}})
        self.enrich()
        self.assertIsNone(self.job.exec_tool)
        self.assertIsNone(self.job.summary)

    def test_foreign_supervisor_announcement_is_refused(self):
        self.log.write_text(self.log.read_text().replace("att-test","att-other"))
        self.enrich()
        self.assertIsNone(self.job.summary)
        self.assertEqual(self.job.association_ambiguity,"opencode-announcement-identity-mismatch")

    def test_streaming_copy_stops_at_byte_budget_when_source_grows(self):
        src = self.root / "growing"
        target = self.root / "copy"
        src.write_bytes(b"x"*40)
        smaller = mock.Mock(st_size=1,st_mode=0o600)
        native_stat = os.stat
        def source_stat(path, *args, **kwargs):
            return smaller if str(path) == str(src) else native_stat(path,*args,**kwargs)
        with mock.patch.object(rt.os,"stat",side_effect=source_stat), \
                mock.patch.object(rt,"fcntl",None):
            with self.assertRaises(OSError):
                rt._copy_opencode_file(str(src),str(target),max_bytes=10)
        self.assertLessEqual(target.stat().st_size,10)

    def test_tf_and_bc_terminal_protocol_is_readable(self):
        self.message(completed=True)
        self.part({"type":"text","text":"artifact: /private/path\nverdict: BLOCKED\nblocker: runtime_wait"})
        self.enrich()
        self.assertEqual(self.job.summary,"BLOCKED · runtime_wait")
        self.assertNotIn("/private",self.job.summary)
        self.assertEqual(rt.read_opencode_activity(self.con,"ses_missing")["summary"],None)

    def test_live_wal_database_is_unchanged_by_observation(self):
        self.con.execute("PRAGMA journal_mode=WAL")
        self.message(completed=True)
        self.part({"type":"text","text":"WAITING_FOR_RUNTIME"})
        paths=[Path(str(self.db)+suffix) for suffix in ("","-wal","-shm")]
        before={str(p):p.read_bytes() for p in paths if p.exists()}
        self.enrich()
        self.assertEqual(self.job.summary,"runtime waiting")
        self.assertEqual(before,{str(p):p.read_bytes() for p in paths if p.exists()})

    def test_announcement_survives_a_large_control_only_tail(self):
        with self.log.open("a") as f:
            for _ in range(5000):
                f.write(json.dumps({"type":"dispatch.supervisor.resource-parked","padding":"x"*100})+"\n")
        dispatch._enrich_opencode_attempt_session(self.job,fast_first=True)
        self.assertEqual(self.job._runtime_session_id,"ses_owner")


if __name__ == "__main__":
    unittest.main()
