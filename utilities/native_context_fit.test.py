#!/usr/bin/env python3
"""PG-47/PG-48: a native hook context carries only what its runtime shows inline.

Claude Code shows at most 10,000 characters of one hook context and Codex about
2,500 tokens; the rest goes to a file the model is not asked to read. A burst
of notices must therefore be ACKed only for what the context holds whole, the
rest staying pending for the next boundary, and a single text over the limit
must point to a whole copy instead of being ACKed cut, or stay pending when
that copy cannot be saved. A split keeps the order of one whole rendering, and
the rest a Claude prompt could not show continues the same turn at Stop with no
further prompt. PG-48: the Claude peer carrier also runs at a failed tool's end.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

import fixture_processes  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FixtureHome(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="pg40-fit-")
        base = Path(self._tmp.name)
        harness = base / "harness-state"
        for path in (base / "home", base / "xdg", harness):
            path.mkdir(parents=True)
        self.env = {
            "HOME": str(base / "home"), "XDG_STATE_HOME": str(base / "xdg"),
            "HARNESS_STATE_ROOT": str(harness),
            "AGENT_DISPATCH_JOBS": str(harness / "dispatch" / "jobs.log"),
            "AGENT_PEER_LEDGER_ROOT": str(harness / "peer-ledger"),
            "AGENT_HOME": str(ROOT), "AGENT_SESSION_ROLE": "", "AGENT_DISPATCH_CHILD": "",
            "AGENT_DISPATCH_DEPTH": "", "CLAUDE_CONFIG_DIR": str(base / "home" / ".claude"),
        }
        self._saved = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        self.root = harness / "dispatch"

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        fixture_processes.reap(self._tmp.name)
        self._tmp.cleanup()
        super().tearDown()


class SharedFitTest(unittest.TestCase):
    def setUp(self):
        self.sweep = _load("dispatch_session_sweep", HERE / "dispatch_session_sweep.py")

    def render(self, batches):
        return "\n".join(f"- {r['delivery_id']} {r['body']}" for _root, rs in batches for r in rs)

    def test_burst_keeps_only_whole_records_and_hands_back_the_rest(self):
        records = [{"delivery_id": f"delivery-{i:02d}", "body": "x" * 600} for i in range(20)]
        text, chosen, rest = self.sweep.fit([(Path("/r"), records)], self.render, 9_500)
        shown = [r for _root, rs in chosen for r in rs]
        self.assertLessEqual(len(text), 9_500)
        self.assertEqual(len(shown) + sum(len(rs) for _root, rs in rest), 20)
        for record in shown:
            self.assertIn(record["delivery_id"], text)
        for _root, rs in rest:
            for record in rs:
                self.assertNotIn(record["delivery_id"], text)

    def test_codex_budget_counts_utf8_bytes(self):
        records = [{"delivery_id": f"delivery-{i:02d}", "body": "보고" * 300} for i in range(10)]
        text, chosen, _rest = self.sweep.fit([(Path("/r"), records)], self.render, 9_600,
                                             measure=self.sweep.utf8_len)
        self.assertLessEqual(self.sweep.utf8_len(text), 9_600)
        self.assertLess(sum(len(rs) for _root, rs in chosen), 10)

    def test_single_text_over_the_limit_points_to_a_whole_copy(self):
        with tempfile.TemporaryDirectory(prefix="pg40-over-") as tmp:
            body = "HEAD " + "y" * 20_000 + " TAIL"
            records = [{"delivery_id": "delivery-big", "body": body}]
            render = lambda b: self.render(b) + "\nHEARTING_RECEIPT_V1 delivery-big abc"  # noqa: E731
            text, chosen, rest = self.sweep.fit_inline([(Path(tmp), records)], 9_500, render=render)
            self.assertLessEqual(len(text), 9_500)
            self.assertEqual(rest, [])
            self.assertEqual(len(chosen[0][1]), 1)
            self.assertIn("HEARTING_RECEIPT_V1 delivery-big abc", text)
            path = Path(text.split("full text: ", 1)[1].split("]", 1)[0])
            self.assertTrue(path.is_file())
            self.assertIn(" TAIL", path.read_text(encoding="utf-8"))
            self.assertTrue(text.startswith(self.sweep.OVERFLOW_HEADER))


    def test_a_text_over_the_limit_without_a_whole_copy_stays_pending(self):
        with tempfile.TemporaryDirectory(prefix="pg40-blocked-") as tmp:
            (Path(tmp) / self.sweep.OVERFLOW_DIRNAME).write_text("not a directory")
            records = [{"delivery_id": "delivery-big", "body": "z" * 20_000},
                       {"delivery_id": "delivery-small", "body": "small"}]
            render = lambda b: self.render(b)  # noqa: E731 - keeps arrival order
            with mock.patch.object(self.sweep, "_render_keys", side_effect=ValueError):
                text, chosen, rest = self.sweep.fit_inline([(Path(tmp), records)], 9_500,
                                                           render=render)
            self.assertLessEqual(len(text), 9_500)
            self.assertEqual([r["delivery_id"] for _root, rs in chosen for r in rs],
                             ["delivery-small"])
            self.assertEqual([r["delivery_id"] for _root, rs in rest for r in rs],
                             ["delivery-big"])
            text, chosen, rest = self.sweep.fit_inline([(Path(tmp), records[:1])], 9_500,
                                                       render=render)
            self.assertEqual((text, chosen), ("", []))
            self.assertEqual(len(rest[0][1]), 1)

    def test_a_split_keeps_the_order_of_one_whole_rendering(self):
        # The review's order probe (RV-PR1477 P2-2): storage order C, A, B.
        root = Path("/order-fixture")
        records = [{"delivery_id": "delivery-" + ch,
                    "receipt": {"schema_version": 1, "kind": "notice", "key": "order-" + ch,
                                "subject": "report", "text": "ORDER-" + ch + " " + "x" * 60,
                                "required_action": "read-notice"}} for ch in "CAB"]
        whole = self.sweep.delivery_context([(root, records)])
        expected = sorted("ABC", key=lambda ch: whole.index("ORDER-" + ch))
        budget = len(self.sweep.delivery_context([(root, [records[0]])])) + 2
        batches, order = [(root, records)], []
        for _ in range(4):
            if not batches:
                break
            text, _kept, batches = self.sweep.fit_inline(batches, budget)
            order.extend(sorted((ch for ch in "ABC" if "ORDER-" + ch in text),
                                key=lambda ch: text.index("ORDER-" + ch)))
        self.assertEqual(order, expected)
        self.assertEqual(expected, ["A", "B", "C"])

    def test_a_sealed_batch_stays_whole_at_its_first_member_place(self):
        root = Path("/order-fixture")

        def notice(name, owner=None):
            record = {"delivery_id": "delivery-" + name,
                      "receipt": {"kind": "notice", "text": name + " " + "y" * 40}}
            if owner:
                record["claim_owner"] = owner
            return record
        records = [notice("D"), notice("B", "batch-1"), notice("A"), notice("C", "batch-1")]
        units = self.sweep._units([(root, records)])
        self.assertEqual([[r["delivery_id"][-1] for r in rs] for _root, rs in units],
                         [["A"], ["B", "C"], ["D"]])


class ClaudePromptSweepTest(FixtureHome):
    SID = "pg40-claude-session"

    def notify(self, count: int):
        import session_notice
        tails = []
        for i in range(count):
            tail = f"HB-TAIL-{i:02d}"
            text = f"HB-BURST-{i:02d} " + ("worker progress line " * 40)[:520] + " " + tail
            record = session_notice.notify("claude", self.SID, key=f"hb-{i}",
                                           subject=f"burst {i}", text=text)
            self.assertIsNotNone(record)
            tails.append((tail, record["delivery_id"]))
        return tails

    def prompt(self) -> str:
        payload = json.dumps({"session_id": self.SID, "hook_event_name": "UserPromptSubmit"})
        done = subprocess.run([sys.executable, str(ROOT / "hooks" / "dispatch-session-sweep.py")],
                              input=payload, capture_output=True, text=True, timeout=60,
                              env={**os.environ, **self.env})
        self.assertEqual(done.returncode, 0, done.stderr)
        if not done.stdout.strip():
            return ""
        return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_twenty_notices_reach_the_model_over_several_prompts_none_acked_unseen(self):
        import dispatch_pending_delivery as pending
        tails = self.notify(20)
        seen, rounds = set(), []
        for _ in range(6):
            context = self.prompt()
            if not context:
                break
            self.assertLessEqual(len(context), 10_000)
            shown = {tail for tail, _did in tails if tail in context}
            rounds.append(len(shown))
            seen |= shown
            for tail, delivery_id in tails:
                state = pending.read(self.root, self.SID, delivery_id)["state"]
                if state == "acked":
                    self.assertIn(tail, seen, f"{tail} acked but never shown")
        self.assertEqual(len(seen), 20, rounds)
        self.assertGreater(len(rounds), 1)
        self.assertTrue(all(pending.read(self.root, self.SID, did)["state"] == "acked"
                            for _tail, did in tails))


    def stop(self) -> str:
        payload = json.dumps({"session_id": self.SID, "hook_event_name": "Stop",
                              "stop_hook_active": True})
        done = subprocess.run([sys.executable, str(ROOT / "hooks" / "peer-message-record.py"),
                               "stop"], input=payload, capture_output=True, text=True,
                              timeout=60, env={**os.environ, **self.env})
        self.assertEqual(done.returncode, 0, done.stderr)
        if not done.stdout.strip():
            return ""
        output = json.loads(done.stdout)
        self.assertEqual(output["decision"], "block")
        return output["reason"]

    def test_the_rest_of_a_burst_continues_the_same_turn_at_stop_without_a_prompt(self):
        # RV-PR1477 P2-1: one prompt, then only the turn's own Stop boundaries.
        import dispatch_pending_delivery as pending
        settings = json.loads((ROOT / "adapters" / "claude" / "settings.json").read_text())
        commands = [hook["command"] for group in settings["hooks"]["Stop"]
                    for hook in group["hooks"]]
        self.assertTrue(any("peer-message-record.py stop" in c for c in commands))
        tails = self.notify(20)
        first = self.prompt()
        rounds = [sum(tail in first for tail, _did in tails)]
        self.assertTrue(0 < rounds[0] < 20)
        seen = {tail for tail, _did in tails if tail in first}
        order = sorted(seen)
        for _ in range(6):
            reason = self.stop()
            if not reason:
                break
            self.assertLessEqual(len(reason), 10_000)
            shown = sorted(tail for tail, _did in tails if tail in reason)
            self.assertTrue(shown, reason[:200])
            rounds.append(len(shown))
            order.extend(shown)
            seen |= set(shown)
            for tail, delivery_id in tails:
                if pending.read(self.root, self.SID, delivery_id)["state"] == "acked":
                    self.assertIn(tail, seen, f"{tail} acked but never shown")
        self.assertEqual(self.stop(), "")  # nothing owed: the turn ends
        self.assertEqual(len(seen), 20, rounds)
        self.assertEqual(order, sorted(tail for tail, _did in tails))  # no reordering
        self.assertTrue(all(pending.read(self.root, self.SID, did)["state"] == "acked"
                            for _tail, did in tails))


class CodexPromptFitTest(FixtureHome):
    SID = "pg40-codex-thread"

    def test_receipts_fit_beside_the_other_parts_and_the_rest_is_released(self):
        import session_notice
        import dispatch_pending_delivery as pending
        hook = _load("codex_userprompt_pg40", ROOT / "adapters" / "codex" / "hooks"
                     / "userprompt-lifecycle.py")
        ids = []
        for i in range(20):
            record = session_notice.notify("codex", self.SID, key=f"hb-{i}", subject=f"burst {i}",
                                           text=f"HB-{i:02d} " + "z" * 520)
            self.assertIsNotNone(record)
            ids.append(record["delivery_id"])
        with mock.patch("dispatch_contract.dispatch_state_roots", return_value=[self.root]):
            batches, rendered = hook.native_queue_prompt_receipts(self.SID)
        self.assertEqual(sum(len(rs) for _root, rs in batches), 20)
        card = "c" * 3_000
        text, kept = hook.fit_prompt_receipts(self.SID, batches, [card], rendered=rendered)
        sweep = sys.modules["dispatch_session_sweep"]
        self.assertLessEqual(sweep.utf8_len(text) + 1 + len(card), sweep.CODEX_CONTEXT_BYTES)
        kept_ids = {r["delivery_id"] for _root, rs in kept for r in rs}
        self.assertTrue(0 < len(kept_ids) < 20)
        for delivery_id in ids:
            state = pending.read(self.root, self.SID, delivery_id)["state"]
            self.assertEqual(state, "claimed" if delivery_id in kept_ids else "pending")
            self.assertEqual(delivery_id in text, delivery_id in kept_ids)


    def test_no_floor_when_the_other_parts_leave_too_little_room(self):
        # RV-PR1477 P2-3b: other parts 9,500 B leave 99 B; nothing is kept.
        import session_notice
        import dispatch_pending_delivery as pending
        hook = _load("codex_userprompt_pg40_room", ROOT / "adapters" / "codex" / "hooks"
                     / "userprompt-lifecycle.py")
        record = session_notice.notify("codex", self.SID, key="no-room", subject="budget",
                                       text="BUDGET-HEAD " + "b" * 500 + " BUDGET-TAIL")
        self.assertIsNotNone(record)
        with mock.patch("dispatch_contract.dispatch_state_roots", return_value=[self.root]):
            batches, rendered = hook.native_queue_prompt_receipts(self.SID)
        for other in ("o" * 9_500, "o" * 9_700):
            text, kept = hook.fit_prompt_receipts(self.SID, batches, [other], rendered=rendered)
            self.assertEqual((text, kept), ("", []))
            self.assertEqual(pending.read(self.root, self.SID, record["delivery_id"])["state"],
                             "pending")


class ClaudePeerFitTest(unittest.TestCase):
    def setUp(self):
        self.peer = _load("peer_message_pg40", HERE / "peer-message.py")

    def test_bodies_past_the_limit_are_released_not_shown(self):
        claims = [{"ref": f"r{i}", "text": f"BODY-{i} " + "b" * 3_990} for i in range(3)]
        context = [row["text"] for row in claims]
        released = []
        with mock.patch.object(self.peer, "release_pending_herdr_claim",
                               side_effect=lambda row: released.append(row["ref"])):
            self.peer._fit_claude_context(claims, context)
        self.assertEqual([row["ref"] for row in claims], ["r0", "r1"])
        self.assertEqual(released, ["r2"])
        self.assertLessEqual(len("\n\n".join(context)), 9_500)

    def test_an_8000_character_report_is_shown_whole(self):
        claims = [{"ref": "r0", "text": "HEAD " + "c" * 7_990 + " TAIL"}]
        context = [claims[0]["text"]]
        self.peer._fit_claude_context(claims, context)
        self.assertEqual(context, [claims[0]["text"]])

    def test_one_body_over_the_limit_keeps_its_trailer_and_points_to_a_copy(self):
        with tempfile.TemporaryDirectory(prefix="pg40-peer-") as tmp:
            body = "HEAD " + "d" * 15_000 + " TAIL\n(peer-from: claude abc worker)"
            claims = [{"ref": "r0", "text": body}]
            context = [body]
            with mock.patch.object(self.peer, "_pending_dir", return_value=Path(tmp) / "pending"):
                self.peer._fit_claude_context(claims, context)
            self.assertLessEqual(len(context[0]), 9_500)
            self.assertTrue(context[0].endswith("(peer-from: claude abc worker)"))
            self.assertEqual(claims[0]["shown_text"], context[0])
            path = Path(context[0].split("full text: ", 1)[1].split("]", 1)[0])
            self.assertEqual(path.read_text(encoding="utf-8"), body)


    def test_one_body_over_the_limit_without_a_whole_copy_is_released(self):
        # RV-PR1477 P2-3a: the copy directory cannot be created.
        with tempfile.TemporaryDirectory(prefix="pg40-peer-blocked-") as tmp:
            (Path(tmp) / "delivery-text").write_text("not a directory")
            body = "PEER-HEAD " + "p" * 15_000 + " PEER-TAIL\n(peer-from: claude abc worker)"
            claims = [{"ref": "r0", "text": body}]
            context = [body, "other guidance"]
            released = []
            with mock.patch.object(self.peer, "_pending_dir", return_value=Path(tmp) / "pending"), \
                 mock.patch.object(self.peer, "release_pending_herdr_claim",
                                   side_effect=lambda row: released.append(row["ref"])):
                self.peer._fit_claude_context(claims, context)
            self.assertEqual((claims, released, context), ([], ["r0"], ["other guidance"]))


class ClaudePostToolFailureTest(unittest.TestCase):
    def test_failed_tool_end_delivers_peer_context_under_its_own_event(self):
        settings = json.loads((ROOT / "adapters" / "claude" / "settings.json").read_text())
        commands = [hook["command"] for group in settings["hooks"]["PostToolUseFailure"]
                    for hook in group["hooks"]]
        self.assertTrue(any("peer-message-record.py post-tool" in c for c in commands))
        record = _load("peer_message_record_pg41", ROOT / "hooks" / "peer-message-record.py")
        calls = []
        fake = mock.Mock()
        fake.emit_peer_context = lambda recipient, event, **kw: calls.append(event)
        payload = {"hook_event_name": "PostToolUseFailure", "session_id": "s1",
                   "tool_name": "SendMessage", "tool_input": {"to": "x", "message": "m"}}
        with mock.patch.object(record, "_read_stdin_json", return_value=payload), \
             mock.patch.object(record, "_is_helper_process", return_value=False), \
             mock.patch.object(record, "_peer_message_module", return_value=fake), \
             mock.patch.object(record, "handle_post_tool") as handle, \
             mock.patch.object(sys, "argv", ["peer-message-record.py", "post-tool"]), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            record.main()
        handle.assert_not_called()
        self.assertEqual(calls, ["PostToolUseFailure"])
        peer = _load("peer_message_pg41", HERE / "peer-message.py")
        self.assertIn("PostToolUseFailure", peer._NATIVE_BOUNDARY_EVENTS)


if __name__ == "__main__":
    unittest.main()
