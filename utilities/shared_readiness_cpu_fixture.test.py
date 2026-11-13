#!/usr/bin/env python3
"""Narrow baseline/candidate verification entry for readiness measurements."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import argparse
import importlib.util
import json
from pathlib import Path
import traceback


HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "shared_readiness_cpu.test.py"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    if (args.baseline_root is None) != (args.evidence_dir is None):
        parser.error("--baseline-root and --evidence-dir must be supplied together")
    if args.baseline_root is None:
        spec = importlib.util.spec_from_file_location(
            "shared_readiness_cpu_sampler", HERE / "shared_readiness_cpu.test.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("sampler self-check entry cannot be loaded")
        sampler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sampler)
        checks = sampler.sampler_self_checks()
        print(json.dumps({
            "status": "sampler-self-checks-passed",
            "self_checks": checks,
            "paired_manual_measurement": "skipped; baseline and evidence inputs were not supplied",
        }, sort_keys=True))
        return 0
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("readiness-cpu-fixture.json", "readiness-cpu-comparison.md",
                  "readiness-cpu-fixture-failure.json",
                  "readiness-cpu-fixture-cleanup.json"):
        (args.evidence_dir / stale).unlink(missing_ok=True)
    spec = importlib.util.spec_from_file_location("shared_readiness_cpu_fixture", FIXTURE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"measurement fixture cannot be loaded: {FIXTURE}")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    try:
        evidence, evidence_json, comparison = fixture.run_measurements(
            args.baseline_root, args.evidence_dir)
        cleanup = evidence.get("fixture_cleanup") or {}
        if cleanup.get("status") != "pass":
            raise RuntimeError("measurement fixture cleanup was not verified")
    except BaseException as exc:
        cleanup_path = args.evidence_dir / "readiness-cpu-fixture-cleanup.json"
        cleanup = None
        if cleanup_path.is_file():
            try:
                cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cleanup = {"status": "unreadable"}
        failure = {
            "status": "failed-before-measurement-completion",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "baseline_root": str(args.baseline_root),
            "evidence_dir": str(args.evidence_dir),
            "fixture_cleanup": cleanup or {"status": "not-recorded"},
        }
        (args.evidence_dir / "readiness-cpu-fixture-failure.json").write_text(
            json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise
    (args.evidence_dir / "readiness-cpu-fixture-failure.json").unlink(missing_ok=True)
    print(json.dumps({
        "status": "measurement-evidence-written",
        "evidence_json": str(evidence_json),
        "comparison": str(comparison),
        "workload_bytes": evidence["workload"]["bytes"],
        "workload_sha256": evidence["workload"]["sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
