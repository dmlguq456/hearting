#!/usr/bin/env python3

from __future__ import annotations

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
            self.assertEqual(env["ZDOTDIR"], env["HOME"])
            self.assertEqual(env["AGENT_HOME"], str(source))

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
