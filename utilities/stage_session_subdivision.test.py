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

# AC 29 has to cross the admission seam, not stop one step short of it: the
# binding gate (N1) lives in `dispatch-batch`, so a derived manifest that never
# reaches it proves nothing about whether the recovery path can be admitted.
_DB_SPEC = importlib.util.spec_from_file_location(
    "dispatch_batch", Path(__file__).with_name("dispatch-batch.py")
)
DB = importlib.util.module_from_spec(_DB_SPEC)
_DB_SPEC.loader.exec_module(DB)


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
                # N1: an admitted manifest names its leg. The fixture carries it
                # so a derived gap-retry can be measured against the same gate
                # the parent had to pass.
                "node": f"{node['id']}-slice-{index}",
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

    def _attempt_row(self, *, route, manifest, session, index, count, mode="parallel", timestamp="2026-08-14T00:00:00Z"):
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
            "subsession_mode": mode,
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

    def _write_jobs(self, jobs_path, route, manifest, mode="parallel"):
        sessions = manifest["sessions"]
        lines = [
            self._attempt_row(route=route, manifest=manifest, session=session,
                              index=i + 1, count=len(sessions), mode=mode)
            for i, session in enumerate(sessions)
        ]
        jobs_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _admit(self, route, node, manifest):
        """Record the admission-time baseline exactly as dispatch-batch does."""
        return CR.record_subdivision_baseline(route, node["id"], manifest)

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
                self._admit(route, node, manifest)
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
            recorded = []
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs_path)}):
                self._admit(route, node, manifest)
                rogue = worktree / "rogue.py"
                rogue.write_text("unplanned change\n", encoding="utf-8")
                with mock.patch.object(CR, "record_degradation", side_effect=lambda **kw: recorded.append(kw)):
                    with self.assertRaisesRegex(ValueError, "subdivision-scope-violation"):
                        CR.complete_subsession_stage(route, node, "execute", evidence, manifest_path, jobs_path)
                self.assertEqual(len(recorded), 1)
                self.assertEqual(recorded[0]["reason"], "subdivision-scope-violation")
                self.assertIn("rogue.py", recorded[0]["detail"])
                completion_dir = CR.completion_dir(route["route_id"])
                canonical_marker = completion_dir / "execute.json"
                self.assertFalse(canonical_marker.is_file())

    def test_anchor_m3_audit_measures_the_slice_delta_not_the_whole_worktree(self):
        # anchor M3: `git status` reports the whole worktree, so a change the
        # stage made BEFORE the subdivision opened -- inside write_scope but
        # outside the slices' fixed_files union, e.g. its own dev log -- used to
        # be indistinguishable from a slice escaping its fence and refused the
        # marker for work no slice did. The audit now subtracts the
        # admission-time baseline. A change made AFTER admission still fails.
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td)
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            jobs_path = Path(td) / "jobs.log"
            self._write_jobs(jobs_path, route, manifest)
            evidence = Path(td) / "evidence.md"
            evidence.write_text("execute done\n", encoding="utf-8")
            devlog = worktree / "dev_logs"
            devlog.mkdir()
            (devlog / "implementation.md").write_text("pre-subdivision\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs_path)}):
                baseline = self._admit(route, node, manifest)
                # the baseline is a CONTENT snapshot, not a path list: a path
                # list would exempt this file for the rest of the subdivision
                self.assertEqual(
                    baseline["changed_files"][str(devlog / "implementation.md")],
                    SSC.sha256_file(devlog / "implementation.md"),
                )
                marker, status = CR.complete_subsession_stage(
                    route, node, "execute", evidence, manifest_path, jobs_path,
                )
                self.assertEqual(status["status"], "stage-gate-aggregated")
                # B5: the same file changing again after admission is still a
                # violation -- the baseline is a start state, not a permanent
                # exemption list. This is the assertion the comment used to
                # claim while the code below it created a NEW file instead, so
                # the property it named was never the one under test.
                CR.completion_dir(route["route_id"]).joinpath("execute.json").unlink()
                (devlog / "implementation.md").write_text(
                    "rewritten after admission\n", encoding="utf-8"
                )
                rewritten = []
                with mock.patch.object(
                    CR, "record_degradation", side_effect=lambda **kw: rewritten.append(kw)
                ):
                    with self.assertRaisesRegex(ValueError, "subdivision-scope-violation"):
                        CR.complete_subsession_stage(
                            route, node, "execute", evidence, manifest_path, jobs_path,
                        )
                self.assertIn("implementation.md", rewritten[0]["detail"])
                # restoring the admission content makes it exempt again, so the
                # rule is the delta and not "this file is now permanently loud"
                (devlog / "implementation.md").write_text("pre-subdivision\n", encoding="utf-8")
                _marker, status = CR.complete_subsession_stage(
                    route, node, "execute", evidence, manifest_path, jobs_path,
                )
                self.assertEqual(status["status"], "stage-gate-aggregated")
                # and a NEW file outside every fence is still caught
                CR.completion_dir(route["route_id"]).joinpath("execute.json").unlink()
                rogue = worktree / "unplanned.py"
                rogue.write_text("after admission\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "subdivision-scope-violation"):
                    CR.complete_subsession_stage(
                        route, node, "execute", evidence, manifest_path, jobs_path,
                    )

    def test_ac30_slice_commit_is_refused(self):
        # AC 30 / SD-103: parallel slices are no-commit workers. index and HEAD
        # are shared state that fixed_files disjointness cannot protect, so a
        # HEAD that moved between admission and the stage gate is a slice that
        # committed -- typed refusal, ledger row, and no marker.
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td)
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            jobs_path = Path(td) / "jobs.log"
            self._write_jobs(jobs_path, route, manifest)
            evidence = Path(td) / "evidence.md"
            evidence.write_text("execute done\n", encoding="utf-8")
            recorded = []
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs_path)}):
                self._admit(route, node, manifest)
                for session in manifest["sessions"]:
                    Path(session["fixed_files"][0]).write_text("slice edit\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True)
                subprocess.run(
                    ["git", "-C", str(worktree), "commit", "-q", "-m", "slice commit"],
                    check=True,
                )
                with mock.patch.object(CR, "record_degradation", side_effect=lambda **kw: recorded.append(kw)):
                    with self.assertRaisesRegex(ValueError, "subdivision-commit-attempted"):
                        CR.complete_subsession_stage(
                            route, node, "execute", evidence, manifest_path, jobs_path,
                        )
                self.assertEqual(len(recorded), 1)
                self.assertEqual(recorded[0]["reason"], "subdivision-commit-attempted")
                self.assertFalse(
                    (CR.completion_dir(route["route_id"]) / "execute.json").is_file()
                )

    def test_b2_owner_post_quiescence_commit_is_not_a_slice_commit(self):
        # B2: judging "a slice committed" from "HEAD moved at all" refused the
        # OWNER's own commit. SD-103 has the owner commit once after quiescence
        # and `core/OPERATIONS.md` §5.10 already accepts first-parent descendant
        # HEAD movement under the same lineage proof as an in-place retry, so
        # movement alone was the wrong proposition -- and with a write-once
        # baseline and an unrewindable HEAD the refusal had no recovery path.
        import subprocess

        def _commit(worktree, message, *paths):
            subprocess.run(["git", "-C", str(worktree), "add", *paths], check=True)
            subprocess.run(
                ["git", "-C", str(worktree), "commit", "-q", "-m", message], check=True
            )

        def _staged(td):
            # a real worktree has history; the fixture's bare `git init` leaves
            # HEAD unborn, which is the separate no-lineage-anchor case below
            worktree, route, node, manifest_path = self._fixture(td)
            subprocess.run(
                ["git", "-C", str(worktree), "commit", "-q", "--allow-empty", "-m", "base"],
                check=True,
            )
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            jobs_path = Path(td) / "jobs.log"
            self._write_jobs(jobs_path, route, manifest)
            evidence = Path(td) / "evidence.md"
            evidence.write_text("execute done\n", encoding="utf-8")
            return worktree, route, node, manifest, manifest_path, jobs_path, evidence

        # 1. the sanctioned order -- close the stage gate, THEN commit -- and the
        #    idempotent replay of that same gate afterwards
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest, mp, jobs, evidence = _staged(td)
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                self._admit(route, node, manifest)
                for session in manifest["sessions"]:
                    Path(session["fixed_files"][0]).write_text("slice edit\n", encoding="utf-8")
                _marker, status = CR.complete_subsession_stage(
                    route, node, "execute", evidence, mp, jobs
                )
                self.assertEqual(status["status"], "stage-gate-aggregated")
                self.assertNotIn("resumed", status)
                _commit(worktree, "owner post-quiescence commit", "-A")
                _marker, resumed = CR.complete_subsession_stage(
                    route, node, "execute", evidence, mp, jobs
                )
                self.assertEqual(resumed["status"], "stage-gate-aggregated")
                self.assertTrue(resumed["resumed"])

        # 2. a slice that commits its own fence is still refused (AC 30 stands)
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest, mp, jobs, evidence = _staged(td)
            recorded = []
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                self._admit(route, node, manifest)
                for session in manifest["sessions"]:
                    Path(session["fixed_files"][0]).write_text("slice edit\n", encoding="utf-8")
                _commit(worktree, "slice commit", "-A")
                with mock.patch.object(
                    CR, "record_degradation", side_effect=lambda **kw: recorded.append(kw)
                ):
                    with self.assertRaisesRegex(ValueError, "subdivision-commit-attempted"):
                        CR.complete_subsession_stage(
                            route, node, "execute", evidence, mp, jobs
                        )
                self.assertIn("carries", recorded[0]["detail"])

        # 3. history that is NOT a first-parent descendant is refused outright --
        #    that is the lineage break `OPERATIONS.md` §5.10 does not accept
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest, mp, jobs, evidence = _staged(td)
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                self._admit(route, node, manifest)
                subprocess.run(
                    ["git", "-C", str(worktree), "checkout", "-q", "--orphan", "diverged"],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(worktree), "commit", "-q", "--allow-empty",
                     "-m", "diverged"],
                    check=True,
                )
                with self.assertRaisesRegex(ValueError, "subdivision-commit-attempted"):
                    CR.complete_subsession_stage(route, node, "execute", evidence, mp, jobs)

        # 4. accepting a lineage-clean commit must not blind the scope audit: a
        #    commit takes its files OUT of `git status`, so an out-of-fence file
        #    hidden in one is still caught
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest, mp, jobs, evidence = _staged(td)
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                self._admit(route, node, manifest)
                (worktree / "rogue.py").write_text("in no fence\n", encoding="utf-8")
                _commit(worktree, "rogue commit", "rogue.py")
                with self.assertRaisesRegex(ValueError, "subdivision-scope-violation"):
                    CR.complete_subsession_stage(route, node, "execute", evidence, mp, jobs)

        # 5. the resume path claims it recognizes a replay of THIS stage gate and
        #    nothing else -- i.e. exactly what `write_completion_marker` treats as
        #    a replay. Pin that claim in the one state where the two could drift:
        #    the canonical marker's immutable history sibling is gone, which
        #    `write_completion_marker` refuses. The stage gate must refuse
        #    identically rather than report the gate resumed.
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest, mp, jobs, evidence = _staged(td)
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                self._admit(route, node, manifest)
                for session in manifest["sessions"]:
                    Path(session["fixed_files"][0]).write_text("slice edit\n", encoding="utf-8")
                marker, _status = CR.complete_subsession_stage(
                    route, node, "execute", evidence, mp, jobs
                )
                history = (
                    CR.completion_dir(route["route_id"])
                    / f"execute.{marker['sequence']}.json"
                )
                self.assertTrue(history.is_file())
                history.unlink()
                metadata = {
                    "stage_authority": "owner-chain",
                    "subsession_manifest": marker["subsession_manifest"],
                    "subsession_manifest_sha256": marker["subsession_manifest_sha256"],
                    "session_chain_id": marker["session_chain_id"],
                }
                with self.assertRaisesRegex(
                    ValueError, "canonical completion marker history conflict"
                ):
                    CR.write_completion_marker(
                        route, node, "execute", evidence,
                        attempt_id=marker["attempt_id"], attempt_metadata=metadata,
                    )
                with self.assertRaisesRegex(
                    ValueError, "canonical completion marker history conflict"
                ):
                    CR.complete_subsession_stage(route, node, "execute", evidence, mp, jobs)

    def test_missing_admission_baseline_fails_closed(self):
        # For a PARALLEL subdivision, an absent baseline means the audit is not
        # slice attribution at all, so it refuses rather than silently widening
        # back to the whole worktree. A `serial` SD-96 chain is admitted through
        # a path that records no baseline, so it keeps the pre-existing
        # whole-worktree measurement instead of becoming uncompletable.
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td)
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            jobs_path = Path(td) / "jobs.log"
            self._write_jobs(jobs_path, route, manifest)
            evidence = Path(td) / "evidence.md"
            evidence.write_text("execute done\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs_path)}):
                with self.assertRaisesRegex(ValueError, "subdivision-baseline-missing"):
                    CR.complete_subsession_stage(
                        route, node, "execute", evidence, manifest_path, jobs_path,
                    )
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td, mode="serial")
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            jobs_path = Path(td) / "jobs.log"
            self._write_jobs(jobs_path, route, manifest, mode="serial")
            evidence = Path(td) / "evidence.md"
            evidence.write_text("execute done\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs_path)}):
                _marker, status = CR.complete_subsession_stage(
                    route, node, "execute", evidence, manifest_path, jobs_path,
                )
            self.assertEqual(status["status"], "stage-gate-aggregated")

    def test_ac29_gap_retry_uses_only_failed_slice_files(self):
        # AC 29: the retry manifest is DERIVED by production code from the
        # failed slice, then validated by the real loader. The previous fixture
        # assigned `gap["fixed_files"] = failed["fixed_files"]` and compared the
        # result with itself, which no production path could ever contradict.
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td)
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            failed, succeeded = manifest["sessions"][1], manifest["sessions"][0]
            gap = SSC.derive_gap_retry_manifest(manifest, [failed["subsession_id"]])
            gap_path = Path(td) / "gap.json"
            gap_path.write_text(json.dumps(gap, indent=2), encoding="utf-8")
            loaded = SSC.load_manifest(gap_path, route=route, node=node)
            self.assertEqual(len(loaded["sessions"]), 1)
            self.assertEqual(loaded["mode"], "serial")
            self.assertEqual(
                loaded["sessions"][0]["fixed_files"], failed["fixed_files"]
            )
            self.assertEqual(
                loaded["sessions"][0]["subsession_purpose"], SSC.GAP_RETRY_PURPOSE
            )
            self.assertEqual(loaded["sessions"][0]["gap_retry_of"], failed["subsession_id"])
            # the successful sibling's files are NOT re-opened by the retry
            for path in succeeded["fixed_files"]:
                self.assertNotIn(path, loaded["sessions"][0]["fixed_files"])
            # identities are derived from the parent hash, so re-deriving the
            # same retry is byte-identical (resumable, not a fresh chain)
            self.assertEqual(
                SSC.derive_gap_retry_manifest(manifest, [failed["subsession_id"]]), gap
            )
            # two failed slices stay a parallel chain and carry exactly their
            # own two fences
            both = SSC.derive_gap_retry_manifest(
                manifest, [s["subsession_id"] for s in manifest["sessions"]]
            )
            self.assertEqual(both["mode"], "parallel")
            self.assertEqual(
                sorted(f for s in both["sessions"] for f in s["fixed_files"]),
                sorted(f for s in manifest["sessions"] for f in s["fixed_files"]),
            )
            # B1: the derived manifest has to survive the SAME admission seam the
            # parent did -- derive -> load_manifest -> _bind_subdivision_sessions
            # in one chain. Stopping at `load_manifest` is what hid the fact that
            # the derivation dropped the N1 leg key and made 13.30.5's only
            # recovery path refusable at admission whenever two slices fail.
            legs = [{"id": s["node"]} for s in manifest["sessions"]]
            both_path = Path(td) / "gap-both.json"
            both_path.write_text(json.dumps(both, indent=2), encoding="utf-8")
            both_loaded = SSC.load_manifest(both_path, route=route, node=node)
            bound = DB._bind_subdivision_sessions(both_loaded["sessions"], legs)
            self.assertEqual([s["node"] for s in bound], [leg["id"] for leg in legs])
            for slice_, source in zip(bound, manifest["sessions"]):
                self.assertEqual(slice_["fixed_files"], source["fixed_files"])
                self.assertEqual(slice_["gap_retry_of"], source["subsession_id"])
            # and the binding is by NAME, not position: a leg list in the other
            # order still hands each slice its own fence
            reversed_bound = DB._bind_subdivision_sessions(
                both_loaded["sessions"], list(reversed(legs))
            )
            self.assertEqual(
                [s["node"] for s in reversed_bound],
                [leg["id"] for leg in reversed(legs)],
            )
            # a derivation that dropped the leg key would be refused here
            stripped = json.loads(json.dumps(both))
            for session in stripped["sessions"]:
                session.pop("node", None)
            stripped_path = Path(td) / "gap-unbound.json"
            stripped_path.write_text(json.dumps(stripped, indent=2), encoding="utf-8")
            with self.assertRaises(DB.BatchError) as ctx:
                DB._bind_subdivision_sessions(
                    SSC.load_manifest(stripped_path, route=route, node=node)["sessions"],
                    legs,
                )
            self.assertEqual(ctx.exception.reason, "subdivision-manifest-session-leg-unbound")
            with self.assertRaisesRegex(SSC.StageSessionError, "gap-retry-unknown-slice"):
                SSC.derive_gap_retry_manifest(manifest, ["ss-not-a-slice"])
            with self.assertRaisesRegex(SSC.StageSessionError, "gap-retry-requires-a-failed-slice"):
                SSC.derive_gap_retry_manifest(manifest, [])

    def test_ac30_resume_by_manifest_hash(self):
        # AC 30: the resume identity is recovered FROM the manifest hash by the
        # real aggregation path -- a second call finds the prior marker instead
        # of writing a new one, and the admission baseline is looked up by the
        # same key. The previous fixture asserted sha256_file(p) == sha256_file(p).
        with tempfile.TemporaryDirectory() as td:
            worktree, route, node, manifest_path = self._fixture(td)
            manifest = SSC.load_manifest(manifest_path, route=route, node=node)
            jobs_path = Path(td) / "jobs.log"
            self._write_jobs(jobs_path, route, manifest)
            evidence = Path(td) / "evidence.md"
            evidence.write_text("execute done\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs_path)}):
                baseline = self._admit(route, node, manifest)
                # the baseline is addressed by the manifest hash, so a resumed
                # admission recovers the ORIGINAL start state rather than
                # snapshotting the half-finished worktree
                (worktree / "source" / "file-1.py").write_text("slice work\n", encoding="utf-8")
                resumed = self._admit(route, node, manifest)
                self.assertEqual(resumed, baseline)
                self.assertEqual(
                    CR.load_subdivision_baseline(route, "execute", manifest), baseline
                )
                first, _ = CR.complete_subsession_stage(
                    route, node, "execute", evidence, manifest_path, jobs_path,
                )
                again, _ = CR.complete_subsession_stage(
                    route, node, "execute", evidence, manifest_path, jobs_path,
                )
                self.assertEqual(first, again)
                self.assertEqual(
                    first["attempt_id"],
                    "att-stage-" + manifest["_manifest_sha256"][:32],
                )
                # a materially different manifest is a different stage identity,
                # not a resume of this one
                other = json.loads(manifest_path.read_text())
                other["sessions"][0]["expected_round_trips"] = 3
                other_path = Path(td) / "chain-2.json"
                other_path.write_text(json.dumps(other, indent=2), encoding="utf-8")
                other_manifest = SSC.load_manifest(other_path, route=route, node=node)
                self.assertNotEqual(
                    other_manifest["_manifest_sha256"], manifest["_manifest_sha256"]
                )
                self.assertIsNone(
                    CR.load_subdivision_baseline(route, "execute", other_manifest)
                )


if __name__ == "__main__":
    unittest.main()
