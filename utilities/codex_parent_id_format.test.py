import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
from pathlib import Path
import re
import sys
import unittest
import uuid
from datetime import datetime, timezone


class ParentIdFormatTest(unittest.TestCase):
    def test_live_rollout_names_are_uuidv7_when_present(self):
        root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
        paths = list(root.glob("**/rollout-*.jsonl")) if root.is_dir() else []
        if not paths:
            self.skipTest("CODEX_HOME sessions directory absent or empty")
        for path in paths:
            match = re.search(
                r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-"
                r"([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl$", path.name, re.I
            )
            self.assertIsNotNone(match, path.name)
            value = uuid.UUID(match.group(2))
            self.assertEqual(value.version, 7)
            self.assertEqual(value.variant, uuid.RFC_4122)
            # Rollout names use the host's local wall-clock convention, while
            # UUIDv7 timestamps are UTC epoch milliseconds.
            local_zone = datetime.now().astimezone().tzinfo or timezone.utc
            stamp = datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S").replace(tzinfo=local_zone)
            filename_ms = int(stamp.timestamp() * 1000)
            embedded_ms = value.int >> 80
            self.assertLessEqual(abs(embedded_ms - filename_ms), 5000, path.name)


if __name__ == "__main__":
    unittest.main()
