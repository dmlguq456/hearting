from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pass_inputs


class PassInputsTests(unittest.TestCase):
    def setUp(self):
        pass_inputs.install()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.json"
        self.source.write_text("{}")
        self.listed = self.root / "listed"
        self.listed.mkdir()

    def _later(self):
        # A distinct mtime even on coarse-timestamp filesystems.
        time.sleep(0.01)

    def _pass(self):
        with pass_inputs.record() as inputs:
            self.source.read_text()
            os.listdir(self.listed)
            (self.root / "missing.json").exists()
        return inputs

    def test_unchanged_inputs_are_not_a_change(self):
        inputs = self._pass()
        self.assertTrue(inputs.complete)
        self.assertIn(str(self.source), inputs.paths)
        self.assertFalse(inputs.changed())

    def test_rewritten_read_file_is_a_change(self):
        inputs = self._pass()
        self._later()
        self.source.write_text('{"a": 1}')
        self.assertTrue(inputs.changed())

    def test_entry_added_to_listed_directory_is_a_change(self):
        inputs = self._pass()
        self._later()
        (self.listed / "new").write_text("x")
        self.assertTrue(inputs.changed())

    def test_stat_of_missing_path_notices_its_creation(self):
        inputs = self._pass()
        (self.root / "missing.json").write_text("{}")
        self.assertTrue(inputs.changed())

    def test_pass_and_caller_own_writes_are_not_a_later_change(self):
        output = self.root / "projection.json"
        with pass_inputs.record() as inputs:
            output.exists()
            os.listdir(self.root)
            temp = self.root / ".projection.tmp"
            temp.write_text("1")
            os.replace(temp, output)
        self.assertFalse(inputs.changed())
        self._later()
        with pass_inputs.own_writes(inputs):
            temp = self.root / ".projection.tmp"
            temp.write_text("2")
            os.replace(temp, output)
        self.assertFalse(inputs.changed())
        self._later()
        output.write_text("3")   # someone else's write still counts
        self.assertTrue(inputs.changed())

    def test_a_foreign_change_before_an_own_write_is_not_adopted(self):
        output = self.root / "projection.json"
        with pass_inputs.record() as inputs:
            os.listdir(self.root)
            self._later()
            subprocess.run([sys.executable, "-c",  # another process, mid-pass
                            "import sys; open(sys.argv[1], 'w').write(sys.argv[2])",
                            str(self.root / "foreign.json"), "{}"], check=True)
            self._later()
            temp = self.root / ".projection.tmp"
            temp.write_text("1")
            os.replace(temp, output)
        self.assertTrue(inputs.changed(), "the listing must not absorb a foreign entry")

    def test_an_own_temporary_file_written_through_its_descriptor_is_not_a_change(self):
        output = self.root / "projection.json"
        with pass_inputs.record() as inputs:
            os.listdir(self.root)
            fd, temp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
            os.lstat(temp)                 # e.g. a symlink guard on the staged file
            os.write(fd, b"1")
            os.close(fd)
            os.replace(temp, output)
        self.assertTrue(inputs.complete)
        self.assertFalse(inputs.changed())

    def test_a_foreign_rewrite_before_an_own_rewrite_of_a_read_file_is_not_adopted(self):
        with pass_inputs.record() as inputs:
            self.source.read_text()
            self.source.write_text('{"own": 1}')      # its own rewrite is adopted
        self.assertFalse(inputs.changed())
        with pass_inputs.record() as inputs:
            self.source.read_text()
            self._later()
            subprocess.run([sys.executable, "-c",   # another process, mid-pass
                            "import sys; open(sys.argv[1], 'w').write(sys.argv[2])",
                            str(self.source), '{"x": 2}'], check=True)
            self.source.write_text('{"own": 3}')
        self.assertTrue(inputs.changed(), "the foreign rewrite must not be absorbed")

    def test_dir_fd_and_relative_reads_are_recorded(self):
        listed_file = self.listed / "entry.json"
        listed_file.write_text("{}")
        directory = os.open(self.listed, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, directory)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.root)
        with pass_inputs.record() as inputs:
            os.stat("entry.json", dir_fd=directory)
            os.close(os.open("entry.json", os.O_RDONLY, dir_fd=directory))
            os.listdir(directory)
            Path("source.json").read_text()
        self.assertTrue(inputs.complete)
        self.assertIn(str(listed_file), inputs.paths)
        self.assertIn(str(self.source), inputs.paths)
        self.assertFalse(inputs.changed())
        self._later()
        listed_file.write_text('{"changed": 1}')
        self.assertTrue(inputs.changed())

    def test_directory_only_stat_by_a_path_walk_ignores_its_entries(self):
        with pass_inputs.record() as inputs:
            os.path.realpath(self.listed / "x")   # lstat of every ancestor directory
            self.listed.is_dir()
        self._later()
        (self.listed / "unrelated").write_text("x")
        self.assertFalse(inputs.changed())
        self.listed.rename(self.root / "moved")
        self.assertTrue(inputs.changed())

    def test_caller_generation_replaces_a_meaningless_rewrite(self):
        def describe(path):
            if path == str(self.source):
                return ("value", self.source.read_text().split("|", 1)[0])
            return None
        self.source.write_text("a|clock=1")
        with pass_inputs.record(describe=describe) as inputs:
            self.source.read_text()
        self.source.write_text("a|clock=2")
        self.assertFalse(inputs.changed())
        self.source.write_text("b|clock=2")
        self.assertTrue(inputs.changed())

    def test_ignored_paths_are_not_inputs(self):
        marker = self.root / ".demand"
        marker.write_text("{}")
        with pass_inputs.record(ignore=(marker,)) as inputs:
            marker.read_text()
        self._later()
        marker.write_text('{"x": 1}')
        self.assertFalse(inputs.changed())

    def test_probed_process_exit_is_a_change(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.kill)
        with pass_inputs.record() as inputs:
            os.kill(child.pid, 0)
        self.assertFalse(inputs.changed())
        child.kill()
        child.wait()
        self.assertTrue(inputs.changed())

    def test_recording_without_installed_wrappers_never_claims_unchanged(self):
        with mock.patch.object(pass_inputs, "_installed", False):
            with pass_inputs.record() as inputs:
                self.source.read_text()
        self.assertFalse(inputs.complete)
        self.assertTrue(inputs.changed())

    def test_wrappers_are_transparent_outside_a_pass(self):
        self.assertEqual(self.source.read_text(), "{}")
        self.assertEqual(sorted(os.listdir(self.root)), ["listed", "source.json"])
        self.assertIsNone(pass_inputs._active)


if __name__ == "__main__":
    unittest.main()
