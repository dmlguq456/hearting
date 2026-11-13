#!/usr/bin/env python3
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import errno
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest import mock

import execution_access_diagnose as diagnose_module


class ExecutionAccessDiagnoseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "home" / ".codex" / ".harness"
        self.private.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def classification(self, evidence: dict[str, object]) -> str:
        return str(
            diagnose_module.diagnose(
                evidence, launcher_state_roots=(self.private,)
            )["classification"]
        )

    def test_seven_classifications(self) -> None:
        fixtures = {
            "launcher-runtime-state-unwritable": {
                "errno": "EROFS",
                "phase": "write",
                "path": str(self.private / "codex-launcher.lock"),
            },
            "mount-read-only": {
                "errno": "EROFS",
                "phase": "write",
                "path": str(self.root / "mount" / "file"),
            },
            "unix-permission-denied": {
                "errno": "EACCES",
                "phase": "write",
                "mount_writable": True,
            },
            "remote-auth-rejected": {
                "http_status": 403,
                "phase": "connect",
            },
            "dns-resolution-failed": {"errno": "EAI_NONAME", "phase": "dns"},
            "tcp-connect-blocked": {"errno": "ETIMEDOUT", "phase": "connect"},
            "sandbox-network-disabled": {
                "network_all_hosts_failed": True,
                "sandbox_network_enabled": False,
                "phase": "network",
            },
        }
        for expected, evidence in fixtures.items():
            with self.subTest(expected=expected):
                result = diagnose_module.diagnose(
                    evidence, launcher_state_roots=(self.private,)
                )
                self.assertEqual(expected, result["classification"])
                self.assertTrue(result["one_change"])
                self.assertNotIn("stderr", json.dumps(result).lower())

    def test_launcher_state_wins_over_erofs(self) -> None:
        self.assertEqual(
            "launcher-runtime-state-unwritable",
            self.classification(
                {
                    "errno": "EROFS",
                    "phase": "write",
                    "path": str(self.private / "launcher.lock"),
                }
            ),
        )

    def test_launcher_state_path_without_write_failure_is_insufficient(self) -> None:
        with self.assertRaises(diagnose_module.DiagnosisError):
            diagnose_module.diagnose(
                {
                    "phase": "read",
                    "path": str(self.private / "launcher.lock"),
                    "exit_code": 0,
                },
                launcher_state_roots=(self.private,),
            )

    def test_request_absence_alone_does_not_mean_network_off(self) -> None:
        with self.assertRaises(diagnose_module.DiagnosisError):
            diagnose_module.diagnose(
                {
                    "network_all_hosts_failed": False,
                    "sandbox_network_enabled": None,
                    "execution_access_network": "not-requested",
                }
            )

    def test_malformed_evidence_is_typed_and_never_echoed(self) -> None:
        malformed = {
            "http_status": ["Bearer secret-token"],
            "errno": "EROFS",
            "phase": "write",
        }
        with self.assertRaises(diagnose_module.DiagnosisError) as raised:
            diagnose_module.diagnose(malformed)
        self.assertEqual("diagnosis-evidence-insufficient", raised.exception.reason)

        evidence = self.root / "malformed-evidence.json"
        evidence.write_text(json.dumps(malformed), encoding="utf-8")
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = diagnose_module.main(["--evidence-file", str(evidence)])
        self.assertEqual(65, rc)
        self.assertEqual(
            {"reason": "diagnosis-evidence-insufficient"},
            json.loads(stderr.getvalue()),
        )
        self.assertNotIn("secret-token", stderr.getvalue())

        for field, value in (
            ("errno", {"credential": "password=hunter2"}),
            ("phase", ["connect"]),
            ("path", ["/tmp/value"]),
            ("mount_writable", "yes"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(diagnose_module.DiagnosisError):
                    diagnose_module.diagnose({field: value})

    def test_default_cli_creates_no_file_or_socket_and_redacts_raw_error(self) -> None:
        evidence = self.root / "evidence.json"
        evidence.write_text(
            json.dumps(
                {
                    "http_status": 401,
                    "phase": "connect",
                    "stderr_excerpt": "Authorization: Bearer secret-token password=hunter2",
                }
            ),
            encoding="utf-8",
        )
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        stdout = io.StringIO()
        with mock.patch("os.open", side_effect=AssertionError("write probe called")), mock.patch(
            "socket.getaddrinfo", side_effect=AssertionError("network probe called")
        ), mock.patch("socket.socket", side_effect=AssertionError("network probe called")), mock.patch(
            "sys.stdout", stdout
        ):
            rc = diagnose_module.main(["--evidence-file", str(evidence)])
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.assertEqual(0, rc)
        self.assertEqual(before, after)
        rendered = stdout.getvalue()
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("hunter2", rendered)
        self.assertEqual("remote-auth-rejected", json.loads(rendered)["classification"])

    def request_file(self, writable: Path, host: str = "example.com:443") -> Path:
        request = self.root / "request.json"
        request.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "writable_roots": [str(writable)],
                    "read_roots": [],
                    "network": {"required": True, "reason": "probe", "hosts": [host]},
                    "enforcement_required": "any",
                    "justification": {},
                }
            ),
            encoding="utf-8",
        )
        return request

    def context_args(self) -> list[str]:
        worktree = self.root / "worktree"
        artifact = self.root / "artifact"
        state = self.root / "state" / "dispatch"
        agent_home = self.root / "install" / "hearting"
        for path in (worktree, artifact, state, agent_home):
            path.mkdir(parents=True, exist_ok=True)
        return [
            "--worktree",
            str(worktree),
            "--artifact-root",
            str(artifact),
            "--dispatch-state-root",
            str(state),
            "--agent-home",
            str(agent_home),
        ]

    def test_probe_requires_opt_in_and_exact_declared_target(self) -> None:
        writable = self.root / "scoped" / "write"
        writable.mkdir(parents=True)
        request = self.request_file(writable)
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = diagnose_module.main(
                ["--request-file", str(request), "--probe-write", str(writable), *self.context_args()]
            )
        self.assertEqual(64, rc)
        self.assertIn("probe-opt-in-required", stderr.getvalue())

        outside = self.root / "outside"
        outside.mkdir()
        with mock.patch("sys.stderr", io.StringIO()):
            rc = diagnose_module.main(
                [
                    "--request-file",
                    str(request),
                    "--allow-probe",
                    "--probe-write",
                    str(outside),
                    *self.context_args(),
                ]
            )
        self.assertEqual(64, rc)
        self.assertEqual([], list(outside.iterdir()))

    def test_write_probe_cleans_only_its_owned_file(self) -> None:
        writable = self.root / "scoped" / "write"
        writable.mkdir(parents=True)
        keep = writable / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        request = self.request_file(writable)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = diagnose_module.main(
                [
                    "--request-file",
                    str(request),
                    "--allow-probe",
                    "--probe-write",
                    str(writable),
                    *self.context_args(),
                ]
            )
        self.assertEqual(0, rc)
        self.assertEqual("keep", keep.read_text(encoding="utf-8"))
        self.assertEqual([keep], list(writable.iterdir()))
        self.assertEqual("probe-succeeded", json.loads(stdout.getvalue())["classification"])

        descendant = writable / "child"
        descendant.mkdir()
        with mock.patch("sys.stdout", io.StringIO()):
            rc = diagnose_module.main(
                [
                    "--request-file",
                    str(request),
                    "--allow-probe",
                    "--probe-write",
                    str(descendant),
                    *self.context_args(),
                ]
            )
        self.assertEqual(0, rc)
        self.assertEqual([], list(descendant.iterdir()))

    def test_write_probe_uses_statvfs_or_unknown_after_failure(self) -> None:
        writable = self.root / "scoped" / "write"
        writable.mkdir(parents=True)
        statvfs = os.statvfs(writable)
        writable_stat = mock.Mock(f_flag=statvfs.f_flag & ~getattr(os, "ST_RDONLY", 1))
        with mock.patch(
            "execution_access_diagnose.os.open",
            side_effect=PermissionError(errno.EACCES, "secret credential"),
        ), mock.patch("execution_access_diagnose.os.statvfs", return_value=writable_stat):
            result = diagnose_module._probe_write(writable)
        self.assertEqual("failed", result["result"])
        self.assertIs(True, result["mount_writable"])
        self.assertNotIn("secret", json.dumps(result))

        with mock.patch(
            "execution_access_diagnose.os.open",
            side_effect=PermissionError(errno.EACCES, "secret credential"),
        ), mock.patch(
            "execution_access_diagnose.os.statvfs",
            side_effect=OSError(errno.EIO, "unavailable"),
        ):
            result = diagnose_module._probe_write(writable)
        self.assertIsNone(result["mount_writable"])

    def test_write_probe_cleanup_failure_reports_exact_residual(self) -> None:
        writable = self.root / "scoped" / "write"
        writable.mkdir(parents=True)
        with mock.patch.object(
            Path,
            "unlink",
            side_effect=PermissionError(errno.EACCES, "cleanup blocked"),
        ):
            result = diagnose_module._probe_write(writable)
        self.assertEqual("cleanup-failed", result["result"])
        residual = Path(str(result["residual_path"]))
        self.assertEqual(writable, residual.parent)
        self.assertTrue(residual.exists())
        residual.unlink()

        request = self.request_file(writable)
        residual = writable / ".execution-access-probe-fixed"
        stderr = io.StringIO()
        with mock.patch(
            "execution_access_diagnose._probe_write",
            return_value={
                "kind": "write",
                "target": str(writable),
                "result": "cleanup-failed",
                "errno": "EACCES",
                "residual_path": str(residual),
            },
        ), mock.patch("sys.stderr", stderr):
            rc = diagnose_module.main(
                [
                    "--request-file",
                    str(request),
                    "--allow-probe",
                    "--probe-write",
                    str(writable),
                    *self.context_args(),
                ]
            )
        self.assertEqual(64, rc)
        rendered = json.loads(stderr.getvalue())
        self.assertEqual("probe-cleanup-failed", rendered["reason"])
        self.assertEqual(str(residual), rendered["residual_path"])

    def test_connect_probe_is_bounded_and_exact(self) -> None:
        writable = self.root / "scoped" / "write"
        writable.mkdir(parents=True)
        request = self.request_file(writable)
        fake_socket = mock.MagicMock()
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.1", 443))
        ]
        with mock.patch("socket.getaddrinfo", return_value=addresses), mock.patch(
            "socket.socket", return_value=fake_socket
        ), mock.patch("sys.stdout", io.StringIO()):
            rc = diagnose_module.main(
                [
                    "--request-file",
                    str(request),
                    "--allow-probe",
                    "--probe-connect",
                    "example.com:443",
                    "--probe-timeout",
                    "2",
                    *self.context_args(),
                ]
            )
        self.assertEqual(0, rc)

    def test_connect_probe_deadline_and_dns_code_are_process_bounded(self) -> None:
        def slow_resolver(*_args: object, **_kwargs: object):
            time.sleep(1.0)
            return []

        started = time.monotonic()
        with mock.patch("socket.getaddrinfo", side_effect=slow_resolver):
            result = diagnose_module._probe_connect("example.com:443", 0.1)
        elapsed = time.monotonic() - started
        self.assertEqual("failed", result["result"])
        self.assertEqual("ETIMEDOUT", result["errno"])
        self.assertLess(elapsed, 0.75)

        with mock.patch(
            "socket.getaddrinfo",
            side_effect=socket.gaierror(socket.EAI_NONAME, "secret resolver detail"),
        ):
            result = diagnose_module._probe_connect("example.com:443", 1.0)
        self.assertEqual("failed", result["result"])
        self.assertEqual("EAI_NONAME", result["errno"])
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
