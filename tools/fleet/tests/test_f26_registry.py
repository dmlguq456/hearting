"""F-26 — registry first-class + `unused` visibility (PRD v8 §4.8).

Hermetic: the registry is written into a temp CLAUDE_CONFIG_DIR, never read from the real
~/.claude. The live acceptance against the actual ghost session is a separate, measured
verification step (see dev_logs/step_02_f26_registry.md) — not a unit test, because it
depends on a process this suite must never depend on or touch.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import model, render                                  # noqa: E402
from fleet.collectors import claude as claude_collector          # noqa: E402
from fleet.collectors import liveness                            # noqa: E402
from fleet.model import Session                                  # noqa: E402

def _flatten(lines):
    """Segment-lines → plain text (None = blank line)."""
    return "\n".join("" if ln is None else "".join(t for t, _k in ln) for ln in lines)


# The real ghost, captured 2026-07-15 — the row F-26 exists to surface.
GHOST = {
    "pid": 1168514, "sessionId": "00000000-0000-4000-8000-000000000003",
    "cwd": "/home/alice/agent_setting", "startedAt": 1784083189482,
    "procStart": "3918896", "version": "2.1.210", "peerProtocol": 1,
    "kind": "interactive", "entrypoint": "cli", "name": "agent-setting-17",
    "nameSource": "derived", "status": "idle", "updatedAt": 1784083189601,
    "statusUpdatedAt": 1784083189601,
}


class RegistryReadTest(unittest.TestCase):
    """read_registry() is a first-class contract, and it tolerates partial rows."""

    def setUp(self):
        model.reset_state_tracker()
        self.home = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.home, "sessions"))

    def _write(self, pid, payload):
        with open(os.path.join(self.home, "sessions", "%d.json" % pid), "w") as f:
            json.dump(payload, f)

    def test_reads_every_tier1_field(self):
        self._write(1168514, GHOST)
        d = claude_collector.read_registry(1168514, self.home)
        self.assertEqual(d["name"], "agent-setting-17")
        s = Session(harness="claude", pid=1168514)
        claude_collector._apply_registry(s, d)
        self.assertEqual(s.session_id, "00000000-0000-4000-8000-000000000003")
        self.assertEqual(s.registry_name, "agent-setting-17")
        self.assertEqual(s.slug, "agent-setting-17")      # no regression: slug still set
        self.assertEqual(s.status, "idle")
        self.assertEqual(s.kind, "interactive")
        self.assertEqual(s.registry_proc_start, "3918896")
        self.assertAlmostEqual(s.started_at, 1784083189.482, places=3)
        self.assertAlmostEqual(s.updated_at, 1784083189.601, places=3)

    def test_missing_file_is_silence_not_a_crash(self):
        self.assertIsNone(claude_collector.read_registry(999999, self.home))

    def test_corrupt_file_is_silence_not_a_crash(self):
        path = os.path.join(self.home, "sessions", "123.json")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertIsNone(claude_collector.read_registry(123, self.home))

    def test_fresh_row_without_status_or_updatedat_tolerates(self):
        """A session file written milliseconds ago may carry neither key."""
        self._write(555, {"pid": 555, "sessionId": "abc", "cwd": "/x", "startedAt": 1784083189482})
        d = claude_collector.read_registry(555, self.home)
        s = Session(harness="claude", pid=555)
        claude_collector._apply_registry(s, d)          # must not raise
        self.assertIsNone(s.status)
        self.assertIsNone(s.updated_at)
        self.assertEqual(s.session_id, "abc")
        self.assertAlmostEqual(s.started_at, 1784083189.482, places=3)

    def test_non_numeric_timestamps_degrade_to_none(self):
        self._write(556, dict(GHOST, pid=556, startedAt="nope", updatedAt=None))
        s = Session(harness="claude", pid=556)
        claude_collector._apply_registry(s, claude_collector.read_registry(556, self.home))
        self.assertIsNone(s.started_at)
        self.assertIsNone(s.updated_at)


class GhostClassificationTest(unittest.TestCase):
    """The ghost's registry shape → `unused`, end to end through liveness.classify()."""

    def setUp(self):
        model.reset_state_tracker()

    def _ghost_session(self, **over):
        s = Session(harness="claude", pid=1168514, cwd="/home/alice/agent_setting")
        s.status = "idle"
        s.registry_name = "agent-setting-17"
        s.slug = "agent-setting-17"
        s.started_at = 1784083189.482
        s.updated_at = 1784083189.601
        s.proc_start = "3918896"
        s.registry_proc_start = "3918896"
        s.mtime = 1784083189.601
        s._has_transcript = False
        for k, v in over.items():
            setattr(s, k, v)
        return s

    def _classify(self, s, now=1784083200.0):
        # Pin process liveness: this suite must never depend on a real pid being alive.
        real = liveness._alive
        liveness._alive = lambda pid: True
        try:
            return liveness.classify(s, now)
        finally:
            liveness._alive = real

    def test_ghost_is_unused_at_tier_1(self):
        s = self._ghost_session()
        self.assertEqual(self._classify(s), "unused")
        self.assertEqual(s.state_evidence["tier"], 1)
        self.assertFalse(s.state_evidence["derived"])
        self.assertEqual(s.state_evidence["raw_status"], "idle")   # registry claim preserved
        self.assertLess(s.state_evidence["inputs"]["activity_ms"], model.UNUSED_ACTIVITY_MS)

    def test_a_prompted_session_with_the_same_status_stays_idle(self):
        """The control: same registry status, but it was actually used → plain idle."""
        s = self._ghost_session(updated_at=1784083250.0, _has_transcript=True,
                                mtime=1784083250.0)
        self.assertEqual(self._classify(s, now=1784083260.0), "idle")

    def test_transcript_presence_alone_blocks_unused(self):
        s = self._ghost_session(_has_transcript=True)
        self.assertEqual(self._classify(s), "idle")

    def test_activity_window_over_threshold_blocks_unused(self):
        s = self._ghost_session(updated_at=1784083189.482 + 5.0)   # 5s of activity
        self.assertEqual(self._classify(s), "idle")

    def test_old_ghost_stays_unused_past_the_stale_window(self):
        """User decision 2026-07-15: an unused ghost's mtime is frozen at spawn, so the
        stale window would auto-hide it after 48h — exactly the row F-26 exists to show.
        Alive ghost stays `unused` regardless of age; death still ends it (tier 2)."""
        three_days = 3 * 24 * 3600.0
        s = self._ghost_session()
        self.assertEqual(self._classify(s, now=1784083189.601 + three_days), "unused")
        self.assertEqual(s.state_evidence["tier"], 1)

    def test_old_prompted_session_still_goes_stale(self):
        """The exemption is unused-only — a USED session silent past the window keeps
        the pre-F-25 ordering (status never rescues a 48h-silent row)."""
        three_days = 3 * 24 * 3600.0
        s = self._ghost_session(updated_at=1784083250.0, _has_transcript=True,
                                mtime=1784083250.0)
        self.assertEqual(self._classify(s, now=1784083250.0 + three_days), "stale")


class UnusedGlyphContractTest(unittest.TestCase):
    """The glyph table's own contract: readable WITHOUT color."""

    def test_unused_glyph_is_registered(self):
        self.assertEqual(render._LIVE_GLYPH["unused"], "◌")
        self.assertEqual(render._GLYPH_KEY["unused"], "g_unused")

    def test_unused_does_not_collide_with_detached(self):
        """○ is owned by detached (attach axis); unused is the activity-history axis. If they
        shared a glyph, only color would separate them — which the table forbids."""
        self.assertNotEqual(render._LIVE_GLYPH["unused"], render._DETACHED_GLYPH)

    def test_every_live_glyph_is_distinct_from_unused(self):
        for state, g in render._LIVE_GLYPH.items():
            if state in ("unused", "working", "idle"):
                continue      # working/idle share ● by design (separated by color+spinner)
            self.assertNotEqual(g, render._LIVE_GLYPH["unused"], "glyph collision: %s" % state)

    def test_unused_glyph_is_one_cell(self):
        """A 2-cell glyph would shift every column on the row."""
        self.assertEqual(render._cw(render._LIVE_GLYPH["unused"]), 1)

    def test_unused_has_its_own_color_key(self):
        """Distinct from idle (dim green) and stale (colorless dim)."""
        self.assertIn("g_unused", render._HUE_OF)
        self.assertNotEqual(render._HUE_OF["g_unused"], render._HUE_OF["g_work_off"])
        self.assertNotEqual(render._HUE_OF["g_unused"], render._HUE_OF["g_stale"])

    def test_glyph_stays_dim_so_the_ink_gradient_survives(self):
        """The badge brightened, the glyph must not: ● > ○ > ◌ is an ink-WEIGHT gradient,
        so the unused glyph has to stay the lightest mark in the table."""
        self.assertEqual(render._GLYPH_KEY["unused"], "g_unused")
        self.assertEqual(render._HUE_OF["g_unused"][1], render._A_DIM)


class NameChainTest(unittest.TestCase):
    """F-26: the ghost must render NAMED, never as an anonymous row."""

    def test_registry_name_is_used_when_there_is_no_title(self):
        s = Session(harness="claude", pid=1, cwd="/home/u/proj", slug="proj")
        s.registry_name = "agent-setting-17"
        self.assertEqual(render._session_name(s), "agent-setting-17")

    def test_title_still_outranks_registry_name(self):
        s = Session(harness="claude", pid=1, cwd="/home/u/proj", slug="proj",
                    title="Real work title")
        s.registry_name = "agent-setting-17"
        self.assertEqual(render._session_name(s), "Real work title")

    def test_slug_remains_the_last_resort(self):
        s = Session(harness="claude", pid=1, cwd="/home/u/proj", slug="proj")
        self.assertEqual(render._session_name(s), "proj")

    def test_cwd_basename_when_even_slug_is_absent(self):
        s = Session(harness="claude", pid=1, cwd="/home/u/proj")
        self.assertEqual(render._session_name(s), "proj")


class UnusedRowRenderTest(unittest.TestCase):
    """Row assembly — badge present, name intact, no width blowup."""

    def _row(self, name_width=None, **over):
        s = Session(harness="claude", pid=1168514, cwd="/home/alice/agent_setting",
                    slug="agent-setting-17", liveness="unused", elapsed_min=225)
        s.registry_name = "agent-setting-17"
        for k, v in over.items():
            setattr(s, k, v)
        return "".join(t for t, _k in render._session_row(s, narrow=False,
                                                          name_width=name_width))

    def test_badge_shows_state_and_age(self):
        txt = self._row(name_width=40)
        self.assertIn("agent-setting-17", txt)
        self.assertIn("unused", txt)
        self.assertIn("3h 45m", txt)             # 225 min

    def test_glyph_is_the_unused_glyph(self):
        self.assertIn("◌", self._row(name_width=40))

    def test_provenance_tag_renders_when_present(self):
        self.assertIn("terminal", self._row(name_width=60, provenance="terminal"))

    def test_absent_provenance_renders_nothing(self):
        """Misattribution is worse than absence — no placeholder, no empty tag."""
        txt = self._row(name_width=40, provenance=None)
        self.assertNotIn("None", txt)

    def test_idle_row_has_no_unused_badge(self):
        txt = self._row(name_width=40, liveness="idle")
        self.assertNotIn("unused", txt)

    def test_two_line_row_keeps_badge_parity(self):
        s = Session(harness="claude", pid=1168514, cwd="/home/alice/agent_setting",
                    slug="agent-setting-17", liveness="unused", elapsed_min=225)
        s.registry_name = "agent-setting-17"
        l1, _l2 = render._session_row_2line(s, term_width=120)
        txt = "".join(t for t, _k in l1)
        self.assertIn("agent-setting-17", txt)
        self.assertIn("unused", txt)
        self.assertIn("◌", txt)

    def test_narrow_row_drops_provenance_rather_than_starving_the_name(self):
        """A readable name outranks knowing who launched it."""
        s = Session(harness="claude", pid=1168514, cwd="/home/alice/agent_setting",
                    slug="agent-setting-17", liveness="unused", elapsed_min=225)
        s.registry_name = "agent-setting-17"
        s.provenance = "terminal"
        l1, _l2 = render._session_row_2line(s, term_width=60)
        txt = "".join(t for t, _k in l1)
        self.assertNotIn("terminal", txt)        # dropped
        self.assertIn("unused", txt)             # the F-26 signal survives


class DegradationLadderTest(unittest.TestCase):
    """The F-22 40-cell cap makes the name zone genuinely tight at EVERY width, so what
    yields first is a real design contract, not an edge case (design_critic_step2 §2):
        provenance → badge age → name.
    The name yields last because F-26 exists to remove anonymous rows (prd.md:247).
    """

    def _ghost(self, **over):
        s = Session(harness="claude", pid=1168514, cwd="/home/alice/agent_setting",
                    slug="agent-setting-17", liveness="unused", elapsed_min=225)
        s.registry_name = "agent-setting-17"
        s.provenance = "terminal"
        for k, v in over.items():
            setattr(s, k, v)
        return s

    def _wide(self, term_width):
        return "".join(t for t, _k in render._session_row(
            self._ghost(), narrow=False,
            name_width=render._wide_name_width(term_width)))

    def test_at_168_the_name_and_badge_survive_and_provenance_yields(self):
        """The capped 40-cell zone cannot hold name+badge+provenance; the tag goes."""
        name = "agent-setting-17-x"          # 18 cells — just past the provenance-fits window
        s = self._ghost(registry_name=name)
        txt = "".join(t for t, _k in render._session_row(
            s, narrow=False, name_width=render._wide_name_width(168)))
        self.assertIn(name, txt)                     # full name — never anonymous
        self.assertIn("unused 3h 45m", txt)         # full badge — prd.md:248 shape
        self.assertNotIn("terminal", txt)           # provenance yielded first

    def test_badge_age_yields_before_the_name(self):
        """A name sized into the window where shedding the age is exactly what saves it.

        Full badge ` unused 3h 45m` (14 since F-76 spaced the units) plus the 1-cell gap
        leaves the name `zone - 15` cells;
        the compact ` unused` (7) leaves it `zone - 8`. A name in between therefore has to clip
        with the age present, and survives whole once the age yields. The sample is SIZED FROM
        the zone rather than typed in, so every 168-col zone change moves it automatically
        instead of silently leaving the window (F-54's `_HMW` widenings shrank the zone
        40 → 36 → 32; F-57's reservation reclaim put it back at the 40-cell cap). The source
        literal must stay at least `_NAME_WIDE_MAX - 8` cells long for the slice to bite.
        """
        zone = render._wide_name_width(168)
        name = "agent-setting-17-beta1234567890abcdef"[:zone - 8]  # top of (zone-14, zone-8]
        self.assertEqual(render._dw(name), zone - 8)
        s = self._ghost(registry_name=name, provenance=None)
        segs = render._session_row(s, narrow=False,
                                   name_width=render._wide_name_width(168))
        # The badge has its own color key (g_unused_b); the glyph keeps the dim g_unused.
        badge = next(t for t, k in segs if k == "g_unused_b")
        self.assertEqual(badge, " unused")           # age shed from the BADGE...
        txt = "".join(t for t, _k in segs)
        self.assertIn(name, txt)                     # ...so the name stays whole
        self.assertIn("3h 45m", txt)                 # age still on the row, in the time cell

    def test_full_badge_is_kept_when_the_name_already_fits(self):
        """The ladder only sheds under pressure — a short name keeps prd.md:248's shape."""
        s = self._ghost(registry_name="short", provenance=None)
        segs = render._session_row(s, narrow=False,
                                   name_width=render._wide_name_width(168))
        badge = next(t for t, k in segs if k == "g_unused_b")
        self.assertEqual(badge, " unused 3h 45m")

    def test_name_clips_only_when_nothing_else_is_left_to_shed(self):
        s = self._ghost(registry_name="z" * 90, provenance=None)
        txt = "".join(t for t, _k in render._session_row(
            s, narrow=False, name_width=render._wide_name_width(168)))
        self.assertIn("…", txt)                     # last resort
        self.assertIn("unused", txt)                # the F-26 signal still survives

    def test_wide_row_keeps_one_fixed_session_branch_column(self):
        """The integrated title + ``(branch)`` cell keeps its fixed combined width."""
        for name in ("short", "agent-setting-17", "q" * 200):
            s = self._ghost(registry_name=name, branch="main")
            nw = render._wide_name_width(168)
            segs = render._session_row(s, narrow=False, name_width=nw)
            # Skip past the prefix (gap, glyph, gap) + the harness(+model) field — F-33 (v11)
            # guarantees that field sums to EXACTLY _HMW cells, but it may now be split across
            # several segments (harness text, optional "(model · effort)" parenthetical,
            # padding) instead of the old single padded segment.
            i, consumed = 3, 0
            while consumed < render._HMW:
                consumed += render._dw(segs[i][0])
                i += 1
            zone = 0
            target = nw + render._BRANCH_SUFFIX_W
            for t, _k in segs[i:]:
                zone += render._dw(t)
                if zone >= target:
                    break
            self.assertEqual(zone, target, "session (branch) column drifted for %r" % name[:20])

    def test_branch_is_an_integrated_parenthesized_suffix(self):
        for name in ("agent-setting-17", "x" * 60, "Stage-dispatch v10 diagnosis and spec"):
            s = self._ghost(registry_name=name, branch="main")
            segs = render._session_row(s, narrow=False,
                                       name_width=render._wide_name_width(168))
            txt = "".join(t for t, _k in segs)
            self.assertIn("(main)", txt)
            branch_i = next(i for i, (_t, k) in enumerate(segs) if k == "branch_s")
            self.assertEqual(segs[branch_i - 1][0], " (")
            self.assertEqual(segs[branch_i + 1][0], ")")


class UnusedVisibilityTest(unittest.TestCase):
    """F-26's whole point: `unused` must be visible WITHOUT --all. Hiding it would
    reproduce the exact 'not noticeable anywhere' failure the feature removes."""

    def setUp(self):
        model.reset_state_tracker()
        self._prev = render._SHOW_ALL
        render.set_show_all(False)

    def tearDown(self):
        render.set_show_all(self._prev)

    def _sessions(self):
        live = Session(harness="claude", pid=2, cwd="/home/u/proj", slug="proj",
                       liveness="working", title="Live work")
        ghost = Session(harness="claude", pid=1168514, cwd="/home/u/proj",
                        slug="agent-setting-17", liveness="unused", elapsed_min=225)
        ghost.registry_name = "agent-setting-17"
        return [live, ghost]

    def _render(self, width=168):
        return _flatten(render._build_lines(self._sessions(), [], section="fleet",
                                            narrow=False, malformed=0, term_width=width))

    def test_unused_row_is_visible_by_default(self):
        self.assertIn("agent-setting-17", self._render())

    def test_pulse_counts_unused(self):
        self.assertIn("1 unused", self._render())

    def test_legend_shows_unused_when_present(self):
        out = self._render()
        self.assertIn("◌", out)
        self.assertIn("unused", out)

    def test_unused_and_detached_glyphs_are_distinguishable_side_by_side(self):
        """design_critic_step2 §4 — the live captures never had a detached session, so ◌ next
        to ○ (their one genuinely risky adjacency, since both are rings) went unverified.
        Force the case: both must appear, each labelled, in pulse and legend."""
        live = Session(harness="claude", pid=2, cwd="/home/u/proj", slug="proj",
                       liveness="working", title="Live work")
        ghost = Session(harness="claude", pid=1168514, cwd="/home/u/proj",
                        slug="agent-setting-17", liveness="unused", elapsed_min=225)
        ghost.registry_name = "agent-setting-17"
        det = Session(harness="codex", pid=3, cwd="/home/u/proj", slug="det",
                      liveness="idle", detached=True, title="Detached one")
        out = _flatten(render._build_lines([live, ghost, det], [], section="fleet",
                                           narrow=False, malformed=0, term_width=168))
        self.assertIn("◌", out)
        self.assertIn("○", out)
        self.assertNotEqual(render._LIVE_GLYPH["unused"], render._DETACHED_GLYPH)
        # Neither glyph is ever asked to carry the meaning alone — each has its word beside it.
        self.assertIn("1 unused", out)
        self.assertIn("1 detached", out)
        pulse = next(l for l in out.splitlines() if "unused" in l and "detached" in l)
        self.assertIn("◌ 1 unused", pulse)
        self.assertIn("○ 1 detached", pulse)

    def test_legend_stays_quiet_when_no_unused_row(self):
        """F-12: a healthy board says nothing about states it does not have."""
        live = Session(harness="claude", pid=2, cwd="/home/u/proj", slug="proj",
                       liveness="working", title="Live work")
        out = _flatten(render._build_lines([live], [], section="fleet", narrow=False,
                                           malformed=0, term_width=168))
        self.assertNotIn("unused", out)


if __name__ == "__main__":
    unittest.main()
