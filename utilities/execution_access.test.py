#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from execution_access import (
    AccessContext,
    ExecutionAccessError,
    ParentGrant,
    adapter_default_roots,
    assert_within_parent,
    build_grant,
    load_request,
    load_parent_effective_grant,
    receipt_fields,
    request_path,
    publish_effective_grant,
    prepare_task_request,
    compute_execution_scope,
    read_roots_data,
    resolve_task_targets,
)
import execution_access as EA


class ExecutionAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.worktree = self.root / "worktree"
        self.artifact = self.root / "artifact"
        self.state = self.root / "state" / "dispatch"
        self.agent_home = self.root / "install" / "hearting"
        for path in (self.home, self.worktree, self.artifact, self.state, self.agent_home):
            path.mkdir(parents=True)
        self.env = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
        }
        self.context = AccessContext.build(
            worktree=self.worktree,
            artifact_root=self.artifact,
            dispatch_state_root=self.state,
            agent_home=self.agent_home,
            environ=self.env,
        )
        self.request_file = self.root / "request.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def request(self, **changes: object):
        writable = self.root / "scoped" / "write"
        readable = self.root / "scoped" / "read"
        data = {
            "schema_version": 1,
            "writable_roots": [str(writable)],
            "read_roots": [str(readable)],
            "network": {"required": False, "reason": "", "hosts": []},
            "enforcement_required": "any",
            "justification": {str(writable): "build output"},
        }
        data.update(changes)
        self.request_file.write_text(json.dumps(data), encoding="utf-8")
        return load_request(self.request_file, context=self.context)

    def assert_reason(self, expected: str, **changes: object) -> None:
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(**changes)
        self.assertEqual(expected, raised.exception.reason)

    def lab_route(self, **changes: object):
        route = {
            "route_id": "rt-lab-access",
            "route_hash": "sha256:" + "a" * 64,
            "capability": "autopilot-lab",
            "nodes": [{"id": "smoke", "kind": "review-worker"},
                      {"id": "full-run", "kind": "resource-runner"},
                      {"id": "run-verify", "kind": "review-worker"}],
            "cwd": str(self.worktree),
            "artifact_root": str(self.artifact),
            "work_request": {"text": "Use /data/unapproved-text for the experiment"},
        }
        route.update(changes)
        return route

    def inventory(self, run_root: Path) -> Path:
        path = self.root / "compute-hosts.yaml"
        path.write_text(
            f"schema_version: 1\nrun_root: {run_root}\nhosts:\n"
            "  fixture:\n    ssh_host: local\n",
            encoding="utf-8",
        )
        return path

    def test_lab_owner_uses_exact_inventory_root_without_creating_run_storage(self) -> None:
        run_root = self.root / "custom-inventory" / "runs"
        inventory = self.inventory(run_root)
        with mock.patch.dict(os.environ, {**self.env, "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            prepared = prepare_task_request(self.lab_route(), self.state / "jobs.log")
            again = prepare_task_request(self.lab_route(), self.state / "jobs.log")
        self.assertEqual(prepared, again)
        request = load_request(prepared, context=self.context)
        self.assertEqual((run_root,), request.writable_roots)
        self.assertFalse(run_root.exists())
        self.assertNotIn(Path("/data/unapproved-text"), request.writable_roots)

    def test_compute_need_comes_only_from_registered_native_or_borrowed_stage_identity(self) -> None:
        native = self.lab_route(nodes=[{"id": "full-run"}])
        borrowed = self.lab_route(capability="autopilot-code", nodes=[
            {"id": "lab-run", "part": "autopilot-lab:full-run"}])
        misleading = self.lab_route(capability="autopilot-code", nodes=[{"id": "full-run"}],
                                    work_request={"text": "compute-hosts GPU run"})
        self.assertTrue(compute_execution_scope(native, owner=True))
        self.assertTrue(compute_execution_scope(borrowed, owner=True))
        self.assertFalse(compute_execution_scope(misleading, owner=True))
        self.assertFalse(compute_execution_scope(self.lab_route(nodes=[]), owner=True))

    def test_inventory_default_is_not_given_to_code_owners_or_frames(self) -> None:
        inventory = self.inventory(self.root / "runs")
        with mock.patch.dict(os.environ, {**self.env, "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            self.assertIsNone(prepare_task_request(
                self.lab_route(capability="autopilot-code"), self.state / "jobs.log"))
            self.assertIsNone(prepare_task_request(
                self.lab_route(), self.state / "jobs.log", node="frame"))
        self.assertFalse((self.state / "execution-access").exists())

    def test_missing_or_template_inventory_does_not_guess_a_lab_root(self) -> None:
        inventory = self.root / "compute-hosts.yaml"
        with mock.patch.dict(os.environ, {**self.env, "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            self.assertIsNone(prepare_task_request(self.lab_route(), self.state / "jobs.log"))
            inventory.write_text("schema_version: 1\nhosts:\n# uninitialized\n", encoding="utf-8")
            self.assertIsNone(prepare_task_request(self.lab_route(), self.state / "jobs.log"))
        self.assertFalse((self.state / "execution-access").exists())

    def test_invalid_inventory_and_broad_run_root_refuse_before_publishing(self) -> None:
        inventory = self.inventory(self.home)
        with mock.patch.dict(os.environ, {**self.env, "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            with self.assertRaises(ExecutionAccessError) as raised:
                prepare_task_request(self.lab_route(), self.state / "jobs.log")
            self.assertTrue(raised.exception.reason.startswith("execution-access-root-too-broad:"))
            inventory.write_text("schema_version: 2\n", encoding="utf-8")
            with self.assertRaises(ExecutionAccessError) as raised:
                prepare_task_request(self.lab_route(), self.state / "jobs.log")
            self.assertEqual("execution-access-compute-inventory-invalid", raised.exception.reason)
        self.assertFalse((self.state / "execution-access").exists())

    def test_lab_explicit_data_request_is_validated_preserved_and_delivered(self) -> None:
        run_root = self.root / "runs"
        inventory = self.inventory(run_root)
        supplied = self.request(network={"required": True, "reason": "approved transfer", "hosts": ["fixture.invalid:22"]})
        original = self.request_file.read_bytes()
        # Explicit data wins over a malformed old preview-table reference.
        route = self.lab_route(work_request={"text": "## 입력\n- 루트 목록과 경로: unsupported ROOTS input\n"})
        with mock.patch.dict(os.environ, {
            **self.env, "COMPUTE_HOSTS_CONFIG": str(inventory),
            "AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(self.request_file),
        }, clear=True):
            prepared = prepare_task_request(route, self.state / "jobs.log")
        merged = load_request(prepared, context=self.context)
        self.assertEqual(set((*supplied.writable_roots, run_root)), set(merged.writable_roots))
        self.assertEqual(supplied.read_roots, merged.read_roots)
        self.assertEqual(supplied.network_hosts, merged.network_hosts)
        self.assertEqual(supplied.network_reason, merged.network_reason)
        self.assertEqual(supplied.enforcement_required, merged.enforcement_required)
        self.assertEqual(original, self.request_file.read_bytes())

    def test_invalid_explicit_data_does_not_publish_partial_lab_grant(self) -> None:
        inventory = self.inventory(self.root / "runs")
        self.request_file.write_text("{", encoding="utf-8")
        with mock.patch.dict(os.environ, {
            **self.env, "COMPUTE_HOSTS_CONFIG": str(inventory),
            "AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(self.request_file),
        }, clear=True):
            with self.assertRaises(ExecutionAccessError) as raised:
                prepare_task_request(self.lab_route(), self.state / "jobs.log")
        self.assertEqual("execution-access-invalid-json", raised.exception.reason)
        self.assertFalse((self.state / "execution-access").exists())

    def test_lab_resource_request_cannot_expand_the_live_parent_grant(self) -> None:
        inventory = self.inventory(self.root / "runs")
        with mock.patch.dict(os.environ, {**self.env, "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            prepared = prepare_task_request(self.lab_route(), self.state / "jobs.log")
        request = load_request(prepared, context=self.context)
        parent = ParentGrant(writable_roots=request.writable_roots, network_allowed=request.network_required)
        assert_within_parent(request, parent, is_child=True)
        outside = self.request(read_roots=[], writable_roots=[str(self.root / "outside")], justification={})
        with self.assertRaises(ExecutionAccessError) as raised:
            assert_within_parent(outside, parent, is_child=True)
        self.assertTrue(raised.exception.reason.startswith("execution-access-exceeds-parent:"))

    def test_valid_request_normalizes_and_hashes_stably(self) -> None:
        first = self.request(
            writable_roots=[str(self.root / "z"), str(self.root / "a"), str(self.root / "z")],
            read_roots=[],
            justification={},
        )
        second = self.request(
            writable_roots=[str(self.root / "a"), str(self.root / "z")],
            read_roots=[],
            justification={},
        )
        self.assertEqual((self.root / "a", self.root / "z"), first.writable_roots)
        self.assertEqual(first.request_sha256, second.request_sha256)
        self.assertEqual(64, len(first.request_sha256))

    def test_host_ports_are_typed_and_hash_canonical(self) -> None:
        first = self.request(
            read_roots=[],
            network={
                "required": True,
                "reason": "bounded connect",
                "hosts": ["CNN.example:00022", "[2001:0DB8::1]:00443"],
            },
            justification={},
        )
        second = self.request(
            read_roots=[],
            network={
                "required": True,
                "reason": "bounded connect",
                "hosts": ["cnn.example:22", "[2001:db8::1]:443"],
            },
            justification={},
        )
        self.assertEqual(
            ("[2001:db8::1]:443", "cnn.example:22"), first.network_hosts
        )
        self.assertEqual(first.request_sha256, second.request_sha256)

        for host in (
            "cnn.example:ssh",
            "cnn.example:",
            "cnn.example:+22",
            "cnn.example:-22",
            "cnn.example:０２２",
            "cnn.example:0",
            "cnn.example:65536",
            ":22",
            "[2001:db8::1]:",
            "[2001:db8::1]:ssh",
        ):
            with self.subTest(host=host):
                with self.assertRaises(ExecutionAccessError) as raised:
                    self.request(
                        read_roots=[],
                        network={
                            "required": True,
                            "reason": "bounded connect",
                            "hosts": [host],
                        },
                        justification={},
                    )
                self.assertEqual(
                    "execution-access-field-invalid:network.hosts",
                    raised.exception.reason,
                )

    def test_schema_and_fields_are_strict(self) -> None:
        self.assert_reason("execution-access-schema-unsupported:v2", schema_version=2)
        self.assert_reason(
            "execution-access-field-invalid:surprise", surprise=True
        )
        self.assert_reason(
            "execution-access-field-invalid:network.required",
            network={"required": "yes", "reason": "", "hosts": []},
        )
        self.assert_reason(
            "execution-access-field-invalid:enforcement_required",
            enforcement_required="best-effort",
        )
        self.assert_reason(
            "execution-access-field-invalid:network.required",
            writable_roots=["relative/path"],
            read_roots=[],
            justification={},
            network={"required": "yes", "reason": "", "hosts": []},
        )

    def test_request_file_validation_precedes_json(self) -> None:
        missing = self.root / "missing.json"
        with self.assertRaises(ExecutionAccessError) as raised:
            load_request(missing, context=self.context)
        self.assertEqual("execution-access-unreadable", raised.exception.reason)
        self.request_file.write_text("{", encoding="utf-8")
        with self.assertRaises(ExecutionAccessError) as raised:
            load_request(self.request_file, context=self.context)
        self.assertEqual("execution-access-invalid-json", raised.exception.reason)
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        self.request_file.unlink()
        self.request_file.symlink_to(target)
        with self.assertRaises(ExecutionAccessError) as raised:
            load_request(self.request_file, context=self.context)
        self.assertEqual("execution-access-unreadable", raised.exception.reason)

    def test_special_request_file_is_rejected_before_nonblocking_open(self) -> None:
        fifo = self.root / "request.fifo"
        os.mkfifo(fifo)
        with mock.patch("execution_access.os.open") as opened:
            with self.assertRaises(ExecutionAccessError) as raised:
                load_request(fifo, context=self.context)
        self.assertEqual("execution-access-unreadable", raised.exception.reason)
        opened.assert_not_called()

        with self.assertRaises(ExecutionAccessError) as raised:
            load_request(Path(str(self.root / "request") + "\0.json"), context=self.context)
        self.assertEqual("execution-access-unreadable", raised.exception.reason)

        self.request(read_roots=[], justification={})
        real_open = os.open
        with mock.patch("execution_access.os.open", wraps=real_open) as opened:
            load_request(self.request_file, context=self.context)
        request_open = next(
            call for call in opened.call_args_list if Path(call.args[0]) == self.request_file
        )
        self.assertTrue(request_open.args[1] & getattr(os, "O_NONBLOCK", 0))

    def test_resolve_failures_and_deep_json_are_typed(self) -> None:
        loop_a = self.root / "loop-a"
        loop_b = self.root / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(writable_roots=[str(loop_a)], read_roots=[], justification={})
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-path-invalid:")
        )

        valid = {
            "schema_version": 1,
            "writable_roots": [str(self.root / "scoped")],
            "read_roots": [],
            "network": {"required": False, "reason": "", "hosts": []},
            "enforcement_required": "any",
            "justification": {},
        }
        self.request_file.write_text(json.dumps(valid), encoding="utf-8")
        with mock.patch.object(Path, "resolve", side_effect=OSError("resolver failed")):
            with self.assertRaises(ExecutionAccessError) as raised:
                load_request(self.request_file, context=self.context)
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-path-invalid:")
        )

        self.request_file.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
        with self.assertRaises(ExecutionAccessError) as raised:
            load_request(self.request_file, context=self.context)
        self.assertEqual("execution-access-invalid-json", raised.exception.reason)

    def test_symlink_loop_is_invalid_even_when_resolve_does_not_raise(self) -> None:
        # Python 3.13 stopped raising RuntimeError for a loop in non-strict
        # resolve() and returns the unresolved path instead; mimic that so the
        # check is exercised on every interpreter, not only on 3.13+.
        loop_a = self.root / "loop-a"
        loop_b = self.root / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        original = Path.resolve

        def resolve_like_py313(path: Path, strict: bool = False) -> Path:
            try:
                return original(path, strict=strict)
            except RuntimeError:
                return Path(os.path.abspath(path))

        with mock.patch.object(Path, "resolve", resolve_like_py313):
            for roots in ({"writable_roots": [str(loop_a / "leaf")], "read_roots": []},
                          {"writable_roots": [], "read_roots": [str(loop_a)]}):
                with self.subTest(**roots):
                    with self.assertRaises(ExecutionAccessError) as raised:
                        self.request(justification={}, **roots)
                    self.assertTrue(
                        raised.exception.reason.startswith("execution-access-path-invalid:")
                    )

    def test_request_surface_cli_wins_and_environment_is_fallback(self) -> None:
        cli = self.root / "cli.json"
        inherited = self.root / "inherited.json"
        self.assertEqual(
            cli,
            request_path(
                str(cli),
                {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(inherited)},
            ),
        )
        self.assertEqual(
            inherited,
            request_path(None, {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(inherited)}),
        )
        self.assertIsNone(request_path(None, {}))
        self.assertEqual(
            Path(""),
            request_path("", {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(inherited)}),
        )

    def test_invalid_path_forms(self) -> None:
        bad_values = (
            "relative/path",
            "file:///tmp/value",
            "~/value",
            "/tmp/*",
            "/tmp/../etc",
            "/tmp/with,comma",
            "/tmp/with=equals",
            "/tmp/with\tcontrol",
            "/tmp/with\nnewline",
            "/" + "x" * 4097,
        )
        for value in bad_values:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ExecutionAccessError) as raised:
                    self.request(writable_roots=[value], read_roots=[], justification={})
                self.assertTrue(
                    raised.exception.reason.startswith("execution-access-path-invalid:")
                )
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(
                writable_roots=[str(self.root / "roots" / str(index)) for index in range(17)],
                read_roots=[],
                justification={},
            )
        self.assertEqual("execution-access-path-invalid:root-count", raised.exception.reason)

    def test_symlink_escape_and_realpath_broad_root(self) -> None:
        declared = self.root / "declared"
        outside = self.root / "outside"
        declared.mkdir()
        outside.mkdir()
        (declared / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(
                writable_roots=[str(declared), str(declared / "escape")],
                read_roots=[],
                justification={},
            )
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-path-symlink-escape:")
        )

        sensitive_link = declared / "sensitive"
        sensitive_link.symlink_to(self.agent_home, target_is_directory=True)
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(
                writable_roots=[str(declared)],
                read_roots=[str(sensitive_link)],
                justification={},
            )
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-path-symlink-escape:")
        )

        broad_link = self.root / "broad-link"
        broad_link.symlink_to(self.agent_home, target_is_directory=True)
        with self.assertRaises(ExecutionAccessError) as raised:
            self.request(writable_roots=[str(broad_link)], read_roots=[], justification={})
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-root-too-broad:")
        )

    def test_broad_roots_rejected_and_defaults_absorbed(self) -> None:
        for path in (
            Path("/"),
            self.home,
            Path("/home"),
            Path("/usr"),
            Path("/etc"),
            self.state,
            self.root / "state",
            self.root,
        ):
            with self.subTest(path=path):
                with self.assertRaises(ExecutionAccessError) as raised:
                    self.request(writable_roots=[str(path)], read_roots=[], justification={})
                self.assertTrue(
                    raised.exception.reason.startswith("execution-access-root-too-broad:")
                )

        request = self.request(
            writable_roots=[str(self.worktree), str(self.artifact)],
            read_roots=[],
            justification={},
        )
        grant = build_grant(
            request,
            runtime="codex-exec",
            default_writable_roots=(self.worktree, self.artifact),
            network_available=True,
        )
        self.assertEqual((), grant.additional_writable_roots)
        self.assertEqual((self.artifact, self.worktree), grant.absorbed_writable_roots)
        args = type(
            "Args",
            (),
            {
                "worktree": str(self.worktree),
                "artifact_root": str(self.artifact),
                "report_bundle_root": None,
                "runtime_dir": str(self.worktree.parent / "runtime"),
            },
        )()
        self.assertEqual(
            (self.artifact, self.worktree.parent / "runtime", self.worktree), adapter_default_roots(args)
        )

    def test_parent_monotonicity_is_fail_closed(self) -> None:
        request = self.request(read_roots=[], justification={})
        with self.assertRaises(ExecutionAccessError) as raised:
            assert_within_parent(request, None, is_child=True)
        self.assertEqual(
            "execution-access-exceeds-parent:parent-grant-unknown",
            raised.exception.reason,
        )

        parent = ParentGrant(writable_roots=(self.root / "different",))
        with self.assertRaises(ExecutionAccessError) as raised:
            assert_within_parent(request, parent, is_child=True)
        self.assertTrue(
            raised.exception.reason.startswith("execution-access-exceeds-parent:")
        )
        self.assertIn("top-level launch", raised.exception.detail)

        parent = ParentGrant(writable_roots=(self.root / "scoped",))
        assert_within_parent(request, parent, is_child=True)

    def test_roots_reader_selects_only_named_rows_from_data_block(self) -> None:
        manifest = self.root / "run_all.sh"
        selected = tuple(f"project-{index}" for index in range(12))
        rows = [f"{name}|{self.root}/external/{index}/.agent_reports"
                for index, name in enumerate(selected)]
        rows.append(f"unrequested-13|{self.root}/external/13/.agent_reports")
        manifest.write_text(
            "#!/bin/sh\ndone <<'ROOTS'\n" + "\n".join(rows) + "\nROOTS\n",
            encoding="utf-8",
        )
        targets = read_roots_data(manifest, selected)
        self.assertEqual(selected, targets.selected_names)
        self.assertEqual(12, len(targets.writable_roots))
        self.assertNotIn(self.root / "external/13/.agent_reports", targets.writable_roots)
        self.assertEqual(64, len(targets.manifest_sha256))

    def linked_roots_fixture(self):
        primary = self.root / "primary"
        linked = self.root / "linked"
        primary.mkdir()
        subprocess.run(["git", "init", "-q", str(primary)], check=True)
        subprocess.run(["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "init"], check=True)
        subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "fixture-linked", str(linked)], check=True)
        canonical = primary / ".agent_reports"
        canonical.mkdir()
        return primary, linked, canonical

    @staticmethod
    def target_route(cwd: Path, artifact_root: Path, *, preview=".agent_reports/_scratch/flow"):
        return {
            "route_id": "rt-test",
            "route_hash": "sha256:" + "a" * 64,
            "cwd": str(cwd),
            "artifact_root": str(artifact_root),
            "work_request": {"text": (
                "## 입력\n"
                f"- 미리보기(사용자가 본 것): {preview}/previews/<루트>.md\n"
                "- 루트 목록과 경로: previews/run_all.sh 의 ROOTS 표(alpha)\n"
            )},
        }

    def test_task_target_resolution_uses_canonical_artifact_root_for_linked_worktree(self) -> None:
        primary, linked, canonical = self.linked_roots_fixture()
        manifest = canonical / "_scratch/flow/previews/run_all.sh"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("done <<'ROOTS'\nalpha|/tmp/canonical/.agent_reports\nROOTS\n", encoding="utf-8")
        # A same-named linked-worktree shadow carries different authority and must be ignored.
        shadow = linked / ".agent_reports/_scratch/flow/previews/run_all.sh"
        shadow.parent.mkdir(parents=True)
        shadow.write_text("done <<'ROOTS'\nalpha|/tmp/shadow/.agent_reports\nROOTS\n", encoding="utf-8")
        route = self.target_route(linked, canonical)
        targets = resolve_task_targets(route)
        self.assertEqual(canonical.resolve(), targets.manifest_path.parent.parent.parent.parent)
        self.assertEqual((Path("/tmp/canonical/.agent_reports"),), targets.writable_roots)

    def test_missing_canonical_roots_never_falls_back_to_linked_shadow(self) -> None:
        _, linked, canonical = self.linked_roots_fixture()
        shadow = linked / ".agent_reports/_scratch/flow/previews/run_all.sh"
        shadow.parent.mkdir(parents=True)
        shadow.write_text("done <<'ROOTS'\nalpha|/tmp/shadow/.agent_reports\nROOTS\n", encoding="utf-8")
        with self.assertRaises(ExecutionAccessError) as raised:
            resolve_task_targets(self.target_route(linked, canonical))
        self.assertEqual("execution-access-target-input-invalid", raised.exception.reason)
        self.assertIn("No such file", raised.exception.detail)

    def test_canonical_roots_symlink_is_not_followed(self) -> None:
        _, linked, canonical = self.linked_roots_fixture()
        real = canonical / "_scratch/flow/previews/real-roots.sh"
        real.parent.mkdir(parents=True)
        real.write_text("done <<'ROOTS'\nalpha|/tmp/outside/.agent_reports\nROOTS\n", encoding="utf-8")
        manifest = canonical / "_scratch/flow/previews/run_all.sh"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.symlink_to(real)
        with self.assertRaises(ExecutionAccessError) as raised:
            resolve_task_targets(self.target_route(linked, canonical))
        self.assertEqual("execution-access-target-input-invalid", raised.exception.reason)
        self.assertIn("non-symlink regular file", raised.exception.detail)

    def test_source_relative_target_keeps_route_cwd_and_noninput_prose_has_no_authority(self) -> None:
        primary, linked, canonical = self.linked_roots_fixture()
        manifest = linked / "source/_scratch/flow/previews/run_all.sh"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("done <<'ROOTS'\nalpha|/tmp/source/.agent_reports\nROOTS\n", encoding="utf-8")
        route = self.target_route(linked, canonical, preview="source/_scratch/flow")
        targets = resolve_task_targets(route)
        self.assertEqual(manifest.resolve(), targets.manifest_path)
        self.assertEqual((Path("/tmp/source/.agent_reports"),), targets.writable_roots)
        route["work_request"] = {"text": "Quoted old task: .agent_reports/_scratch/flow previews/run_all.sh ROOTS(alpha)"}
        self.assertIsNone(resolve_task_targets(route))
        route["work_request"] = {"text": (
            "## 입력\n"
            "- 미리보기(사용자가 본 것): .agent_reports-copy/_scratch/flow/previews/<루트>.md\n"
            "- 루트 목록과 경로: previews/run_all.sh 의 ROOTS 표(alpha)\n"
        )}
        with self.assertRaises(ExecutionAccessError) as raised:
            resolve_task_targets(route)
        self.assertEqual("execution-access-target-input-invalid", raised.exception.reason)
        route["work_request"] = {"text": (
            "## 입력\n"
            "- 미리보기(사용자가 본 것): source/_scratch/flow/previews/<루트>.md\n"
            "- 루트 목록과 경로: other/run.sh 의 ROOTS 표(alpha)\n"
        )}
        with self.assertRaises(ExecutionAccessError) as raised:
            resolve_task_targets(route)
        self.assertEqual("execution-access-target-input-invalid", raised.exception.reason)

    def test_prepared_request_is_route_bound_and_refuses_changed_manifest(self) -> None:
        _, cwd, artifact_root = self.linked_roots_fixture()
        manifest = artifact_root / "_scratch/flow/previews/run_all.sh"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "done <<'ROOTS'\nalpha|/tmp/alpha/.agent_reports\nROOTS\n",
            encoding="utf-8",
        )
        route = {
            "route_id": "rt-test",
            "route_hash": "sha256:" + "a" * 64,
            "cwd": str(cwd),
            "artifact_root": str(artifact_root),
            "work_request": {"text": (
                "## 입력\n"
                "- 미리보기(사용자가 본 것): .agent_reports/_scratch/flow/previews/<루트>.md\n"
                "- 루트 목록과 경로: previews/run_all.sh 의 ROOTS 표(alpha)\n"
            )},
        }
        jobs = self.state / "jobs.log"
        request_file = prepare_task_request(route, jobs)
        self.assertEqual(request_file, prepare_task_request(route, jobs))
        request = json.loads(request_file.read_text(encoding="utf-8"))
        self.assertEqual(["/tmp/alpha/.agent_reports"], request["writable_roots"])
        binding = json.loads(request_file.with_name("binding.json").read_text(encoding="utf-8"))
        self.assertEqual(str(artifact_root.resolve()), binding["artifact_root"])
        self.assertEqual([], request["read_roots"])
        manifest.write_text(
            "done <<'ROOTS'\nalpha|/tmp/alpha/.agent_reports\nbeta|/tmp/beta/.agent_reports\nROOTS\n",
            encoding="utf-8",
        )
        with self.assertRaises(ExecutionAccessError) as raised:
            prepare_task_request(route, jobs)
        self.assertEqual("execution-access-cache-conflict", raised.exception.reason)

    def test_parent_effective_record_is_full_digest_bound_to_live_attempt(self) -> None:
        request = self.request(read_roots=[], justification={})
        grant = build_grant(
            request, runtime="codex-exec", default_writable_roots=(self.worktree,),
            network_available=False, effective_sandbox="workspace-write",
        )
        jobs = self.state / "jobs.log"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        parent_id = "att-parent"
        route_id = "rt-parent"
        route_hash = "sha256:" + "b" * 64
        effective_path, digest = publish_effective_grant(
            jobs=jobs, attempt_id=parent_id, route_id=route_id,
            route_hash=route_hash, runtime="codex-exec", sandbox="workspace-write",
            grant=grant, default_writable_roots=(self.worktree,), network_allowed=False,
        )
        metadata = {
            "attempt_id": parent_id, "route_id": route_id, "route_hash": route_hash,
            "runtime_sandbox": "workspace-write",
            "execution_access_effective_file": str(effective_path),
            "execution_access_effective_sha256": digest,
        }
        jobs.write_text(
            "now\topen\t12\tparent\tparent-slug\t"
            + ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n",
            encoding="utf-8",
        )
        parent = load_parent_effective_grant(
            jobs=jobs, parent_attempt_id=parent_id, context=self.context,
        )
        self.assertIn(self.worktree, parent.writable_roots)
        self.assertIn(self.root / "scoped" / "write", parent.writable_roots)
        self.assertFalse(parent.network_allowed)
        self.assertEqual("att-parent", parent.attempt_id)
        self.assertEqual("codex-exec", parent.runtime)
        self.assertEqual("workspace-write", parent.sandbox)
        self.assertEqual("os-sandbox", parent.file_enforcement)
        self.assertEqual("os-sandbox", parent.network_enforcement)

        metadata["execution_access_effective_sha256"] = "0" * 64
        jobs.write_text(
            "now\topen\t12\tparent\tparent-slug\t"
            + ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(ExecutionAccessError) as raised:
            load_parent_effective_grant(
                jobs=jobs, parent_attempt_id=parent_id, context=self.context,
            )
        self.assertEqual("execution-access-parent-record-digest-mismatch", raised.exception.reason)

    def test_runtime_grades_network_and_receipt(self) -> None:
        request = self.request(
            read_roots=[],
            network={"required": True, "reason": "fetch source", "hosts": ["EXAMPLE.com:443"]},
            justification={},
        )
        grant = build_grant(
            request,
            runtime="codex-exec",
            default_writable_roots=(),
            network_available=True,
        )
        self.assertEqual("granted-unenforced", grant.network)
        self.assertIn("network-hosts-unenforced", grant.unmet)
        fields = receipt_fields(grant)
        self.assertEqual(
            {
                "execution_access_request",
                "execution_access_roots",
                "execution_access_network",
                "execution_access_enforcement",
                "execution_access_unmet",
            },
            set(fields),
        )
        self.assertEqual("os-sandbox", fields["execution_access_enforcement"])

        claude = build_grant(
            request,
            runtime="claude-cli",
            default_writable_roots=(),
        )
        self.assertEqual("granted-unenforced", claude.network)
        self.assertEqual("tool-permission", claude.file_enforcement)
        self.assertEqual("none", claude.network_enforcement)

    def test_unavailable_enforcement_is_typed(self) -> None:
        network = self.request(
            read_roots=[],
            network={"required": True, "reason": "needed", "hosts": []},
            justification={},
        )
        with self.assertRaises(ExecutionAccessError) as raised:
            build_grant(
                network,
                runtime="codex-app-server",
                network_available=False,
            )
        self.assertEqual(
            "execution-access-enforcement-unavailable:codex-network-role-gated",
            raised.exception.reason,
        )

        os_required = self.request(
            read_roots=[], enforcement_required="os-sandbox", justification={}
        )
        with self.assertRaises(ExecutionAccessError) as raised:
            build_grant(os_required, runtime="opencode")
        self.assertEqual(
            "execution-access-enforcement-unavailable:opencode",
            raised.exception.reason,
        )

    def test_codex_unprojectable_writes_and_strict_hosts_are_refused(self) -> None:
        writable = self.request(read_roots=[], justification={})
        with self.assertRaises(ExecutionAccessError) as raised:
            build_grant(
                writable,
                runtime="codex-exec",
                effective_sandbox="read-only",
            )
        self.assertEqual(
            "execution-access-enforcement-unavailable:codex-read-only",
            raised.exception.reason,
        )

        strict_hosts = self.request(
            read_roots=[],
            network={
                "required": True,
                "reason": "strict destination",
                "hosts": ["example.com:443"],
            },
            enforcement_required="os-sandbox",
            justification={},
        )
        with self.assertRaises(ExecutionAccessError) as raised:
            build_grant(
                strict_hosts,
                runtime="codex-app-server",
                network_available=True,
            )
        self.assertEqual(
            "execution-access-enforcement-unavailable:codex-network-hosts",
            raised.exception.reason,
        )


class DerivedAccessTest(unittest.TestCase):
    """A task folder outside the worktree reaches every harness without a hand-written
    request: the task text names read roots, only the approved scope names write roots."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.worktree = self.root / "worktree"
        self.artifact = self.root / "artifact"
        self.state = self.root / "state" / "dispatch"
        self.other = self.root / "projects" / "other"
        self.elsewhere = self.root / "projects" / "elsewhere"
        for path in (self.home / ".ssh", self.worktree / "src", self.artifact, self.state,
                     self.other / "out", self.other / "ref", self.other / "data" / "raw",
                     self.other / "docs", self.elsewhere):
            path.mkdir(parents=True)
        (self.other / "docs" / "prd.md").write_text("spec\n", encoding="utf-8")
        env = {"HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
               "CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "XDG_CONFIG_HOME": str(self.home / ".config")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AGENT_DISPATCH_EXECUTION_ACCESS_FILE", None)
        self.jobs = self.state / "jobs.log"

    def route(self, text, *, capability="autopilot-code"):
        nodes = ([{"id": "full-run", "kind": "resource-runner"}]
                 if capability == "autopilot-lab" else [])
        return {"route_id": "rt-derive", "route_hash": "sha256:" + "d" * 64, "cwd": str(self.worktree),
                "artifact_root": str(self.artifact), "capability": capability,
                "nodes": nodes, "work_request": {"text": text}}

    def task(self):
        o = self.other
        return ("# 다른 프로젝트 정리\n"
                f"범위: {o}/out에 결과 저장, {o}/ref 읽기만, {o}/data/raw 제외\n"
                f"입력: {o}/data 와 {o}/docs/prd.md, 참고 {self.elsewhere}\n"
                f"{self.home}/.ssh/config, {self.worktree}/src, {o}/missing, /tmp\n")

    def prepared(self, path):
        return (json.loads(path.read_text(encoding="utf-8")),
                json.loads(path.with_name("binding.json").read_text(encoding="utf-8")))

    def test_the_approved_scope_writes_and_the_task_text_only_reads(self):
        path = prepare_task_request(self.route(self.task()), self.jobs)
        request, binding = self.prepared(path)
        o = self.other
        self.assertEqual(request["writable_roots"], [str(o / "out")])
        self.assertEqual(request["read_roots"], sorted([str(o / "docs"), str(o / "ref"), str(self.elsewhere)]))
        self.assertIn("approved scope field (line 2, writes:저장)", request["justification"][str(o / "out")])
        self.assertIn("task text (line 3, task text)", request["justification"][str(self.elsewhere)])
        skipped = {row["path"]: row["reason"] for row in binding["derivation"]["skipped"]}
        self.assertEqual(skipped[str(o / "data" / "raw")], "excluded-by-scope")
        self.assertEqual(skipped[str(o / "data")], "holds-excluded-path")
        self.assertEqual(skipped[str(self.home / ".ssh" / "config")], "sensitive")
        self.assertEqual(skipped[str(self.worktree / "src")], "already-granted")
        self.assertEqual(skipped[str(o / "missing")], "missing")
        self.assertEqual(skipped["/tmp"], "too-broad")
        self.assertEqual(path, prepare_task_request(self.route(self.task()), self.jobs))

    def test_every_harness_projects_the_same_derived_roots(self):
        context = AccessContext.build(worktree=self.worktree, artifact_root=self.artifact,
                                      dispatch_state_root=self.state, agent_home=self.root / "install")
        request = load_request(prepare_task_request(self.route(self.task()), self.jobs), context=context)
        grants = [build_grant(request, runtime=runtime, default_writable_roots=(self.worktree,))
                  for runtime in ("codex-exec", "claude-cli", "opencode")]
        self.assertEqual({(g.writable_roots, g.read_roots) for g in grants},
                         {(request.writable_roots, request.read_roots)})
        self.assertEqual([g.read_enforcement for g in grants], ["os-sandbox", "tool-permission", "tool-permission"])

    def test_a_node_other_than_the_owner_only_reads(self):
        path = prepare_task_request(self.route(self.task()), self.jobs, node="frame")
        self.assertEqual(path.parent.name, "frame")
        request, binding = self.prepared(path)
        self.assertEqual(request["writable_roots"], [])
        self.assertIn(str(self.other / "out"), request["read_roots"])
        notes = {row["path"]: row.get("note") for row in binding["derivation"]["granted"]}
        self.assertEqual(notes[str(self.other / "out")], "node-reads-only")

    def test_a_manual_request_wins_over_derivation(self):
        manual = self.root / "manual.json"
        manual.write_text(json.dumps({"schema_version": 1, "writable_roots": [str(self.elsewhere)],
                                      "read_roots": [], "network": {"required": False}}), encoding="utf-8")
        inventory = self.home / ".config/hearting/compute-hosts.yaml"
        inventory.parent.mkdir(parents=True, exist_ok=True)
        inventory.write_text("schema_version: 1\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(manual),
                                          "COMPUTE_HOSTS_CONFIG": str(inventory)}):
            self.assertIsNone(prepare_task_request(self.route(self.task()), self.jobs))
            run_root = self.root / "runs"
            with mock.patch.object(EA, "_lab_run_root", return_value=run_root):
                path = prepare_task_request(self.route(self.task(), capability="autopilot-lab"), self.jobs)
        request, binding = self.prepared(path)
        self.assertEqual(request["writable_roots"], sorted([str(self.elsewhere), str(run_root)]))
        self.assertEqual(request["read_roots"], [str(inventory.parent)])
        self.assertNotIn("derivation", binding)

    def test_gpu_compute_defaults_upgrade_old_requests_and_reach_each_harness(self):
        inventory = self.home / '.config/hearting/compute-hosts.yaml'
        inventory.parent.mkdir(parents=True)
        run_root = self.root / 'compute-runs'
        inventory.write_text(f'schema_version: 1\nrun_root: {run_root}\nhosts:\n  fixture:\n    ssh_host: example.invalid\n')
        route = self.route(self.task(), capability='autopilot-lab')
        route['nodes'] = [{'id': 'full-run', 'kind': 'resource-runner'}]
        old = prepare_task_request(route, self.jobs)
        old_bytes = old.read_bytes()
        # This is the already prepared BC-style route, upgraded at normal start.
        prepared = prepare_task_request(route, self.jobs)
        request, binding = self.prepared(prepared)
        self.assertEqual(old, prepared)
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(binding['derivation'], self.prepared(old)[1]['derivation'])
        self.assertIn(str(inventory.parent), request['read_roots'])
        self.assertTrue(request['network']['required'])
        self.assertFalse(run_root.exists())
        self.assertEqual(prepared, prepare_task_request(route, self.jobs))
        context = AccessContext.build(worktree=self.worktree, artifact_root=self.artifact,
                                      dispatch_state_root=self.state, agent_home=self.root / 'install')
        loaded = load_request(prepared, context=context)
        for runtime, sandbox in [('opencode', 'adapter-default'), ('claude-cli', 'adapter-default'),
                                 ('codex-exec', 'danger-full-access')]:
            grant = build_grant(loaded, runtime=runtime, network_available=True,
                                effective_sandbox=sandbox, gpu_resource_scope=True)
            effective, digest = publish_effective_grant(
                jobs=self.jobs, attempt_id='att-' + runtime, route_id=route['route_id'],
                route_hash=route['route_hash'], runtime=runtime, sandbox=sandbox, grant=grant,
                default_writable_roots=(self.worktree, self.artifact), network_allowed=False)
            record = json.loads(effective.read_text())
            self.assertTrue(record['network_allowed'])
            self.assertEqual(record['network_enforcement'], 'none')
            self.jobs.write_text('now\topen\tfixture\tfixture\tparent\t'
                                 f'attempt_id=att-{runtime},route_id={route["route_id"]},'
                                 f'route_hash={route["route_hash"]},runtime_sandbox={sandbox},'
                                 f'execution_access_effective_file={effective},execution_access_effective_sha256={digest}\n')
            parent = load_parent_effective_grant(
                jobs=self.jobs, parent_attempt_id='att-' + runtime, context=context)
            assert_within_parent(loaded, parent, is_child=True)
        non_execution = EA.bind_request(str(prepared), environ={}, context=context, is_child=False,
                                     parent=None, runtime='opencode', compute_execution_scope=False)
        self.assertEqual(non_execution.network, 'not-requested')
        self.assertNotIn(inventory.parent, non_execution.read_roots)
        child = prepare_task_request(route, self.jobs, node='handoff')
        self.assertFalse(self.prepared(child)[0]['network']['required'])
        self.assertNotIn(str(inventory.parent), self.prepared(child)[0]['read_roots'])

    def test_gpu_defaults_keep_explicit_network_choice(self):
        inventory = self.home / '.config/hearting/compute-hosts.yaml'
        inventory.parent.mkdir(parents=True)
        inventory.write_text(f'schema_version: 1\nrun_root: {self.root}/runs\nhosts:\n  fixture:\n    ssh_host: local\n')
        manual = self.root / 'offline.json'
        manual.write_text(json.dumps({'schema_version': 1, 'writable_roots': [], 'read_roots': [],
                                      'network': {'required': False, 'reason': '', 'hosts': []}}))
        route = self.route('GPU run', capability='autopilot-lab')
        route['nodes'] = [{'id': 'full-run', 'kind': 'resource-runner'}]
        with mock.patch.dict(os.environ, {'AGENT_DISPATCH_EXECUTION_ACCESS_FILE': str(manual)}):
            prepared = prepare_task_request(route, self.jobs)
        self.assertFalse(self.prepared(prepared)[0]['network']['required'])

    def test_a_node_keeps_what_it_derived_at_its_first_preparation(self):
        (self.other / "out").rmdir()
        first = prepare_task_request(self.route(self.task()), self.jobs)
        before = first.read_bytes()
        (self.other / "out").mkdir()      # appears later: a resume does not widen the grant
        self.assertEqual(prepare_task_request(self.route(self.task()), self.jobs), first)
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(json.loads(before)["writable_roots"], [])

    def test_a_preparation_from_before_derivation_keeps_deriving_nothing(self):
        route = self.route(self.task())
        with mock.patch.object(EA, "_lab_run_root", return_value=self.root / "runs"), \
                mock.patch.object(EA, "derive_task_access", return_value=EA.DerivedAccess((), (), (), {})):
            old = prepare_task_request(dict(route, capability="autopilot-lab"), self.jobs)
        self.assertNotIn("derivation", self.prepared(old)[1])
        with mock.patch.object(EA, "_lab_run_root", return_value=self.root / "runs"):
            again = prepare_task_request(dict(route, capability="autopilot-lab"), self.jobs)
        self.assertEqual(again, old)
        self.assertEqual(self.prepared(again)[0]["read_roots"], [])

    def test_a_derived_root_the_validator_refuses_is_dropped_not_refused(self):
        broad = EA.DerivedAccess((Path("/"),), (), (("/", "Derived write root"),),
                                 {"granted": [{"path": "/", "access": "write", "line": 1, "source": "scope",
                                               "text": "범위: /"}], "skipped": []})
        with mock.patch.object(EA, "derive_task_access", return_value=broad):
            self.assertIsNone(prepare_task_request(self.route("범위: /\n"), self.jobs))
            with mock.patch.object(EA, "_lab_run_root", return_value=self.root / "runs"):
                path = prepare_task_request(self.route("범위: /\n", capability="autopilot-lab"), self.jobs)
        request, binding = self.prepared(path)
        self.assertEqual(request["writable_roots"], [str(self.root / "runs")])
        self.assertEqual(binding["derivation"]["granted"], [])
        self.assertTrue(binding["derivation"]["dropped"].startswith("execution-access-root-too-broad"))

    def test_a_qualifier_after_a_scope_path_belongs_to_that_path(self):
        # The start card's scope holds what is included and what is excluded (WORKFLOW §0.4);
        # a path is written only where its clause says so and nothing else.
        cases = {
            "범위: /x/out, /x/raw(제외)": {"/x/out": "read", "/x/raw": "excluded"},
            "범위: /x/out 쓰기, /x/raw (읽기 전용)": {"/x/out": "write", "/x/raw": "read"},
            "Scope: /x/out (write), /x/raw (read-only)": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out에 저장, /x/raw (손대지 않음)": {"/x/out": "write", "/x/raw": "excluded"},
            "범위: /x/out 저장, /x/raw, 읽기만": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 쓰기 (원본 /x/raw 제외)": {"/x/out": "write", "/x/raw": "excluded"},
            "범위: /x/out 수정, 원본 /x/raw 유지": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, /x/raw is off-limits": {"/x/out": "write", "/x/raw": "excluded"},
            "범위: /x/out 저장, /x/ro (write-protected)": {"/x/out": "write", "/x/ro": "excluded"},
            "범위: /x/out에 쓰지 마": {"/x/out": "excluded"},
            "범위: /x/a 읽고 결과 저장": {"/x/a": "read"},
            "범위: /x/db 기록조사": {"/x/db": "read"},
            "범위: /x/plain": {"/x/plain": "read"},
            "> 범위: /x/quoted 저장": {"/x/quoted": "read"},
            "범위: /data/outputs, /data/write_here": {"/data/outputs": "read", "/data/write_here": "read"},
            "범위: /data/input 저장": {"/data/input": "write"},
            "범위: 읽기만, /x/a 저장": {"/x/a": "read"},
            "범위: 다음은 제외, /x/a 저장, /x/b 저장": {"/x/a": "excluded", "/x/b": "excluded"},
            # A negated action is not a write (hearting-verify-cc PROBE3).
            "범위: /x/out 생성, /x/raw 수정 안 함": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 생성, /x/raw 변경 없음": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 저장, /x/raw 쓰기 불가": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 저장, /x/raw 삭제하면 안 됨": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 저장, /x/raw 수정 못 함": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 저장, /x/raw 수정하지 않음": {"/x/out": "write", "/x/raw": "excluded"},
            "Scope: /x/out (write), /x/raw (no writes)": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, don't modify /x/raw": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, /x/raw cannot be modified": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, /x/raw shouldn't be edited": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, avoid writing /x/raw": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, do not modify /x/raw": {"/x/out": "write", "/x/raw": "excluded"},
            # A write word about something else does not attach to a path.
            "범위: 결과 보고서 작성, /x/raw": {"/x/raw": "read"},
            "범위: /x/raw, 결과 보고서 작성": {"/x/raw": "read"},
            # Where a write word stands decides, not which words surround it (PROBE4).
            "범위: /x/out 저장, /x/raw 삭제하면 안 돼": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 저장, /x/raw 수정 안돼": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 생성, /x/raw 수정 불필요": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, nothing written to /x/raw": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, neither edit nor delete /x/raw": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, refrain from editing /x/raw": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, /x/raw must stay unmodified": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 저장, /x/raw 수정 X": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 저장, /x/raw 수정 대상 아님": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 저장, /x/raw 수정 말 것": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/raw (보고서 작성)": {"/x/raw": "read"},
            "범위: /x/raw (원본) 수정": {"/x/raw": "read"},
            "범위: /x/out (결과 위치, 덮어쓰기 가능)": {"/x/out": "read"},
            "범위: 수정 없음, /x/out 저장": {"/x/out": "read"},
            "범위: /x/raw (write), /x/out (no write)": {"/x/raw": "write", "/x/out": "read"},
            "범위: /x/out (덮어쓰기 가능)": {"/x/out": "write"},
            "범위: /x/out에 결과 저장한다": {"/x/out": "write"},
            "범위: /x/store 저장소 확인": {"/x/store": "read"},
            # An English write word after its path stands alone, and a question is no answer (PROBE5).
            "Scope: /x/out (write), /x/raw (writes disabled)": {"/x/out": "write", "/x/raw": "read"},
            "Scope: /x/out (write), /x/raw (edits blocked)": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write /x/out, /x/raw writes disabled": {"/x/out": "write", "/x/raw": "read"},
            "Scope: /x/out (write), /x/raw (write-locked)": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 저장, /x/raw 수정?": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 쓰기 가능, /x/raw": {"/x/out": "write", "/x/raw": "read"},
            "Scope: write results to /x/out, /x/raw": {"/x/out": "write", "/x/raw": "read"},
            "Scope: save outputs under /x/out, /x/raw": {"/x/out": "write", "/x/raw": "read"},
            "범위: /x/out 생성한다, /x/raw": {"/x/out": "write", "/x/raw": "read"},
            "Scope: /x/out write": {"/x/out": "write"},
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual({row[1]: row[2] for row in EA._task_paths(line)}, expected)

    def test_the_verifier_probe_never_widens_to_write(self):
        # ACCESS-DERIVATION-PROBE.py (hearting-verify-cc [28]), as a regression.
        X = self.root / "X"
        for name in ("out", "raw", "keep", "other", "secret_stuff"):
            (X / name).mkdir(parents=True)
        (X / "out" / "f.txt").write_text("x", encoding="utf-8")
        (self.home / "docs").mkdir()
        (X / "lnk_ssh").symlink_to(self.home / ".ssh")
        (X / "lnk_home").symlink_to(self.home)
        (X / "lnk_etc").symlink_to("/etc")
        context = AccessContext.build(worktree=self.worktree, artifact_root=self.artifact,
                                      dispatch_state_root=self.state, agent_home=self.root / "install",
                                      environ={"HOME": str(self.home)})
        cases = [
            ("범위: $X/out (write), $X/raw (read-only)", True, {"$X/out": "write", "$X/raw": "read"}, {}),
            ("범위: $X/out, $X/raw(제외)", True, {"$X/out": "read"}, {"$X/raw": "excluded-by-scope"}),
            ("범위: $X/out 쓰기, $X/raw (읽기 전용)", True, {"$X/out": "write", "$X/raw": "read"}, {}),
            ("범위: $X/out, $X/raw (손대지 않음)", True, {"$X/out": "read"}, {"$X/raw": "excluded-by-scope"}),
            ("범위: $X/out 쓰기 (원본 $X/raw 제외)", True, {"$X/out": "write"}, {"$X/raw": "excluded-by-scope"}),
            ("범위: $X/out, $X/raw, 읽기만", True, {"$X/out": "read", "$X/raw": "read"}, {}),
            ("범위: $X/out, $X/raw 원본 유지, $X/keep is off-limits", True,
             {"$X/out": "read", "$X/raw": "read"}, {"$X/keep": "excluded-by-scope"}),
            ("범위: $X/lnk_ssh 저장", True, {}, {"$X/lnk_ssh": "sensitive"}),
            ("범위: $X/lnk_home 저장", True, {}, {"$X/lnk_home": "too-broad"}),
            ("범위: $X/lnk_etc 저장", True, {}, {"$X/lnk_etc": "sensitive"}),
            ("범위: /, $H 저장", True, {}, {"$H": "too-broad"}),
            ("범위: $X/out/../../home/.ssh 저장", True, {}, {"$X/out/../../home/.ssh": "not-a-path"}),
            ("범위: $X/out 저장", False, {"$X/out": "read"}, {}),
            ("결과는 $X/out 에 쓰고 $X/raw 를 읽는다", True, {"$X/out": "read", "$X/raw": "read"}, {}),
            ("> 범위: $X/other 저장\n참고 문서 인용", True, {"$X/other": "read"}, {}),
            ("범위: $X/secret_stuff 저장", True, {}, {"$X/secret_stuff": "sensitive"}),
            ("범위: $X/out/f.txt 저장", True, {"$X/out": "read"}, {}),
            ("Scope: write $X/out; not $X/raw", True, {"$X/out": "write"}, {"$X/raw": "excluded-by-scope"}),
            ("범위: $H/docs 저장", True, {"$H/docs": "write"}, {}),
        ]
        def short(path):
            return path.replace(str(X), "$X").replace(str(self.home), "$H")
        for text, write, granted, skipped in cases:
            with self.subTest(text=text):
                derived = EA.derive_task_access(
                    {"work_request": {"text": text.replace("$X", str(X)).replace("$H", str(self.home))}},
                    context, write=write)
                self.assertEqual({short(r["path"]): r["access"] for r in derived.record["granted"]}, granted)
                self.assertEqual({short(r["path"]): r["reason"] for r in derived.record["skipped"]}, skipped)
                for row in derived.record["granted"]:
                    self.assertTrue(row["why"])
        rows = EA.derive_task_access({"work_request": {"text": f"범위: {X}/out에 결과 저장"}}, context)
        self.assertIn("writes:저장", rows.justification[0][1])
    def handed(self, route, roots):
        import route_authority as RA
        given = self.root / "given.json"
        given.write_text(json.dumps({"schema_version": 1, "writable_roots": [str(r) for r in roots],
                                     "read_roots": [], "network": {"required": False}}), encoding="utf-8")
        context = AccessContext.build(worktree=self.worktree, artifact_root=self.artifact,
                                      dispatch_state_root=self.state, agent_home=Path(EA.__file__).resolve().parents[1])
        request = load_request(given, context=context)
        RA.record_access_change(route, request=EA.normalized_request(request), request_sha256=request.request_sha256,
                                by={"harness": "opencode", "session_id": "oc-sid"}, source="unattributed")
        return request

    def test_the_request_the_parent_handed_over_is_the_explicit_one_for_the_next_owner(self):
        route = self.route(self.task())
        first = prepare_task_request(route, self.jobs)          # derived at the first owner launch
        import route_authority as RA
        (derived,) = RA.access_changes(route)
        self.assertEqual(derived["source"], "derived")
        self.assertEqual(derived["request_sha256"], load_request(first, context=AccessContext.build(
            worktree=self.worktree, artifact_root=self.artifact, dispatch_state_root=self.state,
            agent_home=Path(EA.__file__).resolve().parents[1])).request_sha256)
        request = self.handed(route, [self.elsewhere])
        given = prepare_task_request(route, self.jobs)
        self.assertNotEqual(given, first)
        self.assertEqual(json.loads(given.read_text(encoding="utf-8"))["writable_roots"], [str(self.elsewhere)])
        self.assertEqual(prepare_task_request(route, self.jobs, node="frame"), given)
        # A lab owner still adds its run storage, in a file of that request's own.
        run_root = self.root / "runs"
        lab = self.route(self.task(), capability="autopilot-lab")
        self.handed(lab, [self.elsewhere])
        with mock.patch.object(EA, "_lab_run_root", return_value=run_root):
            merged = prepare_task_request(lab, self.jobs)
        self.assertEqual(merged.parent.name, "compute")
        self.assertEqual(json.loads(merged.read_text(encoding="utf-8"))["writable_roots"],
                         sorted([str(self.elsewhere), str(run_root)]))
        # An explicit file in the caller's environment still wins, unless the caller asks for the route's own.
        manual = self.root / "manual.json"
        manual.write_text(json.dumps({"schema_version": 1, "writable_roots": [], "read_roots": [],
                                      "network": {"required": False}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(manual)}):
            self.assertIsNone(prepare_task_request(route, self.jobs))
            self.assertEqual(prepare_task_request(route, self.jobs, environment=False), given)
        self.assertEqual(request.writable_roots, (self.elsewhere,))

    def test_a_frame_s_derivation_adds_no_access_row(self):
        import route_authority as RA
        route = self.route(self.task())
        prepare_task_request(route, self.jobs, node="frame")
        self.assertEqual(RA.access_changes(route), [])

    def test_the_task_text_reader(self):
        rows = EA._task_paths(
            "**범위:** /a/out에 저장, /a/ref 참조, /a/raw 제외, /a/after\n"
            "- Scope: /b/x (read-only /b/y)\n"
            "scope: {writes: [\"/c/w\"], reads: [\"/c/r\"]}\n"
            "see https://example.com/d/e and 경로:/f/g. and /h/보고서_v2초안\n"
            "범위 변경: /i/j\n")
        found = {(path, access, source) for _, path, access, source, _, _ in rows}
        self.assertEqual(found, {
            ("/a/out", "write", "scope"), ("/a/ref", "read", "scope"), ("/a/raw", "excluded", "scope"),
            ("/a/after", "excluded", "scope"), ("/b/x", "read", "scope"), ("/b/y", "read", "scope"),
            ("/c/w", "write", "scope"), ("/c/r", "read", "scope"), ("/f/g", "read", "task"),
            ("/h/보고서_v2초안", "read", "task"), ("/i/j", "read", "task")})


    def inventory(self, roots):
        path = self.home / ".local/share/reader/app-v1/ops/roots.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"roots": [{"artifact_root_path": str(p)} for p in roots],
                                    "writable_roots": [str(self.elsewhere)]}), encoding="utf-8")
        return path

    def test_audit_inventory_reaches_every_root_without_writes(self):
        roots = [self.other / f"project-{i}" / ".agent_reports" for i in range(20)]
        for root in roots:
            root.mkdir(parents=True)
        missing = self.other / "missing" / ".agent_reports"
        inventory = self.inventory([*roots, missing, self.home / ".ssh"])
        route = self.route(f"입력: {inventory} 의 roots 전수 읽기", capability="audit")
        path = prepare_task_request(route, self.jobs)
        request, binding = self.prepared(path)
        self.assertEqual(request["writable_roots"], [])
        self.assertEqual(set(request["read_roots"]), {str(p) for p in (*roots, inventory.parent)})
        self.assertEqual({r["reason"] for r in binding["derivation"]["skipped"]}, {"missing", "sensitive"})
        context = AccessContext.build(worktree=self.worktree, artifact_root=self.artifact,
                                      dispatch_state_root=self.state, agent_home=self.root / "install")
        loaded = load_request(path, context=context)
        for runtime in ("codex-exec", "claude-cli", "opencode"):
            grant = build_grant(loaded, runtime=runtime, default_writable_roots=(self.worktree,))
            self.assertEqual(grant.unwritable_read_roots, loaded.read_roots)
            self.assertEqual(grant.additional_writable_roots, ())

    def test_inventory_exclusions_and_indirect_credentials_stay_out(self):
        inventory = self.inventory([self.other, self.elsewhere])
        excluded = self.route(f"Scope: {inventory} excluded\n입력: {inventory}")
        self.assertIsNone(prepare_task_request(excluded, self.jobs))
        link = self.other / "key-dir"
        link.symlink_to(self.home / ".ssh", target_is_directory=True)
        inventory = self.inventory([link, self.elsewhere])
        request = self.prepared(prepare_task_request(self.route(f"입력: {inventory}"), self.jobs))[0]
        self.assertNotIn(str(link), request["read_roots"])
        self.assertNotIn(str(self.home / ".ssh"), request["read_roots"])
        self.assertNotIn(str(self.elsewhere), request["writable_roots"])

    def test_upgrade_adds_only_reads_and_keeps_old_request_and_later_snapshot(self):
        inventory = self.inventory([self.elsewhere])
        later_write = self.other / "future-output"
        route = self.route(f"Scope: {self.other}/out write, {later_write} write\n입력: {inventory}")
        with mock.patch.object(EA, "_root_inventory", return_value=()):
            old = prepare_task_request(route, self.jobs)
        binding = old.with_name("binding.json")
        record = json.loads(binding.read_text())
        record.pop("read_inputs_version")
        binding.write_text(json.dumps(record))
        before = old.read_bytes()
        later_write.mkdir()
        prepared = prepare_task_request(route, self.jobs)
        self.assertNotEqual(prepared, old)
        self.assertEqual(old.read_bytes(), before)
        request = self.prepared(prepared)[0]
        self.assertEqual(request["writable_roots"], [str(self.other / "out")])
        self.assertIn(str(self.elsewhere), request["read_roots"])
        self.assertNotIn(str(later_write), request["writable_roots"])
        self.inventory([self.other / "data"])
        self.assertEqual(prepare_task_request(route, self.jobs), prepared)
        self.assertEqual(self.prepared(prepared)[0], request)

    def test_large_read_inventory_is_loadable_and_reused_after_preparation(self):
        roots = [self.other / (f"project-{i:03d}-" + "x" * 100) / ".agent_reports"
                 for i in range(256)]
        for root in roots:
            root.mkdir(parents=True)
        inventory = self.worktree / "roots.json"
        inventory.write_text(json.dumps({"roots": [str(root) for root in roots]}))
        self.assertLess(inventory.stat().st_size, EA.MAX_ROOT_INVENTORY_BYTES)
        route = self.route(f"입력: {inventory}", capability="audit")
        prepared = prepare_task_request(route, self.jobs)
        self.assertGreater(prepared.stat().st_size, 64 * 1024)
        self.assertGreater(prepared.with_name("binding.json").stat().st_size, 64 * 1024)
        context = AccessContext.build(worktree=self.worktree, artifact_root=self.artifact,
                                      dispatch_state_root=self.state, agent_home=self.root / "install")
        loaded = load_request(prepared, context=context)
        self.assertEqual(set(loaded.read_roots), set(roots))
        self.assertEqual(loaded.writable_roots, ())
        inventory.write_text('{"roots": []}')
        self.assertEqual(prepare_task_request(route, self.jobs), prepared)
        self.assertEqual(load_request(prepared, context=context), loaded)

    def test_read_upgrade_keeps_existing_writes_and_reads_at_the_read_limit(self):
        inventory = self.inventory([])
        route = self.route(f"Scope: {self.other}/out write, {self.other}/ref read\n입력: {inventory}")
        with mock.patch.object(EA, "_root_inventory", return_value=()):
            old = prepare_task_request(route, self.jobs)
        old_request = self.prepared(old)[0]
        binding = old.with_name("binding.json")
        record = json.loads(binding.read_text())
        record.pop("read_inputs_version")
        binding.write_text(json.dumps(record))
        old_bytes = old.read_bytes()
        roots = [self.elsewhere / f"p{i:03d}" for i in range(256)]
        for root in roots:
            root.mkdir()
        self.inventory(roots)
        upgraded = prepare_task_request(route, self.jobs)
        request, record = self.prepared(upgraded)
        self.assertEqual(request["writable_roots"], old_request["writable_roots"])
        self.assertTrue(set(old_request["read_roots"]).issubset(request["read_roots"]))
        self.assertEqual(len(request["read_roots"]), EA.MAX_READ_ROOTS)
        self.assertIn("root-limit", {row["reason"] for row in record["derivation"]["skipped"]})
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(prepare_task_request(route, self.jobs), upgraded)

    def test_source_contracts_read_without_runtime_state_or_write_derivation(self):
        install = self.home / ".local/share/hearting/releases/v-test"
        for name in ("utilities", "core", ".dispatch"):
            (install / name).mkdir(parents=True)
            (install / name / "source.py").write_text("pass\n")
        context = AccessContext.build(worktree=self.worktree, artifact_root=self.artifact,
                                      dispatch_state_root=self.state, agent_home=install)
        route = self.route(f"입력: {install}/utilities/source.py {install}/core/source.py {install}/.dispatch/source.py")
        reads = EA.derive_task_access(route, context)
        self.assertEqual(set(reads.read_roots), {install / "utilities", install / "core"})
        self.assertEqual(reads.writable_roots, ())
        writes = EA.derive_task_access(self.route(f"Scope: {install}/utilities write"), context)
        self.assertEqual(writes.writable_roots, ())


if __name__ == "__main__":
    unittest.main()
