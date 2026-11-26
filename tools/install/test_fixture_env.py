#!/usr/bin/env python3

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import os
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest import mock

import fixture_env
import safe_fs


class FixtureEnvironmentTest(unittest.TestCase):
    def test_all_path_selectors_are_rebound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / "source"
            source.mkdir()
            hostile = {
                "HOME": "/outside/home",
                "ZDOTDIR": "/outside/zdot",
                "XDG_CACHE_HOME": "/outside/cache",
                "TMPDIR": "/outside/tmp",
                "AGENT_DISPATCH_JOBS": "/outside/jobs.log",
                "HEARTING_MODEL_CONFIG_HOME": "/outside/model-config",
                "PATH": os.environ.get("PATH", ""),
            }
            env = fixture_env.build_environment(root / "fixture", source, base=hostile)
            fixture = Path(env["HEARTING_FIXTURE_ROOT"])
            path_keys = fixture_env._SCRUB - {"AGENT_HOME", "HEARTING_SAFE_LOCK_ROOT"}
            for key in path_keys:
                if key in env:
                    self.assertTrue(
                        Path(env[key]).is_relative_to(fixture),
                        f"{key} escaped fixture: {env[key]}",
                    )
            self.assertNotIn("HEARTING_MODEL_CONFIG_HOME", env)
            self.assertIn("unset HEARTING_MODEL_CONFIG_HOME", fixture_env.shell_exports(env))
            self.assertEqual(env["ZDOTDIR"], env["HOME"])
            self.assertEqual(env["AGENT_HOME"], str(source))

    def test_an_empty_or_relative_root_is_refused_instead_of_becoming_the_checkout(self) -> None:
        # A failed `mktemp -d` hands an empty root to `fixture_env.py shell "$TMP"`; resolving it
        # made the current checkout the fixture root and the caller's EXIT trap deleted it.
        for root in ("", "relative/root"):
            with self.assertRaises(ValueError):
                fixture_env.build_environment(root, Path(__file__).resolve().parents[2])
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = fixture_env.main(["shell", "", str(Path(__file__).resolve().parents[2])])
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("nothing was exported", err.getvalue())

    def test_safe_fs_rejects_outside_target_before_lock_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            fixture = base / "fixture"
            source = base / "source"
            source.mkdir()
            env = fixture_env.build_environment(fixture, source, base={})
            fixture_env.prepare_environment(env)
            lock_root = safe_fs._lock_root()
            outside = base / "outside"
            key = hashlib.sha256(os.fsencode(str(outside))).hexdigest()
            outside_lock = lock_root / f"{key}.lock"
            self.assertFalse(outside_lock.exists())
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(safe_fs.SafetyError, "target-outside-fixture"):
                    safe_fs.authority(
                        outside,
                        owner="fixture-test",
                        allowed_paths=(outside,),
                    )
            self.assertFalse(outside_lock.exists())


if __name__ == "__main__":
    unittest.main()
