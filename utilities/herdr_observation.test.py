import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import herdr_observation as observation


class _MemorySocket:
    def __init__(self, server):
        self.server = server
        self.buffer = bytearray()
        self.closed = False

    def connect(self, _path):
        return None

    def settimeout(self, _timeout):
        return None

    def sendall(self, data):
        self.server.request(self, data)

    def recv(self, size):
        if not self.buffer:
            return b""
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def feed(self, data):
        self.buffer.extend(data)

    def readable(self):
        return bool(self.buffer) or self.closed

    def fileno(self):
        return id(self) & 0x7fffffff

    def close(self):
        self.closed = True
        self.server.disconnect_client(self)


class PaneCensusTest(unittest.TestCase):
    def test_successful_empty_census_confirms_absence_only_on_its_endpoint(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"result": {"panes": []}}), stderr=""))
        census = observation.pane_census(session="isolated", runner=runner,
                                         which=lambda _name: "/usr/bin/herdr")
        self.assertTrue(observation.confirms_absence(census, "w1:p9", endpoint="isolated"))
        self.assertFalse(observation.confirms_absence(census, "w1:p9", endpoint="default"))
        self.assertEqual(runner.call_args.args[0][:3], ["herdr", "--session", "isolated"])

    def test_error_malformed_or_duplicate_census_is_unknown(self):
        for code, body in ((1, "{}"), (0, "not-json"),
                           (0, json.dumps({"result": {"panes": [{"pane_id": "p"}, {"pane_id": "p"}]}}))):
            runner = lambda _argv, _code=code, _body=body, **_kw: subprocess.CompletedProcess(
                [], _code, stdout=_body, stderr="")
            self.assertIsNone(observation.pane_census(runner=runner, which=lambda _n: "herdr"))
        self.assertIsNone(observation.pane_census(which=lambda _n: None))


class EventReducerTest(unittest.TestCase):
    def setUp(self):
        self.state = observation.ObservationSnapshot("named")
        self.assertTrue(self.state.replace_snapshot("named", [{"pane_id": "w1:p9"}]))

    def test_pane_events_invalidate_instead_of_creating_terminal_facts(self):
        self.assertTrue(self.state.apply_event("named", {
            "event": "pane_closed", "data": {"pane_id": "w1:p9"}}))
        self.assertFalse(self.state.complete)
        self.assertEqual(self.state.terminal, {})
        self.assertFalse(self.state.confirms_absence("w1:p9"))
        self.assertTrue(self.state.replace_snapshot("named", [{
            "pane_id": "w1:p9", "agent": "codex",
            "agent_session": {"value": "sid-new"}}]))
        self.assertEqual(self.state.panes["w1:p9"]["agent_session"]["value"], "sid-new")
        self.assertEqual(self.state.terminal, {})
        self.assertTrue(self.state.apply_event("named", {
            "event": "pane.exited", "data": {"pane_id": "w1:p9"}}))
        self.assertFalse(self.state.complete)
        self.assertEqual(self.state.terminal, {})

    def test_reconnect_invalidates_census_and_cross_endpoint_events(self):
        self.state.reconnect()
        self.assertFalse(self.state.confirms_absence("w1:p9"))
        self.assertTrue(self.state.apply_event("named", {
            "event": "pane_closed", "data": {"pane_id": "w1:p9"}}))
        self.assertFalse(self.state.replace_snapshot("other", []))
        self.assertFalse(self.state.complete)

    def test_malformed_update_invalidates_snapshot(self):
        self.assertFalse(self.state.apply_event("named", {
            "event": "pane_updated", "data": {"pane_id": "w1:p9"}}))
        self.assertFalse(self.state.complete)

    def test_events_lost_error_invalidates_without_treating_it_as_an_event_kind(self):
        self.assertFalse(self.state.apply_event("named", {
            "id": "", "error": {"code": "events_lost", "message": "resnapshot"}}))
        self.assertFalse(self.state.complete)

    def test_agent_status_event_invalidates_and_does_not_override_snapshot(self):
        self.assertTrue(self.state.apply_event("named", {
            "event": "pane.agent_status_changed",
            "data": {"type": "pane_agent_status_changed", "pane_id": "w1:p9",
                     "agent_status": "idle"}}))
        self.assertFalse(self.state.complete)
        self.assertEqual(self.state.panes, {})
        self.assertEqual(self.state.terminal, {})


_PROTOCOL22_GLOBAL_PANE_SUBSCRIPTIONS = {
    "pane.created", "pane.updated", "pane.closed", "pane.exited",
    "pane.agent_detected",
}


def _protocol22_subscription_valid(subscription):
    if not isinstance(subscription, dict):
        return False
    event = subscription.get("type")
    if event in _PROTOCOL22_GLOBAL_PANE_SUBSCRIPTIONS:
        return True
    return (event == "pane.agent_status_changed"
            and isinstance(subscription.get("pane_id"), str)
            and bool(subscription["pane_id"]))


class _HerdrApiFixture:
    def __init__(self, path, *, panes=None, protocol=22, interleave=None,
                 repeat_interleave=False):
        self.path = str(path)
        self.panes = list(panes or [{"pane_id": "w1:p9", "agent": "codex"}])
        self.protocol = protocol
        self.interleave = interleave
        self.repeat_interleave = repeat_interleave
        self.methods = []
        self.errors = []
        self.subscribers = []
        self.subscriptions_by_client = {}
        self.subscription_requests = []
        self.lock = threading.Lock()
        self.stopping = False

    def connect(self, _family, _kind):
        return _MemorySocket(self)

    def request(self, client, data):
        try:
            request = json.loads(data.decode("utf-8").splitlines()[0])
            method = request.get("method")
            with self.lock:
                self.methods.append(method)
            if method == "events.subscribe":
                params = request.get("params")
                subscriptions = (params.get("subscriptions")
                                 if isinstance(params, dict) else None)
                valid = (isinstance(subscriptions, list) and bool(subscriptions)
                         and all(_protocol22_subscription_valid(item)
                                 for item in subscriptions))
                with self.lock:
                    self.subscription_requests.append(subscriptions)
                    if valid:
                        self.subscribers.append(client)
                        self.subscriptions_by_client[client] = subscriptions
                if valid:
                    response = {"id": request["id"], "result": {"type": "ok"}}
                else:
                    response = {"id": request["id"], "error": {
                        "code": "invalid_request",
                        "message": "invalid protocol 22 pane subscription"}}
                client.feed((json.dumps(response) + "\n").encode())
                return
            if method == "session.snapshot":
                if self.interleave is not None:
                    with self.lock:
                        subscribers = list(self.subscribers)
                    for subscriber in subscribers:
                        subscriber.feed((json.dumps(self.interleave) + "\n").encode())
                    if not self.repeat_interleave:
                        self.interleave = None
                snapshot = {"protocol": self.protocol, "panes": self.panes,
                            "agents": [{"agent": "codex", "pane_id": "w1:p9"}]}
                response = {"id": request["id"], "result": {
                    "type": "session_snapshot", "snapshot": snapshot}}
                client.feed((json.dumps(response) + "\n").encode())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def publish(self, envelope):
        data = envelope if isinstance(envelope, bytes) else (json.dumps(envelope) + "\n").encode()
        try:
            value = json.loads(data.decode().splitlines()[0])
        except (UnicodeDecodeError, ValueError, IndexError):
            value = None
        payload = value.get("data") if isinstance(value, dict) else None
        event = (value.get("event") or (payload.get("type")
                 if isinstance(payload, dict) else None)) if isinstance(value, dict) else None
        if isinstance(event, str) and "." not in event and "_" in event:
            prefix, suffix = event.split("_", 1)
            event = prefix + "." + suffix
        pane_id = payload.get("pane_id") if isinstance(payload, dict) else None
        pane = payload.get("pane") if isinstance(payload, dict) else None
        if pane_id is None and isinstance(pane, dict):
            pane_id = pane.get("pane_id")
        with self.lock:
            subscribers = [(client, self.subscriptions_by_client.get(client, []))
                           for client in self.subscribers]
        for subscriber, subscriptions in subscribers:
            if value is None or (isinstance(value, dict) and "error" in value):
                matches = True
            else:
                matches = any(
                    item.get("type") == event
                    and (event != "pane.agent_status_changed"
                         or item.get("pane_id") == pane_id)
                    for item in subscriptions if isinstance(item, dict))
            if not matches:
                continue
            subscriber.feed(data)

    def disconnect_client(self, client):
        with self.lock:
            self.subscribers = [row for row in self.subscribers if row is not client]
            self.subscriptions_by_client.pop(client, None)

    def disconnect_subscribers(self):
        with self.lock:
            subscribers = list(self.subscribers)
            self.subscribers.clear()
        for subscriber in subscribers:
            subscriber.close()

    def close(self):
        self.stopping = True
        with self.lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            subscriber.close()


class ObservationSocketOwnershipTest(unittest.TestCase):
    def test_inherited_socket_owner_is_herdr_session_or_default(self):
        socket_path = "/fixture/default.sock"
        env = {"HERDR_SOCKET_PATH": socket_path, "AGENT_HERDR_SESSION": "second"}
        self.assertEqual(observation.socket_path_for_endpoint("default", env), socket_path)
        self.assertIsNone(observation.socket_path_for_endpoint("second", env))
        env = {"HERDR_SOCKET_PATH": "/fixture/second.sock", "HERDR_SESSION": "second",
               "AGENT_HERDR_SESSION": "default"}
        self.assertEqual(observation.socket_path_for_endpoint("second", env), "/fixture/second.sock")
        self.assertIsNone(observation.socket_path_for_endpoint("default", env))
        self.assertEqual(observation.socket_path_for_endpoint("default", {
            "HERDR_SOCKET_PATH": socket_path, "HERDR_SESSION": ""}), socket_path)


class ObservationTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.socket_path = os.path.join(self.tmp.name, "herdr.sock")

    def start(self, **kwargs):
        server = _HerdrApiFixture(self.socket_path, **kwargs)
        self.addCleanup(server.close)
        readiness = mock.patch.object(
            observation.select, "select",
            side_effect=lambda reads, _writes, _errors, _timeout: (
                [sock for sock in reads
                 if isinstance(sock, _MemorySocket) and sock.readable()], [], []))
        readiness.start()
        self.addCleanup(readiness.stop)
        transport = observation.ObservationTransport(
            "isolated", self.socket_path, timeout=0.2, connector=server.connect)
        self.addCleanup(transport.close)
        return server, transport

    def test_protocol22_fixture_rejects_status_subscription_without_exact_pane(self):
        server = _HerdrApiFixture(self.socket_path)
        self.addCleanup(server.close)
        client = server.connect(socket.AF_UNIX, socket.SOCK_STREAM)
        request = {"id": "invalid-status-subscription", "method": "events.subscribe",
                   "params": {"subscriptions": [
                       {"type": "pane.agent_status_changed"}]}}
        server.request(client, (json.dumps(request) + "\n").encode())
        response = json.loads(client.recv(4096).splitlines()[0])
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(server.subscribers, [])

    def test_source_uses_valid_protocol22_endpoint_subscriptions(self):
        server, transport = self.start()
        view = transport.observe()
        self.assertIsNotNone(view, server.errors)
        globals_only = list(observation._PANE_SUBSCRIPTIONS)
        exact_pane_status = globals_only + [{
            "type": "pane.agent_status_changed", "pane_id": "w1:p9"}]
        self.assertEqual(server.subscription_requests, [globals_only, exact_pane_status])
        self.assertNotIn("pane.agent_status_changed", {
            item["type"] for item in server.subscription_requests[0]})
        self.assertEqual(server.subscription_requests[1][-1]["pane_id"], "w1:p9")

    def test_exact_pane_status_event_invalidates_and_refreshes_the_snapshot(self):
        server, transport = self.start(panes=[{
            "pane_id": "w1:p9", "agent": "claude", "agent_status": "idle"}])
        first = transport.observe()
        self.assertIsNotNone(first, server.errors)
        self.assertEqual(first.panes[0]["agent_status"], "idle")
        self.assertEqual(server.subscription_requests[-1][-1], {
            "type": "pane.agent_status_changed", "pane_id": "w1:p9"})

        server.panes = [{
            "pane_id": "w1:p9", "agent": "claude", "agent_status": "working"}]
        server.publish({"event": "pane.agent_status_changed", "data": {
            "type": "pane_agent_status_changed", "pane_id": "w1:p9",
            "agent_status": "working"}})
        current = transport.observe()
        self.assertIsNotNone(current, server.errors)
        self.assertEqual(current.panes[0]["agent_status"], "working")
        self.assertGreater(current.generation, first.generation)

    def test_stale_status_overlapping_newer_snapshot_cannot_regress_to_working(self):
        event = {"event": "pane.updated", "data": {
            "type": "pane_updated", "pane_id": "w1:p9",
            "pane": {"pane_id": "w1:p9", "agent": "codex",
                     "agent_status": "working"}}}
        server, transport = self.start(
            panes=[{"pane_id": "w1:p9", "agent": "codex", "agent_status": "idle"}],
            interleave=event)
        view = transport.observe()
        self.assertIsNotNone(view, server.errors)
        self.assertEqual(view.endpoint, "isolated")
        self.assertEqual(view.panes[0]["agent_status"], "idle")
        self.assertEqual(view.terminal, {})
        self.assertEqual(server.methods[:2], ["events.subscribe", "session.snapshot"])
        self.assertEqual(server.methods.count("session.snapshot"), 2)

    def test_delayed_close_cannot_hide_a_reused_pane_occupant(self):
        event = {"event": "pane.closed", "data": {"type": "pane_closed",
                                                       "pane_id": "w1:p9"}}
        server, transport = self.start(
            panes=[{"pane_id": "w1:p9", "agent": "codex",
                    "agent_session": {"value": "sid-new"}}],
            interleave=event)
        view = transport.observe()
        self.assertIsNotNone(view, server.errors)
        self.assertEqual(view.panes[0]["agent_session"]["value"], "sid-new")
        self.assertEqual(view.terminal, {})
        self.assertEqual(server.methods.count("session.snapshot"), 2)

    def test_ordinary_event_observation_takes_fresh_snapshot_and_invalidates_fleet_generation(self):
        server, transport = self.start(
            panes=[{"pane_id": "w1:p9", "agent": "codex", "agent_status": "working"}])
        first = transport.observe()
        self.assertIsNotNone(first, server.errors)
        server.panes = [{"pane_id": "w1:p9", "agent": "codex", "agent_status": "idle"}]
        server.publish({"event": "pane.updated", "data": {
            "type": "pane_updated", "pane_id": "w1:p9",
            "pane": {"pane_id": "w1:p9", "agent": "codex",
                     "agent_status": "idle"}}})
        current = transport.observe()
        self.assertIsNotNone(current, server.errors)
        self.assertEqual(current.panes[0]["agent_status"], "idle")
        self.assertGreater(current.generation, first.generation)

    def test_explicit_resnapshot_discards_overlapped_candidate(self):
        server, transport = self.start(
            panes=[{"pane_id": "w1:p9", "agent": "codex", "agent_status": "working"}])
        first = transport.observe()
        self.assertIsNotNone(first, server.errors)
        server.panes = [{"pane_id": "w1:p9", "agent": "codex", "agent_status": "idle"}]
        server.interleave = {"event": "pane.updated", "data": {
            "type": "pane_updated", "pane_id": "w1:p9",
            "pane": {"pane_id": "w1:p9", "agent": "codex",
                     "agent_status": "working"}}}
        current = transport.resnapshot()
        self.assertIsNotNone(current, server.errors)
        self.assertEqual(current.panes[0]["agent_status"], "idle")
        self.assertGreater(current.generation, first.generation)
        self.assertEqual(server.methods.count("session.snapshot"), 4)

    def test_event_wait_is_only_a_wake_and_fresh_snapshot_proves_absence(self):
        server, transport = self.start()
        self.assertIsNotNone(transport.observe(), server.errors)
        server.panes = []
        server.publish({"event": "pane.closed", "data": {
            "type": "pane_closed", "pane_id": "w1:p9"}})
        event = transport.wait_for_event("w1:p9", 0.2)
        self.assertIsNotNone(event)
        self.assertFalse(transport._state.complete)
        self.assertEqual(transport._state.terminal, {})
        current = transport.resnapshot()
        self.assertIsNotNone(current, server.errors)
        self.assertEqual(current.panes, ())
        self.assertTrue(current.complete)

    def test_sustained_snapshot_overlap_is_bounded_unknown(self):
        event = {"event": "pane.updated", "data": {"type": "pane_updated",
            "pane_id": "w1:p9", "pane": {"pane_id": "w1:p9", "agent": "codex"}}}
        server, transport = self.start(interleave=event, repeat_interleave=True)
        view = transport.observe()
        self.assertIsNone(view)
        self.assertFalse(transport._state.complete)
        self.assertLessEqual(server.methods.count("session.snapshot"), 4)
        self.assertEqual(server.methods.count("session.snapshot"), 4)

    def test_continuous_event_stream_returns_unknown_then_recovers_when_quiet(self):
        server, transport = self.start()
        original_recv = _MemorySocket.recv
        original_readable = _MemorySocket.readable
        event = (json.dumps({"event": "pane.updated", "data": {
            "pane_id": "w1:p9", "pane": {"pane_id": "w1:p9",
                                             "agent": "codex",
                                             "agent_status": "working"}}}) + "\n").encode()
        reads = 0

        def flooding_recv(sock, size):
            nonlocal reads
            if sock in server.subscribers:
                reads += 1
                if reads > 10000:
                    raise AssertionError("continuous events never yielded the observer")
                if not sock.buffer:
                    sock.feed(event)
            return original_recv(sock, size)

        with mock.patch.object(_MemorySocket, "recv", flooding_recv), \
             mock.patch.object(_MemorySocket, "readable",
                               lambda sock: sock in server.subscribers or original_readable(sock)):
            self.assertIsNone(transport.observe())
        self.assertGreater(reads, 1)
        self.assertFalse(transport._state.complete)
        self.assertEqual(transport._state.terminal, {})
        recovered = transport.observe()
        self.assertIsNotNone(recovered, server.errors)
        self.assertEqual(recovered.panes[0]["pane_id"], "w1:p9")

    def test_continuous_partial_frame_cannot_hold_the_drain_past_its_deadline(self):
        server, transport = self.start()
        self.assertIsNotNone(transport.observe(), server.errors)
        original_readable = _MemorySocket.readable
        ticks = iter(value / 100 for value in range(1000))
        reads = 0

        def drip_recv(_sock, _size):
            nonlocal reads
            reads += 1
            if reads > 1000:
                raise AssertionError("partial frame never yielded the observer")
            return b"{"

        with mock.patch.object(_MemorySocket, "recv", drip_recv), \
             mock.patch.object(_MemorySocket, "readable",
                               lambda sock: sock in server.subscribers or original_readable(sock)), \
             mock.patch.object(observation.time, "monotonic", side_effect=lambda: next(ticks)):
            healthy, _saw_event = transport._drain_events()
        self.assertFalse(healthy)
        self.assertGreater(reads, 0)
        self.assertFalse(transport._state.complete)
        self.assertEqual(transport._state.terminal, {})

    def test_events_lost_error_invalidates_then_one_snapshot_resynchronizes(self):
        server, transport = self.start()
        first = transport.observe()
        self.assertIsNotNone(first, server.errors)
        self.assertEqual([pane["pane_id"] for pane in first.panes], ["w1:p9"])
        server.publish({"id": "", "error": {"code": "events_lost",
                                                "message": "resubscribe and snapshot"}})
        second = transport.observe()
        self.assertIsNotNone(second)
        self.assertTrue(second.complete)
        self.assertEqual(second.panes[0]["pane_id"], "w1:p9")
        self.assertEqual(server.methods.count("events.subscribe"), 3)
        self.assertEqual(server.methods.count("session.snapshot"), 3)

    def test_malformed_event_invalidates_and_reconnects_without_confirming_absence(self):
        server, transport = self.start()
        self.assertIsNotNone(transport.observe(), server.errors)
        server.publish(b"not-json\n")
        view = transport.observe()
        self.assertIsNotNone(view)
        self.assertTrue(view.complete)
        self.assertEqual(view.panes[0]["pane_id"], "w1:p9")
        self.assertEqual(server.methods.count("events.subscribe"), 3)

    def test_disconnect_invalidates_and_reconnects_to_a_complete_snapshot(self):
        server, transport = self.start()
        self.assertIsNotNone(transport.observe(), server.errors)
        server.disconnect_subscribers()
        view = transport.observe()
        self.assertIsNotNone(view)
        self.assertTrue(view.complete)
        self.assertEqual(view.panes[0]["pane_id"], "w1:p9")
        self.assertEqual(server.methods.count("events.subscribe"), 3)
        self.assertEqual(server.methods.count("session.snapshot"), 3)

    def test_disconnect_during_resnapshot_recovers_on_the_next_observation(self):
        server, transport = self.start()
        self.assertIsNotNone(transport.observe(), server.errors)
        server.disconnect_subscribers()
        self.assertIsNone(transport.resnapshot())
        self.assertFalse(transport._state.complete)
        recovered = transport.observe()
        self.assertIsNotNone(recovered, server.errors)
        self.assertEqual(recovered.panes[0]["pane_id"], "w1:p9")
        self.assertEqual(server.methods.count("events.subscribe"), 3)

    def test_events_lost_while_waiting_resubscribes_before_recovering(self):
        server, transport = self.start()
        self.assertIsNotNone(transport.observe(), server.errors)
        server.publish({"id": "", "error": {"code": "events_lost",
                                               "message": "resubscribe and snapshot"}})
        self.assertIsNone(transport.wait_for_event("w1:p9", 0.2))
        self.assertFalse(transport._state.complete)
        recovered = transport.observe()
        self.assertIsNotNone(recovered, server.errors)
        self.assertEqual(recovered.panes[0]["pane_id"], "w1:p9")
        self.assertEqual(server.methods.count("events.subscribe"), 3)
        self.assertEqual(server.methods.count("session.snapshot"), 3)

    def test_protocol_mismatch_is_unknown_and_never_publishes_partial_snapshot(self):
        _server, transport = self.start(protocol=21)
        self.assertIsNone(transport.observe())
        self.assertFalse(transport._state.complete)

    def test_missing_socket_is_a_bounded_unknown_observation(self):
        transport = observation.ObservationTransport("isolated", None, timeout=0.05)
        started = time.monotonic()
        self.assertIsNone(transport.observe())
        self.assertLess(time.monotonic() - started, 0.2)

class NativeStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pid = os.getpid()
        self.start = observation._process_start_ticks(self.pid)
        self.row = {"endpoint": "named", "pane": "w1:p9", "harness": "opencode",
                    "session_id": "sid", "pid": self.pid, "pid_start": self.start,
                    "status": "idle", "observed_at": 100.0}

    def test_native_status_is_exact_bounded_and_owner_only(self):
        with mock.patch.object(observation.time, "time", return_value=100.0):
            self.assertTrue(observation.record_native_status(self.row))
        row = observation.read_native_status("named", "w1:p9", "opencode", "sid",
                                             self.pid, self.start, now=105.0)
        self.assertEqual(row["status"], "idle")
        self.assertEqual(Path(observation._native_status_path(
            "named", "w1:p9", "opencode", "sid")).stat().st_mode & 0o777, 0o600)
        self.assertIsNone(observation.read_native_status(
            "default", "w1:p9", "opencode", "sid", self.pid, self.start, now=105.0))
        self.assertIsNone(observation.read_native_status(
            "named", "w1:p9", "opencode", "sid", self.pid, self.start,
            max_age=1.0, now=105.0))

    def test_stale_process_birth_never_writes_or_reads_native_state(self):
        stale = dict(self.row, pid_start="1")
        self.assertFalse(observation.record_native_status(stale))
        with mock.patch.object(observation, "_process_start_ticks", return_value="2"):
            self.assertIsNone(observation.read_native_status(
                "named", "w1:p9", "opencode", "sid", self.pid, self.start, now=100.0))

if __name__ == "__main__":
    unittest.main()
