#!/usr/bin/env python3
"""D-75 tests for `fleet_cutover_gate.py`.

Every fixture uses isolated temporary repos with their own `.agent_reports`;
the real fleet, canonical root, and registry are never touched.
"""
import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_producer as P  # noqa: E402
import fleet_cutover_gate as G  # noqa: E402

REPO_ID = "repo_" + "a" * 32
ROOT_ID = "root_" + "b" * 32


class GateTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._env = {k: os.environ.get(k) for k in ("AGENT_ARTIFACT_ROOT",)}
        os.environ.pop("AGENT_ARTIFACT_ROOT", None)
        self.addCleanup(self._restore)

        self.active_repo = self.base / "active-repo"
        self.legacy_repo = self.base / "legacy-repo"
        self.empty_repo = self.base / "empty-repo"
        self.malformed_repo = self.base / "malformed-repo"
        for repo in (self.active_repo, self.legacy_repo, self.empty_repo, self.malformed_repo):
            (repo / ".agent_reports").mkdir(parents=True, exist_ok=True)

        self.active_root = self.active_repo / ".agent_reports"
        P.activate(self.active_root, repository_id=REPO_ID, artifact_root_id=ROOT_ID)

        legacy_root = self.legacy_repo / ".agent_reports"
        (legacy_root / "plans").mkdir(parents=True, exist_ok=True)
        (legacy_root / "plans" / "a.md").write_text("legacy\n", encoding="utf-8")

        malformed_root = self.malformed_repo / ".agent_reports"
        P.producer_dir(malformed_root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(malformed_root).write_text("{", encoding="utf-8")

    def _restore(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def write_roster(self, repos, *, fleet_id="fleet-x", extra=None, indent=None):
        payload = {"schema_version": 1, "fleet_id": fleet_id, "repos": repos}
        if extra:
            payload.update(extra)
        path = self.base / "roster.json"
        path.write_text(json.dumps(payload, indent=indent), encoding="utf-8")
        return path

    def default_repos(self):
        return [
            {"repo_path": str(self.active_repo)},
            {"repo_path": str(self.legacy_repo)},
            {"repo_path": str(self.empty_repo)},
            {"repo_path": str(self.malformed_repo)},
        ]

    def run_cli(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = G.main(argv)
        out = buf.getvalue().strip()
        payload = json.loads(out) if out else None
        return exit_code, payload

    def snapshot(self):
        rows = []
        for repo in (self.active_repo, self.legacy_repo, self.empty_repo, self.malformed_repo):
            root = repo / ".agent_reports"
            if not root.exists():
                rows.append((str(root), None))
                continue
            for entry in sorted(root.rglob("*")):
                if entry.is_file():
                    stat = entry.stat()
                    rows.append((str(entry), stat.st_size, stat.st_mtime))
        return rows


class AuditTest(GateTestBase):
    def test_audit_reports_all_four_states(self):
        roster = self.write_roster(self.default_repos())
        _, payload = self.run_cli(["audit", "--roster", str(roster)])
        by_repo = {row["repo_path"]: row for row in payload["roots"]}
        self.assertEqual(by_repo[str(self.active_repo)]["state"], "active")
        self.assertEqual(by_repo[str(self.legacy_repo)]["state"], "inactive-with-legacy")
        self.assertEqual(by_repo[str(self.empty_repo)]["state"], "inactive-empty")
        self.assertEqual(by_repo[str(self.malformed_repo)]["state"], "malformed")

    def test_audit_state_matches_classify_root_for_every_row(self):
        roster = self.write_roster(self.default_repos())
        _, payload = self.run_cli(["audit", "--roster", str(roster)])
        for row in payload["roots"]:
            root = Path(row["resolved_root"])
            klass = P.classify_root(root)
            self.assertEqual(row["state"], klass["state"])
            self.assertEqual(row["state"] == "inactive-empty",
                             klass["state"] == "inactive-empty")

    def test_ambient_artifact_root_does_not_hijack_roster_rows(self):
        os.environ["AGENT_ARTIFACT_ROOT"] = str(self.active_root)
        roster = self.write_roster(self.default_repos())
        _, payload = self.run_cli(["audit", "--roster", str(roster)])
        resolved = {row["resolved_root"] for row in payload["roots"]}
        self.assertEqual(len(resolved), 4)
        for row in payload["roots"]:
            self.assertTrue(row["resolved_root"].startswith(row["repo_path"]))

    def test_missing_repo_directory_is_malformed(self):
        missing = self.base / "does-not-exist-repo"
        roster = self.write_roster([{"repo_path": str(missing)}])
        _, payload = self.run_cli(["audit", "--roster", str(roster)])
        row = payload["roots"][0]
        self.assertEqual(row["state"], "malformed")
        self.assertTrue(row["reason"].startswith("root-unresolved:"))

    def test_audit_mutates_nothing(self):
        roster = self.write_roster(self.default_repos())
        before = self.snapshot()
        self.run_cli(["audit", "--roster", str(roster)])
        after = self.snapshot()
        self.assertEqual(before, after)

    def test_roster_digest_is_indentation_independent(self):
        roster_a = self.write_roster(self.default_repos(), indent=None)
        _, payload_a = self.run_cli(["audit", "--roster", str(roster_a)])
        roster_b = self.write_roster(self.default_repos(), indent=4)
        _, payload_b = self.run_cli(["audit", "--roster", str(roster_b)])
        self.assertEqual(payload_a["roster_digest"], payload_b["roster_digest"])

    def test_roster_schema_errors_are_typed(self):
        cases = [
            ({"schema_version": 2, "fleet_id": "x", "repos": self.default_repos()}, None),
            ({"schema_version": 1, "fleet_id": "x", "repos": []}, "roster-empty"),
            ({"schema_version": 1, "fleet_id": "x", "repos": [{"repo_path": "relative/path"}]}, None),
            ({"schema_version": 1, "fleet_id": "x",
              "repos": [{"repo_path": str(self.active_repo)}, {"repo_path": str(self.active_repo)}]}, None),
        ]
        for payload, _unused in cases:
            path = self.base / "bad-roster.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            exit_code, result = self.run_cli(["audit", "--roster", str(path)])
            self.assertEqual(exit_code, G.INCOMPLETE)
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(result["reason"].startswith("roster-"))


class CompleteTest(GateTestBase):
    def active_only_roster(self):
        return self.write_roster([{"repo_path": str(self.active_repo)}])

    def all_four_roster(self):
        return self.write_roster(self.default_repos())

    def write_waivers(self, waivers):
        payload = {"schema_version": 1, "waivers": waivers}
        path = self.base / "waivers.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_complete_is_incomplete_when_any_root_is_inactive(self):
        roster = self.all_four_roster()
        exit_code, payload = self.run_cli(["complete", "--roster", str(roster)])
        self.assertEqual(payload["verdict"], "incomplete")
        self.assertEqual(exit_code, G.INCOMPLETE)
        blocking_paths = {row["repo_path"] for row in payload["blocking"]}
        self.assertIn(str(self.legacy_repo), blocking_paths)
        self.assertIn(str(self.empty_repo), blocking_paths)
        self.assertIn(str(self.malformed_repo), blocking_paths)

    def test_valid_waiver_flips_only_its_own_root(self):
        roster = self.all_four_roster()
        future = "2099-01-01T00:00:00Z"
        waivers = self.write_waivers([
            {"repo_path": str(self.empty_repo), "reason": "known gap", "issuer": "ops",
             "created_at": "2026-09-01T00:00:00Z", "expires_at": future},
        ])
        _, payload = self.run_cli(["complete", "--roster", str(roster), "--waivers", str(waivers)])
        blocking_paths = {row["repo_path"] for row in payload["blocking"]}
        self.assertNotIn(str(self.empty_repo), blocking_paths)
        self.assertIn(str(self.legacy_repo), blocking_paths)
        self.assertIn(str(self.malformed_repo), blocking_paths)

    def test_expired_waiver_does_not_flip(self):
        roster = self.all_four_roster()
        waivers = self.write_waivers([
            {"repo_path": str(self.empty_repo), "reason": "known gap", "issuer": "ops",
             "created_at": "2000-01-01T00:00:00Z", "expires_at": "2000-02-01T00:00:00Z"},
        ])
        _, payload = self.run_cli(["complete", "--roster", str(roster), "--waivers", str(waivers)])
        self.assertEqual(payload["verdict"], "incomplete")
        blocking_paths = {row["repo_path"] for row in payload["blocking"]}
        self.assertIn(str(self.empty_repo), blocking_paths)
        row = next(r for r in payload["roots"] if r["repo_path"] == str(self.empty_repo))
        self.assertEqual(row["waiver"]["reason"], "waiver-expired")

    def test_malformed_and_foreign_waivers_are_rejected(self):
        roster = self.all_four_roster()
        waivers = self.write_waivers([
            {"repo_path": str(self.empty_repo), "reason": "known gap"},
            {"repo_path": str(self.malformed_repo), "reason": "known gap", "issuer": "ops",
             "created_at": "2026-09-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z",
             "canonical_root": str(self.base / "elsewhere")},
            # repo_path is the primary match key (see match_waiver docstring); the
            # mismatched canonical_root is only checked by validate_time_bounded_grant.
        ])
        _, payload = self.run_cli(["complete", "--roster", str(roster), "--waivers", str(waivers)])
        row_empty = next(r for r in payload["roots"] if r["repo_path"] == str(self.empty_repo))
        row_malformed = next(r for r in payload["roots"] if r["repo_path"] == str(self.malformed_repo))
        self.assertEqual(row_empty["waiver"]["reason"], "waiver-malformed")
        self.assertEqual(row_malformed["waiver"]["reason"], "waiver-foreign-root")

    def test_unmatched_waiver_is_recorded_and_flips_nothing(self):
        roster = self.all_four_roster()
        waivers = self.write_waivers([
            {"repo_path": str(self.base / "no-such-repo"), "reason": "x", "issuer": "ops",
             "created_at": "2026-09-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z"},
        ])
        _, payload = self.run_cli(["complete", "--roster", str(roster), "--waivers", str(waivers)])
        self.assertEqual(len(payload["waivers"]["unmatched"]), 1)
        self.assertEqual(payload["waivers"]["unmatched"][0]["reason"], "waiver-unmatched-root")
        self.assertEqual(payload["verdict"], "incomplete")

    def test_complete_requires_negative_probe_pass_on_active_roots(self):
        roster = self.active_only_roster()
        with mock.patch.object(P, "check_write",
                               return_value={"verdict": "allow", "reason": "unexpected-allow"}):
            _, payload = self.run_cli(["complete", "--roster", str(roster)])
        self.assertEqual(payload["verdict"], "incomplete")
        self.assertEqual(payload["blocking"][0]["reason"], "negative-probe-failed")

    def test_complete_all_active_and_probed_is_complete(self):
        roster = self.write_roster([
            {"repo_path": str(self.active_repo)},
        ])
        exit_code, payload = self.run_cli(["complete", "--roster", str(roster)])
        self.assertEqual(payload["verdict"], "complete")
        self.assertEqual(exit_code, G.OK)

    def test_output_inside_audited_root_is_refused(self):
        roster = self.active_only_roster()
        bad_output = self.active_root / "audit-result.json"
        exit_code, payload = self.run_cli(
            ["complete", "--roster", str(roster), "--output", str(bad_output)])
        self.assertEqual(exit_code, G.INCOMPLETE)
        self.assertEqual(payload["reason"], "output-inside-audited-root")
        self.assertFalse(bad_output.exists())

    def test_output_outside_roster_roots_is_written_atomically(self):
        roster = self.active_only_roster()
        results_dir = self.base / "results"
        results_dir.mkdir()
        output = results_dir / "audit.json"
        exit_code, _ = self.run_cli(["complete", "--roster", str(roster), "--output", str(output)])
        self.assertEqual(exit_code, G.OK)
        self.assertTrue(output.is_file())
        written = [p for p in output.parent.iterdir()]
        self.assertEqual(written, [output])

    def test_route_bookkeeping_is_reported_but_not_classification_input(self):
        routes_dir = self.active_root / ".runtime" / "routes"
        routes_dir.mkdir(parents=True, exist_ok=True)
        (routes_dir / "rt-fixture.json").write_text("{}", encoding="utf-8")
        roster = self.active_only_roster()
        _, payload = self.run_cli(["audit", "--roster", str(roster)])
        row = payload["roots"][0]
        self.assertEqual(row["state"], "active")
        self.assertGreaterEqual(row["route_bookkeeping"]["open_routes"], 1)


class StructureTest(unittest.TestCase):
    def test_gate_never_touches_mutating_entrypoints(self):
        forbidden = {"begin", "activate", "finalize", "admit_shared", "recover",
                    "resolve_output_dir", "review_lease_acquire", "review_lease_release"}
        source = Path(G.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "P":
                used.add(node.attr)
        self.assertEqual(used & forbidden, set())


if __name__ == "__main__":
    unittest.main()
