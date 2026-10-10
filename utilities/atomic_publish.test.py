#!/usr/bin/env python3
"""Run normally on local storage, or with TMPDIR on an actual NFS mount."""
import ctypes
import errno
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import atomic_publish as A


def unsupported_library(code=errno.EINVAL, side_effect=None):
    def rename(*args):
        if side_effect:
            side_effect(*args)
        ctypes.set_errno(code)
        return -1
    return mock.Mock(renameat2=mock.Mock(side_effect=rename))


def competing_writer(source, target, start, results):
    start.wait(10)
    try:
        with mock.patch.object(A.ctypes, "CDLL", return_value=unsupported_library()):
            A.rename_directory(source, target)
        results.put("published")
    except FileExistsError:
        results.put("collision")
    except Exception as exc:
        results.put(repr(exc))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atomic-publish-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "stage"
        self.source.mkdir()
        (self.source / "payload").write_text("complete")
        self.target = self.root / "report"

    def test_actual_filesystem_publishes_complete_tree_and_refuses_collision(self):
        A.rename_directory(self.source, self.target)
        self.assertFalse(self.source.exists())
        self.assertEqual((self.target / "payload").read_text(), "complete")
        inode = self.target.stat().st_ino
        self.source.mkdir()
        with self.assertRaises(FileExistsError):
            A.rename_directory(self.source, self.target)
        self.assertEqual(self.target.stat().st_ino, inode)
        self.assertTrue(self.source.exists())

    def test_unsupported_errors_and_missing_symbol_use_fallback(self):
        for library in [unsupported_library(code) for code in
                        (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP)] + [object()]:
            with self.subTest(library=library), \
                    mock.patch.object(A.ctypes, "CDLL", return_value=library):
                A.rename_directory(self.source, self.target)
            self.assertEqual((self.target / "payload").read_text(), "complete")
            os.rename(self.target, self.source)

    def test_other_errors_never_fall_back(self):
        for code in (errno.EXDEV, errno.EACCES, errno.EIO, errno.EEXIST):
            with self.subTest(code=code), \
                    mock.patch.object(A.ctypes, "CDLL", return_value=unsupported_library(code)), \
                    mock.patch.object(A.os, "rename") as rename, \
                    self.assertRaises(OSError) as caught:
                A.rename_directory(self.source, self.target)
            self.assertEqual(caught.exception.errno, code)
            rename.assert_not_called()
            self.assertTrue(self.source.exists())
            self.assertFalse(self.target.exists())

    def test_existing_empty_directory_file_and_dangling_link_are_preserved(self):
        for kind in ("directory", "file", "symlink"):
            if kind == "directory": self.target.mkdir()
            elif kind == "file": self.target.write_text("foreign")
            else: self.target.symlink_to("absent")
            inode = self.target.lstat().st_ino
            with mock.patch.object(A.ctypes, "CDLL", return_value=unsupported_library()), \
                    self.assertRaises(FileExistsError):
                A.rename_directory(self.source, self.target)
            self.assertEqual(self.target.lstat().st_ino, inode)
            self.assertTrue(self.source.exists())
            if kind == "directory": self.target.rmdir()
            else: self.target.unlink()

    def test_destination_appearing_during_unsupported_syscall_is_preserved(self):
        def foreign(*args): self.target.mkdir()
        with mock.patch.object(A.ctypes, "CDLL", return_value=unsupported_library(side_effect=foreign)), \
                self.assertRaises(FileExistsError):
            A.rename_directory(self.source, self.target)
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertTrue(self.source.exists())

    def test_concurrent_fallback_publishers_have_one_winner(self):
        context = multiprocessing.get_context("fork")
        start, results = context.Event(), context.Queue()
        sources = []
        for i in range(6):
            source = self.root / ("stage-" + str(i)); source.mkdir()
            (source / "payload").write_text(str(i)); sources.append(source)
        processes = [context.Process(target=competing_writer,
                     args=(source, self.target, start, results)) for source in sources]
        for process in processes: process.start()
        start.set()
        try:
            observations = [results.get(timeout=15) for _ in processes]
            self.assertEqual(observations.count("published"), 1, observations)
            self.assertEqual(observations.count("collision"), 5, observations)
            winner = int((self.target / "payload").read_text())
            self.assertEqual([p.exists() for p in sources], [i != winner for i in range(6)])
        finally:
            for process in processes:
                process.join(2)
                if process.is_alive(): process.kill(); process.join()
            results.close()

    def test_process_death_releases_persistent_lock(self):
        lock = self.root / ".hearting-publish.lock"
        script = "import fcntl,os,sys,time; f=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(30)"
        process = subprocess.Popen([sys.executable, "-c", script, str(lock)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            inode = lock.stat().st_ino
            process.kill(); process.wait(timeout=5)
            A.rename_directory(self.source, self.target)
            self.assertEqual(lock.stat().st_ino, inode)
        finally:
            if process.poll() is None: process.kill(); process.wait()
            process.stdout.close()

    def test_foreign_lock_symlink_and_hardlink_fail_without_publication(self):
        foreign = self.root / "foreign"; foreign.write_text("unchanged")
        lock = self.root / ".hearting-publish.lock"
        for kind in ("symlink", "hardlink"):
            if kind == "symlink": lock.symlink_to(foreign)
            else: os.link(foreign, lock)
            with self.assertRaises(OSError): A.rename_directory(self.source, self.target)
            self.assertEqual(foreign.read_text(), "unchanged")
            self.assertTrue(self.source.exists()); self.assertFalse(self.target.exists())
            lock.unlink()


if __name__ == "__main__":
    unittest.main()
