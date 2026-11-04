#!/usr/bin/env python3
"""Exercise human history notices as a channel separate from model context."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools/install"))
import session_tidy

spec = importlib.util.spec_from_file_location(
    "codex_prompt_history", ROOT / "adapters/codex/hooks/userprompt-lifecycle.py")
codex = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = codex
spec.loader.exec_module(codex)


class HistoryChannelTests(unittest.TestCase):
    @patch.dict("os.environ", {"FLEET_NOW_LANG": "Korean"})   # Korean notice words
    def test_person_notice_uses_confirmed_predecessor_and_omits_path(self):
        lineage = [{"harness": "claude", "sid": "new", "relation": "confirmed",
                    "predecessor": {"harness": "claude", "sid": "old"}},
                   {"harness": "claude", "sid": "old", "transcript": {
                       "path": "/private/raw/transcript", "status": "available"}}]
        seat = session_tidy.Seat("pane", "key", "pane", "claude")
        with patch.object(session_tidy, "seat_lineage", return_value=lineage), \
             patch("herdr_integration.history_key_available", return_value=True), \
             patch("herdr_integration.history_key_label", return_value="Ctrl-b Shift-H"):
            notice = session_tidy.build_human_history_notice(
                seat, "claude", "new", env={"HERDR_ACTIVE_PANE_ID": "pane", "HERDR_SOCKET_PATH": "/socket"})
        self.assertIn("Ctrl-b Shift-H", notice)
        self.assertIn("원문 있음", notice)
        self.assertNotIn("/private/raw/transcript", notice)

    def test_unconfirmed_neighbor_does_not_make_notice(self):
        seat = session_tidy.Seat("pane", "key", "pane", "claude")
        with patch.object(session_tidy, "seat_lineage", return_value=[
                {"harness": "claude", "sid": "new", "relation": "chronological"},
                {"harness": "claude", "sid": "neighbor", "relation": "chronological"}]):
            self.assertEqual(session_tidy.build_human_history_notice(seat, "claude", "new", env={}), "")

    def test_codex_places_notice_outside_additional_context(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            codex.emit_context("UserPromptSubmit", ["MODEL-CARD"], "PERSON-NOTICE")
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], "MODEL-CARD")
        self.assertEqual(payload["systemMessage"], "PERSON-NOTICE")

    def test_codex_prompt_card_bridge_keeps_separate_fields(self):
        human, startup = [], {}
        receipt = {"kind": "startup-context", "receipt": "fixture"}
        def shared_hook(*args, **kwargs):
            self.assertEqual(args, ("codex", "prompt", "sid"))
            kwargs["emit"]("MODEL-CARD")
            kwargs["emit_human"]("PERSON-NOTICE")
            kwargs["startup_context"]["due"] = True
            kwargs["deferred"].append(receipt)
        with patch.object(session_tidy, "run_hook", side_effect=shared_hook):
            card, deferred = codex.card_context({"session_id": "sid"}, "/fixture",
                                                 human=human, startup=startup)
        self.assertEqual((card, human, deferred), ("MODEL-CARD", ["PERSON-NOTICE"], [receipt]))
        self.assertTrue(startup["due"])

    def test_human_only_lookup_leaves_ledger_and_receipts_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            seat = session_tidy.Seat("pane", "isolated-seat", "pane", "opencode")
            emitted = []
            with patch.dict("os.environ", {"XDG_STATE_HOME": str(state)}, clear=False), \
                 patch.object(session_tidy, "resolve_seat", return_value=seat), \
                 patch.object(session_tidy, "build_human_history_notice", return_value="PERSON-NOTICE"):
                ledger = session_tidy._ledger_path(seat)
                ledger.parent.mkdir(parents=True)
                before = b'{"event":"start","sid":"prior"}\n'
                ledger.write_bytes(before)
                session_tidy.run_hook("opencode", "start", "current", env={},
                                      emit_human=emitted.append, human_only=True)
                self.assertEqual(emitted, ["PERSON-NOTICE"])
                self.assertEqual(ledger.read_bytes(), before)
                self.assertFalse(session_tidy._consumed_path(seat).exists())

    def test_a_late_start_leaves_out_only_the_person_notice(self):
        # HOOK-LATENCY: the notice reads the whole shared lineage (about half of
        # a 1 s start). Past the hook's deadline it is left out; the start is
        # still recorded and the card path runs as before.
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            seat = session_tidy.Seat("pane", "late-start-seat", "pane", "claude")
            for deadline, expected in ((0.0, []), (None, ["PERSON-NOTICE"]),
                                       (float("inf"), ["PERSON-NOTICE"])):
                with self.subTest(deadline=deadline), \
                     patch.dict("os.environ", {"XDG_STATE_HOME": str(state)}, clear=False), \
                     patch.object(session_tidy, "resolve_seat", return_value=seat), \
                     patch.object(session_tidy, "build_human_history_notice",
                                  return_value="PERSON-NOTICE") as notice:
                    emitted = []
                    session_tidy.run_hook("claude", "start", f"sid-{deadline}", source="startup",
                                          env={}, emit_human=emitted.append, deferred=[],
                                          human_deadline=deadline)
                    self.assertEqual(emitted, expected)
                    self.assertEqual(notice.called, bool(expected))
                    ledger = session_tidy._ledger_path(seat).read_text(encoding="utf-8")
                    self.assertIn(f"sid-{deadline}", ledger)

    def test_the_claude_card_hook_bounds_the_notice_only_for_a_tidy_that_knows_it(self):
        hook = (ROOT / "hooks" / "session-card-inject.sh").read_text(encoding="utf-8")
        self.assertIn('getattr(session_tidy, "HUMAN_NOTICE_BUDGET_SECONDS", None)', hook)
        self.assertEqual(session_tidy.HUMAN_NOTICE_BUDGET_SECONDS, 2.0)

    def test_opencode_history_notice_lookup_settles_per_session(self):
        script = r'''
import { readFileSync } from "node:fs"
import childProcess from "node:child_process"
const lookups = new Map()
const notices = []
const fakeSpawnSync = (command, args, options) => {
  if (args?.includes("--human-only")) {
    const sid = args[args.indexOf("--session-id") + 1]
    const count = (lookups.get(sid) || 0) + 1
    lookups.set(sid, count)
    const notice = sid === "late" && count === 2 ? "NOTICE-APPEARED" : ""
    return { status: 0, stdout: JSON.stringify({ humanNotice: notice }), stderr: "" }
  }
  return { status: 0, stdout: "", stderr: "" }
}
globalThis.__historyNoticeFakeSpawnSync = fakeSpawnSync
globalThis.__historyNoticeFakeSpawn = () => ({ on() {}, unref() {}, stdin: { end() {} } })
let source = readFileSync("adapters/opencode/plugins/hearting-guards.js", "utf8")
source = source.replace('import { spawnSync, spawn } from "node:child_process"',
  'const spawn = globalThis.__historyNoticeFakeSpawn\nconst spawnSync = globalThis.__historyNoticeFakeSpawnSync')
source = source.replace('const pluginDir = path.dirname(fileURLToPath(import.meta.url))',
  'const pluginDir = path.join(process.cwd(), "adapters/opencode/plugins")')
const { AgentHarnessGuards } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"))
const plugin = await AgentHarnessGuards({ directory: process.cwd(), worktree: process.cwd(),
  client: { tui: { showToast: ({ body }) => notices.push(body.message) } } }, { initialize: false })
const created = (sid) => plugin.event({ event: { type: "session.created", properties: { info: { id: sid } } } })
const turn = (sid, id) => plugin["chat.message"]({ sessionID: sid, messageID: id },
  { parts: [{ type: "text", text: "hello" }] })
await created("empty")
await turn("empty", "e1")
await turn("empty", "e2")
await turn("empty", "e3")
await created("late")
await turn("late", "l1")
await turn("late", "l2")
await turn("late", "l3")
if (lookups.get("empty") !== 2) throw new Error(`empty lookup count=${lookups.get("empty")}`)
if (lookups.get("late") !== 2) throw new Error(`late lookup count=${lookups.get("late")}`)
if (notices.filter((notice) => notice === "NOTICE-APPEARED").length !== 1)
  throw new Error(`late notice count=${JSON.stringify(notices)}`)
console.log("empty=2 late=2 late-notice=1")
'''
        with tempfile.TemporaryDirectory() as directory:
            env = dict(__import__("os").environ, XDG_STATE_HOME=directory,
                       PYTHONDONTWRITEBYTECODE="1", AGENT_HOME=str(ROOT))
            for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                        "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH"):
                env.pop(key, None)
            result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                                    env=env, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("empty=2 late=2 late-notice=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
