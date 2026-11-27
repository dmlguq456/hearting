#!/usr/bin/env python3
"""Message display form: the decoration-free trailer, the visible forms and how a receiver finds the sealed record from them. Fixtures only."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import hashlib
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
fleet_peers = module("display_fleet_peers", "tools/fleet/collectors/peer_messages.py")
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


LONG_ID = "0100000a-0000-7000-8000-000000000012"
VISIBLE_NOISE = ("ref=", "sid=", "pane=", "HEARTING_RECEIPT", "peer-from", "runtime-from", "peer-steward")


def number_free(text):
    return not any(len(word.strip(".,:;()[]")) >= 32 and all(c in "0123456789abcdef-" for c in word.strip(".,:;()[]"))
                   for word in text.split())


class DisplayForms(DisplayFixture):
    """Step two: the person reads a short text, the agent still gets everything, and the
    receiver judges the sealed text exactly as it judges the old form."""

    def ready(self, recipient=None):
        pm.note_display_ready(recipient or self.recipient)

    def sealed(self, body="실제 본문 한 줄", sender=None, recipient=None, ready=True, **kwargs):
        recipient = recipient or self.recipient
        if ready:
            self.ready(recipient)
        text, ref = self.prepare(body, sender=sender, recipient=recipient, defer=True, **kwargs)
        row = pm._read_pending(ref)
        return text, ref, row, pm.pasted_text(row)

    def test_sender_keeps_the_old_form_until_the_receiver_has_shown_it_reads_the_new_one(self):
        text, ref, row, shown = self.sealed(ready=False)
        self.assertEqual(shown, text)
        self.assertIn("(peer-from:", shown)
        self.assertNotIn("display", pm._read_transfer_record(ref))
        self.assertNotIn("display", row)
        # another session's sign does not count
        self.ready({"harness": "codex", "session_id": "someone-else"})
        _text, ref2, _row, shown2 = self.sealed("다른 본문", ready=False)
        self.assertIn("(peer-from:", shown2)
        # the sign of this very session does
        text3, ref3, row3, shown3 = self.sealed("셋째 본문")
        self.assertNotIn("peer-from", shown3)
        self.assertEqual(row3["text"], text3)  # the sealed text, and so every other carrier, is unchanged
        self.assertIn("(peer-from:", row3["text"])

    def test_visible_peer_text_is_the_body_and_the_fleet_tag_only(self):
        for name, expected_suffix in ((None, None), ("릴리즈 점검", " 릴리즈 점검")):
            with self.subTest(name=name):
                sender = dict(self.sender, name=name)
                if name is None:
                    sender.pop("name")
                with mock.patch.object(pm, "peer_alias", return_value="[d4]"):
                    _text, _ref, _row, shown = self.sealed("본문 첫 줄\n둘째 줄 " + str(name), sender=sender)
                self.assertEqual(shown, "본문 첫 줄\n둘째 줄 " + str(name) + "\n\n— [d4]" + (expected_suffix or ""))
                for noise in VISIBLE_NOISE:
                    self.assertNotIn(noise, shown)
                self.assertTrue(number_free(shown))
        # no Fleet tag: the harness stands in
        with mock.patch.object(pm, "peer_alias", return_value="[?]"):
            _text, _ref, _row, shown = self.sealed("본문", sender=dict(self.sender, name=None))
        self.assertEqual(shown, "본문\n\n— codex")

    def test_receiver_gives_the_same_answer_for_the_visible_and_the_old_text(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                recipient = {"harness": harness, "session_id": "recipient-" + harness}
                sender = dict(self.sender, harness=harness)
                text, ref, row, shown = self.sealed("본문 " + harness, sender=sender, recipient=recipient)
                self.assertNotEqual(shown, text)
                old = pm.parse_peer_trailer(text, recipient, include_ref=True)
                new = pm.parse_peer_trailer(shown, recipient, include_ref=True)
                self.assertEqual(new, old)
                self.assertEqual((new["session_id"], new["transfer_ref"]), (sender["session_id"], ref))
                self.assertEqual(pm.prompt_origin(shown, harness, recipient["session_id"])["transfer_ref"], ref)
                origin = pm.prompt_origin(shown, harness, recipient["session_id"])
                self.assertEqual(origin["request_digest"], hashlib.sha256(shown.encode()).hexdigest())
                self.assertEqual(pm.receive_peer_message(shown, recipient), 0)
                received = pm._read_pending(ref)
                self.assertEqual((received["state"], received["receipt"]), ("received", "exact-peer-ref"))
                notices = [r for r in pm._iter_records() if r.get("transfer_ref") == ref
                           and r.get("delivery", {}).get("status") == "received"]
                self.assertEqual(len(notices), 1)
                self.assertEqual(notices[0]["from"]["session_id"], sender["session_id"])

    def test_agent_only_context_carries_sender_reply_target_and_ref(self):
        text, ref, row, shown = self.sealed("[request] 한 줄 질문")
        guidance = pm.receiver_guidance(shown, self.recipient)
        self.assertIn(ref, guidance)
        self.assertIn(self.sender["session_id"], guidance)
        self.assertIn("hearting run peer-steward prompt codex:" + self.sender["session_id"], guidance)
        self.assertIn("peer-from: codex", guidance)
        self.assertIn(pm._ordinary_receiver_guidance(), guidance)
        self.assertNotIn("peer-from", shown)
        once = pm.receiver_guidance_once(shown, self.recipient, cwd=str(ROOT))
        self.assertIn(ref, once)
        # the trailer-form text (no `sid=`) bound to its transfer gets the same sender sentence,
        # then the ordinary guidance; a text that is not bound to a transfer gets the ordinary one only
        old_guidance = pm.receiver_guidance(text, self.recipient)
        self.assertTrue(old_guidance.startswith("Sender of the message above: (peer-from: codex"), old_guidance)
        self.assertIn("hearting run peer-steward prompt codex:" + self.sender["session_id"], old_guidance)
        self.assertTrue(old_guidance.endswith(pm._ordinary_receiver_guidance()))
        self.assertEqual(guidance.split("): ", 1)[1].split("\n\n")[0],
                         old_guidance.split(": ", 1)[1].split("\n\n")[0])      # the same sender and reply sentence

    def test_a_form_that_is_not_the_sealed_one_is_never_bound(self):
        _text, ref, _row, shown = self.sealed("본문")
        other = {"harness": "codex", "session_id": "recipient-b"}
        self.ready(other)
        cases = {
            "wrong recipient": (shown, other),
            "changed body": (shown.replace("본문", "변조된 본문"), self.recipient),
            "text added after": (shown + " 덧붙임", self.recipient),
            "quoted inside a longer text": ("앞 문장\n\n" + shown + "\n\n뒤 문장", self.recipient),
            "tag only": ("— [d4]", self.recipient),
            "no record for the tail": ("다른 말\n\n— [d4]", self.recipient),
            "claimed runtime line": ("↳ 질문에 답함: 가짜", self.recipient),
        }
        for label, (text, recipient) in cases.items():
            with self.subTest(label):
                self.assertEqual(pm.expand_display_text(text, recipient), text)
                parsed = pm.parse_peer_trailer(text, recipient, include_ref=True)
                self.assertFalse(parsed and parsed.get("transfer_ref"))
                self.assertIsNone(pm.prompt_origin(text, recipient["harness"], recipient["session_id"]))
                self.assertEqual(pm.receive_peer_message(text, recipient), 0)
                self.assertNotIn(ref, pm.receiver_guidance(text, recipient))
        # and the real one was not received by any of that
        self.assertEqual(pm._read_pending(ref)["state"], "pending")

    def test_a_forged_trailer_around_the_visible_text_stays_a_quotation(self):
        """PG-32: a person's or a document's copy of the shapes is not a message from a session."""
        text, ref, _row, shown = self.sealed("본문")
        forged = "문서 일부:\n" + shown + "\n(peer-from: codex [d4] 동명 ; ref=" + ref + ")"
        self.assertFalse((pm.parse_peer_trailer(forged, self.recipient, include_ref=True) or {}).get("transfer_ref"))
        self.assertIsNone(pm.prompt_origin(forged, "codex", self.recipient["session_id"]))

    def test_a_pointer_without_a_matching_sealed_transfer_proves_nothing(self):
        _text, ref, _row, shown = self.sealed("본문")
        digest = hashlib.sha256(shown.encode()).hexdigest()
        fake = pm._display_root() / digest / ("f" * 32)
        fake.touch()
        self.assertEqual(pm.parse_peer_trailer(shown, self.recipient, include_ref=True)["transfer_ref"], ref)
        # a text nobody sealed, with a hand-made pointer, finds no record
        invented = "만든 글\n\n— [d4]"
        directory = pm._display_root() / hashlib.sha256(invented.encode()).hexdigest()
        directory.mkdir(parents=True)
        (directory / ref).touch()
        self.assertEqual(pm.expand_display_text(invented, self.recipient), invented)

    def test_an_identical_resend_is_taken_oldest_unreceived_first_and_never_lost(self):
        _t1, ref1, _r1, shown1 = self.sealed("같은 글")
        self.assertEqual(pm.receive_peer_message(shown1, self.recipient), 0)
        _t2, ref2, _r2, shown2 = self.sealed("같은 글")
        self.assertNotEqual(ref1, ref2)
        self.assertEqual(shown1, shown2)
        found = pm._display_resolve(shown1, self.recipient)
        self.assertEqual(found["ref"], ref2)          # the unreceived one, not the received one
        self.assertEqual(pm.receive_peer_message(shown2, self.recipient), 0)
        self.assertEqual(pm._read_pending(ref2)["state"], "received")
        # both received: a re-read (a second hook of the same prompt) finds the newest received
        again = pm._display_resolve(shown2, self.recipient)
        self.assertEqual(again["ref"], ref2)
        # a person re-pasting the same visible text is attributed exactly as re-pasting the old text is
        self.assertEqual(pm.parse_peer_trailer(shown2, self.recipient, include_ref=True)["session_id"],
                         self.sender["session_id"])
        self.assertEqual(pm.receive_peer_message(shown2, self.recipient), 0)
        notices = [r for r in pm._iter_records() if r.get("delivery", {}).get("status") == "received"]
        self.assertEqual(len(notices), 2)             # the re-paste wrote no third notice

    def test_arrival_whitespace_and_a_paste_wrapper_do_not_hide_the_record(self):
        _text, ref, _row, shown = self.sealed("본문")
        for arrived in (shown + "\n", "\n" + shown + "  \n",
                        '<pasted_content id="p1">\n' + shown + '\n</pasted_content id="p1">'):
            with self.subTest(arrived=arrived[-20:]):
                self.assertEqual(pm.parse_peer_trailer(arrived, self.recipient, include_ref=True)["transfer_ref"], ref)

    def test_long_body_envelope_and_request_lines_use_the_same_tail(self):
        body = "한글 " * 1800
        evidence = {"schema": "fixture", "recipient": dict(self.recipient)}
        with mock.patch.object(pm, "_reference_access_evidence_valid", return_value=True):
            text, ref, row, shown = self.sealed(body, reference_evidence=evidence)
        self.assertIsNotNone(pm._read_transfer_record(ref).get("body_ref"))
        self.assertTrue(shown.startswith("Long peer message"))
        self.assertTrue(shown.rstrip().rsplit("\n", 1)[-1].startswith("— "))
        self.assertNotIn("ref=", shown)
        self.assertEqual(pm.parse_peer_trailer(shown, self.recipient, include_ref=True)["transfer_ref"], ref)
        self.assertEqual(pm.receive_peer_message(shown, self.recipient), 0)

    def test_the_other_trailer_shapes_a_hook_may_see_still_gate_correctly(self):
        shown = "본문\n\n— [d4] 이름"
        for text in (shown, "본문\n\n— codex", "↳ 작업 끝: 설계 — 완료 (3분)", "x (peer-from: codex [d4] ; ref=" + "a" * 32 + ")",
                     "x (runtime-from: runtime [?] ; ref=" + "a" * 32 + ")"):
            self.assertTrue(pm.looks_like_peer_text(text), text)
        for text in ("그냥 입력", "— 그냥 줄표", "본문 — [d4] 중간", "글\n— [zz]", ""):
            self.assertFalse(pm.looks_like_peer_text(text), text)

    def test_every_receiver_gate_is_the_same_pattern(self):
        import re as regex
        probes = ("본문\n\n— [d4]", "본문\n\n— [d4] 이름", "본문\n\n— opencode", "본문\n\n— [?]",
                  "↳ 질문에 답함: 네", "  ↳ 작업 끝", "그냥", "글\n— [zz]", "글\n— [d4] x\n뒤", "— 줄표",
                  "(peer-from: claude [d4] ; ref=" + "a" * 32 + ")", "a runtime-from: b")
        spec = importlib.util.spec_from_file_location("display_gate_codex_hook",
                                                      ROOT / "adapters/codex/hooks/userprompt-lifecycle.py")
        carrier = module("display_gate_turn_carrier", "utilities/codex_turn_carrier.py")
        source_of = {}
        for name, path in (("claude hook", "hooks/peer-message-record.py"),
                           ("codex hook", "adapters/codex/hooks/userprompt-lifecycle.py"),
                           ("turn carrier", "utilities/codex_turn_carrier.py")):
            source_of[name] = (ROOT / path).read_text(encoding="utf-8")
        patterns = {n: regex.findall(r'_PEER_DISPLAY_TAIL = re\.compile\(\s*r"([^\n]+)"\)', t)
                    for n, t in source_of.items()}
        self.assertEqual({n: len(v) for n, v in patterns.items()}, {n: 1 for n in patterns})
        self.assertEqual(len({v[0] for v in patterns.values()}), 1, patterns)
        for probe in probes:
            expected = pm.looks_like_peer_text(probe)
            self.assertEqual(carrier._looks_like_peer_prompt(probe), expected, probe)
        # the OpenCode plugin's pattern is the same text, and the title reader keeps the same shapes
        plugin = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text(encoding="utf-8")
        self.assertIn(r"/^— (?:\[(?:[0-9a-f]{2}|\?)\]|claude|codex|opencode)(?: [^\n]{1,80})?[ \t]*$/m", plugin)
        title = (ROOT / "tools/fleet/refresh_title.py").read_text(encoding="utf-8")
        self.assertIn(r'(?:\[(?:[0-9a-f]{2}|\?)\]|claude|codex|opencode)(?: [^\n]{1,80})?[ \t]*"', title)


class RuntimeDisplayForms(DisplayFixture):
    """The runtime's own texts: one `↳` line for the person, the whole sealed text for the agent."""

    def runtime_prompt(self, recipient, line, body="Hearting runtime delivery. Act on each required_action.\n- done (required_action=report-to-user)\nHEARTING_RECEIPT_V1 delivery-runtime-completion " + "b" * 64):
        pm.note_display_ready(recipient)
        sender = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}
        jobs = self.state / "jobs.log"
        jobs.touch()
        duty_id = "registered-batch-" + "a" * 32
        refs = [duty_id, "att-runtime-completion", "delivery-runtime-completion"]
        producer = {"schema": pm._RUNTIME_PRODUCER_SCHEMA, "duty_id": duty_id, "jobs": str(jobs),
                    "attempt_id": refs[1], "carrier": "codex-native-queue",
                    "delivery_id": refs[2], "receipt_digest": "b" * 64, "recipient": recipient,
                    "route_id": "rt-runtime", "route_node": "execute"}
        text, ref = pm.prepare_peer_message(body, sender, recipient, defer=True, refs=refs,
                                            producer_binding=producer, receipt="native-pending",
                                            display_line=line)
        return text, ref, producer, pm.pasted_text(pm._read_pending(ref))

    def test_codex_keeps_the_full_runtime_text(self):
        recipient = {"harness": "codex", "session_id": "runtime-codex-full"}
        text, ref, _producer, shown = self.runtime_prompt(recipient, "↳ 작업 끝: 설계 — 완료")
        self.assertEqual(shown, text)
        self.assertNotIn("display", pm._read_transfer_record(ref))

    def test_runtime_line_is_the_whole_visible_text_and_the_agent_context_is_the_old_text(self):
        for harness in ("claude", "opencode"):
            with self.subTest(harness=harness):
                recipient = {"harness": harness, "session_id": "runtime-" + harness}
                line = "↳ 작업 끝: 설계 확정 — 완료 (3분)"
                text, ref, producer, shown = self.runtime_prompt(recipient, line)
                self.assertEqual(shown, line)
                self.assertIn("(runtime-from: runtime [?] Hearting runtime ; ref=" + ref + ")", text)
                self.assertEqual(pm.expand_display_text(shown, recipient), text)
                parsed = pm.parse_peer_trailer(shown, recipient, include_ref=True)
                self.assertEqual((parsed["source"], parsed["transfer_ref"]), ("runtime-completion", ref))
                guidance = pm.receiver_guidance(shown, recipient)
                self.assertIn(text, guidance)             # nothing the agent had is lost
                self.assertIn("delivery-runtime-completion", guidance)
                self.assertIn(ref, guidance)
                origin = pm.prompt_origin(shown, harness, recipient["session_id"])
                self.assertEqual((origin["kind"], origin["producer_binding"]), ("runtime-completion", producer))
                # for another session, or a changed line, nothing is bound
                self.assertIsNone(pm.prompt_origin(shown + "x", harness, recipient["session_id"]))
                self.assertIsNone(pm.prompt_origin(shown, harness, "someone-else"))
                self.assertEqual(pm.receive_peer_message(shown + "x", recipient), 0)
                self.assertEqual(pm._read_pending(ref)["state"], "pending")
                self.assertEqual(pm.receive_peer_message(shown, recipient), 0)
                received = pm._read_pending(ref)
                self.assertEqual((received["state"], received["receipt"]), ("received", "exact-peer-ref"))
                self.assertEqual(received["producer_binding"], producer)
                # consumed once: the same prompt read again is still that record, no second notice
                self.assertEqual(pm.receive_peer_message(shown, recipient), 0)
                notices = [r for r in pm._iter_records() if r.get("transfer_ref") == ref
                           and r.get("delivery", {}).get("status") == "received"]
                self.assertEqual(len(notices), 1)

    def test_two_unreceived_runtime_records_with_the_same_line_the_second_is_the_full_text(self):
        recipient = {"harness": "claude", "session_id": "runtime-same-line"}
        line = "↳ 질문에 답함: 네"
        text1, ref1, producer1, shown1 = self.runtime_prompt(recipient, line, body="[notice] 첫 질문 " + "q" * 5)
        text2, ref2, producer2, shown2 = self.runtime_prompt(recipient, line, body="[notice] 둘째 질문 " + "r" * 5)
        self.assertEqual(shown1, line)
        self.assertEqual(shown2, text2)                       # the second keeps the full text
        self.assertNotIn("display", pm._read_transfer_record(ref2))
        self.assertNotIn("display", pm._read_pending(ref2))
        self.assertIn("display", pm._read_pending(ref1))
        # each arrival is its own record and the agent's context is that record's text
        for shown, ref, text in ((shown2, ref2, text2), (shown1, ref1, text1)):
            resolved = pm._display_resolve(shown, recipient)
            self.assertEqual((resolved or {"ref": ref})["ref"], ref)
            self.assertEqual(pm.receive_peer_message(shown, recipient), 0)
            self.assertEqual(pm._read_pending(ref)["state"], "received")
            self.assertIn(text, pm.receiver_guidance(shown, recipient) if shown == line else text)
            if shown == line:
                self.assertIn("ref=" + ref, pm.receiver_guidance(shown, recipient))
        # the first line is received: the same line is a display form again
        text3, ref3, _p3, shown3 = self.runtime_prompt(recipient, line, body="[notice] 셋째 질문 " + "s" * 5)
        self.assertEqual(shown3, line)

    def test_a_runtime_line_without_the_receivers_sign_is_the_old_text(self):
        recipient = {"harness": "claude", "session_id": "runtime-unsigned"}
        sender = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}
        duty_id = "registered-batch-" + "a" * 32
        refs = [duty_id, "att-x", "delivery-x"]
        producer = {"schema": pm._RUNTIME_PRODUCER_SCHEMA, "duty_id": duty_id,
                    "jobs": str(self.state / "jobs.log"), "attempt_id": refs[1],
                    "carrier": "codex-native-queue", "delivery_id": refs[2],
                    "receipt_digest": "b" * 64, "recipient": recipient,
                    "route_id": "rt", "route_node": "execute"}
        (self.state / "jobs.log").touch()
        text, ref = pm.prepare_peer_message("[notice] x", sender, recipient, defer=True, refs=refs,
                                            producer_binding=producer, display_line="↳ 작업 끝: x — 완료")
        row = pm._read_pending(ref)
        self.assertEqual(pm.pasted_text(row), text)
        self.assertNotIn("display", pm._read_transfer_record(ref))

    def test_a_line_that_is_not_one_clean_line_is_refused_as_a_display_form(self):
        recipient = {"harness": "claude", "session_id": "runtime-lines"}
        for bad in ("작업 끝", "↳ 두\n줄", "↳ " + "긴" * 500, "↳ 제어\x07문자"):
            with self.subTest(bad=bad[:10]):
                text, ref, _producer, shown = self.runtime_prompt(recipient, bad, body="[notice] " + str(len(bad)))
                self.assertEqual(shown, text)
                self.assertNotIn("display", pm._read_transfer_record(ref))



class ReceivingHarnesses(DisplayFixture):
    """The three receiving layers: each reads a form-2 text, records the receipt as for an old
    one, and puts the agent-only context where its harness carries it. Fixtures only."""

    def sealed(self, harness, body="하네스 본문"):
        recipient = {"harness": harness, "session_id": "recipient-" + harness}
        pm.note_display_ready(recipient)
        sender = dict(self.sender, harness="claude" if harness != "claude" else "codex")
        text, ref = pm.prepare_peer_message(body, sender, recipient, defer=True)
        return recipient, text, ref, pm.pasted_text(pm._read_pending(ref)), sender

    def hook_env(self):
        env = dict(os.environ)
        env.update({"AGENT_PEER_LEDGER_ROOT": str(self.state), "AGENT_DISPATCH_JOBS": str(self.state / "jobs.log"),
                    "HOME": str(self.state / "home"), "PYTHONDONTWRITEBYTECODE": "1"})
        env.pop("AGENT_HOME", None)
        return env

    def test_claude_prompt_hook_records_the_receipt_and_adds_the_agent_context(self):
        recipient, text, ref, shown, sender = self.sealed("claude")
        payload = {"session_id": recipient["session_id"], "cwd": str(self.state), "prompt": shown,
                   "hook_event_name": "UserPromptSubmit"}
        proc = subprocess.run([sys.executable, str(ROOT / "hooks/peer-message-record.py"), "prompt"],
                              input=json.dumps(payload), text=True, capture_output=True, timeout=30,
                              env=self.hook_env(), cwd=str(self.state))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        for needed in (ref, sender["session_id"], "peer-steward prompt codex:" + sender["session_id"],
                       "peer-from: codex"):
            self.assertIn(needed, context)
        self.assertEqual(pm._read_pending(ref)["state"], "received")
        for noise in ("ref=", "sid=", "pane="):
            self.assertNotIn(noise, shown)

    def test_claude_prompt_hook_leaves_a_person_who_quotes_the_shapes_alone(self):
        recipient, _text, ref, shown, _sender = self.sealed("claude")
        quoted = "이 글이 뭐야?\n\n" + shown + "\n\n— [d4]"
        proc = subprocess.run([sys.executable, str(ROOT / "hooks/peer-message-record.py"), "prompt"],
                              input=json.dumps({"session_id": recipient["session_id"], "cwd": str(self.state),
                                                "prompt": quoted}),
                              text=True, capture_output=True, timeout=30, env=self.hook_env(), cwd=str(self.state))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # nothing the quoted prompt was taken for: no sender block for "the message above"
        self.assertNotIn("Sender of the message above", proc.stdout)
        self.assertEqual([r for r in pm._iter_records() if r.get("delivery", {}).get("receipt") == "exact-peer-ref"
                          and r.get("transfer_ref") == ref and r.get("surface") == "herdr"], [])

    def load_codex_hook(self):
        spec = importlib.util.spec_from_file_location("display_codex_hook",
                                                      ROOT / "adapters/codex/hooks/userprompt-lifecycle.py")
        hook = importlib.util.module_from_spec(spec)
        sys.modules["display_codex_hook"] = hook
        spec.loader.exec_module(hook)
        return hook

    def test_codex_hook_receives_a_visible_text_and_its_guidance_carries_the_agent_context(self):
        recipient, text, ref, shown, sender = self.sealed("codex")
        hook = self.load_codex_hook()
        self.assertTrue(hook.looks_like_peer_prompt(shown))
        self.assertFalse(hook.looks_like_peer_prompt("그냥 입력"))
        with mock.patch.object(hook, "interaction_session_id", return_value=recipient["session_id"]), \
                mock.patch.object(hook, "ROOT", ROOT):
            hook.peer_notice({"session_id": recipient["session_id"]}, shown, str(self.state))
        self.assertEqual(pm._read_pending(ref)["state"], "received")
        guidance = pm.receiver_guidance_once(shown, recipient, cwd=str(self.state))
        for needed in (ref, sender["session_id"], "peer-steward prompt claude:" + sender["session_id"]):
            self.assertIn(needed, guidance)

    def test_opencode_plugin_receives_a_visible_text_and_its_system_context_carries_the_agent_context(self):
        recipient, text, ref, shown, sender = self.sealed("opencode")
        js = r'''
const fs = require('fs'), vm = require('vm'), path = require('path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.root + '/adapters/opencode/plugins/hearting-guards.js', 'utf8');
const fn = source.slice(source.indexOf('function spawnPeerNotice('), source.indexOf('\nfunction promptText('));
let result = {};
vm.runInNewContext(fn + '\nresult.gate = looksLikePeerPrompt(prompt); result.plain = looksLikePeerPrompt("그냥 입력"); spawnPeerNotice(sid, prompt, root)', {
  path, root: input.root, sid: input.sid, prompt: input.prompt, process: {env: {}}, result,
  spawn: (exe, args) => { result.exe = exe; result.args = args; return {
    on: () => {}, unref: () => {}, stdin: {end: body => {result.body = body;}}
  }; }
});
process.stdout.write(JSON.stringify(result));
'''
        proc = subprocess.run(["node", "-e", js], input=json.dumps(
            {"root": str(ROOT), "sid": recipient["session_id"], "prompt": shown}),
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        call = json.loads(proc.stdout)
        self.assertEqual((call["gate"], call["plain"]), (True, False))
        self.assertEqual(call["body"], shown)
        received = subprocess.run([call["exe"], *call["args"]], input=call["body"], text=True,
                                  capture_output=True, timeout=30, env=self.hook_env())
        self.assertEqual(received.returncode, 0, received.stderr)
        self.assertEqual(pm._read_pending(ref)["state"], "received")
        # the plugin's own system-context command (copied from its prompt callback)
        command = ("import importlib.util, json, sys; s=importlib.util.spec_from_file_location('peer', 'utilities/peer-message.py'); "
                   "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); d=json.load(sys.stdin); "
                   "print(json.dumps(m.receiver_guidance_parts(d.get('prompt', ''), d.get('recipient'), cwd=d.get('cwd'))))")
        guidance = subprocess.run([sys.executable, "-c", command], cwd=str(ROOT), env=dict(self.hook_env(), AGENT_HOME=str(ROOT)),
                                  input=json.dumps({"prompt": shown, "recipient": recipient, "cwd": str(self.state)}),
                                  text=True, capture_output=True, timeout=30)
        self.assertEqual(guidance.returncode, 0, guidance.stderr)
        for needed in (ref, sender["session_id"], "peer-steward prompt claude:" + sender["session_id"]):
            self.assertIn(needed, guidance.stdout)

    def test_the_receiving_code_leaves_the_sign_the_sender_looks_for(self):
        # a hook script is the release's own file; the OpenCode plugin says its gate reads the forms
        with mock.patch.dict(os.environ, {"HEARTING_PEER_DISPLAY_GATE": "2"}):
            for harness in ("claude", "codex", "opencode"):
                recipient = {"harness": harness, "session_id": "sign-" + harness}
                self.assertFalse(pm.receiver_reads_display(recipient))
                pm.emit_peer_context(recipient, None)
                self.assertTrue(pm.receiver_reads_display(recipient), harness)
            pm.cmd_pending(SimpleNamespace(to_harness="opencode", to_session_id="sign-pending", received=None,
                                           claim=None, queued=None, resuming=None, reserve=None,
                                           begin_write=None, release_reservation=None, claim_pid=None, claim_start=None))
            self.assertTrue(pm.receiver_reads_display({"harness": "opencode", "session_id": "sign-pending"}))

    def test_a_plugin_that_does_not_say_its_gate_reads_the_forms_leaves_no_sign(self):
        """`AGENT_HOME` may name a `current` link: a plugin loaded before the forms existed (old gate)
        can run beside a release whose Python is new. Only a plugin that says so leaves the sign."""
        os.environ.pop("HEARTING_PEER_DISPLAY_GATE", None)
        for harness, expected in (("claude", True), ("codex", True), ("opencode", False)):
            recipient = {"harness": harness, "session_id": "unsaid-" + harness}
            pm.emit_peer_context(recipient, None)
            self.assertEqual(pm.receiver_reads_display(recipient), expected, harness)
        pending = SimpleNamespace(to_harness="opencode", to_session_id="unsaid-pending", received=None,
                                  claim=None, queued=None, resuming=None, reserve=None,
                                  begin_write=None, release_reservation=None, claim_pid=None, claim_start=None)
        pm.cmd_pending(pending)
        self.assertFalse(pm.receiver_reads_display({"harness": "opencode", "session_id": "unsaid-pending"}))
        # so the sender keeps the old form for that window
        recipient = {"harness": "opencode", "session_id": "unsaid-pending"}
        _text, ref = pm.prepare_peer_message("옛 플러그인", self.sender, recipient, defer=True)
        self.assertIn("(peer-from:", pm.pasted_text(pm._read_pending(ref)))
        with mock.patch.dict(os.environ, {"HEARTING_PEER_DISPLAY_GATE": "1"}):
            pm.cmd_pending(pending)
        self.assertFalse(pm.receiver_reads_display(recipient))          # only the form-2 declaration counts

    def test_the_plugin_says_so_on_every_command_that_leaves_the_sign_and_not_on_receive(self):
        js = r'''
const fs = require('fs'), vm = require('vm'), path = require('path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.root + '/adapters/opencode/plugins/hearting-guards.js', 'utf8');
const fn = source.slice(source.indexOf('function spawnPeerNotice('), source.indexOf('\nfunction pendingPeerProcessIdentity('));
const seen = {};
const child = () => ({on: () => {}, unref: () => {}, kill: () => {}, stdin: {end: () => {}}, stdout: {on: () => {}}});
const context = {path, root: input.root, process: {env: {}}, seen, setTimeout, clearTimeout, Promise,
  spawnSync: (exe, args, options) => { (seen[args[1]] = seen[args[1]] || []).push(options.env.HEARTING_PEER_DISPLAY_GATE || null); return {status: 1}; },
  spawn: (exe, args, options) => { (seen[args[1]] = seen[args[1]] || []).push(options.env.HEARTING_PEER_DISPLAY_GATE || null); return child(); }};
vm.runInNewContext(fn + `
spawnPeerNotice("s", "본문\\n\\n— [d4]", "/tmp");
pendingPeerCommand("s"); pendingPeerTransition("s", []);
pendingPeerSpawn("s", [], 5); peerToolContextAsync("s", 5, 5);`, context);
setTimeout(() => process.stdout.write(JSON.stringify(seen)), 100);
'''
        proc = subprocess.run(["node", "-e", js], input=json.dumps({"root": str(ROOT)}),
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        seen = json.loads(proc.stdout)
        self.assertEqual(seen.get("receive"), [None])                       # receiving a text leaves no sign
        self.assertEqual(seen.get("pending"), ["2", "2", "2"])
        self.assertEqual(seen.get("context"), ["2"])


class RuntimeLines(unittest.TestCase):
    """The producer's one-line forms: every word comes from the same records the full text does."""

    @classmethod
    def setUpClass(cls):
        cls.intent = module("display_work_intent", "utilities/work_intent.py")
        cls.sweep = module("display_sweep", "utilities/dispatch_session_sweep.py")

    KO = ("[완료 요약] autopilot-code · route rt-demo\n의도: 받는 창에 찍히는 글을 줄인다 (원문 \"글 줄이기\")\n"
          "결과: 끝남 — 실패·못 한 것 있음(아래)\n비용: 걸린 시간 12m 30s · 토큰 1,000\n변경: a.py")
    EN = ("[Completion summary] autopilot-code · route rt-demo\nIntent: Trim the text shown in the window\n"
          "Result: Done\nCost: 1h 05m elapsed · tokens unknown\nChanged: a.py")

    def t(self, lang):
        ui = self.intent._ui_text()
        return ui.translator(lang)

    def test_a_korean_summary_becomes_one_korean_line(self):
        self.assertEqual(self.intent.completion_line(self.KO, self.t("ko")),
                         "↳ 작업 끝: 받는 창에 찍히는 글을 줄인다 — 끝남 — 실패·못 한 것 있음 (12분 30초)")

    def test_an_english_summary_becomes_one_english_line_and_the_line_follows_the_window_language(self):
        self.assertEqual(self.intent.completion_line(self.EN, self.t("en")),
                         "↳ Work done: Trim the text shown in the window — Done (1h 05m)")
        self.assertEqual(self.intent.completion_line(self.EN, self.t("ko")),
                         "↳ 작업 끝: Trim the text shown in the window — Done (1시간 05분)")

    def test_unknown_time_is_left_out_and_an_unreadable_summary_has_no_line(self):
        summary = self.KO.replace("걸린 시간 12m 30s", "걸린 시간 " + self.intent._ui_text().text("completion.cost_unknown", "ko"))
        self.assertEqual(self.intent.completion_line(summary, self.t("ko")),
                         "↳ 작업 끝: 받는 창에 찍히는 글을 줄인다 — 끝남 — 실패·못 한 것 있음")
        for bad in ("그냥 글", "[완료 요약] autopilot-code\n비용: 걸린 시간 1s · x", ""):
            self.assertIsNone(self.intent.completion_line(bad, self.t("ko")))

    def test_summaries_inside_one_followup_are_split_one_each(self):
        text = "Owner finished; no harvest.\n" + self.KO + "\n" + self.EN + "\nnext"
        parts = self.intent.split_completion_summaries(text)
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[0].startswith("[완료 요약]") and parts[1].startswith("[Completion summary]"))

    def record(self, **receipt):
        return {"delivery_id": "delivery-x", "receipt": receipt}

    def test_the_delivery_line_is_built_only_when_every_record_has_a_short_form(self):
        sweep, intent = self.sweep, self.intent
        answer = self.record(kind="notice", subject="Question answer", text="private-answer",
                             answer_binding={"request_id": "S4:" + "a" * 64})
        work = self.record(children=[{"attempt_id": "att-1"}])
        gate = {"delivery_id": "d", "receipt": {"kind": "human-gate"}}
        failure = self.record(children=[{"attempt_id": "att-2"}])
        # A fake answer_binding cannot be read, so the sweep hides it; these tests check the line's shape and use a stand-in (the real path is in peer_display_answer.test.py).
        with mock.patch("work_intent.ui_translator", return_value=self.t("ko")), \
                mock.patch.object(sweep, "_followup", side_effect=lambda root, record: (
                "Owner finished; no harvest.\n" + self.KO if record is work else "no summary here")), \
                mock.patch.object(sweep, "_answered_question_secret", return_value=False), \
                mock.patch("human_wait_state.message_answer_body", return_value="네, 그렇게 하세요\n그리고 하나 더") as body:
            line = sweep.delivery_display_text([(Path("/x"), [work, answer])])
            self.assertEqual(line.split("\n")[0].split(":")[0], "↳ 질문에 답함")
            self.assertIn("네, 그렇게 하세요", line.split("\n")[0])
            self.assertIn("(+1줄)", line.split("\n")[0])
            self.assertTrue(line.split("\n")[1].startswith("↳ 작업 끝: "))
            self.assertEqual(len(line.split("\n")), 2)
            for batch in ([gate], [work, gate], [failure], [work, failure],
                          [self.record(kind="supervision")], [self.record(kind="notice", subject="x", text="y")]):
                self.assertIsNone(sweep.delivery_display_text([(Path("/x"), batch)]), batch)
            self.assertIsNone(sweep.delivery_display_text([]))
        self.assertTrue(body.called)

    def test_equal_lines_of_different_records_are_counted_and_one_record_listed_twice_is_one_line(self):
        sweep = self.sweep

        def answer(delivery_id):
            return {"delivery_id": delivery_id, "receipt": {"kind": "notice", "subject": "Question answer",
                    "text": "x", "answer_binding": {"request_id": "S4:" + "a" * 64}}}
        first, second = answer("delivery-1"), answer("delivery-2")
        # A fake answer_binding cannot be read, so the sweep hides it; these tests check the line's shape and use a stand-in (the real path is in peer_display_answer.test.py).
        with mock.patch("work_intent.ui_translator", return_value=self.t("ko")), \
                mock.patch.object(sweep, "_answered_question_secret", return_value=False), \
                mock.patch("human_wait_state.message_answer_body", return_value="네"):
            self.assertEqual(sweep.delivery_display_text([(Path("/x"), [first, second])]), "↳ 질문에 답함: 네 ×2")
            self.assertEqual(sweep.delivery_display_text([(Path("/x"), [first, answer("delivery-3"), second])]),
                             "↳ 질문에 답함: 네 ×3")
            # the same record twice (here and in another batch of the same root) is still one line
            self.assertEqual(sweep.delivery_display_text([(Path("/x"), [first]), (Path("/x"), [first])]),
                             "↳ 질문에 답함: 네")
            self.assertEqual(sweep.delivery_display_text([(Path("/x"), [first, first])]), "↳ 질문에 답함: 네")
            # the same delivery id under another root is another record
            self.assertEqual(sweep.delivery_display_text([(Path("/x"), [first]), (Path("/y"), [first])]),
                             "↳ 질문에 답함: 네 ×2")
        # records with no delivery id cannot be told apart: they are never merged
        bare1, bare2 = answer("x"), answer("x")
        bare1.pop("delivery_id"), bare2.pop("delivery_id")
        # A fake answer_binding cannot be read, so the sweep hides it; these tests check the line's shape and use a stand-in (the real path is in peer_display_answer.test.py).
        with mock.patch("work_intent.ui_translator", return_value=self.t("ko")), \
                mock.patch.object(sweep, "_answered_question_secret", return_value=False), \
                mock.patch("human_wait_state.message_answer_body", return_value="네"):
            self.assertEqual(sweep.delivery_display_text([(Path("/x"), [bare1, bare2])]), "↳ 질문에 답함: 네 ×2")
        # a work line shared by two finished works counts the same way, after the answers
        work1, work2 = {"delivery_id": "delivery-w1", "receipt": {}}, {"delivery_id": "delivery-w2", "receipt": {}}
        # A fake answer_binding cannot be read, so the sweep hides it; these tests check the line's shape and use a stand-in (the real path is in peer_display_answer.test.py).
        with mock.patch("work_intent.ui_translator", return_value=self.t("ko")), \
                mock.patch.object(sweep, "_followup", return_value="Owner finished; no harvest.\n" + self.KO), \
                mock.patch.object(sweep, "_answered_question_secret", return_value=False), \
                mock.patch("human_wait_state.message_answer_body", return_value="네"):
            lines = sweep.delivery_display_text([(Path("/x"), [work1, first, work2])]).split("\n")
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], "↳ 질문에 답함: 네")
            self.assertTrue(lines[1].startswith("↳ 작업 끝: ") and lines[1].endswith(" ×2"))


class SameVisibleText(DisplayFixture):
    """One visible text can be sealed for several messages (two untagged senders of one harness
    send the same body; two questions get the same short answer). A recipient holds at most one
    unreceived message per visible text, so the arrival and the agent's guidance name one record."""

    S1 = {"harness": "claude", "session_id": "0100000a-0000-7000-8000-0000000000a1"}
    S2 = {"harness": "claude", "session_id": "0100000a-0000-7000-8000-0000000000a2"}
    S3 = {"harness": "claude", "session_id": "0100000a-0000-7000-8000-0000000000a3"}

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(pm, "peer_alias", return_value="[?]")  # no Fleet tag: `— claude`
        patcher.start()
        self.addCleanup(patcher.stop)
        pm.note_display_ready(self.recipient)

    def seal(self, sender, body="작업 끝났습니다"):
        text, ref = pm.prepare_peer_message(body, sender, self.recipient, defer=True)
        row = pm._read_pending(ref)
        return text, ref, row, pm.pasted_text(row)

    def pointers(self, shown):
        digest = hashlib.sha256(shown.encode("utf-8")).hexdigest()
        try:
            return sorted(os.listdir(pm._display_root() / digest))
        except OSError:
            return []

    def arrive(self, shown, expected_ref, expected_sender):
        """One arrival as a hook sees it: receive, then the agent's guidance; both name one record."""
        resolved = pm._display_resolve(shown, self.recipient)
        picked_by_receive = (resolved or {}).get("ref")
        self.assertEqual(pm.receive_peer_message(shown, self.recipient), 0)
        trailer = pm.parse_peer_trailer(shown, self.recipient, include_ref=True)
        self.assertEqual((trailer["transfer_ref"], trailer["session_id"]),
                         (expected_ref, expected_sender["session_id"]))
        self.assertEqual(pm._read_pending(expected_ref)["state"], "received")
        guidance = pm.receiver_guidance(shown, self.recipient)
        if "peer-from:" in shown:     # the old form carries its sender in the text itself
            self.assertIn("ref=" + expected_ref, shown)
        else:                          # the display form: the agent-only block names the sender and ref
            self.assertIn("%s:%s" % (expected_sender["harness"], expected_sender["session_id"]), guidance)
            self.assertIn("ref=" + expected_ref, guidance)
        after = pm._display_resolve(shown, self.recipient)
        self.assertEqual((after or {}).get("ref") or expected_ref, expected_ref)
        if resolved is not None:
            self.assertEqual(picked_by_receive, expected_ref)   # the record the receive took is the guided one
            self.assertEqual(after["ref"], expected_ref)
        return guidance

    def test_a_second_unreceived_message_with_the_same_visible_text_is_sealed_in_the_old_form(self):
        text1, ref1, row1, shown1 = self.seal(self.S1)
        text2, ref2, row2, shown2 = self.seal(self.S2)
        self.assertEqual(shown1, "작업 끝났습니다\n\n— claude")
        self.assertIn("display", pm._read_transfer_record(ref1))
        self.assertIn("display", row1)
        # the second is the old form: its ref shows, no display seal, no pointer, row and transfer agree
        self.assertEqual(shown2, text2)
        self.assertIn("ref=" + ref2, shown2)
        self.assertNotIn("display", pm._read_transfer_record(ref2))
        self.assertNotIn("display", row2)
        self.assertEqual(self.pointers(shown1), [ref1])
        # each arrival binds its own record and the agent is told to answer its own sender
        guidance2 = self.arrive(shown2, ref2, self.S2)
        self.assertNotIn(self.S1["session_id"], guidance2)
        guidance1 = self.arrive(shown1, ref1, self.S1)
        self.assertNotIn(self.S2["session_id"], guidance1)

    def edge_pair(self, first_body, second_body):
        return [self.seal(self.S1, body=first_body), self.seal(self.S2, body=second_body)]

    def test_texts_that_differ_only_in_edge_whitespace_bind_each_arrival_to_its_own_record(self):
        # the receiver tries the arrival's raw, rstripped and stripped forms, so a fingerprint of a
        # text with edge whitespace could be hit by its trimmed twin's arrival (and the reverse)
        for order in (("검토 바랍니다", " 검토 바랍니다"), (" 검토 바랍니다", "검토 바랍니다")):
            with self.subTest(order=order):
                (_t1, ref1, row1, shown1), (_t2, ref2, row2, shown2) = self.edge_pair(*order)
                trimmed = (ref1, row1, shown1) if order[0] == order[0].strip() else (ref2, row2, shown2)
                spaced = (ref2, row2, shown2) if trimmed[0] == ref1 else (ref1, row1, shown1)
                # the trimmed text is the display form, the one with edge whitespace the old form
                self.assertEqual(trimmed[2], "검토 바랍니다\n\n— claude")
                self.assertIn("display", trimmed[1])
                self.assertNotIn("display", spaced[1])
                self.assertIn("ref=" + spaced[0], spaced[2])
                self.assertIn(trimmed[0], self.pointers(trimmed[2]))
                self.assertNotIn(spaced[0], self.pointers(trimmed[2]))
                # arrivals: the display one, and the old one in each whitespace variation a window gives
                for arrival in (" " + trimmed[2], trimmed[2] + "\n", "  " + trimmed[2] + "\n\n"):
                    self.assertEqual(pm._display_resolve(arrival, self.recipient)["ref"], trimmed[0])
                self.assertIsNone(pm._display_resolve(spaced[2], self.recipient))   # the old form is read by its ref
                sender_of = {ref1: self.S1, ref2: self.S2}
                self.arrive(spaced[2], spaced[0], sender_of[spaced[0]])
                self.assertEqual(pm._read_pending(trimmed[0])["state"], "pending")
                self.arrive(trimmed[2], trimmed[0], sender_of[trimmed[0]])

    def test_the_text_with_edge_whitespace_stays_in_the_old_form_even_when_alone(self):
        for body in (" 검토 바랍니다", "  \t검토 바랍니다", "\n검토 바랍니다"):
            _t, ref, row, shown = self.seal(self.S1, body=body)
            self.assertNotIn("display", row)
            self.assertNotIn("display", pm._read_transfer_record(ref))
            self.assertEqual(shown, _t)
            self.assertEqual(self.pointers(shown), [])
        # a runtime line with edge whitespace is not sealed in the display form either
        sender = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}
        for line in (" ↳ 질문에 답함: 네", "↳ 질문에 답함: 네 "):
            self.assertIsNone(pm._display_entry("runtime", "full text", None, sender, line=line))
        self.assertIsNotNone(pm._display_entry("runtime", "full text", None, sender, line="↳ 질문에 답함: 네"))

    def test_a_text_without_edge_whitespace_keeps_its_visible_form(self):
        _t, ref, row, shown = self.seal(self.S1, body="[요청] 변경 범위를 확인해 주세요.\n자세한 내용은 /tmp/notes/scope.md 에 있습니다. (2)")
        self.assertEqual(shown, "[요청] 변경 범위를 확인해 주세요.\n자세한 내용은 /tmp/notes/scope.md 에 있습니다. (2)\n\n— claude")
        self.assertIn("display", row)

    def test_a_trailer_form_text_bound_to_its_transfer_carries_the_senders_reply_address(self):
        _t1, ref1, _r1, shown1 = self.seal(self.S1)
        _t2, ref2, row2, shown2 = self.seal(self.S2)             # overlap: the second is the trailer form
        self.assertNotIn("display", row2)
        guidance2 = pm.receiver_guidance(shown2, self.recipient)
        reply2 = "Reply with `hearting run peer-steward prompt claude:%s --body-file <file>`." % self.S2["session_id"]
        trailer2 = shown2[shown2.index("(peer-from:"):]
        self.assertTrue(guidance2.startswith("Sender of the message above: %s. %s" % (trailer2, reply2)), guidance2)
        self.assertTrue(guidance2.endswith(pm._ordinary_receiver_guidance()))
        self.assertNotIn(self.S1["session_id"], guidance2)
        # the display form's sentence has the same shape (only its lead differs), written once
        guidance1 = pm.receiver_guidance(shown1, self.recipient)
        self.assertEqual(guidance1.count("Sender of the message above"), 1)
        self.assertRegex(guidance1, r"^Sender of the message above \(the person sees only `[^`]+`\): \(peer-from: [^)]*\)\. "
                                    r"Reply with `hearting run peer-steward prompt claude:%s --body-file <file>`\." % self.S1["session_id"])
        # the shown text itself is the same as before
        self.assertEqual(shown2, _t2)
        # a text bound to no record, or quoting another session's record, gets no sender sentence
        other = {"harness": "claude", "session_id": "someone-else"}
        self.assertEqual(pm.receiver_guidance(shown2, other), pm._ordinary_receiver_guidance())
        self.assertEqual(pm.receiver_guidance("hello", self.recipient), pm._ordinary_receiver_guidance())

    def test_the_arrival_order_does_not_matter(self):
        _t1, ref1, _r1, shown1 = self.seal(self.S1)
        _t2, ref2, _r2, shown2 = self.seal(self.S2)
        self.arrive(shown1, ref1, self.S1)
        self.assertEqual(pm._read_pending(ref2)["state"], "pending")
        self.arrive(shown2, ref2, self.S2)

    def test_once_the_first_is_received_the_same_text_is_sealed_in_the_display_form_again(self):
        _t1, ref1, _r1, shown1 = self.seal(self.S1)
        _t2, ref2, row2, shown2 = self.seal(self.S2)
        self.assertEqual(shown2, _t2)
        self.arrive(shown1, ref1, self.S1)
        _t3, ref3, row3, shown3 = self.seal(self.S3)
        self.assertEqual(shown3, shown1)             # an overlap once does not make the text long for good
        self.assertIn("display", row3)
        # ... but with that third still unreceived a fourth is old-form again
        _t4, ref4, row4, shown4 = self.seal(self.S1)
        self.assertEqual(shown4, _t4)
        self.assertNotIn("display", row4)
        self.assertNotEqual(len({ref1, ref2, ref3, ref4}), 1)

    def test_a_person_pasting_a_received_text_again_takes_the_waiting_record_once(self):
        """Supervisor judgement, kept: the visible text is the only key, so a text pasted again
        while a new record with the same visible text waits is that record's arrival."""
        _t1, ref1, _r1, shown1 = self.seal(self.S1)
        self.arrive(shown1, ref1, self.S1)
        _t2, ref2, _r2, shown2 = self.seal(self.S2)          # same visible text, sealed after the first was received
        self.assertEqual(shown2, shown1)
        self.assertEqual(pm._read_pending(ref2)["state"], "pending")
        # the person pastes the first message's text again
        self.assertEqual(pm._display_resolve(shown1, self.recipient)["ref"], ref2)
        self.assertEqual(pm.receive_peer_message(shown1, self.recipient), 0)
        self.assertEqual(pm._read_pending(ref2)["state"], "received")   # the record it stands for, this recipient's own
        def received_notices(ref):
            return [r for r in pm._iter_records() if r.get("transfer_ref") == ref
                    and r.get("delivery", {}).get("status") == "received"]
        self.assertEqual(len(received_notices(ref2)), 1)
        # closed once: the courier's own later arrival of that text and a third paste write nothing new
        for _again in range(2):
            self.assertEqual(pm.receive_peer_message(shown2, self.recipient), 0)
        self.assertEqual(len(received_notices(ref2)), 1)
        self.assertEqual(len(received_notices(ref1)), 1)

    def test_a_message_that_never_arrives_does_not_keep_the_text_long_for_good(self):
        _t1, ref1, row1, shown1 = self.seal(self.S1)
        # a row received and then dropped by the history window (a week on) is not waiting
        received = pm._read_pending(ref1)
        os.unlink(pm._pending_path(ref1))
        old = time.time() - 8 * 86400
        os.utime(pm._transfer_path(ref1), (old, old))
        self.assertIsNone(pm._read_pending(ref1))
        _t2, ref2, row2, shown2 = self.seal(self.S2)
        self.assertEqual(shown2, shown1)
        self.assertIn("display", row2)
        # and the old record cannot take the new one's arrival
        self.assertEqual(pm._display_resolve(shown2, self.recipient)["ref"], ref2)
        # while a row-less transfer that is still young is a send not yet received: it holds the text
        _t3, ref3, row3, shown3 = self.seal(self.S1, body="다른 글")
        os.unlink(pm._pending_path(ref3))
        _t4, ref4, row4, shown4 = self.seal(self.S2, body="다른 글")
        self.assertEqual(shown4, _t4)
        self.assertNotIn("display", row4)
        self.assertIsNotNone(received)

    def test_a_seal_that_finds_the_text_lock_busy_takes_the_old_form(self):
        _t1, ref1, _r1, shown1 = self.seal(self.S1)
        self.arrive(shown1, ref1, self.S1)
        digest = hashlib.sha256(shown1.encode("utf-8")).hexdigest()
        with pm.pending_lock(pm._display_seal_key(digest, self.recipient)):
            _t2, ref2, row2, shown2 = self.seal(self.S2)     # another seal of this text is in flight
        self.assertEqual(shown2, _t2)
        self.assertNotIn("display", row2)
        self.assertEqual(self.pointers(shown1), [ref1])
        _t3, ref3, row3, shown3 = self.seal(self.S1)   # lock released: the display form again
        self.assertEqual(shown3, shown1)
        self.assertIn("display", row3)

    def test_an_unexpected_error_while_sealing_releases_the_seal_lock_at_once(self):
        # the exception (and its traceback, so the frames) is still alive when the lock is looked at:
        # the lock directory must be gone because the seal left its `with`, not because of garbage collection
        caught = None
        with mock.patch.object(pm, "_display_unreceived_holder", side_effect=RuntimeError("boom")):
            try:
                self.seal(self.S1)
            except RuntimeError as exc:
                caught = exc          # kept: its traceback keeps the sealing frames alive
        self.assertIsNotNone(caught)
        self.assertEqual(sorted(path.name for path in pm._pending_dir().glob(".lock-prepare-*")), [])
        _t, _ref, row, _shown = self.seal(self.S1)      # and the text can be sealed at once, in the display form
        self.assertIn("display", row)

    def test_the_visible_text_of_another_recipient_is_independent(self):
        other = {"harness": "claude", "session_id": "recipient-other"}
        pm.note_display_ready(other)
        _t1, ref1, _r1, shown1 = self.seal(self.S1)
        text2, ref2 = pm.prepare_peer_message("작업 끝났습니다", self.S2, other, defer=True)
        self.assertEqual(pm.pasted_text(pm._read_pending(ref2)), shown1)    # not blocked by a message for another session

    def test_a_delayed_delivery_prefix_binds_only_to_the_record_it_names(self):
        _t1, ref1, _r1, shown1 = self.seal(self.S1)
        self.arrive(shown1, ref1, self.S1)
        _t2, ref2, _r2, shown2 = self.seal(self.S2)
        self.assertEqual(shown2, shown1)

        def delayed(ref):
            return "[지연 전달 — 원래 보낸 시각 2026-11-19 10:00, 약 2시간 지연] (ref %s)\n%s" % (ref[:8], shown1)
        # the prefix names the received first record: it binds that one, though the second waits
        self.assertEqual(pm._display_resolve(delayed(ref1), self.recipient)["ref"], ref1)
        self.assertEqual(pm._display_resolve(delayed(ref2), self.recipient)["ref"], ref2)
        # a prefix that names no candidate binds nothing, as the old form's prefix check does
        other8 = "0" * 8 if not ref1.startswith("0" * 8) and not ref2.startswith("0" * 8) else "f" * 8
        stray = "[지연 전달 — 원래 보낸 시각 2026-11-19 10:00, 약 2시간 지연] (ref %s)\n%s" % (other8, shown1)
        self.assertIsNone(pm._display_resolve(stray, self.recipient))
        self.assertEqual(pm.expand_display_text(stray, self.recipient), stray)
        self.assertIsNone((pm.parse_peer_trailer(stray, self.recipient) or {}).get("session_id"))
        self.assertEqual(pm._read_pending(ref2)["state"], "pending")

    def test_crlf_and_collapsed_blank_lines_are_not_bound_as_the_old_form_does_not(self):
        body = "첫 줄\n\n셋째 줄"
        text, ref, row, shown = self.seal(self.S1, body=body)
        self.assertIsNotNone(pm._display_resolve(shown, self.recipient))
        for name, changed in (("crlf", shown.replace("\n", "\r\n")),
                              ("collapsed-blank-line", shown.replace("\n\n", "\n"))):
            with self.subTest(changed=name):
                self.assertNotEqual(changed, shown)
                self.assertIsNone(pm._display_resolve(changed, self.recipient))
                self.assertEqual(pm.expand_display_text(changed, self.recipient), changed)
                self.assertIsNone((pm.parse_peer_trailer(changed, self.recipient) or {}).get("session_id"))
                # the old form falls the same way for the same change
                old_changed = text.replace("\n", "\r\n") if name == "crlf" else text.replace("\n\n", "\n")
                self.assertIsNone((pm.parse_peer_trailer(old_changed, self.recipient) or {}).get("session_id"))
        self.assertEqual(pm._read_pending(ref)["state"], "pending")


class CodexQueueResend(DisplayFixture):
    """The Codex queue compares what it sent with what it sends again: the text must not change
    between attempts because a sealed record could not be read this time."""

    def test_an_unreadable_sealed_record_stops_the_attempt_instead_of_changing_the_text(self):
        import codex_queue_delivery
        import codex_queue_dispatch
        recipient = {"harness": "codex", "session_id": "queue-resend"}
        pm.note_display_ready(recipient)
        sender = {"harness": "claude", "session_id": "0100000a-0000-7000-8000-0000000000b1"}
        text, ref = pm.prepare_peer_message("큐 재시도", sender, recipient, defer=True)
        row = pm._read_pending(ref)
        visible = pm.pasted_text(row)
        self.assertNotEqual(visible, text)
        self.assertEqual(pm.pasted_text(row, strict=True), visible)
        real = pm._read_transfer_record

        def unreadable_for_the_text(*args, **kwargs):
            import inspect
            if any(frame.function == "pasted_text" for frame in inspect.stack()[:8]):
                raise OSError("transient read failure")
            return real(*args, **kwargs)
        with mock.patch.object(pm, "_read_transfer_record", side_effect=unreadable_for_the_text):
            self.assertEqual(pm.pasted_text(row), text)                     # a plain carrier keeps the sealed text
            with self.assertRaises(ValueError):
                pm.pasted_text(row, strict=True)
            with mock.patch.object(codex_queue_delivery, "send_at_least_once") as send, \
                    mock.patch.object(codex_queue_dispatch, "resolve_queue_socket", return_value="unused"):
                outcome = pm._deliver_pending_codex_row(ref, timeout=1, start_interrupted=False)
        send.assert_not_called()                        # nothing was handed over under a changed text
        self.assertEqual(outcome["status"], "unverified")
        self.assertIn("peer-display-unreadable", outcome["reason"])
        after = pm._read_pending(ref)
        self.assertEqual((after["state"], after.get("rpc_claim")), ("pending", None))   # retried by the next pass
        # read again, the same visible text goes out
        with mock.patch.object(codex_queue_delivery, "send_at_least_once",
                               return_value={"status": "queued", "queued_submission_id": "q1"}) as send, \
                mock.patch.object(codex_queue_dispatch, "resolve_queue_socket", return_value="unused"):
            pm._deliver_pending_codex_row(ref, timeout=1, start_interrupted=False)
        self.assertEqual(send.call_args.kwargs["message"], visible)


class OldFormsStillWork(DisplayFixture):
    """Mixed versions: texts already sent in an older form are received exactly as before, and a
    receiver whose code predates the short forms is never sent one."""

    def test_a_text_sent_before_with_the_decorated_trailer_is_received_and_settled(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                recipient = {"harness": harness, "session_id": "old-" + harness}
                with mock.patch.object(pm, "peer_trailer", side_effect=old_decorated_trailer):
                    text, ref = pm.prepare_peer_message("이미 보낸 본문 " + harness, self.sender, recipient, defer=True)
                self.assertIn(" ; pane=- ; sid=" + self.sender["session_id"], text)
                self.assertEqual(pm.parse_peer_trailer(text, recipient, include_ref=True)["transfer_ref"], ref)
                self.assertEqual(pm.receive_peer_message(text, recipient), 0)
                self.assertEqual(pm._read_pending(ref)["state"], "received")
                self.assertEqual(pm.receiver_guidance(text, recipient), pm._ordinary_receiver_guidance())

    def test_a_runtime_text_sent_before_with_the_decorated_trailer_is_received_and_settled(self):
        recipient = {"harness": "claude", "session_id": "old-runtime"}
        sender = {"harness": "runtime", "session_id": None, "name": "Hearting runtime"}
        duty_id = "registered-batch-" + "c" * 32
        refs = [duty_id, "att-old", "delivery-old"]
        producer = {"schema": pm._RUNTIME_PRODUCER_SCHEMA, "duty_id": duty_id, "jobs": str(self.state / "jobs.log"),
                    "attempt_id": refs[1], "carrier": "codex-native-queue", "delivery_id": refs[2],
                    "receipt_digest": "e" * 64, "recipient": recipient, "route_id": "rt", "route_node": "execute"}
        (self.state / "jobs.log").touch()
        body = ("Hearting notice about work you started (a paused route resumed, a remote run ended). "
                "Act only where a line's required_action asks for it.\nHEARTING_RECEIPT_V1 delivery-old " + "e" * 64)
        with mock.patch.object(pm, "peer_trailer", side_effect=old_decorated_trailer):
            text, ref = pm.prepare_peer_message(body, sender, recipient, defer=True, refs=refs,
                                                producer_binding=producer, receipt="native-pending")
        self.assertIn("(runtime-from: runtime [?] Hearting runtime ; pane=- ; sid=- ; ref=" + ref + ")", text)
        self.assertEqual(pm.parse_peer_trailer(text, recipient, include_ref=True)["source"], "runtime-completion")
        self.assertEqual(pm.prompt_origin(text, "claude", recipient["session_id"])["kind"], "runtime-completion")
        self.assertEqual(pm.receive_peer_message(text, recipient), 0)
        self.assertEqual(pm._read_pending(ref)["state"], "received")

    def test_a_receiver_that_predates_the_short_forms_cannot_read_them_so_it_is_not_sent_them(self):
        recipient = {"harness": "codex", "session_id": "old-receiver"}
        # no sign: the old-form text goes out, and every published receiver reads it
        text, ref = pm.prepare_peer_message("본문", self.sender, recipient, defer=True)
        self.assertEqual(pm.pasted_text(pm._read_pending(ref)), text)
        checked = 0
        for tag in OLD_RELEASES:
            source = release_source(tag, "utilities/peer-message.py")
            if source is None:
                continue
            scope = {"__file__": str(ROOT / "utilities/peer-message.py"), "__name__": "pinned_" + tag.replace(".", "_")}
            exec(compile(source, "peer-message@" + tag, "exec"), scope)
            self.assertEqual(scope["parse_peer_trailer"](text, recipient, include_ref=True)["transfer_ref"], ref, tag)
            # the short form is exactly what such a receiver would take for a person's input
            pm.note_display_ready(recipient)
            sent, ref2 = pm.prepare_peer_message("본문 " + tag, self.sender, recipient, defer=True)
            shown = pm.pasted_text(pm._read_pending(ref2))
            self.assertNotEqual(shown, sent)
            self.assertIsNone(scope["parse_peer_trailer"](shown, recipient, include_ref=True))
            os.unlink(pm._display_ready_path(recipient))
            checked += 1
        if not checked:
            self.skipTest("no published release is available in this clone")

    def test_the_sign_is_read_from_the_exact_session_only(self):
        pm.note_display_ready({"harness": "claude", "session_id": "sign-a"})
        for other in ({"harness": "codex", "session_id": "sign-a"}, {"harness": "claude", "session_id": "sign-b"},
                      {"harness": "claude", "session_id": None}, {"harness": "runtime", "session_id": "sign-a"},
                      {"harness": "claude", "session_id": "../x"}):
            self.assertFalse(pm.receiver_reads_display(other), other)
        self.assertTrue(pm.receiver_reads_display({"harness": "claude", "session_id": "sign-a"}))
    def test_a_sign_older_than_two_days_is_not_believed_and_one_a_day_old_is_renewed(self):
        recipient = {"harness": "opencode", "session_id": "sign-age"}
        pm.note_display_ready(recipient)
        path = pm._display_ready_path(recipient)
        self.assertTrue(pm.receiver_reads_display(recipient))
        for age_days, believed in ((0.5, True), (1.9, True), (2.1, False), (30, False)):
            old = time.time() - age_days * 86400
            os.utime(path, (old, old))
            self.assertEqual(pm.receiver_reads_display(recipient), believed, age_days)
        # a session whose code went back is sent the old form once its sign has gone stale
        text, ref = pm.prepare_peer_message("되돌린 세션", self.sender, recipient, defer=True)
        self.assertIn("(peer-from:", pm.pasted_text(pm._read_pending(ref)))
        self.assertNotIn("display", pm._read_transfer_record(ref))
        # the receiving code renews the sign at most once a day
        old = time.time() - 0.5 * 86400
        os.utime(path, (old, old))
        pm.note_display_ready(recipient)
        self.assertAlmostEqual(path.stat().st_mtime, old, delta=2)
        old = time.time() - 1.5 * 86400
        os.utime(path, (old, old))
        pm.note_display_ready(recipient)
        self.assertGreater(path.stat().st_mtime, time.time() - 60)
        self.assertTrue(pm.receiver_reads_display(recipient))

    def test_the_hint_and_sign_folders_are_dot_named_and_no_ledger_scanner_reads_them(self):
        recipient = {"harness": "claude", "session_id": "dot-folders"}
        pm.note_display_ready(recipient)
        text, ref = pm.prepare_peer_message("점 폴더", self.sender, recipient, defer=True)
        self.assertIn("display", pm._read_transfer_record(ref))
        base = pm.peer_state_root() / "peer-messages"
        self.assertEqual(pm._display_root(), base / ".display")
        self.assertEqual(pm._display_ready_path(recipient).parents[1], base / ".display-ready")
        self.assertEqual(sorted(p.name for p in base.iterdir() if "display" in p.name), [".display", ".display-ready"])
        # a ledger-looking file inside either folder, and a real month file for contrast
        record = json.dumps({"ts": "2026-11-19T00:00:00Z", "kind": "notice", "transfer_ref": "d" * 32})
        for folder in (".display", ".display-ready"):
            (base / folder / "stray.jsonl").write_text(record + "\n", encoding="utf-8")
        month = base / "2026-11"
        month.mkdir()
        (month / "peer.jsonl").write_text(record.replace("d" * 32, "e" * 32) + "\n", encoding="utf-8")
        self.assertEqual([r["transfer_ref"] for r in pm._iter_records() if r.get("transfer_ref") in ("d" * 32, "e" * 32)],
                         ["e" * 32])
        diagnostics, status = [], {"checked_roots": 0, "failed_roots": 0}
        paths = fleet_peers._ledger_paths([str(pm.peer_state_root())], diagnostics, status)
        self.assertEqual([os.path.relpath(p, base) for p in paths], ["2026-11/peer.jsonl"])


if __name__ == "__main__":
    unittest.main()
