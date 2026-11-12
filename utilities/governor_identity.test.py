import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import governor_identity as G


class IdentityWitnessTest(unittest.TestCase):
    def test_capture_has_stable_process_coordinates(self):
        identity = G.capture_local_identity()
        self.assertEqual(identity["pid"], os.getpid())
        self.assertTrue(identity["starttime"])
        from resource_run_registry import boot_identity
        self.assertEqual(identity['boot_host'], boot_identity()['boot_host'])

    def test_held_witness_is_live_and_close_removes_exact_file(self):
        with tempfile.TemporaryDirectory() as td:
            handle = G.create_witness(td, "reservation")
            self.assertEqual(G.observe_witness(td, handle.binding()).state, "live")
            path = handle.path
            G.close_witness(handle)
            self.assertFalse(path.exists())

    def test_unlocked_witness_is_unknown_not_dead(self):
        with tempfile.TemporaryDirectory() as td:
            handle = G.create_witness(td, "reservation")
            binding = handle.binding()
            os.close(handle.fd)
            handle._closed = True
            self.assertEqual(G.observe_witness(td, binding).state, "unknown")
            (Path(td) / binding["relative_path"]).unlink(missing_ok=True)

    def test_symlink_and_replacement_are_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            handle = G.create_witness(td, "reservation")
            binding = handle.binding()
            alias = Path(td) / "identity-witnesses" / "alias.lock"
            alias.symlink_to(handle.path.name)
            alias_binding = {**binding, "relative_path": "identity-witnesses/alias.lock"}
            self.assertEqual(G.observe_witness(td, alias_binding).state, "unknown")
            path = handle.path
            G.close_witness(handle)
            path.write_bytes(b"replacement")
            self.assertEqual(G.observe_witness(td, binding).state, "unknown")
            path.unlink()
            alias.unlink()

    def test_payload_is_checked_beyond_digest(self):
        with tempfile.TemporaryDirectory() as td:
            handle = G.create_witness(td, "reservation")
            binding = handle.binding()
            payload = b'{"identity":{},"nonce":"wrong","phase":"reservation","schema":1}\n'
            os.pwrite(handle.fd, payload, 0)
            os.ftruncate(handle.fd, len(payload))
            forged = {**binding, "payload_sha256": G._digest(payload)}
            self.assertEqual(G.observe_witness(td, forged).reason, "witness-payload-mismatch")
            G.close_witness(handle)

    def test_create_witness_handles_partial_write(self):
        real_write = os.write
        calls = []

        def partial(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return real_write(fd, data[:1])
            return real_write(fd, data)

        with tempfile.TemporaryDirectory() as td, mock.patch.object(G.os, "write", side_effect=partial):
            handle = G.create_witness(td, "reservation")
            self.assertGreater(len(calls), 1)
            self.assertEqual(G.observe_witness(td, handle.binding()).state, "live")
            G.close_witness(handle)


class IdentityBoundaryRegressionTest(unittest.TestCase):
    def test_missing_namespace_coordinates_and_capture_race_refuse(self):
        with mock.patch.object(G.os, "stat", side_effect=PermissionError("fixture")):
            with self.assertRaises(G.IdentityCaptureError): G.capture_local_identity()
        original = G.Path.read_text
        def missing(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            return "\n".join(x for x in text.splitlines() if not x.startswith("NSpid:")) if str(path)=="/proc/self/status" else text
        with mock.patch.object(G.Path, "read_text", missing):
            with self.assertRaises(G.IdentityCaptureError): G.capture_local_identity()
        with mock.patch.object(G, "_starttime", side_effect=["1", "2"]):
            with self.assertRaises(G.IdentityCaptureError): G.capture_local_identity()

    def test_replaced_during_observation_and_fifo_are_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            h=G.create_witness(td,"reservation"); binding=h.binding(); real=G.fcntl.flock
            def replaced(fd, op):
                if op == G.fcntl.LOCK_SH | G.fcntl.LOCK_NB:
                    h.path.unlink(); h.path.write_text("successor")
                return real(fd,op)
            with mock.patch.object(G.fcntl,"flock",side_effect=replaced):
                self.assertEqual(G.observe_witness(td,binding).state,"unknown")
            G.close_witness(h);self.assertEqual(h.path.read_text(),"successor")
            h.path.unlink(); os.mkfifo(h.path)
            self.assertEqual(G.observe_witness(td,binding).state,"unknown")

    def test_fd_loss_reuse_does_not_close_successor(self):
        with tempfile.TemporaryDirectory() as td:
            h=G.create_witness(td,"claimant");fd=h.fd;os.close(fd)
            successor=os.open(Path(td)/"successor",os.O_CREAT|os.O_RDWR,0o600)
            self.assertEqual(successor,fd)
            self.assertFalse(G.retained_witness_is_held(h))
            G.close_witness(h);os.fstat(successor);os.close(successor)
            self.assertTrue(h.path.exists())

    def test_payload_modified_in_place_is_not_deleted_on_close(self):
        with tempfile.TemporaryDirectory() as td:
            h=G.create_witness(td,"claimant");h.path.write_text("changed")
            G.close_witness(h);self.assertEqual(h.path.read_text(),"changed")


if __name__ == "__main__":
    unittest.main()
