#!/usr/bin/env python3
"""Hermetic test — managed (client-server) Codex must attribute its rollout to the TUI row.

`codex-managed-entry.py` splits one session across two `codex` processes sharing a
`~/.codex/.harness/managed-sessions/<name>/` state dir:
  · `codex app-server --listen unix://<dir>/app-server.sock` — holds the rollout fd, and
    procscan marks it an app_server companion (hidden from the render).
  · `codex --remote unix://<dir>/managed-tui.sock` — the row the user actually sees, with
    NO transcript fd at all.
Before this transfer the visible row lost session_id/title/context entirely (사용자
2026-07-29 "codex가 아예 안 잡혀"). The state dir is the deterministic join key; a dir
without exactly one owning app-server and one TUI client stays unattributed (F-26), and a
donated rollout is never claimed twice (F-24).
/proc fd reads are monkeypatched; the rollout file is real so `_rollout_meta`/`_sid` run.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet.collectors import codex, herdr, procscan  # noqa: E402
from fleet.model import Session  # noqa: E402
from fleet.session_tags import assign_observed  # noqa: E402

_SID = "019facc9-fe83-7480-89f6-9e64fbebf0ca"
_REAL_LISTDIR = os.listdir
_REAL_READLINK = os.readlink


def _make_rollout(home, cwd, sid=_SID):
    d = os.path.join(home, "sessions", "2026", "07", "29")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "rollout-2026-07-29T09-15-02-%s.jsonl" % sid)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta",
                            "payload": {"cwd": cwd, "id": sid}}) + "\n")
    return path


def _session(pid, cwd, managed_dir, app_server=False):
    return Session(harness="codex", pid=pid, cwd=cwd, app_server=app_server,
                   managed_dir=managed_dir, elapsed_min=5,
                   slug=os.path.basename(cwd.rstrip("/")))


class _FdStub:
    """Serve /proc/<pid>/fd from a pid -> [paths] map; every other path hits the real os."""

    def __init__(self, fds):
        self.fds = fds

    def _pid(self, path):
        parts = path.split("/")
        if len(parts) >= 4 and parts[1] == "proc" and parts[3] == "fd":
            try:
                return int(parts[2]), (parts[4] if len(parts) > 4 else None)
            except ValueError:
                return None, None
        return None, None

    def listdir(self, path):
        pid, _ = self._pid(path)
        if pid is None:
            return _REAL_LISTDIR(path)
        return [str(i) for i in range(len(self.fds.get(pid, [])))]

    def readlink(self, path):
        pid, fd = self._pid(path)
        if pid is None or fd is None:
            return _REAL_READLINK(path)
        return self.fds[pid][int(fd)]


class ManagedCodexAttributionTest(unittest.TestCase):
    def _prepare(self, sessions, fds, home):
        stub = _FdStub(fds)
        with mock.patch.dict(os.environ, {"CODEX_HOME": home}), \
             mock.patch.object(codex.os, "listdir", side_effect=stub.listdir), \
             mock.patch.object(codex.os, "readlink", side_effect=stub.readlink):
            return codex.prepare_tick(sessions)

    def test_tui_row_inherits_the_app_server_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cwd = os.path.join(tmp, "dot-codex"), os.path.join(tmp, "agent_setting")
            os.makedirs(cwd, exist_ok=True)
            rollout = _make_rollout(home, cwd)
            managed = os.path.join(home, ".harness", "managed-sessions", "session-r5rh60f9")
            server = _session(100, cwd, managed, app_server=True)
            tui = _session(200, cwd, managed)
            tick = self._prepare([server, tui], {100: [rollout], 200: []}, home)

            self.assertEqual(tick.proc_paths, {200: rollout})

            with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
                for sess in (server, tui):
                    codex.enrich(sess, tick=tick)
            self.assertEqual(tui.session_id, _SID)
            self.assertIsNotNone(tui.mtime)
            # One directly owned rollout stays on the visible managed TUI row.
            self.assertIsNone(server.session_id)
            sids = [s.session_id for s in (server, tui) if s.session_id]
            self.assertEqual(len(sids), len(set(sids)))

    def test_different_managed_dirs_do_not_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cwd = os.path.join(tmp, "dot-codex"), os.path.join(tmp, "agent_setting")
            os.makedirs(cwd, exist_ok=True)
            rollout = _make_rollout(home, cwd)
            base = os.path.join(home, ".harness", "managed-sessions")
            server = _session(100, cwd, os.path.join(base, "session-aaa"), app_server=True)
            tui = _session(200, cwd, os.path.join(base, "session-bbb"))
            tick = self._prepare([server, tui], {100: [rollout], 200: []}, home)
            self.assertEqual(tick.proc_paths, {100: rollout})

    def test_two_clients_in_one_managed_dir_stay_unattributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cwd = os.path.join(tmp, "dot-codex"), os.path.join(tmp, "agent_setting")
            os.makedirs(cwd, exist_ok=True)
            rollout = _make_rollout(home, cwd)
            managed = os.path.join(home, ".harness", "managed-sessions", "session-r5rh60f9")
            server = _session(100, cwd, managed, app_server=True)
            tick = self._prepare(
                [server, _session(200, cwd, managed), _session(201, cwd, managed)],
                {100: [rollout], 200: [], 201: []}, home)
            self.assertEqual(tick.proc_paths, {100: rollout})

    def test_plain_codex_row_has_no_managed_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cwd = os.path.join(tmp, "dot-codex"), os.path.join(tmp, "agent_setting")
            os.makedirs(cwd, exist_ok=True)
            rollout = _make_rollout(home, cwd)
            tick = self._prepare([_session(100, cwd, None)], {100: [rollout]}, home)
            self.assertEqual(tick.proc_paths, {100: rollout})

    def test_shared_daemon_with_two_same_cwd_rollouts_has_no_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cwd = os.path.join(tmp, "dot-codex"), os.path.join(tmp, "project")
            os.makedirs(cwd)
            first = _make_rollout(home, cwd)
            second = _make_rollout(home, cwd, sid="019facc9-fe83-7480-89f6-9e64fbebf0cb")
            stub = _FdStub({100: [first, second]})
            with mock.patch.object(codex.os, "listdir", side_effect=stub.listdir), \
                 mock.patch.object(codex.os, "readlink", side_effect=stub.readlink):
                self.assertIsNone(codex._proc_rollout(100, cwd, home))

    def test_unresolved_tui_uses_exact_herdr_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cwd = os.path.join(tmp, "dot-codex"), os.path.join(tmp, "project")
            os.makedirs(cwd)
            rollout = _make_rollout(home, cwd)
            tui = _session(200, cwd, None)
            with mock.patch.object(herdr, "codex_pane_sessions", return_value={200: _SID}), \
                 mock.patch.object(procscan, "read_environ", return_value={}):
                tick = self._prepare([tui], {200: []}, home)
            self.assertEqual(tick.proc_paths, {200: rollout})
            with mock.patch.dict(os.environ, {"CODEX_HOME": home, "XDG_STATE_HOME": tmp}):
                codex.enrich(tui, tick=tick)
                self.assertEqual(tui.session_id, _SID)
                self.assertIsNone(tui.session_tag)
                confirmed = assign_observed("codex", _SID)
                self.assertIsNotNone(confirmed)
                codex.enrich(tui, tick=tick)
                self.assertEqual(tui.session_tag, confirmed)

    def test_current_pane_thread_replaces_an_already_selected_old_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cwd = os.path.join(tmp, "dot-codex"), os.path.join(tmp, "project")
            os.makedirs(cwd)
            old_sid = "019facc9-fe83-7480-89f6-9e64fbebf0cb"
            new_sid = "019facc9-fe83-7480-89f6-9e64fbebf0cc"
            old = _make_rollout(home, cwd, old_sid)
            new = _make_rollout(home, cwd, new_sid)
            for path, active, window in ((old, 80, 100), (new, 30, 200)):
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"payload": {"type": "token_count", "info": {
                        "last_token_usage": {"total_tokens": active},
                        "model_context_window": window}}}) + "\n")
            tui = _session(200, cwd, None)
            with mock.patch.object(herdr, "codex_pane_sessions", return_value={200: new_sid}), \
                    mock.patch.object(procscan, "read_environ", return_value={}):
                tick = self._prepare([tui], {200: [old]}, home)
            self.assertEqual(tick.proc_paths[200], new)
            self.assertEqual(tui._herdr_session_id, new_sid)
            with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
                codex.enrich(tui, tick=tick)
            self.assertEqual((tui.session_id, tui.active_context_tokens,
                              tui.context_window_tokens), (new_sid, 30, 200))

    def test_current_pane_thread_without_rollout_drops_old_session_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cwd = os.path.join(tmp, "dot-codex"), os.path.join(tmp, "project")
            os.makedirs(cwd)
            old_sid = "019facc9-fe83-7480-89f6-9e64fbebf0cb"
            new_sid = "019facc9-fe83-7480-89f6-9e64fbebf0cc"
            old = _make_rollout(home, cwd, old_sid)
            with open(old, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"payload": {"type": "token_count", "info": {
                    "last_token_usage": {"total_tokens": 80},
                    "model_context_window": 100}}}) + "\n")
            tui = _session(200, cwd, None)
            with mock.patch.object(herdr, "codex_pane_sessions", return_value={200: new_sid}), \
                    mock.patch.object(procscan, "read_environ", return_value={}):
                tick = self._prepare([tui], {200: [old]}, home)
            self.assertNotIn(200, tick.proc_paths)
            with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
                codex.enrich(tui, tick=tick)
            self.assertEqual(tui.session_id, new_sid)
            self.assertIsNone(tui.active_context_tokens)
            self.assertIsNone(tui.context_window_tokens)
            self.assertIsNone(tui.session_input_tokens)

    def test_herdr_thread_without_rollout_never_claims_other_cwd_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cwd = os.path.join(tmp, "dot-codex"), os.path.join(tmp, "project")
            os.makedirs(cwd)
            _make_rollout(home, cwd)
            tui = _session(200, cwd, None)
            other_sid = "019facc9-fe83-7480-89f6-9e64fbebf0cb"
            with mock.patch.object(herdr, "codex_pane_sessions", return_value={200: other_sid}), \
                 mock.patch.object(procscan, "read_environ", return_value={}):
                tick = self._prepare([tui], {200: []}, home)
            self.assertIsNone(tick.proc_paths.get(200))
            with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
                codex.enrich(tui, tick=tick)
            self.assertEqual(tui.session_id, other_sid)

    def test_duplicate_pane_thread_is_not_stamped_on_two_tuis(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cwd = os.path.join(tmp, "dot-codex"), os.path.join(tmp, "project")
            os.makedirs(cwd)
            rollout = _make_rollout(home, cwd)
            first, second = _session(200, cwd, None), _session(201, cwd, None)
            with mock.patch.object(herdr, "codex_pane_sessions",
                                   return_value={200: _SID, 201: _SID}), \
                 mock.patch.object(procscan, "read_environ", return_value={}):
                tick = self._prepare([first, second], {200: [], 201: []}, home)
            self.assertEqual(tick.proc_paths, {200: rollout})
            with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
                codex.enrich(first, tick=tick)
                codex.enrich(second, tick=tick)
            self.assertEqual(first.session_id, _SID)
            self.assertIsNone(second.session_id)


class ManagedDirParseTest(unittest.TestCase):
    def test_listen_and_remote_argv_yield_the_same_dir(self):
        managed = "/home/u/.codex/.harness/managed-sessions/session-r5rh60f9"
        self.assertEqual(
            procscan._managed_dir("codex app-server --listen unix://%s/app-server.sock" % managed),
            managed)
        self.assertEqual(
            procscan._managed_dir("codex --remote unix://%s/managed-tui.sock --yolo" % managed),
            managed)

    def test_unrelated_argv_yields_none(self):
        self.assertIsNone(procscan._managed_dir("codex --yolo"))
        self.assertIsNone(procscan._managed_dir("codex exec --cd /repo 'do a thing'"))
        self.assertIsNone(procscan._managed_dir(""))
        self.assertIsNone(procscan._managed_dir(None))

    def test_shared_service_argv_is_distinct_from_per_session_server(self):
        self.assertTrue(procscan._shared_codex_daemon(
            "codex app-server --listen unix:// --managed-daemon"))
        self.assertTrue(procscan._shared_codex_daemon(
            "codex app-server daemon pid-update-loop"))
        self.assertFalse(procscan._shared_codex_daemon(
            "codex app-server --listen unix:///tmp/managed-sessions/x/app-server.sock"))
        self.assertFalse(procscan._shared_codex_daemon(
            "codex exec app-server --managed-daemon"))

    def test_scan_keeps_tui_and_legacy_server_but_excludes_shared_service(self):
        lines = [
            "137 codex 01:00 codex app-server daemon pid-update-loop",
            "149 codex 01:00 codex app-server --listen unix:// --managed-daemon",
            "243 codex 01:00 codex --dangerously-bypass-approvals-and-sandbox",
            "100 codex 01:00 codex app-server --listen unix:///tmp/managed-sessions/x/app-server.sock",
        ]
        with mock.patch.object(procscan, "_ps_lines", return_value=lines), \
             mock.patch.object(procscan, "_pid_ttys", return_value={137: "?", 149: "?", 243: "pts/1", 100: "?"}), \
             mock.patch.object(procscan, "_detached_ttys", return_value=set()), \
             mock.patch.object(procscan, "proc_tree", return_value={}), \
             mock.patch.object(procscan, "children_index", return_value={}), \
             mock.patch.object(procscan, "_read_argv", side_effect=lambda pid: next(
                 line.split(maxsplit=3)[3].split() for line in lines if line.startswith(str(pid) + " "))), \
             mock.patch("fleet.session_registry.read", return_value=None), \
             mock.patch.object(procscan, "_read_cwd", return_value=("/project", False)), \
             mock.patch.object(procscan, "read_environ", return_value={}), \
             mock.patch.object(procscan, "read_proc_start", return_value="123"), \
             mock.patch.object(procscan, "exec_child", return_value=None) as child, \
             mock.patch.object(procscan, "_orca_dead_socks", return_value=set()):
            rows = procscan.scan(harness_filter={"codex"})
            from fleet.process_identity import finalize_process_roles
            finalize_process_roles(rows, [])
        self.assertEqual({s.pid for s in rows}, {243, 100})
        self.assertEqual({s.pid for s in rows if s.app_server}, {100})
        self.assertEqual({call.args[0] for call in child.call_args_list}, {137, 149, 243, 100})


if __name__ == "__main__":
    unittest.main()
