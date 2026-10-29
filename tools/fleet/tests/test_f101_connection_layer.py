"""F-101 connection-strip and peer-correlation contract tests."""
import json
import os
import re
import sys
import tempfile
import time
import unittest
from inspect import signature
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet import render
from fleet.collectors import peer_messages
from fleet.model import DispatchJob, Session, SubAgent

_GOLDEN_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "connection_layer")


def _text(segs):
    return "".join(t for t, _k in segs)


def _lines_text(lines):
    return ["" if ln is None else _text(ln) for ln in lines]


def _rec(frm, to, kind, minutes=1, status="sent", harness="claude"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes * 60))
    return {"ts": ts, "kind": kind,
            "from": {"harness": harness, "session_id": frm, "name": "sender"},
            "to": {"harness": harness, "session_id": to},
            "delivery": {"status": status}}


class StripContractTest(unittest.TestCase):
    def test_all_connection_strips_accept_width(self):
        for name in ("_subagent_strip", "_gpu_resource_strip", "_peer_link_strip"):
            self.assertIn("term_width", signature(getattr(render, name)).parameters)

    def test_peer_fail_soft_and_fit(self):
        peer = render._peer_link_strip(None, {"from_session_id": "sid", "from_name": "a",
                                              "kind": "handoff", "age_min": 2}, term_width=12)
        self.assertLessEqual(sum(render._dw(t) for t, _ in peer[0]), 12)
        # A record naming no endpoint at all — no tag, no name, no id, no harness — is
        # not a relation and draws nothing. Anything else now DOES draw (see
        # OffScreenPeerTest): endpoint visibility stopped deciding existence.
        self.assertEqual(render._peer_link_strip(None, {"from_session_id": ""}), [])
        self.assertEqual(render._peer_link_strip(None, None), [])
    def test_icon_says_kind_and_arrow_says_direction(self):
        """User decision 2026-09-09: the glyph carries WHAT, the arrow carries WHICH WAY,
        and neither repeats the other — so no icon may vary between the two directions."""
        recv = _text(render._peer_link_strip(
            None, {"from_session_id": "s", "from_name": "peer", "age_min": 1}, {})[0])
        sent = _text(render._peer_link_strip(
            {"to_session_id": "s", "to_name": "peer", "age_min": 1}, None, {})[0])
        self.assertIn("✉ ←", recv)
        self.assertIn("✉ →", sent)


class PeerEndpointLabelTest(unittest.TestCase):
    """The label ladder: on-screen tag → ledger name → `<harness>:<sid8>`. A full raw
    session id is never a label (user 2026-09-09 saw
    `← 01a084f7-63f2-7961-ae60-6fc2d8e60fc2` on the board)."""

    _SID = "01a084f7-63f2-7961-ae60-6fc2d8e60fc2"

    def _label(self, tag_by_key, name=None):
        entry = {"from_harness": "codex", "from_session_id": self._SID,
                 "from_name": name, "age_min": 3}
        return _text(render._peer_link_strip(None, entry, tag_by_key)[0])

    def test_visible_endpoint_uses_its_badge(self):
        self.assertIn("[3a]", self._label({("codex", self._SID): "3a"}))

    # These two pin the fallback ladder BELOW the badge, so the resolver is stubbed out.
    # Without the stub they read this machine's live session records: `_SID` is a real id,
    # and once off-board peers learned to resolve their own tag both started returning
    # `[3a] codex` here — a test failing on live state rather than on the behavior it names.

    def test_offscreen_endpoint_falls_back_to_the_ledger_name(self):
        with mock.patch.object(render, "_resolve_session_tag", return_value=None):
            text = self._label({}, name="peer-test-codex")
        self.assertIn("peer-test-codex", text)
        self.assertNotIn(self._SID, text)

    def test_nameless_offscreen_endpoint_keeps_short_prefix_without_repo_evidence(self):
        with mock.patch.object(render, "_resolve_session_tag", return_value=None):
            text = self._label({})
        self.assertIn("01a084f7", text)
        self.assertNotIn(self._SID, text)

    def test_an_offscreen_endpoint_still_gets_its_badge_when_one_resolves(self):
        # The point of resolving off-board: one session keeps one shape whether or not it
        # happens to be on screen this tick.
        with mock.patch.object(render, "_resolve_session_tag", return_value="3a"):
            text = self._label({}, name="peer-test-codex")
        self.assertIn("[3a]", text)

    def test_the_badge_wins_over_a_name(self):
        text = self._label({("codex", self._SID): "3a"}, name="peer-test-codex")
        self.assertIn("[3a]", text)
        self.assertNotIn("peer-test-codex", text)


class PeerCorrelationTest(unittest.TestCase):
    def _write(self, root, records, sender="source"):
        path = os.path.join(root, "peer-messages", "2026-09")
        os.makedirs(path)
        with open(os.path.join(path, sender + ".jsonl"), "w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def test_notice_inherits_kind_and_is_not_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, [_rec("a", "b", "steer"), _rec("a", "b", "notice")])
            row = peer_messages.collect(state_roots=[tmp])["by_session"][("claude", "b")]
        self.assertEqual(row["recv_1h"], 1)
        self.assertEqual(row["last_recv"]["kind"], "steer")
        self.assertEqual(set(row["last_recv"]),
                         {"from_name", "from_session_id", "from_harness", "kind", "age_min"})

    def test_correlated_notice_replaces_stale_intervening_sender(self):
        """A correlated notice is the newest successful receipt for `to`, even when a
        different sender's record landed in between — `last_recv` must move to the
        notice's correlated (sender, kind), not stay pinned on the intervening sender.
        `recv_1h` still counts the notice's logical message once (F-101i)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, [_rec("a", "b", "steer", minutes=3),
                              _rec("c", "b", "handoff", minutes=2),
                              _rec("a", "b", "notice", minutes=1)])
            row = peer_messages.collect(state_roots=[tmp])["by_session"][("claude", "b")]
        self.assertEqual(row["recv_1h"], 2)
        self.assertEqual(row["last_recv"]["from_session_id"], "a")
        self.assertEqual(row["last_recv"]["kind"], "steer")

    def test_cross_harness_identity_and_failed_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, [_rec("a", "same", "steer", harness="claude"),
                              _rec("a", "same", "handoff", harness="codex"),
                              _rec("a", "b", "steer", status="failed")])
            result = peer_messages.collect(state_roots=[tmp])
        self.assertIn(("claude", "same"), result["by_session"])
        self.assertIn(("codex", "same"), result["by_session"])
        failed_sender = result["by_session"][("claude", "a")]
        self.assertEqual(failed_sender["sent_1h"], 2)
        self.assertNotIn(("claude", "b"), result["by_session"])


class ConnectionLayerInsetOrderTest(unittest.TestCase):
    """Subagent/GPU strips share the inset and precede children.
    Peer direction now lives in the session identity row."""

    def test_three_strips_share_inset_and_precede_children(self):
        self.addCleanup(render.set_compute_hosts, None)
        render.set_compute_hosts({"hosts": [{"host": "h1", "gpus": [{
            "index": 0, "name": "RTX", "processes": [{
                "session_owner": {"kind": "session", "harness": "claude", "id": "sidA"},
                "used_memory_mib": 100}]}]}]})
        a = Session(harness="claude", pid=1, cwd="/x/repo-a", slug="a", session_id="sidA",
                   liveness="working", elapsed_min=1,
                   subagents=[SubAgent(agent_type="explore", active=True,
                                       started_at=time.time() - 60)],
                   peer_last_recv={"from_name": "peerB", "from_session_id": "sidB",
                                   "from_harness": "claude", "kind": "handoff", "age_min": 2},
                   steward=True, steward_targets=[{"harness": "claude", "session_id": "sidC"}])
        b = Session(harness="claude", pid=2, cwd="/x/repo-b", slug="b", session_id="sidB",
                   liveness="working", elapsed_min=1)
        c = Session(harness="claude", pid=3, cwd="/x/repo-c", slug="c", session_id="sidC",
                   liveness="working", elapsed_min=1, session_tag="46")
        dispatch_kid = DispatchJob(key="autopilot-code", slug="job1", cwd="/x/repo-a",
                                   parent_sid="sidA", is_child=True, liveness="working")
        plugin_kid = DispatchJob(key="codex", slug="plugin1", cwd="/x/repo-a",
                                 parent_sid="sidA", is_child=True, liveness="working",
                                 surface_kind="plugin-agent")
        lines = render._build_lines([a, b, c], [dispatch_kid, plugin_kid], "both", False, 0,
                                    layout="wide", term_width=168)
        rows = _lines_text(lines)

        def idx(pred):
            for i, t in enumerate(rows):
                if pred(t):
                    return i
            self.fail("no matching row found among: %r" % rows)

        subagent_i = idx(lambda t: render._ICON_SUBAGENT in t and "codex task" not in t)
        gpu_i = idx(lambda t: "GPU h1:0" in t)
        # Each strip's leading CONTENT glyph is its own icon.
        plugin_i = idx(lambda t: "codex task" in t)
        dispatch_i = idx(lambda t: "job1" in t)

        # A group-body tint (F-19, `▍` rail-char fallback outside true-color terminals)
        # may consume one cell of the leading inset without changing its total display
        # width — compare the DISPLAY-WIDTH offset of each strip's first content glyph
        # rather than the raw segment text, so the assertion holds under either tint mode.
        def content_offset(i, glyph):
            text = rows[i]
            return render._dw(text[:text.index(glyph)])

        offsets = {"subagent": content_offset(subagent_i, render._ICON_SUBAGENT),
                   "gpu": content_offset(gpu_i, "●")}
        self.assertEqual(len(set(offsets.values())), 1,
                         "inset offsets differ across strips: %r" % offsets)
        self.assertEqual(next(iter(offsets.values())), render._dw(render._SUBAGENT_IND))

        for label, i in (("subagent", subagent_i), ("gpu", gpu_i)):
            self.assertLess(i, plugin_i, "%s row must precede the plugin-agent row" % label)
            self.assertLess(i, dispatch_i, "%s row must precede the dispatch-child row" % label)


class OffScreenPeerTest(unittest.TestCase):
    """The inline relation survives off-board and folded peer rows."""

    def _fixture(self):
        a = Session(harness="claude", pid=1, cwd="/x/group1", slug="a", session_id="sidA",
                   liveness="working", elapsed_min=1,
                   peer_last_recv={"from_name": "peerB", "from_session_id": "sidB",
                                   "from_harness": "claude", "kind": "handoff", "age_min": 2})
        b = Session(harness="claude", pid=2, cwd="/x/group2", slug="b", session_id="sidB",
                   liveness="stale", elapsed_min=100, session_tag="b0")
        return a, b

    def _rows(self, sessions, show_all=False):
        prev = render._SHOW_ALL
        render._SHOW_ALL = show_all
        try:
            return _lines_text(render._build_lines(sessions, [], "fleet", False, 0,
                                                   layout="wide", term_width=168))
        finally:
            render._SHOW_ALL = prev

    def test_link_survives_a_folded_target_and_is_named(self):
        a, b = self._fixture()
        rows = self._rows([a, b])
        self.assertTrue(any("← CL/sidB" in t for t in rows))
        self.assertTrue(any("folded" in t for t in rows))

    def test_visible_target_upgrades_the_label_to_its_badge(self):
        a, b = self._fixture()
        rows = self._rows([a, b], show_all=True)
        self.assertTrue(any("← [b0]" in t for t in rows))

    def test_shape_is_identical_under_group_order_reversal(self):
        a, b = self._fixture()
        # Both renders must observe the same animation frame. Working-session
        # spinners are clock-driven, and these calls can otherwise straddle a tick.
        with mock.patch.object(render.time, "time", return_value=1_800_000_000.0):
            forward = [t for t in self._rows([a, b]) if "←" in t and "sidB" in t]
            reversed_ = [t for t in self._rows([b, a]) if "←" in t and "sidB" in t]
        self.assertEqual(forward, reversed_)
        self.assertEqual(len(forward), 1)


class StewardParentStripTest(unittest.TestCase):
    """`⚑ ←` — the watched session finally shows who is watching it. The steward marker
    is written on one side only, so before this the target row carried no reverse field
    at all (measured 2026-09-09)."""

    def test_target_row_shows_its_supervisor(self):
        parent = Session(harness="claude", pid=1, cwd="/x/p", slug="p", session_id="sidP",
                         liveness="working", elapsed_min=1, session_tag="b0", steward=True,
                         steward_targets=[{"harness": "codex", "session_id": "sidT"}])
        target = Session(harness="codex", pid=2, cwd="/x/p", slug="t", session_id="sidT",
                         liveness="working", elapsed_min=1, session_tag="13",
                         steward_parents=[{"harness": "claude", "session_id": "sidP",
                                           "name": "hearting-b0"}])
        rows = _lines_text(render._build_lines([parent, target], [], "fleet", False, 0,
                                               layout="wide", term_width=168))
        self.assertFalse(any("⚑ →" in t for t in rows))
        self.assertFalse(any("⚑ ←" in t for t in rows))
        self.assertTrue(any("⚑" in t for t in rows))


class RelationLineBudgetTest(unittest.TestCase):
    """Peer relations use one row; steward relations use under-id lines."""

    def _busiest_session(self):
        return Session(
            harness="claude", pid=1, cwd="/x/repo", slug="a", session_id="sidA",
            liveness="working", elapsed_min=1, session_tag="a1",
            peer_last_sent={"to_name": "peerB", "to_session_id": "sidB",
                            "to_harness": "claude", "kind": "steer", "age_min": 1},
            peer_last_recv={"from_name": "peerC", "from_session_id": "sidC",
                            "from_harness": "codex", "kind": "handoff", "age_min": 2},
            steward=True,
            steward_targets=[{"harness": "claude", "session_id": "sidD"}],
            steward_parents=[{"harness": "claude", "session_id": "sidE",
                              "name": "hearting-b0"}])

    def _relation_row(self, width=168):
        row = self._busiest_session()
        return render._plain(render._session_row(
            row, narrow=False, name_width=max(4, width - 100)))

    def test_peer_relation_spends_no_extra_row(self):
        row = self._relation_row()
        self.assertIn("→", row)
        self.assertIn("←", row)
        self.assertNotIn(render._ICON_PEER, row)

    def test_each_row_carries_both_of_its_directions(self):
        row = self._relation_row()
        self.assertIn("→", row)
        self.assertIn("←", row)

    def test_the_icon_is_not_repeated_for_the_second_direction(self):
        # The icon says what the relation is — once per row. Repeating it would make the
        # second half read as a separate relation, which is what the two rows already did.
        row = self._relation_row()
        self.assertEqual(row.count(render._ICON_PEER), 0)

    def test_a_single_direction_still_draws_one_row_not_a_dangling_arrow(self):
        line = render._peer_link_strip(
            {"to_session_id": "s", "to_name": "peer", "age_min": 1}, None, {})
        self.assertEqual(len(line), 1)
        self.assertNotIn("←", _text(line[0]))

    def test_a_narrow_terminal_keeps_both_endpoints_and_drops_the_detail(self):
        # Endpoints are the fact; kind and age are the trimming allowance.
        sent = {"to_session_id": "sB", "to_name": "peerB", "kind": "steer", "age_min": 1}
        recv = {"from_session_id": "sC", "from_name": "peerC", "kind": "handoff",
                "age_min": 2}
        tags = {("", "sB"): "b0", ("", "sC"): "c0"}
        with mock.patch.object(render, "_resolve_session_tag", return_value=None):
            segs = render._peer_link_strip(sent, recv, tags, term_width=46,
                repo_by_key={("", "sB"): "repoB", ("", "sC"): "repoC"})[0]
        text = _text(segs)
        self.assertLessEqual(sum(render._dw(t) for t, _k in segs), 46)
        self.assertIn("[b0]", text)
        self.assertIn("[c0]", text)
        self.assertIn("repoB", text)
        self.assertIn("repoC", text)
        self.assertNotIn("steer", text)

class LedgerAbsentByteIdenticalTest(unittest.TestCase):
    """F-101g-(7) — with no connection-layer fields set at all (ledger absent), a
    fixed 3-session render must stay byte-identical to the pre-F-101 golden captured
    on `a1f16631` (see plan.md §5, procedure step 3), except for explicitly recorded
    intentional output changes below. Each exception changes only its named captured
    bytes; a mismatch elsewhere is a real regression, not a stale fixture.
    Exception on record: 2026-09-04 the opencode row's source-absence message was
    corrected ("plan quota is console-only" → "opencode-go key not found") after the
    Go usage API shipped upstream (#31084/PR #2879) — the four r7 goldens were
    regenerated with the exact capture procedure above for that intentional change.
    Exception on record: 2026-09-29 the usage-header label for that same harness became
    "opencode go" (user: 상단 opencode→opencode go, so the row names the GO account whose
    quota it shows, matching 'claude code') — the four r7 goldens were regenerated with
    the exact capture procedure above; the only diff was that one label line per width.
    Exception on record: 2026-10-08 the approved shared detail inset and aligned
    MAIN/OWNER/FRAME gauge anchor moved the three context tracks from 12 to 20;
    these four goldens record that intentional layout change.
    Exception on record: 2026-10-10 the approved P5 audit fixes add Korean
    vocabulary, named context and separate state legends; below 138 columns the
    public renderer now uses the actual narrow layout. Later that day the user
    requested original technical terms and removal of context/NOW labels. These
    fixed-clock goldens retain the layout changes without absent connection rows.
    Exception on record: 2026-10-10 the user then requested compact English
    Fleet labels; only the three legend lines change in these four goldens.
    Exception on record: 2026-10-12 the approved per-repository mouse fold adds
    the open-repository arrow to each heading. Only those three heading prefixes
    change in these four goldens; absent connection rows remain absent.
    Exception on record: 2026-10-19 provider status without an account observation
    reads `provider availability unknown`. The approved 2026-10-20 OpenCode header
    cleanup now limits the default row to Go quota bars or `Go usage unavailable` with
    one approved failure reason; provider matrices remain in JSON. At each captured width only the
    OpenCode usage row changes; all other bytes are kept."""

    # 2026-10-29: the approved Claude account-status header replaces the obsolete
    # console-only message with `unavailable`; all connection/body bytes stay fixed.
    def _render(self, width):
        with mock.patch("time.time", return_value=1700000000.0):
            # Fixed successful subjects keep this connection-layer golden
            # independent of title generation/failure fallback policy.
            beta = Session(harness="codex", pid=1, cwd="/x/beta", slug="beta",
                           session_id="b1", runtime_name="beta", liveness="working", elapsed_min=7)
            alpha = Session(harness="claude", pid=2, cwd="/x/alpha", slug="alpha",
                            session_id="a1", runtime_name="alpha", liveness="idle", elapsed_min=5)
            gamma = Session(harness="opencode", pid=3, cwd="/x/gamma", slug="gamma",
                            session_id="g1", runtime_name="gamma", liveness="done", elapsed_min=9)
            lines = render._build_lines([beta, alpha, gamma], [], "fleet", False, 0,
                                        layout="wide", term_width=width)
        return "\n".join(_lines_text(lines))

    def test_matches_committed_golden_at_every_captured_width(self):
        for width in (60, 100, 120, 168):
            with self.subTest(width=width):
                golden_path = os.path.join(_GOLDEN_DIR,
                                           "fleet-render-r7-noledger-%d.txt" % width)
                with open(golden_path, encoding="utf-8") as fh:
                    golden = fh.read().rstrip("\n")
                self.assertEqual(self._render(width), golden)


if __name__ == "__main__":
    unittest.main()
