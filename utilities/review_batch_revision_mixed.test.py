#!/usr/bin/env python3
"""One semantic slice of the review batch revision lifecycle."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from review_batch_revision_fixture import (RevisionBatchFixture, AdmissionSelectionFixture,
                                          CurrentDesignPeerSelectionTest)

class RevisionBatchCLI(RevisionBatchFixture):
    def test_mixed_pass_fail_revision_through_public_batch(self):
        self._case_test_mixed_pass_fail_revision_through_public_batch()

    def test_mixed_one_sided_supersession_through_public_batch(self):
        self._case_test_mixed_one_sided_supersession_through_public_batch()

    def test_one_sided_revision_uses_canonical_writer_and_reader(self):
        self._case_test_one_sided_revision_uses_canonical_writer_and_reader()


class CurrentDesignPeerSelection(CurrentDesignPeerSelectionTest):
    def test_current_owner_closure_peer_precedes_round_admission(self):
        self._case_test_current_owner_closure_peer_precedes_round_admission()

    def test_replacement_owner_starts_only_primary_and_preserves_original_fail_peer(self):
        self._case_test_current_owner_closure_peer_precedes_round_admission(replacement_owner=True)

if __name__ == "__main__":
    unittest.main()
