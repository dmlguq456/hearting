"""A Claude completion the runner could not hand to a resting shell is still carried once.

The runner (peer-steward) reaches a route parent after the rewake and the hooks have each
had their turn. It claims and injects a Claude completion only through a Hearting shell that
is resting with an empty input; anywhere else the record stays pending so the turn's Stop
(or the next input) carries it. These are the ordered sequences of the independent review
of PR #1647 (RV-PR1647-1115), run with the real runner, the real hooks and a fake shell host
and lease clock, in and outside a shell. Each completion must reach the model once; only
the orders the documentation names (an API error that skips Stop) need a person.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import io
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "hooks"))

import parent_context_fixture as fx  # noqa: E402

S = 10**9
# Times are seconds on the lease clock.
SEQUENCES = {
    # Two completions held by one boundary watcher: A wakes the session, B's arm is left lapsed.
    "two together, short turn": [
        ("arm", "A"), ("arm", "B"), ("done", "A"), ("wake", "A"), ("lapse", "B"), ("at", 5),
        ("done", "B"), ("at", 6), ("runner",), ("at", 8), ("tool",), ("at", 15), ("stop",), ("rest",)],
    "two together, long turn": [
        ("arm", "A"), ("arm", "B"), ("done", "A"), ("wake", "A"), ("lapse", "B"), ("at", 5),
        ("done", "B"), ("at", 6), ("runner",), ("at", 8), ("tool",), ("at", 45), ("stop",), ("rest",)],
    # The hook died after arming; the child ends during a busy turn the runner already saw.
    "hook died, rest 12 s apart": [
        ("arm", "A"), ("done", "A"), ("at", 2), ("runner",), ("at", 5), ("tool",), ("at", 22),
        ("stop",), ("rest", 12)],
    "hook died, rest 5 s apart": [
        ("arm", "A"), ("done", "A"), ("at", 2), ("runner",), ("at", 5), ("tool",), ("at", 22),
        ("stop",), ("rest", 5)],
    "hook died, no tool": [
        ("arm", "A"), ("done", "A"), ("at", 2), ("runner",), ("at", 22), ("stop",), ("rest", 12)],
    # The turn ends in an API error (no Stop): inside a shell the resting shell takes it.
    "hook died, API error": [("arm", "A"), ("done", "A"), ("at", 5), ("tool",), ("rest", 10)],
}
# Orders no Stop reaches and no resting shell exists for: the next input carries the completion.
NEEDS_A_PERSON = {("hook died, API error", "out")}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_sequence(shell, steps):
    """Drive one sequence; return ``{completion: {"roads": [...], "person": n, "final": state}}``."""
    import dispatch_pending_delivery as pending
    import peer_obligations
    import peer_address
    import session_shell
    with tempfile.TemporaryDirectory(prefix="ccs-") as tmp, fx._own_obligation_runners():
        sc = fx.Scenario(ROOT, Path(tmp), "claude", completions=(("normal", "a"), ("normal", "b")))
        sc.build()
        sid = fx.PARENT_SID
        rewake = _load(ROOT / "hooks" / "dispatch-owner-rewake.py", "_retained_rewake")
        sweep_hook = _load(ROOT / "hooks" / "dispatch-session-sweep.py", "ccs_sweep_hook")
        record_hook = _load(ROOT / "hooks" / "peer-message-record.py", "ccs_record_hook")
        steward = _load(ROOT / "utilities" / "peer-steward.py", "ccs_steward")
        items = {"A": sc.items[0], "B": sc.items[1]}
        sc.transcript_path().write_text("", encoding="utf-8")
        log, injected = [], []
        shell_state = {"idle": False}
        claims = {}
        binding = {"harness": "claude", "sid": sid, "incarnation": "i1", "host_pid": 4242,
                   "host_start": "1", "native_pid": 4243, "native_start": "1",
                   "socket": "/nonexistent.sock"}
        shell_binding = (binding, "") if shell == "in" else (None, "attachment-unavailable")

        def control(_binding, operation, **fields):
            if operation == "status":
                return {"ok": True, "state": "idle" if shell_state["idle"] else "busy",
                        "empty": True, "epoch": 1}
            if operation == "inject":
                injected.append(fields.get("ref"))
                return {"ok": True}
            return {"ok": False, "reason": "unsupported", "not_started": True}

        t0 = sc.clock[0]

        def now():
            return (sc.clock[0] - t0) // S

        def reached(carrier, text, *, person=False):
            for key, item in items.items():
                if text and item["delivery_id"] in text:
                    log.append({"c": key, "carrier": carrier, "t": now(), "person": person})

        def hook(mode, payload):
            record_hook._STARTED = time.monotonic()
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["peer-message-record.py", mode]), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                    mock.patch.object(sys, "stdout", out):
                record_hook.main()
            text = ""
            for line in out.getvalue().splitlines():
                with contextlib.suppress(ValueError, KeyError, TypeError, AttributeError):
                    body = json.loads(line)
                    text += body.get("reason", "") if body.get("decision") == "block" else \
                        body["hookSpecificOutput"]["additionalContext"]
            return text

        def tool():
            text = hook("post-tool", sc.payload("PostToolUse", tool_name="Bash",
                                                tool_input={"command": "true"}))
            if text:
                sc.transcript({"type": "attachment", "attachment": {
                    "type": "hook_additional_context", "hookEvent": "PostToolUse", "content": [text]}})
            reached("post-tool", text)

        def stop():
            active = False
            for _ in range(4):
                text = hook("stop", sc.payload("Stop", **({"stop_hook_active": True} if active else {})))
                if text:
                    sc.transcript({"type": "user", "isMeta": True,
                                   "message": {"content": "Stop hook feedback:\n" + text}})
                reached("stop", text)
                if not text:
                    return
                active = True

        def prompt(text_in, *, person, label):
            sc.turn += 1
            payload = sc.payload("UserPromptSubmit", prompt=text_in)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                sweep_hook._process_session(payload, sid)
            context = ""
            for line in out.getvalue().splitlines():
                with contextlib.suppress(ValueError, KeyError, TypeError):
                    context += json.loads(line)["hookSpecificOutput"]["additionalContext"]
            context += hook("prompt", payload)
            if text_in.lstrip().startswith("<task-notification>"):
                sc.transcript({"type": "user", "origin": {"kind": "task-notification",
                               "producer": "session-task"}, "promptSource": "system",
                               "turnOrigin": "task_notification", "message": {"content": text_in}})
            else:
                sc.transcript({"type": "user", "message": {"content": text_in}})
            if context:
                sc.transcript({"type": "attachment", "attachment": {
                    "type": "hook_additional_context", "hookEvent": "UserPromptSubmit",
                    "content": [context]}})
            reached(label, text_in, person=person)
            reached("prompt-hook", context, person=person)

        def runner():
            stores = [peer_obligations.ObligationStore(sc.jobs.parent), peer_obligations.ObligationStore()]
            for _round in range(2):
                for store in stores:
                    for duty in store.list():
                        if duty.get("state") in {"complete", "cancelled"}:
                            continue
                        kind = (duty.get("intent") or {}).get("kind")
                        if kind == "registered-batch":
                            steward._resume_registered_obligation(duty, store)
                        elif kind == "message":
                            steward._resume_message_obligation(duty, store)
            while injected:
                row = steward.peer_message._read_pending(injected.pop(0))
                prompt((row or {}).get("text") or "", person=False, label="shell-inject")
                stop()

        def rewake_emit(item):
            launch = rewake.Launch(attempt_id=item["attempt"], jobs=sc.jobs, session_id=sid, armed="registry")
            metadata = {"delivery_id": item["delivery_id"], "parent_sid": sid}
            claim = claims.get(item["attempt"]) or rewake.claim_arm(sc.jobs, item["attempt"], sid, fresh=True)
            delivery = SimpleNamespace(status="done", row_revision=item["row_revision"], row_digest="d" * 64,
                                       owned_children=0, quiescent=True, advanced=False)
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(rewake, "_incarnation_binding_matches", return_value=True), \
                    mock.patch.object(rewake, "_gate_notices", return_value=[]), \
                    mock.patch.object(rewake, "settle_arm", return_value=True), \
                    mock.patch.object(rewake, "_ended", side_effect=lambda _c, code, **_k: code), \
                    mock.patch.object(rewake, "current_delivery_state", return_value=delivery), \
                    mock.patch.object(rewake, "_completion_evidence_current", return_value=True), \
                    mock.patch.object(rewake, "delivery_classification", return_value="success"), \
                    mock.patch.object(rewake, "delivery_required_action", return_value="advance-completed"), \
                    mock.patch.object(rewake.sys, "stdout", stdout), mock.patch.object(rewake.sys, "stderr", stderr):
                win = rewake._carrier_one_claim(launch, metadata, arm=claim)
                if not isinstance(win, getattr(rewake, "ClaimWin", ())):
                    return ""
                state, message = rewake.classified_receipt(launch, "ready", "terminal-complete", ROOT)
                from dispatch_recipient_receipt import receipt_marker
                message += "\n" + receipt_marker(pending.read(sc.state, sid, win.delivery_id), sid)
                code = rewake._emit_with_gates(launch, claim, state, message, block=False, owner_closed=True)
            if code != 2:
                return ""
            with contextlib.suppress(pending.PendingDeliveryError):
                pending.mark_sent_ambiguous(sc.state, sid, win.delivery_id, claim_owner=win.claim_owner)
            return (stderr.getvalue() or stdout.getvalue()).strip()

        with contextlib.ExitStack() as stack:
            for patch in (
                mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=True),
                mock.patch.object(peer_obligations, "request_dispatch_reconciliation", return_value=True),
                mock.patch.object(peer_address, "presence", return_value="present"),
                mock.patch.object(peer_address, "resolve",
                                  return_value=({"harness": "claude", "session_id": sid}, None)),
                mock.patch.object(session_shell, "exact_binding", return_value=shell_binding),
                mock.patch.object(session_shell, "control", side_effect=control),
                mock.patch.object(steward, "_current_shell_binding", return_value=shell_binding),
                mock.patch.object(steward, "_record", return_value=None),
                mock.patch.object(rewake, "_holder_alive", return_value=False),
                mock.patch("time.monotonic_ns", side_effect=lambda: sc.clock[0]),
            ):
                stack.enter_context(patch)
            for step in steps:
                op = step[0]
                if op == "arm":
                    item = items[step[1]]
                    claim = rewake.claim_arm(sc.jobs, item["attempt"], sid, fresh=True)
                    claims[item["attempt"]] = claim
                    peer_obligations.retain_registered_completion(
                        sc.jobs, item["attempt"], sid, "claude-parent-runtime",
                        holder=getattr(claim, "holder", None))
                elif op == "lapse":
                    rewake.settle_arm(claims[items[step[1]]["attempt"]], "lapsed")
                elif op == "done":
                    sc.materialize(items[step[1]])
                elif op == "wake":
                    note = sc.notification(rewake_emit(items[step[1]]))
                    sc.clock[0] += 1 * S
                    prompt(note, person=False, label="rewake")
                elif op == "at":
                    sc.clock[0] = t0 + step[1] * S
                elif op == "runner":
                    runner()
                elif op == "tool":
                    tool()
                elif op == "stop":
                    stop()
                elif op == "rest":
                    shell_state["idle"] = True
                    for _ in range(6):
                        sc.clock[0] += (step[1] if len(step) > 1 else 10) * S
                        runner()
            sc.clock[0] += 120 * S
            prompt("a person asks what has finished so far.", person=True, label="typed")
            stop()
        done = {s[1] for s in steps if s[0] == "done"}
        return {key: {"roads": [r["carrier"] for r in log if r["c"] == key and not r["person"]],
                      "person": len([r for r in log if r["c"] == key and r["person"]]),
                      "final": (pending.read(sc.state, sid, items[key]["delivery_id"]) or {}).get("state")}
                for key in sorted(done)}


class RunnerHeldCompletionTest(unittest.TestCase):
    def test_a_completion_is_carried_once_whether_or_not_a_shell_is_there(self):
        for name, steps in SEQUENCES.items():
            for shell in ("in", "out"):
                with self.subTest(sequence=name, shell=shell):
                    for key, got in run_sequence(shell, steps).items():
                        carried = len(got["roads"]) + got["person"]
                        self.assertEqual(carried, 1, (key, got))
                        self.assertEqual(got["final"], "acked")
                        if (name, shell) not in NEEDS_A_PERSON:
                            self.assertEqual(got["person"], 0, (key, got))

    def test_a_turn_that_ends_while_the_runner_watched_is_supplemented_by_its_stop(self):
        # Before this fix the runner's 30 s claim kept the Stop from carrying the completion:
        # outside a shell it waited for a person, inside one it was carried twice.
        for name in ("two together, short turn", "hook died, rest 12 s apart",
                     "hook died, rest 5 s apart", "hook died, no tool"):
            for shell in ("in", "out"):
                with self.subTest(sequence=name, shell=shell):
                    got = run_sequence(shell, SEQUENCES[name])
                    last = got["B" if "B" in got else "A"]
                    self.assertEqual(last["roads"], ["stop"])


if __name__ == "__main__":
    unittest.main()
