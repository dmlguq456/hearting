// Hearting TUI identity provenance — target-only TUI entry, never server.
//
// Reads this TUI's actual current session selection from the native route and
// records it in a scoped runtime-state file. The server identity publisher
// verifies that record against the same process lifecycle (pid + start) and
// the exact session id before its own SDK check; this entry publishes nothing
// itself: no identity report, no peer write, no client calls. Routes without
// a selected session (home, custom, unknown) and teardown remove the record,
// so a blank home never claims an identity.
//
// Plain JS (no type annotations) so the regression fixture can execute it
// outside the TUI host; Bun loads it as a target-only module.

import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs"
import path from "node:path"
import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"

const PLUGIN_ID = "hearting.tui-identity"
const RECORD_SCHEMA = "hearting-tui-selection-v1"
const RECORD_SUBDIR = ["hearting", "tui-identity"]
// Verified SDK v2 event names (packages/sdk/js v2 gen types, pinned 1.18.34).
// Bus traffic is wake-assist only: native navigation itself emits no bus
// event (create waits ~50ms before route.navigate; select navigates
// directly), so handlers below only re-read the live native getter. The
// primary observation is the host-driven slot render further down, which
// runs synchronously with the mounted route.
const REFRESH_EVENTS = ["session.created", "session.deleted", "tui.session.select",
  "session.next.prompted", "message.updated", "session.status", "session.idle"]

function cleanSid(value) {
  if (typeof value !== "string" || !value || value.length > 256 || /[\x00-\x1f\x7f]/.test(value)) return null
  return value
}

function recordDir(env) {
  const home = (env && env.HOME) || ""
  const base = (env && env.XDG_STATE_HOME) || (home ? path.join(home, ".local", "state") : "")
  if (!base) return ""
  return path.join(base, ...RECORD_SUBDIR)
}

function ownLifecycle() {
  // Same lifecycle proof the server publisher uses: the start time guards
  // PID reuse, so a record can only ever match its own process.
  try {
    const pid = typeof process !== "undefined" ? process.pid : 0
    if (!Number.isInteger(pid) || pid <= 1) return null
    const stat = readFileSync("/proc/self/stat", "utf8")
    const body = stat.slice(stat.lastIndexOf(") ") + 2)
    if (stat.lastIndexOf(") ") < 0) return null
    const fields = body.trim().split(/\s+/)
    const start = fields[19]
    const self = Number(stat.slice(0, stat.indexOf(" (")))
    if (self !== pid || !/^\d+$/.test(start || "")) return null
    return { pid, start }
  } catch { return null }
}

function selectedSid(api) {
  try {
    let current = api && api.route && api.route.current
    if (typeof current === "function") current = current()
    if (!current && api && api.ui && api.ui.router
        && typeof api.ui.router.current === "function") {
      current = api.ui.router.current()
    }
    if (!current) return null
    if (current.name === "session") return cleanSid(current.params && current.params.sessionID)
    if (current.type === "session") return cleanSid(current.sessionID)
    return null
  } catch { return null }
}

function lifecycleSource(type, event, sid) {
  const properties = (event && event.properties) || {}
  const info = properties.info || {}
  const eventSid = cleanSid(info.id) || cleanSid(properties.sessionID)
  const explicit = cleanSid(properties.session_start_source) || cleanSid(info.session_start_source)
  if (["startup", "resume", "clear", "compact", "fork"].includes(explicit)) return explicit
  if (type === "session.created" && eventSid === sid) {
    return cleanSid(info.parentID) ? "fork" : "startup"
  }
  if (type === "tui.session.select") {
    if (eventSid && eventSid !== sid) return null
    return "select"
  }
  return null
}

function recordPath(dir, own) {
  if (!dir || !own) return ""
  return path.join(dir, own.pid + "-" + own.start + ".json")
}

function reportResumeOnce(mem, sid, own) {
  if (mem.resumeReported) return
  const argv = process.argv || []
  if (argv.some((arg) => arg === "--fork" || arg === "--fork=true")) return
  const requested = argv.some((arg, i) =>
    ((arg === "--session" || arg === "-s") && argv[i + 1] === sid)
      || arg === `--session=${sid}` || arg === `-s=${sid}`
      || ["--continue", "--last", "-c"].includes(arg))
  if (!requested) return
  mem.resumeReported = true
  const home = process.env.HOME || ""
  const config = process.env.XDG_CONFIG_HOME || path.join(home, ".config")
  const roots = [process.env.AGENT_HOME,
    path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../.."),
    path.join(config, "opencode", "hearting")].filter(Boolean)
  for (const root of roots) {
    try {
      const script = path.join(root, "utilities", "session_resume.py")
      readFileSync(script, { encoding: "utf8", flag: "r" })
      // This bounded helper records native lifecycle only. It never emits or
      // consumes card/context output, and the existing courier owns wake I/O.
      const child = spawn("python3", [script], { stdio: ["pipe", "ignore", "ignore"] })
      child.on("error", () => {})
      child.stdin.on("error", () => {})
      child.stdin.end(JSON.stringify({ harness: "opencode", sid, pid: own.pid, start: own.start }))
      child.unref?.()
      return
    } catch {}
  }
}

function syncRecord(api, mem, sourceHint = null) {
  const env = (typeof process !== "undefined" && process.env) || {}
  const own = ownLifecycle()
  const file = recordPath(recordDir(env), own)
  if (!file || !own) return false
  const sid = selectedSid(api)
  try {
    if (!sid) {
      rmSync(file, { force: true })
      mem.last = ""
      mem.sequence = 0
      mem.source = ""
      return true
    }
    // Change-gated: steady activity re-reads the same selection without
    // churning the record, so the server's pre-publication generation
    // comparison stays stable until the selection actually moves.
    const key = "S:" + sid
    const changed = mem.last !== key || (sourceHint && mem.source !== sourceHint)
    if (!changed) return true
    if (mem.last !== key) {
      mem.last = key
      mem.sequence = Math.max(Date.now() * 1000, (mem.sequence || 0) + 1)
      mem.source = ""
    }
    if (["startup", "resume", "clear", "compact", "fork"].includes(sourceHint)) {
      mem.source = sourceHint
    }
    mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 })
    const record = { schema: RECORD_SCHEMA, sessionID: sid, pid: own.pid, start: own.start,
      sessionSeq: mem.sequence, writtenAt: new Date().toISOString() }
    if (mem.source) record.sessionStartSource = mem.source
    const body = JSON.stringify(record)
    const tmp = file + ".tmp-" + own.pid
    writeFileSync(tmp, body, { encoding: "utf8", mode: 0o600 })
    renameSync(tmp, file)
    reportResumeOnce(mem, sid, own)
    return true
  } catch { return false }
}

function deletedSid(event) {
  // A deleted session's selection is dead whatever the getter still shows:
  // the event carries the deletion truth, the getter carries this TUI's
  // own route, and only their agreement clears. Anything else is untouched.
  // Official shape (pinned 1.18.34 SDK v2 gen types) names the session at
  // properties.info.id; properties.sessionID is read only as a fallback.
  try {
    if (!event || typeof event.type !== "string") return null
    if (event.type !== "session.deleted") return null
    const props = (event && event.properties) || {}
    const info = props.info || {}
    const sid = cleanSid(info.id) || cleanSid(props.sessionID)
    if (!sid) return null
    return sid
  } catch { return null }
}

function clearRecord() {
  const env = (typeof process !== "undefined" && process.env) || {}
  const file = recordPath(recordDir(env), ownLifecycle())
  if (!file) return false
  try {
    rmSync(file, { force: true })
    return true
  } catch { return false }
}

async function tui(api) {
  // V2 invokes setup() with ui.router.current(); keep one producer per API surface.
  if (api && api.ui && api.ui.router && typeof api.ui.router.current === "function") return
  const mem = { last: "", sequence: 0, source: "" }
  const refresh = () => { syncRecord(api, mem) }
  refresh()
  const unsubs = []
  try {
    for (const type of REFRESH_EVENTS) {
      if (type === "session.deleted") unsubs.push(api.event.on(type, (event) => {
        // Deletion truth beats a lagging getter: when the event names the
        // session this TUI still shows, the selection is already dead.
        const gone = deletedSid(event)
        if (gone && selectedSid(api) === gone) clearRecord()
        refresh()
      }))
      else unsubs.push(api.event.on(type, (event) => {
        syncRecord(api, mem, lifecycleSource(type, event, selectedSid(api)))
      }))
    }
  } catch {
    // Partial subscriptions are still disposed below; a broken bus must
    // never block activation.
  }
  // Primary observation: host-driven slot renders run synchronously with
  // the mounted route (verified official in-repo smoke shape: slots record
  // of (ctx, value) render fns; session_prompt_right carries session_id).
  // No bus traffic is needed and none is inferred from: a agreeing
  // session mount writes, a agreeing home mount clears, anything
  // disagreeing is left for the next render or wake. Renders return null
  // and change nothing visible.
  // The app-level render additionally covers routes with no session/home
  // slot (custom plugin routes): it reads only the live getter, so a
  // non-session route clears whatever a previous selection left behind.
  // A render runs inside the host's reactive tree, so a getter read here
  // tracks the native route store and re-runs when the selection moves.
  const slotPlugin = { slots: {
    app_bottom() {
      try {
        const sid = selectedSid(api)
        if (!sid && mem.last !== "") {
          clearRecord()
          mem.last = ""
        } else if (sid) syncRecord(api, mem)
      } catch { /* a broken render must never break the host view */ }
      return null
    },
    session_prompt_right(ctx, value) {
      try {
        const sid = cleanSid(value && value.session_id)
        if (sid && selectedSid(api) === sid) syncRecord(api, mem)
      } catch { /* a broken render must never break the host view */ }
      return null
    },
    home_prompt_right() {
      try {
        if (!selectedSid(api) && mem.last !== "") {
          clearRecord()
          mem.last = ""
        }
      } catch { /* a broken render must never break the host view */ }
      return null
    },
  } }
  try {
    api.slots.register(slotPlugin)
  } catch {
    // Hosts without slot registration keep the bus wake-assist path.
  }
  const done = () => {
    for (const off of unsubs) {
      try { off() } catch { /* teardown is best-effort */ }
    }
    clearRecord()
  }
  try {
    api.lifecycle.onDispose(done)
  } catch {
    // The host still owns teardown; without a disposer the record simply
    // stops being refreshed and never matches a new lifecycle.
  }
}

function setup(api) {
  if (!api || !api.ui || !api.ui.router || typeof api.ui.router.current !== "function") return
  const mem = { last: "", sequence: 0, source: "" }
  const refresh = () => { syncRecord(api, mem) }
  refresh()
  const unsubs = []
  try {
    for (const type of REFRESH_EVENTS) {
      unsubs.push(api.event.on(type, (event) => {
        const sid = selectedSid(api)
        if (type === "session.deleted" && deletedSid(event) === sid) clearRecord()
        syncRecord(api, mem, lifecycleSource(type, event, sid))
      }))
    }
  } catch {
    // A partial event bus is repaired by the route poll below.
  }
  const poll = setInterval(refresh, 100)
  poll.unref?.()
  const done = () => {
    clearInterval(poll)
    for (const off of unsubs) {
      try { off() } catch { /* teardown is best-effort */ }
    }
    clearRecord()
  }
  try {
    api.lifecycle.onDispose(done)
  } catch {
    // A missing disposer leaves only this process's bounded identity record.
  }
  return done
}

export default { id: PLUGIN_ID, setup, tui }
