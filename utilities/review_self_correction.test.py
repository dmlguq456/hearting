#!/usr/bin/env python3
"""Round1 reproductions use real settlement, revision and strict consumers."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import os
from pathlib import Path
import threading
import unittest
from unittest import mock
from split_module_fixture import patch_split


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


F = load("correction_fixture", "capability_route.test.py")
W = load("correction_coupled", "work_start.test.py")
R, D = F.R, F.D


class CorrectionTest(unittest.TestCase):
    def fixture(self, **options):
        f = F.HistoricalReviewSelfDispositionTest()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.historical_fixture(**options)
        return f

    def snapshot(self, f):
        return {p: p.read_bytes() for p in f.directory.glob("*.json")}

    def test_provenance_blocked_joint_basis_and_original_bytes(self):
        for harness in ("codex", "claude", "opencode"):
            for entrance in ("complete", "writer"):
                with self.subTest(harness=harness, entrance=entrance):
                    f = self.fixture(harness=harness, ship=True, provenance_blocked=True,
                                     legacy=harness == "codex", append_hash=harness == "codex")
                    marker = f.replay(entrance)
                    self.assertEqual(marker["review_independence"], "independent")
                    self.assertEqual(marker["reviewer_disposition"]["settled_basis"], "historical-provenance-blocked")
                    self.assertEqual(marker["reviewer_disposition"]["authority_basis"]["terminal_verdict"], "BLOCKED")
                    self.assertEqual(marker["round_census"], f.origin["round_census"])
                    self.assertEqual(f.replay("complete"), marker)
                    self.assertEqual(f.replay("writer"), marker)
                    for path, raw in f.preserved.items():
                        self.assertEqual(path.read_bytes(), raw)
                    self.assertTrue(R._marker_identity_row(f.route, f.node, f.node["id"],
                                    f.node["completion_gate"], jobs=f.jobs, exact_terminal=True)["passed"])
                    metadata=R._find_attempt_row_metadata(f.jobs,"att-review-self")
                    proof = D._marker_bound_prepare_marker_proof(metadata, "att-review-self")
                    self.assertIsNotNone(proof)
                    f.report.write_text(f.report.read_text() + "changed after receipt\n")
                    self.assertIsNone(D._marker_bound_current_marker(metadata, "att-review-self", proof)[0])
                    self.assert_refused(f)

    def test_provenance_blocked_refusals_before_publication(self):
        for case in ("generic", "FAIL", "substantive-blocked", "report-mutation", "no-hash", "hash-exit",
                     "hash-spoof", "late-hash", "duplicate-hash", "no-claim", "late-claim", "wrong-report",
                     "wrong-reviewer", "wrong-cycle", "wrong-gate", "wrong-route", "wrong-round", "wrong-request",
                     "wrong-pins", "explicit-content-FAIL", "explicit-content-BLOCKED", "missing-embedded-receipt",
                     "structured-content-FAIL", "structured-content-BLOCKED", "conflicting-closed-receipt"):
            with self.subTest(case=case):
                changes={"wrong-report":("review_artifact","reviews/release/other.md"),
                    "wrong-reviewer":("guard_session_id","att-foreign"), "wrong-cycle":("cycle_id","cyc_foreign"),
                    "wrong-gate":("completion_gate","foreign"), "wrong-route":("route_id","rt-foreign"),
                    "wrong-round":("round",3),"wrong-request":("request_digest","foreign"),"wrong-pins":("bound_inputs_verified",31)}
                f = self.fixture(ship=True, legacy=True, provenance_blocked=True,
                                 evidence_changes=dict([changes[case]]) if case in changes else None,
                                 report_prefix=(json.dumps({"verdict":case.removeprefix("structured-content-"),"gate_met":False})+"\n"
                                     if case.startswith("structured-content-") else
                                     '{"attempt_id":"att-foreign","status":"closed"}\n' if case=="conflicting-closed-receipt" else ""))
                events = [json.loads(line) for line in f.log.read_text().splitlines()]
                if case == "generic":
                    events[-2]["item"]["text"] = events[-2]["item"]["text"].replace(D.REVIEW_SELF_PROVENANCE_BLOCKER, "review could not finish")
                elif case in ("FAIL", "substantive-blocked"):
                    if case == "FAIL": events[-2]["item"]["text"] = events[-2]["item"]["text"].replace("verdict: BLOCKED", "verdict: FAIL")
                    else: f.evidence.write_text(f.evidence.read_text().replace('"verdict": "PASS"', '"verdict": "BLOCKED"'))
                elif case == "report-mutation": f.report.write_text(f.report.read_text()+"changed before disposition\n")
                elif case == "no-hash": events.pop(1)
                elif case == "hash-exit": events[1]["item"]["exit_code"] = 64
                elif case == "hash-spoof": events[1]["item"]["command"] = "echo sha256sum"
                elif case == "late-hash": events.append(events.pop(1))
                elif case == "duplicate-hash": events.insert(1, events[1])
                elif case == "no-claim": events.pop(0)
                elif case == "late-claim": events[0]["item"]["aggregated_output"] = events[0]["item"]["aggregated_output"].replace('"closed"', '"already-closed"')
                elif case.startswith("explicit-content-"): f.report.write_text("# Verdict: " + case.removeprefix("explicit-content-") + "\n" + f.report.read_text())
                elif case == "missing-embedded-receipt": f.report.write_text("## Code Review Results\nPASS in prose\n")
                f.log.write_text("\n".join(json.dumps(e) for e in events)+"\n")
                before = self.snapshot(f)
                with self.assertRaises(ValueError): f.replay()
                self.assertEqual(before, self.snapshot(f))

    def test_provenance_blocked_basis_cannot_be_forged_or_downgraded_to_old_basis(self):
        for field in ("report_path", "report_sha256", "hash_receipt", "publication_receipt", "terminal_sha256", "terminal_message_sha256", "terminal_verdict", "settled_basis"):
            with self.subTest(field=field):
                f=self.fixture(ship=True,legacy=True,provenance_blocked=True)
                marker=f.replay()
                if field=="settled_basis":
                    marker["reviewer_disposition"]["settled_basis"]="successful-quiescent-terminal"
                    marker["reviewer_disposition"].pop("authority_basis")
                else: marker["reviewer_disposition"]["authority_basis"][field]="forged"
                self.corrupt_head(f,marker)
                self.assert_refused(f)

    def test_provenance_blocked_terminal_message_bytes_remain_bound(self):
        f=self.fixture(ship=True,legacy=True,provenance_blocked=True)
        f.replay()
        # The normalized handoff remains identical; its registered receipt
        # bytes still cannot change after disposition publication.
        f.log.write_text(f.log.read_text().replace("blocker: v3.28.6", "blocker:  v3.28.6"))
        self.assert_refused(f)

    def test_provenance_blocked_observation_leaves_locator_indexes_alone(self):
        import artifact_locator
        f=self.fixture(ship=True,legacy=True,provenance_blocked=True)
        f.replay()
        index=f.artifact/"campaigns"/artifact_locator.INDEX_JSON
        index.unlink()
        before={p:p.read_bytes() for p in f.base.rglob('*') if p.is_file()}
        canonical=f.directory/(f.node["id"]+".json")
        self.assertEqual(D.gate_currency(f.route,f.node,canonical,observe=True).state,"current")
        self.assertEqual(before,{p:p.read_bytes() for p in f.base.rglob('*') if p.is_file()})

    def duplicate_report_hash_output(self, f, identical):
        events = [json.loads(line) for line in f.log.read_text().splitlines()]
        item = events[1]["item"]
        report_line, evidence_line = item["aggregated_output"].splitlines()
        digest, path = report_line.split("  ", 1)
        alias = f.report.parent / ".." / f.report.parent.name / f.report.name
        self.assertEqual(alias.resolve(), f.report.resolve())
        first_digest = digest if identical else "0" * 64
        item["aggregated_output"] = (
            first_digest + "  " + path + "\n" + digest + "  " + str(alias) + "\n" + evidence_line + "\n"
        )
        f.log.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        return events[1]

    def test_duplicate_normalized_hash_output_refused_by_both_writers(self):
        for identical in (False, True):
            for entrance in ("complete", "writer"):
                with self.subTest(identical=identical, entrance=entrance):
                    f = self.fixture(ship=True, legacy=True, provenance_blocked=True)
                    self.duplicate_report_hash_output(f, identical)
                    before = self.snapshot(f)
                    preserved = {path: path.read_bytes() for path in f.preserved}
                    with self.assertRaisesRegex(ValueError, "review-self-disposition-report-bytes-unverified"):
                        f.replay(entrance)
                    self.assertEqual(before, self.snapshot(f))
                    self.assertEqual(preserved, {path: path.read_bytes() for path in preserved})

    def test_duplicate_normalized_hash_output_refused_by_recorded_readers(self):
        import hashlib
        import ship_parent_deploy
        for identical in (False, True):
            with self.subTest(identical=identical):
                f = self.fixture(ship=True, legacy=True, provenance_blocked=True)
                marker = f.replay()
                metadata = R._find_attempt_row_metadata(f.jobs, "att-review-self")
                prepared = D._marker_bound_prepare_marker_proof(metadata, "att-review-self")
                self.assertIsNotNone(prepared)
                event = self.duplicate_report_hash_output(f, identical)
                # Even a forged basis citing the changed receipt cannot hide
                # contradictory or identical aliases of the same output path.
                marker["reviewer_disposition"]["authority_basis"]["hash_receipt"]["receipt_sha256"] = (
                    hashlib.sha256(json.dumps(event).encode()).hexdigest()
                )
                self.corrupt_head(f, marker)
                self.assert_refused(f)
                self.assertIsNone(D._marker_bound_current_marker(metadata, "att-review-self", prepared)[0])
                deploy = next(node for node in f.route["nodes"] if node["id"] == "deploy")
                with self.assertRaisesRegex(ValueError, '"release-review": "completion-marker-not-current"'):
                    ship_parent_deploy.reviews(f.route, deploy, f.jobs, json.loads(Path(f.prepared["request"]).read_text()))

    def corrupt_head(self, f, marker):
        R.atomic_write(f.directory / (f.node["id"] + ".json"), marker)
        R.atomic_write(f.directory / f"{f.node['id']}.{marker['sequence']}.json", marker)

    def assert_refused(self, f):
        before = {p: p.read_bytes() for p in f.base.rglob("*") if p.is_file()}
        canonical = f.directory / (f.node["id"] + ".json")
        self.assertNotEqual(D.gate_currency(f.route, f.node, canonical, observe=True).state, "current")
        metadata = R._find_attempt_row_metadata(f.jobs, "att-review-self")
        self.assertIsNone(D._marker_bound_prepare_marker_proof(metadata or f.meta, "att-review-self"))
        self.assertEqual(before, {p: p.read_bytes() for p in f.base.rglob("*") if p.is_file()})

    def test_r1_forged_disposition_fields_and_registered_bindings(self):
        for component in ("actor", "time", "time-type", "old-time", "future-time", "jobs", "foreign-jobs", "unsupported",
                          "FAIL", "BLOCKED", "absent-row", "duplicate-row", "route-hash", "history",
                          "pid", "pid-start", "pgid", "namespace", "worker-type", "subsession"):
            with self.subTest(component=component):
                f = self.fixture()
                marker = f.replay()
                if component in ("actor", "time", "time-type", "old-time", "future-time", "jobs", "foreign-jobs", "unsupported"):
                    disposition = marker["reviewer_disposition"]
                    if component == "actor": disposition["actor"] = "att-foreign"
                    elif component == "time": disposition["recorded_at"] = "forged"
                    elif component == "time-type": disposition["recorded_at"] = True
                    elif component == "old-time": disposition["recorded_at"] = "2000-01-01T00:00:00Z"
                    elif component == "future-time": disposition["recorded_at"] = "2099-01-01T00:00:00Z"
                    elif component == "jobs": disposition.pop("jobs")
                    elif component == "unsupported": disposition["proof"] = "invented"
                    else:
                        foreign = f.base / "foreign" / "jobs.log"
                        foreign.parent.mkdir(); foreign.write_bytes(f.jobs.read_bytes())
                        disposition["jobs"] = str(foreign)
                    self.corrupt_head(f, marker)
                elif component in ("FAIL", "BLOCKED"):
                    f.log.write_text(f.log.read_text().replace("verdict: PASS", "verdict: " + component))
                else:
                    lines = f.jobs.read_text().splitlines()
                    if component == "absent-row": lines = lines[1:]
                    elif component == "duplicate-row": lines.append(lines[0])
                    else:
                        key, value = {"route-hash": ("route_hash", "sha256:foreign"),
                                      "history": ("completion_marker_history", "/foreign/history.json"),
                                      "pid": ("pid", ""), "pid-start": ("pid_start", ""),
                                      "pgid": ("pgid", ""), "namespace": ("pid_ns", "unknown"),
                                      "worker-type": ("worker_type", "owner"),
                                      "subsession": ("stage_authority", "0")}[component]
                        lines[0] += "," + key + "=" + value
                    f.jobs.write_text("\n".join(lines) + "\n")
                self.assert_refused(f)

    def test_r1_legacy_log_must_be_the_exact_registered_log(self):
        f = self.fixture(legacy=True)
        marker = f.replay()
        alternate = f.base / "alternate.jsonl"
        alternate.write_bytes(f.log.read_bytes())
        marker["reviewer_disposition"]["original_claim"]["log_file"] = str(alternate)
        self.corrupt_head(f, marker)
        self.assert_refused(f)

    def test_r2_substitute_pass_artifact_refuses_both_entrances_and_reader(self):
        for entrance in ("writer", "complete"):
            for disposed in (False, True):
                with self.subTest(entrance=entrance, disposed=disposed):
                    f = self.fixture()
                    if disposed: f.replay()
                    alternate = f.output / "other-pass.json"
                    # Even identical bytes are a different judgment locator.
                    alternate.write_bytes(f.evidence.read_bytes())
                    f.log.write_text(f.log.read_text().replace(str(f.evidence), str(alternate)))
                    before = self.snapshot(f)
                    with self.assertRaisesRegex(ValueError, "terminal-artifact-mismatch|history-conflict|link-invalid"):
                        f.replay(entrance)
                    self.assertEqual(self.snapshot(f), before)
                    if disposed: self.assert_refused(f)

    def test_r2_supported_same_evidence_relative_and_symlink_aliases(self):
        for alias_kind in ("relative", "symlink"):
            with self.subTest(alias=alias_kind):
                f = self.fixture()
                if alias_kind == "relative": alias = str(f.evidence.relative_to(f.artifact))
                else:
                    alias_path = f.output / "alias.json"
                    alias_path.symlink_to(f.evidence); alias = str(alias_path)
                f.log.write_text(f.log.read_text().replace(str(f.evidence), alias))
                marker = f.replay()
                self.assertEqual(f.replay("writer"), marker)
                self.assertEqual(D.gate_currency(f.route, f.node, f.directory / (f.node["id"] + ".json"), observe=True).state, "current")

    def test_r1_failure_envelopes_refuse_publication_without_allocating_history(self):
        for entrance in ("writer", "complete"):
            for verdict in ("FAIL", "BLOCKED"):
                with self.subTest(entrance=entrance, verdict=verdict):
                    f = self.fixture()
                    f.log.write_text(f.log.read_text().replace("verdict: PASS", "verdict: " + verdict))
                    before = self.snapshot(f)
                    with self.assertRaisesRegex(ValueError, "terminal-unverified"):
                        f.replay(entrance)
                    self.assertEqual(before, self.snapshot(f))

    def test_r1_prepared_observer_proof_cannot_outlive_bound_terminal_bytes(self):
        f = self.fixture()
        f.replay()
        metadata = R._find_attempt_row_metadata(f.jobs, 'att-review-self')
        proof = D._marker_bound_prepare_marker_proof(metadata, 'att-review-self')
        self.assertIsNotNone(proof)
        self.assertIsNotNone(D._marker_bound_current_marker(metadata, 'att-review-self', proof)[0])
        f.log.write_text(f.log.read_text().replace('verdict: PASS', 'verdict: FAIL'))
        self.assertIsNone(D._marker_bound_current_marker(metadata, 'att-review-self', proof)[0])

    def setup_revision(self, f):
        node = next(n for n in f.route["nodes"] if n["id"] == "release-setup")
        evidence = f.output / "setup.md"
        evidence.write_text("prepared setup\n")
        axes = {"attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                "transport": "headless", "execution_surface": "inline", "registered_worker": False,
                "fallback_hop": "inline"}
        R.complete_node(f.route, node, node["id"], evidence, attempt_id="att-setup-inline", explicit_attempt_metadata=axes)
        evidence.write_text("revised setup\n")
        return node, evidence

    def revise(self, f, node, evidence):
        return R.publish_revision_locked(f.route, node["id"], evidence, basis="owner-correction",
                    reason="actual concurrent upstream revision", author_attempt_id="att-owner", jobs=f.jobs)

    def assert_tombstone(self, f):
        head = json.loads((f.directory / "release-review.json").read_text())
        self.assertEqual(head["state"], "superseded-by-upstream-revision")
        self.assertEqual(D.gate_currency(f.route, f.node, f.directory / "release-review.json", observe=True).state, "superseded")
        self.assertEqual(head["round_census"], f.origin["round_census"])
        for p, raw in f.preserved.items(): self.assertEqual(p.read_bytes(), raw)
        import ship_parent_deploy as ship
        request = json.loads(Path(f.prepared["request"]).read_text())
        deploy = next(n for n in f.route['nodes'] if n['id'] == 'deploy')
        with self.assertRaises(ValueError): ship.reviews(f.route, deploy, f.jobs, request)
        before = self.snapshot(f)
        for entrance in ("complete", "writer"):
            with self.assertRaises(ValueError): f.replay(entrance)
            self.assertEqual(self.snapshot(f), before)

    def test_r3_actual_upstream_revision_waits_at_every_disposition_boundary(self):
        for entrance in ("writer", "complete"):
            for boundary in ("before-allocation", "after-history", "before-replace"):
                with self.subTest(entrance=entrance, boundary=boundary):
                    f = self.fixture(ship=True)
                    node, evidence = self.setup_revision(f)
                    started, acquired, finished = threading.Event(), threading.Event(), threading.Event()
                    results, errors, threads = [], [], []
                    real_lock, real_once, real_atomic = R._exclusive_lock, R.write_once, R.atomic_write
                    import contextlib
                    @contextlib.contextmanager
                    def observed_lock(path):
                        with real_lock(path) as handle:
                            if threading.current_thread().name == "upstream-revision" and Path(path).name == '.release-review.completion.lock': acquired.set()
                            yield handle
                    def revision():
                        started.set()
                        try: results.append(self.revise(f, node, evidence))
                        except BaseException as exc: errors.append(exc)
                        finally: finished.set()
                    def launch():
                        if threads: return
                        t = threading.Thread(target=revision, name="upstream-revision", daemon=True)
                        threads.append(t); t.start()
                        self.assertTrue(started.wait(2))
                        # Revision's first sorted affected lock is this review lock.
                        self.assertFalse(acquired.wait(.1))
                        self.assertFalse(finished.is_set())
                    def once(path, payload):
                        if "reviewer_disposition" in payload and boundary == "before-allocation": launch()
                        result = real_once(path, payload)
                        if "reviewer_disposition" in payload and boundary == "after-history": launch()
                        return result
                    def atomic(path, payload):
                        if Path(path).name == "release-review.json" and "reviewer_disposition" in payload and boundary == "before-replace": launch()
                        return real_atomic(path, payload)
                    with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-owner"}), \
                         patch_split(R, "_exclusive_lock", side_effect=observed_lock), \
                         patch_split(R, "write_once", side_effect=once), \
                         patch_split(R, "atomic_write", side_effect=atomic):
                        marker = f.replay(entrance)
                        self.assertEqual(marker["sequence"], 2)
                        self.assertTrue(finished.wait(10), "revision deadlocked")
                        threads[0].join(1)
                    self.assertEqual(errors, [])
                    self.assertIn("release-review", results[0]["tombstoned"])
                    self.assertEqual(sorted(p.name for p in f.directory.glob("release-review.[0-9]*.json")),
                                     ["release-review.1.json", "release-review.2.json", "release-review.3.json"])
                    self.assert_tombstone(f)

    def test_r3_reciprocal_revision_and_crash_retry_cannot_restore_stale_head(self):
        for entrance in ("writer", "complete"):
            for interrupted in (False, True):
                with self.subTest(entrance=entrance, interrupted=interrupted):
                    f = self.fixture(ship=True)
                    node, evidence = self.setup_revision(f)
                    if interrupted:
                        real_once = R.write_once
                        def crash(path, payload):
                            result = real_once(path, payload)
                            if "reviewer_disposition" in payload: raise RuntimeError("interrupted")
                            return result
                        with patch_split(R, "write_once", side_effect=crash), self.assertRaisesRegex(RuntimeError, "interrupted"):
                            f.replay(entrance)
                    with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-owner"}): self.revise(f, node, evidence)
                    self.assert_tombstone(f)

    def test_r3_final_check_reads_actual_head_even_for_stale_supplied_marker(self):
        f = self.fixture()
        real_once = R.write_once
        canonical = f.directory / (f.node["id"] + ".json")
        changed = dict(f.origin, state="superseded-by-upstream-revision")
        def perturb(path, payload):
            result = real_once(path, payload)
            if "reviewer_disposition" in payload: R.atomic_write(canonical, changed)
            return result
        # The source-epoch integrity check can see the edited head (canonical no
        # longer equals its history) before the final origin check; either refusal
        # leaves the perturbed head in place.
        with patch_split(R, "write_once", side_effect=perturb), \
                self.assertRaisesRegex(ValueError, "origin-drift|terminal-artifact-mismatch"):
            f.replay()
        self.assertEqual(json.loads(canonical.read_text()), changed)

    def test_r3_reciprocal_concurrent_order_commits_revision_then_refuses_disposition(self):
        for entrance in ('writer', 'complete'):
            with self.subTest(entrance=entrance):
                f = self.fixture(ship=True)
                node, evidence = self.setup_revision(f)
                held, release, entered = threading.Event(), threading.Event(), threading.Event()
                errors, revisions = [], []
                real_atomic = R.atomic_write
                def atomic(path, payload):
                    if Path(path).name == 'release-setup.json' and payload.get('stage_authority') == 'revision':
                        held.set()
                        if not release.wait(5): raise RuntimeError('fixture-interleave-timeout')
                    return real_atomic(path, payload)
                def revise():
                    try: revisions.append(self.revise(f, node, evidence))
                    except BaseException as exc: errors.append(exc)
                def replay():
                    entered.set()
                    try: f.replay(entrance)
                    except BaseException as exc: errors.append(exc)
                with mock.patch.dict(os.environ, {'AGENT_DISPATCH_ATTEMPT_ID': 'att-owner'}), \
                     patch_split(R, 'atomic_write', side_effect=atomic):
                    upstream = threading.Thread(target=revise, daemon=True); upstream.start()
                    self.assertTrue(held.wait(5))
                    downstream = threading.Thread(target=replay, daemon=True); downstream.start()
                    self.assertTrue(entered.wait(2))
                    downstream.join(.1)
                    self.assertTrue(downstream.is_alive())
                    release.set()
                    upstream.join(10); downstream.join(10)
                    self.assertFalse(upstream.is_alive() or downstream.is_alive())
                self.assertEqual(len(revisions), 1)
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], ValueError)
                self.assert_tombstone(f)

    def test_r1_strict_ship_and_marker_observer_reject_reviewer_reproduction(self):
        import ship_parent_deploy as ship
        f = W.CoupledReviewSelfDispositionTest(); f.setUp(); self.addCleanup(f.doCleanups)
        f.test_public_correction_replacement_disposition_and_unchanged_strict_ship_consumption()
        original = json.loads((f.f.directory / "release-review.json").read_text())
        log = f.f.log.read_bytes()
        deploy = next(n for n in f.f.route["nodes"] if n["id"] == "deploy")
        for corruption in ("actor", "time", "FAIL", "BLOCKED", "different-artifact", "unsupported"):
            with self.subTest(corruption=corruption):
                marker = json.loads(json.dumps(original)); f.f.log.write_bytes(log)
                if corruption == "actor": marker["reviewer_disposition"]["actor"] = "att-foreign"
                elif corruption == "time": marker["reviewer_disposition"]["recorded_at"] = "forged"
                elif corruption == "unsupported": marker["reviewer_disposition"]["extra"] = True
                elif corruption == "different-artifact":
                    alternate = f.f.output / "other.json"; alternate.write_bytes(f.f.evidence.read_bytes())
                    f.f.log.write_text(f.f.log.read_text().replace(str(f.f.evidence), str(alternate)))
                else: f.f.log.write_text(f.f.log.read_text().replace("verdict: PASS", "verdict: " + corruption))
                self.corrupt_head(f.f, marker)
                before = self.snapshot(f.f)
                with self.assertRaises(ValueError): ship.reviews(f.f.route, deploy, f.jobs, f.request)
                self.assert_refused(f.f)
                self.assertEqual(self.snapshot(f.f), before)

    def test_r1_interrupted_recorded_actor_survives_real_owner_replacement(self):
        f = W.CoupledReviewSelfDispositionTest(); f.setUp(); self.addCleanup(f.doCleanups)
        owner = dict(f.owner); owner.pop("note"); owner.pop("failure_class")
        f.write_owner(owner, "running")
        # Use the fixture's actual writer module for the interruption.
        real_once = f.cr.write_once
        def crash(path, payload):
            result = real_once(path, payload)
            if "reviewer_disposition" in payload: raise RuntimeError("interrupted")
            return result
        with patch_split(f.cr, "write_once", side_effect=crash), self.assertRaisesRegex(RuntimeError, "interrupted"):
            f.f.replay()
        pending = json.loads((f.f.directory / "release-review.2.json").read_text())
        self.assertEqual(pending["reviewer_disposition"]["actor"], "att-owner")
        f.write_owner(f.owner, "done")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "CODEX_", "CLAUDE_", "OPENCODE_"))}
        env.update(CODEX_THREAD_ID="parent", AGENT_DISPATCH_JOBS=str(f.jobs), AGENT_ARTIFACT_CHECKPOINT="off")
        defaults = dict(W.W.start_work.__kwdefaults__)
        W.W.start_work.__kwdefaults__["run"] = f.native_owner_launch
        self.addCleanup(setattr, W.W.start_work, "__kwdefaults__", defaults)
        receipt = f.cli(["correct", "--jobs", str(f.jobs), "--attempt-id", "att-owner", "--message-file", str(f.answer)], env)[-1]
        self.assertEqual(receipt["state"], "running")
        current = f.cr._current_owner_attempt(f.jobs, "att-owner")
        self.assertNotEqual(current, "att-owner")
        # The inherited fake native launcher uses a fixed fixture timestamp.
        # Bind this actual replacement to its real admission time for the
        # historical actor's authority window, just as registration does.
        lines = f.jobs.read_text().splitlines()
        for index, line in enumerate(lines):
            fields = line.split('\t')
            if f.d.parse_registry_metadata(fields[5]).get('attempt_id') == current:
                fields[0] = f.cr.datetime.now(f.cr.timezone.utc).isoformat()
                lines[index] = '\t'.join(fields)
        f.jobs.write_text('\n'.join(lines) + '\n')
        marker = f.f.replay(actor=current)
        self.assertEqual(marker, pending)
        self.assertEqual(f.f.replay("writer", actor=current), marker)
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "unrelated-observer"}):
            self.assertEqual(D.gate_currency(f.f.route, f.f.node, f.f.directory / "release-review.json", observe=True).state, "current")
        import ship_parent_deploy as ship
        deploy = next(n for n in f.f.route["nodes"] if n["id"] == "deploy")
        self.assertEqual({p["attempt_id"] for p in ship.reviews(f.f.route, deploy, f.jobs, f.request).values()},
                         {"att-review-self", "att-security"})
        self.assertEqual(len(list(f.f.directory.glob("release-review.[0-9]*.json"))), 2)
        forged = json.loads(json.dumps(marker))
        forged['reviewer_disposition']['recorded_at'] = f.cr.datetime.now(f.cr.timezone.utc).isoformat()
        self.corrupt_head(f.f, forged)
        self.assert_refused(f.f)


if __name__ == "__main__":
    unittest.main()
