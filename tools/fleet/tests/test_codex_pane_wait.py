"""Full refresh waits for cold Codex pane identity; fast refresh reuses it."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "tools/fleet").is_dir())
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))

import importlib
collectors = importlib.import_module("fleet.collectors")
from fleet.collectors import codex, herdr, opencode, procscan  # noqa: E402


class StopAtPrepare(BaseException):
    pass


class CodexPaneWaitTest(unittest.TestCase):
    def _collect_until_prepare(self, harness, fast_first):
        calls = []
        session = SimpleNamespace(harness=harness, pid=123, cwd="/work", session_id=None)

        def stop(*args, **kwargs):
            calls.append((args, kwargs))
            raise StopAtPrepare()

        modules = {
            ".claude": SimpleNamespace(enrich=lambda *_args, **_kwargs: None),
            ".codex": SimpleNamespace(prepare_tick=stop, enrich=lambda *_args, **_kwargs: None),
            ".opencode": SimpleNamespace(enrich=stop),
        }
        def load(name, _package):
            return modules[name]

        with mock.patch.object(collectors.procscan, "scan", return_value=[session]), \
             mock.patch.object(collectors.importlib, "import_module", side_effect=load):
            try:
                collectors.collect_all(harness_filter=(harness,), fast_first=fast_first)
            except StopAtPrepare:
                pass
        return calls

    def test_collect_all_waits_only_for_codex_prepare_and_enriches_opencode_directly(self):
        self.assertEqual(self._collect_until_prepare("codex", False)[0][1], {"wait_timeout": 0.2})
        self.assertEqual(self._collect_until_prepare("codex", True)[0][1], {"wait_timeout": 0.0})
        args, kwargs = self._collect_until_prepare("opencode", False)[0]
        self.assertEqual(len(args), 1)
        self.assertEqual(kwargs, {})
        self.assertFalse(hasattr(opencode, "prepare_tick"))

    def test_cold_full_prepare_wait_can_attach_current_pane_sid(self):
        sid = "01a0c233-96b3-7cf3-871b-7beb1fc71679"
        session = SimpleNamespace(harness="codex", pid=123, proc_start="start-1", cwd="/work",
                                  app_server=False, managed_dir=None, is_child=False, session_id=None)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(codex, "_home", return_value=tmp), \
             mock.patch.object(codex, "process_rollouts", return_value=({}, set())), \
             mock.patch.object(codex, "_transfer_managed_rollouts"), \
             mock.patch.object(codex, "_tick_subagents"), \
             mock.patch.object(herdr, "codex_pane_sessions", return_value={123: sid}) as pane_read, \
             mock.patch.object(procscan, "read_environ", return_value={}), \
             mock.patch.object(codex.session_registry, "read", return_value=None):
            codex.prepare_tick([session], wait_timeout=0.2)
        pane_read.assert_called_once_with([session], force_current=True, wait_timeout=0.2)
        self.assertEqual(session._herdr_session_id, sid)

    def test_exact_pid_start_and_current_thread_proof_gate_pane_identity(self):
        import pane_ownership

        sid = "01a0c233-96b3-7cf3-871b-7beb1fc71679"
        session = SimpleNamespace(pid=123, proc_start="start-1", cwd="/work")
        panes = [{"pane_id": "w1:p1", "agent": "codex", "cwd": "/work"}]
        evidence = herdr.PaneEvidence((set(), {123}, {}), complete=True)
        with mock.patch.object(herdr, "list_panes", return_value=panes) as pane_list, \
             mock.patch.object(herdr, "pane_evidence", return_value=evidence), \
             mock.patch.object(procscan, "read_proc_start", return_value="start-1"), \
             mock.patch.object(pane_ownership, "current_codex_thread", return_value=sid):
            self.assertEqual(herdr.codex_pane_sessions(
                [session], force_current=True, wait_timeout=0.2), {123: sid})
        pane_list.assert_called_once_with(wait_timeout=0.2)

        with mock.patch.object(herdr, "list_panes", return_value=panes), \
             mock.patch.object(herdr, "pane_evidence", return_value=evidence), \
             mock.patch.object(procscan, "read_proc_start", return_value="reused-start"), \
             mock.patch.object(pane_ownership, "current_codex_thread", return_value=sid):
            self.assertEqual(herdr.codex_pane_sessions([session], force_current=True), {})

        no_foreground = herdr.PaneEvidence((set(), set(), {}), complete=True)
        with mock.patch.object(herdr, "list_panes", return_value=panes), \
             mock.patch.object(herdr, "pane_evidence", return_value=no_foreground), \
             mock.patch.object(procscan, "read_proc_start", return_value="start-1"), \
             mock.patch.object(pane_ownership, "current_codex_thread", return_value=sid):
            self.assertEqual(herdr.codex_pane_sessions([session], force_current=True), {})

    def test_warm_fast_tick_reuses_sid_without_a_second_pane_read(self):
        sid = "01a0c233-96b3-7cf3-871b-7beb1fc71679"
        session = SimpleNamespace(pid=123, proc_start="start-1", cwd="/work")
        panes = [{"pane_id": "w1:p1", "agent": "codex", "cwd": "/work"}]
        evidence = herdr.PaneEvidence((set(), {123}, {}), complete=True)
        old_cache = dict(herdr._CODEX_PANE_CACHE)
        self.addCleanup(lambda: herdr._CODEX_PANE_CACHE.update(old_cache))
        herdr._CODEX_PANE_CACHE.update(key=None, until=0.0, identities={})
        with mock.patch.object(herdr, "list_panes", return_value=panes) as pane_list, \
             mock.patch.object(herdr, "pane_evidence", return_value=evidence), \
             mock.patch.object(procscan, "read_proc_start", return_value="start-1"):
            with mock.patch("pane_ownership.current_codex_thread", return_value=sid):
                self.assertEqual(herdr.codex_pane_sessions([session]), {123: sid})
                self.assertEqual(herdr.codex_pane_sessions([session]), {123: sid})
        pane_list.assert_called_once_with(wait_timeout=0.0)


if __name__ == "__main__":
    unittest.main()
