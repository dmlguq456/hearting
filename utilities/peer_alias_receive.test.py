#!/usr/bin/env python3
"""별칭은 표시 전용: 전송 본문·실제 수신자·로컬 원장 결속 회귀. 운영 전송 없음."""
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


pm = module("alias_peer_message", "utilities/peer-message.py")
fixture_processes = module("alias_fixture_processes", "tools/fixture_processes.py")


def pinned_source(path):
    # HOME is isolated, so use command-local trust for this exact test checkout.
    return subprocess.check_output(["git", "-c", f"safe.directory={ROOT}", "show", f"1c201125:{path}"],
                                   cwd=ROOT, text=True, timeout=5)


class AliasReceive(unittest.TestCase):
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
        self.sender = {"harness": "codex", "session_id": "01a07b2b-9108-7110-bd41-93b7ade4d9c4", "name": "동명"}
        self.recipient = {"harness": "codex", "session_id": "recipient-a"}

    def prepare(self, body="검증할 실제 본문", sender=None, recipient=None):
        return pm.prepare_peer_message(body, sender or self.sender, recipient or self.recipient)

    def prepare_legacy_reference(self, body, sender=None, recipient=None):
        sender = sender or self.sender
        recipient = recipient or self.recipient
        evidence = {"schema": "legacy-reference-fixture", "recipient": dict(recipient)}
        with mock.patch.object(pm, "_reference_access_evidence_valid", return_value=True):
            return pm.prepare_peer_message(body, sender, recipient, defer=True,
                                           reference_evidence=evidence)

    def parse(self, text, recipient=None):
        return pm.parse_peer_trailer(text, recipient or self.recipient)["session_id"]

    def rows(self):
        return [json.loads(line) for p in (self.state / "peer-messages").glob("*/*.jsonl")
                for line in p.read_text().splitlines()]

    def test_exact_body_recipient_and_transfer_record(self):
        text, ref = self.prepare()
        self.assertIn(" ; sid=" + self.sender["session_id"], text)
        self.assertIn(" ; pane=- ;", text)
        self.assertEqual(self.parse(text), self.sender["session_id"])
        self.assertIsNone(self.parse(text + "변조"))
        self.assertIsNone(self.parse(text.replace("실제", "다른")))
        for target in ({"harness": "claude", "session_id": "recipient-a"},
                       {"harness": "codex", "session_id": "recipient-b"},
                       {"harness": "codex", "session_id": ""}):
            self.assertIsNone(self.parse(text, target))
        record = json.loads(pm._transfer_path(ref).read_text())
        self.assertEqual(record["message_id"], ref)
        self.assertEqual(record["from"], self.sender)
        self.assertEqual(record["to"], self.recipient)
        self.assertNotIn("검증할", json.dumps(record, ensure_ascii=False))

    def test_visible_sid_and_pane_never_bypass_ref_recipient_or_body_validation(self):
        for harness in ("claude", "codex", "opencode"):
            sender = dict(self.sender, harness=harness, pane_id="w:sender")
            text, ref = self.prepare(sender=sender)
            self.assertIn(" ; pane=w:sender ; sid=" + sender["session_id"], text)
            self.assertEqual(self.parse(text), sender["session_id"])
            self.assertEqual(pm.parse_peer_trailer(text)["name"], sender["name"])
            for changed in (text.replace(" ; ref=" + ref, ""),
                            text.replace("pane=w:sender", "pane=w:recipient"),
                            text.replace("sid=" + sender["session_id"], "sid=other-session"),
                            text.replace("동명", "다른 이름")):
                self.assertIsNone(self.parse(changed), changed)
        unknown = pm.peer_trailer("runtime", None, "Hearting runtime")
        self.assertIn("[?]", unknown)
        self.assertIn("pane=- ; sid=-", unknown)
        self.assertIsNone(pm.parse_peer_trailer(unknown)["session_id"])

    def test_trailer_control_characters_cannot_create_another_sender(self):
        sender = dict(self.sender, pane_id="w:p)\n; injected", name="작업\x00 ; ) 이름")
        text, _ = self.prepare(sender=sender)
        self.assertEqual(text.count("(peer-from:"), 1)
        self.assertNotIn("\x00", text)
        self.assertEqual(text.count(")"), 1)
        self.assertEqual(self.parse(text), sender["session_id"])

    def test_whitespace_a_runtime_adds_around_the_prompt_keeps_the_attribution(self):
        # OpenCode delivers the typed prompt with a trailing space; every OpenCode receipt was
        # unattributed (2026-10-06: 0 of 299) until the arrival was compared without it.
        text, ref = self.prepare()
        for arrived in (text + " ", text + "\n", "\n" + text + "  \n"):
            with self.subTest(arrived=arrived[-3:]):
                self.assertEqual(self.parse(arrived), self.sender["session_id"])
                trailer = pm.parse_peer_trailer(arrived, self.recipient, include_ref=True)
                self.assertEqual(trailer["transfer_ref"], ref)
        self.assertIsNone(self.parse(text + " 변조"))
        self.assertIsNone(self.parse(text.replace("실제 본문", "실제  본문")))

    def test_collision_same_name_and_swapped_refs(self):
        # Identical unknown aliases still require independent sealed transfers.
        self.assertEqual(pm.peer_alias("codex", self.sender["session_id"]), "[?]")
        other = "other-unassigned-session"
        self.assertEqual(pm.peer_alias("codex", other), "[?]")
        sender2 = dict(self.sender, session_id=other)
        one, r1 = self.prepare("첫 본문")
        two, r2 = self.prepare("둘째 본문", sender=sender2)
        self.assertEqual(self.parse(one), self.sender["session_id"])
        self.assertEqual(self.parse(two), other)
        self.assertNotEqual(r1, r2)
        self.assertIsNone(self.parse(one.replace(r1, r2)))
        self.assertIsNone(self.parse(two.replace(r2, r1)))
        self.assertIsNone(self.parse("복사한 본문\n" + one.splitlines()[-1]))

    def test_missing_corrupt_unknown_and_path_reference(self):
        for damage in ("missing", "json", "shape", "id", "digest"):
            text, ref = self.prepare()
            path = pm._transfer_path(ref)
            if damage == "missing":
                path.rename(path.with_suffix(".missing"))
            elif damage == "json":
                path.write_text("broken")
            elif damage == "shape":
                path.write_text("[]")
            else:
                rec = json.loads(path.read_text())
                rec["message_id" if damage == "id" else "body_sha256"] = "wrong"
                path.write_text(json.dumps(rec))
            self.assertIsNone(self.parse(text), damage)
        text, ref = self.prepare()
        with mock.patch.object(pm, "peer_state_root", side_effect=AssertionError("no path lookup")):
            self.assertIsNone(self.parse(text.replace(ref, "../../secret")))
        unknown, _ = self.prepare(sender={"harness": "unknown", "session_id": "known-id"})
        self.assertIn("[?]", unknown)
        self.assertIsNone(self.parse(unknown))

    def deferred(self, recipient=None, sender=None, old=False):
        text, ref = pm.prepare_peer_message("deferred full body", sender or self.sender,
                                            recipient or self.recipient, defer=True)
        if old:
            with pm.pending_lock(ref):
                row = pm._read_pending(ref)
                pm._save_pending(dict(row, created=row["created"] - 7200))
        return text, ref

    def test_busy_claude_hook_delivers_body_without_pane_input_then_exact_receive(self):
        recipient = dict(self.recipient, harness="claude")
        text, ref = self.deferred(recipient)
        env = dict(os.environ, HERDR_PANE_ID="foreign-pane")
        payload = {"session_id": recipient["session_id"], "tool_name": "Bash", "tool_input": {}}
        def hook():
            return subprocess.run([sys.executable, str(ROOT / "hooks/peer-message-record.py"), "post-tool"],
                input=json.dumps(payload), env=env, capture_output=True, text=True, timeout=10)
        first = hook()
        self.assertEqual(first.returncode, 0, first.stderr)
        context = json.loads(first.stdout)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "PostToolUse")
        self.assertEqual(context["additionalContext"], text)
        row = pm._read_pending(ref)
        self.assertEqual((row["state"], row["receipt"], row["text"]),
                         ("received", "exact-peer-ref", None))
        self.assertEqual(len(self.rows()), 1, "native context delivery settles one exact receive")
        self.assertEqual(hook().stdout, "", "a received context must not be emitted twice")
        self.assertEqual(pm.receive_peer_message(text, recipient), 0)
        self.assertEqual(pm._read_pending(ref)["state"], "received")
        self.assertIsNone(pm._read_pending(ref)["text"])
        self.assertEqual(len(self.rows()), 1)

    def test_hook_output_failure_preserves_ambiguous_body_without_resend(self):
        recipient = dict(self.recipient, harness="claude")
        text, ref = self.deferred(recipient)
        stream = mock.Mock()
        stream.flush.side_effect = BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            pm.emit_peer_context(recipient, "PostToolUse", stream=stream)
        row = pm._read_pending(ref)
        self.assertEqual(row["state"], "unverified")
        self.assertEqual(row["text"], text)
        output = io.StringIO()
        pm.emit_peer_context(recipient, "PostToolUse", stream=output)
        self.assertEqual(output.getvalue(), "")

    def test_foreign_and_worker_hooks_never_consume_pending_context(self):
        recipient = dict(self.recipient, harness="claude")
        text, ref = self.deferred(recipient)
        for sid, worker in (("foreign", False), (recipient["session_id"], True)):
            env = dict(os.environ)
            if worker:
                env["AGENT_SESSION_ROLE"] = "worker"
            result = subprocess.run([sys.executable, str(ROOT / "hooks/peer-message-record.py"), "post-tool"],
                input=json.dumps({"session_id": sid, "tool_name": "Bash"}), env=env,
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.stdout, "")
            self.assertEqual(pm._read_pending(ref)["state"], "pending")

    def test_codex_receiver_keeps_native_queue_on_existing_tool_boundary(self):
        text, ref = self.deferred()
        output = io.StringIO()
        with mock.patch.object(pm, "deliver_pending_codex", return_value={"status": "queued"}) as send:
            pm.emit_peer_context(self.recipient, "PostToolUse", stream=output)
        send.assert_called_once_with(ref, timeout=0.25)
        self.assertEqual(output.getvalue(), "", "no second body delivery transport")

    def test_codex_confirmed_sessionstart_clear_input_reaches_shared_handover(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import pane_ownership
        import session_tidy
        import dispatch_seat_handover as handover

        hook = module("watch_continuity_codex_sessionstart",
                      "adapters/codex/hooks/sessionstart-lifecycle.py")
        pane, a, b = "fixture:codex-start-seat", "start-input-A", "start-input-B"
        base_env = {"HERDR_PANE_ID": pane, "AGENT_DISPATCH_CALLER_HARNESS": "codex"}
        commands = []
        with mock.patch.object(pane_ownership, "_caller_runtime", return_value=(None, False)), \
             mock.patch.object(pane_ownership, "verified_pane", side_effect=lambda value, *a, **k: value or ""), \
             mock.patch.object(pane_ownership, "current_codex_thread_for_caller", return_value=None):
            with mock.patch.dict(os.environ, {**base_env, "CODEX_THREAD_ID": a}):
                self.assertTrue(pm.mark_steward("codex", a,
                    {"harness": "claude", "session_id": "start-target", "name": "target"},
                    "watch", "fixture-time", source="watch"))
                seat = session_tidy.resolve_seat("codex", str(ROOT), os.environ, a)
                self.assertEqual((seat.kind, seat.pane), ("pane", pane))
                with session_tidy.seat_lock(seat.key):
                    session_tidy.record_event(seat, "codex", a, "start", source="startup",
                                              cwd=str(ROOT))
                    self.assertEqual(handover.write_snapshot_locked(
                        seat, "codex", a, jobs=self.state / "jobs.log"), 0)

            with mock.patch.dict(os.environ, {**base_env, "CODEX_THREAD_ID": b}):
                def confirmed_start(argv, **kwargs):
                    commands.append(list(argv))
                    self.assertIn("--harness", argv)
                    self.assertIn("codex", argv)
                    self.assertIn("--event", argv)
                    self.assertIn("start", argv)
                    self.assertIn("--session-id", argv)
                    self.assertIn(b, argv)
                    self.assertIn("--source", argv)
                    self.assertIn("clear", argv)
                    session_tidy.run_hook("codex", "start", b, source="clear",
                                           cwd=str(ROOT), env=os.environ)
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

                with mock.patch.object(hook.subprocess, "run", side_effect=confirmed_start):
                    self.assertEqual(hook.card_context(
                        {"session_id": b, "source": "clear"}, str(ROOT)), ("", "", None))
                self.assertEqual(len(commands), 1)
                rows = handover.handover_rows(seat)
                self.assertEqual([(row["from"], row["sid"]) for row in rows], [(a, b)])
                self.assertEqual(handover.resolved_steward_relations(
                    "codex", b, seat_key=seat.key)[0]["origin"]["sid"], a)

    def test_codex_native_queue_receives_same_watch_ref_at_confirmed_c_and_acks_c(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import pane_ownership
        import session_tidy
        import dispatch_seat_handover as handover

        pane = "fixture:watch-seat"
        a, b, c = "codex-watch-A", "codex-watch-B", "codex-watch-C"
        watch_id = "watch-queue-fixture"
        base_env = {"HERDR_PANE_ID": pane, "AGENT_DISPATCH_CALLER_HARNESS": "codex"}
        with mock.patch.object(pane_ownership, "_caller_runtime", return_value=(None, False)), \
             mock.patch.object(pane_ownership, "verified_pane", side_effect=lambda value, *a, **k: value or ""), \
             mock.patch.object(pane_ownership, "current_codex_thread_for_caller", return_value=None):
            with mock.patch.dict(os.environ, {**base_env, "CODEX_THREAD_ID": a}):
                self.assertTrue(pm.mark_steward("codex", a,
                    {"harness": "claude", "session_id": "target-session", "name": "target"},
                    "watch", "fixture-time", source="watch"))
                seat = session_tidy.resolve_seat("codex", str(ROOT), os.environ, a)
                self.assertEqual((seat.kind, seat.pane), ("pane", pane))
                watch_root = pm.peer_state_root() / "peer-watches"
                watch_root.mkdir(parents=True, exist_ok=True)
                arm = {"schema_version": 1, "watch_id": watch_id, "target": "target-name",
                       "server": "queue-fixture", "until": ["done"],
                       "deadline": {"wall_deadline": 9999999999, "requested_ms": 60000,
                                    "boot_id": "fixture"}, "armed_ts": "fixture-time",
                       "steward": {"harness": "codex", "session_id": a},
                       "steward_seat": seat.as_dict(),
                       "agent": {"harness": "claude", "session_id": "target-session",
                                 "name": "target", "pane": "fixture:target"}}
                (watch_root / f"{watch_id}.json").write_text(json.dumps(arm), encoding="utf-8")
                with session_tidy.seat_lock(seat.key):
                    session_tidy.record_event(seat, "codex", a, "start", source="startup",
                                              cwd=str(ROOT))
                    self.assertEqual(handover.write_snapshot_locked(
                        seat, "codex", a, jobs=self.state / "jobs.log"), 0)

            with mock.patch.dict(os.environ, {**base_env, "CODEX_THREAD_ID": b}):
                session_tidy.run_hook("codex", "start", b, source="clear", cwd=str(ROOT), env=os.environ)
                with session_tidy.seat_lock(seat.key):
                    self.assertEqual(handover.write_snapshot_locked(
                        seat, "codex", b, jobs=self.state / "jobs.log"), 0)

            sender = {"harness": "codex", "session_id": a}
            original_storage = {"harness": "codex", "session_id": a}
            body = f"[peer-steward] watch_id={watch_id} state=idle target=target-name"
            text, transfer_ref = pm.prepare_peer_message(
                body, sender, original_storage, defer=True,
                refs=[f"peer-watch:{watch_id}"], receipt=f"peer-steward-watch:{watch_id}")
            transfer_before = pm._read_transfer_record(transfer_ref)
            self.assertEqual(pm.pending_delivery_recipient(pm._read_pending(transfer_ref)),
                             {"harness": "codex", "session_id": b})
            self.assertEqual(handover.acknowledge_watch(watch_id, "codex", a, "stale-A"),
                             "not-recipient")

            with mock.patch.dict(os.environ, {**base_env, "CODEX_THREAD_ID": c}):
                session_tidy.run_hook("codex", "start", c, source="clear", cwd=str(ROOT), env=os.environ)
                with session_tidy.seat_lock(seat.key):
                    self.assertEqual(handover.write_snapshot_locked(
                        seat, "codex", c, jobs=self.state / "jobs.log"), 0)
                recipient = {"harness": "codex", "session_id": c}
                self.assertEqual(pm.pending_delivery_recipient(pm._read_pending(transfer_ref)), recipient)
                self.assertEqual(pm.pending_messages(recipient)[0]["ref"], transfer_ref)
                self.assertEqual(handover.acknowledge_watch(watch_id, "codex", b, "stale-B"),
                                 "not-recipient")

                # Fleet forward/reverse uses the same resolved holder and target.
                sys.path.insert(0, str(ROOT / "tools"))
                from fleet.collectors import steward as fleet_steward
                predecessor_session = SimpleNamespace(
                    harness="codex", session_id=a, cwd=str(ROOT), slug="steward-A",
                    runtime_name=None, _herdr_name=None, session_aliases=[],
                    _gpu_session_aliases=[])
                steward_session = SimpleNamespace(
                    harness="codex", session_id=c, cwd=str(ROOT), slug="steward-C",
                    runtime_name=None, _herdr_name=None, session_aliases=[],
                    _gpu_session_aliases=[])
                target_session = SimpleNamespace(
                    harness="claude", session_id="target-session", cwd=str(ROOT),
                    slug="target", runtime_name="target", _herdr_name=None,
                    session_aliases=[], _gpu_session_aliases=[])
                marker_before = pm.steward_marker_path("codex", a).read_bytes()
                ledger_path = session_tidy._ledger_path(seat)
                ledger_before = ledger_path.read_bytes()
                arm_before = (watch_root / f"{watch_id}.json").read_bytes()
                fleet_steward.enrich([predecessor_session, steward_session, target_session],
                                     markers=pm.read_steward_markers([self.state]))
                self.assertFalse(predecessor_session.steward)
                self.assertTrue(steward_session.steward)
                self.assertEqual(steward_session.steward_targets[0]["session_id"], "target-session")
                self.assertEqual(target_session.steward_parents[0]["session_id"], c)
                self.assertEqual((marker_before, ledger_before, arm_before),
                                 (pm.steward_marker_path("codex", a).read_bytes(),
                                  ledger_path.read_bytes(),
                                  (watch_root / f"{watch_id}.json").read_bytes()))

                import codex_queue_delivery as queue
                import codex_queue_dispatch as queue_dispatch
                with mock.patch.object(queue_dispatch, "resolve_queue_socket", return_value="fixture-socket"), \
                     mock.patch.object(queue, "send_at_least_once", return_value={"status": "queued"}) as send:
                    self.assertEqual(pm.deliver_pending_codex(transfer_ref)["status"], "queued")
                self.assertEqual(send.call_args.kwargs["message"], text)
                self.assertEqual(pm._read_transfer_record(transfer_ref), transfer_before)

                import peer_obligations
                store = peer_obligations.ObligationStore()
                identity = {"server": "queue-fixture", "pane": pane,
                            "harness": "codex", "session_id": a}
                first_duty = store.create("message-" + transfer_ref, "message", identity,
                    {"ref": transfer_ref, "body_digest": transfer_before["body_sha256"],
                     "from": sender, "to": original_storage,
                     "refs": [f"peer-watch:{watch_id}"]})
                first_pending = pm._read_pending(transfer_ref)
                stale_ack_binding = pm.pending_delivery_binding(
                    first_pending, include_acked=True)
                stale_ack = dict(first_pending, state="received", text=None, rpc_claim=None,
                    receipt="exact-peer-ref", received_at=time.time(),
                    exact_receiver_receipt={
                        "receipt_type": "exact-peer-ref",
                        "provenance": "validated-peer-ref-receiver-v1",
                        "transfer_ref": transfer_ref,
                        "transfer_body_sha256": transfer_before["body_sha256"],
                        "effective_recipient": stale_ack_binding["recipient"],
                        "recipient_binding": stale_ack_binding,
                        "received_at": time.time()})

                # Pause after the census has been judged against C but before
                # the duty writer takes the existing seat lock. The real C -> D
                # handover must make this evidence stale rather than cancel D.
                race_text, race_ref = pm.prepare_peer_message(
                    "watch cancellation handover race", sender, original_storage,
                    defer=True, refs=[f"peer-watch:{watch_id}"])
                race_pending = pm._read_pending(race_ref)
                race_duty = store.create("message-" + race_ref, "message", identity,
                    {"ref": race_ref, "body_digest": race_pending["body_sha256"],
                     "from": sender, "to": original_storage,
                     "refs": [f"peer-watch:{watch_id}"]})
                stale_binding = pm.pending_delivery_binding(race_pending, include_acked=True)
                stale_evidence = {"kind": "recipient-session-absent", "server": "queue-fixture",
                    "harness": "codex", "session_id": c, "census_complete": True,
                    "session_absent": True, "census_endpoint": "queue-fixture",
                    "recipient_binding": stale_binding}
                observed, resume_cancel = threading.Event(), threading.Event()
                real_seat_lock = session_tidy.seat_lock
                race_outcomes = []

                def hold_before_seat_lock(key, **kwargs):
                    if threading.current_thread().name == "watch-cancel-race":
                        observed.set()
                        if not resume_cancel.wait(5):
                            raise TimeoutError("handover barrier was not released")
                    return real_seat_lock(key, **kwargs)

                def cancel_observed_generation():
                    race_outcomes.append(pm.cancel_message_obligation(
                        race_ref, duty_id=race_duty["id"],
                        expected_revision=race_duty["revision"], identity=identity,
                        message=race_pending, evidence=stale_evidence))

                d = "codex-watch-D"
                with mock.patch.object(session_tidy, "seat_lock", side_effect=hold_before_seat_lock):
                    worker = threading.Thread(target=cancel_observed_generation,
                                              name="watch-cancel-race")
                    worker.start()
                    self.assertTrue(observed.wait(5), "cancellation did not reach the handover boundary")
                    with mock.patch.dict(os.environ, {**base_env, "CODEX_THREAD_ID": d}):
                        session_tidy.run_hook("codex", "start", d, source="clear",
                                              cwd=str(ROOT), env=os.environ)
                    resume_cancel.set()
                    worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(race_outcomes, ["changed"])
                self.assertEqual(store.get(race_duty["id"])["state"], "pending")
                self.assertFalse(store.acknowledge_message_receipt(
                    first_duty["id"], stale_ack, {"harness": "codex", "session_id": c}))
                self.assertEqual(store.get(first_duty["id"])["state"], "pending")

                current_recipient = {"harness": "codex", "session_id": d}
                self.assertEqual(pm.pending_delivery_recipient(pm._read_pending(transfer_ref)),
                                 current_recipient)
                cancel_text, cancel_ref = pm.prepare_peer_message(
                    "watch absent successor cancellation", sender, original_storage,
                    defer=True, refs=[f"peer-watch:{watch_id}"])
                cancel_pending = pm._read_pending(cancel_ref)
                cancel_duty = store.create("message-" + cancel_ref, "message", identity,
                    {"ref": cancel_ref, "body_digest": cancel_pending["body_sha256"],
                     "from": sender, "to": original_storage,
                     "refs": [f"peer-watch:{watch_id}"]})
                successor_binding = pm.pending_delivery_binding(
                    cancel_pending, include_acked=True)
                absent_successor = {"kind": "recipient-session-absent",
                    "server": "queue-fixture", "harness": "codex", "session_id": d,
                    "census_complete": True, "session_absent": True,
                    "census_endpoint": "queue-fixture",
                    "recipient_binding": successor_binding}
                self.assertEqual(pm.cancel_message_obligation(
                    cancel_ref, duty_id=cancel_duty["id"],
                    expected_revision=cancel_duty["revision"], identity=identity,
                    message=cancel_pending, evidence=absent_successor), "cancelled")
                self.assertEqual(store.get(cancel_duty["id"])["state"], "cancelled")

                with mock.patch.dict(os.environ, {**base_env, "CODEX_THREAD_ID": d}):
                    lifecycle = module("watch_transfer_codex_lifecycle",
                                       "adapters/codex/hooks/userprompt-lifecycle.py")
                    ack_path = watch_root / f"{watch_id}.ack.json"
                    real_write = os.write
                    writes = []

                    def partial_ack_then_fail(fd, data):
                        try:
                            target_path = os.readlink(f"/proc/self/fd/{fd}")
                        except OSError:
                            target_path = ""
                        if f".{ack_path.name}." not in target_path or not target_path.endswith(".tmp"):
                            return real_write(fd, data)
                        writes.append(len(data))
                        if len(writes) == 1:
                            return real_write(fd, bytes(data[:max(1, len(data) // 2)]))
                        raise OSError("fixture interrupted native ACK write")

                    with mock.patch.object(handover.os, "write", side_effect=partial_ack_then_fail):
                        lifecycle.peer_notice({"session_id": d}, text, str(ROOT))
                    self.assertGreaterEqual(len(writes), 2)
                    self.assertFalse(ack_path.exists())
                    self.assertEqual(pm._read_pending(transfer_ref)["state"], "received")
                    lifecycle.peer_notice({"session_id": d}, cancel_text, str(ROOT))
                    late = store.get(cancel_duty["id"])
                    self.assertEqual((late["state"], late["result"], late["delivery"]),
                                     ("cancelled", None, "acknowledged"))
                    self.assertEqual(store.get(first_duty["id"])["state"], "complete")
                    # The next ordinary native callback retries the same watch ID.
                    lifecycle.peer_notice({"session_id": d}, text, str(ROOT))
                    pending = pm._read_pending(transfer_ref)
                    self.assertEqual((pending["state"], pending["receipt"]),
                                     ("received", "exact-peer-ref"))
                    ack = json.loads(ack_path.read_text(encoding="utf-8"))
                    self.assertEqual((ack["watch_id"], ack["harness"], ack["session_id"]),
                                     (watch_id, "codex", d))
                    self.assertEqual(handover.resolved_watch_references(
                        "codex", d, seat_key=seat.key), [])

    def test_original_sender_stuck_notice_on_all_existing_tool_callbacks(self):
        for harness in ("claude", "codex", "opencode"):
            sender = dict(self.sender, harness=harness, session_id=f"sender-{harness}")
            text, ref = self.deferred(sender=sender, old=True)
            if harness == "opencode":
                js = r'''import {pathToFileURL} from "node:url";
const {AgentHarnessGuards} = await import(pathToFileURL(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js"));
const hooks = await AgentHarnessGuards({client: {}});
const output = {output: "ordinary tool output"};
await hooks["tool.execute.after"]({sessionID: "sender-opencode", tool: "fixture-noop", args: {}}, output);
console.log(JSON.stringify(output));'''
                result = subprocess.run(["node", "--input-type=module", "-e", js], env=dict(os.environ, HERDR_PANE_ID=""),
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                context = json.loads(result.stdout)["output"]
                self.assertTrue(context.startswith("ordinary tool output"))
            else:
                command = ([sys.executable, str(ROOT / "hooks/peer-message-record.py"), "post-tool"] if harness == "claude"
                           else [sys.executable, str(ROOT / "adapters/codex/hooks/posttooluse-interaction-clear.py")])
                result = subprocess.run(command, input=json.dumps({"session_id": sender["session_id"], "tool_name": "Bash"}),
                                        env=dict(os.environ), capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn(ref[:8], context)
            self.assertIn("1시간", context)
            row = pm._read_pending(ref)
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["text"], text)
            self.assertTrue(row["sender_notice_at"])
            stream = io.StringIO()
            pm.emit_peer_context(sender, "PostToolUse", stream=stream)
            self.assertEqual(stream.getvalue(), "")

    def test_sender_notice_includes_accepted_but_unreceived_and_ambiguous_transfers(self):
        for state in ("queued", "unverified"):
            sender = dict(self.sender, session_id="sender-" + state)
            text, ref = self.deferred(sender=sender, old=True)
            with pm.pending_lock(ref):
                pm._save_pending(dict(pm._read_pending(ref), state=state))
            output = io.StringIO()
            pm.emit_peer_context(sender, "PostToolUse", stream=output)
            self.assertIn(ref[:8], output.getvalue())
            self.assertEqual(pm._read_pending(ref)["state"], state)
            self.assertEqual(pm._read_pending(ref)["text"], text)
            output = io.StringIO()
            pm.emit_peer_context(sender, "PostToolUse", stream=output)
            self.assertEqual(output.getvalue(), "")

    def test_parallel_sender_callbacks_claim_one_notice_before_output(self):
        import concurrent.futures
        import threading
        sender = dict(self.sender, harness="claude")
        text, ref = self.deferred(sender=sender, old=True)
        rows = list(pm._pending_rows())
        barrier = threading.Barrier(2)
        def same_snapshot(**_kwargs):
            barrier.wait(timeout=5)
            return iter(rows)
        streams = [io.StringIO(), io.StringIO()]
        with mock.patch.object(pm, "_pending_rows", side_effect=same_snapshot):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(pm.emit_peer_context, sender, "PostToolUse", stream=s) for s in streams]
                for future in futures:
                    future.result(timeout=5)
        self.assertEqual(sum(ref[:8] in s.getvalue() for s in streams), 1)
        self.assertEqual(pm._read_pending(ref)["sender_notice_receipt"], "context-emitted")
        self.assertEqual(pm._read_pending(ref)["text"], text)

    def test_sender_notice_failed_flush_is_not_repeated(self):
        sender = dict(self.sender, harness="claude")
        text, ref = self.deferred(sender=sender, old=True)
        stream = mock.Mock()
        stream.flush.side_effect = BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            pm.emit_peer_context(sender, "PostToolUse", stream=stream)
        self.assertEqual(pm._read_pending(ref)["sender_notice_receipt"], "context-unverified")
        self.assertEqual(pm._read_pending(ref)["text"], text)
        output = io.StringIO()
        pm.emit_peer_context(sender, "PostToolUse", stream=output)
        self.assertEqual(output.getvalue(), "")

    def test_delay_prefix_preserves_exact_body_recipient_and_receive_dedup(self):
        for harness in ("claude", "codex", "opencode"):
            recipient = dict(self.recipient, harness=harness)
            text, ref = self.prepare(recipient=recipient)
            prefix = f"[지연 전달 — 원래 보낸 시각 2026-10-07 18:47, 약 2시간 지연] (ref {ref[:8]})\n"
            delayed = prefix + text
            wrapped = '<pasted_content id="late">\n' + delayed + '\n</pasted_content id="late">'
            for value in (delayed, wrapped):
                self.assertEqual(self.parse(value, recipient), self.sender["session_id"])
                self.assertIsNone(self.parse(value.replace("검증할", "변조할"), recipient))
                self.assertIsNone(self.parse(value, dict(recipient, session_id="foreign")))
            self.assertIsNone(self.parse(delayed.replace(ref[:8] + ")", "00000000)"), recipient))
            self.assertEqual(pm.receive_peer_message(delayed, recipient), 0)
            self.assertEqual(pm.receive_peer_message(wrapped, recipient), 0)
            notices = [r for r in pm._iter_records() if r.get("transfer_ref") == ref]
            self.assertEqual(len(notices), 1)

    def test_legacy_last_trailer_and_pinned_old_receiver(self):
        legacy = "기존 본문\n(peer-from: claude full-legacy-id old-name)"
        self.assertEqual(self.parse(legacy), "full-legacy-id")
        text, _ = self.prepare(legacy)
        self.assertEqual(self.parse(text), self.sender["session_id"])
        self.assertIsNone(self.parse("앞에 추가\n" + text))
        self.assertEqual(self.parse(text + "\n(peer-from: claude last-id last-name)"), "last-id")
        # Execute the actual pinned pre-change parser; no mock reinterpretation.
        source = pinned_source("utilities/peer-message.py")
        scope = {"__file__": str(ROOT / "utilities/peer-message.py"), "__name__": "pinned_peer"}
        exec(compile(source, "pinned-peer-message", "exec"), scope)
        old = scope["parse_peer_trailer"](text)
        self.assertIsNone(old["session_id"])
        self.assertIsNone(pm.parse_peer_trailer(text)["session_id"])

    def test_claude_derived_then_rename_remembered_tag(self):
        from fleet import titles
        sessions = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions"
        sessions.mkdir(parents=True)
        path = sessions / "123.json"
        path.write_text(json.dumps({"sessionId": "claude-id", "name": "project-ab", "nameSource": "derived"}))
        with mock.patch.object(titles, "read_tag", return_value="cd"):
            self.assertEqual(pm.peer_alias("claude", "claude-id"), "[ab]")
            path.write_text(json.dumps({"sessionId": "claude-id", "name": "renamed-ef", "nameSource": "user"}))
            self.assertEqual(pm.peer_alias("claude", "claude-id"), "[cd]")
        with mock.patch.object(titles, "read_tag", return_value=None):
            self.assertEqual(pm.peer_alias("claude", "claude-id"), "[?]")

    def test_pane_move_and_reuse_do_not_supply_identity(self):
        text, _ = self.prepare(recipient=dict(self.recipient, pane="w1:p1", name="동명"))
        self.assertEqual(self.parse(text, dict(self.recipient, pane="w2:p8")), self.sender["session_id"])
        self.assertIsNone(self.parse(text, dict(self.recipient, pane="w1:p1", session_id="new-occupant")))

    def test_transfer_ref_cannot_be_rebound_or_read_through_symlink(self):
        text, ref = self.prepare()
        path = pm._transfer_path(ref)
        original = path.read_bytes()
        with mock.patch.object(pm.secrets, "token_hex", return_value=ref):
            with self.assertRaises(FileExistsError):
                self.prepare("다른 전송")
        self.assertEqual(path.read_bytes(), original)
        renamed = path.with_suffix(".saved")
        path.rename(renamed)
        path.symlink_to(renamed)
        self.assertIsNone(self.parse(text))

    def _receive_actual(self, harness, text, sid="recipient-a"):
        if harness == "claude":
            hook = module("alias_claude_hook", "hooks/peer-message-record.py")
            hook.handle_prompt({"session_id": sid, "cwd": str(ROOT), "prompt": text})
        elif harness == "codex":
            hook = module("alias_codex_hook", "adapters/codex/hooks/userprompt-lifecycle.py")
            hook.peer_notice({"session_id": sid}, text, str(ROOT))
        else:
            proc = subprocess.run([sys.executable, str(ROOT / "utilities/peer-message.py"), "receive",
                                   "--to-harness", harness, "--to-session-id", sid],
                                  input=text, text=True, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_distinct_installed_runtime_roots_all_sender_receiver_pairs(self):
        roots = {"claude": self.state / "xdg/hearting/dispatch",
                 "codex": self.state / "home/.codex/.harness/dispatch",
                 "opencode": self.state / "home/.config/opencode/.harness/dispatch"}
        for sender_harness, sender_root in roots.items():
            for receiver_harness, receiver_root in roots.items():
                with self.subTest(sender=sender_harness, receiver=receiver_harness):
                    sender = dict(self.sender, harness=sender_harness,
                                  session_id=f"sender-{sender_harness}-{receiver_harness}")
                    target = dict(self.recipient, harness=receiver_harness)
                    os.environ["AGENT_PEER_LEDGER_ROOT"] = str(sender_root)
                    text, ref = self.prepare(sender=sender, recipient=target)
                    os.environ["AGENT_PEER_LEDGER_ROOT"] = str(receiver_root)
                    self.assertEqual(self.parse(text, target), sender["session_id"])
                    self.assertIsNone(self.parse(text + "改", target))
                    self.assertIsNone(self.parse(text, dict(target, session_id="new-occupant")))
                    self._receive_actual(receiver_harness, text)
                    path = receiver_root / "peer-messages" / time.strftime("%Y-%m", time.gmtime()) / (sender["session_id"] + ".jsonl")
                    row = json.loads(path.read_text().splitlines()[-1])
                    self.assertEqual(row["from"]["session_id"], sender["session_id"])
                    self.assertEqual(row["to"]["session_id"], target["session_id"])
                    transfer = sender_root / "peer-messages/transfers" / (ref + ".json")
                    transfer.rename(transfer.with_suffix(".missing"))
                    self.assertIsNone(self.parse(text, target))

    def test_metadata_types_fail_soft_in_all_receivers(self):
        changes = [(side, field, value) for side in ("from", "to")
                   for field, values in (("session_id", (123, True, [], None, " padded ")),
                                         ("name", ([], {}))) for value in values]
        changes += [(side, None, value) for side in ("from", "to") for value in ([], None)]
        for harness in ("claude", "codex", "opencode"):
            for side, field, value in changes:
                with self.subTest(harness=harness, side=side, field=field, value=value):
                    text, ref = self.prepare(recipient=dict(self.recipient, harness=harness))
                    path = pm._transfer_path(ref)
                    record = json.loads(path.read_text())
                    if field:
                        record[side][field] = value
                    else:
                        record[side] = value
                    path.write_text(json.dumps(record))
                    self._receive_actual(harness, text)
        rows = self.rows()
        self.assertEqual(len(rows), len(changes) * 3)
        self.assertTrue(all(row["from"]["session_id"] == "" for row in rows))

    def test_missing_claude_tag_does_not_remove_proven_identity(self):
        sender = dict(self.sender, harness="claude", session_id="claude-unremembered")
        text, ref = self.prepare(sender=sender)
        self.assertIn("[?]", text)
        self.assertEqual(self.parse(text), sender["session_id"])
        self.assertIsNone(pm.parse_peer_trailer(text)["session_id"])
        pm._transfer_path(ref).write_text("null")
        self.assertIsNone(self.parse(text))

    def test_duplicate_ref_across_trusted_roots_is_ambiguous(self):
        text, ref = self.prepare()
        other_root = self.state / "home/.codex/.harness/dispatch"
        other = other_root / "peer-messages/transfers" / (ref + ".json")
        other.parent.mkdir(parents=True)
        other.write_bytes(pm._transfer_path(ref).read_bytes())
        self.assertIsNone(self.parse(text))

    def test_pinned_opencode_regex_and_writer_leave_new_sender_unattributed(self):
        source = pinned_source("adapters/opencode/plugins/hearting-guards.js")
        old_python = pinned_source("utilities/peer-message.py")
        scope = {"__file__": str(ROOT / "utilities/peer-message.py"), "__name__": "pinned_peer"}
        exec(compile(old_python, "pinned-peer-message", "exec"), scope)
        js = r'''
const fs = require('fs'), vm = require('vm'), path = require('path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const fn = input.source.slice(input.source.indexOf('const peerTrailerRe ='), input.source.indexOf('\nfunction promptText('));
let result = {};
vm.runInNewContext(fn + '\nspawnPeerNotice("recipient-a", prompt, root)', {
  path, root: input.root, prompt: input.prompt, process: {env: {}},
  spawn: (exe, args) => { result = {exe, args}; return {
    on: () => {}, unref: () => {}, stdin: {end: body => {result.body = body;}}
  }; }
});
process.stdout.write(JSON.stringify(result));
'''
        for harness in ("codex", "claude"):
            text, _ = self.prepare(sender=dict(self.sender, harness=harness),
                                   recipient={"harness": "opencode", "session_id": "recipient-a"})
            proc = subprocess.run(["node", "-e", js],
                                  input=json.dumps({"root": str(ROOT), "source": source, "prompt": text}),
                                  text=True, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            call = json.loads(proc.stdout)
            with mock.patch.object(sys, "stdin", io.StringIO(call["body"])):
                self.assertEqual(scope["main"](call["args"][1:]), 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row["from"]["session_id"] == "" for row in self.rows()))

    def test_python_receive_cli_exact_and_wrong_recipient(self):
        for sid in ("recipient-a", "new-occupant"):
            text, _ = self.prepare(recipient={"harness": "opencode", "session_id": "recipient-a"})
            proc = subprocess.run([sys.executable, str(ROOT / "utilities/peer-message.py"), "receive",
                                   "--to-harness", "opencode", "--to-session-id", sid],
                                  input=text, text=True, capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["to"]["session_id"]: r["from"]["session_id"] for r in rows},
                         {"recipient-a": self.sender["session_id"], "new-occupant": ""})

    def test_claude_and_codex_actual_receiver_hooks(self):
        claude = module("alias_claude_hook", "hooks/peer-message-record.py")
        codex = module("alias_codex_hook", "adapters/codex/hooks/userprompt-lifecycle.py")
        for harness, hook in (("claude", claude), ("codex", codex)):
            for target in ("recipient-a", "new-occupant"):
                text, _ = self.prepare(recipient={"harness": harness, "session_id": "recipient-a"})
                payload = {"session_id": target, "cwd": str(ROOT), "prompt": text}
                if harness == "claude":
                    hook.handle_prompt(payload)
                else:
                    with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "ambient-wrong"}):
                        hook.peer_notice(payload, text, str(ROOT))
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["from"]["session_id"], self.sender["session_id"] if row["to"]["session_id"] == "recipient-a" else "")

    def test_long_original_remains_readable_after_each_harness_receipt(self):
        tail = "실행하지 말 것; 확인 답장에는 마지막 조건만 인용\nSTFT 크기 하한 1e-5·위상 유지"
        body = "[steer] preserve every instruction\n" + "evidence detail with English and 한글 " * 240 + "\n" + tail
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                recipient = {"harness": harness, "session_id": f"{harness}-recipient"}
                sender = dict(self.sender, session_id=f"sender-{harness}")
                text, ref = self.prepare_legacy_reference(body, sender, recipient)
                self.assertLessEqual(len(text.encode("utf-8")), 1024)
                self.assertNotIn("STFT 크기 하한", text)
                path = Path(pm.read_peer_payload(ref)["path"])
                self.assertTrue(path.is_absolute())
                self.assertEqual(path.read_bytes(), body.encode("utf-8"))
                guidance = pm.receiver_guidance(text, recipient, cwd=str(ROOT))
                if harness == "codex":
                    self.assertIn("Open the complete referenced file before acting", guidance)
                else:
                    self.assertIn(body, guidance)
                    self.assertIn("Do not act on the bounded preview", guidance)

                self._receive_actual(harness, text, recipient["session_id"])
                self.assertEqual(pm._read_pending(ref)["state"], "received")
                self.assertIsNone(pm._read_pending(ref)["text"])
                self.assertEqual(pm.read_peer_payload(ref)["body"], body)
                self.assertIn(tail, path.read_bytes().decode("utf-8"))

    def test_opencode_plugin_forwards_actual_prompt_to_canonical_cli(self):
        # Run the unchanged plugin function in a VM with a recording transport;
        # then execute the captured Python CLI synchronously in the temp root.
        text, _ = self.prepare(recipient={"harness": "opencode", "session_id": "recipient-a"})
        js = r'''
const fs = require('fs'), vm = require('vm'), path = require('path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.root + '/adapters/opencode/plugins/hearting-guards.js', 'utf8');
const fn = source.slice(source.indexOf('function spawnPeerNotice('), source.indexOf('\nfunction promptText('));
let result = {};
vm.runInNewContext(fn + '\nspawnPeerNotice(sid, prompt, root)', {
  path, root: input.root, sid: input.sid, prompt: input.prompt, process: {env: {}},
  spawn: (exe, args) => { result = {exe, args}; return {
    on: () => {}, unref: () => {}, stdin: {end: body => {result.body = body;}}
  }; }
});
process.stdout.write(JSON.stringify(result));
'''
        for sid in ("recipient-a", "new-occupant"):
            proc = subprocess.run(["node", "-e", js], input=json.dumps({"root": str(ROOT), "sid": sid, "prompt": text}),
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            call = json.loads(proc.stdout)
            self.assertEqual(call["body"], text)
            self.assertIn("receive", call["args"])
            received = subprocess.run([call["exe"], *call["args"]], input=call["body"], text=True, capture_output=True, timeout=5)
            self.assertEqual(received.returncode, 0, received.stderr)
        self.assertEqual(len(self.rows()), 2)
        for row in self.rows():
            self.assertEqual(row["from"]["session_id"], self.sender["session_id"] if row["to"]["session_id"] == "recipient-a" else "")


class PendingCallbacks(unittest.TestCase):
    setUp = AliasReceive.setUp
    rows = AliasReceive.rows

    def test_opencode_peer_guidance_is_read_only_until_native_history_consumption(self):
        recipient = {"harness": "opencode", "session_id": "fixture-oc"}
        body = "[handoff] exact card\n" + "complete context " * 260 + "\nDo not run; final instruction: preserve the original"
        text, ref = AliasReceive.prepare_legacy_reference(self, body, self.sender, recipient)
        guidance = pm.receiver_guidance(text, recipient, cwd=str(ROOT))
        self.assertIn("immediate results once", guidance)
        self.assertIn("Receipt/queue/ACK is not execution", guidance)
        self.assertNotIn("handoff accept " + ref, guidance)
        self.assertNotIn("acceptance", pm._read_pending(ref))
        self.assertEqual(pm._read_pending(ref)["state"], "pending")
        self.assertEqual(self.rows(), [])

    def test_limited_or_changed_receiver_gets_verified_body_in_native_context_without_resealing(self):
        body = "[steer] full source\n" + "supporting detail " * 180 + "\nDo not execute; final condition: keep sign"
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                recipient = {"harness": harness, "session_id": harness + "-limited"}
                limited_root = self.state / "different-limited-read-root"
                limited_root.mkdir(exist_ok=True)
                if harness == "claude":
                    recipient.update(cwd=str(limited_root), read_roots=[str(limited_root)])
                text, ref = AliasReceive.prepare_legacy_reference(self, body, self.sender, recipient)
                original_transfer = pm._read_transfer_record(ref)
                changed = {"harness": "claude", "session_id": "replacement-session"}
                if harness == "codex":
                    changed_guidance = pm.receiver_guidance(text, changed)
                    self.assertIn(body, changed_guidance)
                    self.assertIn("do not execute it in this session", changed_guidance)
                    self.assertEqual(pm.receive_peer_message(text, changed), 0)
                    self.assertEqual(pm._read_pending(ref)["state"], "pending")
                    import codex_queue_delivery as queue
                    import codex_queue_dispatch as queue_dispatch
                    with mock.patch.object(pm, "_codex_model_read_access", return_value=False), \
                         mock.patch.object(queue_dispatch, "resolve_queue_socket", return_value="fixture-socket"), \
                         mock.patch.object(queue, "send_at_least_once", return_value={"status": "queued"}) as send:
                        self.assertEqual(pm.deliver_pending_codex(ref)["status"], "queued")
                    queued = pm._read_pending(ref)
                    self.assertTrue(queued["reference_context_fallback"])
                    self.assertEqual(queued["text"], text)
                    self.assertEqual(send.call_args.kwargs["message"], text)
                    self.assertEqual(pm._read_transfer_record(ref), original_transfer)
                self.assertEqual(pm.receive_peer_message(text, recipient), 0)
                if harness == "codex":
                    guidance = pm.receiver_guidance(text, recipient, cwd=str(limited_root))
                else:
                    guidance = pm.receiver_guidance(text, recipient,
                        cwd=str(limited_root))
                self.assertIn(body, guidance)
                self.assertIn("Do not act on the bounded preview", guidance)
                self.assertIn("final condition: keep sign", guidance)
                self.assertEqual(pm._read_transfer_record(ref), original_transfer)

                if harness == "codex":
                    self.assertEqual(pm._read_pending(ref)["state"], "received")

                if harness == "claude":
                    output = io.StringIO()
                    pm.emit_peer_context(recipient, "UserPromptSubmit",
                                         extra_context=guidance, stream=output)
                    native = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
                else:
                    hook = module("fallback_codex_hook", "adapters/codex/hooks/userprompt-lifecycle.py")
                    output = io.StringIO()
                    with mock.patch("sys.stdout", output):
                        hook.emit_context("UserPromptSubmit", [guidance])
                    native = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
                self.assertIn(body, native)
                self.assertIn("Do not act on the bounded preview", native)
                self.assertIn("final condition: keep sign", native)

    def test_unreadable_original_is_blocked_in_native_context_without_receipt(self):
        recipient = {"harness": "codex", "session_id": "codex-unreadable"}
        body = "[steer] original unavailable\n" + "source detail " * 200 + "\nfinal condition: keep phase"
        text, ref = AliasReceive.prepare_legacy_reference(self, body, self.sender, recipient)
        payload_path = Path(pm.read_peer_payload(ref)["path"])
        payload_path.unlink()
        self.assertEqual(pm.receive_peer_message(text, recipient), 1)
        guidance = pm.receiver_guidance(text, recipient)
        hook = module("unreadable_codex_hook", "adapters/codex/hooks/userprompt-lifecycle.py")
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            hook.emit_context("UserPromptSubmit", [guidance])
        native = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Do not act on this preview", native)
        self.assertIn("existing continuation", native)
        pending = json.loads(pm._pending_path(ref).read_text())
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["text"], text)
        self.assertFalse([row for row in self.rows() if row.get("transfer_ref") == ref])
        pm._transfer_path(ref).unlink()
        missing_seal = pm.receiver_guidance(text, recipient)
        self.assertIn("transfer seal is missing or invalid", missing_seal)
        self.assertIn("Do not act on this preview", missing_seal)

    def test_opencode_normal_registration_and_callback_observation_is_bounded_and_fail_soft(self):
        js = r'''
import { pathToFileURL } from "node:url";
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js"));
const logs = [];
process.env.HERDR_PANE_ID = "";
const hooks = await AgentHarnessGuards({client: {app: {log: async ({body}) => logs.push(body)}}});
if (typeof hooks.dispose !== "function") throw Error("normal host disposal missing");
await hooks["tool.execute.after"]({sessionID: "actual-callback", tool: "fixture-noop", args: {secret: "private-tool-body"}}, {});
await hooks["tool.execute.after"]({sessionID: "actual-callback", tool: "fixture-noop", args: {}}, {});
const failing = await AgentHarnessGuards({client: {app: {log: () => {throw Error("private-error")}}}});
await failing["tool.execute.after"]({sessionID: "another-callback", tool: "fixture-noop", args: {}}, {});
console.log(JSON.stringify(logs));
'''
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=dict(os.environ),
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        logs = json.loads(run.stdout)
        self.assertEqual([row["extra"]["reason"] for row in logs], ["plugin-registered", "callback-no-pane"])
        self.assertEqual([row["extra"]["stage"] for row in logs], ["plugin", "callback"])
        self.assertTrue(all(row["level"] == "info" and row["extra"]["module"] == "hearting-peer-identity" for row in logs))
        self.assertEqual(logs[1]["extra"]["sessionID"], "actual-callback")
        self.assertNotIn("private-tool-body", run.stdout)
        self.assertNotIn("private-error", run.stdout)

    def test_opencode_publisher_observation_preserves_attempt_receipt_and_failure_boundaries(self):
        js = r'''
import { readFileSync } from "node:fs";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import vm from "node:vm";
const source = readFileSync(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js", "utf8");
const logs = [], timers = [];
const scope = {process: {env: {}}, isWorkerSession: () => false, Buffer,
  setTimeout: cb => {const t = {cb, unref() {}}; timers.push(t); return t}, clearTimeout: () => {}};
function paneProjectionSource(source) {
  const start = source.indexOf("function sdkResponseData(");
  const exportStart = source.indexOf("\nexport default {\n", start);
  const exportEnd = source.indexOf("\n}\n\n// Invocation-time root", exportStart);
  const end = source.indexOf("\nfunction collectPreflight", exportEnd);
  if (start < 0 || exportStart < start || exportEnd < exportStart || end < exportEnd)
    throw Error("OpenCode pane fixture extraction boundary changed");
  const moduleExport = source.slice(exportStart, exportEnd + 2);
  if (!moduleExport.includes('id: "hearting-guards"')
      || !moduleExport.includes("setup: setupOpenCodeV2")
      || !moduleExport.includes("AgentHarnessGuards"))
    throw Error("OpenCode V2 plugin export contract changed");
  return source.slice(start, exportStart) + source.slice(exportEnd + 2, end);
}
vm.runInNewContext(paneProjectionSource(source), scope);
const ctx = {client: {app: {log: async ({body}) => logs.push(body)}}};
const observation = {schema: "hearting-pane-observation-v1", reason: "report-attempts-finished",
  session_report: "exit0", metadata_report: "timeout", session_report_rc: 0};
async function emit(text, code = 0, action = "close") {
  const child = new EventEmitter(); child.stdout = new PassThrough(); child.kill = () => {throw Error("no helper signal")};
  scope.observePanePublisher(child, ctx, "exact", 0);
  if (text) child.stdout.write(text);
  await new Promise(resolve => setImmediate(resolve));
  if (action === "timeout") timers.at(-1).cb();
  if (action === "error") child.emit("error", Object.assign(Error("synthetic spawn denied"), {code: "EACCES", errno: -13}));
  if (action === "read-error") child.stdout.emit("error", Error("private-read-error"));
  child.emit("close", code);
  return child;
}
await emit(JSON.stringify(observation));
await emit(JSON.stringify({schema: "hearting-pane-observation-v1", reason: "guard-refused",
  session_report: "not-attempted", metadata_report: "not-attempted"}));
await emit("private raw text");
await emit(JSON.stringify(observation) + "\n" + JSON.stringify(observation));
await emit("가".repeat(400)); // 1200 UTF-8 bytes, below 1024 characters.
await emit("", 7);
await emit("", 0, "error");
await emit("", 0, "read-error");
const late = await emit("", 0, "timeout");
late.emit("close", 0); // Timeout result cannot later become an attempt success.
await emit(JSON.stringify({...observation, sessionID: "foreign", body: "private-body"}));
const stale = new EventEmitter(); stale.stdout = new PassThrough();
scope.observePanePublisher(stale, ctx, "old", -1);
stale.stdout.write(JSON.stringify(observation));
await new Promise(resolve => setImmediate(resolve)); stale.emit("close", 0);
scope.peerIdentityLog(ctx, "callback", "callback-entry", {sessionID: "x".repeat(10000)});
console.log(JSON.stringify(logs));
'''
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=dict(os.environ),
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        logs = json.loads(run.stdout)
        self.assertEqual([row["extra"]["reason"] for row in logs], [
            "report-attempts-finished", "guard-refused", "publisher-observation-invalid",
            "publisher-observation-invalid", "publisher-observation-overflow", "publisher-exit-error",
            "publisher-spawn-error", "publisher-observation-read-error", "publisher-observation-timeout", "publisher-observation-invalid",
            "publisher-stale-observation", "callback-entry"])
        self.assertEqual(logs[0]["extra"]["sessionReport"], "exit0")
        self.assertEqual(logs[0]["extra"]["metadataReport"], "timeout")
        self.assertEqual({key: logs[6]["extra"][key] for key in ["errorCode", "errorErrno", "errorMessage"]},
                         {"errorCode": "EACCES", "errorErrno": -13, "errorMessage": "synthetic spawn denied"})
        self.assertNotIn("sessionReport", logs[-2]["extra"])
        self.assertNotIn("sessionID", logs[-1]["extra"])
        self.assertTrue(logs[-1]["extra"]["sessionIDInvalid"])
        self.assertNotIn("private", run.stdout)
        self.assertNotIn("received", run.stdout)

    def run_pane_projection_fixture(self, body, timeout=5):
        # Fixed native invocation/SDK/child fixtures; expected results below are literals.
        js = r'''
import { existsSync as fsExistsSync, readFileSync, readFileSync as fsReadFileSync } from "node:fs";
import path from "node:path";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import vm from "node:vm";
const source = readFileSync(process.env.AGENT_HOME + "/adapters/opencode/plugins/hearting-guards.js", "utf8");
function fixture(options = {}) {
  const commands = [], children = [], logs = [], reads = [], timers = [], sync = [];
  let now = 1000;
  const native = options.command || Buffer.from((options.argv || ["/native/opencode", "--session", "ses_A"]).join("\0") + "\0");
  const files = {"/proc/self/cmdline": native,
    "/proc/self/stat": Buffer.from(options.stat || "123 (opencode) S " + "0 ".repeat(18) + "77 0")};
  const scope = {path, Buffer, process: {pid: 123, env: Object.assign(
      {HERDR_PANE_ID: "fixture-pane", OPENCODE_SESSION_ID: "ses_foreign"}, options.env || {})},
    isWorkerSession: () => !!options.worker, existsSync: fsExistsSync, readFileSync: fsReadFileSync,
    root: options.root || "fixture-root", herdrProjection: options.herdrProjection || "fixture-root/tools/fleet/herdr_projection.py",
    Date: {now: () => now}, AbortController,
    openSync: file => {reads.push(file);if (!files[file]) throw Error("foreign proc read");return file},
    readSync: (fd, buffer, offset, size) => files[fd].copy(buffer, offset, 0, size), closeSync: () => {},
    realpathSync: file => file === "/proc/self/cwd" ? "/fixture/project" : file,
    setTimeout: (cb, ms) => {const timer = setTimeout(cb, ms);timers.push({cb, ms, timer});return timer}, clearTimeout,
    spawn: (exe, args) => {
      commands.push(args);
      if (options.spawnThrows) {options.spawnThrows = false;throw Object.assign(Error("synthetic spawn failure"), {code: "EACCES", errno: -13})}
      const child = new EventEmitter();child.pid = options.spawnNoPid ? undefined : children.length + 100;
      child.stdout = new PassThrough();child.unref = () => {};child.kill = () => {throw Error("no signals")};
      children.push(child);return child;
    },
    spawnSync: (exe, args, config) => {
      sync.push({exe, args, config});
      if (options.syncThrows) throw Object.assign(Error("synthetic sync failure"), {code: "EFAULT", errno: -14});
      return options.syncResult || {pid: 0, error: Object.assign(Error("synthetic fallback denied"), {code: "EACCES", errno: -13})};
    }};
  function paneProjectionSource(source) {
    const start = source.indexOf("function sdkResponseData(");
    const exportStart = source.indexOf("\nexport default {\n", start);
    const exportEnd = source.indexOf("\n}\n\n// Invocation-time root", exportStart);
    const end = source.indexOf("\nfunction collectPreflight", exportEnd);
    if (start < 0 || exportStart < start || exportEnd < exportStart || end < exportEnd)
      throw Error("OpenCode pane fixture extraction boundary changed");
    const moduleExport = source.slice(exportStart, exportEnd + 2);
    if (!moduleExport.includes('id: "hearting-guards"')
        || !moduleExport.includes("setup: setupOpenCodeV2")
        || !moduleExport.includes("AgentHarnessGuards"))
      throw Error("OpenCode V2 plugin export contract changed");
    return source.slice(start, exportStart) + source.slice(exportEnd + 2, end);
  }
  vm.runInNewContext(paneProjectionSource(source), scope);
  return {scope, commands, children, logs, reads, timers, sync, setNow: value => {now = value},
    ctx: get => ({directory: "/fixture/project", client: {app: {log: async ({body}) => logs.push(body)},
      session: {get: get || (async () => ({data: {id: "ses_A"}}))}}}),
    exit: index => {children[index].emit("exit", 0);children[index].emit("close", 0)}};
}
''' + body
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=dict(os.environ),
                             capture_output=True, text=True, timeout=timeout)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def run_agent_initializer_fixture(self, body, timeout=10, pane_id=True, worker=False, pending_ref=None,
                                      bare_origin=False, selection_sid="ses_A",
                                      disable_intervals=False, pending_command_timeout_ms=0):
        """Run the actual AgentHarnessGuards initializer with isolated native/runtime edges."""
        source = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text()
        origin_start = source.index("function ownNativePaneOrigin() {")
        origin_end = source.index("\n}\n\nfunction sameNativePaneContext", origin_start) + 2
        source = source[:origin_start] + '''function ownNativePaneOrigin() {
  const bare = process.env.FIXTURE_BARE_NATIVE_ORIGIN === "1"
  const stat = readFileSync("/proc/self/stat", "utf8")
  const close = stat.lastIndexOf(") ")
  const start = stat.slice(close + 2).trim().split(/\\s+/)[19]
  return {pid: process.pid, start, sid: bare ? null : "ses_A", bare,
    directory: realpathSync(process.cwd()), state: "pending"}
}''' + source[origin_end:]
        source = source.replace('import { spawnSync, spawn } from "node:child_process"',
            "const spawnSync = globalThis.__fixtureSpawnSync; const spawn = globalThis.__fixtureSpawn")
        source = source.replace("markPluginLoaded(dispatchSlug())", "// fixture: plugin marker disabled")
        source = source.replace("if (!isWorkerSession()) reconnectPeerObligations()", "if (!isWorkerSession()) {}")
        source += '''\nexport const __fixturePaneState = () => {
  const binding = paneOwningContext && paneContexts.get(paneOwningContext)
  return {originState: paneNativeOrigin?.state, timerQueued: !!binding?.startupQueued,
    startupRunning: !!binding?.startupRunning, refreshPending: !!binding?.refreshPending,
    hasPublisher: !!panePublisherSlot, busy: [...paneProjectionBusy.keys()],
    originSID: paneNativeOrigin?.sid, bindingOwnsOrigin: !!binding?.ownsOrigin,
    ownsContext: !!paneOwningContext}
}\n'''
        with tempfile.TemporaryDirectory() as tmp:
            module_path = Path(tmp) / "hearting-guards-fixture.mjs"
            module_path.write_text(source)
            fixture_state = Path(tmp) / "state"
            js = r'''
import { pathToFileURL } from "node:url";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { spawnSync as realChildSpawnSync } from "node:child_process";
const realSetTimeout = globalThis.setTimeout.bind(globalThis);
const realClearTimeout = globalThis.clearTimeout.bind(globalThis);
const realClearInterval = globalThis.clearInterval.bind(globalThis);
globalThis.__fixtureTimers = [];
globalThis.__fixtureChildren = [];
globalThis.__fixturePublishers = [];
globalThis.__fixtureSyncCalls = [];
globalThis.setTimeout = (cb, ms, ...args) => {
  if (ms === 0) {
    const timer = {cb, cancelled: false, startupTimer: true};
    globalThis.__fixtureTimers.push(timer);
    return timer;
  }
  return realSetTimeout(cb, ms, ...args);
};
globalThis.clearTimeout = timer => {
  if (timer?.startupTimer) { timer.cancelled = true; return; }
  return realClearTimeout(timer);
};
if (process.env.FIXTURE_DISABLE_INTERVALS === "1") {
  globalThis.setInterval = (cb, ms) => ({fixtureInterval: true, cb, ms, unref() {}});
  globalThis.clearInterval = timer => timer?.fixtureInterval ? undefined : realClearInterval(timer);
}
globalThis.__fixturePendingRow = process.env.FIXTURE_PENDING_ROW
  ? JSON.parse(process.env.FIXTURE_PENDING_ROW) : null;
globalThis.__fixtureSpawnSync = (exe, args, options) => {
  globalThis.__fixtureSyncCalls.push({exe, args, options});
  if (Array.isArray(args) && args.some(value => String(value).endsWith("peer-message.py"))) {
    const intent = args.slice(6).find(value => ["--claim", "--queued", "--resuming", "--received",
      "--reserve", "--begin-write", "--release-reservation"].includes(value));
    if (args[1] === "pending" && !intent && globalThis.__fixturePendingRow) {
      const row = globalThis.__fixturePendingRow;
      return {status: 0, stdout: JSON.stringify(row.state === "received" ? [] : [row]), stderr: ""};
    }
    // Keep this isolated semantic fixture deterministic when repeated cold
    // Python entrypoints contend with the full suite; production keeps its
    // own shorter callback timeout.
    const fixtureTimeout = Number(process.env.FIXTURE_PENDING_COMMAND_TIMEOUT_MS || 0);
    const commandOptions = fixtureTimeout > 0 && args.some(value => String(value).endsWith("peer-message.py"))
      ? {...(options || {}), timeout: fixtureTimeout} : options;
    const result = realChildSpawnSync(exe, args, commandOptions);
    if (result.status === 0 && globalThis.__fixturePendingRow) {
      if (intent === "--claim") {
        globalThis.__fixturePendingRow.state = "unverified";
        globalThis.__fixturePendingRow.receipt = "context-api-inflight";
      } else if (intent === "--reserve" || intent === "--begin-write") {
        // Keep the fixture's subsequent list calls in sync with the real
        // helper's persisted transition and claimant token.
        globalThis.__fixturePendingRow = JSON.parse(result.stdout);
      } else if (intent === "--release-reservation") {
        globalThis.__fixturePendingRow = {...globalThis.__fixturePendingRow,
          rpc_claim: null, receipt: "opencode-prompt-reservation-released"};
      } else if (intent === "--queued") {
        globalThis.__fixturePendingRow.state = "queued";
        globalThis.__fixturePendingRow.actual_message_id = args[args.indexOf("--message-id") + 1];
        globalThis.__fixturePendingRow.receipt = "context-api-accepted";
      } else if (intent === "--resuming") {
        globalThis.__fixturePendingRow.resume_attempted = true;
      } else if (intent === "--received") {
        globalThis.__fixturePendingRow.state = "received";
        globalThis.__fixturePendingRow.text = null;
        globalThis.__fixturePendingRow.receipt = "exact-native-history";
      }
      if (["--claim", "--reserve", "--begin-write"].includes(intent) && result.status === 0) {
        return {status: 0, stdout: JSON.stringify(globalThis.__fixturePendingRow), stderr: ""};
      }
    }
    return result;
  }
  return {status: 0, stdout: ""};
};
globalThis.__fixtureSpawn = (exe, args, options) => {
  const child = new EventEmitter();
  child.args = Array.from(args);
  child.pid = 700 + globalThis.__fixtureChildren.length;
  child.stdout = new PassThrough();
  child.stdin = new PassThrough();
  child.unref = () => {};
  child.kill = () => { throw Error("fixture must not signal children"); };
  globalThis.__fixtureChildren.push(child);
  if (child.args[0].endsWith("herdr_projection.py")) {
    globalThis.__fixturePublishers.push(child);
    setImmediate(() => child.emit("spawn"));
  } else {
    setImmediate(() => { child.stdout.end("null"); child.emit("close", 0); });
  }
  return child;
};
const guardModule = await import(pathToFileURL(process.env.FIXTURE_GUARDS).href);
const {AgentHarnessGuards, __fixturePaneState} = guardModule;
const settleTurn = () => new Promise(resolve => setImmediate(resolve));
const fireStartupTimer = index => {
  const timer = globalThis.__fixtureTimers[index];
  if (!timer || timer.cancelled) throw Error("startup timer missing or cancelled");
  timer.cb();
};
if (process.env.FIXTURE_BARE_NATIVE_ORIGIN === "1") {
  const fs = await import("node:fs");
  const path = await import("node:path");
  const directory = path.join(process.env.XDG_STATE_HOME, "hearting", "tui-identity");
  const stat = fs.readFileSync("/proc/self/stat", "utf8");
  const close = stat.lastIndexOf(") ");
  const start = stat.slice(close + 2).trim().split(/\s+/)[19];
  fs.mkdirSync(directory, {recursive: true});
  fs.writeFileSync(path.join(directory, `${process.pid}-${start}.json`), JSON.stringify({
    schema: "hearting-tui-selection-v1", sessionID: process.env.FIXTURE_SELECTION_SID,
    pid: process.pid, start,
  }), {mode: 0o600});
}
''' + body
            env = dict(os.environ)
            for key in tuple(env):
                if key.startswith(("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_", "HERDR_")):
                    env.pop(key, None)
            env.pop("OPENCODE_DISPATCH_SLUG", None)
            env.update({"AGENT_HOME": str(self.agent_home), "AGENT_ARTIFACT_CHECKPOINT": "off",
                        "FIXTURE_GUARDS": str(module_path),
                        "XDG_STATE_HOME": str(fixture_state),
                        "FIXTURE_BARE_NATIVE_ORIGIN": "1" if bare_origin else "0",
                        "FIXTURE_DISABLE_INTERVALS": "1" if disable_intervals else "0",
                        "FIXTURE_PENDING_COMMAND_TIMEOUT_MS": str(pending_command_timeout_ms),
                        "FIXTURE_SELECTION_SID": selection_sid})
            if pending_ref:
                env["FIXTURE_PENDING_ROW"] = json.dumps(pm._read_pending(pending_ref))
            else:
                rows = pm.pending_messages({"harness": "opencode", "session_id": "ses_A"})
                if len(rows) == 1:
                    env["FIXTURE_PENDING_ROW"] = json.dumps(rows[0])
                else:
                    env.pop("FIXTURE_PENDING_ROW", None)
            if pane_id:
                env["HERDR_PANE_ID"] = "fixture-pane"
            else:
                env.pop("HERDR_PANE_ID", None)
            if worker:
                env["AGENT_SESSION_ROLE"] = "worker"
            run = subprocess.run(["node", "--input-type=module", "-e", js], env=env,
                                 capture_output=True, text=True, timeout=timeout)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def run_pending_agent_initializer_fixture(self, body, ref=None, timeout=10, bare_origin=False,
                                              selection_sid="ses_A", disable_intervals=False):
        if ref is None:
            rows = pm.pending_messages({"harness": "opencode", "session_id": "ses_A"})
            if len(rows) != 1:
                raise AssertionError(f"expected one exact pending OpenCode row, got {len(rows)}")
            ref = rows[0]["ref"]
        return self.run_agent_initializer_fixture(body, timeout=timeout, pending_ref=ref,
                                                  bare_origin=bare_origin, selection_sid=selection_sid,
                                                  disable_intervals=disable_intervals,
                                                  pending_command_timeout_ms=5000)

    def test_opencode_agent_initializer_returns_before_sdk_and_publishes_resume_once(self):
        result = self.run_agent_initializer_fixture(r'''
let returned = false, sdkCalls = 0, prompts = 0, logs = [];
const ctx = {directory: process.cwd(), worktree: process.cwd(), client: {
  app: {log: async ({body}) => logs.push(body)}, session: {
    get: async ({path, signal}) => {
      sdkCalls++;
      if (!returned) return await new Promise(() => {});
      if (path.id !== "ses_A" || !signal) throw Error("wrong exact SDK lookup");
      return {data: {id: "ses_A", parentID: null}};
    },
    promptAsync: async () => { prompts++; throw Error("promptAsync is outside this fixture"); },
  },
}};
const hooks = await Promise.race([AgentHarnessGuards(ctx), new Promise((_, reject) =>
  realSetTimeout(() => reject(Error("initializer awaited SDK readiness")), 500))]);
returned = true;
if (sdkCalls !== 0 || globalThis.__fixtureTimers.length !== 1) throw Error("initializer barrier not respected");
fireStartupTimer(0);
await settleTurn(); await settleTurn();
const args = globalThis.__fixturePublishers.map(child => child.args);
if (sdkCalls !== 1 || args.length !== 1) throw Error("resume did not publish exactly once");
if (!args[0].includes("--no-report-session") || args[0].includes("--session-start-source")) throw Error("Herdr owns lifecycle reporting");
const state = __fixturePaneState();
hooks.dispose();
globalThis.__fixturePublishers[0].emit("exit", 0);
globalThis.__fixturePublishers[0].emit("close", 0);
if (prompts !== 0) throw Error("fixture ran promptAsync");
console.log(JSON.stringify({sdkCalls, args, state, prompts, logs: logs.map(row => row.extra?.reason)}));
''')
        self.assertEqual(result["sdkCalls"], 1)
        self.assertEqual(len(result["args"]), 1)
        args = result["args"][0]
        self.assertIn("--no-report-session", args)
        self.assertNotIn("--session-start-source", args)
        self.assertEqual(result["state"]["originState"], "pending")
        self.assertTrue(result["state"]["hasPublisher"])  # one child held the live slot
        self.assertEqual(result["prompts"], 0)

    def test_opencode_startup_and_normal_callbacks_keep_single_flight_and_pending_refresh(self):
        result = self.run_agent_initializer_fixture(r'''
let returned = false, sdkCalls = 0, releaseFirst, prompts = 0;
const ctx = {directory: process.cwd(), worktree: process.cwd(), client: {
  app: {log: async () => {}}, session: {
    get: async ({path}) => {
      if (path.id !== "ses_A") throw Error("wrong SID");
      sdkCalls++;
      if (sdkCalls === 1) return await new Promise(resolve => {releaseFirst = resolve});
      return {data: {id: "ses_A", parentID: null}};
    },
    promptAsync: async () => { prompts++; throw Error("promptAsync must not run"); },
  },
}};
const hooks = await AgentHarnessGuards(ctx); returned = true;
const callback = () => hooks["chat.message"]({sessionID: "ses_A", messageID: "fixture-turn"},
  {message: {sessionID: "ses_A", agent: "title"}, parts: []});
const beforeTimer = callback();
await settleTurn();
const duringWait = callback();
await duringWait;
fireStartupTimer(0);
await settleTurn();
const waiting = __fixturePaneState();
if (sdkCalls !== 1 || globalThis.__fixturePublishers.length !== 0) throw Error("busy SDK call duplicated");
releaseFirst({data: {id: "ses_A", parentID: null}});
await beforeTimer; await settleTurn();
const firstCount = globalThis.__fixturePublishers.length;
const whileLive = callback(); await whileLive;
const liveState = __fixturePaneState();
const first = globalThis.__fixturePublishers[0];
first.emit("exit", 0); first.emit("close", 0);
await callback(); await settleTurn();
const afterRefresh = __fixturePaneState();
const args = globalThis.__fixturePublishers.map(child => child.args);
if (sdkCalls !== 2 || firstCount !== 1 || args.length !== 2) throw Error("normal callback refresh was lost or duplicated");
if (liveState.refreshPending !== true || afterRefresh.refreshPending !== false) throw Error("pending refresh lifecycle mismatch");
if (globalThis.__fixturePublishers[1] === first) throw Error("publisher fixture identity reused");
hooks.dispose();
globalThis.__fixturePublishers[1].emit("exit", 0);
globalThis.__fixturePublishers[1].emit("close", 0);
if (prompts !== 0) throw Error("fixture ran promptAsync");
console.log(JSON.stringify({sdkCalls, args, waiting, liveState, afterRefresh, prompts}));
''')
        self.assertEqual(result["sdkCalls"], 2)
        self.assertEqual(len(result["args"]), 2)
        first, second = result["args"]
        self.assertIn("--no-report-session", first)
        self.assertIn("--no-report-session", second)
        self.assertNotIn("--session-start-source", first + second)
        self.assertEqual(result["waiting"]["busy"], ["ses_A"])
        self.assertFalse(result["waiting"]["refreshPending"])
        self.assertTrue(result["liveState"]["refreshPending"])
        self.assertFalse(result["afterRefresh"]["refreshPending"])
        self.assertEqual(result["prompts"], 0)

    def test_opencode_native_status_events_publish_exact_pane_session_and_process_birth(self):
        result = self.run_agent_initializer_fixture(r'''
const ctx = {directory: process.cwd(), worktree: process.cwd(), client: {
  app: {log: async () => {}}, session: {get: async () => ({data: {id: "ses_A"}})},
}};
const hooks = await AgentHarnessGuards(ctx);
await hooks.event({event: {type: "session.status", properties: {
  sessionID: "ses_A", status: {type: "busy"}}}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses_A"}}});
const rows = globalThis.__fixtureSyncCalls
  .filter(call => call.args[0].endsWith("herdr_observation.py"))
  .map(call => JSON.parse(call.options.input));
hooks.dispose();
console.log(JSON.stringify(rows));
''')
        self.assertEqual([row["status"] for row in result], ["working", "idle"])
        for row in result:
            self.assertEqual((row["endpoint"], row["pane"], row["harness"], row["session_id"]),
                             ("default", "fixture-pane", "opencode", "ses_A"))
            self.assertGreater(row["pid"], 1)
            self.assertRegex(row["pid_start"], r"^\d+$")

    def test_opencode_bare_native_status_uses_current_selection_and_rejects_old_callback(self):
        result = self.run_agent_initializer_fixture(r'''
const fs = await import("node:fs");
const path = await import("node:path");
const ctx = {directory: process.cwd(), worktree: process.cwd(), client: {
  app: {log: async () => {}}, session: {get: async () => ({data: {id: "ses_A"}})},
}};
const hooks = await AgentHarnessGuards(ctx);
await hooks.event({event: {type: "session.status", properties: {
  sessionID: "ses_A", status: {type: "idle"}}}});
const stat = fs.readFileSync("/proc/self/stat", "utf8");
const close = stat.lastIndexOf(") ");
const start = stat.slice(close + 2).trim().split(/\s+/)[19];
const selection = path.join(process.env.XDG_STATE_HOME, "hearting", "tui-identity", `${process.pid}-${start}.json`);
const row = JSON.parse(fs.readFileSync(selection, "utf8"));
row.sessionID = "ses_B";
fs.writeFileSync(selection, JSON.stringify(row));
await hooks.event({event: {type: "session.status", properties: {
  sessionID: "ses_A", status: {type: "idle"}}}});
const reports = globalThis.__fixtureSyncCalls
  .filter(call => call.args[0].endsWith("herdr_observation.py") && call.args[1] === "record-native-turn")
  .map(call => JSON.parse(call.options.input));
hooks.dispose();
console.log(JSON.stringify(reports));
''', bare_origin=True, selection_sid="ses_A")
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual((row["harness"], row["session_id"], row["status"]),
                         ("opencode", "ses_A", "idle"))
        self.assertEqual((row["endpoint"], row["pane"]), ("default", "fixture-pane"))
        self.assertGreater(row["pid"], 1)
        self.assertRegex(row["pid_start"], r"^\d+$")

    def test_opencode_dispose_cancels_the_initializer_startup_timer(self):
        result = self.run_agent_initializer_fixture(r'''
let sdkCalls = 0;
const ctx = {directory: process.cwd(), worktree: process.cwd(), client: {
  app: {log: async () => {}}, session: {
    get: async () => {sdkCalls++; return {data: {id: "ses_A"}};},
    promptAsync: async () => {throw Error("promptAsync must not run");},
  },
}};
const hooks = await AgentHarnessGuards(ctx);
const queued = __fixturePaneState();
hooks.dispose();
const cancelled = globalThis.__fixtureTimers[0].cancelled;
if (!cancelled) throw Error("dispose left startup timer armed");
if (!globalThis.__fixtureTimers[0].cancelled) fireStartupTimer(0);
await settleTurn();
console.log(JSON.stringify({queued, cancelled, sdkCalls, publishers: globalThis.__fixturePublishers.length,
  state: __fixturePaneState()}));
''')
        self.assertEqual(result["queued"]["timerQueued"], True)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["sdkCalls"], 0)
        self.assertEqual(result["publishers"], 0)
        self.assertEqual(result["state"]["originState"], "invalidated")

    def test_opencode_initializer_does_not_schedule_without_pane_or_for_worker(self):
        body = r'''
const ctx = {directory: process.cwd(), worktree: process.cwd(), client: {
  app: {log: async () => {}}, session: {promptAsync: async () => {throw Error("unexpected prompt");}},
}};
const hooks = await AgentHarnessGuards(ctx);
const state = __fixturePaneState();
hooks.dispose();
console.log(JSON.stringify({timers: globalThis.__fixtureTimers.length, state}));
'''
        for label, kwargs in (("no-pane", {"pane_id": False}), ("worker", {"worker": True})):
            with self.subTest(case=label):
                result = self.run_agent_initializer_fixture(body, **kwargs)
                self.assertEqual(result["timers"], 0)
                self.assertFalse(result["state"]["timerQueued"])

    def test_opencode_native_selector_keeps_pure_invocation_and_rejects_mixed_continue(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture();
const cases = [
 ["opencode", "--session", "ses_A"], ["/native/opencode", "-s", "ses_A", "--model", "provider/model"],
 ["opencode", "--session=ses_A", "--prompt=--continue"],
 ["opencode", "--session", "ses_A", "--continue=false", "--fork=false"],
 ["opencode", "--no-continue", "--session", "ses_A"],
 ["opencode", "--session", "ses_A", "--", "/fixture/project"],
 ["opencode", "-s", "ses_A", "-c"], ["opencode", "--session", "ses_A", "--continue"],
 ["opencode", "--continue=true", "--session", "ses_A"], ["opencode", "--session", "ses_A", "--fork"],
 ["opencode", "--session", "ses_A", "--fork", "--continue=false"],
 ["opencode", "--continue"], ["opencode", "run", "--session", "ses_A"],
 ["opencode", "serve", "--session", "ses_A"], ["opencode", "attach", "--session", "ses_A"],
 ["opencode", "--", "--session", "ses_A"], ["opencode", "--prompt", "--session", "ses_A"],
 ["opencode", "--session", "ses_A", "--session", "ses_A"], ["opencode", "-sA"],
 ["opencode", "--unknown", "--session", "ses_A"], ["node", "--session", "ses_A"],
 ["opencode", "--session", "ses_A\0foreign"], ["opencode", "--session", "ses_A", "--mini"],
 ["opencode"], ["opencode", "--session", "ses_A", "--continue=0"],
];
for (const key of ["--prompt", "--model", "-m", "--agent", "--port", "--hostname", "--mdns-domain", "--cors", "--log-level"]) {
 for (const flag of ["--continue", "-c", "--fork"]) cases.push(["opencode", "--session=ses_A", key, flag]);
}
console.log(JSON.stringify(cases.map(argv => f.scope.nativePaneSelector(argv))));
''')
        self.assertEqual(result, ["ses_A"] * 6 + [None] * 46)

    def test_opencode_native_origin_invalid_or_bounded_proc_data_has_no_sdk_fallback(self):
        result = self.run_pane_projection_fixture(r'''
const results = [];
const cases = [{stat: "999 (opencode) S " + "0 ".repeat(18) + "77 0"}, {stat: "123 (opencode) S 0"},
 {command: Buffer.from("opencode\0--session\0ses_A")},
 {argv: ["opencode", "--session", "ses_A", "--prompt", "x".repeat(33000)]},
 {argv: ["opencode", "--session", "ses_A", "--continue"]},
 {argv: ["opencode", "--session=ses_A", "--prompt", "--continue"]}, {worker: true}];
for (const options of cases) {
 const f = fixture(options);let gets = 0;
 await f.scope.projectPane("ses_A", f.ctx(async () => {gets++;return {id: "ses_A"}}));
 results.push([gets, f.commands.length]);
}
console.log(JSON.stringify(results));
''')
        self.assertEqual(result, [[0, 0]] * 7)

    def test_opencode_foreign_first_directory_and_unknown_origin_consume_nothing(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture();let gets = 0;
const ctx = f.ctx(async ({path}) => {gets++;return {data: {id: path.id}}});
await f.scope.projectPane("ses_B", ctx); // Another parentless root in the same directory arrives first.
const foreignDirectory = f.ctx();foreignDirectory.directory = "/another/project";
await f.scope.projectPane("ses_A", foreignDirectory);
const before = [gets, f.commands.length];
await f.scope.projectPane("ses_A", ctx);
const unavailable = fixture({argv: ["opencode", "--session", "ses_A", "--continue"]});
await unavailable.scope.projectPane("ses_A", unavailable.ctx());
console.log(JSON.stringify({before, gets, commands: f.commands, unavailable: unavailable.commands, reads: f.reads}));
''')
        self.assertEqual(result["before"], [0, 0])
        self.assertEqual(result["gets"], 1)
        self.assertEqual(result["commands"], [["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A",
                                              "--no-report-session"]])
        self.assertEqual(result["unavailable"], [])
        self.assertEqual(result["reads"], ["/proc/self/cmdline", "/proc/self/stat"])

    def test_opencode_peer_identity_publication_requires_exact_sdk_top_level_session(self):
        result = self.run_pane_projection_fixture(r'''
const commands = [];
for (const response of [{data: {id: "ses_A"}}, {id: "ses_A", parentID: null},
 {data: {id: "ses_foreign"}}, {data: {id: "ses_A", parentID: "child"}},
 {data: {id: "ses_A", parentID: 0}}, {error: "failed", data: {id: "ses_A"}},
 {response: {ok: false}, data: {id: "ses_A"}}, {}]) {
 const f = fixture();await f.scope.projectPane("ses_A", f.ctx(async options => {
  if (options.path.id !== "ses_A" || options.throwOnError !== true || !options.signal) throw Error("wrong SDK v1 call");
  return response;
 }));commands.push(f.commands[0]);f.exit(0);
}
console.log(JSON.stringify(commands));
''')
        metadata = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode",
                    "--session-id", "ses_A", "--no-report-session"]
        self.assertEqual(result, [metadata] * 8)

    def test_opencode_normal_tool_retry_retains_actual_sdk_identity_and_observes_timeout(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture();let gets = 0, resolveFirst;
const ctx = f.ctx(async ({path}) => {
 if (path.id !== "ses_A") throw Error("guessed identity");gets++;
 if (gets === 1) return await new Promise(resolve => {resolveFirst = resolve});
 return {id: "ses_A"};
});
await Promise.all([f.scope.projectPane("ses_A", ctx, true), f.scope.projectPane("ses_A", ctx, true)]);
resolveFirst({id: "late-foreign"});f.exit(0);
await f.scope.projectPane("ses_A", ctx, true);
f.setNow(12000);await f.scope.projectPane("ses_A", ctx, true);
const sdk = f.logs.filter(row => row.extra.stage === "sdk").map(row => [row.extra.reason, row.extra.verified]);
console.log(JSON.stringify({gets, commands: f.commands, sdk}));
''')
        self.assertEqual(result["gets"], 2)
        self.assertEqual(result["commands"], [
            ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A", "--no-report-session"],
            ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A",
             "--no-report-session"]])
        self.assertEqual(result["sdk"], [["sdk-timeout", False], ["verified", True]])

    def test_opencode_spent_startup_and_child_slot_survive_reload_and_observer_timeout(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture();const first = f.ctx();
f.scope.registerPaneContext(first);await f.scope.projectPane("ses_A", first);
f.scope.retirePaneContext(first);const next = f.ctx();f.scope.registerPaneContext(next);
f.timers.find(t => t.ms === 5000).cb(); // Observation ends, actual child remains alive.
await Promise.all([f.scope.projectPane("ses_A", next), f.scope.projectPane("ses_A", next)]);
const whileAlive = f.commands.length;
f.exit(0);await f.scope.projectPane("ses_A", next);f.exit(1);
f.scope.retirePaneContext(next);const third = f.ctx();f.scope.registerPaneContext(third);
await f.scope.projectPane("ses_A", third);f.exit(2);
f.scope.invalidatePaneOrigin("ses_A");await f.scope.projectPane("ses_A", third);f.exit(3);
console.log(JSON.stringify({whileAlive, commands: f.commands,
 timeout: f.logs.some(row => row.extra.reason === "publisher-stale-observation")}));
''')
        self.assertEqual(result["whileAlive"], 1)
        prefix = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A"]
        self.assertEqual(result["commands"], [prefix + ["--no-report-session"],
            prefix + ["--no-report-session"], prefix + ["--no-report-session"], prefix + ["--no-report-session"]])
        self.assertTrue(result["timeout"])

    def test_opencode_disposed_pending_origin_never_rearms_or_spawns_from_late_sdk(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture();let resolveOld;
const old = f.ctx(async () => await new Promise(resolve => {resolveOld = resolve}));
f.scope.registerPaneContext(old);const waiting = f.scope.projectPane("ses_A", old);
f.scope.retirePaneContext(old);const next = f.ctx();f.scope.registerPaneContext(next);
await f.scope.projectPane("ses_A", next);f.exit(0);
resolveOld({data: {id: "ses_A"}});await waiting;
f.scope.retirePaneContext(next);const third = f.ctx();f.scope.registerPaneContext(third);
await f.scope.projectPane("ses_A", third);f.exit(1);
await f.scope.projectPane("ses_B", third); // A later fork is not the native launch selection.
console.log(JSON.stringify({commands: f.commands,
 sdk: f.logs.filter(row => row.extra.stage === "sdk").map(row => row.extra.reason)}));
''')
        metadata = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A", "--no-report-session"]
        self.assertEqual(result["commands"], [metadata, metadata])
        self.assertEqual(result["sdk"], ["verified", "sdk-stale-callback", "verified"])

    def test_opencode_spawn_failure_report_zero_can_retry_but_created_attempt_never_replays(self):
        result = self.run_pane_projection_fixture(r'''
const f = fixture({spawnThrows: true});const ctx = f.ctx();
await f.scope.projectPane("ses_A", ctx);await f.scope.projectPane("ses_A", ctx);
f.children[0].stdout.write(JSON.stringify({schema: "hearting-pane-observation-v1", reason: "guard-refused",
 session_report: "not-attempted", metadata_report: "not-attempted"}));
await new Promise(resolve => setImmediate(resolve));f.exit(0);
await f.scope.projectPane("ses_A", ctx);f.exit(1);
const noPid = fixture({spawnNoPid: true});const other = noPid.ctx();
await noPid.scope.projectPane("ses_A", other);noPid.children[0].emit("error", Error("no process"));
await noPid.scope.projectPane("ses_A", other);
console.log(JSON.stringify({commands: f.commands, noPid: noPid.commands}));
''')
        prefix = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A"]
        self.assertEqual(result["commands"], [prefix + ["--no-report-session"],
            prefix + ["--no-report-session"], prefix + ["--no-report-session"]])
        self.assertEqual(result["noPid"], [prefix + ["--no-report-session"],
            prefix + ["--no-report-session"]])

    def test_opencode_spawn_error_fallback_uses_same_invocation_once_and_preserves_errno(self):
        result = self.run_pane_projection_fixture(r'''
const observation = JSON.stringify({schema: "hearting-pane-observation-v1", reason: "report-attempts-finished",
 session_report: "exit0", metadata_report: "exit0", session_report_rc: 0, metadata_report_rc: 0});
const syncResult = {pid: 901, status: 0, stdout: observation};
const f = fixture({spawnNoPid: true, syncResult});const ctx = f.ctx();
await f.scope.projectPane("ses_A", ctx);
const error = Object.assign(Error("synthetic async failure\n" + "가".repeat(200)), {code: "EFAULT", errno: -14});
f.children[0].emit("error", error);f.children[0].emit("error", error);f.exit(0);
await f.scope.projectPane("ses_A", ctx);f.exit(1);
const thrown = fixture({spawnThrows: true, syncResult});const other = thrown.ctx();
await thrown.scope.projectPane("ses_A", other);await thrown.scope.projectPane("ses_A", other);thrown.exit(0);
console.log(JSON.stringify({commands: f.commands, sync: f.sync, logs: f.logs,
 thrown: {commands: thrown.commands, sync: thrown.sync, logs: thrown.logs}}));
''')
        prefix = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A"]
        startup = prefix + ["--no-report-session"]
        refresh = prefix + ["--no-report-session"]
        for record in [result, result["thrown"]]:
            self.assertEqual(record["commands"], [startup, refresh])
            self.assertEqual(len(record["sync"]), 1)
            attempt = record["sync"][0]
            self.assertEqual(attempt["exe"], "python3")
            self.assertEqual(attempt["args"], startup)
            self.assertEqual(attempt["config"], {"cwd": "fixture-root",
                "env": {"HERDR_PANE_ID": "fixture-pane", "OPENCODE_SESSION_ID": "ses_foreign", "AGENT_HOME": "fixture-root"},
                "detached": True, "stdio": ["ignore", "pipe", "ignore"], "encoding": "utf8",
                "timeout": 10000, "killSignal": "SIGKILL", "maxBuffer": 1024})
            success = [row["extra"] for row in record["logs"] if row["extra"]["reason"] == "publisher-path-success"]
            self.assertEqual(success, [{"module": "hearting-peer-identity", "stage": "publisher",
                "reason": "publisher-path-success", "sessionID": "ses_A", "publisherPath": "sync-fallback", "publisherRc": 0}])
            self.assertTrue(any(row["extra"].get("sessionReport") == "exit0" for row in record["logs"]))
        error = next(row["extra"] for row in result["logs"] if row["extra"]["reason"] == "publisher-spawn-error")
        self.assertEqual((error["errorCode"], error["errorErrno"]), ("EFAULT", -14))
        self.assertLessEqual(len(error["errorMessage"].encode("utf-8")), 256)
        self.assertNotIn("\n", error["errorMessage"])
        error = next(row["extra"] for row in result["thrown"]["logs"] if row["extra"]["reason"] == "publisher-spawn-error")
        self.assertEqual((error["errorCode"], error["errorErrno"], error["errorMessage"]),
                         ("EACCES", -13, "synthetic spawn failure"))

    def test_opencode_spawn_fallback_failure_live_and_stale_boundaries(self):
        result = self.run_pane_projection_fixture(r'''
const resultRows = [];
for (const options of [{syncResult: {pid: 903, status: null, error: Object.assign(Error("synthetic timeout"), {code: "ETIMEDOUT", errno: -110})}},
 {syncResult: {pid: 904, status: 7, stdout: "private-stdout"}},
 {syncResult: {pid: 905, status: 0, stdout: "private-invalid-output"}}, {syncThrows: true},
 {syncResult: {pid: 906, status: null, error: Object.assign(Error("synthetic overflow"), {code: "ENOBUFS", errno: -105})}}]) {
 const f = fixture({...options, spawnThrows: true});await f.scope.projectPane("ses_A", f.ctx());
 resultRows.push({sync: f.sync.length, logs: f.logs});
}
const live = fixture();const liveCtx = live.ctx();await live.scope.projectPane("ses_A", liveCtx);
live.children[0].emit("spawn");live.children[0].emit("error", Object.assign(Error("synthetic launched error"), {errno: -14}));
await live.scope.projectPane("ses_A", liveCtx);const held = live.commands.length;live.exit(0);
await live.scope.projectPane("ses_A", liveCtx);live.exit(1);
const stale = fixture();const old = stale.ctx();await stale.scope.projectPane("ses_A", old);
stale.scope.retirePaneContext(old);stale.children[0].emit("error", Error("synthetic old error"));stale.exit(0);
console.log(JSON.stringify({numericError: live.scope.publisherErrorFields({code: 14, errno: -14, message: "synthetic numeric code"}),
 resultRows, live: {sync: live.sync.length, held, commands: live.commands, logs: live.logs},
 stale: {sync: stale.sync.length, logs: stale.logs}}));
''')
        expected = ["publisher-sync-fallback-error", "publisher-sync-fallback-exit-error",
                    "publisher-observation-invalid", "publisher-sync-fallback-error", "publisher-sync-fallback-error"]
        for row, reason in zip(result["resultRows"], expected):
            self.assertEqual(row["sync"], 1)
            self.assertIn(reason, [log["extra"]["reason"] for log in row["logs"]])
            self.assertNotIn("private", json.dumps(row["logs"]))
        self.assertEqual(result["resultRows"][0]["logs"][-1]["extra"]["errorCode"], "ETIMEDOUT")
        self.assertEqual(result["resultRows"][3]["logs"][-1]["extra"]["errorErrno"], -14)
        self.assertEqual(result["numericError"], {"errorCode": 14, "errorErrno": -14, "errorMessage": "synthetic numeric code"})
        self.assertEqual(result["live"]["sync"], 0)
        self.assertEqual(result["live"]["held"], 1)
        self.assertEqual(len(result["live"]["commands"]), 2)
        self.assertIn("publisher-sync-fallback-skipped-live", [row["extra"]["reason"] for row in result["live"]["logs"]])
        self.assertIn("async", [row["extra"].get("publisherPath") for row in result["live"]["logs"]])
        self.assertEqual(result["stale"]["sync"], 0)
        self.assertIn("publisher-sync-fallback-stale", [row["extra"]["reason"] for row in result["stale"]["logs"]])

    def _live_publisher_root(self, name):
        live = self.state / name
        (live / "core").mkdir(parents=True)
        (live / "core" / "CORE.md").write_text("live")
        projector_dir = live / "adapters" / "opencode" / "bin"
        projector_dir.mkdir(parents=True)
        (projector_dir / "preflight.sh").write_text("live")
        helper_dir = live / "tools" / "fleet"
        helper_dir.mkdir(parents=True)
        (helper_dir / "herdr_projection.py").write_text("live")
        return live

    def test_opencode_stale_release_root_resolves_live_target_with_same_invocation(self):
        # Deleted import-time release (old rune ENOENT/-2 shape): async and sync
        # share one resolved live target, same argv/seq/identity, fallback once.
        live = self._live_publisher_root("live-root")
        helper = str(live / "tools" / "fleet" / "herdr_projection.py")
        observation = {"schema": "hearting-pane-observation-v1", "reason": "report-attempts-finished",
                       "session_report": "exit0", "metadata_report": "exit0"}
        body = r'''
const observation = %%OBSERVATION%%;
const syncResult = {pid: 901, status: 0, stdout: JSON.stringify(observation)};
const f = fixture({root: "/deleted/releases/v3.2.13",
  herdrProjection: "/deleted/releases/v3.2.13/tools/fleet/herdr_projection.py",
  env: {AGENT_HOME: "%%LIVE%%"}, spawnNoPid: true, syncResult});
const ctx = f.ctx();
await f.scope.projectPane("ses_A", ctx);
f.children[0].emit("error", Object.assign(Error("synthetic stale release failure"), {code: "ENOENT", errno: -2}));
await f.scope.projectPane("ses_A", ctx); f.exit(1);
console.log(JSON.stringify({commands: f.commands, sync: f.sync, logs: f.logs}));
'''.replace("%%OBSERVATION%%", json.dumps(observation)).replace("%%LIVE%%", str(live))
        result = self.run_pane_projection_fixture(body)
        startup = [helper, "--harness", "opencode", "--session-id", "ses_A",
                   "--no-report-session"]
        refresh = [helper, "--harness", "opencode", "--session-id", "ses_A", "--no-report-session"]
        self.assertEqual(result["commands"], [startup, refresh])
        self.assertEqual(len(result["sync"]), 1)
        attempt = result["sync"][0]
        self.assertEqual(attempt["exe"], "python3")
        self.assertEqual(attempt["args"], startup)
        self.assertEqual(attempt["config"]["cwd"], str(live))
        self.assertEqual(attempt["config"]["env"]["AGENT_HOME"], str(live))
        self.assertNotIn("deleted", json.dumps(result))
        error = next(row["extra"] for row in result["logs"] if row["extra"]["reason"] == "publisher-spawn-error")
        self.assertEqual((error["errorCode"], error["errorErrno"]), ("ENOENT", -2))
        success = [row["extra"] for row in result["logs"] if row["extra"]["reason"] == "publisher-path-success"]
        self.assertEqual([(row["publisherPath"], row["publisherRc"]) for row in success], [("sync-fallback", 0)])

    def test_opencode_live_root_kept_when_interpreter_missing_on_both_paths(self):
        # Same errno, other boundary: frozen root/helper live, so no switch —
        # the interpreter failure stays bounded on async and sync, fallback once.
        live = self._live_publisher_root("live-root")
        other = self._live_publisher_root("other-root")
        helper = str(live / "tools" / "fleet" / "herdr_projection.py")
        body = r'''
const f = fixture({root: "%%LIVE%%", herdrProjection: "%%HELPER%%",
  env: {AGENT_HOME: "%%OTHER%%"}, spawnNoPid: true,
  syncResult: {pid: 0, error: Object.assign(Error("synthetic interpreter missing"), {code: "ENOENT", errno: -2})}});
const ctx = f.ctx();
await f.scope.projectPane("ses_A", ctx);
f.children[0].emit("error", Object.assign(Error("synthetic async interpreter missing"), {code: "ENOENT", errno: -2}));
await f.scope.projectPane("ses_A", ctx); f.exit(1);
console.log(JSON.stringify({commands: f.commands, sync: f.sync, logs: f.logs}));
'''.replace("%%LIVE%%", str(live)).replace("%%HELPER%%", helper).replace("%%OTHER%%", str(other))
        result = self.run_pane_projection_fixture(body)
        prefix = [helper, "--harness", "opencode", "--session-id", "ses_A"]
        self.assertEqual(result["commands"], [prefix + ["--no-report-session"],
            prefix + ["--no-report-session"]])
        self.assertEqual(len(result["sync"]), 1)
        attempt = result["sync"][0]
        self.assertEqual(attempt["args"], result["commands"][0])
        self.assertEqual(attempt["config"]["cwd"], str(live))
        self.assertEqual(attempt["config"]["env"]["AGENT_HOME"], str(live))
        self.assertNotIn(str(other), json.dumps(result))
        reasons = [row["extra"]["reason"] for row in result["logs"]]
        self.assertIn("publisher-spawn-error", reasons)
        self.assertIn("publisher-sync-fallback-error", reasons)
        self.assertNotIn("publisher-path-success", reasons)
        for row in result["logs"]:
            if row["extra"]["reason"] in ("publisher-spawn-error", "publisher-sync-fallback-error"):
                self.assertEqual((row["extra"]["errorCode"], row["extra"]["errorErrno"]), ("ENOENT", -2))

    def _write_tui_record(self, state_dir, pid, start, sid):
        record_dir = Path(state_dir) / "hearting" / "tui-identity"
        record_dir.mkdir(parents=True, exist_ok=True)
        record = record_dir / f"{pid}-{start}.json"
        record.write_text(json.dumps({"schema": "hearting-tui-selection-v1",
                                      "sessionID": sid, "pid": pid, "start": start}))
        return record

    def test_opencode_bare_origin_with_tui_record_publishes_through_existing_path(self):
        self._write_tui_record(str(self.state), 123, "77", "ses_A")
        result = self.run_pane_projection_fixture(r'''
const observation = JSON.stringify({schema: "hearting-pane-observation-v1", reason: "report-attempts-finished",
 session_report: "exit0", metadata_report: "exit0"});
const f = fixture({argv: ["opencode", "--auto", "--model", "provider/model"],
  env: {XDG_STATE_HOME: "%%STATE%%"}});
const ctx = f.ctx();
await f.scope.projectPane("ses_A", ctx);
f.children[0].stdout.write(observation);
await new Promise(resolve => setImmediate(resolve)); f.exit(0);
console.log(JSON.stringify({commands: f.commands, logs: f.logs}));
'''.replace("%%STATE%%", str(self.state)))
        prefix = ["fixture-root/tools/fleet/herdr_projection.py", "--harness", "opencode", "--session-id", "ses_A"]
        self.assertEqual(result["commands"], [prefix + ["--no-report-session"]])
        finished = [row["extra"] for row in result["logs"] if row["extra"]["reason"] == "report-attempts-finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual((finished[0]["sessionReport"], finished[0]["metadataReport"]), ("exit0", "exit0"))

    def test_opencode_bare_origin_without_matching_tui_record_stays_unavailable(self):
        self._write_tui_record(str(self.state), 123, "77", "ses_other")
        for options, record in (({}, None), ({"record": "mismatch"}, None)):
            with self.subTest(options=options):
                result = self.run_pane_projection_fixture(r'''
const f = fixture({argv: ["opencode", "--auto", "--model", "provider/model"],
  env: {XDG_STATE_HOME: "%%STATE%%"}});
await f.scope.projectPane("ses_A", f.ctx());
console.log(JSON.stringify({commands: f.commands, logs: f.logs}));
'''.replace("%%STATE%%", str(self.state) if options.get("record") else "/nonexistent-state-dir"))
                self.assertEqual(result["commands"], [])
                self.assertIn("native-origin-unavailable", [row["extra"]["reason"] for row in result["logs"]])

    def test_opencode_resume_argv_never_uses_tui_record(self):
        self._write_tui_record(str(self.state), 123, "77", "ses_A")
        result = self.run_pane_projection_fixture(r'''
const f = fixture({argv: ["opencode", "--session", "ses_A", "--continue"],
  env: {XDG_STATE_HOME: "%%STATE%%"}});
await f.scope.projectPane("ses_A", f.ctx());
console.log(JSON.stringify({commands: f.commands, logs: f.logs}));
'''.replace("%%STATE%%", str(self.state)))
        self.assertEqual(result["commands"], [])
        self.assertIn("native-origin-unavailable", [row["extra"]["reason"] for row in result["logs"]])

    def test_opencode_selection_swap_during_sdk_skips_late_publish(self):
        record = self._write_tui_record(str(self.state), 123, "77", "ses_A")
        swapped = {"schema": "hearting-tui-selection-v1", "sessionID": "ses_B", "pid": 123, "start": "77"}
        body = r'''
import * as realFs from "node:fs";
const f = fixture({argv: ["opencode", "--auto", "--model", "provider/model"],
  env: {XDG_STATE_HOME: "%%STATE%%"}});
const ctx = f.ctx(async () => {
  realFs.writeFileSync("%%RECORD%%", JSON.stringify(%%SWAPPED%%));
  return {data: {id: "ses_A"}};
});
await f.scope.projectPane("ses_A", ctx);
console.log(JSON.stringify({commands: f.commands, logs: f.logs}));
'''.replace("%%STATE%%", str(self.state)).replace("%%RECORD%%", str(record)).replace(
            "%%SWAPPED%%", json.dumps(swapped))
        result = self.run_pane_projection_fixture(body)
        self.assertEqual(result["commands"], [])
        reasons = [row["extra"]["reason"] for row in result["logs"]]
        self.assertIn("publisher-stale-selection", reasons)
        self.assertNotIn("publisher-spawned", reasons)

    def test_opencode_tui_entry_records_selection_and_clears_on_home_and_dispose(self):
        body = open(ROOT / "adapters/opencode/tui/hearting-tui-identity.ts").read().replace(
            'import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs"', "").replace(
            'import path from "node:path"', "").replace(
            "export default", "var __module =")
        js = r'''
import path from "node:path";
import * as realFs from "node:fs";
import vm from "node:vm";
const input = JSON.parse(process.env.FIXTURE_TUI);
const stat = "456 (opencode) S " + "0 ".repeat(18) + "99 0";
const scope = {path, Date, JSON,
  mkdirSync: realFs.mkdirSync, renameSync: realFs.renameSync, rmSync: realFs.rmSync,
  writeFileSync: realFs.writeFileSync,
  readFileSync: (file, enc) => String(file) === "/proc/self/stat" ? stat : realFs.readFileSync(file, enc),
  process: {pid: 456, env: {XDG_STATE_HOME: input.state}}};
let current = {name: "home"};
const subs = [], unsubs = [], disposed = [], slotRegs = [];
const api = {route: {get current() {return current}},
  event: {on: (type, handler) => {subs.push([type, handler]); return () => unsubs.push(type)}},
  lifecycle: {onDispose: (fn) => disposed.push(fn)},
  slots: {register: (plugin) => {slotRegs.push(plugin); return "mock-slot-id"}}};
vm.runInNewContext(input.source, scope);
await scope.__module.tui(api);
const read = () => {
  try {
    return realFs.readFileSync(input.record, "utf8");
  } catch { return null }
};
const slots = slotRegs[0].slots;
const fire = (type, event) => subs.find(([name]) => name === type)[1](event || {});
const home = read();
fire("session.created", {type: "session.created", properties: {sessionID: "ses_new", info: {id: "ses_new"}}});
const gap = read();
current = {name: "session", params: {sessionID: "ses_new"}};
slots.session_prompt_right({}, {session_id: "ses_new"});
const mounted = read();
slots.session_prompt_right({}, {session_id: "ses_other"});
const disagree = read();
current = {name: "workspace-smoke", params: {tab: 0}};
slots.app_bottom({});
const customCleared = read();
current = {name: "session", params: {sessionID: "ses_back"}};
slots.app_bottom({});
const appWrote = read();
fire("session.deleted", {type: "session.deleted", properties: {sessionID: "ses_back", info: {id: "ses_back"}}});
const clearedByDelete = read();
current = {name: "home"};
slots.home_prompt_right({}, {});
const cleared = read();
for (const fn of disposed) await fn();
console.log(JSON.stringify({id: scope.__module.id, hasServer: "server" in scope.__module,
  slotNames: Object.keys(slots), subscribed: subs.map(([type]) => type),
  unsubscribed: unsubs, home, gap, mounted: mounted && JSON.parse(mounted).sessionID,
  disagree: disagree && JSON.parse(disagree).sessionID, customCleared,
  appWrote: appWrote && JSON.parse(appWrote).sessionID, clearedByDelete, cleared}));
'''
        env = dict(os.environ, FIXTURE_TUI=json.dumps({
            "source": body, "state": str(self.state),
            "record": str(Path(self.state) / "hearting" / "tui-identity" / "456-99.json")}))
        run = subprocess.run(["node", "--input-type=module", "-e", js], env=env,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["id"], "hearting.tui-identity")
        self.assertFalse(result["hasServer"])
        self.assertEqual(sorted(result["slotNames"]), ["app_bottom", "home_prompt_right", "session_prompt_right"])
        self.assertEqual(result["subscribed"], ["session.created", "session.deleted", "tui.session.select",
            "session.next.prompted", "message.updated", "session.status", "session.idle"])
        self.assertIsNone(result["home"])
        self.assertIsNone(result["gap"])
        self.assertEqual(result["mounted"], "ses_new")
        self.assertEqual(result["disagree"], "ses_new")
        self.assertIsNone(result["customCleared"])
        self.assertEqual(result["appWrote"], "ses_back")
        self.assertIsNone(result["clearedByDelete"])
        self.assertIsNone(result["cleared"])
        self.assertEqual(sorted(result["unsubscribed"]), sorted(result["subscribed"]))

    def test_opencode_native_idle_persists_and_resumes_exact_message_once(self):
        recipient = {"harness": "opencode", "session_id": "ses_A"}
        text, ref = pm.prepare_peer_message("native OpenCode body", self.sender, recipient, defer=True)
        result = self.run_pending_agent_initializer_fixture(r'''
const sid = "ses_A", messages = [];
let status = "idle", saves = 0, resumes = [], statusReads = 0, sessionReads = 0, messageReads = 0;
const sdkResult = (data, status = 200) => ({data, error: undefined,
  response: {ok: true, status}});
const rootDto = id => ({id, projectID: "project_fixture", directory: process.cwd(), slug: "fixture-root",
  title: "fixture root", version: "1.19.0", time: {created: Date.now(), updated: Date.now()}});
const session = {
  status: async ({query}) => {statusReads++;if(query.directory !== process.cwd()) throw Error("status directory mismatch");return sdkResult({})},
  get: async ({path, query}) => {sessionReads++;if(path.id !== sid || query.directory !== process.cwd()) throw Error("root lookup mismatch");return sdkResult(rootDto(path.id))},
  messages: async ({query}) => {messageReads++;if(query.directory !== process.cwd()) throw Error("history directory mismatch");return sdkResult(messages)},
  prompt: async args => {
    saves++;
    if (args.path.id !== sid || args.query.directory !== process.cwd() || args.body.noReply !== true
        || Object.keys(args.body).sort().join() !== "noReply,parts"
        || args.body.parts[0].text !== FIXTURE_EXPECTED_TEXT) throw Error("noReply save mismatch");
    const row = {info: {id: "native-message-1", role: "user", sessionID: sid,
      time: {created: Date.now()}}, parts: args.body.parts};
    messages.push(row);
    return sdkResult(row);
  },
  promptAsync: async args => {
    resumes.push(args);
    if (args.path.id !== sid || args.query.directory !== process.cwd()
        || Object.keys(args.body).sort().join() !== "messageID,parts"
        || args.body.messageID !== "native-message-1" || args.body.parts.length !== 0)
      throw Error("wrong native resume request");
    status = "busy";
    return sdkResult(undefined, 204);
  },
};
const carrierLogs = [];
const ctx = {directory: process.cwd(), worktree: process.cwd(), client: {session,
  app: {log: async ({body}) => carrierLogs.push(body)}}};
const hooks = await AgentHarnessGuards(ctx);
const bindingBeforeIdle = __fixturePaneState();
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
const savesAfterIdle = saves;
const queued = JSON.parse(__fixtureSpawnSync("python3", [process.env.AGENT_HOME + "/utilities/peer-message.py",
  "pending", "--to-harness", "opencode", "--to-session-id", sid], {encoding: "utf8"}).stdout)[0];
await hooks.event({event: {type: "session.status", properties: {sessionID: sid, status: {type: "busy"}}}});
await hooks.event({event: {type: "question.asked", properties: {sessionID: sid}}});
await hooks.event({event: {type: "permission.asked", properties: {sessionID: sid}}});
await hooks["tool.execute.after"]({sessionID: sid, tool: "fixture-noop", args: {}}, {output: ""});
messages.push({info: {id: "assistant-1", sessionID: sid, role: "assistant",
  parentID: "native-message-1", time: {completed: Date.now()}}, parts: []});
status = "idle";
await hooks.event({event: {type: "session.status", properties: {sessionID: sid, status: {type: "idle"}}}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
hooks.dispose();
console.log(JSON.stringify({saves, resumes, queued, messages, bindingBeforeIdle,
  savesAfterIdle, statusReads, sessionReads, messageReads,
  carrierArmed: carrierLogs.some(row => row.extra?.module === "hearting-completion-carrier"
    && row.extra.stage === "env" && row.extra.reason === "carrier-armed")}));
'''.replace("FIXTURE_EXPECTED_TEXT", json.dumps(text)), ref=ref, bare_origin=True,
                                                   disable_intervals=True)
        self.assertTrue(result["bindingBeforeIdle"]["bindingOwnsOrigin"], result)
        self.assertIsNone(result["bindingBeforeIdle"]["originSID"], result)
        self.assertEqual(result["saves"], 1, result)
        self.assertTrue(result["carrierArmed"], result)
        self.assertEqual(result["savesAfterIdle"], 1, result)
        self.assertEqual(len(result["resumes"]), 1, result)
        self.assertEqual(result["resumes"][0]["body"], {"messageID": "native-message-1", "parts": []})
        self.assertEqual(result["queued"]["state"], "queued")
        self.assertEqual(result["queued"]["actual_message_id"], "native-message-1")
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(result["messages"][1]["info"]["parentID"], "native-message-1")
        row = pm._read_pending(ref)
        self.assertEqual((row["state"], row["receipt"], row["actual_message_id"]),
                         ("received", "exact-native-history", "native-message-1"))
        self.assertEqual(len([r for r in self.rows() if r.get("transfer_ref") == ref]), 1)

    def test_opencode_bare_tui_requires_exact_pid_birth_selection_record(self):
        recipient = {"harness": "opencode", "session_id": "ses_A"}
        _text, ref = pm.prepare_peer_message("foreign bare TUI selection", self.sender, recipient, defer=True)
        result = self.run_pending_agent_initializer_fixture(r'''
const sid = "ses_A";
let gets = 0, saves = 0, resumes = 0;
const sdkResult = (data, status = 200) => ({data, error: undefined,
  response: {ok: true, status}});
const rootDto = {id: sid, projectID: "project_fixture", directory: process.cwd(), slug: "fixture-root",
  title: "fixture root", version: "1.19.0", time: {created: 1, updated: 1}};
const session = {
  status: async () => sdkResult({}),
  get: async () => {gets++;return sdkResult(rootDto)},
  messages: async () => sdkResult([]),
  prompt: async () => {saves++;return sdkResult(null)},
  promptAsync: async () => {resumes++;return sdkResult(undefined, 204)},
};
const hooks = await AgentHarnessGuards({directory: process.cwd(), worktree: process.cwd(), client: {session}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
hooks.dispose();
console.log(JSON.stringify({gets, saves, resumes, pane: __fixturePaneState()}));
''', ref=ref, bare_origin=True, selection_sid="ses_foreign")
        self.assertEqual((result["gets"], result["saves"], result["resumes"]), (0, 0, 0), result)
        self.assertEqual(pm._read_pending(ref)["state"], "pending")

    def test_opencode_same_native_origin_context_rebind_preserves_live_owner(self):
        result = self.run_agent_initializer_fixture(r'''
const directory = process.cwd();
const first = await AgentHarnessGuards({directory, worktree: directory, client: {}});
const firstState = __fixturePaneState();
const second = await AgentHarnessGuards({directory, worktree: directory, client: {}});
const secondState = __fixturePaneState();
first.dispose();
const afterFirstDispose = __fixturePaneState();
second.dispose();
console.log(JSON.stringify({firstState, secondState, afterFirstDispose}));
''')
        self.assertEqual(result["firstState"]["originState"], "pending")
        self.assertEqual(result["secondState"]["originState"], "pending")
        self.assertTrue(result["secondState"]["bindingOwnsOrigin"])
        self.assertEqual(result["afterFirstDispose"]["originState"], "pending")
        self.assertTrue(result["afterFirstDispose"]["bindingOwnsOrigin"])

    def test_opencode_root_and_status_sdk_shapes_fail_closed(self):
        cases = ("valid-null-parent", "unwrapped-status", "failed-status", "unwrapped-root",
                 "failed-root", "wrong-id", "wrong-directory", "child", "bad-parent", "malformed")
        for mode in cases:
            with self.subTest(mode=mode):
                _text, ref = pm.prepare_peer_message("root dto shape " + mode, self.sender,
                    {"harness": "opencode", "session_id": "ses_A"}, defer=True)
                body = r'''
const sid = "ses_A", mode = ROOT_MODE, queued = globalThis.__fixturePendingRow;
let gets = 0, messageReads = 0, saves = 0, resumes = 0;
const sdkResult = (data, status = 200) => ({data, error: undefined,
  response: {ok: true, status}});
const rootDto = id => ({id, projectID: "project_fixture", directory: process.cwd(), slug: "fixture-root",
  title: "fixture root", version: "1.19.0", time: {created: 1, updated: 1}});
const messages = mode === "valid-null-parent" && queued ? [
  {info: {id: "exact-user-message", role: "user", sessionID: sid, time: {created: Date.now()}},
    parts: [{type: "text", text: queued.text}]},
  {info: {id: "exact-assistant-message", role: "assistant", sessionID: sid,
    parentID: "exact-user-message", time: {completed: Date.now()}}, parts: []},
] : [];
const session = {
  status: async () => mode === "unwrapped-status"
    ? {[sid]: {type: "idle"}}
    : mode === "failed-status"
      ? {data: {[sid]: {type: "idle"}}, error: {name: "StatusError"}, response: {ok: false, status: 500}}
      : sdkResult({}),
  get: async ({path}) => {
    gets++;
    const root = rootDto(mode === "wrong-id" ? "ses_foreign" : path.id);
    if (mode === "unwrapped-root") return root;
    if (mode === "failed-root") return {data: root, error: {name: "NotFoundError"}, response: {ok: false, status: 404}};
    if (mode === "wrong-directory") root.directory += "/foreign";
    if (mode === "child") root.parentID = "ses_parent";
    if (mode === "bad-parent") root.parentID = 7;
    if (mode === "malformed") delete root.time;
    if (mode === "valid-null-parent") root.parentID = null;
    return sdkResult(root);
  },
  messages: async () => {messageReads++;return sdkResult(messages)},
  prompt: async () => {saves++;throw Error("invalid root received peer body")},
  promptAsync: async () => {resumes++;throw Error("invalid root resumed turn")},
};
const hooks = await AgentHarnessGuards({directory: process.cwd(), worktree: process.cwd(), client: {session}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
hooks.dispose();
const pendingReads = globalThis.__fixtureSyncCalls.filter(call =>
  call.args.some(value => String(value).endsWith("peer-message.py")) && call.args.includes("pending")).length;
const receivedCall = globalThis.__fixtureSyncCalls.find(call => call.args.includes("--received"));
const receivedMessageID = receivedCall ? receivedCall.args[receivedCall.args.indexOf("--message-id") + 1] : null;
console.log(JSON.stringify({gets, messageReads, pendingReads, saves, resumes, receivedMessageID}));
'''.replace("ROOT_MODE", json.dumps(mode))
                result = self.run_pending_agent_initializer_fixture(
                    body, ref=ref, disable_intervals=True)
                self.assertEqual((result["saves"], result["resumes"]), (0, 0), result)
                self.assertGreaterEqual(result["pendingReads"], 1, result)
                if mode == "valid-null-parent":
                    self.assertEqual((result["gets"], result["messageReads"]), (2, 1), result)
                    self.assertEqual(result["receivedMessageID"], "exact-user-message")
                else:
                    self.assertEqual((result["gets"], result["messageReads"]), (2, 0), result)
                    self.assertEqual(pm._read_pending(ref)["state"], "pending")

    def test_opencode_unwrapped_noReply_result_and_failed_resume_do_not_receive(self):
        recipient = {"harness": "opencode", "session_id": "ses_A"}
        for response_mode in ("unwrapped", "failed"):
            with self.subTest(response_mode=response_mode):
                _text, ref = pm.prepare_peer_message("unwrapped OpenCode response " + response_mode, self.sender,
                                                     recipient, defer=True)
                body = r'''
const sid = "ses_A", responseMode = RESPONSE_MODE, messages = [];
let saves = 0, resumes = 0, messageReads = 0;
const sdkResult = (data, status = 200) => ({data, error: undefined,
  response: {ok: true, status}});
const rootDto = id => ({id, projectID: "project_fixture", directory: process.cwd(), slug: "fixture-root",
  title: "fixture root", version: "1.19.0", time: {created: 1, updated: 1}});
const session = {
  status: async () => sdkResult({}),
  get: async ({path}) => sdkResult(rootDto(path.id)),
  messages: async () => {messageReads++;return sdkResult(messages)},
  prompt: async args => {
    saves++;
    const row = {info: {id: "native-message-ambiguous", role: "user", sessionID: sid,
      time: {created: Date.now()}}, parts: args.body.parts};
    messages.push(row);
    if (responseMode === "unwrapped") return row;
    return {data: row, error: {name: "PromptError"}, response: {ok: false, status: 500}};
  },
  promptAsync: async () => {resumes++;return {data: undefined, error: undefined,
    response: {ok: true, status: 204}};},
};
const hooks = await AgentHarnessGuards({directory: process.cwd(), worktree: process.cwd(), client: {session}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
hooks.dispose();
console.log(JSON.stringify({saves, resumes, messageReads}));
'''.replace("RESPONSE_MODE", json.dumps(response_mode))
                result = self.run_pending_agent_initializer_fixture(
                    body, ref=ref, disable_intervals=True)
                self.assertEqual((result["saves"], result["resumes"], result["messageReads"]),
                                 (1, 0, 1), result)
                self.assertEqual(pm._read_pending(ref)["state"], "unverified")

        _text, ref = pm.prepare_peer_message("failed OpenCode resume", self.sender, recipient, defer=True)
        result = self.run_pending_agent_initializer_fixture(r'''
const sid = "ses_A", messages = [];
let saves = 0, resumes = 0;
const sdkResult = (data, status = 200) => ({data, error: undefined,
  response: {ok: true, status}});
const rootDto = id => ({id, projectID: "project_fixture", directory: process.cwd(), slug: "fixture-root",
  title: "fixture root", version: "1.19.0", time: {created: 1, updated: 1}});
const session = {
  status: async () => sdkResult({}),
  get: async ({path}) => sdkResult(rootDto(path.id)),
  messages: async () => sdkResult(messages),
  prompt: async args => {
    saves++;
    const row = {info: {id: "native-message-resume-failed", role: "user", sessionID: sid,
      time: {created: Date.now()}}, parts: args.body.parts};
    messages.push(row);return sdkResult(row);
  },
  promptAsync: async args => {
    resumes++;
    if (args.body.messageID !== "native-message-resume-failed" || args.body.parts.length !== 0)
      throw Error("wrong failed-resume request");
    return {data: undefined, error: {name: "BadRequestError"}, response: {ok: false, status: 400}};
  },
};
const hooks = await AgentHarnessGuards({directory: process.cwd(), worktree: process.cwd(), client: {session}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
hooks.dispose();
console.log(JSON.stringify({saves, resumes, userID: messages[0]?.info.id}));
''', ref=ref, disable_intervals=True)
        self.assertEqual((result["saves"], result["resumes"], result["userID"]),
                         (1, 1, "native-message-resume-failed"), result)
        failed_resume = pm._read_pending(ref)
        self.assertNotEqual(failed_resume["state"], "received")
        self.assertEqual(failed_resume["actual_message_id"], "native-message-resume-failed")

    def test_opencode_lost_noReply_response_reconciles_exact_history_without_resubmit(self):
        recipient = {"harness": "opencode", "session_id": "ses_A"}
        _text, ref = pm.prepare_peer_message("lost OpenCode response", self.sender, recipient, defer=True)
        result = self.run_pending_agent_initializer_fixture(r'''
const sid = "ses_A", messages = [];
let status = "idle", saves = 0, resumes = 0;
const sdkResult = (data, status = 200) => ({data, error: undefined,
  response: {ok: true, status}});
const rootDto = id => ({id, projectID: "project_fixture", directory: process.cwd(), slug: "fixture-root",
  title: "fixture root", version: "1.19.0", time: {created: Date.now(), updated: Date.now()}});
const session = {
  status: async () => sdkResult({}),
  get: async ({path}) => sdkResult(rootDto(path.id)),
  messages: async () => sdkResult(messages),
  prompt: async args => {
    saves++;
    const row = {info: {id: "saved-before-lost-response", role: "user", sessionID: sid,
      time: {created: Date.now()}}, parts: args.body.parts};
    messages.push(row);
    throw Error("response-lost-after-persist");
  },
  promptAsync: async args => { resumes++; status = "busy";
    if (args.body.messageID !== "saved-before-lost-response" || args.body.parts.length !== 0)
      throw Error("wrong noReply continuation");
    return sdkResult(undefined, 204);
  },
};
const hooks = await AgentHarnessGuards({directory: process.cwd(), worktree: process.cwd(), client: {session}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
messages.push({info: {id: "assistant-1", sessionID: sid, role: "assistant",
  parentID: "saved-before-lost-response", time: {completed: Date.now()}}, parts: []});
status = "idle";
await hooks.event({event: {type: "session.status", properties: {sessionID: sid, status: {type: "idle"}}}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: sid}}});
hooks.dispose();
console.log(JSON.stringify({saves, resumes}));
''', disable_intervals=True)
        self.assertEqual((result["saves"], result["resumes"]), (1, 1))
        row = pm._read_pending(ref)
        self.assertEqual((row["state"], row["actual_message_id"], row["receipt"]),
                         ("received", "saved-before-lost-response", "exact-native-history"), result)

    def test_opencode_duplicate_history_wrong_parent_and_sid_swap_never_fabricate_receipt(self):
        recipient = {"harness": "opencode", "session_id": "ses_A"}
        text, ref = pm.prepare_peer_message("duplicate-bound OpenCode body", self.sender, recipient, defer=True)
        result = self.run_pending_agent_initializer_fixture(r'''
const sid = "ses_A", messages = [];
let status = "idle", saves = 0, resumes = 0;
const sdkResult = (data, status = 200) => ({data, error: undefined,
  response: {ok: true, status}});
const rootDto = id => ({id, projectID: "project_fixture", directory: process.cwd(), slug: "fixture-root",
  title: "fixture root", version: "1.19.0", time: {created: Date.now(), updated: Date.now()}});
const session = {
  status: async () => sdkResult({}),
  get: async ({path}) => sdkResult(path.id === sid ? rootDto(path.id) : {...rootDto(path.id), parentID: sid}),
  messages: async ({path}) => sdkResult(path.id === sid ? messages : []),
  prompt: async args => {
    saves++;
    const row = {info: {id: "native-message-1", role: "user", sessionID: sid,
      time: {created: Date.now()}}, parts: args.body.parts};
    messages.push(row); return sdkResult(row);
  },
  promptAsync: async args => { resumes++; status = "busy";
    if (args.body.messageID !== messages.find(m => m.info.role === "user")?.info.id || args.body.parts.length !== 0)
      throw Error("wrong native continuation");
    return sdkResult(undefined, 204);
  },
};
const hooks = await AgentHarnessGuards({directory: process.cwd(), worktree: process.cwd(), client: {session}});
const fireIdle = async sessionID => {
  status = "idle";
  await hooks.event({event: {type: "session.status", properties: {sessionID, status: {type: "idle"}}}});
  await hooks.event({event: {type: "session.idle", properties: {sessionID}}});
};
const duplicate = {info: {id: "old-duplicate", role: "user", sessionID: sid,
  time: {created: Date.now()}}, parts: [{type: "text", text: FIXTURE_EXPECTED_TEXT}]};
messages.push(duplicate, {...duplicate, info: {...duplicate.info, id: "second-duplicate"}});
await fireIdle(sid);
const duplicates = {saves, resumes};
messages.length = 0;
await fireIdle(sid);
const savedID = messages[0]?.info.id;
messages.push({info: {id: "wrong-parent", sessionID: sid, role: "assistant",
  parentID: "foreign-message", time: {completed: Date.now()}}, parts: []});
await fireIdle(sid);
const wrongParent = {saves, resumes};
messages.pop();
messages.push({info: {id: "assistant-1", sessionID: sid, role: "assistant",
  parentID: savedID, time: {completed: Date.now()}}, parts: []});
await fireIdle(sid);
await fireIdle("ses_child");
hooks.dispose();
console.log(JSON.stringify({duplicates, wrongParent, saves, resumes}));
'''.replace("FIXTURE_EXPECTED_TEXT", json.dumps(text)))
        self.assertEqual(result["duplicates"], {"saves": 0, "resumes": 0})
        self.assertEqual(result["wrongParent"], {"saves": 1, "resumes": 1})
        self.assertEqual((result["saves"], result["resumes"]), (1, 1))
        self.assertEqual(pm._read_pending(ref)["state"], "received")

    def test_opencode_busy_questions_permissions_and_nonroot_sid_do_not_receive_prompt(self):
        recipient = {"harness": "opencode", "session_id": "ses_A"}
        _text, ref = pm.prepare_peer_message("busy OpenCode body", self.sender, recipient, defer=True)
        result = self.run_pending_agent_initializer_fixture(r'''
let status = "busy", gets = 0, saves = 0;
const session = {
  status: async () => ({data: {ses_A: {type: status}, ses_child: {type: status}}, error: undefined,
    response: {ok: true, status: 200}}),
  get: async ({path}) => {gets++;return {data: {id: path.id, projectID: "project_fixture",
    directory: process.cwd(), slug: "fixture-child", title: "child", version: "1.19.0", time: {created: 1, updated: 1},
    parentID: "not-root"}, error: undefined, response: {ok: true, status: 200}}},
  messages: async () => ({data: [], error: undefined, response: {ok: true, status: 200}}),
  prompt: async () => {saves++;throw Error("blocked session received peer message")},
  promptAsync: async () => {throw Error("blocked session resumed")},
};
const hooks = await AgentHarnessGuards({directory: process.cwd(), worktree: process.cwd(), client: {session}});
await hooks.event({event: {type: "session.status", properties: {sessionID: "ses_A", status: {type: "busy"}}}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses_A"}}});
await hooks.event({event: {type: "question.asked", properties: {sessionID: "ses_A"}}});
await hooks.event({event: {type: "permission.asked", properties: {sessionID: "ses_A"}}});
await hooks["tool.execute.after"]({sessionID: "ses_A", tool: "fixture", args: {}}, {output: ""});
status = "idle";
await hooks.event({event: {type: "session.status", properties: {sessionID: "ses_A", status: {type: "idle"}}}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses_child"}}});
await hooks.event({event: {type: "session.idle", properties: {sessionID: "ses_A"}}});
hooks.dispose();
console.log(JSON.stringify({gets, saves}));
''')
        self.assertEqual(result["saves"], 0)
        self.assertEqual(pm._read_pending(ref)["state"], "pending")

    def test_claude_actual_prompt_observes_ref_once_without_output_ack(self):
        recipient = {"harness": "claude", "session_id": "recipient-a"}
        text, ref = pm.prepare_peer_message("original peer body", self.sender, recipient, defer=True)
        AliasReceive._receive_actual(self, "claude", text)
        original = self.rows()
        AliasReceive._receive_actual(self, "claude", text)
        self.assertEqual(self.rows(), original)
        self.assertEqual(pm._read_pending(ref)["state"], "received")

    def test_codex_actual_prompt_observes_ref_once_without_queue_acceptance_ack(self):
        text, ref = pm.prepare_peer_message("original peer body", self.sender, self.recipient, defer=True)
        AliasReceive._receive_actual(self, "codex", text)
        original = self.rows()
        AliasReceive._receive_actual(self, "codex", text)
        self.assertEqual(self.rows(), original)
        self.assertEqual(pm._read_pending(ref)["state"], "received")


class AliasReceiveFixtureCleanupBoundaryTest(unittest.TestCase):
    def _run_probe(self, mode):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable for the fixture cleanup boundary probe")
        observations = {}
        original_cleanup = tempfile.TemporaryDirectory.cleanup
        writer = (
            "import os,time\n"
            "d=os.environ['FIXTURE_DIR']\n"
            "open(os.path.join(d,'ready'),'w').write(str(os.getpid()))\n"
            "i=0\n"
            "while True:\n"
            "    i+=1; open(os.path.join(d,'write-%d'%i),'w').close(); time.sleep(0.005)\n"
        )
        node_source = r'''
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
const child = spawn(process.env.FIXTURE_PYTHON, ["-c", process.env.FIXTURE_WRITER], {
  env: process.env, detached: true, stdio: "ignore"
});
child.unref();
const ready = `${process.env.FIXTURE_DIR}/ready`;
const deadline = Date.now() + 10000;
while (!existsSync(ready) && Date.now() < deadline)
  await new Promise((resolve) => setTimeout(resolve, 5));
if (!existsSync(ready)) process.exit(4);
if (process.env.FIXTURE_HANG_NODE === "1") setInterval(() => {}, 1000);
'''

        class Probe(AliasReceive):
            def __init__(self):
                super().__init__("runTest")

            def _launch_writer(self, *, hang=False):
                env = {
                    **os.environ,
                    "FIXTURE_DIR": str(self.state),
                    "FIXTURE_PYTHON": sys.executable,
                    "FIXTURE_WRITER": writer,
                }
                if hang:
                    env["FIXTURE_HANG_NODE"] = "1"
                return subprocess.run(
                    [node, "--input-type=module", "-e", node_source],
                    cwd=ROOT, env=env, text=True, capture_output=True,
                    timeout=3 if hang else 10,
                )

            def _record_writer(self):
                pid = int((self.state / "ready").read_text(encoding="utf-8"))
                start = fixture_processes._start_ticks(pid)
                self.assertIsNotNone(start)
                self.assertNotIn(str(self.state).encode(), Path(f"/proc/{pid}/cmdline").read_bytes())
                observations.update(root=str(self.state), pid=pid, start=start)
                self.assertIn(pid, fixture_processes.marked_processes(str(self.state)))

            def setUp(self):
                super().setUp()
                if mode != "timeout":
                    run = self._launch_writer()
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    self._record_writer()
                if mode == "setup-error":
                    raise RuntimeError("intentional setup failure after detached writer start")

            def runTest(self):
                if mode == "timeout":
                    with self.assertRaises(subprocess.TimeoutExpired):
                        self._launch_writer(hang=True)
                    self._record_writer()
                pid = observations["pid"]
                self.assertIn(pid, fixture_processes.marked_processes(observations["root"]))
                if mode == "assertion-error":
                    self.fail("intentional body assertion failure")

        def observe_before_delete(temporary_directory):
            if temporary_directory.name == observations.get("root"):
                pid, start = observations["pid"], observations["start"]
                observations["remaining"] = fixture_processes.marked_processes(temporary_directory.name)
                observations["gone_before_delete"] = fixture_processes._gone(pid, start)
                observations["root_present_before_delete"] = Path(temporary_directory.name).is_dir()
            return original_cleanup(temporary_directory)

        result = unittest.TestResult()
        with mock.patch.object(tempfile.TemporaryDirectory, "cleanup", observe_before_delete):
            Probe().run(result)
        return result, observations

    def _assert_cleanup_evidence(self, observations):
        self.assertTrue(observations["root_present_before_delete"])
        self.assertTrue(observations["gone_before_delete"])
        self.assertEqual(observations["remaining"], {})
        self.assertFalse(Path(observations["root"]).exists())

    def test_alias_fixture_drains_detached_writer_after_normal_body(self):
        result, observations = self._run_probe("normal")
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self._assert_cleanup_evidence(observations)

    def test_alias_fixture_preserves_body_assertion_failure_and_drains_writer(self):
        result, observations = self._run_probe("assertion-error")
        self.assertEqual(len(result.failures), 1)
        self.assertIn("intentional body assertion failure", result.failures[0][1])
        self.assertEqual(result.errors, [])
        self._assert_cleanup_evidence(observations)

    def test_alias_fixture_drains_writer_after_setup_failure(self):
        result, observations = self._run_probe("setup-error")
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn("intentional setup failure", result.errors[0][1])
        self._assert_cleanup_evidence(observations)

    def test_alias_fixture_drains_writer_after_node_timeout(self):
        result, observations = self._run_probe("timeout")
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self._assert_cleanup_evidence(observations)


if __name__ == "__main__":
    unittest.main()
