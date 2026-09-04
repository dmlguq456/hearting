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

_EVIDENCE_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "evidence"))


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
        for name in ("_subagent_strip", "_gpu_resource_strip", "_peer_link_strip",
                     "_steward_link_strip"):
            self.assertIn("term_width", signature(getattr(render, name)).parameters)

    def test_peer_and_steward_fail_soft_and_fit(self):
        peer = render._peer_link_strip({"from_session_id": "sid", "from_name": "a",
                                        "kind": "handoff", "age_min": 2}, term_width=12)
        self.assertLessEqual(sum(render._dw(t) for t, _ in peer[0]), 12)
        self.assertEqual(render._peer_link_strip({"from_session_id": ""}), [])
        steward = render._steward_link_strip(
            [{"harness": "claude", "session_id": "s%d" % i} for i in range(20)],
            {("claude", "s%d" % i): "%02x" % i for i in range(20)}, term_width=60)
        self.assertLessEqual(sum(render._dw(t) for t, _ in steward[0]), 60)
        self.assertIn("+", "".join(t for t, _ in steward[0]))


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
    """F-101g-(3) — subagent/GPU/peer/steward all share the one inset and all four
    precede plugin-agent and dispatch-child rows for the same session."""

    def test_four_strips_share_inset_and_precede_children(self):
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
        peer_i = idx(lambda t: "←" in t and "peerB" in t)
        steward_i = idx(lambda t: "[46]" in t and "→" in t)
        plugin_i = idx(lambda t: "codex task" in t)
        dispatch_i = idx(lambda t: "job1" in t)

        # A group-body tint (F-19, `▍` rail-char fallback outside true-color terminals)
        # may consume one cell of the leading inset without changing its total display
        # width — compare the DISPLAY-WIDTH offset of each strip's first content glyph
        # rather than the raw segment text, so the assertion holds under either tint mode.
        def content_offset(i, glyph):
            text = rows[i]
            return render._dw(text[:text.index(glyph)])

        offsets = {
            "subagent": content_offset(subagent_i, render._ICON_SUBAGENT),
            "gpu": content_offset(gpu_i, "●"),
            "peer": content_offset(peer_i, "←"),
            "steward": content_offset(steward_i, "→"),
        }
        self.assertEqual(len(set(offsets.values())), 1,
                         "inset offsets differ across strips: %r" % offsets)
        self.assertEqual(next(iter(offsets.values())), render._dw(render._SUBAGENT_IND))

        for label, i in (("subagent", subagent_i), ("gpu", gpu_i), ("peer", peer_i),
                        ("steward", steward_i)):
            self.assertLess(i, plugin_i, "%s row must precede the plugin-agent row" % label)
            self.assertLess(i, dispatch_i, "%s row must precede the dispatch-child row" % label)


class StewardFoldPlaceholderTest(unittest.TestCase):
    """F-101g-(5) — steward `+N` folding keeps front tags and drops placeholders,
    join failures, and untagged children before folding at all."""

    def test_placeholders_and_join_failures_vanish_before_folding(self):
        targets = [{"harness": "claude", "session_id": "s%d" % i} for i in range(20)]
        targets.append({"harness": "claude", "session_id": None})          # placeholder
        targets.append({"harness": "claude", "session_id": "sBadJoin"})    # no ledger tag
        targets.append({"harness": "unknown", "session_id": "sBadTag"})    # tag resolves to None
        tag_by_key = {("claude", "s%d" % i): "%02x" % i for i in range(20)}
        tag_by_key[("unknown", "sBadTag")] = None

        segs = render._steward_link_strip(targets, tag_by_key, term_width=60)[0]
        text = _text(segs)
        self.assertLessEqual(sum(render._dw(t) for t, _k in segs), 60)
        self.assertRegex(text, r"^\s*→( \[[0-9a-f]{2}\])+ \+\d+$")
        self.assertIn("[00]", text)
        self.assertIn("[01]", text)
        shown = len(re.findall(r"\[[0-9a-f]{2}\]", text))
        rest = int(re.search(r"\+(\d+)", text).group(1))
        self.assertEqual(shown + rest, 20)
        self.assertNotIn("sBadJoin", text)
        self.assertNotIn("None", text)


class ConnectionLinkFoldTest(unittest.TestCase):
    """F-101g-(6) — a peer link to a folded/hidden target session disappears along
    with that session's row; the effect is symmetric under group-order reversal."""

    def _fixture(self):
        a = Session(harness="claude", pid=1, cwd="/x/group1", slug="a", session_id="sidA",
                   liveness="working", elapsed_min=1,
                   peer_last_recv={"from_name": "peerB", "from_session_id": "sidB",
                                   "from_harness": "claude", "kind": "handoff", "age_min": 2})
        b = Session(harness="claude", pid=2, cwd="/x/group2", slug="b", session_id="sidB",
                   liveness="stale", elapsed_min=100)
        return a, b

    def test_link_vanishes_when_target_group_folds(self):
        a, b = self._fixture()
        lines = render._build_lines([a, b], [], "fleet", False, 0, layout="wide",
                                    term_width=168)
        rows = _lines_text(lines)
        self.assertFalse(any("←" in t for t in rows))
        self.assertTrue(any("folded" in t for t in rows))

    def test_link_reappears_when_show_all_reveals_the_target(self):
        a, b = self._fixture()
        prev = render._SHOW_ALL
        render._SHOW_ALL = True
        try:
            lines = render._build_lines([a, b], [], "fleet", False, 0, layout="wide",
                                        term_width=168)
        finally:
            render._SHOW_ALL = prev
        rows = _lines_text(lines)
        self.assertTrue(any("←" in t and "peerB" in t for t in rows))

    def test_fold_effect_is_symmetric_under_group_order_reversal(self):
        a, b = self._fixture()
        lines = render._build_lines([b, a], [], "fleet", False, 0, layout="wide",
                                    term_width=168)
        rows = _lines_text(lines)
        self.assertFalse(any("←" in t for t in rows))
        self.assertTrue(any("folded" in t for t in rows))


class LedgerAbsentByteIdenticalTest(unittest.TestCase):
    """F-101g-(7) — with no connection-layer fields set at all (ledger absent), a
    fixed 3-session render must stay byte-identical to the pre-F-101 golden captured
    on `a1f16631` (see plan.md §5, procedure step 3). These goldens are never
    regenerated here; a mismatch is a real regression, not a stale fixture.
    Exception on record: 2026-09-04 the opencode row's source-absence message was
    corrected ("plan quota is console-only" → "opencode-go key not found") after the
    Go usage API shipped upstream (#31084/PR #2879) — the four r7 goldens were
    regenerated with the exact capture procedure above for that intentional change."""

    def _render(self, width):
        with mock.patch("time.time", return_value=1700000000.0):
            beta = Session(harness="codex", pid=1, cwd="/x/beta", slug="beta",
                           session_id="b1", liveness="working", elapsed_min=7)
            alpha = Session(harness="claude", pid=2, cwd="/x/alpha", slug="alpha",
                            session_id="a1", liveness="idle", elapsed_min=5)
            gamma = Session(harness="opencode", pid=3, cwd="/x/gamma", slug="gamma",
                            session_id="g1", liveness="done", elapsed_min=9)
            lines = render._build_lines([beta, alpha, gamma], [], "fleet", False, 0,
                                        layout="wide", term_width=width)
        return "\n".join(_lines_text(lines))

    def test_matches_committed_golden_at_every_captured_width(self):
        for width in (60, 100, 120, 168):
            with self.subTest(width=width):
                golden_path = os.path.join(_EVIDENCE_DIR,
                                           "fleet-render-r7-noledger-%d.txt" % width)
                with open(golden_path, encoding="utf-8") as fh:
                    golden = fh.read().rstrip("\n")
                self.assertEqual(self._render(width), golden)


if __name__ == "__main__":
    unittest.main()
