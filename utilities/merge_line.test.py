#!/usr/bin/env python3
"""Real process contention plus fake GitHub head/CI transitions; no live merges."""
import copy
import importlib.util
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("merge_line", Path(__file__).with_name("merge-line.py"))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def hold_turn(directory, pr, entered, release, messages, fail):
    try:
        with M.MergeTurn(directory, pr, emit=messages.put, interval=0.01):
            messages.put(("entered", pr))
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test release timeout")
            if fail:
                raise M.MergeError("CI failed: unit-tests")
    except M.MergeError as exc:
        messages.put(("failed", pr, str(exc)))


class Contention(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = mp.get_context("fork")
        self.messages = self.ctx.Queue()
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.is_alive():
                child.kill()
            child.join(3)
        self.messages.close()

    def start(self, pr, fail=False):
        entered, release = self.ctx.Event(), self.ctx.Event()
        process = self.ctx.Process(target=hold_turn,
            args=(self.root, pr, entered, release, self.messages, fail))
        process.start()
        self.children.append(process)
        return process, entered, release

    def queued(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                rows = json.loads((self.root / "line.json").read_text())
            except FileNotFoundError:
                rows = []
            if len(rows) == count:
                return rows
            time.sleep(0.01)
        self.fail("request did not enter the queue")

    def entries(self):
        messages = []
        while True:
            try:
                messages.append(self.messages.get(timeout=0.1))
            except queue.Empty:
                return messages

    def test_simultaneous_prs_wait_in_fifo_order_and_show_predecessor(self):
        first, entered1, release1 = self.start(11)
        self.assertTrue(entered1.wait(5))
        second, entered2, release2 = self.start(22)
        self.queued(2)
        third, entered3, release3 = self.start(33)
        self.queued(3)
        self.assertFalse(entered2.is_set())
        self.assertFalse(entered3.is_set())
        release1.set()
        self.assertTrue(entered2.wait(5))
        self.assertFalse(entered3.is_set())
        release2.set()
        self.assertTrue(entered3.wait(5))
        release3.set()
        for process in (first, second, third):
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        messages = self.entries()
        self.assertEqual([m for m in messages if isinstance(m, tuple) and m[0] == "entered"],
                         [("entered", 11), ("entered", 22), ("entered", 33)])
        self.assertIn("merge-line: waiting PR=#22 position=1 ahead=#11", messages)
        self.assertIn("merge-line: waiting PR=#33 position=2 ahead=#22", messages)
        self.assertEqual(json.loads((self.root / "line.json").read_text()), [])

    def test_failed_front_pr_releases_next_turn(self):
        first, entered1, release1 = self.start(11, fail=True)
        self.assertTrue(entered1.wait(5))
        second, entered2, release2 = self.start(22)
        self.queued(2)
        release1.set()
        self.assertTrue(entered2.wait(5))
        release2.set()
        first.join(5)
        second.join(5)
        self.assertIn(("failed", 11, "CI failed: unit-tests"), self.entries())
        self.assertEqual(json.loads((self.root / "line.json").read_text()), [])

    def test_killed_holder_releases_flock_and_dead_pid_start_entry(self):
        first, entered1, _ = self.start(11)
        self.assertTrue(entered1.wait(5))
        second, entered2, release2 = self.start(22)
        self.queued(2)
        first.kill()
        first.join(5)
        self.assertTrue(entered2.wait(5))
        rows = self.queued(1)
        self.assertEqual(rows[0]["pr"], 22)
        release2.set()
        second.join(5)
        self.assertEqual(second.exitcode, 0)

    def test_reused_pid_with_wrong_start_does_not_hold_queue(self):
        (self.root / "line.json").write_text(json.dumps([
            {"token": "old", "pr": 11, "pid": os.getpid(), "start": "old-start", "holding": True}]))
        with M.MergeTurn(self.root, 22, emit=lambda _: None):
            self.assertEqual(self.queued(1)[0]["pr"], 22)


def snapshot(head="head1", checks=None, **extra):
    return {"state": "OPEN", "isDraft": False, "baseRefName": "main",
            "headRefOid": head, "mergeable": "MERGEABLE", "statusCheckRollup": checks if checks is not None else [
                {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}], **extra}


class FakeGitHub:
    base = "main"

    def __init__(self):
        self.current = snapshot()
        self.main = "base1"
        self.ancestry = {("base1", "head1")}
        self.updates, self.merges = [], []
        self.on_snapshot = self.on_sleep = lambda: None

    def snapshot(self, pr):
        self.on_snapshot()
        return copy.deepcopy(self.current)

    def base_head(self):
        return self.main

    def contains_base(self, base, head):
        return (base, head) in self.ancestry

    def update(self, pr, head):
        self.updates.append((pr, head))
        self.current = snapshot(head=head + "-updated")
        self.ancestry.add((self.main, self.current["headRefOid"]))

    def merge(self, pr, head):
        self.merges.append((pr, head))
        return "merge1"

    def sleep(self, _):
        self.on_sleep()


class HeadCI(unittest.TestCase):
    def run_pr(self, client):
        messages = []
        result = M.merge_pr(client, 123, emit=messages.append, sleep=client.sleep, interval=0)
        return result, messages

    def test_up_to_date_success_reuses_ci_without_update(self):
        client = FakeGitHub()
        self.assertEqual(self.run_pr(client)[0], "merge1")
        self.assertEqual(client.updates, [])
        self.assertEqual(client.merges, [(123, "head1")])

    def test_stale_head_is_updated_before_observing_old_failure(self):
        client = FakeGitHub()
        client.main = "base2"
        client.current["statusCheckRollup"][0]["conclusion"] = "FAILURE"
        self.run_pr(client)
        self.assertEqual(client.updates, [(123, "head1")])
        self.assertEqual(client.merges, [(123, "head1-updated")])

    def test_ci_failure_never_merges(self):
        client = FakeGitHub()
        client.current["statusCheckRollup"][0]["conclusion"] = "FAILURE"
        with self.assertRaisesRegex(M.MergeError, "CI failed: tests"):
            self.run_pr(client)
        self.assertEqual(client.merges, [])

    def test_no_checks_and_pending_checks_wait_for_actual_success(self):
        client = FakeGitHub()
        client.current = snapshot(checks=[])
        seen = []
        def advance():
            seen.append(1)
            if len(seen) == 1:
                client.current["statusCheckRollup"] = [{"name": "tests", "status": "IN_PROGRESS"}]
            else:
                client.current = snapshot()
        client.on_sleep = advance
        self.run_pr(client)
        self.assertEqual(len(seen), 2)
        self.assertEqual(client.merges, [(123, "head1")])

    def test_main_movement_during_ci_rechecks_and_updates_once(self):
        client = FakeGitHub()
        client.current["statusCheckRollup"][0]["status"] = "IN_PROGRESS"
        def advance():
            client.main = "base2"
            client.current = snapshot()
        client.on_sleep = advance
        self.run_pr(client)
        self.assertEqual(client.updates, [(123, "head1")])
        self.assertEqual(client.merges, [(123, "head1-updated")])

    def test_head_push_before_merge_checks_new_head_instead(self):
        client = FakeGitHub()
        calls = []
        def push():
            calls.append(1)
            if len(calls) == 2:
                client.current = snapshot(head="head2")
                client.ancestry.add((client.main, "head2"))
        client.on_snapshot = push
        self.run_pr(client)
        self.assertEqual(client.merges, [(123, "head2")])

    def test_checks_skips_are_normal_but_all_skips_or_cancel_are_not_success(self):
        current = snapshot()
        current["statusCheckRollup"].append({"name": "conditional", "status": "COMPLETED", "conclusion": "SKIPPED"})
        current["statusCheckRollup"].append({"__typename": "StatusContext", "context": "legacy", "state": "SUCCESS"})
        self.assertTrue(M.check_state(current))
        current["statusCheckRollup"] = current["statusCheckRollup"][1:2]
        with self.assertRaisesRegex(M.MergeError, "without a successful check"):
            M.check_state(current)
        current["statusCheckRollup"][0]["conclusion"] = "CANCELLED"
        with self.assertRaises(M.MergeError):
            M.check_state(current)

    def test_closed_draft_wrong_base_and_conflict_release_without_merge(self):
        for extra in ({"state": "CLOSED"}, {"isDraft": True}, {"baseRefName": "other"}, {"mergeable": "CONFLICTING"}):
            client = FakeGitHub()
            client.current.update(extra)
            with self.subTest(extra=extra), self.assertRaises(M.MergeError):
                self.run_pr(client)
            self.assertEqual(client.merges, [])

    def test_original_path_checks_caller_again_after_waiting_for_ci(self):
        client = FakeGitHub()
        client.current["statusCheckRollup"][0]["status"] = "IN_PROGRESS"
        live = [True]
        client.on_sleep = lambda: live.__setitem__(0, False)
        with self.assertRaises(M.Withdrawn):
            M.merge_pr(client, 123, sleep=client.sleep, interval=0, admitted=lambda: live[0])
        self.assertEqual(client.merges, [])

    def test_merged_pr_is_idempotent(self):
        client = FakeGitHub()
        client.current["state"] = "MERGED"
        self.assertIsNone(self.run_pr(client)[0])
        self.assertEqual(client.merges, [])

    def test_main_push_immediately_before_merge_restarts_from_new_base(self):
        client = FakeGitHub()
        calls = []
        def push():
            calls.append(1)
            if len(calls) == 2:
                client.main = "base2"
        client.on_snapshot = push
        self.run_pr(client)
        self.assertEqual(client.updates, [(123, "head1")])
        self.assertEqual(client.merges, [(123, "head1-updated")])


class GitHubRequests(unittest.TestCase):
    def test_updates_and_merges_pin_the_observed_head(self):
        repo = {"nameWithOwner": "owner/project", "url": "https://github.com/owner/project",
                "defaultBranchRef": {"name": "main"}}
        responses = [repo, {"message": "Updating"}, {"merged": True, "sha": "merge1"}]
        with mock.patch.object(subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr="") for row in responses]) as run:
            client = M.GitHub()
            client.update(123, "head1")
            self.assertEqual(client.merge(123, "head1"), "merge1")
        self.assertIn("expected_head_sha=head1", run.call_args_list[1].args[0])
        self.assertIn("sha=head1", run.call_args_list[2].args[0])
        self.assertIn("merge_method=merge", run.call_args_list[2].args[0])

    def test_worktrees_and_harnesses_share_repository_key(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"XDG_STATE_HOME": td}):
            self.assertEqual(M.state_directory("github.com/OWNER/Repo"), M.state_directory("github.com/owner/repo"))
            self.assertNotEqual(M.state_directory("github.com/owner/repo"), M.state_directory("other.host/owner/repo"))


class FakeIntegration:
    def __init__(self, client, base, entries):
        self.client, self.base = client, base
        self.entries = [dict(row, tree="tree-" + str(row["pr"]), commit="merge-" + str(row["pr"])) for row in entries
                        if row["pr"] not in client.conflicts]
        self.conflicts = [row["pr"] for row in entries if row["pr"] in client.conflicts]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.cleaned += 1

    def publish(self):
        for row in self.entries:
            self.client.merge(row["pr"], row["head"])

    def validate(self, **_):
        prs = [row["pr"] for row in self.entries]
        self.client.validations.append(prs)
        self.client.before_validation(self.client, self)
        if any(pr in self.client.bad for pr in prs):
            raise M.CIFailure("CI failed: suite")


class BatchClient:
    base = "main"

    def __init__(self, prs):
        self.current = {pr: snapshot(head=f"head-{pr}") for pr in prs}
        self.main = "base-0"
        self.bad, self.conflicts = set(), set()
        self.validations, self.merges, self.single = [], [], []
        self.cleaned = 0
        self.before_validation = lambda *_: None
        self.before_merge = lambda *_: None

    def snapshot(self, pr):
        self.before_merge(self, pr)
        return copy.deepcopy(self.current[pr])

    def base_head(self):
        return self.main

    def integration(self, base, entries):
        return FakeIntegration(self, base, entries)

    def merge(self, pr, head):
        self.merges.append((pr, head))
        self.main = f"merge-{pr}"
        self.current[pr]["state"] = "MERGED"
        return self.main

    def tree(self, commit):
        return "tree-" + commit.split("-")[-1]


class BatchMerge(unittest.TestCase):
    def run_batch(self, client, prs):
        results = {}
        M.merge_batch(client, prs, lambda pr, result: results.setdefault(pr, result),
                      emit=lambda _: None, sleep=lambda _: None)
        return results

    def test_three_prs_share_one_ci_and_preserve_original_heads(self):
        client = BatchClient([11, 22, 33])
        results = self.run_batch(client, [11, 22, 33])
        self.assertEqual(client.validations, [[11, 22, 33]])
        self.assertEqual(client.merges, [(11, "head-11"), (22, "head-22"), (33, "head-33")])
        self.assertTrue(all(row["ok"] for row in results.values()))
        self.assertEqual(client.cleaned, 1)

    def test_failed_member_is_isolated_and_others_finish(self):
        client = BatchClient([11, 22, 33, 44])
        client.bad = {22}
        results = self.run_batch(client, [11, 22, 33, 44])
        self.assertEqual([pr for pr, _ in client.merges], [11, 33, 44])
        self.assertFalse(results[22]["ok"])
        self.assertIn("CI failed", results[22]["message"])
        self.assertTrue(results[44]["ok"])

    def test_base_change_revalidates_before_merging(self):
        client = BatchClient([11, 22])
        def move(client, _):
            if len(client.validations) == 1:
                client.main = "external-base"
        client.before_validation = move
        self.run_batch(client, [11, 22])
        self.assertEqual(client.validations, [[11, 22], [11, 22]])

    def test_head_change_revalidates_original_pr(self):
        client = BatchClient([11, 22])
        def move(client, _):
            if len(client.validations) == 1:
                client.current[22]["headRefOid"] = "new-head-22"
        client.before_validation = move
        self.run_batch(client, [11, 22])
        self.assertEqual(client.merges[-1], (22, "new-head-22"))
        self.assertEqual(client.validations, [[11, 22], [11, 22]])

    def test_conflicting_member_alone_uses_original_path(self):
        client = BatchClient([11, 22, 33])
        client.conflicts = {22}
        with mock.patch.object(M, "merge_pr", return_value="merge-single") as single:
            results = self.run_batch(client, [11, 22, 33])
        self.assertEqual(client.validations, [[11, 33]])
        self.assertEqual(single.call_args.args[1], 22)
        self.assertTrue(results[22]["ok"])

    def test_draft_and_closed_prs_do_not_block_eligible_members(self):
        client = BatchClient([11, 22, 33])
        client.current[22]["isDraft"] = True
        client.current[33]["state"] = "CLOSED"
        results = self.run_batch(client, [11, 22, 33])
        self.assertEqual(client.merges, [(11, "head-11")])
        self.assertFalse(results[22]["ok"])
        self.assertFalse(results[33]["ok"])

    def test_failure_on_old_head_revalidates_new_head_instead_of_rejecting_caller(self):
        client = BatchClient([11, 22])
        client.bad = {22}
        def fix(client, batch):
            if [row["pr"] for row in batch.entries] == [22]:
                if client.current[22]["headRefOid"] == "head-22":
                    client.current[22]["headRefOid"] = "fixed-head-22"
                else:
                    client.bad.clear()
        client.before_validation = fix
        results = self.run_batch(client, [11, 22])
        self.assertTrue(results[22]["ok"])
        self.assertEqual(client.merges[-1], (22, "fixed-head-22"))
        self.assertEqual(client.validations, [[11, 22], [11], [22], [22]])

    def test_withdrawn_caller_is_removed_before_publication(self):
        client = BatchClient([11, 22])
        live = {11, 22}
        client.before_validation = lambda *_: live.discard(22)
        results = {}
        M.merge_batch(client, [11, 22], lambda pr, result: results.setdefault(pr, result),
                      emit=lambda _: None, sleep=lambda _: None, admitted=lambda pr: pr in live)
        self.assertEqual(client.merges, [(11, "head-11")])
        self.assertNotIn(22, results)

    def test_withdrawn_conflict_caller_never_enters_original_path(self):
        client = BatchClient([11, 22, 33])
        client.conflicts = {22}
        live = {11, 22, 33}
        client.before_validation = lambda *_: live.discard(22)
        results = {}
        with mock.patch.object(M, "merge_pr") as single:
            M.merge_batch(client, [11, 22, 33], lambda pr, result: results.setdefault(pr, result),
                          emit=lambda _: None, sleep=lambda _: None, admitted=lambda pr: pr in live)
        single.assert_not_called()
        self.assertNotIn(22, results)
        self.assertTrue(results[11]["ok"])

    def test_lost_pr_observation_after_publication_preserves_actual_success(self):
        client = BatchClient([11, 22])
        def fail_after_push(client, pr):
            if client.merges:
                raise M.MergeError("API unavailable after successful push")
        client.before_merge = fail_after_push
        results = self.run_batch(client, [11, 22])
        self.assertEqual([pr for pr, _ in client.merges], [11, 22])
        self.assertTrue(all(row["ok"] for row in results.values()))

    def test_unrelated_infrastructure_failure_is_not_bisected_as_bad_pr(self):
        client = BatchClient([11, 22])
        client.before_validation = lambda *_: (_ for _ in ()).throw(M.MergeError("network unavailable"))
        results = self.run_batch(client, [11, 22])
        self.assertEqual(client.validations, [[11, 22]])
        self.assertEqual(client.merges, [])
        self.assertTrue(all(not row["ok"] for row in results.values()))


class LeasePublication(unittest.TestCase):
    def test_main_successor_push_is_preserved_and_old_ci_is_not_published(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, remote = root / "source", root / "remote.git"
            source.mkdir()
            def git(path, *args):
                return subprocess.run(["git", "-C", str(path), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.test", *args], check=True,
                    capture_output=True, text=True).stdout.strip()
            git(source, "init", "-q", "-b", "main")
            (source / "base").write_text("base")
            git(source, "add", "base")
            git(source, "commit", "-qm", "base")
            base = git(source, "rev-parse", "HEAD")
            git(source, "clone", "-q", "--bare", str(source), str(remote))
            (source / "tested").write_text("tested")
            git(source, "add", "tested")
            git(source, "commit", "-qm", "tested integration")
            checked = git(source, "rev-parse", "HEAD")
            plan = M.Integration.__new__(M.Integration)
            plan.temp = SimpleNamespace(name=str(source))
            plan.base, plan.data = base, {"head": checked}
            client = mock.Mock(base="main")
            client.batch_allowed.return_value = True
            client.base_head.side_effect = lambda: git(remote, "rev-parse", "main")
            client.contains_base.return_value = False
            plan.client = client
            git(source, "remote", "add", "origin", str(remote))
            git(source, "checkout", "-qb", "successor", base)
            (source / "successor").write_text("outside change")
            git(source, "add", "successor")
            git(source, "commit", "-qm", "outside main update")
            successor = git(source, "rev-parse", "HEAD")
            git(source, "push", "-q", "origin", "HEAD:main")
            with self.assertRaises(M.Changed):
                plan.publish()
            self.assertEqual(git(remote, "rev-parse", "main"), successor)
            self.assertNotEqual(git(remote, "rev-parse", "main"), checked)

    def test_protected_branch_never_uses_indirect_merge_even_for_admin_credentials(self):
        client = M.GitHub.__new__(M.GitHub)
        client.base = "main"
        client.api = mock.Mock(return_value={"protected": True})
        self.assertFalse(client.batch_allowed())
        client.api.assert_called_once_with("branches/main")

    def test_ruleset_uses_existing_path_without_new_input(self):
        client = M.GitHub.__new__(M.GitHub)
        client.base = "main"
        client.api = mock.Mock(side_effect=[{"protected": False}, [{"type": "pull_request"}]])
        self.assertFalse(client.batch_allowed())

    def test_cleanup_deletion_uses_server_lease(self):
        client = M.GitHub.__new__(M.GitHub)
        client.url = "https://github.com/owner/fixture.git"
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            client.delete_branch("merge-line/abc", "checked-head")
        self.assertIn("--force-with-lease=refs/heads/merge-line/abc:checked-head", run.call_args.args[0])
        self.assertIn(":refs/heads/merge-line/abc", run.call_args.args[0])


class WorkflowConclusion(unittest.TestCase):
    def plan(self, runs, *, tree="tested-tree", checks=None):
        client = mock.Mock()
        client.base_head.return_value = "base"
        client.tree.return_value = tree
        client.snapshot.return_value = snapshot(checks=checks)
        client.api.side_effect = lambda endpoint: ({"state": "open", "head": {"sha": "head"},
                    "merge_commit_sha": "merge"} if endpoint.startswith("pulls/")
                    else {"workflow_runs": runs})
        plan = M.Integration.__new__(M.Integration)
        plan.client, plan.base = client, "base"
        plan.data = {"head": "head", "pr": 999}
        plan.entries = [{"pr": 11, "head": "head-11", "tree": "tested-tree"}]
        return plan

    def workflow(self, conclusion="success", status="completed", **extra):
        return {"id": 2, "workflow_id": 1, "name": "Checks", "head_sha": "head",
                "event": "pull_request", "status": status, "conclusion": conclusion, **extra}

    def test_failed_workflow_is_rejected_even_with_successful_check(self):
        plan = self.plan([self.workflow("failure")])
        with self.assertRaises(M.CIFailure):
            plan.validate(emit=lambda _: None, sleep=lambda _: None)

    def test_exact_workflow_success_and_tree_are_both_required(self):
        plan = self.plan([self.workflow()])
        plan.validate(emit=lambda _: None)
        plan.client.tree.assert_called_once_with("merge")
        plan = self.plan([self.workflow()], tree="other-tree")
        with self.assertRaises(M.Changed):
            plan.validate(emit=lambda _: None)

    def test_old_run_success_does_not_mask_latest_failed_run(self):
        plan = self.plan([self.workflow(id=1), self.workflow("failure", id=2)])
        with self.assertRaises(M.CIFailure):
            plan.validate(emit=lambda _: None)

    def test_other_head_run_cannot_authorize_merge(self):
        plan = self.plan([self.workflow(head_sha="old-head")])
        def advance(_):
            plan.client.api.side_effect = lambda endpoint: ({"state": "open", "head": {"sha": "head"},
                        "merge_commit_sha": "merge"} if endpoint.startswith("pulls/")
                        else {"workflow_runs": [self.workflow()]})
        plan.validate(emit=lambda _: None, sleep=advance)
        self.assertEqual(plan.client.snapshot.call_count, 1)


def receive_result(directory, pr, output):
    with M.MergeTurn(directory, pr, emit=lambda _: None, interval=0.01) as turn:
        output.put((pr, turn.result))


class ResultDelivery(unittest.TestCase):
    setUp = Contention.setUp
    stop_children = Contention.stop_children
    queued = Contention.queued
    def test_batched_follower_receives_own_failure_while_leader_holds_lock(self):
        with M.MergeTurn(self.root, 11, emit=lambda _: None, interval=0.01) as leader:
            child = self.ctx.Process(target=receive_result, args=(self.root, 22, self.messages))
            child.start()
            self.children.append(child)
            self.queued(2)
            self.assertEqual(leader.collect(), [11, 22])
            result = M.outcome(22, False, "CI failed: suite")
            leader.settle(22, result)
            self.assertEqual(self.messages.get(timeout=3), (22, result))
            child.join(3)
            self.assertEqual(child.exitcode, 0)
            self.assertEqual(leader.collect(), [11])

    def test_duplicate_callers_receive_same_pr_result(self):
        with M.MergeTurn(self.root, 11, emit=lambda _: None, interval=0.01) as leader:
            for _ in range(2):
                child = self.ctx.Process(target=receive_result, args=(self.root, 22, self.messages))
                child.start()
                self.children.append(child)
            self.queued(3)
            self.assertEqual(leader.collect(), [11, 22])
            result = M.outcome(22, True, "merge-22")
            leader.settle(22, result)
            self.assertEqual(self.messages.get(timeout=3), (22, result))
            self.assertEqual(self.messages.get(timeout=3), (22, result))


class RealIntegration(unittest.TestCase):
    def test_conflict_is_aborted_without_losing_good_prefix_and_temporary_branch_is_removed(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"XDG_STATE_HOME": td}):
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.test", *args], check=True,
                    capture_output=True, text=True).stdout.strip()
            git("init", "-q", "-b", "main")
            (repo / "base").write_text("base")
            git("add", "base")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            rows = []
            for pr, filename, content in [(11, "a", "first"), (22, "a", "conflict"), (33, "b", "last")]:
                git("checkout", "-qb", f"pr-{pr}", base)
                (repo / filename).write_text(content)
                git("add", filename)
                git("commit", "-qm", f"PR {pr}")
                rows.append({"pr": pr, "head": git("rev-parse", "HEAD")})
            git("checkout", "-q", "main")
            client = mock.Mock(identity="local/fixture", url=str(repo), base="main", repo="owner/fixture")
            def api(endpoint, *args):
                if endpoint == "pulls":
                    return {"number": 99}
                if endpoint.startswith("pulls?"):
                    return [{"number": 99, "head": {"sha": git("rev-parse", branch)}}]
                if endpoint.startswith("pulls/"):
                    return {}
                if endpoint.startswith("git/matching-refs/"):
                    return [{"ref": f"refs/heads/{branch}", "object": {"sha": git("rev-parse", branch)}}]
                if endpoint.startswith("git/refs/"):
                    git("update-ref", "-d", f"refs/heads/{branch}")
                    return None
                self.fail(endpoint)
            client.api.side_effect = api
            client.delete_branch.side_effect = lambda branch, head: git("push", f"--force-with-lease=refs/heads/{branch}:{head}", str(repo), f":refs/heads/{branch}")
            with M.Integration(client, base, rows) as integration:
                branch = integration.branch
                self.assertEqual([row["pr"] for row in integration.entries], [11, 33])
                self.assertEqual(integration.conflicts, [22])
                head = integration.data["head"]
                self.assertEqual(git("show", f"{head}:a"), "first")
                self.assertEqual(git("show", f"{head}:b"), "last")
                self.assertEqual(git("rev-parse", "main"), base)
                self.assertTrue(integration.record.exists())
            self.assertFalse(integration.record.exists())
            self.assertEqual(git("for-each-ref", f"refs/heads/{branch}"), "")

    def test_cleanup_preserves_a_branch_changed_by_another_writer(self):
        with tempfile.TemporaryDirectory() as td:
            record = Path(td) / "abc.json"
            record.write_text("{}")
            client = mock.Mock(repo="owner/fixture")
            client.api.return_value = [{"number": 99, "head": {"sha": "foreign-head"}}]
            with self.assertRaisesRegex(M.MergeError, "preserved"):
                M.cleanup_integration(client, record, {"branch": "merge-line/abc", "head": "our-head"})
            self.assertTrue(record.exists())
            self.assertEqual(client.api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
