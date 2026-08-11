#!/usr/bin/env python3
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_activation as activation  # noqa: E402
import installer  # noqa: E402


class RuntimeSnapshotTest(unittest.TestCase):
    def test_runtime_activate_defaults_to_packaged_snapshot(self):
        args = installer.build_parser().parse_args(
            ["runtime", "activate", "--runtime", "codex"]
        )
        self.assertEqual(args.mode, "packaged")

    def test_snapshot_and_restore_preserve_live_managed_session_sockets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".harness"
            managed = state / "managed-sessions" / "live"
            managed.mkdir(parents=True)
            current = state / "activation.json"
            current.write_text("before\n", encoding="utf-8")
            socket_path = managed / "app-server.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            try:
                record = activation._copy_snapshot(
                    state, root / "backup", 0,
                    preserve_names=("managed-sessions",),
                )
                self.assertFalse(
                    (Path(record["backup"]) / "managed-sessions").exists())
                current.write_text("after\n", encoding="utf-8")
                (state / "new-projection").write_text("remove me\n", encoding="utf-8")
                activation._restore([record])
                self.assertEqual(current.read_text(encoding="utf-8"), "before\n")
                self.assertFalse((state / "new-projection").exists())
                self.assertTrue(socket_path.is_socket())
            finally:
                listener.close()

    def test_preserved_runtime_directory_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".harness"
            state.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (state / "managed-sessions").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(activation.ActivationError):
                activation._copy_snapshot(
                    state, root / "backup", 0,
                    preserve_names=("managed-sessions",),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
