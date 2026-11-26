#!/usr/bin/env python3
"""One semantic slice of the review batch revision lifecycle."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from review_batch_revision_fixture import RevisionBatchFixture, AdmissionSelectionFixture

class RevisionBatchCLI(RevisionBatchFixture):
    def test_two_pass_revision_inline_next_generation_and_cap_replay(self):
        self._case_test_two_pass_revision_inline_next_generation_and_cap_replay()

    def test_foreign_registry_cannot_override_the_sealed_jobs(self):
        self._case_test_foreign_registry_cannot_override_the_sealed_jobs()

class AdmissionSelection(AdmissionSelectionFixture):
    def test_logical_budget_and_physical_generation_selection(self):
        self._case_test_logical_budget_and_physical_generation_selection()

if __name__ == "__main__":
    unittest.main()
