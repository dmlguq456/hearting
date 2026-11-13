#!/usr/bin/env python3
"""One reading of the harness identity variables, and how sure it is."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import session_identity as S  # noqa: E402


class IdentityTest(unittest.TestCase):
    def test_each_confidence(self):
        cases = (
            ({}, ("", "", "", "none")),
            ({"CLAUDE_CODE_SESSION_ID": "c1"}, ("claude", "c1", "CLAUDE_CODE_SESSION_ID", "sole")),
            ({"CLAUDE_SESSION_ID": "c0"}, ("claude", "c0", "CLAUDE_SESSION_ID", "sole")),
            ({"CODEX_SESSION_ID": "x0", "CODEX_THREAD_ID": "x1"}, ("codex", "x1", "CODEX_THREAD_ID", "sole")),
            ({"OPENCODE_SESSION_ID": "ses_1"}, ("opencode", "ses_1", "OPENCODE_SESSION_ID", "sole")),
            ({"CLAUDE_CODE_SESSION_ID": "c1", "CODEX_THREAD_ID": "x1"},
             ("", "", "CLAUDE_CODE_SESSION_ID,CODEX_THREAD_ID", "ambiguous")),
            ({"CLAUDE_CODE_SESSION_ID": "c1", "CODEX_THREAD_ID": "x1", "AGENT_DISPATCH_CALLER_HARNESS": "codex"},
             ("codex", "x1", "CODEX_THREAD_ID", "named")),
            ({"AGENT_DISPATCH_CURRENT_HARNESS": "opencode"},
             ("opencode", "", "AGENT_DISPATCH_CURRENT_HARNESS", "named")),
            ({"AGENT_DISPATCH_CALLER_HARNESS": "gemini", "CLAUDE_CODE_SESSION_ID": "c1"},
             ("", "", "AGENT_DISPATCH_CALLER_HARNESS", "invalid")),
        )
        for env, expected in cases:
            with self.subTest(env=env):
                found = S.identity(env)
                self.assertEqual((found.harness, found.session_id, found.source, found.confidence), expected)
                self.assertEqual(found.known, expected[3] in ("named", "sole"))

    def test_a_label_never_refuses(self):
        self.assertEqual(S.session_label({}), "operator")
        self.assertEqual(S.session_label({"CLAUDE_CODE_SESSION_ID": "c1"}), "c1")
        self.assertEqual(S.session_label({"CODEX_THREAD_ID": "x1", "CLAUDE_SESSION_ID": "c0"}), "c0")
        self.assertEqual(S.session_label({"CODEX_THREAD_ID": "x1", "CLAUDE_SESSION_ID": "c0",
                                          "AGENT_DISPATCH_CALLER_HARNESS": "codex"}), "x1")


class SubSessionRootTest(unittest.TestCase):
    """A native sub-session answers with its root session, as a Claude subagent does (PG-49)."""

    def test_codex_thread_walks_the_spawn_edges_to_its_root(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            db = sqlite3.connect(str(Path(home) / "state_5.sqlite"))
            db.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)")
            db.executemany("INSERT INTO thread_spawn_edges VALUES (?, ?, 'open')",
                           (("root-1", "child-1"), ("child-1", "grandchild-1"),
                            ("p-a", "twice"), ("p-b", "twice")))
            db.commit()
            db.close()
            S._ROOTS.clear()
            for sid, root in (("grandchild-1", "root-1"), ("child-1", "root-1"), ("root-1", "root-1"),
                              ("twice", "twice"), ("unknown", "unknown")):
                with self.subTest(sid=sid):
                    found = S.identity({"CODEX_THREAD_ID": sid, "CODEX_HOME": home})
                    self.assertEqual((found.harness, found.session_id, found.confidence),
                                     ("codex", root, "sole"))
                    self.assertEqual(found.native_session_id, sid if root != sid else "")
            self.assertEqual(S.session_label({"CODEX_THREAD_ID": "child-1", "CODEX_HOME": home}), "root-1")
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(S.identity({"CODEX_THREAD_ID": "x1", "CODEX_HOME": empty}).session_id, "x1")
            (Path(empty) / "state_5.sqlite").write_text("not a database")
            self.assertEqual(S.identity({"CODEX_THREAD_ID": "x1", "CODEX_HOME": empty}).session_id, "x1")

    def test_opencode_child_reads_the_plugin_root(self):
        found = S.identity({"OPENCODE_SESSION_ID": "ses_child", "OPENCODE_ROOT_SESSION_ID": "ses_root"})
        self.assertEqual((found.session_id, found.native_session_id), ("ses_root", "ses_child"))
        self.assertEqual(S.identity({"OPENCODE_SESSION_ID": "ses_1", "OPENCODE_ROOT_SESSION_ID": ""}).session_id,
                         "ses_1")
        # Another harness never reads OpenCode's root variable.
        self.assertEqual(S.identity({"CLAUDE_CODE_SESSION_ID": "c1",
                                     "OPENCODE_ROOT_SESSION_ID": "ses_root"}).session_id, "c1")


class CallersAgreeTest(unittest.TestCase):
    """The callers that used to keep their own copy now give the module's answer."""

    ENVS = ({}, {"CLAUDE_CODE_SESSION_ID": "c1"}, {"CODEX_THREAD_ID": "x1"}, {"OPENCODE_SESSION_ID": "ses_1"},
            {"CLAUDE_CODE_SESSION_ID": "c1", "CODEX_THREAD_ID": "x1"},
            {"CLAUDE_CODE_SESSION_ID": "c1", "CODEX_THREAD_ID": "x1", "AGENT_DISPATCH_CURRENT_HARNESS": "claude"})

    def test_route_authority_route_chain_and_tidy(self):
        import route_authority
        import session_tidy
        sys.path.insert(0, str(HERE.parent / "tools"))
        from fleet import route_chain
        for env in self.ENVS:
            found = S.identity(env)
            with self.subTest(env=env):
                if found.known:
                    self.assertEqual(route_authority.caller_identity(env), (found.harness, found.session_id))
                    expected_chain = (found.harness, found.session_id) if found.session_id else None
                    self.assertEqual(route_chain.writer_identity(env) if found.session_id else None, expected_chain)
                else:
                    if found.confidence == "ambiguous":
                        with self.assertRaises(Exception):
                            route_authority.caller_identity(env)
                    self.assertIsNone(route_chain.writer_identity(env))
                self.assertEqual(route_authority.correction_source_session(env), S.session_label(env))
                tidy = session_tidy.session_from_env(env=env)
                if found.known and found.session_id:
                    self.assertEqual(tidy, (found.harness, found.session_id))


if __name__ == "__main__":
    unittest.main()
