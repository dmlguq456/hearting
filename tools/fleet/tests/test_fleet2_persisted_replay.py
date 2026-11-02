"""Private complete-VM cache and stale replay regressions."""
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.fleet.explorer import order
from tools.fleet.explorer.frame import CellFrame
from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.view_model import ExplorerViewModel
from tools.fleet import render


def complete_readiness(at=100.0, generation=3):
    return {
        "ready": True, "fully_fresh": True, "all_new": True,
        "observed_at": at, "generation": generation,
        "generation_identity": "generation-" + str(generation),
        "sources": {"basic": {"state": "complete", "observed_at": at},
                    "required-details": {"state": "complete", "observed_at": at},
                    "resource-runs": {"state": "absent", "attempted": True,
                                      "all_new": True, "observed_at": at}},
    }


def display_vm(**session_overrides):
    session = {
        "tag": "c7", "harness": "codex", "state": "working", "title": "safe title",
        "now": "editing", "context": 42, "repo": "hearting", "repo_id": "repo-a",
        "branch": "main", "pid": 48123, "sid": "private-session-id",
        "cwd": "/home/person/private-worktree", "pane": "%99",
        "pane_lines": [["PANE_SENTINEL", "soft"]],
        "attempt_id": "ATTEMPT_SENTINEL", "source_records": {"raw": "SOURCE_SENTINEL"},
        "question": "QUESTION_SENTINEL",
    }
    session.update(session_overrides)
    return ExplorerViewModel.from_mapping({
        "identity": "internal-generation-id", "hearting": {
            "version": "v3.50.3-20-gad247e2b6", "install_method": "managed/stable"},
        "clock": 1700000000,
        "hosts": [{"mark": "⌂", "name": "gpu-a01", "key": "base_host"}],
        "usage": [{"harness": "codex", "text": "5h 72%", "key": "usage_codex",
                    "windows": [["5h", 72, 28]]}],
        "repos": [{"id": "repo-a", "name": "hearting", "done": 2, "age": "4m"}],
        "sessions": [session],
        "routes": [{"id": "route-a", "repo": "hearting", "session_tag": "c7",
                    "title": "safe route", "state": "active", "worker_stage": "execute",
                    "worktree": "/private/path", "activity": "RAW_ACTIVITY_SENTINEL"}],
        "compute": [["gpu-a01", True, [12.0, 16], None, None, []]],
        "nodes": {"route-a": [{"lines": ["RAW_NODE_SENTINEL"]}]},
        "attempts": {"lines": ["RAW_ATTEMPT_SENTINEL"]},
        "questions": [{"title": "RAW_QUESTION_SENTINEL"}],
        "dialogue": {"logs": {"c7:0": {"lines": ["RAW_DIALOGUE_SENTINEL"]}}},
        "config": {"api_key": "SECRET_SENTINEL=do-not-cache"},
        "source_records": {"raw": "RAW_SOURCE_SENTINEL"},
        "ordering": {"saved": {"repos": ["repo-a"], "sessions": {"repo-a": ["sid:codex:private-session-id"]}},
                      "repo_keys": ["repo-a"], "session_keys": {"repo-a": ["sid:codex:private-session-id"]},
                      "tag_keys": {"c7": "sid:codex:private-session-id"},
                      "repo_by_tag": {"c7": "repo-a"}, "tags_by_key": {"sid:codex:private-session-id": "c7"},
                      "tag_cycle": ["session:c7"]},
    })


class PersistedReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_home = Path(self.temp.name)
        self.runtime = ExplorerRuntime(self.state_home)
        self.runtime.configure_persistence({"codex"})
        self.order_patch = patch.object(order, "order_path", return_value=self.state_home / "order.json")
        self.order_patch.start()
        self.addCleanup(self.order_patch.stop)

    def save(self, vm=None, readiness=None):
        thread = self.runtime.persist_complete_async(vm or display_vm(),
                                                     readiness or complete_readiness())
        self.assertIsNotNone(thread)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.runtime.cache_path.exists())

    def test_only_allowlisted_display_vm_is_persisted_and_loaded_frozen(self):
        vm = display_vm(title="SECRET_SENTINEL=redact-this")
        self.save(vm)
        cache = self.runtime.cache_path
        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
        raw = cache.read_bytes()
        for sentinel in (b"SECRET_SENTINEL", b"PANE_SENTINEL", b"SOURCE_SENTINEL",
                         b"QUESTION_SENTINEL", b"RAW_DIALOGUE_SENTINEL",
                         b"RAW_ATTEMPT_SENTINEL", b"RAW_NODE_SENTINEL",
                         b"RAW_ACTIVITY_SENTINEL", b"/home/person/private-worktree"):
            self.assertNotIn(sentinel, raw)
        self.assertNotIn(b"v3.50.3-20-gad247e2b6", raw)
        self.assertNotIn(b"managed/stable", raw)
        cached = self.runtime.load_last_complete({"codex"})
        self.assertIsNotNone(cached)
        session = cached.sessions[0]
        self.assertNotIn("title", session)
        self.assertNotIn("pid", session)
        self.assertNotIn("sid", session)
        self.assertNotIn("pane", session)
        self.assertEqual(cached.clock, 1700000000)
        self.assertIsNone(cached.hearting)
        self.assertEqual(cached.compute[0][2], (12.0, 16))
        with self.assertRaises(TypeError):
            session["state"] = "dead"

    def test_incomplete_and_continued_generations_are_not_written(self):
        vm = display_vm()
        for readiness in (
            {"ready": False, "fully_fresh": False, "all_new": False,
             "observed_at": 100.0, "generation": 3},
            {"ready": True, "fully_fresh": True, "all_new": False,
             "observed_at": 100.0, "generation": 3},
        ):
            self.assertIsNone(self.runtime.persist_complete_async(vm, readiness))
        self.assertFalse(self.runtime.cache_path.exists())

    def test_filter_mismatch_permissions_and_unknown_nested_fields_fail_closed(self):
        self.save()
        cache = self.runtime.cache_path
        wrong_filter = ExplorerRuntime(self.state_home)
        self.assertIsNone(wrong_filter.load_last_complete({"claude"}))

        cache.chmod(0o644)
        self.assertIsNone(self.runtime.load_last_complete({"codex"}))
        cache.chmod(0o600)

        envelope = json.loads(cache.read_text(encoding="utf-8"))
        envelope["vm"]["source_records"] = {"raw": "must be rejected"}
        cache.write_text(json.dumps(envelope), encoding="utf-8")
        cache.chmod(0o600)
        self.assertIsNone(self.runtime.load_last_complete({"codex"}))

    def test_older_late_writer_and_failed_replace_preserve_newer_valid_cache(self):
        newer = self.runtime._cache_envelope(display_vm(), complete_readiness(200, 4))
        older = self.runtime._cache_envelope(display_vm(), complete_readiness(100, 3))
        self.assertTrue(self.runtime._write_cache(newer))
        before = self.runtime.cache_path.read_bytes()
        self.assertFalse(self.runtime._write_cache(older))
        self.assertEqual(self.runtime.cache_path.read_bytes(), before)

        latest = self.runtime._cache_envelope(display_vm(), complete_readiness(300, 5))
        with patch("tools.fleet.explorer.runtime.os.replace", side_effect=OSError("simulated interruption")):
            self.assertFalse(self.runtime._write_cache(latest))
        self.assertEqual(self.runtime.cache_path.read_bytes(), before)
        with patch("tools.fleet.explorer.runtime.os.fsync", side_effect=[None, OSError("directory sync")]):
            self.assertFalse(self.runtime._write_cache(latest))
        self.assertEqual(self.runtime.cache_path.read_bytes(), before)
        self.assertIsNotNone(self.runtime.load_last_complete({"codex"}))

    def test_symlink_cache_is_ignored(self):
        path = self.runtime.cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.state_home / "target.json"
        target.write_text("{}", encoding="utf-8")
        path.symlink_to(target)
        self.assertIsNone(self.runtime.load_last_complete({"codex"}))

    def test_replay_redraw_uses_current_geometry_and_freezes_dim_spinner(self):
        cached = self.runtime.prepare_replay(display_vm())
        with patch.object(render, "_HEARTING", {
                "version": "v3.50.3-20-gad247e2b6", "install_method": "managed/stable"}):
            small = render._explorer_frame_from_model(cached, None, 80, 24, beat=1)
            wide = render._explorer_frame_from_model(cached, None, 140, 45, beat=1)
        self.assertEqual((small.cols, small.rows), (80, 24))
        self.assertEqual((wide.cols, wide.rows), (140, 45))
        self.assertIn("hearting", small.text_rows()[0])
        self.assertIn("managed/stable", small.text_rows()[0])

        class Screen:
            def __init__(self):
                self.size, self.writes = (1, 1), []
            def getmaxyx(self):
                return self.size
            def erase(self):
                self.writes.clear()
            def addstr(self, y, x, text, attr=0):
                self.writes.append((text, attr))
            def refresh(self):
                pass

        screen = Screen()
        frame = CellFrame(((('⠏', 'spin', None),),), (), 1, 1)
        with patch.object(render, "_SPIN", tuple("abc")), patch.object(render.time, "time", return_value=0.1):
            render._draw_explorer_frame(screen, frame, object(), stale=True)
            self.assertEqual(screen.writes[0][0], "⠏")
            if getattr(render.curses, "A_DIM", 0):
                self.assertTrue(screen.writes[0][1] & render.curses.A_DIM)
            render._draw_explorer_frame(screen, frame, object(), stale=False)
            self.assertEqual(screen.writes[0][0], "b")

    def test_overbudget_identity_redraws_from_cache_at_resized_widths(self):
        from tools.fleet.explorer.grid import cell_width

        source = display_vm()
        values = source.to_mapping()
        host_name = "trainer-west-gpu-node-01"
        version = "v" + "x" * 100
        values["hosts"] = [{"mark": "⌂", "name": host_name, "key": "base_host"}]
        values["hearting"] = {"version": "stale-other-root", "install_method": "packaged"}
        self.save(ExplorerViewModel.from_mapping(values))
        cached = self.runtime.load_last_complete({"codex"})
        self.assertIsNotNone(cached)
        self.assertEqual(cached.hosts[0]["name"], host_name)
        self.assertIsNone(cached.hearting)

        with patch.object(render, "_HEARTING", {"version": version, "install_method": "unmanaged"}):
            for width, height in ((80, 24), (100, 30), (140, 45)):
                with self.subTest(width=width):
                    frame = render._explorer_frame_from_model(cached, None, width, height, beat=1)
                    row = frame.text_rows()[0]
                    self.assertEqual((frame.cols, frame.rows), (width, height))
                    self.assertLessEqual(cell_width(row), width)
                    self.assertIn("hearting", row)
                    self.assertIn("unmanaged", row)
                    self.assertNotIn("packaged", row)
                    self.assertRegex(row.rstrip(), r"\d{2}:\d{2}$")
                    if width == 80:
                        self.assertIn("…", row)


if __name__ == "__main__":
    unittest.main()
