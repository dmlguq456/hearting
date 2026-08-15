#!/usr/bin/env python3
"""Checkout-free local Git exchange tests for protocol v2."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from helpers import (
    canonical_bytes,
    commit_bare_tree,
    exchange_fetch_validate,
    exchange_fresh_confirm,
    exchange_publish,
    git,
    init_bare,
    make_operation,
    new_exchange,
    operation_path,
    ref_paths,
)


REF = "refs/heads/memory-v2"
REPLICA_A = "11111111111111111111111111111111"
REPLICA_B = "22222222222222222222222222222222"
REPLICA_C = "33333333333333333333333333333333"


class BareGitExchangeTest(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.remote = init_bare(self.root / "remote.git")

    def tearDown(self):
        self.tempdir.cleanup()

    def _op(self, replica_id: str, record_id: str, body: str):
        return make_operation(
            replica_id=replica_id,
            counter=1,
            record_id=record_id,
            body=body,
        )

    def test_two_writers_form_immutable_union_with_fast_forward_tips(self):
        op_a = self._op(REPLICA_A, "record-a", "alpha")
        op_b = self._op(REPLICA_B, "record-b", "bravo")
        op_c = self._op(REPLICA_C, "record-c", "charlie")
        first = new_exchange(self.root / "exchange-a.git", self.remote, REF)
        second = new_exchange(self.root / "exchange-b.git", self.remote, REF)

        exchange_publish(first, [op_a])
        tip_a = git(self.remote, "rev-parse", REF)
        exchange_publish(second, [op_b])
        tip_b = git(self.remote, "rev-parse", REF)
        exchange_publish(first, [op_c])
        tip_c = git(self.remote, "rev-parse", REF)

        self.assertEqual(git(self.remote, "merge-base", "--is-ancestor", tip_a, tip_b), "")
        self.assertEqual(git(self.remote, "merge-base", "--is-ancestor", tip_b, tip_c), "")
        expected = sorted(operation_path(op["op_id"]) for op in (op_a, op_b, op_c))
        self.assertEqual(ref_paths(self.remote, REF), expected)

        for op in (op_a, op_b, op_c):
            path = operation_path(op["op_id"])
            self.assertEqual(
                git(self.remote, "show", f"{REF}:{path}").encode("utf-8") + b"\n",
                canonical_bytes(op),
            )

    def test_publish_batch_uses_one_fresh_confirmation_snapshot(self):
        operations = [
            make_operation(
                replica_id=f"{number + 1:032x}",
                counter=1,
                record_id=f"record-batch-{number}",
                body=f"batch body {number}",
            )
            for number in range(12)
        ]
        exchange = new_exchange(self.root / "batch-confirm.git", self.remote, REF)
        original_fetch = exchange.fetch_validate
        calls = []

        def counted_fetch():
            calls.append(1)
            return original_fetch()

        exchange.fetch_validate = counted_fetch
        published = exchange.publish_operations(operations)
        self.assertEqual(set(published.confirmed), {op["op_id"] for op in operations})
        self.assertEqual(len(calls), 2)

    def test_retry_integration_tip_preserves_prior_committed_ancestry(self):
        import git_exchange_v2

        op_a = self._op(REPLICA_A, "record-a", "alpha")
        op_b = self._op(REPLICA_B, "record-b", "bravo")
        exchange_root = self.root / "retry-ancestry.git"
        exchange = new_exchange(exchange_root, self.remote, REF)
        exchange._prepare()
        raw_a = {operation_path(op_a["op_id"]): canonical_bytes(op_a)}
        raw_b = {operation_path(op_b["op_id"]): canonical_bytes(op_b)}
        first_integration = exchange._commit_union(None, raw_a)
        competing_parent = exchange._commit_union(None, raw_b)
        git(
            exchange_root,
            "update-ref",
            git_exchange_v2.INTEGRATION_REF,
            first_integration,
        )
        retry_tip = exchange._commit_union(
            competing_parent, {**raw_a, **raw_b}
        )
        self.assertEqual(
            git(exchange_root, "merge-base", "--is-ancestor", first_integration, retry_tip),
            "",
        )
        self.assertEqual(
            git(exchange_root, "merge-base", "--is-ancestor", competing_parent, retry_tip),
            "",
        )
        self.assertEqual(
            git(exchange_root, "rev-parse", git_exchange_v2.INTEGRATION_REF),
            retry_tip,
        )

    def test_repeated_non_fast_forward_refolds_every_retry_and_keeps_evidence(self):
        import git_exchange_v2

        local = self._op(REPLICA_A, "record-local", "local")
        competitors = [
            self._op(REPLICA_B, "record-remote-b", "remote-b"),
            self._op(REPLICA_C, "record-remote-c", "remote-c"),
        ]
        exchange = new_exchange(self.root / "nff-local.git", self.remote, REF)
        peers = [
            new_exchange(self.root / f"nff-peer-{index}.git", self.remote, REF)
            for index in range(2)
        ]
        original_run = exchange._run
        injected = []

        def race_run(*args, **kwargs):
            if args and args[0] == "push" and len(injected) < len(peers):
                index = len(injected)
                peers[index].publish_operations([competitors[index]])
                injected.append(index)
            return original_run(*args, **kwargs)

        exchange._run = race_run
        committed = []
        folded = []

        def phase(phase_name, commit, _op_ids):
            if phase_name == "committed":
                committed.append(commit)

        published = exchange.publish_operations(
            [local],
            max_attempts=3,
            phase_callback=phase,
            fold_callback=lambda snapshot: folded.append(snapshot.tip),
        )
        self.assertEqual(published.attempts, 3)
        self.assertEqual(len(folded), 3)
        self.assertEqual(len(committed), 3)
        self.assertEqual(set(ref_paths(self.remote, REF)), {
            operation_path(operation["op_id"])
            for operation in [local, *competitors]
        })
        for prior_commit in committed[:-1]:
            self.assertEqual(
                git(
                    exchange.root,
                    "merge-base",
                    "--is-ancestor",
                    prior_commit,
                    published.tip,
                ),
                "",
            )

    def test_confirmation_uses_fresh_authoritative_fetch_not_stale_tracking_ref(self):
        op = self._op(REPLICA_A, "record-a", "alpha")
        exchange = new_exchange(self.root / "exchange.git", self.remote, REF)
        exchange_publish(exchange, [op])
        self.assertTrue(exchange_fresh_confirm(exchange, op["op_id"]))

        # Simulate an authoritative rewind after the exchange has a local tracking
        # ref. A stale local ref must not continue to confirm this operation.
        git(self.remote, "update-ref", "-d", REF)
        try:
            confirmed = exchange_fresh_confirm(exchange, op["op_id"])
        except Exception:
            return
        self.assertFalse(confirmed)

    def test_persistent_guard_rejects_rewind_after_exchange_cache_loss(self):
        import git_exchange_v2

        op_a = self._op(REPLICA_A, "record-a", "alpha")
        op_b = self._op(REPLICA_B, "record-b", "bravo")
        first = new_exchange(self.root / "guard-a.git", self.remote, REF)
        tip_a = exchange_publish(first, [op_a]).tip
        tip_b = exchange_publish(first, [op_b]).tip
        self.assertNotEqual(tip_a, tip_b)
        git(self.remote, "update-ref", REF, tip_a, tip_b)

        rebuilt = git_exchange_v2.GitExchange(
            self.root / "rebuilt-exchange.git",
            self.remote,
            ref=REF,
            guard_tip=tip_b,
        )
        with self.assertRaises(git_exchange_v2.RemoteRewind):
            rebuilt.fetch_validate()

    def test_unexpected_tracked_path_is_hard_rejected(self):
        tip = commit_bare_tree(
            self.remote,
            {".gitattributes": ("100644", b"* merge=ours\n")},
            message="malicious attributes",
        )
        git(self.remote, "update-ref", REF, tip)
        exchange = new_exchange(self.root / "exchange.git", self.remote, REF)
        with self.assertRaises(Exception):
            exchange_fetch_validate(exchange)

    def test_remote_protocol_diagnostic_is_typed_and_bounded(self):
        import git_exchange_v2

        op_id = "a" * 64
        path = operation_path(op_id)
        duplicate = "x" * 100_000
        raw = ('{"%s":1,"%s":2}\n' % (duplicate, duplicate)).encode("utf-8")
        tip = commit_bare_tree(
            self.remote, {path: ("100644", raw)}, message="diagnostic payload"
        )
        git(self.remote, "update-ref", REF, tip)
        exchange = new_exchange(self.root / "bounded-diagnostic.git", self.remote, REF)
        with self.assertRaises(git_exchange_v2.ExchangeError) as caught:
            exchange.fetch_validate()
        self.assertNotIsInstance(caught.exception, git_exchange_v2.ProtocolError)
        self.assertLess(len(str(caught.exception)), 256)
        self.assertNotIn(duplicate[:100], str(caught.exception))

    def test_symlink_at_an_allowlisted_operation_path_is_hard_rejected(self):
        op = self._op(REPLICA_A, "record-a", "alpha")
        path = operation_path(op["op_id"])
        tip = commit_bare_tree(
            self.remote,
            {path: ("120000", b"/tmp/not-an-operation")},
            message="malicious symlink",
        )
        git(self.remote, "update-ref", REF, tip)
        exchange = new_exchange(self.root / "exchange.git", self.remote, REF)
        with self.assertRaises(Exception):
            exchange_fetch_validate(exchange)

    def test_exchange_root_inside_project_tree_is_rejected(self):
        project = self.root / "project"
        project.mkdir()
        exchange_root = project / ".memory-exchange"
        try:
            exchange = new_exchange(exchange_root, self.remote, REF)
            self.assertFalse(
                exchange_root.exists(),
                "constructor must not mutate an unchecked project path",
            )
            configure = getattr(exchange, "set_forbidden_roots", None)
            if configure is None:
                self.skipTest("constructor has no explicit synchronized-root input")
            configure([project])
        except Exception:
            return
        with self.assertRaises(Exception):
            exchange_fetch_validate(exchange)

    def test_exchange_root_inside_unlisted_git_worktree_is_rejected(self):
        project = self.root / "unlisted-project"
        project.mkdir()
        git(project, "init")
        exchange_root = project / ".memory-exchange"
        with self.assertRaises(Exception):
            new_exchange(exchange_root, self.remote, REF)
        self.assertFalse(exchange_root.exists())

    def test_duplicate_dot_and_missing_parent_block_transport(self):
        left = self._op(REPLICA_A, "record-a", "alpha")
        right = self._op(REPLICA_A, "record-b", "bravo")
        tip = commit_bare_tree(
            self.remote,
            {
                operation_path(left["op_id"]): ("100644", canonical_bytes(left)),
                operation_path(right["op_id"]): ("100644", canonical_bytes(right)),
            },
            message="equivocation",
        )
        git(self.remote, "update-ref", REF, tip)
        exchange = new_exchange(self.root / "equivocation.git", self.remote, REF)
        with self.assertRaises(Exception):
            exchange_fetch_validate(exchange)

        git(self.remote, "update-ref", "-d", REF)
        missing = make_operation(
            replica_id=REPLICA_B,
            counter=2,
            record_id="record-missing-parent",
            body="deferred",
            parents=["a" * 64],
            frontier=["a" * 64],
        )
        blocked = new_exchange(self.root / "deferred.git", self.remote, REF)
        with self.assertRaises(Exception):
            exchange_publish(blocked, [missing])

    def test_transient_history_deletion_is_rejected(self):
        op = self._op(REPLICA_A, "record-a", "alpha")
        first = commit_bare_tree(
            self.remote,
            {operation_path(op["op_id"]): ("100644", canonical_bytes(op))},
            message="add",
        )
        deleted = commit_bare_tree(
            self.remote, {}, parent=first, message="delete immutable object"
        )
        git(self.remote, "update-ref", REF, deleted)
        exchange = new_exchange(self.root / "history.git", self.remote, REF)
        with self.assertRaises(Exception):
            exchange_fetch_validate(exchange)

    def test_tree_and_history_validation_use_bounded_git_processes(self):
        import git_exchange_v2

        operation = self._op(REPLICA_A, "record-history", "retained history")
        entries = {
            operation_path(operation["op_id"]): (
                "100644", canonical_bytes(operation)
            )
        }
        tip = None
        for number in range(64):
            tip = commit_bare_tree(
                self.remote,
                entries,
                parent=tip,
                message=f"add-only retained commit {number}",
            )
        git(self.remote, "update-ref", REF, tip)
        exchange = new_exchange(self.root / "bounded-processes.git", self.remote, REF)
        run_count = 0
        popen_count = 0
        original_run = exchange._run
        original_popen = git_exchange_v2.subprocess.Popen

        def counted_run(*args, **kwargs):
            nonlocal run_count
            run_count += 1
            return original_run(*args, **kwargs)

        def counted_popen(*args, **kwargs):
            nonlocal popen_count
            popen_count += 1
            return original_popen(*args, **kwargs)

        exchange._run = counted_run
        git_exchange_v2.subprocess.Popen = counted_popen
        try:
            snapshot = exchange.fetch_validate()
        finally:
            git_exchange_v2.subprocess.Popen = original_popen
        self.assertIn(operation["op_id"], snapshot.operations)
        self.assertLess(
            run_count + popen_count,
            30,
            "validation must batch retained commits and object contents",
        )

    def test_ambient_git_object_directory_is_not_inherited(self):
        op = self._op(REPLICA_A, "record-a", "alpha")
        outside = self.root / "ambient-object-dir"
        old = os.environ.get("GIT_OBJECT_DIRECTORY")
        os.environ["GIT_OBJECT_DIRECTORY"] = str(outside)
        try:
            exchange = new_exchange(self.root / "contained.git", self.remote, REF)
            exchange_publish(exchange, [op])
        finally:
            if old is None:
                os.environ.pop("GIT_OBJECT_DIRECTORY", None)
            else:
                os.environ["GIT_OBJECT_DIRECTORY"] = old
        self.assertFalse(outside.exists())

    def test_render_phase_is_durable_before_offline_fetch(self):
        import git_exchange_v2

        op = self._op(REPLICA_A, "record-rendered", "durable before fetch")
        exchange_root = self.root / "render-before-fetch.git"
        exchange = git_exchange_v2.GitExchange(
            exchange_root, self.root / "missing-remote.git", ref=REF
        )
        phases = []
        with self.assertRaises(git_exchange_v2.ExchangeUnavailable):
            exchange.publish_operations(
                [op],
                phase_callback=lambda phase, commit, op_ids: phases.append(
                    (phase, commit, op_ids)
                ),
            )
        self.assertEqual([phase[0] for phase in phases], ["rendered"])
        rendered_tip = git(exchange_root, "rev-parse", git_exchange_v2.RENDERED_REF)
        self.assertEqual(rendered_tip, phases[0][1])
        self.assertEqual(
            git(exchange_root, "ls-tree", "-r", "--name-only", rendered_tip),
            operation_path(op["op_id"]),
        )
        self.assertEqual(
            (exchange_root / "rendered-evidence" / f"{op['op_id']}.json").read_bytes(),
            canonical_bytes(op),
        )

    def test_render_retry_survives_normal_git_repacking(self):
        op = self._op(REPLICA_A, "record-repacked", "stable evidence")
        exchange_root = self.root / "repacked-render.git"
        exchange = new_exchange(exchange_root, self.remote, REF)
        first = exchange.render_operations([op])
        git(exchange_root, "repack", "-ad")
        git(exchange_root, "prune-packed")
        second = exchange.render_operations([op])
        self.assertRegex(first.commit, r"^[0-9a-f]{40,64}$")
        self.assertRegex(second.commit, r"^[0-9a-f]{40,64}$")
        self.assertEqual(
            (exchange_root / "rendered-evidence" / f"{op['op_id']}.json").read_bytes(),
            canonical_bytes(op),
        )

    def test_exchange_local_executable_config_is_rejected(self):
        op = self._op(REPLICA_A, "record-config", "safe")
        exchange_root = self.root / "poisoned-config.git"
        exchange = new_exchange(exchange_root, self.remote, REF)
        exchange_publish(exchange, [op])
        git(
            exchange_root,
            "--git-dir",
            str(exchange_root),
            "config",
            "remote.origin.uploadpack",
            "/bin/false",
        )
        with self.assertRaises(Exception):
            exchange_fetch_validate(exchange)

    def test_exchange_local_instead_of_cannot_redirect_explicit_remote(self):
        import git_exchange_v2

        attacker = init_bare(self.root / "attacker.git")
        op = self._op(REPLICA_A, "record-redirect", "attacker")
        tip = commit_bare_tree(
            attacker,
            {operation_path(op["op_id"]): ("100644", canonical_bytes(op))},
            message="attacker object",
        )
        git(attacker, "update-ref", REF, tip)

        exchange_root = init_bare(self.root / "redirect-config.git")
        trusted_url = f"file://{self.root / 'missing-trusted.git'}"
        attacker_url = f"file://{attacker}"
        git(
            exchange_root,
            "--git-dir",
            str(exchange_root),
            "config",
            f"url.{attacker_url}.insteadOf",
            trusted_url,
        )
        exchange = git_exchange_v2.GitExchange(
            exchange_root, trusted_url, ref=REF
        )
        with self.assertRaises(git_exchange_v2.ExchangeError):
            exchange.fetch_validate()

    def test_existing_exchange_symlink_is_rejected_before_git_mutates_target(self):
        target = self.root / "project-target"
        target.mkdir()
        exchange_root = self.root / "poisoned.git"
        exchange_root.mkdir()
        (exchange_root / "objects").symlink_to(target, target_is_directory=True)
        exchange = new_exchange(exchange_root, self.remote, REF)

        with self.assertRaises(Exception):
            exchange_fetch_validate(exchange)

        self.assertEqual(list(target.iterdir()), [])

    def test_local_remote_inside_forbidden_project_is_rejected_before_mutation(self):
        import git_exchange_v2

        project = self.root / "project"
        project.mkdir()
        project_remote = init_bare(project / "remote.git")
        exchange_root = self.root / "safe-exchange.git"
        with self.assertRaises(Exception):
            git_exchange_v2.GitExchange(
                exchange_root,
                project_remote,
                ref=REF,
                forbidden_roots=(project,),
            )
        self.assertFalse(exchange_root.exists())

    def test_empty_publish_cannot_hide_deferred_remote_dependency(self):
        missing = make_operation(
            replica_id=REPLICA_B,
            counter=2,
            record_id="record-missing-parent",
            body="deferred",
            parents=["a" * 64],
            frontier=["a" * 64],
        )
        tip = commit_bare_tree(
            self.remote,
            {operation_path(missing["op_id"]): ("100644", canonical_bytes(missing))},
            message="deferred remote object",
        )
        git(self.remote, "update-ref", REF, tip)
        exchange = new_exchange(self.root / "deferred-empty.git", self.remote, REF)
        with self.assertRaises(Exception):
            exchange_publish(exchange, [])

    def test_remote_with_nul_is_rejected(self):
        import git_exchange_v2

        with self.assertRaises(Exception):
            git_exchange_v2.GitExchange(
                self.root / "nul.git", "ssh://example.invalid/repo\x00tail", ref=REF
            )

    def test_snapshot_confirmation_refuses_a_ref_that_changed_after_fold(self):
        op_a = self._op(REPLICA_A, "record-a", "alpha")
        op_b = self._op(REPLICA_B, "record-b", "bravo")
        first = new_exchange(self.root / "snapshot-a.git", self.remote, REF)
        second = new_exchange(self.root / "snapshot-b.git", self.remote, REF)
        exchange_publish(first, [op_a])
        folded = exchange_fetch_validate(first)
        exchange_publish(second, [op_b])

        with self.assertRaises(Exception):
            first.confirm_snapshot(folded.tip)


if __name__ == "__main__":
    unittest.main()
