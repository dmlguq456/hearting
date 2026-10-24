#!/usr/bin/env python3
"""OpenCode context window and summary subtitle — the fields that never rendered.

Four independent producer defects kept every opencode row blank where claude/codex rows
were populated. Each class below pins one of them against the real on-disk shapes:

  1. ``opencode run --format json`` wraps every event in a singular ``part`` envelope the
     transcript text extractor did not descend into, so a dispatch attempt yielded zero
     characters and the refresher wrote an empty title and no summary at all.
  2. The SQLite refresh cursor anchored on the ``message`` table, which holds only
     per-message metadata. Conversational text lives in ``part``.
  3. ``collectors/dispatch.py`` associates native attempt context by exact session/attempt.
  4. A catalogue window is not an applied trigger; exact chat-parameter evidence now feeds
     the registered row.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import refresh_title as rt                       # noqa: E402
from fleet.collectors import dispatch                       # noqa: E402
from fleet.collectors import opencode                       # noqa: E402
from fleet.model import DispatchJob, Session                # noqa: E402
from fleet.context_policy import SCHEMA, policy_path       # noqa: E402


def _envelope(event_type, part):
    """One line of `opencode run --format json` output."""
    return {"type": event_type, "timestamp": 1786081697040,
            "sessionID": "ses_test", "part": part}


_TEXT_LINE = _envelope("text", {
    "id": "prt_1", "messageID": "msg_1", "sessionID": "ses_test", "type": "text",
    "text": "Reading the precedent files before writing the parser."})
_TOOL_LINE = _envelope("tool_use", {
    "id": "prt_2", "type": "tool", "callID": "call_1",
    "state": {"status": "completed", "output": "SECRET TOOL OUTPUT"}})
_STEP_LINE = _envelope("step_finish", {
    "id": "prt_3", "type": "step-finish", "reason": "tool-calls",
    "tokens": {"total": 19691, "input": 1611, "output": 160, "reasoning": 40,
               "cache": {"write": 300, "read": 17920}},
    "cost": 0.0076186})


class TranscriptTextTest(unittest.TestCase):
    """Defect 1 — the `part` envelope."""

    def test_part_envelope_yields_assistant_text(self):
        raw = "\n".join(json.dumps(line) for line in
                        (_STEP_LINE, _TEXT_LINE, _TOOL_LINE)) + "\n"
        text = rt._delta_text(raw, harness="opencode")
        self.assertIn("Reading the precedent files", text)

    def test_tool_and_step_envelopes_contribute_nothing(self):
        raw = "\n".join(json.dumps(line) for line in (_TOOL_LINE, _STEP_LINE)) + "\n"
        self.assertEqual(rt._delta_text(raw, harness="opencode"), "")

    def test_tool_output_never_leaks_into_the_summary_source(self):
        raw = json.dumps(_TOOL_LINE) + "\n"
        self.assertNotIn("SECRET", rt._delta_text(raw, harness="opencode"))


class MessageTableTest(unittest.TestCase):
    """Defect 2 — the refresh cursor must anchor on the table that holds text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "opencode.db")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT)")
        con.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, data TEXT)")
        # Real shapes: `message` rows are metadata only; the text lives in `part`.
        con.execute("INSERT INTO message VALUES (?,?,?)", (
            "msg_1", "ses_test",
            json.dumps({"role": "assistant", "modelID": "glm-5.2",
                        "providerID": "opencode-go", "cost": 0.02,
                        "tokens": {"input": 1611, "cache": {"read": 17920, "write": 0}}})))
        con.execute("INSERT INTO part VALUES (?,?,?,?)", (
            "prt_1", "msg_1", "ses_test",
            json.dumps({"type": "text", "text": "Now writing the artifact."})))
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_part_table_is_selected(self):
        self.assertEqual(rt.opencode_message_table(self.db), "part")

    def test_delta_reads_conversational_text(self):
        table = rt.opencode_message_table(self.db)
        text, cursor, _ = rt.read_opencode_delta(self.db, "ses_test", 0, table=table)
        self.assertIn("Now writing the artifact.", text)
        self.assertGreater(cursor, 0)

    def test_collector_and_refresher_agree_on_table_order(self):
        # A cursor written under one module's order must stay readable by the other.
        self.assertEqual(tuple(opencode._MESSAGE_TABLES), tuple(rt.OPENCODE_MESSAGE_TABLES))


class _Registry:
    """models.dev cache with one model id published at two different windows."""

    PAYLOAD = {
        "opencode-go": {"id": "opencode-go", "models": {
            "glm-5.2": {"limit": {"context": 1000000, "output": 131072}}}},
        "nano-gpt": {"id": "nano-gpt", "models": {
            "TEE/glm-5.2": {"limit": {"context": 1048576}}}},
    }

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "models.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.PAYLOAD, handle)
        self.saved = os.environ.get("OPENCODE_MODELS")
        os.environ["OPENCODE_MODELS"] = path
        opencode._REG.update(ts=0.0, map=None, by_provider=None)
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop("OPENCODE_MODELS", None)
        else:
            os.environ["OPENCODE_MODELS"] = self.saved
        opencode._REG.update(ts=0.0, map=None, by_provider=None)
        self.tmp.cleanup()


class ContextWindowTest(unittest.TestCase):
    """Defect 4 — providerID decides the window, not the cross-provider maximum."""

    def test_provider_scoped_window_wins(self):
        with _Registry():
            self.assertEqual(opencode._model_ctx_limit("glm-5.2", "opencode-go"), 1000000)
            self.assertEqual(opencode._model_ctx_limit("glm-5.2", "nano-gpt"), 1048576)

    def test_unknown_provider_falls_back_to_the_widest_window(self):
        # Over-large window understates ctx% — the safer direction to be wrong.
        with _Registry():
            self.assertEqual(opencode._model_ctx_limit("glm-5.2", "no-such"), 1048576)
            self.assertEqual(opencode._model_ctx_limit("glm-5.2"), 1048576)

    def test_missing_registry_reports_no_window(self):
        saved = os.environ.get("OPENCODE_MODELS")
        os.environ["OPENCODE_MODELS"] = "/nonexistent/models.json"
        opencode._REG.update(ts=0.0, map=None, by_provider=None)
        try:
            self.assertIsNone(opencode._model_ctx_limit("glm-5.2", "opencode-go"))
        finally:
            if saved is None:
                os.environ.pop("OPENCODE_MODELS", None)
            else:
                os.environ["OPENCODE_MODELS"] = saved
            opencode._REG.update(ts=0.0, map=None, by_provider=None)


class DispatchTelemetryTest(unittest.TestCase):
    """Defect 3 — an opencode job row must carry its own ctx%."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, ".dispatch", "logs")
        os.makedirs(self.logs)
        self.saved_home = os.environ.get("AGENT_HOME")
        self.saved_state = os.environ.get("XDG_STATE_HOME")
        self.saved_registry = os.environ.get("FLEET_SESSION_REGISTRY_DIR")
        os.environ["AGENT_HOME"] = self.tmp.name
        os.environ["XDG_STATE_HOME"] = os.path.join(self.tmp.name, "state")
        os.environ["FLEET_SESSION_REGISTRY_DIR"] = os.path.join(
            self.tmp.name, "state", "agent-fleet", "sessions")
        dispatch._OPENCODE_ATTEMPT_CACHE.clear()

    def tearDown(self):
        if self.saved_home is None:
            os.environ.pop("AGENT_HOME", None)
        else:
            os.environ["AGENT_HOME"] = self.saved_home
        if self.saved_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.saved_state
        if self.saved_registry is None:
            os.environ.pop("FLEET_SESSION_REGISTRY_DIR", None)
        else:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = self.saved_registry
        dispatch._OPENCODE_ATTEMPT_CACHE.clear()
        self.tmp.cleanup()

    def _job(self, lines, attempt="att-1", harness="opencode"):
        path = os.path.join(self.logs, "slug.%s.%s.jsonl" % (attempt, harness))
        with open(path, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        job = DispatchJob(key="code", slug="slug")
        job.harness = harness
        job.attempt_id = attempt
        job.model = "opencode-go/glm-5.2"
        job._log_file = path
        return job

    def test_last_step_finish_without_native_policy_stays_unknown(self):
        early = json.loads(json.dumps(_STEP_LINE))
        early["part"]["tokens"] = {"input": 100, "cache": {"read": 0, "write": 0}}
        job = self._job([early, _TEXT_LINE, _STEP_LINE])
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        # OpenCode 1.19.0 compaction uses tokens.total when present.
        self.assertEqual(job.active_context_tokens, 19691)
        self.assertIsNone(job.context_window_tokens)
        self.assertIsNone(job.context_display_window_tokens)
        self.assertIsNone(job.ctx_pct)
        self.assertEqual(job.context_policy["state"], "unknown")
        self.assertEqual(job._runtime_session_id, "ses_test")

    def test_attempt_uses_exact_chat_params_policy_not_catalogue_window(self):
        job = self._job([_STEP_LINE])
        now = time.time()
        path = policy_path("opencode", attempt_id=job.attempt_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": SCHEMA, "harness": "opencode", "session_id": "ses_test",
            "pid": 123, "proc_start": "456", "attempt_id": job.attempt_id,
            "model": "opencode-go/glm-5.2", "observed_at": now,
            "policy": {"state": "applied", "source": "opencode-native-chat-params",
                       "runtime_version": "1.19.0", "configured_request_tokens": 800000,
                       "configured_request": "800000", "effective_window_tokens": 1000000,
                       "applied_trigger_tokens": 40000, "output_allowance_tokens": 8192,
                       "compaction_reserve_tokens": 20000, "enabled": True,
                       "enforced": True, "disabled": False},
        }), encoding="utf-8")
        os.chmod(path, 0o600)
        os.utime(job._log_file, (now + 2, now + 2))
        dispatch._enrich_opencode_attempt_session(job)
        self.assertEqual(job.active_context_tokens, 19691)
        self.assertEqual(job.context_window_tokens, 1000000)
        self.assertEqual(job.context_display_window_tokens, 40000)
        self.assertEqual(job.ctx_pct, 49)
        self.assertEqual(job.context_policy["configured_request_tokens"], 800000)

    def test_native_total_is_the_trigger_count_when_present(self):
        job = self._job([_STEP_LINE])
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        self.assertEqual(job.active_context_tokens, 19691)

    def test_native_fallback_sum_includes_output_and_cache(self):
        step = json.loads(json.dumps(_STEP_LINE))
        del step["part"]["tokens"]["total"]
        job = self._job([step])
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        self.assertEqual(job.active_context_tokens, 19991)

    def test_transcript_without_tokens_reports_no_context(self):
        job = self._job([_TEXT_LINE, _TOOL_LINE])
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        self.assertIsNone(job.ctx_pct)

    def test_mixed_session_ids_refuse_to_guess(self):
        other = json.loads(json.dumps(_STEP_LINE))
        other["sessionID"] = "ses_other"
        job = self._job([_STEP_LINE, other])
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        self.assertEqual(job.association_ambiguity, "multiple-stream-session-ids")
        self.assertIsNone(job.ctx_pct)

    def test_foreign_attempt_log_is_refused(self):
        job = self._job([_STEP_LINE], attempt="att-1")
        job.attempt_id = "att-2"           # log basename no longer binds this attempt
        with _Registry():
            dispatch._enrich_opencode_attempt_session(job)
        self.assertIsNone(job.ctx_pct)

    def test_attempt_summary_sidecar_is_readable_for_opencode(self):
        # The sidecar reader shares `_owned_attempt_log_path`; before the fix it failed
        # closed on harness, so an opencode job could never show a subtitle.
        job = self._job([_TEXT_LINE])
        self.assertIsNotNone(dispatch._owned_attempt_log_path(job))


class CurrentSessionBindingTest(unittest.TestCase):
    """Conversation enrichment requires the current PID/start TUI selection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "FLEET_TITLE_STATE_DIR": str(self.root / "titles"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = self.root / "opencode.db"
        self.con = sqlite3.connect(self.db)
        self.addCleanup(self.con.close)
        self.con.executescript(
            "CREATE TABLE session (id TEXT, slug TEXT, agent TEXT, model TEXT, cost REAL, "
            "tokens_input INT, tokens_output INT, tokens_reasoning INT, time_updated INT, "
            "parent_id TEXT, directory TEXT);"
            "CREATE TABLE message (id TEXT, session_id TEXT, data TEXT, time_updated INT);")

    def _add(self, sid, *, updated, context, slug=None, model=None):
        self.con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            sid, slug or sid, None, model, 1.0, context, 2, 3, updated, None, "/repo"))
        self.con.execute("INSERT INTO message VALUES (?,?,?,?)", (
            "m-" + sid, sid, json.dumps({"tokens": {"total": context}}), updated))
        self.con.commit()

    def _session(self, *, prior=None, attempt=None):
        sess = Session(harness="opencode", pid=4242, proc_start="123", cwd="/repo")
        sess.session_id = prior
        sess.title = "STALE TITLE"
        sess.summary = "STALE SUMMARY"
        sess.active_context_tokens = 999
        sess.mtime = 999.0
        sess._refresh_source = {"session_id": "stale"}
        if attempt:
            sess.attempt_id = attempt
        return sess

    def _title_patches(self):
        title_map = {"ses_a": "Title A", "ses_b": "Title B",
                     "attempt-exact": "Attempt title"}
        summary_map = {"ses_a": "Summary A", "ses_b": "Summary B",
                       "attempt-exact": "Attempt summary"}
        return (
            mock.patch.object(opencode.titles, "attempt_sid",
                              side_effect=lambda attempt: "attempt-exact" if attempt else None),
            mock.patch.object(opencode.titles, "last_title",
                              side_effect=lambda sid, harness=None: title_map.get(sid)),
            mock.patch.object(opencode.titles, "fresh_summary_with_ts",
                              side_effect=lambda sid, harness=None:
                              (summary_map.get(sid), 10.0 if sid in summary_map else None)),
        )

    def _run_v1(self, sess, claim, registry=None):
        with mock.patch.object(opencode, "session_of_process", return_value=claim), \
                mock.patch.object(opencode, "_proc_start_ticks", return_value="123"), \
                mock.patch.object(opencode, "_db", return_value=str(self.db)), \
                mock.patch.object(opencode.session_registry, "read", return_value=registry), \
                mock.patch.object(opencode, "_enrich_v2", return_value=False) as v2_mock, \
                mock.patch("fleet.context_policy.read_policy", return_value=None), \
                self._title_patches()[0], self._title_patches()[1], self._title_patches()[2]:
            opencode.enrich(sess)
        return sess, v2_mock

    def test_no_selection_or_argv_only_does_not_bind_the_single_same_cwd_candidate(self):
        self._add("ses_a", updated=999999, context=41)
        for claim in ((None, ""), ("ses_a", "opencode-argv")):
            with self.subTest(claim=claim):
                sess = self._session(prior="ses_a", attempt="att-exact")
                sess, enrich_v2 = self._run_v1(sess, claim)
                enrich_v2.assert_not_called()
                self.assertIsNone(sess.session_id)
                self.assertEqual(sess.session_identity_evidence["verdict"], "unobserved")
                self.assertIsNone(sess.session_identity_evidence["claims"].get("opencode-argv"))
                if claim[1] == "opencode-argv":
                    self.assertEqual(sess.session_identity_evidence["started_on"], "ses_a")
                self.assertEqual((sess.title, sess.summary),
                                 ("Attempt title", "Attempt summary"))
                self.assertIsNone(sess.active_context_tokens)
                self.assertIsNone(sess._refresh_source)
                self.assertIsNone(sess.mtime)

    def test_exact_selection_beats_stale_projected_identity_and_newer_neighbor(self):
        self._add("ses_a", updated=999999, context=99)
        self._add("ses_b", updated=1000, context=22)
        sess, _v2 = self._run_v1(self._session(prior="ses_a"),
                                 ("ses_b", "opencode-tui-selection"))
        self.assertEqual(sess.session_id, "ses_b")
        self.assertEqual(sess.session_identity_evidence["verdict"], "resolved")
        self.assertEqual(sess.session_identity_evidence["current_source"],
                         "opencode-tui-selection")
        self.assertEqual(sess.session_identity_evidence["prior_projection"], "ses_a")
        self.assertEqual(sess.session_identity_evidence["claims"],
                         {"opencode-tui-selection": "ses_b"})
        self.assertEqual((sess.title, sess.summary), ("Title B", "Summary B"))
        self.assertEqual(sess.active_context_tokens, 22)
        self.assertEqual(sess._refresh_source["session_id"], "ses_b")
        self.assertEqual(sess.mtime, 1.0)

    def test_missing_exact_selection_row_never_borrows_the_only_neighbor(self):
        self._add("ses_a", updated=999999, context=99)
        sess, _v2 = self._run_v1(self._session(),
                                 ("ses_b", "opencode-tui-selection"))
        self.assertEqual(sess.session_id, "ses_b")
        self.assertIsNone(sess.title)
        self.assertIsNone(sess.summary)
        self.assertIsNone(sess.active_context_tokens)
        self.assertIsNone(sess._refresh_source)
        self.assertIsNone(sess.mtime)

    def test_pid_reuse_since_process_scan_rejects_the_new_process_selection(self):
        sess = self._session()
        with mock.patch.object(opencode, "_proc_start_ticks", return_value="124"), \
                mock.patch.object(opencode, "session_of_process",
                                  return_value=("ses_b", "opencode-tui-selection")) as identity, \
                mock.patch.object(opencode, "_db") as db_path:
            opencode.enrich(sess)
        identity.assert_not_called()
        db_path.assert_not_called()
        self.assertIsNone(sess.session_id)
        self.assertEqual(sess.session_identity_evidence["verdict"], "unobserved")
        self.assertIsNone(sess.title)
        self.assertIsNone(sess._refresh_source)

    def _run_v2(self, sess, claim, observed):
        runtime = mock.Mock()
        runtime.credential_store_present.return_value = True
        runtime.read_session_observation.return_value = observed
        runtime.list_session_candidates.return_value = [{"id": "ses_a", "generation": "v2"}]
        with mock.patch.object(opencode, "session_of_process", return_value=claim), \
                mock.patch.object(opencode, "_proc_start_ticks", return_value="123"), \
                mock.patch.object(opencode, "_db", return_value=str(self.db)), \
                mock.patch.object(opencode.session_registry, "read", return_value=None), \
                mock.patch.object(opencode, "_opencode_v2_runtime", return_value=runtime), \
                mock.patch.object(opencode, "_attach_v2_shell_tasks"), \
                mock.patch("fleet.context_policy.read_policy", return_value=None), \
                self._title_patches()[0], self._title_patches()[1], self._title_patches()[2]:
            opencode.enrich(sess)
        return sess, runtime

    def test_v2_exact_selection_reads_only_its_title_summary_context_and_refresh_source(self):
        observed = {"state": "observed", "generation": "v2", "cursor": 8,
                    "cursor_kind": "session-message-rowid-v1",
                    "session": {"directory": "/repo", "slug": "slug-b",
                                "model": {"id": "model-b", "providerID": "provider"},
                                "time_updated": 1000, "tokens_input": 3},
                    "messages": [{"type": "assistant", "tokens": {"total": 123}}]}
        sess, runtime = self._run_v2(self._session(prior="ses_a"),
                                     ("ses_b", "opencode-tui-selection"), observed)
        self.assertEqual(runtime.read_session_observation.call_args.args[0], "ses_b")
        runtime.list_session_candidates.assert_not_called()
        self.assertEqual(sess.session_id, "ses_b")
        self.assertEqual((sess.title, sess.summary), ("Title B", "Summary B"))
        self.assertEqual(sess.active_context_tokens, 123)
        self.assertEqual(sess._refresh_source["session_id"], "ses_b")

    def test_v2_real_reader_keeps_selected_directory_when_launch_cwd_differs(self):
        from fleet import session_cwd

        db = self.root / "opencode-v2.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE credential (id TEXT, integration_id TEXT, value TEXT)")
            con.execute(
                "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, "
                "slug TEXT, title TEXT, agent TEXT, model TEXT, time_created INT, "
                "time_updated INT, cost REAL, tokens_input INT, tokens_output INT, "
                "tokens_reasoning INT)")
            con.execute(
                "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
                "seq INT, time_created INT, time_updated INT, data TEXT)")
            con.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "ses_a", "/other/project", None, "slug-a", "native-a", "build",
                json.dumps({"id": "model-a", "providerID": "provider"}),
                1000, 9000, 9.0, 90, 0, 0))
            con.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "ses_b", "/selected/project", None, "slug-b", "native-b", "build",
                json.dumps({"id": "model-b", "providerID": "provider"}),
                1000, 2000, 0.2, 7, 3, 1))
            con.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)", (
                "msg-b", "ses_b", "assistant", 1, 1500, 2000,
                json.dumps({"content": [{"type": "text", "text": "selected B"}],
                            "tokens": {"total": 123},
                            "time": {"created": 1500, "completed": 2000}})))
        before = db.read_bytes()
        runtime = opencode._opencode_v2_runtime()
        session = self._session(prior="ses_a")
        session.cwd = "/launch/project"
        shell_key = opencode._v2_shell_key(str(db), "ses_b", None)
        self.addCleanup(opencode._V2_SHELL_TASKS.pop, shell_key, None)
        with mock.patch.object(runtime, "read_session_observation",
                               wraps=runtime.read_session_observation) as reader, \
                mock.patch.object(opencode, "session_of_process",
                                  return_value=("ses_b", "opencode-tui-selection")), \
                mock.patch.object(opencode, "_proc_start_ticks", return_value="123"), \
                mock.patch.object(opencode, "_db", return_value=str(db)), \
                mock.patch.object(opencode.session_registry, "read", return_value=None), \
                mock.patch("fleet.context_policy.read_policy", return_value=None), \
                self._title_patches()[0], self._title_patches()[1], self._title_patches()[2]:
            opencode.enrich(session)

        reader.assert_called_once()
        self.assertEqual(reader.call_args.args[0], "ses_b")
        self.assertEqual(session.session_id, "ses_b")
        self.assertEqual((session.title, session.summary), ("Title B", "Summary B"))
        self.assertEqual(session.model, "model-b")
        self.assertEqual(session.active_context_tokens, 123)
        self.assertEqual(session._refresh_source["session_id"], "ses_b")
        self.assertEqual(session._session_cwd, ("ses_b", "/selected/project"))
        self.assertEqual(session.cwd, "/launch/project")
        session_cwd.project([session])
        self.assertEqual(session.cwd, "/selected/project")
        self.assertEqual(db.read_bytes(), before)

    def test_repeated_exact_sid_enrichment_clears_missing_selected_directory(self):
        from fleet import session_cwd

        db = self.root / "opencode-repeated-cwd.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE credential (id TEXT, integration_id TEXT, value TEXT)")
            con.execute(
                "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, "
                "slug TEXT, title TEXT, agent TEXT, model TEXT, time_created INT, "
                "time_updated INT, cost REAL, tokens_input INT, tokens_output INT, "
                "tokens_reasoning INT)")
            con.execute(
                "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
                "seq INT, time_created INT, time_updated INT, data TEXT)")
            con.execute("INSERT INTO credential VALUES ('id','provider','fixture-only')")
            con.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "ses_b", "/B", None, "slug-b", "native-b", "build",
                json.dumps({"id": "model-b", "providerID": "provider"}),
                1000, 2000, 0.2, 7, 3, 1))
            con.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)", (
                "msg-b", "ses_b", "assistant", 1, 1500, 2000,
                json.dumps({"content": [{"type": "text", "text": "selected B"}],
                            "tokens": {"total": 123},
                            "time": {"created": 1500, "completed": 2000}})))

        session = self._session(prior="ses_a")
        session.cwd = "/launch/project"
        shell_key = opencode._v2_shell_key(str(db), "ses_b", None)
        self.addCleanup(opencode._V2_SHELL_TASKS.pop, shell_key, None)
        with mock.patch.object(opencode, "session_of_process",
                               return_value=("ses_b", "opencode-tui-selection")), \
                mock.patch.object(opencode, "_proc_start_ticks", return_value="123"), \
                mock.patch.object(opencode, "_db", return_value=str(db)), \
                mock.patch.object(opencode.session_registry, "read", return_value=None), \
                mock.patch("fleet.context_policy.read_policy", return_value=None), \
                self._title_patches()[0], self._title_patches()[1], self._title_patches()[2]:
            opencode.enrich(session)
            self.assertEqual(session._session_cwd, ("ses_b", "/B"))
            with sqlite3.connect(db) as con:
                con.execute("UPDATE session_v2 SET directory=NULL WHERE id='ses_b'")
            before_second_read = db.read_bytes()
            opencode.enrich(session)

        self.assertEqual(session.session_id, "ses_b")
        self.assertEqual((session.title, session.active_context_tokens), ("Title B", 123))
        self.assertEqual(session._refresh_source["session_id"], "ses_b")
        self.assertEqual(session._session_cwd, (None, None))
        self.assertEqual(session.cwd, "/launch/project")
        session_cwd.project([session])
        self.assertEqual(session.cwd, "/launch/project")
        self.assertEqual(db.read_bytes(), before_second_read)

    def test_credential_mixed_database_exact_v1_session_uses_v1_reader(self):
        db = self.root / "opencode-credential-v1.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE credential (id TEXT, integration_id TEXT, value TEXT)")
            con.execute("INSERT INTO credential VALUES ('id','provider','fixture-only')")
            con.execute(
                "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, "
                "slug TEXT, title TEXT, agent TEXT, model TEXT, time_created INT, "
                "time_updated INT, cost REAL, tokens_input INT, tokens_output INT, "
                "tokens_reasoning INT)")
            con.execute(
                "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
                "seq INT, time_created INT, time_updated INT, data TEXT)")
            con.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "ses_a", "/A", None, "slug-a", "native-a", "build",
                json.dumps({"id": "model-a", "providerID": "provider"}),
                1000, 9000, 0.9, 90, 0, 0))
            con.execute(
                "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, "
                "slug TEXT, agent TEXT, model TEXT, time_created INTEGER, time_updated INTEGER, "
                "summary TEXT, title TEXT, cost REAL, tokens_input INTEGER, "
                "tokens_output INTEGER, tokens_reasoning INTEGER)")
            con.execute(
                "CREATE TABLE message (id TEXT, session_id TEXT, role TEXT, "
                "time_updated INTEGER, data TEXT)")
            con.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, data TEXT)")
            con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "ses_v1", "/V1", None, "slug-v1", "build",
                json.dumps({"id": "model-v1", "providerID": "provider"}),
                1000, 2000, "native summary v1", "native v1", 0.2, 17, 3, 0))
            con.execute("INSERT INTO message VALUES (?,?,?,?,?)", (
                "msg-v1", "ses_v1", "assistant", 2000,
                json.dumps({"tokens": {"total": 23}, "finish": "stop"})))
            con.execute("INSERT INTO part VALUES (?,?,?,?)", (
                "part-v1", "msg-v1", "ses_v1",
                json.dumps({"type": "text", "text": "exact V1 content"})))

        before = db.read_bytes()
        runtime = opencode._opencode_v2_runtime()
        observation = runtime.read_session_observation("ses_v1", db_path=db)
        self.assertEqual((observation.get("state"), observation.get("generation")),
                         ("observed", "v1"))
        session = self._session(prior="ses_a")
        with mock.patch.object(opencode, "session_of_process",
                               return_value=("ses_v1", "opencode-tui-selection")), \
                mock.patch.object(opencode, "_proc_start_ticks", return_value="123"), \
                mock.patch.object(opencode, "_db", return_value=str(db)), \
                mock.patch.object(opencode.session_registry, "read", return_value=None), \
                mock.patch("fleet.context_policy.read_policy", return_value=None), \
                mock.patch.object(opencode.titles, "last_title", return_value="Title V1"), \
                mock.patch.object(opencode.titles, "fresh_summary_with_ts",
                                  return_value=("Summary V1", 2.0)), \
                mock.patch.object(runtime, "read_session_observation",
                                  wraps=runtime.read_session_observation) as reader:
            opencode.enrich(session)

        reader.assert_called_once()
        self.assertEqual(session.session_id, "ses_v1")
        self.assertEqual((session.title, session.summary), ("Title V1", "Summary V1"))
        self.assertEqual(session.active_context_tokens, 23)
        self.assertEqual(session._refresh_source["kind"], "opencode-db")
        self.assertEqual(session._refresh_source["session_id"], "ses_v1")
        self.assertEqual(session._refresh_source["table"], "part")
        self.assertEqual(session.shell_jobs, [])
        self.assertEqual(session.shell_job_observation["reason"],
                         "v1-background-native-record-not-observed")
        self.assertEqual(db.read_bytes(), before)

    def test_registry_requires_live_pid_start_and_current_session_before_apply(self):
        self._add("ses_a", updated=999999, context=99)
        self._add("ses_b", updated=1000, context=22)
        stale_records = (
            {"pid": 4243, "procStart": "123", "sessionId": "ses_b",
             "name": "wrong-pid", "status": "busy", "cwd": "/wrong-pid",
             "kind": "wrong-pid", "startedAt": 1000000, "updatedAt": 1000500},
            {"pid": 4242, "procStart": "122", "sessionId": "ses_b",
             "name": "old-start", "status": "busy", "cwd": "/old-start",
             "kind": "old-start", "startedAt": 1000000, "updatedAt": 1000500},
            {"pid": 4242, "procStart": "123", "sessionId": "ses_a",
             "name": "wrong-session", "status": "busy", "cwd": "/wrong-session",
             "kind": "wrong-session", "startedAt": 1000000, "updatedAt": 1000500},
        )
        for record in stale_records:
            with self.subTest(record=record):
                sess = self._session(prior="ses_a")
                sess.process_role = "conversation"
                sess.elapsed_min = 17
                with mock.patch.object(opencode.session_registry, "apply_to_session",
                                       wraps=opencode.session_registry.apply_to_session) as apply:
                    self._run_v1(sess, ("ses_b", "opencode-tui-selection"), record)
                apply.assert_not_called()
                self.assertEqual(sess.session_id, "ses_b")
                self.assertEqual(sess.cwd, "/repo")
                self.assertEqual((sess.process_role, sess.elapsed_min), ("conversation", 17))
                for field in ("status", "kind", "started_at", "updated_at",
                              "registry_proc_start", "registry_name", "runtime_name"):
                    self.assertIsNone(getattr(sess, field), field)
                self.assertNotIn(record["name"], (sess.slug, sess.title, sess.runtime_name))

    def test_matching_registry_record_applies_only_to_its_current_sid(self):
        self._add("ses_b", updated=1000, context=22)
        record = {"pid": 4242, "procStart": "123", "sessionId": "ses_b",
                  "name": "current-name", "nameSource": "user", "status": "busy",
                  "cwd": "/registry-cwd", "kind": "interactive",
                  "startedAt": 1000000, "updatedAt": 1000500}
        sess, _v2 = self._run_v1(
            self._session(prior="ses_a"), ("ses_b", "opencode-tui-selection"), record)
        self.assertEqual(sess.session_id, "ses_b")
        self.assertEqual((sess.registry_name, sess.runtime_name),
                         ("current-name", "current-name"))
        self.assertEqual((sess.status, sess.kind), ("busy", "interactive"))
        self.assertEqual((sess.registry_proc_start, sess.started_at, sess.updated_at),
                         ("123", 1000.0, 1000.5))
        self.assertEqual(sess.cwd, "/repo")

    def test_registry_fields_clear_when_same_session_transitions_to_unknown(self):
        self._add("ses_a", updated=999999, context=99)
        self._add("ses_b", updated=1000, context=22)
        record = {"pid": 4242, "procStart": "123", "sessionId": "ses_b",
                  "name": "prior-name", "nameSource": "user", "status": "busy",
                  "cwd": "/registry-cwd", "kind": "interactive",
                  "startedAt": 1000000, "updatedAt": 1000500}
        sess = self._session()
        sess.process_role = "conversation"
        self._run_v1(sess, ("ses_b", "opencode-tui-selection"), record)
        self.assertEqual(sess.registry_name, "prior-name")

        self._run_v1(sess, ("ses_a", "opencode-tui-selection"), record)
        self.assertEqual(sess.session_id, "ses_a")
        self.assertIsNone(sess.registry_name)
        self.assertIsNone(sess.status)
        self.assertIsNone(sess.kind)

        self._run_v1(sess, (None, ""), record)
        self.assertIsNone(sess.session_id)
        self.assertEqual(sess.session_identity_evidence["verdict"], "unobserved")
        self.assertEqual(sess.cwd, "/repo")
        self.assertEqual(sess.process_role, "conversation")
        for field in ("status", "kind", "started_at", "updated_at",
                      "registry_proc_start", "registry_name", "runtime_name", "slug"):
            self.assertIsNone(getattr(sess, field), field)

    def test_v2_does_not_enumerate_a_unique_candidate_without_current_selection(self):
        self._add("ses_a", updated=999999, context=41)
        sess = self._session(prior="ses_a")
        runtime = mock.Mock()
        runtime.list_session_candidates.return_value = [{"id": "ses_a", "generation": "v2"}]
        with mock.patch.object(opencode, "session_of_process",
                               return_value=("ses_a", "opencode-argv")), \
                mock.patch.object(opencode, "_proc_start_ticks", return_value="123"), \
                mock.patch.object(opencode, "_db") as db_path, \
                mock.patch.object(opencode, "_opencode_v2_runtime", return_value=runtime):
            opencode.enrich(sess)
        db_path.assert_not_called()
        runtime.credential_store_present.assert_not_called()
        runtime.list_session_candidates.assert_not_called()
        self.assertIsNone(sess.session_id)
        self.assertEqual(sess.session_identity_evidence["verdict"], "unobserved")
        self.assertIsNone(sess.title)
        self.assertIsNone(sess.summary)
        self.assertIsNone(sess.active_context_tokens)
        self.assertIsNone(sess._refresh_source)

    def test_v2_missing_exact_selection_keeps_identity_and_does_not_query_neighbors(self):
        sess, runtime = self._run_v2(
            self._session(), ("ses_b", "opencode-tui-selection"),
            {"state": "unobserved", "reason": "selected-session-row-missing"})
        runtime.read_session_observation.assert_called_once()
        runtime.list_session_candidates.assert_not_called()
        self.assertEqual(sess.session_id, "ses_b")
        self.assertIsNone(sess.title)
        self.assertIsNone(sess.summary)
        self.assertIsNone(sess.active_context_tokens)
        self.assertIsNone(sess._refresh_source)
        self.assertIsNone(sess.mtime)


class DuplicateSidCollapseTest(unittest.TestCase):
    """Duplicate exact bindings collapse while process rows and attempt data remain."""

    def _row(self, pid, sid, kind=None, started=None, harness="opencode",
             cwd="/repo", title="T", tag="[83]"):
        from types import SimpleNamespace
        return SimpleNamespace(
            pid=pid, harness=harness, cwd=cwd, session_id=sid,
            _opencode_bind_kind=kind,
            proc_start=str(started) if started is not None else None,
            session_tag=tag, slug="db-slug", title=title, summary="S",
            summary_ts=1.0, subagents=[], model="m", effort="high",
            effort_default=False, cost=1.0, tokens=10,
            session_input_tokens=1, session_output_tokens=2,
            session_reasoning_output_tokens=3, session_total_tokens=6,
            active_context_tokens=5, context_window_tokens=100, ctx_pct=5,
            _context_evidence="e", _refresh_source={"session_id": sid},
            mtime=999.0)

    def test_exact_selection_keeps_sid_and_helper_is_unbound(self):
        leader = self._row(100, "ses_1", kind="process", started=1000)
        helper = self._row(200, "ses_1", kind=None, started=2000)
        opencode.collapse_duplicate_sids([leader, helper])
        self.assertEqual(leader.session_id, "ses_1")
        self.assertEqual(leader.title, "T")
        self.assertIsNone(helper.session_id)
        self.assertIsNone(helper.session_tag)
        self.assertIsNone(helper.title)
        self.assertIsNone(helper.summary)
        self.assertIsNone(helper.model)
        self.assertIsNone(helper.ctx_pct)
        self.assertIsNone(helper._refresh_source)
        # the process row itself stays, with process-owned fields intact
        self.assertEqual(helper.pid, 200)
        self.assertEqual(helper.cwd, "/repo")
        self.assertEqual(helper.slug, "repo")
        self.assertEqual(helper.mtime, 999.0)

    def test_exact_selection_beats_unproven_binding_despite_pid_order(self):
        unproven = self._row(50, "ses_1", kind=None, started=1000)
        owner = self._row(200, "ses_1", kind="process", started=2000)
        opencode.collapse_duplicate_sids([unproven, owner])
        self.assertEqual(owner.session_id, "ses_1")
        self.assertIsNone(unproven.session_id)

    def test_duplicate_bindings_keep_the_earliest_start_if_neither_proves_selection(self):
        old = self._row(300, "ses_1", kind=None, started=1000)
        new = self._row(100, "ses_1", kind=None, started=2000)
        opencode.collapse_duplicate_sids([old, new])
        self.assertEqual(old.session_id, "ses_1")
        self.assertIsNone(new.session_id)

    def test_distinct_sids_and_other_harnesses_untouched(self):
        a = self._row(100, "ses_1", kind="process", started=1000)
        b = self._row(200, "ses_2", kind=None, started=2000)
        c = self._row(300, "ses_1", kind=None, started=3000,
                      harness="codex")
        d = self._row(400, None, kind=None, started=4000)
        opencode.collapse_duplicate_sids([a, b, c, d])
        self.assertEqual((a.session_id, b.session_id, c.session_id, d.session_id),
                         ("ses_1", "ses_2", "ses_1", None))
        self.assertEqual(c.title, "T")

    def test_anonymous_duplicate_row_keeps_exact_attempt_sidecar(self):
        selected = self._row(100, "ses_1", kind="process", started=1000)
        anonymous = self._row(200, "ses_1", kind=None, started=2000)
        anonymous.attempt_id = "att-exact"
        with mock.patch.object(opencode.titles, "attempt_sid", return_value="attempt-exact"), \
                mock.patch.object(opencode.titles, "last_title", return_value="Attempt title"), \
                mock.patch.object(opencode.titles, "fresh_summary_with_ts",
                                  return_value=("Attempt summary", 12.0)):
            opencode.collapse_duplicate_sids([selected, anonymous])
        self.assertIsNone(anonymous.session_id)
        self.assertEqual((anonymous.title, anonymous.summary),
                         ("Attempt title", "Attempt summary"))


if __name__ == "__main__":
    unittest.main()
