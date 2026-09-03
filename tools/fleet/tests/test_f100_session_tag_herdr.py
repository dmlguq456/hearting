#!/usr/bin/env python3
"""F-100 (user 2026-09-03) — session-tag chip and herdr chip on depth-0 rows.

  a) The derived `<basename>-<xx>` name's 2-hex tag rides a reverse chip between the
     status glyph and the harness text, INSIDE the harness field's measured slack, so
     the title owns the whole name zone and no other column ledger moves. The tag is
     snapshot once per session so a later user rename keeps it.
  b) The context row's lead slot names WHERE the session runs: ` herdr ` reversed when
     `herdr agent list` names its session id, dim `tty` when herdr answered and it does
     not, blank when there is no evidence. The `working`/`idle` word that lived there
     duplicated the L1 glyph and is gone.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render, titles                                  # noqa: E402
from fleet.model import Session                                   # noqa: E402
from fleet.session_handle import derived_tag                      # noqa: E402
from fleet.collectors import herdr, procscan                      # noqa: E402
from fleet.collectors import claude as claude_collector           # noqa: E402


def _text(segs):
    return "".join(t for t, _k in segs)


class DerivedTagTest(unittest.TestCase):
    def test_measured_shapes(self):
        # Measured 2026-09-03 in ~/.claude/sessions/*.json (nameSource=derived).
        for name, tag in (("hearting-46", "46"), ("hearting-d5", "d5"), ("cairn-47", "47"),
                          ("claude-cf", "cf"), ("my-repo-name-0a", "0a")):
            with self.subTest(name=name):
                self.assertEqual(derived_tag(name), tag)

    def test_non_derived_shapes_yield_none(self):
        for name in (None, "", "hearting", "hearting-4", "hearting-46x", "hearting-FB",
                     "hearting-4g", "-46", 46):
            with self.subTest(name=name):
                self.assertIsNone(derived_tag(name))

    def test_shape_alone_is_not_provenance(self):
        """A user-set name can look derived; the caller gates on nameSource, not on this."""
        self.assertEqual(derived_tag("release-1a"), "1a")


class TagChipLedgerTest(unittest.TestCase):
    def _s(self, **over):
        base = dict(harness="claude", pid=1, cwd="/x", slug="s", title="a title",
                    liveness="idle", elapsed_min=1)
        base.update(over)
        return Session(**base)

    def test_chip_is_exactly_tag_w_cells_tagged_or_not(self):
        self.assertEqual(render._session_tag_chip(self._s(session_tag="46")),
                         [("[", "dim"), ("46", "tag"), ("]", "dim"), (" ", None)])
        self.assertEqual(render._session_tag_chip(self._s()), [(" " * render._TAG_W, None)])
        for s in (self._s(session_tag="46"), self._s(), self._s(session_tag="abcd")):
            self.assertEqual(sum(render._dw(t) for t, _k in render._session_tag_chip(s)),
                             render._TAG_W)

    def test_badge_is_soft_white_on_every_harness_and_dims_with_the_row(self):
        """User 2026-09-03: white, not the harness hue, so the badge separates from the
        harness text; no reverse video anywhere on the F-100 surfaces."""
        for harness in ("claude", "codex", "opencode", "zzz"):
            with self.subTest(harness=harness):
                self.assertEqual(render._session_tag_chip(self._s(harness=harness, session_tag="0b")),
                                 [("[", "dim"), ("0b", "tag"), ("]", "dim"), (" ", None)])
        self.assertEqual(render._session_tag_chip(self._s(session_tag="0b"), dim=True),
                         [("[", "dim"), ("0b", "tag_dim"), ("]", "dim"), (" ", None)])
        self.assertEqual(render._HUE_OF["tag"], ("w", 0))
        self.assertEqual(render._HUE_OF["herdr_on"], ("w", 0))
        for key in ("tag", "tag_dim", "herdr_on"):
            with self.subTest(key=key):
                self.assertFalse(render._HUE_OF[key][1] & render._A_REVERSE)
                self.assertFalse(render._HUE_OF[key][1] & render._A_BOLD)
        self.assertNotEqual(render._HUE_OF["tag"][0], render._NAME_HUE["claude"])

    def test_wide_row_charges_the_chip_inside_the_harness_field(self):
        """Segments after the 3-cell prefix still sum to EXACTLY _HMW (F-33's contract),
        so _NAME_COL and everything right of it are untouched — with or without a tag."""
        for s in (self._s(session_tag="46"), self._s(), self._s(harness="opencode",
                                                             model="claude-sonnet-4-5",
                                                             effort="high", session_tag="9e")):
            with self.subTest(harness=s.harness, tag=s.session_tag):
                segs = render._session_row(s, narrow=False,
                                           name_width=render._wide_name_width(168))
                i, consumed = 3, 0
                while consumed < render._HMW:
                    consumed += render._dw(segs[i][0])
                    i += 1
                self.assertEqual(consumed, render._HMW)
                self.assertEqual(sum(render._dw(t) for t, _k in segs[:i]), render._NAME_COL)
                self.assertEqual(_text(segs).index("a title"), render._NAME_COL)
                if s.session_tag:
                    self.assertEqual(segs[3:7], [("[", "dim"), (s.session_tag, "tag"),
                                                 ("]", "dim"), (" ", None)])
                    self.assertIn("[%s] claude code" % s.session_tag if s.harness == "claude"
                                  else "[%s] opencode" % s.session_tag, _text(segs))
                else:
                    self.assertEqual(segs[3], (" " * render._TAG_W, None))

    def test_wide_row_chip_sits_between_the_glyph_and_the_harness_text(self):
        segs = render._session_row(self._s(session_tag="46", liveness="idle"), narrow=False,
                                   name_width=render._wide_name_width(168))
        txt = _text(segs)
        self.assertLess(txt.index(render._LIVE_GLYPH["idle"]), txt.index("[46]"))
        self.assertLess(txt.index("[46]"), txt.index("claude code"))

    def test_narrow_row_keeps_the_name_column_and_one_gap_after_claude_code(self):
        tagged, _ = render._session_row_2line(self._s(session_tag="46"), term_width=100)
        bare, _ = render._session_row_2line(self._s(), term_width=100)
        for l1 in (tagged, bare):
            prefix = l1[: l1.index(next(seg for seg in l1 if seg[1] in render.NAME_KEYS))]
            self.assertEqual(sum(render._dw(t) for t, _k in prefix), 4 + render._HW)
        self.assertIn(("46", "tag"), tagged)
        # narrow: 11 cells remain for the harness badge, so `claude code` falls back to its
        # first word with the guaranteed blank last cell — the same shape the narrow
        # dispatch rows already draw — and the badge keeps its own gap cell.
        self.assertIn("[46] claude", _text(tagged))
        self.assertIn((render._badge_cell("claude code", render._HW - render._TAG_W), "hb_claude"),
                      tagged)
        self.assertNotIn("]claude", _text(tagged))
        self.assertNotIn("claudea", _text(tagged))

    def test_dim_rows_use_the_dim_chip(self):
        for over in (dict(liveness="stale"), dict(detached=True), dict(app_server=True)):
            with self.subTest(over=over):
                segs = render._session_row(self._s(session_tag="46", **over), narrow=False,
                                           name_width=render._wide_name_width(168))
                self.assertEqual(segs[4], ("46", "tag_dim"))
                l1, _ = render._session_row_2line(self._s(session_tag="46", **over),
                                                  term_width=100)
                self.assertIn(("46", "tag_dim"), l1)


class LegendTest(unittest.TestCase):
    def _legend(self, **over):
        base = dict(harness="claude", pid=1, cwd="/x", slug="s", liveness="idle",
                    ctx_pct=10, elapsed_min=1)
        base.update(over)
        lines = render._build_lines([Session(**base)], [], "fleet", False, 0,
                                    layout="wide", term_width=168)
        return _text([ln for ln in lines if ln][-1])

    def test_entries_appear_only_when_seen(self):
        plain = self._legend()
        self.assertNotIn("[id]", plain)
        self.assertNotIn("herdr", plain)
        self.assertNotIn("tty", plain)
        self.assertIn("[id]", self._legend(session_tag="46"))
        self.assertIn("herdr pane", self._legend(herdr_attached=True))
        self.assertIn("tty", self._legend(herdr_attached=False))


class HerdrCollectorTest(unittest.TestCase):
    _PAYLOAD = {"id": "cli:agent:list", "result": {"agents": [
        {"agent": "claude", "agent_session": {"agent": "claude", "kind": "id",
                                             "source": "herdr:claude", "value": "sid-claude-1"},
         "agent_status": "working", "pane_id": "w1:pP", "focused": True},
        {"agent": "codex", "agent_session": {"agent": "codex", "kind": "id",
                                            "source": "herdr:codex", "value": "thread-9"},
         "agent_status": "idle"},
        "not-a-dict",
        {"agent": "claude"},                                   # no agent_session
    ]}, "type": "agent_list"}

    def _runner(self, stdout=None, returncode=0, raise_exc=None):
        def run(argv, **kw):
            self.assertEqual(argv, ["herdr", "agent", "list"])
            if raise_exc:
                raise raise_exc
            return SimpleNamespace(returncode=returncode,
                                   stdout=json.dumps(self._PAYLOAD) if stdout is None else stdout)
        return run

    def test_list_agents_parses_the_measured_shape(self):
        agents = herdr.list_agents(runner=self._runner(), which=lambda _n: "/x/herdr")
        self.assertEqual(len(agents), 3)
        index = herdr.attached_index(agents)
        self.assertEqual(set(index), {("claude", "sid-claude-1"), ("codex", "thread-9")})

    def test_every_failure_path_is_none(self):
        which = lambda _n: "/x/herdr"  # noqa: E731
        self.assertIsNone(herdr.list_agents(runner=self._runner(), which=lambda _n: None))
        self.assertIsNone(herdr.list_agents(runner=self._runner(returncode=2), which=which))
        self.assertIsNone(herdr.list_agents(runner=self._runner(stdout="nope"), which=which))
        self.assertIsNone(herdr.list_agents(runner=self._runner(stdout='{"result": {}}'),
                                            which=which))
        self.assertIsNone(herdr.list_agents(runner=self._runner(raise_exc=OSError("x")),
                                            which=which))

    def _sessions(self):
        return [
            Session(harness="claude", pid=1, session_id="sid-claude-1"),
            Session(harness="claude", pid=2, session_id="sid-claude-2"),
            Session(harness="codex", pid=3, session_id="thread-9"),
            Session(harness="codex", pid=4, session_id="thread-other"),
            Session(harness="claude", pid=5),                                  # no sid
            Session(harness="claude", pid=6, session_id="sid-claude-1", is_child=True),
            Session(harness="codex", pid=7, session_id="thread-9", app_server=True),
            Session(harness="claude", pid=8, session_id="sid-claude-1", mem_worker=True),
        ]

    def test_verdicts(self):
        sessions = self._sessions()
        herdr.enrich(sessions, agents=self._PAYLOAD["result"]["agents"])
        self.assertEqual([s.herdr_attached for s in sessions],
                         [True, False, True, False, None, None, None, None])

    def test_absent_herdr_falls_back_to_lineage_and_only_ever_promotes(self):
        sessions = self._sessions()[:4]
        lineage = {1: "herdr", 2: "terminal", 3: None, 4: "vscode"}.get
        real_list = herdr.list_agents
        herdr.list_agents = lambda *a, **k: None
        try:
            herdr.enrich(sessions, lineage=lineage)
        finally:
            herdr.list_agents = real_list
        self.assertEqual([s.herdr_attached for s in sessions], [True, None, None, None])


class ProvenanceWalkTest(unittest.TestCase):
    """The lineage walk sees herdr past the shell it starts the harness through."""

    def _walk(self, chain, environ=None):
        # chain: [(pid, comm, ppid), ...] rooted at the harness pid
        ppid = {pid: parent for pid, _comm, parent in chain}
        comm = {pid: c for pid, c, _parent in chain}
        saved = (procscan._ppid_of, procscan._comm_of, procscan.read_environ)
        procscan._ppid_of = lambda pid: ppid.get(pid)
        procscan._comm_of = lambda pid: comm.get(pid)
        procscan.read_environ = lambda pid: dict(environ or {})
        try:
            return procscan.provenance(chain[0][0])
        finally:
            procscan._ppid_of, procscan._comm_of, procscan.read_environ = saved

    def test_herdr_behind_a_shell_is_herdr(self):
        # Measured 2026-09-03: claude(2758089) -> zsh(170271) -> herdr(680889) -> 1
        self.assertEqual(self._walk([(10, "claude", 11), (11, "zsh", 12), (12, "herdr", 1)]),
                         "herdr")

    def test_plain_terminal_chain_is_still_terminal(self):
        self.assertEqual(self._walk([(10, "claude", 11), (11, "zsh", 12), (12, "tmux: server", 13),
                                     (13, "sshd", 1)]), "terminal")

    def test_unrecognised_chain_is_none_and_worker_env_wins(self):
        self.assertIsNone(self._walk([(10, "claude", 11), (11, "python3", 1)]))
        self.assertEqual(self._walk([(10, "claude", 11), (11, "zsh", 12), (12, "herdr", 1)],
                                    environ={"AGENT_SESSION_ROLE": "worker"}), "worker")


class TagPersistenceTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLEET_TITLE_STATE_DIR"] = os.path.join(self.tmp.name, "titles")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def test_tags_live_beside_the_titles_root_not_under_it(self):
        self.assertEqual(os.path.dirname(titles.tags_dir("claude")),
                         os.path.join(self.tmp.name, "tags"))
        self.assertFalse(titles.tags_dir("claude").startswith(titles.state_root()))

    def test_remember_once_then_read(self):
        self.assertIsNone(titles.read_tag("sid-1"))
        self.assertTrue(titles.remember_tag("sid-1", "46"))
        self.assertEqual(titles.read_tag("sid-1"), "46")
        self.assertFalse(titles.remember_tag("sid-1", "46"))      # steady state: no write
        self.assertFalse(titles.remember_tag("sid-1", ""))
        self.assertFalse(titles.remember_tag("", "46"))

    def test_title_sweep_leaves_fresh_tags_and_reaps_thirty_day_old_ones(self):
        titles.remember_tag("sid-fresh", "aa")
        titles.remember_tag("sid-old", "bb")
        old = time.time() - 31 * 24 * 3600
        os.utime(titles.tag_path("sid-old"), (old, old))
        titles.sweep()
        self.assertEqual(titles.read_tag("sid-fresh"), "aa")
        self.assertIsNone(titles.read_tag("sid-old"))


class ClaudeCollectorTagTest(unittest.TestCase):
    """The collector reads the tag while the runtime record says `derived`, snapshots it,
    and falls back to the snapshot once a rename has replaced the record's name."""

    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        for key in ("CLAUDE_CONFIG_DIR", "HOME", "AGENT_HOME"):
            os.environ[key] = self.tmp.name
        os.environ["FLEET_TITLE_STATE_DIR"] = os.path.join(self.tmp.name, "titles")
        for key in ("CLAUDE_HOME", "AGENT_DISPATCH_JOBS", "XDG_STATE_HOME", "HARNESS_STATE_ROOT"):
            os.environ.pop(key, None)
        os.makedirs(os.path.join(self.tmp.name, "sessions"))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _record(self, pid, name, source):
        with open(os.path.join(self.tmp.name, "sessions", "%d.json" % pid), "w") as fh:
            json.dump({"sessionId": "sid-100", "name": name, "nameSource": source}, fh)

    def test_apply_registry_reads_the_tag_only_from_a_derived_name(self):
        derived = Session(harness="claude", pid=1)
        claude_collector._apply_registry(derived, {"sessionId": "s", "name": "hearting-46",
                                                   "nameSource": "derived"})
        self.assertEqual(derived.session_tag, "46")
        self.assertIsNone(derived.runtime_name)
        user = Session(harness="claude", pid=1)
        claude_collector._apply_registry(user, {"sessionId": "s", "name": "release-1a",
                                                "nameSource": "user"})
        self.assertIsNone(user.session_tag)
        self.assertEqual(user.runtime_name, "release-1a")

    def test_enrich_snapshots_then_survives_a_rename(self):
        pid = os.getpid()
        self._record(pid, "hearting-46", "derived")
        first = Session(harness="claude", pid=pid, cwd=self.tmp.name)
        claude_collector.enrich(first)
        self.assertEqual(first.session_tag, "46")
        self.assertEqual(titles.read_tag("sid-100"), "46")
        self._record(pid, "my thread", "user")
        renamed = Session(harness="claude", pid=pid, cwd=self.tmp.name)
        claude_collector.enrich(renamed)
        self.assertEqual(renamed.runtime_name, "my thread")
        self.assertEqual(renamed.session_tag, "46")


if __name__ == "__main__":
    unittest.main()
