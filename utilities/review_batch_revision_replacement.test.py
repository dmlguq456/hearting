#!/usr/bin/env python3
"""One semantic slice of the review batch revision lifecycle."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from review_batch_revision_fixture import RevisionBatchFixture, AdmissionSelectionFixture

class RevisionBatchCLI(RevisionBatchFixture):
    def test_death_partial_replacement_then_revision_and_cap_replay(self):
        self._case_test_death_partial_replacement_then_revision_and_cap_replay()

    def test_r2_unknown_deferred_and_verdictless_bound_are_atomic(self):
        self._case_test_r2_unknown_deferred_and_verdictless_bound_are_atomic()

    def test_terminal_no_start_reuses_group_start_for_the_exact_gap(self):
        self._case_test_terminal_no_start_reuses_group_start_for_the_exact_gap()

if __name__ == "__main__":
    unittest.main()
