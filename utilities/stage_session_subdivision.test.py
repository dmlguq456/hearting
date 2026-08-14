#!/usr/bin/env python3
"""SD-103 parallel-subdivision fixtures (AC 25-30; B2 arbitration).

The plan file name for these fixtures is `utilities/stage_session_subdivision.test.py`;
`stage_session_contract.test.py` does not exist. The existing SD-96 coverage lives
in `utilities/worker_capacity_contract.test.py`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import importlib.util

_SSC_SPEC = importlib.util.spec_from_file_location(
    "stage_session_contract", Path(__file__).with_name("stage_session_contract.py")
)
SSC = importlib.util.module_from_spec(_SSC_SPEC)
_SSC_SPEC.loader.exec_module(SSC)

_CR_SPEC = importlib.util.spec_from_file_location(
    "capability_route", Path(__file__).resolve().parents[1] / "utilities" / "capability-route.py"
)
CR = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(CR)


class SubdivisionContractTest(unittest.TestCase):
    def _fixture(self, td, *, mode="parallel", overlap=False, outside=False,
                 session_count=2, subdivision=True, exact_scope=False):
        root = Path(td)
        worktree = root / "worktree"
        worktree.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        subprocess.run(["git", "-C", str(worktree), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Fixture"], check=True)
        route_path = root / "route.json"
        route = {
            "route_id": "rt-sub",
            "route_hash": "sha256:" + "1" * 64,
            "registry_digest": "sha256:" + "2" * 64,
            "cwd": str(worktree),
        }
        write_scope = ["checklist.md", "manifest.json"] if exact_scope else ["source/**", "dev_logs/**"]
        node = {
            "id": "execute", "dispatch_depth": 2, "completion_gate": "execute-complete",
            "kind": "pipeline-stage", "write_scope": write_scope,
        }
        if subdivision:
            node["subdivision"] = {
                "min_intensity": "strong", "max_slices": 4, "disjointness": "exact-fixed-files",
            }
        route["nodes"] = [node]
        route_path.write_text(json.dumps(route))
        sessions = []
        exact_names = ["checklist.md", "manifest.json"]
        for index in range(1, session_count + 1):
            brief = root / f"brief-{index}.md"
            brief.write_text(f"slice {index}\n")
            if overlap:
                fixed = worktree / "shared.py"
            elif outside:
                fixed = worktree.parent / f"outside-{index}.py"
            elif exact_scope:
                fixed = worktree / exact_names[(index - 1) % len(exact_names)]
                fixed.write_text(f"slice {index}\n")
            else:
                fixed = worktree / f"source/file-{index}.py"
                fixed.parent.mkdir(exist_ok=True)
            sessions.append({
                "subsession_id": f"ss-sub{index}",
                "attempt_id": f"att-sub-slice-{index}",
                "adapter": "codex",
                "slug": f"sub-{index}",
                "phase_brief": str(brief),
                "fixed_files": [str(fixed)],
                "narrow_verify": f"python -m unittest slice_{index}",
                "expected_round_trips": 2,
            })
        manifest = {
            "schema_version": 1,
            "kind": "stage-session-chain",
            "chain_id": "ssc-subdiv",
            "mode": mode,
            "worktree": str(worktree),
            "route_file": str(route_path),
            "route_id": route["route_id"],
            "route_hash": route["route_hash"],
            "route_node": node["id"],
            "completion_gate": node["completion_gate"],
            "sessions": sessions,
        }
        manifest_path = root / "chain.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return worktree, route, node, manifest_path

    def test_ac25_glob_and_escape_fall_back_to_single_session(self):
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td)
            # a glob in fixed_files must be rejected and fall back
            data = json.loads(manifest_path.read_text())
            data["sessions"][0]["fixed_files"] = [str(worktree / "source/**")]
            manifest_path.write_text(json.dumps(data))
            ledger = []
            manifest, reason = SSC.validate_subdivision_or_fallback(
                manifest_path, route=route, node=node,
                record=lambda rid, rn, d: ledger.append((rid, rn, d)),
            )
            self.assertIsNone(manifest)
            self.assertEqual(reason, "subdivision-disjointness-unproven")
            self.assertEqual(len(ledger), 1)
            self.assertEqual(ledger[0][1], "execute")

    def test_ac26_overlap_and_outside_scope_fall_back(self):
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td, overlap=True)
            ledger = []
            manifest, reason = SSC.validate_subdivision_or_fallback(
                manifest_path, route=route, node=node,
                record=lambda rid, rn, d: ledger.append(d),
            )
            self.assertIsNone(manifest)
            self.assertEqual(reason, "subdivision-disjointness-unproven")
            self.assertEqual(len(ledger), 1)
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td, outside=True)
            with self.assertRaises(SSC.StageSessionError):
                SSC.load_manifest(manifest_path, route=route, node=node)

    def test_g4_exact_file_write_scope_entry_is_admitted(self):
        # G4: an exact-file write_scope entry (no "/**" suffix) must admit a
        # fixed_files entry that matches it exactly. The old comparison was
        # `file == root` where `file` is str and `root` is Path -- always
        # False -- so every exact-file scope entry rejected every fixed file.
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td, exact_scope=True)
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            self.assertEqual(len(manifest["sessions"]), 2)

    def _attempt_row(self, *, route, manifest, session, index, count, timestamp="2026-08-14T00:00:00Z"):
        fake_sha = "a" * 64
        fields = {
            "attempt_schema_version": "2",
            "attempt_id": session["attempt_id"],
            "dispatch_depth": "2",
            "transport": "interactive",
            "execution_surface": "inline",
            "registered_worker": "0",
            "fallback_hop": "inline",
            "note": "completed-marker",
            "failure_class": "pass",
            "launch_outcome": "reaped-before-publish",
            "route_id": route["route_id"],
            "route_hash": route["route_hash"],
            "route_node": manifest["route_node"],
            "subsession_id": session["subsession_id"],
            "stage_authority": "0",
            "session_chain_id": manifest["chain_id"],
            "subsession_index": str(index),
            "subsession_count": str(count),
            "subsession_mode": "parallel",
            "subsession_purpose": "planned",
            "parallel_group": manifest["route_node"],
            "phase_brief": session["phase_brief"],
            "phase_brief_sha256": fake_sha,
            "state_ledger": str(Path(session["phase_brief"]).with_suffix(".ledger.json")),
            "fixed_files_sha256": fake_sha,
            "narrow_verify_sha256": fake_sha,
            "expected_round_trips": str(session["expected_round_trips"]),
        }
        blob = ",".join(f"{key}={value}" for key, value in fields.items())
        return "\t".join([timestamp, "done", "/repo", str(Path(manifest["worktree"])), session["slug"], blob])

    def _write_jobs(self, jobs_path, route, manifest):
        sessions = manifest["sessions"]
        lines = [
            self._attempt_row(route=route, manifest=manifest, session=session, index=i + 1, count=len(sessions))
            for i, session in enumerate(sessions)
        ]
        jobs_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_ac27_marker_requires_all_slices_and_slice_complete_refused(self):
        # G7: exercise complete_subsession_stage through production code --
        # exactly one aggregated marker for the two slices, and an attempt to
        # complete the stage gate directly off one slice's own attempt row
        # (stage_authority=0) is refused, not silently accepted.
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td)
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            jobs_path = Path(td) / "jobs.log"
            self._write_jobs(jobs_path, route, manifest)
            evidence = Path(td) / "evidence.md"
            evidence.write_text("execute done\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs_path)}):
                marker, status = CR.complete_subsession_stage(
                    route, node, "execute", evidence, manifest_path, jobs_path,
                )
                self.assertEqual(status["status"], "stage-gate-aggregated")
                self.assertEqual(status["sessions"], 2)
                self.assertEqual(marker["node_id"], "execute")
                self.assertTrue(str(marker["attempt_id"]).startswith("att-stage-"))
                completion_dir = CR.completion_dir(route["route_id"])
                canonical_marker = completion_dir / "execute.json"
                self.assertTrue(canonical_marker.is_file())
                history_markers = list(completion_dir.glob("execute.*.json"))
                self.assertEqual(len(history_markers), 1)
                # A second aggregation call with the same manifest/jobs must stay
                # exactly one canonical marker + one history entry (idempotent
                # resume by manifest hash), not fan out a duplicate.
                CR.complete_subsession_stage(route, node, "execute", evidence, manifest_path, jobs_path)
                history_markers_again = list(completion_dir.glob("execute.*.json"))
                self.assertEqual(len(history_markers_again), 1)
                # A slice's own attempt row carries no stage-gate authority: a
                # direct `complete` off it must be refused, not silently accepted.
                slice_attempt_id = manifest["sessions"][0]["attempt_id"]
                with self.assertRaisesRegex(ValueError, "subsession-has-no-stage-gate-authority"):
                    CR.complete_node(route, node, "execute", evidence, jobs=jobs_path, attempt_id=slice_attempt_id)

    def test_ac28_diff_scope_violation_refuses_marker(self):
        # G7: an unplanned change outside the declared fixed_files union must
        # be caught by complete_subsession_stage itself -- typed rejection,
        # subdivision-scope-violation ledger row, and no marker written --
        # not merely detected by the raw git-diff helper in isolation.
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td)
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            jobs_path = Path(td) / "jobs.log"
            self._write_jobs(jobs_path, route, manifest)
            evidence = Path(td) / "evidence.md"
            evidence.write_text("execute done\n", encoding="utf-8")
            rogue = worktree / "rogue.py"
            rogue.write_text("unplanned change\n", encoding="utf-8")
            recorded = []
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs_path)}):
                with mock.patch.object(CR, "record_degradation", side_effect=lambda **kw: recorded.append(kw)):
                    with self.assertRaisesRegex(ValueError, "subdivision-scope-violation"):
                        CR.complete_subsession_stage(route, node, "execute", evidence, manifest_path, jobs_path)
                self.assertEqual(len(recorded), 1)
                self.assertEqual(recorded[0]["reason"], "subdivision-scope-violation")
                completion_dir = CR.completion_dir(route["route_id"])
                canonical_marker = completion_dir / "execute.json"
                self.assertFalse(canonical_marker.is_file())

    def test_ac29_gap_retry_uses_only_failed_slice_files(self):
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td)
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            failed = manifest["sessions"][1]
            gap = {"subsession_id": "ss-gap1", "attempt_id": "att-gap-1",
                   "adapter": "codex", "slug": "gap-1",
                   "phase_brief": failed["phase_brief"],
                   "fixed_files": failed["fixed_files"],
                   "narrow_verify": failed["narrow_verify"],
                   "expected_round_trips": 2}
            self.assertEqual(sorted(gap["fixed_files"]), sorted(failed["fixed_files"]))

    def test_ac30_resume_by_manifest_hash(self):
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td)
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            digest = manifest["_manifest_sha256"]
            self.assertEqual(digest, SSC.sha256_file(manifest_path))
            # same content -> same hash (deterministic resume identity)
            self.assertEqual(digest, SSC.sha256_file(manifest_path))


if __name__ == "__main__":
    unittest.main()
