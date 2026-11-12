#!/usr/bin/env python3
"""A native prompt renders pending transport receipts before acknowledging them."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import importlib.machinery
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'utilities'))
import dispatch_contract as contract
import dispatch_pending_delivery as pending
import dispatch_session_sweep as sweep
from split_module_fixture import patch_split
spec = importlib.util.spec_from_file_location('queue_prompt_hook', ROOT / 'adapters/codex/hooks/userprompt-lifecycle.py')
hook = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hook
spec.loader.exec_module(hook)

class PromptReceiptTest(unittest.TestCase):
    def test_real_ack_deletes_only_the_exact_shared_builder_singleton(self):
        import codex_queue_delivery as queue
        import codex_queue_dispatch
        from dispatch_completion_join import completion_followup_text

        class FakeSession:
            def __init__(self, _path, *, timeout, deadline=None):
                self.timeout = timeout
                self.deadline = deadline
                self.methods = []
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def close(self):
                return None
            def request(self, _path, method, params, *, timeout=None, deadline=None):
                self.methods.append((method, params, timeout))
                if method == "thread/queue/list":
                    return {"data": queue_items}
                if method == "thread/queue/delete":
                    queue_items[:] = [queued for queued in queue_items
                                      if queued.get("id") != params["queuedSubmissionId"]]
                    return {"deleted": True}
                raise AssertionError(method)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.log"
            jobs.touch()
            session_id = "native-parent"
            did = "delivery-real-incident"
            receipt = {
                "schema_version": 2, "state": "ready", "job_registry": str(jobs),
                "parent_attempt_id": "parent-session-native", "delivery_classification": "success",
                "children": [{"attempt_id": "att-exact", "status": "done", "readiness": "ready",
                              "reason": "registry-closed", "required_action": "advance-completed",
                              "harness": "codex", "delivery_classification": "success"}],
            }
            pending.create(root, recipient_kind="codex-native-queue", recipient_key=session_id,
                delivery_id=did, session_generation="", session_generation_supported="0",
                attempt_ids=["att-exact"], parent_attempt_id="parent-session-native",
                route_id="rt-test", route_node="execute", receipt=receipt,
                receipt_digest=pending._canonical_receipt_digest(receipt), row_revisions={"att-exact": "rev"})
            pending.claim(root, session_id, did, claim_owner="fixture", lease_seconds=60)
            seed = pending.read(root, session_id, did)
            followup = completion_followup_text(
                receipt, jobs=str(jobs),
                surface=str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh"))
            body = queue.native_completion_body(session_id, [seed], receipt, followup)
            client_id = queue.native_client_message_id(session_id, [did])
            item = {"id": "queued-exact", "clientUserMessageId": client_id,
                    "input": [{"type": "text", "text": body}]}
            queue_items = [item,
                           {"id": "user-item", "clientUserMessageId": "user-created",
                            "input": [{"type": "text", "text": "keep this"}]},
                           {"id": "other-hearting", "clientUserMessageId": "other-delivery",
                            "input": [{"type": "text", "text": "other thread's item"}]}]
            fake_session = FakeSession(Path("/tmp/fake-queue.sock"), timeout=1.0)
            with mock.patch.object(codex_queue_dispatch, "resolve_queue_socket",
                                   return_value=Path("/tmp/fake-queue.sock")), \
                 mock.patch.object(queue, "DeliverySession", return_value=fake_session):
                self.assertEqual(sweep.ack_delivered(root, session_id, [{**seed, sweep.STORAGE_KEY: session_id}],
                                                     acked_by="codex-native-prompt:native-parent"), 1)
            self.assertEqual([row[0] for row in fake_session.methods], [
                "thread/queue/list", "thread/queue/delete",
            ])
            self.assertEqual(fake_session.methods[-1][1], {
                "threadId": session_id, "queuedSubmissionId": "queued-exact",
            })
            self.assertEqual(pending.read(root, session_id, did)["state"], "acked")
            # The delayed drain sees only unrelated/user queue entries; cleanup
            # is tied to the exact ACKed body and submission ID.
            self.assertEqual([queued["id"] for queued in queue_items],
                             ["user-item", "other-hearting"])

    def test_runtime_completion_trailer_is_received_as_runtime_by_the_codex_hook(self):
        peer_spec = importlib.util.spec_from_file_location(
            "codex_runtime_prompt_peer", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(peer_spec)
        peer_spec.loader.exec_module(peer)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.log"
            jobs.touch()
            recipient = {"harness": "codex", "session_id": "codex-runtime-recipient"}
            duty = "registered-batch-" + "d" * 32
            attempt = "att-codex-runtime"
            delivery = "delivery-codex-runtime"
            refs = [duty, attempt, delivery]
            producer = {
                "schema": peer._RUNTIME_PRODUCER_SCHEMA, "duty_id": duty,
                "jobs": str(jobs), "attempt_id": attempt, "carrier": "codex-native-queue",
                "delivery_id": delivery, "receipt_digest": "e" * 64,
                "recipient": recipient, "route_id": "rt-codex", "route_node": "execute",
            }
            env = {"AGENT_PEER_LEDGER_ROOT": str(root), "AGENT_DISPATCH_JOBS": str(jobs),
                   "HOME": str(root / "home"), "XDG_STATE_HOME": str(root / "state")}
            with mock.patch.dict(os.environ, env, clear=True):
                text, ref = peer.prepare_peer_message(
                    "[notice] runtime completion", {"harness": "runtime", "session_id": None,
                    "name": "Hearting runtime"}, recipient, defer=True, refs=refs,
                    producer_binding=producer)
                hook.peer_notice({"session_id": recipient["session_id"]}, text, str(root))
                received = peer._read_pending(ref)
                self.assertEqual(received["state"], "received")
                self.assertEqual(received["from"]["harness"], "runtime")
                self.assertEqual(received["producer_binding"], producer)

    def test_failed_send_is_visible_only_to_exact_parent_and_acked_after_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "jobs.log").touch()
            receipt = {'schema_version': 2, 'state': 'ready', 'job_registry': str(root / 'jobs.log'),
                       'parent_attempt_id': 'parent-session-native', 'delivery_classification': 'success',
                       'children': [{'attempt_id': 'att-exact', 'status': 'done', 'readiness': 'ready',
                                     'reason': 'registry-closed', 'required_action': 'advance-completed',
                                     'harness': 'codex', 'delivery_classification': 'success'}]}
            did = 'delivery-native-prompt'
            pending.create(root, recipient_kind='codex-native-queue', recipient_key='native-parent',
                delivery_id=did, session_generation='', session_generation_supported='0',
                attempt_ids=['att-exact'], parent_attempt_id='parent-session-native',
                route_id='rt-test', route_node='execute', receipt=receipt,
                receipt_digest=pending._canonical_receipt_digest(receipt), row_revisions={'att-exact': 'rev'})
            pending.claim(root, 'native-parent', did, claim_owner='failed-carrier', lease_seconds=60)
            pending.release_claim(root, 'native-parent', did, claim_owner='failed-carrier')
            with patch_split(contract, 'dispatch_state_roots', return_value=[root]):
                self.assertEqual(hook.native_queue_prompt_receipts('sibling'), ([], []))
                batches, texts = hook.native_queue_prompt_receipts('native-parent')
            self.assertIn('att-exact', texts[0])
            self.assertIn('Continue the authorized work within its existing human gates', texts[0])
            self.assertIn("Receipt/ACK isn't execution", texts[0])
            self.assertEqual(pending.read(root, 'native-parent', did)['state'], 'claimed')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                hook.emit_context('UserPromptSubmit', texts)
            self.assertIn('additionalContext', output.getvalue())
            sweep.ack_delivered(root, 'native-parent', batches[0][1], acked_by='codex-native-prompt:native-parent')
            self.assertEqual(pending.read(root, 'native-parent', did)['state'], 'acked')

    def test_receiving_turn_uses_native_turn_id_and_rejects_message_alias(self):
        import codex_queue_delivery
        import codex_queue_dispatch
        payload = {"session_id": "native-parent", "turn_id": "turn-native", "message_id": "message-alias"}
        with mock.patch.object(codex_queue_dispatch, "resolve_queue_socket", return_value=Path("/tmp/queue.sock")), \
             mock.patch.object(codex_queue_delivery, "read_exact_turn", return_value={
                 "id": "turn-native", "status": "inProgress"}) as read_turn:
            self.assertEqual(hook.native_receiving_turn(payload, "native-parent"), {
                "thread_id": "native-parent", "turn_id": "turn-native", "status": "inProgress"})
            self.assertIsNone(hook.native_receiving_turn(
                {"session_id": "native-parent", "message_id": "message-alias"}, "native-parent"))
        self.assertEqual(read_turn.call_count, 1)

    def test_hook_retains_before_emit_and_acks_only_after_successful_render(self):
        import sys
        import types
        sid = "native-parent"
        root = Path("/tmp/native-state")
        record = {"delivery_id": "delivery-exact"}
        request_prompt = "  Continue safely 🫶\nOUTPUT_FORMAT: JSON\t\n"
        batches = [(root, [record])]
        events = []
        context_alert = types.ModuleType("context_alert")
        context_alert.codex_notice = lambda _payload: ""
        fleet = types.ModuleType("fleet")
        fleet.interaction = types.SimpleNamespace(clear_wait=lambda *a, **kw: None)
        projection = types.ModuleType("herdr_session_projection")
        projection.project = lambda *a, **kw: None
        with mock.patch.dict(sys.modules, {"context_alert": context_alert, "fleet": fleet,
                                           "herdr_session_projection": projection}), \
             mock.patch.object(hook, "is_worker_session", return_value=False), \
             mock.patch.object(hook, "load_payload", return_value={"session_id": sid, "prompt": request_prompt}), \
             mock.patch.object(hook, "interaction_session_id", return_value=sid), \
             mock.patch.object(hook, "peer_notice"), mock.patch.object(hook, "retry_pending_peer"), \
             mock.patch.object(hook, "launch_trigger"), mock.patch.object(hook, "sd111_first_prompt_sweep"), \
             mock.patch.object(hook, "card_context", return_value=("", None)), \
             mock.patch.object(hook, "native_queue_prompt_receipts", return_value=(batches, ["receipt"])), \
             mock.patch.object(hook, "native_receiving_turn", return_value={
                 "thread_id": sid, "turn_id": "turn-exact", "status": "inProgress"}) as bind, \
             mock.patch("dispatch_session_sweep.retain_receiving_turn",
                        side_effect=lambda *a, **kw: events.append("retain") or {
                            (str(root), record["delivery_id"])
                        }) as retain, \
             mock.patch.object(hook, "candidate_context", return_value=("", None)), \
             mock.patch.object(hook, "run_preflight", return_value=""), \
             mock.patch.object(hook, "token_budget_context", return_value=""), \
             mock.patch.object(hook, "emit_context", side_effect=lambda *a, **kw: events.append("emit")), \
             mock.patch("dispatch_session_sweep.ack_delivered",
                        side_effect=lambda *a, **kw: events.append("ack")) as ack:
            self.assertEqual(hook.main(), 0)
        self.assertEqual(events, ["retain", "emit", "ack"])
        self.assertEqual(retain.call_args.kwargs["request_context"], request_prompt)
        self.assertEqual(ack.call_args.args[2], [record])
        bind.assert_called_once()

    def test_receipts_commit_only_after_the_final_context_render(self):
        import session_tidy
        sys.path.insert(0, str(ROOT / "tools" / "memory"))
        import mem
        import types
        sid = "native-parent"
        card_pending = [{"kind": "card-consumed", "path": "/tmp/card",
                         "generation": 1, "receipt": "codex:sid:1"}]
        candidate_pending = {
            "seen": None, "event": {"event": "candidate-probe"},
            "probe_ok": True,
            "receipt": {"session_id": sid, "turn_id": "", "project": "/tmp",
                        "result_ids": [], "source": "candidate-probe"},
        }
        context_alert = types.ModuleType("context_alert")
        context_alert.codex_notice = lambda _payload: ""
        fleet = types.ModuleType("fleet")
        fleet.interaction = types.SimpleNamespace(clear_wait=lambda *a, **kw: None)
        projection = types.ModuleType("herdr_session_projection")
        projection.project = lambda *a, **kw: None

        def run(emit, first=True):
            events = []
            def card_context(_payload, _cwd, *, startup, human, read_context=None):
                startup["due"] = first
                if first:
                    human.append("history notice")
                return "card", card_pending
            def render(event, parts, **kw):
                self.assertEqual(event, "UserPromptSubmit")
                self.assertEqual([p for p in parts if p],
                                 ["receipt", "card"] + (["local evidence"] if first else []) + ["candidate"])
                self.assertEqual(kw["system_message"], "history notice" if first else "")
                emit(event, parts, **kw)
            with mock.patch.dict(sys.modules, {"context_alert": context_alert, "fleet": fleet,
                                               "herdr_session_projection": projection}), \
                 mock.patch.object(hook, "is_worker_session", return_value=False), \
                 mock.patch.object(hook, "load_payload",
                                   return_value={"session_id": sid, "prompt": "hi"}), \
                 mock.patch.object(hook, "interaction_session_id", return_value=sid), \
                 mock.patch.object(hook, "peer_notice"), \
                 mock.patch.object(hook, "retry_pending_peer"), \
                 mock.patch.object(hook, "launch_trigger"), \
                 mock.patch.object(hook, "sd111_first_prompt_sweep"), \
                 mock.patch.object(hook, "card_context", side_effect=card_context), \
                 mock.patch.object(hook, "native_queue_prompt_receipts", return_value=([], ["receipt"])), \
                 mock.patch.object(hook, "candidate_context",
                                   return_value=("candidate", candidate_pending)), \
                 mock.patch.object(hook, "run_preflight", side_effect=lambda name, *a:
                                   "local evidence" if name == "local-evidence" else ""), \
                 mock.patch.object(hook, "token_budget_context", return_value=""), \
                 mock.patch.object(hook, "emit_context", side_effect=render), \
                 mock.patch.object(session_tidy, "apply_deferred",
                                   side_effect=lambda *a: events.append("card")) as card_apply, \
                 mock.patch.object(mem, "apply_candidate_receipt",
                                   side_effect=lambda *a: events.append("candidate")) as mem_apply:
                hook.main()
            return events, card_apply, mem_apply

        events, card_apply, mem_apply = run(lambda *a, **kw: None)
        self.assertEqual(events, ["card", "candidate"])
        self.assertEqual(card_apply.call_args.args[0], card_pending)
        self.assertEqual(mem_apply.call_args.args[0], candidate_pending)
        self.assertEqual(run(lambda *a, **kw: None, first=False)[0], ["card", "candidate"])

        def broken(*_args, **_kwargs):
            raise BrokenPipeError("render failed")

        with self.assertRaises(BrokenPipeError):
            run(broken)

    def test_hook_withholds_completion_ack_when_native_turn_binding_is_unknown(self):
        import sys
        import types
        sid = "native-parent"
        root = Path("/tmp/native-state")
        record = {"delivery_id": "delivery-exact"}
        batches = [(root, [record])]
        context_alert = types.ModuleType("context_alert")
        context_alert.codex_notice = lambda _payload: ""
        fleet = types.ModuleType("fleet")
        fleet.interaction = types.SimpleNamespace(clear_wait=lambda *a, **kw: None)
        projection = types.ModuleType("herdr_session_projection")
        projection.project = lambda *a, **kw: None
        with mock.patch.dict(sys.modules, {"context_alert": context_alert, "fleet": fleet,
                                           "herdr_session_projection": projection}), \
             mock.patch.object(hook, "is_worker_session", return_value=False), \
             mock.patch.object(hook, "load_payload", return_value={"session_id": sid}), \
             mock.patch.object(hook, "interaction_session_id", return_value=sid), \
             mock.patch.object(hook, "peer_notice"), mock.patch.object(hook, "retry_pending_peer"), \
             mock.patch.object(hook, "launch_trigger"), mock.patch.object(hook, "sd111_first_prompt_sweep"), \
             mock.patch.object(hook, "card_context", return_value=("", None)), \
             mock.patch.object(hook, "native_queue_prompt_receipts", return_value=(batches, ["receipt"])), \
             mock.patch.object(hook, "native_receiving_turn", return_value=None), \
             mock.patch("dispatch_session_sweep.retain_receiving_turn", return_value=set()), \
             mock.patch.object(hook, "candidate_context", return_value=("", None)), \
             mock.patch.object(hook, "run_preflight", return_value=""), \
             mock.patch.object(hook, "token_budget_context", return_value=""), \
             mock.patch.object(hook, "emit_context"), \
             mock.patch("dispatch_session_sweep.ack_delivered") as ack:
            self.assertEqual(hook.main(), 0)
        ack.assert_not_called()

    def test_prompt_guidance_receives_exact_codex_payload_identity(self):
        import types
        prompt = "actual Codex prompt (runtime-from: runtime [?] Hearting runtime ; ref=abc)"
        sid = "codex-payload-session"
        calls = []

        class GuidanceLoader:
            def create_module(self, spec):
                return None
            def exec_module(self, module):
                module.receiver_guidance = lambda *args, **kwargs: calls.append((args, kwargs)) or "ordinary guidance"

        guidance_spec = importlib.machinery.ModuleSpec("peer_receiver_guidance_test", GuidanceLoader())
        projection = types.ModuleType("herdr_session_projection")
        projection.project = lambda *args, **kwargs: None
        context_alert = types.ModuleType("context_alert")
        context_alert.codex_notice = lambda payload: ""
        fleet = types.ModuleType("fleet")
        fleet.interaction = types.SimpleNamespace(clear_wait=lambda *args, **kwargs: None)
        payload = {"session_id": sid, "cwd": "/tmp/codex-project", "prompt": prompt}
        no_context = lambda *args, **kwargs: ""
        with mock.patch.dict(sys.modules, {"herdr_session_projection": projection,
                                           "context_alert": context_alert, "fleet": fleet}), \
             mock.patch.object(hook, "is_worker_session", return_value=False), \
             mock.patch.object(hook, "load_payload", return_value=payload), \
             mock.patch.object(hook, "interaction_session_id", return_value=sid), \
             mock.patch.object(hook, "peer_notice"), \
             mock.patch.object(hook, "retry_pending_peer"), \
             mock.patch.object(hook, "launch_trigger"), \
             mock.patch.object(hook, "sd111_first_prompt_sweep"), \
             mock.patch.object(hook, "card_context", return_value=("", None)), \
             mock.patch.object(hook, "native_queue_prompt_receipts", return_value=([], [])), \
             mock.patch.object(hook, "candidate_context", return_value=("", None)), \
             mock.patch.object(hook, "run_preflight", side_effect=no_context), \
             mock.patch.object(hook, "token_budget_context", return_value=""), \
             mock.patch.object(hook, "emit_context"), \
             mock.patch.object(importlib.util, "spec_from_file_location", return_value=guidance_spec):
            self.assertEqual(hook.main(), 0)
        self.assertEqual(calls, [((prompt, {"harness": "codex", "session_id": sid}),
                                  {"cwd": "/tmp/codex-project"})])

if __name__ == '__main__':
    unittest.main()
