import path from "node:path"
import { fileURLToPath } from "node:url"
import { AsyncLocalStorage } from "node:async_hooks"
import { spawnSync, spawn } from "node:child_process"
import { existsSync, mkdirSync, writeFileSync, renameSync, utimesSync, openSync, readSync, closeSync, realpathSync, readFileSync, readdirSync } from "node:fs"
import { createHash } from "node:crypto"

if (!process.env.PYTHONPYCACHEPREFIX && !process.env.PYTHONDONTWRITEBYTECODE) {
  const stateRoot = process.env.HARNESS_STATE_ROOT ||
    (process.env.XDG_STATE_HOME && path.join(process.env.XDG_STATE_HOME, "hearting")) ||
    (process.env.HOME && path.join(process.env.HOME, ".local", "state", "hearting"))
  if (stateRoot) process.env.PYTHONPYCACHEPREFIX = path.join(stateRoot, "pycache")
}

const pluginDir = path.dirname(fileURLToPath(import.meta.url))
const pluginRoot = path.resolve(pluginDir, "../../..")
const python3 = existsSync("/usr/bin/python3") ? "/usr/bin/python3" : "python3"
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
const sessionTidyIdle = path.join(root, "utilities", "session_tidy_idle.py")
const fleetInteractionState = path.join(root, "hooks", "fleet-interaction-state.py")
const interactionWrites = new Map()

function publishInteraction(action, sessionID, refs = {}, kind = null) {
  // Observation stays off the native event consumer. Serialize the existing
  // short-lived hook per session so a slow set cannot land after its reply.
  const previous = interactionWrites.get(sessionID) || Promise.resolve()
  const pending = previous.catch(() => {}).then(() => new Promise((resolve) => {
    let timer
    const done = () => { clearTimeout(timer); resolve() }
    try {
      const args = [fleetInteractionState, action, "--harness", "opencode"]
      if (kind) args.push("--kind", kind)
      const child = spawn("python3", args, { stdio: ["pipe", "ignore", "ignore"], detached: true })
      child.on("error", done)
      child.on("close", done)
      child.stdin?.on?.("error", () => {})
      timer = setTimeout(() => { try { child.kill("SIGKILL") } catch {} }, 5000)
      timer.unref?.()
      child.unref()
      child.stdin?.end(JSON.stringify({ session_id: sessionID, ...refs }))
    } catch { done() }
  })).finally(() => {
    if (interactionWrites.get(sessionID) === pending) interactionWrites.delete(sessionID)
  })
  interactionWrites.set(sessionID, pending)
}

const herdrProjection = path.join(root, "tools", "fleet", "herdr_projection.py")
const coreWriteGuard = path.join(root, "hooks", "core-write-guard.py")
const routePresenceGate = path.join(root, "utilities", "route_presence_gate.py")
const contextWindowHelper = path.join(root, "utilities", "opencode_context_window.py")
const nativeContextPolicyVersion = "1.19.0"
let resolvedRuntimeConfig = null
let contextPolicyRuntimeVersion = null
let contextPolicyRequestedWindow = null
const frameNativeAnswer = path.join(root, "utilities", "frame_native_answer.py")
const startReceipt = path.join(root, "utilities", "start_receipt.py")
const routeGateTools = new Set(["write", "edit", "multiedit", "patch", "apply_patch", "bash"])
const designPattern = /(designs?\/|\/design\/|spec\/design|preview\.html$|slides?\.html$|03_components|scaffolds\/)/
const promptBySession = new Map()
const turnBySession = new Map()
const sessionInfoById = new Map()
const compactStartedBySession = new Map()
const compactStatusUntil = new Map()
const runtimeStatusBySession = new Map()
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
const historyNoticeSettledBySession = new Set()
const peerGuidanceBySession = new Map()
// One threshold notice per native session, retained for the triggering turn so
// an auxiliary title call cannot consume the answering model's system context.
const contextAlertBySession = new Map()
const v2SessionInfo = new Map()
const v2WorkerSessions = new Set()
const v2ContractSessions = new Set()
const v2ActiveSession = new AsyncLocalStorage()
let terminalContract = ""
let terminalContractRetryAt = 0

function workerTerminalContract() {
  if (terminalContract || Date.now() < terminalContractRetryAt) return terminalContract
  // Render the short canonical kernel section once, never the full kernel or
  // main lifecycle context on every model request. A failed render is retried
  // later instead of disabling the contract for a long-lived shared service.
  const result = spawnSync(python3, ["-B", "-c",
    "import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.argv[1]) / 'utilities')); from worker_bootstrap import worker_terminal_contract; print(worker_terminal_contract(Path(sys.argv[1])))", root],
  { cwd: root, encoding: "utf8", timeout: 4000 })
  terminalContract = result.status === 0 ? (result.stdout || "").trim() : ""
  if (!terminalContract) terminalContractRetryAt = Date.now() + 60000
  return terminalContract
}

async function sessionWorkerContract(ctx, sid) {
  if (!sid) return ""
  if (v2ActiveSession.getStore()) {
    return v2ContractSessions.has(sid) ? workerTerminalContract() : ""
  }
  if (!isWorkerSession() || process.env.AGENT_DISPATCH_REGISTERED_WORKER !== "1") return ""
  const info = await compactSessionInfo(ctx, sid, true)
  // A native child inherits the private runtime's environment, not its
  // registered parent's terminal task contract.
  return info && !info.parentID ? workerTerminalContract() : ""
}

function compactionWorkerContract(contract) {
  return "Preserve the following worker completion contract verbatim in the summary for " +
    "the continuing worker. Produce the normal compaction summary; this contract applies " +
    "to the worker's final response, not to this summary request.\n\n" + contract
}

async function contextAlert(ctx, sid, output) {
  if (!sid || isWorkerSession() || process.env.MEM_DISTILL === "1"
      || ["owner", "worker", "auxiliary", "reviewer"].includes((process.env.AGENT_SESSION_ROLE || "").toLowerCase())
      || ["compaction", "title", "summary"].includes(output?.message?.agent)
      || (output?.parts?.length && output.parts.every((part) => part.synthetic || part.ignored))) return ""
  const client = ctx.client
  if (typeof client?.session?.get !== "function" || typeof client?.session?.messages !== "function") return ""
  const controller = new AbortController()
  let timer
  try {
    return await Promise.race([(async () => {
      const options = { throwOnError: true, signal: controller.signal }
      const session = sdkResponseData(await client.session.get({ path: { id: sid }, ...options }))
      if (session?.id !== sid || session.parentID) return ""
      const rows = sdkResponseData(await client.session.messages({ path: { id: sid },
        query: { limit: 16 }, ...options }))
      if (controller.signal.aborted || !Array.isArray(rows)) return ""
      const latest = rows.map((row) => row?.info)
        .filter((info) => info?.sessionID === sid && info.role === "assistant" && !info.summary)
        .sort((a, b) => (b.time?.created || 0) - (a.time?.created || 0)
          || String(b.id || "").localeCompare(String(a.id || "")))[0]
      if (!latest?.tokens) return ""
      const result = spawnSync(python3, [path.join(root, "utilities", "context_alert.py")], {
        input: JSON.stringify({ session_id: sid, tokens: latest.tokens }) + "\n",
        encoding: "utf8", timeout: 1000,
      })
      return result.status === 0 ? (result.stdout || "").trim() : ""
    })(), new Promise((resolve) => {
      timer = setTimeout(() => { controller.abort(); resolve("") }, 500)
    })])
  } catch { return "" } finally { clearTimeout(timer) }
}

function showContextAlert(ctx, text) {
  if (!text || typeof ctx.client?.tui?.showToast !== "function") return
  // Official OpenCode SDK UI channel, not a Claude hook object or a new prompt.
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 500)
  try {
    Promise.resolve(ctx.client.tui.showToast({ body: { message: text, variant: "warning", duration: 10000 },
      signal: controller.signal })).catch(() => {}).finally(() => clearTimeout(timer))
  } catch { clearTimeout(timer) }
}

function baseDir(ctx) {
  const sessionID = v2ActiveSession.getStore()
  const location = sessionID && v2SessionInfo.get(sessionID)?.location?.directory
  return location || ctx.worktree || ctx.directory || ctx.location?.directory || process.cwd()
}

// Headless dispatch liveness probe support.
// When the OpenCode runtime starts a headless dispatch via dispatch-headless.py,
// it exports OPENCODE_DISPATCH_SLUG (and the dispatch interpreter passes the
// same env to the runtime child). Recording two artifacts at plugin init gives
// dispatch-liveness.py a secondary, cheap signal independent of the OpenCode
// SQLite session mtime:
//   * <state-root>/plugin-load.<slug>.mark — created once at plugin
//     init, proving the plugin was actually loaded by the headless runtime.
//   * <state-root>/logs/<slug>.heartbeat — touched on every
//     session.idle event (idle == turn done == still alive), so a stale or
//     crashed headless that never reaches idle will have an aging heartbeat.
// <state-root> is the directory of the registry the dispatch exported as
// AGENT_DISPATCH_JOBS, the root dispatch-liveness.py and Fleet read
// (dispatch_contract.dispatch_state_root). AGENT_HOME is the installed release:
// writing there hides both files from those readers and leaves a .dispatch tree
// in the release that keeps it from being pruned once it is superseded.
// Both are best-effort: a plugin must never block a turn because it failed to
// record a liveness side-channel.
function dispatchSlug() {
  return process.env.OPENCODE_DISPATCH_SLUG || ""
}

function dispatchStateRoot() {
  const jobs = process.env.AGENT_DISPATCH_JOBS || ""
  if (!jobs) return ""
  let registry = path.resolve(jobs)
  try {
    registry = realpathSync(registry)
  } catch {
    // not created yet; the unresolved path names the same directory
  }
  const stateRoot = path.dirname(registry)
  return existsSync(stateRoot) ? stateRoot : ""
}

function isWorkerSession() {
  const sessionID = v2ActiveSession.getStore()
  if (sessionID && v2WorkerSessions.has(sessionID)) return true
  return (
    (process.env.AGENT_SESSION_ROLE || "").toLowerCase() === "worker" ||
    process.env.AGENT_DISPATCH_CHILD === "1" ||
    Boolean(process.env.AGENT_DISPATCH_DEPTH) ||
    Boolean(process.env.OPENCODE_DISPATCH_SLUG) ||
    process.env.FLEET_TITLE_REFRESH === "1"
  )
}

function mayReconnectPeerObligations() {
  // Ordinary worker turns keep existing shared recovery available. A
  // registered worker must not start the primary lifecycle runner from a hook.
  return !isWorkerSession() || process.env.AGENT_DISPATCH_REGISTERED_WORKER !== "1"
}

function touchHeartbeat(slug) {
  const stateRoot = dispatchStateRoot()
  if (!slug || !stateRoot) return
  try {
    const logsDir = path.join(stateRoot, "logs")
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
  const stateRoot = dispatchStateRoot()
  if (!slug || !stateRoot) return
  try {
    const marker = path.join(stateRoot, `plugin-load.${slug}.mark`)
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

function specSyncNudgeText(ctx, input, output, harnessRoot) {
  const args = input.args || output.args || {}
  const toolName = typeof input.tool === "string" ? input.tool : input.tool?.name || ""
  if ((toolName !== "edit" && toolName !== "apply_patch") || typeof output?.output !== "string") return Promise.resolve("")
  return new Promise((resolve) => {
    let settled = false
    let stdout = ""
    let child
    let timer
    const finish = (code) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      resolve(code === 0 ? stdout.trim() : "")
    }
    const cwd = typeof args.workdir === "string" ? path.resolve(baseDir(ctx), args.workdir) : baseDir(ctx)
    try {
      child = spawn("bash", [path.join(harnessRoot, "hooks", "spec-sync-nudge.sh"), "--format", "text"], {
        cwd, env: { ...process.env, AGENT_HOME: harnessRoot }, stdio: ["pipe", "pipe", "ignore"],
      })
      child.stdout?.setEncoding("utf8")
      child.stdout?.on("data", (chunk) => { stdout += chunk })
      child.once("error", () => finish(null))
      child.once("close", (code) => finish(code))
      timer = setTimeout(() => {
        try { child.kill("SIGKILL") } catch { /* fail-open timeout */ }
        finish(null)
      }, 8000)
      child.stdin.end(JSON.stringify({ hook_event_name: "PostToolUse", cwd, tool_name: toolName, tool_input: args }))
    } catch {
      finish(null)
    }
  })
}

function appendSpecSyncNudge(ctx, input, output, harnessRoot) {
  return specSyncNudgeText(ctx, input, output, harnessRoot).then((text) => {
    if (text && typeof output?.output === "string") output.output += "\n" + text
  })
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

function spawnIdleObserver(sid, cwd) {
  if (!sid || isWorkerSession()) return
  try {
    const child = spawn("python3", [sessionTidyIdle, "--harness", "opencode", "--detach"], {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root },
      detached: true,
      stdio: ["pipe", "ignore", "ignore"],
    })
    child.on("error", () => {})
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify({ event: "session.idle", session_id: sid, cwd: cwd || root }))
    child.unref()
  } catch {
    // Optional idle observation never delays summary, checkpoint or peer delivery.
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

function runCompactHook(action, payload = {}) {
  try {
    const result = spawnSync("python3", [path.join(root, "utilities", "worker-state-hook.py"),
      action, "--harness", "opencode"], {
      cwd: root, env: { ...process.env, AGENT_HOME: root }, input: JSON.stringify(payload),
      encoding: "utf8", timeout: action === "runtime-status" ? 750 : 4000, killSignal: "SIGKILL",
    })
    return result.status === 0 ? (result.stdout || "").trim() : ""
  } catch { return "" }
}

function observeCompactRuntimeStatus(ctx, sid, status) {
  if (!sid || isWorkerSession() || isCompactSubSession()) return
  const normalized = String(status || "").toLowerCase()
  if (!normalized) return
  const observedAt = Date.now()
  runtimeStatusBySession.set(sid, { status: normalized, observedAt })
  recordNativeTurnStatus(ctx, sid, normalized)
  const until = compactStatusUntil.get(sid) || 0
  if (until <= observedAt) {
    compactStatusUntil.delete(sid)
    return
  }
  const result = runCompactHook("runtime-status", {
    sessionID: sid, cwd: baseDir(ctx), status: normalized, observed_at: observedAt,
  })
  if (result !== "booking=pending") compactStatusUntil.delete(sid)
}

function recordNativeTurnStatus(ctx, sid, status) {
  const pane = process.env.HERDR_PANE_ID || ""
  const endpoint = process.env.AGENT_HERDR_SESSION || "default"
  const origin = paneNativeOrigin
  const binding = paneContexts.get(ctx)
  const generation = paneProjectionGeneration
  if (!sid || isWorkerSession() || isCompactSubSession()
      || !binding?.active || binding.generation !== generation || paneOwningContext !== ctx
      || !origin || origin.state === "invalidated" || !pendingPeerRoot(ctx, sid)
      || !sameNativePaneContext(ctx)) return
  const identity = contextPolicyProcess()
  if (!pane || !identity || !Number.isInteger(identity.pid) || identity.pid <= 1
      || !/^\d+$/.test(String(identity.start || ""))
      || identity.pid !== origin.pid || String(identity.start) !== String(origin.start)) return
  const lower = String(status || "").toLowerCase()
  const normalized = lower === "idle" ? "idle"
    : ["active", "busy", "pending", "retry", "running", "streaming", "working"].includes(lower) ? "working"
      : lower === "blocked" ? "blocked" : "unknown"
  try {
    spawnSync("python3", [path.join(root, "utilities", "herdr_observation.py"), "record-native-turn"], {
      cwd: root, env: { ...process.env, AGENT_HOME: root },
      input: JSON.stringify({ endpoint, pane, harness: "opencode", session_id: sid,
        pid: identity.pid, pid_start: String(identity.start), status: normalized,
        observed_at: Date.now() / 1000 }),
      encoding: "utf8", timeout: 750, killSignal: "SIGKILL",
    })
  } catch {}
}

async function currentCompactRuntimeStatus(ctx, sid) {
  if (typeof ctx.client?.session?.status !== "function") return ""
  const controller = new AbortController()
  let timer
  try {
    const result = await Promise.race([
      ctx.client.session.status({ signal: controller.signal }),
      new Promise((resolve) => { timer = setTimeout(() => { controller.abort(); resolve(null) }, 500) }),
    ])
    const data = sdkResponseData(result)
    const value = data?.[sid] || data?.status?.[sid]
    if (value == null) return ""
    return typeof value === "string" ? value : String(value?.type || "")
  } catch { return "" } finally { clearTimeout(timer) }
}

function compactInfoTime(info) {
  const value = Number(info?.time?.created || info?.time?.updated || 0)
  return value > 0 && value < 1e12 ? value * 1000 : value
}

function completedSummary(rows, sid) {
  if (!Array.isArray(rows)) return null
  return rows.map((row) => row?.info)
    .filter((info) => info?.sessionID === sid && info.role === "assistant" && info.summary === true
      && info.finish && !info.error)
    .sort((a, b) => compactInfoTime(b) - compactInfoTime(a)
      || String(b.id || "").localeCompare(String(a.id || "")))[0] || null
}

async function compactSessionInfo(ctx, sid, parentOptional = false) {
  const known = sessionInfoById.get(sid)
  if (known?.id === sid && (parentOptional || Object.hasOwn(known, "parentID"))) return known
  if (typeof ctx.client?.session?.get !== "function") return null
  const controller = new AbortController()
  let timer
  try {
    const result = await Promise.race([
      ctx.client.session.get({ path: { id: sid }, query: { directory: baseDir(ctx) }, signal: controller.signal }),
      new Promise((resolve) => { timer = setTimeout(() => { controller.abort(); resolve(null) }, 750) }),
    ])
    const info = sdkResponseData(result)
    if (!info || info.id !== sid || (!parentOptional && !Object.hasOwn(info, "parentID"))) return null
    sessionInfoById.set(sid, info)
    return info
  } catch { return null } finally { clearTimeout(timer) }
}

async function compactMessages(ctx, sid) {
  if (typeof ctx.client?.session?.messages !== "function") return null
  const controller = new AbortController()
  let timer
  try {
    return await Promise.race([
      ctx.client.session.messages({ path: { id: sid },
        query: { directory: baseDir(ctx), limit: 64 }, signal: controller.signal }),
      new Promise((resolve) => { timer = setTimeout(() => { controller.abort(); resolve(null) }, 1000) }),
    ]).then(sdkResponseData)
  } catch { return null } finally { clearTimeout(timer) }
}

// A declared sub-session keeps its fail-closed worker ledger flush/re-anchor
// (runWorkerState throws); everything below is the main-session compact tidy.
function isCompactSubSession() {
  return process.env.AGENT_DISPATCH_STAGE_AUTHORITY === "0" || Boolean(process.env.AGENT_DISPATCH_SUBSESSION_ID)
}

async function beforeCompact(ctx, input, output) {
  const sid = input?.sessionID || input?.sessionId || ""
  if (isCompactSubSession()) {
    runWorkerState("compact-before", input || {})
    return
  }
  if (!sid || isWorkerSession()) return
  const info = await compactSessionInfo(ctx, sid)
  if (!info || info.parentID) return
  // The summary message is created after this hook returns (session/compaction.ts),
  // so a completed summary created at or after this time belongs to this compaction.
  compactStartedBySession.set(sid, Date.now())
  const instructions = runCompactHook("compact-before", { ...input, worker: false })
  if (instructions && output && typeof output === "object") {
    if (!Array.isArray(output.context)) output.context = output.context ? [String(output.context)] : []
    output.context.push(instructions)
  }
}

async function afterCompact(ctx, event) {
  const sid = event?.properties?.sessionID || ""
  if (isCompactSubSession()) {
    runWorkerState("compact-after", event || {})
    return
  }
  if (!sid || isWorkerSession()) return
  const started = compactStartedBySession.get(sid)
  compactStartedBySession.delete(sid)
  const info = await compactSessionInfo(ctx, sid)
  // Unknown identity: count the compaction in the ledger only, as before; no card or tidy.
  if (!info) { collectCard("compact", sid, baseDir(ctx)); return }
  if (info.parentID) return
  let summary = ""
  if (started) {
    const rows = await compactMessages(ctx, sid)
    const latest = completedSummary(rows, sid)
    if (latest && compactInfoTime(latest) >= started) {
      summary = (rows.find((row) => row?.info?.id === latest.id)?.parts || [])
        .filter((part) => part?.type === "text" && typeof part.text === "string")
        .map((part) => part.text).join("\n")
      const parentID = String(latest.parentID || "")
      const parent = rows.find((row) => row?.info?.id === parentID)
      const compaction = (parent?.parts || []).find((part) => part?.type === "compaction")
      const auto = typeof compaction?.auto === "boolean" ? compaction.auto
        : typeof compaction?.data?.auto === "boolean" ? compaction.data.auto : null
      const trigger = auto === true ? "auto" : auto === false ? "manual" : "unknown"
      const tailStartID = String(compaction?.tail_start_id || compaction?.data?.tail_start_id || parentID)
      const booking = runCompactHook("compact-after", {
        sessionID: sid, cwd: baseDir(ctx), worker: false, summary, trigger,
        summary_id: String(latest.id || ""), parent_id: parentID,
        tail_start_id: tailStartID, compaction_part_id: String(compaction?.id || ""),
      })
      if (trigger === "auto" && booking === "booking=scheduled") {
        compactStatusUntil.set(sid, Date.now() + 3 * 60 * 60 * 1000)
      }
      const status = compactStatusUntil.get(sid)
      if (status && status > Date.now()) {
        const current = await currentCompactRuntimeStatus(ctx, sid)
        if (current) observeCompactRuntimeStatus(ctx, sid, current)
      }
      return
    }
  }
  runCompactHook("compact-after", { sessionID: sid, cwd: baseDir(ctx), worker: false, summary, trigger: "unknown" })
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

function sameNativePaneContext(ctx) {
  const origin = paneNativeOrigin
  if (!origin || origin.state !== "pending" || origin.pid !== process.pid) return false
  try {
    const stat = readPaneProc("/proc/self/stat", 4096).toString("utf8")
    const split = stat.lastIndexOf(") ")
    const start = stat.slice(split + 2).trim().split(/\s+/)[19]
    return split >= 0 && String(start) === String(origin.start)
      && realpathSync(ctx.directory) === origin.directory
  } catch { return false }
}

let paneNativeOrigin // undefined means not yet read; null is unsupported, never SDK-filled.
const paneContexts = new WeakMap()
let paneOwningContext = null
let panePublisherSlot = null

function registerPaneContext(ctx) {
  if (paneContexts.has(ctx)) return paneContexts.get(ctx)
  if (paneNativeOrigin === undefined) paneNativeOrigin = ownNativePaneOrigin()
  let ownsOrigin = false
  try { ownsOrigin = !!paneNativeOrigin && realpathSync(ctx.directory) === paneNativeOrigin.directory } catch {}
  const binding = { active: true, ownsOrigin, refreshPending: false, originObserved: false,
    generation: null, startupTimer: null, startupQueued: false, startupScheduled: false,
    startupRunning: false }
  paneContexts.set(ctx, binding)
  if (ownsOrigin) {
    if (paneOwningContext) {
      const preserveOrigin = sameNativePaneContext(paneOwningContext) && sameNativePaneContext(ctx)
      retirePaneContext(paneOwningContext, preserveOrigin)
    }
    paneOwningContext = ctx
    paneProjectionGeneration++
    binding.generation = paneProjectionGeneration
    paneProjectionRetryAt.delete(paneNativeOrigin.sid)
  }
  return binding
}

function retirePaneContext(ctx, preserveOrigin = false) {
  const binding = paneContexts.get(ctx)
  if (!binding) return
  if (binding.startupTimer !== null) {
    clearTimeout(binding.startupTimer)
    binding.startupTimer = null
    binding.startupQueued = false
  }
  binding.active = false
  if (paneOwningContext === ctx) {
    paneProjectionGeneration++
    paneOwningContext = null
    if (paneNativeOrigin?.state === "pending" && !preserveOrigin) paneNativeOrigin.state = "invalidated"
  }
  // A live publisher survives disposal. Closing its observer is not child exit.
}

function invalidatePaneOrigin(sid) {
  if (paneNativeOrigin?.sid === sid) {
    const binding = paneOwningContext && paneContexts.get(paneOwningContext)
    if (binding?.startupTimer !== null && binding?.startupTimer !== undefined) {
      clearTimeout(binding.startupTimer)
      binding.startupTimer = null
      binding.startupQueued = false
    }
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

// OpenCode 2's Promise plugin API is distinct from the V1 returned hook map.
// The object shape intentionally exposes both entrypoints: V1 invokes `server`,
// while V2 registers only `setup`. Plugin.define is an identity constructor in
// the pinned V2 implementation, so this default object is its portable result.
async function responsePrefix(response, cap = 64 * 1024) {
  let reader
  let timer
  try {
    reader = response.clone().body?.getReader()
    if (!reader) return new Uint8Array()
    const deadline = new Promise((resolve) => {
      timer = setTimeout(() => resolve({ done: true }), 1000)
    })
    const chunks = []
    let size = 0
    while (size < cap) {
      const { done, value } = await Promise.race([reader.read(), deadline])
      if (done) break
      const part = value.subarray(0, cap - size)
      chunks.push(part)
      size += part.byteLength
      if (part.byteLength !== value.byteLength) break
    }
    const merged = new Uint8Array(size)
    let offset = 0
    for (const part of chunks) { merged.set(part, offset); offset += part.byteLength }
    return merged
  } catch { return new Uint8Array() }
  finally {
    clearTimeout(timer)
    // Cancelling a tee branch may wait for the runtime's original consumer.
    try { void reader?.cancel().catch(() => {}) } catch {}
  }
}

async function setupOpenCodeV2(ctx) {
  const controller = new AbortController()
  const sessionInfo = async (sessionID) => {
    if (!sessionID || typeof ctx.session?.get !== "function") return null
    try {
      const result = await ctx.session.get({ sessionID })
      const value = result?.data ?? result
      if (value && typeof value === "object") {
        v2SessionInfo.set(sessionID, value)
        return value
      }
    } catch {}
    return v2SessionInfo.get(sessionID) || null
  }
  const markSession = async (sessionID, agent) => {
    const info = await sessionInfo(sessionID)
    const title = String(info?.title || "")
    const selectedAgent = String(agent || info?.agent || "")
    if (title.startsWith("hearting-worker-") || selectedAgent.startsWith("hearting-")) {
      v2WorkerSessions.add(sessionID)
    }
    if (title.startsWith("hearting-worker-att-") && !info?.parentID && !info?.parent_id) {
      v2ContractSessions.add(sessionID)
    } else {
      v2ContractSessions.delete(sessionID)
    }
    return info
  }
  const withSession = (sessionID, work) => v2ActiveSession.run(sessionID || "", async () => {
    const info = await markSession(sessionID)
    return work(info)
  })
  // OpenCode 2.0.32 runs tool.execute.before before shell.create.before and
  // inserts OPENCODE_SESSION_ID only after the shell hooks return. Pair the
  // exact command with its session while both are in flight; a command string
  // two sessions are waiting on stays empty (poll-fallback) instead of guessing.
  const shellCommands = new Map()
  const rememberShellCommand = (command, sessionID) => {
    if (!command || !sessionID) return
    const sessions = shellCommands.get(command) || new Set()
    sessions.add(sessionID)
    shellCommands.set(command, sessions)
  }
  const takeShellCommandSession = (command) => {
    if (typeof command !== "string" || !command) return null
    const sessions = shellCommands.get(command)
    if (!sessions) return null
    if (sessions.size !== 1) return ""
    const [sessionID] = sessions
    shellCommands.delete(command)
    return sessionID
  }
  const forgetShellCommand = (command, sessionID) => {
    if (typeof command !== "string" || !command) return
    const sessions = shellCommands.get(command)
    if (!sessions) return
    sessions.delete(sessionID)
    if (!sessions.size) shellCommands.delete(command)
  }
  const sessionPrompt = async ({ path: target, body }) => {
    const sessionID = target?.id
    const text = (body?.parts || []).map((part) => part?.text || "").filter(Boolean).join("\n")
    if (!sessionID || !text || typeof ctx.session?.prompt !== "function") return { error: true }
    try {
      // 2.0.32 PromptInput carries the text at the top level.
      await ctx.session.prompt({ sessionID, text, delivery: "steer" })
      return { error: null, response: { ok: true } }
    } catch (error) {
      return { error, response: { ok: false } }
    }
  }
  const sessionMessages = async ({ path: target, sessionID } = {}) => {
    const sid = sessionID || target?.id
    if (!sid || typeof ctx.session?.context !== "function") return { error: true }
    try {
      const result = await ctx.session.context({ sessionID: sid })
      const rows = result?.data ?? result
      if (result?.error || !Array.isArray(rows)) return { error: true }
      return { data: rows.flatMap((row) => row?.type === "user" && typeof row.text === "string"
        ? [{ info: { id: row.id, sessionID: sid, role: "user", time: row.time },
            parts: [{ type: "text", text: row.text }] }]
        : []), response: { ok: true, status: 200 } }
    } catch { return { error: true } }
  }
  const legacyContext = {
    ...ctx,
    directory: ctx.location?.directory || "",
    client: {
      session: {
        get: async ({ path: target, sessionID }) => {
          // Admission uses a fresh native read, never sessionInfo's display cache.
          const result = await ctx.session.get({ sessionID: sessionID || target?.id })
          if (result?.error) return { error: result.error }
          return { data: result?.data ?? result, response: { ok: true, status: 200 } }
        },
        wait: async ({ sessionID: sid }) => {
          // V2 has no session.status query. Its exact session.wait resolves
          // only after native execution is idle; the common carrier bounds it.
          if (!sid || !pendingPeerRoot(legacyContext, sid)
              || typeof ctx.session?.wait !== "function") throw new Error("native-idle-unavailable")
          const controller = new AbortController()
          const timer = setTimeout(() => controller.abort(), 1000)
          try {
            const result = await ctx.session.wait({ sessionID: sid }, { signal: controller.signal })
            if (result?.error || controller.signal.aborted) throw new Error("native-idle-unavailable")
          } finally { clearTimeout(timer) }
        },
        // V2 exposes the post-compaction user messages as session.context.
        messages: sessionMessages,
        promptAsync: sessionPrompt,
      },
      app: { log: (input) => ctx.app?.log?.(input) },
      tui: { showToast: async () => {} },
      permission: ctx.permission,
      project: ctx.project,
    },
  }
  registerPaneContext(legacyContext)
  const hooks = await AgentHarnessGuards(legacyContext, { initialize: false })

  await ctx.tool.hook("execute.before", async (event) => {
    const command = event.tool === "shell" && typeof event.input?.command === "string"
      ? event.input.command : ""
    if (command) rememberShellCommand(command, event.sessionID)
    try {
      await withSession(event.sessionID, async () => {
        const input = { tool: event.tool, sessionID: event.sessionID }
        const output = { args: event.input || {} }
        await hooks["tool.execute.before"]?.(input, output)
      })
    } catch (error) {
      if (command) forgetShellCommand(command, event.sessionID)
      throw error
    }
  })
  await ctx.tool.hook("execute.after", async (event) => {
    const command = event.tool === "shell" && typeof event.input?.command === "string"
      ? event.input.command : ""
    if (command) forgetShellCommand(command, event.sessionID)
    await withSession(event.sessionID, async () => {
      const content = event.status === "completed" ? event.result?.content : []
      const output = { output: Array.isArray(content)
        ? content.map((part) => part?.text || "").filter(Boolean).join("\n") : "",
        metadata: event.status === "error" ? { exit: 1 } : {} }
      const originalText = output.output
      if (v2WorkerSessions.has(event.sessionID)) {
        await appendSpecSyncNudge(ctx, { tool: event.tool, sessionID: event.sessionID, args: event.input || {} }, output, root)
      } else {
        await hooks["tool.execute.after"]?.(
          { tool: event.tool, sessionID: event.sessionID, args: event.input || {} }, output)
      }
      if (event.status === "completed" && Array.isArray(content) &&
          output.output.startsWith(originalText) && output.output.length > originalText.length) {
        // V2 consumes the native result, not the legacy adapter's temporary string.
        const text = output.output.slice(originalText.length).replace(/^\n/, "")
        event.result = { ...event.result, content: [...content, { type: "text", text }] }
      }
    })
  })
  await ctx.shell.hook("create.before", async (event) => {
    const env = event.env || {}
    const mapped = takeShellCommandSession(event.command)
    const sessionID = mapped === null ? (env.OPENCODE_SESSION_ID || "") : mapped
    return withSession(sessionID, async () => {
      if (v2WorkerSessions.has(sessionID)) {
        for (const key of inheritedIdentityEnv) env[key] = ""
        env[CARRIER_ENV] = ""
        if (sessionID) env.OPENCODE_SESSION_ID = sessionID
        return
      }
      await hooks["shell.env"]?.({ sessionID }, { env })
    })
  })
  await ctx.session.hook("prompt", async (event) => withSession(event.sessionID, async () => {
    if (v2WorkerSessions.has(event.sessionID)) return
    const text = event.prompt?.text || ""
    await hooks["chat.message"]?.(
      { sessionID: event.sessionID, messageID: event.messageID },
      { message: { id: event.messageID, sessionID: event.sessionID, agent: event.agent },
        parts: text ? [{ type: "text", text }] : [] })
  }))
  await ctx.session.hook("context", async (event) => withSession(event.sessionID, async () => {
    if (v2WorkerSessions.has(event.sessionID)) {
      const contract = await sessionWorkerContract(ctx, event.sessionID)
      if (contract && Array.isArray(event.system)
          && !event.system.some((part) => part.type === "text" && part.text === contract)) {
        event.system.push({ type: "text", text: contract })
      }
      return
    }
    const output = { system: [] }
    await hooks["experimental.chat.system.transform"]?.(
      { sessionID: event.sessionID }, output)
    if (Array.isArray(event.system)) {
      for (const text of output.system) {
        if (typeof text === "string" && text) event.system.push({ type: "text", text })
      }
    }
  }))
  await ctx.session.hook("compaction", async (event) => withSession(event.sessionID, async () => {
    if (v2WorkerSessions.has(event.sessionID)) {
      const contract = await sessionWorkerContract(ctx, event.sessionID)
      if (contract && Array.isArray(event.system)) {
        const text = compactionWorkerContract(contract)
        if (!event.system.some((part) => part.type === "text" && part.text === text)) {
          event.system.push({ type: "text", text })
        }
      }
      return
    }
    await hooks["experimental.session.compacting"]?.(
      { sessionID: event.sessionID }, { ...event })
  }))
  await ctx.session.hook("http.response", async (event) => {
    const model = event.model
    const status = event.response?.status
    if (!model?.providerID || !model?.id || !Number.isInteger(status)) return
    // The V2 request's model is exact to this session. Retain only limits the
    // callback actually supplies; V1's compaction formula is not a V2 policy.
    writeContextPolicy({ sessionID: event.sessionID, model, contextObservation: "v2-http-response" }, {})
    // The runtime consumes the original body after this callback returns.
    // Reading/cancelling its clone must not hold that consumer behind telemetry.
    void (async () => {
      const body = status >= 200 && status < 300
        ? new Uint8Array() : await responsePrefix(event.response)
      try {
        const child = spawn("python3", [path.join(root, "utilities", "opencode_runtime.py"),
          "record-provider-response", "--model", `${model.providerID}/${model.id}`,
          "--status", String(status)], {
          cwd: root, env: { ...process.env, AGENT_HOME: root },
          stdio: ["pipe", "ignore", "ignore"],
        })
        child.on("error", () => {})
        child.stdin.on("error", () => {})
        child.stdin.end(body)
      } catch { /* Eligibility telemetry remains best-effort. */ }
    })().catch(() => {})
  })

  void (async () => {
    try {
      for await (const native of ctx.event.subscribe({ signal: controller.signal })) {
        const data = native?.data || native?.properties || {}
        const info = data.info || data.session || null
        const sessionID = data.sessionID || data.form?.sessionID || info?.id || native?.sessionID || ""
        if (!sessionID) continue
        if (native.type === "session.deleted" && v2WorkerSessions.has(sessionID)) {
          v2WorkerSessions.delete(sessionID)
          v2ContractSessions.delete(sessionID)
          v2SessionInfo.delete(sessionID)
          continue
        }
        await withSession(sessionID, async (current) => {
          if (v2WorkerSessions.has(sessionID)) return
          const type = ["session.execution.succeeded", "session.execution.failed",
            "session.execution.interrupted"].includes(native.type) ? "session.idle" : native.type
          const event = { ...native, type, properties: { ...data,
            ...(native.type === "session.execution.started" ? { status: { type: "busy" } } : {}),
            ...(info ? { info } : {}), sessionID } }
          if (native.type === "session.execution.started") event.type = "session.status"
          if (native.type === "form.created") {
            event.type = "question.asked"
            event.properties = { sessionID, id: data.form?.id, source: "opencode-form" }
          } else if (["form.replied", "form.cancelled"].includes(native.type)) {
            event.type = native.type === "form.replied" ? "question.replied" : "question.rejected"
            event.properties = { sessionID, requestID: data.id, source: "opencode-form" }
          }
          await hooks.event?.({ event })
        })
        if (native.type === "session.deleted") v2SessionInfo.delete(sessionID)
      }
    } catch {
      // Event-stream loss is an observation gap; it does not settle a session.
    }
  })()

  return () => {
    controller.abort()
    hooks.dispose?.({ silent: true })
    retirePaneContext(legacyContext)
    v2WorkerSessions.clear()
    v2ContractSessions.clear()
    v2SessionInfo.clear()
  }
}

export default {
  id: "hearting-guards",
  setup: setupOpenCodeV2,
  server: (...args) => AgentHarnessGuards(...args),
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
    // Herdr v13 owns session identity and lifecycle. Hearting validates the
    // exact native selection and refreshes display metadata only.
    const target = resolvePublisherTarget()
    const args = [target.helper, "--harness", "opencode", "--session-id", sid,
      "--no-report-session"]
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
        // Same executable, argv and environment. The synchronous child is
        // bounded and owns this slot.
        const result = spawnSync("python3", args, { ...options, encoding: "utf8",
          timeout: 10000, killSignal: "SIGKILL", maxBuffer: 1024 })
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
    child.on("spawn", () => {
      launched = true
      peerIdentityLog(ctx, "publisher", "publisher-path-success", { sessionID: sid, publisherPath: "async" })
    })
    child.on("exit", () => releasePanePublisher(slot))
    child.on("close", () => releasePanePublisher(slot))
    observePanePublisher(child, ctx, sid, generation, fallback)
    peerIdentityLog(ctx, "publisher", "publisher-spawned", { sessionID: sid,
      sessionLifecycle: "herdr-v13", metadataOnly: true, publisherCreated: created })
    child.unref()
  } catch (error) {
    peerIdentityLog(ctx, "publisher", "publisher-spawn-error", { sessionID: sid, ...publisherErrorFields(error) })
  } finally { if (paneProjectionBusy.get(sid) === binding) paneProjectionBusy.delete(sid) }
}

function scheduleInitialPaneProjection(ctx, binding, sid) {
  const origin = paneNativeOrigin
  if (!sid || !binding?.active || !binding.ownsOrigin || paneOwningContext !== ctx
      || !origin || origin.sid !== sid || origin.state !== "pending"
      || binding.startupScheduled || !process.env.HERDR_PANE_ID || isWorkerSession()) return
  binding.startupScheduled = true
  binding.startupQueued = true
  const generation = paneProjectionGeneration
  const timer = setTimeout(() => {
    if (binding.startupTimer !== timer) return
    binding.startupTimer = null
    binding.startupQueued = false
    if (!binding.active || paneOwningContext !== ctx || binding.generation !== generation
        || generation !== paneProjectionGeneration || paneNativeOrigin !== origin
        || origin.sid !== sid || origin.state !== "pending"
        || !process.env.HERDR_PANE_ID || isWorkerSession()) return
    binding.startupRunning = true
    Promise.resolve().then(() => projectPane(sid, ctx)).catch(() => {}).finally(() => {
      binding.startupRunning = false
    })
  }, 0)
  binding.startupTimer = timer
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
  const [prompt, cwd, sid, turn] = args
  const command = [path.join(root, "hooks", "mem-recall-inject.sh"),
    "--prompt", prompt, "--cwd", cwd, "--session-id", sid,
    "--runtime", "opencode", "--format", "hook-json"]
  if (turn) command.push("--turn-id", turn)
  const result = spawnSync("bash", command, {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root, MEM_CANDIDATE_DEFER_COMMIT: "1" },
    encoding: "utf8",
    timeout: 3000,
    killSignal: "SIGKILL",
  })
  if (result.error || result.status !== 0) return { text: "", pending: null }
  try {
    const value = JSON.parse(result.stdout || "{}")
    const output = value.hookSpecificOutput || {}
    return {
      text: String(output.additionalContext || "").trim(),
      pending: value.deferredCandidateReceipt || null,
    }
  } catch { return { text: "", pending: null } }
}

// The plugin renders the candidate/card text into the system prompt of a model
// call; the helper only prepares its receipt. Commit it here, after the block
// was appended to this call's output, so a lost render leaves the same
// candidates and card generation due for the next prompt. Fail-open.
function commitDeferred(helper, command, pending) {
  if (!pending || isWorkerSession()) return
  try {
    spawnSync("python3", [helper, command], {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root },
      input: JSON.stringify(pending),
      encoding: "utf8",
      timeout: 3000,
      killSignal: "SIGKILL",
    })
  } catch {}
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
const CARRIER_RECIPIENT_CACHE_MS = 10000
const CARRIER_NEGATIVE_CACHE_MS = 1200
const sweepRel = path.join("utilities", "dispatch_session_sweep.py")
const OWED_STATES = new Set(["pending", "claimed", "sent-ambiguous"])

// One bounded sweep step; any failure is null. Ordinary steps are asynchronous;
// the last effect check uses the same operation synchronously before the write.
function carrierCommand(action, sid, handed, observe = () => {}, synchronous = false) {
  if (synchronous) {
    // At the effect boundary, a Promise response would leave another event-loop
    // turn in which the returned authority could become stale. Reuse the same
    // settle operation synchronously, then call promptAsync without an await.
    const target = resolveRuntimeTarget(sweepRel)
    try {
      const result = spawnSync("python3", [target.helper, action, "--recipient-kind", CARRIER_KIND,
        "--session", sid || ""], { cwd: target.root,
        env: { ...process.env, AGENT_HOME: target.root }, input: JSON.stringify(handed || {}),
        encoding: "utf8", timeout: 10000, killSignal: "SIGKILL", maxBuffer: Infinity })
      return !result.error && result.status === 0 ? JSON.parse(result.stdout || "null") : null
    } catch { return null }
  }
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
  const recipientCache = new Map()
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
    if (!sid) return false
    const now = Date.now()
    const current = await command("recipients", sid)
    let cached = recipientCache.get(sid)
    if (current && typeof current.generation === "string" && Array.isArray(current.digests)) {
      if (!cached || cached.generation !== current.generation || now >= cached.expiresAt) {
        cached = {
          generation: current.generation,
          digests: current.digests.filter(value => /^[0-9a-f]{64}$/.test(value)),
          expiresAt: now + CARRIER_RECIPIENT_CACHE_MS,
          negativeUntil: 0,
        }
        recipientCache.set(sid, cached)
      }
    } else if (!cached || now >= cached.expiresAt) {
      log("skip", sid, "lineage-unavailable")
      return false
    }
    if (!cached || now < cached.negativeUntil) return false
    for (const base of roots) {
      for (const digest of cached.digests) {
        let names = []
        try { names = readdirSync(path.join(base, "pending-delivery", digest)) } catch { continue }
        for (const name of names) {
          if (!name.startsWith("delivery-") || !name.endsWith(".json")) continue
          try {
            const record = JSON.parse(readFileSync(path.join(base, "pending-delivery", digest, name), "utf8"))
            if (record?.recipient_kind === CARRIER_KIND && OWED_STATES.has(record.state)) {
              cached.negativeUntil = 0
              return true
            }
          } catch {}
        }
      }
    }
    cached.negativeUntil = now + CARRIER_NEGATIVE_CACHE_MS
    return false
  }

  async function deliver(sid, look = true) {
    if (!sid) return
    if (busy.has(sid)) { log("skip", sid, "delivery-in-flight"); return }
    if (!available()) { log("skip", sid, isWorkerSession() ? "worker-session" : "promptAsync-unavailable"); return }
    const paused = () => ["busy", "retry", "blocked", "working"].includes(status.get(sid))
      || ["busy", "retry", "blocked", "working"].includes(pendingPeerStatus.get(sid))
    if (paused()) {
      log("skip", sid, `session-${status.get(sid)}`); return
    }
    const origin = paneNativeOrigin
    const client = prompter()
    const current = () => available() && sessions.has(sid) && !paused()
      && prompter() === client && paneNativeOrigin === origin && pendingPeerRoot(ctx, sid)
    // Use the peer carrier's exact root and native status interpretation. An
    // empty complete status map is idle; unavailable or malformed is unknown.
    const nativeIdle = async () => {
      if (!current() || typeof client?.get !== "function"
          || (typeof client?.status !== "function" && typeof client?.wait !== "function")) return false
      let nativeTimeout
      const bounded = async call => {
        try {
          return await Promise.race([call, new Promise((_, reject) => {
            nativeTimeout = setTimeout(() => reject(new Error("carrier-context-timeout")), 1000)
          })])
        } finally { clearTimeout(nativeTimeout) }
      }
      try {
        const info = pendingPeerSdkData(await bounded(client.get({
          path: { id: sid }, query: { directory: origin.directory },
        })))
        if (!current() || !pendingPeerIsNativeRoot(info, sid, origin)) return false
        if (typeof client.wait === "function") {
          await bounded(client.wait({ sessionID: sid }))
          return current()
        }
        const result = await bounded(client.status({ query: { directory: origin.directory } }))
        return current() && pendingPeerNativeStatusType(pendingPeerSdkData(result), sid) === "idle"
      } catch { return false }
    }
    busy.add(sid)
    let timeout
    try {
      if (look && !(await owed(sid))) { log("skip", sid, "no-owed-record"); return }
      if (!(await nativeIdle()) || !current()) { log("skip", sid, "native-admission-withheld"); return }
      // The shared courier checks native user-message evidence before claim.
      // This read is scoped to the one session and its bounded recent history.
      const beforeMessages = await compactMessages(ctx, sid)
      if (!Array.isArray(beforeMessages)) { log("skip", sid, "native-history-unavailable"); return }
      const claimed = await command("deliver", sid, { messages: beforeMessages })
      if (!claimed?.text || !Array.isArray(claimed.records) || !claimed.records.length) {
        log("skip", sid, claimed === null ? "claim-unavailable" : "nothing-claimed"); return
      }
      log("claim", sid, "claimed", { deliveries: claimed.records.map(record => record.delivery_id) })
      const canSend = await nativeIdle()
      // A second native read is the final output/send recheck. If another
      // carrier's exact receipt appeared after our first read, settle it here
      // and never submit this copy to promptAsync. Keep this claim/receipt
      // recheck after the native admission awaits, including when admission
      // failed: historical exact receipts still settle without a new send.
      const finalMessages = await compactMessages(ctx, sid)
      let beforeSend = await command("settle", sid, {
        records: claimed.records, messages: finalMessages, text: claimed.text,
      })
      if (!beforeSend) {
        await command("release", sid, { records: claimed.records })
        log("settle", sid, "native-recheck-unavailable-released")
        return
      }
      if (!beforeSend.records?.length) {
        log("settle", sid, "native-receipt-before-send", {
          received: beforeSend?.received_ids?.length ?? null,
          acked: beforeSend?.settled_count ?? null,
        })
        return
      }
      if (beforeSend.unknown_ids?.length && beforeSend.received_ids?.length) {
        log("settle", sid, "partial-native-observation-held", {
          received: beforeSend.received_ids.length, unknown: beforeSend.unknown_ids.length,
        })
        return
      }
      if (!beforeSend.text) {
        await command("release", sid, { records: beforeSend.records })
        log("settle", sid, "empty-context-released")
        return
      }
      // Revalidate after the asynchronous helper response too. No native read
      // follows this transport-boundary check; exact receipts still settle first.
      beforeSend = carrierCommand("settle", sid, {
        records: claimed.records, messages: finalMessages, text: claimed.text,
      }, (reason, extra) => log("command", sid, reason, extra), true)
      if (!beforeSend) {
        await command("release", sid, { records: claimed.records })
        log("settle", sid, "effect-boundary-unavailable-released")
        return
      }
      if (!beforeSend?.text || !beforeSend.records?.length || beforeSend.unknown_ids?.length) {
        log("settle", sid, "effect-boundary-withheld")
        return
      }
      // No await between this last local/identity check and promptAsync.
      // Only the remaining claims validated by settle may be released here.
      if (!canSend || !Array.isArray(finalMessages) || !current()) {
        await command("release", sid, { records: beforeSend.records })
        log("skip", sid, "final-admission-withheld-released")
        return
      }
      let taken = false
      let promptReason = "not-accepted"
      let httpStatus
      try {
        const result = await Promise.race([
          client.promptAsync({ path: { id: sid },
            body: { parts: [{ type: "text", text: beforeSend.text }] } }),
          new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error("carrier-timeout")), 5000) }),
        ])
        taken = Boolean(result) && result.error == null && result.response?.ok !== false
        httpStatus = result?.response?.status
        promptReason = taken ? "accepted" : "not-accepted"
      } catch (error) { promptReason = error?.message === "carrier-timeout" ? "timeout" : "error" }
      log("prompt", sid, promptReason, { httpStatus })
      // SDK acceptance and process completion are not receipt evidence. Settle
      // only if this exact marker now exists in this native session's user
      // history; otherwise release the claim for the existing fallback.
      const observed = await command("settle", sid, {
        records: beforeSend.records, messages: await compactMessages(ctx, sid), text: beforeSend.text,
      })
      const received = new Set(observed?.received_ids || [])
      const releaseRecords = (beforeSend.records || []).filter(
        record => !received.has(record.delivery_id))
      const released = releaseRecords.length
        ? await command("release", sid, { records: releaseRecords }) : { count: 0 }
      log("settle", sid, received.size ? "native-receipt" : "release-fallback", {
        received: received.size, acked: observed?.settled_count ?? null,
        released: released?.count ?? null, prompt_accepted: taken,
      })
    } catch (error) {
      log("skip", sid, "delivery-error", { error: String(error?.message || error).slice(0, 180) })
    } finally {
      clearTimeout(timeout)
      busy.delete(sid)
    }
  }

  return {
    async activate(sid) {
      if (!sid || !available()) return false
      const result = await command("activate", sid)
      if (result === null) return false
      sessions.add(sid)
      // The existing timer and idle callback deliver the retained receipt;
      // session creation by itself never acknowledges it.
      this.env(sid)
      return true
    },
    // The env a tool command of `sid` carries: only a runtime that will carry the receipt says so.
    env(sid) {
      if (!sid || !available()) return null
      sessions.add(sid)
      if (!timer) {
        timer = setInterval(() => {
          for (const id of sessions) {
            deliver(id).catch(() => {})
            if (status.get(id) === "idle") pendingPeerDelivery(ctx, id).catch(() => {})
          }
        }, CARRIER_INTERVAL_MS)
        timer.unref?.()
      }
      log("env", sid, "carrier-armed")
      return `${CARRIER_KIND}:${sid}`
    },
    status(sid, type) { if (sid && type) status.set(sid, type) },
    // A finished turn also looks past its own records (a seat handover stores them elsewhere).
    async idle(sid) { if (!sid) return; status.set(sid, "idle"); if (sessions.has(sid)) await deliver(sid, false) },
    forget(sid) { sessions.delete(sid); status.delete(sid) },
    dispose(silent = false) { if (!silent) log("dispose", "", "context-disposed", { sessions: [...sessions] }); if (timer) clearInterval(timer); timer = null; sessions.clear() },
  }
}

// Session card / tidy notice: `session_tidy.py hook` records the event and prints
// what this session is due, once. Every failure is empty (no card, no throw).
function collectCard(event, sid, cwd, prompt = "") {
  if (!sid || isWorkerSession()) return { text: "", pending: null }
  const result = spawnSync("python3", [sessionTidy, "hook", "--harness", "opencode",
    "--event", event, "--session-id", sid, "--cwd", cwd, "--format", "json"], {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root },
    input: JSON.stringify({ prompt }),
    encoding: "utf8",
    timeout: 4000,
    killSignal: "SIGKILL",
  })
  if (result.error || result.status !== 0) return { text: "", pending: null }
  try {
    const value = JSON.parse(result.stdout || "{}")
    return {
      text: String(value.additionalContext || "").trim(),
      pending: value.deferredConsumed || null,
    }
  } catch { return { text: "", pending: null } }
}

function collectCardPayload(event, sid, cwd, prompt = "") {
  if (!sid || isWorkerSession()) return { additionalContext: "", humanNotice: "" }
  const result = spawnSync("python3", [sessionTidy, "hook", "--harness", "opencode",
    "--event", event, "--session-id", sid, "--cwd", cwd, "--format", "json", "--human-only"], {
    cwd: root, env: { ...process.env, AGENT_HOME: root }, input: JSON.stringify({ prompt }),
    encoding: "utf8", timeout: 4000, killSignal: "SIGKILL",
  })
  if (result.error || result.status !== 0) return { additionalContext: "", humanNotice: "" }
  try {
    const value = JSON.parse(result.stdout || "{}")
    return { additionalContext: String(value.additionalContext || ""), humanNotice: String(value.humanNotice || "") }
  } catch { return { additionalContext: "", humanNotice: "" } }
}

function showHistoryNotice(ctx, sid, notice) {
  if (!notice || !sid || historyNoticeSettledBySession.has(sid)) return
  historyNoticeSettledBySession.add(sid)
  showContextAlert(ctx, notice)
}

function collectHistoryNotice(ctx, sid, cwd, prompt = "", settleEmpty = false) {
  if (!sid || isWorkerSession() || historyNoticeSettledBySession.has(sid)) return
  const payload = collectCardPayload("start", sid, cwd, prompt)
  showHistoryNotice(ctx, sid, payload.humanNotice)
  if (settleEmpty) historyNoticeSettledBySession.add(sid)
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
  if (!sid || !prompt || (!prompt.includes("peer-from:") && !prompt.includes("runtime-from:"))) return
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
  if (result.status !== 0) return null
  try { return JSON.parse(result.stdout || "null") } catch { return null }
}

function pendingPeerTransition(sid, options) {
  if (!sid) return false
  const result = spawnSync("python3", [path.join(root, "utilities", "peer-message.py"),
    "pending", "--to-harness", "opencode", "--to-session-id", sid, ...options], {
    cwd: root, env: { ...process.env, AGENT_HOME: root }, encoding: "utf8", timeout: 1500,
  })
  return result.status === 0
}

function pendingPeerProcessIdentity() {
  try {
    const stat = readFileSync("/proc/self/stat", "utf8")
    const split = stat.lastIndexOf(") ")
    const start = stat.slice(split + 2).trim().split(/\s+/)[19]
    return split >= 0 && /^\d+$/.test(start) ? { pid: process.pid, start } : null
  } catch { return null }
}

function reconnectPeerObligations() {
  let lifecycleRoot = pluginRoot
  try { lifecycleRoot = realpathSync(pluginRoot) } catch {}
  const child = spawn("python3", [path.join(root, "utilities", "peer-steward.py"),
    "__ensure-obligations", "--lifecycle-root", lifecycleRoot], {
    cwd: root, env: { ...process.env, AGENT_HOME: root },
    stdio: "ignore", detached: true,
  })
  child.on("error", () => {})
  child.unref()
}

const pendingPeerBusy = new Set()
const pendingPeerStatus = new Map()
const pendingPeerTraceAt = new Map()

function pendingPeerTrace(ctx, sid, stage, reason, extra = {}) {
  const key = `${sid}:${stage}:${reason}`
  const now = Date.now()
  if (now < (pendingPeerTraceAt.get(key) || 0)) return
  if (pendingPeerTraceAt.size >= 256) pendingPeerTraceAt.delete(pendingPeerTraceAt.keys().next().value)
  pendingPeerTraceAt.set(key, now + 60000)
  const traceReason = `native-peer-delivery:${stage}:${reason}`
  peerIdentityLog(ctx, "native-peer-delivery", traceReason, {
    module: "hearting-native-peer-delivery", diagnostic: traceReason,
    sessionID: sid, stage, delivery_reason: reason, ...extra,
  })
}

function pendingPeerSdkMeta(result, sid) {
  const hasData = !!result && typeof result === "object" && Object.hasOwn(result, "data")
  const data = hasData ? result.data : undefined
  const info = data?.info
  return {
    response_ok: result?.response?.ok === true,
    response_status: Number.isFinite(result?.response?.status) ? result.response.status : null,
    sdk_error_name: typeof result?.error?.name === "string" ? result.error.name : null,
    data_present: hasData,
    data_type: Array.isArray(data) ? "array" : data === null ? "null" : typeof data,
    data_count: Array.isArray(data) ? data.length : undefined,
    data_key_count: data && typeof data === "object" && !Array.isArray(data)
      ? Object.keys(data).length : undefined,
    message_role: typeof info?.role === "string" ? info.role : undefined,
    message_session_match: typeof info?.sessionID === "string" ? info.sessionID === sid : undefined,
    message_id_present: typeof info?.id === "string" && !!info.id,
    part_types: Array.isArray(data?.parts)
      ? [...new Set(data.parts.filter((part) => typeof part?.type === "string").map((part) => part.type))]
      : undefined,
  }
}

function pendingPeerRoot(ctx, sid) {
  const origin = paneNativeOrigin
  const binding = paneContexts.get(ctx)
  if (!binding?.active || !binding.ownsOrigin || paneOwningContext !== ctx
      || !origin || origin.state === "invalidated") return false
  if (origin.sid !== sid && !(origin.bare && tuiSelectionRead(sid))) return false
  try { return realpathSync(ctx.directory) === origin.directory } catch { return false }
}

function pendingPeerRootBlockReason(ctx, sid) {
  const origin = paneNativeOrigin
  const binding = paneContexts.get(ctx)
  if (!origin) return "origin-unavailable"
  if (!binding?.active) return "binding-inactive"
  if (!binding.ownsOrigin) return "binding-not-owner"
  if (paneOwningContext !== ctx) return "context-not-owner"
  if (origin.state === "invalidated") return "origin-invalidated"
  if (origin.sid !== sid) {
    if (!origin.bare) return "argv-session-mismatch"
    if (!tuiSelectionRead(sid)) return "tui-selection-mismatch"
  }
  try {
    if (realpathSync(ctx.directory) !== origin.directory) return "directory-mismatch"
  } catch { return "directory-unavailable" }
  return "owned"
}

function pendingPeerSdkData(result) {
  if (!result || typeof result !== "object" || result.error != null
      || result.response?.ok !== true || result.response.status !== 200
      || !Object.hasOwn(result, "data")) return null
  return result.data
}

function pendingPeerSdkNoContent(result) {
  return !!result && typeof result === "object" && result.error == null
    && result.response?.ok === true && result.response.status === 204
}

function pendingPeerIsNativeRoot(info, sid, origin) {
  if (!info || typeof info !== "object" || Array.isArray(info)
      || info.id !== sid || typeof info.projectID !== "string"
      || !info.time
      || !Number.isFinite(info.time.created) || !Number.isFinite(info.time.updated)
      || (info.parentID !== undefined && info.parentID !== null)) return false
  const directory = info.location?.directory
  if (typeof directory === "string") {
    // V2 Session.Info uses location, with optional title and no slug/version.
    if (info.title !== undefined && typeof info.title !== "string") return false
    try { return realpathSync(directory) === origin.directory } catch { return false }
  }
  if (typeof info.directory !== "string" || typeof info.slug !== "string"
      || typeof info.title !== "string" || typeof info.version !== "string") return false
  try { return realpathSync(info.directory) === origin.directory } catch { return false }
}

function pendingPeerNativeStatusType(statuses, sid) {
  if (!statuses || typeof statuses !== "object" || Array.isArray(statuses)) return null
  const valid = (value) => value && typeof value === "object" && !Array.isArray(value)
    && (value.type === "idle" || value.type === "busy"
      || (value.type === "retry" && Number.isFinite(value.attempt)
        && typeof value.message === "string" && Number.isFinite(value.next)))
  if (!Object.entries(statuses).every(([id, value]) => id && valid(value))) return null
  const current = statuses[sid]
  return current === undefined ? "idle" : current.type
}

function nativeMessageTime(info) {
  const value = Number(info?.time?.created)
  return Number.isFinite(value) && value > 0 ? (value < 1e12 ? value * 1000 : value) : 0
}

async function pendingPeerDelivery(ctx, sid) {
  const inFlight = !!sid && pendingPeerBusy.has(sid)
  const worker = isWorkerSession()
  if (!sid || inFlight || worker) {
    if (sid && (paneNativeOrigin?.sid === sid || paneNativeOrigin?.bare)) pendingPeerTrace(ctx, sid, "guard", "inactive-session", {
      in_flight: inFlight, worker_session: worker,
    })
    return "inactive-session"
  }
  const client = ctx.client?.session
  if (!pendingPeerRoot(ctx, sid)) {
    const origin = paneNativeOrigin
    if (origin?.sid === sid || origin?.bare) {
      const binding = paneContexts.get(ctx)
      let directoryMatches = false
      try { directoryMatches = realpathSync(ctx.directory) === origin.directory } catch {}
      pendingPeerTrace(ctx, sid, "guard", `origin-${pendingPeerRootBlockReason(ctx, sid)}`, {
        origin_state: origin.state, origin_bare: origin.bare,
        origin_sid_match: origin.sid === sid,
        tui_selection_record_valid: origin.bare ? !!tuiSelectionRead(sid) : null,
        origin_pid_matches_process: origin.pid === process.pid,
        origin_birth_valid: /^\d+$/.test(String(origin.start || "")),
        binding_active: !!binding?.active, binding_owns_origin: !!binding?.ownsOrigin,
        owning_context_match: paneOwningContext === ctx, directory_matches: directoryMatches,
      })
    }
    return "root-or-status-blocked"
  }
  if (pendingPeerStatus.get(sid) === "blocked" || pendingPeerStatus.get(sid) === "working") {
    if (paneNativeOrigin?.sid === sid) pendingPeerTrace(ctx, sid, "guard", "locally-blocked", {
      local_status: pendingPeerStatus.get(sid),
    })
    return "root-or-status-blocked"
  }
  if (typeof client?.messages !== "function" || typeof client?.prompt !== "function"
      || typeof client?.promptAsync !== "function" || typeof client?.get !== "function"
      || typeof client?.status !== "function") {
    if (paneNativeOrigin?.sid === sid) pendingPeerTrace(ctx, sid, "guard", "native-api-unavailable", {
      has_status: typeof client?.status === "function", has_get: typeof client?.get === "function",
      has_messages: typeof client?.messages === "function", has_prompt: typeof client?.prompt === "function",
      has_prompt_async: typeof client?.promptAsync === "function",
    })
    return "native-api-unavailable"
  }
  const origin = paneNativeOrigin
  const directory = origin.directory
  pendingPeerBusy.add(sid)
  let timer
  const bounded = async (call, timeoutMs = 1000) => {
    try {
      return await Promise.race([call, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("peer-context-timeout")), timeoutMs)
      })])
    } finally { clearTimeout(timer) }
  }
  try {
    const rows = pendingPeerCommand(sid)
    if (!Array.isArray(rows)) {
      pendingPeerTrace(ctx, sid, "pending", "pending-read-unavailable")
      return "pending-read-unavailable"
    }
    if (!rows.length) {
      pendingPeerTrace(ctx, sid, "pending", "no-pending-rows")
      return "no-pending-rows"
    }
    pendingPeerTrace(ctx, sid, "pending", "rows-found", { row_count: rows.length })
    const rootResponse = await bounded(client.get({
      path: { id: sid }, query: { directory },
    }))
    const rootInfo = pendingPeerSdkData(rootResponse)
    const rootValid = pendingPeerIsNativeRoot(rootInfo, sid, origin)
    pendingPeerTrace(ctx, sid, "native-root", rootValid ? "verified" : "rejected", {
      ...pendingPeerSdkMeta(rootResponse, sid),
      native_id_match: rootInfo?.id === sid,
      native_directory_match: typeof rootInfo?.directory === "string"
        && (() => { try { return realpathSync(rootInfo.directory) === origin.directory } catch { return false } })(),
      parent_field_present: !!rootInfo && Object.hasOwn(rootInfo, "parentID"),
      parent_type: rootInfo?.parentID === null ? "null" : typeof rootInfo?.parentID,
      required_fields_present: !!rootInfo && ["id", "projectID", "directory", "slug", "title", "version", "time"]
        .every((key) => Object.hasOwn(rootInfo, key)),
    })
    if (!rootValid) return "not-root-session"
    if (!pendingPeerRoot(ctx, sid)) return "native-origin-changed"
    const status = await bounded(client.status({ query: { directory } }))
    const statusData = pendingPeerSdkData(status)
    const currentType = pendingPeerNativeStatusType(statusData, sid)
    pendingPeerTrace(ctx, sid, "native-status", currentType || "unproven", {
      ...pendingPeerSdkMeta(status, sid),
      target_present: !!statusData && Object.hasOwn(statusData, sid),
      status_count: statusData && typeof statusData === "object" ? Object.keys(statusData).length : null,
      resolved_status: currentType,
    })
    if (!currentType) return "session-status-unavailable"
    if (currentType !== "idle") {
      pendingPeerStatus.set(sid, currentType === "blocked" ? "blocked" : "working")
      return "not-idle:" + String(currentType || "unknown")
    }
    if (!pendingPeerRoot(ctx, sid)) return "native-origin-changed"
    pendingPeerStatus.set(sid, "idle")
    const response = await bounded(client.messages({ path: { id: sid }, query: { directory } }))
    const messages = pendingPeerSdkData(response)
    pendingPeerTrace(ctx, sid, "native-history", Array.isArray(messages) ? "read" : "unavailable", {
      ...pendingPeerSdkMeta(response, sid), message_count: Array.isArray(messages) ? messages.length : null,
    })
    if (!Array.isArray(messages)) return "history-unavailable"
    if (!pendingPeerRoot(ctx, sid)) return "native-origin-changed"
    const dispatchReceiptIds = (text) => Array.from(String(text || "").matchAll(
      /HEARTING_RECEIPT_V1\s+(delivery-[A-Za-z0-9._-]{1,128})\s+[0-9a-f]{64}/g),
    match => match[1])
    const expectedDispatchMessage = (row) => {
      const binding = row?.dispatch_notice
      if (!binding || binding.carrier !== CARRIER_KIND || binding.recipient_sid !== sid
          || typeof row.text !== "string"
          || ![binding.delivery_id, binding.storage_recipient, binding.claim_owner,
            binding.duty_id, binding.jobs, binding.attempt_id].every(value => typeof value === "string" && value)
          || !dispatchReceiptIds(row.text).includes(binding.delivery_id)
          || !Array.isArray(row.refs) || !row.refs.includes(binding.duty_id)
          || !binding.duty_id.startsWith("registered-batch-")
          || binding.claim_owner !== `retained-rewake:${binding.duty_id}`
          || !path.isAbsolute(binding.jobs)) return null
      return { text: row.text, ...binding }
    }
    const expectedNativeMessages = rows.map(expectedDispatchMessage).filter(Boolean)
    const nativeReceiptCheck = async (nativeRows, expected = []) => {
      if (!Array.isArray(nativeRows)) return new Set()
      const result = await carrierCommand("settle", sid, {
        messages: nativeRows, expected_native_messages: expected,
      })
      return new Set(Array.isArray(result?.received_ids) ? result.received_ids : [])
    }
    const alreadyReceived = await nativeReceiptCheck(messages, expectedNativeMessages)
    const recheckReceipt = async (row) => {
      const expected = expectedDispatchMessage(row)
      if (!expected) return false
      let latest
      try {
        latest = pendingPeerSdkData(await bounded(client.messages({
          path: { id: sid }, query: { directory },
        })))
      } catch { return false }
      if (!Array.isArray(latest) || !pendingPeerRoot(ctx, sid)) return false
      const received = await nativeReceiptCheck(latest, [expected])
      return received.has(expected.delivery_id)
    }
    for (const row of rows.slice(0, 3)) {
      if (!row || typeof row.ref !== "string" || !row.ref || typeof row.text !== "string") continue
      const expectedDispatch = expectedDispatchMessage(row)
      if (expectedDispatch && alreadyReceived.has(expectedDispatch.delivery_id)) {
        pendingPeerTrace(ctx, sid, "dispatch-receipt", "already-received", {
          refs: [row.ref],
        })
        continue
      }
      const created = Number(row.created) * 1000
      const matches = messages.filter((m) => m?.info?.sessionID === sid && m.info.role === "user"
        && promptText(m) === row.text
        && (!row.no_trailer || (nativeMessageTime(m.info) > 0
          && nativeMessageTime(m.info) >= created - 2000)))
      if (matches.length > 1) continue // Ambiguous history is never an exact receipt.
      if (matches.length === 1) {
        const mid = matches[0].info.id
        if (!mid || (row.actual_message_id && row.actual_message_id !== mid)) continue
        const consumed = messages.some((m) => m?.info?.sessionID === sid && m.info.role === "assistant"
          && m.info.parentID === mid && typeof m.info.time?.completed === "number" && !m.info.error)
        if (consumed) {
          pendingPeerCommand(sid, ["--received", row.ref, "--message-id", mid])
          continue
        }
        if (!row.actual_message_id) {
          if (!pendingPeerTransition(sid, ["--queued", row.ref, "--message-id", mid])) continue
        }
        // Continue the already persisted noReply message by its native ID. The
        // exact text is never submitted a second time after an ambiguous result.
        if (!pendingPeerRoot(ctx, sid)) return "native-origin-changed"
        if (await recheckReceipt(row)) {
          pendingPeerTrace(ctx, sid, "dispatch-receipt", "received-before-resume", {
            refs: [row.ref], message_id_present: !!mid,
          })
          continue
        }
        if (!pendingPeerTransition(sid, ["--resuming", row.ref, "--message-id", mid])) continue
        const resumed = await bounded(client.promptAsync({ path: { id: sid }, query: { directory },
          body: { messageID: mid, parts: [] } }))
        const acceptedResume = pendingPeerSdkNoContent(resumed)
        pendingPeerTrace(ctx, sid, "promptAsync-resume", acceptedResume ? "204-accepted" : "not-accepted", {
          ...pendingPeerSdkMeta(resumed, sid), message_id_present: !!mid, empty_parts: true,
        })
        if (acceptedResume) {
          pendingPeerStatus.set(sid, "submitted")
        }
        continue
      }
      if (row.state !== "pending") continue
      const identity = pendingPeerProcessIdentity()
      if (!identity) return "claim-unavailable"
      const reserveStarted = performance.now()
      const reserved = pendingPeerCommand(sid, ["--reserve", row.ref,
        "--claim-pid", String(identity.pid), "--claim-start", identity.start])
      const reserveMs = performance.now() - reserveStarted
      pendingPeerTrace(ctx, sid, "reserve", reserved ? "reserved" : "failed", {
        elapsed_ms: reserveMs, timeout_ms: 1500,
      })
      if (!reserved) return "claim-unavailable"
      const releaseReservation = () => pendingPeerTransition(sid, ["--release-reservation", row.ref,
        "--claim-token", reserved.rpc_claim?.token || "",
        "--claim-pid", String(identity.pid), "--claim-start", identity.start])
      if (reserved.ref !== row.ref) { releaseReservation(); return "claim-ref-mismatch" }
      if (reserved.state !== "pending") { releaseReservation(); return "claim-state-mismatch" }
      if (reserved.text !== row.text) { releaseReservation(); return "claim-body-mismatch" }
      if (!pendingPeerRoot(ctx, sid)) { releaseReservation(); return "native-origin-changed" }
      if (await recheckReceipt(reserved)) {
        releaseReservation()
        pendingPeerTrace(ctx, sid, "dispatch-receipt", "received-before-write", {
          refs: [row.ref],
        })
        continue
      }
      const beginStarted = performance.now()
      const claimed = pendingPeerCommand(sid, ["--begin-write", row.ref,
        "--claim-token", reserved.rpc_claim?.token || "",
        "--claim-pid", String(identity.pid), "--claim-start", identity.start])
      const beginMs = performance.now() - beginStarted
      pendingPeerTrace(ctx, sid, "begin-write", claimed ? "authorized" : "failed", {
        elapsed_ms: beginMs, timeout_ms: 1500,
      })
      if (!claimed || claimed.ref !== row.ref || claimed.state !== "unverified"
          || claimed.rpc_claim?.token !== reserved.rpc_claim?.token) {
        releaseReservation()
        continue
      }
      let accepted
      let persistenceResponseLost = false
      try {
        accepted = await bounded(client.prompt({ path: { id: sid }, query: { directory }, body: {
          noReply: true, parts: [{ type: "text", text: claimed.text }],
        }}), 5000)
      } catch (error) {
        accepted = null
        persistenceResponseLost = error?.response?.ok !== false
        pendingPeerTrace(ctx, sid, "noReply-save", persistenceResponseLost ? "response-lost" : "request-failed", {
          error_name: typeof error?.name === "string" ? error.name : null,
          response_status: Number.isFinite(error?.response?.status) ? error.response.status : null,
          response_ok: error?.response?.ok === true,
        })
      }
      const saved = pendingPeerSdkData(accepted)
      let savedInfo = saved?.info?.sessionID === sid && saved.info.role === "user"
        && typeof saved.info.id === "string" && saved.info.id
        && promptText(saved) === claimed.text ? saved : null
      pendingPeerTrace(ctx, sid, "noReply-save", savedInfo ? "persisted-response-verified" : "persisted-response-unverified", {
        ...pendingPeerSdkMeta(accepted, sid),
        user_session_match: saved?.info?.sessionID === sid,
        user_role_match: saved?.info?.role === "user",
        user_id_present: typeof saved?.info?.id === "string" && !!saved.info.id,
      })
      if (!savedInfo) {
        if (!persistenceResponseLost) continue
        // A lost response is reconciled from persisted exact history before the
        // same ref can be continued. An unresolved claim is never resubmitted.
        let latest
        try { latest = pendingPeerSdkData(await bounded(client.messages({ path: { id: sid }, query: { directory } }))) } catch { latest = null }
        const reconciled = Array.isArray(latest) ? latest.filter((m) => m?.info?.sessionID === sid
          && m.info.role === "user" && promptText(m) === claimed.text
          && (!claimed.no_trailer || (nativeMessageTime(m.info) > 0
            && nativeMessageTime(m.info) >= Number(claimed.created) * 1000 - 2000))) : []
        pendingPeerTrace(ctx, sid, "noReply-reconcile", reconciled.length === 1 ? "one-exact-history-match" : "not-exact", {
          history_response_valid: Array.isArray(latest), match_count: reconciled.length,
        })
        if (reconciled.length !== 1 || !reconciled[0]?.info?.id) continue
        savedInfo = reconciled[0].info
      }
      const mid = savedInfo.info ? savedInfo.info.id : savedInfo.id
      if (typeof mid !== "string" || !mid) continue
      if (!pendingPeerRoot(ctx, sid)) return "native-origin-changed"
      if (pendingPeerTransition(sid, ["--queued", row.ref, "--message-id", mid])) {
        if (pendingPeerTransition(sid, ["--resuming", row.ref, "--message-id", mid])) {
          if (!pendingPeerRoot(ctx, sid)) return "native-origin-changed"
          const resumed = await bounded(client.promptAsync({ path: { id: sid }, query: { directory },
            body: { messageID: mid, parts: [] } }))
          const acceptedResume = pendingPeerSdkNoContent(resumed)
          pendingPeerTrace(ctx, sid, "promptAsync-resume", acceptedResume ? "204-accepted" : "not-accepted", {
            ...pendingPeerSdkMeta(resumed, sid), message_id_present: !!mid, empty_parts: true,
          })
          if (acceptedResume) {
            pendingPeerStatus.set(sid, "submitted")
          }
        }
      }
    }
    return "processed"
  } catch (error) {
    pendingPeerTrace(ctx, sid, "delivery", "delivery-error", {
      error_name: typeof error?.name === "string" ? error.name : null,
      response_status: Number.isFinite(error?.response?.status) ? error.response.status : null,
    })
    return "delivery-error" /* Inflight/ambiguous payload remains unverified; no resend loop. */
  }
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

function contextPolicyProcess() {
  try {
    const pid = process.pid
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8")
    const close = stat.lastIndexOf(") ")
    if (close < 0) return null
    const start = stat.slice(close + 2).trim().split(/\s+/)[19]
    const own = Number.isInteger(pid) && pid > 1 && /^\d+$/.test(start || "") ? { pid, start } : null
    const origin = paneNativeOrigin
    if (!isWorkerSession() && origin && origin.state !== "invalidated"
        && Number.isInteger(origin.pid) && origin.pid > 1 && /^\d+$/.test(String(origin.start || ""))) {
      return { pid: origin.pid, start: String(origin.start) }
    }
    return own
  } catch { return null }
}

function detectContextPolicyRuntimeVersion() {
  if (typeof process.env.OPENCODE_VERSION === "string" && process.env.OPENCODE_VERSION) {
    return process.env.OPENCODE_VERSION
  }
  try {
    const result = spawnSync(process.execPath, ["--version"], {
      encoding: "utf8", timeout: 1000, maxBuffer: 4096,
    })
    if (result.status !== 0) return null
    return (String(result.stdout || "").match(/(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)/) || [])[1] || null
  } catch { return null }
}

function contextPolicyFromChat(input, output) {
  const model = input?.model || {}
  const limit = model.limit && typeof model.limit === "object" ? model.limit : {}
  const compaction = resolvedRuntimeConfig?.compaction
    && typeof resolvedRuntimeConfig.compaction === "object" ? resolvedRuntimeConfig.compaction : {}
  const context = Number.isSafeInteger(limit.context) && limit.context >= 0 ? limit.context : null
  const inputLimit = Number.isSafeInteger(limit.input) && limit.input >= 0 ? limit.input : null
  const configuredReserved = compaction.reserved
  const reservedValid = configuredReserved === undefined || configuredReserved === null
    || Number.isSafeInteger(configuredReserved) && configuredReserved >= 0
  const outputAllowance = Number.isSafeInteger(output?.maxOutputTokens) && output.maxOutputTokens >= 0
    ? output.maxOutputTokens
    : Number.isSafeInteger(limit.output) && limit.output >= 0 ? limit.output : null
  const requestText = typeof contextPolicyRequestedWindow === "string"
    ? contextPolicyRequestedWindow.slice(0, 128) : null
  const requestTokens = requestText !== null && /^\d+$/.test(requestText)
    ? Number(requestText) : null
  const policy = {
    state: "unknown", source: "opencode-native-chat-params",
    runtime_version: contextPolicyRuntimeVersion,
    configured_request_tokens: Number.isSafeInteger(requestTokens) ? requestTokens : null,
    configured_request: requestText,
    effective_window_tokens: context,
    applied_trigger_tokens: null,
    output_allowance_tokens: outputAllowance,
    compaction_reserve_tokens: null,
    disabled: null, enabled: null, enforced: null,
    raw_percentage: null, session_id: input?.sessionID || null,
    attempt_id: process.env.AGENT_DISPATCH_ATTEMPT_ID || null,
    reason: "native-policy-input-unavailable",
  }
  if (input?.contextObservation === "v2-http-response") {
    policy.source = "opencode-native-v2-http-response"
    policy.reason = context === null ? "native-model-window-not-observed" : "native-consumer-version-unverified"
    return policy
  }
  if (contextPolicyRuntimeVersion !== nativeContextPolicyVersion) {
    policy.reason = "native-consumer-version-unverified"
    return policy
  }
  if (compaction.auto === false || context === 0) {
    return { ...policy, state: "disabled", disabled: true, enabled: false, enforced: false,
      reason: "native-auto-compaction-disabled" }
  }
  if (compaction.auto !== undefined && compaction.auto !== true || !reservedValid) {
    policy.reason = "native-compaction-config-invalid"
    return policy
  }
  policy.enabled = true
  const configuredReserveTokens = Number.isSafeInteger(configuredReserved)
    ? configuredReserved : null
  let effectiveReserve = null
  let trigger = null
  if (inputLimit !== null && inputLimit > 0) {
    effectiveReserve = configuredReserveTokens !== null ? configuredReserveTokens
      : outputAllowance !== null ? Math.min(20_000, outputAllowance) : null
    if (effectiveReserve !== null) trigger = Math.max(0, inputLimit - effectiveReserve)
  } else if (context !== null && outputAllowance !== null) {
    effectiveReserve = outputAllowance
    trigger = Math.max(0, context - outputAllowance)
  }
  if (trigger === null) {
    policy.reason = "native-output-or-input-limit-unavailable"
    return policy
  }
  return { ...policy, state: "applied", enabled: true, enforced: true, disabled: false,
    applied_trigger_tokens: trigger, compaction_reserve_tokens: effectiveReserve, reason: null }
}

function writeContextPolicy(input, output) {
  try {
    const sid = input?.sessionID
    const own = contextPolicyProcess()
    if (typeof sid !== "string" || !sid || !own) return
    const env = process.env || {}
    const stateRoot = env.FLEET_SESSION_REGISTRY_DIR
      ? path.dirname(path.resolve(env.FLEET_SESSION_REGISTRY_DIR))
      : path.join(env.XDG_STATE_HOME || path.join(env.HOME || "", ".local", "state"), "agent-fleet")
    if (!stateRoot || stateRoot === "." || !path.isAbsolute(stateRoot)) return
    const directory = path.join(stateRoot, "context-policy", "opencode")
    mkdirSync(directory, { recursive: true, mode: 0o700 })
    const attemptID = env.AGENT_DISPATCH_ATTEMPT_ID || null
    const name = attemptID ? `attempt-${attemptID}.json` : `${own.pid}-${own.start}.json`
    if (!/^[A-Za-z0-9._-]{1,180}\.json$/.test(name)) return
    const target = path.join(directory, name)
    const temporary = `${target}.tmp-${own.pid}`
    const policy = contextPolicyFromChat(input, output)
    const payload = { schema: "hearting-context-policy-v1", harness: "opencode",
      session_id: sid, pid: own.pid, proc_start: own.start, attempt_id: attemptID,
      model: `${input?.model?.providerID || input?.provider?.info?.id || ""}/${input?.model?.id || ""}`,
      observed_at: Date.now() / 1000, policy }
    writeFileSync(temporary, JSON.stringify(payload), { encoding: "utf8", mode: 0o600 })
    renameSync(temporary, target)
  } catch { /* Display telemetry must never delay or block a native model request. */ }
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

export const AgentHarnessGuards = async (ctx, options = {}) => {
  const initialize = options.initialize !== false
  // Record plugin-load marker once per plugin init. In a headless dispatch the
  // runtime child inherits OPENCODE_DISPATCH_SLUG, so this proves the plugin
  // was loaded by the headless runtime (dispatch-liveness.py inspects it).
  if (initialize) {
    markPluginLoaded(dispatchSlug())
    peerIdentityLog(ctx, "plugin", "plugin-registered")
  }
  const paneBinding = initialize ? registerPaneContext(ctx) : { ownsOrigin: false }
  const completionCarrier = createCompletionCarrier(ctx)
  // Restoring an existing -s/--session TUI does not emit session.created.
  // Reuse the process-bound selector; a directory/SDK root cannot pick a recipient.
  if (initialize && paneBinding.ownsOrigin && paneNativeOrigin?.sid) {
    let activated = false
    try { activated = await completionCarrier.activate(paneNativeOrigin.sid) } catch {}
    if (!activated && mayReconnectPeerObligations()) reconnectPeerObligations()
    scheduleInitialPaneProjection(ctx, paneBinding, paneNativeOrigin.sid)
  } else if (initialize && mayReconnectPeerObligations()) {
    reconnectPeerObligations()
  }

  return ({
  dispose: (settings = {}) => {
    completionCarrier.dispose(settings.silent === true)
    if (initialize) retirePaneContext(ctx)
  },
  async config(input) {
    const config = input && typeof input === "object" ? input : {}
    if (contextPolicyRuntimeVersion === null) {
      contextPolicyRuntimeVersion = detectContextPolicyRuntimeVersion()
    }
    resolvedRuntimeConfig = config
    const child = spawnSync(python3, [contextWindowHelper], {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root },
      input: JSON.stringify({ config }) + "\n",
      encoding: "utf8",
      timeout: 2000,
      maxBuffer: 4 * 1024 * 1024,
    })
    if (child.status !== 0) {
      process.stderr.write(`hearting: context window config hook unavailable (${child.error?.code || child.status || "unknown"})\n`)
      return config
    }
    try {
      const result = JSON.parse(child.stdout || "{}")
      contextPolicyRequestedWindow = typeof result?.context_window_request === "string"
        ? result.context_window_request : null
      const projected = result?.config?.provider
      if (!projected || typeof projected !== "object") return config
      if (!config.provider || typeof config.provider !== "object") config.provider = {}
      for (const [providerID, provider] of Object.entries(projected)) {
        if (!provider || typeof provider.models !== "object") continue
        if (!config.provider[providerID]) config.provider[providerID] = provider
        else {
          const targetProvider = config.provider[providerID]
          if (!targetProvider.models || typeof targetProvider.models !== "object") targetProvider.models = {}
          for (const [modelID, model] of Object.entries(provider.models)) {
            if (!targetProvider.models[modelID]) targetProvider.models[modelID] = model
            else if (model?.limit && typeof model.limit === "object") {
              targetProvider.models[modelID].limit = {
                ...(targetProvider.models[modelID].limit || {}), ...model.limit,
              }
            }
          }
        }
      }
      return config
    } catch {
      process.stderr.write("hearting: context window config hook returned invalid data\n")
      return config
    }
  },
  "chat.params": async (input, output) => {
    writeContextPolicy(input, output)
  },
  event: async ({ event }) => {
    if (event && event.type === "session.created") {
      const info = event.properties?.info
      const createdID = info?.id || event.properties?.sessionID || ""
      if (createdID && info && typeof info === "object") sessionInfoById.set(createdID, info)
      if (createdID && !isWorkerSession()) {
        collectHistoryNotice(ctx, createdID, baseDir(ctx))
      }
    }
    if (event && event.type === "session.created") {
      const sid = event.properties?.info?.id || event.properties?.sessionID || ""
      let activated = false
      if (!isWorkerSession() && sid) {
        try { activated = await completionCarrier.activate(sid) } catch {}
      }
      if (!activated && mayReconnectPeerObligations()) reconnectPeerObligations()
    }
    if (event && event.type === "session.compacted") {
      const sid = event.properties?.sessionID || ""
      await afterCompact(ctx, event)
      // The compacted session no longer holds this turn's card or candidates.
      turnContextBySession.delete(sid)
      cardBySession.delete(sid)
      forgetShownCandidates(sid)
    }
    // session.idle fires after each turn (the session is waiting for the user).
    // It refreshes the summary and pane, starts the cycle checkpoint observation and
    // touches the heartbeat; memory has no
    // idle or session-end step (it exchanges after writes and reads, D-82/D-83).
    if (event && event.type === "session.status") {
      const statusSid = event.properties?.sessionID || ""
      const statusType = event.properties?.status?.type || ""
      if (statusSid && statusType) pendingPeerStatus.set(statusSid, String(statusType).toLowerCase())
      completionCarrier.status(statusSid, statusType)
      observeCompactRuntimeStatus(ctx, statusSid, statusType)
    }
    if (event && ["question.asked", "permission.asked"].includes(event.type)) {
      const sid = event.properties?.sessionID || ""
      if (sid) { pendingPeerStatus.set(sid, "blocked"); completionCarrier.status(sid, "blocked") }
      observeCompactRuntimeStatus(ctx, sid, "blocked")
      if (sid && !isWorkerSession()) {
        publishInteraction("set", sid, { request_id: event.properties?.id, source: event.properties?.source },
          event.type === "permission.asked" ? "permission" : "decision")
      }
    }
    if (event && ["question.replied", "question.rejected", "permission.replied"].includes(event.type)) {
      const sid = event.properties?.sessionID || ""
      if (sid) { pendingPeerStatus.set(sid, "working"); completionCarrier.status(sid, "working") }
      observeCompactRuntimeStatus(ctx, sid, "working")
      if (sid && !isWorkerSession()) {
        publishInteraction("clear", sid, { request_id: event.properties?.requestID })
      }
    }
    if (event && event.type === "session.idle") {
      const eventSid = (event.properties && event.properties.sessionID) || ""
      if (eventSid) pendingPeerStatus.set(eventSid, "idle")
      observeCompactRuntimeStatus(ctx, eventSid, "idle")
      observePaneCallback(ctx, "session.idle", eventSid)
      const exactCurrentRoot = eventSid && pendingPeerRoot(ctx, eventSid)
      if (exactCurrentRoot && paneNativeOrigin?.bare) completionCarrier.env(eventSid)
      if (eventSid && (exactCurrentRoot || paneNativeOrigin?.bare)) {
        const binding = paneContexts.get(ctx)
        pendingPeerTrace(ctx, eventSid, "idle-event", exactCurrentRoot
          ? "delivery-evaluation-started" : `origin-${pendingPeerRootBlockReason(ctx, eventSid)}`, {
          origin_bare: paneNativeOrigin?.bare === true,
          origin_sid_match: paneNativeOrigin?.sid === eventSid,
          origin_pid_matches_process: paneNativeOrigin?.pid === process.pid,
          origin_birth_valid: /^\d+$/.test(String(paneNativeOrigin?.start || "")),
          tui_selection_record_valid: paneNativeOrigin?.bare ? !!tuiSelectionRead(eventSid) : null,
          binding_active: !!binding?.active, binding_owns_origin: !!binding?.ownsOrigin,
          owning_context_match: paneOwningContext === ctx,
        })
      }
      if (!isWorkerSession()) {
        spawnIdleObserver(eventSid, baseDir(ctx))
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
        if (!isWorkerSession()) {
          const child = spawn("python3", [fleetInteractionState, "clear", "--harness", "opencode"], {
            stdio: ["pipe", "ignore", "ignore"], detached: true })
          child.on("error", () => {})
          child.unref()
          child.stdin?.end(JSON.stringify({ session_id: sid }))
        }
        spawnSummary(sid, "final")
        completionCarrier.forget(sid)
        promptBySession.delete(sid)
        turnBySession.delete(sid)
        memoryBySession.delete(sid)
        localEvidenceBySession.delete(sid)
        turnContextBySession.delete(sid)
        cardBySession.delete(sid)
        historyNoticeSettledBySession.delete(sid)
        peerGuidanceBySession.delete(sid)
        contextAlertBySession.delete(sid)
        paneProjectionRetryAt.delete(sid)
        compactStatusUntil.delete(sid)
        runtimeStatusBySession.delete(sid)
        pendingPeerStatus.delete(sid)
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
    peerGuidanceBySession.delete(sid)
    // Actual peer receipt is observed after persistence, not callback rendering.
    if (prompt && eventSid && (prompt.includes("peer-from:") || prompt.includes("runtime-from:"))) {
      const guidance = spawnSync(
        "python3",
        ["-c", "import importlib.util, json, sys; s=importlib.util.spec_from_file_location('peer', 'utilities/peer-message.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); d=json.load(sys.stdin); print(m.receiver_guidance(d.get('prompt', ''), d.get('recipient'), cwd=d.get('cwd')))"],
        { cwd: root, env: { ...process.env, AGENT_HOME: root }, encoding: "utf8", timeout: 4000,
          input: JSON.stringify({ prompt, recipient: { harness: "opencode", session_id: eventSid }, cwd: baseDir(ctx) }) })
      if (guidance.status === 0) peerGuidanceBySession.set(sid, (guidance.stdout || "").trim())
      // Ordinary manual sends retain their existing notice path. Pending refs
      // are acknowledged by the persisted-context observation at normal idle.
      const rows = pendingPeerCommand(eventSid)
      if (Array.isArray(rows) && !rows.some((row) => row.text === prompt)) {
        spawnPeerNotice(eventSid, prompt, baseDir(ctx))
      }
    }
    if (turn) turnBySession.set(sid, turn)
    const alertTurn = turn || prompt
    if (eventSid && contextAlertBySession.get(sid)?.turn !== alertTurn) {
      const text = await contextAlert(ctx, eventSid, output)
      contextAlertBySession.set(sid, { turn: alertTurn, text })
      showContextAlert(ctx, text)
    }
    const cardTurn = turn || prompt
    if (eventSid && (!cardTurn || cardBySession.get(sid)?.turn !== cardTurn)) {
      const cardResult = collectCard("prompt", eventSid, baseDir(ctx), prompt)
      collectHistoryNotice(ctx, eventSid, baseDir(ctx), prompt, true)
      cardBySession.set(sid, { turn: cardTurn, text: cardResult.text,
                               pending: cardResult.pending })
    }
  },
  "experimental.chat.messages.transform": async (_input, output) => {
    if (!isWorkerSession() || !Array.isArray(output.messages)) return
    const index = output.messages.findLastIndex((row) => row.info?.role === "user")
    const row = output.messages[index]
    const sid = row?.info?.sessionID
    const contract = await sessionWorkerContract(ctx, sid)
    if (!contract || !Array.isArray(row?.parts)
        || row.parts.some((part) => part.type === "text" && part.text === contract)) return
    // V1's system hook also runs for title and compaction model calls and
    // carries no request kind. Decorate only the primary transcript instead.
    // Replace the row/parts rather than mutating a user message shared with an
    // auxiliary title request. Native persistence is untouched.
    output.messages[index] = { ...row, parts: [...row.parts, {
      id: "prt_heartingterminal", sessionID: sid, messageID: row.info.id,
      type: "text", text: contract, synthetic: true,
    }] }
  },
  "experimental.chat.system.transform": async (input, output) => {
    const sid = input.sessionID || "opencode-plugin"
    const cwd = baseDir(ctx)
    if (isWorkerSession()) {
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
    appendContext(output, peerGuidanceBySession.get(sid))
    appendContext(output, contextAlertBySession.get(sid)?.text)

    const prompt = promptBySession.get(sid) || ""
    const turn = turnBySession.get(sid) || ""
    const cached = turnContextBySession.get(sid)
    // A turn is new when chat.message recorded a prompt this plugin has not
    // built context for yet. Sessions whose runtime supplies no message ID fall
    // back to the prompt text itself as the turn key.
    const turnKey = turn || prompt
    if (prompt && (!cached || cached.turn !== turnKey)) {
      const candidates = collectCandidates([prompt, cwd, sid, turn])
      const blocks = [
        candidates.text,
        collectPreflight("prompt-signal", [cwd, sid]),
        collectPreflight("briefing", [cwd]),
      ].filter(Boolean)
      turnContextBySession.set(sid, { turn: turnKey, blocks,
                                      pending: candidates.pending })
    }
    for (const block of turnContextBySession.get(sid)?.blocks || []) {
      appendContext(output, block)
    }
    // Commit only after this call's output exists. A later failure leaves the
    // same material due next prompt; the helper never wrote it itself.
    const turnContext = turnContextBySession.get(sid)
    if (turnContext?.pending) {
      turnContextBySession.set(sid, { ...turnContext, pending: null })
      commitDeferred(path.join(root, "tools", "memory", "mem.py"),
                     "_apply-candidate-receipt", turnContext.pending)
    }
    const cardState = cardBySession.get(sid)
    if (cardState?.pending) {
      cardBySession.set(sid, { ...cardState, pending: null })
      commitDeferred(sessionTidy, "_apply-deferred", cardState.pending)
    }
  },
  "experimental.session.compacting": async (input, output) => {
    const contract = await sessionWorkerContract(ctx, input?.sessionID)
    if (contract) {
      if (!Array.isArray(output.context)) output.context = []
      const text = compactionWorkerContract(contract)
      if (!output.context.includes(text)) output.context.push(text)
    }
    await beforeCompact(ctx, input || {}, output)
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
    const workerSession = isWorkerSession()
    // Start the general-session helper before the existing synchronous post-tool work.
    // Its text is appended once at the end, after all other output additions.
    const specSyncNudge = workerSession ? null : specSyncNudgeText(ctx, input, output, root)
    if (workerSession) await appendSpecSyncNudge(ctx, input, output, root)
    observePaneCallback(ctx, "tool.execute.after", input.sessionID || "")
    const args = input.args || output.args || {}
    const toolName = typeof input.tool === "string" ? input.tool : input.tool?.name || ""
    const nativeExit = output?.metadata?.exit
    const instanceDir = typeof ctx.directory === "string" && path.isAbsolute(ctx.directory)
      ? ctx.directory : ""
    if (toolName === "bash" && typeof args.command === "string" && instanceDir &&
        Number.isInteger(nativeExit) && nativeExit > 0 && nativeExit <= 128 &&
        typeof output?.output === "string") {
      const cwd = typeof args.workdir === "string"
        ? path.resolve(instanceDir, args.workdir) : instanceDir
      const payload = {
        tool_name: toolName,
        tool_input: { command: args.command },
        cwd,
        tool_response: { exit: nativeExit, output: output.output },
      }
      const diagnosis = spawnSync("python3", [path.join(root, "utilities", "git_index_lock.py"),
        "--harness", "opencode"], {
        cwd: root, env: { ...process.env, AGENT_HOME: root },
        input: JSON.stringify(payload), encoding: "utf8", timeout: 2200,
      })
      try {
        const context = JSON.parse(diagnosis.stdout || "{}").diagnosis?.message
        if (diagnosis.status === 0 && typeof context === "string" && context &&
            typeof output.output === "string") output.output += "\n" + context
      } catch { /* Preserve the completed shell result if diagnosis is unavailable. */ }
    }
    if (input.sessionID && typeof output?.output === "string") {
      const receipt = spawnSync("python3", [startReceipt, "--harness", "opencode", "--session-id", input.sessionID],
        { encoding: "utf8", timeout: 1000 })
      if (receipt.status === 0 && receipt.stdout?.trim()) output.output += "\n" + receipt.stdout.trim()
    }
    const files = targetFiles(ctx, input.tool || {}, args)
    for (const file of files) {
      if (isDesignHtml(file)) runPreflight("design", [file])
    }
    await projectPane(input.sessionID || "", ctx, true)
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
    if (specSyncNudge && typeof output?.output === "string") {
      const text = await specSyncNudge
      if (text) output.output += "\n" + text
    }
  },
  })
}
