#!/usr/bin/env python3
"""Temporary-state checks for the report verification read projection."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_verification_projection as projection  # noqa: E402
import artifact_lifecycle  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_manifest  # noqa: E402
import artifact_reader  # noqa: E402
import artifact_producer  # noqa: E402
import dispatch_contract  # noqa: E402
import dispatch_batch_obligations  # noqa: E402
import peer_obligations  # noqa: E402
import route_identity  # noqa: E402
import route_lineage  # noqa: E402

REAL_EVIDENCE = dispatch_contract.evidence_currency
REAL_GATE = dispatch_contract.gate_currency
REAL_READINESS = dispatch_contract.completion_attempt_readiness
REAL_LINEAGE = route_lineage.verified_route_lineage
REAL_ROUTE_HASH = route_identity.route_hash


ROOT_ID = "root_" + "a" * 32
CYCLE_ID = "cyc_" + "b" * 32
CAMP_ID = "camp_" + "c" * 32
ROUTE_ID = "rt-" + "d" * 16
ROUTE_HASH = "sha256:" + "e" * 64


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode()


class ProjectionFixture(unittest.TestCase):
    def test_empty_report_directory_is_hidden_and_open_report_is_explained(self):
        with mock.patch.object(artifact_lifecycle, "read_admitted_cycle", return_value=None):
            present = projection.project_report(self.source, artifact_root=self.root)
            detail = projection.report_detail_payload(present)
            self.assertIn("보고서 있음", detail["display"]["detail_label"])
            self.assertIn("담당 작업의 검증·마감 확인 필요", detail["display"]["detail_label"])
            self.assertEqual(present["verification"]["reason"], "report-cycle-unadmitted")
            for path in self.source.iterdir():
                if path.is_file():
                    path.unlink()
            absent = projection.project_report(self.source, artifact_root=self.root)
            self.assertIsNone(projection.report_detail_payload(absent))
    def test_route_without_report_keeps_absence_and_real_hash_mismatch(self):
        with mock.patch.object(artifact_producer, "route_cycle_for",
                               return_value={"cycle_id": CYCLE_ID}):
            # Use an admitted cycle with no report stage and no report directory.
            self.route["nodes"] = [{"id": "full-run", "outputs": []}]
            self.route_path.write_bytes(encoded(self.route))
            shutil.rmtree(self.source)
            absent = projection.project_route(self.root, ROUTE_ID, ROUTE_HASH)
            self.assertEqual(absent["verification"]["reason"], "report-source-unavailable")
            wrong = projection.project_route(self.root, ROUTE_ID, "sha256:" + "9" * 64)
            self.assertEqual(wrong["verification"]["reason"], "route-hash-binding-mismatch")

    def test_existing_invalid_report_source_is_visible(self):
        with mock.patch.object(artifact_producer, "route_cycle_for",
                               return_value={"cycle_id": CYCLE_ID}):
            self.source.rename(self.source.with_name("report-original"))
            self.source.write_text("invalid report directory", encoding="utf-8")
            payload = projection.project_route(self.root, ROUTE_ID, ROUTE_HASH)
            self.assertEqual(payload["verification"]["reason"], "report-source-kind-invalid")
            self.assertIs(projection.report_detail_payload(payload), payload)

    def test_route_keeps_other_unresolved_reasons_and_checks_bound_subject_hash(self):
        with mock.patch.object(artifact_producer, "route_cycle_for",
                               return_value={"cycle_id": CYCLE_ID}):
            for payload, reason in ((projection._unresolved("artifact-revision-stale"),
                                     "artifact-revision-stale"),
                                    ({"subject": {"state": "bound", "route_hash": "wrong"}},
                                     "route-hash-binding-mismatch")):
                with self.subTest(reason=reason), mock.patch.object(
                        projection, "_resolve_source", return_value=payload):
                    actual = projection.project_route(self.root, ROUTE_ID, ROUTE_HASH)
                    self.assertEqual(actual["verification"]["reason"], reason)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "root"
        self.cycle = self.root / "campaigns" / "fixture" / "cycle"
        self.source = self.cycle / "artifacts" / "report"
        self.jobs = base / "jobs.log"
        self.marker_dir = base / "completion" / ROUTE_ID
        self.source.mkdir(parents=True)
        self.jobs.write_bytes(b"")
        self.marker_dir.mkdir(parents=True)
        (self.cycle / "artifacts" / "reviews").mkdir()
        (self.cycle / "artifacts" / "reviews-alternative").mkdir()
        (self.source / "REPORT.md").write_text(
            "# Existing report\n\nPENDING: historical browser observation.\n", encoding="utf-8")
        (self.source / "index.html").write_text(
            "<!doctype html><html><body><p>PENDING</p></body></html>\n", encoding="utf-8")
        report_files = [
            {"path": name, "sha256": sha((self.source / name).read_bytes())}
            for name in ("REPORT.md", "index.html")
        ]
        (self.source / "report_manifest.json").write_bytes(encoded({
            "schema_version": 2, "entrypoint": "index.html", "files": report_files,
            "media": [],
        }))
        self.route = {
            "route_id": ROUTE_ID, "route_hash": ROUTE_HASH,
            "artifact_root": str(self.root), "harness": "codex",
            "nodes": [
                {"id": "report-finalize", "outputs": [
                    "report/REPORT.md", "report/index.html", "report/report_manifest.json"]},
                self.peer("independent-verify", "reviews/eval-verdict.json"),
                self.peer("independent-verify-alternative",
                          "reviews-alternative/eval-verdict.json"),
            ],
        }
        self.route_path = base / "route.json"
        self.route_path.write_bytes(encoded(self.route))
        self.document = {
            "cycle": {"campaign_id": CAMP_ID, "cycle_id": CYCLE_ID},
            "artifact_root_id": ROOT_ID, "manifest_revision_id": "mrev_" + "f" * 32,
            "routes": [{"route_id": ROUTE_ID, "route_hash": ROUTE_HASH,
                        "artifact_root_id": ROOT_ID}],
            "artifact_revisions": [],
        }
        self._add_revision("artifacts/report/REPORT.md", "art_" + "1" * 32)
        self._add_revision("artifacts/report/index.html", "art_" + "2" * 32)
        self._add_revision("artifacts/report/report_manifest.json", "art_" + "3" * 32)
        self.peer_paths = {}
        self.attempts = {}
        for leg, relative in (("independent-verify", "reviews/eval-verdict.json"),
                              ("independent-verify-alternative",
                               "reviews-alternative/eval-verdict.json")):
            path = self.cycle / "artifacts" / relative
            inputs = {
                str((self.cycle / "artifacts" / f"report/{name}").resolve()):
                    sha((self.source / name).read_bytes())
                for name in ("REPORT.md", "index.html", "report_manifest.json")
            }
            self.attempts[leg] = "att-current-" + leg
            self.peer_paths[leg] = path
            self.write_verdict(leg, path, "PASS", inputs)
            self._add_revision("artifacts/" + relative, "art_" + ("4" if leg.endswith("verify")
                                                                    else "5") * 32)
            marker = {
                "schema_version": 2,
                "route_id": ROUTE_ID, "route_hash": ROUTE_HASH, "node_id": leg,
                "attempt_id": self.attempts[leg], "stage_authority": "attempt",
                "evidence": {"path": str(path.resolve()), "sha256": sha(path.read_bytes())},
            }
            (self.marker_dir / f"{leg}.json").write_bytes(encoded(marker))
        (self.cycle / "manifest.json").write_bytes(encoded(self.document))
        self.completion_state = "complete"
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(
            artifact_reader, "bucket_dirs",
            return_value=[(self.source, {"campaign_id": CAMP_ID, "cycle_id": CYCLE_ID})]))
        stack.enter_context(mock.patch.object(artifact_lifecycle, "read_admitted_cycle",
                                              return_value={"cycle_id": CYCLE_ID}))
        stack.enter_context(mock.patch.object(artifact_lifecycle, "read_root_identity",
                                              return_value=SimpleNamespace(artifact_root_id=ROOT_ID)))
        stack.enter_context(mock.patch.object(artifact_locator, "find_path_by_id",
                                              side_effect=lambda _root, _cid: self.cycle))
        stack.enter_context(mock.patch.object(artifact_manifest, "validate",
                                              return_value=SimpleNamespace(ok=True)))
        stack.enter_context(mock.patch.object(artifact_manifest, "manifest_digest",
                                              return_value="sha256:" + "0" * 64))
        stack.enter_context(mock.patch.object(artifact_lifecycle, "canonical_route_path",
                                              return_value=self.route_path))
        stack.enter_context(mock.patch.object(
            artifact_lifecycle, "bind_existing_runtime_route",
            side_effect=lambda *_a, **_kw: (object(), self.route)))
        stack.enter_context(mock.patch.object(route_identity, "route_hash", return_value=ROUTE_HASH))
        stack.enter_context(mock.patch.object(route_lineage, "verified_route_lineage", return_value=[self.route]))
        stack.enter_context(mock.patch.object(artifact_lifecycle, "read_manifest_snapshots", return_value=[]))
        stack.enter_context(mock.patch.object(
            artifact_lifecycle, "evaluate_cycle_completion",
            side_effect=lambda *_a, **_kw: SimpleNamespace(
                state=self.completion_state, reasons=[])))
        stack.enter_context(mock.patch.object(
            artifact_lifecycle, "_load_capability_route",
            return_value=SimpleNamespace(completion_dir=lambda _rid, jobs=None: self.marker_dir)))
        stack.enter_context(mock.patch.object(
            dispatch_contract, "evidence_currency", return_value=SimpleNamespace(state="current")))
        stack.enter_context(mock.patch.object(
            dispatch_contract, "gate_currency", return_value=SimpleNamespace(state="current")))
        stack.enter_context(mock.patch.object(
            dispatch_contract, "completion_attempt_readiness",
            return_value=SimpleNamespace(state="ready")))

    @staticmethod
    def peer(leg, output):
        return {"id": leg, "kind": "review-worker", "leg_class": "peer",
                "parallel_group_kind": "verify", "parallel_group": "independent-verify",
                "parallel_join_policy": "all", "depends_on": ["report-finalize"],
                "outputs": [output],
                "inputs": ["report/REPORT.md", "report/index.html",
                           "report/report_manifest.json"]}

    def _add_revision(self, locator, artifact_id):
        path = self.cycle / locator
        raw = path.read_bytes()
        self.document["artifact_revisions"].append({
            "artifact_id": artifact_id,
            "artifact_revision_id": "arev_" + hashlib.sha256(locator.encode()).hexdigest()[:32],
            "locator": {"path": locator}, "content_digest": "sha256:" + sha(raw),
            "byte_size": len(raw), "provenance": {"producer_route_id": ROUTE_ID},
        })

    def write_verdict(self, leg, path, value, inputs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded({"schema": "acfu-eval-verdict-v1", "route": ROUTE_ID,
                                  "leg": leg, "verdict": value, "round": 2,
                                  "attempt_id": self.attempts.get(leg, "att-current-" + leg),
                                  "inputs": inputs}))

    def revise_peer(self, leg, verdict=None, *, input_digest=None):
        path = self.peer_paths[leg]
        value = json.loads(path.read_text(encoding="utf-8"))
        if verdict:
            value["verdict"] = verdict
        if input_digest:
            key = next(iter(value["inputs"]))
            value["inputs"][key] = input_digest
        path.write_bytes(encoded(value))
        for row in self.document["artifact_revisions"]:
            if row["locator"]["path"] == "artifacts/" + path.relative_to(
                    self.cycle / "artifacts").as_posix():
                raw = path.read_bytes()
                row.update(content_digest="sha256:" + sha(raw), byte_size=len(raw))
        marker_path = self.marker_dir / f"{leg}.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["evidence"]["sha256"] = sha(path.read_bytes())
        marker_path.write_bytes(encoded(marker))
        (self.cycle / "manifest.json").write_bytes(encoded(self.document))

    def payload(self):
        return projection.project_report(self.source, artifact_root=self.root, jobs=self.jobs)


class ProjectionBehaviorTests(ProjectionFixture):
    def test_stale_pending_body_and_three_harnesses_keep_axes_separate_without_writes(self):
        before = tree_snapshot(Path(self.tmp.name))
        results = []
        for harness in ("claude", "codex", "opencode"):
            self.route["harness"] = harness
            self.route_path.write_bytes(encoded(self.route))
            before_read = tree_snapshot(Path(self.tmp.name))
            payload = self.payload()
            self.assertEqual(tree_snapshot(Path(self.tmp.name)), before_read)
            results.append((payload["verification"]["verdict"],
                            payload["completion"]["state"],
                            payload["required_input_observation"]["state"]))
            self.assertIn("PENDING", (self.source / "REPORT.md").read_text(encoding="utf-8"))
            self.assertEqual(payload["entrypoints"][0]["path"], "index.html")
        self.assertEqual(results, [("PASS", "complete", "confirmed")] * 3)
        self.route["harness"] = "codex"
        self.route_path.write_bytes(encoded(self.route))
        self.assertEqual(tree_snapshot(Path(self.tmp.name)), before)

    def test_authoritative_fail_survives_operational_success(self):
        self.revise_peer("independent-verify", "FAIL")
        payload = self.payload()
        self.assertEqual(payload["verification"]["verdict"], "FAIL")
        self.assertEqual(payload["completion"]["state"], "complete")
        self.assertEqual(payload["required_input_observation"]["state"], "confirmed")

    def test_required_input_mismatch_and_stale_report_revision_never_pass(self):
        self.revise_peer("independent-verify", input_digest="sha256:" + "0" * 64)
        payload = self.payload()
        self.assertEqual(payload["verification"]["verdict"], "unresolved")
        self.assertEqual(payload["required_input_observation"]["state"], "failed")
        self.assertEqual(payload["verification"]["reason"], "required-input-digest-mismatch")
        report = self.source / "REPORT.md"
        report.write_text(report.read_text(encoding="utf-8") + " changed\n", encoding="utf-8")
        stale = self.payload()
        self.assertEqual(stale["verification"]["verdict"], "unresolved")
        self.assertEqual(stale["integrity"]["reason"], "artifact-revision-stale")

    def test_marker_evidence_conflict_and_wrong_route_hash_are_unresolved(self):
        marker_path = self.marker_dir / "independent-verify.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["evidence"]["sha256"] = "sha256:" + "0" * 64
        marker_path.write_bytes(encoded(marker))
        payload = self.payload()
        self.assertEqual(payload["verification"]["verdict"], "unresolved")
        self.assertEqual(payload["verification"]["peers"][0]["reason"],
                         "completion-marker-evidence-conflict")
        route = dict(self.route, route_hash="sha256:" + "9" * 64)
        with mock.patch.object(artifact_lifecycle, "bind_existing_runtime_route",
                               return_value=(object(), route)):
            wrong = self.payload()
        self.assertEqual(wrong["verification"]["verdict"], "unresolved")
        self.assertEqual(wrong["verification"]["reason"], "route-hash-binding-mismatch")

    def test_incomplete_required_report_input_declaration_cannot_pass(self):
        route_copy = json.loads(json.dumps(self.route))
        route_copy["nodes"][1]["inputs"] = ["report/REPORT.md"]
        with mock.patch.object(artifact_lifecycle, "bind_existing_runtime_route",
                               return_value=(object(), route_copy)):
            payload = self.payload()
        self.assertEqual(payload["verification"]["verdict"], "unresolved")
        self.assertEqual(payload["required_input_observation"]["state"], "failed")
        self.assertEqual(payload["verification"]["reason"],
                         "required-input-declaration-incomplete")

    def test_corrected_current_pass_retains_historical_fail(self):
        leg = "independent-verify"
        prior_verdict = self.cycle / "artifacts" / "reviews" / "prior.json"
        prior_verdict.write_bytes(encoded({"schema": "acfu-eval-verdict-v1", "route": ROUTE_ID,
                                           "leg": leg, "verdict": "FAIL", "round": 1}))
        prior_marker = {"schema_version": 2, "stage_authority": "attempt",
                        "route_id": ROUTE_ID, "node_id": leg,
                        "evidence": {"path": str(prior_verdict),
                                     "sha256": sha(prior_verdict.read_bytes())}}
        prior_marker_path = self.marker_dir / f"{leg}.1.json"
        prior_marker_path.write_bytes(encoded(prior_marker))
        marker_path = self.marker_dir / f"{leg}.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["stage_authority"] = "revision"
        marker["revision"] = {"of_sequence": 1,
                              "of_marker_sha256": "sha256:" + sha(prior_marker_path.read_bytes())}
        marker_path.write_bytes(encoded(marker))
        payload = self.payload()
        self.assertEqual(payload["verification"]["verdict"], "PASS")
        self.assertEqual(payload["verification"]["history"],
                         [{"sequence": 1, "round": 1, "verdict": "FAIL"}])

    def test_cairn_exact_revision_join_and_local_uncertainty(self):
        report_revision = next(row for row in self.document["artifact_revisions"]
                               if row["locator"]["path"] == "artifacts/report/REPORT.md")
        row = {"stable_id": report_revision["artifact_id"], "artifact_root_id": ROOT_ID,
               "namespace_state": "active", "integrity": {"verified": True,
                 "expected_digest": report_revision["content_digest"]}, "freshness": {"stale": False}}
        joined = projection.project_rows([row], artifact_root=self.root, jobs=self.jobs)
        self.assertEqual(joined[0]["payload"]["verification"]["verdict"], "PASS")
        wrong = dict(row, artifact_root_id="root_" + "0" * 32)
        unresolved = projection.project_rows([wrong], artifact_root=self.root, jobs=self.jobs)
        self.assertEqual(unresolved[0]["payload"]["verification"]["verdict"], "unresolved")
        self.assertEqual(unresolved[0]["payload"]["verification"]["reason"],
                         "remote-root-binding-mismatch")
        stale = dict(row, partial=True)
        unresolved = projection.project_rows([stale], artifact_root=self.root, jobs=self.jobs)
        self.assertEqual(unresolved[0]["payload"]["verification"]["reason"],
                         "remote-projection-stale")

    def test_obligation_read_distinguishes_pending_unknown_and_not_applicable(self):
        self.assertEqual(projection._obligations(self.jobs, [])["state"], "not-applicable")
        metadata = [("done", {"peer_obligation_id": "peer-fixture"})]
        with mock.patch.object(peer_obligations, "ObligationStore") as store_type:
            store_type.return_value.get.return_value = {"state": "pending"}
            self.assertEqual(projection._obligations(self.jobs, metadata)["state"], "pending")
            store_type.return_value.get.return_value = None
            unknown = projection._obligations(self.jobs, metadata)
            self.assertEqual(unknown["state"], "unknown")
            self.assertEqual(unknown["items"][0]["reason"], "obligation-unreadable")
            store_type.return_value.get.return_value = {"state": "complete"}
            self.assertEqual(projection._obligations(self.jobs, metadata)["state"], "complete")
        batch_id = "batch-" + "1" * 32
        batch_meta = [("done", {"batch_obligation_id": batch_id})]
        with mock.patch.object(dispatch_batch_obligations, "read",
                               return_value={"state": "observing"}):
            self.assertEqual(projection._obligations(self.jobs, batch_meta)["state"], "pending")
        with mock.patch.object(dispatch_batch_obligations, "read", return_value=None):
            self.assertEqual(projection._obligations(self.jobs, batch_meta)["state"], "unknown")

    def test_cli_subprocess_json_text_and_scriptless_html(self):
        cli = Path(__file__).resolve().parents[1] / "tools" / "report-bundle.py"
        common = [sys.executable, str(cli)]
        base = Path(self.tmp.name)
        config_path = base / "subprocess-fixture.json"
        config_path.write_bytes(encoded({"source": str(self.source), "cycle": str(self.cycle),
                                         "root": str(self.root), "jobs": str(self.jobs),
                                         "route_path": str(self.route_path),
                                         "marker_dir": str(self.marker_dir),
                                         "route": self.route, "cycle_id": CYCLE_ID,
                                         "campaign_id": CAMP_ID, "root_id": ROOT_ID,
                                         "route_hash": ROUTE_HASH,
                                         "utilities": str(Path(__file__).resolve().parent)}))
        (base / "sitecustomize.py").write_text(SUBPROCESS_FIXTURE_ADAPTER, encoding="utf-8")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   PROJECTION_FIXTURE_CONFIG=str(config_path),
                   PYTHONPATH=str(base) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        before = tree_snapshot(base)
        for command in ("status", "read"):
            result = subprocess.run(common + [command, "--source", str(self.source),
                                  "--artifact-root", str(self.root), "--jobs", str(self.jobs),
                                  "--json"], text=True, capture_output=True, env=env, check=True)
            self.assertEqual(json.loads(result.stdout)["verification"]["verdict"], "PASS")
            self.assertEqual(result.stderr, "")
        text_result = subprocess.run(common + ["read", "--source", str(self.source),
                                       "--artifact-root", str(self.root), "--jobs", str(self.jobs)],
                                     text=True, capture_output=True, env=env, check=True)
        self.assertIn("검증 통과", text_result.stdout)
        self.assertIn("file://", text_result.stdout)
        html_result = subprocess.run(common + ["read", "--source", str(self.source),
                                      "--artifact-root", str(self.root), "--jobs", str(self.jobs),
                                      "--format", "html"], text=True, capture_output=True,
                                    env=env, check=True)
        self.assertIn('<html lang="ko">', html_result.stdout)
        self.assertNotIn("<script", html_result.stdout.lower())
        self.assertIn("검증 통과", html_result.stdout)
        self.assertIn("file://", html_result.stdout)
        self.assertEqual(tree_snapshot(base), before)


class ClosedFindingTests(ProjectionFixture):
    def real_currency(self, leg="independent-verify"):
        node = next(n for n in self.route["nodes"] if n["id"] == leg)
        node["dispatch_depth"] = 2
        node["completion_gate"] = "review"
        marker_path = self.marker_dir / f"{leg}.json"
        marker = json.loads(marker_path.read_text())
        marker.update(sequence=1, registry_digest=None, completion_gate="review",
                      dispatch_depth=2, transport="headless",
                      execution_surface="registered-headless", registered_worker=True,
                      fallback_hop="same-harness-headless")
        link = {key: marker.get(key) for key in (
            "schema_version", "route_id", "node_id", "attempt_id", "dispatch_depth",
            "transport", "execution_surface", "registered_worker", "fallback_hop")}
        link.update(evidence_sha256=marker["evidence"]["sha256"],
                    completion_marker=str(marker_path),
                    completion_marker_history=str(self.marker_dir / f"{leg}.1.json"))
        (self.marker_dir / f"{leg}.{marker['attempt_id']}.attempt.json").write_bytes(encoded(link))
        self.save_marker(leg, marker)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(dispatch_contract, "evidence_currency", REAL_EVIDENCE).start()
        mock.patch.object(dispatch_contract, "gate_currency", REAL_GATE).start()
        return node, marker_path, marker

    def save_marker(self, leg, marker):
        for name in (f"{leg}.json", f"{leg}.{marker['sequence']}.json"):
            (self.marker_dir / name).write_bytes(encoded(marker))

    def test_gates_off_tombstone_is_execution_current_but_read_unresolved(self):
        node, path, marker = self.real_currency()
        marker["state"] = "superseded-by-upstream-revision"
        self.save_marker(node["id"], marker)
        with mock.patch.dict(os.environ, HEARTING_GATES="off"):
            self.assertEqual(REAL_GATE(self.route, node, path, marker).state, "current")
            self.assertEqual(REAL_GATE(self.route, node, path, marker, observe=True).state, "superseded")
            before = tree_snapshot(Path(self.tmp.name))
            self.assertEqual(self.payload()["verification"]["verdict"], "unresolved")
            self.assertEqual(tree_snapshot(Path(self.tmp.name)), before)
            self.assertEqual(os.environ["HEARTING_GATES"], "off")

    def test_legacy_tombstone_retains_sequence_and_revision_provenance_checks(self):
        node, path, original = self.real_currency()
        for fields in ({"sequence": True}, {"sequence": "1"}, {"stage_authority": "revision"}):
            with self.subTest(fields=fields):
                marker = dict(original, state="superseded-by-upstream-revision", **fields)
                path.write_bytes(encoded(marker))
                (self.marker_dir / f"{node['id']}.1.json").write_bytes(encoded(marker))
                with mock.patch.dict(os.environ, HEARTING_GATES="off"):
                    self.assertEqual(REAL_GATE(self.route, node, path, marker).reason,
                                     "revision-provenance-invalid")

    def test_history_conflict_and_live_digest_edit_are_not_read_current(self):
        node, path, marker = self.real_currency()
        prior = dict(marker, route_hash="sha256:" + "0" * 64)
        (self.marker_dir / f"{node['id']}.1.json").write_bytes(encoded(prior))
        with mock.patch.dict(os.environ, HEARTING_GATES="off"):
            self.assertEqual(REAL_GATE(self.route, node, path, marker).state, "current")
            self.assertNotEqual(REAL_GATE(self.route, node, path, marker, observe=True).state, "current")
            self.assertEqual(self.payload()["verification"]["verdict"], "unresolved")
            self.save_marker(node["id"], marker)
            self.peer_paths[node["id"]].write_bytes(b"changed evidence")
            self.assertEqual(REAL_EVIDENCE(self.route, node, path, marker).state, "current")
            self.assertEqual(REAL_EVIDENCE(self.route, node, path, marker, observe=True).state,
                             "revised-unrecorded")

    def test_attempt_hash_mismatch_retains_execution_compatibility(self):
        node, path, marker = self.real_currency()
        meta = self.terminal_metadata(node["id"])
        meta["route_hash"] = "sha256:" + "0" * 64
        meta["note"] = "completed-marker"
        lines = [self.registry_line(meta)]
        with mock.patch.dict(os.environ, HEARTING_GATES="off"):
            self.assertEqual(REAL_READINESS(self.route, node, marker, self.jobs,
                                           registry_lines=lines).state, "ready")
            result = REAL_READINESS(self.route, node, marker, self.jobs,
                                    registry_lines=lines, observe=True)
            self.assertEqual(result.reason, "attempt-route-hash-mismatch")
            with mock.patch.object(dispatch_contract, "completion_attempt_readiness", REAL_READINESS):
                self.jobs.write_text(lines[0] + "\n")
                self.assertEqual(self.payload()["verification"]["verdict"], "unresolved")

    def test_real_lineage_source_hash_mismatch_cannot_pass(self):
        parent = dict(self.route, route_id="rt-" + "a" * 16)
        parent["route_hash"] = REAL_ROUTE_HASH(parent)
        routes = self.root / ".runtime" / "routes"
        routes.mkdir(parents=True)
        (routes / f"{parent['route_id']}.json").write_bytes(encoded(parent))
        self.route.update(continuation_contract_version=1, source_route_id=parent["route_id"],
                          source_route_hash="sha256:" + "0" * 64)
        self.route["route_hash"] = REAL_ROUTE_HASH(self.route)
        self.document["routes"][0]["route_hash"] = self.route["route_hash"]
        self.route_path.write_bytes(encoded(self.route))
        (self.cycle / "manifest.json").write_bytes(encoded(self.document))
        with mock.patch.dict(os.environ, HEARTING_GATES="off"), mock.patch.object(
                route_identity, "route_hash", REAL_ROUTE_HASH), mock.patch.object(
                route_lineage, "verified_route_lineage", REAL_LINEAGE):
            self.assertEqual(len(REAL_LINEAGE(self.route)), 2)
            with self.assertRaises(route_lineage.RouteLineageError):
                REAL_LINEAGE(self.route, observe=True)
            before = tree_snapshot(Path(self.tmp.name))
            self.assertEqual(self.payload()["verification"]["verdict"], "unresolved")
            self.assertEqual(tree_snapshot(Path(self.tmp.name)), before)

    def terminal_metadata(self, leg):
        work = Path(self.tmp.name) / "work"
        work.mkdir(exist_ok=True)
        self.route["cwd"] = str(work)
        self.route_path.write_bytes(encoded(self.route))
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(.1)"],
                                 start_new_session=True)
        try:
            identity = dispatch_contract.process_launch_identity(child.pid)
        finally:
            child.wait(timeout=5)
        return dict(identity, attempt_schema_version="2", dispatch_depth="2", transport="headless",
                    execution_surface="registered-headless", registered_worker="1",
                    fallback_hop="same-harness-headless", worker_type="review",
                    artifact_root=str(self.root), route_id=ROUTE_ID, route_hash=ROUTE_HASH,
                    route_node=leg, attempt_id=self.attempts[leg], note="completed-review-blocking")

    def registry_line(self, meta):
        return "\t".join(["fixture", "done", self.route["cwd"], self.route["cwd"], "-",
                            ",".join(f"{key}={value}" for key, value in meta.items())])

    def terminal_fail(self):
        leg = "independent-verify"
        self.revise_peer(leg, "FAIL")
        (self.marker_dir / f"{leg}.json").unlink()
        meta = self.terminal_metadata(leg)
        log = Path(self.tmp.name) / "review.log"
        text = f"artifact: {self.peer_paths[leg]}\nverdict: FAIL\nblocker: scientific finding\n"
        log.write_bytes(encoded({"type": "item.completed", "item": {"type": "agent_message",
                              "text": text}}) + encoded({"type": "turn.completed"}))
        meta["log_file"] = str(log)
        self.jobs.write_text(self.registry_line(meta) + "\n")
        return meta

    def test_markerless_fail_uses_real_terminal_inspector_and_preserves_axes(self):
        self.terminal_fail()
        with mock.patch.dict(os.environ, AGENT_ARTIFACT_ROOT=str(self.root)):
            before = tree_snapshot(Path(self.tmp.name))
            result = self.payload()
            self.assertEqual(result["verification"]["verdict"], "FAIL")
            self.assertEqual(result["completion"]["state"], "complete")
            self.assertEqual(result["required_input_observation"]["state"], "confirmed")
            self.assertEqual(tree_snapshot(Path(self.tmp.name)), before)
            self.revise_peer("independent-verify-alternative", input_digest="0" * 64)
            self.assertEqual(self.payload()["verification"]["verdict"], "FAIL")
            self.assertEqual(self.payload()["required_input_observation"]["state"], "failed")

    def test_markerless_fail_wrong_attempt_digest_conflict_and_live_retry_are_unresolved(self):
        meta = self.terminal_fail()
        with mock.patch.dict(os.environ, AGENT_ARTIFACT_ROOT=str(self.root)):
            for key, value in (("attempt_id", "att-wrong"), ("route_hash", "wrong"),
                               ("terminal_conflict", "1")):
                bad = dict(meta, **{key: value})
                self.jobs.write_text(self.registry_line(bad) + "\n")
                self.assertEqual(self.payload()["verification"]["verdict"], "unresolved", key)
            retry = dict(meta, attempt_id="att-active-retry")
            self.jobs.write_text(self.registry_line(meta) + "\n" +
                                self.registry_line(retry).replace("\tdone\t", "\trunning\t") + "\n")
            self.assertEqual(self.payload()["verification"]["verdict"], "unresolved")
            self.jobs.write_text(self.registry_line(meta) + "\n")
            # The marker is intentionally absent: change the bound input and manifest only.
            value = json.loads(self.peer_paths["independent-verify"].read_text())
            value["inputs"][next(iter(value["inputs"]))] = "0" * 64
            path = self.peer_paths["independent-verify"]
            path.write_bytes(encoded(value))
            for row in self.document["artifact_revisions"]:
                if row["locator"]["path"] == "artifacts/reviews/eval-verdict.json":
                    row.update(content_digest="sha256:" + sha(path.read_bytes()), byte_size=path.stat().st_size)
            (self.cycle / "manifest.json").write_bytes(encoded(self.document))
            self.assertEqual(self.payload()["verification"]["verdict"], "unresolved")

    def test_markerless_fail_ignores_unrelated_owner_and_worker_rows(self):
        meta = self.terminal_fail()
        owner = dict(meta, attempt_id="att-unrelated-owner", dispatch_depth="1",
                     worker_type="owner", unit="_kernel/owner", route_node="_owner",
                     owner_route_id=ROUTE_ID, owner_route_hash=ROUTE_HASH)
        worker = dict(meta, attempt_id="att-unrelated-worker", route_node="other-review")
        with mock.patch.dict(os.environ, AGENT_ARTIFACT_ROOT=str(self.root)):
            for unrelated in (owner, worker):
                for status in ("done", "running"):
                    line = self.registry_line(unrelated).replace("\tdone\t", f"\t{status}\t")
                    for before in (True, False):
                        rows = [line, self.registry_line(meta)] if before else [self.registry_line(meta), line]
                        self.jobs.write_text("\n".join(rows) + "\n")
                        snapshot = tree_snapshot(Path(self.tmp.name))
                        result = self.payload()
                        self.assertEqual(result["verification"]["verdict"], "FAIL")
                        self.assertEqual(result["required_input_observation"]["state"], "confirmed")
                        self.assertEqual(tree_snapshot(Path(self.tmp.name)), snapshot)
            invalid_exact = dict(owner, attempt_id=meta["attempt_id"])
            self.jobs.write_text(self.registry_line(invalid_exact) + "\n")
            self.assertEqual(self.payload()["verification"]["verdict"], "unresolved")

    def test_terminal_fail_observation_decodes_unpadded_paths_for_all_lengths(self):
        meta = self.terminal_fail()
        original = self.peer_paths["independent-verify"]
        raw = original.read_bytes()
        node = next(node for node in self.route["nodes"] if node["id"] == "independent-verify")
        lengths = set()
        with mock.patch.dict(os.environ, AGENT_ARTIFACT_ROOT=str(self.root)):
            for suffix in ("a", "aa", "aaa"):
                path = original.with_name(f"evidence-{suffix}.json")
                path.write_bytes(raw)
                lengths.add(len(str(path).encode()) % 3)
                text = f"artifact: {path}\nverdict: FAIL\nblocker: scientific finding\n"
                Path(meta["log_file"]).write_bytes(encoded({
                    "type": "item.completed", "item": {"type": "agent_message", "text": text}})
                    + encoded({"type": "turn.completed"}))
                before = tree_snapshot(Path(self.tmp.name))
                terminal = dispatch_contract.observe_terminal_review_failure(
                    self.route, node, meta["attempt_id"], path, sha(raw), [self.registry_line(meta)])
                self.assertEqual(terminal["verdict"], "FAIL")
                self.assertEqual(tree_snapshot(Path(self.tmp.name)), before)
                with self.assertRaisesRegex(ValueError, "terminal-review-evidence-conflict"):
                    dispatch_contract.observe_terminal_review_failure(
                        self.route, node, meta["attempt_id"], path, "0" * 64, [self.registry_line(meta)])
        self.assertEqual(lengths, {0, 1, 2})

    def test_reused_historical_path_never_substitutes_current_pass(self):
        self.real_currency("independent-verify-alternative")
        node, path, prior = self.real_currency()
        leg = node["id"]
        self.revise_peer(leg, "FAIL")
        prior = json.loads(path.read_text())
        self.save_marker(leg, prior)
        link_path = self.marker_dir / f"{leg}.{prior['attempt_id']}.attempt.json"
        link = json.loads(link_path.read_text())
        link["evidence_sha256"] = prior["evidence"]["sha256"]
        link_path.write_bytes(encoded(link))
        prior_raw = (self.marker_dir / f"{leg}.1.json").read_bytes()
        self.revise_peer(leg, "PASS")
        current = json.loads(path.read_text())
        current.update(sequence=2, stage_authority="revision",
                       revision={"of_sequence": 1, "of_marker_sha256": sha(prior_raw),
                                 "evidence_sha256": current["evidence"]["sha256"]})
        self.save_marker(leg, current)
        with mock.patch.dict(os.environ, HEARTING_GATES="off"):
            self.assertEqual(REAL_GATE(self.route, node, path, current, observe=True).state, "current")
            result = self.payload()
            self.assertEqual(result["verification"]["verdict"], "PASS")
            self.assertEqual(result["verification"]["history"], [{"sequence": 1,
                "verdict": "unresolved", "reason": "historical-evidence-unbound"}])

    def test_existing_scope_disclosures_travel_with_peer_payload(self):
        leg = "independent-verify"
        path = self.peer_paths[leg]
        value = json.loads(path.read_text())
        value.update(unverified=["browser observation"], fallback={"approved": True})
        path.write_bytes(encoded(value))
        self.revise_peer(leg)
        payload = self.payload()
        peer = next(row for row in payload["verification"]["peers"] if row["leg"] == leg)
        self.assertEqual(peer["limitations"], {"unverified": ["browser observation"],
                                             "fallback": {"approved": True}})

    def test_fail_and_deleted_required_peer_output_do_not_confirm_inputs(self):
        self.revise_peer("independent-verify", "FAIL")
        self.peer_paths["independent-verify-alternative"].unlink()
        result = self.payload()
        self.assertEqual(result["verification"]["verdict"], "FAIL")
        self.assertEqual(result["required_input_observation"]["state"], "failed")
        self.assertIn("peer-output-unreadable", result["required_input_observation"]["reasons"])


def tree_snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[rel] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[rel] = ("file", sha(path.read_bytes()))
        elif path.is_dir():
            result[rel] = ("dir",)
    return result


SUBPROCESS_FIXTURE_ADAPTER = r'''import json, os, sys
from pathlib import Path
from types import SimpleNamespace
cfg = json.loads(Path(os.environ["PROJECTION_FIXTURE_CONFIG"]).read_text())
sys.path.insert(0, cfg["utilities"])
import artifact_lifecycle, artifact_locator, artifact_manifest, artifact_reader
import dispatch_contract, route_identity, route_lineage
source, cycle = Path(cfg["source"]), Path(cfg["cycle"])
artifact_reader.bucket_dirs = lambda *_a, **_kw: [(source, {"campaign_id": cfg["campaign_id"], "cycle_id": cfg["cycle_id"]})]
artifact_lifecycle.read_admitted_cycle = lambda *_a, **_kw: {"cycle_id": cfg["cycle_id"]}
artifact_lifecycle.read_root_identity = lambda *_a, **_kw: SimpleNamespace(artifact_root_id=cfg["root_id"])
artifact_locator.find_path_by_id = lambda *_a, **_kw: cycle
artifact_manifest.validate = lambda *_a, **_kw: SimpleNamespace(ok=True)
artifact_manifest.manifest_digest = lambda *_a, **_kw: "sha256:" + "0" * 64
artifact_lifecycle.canonical_route_path = lambda *_a, **_kw: Path(cfg["route_path"])
artifact_lifecycle.bind_existing_runtime_route = lambda *_a, **_kw: (object(), cfg["route"])
route_identity.route_hash = lambda *_a, **_kw: cfg["route_hash"]
route_lineage.verified_route_lineage = lambda *_a, **_kw: [cfg["route"]]
artifact_lifecycle.read_manifest_snapshots = lambda *_a, **_kw: []
artifact_lifecycle.evaluate_cycle_completion = lambda *_a, **_kw: SimpleNamespace(state="complete", reasons=[])
artifact_lifecycle._load_capability_route = lambda: SimpleNamespace(
    completion_dir=lambda *_a, **_kw: Path(cfg["marker_dir"]))
dispatch_contract.evidence_currency = lambda *_a, **_kw: SimpleNamespace(state="current")
dispatch_contract.gate_currency = lambda *_a, **_kw: SimpleNamespace(state="current")
dispatch_contract.completion_attempt_readiness = lambda *_a, **_kw: SimpleNamespace(state="ready")
'''


if __name__ == "__main__":
    unittest.main(verbosity=2)
