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

    def test_candidates_projection_feeds_quick_compile_directly(self) -> None:
        """6b227d: the documented depth-0 direct/quick path is
        dispatch-readiness → `capability-route.py compile
        --registered-headless-evidence`, but compile validates a `candidates`
        list this tool never emitted, so the path always failed
        `quick-headless-unavailable`. The projection keys one candidate per
        child harness in compile's exact field shape, a supported probe
        outranking an unsupported one."""

        def evaluate(args):
            supported = not (args.parent_harness == "codex"
                             and args.child_harness == "claude")
            return {
                "parent_harness": args.parent_harness,
                "child_harness": args.child_harness,
                "status": "supported" if supported else "unsupported",
                "probe_source": "fixture-probe",
                "probe_time": "2026-08-21T00:00:00Z",
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
        candidates = evidence["candidates"]
        self.assertEqual(
            [row["harness"] for row in candidates], ["claude", "codex"]
        )
        for row in candidates:
            self.assertEqual(
                set(row),
                {"harness", "transport", "surface", "status",
                 "probe_source", "probe_time"},
            )
            self.assertEqual(row["transport"], "headless")
            self.assertEqual(row["surface"], "registered-headless")
            # claude was unsupported under the codex parent but supported under
            # the claude parent — the supported probe wins the single slot.
            self.assertEqual(row["status"], "supported")
            self.assertEqual(row["probe_source"], "fixture-probe")

    def test_candidates_status_words_fail_closed_to_unknown(self) -> None:
        def evaluate(args):
            return {
                "parent_harness": args.parent_harness,
                "child_harness": args.child_harness,
                "status": "not-a-status",
                "probe_source": "",
                "probe_time": "",
            }

        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            READINESS.NESTED, "evaluate", side_effect=evaluate
        ):
            root = Path(raw)
            evidence = READINESS.generate(
                worktree=root,
                jobs=root / "jobs.log",
                owner_harnesses=["claude"],
                child_harnesses=["codex"],
            )
        (candidate,) = evidence["candidates"]
        self.assertEqual(candidate["status"], "unknown")
        self.assertEqual(candidate["probe_source"], "dispatch-readiness")


if __name__ == "__main__":
    unittest.main()
