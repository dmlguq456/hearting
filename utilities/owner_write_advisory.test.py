#!/usr/bin/env python3
"""SD-162 observations must remain visible without changing launch authority."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import owner_write_advisory as A
import work_start as W
from split_module_fixture import patch_split

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


R = load("advisory_route", "utilities/capability-route.py")
C = load("advisory_codex", "adapters/codex/bin/dispatch-headless.py")


class OwnerWriteAdvisoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.primary = self.root / "primary"
        self.linked = self.root / "linked"
        self.git("init", "-q", str(self.primary))
        self.git("-C", str(self.primary), "-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.com", "commit", "--allow-empty", "-qm", "fixture")
        self.git("-C", str(self.primary), "worktree", "add", "-qb", "linked", str(self.linked))
        self.route = {"route_id": "rt-fixture", "route_hash": "sha256:unchanged",
                      "capability": "autopilot-code", "effective_intensity": "standard",
                      "owner_dispatch_depth": 1, "slug": "fixture", "cwd": str(self.primary),
                      "artifact_root": str(self.root / "artifacts"), "selection": {"shape": "staged"},
                      "work_request": {"owner_harness": "codex", "text": "write ~/.config"},
                      "nodes": [{"id": "execute", "write_scope": ["source/**"]}]}

    def git(self, *args):
        subprocess.run(["git", *args], check=True, capture_output=True, text=True)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def args(self, **updates):
        values = dict(worker_type="owner", dispatch_depth=1, worktree=str(self.primary),
                      sandbox="workspace-write", launch_lifecycle="detached", route_file=None,
                      write_scope="source/**;artifacts/**", owner_route_binding=None,
                      execution_access_grant=None)
        values.update(updates)
        return SimpleNamespace(**values)

    def test_primary_linked_readonly_and_direct(self):
        before = self.snapshot()
        message = A.advisories(self.route)[0]["message"]
        self.assertIn("커밋에 필요한 부분만 쓰기", message)
        self.assertIn("config·hooks는 읽기 전용", message)
        self.assertIn("지원하지 않으면 기존처럼 .git 전체가 보호", message)
        self.route["cwd"] = str(self.linked)
        row = A.advisories(self.route)[0]
        self.assertEqual(row["git_topology"], "linked")
        self.assertIn("좁은 Git", row["message"])
        self.route["nodes"][0]["write_scope"] = ["artifacts/reviews/**"]
        self.assertEqual(A.advisories(self.route), [])
        self.route["nodes"][0]["write_scope"] = ["source/**"]
        self.route["owner_dispatch_depth"] = 0
        self.assertEqual(A.advisories(self.route), [])
        self.assertEqual(before, self.snapshot())

    def test_explicit_other_owner_and_auto_do_not_infer_paths(self):
        for owner in ("claude", "opencode"):
            self.assertEqual(A.advisories(self.route, owner_harness=owner), [])
        self.route.pop("work_request")
        row = A.advisories(self.route)[0]
        self.assertEqual(row["owner_harness"], "auto")
        self.assertIn("선택되면", row["message"])
        self.assertEqual(row["explicit_writable_roots"], [])
        self.assertNotIn("~/.config", row["message"])

    def test_shared_mutation_predicate_preserves_all_scopes(self):
        for scope, expected in (("source/**", True), ("source", True), ("source-scoped", True),
                                ("target-artifact", True), ("source/subdir", False),
                                ("artifacts/**", False), (None, False)):
            self.assertEqual(R.worktree_mutating_scope(scope), expected)

    def test_actual_wrapper_sandboxes_and_depth2(self):
        for sandbox in ("workspace-write", "read-only", "danger-full-access"):
            args = self.args(sandbox=sandbox)
            before = copy.deepcopy(vars(args))
            row = C.owner_write_advisories(args)[0]
            self.assertEqual(row["phase"], "applied")
            self.assertEqual(row["sandbox"], sandbox)
            self.assertIn(sandbox, row["message"])
            self.assertEqual(before, vars(args))
        self.assertEqual(C.owner_write_advisories(self.args(worker_type="stage", dispatch_depth=2)), [])

    def test_wrapper_reads_route_and_reports_only_existing_explicit_grants(self):
        self.route["cwd"] = str(self.linked)
        path = self.root / "route.json"
        path.write_text(json.dumps(self.route))
        request = self.root / "bounded-output"
        args = self.args(worktree=str(self.linked), write_scope=None,
                         owner_route_binding=SimpleNamespace(route_file=path),
                         execution_access_grant=SimpleNamespace(writable_roots=(request,)))
        expected = [str(p) for p in C.linked_worktree_git_writable_dirs(args)]
        before = self.snapshot()
        row = C.owner_write_advisories(args)[0]
        self.assertEqual(row["git_writable_roots"], expected)
        self.assertEqual(row["explicit_writable_roots"], [str(request)])
        self.assertNotIn(str(self.primary / ".git"), expected)
        self.assertEqual(before, self.snapshot())
        self.route["nodes"][0]["write_scope"] = []
        path.write_text(json.dumps(self.route))
        self.assertEqual(C.owner_write_advisories(args)[0]["explicit_writable_roots"], [str(request)])
        args.execution_access_grant = None
        self.assertEqual(C.owner_write_advisories(args), [])

    def test_absorbed_explicit_request_stays_explicit_without_default_root_expansion(self):
        from execution_access import AccessContext, bind_request
        output = self.primary / "output"
        output.mkdir()
        request = self.root / "access.json"
        request.write_text(json.dumps({"schema_version": 1, "writable_roots": [str(output)],
            "read_roots": [], "network": {"required": False, "reason": "", "hosts": []},
            "enforcement_required": "any", "justification": {str(output): "bounded output"}}))
        env = {"HOME": str(self.root / "home")}
        grant = bind_request(str(request), environ=env, context=AccessContext.build(
            worktree=self.primary, artifact_root=self.root / "artifacts",
            dispatch_state_root=self.root / "state", agent_home=ROOT, environ=env),
            is_child=False, parent=None, runtime="codex-exec", default_writable_roots=(self.primary,))
        self.assertEqual(grant.additional_writable_roots, ())
        self.assertEqual(grant.absorbed_writable_roots, (output,))
        row = C.owner_write_advisories(self.args(execution_access_grant=grant, write_scope="artifacts/**"))[0]
        self.assertEqual(row["explicit_writable_roots"], [str(output)])
        self.assertNotIn(str(self.primary), row["explicit_writable_roots"])

    def test_compose_card_receipt_do_not_mutate_route(self):
        before = copy.deepcopy(self.route)
        with patch_split(R, "compose_campaign_selection", return_value={}), \
             patch_split(R, "_compose_campaign_line", return_value="campaign fixture"):
            card = R.compose_card(self.route)
            receipt = R.compose_receipt(self.route, self.root / "route.json")
        self.assertIn(receipt["advisories"][0]["message"], card)
        self.assertEqual(self.route, before)

    def test_explain_is_visible_and_read_only_without_prompt_file(self):
        before = self.snapshot()
        # Owner selection is an explicit compose argument even without work_request.
        self.route.pop("work_request")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["capability-route", "compose", "--explain",
                "--shape", "staged", "--owner", "codex", "--slug", "fixture", "--unassigned",
                "--cwd", str(self.primary), "--artifact-root", str(self.root / "artifacts")]), \
             mock.patch.object(R, "compose_route", return_value=self.route), \
             mock.patch.object(R, "_resolve_compose_plan", return_value=(None, None)), \
             patch_split(R, "compose_campaign_selection", return_value={}), \
             patch_split(R, "_compose_campaign_line", return_value="campaign fixture"), \
             patch_split(R, "_emit_compiled_route") as emit, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.main(), 0)
        emit.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["advisories"][0]["owner_harness"], "codex")
        self.assertEqual(before, self.snapshot())

    def test_start_and_resume_promote_actual_receipt_to_top_level(self):
        actual = C.owner_write_advisories(self.args())[0]
        receipt = A.RECEIPT_KEY + json.dumps(actual)
        def advance(route, path, jobs, result, **kw):
            result.update(state="waiting", launches=[{"receipt": receipt}])
            return result
        before = copy.deepcopy(self.route)
        for wait in (False, True):
            with mock.patch.object(W, "_advance", side_effect=advance):
                result = W.start_work(self.route, self.root / "route.json", self.root / "jobs", wait=wait)
            self.assertIn(actual, result["advisories"])
            self.assertEqual(result["advisories"][0]["phase"], "prospective")
        self.assertEqual(before, self.route)

    def test_invalid_receipt_is_ignored(self):
        self.assertEqual(A.receipt_advisories(A.RECEIPT_KEY + "oops\n" + A.RECEIPT_KEY + "[]"), [])

    def test_real_wrapper_reports_after_grant_before_registration(self):
        out = io.StringIO()
        jobs = self.root / "jobs.log"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "CODEX_", "CLAUDE_"))}
        env.update(AGENT_DISPATCH_JOBS=str(jobs), AGENT_DISPATCH_PARENT_SESSION_ID="fixture",
                   AGENT_DISPATCH_CURRENT_HARNESS="claude", AGENT_DISPATCH_CURRENT_TRANSPORT="headless",
                   AGENT_DISPATCH_CURRENT_SANDBOX="default", AGENT_DISPATCH_CALLER_HARNESS="claude")
        def observed(args):
            self.assertEqual(args.execution_access_grant, None)
            self.assertEqual(A.receipt_advisories(out.getvalue())[0]["sandbox"], "workspace-write")
            raise RuntimeError("stop-before-registry")
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(C, "check_runtime_projection", return_value=0), \
             mock.patch.object(C, "resolve_artifact_root", return_value=str(self.root / "artifacts")), \
             mock.patch.object(C, "validate_nested_owner_registry_projection", side_effect=observed), \
             mock.patch.object(C, "append_job") as append, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "stop-before-registry"):
                rc = C.main(["dispatch-headless.py", "--register", "--worktree", str(self.primary),
                             "--jobs", str(jobs), "--slug", "fixture", "--capability", "autopilot-code",
                             "--capability-mode", "debug", "--worker-type", "owner", "--unit", "_kernel/owner",
                             "--model", "fixture", "--reasoning", "low", "--write-scope", "source/**",
                             "--completion-delivery", "poll", "--sandbox", "workspace-write"])
                self.fail(f"wrapper stopped before advisory: rc={rc}, output={out.getvalue()}")
        append.assert_not_called()
        self.assertEqual(jobs.read_text() if jobs.exists() else "", "")

    def test_compose_start_and_later_start_cli_expose_top_level_advisory(self):
        path = self.root / "route.json"
        path.write_text(json.dumps(self.route))
        prompt = self.root / "task.txt"
        prompt.write_text("fixture")
        before = self.snapshot()
        def advance(route, path, jobs, result, **kw):
            return {**result, "state": "waiting"}
        for argv in (["compose", "--start", "--owner", "codex", "--shape", "staged",
                      "--slug", "fixture", "--unassigned", "--prompt-file", str(prompt),
                      "--cwd", str(self.primary), "--artifact-root", str(self.root / "artifacts")],
                     ["start", "--route", str(path)]):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["capability-route", *argv, "--jobs", str(self.root / "jobs")]), \
                 mock.patch.object(R, "compose_route", return_value=self.route), \
                 mock.patch.object(R, "prepare_isolated_worktree",
                                   return_value={"state": "reused", "cwd": str(self.primary)}), \
                 patch_split(R, "verify_route", return_value=self.route), \
                 patch_split(R, "_record_route_chain"), \
                 mock.patch.object(R, "_resolve_compose_plan", return_value=(None, None)), \
                 patch_split(R, "compose_campaign_selection", return_value={}), \
                 patch_split(R, "_compose_campaign_line", return_value="campaign fixture"), \
                 patch_split(R, "_emit_compiled_route", return_value=path), \
                 mock.patch.object(W, "_advance", side_effect=advance), \
                 mock.patch("start_receipt.save", side_effect=lambda result, jobs: result), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(R.main(), 0)
            self.assertEqual(json.loads(out.getvalue())["advisories"][0]["owner_harness"], "codex")
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
