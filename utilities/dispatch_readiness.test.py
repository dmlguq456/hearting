#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "dispatch_readiness", HERE / "dispatch-readiness.py"
)
READINESS = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(READINESS)


class DispatchReadinessTest(unittest.TestCase):
    def test_codex_parent_automatically_uses_prospective_owner_contract(self) -> None:
        observed = []

        def evaluate(args):
            observed.append(args)
            return {
                "parent_harness": args.parent_harness,
                "child_harness": args.child_harness,
                "status": "supported",
            }

        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            READINESS.NESTED, "evaluate", side_effect=evaluate
        ):
            root = Path(raw)
            evidence = READINESS.generate(
                worktree=root,
                jobs=root / "jobs.log",
                owner_harnesses=["claude", "codex"],
                child_harnesses=["claude", "codex"],
            )
        self.assertEqual(len(evidence["tuples"]), 4)
        codex = [row for row in observed if row.parent_harness == "codex"]
        claude = [row for row in observed if row.parent_harness == "claude"]
        self.assertTrue(all(row.prospective_standard_owner for row in codex))
        self.assertTrue(all(not row.prospective_standard_owner for row in claude))
        self.assertTrue(all(row.parent_transport == "headless" for row in observed))

    def test_active_dispatch_cannot_masquerade_as_pre_owner_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            os.environ, {"AGENT_DISPATCH_DEPTH": "1"}, clear=True
        ):
            root = Path(raw)
            with self.assertRaises(READINESS.ReadinessError) as raised:
                READINESS.generate(
                    worktree=root,
                    jobs=root / "jobs.log",
                    owner_harnesses=["codex"],
                    child_harnesses=["codex"],
                )
        self.assertEqual(str(raised.exception), "dispatch-readiness-inside-dispatch")

    def test_disabled_child_is_sealed_without_an_implicit_probe(self) -> None:
        observed = []

        def evaluate(args):
            observed.append(args)
            return {"status": "unsupported", "failure_class": "user-disabled"}

        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            READINESS.NESTED, "evaluate", side_effect=evaluate
        ):
            root = Path(raw)
            READINESS.generate(
                worktree=root,
                jobs=root / "jobs.log",
                owner_harnesses=["codex"],
                child_harnesses=["claude"],
                disabled_harnesses={"claude"},
            )
        self.assertTrue(observed[0].user_disabled)


if __name__ == "__main__":
    unittest.main()
