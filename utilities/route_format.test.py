#!/usr/bin/env python3
"""route_format is the one reader of which route contracts and formats a release supports."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import route_format as F  # noqa: E402

CURRENT = {"schema_version": 2, "dispatch_contract_version": 3}


class UnsupportedReasonTest(unittest.TestCase):
    def test_a_current_record_is_supported_with_or_without_the_optional_markers(self):
        for extra in ({}, {"dispatch_evidence_scope_version": 1},
                      {"persona_independence_contract_version": 1},
                      {"validation_basis": {"basis_version": 1}}):
            with self.subTest(extra=extra):
                self.assertIsNone(F.unsupported_reason({**CURRENT, **extra}))

    def test_each_unsupported_declaration_gets_the_message_its_old_check_gave(self):
        cases = (
            ({"schema_version": 1}, "legacy-broker-route-read-only"),
            ({"broker_contract_version": 2}, "legacy-broker-route-read-only"),
            ({"dispatch_contract_version": 2}, "legacy dispatch contract is read-only"),
            ({"dispatch_contract_version": None}, "legacy dispatch contract is read-only"),
            ({"dispatch_evidence_scope_version": 2}, "unsupported dispatch evidence scope version"),
            ({"persona_independence_contract_version": 2},
             "unsupported-persona-independence-contract-version"),
            ({"persona_independence_contract_version": True},
             "unsupported-persona-independence-contract-version"),
            ({"validation_basis": {"basis_version": 2}},
             "unsupported-validation-basis-version(basis_version=2)"),
        )
        for fields, reason in cases:
            with self.subTest(fields=fields):
                self.assertEqual(F.unsupported_reason({**CURRENT, **fields}), reason)

    def test_a_newer_basis_degrades_only_for_the_caller_that_tolerates_it(self):
        newer = {**CURRENT, "validation_basis": {"basis_version": 2}}
        self.assertIsNone(F.unsupported_reason(newer, allow_stale_basis=True))

    def test_the_route_compiler_and_the_wrapper_use_these_definitions(self):
        spec = importlib.util.spec_from_file_location("route_format_capability_route",
                                                      ROOT / "utilities" / "capability-route.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ("ROUTE_SCHEMA_VERSION", "DISPATCH_CONTRACT_VERSION",
                     "DISPATCH_EVIDENCE_SCOPE_VERSION", "VALIDATION_BASIS_VERSION"):
            self.assertEqual(getattr(module, name), getattr(F, name), name)
        self.assertEqual(F.schema_reason({"schema_version": 1}),
                         "legacy route schema_version=1 rejected for mutating/resume use")


class CommandLineTest(unittest.TestCase):
    def run_reader(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "route.json"
            if content is not None:
                path.write_text(content, encoding="utf-8")
            return subprocess.run([sys.executable, "-I", str(ROOT / "utilities" / "route_format.py"),
                                   str(path)], text=True, capture_output=True)

    def test_exit_codes_separate_supported_unsupported_and_unreadable(self):
        supported = self.run_reader(json.dumps(CURRENT))
        self.assertEqual((supported.returncode, supported.stdout), (0, ""))
        refused = self.run_reader(json.dumps({**CURRENT, "dispatch_contract_version": 2}))
        self.assertEqual((refused.returncode, refused.stdout.strip()),
                         (65, "legacy dispatch contract is read-only"))
        for content in (None, "{not json"):
            with self.subTest(content=content):
                unreadable = self.run_reader(content)
                self.assertEqual((unreadable.returncode, unreadable.stdout), (66, ""))
        self.assertEqual(subprocess.run([sys.executable, "-I", str(ROOT / "utilities" / "route_format.py")],
                                        capture_output=True).returncode, 66)


if __name__ == "__main__":
    unittest.main()
