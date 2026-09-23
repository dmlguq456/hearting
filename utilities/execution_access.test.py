#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
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
    receipt_fields,
    request_path,
)


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
            },
        )()
        self.assertEqual(
            (self.artifact, self.worktree), adapter_default_roots(args)
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


if __name__ == "__main__":
    unittest.main()
