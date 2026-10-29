#!/usr/bin/env python3
"""Hermetic registration and D-42 regression for the OpenCode V2 plugin API."""
import os
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[1]
# A test run inside a registered worker must not lend the fixture its markers.
WORKER_MARKERS = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                  "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "AGENT_DISPATCH_REGISTERED_WORKER")
BASE_ENV = {key: value for key, value in os.environ.items() if key not in WORKER_MARKERS}
sys.path.insert(0, str(ROOT / "tools"))
import fixture_processes


def fixture_tempdir(stack):
    raw = stack.enter_context(tempfile.TemporaryDirectory())
    stack.callback(fixture_processes.reap, raw)
    return raw


class OpenCodeV2PluginTest(unittest.TestCase):
    def test_synchronous_completion_transport_preserves_existing_output_capacity(self):
        node = shutil.which("node")
        self.assertIsNotNone(node)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            helper = folder / "reply.py"
            helper.write_text("import json\nprint(json.dumps({'text': 'x'*1100000, 'records': []}))\n")
            script = r'''
import fs from 'node:fs'
import {spawnSync} from 'node:child_process'
import assert from 'node:assert/strict'
const source = fs.readFileSync(process.env.PLUGIN_PATH, 'utf8')
const a = source.indexOf('function carrierCommand(')
const b = source.indexOf('function createCompletionCarrier(ctx)', a)
assert(a >= 0 && b > a)
const invoke = new Function('resolveRuntimeTarget', 'sweepRel', 'root', 'spawnSync', 'CARRIER_KIND',
  source.slice(a,b) + '\nreturn carrierCommand("settle", "B", {}, () => {}, true)')
const result = invoke(() => ({root: process.cwd(), helper: process.env.REPLY_HELPER}),
  'utilities/dispatch_session_sweep.py', process.cwd(), spawnSync, 'opencode-turn')
assert.equal(result?.text?.length, 1100000)
console.log(JSON.stringify({bytes: result.text.length}))
'''
            result = subprocess.run([node, "--input-type=module", "-e", script], cwd=folder,
                env=dict(BASE_ENV, PLUGIN_PATH=str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                         REPLY_HELPER=str(helper)), capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def completion_case(self, **case):
        """Exact carrier/helpers/events; real private notice, lineage, sweep and store.

        Only pane admission, native SDK and the interval clock are deterministic.
        Every case gets separate state, including the Python helper processes.
        """
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_pending_delivery as pending
        import dispatch_session_sweep as sweep
        import peer_obligations
        import session_notice
        import session_tidy as tidy
        for module in (pending, sweep, peer_obligations, session_notice, tidy):
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(ROOT))
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for behavioral carrier checks")
        with ExitStack() as stack:
            root = Path(fixture_tempdir(stack))
            env = dict(BASE_ENV, AGENT_HOME=str(ROOT), HERDR_PANE_ID="w1:pCarrier",
                       OPENCODE_SESSION_ID="B", AGENT_DISPATCH_CALLER_HARNESS="opencode",
                       HARNESS_STATE_ROOT=str(root / "harness"), AGENT_PEER_LEDGER_ROOT=str(root / "peer"),
                       AGENT_DISPATCH_JOBS=str(root / "jobs.log"),
                       HERDR_SOCKET_PATH=str(root / "native.sock"),
                       CODEX_HOME=str(root / "codex"), CLAUDE_CONFIG_DIR=str(root / "claude"))
            for name in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                         "XDG_CACHE_HOME", "MEM_STORE", "TMPDIR"):
                target = root / name.lower()
                target.mkdir()
                env[name] = str(target)
            (root / "jobs.log").touch()
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch("pane_ownership.verified_pane",
                                          side_effect=lambda pane, *a, **kw: pane))
            stack.enter_context(mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=False))
            seat = tidy.resolve_seat("opencode", str(ROOT), os.environ, "B")
            for row in (dict(event="start", harness="opencode", sid="A", ts=1),
                        dict(event="succession", source="tidy-clear", harness="opencode",
                             sid="B", **{"from": "A"}, ts=2),
                        dict(event="start", harness="opencode", sid="B", ts=3)):
                tidy._append_ledger_entry(seat, row)
            storage = case.get("storage", "A")
            notices = [session_notice.notify("opencode", storage, key=f"carrier-{index}",
                subject="carrier fixture", text=f"private notice {index}", jobs=env["AGENT_DISPATCH_JOBS"])
                for index in range(case.get("count", 1))]
            identities = [pending.received_ack_identity(row, storage, row["delivery_id"]) for row in notices]
            if case.get("historical"):
                row = pending.claim(root, storage, notices[0]["delivery_id"], claim_owner="old-carrier",
                                    lease_seconds=30, require_generation_proof=False)
                row = pending.mark_sent_ambiguous(root, storage, row["delivery_id"], claim_owner="old-carrier")
                case["history"] = sweep.delivery_context([(root, [{**row, sweep.STORAGE_KEY: storage}])])
                pending.reclaim(root, storage, row["delivery_id"], now_ns=row["claim_deadline_ns"] + 1)
            bridge = root / "bridge.py"
            bridge.write_text(r'''
import os, sys
from pathlib import Path
from unittest import mock
root = Path(os.environ['AGENT_HOME']).resolve()
sys.path[:0] = [str(root/'utilities'), str(root/'tools')]
import dispatch_session_sweep as sweep
import dispatch_pending_delivery as pending
assert Path(sweep.__file__).resolve() == root/'utilities/dispatch_session_sweep.py'
sweep._state_roots = lambda: [Path(os.environ['AGENT_DISPATCH_JOBS']).parent]
if sys.argv[1] == 'move-lineage':
    import json
    import dispatch_seat_handover as handover
    import session_tidy as tidy
    folder = tidy.state_root() / 'sessions'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'reviewer-late-successor.jsonl').write_text(json.dumps({
        'event': 'succession', 'source': 'tidy-clear', 'harness': 'opencode',
        'sid': 'C', 'from': 'B', 'ts': 4}) + '\n')
    handed = json.load(sys.stdin)['records']
    targets = [handover.record_recipient(pending.read(Path(r['root']), r['storage_key'], r['delivery_id']),
        r['storage_key'], harness='opencode') for r in handed]
    assert targets and set(targets) == {'C'}, targets
    print(json.dumps({'actual_current_recipients': targets}))
elif sys.argv[1] == 'replace-claim':
    import json
    handed = json.load(sys.stdin)['records'][0]
    base, sid, did = Path(handed['root']), handed['storage_key'], handed['delivery_id']
    row = pending.read(base, sid, did)
    pending.reclaim(base, sid, did, now_ns=row['claim_deadline_ns']+1)
    pending.claim(base, sid, did, claim_owner='replacement-carrier', lease_seconds=30,
                  require_generation_proof=False)
    print('{}')
else:
    with mock.patch('pane_ownership.verified_pane', side_effect=lambda pane,*a,**kw:pane):
        sys.exit(sweep.main([sys.argv[1], '--recipient-kind', 'opencode-turn', '--session', sys.argv[2]]))
''')
            script = root / "carrier.mjs"
            script.write_text(r'''
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import {createHash} from 'node:crypto'
import {spawnSync} from 'node:child_process'
const cfg = JSON.parse(process.env.CARRIER_CASE)
const source = fs.readFileSync(process.env.PLUGIN_PATH, 'utf8')
const extract = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a)
  assert(a >= 0 && b > a, `missing actual source: ${start}`)
  return source.slice(a, b)
}
let tick, carrier, api, handed, writes = 0, histories = 0, roots = 0, statuses = 0
let changed = false
const actions = [], logs = []
const bridge = (action, data) => {
  actions.push(action)
  const result = spawnSync('python3', [process.env.CARRIER_BRIDGE, action, 'B'], {
    input: data ? JSON.stringify(data) : '', encoding: 'utf8',
  })
  assert.equal(result.status, 0, result.stderr)
  return JSON.parse(result.stdout)
}
const boundary = name => {
  if (name !== cfg.boundary || changed) return
  changed = true
  if (cfg.change === 'origin') api.invalidate()
  else if (cfg.change === 'client') ctx.client = {...ctx.client, session: {...session}}
  else if (cfg.change === 'dispose') carrier.dispose(true)
  else if (cfg.change === 'claim') bridge('replace-claim', handed)
  else if (cfg.change === 'lineage') logs.push(bridge('move-lineage', handed))
  else if (cfg.change === 'peer-working') api.peerStatus('working')
  else carrier.status('B', cfg.change || 'blocked')
}
const nativeResponse = data => ({data, response: {ok: true, status: 200}})
const session = {
  get: async () => {
    roots++
    boundary(`root:${roots}`)
    let info = {id: 'B', projectID: 'fixture', directory: process.cwd(), slug: 'fixture',
      title: 'fixture', version: '1', time: {created: 1, updated: 1}}
    if (cfg.native === 'wrong-root') info.id = 'C'
    if (cfg.native === 'child') info.parentID = 'A'
    if (cfg.native === 'malformed-root') delete info.time
    return nativeResponse(info)
  },
  status: async () => {
    statuses++
    boundary(`status:${statuses}`)
    if (cfg.native === 'unavailable') throw Error('fixture native unavailable')
    if (cfg.native === 'invalid-response') return {data: {}, response: {ok: false, status: 503}}
    const kind = cfg.native?.startsWith('final-')
      ? (statuses > 1 ? cfg.native.slice(6) : 'idle') : cfg.native
    const values = {empty: {}, missing: {other: {type: 'idle'}}, invalid: [],
      unknown: {B: {type: 'unknown'}}, malformed: {B: {type: 'retry'}},
      'invalid-other': {other: {type: 'unknown'}}, busy: {B: {type: 'busy'}},
      retry: {B: {type: 'retry', attempt: 1, message: 'retry', next: 10}}}
    return nativeResponse(values[kind] ?? {B: {type: 'idle'}})
  },
  messages: async () => {
    histories++
    boundary(`history:${histories}`)
    if (cfg.receipt === 'unknown' || (cfg.receipt === 'final-unknown' && histories >= 2))
      return {error: {name: 'unavailable'}}
    let text = cfg.history || ''
    if (handed && ['before', 'partial'].includes(cfg.receipt) && histories >= 2) text = handed.text
    if (handed && cfg.receipt === 'after' && writes) text = handed.text
    if (text && cfg.receipt === 'partial') text = text.slice(0, text.lastIndexOf('HEARTING_RECEIPT_V1'))
    return nativeResponse(text ? [{info: {sessionID: cfg.wrongReceiver ? 'C' : 'B', role: 'user'},
      parts: [{type: 'text', text}]}] : [])
  },
  promptAsync: async request => {
    writes++
    assert.equal(request.path.id, 'B')
    assert(request.body.parts[0].text.includes('HEARTING_RECEIPT_V1'))
    if (cfg.acceptance === 'ambiguous') throw Error('fixture lost response')
    return {response: {ok: true, status: 204}}
  },
}
if (cfg.native === 'missing-api') delete session.status
const ctx = {directory: process.cwd(), client: {session, app: {log: async row => logs.push(row.body.extra)}}}
const command = (action, sid, data, observe, synchronous = false) => {
  const result = bridge(action, data)
  if (action === 'deliver') handed = result
  // Queue the event after the helper response, before the caller resumes.
  if (cfg.afterAwait && action === cfg.boundary) queueMicrotask(() => boundary(action))
  else boundary(action)
  return synchronous ? result : Promise.resolve(result)
}
const setup = new Function('ctx', 'carrierCommand', 'path', 'readdirSync', 'readFileSync',
  'realpathSync', 'createHash', 'setInterval', 'baseDir', 'isWorkerSession', 'pendingPeerDelivery',
  `let paneNativeOrigin = {sid: 'B', directory: ctx.directory, state: 'pending'};
   const paneContexts = new WeakMap([[ctx, {active: true, ownsOrigin: true}]]);
   let paneOwningContext = ctx;
   const pendingPeerStatus = new Map();
   ${extract('function pendingPeerRoot(ctx, sid)', 'function nativeMessageTime')}
   ${extract('function sdkResponseData(result)', 'function runCompactHook')}
   ${extract('async function compactMessages(ctx, sid)', '// A declared sub-session')}
   ${extract('const CARRIER_ENV =', '// One bounded sweep step')}
   ${extract('function createCompletionCarrier(ctx)', '// Session card / tidy notice')}
   const completionCarrier = createCompletionCarrier(ctx);
   const observeCompactRuntimeStatus = () => {};
   const event = event => {
     ${extract('    if (event && ["question.asked", "permission.asked"]', '    if (event && event.type === "session.idle")')}
   };
   return {carrier: completionCarrier, event, invalidate: () => {paneNativeOrigin.state = 'invalidated'},
     peerStatus: type => pendingPeerStatus.set('B', type)};`)
// The event's external telemetry is silent under its existing worker check;
// delivery itself runs as a normal carrier in the same exact function.
let eventMode = false
api = setup(ctx, command, path, fs.readdirSync, fs.readFileSync, fs.realpathSync, createHash,
  fn => {tick = fn; return {unref(){}}}, x => x.directory, () => eventMode, async () => {})
carrier = api.carrier
carrier.env('B')
carrier.status('B', cfg.local || 'idle')
const drain = async () => {for (let i=0; i<100; i++) await new Promise(resolve => setImmediate(resolve))}
try {
  if (cfg.event) {
    eventMode = true; api.event({type: cfg.event+'.asked', properties: {sessionID: 'B'}}); eventMode = false
    tick(); await drain(); assert.equal(writes, 0)
    eventMode = true; api.event({type: cfg.event+'.replied', properties: {sessionID: 'B'}}); eventMode = false
    tick(); await drain(); assert.equal(writes, 0)
    api.peerStatus('idle'); carrier.status('B','idle')
  }
  tick(); await drain()
  assert.equal(writes, cfg.writes, JSON.stringify({cfg, writes, actions, logs}))
  if (cfg.boundary) assert(changed, `boundary not reached: ${cfg.boundary}`)
  console.log(JSON.stringify({writes, actions, logs, changed, roots, statuses, histories}))
} finally {carrier.dispose(true)}
''')
            result = subprocess.run([node, str(script)], cwd=ROOT, env=dict(env,
                CARRIER_CASE=json.dumps(case), CARRIER_BRIDGE=str(bridge),
                PLUGIN_PATH=str(ROOT / "adapters/opencode/plugins/hearting-guards.js")),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            observed = json.loads(result.stdout)
            final = [pending.read(root, storage, row["delivery_id"]) for row in notices]
            self.assertEqual([pending.received_ack_identity(row, storage, row["delivery_id"])
                              for row in final], identities)
            self.assertTrue(all(row["state"] == case.get("state", "pending") for row in final), final)
            self.assertTrue(all(pending.read(root, "B" if storage == "A" else "A", row["delivery_id"])
                                is None for row in notices))
            return observed, final

    def test_completion_carrier_rechecks_lineage_after_native_and_helper_awaits(self):
        for storage in ("A", "B"):
            for boundary in ("root:2", "status:2", "history:2", "settle"):
                for count in (1, 2):
                    with self.subTest(storage=storage, boundary=boundary, count=count):
                        self.completion_case(storage=storage, boundary=boundary,
                                             change="lineage", writes=0, count=count,
                                             afterAwait=boundary == "settle")

    def test_completion_carrier_withholds_own_and_inherited_non_idle_turns(self):
        for storage in ("A", "B"):
            for status in ("blocked", "working", "busy", "retry", "idle"):
                with self.subTest(storage=storage, status=status):
                    self.completion_case(storage=storage, local=status, writes=int(status == "idle"))

    def test_completion_carrier_uses_existing_exact_native_root_and_idle_contract(self):
        for native in ("empty", "missing", "busy", "retry", "invalid", "unknown", "malformed",
                       "invalid-other", "unavailable", "invalid-response", "wrong-root", "child",
                       "malformed-root", "missing-api", "final-busy", "final-unknown", "final-invalid"):
            with self.subTest(native=native):
                self.completion_case(native=native, writes=int(native in {"empty", "missing"}))

    def test_completion_carrier_rechecks_every_prewrite_await_and_releases_own_claim(self):
        for boundary in ("roots", "recipients", "root:1", "status:1", "history:1", "deliver",
                         "root:2", "status:2", "history:2", "settle"):
            for change in ("blocked", "working", "origin", "client"):
                with self.subTest(boundary=boundary, change=change):
                    self.completion_case(boundary=boundary, change=change, writes=0)
        for change in ("blocked", "peer-working", "dispose", "client"):
            with self.subTest(afterAwait=change):
                self.completion_case(boundary="settle", afterAwait=True, change=change, writes=0)

    def test_completion_carrier_question_and_permission_resume_only_after_idle(self):
        for event in ("question", "permission"):
            with self.subTest(event=event):
                self.completion_case(event=event, writes=1)

    def test_completion_carrier_preserves_claim_change_and_exact_receipts(self):
        observed, rows = self.completion_case(boundary="status:2", change="claim", writes=0, state="claimed")
        self.assertEqual(rows[0]["claim_owner"], "replacement-carrier")
        self.assertNotIn("release", observed["actions"])
        for case in (dict(receipt="before", boundary="root:2", change="blocked", writes=0, state="acked"),
                     dict(receipt="after", writes=1, state="acked"),
                     dict(receipt="after", wrongReceiver=True, writes=1),
                     dict(historical=True, writes=0, state="acked"),
                     dict(acceptance="ambiguous", writes=1),
                     dict(receipt="unknown", writes=0),
                     dict(receipt="final-unknown", writes=0),
                     dict(count=2, receipt="partial", writes=1),
                     dict(count=2, receipt="before", writes=0, state="acked")):
            with self.subTest(case=case):
                self.completion_case(**case)

    def test_completion_carrier_queries_shared_lineage_and_bounds_negative_cache(self):
        source = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text(encoding="utf-8")
        self.assertIn('command("recipients", sid)', source)
        self.assertIn('const recipientCache = new Map()', source)
        self.assertIn("cached.generation !== current.generation", source)
        self.assertIn("CARRIER_RECIPIENT_CACHE_MS", source)
        self.assertIn("CARRIER_NEGATIVE_CACHE_MS", source)
        self.assertIn("cached.negativeUntil = now + CARRIER_NEGATIVE_CACHE_MS", source)
        self.assertIn("for (const digest of cached.digests)", source)

    def test_pending_peer_delivery_releases_only_prewrite_reservations(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the native plugin harness")
        spec = importlib.util.spec_from_file_location("opencode_delivery_peer", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        real_python = shutil.which("python3")
        plugin = ROOT / "adapters/opencode/plugins/hearting-guards.js"
        script = r'''
import assert from "node:assert/strict"
import fs from "node:fs"
import childProcess from "node:child_process"
import { pathToFileURL } from "node:url"
const ownStat = fs.readFileSync("/proc/self/stat", "utf8")
const ownSplit = ownStat.lastIndexOf(") ")
const ownStart = ownStat.slice(ownSplit + 2).trim().split(/\s+/)[19]
const selectionDir = `${process.env.XDG_STATE_HOME}/hearting/tui-identity`
fs.mkdirSync(selectionDir, { recursive: true })
fs.writeFileSync(`${selectionDir}/${process.pid}-${ownStart}.json`, JSON.stringify({
  schema: "hearting-tui-selection-v1", sessionID: process.env.TARGET_SID,
  pid: process.pid, start: ownStart,
}))
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.PLUGIN_PATH))
const sid = process.env.TARGET_SID
const marker = process.env.RESERVE_MARKER
const otherDirectory = process.env.ALT_DIRECTORY
let writes = 0
const traces = []
let directoryReads = 0
let messageReads = 0
const nativeResponse = (data) => ({ data, response: { ok: true, status: 200 } })
const ctx = { location: { directory: process.cwd() }, client: { app: { log: async (row) => traces.push(row.body.extra) }, session: {
  get: async () => nativeResponse({ id: sid, projectID: "fixture", directory: process.cwd(),
    slug: "fixture", title: "fixture", version: "1", time: { created: 1, updated: 1 } }),
  status: async () => nativeResponse({ [sid]: { type: "idle" } }),
  messages: async () => {
    messageReads++
    if (["dispatch-race", "quoted-dispatch-marker"].includes(process.env.CASE) && messageReads >= 2) {
      return nativeResponse([{ info: { sessionID: sid, role: "user" },
        parts: [{ type: "text", text: process.env.CASE === "quoted-dispatch-marker"
          ? `Quoted text: ${process.env.RECEIPT_MARKER}` : process.env.EXPECTED_TEXT }] }])
    }
    return nativeResponse([])
  },
  prompt: async () => {
    writes++
    if (process.env.CASE === "ambiguous") throw Object.assign(new Error("lost"), {
      response: { ok: false, status: 500 },
    })
    return nativeResponse({ info: { id: "user_saved", sessionID: sid, role: "user" },
      parts: [{ type: "text", text: process.env.EXPECTED_TEXT }] })
  },
  promptAsync: async () => ({ response: { ok: true, status: 204 } }),
} } }
Object.defineProperty(ctx, "directory", { get() {
  directoryReads++
  if (fs.existsSync(marker)) { fs.unlinkSync(marker); return otherDirectory }
  return process.cwd()
} })
const hooks = await AgentHarnessGuards(ctx)
const readTransfer = () => {
  const result = childProcess.spawnSync("python3", [process.env.PEER_HELPER, "pending",
    "--to-harness", "opencode", "--to-session-id", sid], { encoding: "utf8" })
  assert.equal(result.status, 0, result.stderr)
  return JSON.parse(result.stdout).find((candidate) => candidate.ref === process.env.TRANSFER_REF)
}
try {
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: sid } } })
  if (process.env.CASE === "root-change") {
    assert.equal(writes, 0)
    const released = readTransfer()
    assert.equal(released.state, "pending")
    assert.equal(released.rpc_claim, null)
    await hooks.event({ event: { type: "session.idle", properties: { sessionID: sid } } })
    assert.equal(writes, 1, `the next callback did not deliver the released reservation: ${JSON.stringify({ traces, row: readTransfer(), directoryReads, reserves: fs.existsSync(process.env.RESERVE_LOG) ? fs.readFileSync(process.env.RESERVE_LOG, "utf8") : "" })}`)
  } else if (process.env.CASE === "begin-refused") {
    assert.equal(writes, 0)
  } else if (process.env.CASE === "dispatch-race") {
    assert.equal(writes, 0, "peer carrier sent after final exact native receipt recheck")
    const released = readTransfer()
    assert.equal(released.state, "pending")
    assert.equal(released.rpc_claim, null)
  } else if (process.env.CASE === "quoted-dispatch-marker") {
    assert.equal(writes, 1, "a quoted marker was treated as a native dispatch receipt")
  } else {
    assert.equal(writes, 1, JSON.stringify(traces))
    const stat = fs.readFileSync("/proc/self/stat", "utf8")
    const split = stat.lastIndexOf(") ")
    const start = stat.slice(split + 2).trim().split(/\s+/)[19]
    const row = readTransfer()
    assert.equal(row.state, "unverified")
    const released = childProcess.spawnSync("python3", [process.env.PEER_HELPER, "pending",
      "--to-harness", "opencode", "--to-session-id", sid, "--release-reservation", row.ref,
      "--claim-token", row.rpc_claim.token, "--claim-pid", String(process.pid), "--claim-start", start],
      { encoding: "utf8" })
    assert.equal(released.status, 1, "release must be a no-op after begin-write")
    await hooks.event({ event: { type: "session.idle", properties: { sessionID: sid } } })
    assert.equal(writes, 1, "an ambiguous write was submitted again")
  }
} finally { hooks.dispose?.({ silent: true }) }
'''
        for case in ("root-change", "begin-refused", "ambiguous", "dispatch-race",
                     "quoted-dispatch-marker"):
            with self.subTest(case=case), ExitStack() as stack:
                tmp = fixture_tempdir(stack)
                root = Path(tmp)
                peer_root = root / "peer"
                fake_bin = root / "bin"
                fake_bin.mkdir()
                native_node = fake_bin / "opencode"
                native_node.symlink_to(node)
                marker = root / "reserved"
                once = root / "root-change-used"
                sid = "ses_deliveryfixture"
                recipient = {"harness": "opencode", "session_id": sid}
                dispatch_root = root / "harness" / "dispatch"
                dispatch_root.mkdir(parents=True, exist_ok=True)
                (dispatch_root / "jobs.log").touch()
                with mock.patch.dict(os.environ, {
                        "HOME": str(root / "home"), "XDG_STATE_HOME": str(root / "state"),
                        "AGENT_PEER_LEDGER_ROOT": str(peer_root),
                        "HARNESS_STATE_ROOT": str(root / "harness"),
                        "AGENT_DISPATCH_JOBS": str(root / "harness" / "dispatch" / "jobs.log"),
                        "AGENT_RUNTIME_ROOT": str(root / "runtime")}):
                    receipt_marker = ""
                    if case in {"dispatch-race", "quoted-dispatch-marker"}:
                        import dispatch_pending_delivery as pending
                        import dispatch_recipient_receipt as recipient_receipt
                        jobs = dispatch_root / "jobs.log"
                        jobs.parent.mkdir(parents=True, exist_ok=True)
                        jobs.touch()
                        body = {"schema_version": 1, "kind": "supervision",
                                "owner_attempt_id": "att-owner", "reason": "waiting"}
                        record = pending.create(
                            dispatch_root, recipient_kind="opencode-turn", recipient_key=sid,
                            delivery_id="delivery-" + "d" * 32, session_generation="g1",
                            session_generation_supported="1", attempt_ids=["att-owner"],
                            parent_attempt_id="att-owner", route_id="route-fixture",
                            route_node="execute", receipt=body,
                            receipt_digest=pending._canonical_receipt_digest(body),
                            row_revisions={"att-owner": "revision-1"})
                        pending.claim(dispatch_root, sid, record["delivery_id"],
                                      claim_owner="sweep-carrier", lease_seconds=30,
                                      require_generation_proof=False)
                        pending.mark_sent_ambiguous(
                            dispatch_root, sid, record["delivery_id"],
                            claim_owner="sweep-carrier")
                        record = pending.read(dispatch_root, sid, record["delivery_id"])
                        receipt_marker = recipient_receipt.receipt_marker(record, sid)
                    transfer_text = ("Hearting delivery context\n" + receipt_marker
                                     if receipt_marker else "one original prompt")
                    duty_id = "registered-batch-fixture"
                    refs = ([duty_id, "att-owner", record["delivery_id"]]
                            if receipt_marker else [])
                    _text, ref = peer.prepare_peer_message(transfer_text, {
                        "harness": "codex", "session_id": "sender"}, recipient,
                        defer=True, refs=refs)
                    if receipt_marker:
                        peer.bind_dispatch_notice(ref, {
                            "duty_id": duty_id,
                            "jobs": str(dispatch_root / "jobs.log"),
                            "attempt_id": "att-owner", "carrier": "opencode-turn",
                            "recipient_sid": sid, "storage_recipient": sid,
                            "delivery_id": record["delivery_id"],
                            "claim_owner": "retained-rewake:" + duty_id,
                        })
                    expected_text = peer._read_pending(ref)["text"]
                    fake_peer = root / "fake-peer-command.py"
                    fake_peer.write_text(r'''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
def value(option, default=None):
    try: return args[args.index(option) + 1]
    except (ValueError, IndexError): return default
ref = (value("--reserve") or value("--release-reservation") or value("--begin-write")
       or value("--queued") or value("--resuming") or os.environ["TRANSFER_REF"])
path = Path(os.environ["AGENT_PEER_LEDGER_ROOT"]) / "peer-messages" / "pending" / (ref + ".json")
row = json.loads(path.read_text())
def save(): path.write_text(json.dumps(row))
if "--reserve" in args:
    row["rpc_claim"] = {"token": "fixture-claim", "pid": int(value("--claim-pid")),
        "start": value("--claim-start"), "receipt": "opencode-prompt-inflight",
        "recipient": {"harness": "opencode", "session_id": value("--to-session-id"), "pane_id": None}}
    save(); print(json.dumps(row)); raise SystemExit(0)
if "--release-reservation" in args:
    if row.get("state") != "pending" or row.get("rpc_claim", {}).get("token") != value("--claim-token"):
        raise SystemExit(1)
    row.update(rpc_claim=None, receipt="opencode-prompt-reservation-released")
    save(); raise SystemExit(0)
if "--begin-write" in args:
    if os.environ.get("CASE") == "begin-refused": raise SystemExit(1)
    row.update(state="unverified", receipt="opencode-prompt-inflight")
    save(); print(json.dumps(row)); raise SystemExit(0)
if "--queued" in args:
    row.update(state="queued", rpc_claim=None, receipt="context-api-accepted",
               actual_message_id=value("--message-id"))
    save(); raise SystemExit(0)
if "--resuming" in args:
    row["resume_attempted"] = True
    save(); raise SystemExit(0)
if "--received" in args:
    row.update(state="received", receipt="exact-native-history")
    save(); raise SystemExit(0)
print(json.dumps([row]))
''', encoding="utf-8")
                    wrapper = fake_bin / "python3"
                    wrapper.write_text(
                        "#!/bin/sh\n"
                        f"REAL_PYTHON={real_python}\n"
                        + (f"case \" $* \" in *\" --reserve \"*) if [ ! -e {once} ]; then touch {once} {marker}; fi;; esac\n"
                           if case == "root-change" else "")
                        + (f"case \" $* \" in *\" --reserve \"*) echo reserve >> {root / 'reserves'};; esac\n"
                           if case == "root-change" else "")
                        + ("for arg in \"$@\"; do [ \"$arg\" = \"--begin-write\" ] && exit 1; done\n"
                           if case == "begin-refused" else "")
                        + "if [ \"$1\" = \"$PEER_HELPER\" ] && [ \"$2\" = \"pending\" ]; then shift; exec \"$REAL_PYTHON\" \"$FAKE_PEER_HELPER\" \"$@\"; fi\n"
                        + "exec \"$REAL_PYTHON\" \"$@\"\n", encoding="utf-8")
                    wrapper.chmod(0o755)
                    alt = root / "other-directory"
                    alt.mkdir()
                    env = {**BASE_ENV, "PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(root / "home"),
                        "XDG_STATE_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_DATA_HOME": str(root / "data"), "XDG_CACHE_HOME": str(root / "cache"),
                        "AGENT_HOME": str(ROOT), "AGENT_PEER_LEDGER_ROOT": str(peer_root),
                        "HARNESS_STATE_ROOT": str(root / "harness"),
                        "AGENT_DISPATCH_JOBS": str(root / "harness" / "dispatch" / "jobs.log"),
                        "AGENT_RUNTIME_ROOT": str(root / "runtime"),
                        "PLUGIN_PATH": str(plugin), "TARGET_SID": sid, "TRANSFER_REF": ref,
                        "EXPECTED_TEXT": expected_text, "RESERVE_MARKER": str(marker),
                        "ALT_DIRECTORY": str(alt), "PEER_HELPER": str(ROOT / "utilities/peer-message.py"),
                        "FAKE_PEER_HELPER": str(fake_peer),
                        "RESERVE_LOG": str(root / "reserves"),
                        "CASE": case, "RECEIPT_MARKER": receipt_marker,
                        "PYTHONDONTWRITEBYTECODE": "1", "OPENCODE_VERSION": "1.19.0"}
                    run = subprocess.run([str(native_node), "--input-type=module", "-e", script],
                        cwd=root, env=env, text=True, capture_output=True, timeout=20)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    row = peer._read_pending(ref)
                    if case == "root-change":
                        self.assertIn(row["state"], {"queued", "received"})
                    elif case == "begin-refused":
                        self.assertEqual(row["state"], "pending")
                        self.assertIsNone(row.get("rpc_claim"))
                    elif case == "dispatch-race":
                        self.assertEqual(row["state"], "pending")
                        self.assertIsNone(row.get("rpc_claim"))
                        self.assertEqual(pending.read(dispatch_root, sid,
                            "delivery-" + "d" * 32)["state"], "acked")
                    elif case == "quoted-dispatch-marker":
                        self.assertEqual(pending.read(dispatch_root, sid,
                            "delivery-" + "d" * 32)["state"], "sent-ambiguous")
                    else:
                        self.assertEqual(row["state"], "unverified")

    def test_peer_prompt_write_boundary_orders_reserve_recheck_begin_and_no_reply(self):
        source = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text()
        start = source.index('const reserveStarted = performance.now()',
                             source.index('async function pendingPeerDelivery(ctx, sid)'))
        end = source.index('pendingPeerTrace(ctx, sid, "noReply-save"', start)
        delivery = source[start:end]
        reserve = delivery.index('["--reserve", row.ref')
        root_recheck = delivery.index('if (!pendingPeerRoot(ctx, sid)) { releaseReservation(); return "native-origin-changed" }')
        receipt_recheck = delivery.index('if (await recheckReceipt(reserved))')
        begin_write = delivery.index('["--begin-write", row.ref')
        sdk_write = delivery.index('client.prompt({ path: { id: sid }')
        self.assertLess(reserve, root_recheck)
        self.assertLess(root_recheck, receipt_recheck)
        self.assertLess(receipt_recheck, begin_write)
        self.assertLess(begin_write, sdk_write)
        self.assertIn('noReply: true', delivery[sdk_write:])

    def test_python_reserve_begin_write_failure_stays_pending_and_ambiguous_write_is_not_retried(self):
        spec = importlib.util.spec_from_file_location("opencode_peer_message", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "HOME": tmp, "XDG_STATE_HOME": tmp + "/state",
                "AGENT_PEER_LEDGER_ROOT": tmp + "/peer", "AGENT_DISPATCH_JOBS": tmp + "/jobs.log",
                "AGENT_RUNTIME_ROOT": tmp + "/runtime"}):
            sender = {"harness": "codex", "session_id": "sender"}
            recipient = {"harness": "opencode", "session_id": "recipient"}
            _text, ref = peer.prepare_peer_message("one original prompt", sender, recipient, defer=True)
            claimant = {"pid": os.getpid(), "start": "123"}
            reserved = peer.reserve_pending_herdr(ref, recipient, claimant=claimant)
            self.assertEqual(peer._read_pending(ref)["state"], "pending")
            # A begin-write transition that fails or times out has not crossed the SDK boundary.
            with mock.patch("dispatch_contract.process_identity_disposition", return_value="unknown"):
                self.assertIsNone(peer.begin_pending_write(reserved, recipient=recipient, claimant=claimant))
            pending = peer._read_pending(ref)
            self.assertEqual(pending["state"], "pending")
            self.assertTrue(pending["text"].startswith("one original prompt"))
            self.assertTrue(peer.release_unsent_herdr_claim(reserved))
            retry = peer.reserve_pending_herdr(ref, recipient, claimant=claimant)
            self.assertIsNotNone(retry)
            with mock.patch("dispatch_contract.process_identity_disposition", return_value="live"):
                writing = peer.begin_pending_write(retry, recipient=recipient, claimant=claimant)
            self.assertEqual(writing["state"], "unverified")
            self.assertIsNone(peer.reserve_pending_herdr(ref, recipient, claimant=claimant))
            self.assertTrue(peer._read_pending(ref)["text"].startswith("one original prompt"))

    def test_v1_reinjects_contract_without_main_lifecycle_or_native_child_leak(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        with ExitStack() as stack:
            tmp = fixture_tempdir(stack)
            script = r'''
import assert from "node:assert/strict"
import { pathToFileURL } from "node:url"
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.PLUGIN_PATH))
const hooks = await AgentHarnessGuards({
  directory: process.env.HOME,
  client: { session: { get: async ({ path }) => ({ data: {
    id: path.id, ...(path.id === "child" ? { parentID: "worker" } : {})
  } }) } }
})
const output = { system: ["native instructions"] }
await hooks["experimental.chat.system.transform"]({ sessionID: "worker" }, output)
assert.deepEqual(output.system, ["native instructions"], "auxiliary system hook received a terminal instruction")
const original = { info: { id: "msg_user", sessionID: "worker", role: "user" },
                   parts: [{ type: "text", text: "summary without terminal fields" }] }
const primary = { messages: [original] }
await hooks["experimental.chat.messages.transform"]({}, primary)
assert.equal(original.parts.length, 1, "the row shared with title generation was mutated")
const contract = primary.messages[0].parts.at(-1).text
assert.match(contract, /verdict: PASS \| FAIL \| BLOCKED/)
assert.match(contract, /runtime_wait: registered-children/)
for (let i = 0; i < 2; i++) {
  const compacted = { messages: [original] }
  await hooks["experimental.chat.messages.transform"]({}, compacted)
  await hooks["experimental.chat.messages.transform"]({}, compacted)
  assert.equal(compacted.messages[0].parts.length, 2)
  assert.equal(compacted.messages[0].parts.at(-1).text, contract)
  const summary = { context: [] }
  await hooks["experimental.session.compacting"]({ sessionID: "worker" }, summary)
  await hooks["experimental.session.compacting"]({ sessionID: "worker" }, summary)
  assert.equal(summary.context.length, 1)
  assert.ok(summary.context[0].endsWith(contract))
}
for (const input of [{ sessionID: "child" }, {}]) {
  const unrelated = { system: [] }
  await hooks["experimental.chat.system.transform"](input, unrelated)
  assert.deepEqual(unrelated.system, [])
}
const child = { messages: [{ info: { id: "msg_child", sessionID: "child", role: "user" }, parts: [] }] }
await hooks["experimental.chat.messages.transform"]({}, child)
assert.deepEqual(child.messages[0].parts, [])
hooks.dispose?.({ silent: true })
const fs = await import("node:fs")
assert.deepEqual(fs.readdirSync(process.env.HOME, { recursive: true }), [],
  "worker hooks wrote main lifecycle state")
'''
            env = {**BASE_ENV, "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "HOME": tmp, "AGENT_HOME": str(ROOT), "AGENT_SESSION_ROLE": "worker",
                   "AGENT_DISPATCH_REGISTERED_WORKER": "1", "AGENT_DISPATCH_STAGE_AUTHORITY": "1",
                   "AGENT_DISPATCH_SUBSESSION_ID": "", "PYTHONDONTWRITEBYTECODE": "1",
                   "XDG_STATE_HOME": tmp + "/.state", "XDG_CONFIG_HOME": tmp + "/.config",
                   "XDG_DATA_HOME": tmp + "/.data", "XDG_CACHE_HOME": tmp + "/.cache"}
            run = subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT,
                                 env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_response_telemetry_does_not_block_the_original_stream(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable for the V2 plugin module contract")
        with tempfile.TemporaryDirectory() as tmp:
            script = r'''
import assert from "node:assert/strict"
import childProcess from "node:child_process"
import { syncBuiltinESMExports } from "node:module"
import { pathToFileURL } from "node:url"
let recorded
const calls = []
childProcess.spawn = (command, args) => ({
  on() {},
  stdin: {
    on() {},
    end(body) { calls.push({ command, args, body }); recorded?.() },
  },
})
syncBuiltinESMExports()
const plugin = (await import(pathToFileURL(process.env.PLUGIN_PATH))).default
const hooks = new Map()
const ctx = {
  location: { directory: process.env.TEMP_ROOT },
  session: { hook: async (name, fn) => hooks.set(name, fn) },
  tool: { hook: async () => {} },
  shell: { hook: async () => {} },
  permission: { hook: async () => {} },
  event: { subscribe: async function* ({ signal }) {
    await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }))
  } },
}
const cleanup = await plugin.setup(ctx)
try {
  const hook = hooks.get("http.response")
  assert.equal(typeof hook, "function")
  for (const status of [200, 403]) for (const size of [32, 65537]) {
    let controller
    const bytes = new Uint8Array(size).fill(65)
    const response = new Response(new ReadableStream({ start(value) {
      controller = value
      if (size > 65536) value.enqueue(bytes)
    } }), { status, headers: { "content-type": "text/event-stream" } })
    let clones = 0
    const clone = response.clone.bind(response)
    response.clone = () => { clones++; return clone() }
    let returned = false
    const observed = new Promise((resolve) => { recorded = resolve })
    const running = Promise.resolve(hook({
      sessionID: "ses_streamfixture", model: { providerID: "fixture", id: "stream" }, response,
    })).then(() => { returned = true })
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(returned, true, "telemetry held the provider callback before the original consumer")
    assert.equal(response.bodyUsed, false, "telemetry consumed the original body")
    assert.equal(clones, status === 200 ? 0 : 1, "successful streams must not be cloned")
    const reader = response.body.getReader()
    const first = reader.read()
    if (size <= 65536) controller.enqueue(bytes)
    assert.deepEqual((await first).value, bytes, "the original consumer did not receive its first chunk")
    controller.close()
    assert.equal((await reader.read()).done, true)
    await running
    let timeout
    try {
      await Promise.race([observed, new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("provider observation was lost")), 1000)
      })])
    } finally { clearTimeout(timeout) }
    const call = calls.at(-1)
    assert.equal(call.command, "python3")
    assert.equal(call.args[1], "record-provider-response")
    assert.deepEqual(call.args.slice(2), ["--model", "fixture/stream", "--status", String(status)])
    assert.equal(call.body.byteLength, status === 200 ? 0 : Math.min(size, 65536))
  }
  const stalled = new Response(new ReadableStream(), { status: 403 })
  let timeout
  try {
    const observed = new Promise((resolve) => { recorded = resolve })
    await hooks.get("http.response")({
      sessionID: "ses_streamfixture", model: { providerID: "fixture", id: "stream" }, response: stalled,
    })
    await Promise.race([observed, new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error("error observation waited indefinitely")), 2000)
    })])
    assert.equal(calls.at(-1).body.byteLength, 0)
  } finally {
    clearTimeout(timeout)
    await stalled.body.cancel()
  }
  assert.equal(calls.length, 5, "provider observations were duplicated")
} finally { await cleanup() }
'''
            env = {**os.environ, "HOME": tmp, "XDG_STATE_HOME": str(Path(tmp) / "state"),
                   "FLEET_SESSION_REGISTRY_DIR": str(Path(tmp) / "state/agent-fleet/sessions"),
                   "AGENT_DISPATCH_ATTEMPT_ID": "",
                   "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "TEMP_ROOT": tmp, "AGENT_HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([node, "--input-type=module", "-e", script],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_v2_form_permission_refs_and_model_limits_are_exact_body_free_observations(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            script = r'''
import assert from "node:assert/strict"
import fs from "node:fs"
import childProcess from "node:child_process"
import { syncBuiltinESMExports } from "node:module"
import { pathToFileURL } from "node:url"
const writes = []
let releaseFirst
childProcess.spawn = (command, args) => ({
  on(name, callback) {
    if (name === "close" && args[0].endsWith("fleet-interaction-state.py")) {
      if (!releaseFirst) releaseFirst = callback
      else queueMicrotask(() => callback(0))
    }
  },
  unref() {}, kill() {}, stdin: { on() {}, end(body) { writes.push({ args, body }) } },
})
syncBuiltinESMExports()
const plugin = (await import(pathToFileURL(process.env.PLUGIN_PATH))).default
const hooks = new Map()
let drained
const eventsDone = new Promise(resolve => { drained = resolve })
const nativeEvents = [
  { type: "form.created", data: { form: { sessionID: "ses_exact", id: "form_a",
    questions: [{ question: "PRIVATE-PROMPT", options: ["PRIVATE-CHOICE"] }] } } },
  { type: "form.replied", data: { sessionID: "ses_exact", id: "form_a", answers: ["PRIVATE-ANSWER"] } },
  { type: "permission.asked", data: { sessionID: "ses_exact", id: "perm_b", pattern: "PRIVATE-PATH" } },
  { type: "permission.replied", data: { sessionID: "ses_exact", requestID: "perm_b", reply: "always" } },
]
const ctx = {
  location: { directory: process.env.TEMP_ROOT },
  session: { hook: async (name, fn) => hooks.set(name, fn),
    get: async ({ sessionID }) => ({ id: sessionID, title: "Main work" }) },
  tool: { hook: async () => {} }, shell: { hook: async () => {} },
  permission: { hook: async () => {} },
  event: { subscribe: async function* ({ signal }) {
    for (const event of nativeEvents) yield event
    drained()
    await new Promise(resolve => signal.addEventListener("abort", resolve, { once: true }))
  } },
}
const cleanup = await plugin.setup(ctx)
try {
  await eventsDone
  assert.equal(writes.filter(call => call.args[0].endsWith("fleet-interaction-state.py")).length, 1,
    "a reply overtook the still-running set hook")
  releaseFirst(0)
  for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve))
  const calls = writes.filter(call => call.args[0].endsWith("fleet-interaction-state.py"))
  assert.equal(calls.length, 4)
  assert.deepEqual(calls.map(call => JSON.parse(call.body)), [
    { session_id: "ses_exact", request_id: "form_a", source: "opencode-form" },
    { session_id: "ses_exact", request_id: "form_a" },
    { session_id: "ses_exact", request_id: "perm_b" },
    { session_id: "ses_exact", request_id: "perm_b" },
  ])
  assert.equal(calls[2].args.at(-1), "permission")
  assert.doesNotMatch(JSON.stringify(calls), /PRIVATE-|answers|questions|pattern/)
  await hooks.get("http.response")({ sessionID: "ses_exact",
    model: { providerID: "fixture", id: "model", limit: { context: 900000, output: 8192 } },
    response: new Response("", { status: 200 }) })
  const directory = `${process.env.XDG_STATE_HOME}/agent-fleet/context-policy/opencode`
  let record = JSON.parse(fs.readFileSync(`${directory}/${fs.readdirSync(directory)[0]}`))
  assert.equal(record.session_id, "ses_exact")
  assert.equal(record.model, "fixture/model")
  assert.equal(record.policy.effective_window_tokens, 900000)
  assert.equal(record.policy.state, "unknown")
  assert.equal(record.policy.applied_trigger_tokens, null)
  await hooks.get("http.response")({ sessionID: "ses_other",
    model: { providerID: "fixture", id: "no-limits" }, response: new Response("", { status: 200 }) })
  record = JSON.parse(fs.readFileSync(`${directory}/${fs.readdirSync(directory)[0]}`))
  assert.equal(record.session_id, "ses_other")
  assert.equal(record.policy.effective_window_tokens, null)
  assert.equal(record.policy.reason, "native-model-window-not-observed")
} finally { cleanup() }
'''
            env = {**BASE_ENV, "HOME": tmp, "XDG_STATE_HOME": str(Path(tmp) / "state"),
                   "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
                   "FLEET_SESSION_REGISTRY_DIR": str(Path(tmp) / "state/agent-fleet/sessions"),
                   "AGENT_DISPATCH_ATTEMPT_ID": "", "OPENCODE_VERSION": "2.0.32",
                   "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "TEMP_ROOT": tmp, "AGENT_HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([node, "--input-type=module", "-e", script],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_v2_shell_env_carries_the_executing_session(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable for the V2 plugin module contract")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            home.mkdir()
            project.mkdir()
            script = r'''
import assert from "node:assert/strict"
import { pathToFileURL } from "node:url"
const plugin = (await import(pathToFileURL(process.env.PLUGIN_PATH))).default
const hooks = new Map()
const sessions = {
  ses_main: { id: "ses_main", agent: "build", title: "Main work", parentID: null },
  ses_other: { id: "ses_other", agent: "build", title: "Other work", parentID: null },
  ses_worker: { id: "ses_worker", agent: "hearting-worker",
                title: "hearting-worker-att-fixture", parentID: null },
}
const ctx = {
  location: { directory: process.env.TEMP_ROOT, project: { id: "fixture" } },
  app: { log: async () => {} },
  session: {
    get: async ({ sessionID }) => ({ data: sessions[sessionID] || null }),
    prompt: async () => ({ data: null }),
    context: async () => ({ data: [] }),
    hook: async (name, fn) => hooks.set("session." + name, fn),
  },
  tool: { hook: async (name, fn) => hooks.set("tool." + name, fn) },
  shell: { hook: async (name, fn) => hooks.set("shell." + name, fn) },
  event: { subscribe: async function* ({ signal }) {
    await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }))
  } },
  permission: { hook: async (name, fn) => hooks.set("permission." + name, fn) },
}
const cleanup = await plugin.setup(ctx)
const shellEnv = async (sessionID, command) => {
  await hooks.get("tool.execute.before")({ tool: "shell", sessionID, input: { command } })
  const event = { command, env: {} }
  await hooks.get("shell.create.before")(event)
  return event.env
}
try {
  // 2.0.32 order: execute.before, then create.before with no OPENCODE_SESSION_ID.
  const main = await shellEnv("ses_main", "echo main")
  assert.equal(main.AGENT_PARENT_COMPLETION_CARRIER, "opencode-turn:ses_main")
  const worker = await shellEnv("ses_worker", "echo worker")
  assert.equal(worker.AGENT_PARENT_COMPLETION_CARRIER, "")
  await hooks.get("tool.execute.before")({ tool: "shell", sessionID: "ses_main",
                                           input: { command: "echo shared" } })
  await hooks.get("tool.execute.before")({ tool: "shell", sessionID: "ses_other",
                                           input: { command: "echo shared" } })
  const shared = { command: "echo shared", env: {} }
  await hooks.get("shell.create.before")(shared)
  assert.equal(shared.env.AGENT_PARENT_COMPLETION_CARRIER, "",
               "two sessions waiting on one command must stay fail-closed")
  // An unmapped command keeps the existing env fallback.
  const fallback = { command: "echo native", env: { OPENCODE_SESSION_ID: "ses_main" } }
  await hooks.get("shell.create.before")(fallback)
  assert.equal(fallback.env.AGENT_PARENT_COMPLETION_CARRIER, "opencode-turn:ses_main")
} finally { await cleanup() }
'''
            env = {**BASE_ENV, "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "TEMP_ROOT": str(project), "HOME": str(home), "AGENT_HOME": str(ROOT),
                   "PYTHONDONTWRITEBYTECODE": "1", "XDG_STATE_HOME": str(home / ".state"),
                   "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".data"),
                   "XDG_CACHE_HOME": str(home / ".cache")}
            result = subprocess.run([node, "--input-type=module", "-e", script],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_v2_registers_guards_and_worker_hooks_are_silent(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable for the V2 plugin module contract")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            home.mkdir()
            project.mkdir()
            script = r'''
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import { pathToFileURL } from "node:url"
const plugin = (await import(pathToFileURL(process.env.PLUGIN_PATH))).default
assert.equal(plugin.id, "hearting-guards")
assert.equal(typeof plugin.server, "function")
const hooks = new Map()
const sessionID = "ses_workerfixture"
let modelCalls = 0
let appLogs = 0
const controller = new AbortController()
const ctx = {
  location: { directory: process.env.TEMP_ROOT, project: { id: "fixture" } },
  app: { log: async () => { appLogs++ } },
  session: {
    get: async ({ sessionID: sid }) => ({
      id: sid, agent: "build",
      title: { ses_main: "Main work", ses_titler: "hearting-worker-hearting-fleet-titler" }[sid]
        || "hearting-worker-att-fixture",
      // Native 2.0.32 session info carries parentID, null for a root session.
      parentID: sid === "ses_child" ? sessionID : null,
      location: { directory: process.env.TEMP_ROOT },
    }),
    prompt: async () => { modelCalls++ },
    hook: async (name, fn) => hooks.set("session." + name, fn),
  },
  tool: { hook: async (name, fn) => hooks.set("tool." + name, fn) },
  shell: { hook: async (name, fn) => hooks.set("shell." + name, fn) },
  event: {
    subscribe: async function* ({ signal }) {
      await new Promise((resolve) => setTimeout(resolve, 5))
      yield { type: "session.idle", data: { sessionID } }
      await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }))
    },
  },
  permission: { hook: async (name, fn) => hooks.set("permission." + name, fn) },
}
const cleanup = await plugin.setup(ctx)
for (const name of ["tool.execute.before", "tool.execute.after", "shell.create.before",
                    "session.prompt", "session.context", "session.compaction"]) {
  assert.equal(typeof hooks.get(name), "function", "missing V2 hook " + name)
}
const prompt = hooks.get("session.prompt")
await prompt({ sessionID, messageID: "msg_fixture", agent: "build",
               prompt: { text: "worker prompt" } })
const context = { sessionID, system: [], messages: [], options: {}, tools: {} }
await hooks.get("session.context")(context)
await new Promise((resolve) => setTimeout(resolve, 20))
assert.equal(modelCalls, 0, "worker hook started another model request")
assert.equal(appLogs, 0, "worker lifecycle emitted state/log writes")
assert.equal(context.system.length, 1)
const contract = context.system[0].text
assert.match(contract, /artifact: <canonical path/)
assert.match(contract, /verdict: PASS \| FAIL \| BLOCKED/)
assert.match(contract, /blocker: none/)
assert.match(contract, /runtime_wait: registered-children/)
assert.doesNotMatch(contract, /Portable Worker Kernel|memory lifecycle/)
// A summary can forget every field; each subsequent primary request still
// receives the exact canonical contract, without repeated accumulation.
for (let i = 0; i < 2; i++) {
  const compacted = { sessionID, system: [], messages: [{ role: "user", content: "summary without fields" }] }
  await hooks.get("session.context")(compacted)
  await hooks.get("session.context")(compacted)
  assert.deepEqual(compacted.system, [{ type: "text", text: contract }])
  const summary = { sessionID, system: [] }
  await hooks.get("session.compaction")(summary)
  await hooks.get("session.compaction")(summary)
  assert.equal(summary.system.length, 1)
  assert.ok(summary.system[0].text.endsWith(contract))
  assert.match(summary.system[0].text, /normal compaction summary/)
  assert.equal(summary.context, undefined, "V1 context leaked into the V2 event")
}
assert.equal(modelCalls, 0)
assert.equal(appLogs, 0)
assert.deepEqual(fs.readdirSync(process.env.TEMP_ROOT, { recursive: true }), [],
  "worker lifecycle wrote project state")
assert.deepEqual(fs.readdirSync(process.env.HOME, { recursive: true }), [],
  "worker lifecycle wrote user state")
// Neighbors in the shared service: a native child, a fleet helper session and
// a main session, whose own normal lifecycle may write main state.
for (const sid of ["ses_child", "ses_titler", "ses_main"]) {
  const unrelated = { sessionID: sid, system: [] }
  await hooks.get("session.context")(unrelated)
  assert.ok(!unrelated.system.some((part) => part.text?.includes(contract)), "worker contract leaked to " + sid)
  const summary = { sessionID: sid, system: [] }
  await hooks.get("session.compaction")(summary)
  // Main V2 compaction keeps the native summary template untouched.
  assert.deepEqual(summary.system, [], "compaction instructions leaked to " + sid)
}
await cleanup()
'''
            env = {**BASE_ENV, "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "TEMP_ROOT": str(project), "HOME": str(home), "AGENT_HOME": str(ROOT),
                   "PYTHONDONTWRITEBYTECODE": "1", "XDG_STATE_HOME": str(home / ".state"),
                   "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".data"),
                   "XDG_CACHE_HOME": str(home / ".cache")}
            result = subprocess.run([node, "--input-type=module", "-e", script],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
