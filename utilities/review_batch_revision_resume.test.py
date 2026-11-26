#!/usr/bin/env python3
"""One semantic slice of the review batch revision lifecycle."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from review_batch_revision_fixture import RevisionBatchFixture, AdmissionSelectionFixture

class RevisionBatchCLI(RevisionBatchFixture):
    def test_one_started_one_missing_admitted_resume(self):
        self._case_test_one_started_one_missing_admitted_resume()

    def test_both_zero_admitted_resume(self):
        self._case_test_both_zero_admitted_resume()

    def test_closure_capped_peer_pending_sibling(self):
        self._case_test_closure_capped_peer_pending_sibling()

    def test_live_peer_preserved_and_unknown_unsettled_refused(self):
        self._case_test_live_peer_preserved_and_unknown_unsettled_refused()

if __name__ == "__main__":
    unittest.main()
