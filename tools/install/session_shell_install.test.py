"""Opt-in installation only touches a private prefix and its exact profile block."""
from pathlib import Path
import importlib.util
import json
import os
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_shell_install as install
ROOT = Path(__file__).resolve().parents[2]


class SessionShellInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shell-install-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = {"HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config"),
               "XDG_DATA_HOME": str(self.root / "data"), "XDG_CACHE_HOME": str(self.root / "cache"),
               "XDG_STATE_HOME": str(self.root / "state"), "SHELL": "/bin/bash", "ZDOTDIR": "",
               "AGENT_HOME": str(ROOT), "HEARTING_FIXTURE_ROOT": str(self.root)}
        self.env = patch.dict(os.environ, env);self.env.start();self.addCleanup(self.env.stop)
        self.home = self.root / "home";self.home.mkdir()
        self.profile = self.home / ".bashrc"
        self.profile.write_bytes(b"# person's existing profile\nalias ll='ls -l'\n");self.profile.chmod(0o600)
        self.native = self.root / "native";self.native.mkdir()
        for harness in install.HARNESSES:
            target = self.native / harness;target.write_bytes(b"#!/bin/sh\nprintf 'native\\n'\n");target.chmod(0o755)
        os.environ["PATH"] = str(self.native)
        self.assets = patch.object(install, "prepare_assets", return_value=self.root / "prepared")
        self.assets.start();self.addCleanup(self.assets.stop)

    def test_default_off_does_not_create_policy_prefix_or_change_profile(self):
        before = self.profile.read_bytes()
        self.assertEqual(install.ensure(), {"status": "disabled", "changed": False})
        self.assertFalse(install._bin().exists())
        self.assertFalse(install._client().policy_path().exists())
        self.assertEqual(self.profile.read_bytes(), before)

    def test_enable_and_disable_keep_native_files_and_restore_profile(self):
        before = self.profile.read_bytes()
        natives = {p: p.read_bytes() for p in self.native.iterdir()}
        result = install.set_enabled(True)
        self.assertEqual(result["status"], "enabled")
        self.assertTrue(install._client().policy()[0])
        for harness in install.HARNESSES:
            self.assertEqual((install._bin() / harness).read_bytes(), install._wrapper(harness, str(self.native / harness)))
        install.set_enabled(False)
        self.assertFalse(install._client().policy()[0])
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertEqual({p: p.read_bytes() for p in self.native.iterdir()}, natives)

    def test_enable_disable_preserves_optional_layout_preferences(self):
        policy=install._client().policy_path();policy.parent.mkdir(parents=True,exist_ok=True)
        policy.write_text(json.dumps({'enabled':False,'layout':'side','side_width':48,'side_min_columns':150}));policy.chmod(0o600)
        install.set_enabled(True)
        self.assertEqual(install._client().display_policy(),{'layout':'side','side_width':48,'side_min_columns':150,'narrow':'bottom','auxiliary':'summary','bottom_rows':14})
        install.set_enabled(False)
        self.assertEqual(json.loads(policy.read_text())['side_width'],48)

    def test_enable_and_disable_restore_a_profile_without_its_own_final_lf(self):
        before = b"alias keep='printf keep'"
        self.profile.write_bytes(before)
        install.set_enabled(True)
        install.set_enabled(True)  # Repeated ensure preserves the separator provenance.
        install.set_enabled(False)
        self.assertEqual(self.profile.read_bytes(), before)

    def test_disable_preserves_lines_appended_after_the_owned_block(self):
        for suffix in (b"export AFTER=two\n", b"\nexport AFTER=two\n", b"# appended comment"):
            with self.subTest(suffix=suffix):
                self.profile.write_bytes(b"export BEFORE=one")
                install.set_enabled(True)
                with self.profile.open("ab") as stream:
                    stream.write(suffix)
                install.set_enabled(False)
                self.assertEqual(self.profile.read_bytes(), b"export BEFORE=one\n" + suffix)
                if b"AFTER" in suffix:
                    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c",
                        'source "$1"; printf "%s|%s" "$BEFORE" "$AFTER"', "fixture", str(self.profile)],
                        env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
                        capture_output=True, text=True, check=True)
                    self.assertEqual(result.stdout, "one|two")

    def test_changed_launcher_is_preserved_before_any_profile_or_policy_change(self):
        target = install._bin() / "claude";target.parent.mkdir(parents=True)
        target.write_bytes(b"#!/bin/sh\n# user-owned command\n")
        before = self.profile.read_bytes()
        with self.assertRaisesRegex(ValueError, "foreign"):
            install.set_enabled(True)
        self.assertEqual(target.read_bytes(), b"#!/bin/sh\n# user-owned command\n")
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertFalse(install._client().policy_path().exists())

    def test_dry_run_has_no_writes(self):
        before = self.profile.read_bytes()
        self.assertEqual(install.set_enabled(True, dry_run=True)["status"], "planned")
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertFalse(install._bin().exists())
        self.assertFalse(install._client().policy_path().exists())

    def test_modified_profile_block_is_not_overwritten(self):
        install.set_enabled(True)
        self.profile.write_bytes(self.profile.read_bytes().replace(b'export PATH=', b'export CUSTOM='))
        before = self.profile.read_bytes()
        with self.assertRaisesRegex(ValueError, "preserved"):
            install.set_enabled(False)
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertFalse(install._client().policy()[0])


if __name__ == "__main__":
    unittest.main()
