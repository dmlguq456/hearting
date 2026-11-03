#!/usr/bin/env python3
"""One semantic slice of the review batch revision lifecycle."""
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
