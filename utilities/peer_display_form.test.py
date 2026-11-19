#!/usr/bin/env python3
"""Message display form: the decoration-free trailer, the visible forms and how a receiver finds the sealed record from them. Fixtures only."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pm = module("display_peer_message", "utilities/peer-message.py")
fixture_processes = module("display_fixture_processes", "tools/fixture_processes.py")


def release_source(tag, path):
    """The receiver code a published release shipped, or None when the tag is not in this clone."""
    try:
        return subprocess.check_output(["git", "-c", f"safe.directory={ROOT}", "show", f"{tag}:{path}"],
                                       cwd=ROOT, text=True, timeout=10, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        return None


OLD_RELEASES = ("v4.62.5", "v4.60.0", "v4.58.0")


def old_decorated_trailer(harness, session_id, name=None, *, transfer_ref=None, pane_id=None):
    """The trailer as releases before the decoration was dropped built it (frozen)."""
    parts = [harness or "unknown", pm.peer_alias(harness, session_id)]
    if name:
        parts.append(name)
    suffix = " ; pane=%s ; sid=%s" % (pane_id or "-", pm.usable_session_id(session_id) or "-")
    if transfer_ref:
        suffix += f" ; ref={transfer_ref}"
    return "(%s: %s%s)" % ("runtime-from" if harness == "runtime" else "peer-from", " ".join(parts), suffix)


class DisplayFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(fixture_processes.reap, self.tmp.name)
        self.state = Path(self.tmp.name)
        for path in (self.state / "home", self.state / "xdg", self.state / "xdg-config",
                     self.state / "xdg-data", self.state / "artifacts", self.state / "runtime",
                     self.state / "peer-ledger"):
            path.mkdir(parents=True, exist_ok=True)
        self.agent_home = self.state / "agent-home"
        self.agent_home.mkdir()
        (self.agent_home / "core").mkdir()
        shutil.copy2(ROOT / "core/CORE.md", self.agent_home / "core/CORE.md")
        if (ROOT / "harness-manifest.json").is_file():
            shutil.copy2(ROOT / "harness-manifest.json", self.agent_home / "harness-manifest.json")
        if (ROOT / "RELEASE_VERSION").is_file():
            shutil.copy2(ROOT / "RELEASE_VERSION", self.agent_home / "RELEASE_VERSION")
        for name in ("adapters", "utilities", "tools"):
            (self.agent_home / name).symlink_to(ROOT / name, target_is_directory=True)
        inherited = dict(os.environ)
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_ARTIFACT_", "AGENT_SESSION_", "AGENT_RUNTIME_",
                               "AGENT_THREAD_", "HERDR_")):
                os.environ.pop(key, None)
        for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID",
                    "CODEX_SESSION_ID", "OPENCODE_SESSION_ID", "OPENCODE_DISPATCH_SLUG",
                    "FLEET_TITLE_REFRESH", "HARNESS_STATE_ROOT", "HARNESS_DATA_ROOT",
                    "AGENT_SERVICE_CURRENT", "HEARTING_SERVICE_CURRENT", "SERVICE_CURRENT",
                    "AGENT_INSTALL_CURRENT", "AGENT_INSTALL_ROOT"):
            os.environ.pop(key, None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(inherited)))
        self.env = mock.patch.dict(os.environ, {
            "AGENT_DISPATCH_JOBS": str(self.state / "jobs.log"),
            "AGENT_PEER_LEDGER_ROOT": str(self.state),
            "HOME": str(self.state / "home"), "CODEX_HOME": str(self.state / "home/.codex"),
            "AGENT_HOME": str(self.state / "agent-home"),
            "CLAUDE_HOME": str(self.state / "claude-home"),
            "AGENT_RUNTIME_ROOT": str(self.state / "runtime"),
            "CLAUDE_CONFIG_DIR": str(self.state / "home/.claude"),
            "OPENCODE_CONFIG_DIR": str(self.state / "home/.config/opencode"),
            "XDG_STATE_HOME": str(self.state / "xdg"),
            "XDG_CONFIG_HOME": str(self.state / "xdg-config"),
            "XDG_DATA_HOME": str(self.state / "xdg-data"),
            "PYTHONPYCACHEPREFIX": str(self.state / "pycache"),
            "AGENT_ARTIFACT_ROOT": str(self.state / "artifacts"),
            "HARNESS_DATA_ROOT": str(self.state / "data"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sender = {"harness": "codex", "session_id": "0100000a-0000-7000-8000-000000000012", "name": "동명"}
        self.recipient = {"harness": "codex", "session_id": "recipient-a"}

    def prepare(self, body="실제 본문 한 줄", sender=None, recipient=None, **kwargs):
        return pm.prepare_peer_message(body, sender or self.sender, recipient or self.recipient, **kwargs)


class TrailerDecorationDropped(DisplayFixture):
    """Step one: `pane=` and `sid=` are not sent, and no receiver ever read them."""

    def test_trailer_carries_the_tag_the_optional_name_and_the_ref_only(self):
        text, ref = self.prepare(sender=dict(self.sender, pane_id="w:sender"))
        trailer = text.rsplit("\n\n", 1)[1]
        self.assertRegex(trailer, r"^\(peer-from: codex \[(?:[0-9a-f]{2}|\?)\] 동명 ; ref=" + ref + r"\)$")
        for hidden in ("pane=", "sid=", self.sender["session_id"], "w:sender"):
            self.assertNotIn(hidden, text)

    def test_every_published_receiver_answers_the_same_for_both_forms(self):
        checked = 0
        for tag in OLD_RELEASES:
            source = release_source(tag, "utilities/peer-message.py")
            if source is None:
                continue
            scope = {"__file__": str(ROOT / "utilities/peer-message.py"), "__name__": "pinned_" + tag.replace(".", "_")}
            exec(compile(source, "peer-message@" + tag, "exec"), scope)
            answers = []
            for builder in (old_decorated_trailer, pm.peer_trailer):
                with mock.patch.object(pm, "peer_trailer", side_effect=builder):
                    text, ref = self.prepare(body="본문 " + tag + str(len(answers)))
                answer = scope["parse_peer_trailer"](text, self.recipient, include_ref=True)
                self.assertEqual(answer["session_id"], self.sender["session_id"], (tag, text))
                self.assertEqual(answer["name"], self.sender["name"])
                self.assertEqual(answer["transfer_ref"], ref)
                # the same text for a wrong recipient or a changed body stays unattributed
                for bad_text, bad_recipient in ((text + "변조", self.recipient),
                                                (text, {"harness": "codex", "session_id": "other"})):
                    self.assertIsNone(scope["parse_peer_trailer"](bad_text, bad_recipient)["session_id"])
                answers.append({k: v for k, v in answer.items() if k != "transfer_ref"})
            self.assertEqual(answers[0], answers[1], tag)
            checked += 1
        if not checked:
            self.skipTest("no published release is available in this clone")


if __name__ == "__main__":
    unittest.main()
