"""Focused F-38 display-only pressure and compaction-sequence checks."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import projection  # noqa: E402
from fleet.model import ContextEvidence, DispatchJob, Session  # noqa: E402


class ContextOrthogonalityTest(unittest.TestCase):
    def test_interactive_threshold_truth_table_is_display_only(self):
        projections = []
        for pct in (69, 70, 85):
            entity = Session(harness="codex", pid=pct, slug="s-%d" % pct)
            entity._context_evidence = ContextEvidence(used_pct=pct, source="codex",
                                                       sequence=(2,), source_head_sequence=(2,))
            projection.attach_projections([entity], [], now=100.0)
            projections.append((entity.work_projection.to_dict(), entity.context.to_dict()))
        self.assertEqual(projections[0][0], projections[1][0])
        self.assertEqual(projections[1][0], projections[2][0])
        self.assertEqual([row[1]["band"] for row in projections], ["normal", "tight", "critical"])

    def test_dispatch_drops_legacy_context_instead_of_projecting_unknown(self):
        job = DispatchJob(key="code", slug="worker")
        job._context_evidence = ContextEvidence(used_pct=85, source="legacy")
        projection.attach_projections([], [job], now=100.0)
        self.assertIsNone(job.context)
        self.assertIsNone(job._context_evidence)

    def test_newer_compaction_decrease_is_valid_but_sequence_regression_is_unknown(self):
        valid = ContextEvidence(used_pct=40, source="claude", sequence=(11,),
                                source_head_sequence=(11,))
        public, _ = projection.normalize_context(valid, now=100.0)
        self.assertEqual((public.used_pct, public.band), (40, "normal"))
        invalid = ContextEvidence(used_pct=40, source="claude", sequence=(9,),
                                  source_head_sequence=(11,))
        public, evidence = projection.normalize_context(invalid, now=100.0)
        self.assertEqual(public.band, "unknown")
        self.assertEqual(evidence.invalid_reason, "selected-sequence-before-source-head")

    def test_missing_stale_and_malformed_telemetry_are_unknown(self):
        for evidence in (
            ContextEvidence(),
            ContextEvidence(used_pct=101, source="codex"),
            ContextEvidence(used_pct=50, source="codex", observed_at=0, fresh_until=1),
        ):
            public, _ = projection.normalize_context(evidence, now=100)
            self.assertEqual(public.band, "unknown")
            self.assertIsNone(public.used_pct)


if __name__ == "__main__":
    unittest.main()


class F62LiveContextDoesNotExpireTest(unittest.TestCase):
    """F-62: elapsed time alone never blanks a live row's context."""

    def _aged(self, pct=33):
        # observed 1h ago with the collectors' 15-minute stamp: long past `fresh_until`.
        return ContextEvidence(used_pct=pct, source="claude-transcript",
                               observed_at=1000.0, fresh_until=1900.0)

    def test_live_row_keeps_the_last_observed_value(self):
        public, evidence = projection.normalize_context(self._aged(), now=4600.0, live=True)
        self.assertEqual(public.used_pct, 33)
        self.assertIsNone(evidence.invalid_reason)

    def test_non_live_row_still_expires(self):
        public, evidence = projection.normalize_context(self._aged(), now=4600.0, live=False)
        self.assertIsNone(public.used_pct)
        self.assertEqual(evidence.invalid_reason, "stale-context")

    def test_self_contradictory_stamp_is_rejected_even_when_live(self):
        """Observed AFTER its own expiry is a broken record, not an aged one."""
        broken = ContextEvidence(used_pct=33, source="claude-transcript",
                                 observed_at=2000.0, fresh_until=1900.0)
        public, evidence = projection.normalize_context(broken, now=2000.0, live=True)
        self.assertIsNone(public.used_pct)
        self.assertEqual(evidence.invalid_reason, "stale-context")

    def test_other_invalid_reasons_are_untouched_by_liveness(self):
        for evidence, reason in (
            (ContextEvidence(source="claude-transcript"), "missing-context"),
            (ContextEvidence(used_pct=101, source="claude-transcript"), "malformed-context"),
            (ContextEvidence(used_pct=40, source="claude-transcript", sequence=(9,),
                             source_head_sequence=(11,)), "selected-sequence-before-source-head"),
        ):
            with self.subTest(reason=reason):
                public, private = projection.normalize_context(evidence, now=4600.0, live=True)
                self.assertIsNone(public.used_pct)
                self.assertEqual(private.invalid_reason, reason)

    def test_idle_session_survives_a_quiet_transcript_end_to_end(self):
        """The exact reported shape: registry says idle, transcript went quiet 1h ago."""
        session = Session(harness="claude", pid=1, cwd="/x", liveness="idle", slug="s",
                          elapsed_min=1)
        session._context_evidence = self._aged(pct=34)
        projection.attach_projections([session], [], now=4600.0)
        self.assertEqual(session.context.used_pct, 34)
        self.assertEqual(session.context.band, "normal")

    def test_dead_session_context_still_drops(self):
        session = Session(harness="claude", pid=1, cwd="/x", liveness="dead", slug="s",
                          elapsed_min=1)
        session._context_evidence = self._aged()
        projection.attach_projections([session], [], now=4600.0)
        self.assertIsNone(session.context.used_pct)

    def test_live_states_cover_the_reported_vocabulary(self):
        for state in ("working", "idle", "blocked", "unused", "queued"):
            self.assertTrue(projection._is_live(Session(harness="claude", pid=1, cwd="/x",
                                                        liveness=state)))
        for state in ("stale", "dead", "done", "unknown", None):
            self.assertFalse(projection._is_live(Session(harness="claude", pid=1, cwd="/x",
                                                         liveness=state)))
