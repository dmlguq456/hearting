"""Compact peer direction labels and freshness-bound display observations."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import details, model, render  # noqa: E402
from fleet.display_snapshot import DisplaySnapshot, build, extend, path_label  # noqa: E402
from fleet.refresh import LiveSnapshot  # noqa: E402


def _session(sent=None, received=None):
    row = model.Session(harness="codex", pid=11, proc_start="11-start",
                        session_id="current-thread", cwd="/workspace/hearting",
                        slug="hearting", title="Current conversation", liveness="working",
                        session_tag="11")
    row.peer_last_sent = sent
    row.peer_last_recv = received
    return row


def _plain(segs):
    return "".join(text for text, _key in segs)


class PeerInlineTest(unittest.TestCase):
    def setUp(self):
        self.peer_id = "0123456789abcdef0123456789abcdef"
        self.tags = {("claude", self.peer_id): "4a",
                     ("opencode", self.peer_id): "7b"}

    def test_exact_same_peer_uses_one_bidirectional_identity(self):
        sent = {"to_harness": "claude", "to_session_id": self.peer_id}
        received = {"from_harness": "claude", "from_session_id": self.peer_id}
        row = _session(sent, received)
        wide = _plain(render._session_row(row, narrow=False, name_width=40,
                                          tag_by_key=self.tags))
        narrow, _detail = render._session_row_2line(row, term_width=70,
                                                    tag_by_key=self.tags)
        self.assertIn("↔", wide)
        self.assertIn("[4a]", wide)
        self.assertIn("↔", _plain(narrow))
        self.assertNotIn("→", wide)
        self.assertNotIn("←", wide)

    def test_same_short_id_across_harnesses_keeps_two_directions(self):
        sent = {"to_harness": "claude", "to_session_id": self.peer_id}
        received = {"from_harness": "opencode", "from_session_id": self.peer_id}
        row = _session(sent, received)
        row.peer_last_sent = sent
        row.peer_last_recv = received
        for width in (46, 60, 69, 70, 71, 100, 137, 138, 168):
            with self.subTest(width=width):
                if width < render._NARROW_CUTOFF:
                    lines = render._session_row_stack(row, term_width=width,
                                                       tag_by_key=self.tags)
                    identity = _plain(lines[0])
                    self.assertEqual(len(lines), 2)
                elif width < render._TWO_LINE_CUTOFF:
                    lines = render._session_row_2line(row, term_width=width,
                                                       tag_by_key=self.tags)
                    identity = _plain(lines[0])
                    self.assertEqual(len(lines), 2)
                else:
                    identity = _plain(render._session_row(
                        row, narrow=False, name_width=max(4, width - 100),
                        tag_by_key=self.tags))
                self.assertIn("→", identity)
                self.assertIn("←", identity)
                self.assertNotIn("↔", identity)

    def test_unproven_endpoints_keep_separate_directions(self):
        cases = (
            ({"to_harness": "claude"}, {"from_harness": "claude"}),
            ({"to_session_id": self.peer_id}, {"from_session_id": self.peer_id}),
            ({"to_harness": "unknown", "to_session_id": self.peer_id},
             {"from_harness": "unknown", "from_session_id": self.peer_id}),
            ({"kind": "steer"}, {"kind": "report"}),
        )
        for sent, received in cases:
            with self.subTest(sent=sent, received=received):
                row = _session(sent, received)
                for option in render._peer_inline_options(row, self.tags):
                    text = _plain(option)
                    self.assertIn("→", text)
                    self.assertIn("←", text)
                    self.assertNotIn("↔", text)
                self.assertEqual(row.to_dict()["peer_last_sent"], sent)
                self.assertEqual(row.to_dict()["peer_last_recv"], received)

    def test_colliding_tags_are_qualified_and_disambiguated(self):
        sent_id, received_id = "peerAAAA1", "peerBBBB2"
        row = _session({"to_harness": "claude", "to_session_id": sent_id},
                       {"from_harness": "codex", "from_session_id": received_id})
        text = _plain(render._peer_inline_options(row, {
            ("claude", sent_id): "4a11", ("codex", received_id): "4a22"})[0])
        self.assertIn("→ CL[4a]", text)
        self.assertIn("← CX[4a]", text)

        row.peer_last_recv = {"from_harness": "claude", "from_session_id": received_id}
        text = _plain(render._peer_inline_options(row, {
            ("claude", sent_id): "4a11", ("claude", received_id): "4a22"})[0])
        self.assertIn("CL[4a]~1", text)
        self.assertIn("CL[4a]~2", text)

    def test_colliding_untagged_sid_prefixes_remain_distinct(self):
        sent_id, received_id = "abcdef123456", "abcdef654321"
        row = _session({"to_harness": "claude", "to_session_id": sent_id},
                       {"from_harness": "claude", "from_session_id": received_id})
        text = _plain(render._peer_inline_options(row, {
            ("claude", sent_id): None, ("claude", received_id): None})[0])
        self.assertIn("CL/abcdef~6", text)
        self.assertIn("CL/abcdef~1", text)
        self.assertIn("→", text)
        self.assertIn("←", text)

    def test_snapshot_draw_has_no_peer_strip_or_extra_peer_row(self):
        row = _session({"to_harness": "claude", "to_session_id": self.peer_id},
                       {"from_harness": "opencode", "from_session_id": self.peer_id})
        display = DisplaySnapshot({row.cwd: "hearting"})
        with mock.patch.object(render, "_peer_link_strip",
                               side_effect=AssertionError("legacy peer strip rendered")), \
                mock.patch.object(render, "_gpu_session_resources", return_value={}), \
                mock.patch.object(render, "_fresh_compute_hosts", return_value=({}, 0)), \
                mock.patch.object(render, "project_of",
                                  side_effect=AssertionError("render repeated project lookup")):
            for width in (168, 100, 70, 69, 60, 46):
                with self.subTest(width=width):
                    row.peer_last_sent = {"to_harness": "claude", "to_session_id": self.peer_id}
                    row.peer_last_recv = {"from_harness": "opencode", "from_session_id": self.peer_id}
                    peer_lines = render._build_lines(
                        [row], [], "both", width < render._NARROW_CUTOFF, 0,
                        term_width=width, resources=[], usage_snapshots={},
                        display_snapshot=display)
                    peer_selectable = list(render._SELECTABLE)
                    plain_peer = "\n".join(_plain(line or []) for line in peer_lines)
                    row.peer_last_sent = None
                    row.peer_last_recv = None
                    no_peer_lines = render._build_lines(
                        [row], [], "both", width < render._NARROW_CUTOFF, 0,
                        term_width=width, resources=[], usage_snapshots={},
                        display_snapshot=display)
                    no_peer_selectable = list(render._SELECTABLE)
                    self.assertEqual(len(peer_lines), len(no_peer_lines))
                    self.assertEqual(peer_selectable, no_peer_selectable)
                    self.assertIn("→", plain_peer)
                    self.assertIn("←", plain_peer)

    def test_wire_json_keeps_full_peer_identity_and_payload_shape(self):
        sent = {"to_harness": "claude", "to_session_id": self.peer_id,
                "kind": "steer", "age_min": 3}
        row = _session(sent)
        self.assertEqual(row.to_dict()["peer_last_sent"], sent)


class DisplaySnapshotTest(unittest.TestCase):
    def test_project_labels_resolve_once_per_distinct_cwd(self):
        rows = [model.Session(harness="codex", pid=1, cwd="/repo/a"),
                model.Session(harness="codex", pid=2, cwd="/repo/a"),
                model.Session(harness="codex", pid=3, cwd="/repo/b")]
        jobs = [model.DispatchJob(key="job", cwd="/repo/job", parent_cwd="/repo/a")]
        seen = []
        with mock.patch.object(model, "project_of",
                               side_effect=lambda cwd: seen.append(cwd) or cwd.rsplit("/", 1)[-1]):
            observed = build(rows, jobs)
            detailed = extend(observed, memory={"periodic_curate": {
                "total": 1, "current_cwd": "/repo/curator"}})
        self.assertEqual(seen, ["/repo/a", "/repo/b", "/repo/job", "/repo/curator"])
        self.assertEqual(observed.project_for("/repo/a", lambda _cwd: "fallback"), "a")
        self.assertEqual(detailed.project_for("/repo/curator", lambda _cwd: "fallback"),
                         "curator")

    def test_missing_snapshot_path_uses_pure_grouping_fallback(self):
        with mock.patch.object(render, "project_of",
                               side_effect=AssertionError("fallback touched the filesystem")):
            self.assertEqual(render._project_for_display(
                "/repo/hearting/.worktrees/task", DisplaySnapshot({})), "hearting")
        self.assertEqual(path_label("/workspace/repo-wt"), "repo")

    def test_detail_merge_keeps_the_basic_display_observation(self):
        row = model.Session(harness="codex", pid=1, session_id="one", cwd="/repo/a",
                            liveness="working")
        source = LiveSnapshot(sessions=[row],
                              display_snapshot=DisplaySnapshot({"/repo/a": "basic"}))
        detailed = LiveSnapshot(sessions=[row],
                                display_snapshot=DisplaySnapshot({"/repo/a": "detail"}))
        detailed.display_snapshot = extend(
            detailed.display_snapshot, paths=("/repo/curator",))
        merged = details.merge(source, details.DetailSnapshot(details.state_key(source), detailed))
        self.assertEqual(merged.display_snapshot.project_for("/repo/a", str), "basic")
        self.assertEqual(merged.display_snapshot.project_for("/repo/curator", str), "curator")

        newer = LiveSnapshot(sessions=[model.Session(harness="codex", pid=2, session_id="two",
                                                      cwd="/repo/b", liveness="working")],
                             display_snapshot=DisplaySnapshot({"/repo/b": "new basic"}))
        stale_merge = details.merge(newer, details.DetailSnapshot(details.state_key(source), detailed))
        self.assertEqual(stale_merge.display_snapshot.project_for("/repo/b", str), "new basic")
        self.assertEqual(stale_merge.display_snapshot.project_for(
            "/repo/curator", lambda _cwd: "missing"), "missing")


if __name__ == "__main__":
    unittest.main()
