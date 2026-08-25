#!/usr/bin/env python3
"""W7C producer-lifecycle canary: 12 entry capabilities x direct/quick/standard.

Runs the full begin -> write -> (stage join) -> route close -> finalize
lifecycle for every entry-router capability at the three intensity classes in
a throw-away artifact root (`AGENT_ARTIFACT_ROOT` is a temporary directory,
`AGENT_HOME` is an isolated fixture), plus the negative and crash-recovery
cases the W7C contract requires.  Nothing under the real artifact root, the
installed harness, or the dispatch registry is touched.

Exit 0 only when every canary, negative, and recovery check passes.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import artifact_admission as adm  # noqa: E402
import artifact_identity as idm  # noqa: E402
import artifact_lifecycle as L  # noqa: E402
import artifact_manifest as m  # noqa: E402
import artifact_producer as P  # noqa: E402

_S = importlib.util.spec_from_file_location("route_for_canary", ROOT / "utilities" / "capability-route.py")
R = importlib.util.module_from_spec(_S)
_S.loader.exec_module(R)

ALL_PREDICATES = [
    "atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
    "no-artifact-handoff", "no-independent-verifier", "focused-verification",
]
INTENSITIES = ("direct", "quick", "standard")
BUCKET = {
    "analyze-project": "analysis_project", "analyze-user": "user_profile", "audit": "reviews",
    "autopilot-apply": "apply-log", "autopilot-code": "plans", "autopilot-design": "designs",
    "autopilot-draft": "documents", "autopilot-lab": "experiments", "autopilot-refine": "documents",
    "autopilot-research": "research", "autopilot-ship": "release-config", "autopilot-spec": "spec",
}
REPO_ID = "repo_" + "c" * 32
ROOT_ID = "root_" + "d" * 32


def gate_evidence():
    return {
        "spec_read": {"satisfied": True, "source": "canary"},
        "drift_verdict": "within-spec", "workflow_mode": "tracked",
        "artifact_guard": {"satisfied": True, "source": "canary"},
    }


def registered_headless():
    return {"candidates": [{
        "harness": "codex", "transport": "headless", "surface": "registered-headless",
        "status": "supported", "probe_source": "canary-probe", "probe_time": "2026-08-25T00:00:00Z",
    }]}


def dispatch_evidence():
    sandbox = R.WRAPPER_PARENT_SANDBOXES["codex"][0] if "codex" in R.WRAPPER_PARENT_SANDBOXES else "workspace-write"
    tuple_row = {
        "parent_harness": "codex", "parent_transport": "headless", "parent_sandbox": sandbox,
        "child_harness": "codex", "launch_authority": "conductor", "status": "supported",
        "probe_source": "canary-probe", "probe_time": "2026-08-25T00:00:00Z", "failure_class": "",
        "checked_worktree": str(R.ROOT.resolve()), "failure_scope": "none",
        "codex_command": "ok", "retry_on_isolated_worktree": 0,
    }
    return {"tuples": [tuple_row], "native_subagent": [{
        "harness": "codex", "transport": "headless", "execution_surface": "codex-native-subagent",
        "registered_worker": False, "status": "supported", "check_source": "canary-native-check",
    }]}


def compile_for(capability, mode, intensity, root):
    common = dict(cwd=R.ROOT, artifact_root=root, tracking="tracked", tracked_gate_evidence=gate_evidence())
    if intensity == "direct":
        return R.compile_route(capability, mode, "direct", predicates=ALL_PREDICATES, transport=None,
                               inline_reason="atomic-direct", **common)
    if intensity == "quick":
        return R.compile_route(capability, mode, "quick", predicates=[], transport=None,
                               registered_headless_evidence=registered_headless(), **common)
    return R.compile_route(capability, mode, intensity, predicates=[], transport="headless",
                           dispatch_evidence=dispatch_evidence(), **common)


def close_route(route, route_file, scratch):
    evidence = scratch / f"evidence-{route['route_id']}.txt"
    evidence.write_text("canary terminal evidence\n", encoding="utf-8")
    for node in route["nodes"]:
        if node.get("terminal") is not True:
            continue
        if node.get("dispatch_depth", 0) == 0:
            R.write_completion_marker(route, node, node["id"], evidence)
            continue
        metadata = {
            "attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
        }
        R.write_completion_marker(route, node, node["id"], evidence,
                                  attempt_id=f"att-canary-{node['id']}", attempt_metadata=metadata)
    outcome, _ = R.close_route(route, route_file, commit="b" * 40, summary="canary")
    if not outcome.get("terminal_gate_proven"):
        raise RuntimeError(f"terminal gate not proven: {outcome}")


def entry_capabilities():
    manifest = json.loads((ROOT / "harness-manifest.json").read_text(encoding="utf-8"))
    rows = []
    for name, spec in manifest["capabilities"].items():
        if spec.get("invocation", {}).get("class") == "entry-router":
            rows.append((name, (spec.get("modes") or ["default"])[0]))
    return rows


class Canary:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.root = workdir / "artifact-root"
        self.root.mkdir(parents=True)
        self.scratch = workdir / "scratch"
        self.scratch.mkdir()
        home = workdir / "agent-home"
        (home / "core").mkdir(parents=True)
        (home / "core" / "CORE.md").write_text("canary fixture\n", encoding="utf-8")
        os.environ["AGENT_HOME"] = str(home)
        for key in ("AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_ROOT"):
            os.environ.pop(key, None)
        P.activate(self.root, repository_id=REPO_ID, artifact_root_id=ROOT_ID,
                   w7={"campaign_id": "camp_6d5451e4267fc03e5f62b5069de6a3c4"})

    # -- one canary ----------------------------------------------------------
    def run_one(self, capability, mode, intensity):
        row = {"capability": capability, "mode": mode, "intensity": intensity, "checks": []}
        route = compile_for(capability, mode, intensity, self.root)
        binding = L.admit_runtime_route(self.root, route)
        route_file = Path(binding.route_file)
        begun = P.begin(self.root, route_file=route_file, capability=capability, intensity=intensity)
        assert begun["status"] == "begun", begun
        row["cycle_id"], row["campaign_id"], row["producer_id"] = begun["cycle_id"], begun["campaign_id"], begun["producer_id"]
        cycle_dir = Path(begun["cycle_dir"])
        assert sorted(os.listdir(cycle_dir)) == ["artifacts"], "ids issued before first write"
        row["checks"].append("ids-issued-before-first-write")
        bucket = BUCKET[capability]
        target = cycle_dir / "artifacts" / bucket / f"{capability}-{intensity}" / "final_report.md"
        verdict = P.check_write(self.root, target)
        assert verdict["verdict"] == "allow" and verdict["bucket"] == bucket, verdict
        row["checks"].append("check-write-allows-open-cycle")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# canary {capability} {intensity}\n", encoding="utf-8")
        legacy = P.check_write(self.root, self.root / bucket / "legacy.md")
        assert legacy["verdict"] == "deny" and legacy["reason"] == "legacy-top-level-write-denied", legacy
        row["checks"].append("legacy-top-level-denied-while-active")
        resolved, layout = P.resolve_output_dir(self.root, bucket, cycle_dir_hint=str(cycle_dir))
        assert layout == "cycle" and resolved == cycle_dir / "artifacts" / bucket
        row["checks"].append("resolve-output-dir-cycle")
        if intensity == "standard":
            stage_node = route["nodes"][0]["id"]
            joined = P.begin(self.root, route_file=route_file, capability=capability, intensity=intensity,
                             node_id=stage_node)
            assert joined["status"] == "resumed" and joined["cycle_id"] == begun["cycle_id"], joined
            (cycle_dir / "artifacts" / bucket / f"{capability}-{intensity}" / "stage.md").write_text(
                f"stage {stage_node}\n", encoding="utf-8")
            row["checks"].append(f"stage-worker-joined:{stage_node}")
        close_route(route, route_file, self.scratch)
        sealed = P.finalize(self.root, cycle_id=begun["cycle_id"])
        assert sealed["status"] == "sealed" and sealed["cycle_state"] == "completed", sealed
        document = json.loads((cycle_dir / "manifest.json").read_text(encoding="utf-8"))
        report = m.validate(document)
        assert report.ok, [v.code for v in report.violations]
        completion = L.evaluate_cycle_completion(document, content_root=cycle_dir, route_file=route_file,
                                                 expected_root_id=ROOT_ID)
        assert completion.status == "complete", completion.to_payload()
        assert begun["cycle_id"] in adm.load_index(self.root).manifests
        row["checks"].append("finalize-sealed-manifest-valid-index-applied")
        after = P.check_write(self.root, target)
        assert after["verdict"] == "deny" and after["reason"] == "cycle-not-open", after
        row["checks"].append("sealed-cycle-denies-writes")
        row["manifest_digest"] = sealed["manifest_digest"]
        row["artifact_count"] = sealed["artifact_count"]
        row["status"] = "pass"
        return row

    def negatives(self):
        rows = []

        def case(name, fn):
            try:
                fn()
                rows.append({"case": name, "status": "pass"})
            except Exception as exc:  # noqa: BLE001
                rows.append({"case": name, "status": "fail", "error": f"{type(exc).__name__}: {exc}",
                             "trace": traceback.format_exc()})

        def legacy_denied():
            for bucket in ("plans", "spec", "research", "documents", "analysis_project", "experiments", "designs"):
                verdict = P.check_write(self.root, self.root / bucket / "x" / "y.md")
                assert verdict["verdict"] == "deny", verdict
                assert verdict["reason"] == "legacy-top-level-write-denied", verdict
            with_exit = P.main(["check-write", "--artifact-root", str(self.root), "--file", str(self.root / "plans" / "z.md")])
            assert with_exit == P.BLOCKED
        case("legacy-top-level-new-write-hard-deny", legacy_denied)

        def shared_immutable():
            target = self.root / "shared" / "spec" / ("ref_" + "1" * 32) / "revisions" / ("rrev_" + "2" * 32) / "prd.md"
            verdict = P.check_write(self.root, target)
            assert verdict["reason"] == "shared-revision-immutable", verdict
        case("shared-revision-immutable", shared_immutable)

        def campaign_record_denied():
            verdict = P.check_write(self.root, self.root / "campaigns" / ("camp_" + "3" * 32) / "campaign.json")
            assert verdict["reason"] == "campaign-record-machine-managed", verdict
            verdict = P.check_write(self.root, self.root / "campaigns" / ("camp_" + "3" * 32) / "cycles" / ("cyc_" + "4" * 32) / "manifest.json")
            assert verdict["reason"] == "outside-cycle-artifacts", verdict
        case("campaign-and-manifest-records-machine-managed", campaign_record_denied)

        def unknown_cycle_denied():
            verdict = P.check_write(self.root, self.root / "campaigns" / ("camp_" + "3" * 32) / "cycles" / ("cyc_" + "4" * 32) / "artifacts" / "a.md")
            assert verdict["reason"] == "cycle-unknown", verdict
        case("unknown-cycle-denied", unknown_cycle_denied)

        def research_without_promotion():
            route = compile_for("autopilot-research", "technology", "direct", self.root)
            route_file = Path(L.admit_runtime_route(self.root, route).route_file)
            begun = P.begin(self.root, route_file=route_file, capability="autopilot-research", intensity="direct")
            out = Path(begun["cycle_dir"]) / "artifacts" / "research" / "neg-topic"
            out.mkdir(parents=True)
            (out / "report.md").write_text("r\n", encoding="utf-8")
            (out / "promotion.md").write_text("approved by user\n", encoding="utf-8")
            close_route(route, route_file, self.scratch)
            P.finalize(self.root, cycle_id=begun["cycle_id"])
            try:
                P.admit_shared(self.root, cycle_id=begun["cycle_id"], kind="research", source="research/neg-topic")
                raise AssertionError("research admitted without promotion")
            except P.ProducerError as exc:
                assert exc.code == "research-promotion-required", exc.code
            assert not (self.root / "shared" / "research").exists()
            admitted = P.admit_shared(self.root, cycle_id=begun["cycle_id"], kind="research", source="research/neg-topic",
                                      promote_research=True, promotion_evidence="research/neg-topic/promotion.md")
            assert admitted["promotion"]["kind"] == "explicit"
            revision_dir = Path(admitted["revision_dir"])
            assert P.check_write(self.root, revision_dir / "report.md")["verdict"] == "deny"
        case("research-shared-only-with-explicit-promotion", research_without_promotion)

        def spec_is_canonical_shared():
            route = compile_for("autopilot-spec", "update", "direct", self.root)
            route_file = Path(L.admit_runtime_route(self.root, route).route_file)
            begun = P.begin(self.root, route_file=route_file, capability="autopilot-spec", intensity="direct")
            out = Path(begun["cycle_dir"]) / "artifacts" / "spec"
            out.mkdir(parents=True)
            (out / "prd.md").write_text("# prd\n", encoding="utf-8")
            close_route(route, route_file, self.scratch)
            P.finalize(self.root, cycle_id=begun["cycle_id"])
            admitted = P.admit_shared(self.root, cycle_id=begun["cycle_id"], kind="spec", source="spec", key="prd")
            assert admitted["promotion"]["kind"] == "canonical-shared-kind"
            try:
                P.admit_shared(self.root, cycle_id=begun["cycle_id"], kind="plans", source="spec")
                raise AssertionError("plans admitted to shared")
            except P.ProducerError as exc:
                assert exc.code == "shared-kind-not-admissible"
        case("spec-canonical-shared-kind-plans-not-admissible", spec_is_canonical_shared)

        def open_route_cannot_complete():
            route = compile_for("autopilot-code", "debug", "direct", self.root)
            route_file = Path(L.admit_runtime_route(self.root, route).route_file)
            begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
            out = Path(begun["cycle_dir"]) / "artifacts" / "plans"
            out.mkdir(parents=True)
            (out / "plan.md").write_text("p\n", encoding="utf-8")
            try:
                P.finalize(self.root, cycle_id=begun["cycle_id"])
                raise AssertionError("finalized with open route")
            except P.ProducerError as exc:
                assert exc.code == "route-not-closed"
        case("finalize-requires-closed-route", open_route_cannot_complete)

        def empty_cycle_no_lineage():
            route = compile_for("autopilot-draft", "doc", "direct", self.root)
            route_file = Path(L.admit_runtime_route(self.root, route).route_file)
            begun = P.begin(self.root, route_file=route_file, capability="autopilot-draft", intensity="direct")
            close_route(route, route_file, self.scratch)
            outcome = P.finalize(self.root, cycle_id=begun["cycle_id"])
            assert outcome["status"] == "no-lineage"
            assert not Path(begun["cycle_dir"]).exists()
            assert begun["cycle_id"] not in adm.load_index(self.root).manifests
        case("empty-output-no-lineage-d9", empty_cycle_no_lineage)
        return rows

    def recovery(self):
        rows = []
        route = compile_for("autopilot-lab", "eval", "direct", self.root)
        route_file = Path(L.admit_runtime_route(self.root, route).route_file)
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-lab", intensity="direct")
        out = Path(begun["cycle_dir"]) / "artifacts" / "experiments" / "canary"
        out.mkdir(parents=True)
        (out / "final_report.md").write_text("e\n", encoding="utf-8")
        close_route(route, route_file, self.scratch)
        try:
            P.finalize(self.root, cycle_id=begun["cycle_id"], crash_after_manifest=True)
            rows.append({"case": "crash-after-manifest", "status": "fail", "error": "no crash raised"})
        except adm.AdmissionRecoveryRequired:
            pending = P.status(self.root)["pending_journals"]
            record = P.read_cycle_record(self.root, begun["cycle_id"])
            ok = begun["cycle_id"] in pending and record["state"] == "open"
            rows.append({"case": "crash-after-manifest-leaves-journal", "status": "pass" if ok else "fail",
                         "pending": pending, "record_state": record["state"]})
        recovered = P.recover(self.root)
        record = P.read_cycle_record(self.root, begun["cycle_id"])
        ok = (begun["cycle_id"] in recovered["producer"]["rolled_forward"] and record["state"] == "sealed"
              and begun["cycle_id"] in adm.load_index(self.root).manifests
              and P.status(self.root)["pending_journals"] == [])
        rows.append({"case": "recover-rolls-forward-to-sealed", "status": "pass" if ok else "fail",
                     "recovered": recovered["producer"], "record_state": record["state"]})
        # crash before the manifest commit point: journal only, cycle stays open
        route2 = compile_for("analyze-project", "paper", "direct", self.root)
        route_file2 = Path(L.admit_runtime_route(self.root, route2).route_file)
        begun2 = P.begin(self.root, route_file=route_file2, capability="analyze-project", intensity="direct")
        P._write_journal(self.root, begun2["cycle_id"], state="sealing", manifest_digest="sha256:" + "0" * 64,
                         cycle_path=os.path.relpath(begun2["cycle_dir"], self.root))
        recovered2 = P.recover(self.root)
        record2 = P.read_cycle_record(self.root, begun2["cycle_id"])
        ok2 = begun2["cycle_id"] in recovered2["producer"]["rolled_back"] and record2["state"] == "open"
        rows.append({"case": "crash-before-manifest-rolls-back-cycle-open", "status": "pass" if ok2 else "fail",
                     "recovered": recovered2["producer"], "record_state": record2["state"]})
        verify = adm.verify_index(self.root)
        rows.append({"case": "index-verifies-after-recovery", "status": "pass" if verify.ok else "fail",
                     "violations": [v.code for v in verify.violations]})
        return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--output", help="write the JSON report here")
    parser.add_argument("--keep", action="store_true", help="keep the temporary artifact root")
    args = parser.parse_args(argv)
    started = time.time()
    tmp = tempfile.mkdtemp(prefix="w7c-canary-")
    canary = Canary(Path(tmp))
    report = {"schema_version": 1, "kind": "w7c-producer-canary", "artifact_root": str(canary.root),
              "harness_source_commit": None, "canaries": [], "negatives": [], "recovery": []}
    try:
        report["harness_source_commit"] = os.popen(f"git -C {ROOT} rev-parse HEAD").read().strip() or None
    except OSError:
        pass
    for capability, mode in entry_capabilities():
        for intensity in INTENSITIES:
            try:
                report["canaries"].append(canary.run_one(capability, mode, intensity))
            except Exception as exc:  # noqa: BLE001
                report["canaries"].append({"capability": capability, "mode": mode, "intensity": intensity,
                                           "status": "fail", "error": f"{type(exc).__name__}: {exc}",
                                           "trace": traceback.format_exc()})
    report["negatives"] = canary.negatives()
    report["recovery"] = canary.recovery()
    report["producer_status"] = P.status(canary.root)
    passed = sum(1 for r in report["canaries"] if r.get("status") == "pass")
    neg_passed = sum(1 for r in report["negatives"] if r.get("status") == "pass")
    rec_passed = sum(1 for r in report["recovery"] if r.get("status") == "pass")
    report["summary"] = {
        "canaries": f"{passed}/{len(report['canaries'])}",
        "negatives": f"{neg_passed}/{len(report['negatives'])}",
        "recovery": f"{rec_passed}/{len(report['recovery'])}",
        "elapsed_seconds": round(time.time() - started, 2),
        "ok": passed == len(report["canaries"]) == 36 and neg_passed == len(report["negatives"])
              and rec_passed == len(report["recovery"]),
    }
    if not args.keep:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        report["artifact_root"] = None
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))
    for row in report["canaries"] + report["negatives"] + report["recovery"]:
        if row.get("status") != "pass":
            print("FAIL", json.dumps({k: v for k, v in row.items() if k != "trace"}, sort_keys=True), file=sys.stderr)
    return 0 if report["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
