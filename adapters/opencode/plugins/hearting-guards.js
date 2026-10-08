import path from "node:path"
import { fileURLToPath } from "node:url"
import { spawnSync, spawn } from "node:child_process"
import { existsSync, mkdirSync, writeFileSync, utimesSync, openSync, readSync, closeSync, realpathSync, readFileSync, readdirSync } from "node:fs"
import { createHash } from "node:crypto"

const pluginDir = path.dirname(fileURLToPath(import.meta.url))
const pluginRoot = path.resolve(pluginDir, "../../..")
const envRoot = process.env.AGENT_HOME ? path.resolve(process.env.AGENT_HOME) : ""
const isHarnessRoot = (candidate) =>
  candidate &&
  existsSync(path.join(candidate, "core", "CORE.md")) &&
  existsSync(path.join(candidate, "adapters", "opencode", "bin", "preflight.sh"))
const root = isHarnessRoot(envRoot) ? envRoot : pluginRoot
const preflight = path.join(root, "adapters", "opencode", "bin", "preflight.sh")
const summaryTrigger = path.join(root, "utilities", "session_summary_trigger.py")
const checkpointTrigger = path.join(root, "utilities", "artifact_checkpoint_trigger.py")
const sessionTidy = path.join(root, "utilities", "session_tidy.py")
const herdrProjection = path.join(root, "tools", "fleet", "herdr_projection.py")
const coreWriteGuard = path.join(root, "hooks", "core-write-guard.py")
const routePresenceGate = path.join(root, "utilities", "route_presence_gate.py")
const frameNativeAnswer = path.join(root, "utilities", "frame_native_answer.py")
const startReceipt = path.join(root, "utilities", "start_receipt.py")
const routeGateTools = new Set(["write", "edit", "multiedit", "patch", "apply_patch", "bash"])
const designPattern = /(designs?\/|\/design\/|spec\/design|preview\.html$|slides?\.html$|03_components|scaffolds\/)/
const promptBySession = new Map()
const turnBySession = new Map()
// Prompt-lifecycle context that must stay visible for every model call of a
// session, not just its first one. OpenCode has no Claude-style
// `additionalContext` that merges into the user turn and persists in history:
// `experimental.chat.system.transform` output lives only in the system prompt of
// the single request it decorates. Measured on opencode 1.17.13 — the transform
// fires once per model call (title generation, the answering turn, and every
// tool-loop continuation), so a once-per-session injection is consumed by the
// title call and never reaches the answering model at all.
//   * memoryBySession — session memory briefing, computed once per session and
//     re-emitted on every call so it persists the way Claude's SessionStart
//     additionalContext does.
//   * localEvidenceBySession — artifact-root presence probe, computed once per
//     session for the same reason: the block is byte-identical between turns
//     because only a newly written artifact changes it, so recomputing it per
//     turn bought nothing and spent ~360 tokens a turn.
//   * turnContextBySession — { turn, blocks } for the capsule candidate probe
//     and the per-turn signals: recomputed when a new user turn arrives, then
//     re-emitted on every model call of that turn.
const memoryBySession = new Map()
const localEvidenceBySession = new Map()
const turnContextBySession = new Map()
//   * cardBySession — { turn, text } for the session card / tidy notice. The
//     card is consumed once per user turn in chat.message (independent of the
//     candidate probe) and the kept text is re-emitted on every model call of
//     that turn, so a tool-loop continuation never consumes a second card.
const cardBySession = new Map()

function baseDir(ctx) {
  return ctx.worktree || ctx.directory || process.cwd()
}

// Headless dispatch liveness probe support.
// When the OpenCode runtime starts a headless dispatch via dispatch-headless.py,
// it exports OPENCODE_DISPATCH_SLUG (and the dispatch interpreter passes the
// same env to the runtime child). Recording two artifacts at plugin init gives
// dispatch-liveness.py a secondary, cheap signal independent of the OpenCode
// SQLite session mtime:
//   * <agent-home>/.dispatch/plugin-load.<slug>.mark — created once at plugin
//     init, proving the plugin was actually loaded by the headless runtime.
//   * <agent-home>/.dispatch/logs/<slug>.heartbeat — touched on every
//     session.idle event (idle == turn done == still alive), so a stale or
//     crashed headless that never reaches idle will have an aging heartbeat.
// Both are best-effort: a plugin must never block a turn because it failed to
// record a liveness side-channel.
function dispatchSlug() {
  return process.env.OPENCODE_DISPATCH_SLUG || ""
}

function isWorkerSession() {
  return (
    (process.env.AGENT_SESSION_ROLE || "").toLowerCase() === "worker" ||
    process.env.AGENT_DISPATCH_CHILD === "1" ||
    Boolean(process.env.AGENT_DISPATCH_DEPTH) ||
    Boolean(process.env.OPENCODE_DISPATCH_SLUG) ||
    process.env.FLEET_TITLE_REFRESH === "1"
  )
}

function touchHeartbeat(slug) {
  if (!slug) return
  try {
    const dispatchDir = path.join(root, ".dispatch")
    const logsDir = path.join(dispatchDir, "logs")
    mkdirSync(logsDir, { recursive: true })
    const hb = path.join(logsDir, `${slug}.heartbeat`)
    const now = new Date()
    try {
      utimesSync(hb, now, now)
    } catch {
      writeFileSync(hb, `${now.toISOString()}\n`, { encoding: "utf8" })
    }
  } catch {
    // best-effort; liveness side-channel must never throw
  }
}

function markPluginLoaded(slug) {
  if (!slug) return
  try {
    const dispatchDir = path.join(root, ".dispatch")
    mkdirSync(dispatchDir, { recursive: true })
    const marker = path.join(dispatchDir, `plugin-load.${slug}.mark`)
    writeFileSync(marker, `${new Date().toISOString()}\n`, { encoding: "utf8" })
  } catch {
    // best-effort
  }
}

function normalizeFile(ctx, file) {
  if (!file || file === "/dev/null") return ""
  if (path.isAbsolute(file)) return file
  return path.resolve(baseDir(ctx), file)
}

function patchFiles(ctx, patch) {
  if (!patch) return []
  const files = []
  const pattern = /^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$/gm
  let match
  while ((match = pattern.exec(patch)) !== null) {
    const file = normalizeFile(ctx, match[1] || match[2])
    if (file) files.push(file)
  }
  return files
}

function targetFiles(ctx, tool, args) {
  const name = typeof tool === "string" ? tool : tool?.name || ""
  if (name === "write" || name === "edit") {
    return [normalizeFile(ctx, args.filePath || args.path || args.file)].filter(Boolean)
  }
  if (name === "apply_patch" || name === "patch") {
    return patchFiles(ctx, args.patchText || args.patch || "")
  }
  return []
}

function isDesignHtml(file) {
  return /\.html?$/i.test(file) && designPattern.test(file.replaceAll(path.sep, "/"))
}

function runPreflight(command, args) {
  const result = spawnSync(preflight, [command, ...args], {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root },
    encoding: "utf8",
  })

  if (result.status !== 0) {
    const detail = [result.stdout, result.stderr].filter(Boolean).join("\n").trim()
    throw new Error(detail || `agent harness preflight failed: ${command}`)
  }
}

function runWorkerState(action, payload = {}) {
  const helper = path.join(root, "utilities", "worker-state-hook.py")
  const result = spawnSync("python3", [helper, action], {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root },
    input: JSON.stringify(payload),
    encoding: "utf8",
  })
  if (result.status !== 0) {
    const detail = [result.stdout, result.stderr].filter(Boolean).join("\n").trim()
    throw new Error(detail || `worker state hook failed: ${action}`)
  }
  return (result.stdout || "").trim()
}

function spawnSummary(sid, phase) {
  if (!sid || isWorkerSession()) return
  try {
    const child = spawn("python3", [summaryTrigger, "--harness", "opencode",
      "--sid", sid, "--phase", phase, "--wait", phase === "initial" ? "5" : "1"], {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root, AGENT_SESSION_ROLE: "worker" },
      detached: true,
      stdio: "ignore",
    })
    child.unref()
  } catch {
    // best-effort; session execution never depends on observational summaries
  }
}

// Turn-end cycle observation, shared with Claude and Codex Stop: the same trigger
// launcher (utilities/artifact_checkpoint_trigger.py) takes the session id on stdin,
// applies its own interval, off switch and cutover checks, and starts the detached
// checkpoint child. Main and worker sessions both run it (a worker names its cycle in
// its environment). Fire-and-forget: a turn never waits on it.
function spawnCheckpoint(sid) {
  if (/^(off|0|false|no|disabled)$/i.test((process.env.AGENT_ARTIFACT_CHECKPOINT || "").trim())) return
  try {
    const child = spawn("python3", [checkpointTrigger, "turn-end", "--harness", "opencode"], {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root },
      detached: true,
      stdio: ["pipe", "ignore", "ignore"],
    })
    child.on("error", () => {})
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify({ sessionID: sid || "" }))
    child.unref()
  } catch {
    // best-effort; a turn never depends on the checkpoint observation
  }
}

// Pane-header identity, shared with Claude and Codex (tools/fleet/herdr_projection.py).
// OpenCode has no user-configurable status line, so the pane header is the only place
// this session can say which session it is -- and it must say it in the same shape the
// other two harnesses do. Fire-and-forget: a turn never waits on a display projection.
function sdkResponseData(result) {
  if (!result || typeof result !== "object" || result.error != null || result.response?.ok === false) return null
  return Object.hasOwn(result, "data") ? result.data : result
}

// One native launch event and one publisher belong to this module/process, not
// to a directory-scoped plugin constructor. worker.reload recreates those contexts.
function nativePaneSelector(argv) {
  if (!Array.isArray(argv) || !argv.length || path.basename(argv[0]) !== "opencode") return null
  const values = new Set(["--model", "-m", "--prompt", "--agent", "--port", "--hostname",
    "--mdns-domain", "--cors", "--log-level"])
  const booleans = new Set(["--continue", "-c", "--fork", "--auto", "--yolo",
    "--dangerously-skip-permissions", "--mdns", "--print-logs"])
  const commands = new Set(["run", "serve", "attach", "web", "auth", "agent", "models",
    "stats", "export", "import", "session", "upgrade", "uninstall", "mcp", "acp", "debug", "completion"])
  let sid = null, positional = 0, ended = false
  for (let i = 1; i < argv.length; i++) {
    const arg = argv[i]
    if (typeof arg !== "string" || /[\x00-\x1f\x7f]/.test(arg)) return null
    if (arg === "--" && !ended) { ended = true; continue }
    if (ended || !arg.startsWith("-")) {
      if (++positional > 1 || (!ended && commands.has(arg))) return null
      continue
    }
    const equal = arg.indexOf("=")
    const key = equal < 0 ? arg : arg.slice(0, equal)
    let value = equal < 0 ? undefined : arg.slice(equal + 1)
    if (key === "--session" || key === "-s" || values.has(key)) {
      if (value === undefined) {
        value = argv[++i]
        // yargs leaves a following option in the option stream. Treat that
        // ambiguous separated value as unavailable; explicit = values remain literal.
        if (typeof value !== "string" || value.startsWith("-")) return null
      }
      if (typeof value !== "string" || /[\x00-\x1f\x7f]/.test(value)) return null
      if (key === "--session" || key === "-s") {
        if (sid !== null || !/^ses_[A-Za-z0-9]+$/.test(value) || value.length > 256) return null
        sid = value
      }
      continue
    }
    if (key === "--no-continue" || key === "--no-fork" || key === "--no-mdns") {
      if (value !== undefined) return null
      continue
    }
    if (!booleans.has(key)) return null // Unknown/mini/command grammar has no selector fallback.
    if (value === undefined && ["true", "false"].includes(argv[i + 1])) value = argv[++i]
    if (value !== undefined && !["true", "false"].includes(value)) return null
    if (["--continue", "-c", "--fork"].includes(key) && value !== "false") return null
  }
  return sid
}

function readPaneProc(file, limit) {
  const fd = openSync(file, "r")
  try {
    const data = Buffer.alloc(limit + 1)
    const bytes = readSync(fd, data, 0, data.length, null)
    if (bytes > limit) throw new Error("native origin unavailable")
    return data.subarray(0, bytes)
  } finally { closeSync(fd) }
}

function ownNativePaneOrigin() {
  try {
    const command = readPaneProc("/proc/self/cmdline", 32768)
    if (!command.length || command.at(-1) !== 0) return null
    const argv = command.toString("utf8").slice(0, -1).split("\0")
    const sid = nativePaneSelector(argv)
    // A fresh bare TUI launch carries no session selector at all (and is not
    // a resume/attach/mini invocation): it owns no argv identity, but its
    // lifecycle can still carry the TUI entry's own selection record below.
    const bare = !sid && argv.length > 0 && path.basename(argv[0]) === "opencode"
      && !argv.some(arg => typeof arg === "string" && (arg === "--session" || arg === "-s"
        || arg.startsWith("--session=") || arg === "--continue" || arg === "-c"
        || arg === "--fork" || arg === "--mini"))
    if (!sid && !bare) return null
    const stat = readPaneProc("/proc/self/stat", 4096).toString("utf8")
    const split = stat.lastIndexOf(") ")
    const start = stat.slice(split + 2).trim().split(/\s+/)[19]
    if (split < 0 || Number(stat.slice(0, stat.indexOf(" ("))) !== process.pid || !/^\d+$/.test(start)) return null
    return { pid: process.pid, start, sid, bare, directory: realpathSync("/proc/self/cwd"), state: "pending" }
  } catch { return null }
}

let paneNativeOrigin // undefined means not yet read; null is unsupported, never SDK-filled.
const paneContexts = new WeakMap()
let paneOwningContext = null
let panePublisherSlot = null
let paneReportSequence = Date.now() * 1000

function registerPaneContext(ctx) {
  if (paneContexts.has(ctx)) return paneContexts.get(ctx)
  if (paneNativeOrigin === undefined) paneNativeOrigin = ownNativePaneOrigin()
  let ownsOrigin = false
  try { ownsOrigin = !!paneNativeOrigin && realpathSync(ctx.directory) === paneNativeOrigin.directory } catch {}
  const binding = { active: true, ownsOrigin, refreshPending: false, originObserved: false }
  paneContexts.set(ctx, binding)
  if (ownsOrigin) {
    if (paneOwningContext) retirePaneContext(paneOwningContext)
    paneOwningContext = ctx
    paneProjectionGeneration++
    paneProjectionRetryAt.delete(paneNativeOrigin.sid)
  }
  return binding
}

function retirePaneContext(ctx) {
  const binding = paneContexts.get(ctx)
  if (!binding) return
  binding.active = false
  if (paneOwningContext === ctx) {
    paneProjectionGeneration++
    paneOwningContext = null
    if (paneNativeOrigin?.state === "pending") paneNativeOrigin.state = "invalidated"
  }
  // A live publisher survives disposal. Closing its observer is not child exit.
}

function invalidatePaneOrigin(sid) {
  if (paneNativeOrigin?.sid === sid) {
    paneNativeOrigin.state = "invalidated"
    paneProjectionGeneration++
  }
}

function releasePanePublisher(slot) {
  if (panePublisherSlot === slot) panePublisherSlot = null
}

function peerIdentityLog(ctx, stage, reason, extra = {}) {
  try {
    if (typeof ctx?.client?.app?.log === "function") {
      const metadata = { ...extra }
      if (Object.hasOwn(metadata, "sessionID") && (typeof metadata.sessionID !== "string"
          || metadata.sessionID.length > 256 || /[\x00-\x1f\x7f]/.test(metadata.sessionID))) {
        delete metadata.sessionID
        metadata.sessionIDInvalid = true
      }
      Promise.resolve(ctx.client.app.log({ body: { service: "hearting-peer-identity",
        level: "info", message: "hearting-peer-identity",
        // OpenCode's host logger drops service; retain a fixed source in extra.
        extra: { module: "hearting-peer-identity", stage, reason, ...metadata },
      }})).catch(() => {})
    }
  } catch { /* Observation never gates an existing callback or publication. */ }
}

const paneCallbackObservedAt = new Map()
function observePaneCallback(ctx, callback, sid) {
  const key = callback + ":" + (typeof sid === "string" ? sid.slice(0, 256) : "invalid")
  const now = Date.now()
  if (now < (paneCallbackObservedAt.get(key) || 0)) return
  if (paneCallbackObservedAt.size >= 128) paneCallbackObservedAt.delete(paneCallbackObservedAt.keys().next().value)
  paneCallbackObservedAt.set(key, now + 10000)
  const reason = !sid ? "callback-no-session" : isWorkerSession() ? "callback-worker"
    : !process.env.HERDR_PANE_ID ? "callback-no-pane" : "callback-entry"
  peerIdentityLog(ctx, "callback", reason, { callback, sessionID: sid })
}

function publisherErrorFields(error) {
  const text = (value, limit) => {
    if (typeof value !== "string") return undefined
    let result = "", bytes = 0
    for (const char of value.replace(/[\x00-\x1f\x7f]/g, " ")) {
      const size = Buffer.byteLength(char, "utf8")
      if (bytes + size > limit) break
      result += char; bytes += size
    }
    return result
  }
  try {
    const fields = {}
    const code = Number.isInteger(error?.code) && error.code >= -2147483648 && error.code <= 2147483647
      ? error.code : text(error?.code, 64)
    const message = text(error?.message, 256)
    if (code !== undefined) fields.errorCode = code
    if (message !== undefined) fields.errorMessage = message
    if (Number.isInteger(error?.errno) && error.errno >= -2147483648 && error.errno <= 2147483647) {
      fields.errorErrno = error.errno
    }
    return fields
  } catch { return {} }
}

function publisherObservation(output) {
  if (typeof output !== "string" || Buffer.byteLength(output, "utf8") > 1024) throw new Error("invalid observation")
  const row = JSON.parse(output)
  const reasons = ["herdr-unavailable", "pane-unavailable", "guard-refused", "report-attempts-finished"]
  const statuses = ["not-attempted", "skipped", "exit0", "nonzero", "timeout", "spawn-error"]
  const keys = ["schema", "reason", "session_report", "metadata_report", "session_report_rc", "metadata_report_rc"]
  if (!row || row.schema !== "hearting-pane-observation-v1" || !reasons.includes(row.reason)
      || !statuses.includes(row.session_report) || !statuses.includes(row.metadata_report)
      || Object.keys(row).some(key => !keys.includes(key))) throw new Error("invalid observation")
  const extra = { sessionReport: row.session_report, metadataReport: row.metadata_report }
  for (const key of ["session_report_rc", "metadata_report_rc"]) {
    if (Object.hasOwn(row, key)) {
      if (!Number.isInteger(row[key]) || row[key] < -128 || row[key] > 255) throw new Error("invalid rc")
      extra[key] = row[key]
    }
  }
  return { reason: row.reason, extra }
}

function observePanePublisher(child, ctx, sid, generation, onSpawnError = () => {}) {
  let output = "", bytes = 0, finished = false
  const finish = (reason, extra = {}) => {
    if (finished) return
    finished = true
    clearTimeout(timer)
    child.stdout?.destroy()
    if (generation !== paneProjectionGeneration) {
      peerIdentityLog(ctx, "publisher", "publisher-stale-observation", { sessionID: sid })
    } else {
      peerIdentityLog(ctx, "publisher", reason, { sessionID: sid, ...extra })
    }
  }
  // Bound the observation, not the existing helper's execution.
  const timer = setTimeout(() => finish("publisher-observation-timeout"), 5000)
  timer.unref?.()
  child.stdout?.setEncoding("utf8")
  child.stdout?.on("error", () => finish("publisher-observation-read-error"))
  child.stdout?.on("data", chunk => {
    if (finished) return
    bytes += Buffer.byteLength(chunk, "utf8")
    if (bytes > 1024) { output = ""; finish("publisher-observation-overflow"); return }
    output += chunk
  })
  child.on("error", error => {
    const extra = publisherErrorFields(error)
    if (finished) peerIdentityLog(ctx, "publisher", "publisher-spawn-error", { sessionID: sid, ...extra })
    else finish("publisher-spawn-error", extra)
    onSpawnError()
  })
  child.on("close", code => {
    if (finished) return
    const extra = { sessionID: sid, publisherRc: Number.isInteger(code) ? code : null }
    if (code !== 0) {
      finish("publisher-exit-error", extra)
      return
    }
    try {
      const observation = publisherObservation(output)
      finish(observation.reason, { ...extra, ...observation.extra })
    } catch { finish("publisher-observation-invalid", extra) }
  })
}

// Invocation-time root for identity and completion helpers (core/CORE.md §2,
// core/ADAPTATION.md): preserve the session and inherited registry after pruning.
// The module-time root/helper freeze the release the plugin was imported from;
// a managed release pruned afterwards leaves a deleted cwd and a missing
// helper, so both the async spawn and the sync fallback fail with ENOENT
// (Node child_process: a nonexistent cwd and a missing command both emit
// ENOENT, errno -2 — the two causes are separated here, before spawn, not
// inferred from the error). When the frozen pair is live it is used unchanged;
// otherwise the first live harness root in the documented order wins and the
// invocation (session argv, sequence, identity) stays identical. With no live
// root the frozen pair is kept and the existing bounded error logs describe
// the failure as before.
function resolveRuntimeTarget(helperRel) {
  const live = (dir, helper) => {
    try {
      return !!dir && existsSync(path.join(dir, "core", "CORE.md"))
        && existsSync(path.join(dir, "adapters", "opencode", "bin", "preflight.sh"))
        && existsSync(helper)
    } catch { return false }
  }
  const frozenHelper = path.join(root, helperRel)
  if (live(root, frozenHelper)) return { root, helper: frozenHelper }
  // Portable order only (core/CORE.md §2 minus adapter-specific compat keys,
  // which this adapter must not reference): active AGENT_HOME, managed
  // current, linked fallbacks.
  const env = (process && process.env) || {}
  const home = env.HOME || ""
  const xdg = env.XDG_DATA_HOME || (home ? path.join(home, ".local", "share") : "")
  const candidates = [env.AGENT_HOME,
    xdg ? path.join(xdg, "hearting", "current") : "",
    home ? path.join(home, "hearting") : "",
    home ? path.join(home, "agent_setting") : ""]
  for (const candidate of candidates) {
    if (!candidate) continue
    let dir = ""
    try { dir = path.resolve(candidate) } catch { continue }
    if (!dir) continue
    const helper = path.join(dir, helperRel)
    if (live(dir, helper)) return { root: dir, helper }
  }
  return { root, helper: frozenHelper }
}

function resolvePublisherTarget() {
  return resolveRuntimeTarget(path.join("tools", "fleet", "herdr_projection.py"))
}

// TUI current-selection provenance (core/ADAPTATION.md): the TUI-only entry
// records its native route selection for its own process lifecycle. Returns
// the exact record bytes when they name this session for the same pid and
// start time, else null. This is estimation of nothing: no callback/SDK-first
// root, timing, pane, or daemon value becomes an identity, and a blank home
// (no record) stays unverified. Publication itself stays single-owned: the
// existing publisher path below is the only herdr writer.
function tuiSelectionRead(sid) {
  try {
    const origin = paneNativeOrigin
    if (!origin || !origin.bare || origin.state === "invalidated") return null
    if (typeof sid !== "string" || !sid) return null
    if (!Number.isInteger(origin.pid) || origin.pid <= 1 || !/^\d+$/.test(origin.start || "")) return null
    const env = (process && process.env) || {}
    const home = env.HOME || ""
    const base = env.XDG_STATE_HOME || (home ? path.join(home, ".local", "state") : "")
    if (!base) return null
    const raw = readFileSync(path.join(base, "hearting", "tui-identity", origin.pid + "-" + origin.start + ".json"), "utf8")
    if (typeof raw !== "string" || Buffer.byteLength(raw, "utf8") > 1024) return null
    const row = JSON.parse(raw)
    if (!row || row.schema !== "hearting-tui-selection-v1" || row.sessionID !== sid
      || row.pid !== origin.pid || String(row.start) !== String(origin.start)) return null
    return raw
  } catch { return null }
}

const paneProjectionBusy = new Map()
const paneProjectionRetryAt = new Map()
let paneProjectionGeneration = 0
async function projectPane(sid, ctx, retry = false) {
  if (!sid || isWorkerSession() || !process.env.HERDR_PANE_ID) return
  const binding = registerPaneContext(ctx)
  if (!binding.active) return
  let ownsSession = binding.ownsOrigin && paneNativeOrigin?.sid === sid
  // A fresh-bare TUI carries no argv identity; the TUI entry's own
  // lifecycle-bound selection record is the only other provenance this
  // publisher accepts, and only before the exact SDK check below. The
  // record bytes are kept so the selection is re-verified after the SDK
  // call: a swap mid-verification must not publish the late success.
  let tuiRecord = null
  if (!ownsSession && binding.ownsOrigin) {
    tuiRecord = tuiSelectionRead(sid)
    if (tuiRecord) ownsSession = true
  }
  // Directory events may contain another parentless root. Neither SDK root
  // verification nor first arrival selects the native owner or a peer recipient.
  if (!ownsSession) {
    if (!binding.originObserved) {
      binding.originObserved = true
      peerIdentityLog(ctx, "origin", "native-origin-unavailable", { sessionID: sid })
    }
    return
  }
  // Retain only one refresh intent. After actual exit, the next normal callback
  // validates it again; no timer, autonomous SDK retry or publisher loop is armed.
  if (panePublisherSlot) { binding.refreshPending = true; return }
  const busy = paneProjectionBusy.get(sid)
  if ((busy && busy.active) || (retry && Date.now() < (paneProjectionRetryAt.get(sid) || 0))) return
  paneProjectionBusy.set(sid, binding)
  paneProjectionRetryAt.set(sid, Date.now() + 10000)
  binding.refreshPending = false
  const generation = paneProjectionGeneration
  let timer
  let verified = false
  let reason = "sdk-unavailable"
  let style = "unobserved"
  let httpStatus = null
  const started = Date.now()
  const controller = new AbortController()
  try {
    if (typeof ctx?.client?.session?.get === "function") {
      const result = await Promise.race([ctx.client.session.get({ path: { id: sid },
        throwOnError: true, signal: controller.signal }), new Promise((_, reject) => {
        timer = setTimeout(() => { controller.abort(); reject(new Error("peer-identity-timeout")) }, 500)
      })])
      style = result && Object.hasOwn(result, "data") ? "wrapped" : "data"
      if (Number.isInteger(result?.response?.status)) httpStatus = result.response.status
      const session = sdkResponseData(result)
      reason = !session || typeof session.id !== "string" ? "sdk-response-invalid"
        : session.id !== sid ? "sdk-session-mismatch"
        : session.parentID !== undefined && session.parentID !== null ? "sdk-child-session" : "verified"
      verified = reason === "verified"
    }
  } catch {
    reason = controller.signal.aborted ? "sdk-timeout" : "sdk-error"
  } finally { clearTimeout(timer) }
  if (!binding.active || generation !== paneProjectionGeneration) {
    verified = false
    reason = "sdk-stale-callback"
  }
  peerIdentityLog(ctx, "sdk", reason, { sessionID: sid, verified,
    responseStyle: style, httpStatus, elapsedMs: Date.now() - started })
  try {
    if (!binding.active || generation !== paneProjectionGeneration) return
    if (panePublisherSlot) { binding.refreshPending = true; return }
    // The SDK wait is not atomic with the selection: re-verify the exact
    // record bytes before publication so a late swap never publishes.
    if (tuiRecord && tuiSelectionRead(sid) !== tuiRecord) {
      peerIdentityLog(ctx, "publisher", "publisher-stale-selection", { sessionID: sid })
      return
    }
    const reportSession = verified && paneNativeOrigin.state !== "invalidated"
    const startup = reportSession && paneNativeOrigin.state === "pending"
    // A pruned import-time release must not take both publisher paths down:
    // resolve the live root/helper here so async and sync share one target.
    const target = resolvePublisherTarget()
    const args = [target.helper, "--harness", "opencode", "--session-id", sid]
    if (reportSession) {
      args.push("--seq", String(++paneReportSequence))
      if (startup) args.push("--session-start-source", "startup")
    } else args.push("--no-report-session")
    const slot = { child: null }
    panePublisherSlot = slot // Reserve before spawn, including synchronous reentrancy.
    const options = { cwd: target.root, env: { ...process.env, AGENT_HOME: target.root },
      detached: true, stdio: ["ignore", "pipe", "ignore"] }
    let fallbackTried = false, launched = false
    const fallback = () => {
      if (fallbackTried) return
      fallbackTried = true
      if (launched) {
        peerIdentityLog(ctx, "publisher", "publisher-sync-fallback-skipped-live", { sessionID: sid })
        return
      }
      if (!binding.active || generation !== paneProjectionGeneration || panePublisherSlot !== slot) {
        peerIdentityLog(ctx, "publisher", "publisher-sync-fallback-stale", { sessionID: sid })
        releasePanePublisher(slot)
        return
      }
      try {
        // Same executable, argv, sequence and environment; no startup replay with
        // a newer sequence. The synchronous child is bounded and owns this slot.
        const result = spawnSync("python3", args, { ...options, encoding: "utf8",
          timeout: 10000, killSignal: "SIGKILL", maxBuffer: 1024 })
        if (startup && Number.isInteger(result.pid) && result.pid > 0) paneNativeOrigin.state = "spent"
        if (result.error) {
          peerIdentityLog(ctx, "publisher", "publisher-sync-fallback-error", {
            sessionID: sid, ...publisherErrorFields(result.error) })
        } else if (result.status !== 0) {
          peerIdentityLog(ctx, "publisher", "publisher-sync-fallback-exit-error", {
            sessionID: sid, publisherRc: Number.isInteger(result.status) ? result.status : null })
        } else {
          peerIdentityLog(ctx, "publisher", "publisher-path-success", {
            sessionID: sid, publisherPath: "sync-fallback", publisherRc: 0 })
          try {
            const observation = publisherObservation(result.stdout)
            peerIdentityLog(ctx, "publisher", observation.reason, {
              sessionID: sid, publisherPath: "sync-fallback", ...observation.extra })
          } catch {
            peerIdentityLog(ctx, "publisher", "publisher-observation-invalid", {
              sessionID: sid, publisherPath: "sync-fallback" })
          }
        }
      } catch (error) {
        peerIdentityLog(ctx, "publisher", "publisher-sync-fallback-error", {
          sessionID: sid, ...publisherErrorFields(error) })
      } finally { releasePanePublisher(slot) }
    }
    let child
    try {
      child = spawn("python3", args, options)
    } catch (error) {
      peerIdentityLog(ctx, "publisher", "publisher-spawn-error", { sessionID: sid, ...publisherErrorFields(error) })
      fallback()
      return
    }
    slot.child = child
    const created = Number.isInteger(child.pid) && child.pid > 0
    if (startup && created) paneNativeOrigin.state = "spent"
    child.on("spawn", () => {
      launched = true
      peerIdentityLog(ctx, "publisher", "publisher-path-success", { sessionID: sid, publisherPath: "async" })
    })
    child.on("exit", () => releasePanePublisher(slot))
    child.on("close", () => releasePanePublisher(slot))
    observePanePublisher(child, ctx, sid, generation, fallback)
    peerIdentityLog(ctx, "publisher", "publisher-spawned", { sessionID: sid,
      nativeStartupAttempt: startup && created })
    child.unref()
  } catch (error) {
    peerIdentityLog(ctx, "publisher", "publisher-spawn-error", { sessionID: sid, ...publisherErrorFields(error) })
  } finally { if (paneProjectionBusy.get(sid) === binding) paneProjectionBusy.delete(sid) }
}

function collectPreflight(command, args) {
  const result = spawnSync(preflight, [command, ...args], {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root },
    encoding: "utf8",
  })

  return [result.stdout, result.stderr].filter(Boolean).join("\n").trim()
}

// A compacted session no longer holds the candidates memory showed it, so its
// display history is emptied through the one shared mem.py helper (fail-open).
// A brand-new session ID starts with an empty history on its own.
function forgetShownCandidates(sessionID) {
  if (!sessionID || isWorkerSession()) return
  try {
    spawnSync("python3", [path.join(root, "tools", "memory", "mem.py"),
      "_seen-reset", "--session-id", sessionID], {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root },
      stdio: "ignore",
      timeout: 3000,
      killSignal: "SIGKILL",
    })
  } catch {}
}

function collectCandidates(args) {
  const result = spawnSync(preflight, ["candidates", ...args], {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root },
    encoding: "utf8",
    timeout: 3000,
    killSignal: "SIGKILL",
  })
  if (result.error || result.status !== 0) return ""
  return (result.stdout || "").trim()
}

// Parent completion carrier (`opencode-turn`, utilities/harness_capabilities.py). A parent
// that launched a registered owner is told `parent_next=end-turn` and yields; this plugin
// process -- not the model -- finds the records owed to that session and starts its next
// turn with them through promptAsync. The shared sweep claims, renders and acks
// (utilities/dispatch_session_sweep.py); the plugin only looks, hands the text over and
// reports whether OpenCode took it. Tool commands carry CARRIER_ENV so a launch can tell
// a runtime that has this carrier from one that predates it. Fail-open: a carrier
// problem never blocks or throws into a turn.
const CARRIER_ENV = "AGENT_PARENT_COMPLETION_CARRIER"
const CARRIER_KIND = "opencode-turn"
const CARRIER_INTERVAL_MS = 5000
const CARRIER_ROOTS_RETRY_MS = 60000
const sweepRel = path.join("utilities", "dispatch_session_sweep.py")
const OWED_STATES = new Set(["pending", "claimed", "sent-ambiguous"])

// One bounded sweep step off the server's event loop; any failure is null.
function carrierCommand(action, sid, handed, observe = () => {}) {
  return new Promise((resolve) => {
    let out = ""
    let child
    let finished = false
    const target = resolveRuntimeTarget(sweepRel)
    const detail = { action, root: target.root, helper: target.helper }
    const finish = (value, reason, extra = {}) => {
      if (finished) return
      finished = true
      clearTimeout(timer)
      if (reason) observe(reason, { ...detail, ...extra })
      resolve(value)
    }
    const timer = setTimeout(() => {
      try { child?.kill("SIGKILL") } catch {}
      finish(null, "helper-timeout")
    }, 10000)
    if (!existsSync(target.root)) { finish(null, "directory-missing"); return }
    if (!existsSync(target.helper)) { finish(null, "helper-missing"); return }
    if (target.root !== root) observe("root-fallback", detail)
    try {
      child = spawn("python3", [target.helper, action, "--recipient-kind", CARRIER_KIND, "--session", sid || ""], {
        cwd: target.root, env: { ...process.env, AGENT_HOME: target.root }, stdio: ["pipe", "pipe", "ignore"],
      })
    } catch (error) { finish(null, "spawn-error", { code: error?.code || "" }); return }
    child.on("error", (error) => finish(null, "spawn-error", { code: error?.code || "" }))
    child.stdout.on("data", (chunk) => { out += chunk })
    child.on("close", (code) => {
      if (code !== 0) { finish(null, "helper-exit", { code }); return }
      try { finish(JSON.parse(out || "null")) } catch { finish(null, "invalid-output") }
    })
    child.stdin.on("error", () => {})
    child.stdin.end(handed ? JSON.stringify(handed) : "")
  })
}

function createCompletionCarrier(ctx) {
  const sessions = new Set()
  const status = new Map()
  const busy = new Set()
  let roots = null
  let rootsAt = 0
  let timer = null
  const prompter = () => ctx.client?.session
  const available = () => !isWorkerSession() && typeof prompter()?.promptAsync === "function"
  const skips = new Map()
  const command = (action, sid, handed) => carrierCommand(action, sid, handed,
    (reason, extra) => log("command", sid, reason, extra))
  function log(stage, sid, reason, extra = {}) {
    if (stage === "skip" && skips.get(sid) === reason) return
    if (stage === "skip") skips.set(sid, reason)
    else skips.delete(sid)
    try {
      Promise.resolve(ctx.client?.app?.log({ body: { service: "hearting-completion-carrier",
        level: "info", message: "hearting-completion-carrier",
        extra: { module: "hearting-completion-carrier", stage, reason, sessionID: sid || "",
          pid: process.pid, directory: baseDir(ctx), ...extra },
      }})).catch(() => {})
    } catch {}
  }

  // A cheap look at this session's own records, so an idle tick spawns nothing.
  async function owed(sid) {
    // Known roots are kept; none known yet (no state root, or the lookup failed) is asked again later.
    if (!roots?.length && Date.now() - rootsAt >= CARRIER_ROOTS_RETRY_MS) {
      rootsAt = Date.now()
      roots = (await command("roots", sid)) || []
      log("roots", sid, roots.length ? "resolved" : "unavailable", { roots })
    }
    const digest = createHash("sha256").update(sid).digest("hex")
    for (const base of roots) {
      let names = []
      try { names = readdirSync(path.join(base, "pending-delivery", digest)) } catch { continue }
      for (const name of names) {
        if (!name.startsWith("delivery-") || !name.endsWith(".json")) continue
        try {
          const record = JSON.parse(readFileSync(path.join(base, "pending-delivery", digest, name), "utf8"))
          if (record?.recipient_kind === CARRIER_KIND && OWED_STATES.has(record.state)) return true
        } catch {}
      }
    }
    return false
  }

  async function deliver(sid, look = true) {
    if (!sid) return
    if (busy.has(sid)) { log("skip", sid, "delivery-in-flight"); return }
    if (!available()) { log("skip", sid, isWorkerSession() ? "worker-session" : "promptAsync-unavailable"); return }
    if (status.get(sid) === "busy" || status.get(sid) === "retry") {
      log("skip", sid, `session-${status.get(sid)}`); return
    }
    busy.add(sid)
    let timeout
    try {
      if (look && !(await owed(sid))) { log("skip", sid, "no-owed-record"); return }
      const claimed = await command("deliver", sid)
      if (!claimed?.text || !Array.isArray(claimed.records) || !claimed.records.length) {
        log("skip", sid, claimed === null ? "claim-unavailable" : "nothing-claimed"); return
      }
      log("claim", sid, "claimed", { deliveries: claimed.records.map(record => record.delivery_id) })
      let taken = false
      let promptReason = "not-accepted"
      let httpStatus
      try {
        const result = await Promise.race([
          prompter().promptAsync({ path: { id: sid }, body: { parts: [{ type: "text", text: claimed.text }] } }),
          new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error("carrier-timeout")), 5000) }),
        ])
        taken = Boolean(result) && result.error == null && result.response?.ok !== false
        httpStatus = result?.response?.status
        promptReason = taken ? "accepted" : "not-accepted"
      } catch (error) { promptReason = error?.message === "carrier-timeout" ? "timeout" : "error" }
      log("prompt", sid, promptReason, { httpStatus })
      // Taken: the session's next turn holds the receipt. Not taken: the next pass claims it again.
      const settled = await command(taken ? "ack" : "release", sid, claimed)
      log("settle", sid, taken ? "ack" : "release", { count: settled?.count ?? null })
    } catch (error) {
      log("skip", sid, "delivery-error", { error: String(error?.message || error).slice(0, 180) })
    } finally {
      clearTimeout(timeout)
      busy.delete(sid)
    }
  }

  return {
    // The env a tool command of `sid` carries: only a runtime that will carry the receipt says so.
    env(sid) {
      if (!sid || !available()) return null
      sessions.add(sid)
      if (!timer) {
        timer = setInterval(() => { for (const id of sessions) deliver(id).catch(() => {}) }, CARRIER_INTERVAL_MS)
        timer.unref?.()
      }
      log("env", sid, "carrier-armed")
      return `${CARRIER_KIND}:${sid}`
    },
    status(sid, type) { if (sid && type) status.set(sid, type) },
    // A finished turn also looks past its own records (a seat handover stores them elsewhere).
    async idle(sid) { if (!sid) return; status.set(sid, "idle"); if (sessions.has(sid)) await deliver(sid, false) },
    forget(sid) { sessions.delete(sid); status.delete(sid) },
    dispose() { log("dispose", "", "context-disposed", { sessions: [...sessions] }); if (timer) clearInterval(timer); timer = null; sessions.clear() },
  }
}

// Session card / tidy notice: `session_tidy.py hook` records the event and prints
// what this session is due, once. Every failure is empty (no card, no throw).
function collectCard(event, sid, cwd) {
  if (!sid || isWorkerSession()) return ""
  const result = spawnSync("python3", [sessionTidy, "hook", "--harness", "opencode",
    "--event", event, "--session-id", sid, "--cwd", cwd], {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root },
    encoding: "utf8",
    timeout: 4000,
    killSignal: "SIGKILL",
  })
  if (result.error || result.status !== 0) return ""
  return (result.stdout || "").trim()
}

function appendContext(output, text) {
  if (!text) return
  if (!Array.isArray(output.system)) output.system = []
  output.system.push(text)
}

// F-100c -- receive side of a herdr steer, harness-neutral: a steward appends
// `(peer-from: <harness> <sid> <name>)` to the prompt body; the receiver writes its
// own `notice` peer_message_v1 under its exact session id (same record the Claude
// hook and the Codex hook write). Detached, fail-soft, never blocks the turn.
function spawnPeerNotice(sid, prompt, cwd) {
  if (!sid || !prompt || !prompt.includes("peer-from:")) return
  const tool = path.join(root, "utilities", "peer-message.py")
  const args = [tool, "receive",
    "--from-project", path.basename(cwd || ""),
    "--to-harness", "opencode",
    "--to-session-id", sid]
  try {
    const child = spawn("python3", args, {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root },
      stdio: ["pipe", "ignore", "ignore"],
      detached: true,
    })
    child.on("error", () => {})
    child.stdin.end(prompt)
    child.unref()
  } catch {}
}

// Pending transport uses the current plugin client and exact callback SID. It
// never touches the TUI draft or system-transform output. Acceptance is queued;
// only persisted exact text followed by a completed assistant turn acknowledges.
function pendingPeerCommand(sid, options = []) {
  if (!sid) return null
  const result = spawnSync("python3", [path.join(root, "utilities", "peer-message.py"),
    "pending", "--to-harness", "opencode", "--to-session-id", sid, ...options], {
    cwd: root, env: { ...process.env, AGENT_HOME: root }, encoding: "utf8", timeout: 1500,
  })
  if (result.error || result.status !== 0) return null
  try { return JSON.parse(result.stdout || "null") } catch { return null }
}

const pendingPeerBusy = new Set()
async function pendingPeerDelivery(ctx, sid) {
  if (!sid || pendingPeerBusy.has(sid) || isWorkerSession()) return
  const client = ctx.client?.session
  if (typeof client?.messages !== "function" || typeof client?.prompt !== "function") return
  pendingPeerBusy.add(sid)
  let timer
  const bounded = async (call) => {
    try {
      return await Promise.race([call, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("peer-context-timeout")), 1000)
      })])
    } finally { clearTimeout(timer) }
  }
  try {
    const rows = pendingPeerCommand(sid)
    if (!Array.isArray(rows) || !rows.length) return
    const response = await bounded(client.messages({ path: { id: sid } }))
    const messages = sdkResponseData(response)
    if (!Array.isArray(messages)) return
    for (const row of rows.slice(0, 3)) {
      const matches = messages.filter((m) => m?.info?.sessionID === sid && m.info.role === "user"
        && promptText(m) === row.text)
      if (matches.length > 1) continue // Ambiguous history is never an exact receipt.
      if (matches.length === 1) {
        const mid = matches[0].info.id
        if (!mid || (row.actual_message_id && row.actual_message_id !== mid)) continue
        const consumed = messages.some((m) => m?.info?.sessionID === sid && m.info.role === "assistant"
          && m.info.parentID === mid && typeof m.info.time?.completed === "number" && !m.info.error)
        if (consumed) {
          // Ack uses actual persisted history, independent of callback output.
          spawnSync("python3", [path.join(root, "utilities", "peer-message.py"), "receive",
            "--to-harness", "opencode", "--to-session-id", sid,
            "--from-project", path.basename(baseDir(ctx))], {
            cwd: root, env: { ...process.env, AGENT_HOME: root },
            input: row.text, encoding: "utf8", timeout: 1500,
          })
        }
        continue // Existing persisted text must never be inserted a second time.
      }
      const claimed = pendingPeerCommand(sid, ["--claim", row.ref])
      if (!claimed) continue
      const accepted = await bounded(client.prompt({ path: { id: sid }, body: {
        noReply: true, parts: [{ type: "text", text: claimed.text }],
      }}))
      const message = sdkResponseData(accepted)
      if (message?.info?.sessionID === sid && message.info.role === "user"
          && typeof message.info.id === "string" && message.info.id
          && promptText(message) === claimed.text) {
        pendingPeerCommand(sid, ["--queued", claimed.ref, "--message-id", message.info.id])
      }
    }
  } catch { /* Inflight/ambiguous payload remains unverified; no resend loop. */ }
  finally { pendingPeerBusy.delete(sid) }
}

function promptText(output) {
  if (typeof output?.message?.content === "string") return output.message.content
  if (!Array.isArray(output?.parts)) return ""
  return output.parts
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n")
}

// The inherited caller name and the other harnesses' session identity variables, blanked in
// OpenCode tool commands. The worker marker AGENT_DISPATCH_CURRENT_HARNESS is left alone.
const inheritedIdentityEnv = [
  "AGENT_DISPATCH_CALLER_HARNESS",
  "CLAUDE_CODE_SESSION_ID",
  "CLAUDE_SESSION_ID",
  "CLAUDECODE",
  "CLAUDE_CODE_CHILD_SESSION",
  "CODEX_THREAD_ID",
  "CODEX_SESSION_ID",
]

export const AgentHarnessGuards = async (ctx) => {
  // Record plugin-load marker once per plugin init. In a headless dispatch the
  // runtime child inherits OPENCODE_DISPATCH_SLUG, so this proves the plugin
  // was loaded by the headless runtime (dispatch-liveness.py inspects it).
  markPluginLoaded(dispatchSlug())
  peerIdentityLog(ctx, "plugin", "plugin-registered")
  registerPaneContext(ctx)
  const completionCarrier = createCompletionCarrier(ctx)

  return ({
  dispose: () => { completionCarrier.dispose(); retirePaneContext(ctx) },
  event: async ({ event }) => {
    if (event && event.type === "session.compacted") {
      collectCard("compact", (event.properties && event.properties.sessionID) || "", baseDir(ctx))
      runWorkerState("compact-after", event)
      forgetShownCandidates(event.properties && event.properties.sessionID)
    }
    // session.idle fires after each turn (the session is waiting for the user).
    // It refreshes the summary and pane, starts the cycle checkpoint observation and
    // touches the heartbeat; memory has no
    // idle or session-end step (it exchanges after writes and reads, D-82/D-83).
    if (event && event.type === "session.status") {
      completionCarrier.status(event.properties?.sessionID || "", event.properties?.status?.type || "")
    }
    if (event && event.type === "session.idle") {
      const eventSid = (event.properties && event.properties.sessionID) || ""
      observePaneCallback(ctx, "session.idle", eventSid)
      if (!isWorkerSession()) {
        spawnSummary(eventSid, "final")
        await projectPane(eventSid, ctx)
      }
      spawnCheckpoint(eventSid)
      await pendingPeerDelivery(ctx, eventSid)
      completionCarrier.idle(eventSid).catch(() => {})
      // Liveness side-channel: touch the heartbeat for the active dispatch slug
      // so dispatch-liveness.py can detect stale/crashed headless sessions even
      // when the OpenCode SQLite session mtime is inconclusive.
      touchHeartbeat(dispatchSlug())
    }
    if (event && event.type === "session.deleted") {
      const sid = (event.properties && event.properties.sessionID) || ""
      if (sid) {
        spawnSummary(sid, "final")
        completionCarrier.forget(sid)
        promptBySession.delete(sid)
        turnBySession.delete(sid)
        memoryBySession.delete(sid)
        localEvidenceBySession.delete(sid)
        turnContextBySession.delete(sid)
        cardBySession.delete(sid)
        paneProjectionRetryAt.delete(sid)
        invalidatePaneOrigin(sid)
      }
    }
  },
  "chat.message": async (input, output) => {
    observePaneCallback(ctx, "chat.message", input.sessionID || output?.message?.sessionID || "")
    if (isWorkerSession()) return
    const eventSid = input.sessionID || output?.message?.sessionID || ""
    const sid = eventSid || "opencode-plugin"
    spawnSummary(eventSid, "initial")
    await projectPane(eventSid, ctx)
    const prompt = promptText(output)
    const turn = input.messageID || output?.message?.id || ""
    if (prompt) promptBySession.set(sid, prompt)
    // Actual peer receipt is observed after persistence, not callback rendering.
    if (prompt && eventSid && prompt.includes("peer-from:")) {
      // Ordinary manual sends retain their existing notice path. Pending refs
      // are acknowledged by the persisted-context observation at normal idle.
      const rows = pendingPeerCommand(eventSid)
      if (Array.isArray(rows) && !rows.some((row) => row.text === prompt)) {
        spawnPeerNotice(eventSid, prompt, baseDir(ctx))
      }
    }
    if (turn) turnBySession.set(sid, turn)
    const cardTurn = turn || prompt
    if (eventSid && (!cardTurn || cardBySession.get(sid)?.turn !== cardTurn)) {
      cardBySession.set(sid, { turn: cardTurn, text: collectCard("prompt", eventSid, baseDir(ctx)) })
    }
  },
  "experimental.chat.system.transform": async (input, output) => {
    const sid = input.sessionID || "opencode-plugin"
    const cwd = baseDir(ctx)
    if (isWorkerSession()) {
      // Dispatch prompts own explicit status/prompt-signal bootstrap;
      // memory/briefing/context stay main-only.
      return
    }
    // Every model call re-emits the same blocks. The probe/preflight work still
    // runs once per session (memory, local evidence) or once per user turn
    // (candidates, prompt-signal, briefing) — only the emission repeats, so the
    // caps in core/MEMORY.md are unchanged and no extra process is spawned per
    // tool-loop continuation.
    if (!memoryBySession.has(sid)) {
      memoryBySession.set(sid, collectPreflight("memory", [cwd]))
    }
    appendContext(output, memoryBySession.get(sid))

    if (!localEvidenceBySession.has(sid)) {
      localEvidenceBySession.set(sid, collectPreflight("local-evidence", [cwd]))
    }
    appendContext(output, localEvidenceBySession.get(sid))
    appendContext(output, cardBySession.get(sid)?.text)

    const prompt = promptBySession.get(sid) || ""
    const turn = turnBySession.get(sid) || ""
    const cached = turnContextBySession.get(sid)
    // A turn is new when chat.message recorded a prompt this plugin has not
    // built context for yet. Sessions whose runtime supplies no message ID fall
    // back to the prompt text itself as the turn key.
    const turnKey = turn || prompt
    if (prompt && (!cached || cached.turn !== turnKey)) {
      const blocks = [
        collectCandidates([prompt, cwd, sid, turn]),
        collectPreflight("prompt-signal", [cwd, sid]),
        collectPreflight("briefing", [cwd]),
      ].filter(Boolean)
      turnContextBySession.set(sid, { turn: turnKey, blocks })
    }
    for (const block of turnContextBySession.get(sid)?.blocks || []) {
      appendContext(output, block)
    }
  },
  "experimental.session.compacting": async (input, output) => {
    runWorkerState("compact-before", input || {})
  },
  "shell.env": async (input, output) => {
    // A harness clears the inherited caller name and every other harness's session id from
    // its own tool commands (core/OPERATIONS.md); its own session id is then the only
    // identity evidence and nothing exported here names a harness. The plugin env is merged
    // over the inherited one. Sessionless compiles/binds are a real runtime state (undocumented sessionID,
    // only typed optional upstream), not a theoretical one — never throw here, and set
    // OPENCODE_SESSION_ID only when input.sessionID is present.
    if (!output) return
    if (!output.env) output.env = {}
    for (const key of inheritedIdentityEnv) output.env[key] = ""
    const sid = input && input.sessionID
    if (sid) output.env.OPENCODE_SESSION_ID = sid
    output.env[CARRIER_ENV] = completionCarrier.env(sid) || ""
  },
  // The two kept write gates (hooks/core-write-guard.py): installed release copies
  // and the shared checkout seen from a linked worktree. Anything else is allowed.
  "tool.execute.before": async (input, output) => {
    for (const file of targetFiles(ctx, input.tool || {}, output.args || {})) {
      const result = spawnSync("python3", [coreWriteGuard, "--check", file, "--cwd", baseDir(ctx)], {
        encoding: "utf8",
      })
      if (result.status === 1) throw new Error((result.stdout || "").trim())
    }
    // The route presence gate (utilities/route_presence_gate.py): a session's first source
    // edit, commit or long run in a folder needs a route there. Anything else passes.
    const toolName = typeof input.tool === "string" ? input.tool : input.tool?.name || ""
    if (routeGateTools.has(toolName)) {
      const result = spawnSync("python3", [routePresenceGate, "--opencode"], {
        input: JSON.stringify({ tool: toolName, args: output.args || {}, sessionID: input.sessionID || "", cwd: baseDir(ctx) }),
        encoding: "utf8",
      })
      if (result.status === 1) throw new Error((result.stdout || "").trim())
    }
  },
  "tool.execute.after": async (input, output) => {
    observePaneCallback(ctx, "tool.execute.after", input.sessionID || "")
    if (input.sessionID && typeof output?.output === "string") {
      const receipt = spawnSync("python3", [startReceipt, "--harness", "opencode", "--session-id", input.sessionID],
        { encoding: "utf8", timeout: 1000 })
      if (receipt.status === 0 && receipt.stdout?.trim()) output.output += "\n" + receipt.stdout.trim()
    }
    const args = input.args || output.args || {}
    const files = targetFiles(ctx, input.tool || {}, args)
    for (const file of files) {
      if (isDesignHtml(file)) runPreflight("design", [file])
    }
    await projectPane(input.sessionID || "", ctx, true)
    await pendingPeerDelivery(ctx, input.sessionID || "")
    if (!isWorkerSession() && input.sessionID && typeof output?.output === "string") {
      // The shared sender notice uses this existing model-context boundary.
      // OpenCode's body delivery remains on the native API above.
      const notice = spawnSync("python3", [path.join(root, "utilities", "peer-message.py"),
        "context", "--to-harness", "opencode", "--to-session-id", input.sessionID], {
        cwd: root, env: { ...process.env, AGENT_HOME: root },
        encoding: "utf8", timeout: 1500,
      })
      try {
        const text = JSON.parse(notice.stdout || "{}").context
        if (notice.status === 0 && typeof text === "string" && text) output.output += "\n" + text
      } catch { /* No new turn, prompt input, or callback retry. */ }
    }
    // Record actual spec reads for workflow and display evidence.
    // Non-blocking: a marker failure must never abort a successful read.
    const toolName = typeof input.tool === "string" ? input.tool : input.tool?.name || ""
    if (toolName === "read") {
      const readFile = normalizeFile(ctx, args.filePath || args.path || args.file)
      if (readFile) collectPreflight("read", [readFile, input.sessionID || "opencode-plugin"])
    }
    // The person's reply to a registered frame question is kept for the next start
    // (utilities/frame_native_answer.py). Non-blocking, like the read marker.
    if (toolName === "question") {
      spawnSync("python3", [frameNativeAnswer, "--opencode"], {
        input: JSON.stringify({ tool: toolName, sessionID: input.sessionID || "", args,
                                answers: output?.metadata?.answers || [] }),
        encoding: "utf8",
      })
    }
  },
  })
}
