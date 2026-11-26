import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from codex_hook_definition_age import prove_parent_definition


def sid(ms):
    return str(uuid.UUID(int=(ms << 80) | (7 << 76) | (0x8 << 60)))


class HookDefinitionAgeTest(unittest.TestCase):
    def test_hash_ledger_preserves_first_observation_and_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks = root / "target.json"
            hooks.write_text("{}", encoding="utf-8")
            os.utime(hooks, ns=(2_000_000_000_000_000_000,) * 2)
            kwargs = dict(hooks_path=root / "link.json", ledger_path=root / "ledger.json", lock_path=root / "ledger.lock")
            (root / "link.json").symlink_to(hooks)
            self.assertTrue(prove_parent_definition(sid(2_000_000_000_000), **kwargs).eligible)
            os.utime(hooks, ns=(2_100_000_000_000_000_000,) * 2)
            self.assertTrue(prove_parent_definition(sid(2_000_000_000_000), **kwargs).eligible)
            self.assertFalse(prove_parent_definition(sid(1_999_999_999_999), **kwargs).eligible)
            hooks.write_text("changed", encoding="utf-8")
            os.utime(hooks, ns=(2_100_000_000_000_000_000,) * 2)
            proof = prove_parent_definition(sid(2_100_000_000_000), **kwargs)
            self.assertTrue(proof.eligible)
            data = json.loads((root / "ledger.json").read_text())
            self.assertEqual(len(data["entries"]), 2)

    def test_invalid_parent_and_corrupt_ledger_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks = root / "hooks.json"
            hooks.write_text("{}")
            kwargs = dict(hooks_path=hooks, ledger_path=root / "ledger.json", lock_path=root / "ledger.lock")
            self.assertEqual(prove_parent_definition("legacy-parent", **kwargs).reason, "parent-id-format-unproven")
            (root / "ledger.json").write_text("{bad")
            self.assertEqual(prove_parent_definition(sid(2_000_000_000_000), **kwargs).reason, "definition-ledger-unavailable")


if __name__ == "__main__":
    unittest.main()
